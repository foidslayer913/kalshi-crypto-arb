# Findings: measured results against real Kalshi data

Everything here comes from Kalshi production market data and Binance index history, not simulation.
Reproduction commands are at the end. Dates refer to July 2026.

## Summary

Five strategy variants were tested. All five fail, and they fail for one underlying reason:

> **The cost of crossing the spread exceeds the market's pricing error.**

Measured across 53,571 opportunities in 2,749 markets, the barrier decomposes as:

| component | size |
| --- | --- |
| how far the **ask** sits above fair value | **0.44¢** |
| **taker fee** | **1.16¢** |
| total cost to buy at the ask | **1.60¢** |
| the market's own pricing error | **0.86¢** |

The decomposition is the important part. The ask is very nearly fair — the market is not
mispricing anything worth taking — and **the fee, not the market's error, is the dominant term**.
So the problem was never prediction quality. Every variant below paid ~1.6¢ to trade at roughly
fair value, and no amount of model improvement can recover a cost that large.

This is a *taker* result. It says nothing about resting orders, which is why market making is the
one avenue left with different arithmetic (see "What remains open").

## 1. Strict settlement invariant — fails on timing

`S_k / 60 > K` is a genuine mathematical guarantee, and the Tier 2 backtest confirms it never
misfires (false-positive rate 0.0% across 123,380 settled `KXBTCD` markets).

It is also unusable. With `P_floor = 0` the bound requires the observed price to sit ~1.7% above the
strike at second 59, ~3.5% at second 58, ~20% at second 50 — while Kalshi lists strikes a fraction
of a percent from spot. Median fire second: **55**. Actionable rate: 45%. By the time it closes the
market is quoted at 99¢ with no yield left.

## 2. Relaxed variant — fails on liquidity

`relative_floor(delta)` / `relative_cap(delta)` assume unknown ticks land within `delta` of the last
observed price. This fires far earlier (median second **1** at delta 1%) with a false-positive rate
of **0.1–0.2%**, comfortably under the ~1% break-even bar.

On paper that looked like a business. It is not, because **firing and liquidity are disjoint sets**.
A live capture of 386,263 order book deltas across a settlement window showed:

- The markets that fire early are far-from-money strikes whose outcome is already decided.
- On a decided market nobody rests a bid on the losing side, so there is no ask to buy the winning
  side. Observed pattern: a 99¢ bid on the winning side and **no offer at all**.
- The 386K deltas were concentrated in near-money strikes — which never fire, because they are
  genuinely undecided.

## 3. Unconditional calibration — mispricing ≈ the fee

Kalshi's candlestick history carries `yes_bid` / `yes_ask` per minute, so "when a side is offered at
price P, how often does it win?" can be answered over months of history. Across **2,749** `KXBTC15M`
markets and 35,685 tradeable observations:

There *is* a real favourite–longshot bias — gross mispricing averages **+1.15¢** across favourites
(≥78¢), with 12 of 17 buckets positive. But:

- The magnitude is the same size as the fee, which is what an efficient market looks like.
- It does not hold up across a held-out split: **+0.75¢** in July 1–15 versus **+1.40¢** in July
  16–30, with 3 of 11 buckets flipping sign.

No bucket is profitable once uncertainty is accounted for.

## 4. Conditional index divergence — the market wins decisively

The strongest test, and the one that matches what a discretionary trader actually does: standardise
the index's distance from the strike by the move still available
(`z = ln(index/strike) / (sigma*sqrt(minutes))`), learn `P(win | z)` empirically on July 1–15, and
evaluate on July 16–30.

Measured volatility: **sigma = 0.0478%/minute**, i.e. ~34.7% annualised. The learned table is
strongly monotone (9.5% at z = −3 to 98.7% at z = +4), so `z` is genuinely informative.

It still loses, because the market already knows everything `z` knows:

| predictor | mean absolute error vs realized outcome |
| --- | --- |
| market price | **0.0086** |
| index model | 0.1616 (18.7x worse) |

In every divergence bucket the realized rate tracks the **price**, not the model. Where they
disagree, the disagreement is model error. No positive-divergence bucket is profitable out of
sample.

### Worked example

The setup that motivated this study — index 0.13% below the line, 5 minutes left, "down" quoted at
91¢:

- sigma over 5 minutes = 0.107%, so 0.13% is only **z = 1.22**
- empirical win rate at that z: **77.2%**
- break-even at 91¢ (100-contract fee): **91.6%**
- **EV = −14.4¢ per contract**

The market was overpricing that side, not lagging. A 1.2-sigma move reads intuitively as
near-certainty but is about 77%.

## 5. Buying the dip — fails; sharp moves carry information

Does a violent move overshoot, leaving the price too cheap right after? Distinct from the level
question, and the only variant here about *dynamics*. Measured over 53,571 opportunities by the size
of the bought side's price move over the preceding 2 minutes:

**Every one of the 18 buckets, across both held-out halves, has a negative point estimate.** Not one
positive, at any move size, in either period.

After a drop of 25¢ or more — the exact scenario of a 96¢ contract collapsing to 71¢:

