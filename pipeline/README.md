# pipeline

The whispr pipeline: prepare → extract → write → maintain, plus the scheduled tasks that run it.
Run commands as `python -m pipeline <command>`. Today the only command is `schedule --check | --apply | --register`.
Every tunable is read from `config/defaults.yaml` (merged with `%APPDATA%\whispr\config.yaml`) and validated by `config/config.schema.json`.
Scheduled tasks are configured under `schedules.*`. Each task is one entry in `schedules.tasks`, keyed by its task name.
The design is in `docs/plan/D-architecture-and-ops.md` (D.2, D.6, D.7). The traps it guards against are in `docs/plan/E-lessons.md` (L6, L14, L16).

## Scheduled tasks (`pipeline/schedule.py`)

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
    is every shipped task, until `run`, `backup` and `liveness` exist

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
| `whispr-pipeline` | `-m pipeline run` | nightly pipeline: AC power only, catch-up after a missed run |
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

- `READ_SCRIPT` reads with `-ErrorAction SilentlyContinue`, so a task it is denied access to reads as missing (still a finding, and `--register` then fails loudly).
- `schedules.reserved_names` lists only the hand-registered tasks; add any live eval task there if it becomes permanent.
