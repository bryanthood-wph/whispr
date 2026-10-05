"""Judging (docs/plan/B-eval.md B.5): seven typed decisions, their confidence
escalation, deterministic claim splitting, and calibration by planted errors.

All model access goes through an injected `ask` (eval/ask.py), so this module never
calls a model itself and is tested with a plain function.

- `build_prompt` fills config/prompts/judge.md. The shared material (rules, the
  transcript, the rendered summary) comes first and the decision-specific question,
  item and answers last, so every call on one transcript shares a byte-identical
  prefix for prompt caching. Rendering labels come from pipeline/render.py.
- `decide` samples a decision `eval.judge.samples` times on role `judge`; a split
  goes to `judge_escalate_1`, whose answer is accepted at confidence of at least
  `eval.judge.escalate_below`, else `judge_escalate_2` decides. Model-facing schemas
  carry no numeric constraints, so the vocabulary and the [0, 1] range are checked
  here and a violation raises JudgeError.
- `plant` puts one error of a known kind into a copy of an extract document, and
  `plant_positive` puts a verbatim transcript claim (from `plant_positives`) into one
  the same way. The planting material is seed data in config/planting.yaml.
  `sensitivity`, `specificity` and `calibrate` turn verdicts into per-decision rates.
"""

from __future__ import annotations

import copy
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Optional

from pipeline import prompts, render
from pipeline.config import PLANTING_PATH, _read_yaml, load_schema
from pipeline.jsonschema_lite import validate
from pipeline.prepare import Prepared

# B.5's decisions and their answer vocabularies. The owner/due row is two decisions,
# as B.6 scores owner and due accuracy apart.
DECISIONS: dict[str, tuple[str, ...]] = {
    "present": ("yes", "partial", "no"),
    "supported": ("supported", "unsupported", "contradicted"),
    "attribution": ("yes", "no"),
    "task_owner": ("yes", "no"),
    "task_due": ("yes", "no"),
    "actionable": ("yes", "no"),
    "my_actions": ("yes", "no"),
}
# The answer that flags no error, per decision; any other answer is detected as one.
# For presence that makes a partial a detection: the calibration convention the pilot
# contract fixes (a plant that leaves a task partial was caught). The primary endpoint
# instead scores a partial as 0.5 (PRESENT_VALUE), and as 0 only in a sensitivity
# analysis (eval/PREREGISTRATION.md, primary endpoint).
PASS = {"present": "yes", "supported": "supported", "attribution": "no", "task_owner": "yes",
        "task_due": "yes", "actionable": "yes", "my_actions": "yes"}
# Presence answers as outcome values (eval/records.py Outcome.value: 1 hit, 0.5 partial, 0 miss).
PRESENT_VALUE = {"yes": 1.0, "partial": 0.5, "no": 0.0}
# (decision, answer): the only verdict that carries missing ids.
MISSING = ("my_actions", "no")

# Escalation tiers, in order: config `models` roles (B.10).
FIRST, ESCALATE_1, ESCALATE_2 = TIERS = ("judge", "judge_escalate_1", "judge_escalate_2")

# B.5's planted error kinds, and the decision each one tests.
PLANT_KINDS = ("owner_swap", "deleted_my_task", "changed_number",
               "invented_claim", "reattribution", "dropped_due")
KIND_DECISION = {
    "owner_swap": "task_owner", "deleted_my_task": "present", "changed_number": "supported",
    "invented_claim": "supported", "reattribution": "attribution", "dropped_due": "task_due",
}
POSITIVE = "positive"               # Planted.kind of a verbatim transcript claim

_NOT_STATED = "not_stated"          # extract.json due.basis when no due date is stated (a test checks the enum)
_QUESTION = re.compile(r"\?[\"'”’)\]}]*$")   # a question, even inside closing quotes or brackets
_HEADING = re.compile(r"^#{1,6}\s")
_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)]|>)\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?!\w)")     # not "S3", "v2" or "x86"
_DECISION_BLOCK = re.compile(r"^## decision: (\w+)[ \t]*$", re.MULTILINE)


class JudgeError(ValueError):
    """A judge answer outside its decision's vocabulary or range, or a bad prompt file."""


class NotApplicable(ValueError):
    """The planting kind has nothing to act on in this document."""


@dataclass(frozen=True)
class Verdict:
    decision: str
    value: str
    tier: str                         # the role whose answer was accepted
    samples: tuple[str, ...]          # the first-tier answers, in replicate order
    missing: tuple[str, ...] = ()     # my_actions "no" only: ids of absent reference tasks
    confidence: Optional[float] = None  # the accepted call's (unanimous: lowest sample's)


