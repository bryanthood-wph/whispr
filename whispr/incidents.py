"""Durable, greppable record of rare-but-actionable events (crashes, auto-discards).

whispr.log rotates and is meant for chronological debugging; incidents.jsonl is a
small, append-only side channel for the handful of event kinds worth surfacing
without reading the whole log. `python -m whispr doctor` reads it back.

Never raises: a logger that can crash the app defeats its purpose.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from whispr.log import get_logger

log = get_logger("incidents")

_FILENAME = "incidents.jsonl"


def record_incident(cfg: dict, kind: str, **fields: Any) -> None:
    """Append one incident record. Best-effort; swallows all errors."""
    try:
        path = Path(cfg["paths"]["logs"]) / _FILENAME
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        log.debug("record_incident failed", exc_info=True)


def read_incidents(cfg: dict, since_days: float = 7.0) -> list[dict]:
    """Return incident records newer than `since_days` ago, oldest first.

    Tolerant of a missing/unreadable file and of malformed/partial lines (e.g. a
    write that raced a crash) — those are skipped, not fatal, honoring the module's
    "never raises" contract so `whispr doctor` degrades gracefully.
    """
    path = Path(cfg["paths"]["logs"]) / _FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception:
        # Locked file, permission denial, decode error, etc. — a diagnostic reader
        # must not crash the caller over an unreadable log.
        log.debug("read_incidents could not read %s", path, exc_info=True)
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["ts"]).timestamp()
        except Exception:
            continue
        if ts >= cutoff:
            out.append(entry)
    return out
