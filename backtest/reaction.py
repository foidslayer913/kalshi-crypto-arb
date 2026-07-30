"""Does a violent price move overshoot? Buying the dip, measured.

A near-certain contract collapsing from 96c to 71c is arresting to watch, and the natural thought is
that 71c must be too cheap. Two distinct claims hide in that thought:

* **Level.** "71c is cheap." Unconditionally it is not: 71c contracts win about 72% of the time,
  which is what 71c means. Reading the old 96c as evidence about fair value anchors on a price the
  index has already invalidated.
* **Dynamics.** "71c *right after a collapse* is cheap." This is a different and legitimate claim —
  that the market overreacts to a sharp move and mean-reverts. Nothing in the level studies tests
  it, because none of them conditioned on recent price change.

This module tests the dynamics claim. For each side of each market at each minute it measures the
move in that side's price over the preceding `lookback` minutes, then asks whether buying *after* a
move of that size beats break-even.

The sign convention answers the question as asked: a negative move means the side being bought just
got cheaper. So the "buy the dip" trade lives in the most negative bucket, and the verdict there is
whether overreaction is real:

* realized above break-even after large drops -> the market overshoots and the dip is tradeable;
* realized below break-even -> moves continue rather than revert, and buying the dip pays for the
  privilege of being run over.

Prices come from the *ask* for the transaction and the *mid* for measuring the move, because an ask
is reported at $1.00 when no offer exists and would fabricate enormous phantom moves.

As everywhere else here, the model is fit on one date range and judged on a later one.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from backtest.calibration import Observation, PooledResult, pool

Side = Literal["yes", "no"]


@dataclass(frozen=True)
class ReactionObservation:
    ticker: str
    side: Side
    price: float
    """Ask for the side being bought — what the trade actually costs."""
    move: float
    """Change in this side's mid over the lookback window. Negative means it just got cheaper."""
    won: bool
    minutes_to_close: float
    close_date: str
    contracts: int = 100

    def as_observation(self) -> Observation:
        return Observation(
            self.ticker, self.side, self.price, self.won, self.minutes_to_close, self.contracts
        )


def _rows(path: str | Path) -> Iterator[dict]:
    with Path(path).open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def build_observations(
    path: str | Path,
    *,
    lookback: int = 2,
    contracts: int = 100,
    min_minutes: float = 1.0,
    min_price: float = 0.05,
    max_price: float = 0.995,
) -> list[ReactionObservation]:
    """Reconstruct each market's price path and attach the recent move to every opportunity."""
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for row in _rows(path):
        if row.get("result") in ("yes", "no") and row.get("close_time"):
            by_ticker[row.get("ticker", "?")].append(row)

    built: list[ReactionObservation] = []
    for ticker, rows in by_ticker.items():
        # minutes_to_close counts down, so sorting descending walks the market forward in time.
        ordered = sorted(rows, key=lambda r: -float(r.get("minutes_to_close", 0.0)))
        mids: list[float | None] = []
        for row in ordered:
            ask, bid = row.get("yes_ask"), row.get("yes_bid")
            mids.append(None if ask is None or bid is None else (float(ask) + float(bid)) / 2.0)

        for index, row in enumerate(ordered):
            minutes = float(row.get("minutes_to_close", 0.0))
            if minutes < min_minutes or index < lookback:
                continue
            now_mid, past_mid = mids[index], mids[index - lookback]
            if now_mid is None or past_mid is None:
                continue
            result = row["result"]
            close_date = str(row["close_time"])[:10]
            move_yes = now_mid - past_mid

            yes_ask = row.get("yes_ask")
            if yes_ask is not None and min_price <= float(yes_ask) < max_price:
                built.append(
                    ReactionObservation(
                        ticker, "yes", float(yes_ask), move_yes, result == "yes",
                        minutes, close_date, contracts,
                    )
                )
            yes_bid = row.get("yes_bid")
            if yes_bid is not None:
                no_ask = 1.0 - float(yes_bid)
                if min_price <= no_ask < max_price:
                    built.append(
                        ReactionObservation(
                            ticker, "no", no_ask, -move_yes, result == "no",
                            minutes, close_date, contracts,
                        )
                    )
    return built


