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
- `session_start_context` is what the hook itself uses: that alert line plus the
  graph-first note (graph_first_note.text, P2b), which goes to Claude's context only.
  The session's environment turns it on or off (graph_first_note.switch_env set to
  on_value or off_value); otherwise graph_first_note.default_on decides.
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Callable, Mapping, Optional

from kg import db
from kg.state import State
from pipeline.config import load_config
from pipeline.exits import EXIT_FAILED, EXIT_OK, EXIT_USAGE, error_text

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
        out(f"alerts could not be read: {error_text(exc)}")
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
            box["error"] = error_text(exc)

    worker = threading.Thread(target=work, name="whispr-session-start", daemon=True)
    try:
        worker.start()
        worker.join(s["timeout_s"])
    except Exception as exc:
        return f"{PRODUCT}: alerts unavailable ({_clip(error_text(exc), s['message_chars'])})."
    if worker.is_alive():
        return f"{PRODUCT}: alerts not read within {s['timeout_s']} s; run `{DOCTOR_COMMAND}`."
    if "error" in box:
        return (f"{PRODUCT}: alerts could not be read ({_clip(box['error'], s['message_chars'])}); "
                f"run `{DOCTOR_COMMAND}`.")
    return box.get("text", "")


def graph_first_note(cfg: dict, environ: Mapping[str, str]) -> str:
    """The graph-first note, or "". `environ`'s graph_first_note.switch_env picks on_value
    or off_value (letter case and surrounding space ignored); unset or any other value
    falls back to default_on. P2b's harness sets the switch in each session it starts."""
    note = cfg["graph_first_note"]
    value = (environ.get(note["switch_env"]) or "").strip().casefold()
    fold = lambda key: note[key].strip().casefold()
    on = True if value == fold("on_value") else False if value == fold("off_value") else note["default_on"]
    return note["text"].strip() if on else ""


def session_start_context(overlay_path: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None, *,
                          cfg: Optional[dict] = None,
                          read: Callable[[dict], list[dict]] = open_alerts) -> tuple[str, str]:
    """(the SessionStart alert line or "", the graph-first note or ""). Never raises: a
    config that does not load gives session_start_text's message and no note."""
    try:
        cfg = cfg if cfg is not None else load_config(overlay_path=overlay_path)
    except Exception:
        return session_start_text(overlay_path, read=read), ""
    alert = session_start_text(overlay_path, cfg=cfg, read=read)
    try:
        note = graph_first_note(cfg, os.environ if environ is None else environ)
    except Exception:
        note = ""
    return alert, note


def session_start(overlay_path: Optional[Path] = None, *, out: Callable[[str], None] = print) -> int:
    """Print the SessionStart message, if any. Always EXIT_OK."""
    try:
        text = session_start_text(overlay_path)
        if text:
            out(text)
    except Exception:
        pass                                    # never break a session start
    return EXIT_OK
