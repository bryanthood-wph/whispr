"""Pipeline state: runs, per-item progress and the one alert surface (D.1, D.6).

- **Success means progress** (lesson L1). `finish_run` marks a run succeeded only if
  it processed >= 1 item or proved zero items were eligible (eligible == 0; None means
  it never counted, which proves nothing), and raised no error. Every run records
  its backlog. A run that fails raises a `run-failed` alert for its job; so does a
  run still marked running when its job starts again (it never finished, D.6). The
  job's next successful run closes that alert.
- **One bad item never blocks the queue** (lesson L7). Each transcript/episode has one
  item row per stage. `begin_item` counts an attempt *before* the work, so an item
  that kills the process still uses up its attempts; once attempts reach
  `pipeline.max_attempts` the item is quarantined (after a failure, or at its next
  start if the last attempt never reported back) with one alert naming the fix. A
  failure may set the item's next attempt time (retry backoff, chosen by the caller);
  `eligible` leaves the item out until then, `backlog` still counts it.
- **Repeat items** (lesson L1). An item started in `alerts.repeat_item_runs`
  consecutive runs of the same job, whatever their outcome, raises an alert: the
  queue is not advancing past it.
- **Alerts are rows** with a dedupe key, first/last seen and a count. Raising a known
  key counts it and reopens it if it was acknowledged. `quarantine_digest` moves
  items quarantined `alerts.quarantine_digest_days` ago out of the open alerts into
  one digest alert, re-raised at most once per that period unless new items join it.
- C.5 maintenance writes its per-check log and health metrics here, against its run.

Run and item statuses are this module's own state machine (the CHECK constraints in
migration 0001 hold the same names); every threshold comes from config.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from kg.db import fetch_all, fetch_one, new_id, stable_id, transaction, utc_now

RUNNING, SUCCEEDED, FAILED = "running", "succeeded", "failed"
QUEUED, DONE, QUARANTINED = "queued", "done", "quarantined"

KIND_RUN_FAILED = "run-failed"
KIND_QUARANTINE = "quarantine"
KIND_DIGEST = "quarantine-digest"
KIND_REPEAT = "repeat-item"
DIGEST_KEY = KIND_DIGEST


class StateError(RuntimeError):
    pass


def item_key(kind: str, item: dict) -> str:
    """An item's alert dedupe key for `kind`."""
    return f"{kind}:{item['stage']}:{item['ref']}"


def alert_key(kind: str, subject: str) -> str:
    """An alert's dedupe key: `<kind>:<subject>` (the job, or what the alert is about)."""
    return f"{kind}:{subject}"


