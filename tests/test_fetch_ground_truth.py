import pytest

from backtest.reconstruct import PriceSeries
from scripts.fetch_ground_truth import (
    DEFAULT_PREFIX_TO_SYMBOL,
    _parse_mapping,
    _to_unix,
    bucket_trades_to_seconds,
    convert_market,
    resolve_symbol,
)


def _raw(**overrides):
    raw = {
        "ticker": "KXBTCD-26JUL2915-T118000",
        "strike_type": "greater",
        "floor_strike": 118000.0,
        "cap_strike": None,
        "close_time": "2026-07-29T15:00:00Z",
        "result": "yes",
    }
    raw.update(overrides)
    return raw


# Trimmed from a real settled market returned by Kalshi production market data. Numeric fields
# genuinely arrive as strings, and settlement_value_dollars is the payout (0 for a "no"), which is
# exactly the field that must NOT be mistaken for the settled index level.
REAL_PAYLOAD = {
    "ticker": "KXBTCD-26JUL2907-T72299.99",
    "event_ticker": "KXBTCD-26JUL2907",
    "market_type": "binary",
    "status": "finalized",
    "strike_type": "greater",
    "floor_strike": 72299.99,
    "close_time": "2026-07-29T11:00:00Z",
    "expiration_value": "64398.56",
    "settlement_value_dollars": "0.0000",
    "settlement_timer_seconds": 60,
    "result": "no",
    "volume_fp": "0.00",
    "open_interest_fp": "0.00",
    "yes_ask_dollars": "1.0000",
    "no_bid_dollars": "0.0000",
}


def test_real_kalshi_payload_converts():
    record = convert_market(REAL_PAYLOAD, DEFAULT_PREFIX_TO_SYMBOL)
    assert record == {
        "ticker": "KXBTCD-26JUL2907-T72299.99",
        "strike_type": "greater",
        "strike_price": 72299.99,
        "close_time": "2026-07-29T11:00:00Z",
        "result": "no",
        "crypto_symbol": "BTC-USD",
        "settlement_value": 64398.56,
        "volume": 0.0,
        "open_interest": 0.0,
    }


def test_settlement_value_is_the_index_level_not_the_payout():
    # settlement_value_dollars is 0.0000 here because the market resolved no. Picking it up would
    # make every divergence number wrong while still looking like a plausible price.
    record = convert_market(REAL_PAYLOAD, DEFAULT_PREFIX_TO_SYMBOL)
    assert record["settlement_value"] == 64398.56
    assert record["settlement_value"] != 0.0


def test_daily_series_ticker_still_resolves_to_a_symbol():
    # The live series is KXBTCD, not KXBTC -- prefix matching has to tolerate the suffix.
    assert resolve_symbol("KXBTCD-26JUL2907-T72299.99", DEFAULT_PREFIX_TO_SYMBOL) == "BTC-USD"


def test_zero_volume_market_is_recorded_as_untraded():
    from backtest.reconstruct import SettledMarket, Variant, evaluate_window

    record = convert_market(REAL_PAYLOAD, DEFAULT_PREFIX_TO_SYMBOL)
    market = SettledMarket(
        ticker=record["ticker"], strike_type=record["strike_type"],
        strike_price=record["strike_price"],
        close_time=__import__("datetime").datetime.fromisoformat("2026-07-29T11:00:00+00:00"),
        result=record["result"], crypto_symbol=record["crypto_symbol"],
        settlement_value=record["settlement_value"], volume=record["volume"],
    )
    result = evaluate_window(market, [64398.56] * 60, Variant("strict"))
    assert result.traded is False


def test_convert_market_greater_uses_floor_strike():
    record = convert_market(_raw(), DEFAULT_PREFIX_TO_SYMBOL)
    assert record["strike_price"] == 118000.0
    assert record["strike_type"] == "greater"
    assert record["crypto_symbol"] == "BTC-USD"
    assert record["result"] == "yes"


def test_convert_market_less_uses_cap_strike():
    record = convert_market(
        _raw(strike_type="less", floor_strike=None, cap_strike=117000.0), DEFAULT_PREFIX_TO_SYMBOL
    )
    assert record["strike_price"] == 117000.0
    assert record["strike_type"] == "less"


def test_convert_market_normalises_result_case():
    assert convert_market(_raw(result="YES"), DEFAULT_PREFIX_TO_SYMBOL)["result"] == "yes"


