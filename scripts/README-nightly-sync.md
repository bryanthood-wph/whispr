# whispr → cohoodOBS vault sync

Two automated jobs (Windows Task Scheduler) that keep the cohoodOBS knowledge vault
current with whispr call transcripts:

| Job | Script | Schedule | Cost/latency |
|---|---|---|---|
| Nightly ingest | `nightly-ingest.ps1` | Mon–Fri, 22:00 | Cheap, fast — per-file summarize + ingest only |
| Weekly hygiene | `weekly-lint-compile.ps1` | Sunday, 22:00 | Expensive, slow — full-vault `/lint` measured at **~19 min / ~$8** |

They used to be one script. They were split because a full-vault `/lint` is too
slow/expensive to run every weeknight — see "Why two jobs" below.

## ⚠️ Egress decision (read before enabling)

whispr is designed local-only — "nothing leaves the machine" (see
[`SUMMARY_AGENT.md`](../SUMMARY_AGENT.md), which explicitly says to summarize
transcripts *locally*). **Both jobs are a deliberate, documented exception to that
posture.**

They send meeting-transcript summaries (nightly) and vault content (weekly) to the
Claude API via the Claude Code CLI. This was accepted on **2026-07-13** on the basis
that:

- Claude Code here is authenticated under the **Deloitte enterprise plan**
  (zero-retention / no-training) — corporate-governed egress, not a personal API key.
- The cohoodOBS vault **already** operates on that same enterprise-governed API for
  every `/ingest`, `/lint`, `/compile`, and `/query`. Transcripts are the same data
  category through the same pipe.

The one genuinely new consideration is that recorded-conversation transcripts may be
higher-sensitivity (all-party consent / corporate recording policy). That remains a
compliance question for the human, tracked in `UNCERTAINTIES.md` — the tooling does
not settle it.

## Why two jobs

The original single script ran the full pipeline (summarize → ingest → lint →
compile) every weeknight. A live `/lint` run over the full vault measured at **~19
minutes and ~$8** — acceptable occasionally, not acceptable as a nightly cost/latency
tax on top of the actual per-file ingest work. So the pipeline is now split:

- **`nightly-ingest.ps1`** does only the cheap, fast, per-file work: summarize a
  transcript, assemble the vault source file, `/ingest` it, advance the watermark.
  No `/lint`, no `/compile`.
- **`weekly-lint-compile.ps1`** does the expensive hygiene pass once a week: `/lint`
  the whole vault, derive Colby's currently **active workstreams** from recent
  meeting notes, extract compile candidates from the report split into a
  **workstream tier** (uncapped — everything that matches active work gets
  compiled) and a **generic tier** (capped at `-MaxCompiles`, backlog-first).
  Generic candidates that don't fit under the cap in a given run persist in an
  on-disk backlog (`logs/compile-backlog.txt`) and are drained first next time —
  nothing is silently dropped. See "Workstream-first selection" below.

Shared plumbing (claude process invocation, logging, failure handling/alerting,
frontmatter parsing) lives in one place — `sync-common.ps1` — dot-sourced by both
scripts, so none of it is duplicated between them.

## Workstream-first selection

The weekly job doesn't just compile candidates in priority order — it compiles
whatever Colby is *actively working on* first, uncapped, and treats everything
else as a lower-priority, capped, backlog-persisted queue:

1. **Active-workstream derivation (deterministic, no API call).** Enumerate
   `cohoodOBS/sources/*.md`. A note counts as an active-workstream signal if its
   frontmatter has `source: meeting` and its `date:` (or, if unparseable, the
   file's last-write time) is within `-WorkstreamDays` (default 21) of today. The
   union of those notes' `topic:` tags and `call_title` values — deduped — is the
   "active workstream signal," logged every run.
2. **Extraction (haiku)** is given both the lint report and that signal, and
   splits candidates into:
   - `workstream[]` — compile candidates or stale existing pages that clearly
     refer to the same project/entity as one of the active-workstream terms. A
     generic cross-cutting tag ("staffing", "kickoff") does *not* by itself
     qualify — only concrete projects/entities do.
   - `other[]` — the remaining genuine candidates, ranked, with broad
     catch-all umbrellas (e.g. "Deloitte") and mis-transcription noise excluded.
3. **Tier 1 (workstream) is compiled uncapped** — every item, one at a time,
   stop-on-failure. `-MaxWorkstreamCompiles` (default 25) is a **catastrophic
   backstop only**; it should never trigger in normal use. If it does, the first
   N are compiled, a prominent WARN names the rest, and they're pushed into the
   backlog rather than compiled unboundedly in one run.
4. **Tier 2 (generic) is capped and backlog-first.** The queue is
   `(persistent backlog) + (this week's other[])`, deduped, backlog entries
   first. Up to `-MaxCompiles` are compiled; after **each** successful compile the
   backlog file is rewritten to contain exactly what's still uncompiled, so a
   mid-run failure leaves an accurate on-disk backlog. Anything beyond the cap
   simply stays in the backlog file for next run.
5. **Already-compiled skip guard.** Before compiling anything (either tier), if a
   vault page already exists for the concept AND it's not in the lint report's
   "stale wiki pages" list, it's skipped and dropped (logged as
   skipped-already-done) rather than recompiled.
