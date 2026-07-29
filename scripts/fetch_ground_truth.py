#!/usr/bin/env python3
"""Fetch the two inputs the Tier 2 backtest needs. Run this on your own machine.

    # 0. See what Kalshi actually returns before trusting the parser below
    python scripts/fetch_ground_truth.py inspect --series-ticker KXBTCD

    # 1. Settled markets (ground truth labels)
    python scripts/fetch_ground_truth.py markets \
        --series-ticker KXBTCD --start 2026-07-01 --end 2026-07-20 -o data/settled.jsonl

    # 2. 1 Hz index series, fetched only for each market's settlement window
    python scripts/fetch_ground_truth.py series --markets data/settled.jsonl --out-dir data/

    # 3. Score it
    python -m backtest signal --markets data/settled.jsonl --series BTC-USD=data/BTC-USD.csv

READ-ONLY. This script issues GET requests against market-data endpoints and nothing else; it
never places, cancels, or queries orders. It deliberately does not go through `config.Settings`,
because settled *crypto* history lives on Kalshi's production market data — the demo sandbox's
settlements are synthetic and would tell you nothing about real index divergence. That is exactly
why the demo-only guardrail in `config.py` stays where it is and is not reused here: reading
public settlement history and routing orders are different privileges, and only the latter is
what the guardrail exists to constrain.

Why Binance for the index series: the settlement windows are 60 seconds, and Coinbase's public
historical endpoints bottom out at 60-second candles — one candle per window, which is useless.
Binance's aggTrades accepts an explicit time range, so each window costs a couple of requests.
See the note in `series` about what this does and does not measure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

KALSHI_PROD_MARKET_DATA = "https://api.elections.kalshi.com/trade-api/v2"
BINANCE_DATA_API = "https://data-api.binance.vision"

DEFAULT_PREFIX_TO_SYMBOL = {"KXBTC": "BTC-USD", "KXETH": "ETH-USD"}
DEFAULT_SYMBOL_TO_BINANCE = {"BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT"}

# The settled index level lives in `expiration_value` (a string, e.g. "64398.56").
#
# Do NOT reach for `settlement_value_dollars`: that is the per-contract *payout* (0.0000 when the
# market resolved no, 1.0000 when yes), not the BRTI level the strike is compared against. Using
# it would silently make every proxy-divergence number garbage while still looking plausible.
SETTLEMENT_VALUE_KEYS = ("expiration_value", "settlement_value", "settled_value")

# Cumulative contracts traded over the market's life. A market with zero volume never traded at
# all, so no signal in it was executable regardless of how good the signal was. `liquidity_dollars`
# is deliberately not used: on a finalized market the book is already torn down, so it reads 0
# whether or not there was depth during the settlement window.
VOLUME_KEYS = ("volume_fp", "volume")
OPEN_INTEREST_KEYS = ("open_interest_fp", "open_interest")


def _log(message: str) -> None:
    print(message, file=sys.stderr)


_last_request_at = 0.0


def _throttle(min_interval: float) -> None:
    """Space requests out. Staying under the limit beats backing off after hitting it."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_request_at = time.monotonic()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _get(
    client: httpx.Client, url: str, params: dict[str, Any],
    *, min_interval: float = 0.5, attempts: int = 6,
) -> dict | list:
    """GET with throttling and backoff. The only verb this script uses.

    429 gets a much longer backoff than a transient 5xx: a rate limit means "you are asking too
    often", and retrying a second later just re-triggers it. Honours Retry-After when sent.
    """
    rate_limit_delay = 5.0
    transient_delay = 1.0
    for attempt in range(1, attempts + 1):
        _throttle(min_interval)
        response = client.get(url, params=params)

        if response.status_code == 429:
            if attempt == attempts:
                response.raise_for_status()
            wait = _retry_after_seconds(response) or rate_limit_delay
            _log(f"  rate limited (attempt {attempt}/{attempts}); waiting {wait:.0f}s")
            time.sleep(wait)
            rate_limit_delay = min(rate_limit_delay * 2, 60.0)
            continue

        if response.status_code in (500, 502, 503, 504):
            if attempt == attempts:
                response.raise_for_status()
            _log(f"  HTTP {response.status_code} (attempt {attempt}/{attempts}); "
                 f"retrying in {transient_delay:.0f}s")
            time.sleep(transient_delay)
            transient_delay = min(transient_delay * 2, 30.0)
            continue

        response.raise_for_status()
        return response.json()
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- Kalshi settled markets


