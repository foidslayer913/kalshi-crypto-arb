# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

`SPEC.md` is the authoritative technical specification and should be read in full before writing any code. Phases 1-3 (ingestion, math engine + fee calculator, demo execution + telemetry) are implemented. `main.py` currently only runs the ingestion streams — it does not yet wire `strategy/math_engine.py`'s per-market settlement windows to `execution/demo_trader.py`'s order placement, since that requires deciding how each Kalshi market ticker maps to its settlement time/strike and crypto symbol, which SPEC.md doesn't specify. That end-to-end wiring is the natural next step.

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

1. **Ingestion** (`ingestion/`, implemented) — `kalshi_ws.py` (`KalshiWebSocketClient`) streams L2 order book data over Kalshi's WebSocket, reconnecting automatically via `run_forever()`; `crypto_feed.py` (`CryptoIndexFeed`) polls Coinbase spot prices once per second and keeps a rolling 60-tick `deque` per symbol as a proxy for the CFB RTI averaging window. `kalshi_auth.py` implements Kalshi's RSASSA-PSS request signing (shared by the WS client now, and will be reused by `execution/demo_trader.py` later — keep signing logic there rather than duplicating it).
2. **Strategy & math** (`strategy/`, implemented) — `math_engine.py`'s `SettlementWindow` tracks up to 60 ticks (`None` for a missing tick) and exposes `guaranteed_floor_average()` / `is_guaranteed_above(strike)` for the core $1.00 settlement invariant, plus the symmetric `guaranteed_ceiling_average()` / `is_guaranteed_below(strike)` for the $0.00 case. Missing ticks and not-yet-arrived ticks are both excluded from `cumulative_sum`, so they're automatically treated at `price_floor` (0 for crypto) — the conservative assumption the whole invariant depends on. `fee_calculator.py`'s `calculate_fee(contracts, price)` implements Kalshi's fee rounded up to the nearest **cent** (`ceil(0.07 * C * P * (1-P) * 100) / 100`) — SPEC.md states the formula without the `* 100`/`/ 100`, but evaluated in raw dollars every nonzero fee would round up to $1, which is wrong; see the comment in `fee_calculator.py`. `calculate_net_yield()` and `meets_yield_threshold()` build on it for the `1.00 - P_ask - Fee >= Min Yield Threshold` trade trigger.
3. **Execution** (`execution/`, implemented) — `demo_trader.py`'s `DemoTrader` signs REST requests using `ingestion/kalshi_auth.py` and places limit orders against the Kalshi Demo sandbox only (it raises at construction time if `settings.kalshi_base_url` isn't a demo URL, on top of the `config.py` guardrail). In dry-run mode (the default, `DRY_RUN=true`) it never touches the network: `simulate_fill()` compares a `BookQuote` from decision time against one from order time to detect phantom fills — book movement between signal and order arrival. `KillSwitch` (constructed with `MAX_DAILY_LOSS`) tracks realized simulated losses and makes every `place_order()` call raise `KillSwitchTripped` once the daily loss limit is reached; it does not auto-reset, so a new trading day means constructing a new one (or calling `.reset()`).
4. **Telemetry** (`telemetry/`, implemented) — `logger.py`'s `TelemetryLogger` appends `ExecutionRecord`s (tick-to-order latency, phantom fill flag, simulated P&L) to a CSV file and exposes `summary()` for aggregate phantom-fill-rate/latency/P&L stats.

`main.py` is the asyncio orchestrator; it currently runs the two ingestion streams concurrently via `asyncio.gather`. `config.py` loads settings via pydantic-settings from `.env`, exposing `market_tickers` / `crypto_feed_symbols` as parsed lists from comma-separated env vars, and **enforces the demo-only guardrail**: `Settings` raises a `ValueError` at load time if `KALSHI_BASE_URL` or `KALSHI_WS_URL` doesn't contain `"demo"`. Do not remove or weaken this check.

## Security and safety rules (non-negotiable)

- API Key ID and RSA private key are loaded only from a local `.env` file — never commit keys or `.env` to version control.
- All trading operations, at every phase, must be restricted to Kalshi's Demo environment (`https://external-api.demo.kalshi.co/trade-api/v2`, `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`). Never point execution code at Kalshi's production/live trading API.
- The global kill-switch (`execution/demo_trader.py`'s `KillSwitch`, max daily simulated loss breaker) must gate every order placement. Do not remove or bypass the `KillSwitch.check()` call in `DemoTrader.place_order()`.

## Implementation roadmap (SPEC.md section 6)

All three phases are implemented: ingestion; the math engine and fee calculator (`tests/test_math.py` covers partial-window evaluation at seconds 15/30/45/59, missing ticks, and fee calculations at 90¢/95¢/98¢); and demo execution with RSA-signed orders, dry-run phantom-fill simulation (`tests/test_execution.py`), and CSV telemetry (`tests/test_telemetry.py`). What's left is integration: wiring `main.py` to run a per-market `SettlementWindow`, feed it ticks from `CryptoIndexFeed`, evaluate `is_guaranteed_above`/net yield against live `KalshiWebSocketClient` order book asks, and call `DemoTrader.place_order()` when the threshold is met — plus deciding the market-ticker-to-strike/settlement-time/crypto-symbol mapping that makes that loop possible.
