import dataclasses
import gzip
import json
import time
import typing


JsonObject = dict[str, typing.Any]


@dataclasses.dataclass(frozen=True)
class MarketSpec:
    base: str
    quote: str
    symbol: str


# These are the USDC perpetuals present in the venue catalogs when this collector was
# deployed. USDT is the broad/default linear-perpetual universe. Keeping the narrower
# USDC allowlists prevents one unsupported topic from rejecting a batched subscription.
_BINANCE_USDC_BASES = frozenset(
    "BTC ETH BCH SOL DOGE XRP LTC ADA BNB SUI LINK AVAX AAVE UNI NEAR ENA ZEC FIL".split()
)
_BYBIT_USDC_BASES = frozenset(
    "BTC ETH BCH SOL DOGE XRP LTC BNB SUI LINK AAVE UNI HYPE ENA FIL XLM ONDO".split()
)
_BINANCE_SPOT_BASES = frozenset(
    "BTC ETH BCH SOL DOGE XRP LTC ADA BNB SUI LINK AVAX AAVE UNI NEAR ENA ZEC FIL XLM ONDO".split()
)
_MEXC_USDT_BASES = frozenset(
    "BTC ETH BCH SOL DOGE XRP LTC ADA BNB SUI LINK AVAX AAVE UNI HYPE NEAR ENA ZEC XLM ONDO".split()
)
_MEXC_USDC_BASES = frozenset(
    "BTC ETH BCH SOL DOGE XRP LTC ADA BNB SUI LINK AVAX AAVE UNI HYPE NEAR ENA ZEC FIL XLM ONDO".split()
)


@dataclasses.dataclass(frozen=True)
class DecodedMessage:
    payload: typing.Optional[JsonObject]
    replies: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class VenueAdapter:
    name: str
    url: str
    subscriptions: tuple[JsonObject, ...]
    connection_label: str = "market"
    heartbeat_interval: typing.Optional[float] = None
    heartbeat_payload: typing.Optional[typing.Callable[[], str]] = None
    websocket_ping_interval: typing.Optional[float] = 20
    websocket_ping_timeout: typing.Optional[float] = 20
    receive_timeout: typing.Optional[float] = None

    def decode(self, raw: typing.Union[str, bytes]) -> DecodedMessage:
        if self.name == "htx" and isinstance(raw, bytes):
            raw = gzip.decompress(raw).decode("utf-8")
        elif isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw == "pong":
            return DecodedMessage(None)
        payload = json.loads(raw)
        if self.name == "htx" and "ping" in payload:
            return DecodedMessage(None, (json.dumps({"pong": payload["ping"]}),))
        if self.name == "bybit" and payload.get("op") == "pong":
            return DecodedMessage(None)
        if self.name == "hyperliquid" and payload.get("channel") == "pong":
            return DecodedMessage(None)
        if self.name == "mexc" and payload.get("channel") == "pong":
            return DecodedMessage(None)
        return DecodedMessage(payload)


def _chunks(values: tuple[MarketSpec, ...], size: int) -> tuple[tuple[MarketSpec, ...], ...]:
    return tuple(values[index : index + size] for index in range(0, len(values), size))


def market_specs(
    venue: str, symbols: tuple[str, ...], quote_assets: tuple[str, ...]
) -> tuple[MarketSpec, ...]:
    markets = []
    for base in symbols:
        for quote in quote_assets:
            if venue in {"gate", "htx", "okx"} and quote != "USDT":
                continue
            if venue == "binance" and quote == "USDC" and base not in _BINANCE_USDC_BASES:
                continue
            if venue == "bybit" and quote == "USDC" and base not in _BYBIT_USDC_BASES:
                continue
            if venue == "hyperliquid" and quote != "USDC":
                continue
            if venue == "binance_spot" and base not in _BINANCE_SPOT_BASES:
                continue
            if venue == "mexc" and quote == "USDT" and base not in _MEXC_USDT_BASES:
                continue
            if venue == "mexc" and quote == "USDC" and base not in _MEXC_USDC_BASES:
                continue
            if venue in {"gate", "mexc"}:
                native_symbol = f"{base}_{quote}"
            elif venue in {"htx", "okx"}:
                suffix = "-SWAP" if venue == "okx" else ""
                native_symbol = f"{base}-{quote}{suffix}"
            elif venue == "bybit" and quote == "USDC":
                # Bybit displays these as BTCUSDC, etc., but the V5 WebSocket and
                # instruments API use legacy native symbols such as BTCPERP.
                native_symbol = f"{base}PERP"
            elif venue in {"binance", "bybit", "binance_spot"}:
                native_symbol = f"{base}{quote}"
            elif venue == "okx_spot":
                native_symbol = f"{base}-{quote}"
            else:
                native_symbol = base
            markets.append(MarketSpec(base, quote, native_symbol))
    return tuple(markets)


