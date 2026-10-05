"""eval/scoring.py (issue #17, B.6, B.7): builder tests beyond the contract. No model calls."""

from __future__ import annotations

import copy
import json
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from eval import scoring as S
from eval.records import DEVICE_OTHER, DEVICE_SPEAKER, Design, Outcome, Unit
from pipeline.config import config_file, load_config
from pipeline.jsonschema_lite import validate
from pipeline_helpers import overlay

M = "my_task_recall"
HIT = S.Calibration(0.9, 0.95, S.CODING_HIT)


def _cfg(tmp: Path) -> dict:
    return load_config(overlay=overlay(tmp))


def _design(cells: dict[str, tuple[int, int]], low_mic_every: int = 0) -> Design:
    """cells: name -> (frame_n, sample_n). Every `low_mic_every`-th unit is low-mic."""
    units = []
    for cell, (_, n) in cells.items():
        units += [Unit(f"{cell}-{i}", cell, DEVICE_SPEAKER if i % 2 else DEVICE_OTHER,
                       bool(low_mic_every) and i % low_mic_every == 0) for i in range(n)]
    return Design({c: fn for c, (fn, _) in cells.items()}, tuple(units))


def _outs(design: Design, config: str, p: float, seed: int, items: int = 3, metric: str = M) -> list[Outcome]:
    rng = random.Random(seed)
    return [Outcome(u.id, config, metric, f"{u.id}-{k}", float(rng.random() < p))
            for u in design.units for k in range(items)]


def _sample_scorecard(entry: dict, comparisons: dict, bar: dict) -> dict:
    table = {"B": {M: entry}}
    return {
        "metrics": table,
        "strata": {"cell": {"c1": table}, "device": {DEVICE_SPEAKER: table}, "low_mic": {"False": table}},
        "comparisons": comparisons,
        "my_task_bar": bar,
        "miss_phantom_ledger": [
            {"item_id": "t1-3", "unit_id": "t1", "config": "B", "kind": "miss", "root_cause": "asr", "quote": "send it"},
            {"item_id": "t2-1", "unit_id": "t2", "config": "B", "kind": "phantom", "root_cause": None, "quote": "ok"},
        ],
        "escalation_rate": {"presence": 0.12, "support": 0.05},
        "calibration": {"matcher": {"sensitivity": 0.95, "specificity": 0.97, "n_planted": 40, "reliable": True},
                        "owner_swap": {"sensitivity": 0.85, "specificity": None, "n_planted": 20, "reliable": False}},
        "simplicity": {"Scheduled tasks": 4, "Places holding pipeline code": "3", "Setup steps for a new user": None},
        "cost": {"total_usd": 61.2, "per_transcript_usd": {"A": 0.49, "B": 0.21}},
        "latency": {"per_transcript_s": {"A": 41.0, "B": 18.5}},
        "run": {"commit_sha": "5a40b12", "tag": "eval-run1"},
        "hashes": {"config": "ab12", "prompts": "cd34", "schemas": "ef56"},
    }


class TestZeroDenominator(unittest.TestCase):
    """Defined behaviour: no items -> NaN; NaN replicates kept but ignored by ci()."""

    def test_ratio_without_items_is_nan(self):
        d = _design({"c1": (10, 2)})
        self.assertTrue(math.isnan(S.ratio(d, [], "B", M)))

    def test_undefined_replicates_are_kept_and_ignored(self):
        d = _design({"c1": (10, 4)})
        outs = [Outcome("c1-0", "B", M, "i1", 1.0)]          # only one of four units has items
        reps = S.bootstrap(d, outs, "B", M, replicates=400, seed=2)
        self.assertEqual(len(reps), 400)
        nan_share = sum(math.isnan(r) for r in reps) / 400
        self.assertAlmostEqual(nan_share, (3 / 4) ** 3, delta=0.06)   # P(c1-0 missed by n-1 = 3 draws)
        self.assertEqual(S.ci(reps, 0.05), (1.0, 1.0))

    def test_ci_with_no_defined_replicate_raises(self):
        with self.assertRaises(ValueError):
            S.ci([math.nan, math.nan], 0.05)

    def test_outcomes_outside_design_are_ignored(self):
        d = _design({"c1": (10, 2)})
        outs = [Outcome("c1-0", "B", M, "i1", 1.0), Outcome("nope", "B", M, "i2", 0.0)]
        self.assertEqual(S.ratio(d, outs, "B", M), 1.0)


