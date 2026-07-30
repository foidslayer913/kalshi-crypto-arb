import json

import pytest
from datetime import datetime, timezone

from backtest.reconstruct import Variant
from backtest.window_fills import analyze_fills, reconstruct_ask_at, summarize_fills

CLOSE = datetime(2026, 7, 29, 22, 0, 0, tzinfo=timezone.utc)
CLOSE_TS = CLOSE.timestamp()


def _levels(pairs):
    return [[f"{price / 100:.4f}", f"{qty:.2f}"] for price, qty in pairs]


def _snapshot(ticker, yes=(), no=()):
    return {
        "type": "orderbook_snapshot",
        "msg": {"market_ticker": ticker, "yes_dollars_fp": _levels(yes), "no_dollars_fp": _levels(no)},
    }


def _delta(ticker, side, price, delta, ts):
    return {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": ticker, "side": side,
            "price_dollars": f"{price / 100:.4f}", "delta_fp": f"{delta:.2f}",
            "ts_ms": int(ts * 1000),
        },
    }


def test_reconstruct_ask_gates_on_venue_time():
    # A snapshot puts a no-bid at 5c (yes ask 0.95); a late delta removes it. Sampling before the
    # delta must still see 0.95; sampling after must see it gone -- no post-instant leak.
    ticker = "KXBTCD-T1"
    payloads = [
        _snapshot(ticker, no=[[5, 50]]),
        _delta(ticker, "no", 5, -50, CLOSE_TS - 2),
    ]
    asks = reconstruct_ask_at(payloads, ticker, "yes", [CLOSE_TS - 29, CLOSE_TS - 1])
    assert asks[CLOSE_TS - 29] == 0.95
    assert asks[CLOSE_TS - 1] is None


def _write_capture(path, lines):
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _capture_dir(tmp_path):
    directory = tmp_path / "captures"
    directory.mkdir()
    ticker = "KXBTCD-26JUL2922-T50"
    lines = []
    lines.append({
        "t": CLOSE_TS - 60, "kind": "market", "ticker": ticker,
        "strike_type": "greater", "strike_price": 50.0, "close_time": CLOSE.isoformat(),
    })
    # 61 index ticks at 100 across the window: floor average 100 >> strike 50, so strict fires.
    for i in range(61):
        ts = CLOSE_TS - 60 + i
        lines.append({"t": ts, "kind": "tick", "symbol": "BTC-USD", "price": 100.0, "ts": ts})
    # Book: no-bid at 5c (yes ask 0.95) from the snapshot, pulled 2s before close.
    lines.append({"t": CLOSE_TS - 60, "kind": "ws", "payload": _snapshot(ticker, no=[[5, 50]])})
    lines.append({"t": CLOSE_TS - 2, "kind": "ws", "payload": _delta(ticker, "no", 5, -50, CLOSE_TS - 2)})
    _write_capture(directory / "capture-2026-07-29.jsonl", lines)
    return directory


def test_analyze_fills_finds_the_ask_at_the_fire_second(tmp_path):
    directory = _capture_dir(tmp_path)
    results = analyze_fills(directory, Variant("strict"), window_size=60)
    assert len(results) == 1
    result = results[0]
    assert result.side == "yes"  # a "greater" market with floor above strike pays YES
    assert result.fire_second == 31  # 100*k/60 > 50 first holds at k=31
    assert result.ask_at_fire == 0.95
    # net yield = 1 - 0.95 - fee(0.01) = 0.04
    assert result.net_yield_at_fire == pytest.approx(0.04)
    assert result.ask_at_close is None  # the ask was pulled before the close


def test_summarize_fills_counts_fillable(tmp_path):
    directory = _capture_dir(tmp_path)
    results = analyze_fills(directory, Variant("strict"), window_size=60)
    summary = summarize_fills(results, "strict", min_yield=0.01)
    assert summary.fired == 1
    assert summary.had_ask == 1
    assert summary.fillable == 1
    assert summary.median_ask_at_fire == 0.95
    assert summary.median_net_yield_fillable == pytest.approx(0.04)


def test_market_whose_window_falls_in_a_gap_between_runs_is_not_scored(tmp_path):
    # The failure that produced a confident "0 fills": one capture file holding two runs. A market
    # from run 1 whose window sits in the dead air before run 2 has no recorded book, and must be
    # skipped rather than reported as having fired into an empty book.
    directory = tmp_path / "captures"
    directory.mkdir()
    gap_ticker = "KXBTCD-26JUL2922-T50"
    later_close = CLOSE_TS + 3600
    lines = [
        {
            "t": CLOSE_TS - 3600, "kind": "market", "ticker": gap_ticker,
            "strike_type": "greater", "strike_price": 50.0, "close_time": CLOSE.isoformat(),
        },
    ]
    # Run 1: an hour before this market's window, then the process stops.
    for i in range(30):
        ts = CLOSE_TS - 3600 + i
        lines.append({"t": ts, "kind": "tick", "symbol": "BTC-USD", "price": 100.0, "ts": ts})
    lines.append({"t": CLOSE_TS - 3600, "kind": "ws", "payload": _snapshot(gap_ticker, no=[[5, 50]])})
    # Run 2: starts an hour after, well past the market's settlement window.
    for i in range(61):
        ts = later_close - 60 + i
        lines.append({"t": ts, "kind": "tick", "symbol": "BTC-USD", "price": 100.0, "ts": ts})
    _write_capture(directory / "capture-2026-07-29.jsonl", lines)

    assert analyze_fills(directory, Variant("strict"), window_size=60) == []


def test_no_ask_when_book_empty_on_winning_side(tmp_path):
    directory = tmp_path / "captures"
    directory.mkdir()
    ticker = "KXBTCD-26JUL2922-T50"
    lines = [{
        "t": CLOSE_TS - 60, "kind": "market", "ticker": ticker,
        "strike_type": "greater", "strike_price": 50.0, "close_time": CLOSE.isoformat(),
    }]
    for i in range(61):
        ts = CLOSE_TS - 60 + i
        lines.append({"t": ts, "kind": "tick", "symbol": "BTC-USD", "price": 100.0, "ts": ts})
    # Only a yes-bid exists, so there is no ask on the YES side (that needs a NO bid).
    lines.append({"t": CLOSE_TS - 60, "kind": "ws", "payload": _snapshot(ticker, yes=[[5, 50]])})
    _write_capture(directory / "capture-2026-07-29.jsonl", lines)

    results = analyze_fills(directory, Variant("strict"), window_size=60)
    assert len(results) == 1
    assert results[0].ask_at_fire is None
    summary = summarize_fills(results, "strict", min_yield=0.01)
    assert summary.had_ask == 0
