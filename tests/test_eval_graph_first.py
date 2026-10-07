"""eval/graph_first.py (P2b): the scorer on synthetic stream-json fixtures, and the
harness against a stand-in call and the fake CLI. No model calls, no real user data."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import eval.__main__ as cli
from eval import graph_first as G
from pipeline import models
from pipeline.config import load_config
from pipeline_helpers import FAKE_CLAUDE, fake_cli, overlay, scrubbed_env

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "graph_first"
KG = "mcp__plugin_whispr_whispr-kg__"


def fixture(name: str) -> list[dict]:
    return models.read_jsonl(FIXTURES / f"{name}.jsonl")


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = load_config(overlay=overlay(self.root))
        self.rules = G.rules(self.cfg)
        self.g = G.settings(self.cfg)

    def tearDown(self):
        self._tmp.cleanup()


class TestScoreSession(_Case):
    def score(self, name: str) -> dict:
        return G.score_session(fixture(name), self.rules)

    def test_graph_first_and_cited_passes(self):
        s = self.score("graph_first_cited")
        self.assertEqual(s["first_knowledge_tool"], KG + "search")      # ToolSearch is no lookup
        self.assertTrue(s["first_is_graph"] and s["graph_used"] and s["passed"])
        self.assertIn("forecast agent", s["cited_quote"])                # curly vs straight apostrophe

    def test_vault_first_fails_even_with_a_graph_quote(self):
        s = self.score("vault_first")
        self.assertEqual(s["first_knowledge_tool"], "mcp__vault__vault_search")
        self.assertTrue(s["graph_used"])
        self.assertIsNotNone(s["cited_quote"])
        self.assertFalse(s["passed"])

    def test_graph_used_without_a_quote_fails(self):
        s = self.score("graph_no_quote")
        self.assertTrue(s["first_is_graph"])
        self.assertIsNone(s["cited_quote"])
        self.assertFalse(s["passed"])

    def test_no_tools_fails(self):
        s = self.score("no_tools")
        self.assertEqual((s["first_knowledge_tool"], s["graph_used"], s["passed"]), (None, False, False))

    def test_a_quote_only_another_tool_returned_does_not_count(self):
        events = fixture("graph_first_cited")
        for e in events:                                     # the graph's results lose the quote
            if e["type"] == "user":
                e["message"]["content"][0]["content"] = [{"type": "text", "text": "{}"}]
        self.assertIsNone(G.score_session(events, self.rules)["cited_quote"])

    def test_vault_skill_and_note_reads_are_lookups_and_toolsearch_is_not(self):
        r = self.rules
        self.assertEqual(G.lookup_kind(G.ToolCall("1", "Skill", {"skill": "vault"}), r), G.OTHER)
        self.assertEqual(G.lookup_kind(G.ToolCall("1", "Skill", {"skill": "plugin:vault"}), r), G.OTHER)
        self.assertIsNone(G.lookup_kind(G.ToolCall("1", "Skill", {"skill": "clarify"}), r))
        self.assertEqual(G.lookup_kind(G.ToolCall("1", "Read", {"file_path": r"C:\github\cohoodOBS\index.md"}), r), G.OTHER)
        transcripts = str(Path(self.cfg["paths"]["transcripts"]) / "2026-10-01.md")
        self.assertEqual(G.lookup_kind(G.ToolCall("1", "Grep", {"path": transcripts, "pattern": "x"}), r), G.OTHER)
        self.assertIsNone(G.lookup_kind(G.ToolCall("1", "Read", {"file_path": r"C:\code\app.py"}), r))
        self.assertIsNone(G.lookup_kind(G.ToolCall("1", "ToolSearch", {"query": "whispr"}), r))
        self.assertEqual(G.lookup_kind(G.ToolCall("1", "mcp__whispr-kg__get", {}), r), G.GRAPH)

    def test_quote_rules(self):
        hay = [json.dumps({"quote": "the export keeps the 4-4-5 retail calendar format, not Gregorian"})]
        n = self.rules.quote_min_words
        self.assertIsNotNone(G.cited_quote('They said "the export keeps the 4-4-5 retail calendar".', hay, n))
        self.assertIsNotNone(G.cited_quote('"the export keeps the 4-4-5 ... calendar format, not Gregorian"', hay, n))
        self.assertIsNone(G.cited_quote('"not Gregorian"', hay, n))               # too short to prove anything
        self.assertIsNone(G.cited_quote('"the export keeps the weekly calendar"', hay, n))
        self.assertIsNone(G.cited_quote("the export keeps the 4-4-5 retail calendar", hay, n))   # not quoted

    def test_summary_against_the_bar(self):
        rows = [{"arm": "after", "passed": i < 9, "first_is_graph": True, "graph_used": True,
                 "cited_quote": "q" if i < 9 else None} for i in range(10)]
        rows += [{"arm": "before", "passed": False, "first_is_graph": False, "graph_used": False, "cited_quote": None}
                 for _ in range(10)]
        s = G.summarize(rows, ["before", "after"], "after", 9, 10)
        self.assertEqual((s["after"]["passed"], s["after"]["meets_bar"], s["after"]["scored"]), (9, True, True))
        self.assertEqual((s["before"]["passed"], s["before"]["meets_bar"], s["before"]["scored"]), (0, False, False))
        rows[0]["passed"] = False
        self.assertFalse(G.summarize(rows, ["before", "after"], "after", 9, 10)["after"]["meets_bar"])


class TestPlan(_Case):
    def test_questions_are_the_agreed_ten(self):
        qs = G.questions(self.cfg)
        self.assertEqual(len(qs), 10)
        self.assertEqual(qs[0].text, "Who's in the audience for the Purina agentic pilot demo?")
        self.assertEqual(qs[7].text, "How does Kroger's use of AI Advantage differ from Nestlé's?")
        self.assertLessEqual(self.g["pass_min"], len(qs))

    def test_session_args_are_read_only_and_from_config(self):
        args = G.cli_base(self.cfg)
        self.assertEqual(args[:len(self.g["cli_args"])], self.g["cli_args"])
        self.assertEqual(args[args.index("--tools") + 1], ",".join(self.g["builtin_tools"]))
        allowed = args[args.index("--allowedTools") + 1].split(",")
        for forbidden in ("Write", "Edit", "Bash", "WebFetch", "WebSearch"):
            self.assertNotIn(forbidden, allowed)
            self.assertIn(forbidden, args[args.index("--disallowedTools") + 1].split(","))
        self.assertNotIn("--setting-sources", args)                 # a live session: settings load

    def test_arms_set_the_switch_and_the_plugin(self):
        note = self.cfg["graph_first_note"]
        on, off = G.session_env(self.cfg, True), G.session_env(self.cfg, False)
        self.assertEqual((on[note["switch_env"]], off[note["switch_env"]]), (note["on_value"], note["off_value"]))
        self.assertEqual(on[self.g["plugin_dirs_env"]], str(G.plugin_dir(self.cfg)))
        self.assertEqual(G.working_dir(self.cfg), Path.home())

    def test_estimate_uses_the_ledger_else_the_seed(self):
        per, basis = G.estimate(self.cfg, [])
        self.assertEqual(per, self.g["cost_seed_usd"])
        self.assertIn("config seed", basis)
        role, model = self.g["role"], self.cfg["models"][self.g["role"]]["model"]
        rows = [{"role": role, "model": model, "cost_usd": 0.2}, {"role": role, "model": model, "cost_usd": 0.4},
                {"role": role, "model": model, "cost_usd": 1.0, "cost_is_upper_bound": True},
                {"role": "judge", "model": model, "cost_usd": 9.0}]
        per, basis = G.estimate(self.cfg, rows)
        self.assertAlmostEqual(per, 0.3)
        self.assertIn("ledger: mean of 2", basis)

    def test_the_hook_check_refuses_a_wrong_arm(self):
        qs, note = G.questions(self.cfg), self.cfg["graph_first_note"]["text"].strip()
        self.assertEqual(G.refusals(self.cfg, qs, {"before": "alerts", "after": "alerts\n\n" + note}), [])
        bad = G.refusals(self.cfg, qs, {"before": note, "after": "", })
        self.assertEqual(len(bad), 2)
        self.assertTrue(any("did not run" in r for r in G.refusals(self.cfg, qs, {"before": "", "after": None})))

    def test_the_real_hook_gives_the_note_only_in_the_after_arm(self):
        appdata = self.root / "appdata"                  # the hook reads a temp overlay, never the real one
        (appdata / "whispr").mkdir(parents=True)
        (appdata / "whispr" / "config.yaml").write_text(yaml.safe_dump(overlay(self.root)), encoding="utf-8")
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
            contexts = {arm: G.hook_context(self.cfg, on) for arm, on in self.g["arms"].items()}
        self.assertEqual(G.refusals(self.cfg, G.questions(self.cfg), contexts), [])


class _Stub:
    """Stands in for models.call: streams a fixture through on_event and books a row."""

    def __init__(self, by_arm: dict[str, str], cost: float = 0.25, fail: bool = False):
        self.by_arm, self.cost, self.fail, self.seen = by_arm, cost, fail, []

    def __call__(self, cfg, role, prompt, *, max_budget_usd, ledger, request_key, cli_base, cwd, env, on_event):
        note = cfg["graph_first_note"]
        arm = "after" if env[note["switch_env"]] == note["on_value"] else "before"
        self.seen.append({"arm": arm, "prompt": prompt, "role": role, "cwd": cwd, "env": env, "cap": max_budget_usd})
        for event in fixture(self.by_arm[arm]):
            on_event(event)
        models.append_jsonl(ledger, {"role": role, "model": cfg["models"][role]["model"],
                                     "request_key": request_key, "cost_usd": self.cost})
        if self.fail:
            raise models.ModelCallError("graph_first_session call failed (exit 1)")
        return mock.Mock(cost_usd=self.cost)


class TestRun(_Case):
    def setUp(self):
        super().setUp()
        note = self.cfg["graph_first_note"]["text"].strip()
        self.contexts = {"before": "", "after": note}
        env = scrubbed_env(self.cfg)
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    def run_it(self, stub, **kw) -> tuple[int, str]:
        buf = io.StringIO()
        code = G.run(self.cfg, out=lambda line: buf.write(line + "\n"), call=stub, contexts=self.contexts, **kw)
        return code, buf.getvalue()

    def only_run(self) -> Path:
        run_dir, = (G.gf_dir(self.cfg) / "results").iterdir()
        return run_dir

    def test_dry_run_makes_no_call(self):
        stub = _Stub({})
        code, text = self.run_it(stub, dry_run=True)
        self.assertEqual((code, stub.seen), (G.EXIT_PASS, []))
        self.assertIn("20 sessions", text)
        self.assertIn("config seed", text)
        self.assertIn("zero model calls", text)

    def test_both_arms_run_and_are_scored(self):
        stub = _Stub({"before": "vault_first", "after": "graph_first_cited"})
        code, text = self.run_it(stub)
        self.assertEqual(code, G.EXIT_PASS, text)
        self.assertEqual([s["arm"] for s in stub.seen], ["before"] * 10 + ["after"] * 10)
        self.assertEqual(stub.seen[0]["prompt"], G.questions(self.cfg)[0].text)
        self.assertEqual({s["cwd"] for s in stub.seen}, {Path.home()})
        self.assertEqual({s["cap"] for s in stub.seen}, {self.g["max_budget_per_session_usd"]})
        run_dir = self.only_run()
        rows = models.read_jsonl(run_dir / G.SCORED_FILE)
        self.assertEqual(len(rows), 20)
        self.assertEqual(sum(r["passed"] for r in rows if r["arm"] == "after"), 10)
        self.assertEqual(sum(r["passed"] for r in rows if r["arm"] == "before"), 0)
        self.assertEqual(len(list((run_dir / G.RAW_DIR).iterdir())), 20)
        summary = json.loads((run_dir / G.SUMMARY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(summary["verdict"], "PASS")
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.25)
        # The next dry run estimates from these ledger rows.
        code, text = self.run_it(_Stub({}), dry_run=True)
        self.assertIn("ledger: mean of 20", text)

    def test_the_scored_arm_below_the_bar_fails(self):
        code, text = self.run_it(_Stub({"before": "graph_first_cited", "after": "graph_no_quote"}))
        self.assertEqual(code, G.EXIT_FAIL, text)
        self.assertIn("verdict: FAIL", text)

    def test_a_failed_session_is_still_scored(self):
        code, _ = self.run_it(_Stub({"before": "no_tools", "after": "graph_first_cited"}, fail=True))
        self.assertEqual(code, G.EXIT_PASS)
        rows = models.read_jsonl(self.only_run() / G.SCORED_FILE)
        self.assertTrue(all(r["error"] for r in rows))

    def test_the_cap_stops_the_run(self):
        self.g["cap_usd"] = 3.0          # a $2.50 session cap: three sessions at $0.25 fit, a fourth could pass $3
        self.g["max_budget_per_session_usd"] = 2.5
        code, text = self.run_it(_Stub({"before": "no_tools", "after": "graph_first_cited"}, cost=0.25))
        self.assertEqual(code, G.EXIT_REFUSED, text)
        self.assertIn("STOPPED", text)
        self.assertEqual(len(models.read_jsonl(self.only_run() / G.SCORED_FILE)), 3)

    def test_refuses_inside_a_claude_session_and_on_a_wrong_hook(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            code, text = self.run_it(_Stub({}))
        self.assertEqual(code, G.EXIT_REFUSED)
        self.assertIn("CLAUDECODE", text)
        self.contexts = {"before": "", "after": ""}
        code, text = self.run_it(_Stub({}))
        self.assertEqual(code, G.EXIT_REFUSED)
        self.assertIn("lacks the graph-first note", text)

    def test_rescore_rewrites_the_scores_from_the_raw_transcripts(self):
        self.run_it(_Stub({"before": "vault_first", "after": "graph_first_cited"}))
        run_dir = self.only_run()
        self.g["knowledge"]["other_tools"] = []            # vault no longer counts: before arm now passes
        buf = io.StringIO()
        self.assertEqual(G.rescore(self.cfg, run_dir.name, out=lambda line: buf.write(line + "\n")), G.EXIT_PASS)
        rows = models.read_jsonl(run_dir / G.SCORED_FILE)
        self.assertEqual(sum(r["passed"] for r in rows if r["arm"] == "before"), 10)

    def test_cli_dry_run(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=self.cfg), \
                mock.patch.object(G, "hook_context", side_effect=lambda cfg, on: self.contexts["after" if on else "before"]), \
                contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["graph-first", "--dry-run"]), 0)
        self.assertIn("dry run: zero model calls made", buf.getvalue())


class TestThroughModels(_Case):
    """The harness's session options through the real pipeline/models.py and the fake CLI."""

    def setUp(self):
        super().setUp()
        ov = overlay(self.root)
        ov["cli"] = fake_cli()
        self.cfg = load_config(overlay=ov)
        self.g = G.settings(self.cfg)
        self.g["cli_args"] = [FAKE_CLAUDE, *self.g["cli_args"]]
        self.args_out = self.root / "args.json"
        env = scrubbed_env(self.cfg, FAKE_CLAUDE_ARGS_OUT=str(self.args_out), CLAUDE_CODE_PLUGIN_DIRS="inherited")
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    def test_a_session_gets_its_args_cwd_env_and_events(self):
        events: list[dict] = []
        work = self.root / "home"
        work.mkdir()
        models.call(self.cfg, self.g["role"], "Q?", max_budget_usd=1.0, ledger=self.root / "l.jsonl",
                    request_key="k", cli_base=G.cli_base(self.cfg), cwd=work, env=G.session_env(self.cfg, True),
                    on_event=events.append)
        seen = json.loads(self.args_out.read_text(encoding="utf-8"))
        self.assertEqual(seen["argv"][:len(self.g["cli_args"]) - 1], self.g["cli_args"][1:])
        self.assertIn("--allowedTools", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(seen["argv"][seen["argv"].index("--effort") + 1], "medium")
        self.assertEqual(Path(seen["cwd"]).resolve(), work.resolve())
        note = self.cfg["graph_first_note"]
        self.assertEqual(seen["env_whispr"][note["switch_env"]], note["on_value"])
        self.assertEqual(seen["env_whispr"][self.g["plugin_dirs_env"]], str(G.plugin_dir(self.cfg)))
        self.assertEqual([e["type"] for e in events], ["system", "result"])
        self.assertEqual(models.read_jsonl(self.root / "l.jsonl")[0]["request_key"], "k")

    def test_an_on_event_failure_fails_the_call_after_its_ledger_row(self):
        def boom(event):
            raise OSError("disk full")
        with self.assertRaisesRegex(models.ModelCallError, "on_event failed"):
            models.call(self.cfg, self.g["role"], "Q?", max_budget_usd=1.0, ledger=self.root / "l.jsonl",
                        cli_base=G.cli_base(self.cfg), on_event=boom)
        self.assertEqual(len(models.read_jsonl(self.root / "l.jsonl")), 1)


if __name__ == "__main__":
    unittest.main()
