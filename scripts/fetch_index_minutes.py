"""Fetch a 1-minute index series over a date range, for joining to Kalshi candle observations.

The existing `fetch_ground_truth series` command pulls second-resolution trades for each market's
final 60 seconds, which is what the settlement invariant needs. The conditional study needs
something different: the index at *every* candle boundary across a market's whole life, so that
"where was the index when the market quoted this price?" can be answered.

Binance 1-minute klines line up exactly with Kalshi's 1-minute candles and cost about one request
per 1000 minutes — a month is ~44 requests rather than the thousands aggTrades would need.

Each row is stamped at the **end** of its minute and carries that minute's closing price, so a
reader querying at time T only ever sees trades that had already happened by T. Stamping at the
start would leak a minute of future prices into every lookup.

Read-only: GETs against Binance's public market-data API.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scripts.fetch_ground_truth import (
    BINANCE_DATA_API,
    DEFAULT_SYMBOL_TO_BINANCE,
    _get,
    _log,
    _to_unix,
)

KLINE_LIMIT = 1000


def fetch_minute_closes(
    client: httpx.Client, binance_symbol: str, start_ms: int, end_ms: int
) -> dict[int, float]:
    """Closing price per minute, keyed by the unix second at which that price is known."""
    closes: dict[int, float] = {}
    cursor = start_ms
    while cursor < end_ms:
        payload = _get(
            client,
            f"{BINANCE_DATA_API}/api/v3/klines",
            {
                "symbol": binance_symbol,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": KLINE_LIMIT,
            },
            min_interval=0.12,
        )
        if not isinstance(payload, list) or not payload:
            break
        for row in payload:
            # row = [openTime, open, high, low, close, volume, closeTime, ...]
            close_time_ms = int(row[6])
            # closeTime is the last millisecond of the minute; the price is known from the next
            # second onward, which is the timestamp a lookup at a candle boundary will use.
            closes[close_time_ms // 1000 + 1] = float(row[4])
        last_open = int(payload[-1][0])
        if len(payload) < KLINE_LIMIT or last_open <= cursor:
            break
        cursor = last_open + 60_000
        if len(closes) % 5000 < KLINE_LIMIT:
            _log(f"  {len(closes)} minutes collected")
    return closes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC-USD", help="Feed symbol, e.g. BTC-USD")
    parser.add_argument("--start", required=True, help="e.g. 2026-07-01")
    parser.add_argument("--end", required=True, help="e.g. 2026-07-31")
    parser.add_argument("-o", "--out", default=None, help="Default: data/<SYMBOL>-1m.csv")
    parser.add_argument(
        "--binance-symbol", default=None, help="Override the mapped Binance symbol (e.g. BTCUSDT)."
    )
    args = parser.parse_args()

    binance_symbol = args.binance_symbol or DEFAULT_SYMBOL_TO_BINANCE.get(args.symbol)
    if binance_symbol is None:
        raise SystemExit(
            f"No Binance symbol mapped for {args.symbol!r}; pass --binance-symbol explicitly."
        )

    out_path = Path(args.out or f"data/{args.symbol}-1m.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Pad the start by a minute so the first candle boundary in range has a price to look up.
    start_ms = int(_to_unix(args.start) - 60) * 1000
    end_ms = int(_to_unix(args.end)) * 1000
    _log(f"Fetching {binance_symbol} 1m closes {args.start}..{args.end}")

    with httpx.Client(timeout=30.0) as client:
        closes = fetch_minute_closes(client, binance_symbol, start_ms, end_ms)

    if not closes:
        raise SystemExit("No klines returned; check the symbol and date range.")

    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "price"])
        for timestamp in sorted(closes):
            writer.writerow([timestamp, f"{closes[timestamp]:.2f}"])

    first, last = min(closes), max(closes)
    _log(
        f"Wrote {len(closes)} minutes to {out_path} "
        f"({datetime.fromtimestamp(first, tz=timezone.utc)} .. "
        f"{datetime.fromtimestamp(last, tz=timezone.utc)} UTC)"
    )
    expected = (last - first) // 60 + 1
    if len(closes) < expected * 0.98:
        _log(f"WARNING: {expected - len(closes)} minutes missing — gaps will drop observations.")


if __name__ == "__main__":
    main()
