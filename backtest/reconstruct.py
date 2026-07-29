"""Tier 2 backtest: signal-correctness without order book data.

Replays each expired market's final 60 seconds from a historical 1-second index series, evaluates
the settlement bounds exactly as `SettlementArbScanner` would, and scores the resulting signal
against how the market actually settled.

The question this answers is not "how much would we have made" — that needs resting depth, which
is not in the historical record (see Tier 1). It answers the question that decides whether the
strategy is safe to run at all: **how often does the bound say the outcome is decided, and the
market then settle the other way?** With the true settlement index that rate is zero by
construction. With a proxy feed standing in for CF Benchmarks RTI it is not, and that gap is the
strategy's real risk.
"""

from __future__ import annotations

import bisect
import csv
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ingestion.kalshi_rest import MarketInfo, StrikeType
from strategy.math_engine import SettlementWindow, relative_cap, relative_floor
from strategy.scanner import MAX_TICK_STALENESS_SECONDS, guaranteed_side

Outcome = Literal["yes", "no"]


@dataclass(frozen=True)
class SettledMarket:
    """An expired market plus how it actually resolved — the ground truth label."""

    ticker: str
    strike_type: StrikeType
    strike_price: float
    close_time: datetime
    result: Outcome
    crypto_symbol: str
    settlement_value: float | None = None
    volume: float | None = None
    """Contracts traded over the market's life. Zero means nothing was executable in it at any
    price, so a signal there is a paper opportunity only."""

    def as_market_info(self) -> MarketInfo:
        return MarketInfo(
            ticker=self.ticker,
            strike_type=self.strike_type,
            strike_price=self.strike_price,
            close_time=self.close_time,
        )


@dataclass(frozen=True)
class Variant:
    """A bound configuration to score. `delta=None` is SPEC.md's strict floor of 0."""

    name: str
    delta: float | None = None

    def build_window(self, window_size: int = 60) -> SettlementWindow:
        if self.delta is None:
            return SettlementWindow(window_size=window_size)
        return SettlementWindow(
            window_size=window_size,
            floor_policy=relative_floor(self.delta),
            cap_policy=relative_cap(self.delta),
        )


STRICT = Variant("strict")
DEFAULT_VARIANTS: tuple[Variant, ...] = (
    STRICT,
    Variant("relaxed-0.5%", 0.005),
    Variant("relaxed-1%", 0.01),
    Variant("relaxed-2%", 0.02),
)


@dataclass(frozen=True)
class WindowResult:
    ticker: str
    variant: str
    fired: bool
    fire_second: int | None
    predicted: Outcome | None
    actual: Outcome
    ticks_known: int
    reconstructed_average: float | None
    settlement_value: float | None
    window_size: int = 60
    volume: float | None = None

    @property
    def traded(self) -> bool | None:
        """Whether this market ever traded. None when the source data did not say."""
        if self.volume is None:
            return None
        return self.volume > 0

    @property
    def actionable(self) -> bool:
        """Whether the bound closed with time left to actually place an order.

        A bound that only closes at the last tick is trivially true — the whole window is known,
        so it says nothing more than the settled result does — and there is no time left to trade
        on it. Those are counted separately from tradeable signals.
        """
        return self.fired and self.fire_second is not None and self.fire_second < self.window_size

    @property
    def correct(self) -> bool | None:
        if self.predicted is None:
            return None
        return self.predicted == self.actual

    @property
    def false_positive(self) -> bool:
        """The bound declared the outcome decided and the market settled the other way."""
        return self.predicted is not None and self.predicted != self.actual

    @property
    def proxy_divergence(self) -> float | None:
        """How far the reconstructed settlement average sits from the real settlement value."""
        if self.reconstructed_average is None or self.settlement_value is None:
            return None
        return abs(self.reconstructed_average - self.settlement_value)


