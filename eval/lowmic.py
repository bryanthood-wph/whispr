"""Low mic coverage (B.3, lesson L5), derived from the recorder's own log.

The recorder logs `recording stopped (mic frames=M, loopback frames=L)` when a call
ends, timestamped to the millisecond in local time; the transcript's frontmatter
`end` is the same instant. A frame transcript joins to the nearest stop record within
eval.sample.low_mic.join_tolerance_s. It is low-mic when it lasted at least
min_duration_min and M / L < max_mic_ratio (its owner's words are largely missing,
so it can't show their tasks). The frame/sample rate cancels out of the ratio.

The recorder's log rotates (whispr/log.py), so the rotated files whispr.log.N are read
too, and the result is frozen into the frame when the frame is frozen: later rotation
can't change which transcripts are low-mic.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from pipeline.prepare import parse_iso

_STOP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) .*recording stopped "
                   r"\(mic frames=(\d+), loopback frames=(\d+)\)")
_LOG_TS = "%Y-%m-%d %H:%M:%S,%f"


class LowMicError(RuntimeError):
    pass


def _log_files(log_path: Path) -> list[Path]:
    """The live log plus its rotated backups (whispr.log.1, .2, ...)."""
    return [log_path, *sorted(p for p in log_path.parent.glob(log_path.name + ".*") if p.suffix[1:].isdigit())]


def stop_records(log_path: Optional[Path]) -> list[tuple[datetime, int, int]]:
    if not log_path:
        raise LowMicError("paths.recorder_log is not set; the eval needs the recorder's log (B.3)")
    log_path = Path(log_path)
    if not log_path.exists():
        raise LowMicError(f"recorder log not found: {log_path} (paths.recorder_log)")
    out = []
    for path in _log_files(log_path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _STOP.match(line)
                if m:
                    out.append((datetime.strptime(m.group(1), _LOG_TS), int(m.group(2)), int(m.group(3))))
    return out


def flags(items, cfg: dict) -> dict[str, Optional[bool]]:
    """id -> True (low-mic), False, or None (no stop record within the join tolerance).
    `items` need .id, .end and .duration_min."""
    rule = cfg["eval"]["sample"]["low_mic"]
    records = stop_records(cfg["paths"]["recorder_log"])
    out: dict[str, Optional[bool]] = {}
    for item in items:
        end = parse_iso(item.end)
        if end is None:
            out[item.id] = None
            continue
        end = end.replace(tzinfo=None)
        gap, best = min(((abs((r[0] - end).total_seconds()), r) for r in records), default=(None, None))
        if best is None or gap > rule["join_tolerance_s"]:
            out[item.id] = None
            continue
        _, mic, loop = best
        out[item.id] = bool((item.duration_min or 0) >= rule["min_duration_min"]
                            and loop and mic / loop < rule["max_mic_ratio"])
    return out
