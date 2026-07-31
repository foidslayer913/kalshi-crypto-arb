import asyncio
import json
from datetime import datetime, timezone

import pytest

from backtest.recorder import (
    CAPTURE_SCHEMA_VERSION,
    CaptureRecorder,
    capture_files,
    capture_stats,
    captured_markets,
    read_capture,
    read_captures,
)
from ingestion.crypto_feed import PriceTick
from ingestion.kalshi_rest import MarketInfo

DAY_ONE = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc).timestamp()
DAY_TWO = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc).timestamp()


class SettableClock:
    """A Clock whose time the test moves explicitly, so capture timestamps are deterministic."""

    def __init__(self, now: float) -> None:
        self._now = now

    def now(self) -> float:
        return self._now

    def set(self, value: float) -> None:
        self._now = value

    async def sleep(self, seconds: float) -> None:
        self._now += seconds
        await asyncio.sleep(0)


def _market(ticker="KXBTC-TEST"):
    return MarketInfo(
        ticker=ticker, strike_type="greater", strike_price=118000.0,
        close_time=datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc),
    )


def _recorder(tmp_path, start=DAY_ONE, **kwargs):
    return CaptureRecorder(tmp_path / "captures", clock=SettableClock(start), **kwargs)


def test_recording_does_not_touch_disk_until_flush(tmp_path):
    # The hot path must not pay for disk I/O -- latency is the thing being measured.
    recorder = _recorder(tmp_path)
    recorder.record_ws({"type": "orderbook_snapshot"})
    assert recorder.buffered == 1
    assert capture_files(recorder.directory) == []
    recorder.flush()
    assert len(capture_files(recorder.directory)) == 1


