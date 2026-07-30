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