DEFAULT_MOVE_EDGES: tuple[float, ...] = (-0.25, -0.15, -0.08, -0.03, 0.03, 0.08, 0.15, 0.25)


@dataclass(frozen=True)
class MoveBucket:
    low: float
    high: float
    result: PooledResult

    @property
    def significant(self) -> bool:
        return self.result.significant


def bucket_by_move(
    observations: list[ReactionObservation], edges: tuple[float, ...] = DEFAULT_MOVE_EDGES
) -> list[MoveBucket]:
    bounds = (-float("inf"),) + edges + (float("inf"),)
    buckets: list[MoveBucket] = []
    for low, high in zip(bounds, bounds[1:]):
        group = [o for o in observations if low <= o.move < high]
        if not group:
            continue
        result = pool([o.as_observation() for o in group], f"{low:+.2f} to {high:+.2f}")
        if result is not None:
            buckets.append(MoveBucket(low, high, result))
    return buckets


def split_by_date(
    observations: list[ReactionObservation], train_end: str
) -> tuple[list[ReactionObservation], list[ReactionObservation]]:
    train = [o for o in observations if o.close_date <= train_end]
    test = [o for o in observations if o.close_date > train_end]
    return train, test


def format_moves(buckets: list[MoveBucket], label: str) -> str:
    lines = [
        f"{label}",
        "Move is the change in the bought side's price over the lookback. Negative = it just got",
        "cheaper, so 'buy the dip' is the leftmost row.",
        "",
        f"{'move':<20}{'obs':>8}{'mkts':>7}{'price':>8}{'realized':>10}"
        f"{'edge/contract':>15}{'95% CI':>20}{'verdict':>9}",
    ]
    lines.append("-" * len(lines[-1]))
    for bucket in buckets:
        result = bucket.result
        name = f"{bucket.low:+.2f} to {bucket.high:+.2f}".replace("-inf", "-inf ").replace("+inf", "+inf ")
        lines.append(
            f"{name:<20}{result.observations:>8}{result.markets:>7}{result.mean_price:>8.3f}"
            f"{result.realized:>10.3f}{result.edge:>+15.4f}"
            f"{f'{result.edge_low:+.4f} to {result.edge_high:+.4f}':>20}"
            f"{('SIGNIF' if bucket.significant else 'no'):>9}"
        )
    return "\n".join(lines)


def format_verdict(buckets: list[MoveBucket]) -> str:
    drops = [b for b in buckets if b.high <= -0.08]
    rises = [b for b in buckets if b.low >= 0.08]
    lines: list[str] = []
    if drops:
        worst = min(drops, key=lambda b: b.low)
        result = worst.result
        lines.append(
            f"After a drop of {abs(worst.high):.2f} or more ({result.observations} chances, "
            f"{result.markets} markets): buying earns {result.edge:+.4f}/contract "
            f"({result.edge_low:+.4f} to {result.edge_high:+.4f})."
        )
        if result.significant:
            lines.append(
                "  Positive at the pessimistic bound — the market overshoots on sharp moves and "
                "the dip is genuinely tradeable. Confirm on another period before sizing it."
            )
        elif result.edge_high < 0:
            lines.append(
                "  Significantly NEGATIVE: sharp moves continue rather than revert. Buying the dip "
                "is a losing trade, not a discount — the move was information, not panic."
            )
        else:
            lines.append(
                "  Indistinguishable from zero: the collapse already reflected the new fair value, "
                "so there is no overreaction to harvest."
            )
    if rises:
        best = max(rises, key=lambda b: b.high)
        lines.append("")
        lines.append(
            f"For symmetry, after a rise of {best.low:.2f} or more: "
            f"{best.result.edge:+.4f}/contract "
            f"({best.result.edge_low:+.4f} to {best.result.edge_high:+.4f})."
        )
    return "\n".join(lines)
