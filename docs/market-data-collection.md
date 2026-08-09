# Futures market-data collection

This recorder captures public USDT/USDC perpetual market data from Gate, HTX, Binance, OKX,
Bybit, MEXC, and Hyperliquid, together with Binance and OKX USDT/USDC spot reference feeds. It is
separate from the OctoBot trading process and does not read API
keys or place orders. Unsupported quote/venue combinations are omitted: Gate, HTX, and OKX use
USDT for this linear-perpetual universe; Binance and Bybit add their listed USDC contracts; and
Hyperliquid supplies USDC-settled perpetuals.

## Install and run

```shell
uv venv .venv-data
uv pip install --python .venv-data/bin/python -r data_requirements.txt
.venv-data/bin/python -m octobot_market_making.data_collector \
  --config market_data_config.example.json
```

For a short connectivity test:

```shell
.venv-data/bin/python -m octobot_market_making.data_collector \
  --symbols SOL \
  --venues gate,htx,binance,okx,bybit,hyperliquid,mexc,binance_spot,okx_spot \
  --quote-assets USDT,USDC \
  --duration 30
```

The deployed research universe records BTC, ETH, BCH, SOL, DOGE, XRP, LTC, ADA, BNB, SUI,
LINK, AVAX, AAVE, UNI, HYPE, NEAR, ENA, ZEC, FIL, XLM, and ONDO. Raw events are written under:

```text
market_data/
  run=<UTC timestamp>-<id>/
    manifest.json
    raw/
      venue=<venue>/
        date=<UTC date>/
          events-<UTC rotation time>.jsonl.gz
```

Every record contains the unmodified exchange payload plus:

- `received_ts_ns`: local wall-clock receive time in nanoseconds.
- `received_monotonic_ns`: monotonic receive time for local interval measurements.
- `exchange_ts_ms`: best available exchange event timestamp.
- `connection_id`: changes after every reconnect.
- `local_sequence`: monotonically increasing per-venue recorder sequence.
- `stream` and `symbol`: extracted routing fields.

Connection and reconnection incidents are stored as `record_type=system`. A bounded queue applies backpressure instead of silently discarding records.
Once per minute a `venue=collector`, `stream=health` record stores queue occupancy, total records
written, and event-loop lag so overloaded periods can be excluded from latency research.

Public REST snapshots are stored as `record_type=rest`. Contract specifications are refreshed hourly; funding, index/mark and open-interest metrics that are missing from a venue's WebSocket feed are refreshed every 60 seconds. Both intervals are configurable.

## What is collected

| Venue | Books | Public trades | Market state |
|---|---|---|---|
| Gate | 20-level, 100 ms incremental updates plus sequence-bearing REST anchors | Every public futures trade | Ticker, mark, index, funding, 24-hour turnover |
| HTX | 20-level high-frequency incremental updates | Public trade detail | Market detail, contract metadata, funding, index, open interest and 24-hour turnover |
| Binance | 20-level, 100 ms snapshots | Aggregate trades | Mark, index, funding and rolling ticker |
| OKX | Five-level snapshots | Public trades | Contract metadata, ticker, mark, funding and open interest |
| Bybit | 50-level order book | Public trades | Ticker, mark, index, funding, open interest and turnover |
| Hyperliquid | 20-level L2 book | Public trades | Active-asset context, oracle/mark price, funding, open interest and daily notional volume |
| MEXC futures | 20-level snapshots plus 100-level REST anchors | Public trades | USDT/USDC ticker, mark/index, funding, open interest and turnover |
| Binance spot | Event-driven best bid/ask | Individual public trades | USDT and USDC reference markets |
| OKX spot | Five-level snapshots | Public trades | USDT and USDC tickers/reference markets |

The first research version intentionally stores raw payloads. Local order-book reconstruction, feature generation, and Parquet conversion should be deterministic downstream jobs so improvements do not require recollecting data.

## Data still required from private trading streams

Public data cannot reveal whether one of our orders would really have filled. Once credentials are introduced, record a separate private dataset containing:

- Client order ID and exchange order ID.
- Submit, acknowledgement, amend, cancel-request and cancel-confirm timestamps.
- Requested and accepted post-only status.
- Original, remaining and filled quantities.
- Every partial fill with price, quantity, maker/taker flag, fee and rebate.
- Position, margin, liquidation price and funding ledger snapshots.
- Post-only rejections, rate-limit responses and cancel failures.

Use genuine third-party fills only. Self-trades do not produce useful fill-quality data and can invalidate exchange volume-program statistics.

