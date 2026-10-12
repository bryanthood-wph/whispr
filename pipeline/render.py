"""Render an extract document (config/schema/extract.json) as Markdown.

This is the text a judge reads (B.5) and, later, what the vault gets, so it is plain
and readable. It is deterministic: lists keep the document's own order and fields
appear in schema order, so the same document always renders to the same bytes.

The headline is the first paragraph rather than a heading, so it reads as a claim,
not a title. Entities, edges and topics are graph data and tags, not reading matter,
and are not rendered.

`render` is the judge's view and keeps the document's own order. `note` is the
summary note the write stage files for a reader (README §3 progressive disclosure):
the same parts, My Actions first, with tasks to confirm marked.
"""

from __future__ import annotations

from typing import Callable, Optional

MY_ACTIONS = "My Actions"
OTHER_TASKS = "Other Tasks"
FACTS = "Key Facts"
NONE = "None"
ME = "me"                       # owner of every My Actions task: the recording owner
NO_OWNER = "not named"
NO_DUE = "not stated"
CONFIRM = "confirm?"            # D.5: a task whose ownership the transcript can't settle


def labels() -> dict[str, str]:
    """The labels a reader of the rendering must know, as prompt placeholder values
    (config/prompts/judge.md), so no label is typed in two places. Read at call time."""
    return {"ME": ME, "NO_OWNER": NO_OWNER, "NO_DUE": NO_DUE, "MY_ACTIONS": MY_ACTIONS, "NONE": NONE}


def _label(value: str) -> str:
    """A schema enum value as a label: 'open_question' -> 'Open question'."""
    return value.replace("_", " ").capitalize()


def task(item: dict, *, mine: bool, confirm: bool = False) -> str:
    """One task as a bullet with its owner, due date and context on sub-bullets.
    Standalone, so a judge prompt can quote a single task with its ownership.
    `confirm` marks the action "(confirm?)"; the judge's view never sets it."""
    owner = f"{ME} ({item['owner_basis'].replace('_', ' ')})" if mine else (item["owner"] or NO_OWNER)
    action = f"{item['action']} ({CONFIRM})" if confirm else item["action"]
    lines = [f"- {action}", f"  - Owner: {owner}", f"  - Due: {item['due']['text'] or NO_DUE}"]
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


def my_actions_block(lines: list[str]) -> str:
    """The My Actions section: always shown, with "None" when empty (B.8)."""
    return _block(MY_ACTIONS, lines or [NONE])


def _sections(doc: dict) -> list[str]:
    return [_block(s["heading"], [point(p) for p in s["points"]]) for s in doc["sections"]]


def _tail(doc: dict, other_lines: list[str]) -> list[str]:
    """Other tasks and facts, each only when non-empty."""
    blocks = [_block(OTHER_TASKS, other_lines)] if other_lines else []
    if doc["facts"]:
        blocks.append(_block(FACTS, [fact(f) for f in doc["facts"]]))
    return blocks


def render(doc: dict) -> str:
    """Headline, points and facts use the same per-field functions as claim_parts."""
    blocks = [headline(doc), *_sections(doc), my_actions_block([line for _, line in tasks(doc, mine=True)]),
              *_tail(doc, [line for _, line in tasks(doc, mine=False)])]
    return "\n\n".join(blocks) + "\n"


def note(doc: dict, *, confirm: Callable[[dict], bool] = lambda _item: False) -> str:
    """The summary note's body: My Actions first, each task marked when `confirm(item)`,
    then the headline, sections, other tasks and facts, from the same functions as
    `render`, so the note and the judge's view never disagree on a line's text."""
    mine = [task(t, mine=True, confirm=confirm(t)) for t in doc["my_actions"]]
    others = [task(t, mine=False, confirm=confirm(t)) for t in doc["other_tasks"]]
    blocks = [my_actions_block(mine), headline(doc), *_sections(doc), *_tail(doc, others)]
    return "\n\n".join(blocks) + "\n"
