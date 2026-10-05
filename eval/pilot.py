"""The pilot (issue #13; docs/plan/README.md §7 Phase 1, B.3, B.9): a few transcripts
through every step inside eval.stages.pilot.cap_usd, to measure cost per step and
role, test H-S2, estimate the within-transcript correlation that sizes n, and try the
judge and matcher calibration once.

    extract (both system variants, H-S2) -> reference -> judge (default variant)
    -> judge (minimal variant) -> calibration (plants, positives, matcher)

- Units run one at a time, smallest transcript first, each through every step
  before the next unit starts, so a budget stop still leaves a measured cost for
  every step from at least the smallest unit (step-major order let the reference
  step's per-call reservations exhaust the cap before any judge call). A stub makes
  no call and gets one "skipped" record; every other unit gets a "prepared" record
  up front (its echo removal counts, B.8).
- eval.ledger.BudgetStop is an expected outcome: it becomes a "budget_stop" record
  and `run` returns normally; Result.stopped says so.
- A model answer that fails its schema or its vocabulary (calls.OutputError,
  judge.JudgeError) becomes an "error" record and that item is skipped. Anything
  else (auth, CLI, timeout) propagates: the run is broken, not the answer.
- A step's judge decisions (and the calibration plants) run together on eval.workers
  threads (`batch`, eval.ask.fan_out); their records are emitted in the order a
  one-at-a-time loop emits them. A batch a budget stop ends keeps every finished job's
  record: which jobs finished depends on timing (calls in flight are each reserved at
  their cap, so the stop can come a few calls earlier), never on an answer.
- Every record is appended to the output JSONL as it happens, and every call entry
  to calls.jsonl beside it, so a crash keeps all work done so far and the cost join
  (`load` reads both back).
- Every call goes through the injected `ask`, wrapped to log (step, role, request
  key), the key exactly as the ledger records it, so `report` joins the ledger for
  measured cost by step and role. A call the budget guard refused never launched
  and is not logged.
- judge.check(cfg) runs before any model call: a broken judge config fails loudly
  before spend instead of turning every answer into an "error" record.
- Judge subjects are capped at eval.stages.pilot.judge_subjects per decision and
  chosen deterministically (`subjects`); every reference my-task is always judged
  for presence, since my-task presence is what sizes n (B.3).
- `probe` checks whether the CLI accepts `--setting-sources ""` (deferred from #8),
  on its own small per-call cap (eval.stages.pilot.probe_max_budget_usd). In `execute`
  its calls are logged like the steps', as step "probe", so its cost is a step cost.
- `execute` is what `python -m eval run --stage pilot` does after planning: every
  step, then the probe, then report.json/report.md in the run's results directory.
"""

from __future__ import annotations

import copy
import json
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from eval import judge as J
from eval import ledger as L
from eval import reference as R
from eval import scoring as S
from eval import stages
from eval.ask import Ask, fan_out, make_ask, values
from pipeline import calls, extract, render
from pipeline.models import TOKEN_FIELDS, ModelCallError, append_jsonl, read_jsonl
from pipeline.prepare import Prepared, owner_names   # names a plant must never choose as a person
from whispr.fileio import atomic_write_text

# Record steps. extract..calibration are the priority order; the rest are bookkeeping.
PREPARED, SKIPPED, EXTRACT, REFERENCE, JUDGE, CALIBRATION, ERROR, BUDGET_STOP = (
    "prepared", "skipped", "extract", "reference", "judge", "calibration", "error", "budget_stop")
PROBE = "probe"     # the --setting-sources probe's step in the call log (it writes no records)
JUDGED, NOT_APPLICABLE = "judged", "not_applicable"     # calibration record status
MATCHER = "matcher"                                     # calibration record kind for the matcher
UNUSABLE = (calls.OutputError, J.JudgeError)    # a model answer that becomes an "error" record

# Files a pilot run writes into results/<run_id>/.
RECORDS_FILE, CALLS_FILE, REPORT_JSON, REPORT_MD = "pilot.jsonl", "calls.jsonl", "report.json", "report.md"

CLAIM_DECISIONS = ("supported", "attribution")          # subjects: summary claims
TASK_DECISIONS = ("task_owner", "task_due", "actionable")   # subjects: rendered tasks
POSITIVE_DECISION = "supported"     # a planted positive is a true claim the faithfulness judge must pass

