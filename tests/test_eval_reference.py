"""eval/reference.py beyond the contract (issue #15, B.4): artifact consistency, the
shared prompt prefix, the composed build, and calibration-pair edge cases. No model calls."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from eval import reference as R
from pipeline import prepare, prompts
from pipeline.config import config_file, load_config
from pipeline_helpers import overlay, prepared

TURNS = [
    ("00:00:05", "Others", "Okay so the decision is we ship the Oracle forms deck on Friday."),
    ("00:00:20", "Me", "I will send the revised deck to Jamie by Thursday afternoon."),
    ("00:00:41", "Others", "Revenue for the quarter came in at 4.2 million which is above plan."),
    ("00:01:02", "Others", "The main risk is that the data migration slips into next month."),
    ("00:01:30", "Me", "Who owns the cutover checklist, do we know yet?"),
    ("00:01:40", "Others", "Can you pull the vendor list together?"),
    ("00:01:45", "Me", "Sure, I will do that today."),
]
META_FIELDS = {"CALL_TITLE", "DATE", "CALL_TYPE", "ORGANIZER", "ATTENDEES", "OWNER_NAME", "TRANSCRIPT"}


def raw(text, quote, *, type="task", mine=False, owner=None):
    return {"type": type, "text": text, "owner": "Me" if mine else owner,
            "owner_basis": "volunteered" if mine else "others" if owner else "unclear", "mine": mine,
            "due": None, "quote": quote, "importance": 2}


def ref(id, family, text, quote, **kw):
    return R.RefItem(id, family, **raw(text, quote, **kw))


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(overlay=overlay(Path(self._tmp.name)))
        self.prep = prepared(self.cfg, "2026-09-01T09:00:00-04:00", TURNS)
        self.asked = []

    def tearDown(self):
        self._tmp.cleanup()

    def fake_ask(self, outputs):
        def ask(role, prompt, schema, *, system_prompt=None, replicate=0):
            self.asked.append((role, prompt))
            out = outputs[role]
            return out(prompt) if callable(out) else out
        return ask


class TestArtifacts(_Base):
    def schema(self, key):
        return json.loads(config_file(self.cfg["schemas"][key]).read_text(encoding="utf-8"))

    def test_item_vocabularies_match_the_ontology(self):
        with open(config_file(self.cfg["ontology"]), encoding="utf-8") as fh:
            onto = yaml.safe_load(fh)
        props = self.schema("reference")["properties"]["items"]["items"]["properties"]
        self.assertEqual(tuple(props["type"]["enum"]), R.ITEM_TYPES)
        self.assertEqual(set(R.ITEM_TYPES), set(onto["fact_types"]) | {R.TASK})
        self.assertEqual(set(props["owner_basis"]["enum"]), set(onto["owner_basis"]))
        self.assertEqual(tuple(props), R._MODEL_FIELDS)

    def test_mine_bases_are_schema_and_ontology_owner_bases(self):
        # No file marks which bases mean the owner's own task, so MINE_BASES stays a
        # literal; every value must still be a real owner_basis wherever one is defined.
        with open(config_file(self.cfg["ontology"]), encoding="utf-8") as fh:
            onto = yaml.safe_load(fh)
        ref_bases = self.schema("reference")["properties"]["items"]["items"]["properties"]["owner_basis"]["enum"]
        my_action = self.schema("extract")["properties"]["my_actions"]["items"]["properties"]["owner_basis"]["enum"]
        for bases in (ref_bases, my_action, list(onto["owner_basis"])):
            self.assertLessEqual(set(R.MINE_BASES), set(bases))
        self.assertEqual(len(set(R.MINE_BASES)), len(R.MINE_BASES))

    def test_schemas_use_only_structured_output_keywords(self):
        banned = {"minimum", "maximum", "minLength", "maxLength", "pattern", "format"}
        for key in ("reference", "matcher", "presence"):
            text = json.dumps(self.schema(key))
            self.assertFalse([k for k in banned if f'"{k}"' in text], key)
        self.assertEqual(self.schema("presence")["properties"]["verdict"]["enum"][0], R.PRESENT)

    def test_placeholders(self):
        extra = {"reference": set(), "matcher": {"ITEMS_A", "ITEMS_B"}, "presence": {"ITEM"}}
        for key, more in extra.items():
            template = prompts.load(self.cfg["prompts"][key])
            self.assertEqual(prompts.placeholders(template), META_FIELDS | more, key)
            for name in ("Colby", "Hood", "Deloitte"):
                self.assertNotIn(name, template)

    def test_prompts_share_a_byte_identical_prefix_through_the_transcript(self):
        # Prompt caching only applies across calls with an identical prefix, so every
        # prompt on one transcript starts with the same context + transcript block.
        a = [ref("a.0.0", "a", "Ship Friday", "we ship the Oracle forms deck on Friday", type="decision")]
        b = [ref("b.0.0", "b", "Deck ships Friday", "ship the Oracle forms deck", type="decision")]
        rendered = [
            R._prompt(self.cfg, "reference", self.prep),
            R._prompt(self.cfg, "matcher", self.prep, ITEMS_A=R.item_json(a[0], with_id=True),
                      ITEMS_B=R.item_json(b[0], with_id=True)),
            R._prompt(self.cfg, "presence", self.prep, ITEM=R.item_json(a[0], with_id=False)),
            R._prompt(self.cfg, "presence", self.prep, ITEM=R.item_json(b[0], with_id=False)),
        ]
        body = self.prep.render()
        end = rendered[0].index(body) + len(body)
        for prompt in rendered[1:]:
            self.assertEqual(prompt[:end], rendered[0][:end])
            self.assertNotIn("Ship Friday", prompt[:end])          # per-call items come after it
            self.assertNotIn("Deck ships Friday", prompt[:end])


class TestBuild(_Base):
    def test_quote_rejects_are_dropped_before_matching_and_recorded(self):
        send = raw("Send deck to Jamie", "I will send the revised deck to Jamie", mine=True)
        fake = raw("Cancel migration", "we are cancelling the migration", type="decision")
        ask = self.fake_ask({"reference_a": lambda p: ({"verdict": "present"} if "THE ITEM" in p
                                                       else {"items": [send, fake]}),
                             "reference_b": lambda p: ({"verdict": "present"} if "THE ITEM" in p
                                                       else {"items": [send]}),
                             "matcher": lambda p: {"pairs": [{"a": "a.0.0", "b": "b.0.0"}]}})
        out = R.build(self.prep, self.cfg, ask)
        self.assertEqual([i.id for i in out.accepted], ["a.0.0"])
        self.assertEqual([i.text for i in out.rejected], ["Cancel migration"])
        self.assertEqual(out.both_found, frozenset({"a.0.0"}))
        matcher_prompt = next(p for r, p in self.asked if r == "matcher")
        self.assertNotIn("Cancel migration", matcher_prompt)
        self.assertEqual([r for r, _ in self.asked], ["reference_a", "reference_b", "matcher"])

    def test_replicate_ids_never_collide(self):
        ask = self.fake_ask({"reference_a": {"items": [raw("x", "ship")]}})
        first = R.extract(self.prep, self.cfg, "a", ask)
        second = R.extract(self.prep, self.cfg, "a", ask, replicate=1)
        self.assertNotEqual(first[0].id, second[0].id)

    def test_unknown_family_fails(self):
        with self.assertRaises(ValueError):
            R.extract(self.prep, self.cfg, "c", self.fake_ask({}))


class TestStabilityHelpers(_Base):
    def test_rerun_counts_match_my_tasks_only(self):
        first = [ref("a.0.0", "a", "Send deck", "send the revised deck", mine=True),
                 ref("a.0.1", "a", "Ship", "we ship", type="decision")]
        second = [ref("a.1.0", "a", "Send the deck", "send the revised deck", mine=True)]
        ask = self.fake_ask({"matcher": {"pairs": [{"a": "a.0.0", "b": "a.1.0"}]}})
        self.assertEqual(R.rerun_counts(first, second, self.prep, self.cfg, ask), (1, 1, 1))

    def test_no_my_tasks_makes_no_call(self):
        self.assertEqual(R.rerun_counts([], [], self.prep, self.cfg, self.fake_ask({})), (0, 0, 0))

    def test_gate_needs_evidence_and_possible_counts(self):
        cfg = {"eval": {"stability_min_jaccard": 0.8}}
        with self.assertRaises(ValueError):
            R.stable([], cfg)
        with self.assertRaises(ValueError):
            R.jaccard(matched=3, n_a=2, n_b=5)


class TestCalibrationPairs(_Base):
    def test_ambiguous_pairs_are_left_out(self):
        a = [ref("a0", "a", "Data request", "send the data request and the access request"),
             ref("a1", "a", "Access request", "send the data request and the access request"),
             ref("a2", "a", "Deck to Jamie", "I will send the revised deck", mine=True)]
        b = [ref("b0", "b", "Data request", "send the data request and the access request"),
             ref("b1", "b", "Deck", "the revised deck to Jamie by Thursday", mine=True)]
        positives, negatives = R.calibration_pairs(a, b, self.prep, self.cfg)
        self.assertEqual(positives, [])                     # a quote two items share is no proof of sameness
        self.assertNotIn((a[2], b[1]), negatives)          # overlapping spans may be one statement
        self.assertNotIn((a[2], b[0]), negatives)          # mine differs: match() drops it in code, a free "no"

    def test_mine_disagreement_is_not_a_positive(self):
        a = [ref("a0", "a", "Send deck", "I will send the revised deck", mine=True)]
        b = [ref("b0", "b", "Send deck", "I will send the revised deck", mine=False)]
        self.assertEqual(R.calibration_pairs(a, b, self.prep, self.cfg), ([], []))

    def test_normalize_quote_only_folds(self):
        # Review fix 9: whitespace collapse and edge strip were dead for the tokenizer.
        self.assertEqual(R.normalize_quote(" “Ship  It.” — ok"), ' "ship  it." - ok')
        self.assertEqual(R.normalize_quote(""), "")


class TestMatchGuards(_Base):
    def test_empty_side_makes_no_call(self):
        one = [ref("a0", "a", "x", "ship")]
        self.assertEqual(R.match(one, [], self.prep, self.cfg, self.fake_ask({})), [])
        self.assertEqual(R.match([], one, self.prep, self.cfg, self.fake_ask({})), [])

    def test_outcomes_without_calibration_pairs_make_no_call(self):
        one = [ref("a0", "a", "x", "ship")]
        other = [ref("b0", "b", "y", "migration", type="risk")]
        self.assertEqual(R.matcher_outcomes(one, other, [], [], self.prep, self.cfg, self.fake_ask({})), ([], []))

    def test_outcomes_see_every_item_as_a_distractor(self):
        a = [ref("a0", "a", "Send deck", "send the revised deck", mine=True),
             ref("a1", "a", "Ship Friday", "we ship the Oracle forms deck", type="decision")]
        b = [ref("b0", "b", "Email the deck", "send the revised deck", mine=True),
             ref("b1", "b", "Migration risk", "the data migration slips", type="risk")]
        ask = self.fake_ask({"matcher": {"pairs": [{"a": "a0", "b": "b0"}]}})
        got = R.matcher_outcomes(a, b, [(a[0], b[0])], [], self.prep, self.cfg, ask)
        self.assertEqual(got, ([True], []))
        (_role, prompt), = self.asked
        for item in a + b:                                  # the full lists, not just the pair
            self.assertIn(item.text, prompt)


class TestVerifierFixes(_Base):
    """Regressions from the #15 independent verification (each failed before its fix)."""

    def presence_all(self, verdict="present"):
        return lambda p: {"verdict": verdict}

    # Fix 1: a type or mine disagreement is never settled by family a winning.
    def test_task_never_pairs_with_a_non_task_or_across_mine(self):
        vendor = "pull the vendor list together"
        a = [ref("a0", "a", "Pat to pull vendor list", vendor, type="decision"),
             ref("a1", "a", "Pull vendor list", vendor, mine=True),
             ref("a2", "a", "Ship Friday", "we ship the Oracle forms deck", type="decision")]
        b = [ref("b0", "b", "Pull the vendor list", vendor, mine=True),
             ref("b1", "b", "Pull vendor list", vendor, mine=False),
             ref("b2", "b", "Deck ships Friday", "we ship the Oracle forms deck", type="fact")]
        ask = self.fake_ask({"matcher": {"pairs": [{"a": "a0", "b": "b0"},     # decision vs task
                                                   {"a": "a1", "b": "b1"},     # mine vs not mine
                                                   {"a": "a2", "b": "b2"}]}})  # decision vs fact: fine
        self.assertEqual(R.match(a, b, self.prep, self.cfg, ask), [("a2", "b2")])

    def test_probe_my_task_does_not_leave_the_denominator(self):
        vendor = "pull the vendor list together"
        ask = self.fake_ask({
            "reference_a": lambda p: {"verdict": "absent"} if "THE ITEM" in p else
            {"items": [raw("Pat to pull vendor list", vendor, type="decision")]},
            "reference_b": lambda p: {"verdict": "present"} if "THE ITEM" in p else
            {"items": [raw("Pull the vendor list", vendor, mine=True)]},
            "matcher": {"pairs": [{"a": "a.0.0", "b": "b.0.0"}]}})
        out = R.build(self.prep, self.cfg, ask)
        self.assertEqual(out.both_found, frozenset())
        self.assertEqual({i.id for i in out.contested}, {"a.0.0", "b.0.0"})   # escalated, not waved through

    # Fix 2: token sequences within one turn, with a minimum length.
    def test_quote_check_tokens_within_one_turn(self):
        quotes = {"s": False, "ok": False, "on": False, "together? sure, i will": False,
                  "Sure I will": True, "‘Sure, I will’": True, "We don’t ship the Oracle": False}
        items = [ref(f"a{n}", "a", q, q) for n, q in enumerate(quotes)]
        kept, rejected = R.quote_check(items, self.prep, self.cfg)
        self.assertEqual({i.quote for i in kept}, {q for q, ok in quotes.items() if ok})
        self.assertEqual(self.cfg["eval"]["reference"]["min_quote_words"], 3)

    def test_build_applies_the_word_floor(self):
        ask = self.fake_ask({"reference_a": {"items": [raw("x", "on")]}, "reference_b": {"items": []}})
        self.assertEqual([i.quote for i in R.build(self.prep, self.cfg, ask).rejected], ["on"])

    # Fix 3: singletons both families accepted are de-duplicated by one more match.
    def test_accepted_singletons_that_match_collapse_into_the_a_item(self):
        a_item = raw("Send deck to Jamie", "I will send the revised deck to Jamie", mine=True)
        b_item = raw("Email Jamie the deck", "send the revised deck to Jamie by Thursday", mine=True)
        calls = []

        def matcher(prompt):
            calls.append(prompt)
            return {"pairs": [] if len(calls) == 1 else [{"a": "a.0.0", "b": "b.0.0"}]}
        ask = self.fake_ask({
            "reference_a": lambda p: {"verdict": "present"} if "THE ITEM" in p else {"items": [a_item]},
            "reference_b": lambda p: {"verdict": "present"} if "THE ITEM" in p else {"items": [b_item]},
            "matcher": matcher})
        out = R.build(self.prep, self.cfg, ask)
        self.assertEqual([i.id for i in out.accepted], ["a.0.0"])
        self.assertEqual(out.both_found, frozenset({"a.0.0"}))
        self.assertEqual(out.collapsed, (("a.0.0", "b.0.0"),))
        self.assertEqual(len(calls), 2)

    # Fix 4: calibration pairs that are unambiguous and true near misses.
    def test_positive_quote_must_be_unique_across_both_lists(self):
        q = "I will send the revised deck"
        a = [ref("a0", "a", "Send deck", q, mine=True)]
        b = [ref("b0", "b", "Email deck", q, mine=True), ref("b1", "b", "Deck decision", q, type="decision")]
        self.assertEqual(R.calibration_pairs(a, b, self.prep, self.cfg)[0], [])

    def test_tasks_agreeing_on_mine_and_owner_are_not_negatives(self):
        a = [ref("a0", "a", "Sam asks for list", "Can you pull the vendor list", owner="Sam")]
        b = [ref("b0", "b", "Sam pulls list", "Sure, I will do that today", owner="Sam"),
             ref("b1", "b", "Jo pulls list", "Sure, I will do that today", owner="Jo")]
        self.assertEqual(R.calibration_pairs(a, b, self.prep, self.cfg)[1], [(a[0], b[1])])

    def test_negatives_are_near_in_the_transcript(self):
        a = [ref("a0", "a", "Ship", "we ship the Oracle forms deck", type="fact")]
        b = [ref("b0", "b", "Revenue", "Revenue for the quarter came in", type="fact"),      # 2 turns on
             ref("b1", "b", "Checklist", "Who owns the cutover checklist", type="fact")]     # 4 turns on
        _, negatives = R.calibration_pairs(a, b, self.prep, self.cfg)
        self.assertEqual(negatives, [(a[0], b[0])])
        self.assertEqual(self.cfg["eval"]["reference"]["near_miss_max_turns"], 2)


