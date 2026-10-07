"""python -m pipeline run: the per-call runner (docs/plan/D-architecture-and-ops.md D.1,
D.6; decision 2026-10-05: a 15-minute poll, not a recorder handoff).

The `pipeline` scheduled task starts it at logon and every 15 minutes. A run:

1. Takes the single-instance lock: an OS lock on pipeline.files.lock (msvcrt.locking
   on Windows, flock elsewhere), which the OS drops when the holder exits, however it
   dies, so a crashed run can never leave it held. A second run finds it held and exits
   0 at once with a log line. Everything below happens under the lock, begin_run
   included (it fails any run of this job still marked running).
2. Refuses, with one alert and no model call, while an auth.refuse_if_set variable is
   set (a nested Claude Code session, whose calls die with zero tokens: lesson L3).
3. Discovers transcripts dated (by file name) on or after pipeline.run.process_since;
   while that is unset it refuses with one alert, so the first run never takes the
   whole history unasked. Older transcripts are queued only by `python -m pipeline
   backfill`, which quotes their count and cost and queues nothing without --yes. A
   transcript with no episode is enqueued; one whose sha256 differs from its episode's
   (edited, re-transcribed, or fixed after quarantine) is requeued with fresh attempts.
   An unchanged one costs a hash and nothing else.
4. Works the due queue newest call first, one item per transcript: prepare -> extract (one
   model call, skipped for a stub) -> write (pipeline/write.py). A failed item is
   retried after pipeline.run.retry_backoff_min and quarantined at pipeline.max_attempts
   with one alert and an "Unavailable" note; the queue moves on either way (L7); items
   that failed before go after fresh ones. An auth failure is the machine's, not the
   item's: the attempt is given back, an alert is raised and the run stops.
   Any other model-call failure counts against the item only if a model call earlier in
   the same run succeeded; otherwise (signed out, offline, no CLI, an outage) it is
   handled like auth, with one model-unavailable alert, so an outage never burns the
   queue's attempts. Extract and schema failures always count. `python -m pipeline
   requeue <ref|--all-quarantined>` puts quarantined items back after a fix.
5. Stops early, PARTIAL (exit 3), before the time budget, the per-run item limit or a
   spend cap would be passed; the rest stay queued for the next run (L6, L3).
6. Finishes the run in kg.state: success means progress, or proof that nothing was
   eligible (L1). An item counts as processed when it leaves the queue (written, or
   quarantined with its alert); the backlog is recorded every run.

Every item and every run appends one JSON line to pipeline.files.log. `--dry-run`
discovers and plans with zero model calls against an in-memory copy of the database
(kg.db.snapshot: the file is opened read-only, never created or migrated) and prints
to stdout, writing nothing under paths.data_dir; `--once` runs one transcript now,
whatever its queue state (troubleshooting).
"""

from __future__ import annotations

import contextlib
import os
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional

from kg import db
from kg.state import KIND_QUARANTINE, QUARANTINED, QUEUED, State, alert_key, item_key
from kg.store import Store
from pipeline import calls, extract, models, prepare, write
from pipeline.config import ConfigError, data_dir
from pipeline.jsonschema_lite import validate

if os.name == "nt":
    import msvcrt
else:
    import fcntl

JOB = "pipeline"                # the kg.state job
COMMAND = "run"                 # its `python -m pipeline` subcommand
STAGE = "transcript"            # one queue item per transcript; its ref is the episode id

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PARTIAL, EXIT_REFUSED = 0, 1, 2, 3, 4
# Run outcomes, as logged.
OK, PARTIAL, FAILED, LOCKED, REFUSED = "ok", "partial", "failed", "locked", "refused"
# Item outcomes, as logged.
SUMMARIZED, NO_CONTENT, RETRY, QUARANTINE, REQUEUED = "summarized", "no-content", "retry", "quarantined", "requeued"
# Why a run stopped early.
MAX_ITEMS, TIME_BUDGET, RUN_CAP, DAILY_CAP = "max items", "time budget", "run spend cap", "daily spend cap"
AUTH, UNAVAILABLE = "auth", "model unavailable"
# A pipeline-ledger row booking a call in flight at its full cap (reserve_call).
RESERVE = "reserve"
# What discovery found for a transcript.
NEW, CHANGED, PENDING, HELD, UNCHANGED = "new", "changed", "queued", "quarantined", "unchanged"

