# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

`SPEC.md` is the authoritative technical specification and should be read in full before writing any code. Phase 1 (setup + ingestion) is implemented: `config.py` and the `ingestion/` package exist and are wired up in `main.py`. Phases 2 and 3 (`strategy/`, `execution/`, `telemetry/`) are empty packages awaiting implementation — see the roadmap below.

## Commands

```bash
pip install -r requirements.txt          # install dependencies
cp .env.example .env                     # then fill in KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH
python main.py                           # run the ingestion orchestrator
```

There is no test suite or linter configured yet. When `tests/test_math.py` is added per the roadmap, use `pytest` (all tests) or `pytest tests/test_math.py::test_name` (a single test).

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
2. **Strategy & math** (`strategy/`, not yet implemented) — `math_engine.py` should maintain the 60-slot rolling window and evaluate the floor-average invariant; `fee_calculator.py` should apply the exact Kalshi fee formula to compute net yield per ask level.
3. **Execution** (`execution/`, not yet implemented) — `demo_trader.py` should sign REST requests using `ingestion/kalshi_auth.py` and place limit/market orders against the Kalshi Demo sandbox only, with a dry-run mode that simulates fills to detect book movement before order arrival ("phantom fills").
4. **Telemetry** (`telemetry/`, not yet implemented) — `logger.py` should record tick-to-order latency, phantom fill rate, and simulated P&L (SQLite or CSV).

`main.py` is the asyncio orchestrator; it currently runs the two ingestion streams concurrently via `asyncio.gather`. `config.py` loads settings via pydantic-settings from `.env`, exposing `market_tickers` / `crypto_feed_symbols` as parsed lists from comma-separated env vars, and **enforces the demo-only guardrail**: `Settings` raises a `ValueError` at load time if `KALSHI_BASE_URL` or `KALSHI_WS_URL` doesn't contain `"demo"`. Do not remove or weaken this check.

## Security and safety rules (non-negotiable)

- API Key ID and RSA private key are loaded only from a local `.env` file — never commit keys or `.env` to version control.
- All trading operations, at every phase, must be restricted to Kalshi's Demo environment (`https://external-api.demo.kalshi.co/trade-api/v2`, `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`). Never point execution code at Kalshi's production/live trading API.
- Implement and preserve a global kill-switch (max daily simulated loss breaker) once execution code exists.

## Implementation roadmap (SPEC.md section 6)

Phase 1 (async setup + WebSocket ingestion) is done. Remaining work: (2) math engine + fee calculator with unit tests covering partial-window evaluation (seconds 15/30/45/59), missing ticks, and fee calculations at 90¢/95¢/98¢, (3) demo execution with RSA-signed orders, dry-run phantom-fill simulation, and CSV latency/ROI reporting. Prefer implementing and testing the math engine (phase 2) thoroughly before wiring up execution, since correctness of the settlement invariant is what makes this strategy safe to run even in paper mode.
