"""The write stage: a validated extract document -> graph rows + a summary note
(docs/plan/D-architecture-and-ops.md D.1, D.5). Deterministic code, never the model (L18).

- **Episode.** One per transcript; its id is the transcript's file stem, which the
  recorder sets once from the call's start and title. Re-writing a changed transcript
  therefore updates the same episode (new sha256), and a task whose quote is unchanged
  keeps its id, so its lifecycle survives (L19). Every write is idempotent: rows are
  keyed by content (kg/store.py), so writing the same document twice adds nothing.
- **People.** The recording owner is a person keyed by `owner.email`; Outlook attendees
  and the organizer, and the document's person entities, are mentions resolved by
  alias (a lone first name with no single match stays an unresolved alias, L12).
- **Facts and edges** carry their quotes; the store marks each EXTRACTED only if the
  quote is verbatim in the prepared transcript the model saw, else AMBIGUOUS. An edge
  whose endpoints aren't known entities, or whose relation doesn't allow their types,
  is skipped and counted.
- **Tasks** are stored as `captured` in the D.5 contract. A my-task is marked
  "confirm?" (stored with owner_basis `unclear`, the ontology's "can't be settled")
  when the model says ownership is unclear, when its quote matches a Me line removed
  as echo (D.4), or when it has no quote. Nothing is dropped: a task's id is
  sha1(episode + quote) while its quote is unique among the document's tasks; tasks
  sharing one quote each add their normalized action to it, so each keeps its own row.
- **Every transcript gets a note** with a My Actions section (D.1): a summary, a stub
  ("no content", My Actions "None"), or a quarantined item ("Unavailable: extraction
  failed (see alert)"). Low mic coverage adds a warning. Notes are written atomically,
  after the graph commits, into pipeline.files.notes; lifecycle state never goes in
  them (L19).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

from kg.db import transaction
from kg.store import EXTRACTED, PERSON, Store, StoreError, name_key, provenance, task_id
from pipeline import render
from pipeline.config import data_dir
from pipeline.prepare import (Prepared, PrepareError, Transcript, check_encoding, file_sha256, owner_name, parse,
                              rederive_meta, word_tokens)
from whispr.fileio import atomic_write_text

# Note kinds, recorded in each note's frontmatter.
SUMMARY, NO_CONTENT, UNAVAILABLE = "summary", "no-content", "unavailable"
# The note lines D.1 and D.4 specify.
NO_CONTENT_LINE = "No content: the call was too short to summarize, so no model saw it."
UNAVAILABLE_LINE = "Unavailable: extraction failed (see alert)"
LOW_MIC_LINE = "may be incomplete: low mic coverage"
SOURCE_LABEL = "Source"

CAPTURED = "captured"           # D.5: write stores every task as captured
UNCLEAR = "unclear"             # owner_basis shown as "confirm?" (config/ontology.yaml)
OTHERS = "others"               # owner_basis of every task in other_tasks
# alias.source values: where a name came from.
FROM_CONFIG, FROM_OUTLOOK, FROM_EXTRACT = "config", "outlook", "extract"


@dataclass
class Written:
    episode_id: str
    kind: str
    note: Path
    entities: int = 0
    facts: int = 0
    edges: int = 0
    edges_skipped: int = 0
    tasks: int = 0
    confirm: int = 0
    low_mic: bool = False


def episode_id(path: Path) -> str:
    """A transcript's episode id: its file stem (also its queue item's ref)."""
    return Path(path).stem


def note_path(cfg: dict, episode: str) -> Path:
    return data_dir(cfg, cfg["pipeline"]["files"]["notes"]) / f"{episode}.md"


def mic_coverage(meta: dict) -> Optional[float]:
    """The transcript's recorded mic coverage (D.6), or None when it records none."""
    value = meta.get("mic_coverage")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_low_mic(cfg: dict, coverage: Optional[float]) -> bool:
    return coverage is not None and coverage < cfg["pipeline"]["write"]["low_mic_coverage"]


def echo_quoted(quote: str, prep: Prepared) -> bool:
    """True when the quote's words run, in order, inside a line removed as echo (D.4)."""
    words = " ".join(word_tokens(quote))
    return bool(words) and any(f" {words} " in f" {' '.join(word_tokens(t.text))} " for t in prep.echo_dropped)


