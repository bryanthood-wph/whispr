"""python -m pipeline liveness: is the recorder running, and relaunch it if not
(docs/plan/D-architecture-and-ops.md D.6; lesson L14, the 2026-08-23 outage). The
port of scripts/watch-recorder.ps1, which the `whispr-liveness` task replaces.

- **Detection is the recorder's own mutex** (liveness.mutex, whispr/__main__.py's
  single-instance mutex), never a process scan: `pythonw -m whispr` is two processes
  (the venv stub and the app), so a stub left by a half-dead app would read as alive.
  The mutex is exactly what the recorder itself uses to decide whether it is running.
  An unexpected probe failure counts as **down**, on purpose: a false "down" costs one
  launch the recorder's mutex ends in seconds; a false "alive" makes this a silent no-op.
- **A held mutex never launches anything.**
- **Launch is Win32_Process::Create** (through PowerShell's Invoke-CimMethod, the
  schedules.powershell runner, a fixed script with the request on stdin), never a
  child process: the WMI host parents the recorder outside the scheduled task's job
  object, so the scheduler killing this check cannot take the recorder down with it.
  The command and working directory come from config (liveness.command, .working_dir).
- **A launch is confirmed, not assumed:** the new process is bound by handle at once
  (a PID can be reused while we wait), then the mutex is polled every
  liveness.confirm_poll_s for liveness.confirm_timeout_s. A launch that never takes it
  is reaped through that handle, so a recorder hanging before its mutex can't pile up
  one process per check.
- **Outcomes.** Alive: exit 0, logged, and an open recorder-down alert is acknowledged
  (the recorder is back), as is setup:liveness once command and working dir are set. Relaunched: exit 0, logged (a successful self-heal is the
  design working; how often it happens is in the log). Failed: one `recorder-down:liveness`
  alert, exit 1; the next check, 15 minutes on, is the retry. Command or working
  directory unset: one `setup:liveness` alert, exit 4. `--dry-run` reports what it
  would do and launches, alerts and logs nothing.

It makes no model call and keeps no run rows (it checks every 15 minutes); its record is
the `liveness` lines in pipeline.files.log, which doctor reads. The database is opened
only to raise or close an alert, and a database failure never stops the check.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

from kg import db
from kg.state import State
from pipeline import run as pipeline_run
from pipeline import schedule as S
from pipeline.run import EXIT_FAILED, EXIT_OK, EXIT_REFUSED

JOB = "liveness"
COMMAND = "liveness"
KIND_DOWN = "recorder-down"
ALIVE, RELAUNCHED, FAILED, DRY_RUN, UNCONFIGURED = "alive", "relaunched", "failed", "dry-run", "unconfigured"

# Win32_Process::Create: the request ({command_line, working_dir}) on stdin; prints
# {return_value, pid}. A fixed script with no double quotes (schedule.py's rule: values
# never reach a command line). A CIM-layer failure (WMI stopped, RPC unavailable, an
# EDR block) throws, and the runner reports the non-zero exit.
LAUNCH_SCRIPT = """
$ErrorActionPreference = 'Stop'
$req = [Console]::In.ReadToEnd() | ConvertFrom-Json
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $req.command_line; CurrentDirectory = $req.working_dir }
@{ return_value = [int]$r.ReturnValue; pid = [int]$r.ProcessId } | ConvertTo-Json -Compress
""".strip()

DOWN_KEY = pipeline_run.alert_key(KIND_DOWN, JOB)
SETUP_KEY = pipeline_run.alert_key(pipeline_run.KIND_SETUP, JOB)

SETUP_FIX = ("Set liveness.command (the recorder's argv, e.g. its venv pythonw.exe, -m, whispr) and "
             "liveness.working_dir in the config overlay; /whispr-setup sets both.")


class LaunchError(RuntimeError):
    """The recorder could not be launched."""


class Recorder(Protocol):
    """The machine, as liveness sees it. WinRecorder is the real one; tests pass a fake."""

    def held(self, mutex: str) -> bool: ...                          # OSError: the probe itself failed
    def launch(self, command_line: str, working_dir: str) -> int: ...  # the new PID; LaunchError
    def bind(self, pid: int) -> Optional[int]: ...                   # a process handle, or None (gone)
    def reap(self, handle: int) -> bool: ...                          # True if it was still running
    def release(self, handle: int) -> None: ...
    def sleep(self, seconds: float) -> None: ...
    def clock(self) -> float: ...


# Win32 access rights and error codes (winnt.h, winerror.h).
_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259


class WinRecorder:
    """The real machine: kernel32 for the mutex and the process handle, PowerShell's
    Invoke-CimMethod for the launch."""

    def __init__(self, cfg: dict, runner: Optional[S.Runner] = None):
        self.run = runner or S.powershell_runner(cfg)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenMutexW.restype = ctypes.c_void_p
        k.OpenMutexW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32)
        k.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        k.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        k.CloseHandle.argtypes = (ctypes.c_void_p,)
        self.k = k

    def held(self, mutex: str) -> bool:
        handle = self.k.OpenMutexW(_SYNCHRONIZE, False, mutex)
        if handle:
            self.k.CloseHandle(handle)
            return True
        err = ctypes.get_last_error()
        if err == _ERROR_ACCESS_DENIED:      # it exists; another session or integrity level owns it
            return True
        if err == _ERROR_FILE_NOT_FOUND:
            return False
        raise OSError(err, f"OpenMutexW({mutex!r}) failed: {ctypes.FormatError(err)}")

    def launch(self, command_line: str, working_dir: str) -> int:
        try:
            out = S._call(self.run, LAUNCH_SCRIPT, {"command_line": command_line, "working_dir": working_dir})
        except S.ScheduleError as exc:
            raise LaunchError(f"Win32_Process::Create threw ({exc}): the WMI layer may be unavailable") from exc
        try:
            got = json.loads(out)
        except ValueError as exc:
            raise LaunchError(f"Win32_Process::Create returned no result: {out.strip()[:200]!r}") from exc
        if got.get("return_value") != 0:
            raise LaunchError(f"Win32_Process::Create refused the launch (ReturnValue={got.get('return_value')})")
        return int(got["pid"])

    def bind(self, pid: int) -> Optional[int]:
        return self.k.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                                  False, pid) or None

    def reap(self, handle: int) -> bool:
        code = ctypes.c_uint32()
        if self.k.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
            return False
        return bool(self.k.TerminateProcess(handle, 1))

    def release(self, handle: int) -> None:
        self.k.CloseHandle(handle)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def clock(self) -> float:
        return time.monotonic()


def command_line(cfg: dict) -> Optional[str]:
    """The recorder's command line (Windows quoting), or None while unconfigured."""
    argv = cfg["liveness"]["command"]
    return subprocess.list2cmdline(argv) if argv and cfg["liveness"]["working_dir"] else None


