from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{symbol}/spot"


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    price: float
    timestamp: float


class CryptoIndexFeed:
    """Polls a crypto price source once per second and keeps a rolling 60-tick buffer per symbol.

    Each buffer is a proxy for one second of the CFB RTI settlement averaging window and is the
    input the strategy math engine (strategy/math_engine.py) will consume.
    """

    def __init__(
        self,
        symbols: list[str],
        *,
        poll_interval: float = 1.0,
        buffer_size: int = 60,
        on_tick: Callable[[PriceTick], None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._symbols = symbols
        self._poll_interval = poll_interval
        self._on_tick = on_tick
        self._client = client
        self._buffers: dict[str, deque[PriceTick]] = {
            symbol: deque(maxlen=buffer_size) for symbol in symbols
        }

    def buffer(self, symbol: str) -> deque[PriceTick]:
        return self._buffers[symbol]

    async def _fetch_price(self, client: httpx.AsyncClient, symbol: str) -> PriceTick:
        response = await client.get(COINBASE_SPOT_URL.format(symbol=symbol))
        response.raise_for_status()
        amount = float(response.json()["data"]["amount"])
        return PriceTick(symbol=symbol, price=amount, timestamp=time.time())

    async def run_forever(self) -> None:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=5.0)
        try:
            while True:
                start = time.monotonic()
                results = await asyncio.gather(
                    *(self._fetch_price(client, symbol) for symbol in self._symbols),
                    return_exceptions=True,
                )
                for symbol, result in zip(self._symbols, results):
                    if isinstance(result, Exception):
                        logger.warning("Failed to fetch price for %s: %s", symbol, result)
                        continue
                    self._buffers[symbol].append(result)
                    if self._on_tick is not None:
                        self._on_tick(result)
                elapsed = time.monotonic() - start
                await asyncio.sleep(max(0.0, self._poll_interval - elapsed))
        finally:
            if owns_client:
                await client.aclose()
