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

## Architecture (per SPEC.md section 4-5)

Data flows one direction through four stages:

1. **Ingestion** (`ingestion/`, implemented) — `kalshi_ws.py` (`KalshiWebSocketClient`) streams L2 order book data over Kalshi's WebSocket, reconnecting automatically via `run_forever()`; `order_book.py`'s `OrderBookStore` consumes those `orderbook_snapshot`/`orderbook_delta` messages and maintains per-market resting-bid books. Kalshi markets only carry bids on both the yes/no side (no separate ask book), so the implied ask on one side is derived as `100 - opposing_side_best_bid` — see `OrderBookStore.implied_ask_dollars()`. `crypto_feed.py` (`CryptoIndexFeed`) polls Coinbase spot prices once per second and keeps a rolling 60-tick `deque` per symbol as a proxy for the CFB RTI averaging window. `kalshi_auth.py` implements Kalshi's RSASSA-PSS request signing, shared by `kalshi_ws.py`, `kalshi_rest.py`, and `execution/demo_trader.py` — keep signing logic there rather than duplicating it. `kalshi_rest.py`'s `KalshiRestClient.get_market(ticker)` fetches a market's `strike_type` (`"greater"`/`"less"`), strike price (`floor_strike`/`cap_strike` respectively), and `close_time`.
2. **Strategy & math** (`strategy/`, implemented) — `math_engine.py`'s `SettlementWindow` tracks up to 60 ticks (`None` for a missing tick) and exposes `guaranteed_floor_average()` / `is_guaranteed_above(strike)` for the core $1.00 settlement invariant, plus the symmetric `guaranteed_ceiling_average()` / `is_guaranteed_below(strike)` for the $0.00 case. Missing ticks and not-yet-arrived ticks are both excluded from `cumulative_sum`, so they're automatically treated at `price_floor` (0 for crypto) — the conservative assumption the whole invariant depends on. `guaranteed_ceiling_average()` special-cases a full window (`remaining == 0`) so the default `price_cap = inf` can't turn `0 * inf` into `nan`. `fee_calculator.py`'s `calculate_fee(contracts, price)` implements Kalshi's fee rounded up to the nearest **cent** (`ceil(0.07 * C * P * (1-P) * 100) / 100`) — SPEC.md states the formula without the `* 100`/`/ 100`, but evaluated in raw dollars every nonzero fee would round up to $1, which is wrong; see the comment in `fee_calculator.py`. `calculate_net_yield()` and `meets_yield_threshold()` build on it for the `1.00 - P_ask - Fee >= Min Yield Threshold` trade trigger. `scanner.py`'s `SettlementArbScanner` is the per-market runner: it sleeps until the final 60-second window opens, samples one crypto tick per second into its `SettlementWindow`, and on each tick checks the invariant against `OrderBookStore`'s implied ask, firing at most one `DemoTrader.place_order()` call per market once yield clears the threshold.
3. **Execution** (`execution/`, implemented) — `demo_trader.py`'s `DemoTrader` signs REST requests using `ingestion/kalshi_auth.py` and places limit orders against the Kalshi Demo sandbox only (it raises at construction time if `settings.kalshi_base_url` isn't a demo URL, on top of the `config.py` guardrail). In dry-run mode (the default, `DRY_RUN=true`) it never touches the network: `simulate_fill()` compares a `BookQuote` from decision time against one from order time to detect phantom fills — book movement between signal and order arrival. `KillSwitch` (constructed with `MAX_DAILY_LOSS`) tracks realized simulated losses and makes every `place_order()` call raise `KillSwitchTripped` once the daily loss limit is reached; it does not auto-reset, so a new trading day means constructing a new one (or calling `.reset()`).
4. **Telemetry** (`telemetry/`, implemented) — `logger.py`'s `TelemetryLogger` appends `ExecutionRecord`s (tick-to-order latency, phantom fill flag, simulated P&L) to a CSV file and exposes `summary()` for aggregate phantom-fill-rate/latency/P&L stats.

`main.py` is the asyncio orchestrator: it builds a `SettlementArbScanner` per configured market ticker (resolving each ticker's crypto symbol via `config.py`'s `market_crypto_symbols` prefix map, e.g. `KXBTC` → `BTC-USD`) and runs them alongside the two ingestion streams via `asyncio.gather`. `config.py` loads settings via pydantic-settings from `.env`, exposing `market_tickers` / `crypto_feed_symbols` / `market_crypto_symbols` as parsed values from comma-separated env vars, and **enforces the demo-only guardrail**: `Settings` raises a `ValueError` at load time if `KALSHI_BASE_URL` or `KALSHI_WS_URL` doesn't contain `"demo"`. Do not remove or weaken this check.

## Security and safety rules (non-negotiable)

- API Key ID and RSA private key are loaded only from a local `.env` file — never commit keys or `.env` to version control.
- All trading operations, at every phase, must be restricted to Kalshi's Demo environment (`https://external-api.demo.kalshi.co/trade-api/v2`, `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`). Never point execution code at Kalshi's production/live trading API.
- The global kill-switch (`execution/demo_trader.py`'s `KillSwitch`, max daily simulated loss breaker) must gate every order placement. Do not remove or bypass the `KillSwitch.check()` call in `DemoTrader.place_order()`.

## Implementation roadmap (SPEC.md section 6)

All three phases are implemented end-to-end: ingestion (including order book state and market metadata), the math engine and fee calculator (`tests/test_math.py`), demo execution with dry-run phantom-fill simulation and a kill-switch (`tests/test_execution.py`), CSV telemetry (`tests/test_telemetry.py`), and the `strategy/scanner.py` runner tying them together (`tests/test_scanner.py`, `tests/test_order_book.py`, `tests/test_kalshi_rest.py`). Since there's no live Kalshi Demo account in this environment, none of `kalshi_ws.py`, `kalshi_rest.py`, or `order_book.py` has been exercised against a real connection — the highest-value next step is running `main.py` against a real Demo account and fixing any mismatch between the assumed WebSocket/REST message shapes and Kalshi's actual ones.
