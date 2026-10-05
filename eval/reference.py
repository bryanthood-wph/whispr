"""The reference ("truth") with no humans (docs/plan/B-eval.md B.4).

    extract (family a and b) -> quote_check -> match -> consensus      (build does all four)

- Two model families (`reference_a`, `reference_b`) each extract typed items.
- `quote_check` deterministically rejects an item whose quote isn't in the transcript.
- The `matcher` pairs items across the families; a pair is accepted once, represented
  by its family-a item (`Reference.both_found`, the self-agreement subset).
- An item only one family found is asked present/absent of BOTH families; it is
  accepted only if both say present, else it is contested. A second match over the
  accepted singletons collapses duplicates and contests contradictions.
- Stability: the reference is rebuilt with `replicate=1` (tasks only), and my-task
  Jaccard, pooled over the rerun transcripts, must reach `eval.stability_min_jaccard`.
- Matcher calibration (B.5): paraphrase / near-miss pairs built in code from real
  items, scored on one matcher call over the full lists (as in use), give the matcher
  its sensitivity and specificity.

All model access goes through the injected `ask` (eval/ask.py). Prompts and schemas
come from the config's `prompts` / `schemas` maps.
"""

from __future__ import annotations

import json
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from functools import lru_cache
from typing import Optional, Sequence

from eval.ask import Ask
from pipeline import prepare, prompts
from pipeline.config import config_file
from pipeline.extract import prompt_values
from pipeline.prepare import Prepared, word_tokens

ITEM_TYPES = ("decision", "task", "number", "risk", "open_question", "fact")
TASK = "task"
FAMILY_ROLES = {"a": "reference_a", "b": "reference_b"}   # family -> config `models` key
MATCHER_ROLE = "matcher"
PRESENT = "present"
MINE_BASES = ("assigned", "volunteered")       # the ontology's owner_basis values for the owner's own task

# relation(): may two items from different families be one item?
PAIR, CONFLICT, REJECT = "pair", "conflict", "reject"
# quote_relation(): how two quotes sit in the transcript.
SAME, OVERLAP, NEAR, FAR = "same", "overlap", "near", "far"

# Typographic characters a model may type where the transcript has plain ASCII (L9).
_FOLD = str.maketrans({
    **dict.fromkeys("‘’‚‛′´`", "'"),
    **dict.fromkeys("“”„‟″«»", '"'),
    **dict.fromkeys("‐‑‒–—―−", "-"),
})


@dataclass(frozen=True)
class RefItem:
    id: str
    family: str
    type: str
    text: str
    owner: Optional[str]
    owner_basis: str
    mine: bool
    due: Optional[str]
    quote: str
    importance: int


# The fields a model writes (everything but id and family), in schema order.
_MODEL_FIELDS = tuple(f.name for f in fields(RefItem))[2:]


@dataclass(frozen=True)
class Reference:
    """One transcript's reference.
    - `both_found`: the family-a id of every item both families found.
    - `verdicts`: each escalated singleton's id -> {family: verdict}.
    - `collapsed`: (a id, b id) accepted singletons the second match found to be one item.
    - `conflicted`: (a id, b id) accepted singletons the second match paired although
      they disagree on `mine`; both are moved to contested.
    - `duplicates`: groups of ids within one family that share type and quote (flagged,
      not dropped).
    - `mine_disagreements`: extracted items whose model `mine` flag differed from the
      derived one (set by `build`).
    - `rejected`: items quote_check discarded (set by `build`)."""
    accepted: tuple[RefItem, ...]
    contested: tuple[RefItem, ...]
    both_found: frozenset[str]
    verdicts: dict[str, dict[str, str]] = field(default_factory=dict)
    collapsed: tuple[tuple[str, str], ...] = ()
    conflicted: tuple[tuple[str, str], ...] = ()
    duplicates: tuple[tuple[str, ...], ...] = ()
    mine_disagreements: int = 0
    rejected: tuple[RefItem, ...] = ()


# ---------------------------------------------------------------- prompts and schemas