# The --setting-sources probe (#8): one tiny call with and without these arguments.
NO_SETTINGS_ARGS = ("--setting-sources", "")
PROBE_PROMPT = 'Reply with the JSON {"ok": true}.'
PROBE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}}}

# What the dry run says about the steps it cannot list ahead of the run.
DRY_RUN_NOTE = "reference, judge and calibration calls are planned at run time from the extract outputs"
JUDGE_COST_NOTE = ("order-confounded: the default variant is judged first and warms the prompt-cache prefix both "
                   "variants share, and the default system prompt also shares the CLI's cached system prompt")


@dataclass
class Result:
    records: list[dict] = field(default_factory=list)   # as written to the JSONL, in order
    calls: list[dict] = field(default_factory=list)     # {"step", "role", "key"[, "variant"]} per call

    @property
    def stopped(self) -> bool:
        """Whether the run ended on a budget stop."""
        return any(r["step"] == BUDGET_STOP for r in self.records)


# --- helpers the run and the tests share ---------------------------------------------

def detected(decision: str, value: str) -> bool:
    """Whether a verdict flags an error (judge.PASS); ValueError for an unknown decision."""
    return J.flags_error(decision, value)


def _capped(items: Sequence, k: int, rng: random.Random) -> list:
    """At most k of `items`, chosen by `rng`, kept in their own order."""
    return [items[i] for i in sorted(rng.sample(range(len(items)), min(k, len(items))))]


def _subject_items(decision: str, doc: dict, reference_items: Sequence[R.RefItem], cfg: dict, *,
                   key: str) -> list[tuple[str, Optional[R.RefItem]]]:
    """(subject, the reference item it shows or None) for one decision on one summary."""
    k = cfg["eval"]["stages"]["pilot"]["judge_subjects"]
    rng = random.Random(f"{cfg['eval']['seed']}:{key}:{decision}")
    mine = R.my_tasks(reference_items)
    if decision == "present":
        mine_ids = {i.id for i in mine}
        others = [i for i in reference_items if i.id not in mine_ids]
        return [(R.item_json(i, with_id=False), i) for i in [*mine, *_capped(others, k, rng)]]
    if decision == "my_actions":
        return [("\n".join(R.item_json(i, with_id=True) for i in mine) or render.NONE, None)]
    if decision in CLAIM_DECISIONS:
        return [(c, None) for c in _capped(J.summary_claims(doc), k, rng)]
    if decision in TASK_DECISIONS:
        return [(t, None) for t in _capped([line for _, line in render.tasks(doc)], k, rng)]
    raise ValueError(f"unknown decision {decision!r}")


def subjects(decision: str, doc: dict, reference_items: Sequence[R.RefItem], cfg: dict, *,
             key: str) -> list[str]:
    """What one decision judges on one summary, deterministic for (cfg seed, `key`):
    - present: every reference my-task, plus up to k other reference items;
    - supported, attribution: up to k of judge.summary_claims(doc);
    - task_owner, task_due, actionable: up to k tasks, each rendered on its own;
    - my_actions: one subject listing the reference my-tasks with their ids.
    k is eval.stages.pilot.judge_subjects; reference items show as the judge's
    presence prompt shows them (reference.item_json)."""
    return [s for s, _ in _subject_items(decision, doc, reference_items, cfg, key=key)]


def _task_subject(doc: dict, action: str) -> str:
    """The rendered task in `doc` whose action is `action`."""
    for act, line in render.tasks(doc):
        if act == action:
            return line
    raise ValueError(f"no task {action!r} in the document")


def _plant_subject(planted: J.Planted, original: dict, decision: str) -> str:
    """What the judge is asked about for a plant. Planted.target is the exact claim
    for claim decisions and the task's action otherwise. A deleted my-task (present)
    is asked about as it stood in the original summary."""
    if decision in CLAIM_DECISIONS:
        return planted.target
    return _task_subject(original if decision == "present" else planted.doc, planted.target)


def _verdict(record: dict) -> J.Verdict:
    """A record's verdict dict back as a judge.Verdict."""
    v = record["verdict"]
    return J.Verdict(**{**v, "samples": tuple(v["samples"]), "missing": tuple(v["missing"])})


# --- the run ------------------------------------------------------------------------