6. **`-BacklogOnly`** skips `/lint`, workstream derivation, and extraction
   entirely — it just drains up to `-MaxCompiles` items from the persistent
   backlog. This is the cheap, on-demand catch-up path (no ~$8 `/lint`).

## Pipeline

```
NIGHTLY (nightly-ingest.ps1), Mon-Fri 22:00:
  For each quiet, non-partial transcript newer than the watermark (oldest first):
    0. gate       (PowerShell, deterministic, no API call) — see "Idempotency" below
                   → SKIP if already ingested, INGEST-ONLY if an earlier run wrote the
                     note but its /ingest never finished, otherwise continue
    1. summarize  (haiku)  → clean paraphrased prose
    2. write      (PowerShell, deterministic) → cohoodOBS/sources/<name>.md
                   original whispr frontmatter carried over BYTE-FOR-BYTE + summary body;
                   create-only — an existing note is never overwritten
    3. /ingest    (sonnet) → topic tags + [[wiki-links]] + log.md entry
    4. append to the ingest ledger, then advance the watermark
                   ← only after this file fully succeeds (data-loss guard)
  On ANY failure: stop, fire the durable trio (below). Watermark does NOT advance
  past the failing file. An unresolvable file (no frontmatter, partial: true) is
  parked in transcripts\_needs-attention\ instead. No lint/compile runs here.

WEEKLY (weekly-lint-compile.ps1), Sunday 22:00:
  0. load backlog (PowerShell, deterministic) → logs/compile-backlog.txt
  1. derive workstreams (PowerShell, deterministic) → active topics/call-titles
                            from meeting notes within -WorkstreamDays
  2. /lint      (sonnet)  → full-vault lint report
  3. extract    (haiku)   → { "workstream": [...], "other": [...] } JSON split,
                            given both the lint report and the workstream signal
  4. /compile X (sonnet)  → Tier 1: EVERY workstream[] item, uncapped (backstop
                            -MaxWorkstreamCompiles only), one at a time, stop-on-
                            failure
  5. /compile X (sonnet)  → Tier 2: up to -MaxCompiles of (backlog + other[]),
                            backlog-first, one at a time, stop-on-failure;
                            backlog file rewritten after each success; anything
                            over the cap persists in the backlog for next run
  On ANY failure: stop, fire the durable trio (below).

WEEKLY -BacklogOnly (on-demand, no schedule):
  Skip /lint, workstream derivation, and extraction. Drain up to -MaxCompiles
  from the persistent backlog only. Cheap — no ~$8 /lint.
```

## Idempotency

The watermark only decides which transcripts are *listed*. Until 2026-09-30,
nothing decided whether a listed transcript had already been ingested, so a
re-listed one was re-summarized and written over its vault note. The transcript's
frontmatter always reads `status: raw` / `topic: [unsorted]`, so every rewrite put an
already-integrated note back to raw with a new body. During the 2026-09-09..09-23
watermark freeze that was 155 of 278 `/ingest` calls (~$75) and 9 integrated notes
rewritten. The gate (step 0) closes this with two layers, both checked before any
claude call:

1. **Ingest ledger** (`logs/nightly-ingest-ledger.jsonl`, append-only, one
   `{transcript, sha256, origin, recordedAt}` line per ingested transcript). A name
   already in the ledger is skipped at zero cost. If its sha256 differs (the
   transcript changed after ingest) it is still skipped, with a WARN line in the
   daily log and no alert. The note stays as it was ingested. On first run the
   ledger is seeded from every past `SUCCESS '<file>'` log line, which also covers
   notes that were later deleted from the vault on purpose.
2. **The vault note.** For a name missing from the ledger whose note already
   exists, the note is compared with the transcript's own frontmatter rather than
   any literal value. If `status` or `topic` differs, `/ingest` already ran, so the
   file is skipped and backfilled into the ledger. If both are unchanged, an earlier
   run wrote the note but its `/ingest` never finished, so only `/ingest` is re-run.
   If the note's frontmatter does not parse, the file is skipped and reported
   through `Invoke-JobFailure -NonFatal` (sentinel + Event Log). The note is left
   untouched and the run continues.

