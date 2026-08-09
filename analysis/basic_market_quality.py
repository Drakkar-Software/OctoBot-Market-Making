"""Normalize live raw feeds and produce a first market-making quality report."""

import argparse
import collections
import dataclasses
import datetime
import gzip
import json
import math
import pathlib
import statistics
import typing

try:
    import orjson

    def _loads(line: bytes) -> dict:
        return orjson.loads(line)

except ImportError:

    def _loads(line: bytes) -> dict:
        return json.loads(line)


ASSETS = ("BCH", "SOL", "DOGE", "XRP", "LTC")
VENUES = ("gate", "htx", "binance", "okx", "bybit")
REFERENCE_VENUES = ("binance", "okx", "bybit")
TARGET_VENUES = ("gate", "htx")
CONTRACT_MULTIPLIERS = {
    "gate": {"BCH": 0.01, "SOL": 1.0, "DOGE": 10.0, "XRP": 10.0, "LTC": 0.1},
    "htx": {"BCH": 0.01, "SOL": 1.0, "DOGE": 100.0, "XRP": 10.0, "LTC": 0.1},
    "binance": {asset: 1.0 for asset in ASSETS},
    "okx": {"BCH": 0.1, "SOL": 1.0, "DOGE": 1000.0, "XRP": 100.0, "LTC": 1.0},
    "bybit": {asset: 1.0 for asset in ASSETS},
}


@dataclasses.dataclass
class Book:
    bids: dict[float, float] = dataclasses.field(default_factory=dict)
    asks: dict[float, float] = dataclasses.field(default_factory=dict)
    initialized: bool = False

    def replace(self, bids: typing.Iterable, asks: typing.Iterable) -> None:
        self.bids = _levels(bids)
        self.asks = _levels(asks)
        self.initialized = True

    def update(self, bids: typing.Iterable, asks: typing.Iterable) -> None:
        if not self.initialized:
            return
        _update_levels(self.bids, bids)
        _update_levels(self.asks, asks)

    def sample(self, multiplier: float) -> typing.Optional[dict[str, float]]:
        if not self.initialized or not self.bids or not self.asks:
            return None
        bid = max(self.bids)
        ask = min(self.asks)
        if bid <= 0 or ask <= bid:
            return None
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 10_000
        lower = mid * (1 - 5 / 10_000)
        upper = mid * (1 + 5 / 10_000)
        bid_depth = sum(
            price * quantity * multiplier
            for price, quantity in self.bids.items()
            if price >= lower
        )
        ask_depth = sum(
            price * quantity * multiplier
            for price, quantity in self.asks.items()
            if price <= upper
        )
        total_depth = bid_depth + ask_depth
        return {
            "mid": mid,
            "spread_bps": spread_bps,
            "bid_depth_5bps_usd": bid_depth,
            "ask_depth_5bps_usd": ask_depth,
            "min_depth_5bps_usd": min(bid_depth, ask_depth),
            "top_min_usd": min(
                bid * self.bids[bid] * multiplier,
                ask * self.asks[ask] * multiplier,
            ),
            "imbalance_5bps": (
                (bid_depth - ask_depth) / total_depth if total_depth else 0.0
            ),
        }


def _levels(levels: typing.Iterable) -> dict[float, float]:
    result = {}
    for level in levels or ():
        if isinstance(level, dict):
            price, quantity = level.get("p"), level.get("s")
        else:
            price, quantity = level[0], level[1]
        price_float, quantity_float = float(price), float(quantity)
        if quantity_float > 0:
            result[price_float] = quantity_float
    return result


def _update_levels(book_side: dict[float, float], levels: typing.Iterable) -> None:
    for level in levels or ():
        if isinstance(level, dict):
            price, quantity = level.get("p"), level.get("s")
        else:
            price, quantity = level[0], level[1]
        price_float, quantity_float = float(price), float(quantity)
        if quantity_float == 0:
            book_side.pop(price_float, None)
        else:
            book_side[price_float] = quantity_float


def _asset(symbol: typing.Optional[str]) -> typing.Optional[str]:
    if not symbol:
        return None
    normalized = symbol.upper().replace("_", "-")
    if "-" in normalized:
        candidate = normalized.split("-", maxsplit=1)[0]
    elif normalized.endswith("USDT"):
        candidate = normalized[:-4]
    else:
        candidate = normalized
    return candidate if candidate in ASSETS else None


