"""Graph integrity checks and health metrics (C.5 steps 3 and 5), run by the daily job's
maintenance step after entity resolution.

- **Database.** `PRAGMA integrity_check` and `PRAGMA foreign_key_check` (SQLite's own
  checks; a Postgres port replaces these two statements). Any problem makes `ok`
  False: the daily job fails its maintenance step and alerts.
- **Dangling references are repaired.** An edge end, a fact subject, an alias or a task
  link still pointing at a merged-away entity is repointed to the survivor its merges
  lead to (Store.live_id). A merge repoints all of these itself, so this finds nothing
  unless a write raced a merge; each repair is logged with the old and new id.
- **Orphans are flagged, not deleted.** A live entity named in no episode (no alias
  recorded with one), with no edge, fact or task link.
- **Health metrics** on the run: live entities, orphans and their rate, and the
  AMBIGUOUS share of active facts and edges (provenance whose quote was not verbatim).

Not yet: the C.5 quote re-check (a fact whose quote check fails is downgraded) needs the
prepared transcript text the write stage had; the write stage already sets provenance
from it, so this pass does not repeat it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from kg.db import fetch_all, transaction
from kg.state import State
from kg.store import AMBIGUOUS, LIVE_EPISODE, Store

CHECK = "integrity"
# (table, column) pairs that name an entity by id, repointed off a merged-away one.
_ENTITY_REFS = (("edge", "src_entity_id"), ("edge", "dst_entity_id"), ("fact", "subject_entity_id"),
                ("alias", "entity_id"))
_ORPHANS = ("SELECT e.id FROM entity e WHERE e.merged_into IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM alias a WHERE a.entity_id = e.id AND a.episode_id IS NOT NULL)"
            " AND NOT EXISTS (SELECT 1 FROM edge g WHERE g.src_entity_id = e.id OR g.dst_entity_id = e.id)"
            " AND NOT EXISTS (SELECT 1 FROM fact f WHERE f.subject_entity_id = e.id)"
            " AND NOT EXISTS (SELECT 1 FROM task_entity t WHERE t.entity_id = e.id) ORDER BY e.id")


def _repair_dangling(store: Store) -> list[dict]:
    conn, repaired = store.conn, []
    for table, column in _ENTITY_REFS:
        for row in fetch_all(conn, f"SELECT x.id AS id, x.{column} AS old FROM {table} x"
                                   f" JOIN entity e ON e.id = x.{column} WHERE e.merged_into IS NOT NULL"):
            new = store.live_id(row["old"])
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (new, row["id"]))
            repaired.append({"table": table, "column": column, "id": row["id"], "old": row["old"], "new": new})
    for row in fetch_all(conn, "SELECT t.task_id, t.entity_id FROM task_entity t"
                               " JOIN entity e ON e.id = t.entity_id WHERE e.merged_into IS NOT NULL"):
        new = store.live_id(row["entity_id"])
        held = conn.execute("SELECT 1 FROM task_entity WHERE task_id = ? AND entity_id = ?",
                            (row["task_id"], new)).fetchone()
        if held:        # the task already links the survivor: the stale link goes
            conn.execute("DELETE FROM task_entity WHERE task_id = ? AND entity_id = ?",
                         (row["task_id"], row["entity_id"]))
        else:
            conn.execute("UPDATE task_entity SET entity_id = ? WHERE task_id = ? AND entity_id = ?",
                         (new, row["task_id"], row["entity_id"]))
        repaired.append({"table": "task_entity", "column": "entity_id", "id": row["task_id"],
                         "old": row["entity_id"], "new": new})
    return repaired


def _ambiguous_share(store: Store) -> float:
    counts = {"all": 0, AMBIGUOUS: 0}
    for table in ("fact", "edge"):
        for row in store.conn.execute(
                f"SELECT x.provenance, COUNT(*) FROM {table} x JOIN episode ep ON ep.id = x.episode_id"
                f" WHERE x.superseded_by IS NULL AND {LIVE_EPISODE} GROUP BY x.provenance"):
            counts["all"] += row[1]
            if row[0] == AMBIGUOUS:
                counts[AMBIGUOUS] += row[1]
    return counts[AMBIGUOUS] / counts["all"] if counts["all"] else 0.0


def check(store: Store, state: State, run_id: str, *, listed: int,
          now: Optional[datetime] = None) -> dict:
    """Run every check against a running run; one `integrity` maintenance_log row and the
    health metrics. Returns {"ok", "problems", "repaired", "orphans", "metrics"}, with at
    most `listed` orphan ids in it."""
    conn = store.conn
    problems = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    problems = [] if problems == ["ok"] else problems
    problems += [f"foreign key: {row[0]} row {row[1]} -> {row[2]}" for row in conn.execute("PRAGMA foreign_key_check")]
    with transaction(conn):
        repaired = _repair_dangling(store)
    orphans = [row[0] for row in conn.execute(_ORPHANS)]
    live = conn.execute("SELECT COUNT(*) FROM entity WHERE merged_into IS NULL").fetchone()[0]
    metrics = {"live_entities": float(live), "orphans": float(len(orphans)),
               "orphan_rate": len(orphans) / live if live else 0.0, "ambiguous_share": _ambiguous_share(store)}
    for name, value in metrics.items():
        state.record_metric(run_id, name, value)
    out = {"ok": not problems, "problems": problems[:listed], "repaired": repaired,
           "orphans": {"count": len(orphans), "ids": orphans[:listed]}, "metrics": metrics}
    state.log_maintenance(run_id, CHECK, found=len(problems) + len(repaired) + len(orphans), repaired=len(repaired),
                          escalated=len(orphans), details=json.dumps(out), now=now)
    return out
