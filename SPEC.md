# Technical Specification: Kalshi Crypto Settlement Arbitrage Bot

## 1. Executive Summary & Strategy Overview
This document outlines the technical specification for an automated, low-latency research and paper-trading bot targeting short-duration crypto event contracts (e.g., hourly BTC/ETH threshold contracts) on **Kalshi**.

### The Core Alpha (Settlement Determinism)
Kalshi crypto hourly contracts settle based on the **CF Benchmarks Real-Time Index (CFB RTI)** 60-second volume-weighted/simple average during the final minute before contract expiration (e.g., 2:59:00 PM to 3:00:00 PM).

As time progresses through the final minute ($t = 1 \dots 60$), the variance of the final settlement price shrinks to zero:
* At second $k$, $k$ out of 60 price data points are fixed.
* The system calculates the theoretical worst-case minimum average assuming all remaining $60-k$ prices are 0 (or extreme lower bound).
* If $\text{Theoretical Worst-Case Average} > \text{Strike Price}$, the contract **must settle at $1.00 (100¢)** with 100% mathematical certainty.
* If order book asks exist below this guaranteed payout (e.g., at 93¢–95¢), an immediate net-positive arbitrage opportunity exists after accounting for Kalshi's taker fees.

---

## 2. Environment, Guardrails & Security

* **API Target:** Kalshi **Demo API** (`https://external-api.demo.kalshi.co/trade-api/v2`)
* **WebSocket Endpoint:** `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`
* **Authentication:** RSA Private Key (PEM format) & API Key ID.
* **Security Rules:**
  1. API keys and RSA private keys must be loaded strictly from local `.env` files. Never commit keys to version control.
  2. All trading operations MUST be restricted to the **Demo Environment**.
  3. Include a global kill-switch mechanism (max daily simulated loss breaker).

---

## 3. Mathematical Model & Invariants

### A. Settlement Price Accumulator
Let $P_i$ be the CFB RTI index price tick recorded at second $i \in \{1, 2, \dots, 60\}$ during the expiration minute.

$$\text{Cumulative Sum at second } k = S_k = \sum_{i=1}^{k} P_i$$

### B. Invariant Bounds
At any second $k$ ($1 \le k \le 60$):
$$\text{Min Final Average}_k = \frac{S_k + (60 - k) \times P_{\text{floor}}}{60}$$
$$\text{Max Final Average}_k = \frac{S_k + (60 - k) \times P_{\text{cap}}}{60}$$

For crypto, $P_{\text{floor}} = 0$. Thus:
$$\text{Guaranteed Floor Average}_k = \frac{S_k}{60}$$

If $\frac{S_k}{60} > \text{Strike Price}$, Probability of Payout $1.00 = 100\%$.

### C. Fee Model & Net Yield Determination
Kalshi taker fee formula per contract:
$$\text{Fee} = \lceil 0.07 \times C \times P \times (1 - P) \rceil$$
Where:
* $C$ = Number of contracts
* $P$ = Probability / Price in dollars (e.g., $0.95$ for $95¢$)

**Net Expected Value per Contract:**
$$\text{Net Yield} = 1.00 - P_{\text{ask}} - \text{Fee Per Contract}$$

Trigger execution **ONLY IF** $\text{Net Yield} \ge \text{Min Yield Threshold}$ (e.g., $\$0.01$ or $1¢$ per contract).

---

## 4. System Architecture & Component Breakdown

```
                   +-----------------------------------+
                   |     External Price Data Feed      |
                   |   (CFB RTI / Binance / Coinbase)  |
                   +-----------------+-----------------+
                                     |
                                     v
+-----------------------------------------------------------------------+
|                         INGESTION ENGINE                              |
|  - Kalshi WS Client: Order book bids, asks, depth (L2)                 |
|  - Crypto Index Listener: 1-second interval price recorder            |
+-----------------------------------+-----------------------------------+
                                     |
                                     v
+-----------------------------------------------------------------------+
|                         STRATEGY & MATH ENGINE                        |
|  - Maintains 60-slot rolling window buffer                            |
|  - Evaluates Invariant Floor: Floor_Avg > Strike                      |
|  - Computes exact taker fees and net ROI per ask level                |
+-----------------------------------+-----------------------------------+
                                     |
                                     v
+-----------------------------------------------------------------------+
|                        DEMO EXECUTION ENGINE                          |
|  - Formats & signs REST orders via RSA private key                    |
|  - Places limit/market buy orders on Kalshi Sandbox                   |
|  - Monitors latency, fill ratios, and slippage                        |
+-----------------------------------+-----------------------------------+
                                     |
                                     v
+-----------------------------------------------------------------------+
|                         TELEMETRY & LOGGING                           |
|  - Logs tick-to-order latency, phantom fill rate, net simulated P&L   |
+-----------------------------------------------------------------------+
```

---

## 5. Repository Structure

```text
kalshi_arb_bot/
├── .env.example              # Sample configuration template
├── README.md                 # Project documentation
├── SPEC.md                   # Technical specification file
├── requirements.txt          # Python dependencies
├── main.py                   # Asyncio orchestrator and event loop
├── config.py                 # Configuration loader (pydantic-settings)
├── ingestion/
│   ├── __init__.py
│   ├── kalshi_ws.py          # Kalshi L2 Order Book WebSocket Client
│   └── crypto_feed.py        # Real-time crypto index feed recorder
├── strategy/
│   ├── __init__.py
│   ├── math_engine.py        # 60s accumulator and deterministic bound logic
│   └── fee_calculator.py     # Kalshi probability fee formula calculator
├── execution/
│   ├── __init__.py
│   └── demo_trader.py        # Kalshi Demo REST API client & order manager
└── telemetry/
    ├── __init__.py
    └── logger.py             # Performance metric logger (SQLite / CSV)
```

---

## 6. Implementation Roadmap for Claude Code

### Phase 1: Setup & Data Pipelines
1. Initialize Python 3.11+ async environment with `httpx`, `websockets`, `pydantic`, `cryptography`.
2. Implement `config.py` loading `.env` variables (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_BASE_URL`).
3. Build `ingestion/kalshi_ws.py` to stream real-time order books for active hourly BTC/ETH markets using `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`.

### Phase 2: Math Engine & Unit Tests
1. Implement `strategy/math_engine.py` with 60-second rolling buffer logic.
2. Build `strategy/fee_calculator.py` using Kalshi's exact ceiling formula.
3. Write comprehensive unit tests in `tests/test_math.py` covering:
   * Partial array evaluation at second 15, 30, 45, 59.
   * Edge cases with missing ticks.
   * Fee calculations at 90¢, 95¢, 98¢ prices.

### Phase 3: Paper Execution & Telemetry
1. Implement `execution/demo_trader.py` using Kalshi RSA signature header generation (`RSA-SHA256`).
2. Integrate dry-run simulation mode to log "phantom fills" (evaluating whether the book moved before order arrival).
3. Generate detailed CSV execution reports measuring latency (in ms) and net ROI after simulated fees.

---

## 7. How to Direct Claude Code
1. Place this file as `SPEC.md` in your project root directory.
2. Run `claude` in your terminal.
3. Prompt: `"Read SPEC.md. Scaffold the repository structure and implement Phase 1: Setup & Data Pipelines."`