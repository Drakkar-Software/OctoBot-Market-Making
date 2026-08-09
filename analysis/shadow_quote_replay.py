"""Replay activity-clock quotes against recorded futures books and public trades.

This is a shadow execution harness, not a claim that public trades reveal our true
queue position.  It runs the same causal, expanding-window Ridge signal with and
without its prediction under both a touch fill proxy and a stricter trade-through
proxy.  Every hypothetical order is persisted using a schema that can later be
matched to genuine private order acknowledgements and fills.
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
from dataclasses import dataclass

import numpy as np
from sklearn.base import clone
from sklearn.linear_model import Ridge

from cross_venue_target_research import _market_rows
from model_research import _linear_pipeline
from quote_distance_research import GRID_MS, WindowSeries
from volume_clock_research import (
    _activity_arrays,
    _build_bar_dataset,
    _walk_forward_masks,
)


DEFAULT_TARGET = ("mexc", "SOL", "USDT")
DEFAULT_DISTANCES_BPS = (2.0, 3.0, 5.0, 8.0)
MARKOUT_SECONDS = (1, 5, 30)


def _parse_target(value: str) -> tuple[str, str, str]:
    venue, asset, quote = value.strip().split(":", maxsplit=2)
    return venue.lower(), asset.upper(), quote.upper()


def _parse_floats(value: str) -> tuple[float, ...]:
    return tuple(float(item) for item in value.split(","))


@dataclass
class Ledger:
    starting_equity_usd: float
    cash_usd: float
    inventory_base: float = 0.0
    fees_usd: float = 0.0
    turnover_usd: float = 0.0
    fills: int = 0
    eligible_orders: int = 0
    peak_equity_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    max_abs_inventory_usd: float = 0.0

    @classmethod
    def create(cls, starting_equity_usd: float) -> "Ledger":
        return cls(
            starting_equity_usd=starting_equity_usd,
            cash_usd=starting_equity_usd,
            peak_equity_usd=starting_equity_usd,
        )

    def equity(self, mid: float) -> float:
        return self.cash_usd + self.inventory_base * mid

    def mark(self, mid: float) -> None:
        equity = self.equity(mid)
        self.peak_equity_usd = max(self.peak_equity_usd, equity)
        self.max_drawdown_usd = max(
            self.max_drawdown_usd, self.peak_equity_usd - equity
        )
        self.max_abs_inventory_usd = max(
            self.max_abs_inventory_usd, abs(self.inventory_base * mid)
        )


def _apply_fill(
    ledger: Ledger,
    side: str,
    price: float,
    quantity: float,
    maker_fee_bps: float,
) -> float:
    notional = price * quantity
    fee = notional * maker_fee_bps / 10_000
    if side == "bid":
        ledger.inventory_base += quantity
        ledger.cash_usd -= notional + fee
    else:
        ledger.inventory_base -= quantity
        ledger.cash_usd += notional - fee
    ledger.fees_usd += fee
    ledger.turnover_usd += notional
    ledger.fills += 1
    ledger.mark(price)
    return fee


def _side_allowed(
    inventory_base: float,
    side: str,
    quantity: float,
    mid: float,
    max_inventory_usd: float,
) -> bool:
    change = quantity if side == "bid" else -quantity
    old_exposure = abs(inventory_base * mid)
    new_exposure = abs((inventory_base + change) * mid)
    return new_exposure <= max_inventory_usd or new_exposure < old_exposure


def _order_quantity(
    requested_notional_usd: float,
    mid: float,
    contract_size_base: typing.Optional[float],
) -> float:
    if requested_notional_usd <= 0 or mid <= 0:
        return 0.0
    if contract_size_base is None:
        return requested_notional_usd / mid
    if contract_size_base <= 0:
        raise ValueError("contract size must be positive")
    contracts = math.floor(
        requested_notional_usd / (mid * contract_size_base) + 1e-12
    )
    return contracts * contract_size_base


def _quote_prices(
    mid: float,
    half_spread_bps: float,
    prediction_bps: float,
    inventory_base: float,
    max_inventory_usd: float,
    inventory_skew_at_cap_bps: float,
    distance_bps: float,
) -> tuple[float, float, float, float, float]:
    inventory_ratio = (
        inventory_base * mid / max_inventory_usd if max_inventory_usd > 0 else 0.0
    )
    inventory_ratio = min(max(inventory_ratio, -1.5), 1.5)
    inventory_skew_bps = inventory_ratio * inventory_skew_at_cap_bps
    center_shift_bps = prediction_bps - inventory_skew_bps
    center = mid * math.exp(center_shift_bps / 10_000)
    observed_bid = mid * (1 - half_spread_bps / 10_000)
    observed_ask = mid * (1 + half_spread_bps / 10_000)
    raw_bid = center * math.exp(-distance_bps / 10_000)
    raw_ask = center * math.exp(distance_bps / 10_000)
    # Joining the observed touch is allowed; crossing it is not.
    return (
        min(raw_bid, observed_bid),
        max(raw_ask, observed_ask),
        center,
        inventory_skew_bps,
        center_shift_bps,
    )


def _first_fill(
    trade_min: np.ndarray,
    trade_max: np.ndarray,
    quote: float,
    side: str,
    fill_policy: str,
    trade_through_bps: float,
) -> typing.Optional[int]:
    if fill_policy == "touch":
        condition = trade_min <= quote if side == "bid" else trade_max >= quote
    elif fill_policy == "trade_through":
        threshold = quote * (
            1 - trade_through_bps / 10_000
            if side == "bid" else 1 + trade_through_bps / 10_000
        )
        condition = trade_min <= threshold if side == "bid" else trade_max >= threshold
    else:
        raise ValueError(f"unknown fill policy {fill_policy}")
    matches = np.flatnonzero(np.isfinite(condition) & condition)
    return int(matches[0]) if matches.size else None


def _markout_bps(
    mids: np.ndarray,
    fill_index: int,
    fill_price: float,
    side: str,
    seconds: int,
) -> typing.Optional[float]:
    index = fill_index + seconds * 1_000 // GRID_MS
    if index >= len(mids) or not np.isfinite(mids[index]):
        return None
    return float(
        (mids[index] - fill_price) / fill_price * 10_000
        if side == "bid"
        else (fill_price - mids[index]) / fill_price * 10_000
    )


def _prediction_series(
    feature_cache_path: pathlib.Path,
    activity_cache_path: pathlib.Path,
    target: tuple[str, str, str],
    bars_per_hour: int,
    initial_train_hours: int,
    ridge_alpha: float,
) -> tuple[np.ndarray, np.ndarray, dict]:
    feature_cache = np.load(feature_cache_path, allow_pickle=False)
    activity_cache = np.load(activity_cache_path, allow_pickle=False)
    metadata = json.loads(str(feature_cache["metadata"]))
    market = next(
        row for row in _market_rows(feature_cache)
        if (row["venue"], row["asset"], row["quote"]) == target
    )
    volumes, signed, counts = _activity_arrays(activity_cache, target[1])
    matrix, labels, times, outcome_end_times, dataset_info = _build_bar_dataset(
        market, "volume", bars_per_hour, initial_train_hours,
        volumes, signed, counts,
    )
    selected = matrix[:, : dataset_info["base_feature_count"]]
    start = datetime.datetime.fromisoformat(metadata["start"])
    end = datetime.datetime.fromisoformat(metadata["end"])
    total_hours = int((end - start).total_seconds() // 3600)
    estimator = _linear_pipeline(Ridge(alpha=ridge_alpha))
    prediction_times: list[np.ndarray] = []
    predictions: list[np.ndarray] = []
    fold_rows = []
    warnings.filterwarnings(
        "ignore", message="Skipping features without any observed values"
    )
    for test_hour in range(initial_train_hours, total_hours):
        train, test = _walk_forward_masks(times, outcome_end_times, test_hour)
        if train.sum() < 200 or test.sum() < 30:
            continue
        fitted = clone(estimator).fit(selected[train], labels[train])
        prediction = fitted.predict(selected[test])
        prediction_times.append(times[test])
        predictions.append(prediction)
        baseline = float(np.mean(labels[test] ** 2))
        model_loss = float(np.mean((labels[test] - prediction) ** 2))
        fold_rows.append({
            "test_hour": test_hour,
            "observations": int(test.sum()),
            "mse_skill_pct": 100 * (1 - model_loss / baseline) if baseline else 0.0,
        })
    if not predictions:
        raise ValueError(f"no valid prediction folds for {' '.join(target)}")
    return (
        np.concatenate(prediction_times),
        np.concatenate(predictions),
        {
            "feature_start": start.isoformat(),
            "feature_end": end.isoformat(),
            "dataset": dataset_info,
            "folds": fold_rows,
        },
    )


def _load_target_series(
    run: pathlib.Path,
    start: datetime.datetime,
    end: datetime.datetime,
    target: tuple[str, str, str],
) -> tuple[WindowSeries, np.ndarray, np.ndarray]:
    from exploratory_market_making import _files, _loads

    series = WindowSeries(start, end, (target,))
    paths = _files(run, target[0], start, end)
    print(f"Reading replay target {target[0]}: {len(paths)} file(s)", flush=True)
    for path in paths:
        with gzip.open(path, "rb") as input_file:
            for line in input_file:
                series.consume(target[0], _loads(line))
    mids, half_spreads = series.filled_mid_and_spread(target)
    if not np.any(np.isfinite(mids)):
        raise ValueError(
            f"no valid BBO data for {':'.join(target)} in {run} between "
            f"{start.isoformat()} and {end.isoformat()}"
        )
    return series, mids, half_spreads


def _timestamp(start: datetime.datetime, bucket: int) -> str:
    return (
        start + datetime.timedelta(milliseconds=bucket * GRID_MS)
    ).isoformat()


def _summary(values: list[float]) -> dict[str, typing.Optional[float]]:
    return {
        "mean": float(statistics.fmean(values)) if values else None,
        "median": float(statistics.median(values)) if values else None,
        "p10": float(np.quantile(values, 0.10)) if values else None,
    }


def simulate(
    series: WindowSeries,
    mids: np.ndarray,
    half_spreads: np.ndarray,
    prediction_times: np.ndarray,
    predictions: np.ndarray,
    feature_start: datetime.datetime,
    replay_start: datetime.datetime,
    target: tuple[str, str, str],
    distances_bps: tuple[float, ...],
    decision_seconds: float,
    lifetime_seconds: float,
    entry_latency_ms: int,
    order_notional_usd: float,
    contract_size_base: typing.Optional[float],
    starting_equity_usd: float,
    maker_fee_bps: float,
    max_inventory_usd: float,
    inventory_skew_at_cap_bps: float,
    trade_through_bps: float,
    prediction_clip_bps: float,
    max_signal_age_seconds: float,
    telemetry_path: pathlib.Path,
) -> list[dict]:
    decision_buckets = max(1, int(round(decision_seconds * 1_000 / GRID_MS)))
    lifetime_buckets = max(1, int(round(lifetime_seconds * 1_000 / GRID_MS)))
    latency_buckets = max(0, int(math.ceil(entry_latency_ms / GRID_MS)))
    replay_offset_seconds = (replay_start - feature_start).total_seconds()
    scenarios = {
        (distance, fill_policy, model_mode): Ledger.create(starting_equity_usd)
        for distance in distances_bps
        for fill_policy in ("touch", "trade_through")
        for model_mode in ("signal", "no_signal")
    }
    markouts: dict[tuple, dict[int, list[float]]] = {
        key: {seconds: [] for seconds in MARKOUT_SECONDS} for key in scenarios
    }
    telemetry_path.parent.mkdir(parents=True, exist_ok=True)
    final_mid = float(mids[np.flatnonzero(np.isfinite(mids))[-1]])
    with gzip.open(telemetry_path, "wt", encoding="utf-8") as telemetry:
        for decision_index in range(
            0, max(0, len(mids) - latency_buckets - lifetime_buckets), decision_buckets
        ):
            mid = mids[decision_index]
            half_spread = half_spreads[decision_index]
            if not np.isfinite(mid) or not np.isfinite(half_spread):
                continue
            absolute_seconds = replay_offset_seconds + decision_index * GRID_MS / 1_000
            signal_position = int(
                np.searchsorted(prediction_times, absolute_seconds, side="right") - 1
            )
            if signal_position < 0:
                continue
            signal_age = absolute_seconds - float(prediction_times[signal_position])
            if signal_age < 0 or signal_age > max_signal_age_seconds:
                continue
            raw_prediction = float(predictions[signal_position])
            clipped_prediction = min(
                max(raw_prediction, -prediction_clip_bps), prediction_clip_bps
            )
            active_start = decision_index + latency_buckets
            active_end = min(active_start + lifetime_buckets, len(mids))
            trade_min = series.trade_min[target][active_start:active_end]
            trade_max = series.trade_max[target][active_start:active_end]
            quantity = _order_quantity(
                order_notional_usd, float(mid), contract_size_base
            )
            if quantity <= 0:
                continue
            for key, ledger in scenarios.items():
                distance, fill_policy, model_mode = key
                ledger.mark(float(mid))
                prediction = clipped_prediction if model_mode == "signal" else 0.0
                bid, ask, center, inventory_skew, center_shift = _quote_prices(
                    float(mid), float(half_spread), prediction,
                    ledger.inventory_base, max_inventory_usd,
                    inventory_skew_at_cap_bps, distance,
                )
                candidates = []
                for side, quote in (("bid", bid), ("ask", ask)):
                    if not _side_allowed(
                        ledger.inventory_base, side, quantity, float(mid),
                        max_inventory_usd,
                    ):
                        continue
                    ledger.eligible_orders += 1
                    relative_fill = _first_fill(
                        trade_min, trade_max, quote, side, fill_policy,
                        trade_through_bps,
                    )
                    candidates.append((
                        relative_fill if relative_fill is not None else math.inf,
                        side, quote,
                    ))
                for relative_fill, side, quote in sorted(candidates):
                    fill_index = (
                        active_start + int(relative_fill)
                        if math.isfinite(relative_fill) else None
                    )
                    inventory_before = ledger.inventory_base
                    fee = None
                    fill_markouts = {str(seconds): None for seconds in MARKOUT_SECONDS}
                    if fill_index is not None:
                        fee = _apply_fill(
                            ledger, side, quote, quantity, maker_fee_bps
                        )
                        for seconds in MARKOUT_SECONDS:
                            value = _markout_bps(
                                mids, fill_index, quote, side, seconds
                            )
                            fill_markouts[str(seconds)] = value
                            if value is not None:
                                markouts[key][seconds].append(value)
                    event = {
                        "schema_version": 1,
                        "event_type": "shadow_order",
                        "target": {
                            "venue": target[0], "asset": target[1], "quote": target[2]
                        },
                        "decision_ts": _timestamp(replay_start, decision_index),
                        "activation_ts": _timestamp(replay_start, active_start),
                        "expiry_ts": _timestamp(replay_start, active_end),
                        "side": side,
                        "status": "filled" if fill_index is not None else "expired",
                        "fill_policy": fill_policy,
                        "model_mode": model_mode,
                        "distance_bps": distance,
                        "order_quantity_base": quantity,
                        "contract_size_base": contract_size_base,
                        "order_contracts": (
                            quantity / contract_size_base
                            if contract_size_base is not None else None
                        ),
                        "order_notional_usd_at_decision": quantity * mid,
                        "quote_price": quote,
                        "target_mid": float(mid),
                        "target_half_spread_bps": float(half_spread),
                        "reservation_center": center,
                        "raw_prediction_bps": raw_prediction,
                        "used_prediction_bps": prediction,
                        "signal_age_seconds": signal_age,
                        "inventory_skew_bps": inventory_skew,
                        "center_shift_bps": center_shift,
                        "inventory_before_base": inventory_before,
                        "inventory_after_base": ledger.inventory_base,
                        "assumed_entry_latency_ms": entry_latency_ms,
                        "maker_fee_bps": maker_fee_bps,
                        "fill_ts": (
                            _timestamp(replay_start, fill_index)
                            if fill_index is not None else None
                        ),
                        "fill_price": quote if fill_index is not None else None,
                        "fee_usd": fee,
                        "markout_bps": fill_markouts,
                    }
                    telemetry.write(json.dumps(event, separators=(",", ":")) + "\n")
    rows = []
    for key, ledger in sorted(scenarios.items()):
        distance, fill_policy, model_mode = key
        ledger.mark(final_mid)
        ending_equity = ledger.equity(final_mid)
        net_pnl = ending_equity - ledger.starting_equity_usd
        gross_pnl = net_pnl + ledger.fees_usd
        rows.append({
            "distance_bps": distance,
            "fill_policy": fill_policy,
            "model_mode": model_mode,
            "eligible_orders": ledger.eligible_orders,
            "fills": ledger.fills,
            "fill_rate_pct": (
                100 * ledger.fills / ledger.eligible_orders
                if ledger.eligible_orders else None
            ),
            "turnover_usd": ledger.turnover_usd,
            "fees_usd": ledger.fees_usd,
            "ending_inventory_base": ledger.inventory_base,
            "ending_inventory_usd": ledger.inventory_base * final_mid,
            "ending_equity_usd": ending_equity,
            "gross_pnl_before_fees_usd": gross_pnl,
            "net_pnl_usd": net_pnl,
            "return_pct": (
                100 * (ending_equity / ledger.starting_equity_usd - 1)
            ),
            "break_even_maker_fee_bps": (
                gross_pnl / ledger.turnover_usd * 10_000
                if ledger.turnover_usd else None
            ),
            "fee_sensitivity_net_pnl_usd": {
                str(fee_bps): (
                    gross_pnl - ledger.turnover_usd * fee_bps / 10_000
                )
                for fee_bps in (0.0, 0.5, 1.0, 2.0)
            },
            "max_drawdown_usd": ledger.max_drawdown_usd,
            "max_abs_inventory_usd": ledger.max_abs_inventory_usd,
            "markout_bps": {
                str(seconds): _summary(markouts[key][seconds])
                for seconds in MARKOUT_SECONDS
            },
        })
    return rows


def markdown_report(report: dict) -> str:
    def number(value: typing.Optional[float], digits: int = 2) -> str:
        return "n/a" if value is None else f"{value:,.{digits}f}"

    lines = [
        "# Activity-clock shadow quote replay",
        "",
        "This is a public-data execution proxy. `touch` is an optimistic fill bound; ",
        "`trade_through` is stricter but still does not reconstruct our true queue position.",
        "",
        "| Distance/side | Fill proxy | Model | Orders | Fills | Fill rate | Turnover | Gross PnL | Net PnL | Break-even fee | End inventory | Mean 5s markout |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        lines.append(
            f"| {number(row['distance_bps'])} bps "
            f"| {row['fill_policy']} | {row['model_mode']} "
            f"| {row['eligible_orders']:,} | {row['fills']:,} "
            f"| {number(row['fill_rate_pct'])}% "
            f"| ${number(row['turnover_usd'])} "
            f"| ${number(row['gross_pnl_before_fees_usd'], 4)} "
            f"| ${number(row['net_pnl_usd'], 4)} "
            f"| {number(row['break_even_maker_fee_bps'])} bps "
            f"| ${number(row['ending_inventory_usd'])} "
            f"| {number(row['markout_bps']['5']['mean'])} bps |"
        )
    lines.extend([
        "",
        "Net PnL is a marked futures-style cash/inventory ledger after the configured maker fee. ",
        "Quantities use captured contract sizes, but the replay excludes funding, liquidation ",
        "mechanics, price-tick rounding, venue minimum checks, exchange rejects, and ",
        "queue priority. Live promotion requires genuine post-only acknowledgements and fills.",
        "",
    ])
    return "\n".join(lines)


def run(arguments: argparse.Namespace) -> dict:
    prediction_times, predictions, prediction_metadata = _prediction_series(
        arguments.feature_cache, arguments.activity_cache, arguments.target,
        arguments.bars_per_hour, arguments.initial_train_hours,
        arguments.ridge_alpha,
    )
    feature_start = datetime.datetime.fromisoformat(
        prediction_metadata["feature_start"]
    )
    feature_end = datetime.datetime.fromisoformat(
        prediction_metadata["feature_end"]
    )
    activity_cache = np.load(arguments.activity_cache, allow_pickle=False)
    activity_metadata = json.loads(str(activity_cache["metadata"]))
    inferred_contract_size = activity_metadata.get("multipliers", {}).get(
        "|".join(arguments.target)
    )
    contract_size_base = (
        arguments.contract_size_base
        if arguments.contract_size_base is not None
        else inferred_contract_size
    )
    replay_start = feature_start + datetime.timedelta(hours=arguments.initial_train_hours)
    series, mids, half_spreads = _load_target_series(
        arguments.run, replay_start, feature_end, arguments.target
    )
    telemetry_path = arguments.output.with_name(
        arguments.output.stem + "-orders.jsonl.gz"
    )
    rows = simulate(
        series, mids, half_spreads, prediction_times, predictions,
        feature_start, replay_start, arguments.target,
        tuple(arguments.distances_bps), arguments.decision_seconds,
        arguments.lifetime_seconds, arguments.entry_latency_ms,
        arguments.order_notional_usd, contract_size_base,
        arguments.starting_equity_usd,
        arguments.maker_fee_bps, arguments.max_inventory_usd,
        arguments.inventory_skew_at_cap_bps, arguments.trade_through_bps,
        arguments.prediction_clip_bps, arguments.max_signal_age_seconds,
        telemetry_path,
    )
    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "run": str(arguments.run),
        "feature_cache": str(arguments.feature_cache),
        "activity_cache": str(arguments.activity_cache),
        "telemetry": str(telemetry_path),
        "target": arguments.target,
        "replay_start": replay_start.isoformat(),
        "replay_end": feature_end.isoformat(),
        "configuration": {
            "bars_per_hour": arguments.bars_per_hour,
            "initial_train_hours": arguments.initial_train_hours,
            "ridge_alpha": arguments.ridge_alpha,
            "distances_bps": arguments.distances_bps,
            "decision_seconds": arguments.decision_seconds,
            "lifetime_seconds": arguments.lifetime_seconds,
            "entry_latency_ms": arguments.entry_latency_ms,
            "order_notional_usd": arguments.order_notional_usd,
            "contract_size_base": contract_size_base,
            "contract_size_source": (
                "command_line" if arguments.contract_size_base is not None
                else "captured_contract_metadata" if inferred_contract_size is not None
                else "continuous_base_quantity"
            ),
            "starting_equity_usd": arguments.starting_equity_usd,
            "maker_fee_bps": arguments.maker_fee_bps,
            "max_inventory_usd": arguments.max_inventory_usd,
            "inventory_skew_at_cap_bps": arguments.inventory_skew_at_cap_bps,
            "trade_through_bps": arguments.trade_through_bps,
            "prediction_clip_bps": arguments.prediction_clip_bps,
            "max_signal_age_seconds": arguments.max_signal_age_seconds,
        },
        "prediction": prediction_metadata,
        "important_limits": [
            "public trades do not reveal our real queue position",
            "prices are continuous hypotheticals without tick rounding; quantities use captured contract sizes but venue minimum rules are not validated",
            "entry latency is assumed and cancel acknowledgement latency is not reconstructed",
            "maker fee is configurable and must be replaced by the account's actual fill fee",
            "funding, margin, liquidation, rejects and rate limits are excluded",
        ],
        "rows": rows,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    arguments.output.with_suffix(".md").write_text(
        markdown_report(report), encoding="utf-8"
    )
    return report


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=pathlib.Path, required=True)
    parser.add_argument("--feature-cache", type=pathlib.Path, required=True)
    parser.add_argument("--activity-cache", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--target", type=_parse_target, default=DEFAULT_TARGET)
    parser.add_argument("--bars-per-hour", type=int, default=720)
    parser.add_argument("--initial-train-hours", type=int, default=8)
    parser.add_argument("--ridge-alpha", type=float, default=100.0)
    parser.add_argument("--distances-bps", type=_parse_floats, default=DEFAULT_DISTANCES_BPS)
    parser.add_argument("--decision-seconds", type=float, default=5.0)
    parser.add_argument("--lifetime-seconds", type=float, default=5.0)
    parser.add_argument("--entry-latency-ms", type=int, default=300)
    parser.add_argument("--order-notional-usd", type=float, default=2.0)
    parser.add_argument("--contract-size-base", type=float)
    parser.add_argument("--starting-equity-usd", type=float, default=100.0)
    parser.add_argument("--maker-fee-bps", type=float, default=2.0)
    parser.add_argument("--max-inventory-usd", type=float, default=10.0)
    parser.add_argument("--inventory-skew-at-cap-bps", type=float, default=5.0)
    parser.add_argument("--trade-through-bps", type=float, default=0.2)
    parser.add_argument("--prediction-clip-bps", type=float, default=5.0)
    parser.add_argument("--max-signal-age-seconds", type=float, default=30.0)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    report = run(arguments)
    print(f"Wrote {arguments.output}", flush=True)
    print(f"Wrote {arguments.output.with_suffix('.md')}", flush=True)
    print(f"Wrote {report['telemetry']}", flush=True)


if __name__ == "__main__":
    main()