class TestReviewFixes(_Base):
    """Regressions from the #15 /code-review pass (each failed before its fix)."""

    def present(self, items):
        return lambda p: {"verdict": "present"} if "THE ITEM" in p else {"items": items}

    # Fix 2: an unknown owner, or one name a prefix of the other, is not an owner mismatch.
    def test_unknown_or_prefix_owner_is_not_a_negative(self):
        a = [ref("a0", "a", "Pull list", "Can you pull the vendor list", owner="Sam")]
        b = [ref("b0", "b", "Do it today", "Sure, I will do that today"),                  # owner unknown
             ref("b1", "b", "Lee does it", "Sure, I will do that today", owner="sam LEE"),  # prefix
             ref("b2", "b", "Jo does it", "Sure, I will do that today", owner="Jo")]
        self.assertEqual(R.calibration_pairs(a, b, self.prep, self.cfg)[1], [(a[0], b[2])])

    # Fix 3: one task listed once; within-family duplicates are flagged, not dropped.
    def test_reference_prompt_lists_each_task_once(self):
        template = prompts.load(self.cfg["prompts"]["reference"])
        self.assertIn("exactly once", template)
        self.assertIn("commitment", template)

    def test_within_family_duplicates_are_flagged(self):
        q = "I will send the revised deck to Jamie"
        ask = self.fake_ask({"reference_a": self.present([raw("Send deck", q, mine=True),
                                                          raw("Deck to Jamie", f"{q}.", mine=True)]),
                             "reference_b": self.present([])})
        out = R.build(self.prep, self.cfg, ask)
        self.assertEqual(out.duplicates, (("a.0.0", "a.0.1"),))
        self.assertEqual(len(out.accepted), 2)                          # flagged, not dropped

    # Fix 4: mine is derived from owner and owner_basis; disagreements are counted.
    def test_mine_is_derived_from_owner_and_basis(self):
        says_not_mine = dict(raw("Send deck", "I will send the revised deck to Jamie", mine=True), mine=False)
        says_mine = dict(raw("Jamie ships", "we ship the Oracle forms deck", owner="Jamie"), mine=True)
        ask = self.fake_ask({"reference_a": self.present([says_not_mine, says_mine]),
                             "reference_b": self.present([])})
        items = R.extract(self.prep, self.cfg, "a", ask)
        self.assertEqual([i.mine for i in items], [True, False])
        self.assertEqual(R.build(self.prep, self.cfg, ask).mine_disagreements, 2)

    def test_me_or_the_full_owner_name_in_any_spelling_is_the_me_label(self):
        first, last = self.cfg["owner"]["name"].split()
        spellings = ["me", " ME ", "Me.", f"{first} {last}", f"{first} {last}".upper(), f"{last}, {first}",
                     f"{first}\u00a0{last}", f"{first.lower()}  {last.lower()}."]
        others = [first, f" {first.lower()} ", f"{first} Jones", "Pattern", "Jamie"]   # a first name can be anyone's
        mine = [dict(raw("Send deck", "I will send the revised deck to Jamie", mine=True), owner=s)
                for s in spellings]
        theirs = [raw("Jamie ships", "we ship the Oracle forms deck", owner=s) for s in others]
        ask = self.fake_ask({"reference_a": self.present([*mine, *theirs]), "reference_b": self.present([])})
        items = R.extract(self.prep, self.cfg, "a", ask)
        self.assertEqual([i.owner for i in items], [prepare.ME] * len(spellings) + others)
        self.assertEqual([i.mine for i in items], [True] * len(spellings) + [False] * len(others))
        self.assertEqual(R.build(self.prep, self.cfg, ask).mine_disagreements, 0)
        self.assertIsNone(R.owner_label(None, R.owner_spellings(self.cfg)))

    def test_owner_spellings_are_built_once_per_extract_call(self):
        items = [raw("Send deck", "I will send the revised deck to Jamie", mine=True)] * 3
        ask = self.fake_ask({"reference_a": self.present(items)})
        with mock.patch.object(R, "owner_spellings", wraps=R.owner_spellings) as built:
            R.extract(self.prep, self.cfg, "a", ask)
        self.assertEqual(built.call_count, 1)

    # Fix 5: quotes overlap only within one turn, by containment or a 2-token edge.
    def test_overlap_needs_one_turn_and_a_real_edge(self):
        a = [ref("a0", "a", "Revenue", "Revenue for the", type="fact"),
             ref("a1", "a", "Risk", "The main risk is that the", type="risk"),
             ref("a2", "a", "Risk 2", "The main risk is that the data", type="risk")]
        b = [ref("b0", "b", "Migration", "the data migration slips", type="fact"),       # next turn
             ref("b1", "b", "Migration risk", "the data migration slips", type="risk")]  # same turn
        negatives = R.calibration_pairs(a, b, self.prep, self.cfg)[1]
        self.assertIn((a[0], b[0]), negatives)          # "the" edge, but different turns
        self.assertIn((a[1], b[1]), negatives)          # same turn, 1-token edge: below the floor
        self.assertNotIn((a[2], b[1]), negatives)       # same turn, "the data" edge: may be one statement
        self.assertEqual(self.cfg["eval"]["reference"]["overlap_min_tokens"], 2)

    # Fix 6: the collapse pass contests a mine contradiction instead of keeping both.
    def test_collapse_pass_contests_a_mine_contradiction(self):
        mine = raw("Send deck", "I will send the revised deck to Jamie", mine=True)
        theirs = raw("Jamie gets deck", "send the revised deck to Jamie by Thursday", owner="Jamie")
        ask = self.fake_ask({"reference_a": self.present([mine]), "reference_b": self.present([theirs]),
                             "matcher": {"pairs": [{"a": "a.0.0", "b": "b.0.0"}]}})
        out = R.build(self.prep, self.cfg, ask)
        self.assertEqual(out.accepted, ())
        self.assertEqual({i.id for i in out.contested}, {"a.0.0", "b.0.0"})
        self.assertEqual(out.conflicted, (("a.0.0", "b.0.0"),))
        self.assertEqual(out.collapsed, ())
        self.assertEqual(out.both_found, frozenset())

    # Fix 8: each template is read once per process.
    def test_templates_cached(self):
        items = [raw("Send deck", "I will send the revised deck to Jamie", mine=True)]
        ask = self.fake_ask({"reference_a": self.present(items), "reference_b": self.present(
            [raw("Ship", "we ship the Oracle forms deck", type="decision")]),
            "matcher": {"pairs": []}})
        with mock.patch.object(prompts, "load", wraps=prompts.load) as load:
            R._template.cache_clear()
            R.build(self.prep, self.cfg, ask)
            R.build(self.prep, self.cfg, ask)
        self.assertEqual(load.call_count, 3)                             # reference, matcher, presence

    # Fix 10: the author note never reaches a model.
    def test_author_note_is_stripped_by_the_loader(self):
        for key in ("reference", "matcher", "presence"):
            self.assertTrue(config_file(self.cfg["prompts"][key]).read_text(encoding="utf-8").startswith("<!--"))
            self.assertFalse(prompts.load(self.cfg["prompts"][key]).lstrip().startswith("<!--"), key)
        self.assertFalse(R._prompt(self.cfg, "presence", self.prep, ITEM="x").startswith("<!--"))

    # /simplify item 6: the stability rerun escalates task singletons only.
    def test_types_limit_presence_to_those_singletons(self):
        task = raw("Send deck", "I will send the revised deck to Jamie", mine=True)
        decision = raw("Ship Friday", "we ship the Oracle forms deck", type="decision")
        ask = self.fake_ask({"reference_a": self.present([task, decision]), "reference_b": self.present([])})
        out = R.build(self.prep, self.cfg, ask, replicate=1, types=frozenset({R.TASK}))
        presence = [p for r, p in self.asked if "THE ITEM" in p]
        self.assertEqual(len(presence), 2)                              # the task, asked of both families
        self.assertTrue(all("Send deck" in p and "Ship Friday" not in p for p in presence))
        self.assertEqual([i.id for i in out.accepted], ["a.1.0"])
        self.assertEqual(out.contested, ())                             # the decision is not escalated at all


if __name__ == "__main__":
    unittest.main()
