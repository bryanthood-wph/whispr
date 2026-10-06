---
name: create-tasks
description: Review the tasks whispr captured from your meetings, one at a time, and record your decision on each (confirm, clarify, attach inputs, drop, or mark ready through an intake that settles the task's scope, brief and budget). "confirm?" tasks come first. Use when asked to review tasks, triage my actions, confirm captured tasks, create tasks, or clear the confirm? queue.
# D.3 role "/create-tasks review": MCP read + task_update_status (and project_scope_set
# for a project's default scope) only. Both naming forms: registered by hand
# (claude mcp add) and from the packaged plugin "whispr". AskUserQuestion asks the
# intake's rounds. No file tools.
allowed-tools:
  - mcp__whispr-tasks__task_list
  - mcp__whispr-tasks__task_get
  - mcp__whispr-tasks__task_update_status
  - mcp__whispr-tasks__project_scope_get
  - mcp__whispr-tasks__project_scope_set
  - mcp__whispr-kg__search
  - mcp__whispr-kg__get
  - mcp__whispr-kg__source
  - mcp__plugin_whispr_whispr-tasks__task_list
  - mcp__plugin_whispr_whispr-tasks__task_get
  - mcp__plugin_whispr_whispr-tasks__task_update_status
  - mcp__plugin_whispr_whispr-tasks__project_scope_get
  - mcp__plugin_whispr_whispr-tasks__project_scope_set
  - mcp__plugin_whispr_whispr-kg__search
  - mcp__plugin_whispr_whispr-kg__get
  - mcp__plugin_whispr_whispr-kg__source
  - AskUserQuestion
---

# /create-tasks: review captured tasks

You run a review with the user over the task server (`whispr-tasks`). A task's state
lives only in the database (lesson L19): **every change goes through
`task_update_status`** (and a project's default scope through `project_scope_set`).
Never read, write, edit or create a note, file or vault page, and use no tool outside
`allowed-tools`.

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
| Mark ready | first confirm it if it is still `captured` (two calls). Then run the **intake** (section 4); the move to `ready` is its last step |
| Skip | no call |

If a call returns `isError`, tell the user. For an invalid move, offer the states in
`allowed`. Don't retry another way without asking. After a change, a one-line
confirmation is enough (`task_get` shows the full history if asked).

## 4. Mark ready: the intake

A task can be `ready` only when its brief has no open field: the server refuses the
move otherwise, whatever this skill does. The intake settles the brief in three rounds,
the way `/clarify` runs:

- **Every question goes through AskUserQuestion**, one round at a time (a round with
  more questions than one call holds is split over calls). Each question names what is
  open, proposes options with the likely one first, and leaves free text open.
- **Start from `task_get`.** `brief` holds the answers so far (newest per field),
  `open_fields` the required fields still unanswered. The `task_update_status` schema
  lists every brief field with the question to ask for it. Don't re-ask an answered
  field; offer to keep it.
- **Record each round with one `task_update_status` call:** `brief` (field -> answer)
  and/or `scope`, with `brief_source` `"stated"` when the user wrote the answer, or
  `"confirmed"` when they picked what you proposed. Record only what the user answered;
  never fill a field yourself.

**Round 1: scope.**
1. Find the task's project. `task_get`'s `projects` lists the projects it is linked to.
   If there is none, or more than one, ask which project it belongs to (candidates from
   `whispr-kg` `search`), with "no project" as an option. Save the answer: a project the
   user named goes in this round's `task_update_status` call as `project` (its entity id),
   which links the task to it. A link is only ever added, never removed.
2. `project_scope_get` on that project: its saved items, and `open_types` (source types
   with nothing saved). A type saved as `none` has nothing in scope: don't ask about it.
3. Add candidates from the call. Use `source` on the task's episode and `get` on what it
   names: the meeting subject, the attendees (email senders, Teams chats), and systems
   or repos mentioned. A folder or repo is offered only as a path the user or the graph
   gave; never invent one.
4. Ask one question per source type: confirm or edit the saved values and candidates
   (multi-select), and for an open type offer **none** as a one-click option. Ask about
   Teams chats by name: which chats have context for this task?
   **Email** is recorded one clause per scope item, in the grammar the worker's Outlook
   tools read (the `email` scope type's description in the `task_update_status` schema
   spells it): `sender:jane@example.com` or `sender:@example.com` (a whole domain),
   `folder:Inbox/Projects` (a path under the mailbox; its subfolders count),
   `subject:<words in the thread's subject>`, `since:YYYY-MM-DD`, `until:YYYY-MM-DD`.
   Propose senders from the attendees' addresses, and ask the window as a since date
   (with none, the server's default window applies). Clauses of one kind are
   alternatives; different kinds all apply. A draft may go only to an address the brief
   names, so a recipient the user wants should be named by address in the audience or a
   `sender:` item. A malformed clause is refused by name: fix that item, don't drop the type.
5. **Teams is pasted, not read.** No tool on this machine can read Teams, so for each
   `teams_channel` and `teams_chat` item in the answer, ask the user to paste the lines
   that matter. Save each paste as one of the call's `inputs`, starting with the item's
   name (e.g. `Teams chat "Apollo core team": <the pasted lines>`). Keep the item in the
   scope too, so the worker's output can list it as pasted. If the user has nothing to
   paste for an item, ask whether to drop it from the scope.
6. Record `scope`: every source type answered, `none` for a type with nothing in scope,
   in one call with `project` and the pasted `inputs`.
7. If the answer differs from the project's saved scope, ask whether to save it back as
   the project's default. On yes, `project_scope_set` with the whole new list (it
   replaces the saved one). With no project, skip this.

**Round 2: fields and budget.** Ask the open fields that remain:
- **Deliverable and format**, and from it the **work type** (one short name, e.g.
  `status-memo`), recorded as `work_type`.
- **Audience.**
- **What good looks like.** Ask it every time: the library of past answers per work
  type doesn't exist yet. Propose what the deliverable, audience and call suggest, for the
  user to confirm or change.
- **Due date and constraints.** Propose the task's `due` when one was stated.
- **Budget.** Propose a figure with its reason, for the user to approve or change. There
  is no run history yet, so there is no seed figure for any work type: say so, and base
  the figure on the task's scope and size, saying how. Record it as the budget object the
  `task_update_status` schema describes: the amount in USD (at least the minimum the
  schema states) and the reason.

**Round 3: residual clarify, then the gate.**
1. Scan the brief the way `/clarify` does: the purpose behind the deliverable, forks
   inside the work (two readings of the ask, two ways to do it), and reference points
   (an earlier version, an example to match). Ask whatever is still open; record
   answers as a `clarification`, or as a newer `brief` answer when they change a field.
2. The gate: show a summary of the brief (each field and its answer, and the scope by
   source type), then ask **Proceed?**, with the open door: "What haven't I asked that
   matters for this?" Act on anything it raises before going on.
3. On proceed, ask which tools the work may use. Leave `tools_allowed` out for a
   read-only worker, and never propose shell or write tools unless the task needs them
   and the user agrees. Then `task_update_status` with `status: "ready"`, a `reason`
   and `tools_allowed` if any.
4. If the move returns `isError` with `open_fields`, go back to the round that asks
   those fields. Don't try another way.

## 5. Close

Summarize: confirmed, clarified, dropped, marked ready, skipped. Give the "confirm?"
count still waiting.
