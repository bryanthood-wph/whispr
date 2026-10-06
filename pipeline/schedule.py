"""Windows scheduled tasks generated from config (docs/plan/D-architecture-and-ops.md D.2,
D.6, D.7; lessons L6, L14, L16).

`schedules.tasks` in config is the only description of whispr's scheduled jobs. This
module turns it into task definitions and compares, updates or creates the live tasks:

  desired(cfg)        the task definitions config asks for
  check(cfg, run)     read-only. A Finding for each task that is missing, differs from
                      config (action, triggers, settings, principal), has no NextRunTime
                      or runs a `-m pipeline` subcommand that __main__.py does not define
  apply(cfg, run)     Set-ScheduledTask on tasks that exist and drift, then re-check. It
                      never registers: a missing task stays a finding (D.2)
  register(cfg, run)  /whispr-setup only. Register-ScheduledTask for each missing task
                      (without -Force, so an existing task is never overwritten), then
                      reads every task back and raises RegistrationFailed unless all of
                      them match and are armed
  apply and register pass their planned writes to `confirm` before writing any (the CLI
  prints them and writes only with --yes), and never write a task whose subcommand does
  not exist: armed, it would fail every run with nothing reporting it.

Design
- One normalized form. desired() builds it from config and _normalize_xml() parses it
  from Export-ScheduledTask, so drift is plain equality per part and a finding names
  exactly what differs. Task Scheduler omits an element that sits at its schema default;
  _XML_DEFAULTS fills those in (facts of the task schema, not tunables).
- The registration call is never trusted alone (register-task.ps1's lesson). A
  repetition grafted onto a logon trigger registered cleanly, read back "correct" and
  fired nothing (2026-08-27). Only NextRunTime, the scheduler's own answer to "when does
  this run", proves a task is armed, so check() reports an empty one and register()
  fails on it. Every task therefore needs a time-based trigger (daily_at or every_min).
- Repetition is its own trigger (L16): a one-time trigger at midnight today (already
  past, so armed at once) repeating forever. It is built the way register-task.ps1
  verified, because [TimeSpan]::MaxValue is rejected (2026-08-26).
- Start times are local, with no UTC offset. New-ScheduledTaskTrigger writes one (UTC,
  'Z'), which Task Scheduler reads as "synchronize across time zones": the local fire
  time then moves an hour when DST starts or ends. A live offset is drift.
- Task names compare case-insensitively, as Task Scheduler resolves them.
- The interpreter path carries no version stamp (L16, D.7). An MSIX package folder is
  deleted by the next update, and every task then fails "file not found" with nothing
  reporting it. A venv's base interpreter (pyvenv.cfg `home`) is checked too: the venv's
  launcher stub has a stable path, but it starts the base interpreter.
- Every PowerShell call goes through `run(script, payload)`, a Runner. The scripts are
  fixed text in this module. The payload is JSON on stdin, ASCII-only, so no config value
  is ever spliced into a command line, and tests pass a fake runner. The default runner
  passes the script with -Command rather than -EncodedCommand (a base64 command line is
  what endpoint monitoring flags). The scripts hold no double quote, which the Windows
  command-line parser would strip, and they escape their output to ASCII, so the result
  does not depend on the console code page.
"""

from __future__ import annotations

import json
import ntpath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from pipeline.config import ConfigError

# The folder `-m pipeline` resolves from: every task's working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent

# run(script, payload_json) -> CompletedProcess with text stdout/stderr.
Runner = Callable[[str, str], "subprocess.CompletedProcess[str]"]

# Finding kinds.
MISSING = "missing"
ACTION = "action"
TRIGGERS = "triggers"
SETTINGS = "settings"
PRINCIPAL = "principal"
NOT_ARMED = "not-armed"
NO_COMMAND = "no-command"

