"""Normalise a raw capture into per-second, per-market rows.

A capture is an append-ordered log of unparsed WebSocket messages — replayable, but useless to
anyone who does not already know Kalshi's wire format. This turns it into the thing the capture
exists to produce: a table where each row is one market at one second, with the book state, the
strike, the time left, and the index.

Three decisions worth stating, because they determine whether the output can be trusted:

* **Arrival time is the clock.** Rows are stamped with when the process observed an event, never
  with a venue timestamp, so a consumer replaying this can never see a price before it was
  knowable. This is the same invariant the capture format is built on.
* **Rows are emitted for every second in a market's window, not only on change.** A gap in an
  event-driven log is ambiguous — nothing happened, or nothing was recorded? Emitting every second
  with a forward-filled book makes the distinction explicit: a row always exists, and `stale_seconds`
  says how long since the book last moved.
* **Only markets near their close are emitted.** A day's capture spans thousands of open strikes
  settling years out. Writing every one every second would produce tens of millions of rows that
  nobody wants; the window before close is where the information is.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterator

from backtest.recorder import captured_markets, read_captures
from ingestion.kalshi_rest import MarketInfo
from ingestion.order_book import OrderBookStore


@dataclass(frozen=True)
class Row:
    """One market at one second. Prices in cents; sizes in contracts."""

    t: int
    ticker: str
    seconds_to_close: int
    yes_bid: int | None
    yes_ask: int | None
    """Implied: 100 - best no bid. Kalshi carries only resting bids, so this is what a buyer pays."""
    no_bid: int | None
    no_ask: int | None
    yes_bid_size: float
    yes_ask_size: float
    mid: float | None
    spread: int | None
    strike: float
    index_price: float | None
    index_distance_pct: float | None
    """Signed distance of the index from the strike, in percent. Positive favours YES."""
    stale_seconds: int
    """Seconds since this market's book last changed. 0 means it moved this second."""


CSV_COLUMNS = [field.name for field in fields(Row)]


def _mid(yes_bid: int | None, yes_ask: int | None) -> float | None:
    if yes_bid is None or yes_ask is None:
        return None
    return (yes_bid + yes_ask) / 2.0


def build_rows(
    directory: str | Path,
    *,
    within_minutes: float = 20.0,
    index_symbol_for: dict[str, str] | None = None,
) -> Iterator[Row]:
    """Replay a capture and yield one row per market per second inside its pre-close window.

    The replay walks the merged log forward in arrival order, applying order book messages and index
    ticks as they land, and emits a snapshot whenever the wall clock crosses a second boundary.
    """
    index_symbol_for = index_symbol_for or {"KXBTC": "BTC-USD", "KXETH": "ETH-USD"}
    markets: dict[str, MarketInfo] = {m.ticker: m for m in captured_markets(directory)}
    if not markets:
        return

    store = OrderBookStore()
    latest_index: dict[str, float] = {}
    last_change: dict[str, int] = {}
    current_second: int | None = None

    def symbol_for(ticker: str) -> str | None:
        for prefix, symbol in index_symbol_for.items():
            if ticker.startswith(prefix):
                return symbol
        return None

    def snapshot(second: int) -> Iterator[Row]:
        for ticker, market in markets.items():
            close_ts = int(market.close_time.timestamp())
            remaining = close_ts - second
            if remaining < 0 or remaining > within_minutes * 60:
                continue
            yes_bid = store.best_bid_cents(ticker, "yes")
            no_bid = store.best_bid_cents(ticker, "no")
            yes_ask = None if no_bid is None else 100 - no_bid
            no_ask = None if yes_bid is None else 100 - yes_bid
            if yes_bid is None and no_bid is None:
                continue  # never quoted; an empty row would imply a book we never saw
            symbol = symbol_for(ticker)
            index_price = latest_index.get(symbol) if symbol else None
            distance = (
                (index_price - market.strike_price) / market.strike_price * 100.0
                if index_price and market.strike_price
                else None
            )
            yield Row(
                t=second,
                ticker=ticker,
                seconds_to_close=remaining,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                yes_bid_size=store.ask_depth(ticker, "no"),
                yes_ask_size=store.ask_depth(ticker, "yes"),
                mid=_mid(yes_bid, yes_ask),
                spread=None if yes_bid is None or yes_ask is None else yes_ask - yes_bid,
                strike=market.strike_price,
                index_price=index_price,
                index_distance_pct=None if distance is None else round(distance, 6),
                stale_seconds=second - last_change.get(ticker, second),
            )

    for event in read_captures(directory, kinds={"ws", "tick"}):
        second = int(event.t)
        if current_second is None:
            current_second = second
        # Emit for every second the clock crossed, so a quiet stretch still produces rows rather
        # than a hole a consumer would have to guess about.
        while current_second < second:
            yield from snapshot(current_second)
            current_second += 1

        if event.kind == "tick":
            latest_index[event.data["symbol"]] = float(event.data["price"])
            continue
        payload = event.data.get("payload", {})
        ticker = payload.get("msg", {}).get("market_ticker")
        store.apply(payload)
        if ticker is not None:
            last_change[ticker] = second

    if current_second is not None:
        yield from snapshot(current_second)


def write_csv(rows: Iterator[Row], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
            written += 1
    return written
