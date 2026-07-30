"""Conditional study: is the market wrong *when the index says it should be*?

The unconditional calibration study averages over every candle of every market and finds the price
about as accurate as the fee is large. That answers "is this market broadly mispriced?" — not "are
there identifiable moments when it is mispriced?", which is the question a discretionary trader is
actually asking. Averaging over everything destroys exactly the signal such a trader selects for:
if the price is fair 90% of the time and 10c cheap the other 10%, the unconditional average is
+0.1c and invisible, while trading only that tenth earns 10c a go.

The distinguishing information is the **index**. A trade like "BTC is 0.13% below the line with five
minutes left and down is only 91c" compares two things, and the unconditional study only had the
price. Here both are used:

1. Standardise the index's distance from the strike by the move still available in the time left:
   `z = ln(index / strike) / (sigma_per_minute * sqrt(minutes_left))`, signed so a larger `z` always
   favours the side being bought. `z` is the natural coordinate — it makes a market 0.1% away with
   one minute left comparable to one 0.3% away with nine.

2. Learn what `z` is *actually worth* from realized outcomes, rather than assuming a distribution.
   A parametric normal would import assumptions (lognormality, no drift, a point settlement rather
   than a 60-second average); an empirical table absorbs all of that, needing only that `z` be
   monotone in the true probability.

3. Fit that table on a **training** date range and evaluate on a **later, held-out** range. Fitting
   and testing on the same data guarantees finding "edge" wherever the market disagrees with sample
   noise, which is how a study like this manufactures a strategy that does not exist.

The reported quantity is then: among held-out moments where the model and the market disagree by
more than a threshold, what did buying actually earn per contract?
"""

from __future__ import annotations

import bisect
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from backtest.calibration import Observation, PooledResult, pool
from strategy.fee_calculator import calculate_fee

Side = Literal["yes", "no"]


class MinuteIndex:
    """A 1-minute index series, queried at a timestamp without looking into the future."""

    def __init__(self, points: list[tuple[int, float]]) -> None:
        ordered = sorted(points)
        self._timestamps = [timestamp for timestamp, _ in ordered]
        self._prices = [price for _, price in ordered]

    def __len__(self) -> int:
        return len(self._timestamps)

    @classmethod
    def from_csv(cls, path: str | Path) -> "MinuteIndex":
        points: list[tuple[int, float]] = []
        with Path(path).open() as handle:
            for row in csv.DictReader(handle):
                points.append((int(float(row["timestamp"])), float(row["price"])))
        return cls(points)

    def price_at(self, timestamp: float, max_staleness: float = 90.0) -> float | None:
        """Most recent price at or before `timestamp`, or None if nothing recent enough.

        Staleness defaults to 90s: one minute of spacing plus slack. A wider tolerance would happily
        answer with a price from before a gap, quietly pretending the index stood still.
        """
        position = bisect.bisect_right(self._timestamps, timestamp) - 1
        if position < 0:
            return None
        if timestamp - self._timestamps[position] > max_staleness:
            return None
        return self._prices[position]

    def sigma_per_minute(self) -> float:
        """Standard deviation of 1-minute log returns, measured from the series itself.

        Only consecutive minutes are used, so a gap does not masquerade as one enormous return.
        """
        returns: list[float] = []
        for i in range(1, len(self._timestamps)):
            if self._timestamps[i] - self._timestamps[i - 1] != 60:
                continue
            previous, current = self._prices[i - 1], self._prices[i]
            if previous > 0 and current > 0:
                returns.append(math.log(current / previous))
        if len(returns) < 2:
            raise ValueError("Not enough consecutive minutes to estimate volatility")
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(variance)


