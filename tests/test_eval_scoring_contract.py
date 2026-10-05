"""CONTRACT for eval/scoring.py (issue #17, B.6, B.7). Written by the build manager.

Builders must not edit this file (checksummed, G.6). Add further tests in
tests/test_eval_scoring.py. No model calls.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from eval import scoring as S
from eval.records import DEVICE_OTHER, DEVICE_SPEAKER, Design, Outcome, Unit
from pipeline.config import load_config
from pipeline_helpers import overlay


def _design(cells: dict[str, tuple[int, int]]) -> Design:
    """cells: name -> (frame_n, sample_n)."""
    units = []
    for cell, (_, n) in cells.items():
        units += [Unit(f"{cell}-{i}", cell, DEVICE_SPEAKER if i % 2 else DEVICE_OTHER, False) for i in range(n)]
    return Design({c: fn for c, (fn, _) in cells.items()}, tuple(units))


class TestKnownAnswers(unittest.TestCase):
    def test_holm_textbook(self):
        adj = S.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.005})
        for k, v in {"d": 0.02, "a": 0.03, "c": 0.06, "b": 0.06}.items():
            self.assertAlmostEqual(adj[k], v)

    def test_holm_caps_at_one(self):
        self.assertEqual(S.holm({"a": 0.6, "b": 0.7}), {"a": 1.0, "b": 1.0})

    def test_wilson_8_of_10(self):
        lo, hi = S.wilson(8, 10, 0.05)
        self.assertAlmostEqual(lo, 0.4902, places=3)
        self.assertAlmostEqual(hi, 0.9433, places=3)

    def test_rogan_gladen(self):
        self.assertAlmostEqual(S.rogan_gladen(0.8, sens=0.9, spec=0.95), 0.75 / 0.85)
        self.assertEqual(S.rogan_gladen(0.01, sens=0.9, spec=0.95), 0.0)   # clipped to [0, 1]
        self.assertEqual(S.rogan_gladen(0.99, sens=0.9, spec=0.95), 1.0)

    def test_weighted_ratio_uses_design_weights(self):
        # c1: frame 10, n 2 (weight 5); c2: frame 2, n 2 (weight 1)
        d = _design({"c1": (10, 2), "c2": (2, 2)})
        outs = [Outcome("c1-0", "B", "my_task_recall", "i1", 1.0), Outcome("c1-0", "B", "my_task_recall", "i2", 0.0),
                Outcome("c2-0", "B", "my_task_recall", "i3", 0.0), Outcome("c2-1", "B", "my_task_recall", "i4", 0.0)]
        # (5*1 + 5*0 + 1*0 + 1*0) / (5*2 + 1*1 + 1*1) = 5/12
        self.assertAlmostEqual(S.ratio(d, outs, "B", "my_task_recall"), 5 / 12)


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        self.d = _design({"c1": (100, 40), "c2": (50, 40)})

    def _outs(self, config, p, seed):
        rng = random.Random(seed)
        return [Outcome(u.id, config, "my_task_recall", f"{u.id}-{k}", float(rng.random() < p))
                for u in self.d.units for k in range(3)]

    def test_seeded_and_deterministic(self):
        outs = self._outs("B", 0.8, 1)
        r1 = S.bootstrap(self.d, outs, "B", "my_task_recall", replicates=500, seed=7)
        r2 = S.bootstrap(self.d, outs, "B", "my_task_recall", replicates=500, seed=7)
        self.assertEqual(r1, r2)
        self.assertEqual(len(r1), 500)

    def test_all_hits_gives_degenerate_ci(self):
        outs = self._outs("B", 1.1, 1)
        lo, hi = S.ci(S.bootstrap(self.d, outs, "B", "my_task_recall", replicates=300, seed=1), 0.05)
        self.assertEqual((lo, hi), (1.0, 1.0))

    def test_ci_covers_truth(self):
        outs = self._outs("B", 0.8, 3)
        lo, hi = S.ci(S.bootstrap(self.d, outs, "B", "my_task_recall", replicates=2000, seed=3), 0.05)
        self.assertLess(lo, 0.8)
        self.assertGreater(hi, 0.8)
        self.assertLess(hi - lo, 0.2)

    def test_paired_family_holm_and_win(self):
        outs = self._outs("A", 0.5, 11) + self._outs("B", 0.95, 12) + self._outs("C", 0.95, 13)
        res = S.compare_family(self.d, outs, [("B", "A"), ("C", "B")], "my_task_recall",
                               replicates=2000, seed=5, alpha=0.05)
        self.assertTrue(res["B-A"]["win"])
        self.assertGreater(res["B-A"]["ci_lo"], 0)
        self.assertFalse(res["C-B"]["win"])
        for r in res.values():
            self.assertGreaterEqual(r["p_holm"], r["p"])
            self.assertTrue({"estimate", "ci_lo", "ci_hi", "p", "p_holm", "win"} <= set(r))


class TestBarAndScorecard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(overlay=overlay(Path(self._tmp.name)))

    def tearDown(self):
        self._tmp.cleanup()

    def test_bar_needs_all_four(self):
        ok = S.my_task_bar(recall=0.96, recall_lb=0.91, precision=0.92, unexplained_misses=0, cfg=self.cfg)
        self.assertTrue(ok["pass"])
        for kw, failing in [("recall", 0.94), ("recall_lb", 0.89), ("precision", 0.89), ("unexplained_misses", 1)]:
            args = dict(recall=0.96, recall_lb=0.91, precision=0.92, unexplained_misses=0)
            args[kw] = failing
            self.assertFalse(S.my_task_bar(**args, cfg=self.cfg)["pass"], kw)

    def test_scorecard_schema_rejects_missing_keys(self):
        path = Path(self._tmp.name) / "scorecard.json"
        with self.assertRaises(S.ScorecardError):
            S.write_scorecard(path, {"metrics": {}})
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
