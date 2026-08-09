# Initial multi-venue market-making research

## Outcome

The current data supports a statistically repeatable short-horizon cross-venue signal within
these captures, but it does not yet support a claim of profitable passive fills. The best first
predictive model is a regularized linear fair-price/catch-up model on a turnover-normalized
participant-activity clock. Quote cancellation and risk controls must still process raw events
and wall-clock deadlines. A neural network is not justified by these captures.

Small turnover is a capacity constraint rather than a reason to reject a market. Some HTX
and MEXC markets are suitable as $1-$20 research targets even though they could not absorb
large capital. Start with two or three targets, measure actual fills, and expand only when
the live fill-conditioned edge is positive or the deliberately negative VIP-tier budget is
still within its pre-set limit.

## Exactly which data was used

The original model, HMM, quote-distance, and activity-clock discovery use this raw run and interval:

- `/mnt/shared/crypto_market_making/live/run=20260806T222542Z-8c036da2`
- 2026-08-07 00:00:00 through 08:00:00 UTC: eight hours from one day.
- Futures books/BBO and public trades from Gate, HTX, Binance, OKX, Bybit, MEXC, and
  Hyperliquid, plus Binance and OKX spot reference feeds where available.
- Local `received_ts_ns`, rather than exchange clocks, determines what was observable in a
  causal cross-venue strategy. Exchange timestamps are retained for latency diagnostics.
- Price state is reconstructed on a 100 ms last-observation grid, with no state carried more
  than one second. The original comparison samples non-overlapping 500 ms observations.
- The first three hours train/calibrate the model. The following five one-hour folds are tested
  chronologically; transformations are train-only and boundary-crossing labels are purged.

The discovery activity cache parses 865,455 public futures trades in the interval for SOL,
DOGE, BCH, ADA, ENA, and XLM. Dollar notional is `price × quantity × contract multiplier`;
buyer/seller aggressor direction comes from each venue's public trade schema. The clock is
formed on 500 ms receive-time buckets. It does not use our orders, queue position, private
fills, fees, or account state.

The preselected activity-clock design was then confirmed without changing the model or 360/720
bar rates on `/mnt/shared/crypto_market_making/live/run=20260808T112313Z-999c7abc`, from
2026-08-08 12:00 through 2026-08-09 12:00 UTC. That cache contains 2,115,624 parsed public
futures trades. The first eight hours calibrated the thresholds/model and the following sixteen
one-hour folds were held out chronologically.

The spread/depth and quote-distance tables use two separate one-hour windows, 03:00-04:00 and
07:00-08:00 UTC, from the discovery run. The activity-clock result now spans a discovery window
and a later 24-hour confirmation, but none of the present results is a live-fill backtest.

## Participant-activity clock experiment

Equal-dollar/event bars are materially more promising than treating every second as equally
informative. The clean definition is not ten base coins, which has different economic meaning
for SOL and DOGE, nor one universal $10,000 threshold, which generates radically different bar
rates. Instead, use one common activity fraction for every asset:

`threshold(asset, B) = causal normal reference turnover per hour(asset) / B`

`B` is fixed across assets and contracts. Discovery tried 120, 360, and 720 expected bars per
hour; confirmation retained only the preselected 360 and 720 rates. The dollar threshold differs
by asset, but every contract of the same asset shares one
clock. Its closes use only public trades on the fixed liquid reference set of Binance, OKX,
Bybit, and Hyperliquid. Thus Gate SOL, MEXC SOL-USDT, and MEXC SOL-USDC receive exactly the same
SOL activity-bar boundaries. Thresholds were frozen from the initial calibration period without
future turnover.

Each bar adds prior target return, calendar duration, dollar volume, trade count, mean trade
size, signed reference imbalance, threshold overshoot, and per-venue volume shares/imbalances to
the existing cross-venue price features. A fixed Ridge model was tested against the same model on
matched fixed-time bars:

