"""CONTRACT for eval/judge.py and pipeline/render.py (issue #16, docs/plan/B-eval.md
B.5). Written by the manager before the build and checksummed: the builder must not
edit this file.

Seven typed decisions (B.5's owner/due row is two, as B.6 scores them apart), each sampled on Haiku (eval.judge.samples, distinct replicates so
each sample is a real call); a split goes to Sonnet, a Sonnet answer below
eval.judge.escalate_below goes to Opus. Calibration plants six error kinds in code
into copies of extract-shaped summaries, which pipeline/render.py turns into the text
a judge reads. All model access is through an injected `ask` (eval/ask.py).
Model-facing schemas carry no numeric constraints (config/schema/extract.json), so
range checks such as confidence in [0, 1] live in code.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from eval import judge as J
from pipeline import prepare, render
from pipeline.config import config_file, load_config
from pipeline.jsonschema_lite import validate
from pipeline_helpers import EXTRACT_SAMPLE, overlay, transcript

# An extract-shaped summary every planting kind applies to.
DOC = copy.deepcopy(EXTRACT_SAMPLE)
DOC["other_tasks"][0].update({"owner": "Jamie Doe", "due": {"text": "Monday", "basis": "stated"}})
DOC["facts"].append({"type": "number", "text": "Revenue was 4.2 million for the quarter", "subject": None,
                     "quote": "revenue came in at 4.2 million", "start": "00:03:00"})
DOC["sections"][0]["points"].append("The client said the migration is on track.")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(overlay=overlay(Path(self._tmp.name)))
        self.asked = []

    def tearDown(self):
        self._tmp.cleanup()

    def ask_answers(self, by_role):
        """by_role: role -> list of {"answer", "confidence"} consumed in order."""
        queues = {r: list(v) for r, v in by_role.items()}

        def ask(role, prompt, schema, *, system_prompt=None, replicate=0):
            self.asked.append((role, replicate))
            return queues[role].pop(0)
        return ask

    @property
    def n(self):
        return self.cfg["eval"]["judge"]["samples"]

    def split(self, first, other):
        return [a(first)] * (self.n - 1) + [a(other)]


def a(answer, confidence=0.9, **extra):
    return {"answer": answer, "confidence": confidence, **extra}


class TestDecisions(_Base):
    def test_decision_vocabulary_and_schema(self):
        self.assertEqual(J.DECISIONS, {
            "present": ("yes", "partial", "no"),
            "supported": ("supported", "unsupported", "contradicted"),
            "attribution": ("yes", "no"),
            "task_owner": ("yes", "no"),
            "task_due": ("yes", "no"),
            "actionable": ("yes", "no"),
            "my_actions": ("yes", "no"),
        })
        self.assertIn("judge", self.cfg["prompts"])
        schema = json.loads(config_file(self.cfg["schemas"]["judge"]).read_text(encoding="utf-8"))
        self.assertEqual(validate(a("yes", 0.8), schema), [])
        self.assertEqual(validate(a("no", 0.8, missing=["t1"]), schema), [])

    def test_unanimous_haiku_answer_is_accepted_with_distinct_replicates(self):
        v = J.decide("attribution", "PROMPT", self.cfg, self.ask_answers({"judge": [a("no")] * self.n}))
        self.assertEqual((v.decision, v.value, v.tier), ("attribution", "no", "judge"))
        self.assertEqual(self.asked, [("judge", r) for r in range(self.n)])

    def test_split_goes_to_sonnet_and_its_threshold_is_inclusive(self):
        at_bar = self.cfg["eval"]["judge"]["escalate_below"]
        ask = self.ask_answers({"judge": self.split("yes", "no"), "judge_escalate_1": [a("no", at_bar)]})
        v = J.decide("attribution", "PROMPT", self.cfg, ask)
        self.assertEqual((v.value, v.tier), ("no", "judge_escalate_1"))
        self.assertEqual([r for r, _ in self.asked], ["judge"] * self.n + ["judge_escalate_1"])

    def test_low_confidence_sonnet_goes_to_opus(self):
        low = self.cfg["eval"]["judge"]["escalate_below"] - 0.1
        ask = self.ask_answers({"judge": self.split("supported", "unsupported"),
                                "judge_escalate_1": [a("unsupported", low)],
                                "judge_escalate_2": [a("contradicted", 0.6)]})
        v = J.decide("supported", "PROMPT", self.cfg, ask)
        self.assertEqual((v.value, v.tier), ("contradicted", "judge_escalate_2"))

    def test_answer_outside_the_vocabulary_or_range_fails(self):
        with self.assertRaises(J.JudgeError):
            J.decide("actionable", "PROMPT", self.cfg, self.ask_answers({"judge": [a("partial")] * self.n}))
        with self.assertRaises(J.JudgeError):
            J.decide("actionable", "PROMPT", self.cfg, self.ask_answers({"judge": [a("yes", 1.5)] * self.n}))

    def test_my_actions_carries_missing_ids(self):
        v = J.decide("my_actions", "PROMPT", self.cfg,
                     self.ask_answers({"judge": [a("no", missing=["t2", "t5"])] * self.n}))
        self.assertEqual((v.value, v.missing), ("no", ("t2", "t5")))

    def test_escalation_rate_per_decision(self):
        verdicts = [J.Verdict("present", "yes", "judge", ("yes",) * 3),
                    J.Verdict("present", "no", "judge_escalate_1", ("yes", "no", "no")),
                    J.Verdict("supported", "supported", "judge", ("supported",) * 3)]
        self.assertEqual(J.escalation_rates(verdicts), {"present": 0.5, "supported": 0.0})


class TestClaims(unittest.TestCase):
    def test_split_one_claim_per_sentence_or_bullet(self):
        text = ("## Deck\n"
                "The team agreed to ship Friday. Revenue grew 3.5 percent!\n"
                "- Review moved to Thursday\n"
                "* Jamie owns the deck.\n"
                "\n"
                "Is the room booked?\n")
        self.assertEqual(J.split_claims(text), [
            "The team agreed to ship Friday.", "Revenue grew 3.5 percent!",
            "Review moved to Thursday", "Jamie owns the deck.", "Is the room booked?"])
        self.assertEqual(J.split_claims(text), J.split_claims(text))


class TestRender(unittest.TestCase):
    def test_render_is_deterministic_and_shows_what_planting_changes(self):
        text = render.render(DOC)
        self.assertEqual(text, render.render(copy.deepcopy(DOC)))
        for shown in ("My Actions", DOC["headline"], DOC["my_actions"][0]["action"], "Jamie Doe", "Monday",
                      DOC["facts"][1]["text"], DOC["sections"][0]["points"][1]):
            self.assertIn(shown, text)

    def test_empty_my_actions_says_none(self):
        bare = dict(copy.deepcopy(DOC), my_actions=[])
        text = render.render(bare)
        self.assertIn("My Actions", text)
        self.assertIn("None", text)


class TestPlanting(unittest.TestCase):
    def test_every_kind_mutates_a_copy_deterministically(self):
        self.assertEqual(J.PLANT_KINDS, ("owner_swap", "deleted_my_task", "changed_number",
                                         "invented_claim", "reattribution", "dropped_due"))
        original = copy.deepcopy(DOC)
        for kind in J.PLANT_KINDS:
            p1, p2 = J.plant(kind, DOC, seed=7), J.plant(kind, DOC, seed=7)
            self.assertEqual(DOC, original, f"{kind} mutated its input")
            self.assertNotEqual(p1.doc, DOC, kind)
            self.assertEqual((p1.doc, p1.target), (p2.doc, p2.target), kind)
            self.assertEqual(p1.kind, kind)
            self.assertTrue(p1.target)
            self.assertNotEqual(render.render(p1.doc), render.render(DOC), kind)

    def test_kind_that_cannot_apply_says_so(self):
        bare = copy.deepcopy(DOC)
        bare["my_actions"] = []
        with self.assertRaises(J.NotApplicable):
            J.plant("deleted_my_task", bare, seed=7)


class TestPositives(unittest.TestCase):
    def test_positives_are_verbatim_transcript_claims(self):
        turns = [("00:00:05", "Others", "We agreed to ship the forms deck on Friday. The budget is fixed."),
                 ("00:00:30", "Me", "I will send Jamie the revised deck by Thursday afternoon at the latest."),
                 ("00:01:00", "Others", "Revenue for the quarter came in at 4.2 million, above plan.")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(overlay=overlay(root))
            path = root / "transcripts" / "2026-09-01-0900-t.md"
            path.write_text(transcript("2026-09-01T09:00:00-04:00", turns * 3), encoding="utf-8")
            prep = prepare.prepare([prepare.parse(path)], cfg)
            claims = J.plant_positives(prep, seed=3, n=2)
            self.assertEqual(claims, J.plant_positives(prep, seed=3, n=2))
            self.assertEqual(len(claims), 2)
            text = prep.render()
            for claim in claims:
                self.assertIn(claim, text)


class TestCalibration(unittest.TestCase):
    def test_every_kind_tests_one_decision(self):
        self.assertEqual(set(J.KIND_DECISION), set(J.PLANT_KINDS))
        self.assertEqual(J.KIND_DECISION, {
            "owner_swap": "task_owner", "deleted_my_task": "present", "changed_number": "supported",
            "invented_claim": "supported", "reattribution": "attribution", "dropped_due": "task_due"})

    def test_sensitivity_specificity_and_per_decision_calibration(self):
        detections = [("owner_swap", True), ("owner_swap", False), ("dropped_due", True),
                      ("changed_number", True), ("invented_claim", True), ("invented_claim", False),
                      ("invented_claim", True), ("invented_claim", True)]
        self.assertEqual(J.sensitivity(detections),
                         {"owner_swap": 0.5, "dropped_due": 1.0, "changed_number": 1.0, "invented_claim": 0.75})
        positives = [("supported", False)] * 3 + [("supported", True), ("task_owner", False)]
        self.assertEqual(J.specificity(positives), {"supported": 0.75, "task_owner": 1.0})
        cal = J.calibrate(detections, positives)
        # A decision's sensitivity is its weakest planted kind (B.5: any kind under
        # target makes the metric unreliable); nothing measured -> None.
        self.assertEqual(cal["supported"], {"sensitivity": 0.75, "specificity": 0.75})
        self.assertEqual(cal["task_owner"], {"sensitivity": 0.5, "specificity": 1.0})
        self.assertEqual(cal["task_due"], {"sensitivity": 1.0, "specificity": None})


if __name__ == "__main__":
    unittest.main()
