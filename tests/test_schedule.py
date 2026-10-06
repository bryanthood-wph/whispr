"""pipeline/schedule.py: Windows scheduled tasks generated from config (D.2, D.7, L16).

A fake runner stands in for PowerShell: no test reads or writes a real scheduled task.
FakeScheduler answers the read script from XML shaped like Export-ScheduledTask output
(UTF-16 declaration, task namespace, local start times, the user as DOMAIN\\user on a
logon trigger and as a SID on the principal, elements at their schema default left out)
and records every write.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

import yaml

from pipeline import schedule as S
from pipeline.__main__ import main
from pipeline.config import SCHEMA_PATH, ConfigError, load_config
from pipeline.jsonschema_lite import validate
from pipeline_helpers import overlay

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
ARMED = "2026-10-05T22:00:00.0000000-04:00"
STAMPED_DIR = "WindowsApps/PythonSoftwareFoundation.Python.3.12_3.12.2032.0_x64__qbz5n2kfra8p0"
# The account running the command, as the read script reports it: DOMAIN\user, SID.
ME_IDS = ["CORP\\pat", "S-1-5-21-1-2-3-1001"]
# Before _Base patches it: the subcommands main()'s parser really defines.
REAL_COMMANDS = S.pipeline_commands


def _iso(minutes: int) -> str:
    return f"PT{minutes // 60}H" if minutes % 60 == 0 else f"PT{minutes}M"


def _user_id(user: str | None, index: int) -> str:
    """ME as the read script's identity `index` (0 name, 1 SID), any other user as is."""
    return f"<UserId>{ME_IDS[index] if user == S.ME else user}</UserId>" if user else ""


def render_xml(task: dict) -> str:
    """Task XML for a normalized task, as Export-ScheduledTask writes it."""
    triggers = []
    for t in task["triggers"]:
        body = []
        if t["repeat"]:
            dur = t["repeat"]["duration_min"]
            body.append(f"<Repetition><Interval>{_iso(t['repeat']['every_min'])}</Interval>"
                        + (f"<Duration>{_iso(dur)}</Duration>" if dur else "")
                        + "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>")
        if not t["enabled"]:
            body.append("<Enabled>false</Enabled>")
        if t.get("end"):
            body.append(f"<EndBoundary>{t['end']}</EndBoundary>")
        for tag, key in (("Delay", "delay_min"), ("RandomDelay", "random_delay_min")):
            if t.get(key):
                body.append(f"<{tag}>{_iso(t[key])}</{tag}>")
        start = f"<StartBoundary>2026-10-05T{t.get('at')}:00{t.get('utc_offset') or ''}</StartBoundary>"
        if t["kind"] == "daily":
            body.append(f"{start}<ScheduleByDay><DaysInterval>{t['days_interval']}</DaysInterval></ScheduleByDay>")
            tag = "CalendarTrigger"
        elif t["kind"] == "logon":
            body.append(_user_id(t.get("user"), 0))
            tag = "LogonTrigger"
        else:
            body.append(start)
            tag = "TimeTrigger"
        triggers.append(f"<{tag}>{''.join(body)}</{tag}>")
    s = task["settings"]
    settings = [f"<MultipleInstancesPolicy>{s['multiple_instances']}</MultipleInstancesPolicy>"]
    for tag, value, default in (("DisallowStartIfOnBatteries", s["ac_power_only"], True),
                                ("StopIfGoingOnBatteries", s["stop_on_battery"], True),
                                ("StartWhenAvailable", s["start_when_available"], False),
                                ("WakeToRun", s["wake_to_run"], False),
                                ("Enabled", s["enabled"], True),
                                ("RunOnlyIfIdle", s["run_only_if_idle"], False),
                                ("RunOnlyIfNetworkAvailable", s["run_only_if_network"], False),
                                ("AllowHardTerminate", s["allow_hard_terminate"], True)):
        if value != default:   # Windows leaves an element at its schema default out
            settings.append(f"<{tag}>{str(value).lower()}</{tag}>")
    settings.append(f"<ExecutionTimeLimit>{_iso(s['time_limit_min'])}</ExecutionTimeLimit>")
    if s["restart_count"]:
        settings.append(f"<RestartOnFailure><Interval>{_iso(s['restart_interval_min'])}</Interval>"
                        f"<Count>{s['restart_count']}</Count></RestartOnFailure>")
    p = task["principal"]
    level = {"Limited": "", "Highest": "<RunLevel>HighestAvailable</RunLevel>"}[p["run_level"]]
    logon = {"Interactive": "InteractiveToken", "S4U": "S4U"}[p["logon_type"]]
    actions = "".join(
        f"<Exec><Command>{escape(a['command'])}</Command><Arguments>{escape(a['arguments'])}</Arguments>"
        f"<WorkingDirectory>{escape(a['working_directory'])}</WorkingDirectory></Exec>" for a in task["actions"])
    return (f'<?xml version="1.0" encoding="UTF-16"?>\n<Task version="1.4" xmlns="{NS}">'
            f"<RegistrationInfo><URI>\\{task['name']}</URI></RegistrationInfo>"
            f"<Triggers>{''.join(triggers)}</Triggers>"
            f"<Principals><Principal id=\"Author\">{_user_id(p['user'], 1)}"
            f"<LogonType>{logon}</LogonType>{level}</Principal></Principals>"
            f"<Settings>{''.join(settings)}</Settings>"
            f'<Actions Context="Author">{actions}</Actions></Task>')


