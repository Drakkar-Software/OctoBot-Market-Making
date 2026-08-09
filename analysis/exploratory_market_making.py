"""Explore market quality and short-horizon cross-venue signals in a live capture.

The report is deliberately fill-model agnostic.  Public market data can measure quoted
spread, depth, turnover, basis, volatility and price discovery, but it cannot establish
our queue position or whether a hypothetical passive order would have filled.
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
from array import array

try:
    import orjson

    def _loads(line: bytes) -> dict:
        return orjson.loads(line)

except ImportError:

    def _loads(line: bytes) -> dict:
        return json.loads(line)


ASSETS = (
    "BTC", "ETH", "BCH", "SOL", "DOGE", "XRP", "LTC", "ADA", "BNB", "SUI",
    "LINK", "AVAX", "AAVE", "UNI", "HYPE", "NEAR", "ENA", "ZEC", "FIL",
    "XLM", "ONDO",
)
VENUES = (
    "gate", "htx", "binance", "okx", "bybit", "hyperliquid", "mexc",
    "binance_spot", "okx_spot",
)
TARGET_VENUES = ("gate", "htx")
REFERENCE_VENUES = ("binance", "okx", "bybit", "mexc")
SPOT_VENUES = ("binance_spot", "okx_spot")
SOURCE_SERIES = (
    ("binance_perp_usdt", "binance", "USDT"),
    ("okx_perp_usdt", "okx", "USDT"),
    ("bybit_perp_usdt", "bybit", "USDT"),
    ("mexc_perp_usdt", "mexc", "USDT"),
    ("binance_perp_usdc", "binance", "USDC"),
    ("bybit_perp_usdc", "bybit", "USDC"),
    ("mexc_perp_usdc", "mexc", "USDC"),
    ("hyperliquid_perp_usdc", "hyperliquid", "USDC"),
    ("binance_spot_usdt", "binance_spot", "USDT"),
    ("binance_spot_usdc", "binance_spot", "USDC"),
    ("okx_spot_usdt", "okx_spot", "USDT"),
    ("okx_spot_usdc", "okx_spot", "USDC"),
)
GRID_MS = 100
STALE_BUCKETS = 10


def _percentile(values: typing.Iterable[float], probability: float) -> typing.Optional[float]:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _summary(values: typing.Iterable[float]) -> dict[str, typing.Optional[float]]:
    materialized = list(values)
    return {
        "p10": _percentile(materialized, 0.10),
        "median": _percentile(materialized, 0.50),
        "p90": _percentile(materialized, 0.90),
        "p95": _percentile(materialized, 0.95),
        "p99": _percentile(materialized, 0.99),
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _levels(levels: typing.Iterable) -> dict[float, float]:
    result = {}
    for level in levels or ():
        if isinstance(level, dict):
            price = level.get("p", level.get("px"))
            quantity = level.get("s", level.get("sz"))
        else:
            price, quantity = level[0], level[1]
        if price is None or quantity is None:
            continue
        price_float, quantity_float = float(price), float(quantity)
        if price_float > 0 and quantity_float > 0:
            result[price_float] = quantity_float
    return result


def _update_levels(book_side: dict[float, float], levels: typing.Iterable) -> None:
    for level in levels or ():
        if isinstance(level, dict):
            price = level.get("p", level.get("px"))
            quantity = level.get("s", level.get("sz"))
        else:
            price, quantity = level[0], level[1]
        if price is None or quantity is None:
            continue
        price_float, quantity_float = float(price), float(quantity)
        if quantity_float == 0:
            book_side.pop(price_float, None)
        elif price_float > 0:
            book_side[price_float] = quantity_float


class Book:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.initialized = False

    def replace(self, bids: typing.Iterable, asks: typing.Iterable) -> None:
        self.bids, self.asks = _levels(bids), _levels(asks)
        self.initialized = True

    def update(self, bids: typing.Iterable, asks: typing.Iterable) -> None:
        if not self.initialized:
            return
        _update_levels(self.bids, bids)
        _update_levels(self.asks, asks)

    def quote(self) -> typing.Optional[tuple[float, float, float, float, float, float]]:
        if not self.initialized or not self.bids or not self.asks:
            return None
        bid, ask = max(self.bids), min(self.asks)
        if bid <= 0 or ask <= bid:
            return None
        mid = (bid + ask) / 2
        lower, upper = mid * (1 - 5 / 10_000), mid * (1 + 5 / 10_000)
        bid_depth = sum(
            price * quantity for price, quantity in self.bids.items() if price >= lower
        )
        ask_depth = sum(
            price * quantity for price, quantity in self.asks.items() if price <= upper
        )
        return bid, ask, self.bids[bid], self.asks[ask], bid_depth, ask_depth


def _quote_from_levels(
    bids: typing.Iterable, asks: typing.Iterable
) -> typing.Optional[tuple[float, float, float, float, float, float]]:
    book = Book()
    book.replace(bids, asks)
    return book.quote()


def _file_bucket(path: pathlib.Path) -> datetime.datetime:
    timestamp = path.name.removeprefix("events-").removesuffix(".jsonl.gz")
    return datetime.datetime.strptime(timestamp, "%Y%m%dT%H%MZ").replace(
        tzinfo=datetime.timezone.utc
    )


def _files(
    run: pathlib.Path,
    venue: str,
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[pathlib.Path]:
    candidates = run.glob(f"raw/venue={venue}/date=*/events-*.jsonl.gz")
    return sorted(path for path in candidates if start <= _file_bucket(path) < end)


class Analyzer:
    def __init__(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
        assets: typing.Iterable[str] = ASSETS,
    ) -> None:
        self.start, self.end = start, end
        self.assets = frozenset(assets)
        self.start_ns = int(start.timestamp() * 1_000_000_000)
        self.end_ns = int(end.timestamp() * 1_000_000_000)
        self.start_bucket = int(start.timestamp() * 1000) // GRID_MS
        self.bucket_count = int((end - start).total_seconds() * 1000) // GRID_MS
        self.duration_seconds = (end - start).total_seconds()
        self.books: dict[tuple[str, str, str], Book] = collections.defaultdict(Book)
        self.tickers: dict[tuple[str, str, str], dict[str, float]] = collections.defaultdict(dict)
        self.mids: dict[tuple[str, str, str], array] = {}
        self.target_bids: dict[tuple[str, str], array] = {}
        self.target_asks: dict[tuple[str, str], array] = {}
        self.quality: dict[
            tuple[str, str], dict[int, tuple[float, float, float, float, float, float]]
        ] = collections.defaultdict(dict)
        self.contract_multipliers: dict[tuple[str, str, str], float] = {}
        self.records: collections.Counter = collections.Counter()
        self.market_records: collections.Counter = collections.Counter()
        self.system_events: collections.Counter = collections.Counter()
        self.book_updates: collections.Counter = collections.Counter()
        self.trade_events: collections.Counter = collections.Counter()
        self.trade_direct_usd: collections.Counter = collections.Counter()
        self.trade_contract_px_qty: collections.Counter = collections.Counter()

    def _empty(self) -> array:
        return array("d", [math.nan]) * self.bucket_count

    def _in_window(self, timestamp_ns: int) -> bool:
        return self.start_ns <= timestamp_ns < self.end_ns

    def _index(self, timestamp_ns: int) -> int:
        return timestamp_ns // (GRID_MS * 1_000_000) - self.start_bucket

    def _record_quote(
        self,
        key: tuple[str, str, str],
        quote: typing.Optional[tuple[float, float, float, float, float, float]],
        timestamp_ns: int,
    ) -> None:
        if quote is None or not self._in_window(timestamp_ns):
            return
        bid, ask, bid_quantity, ask_quantity, bid_depth, ask_depth = quote
        index = self._index(timestamp_ns)
        if not 0 <= index < self.bucket_count:
            return
        mid = (bid + ask) / 2
        series = self.mids.get(key)
        if series is None:
            series = self._empty()
            self.mids[key] = series
        series[index] = mid
        venue, asset, quote_asset = key
        if venue in TARGET_VENUES and quote_asset == "USDT":
            target_key = venue, asset
            bid_series = self.target_bids.get(target_key)
            if bid_series is None:
                bid_series = self._empty()
                self.target_bids[target_key] = bid_series
            ask_series = self.target_asks.get(target_key)
            if ask_series is None:
                ask_series = self._empty()
                self.target_asks[target_key] = ask_series
            bid_series[index] = bid
            ask_series[index] = ask
            second = (timestamp_ns - self.start_ns) // 1_000_000_000
            self.quality[target_key][int(second)] = (
                (ask - bid) / mid * 10_000,
                bid * bid_quantity,
                ask * ask_quantity,
                bid_depth,
                ask_depth,
                mid,
            )

    def _contract_info(self, venue: str, record: dict) -> None:
        payload = record.get("payload", {})
        base, quote = record.get("base_asset"), record.get("quote_asset")
        try:
            if venue == "gate" and base and quote:
                self.contract_multipliers[(venue, base, quote)] = float(
                    payload["quanto_multiplier"]
                )
            elif venue == "htx" and base and quote:
                data = payload.get("data") or []
                if data:
                    self.contract_multipliers[(venue, base, quote)] = float(
                        data[0]["contract_size"]
                    )
            elif venue == "okx" and base and quote:
                data = payload.get("data") or []
                if data:
                    self.contract_multipliers[(venue, base, quote)] = float(data[0]["ctVal"])
            elif venue == "mexc":
                for item in payload.get("data") or ():
                    symbol = str(item.get("symbol", ""))
                    if "_" not in symbol:
                        continue
                    item_base, item_quote = symbol.rsplit("_", 1)
                    self.contract_multipliers[(venue, item_base, item_quote)] = float(
                        item["contractSize"]
                    )
        except (KeyError, TypeError, ValueError):
            return

    def consume(self, venue: str, record: dict) -> None:
        timestamp_ns = int(record.get("received_ts_ns") or 0)
        in_window = self._in_window(timestamp_ns)
        record_type, stream = record.get("record_type"), str(record.get("stream", ""))
        base, quote_asset = record.get("base_asset"), record.get("quote_asset")
        if in_window:
            self.records[venue] += 1
            if record_type == "market":
                self.market_records[venue] += 1
            elif record_type == "system":
                self.system_events[(venue, stream)] += 1
        if record_type == "rest" and stream == "rest.contract_info":
            self._contract_info(venue, record)
        if not base or base not in self.assets or not quote_asset:
            return
        key = venue, base, quote_asset
        payload = record.get("payload", {})

        if venue == "gate":
            if record_type == "rest" and stream == "rest.order_book":
                self.books[key].replace(payload.get("bids"), payload.get("asks"))
                self._record_quote(key, self.books[key].quote(), timestamp_ns)
            elif record_type == "market" and stream == "futures.order_book_update":
                result = payload.get("result", {})
                self.books[key].update(result.get("b"), result.get("a"))
                self._record_quote(key, self.books[key].quote(), timestamp_ns)
                if in_window:
                    self.book_updates[(venue, base)] += 1
            elif record_type == "market" and stream == "futures.trades" and in_window:
                for trade in payload.get("result") or ():
                    self.trade_events[(venue, base)] += 1
                    self.trade_contract_px_qty[(venue, base)] += abs(
                        float(trade["price"]) * float(trade["size"])
                    )
            return

        if venue == "htx":
            if record_type == "market" and ".depth." in stream:
                tick = payload.get("tick", {})
                if tick.get("event") == "snapshot":
                    self.books[key].replace(tick.get("bids"), tick.get("asks"))
                else:
                    self.books[key].update(tick.get("bids"), tick.get("asks"))
                self._record_quote(key, self.books[key].quote(), timestamp_ns)
                if in_window:
                    self.book_updates[(venue, base)] += 1
            elif record_type == "market" and stream.endswith(".trade.detail") and in_window:
                for trade in payload.get("tick", {}).get("data") or ():
                    self.trade_events[(venue, base)] += 1
                    turnover = trade.get("trade_turnover")
                    if turnover is not None:
                        self.trade_direct_usd[(venue, base)] += abs(float(turnover))
                    else:
                        self.trade_contract_px_qty[(venue, base)] += abs(
                            float(trade["price"]) * float(trade.get("amount", 0))
                        )
            return

        if record_type != "market":
            return
        direct_quote = None
        if venue == "binance" and stream == "depthUpdate":
            direct_quote = _quote_from_levels(payload.get("b"), payload.get("a"))
        elif venue in {"okx", "okx_spot"} and stream == "books5":
            data = payload.get("data") or []
            if data:
                direct_quote = _quote_from_levels(data[0].get("bids"), data[0].get("asks"))
        elif venue == "bybit" and stream.startswith("tickers."):
            data = payload.get("data", {})
            state = self.tickers[key]
            for source, destination in (
                ("bid1Price", "bid"), ("ask1Price", "ask"),
                ("bid1Size", "bid_size"), ("ask1Size", "ask_size"),
            ):
                if data.get(source) not in (None, ""):
                    state[destination] = float(data[source])
            if state.get("bid", 0) and state.get("ask", 0) > state.get("bid", 0):
                direct_quote = (
                    state["bid"], state["ask"], state.get("bid_size", 0),
                    state.get("ask_size", 0), state["bid"] * state.get("bid_size", 0),
                    state["ask"] * state.get("ask_size", 0),
                )
        elif venue == "hyperliquid" and stream == "l2Book":
            levels = payload.get("data", {}).get("levels") or []
            if len(levels) >= 2:
                direct_quote = _quote_from_levels(levels[0], levels[1])
        elif venue == "mexc" and stream == "push.depth.full":
            data = payload.get("data", {})
            direct_quote = _quote_from_levels(data.get("bids"), data.get("asks"))
        elif venue == "binance_spot" and "@bookTicker" in stream:
            data = payload.get("data", payload)
            try:
                bid, ask = float(data["b"]), float(data["a"])
                bid_size, ask_size = float(data["B"]), float(data["A"])
                direct_quote = (
                    bid, ask, bid_size, ask_size, bid * bid_size, ask * ask_size
                )
            except (KeyError, TypeError, ValueError):
                pass
        if direct_quote is not None:
            self._record_quote(key, direct_quote, timestamp_ns)
            if in_window:
                self.book_updates[(venue, base)] += 1

    def filled(self, key: tuple[str, str, str]) -> array:
        source = self.mids.get(key)
        result = self._empty()
        if source is None:
            return result
        last = math.nan
        last_index = -STALE_BUCKETS - 1
        for index, value in enumerate(source):
            if math.isfinite(value):
                last, last_index = value, index
            if index - last_index <= STALE_BUCKETS:
                result[index] = last
        return result


def _valid(value: float) -> bool:
    return math.isfinite(value) and value > 0


def _composite(series: list[array], minimum: int = 2) -> array:
    if not series:
        return array("d")
    result = array("d", [math.nan]) * len(series[0])
    for index in range(len(result)):
        values = [item[index] for item in series if _valid(item[index])]
        if len(values) >= minimum:
            result[index] = _median(values)
    return result


def _basis_normalized_composite(series: list[array], minimum: int = 2) -> array:
    """Put venue mids on the first venue's price level before taking a median."""
    if not series:
        return array("d")
    anchor = series[0]
    offsets = []
    for component in series:
        differences = [
            math.log(component[index] / anchor[index]) * 10_000
            for index in range(0, min(len(component), len(anchor)), 10)
            if _valid(component[index]) and _valid(anchor[index])
        ]
        offsets.append(_percentile(differences, 0.50) or 0.0)
    result = array("d", [math.nan]) * len(anchor)
    for index in range(len(result)):
        values = [
            component[index] / math.exp(offset / 10_000)
            for component, offset in zip(series, offsets)
            if _valid(component[index])
        ]
        if len(values) >= minimum:
            result[index] = _median(values)
    return result


