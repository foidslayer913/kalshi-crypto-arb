"""Probe Kalshi's candlestick history and report whether it carries quotes, not just trades.

This decides how the calibration study gets its price side. The study needs, for each moment before
expiry, the market's implied probability — i.e. an **ask** — paired with the index distance from the
strike and the eventual outcome. Two very different worlds:

* candlesticks include bid/ask -> months of history are already available, and the study is a
  backtest we can run immediately;
* candlesticks only include traded prices -> a thin market's last trade can be stale or absent, so
  the price side has to come from live capture, and the study waits on data collection.

Read-only: GETs against Kalshi's production market-data API, the same carve-out `fetch_ground_truth.py`
relies on (reading public history is a different privilege from routing orders).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scripts.fetch_ground_truth import KALSHI_PROD_MARKET_DATA, _get

# Documented shape; probed rather than trusted, which is how the order book schema turned out wrong.
CANDLESTICK_PATH = "/series/{series}/markets/{ticker}/candlesticks"
QUOTE_KEYS = ("yes_bid", "yes_ask", "no_bid", "no_ask", "bid", "ask")


def _series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def _pick_market(markets_path: Path, prefer_traded: bool = True) -> dict:
    """Choose a market to probe, preferring one that actually traded — an untraded market has no
    price history at all, which would look like a missing endpoint rather than an empty market."""
    candidates: list[dict] = []
    with markets_path.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if prefer_traded and float(record.get("volume") or 0) <= 0:
                continue
            candidates.append(record)
            if len(candidates) >= 50:
                break
    if not candidates:
        raise SystemExit(
            f"No traded markets found in {markets_path}. Re-run with --ticker to probe one directly."
        )
    # The busiest of the sample gives the best chance of a populated series.
    return max(candidates, key=lambda record: float(record.get("volume") or 0))


def probe(ticker: str, close_ts: float, period_interval: int, hours_before: float) -> None:
    series = _series_of(ticker)
    path = CANDLESTICK_PATH.format(series=series, ticker=ticker)
    url = f"{KALSHI_PROD_MARKET_DATA}{path}"
    start_ts = int(close_ts - hours_before * 3600)
    end_ts = int(close_ts)
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}

    print(f"GET {url}")
    print(f"    start_ts={start_ts} end_ts={end_ts} period_interval={period_interval}")
    print(f"    ({datetime.fromtimestamp(start_ts, tz=timezone.utc)} .. "
          f"{datetime.fromtimestamp(end_ts, tz=timezone.utc)} UTC)\n")

    with httpx.Client(timeout=30.0) as client:
        try:
            payload = _get(client, url, params)
        except httpx.HTTPStatusError as error:
            print(f"HTTP {error.response.status_code}: {error.response.text[:400]}")
            print("\nIf this is a 404, the path or parameter names differ from the documented shape.")
            return

    candles = payload.get("candlesticks") if isinstance(payload, dict) else None
    if candles is None:
        print("Response had no 'candlesticks' key. Raw payload (truncated):")
        print(json.dumps(payload, indent=2)[:3000])
        return

    print(f"Returned {len(candles)} candle(s).")
    if not candles:
        print("Empty series — try a different market, a wider --hours-before, or --period 60.")
        return

    print("\n--- first candle ---")
    print(json.dumps(candles[0], indent=2))
    print("\n--- last candle ---")
    print(json.dumps(candles[-1], indent=2))

    present = sorted({key for candle in candles for key in candle if key in QUOTE_KEYS})
    print("\n" + "=" * 70)
    if present:
        populated = [
            key for key in present
            if any(candle.get(key) not in (None, {}, []) for candle in candles)
        ]
        print(f"Quote fields present: {present}")
        print(f"Quote fields actually populated: {populated or 'NONE — present but empty'}")
        if populated:
            print(
                "\nVERDICT: candlesticks carry quotes. The calibration study can run against\n"
                "months of history immediately — no waiting on live capture."
            )
        else:
            print(
                "\nVERDICT: quote fields exist but are empty for this market. Try a busier market\n"
                "before concluding; if they are always empty, the price side needs live capture."
            )
    else:
        print(f"No quote fields found. Keys seen: {sorted(candles[0].keys())}")
        print(
            "\nVERDICT: trade prices only. A thin market's last trade can be stale or missing, so\n"
            "the study's price side has to come from live capture."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", default="data/settled.jsonl", help="JSONL from fetch_ground_truth markets")
    parser.add_argument("--ticker", default=None, help="Probe this market ticker directly.")
    parser.add_argument("--close-time", default=None, help="ISO close time, required with --ticker.")
    parser.add_argument("--period", type=int, default=1, help="period_interval in minutes (1, 60, 1440).")
    parser.add_argument("--hours-before", type=float, default=2.0, help="How far back from close to request.")
    args = parser.parse_args()

    if args.ticker:
        if not args.close_time:
            raise SystemExit("--ticker requires --close-time (ISO 8601, e.g. 2026-07-29T22:00:00Z)")
        ticker = args.ticker
        close_ts = datetime.fromisoformat(args.close_time.replace("Z", "+00:00")).timestamp()
    else:
        market = _pick_market(Path(args.markets))
        ticker = market["ticker"]
        close_ts = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp()
        print(f"Probing busiest traded market from {args.markets}: {ticker} "
              f"(volume {market.get('volume')})\n")

    probe(ticker, close_ts, args.period, args.hours_before)


if __name__ == "__main__":
    main()
