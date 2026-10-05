"""The prepare step: deterministic cleaning before any model sees a transcript
(docs/plan/D-architecture-and-ops.md D.4). Every threshold comes from `prepare.*`.

Steps, in order: parse -> encoding check (L9) -> redaction (L10) -> metadata
re-derivation (L15) -> alias rewrite (L11) -> echo removal (L4) -> stub gate (L25).
Episode merge (L13) groups transcripts before preparing them as one.

Calendar re-matching (skipping reminder, all-day and single-attendee items) needs
Outlook and belongs to the recorder's metadata step; it is not done here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

_TURN = re.compile(r"^\*\*\[(\d+):(\d\d):(\d\d)\] (Me|Others):\*\* ?(.*)$")
_WORD = re.compile(r"[a-z0-9']+")


class PrepareError(ValueError):
    pass


@dataclass
class Turn:
    seconds: float
    speaker: str
    text: str

    @property
    def stamp(self) -> str:
        s = int(self.seconds)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


@dataclass
class Transcript:
    path: Path
    sha256: str
    meta: dict[str, Any]
    turns: list[Turn]


@dataclass
class Prepared:
    sources: list[str]
    meta: dict[str, Any]
    turns: list[Turn]
    is_stub: bool
    words: int
    echo_dropped: list[Turn] = field(default_factory=list)
    redactions: int = 0
    aliases_rewritten: int = 0
    key: str = ""

    def render(self) -> str:
        """Transcript text as sent to the model."""
        return "\n".join(f"[{t.stamp}] {t.speaker}: {t.text}" for t in self.turns)


def parse(path: Path) -> Transcript:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise PrepareError(f"{path}: no frontmatter")
    try:
        _, front, body = text.split("---\n", 2)
    except ValueError as exc:
        raise PrepareError(f"{path}: unterminated frontmatter") from exc
    meta = yaml.safe_load(front) or {}
    turns = []
    for line in body.splitlines():
        m = _TURN.match(line.strip())
        if m:
            h, mi, s, who, said = m.groups()
            turns.append(Turn(int(h) * 3600 + int(mi) * 60 + int(s), who, said.strip()))
    return Transcript(Path(path), hashlib.sha256(raw).hexdigest(), meta, turns)


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def check_encoding(texts: list[str], markers: list[str]) -> None:
    for text in texts:
        for marker in markers:
            if marker in text:
                raise PrepareError(f"mojibake marker {marker!r} found: {text[:80]!r}")


def redact(text: str, patterns: list[re.Pattern], token: str) -> tuple[str, int]:
    count = 0
    for rx in patterns:
        text, n = rx.subn(token, text)
        count += n
    return text, count


def _person(name: str) -> str:
    """'Last, First' -> 'First Last'; anything else unchanged."""
    parts = [p.strip() for p in name.split(",")]
    return f"{parts[1]} {parts[0]}" if len(parts) == 2 and all(parts) else name.strip()


def rederive_meta(meta: dict, cfg: dict) -> dict:
    """Metadata a model may see: no invite text, people only, no generic titles."""
    p = cfg["prepare"]
    rejects = [re.compile(x) for x in p["metadata_reject_patterns"]]
    tenant = cfg["owner"].get("tenant")
    if tenant:
        rejects.append(re.compile(re.escape(tenant), re.IGNORECASE))

    def clean(value: Any) -> Optional[str]:
        if not isinstance(value, str) or not value.strip():
            return None
        return None if any(rx.search(value.strip()) for rx in rejects) else value.strip()

    def clean_title(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        sep = p["title_segment_separator"]
        kept = [s for s in (clean(seg) for seg in value.split(sep)) if s]
        return sep.join(kept) or None

    attendees = []
    for raw in meta.get("attendees") or []:
        name = clean(raw)
        if name and _person(name) not in attendees:
            attendees.append(_person(name))
    organizer = clean(meta.get("organizer"))
    return {
        "call_title": clean_title(meta.get("call_title")),
        "date": str(meta.get("date") or ""),
        "call_type": "meeting" if meta.get("metadata_source") == "outlook" else "call",
        "organizer": _person(organizer) if organizer else None,
        "attendees": attendees,
        "start": str(meta.get("start") or ""),
        "end": str(meta.get("end") or ""),
        "output_device": meta.get("output_device"),
    }


def load_aliases(path: Optional[Path]) -> dict[str, list[str]]:
    """canonical -> [ASR variants]. The table lives in the user's data dir."""
    if not path or not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        table = yaml.safe_load(fh) or {}
    if not isinstance(table, dict) or not all(isinstance(v, list) for v in table.values()):
        raise PrepareError(f"{path}: alias table must map a canonical name to a list of variants")
    return table


