import pytest

from strategy.fee_calculator import calculate_fee, calculate_net_yield, meets_yield_threshold
from strategy.math_engine import SettlementWindow


def _window_with_ticks(count: int, price: float = 1.0, window_size: int = 60) -> SettlementWindow:
    window = SettlementWindow(window_size=window_size)
    for _ in range(count):
        window.record_tick(price)
    return window


@pytest.mark.parametrize(
    "seconds_elapsed,expected_floor_average",
    [(15, 15 / 60), (30, 30 / 60), (45, 45 / 60), (59, 59 / 60)],
)
def test_partial_window_floor_average(seconds_elapsed, expected_floor_average):
    window = _window_with_ticks(seconds_elapsed, price=1.0)
    assert window.ticks_recorded == seconds_elapsed
    assert window.guaranteed_floor_average() == pytest.approx(expected_floor_average)


@pytest.mark.parametrize(
    "seconds_elapsed,strike_below,strike_above",
    [(15, 0.24, 0.26), (30, 0.49, 0.51), (45, 0.74, 0.76), (59, 0.97, 0.99)],
)
def test_partial_window_is_guaranteed_above(seconds_elapsed, strike_below, strike_above):
    window = _window_with_ticks(seconds_elapsed, price=1.0)
    assert window.is_guaranteed_above(strike_below) is True
    assert window.is_guaranteed_above(strike_above) is False


def test_full_window_never_exceeds_actual_average():
    window = _window_with_ticks(60, price=1.0)
    assert window.guaranteed_floor_average() == pytest.approx(1.0)
    assert window.is_guaranteed_above(0.999) is True


def test_empty_window_has_zero_floor_average():
    window = SettlementWindow()
    assert window.ticks_recorded == 0
    assert window.guaranteed_floor_average() == 0.0
    assert window.is_guaranteed_above(0.0) is False


def test_missing_ticks_are_treated_as_floor():
    window = SettlementWindow()
    for _ in range(29):
        window.record_tick(1.0)
    window.record_tick(None)  # a missing tick at second 30
    assert window.ticks_recorded == 30
    # Only 29 known ticks contribute to S_k; the missing one contributes 0, same as unseen ticks.
    assert window.guaranteed_floor_average() == pytest.approx(29 / 60)


def test_missing_ticks_never_produce_a_higher_floor_average_than_all_known():
    with_gap = _window_with_ticks(0, price=1.0)
    for _ in range(10):
        with_gap.record_tick(1.0)
    for _ in range(10):
        with_gap.record_tick(None)
    fully_known = _window_with_ticks(10, price=1.0)
    assert with_gap.guaranteed_floor_average() == fully_known.guaranteed_floor_average()


def test_record_tick_beyond_window_size_raises():
    window = SettlementWindow(window_size=2)
    window.record_tick(1.0)
    window.record_tick(1.0)
    with pytest.raises(ValueError):
        window.record_tick(1.0)


def test_reset_clears_recorded_ticks():
    window = _window_with_ticks(10, price=1.0)
    window.reset()
    assert window.ticks_recorded == 0
    assert window.guaranteed_floor_average() == 0.0


def test_guaranteed_ceiling_average_at_full_window_with_default_infinite_cap():
    # Regression: 0 * inf must not turn into nan once the window is full and price_cap is
    # left at its default (unset caps are only meaningful while ticks remain).
    window = _window_with_ticks(60, price=0.0)
    assert window.guaranteed_ceiling_average() == pytest.approx(0.0)
    assert window.is_guaranteed_below(1.0) is True


def test_guaranteed_ceiling_and_is_guaranteed_below():
    window = SettlementWindow(price_cap=1.0)
    for _ in range(59):
        window.record_tick(0.0)
    # 59 known zeros plus 1 remaining tick capped at 1.0 -> ceiling average = 1/60.
    assert window.guaranteed_ceiling_average() == pytest.approx(1 / 60)
    assert window.is_guaranteed_below(0.5) is True
    assert window.is_guaranteed_below(0.0) is False


@pytest.mark.parametrize("price,expected_fee", [(0.90, 0.01), (0.95, 0.01), (0.98, 0.01)])
def test_calculate_fee_single_contract(price, expected_fee):
    assert calculate_fee(1, price) == pytest.approx(expected_fee)


def test_calculate_fee_scales_with_contract_count():
    # 0.07 * 100 * 0.95 * 0.05 = 0.3325 dollars = 33.25 cents -> rounds up to 34 cents.
    assert calculate_fee(100, 0.95) == pytest.approx(0.34)


def test_calculate_fee_is_symmetric_around_fifty_cents():
    assert calculate_fee(1, 0.30) == pytest.approx(calculate_fee(1, 0.70))


def test_calculate_fee_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        calculate_fee(0, 0.5)
    with pytest.raises(ValueError):
        calculate_fee(1, 1.5)


def test_calculate_net_yield_at_95_cents():
    # fee at 95c/1 contract is $0.01, so net yield = 1.00 - 0.95 - 0.01 = 0.04.
    assert calculate_net_yield(0.95) == pytest.approx(0.04)


def test_meets_yield_threshold():
    assert meets_yield_threshold(0.95, 0.01) is True
    assert meets_yield_threshold(0.95, 0.05) is False
