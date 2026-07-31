"""Tier 1 capture: tee live market data to disk so it can be replayed later.

Kalshi does not publish historical order books, so the resting depth this strategy depends on
only exists if we record it ourselves. This module writes one merged JSONL stream per UTC day.

Two properties matter more than anything else about the format:

* **Every record is keyed on arrival time**, not on any timestamp the venue supplied. `t` is when
  *we* observed the event and therefore the earliest moment we could have acted on it. Pacing a
  replay off venue timestamps would hand the strategy information before it actually had it,
  which is the exact look-ahead bias a capture exists to rule out.
* **The stream is merged and append-ordered.** Order book messages and index ticks interleave on
  disk in the same order they interleaved live, so a replay driver can walk one file forward
  instead of reconciling two.

Writes stay off the hot path: `record_*` builds a dict and appends it, and a background
`run_forever()` task does the serialisation and disk I/O.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clock import Clock, LiveClock
from ingestion.crypto_feed import PriceTick
from ingestion.kalshi_rest import MarketInfo

logger = logging.getLogger(__name__)

CAPTURE_SCHEMA_VERSION = 1
CAPTURE_GLOB = "capture-*.jsonl"
CAPTURE_GLOB_GZ = "capture-*.jsonl.gz"


@dataclass(frozen=True)
class CapturedEvent:
    """One replayable observation. `t` is arrival time — the replay clock's pacing key."""

    t: float
    kind: str
    data: dict[str, Any]


