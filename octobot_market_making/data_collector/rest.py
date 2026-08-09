import dataclasses
import json
import typing
import urllib.parse
import urllib.request

from octobot_market_making.data_collector import adapters


@dataclasses.dataclass(frozen=True)
class RestRequest:
    venue: str
    stream: str
    symbol: typing.Optional[str]
    url: str
    interval_seconds: float
    method: str = "GET"
    body: typing.Optional[dict] = None


def _url(base: str, **query: typing.Any) -> str:
    return f"{base}?{urllib.parse.urlencode(query)}" if query else base


def build_requests(
    venues: tuple[str, ...],
    symbols: tuple[str, ...],
    metrics_interval_seconds: float,
    metadata_interval_seconds: float,
    quote_assets: tuple[str, ...] = ("USDT",),
) -> tuple[RestRequest, ...]:
    requests = []
    for venue in venues:
        markets = adapters.market_specs(venue, symbols, quote_assets)
        server_time_urls = {
            "gate": "https://api.gateio.ws/api/v4/spot/time",
            "htx": "https://api.huobi.pro/v1/common/timestamp",
            "binance": "https://fapi.binance.com/fapi/v1/time",
            "okx": "https://www.okx.com/api/v5/public/time",
            "bybit": "https://api.bybit.com/v5/market/time",
            "mexc": "https://contract.mexc.com/api/v1/contract/ping",
            "binance_spot": "https://api.binance.com/api/v3/time",
            "okx_spot": "https://www.okx.com/api/v5/public/time",
        }
        if venue in server_time_urls:
            requests.append(
                RestRequest(
                    venue,
                    "rest.server_time",
                    None,
                    server_time_urls[venue],
                    metrics_interval_seconds,
                )
            )
        if venue == "binance":
            requests.append(
                RestRequest(
                    venue,
                    "rest.exchange_info",
                    None,
                    "https://fapi.binance.com/fapi/v1/exchangeInfo",
                    metadata_interval_seconds,
                )
            )
        elif venue == "hyperliquid":
            requests.append(
                RestRequest(
                    venue,
                    "rest.meta_and_asset_contexts",
                    None,
                    "https://api.hyperliquid.xyz/info",
                    metadata_interval_seconds,
                    method="POST",
                    body={"type": "metaAndAssetCtxs"},
                )
            )
        elif venue == "binance_spot":
            requests.append(
                RestRequest(
                    venue,
                    "rest.exchange_info",
                    None,
                    "https://api.binance.com/api/v3/exchangeInfo",
                    metadata_interval_seconds,
                )
            )
        elif venue == "okx_spot":
            requests.append(
                RestRequest(
                    venue,
                    "rest.exchange_info",
                    None,
                    _url(
                        "https://www.okx.com/api/v5/public/instruments",
                        instType="SPOT",
                    ),
                    metadata_interval_seconds,
                )
            )
        elif venue == "mexc":
            requests.append(
                RestRequest(
                    venue,
                    "rest.contract_info",
                    None,
                    "https://contract.mexc.com/api/v1/contract/detail",
                    metadata_interval_seconds,
                )
            )
        for market in markets:
            symbol = market.base
            if venue == "gate":
                contract = market.symbol
                requests.extend(
                    (
                        RestRequest(
                            venue,
                            "rest.contract_info",
                            contract,
                            f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{contract}",
                            metadata_interval_seconds,
                        ),
                        RestRequest(
                            venue,
                            "rest.order_book",
                            contract,
                            _url(
                                "https://api.gateio.ws/api/v4/futures/usdt/order_book",
                                contract=contract,
                                limit=100,
                                with_id="true",
                            ),
                            metrics_interval_seconds,
                        ),
                    )
                )
            elif venue == "htx":
                contract = market.symbol
                requests.extend(
                    (
                        RestRequest(
                            venue,
                            "rest.contract_info",
                            contract,
                            _url(
                                "https://api.hbdm.com/linear-swap-api/v1/swap_contract_info",
                                contract_code=contract,
                            ),
                            metadata_interval_seconds,
                        ),
                        RestRequest(
                            venue,
                            "rest.funding_rate",
                            contract,
                            _url(
                                "https://api.hbdm.com/linear-swap-api/v1/swap_funding_rate",
                                contract_code=contract,
                            ),
                            metrics_interval_seconds,
                        ),
                        RestRequest(
                            venue,
                            "rest.open_interest",
                            contract,
                            _url(
                                "https://api.hbdm.com/linear-swap-api/v1/swap_open_interest",
                                contract_code=contract,
                            ),
                            metrics_interval_seconds,
                        ),
                        RestRequest(
                            venue,
                            "rest.index",
                            contract,
                            _url(
                                "https://api.hbdm.com/linear-swap-api/v1/swap_index",
                                contract_code=contract,
                            ),
                            metrics_interval_seconds,
                        ),
                    )
                )
            elif venue == "okx":
                instrument = market.symbol
                requests.extend(
                    (
                        RestRequest(
                            venue,
                            "rest.contract_info",
                            instrument,
                            _url(
                                "https://www.okx.com/api/v5/public/instruments",
                                instType="SWAP",
                                instId=instrument,
                            ),
                            metadata_interval_seconds,
                        ),
                        RestRequest(
                            venue,
                            "rest.funding_rate",
                            instrument,
                            _url(
                                "https://www.okx.com/api/v5/public/funding-rate",
                                instId=instrument,
                            ),
                            metrics_interval_seconds,
                        ),
                        RestRequest(
                            venue,
                            "rest.open_interest",
                            instrument,
                            _url(
                                "https://www.okx.com/api/v5/public/open-interest",
                                instType="SWAP",
                                instId=instrument,
                            ),
                            metrics_interval_seconds,
                        ),
                        RestRequest(
                            venue,
                            "rest.mark_price",
                            instrument,
                            _url(
                                "https://www.okx.com/api/v5/public/mark-price",
                                instType="SWAP",
                                instId=instrument,
                            ),
                            metrics_interval_seconds,
                        ),
                    )
                )
            elif venue == "bybit":
                contract = market.symbol
                requests.append(
                    RestRequest(
                        venue,
                        "rest.contract_info",
                        contract,
                        _url(
                            "https://api.bybit.com/v5/market/instruments-info",
                            category="linear",
                            symbol=contract,
                        ),
                        metadata_interval_seconds,
                    )
                )
            elif venue == "mexc":
                contract = market.symbol
                requests.append(
                    RestRequest(
                        venue,
                        "rest.order_book",
                        contract,
                        _url(
                            f"https://contract.mexc.com/api/v1/contract/depth/{contract}",
                            limit=100,
                        ),
                        metrics_interval_seconds,
                    )
                )
    return tuple(requests)


def fetch_json(request: RestRequest) -> dict:
    body = json.dumps(request.body).encode("utf-8") if request.body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "octobot-market-data/1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    http_request = urllib.request.Request(
        request.url,
        data=body,
        headers=headers,
        method=request.method,
    )
    with urllib.request.urlopen(http_request, timeout=15) as response:
        return json.load(response)