KIND_REFUSED = "refused-env"
KIND_AUTH = "auth"
KIND_UNAVAILABLE = "model-unavailable"
KIND_SETUP = "setup"
SETUP_FIX = ("Set pipeline.run.process_since in the config overlay to an ISO date (YYYY-MM-DD; /whispr-setup sets "
             "the install date). Transcripts dated before it are processed only by `python -m pipeline backfill`.")

# ask(prepared episode) -> the schema-valid extract. Production binds extract.extract to
# the run's ledger and cache (default_ask); tests pass a plain function.
Ask = Callable[[prepare.Prepared], calls.Cached]


class _Systemic(Exception):
    """A failure of the machine, not the item: stop the run, keep the item's attempts.
    args: (why the run stopped, the error)."""


def files(cfg: dict, key: str) -> Path:
    """A pipeline.files path under paths.data_dir, its folder created."""
    path = data_dir(cfg) / cfg["pipeline"]["files"][key]
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def default_ask(cfg: dict) -> Ask:
    p = cfg["pipeline"]
    ledger, cache = files(cfg, "ledger"), files(cfg, "cache")

    def reserve(key: str, per_call: float) -> None:
        reserve_call(cfg, job=JOB, key=key, per_call=per_call)     # stop_reason checked the caps just before
    return lambda prep: extract.extract(prep, cfg, role=extract.ROLE, max_budget_usd=p["max_budget_per_call_usd"],
                                        ledger=ledger, cache_dir=cache, before_call=reserve)


# ---- the single-instance lock -------------------------------------------------------

def _try_lock(fh: BinaryIO) -> bool:
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: BinaryIO) -> None:
    if os.name == "nt":
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def single_instance(path: Path) -> Iterator[bool]:
    """True inside the block if this process holds the lock on `path`, False (at once,
    no waiting) if another does. The lock is the OS's, not the file's existence: it
    goes when the file closes or the process exits, so a stale file means nothing."""
    with open(path, "a+b") as fh:
        held = _try_lock(fh)
        try:
            yield held
        finally:
            if held:
                _unlock(fh)


# ---- discovery ----------------------------------------------------------------------

def transcripts(cfg: dict) -> list[Path]:
    """Every transcript in paths.transcripts, newest first (the recorder's atomic-write
    temp files, dot-prefixed, excluded). A missing folder is an error, never a proof of
    zero (L1)."""
    folder = Path(cfg["paths"]["transcripts"])
    if not folder.is_dir():
        raise FileNotFoundError(f"transcripts folder {folder} does not exist (paths.transcripts)")
    return sorted((p for p in folder.glob("*.md") if not p.name.startswith(".")), reverse=True)


def transcript_date(path: Path) -> Optional[date]:
    """The call's date from the file name (the recorder's YYYY-MM-DD-HHMM-<slug>.md), or None."""
    try:
        return date.fromisoformat(Path(path).stem[:10])
    except ValueError:
        return None


def in_window(path: Path, since: Optional[date], until: Optional[date] = None) -> bool:
    """True for a transcript dated since..until, both inclusive and optional. An undated one is never in."""
    d = transcript_date(path)
    return d is not None and (since is None or d >= since) and (until is None or d <= until)


def process_since(cfg: dict) -> date:
    """pipeline.run.process_since as a date; ValueError when unset or not an ISO date."""
    value = cfg["pipeline"]["run"]["process_since"]
    if value is None:
        raise ValueError("pipeline.run.process_since is not set, so a run would take every historical transcript")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"pipeline.run.process_since {value!r} is not an ISO date (YYYY-MM-DD)") from None


def work_order(items: list[dict]) -> list[dict]:
    """Newest call first (a ref starts with the call's date and time), so a new call
    never waits behind a backlog; items that failed before go last, so one whose model
    call keeps failing can't stop every run before the rest of the queue is tried."""
    newest = sorted(items, key=lambda item: item["ref"], reverse=True)
    return sorted(newest, key=lambda item: item["last_error"] is not None)


