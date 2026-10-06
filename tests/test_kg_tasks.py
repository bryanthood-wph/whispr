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
from kg.mcp_tasks import ACTOR, TASK_REVIEW_TABLES, TaskServer
from kg.store import Store, StoreError, TransitionError, task_id
from pipeline.config import load_config
from pipeline_helpers import overlay

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
OLD_EP, NEW_EP = "ep-2026-09-28-1000", "ep-2026-10-01-0900"
WHO = "tester"
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
        self.writer_conn = db.connect_limited(self.cfg, TASK_REVIEW_TABLES)
        self.addCleanup(self.writer_conn.close)
        self.server = TaskServer(self.reader_conn, self.writer_conn, self.cfg)

    def call(self, name: str, args: dict) -> tuple[bool, object]:
        result = self.server.call_tool(name, args)
        return result["isError"], json.loads(result["content"][0]["text"])

    def test_tools_list_holds_the_review_tools_only(self):
        tools = self.server.dispatch("tools/list", {})["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), ["task_get", "task_list", "task_update_status"])
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
        self.assertEqual(len(by_id[2]["result"]["tools"]), 3)
        self.assertFalse(by_id[10]["result"]["isError"])
        self.assertFalse(by_id[11]["result"]["isError"])
        self.assertTrue(by_id[12]["result"]["isError"])
        self.assertEqual(self.store.get_task(tid)["status"], "confirmed")
        self.assertEqual(self.events(tid)[-1], ("captured", "confirmed", ACTOR, "mine"))
        self.assertEqual(other_tables(), before)


if __name__ == "__main__":
    unittest.main()
