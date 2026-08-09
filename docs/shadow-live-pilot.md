# Shadow-to-live market-making pilot

The USD 100 figure is an isolated experiment and loss budget, not the trading
account size. The pilot should therefore optimize information quality and operational
safety rather than pretend that USD 100 is the strategy's capital ceiling.

No live orders are placed by the current research code. The first implementation is a
causal replay that records hypothetical orders in the same shape needed for later private
order and fill telemetry.

## What is implemented

`analysis/shadow_quote_replay.py` reconstructs the expanding-window Ridge activity-clock
signal and compares it with a zero-signal control. It submits hypothetical bid and ask
orders on recorded 100 ms MEXC books, applies a configurable entry latency and lifetime,
and evaluates two deliberately different public-trade fill proxies:

- `touch`: a public trade prints at or beyond the quote. This is optimistic because it
  ignores queue position.
- `trade_through`: a public trade prints through the quote by a configurable distance.
  This is stricter, but it is still not a genuine fill model.

The marked futures-style ledger includes configurable fees, an inventory limit and
inventory skew. Captured contract sizes are used to round quantity down to whole
contracts. The output includes every shadow order, signal age, assumed latency, fill
status, inventory transition, fee and 1/5/30 second markouts.

The replay deliberately does not invent queue rank. It also excludes price-tick
rounding, current venue minimum checks, rejects, funding, liquidation mechanics, rate
limits and cancel acknowledgement latency. Those must be measured or validated live.

## First replay evidence

The first 16-hour test used August 8--9, 2026 public data, an eight-hour initial training
period, 720 common-dollar-volume bars per hour, a 300 ms assumed entry latency, five-second
order lifetime and a conservative 2 bps maker fee. Results are exploratory rather than a
profit claim.

For MEXC ADA-USDT, the stricter `trade_through` proxy produced the following signal rows:

| Quote distance per side | Fills | Fill rate | Turnover | Gross PnL | Net at 2 bps | Break-even maker fee | Mean 5 s markout |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 bps | 161 | 2.26% | USD 158.53 | USD 0.0471 | USD 0.0154 | 2.97 bps | -2.96 bps |
| 5 bps | 148 | 2.08% | USD 145.67 | USD 0.0389 | USD 0.0097 | 2.67 bps | -1.82 bps |
| 8 bps | 72 | 1.01% | USD 70.96 | USD 0.0332 | USD 0.0190 | 4.68 bps | -0.77 bps |

At a hypothetical 1 bps maker fee, those net results become USD 0.0312, USD 0.0243 and
USD 0.0261. The pennies of PnL are useful only as evidence about the sign and components;
they are not statistically sufficient evidence of profit.

For MEXC SOL-USDT, one captured contract was 0.1 SOL. At this sample's price it was about
USD 7.6, so earlier smaller requested sizes were not executable. With one-contract orders,
the signal lost before fees at 3 and 5 bps. The 8 bps result was positive but had only 13
fills and is not trustworthy. The simple rule should therefore not be promoted live on SOL
from this evidence.

The machine-readable reports and order-level telemetry are stored on the NAS:

- `/mnt/shared/crypto_market_making/analysis/shadow-replay-mexc-ada-usdt-20260808T2000Z-20260809T1200Z.json`
- `/mnt/shared/crypto_market_making/analysis/shadow-replay-mexc-ada-usdt-20260808T2000Z-20260809T1200Z-orders.jsonl.gz`
- `/mnt/shared/crypto_market_making/analysis/shadow-replay-mexc-sol-usdt-one-contract-20260808T2000Z-20260809T1200Z.json`
- `/mnt/shared/crypto_market_making/analysis/shadow-replay-mexc-sol-usdt-one-contract-20260808T2000Z-20260809T1200Z-orders.jsonl.gz`

## Recommended rollout

### 1. Continuous real-time shadow, no credentials

Run the exact live decision loop for at least 24--72 hours without submitting orders.
Record each intended submit, amend and cancel, the current contract specification, local
book state and exchange health. Require:

- causal features only, with a hard maximum signal age;
- deterministic post-only price construction at the venue tick;
- quantities rounded to the current contract size and venue minimum;
- no quoting when the target or reference feeds are stale, crossed or desynchronized;
- latency, disconnect and sequence-gap alarms;
- results broken down by side, distance, volatility, inventory and time of day.

