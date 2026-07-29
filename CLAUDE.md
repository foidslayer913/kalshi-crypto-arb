# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

`SPEC.md` is the authoritative technical specification and should be read in full before writing any code. Phases 1-3 (ingestion, math engine + fee calculator, demo execution + telemetry) are implemented, and `main.py` wires them end-to-end: for each configured market ticker, `strategy/scanner.py`'s `SettlementArbScanner` fetches the market's strike/close time, feeds it ticks from `CryptoIndexFeed`, evaluates the settlement invariant, and calls `DemoTrader.place_order()` against live order book asks once a net-positive opportunity appears. There is no live Kalshi Demo account in this environment, so the WebSocket message schema in `ingestion/order_book.py` (and the REST market schema in `ingestion/kalshi_rest.py`) is implemented from Kalshi's documented API shape but has not been exercised against a real connection — treat it as the first thing to verify against a real Demo account.

## Commands

```bash
pip install -r requirements.txt          # install dependencies
cp .env.example .env                     # then fill in KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH
python main.py                           # run the ingestion orchestrator
pytest                                   # run all tests
pytest tests/test_math.py::test_name     # run a single test

# Tier 2 backtest: score settlement signals against how markets actually settled
python -m backtest signal --markets settled.jsonl --series BTC-USD=btc_1s.csv

# Tier 1: inspect what the live capture has collected so far
python -m backtest capture --dir captures
```

There is no linter configured yet.

## What this project is

An automated, low-latency research and paper-trading bot targeting short-duration crypto event contracts (hourly BTC/ETH threshold contracts) on Kalshi. It never places live trades — all trading operations target Kalshi's **Demo API** only.

### The core alpha (must be preserved in any implementation)

Kalshi crypto hourly contracts settle on the CF Benchmarks Real-Time Index (CFB RTI) average over the final 60 seconds before expiration. As seconds tick by, the range of possible final settlement averages shrinks:

- At second `k`, `k` of 60 price ticks are already fixed (sum `S_k`).
- `Guaranteed Floor Average_k = S_k / 60` (using `P_floor = 0` for crypto, the worst case for remaining ticks).
- If `S_k / 60 > Strike Price`, the contract is mathematically guaranteed to settle at $1.00 — a deterministic arbitrage if asks exist below that price after fees.

Kalshi taker fee: `Fee = ceil(0.07 * C * P * (1 - P))` where `C` = contract count, `P` = price in dollars. Trade only when `Net Yield = 1.00 - P_ask - Fee Per Contract >= Min Yield Threshold`.

Any math engine implementation must get this invariant exactly right — it's the entire trading edge. See SPEC.md sections 1 and 3 for the full derivation and edge cases (partial data, missing ticks).

### Why the strict invariant alone is not a business

With `P_floor = 0`, the trigger `S_k / 60 > K` requires `p / K > 60 / k` when observed ticks are ≈ `p`. That means +1.69% above strike at k=59, +3.45% at k=58, +20% at k=50. Since Kalshi lists crypto strikes within a fraction of a percent of spot, **the strict bound essentially never closes with time left to trade** — the Tier 2 backtest measures 0 actionable signals across a synthetic 200-market run, and on the rare occasions it does close, the market is already quoted at 99¢+ with no yield left. Firing and available yield are anti-correlated by construction.

