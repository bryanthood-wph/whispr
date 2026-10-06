"""The one data-access module for the knowledge graph and its tasks (C.4, D.5).

Every graph read and write goes through `Store`, so the SQL, and the SQLite-only full-
text search behind `search`, can move to Postgres in one place. The rules it enforces:

- **Provenance.** A fact or edge is `EXTRACTED` only if its quote is a verbatim
  substring of the episode's transcript text the caller passes in; anything else,
  an empty quote included, is stored as `AMBIGUOUS`. Nothing is rejected for it.
- **Nothing is deleted.** An episode is tombstoned; a fact or edge is superseded by
  a newer one, with a reason, and stops being active. Reads show only active rows of
  live episodes.
- **People.** A person is keyed by email (Outlook attendees). Every name an entity
  goes by is an alias row; a mention is matched against aliases. A first-name-only
  mention that matches no single person stays an unresolved alias and never becomes
  an entity (lesson L12); so does any name that matches two people. A full name with
  no email and no match becomes a person keyed by that name, for C.5 to merge later.
- **Types.** Entity, fact and relation types come from config/ontology.yaml, and an
  edge's endpoints must have the types its relation allows.
- **Tasks.** A task is inserted from the D.5 contract, validated against
  config/schema/task.json, and its id must be sha1(episode + quote). The allowed
  status changes and the funnel order are rows in the database (migration 0001), and
  their statuses must equal the schema's enum. Re-inserting a known task is a no-op,
  so re-writing an episode never resets a task's lifecycle (lesson L19).
- **Idempotent writes.** Ids are derived from content, so writing the same episode
  twice adds nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Iterable, Optional

from kg.db import fetch_all, fetch_one, new_id, stable_id, transaction, utc_now
from pipeline.config import config_file, load_schema, read_yaml
from pipeline.jsonschema_lite import validate

EXTRACTED = "EXTRACTED"
AMBIGUOUS = "AMBIGUOUS"
# The one entity type keyed by email rather than by name (C.4).
PERSON = "person"
# Relation endpoint wildcard in ontology.yaml ("any -> any").
ANY_TYPE = "any"
# A person with no email is keyed by name under this prefix, which no email can carry.
NAME_KEY_PREFIX = "name:"

_RELATION_NOTE = re.compile(r"\(.*\)")
_WORD = re.compile(r"\w+")
# Searchable rows by kind (kg/migrations/0002_search.sqlite.sql).
_ENTITY_KINDS = ("entity", "alias")
_FACT_KIND = "fact"
# Tables `_supersede` may touch: a fixed map, never caller text, so it is safe in SQL.
_SUPERSEDABLE = {"fact": "fact", "edge": "edge"}

_ACTIVE_FACT = "f.superseded_by IS NULL AND ep.deleted_at IS NULL"
_ACTIVE_EDGE = "g.superseded_by IS NULL AND ep.deleted_at IS NULL"


class StoreError(ValueError):
    pass


def name_key(text: str) -> str:
    """The form names and emails are matched in: case-folded, whitespace collapsed."""
    return " ".join(text.casefold().split())


def provenance(quote: str, transcript_text: str) -> str:
    """EXTRACTED when the quote appears verbatim in the transcript, else AMBIGUOUS."""
    return EXTRACTED if quote.strip() and quote in transcript_text else AMBIGUOUS


def task_id(episode_id: str, quote: str) -> str:
    """The D.5 task id: sha1(episode + quote)."""
    return hashlib.sha1((episode_id + quote).encode("utf-8")).hexdigest()


def _parse_relations(ontology: dict) -> dict[str, tuple[set[str], set[str]]]:
    """relation -> (allowed source types, allowed target types), from "a | b -> c"."""
    parsed = {}
    for relation, spec in ontology["relations"].items():
        src, dst = _RELATION_NOTE.sub("", spec).split("->")
        parsed[relation] = ({t.strip() for t in src.split("|")}, {t.strip() for t in dst.split("|")})
    return parsed


class Store:
    def __init__(self, conn: sqlite3.Connection, cfg: dict):
        self.conn = conn
        self.cfg = cfg
        ontology = read_yaml(config_file(cfg["ontology"]))
        self.entity_types = set(ontology["entity_types"])
        self.fact_types = set(ontology["fact_types"])
        if PERSON not in self.entity_types:
            raise StoreError(f"{cfg['ontology']} has no {PERSON!r} entity type, which people are stored as")
        self.relations = _parse_relations(ontology)
        self.task_schema = load_schema(cfg, "task")
        self._check_task_statuses()

    def _check_task_statuses(self) -> None:
        in_db = {r[0] for r in self.conn.execute("SELECT status FROM task_status")}
        in_schema = set(self.task_schema["properties"]["status"]["enum"])
        if in_db != in_schema:
            raise StoreError(f"task statuses differ: database {sorted(in_db)}, "
                             f"config/schema/task.json {sorted(in_schema)}; add a migration")

    # ---- episodes -------------------------------------------------------------------

    def upsert_episode(self, episode_id: str, *, transcript_path: str, sha256: str,
                       meeting_start: Optional[str] = None, call_type: Optional[str] = None,
                       extractor_version: Optional[str] = None, mic_coverage: Optional[float] = None,
                       output_device: Optional[str] = None, now: Optional[datetime] = None) -> str:
        """Insert or refresh an episode. Writing it again clears a tombstone: the
        transcript is back, so its episode is live again."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO episode (id, transcript_path, sha256, meeting_start, call_type, extractor_version,"
                " mic_coverage, output_device, deleted_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)"
                " ON CONFLICT (id) DO UPDATE SET transcript_path = excluded.transcript_path,"
                " sha256 = excluded.sha256, meeting_start = excluded.meeting_start,"
                " call_type = excluded.call_type, extractor_version = excluded.extractor_version,"
                " mic_coverage = excluded.mic_coverage, output_device = excluded.output_device,"
                " deleted_at = NULL",
                (episode_id, transcript_path, sha256, meeting_start, call_type, extractor_version,
                 mic_coverage, output_device, utc_now(now)))
        return episode_id

    def tombstone_episode(self, episode_id: str, now: Optional[datetime] = None) -> None:
        """Mark an episode deleted. Its rows stay; reads stop showing them."""
        if self.episode(episode_id) is None:
            raise StoreError(f"no episode {episode_id!r}")
        with transaction(self.conn):
            self.conn.execute("UPDATE episode SET deleted_at = COALESCE(deleted_at, ?) WHERE id = ?",
                              (utc_now(now), episode_id))

    def episode(self, episode_id: str) -> Optional[dict]:
        return fetch_one(self.conn, "SELECT * FROM episode WHERE id = ?", (episode_id,))

    def _live_episode(self, episode_id: str) -> dict:
        ep = self.episode(episode_id)
        if ep is None:
            raise StoreError(f"no episode {episode_id!r}")
        if ep["deleted_at"] is not None:
            raise StoreError(f"episode {episode_id!r} is tombstoned")
        return ep

    # ---- entities and aliases -------------------------------------------------------

    def entity(self, entity_id: str) -> Optional[dict]:
        return fetch_one(self.conn, "SELECT * FROM entity WHERE id = ?", (entity_id,))

    def aliases(self, entity_id: str) -> list[str]:
        return [r["alias"] for r in self.conn.execute(
            "SELECT alias FROM alias WHERE entity_id = ? ORDER BY alias_key, alias", (entity_id,))]

    def unresolved_aliases(self) -> list[dict]:
        """Mentions waiting for C.5 entity resolution."""
        return fetch_all(self.conn, "SELECT * FROM alias WHERE entity_id IS NULL ORDER BY created_at")

    def _create_entity(self, type_: str, name: str, key: str, now: Optional[datetime]) -> str:
        entity_id = stable_id("entity", type_, key)
        self.conn.execute(
            "INSERT INTO entity (id, type, canonical_name, canonical_key, merged_into, created_at)"
            " VALUES (?, ?, ?, ?, NULL, ?) ON CONFLICT (id) DO NOTHING",
            (entity_id, type_, name.strip(), key, utc_now(now)))
        return entity_id

    def add_alias(self, entity_id: Optional[str], alias: str, *, source: str,
                  episode_id: Optional[str] = None, now: Optional[datetime] = None) -> str:
        """Record a name an entity goes by; entity_id None records an unresolved mention
        (kept once per episode, so resolution can see where it was said)."""
        key = name_key(alias)
        if not key:
            raise StoreError("empty alias")
        if entity_id is not None and self.entity(entity_id) is None:
            raise StoreError(f"no entity {entity_id!r}")
        alias_id = (stable_id("alias", entity_id, key) if entity_id is not None
                    else stable_id("alias", "", key, episode_id or ""))
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO alias (id, entity_id, alias, alias_key, source, episode_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
                (alias_id, entity_id, alias.strip(), key, source, episode_id, utc_now(now)))
        return alias_id

    def upsert_person(self, email: str, name: str, *, source: str, episode_id: Optional[str] = None,
                      now: Optional[datetime] = None) -> str:
        """A person keyed by email (an Outlook attendee); `name` is kept as an alias."""
        key = name_key(email)
        if not key:
            raise StoreError("a person needs an email to be keyed by")
        with transaction(self.conn):
            entity_id = self._create_entity(PERSON, name or email, key, now)
            if name_key(name):
                self.add_alias(entity_id, name, source=source, episode_id=episode_id, now=now)
        return entity_id

    def person_matches(self, name: str) -> list[str]:
        """Live people with an alias equal to `name` (after name_key)."""
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT e.id FROM entity e JOIN alias a ON a.entity_id = e.id"
            " WHERE e.type = ? AND e.merged_into IS NULL AND a.alias_key = ? ORDER BY e.id",
            (PERSON, name_key(name)))]

    def mention_person(self, name: str, *, source: str, episode_id: Optional[str] = None,
                       now: Optional[datetime] = None) -> Optional[str]:
        """Resolve a person named in a transcript. Returns the entity id, or None when
        the mention stays an unresolved alias (no single match and a first name only,
        or several people match)."""
        key = name_key(name)
        if not key:
            raise StoreError("empty person name")
        matches = self.person_matches(name)
        if len(matches) == 1:
            return matches[0]
        with transaction(self.conn):
            if not matches and len(key.split()) > 1:
                entity_id = self._create_entity(PERSON, name, NAME_KEY_PREFIX + key, now)
                self.add_alias(entity_id, name, source=source, episode_id=episode_id, now=now)
                return entity_id
            self.add_alias(None, name, source=source, episode_id=episode_id, now=now)
        return None

    def upsert_entity(self, type_: str, name: str, *, source: str, aliases: Iterable[str] = (),
                      episode_id: Optional[str] = None, now: Optional[datetime] = None) -> str:
        """A non-person entity keyed by type + name, with its name and aliases recorded."""
        if type_ == PERSON:
            raise StoreError("people are keyed by email: use upsert_person or mention_person")
        if type_ not in self.entity_types:
            raise StoreError(f"entity type {type_!r} is not in the ontology")
        key = name_key(name)
        if not key:
            raise StoreError("empty entity name")
        with transaction(self.conn):
            entity_id = self._create_entity(type_, name, key, now)
            for alias in (name, *aliases):
                if name_key(alias):
                    self.add_alias(entity_id, alias, source=source, episode_id=episode_id, now=now)
        return entity_id

    # ---- facts and edges ------------------------------------------------------------

    def add_fact(self, *, type_: str, text: str, quote: str, episode_id: str, transcript_text: str,
                 subject_entity_id: Optional[str] = None, quote_start: Optional[str] = None,
                 confidence: Optional[float] = None, valid_from: Optional[str] = None,
                 valid_to: Optional[str] = None, now: Optional[datetime] = None) -> str:
        """Store a fact. valid_from defaults to the episode's meeting start."""
        if type_ not in self.fact_types:
            raise StoreError(f"fact type {type_!r} is not in the ontology")
        ep = self._live_episode(episode_id)
        if subject_entity_id is not None and self.entity(subject_entity_id) is None:
            raise StoreError(f"no entity {subject_entity_id!r}")
        fact_id = stable_id("fact", episode_id, type_, text, quote)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO fact (id, type, text, quote, quote_start, episode_id, subject_entity_id, provenance,"
                " confidence, valid_from, valid_to, superseded_by, supersede_reason, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?) ON CONFLICT (id) DO NOTHING",
                (fact_id, type_, text, quote, quote_start, episode_id, subject_entity_id,
                 provenance(quote, transcript_text), confidence, valid_from or ep["meeting_start"],
                 valid_to, utc_now(now)))
        return fact_id

    def add_edge(self, *, src_entity_id: str, dst_entity_id: str, relation: str, quote: str,
                 episode_id: str, transcript_text: str, quote_start: Optional[str] = None,
                 confidence: Optional[float] = None, valid_from: Optional[str] = None,
                 valid_to: Optional[str] = None, now: Optional[datetime] = None) -> str:
        """Store a typed edge whose endpoints have the types the relation allows."""
        if relation not in self.relations:
            raise StoreError(f"relation {relation!r} is not in the ontology")
        ep = self._live_episode(episode_id)
        allowed = self.relations[relation]
        for entity_id, types, end in ((src_entity_id, allowed[0], "source"), (dst_entity_id, allowed[1], "target")):
            ent = self.entity(entity_id)
            if ent is None:
                raise StoreError(f"no entity {entity_id!r}")
            if ANY_TYPE not in types and ent["type"] not in types:
                raise StoreError(f"{relation} {end} must be {' | '.join(sorted(types))}, not {ent['type']}")
        edge_id = stable_id("edge", episode_id, src_entity_id, relation, dst_entity_id, quote)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO edge (id, src_entity_id, dst_entity_id, relation, quote, quote_start, episode_id,"
                " provenance, confidence, valid_from, valid_to, superseded_by, supersede_reason, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?) ON CONFLICT (id) DO NOTHING",
                (edge_id, src_entity_id, dst_entity_id, relation, quote, quote_start, episode_id,
                 provenance(quote, transcript_text), confidence, valid_from or ep["meeting_start"],
                 valid_to, utc_now(now)))
        return edge_id

    def supersede_fact(self, old_id: str, new_id: str, *, reason: str, now: Optional[datetime] = None) -> None:
        self._supersede("fact", old_id, new_id, reason, now)

    def supersede_edge(self, old_id: str, new_id: str, *, reason: str, now: Optional[datetime] = None) -> None:
        self._supersede("edge", old_id, new_id, reason, now)

    def _supersede(self, kind: str, old_id: str, new_id: str, reason: str, now: Optional[datetime]) -> None:
        """Point the old row at its replacement and close its validity at the new
        row's start. Never deletes; a row can be superseded once."""
        table = _SUPERSEDABLE[kind]
        if old_id == new_id:
            raise StoreError(f"a {kind} cannot supersede itself")
        if not reason.strip():
            raise StoreError("superseding needs a reason")
        old = fetch_one(self.conn, f"SELECT * FROM {table} WHERE id = ?", (old_id,))
        new = fetch_one(self.conn, f"SELECT * FROM {table} WHERE id = ?", (new_id,))
        if old is None or new is None:
            raise StoreError(f"no {kind} {old_id if old is None else new_id!r}")
        if old["superseded_by"] is not None:
            raise StoreError(f"{kind} {old_id!r} is already superseded by {old['superseded_by']!r}")
        if new["superseded_by"] is not None:
            raise StoreError(f"{kind} {new_id!r} is itself superseded")
        with transaction(self.conn):
            self.conn.execute(
                f"UPDATE {table} SET superseded_by = ?, supersede_reason = ?,"
                f" valid_to = COALESCE(valid_to, ?) WHERE id = ?",
                (new_id, reason, new["valid_from"] or utc_now(now), old_id))

    # ---- tasks ----------------------------------------------------------------------

    def add_task(self, task: dict, *, entity_ids: Iterable[str] = (), now: Optional[datetime] = None) -> str:
        """Insert a task from the D.5 contract, linked to entities. A known id is left
        as it is (its lifecycle lives here); new entity links are still added."""
        errors = validate(task, self.task_schema)
        if errors:
            raise StoreError("task does not match config/schema/task.json:\n  " + "\n  ".join(errors))
        episode_id = task["source"]["episode"]
        if task["id"] != task_id(episode_id, task["quote"]):
            raise StoreError(f"task id {task['id']!r} is not sha1(episode + quote)")
        self._live_episode(episode_id)
        entity_ids = list(entity_ids)
        for entity_id in entity_ids:
            if self.entity(entity_id) is None:
                raise StoreError(f"no entity {entity_id!r}")
        stamp = utc_now(now)
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO task (id, owner, owner_basis, action, due, due_basis, context, quote, episode_id,"
                " source_start, confidence, status, tools_allowed, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
                (task["id"], task["owner"], task["owner_basis"], task["action"], task["due"], task["due_basis"],
                 task["context"], task["quote"], episode_id, task["source"]["start"], task["confidence"],
                 task["status"], json.dumps(task["tools_allowed"]), stamp, stamp))
            if cur.rowcount == 1:
                self._task_event(task["id"], None, task["status"], None, stamp)
            for entity_id in entity_ids:
                self.conn.execute("INSERT INTO task_entity (task_id, entity_id) VALUES (?, ?)"
                                  " ON CONFLICT (task_id, entity_id) DO NOTHING", (task["id"], entity_id))
        return task["id"]

    def _task_event(self, task_id_: str, from_status: Optional[str], to_status: str,
                    note: Optional[str], stamp: str) -> None:
        self.conn.execute("INSERT INTO task_event (id, task_id, from_status, to_status, note, at)"
                          " VALUES (?, ?, ?, ?, ?, ?)", (new_id(), task_id_, from_status, to_status, note, stamp))

    def allowed_transitions(self, status: str) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT to_status FROM task_transition WHERE from_status = ?", (status,))}

    def set_task_status(self, task_id_: str, status: str, *, note: Optional[str] = None,
                        now: Optional[datetime] = None) -> None:
        """Move a task along an allowed transition, recording the event."""
        row = fetch_one(self.conn, "SELECT status FROM task WHERE id = ?", (task_id_,))
        if row is None:
            raise StoreError(f"no task {task_id_!r}")
        current = row["status"]
        if status not in self.allowed_transitions(current):
            raise StoreError(f"task {task_id_!r}: {current} -> {status} is not an allowed transition")
        stamp = utc_now(now)
        with transaction(self.conn):
            self.conn.execute("UPDATE task SET status = ?, updated_at = ? WHERE id = ?", (status, stamp, task_id_))
            self._task_event(task_id_, current, status, note, stamp)

    def _contract(self, row: dict) -> dict:
        return {"id": row["id"], "owner": row["owner"], "owner_basis": row["owner_basis"],
                "action": row["action"], "due": row["due"], "due_basis": row["due_basis"],
                "context": row["context"], "quote": row["quote"],
                "source": {"episode": row["episode_id"], "start": row["source_start"]},
                "confidence": row["confidence"], "status": row["status"],
                "tools_allowed": json.loads(row["tools_allowed"])}

    def get_task(self, task_id_: str) -> Optional[dict]:
        """The task in its D.5 contract shape."""
        row = fetch_one(self.conn, "SELECT * FROM task WHERE id = ?", (task_id_,))
        return self._contract(row) if row else None

    def tasks(self, status: Optional[str] = None) -> list[dict]:
        sql, params = "SELECT * FROM task", ()
        if status is not None:
            sql, params = sql + " WHERE status = ?", (status,)
        return [self._contract(r) for r in fetch_all(self.conn, sql + " ORDER BY created_at, id", params)]

    def task_entities(self, task_id_: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT entity_id FROM task_entity WHERE task_id = ? ORDER BY entity_id", (task_id_,))]

    def funnel(self, since: Optional[str] = None) -> dict[str, int]:
        """Funnel stage -> tasks that ever reached it (D.5: captured -> confirmed ->
        ready -> done), over tasks created at or after `since` (ISO time) if given."""
        reached = ("SELECT ev.task_id, MAX(stage.funnel_rank) AS top FROM task_event ev"
                   " JOIN task_status st ON st.status = ev.to_status"
                   " JOIN task_status stage ON stage.status = st.reaches GROUP BY ev.task_id")
        where, params = "", []
        if since is not None:
            where, params = " AND t.created_at >= ?", [since]
        stages = fetch_all(self.conn, "SELECT status, funnel_rank FROM task_status WHERE funnel_rank IS NOT NULL"
                                      " ORDER BY funnel_rank")
        counts = {}
        for stage in stages:
            counts[stage["status"]] = self.conn.execute(
                f"SELECT COUNT(*) FROM ({reached}) r JOIN task t ON t.id = r.task_id"
                f" WHERE r.top >= ?{where}", (stage["funnel_rank"], *params)).fetchone()[0]
        return counts

    # ---- reads for MCP: search -> get -> source (progressive disclosure) -------------

    def search(self, text: str) -> list[dict]:
        """Short cards for live entities and active facts matching any word of `text`,
        best match first, at most kg.search_cards. Full text is SQLite FTS5 here."""
        words = _WORD.findall(text)
        if not words:
            return []
        match = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
        hits = self.conn.execute("SELECT kind, ref_id FROM search_fts WHERE search_fts MATCH ? ORDER BY rank",
                                 (match,)).fetchall()
        limit = self.cfg["kg"]["search_cards"]
        cards, seen = [], set()
        for kind, ref_id in hits:
            card = self._entity_card(ref_id) if kind in _ENTITY_KINDS else self._fact_card(ref_id)
            if card is None or card["id"] in seen:
                continue
            seen.add(card["id"])
            cards.append(card)
            if len(cards) == limit:
                break
        return cards

    def _entity_card(self, entity_id: str) -> Optional[dict]:
        row = fetch_one(self.conn,
            "SELECT e.id, e.type, e.canonical_name,"
            f" (SELECT COUNT(*) FROM fact f JOIN episode ep ON ep.id = f.episode_id"
            f"  WHERE f.subject_entity_id = e.id AND {_ACTIVE_FACT}) AS facts"
            " FROM entity e WHERE e.id = ? AND e.merged_into IS NULL", (entity_id,))
        if row is None:
            return None
        return {"kind": "entity", "id": row["id"], "type": row["type"], "name": row["canonical_name"],
                "facts": row["facts"]}

    def _fact_card(self, fact_id: str) -> Optional[dict]:
        row = fetch_one(self.conn, "SELECT f.*, ep.meeting_start FROM fact f"
                                   f" JOIN episode ep ON ep.id = f.episode_id WHERE f.id = ? AND {_ACTIVE_FACT}",
                        (fact_id,))
        if row is None:
            return None
        return {"kind": _FACT_KIND, "id": row["id"], "type": row["type"], "text": row["text"],
                "episode_id": row["episode_id"], "meeting_start": row["meeting_start"],
                "provenance": row["provenance"]}

    def get(self, item_id: str) -> Optional[dict]:
        """One entity (its aliases, active facts with quotes, active edges), fact, edge
        or task by id; None if no such id. A fact or edge is returned even when
        superseded, with superseded_by set, so a history can be followed."""
        ent = self.entity(item_id)
        if ent is not None:
            facts = fetch_all(self.conn,
                "SELECT f.id, f.type, f.text, f.quote, f.quote_start, f.provenance, f.confidence, f.episode_id,"
                f" f.valid_from, f.valid_to FROM fact f JOIN episode ep ON ep.id = f.episode_id"
                f" WHERE f.subject_entity_id = ? AND {_ACTIVE_FACT} ORDER BY f.valid_from, f.id", (item_id,))
            edges = fetch_all(self.conn,
                "SELECT g.id, g.relation, g.src_entity_id, s.canonical_name AS src_name, g.dst_entity_id,"
                " d.canonical_name AS dst_name, g.quote, g.quote_start, g.provenance, g.episode_id,"
                " g.valid_from, g.valid_to FROM edge g JOIN episode ep ON ep.id = g.episode_id"
                " JOIN entity s ON s.id = g.src_entity_id JOIN entity d ON d.id = g.dst_entity_id"
                f" WHERE (g.src_entity_id = ? OR g.dst_entity_id = ?) AND {_ACTIVE_EDGE}"
                " ORDER BY g.valid_from, g.id", (item_id, item_id))
            return {"kind": "entity", "id": ent["id"], "type": ent["type"], "name": ent["canonical_name"],
                    "merged_into": ent["merged_into"], "aliases": self.aliases(item_id),
                    "facts": facts, "edges": edges}
        for kind in _SUPERSEDABLE:
            row = fetch_one(self.conn, f"SELECT * FROM {_SUPERSEDABLE[kind]} WHERE id = ?", (item_id,))
            if row is not None:
                return {"kind": kind, **row}
        task = self.get_task(item_id)
        if task is not None:
            return {"kind": "task", "task": task, "entity_ids": self.task_entities(item_id)}
        return None

    def source(self, episode_id: str, item_id: Optional[str] = None) -> Optional[dict]:
        """Where to read an episode's evidence: the transcript path and sha256, and the
        quoted spans (kind, id, start timestamp, quote) of its facts, edges and tasks,
        or of the one item `item_id`. None if no such episode."""
        ep = self.episode(episode_id)
        if ep is None:
            return None
        spans = fetch_all(self.conn,
            "SELECT 'fact' AS kind, id, quote_start AS start, quote FROM fact WHERE episode_id = ?"
            " UNION ALL SELECT 'edge', id, quote_start, quote FROM edge WHERE episode_id = ?"
            " UNION ALL SELECT 'task', id, source_start, quote FROM task WHERE episode_id = ?"
            " ORDER BY start, kind, id", (episode_id,) * 3)
        if item_id is not None:
            spans = [s for s in spans if s["id"] == item_id]
            if not spans:
                raise StoreError(f"{item_id!r} is not in episode {episode_id!r}")
        return {"episode_id": ep["id"], "transcript_path": ep["transcript_path"], "sha256": ep["sha256"],
                "meeting_start": ep["meeting_start"], "deleted_at": ep["deleted_at"], "spans": spans}
