"""The knowledge graph's MCP server: Claude's read-only window onto it (C.4, D.3 "Graph reader").

Run it as `python -m kg.mcp [--config OVERLAY.yaml]` from the repo root, e.g. registered
with `claude mcp add whispr-kg -- <repo>\\.venv\\Scripts\\python.exe -m kg.mcp`.

- **Protocol.** MCP's stdio transport: JSON-RPC 2.0, one message per line on stdin,
  one reply per line on stdout. It answers `initialize`, `ping`, `tools/list` and
  `tools/call`, and accepts `notifications/initialized` (notifications get no reply).
  Stdlib only: the `mcp` package is not a dependency.
- **Read-only.** The database is opened with kg.db.connect_readonly (a `mode=ro` URI
  plus query_only), and every tool is a read (kg/store.py search/get/source,
  kg/traverse.py neighbors/paths/timeline). Answering never changes the graph.
- **Errors never stop it.** Malformed JSON, a non-object message or an unknown method
  gets a JSON-RPC error; arguments that fail a tool's input schema, an unknown id, a
  time that is not ISO-8601, or a walk past kg.traverse.time_limit_ms get a tool result
  marked isError; an unexpected exception gets an internal error
  and a traceback on stderr. Then the next line is read. Logs go to stderr only,
  since stdout carries the protocol.
- **Progressive disclosure.** search returns short cards, get one record with its
  quotes, source the transcript spans; neighbors, paths and timeline return cards
  bounded by kg.traverse.*. The tool descriptions tell Claude how to chain them.
- **One protocol core.** `RpcServer` (JSON-RPC, tool-schema checks, error results) and
  `run_stdio` are shared with the task server (kg/mcp_tasks.py, the one MCP writer),
  so the two differ only in their tools and connections.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional

from kg import db
from kg.store import Store
from kg.traverse import Traverser
from pipeline.config import load_config
from pipeline.jsonschema_lite import validate

log = logging.getLogger("kg.mcp")

SERVER_NAME = "whispr-kg"
SERVER_VERSION = "1"
# MCP protocol revisions this server speaks, newest first. A client asking for one of
# them gets it back; any other gets the newest (the client then decides).
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
JSONRPC = "2.0"
# JSON-RPC 2.0 error codes.
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = (
    -32700, -32600, -32601, -32602, -32603)

INSTRUCTIONS = (
    "whispr's personal knowledge graph, built from the owner's meeting transcripts: entities "
    "(people, organizations, projects, systems, topics), facts, typed edges between entities, and "
    "tasks, each backed by a verbatim transcript quote. Read-only. To answer: search for each thing the "
    "question names; get the entity to read its facts and edges; for a question that crosses records "
    "(who owns the task on the project X discussed with Y; which system does Y's project use), walk "
    "with neighbors from one end or paths between two ends, and use timeline for how something changed "
    "and for an entity's tasks; then cite every claim with source on the episode_id of the card that "
    "supports it, passing that card's edge or fact id as source's item_id for its exact span. Quote the "
    "transcript; never state a link the cards do not show.")


def _time_props() -> dict:
    when = "ISO-8601 time, any UTC offset (a bare date means its midnight UTC)"
    return {"since": {"type": "string", "minLength": 1, "description": f"Only what is valid at or after this {when}"},
            "until": {"type": "string", "minLength": 1, "description": f"Only what is valid at or before this {when}"}}


def tool_definitions(cfg: dict, relations: list[str], entity_types: list[str]) -> list[dict]:
    """The six tools, their input schemas and the descriptions Claude chains them by."""
    caps = cfg["kg"]["traverse"]
    rel = {"type": "array", "items": {"enum": relations}, "minItems": 1,
           "description": "Walk only edges of these relation types (default: all)"}
    as_of = {"type": "string", "minLength": 1,
             "description": "Show the graph as it stood at this ISO-8601 time: edges valid then, superseded "
                            "ones included (default: only edges active now)"}
    entity = {"type": "string", "minLength": 1, "description": "An entity id from search, get or a card"}
    through_owner = {"type": "boolean",
                     "description": "Also walk through the owner's own person node (default false: the owner is in "
                                    "every meeting, so stepping through them links everything; they can still be "
                                    "an end)"}
    hops = {"type": "integer", "minimum": 1,
            "description": f"How many edges away to look (default {caps['default_hops']}, at most {caps['max_hops']})"}
    return [
        {"name": "search",
         "description": f"Step 1 of every question. Full-text search over entities (by name and alias) and facts; "
                        f"returns at most {cfg['kg']['search_cards']} short cards (kind, id, type, name or text), "
                        "best first. Search each person, project or system the question names, then pass the ids "
                        "to get, neighbors, paths or timeline.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["query"],
                         "properties": {"query": {"type": "string", "minLength": 1,
                                                  "description": "Words to look for"}}}},
        {"name": "get",
         "description": "Step 2: the full record for one id from any card. An entity comes with its aliases, its "
                        "active facts (verbatim quotes, episode ids) and its active edges; a fact, edge or task id "
                        "returns that record (superseded ones too, with superseded_by). Read an entity here before "
                        "walking from it, and to see every edge a neighbors/paths hop folded into `parallel`.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["id"],
                         "properties": {"id": {"type": "string", "minLength": 1,
                                               "description": "An entity, fact, edge or task id"}}}},
        {"name": "source",
         "description": "Last step, for citing: an episode's transcript path, sha256 and meeting time, and the "
                        "quoted spans (hh:mm:ss start + verbatim quote) of its facts, edges and tasks, or of the one "
                        "item_id. Cite every claim in an answer with the episode_id on the card that supports it, "
                        "and pass that card's edge or fact `id` as item_id to get the exact transcript span.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["episode_id"],
                         "properties": {"episode_id": {"type": "string", "minLength": 1,
                                                       "description": "The episode_id from a card"},
                                        "item_id": {"type": "string", "minLength": 1,
                                                    "description": "Only this fact, edge or task's span: the "
                                                                   "`id` of an edge or fact card"}}}},
        {"name": "neighbors",
         "description": "Step 3 for multi-hop questions: walk the graph out from one entity. Returns every entity "
                        f"within `hops` edges (edges are walked either way; at most {caps['max_results']} cards, "
                        "nearest first, then most recently linked), each with the chain of edges that reached it: "
                        "relation, src -> dst, "
                        "verbatim quote, provenance, episode_id, valid_from/valid_to, state. Chain hops to answer "
                        "e.g. person -> project they work on -> system it uses -> who is responsible for that "
                        "system (hops=3). Narrow with relations; as_of shows the graph at a past time; since/until "
                        "keep edges valid in a window; types keeps only entities of those types; when truncated, "
                        "ask again with offset for the next page. Then timeline a reached project or person for "
                        "its tasks, and cite each hop with source(episode_id, item_id = the hop's edge id).",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["entity_id"],
                         "properties": {"entity_id": entity, "hops": hops, "relations": rel, "as_of": as_of,
                                        **_time_props(),
                                        "types": {"type": "array", "items": {"enum": entity_types}, "minItems": 1,
                                                  "description": "Return only entities of these types (the walk "
                                                                 "still passes through any type)"},
                                        "offset": {"type": "integer", "minimum": 0,
                                                   "description": "Skip this many results, for the next page "
                                                                  "(default 0)"},
                                        "through_owner": through_owner}}},
        {"name": "paths",
         "description": "Step 3 when the question names both ends ('how is A connected to B', 'is A on B's "
                        f"project'): up to {caps['max_paths']} shortest routes from src_id to dst_id, each at "
                        f"most max_hops edges (default and cap {caps['max_hops']}), no entity repeated. Each route "
                        "lists its edges in order with quotes and episode ids. An empty list means no connection "
                        "within the hops; try neighbors from each end. Then cite each edge with "
                        "source(episode_id, item_id = the edge's id).",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["src_id", "dst_id"],
                         "properties": {"src_id": entity, "dst_id": entity,
                                        "max_hops": {**hops, "description": f"Longest route, in edges (default "
                                                                            f"and cap {caps['max_hops']})"},
                                        "relations": rel, "as_of": as_of, "through_owner": through_owner}}},
        {"name": "timeline",
         "description": "How one entity changed, and its tasks: its facts, edges and tasks in time order (latest "
                        f"{caps['max_results']} if there are more), superseded facts and edges included and marked "
                        "state=superseded with supersede_reason and superseded_by, so an old value, why it changed "
                        "and its replacement all show. Tasks carry owner, due and status. Use for 'what changed', "
                        "'what did we decide before', 'who owns the task on X', after search or neighbors found "
                        "the entity.",
         "inputSchema": {"type": "object", "additionalProperties": False, "required": ["entity_id"],
                         "properties": {"entity_id": entity, **_time_props()}}},
    ]


class ToolError(Exception):
    """A tool call that can't be answered as asked; returned as a result marked isError,
    with any `fields` (e.g. the allowed next states) beside the message."""

    def __init__(self, message: str, **fields: Any):
        super().__init__(message)
        self.fields = fields


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class RpcServer:
    """MCP over JSON-RPC 2.0 for a fixed set of tools: `tools` maps a name to its
    definition (name, description, inputSchema), `calls` the name to a function of the
    validated arguments. A subclass sets both, and its name and instructions."""
    name = SERVER_NAME
    instructions = INSTRUCTIONS

    def __init__(self, tools: list[dict], calls: dict[str, Callable[[dict], Any]]):
        if sorted(t["name"] for t in tools) != sorted(calls):
            raise ValueError("every tool needs exactly one call")
        self.tools = {t["name"]: t for t in tools}
        self.calls = calls

    @staticmethod
    def _found(result: Optional[dict], wanted: str) -> dict:
        if result is None:
            raise ToolError(f"no record with id {wanted!r}; ids come from search, get or a card")
        return result

    # ---- JSON-RPC ---------------------------------------------------------------------

    def handle_line(self, line: bytes) -> Optional[dict]:
        """The reply to one stdin line, or None (a notification, or a blank line)."""
        try:
            text = line.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            return _error(None, PARSE_ERROR, f"not UTF-8: {exc}")
        if not text:
            return None
        try:
            message = json.loads(text)
        except ValueError as exc:
            return _error(None, PARSE_ERROR, f"not JSON: {exc}")
        return self.handle(message)

    def handle(self, message: Any) -> Optional[dict]:
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "a message must be one JSON object (batches are not supported)")
        is_request = "id" in message
        msg_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != JSONRPC or not isinstance(method, str):
            return _error(msg_id, INVALID_REQUEST, "expected jsonrpc '2.0' and a method") if is_request else None
        if not is_request:
            log.info("notification %s", method)
            return None
        params = message.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params must be an object")
            return {"jsonrpc": JSONRPC, "id": msg_id, "result": self.dispatch(method, params)}
        except RpcError as exc:
            return _error(msg_id, exc.code, str(exc))
        except Exception as exc:                       # keep serving; the traceback goes to stderr
            log.exception("internal error on %s", method)
            return _error(msg_id, INTERNAL_ERROR, f"internal error: {type(exc).__name__}: {exc}")

    def dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": SERVER_VERSION},
                    "instructions": self.instructions}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": list(self.tools.values())}
        if method == "tools/call":
            return self.call_tool(params.get("name"), params.get("arguments") or {})
        raise RpcError(METHOD_NOT_FOUND, f"unknown method {method!r}")

    def call_tool(self, name: Any, arguments: Any) -> dict:
        if name not in self.tools:
            raise RpcError(INVALID_PARAMS, f"unknown tool {name!r}; tools: {sorted(self.tools)}")
        errors = validate(arguments, self.tools[name]["inputSchema"])
        if errors:
            return _tool_result({"error": "arguments do not match the tool's input schema", "details": errors},
                                is_error=True)
        try:
            return _tool_result(self.calls[name](arguments))
        except ToolError as exc:
            return _tool_result({"error": str(exc), **exc.fields}, is_error=True)
        except ValueError as exc:                      # StoreError, or a time that is not ISO-8601
            return _tool_result({"error": str(exc)}, is_error=True)


class Server(RpcServer):
    """The graph reader (D.3): search, get, source, neighbors, paths, timeline, over a
    connection the caller opened (kg.db.connect_readonly in `main`)."""

    def __init__(self, conn: sqlite3.Connection, cfg: dict):
        self.cfg = cfg
        self.store = Store(conn, cfg)
        self.traverser = Traverser(self.store)
        caps = cfg["kg"]["traverse"]
        t = self.traverser
        calls: dict[str, Callable[[dict], Any]] = {
            "search": lambda a: self.store.search(a["query"]),
            "get": lambda a: self._found(self.store.get(a["id"]), a["id"]),
            "source": lambda a: self._found(self.store.source(a["episode_id"], a.get("item_id")), a["episode_id"]),
            "neighbors": lambda a: t.neighbors(a["entity_id"], hops=a.get("hops", caps["default_hops"]),
                                               relations=a.get("relations"), since=a.get("since"),
                                               until=a.get("until"), as_of=a.get("as_of"), types=a.get("types"),
                                               offset=a.get("offset", 0),
                                               through_owner=a.get("through_owner", False)),
            "paths": lambda a: t.paths(a["src_id"], a["dst_id"], max_hops=a.get("max_hops", caps["max_hops"]),
                                       relations=a.get("relations"), as_of=a.get("as_of"),
                                       through_owner=a.get("through_owner", False)),
            "timeline": lambda a: t.timeline(a["entity_id"], since=a.get("since"), until=a.get("until")),
        }
        super().__init__(tool_definitions(cfg, sorted(self.store.relations), sorted(self.store.entity_types)), calls)


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": JSONRPC, "id": msg_id, "error": {"code": code, "message": message}}


def _tool_result(value: Any, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": is_error}


def serve(server: RpcServer, stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Answer stdin line by line until it closes."""
    for line in stdin:
        reply = server.handle_line(line)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