@dataclass(frozen=True)
class ConditionalObservation:
    """A tradeable proposition plus where the index stood when it was quoted."""

    ticker: str
    side: Side
    price: float
    won: bool
    minutes_to_close: float
    z: float
    """Standardised index distance from the strike, signed so higher always favours `side`."""
    close_date: str
    contracts: int = 1
    slope: float = 0.0
    """Standardised index *velocity* over the lookback, signed so positive favours `side`.

    Level and velocity are different information. `z` says where the index sits; `slope` says which
    way it is travelling. For a driftless random walk slope is worthless by construction, so any
    predictive power it shows is evidence of short-horizon trend — and it is only *tradeable* if the
    market has not already priced that trend.
    """

    @property
    def fee_per_contract(self) -> float:
        return calculate_fee(self.contracts, self.price) / self.contracts

    @property
    def breakeven(self) -> float:
        return self.price + self.fee_per_contract

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
    observations_path: str | Path,
    index: MinuteIndex,
    *,
    sigma_per_minute: float | None = None,
    contracts: int = 100,
    min_minutes: float = 1.0,
    max_minutes: float | None = None,
    min_price: float = 0.05,
    max_price: float = 0.995,
    slope_lookback: int = 0,
    settlement_average: bool = True,
) -> list[ConditionalObservation]:
    """Join candle observations to the index and standardise the distance from the strike.

    `min_price` is far lower than the unconditional study's default: the whole point is to find
    moments where the market is wrong, and those can sit anywhere on the price scale.

    `slope_lookback > 0` additionally measures the index's velocity over that many minutes, in the
    same standardised units as `z`, so trend can be tested as information separate from level.

    `settlement_average` controls the variance used to standardise the distance, and it is the whole
    point near the close. Settlement is the average of the index over the final 60 seconds, whose
    variance is `sigma^2 * (t - 2/3)` in per-minute units — not the endpoint's `sigma^2 * t`. At one
    minute left the average's standard deviation is 0.58x the endpoint's, so treating settlement as a
    point (the default a naive model — or a naive market — would use) overstates the remaining
    uncertainty by 1.7x and prices favourites too cheap. Passing False restores the endpoint
    variance, which is exactly the comparison that reveals whether the market makes this error.
    """
    sigma = sigma_per_minute if sigma_per_minute is not None else index.sigma_per_minute()
    built: list[ConditionalObservation] = []
    for row in _rows(observations_path):
        strike = row.get("strike")
        close_time = row.get("close_time")
        result = row.get("result")
        minutes = float(row.get("minutes_to_close", 0.0))
        if strike is None or not close_time or result not in ("yes", "no") or minutes < min_minutes:
            continue
        if max_minutes is not None and minutes > max_minutes:
            continue
        strike = float(strike)
        if strike <= 0:
            continue

        # The absolute candle time is recoverable from the close and the offset, so no re-fetch is
        # needed to add the index dimension to observations already on disk.
        close_ts = _to_unix_iso(close_time)
        candle_ts = close_ts - minutes * 60.0
        spot = index.price_at(candle_ts)
        if spot is None or spot <= 0:
            continue

        # Variance of the settlement average vs the endpoint (see the docstring). For minutes >= 1
        # the whole averaging window is in the future and (minutes - 2/3) is safely positive.
        variance_minutes = (minutes - 2.0 / 3.0) if settlement_average else minutes
        if variance_minutes <= 0:
            continue
        scale = sigma * math.sqrt(variance_minutes)
        if scale <= 0:
            continue
        z_yes = math.log(spot / strike) / scale
        close_date = close_time[:10]

        slope_yes = 0.0
        if slope_lookback > 0:
            earlier = index.price_at(candle_ts - slope_lookback * 60)
            if earlier is None or earlier <= 0:
                continue  # cannot measure velocity here, so do not guess it as flat
            slope_yes = math.log(spot / earlier) / (sigma * math.sqrt(slope_lookback))

        yes_ask = row.get("yes_ask")
        if yes_ask is not None and min_price <= float(yes_ask) < max_price:
            built.append(
                ConditionalObservation(
                    row.get("ticker", "?"), "yes", float(yes_ask), result == "yes",
                    minutes, z_yes, close_date, contracts, slope_yes,
                )
            )
        yes_bid = row.get("yes_bid")
        if yes_bid is not None:
            no_ask = 1.0 - float(yes_bid)
            if min_price <= no_ask < max_price:
                built.append(
                    ConditionalObservation(
                        row.get("ticker", "?"), "no", no_ask, result == "no",
                        minutes, -z_yes, close_date, contracts, -slope_yes,
                    )
                )
    return built