@dataclass(frozen=True)
class Planted:
    kind: str
    doc: dict
    target: str                       # the text of the item the error was planted in


# --- prompt -------------------------------------------------------------------------

def _used(values: dict[str, str], template: str) -> dict[str, str]:
    """The subset of `values` whose placeholders `template` uses."""
    used = prompts.placeholders(template)
    return {k: v for k, v in values.items() if k in used}


@lru_cache(maxsize=None)
def _parsed_template(relative: str) -> tuple[str, Mapping[str, str]]:
    """The judge prompt at `relative` (as configured) split into the shared template and
    one question per decision; parsed once per path."""
    text = prompts.load(relative)
    parts = _DECISION_BLOCK.split(text)
    template, questions = parts[0].rstrip("\n") + "\n", dict(zip(parts[1::2], (q.strip() for q in parts[2::2])))
    if set(questions) != set(DECISIONS):
        raise JudgeError(f"judge prompt decisions {sorted(questions)} != {sorted(DECISIONS)}")
    return template, MappingProxyType(questions)


def build_prompt(decision: str, cfg: dict, *, transcript: str, summary: str, subject: str) -> str:
    """The one way to build a judge prompt. `transcript` is Prepared.render(),
    `summary` is pipeline.render.render(doc), `subject` is the claim or item asked about."""
    vocab = _vocabulary(decision)
    template, questions = _parsed_template(cfg["prompts"]["judge"])
    labels, question = render.labels(), questions[decision]
    # The question is filled first: prompts.render never re-expands a substituted value.
    values = {
        "OWNER_NAME": cfg["owner"]["name"], "TRANSCRIPT": transcript.rstrip("\n"), "SUMMARY": summary.rstrip("\n"),
        "QUESTION": prompts.render(question, _used(labels, question)), "SUBJECT": subject,
        "ANSWERS": " | ".join(vocab),
    }
    return prompts.render(template, {**values, **_used(labels, template)})


# --- deciding -----------------------------------------------------------------------

def _vocabulary(decision: str) -> tuple[str, ...]:
    if decision not in DECISIONS:
        raise JudgeError(f"unknown decision {decision!r}")
    return DECISIONS[decision]


def _checked(decision: str, vocab: tuple[str, ...], out: dict, schema: dict) -> dict:
    errors = validate(out, schema)
    if errors:
        raise JudgeError(f"{decision}: judge output invalid: {errors}")
    if out["answer"] not in vocab:
        raise JudgeError(f"{decision}: answer {out['answer']!r} not in {vocab}")
    if not 0 <= out["confidence"] <= 1:
        raise JudgeError(f"{decision}: confidence {out['confidence']!r} outside [0, 1]")
    return out


def _missing(decision: str, out: dict) -> tuple[str, ...]:
    """The output's missing ids, de-duplicated in order; only a my_actions "no" has any.
    A "no" with none is allowed."""
    if (decision, out["answer"]) != MISSING:
        return ()
    return tuple(dict.fromkeys(out.get("missing", [])))


def check(cfg: dict) -> None:
    """Raise JudgeError for a configuration no answer can fix: a judge role missing from
    `models`, or a judge prompt whose decision blocks don't match DECISIONS. Callers
    run it before any spend; `decide` runs it too."""
    unknown = [r for r in TIERS if r not in cfg["models"]]
    if unknown:
        raise JudgeError(f"config models lacks judge roles {unknown}")
    _parsed_template(cfg["prompts"]["judge"])


def decide(decision: str, prompt: str, cfg: dict, ask: Callable, *,
           system_prompt: Optional[str] = None) -> Verdict:
    vocab = _vocabulary(decision)
    check(cfg)
    schema, jc = load_schema(cfg, "judge"), cfg["eval"]["judge"]

    def call(role: str, replicate: int = 0) -> dict:
        out = ask(role, prompt, schema, system_prompt=system_prompt, replicate=replicate)
        return _checked(decision, vocab, out, schema)

    outs = [call(FIRST, r) for r in range(jc["samples"])]
    answers = tuple(o["answer"] for o in outs)
    # Unanimous means the same answer and, where carried, the same set of missing ids.
    if len({(o["answer"], frozenset(_missing(decision, o))) for o in outs}) == 1:
        return Verdict(decision, answers[0], FIRST, answers, _missing(decision, outs[0]),
                       min(o["confidence"] for o in outs))
    out, tier = call(ESCALATE_1), ESCALATE_1
    if out["confidence"] < jc["escalate_below"]:
        out, tier = call(ESCALATE_2), ESCALATE_2
    return Verdict(decision, out["answer"], tier, answers, _missing(decision, out), out["confidence"])


