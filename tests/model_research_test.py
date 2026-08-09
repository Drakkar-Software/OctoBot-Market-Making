import pathlib
import sys
import unittest

import numpy as np


sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "analysis"))
import model_research  # noqa: E402
import cross_venue_target_research  # noqa: E402
import hmm_regime_research  # noqa: E402


class ModelResearchTest(unittest.TestCase):
    def test_lag_difference_is_causal(self):
        values = np.asarray([1.0, 1.1, 1.3])
        result = model_research._lag_difference(values, 1)
        self.assertTrue(np.isnan(result[0]))
        np.testing.assert_allclose(result[1:], [1_000.0, 2_000.0])

    def test_kalman_normalizes_constant_source_basis(self):
        anchor = np.linspace(10.0, 10.01, 1_000)
        shifted = anchor + 0.002
        result = model_research._causal_kalman_fair([anchor, shifted], 500)
        self.assertTrue(np.isfinite(result["fair_log"]).all())
        self.assertLess(abs(result["fair_log"][-1] - anchor[-1]), 0.001)

    def test_source_ablation_excludes_target_history(self):
        names = [
            "target_return_100ms_bps",
            "target_spread_bps",
            "kalman_fair_gap_bps",
            "binance_perp_usdt_return_100ms_bps",
        ]
        indices = model_research._feature_indices(names)
        selected = {names[index] for index in indices["source_only"]}
        self.assertNotIn("target_return_100ms_bps", selected)
        self.assertNotIn("target_spread_bps", selected)
        self.assertIn("kalman_fair_gap_bps", selected)

    def test_hac_recognizes_positive_loss_improvement(self):
        outcome = np.sin(np.arange(4_000) / 20) * 0.5
        prediction = outcome * 0.5
        difference = outcome**2 - (outcome - prediction) ** 2
        statistic, p_value = model_research._hac_test(difference)
        self.assertGreater(statistic, 0)
        self.assertLess(p_value, 0.01)

    def test_benjamini_hochberg_is_monotone(self):
        rows = [{"hac_p_value": value} for value in (0.001, 0.01, 0.20)]
        model_research._benjamini_hochberg(rows)
        adjusted = [row["bh_q_value"] for row in rows]
        self.assertEqual(adjusted, sorted(adjusted))
        self.assertTrue(all(p <= q <= 1 for p, q in zip((0.001, 0.01, 0.20), adjusted)))

    def test_source_target_reframing_uses_the_next_contiguous_return(self):
        names = [
            "binance_perp_usdt_return_100ms_bps",
            "binance_perp_usdt_return_500ms_bps",
            "binance_perp_usdt_return_1s_bps",
        ]
        matrix = np.asarray([
            [1.0, 10.0, 100.0],
            [2.0, 20.0, 200.0],
            [3.0, 30.0, 300.0],
            [4.0, 40.0, 400.0],
        ])
        times = np.asarray([0.0, 0.5, 1.5, 2.0])
        reframed, labels, label_times = cross_venue_target_research._reframe_source(
            matrix, times, names, "binance_perp_usdt"
        )
        np.testing.assert_array_equal(reframed, matrix[[0, 2]])
        np.testing.assert_array_equal(labels, [20.0, 40.0])
        np.testing.assert_array_equal(label_times, [0.0, 1.5])

    def test_external_source_ablation_removes_candidate_from_composites(self):
        names = [
            "target_return_100ms_bps",
            "core_return_100ms_bps",
            "kalman_fair_gap_bps",
            "binance_perp_usdt_return_100ms_bps",
            "okx_perp_usdt_return_100ms_bps",
        ]
        selected = cross_venue_target_research._source_feature_indices(
            names, "binance_perp_usdt", "external_only"
        )
        selected_names = {names[index] for index in selected}
        self.assertIn("target_return_100ms_bps", selected_names)
        self.assertIn("okx_perp_usdt_return_100ms_bps", selected_names)
        self.assertNotIn("binance_perp_usdt_return_100ms_bps", selected_names)
        self.assertNotIn("core_return_100ms_bps", selected_names)
        self.assertNotIn("kalman_fair_gap_bps", selected_names)

    def test_hmm_filter_is_causal_for_a_fixed_model(self):
        class FixedModel:
            startprob_ = np.asarray([0.7, 0.2, 0.1])
            transmat_ = np.asarray([
                [0.9, 0.08, 0.02],
                [0.1, 0.8, 0.1],
                [0.05, 0.15, 0.8],
            ])
            means_ = np.asarray([[0.0], [1.0], [3.0]])
            _covars_ = np.asarray([[1.0], [1.0], [1.0]])

        observations = np.asarray([[0.0], [0.5], [2.0], [10.0]])
        times = np.arange(4) * 0.5
        prefix, _, _ = hmm_regime_research._causal_filter(
            FixedModel(), observations[:3], times[:3]
        )
        complete, _, _ = hmm_regime_research._causal_filter(
            FixedModel(), observations, times
        )
        np.testing.assert_allclose(prefix, complete[:3])

    def test_hmm_segments_do_not_bridge_missing_time(self):
        times = np.asarray([0.0, 0.5, 1.0, 2.0, 2.5, 4.0])
        self.assertEqual(hmm_regime_research._segment_lengths(times), [3, 2, 1])


if __name__ == "__main__":
    unittest.main()
