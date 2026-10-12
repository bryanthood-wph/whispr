"""CONTRACT for eval/reference.py (issue #15, docs/plan/B-eval.md B.4). Written by the
manager before the build and checksummed: the builder must not edit this file.

The reference ("truth") with no humans: two model families extract typed items, a
matcher pairs them, singletons are escalated present/absent to both families, a
deterministic substring check rejects unquoted items, and a stability rerun gates
the whole eval. All model access is through an injected `ask` (eval/ask.py), so
these tests use plain functions and no CLI.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eval import reference as R
from pipeline import prepare
from pipeline.config import config_file, load_config
from pipeline.jsonschema_lite import validate
from pipeline_helpers import overlay, transcript

TURNS = [
    ("00:00:05", "Others", "Okay so the decision is we ship the Oracle forms deck on Friday."),
    ("00:00:20", "Me", "I will send the revised deck to Jamie by Thursday afternoon."),
    ("00:00:41", "Others", "Revenue for the quarter came in at 4.2 million which is above plan."),
    ("00:01:02", "Others", "The main risk is that the data migration slips into next month."),
    ("00:01:30", "Me", "Who owns the cutover checklist, do we know yet?"),
    ("00:01:45", "Others", "We don't have a date for the go-live yet - maybe next week."),
]


def item(text, quote, *, type="task", owner=None, owner_basis="unclear", mine=False, due=None, importance=2):
    return {"type": type, "text": text, "owner": owner, "owner_basis": owner_basis, "mine": mine,
            "due": due, "quote": quote, "importance": importance}


SHIP = item("Ship the deck Friday", "the decision is we ship the Oracle forms deck on Friday", type="decision")
SEND = item("Send revised deck to Jamie", "I will send the revised deck to Jamie by Thursday afternoon",
            owner="Me", owner_basis="volunteered", mine=True, due="Thursday afternoon", importance=3)
REVENUE = item("Quarter revenue 4.2M", "Revenue for the quarter came in at 4.2 million", type="number")
RISK = item("Migration may slip", "the data migration slips into next month", type="risk")
GOLIVE = item("Go-live date not set", "We don't have a date for the go-live yet - maybe next week",
              type="open_question")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.cfg = load_config(overlay=overlay(root))
        path = root / "transcripts" / "2026-09-01-0900-t.md"
        path.write_text(transcript("2026-09-01T09:00:00-04:00", TURNS), encoding="utf-8")
        self.prep = prepare.prepare([prepare.parse(path)], self.cfg)
        self.asked = []

    def tearDown(self):
        self._tmp.cleanup()

    def ask_returning(self, outputs):
        """An `ask` that records each call and returns outputs[role] (a value or a callable(prompt))."""
        def ask(role, prompt, schema, *, system_prompt=None, replicate=0):
            self.asked.append((role, replicate))
            out = outputs[role]
            return out(prompt) if callable(out) else out
        return ask


class TestSchemaAndConfig(_Base):
    def test_item_types_and_configured_artifacts(self):
        self.assertEqual(R.ITEM_TYPES, ("decision", "task", "number", "risk", "open_question", "fact"))
        for key in ("reference", "matcher", "presence"):
            self.assertIn(key, self.cfg["prompts"])
            self.assertIn(key, self.cfg["schemas"])
        schema = json.loads(config_file(self.cfg["schemas"]["reference"]).read_text(encoding="utf-8"))
        self.assertEqual(validate({"items": [SHIP, SEND]}, schema), [])
        self.assertNotEqual(validate({"items": [dict(SHIP, type="gossip")]}, schema), [])
        self.assertNotEqual(validate({"items": [dict(SHIP, importance=4)]}, schema), [])


class TestExtract(_Base):
    def test_family_selects_role_and_ids_are_deterministic(self):
        ask = self.ask_returning({"reference_a": {"items": [SHIP, SEND]}, "reference_b": {"items": [SEND]}})
        a1 = R.extract(self.prep, self.cfg, "a", ask)
        a2 = R.extract(self.prep, self.cfg, "a", ask)
        b = R.extract(self.prep, self.cfg, "b", ask)
        self.assertEqual([r for r, _ in self.asked], ["reference_a", "reference_a", "reference_b"])
        self.assertEqual([i.id for i in a1], [i.id for i in a2])
        self.assertEqual(len({i.id for i in a1 + b}), 3)
        self.assertEqual({i.family for i in a1}, {"a"})
        self.assertEqual((a1[1].type, a1[1].mine, a1[1].due, a1[1].importance), ("task", True, "Thursday afternoon", 3))

    def test_stability_rerun_passes_its_replicate(self):
        ask = self.ask_returning({"reference_a": {"items": [SHIP]}})
        R.extract(self.prep, self.cfg, "a", ask, replicate=1)
        self.assertEqual(self.asked, [("reference_a", 1)])


class TestQuoteCheck(_Base):
    def test_substring_after_whitespace_and_case_normalization(self):
        ask = self.ask_returning({"reference_a": {"items": [
            SHIP,
            dict(REVENUE, quote="revenue   for the QUARTER came in at 4.2 million"),
            dict(RISK, quote="the migration is cancelled"),          # not said
        ]}})
        items = R.extract(self.prep, self.cfg, "a", ask)
        kept, rejected = R.quote_check(items, self.prep, self.cfg)
        self.assertEqual([i.text for i in kept], [SHIP["text"], REVENUE["text"]])
        self.assertEqual([i.text for i in rejected], [RISK["text"]])

    def test_typographic_punctuation_folds_to_plain(self):
        # A model may type curly quotes or dashes the transcript doesn't have (L9).
        curly = "We don’t have a date for the go-live yet — maybe next week"
        ask = self.ask_returning({"reference_a": {"items": [dict(GOLIVE, quote=curly)]}})
        kept, rejected = R.quote_check(R.extract(self.prep, self.cfg, "a", ask), self.prep, self.cfg)
        self.assertEqual((len(kept), len(rejected)), (1, 0))
        self.assertEqual(R.normalize_quote("It’s “done” — ok"),
                         R.normalize_quote("it's \"done\" - ok"))


class TestStub(unittest.TestCase):
    def test_stub_transcript_gives_no_items_and_no_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = load_config(overlay=overlay(root))
            path = root / "transcripts" / "2026-09-01-0900-t.md"
            path.write_text(transcript("2026-09-01T09:00:00-04:00", [("00:00:01", "Others", "Hello?")]),
                            encoding="utf-8")
            prep = prepare.prepare([prepare.parse(path)], cfg)
            self.assertTrue(prep.is_stub)

            def ask(*_a, **_k):
                raise AssertionError("a stub must not reach the model")
            self.assertEqual(R.extract(prep, cfg, "a", ask), [])


class TestConsensus(_Base):
    def items(self):
        ask = self.ask_returning({"reference_a": {"items": [SHIP, SEND, RISK]},
                                  "reference_b": {"items": [SEND, REVENUE]}})
        return R.extract(self.prep, self.cfg, "a", ask), R.extract(self.prep, self.cfg, "b", ask)

    def test_matcher_pairs_are_one_to_one_and_ids_checked(self):
        a, b = self.items()
        self.asked.clear()
        ask = self.ask_returning({"matcher": {"pairs": [
            {"a": a[1].id, "b": b[0].id},
            {"a": a[1].id, "b": b[1].id},          # a[1] already paired: dropped
            {"a": a[0].id, "b": b[0].id},          # b[0] already paired: dropped
            {"a": b[1].id, "b": a[2].id},          # ids in the wrong family slots: ignored
            {"a": "nope", "b": b[1].id},           # unknown id: ignored
        ]}})
        self.assertEqual(R.match(a, b, self.prep, self.cfg, ask), [(a[1].id, b[0].id)])
        self.assertEqual([r for r, _ in self.asked], ["matcher"])

    def test_matched_accepted_singletons_escalated_to_both_families(self):
        a, b = self.items()
        pairs = [(a[1].id, b[0].id)]                               # SEND found by both
        self.asked.clear()

        # Keyed on item text, which never appears in the transcript, so the presence
        # prompt may carry any amount of transcript context.
        def verdict(says_risk, says_revenue):
            return lambda prompt: {"verdict": says_risk if RISK["text"] in prompt
                                   else says_revenue if REVENUE["text"] in prompt else "absent"}
        ask = self.ask_returning({"reference_a": verdict("present", "absent"),
                                  "reference_b": verdict("present", "present")})
        ref = R.consensus(a, b, pairs, self.prep, self.cfg, ask)
        # SEND: found by both. RISK: a only, both say present. SHIP: a only, both say absent.
        # REVENUE: b only, family a says absent.
        self.assertEqual(len(ref.accepted), 2)                     # a pair counts once
        self.assertEqual({i.text for i in ref.accepted}, {SEND["text"], RISK["text"]})
        self.assertEqual({i.text for i in ref.contested}, {SHIP["text"], REVENUE["text"]})
        self.assertEqual(ref.both_found, frozenset({a[1].id}))     # the family-a item represents a pair
        self.assertIn(a[1].id, {i.id for i in ref.accepted})
        self.assertNotIn(b[0].id, {i.id for i in ref.accepted})
        singletons = 3                                             # SHIP, RISK (a only), REVENUE (b only)
        self.assertEqual(sorted(r for r, _ in self.asked), sorted(["reference_a", "reference_b"] * singletons))
        self.assertFalse({i.id for i in ref.accepted} & {i.id for i in ref.contested})


class TestMatcherCalibration(_Base):
    """B.4/B.5: paraphrase pairs (the same statement, worded differently) and near-miss
    pairs (same type, a different statement) give the matcher its own sensitivity and
    specificity. Pairs are built in code from real items: same quote = paraphrase.

    The matcher is calibrated as it is used: one call over a transcript's two full item
    lists, where every other item is a distractor. `matcher_outcomes` returns, per
    positive and per negative pair, whether the matcher paired it; outcomes from many
    transcripts pool by concatenation before `matcher_rates`."""

    def test_pairs_and_rates(self):
        ask = self.ask_returning({"reference_a": {"items": [SEND, RISK]},
                                  "reference_b": {"items": [dict(SEND, text="Email Jamie the new deck"),
                                                            dict(RISK, quote=REVENUE["quote"], text="Revenue risk")]}})
        a, b = R.extract(self.prep, self.cfg, "a", ask), R.extract(self.prep, self.cfg, "b", ask)
        positives, negatives = R.calibration_pairs(a, b, self.prep, self.cfg)
        self.assertEqual(positives, [(a[0], b[0])])
        self.assertEqual(negatives, [(a[1], b[1])])

        def outcomes(pairs):
            self.asked.clear()
            ask = self.ask_returning({"matcher": {"pairs": [{"a": x.id, "b": y.id} for x, y in pairs]}})
            got = R.matcher_outcomes(a, b, positives, negatives, self.prep, self.cfg, ask)
            self.assertEqual([r for r, _ in self.asked], ["matcher"])     # one call over the full lists
            return got
        self.assertEqual(outcomes(positives), ([True], [False]))
        self.assertEqual(outcomes(positives + negatives), ([True], [True]))
        self.assertEqual(outcomes([]), ([False], [False]))
        self.assertEqual(R.matcher_rates([True], [False]), (1.0, 1.0))
        self.assertEqual(R.matcher_rates([True, False], [True, False, False, False]), (0.5, 0.75))
        self.assertEqual(R.matcher_rates([], []), (None, None))


class TestStability(unittest.TestCase):
    """B.4: my-task stability, pooled over the rerun transcripts (a transcript with no
    my-tasks in either run adds nothing, rather than a free 1.0)."""

    def test_jaccard(self):
        self.assertEqual(R.jaccard(matched=3, n_a=4, n_b=5), 3 / 6)
        self.assertEqual(R.jaccard(matched=0, n_a=0, n_b=0), 1.0)    # both empty: identical

    def test_pooled_gate(self):
        cfg = {"eval": {"stability_min_jaccard": 0.8}}
        self.assertTrue(R.stable([(4, 5, 4)], cfg))                  # 4 / 5 = 0.8 exactly: meets the bar
        self.assertFalse(R.stable([(3, 4, 4)], cfg))                 # 3 / 5
        self.assertFalse(R.stable([(0, 0, 0)] * 5 + [(1, 3, 3)], cfg))   # pooled 1 / 5, not a mean of 0.87
        self.assertTrue(R.stable([(0, 0, 0)] * 3, cfg))              # nothing to disagree on

    def test_my_tasks_are_mine_and_tasks(self):
        items = [R.RefItem("a0", "a", **SEND), R.RefItem("a1", "a", **SHIP),
                 R.RefItem("a2", "a", **dict(SEND, mine=False, owner="Jamie"))]
        self.assertEqual([i.id for i in R.my_tasks(items)], ["a0"])


if __name__ == "__main__":
    unittest.main()
