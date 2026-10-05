"""Render an extract document (config/schema/extract.json) as Markdown.

This is the text a judge reads (B.5) and, later, what the vault gets, so it is plain
and readable. It is deterministic: lists keep the document's own order and fields
appear in schema order, so the same document always renders to the same bytes.

The headline is the first paragraph rather than a heading, so it reads as a claim,
not a title. Entities, edges and topics are graph data and tags, not reading matter,
and are not rendered.
"""

from __future__ import annotations

from typing import Optional

MY_ACTIONS = "My Actions"
OTHER_TASKS = "Other Tasks"
FACTS = "Key Facts"
NONE = "None"
ME = "me"                       # owner of every My Actions task: the recording owner
NO_OWNER = "not named"
NO_DUE = "not stated"


def labels() -> dict[str, str]:
    """The labels a reader of the rendering must know, as prompt placeholder values
    (config/prompts/judge.md), so no label is typed in two places. Read at call time."""
    return {"ME": ME, "NO_OWNER": NO_OWNER, "NO_DUE": NO_DUE, "MY_ACTIONS": MY_ACTIONS, "NONE": NONE}


def _label(value: str) -> str:
    """A schema enum value as a label: 'open_question' -> 'Open question'."""
    return value.replace("_", " ").capitalize()


def task(item: dict, *, mine: bool) -> str:
    """One task as a bullet with its owner, due date and context on sub-bullets.
    Standalone, so a judge prompt can quote a single task with its ownership."""
    owner = f"{ME} ({item['owner_basis'].replace('_', ' ')})" if mine else (item["owner"] or NO_OWNER)
    lines = [f"- {item['action']}", f"  - Owner: {owner}", f"  - Due: {item['due']['text'] or NO_DUE}"]
    if item["context"]:
        lines.append(f"  - Context: {item['context']}")
    return "\n".join(lines)


def headline(doc: dict) -> str:
    return doc["headline"]


def point(text: str) -> str:
    return f"- {text}"


def fact(item: dict) -> str:
    about = f" ({item['subject']})" if item["subject"] else ""
    return f"- {_label(item['type'])}{about}: {item['text']}"


def claim_parts(doc: dict) -> list[tuple[tuple, str]]:
    """The one definition of the claim-bearing parts: (path to the free-text field in
    `doc`, its rendered line) for the headline, each section point and each fact.
    Tasks are not here; they are judged by their own decisions."""
    parts: list[tuple[tuple, str]] = [(("headline",), headline(doc))]
    parts += [(("sections", i, "points", j), point(p))
              for i, s in enumerate(doc["sections"]) for j, p in enumerate(s["points"])]
    parts += [(("facts", i, "text"), fact(f)) for i, f in enumerate(doc["facts"])]
    return parts


def claim_lines(doc: dict) -> list[str]:
    return [line for _, line in claim_parts(doc)]


def _block(heading: str, lines: list[str]) -> str:
    return (f"## {heading}\n\n" + "\n".join(lines)) if lines else f"## {heading}"


# The extract's task lists, and whether each holds the recording owner's tasks.
TASK_LISTS = (("my_actions", True), ("other_tasks", False))


def tasks(doc: dict, *, mine: Optional[bool] = None) -> list[tuple[str, str]]:
    """(action, rendered task) for every task in `doc`, my actions first; only my
    actions (mine=True) or only other tasks (mine=False) when asked."""
    return [(t["action"], task(t, mine=m)) for key, m in TASK_LISTS if mine in (None, m) for t in doc[key]]


def render(doc: dict) -> str:
    """Headline, points and facts use the same per-field functions as claim_parts."""
    blocks = [headline(doc)]
    blocks += [_block(s["heading"], [point(p) for p in s["points"]]) for s in doc["sections"]]
    # My Actions is always shown, with "None" when empty (B.8); the others only when non-empty.
    blocks.append(_block(MY_ACTIONS, [line for _, line in tasks(doc, mine=True)] or [NONE]))
    if doc["other_tasks"]:
        blocks.append(_block(OTHER_TASKS, [line for _, line in tasks(doc, mine=False)]))
    if doc["facts"]:
        blocks.append(_block(FACTS, [fact(f) for f in doc["facts"]]))
    return "\n\n".join(blocks) + "\n"