def iter_settled_markets(
    client: httpx.Client, base_url: str, series_ticker: str,
    min_close_ts: int | None, max_close_ts: int | None, page_size: int = 1000,
    status: str = "settled", min_interval: float = 0.5,
) -> Iterator[dict]:
    """Page through settled markets for one series.

    Note the returned objects carry `status: "finalized"` even when queried with
    `status=settled` — Kalshi treats the filter as covering both. `--status` exists so that can be
    changed without a code edit if the counts ever look wrong.

    Pages are requested as large as the API allows, because the binding constraint here is
    requests-per-second, not bytes. A cursor that repeats is treated as the end of the data rather
    than followed, so a non-advancing cursor cannot spin this into an endless request loop.
    """
    cursor: str | None = None
    seen_cursors: set[str] = set()
    page = 0
    while True:
        params: dict[str, Any] = {"series_ticker": series_ticker, "status": status, "limit": page_size}
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts
        if cursor:
            params["cursor"] = cursor
        payload = _get(client, f"{base_url}/markets", params, min_interval=min_interval)
        markets = payload.get("markets", []) if isinstance(payload, dict) else []
        page += 1
        _log(f"  page {page}: {len(markets)} markets")
        yield from markets

        cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not cursor or not markets:
            return
        if cursor in seen_cursors:
            _log("  cursor stopped advancing; treating this as the end of the data")
            return
        seen_cursors.add(cursor)


def resolve_symbol(ticker: str, prefix_map: dict[str, str]) -> str | None:
    for prefix, symbol in prefix_map.items():
        if ticker.startswith(prefix):
            return symbol
    return None


def convert_market(raw: dict, prefix_map: dict[str, str]) -> dict | None:
    """Map a raw Kalshi market onto the shape `backtest.reconstruct` loads.

    Returns None (with a reason logged) for markets that cannot be scored, rather than guessing.
    """
    ticker = raw.get("ticker")
    if not ticker:
        return None

    strike_type = raw.get("strike_type")
    if strike_type == "greater":
        strike_price = raw.get("floor_strike")
    elif strike_type == "less":
        strike_price = raw.get("cap_strike")
    else:
        return None  # ranged/scalar markets are out of scope for this invariant
    if strike_price is None:
        return None

    result = (raw.get("result") or "").lower()
    if result not in ("yes", "no"):
        return None  # unsettled, voided, or a shape we do not understand

    symbol = resolve_symbol(ticker, prefix_map)
    if symbol is None:
        return None

    close_time = raw.get("close_time")
    if not close_time:
        return None

    record = {
        "ticker": ticker,
        "strike_type": strike_type,
        "strike_price": float(strike_price),
        "close_time": close_time,
        "result": result,
        "crypto_symbol": symbol,
    }
    # Numeric fields arrive as strings on this API ("64398.56"), so everything goes through float.
    settlement_value = _first_number(raw, SETTLEMENT_VALUE_KEYS)
    if settlement_value is not None:
        record["settlement_value"] = settlement_value
    volume = _first_number(raw, VOLUME_KEYS)
    if volume is not None:
        record["volume"] = volume
    open_interest = _first_number(raw, OPEN_INTEREST_KEYS)
    if open_interest is not None:
        record["open_interest"] = open_interest
    return record