def _absolute_moves(series: array, horizon: int) -> list[float]:
    return [
        abs(math.log(series[index] / series[index - horizon]) * 10_000)
        for index in range(horizon, len(series))
        if _valid(series[index]) and _valid(series[index - horizon])
    ]


def _basis(target: array, reference: array, stride: int = 10) -> dict:
    values = [
        math.log(target[index] / reference[index]) * 10_000
        for index in range(0, min(len(target), len(reference)), stride)
        if _valid(target[index]) and _valid(reference[index])
    ]
    center = _percentile(values, 0.50)
    centered = [abs(value - center) for value in values] if center is not None else []
    return {
        "observations": len(values),
        "median_bps": center,
        "absolute_centered_bps": _summary(centered),
    }


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


def _lead_model(reference: array, target: array) -> dict:
    lookback, horizon = 5, 5  # 500 ms at the 100 ms grid
    raw_rows = []
    basis_values = [
        math.log(target[index] / reference[index]) * 10_000
        for index in range(0, min(len(reference), len(target)), 10)
        if _valid(reference[index]) and _valid(target[index])
    ]
    center = _percentile(basis_values, 0.50)
    if center is None:
        return {"observations": 0}
    for index in range(lookback, min(len(reference), len(target)) - horizon):
        required = (
            reference[index], reference[index - lookback], target[index],
            target[index + horizon],
        )
        if not all(_valid(value) for value in required):
            continue
        reference_momentum = math.log(reference[index] / reference[index - lookback]) * 10_000
        gap = -(math.log(target[index] / reference[index]) * 10_000 - center)
        future_return = math.log(target[index + horizon] / target[index]) * 10_000
        raw_rows.append(([1.0, reference_momentum, gap], future_return))
    # Thin overlapping rows to one every 500 ms before chronological train/test.
    rows = raw_rows[::horizon]
    split = int(len(rows) * 0.70)
    if split < 100 or len(rows) - split < 50:
        return {"observations": len(rows)}
    train, test = rows[:split], rows[split:]
    matrix = [[0.0] * 3 for _ in range(3)]
    vector = [0.0] * 3
    for features, outcome in train:
        for row in range(3):
            vector[row] += features[row] * outcome
            for column in range(3):
                matrix[row][column] += features[row] * features[column]
    coefficients = _solve_three(matrix, vector)
    predictions = [sum(c * x for c, x in zip(coefficients, features)) for features, _ in test]
    actual = [outcome for _, outcome in test]
    baseline_rmse = math.sqrt(statistics.fmean(value * value for value in actual))
    rmse = math.sqrt(
        statistics.fmean((prediction - value) ** 2 for prediction, value in zip(predictions, actual))
    )
    active = [
        (prediction, value)
        for prediction, value in zip(predictions, actual)
        if abs(prediction) >= 0.10 and abs(value) >= 0.05
    ]
    return {
        "observations": len(rows),
        "train_observations": len(train),
        "test_observations": len(test),
        "intercept_bps": coefficients[0],
        "reference_momentum_beta": coefficients[1],
        "centered_gap_beta": coefficients[2],
        "test_rmse_bps": rmse,
        "baseline_rmse_bps": baseline_rmse,
        "rmse_improvement_pct": (1 - rmse / baseline_rmse) * 100 if baseline_rmse else 0,
        "active_predictions": len(active),
        "active_direction_accuracy": (
            statistics.fmean(prediction * value > 0 for prediction, value in active)
            if active else None
        ),
        "mean_active_aligned_move_bps": (
            statistics.fmean(math.copysign(value, prediction) for prediction, value in active)
            if active else None
        ),
    }


