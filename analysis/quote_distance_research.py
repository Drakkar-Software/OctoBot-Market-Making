"""Estimate touch rates and markouts for passive quotes placed behind the BBO.

This is a no-queue upper-bound study.  At fixed intervals it places hypothetical
bid and ask quotes at several distances from the then-current mid, observes public
trades for five seconds, and records whether each quote price was touched.  It does
not claim a touch would have filled our order.
"""

import argparse
import collections
import datetime
import gzip
import json
import math
import pathlib
import typing

import numpy as np

from exploratory_market_making import Book, _files, _loads, _quote_from_levels, _utc


GRID_MS = 100
STALE_BUCKETS = 10
HORIZON_BUCKETS = 50  # five seconds
DECISION_BUCKETS = 50  # non-overlapping five-second quote opportunities
MARKOUT_BUCKETS = (10, 50)  # one and five seconds after the first touch
DEFAULT_TARGETS = (
    ("gate", "SOL", "USDT"),
    ("mexc", "SOL", "USDT"),
    ("mexc", "SOL", "USDC"),
    ("mexc", "DOGE", "USDT"),
    ("mexc", "BCH", "USDT"),
    ("mexc", "ADA", "USDT"),
)
DEFAULT_DISTANCES_BPS = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0)


def _parse_windows(value: str) -> tuple[tuple[datetime.datetime, datetime.datetime], ...]:
    result = []
    for item in value.split(","):
        start, end = item.split("/", maxsplit=1)
        result.append((_utc(start), _utc(end)))
    return tuple(result)


def _parse_targets(value: str) -> tuple[tuple[str, str, str], ...]:
    result = []
    for item in value.split(","):
        venue, asset, quote = item.strip().split(":", maxsplit=2)
        result.append((venue.lower(), asset.upper(), quote.upper()))
    return tuple(result)


def _parse_distances(value: str) -> tuple[float, ...]:
    return tuple(float(item) for item in value.split(","))


class WindowSeries:
    def __init__(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
        targets: tuple[tuple[str, str, str], ...],
    ) -> None:
        self.start = start
        self.end = end
        self.start_ns = int(start.timestamp() * 1e9)
        self.end_ns = int(end.timestamp() * 1e9)
        self.start_bucket = int(start.timestamp() * 1_000) // GRID_MS
        self.length = int((end - start).total_seconds() * 1_000) // GRID_MS
        self.targets = frozenset(targets)
        self.books: dict[tuple[str, str, str], Book] = collections.defaultdict(Book)
        self.mids = {
            target: np.full(self.length, np.nan, dtype=np.float64) for target in targets
        }
        self.half_spreads = {
            target: np.full(self.length, np.nan, dtype=np.float64) for target in targets
        }
        self.trade_min = {
            target: np.full(self.length, np.inf, dtype=np.float64) for target in targets
        }
        self.trade_max = {
            target: np.full(self.length, -np.inf, dtype=np.float64) for target in targets
        }

    def _index(self, timestamp_ns: int) -> typing.Optional[int]:
        if not self.start_ns <= timestamp_ns < self.end_ns:
            return None
        index = timestamp_ns // (GRID_MS * 1_000_000) - self.start_bucket
        return int(index) if 0 <= index < self.length else None

    def _record_quote(
        self,
        key: tuple[str, str, str],
        quote: typing.Optional[tuple[float, float, float, float, float, float]],
        timestamp_ns: int,
    ) -> None:
        index = self._index(timestamp_ns)
        if key not in self.targets or index is None or quote is None:
            return
        bid, ask = quote[:2]
        mid = (bid + ask) / 2
        self.mids[key][index] = mid
        self.half_spreads[key][index] = (ask - bid) / mid * 5_000

    def _record_trade(
        self, key: tuple[str, str, str], price: typing.Any, timestamp_ns: int
    ) -> None:
        index = self._index(timestamp_ns)
        if key not in self.targets or index is None:
            return
        try:
            numeric = float(price)
        except (TypeError, ValueError):
            return
        self.trade_min[key][index] = min(self.trade_min[key][index], numeric)
        self.trade_max[key][index] = max(self.trade_max[key][index], numeric)

    def consume(self, venue: str, record: dict) -> None:
        timestamp_ns = int(record.get("received_ts_ns") or 0)
        base, quote_asset = record.get("base_asset"), record.get("quote_asset")
        if not base or not quote_asset:
            return
        key = venue, base, quote_asset
        if key not in self.targets:
            return
        record_type = record.get("record_type")
        stream = str(record.get("stream", ""))
        payload = record.get("payload", {})

        if venue == "gate":
            if record_type == "rest" and stream == "rest.order_book":
                self.books[key].replace(payload.get("bids"), payload.get("asks"))
                self._record_quote(key, self.books[key].quote(), timestamp_ns)
            elif record_type == "market" and stream == "futures.order_book_update":
                result = payload.get("result", {})
                self.books[key].update(result.get("b"), result.get("a"))
                self._record_quote(key, self.books[key].quote(), timestamp_ns)
            elif record_type == "market" and stream == "futures.trades":
                for trade in payload.get("result") or ():
                    self._record_trade(key, trade.get("price"), timestamp_ns)
        elif venue == "mexc" and record_type == "market":
            if stream == "push.depth.full":
                data = payload.get("data", {})
                self._record_quote(
                    key,
                    _quote_from_levels(data.get("bids"), data.get("asks")),
                    timestamp_ns,
                )
            elif stream == "push.deal":
                for trade in payload.get("data") or ():
                    self._record_trade(key, trade.get("p"), timestamp_ns)

    def filled_mid_and_spread(
        self, key: tuple[str, str, str]
    ) -> tuple[np.ndarray, np.ndarray]:
        mid = np.full(self.length, np.nan)
        spread = np.full(self.length, np.nan)
        last_mid = last_spread = np.nan
        last_index = -STALE_BUCKETS - 1
        for index, value in enumerate(self.mids[key]):
            if np.isfinite(value):
                last_mid = value
                last_spread = self.half_spreads[key][index]
                last_index = index
            if index - last_index <= STALE_BUCKETS:
                mid[index] = last_mid
                spread[index] = last_spread
        return mid, spread