def _percentile(values: typing.Iterable[float], probability: float) -> typing.Optional[float]:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _bucket_time(path: pathlib.Path) -> datetime.datetime:
    timestamp = path.name.removeprefix("events-").removesuffix(".jsonl.gz")
    return datetime.datetime.strptime(timestamp, "%Y%m%dT%H%MZ").replace(
        tzinfo=datetime.timezone.utc
    )


def _files(
    live_root: pathlib.Path,
    venue: str,
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[pathlib.Path]:
    candidates = live_root.glob(f"run=*/raw/venue={venue}/date=*/events-*.jsonl.gz")
    return sorted(path for path in candidates if start <= _bucket_time(path) < end)


class Analyzer:
    def __init__(self, start: datetime.datetime, end: datetime.datetime):
        self.start = start
        self.end = end
        self.start_second = int(start.timestamp())
        self.end_second = int(end.timestamp())
        self.books = {
            (venue, asset): Book() for venue in VENUES for asset in ASSETS
        }
        self.raw_samples: dict[tuple[str, str], dict[int, dict[str, float]]] = {
            key: {} for key in self.books
        }
        self.book_messages: collections.Counter = collections.Counter()
        self.trade_events: collections.Counter = collections.Counter()
        self.trade_notional: collections.Counter = collections.Counter()
        self.signed_trade_notional: collections.Counter = collections.Counter()
        self.records = 0

    def consume(self, venue: str, record: dict) -> None:
        self.records += 1
        record_type = record.get("record_type")
        stream = str(record.get("stream", ""))
        asset = _asset(record.get("symbol"))
        if asset is None:
            return
        received_second = int(record["received_ts_ns"] // 1_000_000_000)
        payload = record.get("payload", {})
        if record_type == "rest" and venue == "gate" and stream == "rest.order_book":
            self.books[(venue, asset)].replace(payload.get("bids"), payload.get("asks"))
            self._store_sample(venue, asset, received_second)
            return
        if record_type != "market":
            return
        if self._consume_book(venue, asset, stream, payload):
            self.book_messages[(venue, asset)] += 1
            self._store_sample(venue, asset, received_second)
        self._consume_trades(venue, asset, stream, payload)

    def _consume_book(self, venue: str, asset: str, stream: str, payload: dict) -> bool:
        book = self.books[(venue, asset)]
        if venue == "gate" and stream == "futures.order_book_update":
            result = payload.get("result", {})
            book.update(result.get("b"), result.get("a"))
            return True
        if venue == "htx" and ".depth." in stream:
            tick = payload.get("tick", {})
            if tick.get("event") == "snapshot":
                book.replace(tick.get("bids"), tick.get("asks"))
            else:
                book.update(tick.get("bids"), tick.get("asks"))
            return True
        if venue == "binance" and stream == "depthUpdate":
            book.replace(payload.get("b"), payload.get("a"))
            return True
        if venue == "okx" and stream == "books5":
            data = payload.get("data") or []
            if data:
                book.replace(data[0].get("bids"), data[0].get("asks"))
            return True
        if venue == "bybit" and stream.startswith("orderbook."):
            data = payload.get("data", {})
            if payload.get("type") == "snapshot":
                book.replace(data.get("b"), data.get("a"))
            else:
                book.update(data.get("b"), data.get("a"))
            return True
        return False

    def _store_sample(self, venue: str, asset: str, second: int) -> None:
        if not self.start_second <= second < self.end_second:
            return
        sample = self.books[(venue, asset)].sample(CONTRACT_MULTIPLIERS[venue][asset])
        if sample is not None:
            self.raw_samples[(venue, asset)][second] = sample

    def _add_trade(
        self, venue: str, asset: str, price: float, base_quantity: float, taker_sign: int
    ) -> None:
        notional = abs(price * base_quantity)
        key = venue, asset
        self.trade_events[key] += 1
        self.trade_notional[key] += notional
        self.signed_trade_notional[key] += taker_sign * notional

    def _consume_trades(self, venue: str, asset: str, stream: str, payload: dict) -> None:
        multiplier = CONTRACT_MULTIPLIERS[venue][asset]
        if venue == "gate" and stream == "futures.trades":
            for trade in payload.get("result") or ():
                size = float(trade["size"])
                self._add_trade(
                    venue,
                    asset,
                    float(trade["price"]),
                    abs(size) * multiplier,
                    1 if size > 0 else -1,
                )
        elif venue == "htx" and stream.endswith(".trade.detail"):
            for trade in payload.get("tick", {}).get("data") or ():
                self._add_trade(
                    venue,
                    asset,
                    float(trade["price"]),
                    float(trade["quantity"]),
                    1 if trade["direction"] == "buy" else -1,
                )
        elif venue == "binance" and stream == "aggTrade":
            self._add_trade(
                venue,
                asset,
                float(payload["p"]),
                float(payload["q"]),
                -1 if payload["m"] else 1,
            )
        elif venue == "okx" and stream == "trades":
            for trade in payload.get("data") or ():
                self._add_trade(
                    venue,
                    asset,
                    float(trade["px"]),
                    float(trade["sz"]) * multiplier,
                    1 if trade["side"] == "buy" else -1,
                )
        elif venue == "bybit" and stream.startswith("publicTrade."):
            for trade in payload.get("data") or ():
                self._add_trade(
                    venue,
                    asset,
                    float(trade["p"]),
                    float(trade["v"]),
                    1 if trade["S"] == "Buy" else -1,
                )

    def filled_samples(self) -> dict[tuple[str, str], dict[int, dict[str, float]]]:
        filled = {}
        for key, raw in self.raw_samples.items():
            result = {}
            last_sample = None
            last_second = None
            for second in range(self.start_second, self.end_second):
                if second in raw:
                    last_sample = raw[second]
                    last_second = second
                if last_sample is not None and second - last_second <= 5:
                    result[second] = last_sample
            filled[key] = result
        return filled


def _summary(values: list[float]) -> dict[str, typing.Optional[float]]:
    return {
        "p10": _percentile(values, 0.10),
        "median": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
    }


def _moves(mids: dict[int, float], horizon: int) -> list[float]:
    return [
        abs(math.log(mid / mids[second - horizon]) * 10_000)
        for second, mid in mids.items()
        if second - horizon in mids and mid > 0 and mids[second - horizon] > 0
    ]


def _solve_three(matrix: list[list[float]], vector: list[float]) -> list[float]:
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        if abs(divisor) < 1e-12:
            return [0.0, 0.0, 0.0]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][3] for row in range(3)]


