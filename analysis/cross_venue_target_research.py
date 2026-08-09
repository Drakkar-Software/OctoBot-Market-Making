"""Reframe the causal feature cache to compare every futures feed as a target.

The existing cache was sampled every 500 ms.  A source venue's return over the
*next* 500 ms is therefore the following row's trailing 500 ms return, provided
the two rows are contiguous.  This lets us compare target-history, external-only,
and full linear models without reparsing hundreds of gigabytes of raw events.

This is a forecast comparison, not a passive-fill or net-profit backtest.  Source
venues also lack cached spread, depth, and queue features, so economic ranking is
kept separate from forecast ranking.
"""

import argparse
import collections
import datetime
import json
import math
import pathlib
import statistics
import typing
import warnings

import numpy as np
from sklearn.base import clone
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import ElasticNet, Ridge

from exploratory_market_making import SOURCE_SERIES
from model_research import (
    GRID_MS,
    HORIZON_BUCKETS,
    _benjamini_hochberg,
    _block_bootstrap_ci,
    _hac_test,
    _linear_pipeline,
    _metrics,
)


MODEL_DEFINITIONS = {
    "target_history": _linear_pipeline(Ridge(alpha=10.0)),
    "external_only": _linear_pipeline(
        ElasticNet(alpha=0.01, l1_ratio=0.10, max_iter=2_000)
    ),
    "full": _linear_pipeline(
        ElasticNet(alpha=0.01, l1_ratio=0.10, max_iter=2_000)
    ),
}

GENERIC_CORE_FEATURES = frozenset({
    "core_return_100ms_bps",
    "core_return_500ms_bps",
    "core_return_1s_bps",
    "core_dispersion_100ms_bps",
    "ewma_volatility_bps",
    "jump_score",
    "core_source_count",
    "kalman_fair_gap_bps",
    "kalman_velocity_bps",
    "kalman_uncertainty_bps",
    "kalman_innovation_score",
})


def _candidate_sources() -> dict[str, tuple[str, str]]:
    return {
        name: (venue, quote)
        for name, venue, quote in SOURCE_SERIES
        if "_perp_" in name
    }


def _source_feature_indices(
    names: list[str], source_name: str, model_name: str
) -> np.ndarray:
    own_returns = {
        f"{source_name}_return_100ms_bps",
        f"{source_name}_return_500ms_bps",
        f"{source_name}_return_1s_bps",
    }
    if model_name == "target_history":
        return np.asarray([index for index, name in enumerate(names) if name in own_returns])
    if model_name == "full":
        return np.arange(len(names))

    # A strict external-only ablation must not quietly retain the target through
    # the generic four-USDT-venue composite or Kalman state.
    target_is_in_core = source_name in {
        "binance_perp_usdt",
        "okx_perp_usdt",
        "bybit_perp_usdt",
        "mexc_perp_usdt",
    }
    selected = []
    for index, name in enumerate(names):
        if name.startswith(f"{source_name}_"):
            continue
        if target_is_in_core and name in GENERIC_CORE_FEATURES:
            continue
        selected.append(index)
    return np.asarray(selected)


def _original_feature_indices(names: list[str], model_name: str) -> np.ndarray:
    if model_name == "target_history":
        return np.asarray([
            index for index, name in enumerate(names)
            if name.startswith("target_return_") or name == "target_spread_bps"
        ])
    if model_name == "external_only":
        return np.asarray([
            index for index, name in enumerate(names)
            if not name.startswith("target_return_") and name != "target_spread_bps"
        ])
    return np.arange(len(names))


