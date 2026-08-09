"""Causal walk-forward tournament for short-horizon market-making signals.

This script deliberately evaluates forecasting rather than claiming fill profitability.
Every transformation is causal, test folds are chronological, and labels do not overlap
the train/test boundary.  Model hyperparameters are fixed before seeing test results.
"""

import argparse
import collections
import datetime
import gzip
import json
import math
import pathlib
import statistics
import typing
import warnings

import numpy as np
from scipy import stats
from sklearn.base import clone
from sklearn.compose import TransformedTargetRegressor
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import ElasticNet, Ridge, SGDRegressor
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from exploratory_market_making import (
    Analyzer,
    GRID_MS,
    SOURCE_SERIES,
    VENUES,
    _files,
    _loads,
    _utc,
)


DEFAULT_TARGETS = (
    ("gate", "SOL"),
    ("gate", "XRP"),
    ("gate", "DOGE"),
    ("gate", "ADA"),
    ("gate", "BCH"),
    ("gate", "LTC"),
    ("htx", "AVAX"),
    ("htx", "HYPE"),
    ("htx", "ENA"),
    ("htx", "XLM"),
)
CORE_SOURCES = frozenset(
    ("binance_perp_usdt", "okx_perp_usdt", "bybit_perp_usdt", "mexc_perp_usdt")
)
HORIZON_BUCKETS = 5  # 500 ms
SAMPLE_BUCKETS = 5  # non-overlapping labels