def _linear_model(
    reference: dict[int, float], target: dict[int, float]
) -> dict[str, typing.Any]:
    rows = []
    for second in sorted(set(reference) & set(target)):
        if second - 1 not in reference or second + 1 not in target:
            continue
        momentum = math.log(reference[second] / reference[second - 1]) * 10_000
        dislocation = (target[second] / reference[second] - 1) * 10_000
        future_return = math.log(target[second + 1] / target[second]) * 10_000
        rows.append(([1.0, momentum, -dislocation], future_return))
    split = int(len(rows) * 0.7)
    if split < 100 or len(rows) - split < 50:
        return {"observations": len(rows)}
    train, test = rows[:split], rows[split:]
    matrix = [[0.0] * 3 for _ in range(3)]
    vector = [0.0] * 3
    for features, target_value in train:
        for row in range(3):
            vector[row] += features[row] * target_value
            for column in range(3):
                matrix[row][column] += features[row] * features[column]
    coefficients = _solve_three(matrix, vector)
    predictions = [sum(c * x for c, x in zip(coefficients, features)) for features, _ in test]
    actual = [target_value for _, target_value in test]
    rmse = math.sqrt(
        statistics.fmean(
            (pred - value) ** 2 for pred, value in zip(predictions, actual)
        )
    )
    baseline_rmse = math.sqrt(statistics.fmean(value**2 for value in actual))
    active = [
        (pred, value)
        for pred, value in zip(predictions, actual)
        if abs(pred) >= 0.05 and abs(value) >= 0.05
    ]
    direction_accuracy = (
        statistics.fmean(pred * value > 0 for pred, value in active)
        if active
        else None
    )
    return {
        "observations": len(rows),
        "train_observations": len(train),
        "test_observations": len(test),
        "intercept_bps": coefficients[0],
        "reference_momentum_beta": coefficients[1],
        "dislocation_reversion_beta": coefficients[2],
        "test_rmse_bps": rmse,
        "baseline_rmse_bps": baseline_rmse,
        "rmse_improvement_pct": (1 - rmse / baseline_rmse) * 100 if baseline_rmse else 0.0,
        "active_predictions": len(active),
        "active_direction_accuracy": direction_accuracy,
    }


