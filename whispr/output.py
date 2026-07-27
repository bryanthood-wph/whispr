"""Writes vault-ready markdown transcript files.

Produces a single markdown file per call session, with YAML frontmatter matching
the vault schema plus transcript turns in the body. Writes atomically so a crash
never leaves a half-written transcript on disk.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from whispr.fileio import atomic_write_text
from whispr.log import get_logger
from whispr.models import PENDING_SUMMARY_TOPIC, CallMetadata, CallSession, TranscriptResult

log = get_logger("output")

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_HYPHEN = re.compile(r"-{2,}")
_MULTI_SPACE = re.compile(r"\s+")

_SLUG_MAX = 60  # max slug characters in transcript filename

# vault source-note type for all transcripts (cohoodOBS taxonomy); intentionally
# "meeting" for both scheduled meetings AND ad-hoc calls — this is the vault
# note-type, not the call_type field which distinguishes the two.
VAULT_SOURCE = "meeting"

# Regex to rewrite yaml.safe_dump's block-list topic into flow style so
# summarize.py's _UNSUMMARIZED_RE can match it.
_TOPIC_BLOCK_RE = re.compile(
    r"^topic:\n(?:[ \t]*-[ \t]*" + re.escape(PENDING_SUMMARY_TOPIC) + r"\n)",
    re.MULTILINE,
)
_TOPIC_FLOW = f"topic: [{PENDING_SUMMARY_TOPIC}]\n"


def _slugify(title: str) -> str:
    """Turn a title into a filesystem-safe slug, lowercase and hyphenated."""
    text = title.strip().lower()
    text = _ILLEGAL_CHARS.sub("", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    text = text.replace(" ", "-")
    text = _MULTI_HYPHEN.sub("-", text).strip("-")
    return text[:_SLUG_MAX].strip("-") or "untitled"


def _format_hms(seconds: float) -> str:
    """Format an offset in seconds as zero-padded HH:MM:SS."""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _build_frontmatter(session: CallSession, meta: CallMetadata, cfg: dict[str, Any]) -> dict[str, Any]:
    """Assemble the frontmatter dict in the exact required key order."""
    max_chars = cfg["metadata"]["invite_notes_max_chars"]
    invite_notes = meta.invite_notes
    if invite_notes is not None:
        invite_notes = invite_notes[:max_chars]

    model = f'{cfg["transcription"]["model"]}-{cfg["transcription"]["compute_type"]}'

    return {
        "date": session.start.date().isoformat(),
        "source": VAULT_SOURCE,
        "topic": [PENDING_SUMMARY_TOPIC],
        "status": "raw",
        "confidence": "working",
        "call_title": meta.call_title or session.window_title or "Untitled",
        "call_type": session.call_type,
        "start": session.start.isoformat(),
        "end": session.end.isoformat() if session.end is not None else None,
        "duration_min": session.duration_min,
        "organizer": meta.organizer,
        "attendees": list(meta.attendees) if meta.attendees else [],
        "invite_notes": invite_notes,
        "metadata_source": meta.source,
        "output_device": session.output_device,
        "mic_device": session.mic_device,
        "model": model,
        "partial": session.partial,
        "whispr_version": cfg["version"],
    }


def _render_body(transcript: TranscriptResult) -> str:
    """Render the markdown body after the frontmatter."""
    lines = ["> _Summary pending._", ""]
    if transcript.is_stub:
        lines.append("_No speech detected._")
        return "\n".join(lines)

    turn_blocks = [
        f"**[{_format_hms(turn.start_seconds)}] {turn.speaker}:** {turn.text}"
        for turn in transcript.turns
    ]
    lines.append("\n\n".join(turn_blocks))
    return "\n".join(lines)


def _resolve_unique_path(directory: Path, stem: str, suffix: str = ".md") -> Path:
    """Return a path in directory that doesn't already exist, appending -2, -3, ... if needed."""
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def write_transcript(
    session: CallSession, meta: CallMetadata, transcript: TranscriptResult, cfg: dict[str, Any]
) -> Path:
    """Write a vault-ready markdown transcript file for the session and return its path.

    The filename is `YYYY-MM-DD-HHMM-<slug>.md` based on session.start and the best
    available title. The file is written atomically (temp file + os.replace).
    """
    out_dir: Path = cfg["paths"]["transcripts"]

    title = meta.call_title or session.window_title or "untitled"
    slug = _slugify(title)
    stem = f"{session.start.strftime('%Y-%m-%d-%H%M')}-{slug}"
    final_path = _resolve_unique_path(out_dir, stem)

    frontmatter = _build_frontmatter(session, meta, cfg)
    yaml_text = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    # Render the topic placeholder as YAML flow list so summarize.py's regex matches.
    yaml_text = _TOPIC_BLOCK_RE.sub(_TOPIC_FLOW, yaml_text, count=1)
    body = _render_body(transcript)

    content = f"---\n{yaml_text}---\n\n{body}\n"

    atomic_write_text(final_path, content)

    log.info("Wrote transcript: %s", final_path)
    return final_path
