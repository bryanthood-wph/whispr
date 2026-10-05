"""Run ledger, budget guard and run records (B.9 run safety; lessons L1, L6, L8).

- pipeline/models.py appends a row with `cost_usd` to the call ledger after every
  launched call (a call with no result is recorded at its cap).
- The budget guard runs before every paid launch (pipeline.calls `before_call`). It
  stops once the next call could take total spend past
  eval.budget_usd - eval.stop_margin_usd, or the stage's spend (summed over all its
  runs) past the stage's cap: it stops and asks rather than shrinking n. Otherwise it
  appends a reservation row for the call's cap and stage under its request key.
- Spend = the call rows, plus every reservation no call row has settled yet. A run
  killed mid-call (its row never written) is so charged at the cap, and a call row
  is attributed to the stage of the reservation it settles.
- An unreadable line (a process killed mid-append) is skipped and counted, never
  fatal: losing a call row leaves its reservation charged, so spend errs high.
- A run holds an exclusive lock for its lifetime. A run that started but has no end
  record while the lock is free was killed: `reconcile` marks it failed, and
  `python -m eval status` exits non-zero until it is resolved.
"""

from __future__ import annotations

import json
import msvcrt
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from pipeline.config import data_dir
from pipeline.models import append_jsonl

LEDGER_FILE = "ledger.jsonl"
RUNS_FILE = "runs.jsonl"
LOCK_FILE = "run.lock"
END_EVENTS = {"completed", "failed"}
RESERVE = "reserve"


class BudgetStop(RuntimeError):
    """The next call could break the budget: stop and ask (B.9)."""


class RunError(RuntimeError):
    pass


def eval_dir(cfg: dict) -> Path:
    return data_dir(cfg, "eval")


def ledger_path(cfg: dict) -> Path:
    return eval_dir(cfg) / LEDGER_FILE


def _read(path: Path) -> tuple[list[dict], int]:
    """(rows, unreadable line count)."""
    if not path.exists():
        return [], 0
    rows, bad = [], 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def _rows(path: Path) -> list[dict]:
    return _read(path)[0]


def malformed(cfg: dict) -> int:
    """Unreadable lines in the ledger and run records (shown by `status`)."""
    return sum(_read(eval_dir(cfg) / name)[1] for name in (LEDGER_FILE, RUNS_FILE))


def tally(cfg: dict) -> tuple[float, dict[str, float]]:
    """(total spend, spend by stage), counting unsettled reservations at their cap."""
    total, by_stage = 0.0, {}
    pending: dict[str, list[tuple[str, float]]] = {}
    for row in _rows(ledger_path(cfg)):
        if row.get("event") == RESERVE:
            pending.setdefault(row.get("request_key"), []).append((row["stage"], float(row["reserve_usd"])))
            continue
        cost = float(row.get("cost_usd") or 0.0)
        total += cost
        queue = pending.get(row.get("request_key"))
        if queue:
            stage = queue.pop(0)[0]
            by_stage[stage] = by_stage.get(stage, 0.0) + cost
    for queue in pending.values():
        for stage, usd in queue:
            total += usd
            by_stage[stage] = by_stage.get(stage, 0.0) + usd
    return total, by_stage


def spent(cfg: dict) -> float:
    return tally(cfg)[0]


def budget_limit(cfg: dict) -> float:
    """Spend at which work stops and RESUME.md is written (G.5): cap minus margin."""
    return cfg["eval"]["budget_usd"] - cfg["eval"]["stop_margin_usd"]


def guard(cfg: dict, stage: str) -> Callable[[str, float], None]:
    """A before_call hook: enforce the overall limit and the stage cap, then reserve."""
    stage_cap = cfg["eval"]["stages"][stage]["cap_usd"]

    def check(key: str, call_cap: float) -> None:
        total, by_stage = tally(cfg)
        used = by_stage.get(stage, 0.0)
        if total + call_cap > budget_limit(cfg):
            raise BudgetStop(f"spent ${total:.2f}; the next call (cap ${call_cap:.2f}) could pass "
                             f"the ${budget_limit(cfg):.2f} limit (eval.budget_usd - eval.stop_margin_usd)")
        if used + call_cap > stage_cap:
            raise BudgetStop(f"stage {stage!r} spent ${used:.2f}; the next call (cap ${call_cap:.2f}) "
                             f"could pass its ${stage_cap:.2f} cap (eval.stages.{stage}.cap_usd)")
        _append(ledger_path(cfg), {"event": RESERVE, "request_key": key, "stage": stage, "reserve_usd": call_cap})
    return check


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(path: Path, record: dict) -> None:
    append_jsonl(path, {"ts": _now(), **record})


def _append_run(cfg: dict, record: dict) -> None:
    _append(eval_dir(cfg) / RUNS_FILE, record)


def runs(cfg: dict) -> dict[str, list[dict]]:
    """run_id -> its events, in order."""
    out: dict[str, list[dict]] = {}
    for r in _rows(eval_dir(cfg) / RUNS_FILE):
        out.setdefault(r["run_id"], []).append(r)
    return out


class _Lock:
    """An exclusive, non-blocking lock the OS releases if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.fh = open(self.path, "a+")
        try:
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def release(self) -> None:
        if self.fh:
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            self.fh.close()
            self.fh = None


def reconcile(cfg: dict) -> list[str]:
    """Mark started-but-unfinished runs failed, if no live run holds the lock."""
    lock = _Lock(eval_dir(cfg) / LOCK_FILE)
    if not lock.acquire():
        return []
    try:
        killed = [rid for rid, ev in runs(cfg).items()
                  if not any(e["event"] in END_EVENTS for e in ev)]
        for rid in killed:
            _append_run(cfg, {"run_id": rid, "event": "failed", "error": "killed: no completion record"})
        return killed
    finally:
        lock.release()


def unresolved(cfg: dict) -> dict[str, list[dict]]:
    """Failed runs not yet resolved; status stays non-zero while any exist."""
    return {rid: ev for rid, ev in runs(cfg).items()
            if any(e["event"] == "failed" for e in ev) and not any(e["event"] == "resolved" for e in ev)}


def resolve(cfg: dict, run_id: str, note: str) -> None:
    if run_id not in unresolved(cfg):
        raise RunError(f"{run_id!r} is not an unresolved failed run")
    _append_run(cfg, {"run_id": run_id, "event": "resolved", "note": note})


class Run:
    """`with Run(cfg, stage, provenance) as run:` records started, then completed or
    failed (with the error), and holds the lock throughout."""

    def __init__(self, cfg: dict, stage: str, provenance: Optional[dict] = None):
        self.cfg, self.stage, self.provenance = cfg, stage, provenance or {}
        self.run_id = f"{stage}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        self._lock = _Lock(eval_dir(cfg) / LOCK_FILE)

    def __enter__(self) -> "Run":
        reconcile(self.cfg)
        if bad := unresolved(self.cfg):
            raise RunError(f"unresolved failed runs: {sorted(bad)}; resolve them first")
        if not self._lock.acquire():
            raise RunError("another eval run holds the lock")
        _append_run(self.cfg, {"run_id": self.run_id, "event": "started", "stage": self.stage,
                               "pid": os.getpid(), **self.provenance})
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is None:
                _append_run(self.cfg, {"run_id": self.run_id, "event": "completed"})
            else:
                _append_run(self.cfg, {"run_id": self.run_id, "event": "failed",
                                       "error": f"{exc_type.__name__}: {exc}"})
        finally:
            self._lock.release()
        return False