@pytest.mark.parametrize("result", ["", None, "void", "scalar"])
def test_convert_market_skips_markets_without_a_binary_result(result):
    assert convert_market(_raw(result=result), DEFAULT_PREFIX_TO_SYMBOL) is None


def test_convert_market_skips_unsupported_strike_types():
    assert convert_market(_raw(strike_type="between"), DEFAULT_PREFIX_TO_SYMBOL) is None


def test_convert_market_skips_when_the_strike_is_missing():
    assert convert_market(_raw(floor_strike=None), DEFAULT_PREFIX_TO_SYMBOL) is None


def test_convert_market_skips_unmapped_tickers():
    assert convert_market(_raw(ticker="KXDOGE-XYZ"), DEFAULT_PREFIX_TO_SYMBOL) is None


def test_convert_market_omits_settlement_value_when_absent():
    assert "settlement_value" not in convert_market(_raw(), DEFAULT_PREFIX_TO_SYMBOL)


@pytest.mark.parametrize("key", ["settlement_value", "expiration_value", "settled_value"])
def test_convert_market_accepts_any_known_settlement_value_key(key):
    record = convert_market(_raw(**{key: 118250.75}), DEFAULT_PREFIX_TO_SYMBOL)
    assert record["settlement_value"] == 118250.75


def test_resolve_symbol_matches_prefix():
    assert resolve_symbol("KXETH-26JUL2915", DEFAULT_PREFIX_TO_SYMBOL) == "ETH-USD"
    assert resolve_symbol("NOPE-1", DEFAULT_PREFIX_TO_SYMBOL) is None


def test_bucket_keeps_the_last_trade_of_each_second():
    trades = [(1_000_100, 100.0), (1_000_900, 101.0), (1_001_200, 102.0)]
    assert bucket_trades_to_seconds(trades) == {1001: 101.0, 1002: 102.0}


def test_bucket_is_stamped_at_the_end_of_its_second():
    # A trade 0.9s into second 1000 must not be visible to a reader querying at 1000.0, only at
    # 1001.0 -- otherwise the series leaks up to a second of future prices into the window.
    buckets = bucket_trades_to_seconds([(1_000_900, 101.0)])
    series = PriceSeries(list(buckets.items()))
    assert series.price_at(1000.0) is None
    assert series.price_at(1001.0) == 101.0


def test_bucket_is_order_independent():
    forward = bucket_trades_to_seconds([(1_000_100, 100.0), (1_000_900, 101.0)])
    reversed_input = bucket_trades_to_seconds([(1_000_900, 101.0), (1_000_100, 100.0)])
    assert forward == reversed_input == {1001: 101.0}


def test_bucket_handles_no_trades():
    assert bucket_trades_to_seconds([]) == {}


def test_bucketed_output_feeds_the_backtest_price_series():
    # Close the loop: what the fetcher writes must be directly consumable by Tier 2.
    close_ts = 2_000_000
    trades = [((close_ts - 60 + i) * 1000 + 500, 118000.0 + i) for i in range(60)]
    series = PriceSeries(list(bucket_trades_to_seconds(trades).items()))
    ticks = series.window_ticks(close_ts)
    assert len(ticks) == 60
    assert ticks.count(None) == 0
    assert ticks[-1] == pytest.approx(118059.0)


def test_gaps_in_trading_become_missing_ticks():
    close_ts = 2_000_000
    # Only the first 20 seconds of the window have trades.
    trades = [((close_ts - 60 + i) * 1000 + 500, 118000.0) for i in range(20)]
    series = PriceSeries(list(bucket_trades_to_seconds(trades).items()))
    ticks = series.window_ticks(close_ts)
    assert ticks.count(None) > 0
    assert ticks[0] is not None


def test_to_unix_accepts_dates_and_zulu_timestamps():
    assert _to_unix("2026-07-29T15:00:00Z") == _to_unix("2026-07-29T15:00:00+00:00")
    assert _to_unix("2026-07-29") == _to_unix("2026-07-29T00:00:00Z")


def test_parse_mapping_defaults_and_overrides():
    assert _parse_mapping(None, DEFAULT_PREFIX_TO_SYMBOL) == DEFAULT_PREFIX_TO_SYMBOL
    assert _parse_mapping(["KXSOL=SOL-USD"], DEFAULT_PREFIX_TO_SYMBOL) == {"KXSOL": "SOL-USD"}


def test_parse_mapping_rejects_malformed_entries():
    with pytest.raises(SystemExit):
        _parse_mapping(["KXSOL"], DEFAULT_PREFIX_TO_SYMBOL)
