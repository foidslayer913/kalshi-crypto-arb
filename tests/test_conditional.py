import csv
import json
import math
import random

import pytest

from backtest.conditional import (
    ConditionalObservation,
    FairModel,
    MinuteIndex,
    build_observations,
    evaluate,
    mean_absolute_error,
    split_by_date,
)

MINUTE = 60


def _index_csv(tmp_path, start_ts=1_785_000_000, minutes=600, sigma=0.001, seed=5):
    rng = random.Random(seed)
    price = 64000.0
    rows = []
    for i in range(minutes):
        price *= math.exp(rng.gauss(0, sigma))
        rows.append((start_ts + i * MINUTE, price))
    path = tmp_path / "index.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "price"])
        for timestamp, value in rows:
            writer.writerow([timestamp, f"{value:.2f}"])
    return path, rows


def test_index_lookup_never_returns_a_future_price(tmp_path):
    path, rows = _index_csv(tmp_path, minutes=10)
    index = MinuteIndex.from_csv(path)
    first_ts, first_price = rows[0]
    assert index.price_at(first_ts) == pytest.approx(first_price, abs=0.01)
    assert index.price_at(first_ts - 1) is None  # nothing known before the series starts


def test_stale_index_lookup_returns_none(tmp_path):
    path, rows = _index_csv(tmp_path, minutes=3)
    index = MinuteIndex.from_csv(path)
    last_ts, _ = rows[-1]
    assert index.price_at(last_ts + 30) is not None
    assert index.price_at(last_ts + 600) is None  # a gap must not answer with a stale price


def test_sigma_is_measured_from_consecutive_minutes_only(tmp_path):
    # A gap should not be read as one enormous return.
    path = tmp_path / "gappy.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "price"])
        for timestamp, price in [(0, 100.0), (60, 101.0), (120, 100.5), (100_000, 5000.0)]:
            writer.writerow([timestamp, price])
    sigma = MinuteIndex.from_csv(path).sigma_per_minute()
    assert 0.0 < sigma < 0.05  # the 50x jump across the gap is excluded


def _observations_file(tmp_path, index_rows, strike_offset_pct, result, count=200, start=0):
    """Write candle rows whose index distance from strike is controlled."""
    path = tmp_path / f"obs_{start}.jsonl"
    lines = []
    for i in range(count):
        # Each market closes at a distinct minute so tickers stay distinct.
        close_ts, spot = index_rows[start + i + 15]
        strike = spot * (1 + strike_offset_pct)
        close_iso = __import__("datetime").datetime.fromtimestamp(
            close_ts, tz=__import__("datetime").timezone.utc
        ).isoformat().replace("+00:00", "Z")
        for minute in range(1, 6):
            lines.append({
                "ticker": f"M{start + i}", "result": result,
                "minutes_to_close": float(minute),
                "yes_ask": 0.60, "yes_bid": 0.59,
                "strike": strike, "close_time": close_iso,
            })
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def test_z_is_signed_in_favour_of_the_side_bought(tmp_path):
    index_path, rows = _index_csv(tmp_path, minutes=600)
    index = MinuteIndex.from_csv(index_path)
    # Strike 1% ABOVE spot: YES (index >= strike) is behind, so z_yes < 0 and z_no > 0.
    obs_path = _observations_file(tmp_path, rows, +0.01, "no", count=5)
    built = build_observations(obs_path, index, min_price=0.0)
    by_side = {}
    for observation in built:
        by_side.setdefault(observation.side, []).append(observation.z)
    assert max(by_side["yes"]) < 0
    assert min(by_side["no"]) > 0
    assert by_side["yes"][0] == pytest.approx(-by_side["no"][0])


def test_z_shrinks_as_time_runs_out(tmp_path):
    # The same price gap is a bigger hurdle with less time, so |z| must grow as minutes fall.
    index_path, rows = _index_csv(tmp_path, minutes=600)
    index = MinuteIndex.from_csv(index_path)
    obs_path = _observations_file(tmp_path, rows, +0.01, "no", count=1)
    built = [o for o in build_observations(obs_path, index, min_price=0.0) if o.side == "yes"]
    by_minute = {o.minutes_to_close: o.z for o in built}
    assert by_minute[1.0] < by_minute[5.0]  # more negative with one minute left


def _synthetic(n, price, win_rate, z, date, seed, contracts=100):
    rng = random.Random(seed)
    return [
        ConditionalObservation(
            f"{date}-M{i}", "yes", price, rng.random() < win_rate, 5.0, z, date, contracts
        )
        for i in range(n)
    ]


def test_model_learns_that_higher_z_wins_more():
    train = (
        _synthetic(300, 0.60, 0.30, -1.0, "2026-07-01", 1)
        + _synthetic(300, 0.60, 0.60, 0.0, "2026-07-01", 2)
        + _synthetic(300, 0.60, 0.90, 1.0, "2026-07-01", 3)
    )
    model = FairModel(width=0.5, min_samples=40).fit(train)
    assert model.probability(-1.0) < model.probability(0.0) < model.probability(1.0)


