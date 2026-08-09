import pathlib
import sys
import unittest

import numpy as np


sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "analysis"))
import quote_distance_research  # noqa: E402


class QuoteDistanceResearchTest(unittest.TestCase):
    def test_first_touch_respects_quote_side(self):
        values = np.asarray([np.nan, 101.0, 99.0, 102.0])
        self.assertEqual(quote_distance_research._first_touch(values, 100.0, "bid"), 2)
        self.assertEqual(quote_distance_research._first_touch(values, 101.5, "ask"), 3)

    def test_no_trade_has_no_touch(self):
        values = np.asarray([np.inf, np.inf])
        self.assertIsNone(quote_distance_research._first_touch(values, 100.0, "bid"))


if __name__ == "__main__":
    unittest.main()