def rewrite_aliases(text: str, aliases: dict[str, list[str]]) -> tuple[str, int]:
    count = 0
    for canonical, variants in aliases.items():
        for variant in variants:
            text, n = re.subn(rf"\b{re.escape(variant)}\b", canonical, text, flags=re.IGNORECASE)
            count += n
    return text, count


def remove_echo(turns: list[Turn], window_s: float, overlap: float, min_words: int) -> tuple[list[Turn], list[Turn]]:
    """Drop Me lines that repeat a nearby Others line (laptop speakers bleeding into the mic)."""
    others = [(t.seconds, set(_words(t.text))) for t in turns if t.speaker == "Others"]
    kept, dropped = [], []
    for t in turns:
        mine = set(_words(t.text))
        if t.speaker == "Me" and len(_words(t.text)) >= min_words and any(
            abs(t.seconds - s) <= window_s and len(mine & ws) / len(mine) >= overlap for s, ws in others
        ):
            dropped.append(t)
        else:
            kept.append(t)
    return kept, dropped


def _iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def group_episodes(transcripts: list[Transcript], cfg: dict) -> list[list[Transcript]]:
    """Merge split recordings of one meeting: same date and same real title, with a
    gap under prepare.merge_gap_min. A transcript with no usable title stays alone."""
    gap_s = cfg["prepare"]["merge_gap_min"] * 60
    ordered = sorted(transcripts, key=lambda t: str(t.meta.get("start") or ""))
    groups: list[list[Transcript]] = []
    for t in ordered:
        title = rederive_meta(t.meta, cfg)["call_title"]
        prev = groups[-1][-1] if groups else None
        if prev is not None and title:
            prev_title = rederive_meta(prev.meta, cfg)["call_title"]
            prev_end, start = _iso(str(prev.meta.get("end"))), _iso(str(t.meta.get("start")))
            if (prev_title and prev_title.lower() == title.lower()
                    and str(prev.meta.get("date")) == str(t.meta.get("date"))
                    and prev_end and start and 0 <= (start - prev_end).total_seconds() < gap_s):
                groups[-1].append(t)
                continue
        groups.append([t])
    return groups


def idempotency_key(group: list[Transcript], cfg: dict, aliases: dict) -> str:
    material = {
        "sources": sorted(t.sha256 for t in group),
        "prepare": cfg["prepare"],
        "aliases": aliases,
        "owner_tenant": cfg["owner"].get("tenant"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def prepare(group: list[Transcript], cfg: dict, aliases: Optional[dict] = None) -> Prepared:
    """Prepare one episode (one transcript, or several merged)."""
    p = cfg["prepare"]
    aliases = aliases or {}
    first = group[0]
    first_start = _iso(str(first.meta.get("start")))
    turns: list[Turn] = []
    for t in group:
        start = _iso(str(t.meta.get("start")))
        offset = (start - first_start).total_seconds() if start and first_start else 0.0
        turns += [Turn(x.seconds + offset, x.speaker, x.text) for x in t.turns]

    check_encoding([x.text for x in turns] + [str(v) for v in first.meta.values()], p["mojibake_markers"])

    patterns = [re.compile(x) for x in p["redaction_patterns"]]
    redactions = rewritten = 0
    cleaned = []
    for x in turns:
        text, n = redact(x.text, patterns, p["redaction_token"])
        text, a = rewrite_aliases(text, aliases)
        redactions += n
        rewritten += a
        cleaned.append(Turn(x.seconds, x.speaker, text))

    meta = rederive_meta(first.meta, cfg)
    if len(group) > 1:
        meta["end"] = rederive_meta(group[-1].meta, cfg)["end"]
    for key in ("call_title", "organizer"):
        if meta[key]:
            meta[key], n = redact(meta[key], patterns, p["redaction_token"])
            redactions += n

    kept, dropped = remove_echo(cleaned, p["echo_window_s"], p["echo_overlap"], p["echo_min_words"])
    words = sum(len(_words(x.text)) for x in kept)
    return Prepared(
        sources=[str(t.path) for t in group], meta=meta, turns=kept,
        is_stub=not kept or words < p["min_words"], words=words, echo_dropped=dropped,
        redactions=redactions, aliases_rewritten=rewritten, key=idempotency_key(group, cfg, aliases),
    )