Result: a rewound, frozen or lost watermark now re-lists transcripts without
re-ingesting any. Rehearse that any time with
`-DryRun -WatermarkOverrideUtc 1970-01-01`: every transcript should come back
`SKIP`, with no `Would run claude step` lines. To deliberately re-ingest one
transcript, remove its ledger line **and** its vault note, then set the
transcript's `LastWriteTimeUtc` to now
(`(Get-Item <path>).LastWriteTimeUtc = (Get-Date).ToUniversalTime()`) so the
watermark lists it again. Removing only one of the two is refused by the other
layer, and without the mtime bump the watermark never lists the file at all.

**Why summaries, not raw transcripts:** the vault's compile/synthesis steps expect
clean prose source notes (see the existing `sources/*-transcript.md`), not raw
`**[HH:MM:SS] Me:**` turns. The raw transcript stays untouched in
`whispr/transcripts/` as the verbatim record; only the summary is filed.

## Files

| File | Role |
|---|---|
| `sync-common.ps1` | Shared helper module (dot-sourced by both jobs): claude-invocation plumbing, UTF-8/logging helpers, failure handling (durable trio + toast), frontmatter parsing (`Split-Frontmatter`, `ConvertFrom-YamlScalar`, `Get-FrontmatterScalar`, `Get-FrontmatterListArray`). Defines functions only — no main logic runs on load. |
| `nightly-ingest.ps1` | The nightly job (ingest only). Run with `-DryRun` to rehearse safely (no API calls, no vault writes, no watermark advance). |
| `weekly-lint-compile.ps1` | The weekly job (lint + workstream-first compile). Run with `-DryRun` to rehearse safely (no API calls, no backlog writes). `-MaxCompiles` bounds the generic tier; `-MaxWorkstreamCompiles` is a catastrophic backstop on the uncapped workstream tier; `-WorkstreamDays` sets the active-workstream window; `-BacklogOnly` drains just the persistent backlog (no `/lint`). |
| `register-task.ps1` | Registers BOTH Scheduled Tasks. Preview-only unless `-Confirm` is passed. |
| `summarize-prompt.txt` | The summarization prompt template (version-controlled so the standard is enforced every run). Tokens like `{{CALL_TITLE}}`, `{{TRANSCRIPT_BODY}}` are filled by `nightly-ingest.ps1`. |
| `../logs/nightly-ingest-<date>.log` | Nightly job's per-day activity log. |
| `../logs/weekly-lint-compile-<date>.log` | Weekly job's per-day activity log. |
| `../logs/nightly-ingest-last-run.txt` | Nightly watermark (ISO-8601 UTC of the last fully-processed file). |
| `../logs/nightly-ingest-ledger.jsonl` | Nightly ingest ledger: which transcripts have been ingested, with their sha256. It is the idempotency record, so don't delete it casually (see "Idempotency"). |
| `../logs/compile-backlog.txt` | Weekly job's persistent generic-tier compile backlog (one concept name per line). Drained backlog-first on the next run (or via `-BacklogOnly` on demand). |
| `../logs/FAILED-<timestamp>.txt` | Failure sentinel — written by either job only when a run fails. |

The `/ingest` command in cohoodOBS was also updated (one generalized rule): it now
**preserves a pre-populated frontmatter block verbatim** and only fills a placeholder
`topic:` — so the real call date and attendee metadata survive ingestion. This is not
whispr-specific; it applies to any pre-formatted capture.

## One-time setup

Run these once, in order:

