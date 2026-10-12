"""Calls on several workers (eval.workers, eval.ask.fan_out): the same records, calls and
budget accounting as one at a time. Every other test file runs with eval.workers 1."""

from __future__ import annotations

import os
import random
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from eval import ledger as L
from eval import pilot as P
from eval.ask import fan_out, values
from pipeline import calls
from pipeline.config import load_config
from pipeline_helpers import fake_cli, overlay, scrubbed_env
from test_eval_pilot_contract import _Base as _PilotBase, fake_ask

WORKERS = 4
SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


def with_workers(cfg: dict, n: int) -> dict:
    return {**cfg, "eval": {**cfg["eval"], "workers": n}}


def jittered(ask):
    """`ask` after a random short sleep, so calls finish out of the order they started."""
    rng, lock = random.Random(7), threading.Lock()

    def slow(*args, **kw):
        with lock:
            pause = rng.random() / 200
        time.sleep(pause)
        with lock:              # the fake's own check-then-log is not atomic; the guard's is
            return ask(*args, **kw)
    return slow


class TestFanOut(unittest.TestCase):
    def test_values_come_back_in_job_order(self):
        cfg = {"eval": {"workers": WORKERS}}
        jobs = [lambda n=n: (time.sleep((5 - n) / 200), n)[1] for n in range(6)]
        self.assertEqual(values(fan_out(cfg, jobs)), list(range(6)))

    def test_after_a_failure_no_new_job_starts_and_the_first_error_is_raised(self):
        started = []

        def job(n):
            started.append(n)
            if n == 1:
                raise ValueError("job 1")
            return n
        outcomes = fan_out({"eval": {"workers": 1}}, [lambda n=n: job(n) for n in range(4)])
        self.assertEqual(started, [0, 1])                    # one at a time: a plain loop
        self.assertEqual(outcomes[2:], [None, None])
        with self.assertRaisesRegex(ValueError, "job 1"):
            values(outcomes)


class TestGuardUnderContention(unittest.TestCase):
    def test_reservations_never_pass_the_stage_cap_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = overlay(Path(tmp))
            ov["eval"]["stages"] = {"pilot": {"cap_usd": 25}}
            cfg = load_config(overlay=ov)
            check, barrier, passed = L.guard(cfg, "pilot"), threading.Barrier(8), []

            def reserve(n):
                barrier.wait()
                try:
                    check(f"k{n}", 10.0)
                    passed.append(n)
                except L.BudgetStop:
                    pass
            threads = [threading.Thread(target=reserve, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(passed), 2)                 # $25 holds two $10 reservations
            self.assertEqual(L.tally(cfg)[1]["pilot"], 20.0)


class TestOneCallPerKey(unittest.TestCase):
    def test_a_duplicate_request_in_flight_is_served_from_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ov = overlay(root)
            ov["cli"] = fake_cli()
            cfg, ledger, cache = load_config(overlay=ov), root / "ledger.jsonl", root / "cache"
            barrier, got = threading.Barrier(2), []

            def ask():
                barrier.wait()
                got.append(calls.cached_call(cfg, "judge", "SAME", schema=SCHEMA, max_budget_usd=0.5,
                                             ledger=ledger, cache_dir=cache))
            with mock.patch.dict(os.environ, scrubbed_env(cfg), clear=True):
                threads = [threading.Thread(target=ask) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 1)     # one launched call
            self.assertEqual(sorted(c.cached for c in got), [False, True])
            self.assertEqual(got[0].output, got[1].output)


class TestPilotOnWorkers(_PilotBase):
    def run_with(self, workers: int, **kw):
        log = []
        out = self.root / f"pilot-{workers}.jsonl"
        result = P.run(self.units, with_workers(self.cfg, workers), jittered(fake_ask(self.cfg, log, **kw)), out)
        return result, log

    def test_same_records_and_calls_as_one_at_a_time(self):
        one, one_log = self.run_with(1)
        many, many_log = self.run_with(WORKERS)
        self.assertFalse(many.stopped)
        self.assertEqual(many.records, one.records)                          # same content, same order
        self.assertEqual(Counter(many_log), Counter(one_log))                # the same requests
        self.assertEqual(Counter((c["step"], c["key"]) for c in many.calls),
                         Counter((c["step"], c["key"]) for c in one.calls))  # each logged under its step

    def test_a_budget_stop_keeps_every_finished_record_and_ends_the_run(self):
        full, full_log = self.run_with(1)
        stop_at = len(full_log) // 2
        stopped, log = self.run_with(WORKERS, fail_after=stop_at)
        self.assertTrue(stopped.stopped)
        self.assertEqual(stopped.records[-1]["step"], P.BUDGET_STOP)
        kept = stopped.records[:-1]
        self.assertTrue(all(r in full.records for r in kept))      # nothing a one-at-a-time run lacks
        self.assertEqual(len(log), stop_at)                         # no call past the stop


if __name__ == "__main__":
    unittest.main()
