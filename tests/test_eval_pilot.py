"""The pilot beyond its contract (issue #13): the per-stage call cap, the CLI wiring,
the written report, unusable answers and a stop mid-run. Fake asks only: no model call."""

from __future__ import annotations

import copy
import json
import unittest
from collections import Counter
from unittest import mock

from eval import judge as J
from eval import ledger as L
from eval import pilot as P
from eval import scoring as S
from eval import stages
from pipeline import calls, extract
from pipeline.models import read_jsonl
from test_eval_harness import CliBase
from test_eval_pilot_contract import _Base, fake_ask


class TestCallCap(_Base):
    def test_stage_cap_then_global_fallback(self):
        self.assertEqual(L.call_cap(self.cfg, "pilot"), self.cfg["eval"]["stages"]["pilot"]["max_budget_per_call_usd"])
        cfg = copy.deepcopy(self.cfg)
        del cfg["eval"]["stages"]["pilot"]["max_budget_per_call_usd"]
        self.assertEqual(L.call_cap(cfg, "pilot"), cfg["eval"]["max_budget_per_call_usd"])

    def test_pilot_cap_leaves_room_for_several_calls(self):
        # The guard reserves the whole per-call cap, so a cap near the stage cap would
        # stop the pilot after a fraction of its budget (the reason for the stage key).
        pc = self.cfg["eval"]["stages"]["pilot"]
        self.assertLess(L.call_cap(self.cfg, "pilot"), pc["cap_usd"] / 2)


class TestSemantics(unittest.TestCase):
    def test_every_decision_has_a_no_error_answer_in_its_vocabulary(self):
        self.assertEqual(set(J.PASS), set(J.DECISIONS))
        for decision, answer in J.PASS.items():
            self.assertIn(answer, J.DECISIONS[decision])
            self.assertFalse(P.detected(decision, answer))
        self.assertEqual(set(J.PRESENT_VALUE), set(J.DECISIONS["present"]))
        with self.assertRaises(ValueError):
            P.detected("nonsense", "yes")


class _PilotBase(_Base):
    def run_pilot(self, ask=None):
        return P.run(self.units, self.cfg, ask or fake_ask(self.cfg, self.log), self.out)


class TestRunEdges(_PilotBase):
    def test_unusable_answer_is_an_error_record_and_the_run_goes_on(self):
        inner = fake_ask(self.cfg, self.log)

        def ask(role, prompt, schema, *, system_prompt=None, replicate=0):    # attribution answers off-vocabulary
            out = inner(role, prompt, schema, system_prompt=system_prompt, replicate=replicate)
            return {**out, "answer": "maybe"} if "Does it attribute speech" in prompt else out
        result = self.run_pilot(ask)
        self.assertFalse(result.stopped)
        errors = [r for r in result.records if r["step"] == P.ERROR]
        self.assertTrue(errors)
        self.assertTrue(all(r["decision"] == "attribution" and "JudgeError" in r["error"] for r in errors))
        self.assertFalse([r for r in result.records if r.get("decision") == "attribution" and r["step"] == P.JUDGE])
        self.assertTrue([r for r in result.records if r["step"] == P.CALIBRATION])

    def test_stop_in_the_second_unit_keeps_every_step_of_the_first(self):
        self.run_pilot()
        long_id, long_prep = self.units[0]
        first_long = extract.build_request(long_prep, self.cfg, role=stages.EXTRACTOR).key
        stop_at = [key for _, key in self.log].index(first_long) + 1      # the long unit's first extract runs
        self.log.clear()
        result = self.run_pilot(fake_ask(self.cfg, self.log, fail_after=stop_at))
        self.assertTrue(result.stopped)
        short = {r["step"] for r in result.records if r.get("unit") == "2026-09-02-0900-short"}
        self.assertLessEqual({P.EXTRACT, P.REFERENCE, P.JUDGE, P.CALIBRATION}, short)
        self.assertEqual([r["step"] for r in result.records if r.get("unit") == long_id], [P.PREPARED, P.EXTRACT])
        rep = P.report(result, [{"role": c["role"], "request_key": c["key"], "cost_usd": 0.01} for c in result.calls],
                       self.cfg)
        self.assertEqual(set(rep["cost_by_step"]), {P.EXTRACT, P.REFERENCE, P.JUDGE, P.CALIBRATION})

    def test_report_ignores_reservations_and_counts_unknown_rows_apart(self):
        result = self.run_pilot()
        rows = [{"role": "extractor", "request_key": "probe", "cost_usd": 0.5, "input_tokens": 7},
                {"event": L.RESERVE, "request_key": result.calls[0]["key"], "stage": "pilot", "reserve_usd": 0.75}]
        rep = P.report(result, rows, self.cfg)
        logged = Counter(c["step"] for c in result.calls)
        self.assertEqual({s: (c["calls"], c["usd"], c["cached_calls"]) for s, c in rep["cost_by_step"].items()},
                         {s: (0, 0.0, n) for s, n in logged.items()})    # no row for any logged key: all cached
        self.assertEqual((rep["unattributed_cost"]["calls"], rep["unattributed_cost"]["usd"]), (1, 0.5))
        self.assertEqual(rep["unattributed_cost"]["input_tokens"], 7)
        text = P.report_markdown(rep)
        for heading in ("Cost by step", "Cost by role", "H-S2", "Sizing", "Calibration"):
            self.assertIn(heading, text)