Hence the **relaxed variant**: `relative_floor(delta)` / `relative_cap(delta)` in `math_engine.py` assume unknown ticks land within `delta` of the latest observed price. This fires far earlier (median second ~48–57 for delta 0.5%–2%) but is no longer a mathematical guarantee — it holds unless the index moves more than `delta` inside the remainder of the window. That is a tail risk to be *measured* (`backtest/reconstruct.py`'s false-positive rate), not an invariant to be assumed. Keep the two clearly distinguished: `SettlementWindow` defaults to the strict bound, and relaxed behavior is opt-in via the policies.

## Architecture (per SPEC.md section 4-5)

Data flows one direction through four stages:

1. **Ingestion** (`ingestion/`, implemented) — `kalshi_ws.py` (`KalshiWebSocketClient`) streams L2 order book data over Kalshi's WebSocket, reconnecting automatically via `run_forever()`; `order_book.py`'s `OrderBookStore` consumes those `orderbook_snapshot`/`orderbook_delta` messages and maintains per-market resting-bid books. Kalshi markets only carry bids on both the yes/no side (no separate ask book), so the implied ask on one side is derived as `100 - opposing_side_best_bid` — see `OrderBookStore.implied_ask_dollars()`. `crypto_feed.py` (`CryptoIndexFeed`) polls Coinbase spot prices once per second and keeps a rolling 60-tick `deque` per symbol as a proxy for the CFB RTI averaging window. `kalshi_auth.py` implements Kalshi's RSASSA-PSS request signing, shared by `kalshi_ws.py`, `kalshi_rest.py`, and `execution/demo_trader.py` — keep signing logic there rather than duplicating it. `kalshi_rest.py`'s `KalshiRestClient.get_market(ticker)` fetches a market's `strike_type` (`"greater"`/`"less"`), strike price (`floor_strike`/`cap_strike` respectively), and `close_time`.
2. **Strategy & math** (`strategy/`, implemented) — `math_engine.py`'s `SettlementWindow` tracks up to 60 ticks (`None` for a missing tick) and exposes `guaranteed_floor_average()` / `is_guaranteed_above(strike)` for the core $1.00 settlement invariant, plus the symmetric `guaranteed_ceiling_average()` / `is_guaranteed_below(strike)` for the $0.00 case. Missing ticks and not-yet-arrived ticks are both excluded from `cumulative_sum`, so they're automatically treated at `price_floor` (0 for crypto) — the conservative assumption the whole invariant depends on. `guaranteed_ceiling_average()` special-cases a full window (`remaining == 0`) so the default `price_cap = inf` can't turn `0 * inf` into `nan`. `fee_calculator.py`'s `calculate_fee(contracts, price)` implements Kalshi's fee rounded up to the nearest **cent** (`ceil(0.07 * C * P * (1-P) * 100) / 100`) — SPEC.md states the formula without the `* 100`/`/ 100`, but evaluated in raw dollars every nonzero fee would round up to $1, which is wrong; see the comment in `fee_calculator.py`. `calculate_net_yield()` and `meets_yield_threshold()` build on it for the `1.00 - P_ask - Fee >= Min Yield Threshold` trade trigger. `scanner.py`'s `SettlementArbScanner` is the per-market runner: it sleeps until the final 60-second window opens, samples one crypto tick per second into its `SettlementWindow`, and on each tick checks the bounds against `OrderBookStore`'s implied ask, firing at most one `DemoTrader.place_order()` call per market once yield clears the threshold. Its `guaranteed_side()` helper maps (strike_type, which bound closed) to the side that wins: a "greater" market's YES pays out above the strike and a "less" market's YES pays out *below* the cap, and either bound closing decides the market — so a ceiling below the strike is just as tradeable as a floor above it, it simply decides the opposite side. Get this mapping wrong and the bot buys the losing side of a decided market.
3. **Execution** (`execution/`, implemented) — `demo_trader.py`'s `DemoTrader` signs REST requests using `ingestion/kalshi_auth.py` and places limit orders against the Kalshi Demo sandbox only (it raises at construction time if `settings.kalshi_base_url` isn't a demo URL, on top of the `config.py` guardrail). In dry-run mode (the default, `DRY_RUN=true`) it never touches the network: `simulate_fill()` compares a `BookQuote` from decision time against one from order time to detect phantom fills — book movement between signal and order arrival. `KillSwitch` (constructed with `MAX_DAILY_LOSS`) tracks realized simulated losses and makes every `place_order()` call raise `KillSwitchTripped` once the daily loss limit is reached; it does not auto-reset, so a new trading day means constructing a new one (or calling `.reset()`).
4. **Telemetry** (`telemetry/`, implemented) — `logger.py`'s `TelemetryLogger` appends `ExecutionRecord`s (tick-to-order latency, phantom fill flag, simulated P&L) to a CSV file and exposes `summary()` for aggregate phantom-fill-rate/latency/P&L stats.

5. **Backtest** (`backtest/`) — `reconstruct.py` (Tier 2) replays each expired market's final 60 seconds from a historical 1s index series (`PriceSeries`), runs it through the *same* `guaranteed_side()` the live scanner uses, and scores the signal against the market's actual settlement. Its headline metrics are `actionable_rate` (signals with time left to trade — note a bound that only closes at second 60 restates the settled result and is deliberately excluded) and `false_positive_rate` (acted-on signals where the market settled the other way, i.e. proxy-index divergence). `recorder.py` (Tier 1) tees live data to per-UTC-day JSONL so replay backtests become possible at all; see the capture format note below. `replay.py`, `fill_model.py`, and `report.py` are still unbuilt.

#### Capture format invariants (`backtest/recorder.py`)

Two properties of the on-disk format are load-bearing and must not be relaxed:

- **`t` is arrival time, not venue time.** It is when the process observed the event, so it is the earliest moment the bot could have acted on it. Index ticks also carry `ts` (when the price was sampled) — that field is data, never the pacing key. Pacing a replay off venue timestamps would hand the strategy information before it actually had it, which is the precise look-ahead bias the capture exists to rule out.
- **One merged, append-ordered stream.** Order book messages and index ticks interleave on disk exactly as they did live, so a replay driver walks one file forward instead of reconciling two sources.

WebSocket payloads are stored **unparsed**. The schema in `order_book.py` is written against Kalshi's documented shape and has never been checked against a live feed, so captures must stay replayable after that parsing is corrected. `record_*` only builds a dict and appends it; serialisation and disk I/O happen in `run_forever()`'s flush loop, keeping I/O off the latency path being measured. Market metadata is recorded too (`record_market`) because strike and close time come from a REST call replay will not repeat — without it a capture is not self-contained.

`clock.py` supplies the `Clock` protocol (`now()` / `async sleep()`) that lets scanner logic run identically live and simulated. `LiveClock` is the production default; `VirtualClock` advances instantly so a 60-second window replays in microseconds, in either a self-driving mode (`autoadvance=True`, for tests) or an externally-paced mode (`autoadvance=False` plus `await advance_to(t)`, which is what a replay harness needs so recorded events set the pace). A bare `asyncio.sleep(0)` used to yield to the event loop is a scheduling hop, not simulated time, and deliberately does not go through the clock.

`main.py` is the asyncio orchestrator: it builds a `SettlementArbScanner` per configured market ticker (resolving each ticker's crypto symbol via `config.py`'s `market_crypto_symbols` prefix map, e.g. `KXBTC` → `BTC-USD`) and runs them alongside the two ingestion streams via `asyncio.gather`. `config.py` loads settings via pydantic-settings from `.env`, exposing `market_tickers` / `crypto_feed_symbols` / `market_crypto_symbols` as parsed values from comma-separated env vars, and **enforces the demo-only guardrail**: `Settings` raises a `ValueError` at load time if `KALSHI_BASE_URL` or `KALSHI_WS_URL` doesn't contain `"demo"`. Do not remove or weaken this check.

## Security and safety rules (non-negotiable)

- API Key ID and RSA private key are loaded only from a local `.env` file — never commit keys or `.env` to version control.
- All trading operations, at every phase, must be restricted to Kalshi's Demo environment (`https://external-api.demo.kalshi.co/trade-api/v2`, `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`). Never point execution code at Kalshi's production/live trading API.
- The global kill-switch (`execution/demo_trader.py`'s `KillSwitch`, max daily simulated loss breaker) must gate every order placement. Do not remove or bypass the `KillSwitch.check()` call in `DemoTrader.place_order()`.

## Implementation roadmap (SPEC.md section 6)

All three phases are implemented end-to-end, plus the Tier 2 backtest. Two open items, in priority order:

1. **Run the Tier 2 backtest on real data.** It needs a JSONL of settled crypto markets (ticker, strike_type, strike_price, close_time, result, crypto_symbol, settlement_value) and a 1 Hz index series per symbol. Until it runs against a real proxy feed the false-positive rate is unmeasured, and that number is what decides whether any relaxed variant is safe to trade. A synthetic run reports FP=0 only because the signal and the settlement value come from the same series — zero divergence by construction, not a result.
2. **Verify the wire schemas.** There's no live Kalshi Demo account in this environment, so `kalshi_ws.py`, `kalshi_rest.py`, and `order_book.py` have never been exercised against a real connection. Run `main.py` against a real Demo account and fix any mismatch between the assumed WebSocket/REST message shapes and Kalshi's actual ones.

3. **Keep the capture running.** `backtest/recorder.py` is wired into `main.py` and writes to `CAPTURE_DIR` (default `captures/`, empty disables). Every day it is not running is a day of unrecoverable data — Kalshi publishes no historical order books, so the `(fire_second, best_ask)` joint distribution, fill rates, and the P&L-vs-latency curve are all unavailable until a capture exists. Budget roughly 150 bytes/event; at ~1M events/day that is ~150 MB/day uncompressed, so plan on gzipping rotated day files.

Still unbuilt: `replay.py` (drives `VirtualClock(autoadvance=False)` from a capture, feeding `OrderBookStore` and `CryptoIndexFeed` in arrival order), `fill_model.py` (optimistic / latency-shifted / queue-adversarial), and `report.py`. All three are blocked on having a real capture to run against, not on design.
