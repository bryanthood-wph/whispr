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

ME = "Me"            # the recorder's label for the recording owner's microphone
OTHERS = "Others"    # the recorder's label for the combined far-end channel
_TURN = re.compile(rf"^\*\*\[(\d+):(\d\d):(\d\d)\] ({re.escape(ME)}|{re.escape(OTHERS)}):\*\* ?(.*)$")
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


def word_tokens(text: str) -> list[str]:
    """Lowercase word tokens: the one tokenizer for echo removal, the stub gate and the
    eval's quote check."""
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


def owner_name(cfg: dict) -> str:
    """The recording owner's configured full name (owner.name)."""
    return cfg["owner"]["name"]


def owner_names(cfg: dict) -> tuple[str, ...]:
    """The recording owner's spellings: the configured full name and its first word."""
    name = owner_name(cfg)
    return tuple(dict.fromkeys([name, name.split()[0]]))


def _person(name: str) -> str:
    """'Last, First' -> 'First Last'; anything else unchanged."""
    parts = [p.strip() for p in name.split(",")]
    return f"{parts[1]} {parts[0]}" if len(parts) == 2 and all(parts) else name.strip()


MEETING = "meeting"
CALL = "call"
OUTLOOK_SOURCE = "outlook"   # the recorder's metadata_source for an Outlook-matched call


def call_type(meta: dict) -> str:
    """MEETING when the recorder matched the call to an Outlook item, else CALL."""
    return MEETING if meta.get("metadata_source") == OUTLOOK_SOURCE else CALL


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
        "call_type": call_type(meta),
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


def compile_aliases(aliases: dict[str, list[str]]) -> list[tuple[str, re.Pattern]]:
    """One case-insensitive regex per canonical name. The canonical spelling is itself
    an alternative, longest first, so a variant inside an already-canonical name
    ("Jamie" in "Jamie Doe") matches as the canonical name and is left alone."""
    compiled = []
    for canonical, variants in aliases.items():
        alts = sorted({canonical, *variants}, key=len, reverse=True)
        compiled.append((canonical, re.compile(r"\b(?:" + "|".join(map(re.escape, alts)) + r")\b", re.IGNORECASE)))
    return compiled


def rewrite_aliases(text: str, compiled: list[tuple[str, re.Pattern]]) -> tuple[str, int]:
    """Rewrite variants to their canonical spelling; count only real changes. The
    replacement is a function, so a backslash in a name is never read as an escape."""
    count = 0
    for canonical, rx in compiled:
        count += sum(m.group(0) != canonical for m in rx.finditer(text))
        text = rx.sub(lambda _m, c=canonical: c, text)
    return text, count


def redact_tree(value: Any, patterns: list[re.Pattern], token: str) -> tuple[Any, int]:
    """Redact every string inside a metadata value (str, list or dict), so a field
    added later can't bypass redaction on its way to the prompt."""
    if isinstance(value, str):
        return redact(value, patterns, token)
    if isinstance(value, list):
        pairs = [redact_tree(v, patterns, token) for v in value]
        return [v for v, _ in pairs], sum(n for _, n in pairs)
    if isinstance(value, dict):
        pairs = {k: redact_tree(v, patterns, token) for k, v in value.items()}
        return {k: v for k, (v, _) in pairs.items()}, sum(n for _, n in pairs.values())
    return value, 0


def remove_echo(turns: list[Turn], window_s: float, overlap: float, min_words: int) -> tuple[list[Turn], list[Turn]]:
    """Drop Me lines that repeat a nearby Others line (laptop speakers bleeding into the mic)."""
    others = [(t.seconds, set(word_tokens(t.text))) for t in turns if t.speaker == OTHERS]
    kept, dropped = [], []
    for t in turns:
        mine = set(word_tokens(t.text))
        if t.speaker == ME and len(word_tokens(t.text)) >= min_words and any(
            abs(t.seconds - s) <= window_s and len(mine & ws) / len(mine) >= overlap for s, ws in others
        ):
            dropped.append(t)
        else:
            kept.append(t)
    return kept, dropped


def parse_iso(value) -> Optional[datetime]:
    """A datetime, or None for a missing or malformed value."""
    if isinstance(value, datetime):     # an unquoted YAML timestamp
        return value
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
            prev_end, start = parse_iso(prev.meta.get("end")), parse_iso(t.meta.get("start"))
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
    first_start = parse_iso(first.meta.get("start"))
    turns: list[Turn] = []
    for t in group:
        start = parse_iso(t.meta.get("start"))
        offset = (start - first_start).total_seconds() if start and first_start else 0.0
        turns += [Turn(x.seconds + offset, x.speaker, x.text) for x in t.turns]

    check_encoding([x.text for x in turns] + [str(v) for v in first.meta.values()], p["mojibake_markers"])

    # Case-insensitive for every pattern, so a new one can't forget (?i).
    patterns = [re.compile(x, re.IGNORECASE) for x in p["redaction_patterns"]]
    compiled = compile_aliases(aliases)
    redactions = rewritten = 0
    cleaned = []
    for x in turns:
        text, n = redact(x.text, patterns, p["redaction_token"])
        text, a = rewrite_aliases(text, compiled)
        redactions += n
        rewritten += a
        cleaned.append(Turn(x.seconds, x.speaker, text))

    meta = rederive_meta(first.meta, cfg)
    if len(group) > 1:
        meta["end"] = rederive_meta(group[-1].meta, cfg)["end"]
    meta, n = redact_tree(meta, patterns, p["redaction_token"])
    redactions += n

    kept, dropped = remove_echo(cleaned, p["echo_window_s"], p["echo_overlap"], p["echo_min_words"])
    words = sum(len(word_tokens(x.text)) for x in kept)
    return Prepared(
        sources=[str(t.path) for t in group], meta=meta, turns=kept,
        is_stub=not kept or words < p["min_words"], words=words, echo_dropped=dropped,
        redactions=redactions, aliases_rewritten=rewritten, key=idempotency_key(group, cfg, aliases),
    )
