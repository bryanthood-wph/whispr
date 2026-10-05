"""CONTRACT for eval/pilot.py and the pilot additions to eval/scoring.py (issue #13,
docs/plan/README.md §7 Phase 1, B.3, B.9). Written by the manager before the build and
checksummed: the builder must not edit this file.

The pilot runs 3 transcripts through every step within eval.stages.pilot.cap_usd. Units
run one at a time, smallest transcript first, each through every step in this order:

    extract (both system variants, H-S2) -> reference -> judge (default variant)
    -> judge (minimal variant) -> calibration (plants, positives, matcher)

Unit-major, so a budget stop still leaves a measured cost for every step from at least
the smallest unit (amended after the build: step-major order let the reference step's
per-call reservations exhaust the cap before any judge call).

A budget stop (eval.ledger.BudgetStop) is an expected pilot outcome: it is recorded as a
step and the run returns normally with `stopped=True`. Every model call goes through the
injected `ask`; the pilot wraps it to record (step, role, request key) per call, so the
report can join the ledger and give measured cost per step and role.

Record shapes the contract fixes (other fields are free): every record has "step" and,
except budget_stop, "unit"; extract and judge records carry "variant"; judge records
carry "decision" and "verdict" (a judge.Verdict as a dict); calibration plant records
carry "kind" and "status" ("judged" | "not_applicable"); budget_stop carries "error".
The pilot calls judge.plant through the module (J.plant), so tests can patch it.
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval import judge as J
from eval import ledger as L
from eval import pilot as P
from eval import scoring as S
from eval import stages
from pipeline import calls, prepare
from pipeline.config import load_config
from pipeline.models import ModelCallError
from pipeline_helpers import EXTRACT_SAMPLE, overlay, transcript

LINES = [
    ("Others", "Okay so the decision is we ship the Oracle forms deck on Friday."),
    ("Me", "I will send the revised deck to Jamie by Thursday afternoon."),
    ("Others", "Revenue for the quarter came in at 4.2 million which is above plan."),
    ("Others", "The main risk is that the data migration slips into next month."),
    ("Me", "I can also book the review room for the cutover meeting next week."),
    ("Others", "Jamie Doe will own the cutover checklist and circulate it Monday."),
]


def turns(n: int) -> list[tuple[str, str, str]]:
    """n turns cycling LINES, 20 s apart (no echo: Me and Others never share words in a window)."""
    return [(f"00:{(20 * i) // 60:02d}:{(20 * i) % 60:02d}", *LINES[i % len(LINES)]) for i in range(n)]

REF_ITEMS = [
    {"type": "task", "text": "Send revised deck to Jamie", "owner": "Me", "owner_basis": "volunteered",
     "mine": True, "due": "Thursday afternoon", "quote": "I will send the revised deck to Jamie", "importance": 3},
    {"type": "task", "text": "Book the review room", "owner": "Me", "owner_basis": "volunteered",
     "mine": True, "due": "next week", "quote": "I can also book the review room", "importance": 2},
    {"type": "decision", "text": "Ship the deck Friday", "owner": None, "owner_basis": "unclear",
     "mine": False, "due": None, "quote": "the decision is we ship the Oracle forms deck", "importance": 3},
    {"type": "number", "text": "Quarter revenue 4.2M", "owner": None, "owner_basis": "unclear",
     "mine": False, "due": None, "quote": "Revenue for the quarter came in at 4.2 million", "importance": 2},
    {"type": "risk", "text": "Migration may slip", "owner": None, "owner_basis": "unclear",
     "mine": False, "due": None, "quote": "the data migration slips into next month", "importance": 2},
]

# The extract the fake extractor returns: the recording owner appears as a named person,
# so a plant that forgot to exclude them could hand them a task or a quote.
DOC = json.loads(json.dumps(EXTRACT_SAMPLE))
DOC["entities"].append({"name": "Pat Example", "type": "person", "aliases": ["Pat"]})
DOC["facts"].append({"type": "number", "text": "Revenue was 4.2 million for the quarter", "subject": None,
                     "quote": "Revenue for the quarter came in at 4.2 million", "start": "00:00:41"})
DOC["other_tasks"].append({"action": "Circulate the cutover checklist", "owner": "Jamie Doe",
                           "due": {"text": "Monday", "basis": "stated"}, "context": "",
                           "quote": "Jamie Doe will own the cutover checklist", "start": "00:01:45"})
DOC["sections"][0]["points"] += ["The client said the migration is on track.",
                                 "Quarter revenue came in at 4.2 million, above plan."]
DOC["my_actions"].append({"action": "Book the review room", "owner_basis": "volunteered",
                          "due": {"text": "next week", "basis": "stated"}, "context": "",
                          "quote": "I can also book the review room", "start": "00:01:20"})


def fake_ask(cfg, log, *, fail_after=None):
    """Answers by schema shape; logs (role, request key) per call. Raises BudgetStop
    after `fail_after` calls when given."""
    def ask(role, prompt, schema, *, system_prompt=None, replicate=0):
        if fail_after is not None and len(log) >= fail_after:
            raise L.BudgetStop("stage 'pilot' cap reached (test)")
        log.append((role, calls.request_key(cfg, role, prompt, schema, system_prompt, replicate)))
        props = schema.get("properties", {})
        if "items" in props:
            return {"items": REF_ITEMS}
        if "pairs" in props:
            return {"pairs": []}
        if "verdict" in props:
            return {"verdict": "present"}
        if "answer" in props:
            answer = "supported" if "supported | unsupported | contradicted" in prompt else "yes"
            return {"answer": answer, "confidence": 0.9}
        return json.loads(json.dumps(DOC))       # the extractor
    return ask


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = load_config(overlay=overlay(self.root))
        long = self.root / "transcripts" / "2026-09-01-0900-long.md"
        long.write_text(transcript("2026-09-01T09:00:00-04:00", turns(18)), encoding="utf-8")
        short = self.root / "transcripts" / "2026-09-02-0900-short.md"
        short.write_text(transcript("2026-09-02T09:00:00-04:00", turns(12)), encoding="utf-8")
        stub = self.root / "transcripts" / "2026-09-03-0900-stub.md"
        stub.write_text(transcript("2026-09-03T09:00:00-04:00", [("00:00:01", "Others", "Hello?")]),
                        encoding="utf-8")
        self.units = [(p.stem, prepare.prepare([prepare.parse(p)], self.cfg)) for p in (long, short, stub)]
        self.out = self.root / "pilot.jsonl"
        self.log = []

    def tearDown(self):
        self._tmp.cleanup()


class TestConfig(_Base):
    def test_pilot_knobs_are_config(self):
        pc = self.cfg["eval"]["stages"]["pilot"]
        for key in ("judge_subjects", "plants_per_kind", "positives"):
            self.assertIsInstance(pc[key], int)
            self.assertGreater(pc[key], 0)

    def test_owner_names_to_exclude_from_plants(self):
        self.assertEqual(set(P.owner_names(self.cfg)), {"Pat Example", "Pat"})


class TestScoringAdditions(unittest.TestCase):
    """One-way ANOVA ICC (unequal cluster sizes via n0), truncated at 0, and the
    transcripts needed so the effective number of my tasks reaches the target (B.3)."""

    def test_icc(self):
        self.assertAlmostEqual(S.icc([[1, 1, 1], [0, 0, 0], [1, 1, 1]]), 1.0)
        self.assertEqual(S.icc([[1, 0], [1, 0], [1, 0]]), 0.0)          # negative estimate truncated
        groups = [[1, 1, 0], [0, 0, 1, 0], [1, 1]]
        k, n = len(groups), sum(map(len, groups))
        grand = sum(map(sum, groups)) / n
        ssb = sum(len(g) * (sum(g) / len(g) - grand) ** 2 for g in groups)
        ssw = sum((x - sum(g) / len(g)) ** 2 for g in groups for x in g)
        msb, msw = ssb / (k - 1), ssw / (n - k)
        n0 = (n - sum(len(g) ** 2 for g in groups) / n) / (k - 1)
        self.assertAlmostEqual(S.icc(groups), max(0.0, (msb - msw) / (msb + (n0 - 1) * msw)))

    def test_icc_undefined(self):
        self.assertIsNone(S.icc([[1, 0, 1]]))                       # one cluster
        self.assertIsNone(S.icc([[1], [0]]))                        # no within-cluster df
        self.assertIsNone(S.icc([[1, 1], [1, 1]]))                  # no variance at all

    def test_transcripts_needed(self):
        # effective tasks per transcript = m / (1 + (m - 1) rho)
        self.assertEqual(S.transcripts_needed(rho=0.0, tasks_per_transcript=3.0, min_effective=140), 47)
        per = 3.0 / (1 + 2 * 0.2)
        self.assertEqual(S.transcripts_needed(rho=0.2, tasks_per_transcript=3.0, min_effective=140),
                         math.ceil(140 / per))
        self.assertIsNone(S.transcripts_needed(rho=0.2, tasks_per_transcript=0.0, min_effective=140))


class TestSubjects(_Base):
    def test_capped_deterministic_and_every_my_task_judged_for_presence(self):
        from eval import reference as R
        k = self.cfg["eval"]["stages"]["pilot"]["judge_subjects"]
        items = [R.RefItem(f"a.0.{n}", "a", **i) for n, i in enumerate(REF_ITEMS)]
        mine = [i for i in items if i.mine]
        for decision in J.DECISIONS:
            first = P.subjects(decision, DOC, items, self.cfg, key="u1")
            self.assertEqual(first, P.subjects(decision, DOC, items, self.cfg, key="u1"), decision)
            self.assertTrue(first, decision)
            limit = k + len(mine) if decision == "present" else k
            self.assertLessEqual(len(first), limit, decision)
        present = P.subjects("present", DOC, items, self.cfg, key="u1")
        for item in mine:
            self.assertTrue(any(item.text in s for s in present), item.text)
        self.assertEqual(len(P.subjects("my_actions", DOC, items, self.cfg, key="u1")), 1)
        for claim in P.subjects("supported", DOC, items, self.cfg, key="u1"):
            self.assertIn(claim, J.summary_claims(DOC))

    def test_detection_semantics(self):
        self.assertTrue(P.detected("present", "no"))
        self.assertTrue(P.detected("present", "partial"))
        self.assertFalse(P.detected("present", "yes"))
        self.assertTrue(P.detected("supported", "contradicted"))
        self.assertFalse(P.detected("supported", "supported"))
        self.assertTrue(P.detected("attribution", "yes"))          # a violation was found
        self.assertFalse(P.detected("attribution", "no"))
        for decision in ("task_owner", "task_due"):
            self.assertTrue(P.detected(decision, "no"))
            self.assertFalse(P.detected(decision, "yes"))


class TestRun(_Base):
    def records(self):
        return [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines()]

    def test_every_step_unit_by_unit_smallest_first(self):
        with mock.patch.object(J, "plant", wraps=J.plant) as plant:
            result = P.run(self.units, self.cfg, fake_ask(self.cfg, self.log), self.out)
        self.assertFalse(result.stopped)
        self.assertEqual(result.records, self.records())
        order = ["extract", "reference", "judge", "calibration"]
        work = [r for r in result.records if r["step"] in order]
        # Smallest non-stub transcript first, and all of its work before the next unit's.
        units = [r["unit"] for r in work]
        self.assertEqual(units[0], "2026-09-02-0900-short")
        self.assertEqual(units, sorted(units, key=lambda u: u != "2026-09-02-0900-short"))
        # Within a unit, every step's records precede the next step's; default judged first.
        for unit in set(units):
            ranks = [order.index(r["step"]) for r in work if r["unit"] == unit]
            self.assertEqual(ranks, sorted(ranks), unit)
            self.assertEqual(set(ranks), set(range(len(order))), unit)
            judged = [r["variant"] for r in work if r["unit"] == unit and r["step"] == "judge"]
            self.assertEqual(judged, sorted(judged, key=lambda v: v != stages.DEFAULT_SYSTEM), unit)
        # Both system variants are extracted and judged for each non-stub unit.
        for unit in ("2026-09-01-0900-long", "2026-09-02-0900-short"):
            variants = {r["variant"] for r in result.records if r["step"] == "extract" and r["unit"] == unit}
            self.assertEqual(variants, {stages.DEFAULT_SYSTEM, stages.MINIMAL_SYSTEM})
            judged = {r["variant"] for r in result.records if r["step"] == "judge" and r["unit"] == unit}
            self.assertEqual(judged, {stages.DEFAULT_SYSTEM, stages.MINIMAL_SYSTEM})
        # The stub makes no call and says why.
        stub = [r for r in result.records if r["unit"] == "2026-09-03-0900-stub"]
        self.assertEqual([r["step"] for r in stub], ["skipped"])
        # Every plant excludes the recording owner (#16 review: plant() cannot read cfg).
        self.assertTrue(plant.called)
        for call in plant.call_args_list:
            self.assertTrue({"Pat Example", "Pat"} <= set(call.kwargs.get("exclude", ())))

    def test_not_applicable_plant_is_recorded_not_raised(self):
        real = J.plant

        def plant(kind, doc, seed, *, exclude=()):
            if kind == "changed_number":
                raise J.NotApplicable("no number in the summary text (test)")
            return real(kind, doc, seed, exclude=exclude)
        with mock.patch.object(J, "plant", side_effect=plant):
            result = P.run(self.units, self.cfg, fake_ask(self.cfg, self.log), self.out)
        self.assertFalse(result.stopped)
        rows = [r for r in result.records if r["step"] == "calibration" and r.get("kind") == "changed_number"]
        self.assertTrue(rows)
        self.assertTrue(all(r["status"] == "not_applicable" for r in rows))
        others = [r for r in result.records if r["step"] == "calibration" and r.get("kind") == "owner_swap"]
        self.assertTrue(others and all(r["status"] == "judged" for r in others))

    def test_judge_records_and_call_accounting(self):
        result = P.run(self.units, self.cfg, fake_ask(self.cfg, self.log), self.out)
        k = self.cfg["eval"]["stages"]["pilot"]["judge_subjects"]
        per_cell = {}
        for r in result.records:
            if r["step"] == "judge":
                self.assertIn(r["decision"], J.DECISIONS)
                self.assertIn(r["verdict"]["value"], J.DECISIONS[r["decision"]])
                per_cell.setdefault((r["unit"], r["variant"], r["decision"]), []).append(r)
        for (unit, variant, decision), rows in per_cell.items():
            if decision not in ("present", "my_actions"):
                self.assertLessEqual(len(rows), k)
        # The wrapper's call log accounts for every model call, in order, keyed exactly
        # as the ledger keys it (pipeline.calls.request_key), so the report can join them.
        self.assertEqual([(c["role"], c["key"]) for c in result.calls], self.log)
        self.assertTrue(all(c["step"] for c in result.calls))
        self.assertEqual(result.calls[0]["role"], "extractor")

    def test_budget_stop_is_recorded_and_partial_results_kept(self):
        result = P.run(self.units, self.cfg, fake_ask(self.cfg, self.log, fail_after=5), self.out)
        self.assertTrue(result.stopped)
        self.assertEqual(result.records[-1]["step"], "budget_stop")
        self.assertIn("cap", result.records[-1]["error"])
        self.assertEqual(result.records, self.records())
        self.assertEqual(len(self.log), 5)
        self.assertTrue(any(r["step"] == "extract" for r in result.records))


class TestReport(_Base):
    def test_cost_by_step_role_and_h_s2(self):
        result = P.run(self.units, self.cfg, fake_ask(self.cfg, self.log), self.out)
        rows = [{"role": c["role"], "request_key": c["key"], "cost_usd": 0.01, "input_tokens": 100,
                 "output_tokens": 10, "cache_read_tokens": 50, "cache_creation_tokens": 0, "is_error": False}
                for c in result.calls]
        rows.append({"event": L.RESERVE, "request_key": "x", "stage": "pilot", "reserve_usd": 1.5})
        rep = P.report(result, rows, self.cfg)
        self.assertAlmostEqual(sum(v["usd"] for v in rep["cost_by_role"].values()), 0.01 * len(result.calls))
        self.assertAlmostEqual(sum(v["usd"] for v in rep["cost_by_step"].values()), 0.01 * len(result.calls))
        self.assertEqual(sum(v["calls"] for v in rep["cost_by_role"].values()), len(result.calls))
        self.assertEqual(set(rep["h_s2"]), {stages.DEFAULT_SYSTEM, stages.MINIMAL_SYSTEM})
        for variant in rep["h_s2"].values():
            self.assertEqual(variant["extract_calls"], 2)
            self.assertIn("judge_values", variant)
        self.assertFalse(rep["stopped"])
        self.assertIn("icc", rep)
        self.assertIn("transcripts_needed", rep)
        self.assertEqual(set(rep["echo_dropped"]), {"2026-09-01-0900-long", "2026-09-02-0900-short"})
        self.assertIn("calibration", rep)
        self.assertIn("escalation_rates", rep)


class TestProbe(_Base):
    """--setting-sources probe (deferred from #8): the same tiny call with the configured
    base args and with `--setting-sources ""` appended; a failing variant is reported,
    never raised."""

    def test_probe_both_variants_and_failure_is_data(self):
        seen = []

        def make(cfg):
            args = cfg["cli"]["base_args"]
            seen.append(list(args))

            def ask(role, prompt, schema, *, system_prompt=None, replicate=0):
                if "--setting-sources" in args:
                    raise ModelCallError("unknown option (test)")
                return {"ok": True}
            return ask
        out = P.probe(self.cfg, make)
        self.assertEqual(seen[0], self.cfg["cli"]["base_args"])
        self.assertEqual(seen[1], self.cfg["cli"]["base_args"] + ["--setting-sources", ""])
        self.assertEqual(self.cfg["cli"]["base_args"], seen[0])          # cfg not mutated
        self.assertTrue(out["default"]["ok"])
        self.assertFalse(out["no_settings"]["ok"])
        self.assertIn("unknown option", out["no_settings"]["error"])


if __name__ == "__main__":
    unittest.main()