| Target | Activity rate | Frozen threshold | Activity price+flow skill | Positive folds | Matched time-bar skill |
|---|---:|---:|---:|---:|---:|
| Gate SOL-USDT | 360/hour | $310,781 | 1.54% | 10/16 | -2.68% |
| Gate SOL-USDT | 720/hour | $155,391 | 6.05% | 12/16 | -0.81% |
| MEXC SOL-USDT | 360/hour | $310,781 | 14.88% | 16/16 | -2.23% |
| MEXC SOL-USDT | 720/hour | $155,391 | 20.18% | 16/16 | 0.88% |
| MEXC SOL-USDC | 720/hour | $155,391 | 31.81% | 16/16 | 3.73% |
| MEXC ADA-USDT | 720/hour | $7,551 | 23.75% | 15/16 | 5.31% |
| HTX ENA-USDT | 720/hour | $5,135 | 18.90% | 16/16 | 6.41% |

The table is the 24-hour confirmation result after mapping every completed 500 ms activity
bucket to price state at the bucket's right-hand boundary. That prevents current-bar flow from
overlapping the subsequent return label. The advantage remained fold-stable for MEXC SOL, ADA,
and HTX ENA after selection on the earlier eight-hour discovery window. Gate SOL at 720/hour is
positive but substantially weaker, and its 360/hour setting is not convincing. The
discovery's 120/hour SOL setting was weak, suggesting that roughly 30-second aggregation discards
useful lead information, and it was not retested. DOGE, BCH, and XLM did not have complete
matched fold coverage under the target-row continuity filter in either report, so they are not
used as supporting evidence. This is much stronger evidence for the clock/model direction, but
it is still a price-forecast result—not a fill, fee, or profit result.

Most of the confirmed improvement comes from the activity clock rather than the current flow
feature bundle. At 720/hour, activity-bar price-only skill was 5.79% for Gate SOL, 19.82% for
MEXC SOL-USDT, 32.04% for MEXC SOL-USDC, 23.72% for MEXC ADA, and 19.52% for HTX ENA. Adding
500 ms signed imbalance, trade size, and venue-share features changed those results by only
about -0.6 to +0.4 percentage points. Therefore dollar/activity time is promoted as a model
coordinate; coarse signed-flow features are not yet promoted as incremental alpha. Exact-trade
sampling and order-book event/OFI reconstruction are the appropriate next challengers.

In production, use a hybrid clock:

1. Process every book/trade event immediately for fair price, stale-feed checks, quote pulls,
   and jump protection; never wait for a candle to cancel a toxic order.
2. Close a model/activity bar after a fixed fraction of normal liquid-reference dollar volume.
   Update its threshold slowly from a trailing robust turnover estimate, with bounded hourly
   changes; do not let a same-minute volume spike move its own threshold.
3. Keep wall-clock maximum bar age, order lifetime, acknowledgement timeout, funding,
   liquidation distance, and drawdown limits. Quiet markets cannot be allowed to freeze risk.
4. Compare ordinary dollar bars with trade-count, signed-imbalance, dollar-run, and order-book
   event/OFI bars. The next important missing activity is book additions, cancellations, and
   queue depletion—not a larger neural network.

## What the SOL result means

At time `t`, the model sees only prices received by the recorder at or before `t`. It predicts
the candidate target venue's mid-price change from `t` to `t + 500 ms`. Each test hour is later
than its training data, labels crossing the train/test boundary are purged, and all scaling is
fit only on the training fold.

| SOL target | Own-history MSE skill | External-only MSE skill | Full MSE skill | External fold skills | Median spread | Observed turnover/hour |
|---|---:|---:|---:|---|---:|---:|
| MEXC USDC | -0.10% | 20.05% | 37.78% | 21.11%, 17.62%, 21.51%, 16.50%, 24.22% | 1.38 bps | $583k |
| MEXC USDT | 0.34% | 10.88% | 18.64% | 13.67%, 13.66%, 6.02%, 10.07%, 11.65% | 1.38 bps | $16.25m |
| Gate USDT | 0.04% | 7.84% | 9.20% | 6.13%, 7.40%, 11.90%, 5.69%, 7.25% | 1.37-1.38 bps | $2.35m-$5.88m |
| Binance USDT | 0.48% | 3.55% | 5.53% | 6.73%, 4.00%, 3.77%, 1.40%, 1.19% | 1.38 bps | $22.01m |
| OKX USDT | 0.18% | 2.69% | 3.19% | 2.76%, 3.30%, 3.53%, 3.12%, 0.05% | 1.38 bps | $9.73m |
| Bybit USDT | 0.23% | 2.66% | 3.50% | 4.39%, 2.17%, 4.78%, 1.32%, -0.21% | 1.38 bps | $6.04m |

