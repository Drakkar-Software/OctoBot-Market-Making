import gzip
import json
import pathlib
import tempfile
import time

import pytest

from octobot_market_making.data_collector import adapters
from octobot_market_making.data_collector.config import CollectorConfig
from octobot_market_making.data_collector import historical
from octobot_market_making.data_collector.storage import GzipJsonlWriter
from octobot_market_making.data_collector import rest


def test_config_normalizes_and_validates_values():
    config = CollectorConfig(
        symbols=("sol", "xrp", "sol"),
        venues=("Gate", "htx"),
        quote_assets=("usdt", "usdc", "usdt"),
    )
    assert config.symbols == ("SOL", "XRP")
    assert config.venues == ("gate", "htx")
    assert config.quote_assets == ("USDT", "USDC")
    with pytest.raises(ValueError):
        CollectorConfig(symbols=("SOL/USDT",))
    with pytest.raises(ValueError):
        CollectorConfig(venues=("unknown",))
    with pytest.raises(ValueError):
        CollectorConfig(quote_assets=("USD",))


def test_adapters_build_expected_market_subscriptions():
    venue_adapters = {
        adapter.name: adapter
        for adapter in adapters.build_adapters(("gate", "htx"), ("SOL",))
    }
    gate_messages = venue_adapters["gate"].subscriptions
    assert any(
        message.get("channel") == "futures.order_book_update"
        and message["payload"] == ["SOL_USDT", "100ms", "20"]
        for message in gate_messages
    )
    htx_messages = venue_adapters["htx"].subscriptions
    assert any(
        message.get("sub") == "market.sol-usdt.depth.size_20.high_freq"
        for message in htx_messages
    )
    assert venue_adapters["htx"].websocket_ping_interval is None
    assert venue_adapters["htx"].receive_timeout == 30


def test_binance_uses_separate_public_and_market_connections():
    binance_adapters = adapters.build_adapters(("binance",), ("SOL",))
    assert [adapter.connection_label for adapter in binance_adapters] == ["public", "market"]
    assert binance_adapters[0].url.endswith("/public/ws")
    assert binance_adapters[1].url.endswith("/market/ws")
    assert binance_adapters[0].subscriptions[0]["params"] == ["solusdt@depth20@100ms"]
    assert "solusdt@aggTrade" in binance_adapters[1].subscriptions[0]["params"]


def test_usdc_market_allowlists_omit_unsupported_pairs():
    binance = adapters.market_specs(
        "binance", ("SOL", "HYPE"), ("USDT", "USDC")
    )
    assert [market.symbol for market in binance] == ["SOLUSDT", "SOLUSDC", "HYPEUSDT"]
    bybit = adapters.market_specs("bybit", ("ADA", "HYPE"), ("USDT", "USDC"))
    assert [market.symbol for market in bybit] == ["ADAUSDT", "HYPEUSDT", "HYPEPERP"]
    binance_spot = adapters.market_specs(
        "binance_spot", ("BTC", "HYPE"), ("USDT", "USDC")
    )
    assert [market.symbol for market in binance_spot] == ["BTCUSDT", "BTCUSDC"]
    okx_spot = adapters.market_specs("okx_spot", ("BTC",), ("USDT", "USDC"))
    assert [market.symbol for market in okx_spot] == ["BTC-USDT", "BTC-USDC"]
    mexc = adapters.market_specs("mexc", ("BTC", "FIL"), ("USDT", "USDC"))
    assert [market.symbol for market in mexc] == ["BTC_USDT", "BTC_USDC", "FIL_USDC"]


def test_hyperliquid_subscriptions_and_metadata():
    venue_adapters = adapters.build_adapters(
        ("hyperliquid",), ("BTC",), ("USDT", "USDC")
    )
    assert len(venue_adapters) == 1
    subscriptions = venue_adapters[0].subscriptions
    assert {item["subscription"]["type"] for item in subscriptions} == {
        "l2Book",
        "trades",
        "activeAssetCtx",
    }
    payload = {
        "channel": "l2Book",
        "data": {"coin": "BTC", "time": 1_700_000_000_123, "levels": [[], []]},
    }
    assert adapters.message_metadata("hyperliquid", payload) == (
        "l2Book",
        "BTC-USDC",
        1_700_000_000_123,
    )
    assert adapters.market_identity("hyperliquid", "BTC-USDC") == ("BTC", "USDC")
    assert adapters.market_identity("bybit", "BTCPERP") == ("BTC", "USDC")
    assert adapters.market_identity("binance", "BTCUSDT") == ("BTC", "USDT")
    assert adapters.market_identity("okx_spot", "BTC-USDC") == ("BTC", "USDC")


def test_mexc_subscriptions_and_metadata():
    venue_adapters = adapters.build_adapters(
        ("mexc",), ("BTC",), ("USDT", "USDC")
    )
    assert len(venue_adapters) == 1
    methods = {item["method"] for item in venue_adapters[0].subscriptions}
    assert methods == {"sub.depth.full", "sub.deal", "sub.ticker"}
    assert venue_adapters[0].websocket_ping_interval is None
    payload = {
        "channel": "push.depth.full",
        "symbol": "BTC_USDC",
        "ts": 1_700_000_000_123,
        "data": {"asks": [], "bids": [], "version": 1},
    }
    assert adapters.message_metadata("mexc", payload) == (
        "push.depth.full",
        "BTC_USDC",
        1_700_000_000_123,
    )
    assert adapters.market_identity("mexc", "BTC_USDC") == ("BTC", "USDC")


