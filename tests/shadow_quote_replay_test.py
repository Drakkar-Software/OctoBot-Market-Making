import pathlib
import sys
import unittest

import numpy as np


sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "analysis"))
import shadow_quote_replay  # noqa: E402


class ShadowQuoteReplayTest(unittest.TestCase):
    def test_touch_and_trade_through_fill_policies(self):
        trade_min = np.asarray([101.0, 100.0, 99.99])
        trade_max = np.asarray([101.0, 102.0, 102.1])
        self.assertEqual(
            shadow_quote_replay._first_fill(
                trade_min, trade_max, 100.0, "bid", "touch", 0.5
            ),
            1,
        )
        self.assertEqual(
            shadow_quote_replay._first_fill(
                trade_min, trade_max, 100.0, "bid", "trade_through", 0.5
            ),
            2,
        )

    def test_round_trip_ledger_marks_profit_and_fees(self):
        ledger = shadow_quote_replay.Ledger.create(100.0)
        shadow_quote_replay._apply_fill(ledger, "bid", 100.0, 0.02, 1.0)
        shadow_quote_replay._apply_fill(ledger, "ask", 101.0, 0.02, 1.0)
        self.assertAlmostEqual(ledger.inventory_base, 0.0)
        self.assertAlmostEqual(ledger.turnover_usd, 4.02)
        self.assertAlmostEqual(ledger.fees_usd, 0.000402)
        self.assertAlmostEqual(ledger.equity(101.0), 100.019598)

    def test_quotes_never_cross_observed_touch(self):
        bid, ask, _, _, _ = shadow_quote_replay._quote_prices(
            mid=100.0,
            half_spread_bps=1.0,
            prediction_bps=10.0,
            inventory_base=0.0,
            max_inventory_usd=10.0,
            inventory_skew_at_cap_bps=5.0,
            distance_bps=2.0,
        )
        self.assertLessEqual(bid, 99.99)
        self.assertGreaterEqual(ask, 100.01)

    def test_inventory_cap_allows_only_reducing_side(self):
        quantity = 0.02
        self.assertFalse(
            shadow_quote_replay._side_allowed(0.10, "bid", quantity, 100.0, 10.0)
        )
        self.assertTrue(
            shadow_quote_replay._side_allowed(0.10, "ask", quantity, 100.0, 10.0)
        )

    def test_order_quantity_is_rounded_down_to_contracts(self):
        self.assertEqual(
            shadow_quote_replay._order_quantity(1.0, 0.20, 1.0), 5.0
        )
        self.assertEqual(
            shadow_quote_replay._order_quantity(2.0, 76.0, 0.1), 0.0
        )
        self.assertEqual(
            shadow_quote_replay._order_quantity(8.0, 76.0, 0.1), 0.1
        )


if __name__ == "__main__":
    unittest.main()
