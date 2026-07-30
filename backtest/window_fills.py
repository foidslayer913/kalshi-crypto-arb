"""Fill analysis over a Tier 1 capture: when the signal fires, was there an ask to hit?

The Tier 2 backtest (`reconstruct.py`) answers whether the *signal* is right — how often the bound
declares an outcome and the market settles the other way. It cannot answer the other half of the
question, because it has no order book: **when the signal fires, is there actually an ask resting
below the trigger price, and how much of it?** That needs the resting depth, which only exists in a
capture we recorded ourselves.

This module pairs the two. For each captured market it:

1. rebuilds the settlement window from the captured index ticks and runs the *same*
   `guaranteed_side` the live scanner uses, to get the fire second and the winning side (reusing
   `reconstruct.evaluate_window`);
2. reconstructs that market's order book from its captured snapshot/delta stream, sampled at the
   fire second, and reads the implied ask on the winning side and its depth;
3. scores the fill economics: net yield after Kalshi's fee, and whether it clears a threshold.

The book is reconstructed in venue time (each delta's own `ts`): the question here is what liquidity
was *actually resting* at the fire instant, which is a venue-time fact. A latency-shifted fill model
— what survives the round trip from the bot's arrival-time view — is the next refinement
(`fill_model.py`), and is deliberately not baked in here.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Iterable

from backtest.reconstruct import PriceSeries, SettledMarket, Variant, evaluate_window
from backtest.recorder import captured_markets, read_captures
from ingestion.kalshi_rest import MarketInfo
from ingestion.order_book import OrderBookStore, Side
from strategy.fee_calculator import calculate_net_yield
from strategy.scanner import resolve_crypto_symbol

logger = logging.getLogger(__name__)

DEFAULT_PREFIX_MAP = {"KXBTC": "BTC-USD", "KXETH": "ETH-USD"}


def _delta_venue_ts(payload: dict) -> float | None:
    """Venue timestamp of a delta, preferring the millisecond field. Snapshots have none."""
    body = payload.get("msg", {})
    ts_ms = body.get("ts_ms")
    if ts_ms is not None:
        return float(ts_ms) / 1000.0
    ts = body.get("ts")
    if ts is None:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def reconstruct_bids_at(
    payloads: Iterable[dict], ticker: str, sample_times: list[float]
) -> dict[float, tuple[int | None, int | None]]:
    """Best (yes_bid, no_bid) in cents at each venue time in `sample_times` (ascending).

    Payloads are replayed in capture (arrival) order; the book recorded at each sample time reflects
    only deltas whose venue ts is at or before it, so nothing from after the instant leaks in. A
    snapshot is a full reset, applied where it appears in the stream.
    """
    store = OrderBookStore()
    bids: dict[float, tuple[int | None, int | None]] = {}
    idx = 0

    def _record(t: float) -> None:
        bids[t] = (store.best_bid_cents(ticker, "yes"), store.best_bid_cents(ticker, "no"))

    for payload in payloads:
        if payload.get("type") == "orderbook_delta":
            ts = _delta_venue_ts(payload)
            while idx < len(sample_times) and ts is not None and sample_times[idx] <= ts:
                _record(sample_times[idx])
                idx += 1
        store.apply(payload)
    while idx < len(sample_times):
        _record(sample_times[idx])
        idx += 1
    return bids


def _ask_from_bids(bids: tuple[int | None, int | None] | None, side: Side) -> float | None:
    """Implied ask on `side` (dollars) from (yes_bid, no_bid): the opposing best bid sets it."""
    if bids is None:
        return None
    yes_bid, no_bid = bids
    opposite_bid = no_bid if side == "yes" else yes_bid
    return None if opposite_bid is None else (100 - opposite_bid) / 100


def reconstruct_ask_at(
    payloads: Iterable[dict], ticker: str, side: Side, sample_times: list[float]
) -> dict[float, float | None]:
    """Implied ask on `side` at each venue time in `sample_times` (ascending)."""
    bids = reconstruct_bids_at(payloads, ticker, sample_times)
    return {t: _ask_from_bids(bids.get(t), side) for t in sample_times}


@dataclass(frozen=True)
class FillResult:
    ticker: str
    fire_second: int
    side: Side
    ask_at_fire: float | None
    net_yield_at_fire: float | None
    ask_at_close: float | None
    net_yield_at_close: float | None
    strike_price: float = 0.0
    yes_bid_at_fire: int | None = None
    no_bid_at_fire: int | None = None
    ws_events: int = 0

    @property
    def had_ask(self) -> bool:
        return self.ask_at_fire is not None

    @property
    def book_nonempty_at_fire(self) -> bool:
        """Any resting bid on either side — distinguishes a one-sided book (real) from an empty
        one (points at a reconstruction/timing problem, not a market condition)."""
        return self.yes_bid_at_fire is not None or self.no_bid_at_fire is not None

    def fillable(self, min_yield: float) -> bool:
        return self.net_yield_at_fire is not None and self.net_yield_at_fire >= min_yield


def analyze_fills(
    directory: str | Path,
    variant: Variant,
    *,
    window_size: int = 60,
    prefix_map: dict[str, str] | None = None,
) -> list[FillResult]:
    """Score every captured market that fires under `variant` for whether a fillable ask existed."""
    prefix_map = prefix_map or DEFAULT_PREFIX_MAP
    markets = captured_markets(directory)

    ws_by_ticker: dict[str, list[dict]] = defaultdict(list)
    ticks_by_symbol: dict[str, list[tuple[float, float]]] = defaultdict(list)
    first_t: float | None = None
    last_t: float | None = None
    for event in read_captures(directory, kinds={"ws", "tick"}):
        first_t = event.t if first_t is None else min(first_t, event.t)
        last_t = event.t if last_t is None else max(last_t, event.t)
        if event.kind == "tick":
            ticks_by_symbol[event.data["symbol"]].append((event.data["ts"], event.data["price"]))
        else:
            payload = event.data["payload"]
            ticker = payload.get("msg", {}).get("market_ticker")
            if ticker is not None:
                ws_by_ticker[ticker].append(payload)

    series_by_symbol = {symbol: PriceSeries(points) for symbol, points in ticks_by_symbol.items()}

    results: list[FillResult] = []
    skipped_uncaptured = 0
    for market in markets:
        symbol = resolve_crypto_symbol(market.ticker, prefix_map)
        series = series_by_symbol.get(symbol) if symbol else None
        if series is None:
            continue
        close_ts = market.close_time.timestamp()
        ticks = series.window_ticks(close_ts, window_size)
        settled = SettledMarket(
            ticker=market.ticker, strike_type=market.strike_type, strike_price=market.strike_price,
            close_time=market.close_time, result="yes", crypto_symbol=symbol,  # result unused here
        )
        signal = evaluate_window(settled, ticks, variant, window_size)
        if not signal.fired or signal.predicted is None or signal.fire_second is None:
            continue

        side: Side = signal.predicted
        fire_ts = close_ts - (window_size - signal.fire_second)
        # Only score markets whose fire instant was actually recorded; one whose window fell outside
        # the captured span would otherwise masquerade as "fired into an empty book".
        if first_t is not None and last_t is not None and not (first_t <= fire_ts <= last_t):
            skipped_uncaptured += 1
            continue
        close_minus_1 = close_ts - 1
        sample_times = sorted({fire_ts, close_minus_1})
        payloads = ws_by_ticker.get(market.ticker, [])
        bids = reconstruct_bids_at(payloads, market.ticker, sample_times)

        yes_bid, no_bid = bids.get(fire_ts, (None, None))
        ask_fire = _ask_from_bids(bids.get(fire_ts), side)
        ask_close = _ask_from_bids(bids.get(close_minus_1), side)
        results.append(
            FillResult(
                ticker=market.ticker,
                fire_second=signal.fire_second,
                side=side,
                ask_at_fire=ask_fire,
                net_yield_at_fire=calculate_net_yield(ask_fire) if ask_fire is not None else None,
                ask_at_close=ask_close,
                net_yield_at_close=calculate_net_yield(ask_close) if ask_close is not None else None,
                strike_price=market.strike_price,
                yes_bid_at_fire=yes_bid,
                no_bid_at_fire=no_bid,
                ws_events=len(payloads),
            )
        )
    if skipped_uncaptured:
        logger.info(
            "Skipped %d market(s) whose settlement window fell outside the captured time span "
            "(their books were never recorded, so they can't be scored for fills).",
            skipped_uncaptured,
        )
    return results


def format_fill_debug(results: list[FillResult], limit: int = 20) -> str:
    """Per-market view to tell a one-sided book (real: winning side empty, other side full) from an
    empty one (a reconstruction/timing problem)."""
    nonempty = sum(1 for r in results if r.book_nonempty_at_fire)
    # Show the most active markets first: those are the near-the-money strikes where two-sided
    # liquidity — and the real fill question — actually live. Deep in/out-of-money strikes are
    # inactive and trivially have no counterparty on the winning side.
    ordered = sorted(results, key=lambda r: (r.book_nonempty_at_fire, r.ws_events), reverse=True)
    header = (
        f"{'ticker':<28}{'strike':>10}{'side':>5}{'fire_s':>7}"
        f"{'yes_bid':>8}{'no_bid':>8}{'ws':>7}{'ask':>7}"
    )
    lines = [
        f"fired markets with a non-empty book at fire: {nonempty}/{len(results)}",
        "(if this is ~0, signals are firing before the book was captured — a timing issue, not the market)",
        "most-active markets first (near the money is where liquidity is):",
        "",
        header,
        "-" * len(header),
    ]
    for r in ordered[:limit]:
        ask = "-" if r.ask_at_fire is None else f"{r.ask_at_fire:.2f}"
        lines.append(
            f"{r.ticker:<28}{r.strike_price:>10.0f}{r.side:>5}{r.fire_second:>7}"
            f"{'-' if r.yes_bid_at_fire is None else r.yes_bid_at_fire:>8}"
            f"{'-' if r.no_bid_at_fire is None else r.no_bid_at_fire:>8}"
            f"{r.ws_events:>7}{ask:>7}"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class FillSummary:
    variant: str
    fired: int
    had_ask: int
    fillable: int
    fillable_rate: float
    median_ask_at_fire: float | None
    median_net_yield_fillable: float | None
    max_net_yield: float | None


def summarize_fills(results: list[FillResult], variant_name: str, min_yield: float) -> FillSummary:
    had_ask = [r for r in results if r.had_ask]
    fillable = [r for r in had_ask if r.fillable(min_yield)]
    asks = [r.ask_at_fire for r in had_ask if r.ask_at_fire is not None]
    yields = [r.net_yield_at_fire for r in fillable if r.net_yield_at_fire is not None]
    all_yields = [r.net_yield_at_fire for r in had_ask if r.net_yield_at_fire is not None]
    return FillSummary(
        variant=variant_name,
        fired=len(results),
        had_ask=len(had_ask),
        fillable=len(fillable),
        fillable_rate=len(fillable) / len(results) if results else 0.0,
        median_ask_at_fire=median(asks) if asks else None,
        median_net_yield_fillable=median(yields) if yields else None,
        max_net_yield=max(all_yields) if all_yields else None,
    )


def format_fill_summary(summary: FillSummary, min_yield: float) -> str:
    def money(value: float | None) -> str:
        return "-" if value is None else f"{value:.3f}"

    lines = [
        f"variant                    {summary.variant}",
        f"markets fired              {summary.fired}",
        f"  had an ask on the side   {summary.had_ask}",
        f"  fillable (yield >= {min_yield:.2f})   {summary.fillable}  ({summary.fillable_rate * 100:.1f}% of fired)",
        f"median ask at fire         {money(summary.median_ask_at_fire)}",
        f"median net yield (fillable){money(summary.median_net_yield_fillable)}",
        f"best net yield seen        {money(summary.max_net_yield)}",
    ]
    if summary.had_ask == 0:
        lines.append("")
        lines.append("No asks on the winning side at the fire second — signals fired into an empty book.")
    return "\n".join(lines)