class PriceSeries:
    """A historical index series, queried as the 60 one-second ticks before a settlement time.

    Points are (unix_timestamp, price). The series is expected to be at least 1 Hz; each window
    slot takes the most recent point at or before that second's boundary, and slots with nothing
    fresher than `max_staleness` are left as None so the bounds treat them as unknown.
    """

    def __init__(self, points: Iterable[tuple[float, float]]) -> None:
        ordered = sorted(points, key=lambda point: point[0])
        self._timestamps = [timestamp for timestamp, _ in ordered]
        self._prices = [price for _, price in ordered]

    def __len__(self) -> int:
        return len(self._timestamps)

    def price_at(self, timestamp: float, max_staleness: float = MAX_TICK_STALENESS_SECONDS) -> float | None:
        index = bisect.bisect_right(self._timestamps, timestamp) - 1
        if index < 0:
            return None
        if timestamp - self._timestamps[index] > max_staleness:
            return None
        return self._prices[index]

    def window_ticks(
        self,
        close_timestamp: float,
        window_size: int = 60,
        max_staleness: float = MAX_TICK_STALENESS_SECONDS,
    ) -> list[float | None]:
        """The `window_size` ticks ending at `close_timestamp`, one per second."""
        start = close_timestamp - window_size
        return [self.price_at(start + offset + 1, max_staleness) for offset in range(window_size)]


def evaluate_window(
    market: SettledMarket,
    ticks: Sequence[float | None],
    variant: Variant,
    window_size: int = 60,
) -> WindowResult:
    """Feed one market's reconstructed window through the bounds, tick by tick, and record the
    first second at which the outcome was declared decided.
    """
    window = variant.build_window(window_size)
    market_info = market.as_market_info()
    fire_second: int | None = None
    predicted: Outcome | None = None

    for second, tick in enumerate(ticks[:window_size], start=1):
        window.record_tick(tick)
        if predicted is not None:
            continue
        side = guaranteed_side(window, market_info)
        if side is not None:
            fire_second = second
            predicted = side

    known = window.known_ticks
    reconstructed = sum(known) / len(known) if known else None
    return WindowResult(
        ticker=market.ticker,
        variant=variant.name,
        fired=predicted is not None,
        fire_second=fire_second,
        predicted=predicted,
        actual=market.result,
        ticks_known=len(known),
        reconstructed_average=reconstructed,
        settlement_value=market.settlement_value,
        window_size=window_size,
        volume=market.volume,
    )


def reconstruct(
    markets: Iterable[SettledMarket],
    series_by_symbol: dict[str, PriceSeries],
    variants: Sequence[Variant] = DEFAULT_VARIANTS,
    window_size: int = 60,
) -> list[WindowResult]:
    """Score every (market, variant) pair. Markets with no matching price series are skipped."""
    results: list[WindowResult] = []
    for market in markets:
        series = series_by_symbol.get(market.crypto_symbol)
        if series is None:
            continue
        ticks = series.window_ticks(market.close_time.timestamp(), window_size)
        for variant in variants:
            results.append(evaluate_window(market, ticks, variant, window_size))
    return results


@dataclass(frozen=True)
class VariantSummary:
    variant: str
    markets: int
    fired: int
    actionable: int
    actionable_rate: float
    actionable_traded: int
    false_positives: int
    false_positive_rate: float
    fire_second_histogram: dict[int, int]
    median_fire_second: int | None
    mean_proxy_divergence: float | None
    max_proxy_divergence: float | None


