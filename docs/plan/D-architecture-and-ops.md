# D. Target architecture, configuration and operations

> **One screen.** Each transcript goes through four stages: **prepare**
> (deterministic cleaning) → **extract** (one model call into one schema) →
> **write** (deterministic graph + summary note) → **maintain** (a nightly
> self-heal).
> - Every setting lives in `config/`.
> - Every model call goes through one interface, under one agent roster with
>   least privilege.
> - A run counts as successful only if it made progress.
> - Every alert lands where you actually look.
> - A colleague installs it in a few steps, and a setup test proves it.

## D.1 Pipeline stages (replaces `nightly-ingest.ps1`, `/ingest`, `/lint`, `/compile`)

| Stage | Kind | Input → output | Idempotency key |
|---|---|---|---|
| **prepare** | code | transcript → prepared transcript + metadata (D.4) | transcript sha256 + prepare version + hash of the `prepare.*` config and the alias table |
| **extract** | 1 model call | prepared transcript → unified JSON (summary, required `my_actions`, tasks, entities, facts, edges with quotes) validated against `config/schema/extract.json` | sha256 of the exact request (CLI base args, model, effort, system prompt, schema hash, input text) |
| **write** | code | JSON → graph rows + rendered summary note | episode id |
| **maintain** | code + typed decisions | graph → repaired graph + health metrics (Appendix C.5) | run id |

- **Model output never writes files.** Only code writes, after schema
  validation (lesson L18).
- **One bad item never blocks the queue** (lesson L7).
  - Items are processed one at a time. A failure is retried with backoff.
  - After `pipeline.max_attempts` failures the item is **quarantined**,
    with one alert that names the fix.
  - Quarantined items expire into a weekly digest after
    `alerts.quarantine_digest_days`, instead of re-alerting every day.
- **Every transcript gets a note with a My Actions section** (north star).
  - A quarantined item still gets a note: "Unavailable: extraction failed
    (see alert)".
  - A low-mic-coverage note adds "may be incomplete: low mic coverage".
  - `doctor` counts transcripts with no My Actions section, and the target
    is 0.
- **Success means progress** (lesson L1). A run succeeds only if one of
  these holds:
  - it processed ≥1 new item
  - it proved there were zero eligible items

  It logs the backlog depth every run. An item processed in
  `alerts.repeat_item_runs` consecutive runs raises an alert.

## D.2 Configuration: the only place tunables live

```
config/
  defaults.yaml        every tunable, commented: models per role, effort, budgets, schedules
                       (pipeline + eval one-shots), thresholds, retention, alert rules (ships with code)
  config.schema.json   validates defaults + overlay at startup; unknown or missing keys fail loudly
  prompts/             extract.md, judge-*.md — referenced by path, recorded by hash in every run
  schema/              extract.json (the unified output), task.json
  ontology.yaml        graph types and relations, versioned
%APPDATA%\whispr\config.yaml   user overlay written by /whispr-setup: name, email, data dir,
                               mic match, tenant string, schedules if changed
```

**Rules**
- **No machine- or person-specific value appears in code or prompts.**
  The hand-off blockers in A.5 move to the overlay. The exception is the US
  date format, which is fixed in code with a locale-independent Outlook
  filter.
- **Scheduled tasks are generated from config** (lesson L16).
  - `python -m pipeline schedule --check` compares the live tasks with
    config. `doctor` runs the check every time.
  - `--apply` uses `Set-ScheduledTask` on tasks that already exist. It
    never re-registers them blindly.
- **Changing a model, a prompt or a threshold is a config diff.** The eval
  regression gate (Appendix B.7) checks it like code.

## D.3 Model-call interface and production agent roster (least privilege)

All model calls go through `pipeline/models.py`. It implements **only the
CLI path now**, behind an interface; the API path is deferred (README §3).
Before every call it:
- refuses to run if `CLAUDECODE` is set
- clears the inherited variables for this process only, with
  `ANTHROPIC_API_KEY` handled as the auth gate decides
- **asserts the approved auth source** and fails closed if it doesn't match
- sets `--max-budget-usd` from config
- logs model, tokens, cost and the auth source to the run ledger

(Lessons L3 and L8.)

