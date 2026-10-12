"""The prompt-tuning loop (eval/dev.py) and its tuning set (eval/frame.py Sample.dev)."""

from __future__ import annotations

import copy
import unittest

from eval import dev as D
from eval import frame as F
from eval import judge as J
from eval import pilot as P
from eval import reference as R
from eval import stages
from pipeline import calls, render
from test_eval_pilot_contract import DOC, REF_ITEMS, _Base, fake_ask


def _item(n: int, cell: str, low_mic=False) -> F.FrameItem:
    return F.FrameItem(f"{cell}-{n:02d}", f"{cell}-{n:02d}.md", "0" * 64, "2026-09-01", "", cell, 20.0,
                       "speaker" if n % 3 == 0 else "other", 30, low_mic)


class TestTuningSet(_Base):
    def frame(self):
        cells = [c["name"] for c in self.cfg["eval"]["sample"]["cells"]]
        return [_item(n, cell, low_mic=None if n == 9 else n == 7) for cell in cells for n in range(60)]

    def test_disjoint_sized_and_leaves_the_draw_unchanged(self):
        frame = self.frame()
        sample = F.draw(frame, self.cfg)
        used = set(sample.pilot) | {x for d in (sample.core, sample.task_only, sample.low_mic, sample.unscreened)
                                    for v in d.values() for x in v}
        tuned = [x for v in sample.dev.values() for x in v]
        self.assertFalse(used & set(tuned))
        by_id = {i.id: i for i in frame}
        self.assertFalse([x for x in tuned if by_id[x].low_mic is not False])     # screened only
        for c in self.cfg["eval"]["sample"]["cells"]:
            self.assertEqual(len(sample.dev[c["name"]]), c["dev"], c["name"])
        no_dev = copy.deepcopy(self.cfg)
        for c in no_dev["eval"]["sample"]["cells"]:
            c["dev"] = 0
        before = F.draw(frame, no_dev)
        self.assertEqual((before.pilot, before.core, before.task_only), (sample.pilot, sample.core, sample.task_only))
        self.assertEqual(F.draw(frame, self.cfg).dev, sample.dev)                 # deterministic

    def test_a_short_cell_gives_what_is_left(self):
        cells = self.cfg["eval"]["sample"]["cells"]
        name = cells[0]["name"]
        frame = [_item(n, name, low_mic=False) for n in range(cells[0]["core"] + cells[0]["task_only"] + 1)]
        frame += [_item(n, c["name"], low_mic=False) for c in cells[1:] for n in range(60)]
        sample = F.draw(frame, self.cfg)
        self.assertLessEqual(len(sample.dev[name]), 1)

    def test_design_uses_the_tuning_units(self):
        frame = self.frame()
        sample = F.draw(frame, self.cfg)
        design = F.design(sample, frame, include_task_only=False, dev=True)
        self.assertEqual({u.id for u in design.units}, {x for v in sample.dev.values() for x in v})


class TestMineOnlySubjects(_Base):
    def items(self):
        return [R.RefItem(f"a.0.{n}", "a", **i) for n, i in enumerate(REF_ITEMS)]

    def test_present_and_tasks_are_mine_only_and_uncapped(self):
        items = self.items()
        present = P._subject_items("present", DOC, items, self.cfg, key="u", mine_only=True)
        self.assertEqual([i.id for _, i in present], [i.id for i in R.my_tasks(items)])
        owners = P._subject_items("task_owner", DOC, items, self.cfg, key="u", mine_only=True)
        self.assertEqual([s for s, _ in owners], [line for _, line in render.tasks(DOC, mine=True)])
        with self.assertRaises(ValueError):
            P._subject_items("supported", DOC, items, self.cfg, key="u", mine_only=True)


