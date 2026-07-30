"""Wire-format tests for the 1-minute index fetcher.

Binance returns klines as positional arrays, so a wrong index silently yields plausible-looking
prices at the wrong times — and the conditional study's whole `z` coordinate is built on this
series. These pin the field positions and the timestamp convention against a mock transport.
"""

import csv

import httpx
import pytest

import scripts.fetch_ground_truth as fetcher
from scripts.fetch_index_minutes import fetch_minute_closes

# Real kline shape: [openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, ...]
OPEN_MS = 1_785_297_600_000  # a minute boundary
CLOSE_MS = OPEN_MS + 59_999


def _kline(open_ms, close_price):
    return [
        open_ms, "64000.00", "64100.00", "63900.00", f"{close_price:.2f}", "12.5",
        open_ms + 59_999, "800000.0", 250, "6.0", "400000.0", "0",
    ]


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(fetcher.time, "sleep", lambda _: None)
    monkeypatch.setattr(fetcher, "_last_request_at", 0.0)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_close_price_comes_from_field_four():
    with _client(lambda request: httpx.Response(200, json=[_kline(OPEN_MS, 64321.55)])) as client:
        closes = fetch_minute_closes(client, "BTCUSDT", OPEN_MS, OPEN_MS + 60_000)
    assert list(closes.values()) == [64321.55]


def test_price_is_stamped_at_the_end_of_its_minute():
    # The minute [T, T+60) closes at T+59.999s, so its price is only known from T+60 onward.
    # Stamping at the open would let a lookup at T see a price formed over the following minute.
    with _client(lambda request: httpx.Response(200, json=[_kline(OPEN_MS, 64000.0)])) as client:
        closes = fetch_minute_closes(client, "BTCUSDT", OPEN_MS, OPEN_MS + 60_000)
    assert list(closes) == [CLOSE_MS // 1000 + 1]
    assert list(closes) == [OPEN_MS // 1000 + 60]


def test_pagination_walks_forward_by_a_minute():
    pages = [
        [_kline(OPEN_MS + i * 60_000, 64000.0 + i) for i in range(1000)],
        [_kline(OPEN_MS + 1000 * 60_000, 65000.0)],
    ]
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages.pop(0) if pages else [])

    with _client(handler) as client:
        closes = fetch_minute_closes(client, "BTCUSDT", OPEN_MS, OPEN_MS + 2000 * 60_000)
    assert len(closes) == 1001
    # The second request must resume one minute past the last open, not repeat it.
    assert int(seen[1]["startTime"]) == OPEN_MS + 1000 * 60_000


def test_a_short_page_ends_the_fetch():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=[_kline(OPEN_MS, 64000.0)])

    with _client(handler) as client:
        fetch_minute_closes(client, "BTCUSDT", OPEN_MS, OPEN_MS + 10 * 60_000)
    assert calls["n"] == 1  # fewer rows than the limit means there is no more data


def test_empty_response_ends_the_fetch():
    with _client(lambda request: httpx.Response(200, json=[])) as client:
        assert fetch_minute_closes(client, "BTCUSDT", OPEN_MS, OPEN_MS + 60_000) == {}


def test_a_repeated_open_time_cannot_spin_forever():
    # A server that keeps returning the same window must not become an endless loop.
    page = [_kline(OPEN_MS, 64000.0)] * 1000
    with _client(lambda request: httpx.Response(200, json=page)) as client:
        closes = fetch_minute_closes(client, "BTCUSDT", OPEN_MS, OPEN_MS + 10_000 * 60_000)
    assert len(closes) == 1


def test_output_round_trips_into_the_conditional_index(tmp_path):
    # Close the loop: what the fetcher writes must be readable by MinuteIndex, with the same
    # timestamps, or the join to candle observations silently drops everything.
    from backtest.conditional import MinuteIndex

    with _client(
        lambda request: httpx.Response(200, json=[_kline(OPEN_MS + i * 60_000, 64000.0 + i) for i in range(5)])
    ) as client:
        closes = fetch_minute_closes(client, "BTCUSDT", OPEN_MS, OPEN_MS + 5 * 60_000)

    path = tmp_path / "index.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "price"])
        for timestamp in sorted(closes):
            writer.writerow([timestamp, f"{closes[timestamp]:.2f}"])

    index = MinuteIndex.from_csv(path)
    assert len(index) == 5
    first = min(closes)
    assert index.price_at(first) == pytest.approx(64000.0)
    assert index.price_at(first - 1) is None
    assert index.sigma_per_minute() > 0
