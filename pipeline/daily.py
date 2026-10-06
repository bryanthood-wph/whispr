"""python -m pipeline daily: the one daily job (docs/plan/D-architecture-and-ops.md D.6,
docs/plan/C-knowledge-graph.md C.5; decision 2026-10-05).

The `whispr-daily` scheduled task starts it once a day. A run:

1. Takes its own single-instance lock (pipeline.files.daily_lock, the runner's OS
   lock), so it never waits on or blocks the 15-minute pipeline run; a second daily
   run exits 0 at once.
2. Refuses, with one alert and no step run, inside a nested Claude Code session
   (auth.refuse_if_set), exactly as the pipeline run does.
3. Runs three steps in order, each recorded as a `daily:<step>` maintenance_log row
   (outcome, error, what it did) and a log line:
   - **backup** (pipeline/backup.py): first, so the copy predates any repair. An
     unset backup.destination refuses the step with one setup:backup alert.
   - **maintenance** (C.5): entity resolution (kg.resolve.Resolver.run) with its
     "same entity?" calls bound to pipeline/calls.py as the kg.models.resolve role,
     cached under pipeline.files.resolve_cache and booked in the pipeline ledger
     (each call reserved there at its cap while in flight, run.reserve_call). A
     pair whose decision fails on its own (bad output, a failed merge, a call failure
     after another call answered) is recorded as failed and not asked again until it
     changes; the step goes on;
     then the integrity checks (kg/integrity.py) and the quarantine digest, which run
     whatever resolution did. Before every model call: no start past
     maintain.time_budget_s (elapsed since the run started, backup included, plus the
     slowest call so far), and run.spend_stop against maintain.cap_usd and the shared
     pipeline.run.daily_cap_usd (the ledger read again for each call, so a pipeline run
     at the same time counts) at maintain.max_budget_per_call_usd. Over a limit, resolution stops PARTIAL; what it
     decided is kept (each merge and decision commits on its own), so the next run
     resumes. A model-call failure raises the auth or model-unavailable alert.
   - **reconciliation** (pipeline/reconcile.py): transcripts vs the recorder's calls
     and the pipeline queue; its report is the step's row, which doctor reads.
   One failing step never skips the others, but the run then fails (State.finish_run
   with the error: its run-failed alert). Success means every step finished (L1).

Exit codes: 0 ok (or another daily run holds the lock), 1 a step failed or was refused,
3 partial (resolution stopped at a limit; nothing failed), 4 refused (nested session).
`--dry-run` reports what each step would do against an in-memory copy of the database
(kg.db.snapshot), with no model call, no backup written and nothing alerted.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from kg import db, integrity
from kg.resolve import Resolver
from kg.state import State
from kg.store import Store
from pipeline import backup as pipeline_backup
from pipeline import calls, models, reconcile
from pipeline import run as pipeline_run
from pipeline.run import EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, FAILED, LOCKED, OK, PARTIAL, REFUSED, TIME_BUDGET

JOB = "daily"                   # the kg.state job
COMMAND = "daily"               # its `python -m pipeline` subcommand
BACKUP, MAINTENANCE, RECONCILIATION = "backup", "maintenance", "reconciliation"
SETUP_BACKUP = pipeline_run.alert_key(pipeline_run.KIND_SETUP, BACKUP)     # backup.destination unset

# call(role, prompt, schema, before_call) -> the schema-valid structured output, cached.
# Production binds pipeline.calls.cached_call (default_resolve_call); tests pass a fake
# that calls before_call(key, max_budget_usd) on a "cache miss" as the real one does.
ResolveCall = Callable[[str, str, dict, Callable[[str, float], None]], calls.Cached]


def step_check(name: str) -> str:
    """A step's maintenance_log check name."""
    return f"{JOB}:{name}"


def default_resolve_call(cfg: dict) -> ResolveCall:
    ledger, cache = pipeline_run.files(cfg, "ledger"), pipeline_run.files(cfg, "resolve_cache")
    per_call = cfg["maintain"]["max_budget_per_call_usd"]
    return lambda role, prompt, schema, before_call: calls.cached_call(
        cfg, role, prompt, schema=schema, max_budget_usd=per_call, ledger=ledger, cache_dir=cache,
        before_call=before_call)


class _Stopped(Exception):
    """A limit stops resolution before the next call. args: (why)."""