def _done(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeScheduler:
    """The runner tests pass instead of PowerShell."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}   # name -> {"xml", "next_run"}
        self.calls: list[tuple[str, dict]] = []
        self.next_run_on_write: str | None = ARMED
        self.result: subprocess.CompletedProcess | None = None   # answer every call with this
        # Mutates (a copy of) each written task before it is stored: Task Scheduler
        # accepting a definition and keeping a different one.
        self.alter_on_write = None

    def add(self, task: dict, next_run: str | None = ARMED) -> None:
        self.tasks[task["name"]] = {"xml": render_xml(task), "next_run": next_run}

    def writes(self) -> list[tuple[str, str]]:
        return [(req["mode"], req["task"]["name"]) for kind, req in self.calls if kind == "write"]

    def __call__(self, script: str, payload: str) -> subprocess.CompletedProcess:
        payload.encode("ascii")   # the payload must be pure ASCII
        kind = {S.READ_SCRIPT: "read", S.WRITE_SCRIPT: "write"}[script]
        req = json.loads(payload)
        self.calls.append((kind, req))
        if self.result is not None:
            return self.result
        if kind == "read":
            rows = [{"name": n, "exists": n in self.tasks, "xml": self.tasks.get(n, {}).get("xml"),
                     "next_run": self.tasks.get(n, {}).get("next_run")} for n in req["names"]]
            return _done(json.dumps({"me": ME_IDS, "tasks": rows}))
        name = req["task"]["name"]
        if req["mode"] == "register" and name in self.tasks:
            return _done(returncode=1, stderr="Cannot create a file when that file already exists.")
        if req["mode"] == "set" and name not in self.tasks:
            return _done(returncode=1, stderr="The system cannot find the file specified.")
        stored = copy.deepcopy(req["task"])
        if self.alter_on_write:
            self.alter_on_write(stored)
        self.tasks[name] = {"xml": render_xml(stored), "next_run": self.next_run_on_write}
        return _done()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.interp = self._touch(self.root / "venv" / "Scripts" / "pythonw.exe")
        self.cfg = self.load()
        self.fake = FakeScheduler()
        # The shipped jobs' subcommands are not all implemented yet. Tests that are not
        # about that treat every configured one as defined (TestCommands uses the real set).
        sc = self.cfg["schedules"]
        shipped = frozenset(S._pipeline_command([*sc["interpreter_args"], *job["args"]])
                            for job in sc["tasks"].values())
        patcher = mock.patch.object(S, "pipeline_commands", return_value=shipped)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _touch(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    def load(self, **schedules) -> dict:
        ov = overlay(self.root)
        ov["schedules"] = {"interpreter": str(self.interp), **schedules}
        return load_config(overlay=ov)

    def want(self, cfg: dict | None = None) -> dict[str, dict]:
        return {t["name"]: t for t in S.desired(cfg or self.cfg)}

    def install_all(self) -> None:
        for task in self.want().values():
            self.fake.add(task)


class TestDesired(_Base):
    def test_one_task_per_configured_job(self):
        want = self.want()
        self.assertEqual(list(want), list(self.cfg["schedules"]["tasks"]))
        for name in want:
            self.assertNotIn(name, self.cfg["schedules"]["reserved_names"])

    def test_action_runs_interpreter_with_configured_args(self):
        action, = self.want()["whispr-pipeline"]["actions"]
        sc = self.cfg["schedules"]
        self.assertEqual(action["command"], str(self.interp))
        self.assertEqual(action["arguments"].split(),
                         [*sc["interpreter_args"], *sc["tasks"]["whispr-pipeline"]["args"]])
        self.assertEqual(action["working_directory"], str(S.REPO_ROOT))

    def test_null_interpreter_is_interpreter_name_beside_the_running_one(self):
        cfg = self.load(interpreter=None)
        running = self.interp.with_name("python.exe")
        action, = S.desired(cfg, executable=str(running))[0]["actions"]
        self.assertEqual(action["command"], str(running.with_name(cfg["schedules"]["interpreter_name"])))

    def test_version_stamped_interpreter_is_refused(self):
        stamped = self._touch(self.root / STAMPED_DIR / "pythonw.exe")
        with self.assertRaises(S.ScheduleError) as ctx:
            S.desired(self.load(interpreter=str(stamped)))
        self.assertIn("version-stamped", str(ctx.exception))

    def test_venv_on_a_version_stamped_base_is_refused(self):
        (self.root / "venv" / "pyvenv.cfg").write_text(
            f"home = {self.root / STAMPED_DIR}\ninclude-system-site-packages = false\n", encoding="utf-8")
        with self.assertRaises(S.ScheduleError):
            S.desired(self.cfg)

    def test_venv_on_a_stable_base_is_accepted(self):
        (self.root / "venv" / "pyvenv.cfg").write_text("home = C:\\Python314\n", encoding="utf-8")
        self.assertEqual(len(S.desired(self.cfg)), len(self.cfg["schedules"]["tasks"]))

    def test_missing_interpreter_is_refused(self):
        with self.assertRaises(S.ScheduleError):
            S.desired(self.load(interpreter=str(self.root / "nope" / "pythonw.exe")))

    def test_repetition_is_its_own_trigger_not_grafted_onto_logon(self):
        triggers = self.want()["whispr-liveness"]["triggers"]
        every = self.cfg["schedules"]["tasks"]["whispr-liveness"]["every_min"]
        logon = [t for t in triggers if t["kind"] == "logon"]
        repeating = [t for t in triggers if t["repeat"]]
        self.assertEqual(len(logon), 1)
        self.assertIsNone(logon[0]["repeat"])
        self.assertEqual([t["kind"] for t in repeating], ["once"])
        self.assertEqual(repeating[0]["repeat"], {"every_min": every, "duration_min": None})

    def test_daily_trigger_from_config(self):
        triggers = self.want()["whispr-daily"]["triggers"]
        at = self.cfg["schedules"]["tasks"]["whispr-daily"]["daily_at"]
        self.assertEqual(triggers, [{"kind": "daily", "at": at, "days_interval": 1, "repeat": None, "enabled": True,
                                     "utc_offset": None, "end": None, "delay_min": None, "random_delay_min": None}])

    def test_power_and_catch_up_flags(self):
        want = self.want()
        pipeline = want["whispr-pipeline"]["settings"]
        self.assertFalse(pipeline["ac_power_only"])         # model calls run on any power (2026-10-05)
        self.assertFalse(pipeline["stop_on_battery"])
        for task in want.values():
            self.assertTrue(task["settings"]["start_when_available"], task["name"])

    def test_overlay_values_flow_through(self):
        cfg = self.load(tasks={"whispr-liveness": {"every_min": 30, "time_limit_min": 7}})
        task = self.want(cfg)["whispr-liveness"]
        self.assertEqual([t["repeat"]["every_min"] for t in task["triggers"] if t["repeat"]], [30])
        self.assertEqual(task["settings"]["time_limit_min"], 7)

    def test_invalid_jobs_are_refused(self):
        bad = {
            "no time trigger": {"daily_at": None, "every_min": None, "at_logon": True},
            "bad time": {"daily_at": "25:00"},
            "interval without count": {"restart_count": 0, "restart_interval_min": 5},
            "count without interval": {"restart_count": 2, "restart_interval_min": None},
        }
        for label, job in bad.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                S.desired(self.load(tasks={"whispr-pipeline": job}))

    def test_reserved_and_wildcard_names_are_refused(self):
        for name in ("whispr-recorder", "whispr-*"):
            cfg = copy.deepcopy(self.cfg)
            cfg["schedules"]["tasks"][name] = cfg["schedules"]["tasks"].pop("whispr-daily")
            with self.subTest(name), self.assertRaises(ConfigError):
                S.desired(cfg)

    def test_reserved_names_ignore_case(self):
        # Task Scheduler resolves Whispr-Recorder to the live whispr-recorder.
        for reserved in self.cfg["schedules"]["reserved_names"]:
            for name in (reserved.upper(), reserved.title()):
                cfg = copy.deepcopy(self.cfg)
                cfg["schedules"]["tasks"][name] = cfg["schedules"]["tasks"].pop("whispr-daily")
                with self.subTest(name), self.assertRaises(ConfigError):
                    S.desired(cfg)

    def test_task_names_differing_only_in_case_are_refused(self):
        cfg = copy.deepcopy(self.cfg)
        tasks = cfg["schedules"]["tasks"]
        tasks["WHISPR-DAILY"] = copy.deepcopy(tasks["whispr-daily"])
        with self.assertRaises(ConfigError) as ctx:
            S.desired(cfg)
        self.assertIn("ignore case", str(ctx.exception))

    def test_multiple_instances_is_the_cmdlets_enum(self):
        # MultipleInstancesEnum is Parallel, Queue, IgnoreNew: StopExisting would only fail at write time.
        with self.assertRaises(ConfigError):
            self.load(tasks={"whispr-daily": {"multiple_instances": "StopExisting"}})

    def test_schema_does_not_pin_the_task_names(self):
        cfg = copy.deepcopy(self.cfg)
        tasks = cfg["schedules"]["tasks"]
        tasks["whispr-nightly"] = tasks.pop("whispr-pipeline")
        del tasks["whispr-daily"]
        self.assertEqual(validate(cfg, json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))), [])


class TestCommands(_Base):
    """A task whose `-m pipeline` subcommand __main__.py does not define is never written."""

    def with_args(self, *args: str) -> dict:
        return self.load(tasks={"whispr-daily": {"args": list(args)}})

    def test_subcommands_come_from_mains_parser(self):
        self.assertIn("schedule", REAL_COMMANDS())
        self.assertNotIn("no-such-command", REAL_COMMANDS())

    def test_shipped_jobs_are_gated_by_the_real_parser(self):
        sc = self.cfg["schedules"]
        undefined = {name for name, job in sc["tasks"].items()
                     if S._pipeline_command([*sc["interpreter_args"], *job["args"]]) not in REAL_COMMANDS()}
        self.install_all()
        with mock.patch.object(S, "pipeline_commands", REAL_COMMANDS):
            found = {f.task for f in S.check(self.cfg, self.fake) if f.kind == S.NO_COMMAND}
        self.assertEqual(found, undefined)

    def test_undefined_subcommand_is_a_check_finding(self):
        cfg = self.with_args("-m", "pipeline", "no-such-command")
        for task in self.want(cfg).values():
            self.fake.add(task)
        finding, = S.check(cfg, self.fake)
        self.assertEqual((finding.task, finding.kind), ("whispr-daily", S.NO_COMMAND))
        self.assertIn("no-such-command", finding.detail)

    def test_no_subcommand_at_all_is_a_finding(self):
        cfg = self.with_args("-m", "pipeline")
        for task in self.want(cfg).values():
            self.fake.add(task)
        self.assertEqual([f.kind for f in S.check(cfg, self.fake)], [S.NO_COMMAND])

    def test_undefined_subcommand_is_never_registered(self):
        cfg = self.with_args("-m", "pipeline", "no-such-command")
        with self.assertRaises(S.RegistrationFailed) as ctx:
            S.register(cfg, self.fake)
        self.assertEqual(self.fake.writes(), [("register", "whispr-pipeline"), ("register", "whispr-liveness")])
        self.assertEqual([(f.task, f.kind) for f in ctx.exception.outcome.findings],
                         [("whispr-daily", S.NO_COMMAND), ("whispr-daily", S.MISSING)])

    def test_undefined_subcommand_is_never_applied(self):
        cfg = self.with_args("-m", "pipeline", "no-such-command")
        want = self.want(cfg)
        for task in want.values():
            self.fake.add(task)
        drifted = copy.deepcopy(want["whispr-daily"])
        drifted["settings"]["time_limit_min"] = 999
        self.fake.add(drifted)
        outcome = S.apply(cfg, self.fake)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual([f.kind for f in outcome.findings], [S.NO_COMMAND, S.SETTINGS])

    def test_args_running_another_module_are_not_gated(self):
        cfg = self.with_args("-m", "whispr")
        for task in self.want(cfg).values():
            self.fake.add(task)
        self.assertEqual(S.check(cfg, self.fake), [])


class TestNormalizeXml(unittest.TestCase):
    EXPORTED = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.4" xmlns="{NS}">\n'
        '  <RegistrationInfo><URI>\\whispr-liveness</URI></RegistrationInfo>\n'
        '  <Principals><Principal id="Author"><UserId>S-1-5-21-1-2-3-1001</UserId>'
        '<LogonType>InteractiveToken</LogonType></Principal></Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>\n'
        '    <IdleSettings><StopOnIdleEnd>true</StopOnIdleEnd></IdleSettings>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '  </Settings>\n'
        '  <Triggers>\n'
        '    <LogonTrigger><UserId>CORP\\pat</UserId></LogonTrigger>\n'
        '    <TimeTrigger><Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd>'
        '</Repetition><StartBoundary>2026-10-05T00:00:00-04:00</StartBoundary></TimeTrigger>\n'
        '  </Triggers>\n'
        '  <Actions Context="Author"><Exec><Command>"C:\\w\\pythonw.exe"</Command>'
        '<Arguments>-s -m pipeline liveness</Arguments><WorkingDirectory>C:\\w</WorkingDirectory></Exec></Actions>\n'
        '</Task>\n')

    def test_exported_xml_normalizes_with_schema_defaults(self):
        task = S._normalize_xml("whispr-liveness", self.EXPORTED, ME_IDS)
        self.assertEqual(task["actions"], [{"command": "C:\\w\\pythonw.exe", "arguments": "-s -m pipeline liveness",
                                            "working_directory": "C:\\w"}])
        unset = {"end": None, "delay_min": None, "random_delay_min": None}
        self.assertEqual(task["triggers"], [
            {"kind": "logon", "user": S.ME, "repeat": None, "enabled": True, "utc_offset": None, **unset},
            {"kind": "once", "at": "00:00", "repeat": {"every_min": 15, "duration_min": None}, "enabled": True,
             "utc_offset": "-04:00", **unset}])
        self.assertEqual(task["settings"], {
            "enabled": True, "time_limit_min": 5, "ac_power_only": False, "stop_on_battery": False,
            "start_when_available": True, "wake_to_run": False, "restart_count": 0,
            "restart_interval_min": None, "multiple_instances": "IgnoreNew", "run_only_if_idle": False,
            "run_only_if_network": False, "allow_hard_terminate": True})
        self.assertEqual(task["principal"], {"logon_type": "Interactive", "run_level": "Limited", "user": S.ME})

    def test_another_account_is_not_me(self):
        task = S._normalize_xml("whispr-liveness", self.EXPORTED.replace("CORP\\pat", "CORP\\sam"), ME_IDS)
        self.assertEqual(task["triggers"][0]["user"], "CORP\\sam")

    def test_current_user_matches_by_name_bare_name_or_sid(self):
        for user_id in ("CORP\\pat", "corp\\PAT", "pat", "S-1-5-21-1-2-3-1001"):
            with self.subTest(user_id):
                self.assertEqual(S._user(user_id, ME_IDS), S.ME)
        self.assertEqual(S._user("OTHER\\pat", ME_IDS), "OTHER\\pat")
        self.assertIsNone(S._user(None, ME_IDS))

    def test_unreadable_xml_is_an_error(self):
        with self.assertRaises(S.ScheduleError):
            S._normalize_xml("x", "<Task><Triggers>", ME_IDS)


class TestCheck(_Base):
    def drift(self, task: dict, **kw) -> list[S.Finding]:
        self.fake.add(task, **kw)
        return S.check(self.cfg, self.fake)

    def test_matching_live_tasks_pass(self):
        self.install_all()
        self.assertEqual(S.check(self.cfg, self.fake), [])
        self.assertEqual(self.fake.writes(), [])

    def test_missing_task(self):
        want = self.want()
        for name in ("whispr-pipeline", "whispr-daily"):
            self.fake.add(want[name])
        self.assertEqual(S.check(self.cfg, self.fake),
                         [S.Finding("whispr-liveness", S.MISSING, "not registered")])

    def test_each_drift_kind_is_found(self):
        cases = {
            S.TRIGGERS: lambda t: t["triggers"][0].update(at="23:15"),
            S.ACTION: lambda t: t["actions"][0].update(command=str(self.root / STAMPED_DIR / "pythonw.exe")),
            S.SETTINGS: lambda t: t["settings"].update(wake_to_run=True),
            S.PRINCIPAL: lambda t: t["principal"].update(run_level="Highest"),
        }
        for kind, mutate in cases.items():
            with self.subTest(kind):
                self.fake = FakeScheduler()
                self.install_all()
                live = copy.deepcopy(self.want()["whispr-daily"])     # its first trigger is the daily one
                mutate(live)
                findings = self.drift(live)
                self.assertEqual([(f.task, f.kind) for f in findings], [("whispr-daily", kind)])

    def test_setting_finding_names_the_key(self):
        self.install_all()
        live = copy.deepcopy(self.want()["whispr-pipeline"])
        live["settings"]["ac_power_only"] = not live["settings"]["ac_power_only"]
        finding, = self.drift(live)
        self.assertIn("ac_power_only", finding.detail)

    def test_repetition_grafted_onto_logon_is_drift(self):
        self.install_all()
        live = copy.deepcopy(self.want()["whispr-liveness"])
        logon = next(t for t in live["triggers"] if t["kind"] == "logon")
        live["triggers"] = [{**logon, "repeat": {"every_min": 15, "duration_min": None}}]
        self.assertEqual([f.kind for f in self.drift(live)], [S.TRIGGERS])

    def test_values_config_does_not_set_drift_from_the_cmdlet_defaults(self):
        def trigger(kind, **values):
            return lambda task: next(t for t in task["triggers"] if t["kind"] == kind).update(values)
        cases = {
            "expired": ("whispr-daily", trigger("daily", end="2026-01-01T00:00:00"), S.TRIGGERS),
            "random delay": ("whispr-daily", trigger("daily", random_delay_min=30), S.TRIGGERS),
            "logon delay": ("whispr-liveness", trigger("logon", delay_min=5), S.TRIGGERS),
            "logon of another user": ("whispr-liveness", trigger("logon", user="CORP\\sam"), S.TRIGGERS),
            "logon of any user": ("whispr-liveness", trigger("logon", user=None), S.TRIGGERS),
            "once at another time": ("whispr-liveness", trigger("once", at="08:00"), S.TRIGGERS),
            "principal of another user": ("whispr-daily", lambda t: t["principal"].update(user="CORP\\sam"),
                                          S.PRINCIPAL),
            "idle only": ("whispr-daily", lambda t: t["settings"].update(run_only_if_idle=True), S.SETTINGS),
            "network only": ("whispr-daily", lambda t: t["settings"].update(run_only_if_network=True), S.SETTINGS),
            "no hard terminate": ("whispr-daily", lambda t: t["settings"].update(allow_hard_terminate=False),
                                  S.SETTINGS),
        }
        for label, (name, mutate, kind) in cases.items():
            with self.subTest(label):
                self.fake = FakeScheduler()
                self.install_all()
                live = copy.deepcopy(self.want()[name])
                mutate(live)
                self.assertEqual([(f.task, f.kind) for f in self.drift(live)], [(name, kind)])

    def test_utc_offset_start_time_is_drift(self):
        # An offset start time is "synchronize across time zones": it moves an hour with DST.
        for name, kind, offset in (("whispr-daily", "daily", "-04:00"), ("whispr-liveness", "once", "Z")):
            with self.subTest(kind):
                self.fake = FakeScheduler()
                self.install_all()
                live = copy.deepcopy(self.want()[name])
                next(t for t in live["triggers"] if t["kind"] == kind)["utc_offset"] = offset
                finding, = self.drift(live)
                self.assertEqual((finding.task, finding.kind), (name, S.TRIGGERS))
                self.assertIn(offset, finding.detail)

    def test_unarmed_task_is_drift(self):
        self.install_all()
        findings = self.drift(self.want()["whispr-liveness"], next_run=None)
        self.assertEqual([(f.task, f.kind) for f in findings], [("whispr-liveness", S.NOT_ARMED)])

    def test_path_case_is_not_drift(self):
        self.install_all()
        live = copy.deepcopy(self.want()["whispr-daily"])
        live["actions"][0]["command"] = live["actions"][0]["command"].upper()
        self.assertEqual(self.drift(live), [])

    def test_read_sends_names_as_data(self):
        self.install_all()
        S.check(self.cfg, self.fake)
        (kind, req), = self.fake.calls
        self.assertEqual(kind, "read")
        self.assertEqual(req, {"task_path": self.cfg["schedules"]["task_path"], "names": list(self.want())})
        for name in self.want():
            self.assertNotIn(name, S.READ_SCRIPT)


class TestApply(_Base):
    def test_updates_drifted_tasks_and_never_registers(self):
        want = self.want()
        self.fake.add(want["whispr-pipeline"])
        drifted = copy.deepcopy(want["whispr-daily"])
        drifted["settings"]["time_limit_min"] = 999
        self.fake.add(drifted)
        outcome = S.apply(self.cfg, self.fake)
        self.assertEqual(self.fake.writes(), [("set", "whispr-daily")])
        self.assertEqual(outcome.changed, ["whispr-daily"])
        self.assertEqual(outcome.findings, [S.Finding("whispr-liveness", S.MISSING, "not registered")])
        self.assertNotIn("whispr-liveness", self.fake.tasks)

    def test_clean_tasks_are_left_alone(self):
        self.install_all()
        outcome = S.apply(self.cfg, self.fake)
        self.assertEqual((outcome.changed, outcome.findings, self.fake.writes()), ([], [], []))

    def test_unarmed_task_is_rewritten(self):
        self.install_all()
        self.fake.add(self.want()["whispr-liveness"], next_run=None)
        outcome = S.apply(self.cfg, self.fake)
        self.assertEqual(self.fake.writes(), [("set", "whispr-liveness")])
        self.assertEqual(outcome.findings, [])

    def test_still_unarmed_after_set_stays_a_finding(self):
        self.install_all()
        self.fake.add(self.want()["whispr-liveness"], next_run=None)
        self.fake.next_run_on_write = None
        outcome = S.apply(self.cfg, self.fake)
        self.assertEqual([f.kind for f in outcome.findings], [S.NOT_ARMED])

    def test_confirm_sees_the_planned_changes_before_any_write(self):
        self.install_all()
        drifted = copy.deepcopy(self.want()["whispr-daily"])
        drifted["settings"]["time_limit_min"] = 999
        self.fake.add(drifted)
        seen = []

        def confirm(planned):
            seen.append(([(f.task, f.kind) for f in planned], self.fake.writes()))
            return False
        outcome = S.apply(self.cfg, self.fake, confirm=confirm)
        self.assertEqual(seen, [([("whispr-daily", S.SETTINGS)], [])])
        self.assertTrue(outcome.declined)
        self.assertEqual((outcome.changed, self.fake.writes()), ([], []))

    def test_task_altered_on_write_stays_a_finding(self):
        self.install_all()
        drifted = copy.deepcopy(self.want()["whispr-liveness"])
        drifted["settings"]["time_limit_min"] = 999
        self.fake.add(drifted)

        def add_duration(task):   # the repetition comes back bounded: it stops after a day
            next(t for t in task["triggers"] if t["repeat"])["repeat"]["duration_min"] = 1440
        self.fake.alter_on_write = add_duration
        outcome = S.apply(self.cfg, self.fake)
        self.assertEqual(outcome.changed, ["whispr-liveness"])
        self.assertEqual([(f.task, f.kind) for f in outcome.findings], [("whispr-liveness", S.TRIGGERS)])


class TestRegister(_Base):
    def test_registers_missing_tasks_and_reads_them_back(self):
        self.fake.add(self.want()["whispr-pipeline"])
        outcome = S.register(self.cfg, self.fake)
        self.assertEqual(self.fake.writes(), [("register", "whispr-daily"), ("register", "whispr-liveness")])
        self.assertEqual(outcome.changed, ["whispr-daily", "whispr-liveness"])
        self.assertEqual(outcome.findings, [])
        self.assertEqual([kind for kind, _ in self.fake.calls][-1], "read")   # the read-back

    def test_skipped_task_is_neither_registered_nor_a_finding(self):
        cfg = self.load(skip=["WHISPR-LIVENESS"])           # names ignore case, as Task Scheduler's do
        outcome = S.register(cfg, self.fake)
        self.assertEqual(self.fake.writes(), [("register", "whispr-pipeline"), ("register", "whispr-daily")])
        self.assertEqual(outcome.findings, [])
        self.assertEqual(S.check(cfg, self.fake), [])
        self.assertIsNone(S.task_for(cfg, "liveness"))
        self.assertEqual(S.task_for(cfg, "daily"), "whispr-daily")

    def test_skip_naming_no_task_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            S.desired(self.load(skip=["whispr-livenes"]))
        self.assertIn("schedules.skip", str(ctx.exception))

    def test_payload_carries_the_desired_task(self):
        S.register(self.cfg, self.fake)
        sent = {req["task"]["name"]: req["task"] for kind, req in self.fake.calls if kind == "write"}
        self.assertEqual(sent, self.want())

    def test_missing_next_run_time_fails_loudly(self):
        self.fake.next_run_on_write = None
        with self.assertRaises(S.RegistrationFailed) as ctx:
            S.register(self.cfg, self.fake)
        kinds = {(f.task, f.kind) for f in ctx.exception.outcome.findings}
        self.assertEqual(kinds, {(name, S.NOT_ARMED) for name in self.want()})
        self.assertIn("NextRunTime", str(ctx.exception))

    def test_existing_task_is_never_re_registered(self):
        drifted = copy.deepcopy(self.want()["whispr-daily"])
        drifted["settings"]["wake_to_run"] = True
        self.fake.add(drifted)
        with self.assertRaises(S.RegistrationFailed) as ctx:
            S.register(self.cfg, self.fake)
        self.assertNotIn(("register", "whispr-daily"), self.fake.writes())
        self.assertEqual([(f.task, f.kind) for f in ctx.exception.outcome.findings],
                         [("whispr-daily", S.SETTINGS)])

    def test_task_altered_on_write_fails_the_read_back(self):
        def drop_repetition(task):   # registered cleanly, kept without its repetition
            for t in task["triggers"]:
                t["repeat"] = None
        self.fake.alter_on_write = drop_repetition
        with self.assertRaises(S.RegistrationFailed) as ctx:
            S.register(self.cfg, self.fake)
        repeating = [name for name, job in self.cfg["schedules"]["tasks"].items() if job["every_min"] is not None]
        self.assertEqual([(f.task, f.kind) for f in ctx.exception.outcome.findings],
                         [(name, S.TRIGGERS) for name in repeating])
        self.assertEqual(ctx.exception.outcome.changed, list(self.want()))

    def test_declined_registers_nothing_and_does_not_raise(self):
        outcome = S.register(self.cfg, self.fake, confirm=lambda planned: False)
        self.assertTrue(outcome.declined)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual({f.kind for f in outcome.findings}, {S.MISSING})


class TestPowerShellFailures(_Base):
    def test_nonzero_exit_raises_with_stderr(self):
        self.fake.result = _done(returncode=1, stderr="Access is denied.")
        with self.assertRaises(S.ScheduleError) as ctx:
            S.check(self.cfg, self.fake)
        self.assertIn("Access is denied.", str(ctx.exception))

    def test_failed_register_stops(self):
        self.install_all()
        del self.fake.tasks["whispr-daily"]
        real = self.fake.__call__

        def run(script, payload):
            if script == S.WRITE_SCRIPT:
                return _done(returncode=1, stderr="The task XML contains a value which is incorrectly formatted")
            return real(script, payload)
        with self.assertRaises(S.ScheduleError) as ctx:
            S.register(self.cfg, run)
        self.assertIn("incorrectly formatted", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, S.WriteFailed)   # nothing was written

    def fail_after_first_write(self, script: str, payload: str) -> subprocess.CompletedProcess:
        if script == S.WRITE_SCRIPT and self.fake.writes():
            return _done(returncode=1, stderr="Access is denied.")
        return self.fake(script, payload)

    def test_register_failing_partway_names_and_reads_back_what_was_written(self):
        with self.assertRaises(S.WriteFailed) as ctx:
            S.register(self.cfg, self.fail_after_first_write)
        first, failed = list(self.want())[:2]
        self.assertEqual(ctx.exception.outcome.changed, [first])
        self.assertEqual(ctx.exception.outcome.findings, [])
        message = str(ctx.exception)
        for text in (failed, "Access is denied.", first, "match config"):
            self.assertIn(text, message)
        self.assertEqual(self.fake.calls[-1][0], "read")   # the read-back of what was written

    def test_apply_failing_partway_reports_the_read_back(self):
        self.install_all()
        want = self.want()
        for name in ("whispr-pipeline", "whispr-daily"):
            drifted = copy.deepcopy(want[name])
            drifted["settings"]["time_limit_min"] = 999
            self.fake.add(drifted)
        self.fake.next_run_on_write = None   # the one that was written comes back unarmed
        with self.assertRaises(S.WriteFailed) as ctx:
            S.apply(self.cfg, self.fail_after_first_write)
        self.assertEqual(ctx.exception.outcome.changed, ["whispr-pipeline"])
        self.assertEqual([(f.task, f.kind) for f in ctx.exception.outcome.findings],
                         [("whispr-pipeline", S.NOT_ARMED)])
        self.assertIn("NextRunTime", str(ctx.exception))

    def test_timeout_and_missing_host_raise(self):
        for exc in (subprocess.TimeoutExpired("powershell.exe", 1), FileNotFoundError("powershell.exe")):
            def run(script, payload, exc=exc):
                raise exc
            with self.subTest(type(exc).__name__), self.assertRaises(S.ScheduleError):
                S.check(self.cfg, run)

    def test_unreadable_output_raises(self):
        row = {"name": "whispr-pipeline", "exists": True, "xml": None, "next_run": None}
        for stdout in ("WARNING: something\n", json.dumps([row]), json.dumps({"tasks": [row]}),
                       json.dumps({"me": ME_IDS, "tasks": [{"name": "whispr-pipeline"}]}),
                       json.dumps({"me": ME_IDS, "tasks": [row]})):
            self.fake.result = _done(stdout)
            with self.subTest(stdout), self.assertRaises(S.ScheduleError):
                S.check(self.cfg, self.fake)


class TestPowerShellRunner(_Base):
    def test_script_is_fixed_text_and_values_go_over_stdin(self):
        odd = self._touch(self.root / "it's $(calc) \u00e9" / "pythonw.exe")
        cfg = self.load(interpreter=str(odd))
        sent = []

        def fake_run(argv, **kw):
            sent.append((argv, kw))
            rows = [{"name": n, "exists": False, "xml": None, "next_run": None} for n in cfg["schedules"]["tasks"]]
            reply = {"me": ME_IDS, "tasks": rows}
            return subprocess.CompletedProcess(argv, 0, json.dumps(reply).encode("ascii"), b"")
        with mock.patch.object(S.subprocess, "run", fake_run):
            S.check(cfg, S.powershell_runner(cfg))
            S.powershell_runner(cfg)(S.WRITE_SCRIPT, json.dumps({"mode": "set", "task": S.desired(cfg)[0]}))
        for argv, kw in sent:
            sc = cfg["schedules"]
            self.assertEqual(argv[:4], [sc["powershell"], "-NoProfile", "-NonInteractive", "-Command"])
            self.assertIn(argv[4], (S.READ_SCRIPT, S.WRITE_SCRIPT))
            self.assertEqual(len(argv), 5)
            self.assertEqual(kw["timeout"], sc["powershell_timeout_s"])
            kw["input"].decode("ascii")
        self.assertIn(str(odd), json.loads(sent[1][1]["input"])["task"]["actions"][0]["command"])

    def test_scripts_hold_no_double_quote(self):
        for script in (S.READ_SCRIPT, S.WRITE_SCRIPT):
            self.assertNotIn('"', script)

    def test_write_script_rewrites_both_start_times_as_local(self):
        # Checked against Windows PowerShell 5.1: the cmdlet writes 2026-10-06T02:00:00Z for
        # -Daily -At 22:00, and this rewrite gives 2026-10-05T22:00:00.
        self.assertEqual(S.WRITE_SCRIPT.count(".StartBoundary = ([datetime]"), 2)
        self.assertEqual(S.WRITE_SCRIPT.count(".StartBoundary).ToString('s')"), 2)


class TestCli(_Base):
    def cli(self, *args: str, run=None) -> tuple[int, str]:
        ov = overlay(self.root)
        ov["schedules"] = {"interpreter": str(self.interp)}
        path = self.root / "overlay.yaml"
        path.write_text(yaml.safe_dump(ov), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--overlay", str(path), *args], runner=lambda cfg: run or self.fake)
        return code, out.getvalue()

    def test_check_exit_codes(self):
        code, out = self.cli("schedule", "--check")
        self.assertEqual(code, 1)
        self.assertIn("missing", out)
        self.install_all()
        self.assertEqual(self.cli("schedule", "--check")[0], 0)

    def test_apply_reports_missing(self):
        code, out = self.cli("schedule", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("--register", out)
        self.assertEqual(self.fake.writes(), [])

    def test_register(self):
        self.assertEqual(self.cli("schedule", "--register", "--yes")[0], 0)
        self.assertEqual(len(self.fake.writes()), len(self.cfg["schedules"]["tasks"]))
        self.fake = FakeScheduler()
        self.fake.next_run_on_write = None
        code, out = self.cli("schedule", "--register", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)

    def test_register_without_yes_prints_the_plan_and_writes_nothing(self):
        code, out = self.cli("schedule", "--register")
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.writes(), [])
        for name in self.want():
            self.assertIn(f"would register {name}: missing", out)
        self.assertIn("--yes", out)

    def test_apply_prints_the_diff_and_writes_only_with_yes(self):
        self.install_all()
        drifted = copy.deepcopy(self.want()["whispr-daily"])
        drifted["settings"]["time_limit_min"] = 999
        self.fake.add(drifted)
        code, out = self.cli("schedule", "--apply")
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.writes(), [])
        self.assertIn("would change whispr-daily: settings: time_limit_min", out)
        self.assertIn("re-run with --yes", out)
        code, out = self.cli("schedule", "--apply", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.writes(), [("set", "whispr-daily")])
        self.assertLess(out.index("would change whispr-daily"), out.index("updated whispr-daily"))

    def test_partial_write_failure_exits_2_naming_what_was_written(self):
        real = self.fake

        def run(script, payload):
            if script == S.WRITE_SCRIPT and real.writes():
                return _done(returncode=1, stderr="Access is denied.")
            return real(script, payload)
        code, out = self.cli("schedule", "--register", "--yes", run=run)
        self.assertEqual(code, 2)
        self.assertIn(f"already written: {list(self.want())[:1]}", out)

    def test_powershell_error_exits_2(self):
        self.fake.result = _done(returncode=1, stderr="boom")
        code, out = self.cli("schedule", "--check")
        self.assertEqual(code, 2)
        self.assertIn("boom", out)

    def test_a_mode_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli("schedule")


if __name__ == "__main__":
    unittest.main()