def transcript_path(cfg: dict, ref: str) -> Path:
    return Path(cfg["paths"]["transcripts"]) / f"{ref}.md"


def classify(store: Store, state: State, path: Path) -> tuple[str, Optional[dict]]:
    """(what discovery found, the transcript's queue item or None)."""
    ref = write.episode_id(path)
    ep, item = store.episode(ref), state.find(STAGE, ref)
    if ep is not None and ep["deleted_at"] is None and ep["sha256"] == prepare.file_sha256(path):
        return (PENDING if item and item["status"] == QUEUED else UNCHANGED), item
    if item is None:
        return NEW, None
    if item["status"] == QUEUED:
        return PENDING, item
    if item["status"] == QUARANTINED and ep is None:
        return HELD, item           # quarantined before its episode could be written
    return CHANGED, item


def resolve_once(cfg: dict, value: str) -> Optional[Path]:
    """`--once`'s transcript: a path, file name or stem of a transcript in paths.transcripts."""
    folder = Path(cfg["paths"]["transcripts"]).resolve()
    for candidate in (Path(value), folder / value, folder / f"{value}.md"):
        if candidate.is_file() and candidate.suffix == ".md" and candidate.resolve().parent == folder:
            return transcript_path(cfg, candidate.stem)
    return None


def discover(store: Store, state: State, paths: list[Path], now: datetime) -> Counter:
    """Enqueue each new transcript and requeue each changed one; what was found, counted."""
    found: Counter = Counter()
    for path in paths:
        kind, item = classify(store, state, path)
        if kind == NEW:
            state.enqueue(STAGE, write.episode_id(path), now)
        elif kind == CHANGED:
            state.requeue(item["id"], now)
        found[kind] += 1
    return found


def spent_since(cfg: dict, since: datetime) -> float:
    """Model spend in the pipeline ledger since `since` (the daily cap's window): every
    job's call rows, plus each reservation (RESERVE: a call in flight, or one killed
    before its row was written) that no call row with its request key has settled yet,
    at its full cap. So spend errs high, never low."""
    total, pending = 0.0, {}
    for row in models.read_jsonl(files(cfg, "ledger")):
        ts = prepare.parse_iso(row.get("ts"))
        if ts is None or ts.tzinfo is None or ts < since:
            continue
        key = row.get("request_key")
        if row.get("event") == RESERVE:
            pending.setdefault(key, []).append(float(row.get("reserve_usd") or 0))
            continue
        total += float(row.get("cost_usd") or 0)
        if pending.get(key):
            pending[key].pop(0)                 # the call row settles its reservation
    return total + sum(sum(q) for q in pending.values())


def spend_stop(cfg: dict, *, spent: float, day_spent: float, per_call: float, cap: float) -> Optional[str]:
    """RUN_CAP or DAILY_CAP when one more call, counted at its full per-call cap, could
    pass the run's own `cap` (over `spent`, this run's) or pipeline.run.daily_cap_usd
    over `day_spent` (spent_since read now: the ledger's last 24 hours, where every
    job's calls, this run's included, and every call in flight are booked); else None
    (lesson L3). Every job that spends checks this before each call, with day_spent
    read fresh, so two jobs running at once see each other."""
    if spent + per_call > cap:
        return RUN_CAP
    if day_spent + per_call > cfg["pipeline"]["run"]["daily_cap_usd"]:
        return DAILY_CAP
    return None


def reserve_call(cfg: dict, *, job: str, key: str, per_call: float, now: Optional[datetime] = None,
                 check: Optional[Callable[[float], Optional[str]]] = None) -> Optional[str]:
    """A before_call guard step, under models.JSONL_LOCK (as the eval's budget guard):
    read the 24 hours' spend before `now` (default: the clock) fresh (spent_since), and if `check(day_spent)` names a
    stop reason return it; otherwise book a RESERVE row for this call at its full cap,
    which its call row settles, and return None. Within one process the lock makes the
    read and the booking one step; between processes (the daily job and the 15-minute
    run) each still sees the other's calls in flight, and the window between one's read
    and its booking is the only overlap left."""
    now = now or datetime.now(timezone.utc)
    with models.JSONL_LOCK:
        if check is not None:
            reason = check(spent_since(cfg, now - timedelta(days=1)))
            if reason:
                return reason
        models.append_jsonl(files(cfg, "ledger"), {"event": RESERVE, "ts": db.utc_now(now), "job": job,
                                                   "request_key": key, "reserve_usd": per_call})
    return None


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