def build_report(analyzer: Analyzer, run: pathlib.Path) -> dict:
    filled_cache: dict[tuple[str, str, str], array] = {}

    def filled(key: tuple[str, str, str]) -> array:
        if key not in filled_cache:
            filled_cache[key] = analyzer.filled(key)
        return filled_cache[key]

    references = {}
    all_perpetual_references = {}
    reference_risk = {}
    for asset in ASSETS:
        component_series = [filled((venue, asset, "USDT")) for venue in REFERENCE_VENUES]
        composite = _basis_normalized_composite(component_series, minimum=2)
        references[asset] = composite
        all_perpetual_references[asset] = _basis_normalized_composite(
            [
                filled((venue, asset, quote_asset))
                for _, venue, quote_asset in SOURCE_SERIES
                if "perp" in _
            ],
            minimum=3,
        )
        reference_risk[asset] = {
            "coverage_pct": 100 * sum(_valid(value) for value in composite) / len(composite),
            "absolute_move_100ms_bps": _summary(_absolute_moves(composite, 1)),
            "absolute_move_500ms_bps": _summary(_absolute_moves(composite, 5)),
            "absolute_move_1s_bps": _summary(_absolute_moves(composite, 10)),
            "absolute_move_5s_bps": _summary(_absolute_moves(composite, 50)),
        }

    targets = {}
    ranking = []
    for venue in TARGET_VENUES:
        targets[venue] = {}
        for asset in ASSETS:
            key = venue, asset
            multiplier = analyzer.contract_multipliers.get((venue, asset, "USDT"), 1.0)
            samples = list(analyzer.quality[key].values())
            spread_values = [sample[0] for sample in samples]
            min_top_values = [min(sample[1], sample[2]) * multiplier for sample in samples]
            min_depth_values = [min(sample[3], sample[4]) * multiplier for sample in samples]
            trade_notional = analyzer.trade_direct_usd[key] + (
                analyzer.trade_contract_px_qty[key] * multiplier
            )
            target_series = filled((venue, asset, "USDT"))
            target_basis = _basis(target_series, references[asset])
            model = _lead_model(references[asset], target_series)
            all_perpetual_model = _lead_model(
                all_perpetual_references[asset], target_series
            )
            source_models = {}
            for source_name, source_venue, source_quote in SOURCE_SERIES:
                source_series = filled((source_venue, asset, source_quote))
                source_model = _lead_model(source_series, target_series)
                source_model["source_coverage_pct"] = (
                    100 * sum(_valid(value) for value in source_series) / len(source_series)
                )
                source_models[source_name] = source_model
            spread = _summary(spread_values)
            depth = _summary(min_depth_values)
            half_spread = (spread["median"] or 0) / 2
            p95_move_500ms = (
                reference_risk[asset]["absolute_move_500ms_bps"]["p95"] or 0
            )
            p99_move_500ms = (
                reference_risk[asset]["absolute_move_500ms_bps"]["p99"] or 0
            )
            target_row = {
                "coverage_pct": 100 * len(samples) / analyzer.duration_seconds,
                "book_updates_per_second": analyzer.book_updates[key] / analyzer.duration_seconds,
                "spread_bps": spread,
                "top_min_usd": _summary(min_top_values),
                "min_depth_5bps_usd": depth,
                "trade_events_per_second": analyzer.trade_events[key] / analyzer.duration_seconds,
                "observed_trade_notional_usd_per_hour": (
                    trade_notional * 3600 / analyzer.duration_seconds
                ),
                "target_reference_basis": target_basis,
                "lead_model_500ms": model,
                "all_perpetual_lead_model_500ms": all_perpetual_model,
                "individual_source_models_500ms": source_models,
                "median_half_spread_bps": half_spread,
                "conservative_fee_budget_after_p99_500ms_move_bps_per_side": (
                    half_spread - p99_move_500ms
                ),
                "gross_round_trip_after_maker_fees_bps": {
                    str(fee): (spread["median"] or 0) - 2 * fee for fee in (0, 1, 2, 3, 4)
                },
                "contract_multiplier": multiplier,
            }
            targets[venue][asset] = target_row
            centered_gap = target_basis["absolute_centered_bps"]["p95"] or 0
            activity = target_row["observed_trade_notional_usd_per_hour"]
            median_depth = depth["median"] or 0
            score = (
                half_spread
                * math.log1p(activity / 10_000)
                * math.log1p(median_depth / 1_000)
                / (centered_gap + p95_move_500ms + 0.10)
            )
            ranking.append({
                "venue": venue,
                "asset": asset,
                "research_score": score,
                "median_spread_bps": spread["median"],
                "median_half_spread_bps": half_spread,
                "median_min_depth_5bps_usd": depth["median"],
                "observed_trade_notional_usd_per_hour": activity,
                "p95_centered_target_reference_gap_bps": centered_gap,
                "p95_reference_move_500ms_bps": p95_move_500ms,
                "p99_reference_move_500ms_bps": p99_move_500ms,
                "conservative_fee_budget_bps_per_side": half_spread - p99_move_500ms,
                "model_rmse_improvement_pct": model.get("rmse_improvement_pct"),
                "model_direction_accuracy": model.get("active_direction_accuracy"),
                "coverage_pct": target_row["coverage_pct"],
            })
    ranking.sort(key=lambda row: row["research_score"], reverse=True)

    spot_basis = []
    for venue in TARGET_VENUES:
        for asset in ASSETS:
            spot_components = [filled((spot, asset, "USDT")) for spot in SPOT_VENUES]
            spot_composite = _basis_normalized_composite(spot_components, minimum=1)
            values = _basis(filled((venue, asset, "USDT")), spot_composite)
            if values["observations"]:
                spot_basis.append({"venue": venue, "asset": asset, **values})
    spot_basis.sort(
        key=lambda row: row["absolute_centered_bps"]["p95"] or -1, reverse=True
    )

    stablecoin_basis = []
    for venue in ("binance", "bybit", "mexc", "binance_spot", "okx_spot"):
        for asset in ASSETS:
            values = _basis(filled((venue, asset, "USDC")), filled((venue, asset, "USDT")))
            if values["observations"]:
                stablecoin_basis.append({"venue": venue, "asset": asset, **values})
    stablecoin_basis.sort(
        key=lambda row: row["absolute_centered_bps"]["p95"] or -1, reverse=True
    )

    source_model_ranking = []
    for venue in TARGET_VENUES:
        for asset in ASSETS:
            for source, model in targets[venue][asset]["individual_source_models_500ms"].items():
                if model.get("rmse_improvement_pct") is None:
                    continue
                source_model_ranking.append({
                    "target_venue": venue,
                    "asset": asset,
                    "source": source,
                    "rmse_improvement_pct": model["rmse_improvement_pct"],
                    "direction_accuracy": model.get("active_direction_accuracy"),
                    "active_predictions": model.get("active_predictions", 0),
                    "observations": model.get("observations", 0),
                    "source_coverage_pct": model["source_coverage_pct"],
                })
    source_model_ranking.sort(
        key=lambda row: row["rmse_improvement_pct"], reverse=True
    )

    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "run": str(run),
        "window": {"start": analyzer.start.isoformat(), "end": analyzer.end.isoformat()},
        "method": {
            "clock": "local receive timestamp; comparisons use the same recorder clock",
            "grid": "100 ms last observation, carried forward for at most 1 second",
            "reference": (
                "basis-normalized median Binance/OKX/Bybit/MEXC USDT perpetual mid; "
                "at least two"
            ),
            "quality": "last valid target book per UTC second",
            "lead_model": (
                "70/30 chronological OLS at non-overlapping 500 ms intervals: future target "
                "return from reference 500 ms momentum and centered target/reference gap"
            ),
            "fee_budget": (
                "median half-spread minus p99 absolute 500 ms reference move; conservative "
                "latency-risk screen, not expected PnL"
            ),
        },
        "data_health": {
            "records_by_venue": dict(analyzer.records),
            "market_records_by_venue": dict(analyzer.market_records),
            "system_events": {
                f"{venue}.{stream}": count
                for (venue, stream), count in analyzer.system_events.items()
            },
        },
        "reference_risk": reference_risk,
        "targets": targets,
        "target_ranking": ranking,
        "individual_source_model_ranking": source_model_ranking,
        "spot_perpetual_basis": spot_basis,
        "usdc_usdt_basis": stablecoin_basis,
    }


