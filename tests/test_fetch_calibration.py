"""Wire-format tests for the calibration fetcher, against payloads copied from live Kalshi.

Three schema assumptions have already turned out wrong in this codebase (order book field names,
`volume` vs `volume_fp`, `settlement_timer_seconds` as an averaging window). These pin the candle
and market shapes to real observed payloads so the next divergence fails a test rather than
silently producing a plausible study.
"""

import httpx
import pytest

import scripts.fetch_ground_truth as fetcher
from scripts.fetch_calibration import _dollars, _strike_of, fetch_candles, observations_for_market

# Copied verbatim from a live KXBTC15M market (a 15-minute BTC up/down contract). Note that the
# strike is `floor_strike` under strike_type "greater_or_equal", and that every numeric field
# arrives as a string.
REAL_MARKET = {
    "ticker": "KXBTC15M-26JUL290015-15",
    "event_ticker": "KXBTC15M-26JUL290015",
    "market_type": "binary",
    "strike_type": "greater_or_equal",
    "floor_strike": 63650.03,
    "custom_strike": {"round_digits": "2"},
    "settlement_timer_seconds": 1,
    "close_time": "2026-07-29T04:15:00Z",
    "open_time": "2026-07-29T04:00:00Z",
    "result": "yes",
    "expiration_value": "63653.21",
    "volume_fp": "3413404.00",
}

# The market closes at this unix second; the probe that produced the candles used it as end_ts.
CLOSE_TS = 1_785_298_500

REAL_CANDLE_FIRST = {
    "end_period_ts": 1_785_297_660,
    "open_interest_fp": "103611.47",
    "price": {
        "close_dollars": "0.4500", "high_dollars": "0.6500", "low_dollars": "0.4300",
        "mean_dollars": "0.5448", "open_dollars": "0.5300",
    },
    "volume_fp": "192854.05",
    "yes_ask": {
        "close_dollars": "0.4500", "high_dollars": "1.0000",
        "low_dollars": "0.4400", "open_dollars": "1.0000",
    },
    "yes_bid": {
        "close_dollars": "0.4400", "high_dollars": "0.6100",
        "low_dollars": "0.0010", "open_dollars": "0.0010",
    },
}

REAL_CANDLE_LAST = {
    "end_period_ts": CLOSE_TS,
    "open_interest_fp": "387540.99",
    "price": {
        "close_dollars": "0.9990", "high_dollars": "0.9990", "low_dollars": "0.3200",
        "mean_dollars": "0.8699", "open_dollars": "0.9100", "previous_dollars": "0.9110",
    },
    "volume_fp": "593424.63",
    "yes_ask": {
        "close_dollars": "1.0000", "high_dollars": "1.0000",
        "low_dollars": "0.4400", "open_dollars": "0.9130",
    },
    "yes_bid": {
        "close_dollars": "0.9990", "high_dollars": "0.9990",
        "low_dollars": "0.3200", "open_dollars": "0.9100",
    },
}


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)
    monkeypatch.setattr(fetcher, "_last_request_at", 0.0)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _candles_response(candles):
    return lambda request: httpx.Response(200, json={"candlesticks": candles})


def test_dollar_fields_are_strings_and_default_to_the_close():
    assert _dollars(REAL_CANDLE_FIRST["yes_ask"]) == pytest.approx(0.45)
    assert _dollars(REAL_CANDLE_FIRST["yes_bid"]) == pytest.approx(0.44)
    assert _dollars(REAL_CANDLE_FIRST["yes_ask"], "open_dollars") == pytest.approx(1.0)


def test_dollars_tolerates_missing_and_malformed_nodes():
    assert _dollars(None) is None
    assert _dollars({}) is None
    assert _dollars({"close_dollars": None}) is None
    assert _dollars({"close_dollars": "not a number"}) is None


def test_strike_comes_from_floor_strike_for_an_up_down_market():
    assert _strike_of(REAL_MARKET) == pytest.approx(63650.03)


def test_strike_falls_back_to_cap_strike():
    assert _strike_of({"cap_strike": 117000.0}) == pytest.approx(117000.0)
    assert _strike_of({}) is None


