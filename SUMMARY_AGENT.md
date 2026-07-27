# Handoff: filling summaries in whispr transcripts

**For a local, on-device agent.** whispr records and transcribes Teams calls
entirely locally and writes each transcript with two placeholder fields it does
**not** fill. Your job is to fill them by reading the transcript on this machine.
Nothing here should ever send transcript content to an external service.

## Where the files are

`transcripts/*.md` (under the whispr repo root; the exact path is `paths.transcripts`
in `config.yaml`, default `C:\github\whispr\transcripts`).

To see which ones still need you:

```
.venv\Scripts\python.exe -m whispr list-pending
```

That prints every transcript still awaiting a summary (and does nothing else).

## What a transcript looks like

```markdown
---
date: '2026-07-14'
source: meeting
topic: [unsorted]          # <-- placeholder to fill
status: raw
confidence: working
call_title: Weekly AI Sync
call_type: meeting
start: '2026-07-14T10:00:00-04:00'
end: '2026-07-14T10:47:12-04:00'
duration_min: 47
organizer: Jane Doe
attendees: [Jane Doe, John Smith, Colby Hood]
invite_notes: 'Agenda: ...'
metadata_source: outlook
...
---

> _Summary pending._       # <-- placeholder to fill

**[00:00:04] Others:** Hey, can you hear me?

**[00:00:19] Me:** Yep, loud and clear.
...
```

- `Me:` turns are what you (the mic) said; `Others:` turns are everyone on the far
  end (one combined channel — there are no per-person labels).
- The frontmatter already carries the real call title, attendees, organizer, and
  invite notes from Outlook. Use them for context.

## What to do

1. A transcript needs a summary if its frontmatter line is exactly `topic: [unsorted]`.
2. Read the body turns (and the frontmatter context) and produce:
   - a **one-sentence** summary of the call, and
   - **1–4 topic tags**, lowercase and hyphenated (e.g. `pricing`, `pilot-metrics`).
3. Edit the file in place, changing **only these two lines**:
   - `topic: [unsorted]` → `topic: [tag1, tag2]`  (YAML flow list)
   - `> _Summary pending._` → `> <your one-sentence summary>`
4. Leave everything else byte-for-byte unchanged (it is vault-schema frontmatter;
   other tools depend on it).

## Hard constraint

Do this **locally**. Do not transmit transcript text, audio, or metadata to any
external API or service. That is the whole reason whispr hands this off to you
instead of doing it itself — see the "Local-only posture" section of `PLAN.md`.

## Consent / policy note

Whether these calls should be recorded at all (all-party consent, corporate
recording policy) is not settled by this tooling. That is a compliance question
for the human, tracked in `UNCERTAINTIES.md` — not something to resolve here.
