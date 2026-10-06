"""Multi-hop reads over the graph: neighbors, paths and timeline (C.4 "graph traversal").

Claude answers a question that crosses several records by chaining the MCP tools
(kg/mcp_server.py): `search` finds an entity, `get` shows it, `neighbors` or `paths`
walks its edges, `timeline` shows how it changed, and `source` cites the transcript.
This module is the walking part, in the data-access layer beside kg/store.py, and
like every MCP read it never writes.

- **What counts.** The same filters as Store's reads: an edge counts only if its
  episode is live (not tombstoned) and both endpoints are live (not merged away).
  With no time filter, only active (unsuperseded) edges count. With `as_of`, the
  edges valid at that moment count, superseded ones included (valid_from <= as_of <
  valid_to), so the answer shows the graph as it stood then; with `since` / `until`,
  the edges whose validity overlaps that window. Times are stored, and asked-for
  times converted, as UTC ISO-8601 text (kg.db.utc_time), so text order is time order
  whatever offset a time came with; a bare date means its midnight UTC. A merged-away start id is followed to
  its survivor, and the answer says so.
- **Undirected walks.** An edge is walked either way; its card keeps the stored
  direction (src -> dst), so "who works on X" and "what does Y work on" read the same
  edge. Parallel edges between one pair of entities fold into one hop: the card shows
  the earliest and `parallel` counts the rest (`get` lists them all).
- **No hub swamping.** The owner (config owner.email) is in every meeting, so by
  default a walk never steps through their person node (it can start or end there;
  `through_owner` allows it). Within a depth, neighbors lists the most recently linked
  entities first; `types` narrows the answer and `offset` pages it.
- **Bounded.** Hops are cut to kg.traverse.max_hops (the answer reports the hops
  used), answers to kg.traverse.max_results cards and kg.traverse.max_paths paths, and
  quotes to kg.traverse.quote_chars. A cut answer says `truncated`. Paths never repeat
  an entity, so cycles end every walk. A walk running past kg.traverse.time_limit_ms
  is stopped with a StoreError (an isError result over MCP).
- **Shared SQL.** Recursive CTEs with no SQLite-only function, so a Postgres port moves
  the same text. Each step joins `edge` on the node through the edge_src / edge_dst
  indexes, so a walk reads only the edges near it, never the whole table. Entity ids
  are hex (kg.db.stable_id), so they hold no LIKE wildcard.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional, Sequence

from kg.db import fetch_all, utc_time
from kg.store import EDGE_SELECT, FACT_SELECT, PERSON, Store, StoreError, keyed_id, name_key

# Separates entity ids in a walk's trail (",a,b,c,"); hex ids never contain it.
_TRAIL_SEP = ","
# How often (in SQLite VM steps) a running walk checks kg.traverse.time_limit_ms:
# the granularity of the check, not a tunable.
_PROGRESS_STEPS = 1000
# A cut quote ends with this, so a reader knows to open `source` for the whole one.
_CUT_MARK = "…"
ACTIVE, SUPERSEDED = "active", "superseded"
# A task as a timeline shows it, joined to its episode and entity links; `at` as in
# kg/store.py's selects (a task has no validity, so its meeting, else when recorded).
_TASK_SELECT = ("SELECT t.id, t.action, t.owner, t.owner_basis, t.due, t.status, t.quote, t.source_start,"
                " t.episode_id, COALESCE(ep.meeting_start, t.created_at) AS at"
                " FROM task t JOIN episode ep ON ep.id = t.episode_id JOIN task_entity te ON te.task_id = t.id")


def _placeholders(values: Sequence) -> str:
    return ", ".join("?" for _ in values)


def cut(text: Optional[str], limit: int) -> Optional[str]:
    """`text` cut to `limit` characters, ending with the cut mark when it was cut, so
    a reader knows there is more (open `source` for the whole quote). None stays None.
    Shared by the cards here and the viewer (kg/view.py), each with its own limit."""
    if text is None or len(text) <= limit:
        return text
    return text[:limit].rstrip() + _CUT_MARK


class Traverser:
    def __init__(self, store: Store):
        self.store = store
        self.conn = store.conn
        self.caps = store.cfg["kg"]["traverse"]

    # ---- shared pieces --------------------------------------------------------------

    def _start(self, entity_id: str) -> dict:
        """The live entity an id stands for, as a card; `resolved_from` names a
        merged-away id that was followed."""
        live = self.store.live_id(entity_id)
        ent = self.store.entity(live)
        if ent is None:
            raise StoreError(f"no entity {entity_id!r}")
        card = self._entity_card(ent)
        if live != entity_id:
            card["resolved_from"] = entity_id
        return card

    @staticmethod
    def _entity_card(row: dict) -> dict:
        return {"id": row["id"], "type": row["type"], "name": row["canonical_name"]}

    def _hops(self, requested: int) -> int:
        if requested < 1:
            raise StoreError(f"hops must be at least 1, not {requested}")
        return min(requested, self.caps["max_hops"])

    def _scope(self, relations: Optional[Iterable[str]], since: Optional[str], until: Optional[str],
               as_of: Optional[str]) -> tuple[str, list]:
        """The WHERE text (over EDGE_SELECT's aliases) and parameters for the edges a
        walk may use."""
        since, until, as_of = utc_time(since), utc_time(until), utc_time(as_of)
        clauses = ["ep.deleted_at IS NULL", "s.merged_into IS NULL", "d.merged_into IS NULL"]
        params: list = []
        if relations is not None:
            relations = sorted(set(relations))
            unknown = set(relations) - set(self.store.relations)
            if unknown or not relations:
                raise StoreError(f"relations must be from {sorted(self.store.relations)}, not {sorted(unknown)}")
            clauses.append(f"g.relation IN ({_placeholders(relations)})")
            params += relations
        if as_of is None and since is None and until is None:
            clauses.append("g.superseded_by IS NULL")
        if as_of is not None:
            clauses.append("(g.valid_from IS NULL OR g.valid_from <= ?) AND (g.valid_to IS NULL OR g.valid_to > ?)")
            params += [as_of, as_of]
        if since is not None:
            clauses.append("(g.valid_to IS NULL OR g.valid_to > ?)")
            params.append(since)
        if until is not None:
            clauses.append("(g.valid_from IS NULL OR g.valid_from <= ?)")
            params.append(until)
        return " AND ".join(clauses), params

    @staticmethod
    def _step(at: str) -> tuple[str, str]:
        """(JOIN text, other-end expression) for one step from `{at}.node` along an edge
        either way. The join goes through the edge_src / edge_dst indexes, so a step
        reads only the edges at that node, never the whole edge table; the WHERE
        adds `_scope`'s filter over the `g`, `ep`, `s`, `d` aliases it joins."""
        join = (f" JOIN edge g ON (g.src_entity_id = {at}.node OR g.dst_entity_id = {at}.node)"
                " JOIN episode ep ON ep.id = g.episode_id"
                " JOIN entity s ON s.id = g.src_entity_id JOIN entity d ON d.id = g.dst_entity_id")
        return join, f"CASE WHEN g.src_entity_id = {at}.node THEN g.dst_entity_id ELSE g.src_entity_id END"

    @contextmanager
    def _time_limit(self) -> Iterator[None]:
        """Stop a read that runs past kg.traverse.time_limit_ms with a StoreError (an
        isError result over MCP), so one dense neighbourhood never hangs the server."""
        limit = self.caps["time_limit_ms"]
        deadline = time.monotonic() + limit / 1000
        self.conn.set_progress_handler(lambda: time.monotonic() > deadline, _PROGRESS_STEPS)
        try:
            yield
        except sqlite3.OperationalError as exc:
            if time.monotonic() <= deadline:
                raise
            raise StoreError(f"the walk took longer than kg.traverse.time_limit_ms ({limit} ms) and was stopped; "
                             "ask again with fewer hops, a relations filter or a time filter") from exc
        finally:
            self.conn.set_progress_handler(None, 0)

    def _types(self, types: Optional[Iterable[str]]) -> Optional[set[str]]:
        if types is None:
            return None
        wanted = set(types)
        if not wanted or wanted - self.store.entity_types:
            raise StoreError(f"types must be from {sorted(self.store.entity_types)}, not "
                             f"{sorted(wanted - self.store.entity_types)}")
        return wanted

    def _owner(self, through_owner: bool) -> str:
        """The entity a walk must not pass through: the owner's own person node (config
        owner.email). The owner is in every meeting, so stepping through them links
        everything to everything; they can still be a walk's start or end. "" (no
        entity's id) when `through_owner` allows it, or when the owner has no node."""
        email = self.store.cfg["owner"]["email"]
        if through_owner or not email:
            return ""
        return self.store.live_id(keyed_id(PERSON, name_key(email)))

    def _cut(self, text: Optional[str]) -> Optional[str]:
        return cut(text, self.caps["quote_chars"])

    def _edge_card(self, row: dict, parallel: int = 0) -> dict:
        return {"kind": "edge", "id": row["id"], "relation": row["relation"],
                "src_id": row["src_entity_id"], "src_name": row["src_name"],
                "dst_id": row["dst_entity_id"], "dst_name": row["dst_name"],
                "quote": self._cut(row["quote"]), "quote_start": row["quote_start"],
                "provenance": row["provenance"], "confidence": row["confidence"], "episode_id": row["episode_id"],
                "valid_from": row["valid_from"], "valid_to": row["valid_to"],
                "state": SUPERSEDED if row["superseded_by"] else ACTIVE,
                "superseded_by": row["superseded_by"], "supersede_reason": row["supersede_reason"],
                "parallel": parallel}

    @staticmethod
    def _earliest(rows: list[dict]) -> dict:
        """The one edge a hop shows among parallel ones: earliest valid, then by id."""
        return min(rows, key=lambda r: (r["valid_from"] or "", r["id"]))

    def _edges(self, where: str, params: Sequence) -> list[dict]:
        return fetch_all(self.conn, f"{EDGE_SELECT} WHERE {where}", params)

    # ---- neighbors ------------------------------------------------------------------

    def neighbors(self, entity_id: str, *, hops: int, relations: Optional[Iterable[str]] = None,
                  since: Optional[str] = None, until: Optional[str] = None, as_of: Optional[str] = None,
                  types: Optional[Iterable[str]] = None, offset: int = 0, through_owner: bool = False) -> dict:
        """Entities within `hops` edges of `entity_id`, nearest first, then most recently
        linked (the latest valid_from among the edges that reach it), then by name; each
        with the chain of edges that reached it by a shortest route. `types` keeps only
        entities of those types in the answer (the walk still passes through any type);
        `offset` skips that many, for the next page. The walk never passes through the
        owner's own person node unless `through_owner` (see `_owner`)."""
        start = self._start(entity_id)
        used = self._hops(hops)
        scope, params = self._scope(relations, since, until, as_of)
        wanted_types = self._types(types)
        if offset < 0:
            raise StoreError(f"offset must be at least 0, not {offset}")
        owner = self._owner(through_owner)
        reach_join, reach_next = self._step("reach")
        p_join, p_next = self._step("p")
        # Breadth-first reach as (node, depth) rows, deduplicated by UNION, so the walk
        # is bounded by entities x hops rather than by the number of routes; then every
        # edge that steps from one depth to the next, with the child's name for ordering.
        # Only the start may be stepped from when it is the owner (depth 0).
        with self._time_limit():
            rows = fetch_all(self.conn,
                "WITH RECURSIVE reach (node, depth) AS (SELECT ?, 0 UNION"
                f"  SELECT {reach_next}, reach.depth + 1 FROM reach{reach_join}"
                f"  WHERE reach.depth < ? AND (reach.depth = 0 OR reach.node <> ?) AND {scope}),"
                " best (node, depth) AS (SELECT node, MIN(depth) FROM reach GROUP BY node)"
                " SELECT p.node AS parent, c.node AS child, g.id, g.valid_from, c.depth, e.type, e.canonical_name"
                f" FROM best p{p_join} JOIN best c ON c.node = {p_next} JOIN entity e ON e.id = c.node"
                f" WHERE p.depth < ? AND (p.depth = 0 OR p.node <> ?) AND c.depth = p.depth + 1 AND {scope}",
                (start["id"], used, owner, *params, used, owner, *params))
        steps: dict[str, list[dict]] = {}
        for row in rows:
            steps.setdefault(row["child"], []).append(row)
        # Stable sorts, last key first: depth, then latest link, then name.
        order = sorted(steps, key=lambda n: (steps[n][0]["canonical_name"], n))
        order.sort(key=lambda n: max(r["valid_from"] or "" for r in steps[n]), reverse=True)
        order.sort(key=lambda n: steps[n][0]["depth"])
        # Each entity's chain is its parent's plus one hop; parents sit one depth up, so
        # walking in depth order always finds the parent's chain already built.
        chains: dict[str, list[tuple[str, int]]] = {start["id"]: []}
        for node in order:
            step = self._earliest(steps[node])
            parallel = sum(1 for r in steps[node] if r["parent"] == step["parent"]) - 1
            chains[node] = chains[step["parent"]] + [(step["id"], parallel)]
        if wanted_types is not None:
            order = [node for node in order if steps[node][0]["type"] in wanted_types]
        shown = order[offset:offset + self.caps["max_results"]]
        wanted = sorted({edge for node in shown for edge, _ in chains[node]})
        cards = {r["id"]: r for r in self._edges(f"g.id IN ({_placeholders(wanted)})", wanted)} if wanted else {}
        results = [{"entity": {"id": node, "type": steps[node][0]["type"], "name": steps[node][0]["canonical_name"]},
                    "depth": steps[node][0]["depth"],
                    "chain": [self._edge_card(cards[edge], parallel) for edge, parallel in chains[node]]}
                   for node in shown]
        return {"start": start, "hops": used, "hops_requested": hops, "reached": len(order), "offset": offset,
                "truncated": len(order) > offset + len(shown), "results": results}

    # ---- paths ----------------------------------------------------------------------

    def paths(self, src_id: str, dst_id: str, *, max_hops: int, relations: Optional[Iterable[str]] = None,
              as_of: Optional[str] = None, through_owner: bool = False) -> dict:
        """Up to kg.traverse.max_paths shortest routes from src to dst, at most
        `max_hops` edges each, no entity repeated; each route is its ordered edges.
        A route never passes through the owner's own person node (an end may be the
        owner) unless `through_owner`."""
        src, dst = self._start(src_id), self._start(dst_id)
        if src["id"] == dst["id"]:
            raise StoreError(f"{src_id!r} and {dst_id!r} are the same entity")
        used = self._hops(max_hops)
        scope, params = self._scope(relations, None, None, as_of)
        cap = self.caps["max_paths"]
        sep = _TRAIL_SEP
        owner = self._owner(through_owner)
        dist_join, dist_next = self._step("dist")
        walk_join, walk_next = self._step("walk")
        # `near` is each entity's distance to dst (a walk's next entity is never more
        # than max_hops - 1 from it); a walk from src only steps to an entity that can
        # still reach dst within the hop cap, so dead ends are never enumerated.
        # Parallel edges give the same (node, depth, trail) row, which UNION folds;
        # they come back below.
        with self._time_limit():
            rows = fetch_all(self.conn,
                "WITH RECURSIVE dist (node, depth) AS (SELECT ?, 0 UNION"
                f"  SELECT {dist_next}, dist.depth + 1 FROM dist{dist_join}"
                f"  WHERE dist.depth < ? AND (dist.depth = 0 OR dist.node <> ?) AND {scope}),"
                " near (node, depth) AS (SELECT node, MIN(depth) FROM dist GROUP BY node),"
                " walk (node, depth, trail) AS (SELECT ?, 0, ? UNION"
                f"  SELECT near.node, walk.depth + 1, walk.trail || near.node || ? FROM walk{walk_join}"
                f"  JOIN near ON near.node = {walk_next}"
                "  WHERE walk.node <> ? AND (walk.depth = 0 OR walk.node <> ?) AND walk.depth + 1 + near.depth <= ?"
                f"  AND walk.trail NOT LIKE ? || near.node || ? AND {scope})"
                " SELECT trail, depth FROM walk WHERE node = ? ORDER BY depth, trail LIMIT ?",
                (dst["id"], used - 1, owner, *params, src["id"], sep + src["id"] + sep, sep, dst["id"], owner, used,
                 "%" + sep, sep + "%", *params, dst["id"], cap + 1))
        trails = [r["trail"].strip(sep).split(sep) for r in rows[:cap]]
        nodes = sorted({n for trail in trails for n in trail})
        hop_edges: dict[frozenset, list[dict]] = {}
        if nodes:
            marks = _placeholders(nodes)
            for row in self._edges(f"{scope} AND g.src_entity_id IN ({marks}) AND g.dst_entity_id IN ({marks})",
                                   (*params, *nodes, *nodes)):
                hop_edges.setdefault(frozenset((row["src_entity_id"], row["dst_entity_id"])), []).append(row)
        found = []
        for trail in trails:
            edges = []
            for a, b in zip(trail, trail[1:]):
                options = hop_edges[frozenset((a, b))]
                edges.append(self._edge_card(self._earliest(options), len(options) - 1))
            found.append({"length": len(edges), "edges": edges})
        return {"src": src, "dst": dst, "max_hops": used, "max_hops_requested": max_hops,
                "truncated": len(rows) > cap, "paths": found}

    # ---- timeline -------------------------------------------------------------------

    def timeline(self, entity_id: str, *, since: Optional[str] = None, until: Optional[str] = None) -> dict:
        """The entity's facts, edges and tasks in time order, superseded ones included
        and marked, so a change shows as the old row, its reason and the new row. Each
        item's time is when it became valid (else its meeting). When there are more
        than kg.traverse.max_results, the latest are kept."""
        start = self._start(entity_id)
        cap = self.caps["max_results"]
        me = start["id"]
        since, until = utc_time(since), utc_time(until)
        kinds = (
            (f"{FACT_SELECT} WHERE f.subject_entity_id = ? AND ep.deleted_at IS NULL", (me,), self._fact_item),
            (f"{EDGE_SELECT} WHERE (g.src_entity_id = ? OR g.dst_entity_id = ?) AND ep.deleted_at IS NULL",
             (me, me), self._edge_item),
            (f"{_TASK_SELECT} WHERE te.entity_id = ? AND ep.deleted_at IS NULL", (me,), self._task_item),
        )
        window, window_params = [], []
        if since is not None:
            window.append("x.at >= ?")
            window_params.append(since)
        if until is not None:
            window.append("x.at <= ?")
            window_params.append(until)
        where = f" WHERE {' AND '.join(window)}" if window else ""
        items, truncated = [], False
        for select, params, card in kinds:
            # Newest first per kind, one past the cap, so a cut is detected without a count.
            rows = fetch_all(self.conn, f"SELECT * FROM ({select}) x{where} ORDER BY x.at DESC, x.id DESC LIMIT ?",
                             (*params, *window_params, cap + 1))
            truncated = truncated or len(rows) > cap
            items += [card(row) for row in rows[:cap]]
        items.sort(key=lambda i: (i["at"] or "", i["kind"], i["id"]), reverse=True)
        truncated = truncated or len(items) > cap
        return {"entity": start, "since": since, "until": until, "truncated": truncated,
                "items": list(reversed(items[:cap]))}

    def _fact_item(self, row: dict) -> dict:
        return {"kind": "fact", "at": row["at"], "id": row["id"], "type": row["type"], "text": row["text"],
                "quote": self._cut(row["quote"]), "quote_start": row["quote_start"], "provenance": row["provenance"],
                "confidence": row["confidence"], "episode_id": row["episode_id"],
                "valid_from": row["valid_from"], "valid_to": row["valid_to"],
                "state": SUPERSEDED if row["superseded_by"] else ACTIVE,
                "superseded_by": row["superseded_by"], "supersede_reason": row["supersede_reason"]}

    def _edge_item(self, row: dict) -> dict:
        card = self._edge_card(row)
        del card["parallel"]              # a timeline lists every edge; nothing is folded
        return {"at": row["at"], **card}

    def _task_item(self, row: dict) -> dict:
        return {"kind": "task", "at": row["at"], "id": row["id"], "action": row["action"], "owner": row["owner"],
                "owner_basis": row["owner_basis"], "due": row["due"], "status": row["status"],
                "quote": self._cut(row["quote"]), "quote_start": row["source_start"], "episode_id": row["episode_id"]}
