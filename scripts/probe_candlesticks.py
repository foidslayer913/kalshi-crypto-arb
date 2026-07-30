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

from scripts.fetch_ground_truth import (
    KALSHI_PROD_MARKET_DATA,
    VOLUME_KEYS,
    _first_number,
    _get,
    iter_settled_markets,
)


def _volume(market: dict) -> float:
    """Traded contracts. Kalshi reports this as `volume_fp` (a fixed-point string); reading a plain
    `volume` key finds nothing and makes every market look untraded."""
    return _first_number(market, VOLUME_KEYS) or 0.0

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
            if prefer_traded and _volume(record) <= 0:
                continue
            candidates.append(record)
            if len(candidates) >= 50:
                break
    if not candidates:
        raise SystemExit(
            f"No traded markets found in {markets_path}. Re-run with --ticker to probe one directly."
        )
    # The busiest of the sample gives the best chance of a populated series.
    return max(candidates, key=_volume)


STRUCTURE_KEYS = (
    "ticker", "event_ticker", "market_type", "strike_type", "floor_strike", "cap_strike",
    "custom_strike", "settlement_timer_seconds", "close_time", "open_time", "result",
    "expiration_value", "volume", "open_interest", "rules_primary",
)


def pick_from_series(series_ticker: str, lookback_hours: float, limit: int = 400) -> dict:
    """Most-traded recently-settled market in a series.

    Recurring short-duration series list a fresh ticker every period — a 15-minute series turns over
    96 tickers a day — so the useful unit is "the series", not any single ticker. Resolving a
    representative settled market here keeps that churn out of the caller's hands.
    """
    now = datetime.now(timezone.utc).timestamp()
    candidates: list[dict] = []
    with httpx.Client(timeout=30.0) as client:
        for market in iter_settled_markets(
            client, KALSHI_PROD_MARKET_DATA, series_ticker,
            int(now - lookback_hours * 3600), int(now),
        ):
            candidates.append(market)
            if len(candidates) >= limit:
                break
    if not candidates:
        raise SystemExit(
            f"No settled markets for series {series_ticker!r} in the last {lookback_hours:.0f}h. "
            "Try a longer --lookback-hours, or check the series ticker."
        )
    traded = [m for m in candidates if _volume(m) > 0]
    if traded:
        return max(traded, key=_volume)
    # Volume is only a heuristic for "most likely to have populated history". The structural and
    # candlestick-schema questions are still worth answering, so probe the latest market rather than
    # giving up — an empty candle series is itself a useful answer.
    print(
        f"note: none of the {len(candidates)} recent settled {series_ticker} markets report volume; "
        "probing the most recent one anyway.\n"
    )
    return max(candidates, key=lambda record: record.get("close_time", ""))


def describe_structure(market: dict) -> None:
    """Report the fields that decide whether the existing strategy code fits this series."""
    print("--- market structure ---")
    for key in STRUCTURE_KEYS:
        if key in market:
            value = market[key]
            if isinstance(value, str) and len(value) > 200:
                value = value[:200] + "..."
            print(f"  {key:<28}{value}")
    missing = [key for key in ("strike_type", "settlement_timer_seconds") if key not in market]
    if missing:
        print(f"  (absent from payload: {', '.join(missing)})")

    timer = market.get("settlement_timer_seconds")
    strike_type = market.get("strike_type")
    print()
    if timer is not None and timer != 60:
        print(
            f"  NOTE settlement_timer_seconds={timer}, not 60. The math engine's window is 60, so\n"
            f"       backtests on this series need --window-size {timer}."
        )
    elif timer == 60:
        print("  settlement_timer_seconds=60 — matches the 60-second window the math engine assumes.")
    if strike_type not in ("greater", "less"):
        print(
            f"  NOTE strike_type={strike_type!r} is not the greater/less shape kalshi_rest._parse_market\n"
            f"       handles; this series needs its strike derived differently."
        )
    print()


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
    parser.add_argument(
        "--series-ticker", default=None,
        help="Probe a live series, e.g. KXBTC15M. Resolves a recently-settled market for you.",
    )
    parser.add_argument("--lookback-hours", type=float, default=24.0, help="How far back to look for a settled market.")
    parser.add_argument("--markets", default=None, help="JSONL from fetch_ground_truth markets")
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
        if args.series_ticker:
            market = pick_from_series(args.series_ticker, args.lookback_hours)
            source = f"series {args.series_ticker}"
        else:
            market = _pick_market(Path(args.markets or "data/settled.jsonl"))
            source = args.markets or "data/settled.jsonl"
        ticker = market["ticker"]
        close_ts = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp()
        print(f"Probing busiest traded market from {source}: {ticker} "
              f"(volume {_volume(market):.0f})\n")
        describe_structure(market)

    probe(ticker, close_ts, args.period, args.hours_before)


if __name__ == "__main__":
    main()