# Get-ScheduledTask -TaskName takes wildcards; a name of these characters only matches itself.
_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_ISO_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
# StartBoundary: HH:MM, then any seconds, then the UTC offset if there is one.
_BOUNDARY_TIME = re.compile(r"T(\d\d:\d\d)(?::\d\d(?:\.\d+)?)?(Z|[+-]\d\d:\d\d)?$")

# The package `-m <package> <subcommand>` names in a task's args.
_PACKAGE = __name__.rpartition(".")[0]
# A UserId that is the account running the command: WRITE_SCRIPT writes $env:USERNAME, and
# Task Scheduler stores it as DOMAIN\user or as the SID.
ME = "(current user)"
# The repeating trigger's start time of day: WRITE_SCRIPT anchors it at (Get-Date).Date.
_ONCE_AT = "00:00"

# Config (cmdlet) names <-> task XML names.
_LOGON_TYPES = {"Interactive": "InteractiveToken", "S4U": "S4U"}
_RUN_LEVELS = {"Limited": "LeastPrivilege", "Highest": "HighestAvailable"}

# Task Scheduler schema defaults for elements Export-ScheduledTask may omit.
_XML_DEFAULTS = {
    "Enabled": "true",
    "ExecutionTimeLimit": "PT72H",
    "DisallowStartIfOnBatteries": "true",
    "StopIfGoingOnBatteries": "true",
    "StartWhenAvailable": "false",
    "WakeToRun": "false",
    "MultipleInstancesPolicy": "IgnoreNew",
    "RunLevel": "LeastPrivilege",
    "RunOnlyIfIdle": "false",
    "RunOnlyIfNetworkAvailable": "false",
    "AllowHardTerminate": "true",
}
# New-ScheduledTaskSettingsSet defaults for the settings config does not set.
_CMDLET_SETTINGS = {"run_only_if_idle": False, "run_only_if_network": False, "allow_hard_terminate": True}


class ScheduleError(RuntimeError):
    """PowerShell failed, its output was unreadable, or the interpreter is unusable."""


@dataclass(frozen=True)
class Finding:
    task: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.task}: {self.kind}: {self.detail}"


@dataclass
class Outcome:
    changed: list[str] = field(default_factory=list)      # tasks written (Set- or Register-)
    findings: list[Finding] = field(default_factory=list)  # still wrong after the read-back
    declined: bool = False                                 # confirm() refused: nothing written


def _lines(findings: list[Finding]) -> str:
    return "\n  ".join(str(f) for f in findings)


class RegistrationFailed(ScheduleError):
    def __init__(self, outcome: Outcome):
        self.outcome = outcome
        super().__init__(f"read-back after registering {outcome.changed or 'nothing'} found:\n  {_lines(outcome.findings)}")


class WriteFailed(ScheduleError):
    """A write failed after earlier ones in the same run succeeded. `outcome` holds the
    tasks already written and the findings of reading them back."""

    def __init__(self, failed: str, error: ScheduleError, outcome: Outcome, readback: str):
        self.outcome = outcome
        super().__init__(f"writing {failed} failed: {error}\n"
                         f"already written: {outcome.changed}; read-back of those: {readback}")


# --- desired state ---------------------------------------------------------------------

def desired(cfg: dict, executable: str = sys.executable) -> list[dict]:
    """The normalized task definitions config asks for, in config order."""
    interpreter = resolve_interpreter(cfg, executable)
    seen: dict[str, str] = {}
    for name in cfg["schedules"]["tasks"]:
        if name.casefold() in seen:
            raise ConfigError(f"schedules.tasks: {seen[name.casefold()]!r} and {name!r} are the same "
                              "task: Task Scheduler names ignore case")
        seen[name.casefold()] = name
    return [_task(cfg, name, job, interpreter) for name, job in cfg["schedules"]["tasks"].items()]


