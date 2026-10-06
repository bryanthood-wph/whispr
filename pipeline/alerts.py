"""python -m pipeline alerts: the one alert surface (docs/plan/D-architecture-and-ops.md D.6).

- `alerts` lists every open alert (kg.state: neither acknowledged nor expired) with its
  fix, most recent first. Exit 0, or 1 when the database cannot be read.
- `alerts --ack KEY` acknowledges one open alert by dedupe key (as listed) or id.
  Exit 0 acknowledged, 2 no open alert has that key. An alert that recurs is reopened
  by its next raise, so acknowledging never hides a problem that comes back.
- `alerts --session-start` is what the Claude Code SessionStart hook
  (plugin/hooks/session_start.py) shows: one short message when alerts are open,
  nothing at all when none are. It never raises and always exits 0, reads the
  database read-only (kg.db.connect_readonly) on a daemon thread bounded by
  alerts.session_start.timeout_s, and turns any failure (bad config, a missing or
  broken database, a slow disk) into a short message saying so, so a session start is
  never blocked or broken by whispr.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Callable, Optional

from kg import db
from kg.state import State
from pipeline import run as pipeline_run
from pipeline.config import load_config
from pipeline.run import EXIT_FAILED, EXIT_OK, EXIT_USAGE

PRODUCT = "whispr"
LIST_COMMAND = "python -m pipeline alerts"
DOCTOR_COMMAND = "python -m pipeline doctor"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:max(limit - 3, 0)] + "..."


def open_alerts(cfg: dict) -> list[dict]:
    with contextlib.closing(db.connect_readonly(cfg)) as conn:
        return State(conn, cfg).open_alerts()


def list_alerts(cfg: dict, *, out: Callable[[str], None] = print) -> int:
    try:
        alerts = open_alerts(cfg)
    except Exception as exc:
        out(f"alerts could not be read: {pipeline_run._error(exc)}")
        return EXIT_FAILED
    if not alerts:
        out("no open alerts")
        return EXIT_OK
    for a in alerts:
        out(f"{a['dedupe_key']}  ({a['kind']}, seen {a['count']}x, last {a['last_seen']})")
        out(f"  {a['message']}")
        out(f"  fix: {a['fix']}")
    out(f"{len(alerts)} open alert(s); acknowledge one with `{LIST_COMMAND} --ack KEY`")
    return EXIT_OK


def acknowledge(cfg: dict, key: str, *, out: Callable[[str], None] = print) -> int:
    with contextlib.closing(db.connect(cfg)) as conn:
        done = State(conn, cfg).acknowledge(key)
    if not done:
        out(f"no open alert has the key or id {key!r} (`{LIST_COMMAND}` lists them)")
        return EXIT_USAGE
    out(f"acknowledged {key}")
    return EXIT_OK


def summary(cfg: dict, alerts: list[dict]) -> str:
    """The session-start message for `alerts`: empty when there are none."""
    if not alerts:
        return ""
    s = cfg["alerts"]["session_start"]
    shown = [f"{a['dedupe_key']}: {_clip(a['message'], s['message_chars'])}" for a in alerts[:s["max_alerts"]]]
    more = len(alerts) - len(shown)
    return (f"{PRODUCT}: {len(alerts)} open alert(s). " + " | ".join(shown)
            + (f" | and {more} more" if more > 0 else "") + f". Fixes: `{LIST_COMMAND}`.")


def session_start_text(overlay_path: Optional[Path] = None, *, cfg: Optional[dict] = None,
                       read: Callable[[dict], list[dict]] = open_alerts) -> str:
    """The SessionStart message, or "" when no alert is open. Never raises."""
    try:
        cfg = cfg if cfg is not None else load_config(overlay_path=overlay_path)
        s = cfg["alerts"]["session_start"]
    except Exception as exc:
        # No config, so no length limit: name the error only; doctor prints it in full.
        return f"{PRODUCT}: alerts unavailable: the config does not load ({type(exc).__name__}); run `{DOCTOR_COMMAND}`."
    box: dict = {}

    def work() -> None:
        try:
            box["text"] = summary(cfg, read(cfg))
        except Exception as exc:
            box["error"] = pipeline_run._error(exc)

    worker = threading.Thread(target=work, name="whispr-session-start", daemon=True)
    try:
        worker.start()
        worker.join(s["timeout_s"])
    except Exception as exc:
        return f"{PRODUCT}: alerts unavailable ({_clip(pipeline_run._error(exc), s['message_chars'])})."
    if worker.is_alive():
        return f"{PRODUCT}: alerts not read within {s['timeout_s']} s; run `{DOCTOR_COMMAND}`."
    if "error" in box:
        return (f"{PRODUCT}: alerts could not be read ({_clip(box['error'], s['message_chars'])}); "
                f"run `{DOCTOR_COMMAND}`.")
    return box.get("text", "")


def session_start(overlay_path: Optional[Path] = None, *, out: Callable[[str], None] = print) -> int:
    """Print the SessionStart message, if any. Always EXIT_OK."""
    try:
        text = session_start_text(overlay_path)
        if text:
            out(text)
    except Exception:
        pass                                    # never break a session start
    return EXIT_OK
