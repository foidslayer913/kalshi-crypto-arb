from __future__ import annotations

import math

# Kalshi's fee is quoted per SPEC.md as ceil(0.07 * C * P * (1 - P)) but that formula only makes
# sense rounded up to the nearest whole cent — evaluated in raw dollars it would round every
# nonzero fee up to $1. round() guards against float artifacts (e.g. 1.9999999999998) landing on
# the wrong side of math.ceil.
_FEE_RATE = 0.07
_CENT_ROUNDING_PRECISION = 8


def calculate_fee(contracts: int, price: float) -> float:
    """Kalshi taker fee in dollars, per contract count `contracts` at price `price` (0.0-1.0)."""
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    if not 0.0 <= price <= 1.0:
        raise ValueError("price must be between 0.0 and 1.0")
    raw_fee_cents = _FEE_RATE * contracts * price * (1 - price) * 100
    return math.ceil(round(raw_fee_cents, _CENT_ROUNDING_PRECISION)) / 100


def calculate_net_yield(ask_price: float, contracts: int = 1) -> float:
    """Net yield per contract if the position resolves to $1.00: 1.00 - ask - fee per contract."""
    fee_per_contract = calculate_fee(contracts, ask_price) / contracts
    return 1.0 - ask_price - fee_per_contract


def meets_yield_threshold(ask_price: float, min_yield_threshold: float, contracts: int = 1) -> bool:
    return calculate_net_yield(ask_price, contracts) >= min_yield_threshold