| Role | Model / effort | Tools | Why |
|---|---|---|---|
| Extractor | from the eval winner, in config | **none** (`--json-schema`) | text in, JSON out |
| Maintenance decisions (merge, contradiction) | set per role from the C.6 entity-resolution and temporal results (`kg.models.*`) | none | accuracy first; the cheapest model that passes its suite |
| Graph reader (you, in Claude Code) | your session model | MCP `search` / `get` / `source`, **read-only** | answering never changes the graph |
| `/create-tasks` review | your session model | MCP read + `task_update_status` only | confirm, clarify, attach inputs |
| `/execute-tasks` workers | per task type, in config | allowlist declared on the task when it is marked `ready`. Default read-only, writes limited to an output folder, no shell unless the task type needs it and you confirmed | each task gets only what it needs |
| Live assist (future) | from feasibility results | MCP read-only | suggest, never act |

Agents used **while building** this follow the same rule: research and
review agents are read-only and have explicit no-change instructions.

## D.4 The prepare step (cleaning before any model sees text)

Deterministic and versioned, and it gets its own checks (Appendix B.8):

1. **Stub gate** (lesson L25). Zero turns or fewer than `prepare.min_words`
   → no model call. The note says "no content", and My Actions says "None".
2. **Echo removal** (lesson L4). A Me line that repeats an Others line
   (±`prepare.echo_window_s`, ≥`prepare.echo_overlap`) is dropped, and the
   count is recorded. A my-task whose only quote is a removed line can't
   be captured silently: it goes to "confirm?" (D.5).
3. **Redaction** (lesson L10). Join links, passcodes, dial-ins and meeting
   IDs are stripped from the transcript and its metadata. A regression test
   asserts that none survive.
4. **Metadata re-derivation** (lesson L15).
   - `attendees` must be people. Generic title segments, `@` strings and the
     tenant become null.
   - Calendar matching skips reminder-type, all-day, no-Teams-link and
     single-attendee items.
   - The match score and the candidate count are stored.
5. **Alias rewrite** (lesson L11). Known ASR variants are rewritten to their
   canonical spelling, from an alias table seeded with Outlook names and
   grown by entity resolution.
6. **Episode merge** (lesson L13). Same calendar item or same title, with a
   gap under `prepare.merge_gap_min` → one episode.
7. **Encoding** (lesson L9). Stays UTF-8 end to end. Any write containing
   `Γ` sequences or U+FFFD is rejected.

## D.5 Task contract (the substrate for task-to-work)

```json
{ "id": "sha1(episode + quote)", "owner": "...", "owner_basis": "assigned|volunteered",
  "action": "...", "due": "...|null", "due_basis": "stated|not_stated", "context": "...",
  "quote": "...", "source": {"episode": "...", "start": "<timestamp>"},
  "confidence": 0-1, "status": "captured|confirmed|ready|in_progress|done|dropped",
  "tools_allowed": ["..."] }
```

**Id collisions:** when two or more of one transcript's tasks share a quote, each id is instead sha1(episode + quote + "\x1f" + its action, lower-cased and whitespace-collapsed), so none is folded into another.

- **Ownership in production** (JEV-style, and exactly what the eval
  scores).
  - `owner_basis` is a typed choice (assigned / volunteered / other's /
    **unclear**) backed by a quote.
  - These tasks land in My Actions marked **"confirm?"**, at the top of
    `/create-tasks`:
    - `unclear` ownership
    - a task whose only quote was removed as echo (D.4)

    Nothing is dropped silently.
  - Re-sampling borderline items k times is **deferred** (README §3). It is
    added only if the eval winner alone misses the 90% precision bar.
- **Lifecycle state lives only in the database**, never copied into files
  (lesson L19).
- **Funnel metric:** captured → confirmed → ready → done. It is reported
  weekly. Today 0 of 1,262 vault items ever reached the task system
  (lesson L20).
- **Flow:**
  1. extract emits tasks
  2. write stores them as `captured` and renders My Actions
  3. `/create-tasks` reviews them
  4. `/execute-tasks` drains `ready` tasks with the roster above

  Readiness covers steps 1–3. Step 4 is designed after the eval.

## D.6 Operations

**One alert surface** (lesson L2: 51 of 51 toasts failed, and 40 FAILED
files went unread).
- Alerts are rows in the state database: dedupe key, first and last seen,
  count, acknowledged.