def flags_error(decision: str, value: str) -> bool:
    """Whether an answer flags an error (see PASS). JudgeError for an unknown decision."""
    _vocabulary(decision)
    return value != PASS[decision]


def rates(pairs: Iterable[tuple[str, bool]]) -> dict[str, float]:
    """Share of True per key, keys in first-seen order."""
    seen: dict[str, list[bool]] = defaultdict(list)
    for key, flag in pairs:
        seen[key].append(bool(flag))
    return {k: sum(v) / len(v) for k, v in seen.items()}


def escalation_rates(verdicts: Iterable[Verdict]) -> dict[str, float]:
    """Per decision, the share of verdicts not settled by the first tier (B.5, B.7)."""
    return rates((v.decision, v.tier != FIRST) for v in verdicts)


# --- claims -------------------------------------------------------------------------

def split_claims(text: str) -> list[str]:
    """One claim per sentence or bullet. Headings and blank lines are skipped; list
    and quote markers are stripped. Each claim is a substring of `text`."""
    claims = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _HEADING.match(line):
            continue
        while _LIST_MARKER.match(line):
            line = _LIST_MARKER.sub("", line, count=1)
        claims += [s.strip() for s in _SENTENCE_END.split(line) if s.strip()]
    return claims


def summary_claims(doc: dict) -> list[str]:
    """The claims the faithfulness decision ("supported") judges: headline, section
    points and facts, as rendered. Tasks are judged by task_owner, task_due and
    actionable instead, so their labels never become claims."""
    return split_claims("\n".join(render.claim_lines(doc)))


# --- planting -----------------------------------------------------------------------

@lru_cache(maxsize=1)
def planting() -> dict:
    """config/planting.yaml (PLANTING_PATH). Treat as read-only."""
    return _read_yaml(PLANTING_PATH)


def _words(words: Iterable[str]) -> str:
    """A regex matching any of `words` as whole words (never inside another word)."""
    return r"(?<!\w)(?:" + "|".join(map(re.escape, words)) + r")(?!\w)"


@lru_cache(maxsize=1)
def _unnamed_speaker() -> re.Pattern:
    m = planting()["unnamed_speaker"]
    return re.compile(
        rf"^(?P<subject>{_words(m['determiners'])}(?:\s+[\w'-]+){{0,{int(m['subject_extra_words'])}}}?)\s+"
        rf"(?P<rest>{_words(m['speech_verbs'])}.*)$", re.IGNORECASE)


@lru_cache(maxsize=1)
def _pronoun() -> re.Pattern:
    return re.compile(_words(planting()["pronouns"]), re.IGNORECASE)


class _People:
    """Named people in a document, one canonical name each. A spelling that is a
    person entity's alias maps to the entity's name, so "Jamie" and "Jamie Doe" are
    one person. Excluded people (the recording owner) are never chosen."""

    def __init__(self, doc: dict, exclude: Iterable[str]):
        persons = [e for e in doc["entities"] if e["type"] == "person"]
        self._canon = {s.lower(): e["name"] for e in persons for s in (*e["aliases"], e["name"])}
        spellings = sorted({s.lower() for s in (*self._canon, *exclude)})
        self._named = re.compile(_words(spellings), re.IGNORECASE) if spellings else None
        excluded = {self.canon(x).lower() for x in exclude}
        names = {e["name"] for e in persons} | {self.canon(t["owner"]) for t in doc["other_tasks"] if t["owner"]}
        self.allowed = sorted(n for n in names if n.lower() not in excluded)

    def canon(self, name: str) -> str:
        return self._canon.get(name.lower(), name)

    def named_in(self, text: str) -> bool:
        """Any spelling of a known or excluded person occurs in `text` as whole words."""
        return bool(self._named and self._named.search(text))


def _pick(rng: random.Random, items, reason: str):
    """rng.choice(items), or NotApplicable(reason) when there is nothing to choose."""
    if not items:
        raise NotApplicable(reason)
    return rng.choice(items)


def _text_slots(doc: dict) -> list[tuple]:
    """Paths of the summary's claim-bearing fields (render.claim_parts)."""
    return [path for path, _ in render.claim_parts(doc)]