def resolve_interpreter(cfg: dict, executable: str = sys.executable) -> Path:
    """schedules.interpreter, or interpreter_name beside `executable`. Refused when it is
    missing or it, or a venv's base interpreter, matches a version_stamp_pattern."""
    sc = cfg["schedules"]
    path = Path(sc["interpreter"]) if sc["interpreter"] else Path(executable).with_name(sc["interpreter_name"])
    if not path.is_absolute():
        raise ScheduleError(f"interpreter {path} is not an absolute path")
    if not path.is_file():
        raise ScheduleError(f"interpreter {path} does not exist")
    for candidate in (str(path), *_venv_home(path)):
        if _version_stamped(cfg, candidate):
            raise ScheduleError(
                f"interpreter {path} resolves through the version-stamped path {candidate}: the next "
                "update deletes it and every task stops starting. Use an install with a stable path "
                "(python.org installer, or set schedules.interpreter).")
    return path


def _venv_home(interpreter: Path) -> list[str]:
    """The base interpreter folder of the venv `interpreter` belongs to, if any."""
    for folder in (interpreter.parent, interpreter.parent.parent):
        cfg_file = folder / "pyvenv.cfg"
        if cfg_file.is_file():
            for line in cfg_file.read_text(encoding="utf-8", errors="replace").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip().lower() == "home":
                    return [value.strip()]
    return []


def _version_stamped(cfg: dict, path: str) -> bool:
    return any(re.search(p, path) for p in cfg["schedules"]["version_stamp_patterns"])


def _task(cfg: dict, name: str, job: dict, interpreter: Path) -> dict:
    sc = cfg["schedules"]
    _validate_job(sc, name, job)
    return {
        "name": name,
        "actions": [{
            "command": str(interpreter),
            "arguments": subprocess.list2cmdline([*sc["interpreter_args"], *job["args"]]),
            "working_directory": str(REPO_ROOT),
        }],
        "triggers": _triggers(job),
        "settings": {
            "enabled": True,
            "time_limit_min": job["time_limit_min"],
            "ac_power_only": job["ac_power_only"],
            "stop_on_battery": job["stop_on_battery"],
            "start_when_available": job["start_when_available"],
            "wake_to_run": job["wake_to_run"],
            "restart_count": job["restart_count"],
            "restart_interval_min": job["restart_interval_min"],
            "multiple_instances": job["multiple_instances"],
            **_CMDLET_SETTINGS,
        },
        "principal": {**sc["principal"], "user": ME},
    }


def _triggers(job: dict) -> list[dict]:
    """A repeating "once" trigger's start date is not in the normalized form, only its time
    of day: it is anchored at midnight on the day it is written, which drifts by design.
    No trigger has an end, a delay or a UTC offset (see Design)."""
    common = {"repeat": None, "enabled": True, "utc_offset": None, "end": None,
              "delay_min": None, "random_delay_min": None}
    out = []
    if job["daily_at"] is not None:   # every day: days_interval 1
        out.append({**common, "kind": "daily", "at": job["daily_at"], "days_interval": 1})
    if job["at_logon"]:
        out.append({**common, "kind": "logon", "user": ME})
    if job["every_min"] is not None:
        out.append({**common, "kind": "once", "at": _ONCE_AT,
                    "repeat": {"every_min": job["every_min"], "duration_min": None}})
    return out


def pipeline_commands() -> frozenset[str]:
    """The subcommands `python -m pipeline` defines, read from main()'s own parser."""
    from pipeline.__main__ import build_parser   # deferred: __main__ imports this module
    return build_parser()[1]


def _pipeline_command(argv: list[str]) -> Optional[str]:
    """The subcommand an interpreter argv runs through `-m pipeline` ("" for none), or None
    when it runs something else."""
    for i, arg in enumerate(argv[:-1]):
        if arg == "-m" and argv[i + 1] == _PACKAGE:
            return argv[i + 2] if i + 2 < len(argv) else ""
    return None


