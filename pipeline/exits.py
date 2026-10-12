"""The exit codes every `python -m pipeline` command returns, and how a command names an
error in its messages and log lines. Free of imports, so the SessionStart hook's path
(pipeline/alerts.py) can use them without loading the runner (pipeline/run.py), which
re-exports them for the other commands.
"""

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_PARTIAL, EXIT_REFUSED = 0, 1, 2, 3, 4


def error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"