The real-time output should be replayable byte-for-byte through the same decision engine.

### 2. Private read-only preflight

Introduce a dedicated subaccount and an API key held in environment variables or a secret
store. Never paste a key into source, configuration committed to Git, logs or chat. Disable
withdrawals and use an IP allow-list when the exchange supports it.

Before enabling submission, query and persist the current contract detail and account
state. Validate `apiAllowed`, contract size, price unit/tick, minimum and maximum volume,
leverage/margin settings and the actual account fee. Abort on a mismatch. MEXC documents
post-only as order type `2`, but the endpoint's availability and account permission must be
confirmed against the live account before any order mutation.

### 3. Minimum-size ADA post-only pilot

ADA is the first candidate because its captured one-contract granularity permits genuinely
small orders and its stricter replay remained positive at the assumed 2 bps fee. Start with
the dynamically calculated minimum executable quantity, not a hard-coded USD amount, at 5
and 8 bps per side. Use isolated margin and low leverage so the pilot cannot consume the
larger portfolio.

Suggested initial controls, expressed as pilot limits rather than account limits:

- total realized-plus-marked pilot loss stop: USD 100;
- daily pilot loss stop: USD 10 until the mechanics are proven;
- maximum marked ADA inventory: USD 25;
- maximum two live orders, one per side;
- post-only only; cancel or reject anything that could become taker;
- emergency cancel-all on stale feeds, missing acknowledgements, position mismatch,
  repeated rejects or private-stream disconnect;
- stop after any unexpected taker fill until its cause is understood;
- no tier-chasing multiplier until actual fill quality is positive after fees.

These are conservative starting values, not an assertion about how much of the user's
capital the mature strategy should deploy.

### 4. Gather genuine fill evidence

For every live order, store client and exchange IDs; decision, submit, acknowledgement,
amend, cancel and cancel-confirm timestamps; accepted post-only flag; partial fills; maker/
taker classification; charged fee or rebate; position and margin snapshots; and markouts at
100 ms, 1 s, 5 s and 30 s. Reconcile REST, private-stream and local state before placing the
next order after any uncertainty.

Compare the model with a zero-signal shadow control on the same live market. Once execution
is stable and the experiment budget permits it, a small randomized live holdout can identify
the signal's incremental value more cleanly. Stratify randomization by side, quote distance
and market regime; never compare different days as if they were exchangeable.

### 5. Promotion criteria

Do not scale from turnover alone. Require enough independent market regimes and fills for a
blocked confidence interval, not just a large count of highly correlated five-second orders.
Promote only when:

- actual post-only acceptance and maker classification are reliable;
- actual net PnL and inventory-adjusted markouts are positive after charged fees and funding;
- the lower confidence bound is acceptable under conservative latency and missed-fill
  assumptions;
- live inventory and drawdown agree with replay within an explained tolerance;
- model results beat the simultaneous zero-signal control;
- expected future fee savings exceed the measured loss required to reach a higher tier.

## Reproduce the ADA replay

```shell
.venv-data/bin/python analysis/shadow_quote_replay.py \
  --run /mnt/shared/crypto_market_making/live/run=20260808T112313Z-999c7abc \
  --feature-cache /mnt/shared/crypto_market_making/analysis/model-research-cache-20260808T1200Z-20260809T1200Z.npz \
  --activity-cache /mnt/shared/crypto_market_making/analysis/volume-activity-cache-20260808T1200Z-20260809T1200Z.npz \
  --target mexc:ADA:USDT \
  --bars-per-hour 720 \
  --initial-train-hours 8 \
  --distances-bps 3,5,8,13 \
  --decision-seconds 5 \
  --lifetime-seconds 5 \
  --entry-latency-ms 300 \
  --order-notional-usd 1 \
  --starting-equity-usd 100 \
  --maker-fee-bps 2 \
  --max-inventory-usd 10 \
  --output /mnt/shared/crypto_market_making/analysis/shadow-replay-mexc-ada-usdt-20260808T2000Z-20260809T1200Z.json
```

The starting equity above is a reporting ledger. It neither sizes the user's account nor
authorizes a live allocation.
