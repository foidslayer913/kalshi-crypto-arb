from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["yes", "no"]


def _to_cents(price: str | float) -> int:
    """Kalshi sends prices as dollar strings ("0.2100"); the book is keyed in integer cents.

    Contract prices are whole cents 1–99, so this is lossless; round() guards against float noise
    like 0.21 * 100 == 21.000000000000004.
    """
    return round(float(price) * 100)


@dataclass
class _MarketBook:
    yes_bids: dict[int, float] = field(default_factory=dict)
    no_bids: dict[int, float] = field(default_factory=dict)


class OrderBookStore:
    """Maintains Kalshi's resting-bid order books per market from orderbook_snapshot/delta
    WebSocket messages, and derives the implied ask price on each side.

    Kalshi markets carry only resting bids on both the "yes" and "no" side — there is no separate
    ask book. Buying yes at price P is economically identical to someone else selling yes (i.e.
    bidding no) at 100 - P, so the best available yes ask is `100 - best_no_bid`, and vice versa.

    Wire format (verified against the live feed): a snapshot carries `yes_dollars_fp` / `no_dollars_fp`
    as `[[price_dollars, size_fp], ...]` string pairs; a delta carries `price_dollars`, `delta_fp`,
    and `side`. Prices are dollar strings, sizes are fixed-point strings — both are normalised here
    (price -> int cents, size -> float) so the rest of the store speaks cents and counts.
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
            # A snapshot is a full replacement; a side absent from the payload means no resting bids.
            book.yes_bids = {_to_cents(price): float(qty) for price, qty in body.get("yes_dollars_fp", [])}
            book.no_bids = {_to_cents(price): float(qty) for price, qty in body.get("no_dollars_fp", [])}
        elif message.get("type") == "orderbook_delta":
            side: Side = body["side"]
            price = _to_cents(body["price_dollars"])
            levels = book.yes_bids if side == "yes" else book.no_bids
            new_qty = levels.get(price, 0.0) + float(body["delta_fp"])
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

    def ask_depth(self, ticker: str, side: Side) -> float:
        """Quantity available at the implied ask (the size resting on the opposing best bid)."""
        opposite: Side = "no" if side == "yes" else "yes"
        book = self._books.get(ticker)
        if book is None:
            return 0.0
        opposite_bid = self.best_bid_cents(ticker, opposite)
        if opposite_bid is None:
            return 0.0
        levels = book.yes_bids if opposite == "yes" else book.no_bids
        return levels.get(opposite_bid, 0.0)
