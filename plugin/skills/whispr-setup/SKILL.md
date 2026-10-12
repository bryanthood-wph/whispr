---
name: whispr-setup
description: Set up whispr's pipeline for this Windows account, once after installing (or to re-check it). Creates the data folder and database, writes your config overlay, registers the scheduled tasks after you approve them, shows a test alert and runs doctor. Run it only when asked to set up whispr.
disable-model-invocation: true
# D.7 role "/whispr-setup": your session model. One pre-approved tool, AskUserQuestion
# (the user's rule: every question goes through it). No Bash command is pre-approved:
# each one runs this install's interpreter, whose path differs per install, so a
# permission rule can't name it, and every step's command is shown to you for approval.
# That also gives `schedule --register --yes`, the one step that changes the machine
# outside whispr's own folders, an approval of its own.
allowed-tools:
  - AskUserQuestion
---

# /whispr-setup: set up whispr for this account

You set up whispr's pipeline with the user, one step at a time. Every step is one
command, run exactly as written below, with the Bash tool:

```
"${user_config.python}" -s "${CLAUDE_PLUGIN_ROOT}/bin/pipeline.py" "${user_config.whispr_root}" <step>
```

`bin/pipeline.py` runs `python -m pipeline <step>` from the whispr folder, whatever the
current directory is. Below, **`PIPELINE <step>`** means that whole command with `<step>`
filled in. If the plugin's config option is set (`${user_config.config}` is not empty),
put `--overlay "${user_config.config}"` right after the whispr folder in every command.

Rules:
- Run only the commands below. Never edit, create or delete a file yourself, never run
  `schedule --apply`, `run`, `daily`, `backfill` or `liveness`, and never run a task
  (`Start-ScheduledTask`). If a step fails, show its output and stop; don't work around it.
- Ask every question with **AskUserQuestion**, and wait for the answer.
- Show each command's output to the user before going on.

## 1. Plan the overlay

Run `PIPELINE setup plan`. It lists every value for the overlay, each with its source:

- `[overlay]`: already set, kept. `[derived]`: read from this install, shown, not asked.
- `[suggested]`: ask the user to accept it or give another value (one question per value).
- `[ask]`: ask the user for it: `owner.name` (your name as Teams shows it: the speaker
  that is "Me"), `owner.email`, `owner.tenant` (the tenant string Teams puts in window
  titles, e.g. "Contoso (CON)"; they may answer none), and any folder with nothing to
  suggest from.
- `[problem]`: setup can't continue; show the message and stop.

The last line names the recorder's microphone match. It is the recorder's own setting,
chosen by the installer's device picker; if it is wrong, tell the user to re-run the
installer, and go on.

## 2. Write the overlay

Run `PIPELINE setup write --set KEY=VALUE ...` with one `--set` per answer from step 1
(a changed suggestion, and every `[ask]` value; `owner.tenant=` with nothing after `=`
for none). Quote each `--set` argument. It prints what would change and writes nothing.
Show that, ask the user to confirm, and on a yes run the same command with `--yes`
added. "still to set" means an answer is missing: ask for it and repeat this step.

## 3. Create the data folder and database

Run `PIPELINE setup init`.

## 4. Register the scheduled tasks

1. Run `PIPELINE schedule --register` (no `--yes`). It prints each task it would
   register and writes nothing (exit 1 then is expected). Exit 0 ("all N task(s) match
   config") means every task is already registered as configured: go to step 5.
2. Show the list and ask: "Register these scheduled tasks now?" Only on an explicit yes
   run `PIPELINE schedule --register --yes`. On anything else, skip to step 5 and say
   the tasks are not registered and the pipeline won't run until they are.
3. It must end "all N task(s) match config and have a NextRunTime". Otherwise show
   what it printed and stop.

## 5. Show the test alert

Run `PIPELINE setup test-alert`. It prints what the SessionStart hook shows at the start
of every session while an alert is open, then the alert's key. Show the message to the
user, then run `PIPELINE alerts --ack KEY` with that key, so it doesn't stay open.

## 6. Check everything

Run `PIPELINE doctor`. Report "all clear", or each NEEDS ATTENTION item. Right after
setup, expect the jobs to have no successful run yet: they start at the next scheduled
run (the pipeline every 15 minutes). Say so instead of treating it as a failure.
