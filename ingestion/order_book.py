from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["yes", "no"]


@dataclass
class _MarketBook:
    yes_bids: dict[int, int] = field(default_factory=dict)
    no_bids: dict[int, int] = field(default_factory=dict)


class OrderBookStore:
    """Maintains Kalshi's resting-bid order books per market from orderbook_snapshot/delta
    WebSocket messages, and derives the implied ask price on each side.

    Kalshi markets carry only resting bids on both the "yes" and "no" side — there is no separate
    ask book. Buying yes at price P is economically identical to someone else selling yes (i.e.
    bidding no) at 100 - P, so the best available yes ask is `100 - best_no_bid`, and vice versa.
    """

    def __init__(self) -> None:
        self._books: dict[str, _MarketBook] = {}

    def apply(self, message: dict) -> None:
        body = message.get("msg", {})
        ticker = body.get("market_ticker")
        if ticker is None:
            return
        book = self._books.setdefault(ticker, _MarketBook())

        if message.get("type") == "orderbook_snapshot":
            book.yes_bids = {price: qty for price, qty in body.get("yes", [])}
            book.no_bids = {price: qty for price, qty in body.get("no", [])}
        elif message.get("type") == "orderbook_delta":
            side: Side = body["side"]
            price = body["price"]
            levels = book.yes_bids if side == "yes" else book.no_bids
            new_qty = levels.get(price, 0) + body["delta"]
            if new_qty <= 0:
                levels.pop(price, None)
            else:
                levels[price] = new_qty

    def best_bid_cents(self, ticker: str, side: Side) -> int | None:
        book = self._books.get(ticker)
        if book is None:
            return None
        levels = book.yes_bids if side == "yes" else book.no_bids
        return max(levels) if levels else None

    def implied_ask_dollars(self, ticker: str, side: Side) -> float | None:
        """Cheapest price to immediately buy `side`, derived from the opposing side's best bid."""
        opposite: Side = "no" if side == "yes" else "yes"
        opposite_bid = self.best_bid_cents(ticker, opposite)
        if opposite_bid is None:
            return None
        return (100 - opposite_bid) / 100

    def ask_depth(self, ticker: str, side: Side) -> int:
        """Quantity available at the implied ask (the size resting on the opposing best bid)."""
        opposite: Side = "no" if side == "yes" else "yes"
        book = self._books.get(ticker)
        if book is None:
            return 0
        opposite_bid = self.best_bid_cents(ticker, opposite)
        if opposite_bid is None:
            return 0
        levels = book.yes_bids if opposite == "yes" else book.no_bids
        return levels.get(opposite_bid, 0)
