# plugin/hooks/ — the session-start alert hook

What it does: when a Claude Code session starts or resumes, shows whispr's open alerts in one short line (`systemMessage`, and the same line as context for Claude); shows nothing when none are open, and never blocks or fails a session (exit 0 always, database read bounded by `alerts.session_start.timeout_s`).
How to run it: packaged, the plugin picks up `hooks/hooks.json` itself. Until then, add the same command to the `SessionStart` hooks in your Claude Code `settings.json`, with the absolute path: `python "<repo>\plugin\hooks\session_start.py"` (the `python` must be one with whispr's dependencies, e.g. `<repo>\.venv\Scripts\python.exe`). By hand: `python -m pipeline alerts --session-start` prints the same line.
Tests: `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_pipeline_alerts.py" -v` (malformed stdin, no alerts, alerts, broken database).
Config keys read: `alerts.session_start.*` (count, length, timeout), plus what `kg/` reads to open the database.

The hook follows the five hook-security rules (stdin parsed in try/except with fields null-checked, no shell, no user-supplied path read, absolute paths, no sensitive file); `session_start.py`'s docstring says how each is met. `hooks.json`'s `timeout` (seconds) must exceed `alerts.session_start.timeout_s` plus interpreter start-up.
