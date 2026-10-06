"""Claude Code SessionStart hook: show whispr's open alerts when a session starts.

Prints nothing when no alert is open. Otherwise prints one JSON object: `systemMessage`
(shown to you) and `hookSpecificOutput.additionalContext` (the same line, so Claude knows
too). The message comes from pipeline/alerts.py (`python -m pipeline alerts
--session-start`), which reads the database read-only on a bounded thread.

Hook security (user rules, all five):
1. stdin is parsed inside try/except, and every field read is null-checked; malformed
   or missing input is ignored and the hook still exits 0.
2. No shell and no subprocess: nothing is interpolated into a command.
3. It reads no user-supplied path (nothing from stdin is used as a path), so there is
   no traversal to block; the repo root is this file's own resolved location.
4. Absolute paths only: the root comes from Path(__file__).resolve(); hooks.json names
   this script through ${CLAUDE_PLUGIN_ROOT}.
5. It opens no sensitive file: only whispr's own config (the default overlay) and its
   database, read-only.
It always exits 0: whispr must never block or break a session start.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVENT = "SessionStart"
ROOT = Path(__file__).resolve().parents[2]          # plugin/hooks/<this> -> the repo root


def read_event(stream) -> dict:
    """The hook input, or {} when it is missing or malformed."""
    try:
        raw = stream.read()
        event = json.loads(raw) if raw and raw.strip() else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return event if isinstance(event, dict) else {}


def message() -> str:
    """The alert line, or "" (nothing open, or whispr not found here)."""
    if not (ROOT / "pipeline" / "alerts.py").is_file():
        return ""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from pipeline import alerts
    except Exception as exc:                        # e.g. this interpreter lacks whispr's dependencies
        return f"whispr: alerts unavailable: {type(exc).__name__} importing pipeline ({exc})."
    return alerts.session_start_text()


def main() -> int:
    try:
        event = read_event(sys.stdin)
        name = event.get("hook_event_name")
        if name is not None and name != EVENT:      # wired to another event by mistake: do nothing
            return 0
        text = message()
        if text:
            print(json.dumps({"systemMessage": text,
                              "hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": text}}))
    except Exception:
        pass                                        # never break a session start
    return 0


if __name__ == "__main__":
    sys.exit(main())
