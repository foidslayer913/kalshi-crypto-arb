import json
import random

import pytest

from backtest.calibration import (
    Observation,
    bucket_observations,
    load_observations,
    wilson_interval,
)


def _write(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def _row(**overrides):
    row = {
        "ticker": "KXBTC15M-26JUL290015-15",
        "result": "yes",
        "minutes_to_close": 5.0,
        "yes_ask": 0.91,
        "yes_bid": 0.90,
    }
    row.update(overrides)
    return row


def test_each_candle_yields_a_yes_and_a_no_proposition(tmp_path):
    path = tmp_path / "obs.jsonl"
    _write(path, [_row()])
    observations = load_observations(path, min_price=0.0)  # keep both legs regardless of price
    by_side = {o.side: o for o in observations}
    assert by_side["yes"].price == pytest.approx(0.91)
    assert by_side["yes"].won is True
    # A NO ask is 1 - the YES bid, and this market settled yes, so buying NO loses.
    assert by_side["no"].price == pytest.approx(0.10)
    assert by_side["no"].won is False


def test_no_side_price_covers_the_other_tail(tmp_path):
    # A cheap YES means an expensive NO; with min_price filtering only the NO leg should survive.
    path = tmp_path / "obs.jsonl"
    _write(path, [_row(yes_ask=0.10, yes_bid=0.08, result="no")])
    observations = load_observations(path, min_price=0.50)
    assert [(o.side, round(o.price, 2)) for o in observations] == [("no", 0.92)]
    assert observations[0].won is True


def test_breakeven_includes_the_fee():
    observation = Observation("T", "yes", 0.91, True, 5.0)
    # fee = ceil(0.07 * 0.91 * 0.09 * 100)/100 = 0.01, so breakeven is 0.92 not 0.91.
    assert observation.breakeven == pytest.approx(0.92)


def test_final_candle_is_excluded_by_default(tmp_path):
    # The candle ending at the close overlaps the settlement window, so its price reflects an
    # outcome already being determined; counting it would flatter any result.
    path = tmp_path / "obs.jsonl"
    _write(path, [_row(minutes_to_close=0.0)])
    assert load_observations(path) == []
    assert load_observations(path, min_minutes=0.0) != []


def test_prices_at_a_dollar_are_dropped(tmp_path):
    # yes_ask is reported as 1.0000 when there is no offer at all; a $1 contract cannot profit.
    path = tmp_path / "obs.jsonl"
    _write(path, [_row(yes_ask=1.0, yes_bid=0.999)])
    assert [o.side for o in load_observations(path)] == []


def test_wilson_interval_stays_inside_zero_and_one():
    low, high = wilson_interval(40, 40)
    assert low > 0.85
    assert high == 1.0  # a normal approximation would exceed 1 here
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_widens_as_the_sample_shrinks():
    small = wilson_interval(19, 20)
    large = wilson_interval(950, 1000)
    assert (small[1] - small[0]) > (large[1] - large[0])


def _observations(price, win_rate, markets, seed=0):
    rng = random.Random(seed)
    return [
        Observation(f"M{i}", "yes", price, rng.random() < win_rate, 5.0)
        for i in range(markets)
    ]


def test_a_fairly_priced_market_is_not_called_tradeable():
    # Wins at exactly the breakeven rate: there is no edge to find, and the harness must not
    # manufacture one.
    observations = _observations(0.91, 0.92, 800)
    bucket = bucket_observations(observations, width=0.02)[0]
    assert bucket.tradeable is False


def test_a_genuinely_mispriced_bucket_is_detected():
    # Priced at 0.91 (breakeven 0.92) but wins 99% of the time — a real 7c edge.
    observations = _observations(0.91, 0.99, 800, seed=1)
    bucket = bucket_observations(observations, width=0.02)[0]
    assert bucket.edge > 0.05
    assert bucket.tradeable is True


def test_a_small_sample_edge_is_not_called_tradeable():
    # The same 99% win rate on 12 markets: the interval is too wide to act on.
    observations = _observations(0.91, 0.99, 12, seed=2)
    bucket = bucket_observations(observations, width=0.02)[0]
    assert bucket.edge > 0.0
    assert bucket.edge_lower_bound < 0.0
    assert bucket.tradeable is False


def test_uncertainty_uses_market_count_not_observation_count():
    # Fifteen candles from one market share one outcome, so they are not 15 independent samples.
    repeated = [Observation("SAME", "yes", 0.91, True, float(m)) for m in range(1, 16)]
    bucket = bucket_observations(repeated, width=0.02)[0]
    assert bucket.observations == 15
    assert bucket.markets == 1
    assert bucket.realized == 1.0
    assert bucket.realized_low < 0.5  # one market cannot support a confident rate


def test_buckets_are_split_by_price():
    observations = _observations(0.91, 1.0, 40) + _observations(0.95, 1.0, 40, seed=3)
    buckets = bucket_observations(observations, width=0.02)
    assert len(buckets) == 2
    assert buckets[0].low == pytest.approx(0.90)
    assert buckets[1].low == pytest.approx(0.94)
