"""pipeline/liveness.py: the recorder liveness check (D.6, L14). A fake Recorder
stands in for the machine: no mutex is probed, nothing is launched, temp dirs only."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
from pathlib import Path
from typing import Optional
from unittest import mock

import pipeline.__main__ as cli
from pipeline import liveness as L
from pipeline import models
from pipeline import run as runner
from pipeline import schedule as S
from test_pipeline_run import T0, PipelineCase


class FakeRecorder:
    """The machine. held: the mutex answers in order (the last repeats); come_up_after:
    probes after a launch before the mutex appears (None: never)."""

    def __init__(self, held: bool = False, come_up_after: Optional[int] = 1, launch_error: Exception = None,
                 bindable: bool = True, probe_error: Exception = None):
        self.is_held, self.come_up_after, self.launch_error = held, come_up_after, launch_error
        self.bindable, self.probe_error = bindable, probe_error
        self.launched: list[tuple[str, str]] = []
        self.reaped: list[int] = []
        self.released: list[int] = []
        self.now = 0.0
        self.probes_since_launch = 0

    def held(self, mutex: str) -> bool:
        if self.probe_error and not self.launched:
            raise self.probe_error
        if self.launched:
            self.probes_since_launch += 1
            return self.come_up_after is not None and self.probes_since_launch >= self.come_up_after
        return self.is_held

    def launch(self, command_line: str, working_dir: str) -> int:
        if self.launch_error:
            raise self.launch_error
        self.launched.append((command_line, working_dir))
        return 4242

    def bind(self, pid: int):
        return 99 if self.bindable else None

    def reap(self, handle: int) -> bool:
        self.reaped.append(handle)
        return True

    def release(self, handle: int) -> None:
        self.released.append(handle)

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now


class LivenessCase(PipelineCase):
    def setUp(self):
        super().setUp()
        self.exe = self.root / "venv" / "pythonw.exe"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"")
        self.cfg["liveness"]["command"] = [str(self.exe), "-m", "whispr"]
        self.cfg["liveness"]["working_dir"] = str(self.root)
        self.out: list[str] = []

    def check(self, recorder: FakeRecorder, dry_run: bool = False) -> int:
        return L.liveness(self.cfg, dry_run=dry_run, recorder=recorder, now=T0, out=self.out.append)

    def log(self) -> list[dict]:
        return [r for r in models.read_jsonl(runner.files(self.cfg, "log")) if r.get("job") == L.JOB]

    def alerts(self) -> dict[str, dict]:
        with self.db() as (_, state):
            return {a["dedupe_key"]: a for a in state.open_alerts()}


class TestLiveness(LivenessCase):
    def test_a_held_mutex_never_launches(self):
        fake = FakeRecorder(held=True)
        self.assertEqual(self.check(fake), runner.EXIT_OK)
        self.assertEqual(fake.launched, [])
        self.assertEqual([r["outcome"] for r in self.log()], [L.ALIVE])

    def test_absent_relaunches_through_the_configured_command_and_confirms(self):
        fake = FakeRecorder(come_up_after=3)
        self.assertEqual(self.check(fake), runner.EXIT_OK, self.out)
        self.assertEqual(fake.launched, [(subprocess.list2cmdline(self.cfg["liveness"]["command"]), str(self.root))])
        self.assertEqual((fake.reaped, fake.released), ([], [99]))
        self.assertEqual(fake.now, 3 * self.cfg["liveness"]["confirm_poll_s"])
        entry, = self.log()
        self.assertEqual((entry["outcome"], entry["pid"]), (L.RELAUNCHED, 4242))
        self.assertEqual(self.alerts(), {})

    def test_a_launch_that_never_takes_the_mutex_is_reaped_and_alerts(self):
        fake = FakeRecorder(come_up_after=None)
        self.assertEqual(self.check(fake), runner.EXIT_FAILED)
        self.assertEqual((fake.reaped, fake.released), ([99], [99]))
        self.assertGreaterEqual(fake.now, self.cfg["liveness"]["confirm_timeout_s"])
        alert = self.alerts()[f"{L.KIND_DOWN}:{L.JOB}"]
        self.assertIn("never took", alert["message"])
        self.assertEqual(self.log()[-1]["outcome"], L.FAILED)

    def test_a_refused_launch_alerts(self):
        fake = FakeRecorder(launch_error=L.LaunchError("Win32_Process::Create refused the launch (ReturnValue=9)"))
        self.assertEqual(self.check(fake), runner.EXIT_FAILED)
        self.assertIn("ReturnValue=9", self.alerts()[f"{L.KIND_DOWN}:{L.JOB}"]["message"])

    def test_a_missing_executable_alerts_without_launching(self):
        self.exe.unlink()
        fake = FakeRecorder()
        self.assertEqual(self.check(fake), runner.EXIT_FAILED)
        self.assertEqual(fake.launched, [])
        self.assertIn("does not exist", self.alerts()[f"{L.KIND_DOWN}:{L.JOB}"]["message"])

    def test_recovery_closes_the_down_alert(self):
        self.check(FakeRecorder(come_up_after=None))
        self.assertIn(f"{L.KIND_DOWN}:{L.JOB}", self.alerts())
        self.assertEqual(self.check(FakeRecorder(held=True)), runner.EXIT_OK)
        self.assertEqual(self.alerts(), {})

    def test_setup_alert_closes_once_configured_and_passing(self):
        command = self.cfg["liveness"]["command"]
        self.cfg["liveness"]["command"] = None
        self.check(FakeRecorder())
        self.assertIn(f"{runner.KIND_SETUP}:{L.JOB}", self.alerts())
        self.check(FakeRecorder(held=True))                     # up, but still unconfigured: it stays
        self.assertIn(f"{runner.KIND_SETUP}:{L.JOB}", self.alerts())
        self.cfg["liveness"]["command"] = command
        self.assertEqual(self.check(FakeRecorder(held=True)), runner.EXIT_OK)
        self.assertEqual(self.alerts(), {})

    def test_an_unexpected_probe_failure_counts_as_down(self):
        fake = FakeRecorder(probe_error=OSError(87, "bad parameter"))
        self.assertEqual(self.check(fake), runner.EXIT_OK)
        self.assertEqual(len(fake.launched), 1)
        self.assertTrue(any("treating the recorder as down" in line for line in self.out))

    def test_unconfigured_raises_a_setup_alert_and_launches_nothing(self):
        self.cfg["liveness"]["command"] = None
        fake = FakeRecorder()
        self.assertEqual(self.check(fake), runner.EXIT_REFUSED)
        self.assertEqual(fake.launched, [])
        self.assertIn("liveness.command", self.alerts()[f"{runner.KIND_SETUP}:{L.JOB}"]["fix"])

    def test_dry_run_launches_alerts_and_logs_nothing(self):
        for held in (False, True):
            fake = FakeRecorder(held=held)
            with self.subTest(held=held):
                self.assertEqual(self.check(fake, dry_run=True), runner.EXIT_OK)
                self.assertEqual(fake.launched, [])
        self.assertIn("would launch", "\n".join(self.out))
        self.assertFalse(runner.files(self.cfg, "log").exists())
        self.assertFalse(Path(self.cfg["paths"]["data_dir"], self.cfg["kg"]["database"]).exists())

    def test_cli_uses_the_real_recorder_factory(self):
        fake = FakeRecorder(held=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(cli, "load_config", return_value=self.cfg), \
                mock.patch.object(L, "WinRecorder", return_value=fake):
            self.assertEqual(cli.main(["liveness", "--dry-run"]), runner.EXIT_OK)
        self.assertIn("the recorder is running", buf.getvalue())


class FakeRun:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout, self.returncode = stdout, returncode
        self.calls: list[tuple[str, str]] = []

    def __call__(self, script: str, payload: str):
        self.calls.append((script, payload))
        return subprocess.CompletedProcess([], self.returncode, self.stdout, "boom" if self.returncode else "")


@mock.patch.object(L.WinRecorder, "__init__", lambda self, cfg, runner=None: setattr(self, "run", runner))
class TestLaunchScript(LivenessCase):
    """WinRecorder.launch's request and result handling, through a fake PowerShell runner:
    never a real launch."""

    def recorder(self, run: FakeRun) -> L.WinRecorder:
        return L.WinRecorder(self.cfg, run)

    def test_request_goes_over_stdin_and_the_script_has_no_double_quotes(self):
        run = FakeRun('{"return_value": 0, "pid": 77}')
        self.assertEqual(self.recorder(run).launch('"C:\\a b\\pythonw.exe" -m whispr', "C:\\a b"), 77)
        script, payload = run.calls[0]
        self.assertEqual(script, L.LAUNCH_SCRIPT)
        self.assertNotIn('"', script)
        self.assertIn("Win32_Process", script)
        self.assertEqual(json.loads(payload)["working_dir"], "C:\\a b")

    def test_failures_are_launch_errors(self):
        for run, words in ((FakeRun('{"return_value": 9, "pid": 0}'), "ReturnValue=9"),
                           (FakeRun("", returncode=1), "WMI layer"),
                           (FakeRun("not json"), "no result")):
            with self.subTest(words), self.assertRaises(L.LaunchError) as caught:
                self.recorder(run).launch("x", "y")
            self.assertIn(words, str(caught.exception))


class TestScheduled(LivenessCase):
    def test_the_liveness_task_now_runs_a_defined_command(self):
        self.assertNotIn("whispr-liveness", S._unrunnable(self.cfg))
