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
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from backtest.recorder import CaptureRecorder, compress_completed_days, export_completed_days
from ingestion.crypto_feed import CryptoIndexFeed, PriceTick
from ingestion.kalshi_auth import load_private_key
from ingestion.kalshi_rest import (
    ABOVE_STRIKE_TYPES,
    BELOW_STRIKE_TYPES,
    MarketInfo,
    StrikeType,
)
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
    # Live and Demo are separate environments with separate API keys: a Demo key gets a 401 on the
    # Live WebSocket. Prefer dedicated KALSHI_LIVE_* vars so Demo creds (used by main.py) and Live
    # creds (used here, read-only) can coexist in one .env; fall back to the standard names.
    api_key_id = os.getenv("KALSHI_LIVE_API_KEY_ID") or os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_LIVE_PRIVATE_KEY_PATH") or os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key_id or not key_path:
        raise SystemExit(
            "Set KALSHI_LIVE_API_KEY_ID and KALSHI_LIVE_PRIVATE_KEY_PATH in .env (a key generated "
            "on your LIVE Kalshi account — a Demo key will 401 against Live)."
        )
    using_live_vars = bool(os.getenv("KALSHI_LIVE_API_KEY_ID"))
    logger.info(
        "Using %s credentials (key id ...%s)",
        "KALSHI_LIVE_*" if using_live_vars else "KALSHI_* (fallback)",
        api_key_id[-6:],
    )
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
    # Must cover the *_or_equal variants: the 15-minute up/down series reports "greater_or_equal",
    # so a greater/less-only check silently discovers zero markets for it.
    if strike_type in ABOVE_STRIKE_TYPES:
        strike = raw.get("floor_strike")
    elif strike_type in BELOW_STRIKE_TYPES:
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
    config: CaptureConfig,
    capture_dir: str,
    series: list[str],
    within_hours: float,
    max_markets: int,
    refresh_minutes: float = 10.0,
    heartbeat_minutes: float = 5.0,
    export_dir: str | None = None,
) -> None:
    """Capture continuously, re-discovering markets as the series rolls over.

    Discovering once at startup is fatal for a long run: a 15-minute series lists 96 tickers a day,
    so within hours the subscription is pinned to markets that have closed while every new one goes
    unrecorded. The supervisor re-discovers on an interval and, when the ticker set changes,
    restarts the WebSocket so it resubscribes to the current set.
    """
    order_book = OrderBookStore()
    recorder = CaptureRecorder(capture_dir)
    counts = {"ws": 0, "tick": 0}

    def on_ws_message(message: dict[str, Any]) -> None:
        # Record before parsing: the capture must reflect what actually arrived even if the
        # order book parsing rejects it.
        counts["ws"] += 1
        recorder.record_ws(message)
        order_book.apply(message)

    def on_price_tick(tick: PriceTick) -> None:
        counts["tick"] += 1
        recorder.record_tick(tick)

    recorder.record_session()
    crypto_feed = CryptoIndexFeed(config.crypto_feed_symbols, on_tick=on_price_tick)
    known_tickers: set[str] = set()
    ws_task: asyncio.Task | None = None

    async def refresh_markets() -> None:
        """Re-discover, and resubscribe only when the set actually changed."""
        nonlocal ws_task, known_tickers
        # Discovery is blocking HTTP; keep it off the event loop so recording never stalls for it.
        markets = await asyncio.to_thread(
            discover_near_term_markets, config, series, within_hours, max_markets
        )
        tickers = {market.ticker for market in markets}
        if not tickers:
            logger.warning("Discovery returned no markets; keeping the current subscription.")
            return
        if tickers == known_tickers:
            return

        for market in markets:
            # Strike and close time come from REST, which a replay will not repeat, so they must
            # live in the capture or the recording is not self-contained.
            recorder.record_market(market)

        added = len(tickers - known_tickers)
        removed = len(known_tickers - tickers)
        known_tickers = tickers
        config.market_tickers = sorted(tickers)

        if ws_task is not None:
            ws_task.cancel()
            with suppress(asyncio.CancelledError):
                await ws_task
        client = KalshiWebSocketClient(config, on_message=on_ws_message)  # type: ignore[arg-type]
        ws_task = asyncio.create_task(client.run_forever())
        logger.info(
            "Subscribed to %d market(s) (+%d new, -%d closed)", len(tickers), added, removed
        )

    async def supervisor() -> None:
        while True:
            try:
                await refresh_markets()
                # Yesterday's file is finished; compressing keeps a long run from filling the disk.
                await asyncio.to_thread(compress_completed_days, capture_dir)
                if export_dir:
                    # Completed days only — the current file is mid-append and would sync truncated.
                    await asyncio.to_thread(export_completed_days, capture_dir, export_dir)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed refresh must never kill a running capture — the existing subscription
                # keeps recording and the next cycle tries again.
                logger.exception("Market refresh failed; retrying next cycle")
            await asyncio.sleep(refresh_minutes * 60)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_minutes * 60)
            logger.info(
                "capture alive: %d ws events, %d index ticks, %d markets subscribed, %d buffered",
                counts["ws"], counts["tick"], len(known_tickers), recorder.buffered,
            )

    logger.info(
        "Capturing %s to %s, refreshing markets every %.0f min. "
        "READ-ONLY: no trader loaded, cannot place orders. Ctrl+C to stop.",
        series, capture_dir, refresh_minutes,
    )
    try:
        await asyncio.gather(crypto_feed.run_forever(), recorder.run_forever(), supervisor(), heartbeat())
    finally:
        if ws_task is not None:
            ws_task.cancel()
            with suppress(asyncio.CancelledError):
                await ws_task
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
    parser.add_argument(
        "--export-dir", default=None,
        help="Copy completed (compressed) day files here, e.g. a Dropbox/iCloud folder, so another "
             "machine can pick them up. Only finished days are copied; today's is still being written.",
    )
    parser.add_argument(
        "--refresh-minutes", type=float, default=10.0,
        help="How often to re-discover markets and resubscribe. A 15-minute series rolls over "
             "constantly, so this must be well under the contract duration (default 10).",
    )
    parser.add_argument("--base-url", default=LIVE_BASE_URL, help="Live REST base URL.")
    parser.add_argument("--ws-url", default=LIVE_WS_URL, help="Live WebSocket URL.")
    args = parser.parse_args()

    series = args.series or ["KXBTCD"]
    config = _load_config(args.base_url, args.ws_url)

    # Fail fast on a bad series or bad credentials rather than looping quietly forever.
    logger.info("Discovering %s markets closing within %.1fh on %s ...", series, args.hours, args.base_url)
    markets = discover_near_term_markets(config, series, args.hours, args.max_markets)
    if not markets:
        raise SystemExit(
            f"No {series} markets close within {args.hours}h. Try a larger --hours, or check the series."
        )
    logger.info(
        "Found %d markets; closes span %s .. %s UTC",
        len(markets),
        min(markets, key=lambda m: m.close_time).close_time,
        max(markets, key=lambda m: m.close_time).close_time,
    )

    asyncio.run(
        run_capture(
            config, args.capture_dir, series, args.hours, args.max_markets,
            refresh_minutes=args.refresh_minutes, export_dir=args.export_dir,
        )
    )


if __name__ == "__main__":
    main()