def test_model_refuses_to_score_a_thinly_sampled_z():
    train = _synthetic(300, 0.60, 0.8, 0.0, "2026-07-01", 4)
    model = FairModel(width=0.5, min_samples=40).fit(train)
    assert model.probability(0.0) is not None
    assert model.probability(9.0) is None  # no support out there, so no opinion


def test_a_real_conditional_edge_survives_the_held_out_split():
    # The market always asks 0.60, but at high z the side truly wins 90% of the time. A selective
    # strategy should show a large held-out edge where the model disagrees with the price.
    train = (
        _synthetic(400, 0.60, 0.90, 1.2, "2026-07-01", 11)
        + _synthetic(400, 0.60, 0.30, -1.2, "2026-07-01", 12)
    )
    test = (
        _synthetic(400, 0.60, 0.90, 1.2, "2026-07-20", 13)
        + _synthetic(400, 0.60, 0.30, -1.2, "2026-07-20", 14)
    )
    model = FairModel(width=0.5, min_samples=40).fit(train)
    buckets = evaluate(model, test)
    positive = [b for b in buckets if b.low >= 0]
    assert positive, "expected at least one positive-divergence bucket"
    best = max(positive, key=lambda b: b.result.edge)
    assert best.result.edge > 0.15
    assert best.significant is True


def test_a_market_that_already_prices_the_index_yields_nothing():
    # Price equals the true probability at every z, so no divergence bucket should be profitable.
    def group(z, probability, date, seed):
        rng = random.Random(seed)
        return [
            ConditionalObservation(
                f"{date}-M{i}-{z}", "yes", probability - 0.007,
                rng.random() < probability, 5.0, z, date, 100,
            )
            for i in range(500)
        ]

    train = group(-1.0, 0.30, "2026-07-01", 21) + group(1.0, 0.90, "2026-07-01", 22)
    test = group(-1.0, 0.30, "2026-07-20", 23) + group(1.0, 0.90, "2026-07-20", 24)
    model = FairModel(width=0.5, min_samples=40).fit(train)
    buckets = evaluate(model, test)
    assert not [b for b in buckets if b.significant and b.low >= 0]


def test_split_by_date_puts_later_markets_in_test():
    observations = (
        _synthetic(5, 0.6, 0.5, 0.0, "2026-07-10", 31)
        + _synthetic(5, 0.6, 0.5, 0.0, "2026-07-20", 32)
    )
    train, test = split_by_date(observations, "2026-07-15")
    assert {o.close_date for o in train} == {"2026-07-10"}
    assert {o.close_date for o in test} == {"2026-07-20"}


def _index_with_trend(tmp_path, drift_per_min, minutes=400, start_ts=1_785_000_000, sigma=0.0005, seed=7):
    """An index with a deliberate drift, so velocity genuinely predicts where it ends up."""
    rng = random.Random(seed)
    price = 64000.0
    rows = []
    for i in range(minutes):
        price *= math.exp(drift_per_min + rng.gauss(0, sigma))
        rows.append((start_ts + i * MINUTE, price))
    path = tmp_path / f"trend_{drift_per_min}.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "price"])
        for timestamp, value in rows:
            writer.writerow([timestamp, f"{value:.2f}"])
    return path, rows


def test_slope_is_measured_and_signed_per_side(tmp_path):
    path, rows = _index_with_trend(tmp_path, drift_per_min=0.0005)
    index = MinuteIndex.from_csv(path)
    obs_path = _observations_file(tmp_path, rows, +0.0, "yes", count=3)
    built = build_observations(obs_path, index, min_price=0.0, slope_lookback=3)
    yes = [o for o in built if o.side == "yes"]
    no = [o for o in built if o.side == "no"]
    assert all(o.slope > 0 for o in yes)      # index is trending up, favouring YES
    assert all(o.slope < 0 for o in no)
    assert yes[0].slope == pytest.approx(-no[0].slope)


def test_slope_defaults_to_zero_when_lookback_disabled(tmp_path):
    path, rows = _index_with_trend(tmp_path, drift_per_min=0.0005)
    index = MinuteIndex.from_csv(path)
    obs_path = _observations_file(tmp_path, rows, +0.0, "yes", count=3)
    built = build_observations(obs_path, index, min_price=0.0, slope_lookback=0)
    assert all(o.slope == 0.0 for o in built)


def test_observation_is_dropped_when_slope_history_is_missing(tmp_path):
    # No index far enough back to measure velocity: better to drop than to assume flat.
    path, rows = _index_with_trend(tmp_path, drift_per_min=0.0)
    index = MinuteIndex.from_csv(path)
    obs_path = _observations_file(tmp_path, rows, +0.0, "yes", count=2, start=0)
    without = build_observations(obs_path, index, min_price=0.0, slope_lookback=0)
    with_huge = build_observations(obs_path, index, min_price=0.0, slope_lookback=10_000)
    assert without
    assert with_huge == []


