"""Causal hidden-Markov regime test for short-horizon market-making signals.

The HMM is fit only on each chronological training fold.  Test-state probabilities
are filtered forward using observations available at that timestamp; smoothed test
posteriors are deliberately not used.  The study tests whether latent regimes improve
the return forecast and characterizes states that should later control quote width,
fill-intensity curves, and one-sided inventory behavior.
"""

import argparse
import collections
import datetime
import json
import logging
import math
import pathlib
import statistics
import typing
import warnings

import numpy as np
from hmmlearn.hmm import GaussianHMM
from scipy.special import logsumexp
from sklearn.base import clone
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import ElasticNet, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from cross_venue_target_research import _market_rows
from model_research import (
    GRID_MS,
    HORIZON_BUCKETS,
    _benjamini_hochberg,
    _block_bootstrap_ci,
    _hac_test,
    _linear_pipeline,
    _metrics,
)


DEFAULT_TARGETS = (
    ("mexc", "SOL", "USDC"),
    ("mexc", "SOL", "USDT"),
    ("gate", "SOL", "USDT"),
    ("mexc", "DOGE", "USDT"),
    ("mexc", "BCH", "USDT"),
    ("htx", "ENA", "USDT"),
    ("htx", "AVAX", "USDT"),
    ("htx", "XLM", "USDT"),
)
STATE_NAMES = ("calm", "active", "stress")
EXPECTED_STEP_SECONDS = HORIZON_BUCKETS * GRID_MS / 1000


def _parse_targets(value: str) -> tuple[tuple[str, str, str], ...]:
    result = []
    for item in value.split(","):
        venue, asset, quote = item.strip().split(":", maxsplit=2)
        result.append((venue.lower(), asset.upper(), quote.upper()))
    return tuple(result)


def _segment_lengths(times: np.ndarray) -> list[int]:
    if not len(times):
        return []
    boundaries = np.flatnonzero(
        ~np.isclose(np.diff(times), EXPECTED_STEP_SECONDS, atol=1e-9)
    ) + 1
    return [len(part) for part in np.split(np.arange(len(times)), boundaries)]


def _hmm_input_indices(names: list[str], source_name: typing.Optional[str]) -> list[int]:
    gap_name = f"{source_name}_gap_bps" if source_name else "kalman_fair_gap_bps"
    selected_names = (
        "core_return_100ms_bps",
        "core_return_500ms_bps",
        "core_dispersion_100ms_bps",
        "ewma_volatility_bps",
        "jump_score",
        gap_name,
    )
    return [names.index(name) for name in selected_names]


def _prepare_hmm_observations(
    matrix: np.ndarray,
    indices: list[int],
    train: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, Pipeline]:
    observations = matrix[:, indices].astype(np.float64, copy=True)
    # Magnitudes are heavy-tailed. Signed returns and the signed target/reference
    # gap stay signed; dispersion, volatility, and jump score are log-magnitudes.
    observations[:, 2:5] = np.log1p(np.maximum(observations[:, 2:5], 0))
    preprocessing = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
    ])
    train_observations = preprocessing.fit_transform(observations[train])
    all_observations = preprocessing.transform(observations)
    # Prevent isolated feed glitches from dominating diagonal Gaussian variances.
    return (
        np.clip(train_observations, -8, 8),
        np.clip(all_observations, -8, 8),
        preprocessing,
    )


def _fit_hmm(observations: np.ndarray, lengths: list[int]) -> GaussianHMM:
    model = GaussianHMM(
        n_components=3,
        covariance_type="diag",
        min_covar=0.05,
        n_iter=100,
        tol=1e-3,
        random_state=17,
        implementation="scaling",
    )
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Model is not converging")
        model.fit(observations, lengths=lengths)
    return model


