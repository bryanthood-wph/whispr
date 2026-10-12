"""pipeline/write.py and pipeline/run.py: the write stage and the per-call runner (D.1,
D.5, D.6). The write stage is tested on its own; for the runner a fake `ask` stands in
for the extract call: no model call, no CLI, temp dirs only."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest import mock

import pipeline.__main__ as cli
from kg import db
from kg.state import KIND_QUARANTINE, KIND_RUN_FAILED, QUARANTINED, QUEUED, State
from kg.store import AMBIGUOUS, EXTRACTED, Store, task_id
from pipeline import calls, models, prepare, render, write
from pipeline import run as runner
from pipeline.config import ConfigError, load_config
from pipeline_helpers import EXTRACT_SAMPLE, FILLER, overlay, scrubbed_env, transcript

T0 = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
PROCESS_SINCE = "2026-10-01"    # the runner tests' install date: their transcripts are from 2026-10-05
MY_QUOTE = "I'll send the deck to Jamie by Friday"
TURNS = [
    ("00:00:01", "Others", FILLER),
    ("00:01:02", "Me", MY_QUOTE),
    ("00:01:30", "Others", "someone book the room"),
    ("00:02:00", "Others", "we agreed to ship it Friday"),
    ("00:02:30", "Others", "Jamie owns the deck"),
    ("00:03:00", "Others", FILLER),
    ("00:04:00", "Me", FILLER),
]
STUB_TURNS = [("00:00:01", "Me", "hello"), ("00:00:04", "Others", "hi there can you hear me")]
GRAPH_TABLES = ("episode", "entity", "alias", "fact", "edge", "task", "task_entity", "task_event")


def sample_doc() -> dict:
    """EXTRACT_SAMPLE with the edge's target as an entity, so the edge resolves."""
    doc = copy.deepcopy(EXTRACT_SAMPLE)
    doc["entities"].append({"name": "Deck", "type": "project", "aliases": []})
    return doc


class PipelineCase(unittest.TestCase):
    """A fresh config, data dir and transcripts folder; no Claude session variables."""
    overlay_extra: dict = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        ov = overlay(self.root)
        for key, value in self.overlay_extra.items():
            ov[key] = {**ov.get(key, {}), **value}
        self.cfg = load_config(overlay=ov)
        env = mock.patch.dict(os.environ, scrubbed_env(self.cfg), clear=True)
        env.start()
        self.addCleanup(env.stop)

    def transcript(self, start: str, turns=TURNS, **kw) -> Path:
        path = Path(self.cfg["paths"]["transcripts"]) / f"{start[:10]}-{start[11:13]}{start[14:16]}-t.md"
        path.write_text(transcript(start, turns, **kw), encoding="utf-8")
        return path

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(db.connect(self.cfg)) as conn:
            yield Store(conn, self.cfg), State(conn, self.cfg)

    def counts(self) -> dict[str, int]:
        with self.db() as (store, _):
            return {t: store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in GRAPH_TABLES}

    def note(self, path: Path) -> str:
        return write.note_path(self.cfg, write.episode_id(path)).read_text(encoding="utf-8")


# ---- the write stage --------------------------------------------------------------------