The almost-zero own-history result and stable external-only result mean the model is mostly
detecting other venues moving before the target, not extrapolating SOL's own bid/ask bounce.
MEXC is the strongest catch-up target in this sample. Its SOL book updated about 2 times per
second, versus roughly 8.6 on Binance and 8.0 on OKX. Its median public-feed receive lag was
similar to the larger venues, so the result does not look like a simple exchange-clock artifact.
It still needs order-entry and acknowledgement latency tests because a slow public book can be
untradable even when its mid-price forecast is accurate.

The active external SOL calls aligned with about 1.2-1.4 bps of subsequent target movement.
That is not a 1.2-1.4 bps maker profit. A passive order tends to fill specifically when the
market is moving against it, so the next required quantity is the markout conditional on our
order actually filling.

## Other exchanges and targets

The cross-venue reframing evaluated 76 futures contracts on Gate, HTX, Binance, OKX, Bybit,
MEXC, and available USDT/USDC contracts. Hyperliquid raw books and trades are present, but its
approximately 0.2 L2 snapshots per second did not provide enough observations under the
current one-second stale-state rule. It needs an event-time or longer as-of-state test rather
than fabricated 500 ms observations.

MEXC dominated the external-only catch-up ranking in this eight-hour capture:

| Candidate | External-only skill | Positive folds | Median spread | Median 5 bps depth | Turnover/hour | Research use |
|---|---:|---:|---:|---:|---:|---|
| MEXC SOL-USDC | 20.05% | 5/5 | 1.38 bps | $396k | $583k | Best clean SOL catch-up laboratory |
| MEXC SOL-USDT | 10.88% | 5/5 | 1.38 bps | $3.63m | $16.25m | More capacity and VIP turnover |
| MEXC XRP-USDT | 17.59% | 5/5 | 0.97 bps | $649k | $16.15m | Strong signal, but fee headroom is very thin |
| MEXC DOGE-USDT | 17.31% | 5/5 | 1.44 bps | $269k | $1.75m | Strong balanced pilot candidate |
| MEXC BCH-USDT | 8.07% | 5/5 | 2.34 bps | $4.9k | $116k | Wider but small-capacity pilot |
| MEXC ADA-USDT | 13.39% | 5/5 | 4.98 bps | $51k | $824k | Better fee tolerance than SOL/XRP |

MEXC LTC-USDT had 6.13% external-only skill but only four positive folds and a 0.169
multiplicity-adjusted q-value. It remains a holdout candidate, not a promoted model.

Very small markets can still be useful at very small quote sizes:

| Micro target | External-only skill | Median spread | Median top size | Turnover/hour | Approximate pilot order ceiling before equity cap |
|---|---:|---:|---:|---:|---:|
| MEXC AVAX-USDC | 18.07% | 1.56 bps | $117 | $4.5k | $4-$5 |
| MEXC XLM-USDC | 15.25% | 6.19 bps | $135 | $4.1k | $4 |
| MEXC ENA-USDC | 8.55% | 3.23 bps | $118 | $5.0k | $5 |
| HTX ENA-USDT | 10.64% | 10.8-11.8 bps | $172-$180 | $0.1k-$4.3k | $1-$4 |
| HTX AVAX-USDT | 9.21% | 9.4-13.7 bps | $173-$179 | $2.4k-$3.2k | $2-$3 |
| HTX XLM-USDT | 8.62% | 5.6-6.2 bps | about $179 | $1.2k-$2.2k | $1-$2 |
| HTX HYPE-USDT | 2.90% | 5.0-5.6 bps | $256-$334 | $2.6k-$8.9k | $3-$9 |

