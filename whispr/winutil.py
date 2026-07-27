"""Small Windows/COM helpers shared across modules."""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
from ctypes import wintypes
from typing import Iterator, Optional

# WinEvent constants shared by watcher.py and scripts/instrument_titles.py.
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002


@contextmanager
def com_initialized() -> Iterator[None]:
    """Initialize COM for the current thread for the duration of the block.

    Any thread that touches soundcard (Media Foundation) or Outlook/pycaw (COM)
    must call CoInitialize first. Balanced CoInitialize/CoUninitialize; nesting is
    safe (CoInitialize returns S_FALSE if already initialized on this thread).
    """
    import pythoncom

    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def process_name_for_hwnd(hwnd, cache: dict) -> Optional[str]:
    """Return the lowercase process name for the process owning hwnd, caching by PID."""
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    pid_val = pid.value
    if pid_val in cache:
        return cache[pid_val]
    try:
        import psutil

        name = psutil.Process(pid_val).name().lower()
    except Exception:
        name = None
    if name is not None:
        cache[pid_val] = name
    return name


def window_title(hwnd) -> str:
    """Return the window title for hwnd, or empty string."""
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value