def _unrunnable(cfg: dict) -> dict[str, Finding]:
    """name -> a NO_COMMAND Finding for each job whose subcommand __main__.py does not
    define. Such a job is never written: armed, it would fail every run unseen."""
    sc = cfg["schedules"]
    known = pipeline_commands()
    out = {}
    for name, job in sc["tasks"].items():
        command = _pipeline_command([*sc["interpreter_args"], *job["args"]])
        if command is not None and command not in known:
            out[name] = Finding(name, NO_COMMAND,
                                f"runs `-m {_PACKAGE} {command}`, which is not a subcommand of "
                                f"`python -m {_PACKAGE}` (it has {sorted(known)}); not written until it is")
    return out


def _validate_job(sc: dict, name: str, job: dict) -> None:
    where = f"schedules.tasks.{name}"
    if not _TASK_NAME.fullmatch(name):
        raise ConfigError(f"{where}: a task name is letters, digits, '.', '_' and '-' only")
    if name.casefold() in {r.casefold() for r in sc["reserved_names"]}:
        raise ConfigError(f"{where}: {name!r} is a hand-registered live task (schedules.reserved_names); "
                          "Task Scheduler names ignore case")
    if job["daily_at"] is not None and not _HHMM.fullmatch(job["daily_at"]):
        raise ConfigError(f"{where}.daily_at: {job['daily_at']!r} is not HH:MM")
    if job["daily_at"] is None and job["every_min"] is None:
        raise ConfigError(f"{where}: needs daily_at or every_min, so NextRunTime can prove it is armed")
    if (job["restart_count"] > 0) != (job["restart_interval_min"] is not None):
        raise ConfigError(f"{where}: restart_interval_min is set exactly when restart_count > 0")


# --- reading live tasks ----------------------------------------------------------------

READ_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$req = [Console]::In.ReadToEnd() | ConvertFrom-Json
$rows = @(foreach ($name in $req.names) {
  $task = Get-ScheduledTask -TaskPath $req.task_path -TaskName $name -ErrorAction SilentlyContinue
  if (-not $task) {
    [pscustomobject]@{ name = $name; exists = $false; xml = $null; next_run = $null }
    continue
  }
  $info = Get-ScheduledTaskInfo -TaskPath $req.task_path -TaskName $name
  $next = $null
  if ($info.NextRunTime) { $next = $info.NextRunTime.ToString('o') }
  $xml = Export-ScheduledTask -TaskPath $req.task_path -TaskName $name
  [pscustomobject]@{ name = $name; exists = $true; xml = $xml; next_run = $next }
})
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$out = [pscustomobject]@{ me = @($me.Name, $me.User.Value); tasks = $rows }
$json = ConvertTo-Json -InputObject $out -Depth 4 -Compress
[regex]::Replace($json, '[^\x00-\x7f]', { param($m) '\u' + ([int][char]$m.Value).ToString('x4') })
"""


def read_live(cfg: dict, run: Runner, names: list[str]) -> dict[str, dict]:
    """name -> {"exists", "task" (normalized, or None), "next_run" (ISO text, or None)}."""
    out = _call(run, READ_SCRIPT, {"task_path": cfg["schedules"]["task_path"], "names": names})
    try:
        reply = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ScheduleError(f"unreadable task read-back: {exc}: {out[:200]!r}") from exc
    try:
        me, rows = [str(m) for m in reply["me"]], reply["tasks"]
    except (KeyError, TypeError) as exc:
        raise ScheduleError(f"malformed task read-back: {out[:200]!r}") from exc
    if isinstance(rows, dict):   # ConvertTo-Json can unwrap a one-row array
        rows = [rows]
    live = {}
    for row in rows:
        try:
            name, exists, xml, next_run = row["name"], bool(row["exists"]), row["xml"], row["next_run"]
        except (KeyError, TypeError) as exc:
            raise ScheduleError(f"malformed task read-back row: {row!r}") from exc
        task = _normalize_xml(name, xml or "", me) if exists else None
        live[name] = {"exists": exists, "task": task, "next_run": next_run}
    absent = [n for n in names if n not in live]
    if absent:
        raise ScheduleError(f"task read-back is missing {absent}")
    return live


def _normalize_xml(name: str, xml_text: str, me: list[str]) -> dict:
    """Export-ScheduledTask XML -> the normalized form desired() builds. `me` is the
    current account's names (DOMAIN\\user, SID): a UserId naming it normalizes to ME. The
    XML is the local Task Scheduler's own output; the stdlib parser resolves no external
    entities."""
    try:
        root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text))
    except ET.ParseError as exc:
        raise ScheduleError(f"{name}: unreadable task XML: {exc}") from exc
    for el in root.iter():           # the task namespace adds nothing to compare
        el.tag = el.tag.rpartition("}")[2]
    settings = root.find("Settings")
    if settings is None:
        settings = ET.Element("Settings")
    return {
        "name": name,
        "actions": [_action(el) for el in root.findall("Actions/*")],
        "triggers": [_trigger(el, me) for el in root.findall("Triggers/*")],
        "settings": _settings(settings),
        "principal": _principal(root.find("Principals/Principal"), me),
    }


def _user(user_id: Optional[str], me: list[str]) -> Optional[str]:
    """ME when `user_id` is the current account (as DOMAIN\\user, user or SID), else as is."""
    if user_id is None:
        return None
    mine = {m.casefold() for m in me} | {m.rpartition("\\")[2].casefold() for m in me}
    return ME if user_id.casefold() in mine else user_id


def _text(el: Optional[ET.Element], tag: str, default: Optional[str] = None) -> Optional[str]:
    """A child's text; if absent, `default`, else the schema default for `tag`, else None."""
    child = None if el is None else el.find(tag)
    if child is None or child.text is None:
        return default if default is not None else _XML_DEFAULTS.get(tag)
    return child.text.strip()


