"""The task server: the one MCP server that may change the database, and only a task's
review state and a project's default scope (D.3 "/create-tasks review": MCP read +
task_update_status only; the intake, docs/plan/task-intake-and-worker.md §3, §4).

Run it as `python -m kg.mcp_tasks [--config OVERLAY.yaml]` from the repo root, e.g.
registered with `claude mcp add whispr-tasks -- <repo>\\.venv\\Scripts\\python.exe -m kg.mcp_tasks`.

- **Tools.** `task_list`, `task_get` and `project_scope_get` read; `task_update_status`
  and `project_scope_set` write. Nothing else: the graph itself is read through the
  reader server (`python -m kg.mcp`), which stays read-only.
- **Two connections.** The reads go through kg.db.connect_readonly. The one writable
  connection (kg.db.connect_limited) is held by a Store used only by the two writing
  tools, and SQLite's authorizer lets it do exactly the row operations in TASK_WRITE_GRANTS
  and nothing else: no graph row, no schema change, no task-to-project link changed or
  removed (only added), no history row rewritten.
- **Every task change is Store.update_task**: an allowed transition only (an invalid one
  is an isError result naming the allowed next states), recorded with this server as
  the actor, the reason as the why and the time; clarifications, inputs and brief answers
  are rows. Lifecycle state lives only in the database, never in a note or file (L19).
- **The ready gate** is the store's: a move to kg.tasks.tools_status (ready) while a
  required brief field is open is an isError result whose `open_fields` names them, so
  no skill path can mark a task ready with its brief incomplete.
- Protocol, schema checks and error handling are kg/mcp_server.py's (RpcServer).
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any, Callable, Optional

from kg import db
from kg.mcp_server import RpcServer, ToolError, run_stdio
from kg.store import BriefOpenError, Store, TransitionError

SERVER_NAME = "whispr-tasks"
# Recorded as the actor of every change made through this server.
ACTOR = SERVER_NAME
# What the writable connection may do, table -> row operations (kg.db.WRITE_OPS), and
# nothing more (kg/migrations/0001, 0006, 0007). Store.update_task updates the task and
# appends its event, notes and project link (merges and repairs of links stay the
# pipeline's); Store.set_entity_scope replaces a project's scope by deleting and
# inserting rows, never updating one.
TASK_WRITE_GRANTS = {"task": ("update",), "task_event": ("insert",), "task_note": ("insert",),
                     "task_entity": ("insert",), "entity_scope": ("insert", "delete")}

INSTRUCTIONS = (
    "whispr's task review: the owner's tasks captured from meeting transcripts, each with its action, owner, "
    "due date, verbatim quote and source episode. Use it to run a /create-tasks review: task_list shows the "
    "tasks waiting in a status, \"confirm?\" ones (ownership the transcript could not settle, or a quote removed "
    "as echo) first; task_get shows one task with its history, its brief and the brief fields still open; "
    "task_update_status records what the user decided (confirm, clarify, attach inputs, answer brief fields and "
    "the scope, link the task to its project, drop, mark ready with the tools the work may use). Marking ready "
    "is refused while a required "
    "brief field is open. project_scope_get and project_scope_set read and replace a project's default scope, "
    "which the intake proposes from. Only change what the user said; never edit notes or files to record a "
    "task's state.")


def _scope_schema(intake: dict, *, min_items: int) -> dict:
    """A scope argument: [{type, value}], type one of tasks.intake.scope_types."""
    kinds = "; ".join(f"{name}: {what}" for name, what in intake["scope_types"].items())
    return {"type": "array", "minItems": min_items,
            "description": f"Scope items, each a source type and its value ({kinds}). Value "
                           f"{intake['scope_none']!r} means the type has nothing in scope, and stands alone in it",
            "items": {"type": "object", "additionalProperties": False, "required": ["type", "value"],
                      "properties": {"type": {"enum": list(intake["scope_types"])},
                                     "value": {"type": "string", "minLength": 1}}}}


def _budget_schema(intake: dict, question: str) -> dict:
    """The budget field's answer: {amount, reason} under tasks.intake.budget's key names."""
    budget = intake["budget"]
    return {"type": "object", "additionalProperties": False,
            "required": [budget["amount_key"], budget["reason_key"]],
            "description": f"{question} An object: the amount, and the reason for that figure",
            "properties": {budget["amount_key"]: {"type": "number", "minimum": budget["amount_min_usd"],
                                                  "description": f"USD, at least {budget['amount_min_usd']}"},
                           budget["reason_key"]: {"type": "string", "minLength": 1,
                                                  "description": "Why that figure"}}}


def tool_definitions(cfg: dict, statuses: list[str], first_stage: str, store: Store) -> list[dict]:
    """The five tools and their input schemas (`store` gives the intake's field names)."""
    tasks_cfg, intake = cfg["kg"]["tasks"], cfg["tasks"]["intake"]
    scope_field, budget_field, required = store.scope_field, store.budget_field, store.required_fields
    task_id = {"type": "string", "minLength": 1, "description": "A task id from task_list or task_get"}
    entity_id = {"type": "string", "minLength": 1,
                 "description": f"A {intake['scope_entity_type']} entity id (whispr-kg search or get, or the task's "
                                "entity_ids)"}
    brief_fields = {name: (_budget_schema(intake, question) if name == budget_field
                           else {"type": "string", "minLength": 1, "description": question})
                    for name, question in intake["fields"].items() if name != scope_field}
    return [
        {"name": "task_list",
         "description": f"Step 1 of a review: tasks in one status (default {first_stage!r}), at most "
                        f"{tasks_cfg['list_limit']} per call. Tasks marked confirm=true come first (their ownership "
                        "needs the user's word), then the newest meeting first. Each card has the task (action, "
                        "owner, owner_basis, due, due_basis, context, quote, source episode and start time, "
                        "status, tools_allowed), the meeting time and the transcript path. Only the owner's own "
                        "tasks unless mine=false. When truncated, call again with offset.",
         "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
             "status": {"enum": statuses, "description": f"Which status to list (default {first_stage!r})"},
             "mine": {"type": "boolean", "description": "Only the owner's own tasks (default true)"},
             "confirm_only": {"type": "boolean", "description": "Only the \"confirm?\" tasks (default false)"},
             "offset": {"type": "integer", "minimum": 0, "description": "Skip this many, for the next page"}}}},
        {"name": "task_get",
         "description": "One task with its history: the task, whether it needs confirm, the meeting, every "
                        "status change (from, to, actor, reason, details such as the tools granted, time), the "
                        "clarifications and inputs attached so far, the brief (field -> the newest answer with its "
                        f"source, actor and time; {scope_field!r} holds the scope list, {budget_field!r} the "
                        f"amount and reason), every earlier answer in brief_history, open_fields: the required "
                        f"fields ({', '.join(required)}) still unanswered, which must be empty before the task "
                        f"can be marked {tasks_cfg['tools_status']!r}, and projects: the "
                        f"{intake['scope_entity_type']} entities it is linked to. Use it before changing a task "
                        "the user asks about, and to show what a change did.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["id"],
                         "properties": {"id": task_id}}},
        {"name": "task_update_status",
         "description": "Record the user's decision on one task, all of it or none. status moves the task along an "
                        f"allowed transition (e.g. {first_stage} -> confirmed or dropped; confirmed -> ready); an "
                        "invalid move is refused with the allowed next states. reason (why) is required with a "
                        "status. clarification records the user's answer (a corrected owner, due date or scope); "
                        "inputs attaches what the work needs (file paths, links, values). brief answers intake "
                        f"fields and scope sets the {scope_field!r} field, both with brief_source: "
                        f"{' or '.join(intake['answer_sources'])} (the user said it, or confirmed what you "
                        "proposed); a newer answer replaces an older one, which stays in the history. project "
                        f"links the task to its {intake['scope_entity_type']} entity (a link is only ever added). "
                        "Marking "
                        f"{tasks_cfg['tools_status']!r} is refused while a required field is open (the error's "
                        "open_fields names them); answers given in the same call count. tools_allowed is the "
                        f"worker's tool allowlist, given only with status {tasks_cfg['tools_status']!r}; leave it "
                        "out for a read-only worker. Call it only for what the user decided.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {
             "id": task_id,
             "status": {"enum": tasks_cfg["review_statuses"],
                        "description": "The new status (omit to only clarify, attach or answer)"},
             "reason": {"type": "string", "minLength": 1, "description": "Why, in the user's words"},
             "clarification": {"type": "string", "minLength": 1, "description": "The user's clarification"},
             "inputs": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1,
                        "description": "Inputs the work needs: paths, links or values"},
             "brief": {"type": "object", "additionalProperties": False, "properties": brief_fields,
                       "description": "Brief answers: field -> the answer, in the user's words"},
             "scope": _scope_schema(intake, min_items=1),
             "brief_source": {"enum": intake["answer_sources"],
                              "description": "Who gave this call's brief and scope answers: required with them"},
             "project": {**entity_id, "description": f"Link the task to this {intake['scope_entity_type']} "
                                                     "entity id (whispr-kg search or get)"},
             "tools_allowed": {"type": "array", "items": {"type": "string", "minLength": 1},
                               "description": f"Tools the worker may use, with status "
                                              f"{tasks_cfg['tools_status']!r} only"}}}},
        {"name": "project_scope_get",
         "description": f"A {intake['scope_entity_type']}'s default scope: the saved {{type, value}} items the "
                        "intake proposes a task's scope from, and open_types, the source types with nothing saved "
                        f"yet (a type saved as {intake['scope_none']!r} has nothing in scope: don't ask it again). "
                        f"Refused for an entity that is not a {intake['scope_entity_type']}.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["entity_id"],
                         "properties": {"entity_id": entity_id}}},
        {"name": "project_scope_set",
         "description": f"Replace a {intake['scope_entity_type']}'s default scope with these items (an empty list "
                        "clears it). Only after the user agreed to save the intake's scope back to the project. "
                        f"Refused for an entity that is not a {intake['scope_entity_type']}.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["entity_id", "scope"],
                         "properties": {"entity_id": entity_id, "scope": _scope_schema(intake, min_items=0)}}},
    ]


