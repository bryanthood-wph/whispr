"""The task funnel (stream E): review changes in kg/store.py, the funnel report and CLI
(kg/tasks.py), and the task MCP server (kg/mcp_tasks.py) beside the read-only reader.
No model calls; every database lives in a temp data dir."""

from __future__ import annotations

import contextlib
import io
import copy
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from kg import db, tasks
from kg.mcp_server import Server
from kg.mcp_tasks import ACTOR, TASK_LINK_TABLES, TASK_REVIEW_TABLES, TaskServer
from kg.store import BriefOpenError, Store, StoreError, TransitionError, task_id
from pipeline.config import ConfigError, check_intake, load_config, required_fields
from pipeline_helpers import answer_brief, budget_answer, overlay

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
OLD_EP, NEW_EP = "ep-2026-09-28-1000", "ep-2026-10-01-0900"
WHO = "tester"
REVIEW_TOOLS = ["project_scope_get", "project_scope_set", "task_get", "task_list", "task_update_status"]
INSERT = re.compile(r'INSERT INTO "?(\w+)"?')      # a row in an iterdump


class TaskCase(unittest.TestCase):
    """A fresh config and migrated database with two episodes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ov = overlay(self.root)
        self.cfg = load_config(overlay=self.ov)
        self.conn = db.connect(self.cfg)
        self.addCleanup(self.conn.close)
        self.store = Store(self.conn, self.cfg)
        for ep, start in ((OLD_EP, "2026-09-28T10:00:00+00:00"), (NEW_EP, "2026-10-01T09:00:00+00:00")):
            self.store.upsert_episode(ep, transcript_path=f"transcripts/{ep}.md", sha256="ab" * 32,
                                      meeting_start=start, call_type="meeting", now=T0)

    def add(self, quote: str = "I'll send the deck to Jamie by Friday", *, episode: str = NEW_EP,
            now: datetime = T0, **kw) -> str:
        t = {"id": task_id(episode, quote), "owner": "Pat Example", "owner_basis": "volunteered",
             "action": "Send the deck to Jamie", "due": "Friday", "due_basis": "stated", "context": "review copy",
             "quote": quote, "source": {"episode": episode, "start": "00:00:01"}, "confidence": 0.9,
             "status": "captured", "tools_allowed": []}
        t.update(kw)
        return self.store.add_task(t, now=now)

    def events(self, tid: str) -> list[tuple]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT from_status, to_status, actor, note FROM task_event WHERE task_id = ? ORDER BY at, rowid", (tid,))]

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestReviewChanges(TaskCase):
    def test_allowed_transition_records_who_why_when(self):
        tid = self.add()
        t1 = T0 + timedelta(hours=2)
        record = self.store.update_task(tid, actor=WHO, status="confirmed", reason="it is mine", now=t1)
        self.assertEqual(record["task"]["status"], "confirmed")
        self.assertEqual(self.events(tid), [(None, "captured", None, None),
                                            ("captured", "confirmed", WHO, "it is mine")])
        last = record["events"][-1]
        self.assertEqual((last["actor"], last["reason"], last["at"]), (WHO, "it is mine", db.utc_now(t1)))
        self.assertEqual(self.conn.execute("SELECT updated_at FROM task WHERE id = ?", (tid,)).fetchone()[0],
                         db.utc_now(t1))

    def test_blocked_transition_names_the_allowed_states_and_changes_nothing(self):
        tid = self.add()
        with self.assertRaises(TransitionError) as ctx:
            self.store.update_task(tid, actor=WHO, status="done", reason="did it", clarification="x")
        self.assertEqual(ctx.exception.allowed, ["confirmed", "dropped"])
        self.assertIn("confirmed, dropped", str(ctx.exception))
        self.assertEqual(self.store.get_task(tid)["status"], "captured")
        self.assertEqual((len(self.events(tid)), self.count("task_note")), (1, 0))
        with self.assertRaises(TransitionError):        # a status is not a transition to itself
            self.store.update_task(tid, actor=WHO, status="captured", reason="again")

    def test_done_is_final(self):
        tid = self.add()
        answer_brief(self.store, tid, WHO)
        for status in ("confirmed", "ready", "done"):
            self.store.update_task(tid, actor=WHO, status=status, reason="step")
        with self.assertRaises(TransitionError) as ctx:
            self.store.update_task(tid, actor=WHO, status="captured", reason="reopen")
        self.assertEqual(ctx.exception.allowed, [])
        self.assertIn("none (it is final)", str(ctx.exception))

    def test_set_task_status_keeps_its_contract(self):
        tid = self.add()
        self.store.set_task_status(tid, "dropped", note="dup")
        self.assertEqual(self.events(tid)[-1], ("captured", "dropped", None, "dup"))

    def test_clarification_and_inputs_are_rows_not_files(self):
        tid = self.add()
        files_before = sorted(p.name for p in self.root.rglob("*") if p.is_file())
        record = self.store.update_task(tid, actor=WHO, clarification="due is Thursday, not Friday",
                                        inputs=["C:/decks/q4.pptx", "https://example.com/brief"])
        self.assertEqual(record["task"]["status"], "captured")      # no status change, no event
        self.assertEqual(len(record["events"]), 1)
        self.assertEqual([(n["text"], n["actor"]) for n in record["clarifications"]],
                         [("due is Thursday, not Friday", WHO)])
        self.assertEqual([n["text"] for n in record["inputs"]], ["C:/decks/q4.pptx", "https://example.com/brief"])
        files_after = sorted(p.name for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(files_after, files_before)                 # L19: the database only

    def test_tools_allowed_only_when_marking_ready(self):
        tid = self.add()
        with self.assertRaises(StoreError):
            self.store.update_task(tid, actor=WHO, status="confirmed", reason="mine", tools_allowed=["Read"])
        self.store.update_task(tid, actor=WHO, status="confirmed", reason="mine")
        answer_brief(self.store, tid, WHO)
        for bad in ([1], ["Read", " "], "Read"):
            with self.assertRaises(StoreError):
                self.store.update_task(tid, actor=WHO, status="ready", reason="go", tools_allowed=bad)
        self.assertEqual(self.store.get_task(tid)["status"], "confirmed")
        record = self.store.update_task(tid, actor=WHO, status="ready", reason="go",
                                        tools_allowed=["Read", "Write(out/)", "Read"])
        self.assertEqual(record["task"]["tools_allowed"], ["Read", "Write(out/)"])
        self.assertEqual(record["events"][-1]["details"], {"tools_allowed": ["Read", "Write(out/)"]})
        # Back to confirmed (scope changed) and ready again without tools grants none,
        # and the event says so.
        self.store.update_task(tid, actor=WHO, status="confirmed", reason="rethink")
        again = self.store.update_task(tid, actor=WHO, status="ready", reason="go")
        self.assertEqual(again["task"]["tools_allowed"], [])
        self.assertEqual(again["events"][-1]["details"], {"tools_allowed": []})

    def test_the_status_is_checked_inside_the_write_transaction(self):
        """BEGIN IMMEDIATE comes before the read, so another writer cannot commit between
        the check and the write (two review servers, or a worker beside one)."""
        tid = self.add()
        seen = []
        allowed = self.store.allowed_transitions
        def spy(status):
            seen.append(self.conn.in_transaction)
            return allowed(status)
        self.store.allowed_transitions = spy
        self.store.update_task(tid, actor=WHO, status="confirmed", reason="mine")
        self.assertTrue(seen)
        self.assertTrue(all(seen), seen)

    def test_task_config_is_consistent(self):
        for key, value in (("review_statuses", ["captured", "finished"]),
                           ("confirm_owner_basis", ["others"])):
            cfg = copy.deepcopy(self.cfg)
            cfg["kg"]["tasks"][key] = value
            with self.assertRaises(StoreError, msg=key):
                Store(self.conn, cfg)

    def test_bad_requests_are_refused(self):
        tid = self.add()
        cases = [dict(),                                            # nothing to change
                 dict(inputs="C:/one/path"),                         # a string, not a list
                 dict(clarification="  "),
                 dict(status="confirmed", reason=""),
                 dict(inputs=["x"], actor=None)]
        for kw in cases:
            kw.setdefault("actor", WHO)
            with self.subTest(kw=kw), self.assertRaises(StoreError):
                self.store.update_task(tid, **kw)
        with self.assertRaises(StoreError):
            self.store.update_task("no-such-task", actor=WHO, status="confirmed", reason="x")
        self.assertEqual((len(self.events(tid)), self.count("task_note")), (1, 0))

    def test_config_must_name_schema_values(self):
        for key, value in (("confirm_owner_basis", ["maybe"]), ("mine_owner_basis", ["nobody"]),
                           ("tools_status", "launched")):
            cfg = {**self.cfg, "kg": {**self.cfg["kg"], "tasks": {**self.cfg["kg"]["tasks"], key: value}}}
            with self.subTest(key=key), self.assertRaises(StoreError):
                Store(self.conn, cfg)


class TestReviewList(TaskCase):
    def test_confirm_first_then_newest_meeting(self):
        old_unclear = self.add("maybe I'll look at it", episode=OLD_EP, owner_basis="unclear")
        old = self.add("I'll book the room", episode=OLD_EP)
        new = self.add("I'll send the deck to Jamie by Friday")
        others = self.add("Jamie will price it", owner_basis="others", owner="Jamie Doe")
        listed = self.store.review_tasks(status="captured", limit=10)
        self.assertEqual([c["task"]["id"] for c in listed["tasks"]], [old_unclear, new, old])
        self.assertEqual([c["confirm"] for c in listed["tasks"]], [True, False, False])
        self.assertEqual(listed["tasks"][0]["meeting_start"], "2026-09-28T10:00:00.000000+00:00")
        everyone = self.store.review_tasks(status="captured", mine=False, limit=10)
        self.assertIn(others, [c["task"]["id"] for c in everyone["tasks"]])
        only = self.store.review_tasks(status="captured", confirm_only=True, limit=10)
        self.assertEqual([c["task"]["id"] for c in only["tasks"]], [old_unclear])
        page = self.store.review_tasks(status="captured", limit=2, offset=1)
        self.assertEqual(([c["task"]["id"] for c in page["tasks"]], page["total"], page["truncated"]),
                         ([new, old], 3, False))
        self.assertTrue(self.store.review_tasks(status="captured", limit=1)["truncated"])
        with self.assertRaises(StoreError):
            self.store.review_tasks(status="parked", limit=1)

    def test_confirm_flag_ends_when_the_task_moves_on(self):
        tid = self.add(owner_basis="unclear")
        self.assertTrue(self.store.task_record(tid)["confirm"])
        self.assertEqual(self.store.confirm_open(), 1)
        self.store.update_task(tid, actor=WHO, status="confirmed", reason="it is mine")
        self.assertFalse(self.store.task_record(tid)["confirm"])
        self.assertEqual(self.store.confirm_open(), 0)


class TestFunnel(TaskCase):
    def test_a_task_inserted_dropped_is_counted_as_captured(self):
        self.add(status="dropped")
        self.assertEqual(self.store.funnel(), {"captured": 1, "confirmed": 0, "ready": 0, "done": 0})
        self.assertEqual(self.store.task_status_counts()["dropped"], 1)

    def test_report_window_and_conversion(self):
        now = T0 + timedelta(days=3)
        old = self.add("I'll do the old thing", now=T0 - timedelta(days=10))          # before the window
        later = self.add("I'll do the later thing", now=now + timedelta(hours=1))      # after it
        ids = [self.add(f"I'll do thing {i}", now=T0 + timedelta(hours=i)) for i in range(4)]
        self.add("Jamie will do it", owner_basis="others", owner="Jamie Doe", now=T0)
        self.add("maybe me", owner_basis="unclear", now=T0)
        for tid in (ids[0], ids[1], old):
            answer_brief(self.store, tid, WHO)
        for status in ("confirmed", "ready", "done"):
            self.store.update_task(ids[0], actor=WHO, status=status, reason="s")
        for status in ("confirmed", "ready"):
            self.store.update_task(ids[1], actor=WHO, status=status, reason="s")
            self.store.update_task(old, actor=WHO, status=status, reason="s")
        self.store.update_task(ids[2], actor=WHO, status="confirmed", reason="s")
        self.store.update_task(ids[2], actor=WHO, status="dropped", reason="dup")
        self.store.update_task(ids[3], actor=WHO, status="dropped", reason="not mine")
        self.store.update_task(later, actor=WHO, status="confirmed", reason="s")
        report = tasks.funnel_report(self.store, days=7, now=now)
        self.assertEqual(report["stages"], {"captured": 5, "confirmed": 3, "ready": 2, "done": 1})
        self.assertEqual(report["conversion"], [
            {"from": "captured", "to": "confirmed", "rate": 0.6},
            {"from": "confirmed", "to": "ready", "rate": 0.6667},
            {"from": "ready", "to": "done", "rate": 0.5}])
        self.assertEqual(report["overall"], {"from": "captured", "to": "done", "rate": 0.2})
        self.assertEqual(report["status_now"], {"captured": 1, "confirmed": 0, "ready": 1, "in_progress": 0,
                                                "done": 1, "dropped": 2})
        self.assertEqual(report["others_captured"], 1)
        self.assertEqual(report["confirm_open"], 1)
        self.assertEqual((report["since"], report["until"]), (db.utc_now(now - timedelta(days=7)), db.utc_now(now)))
        json.dumps(report)

    def test_empty_window_has_null_rates(self):
        report = tasks.funnel_report(self.store, days=1, now=T0)
        self.assertEqual(set(report["stages"].values()), {0})
        self.assertTrue(all(step["rate"] is None for step in report["conversion"]))
        self.assertIsNone(report["overall"]["rate"])
        with self.assertRaises(ValueError):
            tasks.funnel_report(self.store, days=0, now=T0)

    def test_text_fits_one_screen(self):
        self.add(now=T0)
        text = tasks.render_funnel(tasks.funnel_report(self.store, days=7, now=T0 + timedelta(days=1)))
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 24)
        self.assertLessEqual(max(map(len, lines)), 100)
        self.assertIn("captured", text)
        self.assertIn("n/a of confirmed", text)        # no confirmed task: no rate


class TestFunnelCli(TaskCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        path = self.root / "overlay.yaml"
        path.write_text(yaml.safe_dump(self.ov), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tasks.main(["funnel", "--config", str(path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_json_and_text(self):
        self.add(now=datetime.now(timezone.utc))
        code, out, _ = self.run_cli("--format", "json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["window_days"], self.cfg["kg"]["tasks"]["funnel_days"])
        self.assertEqual(report["stages"]["captured"], 1)
        code, out, _ = self.run_cli("--days", "30")
        self.assertEqual(code, 0)
        self.assertIn("last 30 days", out)

    def test_bad_days_and_missing_database(self):
        self.assertEqual(self.run_cli("--days", "0")[0], 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overlay.yaml"
            path.write_text(yaml.safe_dump(overlay(Path(tmp))), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(tasks.main(["funnel", "--config", str(path)]), 1)
        self.assertIn("no database", err.getvalue())


class TestTaskServer(TaskCase):
    def setUp(self):
        super().setUp()
        self.unclear = self.add("maybe I'll look at it", episode=OLD_EP, owner_basis="unclear")
        self.mine = self.add()
        self.reader_conn = db.connect_readonly(self.cfg)
        self.addCleanup(self.reader_conn.close)
        self.writer_conn = db.connect_limited(self.cfg, TASK_REVIEW_TABLES, insert_only=TASK_LINK_TABLES)
        self.addCleanup(self.writer_conn.close)
        self.server = TaskServer(self.reader_conn, self.writer_conn, self.cfg)

    def call(self, name: str, args: dict) -> tuple[bool, object]:
        result = self.server.call_tool(name, args)
        return result["isError"], json.loads(result["content"][0]["text"])

    def test_tools_list_holds_the_review_tools_only(self):
        tools = self.server.dispatch("tools/list", {})["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), REVIEW_TOOLS)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertGreater(len(tool["description"]), 80, tool["name"])
        update = next(t for t in tools if t["name"] == "task_update_status")
        self.assertEqual(update["inputSchema"]["properties"]["status"]["enum"],
                         self.cfg["kg"]["tasks"]["review_statuses"])
        self.assertEqual(self.server.dispatch("initialize", {})["serverInfo"]["name"], "whispr-tasks")

    def test_list_and_get(self):
        bad, listed = self.call("task_list", {})
        self.assertFalse(bad)
        self.assertEqual([c["task"]["id"] for c in listed["tasks"]], [self.unclear, self.mine])
        bad, record = self.call("task_get", {"id": self.mine})
        self.assertEqual((bad, record["task"]["id"]), (False, self.mine))
        bad, missing = self.call("task_get", {"id": "nope"})
        self.assertTrue(bad)

    def test_review_cannot_set_the_workers_statuses(self):
        answer_brief(self.store, self.mine, WHO)
        for status in ("confirmed", "ready"):
            self.assertFalse(self.call("task_update_status", {"id": self.mine, "status": status, "reason": "go"})[0])
        worker = sorted(set(self.store.task_schema["properties"]["status"]["enum"])
                        - set(self.cfg["kg"]["tasks"]["review_statuses"]))
        self.assertTrue(worker)
        for status in worker:
            bad, answer = self.call("task_update_status", {"id": self.mine, "status": status, "reason": "did it"})
            self.assertTrue(bad, status)
        self.assertEqual(self.store.get_task(self.mine)["status"], "ready")

    def test_update_succeeds_and_is_recorded(self):
        bad, record = self.call("task_update_status", {"id": self.unclear, "status": "confirmed",
                                                       "reason": "yes, mine", "clarification": "due Monday",
                                                       "inputs": ["C:/notes/brief.docx"]})
        self.assertFalse(bad, record)
        self.assertEqual(record["task"]["status"], "confirmed")
        self.assertEqual(record["events"][-1]["actor"], ACTOR)
        self.assertEqual(record["clarifications"][0]["text"], "due Monday")
        self.assertEqual(self.store.get_task(self.unclear)["status"], "confirmed")    # visible to other readers

    def test_invalid_transition_is_an_error_naming_allowed_states(self):
        bad, payload = self.call("task_update_status", {"id": self.mine, "status": "ready", "reason": "go"})
        self.assertTrue(bad)
        self.assertEqual(payload["allowed"], ["confirmed", "dropped"])
        self.assertEqual(payload["status"], "captured")
        self.assertIn("confirmed, dropped", payload["error"])
        self.assertEqual(self.store.get_task(self.mine)["status"], "captured")

    def test_schema_errors(self):
        for args in ({"status": "confirmed", "reason": "x"},                     # no id
                     {"id": self.mine, "status": "parked", "reason": "x"},       # not a status
                     {"id": self.mine, "inputs": []},                            # minItems
                     {"id": self.mine, "tools_allowed": "Read"},                 # not an array
                     {"id": self.mine, "owner": "me"}):                          # not a field
            with self.subTest(args=args):
                bad, payload = self.call("task_update_status", args)
                self.assertTrue(bad)
                self.assertIn("input schema", payload["error"])
        bad, payload = self.call("task_update_status", {"id": self.mine, "status": "confirmed"})
        self.assertTrue(bad)
        self.assertIn("reason", payload["error"])
        bad, payload = self.call("task_update_status", {"id": self.mine})
        self.assertTrue(bad)
        self.assertIn("nothing to change", payload["error"])
        self.assertEqual(len(self.events(self.mine)), 1)

    def test_the_writer_reaches_task_review_tables_only(self):
        statements = [
            "INSERT INTO entity (id, type, canonical_name, canonical_key, created_at) VALUES ('x', 'topic', 'X', 'x', 'now')",
            "UPDATE episode SET deleted_at = 'now'",
            "DELETE FROM task_transition",
            "UPDATE task_status SET funnel_rank = 9",
            "CREATE TABLE sneaky (id TEXT)",
            "DROP TABLE task_note",
            "PRAGMA foreign_keys = OFF",
            f"ATTACH DATABASE '{(self.root / 'other.db').as_posix()}' AS other",
        ]
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError) as ctx:
                self.writer_conn.execute(sql)
            self.assertIn("not authorized", str(ctx.exception))
        self.assertFalse((self.root / "other.db").exists())
        self.writer_conn.execute("UPDATE task SET updated_at = updated_at WHERE id = ?", (self.mine,))

    def test_the_reader_server_still_refuses_writes(self):
        reader = Server(self.reader_conn, self.cfg)
        self.assertNotIn("task_update_status", reader.tools)
        self.assertEqual(sorted(reader.tools), ["get", "neighbors", "paths", "search", "source", "timeline"])
        with self.assertRaises(sqlite3.OperationalError):
            self.reader_conn.execute("UPDATE task SET status = 'confirmed'")
        bad, _ = self.call("task_get", {"id": self.mine})
        self.assertFalse(bad)


class IntakeCase(TaskCase):
    """A captured task, confirmed, and the intake's names from config (never literals)."""

    def setUp(self):
        super().setUp()
        self.intake = self.cfg["tasks"]["intake"]
        self.tid = self.add()
        self.store.update_task(self.tid, actor=WHO, status="confirmed", reason="mine")
        self.ready = self.intake["ready_status"]
        self.stated, self.confirmed = self.intake["answer_sources"][:2]
        self.none = self.intake["scope_none"]
        self.types = list(self.intake["scope_types"])
        self.text_fields = [f for f in self.store.required_fields
                            if f not in (self.store.scope_field, self.store.budget_field)]
        self.budget = self.store.budget_field

    def project(self, name: str = "Apollo") -> str:
        return self.store.upsert_entity(self.intake["scope_entity_type"], name, source="test", now=T0)

    def other_type(self) -> str:
        return next(t for t in sorted(self.store.entity_types) if t not in ("person", self.intake["scope_entity_type"]))