class _Pilot:
    def __init__(self, cfg: dict, ask: Ask, out_path: Path):
        self.cfg, self.out = cfg, Path(out_path)
        self.ask = self.logged(cfg, ask)        # the injected ask, every launched call logged
        self.result = Result()
        self.variants = stages.system_variants(cfg)
        self.step: Optional[str] = None
        self.variant: Optional[str] = None
        self.docs: dict[tuple[str, str], dict] = {}     # (unit, variant) -> extract document
        self.refs: dict[str, R.Reference] = {}

    def logged(self, cfg: dict, ask: Ask) -> Ask:
        """`ask`, built from `cfg`, wrapped to log (step, role, request key) for every
        launched call under the current step and variant; the key is computed from `cfg`,
        exactly as the ledger records it."""
        def wrapped(role: str, prompt: str, schema: dict, *, system_prompt: Optional[str] = None,
                    replicate: int = 0) -> dict:
            entry = {"step": self.step, "role": role,
                     "key": calls.request_key(cfg, role, prompt, schema, system_prompt, replicate)}
            if self.variant:
                entry["variant"] = self.variant
            try:
                out = ask(role, prompt, schema, system_prompt=system_prompt, replicate=replicate)
            except L.BudgetStop:
                raise                           # refused before launch: no call, no ledger row
            except BaseException:
                self.log_call(entry)            # launched and failed: its ledger row still lands
                raise
            self.log_call(entry)
            return out
        return wrapped

    def probe(self, make_ask_fn: Callable[[dict], Ask]) -> dict:
        """The --setting-sources probe (module `probe`), its calls logged as step PROBE
        so its cost lands in the report's cost by step."""
        self.step, self.variant = PROBE, None
        return probe(self.cfg, lambda c: self.logged(c, make_ask_fn(c)))

    def log_call(self, entry: dict) -> None:
        append_jsonl(self.out.with_name(CALLS_FILE), entry)
        self.result.calls.append(entry)

    def emit(self, record: dict) -> None:
        append_jsonl(self.out, record)
        self.result.records.append(json.loads(json.dumps(record)))     # exactly as the file holds it

    def batch(self, jobs: Sequence[Callable[[], dict]]) -> None:
        """Run record-making jobs together (eval.ask.fan_out) and emit their records in job
        order. Every finished job's record is emitted before any job's error (a budget stop
        included) is raised, since its calls are paid."""
        outcomes = fan_out(self.cfg, jobs)
        for done in outcomes:
            if done is not None and done.error is None:
                self.emit(done.value)
        values(outcomes)

    def error_record(self, unit: str, exc: Exception, **context) -> dict:
        return {"step": ERROR, "unit": unit, "during": self.step, **context, "error": f"{type(exc).__name__}: {exc}"}

    def attempt(self, unit: str, fn: Callable, **context):
        """fn(), or None after an "error" record when a model answer is unusable."""
        try:
            return fn()
        except UNUSABLE as exc:
            self.emit(self.error_record(unit, exc, **context))
            return None

    def judged(self, unit: str, decision: str, transcript: str, summary: str, subject: str, record: dict,
               **context) -> Callable[[], dict]:
        """A `batch` job: `record` with the verdict on `subject`, or an "error" record when a
        model answer is unusable."""
        prompt = J.build_prompt(decision, self.cfg, transcript=transcript, summary=summary, subject=subject)

        def job() -> dict:
            try:
                verdict = J.decide(decision, prompt, self.cfg, self.ask)
            except UNUSABLE as exc:
                return self.error_record(unit, exc, decision=decision, **context)
            return {**record, "verdict": asdict(verdict), "detected": detected(decision, verdict.value)}
        return job

    # -- steps

    def run(self, units: Iterable[tuple[str, Prepared]]) -> Result:
        J.check(self.cfg)                       # before any spend
        self.out.parent.mkdir(parents=True, exist_ok=True)
        for path in (self.out, self.out.with_name(CALLS_FILE)):
            path.write_text("", encoding="utf-8")
        units = list(units)
        live = sorted((u for u in units if not u[1].is_stub), key=lambda u: (u[1].words, u[0]))
        for unit, prep in units:
            if prep.is_stub:
                self.emit({"step": SKIPPED, "unit": unit, "reason": "stub: no model call", "words": prep.words})
        for unit, prep in live:
            self.emit({"step": PREPARED, "unit": unit, "words": prep.words, "turns": len(prep.turns),
                       "echo_dropped": len(prep.echo_dropped), "redactions": prep.redactions,
                       "aliases_rewritten": prep.aliases_rewritten})
        try:
            for unit, prep in live:
                self.extract(unit, prep)
                self.reference(unit, prep)
                for variant in self.variants:
                    self.judge(unit, prep, variant)
                self.calibrate(unit, prep)
        except L.BudgetStop as exc:
            self.emit({"step": BUDGET_STOP, "error": str(exc)})
        return self.result

    def extract(self, unit: str, prep: Prepared) -> None:
        self.step = EXTRACT
        for variant, system in self.variants.items():
            self.variant = variant
            req = extract.build_request(prep, self.cfg, role=stages.EXTRACTOR, system_prompt=system)
            doc = self.attempt(unit, lambda: self.ask(req.role, req.prompt, req.schema, system_prompt=system),
                               variant=variant)
            if doc is not None:
                self.docs[unit, variant] = doc
                self.emit({"step": EXTRACT, "unit": unit, "variant": variant, "key": req.key,
                           "template_sha256": req.template_sha256, "schema_sha256": req.schema_sha256, "doc": doc})
        self.variant = None

    def reference(self, unit: str, prep: Prepared) -> None:
        self.step = REFERENCE
        ref = self.attempt(unit, lambda: R.build(prep, self.cfg, self.ask))
        if ref is None:
            return
        self.refs[unit] = ref
        self.emit({"step": REFERENCE, "unit": unit, "my_tasks": len(R.my_tasks(ref.accepted)),
                   "accepted": [asdict(i) for i in ref.accepted], "contested": [asdict(i) for i in ref.contested],
                   "rejected": [asdict(i) for i in ref.rejected], "both_found": sorted(ref.both_found),
                   "verdicts": ref.verdicts, "collapsed": ref.collapsed, "conflicted": ref.conflicted,
                   "duplicates": ref.duplicates, "mine_disagreements": ref.mine_disagreements})

    def judge(self, unit: str, prep: Prepared, variant: str) -> None:
        """Every decision on one variant's summary of the unit. It needs both the summary
        and the reference (an error record already says why one is missing)."""
        doc, ref = self.docs.get((unit, variant)), self.refs.get(unit)
        if doc is None or ref is None:
            return
        self.step, self.variant = JUDGE, variant
        transcript, summary = prep.render(), render.render(doc)
        self.batch([self.judged(unit, decision, transcript, summary, subject,
                                {"step": JUDGE, "unit": unit, "variant": variant, "decision": decision,
                                 "subject": subject, **({"item_id": item.id, "mine": item.mine} if item else {})},
                                variant=variant)
                    for decision in J.DECISIONS
                    for subject, item in _subject_items(decision, doc, ref.accepted, self.cfg, key=unit)])
        self.variant = None

    def calibrate(self, unit: str, prep: Prepared) -> None:
        """Plants, then positives, then the matcher, for one unit. Plants and positives go
        into the default variant's summary; the matcher needs the unit's reference."""
        self.step = CALIBRATION
        pc, seed = self.cfg["eval"]["stages"]["pilot"], self.cfg["eval"]["seed"]
        doc = self.docs.get((unit, stages.DEFAULT_SYSTEM))
        if doc is not None:
            transcript, exclude = prep.render(), owner_names(self.cfg)
            jobs = [self.plant_and_judge(unit, transcript, doc, {"kind": kind, "seed": seed + n},
                                         partial(J.plant, kind, doc, seed + n, exclude=exclude))
                    for kind in J.PLANT_KINDS for n in range(pc["plants_per_kind"])]
            try:
                claims = J.plant_positives(prep, seed, pc["positives"])
            except J.NotApplicable as exc:
                jobs.append(partial(self.not_applicable_record, unit, {"kind": J.POSITIVE, "seed": seed}, exc))
                claims = []
            jobs += [self.plant_and_judge(unit, transcript, doc, {"kind": J.POSITIVE, "seed": seed + n},
                                          partial(J.plant_positive, doc, claim, seed + n))
                     for n, claim in enumerate(claims)]
            self.batch(jobs)
        if unit in self.refs:
            self.calibrate_matcher(unit, prep, self.refs[unit])

    def not_applicable_record(self, unit: str, about: dict, reason) -> dict:
        return {"step": CALIBRATION, "unit": unit, **about, "status": NOT_APPLICABLE, "reason": str(reason)}

    def not_applicable(self, unit: str, about: dict, reason) -> None:
        self.emit(self.not_applicable_record(unit, about, reason))

    def plant_and_judge(self, unit: str, transcript: str, doc: dict, about: dict,
                        make: Callable[[], J.Planted]) -> Callable[[], dict]:
        """A `batch` job for the plant make() makes now: judging it, or a not_applicable
        record when the plant has nothing to act on (or, for a positive, nowhere to go)."""
        try:
            planted = make()
        except J.NotApplicable as exc:
            return partial(self.not_applicable_record, unit, about, exc)
        return self.judge_plant(unit, transcript, planted, doc, {"step": CALIBRATION, "unit": unit, **about})

    def calibrate_matcher(self, unit: str, prep: Prepared, ref: R.Reference) -> None:
        a, b = (ref.kept[family] for family in R.FAMILY_ROLES)
        positives, negatives = R.calibration_pairs(a, b, prep, self.cfg)
        base = {"step": CALIBRATION, "unit": unit, "kind": MATCHER}
        if not positives and not negatives:
            self.not_applicable(unit, {"kind": MATCHER}, "no paraphrase or near-miss pair")
            return
        outcome = self.attempt(unit, lambda: R.matcher_outcomes(a, b, positives, negatives, prep, self.cfg, self.ask),
                               kind=MATCHER)
        if outcome is not None:
            self.emit({**base, "status": JUDGED,
                       "positives": [[x.id, y.id, hit] for (x, y), hit in zip(positives, outcome[0])],
                       "negatives": [[x.id, y.id, hit] for (x, y), hit in zip(negatives, outcome[1])]})

    def judge_plant(self, unit: str, transcript: str, planted: J.Planted, original: dict,
                    base: dict) -> Callable[[], dict]:
        decision = POSITIVE_DECISION if planted.kind == J.POSITIVE else J.KIND_DECISION[planted.kind]
        subject = _plant_subject(planted, original, decision)
        return self.judged(unit, decision, transcript, render.render(planted.doc), subject,
                           {**base, "decision": decision, "status": JUDGED, "target": planted.target,
                            "subject": subject}, kind=planted.kind)


