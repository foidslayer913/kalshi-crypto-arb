import pytest

from ingestion.order_book import OrderBookStore


def _snapshot(ticker, yes, no):
    return {"type": "orderbook_snapshot", "msg": {"market_ticker": ticker, "yes": yes, "no": no}}


def _delta(ticker, side, price, delta):
    return {
        "type": "orderbook_delta",
        "msg": {"market_ticker": ticker, "side": side, "price": price, "delta": delta},
    }


def test_snapshot_sets_best_bids():
    store = OrderBookStore()
    store.apply(_snapshot("T1", [[45, 100], [40, 50]], [[52, 80]]))
    assert store.best_bid_cents("T1", "yes") == 45
    assert store.best_bid_cents("T1", "no") == 52


def test_implied_ask_derived_from_opposite_best_bid():
    store = OrderBookStore()
    store.apply(_snapshot("T1", [[45, 100]], [[52, 80]]))
    assert store.implied_ask_dollars("T1", "yes") == pytest.approx(0.48)
    assert store.implied_ask_dollars("T1", "no") == pytest.approx(0.55)


def test_ask_depth_matches_opposite_level_quantity():
    store = OrderBookStore()
    store.apply(_snapshot("T1", [[45, 100]], [[52, 80]]))
    assert store.ask_depth("T1", "yes") == 80
    assert store.ask_depth("T1", "no") == 100


def test_delta_adds_and_removes_price_levels():
    store = OrderBookStore()
    store.apply(_snapshot("T1", [[45, 100]], []))
    store.apply(_delta("T1", "yes", 46, 20))
    assert store.best_bid_cents("T1", "yes") == 46
    store.apply(_delta("T1", "yes", 46, -20))
    assert store.best_bid_cents("T1", "yes") == 45


def test_delta_on_unknown_ticker_creates_book():
    store = OrderBookStore()
    store.apply(_delta("NEW", "no", 30, 10))
    assert store.best_bid_cents("NEW", "no") == 30


def test_unknown_ticker_returns_none_or_zero():
    store = OrderBookStore()
    assert store.best_bid_cents("UNKNOWN", "yes") is None
    assert store.implied_ask_dollars("UNKNOWN", "yes") is None
    assert store.ask_depth("UNKNOWN", "yes") == 0


def test_message_without_market_ticker_is_ignored():
    store = OrderBookStore()
    store.apply({"type": "orderbook_snapshot", "msg": {}})
    assert store.best_bid_cents("anything", "yes") is None