class TestBrief(IntakeCase):
    def test_ready_is_refused_while_a_required_field_is_open(self):
        first, *rest = self.text_fields
        self.store.update_task(self.tid, actor=WHO, brief={first: "a one-page memo"}, brief_source=self.stated)
        notes = self.count("task_note")
        with self.assertRaises(BriefOpenError) as ctx:
            self.store.update_task(self.tid, actor=WHO, status=self.ready, reason="go",
                                   brief={rest[0]: "the steering group"}, brief_source=self.stated)
        self.assertEqual(ctx.exception.open, [f for f in self.store.required_fields if f not in (first, rest[0])])
        for field in ctx.exception.open:
            self.assertIn(field, str(ctx.exception))
        self.assertEqual(self.store.get_task(self.tid)["status"], "confirmed")
        self.assertEqual(self.count("task_note"), notes)                # all or nothing: the call's answer too
        # Every field answered, in the same call as the move: allowed.
        record = self.store.update_task(self.tid, actor=WHO, status=self.ready, reason="go",
                                        brief={**{f: f"the {f}" for f in rest},
                                               self.budget: budget_answer(self.store)},
                                        scope=[{"type": t, "value": self.none} for t in self.types],
                                        brief_source=self.confirmed)
        self.assertEqual((record["task"]["status"], record["open_fields"]), (self.ready, []))

    def test_newest_answer_wins_and_the_history_is_kept(self):
        field = self.text_fields[0]
        t1, t2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
        self.store.update_task(self.tid, actor=WHO, brief={field: "a deck"}, brief_source=self.stated, now=t1)
        record = self.store.update_task(self.tid, actor=WHO, brief={field.upper(): "  a memo  "},
                                        brief_source=self.confirmed.upper(), now=t2)
        self.assertEqual(record["brief"][field], {"value": "a memo", "source": self.confirmed, "actor": WHO,
                                                  "at": db.utc_now(t2)})
        self.assertEqual([(h["field"], h["value"], h["source"]) for h in record["brief_history"]],
                         [(field, "a deck", self.stated), (field, "a memo", self.confirmed)])
        self.assertNotIn(field, record["open_fields"])
        self.assertEqual(record["open_fields"], [f for f in self.store.required_fields if f != field])
        self.assertEqual((record["clarifications"], record["inputs"]), ([], []))     # kept apart

    def test_task_get_lists_the_open_fields(self):
        record = self.store.task_record(self.tid)
        self.assertEqual((record["brief"], record["brief_history"]), ({}, []))
        self.assertEqual(record["open_fields"], self.store.required_fields)
        answer_brief(self.store, self.tid, WHO)
        record = self.store.task_record(self.tid)
        self.assertEqual(record["open_fields"], [])
        self.assertEqual(record["brief"][self.store.scope_field]["value"],
                         [{"type": t, "value": self.none} for t in self.types])

    def test_bad_answers_are_refused_and_name_the_problem(self):
        field = self.text_fields[0]
        cases = [(dict(brief={"audiance": "x"}, brief_source=self.stated), "audiance"),
                 (dict(brief={self.store.scope_field: "everything"}, brief_source=self.stated), "scope"),
                 (dict(brief={field: "  "}, brief_source=self.stated), field),
                 (dict(brief={}, brief_source=self.stated), "at least one"),
                 (dict(brief={field: "x"}), "brief_source"),
                 (dict(brief={field: "x"}, brief_source="guessed"), "guessed"),
                 (dict(clarification="x", brief_source=self.stated), "only with brief"),
                 (dict(brief={field: "x"}, brief_source=self.stated, actor=None), "actor")]
        for kw, named in cases:
            kw.setdefault("actor", WHO)
            with self.subTest(kw=kw), self.assertRaises(StoreError) as ctx:
                self.store.update_task(self.tid, **kw)
            self.assertIn(named, str(ctx.exception))
        self.assertEqual(self.store.brief_history(self.tid), [])