def _finite_log(values: np.ndarray) -> np.ndarray:
    result = np.full(values.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(values) & (values > 0)
    result[valid] = np.log(values[valid])
    return result


def _lag_difference(values: np.ndarray, lag: int) -> np.ndarray:
    result = np.full(values.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(values[lag:]) & np.isfinite(values[:-lag])
    destination = result[lag:]
    destination[valid] = (values[lag:][valid] - values[:-lag][valid]) * 10_000
    return result


def _filled_sparse(values: typing.Optional[typing.Iterable[float]], length: int) -> np.ndarray:
    result = np.full(length, np.nan, dtype=np.float64)
    if values is None:
        return result
    source = np.asarray(values, dtype=np.float64)
    last = np.nan
    last_index = -12
    for index, value in enumerate(source):
        if np.isfinite(value):
            last, last_index = value, index
        if index - last_index <= 10:
            result[index] = last
    return result


def _ewma_variance(returns: np.ndarray, half_life_seconds: float = 5.0) -> np.ndarray:
    decay = math.exp(math.log(0.5) * (GRID_MS / 1000) / half_life_seconds)
    result = np.full(returns.shape, np.nan, dtype=np.float64)
    variance = 0.0
    initialized = False
    for index, value in enumerate(returns):
        if np.isfinite(value):
            variance = decay * variance + (1 - decay) * value * value
            initialized = True
        if initialized:
            result[index] = variance
    return result


def _causal_kalman_fair(
    log_sources: list[np.ndarray], initial_buckets: int
) -> dict[str, np.ndarray]:
    """Robust local-level Kalman filter on basis-normalized liquid source mids."""
    length = len(log_sources[0])
    anchor = log_sources[0]
    adjusted = []
    for source in log_sources:
        valid = (
            np.isfinite(source[:initial_buckets])
            & np.isfinite(anchor[:initial_buckets])
        )
        offset = np.nanmedian(source[:initial_buckets][valid] - anchor[:initial_buckets][valid]) \
            if valid.any() else 0.0
        adjusted.append(source - offset)
    matrix = np.vstack(adjusted)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        measurements = np.nanmedian(matrix, axis=0)
    initial = measurements[:initial_buckets]
    finite_initial = initial[np.isfinite(initial)]
    differences = np.diff(finite_initial)
    process_variance = max(float(np.nanmedian(differences * differences)) * 0.25, 1e-12)
    residuals = matrix[:, :initial_buckets] - measurements[:initial_buckets]
    measurement_variance = max(float(np.nanmedian(residuals * residuals)), 1e-12)

    fair = np.full(length, np.nan)
    velocity = np.full(length, np.nan)
    uncertainty = np.full(length, np.nan)
    innovation_score = np.full(length, np.nan)
    state = np.nan
    state_velocity = 0.0
    covariance = measurement_variance * 10
    for index in range(length):
        predicted = state + 0.90 * state_velocity if np.isfinite(state) else np.nan
        covariance += process_variance
        available = matrix[:, index][np.isfinite(matrix[:, index])]
        if available.size:
            observation = float(np.median(available))
            cross_variance = float(np.median((available - observation) ** 2))
            current_r = measurement_variance + cross_variance / max(available.size, 1)
            if not np.isfinite(predicted):
                predicted = observation
            innovation = observation - predicted
            innovation_variance = covariance + current_r
            score = abs(innovation) / math.sqrt(max(innovation_variance, 1e-18))
            # The cross-venue median protects common jumps; clip only extreme residual glitches.
            clipped = math.copysign(
                min(abs(innovation), 10 * math.sqrt(max(innovation_variance, 1e-18))),
                innovation,
            )
            gain = covariance / innovation_variance
            updated = predicted + gain * clipped
            state_velocity = 0.95 * state_velocity + 0.05 * (updated - state if np.isfinite(state) else 0)
            state = updated
            covariance = max((1 - gain) * covariance, 1e-18)
            innovation_score[index] = score
        elif np.isfinite(predicted):
            state = predicted
            state_velocity *= 0.90
        fair[index] = state
        velocity[index] = state_velocity * 10_000
        uncertainty[index] = math.sqrt(max(covariance, 0)) * 10_000
    return {
        "fair_log": fair,
        "velocity_bps": velocity,
        "uncertainty_bps": uncertainty,
        "innovation_score": innovation_score,
    }


def _build_dataset(
    analyzer: Analyzer,
    venue: str,
    asset: str,
    initial_train_hours: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    length = analyzer.bucket_count
    target_key = venue, asset, "USDT"
    target_mid = np.asarray(analyzer.filled(target_key), dtype=np.float64)
    target_log = _finite_log(target_mid)
    bid = _filled_sparse(analyzer.target_bids.get((venue, asset)), length)
    ask = _filled_sparse(analyzer.target_asks.get((venue, asset)), length)
    spread = np.full(length, np.nan)
    quote_valid = np.isfinite(bid) & np.isfinite(ask) & (ask > bid) & (bid > 0)
    spread[quote_valid] = (
        (ask[quote_valid] - bid[quote_valid])
        / ((ask[quote_valid] + bid[quote_valid]) / 2)
        * 10_000
    )

    source_logs: dict[str, np.ndarray] = {}
    source_returns: dict[str, dict[int, np.ndarray]] = {}
    for source_name, source_venue, source_quote in SOURCE_SERIES:
        values = np.asarray(analyzer.filled((source_venue, asset, source_quote)), dtype=np.float64)
        log_values = _finite_log(values)
        source_logs[source_name] = log_values
        source_returns[source_name] = {
            lag: _lag_difference(log_values, lag) for lag in (1, 5, 10)
        }

    core_logs = [source_logs[name] for name in SOURCE_SERIES_NAMES if name in CORE_SOURCES]
    initial_buckets = initial_train_hours * 3600 * 1000 // GRID_MS
    kalman = _causal_kalman_fair(core_logs, initial_buckets)

    core_ret_100 = np.vstack([source_returns[name][1] for name in CORE_SOURCES_ORDERED])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        composite_return_100 = np.nanmedian(core_ret_100, axis=0)
        core_dispersion_100 = np.nanmedian(
            np.abs(core_ret_100 - composite_return_100), axis=0
        )
    ewma_variance = _ewma_variance(composite_return_100)
    ewma_vol = np.sqrt(ewma_variance)
    jump_score = np.abs(composite_return_100) / np.maximum(ewma_vol, 1e-6)
    core_count = np.sum(np.isfinite(core_ret_100), axis=0).astype(float)

    feature_columns = [
        _lag_difference(target_log, 1),
        _lag_difference(target_log, 5),
        _lag_difference(target_log, 10),
        spread,
        composite_return_100,
        _quiet_nanmedian([source_returns[name][5] for name in CORE_SOURCES_ORDERED]),
        _quiet_nanmedian([source_returns[name][10] for name in CORE_SOURCES_ORDERED]),
        core_dispersion_100,
        ewma_vol,
        jump_score,
        core_count,
        (kalman["fair_log"] - target_log) * 10_000,
        kalman["velocity_bps"],
        kalman["uncertainty_bps"],
        kalman["innovation_score"],
    ]
    feature_names = [
        "target_return_100ms_bps",
        "target_return_500ms_bps",
        "target_return_1s_bps",
        "target_spread_bps",
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
    ]
    for source_name, _, _ in SOURCE_SERIES:
        feature_columns.extend(
            [
                (source_logs[source_name] - target_log) * 10_000,
                source_returns[source_name][1],
                source_returns[source_name][5],
                source_returns[source_name][10],
            ]
        )
        feature_names.extend(
            [
                f"{source_name}_gap_bps",
                f"{source_name}_return_100ms_bps",
                f"{source_name}_return_500ms_bps",
                f"{source_name}_return_1s_bps",
            ]
        )
    matrix = np.column_stack(feature_columns)
    future = np.full(length, np.nan)
    valid_future = np.isfinite(target_log[:-HORIZON_BUCKETS]) & np.isfinite(
        target_log[HORIZON_BUCKETS:]
    )
    future[:-HORIZON_BUCKETS][valid_future] = (
        target_log[HORIZON_BUCKETS:][valid_future]
        - target_log[:-HORIZON_BUCKETS][valid_future]
    ) * 10_000
    indices = np.arange(10, length - HORIZON_BUCKETS, SAMPLE_BUCKETS)
    required = (
        np.isfinite(future[indices])
        & np.isfinite(target_log[indices])
        & (core_count[indices] >= 2)
    )
    indices = indices[required]
    times = indices * GRID_MS / 1000
    return matrix[indices].astype(np.float32), future[indices].astype(np.float32), times, feature_names


SOURCE_SERIES_NAMES = tuple(item[0] for item in SOURCE_SERIES)
CORE_SOURCES_ORDERED = tuple(name for name in SOURCE_SERIES_NAMES if name in CORE_SOURCES)


def _quiet_nanmedian(values: list[np.ndarray]) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return np.nanmedian(np.vstack(values), axis=0)


def _parse_targets(value: str) -> tuple[tuple[str, str], ...]:
    targets = []
    for item in value.split(","):
        venue, asset = item.strip().split(":", maxsplit=1)
        targets.append((venue.lower(), asset.upper()))
    return tuple(targets)


def build_cache(
    run: pathlib.Path,
    start: datetime.datetime,
    end: datetime.datetime,
    targets: tuple[tuple[str, str], ...],
    initial_train_hours: int,
    cache: pathlib.Path,
) -> None:
    assets = tuple(dict.fromkeys(asset for _, asset in targets))
    analyzer = Analyzer(start, end, assets=assets)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    run_start = _utc(manifest["started_at"]).replace(minute=0, second=0, microsecond=0)
    for venue in VENUES:
        read_start = run_start if venue == "htx" else start
        paths = _files(run, venue, read_start, end)
        print(f"Reading {venue}: {len(paths)} file(s)", flush=True)
        for path in paths:
            with gzip.open(path, "rb") as input_file:
                for line in input_file:
                    analyzer.consume(venue, _loads(line))
    payload: dict[str, typing.Any] = {
        "metadata": np.asarray(json.dumps({
            "run": str(run),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "targets": targets,
            "initial_train_hours": initial_train_hours,
            "grid_ms": GRID_MS,
            "horizon_ms": HORIZON_BUCKETS * GRID_MS,
        })),
    }
    common_names = None
    for venue, asset in targets:
        print(f"Creating features for {venue} {asset}", flush=True)
        matrix, labels, times, names = _build_dataset(
            analyzer, venue, asset, initial_train_hours
        )
        prefix = f"{venue}_{asset}"
        payload[f"{prefix}_X"] = matrix
        payload[f"{prefix}_y"] = labels
        payload[f"{prefix}_time"] = times
        common_names = names
    payload["feature_names"] = np.asarray(common_names)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, **payload)
    print(f"Wrote feature cache {cache}", flush=True)


def _feature_indices(names: list[str]) -> dict[str, np.ndarray]:
    base = np.asarray([index for index, name in enumerate(names) if "_perp_" not in name and "_spot_" not in name])
    core = np.asarray([
        index for index, name in enumerate(names)
        if index in set(base) or any(name.startswith(source) for source in CORE_SOURCES)
    ])
    kalman = np.asarray([
        index for index, name in enumerate(names)
        if name in {
            "target_return_100ms_bps", "target_return_500ms_bps",
            "target_spread_bps", "ewma_volatility_bps", "jump_score",
            "kalman_fair_gap_bps", "kalman_velocity_bps",
            "kalman_uncertainty_bps", "kalman_innovation_score",
        }
    ])
    target_only = np.asarray([
        index for index, name in enumerate(names)
        if name.startswith("target_return_") or name == "target_spread_bps"
    ])
    source_only = np.asarray([
        index for index, name in enumerate(names)
        if not name.startswith("target_return_") and name != "target_spread_bps"
    ])
    return {
        "kalman": kalman,
        "target_only": target_only,
        "source_only": source_only,
        "core": core,
        "expanded": np.arange(len(names)),
    }


def _linear_pipeline(estimator) -> Pipeline:
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
        ("scaler", StandardScaler()),
        ("model", estimator),
    ])