def _to_unix_iso(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class FairModel:
    """Empirical P(win | z), optionally P(win | z, slope), from realized frequencies.

    Adding the slope dimension multiplies the number of cells, so each is fit on fewer samples. That
    is the cost of asking a richer question, and it is why `min_samples` matters more in 2-D: a cell
    with a handful of observations reports noise as a probability.
    """

    def __init__(
        self, width: float = 0.25, min_samples: int = 40, slope_width: float | None = None
    ) -> None:
        self.width = width
        self.min_samples = min_samples
        self.slope_width = slope_width
        self._rate: dict[tuple[int, int], float] = {}
        self._counts: dict[tuple[int, int], int] = {}
        self._fallback = 0.5

    def _bucket(self, z: float) -> int:
        # Clamp so the tails collapse into end buckets instead of forming singleton cells whose
        # rates are pure noise.
        return max(-16, min(16, int(math.floor(z / self.width))))

    def _key(self, z: float, slope: float) -> tuple[int, int]:
        if self.slope_width is None:
            return (self._bucket(z), 0)
        slope_bucket = max(-8, min(8, int(math.floor(slope / self.slope_width))))
        return (self._bucket(z), slope_bucket)

    def fit(self, observations: list[ConditionalObservation]) -> "FairModel":
        wins: dict[tuple[int, int], int] = {}
        total: dict[tuple[int, int], int] = {}
        for observation in observations:
            key = self._key(observation.z, observation.slope)
            total[key] = total.get(key, 0) + 1
            wins[key] = wins.get(key, 0) + (1 if observation.won else 0)
        self._counts = total
        self._rate = {
            key: wins[key] / total[key] for key in total if total[key] >= self.min_samples
        }
        if observations:
            self._fallback = sum(1 for o in observations if o.won) / len(observations)
        return self

    def probability(self, z: float, slope: float = 0.0) -> float | None:
        """Fair win probability for this cell, or None where the fit has too little support."""
        return self._rate.get(self._key(z, slope))

    def cells(self) -> int:
        return len(self._rate)

    def table(self) -> list[tuple[float, float, int, float]]:
        """(z_low, z_high, samples, win_rate) collapsed over slope, for the readability check."""
        by_z: dict[int, tuple[int, float]] = {}
        for (z_key, _), rate in self._rate.items():
            samples = self._counts[(z_key, _)]
            previous_samples, previous_weighted = by_z.get(z_key, (0, 0.0))
            by_z[z_key] = (previous_samples + samples, previous_weighted + rate * samples)
        return [
            (key * self.width, (key + 1) * self.width, samples, weighted / samples)
            for key, (samples, weighted) in sorted(by_z.items())
        ]


def mean_absolute_error(
    model: FairModel, observations: list[ConditionalObservation]
) -> tuple[float, float, int]:
    """(model MAE, market MAE, n) over observations the model will score.

    This is the diagnostic that separates the two questions a new feature raises. Does it make the
    *model* better — model MAE falling when the feature is added? And does the better model beat the
    *market* — model MAE below market MAE? A feature can pass the first and fail the second, which
    means the information is real but already in the price.
    """
    model_errors: list[float] = []
    market_errors: list[float] = []
    for observation in observations:
        probability = model.probability(observation.z, observation.slope)
        if probability is None:
            continue
        outcome = 1.0 if observation.won else 0.0
        model_errors.append(abs(outcome - probability))
        market_errors.append(abs(outcome - observation.price))
    if not model_errors:
        return (float("nan"), float("nan"), 0)
    return (
        sum(model_errors) / len(model_errors),
        sum(market_errors) / len(market_errors),
        len(model_errors),
    )


@dataclass(frozen=True)
class DivergenceBucket:
    low: float
    high: float
    result: PooledResult
    mean_model_probability: float

    @property
    def significant(self) -> bool:
        return self.result.significant


def evaluate(
    model: FairModel,
    observations: list[ConditionalObservation],
    edges: tuple[float, ...] = (-0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10),
) -> list[DivergenceBucket]:
    """Bucket held-out observations by model-minus-market disagreement and measure realized edge.

    Divergence is measured against `breakeven`, not the raw price, so a bucket only looks attractive
    when the model beats the market by more than the fee.
    """
    scored: list[tuple[float, ConditionalObservation, float]] = []
    for observation in observations:
        model_p = model.probability(observation.z, observation.slope)
        if model_p is None:
            continue
        scored.append((model_p - observation.breakeven, observation, model_p))

    buckets: list[DivergenceBucket] = []
    bounds = (-float("inf"),) + edges + (float("inf"),)
    for low, high in zip(bounds, bounds[1:]):
        group = [(o, p) for d, o, p in scored if low <= d < high]
        if not group:
            continue
        result = pool([o.as_observation() for o, _ in group], f"{low:+.2f} to {high:+.2f}")
        if result is None:
            continue
        buckets.append(
            DivergenceBucket(
                low=low, high=high, result=result,
                mean_model_probability=sum(p for _, p in group) / len(group),
            )
        )
    return buckets


def split_by_date(
    observations: list[ConditionalObservation], train_end: str
) -> tuple[list[ConditionalObservation], list[ConditionalObservation]]:
    """Train on markets closing on or before `train_end`, test strictly after."""
    train = [o for o in observations if o.close_date <= train_end]
    test = [o for o in observations if o.close_date > train_end]
    return train, test


def format_model(model: FairModel) -> str:
    lines = [
        "Fair probability learned from the training range (z = standardised index distance,",
        "signed in favour of the side bought; higher z should mean a higher win rate):",
        "",
        f"{'z range':<16}{'samples':>9}{'win rate':>10}",
        "-" * 35,
    ]
    for low, high, samples, rate in model.table():
        lines.append(f"{f'{low:+.2f} to {high:+.2f}':<16}{samples:>9}{rate:>10.3f}")
    rows = model.table()
    if len(rows) >= 4:
        # Compare the well-populated low-z and high-z ends rather than requiring every adjacent pair
        # to be ordered. Extreme-tail cells hold a few dozen samples, so pairwise checks fire on
        # ordinary sampling noise and cry wolf on a perfectly usable coordinate.
        total = sum(samples for *_, samples, _ in rows)
        weighted = sorted(rows, key=lambda row: row[0])
        cumulative = 0
        low_rates: list[tuple[int, float]] = []
        high_rates: list[tuple[int, float]] = []
        for low, _high, samples, rate in weighted:
            cumulative += samples
            if cumulative <= total / 3:
                low_rates.append((samples, rate))
            elif cumulative >= total * 2 / 3:
                high_rates.append((samples, rate))
        lines.append("")
        if low_rates and high_rates:
            low = sum(s * r for s, r in low_rates) / sum(s for s, _ in low_rates)
            high = sum(s * r for s, r in high_rates) / sum(s for s, _ in high_rates)
            if high - low > 0.10:
                lines.append(
                    f"Win rate rises with z as it should ({low:.3f} in the bottom third of z, "
                    f"{high:.3f} in the top third) — the coordinate is informative."
                )
            else:
                lines.append(
                    f"WARNING: win rate barely rises with z ({low:.3f} -> {high:.3f}). The "
                    "coordinate may be mis-signed or the volatility estimate wrong; nothing "
                    "downstream is trustworthy until this separates."
                )
    return "\n".join(lines)


def format_evaluation(buckets: list[DivergenceBucket]) -> str:
    lines = [
        "Held-out edge by disagreement (model probability minus all-in cost).",
        "The rightmost rows are the trades a selective strategy would actually take.",
        "",
        f"{'divergence':<20}{'obs':>8}{'mkts':>7}{'model_p':>9}{'price':>8}"
        f"{'realized':>10}{'edge/contract':>15}{'95% CI':>20}{'verdict':>9}",
    ]
    lines.append("-" * len(lines[-1]))
    for bucket in buckets:
        result = bucket.result
        label = f"{bucket.low:+.2f} to {bucket.high:+.2f}".replace("-inf", " -inf").replace("+inf", " +inf")
        lines.append(
            f"{label:<20}{result.observations:>8}{result.markets:>7}"
            f"{bucket.mean_model_probability:>9.3f}{result.mean_price:>8.3f}"
            f"{result.realized:>10.3f}{result.edge:>+15.4f}"
            f"{f'{result.edge_low:+.4f} to {result.edge_high:+.4f}':>20}"
            f"{('SIGNIF' if bucket.significant else 'no'):>9}"
        )
    lines.append("")
    winners = [b for b in buckets if b.significant and b.low >= 0]
    if winners:
        best = max(winners, key=lambda b: b.result.edge_low)
        lines.append(
            f"Selective trading works on held-out data: {best.low:+.2f} to {best.high:+.2f} earns "
            f"{best.result.edge:+.4f}/contract (worst case {best.result.edge_low:+.4f}) over "
            f"{best.result.markets} markets, {best.result.observations} chances."
        )
    else:
        lines.append(
            "No positive-divergence bucket is profitable on held-out data. The model's disagreements "
            "with the market are not predictive, so the price already reflects the index."
        )
    return "\n".join(lines)