def _gate(markets: tuple[MarketSpec, ...]) -> VenueAdapter:
    gate_symbols = [market.symbol for market in markets]
    subscriptions = [
        {
            "time": int(time.time()),
            "channel": "futures.tickers",
            "event": "subscribe",
            "payload": gate_symbols,
        },
        {
            "time": int(time.time()),
            "channel": "futures.trades",
            "event": "subscribe",
            "payload": gate_symbols,
        },
    ]
    subscriptions.extend(
        {
            "time": int(time.time()),
            "channel": "futures.order_book_update",
            "event": "subscribe",
            "payload": [symbol, "100ms", "20"],
        }
        for symbol in gate_symbols
    )
    return VenueAdapter(
        "gate",
        "wss://fx-ws.gateio.ws/v4/ws/usdt",
        tuple(subscriptions),
        heartbeat_interval=15,
        heartbeat_payload=lambda: json.dumps({"time": int(time.time()), "channel": "futures.ping"}),
    )


def _htx(markets: tuple[MarketSpec, ...]) -> tuple[VenueAdapter, ...]:
    connections = []
    for index, market_chunk in enumerate(_chunks(markets, 10), start=1):
        subscriptions = []
        for market in market_chunk:
            contract = market.symbol.lower()
            subscriptions.extend(
                (
                    {
                        "sub": f"market.{contract}.depth.size_20.high_freq",
                        "data_type": "incremental",
                        "id": f"{market.symbol}-depth",
                    },
                    {
                        "sub": f"market.{contract}.trade.detail",
                        "id": f"{market.symbol}-trades",
                    },
                    {"sub": f"market.{contract}.detail", "id": f"{market.symbol}-detail"},
                )
            )
        connections.append(
            VenueAdapter(
                "htx",
                "wss://api.hbdm.com/linear-swap-ws",
                tuple(subscriptions),
                connection_label=f"market-{index}",
                websocket_ping_interval=None,
                websocket_ping_timeout=None,
                receive_timeout=30,
            )
        )
    return tuple(connections)


def _binance(markets: tuple[MarketSpec, ...]) -> tuple[VenueAdapter, VenueAdapter]:
    public_streams = []
    market_streams = []
    for market in markets:
        contract = market.symbol.lower()
        public_streams.append(f"{contract}@depth20@100ms")
        market_streams.extend(
            (
                f"{contract}@aggTrade",
                f"{contract}@markPrice@1s",
                f"{contract}@ticker",
                f"{contract}@forceOrder",
            )
        )
    return (
        VenueAdapter(
            "binance",
            "wss://fstream.binance.com/public/ws",
            ({"method": "SUBSCRIBE", "params": public_streams, "id": 1},),
            connection_label="public",
        ),
        VenueAdapter(
            "binance",
            "wss://fstream.binance.com/market/ws",
            ({"method": "SUBSCRIBE", "params": market_streams, "id": 1},),
            connection_label="market",
        ),
    )


def _okx(markets: tuple[MarketSpec, ...]) -> tuple[VenueAdapter, ...]:
    connections = []
    for index, market_chunk in enumerate(_chunks(markets, 20), start=1):
        arguments = [
            {"channel": channel, "instId": market.symbol}
            for market in market_chunk
            for channel in ("books5", "trades", "tickers")
        ]
        connections.append(
            VenueAdapter(
                "okx",
                "wss://ws.okx.com:8443/ws/v5/public",
                ({"op": "subscribe", "args": arguments},),
                connection_label=f"market-{index}",
                heartbeat_interval=20,
                heartbeat_payload=lambda: "ping",
            )
        )
    return tuple(connections)