def test_real_payloads_produce_usable_observations():
    with _client(_candles_response([REAL_CANDLE_FIRST, REAL_CANDLE_LAST])) as client:
        rows = list(observations_for_market(client, "KXBTC15M", REAL_MARKET, 1))

    assert len(rows) == 2
    first, last = rows
    assert first["yes_ask"] == pytest.approx(0.45)
    assert first["yes_bid"] == pytest.approx(0.44)
    assert first["strike"] == pytest.approx(63650.03)
    assert first["result"] == "yes"
    assert first["expiration_value"] == pytest.approx(63653.21)
    assert first["candle_volume"] == pytest.approx(192854.05)
    # 1785297660 is 840s before the close, i.e. 14 minutes.
    assert first["minutes_to_close"] == pytest.approx(14.0)
    assert last["minutes_to_close"] == pytest.approx(0.0)


def test_the_candle_overlapping_settlement_is_kept_but_excluded_downstream(tmp_path):
    # The fetcher records the final candle; the loader is what drops it. Keeping it on disk means
    # the choice stays revisitable without re-fetching.
    from backtest.calibration import load_observations
    import json

    with _client(_candles_response([REAL_CANDLE_FIRST, REAL_CANDLE_LAST])) as client:
        rows = list(observations_for_market(client, "KXBTC15M", REAL_MARKET, 1))
    path = tmp_path / "obs.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    kept = load_observations(path, min_price=0.0)
    assert {o.minutes_to_close for o in kept} == {14.0}


def test_a_market_without_a_binary_result_is_skipped():
    with _client(_candles_response([REAL_CANDLE_FIRST])) as client:
        rows = list(
            observations_for_market(client, "KXBTC15M", {**REAL_MARKET, "result": ""}, 1)
        )
    assert rows == []


def test_a_candle_with_no_quotes_at_all_is_skipped():
    bare = {"end_period_ts": 1_785_297_660, "volume_fp": "0.00"}
    with _client(_candles_response([bare])) as client:
        assert list(observations_for_market(client, "KXBTC15M", REAL_MARKET, 1)) == []


def test_a_candle_missing_its_timestamp_is_skipped():
    with _client(_candles_response([{**REAL_CANDLE_FIRST, "end_period_ts": None}])) as client:
        assert list(observations_for_market(client, "KXBTC15M", REAL_MARKET, 1)) == []


def test_the_request_covers_the_market_lifetime():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"candlesticks": []})

    with _client(handler) as client:
        fetch_candles(client, "KXBTC15M", REAL_MARKET["ticker"], CLOSE_TS - 900, CLOSE_TS, 1)
    assert seen["period_interval"] == "1"
    assert int(seen["end_ts"]) == CLOSE_TS


def test_open_time_pads_the_start_so_the_first_candle_is_not_lost():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"candlesticks": []})

    with _client(handler) as client:
        list(observations_for_market(client, "KXBTC15M", REAL_MARKET, 1))
    # open_time is 04:00:00Z = close - 900s; the request must start at or before that.
    assert int(seen["start_ts"]) <= CLOSE_TS - 900


def test_missing_candlesticks_key_yields_nothing():
    with _client(lambda request: httpx.Response(200, json={"unexpected": []})) as client:
        assert fetch_candles(client, "KXBTC15M", "T", 0, 1, 1) == []


def test_real_observations_flow_into_the_conditional_study(tmp_path):
    # End to end on real shapes: fetched rows must join to an index series and produce a z.
    import csv
    import json

    from backtest.conditional import MinuteIndex, build_observations

    with _client(_candles_response([REAL_CANDLE_FIRST])) as client:
        rows = list(observations_for_market(client, "KXBTC15M", REAL_MARKET, 1))
    obs_path = tmp_path / "obs.jsonl"
    obs_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    index_path = tmp_path / "index.csv"
    with index_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "price"])
        # Cover the candle time with a series slightly above the strike.
        for i in range(20):
            writer.writerow([CLOSE_TS - 900 + i * 60, f"{63700.0 + i:.2f}"])

    built = build_observations(obs_path, index_path and MinuteIndex.from_csv(index_path), min_price=0.0)
    assert built, "real payloads must survive the join to the index"
    yes_leg = [o for o in built if o.side == "yes"][0]
    # Index above the strike favours YES, so z must be positive.
    assert yes_leg.z > 0
    no_leg = [o for o in built if o.side == "no"][0]
    assert no_leg.z == pytest.approx(-yes_leg.z)
