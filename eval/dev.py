"""The prompt-tuning loop (stage "dev", 2026-10-05 amendment): each revision of the
extract prompt runs on the tuning set (eval/frame.py Sample.dev) and is scored on the
north star's my-task measures only, so revisions are cheap and the confirmatory draw
stays unseen until the tuned prompt is judged on it.

A run is the pilot's machinery (eval/pilot.py _Pilot) narrowed:
- extract with the CLI's default system prompt only (H-S2 found no gain for the minimal one);
- the reference, as the pilot builds it (cached by request key, so paid once per
  transcript across revisions);
- judge decisions DECISIONS on the summary's my-task subjects only (pilot._subject_items
  mine_only): every reference my-task for presence, the My Actions completeness check,
  and the owner of every task the summary lists as mine;
- no calibration and no probe: the pilot measured both for these prompts and models.

`measures` adds to the report: my-task recall (partial = 0.5), design-weighted with
its CI over the tuning design and unweighted, the share of summaries whose My Actions is
complete (among transcripts whose reference has a my-task), the owner-correct share of
the summary's My Actions tasks, every miss, partial and wrong owner with the reference
text and quote (the input for root-causing the next revision), and coverage: a
transcript whose extract failed scores every reference my-task as a miss, so a revision
cannot raise recall by breaking extraction; my-tasks left unjudged (judge errors, a
budget stop) and transcripts with no reference are counted, not silently dropped.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from eval import frame as F
from eval import judge as J
from eval import ledger as L
from eval import pilot as P
from eval import reference as R
from eval import scoring as S
from eval import stages
from eval.records import Outcome

STAGE = "dev"
DECISIONS = ("present", "my_actions", "task_owner")
RECALL = "my_task_recall"            # the B.6 metric name the scorecard uses
CONFIG = "dev"                       # Outcome.config label for the revision under test
EXTRACT_FAILED = "extract failed"     # the miss answer for a my-task whose summary was never made


class _Dev(P._Pilot):
    def __init__(self, cfg, ask, out_path):
        super().__init__(cfg, ask, out_path)
        self.variants = {stages.DEFAULT_SYSTEM: None}
        self.decisions, self.mine_only = DECISIONS, True

    def calibrate(self, unit, prep) -> None:
        """No calibration in the tuning loop (module doc)."""


def _judged(result: P.Result, decision: str) -> list[dict]:
    return [r for r in result.records if r["step"] == P.JUDGE and r["decision"] == decision and r.get("verdict")]


def measures(cfg: dict, result: P.Result, design: Optional[F.Design] = None) -> dict:
    """The tuning loop's measurements (module doc). `design` gives the weighted estimate
    and its CI; without one the estimate is the unweighted mean."""
    recs = result.records
    mine = {r["unit"]: {i.id: i for i in R.my_tasks([R.RefItem(**x) for x in r["accepted"]])}
            for r in recs if r["step"] == P.REFERENCE}
    extracted = {r["unit"] for r in recs if r["step"] == P.EXTRACT}
    live = [r["unit"] for r in recs if r["step"] == P.PREPARED]
    present = [r for r in _judged(result, "present") if r.get("mine")]
    failed = [(u, item) for u, items in mine.items() if u not in extracted for item in items.values()]
    outcomes = [Outcome(r["unit"], CONFIG, RECALL, r["item_id"], J.PRESENT_VALUE[r["verdict"]["value"]])
                for r in present] + [Outcome(u, CONFIG, RECALL, item.id, 0.0) for u, item in failed]
    values = [o.value for o in outcomes]
    unweighted = sum(values) / len(values) if values else None
    recall = {"estimate": unweighted, "unweighted": unweighted, "n_tasks": len(values),
              "n_transcripts": len({o.unit_id for o in outcomes})}
    if design is not None and outcomes:
        recall["scorecard"] = S.metric_entry(design, outcomes, CONFIG, RECALL, cfg=cfg)
        recall["estimate"] = recall["scorecard"]["raw"]["estimate"]
    judged_ids = {(r["unit"], r["item_id"]) for r in present}
    with_tasks = {u for u, items in mine.items() if items}
    complete = [r for r in _judged(result, "my_actions") if r["unit"] in with_tasks]
    owners = _judged(result, "task_owner")
    by_unit = defaultdict(list)
    for r in present:
        if r["verdict"]["value"] != "yes":
            item = mine[r["unit"]].get(r["item_id"])
            by_unit[r["unit"]].append({"answer": r["verdict"]["value"], "item": r["subject"],
                                       "quote": item.quote if item else ""})
    for u, item in failed:
        by_unit[u].append({"answer": EXTRACT_FAILED, "item": item.text, "quote": item.quote})
    return {"dev": {
        "my_task_recall": recall,
        "bar": cfg["eval"]["bar"],
        "my_actions_complete": {"yes": sum(r["verdict"]["value"] == "yes" for r in complete), "n": len(complete)},
        "my_actions_owner_correct": {"yes": sum(r["verdict"]["value"] == "yes" for r in owners), "n": len(owners)},
        "misses": dict(by_unit),
        "wrong_owner": [{"unit": r["unit"], "task": r["subject"]} for r in owners if r["verdict"]["value"] != "yes"],
        "coverage": {"live": len(live), "with_reference": len(mine), "extract_failed": sorted({u for u, _ in failed}),
                     "no_reference": [u for u in live if u not in mine],
                     "unjudged_my_tasks": sum(1 for u, items in mine.items() if u in extracted
                                              for i in items if (u, i) not in judged_ids)},
    }}


def stage_spend(cfg: dict) -> dict:
    """Spend on the dev stage across every revision so far, against its cap."""
    return {"stage_spent_usd": L.tally(cfg)[1].get(STAGE, 0.0), "stage_cap_usd": cfg["eval"]["stages"][STAGE]["cap_usd"]}


def _share(c: dict) -> str:
    return f"{c['yes']} of {c['n']}" + (f" ({c['yes'] / c['n']:.0%})" if c["n"] else "")


def markdown(rep: dict) -> str:
    """The tuning report: the my-task measures first, then the pilot report's cost tables."""
    d = rep.get("dev", {})
    r = d.get("my_task_recall", {})
    lines = [f"# Tuning run {rep.get('run_id', '')}".rstrip(), ""]
    if rep.get("error"):
        lines.append(f"RUN FAILED: {rep['error']}")
    elif rep.get("stopped"):
        lines.append(f"Stopped on budget: {rep['budget_stop']}")
    if r:
        card = r.get("scorecard")
        weighted = (f"weighted {P._fmt(r['estimate'])}, CI ({card.get('ci_method')}) "
                    f"{P._fmt(card['raw']['ci'][0])}-{P._fmt(card['raw']['ci'][1])}; unweighted " if card else "")
        cov = d["coverage"]
        lines += [f"My-task recall: {weighted}{P._fmt(r['unweighted'])} ({r['n_tasks']} tasks in "
                  f"{r['n_transcripts']} transcripts); bar {d['bar']['my_task_recall']}.",
                  f"My Actions complete: {_share(d['my_actions_complete'])} (transcripts with a reference my-task).",
                  f"My Actions owner correct: {_share(d['my_actions_owner_correct'])}.",
                  f"Coverage: {cov['with_reference']} of {cov['live']} live transcripts have a reference; "
                  f"extract failed (scored as misses): {cov['extract_failed'] or 'none'}; "
                  f"no reference (not scored): {cov['no_reference'] or 'none'}; "
                  f"my-tasks left unjudged: {cov['unjudged_my_tasks']}.", "",
                  "## Misses and partials", ""]
        for unit, items in d["misses"].items():
            lines += [f"- {unit}: {m['answer']}: {m['item']}" + (f' (quote: "{m["quote"]}")' if m["quote"] else "")
                      for m in items]
        lines += ["", "## Listed as mine, owner wrong", ""]
        lines += [f"- {w['unit']}: {w['task']}" for w in d["wrong_owner"]] or ["- none"]
        lines.append("")
    lines += [f"Errors (unusable model answers): {rep['errors']}. Spent this invocation: "
              f"${rep['spent_this_invocation_usd']:.4f}."
              + (f" Dev stage so far: ${d['stage_spent_usd']:.2f} of ${d['stage_cap_usd']:.2f}." if "stage_spent_usd" in d
                 else ""), ""]
    lines += P._cost_table("Cost by step", rep["cost_by_step"]) + P._cost_table("Cost by role", rep["cost_by_role"])
    return "\n".join(lines)


def execute(cfg: dict, eval_run, run_dir, jobs, cache, cap, *, design: Optional[F.Design] = None) -> None:
    """What `python -m eval run --stage dev` does after planning: pilot.execute with this
    module's runner, no probe, and its measures and report."""
    def extend(rep: dict, result: P.Result) -> dict:
        m = measures(cfg, result, design)
        m["dev"].update(stage_spend(cfg))
        return m
    P.execute(cfg, eval_run, run_dir, jobs, cache, cap, runner=_Dev, probing=False, extend=extend, markdown=markdown)