def run(units: Iterable[tuple[str, Prepared]], cfg: dict, ask: Ask, out_path: Path) -> Result:
    """Run the pilot over (unit id, prepared transcript) pairs, writing every record to
    `out_path` and every call entry to CALLS_FILE beside it (both truncated first) as
    they happen. Returns normally on a budget stop."""
    return _Pilot(cfg, ask, out_path).run(units)


def load(out_path: Path) -> Result:
    """A run's Result read back from `out_path` and its CALLS_FILE, e.g. after the run
    raised. Missing files read as empty."""
    out_path = Path(out_path)
    return Result(read_jsonl(out_path), read_jsonl(out_path.with_name(CALLS_FILE)))


# --- report -------------------------------------------------------------------------

def _cost() -> dict:
    return {"calls": 0, "usd": 0.0, **dict.fromkeys(TOKEN_FIELDS, 0)}


def _add(total: dict, row: dict) -> None:
    total["calls"] += 1
    total["usd"] += L.row_cost(row)
    for name in TOKEN_FIELDS:
        total[name] += int(row.get(name) or 0)


def _judge_values(records: list[dict]) -> dict:
    """Per decision: verdict count, answers and the share detected as an error."""
    shares = J.rates((r["decision"], r["detected"]) for r in records)
    values = defaultdict(Counter)
    for r in records:
        values[r["decision"]][r["verdict"]["value"]] += 1
    return {d: {"n": sum(values[d].values()), "values": dict(values[d]), "detected_share": shares[d]}
            for d in J.DECISIONS if d in shares}