class TestBudgetAndWorkType(IntakeCase):
    def test_every_defined_field_is_required_by_default(self):
        self.assertEqual(sorted(self.store.required_fields), sorted(self.intake["fields"]))

    def test_budget_is_an_amount_and_a_reason_stored_as_json(self):
        keys = self.intake["budget"]
        answer = budget_answer(self.store, amount=12.5, reason="  two drafts and a review  ")
        record = self.store.update_task(self.tid, actor=WHO, brief={self.budget: answer}, brief_source=self.stated)
        want = {keys["amount_key"]: answer[keys["amount_key"]], keys["reason_key"]: "two drafts and a review"}
        self.assertEqual(record["brief"][self.budget]["value"], want)
        text = self.conn.execute("SELECT text FROM task_note WHERE field = ?", (self.budget,)).fetchone()[0]
        self.assertEqual(json.loads(text), want)
        self.assertNotIn(self.budget, record["open_fields"])

    def test_bad_budgets_are_refused_naming_the_key(self):
        amount, reason, floor = (self.intake["budget"][k] for k in ("amount_key", "reason_key", "amount_above_usd"))
        bad = [({amount: floor, reason: "x"}, amount), ({amount: floor - 1, reason: "x"}, amount),
               ({amount: "5", reason: "x"}, amount), ({amount: True, reason: "x"}, amount),
               ({amount: float("inf"), reason: "x"}, amount), ({amount: floor + 1, reason: " "}, reason),
               ({amount: floor + 1}, reason), ({amount: floor + 1, reason: "x", "extra": 1}, amount),
               ("5 dollars", amount)]
        for value, named in bad:
            with self.subTest(value=value), self.assertRaises(StoreError) as ctx:
                self.store.update_task(self.tid, actor=WHO, brief={self.budget: value}, brief_source=self.stated)
            self.assertIn(named, str(ctx.exception))
        self.assertEqual(self.store.brief_history(self.tid), [])

    def test_budget_field_must_be_named_apart_from_the_scope(self):
        for key, value in (("budget_field", self.intake["scope_field"]), ("budget_field", "budjet")):
            cfg = copy.deepcopy(self.cfg)
            cfg["tasks"]["intake"][key] = value
            with self.subTest(value=value), self.assertRaises(ConfigError) as ctx:
                check_intake(cfg)
            self.assertIn(key, str(ctx.exception))


