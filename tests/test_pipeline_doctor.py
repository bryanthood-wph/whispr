"""pipeline/doctor.py: the status report (D.6). Fakes for the scheduler, the recorder
and the model; temp dirs only."""

from __future__ import annotations

import contextlib
import io
import json
from datetime import timedelta
from unittest import mock

import pipeline.__main__ as cli
from pipeline import daily, doctor, liveness, models, render, write
from pipeline import run as runner
from pipeline import schedule as S
from pipeline.config import ConfigError
from test_pipeline_daily import NOW, DailyCase
from test_pipeline_liveness import FakeRecorder

LATER = NOW + timedelta(minutes=5)


class DoctorCase(DailyCase):
    def setUp(self):
        super().setUp()
        self.cfg["liveness"]["command"] = ["pythonw.exe", "-m", "whispr"]
        self.cfg["liveness"]["working_dir"] = str(self.root)
        self.schedule = mock.patch.object(S, "check", return_value=[])
        self.check = self.schedule.start()
        self.addCleanup(self.schedule.stop)
        self.assertEqual(daily.daily(self.cfg, now=NOW, resolve_call=self.fake, out=lambda line: None), runner.EXIT_OK)
        with self.db() as (_, state):
            run_id = state.begin_run(runner.JOB, NOW)
            state.finish_run(run_id, processed=0, eligible=0, backlog=0, now=NOW)
        note = write.note_path(self.cfg, self.path.stem)
        note.write_text(f"# Weekly Sync\n\n## {render.MY_ACTIONS}\n\nNone\n", encoding="utf-8")
        with mock.patch.object(runner.db, "utc_now", return_value=NOW.isoformat()):
            liveness.liveness(self.cfg, recorder=FakeRecorder(held=True), now=NOW, out=lambda line: None)

    def report(self, held: bool = True, at=LATER) -> dict:
        return doctor.report(self.cfg, recorder=FakeRecorder(held=held), now=at, run=object())


class TestDoctor(DoctorCase):
    def test_a_healthy_install_is_all_clear(self):
        rep = self.report()
        self.assertEqual(rep["attention"], [])
        self.assertEqual(rep["jobs"][runner.JOB]["backlog"], 0)
        self.assertEqual(rep["jobs"][daily.JOB]["last_success"], rep["jobs"][daily.JOB]["last_run"]["finished_at"])
        self.assertEqual(rep["reconciliation"]["problems"], [])
        self.assertEqual(rep["liveness"]["last"]["outcome"], liveness.ALIVE)
        self.assertTrue(rep["recorder"]["running"])
        self.assertEqual(rep["notes"]["missing_my_actions"], 0)
        self.assertEqual(rep["tasks"]["window_days"], self.cfg["kg"]["tasks"]["funnel_days"])
        self.assertIn("captured", rep["tasks"]["stages"])
        self.assertEqual(rep["auth"], {"last_call": None})

    def test_each_problem_needs_attention(self):
        models.append_jsonl(runner.files(self.cfg, "ledger"), {"ts": NOW.isoformat(), "auth_source": "ANTHROPIC_API_KEY",
                                                              "cost_usd": 0.1})
        models.append_jsonl(self.root / self.cfg["doctor"]["incidents_file"],
                            {"ts": (NOW - timedelta(days=1)).isoformat(), "kind": "crash"})
        write.note_path(self.cfg, self.path.stem).unlink()
        self.check.return_value = [S.Finding("whispr-daily", S.MISSING, "not registered")]
        with self.db() as (_, state):
            state.raise_alert("test", "test:one", "something broke", "fix it", now=NOW)
        later = NOW + timedelta(hours=30)
        rep = self.report(held=False, at=later)
        text = "\n".join(rep["attention"])
        for words in ("pipeline: last successful run", "daily: last successful run", "1 open alert(s): test:one",
                      "whispr-daily: missing", "ANTHROPIC_API_KEY", "recorder: not running", "{'crash': 1}",
                      "liveness: last check", "no note with a My Actions section"):
            self.assertIn(words, text)
        self.assertEqual(rep["notes"]["ids"], [self.path.stem])

    def test_a_section_that_cannot_be_read_says_so_and_the_rest_still_report(self):
        self.check.side_effect = S.ScheduleError("PowerShell did not run")
        rep = self.report()
        self.assertEqual(rep["schedule"], {"error": "ScheduleError: PowerShell did not run"})
        self.assertEqual(rep["attention"], ["schedule: could not be read: ScheduleError: PowerShell did not run"])
        self.assertIn("jobs", rep)

    def test_no_database_needs_attention_and_writes_nothing(self):
        (self.root / "data" / self.cfg["kg"]["database"]).unlink()
        for side in ("-wal", "-shm"):
            (self.root / "data" / (self.cfg["kg"]["database"] + side)).unlink(missing_ok=True)
        rep = self.report()
        self.assertIn("no database", rep["attention"][0])
        self.assertNotIn("jobs", rep)
        self.assertFalse((self.root / "data" / self.cfg["kg"]["database"]).exists())

    def test_reconciliation_problems_are_listed(self):
        self.cfg["paths"]["recorder_log"] = None
        daily.daily(self.cfg, now=NOW + timedelta(minutes=1), resolve_call=self.fake, out=lambda line: None)
        rep = self.report()
        self.assertTrue(any(a.startswith("reconciliation (") and "paths.recorder_log is not set" in a
                            for a in rep["attention"]), rep["attention"])

    def test_cli_json_and_exit_codes(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(cli, "load_config", return_value=self.cfg), \
                mock.patch.object(liveness, "WinRecorder", return_value=FakeRecorder(held=False)):
            code = cli.main(["doctor", "--json"], runner=lambda cfg: object())
        rep = json.loads(buf.getvalue())
        self.assertEqual(code, runner.EXIT_FAILED)
        self.assertIn("recorder: not running", "\n".join(rep["attention"]))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(cli, "load_config", side_effect=ConfigError("bad")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["doctor"]), 2)

    def test_text_output_lists_attention_last(self):
        lines: list[str] = []
        code = doctor.doctor(self.cfg, recorder=FakeRecorder(held=False), now=LATER, run=object(), out=lines.append)
        self.assertEqual(code, runner.EXIT_FAILED)
        self.assertTrue(lines[-2].startswith("NEEDS ATTENTION (1)"), lines)
        self.assertIn("recorder: not running", lines[-1])
