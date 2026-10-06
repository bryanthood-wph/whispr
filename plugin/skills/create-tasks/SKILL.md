---
name: create-tasks
description: Review the tasks whispr captured from your meetings, one at a time, and record your decision on each (confirm, clarify, attach inputs, drop, or mark ready with the tools the work may use). "confirm?" tasks come first. Use when asked to review tasks, triage my actions, confirm captured tasks, create tasks, or clear the confirm? queue.
# D.3 role "/create-tasks review": MCP read + task_update_status only. Both naming forms:
# registered by hand (claude mcp add) and from the packaged plugin "whispr".
allowed-tools:
  - mcp__whispr-tasks__task_list
  - mcp__whispr-tasks__task_get
  - mcp__whispr-tasks__task_update_status
  - mcp__whispr-kg__search
  - mcp__whispr-kg__get
  - mcp__whispr-kg__source
  - mcp__plugin_whispr_whispr-tasks__task_list
  - mcp__plugin_whispr_whispr-tasks__task_get
  - mcp__plugin_whispr_whispr-tasks__task_update_status
  - mcp__plugin_whispr_whispr-kg__search
  - mcp__plugin_whispr_whispr-kg__get
  - mcp__plugin_whispr_whispr-kg__source
---

# /create-tasks: review captured tasks

You run a review with the user over the task server (`whispr-tasks`). A task's state
lives only in the database (lesson L19): **every change goes through
`task_update_status`**. Never write, edit or create a note, file or vault page, and use
no tool outside `allowed-tools`.

## 1. Build the queue

1. `task_list` with `confirm_only: true`: the **"confirm?"** tasks. Their ownership
   could not be settled from the transcript, or their only quote was a line removed as
   speaker echo, or they have no quote.
2. `task_list` (default status `captured`): the rest of your own tasks, newest meeting
   first. Skip the ones step 1 already showed.
3. When a result says `truncated`, call again with `offset`. If both lists are empty,
   say so and stop.

Say how many tasks are waiting and how many are "confirm?", then go through them.

## 2. For each task

Show it compactly:

- **Action**, marked **confirm?** when `confirm` is true (the card doesn't say which of the three reasons; the quote usually shows it)
- **Owner** and `owner_basis`; **Due**, or "no due date stated" when `due_basis` is `not_stated`
- **Quote**, verbatim, with the meeting time and `source.start`; the **context**
- the transcript path, for the user to open

If the user wants more context, use `whispr-kg` `source` (the task's `source.episode`,
with `item_id` set to the task id) or `search`/`get`. Never guess an owner or a due date.

Ask what to do: **confirm**, **clarify**, **drop**, **mark ready**, or **skip**. A short
batch is fine when the user answers several at once.

## 3. Record exactly what the user decided

| Decision | Call `task_update_status` with |
|---|---|
| Confirm | `status: "confirmed"`, `reason` in the user's words |
| Clarify | `clarification` (a corrected owner, due date or scope) and/or `inputs` (file paths, links, values the work needs); add `status: "confirmed"` and a `reason` if they also confirmed it |
| Drop | `status: "dropped"`, `reason` (not mine, duplicate, already done) |
| Mark ready | first confirm it if it is still `captured` (two calls). Then `status: "ready"`, `reason`, and `tools_allowed` only after asking which tools the work may use. Leave it out for a read-only worker. Never propose shell or write tools unless the task needs them and the user agrees |
| Skip | no call |

If a call returns `isError`, tell the user. For an invalid move, offer the states in
`allowed`. Don't retry another way without asking. After a change, a one-line
confirmation is enough (`task_get` shows the full history if asked).

## 4. Close

Summarize: confirmed, clarified, dropped, marked ready, skipped. Give the "confirm?"
count still waiting.
