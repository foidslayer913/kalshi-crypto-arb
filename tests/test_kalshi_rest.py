from datetime import timezone

import pytest

from ingestion.kalshi_rest import _parse_market


def test_parse_market_greater_strike_uses_floor():
    market = {
        "ticker": "KXBTC-24JUL2915-T50000",
        "strike_type": "greater",
        "floor_strike": 50000.0,
        "cap_strike": None,
        "close_time": "2024-07-29T15:00:00Z",
    }
    info = _parse_market(market)
    assert info.ticker == "KXBTC-24JUL2915-T50000"
    assert info.strike_type == "greater"
    assert info.strike_price == 50000.0
    assert info.close_time.tzinfo == timezone.utc


def test_parse_market_less_strike_uses_cap():
    market = {
        "ticker": "KXBTC-24JUL2915-T50000",
        "strike_type": "less",
        "floor_strike": None,
        "cap_strike": 50000.0,
        "close_time": "2024-07-29T15:00:00Z",
    }
    info = _parse_market(market)
    assert info.strike_type == "less"
    assert info.strike_price == 50000.0


def test_parse_market_rejects_unsupported_strike_type():
    market = {"ticker": "T", "strike_type": "between", "close_time": "2024-07-29T15:00:00Z"}
    with pytest.raises(ValueError):
        _parse_market(market)
