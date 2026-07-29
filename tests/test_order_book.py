import pytest

from ingestion.order_book import OrderBookStore


# Message shapes match the live Kalshi feed: prices are dollar strings, sizes fixed-point strings.
def _levels(pairs):
    return [[f"{price / 100:.4f}", f"{qty:.2f}"] for price, qty in pairs]


def _snapshot(ticker, yes, no):
    return {
        "type": "orderbook_snapshot",
        "msg": {"market_ticker": ticker, "yes_dollars_fp": _levels(yes), "no_dollars_fp": _levels(no)},
    }


def _delta(ticker, side, price, delta):
    return {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": ticker,
            "side": side,
            "price_dollars": f"{price / 100:.4f}",
            "delta_fp": f"{delta:.2f}",
        },
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


def test_parses_real_live_payloads_verbatim():
    # Copied from a live capture: dollar-string prices, fixed-point-string sizes, no `no` side.
    store = OrderBookStore()
    store.apply(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "KXBTCD-26JUL2918-T57399.99",
                "yes_dollars_fp": [["0.0100", "1344.00"], ["0.9900", "82646.00"]],
            },
        }
    )
    assert store.best_bid_cents("KXBTCD-26JUL2918-T57399.99", "yes") == 99
    assert store.ask_depth("KXBTCD-26JUL2918-T57399.99", "no") == 82646.0

    store.apply(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 151,
            "msg": {
                "market_ticker": "KXBTCD-26JUL2918-T57399.99",
                "price_dollars": "0.2100",
                "delta_fp": "250.00",
                "side": "no",
                "ts": "2026-07-29T21:52:51.034492Z",
                "ts_ms": 1785361971034,
            },
        }
    )
    assert store.best_bid_cents("KXBTCD-26JUL2918-T57399.99", "no") == 21


def test_subscribed_confirmation_is_ignored():
    store = OrderBookStore()
    store.apply({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}})
    assert store.best_bid_cents("anything", "yes") is None