class TaskServer(RpcServer):
    name = SERVER_NAME
    instructions = INSTRUCTIONS

    def __init__(self, read_conn: sqlite3.Connection, write_conn: sqlite3.Connection, cfg: dict):
        self.cfg = cfg
        self.reader = Store(read_conn, cfg)
        self._writer = Store(write_conn, cfg)          # task_update_status and project_scope_set only
        statuses = list(self.reader.task_schema["properties"]["status"]["enum"])
        self.first_stage = self.reader.funnel_stages()[0]
        calls: dict[str, Callable[[dict], Any]] = {
            "task_list": lambda a: self.reader.review_tasks(
                status=a.get("status", self.first_stage), mine=a.get("mine", True),
                confirm_only=a.get("confirm_only", False), limit=cfg["kg"]["tasks"]["list_limit"],
                offset=a.get("offset", 0)),
            "task_get": lambda a: self._found(self.reader.task_record(a["id"]), a["id"]),
            "task_update_status": self._update,
            "project_scope_get": lambda a: self.reader.entity_scope(a["entity_id"]),
            "project_scope_set": lambda a: self._writer.set_entity_scope(a["entity_id"], a["scope"]),
        }
        super().__init__(tool_definitions(cfg, statuses, self.first_stage, self.reader), calls)

    def _update(self, a: dict) -> dict:
        if "status" in a and "reason" not in a:
            raise ToolError("a status change needs a reason: why the user decided it")
        review = self.cfg["kg"]["tasks"]["review_statuses"]
        if "status" in a and a["status"] not in review:
            raise ToolError(f"the review sets only {review} (kg.tasks.review_statuses); "
                            f"{a['status']!r} is the /execute-tasks worker's to record")
        try:
            return self._writer.update_task(a["id"], actor=ACTOR, status=a.get("status"), reason=a.get("reason"),
                                            clarification=a.get("clarification"), inputs=a.get("inputs", ()),
                                            tools_allowed=a.get("tools_allowed"), brief=a.get("brief"),
                                            scope=a.get("scope"), brief_source=a.get("brief_source"),
                                            project=a.get("project"))
        except TransitionError as exc:
            raise ToolError(str(exc), status=exc.current, allowed=exc.allowed) from exc
        except BriefOpenError as exc:
            raise ToolError(str(exc), open_fields=exc.open) from exc


def _start(cfg: dict) -> tuple[RpcServer, list[sqlite3.Connection], str]:
    conns = []
    try:
        conns.append(db.connect_readonly(cfg))
        conns.append(db.connect_limited(cfg, TASK_WRITE_GRANTS))
        server = TaskServer(conns[0], conns[1], cfg)
    except BaseException:
        for conn in conns:
            conn.close()
        raise
    grants = "; ".join(f"{table} {'/'.join(ops)}" for table, ops in TASK_WRITE_GRANTS.items())
    return server, conns, f"serving {db.database_path(cfg)}: reads, and only these writes: {grants}"


def main(argv: Optional[list[str]] = None) -> int:
    return run_stdio(argv, prog="python -m kg.mcp_tasks", description=__doc__.splitlines()[0], start=_start)


if __name__ == "__main__":
    sys.exit(main())
