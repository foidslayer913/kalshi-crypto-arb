from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import websockets

from config import Settings
from ingestion.kalshi_auth import auth_headers, load_private_key

logger = logging.getLogger(__name__)

# Kalshi signs the WebSocket handshake the same way as a REST GET to this path.
WS_AUTH_PATH = "/trade-api/ws/v2"


class KalshiWebSocketClient:
    """Streams L2 order book updates for the configured markets over Kalshi's WebSocket API."""

    def __init__(self, settings: Settings, on_message: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._settings = settings
        self._on_message = on_message
        self._private_key = load_private_key(settings.private_key_pem)
        self._command_id = 0

    def _next_id(self) -> int:
        self._command_id += 1
        return self._command_id

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Connect, subscribe to order book updates, and yield decoded messages until cancelled."""
        headers = auth_headers(self._private_key, self._settings.kalshi_api_key_id, "GET", WS_AUTH_PATH)
        async with websockets.connect(self._settings.kalshi_ws_url, additional_headers=headers) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": self._next_id(),
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta"],
                            "market_tickers": self._settings.market_tickers,
                        },
                    }
                )
            )
            async for raw in ws:
                message = json.loads(raw)
                if self._on_message is not None:
                    self._on_message(message)
                yield message

    async def run_forever(self, *, reconnect_delay: float = 2.0) -> None:
        """Run the stream indefinitely, reconnecting with a fixed delay if the connection drops."""
        while True:
            try:
                async for _ in self.stream():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Kalshi WebSocket connection dropped; reconnecting in %.1fs", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
