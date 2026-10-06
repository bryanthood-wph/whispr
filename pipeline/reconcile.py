"""The daily reconciliation (docs/plan/D-architecture-and-ops.md D.6; lessons L1, L14):
transcripts written vs expected calls, and every transcript accounted for downstream.

It compares, over the last reconcile.window_days (calls and transcripts newer than
reconcile.settle_min are not judged yet: transcription and the 15-minute run come after
the call):
- **Recorder.** Calls the recorder queued for transcription vs transcripts it wrote,
  counted from its own log lines (paths.recorder_log and its rotated copies; the line
  shapes are reconcile.recorder_log). Each written transcript is matched to the call it
  records (lost_calls), and a settled queued call with none is a lost call (L14: 308
  queued, 304 written), so a recent call's transcript never hides an older lost one. A
  transcript it wrote that is no longer on disk is reported.
- **Pipeline.** Every transcript dated on or after pipeline.run.process_since has an
  episode or a queue item; one with neither (older than settle_min) was never picked up.
  Older transcripts with no episode are counted as not backfilled (not a problem: only
  `backfill` takes them).
- **Episodes.** A live episode whose transcript is gone from paths.transcripts is
  tombstoned (Store.tombstone_episode; reversible: the transcript coming back revives
  it). More than reconcile.max_tombstones at once is refused with a problem instead: an
  offline or moved transcripts folder must not tombstone the graph.
- **Outlook** (optional). `calendar(start, end)` returns the Teams meetings in the window
  as {"subject", "start", "end"} (aware datetimes); one with no transcript starting
  between reconcile.outlook_slack_min before it and its end is reported as unrecorded
  (information, not a problem: you may not have attended). `outlook_reader(cfg)` is the
  production reader and is a documented stub returning None (not checked): the Outlook
  COM code lives in the recorder (whispr/metadata.py), which pipeline/ does not import,
  and a COM reader here waits until the recorder's metadata path is consolidated.

Anything in `problems` raises one `reconcile:<job>` alert, and a reconciliation with
none closes it; `dry_run` reports without tombstoning or alerting.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from kg.db import fetch_all
from kg.state import State
from kg.store import Store
from pipeline import run as pipeline_run

KIND = "reconcile"
# calendar(start, end) -> the Teams meetings in that window, each {"subject", "start", "end"}.
Calendar = Callable[[datetime, datetime], list[dict]]
_TRANSCRIPT_STAMP = "%Y-%m-%d-%H%M"     # the recorder's file name: YYYY-MM-DD-HHMM-<slug>.md


def outlook_reader(cfg: dict) -> Optional[Calendar]:
    """The production Outlook reader: None (a documented stub; see the module docstring)."""
    return None


def transcript_start(path: Path) -> Optional[datetime]:
    """The call's local start from the transcript's file name, or None."""
    try:
        return datetime.strptime(Path(path).stem[:len("YYYY-MM-DD-HHMM")], _TRANSCRIPT_STAMP).astimezone()
    except ValueError:
        return None


def _recorder_logs(cfg: dict) -> list[Path]:
    log = Path(cfg["paths"]["recorder_log"])
    return [p for p in [log, *sorted(log.parent.glob(log.name + ".*"))] if p.is_file()]


def recorder_events(cfg: dict, start: datetime, end: datetime) -> dict:
    """The recorder's queued calls and written transcripts between start and end, from
    its log: {"configured", "files", "queued": [time], "written": [(time, path)]}."""
    if cfg["paths"]["recorder_log"] is None:
        return {"configured": False, "files": [], "queued": [], "written": []}
    shapes = cfg["reconcile"]["recorder_log"]
    stamp, queued, written = (re.compile(shapes[k]) for k in ("stamp", "queued", "written"))
    files = _recorder_logs(cfg)
    out: dict = {"configured": True, "files": [str(p) for p in files], "queued": [], "written": []}
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\r\n")
                m = stamp.match(line)
                if not m:
                    continue
                try:
                    at = datetime.fromisoformat(m.group(1)).astimezone()     # the log's local time
                except ValueError:
                    continue
                if not start <= at <= end:
                    continue
                if queued.search(line):
                    out["queued"].append(at)
                w = written.search(line)
                if w:
                    out["written"].append((at, w.group(1).strip()))
    return out


def lost_calls(queued: list[datetime], written: list[tuple[datetime, str]]) -> list[datetime]:
    """The queued calls no written transcript accounts for. Each transcript is matched
    to one queued call: the earliest unmatched one queued at or after the call's start
    (its file name) and at or before it was written; when the name gives no start, the
    latest unmatched one queued before it was written. So a recent call's transcript
    never counts for an older lost call (L14)."""
    open_ = sorted(queued)
    for at, path in sorted(written):
        start = transcript_start(Path(path))
        fits = [q for q in open_ if q <= at and (start is None or q >= start)]
        if fits:
            open_.remove(fits[-1] if start is None else fits[0])
    return open_


def report(cfg: dict, store: Store, state: State, *, now: datetime, calendar: Optional[Calendar] = None) -> dict:
    """What reconciliation finds; writes nothing."""
    r = cfg["reconcile"]
    start, settled = now - timedelta(days=r["window_days"]), now - timedelta(minutes=r["settle_min"])
    problems: list[str] = []

    events = recorder_events(cfg, start, now)
    judged = [at for at in events["queued"] if at <= settled]
    lost_at = [at for at in lost_calls(events["queued"], events["written"]) if at <= settled]
    lost = len(lost_at)
    gone_written = sorted(path for _, path in events["written"] if not Path(path).exists())
    if not events["configured"]:
        problems.append("paths.recorder_log is not set, so the recorder's calls are not compared")
    elif not events["files"]:
        problems.append(f"the recorder log {cfg['paths']['recorder_log']} does not exist")
    if lost:
        problems.append(f"the recorder queued {len(judged)} call(s) settled in the last {r['window_days']} day(s); "
                        f"{lost} of them never got a transcript: {lost} call(s) lost (queued at "
                        + ", ".join(at.isoformat(timespec="minutes") for at in lost_at[:5]) + ")")

    paths = pipeline_run.transcripts(cfg)
    try:
        since = pipeline_run.process_since(cfg)
    except ValueError:
        since = None
    unpicked, not_backfilled = [], 0
    for path in paths:
        accounted = store.episode(path.stem) is not None or state.find(pipeline_run.STAGE, path.stem) is not None
        if accounted:
            continue
        if since is None or not pipeline_run.in_window(path, since):
            not_backfilled += 1
        elif datetime.fromtimestamp(path.stat().st_mtime).astimezone() <= settled:
            unpicked.append(path.stem)
    if since is None:
        problems.append("pipeline.run.process_since is not set, so no transcript is processed yet")
    if unpicked:
        problems.append(f"{len(unpicked)} transcript(s) have no episode or queue item: the pipeline run has not "
                        f"picked them up ({', '.join(unpicked[:5])})")

    on_disk = {p.stem for p in paths}
    gone = [row["id"] for row in fetch_all(store.conn, "SELECT id FROM episode WHERE deleted_at IS NULL ORDER BY id")
            if row["id"] not in on_disk]
    blocked = len(gone) > r["max_tombstones"]
    if blocked:
        problems.append(f"{len(gone)} episode(s) have no transcript in {cfg['paths']['transcripts']}, more than "
                        f"reconcile.max_tombstones ({r['max_tombstones']}): none tombstoned (is the folder offline or moved?)")

    outlook: dict = {"checked": calendar is not None}
    if calendar is not None:
        slack = timedelta(minutes=r["outlook_slack_min"])
        starts = [s for s in (transcript_start(p) for p in paths) if s is not None]
        meetings = calendar(start, settled)
        unrecorded = [m["subject"] for m in meetings
                      if not any(m["start"] - slack <= s <= m["end"] for s in starts)]
        outlook.update(meetings=len(meetings), unrecorded=unrecorded)

    return {"window": {"start": start.isoformat(), "settled": settled.isoformat(), "end": now.isoformat()},
            "recorder": {"configured": events["configured"], "files": events["files"], "queued": len(judged),
                         "written": len(events["written"]), "lost": lost,
                         "lost_at": [at.isoformat() for at in lost_at], "written_gone": gone_written},
            "pipeline": {"transcripts": len(paths), "unpicked": unpicked, "not_backfilled": not_backfilled},
            "episodes": {"gone": gone, "blocked": blocked, "tombstoned": []},
            "outlook": outlook, "problems": problems}


def reconcile(cfg: dict, store: Store, state: State, *, job: str, now: datetime,
              calendar: Optional[Calendar] = None, dry_run: bool = False) -> dict:
    """report(), then (unless dry_run) tombstone the gone episodes it allows and raise
    the reconcile:<job> alert for any problem. Returns the report."""
    rep = report(cfg, store, state, now=now, calendar=calendar)
    if dry_run:
        return rep
    if not rep["episodes"]["blocked"]:
        for episode_id in rep["episodes"]["gone"]:
            store.tombstone_episode(episode_id, now=now)
            rep["episodes"]["tombstoned"].append(episode_id)
    key = pipeline_run.alert_key(KIND, job)
    if not rep["problems"]:
        rep["cleared"] = pipeline_run.clear_alerts(state, [key], now=now)
    else:
        state.raise_alert(KIND, key, f"{job} reconciliation: " + "; ".join(rep["problems"]),
                          "Each problem names its check: start the recorder or the pipeline task if it is down, or "
                          "find the lost call's audio in the recordings folder; `python -m pipeline doctor` shows "
                          "the full reconciliation.", now=now)
    return rep
