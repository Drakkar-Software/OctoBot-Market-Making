import pathlib
import sys
import unittest

import numpy as np


sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "analysis"))
import volume_clock_research  # noqa: E402


class VolumeClockResearchTest(unittest.TestCase):
    def test_volume_closes_only_after_threshold_is_reached(self):
        volume = np.asarray([2.0, 3.0, 4.0, 1.0, 10.0])
        self.assertEqual(volume_clock_research._volume_closes(volume, 5.0), [1, 3, 4])

    def test_mapped_closes_are_strictly_increasing_market_rows(self):
        row_buckets = np.asarray([0, 1, 3, 4, 8])
        mapped = volume_clock_research._mapped_closes([1, 2, 3, 7, 8], row_buckets)
        self.assertEqual(mapped, [(1, 1), (2, 2), (3, 3), (7, 4)])

    def test_binance_aggressor_sign(self):
        buy = {
            "stream": "aggTrade",
            "payload": {"p": "10", "q": "2", "m": False},
        }
        sell = {
            "stream": "aggTrade",
            "payload": {"p": "10", "q": "3", "m": True},
        }
        self.assertEqual(list(volume_clock_research._trade_rows_and_sign("binance", buy)), [(10.0, 2.0, 1.0)])
        self.assertEqual(list(volume_clock_research._trade_rows_and_sign("binance", sell)), [(10.0, 3.0, -1.0)])

    def test_walk_forward_masks_purge_crossing_labels(self):
        times = np.asarray([10_799.0, 10_800.0, 14_399.0, 14_399.5])
        outcome_end_times = np.asarray([10_800.5, 10_801.0, 14_399.5, 14_400.5])
        train, test = volume_clock_research._walk_forward_masks(
            times, outcome_end_times, test_hour=3
        )
        np.testing.assert_array_equal(train, [False, False, False, False])
        np.testing.assert_array_equal(test, [False, True, True, False])


if __name__ == "__main__":
    unittest.main()