def _alerting(cfg: dict, act: Callable[[State], None], log: Callable[[dict], None]) -> None:
    """Raise or close an alert; a database failure is logged, never raised: the check
    itself must still report and exit."""
    try:
        with contextlib.closing(db.connect(cfg)) as conn:
            act(State(conn, cfg))
    except Exception as exc:
        log({"event": "alert-failed", "error": pipeline_run._error(exc)})


def _passed(cfg: dict, now: datetime, log: Callable[[dict], None]) -> None:
    """The recorder is up: close recorder-down, and setup:liveness once it is configured."""
    keys = [DOWN_KEY] + ([SETUP_KEY] if command_line(cfg) is not None else [])
    _alerting(cfg, lambda state: pipeline_run.clear_alerts(state, keys, now=now), log)


def _probe(recorder: Recorder, mutex: str, warnings: list[str]) -> bool:
    try:
        return recorder.held(mutex)
    except OSError as exc:
        warnings.append(f"mutex probe failed unexpectedly ({exc}); treating the recorder as down")
        return False


def _relaunch(cfg: dict, recorder: Recorder, line: str, warnings: list[str]) -> tuple[bool, str, Optional[int]]:
    """(revived, detail, pid)."""
    lv = cfg["liveness"]
    exe = Path(lv["command"][0])
    if not exe.is_file():
        return False, f"the recorder executable {exe} does not exist: cannot relaunch", None
    try:
        pid = recorder.launch(line, lv["working_dir"])
    except LaunchError as exc:
        return False, str(exc), None
    handle = recorder.bind(pid)                 # now, while the PID is unambiguously ours
    try:
        deadline = recorder.clock() + lv["confirm_timeout_s"]
        while recorder.clock() < deadline:
            recorder.sleep(lv["confirm_poll_s"])
            if _probe(recorder, lv["mutex"], warnings):
                return True, f"relaunched the recorder (PID {pid})", pid
        detail = f"launched PID {pid} but it never took {lv['mutex']} within {lv['confirm_timeout_s']} s"
        if handle is None:
            return False, detail + "; it exited before it could be bound", pid
        reaped = recorder.reap(handle)          # bounded retries: no process piles up per check
        return False, detail + ("; reaped it" if reaped else "; it had already exited"), pid
    finally:
        if handle is not None:
            recorder.release(handle)


