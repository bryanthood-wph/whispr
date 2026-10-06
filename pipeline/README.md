# pipeline/ — every call, from transcript to summary note and graph

What it does: turns each new or changed transcript into a summary note (My Actions first) and graph rows, soon after the call ends: **prepare** (deterministic cleaning) → **extract** (one model call into one schema) → **write** (deterministic graph rows + note).
How to run it: the `whispr-pipeline` scheduled task runs `python -m pipeline run` at logon and every 15 minutes; `whispr-daily` runs `python -m pipeline daily` (backup, maintenance, reconciliation) once a day; `whispr-liveness` runs `python -m pipeline liveness` (relaunch the recorder if it is down) at logon and every 15 minutes. By hand: `python -m pipeline run --dry-run` (plans against a read-only copy of the database, zero model calls, prints and writes nothing, safe anywhere), `python -m pipeline backfill [--since D] [--until D] [--yes]` (older transcripts: quotes their count and cost, queues them only with `--yes`) or `python -m pipeline run --once <transcript>` (one file, now). Never run it without `--dry-run` inside Claude Code: it refuses and alerts.
Where to look: `python -m pipeline doctor` (everything at once; exit 1 when something needs attention), `python -m pipeline alerts` (open alerts and their fixes, from the state database `kg.state`), one JSON line per item and per run in `<data_dir>/logs/pipeline.jsonl`, notes in `<data_dir>/notes/`; anything that escapes a command (bad config, an unwritable data dir), with its traceback, in `%LOCALAPPDATA%\whispr\pipeline-fatal.log` (found without config: under pythonw nothing else shows it).
Tests: `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_pipeline_*.py" -v` (fake model calls, a fake recorder and a fake scheduler; no model call, no launch, no scheduled task touched).
Config keys read: `owner.*`, `paths.*`, `auth.*`, `cli.*`, `models.extractor`, `prompts.extract`, `schemas.extract`, `prepare.*`, `extract.*`, `pipeline.*`, `schedules.*`, `backup.*`, `maintain.*`, `reconcile.*`, `liveness.*`, `doctor.*`, `alerts.session_start.*`, plus what `kg/` reads.

| Module | Role |
|---|---|
| `prepare.py` | parse, encoding check, redaction, metadata re-derivation, alias rewrite, echo removal, stub gate (D.4) |
| `extract.py` | the one extract call per episode, schema-validated, cached (D.1) |
| `calls.py`, `models.py` | the only model-call path: cache, schema check, auth gate, ledger (D.3) |
| `render.py` | the extract as Markdown: the judge's view (`render`) and the note body (`note`) |
| `write.py` | the write stage: graph rows via `kg.store.Store`, then the note |
| `run.py` | the per-call runner behind `python -m pipeline run` |
| `schedule.py` | the scheduled tasks, generated from `schedules.*` (`python -m pipeline schedule`) |
| `backup.py` | the daily backup (database by the SQLite backup API, override data, transcripts) and `restore` |
| `daily.py` | the daily job behind `python -m pipeline daily`: backup, maintenance, reconciliation |
| `reconcile.py` | the daily reconciliation: transcripts vs the recorder's calls and the pipeline queue |
| `liveness.py` | recorder liveness behind `python -m pipeline liveness` (the port of `scripts/watch-recorder.ps1`) |
| `doctor.py` | the one status report behind `python -m pipeline doctor` |
| `alerts.py` | the alert surface: `python -m pipeline alerts [--ack KEY \| --session-start]` |

## The runner (`run.py`)

Plan: `docs/plan/D-architecture-and-ops.md` §D.1, §D.6; decision 2026-10-05 (a 15-minute poll, not a recorder handoff; model calls on any power; maintenance, backup and reconciliation in a separate daily job).

