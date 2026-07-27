# whispr — Build Plan

Local Teams call/meeting recorder + transcriber for this Windows 11 machine.
Viability proven 2026-07-11 (see git history / session notes). This plan incorporates the functional premortem.

## What it does

An always-on watcher detects Teams meetings and calls, records two audio streams
(your mic = "Me", Teams' render loopback = "Others"), transcribes locally after the
call with faster-whisper, enriches with Outlook calendar metadata, and writes a
vault-ready markdown transcript with YAML frontmatter.

**Meetings record automatically. Ad-hoc calls prompt Yes/No** (buffering starts
immediately; no answer = keep). Tray icon = heartbeat + mid-call kill switch.

## Decisions locked in

| Area | Decision |
|---|---|
| Trigger | WinEvent `EVENT_OBJECT_NAMECHANGE` hook on `ms-teams.exe` windows; patterns in config; pycaw audio-session state as classification backstop and end-of-call confirmation |
| Capture | `sounddevice` for mic (Plantronics by name-match, fallback default input); `soundcard` WASAPI loopback for far end; 16 kHz mono float32; **streamed to disk incrementally** |
| Endpoint discovery | Probe-then-lock at call start: brief parallel capture on all active render endpoints, lock the one with energy. Mid-call RMS watchdog re-probes if loopback silent >30 s |
| Transcription | Post-call batch, faster-whisper `small`, CPU int8, `vad_filter=True`, beam 5, language en. Low-priority worker queue (handles back-to-back calls) |
| Body format | Timestamped interleaved speaker turns: `**[HH:MM:SS] Me:** …` / `**[HH:MM:SS] Others:** …`, merged chronologically from the two streams' segment timestamps |
| Metadata | Outlook COM (classic Outlook, confirmed installed). Post-call fetch, never blocks recording. Calendar match by time-overlap scoring with `IncludeRecurrences` handled correctly. Ad-hoc calls: counterpart name parsed from `Calls \|` title |
| Summary | **LOCAL-ONLY (changed 2026-07-12).** whispr does NOT summarize. Transcripts keep placeholder summary + `topic: [unsorted]`; filling them is handed off to a local on-device agent per `SUMMARY_AGENT.md`. `python -m whispr list-pending` lists pending files. See "Local-only posture" below |
| Audio retention | WAVs deleted after successful transcription; kept on failure for retry |
| Output | `C:\github\whispr\transcripts\` — never auto-written into cohoodOBS; promotion via `/ingest` stays manual |
| Lifecycle | Per-user Task Scheduler at logon, restart-on-failure, single-instance mutex, tray icon (Stop & keep / Stop & discard), rolling log file |
| Config | `config.yaml` — all device names, title regexes, paths, model name, thresholds, API model. **No literals in code** |

## Local-only posture (no network egress)

whispr runs entirely on-device. This is a hard design constraint, adopted after a
SOC alert on the viability-test scheduled task prompted a review of what the tool
sends off the machine.

**Nothing leaves the machine.** The full pipeline is local:
- Audio capture — local (sounddevice + soundcard, WASAPI).
- Transcription — local faster-whisper on CPU. `transcription.local_files_only: true`
  forces use of the already-cached `small` model and blocks any HuggingFace download;
  a missing model fails loudly instead of silently fetching.
- Calendar metadata — local Outlook COM.
- Transcript files — written locally to `transcripts/`.

**No summarization egress.** The earlier design made one Anthropic API call per
transcript to generate the summary + topics, which sent transcript text off-device.
That is removed:
- `summary.enabled: false` in config.
- `summarize.py` has no `anthropic` import and no HTTP client — it only does local
  text patching. Summaries/topics stay as placeholders for a local process to fill.
- The `anthropic` package (and its `httpx`/`jiter` stack specific to it) was
  uninstalled from the venv.

**Auditable claim:** `grep -rniE "import (anthropic|httpx|requests|urllib3|aiohttp)|messages.create|api_key" whispr/` returns nothing. (`httpx` remains installed only as a
transitive dependency of `huggingface_hub`, which faster-whisper uses to resolve the
*local* model cache; with `local_files_only: true` it makes no network calls, and no
whispr module imports it.)

**Still the user's call, not the code's:** whether recording Teams calls is permitted
at all (all-party consent, corporate recording policy) is a compliance/legal question
to clear with the right contacts before running for real. Out of scope for the code.

## Accepted limitations (documented, not solved)

- Laptop-speaker mode blurs channel split (mic hears speakers → "Me" contains everyone). Frontmatter records the active output device so label quality is known. Headset = clean.
- Mic channel records even when muted in Teams (hardware-level capture). Kill switch is the control.
- No per-person labels within "Others" (diarization rejected — gated models).
- Screen-locked prompt: ad-hoc call answered while locked can't show the Yes/No box → defaults to record (same as no-answer).

## Transcript file spec

Filename: `YYYY-MM-DD-HHMM-<sanitized-title-slug>.md` (illegal chars stripped; time prevents collisions).

```yaml
---
# — vault source-note schema (cohoodOBS compatible) —
date: 2026-07-14                 # call date, local
source: meeting
topic: [unsorted]                # replaced by Claude post-process
status: raw
confidence: working
# — whispr call fields —
call_title: "Weekly AI Advantage Sync"
call_type: meeting               # meeting | call
start: 2026-07-14T10:00:00-04:00
end: 2026-07-14T10:47:12-04:00
duration_min: 47
organizer: "Jane Doe"
attendees: ["Jane Doe", "John Smith", "Colby Hood"]
invite_notes: "Agenda: review pilot metrics…"   # invite body excerpt, truncated ~500 chars
metadata_source: outlook         # outlook | window-title | none
output_device: "Headset Earphone (Plantronics Blackwire 5220 Series)"
mic_device: "Headset Microphone (Plantronics Blackwire 5220 Series)"
model: small-int8
partial: false                   # true if recording was cut off (crash/sleep/kill)
whispr_version: 0.1.0
---

> One-sentence summary (Claude post-process; placeholder until then).

**[00:00:04] Others:** …
**[00:00:19] Me:** …
```

Vault-ready by construction: the five source-note keys match `cohoodOBS/schema.md`
exactly; extra keys are ignored by the vault commands. Blockquote summary is the
required first body line.

## Architecture

```
whispr/
  config.yaml            # devices, regexes, paths, model, thresholds
  whispr/
    __main__.py          # entrypoint: mutex, tray, watcher, queue
    watcher.py           # WinEvent hook + message pump; pycaw backstop
    capture.py           # dual-stream recorder, endpoint probe, RMS watchdog, incremental WAV
    transcribe.py        # queue worker: whisper, segment merge, turn formatting
    metadata.py          # Outlook COM calendar match; window-title fallback
    summarize.py         # pending-summary tracker (local, read-only); see SUMMARY_AGENT.md
    output.py            # frontmatter build, filename sanitize, atomic write
    tray.py              # pystray icon, state, stop&keep / stop&discard
  transcripts/           # output (gitignored if repo ever gets a remote)
  recordings/            # in-flight + failed WAVs (auto-cleaned on success)
  logs/whispr.log        # rolling
  PLAN.md
```

New dependencies beyond current venv: `pystray` + `Pillow` (tray), `pywin32` (Outlook COM,
WinEvent, mutex, MessageBox), `pyyaml`.

## Build phases

Each phase ends with a live verification — no phase is "done" on code alone.

**Phase 0 — Trigger instrumentation (before any trigger code)**
Title-logger script hooks `EVENT_OBJECT_NAMECHANGE` on `ms-teams.exe` and logs every
title transition. Run through: one scheduled meeting (join → leave), one ad-hoc call
(start → end), window popped out, window minimized. Output → the exact regexes that go
in `config.yaml`. *Verify: both event types produce unambiguous start/end signatures.*

**Phase 1 — Capture engine**
`capture.py`: endpoint probe-then-lock, dual-stream record, incremental WAV writes,
RMS watchdog with re-probe, clean stop + partial finalize. CLI harness to record N
seconds on demand. *Verify: pull headset mid-recording → watchdog recovers; kill the
process mid-recording → partial WAV is playable.*

**Phase 2 — Watcher + prompt + tray**
`watcher.py` (hook + message pump + start/stop state machine, pycaw end-confirmation),
call-type classification, topmost Yes/No box on non-blocking thread, `tray.py` with
kill-switch actions, single-instance mutex. *Verify: real meeting auto-records; real
call prompts; tray discard deletes; two launches → second exits.*

**Phase 3 — Transcription pipeline**
`transcribe.py`: low-priority queue, per-stream whisper pass, segment merge into
timestamped turns, empty/no-speech stub handling, `output.py` write, WAV cleanup on
success. *Verify: transcribe the two Phase-1 test WAVs → correctly interleaved turns;
feed a silent WAV → stub, no hallucinated text.*

**Phase 4 — Outlook metadata**
`metadata.py`: COM connect (running instance or cold-start with retry), calendar
restrict with `IncludeRecurrences` + sort, overlap scoring, attendee display names
(addresses only if the object-model guard allows), invite-body excerpt, window-title
fallback. *Verify: match a real recurring meeting occurrence (right date, not series
master); ad-hoc call yields counterpart name; no calendar match → `metadata_source: none`.*

**Phase 5 — Summary post-process** _(superseded — see "Local-only posture" section above)_

whispr does not summarize. `summarize.py` is a read-only pending tracker;
`python -m whispr list-pending` lists transcripts still awaiting a local summary.
See SUMMARY_AGENT.md for the on-device summary agent workflow.

**Phase 6 — Lifecycle**
Task Scheduler per-user logon task (restart-on-failure), log rotation, end-to-end
dry run. *Verify: reboot → tray appears; full real meeting end-to-end → vault-ready
transcript with Outlook metadata, WAVs gone.*

**Phase 7 — Live soak**
Run for a week of real calls. Review label quality (headset vs speakers), metadata hit
rate, any unclassified-title log entries. Tune regexes/thresholds in config only.

## Premortem register (what shaped the design)

| # | Failure imagined | Countermeasure (phase) |
|---|---|---|
| 1 | Meeting title pattern wrong → silently never records | Phase 0 instrumentation; config regexes; pycaw "unclassified activity" logging |
| 2 | Teams renders to a different endpoint / device switch mid-call → silent far-end | Probe-then-lock + RMS watchdog re-probe (1) |
| 3 | Crash mid-call loses recording; RAM blow-up on long calls | Incremental disk streaming; partial finalize (1) |
| 4 | Watcher dies silently for weeks | Tray-as-heartbeat, task restart, mutex, log (2, 6) |
| 5 | Whisper hallucinates on silence | VAD + no-speech threshold + empty stub (3) |
| 6 | Recurring-meeting COM returns series master; Outlook cold-start; address guard | IncludeRecurrences handling, retries, names-only fallback (4) |
| 7 | Summary API offline → transcript lost/blocked | Transcript-first ordering, degrade + retry (5) |
| 8 | Back-to-back calls contend for CPU | Async low-priority queue (3) |
| 9 | Title chars break filenames; same-title collisions | Sanitize + HHMM in name (3) |
| 10 | Speaker-mode label blur; muted-mic capture surprise | Documented; devices recorded in frontmatter |