# ---- shared by every job's runner (run, daily, liveness) ----------------------------

def job_log(cfg: dict, job: str) -> Callable[[dict], None]:
    """log(record): one JSON line, stamped and tagged with `job`, in pipeline.files.log."""
    def log(record: dict) -> None:
        models.append_jsonl(files(cfg, "log"), {"ts": db.utc_now(), "job": job, **record})
    return log


def task_name(cfg: dict, command: str) -> str:
    """The configured scheduled task that runs `-m pipeline <command>`, for messages
    that tell you to start it; a generic phrase when none is configured."""
    from pipeline import schedule      # deferred: schedule imports __main__, which imports this module
    try:      # a message's hint: a bad schedules.skip must not turn a refusal into a crash
        name = schedule.task_for(cfg, command)
    except ConfigError:
        name = None
    return name or f"<the task running `-m pipeline {command}`>"


def clear_alerts(state: State, keys: list[str], *, now: datetime) -> list[str]:
    """The cause behind these alerts is gone (the check that raised one has passed):
    acknowledge each that is open. Returns the keys closed. A recurrence reopens one
    (State.raise_alert), so closing never hides a problem that comes back."""
    return state.acknowledge_open(keys, now=now)


def refuse(state: State, job: str, kind: str, message: str, fix: str, *, now: datetime,
           log: Callable[[dict], None], out: Callable[[str], None]) -> int:
    """Refuse a run of `job`: one alert (dedupe key kind:job), a log line, EXIT_REFUSED."""
    state.raise_alert(kind, alert_key(kind, job), message, fix, now=now)
    log({"event": "run", "outcome": REFUSED, "exit": EXIT_REFUSED, "error": message})
    out(message)
    return EXIT_REFUSED


def refuse_nested(cfg: dict, state: State, job: str, command: str, *, now: datetime,
                  log: Callable[[dict], None], out: Callable[[str], None]) -> Optional[int]:
    """While an auth.refuse_if_set variable is set (a nested Claude Code session, where
    a model call dies at once with zero tokens: lesson L3), refuse() the run of `job`
    (the subcommand `command`) and return EXIT_REFUSED; else close the job's
    refused-env alert (this run is not nested) and return None."""
    refused = models.refused_env(cfg)
    if not refused:
        clear_alerts(state, [alert_key(KIND_REFUSED, job)], now=now)
        return None
    return refuse(state, job, KIND_REFUSED, f"{job} run refused: {refused[0]} is set, so this is a nested Claude "
                                            "Code session, where a model call dies at once with zero tokens.",
                  f"Let the scheduled task run it (Start-ScheduledTask -TaskName {task_name(cfg, command)}), or "
                  "run it from a terminal outside Claude Code; --dry-run is safe anywhere.", now=now, log=log, out=out)


def model_alert(cfg: dict, state: State, job: str, run_id: str, exc: models.ModelCallError, *, resumes: str,
                now: datetime) -> str:
    """Raise the one alert for a model-call failure that is the machine's, not an
    item's (signed out, an API key preferred, offline, no CLI, an outage):
    auth:<job> or model-unavailable:<job>; `resumes` says what happens next. Returns
    why the run stopped (AUTH or UNAVAILABLE)."""
    if isinstance(exc, models.AuthError):
        kind, why, fix = KIND_AUTH, AUTH, ("Sign in to Claude Code with the approved login (auth.approved_sources) "
                                           "and remove any API key the CLI would prefer")
    else:
        kind, why, fix = KIND_UNAVAILABLE, UNAVAILABLE, (
            f"Check that `{cfg['cli']['executable']}` is installed and signed in and that this machine "
            "is online (the error above says which failed)")
    state.raise_alert(kind, alert_key(kind, job), f"{job} run {run_id} stopped: {_error(exc)}", f"{fix}; {resumes}",
                      now=now)
    return why


