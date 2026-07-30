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
    min_price: float = 0.05,
    max_price: float = 0.995,
) -> list[ConditionalObservation]:
    """Join candle observations to the index and standardise the distance from the strike.

    `min_price` is far lower than the unconditional study's default: the whole point is to find
    moments where the market is wrong, and those can sit anywhere on the price scale.
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

        scale = sigma * math.sqrt(minutes)
        if scale <= 0:
            continue
        z_yes = math.log(spot / strike) / scale
        close_date = close_time[:10]

        yes_ask = row.get("yes_ask")
        if yes_ask is not None and min_price <= float(yes_ask) < max_price:
            built.append(
                ConditionalObservation(
                    row.get("ticker", "?"), "yes", float(yes_ask), result == "yes",
                    minutes, z_yes, close_date, contracts,
                )
            )
        yes_bid = row.get("yes_bid")
        if yes_bid is not None:
            no_ask = 1.0 - float(yes_bid)
            if min_price <= no_ask < max_price:
                built.append(
                    ConditionalObservation(
                        row.get("ticker", "?"), "no", no_ask, result == "no",
                        minutes, -z_yes, close_date, contracts,
                    )
                )
    return built


def _to_unix_iso(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class FairModel:
    """Empirical P(win | z), fit by bucketing z and measuring realized frequency."""

    def __init__(self, width: float = 0.25, min_samples: int = 40) -> None:
        self.width = width
        self.min_samples = min_samples
        self._rate: dict[int, float] = {}
        self._counts: dict[int, int] = {}
        self._fallback = 0.5

    def _bucket(self, z: float) -> int:
        # Clamp so the tails collapse into end buckets instead of forming singleton cells whose
        # rates are pure noise.
        return max(-16, min(16, int(math.floor(z / self.width))))

    def fit(self, observations: list[ConditionalObservation]) -> "FairModel":
        wins: dict[int, int] = {}
        total: dict[int, int] = {}
        for observation in observations:
            key = self._bucket(observation.z)
            total[key] = total.get(key, 0) + 1
            wins[key] = wins.get(key, 0) + (1 if observation.won else 0)
        self._counts = total
        self._rate = {
            key: wins[key] / total[key] for key in total if total[key] >= self.min_samples
        }
        if observations:
            self._fallback = sum(1 for o in observations if o.won) / len(observations)
        return self

    def probability(self, z: float) -> float | None:
        """Fair win probability for this z, or None where the fit has too little support."""
        return self._rate.get(self._bucket(z))

    def table(self) -> list[tuple[float, float, int, float]]:
        """(z_low, z_high, samples, win_rate) for each fitted bucket."""
        return [
            (key * self.width, (key + 1) * self.width, self._counts[key], self._rate[key])
            for key in sorted(self._rate)
        ]


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
        model_p = model.probability(observation.z)
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
    rates = [rate for *_, rate in model.table()]
    if len(rates) >= 2:
        monotone = all(a <= b + 0.05 for a, b in zip(rates, rates[1:]))
        lines.append("")
        lines.append(
            "Win rate rises with z as it should — the coordinate is informative."
            if monotone
            else "WARNING: win rate is not rising with z. The coordinate may be mis-signed or the "
            "volatility estimate wrong; nothing downstream is trustworthy until this is monotone."
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