| period | edge/contract | 95% CI |
| --- | --- | --- |
| Jul 1–15 | **−3.00¢** | −4.68 to −1.32 (significantly negative) |
| Jul 16–30 | **−1.71¢** | −3.49 to +0.07 |

Sharp moves **continue** rather than revert: the collapse was information, not panic. A 96¢ contract
falling to 71¢ is the market correctly repricing a 2σ index move, and 71¢ is then roughly fair —
71¢ contracts win about 72% of the time whether or not they just collapsed. Treating the prior 96¢
as evidence of value anchors on a price the index has already invalidated.

## What remains open

**Sub-minute dislocation.** Every price study here samples 1-minute candle *closes*. Within a single
observed minute `yes_ask` ranged **0.44 → 1.00**, so a dislocation lasting seconds is invisible to
this analysis and might be capturable live. Testing it needs the Tier 1 capture (`capture_live.py`,
order book parser now verified against the live feed). Prior is low: if price tracks truth to 0.86
points on minute closes, systematic sub-minute room is thin and contested by faster participants.

**Market making — the one avenue with different arithmetic.** This is the natural conclusion of the
cost decomposition above, and the only idea here that stops fighting the market's accuracy and
attacks the dominant term instead. Every variant tested pays 1.60¢ to cross: 1.16¢ of fee plus 0.44¢
of ask. A resting bid inverts the second part — with the ~1¢ spread observed on these markets, you
buy roughly 0.5¢ *below* mid instead of 0.44¢ above it, a swing close to 1¢ per trade, which is
larger than any mispricing measured anywhere in this document.

Two things decide whether it is real, and neither is known yet:

1. **The maker fee.** If it is comparable to the taker fee the idea dies immediately. Check Kalshi's
   schedule before building anything.
2. **Adverse selection**, which is the actual risk and is measurable. A resting bid fills precisely
   when someone wants to sell, which correlates with the price about to fall — so the spread earned
   may be smaller than the losses on fills. This needs the Tier 1 capture: with the sub-second order
   book recorded (`capture_live.py`, parser now verified), measure how the mid moves in the seconds
   *after* a fill would have occurred at the resting price. That number, not the spread, decides it.

Note the asymmetry that makes this worth measuring: the taker results are conclusive because the
cost is deterministic and known. The maker case is genuinely open because its main cost —
adverse selection — has never been measured here.

## Corrections made along the way

Recorded because each one initially produced a plausible but wrong result:

- **Order book schema.** `order_book.py` was written from Kalshi's docs; the live feed sends
  `yes_dollars_fp` / `no_dollars_fp` with dollar-string prices and fixed-point sizes, and deltas
  carry `price_dollars` / `delta_fp`. Verified against a live capture and fixed.
- **`greater_or_equal` strike type.** `guaranteed_side` branched on `== "greater"` and fell through
  to the `less` branch, mapping an above-strike market to the **losing** side. Unknown strike types
  now raise.
- **`settlement_timer_seconds` is not the averaging window.** `KXBTC15M` reports `1` while its rules
  specify a sixty-second BRTI average. Deriving a window from that field would discard the invariant.
- **`volume_fp`, not `volume`.** Reading the wrong key made every market look untraded.
- **Fee at order scale.** The fee rounds up per *order*, so 1 contract at 91¢ pays 1¢ while 100 pay
  0.573¢ each. Costing a 1–2¢ effect with the single-contract fee overstated costs by 40–75%.
- **Pooled estimator weighting.** Averaging within markets then across markets inverted a result: a
  losing price range reported a positive edge, because markets passing briefly through a band
  contribute one observation while those sitting in it contribute fifteen.
- **Fill-analysis coverage.** A capture spanning two runs treated the gap between them as covered,
  so markets whose window was never recorded reported "no ask" — indistinguishable from a finding.

## Reproducing

```bash
# Settled markets + index history
python scripts/fetch_ground_truth.py markets --series-ticker KXBTCD -o data/settled.jsonl
python -m scripts.fetch_index_minutes --symbol BTC-USD --start 2026-07-01 --end 2026-07-31

# 1 and 2: settlement invariant
python -m backtest signal --markets data/settled.jsonl --series BTC-USD=data/BTC-USD.csv

# 3: unconditional calibration (fetch is ~25 min)
python -m scripts.fetch_calibration --series-ticker KXBTC15M \
    --start 2026-07-01 --end 2026-07-30 -o data/calibration.jsonl
python -m backtest calibration --observations data/calibration.jsonl --contracts 100

# 4: conditional index divergence, trained and tested on split dates
python -m backtest conditional --observations data/calibration.jsonl \
    --index data/BTC-USD-1m.csv --train-end 2026-07-15

# 5: does a violent move overshoot? (prints both halves for replication)
python -m backtest reaction --observations data/calibration.jsonl --train-end 2026-07-15
```

`python -m backtest conditional` prints a warning if the learned `P(win | z)` table is not monotone
in `z`. That is the canary for a broken index join — if it fires, nothing downstream is meaningful.