The pilot ceiling above is only a liquidity cap: the minimum of $25, 5% of median displayed
top size, and 0.1% of observed hourly turnover. The live order must also be no more than about
0.5%-1% of account equity. The numbers intentionally become tiny on markets where a $25 order
would itself be material. Exchange contract minimums may make some of these micro pilots
impossible; reject the market rather than rounding a test order up to an unsafe size.

Binance, OKX, and Bybit are not only signal sources. Some contracts had statistically positive
external forecasts, such as Binance ENA-USDC (5.29%), Binance HYPE-USDT (4.25%), and OKX SOL-USDT
(2.69%). Their tighter and faster books often leave less economic room, so predictability alone
does not make them better maker targets.

## Fees and deliberate VIP-tier investment

The observed BBO spread is a market-state measurement, not the spread we must quote. The earlier
`BBO / 2` fee-cap interpretation was too restrictive: a maker can rest behind the touch or shift
both quotes around a forecast/inventory reservation price. A wider quote has a lower fill rate and
a different, often more toxic, conditional fill distribution.

For quote distance `delta` and hidden state `s`, optimize the one-sided opportunity as:

`fill_probability(delta, s) × [distance_from_fair + rebate - fee - adverse_markout(delta, s)] - inventory_risk`

The first no-queue touch experiment placed quotes 1, 2, 3, 5, 8, and 13 bps from mid, corresponding
to full widths of 2-26 bps. It confirms both sides of the trade-off:

| Target | Full width | Public-trade touch rate within 5 s | Mean 5 s pre-fee edge after touch |
|---|---:|---:|---:|
| Gate SOL-USDT | 2 bps | 7.96% | -0.88 bps |
| Gate SOL-USDT | 4 bps | 7.93% | 0.11 bps |
| MEXC SOL-USDT | 4 bps | 5.95% | -0.19 bps |
| MEXC BCH-USDT | 6 bps | 1.36% | 0.58 bps |
| MEXC BCH-USDT | 10 bps | 0.38% | 1.59 bps |
| MEXC DOGE-USDT | 10 bps | 0.33% | 0.43 bps |
| MEXC ADA-USDT | 10 bps | 2.70% | -2.06 bps |

A touch is only an upper bound on a fill because our queue position is unknown. These results
show why neither joining a sub-1-bps BBO nor applying one fixed wider spread is sufficient.
High-fee, unhedged operation should normally impose a wider minimum boundary, but it must be
selected from the state-conditioned fill/markout curve. Wider ADA quotes, for example, remained
toxic in this sample.

Temporary negative expectancy can be treated as a measured fee-tier investment, but it should
have its own ledger and stop limit:

`expected tier value = future filled notional × fee reduction / 10,000`

Promote only when the discounted expected tier value exceeds the cumulative intentional loss,
the required turnover is realistically reachable, and the tier can be maintained. Record the
actual fee on every fill; do not infer it from a public fee table. Wider ADA/BCH or micro markets
give more fee buffer, while SOL/XRP are efficient for turnover only if their measured adverse
markout does not consume the already-thin spread.

## Recommended model and quoting policy

The initial model stack should be deliberately simple:

1. Build a basis-normalized fair price from Binance, OKX, Bybit, Gate, MEXC, spot, and available
   USDC contracts. Keep USDT and USDC basis offsets separate.
2. Make the next turnover-normalized activity bar the primary prediction horizon with a fixed,
   regularized linear model. Retain the 100/500 ms state as a fast quote-cancel and latency-risk
   layer. Refit only on a slow rolling schedule after multi-day tests establish the window.
3. Use the causal Kalman fair price as a smoothing and uncertainty feature. It was weak as a
   standalone Gate forecaster; it should not replace the linear lead model.
