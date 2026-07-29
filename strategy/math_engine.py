from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

# A bound policy maps the ticks observed so far to the price assumed for every *unknown* tick.
# Returning a constant reproduces SPEC.md's strict invariant; deriving the bound from the latest
# observed price gives the relaxed variant (see `relative_floor` / `relative_cap`).
BoundPolicy = Callable[[Sequence[float]], float]


def relative_floor(delta: float) -> BoundPolicy:
    """Assume unknown ticks land no lower than `latest * (1 - delta)`.

    This trades certainty for trigger frequency: the strict floor of 0 only becomes binding in the
    final second or two of the window, whereas a relative floor fires far earlier. The result is no
    longer mathematically guaranteed — it holds unless the index falls more than `delta` inside the
    remainder of the window, which is a tail risk to be measured, not an invariant.
    """
    if not 0.0 <= delta <= 1.0:
        raise ValueError("delta must be between 0.0 and 1.0")

    def policy(known_ticks: Sequence[float]) -> float:
        if not known_ticks:
            return 0.0  # nothing observed yet, so fall back to the strict bound
        return known_ticks[-1] * (1.0 - delta)

    return policy


def relative_cap(delta: float) -> BoundPolicy:
    """Assume unknown ticks land no higher than `latest * (1 + delta)`. Mirror of `relative_floor`."""
    if delta < 0.0:
        raise ValueError("delta must be non-negative")

    def policy(known_ticks: Sequence[float]) -> float:
        if not known_ticks:
            return float("inf")
        return known_ticks[-1] * (1.0 + delta)

    return policy


@dataclass
class SettlementWindow:
    """Tracks the fixed price ticks accumulated so far within a 60-second settlement window
    and evaluates whether the final CFB RTI settlement average is already mathematically
    determined, per SPEC.md section 3.
    """

    window_size: int = 60
    price_floor: float = 0.0
    price_cap: float = float("inf")
    floor_policy: BoundPolicy | None = None
    cap_policy: BoundPolicy | None = None
    _ticks: list[float | None] = field(default_factory=list)

    def record_tick(self, price: float | None) -> None:
        """Append one second's tick. Pass None for a missing/unavailable tick."""
        if len(self._ticks) >= self.window_size:
            raise ValueError(f"Settlement window already has {self.window_size} ticks")
        self._ticks.append(price)

    def reset(self) -> None:
        self._ticks.clear()

    @property
    def ticks_recorded(self) -> int:
        return len(self._ticks)

    @property
    def known_ticks(self) -> list[float]:
        return [price for price in self._ticks if price is not None]

    @property
    def cumulative_sum(self) -> float:
        """S_k: sum of the ticks whose value is actually known."""
        return sum(self.known_ticks)

    @property
    def unknown_ticks(self) -> int:
        """Ticks whose value the bounds must stand in for: those not yet recorded, plus those
        recorded as missing. Both are equally unknown, so both take the floor/cap.
        """
        recorded_but_missing = self.ticks_recorded - len(self.known_ticks)
        return (self.window_size - self.ticks_recorded) + recorded_but_missing

    def _effective_floor(self) -> float:
        if self.floor_policy is not None:
            return self.floor_policy(self.known_ticks)
        return self.price_floor

    def _effective_cap(self) -> float:
        if self.cap_policy is not None:
            return self.cap_policy(self.known_ticks)
        return self.price_cap

    def guaranteed_floor_average(self) -> float:
        """Floor Average_k = (S_k + unknown * P_floor) / 60.

        With the strict `price_floor = 0` this is SPEC.md's guaranteed floor. Note the bound is
        applied to *every* unknown tick, including ones recorded as missing — with a zero floor
        that is a no-op, but with a relative floor a gap must not silently count as $0.
        """
        return (self.cumulative_sum + self.unknown_ticks * self._effective_floor()) / self.window_size

    def guaranteed_ceiling_average(self) -> float:
        """Ceiling Average_k = (S_k + unknown * P_cap) / 60.

        Once nothing is unknown the cap contributes nothing regardless of its value; that is
        handled explicitly so the default `price_cap = inf` cannot turn `0 * inf` into `nan`.
        """
        unknown = self.unknown_ticks
        if unknown == 0:
            return self.cumulative_sum / self.window_size
        return (self.cumulative_sum + unknown * self._effective_cap()) / self.window_size

    def is_guaranteed_above(self, strike_price: float) -> bool:
        """True once the floor average alone exceeds the strike, so the settlement average cannot
        end at or below it.
        """
        return self.guaranteed_floor_average() > strike_price

    def is_guaranteed_below(self, strike_price: float) -> bool:
        """True once the ceiling average alone falls below the strike, so the settlement average
        cannot end at or above it.
        """
        return self.guaranteed_ceiling_average() < strike_price