def _bybit(markets: tuple[MarketSpec, ...]) -> tuple[VenueAdapter, ...]:
    connections = []
    for index, market_chunk in enumerate(_chunks(markets, 20), start=1):
        topics = [
            topic
            for market in market_chunk
            for topic in (
                f"orderbook.50.{market.symbol}",
                f"publicTrade.{market.symbol}",
                f"tickers.{market.symbol}",
                f"allLiquidation.{market.symbol}",
            )
        ]
        connections.append(
            VenueAdapter(
                "bybit",
                "wss://stream.bybit.com/v5/public/linear",
                ({"op": "subscribe", "args": topics},),
                connection_label=f"linear-{index}",
                heartbeat_interval=20,
                heartbeat_payload=lambda: json.dumps({"op": "ping"}),
            )
        )
    return tuple(connections)


def _hyperliquid(markets: tuple[MarketSpec, ...]) -> tuple[VenueAdapter, ...]:
    connections = []
    for index, market_chunk in enumerate(_chunks(markets, 10), start=1):
        subscriptions = tuple(
            {
                "method": "subscribe",
                "subscription": {"type": stream, "coin": market.symbol},
            }
            for market in market_chunk
            for stream in ("l2Book", "trades", "activeAssetCtx")
        )
        connections.append(
            VenueAdapter(
                "hyperliquid",
                "wss://api.hyperliquid.xyz/ws",
                subscriptions,
                connection_label=f"market-{index}",
                heartbeat_interval=30,
                heartbeat_payload=lambda: json.dumps({"method": "ping"}),
            )
        )
    return tuple(connections)


def _mexc(markets: tuple[MarketSpec, ...]) -> tuple[VenueAdapter, ...]:
    connections = []
    for index, market_chunk in enumerate(_chunks(markets, 8), start=1):
        subscriptions = tuple(
            {"method": method, "param": {"symbol": market.symbol, **extra}}
            for market in market_chunk
            for method, extra in (
                ("sub.depth.full", {"limit": 20}),
                ("sub.deal", {}),
                ("sub.ticker", {}),
            )
        )
        connections.append(
            VenueAdapter(
                "mexc",
                "wss://contract.mexc.com/edge",
                subscriptions,
                connection_label=f"market-{index}",
                heartbeat_interval=15,
                heartbeat_payload=lambda: json.dumps({"method": "ping"}),
                websocket_ping_interval=None,
                websocket_ping_timeout=None,
            )
        )
    return tuple(connections)


def _binance_spot(markets: tuple[MarketSpec, ...]) -> VenueAdapter:
    streams = [
        stream
        for market in markets
        for stream in (
            f"{market.symbol.lower()}@bookTicker",
            f"{market.symbol.lower()}@trade",
        )
    ]
    return VenueAdapter(
        "binance_spot",
        "wss://stream.binance.com:9443/stream",
        ({"method": "SUBSCRIBE", "params": streams, "id": 1},),
        connection_label="spot",
    )


def _okx_spot(markets: tuple[MarketSpec, ...]) -> tuple[VenueAdapter, ...]:
    connections = []
    for index, market_chunk in enumerate(_chunks(markets, 20), start=1):
        arguments = [
            {"channel": channel, "instId": market.symbol}
            for market in market_chunk
            for channel in ("books5", "trades", "tickers")
        ]
        connections.append(
            VenueAdapter(
                "okx_spot",
                "wss://ws.okx.com:8443/ws/v5/public",
                ({"op": "subscribe", "args": arguments},),
                connection_label=f"spot-{index}",
                heartbeat_interval=20,
                heartbeat_payload=lambda: "ping",
            )
        )
    return tuple(connections)


_BUILDERS = {
    "gate": _gate,
    "htx": _htx,
    "binance": _binance,
    "okx": _okx,
    "bybit": _bybit,
    "hyperliquid": _hyperliquid,
    "mexc": _mexc,
    "binance_spot": _binance_spot,
    "okx_spot": _okx_spot,
}


def build_adapters(
    venues: tuple[str, ...],
    symbols: tuple[str, ...],
    quote_assets: tuple[str, ...] = ("USDT",),
) -> tuple[VenueAdapter, ...]:
    built_adapters = []
    for venue in venues:
        built = _BUILDERS[venue](market_specs(venue, symbols, quote_assets))
        if isinstance(built, tuple):
            built_adapters.extend(built)
        else:
            built_adapters.append(built)
    return tuple(built_adapters)