# start(cfg) -> (the server, the connections to close when stdin ends, a line for the log)
Starter = Callable[[dict], tuple[RpcServer, list[sqlite3.Connection], str]]


def run_stdio(argv: Optional[list[str]], *, prog: str, description: str, start: Starter) -> int:
    """Parse --config, start the server, serve stdin until it closes. Exit 1 when it
    cannot start (no database, a schema this code does not know), logged to stderr."""
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--config", type=Path, default=None,
                        help="the per-user overlay (default %%APPDATA%%\\whispr\\config.yaml)")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        cfg = load_config(overlay_path=args.config)
        server, conns, banner = start(cfg)
    except Exception as exc:
        log.error("cannot start: %s", exc)
        return 1
    try:
        log.info(banner)
        serve(server, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        for conn in conns:
            conn.close()
    return 0


def _start_reader(cfg: dict) -> tuple[RpcServer, list[sqlite3.Connection], str]:
    conn = db.connect_readonly(cfg)
    try:
        return Server(conn, cfg), [conn], f"serving {db.database_path(cfg)} read-only"
    except BaseException:
        conn.close()
        raise


def main(argv: Optional[list[str]] = None) -> int:
    return run_stdio(argv, prog="python -m kg.mcp", description=__doc__.splitlines()[0], start=_start_reader)