class TestBootstrapMechanics(unittest.TestCase):
    def test_resampling_keeps_cell_sizes(self):
        d = _design({"c1": (10, 3), "c2": (50, 5)})
        counts = S._resample_counts(d.units, 200, 1)
        self.assertTrue(np.allclose(counts[:, :3].sum(axis=1), 3))   # Rao-Wu: (n-1) draws * n/(n-1)
        self.assertTrue(np.allclose(counts[:, 3:].sum(axis=1), 5))

    def test_rao_wu_variance_matches_unbiased_s2_over_n(self):
        # Review item 4: one cell [0, 1, 1]; s^2 / n = (1/3) / 3 = 0.111 (naive bootstrap: 0.074)
        d = _design({"c1": (30, 3)})
        outs = [Outcome(f"c1-{i}", "B", M, f"i{i}", v) for i, v in enumerate([0.0, 1.0, 1.0])]
        reps = np.array(S.bootstrap(d, outs, "B", M, replicates=20000, seed=3))
        self.assertAlmostEqual(float(np.var(reps)), 1 / 9, delta=0.006)

    def test_single_unit_cell_stays_fixed(self):
        d = _design({"solo": (5, 1), "c2": (10, 3)})
        counts = S._resample_counts(d.units, 100, 1)
        self.assertTrue((counts[:, 0] == 1).all())

    def test_seed_changes_replicates(self):
        d = _design({"c1": (100, 20)})
        outs = _outs(d, "B", 0.7, 1)
        self.assertNotEqual(S.bootstrap(d, outs, "B", M, replicates=50, seed=1),
                            S.bootstrap(d, outs, "B", M, replicates=50, seed=2))


