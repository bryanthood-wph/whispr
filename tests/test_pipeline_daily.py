"""pipeline/daily.py and pipeline/reconcile.py: the daily job (D.6, C.5). A fake
resolve call stands in for the model: no model call, no CLI, temp dirs only."""

from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pipeline.__main__ as cli
from kg import integrity
from kg.resolve import SAME
from kg.state import KIND_RUN_FAILED
from pipeline import backup as B
from pipeline import calls, daily, models, reconcile
from pipeline import run as runner
from pipeline_helpers import fake_cli
from test_pipeline_run import PROCESS_SINCE, T0, PipelineCase

NOW = T0 + timedelta(hours=6)       # the daily run: after the day's calls have settled


class FakeResolve:
    """The resolve call: runs before_call as a cache miss does, then answers."""

    def __init__(self, decision: str = SAME, cost: float = 0.01, error: Exception = None, errors=None):
        self.decision, self.cost, self.error, self.errors = decision, cost, error, list(errors or [])
        self.calls: list[str] = []

    def __call__(self, role, prompt, schema, before_call):
        before_call(f"key-{len(self.calls)}", self.per_call)
        self.calls.append(role)
        error = self.errors.pop(0) if self.errors else self.error
        if error:
            raise error
        return calls.Cached(f"key-{len(self.calls)}", {"decision": self.decision, "reason": "fake"}, False, "fake",
                            "claude.ai", self.cost)


def log_stamp(at) -> str:
    """A recorder log stamp (its local time)."""
    return at.astimezone().strftime("%Y-%m-%d %H:%M:%S") + ",123"


class DailyCase(PipelineCase):
    def setUp(self):
        super().setUp()
        self.cfg["pipeline"]["run"]["process_since"] = PROCESS_SINCE
        self.cfg["backup"]["destination"] = str(self.root / "backups")
        self.path = self.transcript("2026-10-05T10:00:00-04:00")
        self.recorder_log([("queued", T0), ("written", T0 + timedelta(minutes=5))])
        with self.db() as (store, _):
            store.upsert_episode(self.path.stem, transcript_path=str(self.path), sha256="x")
            self.shah = store.upsert_person("priya.shah@example.test", "Priya Shah", source="outlook")
            self.shaw = store.mention_person("Priya Shaw", source="extract", episode_id=self.path.stem)
        self.fake = FakeResolve()
        self.fake.per_call = self.cfg["maintain"]["max_budget_per_call_usd"]
        self.out: list[str] = []

    def recorder_log(self, events) -> None:
        lines = []
        for kind, at in events:
            if kind == "queued":
                lines.append(f"{log_stamp(at)} INFO whispr.__main__: queuing session for transcription (Weekly Sync)")
            else:
                lines.append(f"{log_stamp(at)} INFO whispr.output: Wrote transcript: {self.path}")
        Path(self.cfg["paths"]["recorder_log"]).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def daily(self, **kw) -> int:
        kw.setdefault("resolve_call", self.fake)
        kw.setdefault("now", NOW)
        return daily.daily(self.cfg, out=self.out.append, **kw)

    def log(self) -> list[dict]:
        return [r for r in models.read_jsonl(runner.files(self.cfg, "log")) if r.get("job") == daily.JOB]

    def steps(self) -> dict[str, dict]:
        with self.db() as (_, state):
            rows = state.conn.execute("SELECT check_name, details FROM maintenance_log WHERE check_name LIKE ?",
                                      (daily.JOB + ":%",)).fetchall()
        return {row[0].split(":", 1)[1]: json.loads(row[1]) for row in rows}

    def alerts(self) -> dict[str, dict]:
        with self.db() as (_, state):
            return {a["dedupe_key"]: a for a in state.open_alerts()}

    def runs(self) -> list[dict]:
        with self.db() as (_, state):
            return [dict(r) for r in state.conn.execute("SELECT * FROM run WHERE job = ? ORDER BY seq", (daily.JOB,))]