def report(result: Result, ledger_rows: Iterable[dict], cfg: dict, *, new_rows: Optional[Iterable[dict]] = None,
           error: Optional[str] = None) -> dict:
    """The pilot's measurements.

    Cost: every call row of `ledger_rows` (pass the whole ledger) whose request key is
    in this run's call log, attributed to the step, role and variant of the first
    logged call with that key. Every row of a key counts (a retry is real spend), and
    rows from earlier invocations count too, so a resumed pilot still shows the cost of
    the steps its cache served. `new_rows` are the rows this invocation appended
    (default: all of `ledger_rows`): they give `spent_this_invocation_usd`, each step's
    `cached_calls` (logged calls whose key got no new row) and any unattributed cost: a
    new row whose key no logged call has, which a complete call log never leaves.
    `error` is the exception a crashed run raised (its report is built from whatever
    was written)."""
    recs = result.records
    rows = L.call_rows(ledger_rows)
    new = rows if new_rows is None else L.call_rows(new_rows)
    by_key: dict[str, dict] = {}
    for c in result.calls:
        by_key.setdefault(c["key"], c)
    by_role, by_step, by_variant = {}, {}, {}
    for row in rows:
        call = by_key.get(row.get("request_key"))
        if call is not None:
            _add(by_role.setdefault(call["role"], _cost()), row)
            _add(by_step.setdefault(call["step"], _cost()), row)
            if call.get("variant"):
                _add(by_variant.setdefault((call["step"], call["variant"]), _cost()), row)
    fresh = Counter(r.get("request_key") for r in new)
    for c in result.calls:
        step = by_step.setdefault(c["step"], _cost())
        step["cached_calls"] = step.get("cached_calls", 0) + (fresh[c["key"]] == 0)
    unattributed = _cost()
    for row in new:
        if row.get("request_key") not in by_key:
            _add(unattributed, row)

    variants = list(stages.system_variants(cfg))
    judged = [r for r in recs if r["step"] == JUDGE]
    # My-task presence, clustered by transcript, per variant: H-S2 recall and B.3 sizing.
    presence: dict[str, dict[str, list[float]]] = {v: defaultdict(list) for v in variants}
    for r in judged:
        if r["decision"] == "present" and r.get("mine"):
            presence[r["variant"]][r["unit"]].append(J.PRESENT_VALUE[r["verdict"]["value"]])

    def recall(v: str) -> dict:
        values = [x for unit in presence[v].values() for x in unit]
        return {"estimate": sum(values) / len(values) if values else None, "n": len(values)}
    h_s2 = {v: {"extract_calls": sum(1 for c in result.calls if c["step"] == EXTRACT and c.get("variant") == v),
                "my_task_recall": recall(v),
                "extract_cost": by_variant.get((EXTRACT, v), _cost()),
                "judge_cost": by_variant.get((JUDGE, v), _cost()),
                "judge_values": _judge_values([r for r in judged if r["variant"] == v])}
            for v in variants}

    counts = [r["my_tasks"] for r in recs if r["step"] == REFERENCE]
    per_transcript = sum(counts) / len(counts) if counts else 0.0
    icc = {v: S.icc(list(presence[v].values())) for v in variants}
    needed = {v: S.transcripts_needed(rho=icc[v], tasks_per_transcript=per_transcript,
                                      min_effective=cfg["eval"]["min_effective_tasks"]) for v in variants}

    plants = [r for r in recs if r["step"] == CALIBRATION and r["kind"] in J.KIND_DECISION]
    detections = [(r["kind"], r["detected"]) for r in plants if r["status"] == JUDGED]
    positives = [(r["decision"], r["detected"]) for r in recs
                 if r["step"] == CALIBRATION and r["kind"] == J.POSITIVE and r["status"] == JUDGED]
    matched = [r for r in recs if r["step"] == CALIBRATION and r["kind"] == MATCHER and r["status"] == JUDGED]
    pos_paired = [p[2] for r in matched for p in r["positives"]]
    neg_paired = [p[2] for r in matched for p in r["negatives"]]
    m_sens, m_spec = R.matcher_rates(pos_paired, neg_paired)
    stop = next((r["error"] for r in recs if r["step"] == BUDGET_STOP), None)
    return {
        "stopped": result.stopped, "budget_stop": stop, "error": error,
        "cost_by_role": by_role, "cost_by_step": by_step, "unattributed_cost": unattributed,
        "spent_this_invocation_usd": sum(L.row_cost(r) for r in new),
        "h_s2": h_s2, "judge_cost_note": JUDGE_COST_NOTE,
        "my_tasks_per_transcript": per_transcript, "icc": icc, "transcripts_needed": needed,
        "min_effective_tasks": cfg["eval"]["min_effective_tasks"],
        # Judge-step verdicts only: plants are built to be wrong, which would skew the rate.
        "escalation_rates": J.escalation_rates(_verdict(r) for r in judged),
        "calibration": {"by_decision": J.calibrate(detections, positives),
                        "sensitivity_by_kind": J.sensitivity(detections),
                        "judged": dict(Counter(k for k, _ in detections)),
                        "not_applicable": dict(Counter(r["kind"] for r in recs if r["step"] == CALIBRATION
                                                       and r["status"] == NOT_APPLICABLE))},
        "matcher": {"sensitivity": m_sens, "specificity": m_spec,
                    "positives": len(pos_paired), "negatives": len(neg_paired)},
        "echo_dropped": {r["unit"]: r["echo_dropped"] for r in recs if r["step"] == PREPARED},
        "skipped": [r["unit"] for r in recs if r["step"] == SKIPPED],
        "errors": len([r for r in recs if r["step"] == ERROR]),
    }


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def _tokens(c: dict) -> str:
    return " | ".join(str(c[t]) for t in TOKEN_FIELDS)


