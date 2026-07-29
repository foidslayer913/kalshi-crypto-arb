from datetime import datetime, timezone

import pytest

from backtest.reconstruct import (
    PriceSeries,
    SettledMarket,
    Variant,
    evaluate_window,
    load_price_series,
    load_settled_markets,
    reconstruct,
    summarize,
)

CLOSE = datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc)
CLOSE_TS = CLOSE.timestamp()


def _market(strike_price=100.0, strike_type="greater", result="yes", settlement_value=None):
    return SettledMarket(
        ticker="KXBTC-TEST", strike_type=strike_type, strike_price=strike_price,
        close_time=CLOSE, result=result, crypto_symbol="BTC-USD",
        settlement_value=settlement_value,
    )


def _flat_series(price=120.0, seconds=120):
    return PriceSeries([(CLOSE_TS - seconds + i, price) for i in range(seconds + 1)])


def _undecided_ticks(price=99.5, known=30, window_size=60):
    """A window that never decides: the feed cuts out halfway, so the strict floor stays far
    below the strike and the infinite cap keeps the ceiling from closing either.
    """
    return [price] * known + [None] * (window_size - known)


def test_window_ticks_returns_one_tick_per_second():
    ticks = _flat_series().window_ticks(CLOSE_TS)
    assert len(ticks) == 60
    assert all(tick == 120.0 for tick in ticks)


def test_window_ticks_marks_gaps_as_none():
    # Points only every 10s, so most one-second slots have nothing fresh enough.
    series = PriceSeries([(CLOSE_TS - 60 + i * 10, 120.0) for i in range(7)])
    ticks = series.window_ticks(CLOSE_TS)
    assert ticks.count(None) > 0
    assert any(tick is not None for tick in ticks)


def test_price_at_respects_staleness():
    series = PriceSeries([(CLOSE_TS - 100, 120.0)])
    assert series.price_at(CLOSE_TS - 100) == 120.0
    assert series.price_at(CLOSE_TS) is None


def test_price_at_before_series_start_returns_none():
    assert PriceSeries([(CLOSE_TS, 120.0)]).price_at(CLOSE_TS - 500) is None


def test_strict_variant_fires_only_at_the_very_end():
    # Price 20% above strike: 60/k > 1.2 means the strict floor only clears at k = 51+.
    market = _market(strike_price=100.0)
    ticks = _flat_series(price=120.0).window_ticks(CLOSE_TS)
    result = evaluate_window(market, ticks, Variant("strict"))
    assert result.fired is True
    assert result.fire_second == 51
    assert result.predicted == "yes"


def test_strict_variant_is_not_actionable_when_price_barely_above_strike():
    # Only 0.5% above strike, so the strict floor (which needs +1.69% at k=59) does not clear
    # until the whole window is known at k=60 — by which point there is nothing left to trade.
    market = _market(strike_price=100.0)
    ticks = _flat_series(price=100.5).window_ticks(CLOSE_TS)
    result = evaluate_window(market, ticks, Variant("strict"))
    assert result.fire_second == 60
    assert result.fired is True
    assert result.actionable is False


def test_strict_variant_never_fires_on_an_undecided_window():
    market = _market(strike_price=100.0, result="no")
    result = evaluate_window(market, _undecided_ticks(), Variant("strict"))
    assert result.fired is False
    assert result.fire_second is None
    assert result.predicted is None


def test_complete_window_always_decides_even_without_an_actionable_signal():
    # With every tick known the average is fully determined, so the bounds necessarily close --
    # at second 60, which is why `actionable` rather than `fired` is the opportunity metric.
    market = _market(strike_price=100.0, result="no")
    ticks = _flat_series(price=99.5).window_ticks(CLOSE_TS)
    result = evaluate_window(market, ticks, Variant("strict"))
    assert result.fire_second == 60
    assert result.predicted == "no"
    assert result.actionable is False


def test_relaxed_variant_fires_far_earlier_than_strict():
    market = _market(strike_price=100.0)
    ticks = _flat_series(price=120.0).window_ticks(CLOSE_TS)
    strict = evaluate_window(market, ticks, Variant("strict"))
    relaxed = evaluate_window(market, ticks, Variant("relaxed-1%", 0.01))
    assert relaxed.fired is True
    assert relaxed.fire_second < strict.fire_second


def test_false_positive_detected_when_signal_contradicts_settlement():
    # Signal says yes (price far above strike) but the market actually settled no — exactly the
    # proxy-divergence failure this tier exists to count.
    market = _market(strike_price=100.0, result="no")
    ticks = _flat_series(price=120.0).window_ticks(CLOSE_TS)
    result = evaluate_window(market, ticks, Variant("strict"))
    assert result.fired is True
    assert result.false_positive is True
    assert result.correct is False


def test_no_false_positive_when_signal_matches_settlement():
    market = _market(strike_price=100.0, result="yes")
    ticks = _flat_series(price=120.0).window_ticks(CLOSE_TS)
    result = evaluate_window(market, ticks, Variant("strict"))
    assert result.false_positive is False
    assert result.correct is True