def _first_number(raw: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = raw.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def cmd_markets(args: argparse.Namespace) -> None:
    prefix_map = _parse_mapping(args.prefix_map, DEFAULT_PREFIX_TO_SYMBOL)
    # Kalshi parses these as int64; a float renders as "1785283200.0" and is rejected outright.
    min_ts = int(_to_unix(args.start)) if args.start else None
    max_ts = int(_to_unix(args.end)) if args.end else None

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    kept = skipped = 0
    no_settlement_value = zero_volume = 0
    with httpx.Client(timeout=30.0, headers=_auth_headers_if_available()) as client, out_path.open("w") as handle:
        for series_ticker in args.series_ticker:
            _log(f"Fetching settled markets for {series_ticker}...")
            for raw in iter_settled_markets(
                client, args.base_url, series_ticker, min_ts, max_ts,
                page_size=args.page_size, status=args.status, min_interval=args.rate_limit,
            ):
                record = convert_market(raw, prefix_map)
                if record is None:
                    skipped += 1
                    continue
                if "settlement_value" not in record:
                    no_settlement_value += 1
                if record.get("volume", 0.0) == 0.0:
                    zero_volume += 1
                handle.write(json.dumps(record) + "\n")
                kept += 1

    _log(f"\nWrote {kept} markets to {out_path} ({skipped} skipped as unscoreable)")
    if kept == 0:
        _log("Nothing was written. Run the `inspect` command to see the raw payload shape.")
        return
    if no_settlement_value:
        _log(
            f"{no_settlement_value} markets had no settlement value under any of "
            f"{SETTLEMENT_VALUE_KEYS}. False-positive rate still works without it; only the "
            f"proxy-divergence magnitude needs it."
        )
    if zero_volume:
        _log(
            f"{zero_volume}/{kept} markets ({zero_volume / kept * 100:.0f}%) never traded a single "
            f"contract. No signal in those was executable at any price, so treat them as a ceiling "
            f"on opportunity, not as tradeable inventory."
        )


def cmd_inspect(args: argparse.Namespace) -> None:
    """Dump the first raw market object so the parser can be reconciled against reality."""
    with httpx.Client(timeout=30.0, headers=_auth_headers_if_available()) as client:
        payload = _get(
            client, f"{args.base_url}/markets",
            {"series_ticker": args.series_ticker, "status": "settled", "limit": 3},
        )
    if not isinstance(payload, dict):
        _log(f"Unexpected top-level type {type(payload).__name__}; raw response:")
        print(json.dumps(payload, indent=2)[:4000])
        return
    _log(f"Top-level keys: {sorted(payload.keys())}")
    markets = payload.get("markets") or []
    if not markets:
        _log("No settled markets returned. Check the series ticker and that history exists.")
        print(json.dumps(payload, indent=2)[:4000])
        return
    _log(f"{len(markets)} markets returned; first one:\n")
    print(json.dumps(markets[0], indent=2))
    _log(f"\nKeys present: {sorted(markets[0].keys())}")
    missing = [key for key in ("ticker", "strike_type", "close_time", "result") if key not in markets[0]]
    if missing:
        _log(f"MISSING expected keys: {missing} -- convert_market() needs updating.")


# --------------------------------------------------------------------------- index series


def bucket_trades_to_seconds(trades: list[tuple[int, float]]) -> dict[int, float]:
    """Reduce (timestamp_ms, price) trades to one price per second.

    Each bucket is stamped at the *end* of its second and carries that second's last trade, so a
    reader querying at time T only ever sees trades that had already happened by T. Stamping at
    the start of the second would leak up to a second of future prices into the window.
    """
    buckets: dict[int, float] = {}
    for timestamp_ms, price in sorted(trades):
        buckets[timestamp_ms // 1000 + 1] = price
    return buckets


def fetch_agg_trades(
    client: httpx.Client, symbol: str, start_ms: int, end_ms: int, min_interval: float = 0.12,
) -> list[tuple[int, float]]:
    """Fetch Binance aggregate trades in [start_ms, end_ms], paging past the 1000-row cap."""
    trades: list[tuple[int, float]] = []
    cursor = start_ms
    while cursor < end_ms:
        payload = _get(
            client, f"{BINANCE_DATA_API}/api/v3/aggTrades",
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000},
            min_interval=min_interval,
        )
        if not isinstance(payload, list) or not payload:
            break
        for row in payload:
            trades.append((int(row["T"]), float(row["p"])))
        last = int(payload[-1]["T"])
        if len(payload) < 1000 or last <= cursor:
            break
        cursor = last + 1
    return trades


def cmd_series(args: argparse.Namespace) -> None:
    symbol_map = _parse_mapping(args.binance_map, DEFAULT_SYMBOL_TO_BINANCE)
    markets = [json.loads(line) for line in Path(args.markets).read_text().splitlines() if line.strip()]
    if not markets:
        raise SystemExit(f"No markets found in {args.markets}")

    windows: dict[str, set[int]] = {}
    for market in markets:
        symbol = market["crypto_symbol"]
        close_ts = int(_to_unix(market["close_time"]))
        windows.setdefault(symbol, set()).add(close_ts)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=30.0) as client:
        for symbol, close_times in sorted(windows.items()):
            binance_symbol = symbol_map.get(symbol)
            if binance_symbol is None:
                _log(f"No Binance symbol mapped for {symbol}; skipping")
                continue

            buckets: dict[int, float] = {}
            ordered = sorted(close_times)
            _log(f"Fetching {len(ordered)} settlement windows for {symbol} ({binance_symbol})...")
            for index, close_ts in enumerate(ordered, start=1):
                start_ms = (close_ts - args.pad_seconds) * 1000
                end_ms = close_ts * 1000
                try:
                    trades = fetch_agg_trades(
                        client, binance_symbol, start_ms, end_ms, min_interval=args.rate_limit
                    )
                except httpx.HTTPError as error:
                    _log(f"  window {index}/{len(ordered)} failed: {error}")
                    continue
                buckets.update(bucket_trades_to_seconds(trades))
                if index % 25 == 0 or index == len(ordered):
                    _log(f"  {index}/{len(ordered)} windows, {len(buckets)} seconds collected")

            out_path = out_dir / f"{symbol}.csv"
            with out_path.open("w") as handle:
                handle.write("timestamp,price\n")
                for second in sorted(buckets):
                    handle.write(f"{second},{buckets[second]}\n")
            _log(f"Wrote {len(buckets)} rows to {out_path}\n")

    _log(
        "Note: this series is Binance, while the live bot polls Coinbase. It measures the "
        "magnitude of single-venue divergence from Kalshi's settlement index during the window, "
        "which is the right first estimate -- if it is large, no single-venue proxy is safe. To "
        "measure the bot's own Coinbase feed exactly, use the Tier 1 capture going forward."
    )


