# Task intake and worker

**Status:** PLAN, 2026-10-06. **Build later:** nothing here starts until you say go.
It replaces the README §3 deferred row "clarify-style task intake" and RESUME's
FUTURE STATE note. The outbox is set aside for now.

**In one screen:**
- **Intake.** `/whispr:create-tasks` grows a clarify-style intake. For each task Claude
  proposes the **scope** (Teams channels and chats, email, folders, repos), you edit it,
  then it asks round by round for every other field until none is open. A task can be
  `ready` only when nothing is open, and the server enforces that, not just the skill.
- **Worker.** `/whispr:run-task`, which you start on demand in the task's own folder or
  repo. Opus 5.5 at high effort acts as manager and spawns least-privilege subagents. It
  searches only the scope set at intake, and writes to `%LOCALAPPDATA%\whispr\tasks\<id>\`.
- **Microsoft tools.** Admin's Outlook and Teams tools are reviewed, then **reimplemented
  in whispr** (no copied code) so they work from any folder. They can read and save unsent
  drafts. **Nothing sends.**
- **What good looks like** is a library, one folder per work type. It grows from your
  answers and from deliverables you approve, so over time it stops being a question.
- **Design rule:** the simplest design that is effective and scales. A fragile route is
  a cost, and the plan names it as one.

---

## 1. Decisions (all from the 2026-10-06 clarify gate)

| Decision | Source |
|---|---|
| This extends the plugin's `/whispr:create-tasks`, not the cohoodOBS skill | confirmed |
| Intake asks the scope first: Teams channels and chats, email, folders, GitHub repos | stated |
| Then a `/clarify`-style pass resolves every other uncertainty, so nothing is inferred | stated |
| Claude proposes the scope (project defaults plus call details) and you edit it | confirmed |
| Scope defaults are saved **on the project entity**, then edited per task | confirmed |
| Intake asks **which Teams chats have relevant context** (no static narrowing list) | stated |
| Required before `ready`: deliverable and format, audience, what good looks like, due date and constraints, and scope | confirmed |
| "What good looks like" is one folder per work type under the whispr data folder, growing from use | stated / confirmed |
| Sources are searched **at execution, within scope** | confirmed |
| The worker is whispr's own, on Opus 5.5 high, as manager plus subagents | stated / confirmed |
| It runs on demand, in the task's folder or repo | stated / confirmed |
| Output goes to `%LOCALAPPDATA%\whispr\tasks\<id>\`, and repo changes to a branch in that repo | confirmed |
| If a source is down, the worker starts what it can and lists what it skipped | confirmed |
| Admin's tools are reviewed for improvements, then reimplemented in whispr | stated |
| The tools can read and save unsent drafts, with no sends | confirmed |
| Repos are local clones | stated |
| Teams chats get a spike through browser Teams; if it fails, you paste the lines at intake | stated / confirmed |
| Set aside: the outbox, scheduled or unattended runs, and anything that sends | stated / confirmed |

**Facts the plan rests on** (checked 2026-10-06):
- **Admin** (Huxley Waitt, v0.8.0, `UNLICENSED`, "not built for deployment") is enabled only
  in `C:\github\admin-workspace`. Its servers refuse to start outside that folder and its
  `.venv`.
- Admin's Teams server can't read chats. The page's Graph token lacks `Chat.Read`, so it
  offers teams, channels and channel messages only. It needs a debug Chrome profile on
  port 9222.
- **Outlook can't reach Teams chats.** A read-only folder scan of your mailbox found
  "Conversation History" with 0 items, and the `TeamsMessagesData` folder isn't exposed
  through Outlook. This route is closed; don't try it again.
- The whispr venv already has `pywin32`. It has no websocket client, so the Teams web
  route needs a new dependency, which needs your approval.

## 2. Flow

```
extract → captured → /whispr:create-tasks review (today) ─┐
                                                          ▼
                  intake rounds: scope → fields → residual clarify
                                                          ▼
                     ready (server refuses while a field is open)
                                                          ▼
       you, in the task's folder: /whispr:run-task <id>  →  in_progress
                                                          ▼
          tasks\<id>\ output + run log  →  you approve → done
                                           (deliverable feeds good\<type>\)
                                       or  send back → ready, with your notes