def clear_model_alerts(state: State, job: str, *, now: datetime) -> list[str]:
    """A model call answered in a run of `job` that raised no model_alert: the machine
    can reach the model again, so close its auth and model-unavailable alerts."""
    return clear_alerts(state, [alert_key(KIND_AUTH, job), alert_key(KIND_UNAVAILABLE, job)], now=now)


# ---- a run --------------------------------------------------------------------------

class _Run:
    def __init__(self, cfg: dict, conn, *, ask: Ask, now: datetime, clock: Callable[[], float],
                 log: Callable[[dict], None]):
        self.cfg, self.ask, self.now, self.clock, self.log = cfg, ask, now, clock, log
        self.store, self.state = Store(conn, cfg), State(conn, cfg)
        self.started = clock()
        self.counts: Counter = Counter()
        self.spent = 0.0
        self.longest = 0.0          # the slowest item so far, the time budget's estimate of the next
        self.model_answered = False  # a model call returned in this run: a later call failure is the item's
        self.version = extract.version(cfg)
        self.schema = extract.load_schema(cfg)
        self.run_id = ""

    def discover(self, paths: list[Path]) -> None:
        self.counts.update(discover(self.store, self.state, paths, self.now))

    def stop_reason(self, started_items: int) -> Optional[str]:
        """Why the next item must not start, or None. Spend is checked as if the next
        item makes a call at its full per-call cap."""
        r = self.cfg["pipeline"]["run"]
        if started_items >= r["max_items"]:
            return MAX_ITEMS
        if self.clock() - self.started + self.longest >= r["time_budget_s"]:
            return TIME_BUDGET
        return spend_stop(self.cfg, spent=self.spent, day_spent=spent_since(self.cfg, self.at() - timedelta(days=1)),
                          per_call=self.cfg["pipeline"]["max_budget_per_call_usd"], cap=r["cap_usd"])

    def work(self, items: list[dict]) -> tuple[Optional[str], Optional[str]]:
        """(why the run stopped early or None, a systemic error or None)."""
        for n, item in enumerate(items):
            reason = self.stop_reason(n)
            if reason:
                return reason, None
            try:
                self.one(item)
            except _Systemic as exc:
                return exc.args
        return None, None

    def at(self) -> datetime:
        """The time now, as the run's clock tells it: `now` (the run's start) plus the time
        elapsed, so a backoff or the daily window is not measured from a start hours ago."""
        return self.now + timedelta(seconds=self.clock() - self.started)

    def retry_at(self, attempts: int) -> datetime:
        schedule = self.cfg["pipeline"]["run"]["retry_backoff_min"]
        return self.at() + timedelta(minutes=schedule[min(attempts, len(schedule)) - 1])

    def one(self, item: dict) -> None:
        t0 = self.clock()
        path = transcript_path(self.cfg, item["ref"])
        rec = {"event": "item", "run": self.run_id, "ref": item["ref"], "attempt": item["attempts"] + 1}
        try:
            if not self.state.begin_item(self.run_id, item["id"], self.now):
                self.quarantined(path, rec, "its attempts were used up (the last never reported back)")
                return
            try:
                rec.update(self.attempt(path))
                self.state.item_done(item["id"], self.now)
            except models.ModelCallError as exc:
                # Auth is always the machine's. Any other call failure is the item's only
                # if the model answered earlier in this run; before that it is most likely
                # the machine's (signed out, offline, no CLI, an outage), and counting it
                # would quarantine the whole queue within max_attempts runs.
                if isinstance(exc, models.AuthError) or not self.model_answered:
                    raise self.systemic(item, rec, exc) from exc
                self.failed(item, path, rec, exc)
            except Exception as exc:            # this item's failure: one bad item never blocks the queue
                self.failed(item, path, rec, exc)
        finally:
            rec["seconds"] = round(self.clock() - t0, 3)
            self.longest = max(self.longest, rec["seconds"])
            self.counts[rec.get("outcome")] += 1
            self.log(rec)

    def failed(self, item: dict, path: Path, rec: dict, exc: Exception) -> None:
        """Count the failure against the item: a retry after backoff, or quarantine."""
        rec["error"], retry = _error(exc), self.retry_at(rec["attempt"])
        if self.state.item_failed(item["id"], rec["error"], self.now, retry_at=retry) == QUARANTINED:
            self.quarantined(path, rec, rec["error"])
        else:
            rec.update(outcome=RETRY, retry_at=db.utc_now(retry))

    def systemic(self, item: dict, rec: dict, exc: models.ModelCallError) -> _Systemic:
        """The machine's failure: give the attempt back and raise its one alert; the
        caller raises what this returns, which stops the run."""
        self.state.undo_attempt(item, _error(exc), self.now)
        why = model_alert(self.cfg, self.state, JOB, self.run_id, exc,
                          resumes="the queue resumes on the next run, no attempt used.", now=self.now)
        rec.update(outcome=REQUEUED, error=_error(exc))
        return _Systemic(why, _error(exc))

    def attempt(self, path: Path) -> dict:
        """prepare -> extract -> write for one transcript; the log fields it adds."""
        t = prepare.parse(path)
        prep = prepare.prepare([t], self.cfg)
        if prep.is_stub:
            out = write.write_stub(self.store, self.cfg, t, prep)
            return {"outcome": NO_CONTENT, "words": prep.words, "note": str(out.note)}
        self.counts["calls"] += 1
        try:
            got = self.ask(prep)
        except Exception:                       # a failed call may still have spent: count its cap
            self.spent += self.cfg["pipeline"]["max_budget_per_call_usd"]
            raise
        self.spent += got.cost_usd
        self.model_answered = self.model_answered or not got.cached     # a cache hit proves nothing about the model
        errors = validate(got.output, self.schema)
        if errors:
            raise extract.ExtractError("extract output failed its schema: " + "; ".join(errors[:5]))
        out = write.write_summary(self.store, self.cfg, t, prep, got.output, extractor_version=self.version)
        return {"outcome": SUMMARIZED, "words": prep.words, "cached": got.cached, "cost_usd": got.cost_usd,
                "tasks": out.tasks, "confirm": out.confirm, "facts": out.facts, "edges": out.edges,
                "edges_skipped": out.edges_skipped, "low_mic": out.low_mic, "note": str(out.note)}

    def quarantined(self, path: Path, rec: dict, why: str) -> None:
        rec.update(outcome=QUARANTINE, error=why)
        try:
            out = write.write_unavailable(self.store, self.cfg, path)
            rec["note"] = str(out.note) if out else None
        except Exception as exc:                # the quarantine alert stands either way
            rec["note_error"] = _error(exc)

    def processed(self) -> int:
        return self.counts[SUMMARIZED] + self.counts[NO_CONTENT] + self.counts[QUARANTINE]

    def finish(self, eligible: int, stop: Optional[str], error: Optional[str]) -> tuple[str, int, int]:
        """Record and close the run: (outcome, exit code, backlog)."""
        processed = self.processed()
        if error is None and stop and processed == 0 and eligible:
            error = f"stopped before any item: {stop}"
        for name, value in (("spent_usd", self.spent), ("calls", self.counts["calls"]),
                            ("partial", float(stop is not None)), ("quarantined", self.counts[QUARANTINE]),
                            ("retried", self.counts[RETRY])):
            self.state.record_metric(self.run_id, name, value)
        if self.model_answered and stop not in (AUTH, UNAVAILABLE):      # those stops raised a model alert
            clear_model_alerts(self.state, JOB, now=self.now)
        backlog = self.state.backlog(STAGE)
        ok = self.state.finish_run(self.run_id, processed=processed, eligible=eligible, backlog=backlog,
                                   error=error, now=self.now)
        if not ok:
            return FAILED, EXIT_FAILED, backlog
        return (PARTIAL, EXIT_PARTIAL, backlog) if stop else (OK, EXIT_OK, backlog)


