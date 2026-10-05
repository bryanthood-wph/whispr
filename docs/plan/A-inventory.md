# A. Inventory: what exists today (2026-10-04)

> **One screen.** A local recorder writes transcripts. Two scheduled
> PowerShell jobs then push them through three Claude call types (Haiku
> summarize, Sonnet `/ingest`, a weekly Sonnet `/lint` + `/compile`) into an
> Obsidian vault in another repo. Five legacy agents, three hooks and three
> read paths sit around that vault.
> - **Spend:** ≈$398 to date. `/ingest` is 85% of the per-file cost, and
>   most of that is fixed overhead.
> - **What can't move to a colleague's machine:** the name "Colby Hood",
>   the vault path, device names and the tenant string are hardcoded.
>
> Sources: three inventory passes and four lessons passes, all read-only.

## A.1 Pipeline map

```
Teams window title ──► watcher.py (2 s poll = primary trigger in practice; WinEvent hook) ── Outlook: is_live_meeting
   ▼
capture.py ──► recordings/<ts>-mic.wav + -loopback.wav   (sessions <12 s discarded)
   ▼
transcribe.py (faster-whisper small, CPU int8; real-time factor 0.32)   ── in-memory queue
   ▼
metadata.py (Outlook match → window-title fallback) ──► output.py ──► transcripts/*.md
   ║ ── local-only boundary ──
   ▼
nightly-ingest.ps1 (01:00 Mon–Fri live): Haiku summarize → write vault source → Sonnet /ingest
weekly-lint-compile.ps1 (Sun): Sonnet /lint → Haiku candidates → Sonnet /compile ×≤5
   ▼
cohoodOBS (~140 wiki pages, sources/, log.md) ── read via /vault, /query, MCP vault_*; /create-tasks, /execute-tasks
watchdogs: whispr-recorder (every 15 min), freshness check (2×/day)
```

## A.2 Components

