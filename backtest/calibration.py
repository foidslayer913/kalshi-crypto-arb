"""Calibration study: is the market's price a fair probability, and where is it wrong?

For every (price, outcome) pair observed before expiry, bucket by price and compare the market's
implied probability against the realized win frequency. Buying a side at price `P` costs `P + fee`
and returns $1 when it wins, so the trade is profitable exactly when

    realized_probability > P + fee_per_contract

Two things this module refuses to do, because both turn noise into a strategy:

* **Report a bucket's edge without its uncertainty.** A 3-point edge on 40 observations is
  indistinguishable from chance. Every bucket carries a Wilson 95% interval on the realized rate,
  and a bucket is only called tradeable when the *lower* bound clears break-even.
* **Treat overlapping observations as independent.** Fifteen candles from one market share a single
  outcome, so the effective sample is closer to the market count than the observation count. Both
  are reported, and the interval is computed on the market count for the tradeable verdict.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Literal

from strategy.fee_calculator import calculate_fee

Side = Literal["yes", "no"]


@dataclass(frozen=True)
class Observation:
    """One tradeable proposition: pay `price` for `side`, receive $1 if `won`."""

    ticker: str
    side: Side
    price: float
    won: bool
    minutes_to_close: float
    contracts: int = 1

    @property
    def fee_per_contract(self) -> float:
        """Taker fee per contract at this order size.

        Order size matters more than it looks: the fee rounds up to a whole cent for the *order*,
        so one contract at 91c pays ceil(0.573c) = 1c while 100 contracts pay 0.573c each. Pricing
        an edge off the single-contract fee overstates costs by more than half, which is decisive
        when the effect being measured is only a cent or two.
        """
        return calculate_fee(self.contracts, self.price) / self.contracts

    @property
    def breakeven(self) -> float:
        """Probability at which this trade is EV-neutral, including the taker fee."""
        return self.price + self.fee_per_contract


def load_observations(
    path: str | Path,
    *,
    min_minutes: float = 1.0,
    max_minutes: float | None = None,
    min_price: float = 0.50,
    max_price: float = 0.995,
    start: str | None = None,
    end: str | None = None,
    contracts: int = 1,
) -> list[Observation]:
    """Expand candle rows into tradeable propositions.

    `min_minutes` defaults to 1: the candle ending at the close overlaps the settlement window
    itself, so its price reflects an outcome already being determined and would flatter any result.
    Prices at or above `max_price` are dropped because a contract bought at $1.00 cannot profit,
    and the ask is reported as 1.0000 precisely when there is no offer at all.
    """
    observations: list[Observation] = []
    for row in _read_rows(path):
        # Date filtering enables the held-out check — fit on one range, confirm on another — without
        # re-fetching. An edge that does not survive a different period is a fit to noise.
        close_time = row.get("close_time") or ""
        if start is not None and close_time[:10] < start:
            continue
        if end is not None and close_time[:10] > end:
            continue
        minutes = float(row.get("minutes_to_close", 0.0))
        if minutes < min_minutes or (max_minutes is not None and minutes > max_minutes):
            continue
        result = row.get("result")
        if result not in ("yes", "no"):
            continue
        ticker = row.get("ticker", "?")

        # Buying YES lifts the yes ask.
        yes_ask = row.get("yes_ask")
        if yes_ask is not None and min_price <= float(yes_ask) < max_price:
            observations.append(
                Observation(ticker, "yes", float(yes_ask), result == "yes", minutes, contracts)
            )
        # Buying NO lifts the no ask, which is 1 - the yes bid.
        yes_bid = row.get("yes_bid")
        if yes_bid is not None:
            no_ask = 1.0 - float(yes_bid)
            if min_price <= no_ask < max_price:
                observations.append(
                    Observation(ticker, "no", no_ask, result == "no", minutes, contracts)
                )
    return observations


def _read_rows(path: str | Path) -> Iterator[dict]:
    with Path(path).open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because these rates sit near 1.0, where the normal
    interval runs past 100% and understates uncertainty in exactly the buckets that matter.
    """
    if n == 0:
        return (0.0, 1.0)
    phat = wins / n
    denominator = 1 + z**2 / n
    centre = (phat + z**2 / (2 * n)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True)
class Bucket:
    low: float
    high: float
    observations: int
    markets: int
    wins: int
    mean_price: float
    mean_breakeven: float
    realized: float
    realized_low: float
    realized_high: float

    @property
    def edge(self) -> float:
        """Expected dollars per contract: realized probability minus the all-in cost."""
        return self.realized - self.mean_breakeven

    @property
    def edge_lower_bound(self) -> float:
        """Edge using the pessimistic end of the confidence interval."""
        return self.realized_low - self.mean_breakeven

    @property
    def tradeable(self) -> bool:
        """Profitable even at the pessimistic end of the interval."""
        return self.edge_lower_bound > 0