class TestWriteStage(PipelineCase):
    def summary(self, path: Path, doc: Optional[dict] = None) -> write.Written:
        t = prepare.parse(path)
        with self.db() as (store, _):
            return write.write_summary(store, self.cfg, t, prepare.prepare([t], self.cfg), doc or sample_doc(),
                                       extractor_version="test")

    def test_note_with_my_actions_first_graph_rows_and_captured_tasks(self):
        path = self.transcript("2026-10-05T10:00:00-04:00")
        out = self.summary(path)
        self.assertEqual((out.tasks, out.confirm, out.facts, out.edges, out.edges_skipped), (2, 0, 1, 1, 0))
        note = self.note(path)
        self.assertLess(note.index(f"## {render.MY_ACTIONS}"), note.index(EXTRACT_SAMPLE["headline"]))
        self.assertIn("- Send the deck to Jamie\n", note)
        self.assertNotIn(render.CONFIRM, note)
        self.assertIn(f"{write.SOURCE_LABEL}: `{path}`", note)
        with self.db() as (store, _):
            ep = store.episode(path.stem)
            self.assertEqual((ep["sha256"], ep["extractor_version"]), (prepare.file_sha256(path), "test"))
            tasks = store.tasks()
            self.assertEqual({t["status"] for t in tasks}, {write.CAPTURED})
            mine = next(t for t in tasks if t["quote"] == MY_QUOTE)
            self.assertEqual((mine["owner"], mine["owner_basis"]), (self.cfg["owner"]["name"], "volunteered"))
            self.assertEqual(mine["confidence"], self.cfg["pipeline"]["write"]["task_confidence"]["verbatim"])
            self.assertEqual(store.task_entities(mine["id"]), store.person_matches(self.cfg["owner"]["name"]))
            other = next(t for t in tasks if t["quote"] == "someone book the room")
            self.assertEqual((other["owner"], other["owner_basis"]), (None, write.OTHERS))
        counts = self.counts()
        # entities: the owner, the attendee Jane Doe, Jamie Doe and the Deck project
        self.assertEqual({t: counts[t] for t in ("episode", "entity", "task", "fact", "edge")},
                         {"episode": 1, "entity": 4, "task": 2, "fact": 1, "edge": 1})

    def test_rewriting_the_same_episode_adds_no_rows_and_keeps_task_lifecycle(self):
        path = self.transcript("2026-10-05T10:00:00-04:00")
        self.summary(path)
        with self.db() as (store, _):
            mine = next(t for t in store.tasks() if t["quote"] == MY_QUOTE)
            store.set_task_status(mine["id"], "confirmed")
        before = self.counts()
        self.summary(path)
        self.assertEqual(self.counts(), before)
        with self.db() as (store, _):
            self.assertEqual(store.get_task(mine["id"])["status"], "confirmed")

    def test_rewrite_retracts_what_the_new_extraction_no_longer_has(self):
        path = self.transcript("2026-10-05T10:00:00-04:00")
        self.summary(path)
        with self.db() as (store, _):
            mine = next(t for t in store.tasks() if t["quote"] == MY_QUOTE)
            store.set_task_status(mine["id"], "confirmed")
            old_fact, old_edge = (store.conn.execute(f"SELECT id FROM {t}").fetchone()[0] for t in ("fact", "edge"))
        doc = sample_doc()
        doc["my_actions"], doc["edges"] = [], []
        doc["facts"][0].update(text="Ship Monday", quote="ship it Monday")
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")       # the transcript changed
        out = self.summary(path, doc)
        self.assertEqual(out.retracted, 3)                       # the old fact, the edge, my task
        sha = prepare.file_sha256(path)
        with self.db() as (store, _):
            for item in (old_fact, old_edge):
                self.assertIn(sha, store.get(item)["retract_reason"])
            self.assertEqual(store.get_task(mine["id"])["status"], "confirmed")     # its status is the user's
            self.assertEqual(store.review_tasks(status="confirmed", limit=10)["total"], 0)
            self.assertIn(sha, store.task_record(mine["id"])["retract_reason"])
            self.assertEqual([c["kind"] for c in store.search("ship Monday Friday")].count("fact"), 1)
        self.assertEqual(self.summary(path).retracted, 1)        # the original again: revived, Monday fact retracted
        with self.db() as (store, _):
            self.assertEqual(store.review_tasks(status="confirmed", limit=10)["total"], 1)
            self.assertIsNone(store.get(old_edge)["retracted_at"])

    def test_a_stub_retracts_the_earlier_extraction_and_a_quarantine_does_not(self):
        path = self.transcript("2026-10-05T09:00:00-04:00")
        self.summary(path)
        with self.db() as (store, _):
            write.write_unavailable(store, self.cfg, path)
            active = store.review_tasks(status=write.CAPTURED, mine=False, limit=10)["total"]
            self.assertEqual(active, 2)
        path.write_text(transcript("2026-10-05T09:00:00-04:00", STUB_TURNS), encoding="utf-8")
        t = prepare.parse(path)
        with self.db() as (store, _):
            out = write.write_stub(store, self.cfg, t, prepare.prepare([t], self.cfg))
            self.assertEqual(out.retracted, 4)                   # two tasks, the fact, the edge
            self.assertEqual(store.review_tasks(status=write.CAPTURED, mine=False, limit=10)["total"], 0)

    def test_quote_not_in_the_transcript_is_ambiguous(self):
        doc = sample_doc()
        doc["facts"].append({"type": "risk", "text": "Budget slips", "subject": "Deck",
                             "quote": "the budget will slip", "start": None})
        self.summary(self.transcript("2026-10-05T09:00:00-04:00"), doc)
        with self.db() as (store, _):
            rows = {r["quote"]: r["provenance"] for r in store.conn.execute("SELECT quote, provenance FROM fact")}
            deck = store.conn.execute("SELECT id FROM entity WHERE canonical_name = 'Deck'").fetchone()[0]
            self.assertEqual([f["quote"] for f in store.get(deck)["facts"]], ["the budget will slip"])
        self.assertEqual(rows, {"ship it Friday": EXTRACTED, "the budget will slip": AMBIGUOUS})

    def test_unclear_owner_and_echo_quoted_tasks_are_confirm(self):
        echo = "can you take the budget review for next week please"
        turns = TURNS + [("00:06:00", "Others", echo), ("00:06:01", "Me", echo)]
        doc = sample_doc()
        doc["my_actions"][0]["owner_basis"] = "unclear"
        doc["my_actions"].append({**doc["my_actions"][0], "action": "Run the budget review", "owner_basis": "assigned",
                                  "quote": "take the budget review for next week"})
        path = self.transcript("2026-10-05T09:00:00-04:00", turns)
        self.assertEqual(self.summary(path, doc).confirm, 2)
        note = self.note(path)
        self.assertIn(f"- Send the deck to Jamie ({render.CONFIRM})", note)
        self.assertIn(f"- Run the budget review ({render.CONFIRM})", note)
        with self.db() as (store, _):
            mine = [t for t in store.tasks() if t["owner"] == self.cfg["owner"]["name"]]
        self.assertEqual([t["owner_basis"] for t in mine], [write.UNCLEAR, write.UNCLEAR])

    def test_tasks_sharing_one_quote_each_keep_their_own_row(self):
        doc = sample_doc()
        doc["my_actions"].append({**doc["my_actions"][0], "action": "Send  the budget to Jamie",
                                  "due": {"text": "Monday", "basis": "stated"}})
        doc["my_actions"].append({**doc["my_actions"][0], "action": "send the budget to jamie"})   # the same task
        doc["other_tasks"] = [{"action": "Review the deck", "owner": "Jamie Doe", "context": "", "quote": MY_QUOTE,
                               "due": {"text": None, "basis": "not_stated"}, "start": None}]
        path = self.transcript("2026-10-05T09:00:00-04:00")
        self.assertEqual(self.summary(path, doc).tasks, 3)
        owner = self.cfg["owner"]["name"]
        with self.db() as (store, _):
            tasks = store.tasks()
            self.assertEqual(sorted((t["owner"], t["action"], t["due"]) for t in tasks),
                             [("Jamie Doe", "Review the deck", None), (owner, "Send  the budget to Jamie", "Monday"),
                              (owner, "Send the deck to Jamie", "Friday")])
            self.assertEqual(store.funnel()["captured"], 3)
            jamie = next(t for t in tasks if t["owner"] == "Jamie Doe")
            self.assertEqual(store.task_entities(jamie["id"]), store.person_matches("Jamie Doe"))
        before = self.counts()
        self.summary(path, doc)                                  # a rerun adds nothing
        self.assertEqual(self.counts(), before)

    def test_a_unique_quote_keeps_the_plain_d5_id(self):
        self.summary(self.transcript("2026-10-05T09:00:00-04:00"))
        with self.db() as (store, _):
            mine = next(t for t in store.tasks() if t["quote"] == MY_QUOTE)
        self.assertEqual(mine["id"], task_id("2026-10-05-0900-t", MY_QUOTE))

    def test_edge_to_an_unknown_entity_is_skipped_and_counted(self):
        out = self.summary(self.transcript("2026-10-05T09:00:00-04:00"), copy.deepcopy(EXTRACT_SAMPLE))
        self.assertEqual((out.edges, out.edges_skipped), (0, 1))

    def test_low_mic_coverage_warns_on_the_note(self):
        low = self.cfg["pipeline"]["write"]["low_mic_coverage"] / 2
        path = self.transcript("2026-10-05T09:00:00-04:00", extra_frontmatter=f"mic_coverage: {low}\n")
        self.assertTrue(self.summary(path).low_mic)
        self.assertIn(write.LOW_MIC_LINE, self.note(path))
        with self.db() as (store, _):
            self.assertEqual(store.episode(path.stem)["mic_coverage"], low)

    def test_stub_note_says_no_content_and_my_actions_none(self):
        path = self.transcript("2026-10-05T09:00:00-04:00", STUB_TURNS)
        t = prepare.parse(path)
        with self.db() as (store, _):
            write.write_stub(store, self.cfg, t, prepare.prepare([t], self.cfg))
            self.assertIsNotNone(store.episode(path.stem))
        note = self.note(path)
        self.assertIn(f"## {render.MY_ACTIONS}\n\n{render.NONE}", note)
        self.assertIn(write.NO_CONTENT_LINE, note)

    def test_a_mojibake_document_commits_nothing(self):
        doc = sample_doc()
        doc["other_tasks"][0]["context"] = f"agreed {self.cfg['prepare']['mojibake_markers'][0]} later"
        path = self.transcript("2026-10-05T09:00:00-04:00")
        with self.assertRaises(prepare.PrepareError):
            self.summary(path, doc)
        self.assertEqual(set(self.counts().values()), {0})
        self.assertFalse(write.note_path(self.cfg, path.stem).exists())

    def test_unavailable_note_even_for_an_unparseable_transcript(self):
        path = Path(self.cfg["paths"]["transcripts"]) / "2026-10-05-0900-broken.md"
        path.write_text("no frontmatter at all\n", encoding="utf-8")
        with self.db() as (store, _):
            out = write.write_unavailable(store, self.cfg, path)
            self.assertEqual(store.episode(path.stem)["sha256"], prepare.file_sha256(path))
            self.assertIsNone(write.write_unavailable(store, self.cfg, path.with_name("gone.md")))
        self.assertIn(f"## {render.MY_ACTIONS}\n\n{write.UNAVAILABLE_LINE}", out.note.read_text(encoding="utf-8"))


