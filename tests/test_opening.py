import json
import random

import pytest

from backtest.opening import (
    observations_below,
    opening_quotes,
    split_by_date,
)


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _market(ticker, result, open_ask, open_bid, close="2026-07-30T04:15:00Z", n=15):
    """A market's candles; only the first (mtc=n-1) carries the opening quote of interest."""
    rows = []
    for minute in range(1, n):
        mtc = float(n - minute)
        is_open = minute == 1
        rows.append({
            "ticker": ticker, "result": result, "minutes_to_close": mtc,
            "close_time": close,
            "yes_ask_open": open_ask if is_open else 0.55,
            "yes_bid_open": open_bid if is_open else 0.53,
        })
    return rows


def test_opening_quote_is_the_first_candle(tmp_path):
    path = tmp_path / "o.jsonl"
    _write(path, _market("M1", "yes", 0.45, 0.43))
    quotes = opening_quotes(path)
    assert len(quotes) == 1
    assert quotes[0].yes_ask_open == pytest.approx(0.45)
    assert quotes[0].minutes_to_close == 14.0  # the candle furthest from close


def test_a_cheap_yes_open_becomes_a_yes_buy(tmp_path):
    path = tmp_path / "o.jsonl"
    _write(path, _market("M1", "yes", 0.45, 0.43))
    obs = observations_below(opening_quotes(path), threshold=0.46)
    yes = [o for o in obs if o.side == "yes"]
    assert len(yes) == 1
    assert yes[0].price == pytest.approx(0.45)
    assert yes[0].won is True


def test_a_cheap_no_open_is_captured_from_the_yes_bid(tmp_path):
    # yes_bid_open 0.60 -> no ask 0.40, which is below 0.46 and should be bought as NO.
    path = tmp_path / "o.jsonl"
    _write(path, _market("M1", "no", open_ask=0.62, open_bid=0.60))
    obs = observations_below(opening_quotes(path), threshold=0.46)
    no = [o for o in obs if o.side == "no"]
    assert len(no) == 1
    assert no[0].price == pytest.approx(0.40)
    assert no[0].won is True


def test_quotes_at_or_above_threshold_are_excluded(tmp_path):
    path = tmp_path / "o.jsonl"
    _write(path, _market("M1", "yes", 0.48, 0.46))
    assert observations_below(opening_quotes(path), threshold=0.46) == []


def _cheap_opens(n, win_rate, ask, date, seed):
    """n markets that all open with yes ask == ask, winning at win_rate."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        result = "yes" if rng.random() < win_rate else "no"
        rows.append({
            "ticker": f"{date}-M{i}", "result": result, "minutes_to_close": 14.0,
            "close_time": f"{date}T04:15:00Z", "yes_ask_open": ask, "yes_bid_open": ask - 0.02,
        })
    return rows


def test_a_genuinely_underpriced_open_shows_a_positive_edge(tmp_path):
    # Opens at 0.45 but wins 55% of the time: a real ~6c edge after the ~1.7c fee.
    path = tmp_path / "o.jsonl"
    _write(path, _cheap_opens(2000, 0.55, 0.45, "2026-07-05", 1))
    from backtest.calibration import pool
    result = pool(observations_below(opening_quotes(path), 0.46), "x")
    assert result.edge > 0.05
    assert result.significant is True


def test_a_fairly_priced_cheap_open_shows_no_edge(tmp_path):
    # Opens at 0.45 and wins 45% of the time: correctly priced, nothing to harvest.
    path = tmp_path / "o.jsonl"
    _write(path, _cheap_opens(1500, 0.45, 0.45, "2026-07-05", 2))
    from backtest.calibration import pool
    result = pool(observations_below(opening_quotes(path), 0.46), "x")
    assert result.significant is False


def test_split_by_date_partitions_quotes(tmp_path):
    path = tmp_path / "o.jsonl"
    _write(path, _cheap_opens(4, 0.5, 0.45, "2026-07-05", 3) + _cheap_opens(4, 0.5, 0.45, "2026-07-25", 4))
    train, test = split_by_date(opening_quotes(path), "2026-07-15")
    assert {q.close_date for q in train} == {"2026-07-05"}
    assert {q.close_date for q in test} == {"2026-07-25"}


def test_missing_opening_fields_are_tolerated(tmp_path):
    # Data fetched before the open fields existed: no crash, just nothing to score.
    path = tmp_path / "o.jsonl"
    _write(path, [{"ticker": "M1", "result": "yes", "minutes_to_close": 14.0,
                   "close_time": "2026-07-30T04:15:00Z"}])
    quotes = opening_quotes(path)
    assert quotes[0].yes_ask_open is None
    assert observations_below(quotes, 0.46) == []