class TestFixes(_PilotBase):
    """Review fixes A-H on the pilot module (issue #13)."""

    def test_a_positive_that_cannot_be_placed_is_not_applicable_and_the_run_goes_on(self):
        with mock.patch.object(J, "plant_positive", side_effect=J.NotApplicable("no section (test)")):
            result = self.run_pilot()
        self.assertFalse(result.stopped)
        positives = [r for r in result.records if r["step"] == P.CALIBRATION and r["kind"] == J.POSITIVE]
        self.assertTrue(positives)
        self.assertTrue(all(r["status"] == P.NOT_APPLICABLE and "no section" in r["reason"] for r in positives))
        matcher_units = {r["unit"] for r in result.records if r["step"] == P.CALIBRATION and r["kind"] == P.MATCHER}
        self.assertEqual(matcher_units, {u for u, prep in self.units if not prep.is_stub})

    def test_b_call_log_is_on_disk_and_load_reads_the_run_back(self):
        result = self.run_pilot()
        on_disk = read_jsonl(self.out.with_name(P.CALLS_FILE))
        self.assertEqual(on_disk, result.calls)
        self.assertEqual(len(on_disk), len(self.log))
        self.assertEqual(P.load(self.out), result)

    def test_b_load_after_a_crash_keeps_what_was_written(self):
        inner = fake_ask(self.cfg, self.log)

        def ask(role, prompt, schema, **kw):
            if len(self.log) >= 3:
                raise RuntimeError("crashed (test)")
            return inner(role, prompt, schema, **kw)
        with self.assertRaises(RuntimeError):
            self.run_pilot(ask)
        loaded = P.load(self.out)
        self.assertEqual(len(loaded.calls), 4)          # three answered, plus the launched call that raised
        self.assertFalse(loaded.stopped)
        rep = P.report(loaded, [], self.cfg, error="RuntimeError: crashed (test)")
        self.assertIn("RUN FAILED: RuntimeError", P.report_markdown(rep))

    def test_c_probe_cap_is_config_and_below_the_step_cap(self):
        self.assertLess(self.cfg["eval"]["stages"]["pilot"]["probe_max_budget_usd"], L.call_cap(self.cfg, "pilot"))

    def test_c_probe_records_a_budget_stop_and_carries_on(self):
        def make(_cfg):
            def ask(*_a, **_kw):
                raise L.BudgetStop("stage 'pilot' cap reached (test)")
            return ask
        out = P.probe(self.cfg, make)
        self.assertEqual(set(out), {"default", "no_settings"})
        for entry in out.values():
            self.assertEqual((entry["ok"], entry["budget_stop"]), (False, True))
            self.assertIn("BudgetStop", entry["error"])

    def test_d_cost_joins_every_row_of_a_logged_key_and_splits_this_invocation(self):
        result = self.run_pilot()
        keys = list(dict.fromkeys(c["key"] for c in result.calls))
        old = [{"request_key": k, "cost_usd": 0.01} for k in keys]          # an earlier invocation's rows
        new = [{"request_key": keys[0], "cost_usd": 0.02},                   # a fresh call of the first key
               {"request_key": "stray", "cost_usd": 0.004}]
        rep = P.report(result, old + new, self.cfg, new_rows=new)
        steps = rep["cost_by_step"].values()
        self.assertEqual(sum(c["calls"] for c in steps), len(keys) + 1)
        self.assertAlmostEqual(sum(c["usd"] for c in steps), 0.01 * len(keys) + 0.02)
        logged = Counter(c["step"] for c in result.calls)
        fresh = Counter(c["step"] for c in result.calls if c["key"] == keys[0])
        self.assertEqual({s: c["cached_calls"] for s, c in rep["cost_by_step"].items()},
                         {s: n - fresh[s] for s, n in logged.items()})
        self.assertEqual((rep["unattributed_cost"]["calls"], rep["unattributed_cost"]["usd"]), (1, 0.004))
        self.assertAlmostEqual(rep["spent_this_invocation_usd"], 0.024)

    def test_e_design_effect_is_clamped_at_one(self):
        # m = 0.5: unclamped, 1 + (m - 1) rho = 0.75 would credit 0.67 effective items per transcript.
        self.assertEqual(S.transcripts_needed(rho=0.5, tasks_per_transcript=0.5, min_effective=140), 280)
        self.assertEqual(S.transcripts_needed(rho=0.0, tasks_per_transcript=2.0, min_effective=140), 70)

    def test_f_check_raises_config_errors_before_any_call(self):
        J.check(self.cfg)
        cfg = copy.deepcopy(self.cfg)
        del cfg["models"][J.ESCALATE_2]
        with self.assertRaises(J.JudgeError):
            J.check(cfg)
        with self.assertRaises(J.JudgeError):
            P.run(self.units, cfg, fake_ask(cfg, self.log), self.out)       # the broken cfg, not self.cfg
        self.assertEqual(self.log, [])
        self.assertFalse(self.out.exists())

    def test_f_check_raises_on_a_judge_prompt_whose_decisions_do_not_match(self):
        text = J.prompts.load(self.cfg["prompts"]["judge"])
        dropped = text[:text.rindex("## decision:")]        # the last decision block removed
        J._parsed_template.cache_clear()
        try:
            with mock.patch.object(J.prompts, "load", return_value=dropped):
                with self.assertRaises(J.JudgeError):
                    J.check(self.cfg)
        finally:
            J._parsed_template.cache_clear()

    def test_g_h_s2_reports_recall_with_n_tokens_and_the_order_note(self):
        result = self.run_pilot()
        rows = [{"request_key": k, "cost_usd": 0.01, "input_tokens": 1000}
                for k in dict.fromkeys(c["key"] for c in result.calls)]
        rep = P.report(result, rows, self.cfg)
        text = P.report_markdown(rep)
        self.assertEqual(text.count("order-confounded"), 1)
        self.assertIn("order-confounded", rep["judge_cost_note"])
        for v, h in rep["h_s2"].items():
            mine = [r for r in result.records if r["step"] == P.JUDGE and r["variant"] == v
                    and r["decision"] == "present" and r.get("mine")]
            self.assertGreater(len(mine), 0)
            self.assertEqual(h["my_task_recall"], {"estimate": 1.0, "n": len(mine)})    # the fake says "yes"
            c = h["extract_cost"]
            self.assertGreater(c["input_tokens"], 0)
            self.assertIn(f"| {v} | 1.000 ({len(mine)}) | extract | {c['calls']} | {c['usd']:.4f} | "
                          f"{c['input_tokens']} | ", text)

    def test_h_extract_records_carry_the_template_and_schema_hashes(self):
        result = self.run_pilot()
        extracts = [r for r in result.records if r["step"] == P.EXTRACT]
        self.assertTrue(extracts)
        req = extract.build_request(self.units[0][1], self.cfg, role=stages.EXTRACTOR)
        # The same hash eval/PREREGISTRATION.md registers and preflight.provenance records.
        self.assertEqual(req.template_sha256, calls.config_file_sha(self.cfg["prompts"]["extract"]))
        for r in extracts:
            self.assertEqual((r["template_sha256"], r["schema_sha256"]), (req.template_sha256, req.schema_sha256))


