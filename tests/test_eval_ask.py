"""eval/ask.py and the replicate index in pipeline.calls (fake CLI only)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval.ask import make_ask
from pipeline import calls
from pipeline.config import load_config
from pipeline_helpers import overlay

FAKE = str(Path(__file__).with_name("fake_claude.py"))
SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


class TestAsk(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        ov = overlay(self.root)
        ov["cli"] = {"executable": sys.executable, "base_args": [FAKE], "timeout_s": 20}
        self.cfg = load_config(overlay=ov)
        self.ledger = self.root / "ledger.jsonl"
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def calls_made(self):
        return len(self.ledger.read_text(encoding="utf-8").splitlines()) if self.ledger.exists() else 0

    def test_replicate_zero_keeps_the_key_and_others_differ(self):
        base = calls.request_key(self.cfg, "judge", "P", SCHEMA, None)
        self.assertEqual(base, calls.request_key(self.cfg, "judge", "P", SCHEMA, None, 0))
        keys = {calls.request_key(self.cfg, "judge", "P", SCHEMA, None, r) for r in range(3)}
        self.assertEqual(len(keys), 3)

    def test_cli_base_args_are_in_the_key(self):
        base = calls.request_key(self.cfg, "judge", "P", SCHEMA, None)
        same = {**self.cfg, "cli": {**self.cfg["cli"], "base_args": list(self.cfg["cli"]["base_args"])}}
        self.assertEqual(base, calls.request_key(same, "judge", "P", SCHEMA, None))
        other = {**self.cfg, "cli": {**self.cfg["cli"], "base_args": [*self.cfg["cli"]["base_args"],
                                                                      "--setting-sources", ""]}}
        self.assertNotEqual(base, calls.request_key(other, "judge", "P", SCHEMA, None))

    def test_ask_caches_per_replicate_and_reports_to_the_guard(self):
        seen, cap = [], self.cfg["eval"]["max_budget_per_call_usd"]
        ask = make_ask(self.cfg, ledger=self.ledger, cache_dir=self.root / "cache", max_budget_usd=cap,
                       before_call=lambda key, c: seen.append(c))
        self.assertEqual(ask("judge", "P", SCHEMA), {"ok": True})
        ask("judge", "P", SCHEMA)                                  # cache hit
        ask("judge", "P", SCHEMA, replicate=1)                     # a real second sample
        self.assertEqual(self.calls_made(), 2)
        self.assertEqual(seen, [cap] * 2)
        argv_model = json.loads(self.ledger.read_text(encoding="utf-8").splitlines()[0])["model"]
        self.assertEqual(argv_model, self.cfg["models"]["judge"]["model"])


if __name__ == "__main__":
    unittest.main()