1. **Single instance.** An OS lock on `pipeline.files.lock`: `msvcrt.locking` on Windows, `flock` elsewhere. The OS drops it when the holding process exits, however it dies, so a crash never leaves it held and the lock file's presence means nothing. A second run exits 0 at once and logs `locked`. `State.begin_run` is called only under the lock (it fails any run of the job still marked running: a killed run is a failed run, and the next one is its retry).
2. **Refusal.** While an `auth.refuse_if_set` variable is set (`CLAUDECODE`: a nested Claude Code session, whose calls die with zero tokens), the run raises one `refused-env:pipeline` alert, records no run, starts no item and exits 4. Nothing is retried in a loop: the next scheduled run, with Task Scheduler's clean environment, just works.
3. **Discovery.** Every `*.md` in `paths.transcripts` dated (by its `YYYY-MM-DD-...` file name) on or after `pipeline.run.process_since` (dot-prefixed temp files skipped; a missing folder fails the run rather than proving zero). While `process_since` is null the run raises one `setup:pipeline` alert and exits 4: a first run never takes the whole history unasked (/whispr-setup sets it to the install date). Older transcripts are queued only by `backfill --yes`, after it has printed their count and estimated cost (`pipeline.run.est_cost_per_call_usd`). One with no episode is enqueued. One whose sha256 differs from its episode's (edited, re-transcribed, or fixed after quarantine) is requeued with fresh attempts. Unchanged ones cost a hash.
4. **Work.** Due items newest call first (by ref), so a new call never waits behind a backlog. A stub gets a "no content" note and no call. A failure is retried after `pipeline.run.retry_backoff_min` (stored as the item's `next_attempt_at`), and quarantined at `pipeline.max_attempts` with one alert and an "Unavailable" note; the next item runs either way; items that failed before go after fresh ones. An `AuthError` is the machine's, not the item's: the attempt is given back (`State.undo_attempt`), an `auth:pipeline` alert is raised, and the run stops. Any other `ModelCallError` counts against the item only if a model call earlier in the same run answered (a cache hit doesn't count); otherwise it is handled the same way under one `model-unavailable:pipeline` alert, so a signed-out CLI, an offline laptop, a missing `claude` or an outage never burns the queue's attempts. Extract, schema and prepare failures always count.
5. **Limits.** Before each item: at most `pipeline.run.max_items`; no start once elapsed time plus the slowest item so far reaches `pipeline.run.time_budget_s`; no start if a call at its full `pipeline.max_budget_per_call_usd` could pass `pipeline.run.cap_usd` (this run) or `pipeline.run.daily_cap_usd` (the ledger's last 24 hours, read again before every item: every job's calls, plus each call in flight, which `run.reserve_call` books at its full cap until its call row settles it, so the daily job and this run see each other). Hitting one ends the run **PARTIAL**; the rest stay queued.
6. **Success means progress** (`State.finish_run`): at least one item left the queue (written, or quarantined with its alert), or nothing was eligible. A run with nothing to do makes no model call and finishes in well under a second. Backlog and metrics (`spent_usd`, `calls`, `partial`, `quarantined`, `retried`) are recorded every run.

Exit codes: 0 ok (or locked), 1 failed, 2 usage, 3 partial, 4 refused.

**Troubleshooting a transcript:** `--dry-run --once <file>` shows what would happen; `--once <file>` runs it now with fresh attempts, whatever its queue state (a re-run of an unchanged one is a cache hit: no spend). After fixing a quarantined transcript's cause, `python -m pipeline requeue <ref>` (or `--all-quarantined`) gives it fresh attempts and closes its alert; one is also retried automatically as soon as it changes.

## Backup and restore (`backup.py`)

Plan: §D.6 "Backups", §D.7 (restore drill in the setup test); lesson L22.

- **What a backup is.** A folder `<backup.prefix><UTC stamp>` under `backup.destination` (the overlay sets it: your OneDrive data folder): the graph + state database copied with the SQLite backup API from the job's own connection (never a file copy, so a pipeline run writing meanwhile can't tear it; switched out of WAL so it is one file), each `backup.include` path under `paths.data_dir` (override data; nothing writes any yet) under `data/`, the transcripts under `transcripts/` (`backup.transcripts`), and `manifest.json`.
- **Verified before it counts.** It is built as `<name>.partial` and renamed only after the copy opens read-only, passes `PRAGMA integrity_check`, and every listed file is there; a failed or killed backup leaves nothing that counts. Then the oldest verified backups beyond `backup.keep` are deleted, plus any leftover `.partial`. Only folders named exactly `<prefix><UTC stamp>` holding a manifest (or that plus `.partial`) are ever counted or deleted, oldest stamp first; a hand-made `<prefix>before-upgrade` copy is never counted against `keep` or deleted.
- **Unset destination.** `backup.destination: null` makes the daily job's backup step refuse with one `setup:backup` alert (the other steps still run; the run fails). The next good backup closes it.
- **Restore** (the setup restore drill, and the real thing): `python -m pipeline restore --from <backup folder>` verifies the backup and prints what it would do, changing nothing; add `--yes` to restore. Under both the `pipeline` and `daily` locks (it refuses while either is held) it checks the live database first: a healthy one is copied aside to `<kg.database>.pre-restore-<stamp>` and the backup's database is written into it through the backup API; a damaged one (restore's main use) is moved aside under that name with its `-wal`/`-shm`, never opened, and the backup is written to a fresh file. Then it re-opens the result (WAL, plus any migration newer than the backup; a backup from a newer schema is refused), and copies back each transcript and include file that is missing here. An existing file is never overwritten. Exit 0 restored or planned, 1 unusable backup or a lock held.