class State:
    def __init__(self, conn: sqlite3.Connection, cfg: dict):
        self.conn = conn
        self.max_attempts = cfg["pipeline"]["max_attempts"]
        self.digest_days = cfg["alerts"]["quarantine_digest_days"]
        self.repeat_runs = cfg["alerts"]["repeat_item_runs"]

    # ---- runs -----------------------------------------------------------------------

    def begin_run(self, job: str, now: Optional[datetime] = None) -> str:
        """Start a run of `job`. A run of the same job still marked running never
        finished (the process died or was killed): it is failed here, with the job's
        run-failed alert, and this run is its retry (D.6). A job runs one at a time."""
        run_id = new_id()
        with transaction(self.conn):
            for dead in fetch_all(self.conn, "SELECT * FROM run WHERE job = ? AND status = ? ORDER BY seq",
                                  (job, RUNNING)):
                why = "it never finished: the process died or was killed before finish_run"
                self.conn.execute("UPDATE run SET finished_at = ?, status = ?, error = ? WHERE id = ?",
                                  (utc_now(now), FAILED, why, dead["id"]))
                self._alert_run_failed(dead, why, dead["backlog"], now)
            seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run").fetchone()[0]
            self.conn.execute("INSERT INTO run (id, seq, job, started_at, status) VALUES (?, ?, ?, ?, ?)",
                              (run_id, seq, job, utc_now(now), RUNNING))
        return run_id

    def run(self, run_id: str) -> Optional[dict]:
        return fetch_one(self.conn, "SELECT * FROM run WHERE id = ?", (run_id,))

    def _running(self, run_id: str) -> dict:
        run = self.run(run_id)
        if run is None:
            raise StateError(f"no run {run_id!r}")
        if run["status"] != RUNNING:
            raise StateError(f"run {run_id!r} already finished ({run['status']})")
        return run

    def finish_run(self, run_id: str, *, processed: int, eligible: Optional[int], backlog: int,
                   error: Optional[str] = None, now: Optional[datetime] = None) -> bool:
        """Close a run; True if it succeeded (made progress or proved there was none
        to make). A failed run raises its job's run-failed alert; a successful one
        closes it (the next failure reopens it). Impossible counts are a caller bug
        and raise ValueError rather than record a false success."""
        if min(processed, backlog, 0 if eligible is None else eligible) < 0:
            raise ValueError(f"negative count: processed={processed}, eligible={eligible}, backlog={backlog}")
        if eligible is not None and processed > eligible:
            raise ValueError(f"processed {processed} of only {eligible} eligible")
        run = self._running(run_id)
        succeeded = error is None and (processed >= 1 or eligible == 0)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE run SET finished_at = ?, status = ?, processed = ?, eligible = ?, backlog = ?, error = ?"
                " WHERE id = ?", (utc_now(now), SUCCEEDED if succeeded else FAILED, processed, eligible,
                                  backlog, error, run_id))
            if not succeeded:
                why = error or ("processed nothing while eligible items were never counted" if eligible is None
                                else f"processed nothing of {eligible} eligible")
                self._alert_run_failed(run, why, backlog, now)
            else:
                self.acknowledge(alert_key(KIND_RUN_FAILED, run["job"]), now)
        return succeeded

    def _alert_run_failed(self, run: dict, why: str, backlog: Optional[int], now: Optional[datetime]) -> None:
        shown = "not recorded" if backlog is None else backlog
        self.raise_alert(KIND_RUN_FAILED, alert_key(KIND_RUN_FAILED, run["job"]),
                         f"{run['job']} run {run['id']} failed: {why} (backlog {shown}).",
                         "Read this run's log for the cause; run doctor to see the backlog.", now=now)

    def last_success(self, job: str) -> Optional[dict]:
        """The job's latest successful run: what doctor reports as the last progress."""
        return fetch_one(self.conn, "SELECT * FROM run WHERE job = ? AND status = ? ORDER BY seq DESC LIMIT 1",
                                    (job, SUCCEEDED))

    def _previous_run_id(self, run: dict) -> Optional[str]:
        row = self.conn.execute("SELECT id FROM run WHERE job = ? AND seq < ? ORDER BY seq DESC LIMIT 1",
                                (run["job"], run["seq"])).fetchone()
        return row[0] if row else None

    # ---- items ----------------------------------------------------------------------

    def enqueue(self, stage: str, ref: str, now: Optional[datetime] = None) -> str:
        """The item for (stage, ref), created queued if new; an existing item is untouched."""
        item_id = stable_id("item", stage, ref)
        stamp = utc_now(now)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO item (id, stage, ref, status, attempts, consecutive_runs, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 0, 0, ?, ?) ON CONFLICT (id) DO NOTHING",
                (item_id, stage, ref, QUEUED, stamp, stamp))
        return item_id

    def item(self, item_id: str) -> Optional[dict]:
        return fetch_one(self.conn, "SELECT * FROM item WHERE id = ?", (item_id,))

    def find(self, stage: str, ref: str) -> Optional[dict]:
        """The item for (stage, ref), or None if it was never enqueued."""
        return fetch_one(self.conn, "SELECT * FROM item WHERE stage = ? AND ref = ?", (stage, ref))

    def by_ref(self, stage: str) -> dict[str, dict]:
        """Every item of a stage by ref, in one read (find, for many refs at once)."""
        return {item["ref"]: item for item in fetch_all(self.conn, "SELECT * FROM item WHERE stage = ?", (stage,))}

    def _item(self, item_id: str) -> dict:
        item = self.item(item_id)
        if item is None:
            raise StateError(f"no item {item_id!r}")
        return item

    def _queued(self, item_id: str) -> dict:
        item = self._item(item_id)
        if item["status"] != QUEUED:
            raise StateError(f"item {item_id!r} is {item['status']}, not {QUEUED}")
        return item

    def eligible(self, stage: str, now: Optional[datetime] = None) -> list[dict]:
        """Queued items of a stage that are due (no next attempt time, or one at or
        before `now`), oldest first; quarantined, done and backed-off ones excluded."""
        return fetch_all(self.conn, "SELECT * FROM item WHERE stage = ? AND status = ?"
                                    " AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at, id",
                         (stage, QUEUED, utc_now(now)))

    def backlog(self, stage: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM item WHERE stage = ? AND status = ?",
                                 (stage, QUEUED)).fetchone()[0]

    def begin_item(self, run_id: str, item_id: str, now: Optional[datetime] = None) -> bool:
        """Count an attempt at a queued item in this run. False (and the item is
        quarantined) when its attempts are already used up: the last one never
        reported back."""
        run = self._running(run_id)
        item = self._queued(item_id)
        with transaction(self.conn):
            if item["attempts"] >= self.max_attempts:
                self._quarantine(item, now)
                return False
            if item["last_run_id"] == run_id:          # a retry inside the same run
                runs = item["consecutive_runs"]
            elif item["last_run_id"] is not None and item["last_run_id"] == self._previous_run_id(run):
                runs = item["consecutive_runs"] + 1
            else:
                runs = 1
            self.conn.execute(
                "UPDATE item SET attempts = attempts + 1, consecutive_runs = ?, last_run_id = ?, updated_at = ?"
                " WHERE id = ?", (runs, run_id, utc_now(now), item_id))
            if runs >= self.repeat_runs and runs != item["consecutive_runs"]:
                self.raise_alert(
                    KIND_REPEAT, item_key(KIND_REPEAT, item),
                    f"{item['stage']} item {item['ref']} was started in {runs} consecutive {run['job']} runs.",
                    "The queue is not moving past this item: check why it is still queued after each run "
                    "(its last_error, or a step that re-queues it).", now=now)
        return True

    def item_done(self, item_id: str, now: Optional[datetime] = None) -> None:
        self._item(item_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE item SET status = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                              (DONE, utc_now(now), item_id))

    def item_failed(self, item_id: str, error: str, now: Optional[datetime] = None,
                    retry_at: Optional[datetime] = None) -> str:
        """Record a failed attempt; returns the item's status (quarantined once its
        attempts reach pipeline.max_attempts, else still queued for a retry, not
        eligible before `retry_at` when one is given)."""
        item = self._queued(item_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE item SET last_error = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                              (error, utc_now(retry_at) if retry_at else None, utc_now(now), item_id))
            if item["attempts"] >= self.max_attempts:
                self._quarantine({**item, "last_error": error}, now)
                return QUARANTINED
        return QUEUED

    def _quarantine(self, item: dict, now: Optional[datetime]) -> None:
        stamp = utc_now(now)
        self.conn.execute("UPDATE item SET status = ?, quarantined_at = ?, digested_at = NULL, updated_at = ?"
                          " WHERE id = ?", (QUARANTINED, stamp, stamp, item["id"]))
        cause = item["last_error"] or "its last attempt never reported back (the process died?)"
        self.raise_alert(
            KIND_QUARANTINE, item_key(KIND_QUARANTINE, item),
            f"{item['stage']} item {item['ref']} quarantined after {item['attempts']} attempts: {cause}",
            f"Fix the cause above, then run `python -m pipeline requeue {item['ref']}` (or `--all-quarantined`); "
            "the rest of the queue keeps running meanwhile.", now=now)

    def undo_attempt(self, item: dict, error: str, now: Optional[datetime] = None) -> None:
        """Undo begin_item: `item` is the row as it was before it. For a failure of the
        machine (auth, the model unreachable), not the item: it uses no attempt and moves
        no repeat count. The error is kept as the item's last_error."""
        with transaction(self.conn):
            self.conn.execute("UPDATE item SET attempts = ?, consecutive_runs = ?, last_run_id = ?, last_error = ?,"
                              " updated_at = ? WHERE id = ?", (item["attempts"], item["consecutive_runs"],
                                                             item["last_run_id"], error, utc_now(now), item["id"]))

    def requeue(self, item_id: str, now: Optional[datetime] = None) -> None:
        """Put an item back in the queue with fresh attempts (after fixing its cause)."""
        self._item(item_id)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE item SET status = ?, attempts = 0, last_error = NULL, quarantined_at = NULL,"
                " digested_at = NULL, next_attempt_at = NULL, updated_at = ? WHERE id = ?", (QUEUED, utc_now(now), item_id))

    def quarantined(self) -> list[dict]:
        return fetch_all(self.conn, "SELECT * FROM item WHERE status = ? ORDER BY quarantined_at, id",
                         (QUARANTINED,))

    # ---- alerts ---------------------------------------------------------------------

    def raise_alert(self, kind: str, dedupe_key: str, message: str, fix: str,
                    now: Optional[datetime] = None) -> str:
        """Create the alert, or count another occurrence of it (reopening it if it had
        been acknowledged or expired). Returns its id."""
        stamp = utc_now(now)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO alert (id, dedupe_key, kind, message, fix, first_seen, last_seen, count,"
                " acknowledged_at, expired_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL, NULL)"
                " ON CONFLICT (dedupe_key) DO UPDATE SET kind = excluded.kind, message = excluded.message,"
                " fix = excluded.fix, last_seen = excluded.last_seen, count = alert.count + 1,"
                " acknowledged_at = NULL, expired_at = NULL",
                (new_id(), dedupe_key, kind, message, fix, stamp, stamp))
        return self.conn.execute("SELECT id FROM alert WHERE dedupe_key = ?", (dedupe_key,)).fetchone()[0]

    def alert(self, dedupe_key: str) -> Optional[dict]:
        return fetch_one(self.conn, "SELECT * FROM alert WHERE dedupe_key = ?", (dedupe_key,))

    def acknowledge(self, alert_id_or_key: str, now: Optional[datetime] = None) -> bool:
        """Mark an open alert seen, by id or dedupe key; False if there is none open."""
        with transaction(self.conn):
            cur = self.conn.execute(
                "UPDATE alert SET acknowledged_at = ? WHERE (id = ? OR dedupe_key = ?)"
                " AND acknowledged_at IS NULL AND expired_at IS NULL",
                (utc_now(now), alert_id_or_key, alert_id_or_key))
        return cur.rowcount > 0

    def acknowledge_open(self, keys: list[str], now: Optional[datetime] = None) -> list[str]:
        """Acknowledge whichever of these dedupe keys has an open alert; returns those
        keys. Looks first, so the usual case (none open) takes no write lock."""
        if not keys:
            return []
        marks = ", ".join("?" * len(keys))
        open_keys = [row[0] for row in self.conn.execute(
            f"SELECT dedupe_key FROM alert WHERE dedupe_key IN ({marks})"
            " AND acknowledged_at IS NULL AND expired_at IS NULL", keys)]
        if open_keys:
            with transaction(self.conn):
                self.conn.executemany(
                    "UPDATE alert SET acknowledged_at = ? WHERE dedupe_key = ?"
                    " AND acknowledged_at IS NULL AND expired_at IS NULL",
                    [(utc_now(now), key) for key in open_keys])
        return [key for key in keys if key in open_keys]

    def open_alerts(self) -> list[dict]:
        """Alerts neither acknowledged nor expired, most recent first."""
        return fetch_all(self.conn, "SELECT * FROM alert WHERE acknowledged_at IS NULL AND expired_at IS NULL"
                                    " ORDER BY last_seen DESC, id")

    def quarantine_digest(self, now: Optional[datetime] = None) -> list[dict]:
        """Fold items quarantined at least alerts.quarantine_digest_days ago into the
        digest alert, expiring their own alerts. The digest is (re-)raised when items
        join it, or once its last raise is that old. Returns the items in it."""
        moment = now or datetime.now(timezone.utc)
        cutoff = utc_now(moment - timedelta(days=self.digest_days))
        stamp = utc_now(moment)
        with transaction(self.conn):
            newly = fetch_all(self.conn, "SELECT * FROM item WHERE status = ? AND digested_at IS NULL"
                                         " AND quarantined_at <= ?", (QUARANTINED, cutoff))
            for item in newly:
                self.conn.execute("UPDATE item SET digested_at = ? WHERE id = ?", (stamp, item["id"]))
                self.conn.execute("UPDATE alert SET expired_at = ? WHERE dedupe_key = ? AND expired_at IS NULL",
                                  (stamp, item_key(KIND_QUARANTINE, item)))
            digest = fetch_all(self.conn, "SELECT * FROM item WHERE status = ? AND digested_at IS NOT NULL"
                                          " ORDER BY quarantined_at, id", (QUARANTINED,))
            existing = self.alert(DIGEST_KEY)
            if digest and (newly or existing is None or existing["last_seen"] <= cutoff):
                listed = "; ".join(f"{i['stage']} {i['ref']}: {i['last_error'] or 'no error recorded'}"
                                   for i in digest)
                self.raise_alert(
                    KIND_DIGEST, DIGEST_KEY,
                    f"{len(digest)} item(s) quarantined for over {self.digest_days} days: {listed}",
                    "Fix each cause and run `python -m pipeline requeue <ref>` (or `--all-quarantined`), or "
                    "leave it: this digest repeats once per period while any remain.", now=moment)
        return digest

    # ---- maintenance (C.5) ----------------------------------------------------------

    def log_maintenance(self, run_id: str, check_name: str, *, found: int, repaired: int, escalated: int,
                        details: Optional[str] = None, now: Optional[datetime] = None) -> str:
        self._running(run_id)
        row_id = new_id()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO maintenance_log (id, run_id, check_name, found, repaired, escalated, details,"
                " recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row_id, run_id, check_name, found, repaired, escalated, details, utc_now(now)))
        return row_id

    def record_metric(self, run_id: str, name: str, value: float) -> None:
        self._running(run_id)
        with transaction(self.conn):
            self.conn.execute("INSERT INTO health_metric (run_id, name, value) VALUES (?, ?, ?)"
                              " ON CONFLICT (run_id, name) DO UPDATE SET value = excluded.value",
                              (run_id, name, value))

    def maintenance(self, run_id: str) -> list[dict]:
        return fetch_all(self.conn, "SELECT * FROM maintenance_log WHERE run_id = ? ORDER BY recorded_at, id",
                         (run_id,))

    def metrics(self, run_id: str) -> dict[str, float]:
        return {r["name"]: r["value"] for r in self.conn.execute(
            "SELECT name, value FROM health_metric WHERE run_id = ?", (run_id,))}