class _Budget:
    """The resolution step's limits around each model call: `ask` is the Resolver's."""

    def __init__(self, cfg: dict, call: ResolveCall, now: datetime, clock: Callable[[], float], started: float):
        self.cfg, self.call, self.now, self.clock = cfg, call, now, clock
        self.started = started      # the run's start: a slow backup spends the same time budget
        self.spent = 0.0
        self.calls = 0
        self.answered = 0           # model calls that returned (not cache hits) in this run
        self.longest = 0.0

    def before_call(self, key: str, max_budget_usd: float) -> None:
        m = self.cfg["maintain"]
        if self.clock() - self.started + self.longest >= m["time_budget_s"]:
            raise _Stopped(TIME_BUDGET)
        reason = pipeline_run.reserve_call(
            self.cfg, job=JOB, key=key, per_call=max_budget_usd, now=self.now,
            check=lambda day_spent: pipeline_run.spend_stop(self.cfg, spent=self.spent, day_spent=day_spent,
                                                            per_call=max_budget_usd, cap=m["cap_usd"]))
        if reason:
            raise _Stopped(reason)
        self.calls += 1

    def ask(self, role: str, prompt: str, schema: dict) -> dict:
        t0, launched = self.clock(), self.calls
        try:
            got = self.call(role, prompt, schema, self.before_call)
        except _Stopped:
            raise
        except Exception:
            if self.calls > launched:       # a failed call may still have spent: count its cap
                self.spent += self.cfg["maintain"]["max_budget_per_call_usd"]
            raise
        finally:
            self.longest = max(self.longest, self.clock() - t0)
        self.spent += got.cost_usd
        self.answered += not got.cached     # a cache hit proves nothing about the model
        return got.output

    def pair_error(self, exc: Exception) -> bool:
        """Resolver.run's pair_error: is this failure the pair's own? A limit stop and an
        auth failure never are; any other model-call failure is only once a call has
        answered in this run, as in pipeline/run.py (before that it is most likely the
        machine's: signed out, offline, no CLI, an outage). Bad output or a failed
        merge always is."""
        if isinstance(exc, (_Stopped, models.AuthError)):
            return False
        if isinstance(exc, models.ModelCallError):
            return self.answered > 0
        return True


@dataclass
class Step:
    name: str
    outcome: str = OK
    error: Optional[str] = None
    stopped: Optional[str] = None
    found: int = 0
    repaired: int = 0
    escalated: int = 0
    details: dict = field(default_factory=dict)


@dataclass
class _Context:
    cfg: dict
    conn: object
    store: Store
    state: State
    run_id: str
    now: datetime
    clock: Callable[[], float]
    resolve_call: ResolveCall
    calendar: Optional[reconcile.Calendar]
    started: float = 0.0            # clock() at the run's start
    budget: Optional[_Budget] = None


def _join(*errors: Optional[str]) -> Optional[str]:
    return "; ".join(e for e in errors if e) or None


def _backup(ctx: _Context) -> Step:
    try:
        made = pipeline_backup.backup(ctx.cfg, ctx.conn, now=ctx.now)
    except pipeline_backup.NotConfigured as exc:
        ctx.state.raise_alert(pipeline_run.KIND_SETUP, SETUP_BACKUP,
                              f"{JOB} backup refused: {exc}.", pipeline_backup.SETUP_FIX, now=ctx.now)
        return Step(BACKUP, REFUSED, error=str(exc))
    cleared = pipeline_run.clear_alerts(ctx.state, [SETUP_BACKUP], now=ctx.now)      # it is configured now
    return Step(BACKUP, found=made["transcripts"]["count"], repaired=len(made["pruned"]),
                details={"path": made["path"], "pruned": made["pruned"], "database": made["database"],
                         "transcripts": made["transcripts"], "include": made["include"], "cleared": cleared})


