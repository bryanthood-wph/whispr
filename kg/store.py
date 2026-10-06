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
  an entity (lesson L12); so does any name that matches two people. A first name is
  matched only against the first word of full names, never a one-word alias. A full name with
  no email and no match becomes a person keyed by that name, for C.5 to merge later.
  After a merge (kg/resolve.py) every lookup and write follows `merged_into` to the
  survivor (`live_id`), so a later mention never lands on the merged-away entity.
- **Types.** Entity, fact and relation types come from config/ontology.yaml, and an
  edge's endpoints must have the types its relation allows.
- **Tasks.** A task is inserted from the D.5 contract, validated against
  config/schema/task.json, and its id must be sha1(episode + quote), or, for a quote
  two tasks of one document share, that plus the normalized action (`task_id`). The allowed
  status changes and the funnel order are rows in the database (migration 0001), and
  their statuses must equal the schema's enum. Re-inserting a known task is a no-op,
  so re-writing an episode never resets a task's lifecycle (lesson L19). A review
  change (`update_task`) moves a task only along an allowed transition and records
  who, why and when, with any clarification, inputs and granted tools, as rows.
- **The brief and the ready gate** (docs/plan/task-intake-and-worker.md §3, §4). A brief
  answer is a task_note row of kind `brief` (migration 0007): the field, the answer, who
  gave it (stated or confirmed) and when; the newest row per field is the answer and the
  rest its history. A move to kg.tasks.tools_status (ready) is refused (`BriefOpenError`)
  while a required field has no answer, whoever asks; a task is only ever inserted at the
  funnel's first stage, so no task reaches ready any other way. A project's default scope is its
  `entity_scope` rows, read and replaced across the entity's merges; a review can link
  a task to its project (a task_entity row, only ever added).
- **Idempotent writes.** Ids are derived from content, so writing the same episode
  twice adds nothing, after a merge too: an edge is matched by episode, relation,
  quote and live endpoints, since a merge repoints an edge but keeps its id.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime
from typing import Iterable, Optional

from kg.db import fetch_all, fetch_one, new_id, stable_id, transaction, utc_now, utc_time
from pipeline.config import config_file, load_schema, named_field, read_yaml, required_fields
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
# Searchable rows by kind (kg/migrations/0002_search.sqlite.sql; an alias row's ref_id is
# the alias id since 0003_alias_fts_by_id.sqlite.sql).
_ENTITY_KIND = "entity"
_ALIAS_KIND = "alias"
_FACT_KIND = "fact"
# Tables `_supersede` may touch: a fixed map, never caller text, so it is safe in SQL.
_SUPERSEDABLE = {"fact": "fact", "edge": "edge"}
# task_note kinds (kg/migrations/0006_task_review.sql).
NOTE_CLARIFICATION = "clarification"
NOTE_INPUT = "input"
# A brief answer (kg/migrations/0007_task_intake.sql): field, answer, source.
NOTE_BRIEF = "brief"
# An entity and every entity merged into it, however deep: the ids a scope is read across.
_MERGE_GROUP = ("WITH RECURSIVE grp(id) AS (SELECT ? UNION SELECT e.id FROM entity e"
                " JOIN grp ON e.merged_into = grp.id)")

ACTIVE_FACT = "f.superseded_by IS NULL AND ep.deleted_at IS NULL"
ACTIVE_EDGE = "g.superseded_by IS NULL AND ep.deleted_at IS NULL"

# The one shape a fact and an edge are read in (get here, the traversals in
# kg/traverse.py), joined to the episode as `ep` (and an edge's endpoints as `s`, `d`)
# so the filters above apply. `at` is when the row became valid, else its meeting,
# else when it was recorded: the time a timeline orders by. Callers add WHERE / ORDER BY.
FACT_SELECT = ("SELECT f.id, f.type, f.text, f.quote, f.quote_start, f.provenance, f.confidence, f.episode_id,"
               " f.subject_entity_id, f.valid_from, f.valid_to, f.superseded_by, f.supersede_reason,"
               " COALESCE(f.valid_from, ep.meeting_start, f.recorded_at) AS at"
               " FROM fact f JOIN episode ep ON ep.id = f.episode_id")
EDGE_SELECT = ("SELECT g.id, g.relation, g.src_entity_id, s.canonical_name AS src_name, g.dst_entity_id,"
               " d.canonical_name AS dst_name, g.quote, g.quote_start, g.provenance, g.confidence, g.episode_id,"
               " g.valid_from, g.valid_to, g.superseded_by, g.supersede_reason,"
               " COALESCE(g.valid_from, ep.meeting_start, g.recorded_at) AS at"
               " FROM edge g JOIN episode ep ON ep.id = g.episode_id"
               " JOIN entity s ON s.id = g.src_entity_id JOIN entity d ON d.id = g.dst_entity_id")


class StoreError(ValueError):
    pass