## The daily job (`daily.py`)

Plan: §D.6, `docs/plan/C-knowledge-graph.md` §C.5; decision 2026-10-05 (one daily job for maintenance, backup and reconciliation). The `whispr-daily` task runs `python -m pipeline daily` once a day; its operating envelope (cadence, cost, caps, time limit, power, kill, alerts, auth, egress) is the comment on `schedules.tasks.whispr-daily` in `config/defaults.yaml`.

- **Lock and refusal.** Its own OS lock (`pipeline.files.daily_lock`), so it never waits on the 15-minute run; a second daily run exits 0. Inside a nested Claude Code session it refuses exactly as `run` does: one `refused-env:daily` alert, no run recorded, exit 4.
- **Three steps, in order**, each recorded as a `daily:<step>` row in `maintenance_log` (outcome, error, details) and a `step` line in `pipeline.jsonl`:
  1. **backup** (see below). First, so the copy predates any repair. An unset `backup.destination` refuses the step with a `setup:backup` alert.
  2. **maintenance** (C.5): entity resolution (`kg.resolve.Resolver.run`), its "same entity?" questions asked through `calls.py` as the `kg.models.resolve` role (cached in `pipeline.files.resolve_cache`, booked in the pipeline ledger); then the integrity checks (`kg/integrity.py`: SQLite integrity and foreign keys, references to merged-away entities repointed, orphan entities counted, health metrics recorded) and the quarantine digest. Before each model call: no start once the time since the run started (backup included) plus the slowest call reaches `maintain.time_budget_s`, nor if a call at `maintain.max_budget_per_call_usd` could pass `maintain.cap_usd` or the shared `pipeline.run.daily_cap_usd` (the ledger read again for each call; the call is then reserved there until it settles). Over a limit resolution stops **partial** and the next run resumes it (each merge and decision is already committed). A pair whose decision fails on its own (bad output, a failed merge, a call failure after another call answered) is recorded as `failed` and not asked again until it changes. An auth failure, or a call failure before any call answered, raises `auth:daily` or `model-unavailable:daily` and stops resolution.
  3. **reconciliation** (`reconcile.py`): over `reconcile.window_days`, ignoring anything newer than `reconcile.settle_min`: calls the recorder queued vs transcripts it wrote (from its log, `paths.recorder_log`; each transcript is matched to its own call by its file name's start, so a recent call's transcript never hides an older lost one), transcripts with no episode or queue item, and episodes whose transcript is gone (tombstoned, unless more than `reconcile.max_tombstones`: an offline folder must not tombstone the graph). Problems raise one `reconcile:daily` alert, and a reconciliation with none closes it. Outlook meetings with no recording are reported when a calendar reader exists (the production reader is a stub today).
- **Failure.** A failing step never skips the others, but the run fails (`State.finish_run` with the error, so its `run-failed:daily` alert).

Exit codes: 0 ok (or locked), 1 a step failed or was refused, 3 partial, 4 refused. `python -m pipeline daily --dry-run` prints what each step would do against an in-memory copy of the database: no model call, no backup written, nothing alerted or logged.

## Recorder liveness (`liveness.py`)

Plan: §D.6; lesson L14 (the 2026-08-23 outage: the recorder was killed externally and stayed dead three days, because its only start was at logon). The `whispr-liveness` task runs `python -m pipeline liveness` at logon and every 15 minutes; it replaces the hand-registered `whispr-recorder` watchdog at cutover.