def _models() -> dict[str, tuple[str, typing.Any]]:
    return {
        "ridge_target_only": ("target_only", _linear_pipeline(Ridge(alpha=10.0))),
        "ridge_source_only": ("source_only", _linear_pipeline(Ridge(alpha=50.0))),
        "elastic_source_only": (
            "source_only",
            _linear_pipeline(ElasticNet(alpha=0.01, l1_ratio=0.10, max_iter=2_000)),
        ),
        "kalman_ridge": ("kalman", _linear_pipeline(Ridge(alpha=20.0))),
        "ridge_core": ("core", _linear_pipeline(Ridge(alpha=30.0))),
        "ridge_expanded": ("expanded", _linear_pipeline(Ridge(alpha=60.0))),
        "elastic_net": (
            "expanded",
            _linear_pipeline(ElasticNet(alpha=0.01, l1_ratio=0.10, max_iter=2_000)),
        ),
        "robust_sgd": (
            "core",
            _linear_pipeline(SGDRegressor(
                loss="huber", epsilon=1.35, penalty="elasticnet", alpha=0.001,
                l1_ratio=0.05, max_iter=2_000, tol=1e-5, random_state=7,
            )),
        ),
        "hist_gradient_boosting": (
            "expanded",
            Pipeline([
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                ("model", HistGradientBoostingRegressor(
                    learning_rate=0.05, max_iter=100, max_leaf_nodes=15,
                    min_samples_leaf=100, l2_regularization=10.0,
                    early_stopping=False, random_state=7,
                )),
            ]),
        ),
        "extra_trees": (
            "expanded",
            Pipeline([
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                ("model", ExtraTreesRegressor(
                    n_estimators=60, max_depth=10, min_samples_leaf=50,
                    max_features=0.70, n_jobs=-1, random_state=7,
                )),
            ]),
        ),
        "neural_mlp": (
            "expanded",
            TransformedTargetRegressor(
                regressor=_linear_pipeline(MLPRegressor(
                    hidden_layer_sizes=(32, 16), activation="tanh", alpha=0.02,
                    learning_rate_init=0.001, max_iter=100, early_stopping=False,
                    random_state=7,
                )),
                transformer=StandardScaler(),
            ),
        ),
    }


