"""Phase 0 — Teams window-title instrumentation (LIVE GATE).

Run this, then drive Teams through the scenarios below while it logs EVERY title
transition on ms-teams.exe windows. Use the captured titles to write the real
regexes in config.yaml (trigger.meeting_title_patterns / call_title_patterns /
end_title_patterns).

    .venv\\Scripts\\python.exe scripts\\instrument_titles.py

Scenarios to run (the log will show what each looks like):
  1. Join a scheduled meeting, then leave it.
  2. Start/receive an ad-hoc call, then end it.
  3. Pop the meeting window out; minimize/restore it.
  4. Return to Activity/Chat home.

Press Ctrl+C to stop. Output also written to logs/title-instrumentation.log.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from whispr.config import load_config  # noqa: E402
from whispr.winutil import (  # noqa: E402
    EVENT_OBJECT_NAMECHANGE,
    WINEVENT_OUTOFCONTEXT,
    WINEVENT_SKIPOWNPROCESS,
    process_name_for_hwnd,
    window_title,
)

_OBJID_WINDOW = 0
_CHILDID_SELF = 0

user32 = ctypes.windll.user32

_pid_name: dict[int, str] = {}
_last_title: dict[int, str] = {}


def main() -> int:
    cfg = load_config()
    process_name = cfg["trigger"]["process_name"].lower()
    log_path = cfg["paths"]["logs"] / "title-instrumentation.log"
    log_file = open(log_path, "a", encoding="utf-8")

    def emit(msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    WinEventProcType = ctypes.WINFUNCTYPE(
        None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
        wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
    )

    def on_event(hHook, event, hwnd, idObject, idChild, thread, ms):
        if idObject != _OBJID_WINDOW or idChild != _CHILDID_SELF or not hwnd:
            return
        try:
            if process_name_for_hwnd(hwnd, _pid_name) != process_name:
                return
            title = window_title(hwnd)
            if not title:
                return
            if _last_title.get(int(hwnd)) == title:
                return
            _last_title[int(hwnd)] = title
            emit(f"hwnd={int(hwnd):#x}  TITLE={title!r}")
        except Exception as exc:
            emit(f"(handler error: {exc})")

    cb = WinEventProcType(on_event)
    hook = user32.SetWinEventHook(
        EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE, 0, cb, 0, 0,
        WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
    )
    if not hook:
        emit("ERROR: SetWinEventHook failed")
        return 1

    emit(f"instrumentation active — watching {process_name} title changes. Ctrl+C to stop.")
    msg = wintypes.MSG()
    try:
        while True:
            # PeekMessage-driven pump so Ctrl+C (KeyboardInterrupt) is responsive.
            if user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.05)
    except KeyboardInterrupt:
        emit("stopping.")
    finally:
        user32.UnhookWinEvent(hook)
        log_file.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