def _maintain(ctx: _Context) -> Step:
    cfg, state, step = ctx.cfg, ctx.state, Step(MAINTENANCE)
    ctx.budget = budget = _Budget(cfg, ctx.resolve_call, ctx.now, ctx.clock, ctx.started)
    try:
        summary = Resolver(ctx.store, state).run(ctx.run_id, budget.ask, now=ctx.now, pair_error=budget.pair_error)
        step.found, step.repaired = summary["candidates"], len(summary["merged"])
        step.escalated = len(summary["unsure"]) + len(summary["failed"])
        step.details["resolve"] = {k: len(v) if isinstance(v, list) else v for k, v in summary.items()}
    except _Stopped as stop:
        step.stopped = stop.args[0]
        step.details["resolve"] = {"stopped": step.stopped}
    except models.ModelCallError as exc:
        why = pipeline_run.model_alert(cfg, state, JOB, ctx.run_id, exc, now=ctx.now,
                                       resumes="the next daily run resumes entity resolution where this one stopped.")
        step.error = f"entity resolution stopped ({why}): {pipeline_run._error(exc)}"
    except Exception as exc:
        step.error = f"entity resolution failed: {pipeline_run._error(exc)}"
    step.details["calls"], step.details["spent_usd"] = budget.calls, round(budget.spent, 6)
    try:                                         # whatever resolution did
        checked = integrity.check(ctx.store, state, ctx.run_id, listed=cfg["maintain"]["listed"], now=ctx.now)
        step.details["integrity"] = {"ok": checked["ok"], "problems": checked["problems"],
                                     "repaired": len(checked["repaired"]), "orphans": checked["orphans"]["count"]}
        if not checked["ok"]:
            step.error = _join(step.error, "database integrity: " + "; ".join(checked["problems"]))
    except Exception as exc:
        step.error = _join(step.error, f"integrity checks failed: {pipeline_run._error(exc)}")
    try:
        step.details["quarantine_digest"] = len(state.quarantine_digest(ctx.now))
    except Exception as exc:
        step.error = _join(step.error, f"quarantine digest failed: {pipeline_run._error(exc)}")
    step.outcome = FAILED if step.error else PARTIAL if step.stopped else OK
    return step


def _reconcile(ctx: _Context) -> Step:
    rep = reconcile.reconcile(ctx.cfg, ctx.store, ctx.state, job=JOB, now=ctx.now, calendar=ctx.calendar)
    return Step(RECONCILIATION, found=len(rep["problems"]), repaired=len(rep["episodes"]["tombstoned"]),
                escalated=len(rep["problems"]), details=rep)


# The steps, in order: backup first, so the copy predates any repair.
STEPS = ((BACKUP, _backup), (MAINTENANCE, _maintain), (RECONCILIATION, _reconcile))


def _locked(cfg: dict, conn, *, resolve_call: Optional[ResolveCall], calendar: Optional[reconcile.Calendar],
            now: datetime, clock: Callable[[], float], log: Callable[[dict], None], out: Callable[[str], None]) -> int:
    started = clock()
    store, state = Store(conn, cfg), State(conn, cfg)
    refused = pipeline_run.refuse_nested(cfg, state, JOB, COMMAND, now=now, log=log, out=out)
    if refused is not None:
        return refused
    run_id = state.begin_run(JOB, now)
    ctx = _Context(cfg, conn, store, state, run_id, now, clock, resolve_call or default_resolve_call(cfg), calendar,
                   started=started)
    steps = []
    for name, run_step in STEPS:
        t0 = clock()
        try:
            step = run_step(ctx)
        except Exception as exc:                # one failing step never skips the others
            step = Step(name, FAILED, error=pipeline_run._error(exc))
        record = {"outcome": step.outcome, "error": step.error, "stopped": step.stopped, **step.details}
        try:
            state.log_maintenance(run_id, step_check(name), found=step.found, repaired=step.repaired,
                                  escalated=step.escalated, details=json.dumps(record, default=str), now=now)
        except Exception as exc:
            step.outcome, step.error = FAILED, _join(step.error, f"not recorded: {pipeline_run._error(exc)}")
        log({"event": "step", "run": run_id, "step": name, "outcome": step.outcome, "error": step.error,
             "stopped": step.stopped, "seconds": round(clock() - t0, 3)})
        out(f"{JOB} {name}: {step.outcome}" + (f" ({step.stopped})" if step.stopped else "")
            + (f": {step.error}" if step.error else ""))
        steps.append(step)
    bad = [s for s in steps if s.outcome in (FAILED, REFUSED)]
    stopped = next((s.stopped for s in steps if s.stopped), None)
    error = _join(*(f"{s.name} {s.outcome}: {s.error}" for s in bad))
    spent, made = (ctx.budget.spent, ctx.budget.calls) if ctx.budget else (0.0, 0)
    for name, value in (("spent_usd", spent), ("calls", made), ("partial", float(stopped is not None)),
                        ("steps_failed", len(bad))):
        state.record_metric(run_id, name, value)
    ok = state.finish_run(run_id, processed=len(steps) - len(bad), eligible=len(steps), backlog=0, error=error,
                          now=now)
    outcome, code = ((FAILED, EXIT_FAILED) if bad or not ok else (PARTIAL, EXIT_PARTIAL) if stopped
                     else (OK, EXIT_OK))
    log({"event": "run", "run": run_id, "outcome": outcome, "exit": code, "steps": {s.name: s.outcome for s in steps},
         "calls": made, "spent_usd": round(spent, 6), "stopped": stopped, "error": error,
         "seconds": round(clock() - started, 3)})
    out(f"{JOB} run {outcome}" + (f", stopped: {stopped}" if stopped else "") + (f", error: {error}" if error else ""))
    return code