def _changed_claim(doc: dict, path: tuple, before: str = "") -> str:
    """The one claim, as summary_claims gives it, that is new in the field at `path`
    against its rendered line `before` (empty for an inserted point)."""
    old = split_claims(before)
    new = [c for c in split_claims(dict(render.claim_parts(doc))[path]) if c not in old]
    if len(new) != 1:
        raise NotApplicable(f"the change touches {len(new)} claims, not one")
    return new[0]


def _edit(doc: dict, parts: dict[tuple, str], path: tuple, new: str) -> str:
    """Set the claim-bearing field at `path` to `new` and return the changed claim.
    `parts` is dict(render.claim_parts(doc)) from before the edit."""
    before = parts[path]
    _set(doc, path, new)
    return _changed_claim(doc, path, before)


def _held_elsewhere(doc: dict, phrase: str, skip: Optional[dict] = None) -> bool:
    """`phrase` occurs, as whole words in any case, in some free text of the summary
    other than task `skip`'s: a claim-bearing field or a task's action or context."""
    texts = [_get(doc, p) for p in _text_slots(doc)]
    for t in (*doc["my_actions"], *doc["other_tasks"]):
        if t is not skip:
            texts += [t["action"], t["context"]]
    rx = re.compile(_words([phrase]), re.IGNORECASE)
    return any(rx.search(text) for text in texts)


def _get(doc: dict, path: tuple):
    node = doc
    for part in path:
        node = node[part]
    return node


def _set(doc: dict, path: tuple, value) -> None:
    _get(doc, path[:-1])[path[-1]] = value


def _insert_point(doc: dict, rng: random.Random, text_for: Callable[[dict, random.Random], str]) -> str:
    """Insert one point into a random section at a random position and return its
    claim. Invented claims and positives both go through here, so they sit in the
    summary identically."""
    index = _pick(rng, range(len(doc["sections"])), "no section to add a point to")
    section = doc["sections"][index]
    position = rng.randint(0, len(section["points"]))
    section["points"].insert(position, text_for(section, rng))
    return _changed_claim(doc, ("sections", index, "points", position))


def _owner_swap(doc: dict, rng: random.Random, people: _People) -> str:
    """Give a task to a different named person; a my-task moves to Other Tasks."""
    candidates = [(lst, i, options)
                  for lst in ("my_actions", "other_tasks") for i, t in enumerate(doc[lst])
                  for current in [t.get("owner") and people.canon(t["owner"])]     # my-tasks: None
                  if (options := [p for p in people.allowed if p != current])]
    lst, i, options = _pick(rng, candidates, "no task with another named person to give it to")
    new_owner = rng.choice(options)
    item = doc[lst][i]
    if lst == "my_actions":
        doc["my_actions"].pop(i)
        doc["other_tasks"].append({"action": item["action"], "owner": new_owner, "due": item["due"],
                                   "context": item["context"], "quote": item["quote"], "start": item["start"]})
    else:
        item["owner"] = new_owner
    return item["action"]


def _deleted_my_task(doc: dict, rng: random.Random) -> str:
    """Delete a my-task that nothing else in the summary restates, other tasks
    included (otherwise the item would still be present and the plant undetectable)."""
    indexes = [i for i, t in enumerate(doc["my_actions"]) if not _held_elsewhere(doc, t["action"], skip=t)]
    return doc["my_actions"].pop(_pick(rng, indexes, "no my-task that only the My Actions section holds"))["action"]


def _changed_number(doc: dict, rng: random.Random) -> str:
    """Replace one digit of one number in the summary text with a different digit."""
    parts = dict(render.claim_parts(doc))
    found = [(path, m) for path in parts for m in _NUMBER.finditer(_get(doc, path))]
    path, m = _pick(rng, found, "no number in the summary text")
    digits = [m.start() + k for k, ch in enumerate(m.group()) if ch.isdigit()]
    pos = rng.choice(digits)
    text = _get(doc, path)
    leading = pos == digits[0] and len(digits) > 1
    choices = [d for d in "0123456789" if d != text[pos] and not (leading and d == "0")]
    return _edit(doc, parts, path, text[:pos] + rng.choice(choices) + text[pos + 1:])


def _invented_claim(doc: dict, rng: random.Random) -> str:
    material = planting()

    def text_for(section: dict, rng: random.Random) -> str:
        template = rng.choice(material["invented_claims"])
        low, high = material["invented_number_range"]
        values = {"TOPIC": section["heading"], "NUMBER": str(rng.randint(low, high))}
        return prompts.render(template, _used(values, template))
    return _insert_point(doc, rng, text_for)


