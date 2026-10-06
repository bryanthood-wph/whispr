# Task intake and worker

**Status:** BUILDING, 2026-10-06. Revised after the premortem, and again after P0: Teams
can't be read by any tool on this machine, so Teams context is pasted at intake. It replaces the README §3 deferred row
"clarify-style task intake" and RESUME's FUTURE STATE note. The outbox is set aside for now.

**In one screen:**
- **Intake.** `/whispr:create-tasks` grows a clarify-style intake. For each task Claude
  proposes the **scope** (Teams channels and chats, email, folders, repos), you edit it,
  then it asks round by round for every other field, **including the task's budget**, until
  none is open. A task can be `ready` only when nothing is open, and the server enforces
  that, not just the skill.
- **Worker.** `python -m pipeline task run <id>`, which you start from a terminal in the
  task's own folder or repo. It is a separate `claude` process with hard limits on tools,
  paths, model, effort and budget. Opus 5.5 at high effort is the manager and does the
  drafting. A researcher and a reviewer are its subagents, plus a code worker for code tasks.
  It searches only the scope set at intake, and writes to `%LOCALAPPDATA%\whispr\tasks\<id>\`.
- **Budgets.** Each task gets its own budget, proposed at intake from what that type of work
  has cost before, and approved by you. A run that reaches its budget stops, reports what is
  done, and proposes an amount to continue. It goes on only with your yes.
- **Microsoft tools.** Admin's Outlook tools are reviewed, then **reimplemented in whispr**
  (no copied code) so they work from any folder. They can read and save unsent drafts.
  **Nothing sends.** **Teams channels and chats are pasted at intake**: Deloitte policy
  blocks the browser route Admin uses for Teams (P0).
- **What good looks like** is a library, one folder per work type. It grows from your
  answers and from deliverables you approve, so over time it stops being a question.
- **Design rule:** the simplest design that is effective and scales. A fragile route is
  a cost, and the plan names it as one.

---

## 1. Decisions (all from the 2026-10-06 clarify gate and premortem)

| Decision | Source |
|---|---|
| This extends the plugin's `/whispr:create-tasks`, not the cohoodOBS skill | confirmed |
| Intake asks the scope first: Teams channels and chats, email, folders, GitHub repos | stated |
| Then a `/clarify`-style pass resolves every other uncertainty, so nothing is inferred | stated |
| Claude proposes the scope (project defaults plus call details) and you edit it | confirmed |
| Scope defaults are saved **on the project entity**, then edited per task | confirmed |
| Intake asks **which Teams chats have relevant context** (no static narrowing list) | stated |
| Required before `ready`: deliverable and format, audience, what good looks like, due date and constraints, scope, and budget | confirmed |
| "What good looks like" is one folder per work type under the whispr data folder, growing from use | stated / confirmed |
| Sources are searched **at execution, within scope** | confirmed |
| The worker is whispr's own: Opus 5.5 high as manager, with subagents | stated / confirmed |
| It is started from a terminal in the task's folder, as its own `claude` process (premortem F1) | confirmed |
| Budgets are set per task. Raising one needs your approval, and the worker proposes the amount (F2) | stated |
| Roster: researcher Sonnet 5.5 medium, reviewer Opus 5.5 high, code worker Opus 5.5 high (F3) | confirmed |
| The manager drafts; there is no separate drafter role (F10) | confirmed |
| Output goes to `%LOCALAPPDATA%\whispr\tasks\<id>\`, and repo changes to a branch in that repo | confirmed |
| If a source is down, the worker starts what it can and lists what it skipped | confirmed |
| Admin's tools are reviewed for improvements, then reimplemented in whispr | stated |
| The tools can read and save unsent drafts, with no sends | confirmed |
| Repos are local clones | stated |
| Teams chats got a spike through browser Teams. It was blocked by policy, so Teams channels **and** chats are pasted at intake (P0, 2026-10-06) | stated / confirmed |
| Set aside: the outbox, scheduled or unattended runs, and anything that sends | stated / confirmed |

**Facts the plan rests on** (checked 2026-10-06):
- **Admin** (Huxley Waitt, **v0.8.0**, `UNLICENSED`, "not built for deployment") is enabled
  only in `C:\github\admin-workspace`. Its servers refuse to start outside that folder and
  its `.venv`.
- Admin's Teams server can't read chats. The page's Graph token lacks `Chat.Read`, so it
  offers teams, channels and channel messages only. It needs a debug Chrome profile on
  port 9222.
- **Deloitte policy blocks that route on this machine.** Both Chrome and Edge have
  `RemoteDebuggingAllowed = 0` under `HKLM\SOFTWARE\Policies` (checked 2026-10-06; a
  Chrome started with the flag opens but never listens). That is a security setting, and
  whispr doesn't work around it. Any Deloitte machine with the same policy is blocked the
  same way, which also rules out Admin's Teams tools there.
- **Outlook can't reach Teams chats.** A read-only folder scan of your mailbox found
  "Conversation History" with 0 items, and the `TeamsMessagesData` folder isn't exposed
  through Outlook. This route is closed; don't try it again.
- The whispr venv already has `pywin32`. It has no websocket client, so the Teams web
  route needs a new dependency, which needs your approval.
- `pipeline/models.py` already starts `claude` with `--model`, `--effort` and
  `--max-budget-usd`, checks which sign-in it uses, refuses to run inside a Claude session,
  and logs every call to the run ledger. The worker reuses it.
- **Why the worker isn't a slash command:**
  - A slash command makes your own session the manager, with your session's model and every
    tool you have.
  - Claude Code subagents can't start subagents of their own.
  - A skill's `allowed-tools` only pre-approves tools; it doesn't take any away.

## 2. Flow

```
extract → captured → /whispr:create-tasks review (today) ─┐
                                                          ▼
          intake rounds: scope → fields + budget → residual clarify
                                                          ▼
                     ready (server refuses while a field is open)
                                                          ▼
   you, in a terminal in the task's folder: python -m pipeline task run <id>
                                                          ▼  in_progress
      budget reached? → stops, proposes more → you approve → it resumes
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
remove items in one round. It asks only about the source types this project uses. "None"
for a source type is one click, and it's saved to the project defaults (F17).

