"""Claude Code SessionStart hook: show whispr's open alerts when a session starts.

Prints nothing when no alert is open. Otherwise prints one JSON object: `systemMessage`
(shown to you) and `hookSpecificOutput.additionalContext` (the same line, so Claude knows
too). The message comes from pipeline/alerts.py (`python -m pipeline alerts
--session-start`), which reads the database read-only on a bounded thread.

Where whispr is. hooks.json runs this file with the plugin's pinned interpreter (its
`python` option, exec form, `-s`: never the python on PATH, never a per-user
site-packages). The whispr folder is the plugin's `whispr_root` option, which Claude Code
exports to hook processes as CLAUDE_PLUGIN_OPTION_WHISPR_ROOT; left unset, the folder
above this plugin (the plugin loaded in place from a checkout or an install). The
overlay is the `config` option (CLAUDE_PLUGIN_OPTION_CONFIG), or whispr's default.
When no whispr install is found there, or an option names an unusable path, it prints
one short line saying so, never nothing: an alert surface that is silently absent is
the failure it exists to prevent (lesson L2).

Hook security (user rules, all five):
1. stdin is parsed inside try/except, and every field read is null-checked; malformed
   or missing input is ignored and the hook still exits 0.
2. No shell and no subprocess: nothing is interpolated into a command.
3. The two paths it takes from its environment (the whispr folder, the overlay) are
   refused if any segment is `..`, then resolved; the folder must hold
   pipeline/__main__.py, and the module imported must resolve under that folder (prefix
   check), so nothing outside it is run. The checks are lib/whispr_paths.py's, shared
   with bin/pipeline.py.
4. Absolute paths only: every path is resolved before use; hooks.json names this script
   through ${CLAUDE_PLUGIN_ROOT}.
5. It opens no sensitive file: an overlay named like .env*, a key or certificate,
   credentials.* or settings.local.json, or under .git/, is refused (SENSITIVE), and
   the overlay must be YAML. It reads only that overlay and whispr's database, read-only.
It always exits 0: whispr must never block or break a session start.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

EVENT = "SessionStart"
PRODUCT = "whispr"
ROOT_ENV = "CLAUDE_PLUGIN_OPTION_WHISPR_ROOT"     # the plugin's whispr_root option
OVERLAY_ENV = "CLAUDE_PLUGIN_OPTION_CONFIG"       # the plugin's config option
LIB = Path(__file__).resolve().parents[1] / "lib"
OVERLAY_SUFFIXES = (".yaml", ".yml")
SETUP_HINT = "set the whispr plugin's options (/config), or run /whispr-setup"


def read_event(stream) -> dict:
    """The hook input, or {} when it is missing or malformed."""
    try:
        raw = stream.read() if stream is not None else ""
        event = json.loads(raw) if raw and raw.strip() else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return event if isinstance(event, dict) else {}


def overlay_path(paths, environ) -> Optional[Path]:
    """The config option's overlay, or None for whispr's default. Refused when unusable."""
    raw = (environ.get(OVERLAY_ENV) or "").strip()
    if not raw:
        return None
    path = paths.checked_path(raw, "config overlay")
    if paths.sensitive(path) or path.suffix.lower() not in OVERLAY_SUFFIXES:
        raise paths.Refused(f"the config overlay {path} is not a YAML overlay this hook will read")
    return path


def message(environ=None) -> str:
    """The alert line, "" when none is open, or one line saying why alerts can't be read."""
    environ = os.environ if environ is None else environ
    try:
        sys.path.insert(0, str(LIB))
        import whispr_paths as paths
    except Exception as exc:                        # the plugin folder is incomplete
        return f"{PRODUCT}: alerts unavailable: the plugin's lib/whispr_paths.py does not load ({exc})."
    try:
        root = paths.whispr_root(environ.get(ROOT_ENV), "the plugin's whispr folder option")
        overlay = overlay_path(paths, environ)
        paths.put_first(root)
        from pipeline import alerts
        paths.loaded_from(root, alerts)
    except paths.Refused as exc:
        return f"{PRODUCT}: alerts unavailable: {exc}; {SETUP_HINT}."
    except Exception as exc:                        # e.g. this interpreter lacks whispr's dependencies
        return (f"{PRODUCT}: alerts unavailable: {type(exc).__name__} importing pipeline ({exc}); "
                f"is the plugin's Python option whispr's own python.exe? {SETUP_HINT}.")
    return alerts.session_start_text(overlay)


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
