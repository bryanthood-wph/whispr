# m365/ — whispr-m365, Outlook for one task's worker

What it does: an MCP server over Classic Outlook (COM) for one task: mail metadata and body-snippet search, one message's text, attachment list and save, calendar search and get (the `read` role, the researcher), and unsent drafts: new, reply, forward (the `draft` role, the manager). Nothing sends, moves, deletes or flags. Plan: `docs/plan/task-intake-and-worker.md` §5 (P0 review notes) and §6.
How to run it: `<python> -s -P -m m365 [--config OVERLAY.yaml]` with `PYTHONPATH=<whispr_root>`, `PYTHONNOUSERSITE=1`, optional `WHISPR_OVERLAY`, and two variables the task worker (P3) writes into its own MCP config: `WHISPR_TASK_ID` (the task) and `WHISPR_M365_ROLE` (`read` or `draft`). The plugin does not register it. Without a task id, with an unknown one, a task outside `tasks.m365.bind_statuses` or an email scope that does not parse, it serves but every tool refuses; the task's status and scope are read again on every call, so a task that leaves `bind_statuses` is refused from its next call on. With any other role it serves no tools. `m365.server.tool_names(role)` is the role's tool list, for the worker's allowlist.
Tests: `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_m365.py" -v` (a fake COM layer; no Outlook needed).
Config keys read: `tasks.folder`, `tasks.m365.*`, `tasks.intake.scope_none`, `tasks.intake.scope_field`, `paths.data_dir`, and the database keys `kg.db` reads.

## Email scope grammar (`m365/scope.py`, `tasks.m365.grammar`)

One clause per scope item of type `tasks.m365.scope_type` (`email`): `sender:<address>` or `sender:@<domain>`, `folder:<path under the mailbox root>` (levels split by `/`; subfolders are searched too, up to `mail.max_folders` folders, then the result is partial), `subject:<words>`, `since:<YYYY-MM-DD>`, `until:<YYYY-MM-DD>`. Clauses of one kind are alternatives, different kinds all apply; at most one since and one until. No since: the window opens `default_window_days` before the server starts; no until: it closes today. No folder: `default_folders`, Outlook's default folders by olFolder name or number (one that does not resolve is counted as an error and the result is partial). `none` alone: nothing in scope. Draft recipients must be addresses the brief or a `sender:` address clause names; a domain clause never authorizes one. Prefixes, separator and date format are config. kg/store.py refuses a malformed clause when the intake records it.

## Drafts

At most one draft per source message: a reply or forward is keyed by the original's EntryID in `drafts.source_property`, beside the task marker `drafts.marker_property`; a new mail is refused only when the task already has a new-mail draft with the same subject (case and spacing ignored). Draft results carry ids, counts and reason codes only (no subjects, no addresses). Attachment sources and save destinations are refused when any path component is a junction or link.

## Files

| File | What |
|---|---|
| `server.py` | `MANIFEST` (the one tool list, each tool's roles), `load_binding` (task, role, scope, allowed recipients, read through `kg.db.connect_readonly` at start and again on every call), `M365Server` (lists and answers only its role's tools; refuses while unbound or unhealthy; a timed-out call makes it unhealthy) |
| `outlook.py` | `ComWorker` (the one COM thread, timeouts), `Outlook` (attach once, re-attach after a disconnect, one retry for reads), `Mailbox` (the tools' Outlook work, every query checked against the scope) |
| `scope.py` | the grammar, `EmailScope`, `allowed_recipients` |
| `sanitize.py` | the allowlist HTML sanitizer for draft bodies |