def test_unfired_signal_is_never_a_false_positive():
    market = _market(strike_price=100.0, result="no")
    result = evaluate_window(market, _undecided_ticks(), Variant("strict"))
    assert result.fired is False
    assert result.false_positive is False
    assert result.correct is None


def test_fire_at_final_second_is_excluded_from_actionable_signals():
    # Firing only once the window is complete restates the settled result; it must not be
    # counted as an opportunity, in either direction.
    market = _market(strike_price=100.0, result="no")
    ticks = _flat_series(price=100.5).window_ticks(CLOSE_TS)
    summary = summarize([evaluate_window(market, ticks, Variant("strict"))])[0]
    assert summary.fired == 1
    assert summary.actionable == 0
    assert summary.false_positives == 0
    assert summary.false_positive_rate == 0.0


def test_less_market_predicts_yes_when_guaranteed_below():
    # A "less" market's YES pays out below the cap, so a ceiling under the strike predicts yes.
    market = _market(strike_price=200.0, strike_type="less", result="yes")
    ticks = _flat_series(price=100.0).window_ticks(CLOSE_TS)
    result = evaluate_window(market, ticks, Variant("relaxed-1%", 0.01))
    assert result.fired is True
    assert result.predicted == "yes"
    assert result.false_positive is False


def test_greater_market_predicts_no_when_guaranteed_below():
    market = _market(strike_price=200.0, strike_type="greater", result="no")
    ticks = _flat_series(price=100.0).window_ticks(CLOSE_TS)
    result = evaluate_window(market, ticks, Variant("relaxed-1%", 0.01))
    assert result.fired is True
    assert result.predicted == "no"
    assert result.false_positive is False


def test_proxy_divergence_measured_against_settlement_value():
    market = _market(strike_price=100.0, settlement_value=121.5)
    ticks = _flat_series(price=120.0).window_ticks(CLOSE_TS)
    result = evaluate_window(market, ticks, Variant("strict"))
    assert result.reconstructed_average == pytest.approx(120.0)
    assert result.proxy_divergence == pytest.approx(1.5)


def test_proxy_divergence_is_none_without_settlement_value():
    market = _market(strike_price=100.0)
    ticks = _flat_series(price=120.0).window_ticks(CLOSE_TS)
    assert evaluate_window(market, ticks, Variant("strict")).proxy_divergence is None


def test_reconstruct_skips_markets_without_a_series():
    markets = [_market()]
    assert reconstruct(markets, {"ETH-USD": _flat_series()}) == []


def test_reconstruct_scores_every_market_variant_pair():
    markets = [_market()]
    variants = (Variant("strict"), Variant("relaxed-1%", 0.01))
    results = reconstruct(markets, {"BTC-USD": _flat_series()}, variants)
    assert len(results) == 2
    assert {result.variant for result in results} == {"strict", "relaxed-1%"}


def test_summarize_reports_fire_and_false_positive_rates():
    ticks = _flat_series(price=120.0).window_ticks(CLOSE_TS)
    variant = Variant("strict")
    results = [
        evaluate_window(_market(result="yes"), ticks, variant),
        evaluate_window(_market(result="no"), ticks, variant),
    ]
    summary = summarize(results)[0]
    assert summary.markets == 2
    assert summary.actionable == 2
    assert summary.actionable_rate == pytest.approx(1.0)
    assert summary.false_positives == 1
    assert summary.false_positive_rate == pytest.approx(0.5)
    assert summary.median_fire_second == 51


def test_summarize_handles_no_fired_signals():
    results = [evaluate_window(_market(), _undecided_ticks(), Variant("strict"))]
    summary = summarize(results)[0]
    assert summary.fired == 0
    assert summary.actionable == 0
    assert summary.false_positive_rate == 0.0
    assert summary.median_fire_second is None


def test_load_settled_markets_from_jsonl(tmp_path):
    path = tmp_path / "markets.jsonl"
    path.write_text(
        '{"ticker":"KXBTC-A","strike_type":"greater","strike_price":100.0,'
        '"close_time":"2026-07-29T15:00:00Z","result":"yes","crypto_symbol":"BTC-USD",'
        '"settlement_value":101.5}\n'
    )
    markets = load_settled_markets(path)
    assert len(markets) == 1
    assert markets[0].ticker == "KXBTC-A"
    assert markets[0].settlement_value == 101.5
    assert markets[0].close_time.tzinfo == timezone.utc


def test_load_price_series_from_csv(tmp_path):
    path = tmp_path / "series.csv"
    path.write_text("timestamp,price\n1000,100.0\n1001,101.0\n")
    series = load_price_series(path)
    assert len(series) == 2
    assert series.price_at(1001) == 101.0


def test_load_price_series_from_jsonl(tmp_path):
    path = tmp_path / "series.jsonl"
    path.write_text('{"timestamp":1000,"price":100.0}\n{"timestamp":1001,"price":101.0}\n')
    series = load_price_series(path)
    assert len(series) == 2
    assert series.price_at(1000) == 100.0
