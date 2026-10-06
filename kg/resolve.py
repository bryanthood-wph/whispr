"""Entity resolution (C.5 step 1, pulled forward): candidates, "same entity?" decisions,
merge and unmerge.

- **Candidates** are deterministic and same-type only, from three signals:
  - *same email*: a person's email key, or an email-shaped alias, held by both;
  - *alias overlap*: a shared distinctive name. Any name of a non-person is
    distinctive; a person's is only when it has two or more words, because a first
    name alone proves nothing (two different "Chris" share it, lesson L12);
  - *near names*: distinctive names at most kg.er.max_edit_distance edits apart
    (Levenshtein, stdlib), catching transcription variants ("Priya Shaw" / "Priya Shah").
    Names whose numbers differ are never near: "Sprint 12" and "Sprint 13" are two
    things, and a model call per numbered pair would be spent for nothing.
  Only entities sharing a blocking key are compared, not every pair: an email, or the
  first kg.er.block_chars letters of a word of a distinctive name.
- **Certain or not.** A candidate is certain on a shared email, or on a shared full
  name between an email-keyed person and a name-keyed one (the person kg/store.py
  creates for this step to merge) when the email-keyed person attended a call the
  name-keyed one was named in (an alias recorded with that episode). Certain candidates merge with no model
  call. Every other one gets a typed "same entity?" decision: the models-roster role
  named by kg.models.resolve, prompt kg.er.prompt, output schema kg.er.schema, with
  each entity's names, emails and kg.er.context_items facts and edges. Only "same"
  merges; "unsure" is counted as escalated and left for review. A model decision is
  kept (`er_decision`, migration 0004) with a fingerprint of each side's names and
  email; the pair is skipped on later passes until either fingerprint changes. The model is reached
  only through the `ask` callable the caller passes (eval.ask.Ask's shape, bound to
  pipeline.calls / pipeline.models), so tests pass a fake and no model is ever
  imported here.
- **Unresolved mentions** (a first name with no single match) never become entities.
  One is attached to a person only when exactly one live person fits it now
  (Store.person_matches: a first name fits only the first word of a full name, so an
  attached first name never keeps resolving to that person once a namesake exists).
- **Merge and unmerge** run in one transaction each. A merge repoints the dropped
  entity's edges, facts, task links and aliases to the survivor, keeps the dropped
  name as an alias, sets `merged_into`, and writes an `entity_merge` undo record
  naming every row it changed; merging the same pair again returns the same record.
  `unmerge` restores exactly those rows. Undo is last-in first-out: a merge whose
  survivor was later merged away is undone only after that later merge. Both are
  logged with State.log_maintenance against a running run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Callable, Iterable, Optional

from kg.db import fetch_all, fetch_one, new_id, stable_id, transaction, utc_now
from kg.state import State
from kg.store import (ACTIVE_EDGE, ACTIVE_FACT, EDGE_SELECT, FACT_SELECT, NAME_KEY_PREFIX, PERSON, Store,
                      StoreError, name_key)
from pipeline import prompts
from pipeline.config import config_file
from pipeline.jsonschema_lite import validate

# The typed decision; must equal the schema's enum (checked when a Resolver is made).
SAME, DIFFERENT, UNSURE = "same", "different", "unsure"
# Who made a merge decision, as recorded in entity_merge.decision.
BASIS_RULE, BASIS_MODEL, BASIS_MANUAL = "rule", "model", "manual"
# maintenance_log check names.
CHECK_ATTACH, CHECK_RESOLVE, CHECK_MERGE, CHECK_UNMERGE = (
    "alias_attach", "entity_resolution", "entity_merge", "entity_unmerge")
# alias.source of a name a merge or an attach adds (migration 0001: outlook | extract | maintain).
SOURCE = "maintain"
_EMAIL_MARK = "@"
_NUMBER = re.compile(r"\d+")

# (role, prompt, schema) -> the schema-valid structured output.
Ask = Callable[[str, str, dict], dict]


def _email(entity: dict) -> Optional[str]:
    """A person's email key; None for a name-keyed person or any other type."""
    key = entity["canonical_key"]
    return key if entity["type"] == PERSON and not key.startswith(NAME_KEY_PREFIX) else None