def build_report(analyzer: Analyzer) -> dict:
    samples = analyzer.filled_samples()
    duration_hours = (analyzer.end - analyzer.start).total_seconds() / 3600
    venue_metrics = {}
    for venue in VENUES:
        venue_metrics[venue] = {}
        for asset in ASSETS:
            key = venue, asset
            asset_samples = list(samples[key].values())
            total_notional = analyzer.trade_notional[key]
            venue_metrics[venue][asset] = {
                "sample_seconds": len(asset_samples),
                "book_updates_per_second": analyzer.book_messages[key]
                / (duration_hours * 3600),
                "spread_bps": _summary([sample["spread_bps"] for sample in asset_samples]),
                "min_depth_5bps_usd": _summary(
                    [sample["min_depth_5bps_usd"] for sample in asset_samples]
                ),
                "top_min_usd": _summary([sample["top_min_usd"] for sample in asset_samples]),
                "absolute_imbalance_5bps": _summary(
                    [abs(sample["imbalance_5bps"]) for sample in asset_samples]
                ),
                "trade_events_per_second": analyzer.trade_events[key]
                / (duration_hours * 3600),
                "trade_notional_usd_per_hour": total_notional / duration_hours,
                "taker_buy_share": (
                    (analyzer.signed_trade_notional[key] / total_notional + 1) / 2
                    if total_notional
                    else None
                ),
            }

    reference_mids = {}
    reference_risk = {}
    for asset in ASSETS:
        mids = {}
        for second in range(analyzer.start_second, analyzer.end_second):
            available = [
                samples[(venue, asset)][second]["mid"]
                for venue in REFERENCE_VENUES
                if second in samples[(venue, asset)]
            ]
            if len(available) >= 2:
                mids[second] = statistics.median(available)
        reference_mids[asset] = mids
        reference_risk[asset] = {
            "sample_seconds": len(mids),
            "absolute_move_1s_bps": _summary(_moves(mids, 1)),
            "absolute_move_5s_bps": _summary(_moves(mids, 5)),
            "absolute_move_30s_bps": _summary(_moves(mids, 30)),
        }

    target_metrics = {}
    rankings = []
    for venue in TARGET_VENUES:
        target_metrics[venue] = {}
        for asset in ASSETS:
            target_mids = {
                second: sample["mid"] for second, sample in samples[(venue, asset)].items()
            }
            common_seconds = set(target_mids) & set(reference_mids[asset])
            dislocations = [
                (target_mids[second] / reference_mids[asset][second] - 1) * 10_000
                for second in common_seconds
            ]
            median_dislocation = _percentile(dislocations, 0.50)
            centered_dislocations = [
                abs(value - median_dislocation) for value in dislocations
            ] if median_dislocation is not None else []
            model = _linear_model(reference_mids[asset], target_mids)
            target_metrics[venue][asset] = {
                "absolute_dislocation_bps": _summary([abs(value) for value in dislocations]),
                "absolute_centered_dislocation_bps": _summary(centered_dislocations),
                "median_dislocation_bps": median_dislocation,
                "signed_dislocation_mean_bps": (
                    statistics.fmean(dislocations) if dislocations else None
                ),
                "one_second_model": model,
            }
            spread = venue_metrics[venue][asset]["spread_bps"]["median"] or 0.0
            depth = venue_metrics[venue][asset]["min_depth_5bps_usd"]["median"] or 0.0
            activity = venue_metrics[venue][asset]["trade_notional_usd_per_hour"]
            dislocation = (
                target_metrics[venue][asset]["absolute_centered_dislocation_bps"]["p95"]
                or 0.0
            )
            move = reference_risk[asset]["absolute_move_1s_bps"]["p95"] or 0.0
            score = (
                (spread / 2) / (dislocation + move + 0.1)
                * math.log1p(activity / 10_000)
                * math.log1p(depth / 1_000)
            )
            rankings.append(
                {
                    "venue": venue,
                    "asset": asset,
                    "research_score": score,
                    "median_spread_bps": spread,
                    "median_half_spread_bps": spread / 2,
                    "median_min_depth_5bps_usd": depth,
                    "trade_notional_usd_per_hour": activity,
                    "p95_absolute_dislocation_bps": dislocation,
                    "p95_reference_move_1s_bps": move,
                    "model_rmse_improvement_pct": model.get("rmse_improvement_pct"),
                    "model_direction_accuracy": model.get("active_direction_accuracy"),
                }
            )
    rankings.sort(key=lambda row: row["research_score"], reverse=True)
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "window": {"start": analyzer.start.isoformat(), "end": analyzer.end.isoformat()},
        "records_parsed": analyzer.records,
        "method": {
            "book_sampling": "last valid normalized book per UTC second; carry-forward <=5s",
            "reference_fair_value": (
                "per-second median of Binance, OKX and Bybit mids; at least two required"
            ),
            "depth": "smaller bid/ask notional within 5 bps of mid",
            "model": (
                "70/30 chronological OLS: next 1s target return from reference 1s "
                "momentum and target/reference dislocation"
            ),
        },
        "venue_metrics": venue_metrics,
        "reference_risk": reference_risk,
        "target_metrics": target_metrics,
        "target_ranking": rankings,
    }