def _schema(cfg: dict, key: str) -> dict:
    with open(config_file(cfg["schemas"][key]), encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def _template(relative: str) -> str:
    """A prompt template, read once per process (keyed by its config-relative path)."""
    return prompts.load(relative)


def _prompt(cfg: dict, key: str, prep: Prepared, **extra: str) -> str:
    """Render prompts.<key> from the episode's metadata and transcript plus `extra`."""
    return prompts.render(_template(cfg["prompts"][key]), {**prompt_values(prep, cfg), **extra})


def _role(cfg: dict, family: str) -> str:
    if family not in FAMILY_ROLES or FAMILY_ROLES[family] not in cfg["models"]:
        raise ValueError(f"unknown reference family {family!r}")
    return FAMILY_ROLES[family]


def _item_json(item: RefItem, *, with_id: bool) -> str:
    """One item as the matcher and presence prompts show it (importance is not shown:
    it bears on neither sameness nor presence)."""
    body = {name: getattr(item, name) for name in _MODEL_FIELDS if name != "importance"}
    return json.dumps({"id": item.id, **body} if with_id else body, ensure_ascii=False)


# ---------------------------------------------------------------- extraction

def is_mine(owner: Optional[str], owner_basis: str) -> bool:
    """The recording owner's own task: owner is the Me label, basis assigned or volunteered."""
    return owner == prepare.ME and owner_basis in MINE_BASES


def _extract(prep: Prepared, cfg: dict, family: str, ask: Ask, replicate: int) -> tuple[list[RefItem], int]:
    """(items, how many items' model `mine` flag differed from the derived one)."""
    if prep.is_stub:
        return [], 0
    out = ask(_role(cfg, family), _prompt(cfg, "reference", prep), _schema(cfg, "reference"),
              replicate=replicate)
    items, disagreements = [], 0
    for n, raw in enumerate(out["items"]):
        values = {k: raw[k] for k in _MODEL_FIELDS}
        mine = is_mine(values["owner"], values["owner_basis"])
        disagreements += mine != values["mine"]
        items.append(RefItem(f"{family}.{replicate}.{n}", family, **{**values, "mine": mine}))
    return items, disagreements


def extract(prep: Prepared, cfg: dict, family: str, ask: Ask, *, replicate: int = 0) -> list[RefItem]:
    """One family's items for one episode. A stub makes no call and has no items. Ids
    are deterministic: family, replicate and position, so a rerun never collides.
    `mine` is derived from owner and owner_basis, not taken from the model's flag."""
    return _extract(prep, cfg, family, ask, replicate)[0]


def normalize_quote(text: str) -> str:
    """NFKC, typographic quotes and dashes folded to ASCII, casefolded."""
    return unicodedata.normalize("NFKC", text).translate(_FOLD).casefold()


def _tokens(text: str) -> list[str]:
    """prepare's word tokens of the normalized text, with quote marks stripped from each
    token's ends (so a model's 'done' matches the transcript's done)."""
    return [w for w in (t.strip("'") for t in word_tokens(normalize_quote(text))) if w]


def _joined(tokens: Sequence[str]) -> str:
    """Space-delimited on both sides, so `in` matches whole tokens only."""
    return f" {' '.join(tokens)} "


def _turn_tokens(prep: Prepared) -> list[str]:
    return [_joined(_tokens(t.text)) for t in prep.turns]


def _quote_turns(tokens: Sequence[str], turns: Sequence[str]) -> frozenset[int]:
    """Indexes of the turns whose token sequence contains `tokens`, contiguously. A
    quote never matches across a turn boundary."""
    if not tokens:
        return frozenset()
    needle = _joined(tokens)
    return frozenset(n for n, turn in enumerate(turns) if needle in turn)


def quote_check(items: Sequence[RefItem], prep: Prepared, cfg: dict
                ) -> tuple[list[RefItem], list[RefItem]]:
    """(kept, rejected): an item is kept when its quote's word tokens appear, in order
    and contiguously, inside one turn of the transcript the model saw, and number at
    least `eval.reference.min_quote_words`."""
    floor = cfg["eval"]["reference"]["min_quote_words"]
    turns = _turn_tokens(prep)
    kept, rejected = [], []
    for item in items:
        tokens = _tokens(item.quote)
        (kept if len(tokens) >= floor and _quote_turns(tokens, turns) else rejected).append(item)
    return kept, rejected


# ---------------------------------------------------------------- quote identity

@dataclass(frozen=True)
class _Quote:
    tokens: tuple[str, ...]
    turns: frozenset[int]


def _quotes(items: Sequence[RefItem], prep: Prepared) -> dict[str, _Quote]:
    """Each item's quote tokens and the turns holding them, computed once."""
    turns = _turn_tokens(prep)
    out = {}
    for i in items:
        tokens = tuple(_tokens(i.quote))
        out[i.id] = _Quote(tokens, _quote_turns(tokens, turns))
    return out


def quote_relation(x: _Quote, y: _Quote, cfg: dict) -> str:
    """How two quotes sit in the transcript:
    SAME     identical tokens;
    OVERLAP  in a shared turn, one contains the other or a suffix of one equals a prefix
             of the other over at least `eval.reference.overlap_min_tokens` tokens;
    NEAR     within `eval.reference.near_miss_max_turns` turns of each other;
    FAR      anything else, including an empty quote."""
    limits = cfg["eval"]["reference"]
    p, q = x.tokens, y.tokens
    if not p or not q:
        return FAR
    if p == q:
        return SAME
    if x.turns & y.turns and (_joined(p) in _joined(q) or _joined(q) in _joined(p) or any(
            p[-k:] == q[:k] or q[-k:] == p[:k]
            for k in range(limits["overlap_min_tokens"], min(len(p), len(q)) + 1))):
        return OVERLAP
    if any(abs(i - j) <= limits["near_miss_max_turns"] for i in x.turns for j in y.turns):
        return NEAR
    return FAR


# ---------------------------------------------------------------- consensus

def relation(x: RefItem, y: RefItem) -> str:
    """May x and y be one item? REJECT a task with a non-task; CONFLICT two tasks that
    disagree on `mine`; else PAIR. Accepting a reject or conflict as a pair would settle
    the disagreement by family a winning, and a my-task could silently leave the primary
    endpoint's denominator."""
    if (x.type == TASK) != (y.type == TASK):
        return REJECT
    if x.type == TASK and x.mine != y.mine:
        return CONFLICT
    return PAIR


def _match(a: Sequence[RefItem], b: Sequence[RefItem], prep: Prepared, cfg: dict, ask: Ask, *,
           replicate: int, keep: frozenset[str]) -> list[tuple[str, str, str]]:
    """The matcher's pairs as (a id, b id, relation), one-to-one, in the matcher's order,
    keeping only pairs whose `relation` is in `keep`. A pair whose ids aren't one from
    `a` then one from `b`, or that reuses an id, is dropped. No call when either side is
    empty."""
    if not a or not b:
        return []
    prompt = _prompt(cfg, "matcher", prep,
                     ITEMS_A="\n".join(_item_json(i, with_id=True) for i in a),
                     ITEMS_B="\n".join(_item_json(i, with_id=True) for i in b))
    out = ask(MATCHER_ROLE, prompt, _schema(cfg, "matcher"), replicate=replicate)
    a_by_id, b_by_id = {i.id: i for i in a}, {i.id: i for i in b}
    used: set[str] = set()
    pairs = []
    for pair in out["pairs"]:
        x, y = pair["a"], pair["b"]
        if x in a_by_id and y in b_by_id and x not in used and y not in used:
            rel = relation(a_by_id[x], b_by_id[y])
            if rel in keep:
                used |= {x, y}
                pairs.append((x, y, rel))
    return pairs


def match(a: Sequence[RefItem], b: Sequence[RefItem], prep: Prepared, cfg: dict, ask: Ask, *,
          replicate: int = 0) -> list[tuple[str, str]]:
    """The matcher's pairs as (a id, b id), one-to-one, in the matcher's order. Only a
    PAIR `relation` is kept (enforced here, not only asked of the matcher)."""
    return [(x, y) for x, y, _ in _match(a, b, prep, cfg, ask, replicate=replicate, keep=frozenset({PAIR}))]


def _duplicates(items: Sequence[RefItem], quotes: dict[str, _Quote], cfg: dict) -> list[tuple[str, ...]]:
    """Groups of ids, within one family, that share type and quote."""
    groups: list[list[RefItem]] = []
    for i in items:
        for group in groups:
            first = group[0]
            if first.type == i.type and quote_relation(quotes[first.id], quotes[i.id], cfg) == SAME:
                group.append(i)
                break
        else:
            groups.append([i])
    return [tuple(i.id for i in g) for g in groups if len(g) > 1]


def consensus(a: Sequence[RefItem], b: Sequence[RefItem], pairs: Sequence[tuple[str, str]],
              prep: Prepared, cfg: dict, ask: Ask, *, replicate: int = 0,
              types: Optional[frozenset[str]] = None) -> Reference:
    """Matched items are accepted once (as the family-a item). Every singleton is asked
    present/absent of both families, and accepted only if both say present. With
    `types`, only singletons of those types are escalated; the others are left out of
    the reference entirely (the stability rerun passes {TASK}). Matching is unaffected.

    Then one more match runs over the accepted a-singletons vs the accepted b-singletons.
    A PAIR there is one item both families found: it collapses into its family-a item. A
    CONFLICT makes the truth ambiguous: both items move to contested."""
    by_id = {i.id: i for i in [*a, *b]}
    paired_a = {x for x, _ in pairs}
    paired_b = {y for _, y in pairs}
    schema = _schema(cfg, "presence")
    accepted, contested, verdicts = [], [], {}
    singles: dict[str, list[RefItem]] = {family: [] for family in FAMILY_ROLES}   # accepted, by family
    for item in [*a, *b]:
        if item.id in paired_a:
            accepted.append(item)
            continue
        if item.id in paired_b or (types is not None and item.type not in types):
            continue
        prompt = _prompt(cfg, "presence", prep, ITEM=_item_json(item, with_id=False))
        verdicts[item.id] = {fam: ask(_role(cfg, fam), prompt, schema, replicate=replicate)["verdict"]
                             for fam in FAMILY_ROLES}
        if all(v == PRESENT for v in verdicts[item.id].values()):
            accepted.append(item)
            singles[item.family].append(item)
        else:
            contested.append(item)

    collapsed, conflicted, removed = [], [], set()
    for x, y, rel in _match(singles["a"], singles["b"], prep, cfg, ask, replicate=replicate,
                            keep=frozenset({PAIR, CONFLICT})):
        if rel == PAIR:
            collapsed.append((x, y))
            removed.add(y)
        else:
            conflicted.append((x, y))
            removed |= {x, y}
            contested += [by_id[x], by_id[y]]
    quotes = _quotes([*a, *b], prep)
    return Reference(
        accepted=tuple(i for i in accepted if i.id not in removed),
        contested=tuple(contested),
        both_found=frozenset(paired_a | {x for x, _ in collapsed}),
        verdicts=verdicts, collapsed=tuple(collapsed), conflicted=tuple(conflicted),
        duplicates=tuple(_duplicates(a, quotes, cfg) + _duplicates(b, quotes, cfg)),
    )


def build(prep: Prepared, cfg: dict, ask: Ask, *, replicate: int = 0,
          types: Optional[frozenset[str]] = None) -> Reference:
    """The whole B.4 reference for one episode: both families, quote check, match,
    consensus. The stability rerun passes `types=frozenset({TASK})` (see `consensus`)."""
    kept, rejected, disagreements = {}, [], 0
    for family in FAMILY_ROLES:
        items, n = _extract(prep, cfg, family, ask, replicate)
        disagreements += n
        kept[family], dropped = quote_check(items, prep, cfg)
        rejected += dropped
    a, b = kept["a"], kept["b"]
    pairs = match(a, b, prep, cfg, ask, replicate=replicate)
    ref = consensus(a, b, pairs, prep, cfg, ask, replicate=replicate, types=types)
    return replace(ref, rejected=tuple(rejected), mine_disagreements=disagreements)


# ---------------------------------------------------------------- stability

def my_tasks(items: Sequence[RefItem]) -> list[RefItem]:
    """The recording owner's own tasks: the primary endpoint's denominator."""
    return [i for i in items if i.type == TASK and i.mine]


def jaccard(*, matched: int, n_a: int, n_b: int) -> float:
    """|A and B| / |A or B|; two empty sets are identical (1.0)."""
    if not 0 <= matched <= min(n_a, n_b):
        raise ValueError(f"matched={matched} is impossible for sets of {n_a} and {n_b}")
    union = n_a + n_b - matched
    return matched / union if union else 1.0


def rerun_counts(first: Sequence[RefItem], second: Sequence[RefItem], prep: Prepared, cfg: dict,
                 ask: Ask) -> tuple[int, int, int]:
    """(matched, n_first, n_second) over the my-tasks of two reference runs on one
    transcript, paired by the matcher: one input row for `stable`."""
    x, y = my_tasks(first), my_tasks(second)
    return len(match(x, y, prep, cfg, ask)), len(x), len(y)


def stable(counts: Sequence[tuple[int, int, int]], cfg: dict) -> bool:
    """B.4 stability gate. `counts` holds (matched, n_a, n_b) per rerun transcript; the
    Jaccard is pooled over them, so a transcript with no my-tasks adds nothing."""
    if not counts:
        raise ValueError("no rerun transcripts: the stability gate has nothing to measure")
    matched, n_a, n_b = (sum(c[k] for c in counts) for k in range(3))
    return jaccard(matched=matched, n_a=n_a, n_b=n_b) >= cfg["eval"]["stability_min_jaccard"]


# ---------------------------------------------------------------- matcher calibration

def _owners_compatible(x: RefItem, y: RefItem) -> bool:
    """Not known to be different people: either owner unknown (None), or one name's
    tokens a prefix of the other's ("Sam" vs "Sam Lee"), case-insensitively."""
    if x.owner is None or y.owner is None:
        return True
    p, q = _tokens(x.owner), _tokens(y.owner)
    n = min(len(p), len(q))
    return p[:n] == q[:n]


def calibration_pairs(a: Sequence[RefItem], b: Sequence[RefItem], prep: Prepared, cfg: dict
                      ) -> tuple[list[tuple[RefItem, RefItem]], list[tuple[RefItem, RefItem]]]:
    """(positives, negatives) across the two families, in (a, b) order. Both need the
    same type and a PAIR `relation` (anything else match() drops in code, a free "no").
    Any other pair is ambiguous and left out.

    Positive (paraphrase): SAME quote, carried by no other item in either list.
    Negative (near miss): NEAR quotes (not overlapping, within
    `eval.reference.near_miss_max_turns` turns), and not two tasks with compatible owners
    (they may be the request and the reply of one task).

    Known limitation: a quote carried by several tasks (two tasks from one sentence)
    never yields a positive, since no code-only signal tells that from a paraphrase."""
    quotes = _quotes([*a, *b], prep)
    carriers = Counter(q.tokens for q in quotes.values())
    positives, negatives = [], []
    for x in a:
        for y in b:
            if x.type != y.type or relation(x, y) != PAIR:
                continue
            where = quote_relation(quotes[x.id], quotes[y.id], cfg)
            if where == SAME and carriers[quotes[x.id].tokens] == 2:
                positives.append((x, y))
            elif where == NEAR and not (x.type == TASK and _owners_compatible(x, y)):
                negatives.append((x, y))
    return positives, negatives


def matcher_outcomes(a: Sequence[RefItem], b: Sequence[RefItem],
                     positives: Sequence[tuple[RefItem, RefItem]], negatives: Sequence[tuple[RefItem, RefItem]],
                     prep: Prepared, cfg: dict, ask: Ask) -> tuple[list[bool], list[bool]]:
    """Whether the matcher paired each positive and each negative, from ONE match call
    over the full lists `a` and `b`, so every other item is a distractor, as in use.
    No call when there is no calibration pair to report on."""
    if not positives and not negatives:
        return [], []
    found = set(match(a, b, prep, cfg, ask))
    return ([(x.id, y.id) in found for x, y in positives],
            [(x.id, y.id) in found for x, y in negatives])


def matcher_rates(pos_paired: Sequence[bool], neg_paired: Sequence[bool]
                  ) -> tuple[Optional[float], Optional[float]]:
    """(sensitivity, specificity): the share of positives paired and of negatives left
    apart. None for an empty side. Pool transcripts by concatenating their outcomes."""
    sens = sum(pos_paired) / len(pos_paired) if pos_paired else None
    spec = sum(not p for p in neg_paired) / len(neg_paired) if neg_paired else None
    return sens, spec
