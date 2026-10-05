# F. Transcription (ASR) and live-assist feasibility

> **One screen.** For `retention.audio_keep_days` (14) we keep new calls'
> audio. It is deleted when Phase 5 finishes, and a scheduled task enforces
> a hard maximum age. On that audio we compare three local Whisper models, plus
> vocabulary hints from Outlook, at $0 model spend. Then we measure whether
> live chunked transcription is fast enough for in-meeting help.
>
> **Limit:** better ASR helps only new calls. Audio for the existing 281 is
> gone, so the rebuild relies on the cleaning step (Appendix D.4) to fix
> known misspellings.

## F.1 Collection (Phase 0 → Phase 5)

- **Keep new calls' audio for `retention.audio_keep_days` (14).**
  - Changing production config needs a gate-0 hotfix on `master` (G.2):
    `retention.delete_audio_on_success: false`, plus one deletion task.
  - **On day 14 the flag resets**, so new calls are no longer kept.
  - **Kept audio is deleted when Phase 5 exits.** It goes no later than
    `retention.audio_max_age_days` (21), when the scheduled task deletes it
    and raises an alert. Colleagues' voices must not linger.
  - Until the Phase 8 alert surface exists, an overdue deletion shows up as
    a failure in the existing freshness check.
- **Deletion never removes a WAV that has no transcript** (lesson L14).
  Those are orphans from a crashed queue, and the deletion job hands them
  to the retry path instead.
- **Size:** about 4.5 GB over 14 days; free disk is 200 GB.

## F.2 What is compared ($0 model spend)

- **Models:** `small` (current), `large-v3-turbo`, `distil-large-v3`. Each
  needs one deliberate download, approved at a gate, and then
  `local_files_only` goes back on.
- **Vocabulary hints** (lesson L11). Each model is also run with
  `initial_prompt` / hotwords built from the meeting's Outlook subject and
  attendee names. Today "Teradyne" is transcribed 0 times against 117
  misspellings.
- **Measures:**
  - real-time factor and CPU%, including during a live Teams call
  - proper-noun error rate against Outlook spellings, which are free ground
    truth
  - disagreement WER between models, reported as disagreement, not accuracy
- **Downstream effect:** 10 calls are summarized from each ASR variant with
  the winning summarizer, and my-task recall is compared (Appendix B.9,
  stage 6, ~$5).

## F.3 Live-assist feasibility (measure only, no build)

The measured real-time factor of `small` is 0.32, so 10 s of audio
transcribes in about 3.2 s.

On the kept audio, measure:
1. WER and proper-noun error for 5, 10 and 20 s chunks vs a whole file.
2. Real-time factor and CPU during a call.
3. Projected latency: chunk + transcription + Claude call.
4. Cost per meeting-hour at a 30 s and a 60 s trigger cadence.

If it is viable, the later design would be:
- a separate process outside `whispr/`, so the recorder stays local-only
- it reads the graph through MCP
- it shows suggestions in a Claude Code view from the same plugin

**Gate before any build:** Deloitte policy on live AI assistance in client
meetings.