def _flag(el: ET.Element, tag: str) -> bool:
    return _text(el, tag) == "true"


def _minutes(iso: Optional[str]) -> Optional[float]:
    if iso is None:
        return None
    m = _ISO_DURATION.fullmatch(iso)
    if not m:
        raise ScheduleError(f"unreadable duration {iso!r}")
    d, h, mins, s = (int(g or 0) for g in m.groups())
    total = d * 1440 + h * 60 + mins + s / 60
    return int(total) if total == int(total) else total


def _action(el: ET.Element) -> dict:
    if el.tag != "Exec":
        return {"kind": el.tag}
    return {"command": (_text(el, "Command", "") or "").strip('"'),
            "arguments": _text(el, "Arguments", ""),
            "working_directory": (_text(el, "WorkingDirectory", "") or "").strip('"')}


def _trigger(el: ET.Element, me: list[str]) -> dict:
    rep = el.find("Repetition")
    repeat = None if rep is None else {"every_min": _minutes(_text(rep, "Interval", "")),
                                       "duration_min": _minutes(_text(rep, "Duration"))}
    start = _BOUNDARY_TIME.search(_text(el, "StartBoundary", "") or "")
    at = start.group(1) if start else None
    out: dict[str, Any] = {"kind": el.tag}
    if el.tag == "CalendarTrigger" and el.find("ScheduleByDay") is not None:
        out = {"kind": "daily", "at": at,
               "days_interval": int(_text(el.find("ScheduleByDay"), "DaysInterval", "1") or 1)}
    elif el.tag == "CalendarTrigger":
        schedule = next((c.tag for c in el if c.tag.startswith("Schedule")), "")
        out = {"kind": f"calendar:{schedule}"}
    elif el.tag == "LogonTrigger":   # no UserId: it fires at any user's logon
        out = {"kind": "logon", "user": _user(_text(el, "UserId"), me)}
    elif el.tag == "TimeTrigger":
        out = {"kind": "once", "at": at}
    out["repeat"] = repeat
    out["enabled"] = _flag(el, "Enabled")
    out["utc_offset"] = start.group(2) if start else None
    out["end"] = _text(el, "EndBoundary")   # a past EndBoundary: the trigger has expired
    out["delay_min"] = _minutes(_text(el, "Delay")) or None   # PT0S is no delay
    out["random_delay_min"] = _minutes(_text(el, "RandomDelay")) or None
    return out