# ---- the runner -------------------------------------------------------------------------

class FakeAsk:
    """Stands in for the extract call: returns `doc`, or raises for refs in `fail`."""

    def __init__(self, doc: Optional[dict] = None, *, fail: dict[str, BaseException] | None = None,
                 cost: float = 0.01, clock=None, seconds: float = 0.0, cached: bool = False):
        self.doc, self.fail, self.cost, self.cached = doc or sample_doc(), fail or {}, cost, cached
        self.clock, self.seconds = clock, seconds
        self.refs: list[str] = []

    def __call__(self, prep) -> calls.Cached:
        ref = Path(prep.sources[0]).stem
        self.refs.append(ref)
        if self.clock is not None:
            self.clock.t += self.seconds
        if ref in self.fail:
            raise self.fail[ref]
        return calls.Cached("key", copy.deepcopy(self.doc), self.cached, "fake", "none", self.cost)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class RunCase(PipelineCase):
    """Runs the runner with a fake extract call, its output collected in self.out."""

    def setUp(self):
        super().setUp()
        self.cfg["pipeline"]["run"]["process_since"] = PROCESS_SINCE
        self.out: list[str] = []

    def go(self, ask=None, **kw) -> int:
        kw.setdefault("now", T0)
        kw.setdefault("clock", Clock())     # frozen: real elapsed time must not move retry_at
        return runner.run(self.cfg, ask=ask, out=self.out.append, **kw)

    def log(self) -> list[dict]:
        return models.read_jsonl(runner.files(self.cfg, "log"))

    def runs(self) -> list[dict]:
        return [r for r in self.log() if r["event"] == "run"]


