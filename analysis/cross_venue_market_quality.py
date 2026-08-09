"""Measure spread, depth, update rate, and turnover on all captured futures venues."""

import argparse
import collections
import datetime
import gzip
import json
import pathlib
import typing

from exploratory_market_making import (
    Book,
    _files,
    _loads,
    _number,
    _quote_from_levels,
    _summary,
    _utc,
)


FUTURES_VENUES = ("binance", "okx", "bybit", "mexc", "hyperliquid")
DEFAULT_ASSETS = ("BCH", "SOL", "DOGE", "XRP", "LTC", "ADA", "AVAX", "HYPE", "ENA", "XLM")


def _parse_windows(value: str) -> tuple[tuple[datetime.datetime, datetime.datetime], ...]:
    windows = []
    for item in value.split(","):
        start, end = item.split("/", maxsplit=1)
        windows.append((_utc(start), _utc(end)))
    return tuple(windows)


def _parse_assets(value: str) -> tuple[str, ...]:
    return tuple(item.strip().upper() for item in value.split(",") if item.strip())


class QualityAnalyzer:
    def __init__(
        self,
        windows: tuple[tuple[datetime.datetime, datetime.datetime], ...],
        assets: typing.Iterable[str],
    ) -> None:
        self.windows = windows
        self.assets = frozenset(assets)
        self.window_ns = tuple(
            (int(start.timestamp() * 1e9), int(end.timestamp() * 1e9))
            for start, end in windows
        )
        self.duration_seconds = sum((end - start).total_seconds() for start, end in windows)
        self.books: dict[tuple[str, str, str], Book] = collections.defaultdict(Book)
        self.quality: dict[
            tuple[str, str, str],
            dict[int, tuple[float, float, float]],
        ] = collections.defaultdict(dict)
        self.contract_multipliers: dict[tuple[str, str, str], float] = {}
        self.book_updates: collections.Counter = collections.Counter()
        self.trade_events: collections.Counter = collections.Counter()
        self.trade_price_quantity: collections.Counter = collections.Counter()
        self.records: collections.Counter = collections.Counter()
        self.book_receive_latency_ms: dict[
            tuple[str, str, str], list[float]
        ] = collections.defaultdict(list)

    def _in_windows(self, timestamp_ns: int) -> bool:
        return any(start <= timestamp_ns < end for start, end in self.window_ns)

    def _contract_info(self, venue: str, record: dict) -> None:
        payload = record.get("payload", {})
        try:
            if venue == "okx":
                base, quote = record.get("base_asset"), record.get("quote_asset")
                data = payload.get("data") or []
                if base and quote and data:
                    self.contract_multipliers[(venue, base, quote)] = float(data[0]["ctVal"])
            elif venue == "mexc":
                for item in payload.get("data") or ():
                    symbol = str(item.get("symbol", ""))
                    if "_" in symbol:
                        base, quote = symbol.rsplit("_", 1)
                        self.contract_multipliers[(venue, base, quote)] = float(item["contractSize"])
        except (KeyError, TypeError, ValueError):
            return

    def _record_quote(
        self,
        key: tuple[str, str, str],
        quote: typing.Optional[tuple[float, float, float, float, float, float]],
        timestamp_ns: int,
    ) -> None:
        if quote is None or not self._in_windows(timestamp_ns):
            return
        bid, ask, bid_quantity, ask_quantity, bid_depth, ask_depth = quote
        mid = (bid + ask) / 2
        if mid <= 0 or ask <= bid:
            return
        second = timestamp_ns // 1_000_000_000
        self.quality[key][second] = (
            (ask - bid) / mid * 10_000,
            min(bid * bid_quantity, ask * ask_quantity),
            min(bid_depth, ask_depth),
        )
        self.book_updates[key] += 1

    def _record_trades(self, venue: str, key: tuple[str, str, str], payload: dict) -> None:
        rows: typing.Iterable = ()
        price_key = quantity_key = None
        if venue == "binance":
            rows, price_key, quantity_key = (payload,), "p", "q"
        elif venue == "okx":
            rows, price_key, quantity_key = payload.get("data") or (), "px", "sz"
        elif venue == "bybit":
            rows, price_key, quantity_key = payload.get("data") or (), "p", "v"
        elif venue == "mexc":
            rows, price_key, quantity_key = payload.get("data") or (), "p", "v"
        elif venue == "hyperliquid":
            rows, price_key, quantity_key = payload.get("data") or (), "px", "sz"
        if price_key is None:
            return
        for row in rows:
            try:
                self.trade_price_quantity[key] += abs(
                    float(row[price_key]) * float(row[quantity_key])
                )
                self.trade_events[key] += 1
            except (KeyError, TypeError, ValueError):
                continue

    def consume(self, venue: str, record: dict) -> None:
        timestamp_ns = int(record.get("received_ts_ns") or 0)
        if record.get("record_type") == "rest" and record.get("stream") == "rest.contract_info":
            self._contract_info(venue, record)
        if not self._in_windows(timestamp_ns):
            return
        self.records[venue] += 1
        if record.get("record_type") != "market":
            return
        base, quote_asset = record.get("base_asset"), record.get("quote_asset")
        if not base or base not in self.assets or quote_asset not in {"USDT", "USDC"}:
            return
        key = venue, base, quote_asset
        payload = record.get("payload", {})
        stream = str(record.get("stream", ""))
        direct_quote = None

        if venue == "binance":
            if stream == "depthUpdate":
                direct_quote = _quote_from_levels(payload.get("b"), payload.get("a"))
            elif stream == "aggTrade":
                self._record_trades(venue, key, payload)
        elif venue == "okx":
            if stream == "books5":
                data = payload.get("data") or []
                if data:
                    direct_quote = _quote_from_levels(data[0].get("bids"), data[0].get("asks"))
            elif stream == "trades":
                self._record_trades(venue, key, payload)
        elif venue == "bybit":
            if stream.startswith("orderbook.50."):
                data = payload.get("data", {})
                if payload.get("type") == "snapshot":
                    self.books[key].replace(data.get("b"), data.get("a"))
                else:
                    self.books[key].update(data.get("b"), data.get("a"))
                direct_quote = self.books[key].quote()
            elif stream.startswith("publicTrade."):
                self._record_trades(venue, key, payload)
        elif venue == "mexc":
            if stream == "push.depth.full":
                data = payload.get("data", {})
                direct_quote = _quote_from_levels(data.get("bids"), data.get("asks"))
            elif stream == "push.deal":
                self._record_trades(venue, key, payload)
        elif venue == "hyperliquid":
            if stream == "l2Book":
                levels = payload.get("data", {}).get("levels") or []
                if len(levels) >= 2:
                    direct_quote = _quote_from_levels(levels[0], levels[1])
            elif stream == "trades":
                self._record_trades(venue, key, payload)
        self._record_quote(key, direct_quote, timestamp_ns)
        exchange_ts_ms = record.get("exchange_ts_ms")
        if direct_quote is not None and exchange_ts_ms is not None:
            latency = timestamp_ns / 1_000_000 - float(exchange_ts_ms)
            if -5_000 < latency < 60_000:
                self.book_receive_latency_ms[key].append(latency)

    def report(self, run: pathlib.Path) -> dict:
        markets = []
        keys = sorted(set(self.quality) | set(self.trade_events))
        for key in keys:
            venue, asset, quote = key
            samples = list(self.quality[key].values())
            multiplier = self.contract_multipliers.get(key, 1.0)
            spreads = [sample[0] for sample in samples]
            top_depth = [sample[1] * multiplier for sample in samples]
            depth_5bps = [sample[2] * multiplier for sample in samples]
            turnover = self.trade_price_quantity[key] * multiplier
            markets.append({
                "venue": venue,
                "asset": asset,
                "quote": quote,
                "coverage_pct": 100 * len(samples) / self.duration_seconds,
                "book_updates_per_second": self.book_updates[key] / self.duration_seconds,
                "book_receive_latency_ms": _summary(self.book_receive_latency_ms[key]),
                "spread_bps": _summary(spreads),
                "top_min_usd": _summary(top_depth),
                "min_depth_5bps_usd": _summary(depth_5bps),
                "trade_events_per_second": self.trade_events[key] / self.duration_seconds,
                "observed_trade_notional_usd_per_hour": turnover * 3600 / self.duration_seconds,
                "contract_multiplier": multiplier,
            })
        return {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "run": str(run),
            "windows": [
                {"start": start.isoformat(), "end": end.isoformat()}
                for start, end in self.windows
            ],
            "method": {
                "clock": "local receive timestamp",
                "quality_sampling": "last full-book observation in each UTC second; coverage is the share of seconds containing an update",
                "depth": "smaller bid/ask displayed notional within 5 bps of mid",
                "turnover": "sum of public trade price times size, adjusted by contract multiplier",
                "important_limit": "displayed public quality, not our queue position or executable fill PnL",
            },
            "records_by_venue": dict(self.records),
            "markets": markets,
        }