def summarize(results: Iterable[WindowResult]) -> list[VariantSummary]:
    """Aggregate per variant.

    Two rates matter and they answer different questions. `actionable_rate` is how often a variant
    produces a signal with time left to trade — the opportunity count. `false_positive_rate` is,
    among actionable signals, how often acting would have lost the whole position; it is the
    safety number, and for a variant to be tradeable it has to be very close to zero.
    """
    by_variant: dict[str, list[WindowResult]] = {}
    for result in results:
        by_variant.setdefault(result.variant, []).append(result)

    summaries: list[VariantSummary] = []
    for variant, group in by_variant.items():
        fired = [result for result in group if result.fired]
        actionable = [result for result in group if result.actionable]
        false_positives = [result for result in actionable if result.false_positive]
        histogram: dict[int, int] = {}
        for result in actionable:
            if result.fire_second is not None:
                histogram[result.fire_second] = histogram.get(result.fire_second, 0) + 1
        fire_seconds = sorted(r.fire_second for r in actionable if r.fire_second is not None)
        divergences = [d for d in (r.proxy_divergence for r in group) if d is not None]
        summaries.append(
            VariantSummary(
                variant=variant,
                markets=len(group),
                fired=len(fired),
                actionable=len(actionable),
                actionable_rate=len(actionable) / len(group) if group else 0.0,
                actionable_traded=sum(1 for result in actionable if result.traded),
                false_positives=len(false_positives),
                false_positive_rate=len(false_positives) / len(actionable) if actionable else 0.0,
                fire_second_histogram=dict(sorted(histogram.items())),
                median_fire_second=fire_seconds[len(fire_seconds) // 2] if fire_seconds else None,
                mean_proxy_divergence=sum(divergences) / len(divergences) if divergences else None,
                max_proxy_divergence=max(divergences) if divergences else None,
            )
        )
    return summaries


def format_summaries(summaries: Sequence[VariantSummary]) -> str:
    """Render summaries as a plain-text table for terminal output."""
    header = (
        f"{'variant':<16}{'markets':>9}{'actionable':>12}{'act%':>8}{'traded':>8}"
        f"{'FP':>5}{'FP%':>8}{'med_s':>7}{'div(mean)':>12}"
    )
    lines = [header, "-" * len(header)]
    for summary in summaries:
        divergence = "-" if summary.mean_proxy_divergence is None else f"{summary.mean_proxy_divergence:.2f}"
        median = "-" if summary.median_fire_second is None else str(summary.median_fire_second)
        lines.append(
            f"{summary.variant:<16}{summary.markets:>9}{summary.actionable:>12}"
            f"{summary.actionable_rate * 100:>7.1f}%{summary.actionable_traded:>8}"
            f"{summary.false_positives:>5}{summary.false_positive_rate * 100:>7.1f}%"
            f"{median:>7}{divergence:>12}"
        )
    lines.append("")
    lines.append("traded = actionable signals in markets that traded at all; the rest are paper only.")
    return "\n".join(lines)


def load_settled_markets(path: str | Path) -> list[SettledMarket]:
    """Load ground-truth markets from JSONL.

    One JSON object per line with keys: ticker, strike_type ("greater"/"less"), strike_price,
    close_time (ISO 8601), result ("yes"/"no"), crypto_symbol, and optionally settlement_value.
    """
    markets: list[SettledMarket] = []
    with Path(path).open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            close_time = datetime.fromisoformat(record["close_time"].replace("Z", "+00:00"))
            markets.append(
                SettledMarket(
                    ticker=record["ticker"],
                    strike_type=record["strike_type"],
                    strike_price=float(record["strike_price"]),
                    close_time=close_time.astimezone(timezone.utc),
                    result=record["result"],
                    crypto_symbol=record["crypto_symbol"],
                    settlement_value=(
                        float(record["settlement_value"]) if record.get("settlement_value") is not None else None
                    ),
                    volume=float(record["volume"]) if record.get("volume") is not None else None,
                )
            )
    return markets


def load_price_series(path: str | Path) -> PriceSeries:
    """Load a historical index series from CSV (timestamp,price header) or JSONL.

    Timestamps are unix seconds. Any 1 Hz source works; the point of Tier 2 is precisely to
    measure how much a proxy source diverges from the real settlement index.
    """
    path = Path(path)
    points: list[tuple[float, float]] = []
    if path.suffix.lower() == ".csv":
        with path.open() as handle:
            for row in csv.DictReader(handle):
                points.append((float(row["timestamp"]), float(row["price"])))
    else:
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                points.append((float(record["timestamp"]), float(record["price"])))
    return PriceSeries(points)