def _dry_run(cfg: dict, conn, *, calendar: Optional[reconcile.Calendar], now: datetime,
             out: Callable[[str], None]) -> int:
    try:
        dest = pipeline_backup.destination(cfg)
        count = len(pipeline_run.transcripts(cfg)) if cfg["backup"]["transcripts"] else 0
        out(f"backup: would copy the database, {count} transcript(s) and {len(cfg['backup']['include'])} include "
            f"path(s) to {dest}\\{cfg['backup']['prefix']}<stamp>, keeping the newest {cfg['backup']['keep']}")
    except pipeline_backup.NotConfigured as exc:
        out(f"backup: would refuse: {exc}. {pipeline_backup.SETUP_FIX}")
    except Exception as exc:
        out(f"backup: would fail: {pipeline_run._error(exc)}")
    store, state = Store(conn, cfg), State(conn, cfg)
    run_id = state.begin_run(JOB, now)          # in the in-memory copy only
    found = Resolver(store, state).candidates()
    certain = sum(c.certain for c in found)
    m = cfg["maintain"]
    out(f"maintenance: {len(found)} entity-resolution candidate(s), {certain} certain (merged by rule), at most "
        f"{len(found) - certain} model decision(s) (pairs decided before and unchanged are skipped), at most "
        f"${m['max_budget_per_call_usd']:.2f} each, stopping at ${m['cap_usd']:.2f} this run")
    checked = integrity.check(store, state, run_id, listed=m["listed"], now=now)
    out(f"maintenance: integrity {'ok' if checked['ok'] else 'PROBLEMS: ' + '; '.join(checked['problems'])}, "
        f"{len(checked['repaired'])} dangling reference(s) to repair, {checked['orphans']['count']} orphan(s)")
    rep = reconcile.reconcile(cfg, store, state, job=JOB, now=now, calendar=calendar, dry_run=True)
    gone = rep["episodes"]["gone"]
    out(f"reconciliation: recorder queued {rep['recorder']['queued']}, wrote {rep['recorder']['written']}; "
        f"{len(rep['pipeline']['unpicked'])} transcript(s) not picked up; {len(gone)} episode(s) whose transcript "
        f"is gone" + ("" if rep["episodes"]["blocked"] or not gone else " (would be tombstoned)"))
    for problem in rep["problems"]:
        out(f"reconciliation: problem: {problem}")
    out("dry run: no model call was made; nothing was written or alerted")
    return EXIT_OK


def daily(cfg: dict, *, dry_run: bool = False, resolve_call: Optional[ResolveCall] = None,
          calendar: Optional[reconcile.Calendar] = None, now: Optional[datetime] = None,
          clock: Callable[[], float] = time.monotonic, out: Callable[[str], None] = print) -> int:
    """One daily run; returns its exit code. `resolve_call`, `calendar` (default:
    reconcile.outlook_reader), `now` and `clock` are for tests."""
    started = clock()
    now = now or datetime.now(timezone.utc)
    calendar = calendar if calendar is not None else reconcile.outlook_reader(cfg)
    log = pipeline_run.job_log(cfg, JOB)        # never called by a dry run: it writes nothing
    try:
        if dry_run:
            with contextlib.closing(db.snapshot(cfg)) as conn:
                return _dry_run(cfg, conn, calendar=calendar, now=now, out=out)
        with pipeline_run.single_instance(pipeline_run.files(cfg, "daily_lock")) as held:
            if not held:
                log({"event": "run", "outcome": LOCKED, "exit": EXIT_OK, "seconds": round(clock() - started, 3)})
                out(f"another {JOB} run holds the lock; exiting")
                return EXIT_OK
            with contextlib.closing(db.connect(cfg)) as conn:
                return _locked(cfg, conn, resolve_call=resolve_call, calendar=calendar, now=now, clock=clock,
                               log=log, out=out)
    except Exception as exc:                    # before the run could be recorded (database, lock folder)
        if not dry_run:
            log({"event": "run", "outcome": FAILED, "exit": EXIT_FAILED, "error": pipeline_run._error(exc)})
        out(f"{JOB} run failed: {pipeline_run._error(exc)}")
        return EXIT_FAILED