def _slope_obs(n, price, z, slope, win_rate, date, seed):
    rng = random.Random(seed)
    return [
        ConditionalObservation(
            f"{date}-{slope}-M{i}", "yes", price, rng.random() < win_rate, 5.0, z, date, 100, slope
        )
        for i in range(n)
    ]


def test_two_dimensional_model_separates_cells_by_slope():
    # Same z, opposite slopes, very different outcomes: only a 2-D model can express this.
    train = (
        _slope_obs(400, 0.50, 0.0, +1.5, 0.85, "2026-07-01", 41)
        + _slope_obs(400, 0.50, 0.0, -1.5, 0.15, "2026-07-01", 42)
    )
    flat = FairModel(width=0.5, min_samples=40).fit(train)
    two_d = FairModel(width=0.5, min_samples=40, slope_width=1.0).fit(train)

    # The level-only model must give one answer regardless of slope.
    assert flat.probability(0.0, +1.5) == flat.probability(0.0, -1.5)
    # The 2-D model must distinguish them.
    assert two_d.probability(0.0, +1.5) > 0.7
    assert two_d.probability(0.0, -1.5) < 0.3


def test_mean_absolute_error_shows_slope_helping_when_it_carries_information():
    train = (
        _slope_obs(400, 0.50, 0.0, +1.5, 0.85, "2026-07-01", 43)
        + _slope_obs(400, 0.50, 0.0, -1.5, 0.15, "2026-07-01", 44)
    )
    test = (
        _slope_obs(400, 0.50, 0.0, +1.5, 0.85, "2026-07-20", 45)
        + _slope_obs(400, 0.50, 0.0, -1.5, 0.15, "2026-07-20", 46)
    )
    flat_mae, market_mae, _ = mean_absolute_error(FairModel(width=0.5, min_samples=40).fit(train), test)
    slope_mae, _, _ = mean_absolute_error(
        FairModel(width=0.5, min_samples=40, slope_width=1.0).fit(train), test
    )
    assert slope_mae < flat_mae - 0.05  # velocity is genuinely informative here
    assert slope_mae < market_mae      # and the price ignores it, so the model wins


def test_mean_absolute_error_shows_the_market_winning_when_the_price_already_knows():
    # Slope predicts the outcome AND the price already reflects it: the model improves but cannot
    # beat the market. This is the case that must not be mistaken for an edge.
    def group(slope, probability, date, seed):
        rng = random.Random(seed)
        return [
            ConditionalObservation(
                f"{date}-{slope}-M{i}", "yes", probability, rng.random() < probability,
                5.0, 0.0, date, 100, slope,
            )
            for i in range(500)
        ]

    train = group(+1.5, 0.85, "2026-07-01", 47) + group(-1.5, 0.15, "2026-07-01", 48)
    test = group(+1.5, 0.85, "2026-07-20", 49) + group(-1.5, 0.15, "2026-07-20", 50)
    model = FairModel(width=0.5, min_samples=40, slope_width=1.0).fit(train)
    model_mae, market_mae, _ = mean_absolute_error(model, test)
    assert market_mae <= model_mae + 0.01  # the price is at least as good as the model


def test_monotonicity_check_is_not_fooled_by_thin_tail_noise():
    # A well-behaved coordinate whose extreme tail cells are noisy must still pass. The pairwise
    # version of this check fired on exactly this shape and told the user to distrust good output.
    from backtest.conditional import format_model

    train = (
        _synthetic(90, 0.5, 0.135, -2.4, "2026-07-01", 61)   # thin, noisy tail
        + _synthetic(215, 0.5, 0.070, -2.1, "2026-07-01", 62)  # thin, out of order vs above
        + _synthetic(2000, 0.5, 0.30, -1.0, "2026-07-01", 63)
        + _synthetic(3000, 0.5, 0.50, 0.0, "2026-07-01", 64)
        + _synthetic(2000, 0.5, 0.75, 1.0, "2026-07-01", 65)
        + _synthetic(500, 0.5, 0.93, 2.0, "2026-07-01", 66)
    )
    output = format_model(FairModel(width=0.25, min_samples=40).fit(train))
    assert "rises with z as it should" in output
    assert "WARNING" not in output


def test_monotonicity_check_still_warns_on_a_useless_coordinate():
    from backtest.conditional import format_model

    train = (
        _synthetic(2000, 0.5, 0.50, -1.5, "2026-07-01", 71)
        + _synthetic(2000, 0.5, 0.51, -0.5, "2026-07-01", 72)
        + _synthetic(2000, 0.5, 0.49, 0.5, "2026-07-01", 73)
        + _synthetic(2000, 0.5, 0.50, 1.5, "2026-07-01", 74)
    )
    output = format_model(FairModel(width=0.25, min_samples=40).fit(train))
    assert "WARNING" in output
