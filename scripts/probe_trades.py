"""Probe Kalshi's trades log and measure how much price moves *inside* a minute.

Every price study in FINDINGS.md samples 1-minute candle closes, so it is structurally blind to a
dislocation lasting seconds — and one observed candle had `yes_ask` ranging 0.44 to 1.00 within a
single minute. Candles bottom out at 1-minute resolution, so the only finer public record is the
trades log: individual executions with sub-second timestamps.

This does two things:

1. Dumps a raw trade payload, so the schema is verified rather than assumed (three schema guesses in
   this project have already produced plausible but wrong results).
2. Measures the **opportunity set**: within each minute, how far below that minute's closing price
   did a trade actually print?

On (2), read the number as an upper bound and nothing more. Buying at a minute's low requires
knowing in advance that it was the low, which is look-ahead. The value is asymmetric: if the upper
bound is smaller than the fee, the whole avenue is closed and no strategy needs building. Only if it
is large does a rule-based test become worth writing.

Read-only: GETs against Kalshi's production market-data API.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

import httpx

from scripts.fetch_ground_truth import KALSHI_PROD_MARKET_DATA, _get, _log, _to_unix
from scripts.probe_candlesticks import _volume, pick_from_series

TRADES_PATH = "/markets/trades"
# Candidate keys for the traded price and timestamp; the probe reports which ones were actually
# present rather than trusting any single documented shape.
PRICE_KEYS = ("yes_price_dollars", "yes_price", "price_dollars", "price")
TIME_KEYS = ("created_time", "ts", "timestamp", "created_ts")
COUNT_KEYS = ("count_fp", "count", "size")


def _first_present(row: dict, keys: tuple[str, ...]) -> tuple[str | None, object]:
    for key in keys:
        if key in row and row[key] is not None:
            return key, row[key]
    return None, None


def _as_dollars(value: object) -> float | None:
    """Normalise a traded price to dollars. Kalshi sends cents as ints and dollars as strings."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # A price above 1.5 cannot be a dollar figure for a $1 binary contract, so it is cents.
    return number / 100.0 if number > 1.5 else number


def _as_epoch(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Milliseconds if it is far too large to be seconds.
        return float(value) / 1000.0 if float(value) > 1e11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fetch_trades(client: httpx.Client, ticker: str, min_ts: int, max_ts: int, pages: int = 20) -> list[dict]:
    trades: list[dict] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(pages):
        params: dict[str, object] = {"ticker": ticker, "limit": 1000, "min_ts": min_ts, "max_ts": max_ts}
        if cursor:
            params["cursor"] = cursor
        payload = _get(client, f"{KALSHI_PROD_MARKET_DATA}{TRADES_PATH}", params)
        if not isinstance(payload, dict):
            break
        page = payload.get("trades") or []
        trades.extend(page)
        cursor = payload.get("cursor") or None
        if not cursor or not page or cursor in seen:
            break
        seen.add(cursor)
    return trades


def summarise_intraminute(trades: list[dict]) -> None:
    price_key, _ = _first_present(trades[0], PRICE_KEYS)
    time_key, _ = _first_present(trades[0], TIME_KEYS)
    count_key, _ = _first_present(trades[0], COUNT_KEYS)
    print(f"price field: {price_key!r}   time field: {time_key!r}   size field: {count_key!r}")
    if price_key is None or time_key is None:
        print("Could not identify price/time fields; inspect the raw payload above.")
        return

    by_minute: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for row in trades:
        price = _as_dollars(row.get(price_key))
        stamp = _as_epoch(row.get(time_key))
        if price is None or stamp is None:
            continue
        by_minute[int(stamp // 60)].append((stamp, price))

    if not by_minute:
        print("No trades could be parsed into minutes.")
        return

    gaps: list[float] = []
    ranges: list[float] = []
    per_minute_counts: list[int] = []
    for minute in sorted(by_minute):
        rows = sorted(by_minute[minute])
        prices = [price for _, price in rows]
        close = prices[-1]
        per_minute_counts.append(len(prices))
        ranges.append(max(prices) - min(prices))
        # How much cheaper than the minute's close did a trade actually print?
        gaps.append(close - min(prices))

    print()
    print(f"minutes with trades      {len(by_minute)}")
    print(f"trades per minute        median {median(per_minute_counts):.0f}, max {max(per_minute_counts)}")
    print(f"intra-minute range       median ${median(ranges):.4f}, max ${max(ranges):.4f}")
    print(f"best print below close   median ${median(gaps):.4f}, mean ${sum(gaps) / len(gaps):.4f}, "
          f"max ${max(gaps):.4f}")
    print()
    print("The 'best print below close' is an UPPER BOUND on intra-minute opportunity: capturing it")
    print("would require knowing the low in advance. Compare it to the ~0.006-0.017 taker fee:")
    median_gap = median(gaps)
    if median_gap < 0.006:
        print(f"  median ${median_gap:.4f} is below the fee — no room here, the avenue is closed.")
    elif median_gap < 0.02:
        print(f"  median ${median_gap:.4f} is comparable to the fee — marginal even before the")
        print("  look-ahead problem; a rule-based test would need a real edge to survive.")
    else:
        print(f"  median ${median_gap:.4f} is well above the fee — worth building a rule-based,")
        print("  causally-honest test (a rule using only information available at decision time).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series-ticker", default="KXBTC15M")
    parser.add_argument("--ticker", default=None, help="Probe this market directly.")
    parser.add_argument("--close-time", default=None, help="ISO close time, required with --ticker.")
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument("--minutes-before", type=float, default=20.0, help="Window before close to fetch.")
    args = parser.parse_args()

    if args.ticker:
        if not args.close_time:
            raise SystemExit("--ticker requires --close-time (e.g. 2026-07-30T00:15:00Z)")
        ticker, close_ts = args.ticker, _to_unix(args.close_time)
    else:
        market = pick_from_series(args.series_ticker, args.lookback_hours)
        ticker = market["ticker"]
        close_ts = _to_unix(market["close_time"])
        print(f"Probing {ticker} (volume {_volume(market):.0f})\n")

    min_ts = int(close_ts - args.minutes_before * 60)
    with httpx.Client(timeout=30.0) as client:
        try:
            trades = fetch_trades(client, ticker, min_ts, int(close_ts))
        except httpx.HTTPStatusError as error:
            print(f"HTTP {error.response.status_code}: {error.response.text[:400]}")
            print("\nIf this is a 404, the trades path or parameter names differ from the assumed shape.")
            return

    _log(f"{len(trades)} trades in the {args.minutes_before:g} minutes before close")
    if not trades:
        print("No trades returned. Try a busier market or a longer --minutes-before.")
        return

    print("\n--- raw trade (schema check) ---")
    print(json.dumps(trades[0], indent=2))
    print(f"\nkeys present: {sorted(trades[0].keys())}\n")
    summarise_intraminute(trades)


if __name__ == "__main__":
    main()