class TestEmptyAndRerun(RunCase):
    def test_empty_run_makes_no_call_and_succeeds_by_proof_of_zero(self):
        ask = FakeAsk()
        started = time.monotonic()
        self.assertEqual(self.go(ask), runner.EXIT_OK)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(ask.refs, [])
        with self.db() as (_, state):
            last = state.last_success(runner.JOB)
        self.assertEqual((last["processed"], last["eligible"], last["backlog"]), (0, 0, 0))
        self.assertEqual(self.runs()[-1]["outcome"], runner.OK)

    def test_rerun_is_a_no_op_and_rewriting_adds_no_rows(self):
        path = self.transcript("2026-10-05T10:00:00-04:00")
        ask = FakeAsk()
        self.assertEqual(self.go(ask), runner.EXIT_OK)
        before = self.counts()
        self.assertEqual(self.go(ask), runner.EXIT_OK)           # nothing new: no call
        self.assertEqual(ask.refs, [path.stem])
        self.assertEqual(self.runs()[-1]["eligible"], 0)
        self.assertEqual(self.go(ask, once=path.name), runner.EXIT_OK)   # the same episode written again
        self.assertEqual(len(ask.refs), 2)
        self.assertEqual(self.counts(), before)


class TestNewTranscript(RunCase):
    def test_new_transcript_gets_a_note_graph_rows_and_captured_tasks(self):
        path = self.transcript("2026-10-05T10:00:00-04:00")
        self.assertEqual(self.go(FakeAsk()), runner.EXIT_OK)
        note = self.note(path)
        self.assertLess(note.index(f"## {render.MY_ACTIONS}"), note.index(EXTRACT_SAMPLE["headline"]))
        with self.db() as (store, state):
            ep = store.episode(path.stem)
            self.assertEqual(ep["sha256"], prepare.file_sha256(path))
            self.assertEqual(ep["extractor_version"], runner.extract.version(self.cfg))
            self.assertEqual({t["status"] for t in store.tasks()}, {write.CAPTURED})
            self.assertEqual(state.item(state.find(runner.STAGE, path.stem)["id"])["status"], "done")
        counts = self.counts()
        self.assertEqual((counts["episode"], counts["task"], counts["fact"], counts["edge"]), (1, 2, 1, 1))

    def test_changed_transcript_is_reprocessed_without_duplicates_or_lost_lifecycle(self):
        path = self.transcript("2026-10-05T10:00:00-04:00")
        ask = FakeAsk()
        self.go(ask)
        with self.db() as (store, _):
            mine = next(t for t in store.tasks() if t["quote"] == MY_QUOTE)
            store.set_task_status(mine["id"], "confirmed")
        before = self.counts()
        path.write_text(transcript("2026-10-05T10:00:00-04:00", TURNS + [("00:05:00", "Others", FILLER)]),
                        encoding="utf-8")
        self.assertEqual(self.go(ask, now=T0 + timedelta(minutes=15)), runner.EXIT_OK)
        self.assertEqual(ask.refs, [path.stem, path.stem])
        self.assertEqual(self.runs()[-1]["changed"], 1)
        self.assertEqual(self.counts(), before)
        with self.db() as (store, _):
            self.assertEqual(store.episode(path.stem)["sha256"], prepare.file_sha256(path))
            self.assertEqual(store.get_task(mine["id"])["status"], "confirmed")


