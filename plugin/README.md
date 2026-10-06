# plugin/ — the whispr Claude Code plugin

What it does: the skills you run in your own Claude Code session over your whispr graph: `/create-tasks` (review captured tasks) now; `/execute-tasks`, `/ask`, the mods (graph view, alerts), the session-start alert hook and `/whispr-setup` come later (docs/plan/README.md §6).
How to run it: until the plugin is packaged (stage 8), register the two MCP servers from the repo root and use the skill folder directly: `claude mcp add whispr-kg -- <repo>\.venv\Scripts\python.exe -m kg.mcp` and `claude mcp add whispr-tasks -- <repo>\.venv\Scripts\python.exe -m kg.mcp_tasks`.
Packaged, the plugin's `.mcp.json` declares the same two servers, named `whispr-kg` and `whispr-tasks`, each `{"command": "<repo>\\.venv\\Scripts\\python.exe", "args": ["-m", "kg.mcp"|"kg.mcp_tasks"], "cwd": "<repo>"}`, and the plugin is named `whispr`: the skills' `allowed-tools` name tools in both forms (`mcp__whispr-tasks__…` registered by hand, `mcp__plugin_whispr_whispr-tasks__…` from the plugin).
Tests: the servers and the review API behind the skills, `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_kg_tasks.py" -v`.
Config keys read (through the servers): `kg.tasks.*`, `kg.search_cards`, `kg.traverse.*`; see kg/README.md.

## Skills

| Skill | Role (D.3) | Tools | Why |
|---|---|---|---|
| `skills/create-tasks` | `/create-tasks` review: your session model | `whispr-kg` `search`, `get`, `source`; `whispr-tasks` `task_list`, `task_get`, `task_update_status`; nothing else | confirm, clarify, attach inputs, drop, mark ready; every change is a database row (lesson L19) |

`allowed-tools` pre-approves exactly these; any other tool would stop and ask, and the skill tells Claude not to use one. The task server's one writable connection can write only the task review tables (kg/README.md, "Task server").

A different `create-tasks` skill (the cohoodOBS inbox flow) lives in `~/.claude/skills`; it is a separate repo and is retired with cohoodOBS (D.8). Installed as a plugin, this one is `/whispr:create-tasks`.