class TestDaily(DailyCase):
    def test_steps_run_in_order_and_each_is_recorded(self):
        self.assertEqual(self.daily(), runner.EXIT_OK, self.out)
        self.assertEqual([r["step"] for r in self.log() if r.get("event") == "step"],
                         [daily.BACKUP, daily.MAINTENANCE, daily.RECONCILIATION])
        steps = self.steps()
        self.assertEqual({k: v["outcome"] for k, v in steps.items()},
                         {daily.BACKUP: runner.OK, daily.MAINTENANCE: runner.OK, daily.RECONCILIATION: runner.OK})
        self.assertTrue(Path(steps[daily.BACKUP]["path"]).is_dir())
        self.assertEqual(steps[daily.MAINTENANCE]["resolve"]["merged"], 1)
        self.assertTrue(steps[daily.MAINTENANCE]["integrity"]["ok"])
        self.assertEqual(steps[daily.RECONCILIATION]["recorder"]["queued"], 1)
        self.assertEqual(self.fake.calls, [self.cfg["kg"]["models"]["resolve"]])
        with self.db() as (store, _):
            self.assertEqual(store.entity(self.shaw)["merged_into"], self.shah)
        run, = self.runs()
        self.assertEqual((run["status"], run["processed"], run["eligible"]), ("succeeded", 3, 3))
        self.assertEqual(self.alerts(), {})

    def test_a_failing_step_skips_nothing_and_fails_the_run(self):
        with mock.patch.object(B, "backup", side_effect=B.BackupError("disk full")):
            self.assertEqual(self.daily(), runner.EXIT_FAILED)
        steps = self.steps()
        self.assertEqual(steps[daily.BACKUP]["outcome"], runner.FAILED)
        self.assertIn("disk full", steps[daily.BACKUP]["error"])
        self.assertEqual((steps[daily.MAINTENANCE]["outcome"], steps[daily.RECONCILIATION]["outcome"]),
                         (runner.OK, runner.OK))
        run, = self.runs()
        self.assertEqual(run["status"], "failed")
        self.assertIn("disk full", run["error"])
        self.assertIn(f"{KIND_RUN_FAILED}:{daily.JOB}", self.alerts())

    def test_unset_backup_destination_refuses_the_step_with_a_setup_alert(self):
        self.cfg["backup"]["destination"] = None
        self.assertEqual(self.daily(), runner.EXIT_FAILED)
        self.assertEqual(self.steps()[daily.BACKUP]["outcome"], runner.REFUSED)
        self.assertEqual(self.steps()[daily.MAINTENANCE]["outcome"], runner.OK)
        alert = self.alerts()[f"{runner.KIND_SETUP}:backup"]
        self.assertIn("backup.destination", alert["fix"])

    def test_setup_and_reconcile_alerts_close_once_their_cause_is_fixed(self):
        dest = self.cfg["backup"]["destination"]
        self.cfg["backup"]["destination"] = None
        self.cfg["paths"]["recorder_log"], log = None, self.cfg["paths"]["recorder_log"]
        self.daily()
        self.assertEqual(set(self.alerts()) & {f"{runner.KIND_SETUP}:backup", f"{reconcile.KIND}:{daily.JOB}"},
                         {f"{runner.KIND_SETUP}:backup", f"{reconcile.KIND}:{daily.JOB}"})
        self.cfg["backup"]["destination"], self.cfg["paths"]["recorder_log"] = dest, log     # fixed
        self.assertEqual(daily.daily(self.cfg, now=NOW + timedelta(days=1), out=self.out.append,
                                     resolve_call=self.fake), runner.EXIT_OK, self.out)
        self.assertNotIn(f"{runner.KIND_SETUP}:backup", self.alerts())
        self.assertNotIn(f"{reconcile.KIND}:{daily.JOB}", self.alerts())

    def test_refused_in_a_nested_claude_session(self):
        var = self.cfg["auth"]["refuse_if_set"][0]
        with mock.patch.dict(os.environ, {var: "1"}):
            self.assertEqual(self.daily(), runner.EXIT_REFUSED)
        self.assertEqual((self.fake.calls, self.runs(), self.steps()), ([], [], {}))
        alert = self.alerts()[f"{runner.KIND_REFUSED}:{daily.JOB}"]
        self.assertIn("whispr-daily", alert["fix"])
        self.assertFalse((self.root / "backups").exists())

    def test_spend_cap_stops_resolution_partial_and_the_rest_still_runs(self):
        self.cfg["maintain"]["cap_usd"] = self.fake.per_call / 2
        self.assertEqual(self.daily(), runner.EXIT_PARTIAL)
        steps = self.steps()
        self.assertEqual((steps[daily.MAINTENANCE]["outcome"], steps[daily.MAINTENANCE]["stopped"]),
                         (runner.PARTIAL, runner.RUN_CAP))
        self.assertTrue(steps[daily.MAINTENANCE]["integrity"]["ok"])            # the checks still ran
        self.assertEqual(steps[daily.RECONCILIATION]["outcome"], runner.OK)
        self.assertEqual(self.fake.calls, [])
        with self.db() as (store, _):
            self.assertIsNone(store.entity(self.shaw)["merged_into"])
        self.assertEqual(self.runs()[0]["status"], "succeeded")

    def test_the_shared_daily_cap_stops_it_too(self):
        # Another job (the 15-minute run) books spend AFTER this run and its resolution
        # started, just before the call: the cap is checked against the ledger as it is
        # at each call, not as it was at the start.
        ledger, cap = runner.files(self.cfg, "ledger"), self.cfg["pipeline"]["run"]["daily_cap_usd"]

        def other_job_spends_first(role, prompt, schema, before_call):
            models.append_jsonl(ledger, {"ts": NOW.isoformat(), "cost_usd": cap, "role": "extract"})
            return self.fake(role, prompt, schema, before_call)
        self.assertEqual(self.daily(resolve_call=other_job_spends_first), runner.EXIT_PARTIAL)
        self.assertEqual(self.steps()[daily.MAINTENANCE]["stopped"], runner.DAILY_CAP)
        self.assertEqual(self.fake.calls, [])

    def test_a_call_in_flight_is_reserved_where_the_other_job_sees_it(self):
        seen = []
        real = self.fake.__call__

        def look(role, prompt, schema, before_call):
            def guarded(key, per_call):
                before_call(key, per_call)
                seen.append(runner.spent_since(self.cfg, NOW - timedelta(days=1)))   # what a pipeline run reads now
            return real(role, prompt, schema, guarded)
        self.assertEqual(self.daily(resolve_call=look), runner.EXIT_OK)
        self.assertEqual(seen, [self.fake.per_call])
        rows = models.read_jsonl(runner.files(self.cfg, "ledger"))
        self.assertEqual([(r["event"], r["job"], r["reserve_usd"]) for r in rows],
                         [(runner.RESERVE, daily.JOB, self.fake.per_call)])

    def test_the_time_budget_counts_from_the_run_start(self):
        clock = [0.0]
        real_backup = daily.pipeline_backup.backup

        def slow_backup(cfg, conn, *, now):
            clock[0] += self.cfg["maintain"]["time_budget_s"]        # a backup that took the whole budget
            return real_backup(cfg, conn, now=now)
        with mock.patch.object(daily.pipeline_backup, "backup", side_effect=slow_backup):
            self.assertEqual(self.daily(clock=lambda: clock[0]), runner.EXIT_PARTIAL)
        self.assertEqual(self.steps()[daily.MAINTENANCE]["stopped"], runner.TIME_BUDGET)
        self.assertEqual(self.fake.calls, [])

    def test_time_budget(self):
        self.cfg["maintain"]["time_budget_s"] = 0
        self.assertEqual(self.daily(), runner.EXIT_PARTIAL)
        self.assertEqual(self.steps()[daily.MAINTENANCE]["stopped"], runner.TIME_BUDGET)

    def test_a_model_failure_raises_its_alert_and_fails_the_run(self):
        self.fake.error = models.AuthError("signed out")
        self.assertEqual(self.daily(), runner.EXIT_FAILED)
        steps = self.steps()
        self.assertEqual(steps[daily.MAINTENANCE]["outcome"], runner.FAILED)
        self.assertEqual(steps[daily.MAINTENANCE]["spent_usd"], self.fake.per_call)   # a failed call counts its cap
        self.assertEqual(steps[daily.RECONCILIATION]["outcome"], runner.OK)
        self.assertIn(f"{runner.KIND_AUTH}:{daily.JOB}", self.alerts())

    def add_second_pair(self) -> None:
        with self.db() as (store, _):
            store.upsert_person("jamie.doe@example.test", "Jamie Doe", source="outlook")
            store.mention_person("Jamie Dae", source="extract", episode_id=self.path.stem)

    def test_a_poison_pair_is_recorded_and_not_asked_again(self):
        bad = FakeResolve(error=calls.OutputError("schema fail"))
        bad.per_call = self.fake.per_call
        self.assertEqual(self.daily(resolve_call=bad), runner.EXIT_OK, self.out)
        self.assertEqual(self.steps()[daily.MAINTENANCE]["outcome"], runner.OK)
        self.assertEqual(self.steps()[daily.MAINTENANCE]["resolve"]["failed"], 1)
        self.out.clear()
        code = daily.daily(self.cfg, now=NOW + timedelta(days=1), out=self.out.append, resolve_call=bad)
        self.assertEqual(code, runner.EXIT_OK, self.out)
        self.assertEqual(len(bad.calls), 1)                       # recorded: not asked, paid and failed again
        with self.db() as (_, state):
            row = state.conn.execute("SELECT decision FROM er_decision").fetchone()
        self.assertEqual(row[0], "failed")

    def test_a_call_failure_after_an_answer_is_the_pairs_own(self):
        self.add_second_pair()
        mixed = FakeResolve(errors=[None, models.ModelCallError("--max-budget-usd exceeded")])
        mixed.per_call = self.fake.per_call
        self.assertEqual(self.daily(resolve_call=mixed), runner.EXIT_OK, self.out)
        resolve = self.steps()[daily.MAINTENANCE]["resolve"]
        self.assertEqual((resolve["merged"], resolve["failed"]), (1, 1))
        self.assertEqual(self.alerts(), {})

    def test_a_call_failure_before_any_answer_is_the_machines(self):
        self.fake.error = models.ModelCallError("CLI 'claude' not found on PATH")
        self.assertEqual(self.daily(), runner.EXIT_FAILED)
        self.assertIn(f"{runner.KIND_UNAVAILABLE}:{daily.JOB}", self.alerts())
        with self.db() as (_, state):
            self.assertIsNone(state.conn.execute("SELECT decision FROM er_decision").fetchone())   # retried next run
        self.fake.error = None                                    # back online: the next run closes both alerts
        self.assertEqual(self.daily(now=NOW + timedelta(days=1)), runner.EXIT_OK, self.out)
        self.assertEqual(self.alerts(), {})

    def test_a_second_run_holding_the_lock_exits_ok(self):
        with runner.single_instance(runner.files(self.cfg, "daily_lock")) as held:
            self.assertTrue(held)
            self.assertEqual(self.daily(), runner.EXIT_OK)
        self.assertEqual(self.runs(), [])

    def test_dry_run_writes_nothing(self):
        before = self.counts()
        self.assertEqual(self.daily(dry_run=True), runner.EXIT_OK)
        text = "\n".join(self.out)
        for word in ("backup: would copy", "1 entity-resolution candidate", "integrity ok", "reconciliation:"):
            self.assertIn(word, text)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.root / "backups").exists())
        self.assertEqual((self.counts(), self.runs(), self.alerts()), (before, [], {}))
        self.assertFalse(runner.files(self.cfg, "log").exists())

    def test_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(cli, "load_config", return_value=self.cfg):
            self.assertEqual(cli.main(["daily", "--dry-run"]), runner.EXIT_OK)
        self.assertIn("dry run: no model call", buf.getvalue())
        self.assertIn("daily", cli.build_parser()[1])