def _first_touch(
    values: np.ndarray, threshold: float, side: str
) -> typing.Optional[int]:
    if side == "bid":
        touched = np.flatnonzero(np.isfinite(values) & (values <= threshold))
    else:
        touched = np.flatnonzero(np.isfinite(values) & (values >= threshold))
    return int(touched[0]) if touched.size else None


def _evaluate_window(
    series: WindowSeries,
    distances_bps: tuple[float, ...],
) -> dict[tuple[tuple[str, str, str], float], dict[str, list[float] | int]]:
    results: dict[
        tuple[tuple[str, str, str], float], dict[str, list[float] | int]
    ] = {}
    for key in series.targets:
        mids, half_spreads = series.filled_mid_and_spread(key)
        for distance in distances_bps:
            result: dict[str, list[float] | int] = {
                "eligible_orders": 0,
                "touched_orders": 0,
                "edge_1s_bps": [],
                "edge_5s_bps": [],
            }
            for start in range(0, series.length - HORIZON_BUCKETS - max(MARKOUT_BUCKETS), DECISION_BUCKETS):
                placement_mid = mids[start]
                if not np.isfinite(placement_mid) or not np.isfinite(half_spreads[start]):
                    continue
                # A chosen distance inside the current touch would not be post-only.
                if distance + 1e-12 < half_spreads[start]:
                    continue
                bid_quote = placement_mid * (1 - distance / 10_000)
                ask_quote = placement_mid * (1 + distance / 10_000)
                trade_slice = slice(start + 1, start + HORIZON_BUCKETS + 1)
                for side, quote, trades in (
                    ("bid", bid_quote, series.trade_min[key][trade_slice]),
                    ("ask", ask_quote, series.trade_max[key][trade_slice]),
                ):
                    result["eligible_orders"] += 1
                    relative_touch = _first_touch(trades, quote, side)
                    if relative_touch is None:
                        continue
                    result["touched_orders"] += 1
                    touch_index = start + 1 + relative_touch
                    for markout_bucket, field in (
                        (MARKOUT_BUCKETS[0], "edge_1s_bps"),
                        (MARKOUT_BUCKETS[1], "edge_5s_bps"),
                    ):
                        markout_index = touch_index + markout_bucket
                        if markout_index >= len(mids) or not np.isfinite(mids[markout_index]):
                            continue
                        edge = (
                            (mids[markout_index] - quote) / placement_mid * 10_000
                            if side == "bid"
                            else (quote - mids[markout_index]) / placement_mid * 10_000
                        )
                        typing.cast(list[float], result[field]).append(float(edge))
            results[(key, distance)] = result
    return results


def _percentile(values: list[float], probability: float) -> typing.Optional[float]:
    return float(np.quantile(values, probability)) if values else None