```

## 3. Intake (extends `/whispr:create-tasks`)

The review steps that exist today (confirm, clarify, drop) stay as they are. **Mark
ready** becomes the intake. Every question goes through AskUserQuestion, one round at a
time, the way `/clarify` runs: it names what's open, proposes options with a likely one,
and leaves free text open.

**Round 1: scope.** Claude links the task to its project entity (from `task_entity`, or
asks), loads the saved scope, and adds candidates from the call: attendees, meeting
subject, and systems or repos mentioned (via `whispr-kg` `source`/`get`). You add or
remove items in one round.

| Source | What's recorded | How the worker reaches it |
|---|---|---|
| Teams channels | team and channel | whispr-m365 Teams read |
| Teams chats | the chats you name as having context | the chats spike (§5); if it fails, the lines you paste |
| Email | senders, threads or folders, and a date window | whispr-m365 Outlook read |
| Folders | absolute paths | file read inside those paths only |
| Repos | local clone paths | file read; a branch when the work is code |

At the end of the round, Claude asks whether to save changes back to the project's defaults.

**Round 2: fields.** Deliverable and format (this also names the **work type**), audience,
what good looks like, due date and constraints. "What good looks like" comes from
`good\<work-type>\` when that folder has content, and you only confirm it or change it. It is
asked from scratch only for a new work type.

**Round 3: residual clarify.** Claude scans the brief the way `/clarify` does: purpose,
forks inside the work, reference points. It asks whatever is still open. Then comes the
gate: a summary of the brief, Proceed?, and the open door ("what haven't I asked?").

**Ready gate.** `task_update_status(status: "ready")` returns an error while any required
field is empty. It's checked in the server, so no skill path can skip it. The required
field list comes from config (`tasks.intake.required_fields`), not from code.

## 4. Data (lifecycle stays in the database, lesson L19)

- **`task_brief`:** one row per field per task (task_id, field, value, source: stated or
  confirmed, at). Answers that change get a new row. The newest row wins, and the history
  is kept.
- **`entity_scope`:** a project entity's default scope (entity_id, source type, value).
- **MCP changes** on `whispr-tasks`:
  - `task_update_status` gains `brief` and `scope` arguments.
  - New `project_scope_get` and `project_scope_set` tools.
  - `task_get` returns the brief.
- **Files:**
  - `tasks\<id>\`: output and the run log. It holds no state, since the state is in the
    database.
  - `good\<work-type>\`: `rubric.md` (your answers) and `examples\` (approved deliverables).
- **Backup:** today's backup copies the database. It must be extended to include `tasks\`
  and `good\`, or the library isn't protected.

## 5. Microsoft tools: `whispr-m365` (a new plugin MCP server)

**Step 1 is a review, not a build.** Read Admin's `outlook_mcp`, `teams_mcp` and
`m365_cdp` and list what to keep, what to improve, and what to leave out. Write it up
before any code.

| Area | Tools (read plus unsent drafts) | Left out |
|---|---|---|
| Outlook (Classic, COM) | search message metadata → bounded body snippets, get message, list and save attachments (only into `tasks\<id>\`), calendar search and get, unsent draft, reply draft, forward draft | send, move, delete, flag, saving meetings |
| Teams web (CDP, 127.0.0.1 only, the token stays in the page) | joined teams, channels, read channel messages | sends of any kind |

- Admin's project-folder lock is dropped. In its place, each call is held to the scope on
  the task the worker is running.
- Admin's safeguards that are kept: attachment saves confined to one folder, and a hook
  that blocks any tool name outside the list.
- The COM helper reuses the `com_initialized` pattern.

**Chats spike** (read-only, run against your own chats):
1. Try the Teams web client's own chat service, using the page's session.
2. Try reading the rendered chat with browser automation.

Pass bar: it reads a named chat's recent messages reliably, three runs out of three, with
no new permission. Otherwise chats fall back to **pasting at intake**, saved as a task input.

**Fragility, stated as a cost:** the Teams web route depends on undocumented web APIs and
on a debug Chrome profile that any program on the PC can drive while it's open. The
worker reports Teams as skipped when the page isn't up.

## 6. Worker: `/whispr:run-task <id>`

You start it in the task's folder or repo. It:
1. Loads the brief and scope, and sets the task to `in_progress`.
2. **Preflight:** checks each in-scope source. It opens Outlook if it's closed, and skips
   Teams if no page is up. Skipped sources are listed at the top of the output.
3. Plans the work as manager (Opus 5.5, high), then spawns subagents from the roster.
4. Writes the output and run log to `tasks\<id>\`, and code changes to a new branch.
5. Leaves the task for you: **approve** → `done`, and the deliverable is added to
   `good\<work-type>\examples\`; or **send back** → `ready`, with your notes on the brief.

**Roster** (models and effort in config; every grant has a reason):

| Role | Model / effort | Tools | Why |
|---|---|---|---|
| Manager (the worker) | Opus 5.5 / high | whispr-tasks read plus its own status updates, whispr-kg read, and spawning the roles below | plans and checks; doesn't do the research itself |
| Researcher | per config | whispr-m365 read, whispr-kg read, Read/Grep inside the scoped folders and repos | gathers context, read-only |
| Drafter | per config | Write inside `tasks\<id>\` only; whispr-m365 unsent drafts when the deliverable is an email | produces the deliverable |
| Reviewer | per config | read-only: the output, the brief, `good\<work-type>\` | checks the work against what good looks like before you see it |
| Code worker (code tasks only) | per config | edit plus shell inside the repo, on a branch, only when you approved it in `tools_allowed` | code changes need shell access to run tests |

## 7. Phases (build later)

| Phase | Work | Done when |
|---|---|---|
| P0 | Review Admin's tools (written up) and run the chats spike | the review is accepted; the spike passes, or chats fall back to pasting |
| P1 | `task_brief`, `entity_scope`, the MCP arguments, the ready gate, the intake rounds in `/whispr:create-tasks` | a captured task goes through intake to `ready`; `ready` with an open field is refused (tested) |
| P2 | `whispr-m365`: Outlook read and drafts, Teams read (needs approval for the new dependency) | read tests pass against your mailbox and a channel; no send tool exists (tested) |
| P3 | `/whispr:run-task`, the roster, preflight and the run log | one real task runs end to end in its own folder; a closed source is listed as skipped |
| P4 | The `good\` library growing from approvals, plus backup coverage | an approved deliverable lands in `examples\`; the next task of that type doesn't ask for what good looks like |

## 8. Risks and what's left out

- **Distribution:** the Outlook route needs **Classic Outlook**. A colleague on New Outlook
  would need a web route, as Admin's `outlook_web` provides. It's left out for now and
  noted for broad distribution.
- **Cost:** Opus at high effort, with subagents, runs in your session. The run log records
  the roles used and the time taken.
- **Left out:** the outbox, sending anything, scheduled or unattended runs.
