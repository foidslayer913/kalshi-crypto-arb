# Kalshi per-second market data — schema and caveats

One row per market per second, covering the window before each market settles.

Produced by `scripts/build_timeseries.py` from a raw capture. If you are consuming this dataset,
this file is the contract: the columns, what they mean, and — more importantly — the things that
would silently corrupt your results if you assumed something different.

## Read this first: timing semantics

**Every timestamp is arrival time — when the recording process observed the event — not a venue
timestamp.**

This is deliberate and it is the single most important property of the dataset. A venue timestamp
tells you when something happened at Kalshi; an arrival timestamp tells you the earliest moment a
participant could have *acted* on it. Pacing a backtest off venue timestamps hands your strategy
information fractionally before it existed, which is the classic way a backtest produces returns
that evaporate live.

The practical consequence: a row stamped `t` reflects only information available at `t`. You can
walk this dataset forward and trust that you are never reading the future.

## Columns

| column | type | meaning |
| --- | --- | --- |
| `t` | int | Unix second (UTC). Arrival time, per above. |
| `ticker` | str | Kalshi market ticker, e.g. `KXBTC15M-26JUL300415`. |
| `seconds_to_close` | int | Seconds until this market settles. Counts down; `0` is the settlement instant. |
| `yes_bid` | int \| null | Best resting bid on YES, in cents. |
| `yes_ask` | int \| null | Cheapest price to **buy** YES, in cents. **Derived** — see below. |
| `no_bid` | int \| null | Best resting bid on NO, in cents. |
| `no_ask` | int \| null | Cheapest price to buy NO, in cents. Derived. |
| `yes_bid_size` | float | Contracts resting at `yes_bid`. |
| `yes_ask_size` | float | Contracts available at `yes_ask`. |
| `mid` | float \| null | `(yes_bid + yes_ask) / 2`. Null when either side is unquoted. |
| `spread` | int \| null | `yes_ask - yes_bid`, in cents. |
| `strike` | float | The market's threshold. For 15-minute up/down series this is the index average at the period open. |
| `index_price` | float \| null | Underlying index (e.g. BTC) at `t`. See sourcing note. |
| `index_distance_pct` | float \| null | Signed % distance of the index from `strike`. Positive favours YES. |
| `stale_seconds` | int | Seconds since this market's book last changed. `0` means it moved this second. |

## Why `yes_ask` is derived, and why that matters

**Kalshi order books contain only resting bids.** There is no separate ask book. Buying YES at price
`P` is economically identical to someone selling YES — i.e. bidding NO — at `100 - P`. So:

```
yes_ask = 100 - no_bid
no_ask  = 100 - yes_bid
```

If you expected a native ask column and treated its absence as missing data, you would discard most
of the book. `yes_ask` is a real, executable price; it is simply computed rather than quoted.

A corollary that trips people up: `yes_ask` is `null` when **nobody is bidding NO**, not when the
market is untradeable in some general sense. A market can have a healthy YES bid and no YES ask at
all, which is exactly what happens on contracts whose outcome is already decided — nobody offers
the winning side cheaply.

## Row emission rules

**A row exists for every second in a market's window**, not only when something changed. A gap in an
event-driven log is ambiguous: did nothing happen, or was nothing recorded? Emitting every second
and reporting `stale_seconds` makes the distinction explicit.

**A market that was never quoted produces no rows at all.** An empty row would assert that we
observed an empty book, when in fact we never saw one.

**Only markets near their close are included** (default: within 20 minutes). Kalshi lists thousands
of open strikes settling years out; including them all would produce tens of millions of rows of
static, unquoted book per day.

## Index sourcing — read before using `index_price`

The index column is **not** purely as-observed. It is filled in this order:

1. A live tick recorded by the capture process, if one arrived within the last 5 seconds.
2. Otherwise, a backfilled value from a historical 1-minute series (Binance).

This is a deliberate trade. The live crypto feed is rate-limited and in practice dropped ~97% of
its ticks, leaving the column nearly empty. Unlike the order book — which Kalshi does not publish
historically and which therefore *only* exists if captured live — the index is public and exactly
reconstructable after the fact. Backfilling costs nothing in fidelity.

Two consequences to hold onto:

- **Backfilled values have 1-minute granularity**, so within a minute the index can appear static
  when it was in fact moving. If your work depends on sub-minute index resolution, do not use this
  column; source a 1-second series directly.
- **The index is a proxy.** Kalshi's crypto contracts settle on the CF Benchmarks BRTI, not on
  Binance. The two track closely but are not identical, and the difference matters for anything
  measuring near-settlement precision.

## Known limitations

- **Coverage is not guaranteed continuous.** Run `scripts/dataset_quality.py` for the actual
  market-second continuity, gap list, and per-column completeness of any given build. Treat that
  report, not this document, as the statement of what is present.
- **Top of book only.** Depth beyond the best bid on each side is not retained in these rows, though
  it is present in the raw capture.
- **No trade prints.** This is quote data. Trades are a separate Kalshi endpoint and are not merged
  in here.
- **Settlement outcomes are not in this file.** Join on `ticker` against settled-market data from
  `scripts/fetch_ground_truth.py`.
- **Prices are integer cents.** Kalshi sends dollar strings on the wire (`"0.4500"`); they are
  normalised to cents here, which is lossless for whole-cent contract prices.

## Provenance

Built from a read-only capture of Kalshi's public WebSocket order book feed. The capture process
loads no order-placing code — a test asserts this structurally by walking its import graph — so it
cannot trade, only observe.