def liveness(cfg: dict, *, dry_run: bool = False, recorder: Optional[Recorder] = None,
             now: Optional[datetime] = None, out: Callable[[str], None] = print) -> int:
    """One liveness check; returns its exit code. `recorder` and `now` are for tests."""
    lv = cfg["liveness"]
    now = now or datetime.now(timezone.utc)
    recorder = recorder or WinRecorder(cfg)
    log = (lambda record: None) if dry_run else pipeline_run.job_log(cfg, JOB)
    warnings: list[str] = []
    alive = _probe(recorder, lv["mutex"], warnings)
    for warning in warnings:
        out(f"WARN: {warning}")
    if alive:
        log({"event": "check", "outcome": ALIVE, "exit": EXIT_OK, "warnings": warnings})
        out(f"ok: the recorder is running (holds {lv['mutex']})")
        if not dry_run:
            _passed(cfg, now, log)
        return EXIT_OK
    line = command_line(cfg)
    if line is None:
        message = (f"the recorder is not running (no holder of {lv['mutex']}), and liveness.command or "
                   "liveness.working_dir is not set, so it cannot be relaunched")
        out(("dry run: " if dry_run else "") + message)
        if not dry_run:
            _alerting(cfg, lambda state: state.raise_alert(pipeline_run.KIND_SETUP, SETUP_KEY, message, SETUP_FIX,
                                                           now=now), log)
            log({"event": "check", "outcome": UNCONFIGURED, "exit": EXIT_REFUSED, "warnings": warnings})
        return EXIT_REFUSED
    if dry_run:
        out(f"dry run: the recorder is not running; would launch {line} (working dir {lv['working_dir']}) "
            "through Win32_Process::Create. Nothing launched.")
        return EXIT_OK
    out(f"the recorder is not running (no holder of {lv['mutex']}); launching {line}")
    revived, detail, pid = _relaunch(cfg, recorder, line, warnings)
    outcome, code = (RELAUNCHED, EXIT_OK) if revived else (FAILED, EXIT_FAILED)
    log({"event": "check", "outcome": outcome, "exit": code, "pid": pid, "detail": detail, "warnings": warnings})
    out(detail)
    if not revived:
        message = f"The recorder is down and the restart failed: {detail}."
        fix = ("Start it by hand (" + line + ") and check its log (paths.recorder_log) for why it will not come up; "
               "the next liveness check, at the next repetition, retries.")
        _alerting(cfg, lambda state: state.raise_alert(KIND_DOWN, DOWN_KEY, message, fix, now=now), log)
    else:
        _passed(cfg, now, log)
    return code
