# whispr

A Windows app that automatically records your Microsoft Teams calls and
meetings, transcribes them entirely on your own machine, and writes a
plain-text transcript. No cloud API, no account, no data leaving your
computer for the recording/transcription pipeline itself.

## Before anything else: read this

**The recording is invisible to everyone else on the call.** Teams' own
recording indicator never appears for anyone but you — the other
participants get no notification. **Muting your microphone in Teams does
not stop it either** — capture happens at the hardware level, below where
Teams' mute button operates.

Recording a call without the knowledge or consent of everyone on it may be
illegal where you are, or against your organization's policies, or both —
this varies by state/country and by employer. **Check your own
organization's recording and data-handling policies before installing or
using this.** That's your responsibility, not something this software
decides or checks for you. The installer makes you explicitly confirm you
understand this before it sets anything up.

## What it does

- Detects Teams calls/meetings automatically (no manual "start recording")
- Records both sides: your microphone and the far end (WASAPI loopback), as
  two independent streams
- Transcribes locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) —
  no network call, no API key, nothing sent anywhere
- Enriches transcripts with Outlook calendar metadata (title, attendees,
  organizer) when available
- Writes a clean markdown transcript with timestamped, speaker-labeled turns
- Starts automatically at login, sits quietly in the tray, one click to stop
  and keep or discard a recording

## Install

See **[README-INSTALL.md](README-INSTALL.md)** for the full walkthrough. Short
version: download this repo, double-click `install.cmd`, confirm the consent
notice, answer two questions (where to save files, which mic/speaker to
use), wait a few minutes on the first run. Windows x64 + the new Teams
client only — see README-INSTALL.md for the full list of known limitations
(Outlook variants, locale, ARM64, etc.).

## Why there's no built-in summarization

This is intentional, not a missing feature. whispr's local-only design means
transcript text never leaves your machine — adding an API-based
summarization step would break that guarantee. Instead, transcripts are
written with a summary placeholder for *you* to fill in however you like:
by hand, or by pointing your own coding agent (Claude Code, or anything
else) at the transcript and the hand-off contract in
[SUMMARY_AGENT.md](SUMMARY_AGENT.md). `python -m whispr list-pending` lists
every transcript still waiting on a summary.

The codebase is deliberately config-driven and legible (see
[CLAUDE.md](CLAUDE.md)) specifically so you can adapt it — retention
policy, output format, matching rules — with your own agent rather than
needing the original author to do it for you.

## Architecture at a glance

```
whispr/
  config.yaml     # every tunable (paths, device matching, model, thresholds)
  whispr/         # watcher -> capture -> transcribe -> output pipeline
  transcripts/    # where finished .md transcripts land
```

Full module-by-module breakdown, key invariants, and the build history are
in [CLAUDE.md](CLAUDE.md), [PLAN.md](PLAN.md), and [UNCERTAINTIES.md](UNCERTAINTIES.md).

## License

All rights reserved — see [LICENSE](LICENSE).