def _cost_table(title: str, costs: dict) -> list[str]:
    lines = [f"## {title}", "", "| | calls | cached | usd | input | output | cache read | cache write |",
             "|---|---|---|---|---|---|---|---|"]
    lines += [f"| {name} | {c['calls']} | {c.get('cached_calls', '')} | {c['usd']:.4f} | {_tokens(c)} |"
              for name, c in costs.items()]
    return lines + [""]


def report_markdown(rep: dict) -> str:
    """A short human-readable view of `report` (plus run_id and probe when present)."""
    lines = [f"# Pilot {rep.get('run_id', '')}".rstrip(), ""]
    if rep.get("error"):
        lines.append(f"RUN FAILED: {rep['error']} (report built from what was written before the failure)")
    elif rep["stopped"]:
        lines.append(f"Stopped on budget: {rep['budget_stop']}")
    else:
        lines.append("Completed every step.")
    lines += [f"Errors (unusable model answers): {rep['errors']}. Skipped stubs: {', '.join(rep['skipped']) or 'none'}.",
              f"Spent this invocation: ${rep['spent_this_invocation_usd']:.4f} (unattributed"
              f" ${rep['unattributed_cost']['usd']:.4f}). Step costs include earlier invocations'"
              " rows for requests this run served from cache.", ""]
    if rep.get("probe"):
        lines += ["## --setting-sources probe", ""]
        lines += [f"- {name}: " + ("ok" if p["ok"] else f"FAILED: {p['error']}") for name, p in rep["probe"].items()]
        lines.append("")
    lines += _cost_table("Cost by step", rep["cost_by_step"]) + _cost_table("Cost by role", rep["cost_by_role"])
    lines += ["## H-S2 (exploratory)", "",
              "| variant | my-task recall (n) | step | calls | usd | input | output | cache read | cache write |",
              "|---|---|---|---|---|---|---|---|---|"]
    for v, h in rep["h_s2"].items():
        r = h["my_task_recall"]
        for step, c in (("extract", h["extract_cost"]), ("judge", h["judge_cost"])):
            lines.append(f"| {v} | {_fmt(r['estimate'])} ({r['n']}) | {step} | {c['calls']} | {c['usd']:.4f} | "
                         f"{_tokens(c)} |")
    lines += ["", f"Judge cost per variant is {rep['judge_cost_note']}.",
              "", "## Sizing (B.3)", "",
              f"My tasks per transcript: {_fmt(rep['my_tasks_per_transcript'])}; "
              f"target effective my tasks: {rep['min_effective_tasks']}."]
    lines += [f"- {v}: ICC {_fmt(rep['icc'][v])}, transcripts needed {_fmt(rep['transcripts_needed'][v])}"
              for v in rep["icc"]]
    lines += ["", "## Calibration", "", "| decision | sensitivity | specificity |", "|---|---|---|"]
    lines += [f"| {d} | {_fmt(c['sensitivity'])} | {_fmt(c['specificity'])} |"
              for d, c in rep["calibration"]["by_decision"].items()]
    m = rep["matcher"]
    lines += ["", f"Matcher: sensitivity {_fmt(m['sensitivity'])} ({m['positives']} pairs), "
              f"specificity {_fmt(m['specificity'])} ({m['negatives']} pairs).",
              "Escalation rates: " + (", ".join(f"{d} {r:.2f}" for d, r in rep["escalation_rates"].items()) or "none"),
              "Echo lines dropped: " + (", ".join(f"{u} {n}" for u, n in rep["echo_dropped"].items()) or "none"), ""]
    return "\n".join(lines)