def _metrics(y: np.ndarray, prediction: np.ndarray, active_threshold: float) -> dict:
    baseline_mse = float(np.mean(y * y))
    mse = float(np.mean((y - prediction) ** 2))
    active = (np.abs(prediction) >= max(active_threshold, 0.05)) & (np.abs(y) >= 0.05)
    correlation = float(np.corrcoef(y, prediction)[0, 1]) \
        if np.std(prediction) > 0 and np.std(y) > 0 else None
    return {
        "observations": int(len(y)),
        "rmse_bps": math.sqrt(mse),
        "baseline_rmse_bps": math.sqrt(baseline_mse),
        "rmse_improvement_pct": (1 - math.sqrt(mse / baseline_mse)) * 100
        if baseline_mse else 0.0,
        "mse_skill_pct": (1 - mse / baseline_mse) * 100 if baseline_mse else 0.0,
        "correlation": correlation,
        "active_predictions": int(active.sum()),
        "active_direction_accuracy": float(np.mean(prediction[active] * y[active] > 0))
        if active.any() else None,
        "mean_active_aligned_move_bps": float(np.mean(np.sign(prediction[active]) * y[active]))
        if active.any() else None,
    }


def _prequential_rls(
    train_x: np.ndarray,
    train_y: np.ndarray,
    test_x: np.ndarray,
    test_y: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    imputer = SimpleImputer(strategy="median", add_indicator=True)
    scaler = StandardScaler()
    transformed_train = scaler.fit_transform(imputer.fit_transform(train_x))
    transformed_test = scaler.transform(imputer.transform(test_x))
    transformed_train = np.column_stack([np.ones(len(transformed_train)), transformed_train])
    transformed_test = np.column_stack([np.ones(len(transformed_test)), transformed_test])
    ridge = Ridge(alpha=30.0, fit_intercept=False).fit(transformed_train, train_y)
    theta = ridge.coef_.astype(float)
    covariance = np.eye(len(theta)) * 0.05
    forgetting = 0.9995
    predictions = np.empty(len(test_y))
    for index, features in enumerate(transformed_test):
        if index:
            previous_features = transformed_test[index - 1]
            previous_error = test_y[index - 1] - previous_features @ theta
            scale = max(float(np.std(train_y)), 0.1)
            previous_error = float(np.clip(previous_error, -5 * scale, 5 * scale))
            denominator = forgetting + previous_features @ covariance @ previous_features
            gain = covariance @ previous_features / denominator
            theta += gain * previous_error
            covariance = (
                covariance - np.outer(gain, previous_features) @ covariance
            ) / forgetting
        predictions[index] = features @ theta
    return predictions, transformed_train @ ridge.coef_


def _regime_ridge(
    train_x: np.ndarray,
    train_y: np.ndarray,
    test_x: np.ndarray,
    jump_train: np.ndarray,
    jump_test: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    positive_jumps = jump_train[np.isfinite(jump_train) & (jump_train > 0)]
    threshold = float(np.quantile(positive_jumps, 0.80)) \
        if positive_jumps.size >= 500 else math.inf
    stressed = np.isfinite(jump_train) & (jump_train >= threshold)
    calm = ~stressed
    common = _linear_pipeline(Ridge(alpha=30.0)).fit(train_x, train_y)
    calm_model = clone(common).fit(train_x[calm], train_y[calm]) if calm.sum() >= 500 else common
    stress_model = clone(common).fit(train_x[stressed], train_y[stressed]) if stressed.sum() >= 500 else common
    prediction = common.predict(test_x)
    test_stressed = np.isfinite(jump_test) & (jump_test >= threshold)
    if (~test_stressed).any():
        prediction[~test_stressed] = calm_model.predict(test_x[~test_stressed])
    if test_stressed.any():
        prediction[test_stressed] = stress_model.predict(test_x[test_stressed])
    train_prediction = common.predict(train_x)
    return prediction, train_prediction


def _hac_test(loss_difference: np.ndarray, lag: int = 20) -> tuple[float, float]:
    centered = loss_difference - np.mean(loss_difference)
    count = len(centered)
    long_run = float(np.dot(centered, centered) / count)
    for offset in range(1, min(lag, count - 1) + 1):
        covariance = float(np.dot(centered[offset:], centered[:-offset]) / count)
        long_run += 2 * (1 - offset / (lag + 1)) * covariance
    standard_error = math.sqrt(max(long_run, 1e-18) / count)
    statistic = float(np.mean(loss_difference) / standard_error)
    return statistic, float(stats.norm.sf(statistic))


def _block_bootstrap_ci(
    values: np.ndarray, block: int = 120, repetitions: int = 500
) -> tuple[float, float]:
    rng = np.random.default_rng(19)
    blocks = [values[index : index + block] for index in range(0, len(values), block)]
    means = np.empty(repetitions)
    for repetition in range(repetitions):
        selected = rng.integers(0, len(blocks), len(blocks))
        means[repetition] = np.mean(np.concatenate([blocks[index] for index in selected]))
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def _benjamini_hochberg(rows: list[dict]) -> None:
    ordered = sorted(enumerate(rows), key=lambda pair: pair[1]["hac_p_value"])
    count = len(ordered)
    running = 1.0
    for reverse_rank, (index, row) in enumerate(reversed(ordered), start=1):
        rank = count - reverse_rank + 1
        running = min(running, row["hac_p_value"] * count / rank)
        rows[index]["bh_q_value"] = running


def evaluate_cache(
    cache_path: pathlib.Path,
    output: pathlib.Path,
    initial_train_hours: int,
) -> dict:
    warnings.filterwarnings(
        "ignore", message="Skipping features without any observed values"
    )
    cache = np.load(cache_path, allow_pickle=False)
    metadata = json.loads(str(cache["metadata"]))
    names = [str(value) for value in cache["feature_names"]]
    indices = _feature_indices(names)
    jump_index = names.index("jump_score")
    model_definitions = _models()
    targets = [tuple(item) for item in metadata["targets"]]
    fold_rows = []
    prediction_store: dict[tuple[str, str, str], list[np.ndarray]] = collections.defaultdict(list)
    outcome_store: dict[tuple[str, str, str], list[np.ndarray]] = collections.defaultdict(list)
    for venue, asset in targets:
        prefix = f"{venue}_{asset}"
        matrix, labels, times = cache[f"{prefix}_X"], cache[f"{prefix}_y"], cache[f"{prefix}_time"]
        total_hours = int(math.floor(float(times.max()) / 3600)) + 1
        for test_hour in range(initial_train_hours, total_hours):
            boundary = test_hour * 3600
            train = (times + HORIZON_BUCKETS * GRID_MS / 1000) <= boundary
            test = (times >= boundary) & (times < (test_hour + 1) * 3600)
            if train.sum() < 5_000 or test.sum() < 1_000:
                continue
            for model_name, (feature_set, estimator) in model_definitions.items():
                selected = indices[feature_set]
                train_x, test_x = matrix[train][:, selected], matrix[test][:, selected]
                train_y, test_y = labels[train], labels[test]
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
                fold_rows.append({
                    "venue": venue, "asset": asset, "model": model_name,
                    "test_hour": test_hour, "feature_set": feature_set,
                    "active_threshold_bps": active_threshold, **metrics,
                })
                key = venue, asset, model_name
                prediction_store[key].append(np.asarray(prediction))
                outcome_store[key].append(test_y)

            selected = indices["core"]
            train_x, test_x = matrix[train][:, selected], matrix[test][:, selected]
            rls_prediction, rls_train_prediction = _prequential_rls(
                train_x, labels[train], test_x, labels[test]
            )
            active_threshold = float(np.quantile(np.abs(rls_train_prediction), 0.90))
            metrics = _metrics(labels[test], rls_prediction, active_threshold)
            fold_rows.append({
                "venue": venue, "asset": asset, "model": "adaptive_rls",
                "test_hour": test_hour, "feature_set": "core",
                "active_threshold_bps": active_threshold, **metrics,
            })
            key = venue, asset, "adaptive_rls"
            prediction_store[key].append(rls_prediction)
            outcome_store[key].append(labels[test])

            regime_prediction, regime_train_prediction = _regime_ridge(
                train_x, labels[train], test_x,
                matrix[train, jump_index], matrix[test, jump_index],
            )
            active_threshold = float(np.quantile(np.abs(regime_train_prediction), 0.90))
            metrics = _metrics(labels[test], regime_prediction, active_threshold)
            fold_rows.append({
                "venue": venue, "asset": asset, "model": "regime_ridge",
                "test_hour": test_hour, "feature_set": "core",
                "active_threshold_bps": active_threshold, **metrics,
            })
            key = venue, asset, "regime_ridge"
            prediction_store[key].append(regime_prediction)
            outcome_store[key].append(labels[test])
        print(f"Evaluated {venue} {asset}", flush=True)

    aggregates = []
    for key, predictions in prediction_store.items():
        venue, asset, model = key
        prediction = np.concatenate(predictions)
        outcome = np.concatenate(outcome_store[key])
        loss_difference = outcome * outcome - (outcome - prediction) ** 2
        fold_subset = [
            row for row in fold_rows
            if (row["venue"], row["asset"], row["model"]) == key
        ]
        statistic, p_value = _hac_test(loss_difference)
        ci_low, ci_high = _block_bootstrap_ci(loss_difference)
        aggregate_metrics = _metrics(outcome, prediction, 0.05)
        aggregates.append({
            "venue": venue,
            "asset": asset,
            "model": model,
            **aggregate_metrics,
            "folds": len(fold_subset),
            "positive_skill_folds": sum(row["mse_skill_pct"] > 0 for row in fold_subset),
            "median_fold_mse_skill_pct": statistics.median(
                row["mse_skill_pct"] for row in fold_subset
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
            "label": "target mid return over the next 500 ms",
            "sampling": "one non-overlapping observation every 500 ms",
            "split": (
                f"expanding train starting with {initial_train_hours} hours, then one-hour "
                "chronological test folds; train labels ending after the boundary are purged"
            ),
            "transformations": "imputation and scaling fit separately inside each train fold",
            "hyperparameters": "fixed before test evaluation; no search on test folds",
            "uncertainty": (
                "Newey-West/HAC one-sided test and 60-second block-bootstrap confidence "
                "interval for squared-loss improvement"
            ),
            "multiplicity": "Benjamini-Hochberg correction across all market/model tests",
            "important_limit": "forecasting study only; no queue-conditioned fill PnL",
        },
        "feature_names": names,
        "fold_results": fold_rows,
        "aggregate_results": aggregates,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as output_file:
        json.dump(report, output_file, indent=2, allow_nan=False)
        output_file.write("\n")
    output.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
    return report


def _number(value: typing.Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def markdown_report(report: dict) -> str:
    lines = [
        "# Short-horizon model research tournament",
        "",
        f"Window: `{report['metadata']['start']}` to `{report['metadata']['end']}`. "
        f"Horizon: {report['metadata']['horizon_ms']} ms.",
        "",
        "All results are genuinely later chronological observations. No model was tuned on the ",
        "reported test folds. Positive forecast skill is not equivalent to passive-fill profit.",
        "",
        "## Strongest aggregate results",
        "",
        "| Rank | Target | Model | MSE skill | Median fold skill | Positive folds | HAC p | BH q | Bootstrap CI | Active accuracy |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(report["aggregate_results"][:40], start=1):
        ci = row["block_bootstrap_loss_difference_ci"]
        lines.append(
            f"| {rank} | {row['venue']} {row['asset']} | {row['model']} "
            f"| {_number(row['mse_skill_pct'])}% "
            f"| {_number(row['median_fold_mse_skill_pct'])}% "
            f"| {row['positive_skill_folds']}/{row['folds']} "
            f"| {_number(row['hac_p_value'], 4)} "
            f"| {_number(row['bh_q_value'], 4)} "
            f"| [{_number(ci[0], 4)}, {_number(ci[1], 4)}] "
            f"| {_number(100 * row['active_direction_accuracy'] if row['active_direction_accuracy'] is not None else None, 1)}% |"
        )
    lines.extend([
        "",
        "A candidate is provisionally credible only when it has positive skill in most folds, ",
        "a positive bootstrap interval, and a small multiplicity-adjusted q-value. Even then it ",
        "must survive fill-conditioned markouts and fees.",
        "",
        "## Best model by target",
        "",
        "| Target | Best model | MSE skill | Positive folds | BH q | Active calls | Mean aligned move |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    targets = {}
    for row in report["aggregate_results"]:
        targets.setdefault((row["venue"], row["asset"]), row)
    for (venue, asset), row in targets.items():
        lines.append(
            f"| {venue} {asset} | {row['model']} | {_number(row['mse_skill_pct'])}% "
            f"| {row['positive_skill_folds']}/{row['folds']} "
            f"| {_number(row['bh_q_value'], 4)} "
            f"| {row['active_predictions']:,} "
            f"| {_number(row['mean_active_aligned_move_bps'])} bps |"
        )
    lookup = {
        (row["venue"], row["asset"], row["model"]): row
        for row in report["aggregate_results"]
    }
    lines.extend([
        "",
        "## Target-history versus external-source ablation",
        "",
        "| Target | Target-only skill | External-source-only skill | Full elastic-net skill | Interpretation |",
        "|---|---:|---:|---:|---|",
    ])
    for venue, asset in targets:
        target_only = lookup[(venue, asset, "ridge_target_only")]["mse_skill_pct"]
        source_only = lookup[(venue, asset, "elastic_source_only")]["mse_skill_pct"]
        full = lookup[(venue, asset, "elastic_net")]["mse_skill_pct"]
        interpretation = (
            "mostly external lead/dislocation" if source_only > max(target_only, 0) * 2
            else "mixed target persistence and external signal"
        )
        lines.append(
            f"| {venue} {asset} | {_number(target_only)}% | {_number(source_only)}% "
            f"| {_number(full)}% | {interpretation} |"
        )
    lines.extend([
        "",
        "## Model families",
        "",
        "- `kalman_ridge`: robust latent-fair gap, velocity, uncertainty and jump innovations.",
        "- `ridge_target_only`: target-book autoregression/bid-ask-bounce control.",
        "- `*_source_only`: liquid-venue signals and current dislocation without target return lags.",
        "- `ridge_*` / `elastic_net`: regularized linear baselines.",
        "- `adaptive_rls`: causal online coefficient updates with delayed labels.",
        "- `regime_ridge`: separate calm and high-jump linear models.",
        "- `hist_gradient_boosting` / `extra_trees`: nonlinear tree challengers.",
        "- `neural_mlp`: small regularized feed-forward neural network on lagged features.",
        "- `robust_sgd`: Huber-loss linear model resistant to large moves.",
        "",
        "The dataset spans only part of one day. Statistical confidence describes this capture, ",
        "not unseen weeks or volatility regimes; durable promotion requires multi-day repetition.",
        "",
    ])
    return "\n".join(lines)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=pathlib.Path, required=True)
    parser.add_argument("--start", type=_utc, required=True)
    parser.add_argument("--end", type=_utc, required=True)
    parser.add_argument("--cache", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--initial-train-hours", type=int, default=3)
    parser.add_argument(
        "--targets", type=_parse_targets,
        default=DEFAULT_TARGETS,
        help="comma-separated venue:asset pairs",
    )
    parser.add_argument("--reuse-cache", action="store_true")
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    if not arguments.reuse_cache or not arguments.cache.exists():
        build_cache(
            arguments.run, arguments.start, arguments.end, tuple(arguments.targets),
            arguments.initial_train_hours, arguments.cache,
        )
    report = evaluate_cache(
        arguments.cache, arguments.output, arguments.initial_train_hours
    )
    print(f"Wrote {arguments.output}", flush=True)
    print(f"Wrote {arguments.output.with_suffix('.md')}", flush=True)
    credible = [
        row for row in report["aggregate_results"]
        if row["bh_q_value"] < 0.10
        and row["positive_skill_folds"] >= max(row["folds"] - 1, 1)
        and row["block_bootstrap_loss_difference_ci"][0] > 0
    ]
    print(
        f"Within-capture statistically repeatable model/market pairs: {len(credible)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