def _locked_run(cfg: dict, conn, *, once: Optional[Path], ask: Optional[Ask], now: datetime,
                clock: Callable[[], float], log: Callable[[dict], None], out: Callable[[str], None]) -> int:
    state = State(conn, cfg)
    refused = refuse_nested(cfg, state, JOB, COMMAND, now=now, log=log, out=out)
    if refused is not None:
        return refused
    since = None
    if once is None:                            # --once names its transcript: no date window
        try:
            since = process_since(cfg)
        except ValueError as exc:
            return refuse(state, JOB, KIND_SETUP, f"{JOB} run refused: {exc}.", SETUP_FIX, now=now, log=log, out=out)
        clear_alerts(state, [alert_key(KIND_SETUP, JOB)], now=now)      # process_since is set now
    r = _Run(cfg, conn, ask=ask or default_ask(cfg), now=now, clock=clock, log=log)
    r.run_id = state.begin_run(JOB, now)
    eligible, stop, error = 0, None, None
    try:
        r.discover([once] if once else [p for p in transcripts(cfg) if in_window(p, since)])
        if once:                                # now, whatever its queue state: fresh attempts, no backoff
            item_id = r.state.enqueue(STAGE, write.episode_id(once), now)
            r.state.requeue(item_id, now)
            items = [r.state.item(item_id)]
        else:
            items = work_order(r.state.eligible(STAGE, now))
        eligible = len(items)
        stop, error = r.work(items)
    except Exception as exc:                    # the run's own failure (database, transcripts folder)
        error = _error(exc)
    outcome, code, backlog = r.finish(eligible, stop, error)
    log({"event": "run", "run": r.run_id, "outcome": outcome, "exit": code, "eligible": eligible,
         "processed": r.processed(), "backlog": backlog, "new": r.counts[NEW], "changed": r.counts[CHANGED],
         "summarized": r.counts[SUMMARIZED], "no_content": r.counts[NO_CONTENT], "retried": r.counts[RETRY],
         "quarantined": r.counts[QUARANTINE], "calls": r.counts["calls"], "spent_usd": round(r.spent, 6),
         "stopped": stop, "error": error, "seconds": round(clock() - r.started, 3)})
    out(f"{JOB} run {outcome}: processed {r.processed()} of {eligible} eligible, backlog {backlog}"
        + (f", stopped: {stop}" if stop else "") + (f", error: {error}" if error else ""))
    return code