class TestRun(_Base):
    def run_dev(self, ask):
        return D._Dev(self.cfg, ask, self.out).run(self.units)

    def test_only_the_default_variant_and_the_dev_decisions(self):
        result = self.run_dev(fake_ask(self.cfg, self.log))
        steps = {r["step"] for r in result.records}
        self.assertNotIn(P.CALIBRATION, steps)
        judged = [r for r in result.records if r["step"] == P.JUDGE]
        self.assertEqual({r["variant"] for r in judged}, {stages.DEFAULT_SYSTEM})
        self.assertEqual({r["decision"] for r in judged}, set(D.DECISIONS))
        self.assertEqual({r["variant"] for r in result.records if r["step"] == P.EXTRACT}, {stages.DEFAULT_SYSTEM})

    def test_measures_count_misses_partials_and_wrong_owners(self):
        base = fake_ask(self.cfg, self.log)

        def ask(role, prompt, schema, *, system_prompt=None, replicate=0):
            out = base(role, prompt, schema, system_prompt=system_prompt, replicate=replicate)
            if "answer" in schema.get("properties", {}):
                if '"Book the review room"' in prompt and "Is it captured in the SUMMARY" in prompt:
                    return {"answer": "partial", "confidence": 0.9}
                if "Owner: me" in prompt and "ITEM\n- Send" in prompt:
                    return {"answer": "no", "confidence": 0.9}
            return out
        result = self.run_dev(ask)
        m = D.measures(self.cfg, result)["dev"]
        live = len([u for u in self.units if not u[1].is_stub])
        mine = sum(r["my_tasks"] for r in result.records if r["step"] == P.REFERENCE)
        partial = sum(1 for r in result.records if r["step"] == P.JUDGE and r["decision"] == "present"
                      and "Book the review room" in r["subject"])
        self.assertTrue(0 < partial < mine)
        self.assertEqual(m["my_task_recall"]["n_tasks"], mine)
        self.assertAlmostEqual(m["my_task_recall"]["estimate"], (mine - 0.5 * partial) / mine)
        self.assertEqual(sum(len(v) for v in m["misses"].values()), partial)
        self.assertTrue(all(x["answer"] == "partial" for v in m["misses"].values() for x in v))
        self.assertEqual(m["my_actions_complete"]["n"], live)
        self.assertTrue(m["wrong_owner"])
        text = D.markdown({"run_id": "r", "errors": 0, "spent_this_invocation_usd": 0.0, "cost_by_step": {},
                           "cost_by_role": {}, **D.measures(self.cfg, result)})
        self.assertIn("My-task recall", text)
        self.assertIn("Book the review room", text)

    def test_a_failed_extract_scores_its_my_tasks_as_misses(self):
        base, failed = fake_ask(self.cfg, self.log), []

        def ask(role, prompt, schema, *, system_prompt=None, replicate=0):
            if role == stages.EXTRACTOR and not failed:          # the first (smallest) live unit
                failed.append(True)
                raise calls.OutputError("schema mismatch (test)")
            return base(role, prompt, schema, system_prompt=system_prompt, replicate=replicate)
        result = self.run_dev(ask)
        m = D.measures(self.cfg, result)["dev"]
        mine = {r["unit"]: r["my_tasks"] for r in result.records if r["step"] == P.REFERENCE}
        lost = [u for u in mine if u not in {r["unit"] for r in result.records if r["step"] == P.EXTRACT}]
        self.assertEqual(len(lost), 1)
        self.assertEqual(m["coverage"]["extract_failed"], lost)
        self.assertEqual(m["my_task_recall"]["n_tasks"], sum(mine.values()))
        self.assertAlmostEqual(m["my_task_recall"]["estimate"], 1 - mine[lost[0]] / sum(mine.values()))
        self.assertEqual([x["answer"] for x in m["misses"][lost[0]]], [D.EXTRACT_FAILED] * mine[lost[0]])
        self.assertEqual(m["coverage"]["unjudged_my_tasks"], 0)
        self.assertEqual(m["my_actions_complete"]["n"], len(mine) - 1)
        text = D.markdown({"errors": 1, "spent_this_invocation_usd": 0.0, "cost_by_step": {}, "cost_by_role": {},
                           "dev": m})
        self.assertIn(f"extract failed (scored as misses): {lost}", text)

    def test_no_judged_tasks_reports_none(self):
        empty = P.Result()
        m = D.measures(self.cfg, empty)["dev"]
        self.assertIsNone(m["my_task_recall"]["estimate"])
        self.assertIn("My-task recall: n/a", D.markdown({"errors": 0, "spent_this_invocation_usd": 0.0,
                                                         "cost_by_step": {}, "cost_by_role": {},
                                                         "dev": m}))


class TestPlan(_Base):
    def test_dev_plans_one_default_extract_per_tuning_unit(self):
        sample = F.Sample(1, [], {}, {}, {}, dev={"x": [u for u, _ in self.units]})
        frame = [F.FrameItem(u, str(self.root / "transcripts" / f"{u}.md"), "0" * 64, "2026-09-01", "", "x",
                             20.0, "other", 1, False) for u, _ in self.units]
        jobs = stages.plan("dev", self.cfg, sample, frame)
        self.assertEqual([j.unit_id for j in jobs], [u for u, _ in self.units])
        self.assertEqual({j.label for j in jobs}, {stages.DEFAULT_SYSTEM})


if __name__ == "__main__":
    unittest.main()