| Source | What's recorded | How the worker reaches it |
|---|---|---|
| Teams channels | team and channel, plus the lines you paste | the pasted lines (a task input) |
| Teams chats | the chats you name as having context, plus the lines you paste | the pasted lines (a task input) |
| Email | senders, threads or folders, and a date window | whispr-m365 Outlook read |
| Folders | absolute paths | read permission limited to those paths |
| Repos | local clone paths | read permission limited to those paths; a branch when the work is code |

At the end of the round, Claude asks whether to save changes back to the project's defaults.

**Round 2: fields and budget.**
- **Deliverable and format.** This also names the **work type**.
- **Audience.**
- **What good looks like.** It comes from `good\<work-type>\` when that folder has content,
  and you only confirm it or change it. It is asked from scratch only for a new work type.
- **Due date and constraints.**
- **Budget.** Claude proposes a figure, with the reason, and you approve or change it.
  The figure is worked out in this order:
  1. what runs of this work type have cost, from the ledger;
  2. adjusted for this task's scope and size;
  3. with no history yet, the seed for that work type in config (§7 P3 sets the seeds
     from measured runs).

**Round 3: residual clarify.** Claude scans the brief the way `/clarify` does: purpose,
forks inside the work, reference points. It asks whatever is still open. Then comes the
gate: a summary of the brief, Proceed?, and the open door ("what haven't I asked?").

**Ready gate.** `task_update_status(status: "ready")` returns an error while any required
field is empty. It's checked in the server, so no skill path can skip it. The required
field list comes from config (`tasks.intake.required_fields`). Config loading rejects an
empty list, or a name that isn't a known brief field, with a ConfigError, the same pattern
as `schedules.skip` (F8).

## 4. Data (lifecycle stays in the database, lesson L19)

- **Brief answers** are `task_note` rows, with kind set to the field name, the answer, who
  gave it (stated or confirmed), and when. That table already exists, so there's no new
  table (F9). The newest row for a field wins, and the history is kept.
- **`entity_scope`:** a project entity's default scope (entity_id, source type, value).
- **MCP changes** on `whispr-tasks`:
  - `task_update_status` gains `brief` and `scope` arguments.
  - New `project_scope_get` and `project_scope_set` tools.
  - `task_get` returns the brief.
- **Files:**
  - `tasks\<id>\`: output and the run log. It holds no state, since the state is in the
    database.
  - `good\<work-type>\`: `rubric.md` (your answers) and `examples\` (approved deliverables,
    each dated). The reviewer grades against the rubric first. It reads only the newest N
    examples, with N in config (`tasks.good.examples_read`), so tokens don't grow with the
    folder (F11). Removing an example you no longer want as the bar is one step (F12).
- **Backup:** today's backup copies the database. It is extended to include `tasks\` and
  `good\` in **P3**, when `tasks\` first fills (F15).

## 5. Microsoft tools: `whispr-m365` (a new plugin MCP server)

**Step 1 is a review, not a build.** Read Admin **v0.8.0**'s `outlook_mcp`, `teams_mcp` and
`m365_cdp`, and list what to keep, what to improve, and what to leave out. The review notes
go into this plan, not just a pointer to the plugin cache, which moves when Admin updates
(F16).

### P0 review notes: Admin v0.8.0 (2026-10-06, read-only review of the source)

The licence is `UNLICENSED` (`.claude-plugin/plugin.json:9`), with a no-responsibility
disclaimer (`plugin.json:4`, `README.md:15-17`) and no LICENSE file. That confirms whispr
reimplements this, and copies nothing.

**Outlook (COM): keep**
- **One thread for all COM work.** whispr's single-threaded serve loop
  (`kg/mcp_server.py`) inside `com_initialized()` gives the same guarantee without a
  worker thread.
- **Attach to a running Outlook before launching one.** It finds the exe through App
  Paths.
- **One retry on an RPC disconnect, for reads only.**
- **Partial results say so** (`search_complete`, `truncated_reason`, `scanned_count`).
  This feeds the F7 output header.
- **Calendar recurrences:** IncludeRecurrences, then Sort, then Restrict.
- **Item ids:** store id plus entry id, opened with GetItemFromID.
- **Attachments:** listing returns metadata only. Saving never overwrites, returns a
  sha256, and checks that the path stays inside the folder.
- **Drafts:**
  - built with Outlook's own Reply/ReplyAll/Forward, which keeps threading;
  - forwards check that the original's attachments came through;
  - no code calls Send;
  - a timeout is reported as "unknown, do not retry".
- **An allowlist sanitizer for generated HTML.**

**Outlook: improve** (Admin's behaviour, then whispr's)
1. **Metadata search.** Admin scans up to 10,000 items in Python, about 15 COM calls
   each, with an Exchange lookup for every sender. whispr uses
   `Folder.GetTable("@SQL=...")` with added columns, or a DASL `Restrict`, and opens an
   item only for get_message.
2. **Body search.** Admin reads the whole body of every scanned item. whispr restricts on
   `urn:schemas:httpmail:textdescription LIKE` first, then cuts snippets from the hits.
3. **get_message.** Admin returns the full body, with an HTML option. whispr returns
   text only, capped, with a truncated flag, marked untrusted, and without internet
   headers.
4. **Attach once.** Admin re-attaches to Outlook and walks every store on each call.
   whispr caches the app, namespace and default store, and re-attaches only after a
   disconnect.
5. **Calendar dates.** Admin's filter uses the US format `%m/%d/%Y`. whispr tests it on
   this PC or formats to the locale, and caps the length of a search window.
6. **Partial calendar results.** Admin returns nothing from an incomplete calendar
   search. whispr returns the partial results, the way mail search does.
7. **Errors.** Admin swallows exceptions (`_safe`), so items silently drop out of
   filters. whispr counts errors per search and reports the count.
8. **Attachment saves.** Admin saves to a path the caller chooses. whispr saves only to
   `tasks\<id>\attachments`, using the file name alone, strips `:` and Windows reserved
   names, and caps the size.
9. **Draft attachments.** Admin allows any file on disk. whispr allows only files under
   `tasks\<id>\`.
10. **Reply recipients.** Admin takes them from the original message. whispr checks the
    built draft's recipients against the people the brief names before `Save()`.
11. **Where draft text goes.** Admin puts new text before `<html>`. whispr inserts it
    after `<body>`.
12. **No draft windows.** Admin calls `Display()`. The headless worker doesn't.
13. **Timeouts.** In Admin a timed-out call keeps running and blocks the queue. whispr
    marks the server unhealthy and refuses further calls.
14. **Task marker on drafts (F13).** whispr stores the task id in a UserProperty and
    checks Drafts for it before creating another.

*The Teams web notes below are kept for the record only: policy blocks the route (§1), so P2 doesn't build it.*

**Teams web (CDP): keep**
- **Finding the page.** The Teams tab is looked up through the local debug endpoint on
  every call, so the server can start before Chrome is open.
- **Where it connects.** Loopback only, https only, a host allowlist, and a fixed table
  of target hosts.
- **Requests run inside the page,** with the page's own session.
- **Input handling.** Ids are percent-encoded per path segment and validated. Values are
  JSON-encoded before they go into the page script.
- **Output limits.** `limit` is held to 1–50, and message bodies are labelled untrusted.
- **Errors.** A 401 maps to a "reload the tab" hint.

**Teams web: improve**
- **Local port exposure.** Admin uses the well-known port 9222, and while the profile is
  open any program on the PC can drive it.
  - whispr uses a non-default port and connects only to a profile you started (F14).
  - It reminds you to close the profile.
  - It doesn't run code tasks while the profile is open, or keeps a shell tripwire.
- **Message size and injection.** Bodies come back as raw HTML with no cap. whispr:
  - picks out only the needed fields inside the page;
  - converts HTML to text;
  - caps length per message and in total;
  - returns the text as quoted data to the researcher only (F5).
- **Silent failures:**
  - A missing `websockets` install is reported as "connection failed". whispr names the
    real cause.
  - A 200 with no `value` becomes an empty list ("no teams"). whispr treats it as an
    error.
  - Result paging (`@odata.nextLink`) is ignored, so long lists are silently cut off.
    whispr follows it up to a cap, or reports `has_more`.
- **Fragility.** whispr runs a health check before each run, and reports Teams as
  skipped when it fails.

**Leave out**
- **Admin's startup machinery,** replaced by the task-scope check and whispr's RpcServer.
  This is safe because the check refuses when the task id is missing or unknown, and the
  pipeline, not the model, writes the MCP config:
  - the roots/list and project-marker checks;
  - the venv check;
  - the mcp-SDK serving workarounds.
- **Body-search receipts and cursors.** offset/limit within a date window replaces them;
  they protected nothing.
- **The write-confirmation two-step and its hooks.** They gated writes only. Draft
  previews are replaced by attachments confined to the task folder, the recipient check,
  and your review of every unsent draft.
- **Out-of-scope tools:** batch drafts, invites, contacts, Free/Busy, sends, meetings,
  and the all-folders walk.
- **The tool-name allowlist hook stays,** but its list is generated from the server's own
  manifest. Admin keeps two hand-copied lists that can drift apart.

**Dependency:** the Teams route would have needed `websockets` (not stdlib). With Teams
pasted at intake, P2 needs no new package.

**Teams chats:** not reachable this way. Admin's comment (`teams_mcp/tools_messaging.py:4-6`)
says the session lacks `Chat.Read`, and its host table is fixed to Graph. A chat read would
mean adding the Teams chat service as a new target. That's what the chats spike tests.

| Area | Tools (read plus unsent drafts) | Left out |
|---|---|---|
| Outlook (Classic, COM) | search message metadata → bounded body snippets, get message, list and save attachments (only into `tasks\<id>\`), calendar search and get, unsent draft, reply draft, forward draft | send, move, delete, flag, saving meetings |

**How each call is limited:**
- **Bound to one task (F6).** The worker starts the server with `WHISPR_TASK_ID` in its MCP
  config.
  - The server loads that task's scope and refuses any query outside it.
  - With no task id, it refuses everything.
  - Admin's project-folder lock is replaced by this check.
- **Read tools and draft tools are served separately (F5):**
  - the read tools go to the researcher only;
  - the draft tools go to the manager only;
  - a draft's recipients must be people the brief names, or the server refuses it.
- **Drafts are marked (F13):**
  - each draft carries its task id;
  - a re-run reports the drafts that already exist instead of making more;
  - the run log lists every draft created.
- **Kept from Admin:**
  - attachment saves are confined to one folder;
  - a hook blocks any tool name outside the list.
- **COM:** the helper reuses the `com_initialized` pattern.

**Chats spike: result (2026-10-06).** The debug Chrome profile started with
`--remote-debugging-port` but never listened. Machine policy disables remote debugging in
Chrome and Edge (§1). Neither spike route can run, and Teams channels are blocked the same
way. Outcome, by your choice: **Teams channels and chats are pasted at intake** and saved as
task inputs. The output header lists them as "pasted". F14 (the debug profile exposure) no
longer applies, because no profile is used.

## 6. Worker: `python -m pipeline task run <id>`

**Where and how:**
- You start it from a terminal in the task's folder or repo.
- It goes through `pipeline/models.py` as its own `claude` process, so the limits are
  enforced by the process, not just written as instructions (F1).
- Like every pipeline call, it refuses to run inside a Claude session.

**What the process is given:**
- **Model and effort:** `--model`/`--effort` from config. The manager is Opus 5.5 high.
- **Subagents:** `--agents`, the roster below, each with its own tools, model and effort.
- **Tools and paths (F4):** `--allowedTools` plus permission rules generated from the
  task's scope.
  - Read is allowed only on the scope's folders and repos.
  - Write and Edit are allowed only in `tasks\<id>\`, or in the repo on the task's branch
    for code tasks.
  - Everything else is denied.
  - A test proves that a read outside the scope is refused.
- **MCP servers:** an MCP config listing only `whispr-tasks`, `whispr-kg` and `whispr-m365`
  (the latter bound to this task).
- **Budget:** `--max-budget-usd` set to the task's approved budget. Every call's cost goes
  to the run ledger. A run that spends money and produces no output raises an alert
  (lesson L3).

**Steps:**
1. Loads the brief and scope, and sets the task to `in_progress`.
2. **Preflight:** checks each in-scope source. It opens Outlook if it's closed. Teams items
   are already in the brief as pasted lines.
3. Plans the work and writes the plan as a step list to `tasks\<id>\progress.md`. It
   updates the list as steps finish.
4. Research goes to the researcher. The manager drafts, and the reviewer checks the draft
   against `good\<work-type>\` before you see it.
5. **Output header (F7):** lists every scope item with its status: read N items, 0 hits, or
   skipped with a reason. A 0-hit item is a warning, and the reviewer must mention it.
6. Writes the output and run log to `tasks\<id>\`, and code changes to a new branch.
7. Leaves the task for you:
   - **approve** → `done`, and the deliverable is added to `good\<work-type>\examples\`;
   - **send back** → `ready`, with your notes on the brief.

**When the budget runs out (F2):**
1. The `claude` process stops at its cap.
2. The pipeline reports what was spent and what is done (from `progress.md`), and proposes
   an amount to finish: the spend so far, scaled by the share of steps left, with the
   reason.
3. It asks you in the terminal. The run goes on only after a yes, by resuming the same
   session with the extra amount. A no leaves the partial output and the task stays
   `in_progress`.
4. The task's budget record keeps the original figure and every increase you approved.

**Roster** (defaults in config; every grant has a reason):

| Role | Model / effort | Tools | Why |
|---|---|---|---|
| Manager (the process itself) | Opus 5.5 / high | whispr-tasks read plus its own status updates; whispr-kg read; whispr-m365 **draft tools only**; Write in `tasks\<id>\`; starting the roles below | plans, drafts and checks. It holds no tool that reads mail or Teams, so text from those sources can't steer what it writes or drafts (F5) |
| Researcher | Sonnet 5.5 / medium | whispr-m365 **read tools only**; whispr-kg read; Read/Grep limited to the scoped paths | searching and quoting is high-volume, and Sonnet is accurate enough for it. It returns quoted excerpts marked as data, never instructions |
| Reviewer | Opus 5.5 / high | read-only: the output, the brief, `good\<work-type>\` (rubric plus the newest N examples) | judging quality needs the strongest model |
| Code worker (code tasks only) | Opus 5.5 / high | Edit plus shell inside the repo, on the task's branch, only when you approved it in `tools_allowed`; no whispr-m365 tools | code changes need a shell to run tests. With no mail or Teams access, it can't be steered by them |

## 7. Phases (build later)

| Phase | Work | Done when |
|---|---|---|
| P0 | Review Admin v0.8.0's tools (notes in this plan) and run the chats spike | **done 2026-10-06**: review accepted; the spike was blocked by policy, so Teams is pasted at intake |
| P1 | Brief answers as `task_note` rows, `entity_scope`, the MCP arguments, the ready gate with its config check, the intake rounds in `/whispr:create-tasks` | a captured task goes through intake to `ready`; `ready` with an open field is refused, and an empty or misspelt `required_fields` is a ConfigError (both tested) |
| P2 | `whispr-m365`: Outlook read and drafts, task binding, the read/draft split, the recipient check | read tests pass against your mailbox; a query outside scope, a call with no task id, and a draft to an unnamed recipient are refused; no send tool exists (all tested) |
| P2b | **whispr's knowledge is used first** (your item, 2026-10-06). Today, every Claude Code session has the graph tools and their instructions, but using them is the model's call. The tools start hidden, and the older `/vault` skill and server compete for the same questions. **Vet, then add the simplest mechanism that ships with the plugin**, so it reaches every machine without a hand-edited file. Candidates:<br>• a short note from the plugin's existing session-start hook: for meetings, people, projects and tasks, use whispr-kg first and cite the quote; `/vault` only for what the graph doesn't cover, until it retires (D.8)<br>• a rule in your user CLAUDE.md, which covers this machine only<br>Also check that intake and the worker reach for the graph when they need context on a task (the scope proposals, the researcher's tools). | **Vetted by measurement, before and after:** a set of real questions from your current work, asked in fresh sessions, scored on whether whispr-kg was used first and the answer cites a transcript quote. The question set and the pass bar are agreed with you at the start of P2b. The mechanism that passes is the one added |
| P3 | `task run`: roster, permission rules, preflight, output header, `progress.md`, budget stop-and-propose, ledger, backup of `tasks\` and `good\` | three real tasks of different types run end to end, each in its own folder. A read outside scope is refused. A closed source is listed as skipped. A run stopped at a deliberately low cap proposes an amount and resumes on yes. **The measured costs become the budget seeds per work type in config** |
| P4 | The `good\` library growing from approvals | an approved deliverable lands in `examples\`; the next task of that type doesn't ask for what good looks like |

## 8. Risks and what's left out

- **Distribution:** the Outlook route needs **Classic Outlook**. A colleague on New Outlook
  would need a web route, as Admin's `outlook_web` provides. It's left out for now and
  noted for broad distribution.
- **Budgets before history:** until P3 has measured real runs, there are no seeds, so each
  P3 run's budget is proposed with its reasoning and approved by you.
- **Left out:** the outbox, sending anything, scheduled or unattended runs.