def _settings(el: ET.Element) -> dict:
    restart = el.find("RestartOnFailure")
    return {
        "enabled": _flag(el, "Enabled"),
        "time_limit_min": _minutes(_text(el, "ExecutionTimeLimit")),
        "ac_power_only": _flag(el, "DisallowStartIfOnBatteries"),
        "stop_on_battery": _flag(el, "StopIfGoingOnBatteries"),
        "start_when_available": _flag(el, "StartWhenAvailable"),
        "wake_to_run": _flag(el, "WakeToRun"),
        "restart_count": int(_text(restart, "Count", "0") or 0) if restart is not None else 0,
        "restart_interval_min": _minutes(_text(restart, "Interval")) if restart is not None else None,
        "multiple_instances": _text(el, "MultipleInstancesPolicy"),
        "run_only_if_idle": _flag(el, "RunOnlyIfIdle"),
        "run_only_if_network": _flag(el, "RunOnlyIfNetworkAvailable"),
        "allow_hard_terminate": _flag(el, "AllowHardTerminate"),
    }


def _principal(el: Optional[ET.Element], me: list[str]) -> dict:
    logon = _text(el, "LogonType", "") if el is not None else ""
    level = _text(el, "RunLevel") if el is not None else _XML_DEFAULTS["RunLevel"]
    by_logon = {v: k for k, v in _LOGON_TYPES.items()}
    by_level = {v: k for k, v in _RUN_LEVELS.items()}
    return {"logon_type": by_logon.get(logon, logon), "run_level": by_level.get(level, level),
            "user": _user(_text(el, "UserId"), me)}


# --- comparing -------------------------------------------------------------------------

def compare(want: dict, live: dict) -> list[Finding]:
    """Findings for one task: `want` from desired(), `live` one row of read_live()."""
    name = want["name"]
    if not live["exists"]:
        return [Finding(name, MISSING, "not registered")]
    have = live["task"]
    out = []
    if _actions_key(want["actions"]) != _actions_key(have["actions"]):
        out.append(Finding(name, ACTION, f"live {_show_actions(have['actions'])}, want {_show_actions(want['actions'])}"))
    if _sorted(want["triggers"]) != _sorted(have["triggers"]):
        out.append(Finding(name, TRIGGERS, f"live {_sorted(have['triggers'])}, want {_sorted(want['triggers'])}"))
    for part, kind in (("settings", SETTINGS), ("principal", PRINCIPAL)):
        for key, value in want[part].items():
            if have[part].get(key) != value:
                out.append(Finding(name, kind, f"{key}: live {have[part].get(key)!r}, want {value!r}"))
    if not live["next_run"]:
        out.append(Finding(name, NOT_ARMED, "NextRunTime is empty: no trigger will fire"))
    return out


def _actions_key(actions: list[dict]) -> list[dict]:
    """Paths compare case-insensitively, as Windows resolves them."""
    return [{k: ntpath.normcase(v) if k in ("command", "working_directory") else v for k, v in a.items()}
            for a in actions]


def _show_actions(actions: list[dict]) -> str:
    return "; ".join(f"{a.get('command')} {a.get('arguments')} (in {a.get('working_directory')})"
                     if "command" in a else str(a) for a in actions) or "none"


def _sorted(triggers: list[dict]) -> list[str]:
    return sorted(json.dumps(t, sort_keys=True) for t in triggers)


# --- check / apply / register ----------------------------------------------------------