def needs_confirm(item: dict, prep: Prepared, *, mine: bool) -> bool:
    """A my-task whose ownership the transcript can't settle (D.5)."""
    return mine and (item["owner_basis"] == UNCLEAR or not item["quote"].strip() or echo_quoted(item["quote"], prep))


# ---- graph ----------------------------------------------------------------------------

def _people(store: Store, cfg: dict, episode: str, meta: dict) -> dict[str, str]:
    """name_key -> entity id for the owner and the resolved attendees and organizer."""
    owner = owner_name(cfg)
    names = {name_key(owner): store.upsert_person(cfg["owner"]["email"], owner, source=FROM_CONFIG,
                                                  episode_id=episode)}
    for person in [meta["organizer"], *meta["attendees"]]:
        if person and name_key(person):
            found = store.mention_person(person, source=FROM_OUTLOOK, episode_id=episode)
            if found:
                names[name_key(person)] = found
    return names


def _entities(store: Store, doc: dict, episode: str, names: dict[str, str]) -> int:
    """Store the document's entities; add every resolved name and alias to `names`."""
    stored = 0
    for e in doc["entities"]:
        if not name_key(e["name"]):
            continue
        aliases = [a for a in e["aliases"] if name_key(a)]
        if e["type"] == PERSON:
            found = store.mention_person(e["name"], source=FROM_EXTRACT, episode_id=episode)
            for alias in aliases if found else []:
                store.add_alias(found, alias, source=FROM_EXTRACT, episode_id=episode)
        else:
            found = store.upsert_entity(e["type"], e["name"], source=FROM_EXTRACT, aliases=aliases,
                                        episode_id=episode)
        if found:
            stored += 1
            names.update({name_key(n): found for n in (e["name"], *aliases)})
    return stored


def task_rows(cfg: dict, episode: str, doc: dict, prep: Prepared, names: dict[str, str],
              owner_id: str) -> list[tuple[dict, list[str]]]:
    """(D.5 task, entity ids to link) for every task in the document, my actions first."""
    text = prep.render()
    confidence = cfg["pipeline"]["write"]["task_confidence"]
    tasks = []                                  # (item, mine, action, quote); none without an action or a quote
    for key, mine in render.TASK_LISTS:
        for item in doc[key]:
            action = item["action"].strip() or item["quote"].strip()
            if action:
                tasks.append((item, mine, action, item["quote"] if item["quote"].strip() else action))
    # Task identity (D.5): the id is sha1(episode + quote) when the quote is unique among
    # this document's tasks, so a rewrite keeps it and its lifecycle (L19). Only when two
    # or more tasks share one quote (two dues in one sentence, a my-task and someone
    # else's) does each id add the normalized action (kg.store.task_id), or one would be lost.
    shared = {quote for quote, n in Counter(quote for *_, quote in tasks).items() if n > 1}
    rows: dict[str, tuple[dict, list[str]]] = {}
    for item, mine, action, quote in tasks:
        row_id = task_id(episode, quote, action if quote in shared else None)
        if row_id in rows:
            continue                            # the same task twice: same quote, same action
        owner = owner_name(cfg) if mine else item["owner"]
        basis = (UNCLEAR if needs_confirm(item, prep, mine=True) else item["owner_basis"]) if mine else OTHERS
        linked = owner_id if mine else names.get(name_key(owner or ""))
        rows[row_id] = ({
            "id": row_id, "owner": owner, "owner_basis": basis, "action": action,
            "due": item["due"]["text"], "due_basis": item["due"]["basis"], "context": item["context"],
            "quote": quote, "source": {"episode": episode, "start": item["start"]},
            "confidence": confidence["verbatim" if provenance(quote, text) == EXTRACTED else "unverified"],
            "status": CAPTURED, "tools_allowed": [],
        }, [linked] if linked else [])
    return list(rows.values())


