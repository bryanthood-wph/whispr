"""python -m pipeline doctor: the single source of status (docs/plan/D-architecture-and-ops.md
D.6). It reads; it never writes, launches, registers or calls a model.

Sections, each read on its own so one broken source never hides the others (a section
that cannot be read says why, and that needs attention):
- **jobs**: each job in doctor.stale_after_h: its last run, last successful run (stale
  past the configured hours, or never) and, for the pipeline, its queue backlog.
- **alerts**: every open alert, with its fix.
- **schedule**: schedule.check, read-only: drift between config and the live tasks.
- **auth**: the auth source of the last model call in the ledger, and whether it is
  approved (auth.approved_sources).
- **recorder**: whether it holds its mutex now (liveness.mutex), its log's age, and its
  incidents over doctor.incident_days (doctor.attention_incidents need attention).
- **liveness**: the last liveness check logged (stale past doctor.liveness_stale_min), unless
  schedules.skip leaves out every task that runs one.
- **reconciliation**: the last daily reconciliation and its problems.
- **tasks**: the task funnel over kg.tasks.funnel_days (kg.tasks.funnel_report); **notes**: live episodes whose note is
  missing or has no My Actions section; **quarantined**: items held after max attempts.

Everything that needs attention is listed in `attention`; exit 1 if any, else 0
(2: the config does not load, from the CLI). `--json` prints the report as JSON.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from kg import db, tasks
from kg.state import FAILED, State
from kg.store import Store
from pipeline import daily, liveness, models, render, write
from pipeline import run as pipeline_run
from pipeline import schedule as S
from pipeline.run import EXIT_FAILED, EXIT_OK


def _age_h(stamp: Optional[str], now: datetime) -> Optional[float]:
    if not stamp:
        return None
    at = datetime.fromisoformat(stamp)
    return round((now - (at if at.tzinfo else at.replace(tzinfo=timezone.utc))).total_seconds() / 3600, 2)


def _jobs(cfg: dict, conn, now: datetime, attention: list[str]) -> dict:
    state, out = State(conn, cfg), {}
    for job, hours in cfg["doctor"]["stale_after_h"].items():
        last = db.fetch_one(conn, "SELECT * FROM run WHERE job = ? ORDER BY seq DESC LIMIT 1", (job,))
        ok = state.last_success(job)
        age = _age_h(ok["finished_at"] if ok else None, now)
        entry = {"last_run": dict(last) if last else None, "last_success": ok["finished_at"] if ok else None,
                 "last_success_age_h": age}
        if job == pipeline_run.JOB:
            entry["backlog"] = state.backlog(pipeline_run.STAGE)
        out[job] = entry
        if age is None:
            attention.append(f"{job}: no successful run yet")
        elif age > hours:
            attention.append(f"{job}: last successful run {age} h ago (more than {hours} h)")
        if last and last["status"] == FAILED:
            attention.append(f"{job}: its last run failed: {last['error']}")
    return out


def _alerts(cfg: dict, conn, attention: list[str]) -> list[dict]:
    alerts = [{"key": a["dedupe_key"], "kind": a["kind"], "message": a["message"], "fix": a["fix"],
               "count": a["count"], "last_seen": a["last_seen"]} for a in State(conn, cfg).open_alerts()]
    if alerts:
        attention.append(f"{len(alerts)} open alert(s): " + ", ".join(a["key"] for a in alerts))
    return alerts


def _schedule(cfg: dict, run: Optional[S.Runner], attention: list[str]) -> dict:
    findings = S.check(cfg, run or S.powershell_runner(cfg))
    if findings:
        attention.append(f"schedule: {len(findings)} finding(s): " + "; ".join(map(str, findings)))
    return {"findings": [str(f) for f in findings]}


def _auth(cfg: dict, attention: list[str]) -> dict:
    rows = [r for r in models.read_jsonl(pipeline_run.files(cfg, "ledger")) if "auth_source" in r]
    if not rows:
        return {"last_call": None}
    last = rows[-1]
    approved = last["auth_source"] in cfg["auth"]["approved_sources"]
    if not approved:
        attention.append(f"auth: the last model call ran on {last['auth_source']!r}, not an approved source")
    return {"last_call": last.get("ts"), "source": last["auth_source"], "approved": approved}


def _recorder(cfg: dict, recorder: Optional[liveness.Recorder], now: datetime, attention: list[str]) -> dict:
    d, mutex = cfg["doctor"], cfg["liveness"]["mutex"]
    try:
        running = (recorder or liveness.WinRecorder(cfg)).held(mutex)
    except OSError as exc:
        running, probe_error = None, str(exc)
    else:
        probe_error = None
    out: dict = {"running": running, "mutex": mutex}
    if probe_error:
        out["probe_error"] = probe_error
        attention.append(f"recorder: the mutex probe failed: {probe_error}")
    elif not running:
        attention.append(f"recorder: not running (no holder of {mutex})")
    if cfg["paths"]["recorder_log"] is None:
        out["log"] = None
        attention.append("recorder: paths.recorder_log is not set, so its log and incidents are not read")
        return out
    log = Path(cfg["paths"]["recorder_log"])
    out["log"] = str(log)
    out["log_age_h"] = (round((now.timestamp() - log.stat().st_mtime) / 3600, 2) if log.is_file() else None)
    since = now - timedelta(days=d["incident_days"])
    counts: dict[str, int] = {}
    for row in models.read_jsonl(log.parent / d["incidents_file"]):
        try:
            at = datetime.fromisoformat(row["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if at.tzinfo is not None and at >= since:
            counts[row.get("kind", "?")] = counts.get(row.get("kind", "?"), 0) + 1
    out["incidents"] = counts
    serious = {k: n for k, n in counts.items() if k in d["attention_incidents"]}
    if serious:
        attention.append(f"recorder: {serious} in the last {d['incident_days']} day(s) (incidents.jsonl)")
    return out


def _liveness(cfg: dict, now: datetime, attention: list[str]) -> dict:
    if S.task_for(cfg, liveness.JOB) is None:
        return {"last": None, "skipped": "no task this machine runs is a liveness check (schedules.skip)"}
    checks = [r for r in models.read_jsonl(pipeline_run.files(cfg, "log"))
              if r.get("job") == liveness.JOB and r.get("event") == "check"]
    if not checks:
        attention.append("liveness: no check has run (is the whispr-liveness task registered?)")
        return {"last": None}
    last = checks[-1]
    age_min = round((_age_h(last["ts"], now) or 0) * 60, 1)
    relaunches = sum(r["outcome"] == liveness.RELAUNCHED for r in checks)
    if age_min > cfg["doctor"]["liveness_stale_min"]:
        attention.append(f"liveness: last check {age_min} min ago (more than {cfg['doctor']['liveness_stale_min']})")
    if last["outcome"] not in (liveness.ALIVE, liveness.RELAUNCHED):
        attention.append(f"liveness: the last check was {last['outcome']}: {last.get('detail', '')}".rstrip(": "))
    return {"last": last, "age_min": age_min, "relaunches_logged": relaunches}


def _reconciliation(cfg: dict, conn, attention: list[str]) -> dict:
    row = db.fetch_one(conn, "SELECT recorded_at, details FROM maintenance_log WHERE check_name = ?"
                             " ORDER BY recorded_at DESC, id DESC LIMIT 1", (daily.step_check(daily.RECONCILIATION),))
    if row is None:
        attention.append("reconciliation: no daily reconciliation has run yet")
        return {"at": None}
    details = json.loads(row["details"])
    problems = details.get("problems", [])
    for problem in problems:
        attention.append(f"reconciliation ({row['recorded_at']}): {problem}")
    if details.get("outcome") == pipeline_run.FAILED:
        attention.append(f"reconciliation ({row['recorded_at']}) failed: {details.get('error')}")
    return {"at": row["recorded_at"], "outcome": details.get("outcome"), "problems": problems,
            "recorder": details.get("recorder"), "pipeline": details.get("pipeline")}


def _notes(cfg: dict, conn, attention: list[str]) -> dict:
    heading = f"## {render.MY_ACTIONS}"
    missing = []
    for row in db.fetch_all(conn, "SELECT id FROM episode WHERE deleted_at IS NULL ORDER BY id"):
        path = write.note_path(cfg, row["id"])
        if not path.is_file() or heading not in path.read_text(encoding="utf-8", errors="replace"):
            missing.append(row["id"])
    if missing:
        attention.append(f"notes: {len(missing)} live episode(s) have no note with a {render.MY_ACTIONS} section")
    return {"missing_my_actions": len(missing), "ids": missing[:cfg["doctor"]["listed"]]}


def _quarantined(cfg: dict, conn) -> dict:
    held = State(conn, cfg).quarantined()
    return {"count": len(held), "refs": [r["ref"] for r in held][:cfg["doctor"]["listed"]]}


def report(cfg: dict, *, run: Optional[S.Runner] = None, recorder: Optional[liveness.Recorder] = None,
           now: Optional[datetime] = None) -> dict:
    """The status report: every section, and `attention`. Never raises for a section."""
    now = now or datetime.now(timezone.utc)
    attention: list[str] = []
    rep: dict = {"generated_at": now.isoformat()}

    def section(name: str, read: Callable[[], object]) -> None:
        try:
            rep[name] = read()
        except Exception as exc:
            rep[name] = {"error": pipeline_run._error(exc)}
            attention.append(f"{name}: could not be read: {pipeline_run._error(exc)}")

    try:
        conn = db.connect_readonly(cfg)
    except Exception as exc:
        conn = None
        rep["database"] = {"error": pipeline_run._error(exc)}
        attention.append(f"database: {pipeline_run._error(exc)}")
    with contextlib.closing(conn) if conn is not None else contextlib.nullcontext():
        if conn is not None:
            rep["database"] = {"path": str(db.database_path(cfg))}
            section("jobs", lambda: _jobs(cfg, conn, now, attention))
            section("alerts", lambda: _alerts(cfg, conn, attention))
        section("schedule", lambda: _schedule(cfg, run, attention))
        section("auth", lambda: _auth(cfg, attention))
        section("recorder", lambda: _recorder(cfg, recorder, now, attention))
        section("liveness", lambda: _liveness(cfg, now, attention))
        if conn is not None:
            section("reconciliation", lambda: _reconciliation(cfg, conn, attention))
            section("tasks", lambda: tasks.funnel_report(Store(conn, cfg), days=cfg["kg"]["tasks"]["funnel_days"],
                                                         now=now))
            section("notes", lambda: _notes(cfg, conn, attention))
            section("quarantined", lambda: _quarantined(cfg, conn))
    rep["attention"] = attention
    return rep


def _text(rep: dict) -> list[str]:
    lines = [f"whispr doctor, {rep['generated_at']}"]
    for name, value in rep.items():
        if name in ("generated_at", "attention"):
            continue
        lines.append(f"{name}: {json.dumps(value, default=str)}")
    if rep["attention"]:
        lines.append(f"NEEDS ATTENTION ({len(rep['attention'])}):")
        lines.extend(f"  - {item}" for item in rep["attention"])
    else:
        lines.append("all clear")
    return lines


def doctor(cfg: dict, *, as_json: bool = False, run: Optional[S.Runner] = None,
           recorder: Optional[liveness.Recorder] = None, now: Optional[datetime] = None,
           out: Callable[[str], None] = print) -> int:
    rep = report(cfg, run=run, recorder=recorder, now=now)
    if as_json:
        out(json.dumps(rep, indent=1, default=str))
    else:
        for line in _text(rep):
            out(line)
    return EXIT_FAILED if rep["attention"] else EXIT_OK
