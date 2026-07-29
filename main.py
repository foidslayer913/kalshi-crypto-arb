from __future__ import annotations

import asyncio
import logging

from config import Settings, load_settings
from execution.demo_trader import DemoTrader, KillSwitch
from ingestion.crypto_feed import CryptoIndexFeed
from ingestion.kalshi_rest import KalshiRestClient
from ingestion.kalshi_ws import KalshiWebSocketClient
from ingestion.order_book import OrderBookStore
from strategy.scanner import SettlementArbScanner, resolve_crypto_symbol
from telemetry.logger import TelemetryLogger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def _build_scanners(
    settings: Settings,
    rest_client: KalshiRestClient,
    crypto_feed: CryptoIndexFeed,
    order_book: OrderBookStore,
    trader: DemoTrader,
    telemetry: TelemetryLogger,
) -> list[SettlementArbScanner]:
    scanners = []
    for ticker in settings.market_tickers:
        crypto_symbol = resolve_crypto_symbol(ticker, settings.market_crypto_symbols)
        if crypto_symbol is None:
            logger.warning("No crypto symbol mapping for market %s; skipping", ticker)
            continue
        market = await rest_client.get_market(ticker)
        scanners.append(
            SettlementArbScanner(
                market, crypto_symbol,
                crypto_feed=crypto_feed, order_book=order_book, trader=trader, telemetry=telemetry,
            )
        )
    return scanners


async def main() -> None:
    settings = load_settings()
    order_book = OrderBookStore()
    ws_client = KalshiWebSocketClient(settings, on_message=order_book.apply)
    crypto_feed = CryptoIndexFeed(settings.crypto_feed_symbols)
    rest_client = KalshiRestClient(settings)
    kill_switch = KillSwitch(max_daily_loss=settings.max_daily_loss)
    trader = DemoTrader(settings, kill_switch=kill_switch, dry_run=settings.dry_run)
    telemetry = TelemetryLogger()

    scanners = await _build_scanners(settings, rest_client, crypto_feed, order_book, trader, telemetry)

    await asyncio.gather(
        ws_client.run_forever(),
        crypto_feed.run_forever(),
        *(scanner.run() for scanner in scanners),
    )


if __name__ == "__main__":
    asyncio.run(main())