class TestFailures(RunCase):
    def test_a_backoff_counts_from_when_the_item_failed_not_from_the_runs_start(self):
        bad = self.transcript("2026-10-05T09:00:00-04:00")
        clock, slow = Clock(), timedelta(hours=2)
        ask = FakeAsk(fail={bad.stem: runner.extract.ExtractError("extractor output failed its schema")},
                      clock=clock, seconds=slow.total_seconds())
        self.go(ask, now=T0, clock=clock)
        with self.db() as (_, state):
            backoff = timedelta(minutes=self.cfg["pipeline"]["run"]["retry_backoff_min"][0])
            self.assertEqual(state.find(runner.STAGE, bad.stem)["next_attempt_at"], db.utc_now(T0 + slow + backoff))

    def test_failing_extract_backs_off_then_quarantines_and_the_queue_moves_on(self):
        bad = self.transcript("2026-10-05T09:00:00-04:00")
        good = self.transcript("2026-10-05T10:00:00-04:00")
        ask = FakeAsk(fail={bad.stem: runner.extract.ExtractError("extractor output failed its schema")})
        backoff = self.cfg["pipeline"]["run"]["retry_backoff_min"]
        self.assertEqual(self.go(ask, now=T0), runner.EXIT_OK)                  # good done, bad retried
        self.assertEqual(sorted(ask.refs), sorted([bad.stem, good.stem]))
        self.assertTrue(write.note_path(self.cfg, good.stem).exists())
        self.assertEqual(self.go(ask, now=T0 + timedelta(minutes=1)), runner.EXIT_OK)   # backing off
        self.assertEqual(len(ask.refs), 2)
        self.assertEqual(self.runs()[-1]["eligible"], 0)
        second = T0 + timedelta(minutes=backoff[0])
        self.assertEqual(self.go(ask, now=second), runner.EXIT_FAILED)          # retried: no progress
        with self.db() as (_, state):
            item = state.find(runner.STAGE, bad.stem)
            self.assertEqual((item["status"], item["attempts"]), (QUEUED, 2))
            self.assertEqual(item["next_attempt_at"], db.utc_now(second + timedelta(minutes=backoff[1])))
            self.assertIsNotNone(state.alert(f"{KIND_RUN_FAILED}:{runner.JOB}"))
        self.assertEqual(self.go(ask, now=second + timedelta(minutes=backoff[1])), runner.EXIT_OK)
        self.assertEqual(len(ask.refs), 4)
        with self.db() as (_, state):
            self.assertEqual(state.find(runner.STAGE, bad.stem)["status"], QUARANTINED)
            alert = state.alert(f"{KIND_QUARANTINE}:{runner.STAGE}:{bad.stem}")
            self.assertEqual(alert["count"], 1)
            self.assertIn("failed its schema", alert["message"])
            self.assertIn(f"python -m pipeline requeue {bad.stem}", alert["fix"])
        note = self.note(bad)
        self.assertIn(f"## {render.MY_ACTIONS}\n\n{write.UNAVAILABLE_LINE}", note)
        self.assertEqual(self.go(ask, now=T0 + timedelta(days=1)), runner.EXIT_OK)   # left alone
        self.assertEqual(len(ask.refs), 4)

    def test_quarantined_transcript_is_requeued_when_it_changes(self):
        bad = self.transcript("2026-10-05T09:00:00-04:00")
        ask = FakeAsk(fail={bad.stem: runner.extract.ExtractError("output failed its schema")})
        later = T0
        for _ in range(self.cfg["pipeline"]["max_attempts"]):
            self.go(ask, now=later)
            later += timedelta(days=1)
        ask.fail = {}
        self.go(ask, now=later)
        self.assertEqual(len(ask.refs), self.cfg["pipeline"]["max_attempts"])     # unchanged: left alone
        bad.write_text(transcript("2026-10-05T09:00:00-04:00", TURNS + [("00:09:00", "Me", FILLER)]),
                       encoding="utf-8")
        self.assertEqual(self.go(ask, now=later), runner.EXIT_OK)
        self.assertNotIn(write.UNAVAILABLE_LINE, self.note(bad))

    def test_auth_failure_requeues_the_item_alerts_and_stops(self):
        refs = [self.transcript(f"2026-10-05T{hour}:00:00-04:00").stem for hour in ("09", "10")]
        ask = FakeAsk(fail=dict.fromkeys(refs, models.AuthError("auth source 'ANTHROPIC_API_KEY' is not approved")))
        self.assertEqual(self.go(ask), runner.EXIT_FAILED)
        self.assertEqual(len(ask.refs), 1)                       # stopped at the first
        with self.db() as (_, state):
            self.assertEqual(state.find(runner.STAGE, ask.refs[0])["attempts"], 0)
            self.assertIsNotNone(state.alert(f"{runner.KIND_AUTH}:{runner.JOB}"))
        self.assertEqual(self.runs()[-1]["stopped"], runner.AUTH)

    def test_an_outage_uses_no_attempts_and_stops_each_run_with_one_alert(self):
        refs = [self.transcript(f"2026-10-05T{hour}:00:00-04:00").stem for hour in ("09", "10")]
        ask = FakeAsk(fail=dict.fromkeys(refs, models.ModelCallError("CLI 'claude' not found on PATH")))
        runs = self.cfg["pipeline"]["max_attempts"] + 2
        for n in range(runs):
            self.assertEqual(self.go(ask, now=T0 + timedelta(minutes=15 * n)), runner.EXIT_FAILED)
            self.assertEqual(self.runs()[-1]["stopped"], runner.UNAVAILABLE)
        self.assertEqual(len(ask.refs), runs)                    # one call per run, then it stopped
        with self.db() as (_, state):
            for ref in refs:
                item = state.find(runner.STAGE, ref)
                self.assertEqual((item["status"], item["attempts"]), (QUEUED, 0))
            self.assertEqual(state.alert(f"{runner.KIND_UNAVAILABLE}:{runner.JOB}")["count"], runs)
            self.assertEqual({a["kind"] for a in state.open_alerts()}, {runner.KIND_UNAVAILABLE, KIND_RUN_FAILED})
        ask.fail = {}                                            # back online: the queue resumes
        self.assertEqual(self.go(ask, now=T0 + timedelta(hours=3)), runner.EXIT_OK)
        self.assertEqual(self.runs()[-1]["summarized"], 2)
        with self.db() as (_, state):                            # and the outage's alerts close
            self.assertEqual(state.open_alerts(), [])

    def test_an_auth_failure_after_the_model_answered_keeps_its_alert(self):
        bad = self.transcript("2026-10-05T09:00:00-04:00")
        self.transcript("2026-10-05T10:00:00-04:00")             # newer: worked first, and answered
        self.go(FakeAsk(fail={bad.stem: models.AuthError("auth source 'ANTHROPIC_API_KEY' is not approved")}))
        with self.db() as (_, state):
            self.assertIn(f"{runner.KIND_AUTH}:{runner.JOB}", {a["dedupe_key"] for a in state.open_alerts()})

    def test_a_call_failure_after_the_model_answered_counts_against_the_item(self):
        bad = self.transcript("2026-10-05T09:00:00-04:00")
        self.transcript("2026-10-05T10:00:00-04:00")             # newer: worked first, and answered
        self.assertEqual(self.go(FakeAsk(fail={bad.stem: models.ModelCallError("extractor call timed out")})),
                         runner.EXIT_OK)
        with self.db() as (_, state):
            item = state.find(runner.STAGE, bad.stem)
            self.assertEqual((item["status"], item["attempts"]), (QUEUED, 1))
            self.assertIn("timed out", item["last_error"])
            self.assertIsNone(state.alert(f"{runner.KIND_UNAVAILABLE}:{runner.JOB}"))

    def test_a_cache_hit_does_not_prove_the_model_answered(self):
        bad = self.transcript("2026-10-05T09:00:00-04:00")
        self.transcript("2026-10-05T10:00:00-04:00")
        ask = FakeAsk(fail={bad.stem: models.ModelCallError("extractor call timed out")}, cached=True)
        self.assertEqual(self.go(ask), runner.EXIT_FAILED)
        with self.db() as (_, state):
            self.assertEqual(state.find(runner.STAGE, bad.stem)["attempts"], 0)

    def test_items_that_failed_before_are_worked_after_fresh_ones(self):
        items = [{"ref": "2026-10-05-1000-a", "last_error": "x"}, {"ref": "2026-10-04-0900-b", "last_error": None},
                 {"ref": "2026-10-05-0900-c", "last_error": None}]
        self.assertEqual([i["ref"] for i in runner.work_order(items)],
                         ["2026-10-05-0900-c", "2026-10-04-0900-b", "2026-10-05-1000-a"])

    def test_requeue_command_puts_a_quarantined_transcript_back(self):
        bad = self.transcript("2026-10-05T09:00:00-04:00")
        ask = FakeAsk(fail={bad.stem: runner.extract.ExtractError("output failed its schema")})
        for day in range(self.cfg["pipeline"]["max_attempts"]):
            self.go(ask, now=T0 + timedelta(days=day))
        ask.fail = {}
        with mock.patch.object(cli, "load_config", return_value=self.cfg), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["requeue", "no-such-ref"]), runner.EXIT_USAGE)
            with self.assertRaises(SystemExit):
                cli.main(["requeue"])
            self.assertEqual(cli.main(["requeue", "--all-quarantined"]), runner.EXIT_OK)
        with self.db() as (_, state):
            item = state.find(runner.STAGE, bad.stem)
            self.assertEqual((item["status"], item["attempts"]), (QUEUED, 0))
            self.assertNotIn(KIND_QUARANTINE, {a["kind"] for a in state.open_alerts()})
        self.assertEqual(self.go(ask, now=T0 + timedelta(days=9)), runner.EXIT_OK)
        self.assertNotIn(write.UNAVAILABLE_LINE, self.note(bad))

    def test_invalid_output_is_an_item_failure(self):
        path = self.transcript("2026-10-05T09:00:00-04:00")
        self.assertEqual(self.go(FakeAsk({"headline": "no other fields"})), runner.EXIT_FAILED)
        with self.db() as (_, state):
            item = state.find(runner.STAGE, path.stem)
        self.assertIn("schema", item["last_error"])

    def test_claudecode_set_alerts_without_a_call_or_a_run(self):
        self.transcript("2026-10-05T09:00:00-04:00")
        ask = FakeAsk()
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertEqual(self.go(ask), runner.EXIT_REFUSED)
            self.assertEqual(self.go(ask), runner.EXIT_REFUSED)
            self.assertEqual(self.go(ask, dry_run=True), runner.EXIT_OK)       # planning is safe anywhere
        self.assertEqual(ask.refs, [])
        with self.db() as (store, state):
            alert = state.alert(f"{runner.KIND_REFUSED}:{runner.JOB}")
            self.assertEqual(alert["count"], 2)
            self.assertIn("CLAUDECODE", alert["message"])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM run").fetchone()[0], 0)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM item").fetchone()[0], 0)
        self.assertEqual(self.go(ask), runner.EXIT_OK)                         # outside Claude Code: it closes
        with self.db() as (_, state):
            self.assertEqual(state.open_alerts(), [])