def edit_distance(a: str, b: str, limit: Optional[int] = None) -> int:
    """Levenshtein distance between a and b. With `limit`, any distance above it comes
    back as limit + 1, and the work stops as soon as that is certain."""
    if a == b:
        return 0
    if limit is not None and abs(len(a) - len(b)) > limit:
        return limit + 1
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if limit is not None and min(current) > limit:
            return limit + 1
        previous = current
    distance = previous[-1]
    return distance if limit is None or distance <= limit else limit + 1


@dataclass(frozen=True)
class Candidate:
    a: str                        # the two entity ids, in sorted order
    b: str
    type: str
    signals: tuple[str, ...]      # why they were paired, e.g. "alias: jamie doe"
    certain: bool


@dataclass
class _Names:
    """What candidate generation compares for one live entity."""
    entity: dict
    emails: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)      # distinctive name keys


class Resolver:
    def __init__(self, store: Store, state: State):
        self.store = store
        self.state = state
        self.conn = store.conn
        cfg = store.cfg
        self.max_distance = cfg["kg"]["er"]["max_edit_distance"]
        self.block_chars = cfg["kg"]["er"]["block_chars"]
        self.context_items = cfg["kg"]["er"]["context_items"]
        self.prompt_file = cfg["kg"]["er"]["prompt"]
        with open(config_file(cfg["kg"]["er"]["schema"]), encoding="utf-8") as fh:
            self.schema = json.load(fh)
        self.role = cfg["kg"]["models"]["resolve"]
        if self.role not in cfg["models"]:
            raise StoreError(f"kg.models.resolve names role {self.role!r}, which is not in the models roster")
        decisions = set(self.schema["properties"]["decision"]["enum"])
        if decisions != {SAME, DIFFERENT, UNSURE}:
            raise StoreError(f"{cfg['kg']['er']['schema']} decisions {sorted(decisions)} differ from "
                             f"kg/resolve.py's {sorted({SAME, DIFFERENT, UNSURE})}")

    # ---- candidates -----------------------------------------------------------------

    @staticmethod
    def _distinctive(type_: str, key: str) -> bool:
        return type_ != PERSON or len(key.split()) > 1

    def _names(self) -> list[_Names]:
        """Every live entity with its emails and distinctive name keys."""
        by_id: dict[str, _Names] = {}
        for ent in fetch_all(self.conn, "SELECT * FROM entity WHERE merged_into IS NULL ORDER BY type, id"):
            names = by_id[ent["id"]] = _Names(ent)
            if _email(ent):
                names.emails.add(_email(ent))
            self._add_name(names, name_key(ent["canonical_name"]))
        for row in fetch_all(self.conn, "SELECT a.entity_id, a.alias_key FROM alias a"
                                        " JOIN entity e ON e.id = a.entity_id WHERE e.merged_into IS NULL"):
            self._add_name(by_id[row["entity_id"]], row["alias_key"])
        return list(by_id.values())

    def _add_name(self, names: _Names, key: str) -> None:
        if _EMAIL_MARK in key:
            names.emails.add(key)
        elif key and self._distinctive(names.entity["type"], key):
            names.names.add(key)

    def candidates(self) -> list[Candidate]:
        """Same-type pairs of live entities sharing an email or a distinctive name, or
        with names within kg.er.max_edit_distance edits; each pair once."""
        by_type: dict[str, list[_Names]] = {}
        for names in self._names():
            by_type.setdefault(names.entity["type"], []).append(names)
        found = []
        for type_, group in sorted(by_type.items()):
            # Blocking: only entities sharing a cheap key are compared, not every pair.
            blocks: dict[str, list[int]] = {}
            for i, names in enumerate(group):
                for key in self._blocks(names):
                    blocks.setdefault(key, []).append(i)
            for i, j in sorted({pair for members in blocks.values() for pair in combinations(members, 2)}):
                x, y = group[i], group[j]
                signals = [f"email: {e}" for e in sorted(x.emails & y.emails)]
                shared = x.names & y.names
                signals += [f"alias: {n}" for n in sorted(shared)]
                signals += self._near(x.names - shared, y.names - shared)
                if not signals:
                    continue
                a, b = sorted((x.entity["id"], y.entity["id"]))
                certain = bool(x.emails & y.emails) or (
                    type_ == PERSON and bool(shared) and not (x.emails and y.emails) and self._met(x, y))
                found.append(Candidate(a, b, type_, tuple(signals), certain))
        return found

    def _blocks(self, names: _Names) -> set[str]:
        """The keys an entity is blocked under: each email, and the first
        kg.er.block_chars letters of each word of each distinctive name. A shared email
        or name always shares a key; a near-name variant does unless every word of it
        differs within its first letters."""
        return names.emails | {word[:self.block_chars] for name in names.names for word in name.split()}

    def _met(self, x: _Names, y: _Names) -> bool:
        """Whether the email-keyed person of a pair attended (an alias recorded with that
        episode) an episode the other, name-keyed, person was named in: a shared full
        name alone is then certain. No email-keyed side: never."""
        keyed, named = (x, y) if _email(x.entity) else (y, x)
        if not _email(keyed.entity):
            return False
        me, other = keyed.entity["id"], named.entity["id"]
        return fetch_one(self.conn,
            "SELECT 1 AS met FROM alias WHERE entity_id = ? AND episode_id IN ("
            " SELECT episode_id FROM alias WHERE entity_id = ?"
            " UNION SELECT episode_id FROM edge WHERE src_entity_id = ? OR dst_entity_id = ?"
            " UNION SELECT episode_id FROM fact WHERE subject_entity_id = ?)",
            (me, other, other, other, other)) is not None

    def _near(self, xs: Iterable[str], ys: Iterable[str]) -> list[str]:
        signals = []
        for x in sorted(xs):
            for y in sorted(ys):
                if _NUMBER.findall(x) != _NUMBER.findall(y):
                    continue
                d = edit_distance(x, y, self.max_distance)
                if d <= self.max_distance:
                    signals.append(f"near names: {x!r} ~ {y!r} ({d} edit{'s' if d != 1 else ''})")
        return signals

    # ---- unresolved mentions --------------------------------------------------------

    def attach_unresolved(self, run_id: str, now: Optional[datetime] = None) -> list[tuple[str, str]]:
        """Attach each unresolved mention that now fits exactly one live person, by
        Store.person_matches (a first name fits only the first word of a full name);
        the rest stay unresolved. The attached alias records where the name was said;
        it never makes that first name resolve to this person later (L12). Returns the
        (alias id, entity id) pairs attached."""
        pending = self.store.unresolved_aliases()
        attached = []
        with transaction(self.conn):
            for alias in pending:
                people = self.store.person_matches(alias["alias_key"])
                if len(people) == 1:
                    (entity_id,) = people
                    self.conn.execute("UPDATE alias SET entity_id = ? WHERE id = ?", (entity_id, alias["id"]))
                    attached.append((alias["id"], entity_id))
            self.state.log_maintenance(run_id, CHECK_ATTACH, found=len(pending), repaired=len(attached),
                                       escalated=0, details=json.dumps({"attached": attached}), now=now)
        return attached

    # ---- decisions ------------------------------------------------------------------

    def context(self, entity_id: str) -> dict:
        """What the model sees of one entity: names, emails, and its latest active facts
        and edges with their quotes."""
        ent = self.store.entity(entity_id)
        if ent is None:
            raise StoreError(f"no entity {entity_id!r}")
        n = self.context_items
        facts = fetch_all(self.conn, f"{FACT_SELECT} WHERE f.subject_entity_id = ? AND {ACTIVE_FACT}"
                                     " ORDER BY f.valid_from DESC, f.id LIMIT ?", (entity_id, n))
        edges = fetch_all(self.conn, f"{EDGE_SELECT} WHERE (g.src_entity_id = ? OR g.dst_entity_id = ?)"
                                     f" AND {ACTIVE_EDGE} ORDER BY g.valid_from DESC, g.id LIMIT ?",
                          (entity_id, entity_id, n))
        email = _email(ent)
        return {"name": ent["canonical_name"], "type": ent["type"], "aliases": self.store.aliases(entity_id),
                "emails": [email] if email else [],
                "facts": [{"type": f["type"], "text": f["text"], "quote": f["quote"]} for f in facts],
                "edges": [{"relation": g["relation"], "src": g["src_name"], "dst": g["dst_name"], "quote": g["quote"]}
                          for g in edges]}

    def prompt(self, candidate: Candidate) -> str:
        values = {"ENTITY_TYPE": candidate.type, "SIGNALS": "\n".join(f"- {s}" for s in candidate.signals)}
        for label, entity_id in (("ENTITY_A", candidate.a), ("ENTITY_B", candidate.b)):
            values[label] = json.dumps(self.context(entity_id), ensure_ascii=False, indent=1)
        return prompts.render(prompts.load(self.prompt_file), values)

    def decide(self, candidate: Candidate, ask: Ask) -> dict:
        """The typed decision for a candidate: by rule when it is certain, else by the
        model through `ask`. Recorded with the merge it leads to."""
        signals = list(candidate.signals)
        if candidate.certain:
            return {"decision": SAME, "reason": "certain: " + "; ".join(signals), "basis": BASIS_RULE,
                    "signals": signals}
        out = ask(self.role, self.prompt(candidate), self.schema)
        errors = validate(out, self.schema)
        if errors:
            raise StoreError(f"{self.role} decision failed {self.prompt_file}'s schema: " + "; ".join(errors))
        return {**out, "basis": BASIS_MODEL, "role": self.role, "signals": signals}

    def survivor(self, a: str, b: str) -> tuple[str, str]:
        """(kept, dropped): an email-keyed person over a name-keyed one, then the older
        entity, then the lower id."""
        def rank(entity_id: str) -> tuple:
            ent = self.store.entity(entity_id)
            return ent["type"] == PERSON and _email(ent) is None, ent["created_at"], ent["id"]
        kept, dropped = sorted((a, b), key=rank)
        return kept, dropped

    def run(self, run_id: str, ask: Ask, now: Optional[datetime] = None) -> dict:
        """One resolution pass: attach mentions, then decide and merge every candidate."""
        attached = self.attach_unresolved(run_id, now=now)
        found = self.candidates()
        merged, unsure, different, skipped = [], [], 0, 0
        for candidate in found:
            a, b = self.store.live_id(candidate.a), self.store.live_id(candidate.b)
            if a == b:                    # an earlier merge in this pass already joined them
                continue
            candidate = Candidate(*sorted((a, b)), candidate.type, candidate.signals, candidate.certain)
            prints = (self._fingerprint(candidate.a), self._fingerprint(candidate.b))
            if not candidate.certain and self._decided(candidate, prints):
                skipped += 1              # asked before, and neither side's names or email changed
                continue
            decision = self.decide(candidate, ask)
            if decision["decision"] == SAME:
                kept, dropped = self.survivor(a, b)
                merged.append(self.merge(kept, dropped, reason=decision["reason"], run_id=run_id,
                                         decision=decision, now=now))
            elif decision["decision"] == UNSURE:
                unsure.append([a, b])
            else:
                different += 1
            if decision["basis"] == BASIS_MODEL:
                self._record(candidate, decision, prints, now)
        summary = {"attached": len(attached), "candidates": len(found), "merged": merged,
                   "different": different, "unsure": unsure, "skipped": skipped}
        self.state.log_maintenance(run_id, CHECK_RESOLVE, found=len(found), repaired=len(merged),
                                   escalated=len(unsure), details=json.dumps(summary), now=now)
        return summary

    def _fingerprint(self, entity_id: str) -> str:
        """What a "same entity?" decision about an entity rests on: its names and its
        key (a person's email). A stored decision stands until either side's changes."""
        ent = self.store.entity(entity_id)
        keys = {ent["canonical_key"], name_key(ent["canonical_name"]), *map(name_key, self.store.aliases(entity_id))}
        return stable_id(*sorted(keys))

    def _decided(self, candidate: Candidate, prints: tuple[str, str]) -> bool:
        row = fetch_one(self.conn, "SELECT a_hash, b_hash FROM er_decision WHERE a_id = ? AND b_id = ?",
                        (candidate.a, candidate.b))
        return row is not None and (row["a_hash"], row["b_hash"]) == prints

    def _record(self, candidate: Candidate, decision: dict, prints: tuple[str, str],
                now: Optional[datetime]) -> None:
        """Keep a model decision (migration 0004), so the pair is not asked again while
        both sides' fingerprints stay the same."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO er_decision (a_id, b_id, decision, reason, a_hash, b_hash, decided_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (a_id, b_id) DO UPDATE SET decision = excluded.decision,"
                " reason = excluded.reason, a_hash = excluded.a_hash, b_hash = excluded.b_hash,"
                " decided_at = excluded.decided_at",
                (candidate.a, candidate.b, decision["decision"], decision.get("reason"), *prints, utc_now(now)))

    # ---- merge and unmerge ----------------------------------------------------------

    def merge(self, keep_id: str, drop_id: str, *, reason: str, run_id: str, decision: Optional[dict] = None,
              now: Optional[datetime] = None) -> str:
        """Merge `drop_id` into `keep_id` (the survivor); returns the entity_merge id.
        Merging a pair that is already merged this way returns its record unchanged."""
        if not reason.strip():
            raise StoreError("a merge needs a reason")
        keep, drop = self.store.entity(keep_id), self.store.entity(drop_id)
        if keep is None or drop is None:
            raise StoreError(f"no entity {keep_id if keep is None else drop_id!r}")
        if drop["merged_into"] == keep_id:
            done = fetch_one(self.conn, "SELECT id FROM entity_merge WHERE kept_id = ? AND merged_id = ?"
                                        " AND undone_at IS NULL ORDER BY merged_at DESC, id LIMIT 1",
                             (keep_id, drop_id))
            if done is not None:
                return done["id"]
        if keep_id == drop_id or keep["merged_into"] or drop["merged_into"]:
            raise StoreError(f"only two different live entities merge ({keep_id!r}, {drop_id!r})")
        if keep["type"] != drop["type"]:
            raise StoreError(f"a {drop['type']} cannot merge into a {keep['type']}")
        merge_id = new_id()
        with transaction(self.conn):
            undo = {"edge_src": self._ids("SELECT id FROM edge WHERE src_entity_id = ?", drop_id),
                    "edge_dst": self._ids("SELECT id FROM edge WHERE dst_entity_id = ?", drop_id),
                    "fact_subject": self._ids("SELECT id FROM fact WHERE subject_entity_id = ?", drop_id),
                    # A task linked to both keeps its survivor link; the dropped one's goes.
                    "task_moved": self._ids("SELECT task_id FROM task_entity WHERE entity_id = ? AND task_id"
                                            " NOT IN (SELECT task_id FROM task_entity WHERE entity_id = ?)",
                                            drop_id, keep_id),
                    "task_dropped": self._ids("SELECT task_id FROM task_entity WHERE entity_id = ? AND task_id"
                                              " IN (SELECT task_id FROM task_entity WHERE entity_id = ?)",
                                              drop_id, keep_id),
                    "alias": self._ids("SELECT id FROM alias WHERE entity_id = ?", drop_id),
                    "alias_added": []}
            self._set("UPDATE edge SET src_entity_id = ? WHERE id = ?", keep_id, undo["edge_src"])
            self._set("UPDATE edge SET dst_entity_id = ? WHERE id = ?", keep_id, undo["edge_dst"])
            self._set("UPDATE fact SET subject_entity_id = ? WHERE id = ?", keep_id, undo["fact_subject"])
            self.conn.executemany("UPDATE task_entity SET entity_id = ? WHERE task_id = ? AND entity_id = ?",
                                  [(keep_id, t, drop_id) for t in undo["task_moved"]])
            self.conn.executemany("DELETE FROM task_entity WHERE task_id = ? AND entity_id = ?",
                                  [(t, drop_id) for t in undo["task_dropped"]])
            self._set("UPDATE alias SET entity_id = ? WHERE id = ?", keep_id, undo["alias"])
            # The dropped entity's own name stays findable, as an alias of the survivor.
            if name_key(drop["canonical_name"]) not in {name_key(a) for a in self.store.aliases(keep_id)}:
                undo["alias_added"].append(self.store.add_alias(keep_id, drop["canonical_name"], source=SOURCE,
                                                                now=now))
            self.conn.execute("UPDATE entity SET merged_into = ? WHERE id = ?", (keep_id, drop_id))
            record = {"reason": reason, **(decision or {"basis": BASIS_MANUAL})}
            self.conn.execute(
                "INSERT INTO entity_merge (id, kept_id, merged_id, decision, undo, run_id, merged_at, undone_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (merge_id, keep_id, drop_id, json.dumps(record), json.dumps(undo), run_id, utc_now(now)))
            self.state.log_maintenance(run_id, CHECK_MERGE, found=1, repaired=1, escalated=0, now=now,
                                       details=json.dumps({"merge_id": merge_id, "kept": keep_id,
                                                           "merged": drop_id, "reason": reason}))
        return merge_id

    def unmerge(self, merge_id: str, *, run_id: str, now: Optional[datetime] = None) -> bool:
        """Reverse a merge exactly, from its undo record. False if it was already undone."""
        rec = fetch_one(self.conn, "SELECT * FROM entity_merge WHERE id = ?", (merge_id,))
        if rec is None:
            raise StoreError(f"no merge {merge_id!r}")
        if rec["undone_at"] is not None:
            return False
        kept, merged = rec["kept_id"], rec["merged_id"]
        later = fetch_one(self.conn, "SELECT id FROM entity_merge WHERE merged_id = ? AND undone_at IS NULL"
                                     " AND id <> ?", (kept, merge_id))
        if later is not None:
            raise StoreError(f"merge {merge_id!r}'s survivor {kept!r} was merged away later (merge "
                             f"{later['id']!r}); undo that one first")
        undo = json.loads(rec["undo"])
        with transaction(self.conn):
            self._set("UPDATE edge SET src_entity_id = ? WHERE id = ?", merged, undo["edge_src"])
            self._set("UPDATE edge SET dst_entity_id = ? WHERE id = ?", merged, undo["edge_dst"])
            self._set("UPDATE fact SET subject_entity_id = ? WHERE id = ?", merged, undo["fact_subject"])
            self.conn.executemany("UPDATE task_entity SET entity_id = ? WHERE task_id = ? AND entity_id = ?",
                                  [(merged, t, kept) for t in undo["task_moved"]])
            self.conn.executemany("INSERT INTO task_entity (task_id, entity_id) VALUES (?, ?)",
                                  [(t, merged) for t in undo["task_dropped"]])
            self._set("UPDATE alias SET entity_id = ? WHERE id = ?", merged, undo["alias"])
            self.conn.executemany("DELETE FROM alias WHERE id = ?", [(a,) for a in undo["alias_added"]])
            self.conn.execute("UPDATE entity SET merged_into = NULL WHERE id = ?", (merged,))
            self.conn.execute("UPDATE entity_merge SET undone_at = ? WHERE id = ?", (utc_now(now), merge_id))
            self.state.log_maintenance(run_id, CHECK_UNMERGE, found=1, repaired=1, escalated=0, now=now,
                                       details=json.dumps({"merge_id": merge_id, "kept": kept, "merged": merged}))
        return True

    def _ids(self, sql: str, *params: str) -> list[str]:
        return [r[0] for r in self.conn.execute(sql + " ORDER BY 1", params)]

    def _set(self, sql: str, value: str, ids: list[str]) -> None:
        """Run an `UPDATE ... SET col = ? WHERE id = ?` for each id."""
        self.conn.executemany(sql, [(value, row_id) for row_id in ids])