class TestProductionResolvePath(DailyCase):
    """The production binding, end to end: default_resolve_call -> the budget's
    before_call -> calls.cached_call -> models.call, with only the CLI subprocess faked
    (fake_claude.py). The other tests pass a fake resolve_call and skip all of this."""
    overlay_extra = {"cli": fake_cli()}

    def setUp(self):
        super().setUp()
        self.args_out = self.root / "args.json"
        env = mock.patch.dict(os.environ, {"FAKE_CLAUDE_ARGS_OUT": str(self.args_out),
                                           "FAKE_CLAUDE_STRUCTURED": json.dumps({"decision": SAME, "reason": "fake"})})
        env.start()
        self.addCleanup(env.stop)

    def ledger(self) -> list[dict]:
        return list(models.read_jsonl(runner.files(self.cfg, "ledger")))

    def test_a_real_call_is_reserved_and_settled(self):
        self.assertEqual(self.daily(resolve_call=None), runner.EXIT_OK, self.out)
        with self.db() as (store, _):
            self.assertEqual(store.entity(self.shaw)["merged_into"], self.shah)
        self.assertIn("--json-schema", json.loads(self.args_out.read_text(encoding="utf-8"))["argv"])
        reserve, call = self.ledger()
        self.assertEqual((reserve["event"], reserve["job"], reserve["reserve_usd"]),
                         (runner.RESERVE, daily.JOB, self.cfg["maintain"]["max_budget_per_call_usd"]))
        self.assertEqual(call["request_key"], reserve["request_key"])     # the call row settles it
        self.assertAlmostEqual(runner.spent_since(self.cfg, NOW - timedelta(days=1)), call["cost_usd"])