## Research outputs

For every asset, venue, side, hour and volatility regime, derive:

- Spread, depth within 1/2/5/10 bps, microprice, order-flow imbalance, trade intensity and realized volatility.
- Cross-venue lead/lag and price-dislocation duration.
- Mark/index basis, funding, open interest and 24-hour turnover.
- Hypothetical queue position and fill probability under explicit latency assumptions.
- Actual fill markouts at 100 ms, 1 s, 5 s and 30 s once private fills are recorded.
- PnL separated into spread capture, inventory movement, fees/rebates and funding.
- Rolling 30-day qualifying turnover and the fee savings expected from the next tier.

Do not optimize only for turnover. Track the cumulative cost of reaching a tier and compare it with the discounted fee savings expected before that tier expires or is recalculated.

Run a bounded exploratory screen on a known-good interval with:

```shell
.venv-data/bin/python analysis/exploratory_market_making.py \
  --run /mnt/shared/crypto_market_making/live/run=<run-id> \
  --start 2026-08-07T03:00:00Z \
  --end 2026-08-07T04:00:00Z \
  --output /mnt/shared/crypto_market_making/analysis
```

The analysis reconstructs HTX's incremental book from the run's original snapshot, uses
local receive time for actionable lead/lag comparisons, basis-normalizes source venues, and
reports fee capacity parametrically. Its ranking is a research priority rather than expected PnL;
fill probability still requires private order and fill telemetry.

For a leakage-controlled model comparison, install `analysis_requirements.txt` and run:

```shell
.venv-data/bin/python analysis/model_research.py \
  --run /mnt/shared/crypto_market_making/live/run=<run-id> \
  --start 2026-08-07T00:00:00Z \
  --end 2026-08-07T08:00:00Z \
  --initial-train-hours 3 \
  --cache /mnt/shared/crypto_market_making/analysis/model-research-cache.npz \
  --output /mnt/shared/crypto_market_making/analysis/model-research.json
```

The reusable feature cache permits new model challengers without reparsing the raw feeds. The
test protocol uses non-overlapping 500 ms labels, expanding chronological folds, boundary purging,
train-only transformations, HAC and block-bootstrap uncertainty, and multiple-testing correction.
It includes target/source ablations so target microstructure predictability is not mistaken for
cross-venue information.

Compare every cached futures feed as a possible quote target with:

```shell
.venv-data/bin/python analysis/cross_venue_target_research.py \
  --cache /mnt/shared/crypto_market_making/analysis/model-research-cache-20260807T0000Z-20260807T0800Z.npz \
  --output /mnt/shared/crypto_market_making/analysis/cross-venue-target-research-20260807T0000Z-20260807T0800Z.json \
  --initial-train-hours 3
```

Measure spread, displayed depth, public turnover, and receive latency on every futures venue:

```shell
.venv-data/bin/python analysis/cross_venue_market_quality.py \
  --run /mnt/shared/crypto_market_making/live/run=20260806T222542Z-8c036da2 \
  --windows 2026-08-07T03:00:00Z/2026-08-07T04:00:00Z,2026-08-07T07:00:00Z/2026-08-07T08:00:00Z \
  --assets BCH,SOL,DOGE,XRP,LTC,ADA,AVAX,HYPE,ENA,XLM \
  --output /mnt/shared/crypto_market_making/analysis/cross-venue-market-quality-two-windows.json
```

Run the train-only, forward-filtered hidden-Markov regime comparison with:

```shell
.venv-data/bin/python analysis/hmm_regime_research.py \
  --cache /mnt/shared/crypto_market_making/analysis/model-research-cache-20260807T0000Z-20260807T0800Z.npz \
  --output /mnt/shared/crypto_market_making/analysis/hmm-regime-research-20260807T0000Z-20260807T0800Z.json \
  --initial-train-hours 3
```

Estimate no-queue touch rates and markouts for quotes placed behind the BBO with:

```shell
.venv-data/bin/python analysis/quote_distance_research.py \
  --run /mnt/shared/crypto_market_making/live/run=20260806T222542Z-8c036da2 \
  --windows 2026-08-07T03:00:00Z/2026-08-07T04:00:00Z,2026-08-07T07:00:00Z/2026-08-07T08:00:00Z \
  --targets gate:SOL:USDT,mexc:SOL:USDT,mexc:SOL:USDC,mexc:DOGE:USDT,mexc:BCH:USDT,mexc:ADA:USDT \
  --distances-bps 1,2,3,5,8,13 \
  --output /mnt/shared/crypto_market_making/analysis/quote-distance-research-two-windows.json
```