def _number(value: typing.Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def markdown_report(report: dict) -> str:
    records = sum(report["data_health"]["records_by_venue"].values())
    lines = [
        "# Exploratory market-making report",
        "",
        f"Window: `{report['window']['start']}` to `{report['window']['end']}`. ",
        f"Parsed {records:,} in-window records from `{report['run']}`.",
        "",
        "This is a market-selection and signal-quality screen. It is not a fill backtest: public ",
        "feeds do not contain our queue position, order acknowledgements or private fills.",
        "",
        "## Feed health",
        "",
        "| Venue | Records | Market records |",
        "|---|---:|---:|",
    ]
    for venue in VENUES:
        lines.append(
            f"| {venue} | {report['data_health']['records_by_venue'].get(venue, 0):,} "
            f"| {report['data_health']['market_records_by_venue'].get(venue, 0):,} |"
        )
    lines.extend([
        "",
        "## Gate and HTX research priority",
        "",
        "| Rank | Market | Coverage | Median spread | 5 bps depth | Observed turnover/h "
        "| p99 500 ms move | Fee budget/side | 500 ms model OOS improvement |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for rank, row in enumerate(report["target_ranking"][:25], start=1):
        lines.append(
            f"| {rank} | {row['venue']} {row['asset']} | {_number(row['coverage_pct'], 1)}% "
            f"| {_number(row['median_spread_bps'])} bps "
            f"| ${_number(row['median_min_depth_5bps_usd'], 0)} "
            f"| ${_number(row['observed_trade_notional_usd_per_hour'], 0)} "
            f"| {_number(row['p99_reference_move_500ms_bps'])} bps "
            f"| {_number(row['conservative_fee_budget_bps_per_side'])} bps "
            f"| {_number(row['model_rmse_improvement_pct'])}% |"
        )
    lines.extend([
        "",
        "The fee budget is deliberately severe: median half-spread minus a p99 500 ms fair-price ",
        "move. Positive values identify markets that may tolerate both latency and a maker fee; ",
        "negative values are not proof of unprofitability because quoting can be selective.",
        "",
        "## Reference volatility",
        "",
        "| Asset | Coverage | p95 100 ms | p95 500 ms | p95 1 s | p95 5 s |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for asset in ASSETS:
        risk = report["reference_risk"][asset]
        lines.append(
            f"| {asset} | {_number(risk['coverage_pct'], 1)}% "
            f"| {_number(risk['absolute_move_100ms_bps']['p95'])} "
            f"| {_number(risk['absolute_move_500ms_bps']['p95'])} "
            f"| {_number(risk['absolute_move_1s_bps']['p95'])} "
            f"| {_number(risk['absolute_move_5s_bps']['p95'])} |"
        )
    model_rows = [
        row for row in report["target_ranking"]
        if row["model_rmse_improvement_pct"] is not None
    ]
    model_rows.sort(key=lambda row: row["model_rmse_improvement_pct"], reverse=True)
    lines.extend([
        "",
        "## Strongest 500 ms cross-venue models",
        "",
        "| Market | OOS RMSE improvement | Active direction accuracy | Active calls | p95 centered gap |",
        "|---|---:|---:|---:|---:|",
    ])
    for row in model_rows[:15]:
        lines.append(
            f"| {row['venue']} {row['asset']} "
            f"| {_number(row['model_rmse_improvement_pct'])}% "
            f"| {_number(100 * row['model_direction_accuracy'] if row['model_direction_accuracy'] is not None else None, 1)}% "
            f"| {report['targets'][row['venue']][row['asset']]['lead_model_500ms'].get('active_predictions', 0):,} "
            f"| {_number(row['p95_centered_target_reference_gap_bps'])} bps |"
        )
    source_rows = [
        row for row in report["individual_source_model_ranking"]
        if row["active_predictions"] >= 20 and row["source_coverage_pct"] >= 80
    ]
    lines.extend([
        "",
        "## Strongest individual source signals",
        "",
        "| Target | Source | OOS RMSE improvement | Direction accuracy | Active calls |",
        "|---|---|---:|---:|---:|",
    ])
    for row in source_rows[:20]:
        lines.append(
            f"| {row['target_venue']} {row['asset']} | {row['source']} "
            f"| {_number(row['rmse_improvement_pct'])}% "
            f"| {_number(100 * row['direction_accuracy'] if row['direction_accuracy'] is not None else None, 1)}% "
            f"| {row['active_predictions']:,} |"
        )
    lines.extend([
        "",
        "## Largest spot/perpetual basis variation",
        "",
        "| Market | Median perp premium | p95 movement around median | Observations |",
        "|---|---:|---:|---:|",
    ])
    for row in report["spot_perpetual_basis"][:12]:
        lines.append(
            f"| {row['venue']} {row['asset']} | {_number(row['median_bps'])} bps "
            f"| {_number(row['absolute_centered_bps']['p95'])} bps "
            f"| {row['observations']:,} |"
        )
    lines.extend([
        "",
        "## Largest USDC/USDT basis variation",
        "",
        "| Venue/asset | Median USDC premium | p95 movement around median | Observations |",
        "|---|---:|---:|---:|",
    ])
    for row in report["usdc_usdt_basis"][:12]:
        lines.append(
            f"| {row['venue']} {row['asset']} | {_number(row['median_bps'])} bps "
            f"| {_number(row['absolute_centered_bps']['p95'])} bps "
            f"| {row['observations']:,} |"
        )
    lines.extend([
        "",
        "## Interpretation limits",
        "",
        "- The ranking is a research priority, not expected PnL.",
        "- Quoted spread is realizable only when both fills occur without excessive adverse selection.",
        "- Fee tables are account-specific; JSON includes round-trip gross edge at 0–4 bps per side.",
        "- Validate signals over several days and volatility regimes before trading them.",
        "- Start a private shadow-order recorder before estimating fill probability or queue position.",
        "",
    ])
    return "\n".join(lines)


def _utc(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--start", type=_utc, required=True)
    parser.add_argument("--end", type=_utc, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    if arguments.end <= arguments.start:
        raise SystemExit("--end must be after --start")
    analyzer = Analyzer(arguments.start, arguments.end)
    manifest_path = arguments.run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_start = _utc(manifest["started_at"]).replace(minute=0, second=0, microsecond=0)
    for venue in VENUES:
        read_start = run_start if venue == "htx" else arguments.start
        venue_files = _files(arguments.run, venue, read_start, arguments.end)
        print(f"Reading {venue}: {len(venue_files)} file(s)", flush=True)
        for path in venue_files:
            with gzip.open(path, "rb") as input_file:
                for line in input_file:
                    analyzer.consume(venue, _loads(line))
    report = build_report(analyzer, arguments.run)
    arguments.output.mkdir(parents=True, exist_ok=True)
    stem = f"exploratory-market-making-{arguments.start:%Y%m%dT%H%MZ}-{arguments.end:%Y%m%dT%H%MZ}"
    json_path, markdown_path = arguments.output / f"{stem}.json", arguments.output / f"{stem}.md"
    with json_path.open("w", encoding="utf-8") as output_file:
        json.dump(report, output_file, indent=2, allow_nan=False)
        output_file.write("\n")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    print(f"Wrote {json_path}", flush=True)
    print(f"Wrote {markdown_path}", flush=True)


if __name__ == "__main__":
    main()
