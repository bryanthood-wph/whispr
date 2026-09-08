# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```powershell
# Activate venv (required before any python command)
.venv\Scripts\python.exe -m ...

# Run all tests
.venv\Scripts\python.exe -m unittest discover -s tests -v

# Run a single test class
.venv\Scripts\python.exe -m unittest tests.test_core.TestWatcherSuppression -v

# Run the app (tray + watcher, requires live Teams)
.venv\Scripts\python.exe -m whispr

# Quick audio hardware check (records N seconds, no Teams needed)
.venv\Scripts\python.exe -m whispr record-test 3

# List transcripts awaiting local summary
.venv\Scripts\python.exe -m whispr list-pending

# Summarize crashes + auto-discarded short sessions from logs/incidents.jsonl
.venv\Scripts\python.exe -m whispr doctor [DAYS]
```

There is no build step, linter config, or CI pipeline — tests are stdlib `unittest` only, no pytest.

## Architecture

whispr is a Windows-only always-on recorder for Microsoft Teams. Everything runs locally; nothing leaves the machine.

**Entry point and orchestrator:** `whispr/__main__.py` — `Orchestrator` wires watcher, capture, transcription queue, and tray together. `_finish(keep)` is the shared stop path used by both the watcher thread (call ended) and the tray thread (user kill-switch). A session shorter than `trigger.min_recording_seconds` (config) is auto-discarded here instead of transcribed — guards against a Teams pre-join preview window briefly flashing a session-shaped title with real audio. `sys.excepthook`/`threading.excepthook` are installed in `_install_crash_logging` so an unhandled exception anywhere leaves a trace (whispr runs under `pythonw.exe`, which has no console — previously a crash left zero evidence).

**Watcher** (`watcher.py`) — sets a `EVENT_OBJECT_NAMECHANGE` WinEvent hook on `ms-teams.exe` windows. Title changes drive a state machine: `session` titles start recording, `end` titles stop it. A pycaw-based backstop thread polls for audio-session absence as a secondary stop trigger. `classify_title` and `session_subject` are pure functions (unit-testable). `_do_start`/`_do_stop`/`notify_stopped` guard `_recording` and `_active_subject` under `_state_lock`.

**Capture** (`capture.py`) — `DualStreamRecorder` records two streams: mic via `sounddevice`, Teams loopback via `soundcard` WASAPI. Probes all active render endpoints at call start and locks the one with audio (needed because Teams routes audio to machine-specific endpoints, not the Windows default). An RMS watchdog re-probes mid-call if the locked endpoint goes silent. Both streams write incrementally to WAV so a crash leaves a playable partial.

**Transcription** (`transcribe.py`) — low-priority background queue; post-call batch transcription with faster-whisper `small` on CPU int8. Segments from the two streams are merged into chronological speaker turns (`_merge_turns`). `TranscriptionQueue` never blocks the watcher thread.

**Metadata** (`metadata.py`) — post-call Outlook COM fetch. Matches the recording's time window to a calendar item via `IncludeRecurrences` + overlap scoring. `is_live_meeting` is called on the watcher hot path (1 Outlook connect retry, fail-fast) to classify meeting vs. ad-hoc call.

**Output** (`output.py`) — builds YAML frontmatter + transcript body, writes atomically to `transcripts/`. `VAULT_SOURCE = "meeting"` is intentionally constant for all call types (vault schema taxonomy).

**Tray** (`tray.py`) — `TrayController` runs the pystray icon on the main thread. Stop&keep / Stop&discard / Quit callbacks are all wrapped in `try/except … finally` so a raising orchestrator can't strand the tray.

**Shared helpers** (`winutil.py`) — `com_initialized()` context manager (wraps `CoInitialize/CoUninitialize`), `process_name_for_hwnd`, `window_title`. Any thread that touches COM or WASAPI must use `com_initialized()`. `models.py` — dataclasses: `CallSession`, `CaptureResult`, `TranscriptResult`, `Turn`, `CallMetadata`; `PENDING_SUMMARY_TOPIC = "unsorted"`.

**Config** (`config.yaml`) — single source of truth for all tunables. `config.py::load_config()` resolves relative paths and creates directories. No literals from config are duplicated in code.

**Incidents** (`incidents.py`) — append-only `logs/incidents.jsonl`, separate from the rotating `whispr.log`, for the two rare-but-actionable event kinds worth surfacing without reading the whole log: `crash` and `short-session-discarded`. Never raises. `python -m whispr doctor` reads it back into a summary.

## Key constraints

- **Local-only:** no network egress. No `anthropic` import anywhere in `whispr/`. `transcription.local_files_only: true` blocks HuggingFace downloads. Summaries are left as placeholders for a local agent (see `SUMMARY_AGENT.md`).
- **Do not restructure `capture.py`'s endpoint probe/rescue/`_loop_had_audio` latch logic** — it encodes three live-verified fixes. Only rename constants or add phase comments when touching that file.
- **State machine invariant:** `_recording` and `_active_subject` in `TeamsWatcher` are always mutated together under `_state_lock`. `_do_stop` clears `_active_subject` BEFORE `notify_stopped` runs — that ordering is what lets `notify_stopped` distinguish a tray kill-switch stop from a watcher-detected end.
- **`notify_stopped()` must remain idempotent** — it is called unconditionally from `_finish()` which is reachable from both the tray and watcher paths.
- **COM threads:** `_run`, `_audio_backstop_loop`, and all Outlook-touching code run inside `with com_initialized():`.

## This repo vs. the author's full personal environment

**This repository is PRIVATE** (made private 2026-08-27). It holds two things
that were previously kept apart:

- `whispr/` itself, plus the machinery that packages it for others to install
  (`README-INSTALL.md`, `install.cmd`/`install.ps1`, `scripts/run_installer.py`,
  `scripts/build-dist.ps1`, and the `v0.1.0` GitHub Release asset). That still
  works, but none of it is publicly reachable any more — a recipient needs
  access to this repo.
- `scripts/` — the author's own scheduled jobs: the recorder watchdog, the task
  registration, and the nightly jobs that push transcript summaries into a
  private vault over an enterprise-governed API (a **local-only-breaking**,
  off-machine sync — the opposite of `whispr/`'s own local-only constraint).

Only the storage location changed. `whispr/`'s local-only constraint is
unchanged and still enforced in code, and the ops scripts are still no part of
the installed distribution — `build-dist.ps1` stages only `wheels/`, `python/`,
`get-pip.py` and `model/`, so a recipient gets neither the watchdog nor the
sync tasks. Before 2026-08-27 these scripts were kept out of the repo entirely,
for one reason only: it was public.

**Guardrail — if this repo is ever made public again, extract `scripts/` first.**
It carries enterprise-API plumbing and per-run cost figures that must not be
published. A `/scripts/*` deny-by-default block in `.gitignore` enforced that
until the repo went private, at which point the block was removed as redundant.
Nothing enforces it now except this paragraph, so reinstate the block *before*
flipping visibility, not after.

**Never run the sync jobs by invoking the script directly from inside a Claude
Code session.** They shell out to the `claude` CLI, and a nested invocation
inherits the parent session's environment — `CLAUDECODE=1`,
`CLAUDE_CODE_CHILD_SESSION=1`, `ANTHROPIC_BASE_URL`, the messaging socket and
token. The nested call dies instantly: `is_error: true`, `duration_api_ms: 0`,
zero tokens, empty stderr, and the job reports `/lint call failed or returned an
empty result` (observed 2026-09-08, and the same signature on 2026-08-03). The
job is fine; the environment is not. Task Scheduler supplies a clean one, so run
them that way instead — it also exercises the real registered action:

```powershell
Start-ScheduledTask -TaskName whispr-weekly-lint-compile   # then watch logs/
```

`-DryRun` is safe from anywhere: it makes no `claude` call at all.

## App auto-start — two different mechanisms, don't conflate them

**On the author's dev machine, whispr is started by the `whispr-recorder`
scheduled task**, whose action is `scripts/watch-recorder.ps1` (a liveness
watchdog), *not* `pythonw.exe` directly. Trigger: at logon **plus a 15-minute
repetition**. The watchdog is the recorder's single start path — every cycle it
checks whispr's own single-instance mutex (`_acquire_single_instance`) and
launches only if no instance holds it. Registered by `scripts/register-task.ps1`
alongside the three sync tasks.

Both `scripts/watch-recorder.ps1` and `scripts/register-task.ps1` are **in this
repository** as of 2026-08-27. They were author-machine-only until then, kept
out while the repo was public (see the section above); they are still not part
of the installed distribution.

Why the repetition exists (2026-08-23 outage): whispr was terminated externally
(`0x40010004`, no crash logged) and stayed dead **three days**. The task's only
trigger was `-AtLogOn`, and the machine neither rebooted nor logged off in that
window, so nothing ever re-fired. `RestartCount` does not cover this — see
`register-task.ps1`'s 2026-07-17 finding, re-confirmed by the outage. Two load-
bearing details in `watch-recorder.ps1`: it detects via the **mutex**, not a
process scan (`pythonw.exe -m whispr` matches both the venv stub and the real
app), and it launches via **`Win32_Process::Create`**, which parents the new
process to the WMI host — outside the task's job object, so terminating the
watchdog can't take the recorder down with it.

**In the distributed installer, whispr starts from a Startup-folder shortcut**
(`%APPDATA%\...\Startup\whispr.lnk`), created by `create_startup_shortcut()` in
`scripts/run_installer.py` via `win32com.client` (`WScript.Shell`). It points at
`run-whispr.cmd` — a thin wrapper, not `pythonw.exe` directly — because a `.lnk`
has no way to set an environment variable, and the launch needs
`PYTHONNOUSERSITE=1` every time (see "Embeddable Python isolation" below). The
watchdog above is **not** part of the installed distribution.

Historical note, now contradicted: this file previously stated that `-AtLogOn`
triggers were empirically blocked by policy (2026-07-17) for the author's
non-admin account, which is why the Startup shortcut was chosen. That no longer
matches observation — an `-AtLogOn` trigger demonstrably fired on 2026-08-23
06:19:33. Either the policy changed or the original isolation missed a variable.
`register-task.ps1` now reads the trigger back after registering rather than
trusting either account.

## Embeddable Python isolation (install.ps1)

The distributed installer bundles its own embeddable Python rather than
requiring one pre-installed. Two non-obvious things had to be fixed after a
live install test surfaced them (not caught by code review alone):

- An embeddable distribution's `._pth` file resolves every path relative to
  the file's OWN directory (i.e. `python\`), not the process's working
  directory — so `-m whispr` failed with `No module named whispr` even
  though `whispr\` lives one level up. `install.ps1` appends a `..` line to
  the `._pth` file to fix this, since `whispr\` is one directory above
  `python\`.
- Enabling `import site` (required for pip/site-packages to work at all)
  also enables `site.ENABLE_USER_SITE` by default, which pulls in *any*
  pre-existing per-user Python installation on the recipient's machine —
  defeating the entire point of bundling an isolated interpreter. Fixed by
  setting `PYTHONNOUSERSITE=1` for every `python.exe` invocation, both during
  install (`install.ps1`) and at runtime (`run-whispr.cmd`).

## Testing notes

`tests/test_core.py` is the only test file. `setUpModule` redirects `CFG["paths"]["transcripts"]` to a `tempfile.TemporaryDirectory` so tests never touch the real transcripts directory.

Live behaviors that cannot be unit-tested (require a real Teams call) are tracked as "live gates" in `REVIEW_FIXES.md`.