WRITE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$req = [Console]::In.ReadToEnd() | ConvertFrom-Json
$task = $req.task
$actions = @(foreach ($a in $task.actions) {
  New-ScheduledTaskAction -Execute $a.command -Argument $a.arguments -WorkingDirectory $a.working_directory
})
$triggers = @(foreach ($t in $task.triggers) {
  # The cmdlet writes StartBoundary in UTC, which Task Scheduler reads as synchronize
  # across time zones (the local time moves with DST). Rewritten as local time, no offset.
  if ($t.kind -eq 'daily') {
    $d = New-ScheduledTaskTrigger -Daily -At $t.at -DaysInterval $t.days_interval
    $d.StartBoundary = ([datetime]$d.StartBoundary).ToString('s')
    $d
  } elseif ($t.kind -eq 'logon') {
    New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  } elseif ($t.kind -eq 'once') {
    # Midnight today is already past, so the repetition is armed the moment it is written.
    # Built with a one-day duration and then cleared: an absent Duration repeats forever,
    # and [TimeSpan]::MaxValue is rejected (register-task.ps1, 2026-08-26).
    $r = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes $t.repeat.every_min) -RepetitionDuration (New-TimeSpan -Days 1)
    $r.StartBoundary = ([datetime]$r.StartBoundary).ToString('s')
    $r.Repetition.Duration = $null
    $r.Repetition.StopAtDurationEnd = $false
    $r
  } else {
    throw ('unknown trigger kind: ' + $t.kind)
  }
})
$s = $task.settings
$opts = @{
  ExecutionTimeLimit = (New-TimeSpan -Minutes $s.time_limit_min)
  MultipleInstances = $s.multiple_instances
  StartWhenAvailable = [bool]$s.start_when_available
  WakeToRun = [bool]$s.wake_to_run
  AllowStartIfOnBatteries = (-not $s.ac_power_only)
  DontStopIfGoingOnBatteries = (-not $s.stop_on_battery)
  Disable = (-not $s.enabled)
  RunOnlyIfIdle = [bool]$s.run_only_if_idle
  RunOnlyIfNetworkAvailable = [bool]$s.run_only_if_network
  DisallowHardTerminate = (-not $s.allow_hard_terminate)
}
if ($s.restart_count -gt 0) {
  $opts.RestartCount = $s.restart_count
  $opts.RestartInterval = (New-TimeSpan -Minutes $s.restart_interval_min)
}
$settings = New-ScheduledTaskSettingsSet @opts
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType $task.principal.logon_type -RunLevel $task.principal.run_level
$common = @{ TaskPath = $req.task_path; TaskName = $task.name; Action = $actions; Trigger = $triggers; Principal = $principal; Settings = $settings }
if ($req.mode -eq 'register') {
  Register-ScheduledTask @common | Out-Null
} elseif ($req.mode -eq 'set') {
  Set-ScheduledTask @common | Out-Null
} else {
  throw ('unknown mode: ' + $req.mode)
}
"""

_REGISTER, _SET = "register", "set"


# confirm(planned) -> True to write: `planned` is the findings of the tasks about to be
# written, before any of them is. The default writes; the CLI asks for --yes.
Confirm = Callable[[list[Finding]], bool]


def check(cfg: dict, run: Runner, executable: str = sys.executable) -> list[Finding]:
    """Drift between config and the live tasks. Reads only."""
    return _findings(cfg, run, desired(cfg, executable))


def apply(cfg: dict, run: Runner, executable: str = sys.executable,
          confirm: Optional[Confirm] = None) -> Outcome:
    """Set-ScheduledTask on each task that exists and drifts, then re-check every task.
    A missing task is left alone and stays a finding: registering is setup's job."""
    return _converge(cfg, run, executable, confirm, _SET)


