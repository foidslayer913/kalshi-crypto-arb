"""Retry, throttle, and pagination behaviour of the ground-truth fetcher.

These exercise the paths that hammered Kalshi with 429s: a backoff too short to clear a rate
limit, no spacing between requests, and a cursor loop with no stop condition.
"""

import httpx
import pytest

import scripts.fetch_ground_truth as fetcher
from scripts.fetch_ground_truth import _get, _retry_after_seconds, iter_settled_markets


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Record sleeps instead of taking them, so the tests assert on timing without spending it."""
    slept: list[float] = []
    monkeypatch.setattr(fetcher.time, "sleep", slept.append)
    monkeypatch.setattr(fetcher, "_last_request_at", 0.0)
    return slept


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_returns_payload_on_success():
    with _client(lambda request: httpx.Response(200, json={"ok": True})) as client:
        assert _get(client, "https://x/markets", {}) == {"ok": True}


def test_rate_limit_backoff_is_long_and_escalates(_no_real_sleeping):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 4 else httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        assert _get(client, "https://x/markets", {}, min_interval=0) == {"ok": True}

    backoffs = [delay for delay in _no_real_sleeping if delay >= 5.0]
    assert backoffs == [5.0, 10.0, 20.0]  # not the 1s that could never clear a rate limit


def test_rate_limit_backoff_is_capped(_no_real_sleeping):
    with _client(lambda request: httpx.Response(429)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            _get(client, "https://x/markets", {}, min_interval=0, attempts=8)
    assert max(_no_real_sleeping) <= 60.0


def test_retry_after_header_wins_over_our_backoff(_no_real_sleeping):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "13"})
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        _get(client, "https://x/markets", {}, min_interval=0)
    assert 13.0 in _no_real_sleeping


def test_malformed_retry_after_falls_back_to_our_backoff():
    assert _retry_after_seconds(httpx.Response(429, headers={"retry-after": "soon"})) is None
    assert _retry_after_seconds(httpx.Response(429)) is None
    assert _retry_after_seconds(httpx.Response(429, headers={"retry-after": "7"})) == 7.0


def test_transient_5xx_uses_a_shorter_backoff_than_a_rate_limit(_no_real_sleeping):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        _get(client, "https://x/markets", {}, min_interval=0)
    assert _no_real_sleeping == [1.0, 2.0]


def test_client_errors_are_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"msg": "bad parameter"})

    with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            _get(client, "https://x/markets", {}, min_interval=0)
    assert calls["n"] == 1  # retrying a rejected parameter would just be noise


def test_throttle_spaces_requests_apart(_no_real_sleeping):
    with _client(lambda request: httpx.Response(200, json={"ok": True})) as client:
        _get(client, "https://x/markets", {}, min_interval=0.5)
        _get(client, "https://x/markets", {}, min_interval=0.5)
    assert any(0 < delay <= 0.5 for delay in _no_real_sleeping)


def _page(markets, cursor):
    return httpx.Response(200, json={"markets": markets, "cursor": cursor})


def test_pagination_follows_the_cursor_to_the_end():
    pages = [_page([{"ticker": "A"}], "c1"), _page([{"ticker": "B"}], "")]

    def handler(request):
        return pages.pop(0)

    with _client(handler) as client:
        markets = list(iter_settled_markets(client, "https://x", "KXBTCD", None, None, min_interval=0))
    assert [market["ticker"] for market in markets] == ["A", "B"]


def test_pagination_stops_when_the_cursor_repeats():
    # The failure mode that produced an endless stream of 429s: a cursor that never advances.
    with _client(lambda request: _page([{"ticker": "A"}], "stuck")) as client:
        markets = list(iter_settled_markets(client, "https://x", "KXBTCD", None, None, min_interval=0))
    assert [market["ticker"] for market in markets] == ["A", "A"]  # second page, then it bails


def test_pagination_stops_on_an_empty_page():
    with _client(lambda request: _page([], "c1")) as client:
        assert list(iter_settled_markets(client, "https://x", "KXBTCD", None, None, min_interval=0)) == []


def test_close_time_bounds_are_sent_as_integers():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return _page([], "")

    with _client(handler) as client:
        list(iter_settled_markets(client, "https://x", "KXBTCD", 1782864000, 1785369600, min_interval=0))
    assert seen["min_close_ts"] == "1782864000"
    assert seen["max_close_ts"] == "1785369600"
    assert "." not in seen["max_close_ts"]