def plan(cfg: dict, conn, now: datetime, paths: list[Path]) -> list[dict]:
    """What a run would do with each transcript (action None: nothing), without a model
    call or a queue write."""
    store, state = Store(conn, cfg), State(conn, cfg)
    due = {i["ref"] for i in state.eligible(STAGE, now)}
    rows, started = [], 0
    for path in paths:
        kind, item = classify(store, state, path)
        row = {"ref": write.episode_id(path), "found": kind, "action": None, "call": False}
        if kind in (NEW, CHANGED) or (kind == PENDING and row["ref"] in due):
            if started >= cfg["pipeline"]["run"]["max_items"]:
                row["action"] = "next run (max items)"
            else:
                started += 1
                try:
                    prep = prepare.prepare([prepare.parse(path)], cfg)
                    row["action"] = "stub: note, no call" if prep.is_stub else "extract: one model call"
                    row["words"], row["call"] = prep.words, not prep.is_stub
                except Exception as exc:
                    row["action"] = f"fails in prepare: {_error(exc)}"
        elif kind == PENDING:
            row["action"] = f"waiting: retry after {item['next_attempt_at']}"
        elif item is not None and item["status"] == QUARANTINED:
            row["action"] = "quarantined: left alone until it changes (see its alert)"
        rows.append(row)
    return rows


def _dry_run(cfg: dict, conn, *, once: Optional[Path], now: datetime, out: Callable[[str], None]) -> int:
    if once is None:
        try:
            since = process_since(cfg)
        except ValueError as exc:
            out(f"dry run: a run would be refused: {exc}. {SETUP_FIX}")
            return EXIT_REFUSED
    rows = plan(cfg, conn, now, [once] if once else [p for p in transcripts(cfg) if in_window(p, since)])
    work = [row for row in rows if row["action"]]
    calls_planned = sum(row["call"] for row in rows)
    for row in work:
        out(f"{row['ref']}: {row['found']} -> {row['action']}")
    worst = calls_planned * cfg["pipeline"]["max_budget_per_call_usd"]
    out(f"dry run: {len(rows)} transcripts, {len(work)} listed, {calls_planned} model call(s) "
        f"(at most ${worst:.2f} at the per-call cap); no model call was made")
    return EXIT_OK