- **Alive = the recorder holds its own mutex** (`liveness.mutex`), never a process scan: `pythonw -m whispr` is two processes (stub and app). An unexpected probe failure counts as down (a redundant launch is harmless: the recorder's mutex ends it). A held mutex never launches anything.
- **Relaunch** runs `liveness.command` in `liveness.working_dir` through `Win32_Process::Create` (PowerShell's `Invoke-CimMethod`, a fixed script, the request on stdin), so the recorder is parented outside the task's job object and the scheduler killing a check can't kill it. The new process is bound by handle at once, the mutex is polled every `liveness.confirm_poll_s` for `liveness.confirm_timeout_s`, and a launch that never takes it is reaped through that handle.
- **Outcomes**: running or relaunched, exit 0 (logged as `liveness` lines in `pipeline.jsonl`; a pass acknowledges any open `recorder-down:liveness` alert, and `setup:liveness` too once `liveness.command` and `working_dir` are set); relaunch failed, one `recorder-down:liveness` alert and exit 1 (the next repetition retries); down with `liveness.command` or `working_dir` unset, one `setup:liveness` alert and exit 4. `--dry-run` launches, alerts and logs nothing. No model call, no run rows: the database is opened only for the alert, and its failure never stops the check.

## Status (`doctor.py`)

`python -m pipeline doctor [--json]` is the single place to ask "is it working?" (§D.6). It only reads: the database through `kg.db.connect_readonly` (never created or migrated; missing is reported), the live tasks through `schedule.check`, the ledger and logs as files. Sections: each job in `doctor.stale_after_h` (last run, last success and its age, the pipeline's backlog), open alerts with their fixes, schedule drift, the last model call's auth source, the recorder (mutex held now, log age, incidents over `doctor.incident_days`), the last liveness check, the last daily reconciliation and its problems, the task funnel, live episodes whose note lacks a My Actions section, and quarantined items. A section that cannot be read says why and the others still report. Everything that needs attention is listed last (in `attention` with `--json`); exit 0 all clear, 1 attention, 2 bad config.

## Alerts (`alerts.py`)

Every job raises its alerts into one table (`kg.state`, deduplicated by key `<kind>:<subject>`, reopened when they recur). A check that raised one closes it itself when it next passes (`run.clear_alerts`: `setup:backup`, `reconcile:daily`, `recorder-down:liveness`, `setup:liveness`). `python -m pipeline alerts` lists the open ones with their fixes; `--ack KEY` acknowledges one (exit 2 if none is open with that key). `--session-start` is the line the Claude Code SessionStart hook shows (`plugin/hooks/`): nothing when no alert is open, else at most `alerts.session_start.max_alerts` alerts, each clipped to `message_chars`. It reads read-only on a daemon thread bounded by `timeout_s`, turns any failure (bad config, missing or broken database, slow disk) into a short message, and always exits 0.

## The write stage (`write.py`)

- One episode per transcript, id = the file stem. A changed transcript rewrites the same episode, and a task whose quote is unchanged keeps its id and its lifecycle (L19). Writes are idempotent: the same document twice adds no rows.
- Facts and edges are `EXTRACTED` only when their quote is verbatim in the prepared transcript the model saw, else `AMBIGUOUS`. An edge whose endpoint is no known entity, or whose relation doesn't allow the endpoints' types, is skipped and counted (`edges_skipped` in the log).
- Tasks are stored `captured`. A my-task is "confirm?" (stored as owner_basis `unclear`) when the model says ownership is unclear, when its quote matches a Me line removed as echo, or when it has no quote. A task's id is sha1(episode + quote) while its quote is unique among the document's tasks; tasks sharing a quote (two dues in one sentence, a my-task and someone else's) each add their normalized action to it (`kg.store.task_id`), so each keeps its own row and owner. Confidence comes from `pipeline.write.task_confidence` (verbatim quote or not).
- Every string of the extract document is checked for mojibake (`prepare.mojibake_markers`, L9) before the graph transaction, so a bad document commits nothing.
- Every transcript gets a note with a My Actions section: summary, stub ("No content", My Actions "None") or quarantined ("Unavailable: extraction failed (see alert)"). A `mic_coverage` below `pipeline.write.low_mic_coverage` adds "may be incomplete: low mic coverage". Notes hold no lifecycle state.

## Scheduled tasks (`schedule.py`)

```powershell
.venv\Scripts\python.exe -m pipeline schedule --check     # read-only; exit 1 on drift, 2 if tasks can't be read
.venv\Scripts\python.exe -m pipeline schedule --apply     # print what would change; add --yes to Set-ScheduledTask it
.venv\Scripts\python.exe -m pipeline schedule --register  # /whispr-setup only: print what would register; add --yes to do it
```

- **`--check`** reads each configured task through `Get-ScheduledTask`, `Get-ScheduledTaskInfo` and
  `Export-ScheduledTask`. It reports a task that:
  - is missing
  - differs from config in its action, triggers, settings or principal. A value config does not
    set (a trigger's end, delay or UTC offset, the user, idle and network conditions, hard
    terminate) is expected at the cmdlet's default, so an expired or idle-only task shows up
  - has an empty `NextRunTime`
  - runs a `-m pipeline` subcommand that `pipeline/__main__.py` does not define. Such a task is
    never written by `--apply` or `--register`: armed, it would fail every run unseen

  An empty `NextRunTime` is the scheduler's own word that nothing will fire. It is the symptom the
  2026-08-27 grafted-repetition task showed, while its XML looked correct.
- **`--apply`** rewrites only tasks that already exist, and then re-checks them. A missing task stays
  a finding, because registering is setup's job (D.2).
- **`--register`** registers each missing task without `-Force`, so an existing task is never
  overwritten. It then reads every task back, and exits 1 unless each one matches config and has a
  `NextRunTime`. The registration call alone is never trusted.
- **`--apply` and `--register` print each planned write** (`would change ...`, `would register ...`)
  and write only with `--yes`. Without it they exit 1 having written nothing. If a write fails
  after earlier ones succeeded, the error names the tasks already written and what reading them
  back found.

The shipped tasks are listed below. Their names avoid the hand-registered live tasks listed in
`schedules.reserved_names`; a configured task with one of those names, in any letter case, is
refused, as are two tasks whose names differ only in case (Task Scheduler names ignore case).

| Task | Runs | Purpose |
|---|---|---|
| `whispr-pipeline` | `-m pipeline run` | the per-call pipeline: at logon and every 15 minutes, on any power, catch-up after a missed run |
| `whispr-daily` | `-m pipeline daily` | the daily job: backup, maintenance, reconciliation; on any power, catch-up after a missed run |
| `whispr-liveness` | `-m pipeline liveness` | recorder liveness at logon plus every 15 minutes (L14, the 08-23 outage fix); relaunches it outside the task's job object |

Times, intervals, limits and flags are in `config/defaults.yaml` under `schedules.tasks`; this file does not restate them.

Rules the generator enforces:
- **Start times are local, with no UTC offset.** `New-ScheduledTaskTrigger` writes one, which Task
  Scheduler reads as "synchronize across time zones", so the fire time moves an hour with DST.
  The generator rewrites it as local time, and a live offset is drift.
- **Repetition is its own trigger.** It is a one-time trigger at midnight that repeats forever,
  never a repetition hung off the logon trigger, which stays dormant until the next logon. Every task
  needs `daily_at` or `every_min`, so `NextRunTime` can prove it is armed.
- **No version-stamped interpreter.** The interpreter is `schedules.interpreter`, or
  `schedules.interpreter_name` beside the interpreter running the command. It is refused if
  it, or its venv's base interpreter, matches `schedules.version_stamp_patterns` (an MSIX package
  folder, deleted by the next update).
- **Values never reach a command line.** PowerShell gets a fixed script with `-Command`, and the
  request goes over stdin as ASCII JSON. Tests pass a fake runner, so no test touches a real
  scheduled task.
- **`RestartCount` covers only a run the scheduler failed or killed**, such as one that hit its time
  limit. It never fires when a process exits non-zero on its own (L16). A job's own failure is
  recorded in its run ledger.

## Known gaps

- A changed transcript's facts and edges that the new extract doesn't reproduce stay active next to the new ones; C.5 maintenance (the daily job) owns superseding them.
- Split recordings are not merged (`prepare.group_episodes`, L13): each transcript is its own episode, because a 15-minute poll can process the first part before the second exists.
- The alias table (`prepare.load_aliases`) is not loaded yet: no file exists until C.5 entity resolution grows one.
- The recorder does not write `mic_coverage` yet (D.6), so the low-mic warning is dormant until it does.
- A deleted transcript's episode is not tombstoned by `run`; the daily reconciliation does it.
- The daily reconciliation's Outlook check has no production reader yet (`reconcile.outlook_reader` returns None): the COM code lives in the recorder, which `pipeline/` does not import.
- Integrity checks do not yet re-verify stored quotes against their transcripts, and health metrics are recorded but no regression on them raises an alert.
- `READ_SCRIPT` reads with `-ErrorAction SilentlyContinue`, so a task it is denied access to reads as missing (still a finding, and `--register` then fails loudly).
- `schedules.reserved_names` lists only the hand-registered tasks; add any live eval task there if it becomes permanent.