def _reframe_source(
    matrix: np.ndarray,
    times: np.ndarray,
    names: list[str],
    source_name: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return current features and the source's subsequent contiguous 500 ms return."""
    label_index = names.index(f"{source_name}_return_500ms_bps")
    expected_step = HORIZON_BUCKETS * GRID_MS / 1000
    contiguous = np.isclose(times[1:] - times[:-1], expected_step, atol=1e-9)
    labels = matrix[1:, label_index].astype(np.float32, copy=False)
    valid = contiguous & np.isfinite(labels)
    return matrix[:-1][valid], labels[valid], times[:-1][valid]


def _market_rows(cache: np.lib.npyio.NpzFile) -> typing.Iterator[dict]:
    metadata = json.loads(str(cache["metadata"]))
    names = [str(value) for value in cache["feature_names"]]
    sources = _candidate_sources()
    for original_venue, asset in (tuple(item) for item in metadata["targets"]):
        prefix = f"{original_venue}_{asset}"
        matrix = cache[f"{prefix}_X"]
        labels = cache[f"{prefix}_y"]
        times = cache[f"{prefix}_time"]
        yield {
            "venue": original_venue,
            "asset": asset,
            "quote": "USDT",
            "source_name": None,
            "matrix": matrix,
            "labels": labels,
            "times": times,
            "feature_indices": {
                model: _original_feature_indices(names, model)
                for model in MODEL_DEFINITIONS
            },
            "cache_conditioning_target": f"{original_venue}:{asset}",
        }
        for source_name, (venue, quote) in sources.items():
            source_matrix, source_labels, source_times = _reframe_source(
                matrix, times, names, source_name
            )
            yield {
                "venue": venue,
                "asset": asset,
                "quote": quote,
                "source_name": source_name,
                "matrix": source_matrix,
                "labels": source_labels,
                "times": source_times,
                "feature_indices": {
                    model: _source_feature_indices(names, source_name, model)
                    for model in MODEL_DEFINITIONS
                },
                "cache_conditioning_target": f"{original_venue}:{asset}",
            }


def evaluate(
    cache_path: pathlib.Path,
    output: pathlib.Path,
    initial_train_hours: int,
) -> dict:
    warnings.filterwarnings("ignore", message="Skipping features without any observed values")
    cache = np.load(cache_path, allow_pickle=False)
    metadata = json.loads(str(cache["metadata"]))
    fold_rows = []
    predictions: dict[tuple[str, str, str, str], list[np.ndarray]] = collections.defaultdict(list)
    outcomes: dict[tuple[str, str, str, str], list[np.ndarray]] = collections.defaultdict(list)
    skipped = []

    for market in _market_rows(cache):
        matrix = market["matrix"]
        labels = market["labels"]
        times = market["times"]
        finite_labels = labels[np.isfinite(labels)]
        market_name = f"{market['venue']} {market['asset']}-{market['quote']}"
        if len(finite_labels) < 6_000 or np.std(finite_labels) < 1e-6:
            skipped.append({"market": market_name, "reason": "insufficient varying labels"})
            continue
        total_hours = int(math.floor(float(times.max()) / 3600)) + 1
        completed_folds = 0
        for test_hour in range(initial_train_hours, total_hours):
            boundary = test_hour * 3600
            # All labels end 500 ms after their feature timestamp.
            train = (times + HORIZON_BUCKETS * GRID_MS / 1000) <= boundary
            test = (times >= boundary) & (times < (test_hour + 1) * 3600)
            train &= np.isfinite(labels)
            test &= np.isfinite(labels)
            if train.sum() < 5_000 or test.sum() < 1_000:
                continue
            completed_folds += 1
            for model_name, estimator in MODEL_DEFINITIONS.items():
                selected = market["feature_indices"][model_name]
                train_x = matrix[train][:, selected]
                test_x = matrix[test][:, selected]
                train_y = labels[train]
                test_y = labels[test]
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=ConvergenceWarning)
                    warnings.filterwarnings(
                        "ignore", message="Skipping features without any observed values"
                    )
                    fitted = clone(estimator).fit(train_x, train_y)
                prediction = fitted.predict(test_x)
                train_prediction = fitted.predict(train_x)
                active_threshold = float(np.quantile(np.abs(train_prediction), 0.90))
                metrics = _metrics(test_y, prediction, active_threshold)
                row = {
                    "venue": market["venue"],
                    "asset": market["asset"],
                    "quote": market["quote"],
                    "model": model_name,
                    "test_hour": test_hour,
                    "feature_count": int(len(selected)),
                    "active_threshold_bps": active_threshold,
                    **metrics,
                }
                fold_rows.append(row)
                key = (market["venue"], market["asset"], market["quote"], model_name)
                predictions[key].append(np.asarray(prediction))
                outcomes[key].append(test_y)
        if completed_folds:
            print(f"Evaluated {market_name}: {completed_folds} fold(s)", flush=True)
        else:
            skipped.append({"market": market_name, "reason": "no sufficiently large folds"})

    aggregates = []
    for key, prediction_parts in predictions.items():
        venue, asset, quote, model = key
        prediction = np.concatenate(prediction_parts)
        outcome = np.concatenate(outcomes[key])
        loss_difference = outcome * outcome - (outcome - prediction) ** 2
        relevant_folds = [
            row for row in fold_rows
            if (row["venue"], row["asset"], row["quote"], row["model"]) == key
        ]
        statistic, p_value = _hac_test(loss_difference)
        ci_low, ci_high = _block_bootstrap_ci(loss_difference)
        aggregates.append({
            "venue": venue,
            "asset": asset,
            "quote": quote,
            "model": model,
            **_metrics(outcome, prediction, 0.05),
            "folds": len(relevant_folds),
            "positive_skill_folds": sum(row["mse_skill_pct"] > 0 for row in relevant_folds),
            "median_fold_mse_skill_pct": statistics.median(
                row["mse_skill_pct"] for row in relevant_folds
            ),
            "hac_t_statistic": statistic,
            "hac_p_value": p_value,
            "block_bootstrap_loss_difference_ci": [ci_low, ci_high],
        })
    _benjamini_hochberg(aggregates)
    aggregates.sort(key=lambda row: row["mse_skill_pct"], reverse=True)
    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cache": str(cache_path),
        "metadata": metadata,
        "protocol": {
            "target": "mid-price return over the next contiguous 500 ms",
            "models": {
                "target_history": "price-return lags from the candidate target only",
                "external_only": "all other venues, with composite features removed when they contain the candidate target",
                "full": "target history plus all external and composite features",
            },
            "split": (
                f"{initial_train_hours} initial hours followed by expanding-train, one-hour "
                "chronological test folds with boundary-overlapping labels purged"
            ),
            "multiplicity": "Benjamini-Hochberg correction across every target/model test",
            "important_limits": [
                "forecasting only: no queue-conditioned passive fills, fees, funding, or markout PnL",
                "reframed source targets have mid-price history but no cached spread/depth/queue features",
                "rows were originally retained when the Gate/HTX conditioning market and core feeds were valid",
                "the capture covers eight hours within one day and cannot establish multi-regime durability",
            ],
        },
        "fold_results": fold_rows,
        "aggregate_results": aggregates,
        "skipped": skipped,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
    return report