def backfill(cfg: dict, *, since: Optional[date] = None, until: Optional[date] = None, yes: bool = False,
             now: Optional[datetime] = None, out: Callable[[str], None] = print) -> int:
    """Queue the historical transcripts dated since..until (inclusive; `until` defaults
    to the day before pipeline.run.process_since) that are not yet processed, for the
    scheduled run to work newest first under its usual caps. It first prints their
    count and estimated cost; without `yes` it stops there, having written nothing."""
    if until is None and cfg["pipeline"]["run"]["process_since"] is not None:
        until = process_since(cfg) - timedelta(days=1)
    paths = [p for p in transcripts(cfg) if in_window(p, since, until)]
    with contextlib.closing(db.connect(cfg) if yes else db.snapshot(cfg)) as conn:
        store, state = Store(conn, cfg), State(conn, cfg)
        todo = [p for p in paths if classify(store, state, p)[0] in (NEW, CHANGED)]
        est, cap = cfg["pipeline"]["run"]["est_cost_per_call_usd"], cfg["pipeline"]["max_budget_per_call_usd"]
        out(f"backfill {since or 'earliest'}..{until or 'latest'}: {len(todo)} transcript(s) not yet processed, "
            f"about ${len(todo) * est:.2f} at ${est:.2f} per call (pipeline.run.est_cost_per_call_usd), "
            f"at most ${len(todo) * cap:.2f} at the per-call cap")
        if not yes:
            out("nothing queued: add --yes to queue them")
            return EXIT_OK
        discover(store, state, todo, now or datetime.now(timezone.utc))
    out(f"queued {len(todo)}; the scheduled {JOB} run works them newest first, within its spend caps")
    return EXIT_OK


def requeue(cfg: dict, ref: Optional[str] = None, *, all_quarantined: bool = False,
            now: Optional[datetime] = None, out: Callable[[str], None] = print) -> int:
    """After fixing the cause: put a transcript's item (its ref, the file stem), or every
    quarantined one, back in the queue with fresh attempts, and close its quarantine
    alert. The next scheduled run works it."""
    with contextlib.closing(db.connect(cfg)) as conn:
        state = State(conn, cfg)
        items = ([i for i in state.quarantined() if i["stage"] == STAGE] if all_quarantined
                 else [state.find(STAGE, ref)])
        if None in items:
            out(f"requeue: no {STAGE} item {ref!r} (a ref is the transcript's file stem, as its alert names it)")
            return EXIT_USAGE
        for item in items:
            state.requeue(item["id"], now)
            state.acknowledge(item_key(KIND_QUARANTINE, item), now)
            out(f"requeued {item['ref']} (was {item['status']})")
    out(f"{len(items)} item(s) requeued; the next {JOB} run works them")
    return EXIT_OK


def run(cfg: dict, *, dry_run: bool = False, once: Optional[str] = None, ask: Optional[Ask] = None,
        now: Optional[datetime] = None, clock: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = print) -> int:
    """One run; returns its exit code. `ask`, `now` and `clock` are for tests."""
    started = clock()
    now = now or datetime.now(timezone.utc)

    log = job_log(cfg, JOB)                     # never called by a dry run: it writes nothing

    path = None
    if once is not None:
        path = resolve_once(cfg, once)
        if path is None:
            out(f"--once: {once!r} is not a transcript in {cfg['paths']['transcripts']}")
            return EXIT_USAGE
    try:
        if dry_run:                             # a read-only copy: no lock needed, safe beside a live run
            with contextlib.closing(db.snapshot(cfg)) as conn:
                return _dry_run(cfg, conn, once=path, now=now, out=out)
        with single_instance(files(cfg, "lock")) as held:
            if not held:
                log({"event": "run", "outcome": LOCKED, "exit": EXIT_OK, "seconds": round(clock() - started, 3)})
                out(f"another {JOB} run holds the lock; exiting")
                return EXIT_OK
            with contextlib.closing(db.connect(cfg)) as conn:
                return _locked_run(cfg, conn, once=path, ask=ask, now=now, clock=clock, log=log, out=out)
    except Exception as exc:                    # before the run could be recorded (database, folder)
        if not dry_run:
            log({"event": "run", "outcome": FAILED, "exit": EXIT_FAILED, "error": _error(exc)})
        out(f"{JOB} run failed: {_error(exc)}")
        return EXIT_FAILED