class TestCliPilot(CliBase):
    """`run --stage pilot` with make_ask replaced by a fake: what each ask is built with,
    and what the run directory holds afterwards."""

    def setUp(self):
        super().setUp()
        self.built, self.log = [], []

    def make_ask(self, fail_after=None, wrap=None):
        def make(cfg, **kwargs):
            self.built.append((list(cfg["cli"]["base_args"]), kwargs))
            ask = fake_ask(cfg, self.log, fail_after=fail_after)
            return wrap(ask) if wrap else ask
        return make

    def main(self, *argv, fail_after=None, wrap=None):
        with mock.patch.object(P, "make_ask", self.make_ask(fail_after, wrap)), \
             mock.patch("eval.preflight.refusals", return_value=[]), \
             mock.patch("eval.preflight.provenance", return_value={"commit": "abc"}):
            return super().main(*argv)

    def test_run_cached_on_the_stage_cap_then_probe_uncached_on_its_own_cap(self):
        code, out = self.main("run", "--stage", "pilot")
        self.assertEqual(code, 0, out)
        base = self.cfg["cli"]["base_args"]
        self.assertEqual([args for args, _ in self.built], [base, base, base + list(P.NO_SETTINGS_ARGS)])
        probe_cap = self.cfg["eval"]["stages"]["pilot"]["probe_max_budget_usd"]
        expected = [(L.call_cap(self.cfg, "pilot"), L.eval_dir(self.cfg) / "cache")] + [(probe_cap, None)] * 2
        self.assertEqual([(kw["max_budget_usd"], kw["cache_dir"]) for _, kw in self.built], expected)
        for _, kwargs in self.built:
            self.assertEqual(kwargs["ledger"], L.ledger_path(self.cfg))
            self.assertTrue(callable(kwargs["before_call"]))
        run_dir, rep = self.run_dir(), self.report()
        self.assertTrue(rep["run_id"].startswith("pilot-"))
        self.assertEqual(set(rep["probe"]), {"default", "no_settings"})
        probed = [c for c in read_jsonl(run_dir / P.CALLS_FILE) if c["step"] == P.PROBE]
        self.assertEqual((len(probed), len({c["key"] for c in probed})), (2, 2))   # base_args are in the key
        self.assertIn(P.PROBE, rep["cost_by_step"])
        records =[json.loads(line) for line in (run_dir / P.RECORDS_FILE).read_text(encoding="utf-8").splitlines()]
        units = {r["unit"] for r in records if "unit" in r}
        self.assertEqual(len(units), self.cfg["eval"]["sample"]["pilot_n"])
        self.assertIn("# Pilot pilot-", (run_dir / P.REPORT_MD).read_text(encoding="utf-8"))
        self.assertEqual(self.main("status")[0], 0)

    def test_dry_run_notes_the_run_time_steps_and_makes_no_ask(self):
        code, out = self.main("run", "--stage", "pilot", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn(P.DRY_RUN_NOTE, out)
        self.assertIn("zero model calls", out)
        self.assertEqual(self.built, [])

    def test_budget_stop_writes_the_report_then_needs_resolution(self):
        with self.assertRaises(L.BudgetStop):
            self.main("run", "--stage", "pilot", fail_after=3)
        rep = self.report()
        self.assertTrue(rep["stopped"])
        self.assertIn("cap", rep["budget_stop"])
        self.assertTrue((self.run_dir() / P.REPORT_MD).is_file())
        code, out = self.main("status")
        self.assertEqual(code, 1)
        self.assertIn("UNRESOLVED", out)

    def test_b_a_crash_still_writes_a_report_that_says_so_then_needs_resolution(self):
        def wrap(ask):
            def crashing(role, prompt, schema, **kw):
                if len(self.log) >= 5:
                    raise RuntimeError("disk on fire (test)")
                return ask(role, prompt, schema, **kw)
            return crashing
        with self.assertRaises(RuntimeError):
            self.main("run", "--stage", "pilot", wrap=wrap)
        rep = self.report()
        self.assertIn("RuntimeError: disk on fire", rep["error"])
        self.assertIsNone(rep["probe"])
        # Built from calls.jsonl: five answered calls plus the one that raised, none with a ledger row.
        self.assertEqual(sum(c["cached_calls"] for c in rep["cost_by_step"].values()), 6)
        self.assertIn("RUN FAILED", (self.run_dir() / P.REPORT_MD).read_text(encoding="utf-8"))
        code, out = self.main("status")
        self.assertEqual(code, 1)
        self.assertIn("UNRESOLVED", out)

    def test_c_a_probe_budget_stop_is_in_the_report_and_fails_the_run(self):
        def wrap(ask):
            def probe_refused(role, prompt, schema, **kw):
                if prompt == P.PROBE_PROMPT:
                    raise L.BudgetStop("probe refused (test)")
                return ask(role, prompt, schema, **kw)
            return probe_refused
        with self.assertRaises(L.BudgetStop):
            self.main("run", "--stage", "pilot", wrap=wrap)
        rep = self.report()
        self.assertFalse(rep["stopped"])
        self.assertIsNone(rep["error"])
        for entry in rep["probe"].values():
            self.assertEqual((entry["ok"], entry["budget_stop"]), (False, True))
        self.assertIn("UNRESOLVED", self.main("status")[1])


if __name__ == "__main__":
    unittest.main()
