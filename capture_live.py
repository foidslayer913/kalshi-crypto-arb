"""Read-only Tier 1 capture against Kalshi's LIVE market data.

Why this exists as a separate entry point from `main.py`: Demo has no resting order book (its
books are synthetic), so the fill data the strategy ultimately needs — does an ask actually exist
below the trigger, and is it still there when an order would land — only exists on Live. Reading
Live public order books is a *different privilege* from placing Live orders (the same carve-out
`fetch_ground_truth.py` relies on for reading production settlement history).

This process is read-only BY CONSTRUCTION, not by a flag:

* It imports nothing from `execution/`. There is no `DemoTrader`, no `KillSwitch`, no scanner, and
  therefore no code path in this process that can submit an order. `DRY_RUN` does not enter into
  it — there is simply no order-placing code loaded.
* It only ever issues WebSocket subscribes and REST GETs.

It bypasses `config.Settings` (whose demo-only guardrail governs *order routing*, which this process
does not do) and points the ingestion clients at Live via a small duck-typed config. Credentials
still come from the local `.env`.

Run it, let it record through a settlement window or two, then inspect with
`python -m backtest capture --dir <capture_dir>`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from backtest.recorder import CaptureRecorder
from ingestion.crypto_feed import CryptoIndexFeed, PriceTick
from ingestion.kalshi_auth import load_private_key
from ingestion.kalshi_rest import MarketInfo, StrikeType
from ingestion.kalshi_ws import KalshiWebSocketClient
from ingestion.order_book import OrderBookStore
from scripts.list_markets import list_markets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

LIVE_BASE_URL = "https://api.elections.kalshi.com"
LIVE_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


@dataclass
class CaptureConfig:
    """The subset of settings the ingestion clients read, with Live endpoints. Duck-types
    `config.Settings` so `KalshiWebSocketClient` and friends work unchanged, without going through
    the demo-only guardrail (which is about order routing — not relevant to a read-only reader)."""

    kalshi_api_key_id: str
    kalshi_private_key_path: str
    kalshi_base_url: str
    kalshi_ws_url: str
    market_tickers: list[str] = field(default_factory=list)
    crypto_feed_symbols: list[str] = field(default_factory=list)

    @property
    def private_key_pem(self) -> bytes:
        with open(self.kalshi_private_key_path, "rb") as handle:
            return handle.read()


def _load_config(base_url: str, ws_url: str) -> CaptureConfig:
    load_dotenv()
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key_id or not key_path:
        raise SystemExit("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in .env")
    symbols_raw = os.getenv("CRYPTO_FEED_SYMBOLS", "BTC-USD,ETH-USD")
    symbols = [symbol.strip() for symbol in symbols_raw.split(",") if symbol.strip()]
    return CaptureConfig(
        kalshi_api_key_id=api_key_id,
        kalshi_private_key_path=key_path,
        kalshi_base_url=base_url,
        kalshi_ws_url=ws_url,
        crypto_feed_symbols=symbols,
    )


def _market_info_from_dict(raw: dict) -> MarketInfo | None:
    """Build MarketInfo from a markets-list row, skipping strikes we don't trade. Mirrors
    `kalshi_rest._parse_market` but tolerant: discovery sweeps many strikes and we just skip the
    unsupported ones rather than raising."""
    strike_type: StrikeType | str = raw.get("strike_type", "")
    if strike_type == "greater":
        strike = raw.get("floor_strike")
    elif strike_type == "less":
        strike = raw.get("cap_strike")
    else:
        return None
    if strike is None or raw.get("close_time") is None:
        return None
    close_time = datetime.fromisoformat(raw["close_time"].replace("Z", "+00:00"))
    return MarketInfo(
        ticker=raw["ticker"],
        strike_type=strike_type,  # type: ignore[arg-type]
        strike_price=float(strike),
        close_time=close_time.astimezone(timezone.utc),
    )


def discover_near_term_markets(
    config: CaptureConfig, series: list[str], within_hours: float, max_markets: int
) -> list[MarketInfo]:
    """Find open markets across the given series that close within `within_hours`, nearest first.

    Capturing every open strike (there are thousands, most settling years out) would be pure waste.
    The near-term contracts are where the settlement window — and any liquidity — actually is."""
    private_key = load_private_key(config.private_key_pem)
    now = datetime.now(timezone.utc)
    horizon = now.timestamp() + within_hours * 3600
    found: list[MarketInfo] = []
    for series_ticker in series:
        rows = list_markets(
            config.kalshi_base_url, config.kalshi_api_key_id, private_key,
            series_ticker, status="open", limit=1000,
        )
        for raw in rows:
            info = _market_info_from_dict(raw)
            if info is None:
                continue
            close_ts = info.close_time.timestamp()
            if now.timestamp() < close_ts <= horizon:
                found.append(info)
    found.sort(key=lambda market: market.close_time)
    return found[:max_markets]


async def run_capture(
    config: CaptureConfig, markets: list[MarketInfo], capture_dir: str
) -> None:
    order_book = OrderBookStore()
    recorder = CaptureRecorder(capture_dir)

    def on_ws_message(message: dict[str, Any]) -> None:
        # Record before parsing: the capture must reflect what actually arrived even if the
        # (still-unverified) order book parsing rejects it.
        recorder.record_ws(message)
        order_book.apply(message)

    def on_price_tick(tick: PriceTick) -> None:
        recorder.record_tick(tick)

    recorder.record_session()
    for market in markets:
        # Strike and close time come from REST and a replay will not repeat that call, so they
        # have to live in the capture or the recording is not self-contained.
        recorder.record_market(market)

    config.market_tickers = [market.ticker for market in markets]
    ws_client = KalshiWebSocketClient(config, on_message=on_ws_message)  # type: ignore[arg-type]
    crypto_feed = CryptoIndexFeed(config.crypto_feed_symbols, on_tick=on_price_tick)

    logger.info(
        "Capturing %d live market(s) + %s to %s. READ-ONLY: no trader loaded, cannot place orders. "
        "Ctrl+C to stop.",
        len(markets), config.crypto_feed_symbols, capture_dir,
    )
    try:
        await asyncio.gather(
            ws_client.run_forever(), crypto_feed.run_forever(), recorder.run_forever()
        )
    finally:
        recorder.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--series", action="append", default=None,
        help="Series to capture (repeatable). Default: KXBTCD. e.g. --series KXBTCD --series KXETHD",
    )
    parser.add_argument("--hours", type=float, default=6.0, help="Only capture markets closing within this many hours.")
    parser.add_argument("--max-markets", type=int, default=150, help="Cap on markets subscribed, nearest close first.")
    parser.add_argument("--capture-dir", default=os.getenv("CAPTURE_DIR", "captures"))
    parser.add_argument("--base-url", default=LIVE_BASE_URL, help="Live REST base URL.")
    parser.add_argument("--ws-url", default=LIVE_WS_URL, help="Live WebSocket URL.")
    args = parser.parse_args()

    series = args.series or ["KXBTCD"]
    config = _load_config(args.base_url, args.ws_url)

    logger.info("Discovering %s markets closing within %.1fh on %s ...", series, args.hours, args.base_url)
    markets = discover_near_term_markets(config, series, args.hours, args.max_markets)
    if not markets:
        raise SystemExit(
            f"No {series} markets close within {args.hours}h. Try a larger --hours, or check the series."
        )
    soonest = min(markets, key=lambda m: m.close_time).close_time
    latest = max(markets, key=lambda m: m.close_time).close_time
    logger.info("Found %d markets; closes span %s .. %s UTC", len(markets), soonest, latest)

    asyncio.run(run_capture(config, markets, args.capture_dir))


if __name__ == "__main__":
    main()