1. **Create the Event Log sources (needs elevation, once):** so failure alerts can
   write to the Windows Application event log. In an **Administrator** PowerShell:
   ```powershell
   New-EventLog -LogName Application -Source 'whispr-nightly'
   New-EventLog -LogName Application -Source 'whispr-weekly'
   ```
   (If you skip this, either job still runs and still alerts via the sentinel file +
   toast; it just logs a warning that it couldn't write the Event Log.)

2. **Rehearse both:**
   ```powershell
   pwsh -NoProfile -File scripts\nightly-ingest.ps1 -DryRun
   pwsh -NoProfile -File scripts\weekly-lint-compile.ps1 -DryRun
   ```
   Confirm the candidate list, frontmatter carry-over, and per-step plan look right
   for the nightly job; confirm the lint + candidate-extraction plan looks right for
   the weekly job.

3. **One real run of each, watched** (optional but recommended):
   ```powershell
   pwsh -NoProfile -File scripts\nightly-ingest.ps1
   pwsh -NoProfile -File scripts\weekly-lint-compile.ps1
   ```
   Then check `cohoodOBS/sources/`, `cohoodOBS/log.md`, and the daily logs.

4. **Register both schedules** (preview, then confirm):
   ```powershell
   pwsh -NoProfile -File scripts\register-task.ps1            # preview
   pwsh -NoProfile -File scripts\register-task.ps1 -Confirm   # actually register both
   ```

## Operating notes

- **Run only when logged on.** Both tasks use interactive logon so the enterprise
  Claude auth session is available; neither will run in Session 0. The machine must
  be on and logged in (**locked is fine**) at the scheduled time.
- **Host availability & missed runs (laptop-resilient settings).** Both tasks are
  registered with `-StartWhenAvailable`, `-WakeToRun`, `-AllowStartIfOnBatteries`,
  and `-DontStopIfGoingOnBatteries`:
  - Asleep on AC at 22:00 → `-WakeToRun` wakes the machine to run. (Wake timers
    generally need AC power; a fully **powered-off** laptop cannot be woken.)
  - Off / logged out at 22:00 → the run is deferred; `-StartWhenAvailable` runs it
    **once, ASAP** at the next wake/logon. For the nightly job the watermark means
    that single make-up run processes **all** transcripts accumulated while the
    machine was down — missed runs *defer* work, never lose it. The weekly job just
    re-lints at the next opportunity (or the following Sunday).
  - On battery → the run still starts and isn't killed on a power-source switch.
- **Transient-failure retry.** Nightly `-RestartCount 2`, weekly `-RestartCount 1`
  (each at 5-min intervals). Weekly is capped at 1 because a retry re-runs the whole
  script — another full ~$8 `/lint` — and the likeliest weekly failure (a lint
  timeout) wouldn't benefit from an immediate retry anyway.
- **Failure alerting (the "durable trio"):** on any failure either job writes (1) a
  `FAILED-*.txt` sentinel in `logs/`, (2) a Windows Application event-log entry
  (source `whispr-nightly` or `whispr-weekly` depending which job failed), and (3) a
  best-effort toast. The sentinel + event log are the reliable ones; the toast may
  not show if you're logged out.
- **Data-loss safety (nightly):** the watermark only advances past a file after its
  `/ingest` fully succeeds. A failure stops the run there, so nothing is skipped
  permanently: the next run retries from where it stopped. A malformed or
  `partial: true` transcript is parked in `transcripts\_needs-attention\` rather than
  freezing the watermark (freezing is only the fallback when the move itself fails).
  Either way, the ingest ledger makes the retry free for everything already ingested.
- **Spend guardrail (weekly):** `-MaxCompiles` (default 5) caps how many GENERIC-tier
  `/compile` calls run in a single weekly pass. Anything over the cap simply stays in
  the persistent backlog (`logs/compile-backlog.txt`) for next week's run rather than
  being lost or silently dropped. The workstream tier is deliberately uncapped (it's
  what Colby is actually working on right now); `-MaxWorkstreamCompiles` (default 25)
  is a catastrophic backstop that should never trigger in normal use — if it does, the
  overflow is pushed into the same backlog rather than compiled unboundedly.
- **On-demand backlog drain:** `pwsh -NoProfile -File scripts\weekly-lint-compile.ps1
  -BacklogOnly` compiles up to `-MaxCompiles` items from the backlog without running
  `/lint` or extraction — useful for catching up between scheduled weekly runs without
  paying the ~$8 lint cost again.
- **Tunables:**
  - Nightly: `-QuietMinutes` (default 10) skips transcripts modified that recently
    (avoids racing whispr mid-write); `-TimeoutSeconds` (default 300) is the hard
    per-claude-call kill timeout; `-WatermarkOverrideUtc` (rehearsal only, requires
    `-DryRun`) replaces the stored watermark for one dry run.
  - Weekly: `-LintTimeoutSeconds` (default 1800 / 30 min — comfortably above the
    measured ~19 min), `-CompileTimeoutSeconds` (default 900 / 15 min per compile),
    `-MaxCompiles` (default 5, generic tier), `-WorkstreamDays` (default 21, active-
    workstream window), `-MaxWorkstreamCompiles` (default 25, backstop only),
    `-BacklogOnly` (skip lint/extraction, just drain the backlog).
- **Model routing** (usage cost): haiku for summarization + candidate extraction,
  sonnet for the vault-mutating `/ingest`, `/lint`, `/compile` steps.

## Remove

```powershell
Unregister-ScheduledTask -TaskName whispr-nightly-ingest -Confirm:$false
Unregister-ScheduledTask -TaskName whispr-weekly-lint-compile -Confirm:$false
```
