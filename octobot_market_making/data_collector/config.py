import dataclasses
import json
import pathlib
import re
import typing


SUPPORTED_VENUES = (
    "gate",
    "htx",
    "binance",
    "okx",
    "bybit",
    "hyperliquid",
    "mexc",
    "binance_spot",
    "okx_spot",
)
DEFAULT_SYMBOLS = (
    "BTC",
    "ETH",
    "BCH",
    "SOL",
    "DOGE",
    "XRP",
    "LTC",
    "ADA",
    "BNB",
    "SUI",
    "LINK",
    "AVAX",
    "AAVE",
    "UNI",
    "HYPE",
    "NEAR",
    "ENA",
    "ZEC",
    "FIL",
    "XLM",
    "ONDO",
)
DEFAULT_QUOTE_ASSETS = ("USDT", "USDC")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,20}$")


@dataclasses.dataclass(frozen=True)
class CollectorConfig:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    venues: tuple[str, ...] = SUPPORTED_VENUES
    quote_assets: tuple[str, ...] = DEFAULT_QUOTE_ASSETS
    output_dir: pathlib.Path = pathlib.Path("market_data")
    rotation_minutes: int = 60
    compression_level: int = 3
    queue_size: int = 100_000
    metrics_interval_seconds: float = 60
    metadata_interval_seconds: float = 3_600

    def __post_init__(self) -> None:
        normalized_symbols = tuple(dict.fromkeys(symbol.upper() for symbol in self.symbols))
        normalized_venues = tuple(dict.fromkeys(venue.lower() for venue in self.venues))
        normalized_quotes = tuple(dict.fromkeys(quote.upper() for quote in self.quote_assets))
        object.__setattr__(self, "symbols", normalized_symbols)
        object.__setattr__(self, "venues", normalized_venues)
        object.__setattr__(self, "quote_assets", normalized_quotes)
        object.__setattr__(self, "output_dir", pathlib.Path(self.output_dir))
        if not normalized_symbols:
            raise ValueError("At least one symbol is required")
        invalid_symbols = [
            symbol for symbol in normalized_symbols if not _SYMBOL_PATTERN.fullmatch(symbol)
        ]
        if invalid_symbols:
            raise ValueError(f"Invalid symbols: {invalid_symbols}")
        invalid_venues = [venue for venue in normalized_venues if venue not in SUPPORTED_VENUES]
        if invalid_venues:
            raise ValueError(f"Unsupported venues: {invalid_venues}")
        if not normalized_venues:
            raise ValueError("At least one venue is required")
        invalid_quotes = [quote for quote in normalized_quotes if quote not in {"USDT", "USDC"}]
        if invalid_quotes:
            raise ValueError(f"Unsupported quote assets: {invalid_quotes}")
        if not normalized_quotes:
            raise ValueError("At least one quote asset is required")
        if self.rotation_minutes < 1 or 60 % self.rotation_minutes:
            raise ValueError("rotation_minutes must be a positive divisor of 60")
        if not 1 <= self.compression_level <= 9:
            raise ValueError("compression_level must be between 1 and 9")
        if self.queue_size < 1_000:
            raise ValueError("queue_size must be at least 1000")
        if self.metrics_interval_seconds < 10:
            raise ValueError("metrics_interval_seconds must be at least 10")
        if self.metadata_interval_seconds < 60:
            raise ValueError("metadata_interval_seconds must be at least 60")

    @classmethod
    def from_file(cls, path: typing.Optional[pathlib.Path]) -> "CollectorConfig":
        if path is None:
            return cls()
        with pathlib.Path(path).open(encoding="utf-8") as config_file:
            values = json.load(config_file)
        allowed = {field.name for field in dataclasses.fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration fields: {sorted(unknown)}")
        return cls(**values)

    def with_overrides(
        self,
        *,
        symbols: typing.Optional[typing.Iterable[str]] = None,
        venues: typing.Optional[typing.Iterable[str]] = None,
        quote_assets: typing.Optional[typing.Iterable[str]] = None,
        output_dir: typing.Optional[pathlib.Path] = None,
        rotation_minutes: typing.Optional[int] = None,
    ) -> "CollectorConfig":
        return dataclasses.replace(
            self,
            symbols=tuple(symbols) if symbols is not None else self.symbols,
            venues=tuple(venues) if venues is not None else self.venues,
            quote_assets=(
                tuple(quote_assets) if quote_assets is not None else self.quote_assets
            ),
            output_dir=pathlib.Path(output_dir) if output_dir is not None else self.output_dir,
            rotation_minutes=(
                rotation_minutes if rotation_minutes is not None else self.rotation_minutes
            ),
        )

    def as_dict(self) -> dict:
        values = dataclasses.asdict(self)
        values["output_dir"] = str(self.output_dir)
        values["symbols"] = list(self.symbols)
        values["venues"] = list(self.venues)
        values["quote_assets"] = list(self.quote_assets)
        return values