class TestFamily(unittest.TestCase):
    def setUp(self):
        self.d = _design({"c1": (100, 40), "c2": (50, 40)})

    def test_family_of_one_has_no_holm_penalty(self):
        outs = _outs(self.d, "A", 0.5, 1) + _outs(self.d, "B", 0.9, 2)
        res = S.compare_family(self.d, outs, [("B", "A")], M, replicates=1000, seed=4, alpha=0.05)["B-A"]
        self.assertEqual(res["p"], res["p_holm"])
        self.assertEqual(res["p"], 1 / 1001)                 # every replicate > 0: floored
        self.assertAlmostEqual(res["ci_level"], 0.95)
        lo, hi = S.ci([a - b for a, b in zip(S.bootstrap(self.d, outs, "B", M, replicates=1000, seed=4),
                                              S.bootstrap(self.d, outs, "A", M, replicates=1000, seed=4))], 0.05)
        self.assertAlmostEqual(res["ci_lo"], lo)            # paired: same resampled units
        self.assertAlmostEqual(res["ci_hi"], hi)
        self.assertAlmostEqual(res["estimate"], S.ratio(self.d, outs, "B", M) - S.ratio(self.d, outs, "A", M))

    def test_clearly_worse_newer_config_is_a_loss_not_a_win(self):
        outs = _outs(self.d, "A", 0.9, 1) + _outs(self.d, "B", 0.4, 2)
        res = S.compare_family(self.d, outs, [("B", "A")], M, replicates=1000, seed=4, alpha=0.05)["B-A"]
        self.assertFalse(res["win"])
        self.assertTrue(res["loss"])
        self.assertLess(res["ci_hi"], 0)

    def test_no_difference_is_neither_win_nor_loss(self):
        outs = _outs(self.d, "A", 0.8, 1) + _outs(self.d, "B", 0.8, 1)    # identical outcomes
        res = S.compare_family(self.d, outs, [("B", "A")], M, replicates=500, seed=4, alpha=0.05)["B-A"]
        self.assertEqual((res["win"], res["loss"], res["p"]), (False, False, 1.0))

    def test_holm_adjusted_cis_widen_with_rank(self):
        outs = _outs(self.d, "A", 0.5, 1) + _outs(self.d, "B", 0.9, 2) + _outs(self.d, "C", 0.92, 3)
        fam = [("B", "A"), ("C", "B")]
        res = S.compare_family(self.d, outs, fam, M, replicates=2000, seed=5, alpha=0.05)
        # B-A ranks first of two -> level alpha/2: wider than the unadjusted 95% CI
        plain = S.compare_family(self.d, outs, fam[:1], M, replicates=2000, seed=5, alpha=0.05)["B-A"]
        self.assertLessEqual(res["B-A"]["ci_lo"], plain["ci_lo"])
        self.assertGreaterEqual(res["B-A"]["ci_hi"], plain["ci_hi"])
        self.assertEqual(list(res), ["B-A", "C-B"])

    def test_step_down_levels_stop_at_first_non_rejection(self):
        # Review item 2: p {X: .03, Y: .04}, alpha .05 -> Holm rejects nothing; both CIs at alpha/2
        levels = S._holm_levels({"X": 0.03, "Y": 0.04}, 0.05)
        self.assertEqual(levels, {"X": 0.025, "Y": 0.025})
        # p = .03 means 1.5% of replicates lie at or below 0, so the alpha/2 CI must contain 0
        d = np.concatenate([np.full(15, -0.01), np.linspace(0.001, 0.2, 985)])
        lo, _ = S.ci(d, levels["X"])
        self.assertLessEqual(lo, 0)
        levels3 = S._holm_levels({"a": 0.001, "b": 0.04, "c": 0.03}, 0.05)
        self.assertEqual(levels3, {"a": 0.05 / 3, "b": 0.025, "c": 0.025})

    def test_no_ci_excludes_zero_without_holm_rejection(self):
        fam = [("B", "A"), ("C", "B")]
        for seed in range(15):
            outs = (_outs(self.d, "A", 0.80, seed) + _outs(self.d, "B", 0.86, seed + 100)
                    + _outs(self.d, "C", 0.90, seed + 200))
            for name, r in S.compare_family(self.d, outs, fam, M, replicates=500, seed=seed, alpha=0.05).items():
                if r["p_holm"] >= 0.05:
                    self.assertTrue(r["ci_lo"] <= 0 <= r["ci_hi"], (seed, name, r))
                    self.assertFalse(r["win"] or r["loss"])

    def test_duplicate_pairs_raise(self):
        outs = _outs(self.d, "A", 0.5, 1) + _outs(self.d, "B", 0.9, 2)
        with self.assertRaises(ValueError):
            S.compare_family(self.d, outs, [("B", "A"), ("B", "A")], M, replicates=100, seed=1, alpha=0.05)

    def test_config_without_items_is_undefined_and_json_safe(self):
        outs = _outs(self.d, "A", 0.5, 1)
        res = S.compare_family(self.d, outs, [("Z", "A")], M, replicates=200, seed=1, alpha=0.05)["Z-A"]
        self.assertTrue(res["undefined"])
        self.assertEqual((res["estimate"], res["ci_lo"], res["ci_hi"], res["win"], res["loss"], res["p"]),
                         (None, None, None, False, False, 1.0))
        json.dumps(res, allow_nan=False)

    def test_holm_is_monotone_and_keeps_order(self):
        adj = S.holm({"x": 0.04, "y": 0.001, "z": 0.04})
        self.assertEqual(list(adj), ["x", "y", "z"])
        self.assertAlmostEqual(adj["y"], 0.003)
        self.assertAlmostEqual(adj["x"], 0.08)
        self.assertAlmostEqual(adj["z"], 0.08)