| Layer | Component | Location | Under git? |
|---|---|---|---|
| Recorder | `whispr/` (13 modules) | this repo | yes |
| Jobs | `scripts/*.ps1`, `summarize-prompt.txt` | this repo | yes |
| Vault rules and commands | `CLAUDE.md`, `schema.md`, `/ingest` `/compile` `/lint` `/query` | cohoodOBS | yes (allowlist) |
| Task machinery | 5 agents, 3 hooks, `_draft-config.json`, `_task-schema.json`, `.js` helpers | cohoodOBS | yes |
| Skills | `create-tasks`, `execute-tasks`, `summarize-transcript`, `vault` | `~/.claude/skills` | separate repo |
| Vault MCP server | `server.js` | `~/.claude/mcp/vault/` | **no** |
| Voice-study agents | 3 `.agent.md` | `.github/agents/` | yes (can't run: missing input file) |
| Vault content | pages, sources, log, tasks | cohoodOBS | **no** (OneDrive only) |

## A.3 Models, effort, tools per call (measured)

| Call | Model | Effort | Tools | Cost per file | Time |
|---|---|---|---|---|---|
| Summarize | Haiku 4.5 | n/a (no effort setting) | none | median $0.072 (fixed part ≈$0.045) | 59 s |
| `/ingest` | Sonnet | CLI default (not set) | Read, Edit, Write, Glob, Grep | median $0.413 (fixed part ≈$0.35) | 61 s |
| `/lint` | Sonnet | not set | same 5 | $5–16 per run; one run $16.30 for 0 output | 554 s |
| `/compile` | Sonnet | not set | same 5 | $0.77 per page | — |
| execute-tasks agents | three model IDs, two of them stale | not set | per hook | 2 tasks ever run | — |
| ASR | faster-whisper `small` int8 | beam 5 | — | $0 | real-time factor 0.32 |

## A.4 Corpus (measured)

All counts are as of 2026-10-04. There are 282 files: 281 transcripts plus
1 parked non-transcript.

- **281 transcripts**, 7,248 call-minutes, Jul–Oct 2026.
  - Metadata: 158 Outlook-matched, 122 window-title, 1 none. 13 of the 281
    are stubs. The frame split by cell is in B.3.
  - Median ≈8.0K tokens; p90 18.3K; max 57.9K.
- **Data quality:**
  - 13 no-speech stubs (zero turns).
  - **Output device** (frontmatter `output_device`): 228 on laptop
    speakers, 53 on a headset or other device. 82 speaker transcripts show
    heavy echo; no headset transcript does (L4).
  - **Mic coverage:** 13 sessions with mic ≪ loopback (L5).
  - 13 split-meeting pairs (L13).
  - 155 transcripts with invite notes, 64 with passcodes (L10).
- **Vault:**
  - **Coverage:** 258 source notes. 254 match a current transcript, 3
    point at transcripts that are gone (L19), and 1 is unexplained.
    230 of the 258 have mojibake (L9).
  - **Action items:** 1,262 in total, 452–591 of them mine depending on
    the parsing rule. 32–50 notes list zero items for me, and **none ever
    reached the task system** (L20).

## A.5 Findings that stay relevant

The full lesson list is in Appendix E. These are the inventory-only items.

- **Hand-off blockers: 7 kinds, in about 15 places.** All move to the config
  overlay (D.2) except the date format, which gets a code fix.
  - the vault path, hardcoded in 3 places
  - "Colby Hood", hardcoded in the prompt, the weekly job and the 5 agents
  - a `.venv`-only watchdog path
  - the repo slug
  - the tenant string
  - mic names
  - the US date format in the Outlook filter
- **Distribution gaps.** `install.ps1` downloads without authentication
  from a now-private repo, so it probably fails for recipients today.
  Recipients also get no summarization, graph, watchdog or tasks.
- **Redundancy:**
  - three auto-start mechanisms (`install_task.ps1` is hazardous)
  - three vault read paths
  - a dead local-summary path (`SUMMARY_AGENT.md`, `list-pending`)
  - unused config keys
  - half-dismantled task machinery
- **Drift.** All three schedules in `register-task.ps1` differ from the
  live tasks, and 6 docs are stale.

## A.6 Configuration files (what's read, by whom)

| File | Key content | Read by |
|---|---|---|
| `config.yaml` (this repo) | paths, audio, trigger, transcription, retention, version | `whispr/` |
| `scripts/summarize-prompt.txt` | the summary prompt, with the owner name hardcoded | `nightly-ingest.ps1` |
| `register-task.ps1` (inline) | schedules, time limits; has drifted from the live tasks | Task Scheduler (once) |
| cohoodOBS `CLAUDE.md`, `schema.md`, `_wiki-template.md` | vault rules and page shapes | `/ingest`, `/compile`, `/lint` |
| cohoodOBS `_draft-config.json`, `_pipeline-settings.json`, `_task-schema.json` | agent models (stale IDs), pipeline flags, task shape | the 5 agents and hooks |
| cohoodOBS `_backup-config.json`, `_ingest-queue.json` | backup and queue state | legacy scripts |
| cohoodOBS `.claude/settings.local.json` | hook wiring and permissions | Claude Code in the vault |

That is **11 files in 3 places**. The target is `config/` plus one user
overlay (D.2).

## A.7 Recordings

- `retention.delete_audio_on_success: true`: audio is deleted once a
  transcript is written.
- **12 WAVs (314 MB) on disk.** They are 6 mic + loopback pairs: 4 orphans
  from a crashed queue (07-17, 08-21, 09-22 ×2) and 2 tiny record-test
  pairs (07-13, 10-02).
- The orphans are the only audio left for any existing call (L14).

## A.8 Handoff chains today

- **Recorder:** watcher → capture → in-memory transcription queue →
  metadata → output.
- **Nightly:** `nightly-ingest.ps1` → `claude -p` Haiku summarize →
  PowerShell writes the source note → `claude -p` Sonnet `/ingest` (edits
  wiki pages and `log.md`).
- **Weekly:** `/lint` → Haiku candidate list → `/compile` ×≤5.
- **Tasks:** `/create-tasks` (interactive) → `_inbox.md` → agents `triage` →
  `extract` → `route` → `draft` → `validate`, with hooks `pre-writescope`,
  `post-log` and `stop-capture`. Only 2 tasks have ever gone through.
