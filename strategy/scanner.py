from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from execution.demo_trader import BookQuote, DemoTrader
from ingestion.crypto_feed import CryptoIndexFeed
from ingestion.kalshi_rest import MarketInfo
from ingestion.order_book import OrderBookStore
from strategy.fee_calculator import meets_yield_threshold
from strategy.math_engine import SettlementWindow
from telemetry.logger import ExecutionRecord, TelemetryLogger

logger = logging.getLogger(__name__)

# How stale a crypto tick can be and still count as the current second's price.
MAX_TICK_STALENESS_SECONDS = 2.0


def resolve_crypto_symbol(ticker: str, mapping: dict[str, str]) -> str | None:
    """Find the crypto feed symbol for a market ticker by matching a configured prefix."""
    for prefix, symbol in mapping.items():
        if ticker.startswith(prefix):
            return symbol
    return None


@dataclass
class ScannerConfig:
    contracts_per_trade: int = 1
    min_yield_threshold: float = 0.01


class SettlementArbScanner:
    """Watches a single market's final 60-second settlement window and fires a demo order the
    instant the guaranteed-floor/ceiling invariant makes settlement mathematically certain and
    the order book offers a net-positive yield after fees. Fires at most one trade per market.
    """

    def __init__(
        self,
        market: MarketInfo,
        crypto_symbol: str,
        *,
        crypto_feed: CryptoIndexFeed,
        order_book: OrderBookStore,
        trader: DemoTrader,
        telemetry: TelemetryLogger,
        config: ScannerConfig | None = None,
    ) -> None:
        self._market = market
        self._crypto_symbol = crypto_symbol
        self._crypto_feed = crypto_feed
        self._order_book = order_book
        self._trader = trader
        self._telemetry = telemetry
        self._config = config or ScannerConfig()
        self._window = SettlementWindow()
        self._executed = False

    def _latest_price(self, as_of: float) -> float | None:
        buffer = self._crypto_feed.buffer(self._crypto_symbol)
        if not buffer:
            return None
        tick = buffer[-1]
        if as_of - tick.timestamp > MAX_TICK_STALENESS_SECONDS:
            return None
        return tick.price

    def _is_guaranteed(self) -> bool:
        if self._market.strike_type == "greater":
            return self._window.is_guaranteed_above(self._market.strike_price)
        return self._window.is_guaranteed_below(self._market.strike_price)

    async def _maybe_trade(self) -> None:
        if self._executed or not self._is_guaranteed():
            return
        side = "yes" if self._market.strike_type == "greater" else "no"
        signal_time = time.time()
        ask = self._order_book.implied_ask_dollars(self._market.ticker, side)
        if ask is None:
            return
        if not meets_yield_threshold(ask, self._config.min_yield_threshold, self._config.contracts_per_trade):
            return

        decision_quote = BookQuote(
            ticker=self._market.ticker, side=side, ask_price=ask,
            ask_depth=self._order_book.ask_depth(self._market.ticker, side),
        )
        # Yield to the event loop to give any in-flight order book updates a chance to land,
        # mirroring the tick-to-order latency the real phantom-fill check needs to catch.
        await asyncio.sleep(0)
        current_quote = BookQuote(
            ticker=self._market.ticker, side=side,
            ask_price=self._order_book.implied_ask_dollars(self._market.ticker, side) or ask,
            ask_depth=self._order_book.ask_depth(self._market.ticker, side),
        )

        self._executed = True  # claim the opportunity before awaiting to avoid double-firing
        result = await self._trader.place_order(
            ticker=self._market.ticker, side=side, count=self._config.contracts_per_trade,
            price=ask, decision_quote=decision_quote, current_quote=current_quote,
        )
        order_time = time.time()
        simulated_pnl = 0.0 if result.phantom_fill else (1.0 - result.price) * result.count
        self._telemetry.record(
            ExecutionRecord(
                timestamp=order_time, ticker=self._market.ticker, side=side, count=result.count,
                price=result.price, signal_time=signal_time, order_time=order_time,
                phantom_fill=result.phantom_fill, simulated_pnl=simulated_pnl,
            )
        )
        logger.info("Executed settlement arbitrage on %s: %s", self._market.ticker, result)

    async def run(self) -> None:
        """Sleep until the final 60-second settlement window opens, then sample one tick per
        second and evaluate the invariant until either a trade fires or the window fills.
        """
        window_start = self._market.close_time.timestamp() - self._window.window_size
        sleep_seconds = window_start - time.time()
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)

        for _ in range(self._window.window_size):
            if self._executed:
                return
            tick_time = time.time()
            self._window.record_tick(self._latest_price(tick_time))
            await self._maybe_trade()
            await asyncio.sleep(max(0.0, tick_time + 1.0 - time.time()))