class TestDiscoveryWindow(RunCase):
    def test_unset_process_since_refuses_with_one_alert_and_no_queue(self):
        self.cfg["pipeline"]["run"]["process_since"] = None
        self.transcript("2026-10-05T09:00:00-04:00")
        ask = FakeAsk()
        self.assertEqual(self.go(ask), runner.EXIT_REFUSED)
        self.assertEqual(self.go(ask), runner.EXIT_REFUSED)
        self.assertEqual(self.go(ask, dry_run=True), runner.EXIT_REFUSED)
        self.assertIn("process_since", self.out[-1])
        self.assertEqual(ask.refs, [])
        with self.db() as (store, state):
            alert = state.alert(f"{runner.KIND_SETUP}:{runner.JOB}")
            self.assertEqual((alert["count"], len(state.open_alerts())), (2, 1))
            self.assertIn("process_since", alert["message"])
            self.assertIn("process_since", alert["fix"])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM item").fetchone()[0], 0)
        self.cfg["pipeline"]["run"]["process_since"] = "2026-10-01"           # set up: the next run closes it
        self.assertEqual(self.go(ask), runner.EXIT_OK)
        with self.db() as (_, state):
            self.assertEqual(state.open_alerts(), [])

    def test_only_transcripts_dated_on_or_after_process_since_are_discovered(self):
        old = self.transcript("2026-09-30T09:00:00-04:00")
        new = self.transcript("2026-10-01T09:00:00-04:00")
        ask = FakeAsk()
        self.assertEqual(self.go(ask), runner.EXIT_OK)
        self.assertEqual(ask.refs, [new.stem])
        with self.db() as (_, state):
            self.assertIsNone(state.find(runner.STAGE, old.stem))
        self.assertEqual(self.runs()[-1]["backlog"], 0)

    def test_newest_call_is_worked_first(self):
        self.cfg["pipeline"]["run"]["max_items"] = 1
        refs = [self.transcript(f"2026-10-0{day}T09:00:00-04:00").stem for day in (3, 5, 4)]
        ask = FakeAsk()
        self.assertEqual(self.go(ask), runner.EXIT_PARTIAL)
        self.assertEqual(ask.refs, [max(refs)])

    def test_backfill_quotes_the_cost_and_queues_only_with_yes(self):
        old = [self.transcript(f"2026-09-{day}T09:00:00-04:00").stem for day in (28, 29)]
        new = self.transcript("2026-10-05T09:00:00-04:00").stem
        est = self.cfg["pipeline"]["run"]["est_cost_per_call_usd"]
        self.assertEqual(runner.backfill(self.cfg, out=self.out.append), runner.EXIT_OK)
        self.assertIn(f"2 transcript(s) not yet processed, about ${2 * est:.2f}", self.out[0])
        self.assertFalse(Path(self.cfg["paths"]["data_dir"]).exists())      # a quote writes nothing
        with mock.patch.object(cli, "load_config", return_value=self.cfg), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(cli.main(["backfill", "--since", "2026-09-29", "--yes"]), runner.EXIT_OK)
        self.assertIn("1 transcript(s)", stdout.getvalue())
        ask = FakeAsk()
        self.assertEqual(self.go(ask), runner.EXIT_OK)
        self.assertEqual(ask.refs, [new, old[1]])                           # newest first; 09-28 never queued
        self.assertEqual(runner.backfill(self.cfg, out=self.out.append), runner.EXIT_OK)
        self.assertIn("1 transcript(s) not yet processed", self.out[-2])


