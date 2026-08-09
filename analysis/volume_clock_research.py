"""Compare causal turnover-normalized activity bars with matched fixed-time bars.

Trade notional is reconstructed from raw public participant trades.  Each asset and
candidate target uses the same requested number of bars per hour; its dollar threshold
is frozen from only the initial training window.  This creates a common normalized
activity scale without using future turnover or comparing incomparable base quantities.
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
from sklearn.base import clone
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import Ridge

from cross_venue_target_research import _market_rows
from exploratory_market_making import _files, _loads
from model_research import (
    GRID_MS,
    HORIZON_BUCKETS,
    _benjamini_hochberg,
    _block_bootstrap_ci,
    _hac_test,
    _linear_pipeline,
    _metrics,
)


ACTIVITY_BUCKET_MS = 500
ACTIVITY_VENUES = (
    "binance", "okx", "bybit", "mexc", "hyperliquid", "gate", "htx"
)
CLOCK_VENUES = ("binance", "okx", "bybit", "hyperliquid")
DEFAULT_TARGETS = (
    ("mexc", "SOL", "USDC"),
    ("mexc", "SOL", "USDT"),
    ("gate", "SOL", "USDT"),
    ("mexc", "DOGE", "USDT"),
    ("mexc", "BCH", "USDT"),
    ("mexc", "ADA", "USDT"),
    ("htx", "ENA", "USDT"),
    ("htx", "XLM", "USDT"),
)
DEFAULT_BARS_PER_HOUR = (120, 360, 720)


def _parse_targets(value: str) -> tuple[tuple[str, str, str], ...]:
    result = []
    for item in value.split(","):
        venue, asset, quote = item.strip().split(":", maxsplit=2)
        result.append((venue.lower(), asset.upper(), quote.upper()))
    return tuple(result)


def _parse_integers(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in value.split(","))


def _contract_multiplier(venue: str, record: dict) -> dict[tuple[str, str, str], float]:
    payload = record.get("payload", {})
    result = {}
    try:
        if venue == "gate":
            base, quote = record.get("base_asset"), record.get("quote_asset")
            if base and quote:
                result[(venue, base, quote)] = float(payload["quanto_multiplier"])
        elif venue == "okx":
            base, quote = record.get("base_asset"), record.get("quote_asset")
            data = payload.get("data") or []
            if base and quote and data:
                result[(venue, base, quote)] = float(data[0]["ctVal"])
        elif venue == "mexc":
            for item in payload.get("data") or ():
                symbol = str(item.get("symbol", ""))
                if "_" in symbol:
                    base, quote = symbol.rsplit("_", 1)
                    result[(venue, base, quote)] = float(item["contractSize"])
    except (KeyError, TypeError, ValueError):
        return {}
    return result


def _preload_multipliers(
    run: pathlib.Path,
    start: datetime.datetime,
    assets: frozenset[str],
) -> dict[tuple[str, str, str], float]:
    multipliers: dict[tuple[str, str, str], float] = {}
    scan_end = start + datetime.timedelta(hours=1)
    for venue in ("gate", "okx", "mexc"):
        for path in _files(run, venue, start, scan_end):
            with gzip.open(path, "rb") as input_file:
                for line in input_file:
                    record = _loads(line)
                    if (
                        record.get("record_type") == "rest"
                        and record.get("stream") == "rest.contract_info"
                    ):
                        multipliers.update(_contract_multiplier(venue, record))
    return {
        key: value for key, value in multipliers.items()
        if key[1] in assets
    }


def _trade_rows_and_sign(
    venue: str, record: dict
) -> typing.Iterable[tuple[float, float, float]]:
    """Yield price, raw quantity, and aggressor sign (+buy/-sell)."""
    payload = record.get("payload", {})
    stream = str(record.get("stream", ""))
    try:
        if venue == "binance" and stream == "aggTrade":
            yield float(payload["p"]), float(payload["q"]), -1.0 if payload.get("m") else 1.0
        elif venue == "okx" and stream == "trades":
            for row in payload.get("data") or ():
                yield float(row["px"]), float(row["sz"]), 1.0 if row.get("side") == "buy" else -1.0
        elif venue == "bybit" and stream.startswith("publicTrade."):
            for row in payload.get("data") or ():
                yield float(row["p"]), float(row["v"]), 1.0 if row.get("S") == "Buy" else -1.0
        elif venue == "mexc" and stream == "push.deal":
            for row in payload.get("data") or ():
                side = 1.0 if int(row.get("T", 0)) == 1 else -1.0
                yield float(row["p"]), float(row["v"]), side
        elif venue == "hyperliquid" and stream == "trades":
            for row in payload.get("data") or ():
                yield float(row["px"]), float(row["sz"]), 1.0 if row.get("side") == "B" else -1.0
        elif venue == "gate" and stream == "futures.trades":
            for row in payload.get("result") or ():
                size = float(row["size"])
                yield float(row["price"]), abs(size), 1.0 if size > 0 else -1.0
        elif venue == "htx" and stream.endswith(".trade.detail"):
            for row in payload.get("tick", {}).get("data") or ():
                turnover = row.get("trade_turnover")
                quantity = float(turnover) / float(row["price"]) if turnover is not None else float(row.get("amount", 0))
                yield float(row["price"]), quantity, 1.0 if row.get("direction") == "buy" else -1.0
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return


def build_activity_cache(
    run: pathlib.Path,
    start: datetime.datetime,
    end: datetime.datetime,
    assets: tuple[str, ...],
    output: pathlib.Path,
) -> None:
    asset_set = frozenset(assets)
    bucket_count = int((end - start).total_seconds() * 1_000) // ACTIVITY_BUCKET_MS
    start_ns = int(start.timestamp() * 1e9)
    end_ns = int(end.timestamp() * 1e9)
    multipliers = _preload_multipliers(run, start, asset_set)
    volume = {
        (venue, asset): np.zeros(bucket_count, dtype=np.float64)
        for venue in ACTIVITY_VENUES for asset in assets
    }
    signed = {
        key: np.zeros(bucket_count, dtype=np.float64) for key in volume
    }
    counts = {
        key: np.zeros(bucket_count, dtype=np.int32) for key in volume
    }
    records = collections.Counter()
    for venue in ACTIVITY_VENUES:
        paths = _files(run, venue, start, end)
        print(f"Reading participant trades from {venue}: {len(paths)} file(s)", flush=True)
        for path in paths:
            with gzip.open(path, "rb") as input_file:
                for line in input_file:
                    record = _loads(line)
                    if record.get("record_type") != "market":
                        continue
                    asset = record.get("base_asset")
                    quote = record.get("quote_asset")
                    timestamp_ns = int(record.get("received_ts_ns") or 0)
                    if asset not in asset_set or not quote or not start_ns <= timestamp_ns < end_ns:
                        continue
                    bucket = (timestamp_ns - start_ns) // (ACTIVITY_BUCKET_MS * 1_000_000)
                    multiplier = multipliers.get((venue, asset, quote), 1.0)
                    for price, quantity, side in _trade_rows_and_sign(venue, record):
                        notional = abs(price * quantity * multiplier)
                        volume[(venue, asset)][bucket] += notional
                        signed[(venue, asset)][bucket] += side * notional
                        counts[(venue, asset)][bucket] += 1
                        records[(venue, asset)] += 1
    payload: dict[str, typing.Any] = {
        "metadata": np.asarray(json.dumps({
            "run": str(run), "start": start.isoformat(), "end": end.isoformat(),
            "assets": assets, "venues": ACTIVITY_VENUES,
            "bucket_ms": ACTIVITY_BUCKET_MS,
            "multipliers": {"|".join(key): value for key, value in multipliers.items()},
            "trade_records": {"|".join(key): value for key, value in records.items()},
        }))
    }
    for (venue, asset), values in volume.items():
        prefix = f"{venue}_{asset}"
        payload[f"{prefix}_volume"] = values.astype(np.float32)
        payload[f"{prefix}_signed"] = signed[(venue, asset)].astype(np.float32)
        payload[f"{prefix}_count"] = counts[(venue, asset)]
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output, **payload)
    print(f"Wrote activity cache {output}", flush=True)


def _activity_arrays(
    activity: np.lib.npyio.NpzFile,
    asset: str,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, np.ndarray]]:
    volumes = {venue: activity[f"{venue}_{asset}_volume"] for venue in ACTIVITY_VENUES}
    signed = {venue: activity[f"{venue}_{asset}_signed"] for venue in ACTIVITY_VENUES}
    counts = {venue: activity[f"{venue}_{asset}_count"] for venue in ACTIVITY_VENUES}
    return volumes, signed, counts


def _volume_closes(volume: np.ndarray, threshold: float) -> list[int]:
    closes = []
    accumulated = 0.0
    for bucket, value in enumerate(volume):
        accumulated += float(value)
        if accumulated >= threshold:
            closes.append(bucket)
            accumulated = 0.0
    return closes


def _time_closes(bucket_count: int, bars_per_hour: int) -> list[int]:
    interval = max(1, int(round(3600_000 / ACTIVITY_BUCKET_MS / bars_per_hour)))
    return list(range(interval - 1, bucket_count, interval))


def _mapped_closes(
    closes: list[int], row_buckets: np.ndarray
) -> list[tuple[int, int]]:
    """Map a completed activity bucket to price state at its right-hand boundary."""
    result = []
    last_position = -1
    for close in closes:
        # Bucket ``close`` contains receipts in [close, close + 1).  Its flow is
        # observable only at the right-hand boundary, so a row at ``close``
        # would leak within-bucket events into the subsequent return label.
        position = int(np.searchsorted(row_buckets, close + 1, side="right") - 1)
        if position > last_position:
            result.append((close, position))
            last_position = position
    return result


def _prefix(values: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(values, dtype=np.float64)])


def _range_sum(prefix: np.ndarray, start: int, end_inclusive: int) -> float:
    return float(prefix[end_inclusive + 1] - prefix[start])


def _build_bar_dataset(
    market: dict,
    clock: str,
    bars_per_hour: int,
    initial_train_hours: int,
    volumes: dict[str, np.ndarray],
    signed: dict[str, np.ndarray],
    counts: dict[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    matrix = market["matrix"]
    labels = market["labels"]
    times = market["times"]
    row_buckets = np.floor(times * 1_000 / ACTIVITY_BUCKET_MS).astype(int)
    clock_volume = np.sum([volumes[venue] for venue in CLOCK_VENUES], axis=0)
    initial_buckets = initial_train_hours * 3600_000 // ACTIVITY_BUCKET_MS
    initial_notional = float(np.sum(clock_volume[:initial_buckets]))
    threshold = initial_notional / max(initial_train_hours * bars_per_hour, 1)
    if not np.isfinite(threshold) or threshold <= 0:
        raise ValueError(f"no initial reference turnover for {market['asset']}")
    closes = (
        _volume_closes(clock_volume, threshold)
        if clock == "volume"
        else _time_closes(len(clock_volume), bars_per_hour)
    )
    mapped = _mapped_closes(closes, row_buckets)
    volume_prefix = {venue: _prefix(values) for venue, values in volumes.items()}
    signed_prefix = {venue: _prefix(values) for venue, values in signed.items()}
    count_prefix = {venue: _prefix(values) for venue, values in counts.items()}
    label_prefix = _prefix(labels)
    gaps = np.concatenate([
        [0],
        np.cumsum(~np.isclose(np.diff(times), HORIZON_BUCKETS * GRID_MS / 1000, atol=1e-9)),
    ])

    features, outcomes, output_times, outcome_end_times = [], [], [], []
    durations = []
    for bar_index in range(1, len(mapped) - 1):
        previous_close, previous_position = mapped[bar_index - 1]
        current_close, current_position = mapped[bar_index]
        _, next_position = mapped[bar_index + 1]
        if not previous_position < current_position < next_position:
            continue
        if (
            gaps[current_position] != gaps[previous_position]
            or gaps[next_position] != gaps[current_position]
        ):
            continue
        start_bucket = previous_close + 1
        if start_bucket > current_close:
            continue
        venue_volume = {
            venue: _range_sum(volume_prefix[venue], start_bucket, current_close)
            for venue in ACTIVITY_VENUES
        }
        venue_signed = {
            venue: _range_sum(signed_prefix[venue], start_bucket, current_close)
            for venue in ACTIVITY_VENUES
        }
        venue_count = {
            venue: _range_sum(count_prefix[venue], start_bucket, current_close)
            for venue in ACTIVITY_VENUES
        }
        total_volume = sum(venue_volume.values())
        total_count = sum(venue_count.values())
        external_bar_volume = sum(venue_volume[venue] for venue in CLOCK_VENUES)
        external_signed = sum(venue_signed[venue] for venue in CLOCK_VENUES)
        duration = (current_close - previous_close) * ACTIVITY_BUCKET_MS / 1_000
        prior_return = _range_sum(label_prefix, previous_position, current_position - 1)
        future_return = _range_sum(label_prefix, current_position, next_position - 1)
        flow_features = [
            prior_return,
            math.log1p(duration),
            math.log1p(total_volume),
            math.log1p(total_count),
            math.log1p(total_volume / total_count) if total_count else 0.0,
            external_signed / external_bar_volume if external_bar_volume else 0.0,
            external_bar_volume / threshold,
        ]
        for venue in ACTIVITY_VENUES:
            flow_features.extend([
                venue_volume[venue] / total_volume if total_volume else 0.0,
                venue_signed[venue] / venue_volume[venue] if venue_volume[venue] else 0.0,
            ])
        features.append(np.concatenate([matrix[current_position], flow_features]))
        outcomes.append(future_return)
        output_times.append(times[current_position])
        outcome_end_times.append(times[next_position])
        durations.append(duration)
    return (
        np.asarray(features, dtype=np.float32),
        np.asarray(outcomes, dtype=np.float32),
        np.asarray(output_times, dtype=np.float64),
        np.asarray(outcome_end_times, dtype=np.float64),
        {
            "threshold_usd": threshold,
            "initial_external_turnover_usd_per_hour": initial_notional / initial_train_hours,
            "bars": len(features),
            "median_duration_seconds": statistics.median(durations) if durations else None,
            "p10_duration_seconds": float(np.quantile(durations, 0.10)) if durations else None,
            "p90_duration_seconds": float(np.quantile(durations, 0.90)) if durations else None,
            "base_feature_count": matrix.shape[1],
        },
    )


def _walk_forward_masks(
    times: np.ndarray,
    outcome_end_times: np.ndarray,
    test_hour: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Create causal folds and purge labels that cross either hourly boundary."""
    boundary = test_hour * 3600
    test_end = (test_hour + 1) * 3600
    train = outcome_end_times <= boundary
    test = (
        (times >= boundary)
        & (times < test_end)
        & (outcome_end_times <= test_end)
    )
    return train, test