class TestReconcile(DailyCase):
    def report(self, calendar=None) -> dict:
        with self.db() as (store, state):
            return reconcile.reconcile(self.cfg, store, state, job=daily.JOB, now=NOW, calendar=calendar)

    def test_a_lost_call_and_an_unpicked_transcript_are_problems(self):
        self.recorder_log([("queued", T0), ("queued", T0 + timedelta(hours=1)), ("written", T0)])
        stray = self.transcript("2026-10-05T12:00:00-04:00")
        old = (NOW - timedelta(days=1)).timestamp()
        os.utime(stray, (old, old))
        rep = self.report()
        self.assertEqual(rep["recorder"]["lost"], 1)
        self.assertEqual(rep["pipeline"]["unpicked"], [stray.stem])
        alert = self.alerts()[f"{reconcile.KIND}:{daily.JOB}"]
        self.assertIn("1 call(s) lost", alert["message"])
        self.assertIn(stray.stem, alert["message"])

    def test_a_recent_written_call_does_not_mask_an_older_lost_one(self):
        # Call A was queued 10 h before the run and never written (lost). Call B, this
        # transcript's call, was queued and written in the last half hour (not settled).
        b_queued = reconcile.transcript_start(self.path) + timedelta(minutes=30)
        now = b_queued + timedelta(minutes=30)
        self.recorder_log([("queued", now - timedelta(hours=10)), ("queued", b_queued),
                           ("written", b_queued + timedelta(minutes=5))])
        with self.db() as (store, state):
            rep = reconcile.report(self.cfg, store, state, now=now)
        self.assertEqual((rep["recorder"]["queued"], rep["recorder"]["lost"]), (1, 1))
        self.assertTrue(any("1 call(s) lost" in p for p in rep["problems"]), rep["problems"])

    def test_each_transcript_is_matched_to_its_own_call(self):
        start = reconcile.transcript_start(self.path)
        events = [start + timedelta(minutes=30), start + timedelta(hours=2)]       # this call, then a later one
        self.assertEqual(reconcile.lost_calls(events, [(start + timedelta(hours=3), str(self.path))]), [events[1]])
        unnamed = str(self.path.with_name("not-a-dated-name.md"))                 # no start: the latest before it
        self.assertEqual(reconcile.lost_calls(events, [(start + timedelta(hours=3), unnamed)]), [events[0]])

    def test_calls_not_yet_settled_are_not_judged(self):
        self.recorder_log([("queued", NOW - timedelta(minutes=5))])
        self.assertEqual(self.report()["recorder"]["lost"], 0)

    def test_outlook_meetings_without_a_transcript_are_reported(self):
        start = reconcile.transcript_start(self.path)       # the file name's (machine-local) start
        meetings = [{"subject": "Recorded", "start": start, "end": start + timedelta(minutes=30)},
                    {"subject": "Missed", "start": start + timedelta(hours=2), "end": start + timedelta(hours=3)}]
        rep = self.report(calendar=lambda a, b: meetings)
        self.assertEqual(rep["outlook"]["unrecorded"], ["Missed"])
        self.assertEqual(rep["problems"], [])                    # information, not a problem

    def test_gone_transcripts_are_tombstoned_unless_too_many(self):
        self.path.unlink()
        self.cfg["reconcile"]["max_tombstones"] = 0
        rep = self.report()
        self.assertEqual((rep["episodes"]["blocked"], rep["episodes"]["tombstoned"]), (True, []))
        self.assertIn("none tombstoned", self.alerts()[f"{reconcile.KIND}:{daily.JOB}"]["message"])
        self.cfg["reconcile"]["max_tombstones"] = 5
        self.assertEqual(self.report()["episodes"]["tombstoned"], [self.path.stem])
        with self.db() as (store, _):
            self.assertIsNotNone(store.episode(self.path.stem)["deleted_at"])

    def test_an_unset_recorder_log_is_a_problem(self):
        self.cfg["paths"]["recorder_log"] = None
        self.assertIn("paths.recorder_log is not set", self.report()["problems"][0])

    def test_the_stub_outlook_reader_checks_nothing(self):
        self.assertIsNone(reconcile.outlook_reader(self.cfg))
        self.assertEqual(self.report()["outlook"], {"checked": False})