def analyze(
    run: pathlib.Path,
    windows: tuple[tuple[datetime.datetime, datetime.datetime], ...],
    targets: tuple[tuple[str, str, str], ...],
    distances_bps: tuple[float, ...],
    output: pathlib.Path,
) -> dict:
    combined: dict[
        tuple[tuple[str, str, str], float], dict[str, list[float] | int]
    ] = {}
    for start, end in windows:
        series = WindowSeries(start, end, targets)
        for venue in sorted({target[0] for target in targets}):
            paths = _files(run, venue, start, end)
            print(f"Reading {venue} {start.isoformat()}: {len(paths)} file(s)", flush=True)
            for path in paths:
                with gzip.open(path, "rb") as input_file:
                    for line in input_file:
                        series.consume(venue, _loads(line))
        window_results = _evaluate_window(series, distances_bps)
        for key, values in window_results.items():
            target = combined.setdefault(key, {
                "eligible_orders": 0, "touched_orders": 0,
                "edge_1s_bps": [], "edge_5s_bps": [],
            })
            target["eligible_orders"] += typing.cast(int, values["eligible_orders"])
            target["touched_orders"] += typing.cast(int, values["touched_orders"])
            typing.cast(list, target["edge_1s_bps"]).extend(
                typing.cast(list, values["edge_1s_bps"])
            )
            typing.cast(list, target["edge_5s_bps"]).extend(
                typing.cast(list, values["edge_5s_bps"])
            )

    rows = []
    for (target, distance), values in sorted(combined.items()):
        eligible = typing.cast(int, values["eligible_orders"])
        touched = typing.cast(int, values["touched_orders"])
        edge_1s = typing.cast(list[float], values["edge_1s_bps"])
        edge_5s = typing.cast(list[float], values["edge_5s_bps"])
        mean_5s = statistics_fmean(edge_5s)
        rows.append({
            "venue": target[0], "asset": target[1], "quote": target[2],
            "distance_each_side_bps": distance,
            "full_quote_width_bps": 2 * distance,
            "eligible_orders": eligible,
            "touched_orders": touched,
            "five_second_touch_rate_pct": 100 * touched / eligible if eligible else None,
            "one_second_edge_bps": {
                "mean": statistics_fmean(edge_1s),
                "p10": _percentile(edge_1s, 0.10),
                "median": _percentile(edge_1s, 0.50),
            },
            "five_second_edge_bps": {
                "mean": mean_5s,
                "p10": _percentile(edge_5s, 0.10),
                "median": _percentile(edge_5s, 0.50),
            },
            "expected_edge_per_quote_opportunity_bps": (
                touched / eligible * mean_5s
                if eligible and mean_5s is not None else None
            ),
        })
    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "run": str(run),
        "windows": [
            {"start": start.isoformat(), "end": end.isoformat()}
            for start, end in windows
        ],
        "protocol": {
            "placement": "bid and ask centered on current mid at fixed distance; must be post-only versus observed BBO",
            "opportunity_spacing": "non-overlapping five-second intervals",
            "touch": "a public trade prints at or through the hypothetical quote within five seconds",
            "edge": "quote price versus target mid one/five seconds after first touch, before fees",
            "important_limits": [
                "touch is an upper bound, not a queue-conditioned fill",
                "zero order-entry and cancellation latency is assumed",
                "quotes are centered on target mid, not the cross-venue forecast or inventory reservation price",
                "public trades can be delayed relative to matching-engine execution",
            ],
        },
        "rows": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
    return report


def statistics_fmean(values: list[float]) -> typing.Optional[float]:
    return float(np.mean(values)) if values else None


def _number(value: typing.Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def markdown_report(report: dict) -> str:
    lines = [
        "# Passive quote-distance touch study",
        "",
        "Quotes are deliberately placed behind the observed BBO. A public trade touching the ",
        "price is only an upper bound on fill probability because queue position is unknown.",
        "",
        "| Target | Distance/side | Full quote width | 5s touch rate | Touches | Mean 1s edge | Mean 5s edge | 5s p10 edge | Edge/opportunity |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        lines.append(
            f"| {row['venue']} {row['asset']}-{row['quote']} "
            f"| {_number(row['distance_each_side_bps'])} bps "
            f"| {_number(row['full_quote_width_bps'])} bps "
            f"| {_number(row['five_second_touch_rate_pct'])}% "
            f"| {row['touched_orders']}/{row['eligible_orders']} "
            f"| {_number(row['one_second_edge_bps']['mean'])} bps "
            f"| {_number(row['five_second_edge_bps']['mean'])} bps "
            f"| {_number(row['five_second_edge_bps']['p10'])} bps "
            f"| {_number(row['expected_edge_per_quote_opportunity_bps'])} bps |"
        )
    lines.extend([
        "",
        "The correct live optimizer must replace touch rate with queue-conditioned fill intensity ",
        "and subtract the actual fee and fill-conditioned adverse markout. This table establishes ",
        "that quote width is a controllable boundary, not the exchange's inside spread.",
        "",
    ])
    return "\n".join(lines)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=pathlib.Path, required=True)
    parser.add_argument("--windows", type=_parse_windows, required=True)
    parser.add_argument("--targets", type=_parse_targets, default=DEFAULT_TARGETS)
    parser.add_argument("--distances-bps", type=_parse_distances, default=DEFAULT_DISTANCES_BPS)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    report = analyze(
        arguments.run, tuple(arguments.windows), tuple(arguments.targets),
        tuple(arguments.distances_bps), arguments.output,
    )
    print(f"Wrote {arguments.output}", flush=True)
    print(f"Wrote {arguments.output.with_suffix('.md')}", flush=True)
    print(f"Evaluated {len(report['rows'])} target/distance pairs", flush=True)


if __name__ == "__main__":
    main()
