"""Opening-price study: is a side that opens cheap actually underpriced?

At the open of a 15-minute market the strike is the index average at that moment, so the index sits
on the line and each side is ~50/50 by construction. If a side's opening quote is well below 50c,
that is a candidate mispricing that needs no prediction at all — fair value is fixed at ~0.5 by the
market's own design, so any side offered materially under 0.5 is cheap unless the index had already
moved.

For each market this takes the first candle (the one covering the open) and, for each side, the
**opening** ask. Where that ask is below a threshold it simulates buying and scores against the
settled result. A threshold sweep shows how the edge behaves as the entry gets more aggressive, and
the split-date option checks it replicates.

Two honesty limits, both stated in the output:

* Resolution is one minute. The `open_dollars` of the first candle is the opening quote, the closest
  public proxy for "the first few seconds", but a rule keyed on "< 3 seconds since open" can only be
  verified exactly against the trades log. This measures the opening quote, not a 3-second window.
* Fillability is unverified. An opening ask of 0.45 means someone offered there, but not that the
  size was more than a token amount. If the effect is real the next step is the order book capture,
  not a bigger backtest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from backtest.calibration import Observation, pool
from strategy.fee_calculator import calculate_fee

Side = Literal["yes", "no"]


@dataclass(frozen=True)
class OpeningQuote:
    ticker: str
    result: str
    minutes_to_close: float
    yes_ask_open: float | None
    yes_bid_open: float | None
    close_date: str


def _rows(path: str | Path) -> Iterator[dict]:
    with Path(path).open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def opening_quotes(path: str | Path) -> list[OpeningQuote]:
    """The first candle of each market — the one covering the open."""
    first: dict[str, dict] = {}
    for row in _rows(path):
        if row.get("result") not in ("yes", "no") or not row.get("close_time"):
            continue
        ticker = row.get("ticker", "?")
        minutes = float(row.get("minutes_to_close", 0.0))
        # The opening candle is the one furthest from close.
        if ticker not in first or minutes > float(first[ticker].get("minutes_to_close", 0.0)):
            first[ticker] = row

    quotes: list[OpeningQuote] = []
    for ticker, row in first.items():
        quotes.append(
            OpeningQuote(
                ticker=ticker,
                result=row["result"],
                minutes_to_close=float(row.get("minutes_to_close", 0.0)),
                yes_ask_open=_maybe(row.get("yes_ask_open")),
                yes_bid_open=_maybe(row.get("yes_bid_open")),
                close_date=str(row["close_time"])[:10],
            )
        )
    return quotes


def _maybe(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def observations_below(
    quotes: list[OpeningQuote], threshold: float, contracts: int = 100
) -> list[Observation]:
    """Every side whose opening ask is below `threshold`, as a buy at that ask."""
    built: list[Observation] = []
    for quote in quotes:
        if quote.yes_ask_open is not None and quote.yes_ask_open < threshold:
            built.append(
                Observation(
                    quote.ticker, "yes", quote.yes_ask_open, quote.result == "yes",
                    quote.minutes_to_close, contracts,
                )
            )
        if quote.yes_bid_open is not None:
            no_ask = 1.0 - quote.yes_bid_open
            if no_ask < threshold:
                built.append(
                    Observation(
                        quote.ticker, "no", no_ask, quote.result == "no",
                        quote.minutes_to_close, contracts,
                    )
                )
    return built


def split_by_date(quotes: list[OpeningQuote], train_end: str) -> tuple[list[OpeningQuote], list[OpeningQuote]]:
    return (
        [q for q in quotes if q.close_date <= train_end],
        [q for q in quotes if q.close_date > train_end],
    )


def format_sweep(
    quotes: list[OpeningQuote],
    thresholds: tuple[float, ...] = (0.40, 0.42, 0.44, 0.46, 0.48, 0.50),
    contracts: int = 100,
    label: str = "",
) -> str:
    lines = []
    if label:
        lines.append(label)
    lines.append(
        f"{'buy if open ask <':<18}{'trades':>8}{'mkts':>7}{'avg cost':>10}"
        f"{'win rate':>10}{'edge/contract':>15}{'95% CI':>20}{'verdict':>9}"
    )
    lines.append("-" * len(lines[-1]))
    any_positive = False
    for threshold in thresholds:
        observations = observations_below(quotes, threshold, contracts)
        result = pool(observations, f"<{threshold}")
        if result is None:
            lines.append(f"{f'{threshold:.2f}':<18}{'0':>8}  (no opening quotes below this)")
            continue
        verdict = "PROFIT" if result.significant else "no"
        any_positive = any_positive or result.significant
        lines.append(
            f"{f'{threshold:.2f}':<18}{result.observations:>8}{result.markets:>7}"
            f"{result.mean_price:>10.3f}{result.realized:>10.3f}{result.edge:>+15.4f}"
            f"{f'{result.edge_low:+.4f} to {result.edge_high:+.4f}':>20}{verdict:>9}"
        )
    lines.append("")
    lines.append(
        "'edge/contract' already nets the taker fee. A side that opens at 0.45 and wins ~50% would"
    )
    lines.append("show a large positive edge; one that wins ~45% (fairly priced) would not.")
    return "\n".join(lines)