class TestProjectLink(IntakeCase):
    def test_a_task_is_linked_to_its_project(self):
        pid = self.project()
        record = self.store.update_task(self.tid, actor=WHO, project=pid)
        self.assertEqual(record["projects"], [{"id": pid, "name": "Apollo"}])
        self.store.update_task(self.tid, actor=WHO, project=pid)               # again: no second link
        self.assertEqual(self.store.task_entities(self.tid), [pid])
        self.assertEqual(len(self.events(self.tid)), 2)                         # no status event for a link

    def test_a_merged_project_links_its_survivor(self):
        keep, drop = self.project("Apollo"), self.project("Apollo Program")
        self.conn.execute("UPDATE entity SET merged_into = ? WHERE id = ?", (keep, drop))
        self.store.update_task(self.tid, actor=WHO, project=drop)
        self.assertEqual(self.store.task_entities(self.tid), [keep])

    def test_only_a_project_can_be_linked(self):
        other = self.store.upsert_entity(self.other_type(), "Billing System", source="test")
        for entity in (other, "no-such-entity"):
            with self.subTest(entity=entity), self.assertRaises(StoreError):
                self.store.update_task(self.tid, actor=WHO, project=entity,
                                       brief={self.text_fields[0]: "x"}, brief_source=self.stated)
        self.assertEqual(self.store.task_entities(self.tid), [])
        self.assertEqual(self.store.brief_history(self.tid), [])               # all or nothing