def _reattribution(doc: dict, rng: random.Random, people: _People) -> str:
    """Name a person as the speaker of a point. Prefer a point whose speaker is unnamed
    ("The client said ..."); otherwise attribute a plain point to the person."""
    parts = dict(render.claim_parts(doc))
    points = [p for p in parts if p[0] == "sections"]
    unnamed = []
    for path in points:
        m = _unnamed_speaker().match(_get(doc, path))
        if m and not people.named_in(m.group("subject")):
            unnamed.append((path, m))
    person = _pick(rng, people.allowed, "no named person to attribute speech to")
    if unnamed:
        path, m = rng.choice(unnamed)
        new = f"{person} {m.group('rest')}"
    else:
        path = _pick(rng, points, "no section point to attribute")
        new = f"{_get(doc, path).rstrip(' .!?')}, {person} said."
    return _edit(doc, parts, path, new)


def _dropped_due(doc: dict, rng: random.Random) -> str:
    """Drop a stated due date that no other text in the summary repeats, the task's
    own wording included (otherwise the date would still show and the plant be
    undetectable)."""
    tasks = [t for lst in ("my_actions", "other_tasks") for t in doc[lst]
             if t["due"]["text"] and not _held_elsewhere(doc, t["due"]["text"])]
    item = _pick(rng, tasks, "no task with a due date only its Due line shows")
    item["due"] = {"text": None, "basis": _NOT_STATED}
    return item["action"]


# Planters that choose a person get the document's _People; the rest do not.
_PLANTERS = {"deleted_my_task": _deleted_my_task, "changed_number": _changed_number,
             "invented_claim": _invented_claim, "dropped_due": _dropped_due}
_PEOPLE_PLANTERS = {"owner_swap": _owner_swap, "reattribution": _reattribution}


def plant(kind: str, doc: dict, seed: int, *, exclude: Iterable[str] = ()) -> Planted:
    """One error of `kind` in a deep copy of `doc`; the same seed gives the same plant.
    `exclude`: names and aliases (the recording owner's) never chosen as a person."""
    planted, rng = copy.deepcopy(doc), random.Random(seed)
    if kind in _PEOPLE_PLANTERS:
        target = _PEOPLE_PLANTERS[kind](planted, rng, _People(planted, tuple(exclude)))
    elif kind in _PLANTERS:
        target = _PLANTERS[kind](planted, rng)
    else:
        raise ValueError(f"unknown planting kind {kind!r}")
    return Planted(kind, planted, target)


def plant_positive(doc: dict, claim: str, seed: int) -> Planted:
    """`claim` (from plant_positives) inserted into a copy of `doc` exactly as an
    invented claim would be: a true item, to measure specificity (B.5)."""
    planted = copy.deepcopy(doc)
    return Planted(POSITIVE, planted, _insert_point(planted, random.Random(seed), lambda section, rng: claim))


def plant_positives(prep: Prepared, seed: int, n: int) -> list[str]:
    """`n` distinct verbatim sentences from the prepared transcript: at least
    planting.yaml `positive_min_words` long, not a question, and free of first- and
    second-person pronouns."""
    min_words = planting()["positive_min_words"]
    claims = dict.fromkeys(c for t in prep.turns for c in split_claims(t.text)
                           if len(c.split()) >= min_words and not _QUESTION.search(c)
                           and not _pronoun().search(c))
    if len(claims) < n:
        raise NotApplicable(f"only {len(claims)} transcript claims, {n} wanted")
    return random.Random(seed).sample(list(claims), n)


# --- calibration --------------------------------------------------------------------

def sensitivity(detections: Iterable[tuple[str, bool]]) -> dict[str, float]:
    """Per planted kind, the share of plants the judge caught."""
    return rates(detections)


def specificity(positives: Iterable[tuple[str, bool]]) -> dict[str, float]:
    """Per decision, the share of true items (planted positives) not flagged as errors."""
    return rates((decision, not flagged) for decision, flagged in positives)


def calibrate(detections: Iterable[tuple[str, bool]], positives: Iterable[tuple[str, bool]]
              ) -> dict[str, dict[str, Optional[float]]]:
    """Per decision: sensitivity is its weakest planted kind (any kind under target
    makes the metric unreliable, B.5); anything not measured is None."""
    sens, spec = sensitivity(detections), specificity(positives)
    out = {}
    for decision in DECISIONS:
        kinds = [sens[k] for k, d in KIND_DECISION.items() if d == decision and k in sens]
        out[decision] = {"sensitivity": min(kinds) if kinds else None, "specificity": spec.get(decision)}
    return out