def _emission_log_likelihood(model: typing.Any, observations: np.ndarray) -> np.ndarray:
    means = np.asarray(model.means_, dtype=float)
    covariance = np.asarray(
        getattr(model, "_covars_", getattr(model, "covars_", None)), dtype=float
    )
    if covariance.ndim == 3:
        covariance = np.diagonal(covariance, axis1=1, axis2=2)
    covariance = np.maximum(covariance, 1e-8)
    difference = observations[:, None, :] - means[None, :, :]
    return -0.5 * (
        np.sum(np.log(2 * np.pi * covariance), axis=1)[None, :]
        + np.sum(difference * difference / covariance[None, :, :], axis=2)
    )


def _causal_filter(
    model: typing.Any,
    observations: np.ndarray,
    times: np.ndarray,
    previous_posterior: typing.Optional[np.ndarray] = None,
    previous_time: typing.Optional[float] = None,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Filter state probabilities without looking at any future observation."""
    emissions = _emission_log_likelihood(model, observations)
    probabilities = np.empty((len(observations), len(model.startprob_)), dtype=float)
    posterior = None if previous_posterior is None else previous_posterior.copy()
    last_time = previous_time
    for index, (timestamp, emission) in enumerate(zip(times, emissions)):
        contiguous = last_time is not None and math.isclose(
            float(timestamp - last_time), EXPECTED_STEP_SECONDS, abs_tol=1e-9
        )
        prior = posterior @ model.transmat_ if contiguous else model.startprob_
        log_weight = np.log(np.maximum(prior, 1e-300)) + emission
        posterior = np.exp(log_weight - logsumexp(log_weight))
        probabilities[index] = posterior
        last_time = float(timestamp)
    return probabilities, posterior, float(last_time)


def _canonical_order(model: GaussianHMM) -> np.ndarray:
    # HMM feature 3 is standardized log EWMA volatility.  Sorting on it prevents
    # arbitrary state labels from changing between markets and folds.
    return np.argsort(model.means_[:, 3])


def _hmm_augmented_features(
    matrix: np.ndarray,
    probabilities: np.ndarray,
    names: list[str],
) -> np.ndarray:
    interaction_names = {
        "target_return_100ms_bps",
        "target_return_500ms_bps",
        "target_return_1s_bps",
        "core_return_100ms_bps",
        "core_return_500ms_bps",
        "core_return_1s_bps",
        "core_dispersion_100ms_bps",
        "ewma_volatility_bps",
        "jump_score",
        "kalman_fair_gap_bps",
        "kalman_velocity_bps",
    }
    selected = np.asarray([
        index for index, name in enumerate(names) if name in interaction_names
    ])
    entropy = -np.sum(
        probabilities * np.log(np.maximum(probabilities, 1e-12)), axis=1
    )[:, None]
    interactions = np.concatenate(
        [matrix[:, selected] * probabilities[:, state, None] for state in range(3)],
        axis=1,
    )
    return np.column_stack([matrix, probabilities, entropy, interactions])


def _mixture_ridge_predict(
    train_x: np.ndarray,
    train_y: np.ndarray,
    test_x: np.ndarray,
    train_probabilities: np.ndarray,
    test_probabilities: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    preprocessing = Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
        ("scaler", StandardScaler()),
    ])
    transformed_train = preprocessing.fit_transform(train_x)
    transformed_test = preprocessing.transform(test_x)
    test_prediction = np.zeros(len(test_x))
    train_prediction = np.zeros(len(train_x))
    for state in range(3):
        expert = Ridge(alpha=60.0).fit(
            transformed_train,
            train_y,
            sample_weight=np.maximum(train_probabilities[:, state], 0.01),
        )
        test_prediction += test_probabilities[:, state] * expert.predict(transformed_test)
        train_prediction += train_probabilities[:, state] * expert.predict(transformed_train)
    return test_prediction, train_prediction


def evaluate(
    cache_path: pathlib.Path,
    output: pathlib.Path,
    targets: tuple[tuple[str, str, str], ...],
    initial_train_hours: int,
) -> dict:
    warnings.filterwarnings("ignore", message="Skipping features without any observed values")
    logging.getLogger("hmmlearn.base").setLevel(logging.ERROR)
    cache = np.load(cache_path, allow_pickle=False)
    metadata = json.loads(str(cache["metadata"]))
    names = [str(value) for value in cache["feature_names"]]
    requested = set(targets)
    selected_markets = [
        market for market in _market_rows(cache)
        if (market["venue"], market["asset"], market["quote"]) in requested
    ]
    missing = sorted(requested - {
        (market["venue"], market["asset"], market["quote"])
        for market in selected_markets
    })
    if missing:
        raise ValueError(f"targets absent from cache: {missing}")

    model_definitions = {
        "elastic_full": _linear_pipeline(
            ElasticNet(alpha=0.01, l1_ratio=0.10, max_iter=2_000)
        ),
        "hmm_augmented_elastic": _linear_pipeline(
            ElasticNet(alpha=0.01, l1_ratio=0.10, max_iter=2_000)
        ),
    }
    fold_rows = []
    predictions: dict[tuple[str, str, str, str], list[np.ndarray]] = collections.defaultdict(list)
    outcomes: dict[tuple[str, str, str, str], list[np.ndarray]] = collections.defaultdict(list)
    regime_rows = []
    transition_rows = []

    for market in selected_markets:
        matrix = market["matrix"]
        labels = market["labels"]
        times = market["times"]
        hmm_indices = _hmm_input_indices(names, market["source_name"])
        total_hours = int(math.floor(float(times.max()) / 3600)) + 1
        for test_hour in range(initial_train_hours, total_hours):
            boundary = test_hour * 3600
            train = (times + EXPECTED_STEP_SECONDS) <= boundary
            test = (times >= boundary) & (times < (test_hour + 1) * 3600)
            train &= np.isfinite(labels)
            test &= np.isfinite(labels)
            if train.sum() < 5_000 or test.sum() < 1_000:
                continue

            train_hmm, all_hmm, _ = _prepare_hmm_observations(matrix, hmm_indices, train)
            model = _fit_hmm(train_hmm, _segment_lengths(times[train]))
            train_probabilities, final_posterior, final_time = _causal_filter(
                model, train_hmm, times[train]
            )
            test_probabilities, _, _ = _causal_filter(
                model,
                all_hmm[test],
                times[test],
                previous_posterior=final_posterior,
                previous_time=final_time,
            )
            order = _canonical_order(model)
            train_probabilities = train_probabilities[:, order]
            test_probabilities = test_probabilities[:, order]

            train_matrix, test_matrix = matrix[train], matrix[test]
            train_y, test_y = labels[train], labels[test]
            base_train = train_matrix
            base_test = test_matrix
            augmented_train = _hmm_augmented_features(
                train_matrix, train_probabilities, names
            )
            augmented_test = _hmm_augmented_features(
                test_matrix, test_probabilities, names
            )

            for model_name, estimator in model_definitions.items():
                current_train = augmented_train if model_name.startswith("hmm_") else base_train
                current_test = augmented_test if model_name.startswith("hmm_") else base_test
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=ConvergenceWarning)
                    fitted = clone(estimator).fit(current_train, train_y)
                prediction = fitted.predict(current_test)
                train_prediction = fitted.predict(current_train)
                active_threshold = float(np.quantile(np.abs(train_prediction), 0.90))
                metrics = _metrics(test_y, prediction, active_threshold)
                fold_rows.append({
                    "venue": market["venue"], "asset": market["asset"],
                    "quote": market["quote"], "model": model_name,
                    "test_hour": test_hour, **metrics,
                })
                key = market["venue"], market["asset"], market["quote"], model_name
                predictions[key].append(prediction)
                outcomes[key].append(test_y)

            mixture_prediction, mixture_train_prediction = _mixture_ridge_predict(
                train_matrix, train_y, test_matrix, train_probabilities, test_probabilities
            )
            active_threshold = float(np.quantile(np.abs(mixture_train_prediction), 0.90))
            metrics = _metrics(test_y, mixture_prediction, active_threshold)
            fold_rows.append({
                "venue": market["venue"], "asset": market["asset"],
                "quote": market["quote"], "model": "hmm_mixture_ridge",
                "test_hour": test_hour, **metrics,
            })
            key = market["venue"], market["asset"], market["quote"], "hmm_mixture_ridge"
            predictions[key].append(mixture_prediction)
            outcomes[key].append(test_y)

            hard_state = np.argmax(test_probabilities, axis=1)
            jump_values = matrix[test, names.index("jump_score")]
            volatility_values = matrix[test, names.index("ewma_volatility_bps")]
            gap_values = matrix[test, hmm_indices[-1]]
            for state, state_name in enumerate(STATE_NAMES):
                state_mask = hard_state == state
                if not state_mask.any():
                    continue
                regime_rows.append({
                    "venue": market["venue"], "asset": market["asset"],
                    "quote": market["quote"], "test_hour": test_hour,
                    "state": state_name, "observations": int(state_mask.sum()),
                    "mean_abs_future_move_bps": float(np.mean(np.abs(test_y[state_mask]))),
                    "p_abs_future_move_over_1bps": float(np.mean(np.abs(test_y[state_mask]) > 1)),
                    "mean_ewma_volatility_bps": float(np.nanmean(volatility_values[state_mask])),
                    "mean_jump_score": float(np.nanmean(jump_values[state_mask])),
                    "mean_abs_target_gap_bps": float(np.nanmean(np.abs(gap_values[state_mask]))),
                })
            canonical_transition = model.transmat_[np.ix_(order, order)]
            transition_rows.append({
                "venue": market["venue"], "asset": market["asset"],
                "quote": market["quote"], "test_hour": test_hour,
                "matrix": canonical_transition.tolist(),
                "expected_duration_seconds": [
                    EXPECTED_STEP_SECONDS / max(1 - canonical_transition[state, state], 1e-6)
                    for state in range(3)
                ],
                "converged": bool(model.monitor_.converged),
                "iterations": int(model.monitor_.iter),
            })
        print(
            f"Evaluated HMM regimes for {market['venue']} {market['asset']}-{market['quote']}",
            flush=True,
        )

    aggregates = []
    for key, prediction_parts in predictions.items():
        prediction = np.concatenate(prediction_parts)
        outcome = np.concatenate(outcomes[key])
        loss_difference = outcome * outcome - (outcome - prediction) ** 2
        relevant = [
            row for row in fold_rows
            if (row["venue"], row["asset"], row["quote"], row["model"]) == key
        ]
        statistic, p_value = _hac_test(loss_difference)
        ci_low, ci_high = _block_bootstrap_ci(loss_difference)
        aggregates.append({
            "venue": key[0], "asset": key[1], "quote": key[2], "model": key[3],
            **_metrics(outcome, prediction, 0.05),
            "folds": len(relevant),
            "positive_skill_folds": sum(row["mse_skill_pct"] > 0 for row in relevant),
            "median_fold_mse_skill_pct": statistics.median(
                row["mse_skill_pct"] for row in relevant
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
        "targets": targets,
        "protocol": {
            "states": list(STATE_NAMES),
            "observations": [names[index] for index in _hmm_input_indices(names, None)[:-1]]
            + ["candidate_target_gap_bps"],
            "fit": "three-state diagonal Gaussian HMM, fit separately on each training fold",
            "inference": "causal forward filtering only; no smoothed test-state probabilities",
            "state_order": "ascending fitted log EWMA-volatility mean",
            "important_limit": (
                "regime forecast study; quote-distance fill intensity and private fill markouts "
                "are not yet available"
            ),
        },
        "fold_results": fold_rows,
        "aggregate_results": aggregates,
        "regime_results": regime_rows,
        "transition_results": transition_rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
    return report


def _number(value: typing.Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def markdown_report(report: dict) -> str:
    lookup = {
        (row["venue"], row["asset"], row["quote"], row["model"]): row
        for row in report["aggregate_results"]
    }
    markets = sorted({key[:3] for key in lookup})
    lines = [
        "# Causal hidden-Markov regime research",
        "",
        "The HMM is refit on each training fold and test probabilities are forward-filtered. ",
        "No future test observation is used to infer the current state.",
        "",
        "## Forecast comparison",
        "",
        "| Target | Plain elastic | HMM interactions | HMM mixture | Best HMM minus plain |",
        "|---|---:|---:|---:|---:|",
    ]
    for venue, asset, quote in markets:
        plain = lookup[(venue, asset, quote, "elastic_full")]
        augmented = lookup[(venue, asset, quote, "hmm_augmented_elastic")]
        mixture = lookup[(venue, asset, quote, "hmm_mixture_ridge")]
        best_hmm = max(augmented["mse_skill_pct"], mixture["mse_skill_pct"])
        lines.append(
            f"| {venue} {asset}-{quote} | {_number(plain['mse_skill_pct'])}% "
            f"| {_number(augmented['mse_skill_pct'])}% "
            f"| {_number(mixture['mse_skill_pct'])}% "
            f"| {_number(best_hmm - plain['mse_skill_pct'])} pp |"
        )

    lines.extend([
        "",
        "## Regime characterization",
        "",
        "| Target | State | Share | Mean absolute next move | P(|move| > 1 bps) | EWMA vol | Jump score | Median duration |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    grouped: dict[tuple[str, str, str, str], list[dict]] = collections.defaultdict(list)
    for row in report["regime_results"]:
        grouped[(row["venue"], row["asset"], row["quote"], row["state"])].append(row)
    transition_grouped: dict[tuple[str, str, str], list[dict]] = collections.defaultdict(list)
    for row in report["transition_results"]:
        transition_grouped[(row["venue"], row["asset"], row["quote"])].append(row)
    for venue, asset, quote in markets:
        market_rows = [
            row for key, values in grouped.items() if key[:3] == (venue, asset, quote)
            for row in values
        ]
        total = sum(row["observations"] for row in market_rows)
        transitions = transition_grouped[(venue, asset, quote)]
        for state_index, state_name in enumerate(STATE_NAMES):
            rows = grouped.get((venue, asset, quote, state_name), [])
            if not rows:
                continue
            observations = sum(row["observations"] for row in rows)
            def weighted(field: str) -> float:
                return sum(row[field] * row["observations"] for row in rows) / observations
            duration = statistics.median(
                row["expected_duration_seconds"][state_index] for row in transitions
            )
            lines.append(
                f"| {venue} {asset}-{quote} | {state_name} "
                f"| {_number(100 * observations / total, 1)}% "
                f"| {_number(weighted('mean_abs_future_move_bps'))} bps "
                f"| {_number(100 * weighted('p_abs_future_move_over_1bps'), 1)}% "
                f"| {_number(weighted('mean_ewma_volatility_bps'))} "
                f"| {_number(weighted('mean_jump_score'))} "
                f"| {_number(duration)} s |"
            )

    lines.extend([
        "",
        "Forecast improvement is not the only valid use of an HMM. A stable stress state can ",
        "still improve quoting by widening boundaries, reducing size, pulling the toxic side, ",
        "and changing inventory limits. Those decisions require state-conditioned fill curves ",
        "and private post-fill markouts before they can be optimized.",
        "",
    ])
    return "\n".join(lines)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--targets", type=_parse_targets, default=DEFAULT_TARGETS)
    parser.add_argument("--initial-train-hours", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    report = evaluate(
        arguments.cache,
        arguments.output,
        tuple(arguments.targets),
        arguments.initial_train_hours,
    )
    print(f"Wrote {arguments.output}", flush=True)
    print(f"Wrote {arguments.output.with_suffix('.md')}", flush=True)
    print(f"Evaluated {len(report['targets'])} HMM target markets", flush=True)


if __name__ == "__main__":
    main()
