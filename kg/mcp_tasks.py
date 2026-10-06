"""The task server: the one MCP server that may change the database, and only a task's
review state (D.3 "/create-tasks review": MCP read + task_update_status only).

Run it as `python -m kg.mcp_tasks [--config OVERLAY.yaml]` from the repo root, e.g.
registered with `claude mcp add whispr-tasks -- <repo>\\.venv\\Scripts\\python.exe -m kg.mcp_tasks`.

- **Tools.** `task_list` and `task_get` read; `task_update_status` writes. Nothing else:
  the graph itself is read through the reader server (`python -m kg.mcp`), which
  stays read-only.
- **Two connections.** The reads go through kg.db.connect_readonly. The one writable
  connection (kg.db.connect_limited) is held by a Store used only by
  task_update_status, and SQLite's authorizer lets it write the task review tables
  (TASK_REVIEW_TABLES) and nothing else: no graph row, no schema change.
- **Every change is Store.update_task**: an allowed transition only (an invalid one
  is an isError result naming the allowed next states), recorded with this server as
  the actor, the reason as the why and the time; clarifications and inputs are rows.
  Lifecycle state lives only in the database, never in a note or file (lesson L19).
- Protocol, schema checks and error handling are kg/mcp_server.py's (RpcServer).
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any, Callable, Optional

from kg import db
from kg.mcp_server import RpcServer, ToolError, run_stdio
from kg.store import Store, TransitionError

SERVER_NAME = "whispr-tasks"
# Recorded as the actor of every change made through this server.
ACTOR = SERVER_NAME
# The tables Store.update_task writes (kg/migrations/0001, 0006); the writable
# connection may write these and no other.
TASK_REVIEW_TABLES = ("task", "task_event", "task_note")

INSTRUCTIONS = (
    "whispr's task review: the owner's tasks captured from meeting transcripts, each with its action, owner, "
    "due date, verbatim quote and source episode. Use it to run a /create-tasks review: task_list shows the "
    "tasks waiting in a status, \"confirm?\" ones (ownership the transcript could not settle, or a quote removed "
    "as echo) first; task_get shows one task with its history; task_update_status records what the user decided "
    "(confirm, clarify, attach inputs, drop, mark ready with the tools the work may use). Only change what the user "
    "said; never edit notes or files to record a task's state.")


def tool_definitions(cfg: dict, statuses: list[str], first_stage: str) -> list[dict]:
    """The three tools and their input schemas."""
    tasks_cfg = cfg["kg"]["tasks"]
    task_id = {"type": "string", "minLength": 1, "description": "A task id from task_list or task_get"}
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
                        "status change (from, to, actor, reason, details such as the tools granted, time), and the "
                        "clarifications and inputs attached so far. Use it before changing a task the user asks "
                        "about, and to show what a change did.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["id"],
                         "properties": {"id": task_id}}},
        {"name": "task_update_status",
         "description": "Record the user's decision on one task, all of it or none. status moves the task along an "
                        f"allowed transition (e.g. {first_stage} -> confirmed or dropped; confirmed -> ready); an "
                        "invalid move is refused with the allowed next states. reason (why) is required with a "
                        "status. clarification records the user's answer (a corrected owner, due date or scope); "
                        "inputs attaches what the work needs (file paths, links, values). tools_allowed is the "
                        f"worker's tool allowlist, given only with status {tasks_cfg['tools_status']!r}; leave it "
                        "out for a read-only worker. Call it only for what the user decided.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {
             "id": task_id,
             "status": {"enum": tasks_cfg["review_statuses"],
                        "description": "The new status (omit to only clarify or attach)"},
             "reason": {"type": "string", "minLength": 1, "description": "Why, in the user's words"},
             "clarification": {"type": "string", "minLength": 1, "description": "The user's clarification"},
             "inputs": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1,
                        "description": "Inputs the work needs: paths, links or values"},
             "tools_allowed": {"type": "array", "items": {"type": "string", "minLength": 1},
                               "description": f"Tools the worker may use, with status "
                                              f"{tasks_cfg['tools_status']!r} only"}}}},
    ]


class TaskServer(RpcServer):
    name = SERVER_NAME
    instructions = INSTRUCTIONS

    def __init__(self, read_conn: sqlite3.Connection, write_conn: sqlite3.Connection, cfg: dict):
        self.cfg = cfg
        self.reader = Store(read_conn, cfg)
        self._writer = Store(write_conn, cfg)          # task_update_status only
        statuses = list(self.reader.task_schema["properties"]["status"]["enum"])
        self.first_stage = self.reader.funnel_stages()[0]
        calls: dict[str, Callable[[dict], Any]] = {
            "task_list": lambda a: self.reader.review_tasks(
                status=a.get("status", self.first_stage), mine=a.get("mine", True),
                confirm_only=a.get("confirm_only", False), limit=cfg["kg"]["tasks"]["list_limit"],
                offset=a.get("offset", 0)),
            "task_get": lambda a: self._found(self.reader.task_record(a["id"]), a["id"]),
            "task_update_status": self._update,
        }
        super().__init__(tool_definitions(cfg, statuses, self.first_stage), calls)

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
                                            tools_allowed=a.get("tools_allowed"))
        except TransitionError as exc:
            raise ToolError(str(exc), status=exc.current, allowed=exc.allowed) from exc


def _start(cfg: dict) -> tuple[RpcServer, list[sqlite3.Connection], str]:
    conns = []
    try:
        conns.append(db.connect_readonly(cfg))
        conns.append(db.connect_limited(cfg, TASK_REVIEW_TABLES))
        server = TaskServer(conns[0], conns[1], cfg)
    except BaseException:
        for conn in conns:
            conn.close()
        raise
    return server, conns, f"serving {db.database_path(cfg)}: reads, and writes to {', '.join(TASK_REVIEW_TABLES)} only"


def main(argv: Optional[list[str]] = None) -> int:
    return run_stdio(argv, prog="python -m kg.mcp_tasks", description=__doc__.splitlines()[0], start=_start)


if __name__ == "__main__":
    sys.exit(main())