def test_htx_gzip_and_ping_decoding():
    adapter = adapters.build_adapters(("htx",), ("SOL",))[0]
    market_message = gzip.compress(
        json.dumps({"ch": "market.sol-usdt.trade.detail", "ts": 123}).encode()
    )
    decoded = adapter.decode(market_message)
    assert decoded.payload["ts"] == 123
    heartbeat = adapter.decode(gzip.compress(json.dumps({"ping": 456}).encode()))
    assert heartbeat.payload is None
    assert json.loads(heartbeat.replies[0]) == {"pong": 456}


def test_message_metadata_extracts_stream_symbol_and_timestamp():
    payload = {"stream": "solusdt@aggTrade", "data": {"s": "SOLUSDT", "E": 1_700_000_000_123}}
    assert adapters.message_metadata("binance", payload) == (
        "solusdt@aggTrade",
        "SOLUSDT",
        1_700_000_000_123,
    )
    raw_payload = {"e": "markPriceUpdate", "s": "SOLUSDT", "E": 1_700_000_000_456}
    assert adapters.message_metadata("binance", raw_payload) == (
        "markPriceUpdate",
        "SOLUSDT",
        1_700_000_000_456,
    )
    spot_bbo = {
        "stream": "btcusdc@bookTicker",
        "data": {"s": "BTCUSDC", "b": "1", "a": "2"},
    }
    assert adapters.message_metadata("binance_spot", spot_bbo) == (
        "btcusdc@bookTicker",
        "BTCUSDC",
        None,
    )


def test_gzip_writer_creates_readable_partitioned_jsonl():
    with tempfile.TemporaryDirectory() as temporary_directory:
        run_dir = pathlib.Path(temporary_directory)
        record = {
            "venue": "gate",
            "received_ts_ns": time.time_ns(),
            "payload": {"channel": "futures.trades"},
        }
        with GzipJsonlWriter(run_dir, rotation_minutes=60, compression_level=1) as writer:
            path = writer.write(record)
            writer.flush()
        with gzip.open(path, "rt", encoding="utf-8") as input_file:
            assert json.loads(input_file.readline()) == record


def test_rest_requests_cover_target_futures_state():
    requests = rest.build_requests(("gate", "htx", "okx"), ("SOL",), 60, 3600)
    streams_by_venue = {}
    for request in requests:
        streams_by_venue.setdefault(request.venue, set()).add(request.stream)
    assert streams_by_venue["gate"] == {
        "rest.contract_info",
        "rest.order_book",
        "rest.server_time",
    }
    assert streams_by_venue["htx"] == {
        "rest.contract_info",
        "rest.funding_rate",
        "rest.open_interest",
        "rest.index",
        "rest.server_time",
    }
    assert streams_by_venue["okx"] == {
        "rest.contract_info",
        "rest.funding_rate",
        "rest.open_interest",
        "rest.mark_price",
        "rest.server_time",
    }


def test_rest_requests_include_binance_and_hyperliquid_metadata():
    requests = rest.build_requests(
        ("binance", "hyperliquid"),
        ("BTC",),
        60,
        3600,
        ("USDT", "USDC"),
    )
    by_stream = {request.stream: request for request in requests}
    assert by_stream["rest.exchange_info"].method == "GET"
    hyperliquid = by_stream["rest.meta_and_asset_contexts"]
    assert hyperliquid.method == "POST"
    assert hyperliquid.body == {"type": "metaAndAssetCtxs"}


def test_okx_spot_uses_one_catalog_request_instead_of_per_market_requests():
    requests = rest.build_requests(
        ("okx_spot",), ("BTC", "ETH"), 60, 3600, ("USDT", "USDC")
    )
    assert [request.stream for request in requests] == [
        "rest.server_time",
        "rest.exchange_info",
    ]


def test_historical_binance_paths_are_partitioned_and_checksum_verified():
    remote = historical._recent_binance_file(  # pylint: disable=protected-access
        "trades", None, "SOLUSDT", historical.datetime.date(2026, 8, 3)
    )
    assert str(remote.relative_path).endswith(
        "daily/trades/SOLUSDT/SOLUSDT-trades-2026-08-03.zip"
    )
    assert remote.checksum_url == f"{remote.url}.CHECKSUM"


def test_historical_kline_path_contains_interval():
    remote = historical._recent_binance_file(  # pylint: disable=protected-access
        "markPriceKlines", "1m", "XRPUSDT", historical.datetime.date(2026, 8, 3)
    )
    assert str(remote.relative_path).endswith(
        "daily/markPriceKlines/XRPUSDT/1m/XRPUSDT-1m-2026-08-03.zip"
    )


def test_htx_checksum_replaces_zip_suffix():
    remote = historical._htx_remote_file(  # pylint: disable=protected-access
        "data/trades/linear-swap/daily/SOL-USDT/"
        "SOL-USDT-trades-2026-08-03.zip",
        75_012,
    )
    assert remote.checksum_url.endswith(
        "SOL-USDT-trades-2026-08-03.CHECKSUM"
    )
    assert remote.size == 75_012