4. Retest the causal three-state hidden Markov chain on activity bars as the regime/risk overlay,
   with calendar bar duration retained as a feature. It is fit only on training folds and
   forward-filtered in test/live operation; smoothed future-aware states are prohibited.
5. Use volatility and jump scores to widen or cancel quotes. The separate observed jump-regime
   ridge did not improve materially over ordinary ridge.
6. Add nonlinear trees only as challengers for HTX and selected DOGE markets. The small neural
   network lost to zero-return prediction on nine of ten initial targets, so neural networks are
   deferred until there is substantially more multi-regime data.

The HMM forecast test produced modest observed within-capture improvements on selected targets.
They are not yet evidence of durable incremental performance over the plain model:

| Target | Plain full model | Best HMM model | Difference |
|---|---:|---:|---:|
| Gate SOL-USDT | 9.20% | 10.06% | +0.85 pp |
| MEXC SOL-USDT | 18.64% | 20.77% | +2.12 pp |
| MEXC SOL-USDC | 37.78% | 37.94% | +0.16 pp |
| HTX XLM-USDT | 19.87% | 22.53% | +2.66 pp |
| HTX ENA-USDT | 12.93% | 13.61% | +0.68 pp |
| MEXC BCH-USDT | 9.62% | 9.62% | no improvement |

The larger value is risk control. On MEXC SOL-USDT, the calm state occupied 97.0% of observations
and had a 0.04 bps mean absolute next move; the stress state occupied 0.5%, had a 0.66 bps mean
move, and a 38% chance of exceeding 1 bps. MEXC DOGE moved from 0.06 bps/3.3% in calm to
0.88 bps/39.0% in stress. These are useful cancel/width/size states even where directional MSE
does not improve.

Provisional behavior by filtered state:

| State | Quote behavior |
|---|---|
| Calm | Quote both sides; optimize normal width over the empirical fill curve; normal size |
| Active | Shift center with fair-price signal; widen the endangered side; reduce size and lifetime |
| Stress | Cancel the endangered side immediately; quote only inventory-reducing side or suspend; use hard risk limits |

For a target fair value `F`, inventory `I`, and volatility `sigma`, use an inventory-adjusted
reservation price such as:

`reservation = F - inventory_penalty × I × sigma² × horizon`

Then choose bid and ask distances independently from a grid such as 1/2/3/5/8/13 bps. Each
distance must cover maker fee, state- and distance-conditioned adverse markout, latency risk,
inventory cost, and desired profit while retaining sufficient fill intensity. A strong upward
SOL signal raises both quotes and immediately pulls an endangered ask; a long inventory shifts
both quotes downward. The signal should control quote placement and cancellation before it is
used to justify directional market orders.

No hedge is required if open exposure is intentional. It does require:

- a soft inventory cap where skew becomes progressively stronger;
- a hard cap where orders that add to exposure are disabled;
- exit-only quoting beyond the hard cap rather than automatic cross-exchange hedging;
- coin and portfolio-level caps because SOL, DOGE, XRP, BCH, and LTC are correlated in stress;
- funding, liquidation-distance, and maximum drawdown limits;
- a kill switch for stale reference feeds, acknowledgement delays, reject bursts, and jumps.

## Data still needed before claiming profit

The public raw data is sufficient for fair-price, lead/lag, volatility, jump, liquidity, and
market-selection research. Proper maker evaluation additionally needs the private execution
record generated by paper orders or very small live orders:

- order decision, send, acknowledgement, exchange order ID, replace, cancel, and cancel-ack times;
- every partial fill, exact fee, maker/taker flag, and account fee tier;
- book state and signal snapshot at each decision and fill;
- queue-ahead estimate and trades/deletions at our price;
- 100 ms, 500 ms, 1 s, 5 s, and 30 s post-fill markouts;
- inventory, realized/unrealized PnL, funding, rejects, disconnects, and rate-limit state.

The next promotion test is therefore a replay/shadow quoter followed by minimum-size live orders.
The primary endpoint is net fill-conditioned PnL and markout by signal decile, venue, side,
inventory bucket, and volatility regime—not mid-price forecast accuracy alone.
