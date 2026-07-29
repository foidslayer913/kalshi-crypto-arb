from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExecutionRecord:
    timestamp: float
    ticker: str
    side: str
    count: int
    price: float
    signal_time: float
    order_time: float
    phantom_fill: bool
    simulated_pnl: float

    @property
    def latency_ms(self) -> float:
        return (self.order_time - self.signal_time) * 1000


class TelemetryLogger:
    """Appends execution records to a CSV file for tick-to-order latency, phantom fill rate, and
    simulated P&L analysis (SPEC.md section 4).
    """

    _FIELDNAMES = [
        "timestamp", "ticker", "side", "count", "price",
        "signal_time", "order_time", "latency_ms", "phantom_fill", "simulated_pnl",
    ]

    def __init__(self, path: str | Path = "telemetry.csv") -> None:
        self._path = Path(path)
        if not self._path.exists():
            with self._path.open("w", newline="") as f:
                csv.DictWriter(f, fieldnames=self._FIELDNAMES).writeheader()

    def record(self, record: ExecutionRecord) -> None:
        row = asdict(record)
        row["latency_ms"] = record.latency_ms
        with self._path.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=self._FIELDNAMES).writerow(row)

    def summary(self) -> dict[str, float]:
        """Aggregate phantom fill rate, average latency, and total simulated P&L from the log."""
        empty = {"count": 0, "phantom_fill_rate": 0.0, "avg_latency_ms": 0.0, "total_pnl": 0.0}
        if not self._path.exists():
            return empty
        with self._path.open() as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return empty
        count = len(rows)
        phantom_fills = sum(1 for row in rows if row["phantom_fill"] == "True")
        avg_latency = sum(float(row["latency_ms"]) for row in rows) / count
        total_pnl = sum(float(row["simulated_pnl"]) for row in rows)
        return {
            "count": count,
            "phantom_fill_rate": phantom_fills / count,
            "avg_latency_ms": avg_latency,
            "total_pnl": total_pnl,
        }