def markdown_report(report: dict) -> str:
    rows = sorted(
        report["markets"],
        key=lambda row: (row["venue"], row["asset"], row["quote"]),
    )
    lines = [
        "# Cross-venue futures market quality",
        "",
        "The two sampled hours use the recorder's local receive clock. Turnover is observed ",
        "public trade flow, while depth is displayed within 5 bps; neither is a fill guarantee.",
        "",
        "| Market | Seconds updated | Updates/s | Median receive lag | Median spread | Median top min | Median 5bps depth min | Turnover/hour | Trades/s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['venue']} {row['asset']}-{row['quote']} "
            f"| {_number(row['coverage_pct'], 1)}% "
            f"| {_number(row['book_updates_per_second'])} "
            f"| {_number(row['book_receive_latency_ms']['median'], 0)} ms "
            f"| {_number(row['spread_bps']['median'])} bps "
            f"| ${_number(row['top_min_usd']['median'], 0)} "
            f"| ${_number(row['min_depth_5bps_usd']['median'], 0)} "
            f"| ${_number(row['observed_trade_notional_usd_per_hour'], 0)} "
            f"| {_number(row['trade_events_per_second'])} |"
        )
    lines.extend([
        "",
        "Low turnover is a capacity constraint, not an automatic rejection. For a small account, ",
        "the relevant follow-up is our fill rate and post-fill markout at the intended quote size.",
        "",
    ])
    return "\n".join(lines)


def analyze(
    run: pathlib.Path,
    windows: tuple[tuple[datetime.datetime, datetime.datetime], ...],
    assets: tuple[str, ...],
    output: pathlib.Path,
) -> dict:
    analyzer = QualityAnalyzer(windows, assets)
    for venue in FUTURES_VENUES:
        paths = sorted({
            path
            for start, end in windows
            for path in _files(run, venue, start, end)
        })
        print(f"Reading {venue}: {len(paths)} file(s)", flush=True)
        for path in paths:
            with gzip.open(path, "rb") as input_file:
                for line in input_file:
                    analyzer.consume(venue, _loads(line))
    report = analyzer.report(run)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
    return report


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=pathlib.Path, required=True)
    parser.add_argument("--windows", type=_parse_windows, required=True)
    parser.add_argument("--assets", type=_parse_assets, default=DEFAULT_ASSETS)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    report = analyze(arguments.run, tuple(arguments.windows), tuple(arguments.assets), arguments.output)
    print(f"Wrote {arguments.output}", flush=True)
    print(f"Wrote {arguments.output.with_suffix('.md')}", flush=True)
    print(f"Measured {len(report['markets'])} futures markets", flush=True)


if __name__ == "__main__":
    main()