def bucket_observations(observations: Iterable[Observation], width: float = 0.02) -> list[Bucket]:
    groups: dict[int, list[Observation]] = {}
    for observation in observations:
        groups.setdefault(int(observation.price / width), []).append(observation)

    buckets: list[Bucket] = []
    for index in sorted(groups):
        group = groups[index]
        wins = sum(1 for observation in group if observation.won)
        # One market contributes many candles that share a single outcome, so the market count is
        # the honest sample size for uncertainty.
        markets = len({observation.ticker for observation in group})
        realized = wins / len(group)
        low, high = wilson_interval(round(realized * markets), markets)
        buckets.append(
            Bucket(
                low=index * width,
                high=(index + 1) * width,
                observations=len(group),
                markets=markets,
                wins=wins,
                mean_price=sum(o.price for o in group) / len(group),
                mean_breakeven=sum(o.breakeven for o in group) / len(group),
                realized=realized,
                realized_low=low,
                realized_high=high,
            )
        )
    return buckets


def format_calibration(buckets: list[Bucket], min_markets: int = 30) -> str:
    header = (
        f"{'price range':<14}{'obs':>7}{'mkts':>6}{'implied':>9}{'breakeven':>11}"
        f"{'realized':>10}{'95% CI':>18}{'edge':>9}{'verdict':>11}"
    )
    lines = [
        "Buying a side at `price` pays off $1 when it wins; breakeven includes the taker fee.",
        "'edge' is expected dollars per contract at the realized rate; verdict uses the",
        "pessimistic end of the interval, so it only says TRADE when the edge survives doubt.",
        "",
        header,
        "-" * len(header),
    ]
    for bucket in buckets:
        thin = bucket.markets < min_markets
        verdict = "thin" if thin else ("TRADE" if bucket.tradeable else "no")
        lines.append(
            f"{bucket.low:.2f}-{bucket.high:.2f}    "
            f"{bucket.observations:>7}{bucket.markets:>6}"
            f"{bucket.mean_price:>9.3f}{bucket.mean_breakeven:>11.3f}"
            f"{bucket.realized:>10.3f}"
            f"{f'{bucket.realized_low:.3f}-{bucket.realized_high:.3f}':>18}"
            f"{bucket.edge:>+9.3f}{verdict:>11}"
        )

    tested = [b for b in buckets if b.markets >= min_markets]
    tradeable = [b for b in tested if b.tradeable]
    lines.append("")
    if tradeable:
        best = max(tradeable, key=lambda b: b.edge_lower_bound)
        lines.append(
            f"{len(tradeable)} of {len(tested)} bucket(s) profitable at the pessimistic bound. Best: "
            f"{best.low:.2f}-{best.high:.2f} at +${best.edge:.3f}/contract "
            f"(worst case +${best.edge_lower_bound:.3f}) over {best.markets} markets."
        )
        # Each bucket is its own 95% test, so scanning many of them will surface roughly one
        # spurious winner per twenty even when nothing is mispriced. An isolated tradeable bucket
        # surrounded by fair ones is far more likely noise than edge.
        expected_false = len(tested) * 0.05
        lines.append(
            f"CAUTION: {len(tested)} buckets tested at 95% confidence, so ~{expected_false:.1f} would "
            f"look tradeable by chance alone."
        )
        if len(tradeable) <= expected_false:
            lines.append(
                "  The count here is within that noise floor. Treat it as no finding unless the "
                "tradeable buckets are adjacent and trend consistently."
            )
        lines.append(
            "  Before acting: confirm the effect holds on a held-out date range, and that "
            "neighbouring buckets point the same way."
        )
    else:
        lines.append(
            f"No bucket is profitable once uncertainty is accounted for (min {min_markets} markets)."
        )
        lines.append(
            "Read this as 'no edge large enough to detect at this sample size', not 'no edge'. "
            "See the detection limits below."
        )

    # State the test's power. Without this, "no finding" reads as "no edge" when it may only mean
    # the sample cannot resolve an edge worth trading.
    if tested:
        lines.append("")
        lines.append("Smallest edge detectable per bucket (95%, from market counts):")
        for bucket in tested:
            p = max(min(bucket.realized, 0.999), 0.001)
            detectable = 1.96 * math.sqrt(p * (1 - p) / bucket.markets)
            lines.append(
                f"  {bucket.low:.2f}-{bucket.high:.2f}  {bucket.markets:>5} markets  "
                f"needs > ${detectable:.3f}/contract to show up"
            )
        lines.append(
            "  More markets, or wider buckets (--width 0.05), lower these thresholds."
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class PooledResult:
    """One hypothesis tested across a whole price range instead of bucket by bucket."""

    label: str
    observations: int
    markets: int
    mean_price: float
    mean_breakeven: float
    realized: float
    edge: float
    edge_se: float

    @property
    def edge_low(self) -> float:
        return self.edge - 1.96 * self.edge_se

    @property
    def edge_high(self) -> float:
        return self.edge + 1.96 * self.edge_se

    @property
    def significant(self) -> bool:
        return self.edge_low > 0


def pool(observations: list[Observation], label: str) -> PooledResult | None:
    """Average expected profit per contract over a price range, with a cluster-robust interval.

    Splitting a ~1c effect across twenty-five buckets tests each on a few hundred markets, where the
    detection floor is several cents — so a real edge is invisible bucket by bucket no matter how
    consistent it is. Pooling tests the pattern directly.

    Two choices make the interval honest:

    * Edge is measured per observation as `won - breakeven`, then **averaged within each market
      first**. Observations from one market share an outcome, so treating them as independent would
      shrink the interval by roughly the number of candles per market.
    * The standard error comes from the spread *across markets*, which is the level at which
      outcomes are actually independent.

    Pooling assumes the per-contract edge is roughly constant in dollars across the range, which is
    why it is worth running on sub-ranges rather than the whole book at once.
    """
    if not observations:
        return None
    per_market: dict[str, list[float]] = {}
    for observation in observations:
        edge = (1.0 if observation.won else 0.0) - observation.breakeven
        per_market.setdefault(observation.ticker, []).append(edge)
    market_edges = [sum(edges) / len(edges) for edges in per_market.values()]
    n = len(market_edges)
    mean_edge = sum(market_edges) / n
    if n > 1:
        variance = sum((edge - mean_edge) ** 2 for edge in market_edges) / (n - 1)
        se = math.sqrt(variance / n)
    else:
        se = float("inf")
    return PooledResult(
        label=label,
        observations=len(observations),
        markets=n,
        mean_price=sum(o.price for o in observations) / len(observations),
        mean_breakeven=sum(o.breakeven for o in observations) / len(observations),
        realized=sum(1 for o in observations if o.won) / len(observations),
        edge=mean_edge,
        edge_se=se,
    )


DEFAULT_POOL_RANGES: tuple[tuple[float, float], ...] = (
    (0.50, 0.995),
    (0.50, 0.78),
    (0.78, 0.995),
    (0.78, 0.90),
    (0.90, 0.995),
)


def format_pooled(
    observations: list[Observation], ranges: tuple[tuple[float, float], ...] = DEFAULT_POOL_RANGES
) -> str:
    lines = [
        "Pooled edge by price range (the per-bucket view cannot see a 1c effect; this can).",
        "Interval is cluster-robust: observations are averaged within a market first, because",
        "candles from one market share one outcome.",
        "",
        f"{'price range':<16}{'obs':>8}{'mkts':>7}{'implied':>9}{'realized':>10}"
        f"{'edge/contract':>15}{'95% CI':>20}{'verdict':>10}",
    ]
    lines.append("-" * len(lines[-1]))
    for low, high in ranges:
        group = [o for o in observations if low <= o.price < high]
        result = pool(group, f"{low:.2f}-{high:.2f}")
        if result is None:
            continue
        verdict = "SIGNIF" if result.significant else "no"
        lines.append(
            f"{result.label:<16}{result.observations:>8}{result.markets:>7}"
            f"{result.mean_price:>9.3f}{result.realized:>10.3f}"
            f"{result.edge:>+15.4f}"
            f"{f'{result.edge_low:+.4f} to {result.edge_high:+.4f}':>20}"
            f"{verdict:>10}"
        )
    lines.append("")
    lines.append(
        "A favourite-longshot bias shows up as a negative edge on the cheap side and a positive one "
        "on the expensive side."
    )
    return "\n".join(lines)


def format_by_horizon(
    observations: list[Observation], edges: tuple[float, ...] = (1, 2, 3, 5, 8, 15)
) -> str:
    """Edge by time remaining. A mispricing that only exists far from expiry is a different
    phenomenon (and a different trade) from one that persists into the final minute."""
    lines = [f"{'minutes to close':<20}{'obs':>8}{'mkts':>7}{'implied':>9}{'realized':>10}{'edge':>9}"]
    lines.append("-" * len(lines[0]))
    for low, high in zip((0.0,) + edges, edges + (float("inf"),)):
        group = [o for o in observations if low <= o.minutes_to_close < high]
        if not group:
            continue
        wins = sum(1 for o in group if o.won)
        realized = wins / len(group)
        breakeven = sum(o.breakeven for o in group) / len(group)
        label = f"{low:g}-{high:g}" if high != float("inf") else f"{low:g}+"
        lines.append(
            f"{label:<20}{len(group):>8}{len({o.ticker for o in group}):>7}"
            f"{sum(o.price for o in group) / len(group):>9.3f}{realized:>10.3f}"
            f"{realized - breakeven:>+9.3f}"
        )
    return "\n".join(lines)
