import argparse
import asyncio
import pathlib

from octobot_market_making.data_collector.collector import PublicMarketDataCollector
from octobot_market_making.data_collector.config import CollectorConfig, SUPPORTED_VENUES


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record public perpetual-futures market data")
    parser.add_argument("--config", type=pathlib.Path, help="JSON configuration file")
    parser.add_argument(
        "--symbols", type=_csv, help="Comma-separated base assets, for example SOL,XRP"
    )
    parser.add_argument(
        "--venues", type=_csv, help=f"Comma-separated venues: {','.join(SUPPORTED_VENUES)}"
    )
    parser.add_argument("--quote-assets", type=_csv, help="Comma-separated quotes: USDT,USDC")
    parser.add_argument("--output", type=pathlib.Path, help="Root output directory")
    parser.add_argument(
        "--rotation-minutes", type=int, help="File rotation interval; must divide 60"
    )
    parser.add_argument(
        "--duration", type=float, help="Stop after this many seconds; default runs until Ctrl+C"
    )
    return parser.parse_args()


async def _run(arguments: argparse.Namespace) -> None:
    config = CollectorConfig.from_file(arguments.config).with_overrides(
        symbols=arguments.symbols,
        venues=arguments.venues,
        quote_assets=arguments.quote_assets,
        output_dir=arguments.output,
        rotation_minutes=arguments.rotation_minutes,
    )
    collector = PublicMarketDataCollector(config)
    print(
        f"Recording {','.join(config.symbols)} quoted in {','.join(config.quote_assets)} "
        f"from {','.join(config.venues)}"
    )
    print(f"Run directory: {collector.run_dir}")
    try:
        run_dir = await collector.run(duration=arguments.duration)
    except asyncio.CancelledError:
        collector.stop()
        raise
    print(f"Stopped cleanly after writing {collector.records_written} records to {run_dir}")


def main() -> None:
    try:
        asyncio.run(_run(_arguments()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
