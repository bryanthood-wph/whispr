"""The eval harness skeleton (issue #11, B.3, B.9). Fake CLI and synthetic data only."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import yaml

from eval import __main__ as cli
from eval import frame as F
from eval import ledger as L
from eval import lowmic, preflight
from eval.records import DEVICE_OTHER, DEVICE_SPEAKER
from pipeline import calls, prompts
from pipeline.config import config_file, load_config
from pipeline_helpers import EXTRACT_SAMPLE, FILLER, fake_cli, overlay, scrubbed_env, transcript

TURNS = [("00:00:01", "Others", FILLER)] * 5
AFTER_CUTOFF = date(2026, 12, 31)
SPEAKERS, HEADSET = "Speakers (Realtek)", "Headset (USB)"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ov = overlay(self.root)
        self.ov["cli"] = fake_cli()
        self.cfg = load_config(overlay=self.ov)
        self.tdir = Path(self.ov["paths"]["transcripts"])
        self.log_lines: list[str] = []
        self.clock = datetime(2026, 9, 1, 9, 0)
        env = scrubbed_env(self.cfg, FAKE_CLAUDE_STRUCTURED=json.dumps(EXTRACT_SAMPLE))
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def add(self, *, minutes=10, source="window-title", device=SPEAKERS, turns=TURNS, mic_ratio=1.0,
            date=None) -> str:
        """Write one transcript and its recorder-log stop line; return its id."""
        self.clock += timedelta(hours=1)
        start = self.clock.replace(tzinfo=None)
        if date:
            start = datetime.fromisoformat(date + "T09:00:00")
        iso = start.isoformat() + "-04:00"
        stem = f"{start:%Y-%m-%d-%H%M}-t"
        turns = [(ts, who, f"{text} {stem}") for ts, who, text in turns]   # distinct requests per transcript
        (self.tdir / f"{stem}.md").write_text(
            transcript(iso, turns, metadata_source=source, duration_min=minutes, output_device=device),
            encoding="utf-8")
        end = start + timedelta(minutes=minutes)
        loop = 16000 * 60 * minutes
        self.log_lines.append(f"{end:%Y-%m-%d %H:%M:%S},000 INFO whispr.capture: recording stopped "
                              f"(mic frames={int(loop * mic_ratio)}, loopback frames={loop})")
        return stem

    def write_log(self):
        Path(self.ov["paths"]["recorder_log"]).write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")

    def populate(self, per_cell=4):
        """per_cell transcripts in every call/meeting cell plus 4 stubs; half on speakers."""
        spans = {"call_le15": ("window-title", 10), "call_15_30": ("window-title", 20),
                 "call_gt30": ("window-title", 40), "meeting_le15": ("outlook", 10),
                 "meeting_15_30": ("outlook", 20), "meeting_30_60": ("outlook", 45),
                 "meeting_gt60": ("outlook", 90)}
        for source, minutes in spans.values():
            for k in range(per_cell):
                self.add(minutes=minutes, source=source, device=SPEAKERS if k % 2 else HEADSET)
        for _ in range(4):
            self.add(turns=[])
        self.write_log()


class TestFrame(_Base):
    def test_cells_devices_and_cutoff(self):
        a = self.add(minutes=10, source="window-title")
        b = self.add(minutes=45, source="outlook", device=HEADSET)
        c = self.add(turns=[])
        self.add(date="2026-12-01")                       # after frame_cutoff: excluded
        self.write_log()
        items = {i.id: i for i in F.build_frame(self.cfg)}
        self.assertEqual(set(items), {a, b, c})
        self.assertEqual((items[a].cell, items[a].device), ("call_le15", DEVICE_SPEAKER))
        self.assertEqual((items[b].cell, items[b].device), ("meeting_30_60", DEVICE_OTHER))
        self.assertEqual(items[c].cell, "stub")

    def test_provisional_until_the_cutoff_day_ends(self):
        self.add()
        self.write_log()
        cutoff = date.fromisoformat(self.cfg["eval"]["sample"]["frame_cutoff"])
        self.assertEqual(F.load_frame(self.cfg, today=cutoff)[1], F.PROVISIONAL)
        self.assertFalse(F.frame_path(self.cfg).exists())

    def test_frozen_frame_detects_edits_and_keeps_low_mic(self):
        a = self.add(minutes=20, mic_ratio=0.1)
        self.write_log()
        items, state = F.load_frame(self.cfg, today=AFTER_CUTOFF)
        self.assertEqual(state, F.FROZEN_NOW)
        Path(self.ov["paths"]["recorder_log"]).write_text("", encoding="utf-8")   # log rotated away
        items, state = F.load_frame(self.cfg, today=AFTER_CUTOFF)
        self.assertEqual(state, F.VERIFIED)
        self.assertTrue(items[0].low_mic)                  # frozen with the frame, not re-derived
        path = self.tdir / f"{a}.md"
        path.write_text(path.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
        with self.assertRaises(F.FrameError):
            F.load_frame(self.cfg)


class TestLowMic(_Base):
    def test_ratio_duration_floor_and_unmatched(self):
        low = self.add(minutes=20, mic_ratio=0.1)
        short = self.add(minutes=2, mic_ratio=0.1)           # under min_duration_min
        ok = self.add(minutes=20, mic_ratio=0.9)
        self.write_log()
        orphan = self.add(minutes=20)                          # no log line written for it
        frame = F.build_frame(self.cfg)
        flags = {i.id: i.low_mic for i in frame}
        self.assertEqual({x for x, f in flags.items() if f}, {low})
        self.assertEqual([x for x, f in flags.items() if f is None], [orphan])
        self.assertIs(flags[short], False)
        self.assertIs(flags[ok], False)

    def test_rotated_logs_are_read(self):
        low = self.add(minutes=20, mic_ratio=0.1)
        self.write_log()
        log = Path(self.ov["paths"]["recorder_log"])
        log.replace(str(log) + ".1")                       # rotated: the live log starts empty
        log.write_text("", encoding="utf-8")
        self.assertTrue({i.id: i for i in F.build_frame(self.cfg)}[low].low_mic)

    def test_missing_log_fails_loudly(self):
        self.add()
        with self.assertRaises(lowmic.LowMicError):
            F.build_frame(self.cfg)


class TestDraw(_Base):
    def test_seeded_pilot_excluded_device_balanced_and_low_mic_replaced(self):
        self.populate(per_cell=12)
        frame = F.build_frame(self.cfg)
        low = set(sorted(i.id for i in frame if i.cell == "meeting_15_30")[:2])
        frame = [replace(i, low_mic=i.id in low) for i in frame]
        s1, s2 = F.draw(frame, self.cfg), F.draw(frame, self.cfg)
        self.assertEqual(s1, s2)
        self.assertEqual(len(s1.pilot), self.cfg["eval"]["sample"]["pilot_n"])
        drawn = {x for cell in [*s1.core.values(), *s1.task_only.values()] for x in cell}
        self.assertFalse(drawn & set(s1.pilot))
        self.assertFalse(drawn & low)
        by_id = {i.id: i for i in frame}
        first_two = s1.core["call_le15"][:2]
        self.assertEqual({by_id[x].device for x in first_two}, {DEVICE_SPEAKER, DEVICE_OTHER})
        # 12 in the cell < 11 + 17 wanted, so every low-mic transcript is reached and replaced
        self.assertEqual(set(s1.low_mic["meeting_15_30"]), low - set(s1.pilot))

    def test_shortfall_reported(self):
        self.populate(per_cell=4)
        sample = F.draw(F.build_frame(self.cfg), self.cfg)
        self.assertIn("meeting_15_30", sample.shortfall)    # wants 11 + 17

    def test_unscreened_transcripts_are_replaced_not_drawn(self):
        self.populate(per_cell=12)
        frame = F.build_frame(self.cfg)
        unknown = {i.id for i in frame if i.cell == "call_15_30"}   # whole cell: no log match
        frame = [replace(i, low_mic=None) if i.id in unknown else i for i in frame]
        sample = F.draw(frame, self.cfg)
        self.assertFalse(unknown & {*sample.pilot, *sample.core["call_15_30"], *sample.task_only["call_15_30"]})
        self.assertEqual(set(sample.unscreened["call_15_30"]), unknown)
        # Not flagged low mic, so in the primary population: counted in the primary frame size.
        self.assertEqual(F.design(sample, frame, include_task_only=False).frame_n["call_15_30"], len(unknown))

    def test_low_mic_units_never_dilute_primary_weights(self):
        self.populate(per_cell=12)
        frame = F.build_frame(self.cfg)
        low = set(sorted(i.id for i in frame if i.cell == "call_le15")[:2])
        frame = [replace(i, low_mic=i.id in low) for i in frame]
        sample = F.draw(frame, self.cfg)
        primary = F.design(sample, frame, include_task_only=False)
        self.assertFalse(any(u.low_mic for u in primary.units))
        unit = next(u for u in primary.units if u.cell == "call_le15")
        self.assertEqual(primary.weight(unit), (12 - len(low)) / len(sample.core["call_le15"]))
        self.assertEqual(primary.frame_n["meeting_15_30"], 12)          # no low-mic transcripts there
        separate = F.design(sample, frame, include_task_only=False, low_mic=True)
        self.assertTrue(separate.units and all(u.low_mic for u in separate.units))

    def test_low_mic_design_weights_by_low_mic_frame_count(self):
        self.populate(per_cell=12)
        frame = F.build_frame(self.cfg)
        low = {cell: set(sorted(i.id for i in frame if i.cell == cell)[:k])
               for cell, k in (("call_le15", 2), ("meeting_15_30", 3))}
        all_low = set().union(*low.values())
        frame = [replace(i, low_mic=i.id in all_low) for i in frame]
        sample = F.draw(frame, self.cfg)    # both cells want more than 12, so every low-mic one is reached
        separate = F.design(sample, frame, include_task_only=False, low_mic=True)
        self.assertEqual(separate.frame_n, {"call_le15": 2, "meeting_15_30": 3})
        for cell, ids in low.items():
            units = [u for u in separate.units if u.cell == cell]
            self.assertEqual({u.id for u in units}, ids)
            self.assertEqual(sum(separate.weight(u) for u in units), len(ids))
        primary = F.design(sample, frame, include_task_only=False)
        self.assertEqual(primary.frame_n["call_le15"], 12 - 2)
        self.assertEqual(primary.frame_n["meeting_15_30"], 12 - 3)
        partition = {c: primary.frame_n.get(c, 0) + separate.frame_n.get(c, 0) for c in primary.frame_n}
        self.assertEqual(partition, Counter(i.cell for i in frame))


class TestLedger(_Base):
    def _row(self, usd, key=None):
        with open(L.ledger_path(self.cfg), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"cost_usd": usd, "request_key": key}) + "\n")

    def test_stage_cap_accumulates_across_runs(self):
        used = self.cfg["eval"]["stages"]["pilot"]["cap_usd"] - 0.5
        L.guard(self.cfg, "pilot")("k1", 1.0)
        self._row(used, "k1")                              # settles k1's reservation
        self.assertEqual(L.tally(self.cfg), (used, {"pilot": used}))
        with self.assertRaisesRegex(L.BudgetStop, "cap"):  # a fresh guard (next run) still sees it
            L.guard(self.cfg, "pilot")("k2", 1.0)

    def test_overall_limit(self):
        self._row(94.5)
        with self.assertRaisesRegex(L.BudgetStop, "limit"):
            L.guard(self.cfg, "pilot")("k1", 1.0)

    def test_call_killed_in_flight_is_charged_at_its_cap(self):
        L.guard(self.cfg, "pilot")("k1", 1.5)              # reserved; the process died before its row
        self.assertEqual(L.spent(self.cfg), 1.5)
        self._row(0.2, "k1")
        self.assertEqual(L.spent(self.cfg), 0.2)

    def test_torn_line_is_skipped_and_counted(self):
        L.guard(self.cfg, "pilot")("k1", 1.5)
        with open(L.ledger_path(self.cfg), "a", encoding="utf-8") as fh:
            fh.write('{"cost_usd": 0.2, "request_')         # killed mid-append
        self.assertEqual(L.spent(self.cfg), 1.5)           # the reservation still counts
        self.assertEqual(L.malformed(self.cfg), 1)

    def test_run_records_and_failure_needs_resolution(self):
        with L.Run(self.cfg, "pilot") as run:
            pass
        self.assertEqual(L.runs(self.cfg)[run.run_id][-1]["event"], "completed")
        with self.assertRaises(ZeroDivisionError):
            with L.Run(self.cfg, "pilot") as bad:
                1 / 0
        self.assertIn(bad.run_id, L.unresolved(self.cfg))
        with self.assertRaises(L.RunError):               # can't start over an unresolved failure
            with L.Run(self.cfg, "pilot"):
                pass
        L.resolve(self.cfg, bad.run_id, "investigated")
        self.assertEqual(L.unresolved(self.cfg), {})

    def test_killed_run_is_marked_failed(self):
        L._append_run(self.cfg, {"run_id": "pilot-x", "event": "started"})   # process died here
        self.assertEqual(L.reconcile(self.cfg), ["pilot-x"])
        self.assertIn("pilot-x", L.unresolved(self.cfg))

    def test_lock_blocks_a_second_run(self):
        with L.Run(self.cfg, "pilot"):
            self.assertEqual(L.reconcile(self.cfg), [])   # a live run is never marked killed
            with self.assertRaises(L.RunError):
                with L.Run(self.cfg, "pilot"):
                    pass


class CliBase(_Base):
    """`python -m eval` over a populated, frozen frame (shared with test_eval_pilot)."""

    def setUp(self):
        super().setUp()
        self.ov["eval"] = {"sample": {"frame_cutoff": "2026-09-30"}}   # ended: the frame freezes
        self.populate(per_cell=12)
        self.ov_path = self.root / "overlay.yaml"
        self.ov_path.write_text(yaml.safe_dump(self.ov), encoding="utf-8")

    def main(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--overlay", str(self.ov_path), *argv])
        return code, buf.getvalue()

    def run_dir(self) -> Path:
        (run_dir,) = (L.eval_dir(self.cfg) / "results").iterdir()
        return run_dir

    def report(self) -> dict:
        return json.loads((self.run_dir() / "report.json").read_text(encoding="utf-8"))


class TestCli(CliBase):

    def test_dry_run_makes_zero_calls(self):
        with mock.patch("pipeline.models.call", side_effect=AssertionError("model called")) as call:
            code, out = self.main("run", "--stage", "pilot", "--dry-run")
        self.assertEqual(code, 0, out)
        call.assert_not_called()
        self.assertIn("6 jobs", out)                       # 3 pilot transcripts x 2 system prompts
        self.assertIn("zero model calls", out)
        self.assertFalse(L.ledger_path(self.cfg).exists())

    def calls_made(self):
        return [r for r in L.rows(self.cfg) if r.get("event") != L.RESERVE]

    def test_stage_tag_is_required(self):
        tags = {"status": "", "tag": "v0.1.0"}
        with mock.patch("eval.preflight.git", side_effect=lambda *a: tags[a[0]]):
            self.assertEqual(preflight.refusals(self.cfg, "pilot"), ["HEAD is not tagged eval-pilot (G.3)"])
            tags["tag"] = "v0.1.0 eval-pilot"
            self.assertEqual(preflight.refusals(self.cfg, "pilot"), [])

    def test_provenance_records_every_registered_hash(self):
        with mock.patch("eval.preflight.git", return_value="abc"):
            hashes = preflight.provenance(self.cfg)["config_sha256"]
        configured = {*self.cfg["prompts"].values(), *self.cfg["schemas"].values(), self.cfg["ontology"]}
        self.assertEqual(set(hashes), configured | {"planting.yaml"})
        registered = preflight.registered_hashes()
        self.assertEqual(hashes, {rel: registered[rel] for rel in hashes})

    def test_provenance_records_the_effective_config_hash(self):
        with mock.patch("eval.preflight.git", return_value="abc"):
            digest = preflight.provenance(self.cfg)["effective_config_sha256"]
            self.assertEqual(digest, prompts.sha256_text(calls.canonical(self.cfg)))
            self.ov["eval"]["seed"] = self.cfg["eval"]["seed"] + 1
            other = preflight.provenance(load_config(overlay=self.ov))["effective_config_sha256"]
        self.assertNotEqual(digest, other)

    def test_unregistered_prompt_is_refused_and_shown_by_the_dry_run(self):
        rel = self.cfg["prompts"]["extract"]
        edited = self.root / "extract-edited.md"
        edited.write_text(config_file(rel).read_text(encoding="utf-8") + "Also list every risk.\n",
                          encoding="utf-8")
        self.ov["prompts"] = {"extract": str(edited)}
        self.ov_path.write_text(yaml.safe_dump(self.ov), encoding="utf-8")
        tags = {"status": "", "tag": "eval-pilot"}
        with mock.patch("eval.preflight.git", side_effect=lambda *a: tags[a[0]]):
            reasons = preflight.refusals(load_config(overlay=self.ov), "pilot")
            self.assertEqual(len(reasons), 1)
            self.assertIn(str(edited), reasons[0])
            self.assertIn("no row", reasons[0])
            code, out = self.main("run", "--stage", "pilot", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn(f"would refuse a scored run: {str(edited)}", out)

    def test_hash_differing_from_its_registered_row_is_refused(self):
        rel = self.cfg["prompts"]["extract"]
        table = preflight.PREREG.read_text(encoding="utf-8")
        digest = preflight.registered_hashes()[rel]
        tampered = self.root / "PREREGISTRATION.md"
        tampered.write_text(table.replace(digest, "0" * 64), encoding="utf-8")
        tags = {"status": "", "tag": "eval-pilot"}
        with mock.patch("eval.preflight.git", side_effect=lambda *a: tags[a[0]]), \
             mock.patch("eval.preflight.PREREG", tampered):
            reasons = preflight.refusals(self.cfg, "pilot")
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith(f"{rel} differs from its registered hash"), reasons[0])

    def test_scored_run_refused_on_dirty_or_untagged(self):
        with mock.patch("eval.preflight.refusals", return_value=["HEAD has no tag"]):
            code, out = self.main("run", "--stage", "pilot")
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", out)

    def test_pilot_end_to_end_then_budget_stop_needs_resolution(self):
        # The fake CLI answers every pilot schema: one my-task per transcript, quoted from
        # TURNS, from both reference families; judges give the first allowed answer.
        quote = FILLER.split(" and ")[0]
        item = {"type": "task", "text": "Walk through the plan", "owner": "Me", "owner_basis": "volunteered",
                "mine": True, "due": None, "quote": quote, "importance": 2}
        by_property = {"ok": {"ok": True}, "items": {"items": [item]}, "pairs": {"pairs": []},
                       "verdict": {"verdict": "present"}, "answer": {"answer": None, "confidence": 0.9}}
        self.ov["eval"].update({"judge": {"samples": 1}, "stages": {"pilot": {"judge_subjects": 1}}})
        self.ov_path.write_text(yaml.safe_dump(self.ov), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_BY_PROPERTY": json.dumps(by_property)}), \
             mock.patch("eval.preflight.refusals", return_value=[]), \
             mock.patch("eval.preflight.provenance", return_value={"commit": "abc", "tags": ["eval-pilot"]}):
            code, out = self.main("run", "--stage", "pilot")
            self.assertEqual(code, 0, out)
            first = len(self.calls_made())
            run_dir, rep = self.run_dir(), self.report()
            self.assertTrue((run_dir / "report.md").is_file() and (run_dir / "pilot.jsonl").is_file())
            self.assertEqual({v["extract_calls"] for v in rep["h_s2"].values()}, {3})   # 3 transcripts each
            self.assertTrue(rep["probe"]["default"]["ok"] and rep["probe"]["no_settings"]["ok"])
            self.assertEqual(rep["errors"], 0)
            # Every call this run made is in the report's steps, the probe's two among them.
            steps = sum(c["calls"] for c in rep["cost_by_step"].values())
            self.assertEqual((steps, rep["cost_by_step"]["probe"]["calls"], rep["unattributed_cost"]["calls"]),
                             (first, 2, 0))
            self.assertEqual(set(rep["cost_by_step"]), {"extract", "reference", "judge", "calibration", "probe"})
            self.assertEqual(self.main("status")[0], 0)
            code, out = self.main("run", "--stage", "pilot")         # all cached: only the uncached probe runs
            self.assertEqual(code, 0, out)
            self.assertEqual(len(self.calls_made()), first + 2)
            self.assertIn("0 to call", self.main("run", "--stage", "pilot", "--dry-run")[1])

            self.ov["eval"]["stages"] = {"pilot": {"cap_usd": 0.01}}
            self.ov_path.write_text(yaml.safe_dump(self.ov), encoding="utf-8")
            for f in (L.eval_dir(self.cfg) / "cache").glob("*.json"):
                f.unlink()
            with self.assertRaises(L.BudgetStop):
                self.main("run", "--stage", "pilot")
        code, out = self.main("status")
        self.assertEqual(code, 1)
        self.assertIn("UNRESOLVED", out)


if __name__ == "__main__":
    unittest.main()