class TestIntegrity(DailyCase):
    def check(self) -> dict:
        with self.db() as (store, state):
            run_id = state.begin_run("integrity-test", NOW)
            return integrity.check(store, state, run_id, listed=self.cfg["maintain"]["listed"], now=NOW)

    def test_a_reference_to_a_merged_away_entity_is_repointed(self):
        with self.db() as (store, _):
            orphan = store.upsert_entity("project", "Lonely", source="extract")
            fact = store.add_fact(type_="fact", text="Runs the budget", quote="runs the budget",
                                  episode_id=self.path.stem, transcript_text="Priya Shaw runs the budget",
                                  subject_entity_id=self.shaw)
            store.conn.execute("UPDATE entity SET merged_into = ? WHERE id = ?", (self.shah, self.shaw))  # a raced write
        got = self.check()
        self.assertTrue(got["ok"])
        self.assertIn({"table": "fact", "column": "subject_entity_id", "id": fact, "old": self.shaw, "new": self.shah},
                      got["repaired"])
        self.assertIn(orphan, got["orphans"]["ids"])
        with self.db() as (store, _):
            subject = store.conn.execute("SELECT subject_entity_id FROM fact WHERE id = ?", (fact,)).fetchone()[0]
        self.assertEqual(subject, self.shah)
        self.assertEqual(self.check()["repaired"], [])           # repaired once, then nothing to do
