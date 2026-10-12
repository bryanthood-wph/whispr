"""whispr-m365: Classic Outlook over COM for one task's worker (docs/plan/task-intake-and-worker.md §5).

`python -m m365` runs the MCP server (m365/server.py). m365.scope (the email scope
grammar) is stdlib only, so kg/store.py can check a scope value when the intake records it.
"""