class TestRunnerNotes(RunCase):
    def test_stub_makes_no_call_and_gets_a_no_content_note(self):
        path = self.transcript("2026-10-05T09:00:00-04:00", STUB_TURNS)
        ask = FakeAsk()
        self.assertEqual(self.go(ask), runner.EXIT_OK)
        self.assertEqual(ask.refs, [])
        note = self.note(path)
        self.assertIn(f"## {render.MY_ACTIONS}\n\n{render.NONE}", note)
        self.assertIn("No content", note)
        with self.db() as (store, _):
            self.assertIsNotNone(store.episode(path.stem))

    def test_item_log_line_counts_tasks_to_confirm(self):
        doc = sample_doc()
        doc["my_actions"][0]["owner_basis"] = "unclear"
        self.transcript("2026-10-05T09:00:00-04:00")
        self.go(FakeAsk(doc))
        self.assertEqual(self.log()[0]["confirm"], 1)


class TestBudgets(RunCase):
    overlay_extra = {"pipeline": {"run": {"time_budget_s": 100, "max_items": 20, "cap_usd": 3.0,
                                          "daily_cap_usd": 10.0, "retry_backoff_min": [15, 60]}}}

    def test_time_budget_exits_partial_with_the_rest_queued(self):
        for hour in ("09", "10", "11"):
            self.transcript(f"2026-10-05T{hour}:00:00-04:00")
        clock = Clock()
        ask = FakeAsk(clock=clock, seconds=60)
        self.assertEqual(self.go(ask, clock=clock), runner.EXIT_PARTIAL)
        self.assertEqual(len(ask.refs), 1)                       # 60 s done + 60 s estimated >= 100 s
        last = self.runs()[-1]
        self.assertEqual((last["outcome"], last["stopped"], last["processed"], last["backlog"]),
                         (runner.PARTIAL, runner.TIME_BUDGET, 1, 2))
        with self.db() as (_, state):
            run = state.last_success(runner.JOB)
            self.assertEqual(state.metrics(run["id"])["partial"], 1.0)

    def test_spend_cap_stops_before_the_call_that_could_pass_it(self):
        self.cfg["pipeline"]["run"]["cap_usd"] = self.cfg["pipeline"]["max_budget_per_call_usd"] * 1.5
        for hour in ("09", "10"):
            self.transcript(f"2026-10-05T{hour}:00:00-04:00")
        ask = FakeAsk(cost=self.cfg["pipeline"]["max_budget_per_call_usd"] * 0.6)
        self.assertEqual(self.go(ask), runner.EXIT_PARTIAL)
        self.assertEqual(len(ask.refs), 1)
        self.assertEqual(self.runs()[-1]["stopped"], runner.RUN_CAP)

    def test_daily_cap_counts_another_jobs_call_in_flight(self):
        self.transcript("2026-10-05T09:00:00-04:00")
        cap = self.cfg["pipeline"]["run"]["daily_cap_usd"]
        runner.reserve_call(self.cfg, job="daily", key="k", per_call=cap, now=T0)    # the daily job, mid-call
        ask = FakeAsk()
        self.assertEqual(self.go(ask, now=T0 + timedelta(hours=1)), runner.EXIT_FAILED)
        self.assertEqual((ask.refs, self.runs()[-1]["stopped"]), ([], runner.DAILY_CAP))
        models.append_jsonl(runner.files(self.cfg, "ledger"), {"request_key": "k", "cost_usd": 0.0,
                                                               "ts": db.utc_now(T0)})    # it settles at $0
        self.assertEqual(self.go(ask, now=T0 + timedelta(hours=2)), runner.EXIT_OK)
        self.assertEqual(len(ask.refs), 1)

    def test_daily_cap_counts_the_ledger(self):
        self.transcript("2026-10-05T09:00:00-04:00")
        models.append_jsonl(runner.files(self.cfg, "ledger"),
                            {"cost_usd": self.cfg["pipeline"]["run"]["daily_cap_usd"], "ts": db.utc_now(T0)})
        ask = FakeAsk()
        self.assertEqual(self.go(ask, now=T0 + timedelta(hours=1)), runner.EXIT_FAILED)   # nothing done
        self.assertEqual(ask.refs, [])
        self.assertEqual(self.runs()[-1]["stopped"], runner.DAILY_CAP)
        self.assertEqual(self.go(ask, now=T0 + timedelta(days=1, hours=1)), runner.EXIT_OK)   # window passed
        self.assertEqual(len(ask.refs), 1)


