"""pipeline/models.py against a fake CLI (issue #8). No model calls."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import calls, models
from pipeline.config import load_config
from pipeline_helpers import fake_cli, overlay, scrubbed_env


class _CallBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        ov = overlay(self.root)
        ov["cli"] = fake_cli()
        self.cfg = load_config(overlay=ov)
        self.ledger = self.root / "ledger.jsonl"
        self.args_out = self.root / "args.json"
        # Plant an API key and a session marker that must never reach the child.
        env = scrubbed_env(self.cfg, ANTHROPIC_API_KEY="sk-test", CLAUDE_CODE_CHILD_SESSION="1",
                           FAKE_CLAUDE_ARGS_OUT=str(self.args_out))
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



class TestModelsCall(_CallBase):
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
        self.assertNotIn("--append-system-prompt-file", argv)      # no split line: sent whole
        self.assertNotIn("--effort", argv)  # extractor effort is null in defaults
        prefixes = tuple(self.cfg["auth"]["strip_env_prefixes"])
        self.assertFalse([k for k in seen["env_keys"] if k.upper().startswith(prefixes)])

    def test_effort_is_passed_when_set(self):
        self.cfg["models"]["extractor"]["effort"] = "high"
        self._call()
        argv = json.loads(self.args_out.read_text(encoding="utf-8"))["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_thinking_budget_is_set_per_role_and_never_inherited(self):
        name = self.cfg["cli"]["thinking_tokens_env"]
        with mock.patch.dict(os.environ, {name: "9999", "MAX_STRUCTURED_OUTPUT_RETRIES": "9"}):
            self._call()                                    # extractor: thinking_tokens null
            self.assertEqual(json.loads(self.args_out.read_text(encoding="utf-8"))["env_max"], {})
            self.cfg["models"]["extractor"]["thinking_tokens"] = 0
            self._call()
            self.assertEqual(json.loads(self.args_out.read_text(encoding="utf-8"))["env_max"], {name: "0"})

    def test_a_thinking_budget_is_keyed_only_when_set(self):
        key = lambda: calls.request_key(self.cfg, "extractor", "P", {"type": "object"}, None)
        unset = key()
        self.cfg["models"]["extractor"]["thinking_tokens"] = 0
        self.assertNotEqual(key(), unset)
        self.cfg["models"]["extractor"]["thinking_tokens"] = None
        self.assertEqual(key(), unset)

    def test_ledger_records_turns_result_length_and_other_usage(self):
        self._call(json_schema={"type": "object"})
        row, = self._ledger()
        self.assertEqual((row["num_turns"], row["result_chars"]), (1, len('{"ok": true}')))
        self.assertEqual(row["usage_other"], {"output_tokens_details": {"thinking_tokens": 12}})

    def test_refuses_inside_a_claude_session(self):
        os.environ["CLAUDECODE"] = "1"
        hook = mock.Mock()
        with self.assertRaises(models.ModelCallError):
            self._call(before_launch=hook)
        self.assertFalse(self.args_out.exists())  # never launched
        hook.assert_not_called()                  # so no budget reservation is left behind

    def test_launch_failure_still_writes_a_row(self):
        hook = mock.Mock()
        with mock.patch("pipeline.models.subprocess.Popen", side_effect=OSError("no such file")):
            with self.assertRaises(models.ModelCallError):
                self._call(before_launch=hook, request_key="k1")
        hook.assert_called_once()
        row = self._ledger()[0]                   # settles the reservation the hook made
        self.assertEqual((row["request_key"], row["cost_usd"], row["is_error"]), ("k1", 0.0, True))

    def test_api_key_auth_fails_closed(self):
        os.environ["FAKE_CLAUDE_MODE"] = "apikey"
        with self.assertRaises(models.AuthError):
            self._call()
        row = self._ledger()[0]   # recorded anyway, at the budget cap as an upper bound
        self.assertEqual((row["auth_source"], row["cost_usd"], row["cost_is_upper_bound"]), ("ANTHROPIC_API_KEY", 0.5, True))

    def test_missing_init_event_fails_closed(self):
        os.environ["FAKE_CLAUDE_MODE"] = "noinit"
        with self.assertRaises(models.AuthError):
            self._call()
        self.assertEqual(len(self._ledger()), 1)

    def test_error_still_records_spend(self):
        os.environ["FAKE_CLAUDE_MODE"] = "error"
        with self.assertRaises(models.ModelCallError):
            self._call()
        self.assertEqual(self._ledger()[0]["cost_usd"], 0.0123)
        self.assertTrue(self._ledger()[0]["is_error"])

    def test_rejected_arguments_record_no_spend(self):
        # No init event and no result: the CLI never started a session, so nothing was
        # spent; booking the cap would charge the budget for a call that never ran.
        os.environ["FAKE_CLAUDE_MODE"] = "badargs"
        with self.assertRaises(models.ModelCallError) as ctx:
            self._call(request_key="k1")
        self.assertIn("not a valid JSON Schema", str(ctx.exception))
        row = self._ledger()[0]
        self.assertEqual((row["request_key"], row["cost_usd"], row["cost_is_upper_bound"], row["is_error"]),
                         ("k1", 0.0, False, True))

    def test_any_event_keeps_the_upper_bound(self):
        # Something ran (a hook, say) before the call died: it may have spent.
        os.environ["FAKE_CLAUDE_MODE"] = "hooksonly"
        with self.assertRaises(models.ModelCallError):
            self._call()
        row = self._ledger()[0]
        self.assertEqual((row["cost_usd"], row["cost_is_upper_bound"]), (0.5, True))

    def test_timeout_before_any_event_keeps_the_upper_bound(self):
        os.environ["FAKE_CLAUDE_MODE"] = "silenthang"
        self.cfg["cli"]["timeout_s"] = 2
        with self.assertRaises(models.ModelCallError) as ctx:
            self._call()
        self.assertIn("timed out", str(ctx.exception))
        row = self._ledger()[0]
        self.assertEqual((row["cost_usd"], row["cost_is_upper_bound"]), (0.5, True))

    def test_non_object_json_line_is_skipped(self):
        os.environ["FAKE_CLAUDE_MODE"] = "junk"
        out = self._call(json_schema={"type": "object"})
        self.assertEqual(out.structured, {"ok": True})
        self.assertEqual(len(self._ledger()), 1)

    def test_schema_dialect_is_not_sent_to_the_cli(self):
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                  "properties": {"ok": {"type": "boolean"}}}
        self._call(json_schema=schema)
        argv = json.loads(self.args_out.read_text(encoding="utf-8"))["argv"]
        sent = json.loads(argv[argv.index("--json-schema") + 1])
        self.assertEqual(sent, {k: v for k, v in schema.items() if k != "$schema"})
        self.assertIn("$schema", schema)                    # the caller's schema is untouched

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
        row = self._ledger()[0]
        self.assertEqual((row["cost_usd"], row["cost_is_upper_bound"]), (0.5, True))


class TestSplitPrompt(_CallBase):
    PROMPT = "CONTEXT\nTRANSCRIPT\nMe: hello\n\nQUESTION\nIs it there?\nALLOWED ANSWERS: yes|no\n"

    def test_shared_prefix_goes_to_the_cached_system_prompt(self):
        models.call(self.cfg, "judge", self.PROMPT, max_budget_usd=0.5, ledger=self.ledger,
                    json_schema={"type": "object"}, system_prompt="SYS")
        seen = json.loads(self.args_out.read_text(encoding="utf-8"))
        self.assertEqual(seen["system_append"], "CONTEXT\nTRANSCRIPT\nMe: hello\n\n")
        self.assertEqual(seen["stdin"], "QUESTION\nIs it there?\nALLOWED ANSWERS: yes|no\n")
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "SYS")    # the variant's prompt stays
        self.assertFalse(Path(argv[argv.index("--append-system-prompt-file") + 1]).exists())

    def test_prefix_file_is_removed_when_the_call_fails(self):
        os.environ["FAKE_CLAUDE_MODE"] = "error"
        with self.assertRaises(models.ModelCallError):
            models.call(self.cfg, "judge", self.PROMPT, max_budget_usd=0.5, ledger=self.ledger)
        argv = json.loads(self.args_out.read_text(encoding="utf-8"))["argv"]
        self.assertFalse(Path(argv[argv.index("--append-system-prompt-file") + 1]).exists())

    def test_prefix_file_is_removed_when_refused_before_launch(self):
        work = models.data_dir(self.cfg, "prompt-parts")

        def refuse():
            raise RuntimeError("budget")
        with self.assertRaises(RuntimeError):
            models.call(self.cfg, "judge", self.PROMPT, max_budget_usd=0.5, ledger=self.ledger,
                        before_launch=refuse)
        self.assertFalse(self.args_out.exists())
        self.assertEqual(list(work.glob("system-*")), [])

    def test_split_is_before_the_last_split_line_and_never_at_the_start(self):
        prompt = "A\nQUESTION\nB\nTHE ITEM\nC\n"
        self.assertEqual(models.split_prompt(self.cfg, prompt), ("A\nQUESTION\nB\n", "THE ITEM\nC\n"))
        self.assertEqual(models.split_prompt(self.cfg, "QUESTION\nB\n"), (None, "QUESTION\nB\n"))
        self.assertEqual(models.split_prompt(self.cfg, "A QUESTION\nB\n"), (None, "A QUESTION\nB\n"))

    def test_request_key_changes_only_for_a_split_prompt(self):
        from pipeline import calls
        whole = load_config(overlay={**overlay(self.root), "cli": {**fake_cli(), "cached_prefix_until": []}})
        for prompt, differs in ((self.PROMPT, True), ("PROMPT TEXT", False)):
            a = calls.request_key(self.cfg, "judge", prompt, {"type": "object"}, None)
            b = calls.request_key(whole, "judge", prompt, {"type": "object"}, None)
            self.assertEqual(a != b, differs, prompt)


class TestResolveExecutable(unittest.TestCase):
    def test_existing_file_is_used_as_is(self):
        self.assertEqual(models.resolve_executable(sys.executable), sys.executable)

    def test_missing_cli_fails(self):
        with self.assertRaises(models.ModelCallError):
            models.resolve_executable("definitely-not-a-cli-xyz")


if __name__ == "__main__":
    unittest.main()
