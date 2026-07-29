from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx

from config import Settings
from ingestion.kalshi_auth import auth_headers, load_private_key

logger = logging.getLogger(__name__)

ORDER_PATH = "/trade-api/v2/portfolio/orders"


class KillSwitchTripped(RuntimeError):
    """Raised when an order is attempted while the daily loss kill-switch is active."""


@dataclass
class KillSwitch:
    """Global guardrail: halts all new orders once simulated losses for the day exceed the limit."""

    max_daily_loss: float
    _realized_loss: float = 0.0

    @property
    def realized_loss(self) -> float:
        return self._realized_loss

    @property
    def tripped(self) -> bool:
        return self._realized_loss >= self.max_daily_loss

    def record_pnl(self, pnl: float) -> None:
        if pnl < 0:
            self._realized_loss += -pnl

    def reset(self) -> None:
        self._realized_loss = 0.0

    def check(self) -> None:
        if self.tripped:
            raise KillSwitchTripped(
                f"Daily simulated loss ${self._realized_loss:.2f} has reached the "
                f"${self.max_daily_loss:.2f} limit; no further orders will be placed today."
            )


@dataclass(frozen=True)
class BookQuote:
    """Minimal order book snapshot used for phantom-fill detection in dry-run mode."""

    ticker: str
    side: Literal["yes", "no"]
    ask_price: float
    ask_depth: int


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    ticker: str
    side: Literal["yes", "no"]
    count: int
    price: float
    dry_run: bool
    phantom_fill: bool = False


class DemoTrader:
    """Places (or simulates) orders against Kalshi's Demo REST API only.

    In dry-run mode no network request is made: `simulate_fill` compares the order book quote a
    trading decision was based on against a fresh quote fetched at "order time" to detect phantom
    fills — book movement between signal and order arrival that means the order would not
    actually have filled at the expected price. In live mode (still demo-only) it signs and POSTs
    the order to the Kalshi Demo API.
    """

    def __init__(self, settings: Settings, *, kill_switch: KillSwitch, dry_run: bool = True) -> None:
        if "demo" not in settings.kalshi_base_url:
            raise RuntimeError("Refusing to construct a DemoTrader against a non-demo Kalshi API")
        self._settings = settings
        self._kill_switch = kill_switch
        self._dry_run = dry_run
        self._private_key = load_private_key(settings.private_key_pem)

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def _headers(self, method: str, path: str) -> dict[str, str]:
        return auth_headers(self._private_key, self._settings.kalshi_api_key_id, method, path)

    def simulate_fill(self, decision_quote: BookQuote, current_quote: BookQuote) -> bool:
        """True if the order would suffer a phantom fill: the ask the decision was based on is no
        longer available (price moved up or depth dried up) by the time the order would arrive.
        """
        if decision_quote.ticker != current_quote.ticker or decision_quote.side != current_quote.side:
            raise ValueError("decision_quote and current_quote must be for the same ticker/side")
        return current_quote.ask_price > decision_quote.ask_price or current_quote.ask_depth < 1

    async def place_order(
        self,
        *,
        ticker: str,
        side: Literal["yes", "no"],
        count: int,
        price: float,
        decision_quote: BookQuote | None = None,
        current_quote: BookQuote | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> OrderResult:
        """Place a limit buy order for `count` contracts of `ticker`/`side` at `price` (dollars)."""
        self._kill_switch.check()
        order_id = str(uuid.uuid4())

        if self._dry_run:
            phantom_fill = False
            if decision_quote is not None and current_quote is not None:
                phantom_fill = self.simulate_fill(decision_quote, current_quote)
            logger.info(
                "[DRY RUN] order %s: %s %s x%d @ %.2f (phantom_fill=%s)",
                order_id, ticker, side, count, price, phantom_fill,
            )
            return OrderResult(order_id, ticker, side, count, price, dry_run=True, phantom_fill=phantom_fill)

        body = {
            "ticker": ticker,
            "client_order_id": order_id,
            "side": side,
            "action": "buy",
            "count": count,
            "type": "limit",
            f"{side}_price": round(price * 100),
        }
        owns_client = client is None
        client = client or httpx.AsyncClient(base_url=self._settings.kalshi_base_url, timeout=10.0)
        try:
            headers = self._headers("POST", ORDER_PATH)
            response = await client.post(ORDER_PATH, json=body, headers=headers)
            response.raise_for_status()
            logger.info("placed order %s: %s", order_id, response.json())
            return OrderResult(order_id, ticker, side, count, price, dry_run=False)
        finally:
            if owns_client:
                await client.aclose()