class TestScope(IntakeCase):
    def test_scope_types_are_checked_and_normalized(self):
        first, second = self.types[:2]
        record = self.store.update_task(self.tid, actor=WHO, brief_source=self.stated, scope=[
            {"type": first.upper(), "value": " C:/work/apollo "}, {"type": first, "value": "C:/work/apollo"},
            {"type": second, "value": self.none.upper()}])
        self.assertEqual(record["brief"][self.store.scope_field]["value"],
                         [{"type": first, "value": "C:/work/apollo"}, {"type": second, "value": self.none}])
        bad = [([{"type": "fax", "value": "x"}], "fax"),
               ([{"type": first, "value": self.none}, {"type": first, "value": "C:/x"}], "stands alone"),
               ([], "empty scope"),
               ([{"type": first}], "type and value"),
               ([{"type": first, "value": ""}], "non-empty"),
               ("C:/x", "list")]
        for scope, named in bad:
            with self.subTest(scope=scope), self.assertRaises(StoreError) as ctx:
                self.store.update_task(self.tid, actor=WHO, scope=scope, brief_source=self.stated)
            self.assertIn(named, str(ctx.exception))
        self.assertEqual(len(self.store.brief_history(self.tid)), 1)

    def test_project_scope_get_set_and_replace(self):
        pid = self.project()
        empty = self.store.entity_scope(pid)
        self.assertEqual((empty["entity_id"], empty["name"], empty["scope"], empty["open_types"]),
                         (pid, "Apollo", [], self.types))
        first, second = self.types[:2]
        got = self.store.set_entity_scope(pid, [{"type": first, "value": "C:/apollo"},
                                                {"type": second, "value": self.none}], now=T0)
        self.assertEqual(got["scope"], sorted([{"type": first, "value": "C:/apollo"},
                                               {"type": second, "value": self.none}],
                                              key=lambda s: (s["type"], s["value"])))
        self.assertEqual(got["open_types"], self.types[2:])
        # Replacing keeps a row that stays (its created_at too) and removes the rest.
        later = T0 + timedelta(days=1)
        got = self.store.set_entity_scope(pid, [{"type": first, "value": "C:/apollo"},
                                                {"type": first, "value": "C:/apollo-2"}], now=later)
        self.assertEqual([s["value"] for s in got["scope"]], ["C:/apollo", "C:/apollo-2"])
        stamps = dict(self.conn.execute("SELECT value, created_at FROM entity_scope WHERE entity_id = ?", (pid,)))
        self.assertEqual(stamps, {"C:/apollo": db.utc_now(T0), "C:/apollo-2": db.utc_now(later)})
        self.assertEqual(self.store.set_entity_scope(pid, [])["scope"], [])        # an empty list clears it
        with self.assertRaises(StoreError) as ctx:
            self.store.set_entity_scope(pid, [{"type": "fax", "value": "x"}])
        self.assertIn("fax", str(ctx.exception))

    def test_only_a_project_carries_a_scope(self):
        other = self.store.upsert_entity(self.other_type(), "Billing System", source="test")
        for call in (lambda: self.store.entity_scope(other),
                     lambda: self.store.set_entity_scope(other, [{"type": self.types[0], "value": "x"}])):
            with self.assertRaises(StoreError) as ctx:
                call()
            self.assertIn(self.intake["scope_entity_type"], str(ctx.exception))
        with self.assertRaises(StoreError):
            self.store.entity_scope("no-such-entity")
        self.assertEqual(self.count("entity_scope"), 0)

    def test_scope_follows_a_merge(self):
        keep, drop = self.project("Apollo"), self.project("Apollo Program")
        self.store.set_entity_scope(drop, [{"type": self.types[0], "value": "C:/old"}])
        self.conn.execute("UPDATE entity SET merged_into = ? WHERE id = ?", (keep, drop))
        self.assertEqual(self.store.entity_scope(drop)["entity_id"], keep)
        self.assertEqual([s["value"] for s in self.store.entity_scope(keep)["scope"]], ["C:/old"])
        self.store.set_entity_scope(keep, [{"type": self.types[0], "value": "C:/new"}])
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT entity_id, value FROM entity_scope")],
                         [(keep, "C:/new")])


class TestIntakeConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ov = overlay(Path(tmp.name))
        self.cfg = load_config(overlay=self.ov)

    def load(self, **intake) -> dict:
        return load_config(overlay={**self.ov, "tasks": {"intake": intake}})

    def test_required_fields_empty_or_misspelt_is_a_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            self.load(required_fields=[])
        self.assertIn("required_fields", str(ctx.exception))
        known = list(self.cfg["tasks"]["intake"]["fields"])
        with self.assertRaises(ConfigError) as ctx:
            self.load(required_fields=[known[0], "audiance"])
        self.assertIn("audiance", str(ctx.exception))
        with self.assertRaises(ConfigError) as ctx:
            self.load(scope_field="scoep")
        self.assertIn("scoep", str(ctx.exception))

    def test_names_match_with_letter_case_ignored(self):
        known = list(self.cfg["tasks"]["intake"]["fields"])
        cfg = self.load(required_fields=[known[1].upper(), known[0]])
        self.assertEqual(required_fields(cfg), [known[1], known[0]])

    def test_empty_or_case_clashing_names_are_config_errors(self):
        for key, value in (("scope_types", {}), ("fields", {}),
                           ("scope_types", {"Email": "a", "email": "b"})):
            cfg = copy.deepcopy(self.cfg)
            cfg["tasks"]["intake"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError) as ctx:
                check_intake(cfg)
            self.assertIn(key, str(ctx.exception))

    def test_store_checks_the_status_and_entity_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(overlay=overlay(Path(tmp)))
            conn = db.connect(cfg)
            try:
                for key, value in (("ready_status", "launched"), ("scope_entity_type", "planet")):
                    bad = copy.deepcopy(cfg)
                    bad["tasks"]["intake"][key] = value
                    with self.subTest(key=key), self.assertRaises(StoreError) as ctx:
                        Store(conn, bad)
                    self.assertIn(value, str(ctx.exception))
            finally:
                conn.close()


