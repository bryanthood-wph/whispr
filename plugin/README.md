# plugin/ — the whispr Claude Code plugin

What it does: whispr in your own Claude Code session. It has two MCP servers, `whispr-kg` (the graph, read-only) and `whispr-tasks` (task review). It has three slash commands: `/create-tasks` (review captured tasks), `/whispr-graph` (the graph pane) and `/whispr-setup` (one-time setup). And it shows open alerts at session start (`hooks/`, see hooks/README.md).
How to run it: load it in place, from this folder: `claude --plugin-dir <whispr folder>\plugin`. Set its options once with `/config` (or `claude plugin configure whispr`): `python` is whispr's interpreter (`<repo>\.venv\Scripts\python.exe` for a checkout, `<install>\python\python.exe` for the installed distribution), and `whispr_root` is the folder holding `pipeline\`, `kg\` and `config\`. Both are required and have no default, so nothing ships with one machine's path. `config` is an overlay other than `%APPDATA%\whispr\config.yaml`; leave it empty for the default. Then run `/whispr-setup`.
Every part runs that pinned interpreter with `-s` (no per-user site-packages). In `.mcp.json` each server is `"${user_config.python}" -s -P -m kg.mcp|kg.mcp_tasks`, with `PYTHONPATH` set to `whispr_root`, `PYTHONNOUSERSITE=1`, and the overlay in `WHISPR_OVERLAY` (pipeline/config.py), so `-m kg…` resolves from the install whatever the session's working directory holds. The skills' `allowed-tools` name MCP tools in both forms: `mcp__whispr-tasks__…` registered by hand (`claude mcp add`), `mcp__plugin_whispr_whispr-tasks__…` from this plugin.
Tests: the servers and the review API, `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_kg_tasks.py" -v`; setup, `-p "test_pipeline_setup.py"`; the hook, `-p "test_pipeline_alerts.py"`; the manifest and the pane, `claude plugin validate plugin` and `claude plugin test plugin`.
Config keys read (through the servers and setup): `kg.tasks.*`, `tasks.intake.*`, `kg.search_cards`, `kg.traverse.*`, `kg.view.*`, `setup.*`; see kg/README.md and config/defaults.yaml.

## Layout

| Path | What |
|---|---|
| `.claude-plugin/plugin.json` | the manifest: name `whispr`, the options above |
| `.mcp.json` | the two MCP servers |
| `hooks/hooks.json` | the SessionStart hook (`session_start.py`) and the pane module (`graph.tsx`) |
| `skills/create-tasks`, `skills/whispr-setup` | the two skills |
| `bin/pipeline.py` | `python -m pipeline` from the folder it is given, from any working directory (what `/whispr-setup` runs) |
| `lib/whispr_paths.py` | the install lookup and path checks shared by the hook and `bin/pipeline.py` |
| `types/`, `tests/` | the pane's types and its `claude plugin test` tests |

## Roles (least privilege)

Every role is your own session model (no model of its own, no effort setting) unless it says otherwise. Each gets only the tools listed.

| Role | Tools | Why |
|---|---|---|
| `/create-tasks` review (`skills/create-tasks`) | `whispr-kg` `search`, `get`, `source`; `whispr-tasks` `task_list`, `task_get`, `task_update_status`, `project_scope_get`, `project_scope_set`; `AskUserQuestion`; `Read` for a `good\<work-type>\rubric.md` only; nothing else | confirm, clarify, attach inputs, drop, mark ready through the intake (scope, brief fields and budget; the server refuses ready while a required field is open). Every change is a database row (lesson L19), and the task server's one writable connection can write only the task review tables (kg/README.md, "Task server") |
| `/whispr-setup` (`skills/whispr-setup`) | pre-approved: `AskUserQuestion` only. Bash: each command asks you | AskUserQuestion: every question goes through it (your rule). Bash runs `bin/pipeline.py` for each step: `setup plan/write/init/test-alert`, `schedule --register`, `alerts --ack`, `doctor`. It is not pre-approved, because the interpreter path differs per install and a rule can't name it, so you see and approve every command. That also gives `schedule --register --yes`, the one step that changes the machine outside whispr's folders, its own approval after the dry listing |
| `whispr-kg` server | read-only database connection | graph reads for Claude and the skills |
| `whispr-tasks` server | a connection limited to the task review tables | `/create-tasks` decisions |
| SessionStart hook | none (a script, no model): reads the overlay and the database read-only | the alert surface (D.6) |
| `/whispr-graph` pane | none (a script, no model): runs `python -m kg.view` read-only; "Ask Claude" only fills your prompt box | the graph view; you choose whether to send the question |

`allowed-tools` pre-approves exactly what is listed; any other tool stops and asks, and each skill tells Claude not to use one.

A different `create-tasks` skill (the cohoodOBS inbox flow) lives in `~/.claude/skills`. It is a separate repo and is retired with cohoodOBS (D.8). From this plugin, this one is `/whispr:create-tasks`.