# --- --setting-sources probe --------------------------------------------------------

def probe_variants(cfg: dict) -> list[tuple[str, dict]]:
    """The probe's configs, deep copies of cfg without cli.isolation_args (which would put
    the flag in both): cli.base_args alone ("default") and with `--setting-sources ""`
    appended ("no_settings"); cfg is never changed."""
    default = copy.deepcopy(cfg)
    default["cli"]["isolation_args"] = []
    no_settings = copy.deepcopy(default)
    no_settings["cli"]["base_args"] = [*cfg["cli"]["base_args"], *NO_SETTINGS_ARGS]
    return [("default", default), ("no_settings", no_settings)]


def probe(cfg: dict, make_ask_fn: Callable[[dict], Ask]) -> dict:
    """One tiny extractor-role call per `probe_variants` config. A failing variant is
    reported as data, never raised; a budget stop is recorded too (`budget_stop: True`)
    for the caller to act on. Pass an uncached ask: the probe tests whether the CLI
    accepts the arguments now, and a cached answer from an earlier run (or an earlier
    CLI version) would be no evidence of that."""
    out = {}
    for name, variant in probe_variants(cfg):
        entry = {"base_args": list(variant["cli"]["base_args"])}
        try:
            entry.update(ok=True, output=make_ask_fn(variant)(stages.EXTRACTOR, PROBE_PROMPT, PROBE_SCHEMA))
        except L.BudgetStop as exc:
            entry.update(ok=False, budget_stop=True, error=f"BudgetStop: {exc}")
        except (ModelCallError, calls.OutputError) as exc:
            entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        out[name] = entry
    return out


