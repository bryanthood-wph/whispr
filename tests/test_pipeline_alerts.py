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
# The plugin options the hook reads, as Claude Code exports them (CLAUDE_PLUGIN_OPTION_<KEY>).
ROOT_OPTION = "CLAUDE_PLUGIN_OPTION_WHISPR_ROOT"
CONFIG_OPTION = "CLAUDE_PLUGIN_OPTION_CONFIG"


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


class HookCase(AlertsCase):
    """The hook as Claude Code runs it: a subprocess, stdin JSON, stdout JSON, exit 0."""

    def setUp(self):
        super().setUp()
        appdata = self.root / "appdata"
        (appdata / "whispr").mkdir(parents=True)
        (appdata / "whispr" / "config.yaml").write_text(yaml.safe_dump(overlay(self.root)), encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if k not in (ROOT_OPTION, CONFIG_OPTION)}
        self.env["APPDATA"] = str(appdata)
        # The alert tests run with the graph-first note off; TestGraphFirstNote turns it on.
        note = self.cfg["graph_first_note"]
        self.switch = note["switch_env"]
        self.env[self.switch] = note["off_value"]

    def hook(self, stdin: str = "{}", **options: str) -> subprocess.CompletedProcess:
        """Run the hook as hooks.json does (`-s`), with plugin options as Claude Code exports them."""
        return subprocess.run([sys.executable, "-s", str(HOOK)], input=stdin, capture_output=True, text=True,
                              env={**self.env, **options}, timeout=60)

    def only_line(self, proc: subprocess.CompletedProcess) -> str:
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        text = json.loads(proc.stdout)["systemMessage"]
        self.assertNotIn("\n", text)
        return text


class TestHook(HookCase):
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
        # Exec form with the pinned interpreter: never the python on PATH, never a shell.
        self.assertEqual(hook["command"], "${user_config.python}")
        self.assertEqual(hook["args"], ["-s", "${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py"])
        self.assertGreater(hook["timeout"], self.cfg["alerts"]["session_start"]["timeout_s"])

    def test_the_whispr_root_option_is_used(self):
        self.raise_alerts(1)
        self.assertIn("test:0", self.only_line(self.hook(**{ROOT_OPTION: str(HOOK.parents[2])})))

    def test_no_install_at_the_root_is_one_line_not_nothing(self):
        elsewhere = self.root / "not-whispr"
        elsewhere.mkdir()
        text = self.only_line(self.hook(**{ROOT_OPTION: str(elsewhere)}))
        self.assertIn("no whispr install at", text)
        self.assertIn("/whispr-setup", text)

    def test_dotdot_in_an_option_is_refused(self):
        for option, value in ((ROOT_OPTION, str(HOOK.parents[2] / "plugin" / "..")),
                              (CONFIG_OPTION, str(self.root / "x" / ".." / "config.yaml"))):
            with self.subTest(option=option):
                self.assertIn("contains '..'", self.only_line(self.hook(**{option: value})))

    def test_a_sensitive_or_non_yaml_overlay_is_refused(self):
        for name in (".env", ".env.local", "credentials.yaml", "settings.local.json", "id.key", "config.txt",
                     str(Path(".git") / "config.yaml")):
            with self.subTest(name=name):
                text = self.only_line(self.hook(**{CONFIG_OPTION: str(self.root / name)}))
                self.assertIn("not a YAML overlay", text)

    def test_the_config_option_names_the_overlay(self):
        bad = self.root / "bad.yaml"
        bad.write_text("owner: {unknown_key: 1}\n", encoding="utf-8")
        self.assertIn("config does not load", self.only_line(self.hook(**{CONFIG_OPTION: str(bad)})))


class TestGraphFirstNote(HookCase):
    """P2b: the hook's graph-first note, in Claude's context only, set by the switch or default_on."""

    def note(self) -> str:
        return self.cfg["graph_first_note"]["text"].strip()

    def test_the_note_is_context_only_when_switched_on(self):
        with self.db():
            pass
        on = self.cfg["graph_first_note"]["on_value"]
        self.env[self.switch] = on
        out = json.loads(self.hook('{"hook_event_name": "SessionStart", "source": "startup"}').stdout)
        self.assertNotIn("systemMessage", out)                       # nothing shown to you
        self.assertEqual(out["hookSpecificOutput"], {"hookEventName": "SessionStart", "additionalContext": self.note()})
        out = json.loads(self.hook("{}", **{self.switch: f" {on.upper()} "}).stdout)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], self.note())

    def test_unset_switch_follows_default_on(self):
        # off until P2b's run passes, so the nightly sync jobs never see an unvetted note
        self.assertIs(self.cfg["graph_first_note"]["default_on"], False)
        for default in (False, True):
            cfg = {**self.cfg, "graph_first_note": {**self.cfg["graph_first_note"], "default_on": default}}
            with self.subTest(default_on=default):
                for environ in ({}, {self.switch: "unexpected"}):
                    self.assertEqual(A.graph_first_note(cfg, environ), self.note() if default else "")

    def test_the_switch_turns_it_off(self):
        with self.db():
            pass
        off = self.cfg["graph_first_note"]["off_value"]
        for value in (off, f" {off.upper()} "):
            with self.subTest(value=value):
                self.assertEqual(self.hook("{}", **{self.switch: value}).stdout, "")

    def test_alerts_come_first_and_only_they_are_shown(self):
        self.raise_alerts(1)
        self.env[self.switch] = self.cfg["graph_first_note"]["on_value"]
        out = json.loads(self.hook("{}").stdout)
        self.assertIn("test:0", out["systemMessage"])
        self.assertNotIn(self.note(), out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], out["systemMessage"] + "\n\n" + self.note())

    def test_no_note_when_the_config_does_not_load(self):
        bad = self.root / "bad.yaml"
        bad.write_text("owner: {unknown_key: 1}\n", encoding="utf-8")
        self.env[self.switch] = self.cfg["graph_first_note"]["on_value"]
        out = json.loads(self.hook(**{CONFIG_OPTION: str(bad)}).stdout)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], out["systemMessage"])

    def test_context_function(self):
        with self.db():
            pass
        note = self.cfg["graph_first_note"]
        self.assertEqual(A.session_start_context(cfg=self.cfg, environ={self.switch: note["on_value"]}), ("", self.note()))
        self.assertEqual(A.session_start_context(cfg=self.cfg, environ={self.switch: note["off_value"]}), ("", ""))
        self.assertEqual(A.session_start_context(cfg=self.cfg, environ={}), ("", ""))   # default_on: false
