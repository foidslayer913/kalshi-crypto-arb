import pytest

from telemetry.logger import ExecutionRecord, TelemetryLogger


def _record(order_time: float, phantom_fill: bool, pnl: float) -> ExecutionRecord:
    return ExecutionRecord(
        timestamp=1000.0,
        ticker="KXBTC-24JUL2915",
        side="yes",
        count=1,
        price=0.95,
        signal_time=1000.0,
        order_time=order_time,
        phantom_fill=phantom_fill,
        simulated_pnl=pnl,
    )


def test_latency_ms_computed_from_signal_and_order_time():
    record = _record(order_time=1000.05, phantom_fill=False, pnl=0.04)
    assert record.latency_ms == pytest.approx(50.0)


def test_logger_creates_file_with_header(tmp_path):
    path = tmp_path / "telemetry.csv"
    TelemetryLogger(path)
    assert path.exists()
    assert path.read_text().splitlines()[0].split(",")[0] == "timestamp"


def test_summary_on_empty_log(tmp_path):
    logger = TelemetryLogger(tmp_path / "telemetry.csv")
    summary = logger.summary()
    assert summary == {"count": 0, "phantom_fill_rate": 0.0, "avg_latency_ms": 0.0, "total_pnl": 0.0}


def test_summary_aggregates_records(tmp_path):
    logger = TelemetryLogger(tmp_path / "telemetry.csv")
    logger.record(_record(order_time=1000.05, phantom_fill=False, pnl=0.04))
    logger.record(_record(order_time=1000.10, phantom_fill=True, pnl=-0.05))

    summary = logger.summary()
    assert summary["count"] == 2
    assert summary["phantom_fill_rate"] == 0.5
    assert summary["avg_latency_ms"] == pytest.approx(75.0)
    assert summary["total_pnl"] == pytest.approx(-0.01)