class TestIntakeMigration(unittest.TestCase):
    """Migration 0007 on a database at the version before it, with review rows in it."""

    def test_old_database_migrates_with_its_notes_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(overlay=overlay(root))
            new = max(m.version for m in db.migrations())
            old_dir = root / "old_migrations"
            old_dir.mkdir()
            for m in db.migrations():
                if m.version < new and m.path is not None:
                    (old_dir / m.path.name).write_bytes(m.path.read_bytes())
            conn = sqlite3.connect(root / "old.db", isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                db.migrate(conn, old_dir)
                self.assertNotIn(new, db.applied_versions(conn))
                old = Store(conn, cfg)
                old.upsert_episode(NEW_EP, transcript_path="t.md", sha256="ab" * 32,
                                   meeting_start="2026-10-01T09:00:00+00:00", call_type="meeting", now=T0)
                quote = "I'll send the deck to Jamie by Friday"
                tid = old.add_task({"id": task_id(NEW_EP, quote), "owner": "Pat Example", "owner_basis": "volunteered",
                                    "action": "Send the deck", "due": None, "due_basis": "not_stated", "context": "",
                                    "quote": quote, "source": {"episode": NEW_EP, "start": "00:00:01"},
                                    "confidence": 0.9, "status": "captured", "tools_allowed": []}, now=T0)
                notes = (("clarification", "due Monday"), ("input", "C:/a"), ("input", "C:/b"))
                for i, (kind, text) in enumerate(notes):
                    conn.execute("INSERT INTO task_note (id, task_id, kind, text, actor, at) VALUES (?, ?, ?, ?, ?, ?)",
                                 (f"n{i}", tid, kind, text, WHO, db.utc_now(T0)))    # rows as 0006 wrote them
                before = [tuple(r) for r in conn.execute("SELECT * FROM task_note ORDER BY rowid")]
                self.assertEqual(db.migrate(conn), [new])
                self.assertEqual(db.migrate(conn), [])                  # idempotent
                after = [tuple(r) for r in conn.execute(
                    "SELECT id, task_id, kind, text, actor, at FROM task_note ORDER BY rowid")]
                self.assertEqual(after, before)
                self.assertEqual({r[0] for r in conn.execute("SELECT field FROM task_note")}, {None})
                store = Store(conn, cfg)
                record = store.task_record(tid)
                self.assertEqual([n["text"] for n in record["inputs"]], ["C:/a", "C:/b"])
                self.assertEqual(record["open_fields"], store.required_fields)
                answer_brief(store, tid, WHO)
                self.assertEqual(store.task_record(tid)["open_fields"], [])
                with self.assertRaises(sqlite3.IntegrityError):         # a brief row needs its field
                    conn.execute("INSERT INTO task_note (id, task_id, kind, text, actor, at)"
                                 " VALUES ('x', ?, 'brief', 't', 'a', 'now')", (tid,))
                self.assertIn("entity_scope", {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")})
            finally:
                conn.close()


class TestIntakeServer(IntakeCase):
    def setUp(self):
        super().setUp()
        self.pid = self.project()
        self.reader_conn = db.connect_readonly(self.cfg)
        self.addCleanup(self.reader_conn.close)
        self.writer_conn = db.connect_limited(self.cfg, TASK_REVIEW_TABLES, insert_only=TASK_LINK_TABLES)
        self.addCleanup(self.writer_conn.close)
        self.server = TaskServer(self.reader_conn, self.writer_conn, self.cfg)

    def call(self, name: str, args: dict) -> tuple[bool, object]:
        result = self.server.call_tool(name, args)
        return result["isError"], json.loads(result["content"][0]["text"])

    def test_the_server_gates_ready_and_names_the_open_fields(self):
        bad, payload = self.call("task_update_status", {"id": self.tid, "status": self.ready, "reason": "go"})
        self.assertTrue(bad)
        self.assertEqual(payload["open_fields"], self.store.required_fields)
        self.assertEqual(self.store.get_task(self.tid)["status"], "confirmed")
        bad, record = self.call("task_update_status", {
            "id": self.tid, "brief": {**{f: f"the {f}" for f in self.text_fields},
                                      self.budget: budget_answer(self.store)}, "brief_source": self.stated,
            "scope": [{"type": self.types[0], "value": "C:/apollo"}]})
        self.assertFalse(bad, record)
        self.assertEqual(record["open_fields"], [])
        self.assertEqual(record["brief"][self.text_fields[0]]["actor"], ACTOR)
        bad, record = self.call("task_update_status", {"id": self.tid, "status": self.ready, "reason": "go",
                                                       "tools_allowed": ["Read"]})
        self.assertFalse(bad, record)
        self.assertEqual(record["task"]["status"], self.ready)

    def test_project_link_through_the_server_and_insert_only_links(self):
        bad, record = self.call("task_update_status", {"id": self.tid, "project": self.pid})
        self.assertFalse(bad, record)
        self.assertEqual([p["id"] for p in record["projects"]], [self.pid])
        other = self.store.upsert_entity(self.other_type(), "Billing System", source="test")
        bad, payload = self.call("task_update_status", {"id": self.tid, "project": other})
        self.assertTrue(bad)
        self.assertIn(self.intake["scope_entity_type"], payload["error"])
        # The writer may add a link but never change or remove one, its own task's included.
        for sql in ("DELETE FROM task_entity", "UPDATE task_entity SET entity_id = entity_id"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError) as ctx:
                self.writer_conn.execute(sql)
            self.assertIn("not authorized", str(ctx.exception))
        self.assertEqual(self.store.task_entities(self.tid), [self.pid])

    def test_budget_through_the_server(self):
        bad, record = self.call("task_update_status", {"id": self.tid, "brief_source": self.stated,
                                                       "brief": {self.budget: budget_answer(self.store)}})
        self.assertFalse(bad, record)
        self.assertEqual(record["brief"][self.budget]["value"], budget_answer(self.store))
        amount = self.intake["budget"]["amount_key"]
        for value in ("5", {amount: 0, self.intake["budget"]["reason_key"]: "x"}):
            bad, payload = self.call("task_update_status", {"id": self.tid, "brief_source": self.stated,
                                                            "brief": {self.budget: value}})
            self.assertTrue(bad, value)
            self.assertIn(self.budget, json.dumps(payload))

    def test_unknown_field_and_scope_type_are_errors_naming_them(self):
        for args, named in (({"brief": {"audiance": "x"}, "brief_source": self.stated}, "audiance"),
                            ({"scope": [{"type": "fax", "value": "x"}], "brief_source": self.stated}, "fax"),
                            ({"brief": {self.text_fields[0]: "x"}}, "brief_source")):
            with self.subTest(args=args):
                bad, payload = self.call("task_update_status", {"id": self.tid, **args})
                self.assertTrue(bad)
                self.assertIn(named, json.dumps(payload))
        self.assertEqual(self.store.brief_history(self.tid), [])

    def test_project_scope_tools(self):
        items = [{"type": self.types[0], "value": "C:/apollo"}]
        bad, got = self.call("project_scope_set", {"entity_id": self.pid, "scope": items})
        self.assertFalse(bad, got)
        self.assertEqual(got["scope"], items)
        bad, got = self.call("project_scope_get", {"entity_id": self.pid})
        self.assertEqual((bad, got["scope"], got["open_types"]), (False, items, self.types[1:]))
        other = self.store.upsert_entity(self.other_type(), "Billing System", source="test")
        for name, args in (("project_scope_get", {"entity_id": other}),
                           ("project_scope_set", {"entity_id": other, "scope": items})):
            bad, payload = self.call(name, args)
            self.assertTrue(bad, name)
            self.assertIn(self.intake["scope_entity_type"], payload["error"])
        bad, payload = self.call("project_scope_set", {"entity_id": self.pid, "scope": [{"type": "fax", "value": "x"}]})
        self.assertTrue(bad)
        self.assertIn("fax", json.dumps(payload))
        self.assertEqual(self.count("entity_scope"), 1)


class TestTaskServerSubprocess(TaskCase):
    """The real `python -m kg.mcp_tasks` over stdio, on a temp database."""

    def test_stdio_session_changes_only_task_review_tables(self):
        tid = self.add()
        path = self.root / "overlay.yaml"
        path.write_text(yaml.safe_dump(self.ov), encoding="utf-8")
        calls = [("task_list", {}), ("task_update_status", {"id": tid, "status": "confirmed", "reason": "mine"}),
                 ("task_update_status", {"id": tid, "status": "done", "reason": "skip ahead"})]
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        lines += [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}}
                  for i, (n, a) in enumerate(calls)]
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        def other_tables() -> list[str]:
            return [line for line in self.conn.iterdump()
                    if not (m := INSERT.match(line)) or m.group(1) not in TASK_REVIEW_TABLES]

        before = other_tables()
        proc = subprocess.run([sys.executable, "-m", "kg.mcp_tasks", "--config", str(path)], cwd=REPO,
                              input=("\n".join(json.dumps(m) for m in lines) + "\n").encode("utf-8"),
                              capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        by_id = {r["id"]: r for r in map(json.loads, proc.stdout.decode("utf-8").splitlines())}
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "whispr-tasks")
        self.assertEqual(len(by_id[2]["result"]["tools"]), len(REVIEW_TOOLS))
        self.assertFalse(by_id[10]["result"]["isError"])
        self.assertFalse(by_id[11]["result"]["isError"])
        self.assertTrue(by_id[12]["result"]["isError"])
        self.assertEqual(self.store.get_task(tid)["status"], "confirmed")
        self.assertEqual(self.events(tid)[-1], ("captured", "confirmed", ACTOR, "mine"))
        self.assertEqual(other_tables(), before)


if __name__ == "__main__":
    unittest.main()
