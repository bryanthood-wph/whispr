"""pipeline/models.py against a fake CLI (issue #8). No model calls."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import models
from pipeline.config import load_config
from pipeline_helpers import overlay

FAKE = str(Path(__file__).with_name("fake_claude.py"))


class TestModelsCall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        ov = overlay(self.root)
        ov["cli"] = {"executable": sys.executable, "base_args": [FAKE], "timeout_s": 20}
        self.cfg = load_config(overlay=ov)
        self.ledger = self.root / "ledger.jsonl"
        self.args_out = self.root / "args.json"
        # The test process may itself run inside a Claude session; clear its markers
        # and plant an API key that must never reach the child.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
        env.update({"ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CODE_CHILD_SESSION": "1",
                    "FAKE_CLAUDE_ARGS_OUT": str(self.args_out)})
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _call(self, **kw):
        kw.setdefault("max_budget_usd", 0.5)
        return models.call(self.cfg, "extractor", "PROMPT TEXT", ledger=self.ledger, **kw)

    def _ledger(self):
        return [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines()] if self.ledger.exists() else []

    def test_ok_returns_structured_and_records_cost(self):
        out = self._call(json_schema={"type": "object"}, request_key="k1")
        self.assertEqual(out.structured, {"ok": True})
        self.assertEqual(out.auth_source, "none")
        rows = self._ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["cost_usd"], rows[0]["request_key"], rows[0]["auth_source"]), (0.0123, "k1", "none"))

    def test_child_gets_prompt_flags_and_a_clean_env(self):
        self._call(json_schema={"type": "object"}, system_prompt="SYS")
        seen = json.loads(self.args_out.read_text(encoding="utf-8"))
        self.assertEqual(seen["stdin"], "PROMPT TEXT")
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.50")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "SYS")
        self.assertIn("--json-schema", argv)
        self.assertNotIn("--effort", argv)  # extractor effort is null in defaults
        self.assertFalse([k for k in seen["env_keys"] if k.upper().startswith(("CLAUDE", "ANTHROPIC"))])

    def test_effort_is_passed_when_set(self):
        self.cfg["models"]["extractor"]["effort"] = "high"
        self._call()
        argv = json.loads(self.args_out.read_text(encoding="utf-8"))["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_refuses_inside_a_claude_session(self):
        os.environ["CLAUDECODE"] = "1"
        with self.assertRaises(models.ModelCallError):
            self._call()
        self.assertFalse(self.args_out.exists())  # never launched

    def test_api_key_auth_fails_closed(self):
        os.environ["FAKE_CLAUDE_MODE"] = "apikey"
        with self.assertRaises(models.AuthError):
            self._call()
        self.assertEqual(self._ledger(), [])

    def test_error_still_records_spend(self):
        os.environ["FAKE_CLAUDE_MODE"] = "error"
        with self.assertRaises(models.ModelCallError):
            self._call()
        self.assertEqual(self._ledger()[0]["cost_usd"], 0.0123)
        self.assertTrue(self._ledger()[0]["is_error"])

    def test_missing_structured_output_fails(self):
        os.environ["FAKE_CLAUDE_MODE"] = "nostructured"
        with self.assertRaises(models.ModelCallError):
            self._call(json_schema={"type": "object"})

    def test_timeout_kills_and_fails(self):
        os.environ["FAKE_CLAUDE_MODE"] = "hang"
        self.cfg["cli"]["timeout_s"] = 2
        with self.assertRaises(models.ModelCallError) as ctx:
            self._call()
        self.assertIn("timed out", str(ctx.exception))


class TestResolveExecutable(unittest.TestCase):
    def test_existing_file_is_used_as_is(self):
        self.assertEqual(models.resolve_executable(sys.executable), sys.executable)

    def test_missing_cli_fails(self):
        with self.assertRaises(models.ModelCallError):
            models.resolve_executable("definitely-not-a-cli-xyz")


if __name__ == "__main__":
    unittest.main()
