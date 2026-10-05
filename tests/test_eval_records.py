"""eval/records.py (issue #11): design weights. No model calls."""

import unittest

from eval.records import DEVICE_OTHER, DEVICE_SPEAKER, Design, Unit


class TestDesign(unittest.TestCase):
    def test_weight_is_frame_n_over_sample_n(self):
        units = (Unit("a", "c1", DEVICE_SPEAKER, False), Unit("b", "c1", DEVICE_OTHER, False),
                 Unit("c", "c2", DEVICE_SPEAKER, False))
        d = Design({"c1": 10, "c2": 7}, units)
        self.assertEqual(d.weight(units[0]), 5.0)
        self.assertEqual(d.weight(units[2]), 7.0)


if __name__ == "__main__":
    unittest.main()
