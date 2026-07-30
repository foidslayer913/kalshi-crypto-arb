import json
import random

import pytest

from backtest.reaction import (
    ReactionObservation,
    bucket_by_move,
    build_observations,
    format_verdict,
    split_by_date,
)


def _write(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def _path(tmp_path, prices, result="yes", ticker="M1", close="2026-07-30T04:00:00Z"):
    """prices is oldest-first; minutes_to_close counts down so the file is written in reverse."""
    rows = []
    total = len(prices)
    for offset, (ask, bid) in enumerate(prices):
        rows.append({
            "ticker": ticker, "result": result,
            "minutes_to_close": float(total - offset),
            "yes_ask": ask, "yes_bid": bid, "close_time": close,
        })
    path = tmp_path / f"{ticker}.jsonl"
    _write(path, rows)
    return path


def test_move_is_measured_over_the_lookback_window(tmp_path):
    # Mid goes 0.95 -> 0.90 -> 0.70; over a 2-minute lookback the last point moved -0.25.
    path = _path(tmp_path, [(0.96, 0.94), (0.91, 0.89), (0.71, 0.69)])
    built = build_observations(path, lookback=2, min_minutes=0.0)
    yes = [o for o in built if o.side == "yes"]
    assert len(yes) == 1
    assert yes[0].move == pytest.approx(-0.25)
    assert yes[0].price == pytest.approx(0.71)  # the ask is what a buyer pays


def test_the_no_side_move_is_the_mirror_image(tmp_path):
    path = _path(tmp_path, [(0.96, 0.94), (0.91, 0.89), (0.71, 0.69)])
    built = build_observations(path, lookback=2, min_minutes=0.0)
    yes = [o for o in built if o.side == "yes"][0]
    no = [o for o in built if o.side == "no"][0]
    assert no.move == pytest.approx(-yes.move)
    assert no.price == pytest.approx(1.0 - 0.69)  # a NO ask is 1 minus the YES bid


def test_move_uses_the_mid_not_the_ask(tmp_path):
    # An ask of 1.0 means "no offer", not a price spike; using it would fabricate a huge move.
    path = _path(tmp_path, [(1.0, 0.50), (0.55, 0.53), (0.56, 0.54)])
    built = build_observations(path, lookback=2, min_minutes=0.0)
    yes = [o for o in built if o.side == "yes"][0]
    # mids are 0.75 and 0.55, so the move is -0.20, not the -0.44 the asks would suggest.
    assert yes.move == pytest.approx(-0.20)


def test_early_minutes_without_enough_history_are_skipped(tmp_path):
    path = _path(tmp_path, [(0.60, 0.58), (0.61, 0.59), (0.62, 0.60)])
    built = build_observations(path, lookback=2, min_minutes=0.0)
    assert {o.minutes_to_close for o in built} == {1.0}


def _synthetic(n, price, win_rate, move, date, seed, prefix="M"):
    rng = random.Random(seed)
    return [
        ReactionObservation(
            f"{prefix}{date}{i}", "yes", price, move, rng.random() < win_rate, 5.0, date, 100
        )
        for i in range(n)
    ]


def test_an_overreaction_is_detected():
    # Priced 0.71 after a big drop but really wins 85% of the time: a genuine dip to buy.
    observations = _synthetic(1200, 0.71, 0.85, -0.25, "2026-07-05", 1)
    dip = [b for b in bucket_by_move(observations) if b.high <= -0.08][0]
    assert dip.result.edge > 0.10
    assert dip.significant is True
    assert "overshoots" in format_verdict(bucket_by_move(observations))


def test_continuation_is_reported_as_a_losing_trade():
    # Priced 0.71 after a drop but only wins 55%: the move was information, not panic.
    observations = _synthetic(1200, 0.71, 0.55, -0.25, "2026-07-05", 2)
    buckets = bucket_by_move(observations)
    dip = [b for b in buckets if b.high <= -0.08][0]
    assert dip.result.edge < 0
    assert dip.result.edge_high < 0  # significantly negative
    verdict = format_verdict(buckets)
    assert "continue rather than revert" in verdict


def test_a_fairly_priced_dip_reads_as_no_effect():
    # Wins at exactly its break-even rate, so there is nothing to harvest.
    observations = _synthetic(2000, 0.71, 0.7157, -0.25, "2026-07-05", 3)
    buckets = bucket_by_move(observations)
    dip = [b for b in buckets if b.high <= -0.08][0]
    assert dip.significant is False
    assert "no overreaction" in format_verdict(buckets)


def test_buckets_separate_drops_from_rises():
    observations = (
        _synthetic(300, 0.71, 0.8, -0.25, "2026-07-05", 4, prefix="D")
        + _synthetic(300, 0.71, 0.8, +0.25, "2026-07-05", 5, prefix="U")
    )
    buckets = bucket_by_move(observations)
    lows = [b.low for b in buckets]
    assert any(low <= -0.25 for low in lows)
    assert any(low >= 0.25 for low in lows)


def test_split_by_date_separates_the_halves():
    observations = (
        _synthetic(5, 0.71, 0.8, -0.25, "2026-07-05", 6)
        + _synthetic(5, 0.71, 0.8, -0.25, "2026-07-25", 7)
    )
    train, test = split_by_date(observations, "2026-07-15")
    assert {o.close_date for o in train} == {"2026-07-05"}
    assert {o.close_date for o in test} == {"2026-07-25"}


def test_real_shaped_collapse_lands_in_the_dip_bucket(tmp_path):
    # The uploaded CSV's path: 96.35 -> 69.02 -> 71.48 in the final minutes.
    path = _path(
        tmp_path,
        [(0.9635, 0.9535), (0.6902, 0.6802), (0.7148, 0.7048)],
        result="no",
    )
    built = build_observations(path, lookback=2, min_minutes=0.0)
    yes = [o for o in built if o.side == "yes"][0]
    assert yes.move < -0.20  # a sharp drop
    assert yes.won is False  # this market settled no
    buckets = bucket_by_move(built)
    assert any(b.high <= -0.08 for b in buckets)