def evaluate(
    feature_cache_path: pathlib.Path,
    activity_cache_path: pathlib.Path,
    output: pathlib.Path,
    targets: tuple[tuple[str, str, str], ...],
    bars_per_hour_values: tuple[int, ...],
    initial_train_hours: int,
) -> dict:
    feature_cache = np.load(feature_cache_path, allow_pickle=False)
    activity = np.load(activity_cache_path, allow_pickle=False)
    feature_metadata = json.loads(str(feature_cache["metadata"]))
    activity_metadata = json.loads(str(activity["metadata"]))
    start = datetime.datetime.fromisoformat(feature_metadata["start"])
    end = datetime.datetime.fromisoformat(feature_metadata["end"])
    total_hours = int((end - start).total_seconds() // 3600)
    expected_test_folds = total_hours - initial_train_hours
    warnings.filterwarnings(
        "ignore", message="Skipping features without any observed values"
    )
    requested = set(targets)
    markets = [
        market for market in _market_rows(feature_cache)
        if (market["venue"], market["asset"], market["quote"]) in requested
    ]
    dataset_metadata = []
    fold_rows = []
    predictions: dict[tuple[str, str, str, str, int, str], list[np.ndarray]] = collections.defaultdict(list)
    outcomes: dict[tuple[str, str, str, str, int, str], list[np.ndarray]] = collections.defaultdict(list)
    estimator = _linear_pipeline(Ridge(alpha=100.0))

    for market in markets:
        volumes, signed, counts = _activity_arrays(activity, market["asset"])
        market_key = market["venue"], market["asset"], market["quote"]
        for bars_per_hour in bars_per_hour_values:
            for clock in ("volume", "time"):
                matrix, labels, times, outcome_end_times, dataset_info = _build_bar_dataset(
                    market, clock, bars_per_hour, initial_train_hours,
                    volumes, signed, counts,
                )
                dataset_metadata.append({
                    "venue": market_key[0], "asset": market_key[1],
                    "quote": market_key[2], "clock": clock,
                    "bars_per_hour": bars_per_hour, **dataset_info,
                })
                for test_hour in range(initial_train_hours, total_hours):
                    train, test = _walk_forward_masks(
                        times, outcome_end_times, test_hour
                    )
                    if train.sum() < 200 or test.sum() < 30:
                        continue
                    for feature_set in ("price_only", "price_and_flow"):
                        selected = (
                            matrix[:, : dataset_info["base_feature_count"]]
                            if feature_set == "price_only" else matrix
                        )
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", category=ConvergenceWarning)
                            warnings.filterwarnings(
                                "ignore", message="Skipping features without any observed values"
                            )
                            fitted = clone(estimator).fit(selected[train], labels[train])
                        prediction = fitted.predict(selected[test])
                        train_prediction = fitted.predict(selected[train])
                        active_threshold = float(np.quantile(np.abs(train_prediction), 0.90))
                        metrics = _metrics(labels[test], prediction, active_threshold)
                        fold_rows.append({
                            "venue": market_key[0], "asset": market_key[1],
                            "quote": market_key[2], "clock": clock,
                            "bars_per_hour": bars_per_hour,
                            "feature_set": feature_set, "test_hour": test_hour,
                            **metrics,
                        })
                        key = (*market_key, clock, bars_per_hour, feature_set)
                        predictions[key].append(prediction)
                        outcomes[key].append(labels[test])
        print(f"Evaluated activity clocks for {' '.join(market_key)}", flush=True)

    aggregates = []
    for key, prediction_parts in predictions.items():
        prediction = np.concatenate(prediction_parts)
        outcome = np.concatenate(outcomes[key])
        loss_difference = outcome * outcome - (outcome - prediction) ** 2
        relevant = [
            row for row in fold_rows
            if (
                row["venue"], row["asset"], row["quote"], row["clock"],
                row["bars_per_hour"], row["feature_set"],
            ) == key
        ]
        statistic, p_value = _hac_test(loss_difference, lag=5)
        ci_low, ci_high = _block_bootstrap_ci(loss_difference, block=20)
        aggregates.append({
            "venue": key[0], "asset": key[1], "quote": key[2],
            "clock": key[3], "bars_per_hour": key[4], "feature_set": key[5],
            **_metrics(outcome, prediction, 0.05),
            "folds": len(relevant),
            "complete_folds": len(relevant) == expected_test_folds,
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
        "feature_cache": str(feature_cache_path),
        "activity_cache": str(activity_cache_path),
        "feature_metadata": feature_metadata,
        "activity_metadata": activity_metadata,
        "targets": targets,
        "protocol": {
            "volume_clock": (
                "absolute public-trade USD notional on the fixed liquid reference set; "
                "threshold frozen from the first training window"
            ),
            "clock_venues": CLOCK_VENUES,
            "normalization": "same target bars/hour for every asset and contract",
            "time_control": "fixed-time bars with the same expected initial bars/hour",
            "flow_features": (
                "prior target bar return, duration, notional, trade count, mean trade size, "
                "external signed imbalance, threshold overshoot, per-venue volume share and imbalance"
            ),
            "split": (
                f"initial {initial_train_hours} hours followed by "
                f"{expected_test_folds} expanding-train one-hour test folds; "
                "labels crossing either fold boundary are purged"
            ),
            "expected_test_folds": expected_test_folds,
            "important_limits": [
                "bars are reconstructed on 500 ms receipt-time buckets, not native tick timestamps",
                "public aggressor classifications and exchange contract multipliers are used",
                "all contracts for an asset share one clock; dollar thresholds differ across assets",
                f"{total_hours} hours from one capture cannot establish regime durability",
            ],
        },
        "dataset_metadata": dataset_metadata,
        "fold_results": fold_rows,
        "aggregate_results": aggregates,
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
        (
            row["venue"], row["asset"], row["quote"], row["clock"],
            row["bars_per_hour"], row["feature_set"],
        ): row for row in rows
    }
    markets = sorted({(row["venue"], row["asset"], row["quote"]) for row in rows})
    dataset_lookup = {
        (row["venue"], row["asset"], row["quote"], row["clock"], row["bars_per_hour"]): row
        for row in report["dataset_metadata"]
    }
    lines = [
        "# Turnover-normalized activity-clock research",
        "",
        f"Source interval: `{report['feature_metadata']['start']}` through ",
        f"`{report['feature_metadata']['end']}`. Public trades from "
        f"{len(report['activity_metadata']['venues'])} futures venues produced "
        f"{sum(report['activity_metadata']['trade_records'].values()):,} parsed participant trades.",
        "",
        "Dollar thresholds are frozen from the initial training window. Every market uses the ",
        "same target number of bars per hour of its own normal activity, while matched fixed-time ",
        "bars provide the control. Only rows with every chronological test fold are shown.",
        "",
        "| Target | Bars/h | Frozen reference-volume threshold | Median duration | Volume + flow skill | Positive folds | Matched time + flow | Difference | Adjusted q |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    shown_markets = set()
    for market in markets:
        for bars_per_hour in sorted({row["bars_per_hour"] for row in rows}):
            time_row = lookup.get((*market, "time", bars_per_hour, "price_only"))
            time_flow_row = lookup.get((*market, "time", bars_per_hour, "price_and_flow"))
            volume_row = lookup.get((*market, "volume", bars_per_hour, "price_only"))
            flow_row = lookup.get((*market, "volume", bars_per_hour, "price_and_flow"))
            metadata = dataset_lookup.get((*market, "volume", bars_per_hour))
            if (
                not time_row or not time_flow_row or not volume_row or not flow_row
                or not metadata
                or not all(row["complete_folds"] for row in (
                    time_row, time_flow_row, volume_row, flow_row
                ))
            ):
                continue
            shown_markets.add(market)
            lines.append(
                f"| {' '.join(market)} | {bars_per_hour} "
                f"| ${_number(metadata['threshold_usd'], 0)} "
                f"| {_number(metadata['median_duration_seconds'])} s "
                f"| {_number(flow_row['mse_skill_pct'])}% "
                f"| {flow_row['positive_skill_folds']}/{flow_row['folds']} "
                f"| {_number(time_flow_row['mse_skill_pct'])}% "
                f"| {_number(flow_row['mse_skill_pct'] - time_flow_row['mse_skill_pct'])} pp "
                f"| {_number(flow_row['bh_q_value'], 4)} |"
            )
    incomplete_markets = sorted(set(markets) - shown_markets)
    if incomplete_markets:
        lines.extend([
            "",
            "Not shown because no setting produced a complete matched set of test folds: "
            + ", ".join(" ".join(market) for market in incomplete_markets) + ".",
        ])
    lines.extend([
        "",
        "MSE skill is measured against a zero-return forecast within each clock. Because volume ",
        "bars have variable calendar duration, compare the skill and fold stability rather than ",
        "raw RMSE levels across clocks. Promotion still requires fill-conditioned PnL.",
        "",
    ])
    return "\n".join(lines)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=pathlib.Path, required=True)
    parser.add_argument("--feature-cache", type=pathlib.Path, required=True)
    parser.add_argument("--activity-cache", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--targets", type=_parse_targets, default=DEFAULT_TARGETS)
    parser.add_argument("--bars-per-hour", type=_parse_integers, default=DEFAULT_BARS_PER_HOUR)
    parser.add_argument("--initial-train-hours", type=int, default=3)
    parser.add_argument("--reuse-activity-cache", action="store_true")
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    feature_cache = np.load(arguments.feature_cache, allow_pickle=False)
    metadata = json.loads(str(feature_cache["metadata"]))
    targets = tuple(arguments.targets)
    assets = tuple(dict.fromkeys(target[1] for target in targets))
    start = datetime.datetime.fromisoformat(metadata["start"])
    end = datetime.datetime.fromisoformat(metadata["end"])
    if not arguments.reuse_activity_cache or not arguments.activity_cache.exists():
        build_activity_cache(
            arguments.run, start, end, assets, arguments.activity_cache
        )
    report = evaluate(
        arguments.feature_cache, arguments.activity_cache, arguments.output,
        targets, tuple(arguments.bars_per_hour), arguments.initial_train_hours,
    )
    print(f"Wrote {arguments.output}", flush=True)
    print(f"Wrote {arguments.output.with_suffix('.md')}", flush=True)
    print(f"Evaluated {len(report['aggregate_results'])} clock/model combinations", flush=True)


if __name__ == "__main__":
    main()