def _number(value: typing.Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def markdown_report(report: dict) -> str:
    lines = [
        "# Basic futures market-quality report",
        "",
        f"Window: `{report['window']['start']}` to `{report['window']['end']}`. ",
        f"Parsed {report['records_parsed']:,} raw records.",
        "",
        "## Reference price risk",
        "",
        "| Asset | p95 abs 1s (bps) | p95 abs 5s (bps) | p95 abs 30s (bps) |",
        "|---|---:|---:|---:|",
    ]
    for asset in ASSETS:
        risk = report["reference_risk"][asset]
        lines.append(
            f"| {asset} | {_number(risk['absolute_move_1s_bps']['p95'])} "
            f"| {_number(risk['absolute_move_5s_bps']['p95'])} "
            f"| {_number(risk['absolute_move_30s_bps']['p95'])} |"
        )
    lines.extend(
        [
            "",
            "## Gate and HTX preliminary ranking",
            "",
            "| Rank | Venue | Asset | Median spread (bps) | Half-spread (bps) "
            "| 5bps depth, smaller side | Trade notional/hour "
            "| p95 residual target/ref gap (bps) | 1s model RMSE improvement |",
            "|---:|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for rank, row in enumerate(report["target_ranking"], start=1):
        lines.append(
            f"| {rank} | {row['venue']} | {row['asset']} "
            f"| {_number(row['median_spread_bps'])} "
            f"| {_number(row['median_half_spread_bps'])} "
            f"| ${_number(row['median_min_depth_5bps_usd'], 0)} "
            f"| ${_number(row['trade_notional_usd_per_hour'], 0)} "
            f"| {_number(row['p95_absolute_dislocation_bps'])} "
            f"| {_number(row['model_rmse_improvement_pct'])}% |"
        )
    lines.extend(
        [
            "",
            "The score is a relative research priority, not expected PnL. It rewards spread, "
            "depth and activity and penalizes reference volatility and variation around each "
            "venue's median basis.",
            "",
            "A median half-spread is the maximum maker fee plus adverse-selection budget available "
            "before inventory effects. Actual profitability requires private fills and post-fill "
            "markouts.",
            "",
        ]
    )
    return "\n".join(lines)


def _utc(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-root", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--hours", type=int, default=5)
    parser.add_argument("--start", type=_utc)
    parser.add_argument("--end", type=_utc)
    arguments = parser.parse_args()
    if bool(arguments.start) != bool(arguments.end):
        parser.error("--start and --end must be supplied together")
    return arguments


def main() -> None:
    arguments = _arguments()
    if arguments.start:
        start, end = arguments.start, arguments.end
    else:
        end = datetime.datetime.now(datetime.timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        start = end - datetime.timedelta(hours=arguments.hours)
    analyzer = Analyzer(start, end)
    for venue in VENUES:
        venue_files = _files(arguments.live_root, venue, start, end)
        print(f"Reading {venue}: {len(venue_files)} files", flush=True)
        for path in venue_files:
            with gzip.open(path, "rb") as input_file:
                for line in input_file:
                    analyzer.consume(venue, _loads(line))
    report = build_report(analyzer)
    arguments.output.mkdir(parents=True, exist_ok=True)
    filename = f"basic-market-quality-{start:%Y%m%dT%H%MZ}-{end:%Y%m%dT%H%MZ}.json"
    json_path = arguments.output / filename
    markdown_path = json_path.with_suffix(".md")
    with json_path.open("w", encoding="utf-8") as output_file:
        json.dump(report, output_file, indent=2, allow_nan=False)
        output_file.write("\n")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    print(f"Wrote {json_path}", flush=True)
    print(f"Wrote {markdown_path}", flush=True)


if __name__ == "__main__":
    main()
