"""eval/judge.py beyond the contract (#16): the judge prompt's cache-friendly layout
and planting edge cases. No model calls."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval import judge as J
from pipeline import render
from pipeline.config import DEFAULTS_PATH, ConfigError, config_file, load_config
from pipeline_helpers import EXTRACT_SAMPLE, overlay, prepared


class TestBuildPrompt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(overlay=overlay(Path(self._tmp.name)))
        self.kw = {"transcript": "[00:00:01] Me: We ship Friday.", "summary": render.render(EXTRACT_SAMPLE)}

    def tearDown(self):
        self._tmp.cleanup()

    def test_shared_prefix_runs_through_the_summary_for_every_decision(self):
        built = [J.build_prompt(d, self.cfg, subject=f"item {d}", **self.kw) for d in J.DECISIONS]
        prefix = built[0][:built[0].index("\nQUESTION\n")]
        self.assertIn(self.kw["summary"].rstrip("\n"), prefix)
        for text in built:
            self.assertTrue(text.startswith(prefix))

    def test_filled_and_free_of_author_notes(self):
        text = J.build_prompt("my_actions", self.cfg, subject="t1: Send the deck", **self.kw)
        self.assertNotIn("{{", text)
        self.assertNotIn("<!--", text)
        self.assertIn(self.cfg["owner"]["name"], text)
        self.assertTrue(text.rstrip().endswith(" | ".join(J.DECISIONS["my_actions"])))

    def test_unknown_decision_fails(self):
        with self.assertRaises(J.JudgeError):
            J.build_prompt("vibes", self.cfg, subject="x", **self.kw)


class TestPlantingEdges(unittest.TestCase):
    def test_empty_document_has_nothing_to_plant(self):
        bare = dict(copy.deepcopy(EXTRACT_SAMPLE), sections=[], my_actions=[], other_tasks=[], facts=[],
                    entities=[], headline="Nothing happened.")
        for kind in J.PLANT_KINDS:
            with self.assertRaises(J.NotApplicable, msg=kind):
                J.plant(kind, bare, seed=1)

    def test_reattribution_without_an_unnamed_speaker_names_one(self):
        p = J.plant("reattribution", EXTRACT_SAMPLE, seed=1)
        self.assertEqual(p.target, "Review moved to Thursday, Jamie Doe said.")

    def test_dropped_due_leaves_no_due_text(self):
        p = J.plant("dropped_due", _doc(headline="The team met.", facts=[]), seed=1)
        self.assertEqual([t["due"]["text"] for t in p.doc["my_actions"]], [None])


SEEDS = range(40)


def _doc(**changes):
    doc = copy.deepcopy(EXTRACT_SAMPLE)
    doc.update(copy.deepcopy(changes))
    return doc


def _person(name, *aliases):
    return {"name": name, "type": "person", "aliases": list(aliases)}


def _task(action, owner=None, due=None, context=""):
    return {"action": action, "owner": owner, "due": {"text": due, "basis": "stated" if due else "not_stated"},
            "context": context, "quote": action.lower(), "start": None}


class _Asking(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(overlay=overlay(Path(self._tmp.name)))
        self.asked = []

    def tearDown(self):
        self._tmp.cleanup()

    def ask(self, by_role):
        queues = {r: list(v) for r, v in by_role.items()}

        def ask(role, prompt, schema, *, system_prompt=None, replicate=0):
            self.asked.append(role)
            return queues[role].pop(0)
        return ask


def _prep(turns):
    """A Prepared transcript of `turns`, each repeated so the stub gate never trips."""
    with tempfile.TemporaryDirectory() as tmp:
        return prepared(load_config(overlay=overlay(Path(tmp))), "2026-09-01T09:00:00-04:00", turns * 5)


def ans(answer, confidence=0.9, **extra):
    return {"answer": answer, "confidence": confidence, **extra}


class TestFix1Missing(_Asking):
    def test_same_answer_with_different_missing_ids_is_a_split(self):
        n = self.cfg["eval"]["judge"]["samples"]
        samples = [ans("no", missing=["t1"])] * (n - 1) + [ans("no", missing=["t2"])]
        v = J.decide("my_actions", "P", self.cfg,
                     self.ask({"judge": samples, "judge_escalate_1": [ans("no", missing=["t1", "t2"])]}))
        self.assertEqual((v.tier, v.missing), (J.ESCALATE_1, ("t1", "t2")))

    def test_missing_order_alone_is_not_a_split(self):
        n = self.cfg["eval"]["judge"]["samples"]
        samples = [ans("no", missing=["t1", "t2"])] * (n - 1) + [ans("no", missing=["t2", "t1"])]
        v = J.decide("my_actions", "P", self.cfg, self.ask({"judge": samples}))
        self.assertEqual(v.tier, J.FIRST)

    def test_yes_drops_missing_and_no_may_have_none(self):
        n = self.cfg["eval"]["judge"]["samples"]
        v = J.decide("my_actions", "P", self.cfg, self.ask({"judge": [ans("yes", missing=["t1"])] * n}))
        self.assertEqual((v.value, v.missing), ("yes", ()))
        v = J.decide("my_actions", "P", self.cfg, self.ask({"judge": [ans("no")] * n}))
        self.assertEqual((v.value, v.missing), ("no", ()))


class TestFix2OwnerAliases(unittest.TestCase):
    def test_owner_named_by_alias_is_never_swapped_to_itself(self):
        doc = _doc(my_actions=[], entities=[_person("Jamie Doe", "Jamie")],
                   other_tasks=[_task("Book the room", owner="Jamie")])
        with self.assertRaises(J.NotApplicable):
            J.plant("owner_swap", doc, seed=1)

    def test_swap_goes_to_a_different_person(self):
        doc = _doc(my_actions=[], entities=[_person("Jamie Doe", "Jamie"), _person("Sam Roe")],
                   other_tasks=[_task("Book the room", owner="Jamie")])
        for seed in SEEDS:
            self.assertEqual(J.plant("owner_swap", doc, seed=seed).doc["other_tasks"][0]["owner"], "Sam Roe")


class TestFix3Exclude(unittest.TestCase):
    DOC = _doc(entities=[_person("Jamie Doe", "Jamie"), _person("Pat Example", "Pat")],
               other_tasks=[_task("Book the room", owner="Jamie Doe")])

    def test_excluded_people_are_never_chosen(self):
        for exclude in (("Pat Example", "Pat"), ("Pat",)):
            for seed in SEEDS:
                for kind in ("owner_swap", "reattribution"):
                    p = J.plant(kind, self.DOC, seed=seed, exclude=exclude)
                    self.assertNotIn("Pat", render.render(p.doc), (kind, seed, exclude))

    def test_excluding_everyone_leaves_nothing(self):
        doc = _doc(my_actions=[], entities=[_person("Pat Example", "Pat")])
        for kind in ("owner_swap", "reattribution"):
            with self.assertRaises(J.NotApplicable, msg=kind):
                J.plant(kind, doc, seed=1, exclude=("Pat",))


class TestFix4DueStaysVisible(unittest.TestCase):
    def test_task_naming_its_due_date_is_skipped(self):
        doc = _doc(my_actions=[], other_tasks=[_task("Send the deck by Friday", due="Friday"),
                                               _task("Book a room", due="Monday", context="For the MONDAY review")])
        with self.assertRaises(J.NotApplicable):
            J.plant("dropped_due", doc, seed=1)
        doc["other_tasks"].append(_task("Order lunch", due="noon"))
        for seed in SEEDS:
            self.assertEqual(J.plant("dropped_due", doc, seed=seed).target, "Order lunch")


class TestFix5Identifiers(unittest.TestCase):
    def test_identifiers_are_not_numbers(self):
        doc = _doc(headline="Move the S3 bucket to the v2 layout.", sections=[{"heading": "Infra", "points": ["Use S3."]}],
                   facts=[])
        with self.assertRaises(J.NotApplicable):
            J.plant("changed_number", doc, seed=1)
        doc["facts"] = [{"type": "number", "text": "Costs rose 12 percent", "subject": None, "quote": "q", "start": None}]
        for seed in SEEDS:
            p = J.plant("changed_number", doc, seed=seed)
            self.assertEqual(p.doc["headline"], doc["headline"])
            self.assertRegex(p.target, r"^Number: Costs rose \d\d percent$")


class TestFix6SummaryClaims(unittest.TestCase):
    def test_claims_come_from_headline_points_and_facts_only(self):
        doc = _doc(my_actions=[])
        claims = J.summary_claims(doc)
        self.assertEqual(claims, ["The team agreed to ship the deck Friday.", "Review moved to Thursday.",
                                  "Decision: Ship Friday"])
        text = render.render(doc)
        for claim in claims:
            self.assertIn(claim, text)


class TestFix7Labels(_Asking):
    def test_prompt_labels_come_from_render(self):
        raw = (Path(__file__).resolve().parent.parent / "config" / "prompts" / "judge.md").read_text(encoding="utf-8")
        for label in (render.ME, render.NO_OWNER, render.NO_DUE, render.MY_ACTIONS, render.NONE):
            self.assertNotIn(f'"{label}"', raw, label)
        kw = {"transcript": "T", "summary": "S", "subject": "X"}
        with mock.patch.multiple(render, NO_OWNER="unowned", NO_DUE="undated", MY_ACTIONS="Mine", NONE="Nil", ME="self"):
            owner = J.build_prompt("task_owner", self.cfg, **kw)
            due = J.build_prompt("task_due", self.cfg, **kw)
            mine = J.build_prompt("my_actions", self.cfg, **kw)
        self.assertIn('"unowned"', owner)
        self.assertIn('"self"', owner)
        self.assertIn('"undated"', due)
        self.assertIn('"Mine"', mine)
        self.assertIn('"Nil"', mine)


class TestFix8PlantingData(unittest.TestCase):
    def test_material_comes_from_the_seed_data_file(self):
        material = J.planting()
        for key in ("invented_claims", "invented_number_range", "positive_min_words", "unnamed_speaker"):
            self.assertIn(key, material)
        for name in ("INVENTED_CLAIMS", "INVENTED_NUMBER_RANGE", "POSITIVE_MIN_WORDS"):
            self.assertFalse(hasattr(J, name), name)
        stems = [t.split("{{")[0] for t in material["invented_claims"]]
        for seed in SEEDS:
            target = J.plant("invented_claim", EXTRACT_SAMPLE, seed=seed).target
            self.assertTrue(any(target.startswith(s) for s in stems), target)


class TestFix10Positives(unittest.TestCase):
    def test_questions_are_not_positives(self):
        prep = _prep([("00:00:05", "Others", "Should the team move the forms deck review to Friday afternoon?"),
                      ("00:00:30", "Others", "The team agreed to ship the forms deck on Friday afternoon.")])
        self.assertEqual(J.plant_positives(prep, seed=1, n=1),
                         ["The team agreed to ship the forms deck on Friday afternoon."])
        with self.assertRaises(J.NotApplicable):
            J.plant_positives(prep, seed=1, n=2)

    def test_positive_is_inserted_like_an_invented_claim(self):
        doc = _doc(sections=[{"heading": "A", "points": ["a1.", "a2."]}, {"heading": "B", "points": ["b1."]}])
        claim = "We agreed to ship the forms deck on Friday."
        for seed in SEEDS:
            pos, inv = J.plant_positive(doc, claim, seed=seed), J.plant("invented_claim", doc, seed=seed)
            self.assertEqual((pos.kind, pos.target), (J.POSITIVE, claim))
            self.assertIn(claim, render.render(pos.doc))
            where = [(i, j) for i, s in enumerate(pos.doc["sections"]) for j, p in enumerate(s["points"]) if p == claim]
            where_inv = [(i, j) for i, s in enumerate(inv.doc["sections"])
                         for j, p in enumerate(s["points"]) if p == inv.target]
            self.assertEqual(where, where_inv, seed)
        self.assertEqual(doc, _doc(sections=[{"heading": "A", "points": ["a1.", "a2."]},
                                             {"heading": "B", "points": ["b1."]}]))


class TestFix11DeletedTaskDetectable(unittest.TestCase):
    def test_my_task_restated_elsewhere_is_skipped(self):
        doc = _doc(sections=[{"heading": "Deck", "points": ["Pat will send the deck to Jamie tonight."]}])
        with self.assertRaises(J.NotApplicable):
            J.plant("deleted_my_task", doc, seed=1)
        doc["my_actions"].append(dict(doc["my_actions"][0], action="Update the budget sheet"))
        for seed in SEEDS:
            self.assertEqual(J.plant("deleted_my_task", doc, seed=seed).target, "Update the budget sheet")


# --- review fixes (d406c7c) -------------------------------------------------------

def _fact(text, type_="number"):
    return {"type": type_, "text": text, "subject": None, "quote": "q", "start": None}


class TestReview2ClaimTargets(unittest.TestCase):
    DOC = _doc(sections=[{"heading": "Deck", "points": ["The client said the migration is on track. It ships in 3 weeks."]}],
               facts=[_fact("Revenue was 4.2 million for the quarter")])

    def test_claim_kinds_target_a_claim_the_supported_judge_sees(self):
        for seed in SEEDS:
            for kind in ("changed_number", "invented_claim", "reattribution"):
                p = J.plant(kind, self.DOC, seed=seed)
                self.assertIn(p.target, J.summary_claims(p.doc), (kind, seed))
            p = J.plant_positive(self.DOC, "Revenue for the quarter came in above plan.", seed=seed)
            self.assertIn(p.target, J.summary_claims(p.doc), seed)

    def test_multi_sentence_point_and_labeled_fact(self):
        p = J.plant("reattribution", self.DOC, seed=1)
        self.assertEqual(p.target, "Jamie Doe said the migration is on track.")
        doc = _doc(sections=[], headline="Quarter closed.", facts=[_fact("Revenue was 4.2 million")])
        self.assertRegex(J.plant("changed_number", doc, seed=1).target, r"^Number: Revenue was \d\.\d million$")


class TestReview3DueElsewhere(unittest.TestCase):
    def test_due_shown_elsewhere_is_skipped_on_word_boundaries(self):
        doc = _doc(headline="The deck is due Friday.", facts=[], my_actions=[],
                   other_tasks=[_task("Send the deck", due="Friday"), _task("Book a room", due="May"),
                                _task("Order lunch", context="Maybe for Friday's review")])
        for seed in SEEDS:
            self.assertEqual(J.plant("dropped_due", doc, seed=seed).target, "Book a room")
        doc["sections"][0]["points"].append("Rooms are scarce in May.")
        with self.assertRaises(J.NotApplicable):
            J.plant("dropped_due", doc, seed=1)


class TestReview4TaskElsewhere(unittest.TestCase):
    def test_my_task_restated_in_another_task_is_skipped(self):
        # "Send the deck" holds most of "Send the deck to finance" (and the reverse), and
        # the other task's context restates the budget sheet: only the room is deletable.
        mine = EXTRACT_SAMPLE["my_actions"][0]
        doc = _doc(my_actions=[dict(mine, action="Send the deck"), dict(mine, action="Send the deck to finance"),
                               dict(mine, action="Update the budget sheet")],
                   other_tasks=[_task("Chase the budget", context="After we update the budget sheet")])
        with self.assertRaises(J.NotApplicable):
            J.plant("deleted_my_task", doc, seed=1)
        doc["my_actions"].append(dict(mine, action="Book the review room"))
        for seed in SEEDS:
            self.assertEqual(J.plant("deleted_my_task", doc, seed=seed).target, "Book the review room")

    def test_an_action_of_short_words_needs_the_phrase_itself(self):
        # No word of 4+ characters: overlap proves nothing, so only the phrase restates it.
        mine = EXTRACT_SAMPLE["my_actions"][0]
        doc = _doc(my_actions=[dict(mine, action="Fix it")], other_tasks=[],
                   sections=[{"heading": "Next", "points": ["The team agreed to ship the deck Friday."]}])
        self.assertEqual(J.plant("deleted_my_task", doc, seed=1).target, "Fix it")
        doc["sections"][0]["points"].append("Pat said they would fix it today.")
        with self.assertRaises(J.NotApplicable):
            J.plant("deleted_my_task", doc, seed=1)

    def test_a_paraphrase_in_a_point_counts_as_restated(self):
        # Pilot 2026-10-05: the task deleted from My Actions was still a section point.
        mine = EXTRACT_SAMPLE["my_actions"][0]
        doc = _doc(my_actions=[dict(mine, action="Read materials tagged in the project and report assessment")],
                   other_tasks=[],
                   sections=[{"heading": "Next", "points": ["Pat will read all materials tagged to them tonight "
                                                            "and report an assessment."]}])
        with self.assertRaises(J.NotApplicable):
            J.plant("deleted_my_task", doc, seed=1)
        doc["sections"][0]["points"] = ["The team agreed to ship the deck Friday."]
        self.assertEqual(J.plant("deleted_my_task", doc, seed=1).target,
                         "Read materials tagged in the project and report assessment")


class TestReview5NoConfigKey(unittest.TestCase):
    def test_planting_is_not_a_config_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = overlay(Path(tmp))
            self.assertNotIn("planting", load_config(overlay=ov))
            with self.assertRaises(ConfigError):
                load_config(overlay=dict(ov, planting="mine.yaml"))
        self.assertEqual(J.PLANTING_PATH, DEFAULTS_PATH.with_name("planting.yaml"))
        self.assertTrue(J.PLANTING_PATH.exists())


class TestReview6WordBoundaries(unittest.TestCase):
    def test_a_name_inside_a_word_is_not_a_name(self):
        doc = _doc(entities=[_person("Ann Lee", "Ann")], other_tasks=[],
                   sections=[{"heading": "Plan", "points": ["The planning lead said the budget is fine."]}])
        for seed in SEEDS:
            self.assertEqual(J.plant("reattribution", doc, seed=seed).target, "Ann Lee said the budget is fine.")


class TestReview7Positives(unittest.TestCase):
    def test_pronouns_and_quoted_questions_are_not_positives(self):
        self.assertIn("pronouns", J.planting())
        prep = _prep([("00:00:05", "Others", "You should send the revised deck to finance today."),
                      ("00:00:10", "Others", "Our budget for the quarter is fixed now."),
                      ("00:00:15", "Others", 'She asked "is the forms deck ready for review, right?"'),
                      ("00:00:20", "Others", "The revised deck goes to finance on Friday."),
                      ("00:00:25", "Others", "Usually the finance team signs off within a week.")])
        self.assertEqual(sorted(J.plant_positives(prep, seed=1, n=2)),
                         ["The revised deck goes to finance on Friday.",
                          "Usually the finance team signs off within a week."])


class TestReview8TemplateCache(unittest.TestCase):
    def test_judge_md_is_parsed_once_per_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(overlay=overlay(Path(tmp)))
        J._parsed_template.cache_clear()
        with mock.patch.object(J.prompts, "load", wraps=J.prompts.load) as load:
            for d in J.DECISIONS:
                J.build_prompt(d, cfg, transcript="T", summary="S", subject="X")
        self.assertEqual(load.call_count, 1)


class TestReview9OneSlotDefinition(unittest.TestCase):
    def test_slots_and_claim_lines_share_one_definition(self):
        doc = _doc()
        parts = render.claim_parts(doc)
        self.assertEqual(J._text_slots(doc), [path for path, _ in parts])
        self.assertEqual(render.claim_lines(doc), [line for _, line in parts])

    def test_every_claim_line_is_rendered(self):
        doc = _doc(facts=[_fact("Revenue was 4.2 million"), dict(_fact("Ship Friday", "decision"), subject="Deck")])
        rendered = render.render(doc).splitlines()
        for line in render.claim_lines(doc):
            self.assertIn(line, rendered)


class TestReview10NotStated(unittest.TestCase):
    def test_not_stated_is_a_schema_due_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(overlay=overlay(Path(tmp)))
        schema = json.loads(config_file(cfg["schemas"]["extract"]).read_text(encoding="utf-8"))
        self.assertIn(J._NOT_STATED, schema["$defs"]["due"]["properties"]["basis"]["enum"])


if __name__ == "__main__":
    unittest.main()