def _day_key(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


class CaptureRecorder:
    """Buffers observations in memory and flushes them to per-day JSONL files."""

    def __init__(
        self,
        directory: str | Path,
        *,
        clock: Clock | None = None,
        flush_interval: float = 5.0,
        max_buffer: int = 2000,
    ) -> None:
        self._directory = Path(directory)
        self._clock = clock or LiveClock()
        self._flush_interval = flush_interval
        self._max_buffer = max_buffer
        self._buffer: list[dict[str, Any]] = []
        self._directory.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def path_for(self, timestamp: float) -> Path:
        return self._directory / f"capture-{_day_key(timestamp)}.jsonl"

    def _append(self, kind: str, data: dict[str, Any]) -> None:
        self._buffer.append({"t": self._clock.now(), "kind": kind, **data})
        # Backstop only: with the flush task running this never trips, but an unbounded buffer
        # would be worse than a rare synchronous write if that task is missing or wedged.
        if len(self._buffer) >= self._max_buffer:
            self.flush()

    def record_session(self) -> None:
        """Write a header marking the start of a capture run."""
        self._append("session", {"version": CAPTURE_SCHEMA_VERSION})

    def record_ws(self, message: dict[str, Any]) -> None:
        """Record a raw Kalshi WebSocket message verbatim.

        Stored unparsed on purpose: the schema in `ingestion/order_book.py` is written against
        Kalshi's documented shape and has not been checked against a live feed, so a capture must
        stay replayable even after that parsing is corrected.
        """
        self._append("ws", {"payload": message})

    def record_tick(self, tick: PriceTick) -> None:
        """Record an index tick. `ts` is when the price was sampled, `t` when we received it."""
        self._append("tick", {"symbol": tick.symbol, "price": tick.price, "ts": tick.timestamp})

    def record_market(self, market: MarketInfo) -> None:
        """Record a market's metadata. Without this a replay cannot rebuild the scanners, since
        strike and close time come from a REST call that will not be repeated at replay time.
        """
        self._append(
            "market",
            {
                "ticker": market.ticker,
                "strike_type": market.strike_type,
                "strike_price": market.strike_price,
                "close_time": market.close_time.isoformat(),
            },
        )

    def flush(self) -> None:
        """Write buffered records out, grouped by the UTC day they arrived on."""
        if not self._buffer:
            return
        pending, self._buffer = self._buffer, []
        by_path: dict[Path, list[str]] = {}
        for record in pending:
            by_path.setdefault(self.path_for(record["t"]), []).append(json.dumps(record))
        for path, lines in by_path.items():
            with path.open("a") as handle:
                handle.write("\n".join(lines) + "\n")

    async def run_forever(self) -> None:
        """Flush on an interval. Runs alongside the ingestion streams in `main.py`."""
        logger.info("Recording market data capture to %s", self._directory)
        self.record_session()
        try:
            while True:
                await self._clock.sleep(self._flush_interval)
                self.flush()
        finally:
            self.flush()

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> CaptureRecorder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_capture(path: str | Path) -> Iterator[CapturedEvent]:
    """Stream events from a single capture file in recorded (arrival) order.

    Transparently reads gzipped day files, so compressing yesterday's capture does not break any
    reader. At roughly 150 bytes an event and ~1M events a day, leaving days uncompressed is what
    eventually fills the disk and stops the capture.
    """
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            data = {key: value for key, value in record.items() if key not in ("t", "kind")}
            yield CapturedEvent(t=record["t"], kind=record["kind"], data=data)


def capture_files(directory: str | Path) -> list[Path]:
    """Day files in chronological order, compressed or not.

    The date sits at a fixed position in the name, so plain lexical sorting keeps days in order even
    when some are gzipped and some are not.
    """
    directory = Path(directory)
    return sorted(
        [*directory.glob(CAPTURE_GLOB), *directory.glob(CAPTURE_GLOB_GZ)],
        key=lambda path: path.name,
    )


def export_completed_days(directory: str | Path, export_dir: str | Path) -> list[Path]:
    """Copy finished, compressed day files into `export_dir`, skipping ones already there.

    Exists so a capture host and an analysis machine can be different computers. Only *completed*
    days are copied: the current day file is still being appended to, and a cloud-sync folder
    replicating a half-written file hands the other machine a truncated final line. Compressed days
    are immutable, so copying them is safe and idempotent.
    """
    directory, export_dir = Path(directory), Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for path in sorted(directory.glob(CAPTURE_GLOB_GZ)):
        target = export_dir / path.name
        if target.exists() and target.stat().st_size == path.stat().st_size:
            continue
        # Write to a temporary name first so a sync client never sees a partial file under the
        # real name.
        staging = target.with_suffix(target.suffix + ".partial")
        shutil.copyfile(path, staging)
        staging.replace(target)
        copied.append(target)
        logger.info("Exported %s -> %s", path.name, export_dir)
    return copied


def compress_completed_days(directory: str | Path, today: str | None = None) -> list[Path]:
    """Gzip finished day files, leaving today's alone because it is still being appended to.

    Returns the paths written. The original is removed only after the compressed copy is complete,
    so an interrupted run cannot lose a day.
    """
    directory = Path(directory)
    current = today or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    written: list[Path] = []
    for path in sorted(directory.glob(CAPTURE_GLOB)):
        if current in path.name:
            continue
        target = path.with_suffix(path.suffix + ".gz")
        if target.exists():
            continue
        with path.open("rb") as source, gzip.open(target, "wb") as sink:
            shutil.copyfileobj(source, sink)
        path.unlink()
        written.append(target)
        logger.info("Compressed %s -> %s", path.name, target.name)
    return written


def read_captures(directory: str | Path, kinds: Iterable[str] | None = None) -> Iterator[CapturedEvent]:
    """Stream every event in a capture directory, ordered across day files.

    Files are named by UTC day and each is append-ordered, so walking them in filename order
    yields globally arrival-ordered events without holding the capture in memory.
    """
    wanted = set(kinds) if kinds is not None else None
    for path in capture_files(directory):
        for event in read_capture(path):
            if wanted is None or event.kind in wanted:
                yield event


def captured_markets(directory: str | Path) -> list[MarketInfo]:
    """Rebuild the market metadata a replay needs, keeping the last record per ticker."""
    markets: dict[str, MarketInfo] = {}
    for event in read_captures(directory, kinds={"market"}):
        markets[event.data["ticker"]] = MarketInfo(
            ticker=event.data["ticker"],
            strike_type=event.data["strike_type"],
            strike_price=float(event.data["strike_price"]),
            close_time=datetime.fromisoformat(event.data["close_time"]).astimezone(timezone.utc),
        )
    return list(markets.values())


def capture_stats(directory: str | Path) -> dict[str, Any]:
    """Summarise a capture so its coverage can be checked before anyone replays it."""
    counts: dict[str, int] = {}
    symbols: set[str] = set()
    tickers: set[str] = set()
    first: float | None = None
    last: float | None = None
    for event in read_captures(directory):
        counts[event.kind] = counts.get(event.kind, 0) + 1
        if first is None:
            first = event.t
        last = event.t
        if event.kind == "tick":
            symbols.add(event.data["symbol"])
        elif event.kind == "market":
            tickers.add(event.data["ticker"])
    return {
        "events": sum(counts.values()),
        "by_kind": dict(sorted(counts.items())),
        "symbols": sorted(symbols),
        "markets": sorted(tickers),
        "first_event": first,
        "last_event": last,
        "duration_seconds": (last - first) if first is not None and last is not None else 0.0,
        "files": [path.name for path in capture_files(directory)],
    }


def format_stats(stats: dict[str, Any]) -> str:
    lines = [
        f"files            {len(stats['files'])}",
        f"events           {stats['events']}",
        f"by kind          {stats['by_kind']}",
        f"symbols          {', '.join(stats['symbols']) or '-'}",
        f"markets          {len(stats['markets'])}",
        f"duration         {stats['duration_seconds'] / 3600:.2f}h",
    ]
    if stats["first_event"] is not None:
        started = datetime.fromtimestamp(stats["first_event"], tz=timezone.utc)
        lines.append(f"first event      {started.isoformat()}")
    return "\n".join(lines)
