"""Run one console program with no visible window, and exit with its exit code.

Task Scheduler starts a console program such as pwsh.exe in a console window of its
own: a black box that opens on every run of a task (every 15 minutes for the recorder
watchdog). Starting the task through pythonw.exe, which has no console, and launching
the program with CREATE_NO_WINDOW gives it a hidden console instead. Its own children
(claude.exe, python.exe) inherit that hidden console, so they open no window either.
`conhost.exe --headless` also hides the window, but it always exits 0, so the task's
last result would stop showing failures. This keeps the program's exit code.

Usage (a scheduled task's action, built by register-task.ps1):
    pythonw.exe -s run-hidden.py <program> [arguments...]
"""

from __future__ import annotations

import subprocess
import sys

USAGE_EXIT = 2      # no program named: the same code argparse uses for a usage error


def main(argv: list[str]) -> int:
    if not argv:
        return USAGE_EXIT
    return subprocess.call(argv, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
