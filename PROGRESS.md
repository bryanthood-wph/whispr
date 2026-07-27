# whispr — Build Progress & Audit Checklist

This is the checklist the `/loop` auditor re-runs every iteration. It runs the
AUTOMATED checks below, records pass/fail, and stops itself once all automated
checks are green AND the quality gates (simplify/debug/code-review) are clean.
LIVE GATES require a human in a real Teams call and are never auto-completable.

Status legend: [ ] pending · [x] pass · [!] fail · [~] blocked-on-user (live gate)

## Automated checks (loop runs these every iteration)

- [x] A1. All modules import cleanly (`python -c import whispr.*`)
- [x] A2. Unit suite passes (`python -m unittest discover -s tests`) — 11 tests
- [x] A3. config.yaml loads; transcripts/recordings/logs dirs created
- [x] A4. `record-test` writes two non-empty WAVs with no COM/traceback errors (see note on transient WASAPI flake below)
- [x] A5. Transcript writer produces vault-schema frontmatter + flow-style `topic: [unsorted]`
- [x] A6. Placeholder contract locked: output emits `topic: [unsorted]` + `> _Summary pending._`, and the pending-tracker's `_UNSUMMARIZED_RE` matches them (test_placeholder_contract)
- [x] A7. `retry-summary` CLI runs without crashing on an empty transcripts dir
- [x] A8. Local-only: no network egress in whispr/ (`grep -rniE "import (anthropic|httpx|requests|urllib3|aiohttp)|messages.create|api_key" whispr/` is empty); `retry-summary` is a read-only pending-tracker

## Quality gates (run once during build, re-checked if code changes)

- [x] Q1. /simplify pass applied — extracted `com_initialized()` for capture.py's 3 COM threads; single-use sites left explicit
- [x] Q2. debug pass — capture COM-init crash on watchdog thread found & fixed; pipeline smoke clean
- [x] Q3. /code-review pass — 2 findings; #1 (quit loses in-flight transcript) FIXED via queue drain; #2 (locale Restrict) verified non-issue on en-US

## Spec coverage (every PLAN.md module exists and is wired)

- [x] S1. config.yaml — all tunables, no code literals
- [x] S2. capture.py — dual stream, probe-then-lock, RMS watchdog, incremental WAV
- [x] S3. watcher.py — WinEvent hook, classification, pycaw backstop, Yes/No prompt
- [x] S4. transcribe.py — whisper queue worker, segment merge, turn formatting
- [x] S5. metadata.py — Outlook COM overlap match + window-title fallback
- [x] S6. summarize.py — LOCAL-ONLY read-only pending-tracker (lists transcripts still `topic: [unsorted]`); summarization handed off to a local agent per SUMMARY_AGENT.md. No network.
- [x] S7. output.py — frontmatter build, slug, atomic write
- [x] S8. tray.py — icon heartbeat, Stop&keep / Stop&discard kill switch
- [x] S9. __main__.py — mutex, orchestrator, tray/watcher/queue wiring, CLI
- [x] S10. scripts/instrument_titles.py — Phase 0 instrumentation
- [x] S11. scripts/install_task.ps1 — per-user logon Task Scheduler
- [x] S12. tests/ — offline unit suite

## LIVE GATES (blocked on user — cannot be auto-verified)

- [~] L1. Phase 0: run instrument_titles.py in a real meeting + call; write real regexes into config.yaml
- [~] L2. Live trigger: meeting auto-detect + calendar classification CONFIRMED live (Neil/Colby -> meeting, silent auto-record, held through rejoin/silence). Freeze bug that hung the app mid-meeting FOUND & FIXED (busy-spin). STILL TO SEE with the fixed build: (a) a clean full meeting start->end auto-capture without freeze, (b) the ad-hoc call Yes/No prompt actually popping (app froze before we could observe it; manual captures used instead).
- [x] L3. Live capture + pipeline PROVEN (call w/ Jayesh, 2026-07-13): dual-stream captured, whisper transcribed 38 turns accurately, interleaved+timestamped, metadata via window-title, vault transcript written, fully local. Two bugs found & fixed: (1) ad-hoc metadata matched a stray calendar item -> now calls skip Outlook; (2) watchdog endpoint ping-pong -> now `had_audio` latch stops switching once audio flows (validated: 1 switch then stable/continuous).
- [~] L4. Watcher end-detection + pycaw backstop fire correctly on real call end
- [~] L5. End-to-end: real meeting -> vault-ready transcript with Outlook metadata, WAVs deleted

(L6 removed 2026-07-12: whispr no longer summarizes or sends transcript text off-device.
Local-only posture — see PLAN.md "Local-only posture". Filling the summary/topic
placeholders is a separate local-agent step per SUMMARY_AGENT.md, not a whispr gate.)

## Notes
Loop iteration (audit): A4 hit a one-off loopback open failure (WASAPI 0x8889000f) on
a rapid back-to-back run — SoundWire endpoint still releasing. Re-ran 3/3 clean, so it
was a transient flake, not a regression. It did surface a real gap: an open-failure on
the locked endpoint used to retry the same endpoint and wait up to 30s for the silence
watchdog. Fixed — capture.py now re-probes to a different endpoint after
`_MAX_OPEN_FAILURES` (2) consecutive failures. Re-verified imports+tests+capture green.

See UNCERTAINTIES.md for assumptions. Live gates are expected to stay `[~]` until
the user runs a real Teams session; the loop will report them as blocked, not failed.
