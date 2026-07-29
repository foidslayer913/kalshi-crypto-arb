# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

This repository currently contains only `SPEC.md` — no implementation exists yet. `SPEC.md` is the authoritative technical specification and should be read in full before writing any code. There are no build, lint, or test commands yet because no source files, dependency manifest, or test suite have been created. Once Phase 1 scaffolding lands (see below), update this file with the actual commands (e.g. `pytest`, `pytest tests/test_math.py::test_name` for a single test, linter invocation, `python main.py`).

## What this project is

An automated, low-latency research and paper-trading bot targeting short-duration crypto event contracts (hourly BTC/ETH threshold contracts) on Kalshi. It never places live trades — all trading operations target Kalshi's **Demo API** only.

### The core alpha (must be preserved in any implementation)

Kalshi crypto hourly contracts settle on the CF Benchmarks Real-Time Index (CFB RTI) average over the final 60 seconds before expiration. As seconds tick by, the range of possible final settlement averages shrinks:

- At second `k`, `k` of 60 price ticks are already fixed (sum `S_k`).
- `Guaranteed Floor Average_k = S_k / 60` (using `P_floor = 0` for crypto, the worst case for remaining ticks).
- If `S_k / 60 > Strike Price`, the contract is mathematically guaranteed to settle at $1.00 — a deterministic arbitrage if asks exist below that price after fees.

Kalshi taker fee: `Fee = ceil(0.07 * C * P * (1 - P))` where `C` = contract count, `P` = price in dollars. Trade only when `Net Yield = 1.00 - P_ask - Fee Per Contract >= Min Yield Threshold`.

Any math engine implementation must get this invariant exactly right — it's the entire trading edge. See SPEC.md sections 1 and 3 for the full derivation and edge cases (partial data, missing ticks).

## Intended architecture (per SPEC.md section 4-5)

Data flows one direction through four stages:

1. **Ingestion** (`ingestion/`) — `kalshi_ws.py` streams L2 order book data over Kalshi's WebSocket; `crypto_feed.py` records the crypto index at 1-second resolution.
2. **Strategy & math** (`strategy/`) — `math_engine.py` maintains the 60-slot rolling window and evaluates the floor-average invariant; `fee_calculator.py` applies the exact Kalshi fee formula to compute net yield per ask level.
3. **Execution** (`execution/`) — `demo_trader.py` signs REST requests with an RSA private key (RSA-SHA256) and places limit/market orders against the Kalshi Demo sandbox only. Includes a dry-run mode that simulates fills to detect book movement before order arrival ("phantom fills").
4. **Telemetry** (`telemetry/`) — `logger.py` records tick-to-order latency, phantom fill rate, and simulated P&L (SQLite or CSV).

`main.py` is the asyncio orchestrator tying these stages together; `config.py` loads settings via pydantic-settings from `.env`.

## Security and safety rules (non-negotiable)

- API Key ID and RSA private key are loaded only from a local `.env` file — never commit keys or `.env` to version control.
- All trading operations, at every phase, must be restricted to Kalshi's Demo environment (`https://external-api.demo.kalshi.co/trade-api/v2`, `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`). Never point execution code at Kalshi's production/live trading API.
- Implement and preserve a global kill-switch (max daily simulated loss breaker) once execution code exists.

## Implementation roadmap (SPEC.md section 6)

Work proceeds in three phases: (1) async setup + WebSocket ingestion, (2) math engine + fee calculator with unit tests covering partial-window evaluation (seconds 15/30/45/59), missing ticks, and fee calculations at 90¢/95¢/98¢, (3) demo execution with RSA-signed orders, dry-run phantom-fill simulation, and CSV latency/ROI reporting. Prefer implementing and testing the math engine (phase 2) thoroughly before wiring up execution, since correctness of the settlement invariant is what makes this strategy safe to run even in paper mode.