Reconstruct a common liquid-reference dollar-volume clock for every contract of an asset and
compare it with matched fixed-time bars:

```shell
.venv-data/bin/python analysis/volume_clock_research.py \
  --run /mnt/shared/crypto_market_making/live/run=20260806T222542Z-8c036da2 \
  --feature-cache /mnt/shared/crypto_market_making/analysis/model-research-cache-20260807T0000Z-20260807T0800Z.npz \
  --activity-cache /mnt/shared/crypto_market_making/analysis/volume-activity-cache-20260807T0000Z-20260807T0800Z.npz \
  --output /mnt/shared/crypto_market_making/analysis/volume-clock-research-20260807T0000Z-20260807T0800Z.json \
  --targets mexc:SOL:USDC,mexc:SOL:USDT,gate:SOL:USDT,mexc:DOGE:USDT,mexc:BCH:USDT,mexc:ADA:USDT,htx:ENA:USDT,htx:XLM:USDT \
  --bars-per-hour 120,360,720 \
  --initial-train-hours 3
```

The clock uses received public trades from Binance, OKX, Bybit, and Hyperliquid. Contract
multipliers convert quantity to dollar notional. Thresholds are calibrated only on the initial
training window, all contracts for one asset share a clock, labels crossing a fold boundary are
purged, and the flow model adds signed aggressor imbalance and per-venue participation features.

Replay causal activity-clock predictions as shadow post-only quotes against the recorded target
book and public trades with:

```shell
.venv-data/bin/python analysis/shadow_quote_replay.py \
  --run /mnt/shared/crypto_market_making/live/run=20260808T112313Z-999c7abc \
  --feature-cache /mnt/shared/crypto_market_making/analysis/model-research-cache-20260808T1200Z-20260809T1200Z.npz \
  --activity-cache /mnt/shared/crypto_market_making/analysis/volume-activity-cache-20260808T1200Z-20260809T1200Z.npz \
  --target mexc:ADA:USDT \
  --bars-per-hour 720 \
  --initial-train-hours 8 \
  --distances-bps 3,5,8,13 \
  --entry-latency-ms 300 \
  --order-notional-usd 1 \
  --starting-equity-usd 100 \
  --maker-fee-bps 2 \
  --max-inventory-usd 10 \
  --output /mnt/shared/crypto_market_making/analysis/shadow-replay-mexc-ada-usdt-20260808T2000Z-20260809T1200Z.json
```

The replay compares the activity-clock model against a zero-signal control under both an
optimistic touch proxy and a stricter trade-through proxy. It records every hypothetical order
and markout in compressed JSONL, rounds quantity using captured contract sizes, and reports fee
sensitivity and the maker fee at which each scenario breaks even. Public trades do not expose
queue priority, so live promotion still requires private post-only acknowledgements and fill
telemetry. See `docs/shadow-live-pilot.md` for the first results and the guarded rollout.

## Historical archive backfill

The resumable backfill downloads the non-redundant public datasets useful for market-making
research:

- Binance USD-M monthly trades, aggregate trades, BBO, funding and one-minute market,
  mark, index and premium-index bars; daily book-depth, metrics and recent daily files.
- HTX USDT linear-swap daily trades and one-minute bars.
- Bybit's complete public per-symbol trade archives.

Binance and HTX files are SHA-256 checked against the exchanges' published checksums. Files
are first written with a `.part` suffix and atomically renamed after validation, so stopping the
process does not make an incomplete file look valid.

```shell
.venv-data/bin/python -m octobot_market_making.data_collector.historical \
  --output /mnt/shared/crypto_market_making/historical \
  --sources binance,bybit,htx \
  --workers 4
```

Gate does not expose an equivalent complete historical order-book archive. Its research-grade
book history therefore starts with this project's live incremental feed and sequence-bearing REST
anchors.

## Persistent NAS deployment on this host

The active deployment uses `/mnt/shared/crypto_market_making` on the 15 TB RAID volume:

```text
crypto_market_making/
  live/          # raw WebSocket and REST events from all configured futures/spot venues
  historical/    # resumable exchange archives and backfill-status.json
```

Both processes are enabled user services:

```shell
systemctl --user status octobot-market-data.service
systemctl --user status octobot-market-backfill.service
journalctl --user -u octobot-market-data.service -f
journalctl --user -u octobot-market-backfill.service -f
```

The service runs one archive cycle in a fresh process every six hours and refuses new downloads if
free space falls below 200 GiB. It never deletes previously collected market data.