def _number(value: typing.Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def markdown_report(report: dict) -> str:
    rows = report["aggregate_results"]
    lookup = {
        (row["venue"], row["asset"], row["quote"], row["model"]): row
        for row in rows
    }
    markets = sorted({
        (row["venue"], row["asset"], row["quote"])
        for row in rows
    })
    comparative = []
    for venue, asset, quote in markets:
        target = lookup.get((venue, asset, quote, "target_history"))
        external = lookup.get((venue, asset, quote, "external_only"))
        full = lookup.get((venue, asset, quote, "full"))
        if target and external and full:
            comparative.append({
                "venue": venue,
                "asset": asset,
                "quote": quote,
                "target": target,
                "external": external,
                "full": full,
                "incremental": full["mse_skill_pct"] - target["mse_skill_pct"],
            })
    comparative.sort(key=lambda row: row["external"]["mse_skill_pct"], reverse=True)

    lines = [
        "# Cross-venue 500 ms target comparison",
        "",
        f"Window: `{report['metadata']['start']}` to `{report['metadata']['end']}`.",
        "",
        "Each futures market is treated as the venue we might quote. The external-only column is ",
        "the cleanest lead/lag test: it excludes that candidate venue's own history. This remains ",
        "a mid-price forecast study, not evidence of passive-fill profitability.",
        "",
        "## External lead by candidate target",
        "",
        "| Rank | Candidate target | Own-history skill | External-only skill | Full skill | Full minus own | External positive folds | External BH q | External active accuracy |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(comparative, start=1):
        external = row["external"]
        lines.append(
            f"| {rank} | {row['venue']} {row['asset']}-{row['quote']} "
            f"| {_number(row['target']['mse_skill_pct'])}% "
            f"| {_number(external['mse_skill_pct'])}% "
            f"| {_number(row['full']['mse_skill_pct'])}% "
            f"| {_number(row['incremental'])} pp "
            f"| {external['positive_skill_folds']}/{external['folds']} "
            f"| {_number(external['bh_q_value'], 4)} "
            f"| {_number(100 * external['active_direction_accuracy'] if external['active_direction_accuracy'] is not None else None, 1)}% |"
        )

    lines.extend([
        "",
        "## Best external candidates by venue",
        "",
        "| Venue / quote | Best three assets by external-only MSE skill |",
        "|---|---|",
    ])
    by_venue: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for row in comparative:
        by_venue[(row["venue"], row["quote"])].append(row)
    for venue_quote in sorted(by_venue):
        venue, quote = venue_quote
        ordered = sorted(
            by_venue[venue_quote],
            key=lambda row: row["external"]["mse_skill_pct"],
            reverse=True,
        )[:3]
        summary = ", ".join(
            f"{row['asset']} {_number(row['external']['mse_skill_pct'])}%"
            for row in ordered
        )
        lines.append(f"| {venue} {quote} | {summary} |")

    lines.extend([
        "",
        "## Interpretation limits",
        "",
        "- A high number can identify a slow venue that catches up to others; it can also identify stale or thin pricing. Only passive-fill-conditioned markouts can distinguish useful fills from toxic fills.",
        "- Low turnover is not a rejection for a small account. It limits safe quote size and growth capacity.",
        "- The source-target reframing has no cached spread, depth, or queue state. Those must be joined from the raw order-book stream before selecting a live target.",
        "- Promote a market only after the result repeats across multiple days, hours, volatility regimes, and fee tiers.",
        "",
    ])
    return "\n".join(lines)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--initial-train-hours", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    report = evaluate(arguments.cache, arguments.output, arguments.initial_train_hours)
    print(f"Wrote {arguments.output}", flush=True)
    print(f"Wrote {arguments.output.with_suffix('.md')}", flush=True)
    print(
        f"Evaluated {len(report['aggregate_results']) // len(MODEL_DEFINITIONS)} candidate markets",
        flush=True,
    )


if __name__ == "__main__":
    main()