class TransitionError(StoreError):
    """A status change the seeded transitions do not allow; `allowed` names the next
    states the task can move to."""

    def __init__(self, task_id_: str, current: str, wanted: str, allowed: Iterable[str]):
        self.current, self.wanted, self.allowed = current, wanted, sorted(allowed)
        nxt = ", ".join(self.allowed) or "none (it is final)"
        super().__init__(f"task {task_id_!r}: {current} -> {wanted} is not an allowed transition; "
                         f"allowed next from {current}: {nxt}")


class BriefOpenError(StoreError):
    """A move to the ready status while required brief fields have no answer; `open`
    names them, in tasks.intake.required_fields order."""

    def __init__(self, task_id_: str, status: str, open_fields: Iterable[str]):
        self.open = list(open_fields)
        super().__init__(f"task {task_id_!r} can't be marked {status} while required brief field(s) are open: "
                         f"{', '.join(self.open)}; answer them (brief, scope) first")


def name_key(text: str) -> str:
    """The form names and emails are matched in: case-folded, whitespace collapsed."""
    return " ".join(text.casefold().split())


def keyed_id(type_: str, key: str) -> str:
    """The id of the entity of this type and canonical key (a person's email key)."""
    return stable_id("entity", type_, key)


def provenance(quote: str, transcript_text: str) -> str:
    """EXTRACTED when the quote appears verbatim in the transcript, else AMBIGUOUS."""
    return EXTRACTED if quote.strip() and quote in transcript_text else AMBIGUOUS