class TestLockDryRunAndCli(RunCase):
    def test_lock_held_second_run_exits_fast(self):
        ask = FakeAsk()
        self.transcript("2026-10-05T09:00:00-04:00")
        with runner.single_instance(runner.files(self.cfg, "lock")) as held:
            self.assertTrue(held)
            started = time.monotonic()
            self.assertEqual(self.go(ask), runner.EXIT_OK)
            self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(ask.refs, [])
        self.assertEqual(self.runs()[-1]["outcome"], runner.LOCKED)
        with runner.single_instance(runner.files(self.cfg, "lock")) as held:
            self.assertTrue(held)                                # released with its holder

    def test_dry_run_plans_with_zero_calls_and_no_queue_writes(self):
        self.transcript("2026-10-05T09:00:00-04:00")
        self.transcript("2026-10-05T10:00:00-04:00", STUB_TURNS)
        ask = FakeAsk()
        self.assertEqual(self.go(ask, dry_run=True), runner.EXIT_OK)
        self.assertEqual(ask.refs, [])
        text = "\n".join(self.out)
        self.assertIn("extract: one model call", text)
        self.assertIn("stub", text)
        self.assertIn("1 model call(s)", text)
        self.assertFalse(Path(self.cfg["paths"]["data_dir"]).exists())       # no database, log or folder made

    def test_dry_run_on_an_older_database_leaves_it_unmigrated_and_unchanged(self):
        path = self.transcript("2026-10-05T09:00:00-04:00")
        older = self.root / "older-migrations"
        older.mkdir()
        for m in db.migrations()[:-1]:
            (older / m.path.name).write_bytes(m.path.read_bytes())
        db.connect(self.cfg, directory=older).close()
        before = db.database_path(self.cfg).read_bytes()
        self.assertEqual(self.go(FakeAsk(), dry_run=True), runner.EXIT_OK)
        self.assertIn(f"{path.stem}: new -> extract: one model call", self.out)
        self.assertEqual(db.database_path(self.cfg).read_bytes(), before)
        self.assertFalse(runner.files(self.cfg, "log").exists())
        with contextlib.closing(sqlite3.connect(db.database_path(self.cfg))) as conn:
            self.assertNotIn(db.migrations()[-1].version, {r[0] for r in conn.execute("SELECT version FROM schema_version")})

    def test_once_rejects_a_file_outside_the_transcripts_folder(self):
        outside = self.root / "elsewhere.md"
        outside.write_text("---\n---\n", encoding="utf-8")
        self.assertEqual(self.go(FakeAsk(), once=str(outside)), runner.EXIT_USAGE)

    def test_cli_run_dry_run(self):
        stdout = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=self.cfg), contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["run", "--dry-run"]), runner.EXIT_OK)
        self.assertIn("no model call was made", stdout.getvalue())          # on stdout, not in the data dir
        self.assertFalse(Path(self.cfg["paths"]["data_dir"]).exists())

    def test_an_escaped_exception_goes_to_the_fatal_log_and_exits_1(self):
        local = self.root / "local"
        blocked = self.root / "a-file"
        blocked.write_text("", encoding="utf-8")
        unwritable = {**self.cfg, "paths": {**self.cfg["paths"], "data_dir": str(blocked / "data")}}
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}), mock.patch.object(sys, "stderr", None), \
                contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(cli, "load_config", side_effect=ConfigError("invalid config: owner.name")):
                self.assertEqual(cli.main(["run"]), runner.EXIT_FAILED)
            with mock.patch.object(cli, "load_config", return_value=unwritable):
                self.assertEqual(cli.main(["run"]), runner.EXIT_FAILED)
        text = (local / "whispr" / "pipeline-fatal.log").read_text(encoding="utf-8")
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\S+ python -m pipeline run\n")
        self.assertIn("ConfigError: invalid config: owner.name", text)
        self.assertIn("FileExistsError", text)                   # the unwritable data dir's error
        self.assertIn(blocked.name, text)

    def test_every_item_and_run_logs_one_json_line(self):
        self.transcript("2026-10-05T09:00:00-04:00")
        self.go(FakeAsk())
        lines = runner.files(self.cfg, "log").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line)["event"] for line in lines]
        self.assertEqual(events, ["item", "run"])


if __name__ == "__main__":
    unittest.main()
