# pipeline/ — every call, from transcript to summary note and graph

What it does: turns each new or changed transcript into a summary note (My Actions first) and graph rows, soon after the call ends: **prepare** (deterministic cleaning) → **extract** (one model call into one schema) → **write** (deterministic graph rows + note).
How to run it: the `pipeline` scheduled task runs `python -m pipeline run` at logon and every 15 minutes. By hand: `python -m pipeline run --dry-run` (plans against a read-only copy of the database, zero model calls, prints and writes nothing, safe anywhere), `python -m pipeline backfill [--since D] [--until D] [--yes]` (older transcripts: quotes their count and cost, queues them only with `--yes`) or `python -m pipeline run --once <transcript>` (one file, now). Never run it without `--dry-run` inside Claude Code: it refuses and alerts.
Where to look: alerts in the state database (`kg.state`), one JSON line per item and per run in `<data_dir>/logs/pipeline.jsonl`, notes in `<data_dir>/notes/`; anything that escapes a command (bad config, an unwritable data dir), with its traceback, in `%LOCALAPPDATA%\whispr\pipeline-fatal.log` (found without config: under pythonw nothing else shows it).
Tests: `.venv\Scripts\python.exe -m unittest tests.test_pipeline_run -v` (a fake extract call; no model calls).
Config keys read: `owner.*`, `paths.*`, `auth.*`, `cli.*`, `models.extractor`, `prompts.extract`, `schemas.extract`, `prepare.*`, `extract.*`, `pipeline.*`, `schedules.*`, plus what `kg/` reads.

| Module | Role |
|---|---|
| `prepare.py` | parse, encoding check, redaction, metadata re-derivation, alias rewrite, echo removal, stub gate (D.4) |
| `extract.py` | the one extract call per episode, schema-validated, cached (D.1) |
| `calls.py`, `models.py` | the only model-call path: cache, schema check, auth gate, ledger (D.3) |
| `render.py` | the extract as Markdown: the judge's view (`render`) and the note body (`note`) |
| `write.py` | the write stage: graph rows via `kg.store.Store`, then the note |
| `run.py` | the per-call runner behind `python -m pipeline run` |
| `schedule.py` | the scheduled tasks, generated from `schedules.*` (`python -m pipeline schedule`) |

## The runner (`run.py`)

Plan: `docs/plan/D-architecture-and-ops.md` §D.1, §D.6; decision 2026-10-05 (a 15-minute poll, not a recorder handoff; model calls on any power; maintenance, backup and reconciliation in a separate daily job).

1. **Single instance.** An OS lock on `pipeline.files.lock`: `msvcrt.locking` on Windows, `flock` elsewhere. The OS drops it when the holding process exits, however it dies, so a crash never leaves it held and the lock file's presence means nothing. A second run exits 0 at once and logs `locked`. `State.begin_run` is called only under the lock (it fails any run of the job still marked running: a killed run is a failed run, and the next one is its retry).
2. **Refusal.** While an `auth.refuse_if_set` variable is set (`CLAUDECODE`: a nested Claude Code session, whose calls die with zero tokens), the run raises one `refused-env:pipeline` alert, records no run, starts no item and exits 4. Nothing is retried in a loop: the next scheduled run, with Task Scheduler's clean environment, just works.
3. **Discovery.** Every `*.md` in `paths.transcripts` dated (by its `YYYY-MM-DD-...` file name) on or after `pipeline.run.process_since` (dot-prefixed temp files skipped; a missing folder fails the run rather than proving zero). While `process_since` is null the run raises one `setup:pipeline` alert and exits 4: a first run never takes the whole history unasked (/whispr-setup sets it to the install date). Older transcripts are queued only by `backfill --yes`, after it has printed their count and estimated cost (`pipeline.run.est_cost_per_call_usd`). One with no episode is enqueued. One whose sha256 differs from its episode's (edited, re-transcribed, or fixed after quarantine) is requeued with fresh attempts. Unchanged ones cost a hash.
4. **Work.** Due items newest call first (by ref), so a new call never waits behind a backlog. A stub gets a "no content" note and no call. A failure is retried after `pipeline.run.retry_backoff_min` (stored as the item's `next_attempt_at`), and quarantined at `pipeline.max_attempts` with one alert and an "Unavailable" note; the next item runs either way; items that failed before go after fresh ones. An `AuthError` is the machine's, not the item's: the attempt is given back (`State.undo_attempt`), an `auth:pipeline` alert is raised, and the run stops. Any other `ModelCallError` counts against the item only if a model call earlier in the same run answered (a cache hit doesn't count); otherwise it is handled the same way under one `model-unavailable:pipeline` alert, so a signed-out CLI, an offline laptop, a missing `claude` or an outage never burns the queue's attempts. Extract, schema and prepare failures always count.
5. **Limits.** Before each item: at most `pipeline.run.max_items`; no start once elapsed time plus the slowest item so far reaches `pipeline.run.time_budget_s`; no start if a call at its full `pipeline.max_budget_per_call_usd` could pass `pipeline.run.cap_usd` (this run) or `pipeline.run.daily_cap_usd` (the ledger's last 24 hours). Hitting one ends the run **PARTIAL**; the rest stay queued.
6. **Success means progress** (`State.finish_run`): at least one item left the queue (written, or quarantined with its alert), or nothing was eligible. A run with nothing to do makes no model call and finishes in well under a second. Backlog and metrics (`spent_usd`, `calls`, `partial`, `quarantined`, `retried`) are recorded every run.

Exit codes: 0 ok (or locked), 1 failed, 2 usage, 3 partial, 4 refused.

**Troubleshooting a transcript:** `--dry-run --once <file>` shows what would happen; `--once <file>` runs it now with fresh attempts, whatever its queue state (a re-run of an unchanged one is a cache hit: no spend). After fixing a quarantined transcript's cause, `python -m pipeline requeue <ref>` (or `--all-quarantined`) gives it fresh attempts and closes its alert; one is also retried automatically as soon as it changes.

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
    never written by `--apply` or `--register`: armed, it would fail every run unseen. Today that
    is `whispr-backup` and `whispr-liveness`, until `backup` and `liveness` exist

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
| `whispr-backup` | `-m pipeline backup` | nightly backup (D.6, L22): catch-up after a missed run |
| `whispr-liveness` | `-m pipeline liveness` | recorder liveness at logon plus a repetition (L14, the 08-23 outage fix) |

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
- A deleted transcript's episode is not tombstoned here; that is the daily reconciliation's job.
- `READ_SCRIPT` reads with `-ErrorAction SilentlyContinue`, so a task it is denied access to reads as missing (still a finding, and `--register` then fails loudly).
- `schedules.reserved_names` lists only the hand-registered tasks; add any live eval task there if it becomes permanent.