def market_identity(
    venue: str, native_symbol: typing.Optional[str]
) -> tuple[typing.Optional[str], typing.Optional[str]]:
    if not native_symbol:
        return None, None
    if venue in {"gate", "mexc"} and "_" in native_symbol:
        base, quote = native_symbol.rsplit("_", 1)
        return base, quote
    if venue in {"htx", "hyperliquid"} and "-" in native_symbol:
        base, quote = native_symbol.rsplit("-", 1)
        return base, quote
    if venue == "okx" and native_symbol.endswith("-SWAP"):
        parts = native_symbol.split("-")
        return (parts[0], parts[1]) if len(parts) >= 3 else (None, None)
    if venue == "bybit" and native_symbol.endswith("PERP"):
        return native_symbol.removesuffix("PERP"), "USDC"
    if venue == "okx_spot" and "-" in native_symbol:
        base, quote = native_symbol.rsplit("-", 1)
        return base, quote
    if venue in {"binance", "bybit", "binance_spot"}:
        for quote in ("USDT", "USDC"):
            if native_symbol.endswith(quote):
                return native_symbol.removesuffix(quote), quote
    return None, None


def message_metadata(
    venue: str, payload: JsonObject
) -> tuple[str, typing.Optional[str], typing.Optional[int]]:
    stream: str = "unknown"
    symbol: typing.Optional[str] = None
    exchange_ts_ms: typing.Optional[int] = None
    if venue == "gate":
        stream = payload.get("channel", payload.get("event", "unknown"))
        exchange_ts_ms = _as_milliseconds(payload.get("time_ms") or payload.get("time"))
        result = payload.get("result")
        sample = result[0] if isinstance(result, list) and result else result
        if isinstance(sample, dict):
            symbol = sample.get("contract") or sample.get("s")
    elif venue == "htx":
        stream = payload.get("ch") or payload.get("topic") or payload.get("op", "unknown")
        exchange_ts_ms = _as_milliseconds(payload.get("ts"))
        if isinstance(stream, str) and stream.startswith("market."):
            parts = stream.split(".")
            symbol = parts[1].upper() if len(parts) > 1 else None
    elif venue in {"binance", "binance_spot"}:
        data = payload.get("data", payload)
        if isinstance(data, dict):
            stream = payload.get("stream") or data.get("e") or "control"
            if venue == "binance_spot" and stream == "control" and {
                "s",
                "b",
                "a",
            }.issubset(data):
                stream = "bookTicker"
            symbol = data.get("s")
            exchange_ts_ms = _as_milliseconds(data.get("E") or data.get("T"))
    elif venue in {"okx", "okx_spot"}:
        argument = payload.get("arg", {})
        if isinstance(argument, dict):
            stream = argument.get("channel", payload.get("event", "unknown"))
            symbol = argument.get("instId")
        data = payload.get("data")
        sample = data[0] if isinstance(data, list) and data else None
        if isinstance(sample, dict):
            exchange_ts_ms = _as_milliseconds(sample.get("ts"))
    elif venue == "bybit":
        stream = payload.get("topic", payload.get("op", "unknown"))
        exchange_ts_ms = _as_milliseconds(payload.get("ts"))
        if isinstance(stream, str) and "." in stream:
            symbol = stream.rsplit(".", 1)[-1]
    elif venue == "hyperliquid":
        stream = payload.get("channel", "unknown")
        data = payload.get("data")
        sample = data[0] if isinstance(data, list) and data else data
        if isinstance(sample, dict):
            coin = sample.get("coin")
            symbol = f"{coin}-USDC" if coin else None
            exchange_ts_ms = _as_milliseconds(sample.get("time"))
    elif venue == "mexc":
        stream = payload.get("channel", "unknown")
        symbol = payload.get("symbol")
        exchange_ts_ms = _as_milliseconds(payload.get("ts"))
    return str(stream), symbol, exchange_ts_ms


def _as_milliseconds(value: typing.Any) -> typing.Optional[int]:
    if value is None:
        return None
    numeric = float(value)
    if numeric < 10_000_000_000:
        numeric *= 1_000
    return int(numeric)