# --- the scored run -----------------------------------------------------------------

def execute(cfg: dict, eval_run: L.Run, run_dir: Path, jobs: list[stages.Job], cache: Path, cap: float) -> None:
    """Every pilot step, then the --setting-sources probe, then the report, for
    `python -m eval run --stage pilot` inside `eval_run`. The steps' calls are cached
    and capped at `cap` each; the probe runs last, uncached (see `probe`), at
    eval.stages.pilot.probe_max_budget_usd per call, so it can neither spend the steps'
    budget nor block the report.

    The report is written whatever happens: after an exception it is built from the
    records and call log on disk and says so, and the exception still propagates, so
    the run is marked failed. A budget stop, in the steps or the probe, raises after
    the report so the run needs resolving: the harness stops and asks (B.9)."""
    check, ledger, before = L.guard(cfg, eval_run.stage), L.ledger_path(cfg), len(L.rows(cfg))
    out = run_dir / RECORDS_FILE
    units = list({j.unit_id: j.prepared for j in jobs}.items())  # plan's preparation, one per unit
    result, probed, error = None, None, None

    def ask_for(c: dict, cache_dir: Optional[Path], max_budget_usd: float) -> Ask:
        return make_ask(c, ledger=ledger, cache_dir=cache_dir, before_call=check, max_budget_usd=max_budget_usd)
    try:
        pilot = _Pilot(cfg, ask_for(cfg, cache, cap), out)
        result = pilot.run(units)
        probe_cap = cfg["eval"]["stages"]["pilot"]["probe_max_budget_usd"]
        probed = pilot.probe(lambda c: ask_for(c, None, probe_cap))
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            rows = L.rows(cfg)
            rep = {"run_id": eval_run.run_id, "probe": probed,
                   **report(result or load(out), rows, cfg, new_rows=rows[before:], error=error)}
            atomic_write_text(run_dir / REPORT_JSON, json.dumps(rep, ensure_ascii=False, indent=1))
            atomic_write_text(run_dir / REPORT_MD, report_markdown(rep))
        except Exception as exc:
            print(f"WARNING: could not write the pilot report: {type(exc).__name__}: {exc}")
            if error is None:
                raise
    stops = ([rep["budget_stop"]] if result.stopped else []) + [p["error"] for p in probed.values() if p.get("budget_stop")]
    if stops:
        print(f"BUDGET STOP: {'; '.join(stops)}; report in {run_dir}")
        raise L.BudgetStop("; ".join(stops))
    print(f"completed {eval_run.run_id}; report {run_dir / REPORT_MD}; spend now ${L.spent(cfg):.2f}")