class TestLowMicAndStrata(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_strata_by_each_dimension(self):
        d = _design({"c1": (100, 20), "c2": (20, 4)}, low_mic_every=4)
        outs = _outs(d, "B", 0.8, 9)
        self.assertEqual(set(S.stratum_entries(d, outs, "B", M, by="cell", cfg=self.cfg)), {"c1", "c2"})
        self.assertEqual(set(S.stratum_entries(d, outs, "B", M, by="device", cfg=self.cfg)),
                         {DEVICE_SPEAKER, DEVICE_OTHER})
        self.assertEqual(set(S.stratum_entries(d, outs, "B", M, by="low_mic", cfg=self.cfg)), {"True", "False"})
        with self.assertRaises(ValueError):
            S.stratum_entries(d, outs, "B", M, by="id", cfg=self.cfg)

    def test_small_single_cell_uses_wilson_else_bootstrap(self):
        d = _design({"c1": (100, 20), "c2": (20, 4)})
        outs = _outs(d, "B", 0.8, 9)
        cells = S.stratum_entries(d, outs, "B", M, by="cell", cfg=self.cfg)
        self.assertEqual(cells["c2"]["ci_method"], "wilson")       # 4 <= eval.scoring.wilson_max_units
        self.assertEqual(cells["c1"]["ci_method"], "bootstrap")
        # Review item 6: Wilson counts clusters (4 units), not items (12)
        est = sum(o.value for o in outs if o.unit_id.startswith("c2-")) / 12
        self.assertEqual(cells["c2"]["raw"]["ci"], list(S.wilson(4 * est, 4, self.cfg["eval"]["alpha"])))
        devices = S.stratum_entries(d, outs, "B", M, by="device", cfg=self.cfg)
        self.assertTrue(all(e["ci_method"] == "bootstrap" for e in devices.values()))   # spans cells: weighted

    def test_stratum_uses_full_design_weights(self):
        # c1 weight 5 (10/2), c2 weight 1 (2/2); speakers: c1-1 (hit) and c2-1 (miss)
        d = _design({"c1": (10, 2), "c2": (2, 2)})
        outs = [Outcome("c1-1", "B", M, "i1", 1.0), Outcome("c2-1", "B", M, "i2", 0.0),
                Outcome("c1-0", "B", M, "i3", 0.0)]
        sp = S.stratum_entries(d, outs, "B", M, by="device", cfg=self.cfg)[DEVICE_SPEAKER]
        self.assertAlmostEqual(sp["raw"]["estimate"], 5 / 6)
        self.assertEqual((sp["n_units"], sp["n_items"]), (2, 2))

    def test_one_domain_unit_with_items_per_cell_is_resampled_with_its_cell(self):
        # Review item 3 asked that this stratum (c1-1 all hits, c2-1 all misses) never
        # report CI [0.5, 0.5] "bootstrap". As a domain each unit is resampled with its
        # cell's other unit, so the domain is sometimes one unit, sometimes both.
        d = _design({"c1": (10, 2), "c2": (10, 2)})
        outs = ([Outcome("c1-1", "B", M, f"a{k}", 1.0) for k in range(3)]
                + [Outcome("c2-1", "B", M, f"b{k}", 0.0) for k in range(3)])
        sp = S.stratum_entries(d, outs, "B", M, by="device", cfg=self.cfg)[DEVICE_SPEAKER]
        self.assertEqual((sp["ci_method"], sp["raw"]["ci"]), ("bootstrap", [0.0, 1.0]))
        self.assertEqual(sp["n_units_with_items"], 2)
        self.assertAlmostEqual(sp["nan_replicate_share"], 0.25, delta=0.02)   # neither drawn

    def test_items_only_in_one_unit_cells_give_no_ci(self):
        # Rao-Wu freezes a one-unit cell, so two units with items there give no variance
        d = _design({"s1": (5, 1), "s2": (5, 1), "c3": (10, 3)})
        outs = [Outcome("s1-0", "B", M, "i1", 1.0), Outcome("s2-0", "B", M, "i2", 0.0)]
        e = S.metric_entry(d, outs, "B", M, cfg=self.cfg)
        self.assertEqual((e["ci_method"], e["raw"]["ci"], e["n_units_with_items"]),
                         ("insufficient_units", [None, None], 2))

    def test_one_unit_with_items_gives_no_ci(self):
        # Verifier repro: a 6-unit cell where one unit holds all items once returned
        # estimate 0.6, CI [0.6, 0.6] "bootstrap" and silently dropped ~33% of replicates.
        d = _design({"c1": (60, 6)})
        outs = [Outcome("c1-0", "B", M, f"i{k}", float(k < 3)) for k in range(5)]
        e = S.metric_entry(d, outs, "B", M, cfg=self.cfg, calibration=HIT)
        self.assertAlmostEqual(e["raw"]["estimate"], 0.6)
        self.assertEqual(e["raw"]["ci"], [None, None])
        self.assertEqual(e["corrected"]["ci"], [None, None])
        self.assertEqual(e["ci_method"], "insufficient_units")
        self.assertEqual((e["n_units"], e["n_units_with_items"], e["n_items"]), (6, 1, 5))
        self.assertIsNone(e["nan_replicate_share"])

    def test_nan_replicate_share_is_reported(self):
        d = _design({"c1": (60, 6)})
        outs = [Outcome(uid, "B", M, f"{uid}-{k}", float(k < 2)) for uid in ("c1-0", "c1-1") for k in range(3)]
        e = S.metric_entry(d, outs, "B", M, cfg=self.cfg)
        self.assertEqual((e["ci_method"], e["n_units_with_items"]), ("bootstrap", 2))
        self.assertAlmostEqual(e["nan_replicate_share"], (4 / 6) ** 5, delta=0.01)   # neither in 5 draws
        self.assertEqual(S.metric_entry(_design({"c1": (60, 4)}), outs, "B", M, cfg=self.cfg)
                         ["nan_replicate_share"], None)                                # Wilson: no bootstrap

    def test_ratio_units_restricts_to_a_domain(self):
        d = _design({"c1": (10, 2), "c2": (2, 2)})
        outs = [Outcome("c1-1", "B", M, "i1", 1.0), Outcome("c2-1", "B", M, "i2", 0.0),
                Outcome("c1-0", "B", M, "i3", 0.0)]
        speakers = [u for u in d.units if u.device == DEVICE_SPEAKER]
        self.assertAlmostEqual(S.ratio(d, outs, "B", M, units=speakers), 5 / 6)

    def test_metric_entry_corrected_and_empty(self):
        d = _design({"c1": (100, 20), "c2": (20, 10)})
        outs = _outs(d, "B", 0.8, 9)
        e = S.metric_entry(d, outs, "B", M, cfg=self.cfg, calibration=HIT)
        self.assertAlmostEqual(e["corrected"]["estimate"],
                               1 - S.rogan_gladen(1 - e["raw"]["estimate"], sens=0.9, spec=0.95))
        self.assertFalse(e["unreliable"])
        plain = S.metric_entry(d, outs, "B", M, cfg=self.cfg)
        self.assertEqual((plain["corrected"], plain["unreliable"]), (None, False))
        empty = S.metric_entry(d, outs, "Z", M, cfg=self.cfg)
        self.assertEqual(empty["raw"], {"estimate": None, "ci": [None, None]})
        self.assertEqual(empty["n_items"], 0)


class TestDomainEstimation(unittest.TestCase):
    """Strata are domains of the whole design: full-design weights and resamples."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _per_unit(design: Design, outs: list[Outcome], domain: list[Unit]):
        """Design weight w, domain indicator, value sum y and item count x per unit."""
        sampled = {c: sum(u.cell == c for u in design.units) for c in design.frame_n}
        w = np.array([design.frame_n[u.cell] / sampled[u.cell] for u in design.units])
        ids = {u.id for u in domain}
        inside = np.array([u.id in ids for u in design.units], dtype=float)
        pos = {u.id: i for i, u in enumerate(design.units)}
        y, x = np.zeros(len(pos)), np.zeros(len(pos))
        for o in outs:
            y[pos[o.unit_id]] += o.value
            x[pos[o.unit_id]] += 1
        return w, inside, y, x

    def test_stratum_replicates_use_the_whole_design_counts(self):
        d = _design({"c1": (100, 12), "c2": (20, 6), "c3": (40, 9)}, low_mic_every=4)
        outs = _outs(d, "B", 0.7, 5)
        low = [u for u in d.units if u.low_mic]
        reps, seed = self.cfg["eval"]["bootstrap_replicates"], self.cfg["eval"]["seed"]
        counts = S._resample_counts(d.units, reps, seed)
        w, inside, y, x = self._per_unit(d, outs, low)
        with np.errstate(invalid="ignore"):
            expected = (counts @ (w * inside * y)) / (counts @ (w * inside * x))
        got = np.array(S.bootstrap(d, outs, "B", M, replicates=reps, seed=seed, units=low))
        self.assertTrue(np.allclose(got, expected, equal_nan=True))
        entry = S.stratum_entries(d, outs, "B", M, by="low_mic", cfg=self.cfg)["True"]
        self.assertEqual(entry["ci_method"], "bootstrap")
        self.assertTrue(np.allclose(entry["raw"]["ci"], S.ci(expected, self.cfg["eval"]["alpha"])))
        self.assertAlmostEqual(entry["nan_replicate_share"], float(np.mean(np.isnan(expected))))

    def test_one_build_draws_the_count_matrix_once(self):
        d = _design({"c1": (100, 20), "c2": (20, 10)}, low_mic_every=3)
        outs = _outs(d, "B", 0.8, 9) + _outs(d, "A", 0.6, 8)
        ev = self.cfg["eval"]
        with mock.patch.object(S, "_resample_counts", wraps=S._resample_counts) as drawn:
            build = S.ScorecardBuild(d, iter(outs))          # outcomes are read once
            for config in ("A", "B"):
                build.metric_entry(config, M, cfg=self.cfg)
                for by in S.STRATA:
                    build.stratum_entries(config, M, by=by, cfg=self.cfg)
            build.compare_family([("B", "A")], M, replicates=ev["bootstrap_replicates"],
                                 seed=ev["seed"], alpha=ev["alpha"])
        self.assertEqual(drawn.call_count, 1)
        self.assertEqual(build.metric_entry("B", M, cfg=self.cfg), S.metric_entry(d, outs, "B", M, cfg=self.cfg))

    def test_domain_bootstrap_variance_matches_linearization(self):
        # Domain share and domain hit rate differ by cell, so the domain's size varies
        # across replicates. Linearization (Taylor) variance of the domain ratio R:
        # z_i = w_i d_i (y_i - R x_i) / sum(w d x),  V = sum_h n_h/(n_h-1) sum_i (z_i - zbar_h)^2
        rng = random.Random(0)
        # cell: frame_n, n, share in the domain, hit rate inside, hit rate outside
        spec = {"c1": (300, 40, 0.3, 0.95, 0.5), "c2": (30, 40, 0.7, 0.3, 0.9),
                "c3": (200, 40, 0.5, 0.8, 0.5), "c4": (60, 40, 0.4, 0.5, 0.8)}
        units, outs = [], []
        for cell, (_, n, share, p_in, p_out) in spec.items():
            for i in range(n):
                inside = rng.random() < share
                u = Unit(f"{cell}-{i}", cell, DEVICE_SPEAKER if inside else DEVICE_OTHER, False)
                units.append(u)
                outs += [Outcome(u.id, "B", M, f"{u.id}-{k}", float(rng.random() < (p_in if inside else p_out)))
                         for k in range(rng.randint(1, 5))]
        d = Design({c: s[0] for c, s in spec.items()}, tuple(units))
        domain = [u for u in units if u.device == DEVICE_SPEAKER]
        w, inside, y, x = self._per_unit(d, outs, domain)
        total_x = float((w * inside * x).sum())
        z = w * inside * (y - (w * inside * y).sum() / total_x * x) / total_x
        cells = np.array([u.cell for u in units])
        taylor = sum(len(zc) / (len(zc) - 1) * float(((zc - zc.mean()) ** 2).sum())
                     for zc in (z[cells == c] for c in spec))
        reps = np.array(S.bootstrap(d, outs, "B", M, replicates=20000, seed=11, units=domain))
        self.assertFalse(np.isnan(reps).any())
        self.assertAlmostEqual(float(np.var(reps)) / taylor, 1.0, delta=0.10)
        # Power check: resampling the domain alone (its size held fixed per cell, the
        # previous method) understates the variance well beyond that tolerance here.
        at = [i for i, u in enumerate(units) if u.device == DEVICE_SPEAKER]
        fixed = S._resample_counts(domain, 20000, 11)
        held = (fixed @ (w * y)[at]) / (fixed @ (w * x)[at])
        self.assertLess(float(np.var(held)) / taylor, 0.9)


class TestCalibrationDirection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(Path(self._tmp.name))
        self.d = _design({"c1": (100, 20), "c2": (20, 10)})
        self.outs = _outs(self.d, "B", 0.8, 9)

    def tearDown(self):
        self._tmp.cleanup()

    def test_hit_coded_metric_corrects_its_miss_rate(self):
        # Review item 1: obs recall 0.95, sens 0.9, spec 0.99 -> ~0.955, not 1.0
        self.assertAlmostEqual(S.corrected_rate(0.95, sens=0.9, spec=0.99, coding=S.CODING_HIT),
                               1 - 0.04 / 0.89)
        self.assertEqual(S.corrected_rate(0.3, sens=0.9, spec=0.95, coding=S.CODING_ERROR),
                         S.rogan_gladen(0.3, sens=0.9, spec=0.95))
        with self.assertRaises(ValueError):
            S.Calibration(0.9, 0.95, "recall")
        with self.assertRaises(TypeError):
            S.Calibration(0.9, 0.95)                          # no silent default direction

    def test_unusable_calibration_marks_metric_unreliable(self):
        # Review item 5: no specificity, sens + spec <= 1, or sensitivity under target
        low_sens = self.cfg["eval"]["calibration_min_sensitivity"] - 0.05
        for cal in (S.Calibration(0.95, None, S.CODING_HIT), S.Calibration(0.95, 0.04, S.CODING_HIT),
                    S.Calibration(low_sens, 0.99, S.CODING_HIT), S.Calibration(None, 0.99, S.CODING_ERROR)):
            e = S.metric_entry(self.d, self.outs, "B", M, cfg=self.cfg, calibration=cal)
            self.assertEqual((e["corrected"], e["unreliable"]), (None, True), cal)


class TestBar(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.cfg = _cfg(Path(tmp))

    def test_reports_each_criterion(self):
        res = S.my_task_bar(recall=0.95, recall_lb=0.90, precision=0.90, unexplained_misses=2, cfg=self.cfg)
        self.assertEqual({k: res[k] for k in ("pass", "recall", "recall_lower_bound", "precision",
                                              "misses_root_caused", "recall_lb_method")},
                         {"pass": False, "recall": True, "recall_lower_bound": True, "precision": True,
                          "misses_root_caused": False, "recall_lb_method": "bootstrap"})
        self.assertEqual(len(res["reasons"]), 1)

    def test_missing_lower_bound_fails_with_a_reason(self):
        # Review item 8: None never raises TypeError; it fails criterion 2
        res = S.my_task_bar(recall=0.97, recall_lb=None, precision=0.95, unexplained_misses=0, cfg=self.cfg)
        self.assertFalse(res["pass"])
        self.assertFalse(res["recall_lower_bound"])
        self.assertTrue(any("lower bound" in r for r in res["reasons"]))

    def test_non_bootstrap_lower_bound_fails(self):
        for method in ("wilson", "insufficient_units"):
            res = S.my_task_bar(recall=0.97, recall_lb=0.93, precision=0.95, unexplained_misses=0,
                                cfg=self.cfg, recall_lb_method=method)
            self.assertFalse(res["pass"], method)
            self.assertFalse(res["recall_lower_bound"], method)
        self.assertTrue(S.my_task_bar(recall=0.97, recall_lb=0.93, precision=0.95, unexplained_misses=0,
                                      cfg=self.cfg, recall_lb_method="bootstrap")["pass"])


class TestScorecard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = _cfg(self.root)
        d = _design({"c1": (100, 20), "c2": (20, 10)})
        outs = _outs(d, "B", 0.8, 9) + _outs(d, "A", 0.6, 8) + _outs(d, "C", 0.8, 7)
        self.entry = S.metric_entry(d, outs, "B", M, cfg=self.cfg, calibration=HIT)
        self.comparisons = S.compare_family(d, outs, [("B", "A"), ("C", "B"), ("Z", "C")], M,
                                            replicates=300, seed=1, alpha=0.05)
        self.bar = S.my_task_bar(recall=0.96, recall_lb=None, precision=0.92, unexplained_misses=0,
                                 cfg=self.cfg, recall_lb_method=self.entry["ci_method"])

    def tearDown(self):
        self._tmp.cleanup()

    def _card(self) -> dict:
        return _sample_scorecard(self.entry, self.comparisons, self.bar)

    def test_valid_sample_round_trips(self):
        card = self._card()
        path = self.root / "results" / "eval-run1" / "scorecard.json"
        S.write_scorecard(path, card, cfg=self.cfg)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), card)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["scorecard.json"])   # no tmp left

    def test_schema_path_comes_from_config(self):
        self.assertEqual(self.cfg["schemas"]["scorecard"], "schema/scorecard.json")
        self.assertTrue(config_file(self.cfg["schemas"]["scorecard"]).exists())

    def test_rejections_write_nothing(self):
        good = self._card()
        bad_cards = []
        for mutate in (
            lambda c: c.pop("hashes"),
            lambda c: c.update(extra=1),
            lambda c: c["miss_phantom_ledger"][0].update(root_cause="vibes"),
            lambda c: c["calibration"].pop("matcher"),
            lambda c: c["strata"].pop("low_mic"),
            lambda c: c["metrics"]["B"][M].pop("corrected"),
            lambda c: c["metrics"]["B"][M].pop("n_units_with_items"),
            lambda c: c["metrics"]["B"][M].update(ci_method="guess"),
            lambda c: c["metrics"]["B"][M]["raw"].update(ci=[0.1]),
            lambda c: c["escalation_rate"].update(presence=1.5),
            lambda c: c["run"].update(commit_sha="abc"),
            lambda c: c["metrics"]["B"][M]["raw"].update(estimate=math.nan),
            lambda c: c["metrics"]["B"][M].pop("unreliable"),
            lambda c: c.pop("comparisons"),
            lambda c: c.pop("my_task_bar"),
            lambda c: c.pop("latency"),
            lambda c: c["comparisons"]["B-A"].pop("ci_level"),
            lambda c: c["comparisons"]["B-A"].update(ci_lo=math.nan),
            lambda c: c["my_task_bar"].update(recall_lb_method="vibes"),
        ):
            card = copy.deepcopy(good)
            mutate(card)
            bad_cards.append(card)
        path = self.root / "scorecard.json"
        for i, card in enumerate(bad_cards):
            with self.assertRaises(S.ScorecardError, msg=str(i)):
                S.write_scorecard(path, card, cfg=self.cfg)
            self.assertFalse(path.exists())

    def test_without_cfg_reads_schema_path_from_defaults(self):
        path = self.root / "scorecard.json"
        S.write_scorecard(path, self._card())
        self.assertTrue(path.exists())

    def test_schema_uses_only_supported_keywords(self):
        with open(config_file(self.cfg["schemas"]["scorecard"]), encoding="utf-8") as fh:
            schema = json.load(fh)
        validate({}, schema)                                  # SchemaError on an unknown keyword
        self.assertEqual(validate(self._card(), schema), [])


if __name__ == "__main__":
    unittest.main()
