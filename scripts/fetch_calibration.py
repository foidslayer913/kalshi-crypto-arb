"""Fetch quote history + outcomes for a calibration study.

The question: when the market offers a side at price P, how often does that side actually win? If
realized frequency systematically exceeds `P + fee`, buying at P is profitable — and the *shape* of
the discrepancy across P matters more than any single point. A constant offset is a spread or fee
artifact; error that grows toward the extremes is the favorite-longshot bias, which is tradeable.

For each settled market in a series this writes one observation per 1-minute candle:

    ticker, minutes_to_close, yes_ask, yes_bid, result, volume, strike, expiration_value

Each observation supports two tradeable propositions, which is how both tails get covered:

* buy YES at `yes_ask`      -> wins when result == "yes"
* buy NO  at `1 - yes_bid`  -> wins when result == "no"   (a NO ask is 100 - the YES bid)

Only candle **close** prices are used. A candle's high/low are not knowable in advance, so sampling
them would quietly smuggle in look-ahead; the close at each minute boundary is a rule a live bot
could actually follow.

Read-only: GETs against Kalshi's production market-data API.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx

from scripts.fetch_ground_truth import (
    KALSHI_PROD_MARKET_DATA,
    SETTLEMENT_VALUE_KEYS,
    VOLUME_KEYS,
    _first_number,
    _get,
    _log,
    _to_unix,
    iter_settled_markets,
)

CANDLESTICK_PATH = "/series/{series}/markets/{ticker}/candlesticks"


def _dollars(node: Any, key: str = "close_dollars") -> float | None:
    """Pull a dollar figure out of a candle sub-object. Kalshi sends these as strings."""
    if not isinstance(node, dict):
        return None
    value = node.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strike_of(market: dict) -> float | None:
    strike = market.get("floor_strike")
    if strike is None:
        strike = market.get("cap_strike")
    try:
        return None if strike is None else float(strike)
    except (TypeError, ValueError):
        return None


def fetch_candles(
    client: httpx.Client, series: str, ticker: str, start_ts: int, end_ts: int, period_interval: int
) -> list[dict]:
    url = f"{KALSHI_PROD_MARKET_DATA}{CANDLESTICK_PATH.format(series=series, ticker=ticker)}"
    params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
    payload = _get(client, url, params)
    if not isinstance(payload, dict):
        return []
    return payload.get("candlesticks") or []


def observations_for_market(
    client: httpx.Client, series: str, market: dict, period_interval: int
) -> Iterator[dict]:
    result = (market.get("result") or "").lower()
    if result not in ("yes", "no"):
        return  # void/unresolved markets carry no label to calibrate against
    close_iso = market.get("close_time")
    open_iso = market.get("open_time")
    if not close_iso:
        return
    close_ts = _to_unix(close_iso)
    # Pad the start: a candle series is requested by time range, and an exact open boundary can drop
    # the first candle.
    start_ts = int(_to_unix(open_iso) - 60) if open_iso else int(close_ts - 3600)

    candles = fetch_candles(client, series, market["ticker"], start_ts, int(close_ts), period_interval)
    strike = _strike_of(market)
    expiration_value = _first_number(market, SETTLEMENT_VALUE_KEYS)
    for candle in candles:
        end_ts = candle.get("end_period_ts")
        if end_ts is None:
            continue
        yes_ask = _dollars(candle.get("yes_ask"))
        yes_bid = _dollars(candle.get("yes_bid"))
        if yes_ask is None and yes_bid is None:
            continue
        yield {
            "ticker": market["ticker"],
            "result": result,
            "minutes_to_close": round((close_ts - float(end_ts)) / 60.0, 3),
            "yes_ask": yes_ask,
            "yes_bid": yes_bid,
            "trade_price": _dollars(candle.get("price")),
            "candle_volume": _first_number(candle, VOLUME_KEYS),
            "strike": strike,
            "expiration_value": expiration_value,
            "close_time": close_iso,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series-ticker", required=True, help="e.g. KXBTC15M")
    parser.add_argument("--start", required=True, help="Earliest close date, e.g. 2026-07-01")
    parser.add_argument("--end", required=True, help="Latest close date, e.g. 2026-07-30")
    parser.add_argument("-o", "--out", default="data/calibration.jsonl")
    parser.add_argument("--period", type=int, default=1, help="Candle period_interval in minutes.")
    parser.add_argument("--max-markets", type=int, default=None, help="Stop after this many markets.")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    min_close = int(_to_unix(args.start))
    max_close = int(_to_unix(args.end))
    _log(f"Fetching settled {args.series_ticker} markets closing {args.start}..{args.end}")

    markets_seen = 0
    observations = 0
    skipped_no_candles = 0
    with httpx.Client(timeout=30.0) as client, out_path.open("w") as handle:
        for market in iter_settled_markets(
            client, KALSHI_PROD_MARKET_DATA, args.series_ticker, min_close, max_close
        ):
            if args.max_markets is not None and markets_seen >= args.max_markets:
                break
            markets_seen += 1
            rows = list(observations_for_market(client, args.series_ticker, market, args.period))
            if not rows:
                skipped_no_candles += 1
            for row in rows:
                handle.write(json.dumps(row) + "\n")
                observations += 1
            if markets_seen % 25 == 0:
                _log(f"  {markets_seen} markets, {observations} observations")

    _log(
        f"\nWrote {observations} observations from {markets_seen} markets to {out_path}"
        f" ({skipped_no_candles} markets had no usable candles)."
    )
    if observations == 0:
        raise SystemExit(
            "No observations written. Check the series ticker and that the date range contains "
            "settled markets."
        )
    print(f"\nNext: python -m backtest calibration --observations {out_path}")


if __name__ == "__main__":
    main()
