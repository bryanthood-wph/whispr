# whispr — Uncertainty Log

Open questions and assumptions logged during the build. Resolved items move to the bottom.
Format: `- [ ] (module) description — assumption made`

## Open

- [ ] (watcher — full-app live trigger, LIVE GATE) The meeting-vs-call classifier is BUILT and verified against the live Outlook calendar (see resolved note). What remains unverified is the whole trigger firing inside the running app: WinEvent title hook -> session-window detection -> pycaw audio-active gate -> is_live_meeting -> auto-record (meeting) or Yes/No prompt (call), then end detection. Needs one run of `python -m whispr` through a real meeting AND a real call.
- [ ] (compliance — USER ACTION, not code) Recording Teams calls on a Deloitte-managed machine raises all-party-consent and corporate recording-policy questions. Clear this with security/compliance/legal BEFORE running the recorder for real. A SOC alert already flagged the viability-test scheduled task (benign, self-cleaned). Not solvable in code.
- [ ] (metadata) Outlook `Restrict` filter uses `%m/%d/%Y %I:%M %p`, which is locale-sensitive. VERIFIED OK on this machine (en-US, M/d/yyyy, AM/PM). Failure mode if locale ever changes is graceful (empty Restrict -> window-title fallback, no crash) but attendees/organizer/notes would silently go missing. Revisit if the machine locale changes.

## Resolved (install 2026-07-13, evening)

- [x] (main) Single-instance mutex was BROKEN: read `kernel32.GetLastError()` as a separate ctypes call, which ctypes clobbers between calls, so a duplicate never saw ERROR_ALREADY_EXISTS. FIXED: `ctypes.WinDLL(use_last_error=True)` + `ctypes.get_last_error()`. Verified cross-process: 2nd acquire returns None.
- [x] (ops) The "two python processes per launch" is the venv launcher stub + the real interpreter, NOT two app instances — only one runs `main()` (one "watcher active", no "already running"). Confirmed after the mutex fix. Not a bug.
- [x] (lifecycle) Task Scheduler logon task installed and verified running a single clean instance. `scripts/install_task.ps1` had em-dashes that broke PowerShell parsing -> replaced with ASCII.

## Resolved (live meeting+call 2026-07-13, afternoon)

- [x] (capture) FREEZE ROOT CAUSE: watchdog/retry loops paced with `self._running.wait(timeout=1.0)`, but `_running` is *set* while recording, so `.wait()` returned instantly -> busy-spin re-probing audio devices thousands of times/sec -> WASAPI/COM deadlock (app hung mid-meeting, stopped logging). FIXED: added `_sleep()` (interruptible time.sleep) and replaced all 3 busy-spin waits. Verified: 0 "silent 30s" spam / 0 switching in idle capture.
- [x] (watcher) Backstop stopped auto-recording during the pre-join empty room (20s) and didn't re-arm. FIXED: backstop arms only after audio was once active; tolerance raised to 45s. Verified live: recording held through a rejoin/silence.
- [x] (main) Single-instance mutex verified: second acquire returns None (blocked). The two live instances seen were a kill/relaunch testing artifact, not a code bug.

## Resolved (live call 2026-07-13)

- [x] (watcher) Phase 0 done: captured real Teams titles. Meeting and ad-hoc call windows are title-identical (`<X> | Microsoft Teams`); title regex alone cannot distinguish them. RESOLVED: classify by Outlook cross-reference — `metadata.is_live_meeting(subject, now)` matches the window subject against live calendar events (match => meeting/auto-record; no match => call/prompt). Verified live: real event subject -> True, "Virkar, Jayesh" -> False. Config reworked to `session_title_suffix` + `nav_prefixes`.
- [x] (metadata) Ad-hoc calls matched a stray overlapping calendar item ("Enter MySource"). RESOLVED: `fetch_metadata` only consults Outlook for meetings; calls use the window-title counterpart.
- [x] (capture) Dual-stream capture PROVEN on a real call (mic + SoundWire loopback, continuous). Watchdog ping-ponged endpoints every ~2s, fragmenting far-end audio. RESOLVED: `_loop_had_audio` latch trusts an endpoint once it carries audio; switching only rescues a dead initial lock, bounded by a tried-dead set. Verified: 1 switch then stable.

## Resolved

- [x] (summarize — RESOLVED by going local-only) The Anthropic API summary call sent transcript text off-device. REMOVED: `summary.enabled: false`, `anthropic` uninstalled, `summarize.py` rewritten with no network client. `ANTHROPIC_API_KEY` visibility is now moot. See PLAN.md "Local-only posture".
- [x] (main/transcribe) FIXED: quit with an in-flight recording could lose the transcript because the daemon transcription worker was killed at process exit. `TranscriptionQueue.stop()` now accepts a join_timeout; `_quit` drains with a 600s timeout before exit.
