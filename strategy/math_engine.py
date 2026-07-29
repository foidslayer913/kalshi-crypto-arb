from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SettlementWindow:
    """Tracks the fixed price ticks accumulated so far within a 60-second settlement window
    and evaluates whether the final CFB RTI settlement average is already mathematically
    determined, per SPEC.md section 3.
    """

    window_size: int = 60
    price_floor: float = 0.0
    price_cap: float = float("inf")
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
    def cumulative_sum(self) -> float:
        """S_k: sum of known ticks recorded so far. Missing ticks contribute nothing, which is
        equivalent to treating them at the floor — the same conservative assumption applied to
        ticks that haven't arrived yet.
        """
        return sum(price for price in self._ticks if price is not None)

    def guaranteed_floor_average(self) -> float:
        """Guaranteed Floor Average_k = (S_k + (60 - k) * P_floor) / 60.

        Missing ticks already contribute price_floor implicitly (excluded from cumulative_sum),
        so this is safe to call at any point during the window, including with gaps.
        """
        remaining = self.window_size - self.ticks_recorded
        return (self.cumulative_sum + remaining * self.price_floor) / self.window_size

    def guaranteed_ceiling_average(self) -> float:
        """Guaranteed Ceiling Average_k = (S_k + (60 - k) * P_cap) / 60."""
        remaining = self.window_size - self.ticks_recorded
        return (self.cumulative_sum + remaining * self.price_cap) / self.window_size

    def is_guaranteed_above(self, strike_price: float) -> bool:
        """True once the floor average alone exceeds the strike — the contract is mathematically
        guaranteed to settle at $1.00 regardless of any remaining or missing ticks.
        """
        return self.guaranteed_floor_average() > strike_price

    def is_guaranteed_below(self, strike_price: float) -> bool:
        """True once the ceiling average alone falls below the strike — the contract is
        mathematically guaranteed to settle at $0.00.
        """
        return self.guaranteed_ceiling_average() < strike_price