def test_flush_is_a_noop_with_an_empty_buffer(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.flush()
    assert capture_files(recorder.directory) == []


def test_ws_message_round_trips_verbatim(tmp_path):
    recorder = _recorder(tmp_path)
    message = {"type": "orderbook_delta", "msg": {"market_ticker": "T1", "side": "yes", "price": 45, "delta": 10}}
    recorder.record_ws(message)
    recorder.flush()

    events = list(read_captures(recorder.directory))
    assert len(events) == 1
    assert events[0].kind == "ws"
    assert events[0].t == DAY_ONE
    assert events[0].data["payload"] == message


def test_tick_records_arrival_time_separately_from_sample_time(tmp_path):
    # `ts` is when the venue sampled the price; `t` is when we saw it. Replay must pace off `t`,
    # so the two have to stay distinguishable on disk.
    recorder = _recorder(tmp_path)
    recorder.record_tick(PriceTick(symbol="BTC-USD", price=118234.5, timestamp=DAY_ONE - 0.4))
    recorder.flush()

    event = next(iter(read_captures(recorder.directory)))
    assert event.kind == "tick"
    assert event.t == DAY_ONE
    assert event.data["ts"] == DAY_ONE - 0.4
    assert event.data["price"] == 118234.5
    assert event.data["symbol"] == "BTC-USD"


def test_session_header_records_schema_version(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.record_session()
    recorder.flush()
    event = next(iter(read_captures(recorder.directory)))
    assert event.kind == "session"
    assert event.data["version"] == CAPTURE_SCHEMA_VERSION


def test_events_keep_arrival_order_across_kinds(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.record_ws({"seq": 1})
    recorder.record_tick(PriceTick("BTC-USD", 1.0, DAY_ONE))
    recorder.record_ws({"seq": 2})
    recorder.flush()

    assert [event.kind for event in read_captures(recorder.directory)] == ["ws", "tick", "ws"]


def test_records_rotate_into_per_day_files(tmp_path):
    clock = SettableClock(DAY_ONE)
    recorder = CaptureRecorder(tmp_path / "captures", clock=clock)
    recorder.record_ws({"day": 1})
    clock.set(DAY_TWO)  # buffer spans midnight before it is ever written
    recorder.record_ws({"day": 2})
    recorder.flush()

    names = [path.name for path in capture_files(recorder.directory)]
    assert names == ["capture-2026-07-29.jsonl", "capture-2026-07-30.jsonl"]
    assert [event.data["payload"]["day"] for event in read_captures(recorder.directory)] == [1, 2]


def test_appending_to_an_existing_day_file_preserves_earlier_records(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.record_ws({"seq": 1})
    recorder.flush()
    recorder.record_ws({"seq": 2})
    recorder.flush()

    assert len(capture_files(recorder.directory)) == 1
    assert [event.data["payload"]["seq"] for event in read_captures(recorder.directory)] == [1, 2]


def test_buffer_cap_forces_a_synchronous_flush(tmp_path):
    recorder = _recorder(tmp_path, max_buffer=3)
    for index in range(3):
        recorder.record_ws({"seq": index})
    assert recorder.buffered == 0  # backstop fired rather than growing without bound
    assert len(list(read_captures(recorder.directory))) == 3


def test_read_captures_can_filter_by_kind(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.record_ws({"seq": 1})
    recorder.record_tick(PriceTick("BTC-USD", 1.0, DAY_ONE))
    recorder.record_market(_market())
    recorder.flush()

    kinds = [event.kind for event in read_captures(recorder.directory, kinds={"tick", "market"})]
    assert kinds == ["tick", "market"]


def test_captured_markets_rebuilds_market_info(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.record_market(_market())
    recorder.flush()

    markets = captured_markets(recorder.directory)
    assert len(markets) == 1
    assert markets[0].ticker == "KXBTC-TEST"
    assert markets[0].strike_price == 118000.0
    assert markets[0].strike_type == "greater"
    assert markets[0].close_time == datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc)


def test_captured_markets_keeps_the_latest_record_per_ticker(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.record_market(_market())
    updated = MarketInfo(
        ticker="KXBTC-TEST", strike_type="greater", strike_price=119000.0,
        close_time=datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc),
    )
    recorder.record_market(updated)
    recorder.flush()

    markets = captured_markets(recorder.directory)
    assert len(markets) == 1
    assert markets[0].strike_price == 119000.0


def test_capture_stats_summarises_coverage(tmp_path):
    clock = SettableClock(DAY_ONE)
    recorder = CaptureRecorder(tmp_path / "captures", clock=clock)
    recorder.record_session()
    recorder.record_market(_market())
    recorder.record_tick(PriceTick("BTC-USD", 1.0, DAY_ONE))
    clock.set(DAY_ONE + 7200)
    recorder.record_ws({"seq": 1})
    recorder.flush()

    stats = capture_stats(recorder.directory)
    assert stats["events"] == 4
    assert stats["by_kind"] == {"market": 1, "session": 1, "tick": 1, "ws": 1}
    assert stats["symbols"] == ["BTC-USD"]
    assert stats["markets"] == ["KXBTC-TEST"]
    assert stats["duration_seconds"] == pytest.approx(7200)


def test_capture_stats_on_empty_directory(tmp_path):
    stats = capture_stats(tmp_path)
    assert stats["events"] == 0
    assert stats["files"] == []
    assert stats["duration_seconds"] == 0.0


def test_read_capture_skips_blank_lines(tmp_path):
    path = tmp_path / "capture-2026-07-29.jsonl"
    path.write_text(json.dumps({"t": 1.0, "kind": "ws", "payload": {}}) + "\n\n")
    assert len(list(read_capture(path))) == 1


def test_run_forever_flushes_on_an_interval(tmp_path):
    clock = SettableClock(DAY_ONE)
    recorder = CaptureRecorder(tmp_path / "captures", clock=clock, flush_interval=5.0)

    async def scenario():
        task = asyncio.create_task(recorder.run_forever())
        await asyncio.sleep(0)
        recorder.record_ws({"seq": 1})
        for _ in range(5):  # let the flush loop come round
            await asyncio.sleep(0)
        assert recorder.buffered == 0
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    payloads = [event.data.get("payload") for event in read_captures(recorder.directory)]
    assert {"seq": 1} in payloads


def test_run_forever_flushes_buffered_records_on_cancellation(tmp_path):
    # A capture run ends by being killed, so anything still buffered has to reach disk then.
    clock = SettableClock(DAY_ONE)
    recorder = CaptureRecorder(tmp_path / "captures", clock=clock, flush_interval=3600.0)

    async def scenario():
        task = asyncio.create_task(recorder.run_forever())
        await asyncio.sleep(0)
        recorder.record_ws({"seq": 99})
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    payloads = [event.data.get("payload") for event in read_captures(recorder.directory)]
    assert {"seq": 99} in payloads


def test_close_flushes_remaining_records(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.record_ws({"seq": 1})
    recorder.close()
    assert len(list(read_captures(recorder.directory))) == 1


def test_context_manager_flushes_on_exit(tmp_path):
    with _recorder(tmp_path) as recorder:
        recorder.record_ws({"seq": 1})
    assert len(list(read_captures(recorder.directory))) == 1


def test_export_copies_only_completed_compressed_days(tmp_path):
    # Splitting capture host from analysis machine: today's file is mid-append, and a sync client
    # replicating it would hand the other machine a truncated final line.
    from backtest.recorder import compress_completed_days, export_completed_days

    directory = tmp_path / "captures"
    directory.mkdir()
    (directory / "capture-2026-07-29.jsonl").write_text('{"t": 1.0, "kind": "ws", "payload": {}}\n')
    (directory / "capture-2026-07-30.jsonl").write_text('{"t": 2.0, "kind": "ws", "payload": {}}\n')
    compress_completed_days(directory, today="2026-07-30")

    export = tmp_path / "export"
    copied = export_completed_days(directory, export)
    names = sorted(path.name for path in copied)
    assert names == ["capture-2026-07-29.jsonl.gz"]
    assert not (export / "capture-2026-07-30.jsonl").exists()  # today's stays behind


def test_export_is_idempotent(tmp_path):
    from backtest.recorder import compress_completed_days, export_completed_days

    directory = tmp_path / "captures"
    directory.mkdir()
    (directory / "capture-2026-07-29.jsonl").write_text('{"t": 1.0, "kind": "ws", "payload": {}}\n')
    compress_completed_days(directory, today="2026-07-30")

    export = tmp_path / "export"
    assert len(export_completed_days(directory, export)) == 1
    assert export_completed_days(directory, export) == []  # already there, not recopied


def test_exported_day_is_still_readable(tmp_path):
    from backtest.recorder import compress_completed_days, export_completed_days, read_captures

    directory = tmp_path / "captures"
    directory.mkdir()
    (directory / "capture-2026-07-29.jsonl").write_text(
        '{"t": 1.0, "kind": "ws", "payload": {"seq": 7}}\n'
    )
    compress_completed_days(directory, today="2026-07-30")
    export = tmp_path / "export"
    export_completed_days(directory, export)

    events = list(read_captures(export))
    assert [event.data["payload"]["seq"] for event in events] == [7]


def test_export_leaves_no_partial_files_behind(tmp_path):
    from backtest.recorder import compress_completed_days, export_completed_days

    directory = tmp_path / "captures"
    directory.mkdir()
    (directory / "capture-2026-07-29.jsonl").write_text('{"t": 1.0, "kind": "ws", "payload": {}}\n')
    compress_completed_days(directory, today="2026-07-30")
    export = tmp_path / "export"
    export_completed_days(directory, export)
    assert list(export.glob("*.partial")) == []
