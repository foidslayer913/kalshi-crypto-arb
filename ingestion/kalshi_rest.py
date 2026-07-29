from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx

from config import Settings
from ingestion.kalshi_auth import auth_headers, load_private_key

MARKET_PATH_TEMPLATE = "/trade-api/v2/markets/{ticker}"

StrikeType = Literal["greater", "less"]


@dataclass(frozen=True)
class MarketInfo:
    ticker: str
    strike_type: StrikeType
    strike_price: float
    close_time: datetime


def _parse_market(market: dict) -> MarketInfo:
    strike_type = market["strike_type"]
    if strike_type == "greater":
        strike_price = market["floor_strike"]
    elif strike_type == "less":
        strike_price = market["cap_strike"]
    else:
        raise ValueError(
            f"Unsupported strike_type for settlement arbitrage: {strike_type!r} "
            f"(only single-sided 'greater'/'less' threshold markets are supported)"
        )
    close_time = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
    return MarketInfo(
        ticker=market["ticker"],
        strike_type=strike_type,
        strike_price=float(strike_price),
        close_time=close_time.astimezone(timezone.utc),
    )


class KalshiRestClient:
    """Minimal REST client for fetching the Kalshi market metadata (strike price, close time)
    needed to evaluate the settlement invariant for a given market ticker.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._private_key = load_private_key(settings.private_key_pem)

    async def get_market(self, ticker: str, *, client: httpx.AsyncClient | None = None) -> MarketInfo:
        path = MARKET_PATH_TEMPLATE.format(ticker=ticker)
        headers = auth_headers(self._private_key, self._settings.kalshi_api_key_id, "GET", path)
        owns_client = client is None
        client = client or httpx.AsyncClient(base_url=self._settings.kalshi_base_url, timeout=10.0)
        try:
            response = await client.get(path, headers=headers)
            response.raise_for_status()
            market = response.json()["market"]
        finally:
            if owns_client:
                await client.aclose()
        return _parse_market(market)