def _graph(store: Store, cfg: dict, episode: str, doc: dict, prep: Prepared, out: Written) -> None:
    text = prep.render()
    names = _people(store, cfg, episode, prep.meta)
    owner_id = names[name_key(owner_name(cfg))]
    out.entities = _entities(store, doc, episode, names)
    for f in doc["facts"]:
        store.add_fact(type_=f["type"], text=f["text"], quote=f["quote"], episode_id=episode, transcript_text=text,
                       subject_entity_id=names.get(name_key(f["subject"] or "")), quote_start=f["start"])
        out.facts += 1
    for g in doc["edges"]:
        src, dst = names.get(name_key(g["src"])), names.get(name_key(g["dst"]))
        try:
            if not (src and dst):
                raise StoreError("endpoint is not a known entity")
            store.add_edge(src_entity_id=src, dst_entity_id=dst, relation=g["relation"], quote=g["quote"],
                           episode_id=episode, transcript_text=text, quote_start=g["start"])
            out.edges += 1
        except StoreError:                      # raised before the store writes anything
            out.edges_skipped += 1
    for row, linked in task_rows(cfg, episode, doc, prep, names, owner_id):
        store.add_task(row, entity_ids=linked)
        out.tasks += 1
        out.confirm += int(row["owner_basis"] == UNCLEAR)


def _episode(store: Store, t: Transcript, meta: dict, extractor_version: Optional[str]) -> str:
    episode = episode_id(t.path)
    store.upsert_episode(episode, transcript_path=str(t.path), sha256=t.sha256, meeting_start=meta.get("start") or None,
                         call_type=meta.get("call_type"), extractor_version=extractor_version,
                         mic_coverage=mic_coverage(t.meta), output_device=meta.get("output_device"))
    return episode


# ---- note -----------------------------------------------------------------------------

def _note(cfg: dict, out: Written, t: Transcript, meta: dict, body: str) -> None:
    front = {"episode": out.episode_id, "note": out.kind, "title": meta.get("call_title"),
             "date": meta.get("date") or None, "call_type": meta.get("call_type"),
             "transcript": str(t.path), "sha256": t.sha256}
    title = meta.get("call_title") or out.episode_id
    warning = [f"> {LOW_MIC_LINE}"] if out.low_mic else []
    text = "\n\n".join(["---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---",
                        f"# {title}", *warning, body.rstrip("\n"), f"{SOURCE_LABEL}: `{t.path}`"]) + "\n"
    check_encoding([text], cfg["prepare"]["mojibake_markers"])     # L9: never write mojibake
    atomic_write_text(out.note, text)


def _strings(value: Any) -> Iterator[str]:
    """Every string in a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _written(cfg: dict, t: Transcript, kind: str) -> Written:
    episode = episode_id(t.path)
    return Written(episode, kind, note_path(cfg, episode), low_mic=is_low_mic(cfg, mic_coverage(t.meta)))


def write_summary(store: Store, cfg: dict, t: Transcript, prep: Prepared, doc: dict, *,
                  extractor_version: str) -> Written:
    """Graph rows for one extracted episode, in one transaction, then its note."""
    out = _written(cfg, t, SUMMARY)
    check_encoding(list(_strings(doc)), cfg["prepare"]["mojibake_markers"])    # L9: before any row commits
    with transaction(store.conn):
        _episode(store, t, prep.meta, extractor_version)
        _graph(store, cfg, out.episode_id, doc, prep, out)
    mine = {id(item) for item in doc["my_actions"]}
    _note(cfg, out, t, prep.meta, render.note(doc, confirm=lambda item: needs_confirm(item, prep, mine=id(item) in mine)))
    return out


def write_stub(store: Store, cfg: dict, t: Transcript, prep: Prepared) -> Written:
    """A stub (L25): its episode, no model output, a "no content" note."""
    out = _written(cfg, t, NO_CONTENT)
    _episode(store, t, prep.meta, None)
    _note(cfg, out, t, prep.meta, "\n\n".join([render.my_actions_block([]), NO_CONTENT_LINE]))
    return out


def write_unavailable(store: Store, cfg: dict, path: Path) -> Optional[Written]:
    """A quarantined transcript's episode and its "Unavailable" note, so discovery
    leaves it alone until it changes. None when the file is gone (nothing to note)."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        t = parse(path)
        meta: dict[str, Any] = rederive_meta(t.meta, cfg)
    except Exception:                           # the reason it was quarantined may be here
        t, meta = Transcript(path, file_sha256(path), {}, []), {}
    out = _written(cfg, t, UNAVAILABLE)
    _episode(store, t, meta, None)
    body = render.my_actions_block([UNAVAILABLE_LINE])
    try:
        _note(cfg, out, t, meta, body)
    except PrepareError:                        # mojibake in its metadata: a note without it
        _note(cfg, out, t, {}, body)
    return out