def register(cfg: dict, run: Runner, executable: str = sys.executable,
             confirm: Optional[Confirm] = None) -> Outcome:
    """Register each missing task, then read every task back. Returns only when all of
    them match config and have a NextRunTime; otherwise raises RegistrationFailed."""
    outcome = _converge(cfg, run, executable, confirm, _REGISTER)
    if outcome.findings and not outcome.declined:
        raise RegistrationFailed(outcome)
    return outcome


def _converge(cfg: dict, run: Runner, executable: str, confirm: Optional[Confirm], mode: str) -> Outcome:
    """Write each task `mode` acts on (_SET: exists and drifts; _REGISTER: missing),
    except one whose subcommand does not exist, then re-check every task."""
    want = desired(cfg, executable)
    live = read_live(cfg, run, [t["name"] for t in want])
    unrunnable = _unrunnable(cfg)
    todo = [t for t in want if t["name"] not in unrunnable
            and (live[t["name"]]["exists"] == (mode == _SET)) and compare(t, live[t["name"]])]
    before = _compared(want, live, unrunnable)
    if not todo:
        return Outcome([], before)
    if confirm is not None and not confirm([f for f in before if f.task in {t["name"] for t in todo}]):
        return Outcome([], before, declined=True)
    return Outcome(_write_each(cfg, run, mode, todo, want), _findings(cfg, run, want))


def _write_each(cfg: dict, run: Runner, mode: str, todo: list[dict], want: list[dict]) -> list[str]:
    """Write `todo` in order. A failure after an earlier write succeeded raises
    WriteFailed, naming the tasks already written and what reading them back found."""
    written: list[str] = []
    for task in todo:
        try:
            _write(cfg, run, mode, task)
        except ScheduleError as exc:
            if not written:
                raise
            findings: list[Finding] = []
            try:
                findings = _findings(cfg, run, [t for t in want if t["name"] in written])
                readback = f"\n  {_lines(findings)}" if findings else "they match config and are armed"
            except ScheduleError as read_exc:
                readback = f"unreadable ({read_exc})"
            raise WriteFailed(task["name"], exc, Outcome(written, findings), readback) from exc
        written.append(task["name"])
    return written


def _findings(cfg: dict, run: Runner, want: list[dict]) -> list[Finding]:
    live = read_live(cfg, run, [t["name"] for t in want])
    return _compared(want, live, _unrunnable(cfg))


def _compared(want: list[dict], live: dict[str, dict], unrunnable: dict[str, Finding]) -> list[Finding]:
    """Each task's NO_COMMAND finding, if any, then compare()'s."""
    return [f for t in want for f in ([unrunnable[t["name"]]] if t["name"] in unrunnable else [])
            + compare(t, live[t["name"]])]


def _write(cfg: dict, run: Runner, mode: str, task: dict) -> None:
    _call(run, WRITE_SCRIPT, {"mode": mode, "task_path": cfg["schedules"]["task_path"], "task": task})


# --- PowerShell ------------------------------------------------------------------------

def _call(run: Runner, script: str, payload: dict) -> str:
    try:
        proc = run(script, json.dumps(payload))   # ensure_ascii: the payload is pure ASCII
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScheduleError(f"PowerShell did not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "no output"
        raise ScheduleError(f"PowerShell failed (exit {proc.returncode}): {detail}")
    return proc.stdout


def powershell_runner(cfg: dict) -> Runner:
    """The real Runner: schedules.powershell with the script as one -Command argument and
    the payload on stdin."""
    sc = cfg["schedules"]

    def run(script: str, payload: str) -> "subprocess.CompletedProcess[str]":
        proc = subprocess.run([sc["powershell"], "-NoProfile", "-NonInteractive", "-Command", script],
                              input=payload.encode("ascii"), capture_output=True,
                              timeout=sc["powershell_timeout_s"])
        return subprocess.CompletedProcess(proc.args, proc.returncode,
                                           proc.stdout.decode("utf-8-sig", errors="replace"),
                                           proc.stderr.decode("utf-8", errors="replace"))
    return run