def task_id(episode_id: str, quote: str, action: Optional[str] = None) -> str:
    """The D.5 task id: sha1(episode + quote). When two or more of one document's tasks
    share a quote (the caller decides), each passes its action, so none folds into
    another: sha1(episode + quote + "\x1f" + the action lower-cased, whitespace collapsed)."""
    key = episode_id + quote if action is None else episode_id + quote + "\x1f" + " ".join(action.lower().split())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _in(column: str, values: Iterable[str]) -> tuple[str, list]:
    """`column IN (?, ...)` and its parameters; an empty list matches nothing (Postgres
    refuses `IN ()`)."""
    values = list(values)
    if not values:
        return "1 = 0", []
    return f"{column} IN ({', '.join('?' * len(values))})", values


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
        self.task_cfg = cfg["kg"]["tasks"]
        self.intake = cfg["tasks"]["intake"]
        self.required_fields = required_fields(cfg)
        self.scope_field = named_field(cfg, "scope_field")
        self.budget_field = named_field(cfg, "budget_field")
        self._check_task_config()

    def _check_task_statuses(self) -> None:
        in_db = {r[0] for r in self.conn.execute("SELECT status FROM task_status")}
        in_schema = set(self.task_schema["properties"]["status"]["enum"])
        if in_db != in_schema:
            raise StoreError(f"task statuses differ: database {sorted(in_db)}, "
                             f"config/schema/task.json {sorted(in_schema)}; add a migration")

    def _check_task_config(self) -> None:
        """kg.tasks names owner_basis values and statuses: each must be in the task schema,
        and every "confirm?" basis must be one of the owner's own (or task_list would
        hide tasks confirm_open counts)."""
        props = self.task_schema["properties"]
        bases, statuses = set(props["owner_basis"]["enum"]), set(props["status"]["enum"])
        for key in ("mine_owner_basis", "confirm_owner_basis"):
            unknown = set(self.task_cfg[key]) - bases
            if unknown:
                raise StoreError(f"kg.tasks.{key}: {sorted(unknown)} not in config/schema/task.json owner_basis "
                                 f"{sorted(bases)}")
        if self.task_cfg["tools_status"] not in statuses:
            raise StoreError(f"kg.tasks.tools_status {self.task_cfg['tools_status']!r} is not a task status "
                             f"{sorted(statuses)}")
        unknown = set(self.task_cfg["review_statuses"]) - statuses
        if unknown:
            raise StoreError(f"kg.tasks.review_statuses: {sorted(unknown)} not task statuses {sorted(statuses)}")
        outside = set(self.task_cfg["confirm_owner_basis"]) - set(self.task_cfg["mine_owner_basis"])
        if outside:
            raise StoreError(f"kg.tasks.confirm_owner_basis: {sorted(outside)} not in kg.tasks.mine_owner_basis")
        if self.intake["scope_entity_type"] not in self.entity_types:
            raise StoreError(f"tasks.intake.scope_entity_type {self.intake['scope_entity_type']!r} is not an entity "
                             f"type in {self.cfg['ontology']} {sorted(self.entity_types)}")

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
                (episode_id, transcript_path, sha256, utc_time(meeting_start), call_type, extractor_version,
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

    def live_id(self, entity_id: str) -> str:
        """The entity an id stands for now: itself, or the survivor its merges lead to
        (merged_into, followed to the end). An unknown id is returned as it is."""
        seen = {entity_id}
        while True:
            row = self.conn.execute("SELECT merged_into FROM entity WHERE id = ?", (entity_id,)).fetchone()
            if row is None or row[0] is None:
                return entity_id
            if row[0] in seen:
                raise StoreError(f"merge cycle at entity {entity_id!r}")
            entity_id = row[0]
            seen.add(entity_id)

    def aliases(self, entity_id: str) -> list[str]:
        """The names an entity goes by, each once (an attached mention and the entity's
        own alias row can carry the same text)."""
        return [r["alias"] for r in self.conn.execute(
            "SELECT DISTINCT alias, alias_key FROM alias WHERE entity_id = ? ORDER BY alias_key, alias",
            (entity_id,))]

    def entities_named(self, name: str) -> list[str]:
        """Live entities `name` exactly names (after name_key): an alias of theirs, or
        their canonical key (a person's email, any other type's name), each followed
        to its survivor, sorted. Empty when nothing matches; more than one when the
        name is shared. For callers that take a name where an id is expected (the
        viewer, kg/view.py); a key is looked up by id, an alias through its index."""
        key = name_key(name)
        if not key:
            return []
        keyed = [keyed_id(type_, key) for type_ in sorted(self.entity_types)]
        keyed.append(keyed_id(PERSON, NAME_KEY_PREFIX + key))
        found = {r[0] for r in self.conn.execute(
            f"SELECT id FROM entity WHERE id IN ({', '.join('?' for _ in keyed)})", keyed)}
        found |= {r[0] for r in self.conn.execute(
            "SELECT DISTINCT entity_id FROM alias WHERE alias_key = ? AND entity_id IS NOT NULL", (key,))}
        return sorted({self.live_id(entity_id) for entity_id in found})

    def unresolved_aliases(self) -> list[dict]:
        """Mentions waiting for C.5 entity resolution."""
        return fetch_all(self.conn, "SELECT * FROM alias WHERE entity_id IS NULL ORDER BY created_at")

    def _create_entity(self, type_: str, name: str, key: str, now: Optional[datetime]) -> str:
        """The entity for (type, key), created if new. A key whose entity was merged
        away returns the survivor, so later writes land on the live entity."""
        entity_id = keyed_id(type_, key)
        self.conn.execute(
            "INSERT INTO entity (id, type, canonical_name, canonical_key, merged_into, created_at)"
            " VALUES (?, ?, ?, ?, NULL, ?) ON CONFLICT (id) DO NOTHING",
            (entity_id, type_, name.strip(), key, utc_now(now)))
        return self.live_id(entity_id)

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
        """Live people `name` may stand for (after name_key). A full name matches an
        equal alias. A single word matches only the first word of a full-name alias:
        one-word aliases are ignored, so a first name once attached to the only person
        it fitted does not keep resolving to them after a namesake appears (lesson
        L12). An alias still on a merged-away person counts for its survivor."""
        key = name_key(name)
        if len(key.split()) > 1:
            where, params = "a.alias_key = ?", (key,)
        else:
            # Keys are whitespace-collapsed, so "word " < key < "word!" is exactly the
            # keys starting with "word " (no character sorts between space and "!").
            where, params = "a.alias_key > ? AND a.alias_key < ?", (key + " ", key + "!")
        ids = {self.live_id(r[0]) for r in self.conn.execute(
            f"SELECT DISTINCT e.id FROM entity e JOIN alias a ON a.entity_id = e.id WHERE e.type = ? AND {where}",
            (PERSON, *params))}
        return sorted(ids)

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
                 provenance(quote, transcript_text), confidence, utc_time(valid_from or ep["meeting_start"]),
                 utc_time(valid_to), utc_now(now)))
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
        # The same edge already stored, perhaps under the id of an endpoint since merged
        # away (a merge repoints the edge but keeps its id): re-writing an episode after
        # a merge must not add a second copy on the survivor.
        ends = (self.live_id(src_entity_id), self.live_id(dst_entity_id))
        for row in self.conn.execute("SELECT id, src_entity_id, dst_entity_id FROM edge WHERE episode_id = ?"
                                     " AND relation = ? AND quote = ?", (episode_id, relation, quote)):
            if (self.live_id(row[1]), self.live_id(row[2])) == ends:
                return row[0]
        edge_id = stable_id("edge", episode_id, src_entity_id, relation, dst_entity_id, quote)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO edge (id, src_entity_id, dst_entity_id, relation, quote, quote_start, episode_id,"
                " provenance, confidence, valid_from, valid_to, superseded_by, supersede_reason, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?) ON CONFLICT (id) DO NOTHING",
                (edge_id, src_entity_id, dst_entity_id, relation, quote, quote_start, episode_id,
                 provenance(quote, transcript_text), confidence, utc_time(valid_from or ep["meeting_start"]),
                 utc_time(valid_to), utc_now(now)))
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
                (new_id, reason, utc_time(new["valid_from"]) or utc_now(now), old_id))

    # ---- tasks ----------------------------------------------------------------------

    def add_task(self, task: dict, *, entity_ids: Iterable[str] = (), now: Optional[datetime] = None) -> str:
        """Insert a task from the D.5 contract, linked to entities. A known id is left
        as it is (its lifecycle lives here); new entity links are still added. It must
        come in at the funnel's first stage (captured): every later status is reached
        through update_task, so its checks, the ready gate first, can't be skipped."""
        errors = validate(task, self.task_schema)
        if errors:
            raise StoreError("task does not match config/schema/task.json:\n  " + "\n  ".join(errors))
        first = self.funnel_stages()[0]
        if task["status"] != first:
            raise StoreError(f"a task is inserted as {first!r}, not {task['status']!r}: later statuses go "
                             "through update_task")
        episode_id = task["source"]["episode"]
        if task["id"] not in (task_id(episode_id, task["quote"]), task_id(episode_id, task["quote"], task["action"])):
            raise StoreError(f"task id {task['id']!r} is neither sha1(episode + quote) nor, for a shared quote, "
                             "sha1(episode + quote + action) (task_id)")
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
                    note: Optional[str], stamp: str, *, actor: Optional[str] = None,
                    details: Optional[dict] = None) -> str:
        event_id = new_id()
        self.conn.execute("INSERT INTO task_event (id, task_id, from_status, to_status, note, at, actor, details)"
                          " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (event_id, task_id_, from_status, to_status, note, stamp, actor,
                           json.dumps(details) if details else None))
        return event_id

    def allowed_transitions(self, status: str) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT to_status FROM task_transition WHERE from_status = ?", (status,))}

    def set_task_status(self, task_id_: str, status: str, *, note: Optional[str] = None,
                        actor: Optional[str] = None, now: Optional[datetime] = None) -> None:
        """Move a task along an allowed transition, recording the event."""
        self.update_task(task_id_, status=status, reason=note, actor=actor, now=now)

    def update_task(self, task_id_: str, *, actor: Optional[str], status: Optional[str] = None,
                    reason: Optional[str] = None, clarification: Optional[str] = None,
                    inputs: Iterable[str] = (), tools_allowed: Optional[list] = None,
                    brief: Optional[dict] = None, scope: Optional[list] = None,
                    brief_source: Optional[str] = None, project: Optional[str] = None,
                    now: Optional[datetime] = None) -> dict:
        """One review change, all of it or none of it (the /create-tasks review, D.5):
        - `status`: move along an allowed transition (task_transition, migration 0001),
          recorded as a task_event with `actor` (who), `reason` (why) and the time;
          anything else raises TransitionError naming the allowed next states.
        - `tools_allowed`: the /execute-tasks worker's allowlist (D.3), granted only with
          a move to kg.tasks.tools_status; it must fit config/schema/task.json, and is
          kept on the task and in the event's details. Each marking declares its own
          list: marking that status without it grants none, so a task re-marked after
          its scope changed inherits no earlier grant.
        - `clarification` and `inputs` (paths, links, values the work needs): task_note
          rows by `actor`, with or without a status change.
        - `brief` (field -> answer, for fields of tasks.intake.fields other than the scope
          field; the budget field's answer an object, see `_budget`) and `scope` (the scope
          field's [{type, value}], see `_scope_items`): brief rows by `actor`, each with
          `brief_source` (tasks.intake.answer_sources), which they require.
        - `project`: link the task to this tasks.intake.scope_entity_type entity (its
          merge survivor), a task_entity row added if missing; never removes a link.
        - The ready gate: a move to kg.tasks.tools_status raises BriefOpenError while
          a required field (tasks.intake.required_fields) has no answer, this call's
          answers counted.
        The read, the checks and the writes share one write transaction (BEGIN IMMEDIATE
        first), so a change another process made meanwhile is never checked stale.
        Returns the task with its history (`task_record`). Never writes a file (L19)."""
        with transaction(self.conn):
            self._update_task(task_id_, actor=actor, status=status, reason=reason, clarification=clarification,
                              inputs=inputs, tools_allowed=tools_allowed, brief=brief, scope=scope,
                              brief_source=brief_source, project=project, now=now)
        return self.task_record(task_id_)

    @staticmethod
    def _known(name: object, known: Iterable[str], what: str) -> str:
        """`name` as `known` spells it, letter case ignored; anything else is a StoreError
        naming it and what is known."""
        known = list(known)
        if isinstance(name, str):
            for k in known:
                if k.casefold() == name.strip().casefold():
                    return k
        raise StoreError(f"unknown {what} {name!r}; known: {known}")

    def _scope_items(self, items: object, *, allow_empty: bool) -> list[dict]:
        """A scope checked and normalized: a list of {type, value}, each type one of
        tasks.intake.scope_types (as written there), each value a non-empty string, each
        pair once, in the order given. tasks.intake.scope_none stands alone in its type."""
        if not isinstance(items, (list, tuple)):
            raise StoreError("a scope is a list of {type, value} items")
        if not items and not allow_empty:
            raise StoreError(f"an empty scope is no answer; give {self.intake['scope_none']!r} as the value of "
                             "each source type with nothing in scope")
        none, found = self.intake["scope_none"], {}
        for i, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != {"type", "value"}:
                raise StoreError(f"scope[{i}] must be an object with exactly type and value")
            type_ = self._known(item["type"], self.intake["scope_types"], "scope type")
            value = item["value"]
            if not isinstance(value, str) or not value.strip():
                raise StoreError(f"scope[{i}].value must be a non-empty string")
            value = none if value.strip().casefold() == none.casefold() else value.strip()
            found.setdefault((type_, value), {"type": type_, "value": value})
        for type_ in self.intake["scope_types"]:
            values = [v for t, v in found if t == type_]
            if none in values and len(values) > 1:
                raise StoreError(f"scope type {type_!r}: {none!r} stands alone, not beside {values}")
        return list(found.values())

    def _budget(self, value: object) -> dict:
        """A budget answer checked: exactly tasks.intake.budget's amount key (a number of
        USD, at least amount_min_usd) and reason key (non-empty text)."""
        cfg = self.intake["budget"]
        amount_key, reason_key = cfg["amount_key"], cfg["reason_key"]
        if not isinstance(value, dict) or set(value) != {amount_key, reason_key}:
            raise StoreError(f"brief.{self.budget_field} must be an object with exactly {amount_key!r} and "
                             f"{reason_key!r}")
        amount, reason = value[amount_key], value[reason_key]
        if (isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount)
                or amount < cfg["amount_min_usd"]):
            raise StoreError(f"brief.{self.budget_field}.{amount_key} must be a number of USD, at least "
                             f"{cfg['amount_min_usd']}, not {amount!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise StoreError(f"brief.{self.budget_field}.{reason_key} must be a non-empty string: why that figure")
        return {amount_key: amount, reason_key: reason.strip()}

    def _brief_rows(self, brief: Optional[dict], scope: Optional[list]) -> list[tuple[str, str]]:
        """(field, text) for each answer given: `brief` keys as tasks.intake.fields spells
        them, values non-empty strings, or the budget field's as its JSON object; `scope`
        as the scope field's JSON list."""
        rows = []
        if brief is not None:
            if not isinstance(brief, dict) or not brief:
                raise StoreError("brief is an object of field -> answer, with at least one field")
            for name, value in brief.items():
                field = self._known(name, self.intake["fields"], "brief field")
                if field == self.scope_field:
                    raise StoreError(f"the {field!r} field is given as scope (a list of {{type, value}}), "
                                     "not in brief")
                if field == self.budget_field:
                    rows.append((field, json.dumps(self._budget(value), ensure_ascii=False)))
                    continue
                if not isinstance(value, str) or not value.strip():
                    raise StoreError(f"brief.{field} must be a non-empty string")
                rows.append((field, value.strip()))
        if scope is not None:
            rows.append((self.scope_field, json.dumps(self._scope_items(scope, allow_empty=False),
                                                       ensure_ascii=False)))
        if len({f for f, _ in rows}) != len(rows):
            raise StoreError(f"brief names a field twice: {[f for f, _ in rows]}")
        return rows

    def open_fields(self, task_id_: str, answering: Iterable[str] = ()) -> list[str]:
        """The required brief fields with no answer yet, besides those in `answering`,
        in tasks.intake.required_fields order. An answer is never empty, so any brief row
        answers its field."""
        answered = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT field FROM task_note WHERE task_id = ? AND kind = ?", (task_id_, NOTE_BRIEF))}
        answered |= set(answering)
        return [f for f in self.required_fields if f not in answered]

    def _update_task(self, task_id_: str, *, actor: Optional[str], status: Optional[str], reason: Optional[str],
                     clarification: Optional[str], inputs: Iterable[str], tools_allowed: Optional[list],
                     brief: Optional[dict], scope: Optional[list], brief_source: Optional[str],
                     project: Optional[str], now: Optional[datetime]) -> None:
        row = fetch_one(self.conn, "SELECT * FROM task WHERE id = ?", (task_id_,))
        if row is None:
            raise StoreError(f"no task {task_id_!r}")
        if isinstance(inputs, str):
            raise StoreError("inputs is a list of strings, not one string")
        inputs = list(inputs)
        texts = [("reason", reason), ("clarification", clarification), ("actor", actor)]
        texts += [(f"inputs[{i}]", text) for i, text in enumerate(inputs)]
        for name, text in texts:
            if text is not None and (not isinstance(text, str) or not text.strip()):
                raise StoreError(f"{name} must be a non-empty string")
        if (status is None and clarification is None and not inputs and tools_allowed is None
                and brief is None and scope is None and project is None):
            raise StoreError("nothing to change: give a status, a clarification, inputs, tools_allowed, "
                             "brief, scope or project")
        project_id = self._scope_entity(project)["id"] if project is not None else None
        if (clarification is not None or inputs or brief is not None or scope is not None) and actor is None:
            raise StoreError("a clarification, an input or a brief answer needs the actor who attached it")
        answers = self._brief_rows(brief, scope)
        if answers:
            if brief_source is None:
                raise StoreError(f"a brief answer needs brief_source, one of {self.intake['answer_sources']}")
            brief_source = self._known(brief_source, self.intake["answer_sources"], "brief_source")
        elif brief_source is not None:
            raise StoreError("brief_source is given only with brief or scope")
        current = row["status"]
        if status is not None and status not in self.allowed_transitions(current):
            raise TransitionError(task_id_, current, status, self.allowed_transitions(current))
        if status == self.task_cfg["tools_status"]:
            still_open = self.open_fields(task_id_, answering=[f for f, _ in answers])
            if still_open:
                raise BriefOpenError(task_id_, status, still_open)
        contract = self._contract(row)
        tools_status = self.task_cfg["tools_status"]
        tools = [] if status == tools_status else contract["tools_allowed"]       # each marking declares its own
        if tools_allowed is not None:
            if status != tools_status:
                raise StoreError(f"tools_allowed is granted only when a task is marked {tools_status}: "
                                 f"pass status {tools_status!r} with it")
            errors = validate(tools_allowed, self.task_schema["properties"]["tools_allowed"])
            if errors:
                raise StoreError("tools_allowed does not match config/schema/task.json:\n  " + "\n  ".join(errors))
            tools = list(dict.fromkeys(t.strip() for t in tools_allowed))
            if "" in tools:
                raise StoreError("tools_allowed has an empty tool name")
        errors = validate({**contract, "status": status or current, "tools_allowed": tools}, self.task_schema)
        if errors:
            raise StoreError("the changed task would not match config/schema/task.json:\n  " + "\n  ".join(errors))
        stamp = utc_now(now)
        with transaction(self.conn):
            self.conn.execute("UPDATE task SET status = ?, tools_allowed = ?, updated_at = ? WHERE id = ?",
                              (status or current, json.dumps(tools), stamp, task_id_))
            if status is not None:
                self._task_event(task_id_, current, status, reason, stamp, actor=actor,
                                 details={"tools_allowed": tools} if status == tools_status else None)
            notes = ([(NOTE_CLARIFICATION, clarification)] if clarification is not None else [])
            for kind, text in notes + [(NOTE_INPUT, text) for text in inputs]:
                self.conn.execute("INSERT INTO task_note (id, task_id, kind, text, actor, at) VALUES (?, ?, ?, ?, ?, ?)",
                                  (new_id(), task_id_, kind, text.strip(), actor, stamp))
            for field, text in answers:
                self.conn.execute("INSERT INTO task_note (id, task_id, kind, field, source, text, actor, at)"
                                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                  (new_id(), task_id_, NOTE_BRIEF, field, brief_source, text, actor, stamp))
            if project_id is not None:
                self.conn.execute("INSERT INTO task_entity (task_id, entity_id) VALUES (?, ?)"
                                  " ON CONFLICT (task_id, entity_id) DO NOTHING", (task_id_, project_id))

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

    def needs_confirm(self, task: dict) -> bool:
        """Whether a task is shown as "confirm?" (D.5): still at the funnel's first stage
        with an owner_basis in kg.tasks.confirm_owner_basis."""
        return task["status"] == self.funnel_stages()[0] and task["owner_basis"] in self.task_cfg["confirm_owner_basis"]

    def task_record(self, task_id_: str) -> Optional[dict]:
        """A task for review: its contract, whether it needs "confirm?", the meeting it
        came from, its status changes (who, why, when, details) and its clarifications
        and inputs, oldest first. None if no such task."""
        row = fetch_one(self.conn, "SELECT t.*, ep.meeting_start, ep.transcript_path FROM task t"
                                   " JOIN episode ep ON ep.id = t.episode_id WHERE t.id = ?", (task_id_,))
        if row is None:
            return None
        events = fetch_all(self.conn, "SELECT from_status, to_status, actor, note AS reason, details, at"
                                      " FROM task_event WHERE task_id = ? ORDER BY at, rowid", (task_id_,))
        for ev in events:
            ev["details"] = json.loads(ev["details"]) if ev["details"] else None
        notes = fetch_all(self.conn, "SELECT kind, text, actor, at FROM task_note WHERE task_id = ?"
                                     " ORDER BY at, rowid", (task_id_,))
        history = self.brief_history(task_id_)
        return {**self._review_card(row), "events": events,
                "clarifications": [n for n in notes if n["kind"] == NOTE_CLARIFICATION],
                "inputs": [n for n in notes if n["kind"] == NOTE_INPUT],
                "brief": {h["field"]: {k: v for k, v in h.items() if k != "field"} for h in history},
                "brief_history": history, "open_fields": self.open_fields(task_id_),
                "projects": self.task_projects(task_id_)}

    def task_projects(self, task_id_: str) -> list[dict]:
        """The tasks.intake.scope_entity_type entities a task is linked to (task_entity),
        each as its merge survivor, once: {id, name}."""
        found = {}
        for entity_id in self.task_entities(task_id_):
            ent = self.entity(self.live_id(entity_id))
            if ent is not None and ent["type"] == self.intake["scope_entity_type"]:
                found[ent["id"]] = {"id": ent["id"], "name": ent["canonical_name"]}
        return list(found.values())

    def brief_history(self, task_id_: str) -> list[dict]:
        """Every brief answer, oldest first (field, value, source, actor, at); the scope
        and budget fields' values as their list and object. Later rows win, so a dict of
        it is the brief now."""
        rows = fetch_all(self.conn, "SELECT field, text AS value, source, actor, at FROM task_note"
                                    " WHERE task_id = ? AND kind = ? ORDER BY at, rowid", (task_id_, NOTE_BRIEF))
        for r in rows:
            if r["field"] in (self.scope_field, self.budget_field):
                r["value"] = json.loads(r["value"])
        return rows

    # ---- project scope (entity_scope, migration 0007) ---------------------------------

    def _scope_entity(self, entity_id: str) -> dict:
        """The live entity `entity_id` stands for, which must be of
        tasks.intake.scope_entity_type: only those carry a default scope or a task's
        project link."""
        ent = self.entity(self.live_id(entity_id))
        if ent is None:
            raise StoreError(f"no entity {entity_id!r}")
        want = self.intake["scope_entity_type"]
        if ent["type"] != want:
            raise StoreError(f"entity {ent['id']!r} ({ent['canonical_name']}) is a {ent['type']}, not a {want}: "
                             f"only a {want} carries a default scope or a task's project link")
        return ent

    def entity_scope(self, entity_id: str) -> dict:
        """A project's default scope: its rows and those of every entity merged into it,
        by source type then value, and the source types with no row yet (`open_types`).
        Where the union holds tasks.intake.scope_none beside a real value of the same type
        (two merged projects disagreeing), the real values win, so the answer is always a
        scope `_scope_items` accepts."""
        ent = self._scope_entity(entity_id)
        scope = fetch_all(self.conn, _MERGE_GROUP + " SELECT DISTINCT source_type AS type, value FROM entity_scope"
                                                    " WHERE entity_id IN (SELECT id FROM grp)"
                                                    " ORDER BY source_type, value", (ent["id"],))
        none = self.intake["scope_none"]
        real = {s["type"] for s in scope if s["value"] != none}
        scope = [s for s in scope if s["value"] != none or s["type"] not in real]
        have = {s["type"] for s in scope}
        return {"entity_id": ent["id"], "name": ent["canonical_name"], "scope": scope,
                "open_types": [t for t in self.intake["scope_types"] if t not in have]}

    def set_entity_scope(self, entity_id: str, items: list, *, now: Optional[datetime] = None) -> dict:
        """Replace a project's default scope with `items` (checked as a task's scope; an
        empty list clears it), on the live entity: rows kept keep their created_at, the
        rest of the merge group's rows go. Returns `entity_scope`."""
        with transaction(self.conn):
            ent = self._scope_entity(entity_id)
            want = {(s["type"], s["value"]) for s in self._scope_items(items, allow_empty=True)}
            held = fetch_all(self.conn, _MERGE_GROUP + " SELECT entity_id, source_type, value FROM entity_scope"
                                                       " WHERE entity_id IN (SELECT id FROM grp)", (ent["id"],))
            for r in held:
                if r["entity_id"] != ent["id"] or (r["source_type"], r["value"]) not in want:
                    self.conn.execute("DELETE FROM entity_scope WHERE entity_id = ? AND source_type = ? AND value = ?",
                                      (r["entity_id"], r["source_type"], r["value"]))
            stamp = utc_now(now)
            for type_, value in sorted(want):
                self.conn.execute("INSERT INTO entity_scope (entity_id, source_type, value, created_at)"
                                  " VALUES (?, ?, ?, ?) ON CONFLICT (entity_id, source_type, value) DO NOTHING",
                                  (ent["id"], type_, value, stamp))
        return self.entity_scope(ent["id"])

    def _review_card(self, row: dict) -> dict:
        task = self._contract(row)
        return {"task": task, "confirm": self.needs_confirm(task), "meeting_start": row["meeting_start"],
                "transcript_path": row["transcript_path"], "created_at": row["created_at"]}

    def review_tasks(self, *, status: str, mine: bool = True, confirm_only: bool = False, limit: int,
                     offset: int = 0) -> dict:
        """Tasks in `status` for the review, "confirm?" ones first, then newest meeting
        first: the owner's own (kg.tasks.mine_owner_basis) unless `mine` is False. At
        most `limit` cards from `offset`, with the total and whether more remain."""
        if status not in self.task_schema["properties"]["status"]["enum"]:
            raise StoreError(f"no task status {status!r}")
        where, params = ["t.status = ?"], [status]
        if mine:
            clause, values = _in("t.owner_basis", self.task_cfg["mine_owner_basis"])
            where.append(clause)
            params += values
        confirm, confirm_params = _in("t.owner_basis", self.task_cfg["confirm_owner_basis"])
        if confirm_only:
            where.append(confirm)
            params += confirm_params
        sql = (" FROM task t JOIN episode ep ON ep.id = t.episode_id WHERE " + " AND ".join(where))
        total = self.conn.execute("SELECT COUNT(*)" + sql, params).fetchone()[0]
        rows = fetch_all(self.conn,
            f"SELECT t.*, ep.meeting_start, ep.transcript_path{sql}"
            f" ORDER BY CASE WHEN {confirm} THEN 0 ELSE 1 END, COALESCE(ep.meeting_start, t.created_at) DESC,"
            " t.source_start, t.id LIMIT ? OFFSET ?", (*params, *confirm_params, limit, offset))
        return {"status": status, "total": total, "offset": offset, "truncated": offset + len(rows) < total,
                "tasks": [self._review_card(r) for r in rows]}

    # ---- the funnel (D.5, lesson L20) -------------------------------------------------

    def funnel_stages(self) -> list[str]:
        """The funnel's stages in order (task_status.funnel_rank): captured first."""
        return [r[0] for r in self.conn.execute(
            "SELECT status FROM task_status WHERE funnel_rank IS NOT NULL ORDER BY funnel_rank")]

    @staticmethod
    def _task_scope(since: Optional[str], until: Optional[str],
                    owner_basis: Optional[Iterable[str]]) -> tuple[str, list]:
        where, params = ["1 = 1"], []
        if since is not None:
            where.append("t.created_at >= ?")
            params.append(utc_time(since))
        if until is not None:
            where.append("t.created_at < ?")
            params.append(utc_time(until))
        if owner_basis is not None:
            clause, values = _in("t.owner_basis", owner_basis)
            where.append(clause)
            params += values
        return " AND ".join(where), params

    def funnel(self, since: Optional[str] = None, *, until: Optional[str] = None,
               owner_basis: Optional[Iterable[str]] = None) -> dict[str, int]:
        """Funnel stage -> tasks that ever reached it (D.5: captured -> confirmed ->
        ready -> done), over tasks created at or after `since` and before `until` (ISO
        times) and with one of `owner_basis`, each if given. Every stored task was captured, whatever status it
        was inserted with."""
        stages = fetch_all(self.conn, "SELECT status, funnel_rank FROM task_status WHERE funnel_rank IS NOT NULL"
                                      " ORDER BY funnel_rank")
        where, params = self._task_scope(since, until, owner_basis)
        tops = [r[0] for r in self.conn.execute(
            "SELECT COALESCE(MAX(stage.funnel_rank), ?) FROM task t"
            " LEFT JOIN task_event ev ON ev.task_id = t.id"
            " LEFT JOIN task_status st ON st.status = ev.to_status"
            " LEFT JOIN task_status stage ON stage.status = st.reaches"
            f" WHERE {where} GROUP BY t.id", (stages[0]["funnel_rank"], *params))]
        return {s["status"]: sum(1 for top in tops if top >= s["funnel_rank"]) for s in stages}

    def task_status_counts(self, since: Optional[str] = None, *, until: Optional[str] = None,
                           owner_basis: Optional[Iterable[str]] = None) -> dict[str, int]:
        """Status -> tasks in it now (every status in config/schema/task.json, zeros
        included), over the same scope as `funnel`."""
        where, params = self._task_scope(since, until, owner_basis)
        counts = dict(self.conn.execute(f"SELECT t.status, COUNT(*) FROM task t WHERE {where} GROUP BY t.status",
                                        params).fetchall())
        return {s: counts.get(s, 0) for s in self.task_schema["properties"]["status"]["enum"]}

    def confirm_open(self) -> int:
        """Tasks still waiting at the funnel's first stage with a "confirm?" owner_basis,
        however old: the review's backlog."""
        clause, values = _in("t.owner_basis", self.task_cfg["confirm_owner_basis"])
        return self.conn.execute(f"SELECT COUNT(*) FROM task t WHERE t.status = ? AND {clause}",
                                 (self.funnel_stages()[0], *values)).fetchone()[0]

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
            if kind == _ALIAS_KIND:       # indexed by alias id (migration 0003): its entity, if resolved
                row = self.conn.execute("SELECT entity_id FROM alias WHERE id = ?", (ref_id,)).fetchone()
                card = self._entity_card(row[0]) if row is not None and row[0] is not None else None
            elif kind == _ENTITY_KIND:
                card = self._entity_card(ref_id)
            else:
                card = self._fact_card(ref_id)
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
            f"  WHERE f.subject_entity_id = e.id AND {ACTIVE_FACT}) AS facts"
            " FROM entity e WHERE e.id = ? AND e.merged_into IS NULL", (entity_id,))
        if row is None:
            return None
        return {"kind": _ENTITY_KIND, "id": row["id"], "type": row["type"], "name": row["canonical_name"],
                "facts": row["facts"]}

    def _fact_card(self, fact_id: str) -> Optional[dict]:
        row = fetch_one(self.conn, "SELECT f.*, ep.meeting_start FROM fact f"
                                   f" JOIN episode ep ON ep.id = f.episode_id WHERE f.id = ? AND {ACTIVE_FACT}",
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
            facts = fetch_all(self.conn, FACT_SELECT + f" WHERE f.subject_entity_id = ? AND {ACTIVE_FACT}"
                                                       " ORDER BY f.valid_from, f.id", (item_id,))
            edges = fetch_all(self.conn, EDGE_SELECT + f" WHERE (g.src_entity_id = ? OR g.dst_entity_id = ?)"
                                                       f" AND {ACTIVE_EDGE} ORDER BY g.valid_from, g.id",
                              (item_id, item_id))
            return {"kind": _ENTITY_KIND, "id": ent["id"], "type": ent["type"], "name": ent["canonical_name"],
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
