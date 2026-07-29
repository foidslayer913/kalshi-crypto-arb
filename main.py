from __future__ import annotations

import asyncio
import logging
from typing import Any

from config import load_settings
from ingestion.crypto_feed import CryptoIndexFeed, PriceTick
from ingestion.kalshi_ws import KalshiWebSocketClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _on_orderbook_message(message: dict[str, Any]) -> None:
    logger.info("orderbook message: %s", message)


def _on_price_tick(tick: PriceTick) -> None:
    logger.info("price tick: %s=%s @ %.0f", tick.symbol, tick.price, tick.timestamp)


async def main() -> None:
    settings = load_settings()
    ws_client = KalshiWebSocketClient(settings, on_message=_on_orderbook_message)
    crypto_feed = CryptoIndexFeed(settings.crypto_feed_symbols, on_tick=_on_price_tick)

    await asyncio.gather(
        ws_client.run_forever(),
        crypto_feed.run_forever(),
    )


if __name__ == "__main__":
    asyncio.run(main())
