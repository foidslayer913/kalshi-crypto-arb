"""Tests for the per-second dataset builder.

The output is meant to be consumed by someone who cannot check it against the raw capture, so the
properties that matter are the ones they would have to take on trust: that timestamps are arrival
times, that a quiet market still produces rows, and that an implied ask really is derived from the
opposing bid.
"""

import csv
import json
from datetime import datetime, timezone

import pytest

from dataset.timeseries import build_rows, write_csv

CLOSE = datetime(2026, 7, 30, 4, 15, 0, tzinfo=timezone.utc)
CLOSE_TS = int(CLOSE.timestamp())
TICKER = "KXBTC15M-26JUL300415"


def _levels(pairs):
    return [[f"{price / 100:.4f}", f"{qty:.2f}"] for price, qty in pairs]


def _snapshot(ticker, yes=(), no=()):
    return {
        "type": "orderbook_snapshot",
        "msg": {"market_ticker": ticker, "yes_dollars_fp": _levels(yes), "no_dollars_fp": _levels(no)},
    }


def _capture(tmp_path, events, strike=64000.0):
    directory = tmp_path / "captures"
    directory.mkdir(exist_ok=True)
    lines = [{
        "t": CLOSE_TS - 600, "kind": "market", "ticker": TICKER,
        "strike_type": "greater", "strike_price": strike, "close_time": CLOSE.isoformat(),
    }]
    lines.extend(events)
    (directory / "capture-2026-07-30.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n"
    )
    return directory


def test_implied_ask_comes_from_the_opposing_bid(tmp_path):
    directory = _capture(tmp_path, [
        {"t": CLOSE_TS - 100, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
        {"t": CLOSE_TS - 98, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
    ])
    rows = list(build_rows(directory))
    assert rows
    row = rows[0]
    assert row.yes_bid == 40
    assert row.no_bid == 45
    assert row.yes_ask == 55  # 100 - best no bid
    assert row.no_ask == 60   # 100 - best yes bid
    assert row.spread == 15
    assert row.mid == pytest.approx(47.5)


def test_a_row_is_emitted_for_every_second_even_when_nothing_happens(tmp_path):
    # A gap in an event-driven log is ambiguous; a row per second with stale_seconds is not.
    directory = _capture(tmp_path, [
        {"t": CLOSE_TS - 100, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
        {"t": CLOSE_TS - 95, "kind": "tick", "symbol": "BTC-USD", "price": 64100.0, "ts": CLOSE_TS - 95},
    ])
    seconds = [row.t for row in build_rows(directory)]
    # Inclusive of the last observed second, and with no holes in between.
    assert seconds == list(range(CLOSE_TS - 100, CLOSE_TS - 94))


def test_stale_seconds_counts_since_the_book_last_moved(tmp_path):
    directory = _capture(tmp_path, [
        {"t": CLOSE_TS - 100, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
        {"t": CLOSE_TS - 96, "kind": "tick", "symbol": "BTC-USD", "price": 64100.0, "ts": CLOSE_TS - 96},
    ])
    by_second = {row.t: row.stale_seconds for row in build_rows(directory)}
    assert by_second[CLOSE_TS - 100] == 0
    assert by_second[CLOSE_TS - 98] == 2


def test_seconds_to_close_counts_down(tmp_path):
    directory = _capture(tmp_path, [
        {"t": CLOSE_TS - 10, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
        {"t": CLOSE_TS - 7, "kind": "ws", "payload": _snapshot(TICKER, yes=[[41, 10]], no=[[45, 20]])},
    ])
    rows = sorted(build_rows(directory), key=lambda r: r.t)
    assert rows[0].seconds_to_close == 10
    assert rows[-1].seconds_to_close == 7  # the last observed second is included


def test_index_distance_is_signed_relative_to_the_strike(tmp_path):
    directory = _capture(tmp_path, [
        {"t": CLOSE_TS - 100, "kind": "tick", "symbol": "BTC-USD", "price": 64640.0, "ts": CLOSE_TS - 100},
        {"t": CLOSE_TS - 99, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
        {"t": CLOSE_TS - 97, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
    ], strike=64000.0)
    row = next(r for r in build_rows(directory) if r.index_price is not None)
    assert row.index_price == pytest.approx(64640.0)
    assert row.index_distance_pct == pytest.approx(1.0)  # 1% above the strike


def test_markets_outside_the_window_are_excluded(tmp_path):
    directory = _capture(tmp_path, [
        {"t": CLOSE_TS - 7200, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
        {"t": CLOSE_TS - 7198, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
    ])
    assert list(build_rows(directory, within_minutes=20.0)) == []
    assert list(build_rows(directory, within_minutes=180.0)) != []


def test_a_market_that_was_never_quoted_produces_no_rows(tmp_path):
    # An empty row would imply a book we observed to be empty, rather than one we never saw.
    directory = _capture(tmp_path, [
        {"t": CLOSE_TS - 100, "kind": "tick", "symbol": "BTC-USD", "price": 64100.0, "ts": CLOSE_TS - 100},
        {"t": CLOSE_TS - 90, "kind": "tick", "symbol": "BTC-USD", "price": 64110.0, "ts": CLOSE_TS - 90},
    ])
    assert list(build_rows(directory)) == []


def test_csv_round_trips_with_a_stable_header(tmp_path):
    directory = _capture(tmp_path, [
        {"t": CLOSE_TS - 30, "kind": "ws", "payload": _snapshot(TICKER, yes=[[40, 10]], no=[[45, 20]])},
        {"t": CLOSE_TS - 28, "kind": "ws", "payload": _snapshot(TICKER, yes=[[41, 12]], no=[[45, 20]])},
    ])
    out = tmp_path / "out.csv"
    written = write_csv(build_rows(directory), out)
    assert written > 0
    with out.open() as handle:
        parsed = list(csv.DictReader(handle))
    assert len(parsed) == written
    assert parsed[0]["ticker"] == TICKER
    assert int(parsed[0]["yes_ask"]) == 55
    # A consumer depends on these names; changing them silently breaks them.
    assert set(parsed[0]) >= {"t", "ticker", "seconds_to_close", "yes_bid", "yes_ask", "mid", "strike"}
