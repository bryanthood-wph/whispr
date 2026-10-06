"""pipeline/setup.py: the /whispr-setup steps. Temp dirs only: a temp recorder config, a
temp overlay path and a fake interpreter folder, never %APPDATA% or the real data dir."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest import mock

import yaml

import pipeline.__main__ as cli
from kg import db
from kg.state import State
from pipeline import setup as P
from pipeline.config import ConfigError, load_config
from pipeline.run import EXIT_FAILED, EXIT_OK
from test_pipeline_run import PipelineCase

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "plugin" / "bin" / "pipeline.py"
TODAY = date(2026, 10, 6)
ANSWERS = ["owner.name=Pat Example", "owner.email=pat@example.com", "owner.tenant="]


class SetupCase(PipelineCase):
    def setUp(self):
        super().setUp()
        self.overlay_path = self.root / "appdata" / "whispr" / "config.yaml"
        self.recorder_path = self.root / "recorder.yaml"
        self.recorder_path.write_text(yaml.safe_dump({
            "paths": {k: str(self.root / "rec" / k) for k in ("transcripts", "recordings", "logs")},
            "audio": {"mic_name_match": "Headset"},
        }), encoding="utf-8")
        bindir = self.root / "python"
        bindir.mkdir()
        for name in ("python.exe", self.cfg["schedules"]["interpreter_name"]):
            (bindir / name).write_bytes(b"")
        self.environ = {"LOCALAPPDATA": str(self.root / "local"), "OneDrive": str(self.root / "onedrive")}
        self.kw = {"python": str(bindir / "python.exe"), "environ": self.environ, "today": TODAY,
                   "recorder_path": self.recorder_path, "root": self.root}

    def plan(self) -> dict:
        return P.plan(self.overlay_path, **self.kw)

    def write(self, pairs: list[str], yes: bool) -> tuple[int, str]:
        lines: list[str] = []
        code = P.write(self.overlay_path, pairs, yes=yes, out=lines.append, **self.kw)
        return code, "\n".join(lines)


class TestPlan(SetupCase):
    def test_from_nothing_it_derives_suggests_and_asks(self):
        values = self.plan()["values"]
        source = {k: v["source"] for k, v in values.items()}
        self.assertEqual({k for k, s in source.items() if s == P.ASK}, set(self.cfg["setup"]["ask"]))
        self.assertEqual(source["paths.data_dir"], P.SUGGESTED)
        self.assertEqual(values["paths.data_dir"]["value"], str(self.root / "local" / self.cfg["setup"]["data_dir"]["name"]))
        self.assertEqual(values["backup.destination"]["value"],
                         str(self.root / "onedrive" / self.cfg["setup"]["backup"]["name"]))
        self.assertEqual(values["paths.transcripts"], {"value": str(self.root / "rec" / "transcripts"), "source": P.DERIVED})
        self.assertEqual(values["paths.recorder_log"]["value"], str(self.root / "rec" / "logs" / "whispr.log"))
        self.assertEqual(values["pipeline.run.process_since"]["value"], TODAY.isoformat())
        self.assertEqual(values["liveness.working_dir"]["value"], str(self.root))
        pythonw = str(self.root / "python" / self.cfg["schedules"]["interpreter_name"])
        self.assertEqual(values["liveness.command"]["value"],
                         [pythonw, *self.cfg["schedules"]["interpreter_args"], *self.cfg["setup"]["recorder_args"]])
        self.assertEqual(self.plan()["recorder"]["mic_name_match"], "Headset")

    def test_no_folder_to_suggest_from_is_asked(self):
        self.environ.clear()
        values = self.plan()["values"]
        self.assertEqual(values["paths.data_dir"]["source"], P.ASK)
        self.assertEqual(values["backup.destination"]["source"], P.ASK)

    def test_the_existing_overlay_is_kept(self):
        self.overlay_path.parent.mkdir(parents=True)
        self.overlay_path.write_text(yaml.safe_dump({"pipeline": {"run": {"process_since": "2026-01-01"}}}),
                                     encoding="utf-8")
        entry = self.plan()["values"]["pipeline.run.process_since"]
        self.assertEqual(entry, {"value": "2026-01-01", "source": P.OVERLAY})   # never moved forward on a re-run

    def test_a_version_stamped_interpreter_is_a_problem_and_blocks_the_write(self):
        stamped = self.root / "WindowsApps" / "PythonSoftwareFoundation.Python.3.14_3.14.5.0_x64__qbz5n2kfra8p0"
        stamped.mkdir(parents=True)
        for name in ("python.exe", self.cfg["schedules"]["interpreter_name"]):
            (stamped / name).write_bytes(b"")
        self.kw["python"] = str(stamped / "python.exe")
        self.assertEqual(self.plan()["values"]["liveness.command"]["source"], P.PROBLEM)
        with self.assertRaisesRegex(P.StillToAsk, "liveness.command .*version-stamped"):
            self.write(ANSWERS, yes=True)


class TestWrite(SetupCase):
    def test_nothing_is_written_without_the_answers_or_without_yes(self):
        with self.assertRaisesRegex(P.StillToAsk, "owner.name; owner.email; owner.tenant"):
            self.write([], yes=True)
        code, text = self.write(ANSWERS, yes=False)
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("owner.name: (unset) -> \"Pat Example\"", text)
        self.assertFalse(self.overlay_path.exists())

    def test_yes_writes_an_overlay_that_loads(self):
        code, _ = self.write(ANSWERS, yes=True)
        self.assertEqual(code, EXIT_OK)
        cfg = load_config(overlay_path=self.overlay_path)
        self.assertEqual((cfg["owner"]["name"], cfg["owner"]["tenant"]), ("Pat Example", None))
        self.assertEqual(cfg["pipeline"]["run"]["process_since"], TODAY.isoformat())
        self.assertEqual(cfg["liveness"]["working_dir"], str(self.root))
        self.assertEqual(self.write(ANSWERS, yes=False), (EXIT_OK, f"overlay {self.overlay_path}: no change"))

    def test_other_keys_of_the_overlay_are_kept(self):
        self.overlay_path.parent.mkdir(parents=True)
        self.overlay_path.write_text(yaml.safe_dump({"kg": {"search_cards": 7}}), encoding="utf-8")
        self.write(ANSWERS, yes=True)
        self.assertEqual(load_config(overlay_path=self.overlay_path)["kg"]["search_cards"], 7)

    def test_set_overrides_a_suggestion_and_takes_only_setup_keys(self):
        elsewhere = str(self.root / "elsewhere")
        self.write([*ANSWERS, f"backup.destination={elsewhere}"], yes=True)
        self.assertEqual(load_config(overlay_path=self.overlay_path)["backup"]["destination"], elsewhere)
        for bad in ("kg.search_cards=3", "owner.name"):
            with self.subTest(bad=bad), self.assertRaises(P.SetupError):
                self.write([*ANSWERS, bad], yes=True)

    def test_a_backup_inside_the_data_dir_is_refused(self):
        inside = str(self.root / "local" / self.cfg["setup"]["data_dir"]["name"] / "backups")
        with self.assertRaisesRegex(ConfigError, "inside paths.data_dir"):
            self.write([*ANSWERS, f"backup.destination={inside}"], yes=True)
        self.assertFalse(self.overlay_path.exists())


class TestInitAndTestAlert(SetupCase):
    def test_init_creates_the_database_and_the_test_alert_reaches_the_surface(self):
        lines: list[str] = []
        self.assertEqual(P.init(self.cfg, out=lines.append), EXIT_OK)
        self.assertTrue(db.database_path(self.cfg).is_file())
        self.assertEqual(P.test_alert(self.cfg, out=lines.append), EXIT_OK)
        key = self.cfg["setup"]["test_alert"]["key"]
        self.assertIn(key, lines[-2])                # the hook's line names it
        self.assertTrue(lines[-1].startswith(f"key {key} "))
        with contextlib.closing(db.connect(self.cfg)) as conn:
            self.assertIsNotNone(State(conn, self.cfg).alert(key))


class TestCli(SetupCase):
    def cli(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(P, "recorder", return_value=P.recorder(self.recorder_path)):
            code = cli.main(["--overlay", str(self.overlay_path), "setup", *argv])
        return code, buf.getvalue()

    def test_plan_write_init_and_test_alert(self):
        code, out = self.cli("plan")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("owner.name: null [ask]", out)
        sets = [arg for pair in ANSWERS for arg in ("--set", pair)]
        sets += ["--set", f"paths.data_dir={self.root / 'data'}", "--set", f"backup.destination={self.root / 'bk'}"]
        self.assertEqual(self.cli("write", *sets)[0], EXIT_FAILED)
        self.assertEqual(self.cli("write", *sets, "--yes")[0], EXIT_OK)
        self.assertEqual(self.cli("write", "--set", "kg.search_cards=1")[0], P.EXIT_USAGE)
        self.assertEqual(self.cli("init")[0], EXIT_OK)
        code, out = self.cli("test-alert")
        self.assertEqual(code, EXIT_OK)
        self.assertIn(self.cfg["setup"]["test_alert"]["key"], out)

    def test_init_with_no_overlay_is_a_config_error(self):
        with mock.patch.dict(os.environ, {"APPDATA": str(self.root / "none")}):
            self.assertEqual(self.cli("init")[0], P.EXIT_USAGE)


class TestLauncher(SetupCase):
    """plugin/bin/pipeline.py, as /whispr-setup runs it: from an unrelated folder that
    holds a decoy `pipeline` package."""

    def launch(self, *argv: str) -> subprocess.CompletedProcess:
        cwd = self.root / "elsewhere"
        (cwd / "pipeline").mkdir(parents=True, exist_ok=True)
        (cwd / "pipeline" / "__init__.py").write_text("raise SystemExit('decoy imported')\n", encoding="utf-8")
        return subprocess.run([sys.executable, "-s", str(LAUNCHER), *argv], cwd=cwd, capture_output=True,
                              text=True, timeout=60)

    def test_it_runs_the_pipeline_of_the_folder_given(self):
        proc = self.launch(str(REPO), "setup", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("test-alert", proc.stdout)

    def test_a_folder_that_is_not_an_install_or_has_dotdot_is_refused(self):
        for folder, why in ((str(self.root), "no whispr install"), (str(REPO / "plugin" / ".."), "contains '..'")):
            with self.subTest(folder=folder):
                proc = self.launch(folder, "doctor")
                self.assertEqual(proc.returncode, P.EXIT_USAGE)
                self.assertIn(why, proc.stderr)