# --------------------------------------------------------------------------- helpers


def _parse_mapping(entries: list[str] | None, default: dict[str, str]) -> dict[str, str]:
    if not entries:
        return dict(default)
    mapping = {}
    for entry in entries:
        key, _, value = entry.partition("=")
        if not value:
            raise SystemExit(f"Expected KEY=VALUE, got {entry!r}")
        mapping[key.strip()] = value.strip()
    return mapping


def _to_unix(value: str) -> float:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _auth_headers_if_available() -> dict[str, str]:
    """Sign requests if credentials happen to be configured; market data is public without them."""
    import os

    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path or not Path(key_path).exists():
        return {}
    try:
        from ingestion.kalshi_auth import auth_headers, load_private_key

        private_key = load_private_key(Path(key_path).read_bytes())
        return auth_headers(private_key, key_id, "GET", "/trade-api/v2/markets")
    except Exception as error:  # signing is optional; never block a public read on it
        _log(f"Could not sign requests ({error}); continuing unauthenticated")
        return {}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fetch_ground_truth", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url", default=KALSHI_PROD_MARKET_DATA,
        help=f"Kalshi market-data base URL, read-only (default: {KALSHI_PROD_MARKET_DATA})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="Dump a raw settled market to check the schema")
    inspect.add_argument("--series-ticker", required=True)

    markets = subparsers.add_parser("markets", help="Write settled markets as JSONL")
    markets.add_argument("--series-ticker", required=True, action="append")
    markets.add_argument("--start", help="ISO date/datetime (UTC)")
    markets.add_argument(
        "--end",
        help="ISO date/datetime (UTC). A bare date means midnight, so pass the following day to "
             "include a full final day of markets.",
    )
    markets.add_argument("-o", "--output", default="data/settled.jsonl")
    markets.add_argument("--prefix-map", action="append", help="KXBTC=BTC-USD, repeatable")
    markets.add_argument(
        "--status", default="settled",
        help="Kalshi status filter (default: settled, which also returns finalized markets)",
    )
    markets.add_argument(
        "--page-size", type=int, default=1000,
        help="Markets per request (default: 1000). Bigger pages mean fewer requests.",
    )
    markets.add_argument(
        "--rate-limit", type=float, default=0.5,
        help="Minimum seconds between requests (default: 0.5). Raise this if you keep hitting 429.",
    )

    series = subparsers.add_parser("series", help="Write 1 Hz index series for each settlement window")
    series.add_argument("--markets", required=True, help="JSONL produced by the `markets` command")
    series.add_argument("--out-dir", default="data")
    series.add_argument("--pad-seconds", type=int, default=120)
    series.add_argument("--binance-map", action="append", help="BTC-USD=BTCUSDT, repeatable")
    series.add_argument(
        "--rate-limit", type=float, default=0.12,
        help="Minimum seconds between requests (default: 0.12). Raise this if you hit 429.",
    )

    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        if args.command == "inspect":
            cmd_inspect(args)
        elif args.command == "markets":
            cmd_markets(args)
        else:
            cmd_series(args)
    except httpx.HTTPStatusError as error:
        raise SystemExit(
            f"\n{error.request.url.host} returned HTTP {error.response.status_code}.\n"
            f"  400     -> a parameter was rejected; the body below names which one\n"
            f"  403/401 -> this endpoint may need signed requests; set KALSHI_API_KEY_ID and\n"
            f"             KALSHI_PRIVATE_KEY_PATH, and run via `python -m scripts.fetch_ground_truth`\n"
            f"  404     -> check --base-url and the series ticker\n"
            f"Request: {error.request.url}\n"
            f"Body: {error.response.text[:300]}"
        ) from error
    except httpx.HTTPError as error:
        raise SystemExit(
            f"\nCould not reach {getattr(getattr(error, 'request', None), 'url', 'the API')}: {error}\n"
            f"This script needs direct outbound HTTPS. If you are behind a corporate proxy or a\n"
            f"restricted network, run it somewhere with open egress -- a sandbox that blocks\n"
            f"api.elections.kalshi.com or data-api.binance.vision cannot produce these files."
        ) from error
    except KeyboardInterrupt:
        raise SystemExit("\nInterrupted.") from None


if __name__ == "__main__":
    main()
