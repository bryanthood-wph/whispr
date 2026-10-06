"""Shared fixtures for pipeline/eval tests. No model calls, no real user data."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from fake_version import FAKE_VERSION
from pipeline import models, prepare

# Stands in for the claude CLI (see fake_claude.py).
FAKE_CLAUDE = str(Path(__file__).with_name("fake_claude.py"))
# A filler turn for synthetic transcripts; tests repeat it to clear the stub gate.
FILLER = "we walked through the quarterly plan and the staffing model in detail today"

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


def budget_answer(store, amount: float = 1, reason: str = "a short memo") -> dict:
    """A budget brief answer under tasks.intake.budget's key names, `amount` above its floor."""
    budget = store.intake["budget"]
    return {budget["amount_key"]: budget["amount_above_usd"] + amount, budget["reason_key"]: reason}


def answer_brief(store, task_id: str, actor: str, **kw) -> dict:
    """Answer every required brief field of a task (kg.store.Store), so it can pass the
    ready gate: each text field a placeholder, the budget one unit above its floor, the
    scope tasks.intake.scope_none for every source type. `kw` goes to update_task too (a
    status, a reason)."""
    intake = store.intake
    brief = {f: f"the {f}" for f in store.required_fields if f not in (store.scope_field, store.budget_field)}
    if store.budget_field in store.required_fields:
        brief[store.budget_field] = budget_answer(store)
    scope = [{"type": t, "value": intake["scope_none"]} for t in intake["scope_types"]]
    return store.update_task(task_id, actor=actor, brief=brief, scope=scope,
                             brief_source=intake["answer_sources"][0], **kw)


def overlay(root: Path) -> dict:
    """A complete, fake per-user overlay rooted in a temp dir."""
    (root / "transcripts").mkdir(exist_ok=True)
    return {
        "owner": {"name": "Pat Example", "email": "pat@example.com", "tenant": "Example (TEN)"},
        "paths": {"transcripts": str(root / "transcripts"), "data_dir": str(root / "data"),
                  "recorder_log": str(root / "whispr.log")},
        # One call at a time: the tests pin that order (eval.ask.fan_out is then exactly a
        # loop). test_eval_parallel runs the same work on several workers.
        "eval": {"workers": 1},
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


def prepared(cfg: dict, start: str, turns: list[tuple[str, str, str]], **kw) -> prepare.Prepared:
    """Write a transcript (arguments as for `transcript`) into cfg's transcripts dir,
    named after its start, then parse and prepare it."""
    path = Path(cfg["paths"]["transcripts"]) / f"{start[:10]}-{start[11:13]}{start[14:16]}-t.md"
    path.write_text(transcript(start, turns, **kw), encoding="utf-8")
    return prepare.prepare([prepare.parse(path)], cfg)


def fake_cli() -> dict:
    """The `cli` overlay section that runs fake_claude.py in place of the claude CLI."""
    return {"executable": sys.executable, "version": FAKE_VERSION, "base_args": [FAKE_CLAUDE],
            "timeout_s": 20}


def scrubbed_env(cfg: dict, **extra: str) -> dict[str, str]:
    """os.environ without the auth.strip_env_prefixes variables, plus `extra`: for
    mock.patch.dict(os.environ, ..., clear=True), since the test process may itself
    run inside a Claude session."""
    return {**models.child_env(cfg), **extra}
