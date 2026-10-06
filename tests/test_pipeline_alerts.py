"""pipeline/alerts.py and plugin/hooks/session_start.py: the alert surface and the
SessionStart hook (D.6). Temp dirs only; the hook runs as a subprocess against a temp
overlay, never the real one."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import yaml

import pipeline.__main__ as cli
from kg import db
from pipeline import alerts as A
from pipeline import run as runner
from pipeline_helpers import overlay
from test_pipeline_run import T0, PipelineCase

HOOK = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "session_start.py"


class AlertsCase(PipelineCase):
    def raise_alerts(self, n: int, message: str = "the thing broke") -> None:
        with self.db() as (_, state):
            for i in range(n):
                state.raise_alert("test", f"test:{i}", f"{message} {i}", f"fix {i}", now=T0)

    def text(self, **kw) -> str:
        return A.session_start_text(cfg=self.cfg, **kw)


class TestAlerts(AlertsCase):
    def test_list_and_ack(self):
        lines: list[str] = []
        with self.db():
            pass                                    # the database exists, empty
        self.assertEqual(A.list_alerts(self.cfg, out=lines.append), runner.EXIT_OK)
        self.assertEqual(lines, ["no open alerts"])
        self.raise_alerts(2)
        lines.clear()
        self.assertEqual(A.list_alerts(self.cfg, out=lines.append), runner.EXIT_OK)
        text = "\n".join(lines)
        self.assertIn("test:0", text)
        self.assertIn("fix: fix 1", text)
        self.assertEqual(A.acknowledge(self.cfg, "test:0", out=lines.append), runner.EXIT_OK)
        self.assertEqual(A.acknowledge(self.cfg, "test:0", out=lines.append), runner.EXIT_USAGE)
        with self.db() as (_, state):
            self.assertEqual([a["dedupe_key"] for a in state.open_alerts()], ["test:1"])

    def test_list_without_a_database_fails(self):
        lines: list[str] = []
        self.assertEqual(A.list_alerts(self.cfg, out=lines.append), runner.EXIT_FAILED)
        self.assertIn("no database", lines[0])

    def test_cli(self):
        self.raise_alerts(1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(cli, "load_config", return_value=self.cfg):
            self.assertEqual(cli.main(["alerts", "--ack", "test:0"]), runner.EXIT_OK)
            self.assertEqual(cli.main(["alerts", "--ack", "nope"]), runner.EXIT_USAGE)
            self.assertEqual(cli.main(["alerts"]), runner.EXIT_OK)
        self.assertIn("no open alerts", buf.getvalue())


class TestSessionStart(AlertsCase):
    def test_nothing_when_none_are_open(self):
        with self.db():
            pass
        self.assertEqual(self.text(), "")

    def test_one_short_message_when_alerts_are_open(self):
        s = self.cfg["alerts"]["session_start"]
        self.raise_alerts(s["max_alerts"] + 2, message="x" * (s["message_chars"] * 2))
        text = self.text()
        self.assertTrue(text.startswith(f"whispr: {s['max_alerts'] + 2} open alert(s)."), text)
        self.assertIn("and 2 more", text)
        self.assertIn("python -m pipeline alerts", text)
        self.assertNotIn("x" * s["message_chars"], text)                   # clipped
        self.assertEqual(text.count("test:"), s["max_alerts"])
        self.assertNotIn("\n", text)

    def test_a_broken_or_missing_database_is_a_short_message(self):
        self.assertIn("could not be read", self.text())                      # missing
        path = db.database_path(self.cfg)
        path.write_bytes(b"not a database" * 200)
        text = self.text()
        self.assertIn("could not be read", text)
        self.assertIn("pipeline doctor", text)

    def test_a_slow_read_is_cut_off(self):
        self.cfg["alerts"]["session_start"]["timeout_s"] = 0.1
        started = time.monotonic()
        text = self.text(read=lambda cfg: time.sleep(5) or [])
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("not read within 0.1 s", text)

    def test_a_bad_config_is_a_short_message_and_the_cli_exits_0(self):
        bad = self.root / "bad.yaml"
        bad.write_text("owner: {unknown_key: 1}\n", encoding="utf-8")
        self.assertIn("config does not load (ConfigError)", A.session_start_text(bad))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["--overlay", str(bad), "alerts", "--session-start"]), runner.EXIT_OK)
        self.assertIn("config does not load", buf.getvalue())

    def test_session_start_never_raises(self):
        lines: list[str] = []
        with mock.patch.object(A, "session_start_text", side_effect=RuntimeError("boom")):
            self.assertEqual(A.session_start(out=lines.append), runner.EXIT_OK)
        self.assertEqual(lines, [])


class TestHook(AlertsCase):
    """The hook as Claude Code runs it: a subprocess, stdin JSON, stdout JSON, exit 0."""

    def setUp(self):
        super().setUp()
        appdata = self.root / "appdata"
        (appdata / "whispr").mkdir(parents=True)
        (appdata / "whispr" / "config.yaml").write_text(yaml.safe_dump(overlay(self.root)), encoding="utf-8")
        self.env = {**os.environ, "APPDATA": str(appdata)}

    def hook(self, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True,
                              env=self.env, timeout=60)

    def test_malformed_stdin_still_exits_0(self):
        for stdin in ("", "not json{", "[1, 2]", '{"hook_event_name": null}'):
            with self.subTest(stdin=stdin):
                proc = self.hook(stdin)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")

    def test_alerts_are_shown_and_none_shows_nothing(self):
        with self.db():
            pass
        self.assertEqual(self.hook('{"hook_event_name": "SessionStart", "source": "startup"}').stdout, "")
        self.raise_alerts(1)
        proc = self.hook('{"hook_event_name": "SessionStart", "source": "startup"}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIn("test:0", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"],
                         {"hookEventName": "SessionStart", "additionalContext": out["systemMessage"]})

    def test_another_event_does_nothing(self):
        self.raise_alerts(1)
        proc = self.hook('{"hook_event_name": "PreToolUse"}')
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_a_broken_database_is_a_short_message_and_exit_0(self):
        db.database_path(self.cfg).write_bytes(b"garbage" * 300)
        proc = self.hook("{}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("could not be read", json.loads(proc.stdout)["systemMessage"])

    def test_hooks_json_runs_this_script(self):
        spec = json.loads((HOOK.parent / "hooks.json").read_text(encoding="utf-8"))
        entry, = spec["hooks"]["SessionStart"]
        hook, = entry["hooks"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py", hook["command"])
        self.assertGreater(hook["timeout"], self.cfg["alerts"]["session_start"]["timeout_s"])
