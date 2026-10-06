"""The task funnel (D.5, lesson L20): how many of the owner's tasks moved captured ->
confirmed -> ready -> done.

`python -m kg.tasks funnel [--days N] [--format text|json] [--config OVERLAY.yaml]`
prints it for tasks captured in the last N days (default kg.tasks.funnel_days). The
weekly report and `doctor` call `funnel_report`, a pure function over the Store: it
reads, never writes, and takes `now` so a test can pin the window.

- **Scope.** The owner's own tasks (kg.tasks.mine_owner_basis); others' tasks are
  stored for the graph but never enter the review, so they are counted apart
  (`others_captured`) rather than diluting the conversion rates.
- **Stages** count the furthest stage each task ever reached (Store.funnel), so a
  task dropped after it was confirmed still counts as confirmed; `status_now` says
  where the window's tasks sit today, `dropped` included.
- **Conversion** is each stage over the one before it, and `overall` the last stage
  over the first; a rate over zero tasks is null, never a division error.
- **confirm_open** is the "confirm?" backlog however old (Store.confirm_open): what
  the next /create-tasks review starts with.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from kg import db
from kg.store import Store
from pipeline.config import load_config

# Rates are kept to this many decimals in the report (a ratio of task counts).
RATE_DIGITS = 4


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, RATE_DIGITS) if denominator else None


def funnel_report(store: Store, *, days: int, now: Optional[datetime] = None) -> dict:
    """The funnel for the owner's tasks captured in the `days` before `now`."""
    if days < 1:
        raise ValueError(f"days must be at least 1, not {days}")
    end = now or datetime.now(timezone.utc)
    since, until = db.utc_now(end - timedelta(days=days)), db.utc_now(end)
    mine = list(store.task_cfg["mine_owner_basis"])
    stages = store.funnel(since, until=until, owner_basis=mine)
    names = list(stages)
    status_now = store.task_status_counts(since, until=until, owner_basis=mine)
    everyone = store.task_status_counts(since, until=until)
    return {
        "window_days": days, "since": since, "until": until, "owner_basis": mine,
        "stages": stages,
        "conversion": [{"from": a, "to": b, "rate": _rate(stages[b], stages[a])} for a, b in zip(names, names[1:])],
        "overall": {"from": names[0], "to": names[-1], "rate": _rate(stages[names[-1]], stages[names[0]])},
        "status_now": status_now,
        "others_captured": sum(everyone.values()) - sum(status_now.values()),
        "confirm_open": store.confirm_open(),
    }


def _pct(rate: Optional[float]) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def render_funnel(report: dict) -> str:
    """The report on one screen."""
    stages = report["stages"]
    width = max(len(s) for s in stages)
    lines = [f"Task funnel: my tasks captured in the last {report['window_days']} days "
             f"({report['since'][:10]} to {report['until'][:10]})"]
    first = next(iter(stages))
    lines.append(f"  {first:<{width}}  {stages[first]:>5}")
    for step in report["conversion"]:
        lines.append(f"  {step['to']:<{width}}  {stages[step['to']]:>5}   {_pct(step['rate'])} of {step['from']}")
    overall = report["overall"]
    lines.append(f"  overall: {_pct(overall['rate'])} of {overall['from']} reached {overall['to']}")
    lines.append("Now: " + ", ".join(f"{status} {n}" for status, n in report["status_now"].items()))
    lines.append(f'"confirm?" waiting for review (any age): {report["confirm_open"]}')
    lines.append(f"Others' tasks captured in the window (not in the funnel): {report['others_captured']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kg.tasks", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    funnel = sub.add_parser("funnel", help="counts per funnel stage and conversion rates")
    funnel.add_argument("--days", type=int, default=None, help="window in days (default kg.tasks.funnel_days)")
    funnel.add_argument("--format", choices=("text", "json"), default="text")
    funnel.add_argument("--config", type=Path, default=None,
                        help="the per-user overlay (default %%APPDATA%%\\whispr\\config.yaml)")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(overlay_path=args.config)
        conn = db.connect_readonly(cfg)
    except Exception as exc:
        print(f"cannot open the task database: {exc}", file=sys.stderr)
        return 1
    try:
        days = args.days if args.days is not None else cfg["kg"]["tasks"]["funnel_days"]
        try:
            report = funnel_report(Store(conn, cfg), days=days)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if args.format == "json" else render_funnel(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