- They are shown **where you already look**: the plugin's Claude Code
  session-start message, plus `doctor`.
- A tray badge is deferred (README §3). It is added only if an alert sits
  unseen for more than a day, because it would make the local-only
  recorder read the pipeline database.
- The WinRT toast and sentinel-file paths are removed.
- Setup creates a test alert, and setup isn't complete until you've seen
  it.

**`doctor` is the single source of status** (lesson L21). One command
reads:
- the last progress run and the backlog
- open alerts
- schedule drift
- the auth source
- the recorder heartbeat
- the daily reconciliation: Outlook Teams meetings vs transcripts written

Docs point at `doctor`, so they never restate live status in prose.

**Recorder liveness** (lesson L14).
- A heartbeat line, and exit reasons captured from the Windows event log.
- Each transcript's frontmatter records `mic_coverage` (mic speech time ÷
  loopback speech time) and the output device, so capture problems are
  visible in the data (lessons L4 and L5).
- The transcription queue lives on disk.
- On startup the recorder sweeps for orphan WAVs, so a crash doesn't drop
  a queued call. Today 308 calls were queued but only 304 written.
- The poll is the primary trigger and the WinEvent hook is secondary
  (lesson L23). Docs say so.

**Operating envelope**, filled in for every scheduled job before it ships
(standing rule):
- cadence
- measured cost per run
- `--max-budget-usd` per call and a nightly cap
- time budget: batch size = time limit ÷ measured time per file, and the
  job exits `PARTIAL` before the limit
- AC-power condition for model spend
- catch-up after a missed run
- kill = failed + retry
- alert rules
- auth source
- first run supervised
- egress boundary
- removal steps

(Lessons L3 and L6.)

**Backups** (lesson L22). A nightly copy of the graph database and the
override data to the user's OneDrive data folder, with a restore drill in
the setup test. Transcripts are the rebuild source, so they are backed up
too.

## D.7 Distribution

- **Lead option: a Claude Code plugin with mods, plus the recorder
  installer.** Each user signs in with their own enterprise Claude login.
  `/whispr-setup` does four things:
  - creates the data folder and database
  - writes the config overlay
  - registers tasks from config
  - shows the test alert
- **Fallbacks, behind the same model-call interface:** the Messages API
  Option 2 (the Messages API) or Option 3 (a hybrid). Either is built only
  if the plugin path fails vetting.
- **Shipped from a separate distribution repo or marketplace,** built by
  `installer/build-dist`, so colleagues never need access to this repo or
  `ops/` (lesson L17).
- **Setup acceptance test (gate)** on a fresh profile or VM. It records the
  steps, time and human prompts, and must pass all of these:
  - existing per-user Python present
  - non-admin managed account
  - launch from an unrelated working directory
  - `NextRunTime` set on every task
  - host exe path has no version stamp
  - recorder recovers after being killed
  - laptop asleep at run time → catch-up
  - test alert visible
  - restore from backup
  - one recorded call → note, tasks and graph, queryable through Claude
- **Recorder changes need a live-call checklist** before cutover (lesson
  L23). Unit tests can't reproduce COM or WASAPI behavior.

## D.8 Retiring cohoodOBS and removals

- **Retired after the graph passes its evals:**
  - `/ingest`, `/compile`, `/lint`, `/query`
  - the 5 legacy agents, hooks and `_draft-config.json`
  - `weekly-lint-compile.ps1`
  - the old MCP server and `/vault`
- **The content** is snapshotted, then becomes a read-only archive. Nothing
  is deleted.
- **Removal candidates**, each needing your approval with evidence:

| Candidate | Evidence | Condition |
|---|---|---|
| `install_task.ps1` | re-creates the design behind the 08-23 outage (lesson L26) | none (approved as an early fix) |
| `SUMMARY_AGENT.md`, `list-pending`, `retry-summary`, `retry_cli` | dead path | none |
| `register-startup-shortcut.ps1`, `audio.channels`, duplicate `version`, `_archive/` | duplicates and unused | none |
| Sonnet `/ingest` | replaced by the one-call extract | retired with cohoodOBS |
| Stale docs | lesson L21 | archived to `docs/history/` |
