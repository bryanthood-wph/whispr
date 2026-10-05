"""pipeline/extract.py against the fake CLI (issue #10). No model calls."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import extract as X
from pipeline import prepare as P
from pipeline.config import load_config
from pipeline_helpers import EXTRACT_SAMPLE, overlay, transcript

FAKE = str(Path(__file__).with_name("fake_claude.py"))
FILLER = "we walked through the quarterly plan and the staffing model in detail today"


class TestExtract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        ov = overlay(self.root)
        ov["cli"] = {"executable": sys.executable, "base_args": [FAKE], "timeout_s": 20}
        self.cfg = load_config(overlay=ov)
        self.ledger = self.root / "ledger.jsonl"
        self.cache = self.root / "cache"
        self.args_out = self.root / "args.json"
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
        env.update({"FAKE_CLAUDE_ARGS_OUT": str(self.args_out),
                    "FAKE_CLAUDE_STRUCTURED": json.dumps(EXTRACT_SAMPLE)})
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def prepared(self, turns=None, **kw) -> P.Prepared:
        path = self.root / "t.md"
        path.write_text(transcript("2026-10-01T10:00:00-04:00",
                                   turns if turns is not None else [("00:00:01", "Others", FILLER)] * 5, **kw),
                        encoding="utf-8")
        return P.prepare([P.parse(path)], self.cfg)

    def run_extract(self, prep=None, **kw):
        kw.setdefault("cache_dir", self.cache)
        return X.extract(prep or self.prepared(), self.cfg, role="extractor", max_budget_usd=0.5,
                         ledger=self.ledger, **kw)

    def ledger_rows(self):
        return self.ledger.read_text(encoding="utf-8").splitlines() if self.ledger.exists() else []

    def test_prompt_is_filled_from_episode_and_config(self):
        req = X.build_request(self.prepared(attendees=[]), self.cfg, role="extractor")
        self.assertNotIn("{{", req.prompt)
        self.assertIn("Pat Example", req.prompt)              # owner name from the overlay
        self.assertIn("Weekly Sync", req.prompt)
        self.assertIn(f"[00:00:01] Others: {FILLER}", req.prompt)
        self.assertIn("Attendees: not recorded", req.prompt)  # extract.missing_value

    def test_valid_output_is_returned_and_cached(self):
        first = self.run_extract()
        self.assertEqual(first.output, EXTRACT_SAMPLE)
        self.assertFalse(first.cached)
        self.assertEqual(len(self.ledger_rows()), 1)
        argv = json.loads(self.args_out.read_text(encoding="utf-8"))["argv"]
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1])["required"][2], "my_actions")

        self.args_out.unlink()
        second = self.run_extract()
        self.assertTrue(second.cached)
        self.assertEqual(second.output, EXTRACT_SAMPLE)
        self.assertEqual(second.key, first.key)
        self.assertFalse(self.args_out.exists())               # no second launch
        self.assertEqual(len(self.ledger_rows()), 1)           # and no second spend

    def test_no_cache_dir_always_calls(self):
        self.run_extract(cache_dir=None)
        self.run_extract(cache_dir=None)
        self.assertEqual(len(self.ledger_rows()), 2)

    def test_schema_violation_fails_and_is_not_cached(self):
        bad = copy.deepcopy(EXTRACT_SAMPLE)
        del bad["my_actions"]
        os.environ["FAKE_CLAUDE_STRUCTURED"] = json.dumps(bad)
        with self.assertRaises(X.ExtractError) as ctx:
            self.run_extract()
        self.assertIn("my_actions", str(ctx.exception))
        self.assertFalse(any(self.cache.glob("*.json")) if self.cache.exists() else False)
        self.assertEqual(len(self.ledger_rows()), 1)           # the failed call's spend is still recorded

    def test_stub_is_refused_without_a_call(self):
        with self.assertRaises(X.ExtractError):
            self.run_extract(self.prepared(turns=[]))
        self.assertFalse(self.args_out.exists())

    def test_key_covers_model_effort_system_prompt_and_input(self):
        prep = self.prepared()
        base = X.build_request(prep, self.cfg, role="extractor").key
        self.assertEqual(base, X.build_request(prep, self.cfg, role="extractor").key)
        self.assertNotEqual(base, X.build_request(prep, self.cfg, role="reference_a").key)
        self.assertNotEqual(base, X.build_request(prep, self.cfg, role="extractor", system_prompt="S").key)
        cfg2 = copy.deepcopy(self.cfg)
        cfg2["models"]["extractor"]["effort"] = "high"
        self.assertNotEqual(base, X.build_request(prep, cfg2, role="extractor").key)
        other = self.prepared(turns=[("00:00:02", "Others", FILLER)] * 5)
        self.assertNotEqual(base, X.build_request(other, self.cfg, role="extractor").key)

    def test_system_prompt_is_passed_through(self):
        self.run_extract(system_prompt="SYS", cache_dir=None)
        argv = json.loads(self.args_out.read_text(encoding="utf-8"))["argv"]
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "SYS")

    def test_budget_hook_runs_only_on_a_miss_and_can_stop_the_call(self):
        seen = []
        first = self.run_extract(before_call=lambda key, cap: seen.append((key, cap)))
        self.run_extract(before_call=lambda key, cap: seen.append((key, cap)))   # cache hit: no hook
        self.assertEqual(seen, [(first.key, 0.5)])

        class Stop(Exception):
            pass

        def refuse(key, cap):
            raise Stop(cap)

        self.args_out.unlink()
        with self.assertRaises(Stop):
            self.run_extract(cache_dir=None, before_call=refuse)
        self.assertFalse(self.args_out.exists())            # stopped before launching

    def test_corrupt_cache_entry_is_ignored(self):
        first = self.run_extract()
        (self.cache / f"{first.key}.json").write_text("{not json", encoding="utf-8")
        again = self.run_extract()
        self.assertFalse(again.cached)
        self.assertEqual(len(self.ledger_rows()), 2)


if __name__ == "__main__":
    unittest.main()
