"""Shared fixtures for pipeline/eval tests. No model calls, no real user data."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

# A valid config/schema/extract.json output.
EXTRACT_SAMPLE = {
    "headline": "The team agreed to ship the deck Friday.",
    "sections": [{"heading": "Deck", "points": ["Review moved to Thursday."]}],
    "my_actions": [{
        "action": "Send the deck to Jamie", "owner_basis": "volunteered",
        "due": {"text": "Friday", "basis": "stated"}, "context": "Final review copy",
        "quote": "I'll send the deck to Jamie by Friday", "start": "00:01:02",
    }],
    "other_tasks": [{
        "action": "Book the room", "owner": None, "due": {"text": None, "basis": "not_stated"},
        "context": "", "quote": "someone book the room", "start": None,
    }],
    "topics": ["deck"],
    "entities": [{"name": "Jamie Doe", "type": "person", "aliases": ["Jamie"]}],
    "facts": [{"type": "decision", "text": "Ship Friday", "subject": None, "quote": "ship it Friday", "start": "00:02:00"}],
    "edges": [{"src": "Jamie Doe", "relation": "works_on", "dst": "Deck", "quote": "Jamie owns the deck", "start": None}],
}


def overlay(root: Path) -> dict:
    """A complete, fake per-user overlay rooted in a temp dir."""
    (root / "transcripts").mkdir(exist_ok=True)
    return {
        "owner": {"name": "Pat Example", "email": "pat@example.com", "tenant": "Example (TEN)"},
        "paths": {"transcripts": str(root / "transcripts"), "data_dir": str(root / "data"),
                  "recorder_log": str(root / "whispr.log")},
    }


def transcript(start: str, turns: list[tuple[str, str, str]], *, call_title: str = "Weekly Sync",
               metadata_source: str = "outlook", duration_min: int = 30,
               output_device: str = "Speakers (Realtek)", attendees: list[str] | None = None,
               extra_frontmatter: str = "") -> str:
    """Render a transcript in the recorder's format: YAML frontmatter + turn lines.
    turns: (hh:mm:ss, Me|Others, text)."""
    people = attendees if attendees is not None else ["Example, Pat", "Doe, Jane"]
    end = (datetime.fromisoformat(start) + timedelta(minutes=duration_min)).isoformat()
    attendee_yaml = "\n" + "".join(f"- {name}\n" for name in people) if people else " []\n"
    front = (
        "---\n"
        f"date: '{start[:10]}'\nsource: meeting\ntopic: [unsorted]\nstatus: raw\nconfidence: working\n"
        f"call_title: {call_title}\ncall_type: meeting\nstart: '{start}'\nend: '{end}'\n"
        f"duration_min: {duration_min}\norganizer: Doe, Jane\nattendees:{attendee_yaml}"
        f"metadata_source: {metadata_source}\noutput_device: {output_device}\npartial: false\n"
        f"{extra_frontmatter}---\n\n> _Summary pending._\n\n"
    )
    body = "\n\n".join(f"**[{ts}] {who}:** {text}" for ts, who, text in turns)
    return front + body + "\n"
