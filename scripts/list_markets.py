"""Read-only probe: list open Kalshi crypto markets for a series.

This exists to answer one question before any capture is wired up: *what markets does this account
actually see, and what do their books look like?* The 404s we hit came from guessing tickers
(`KXBTC`) that don't exist; the real crypto series is `KXBTCD`. Rather than keep guessing, ask the
venue.

It is strictly read-only — GETs against the public/authenticated `markets` endpoint, never an order
path. It deliberately does NOT go through `config.Settings` (which pins the URL to demo, and whose
guardrail is about *order routing*, a different privilege from reading public market data — the same
carve-out `fetch_ground_truth.py` relies on). The base URL defaults to Demo and is overridable, so
the same tool can inspect Live books read-only. Credentials are still loaded from the local `.env`.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx
from dotenv import load_dotenv

from ingestion.kalshi_auth import auth_headers, load_private_key

DEMO_BASE_URL = "https://external-api.demo.kalshi.co"
LIVE_BASE_URL = "https://api.elections.kalshi.com"
MARKETS_PATH = "/trade-api/v2/markets"
ORDERBOOK_PATH_TEMPLATE = "/trade-api/v2/markets/{ticker}/orderbook"


def _load_credentials() -> tuple[str, object]:
    load_dotenv()
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key_id or not key_path:
        sys.exit("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in .env")
    with open(key_path, "rb") as handle:
        private_key = load_private_key(handle.read())
    return api_key_id, private_key


def list_markets(
    base_url: str,
    api_key_id: str,
    private_key: object,
    series_ticker: str,
    status: str,
    limit: int,
) -> list[dict]:
    """Fetch markets for a series. Kalshi signs the path only, so query params stay out of the
    signature. Follows the cursor to the end."""
    markets: list[dict] = []
    cursor: str | None = None
    with httpx.Client(base_url=base_url, timeout=15.0) as client:
        while True:
            headers = auth_headers(private_key, api_key_id, "GET", MARKETS_PATH)
            params = {"series_ticker": series_ticker, "status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            response = client.get(MARKETS_PATH, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
            page = payload.get("markets", [])
            markets.extend(page)
            cursor = payload.get("cursor") or None
            if not cursor or not page:
                break
    return markets


def fetch_orderbook(base_url: str, api_key_id: str, private_key: object, ticker: str) -> dict:
    """Fetch a single market's resting order book. This is where quote/depth actually lives — the
    markets list endpoint doesn't carry it. Answers whether there's anything to trade against."""
    path = ORDERBOOK_PATH_TEMPLATE.format(ticker=ticker)
    with httpx.Client(base_url=base_url, timeout=15.0) as client:
        headers = auth_headers(private_key, api_key_id, "GET", path)
        response = client.get(path, headers=headers)
        response.raise_for_status()
        return response.json()


def _print_orderbook(ticker: str, payload: dict) -> None:
    book = payload.get("orderbook", payload)
    yes = book.get("yes") or []
    no = book.get("no") or []
    print(f"Order book for {ticker}:")
    print(f"  yes side (bids, [price_cents, contracts]): {yes if yes else 'EMPTY'}")
    print(f"  no  side (bids, [price_cents, contracts]): {no if no else 'EMPTY'}")
    if not yes and not no:
        print("\n  Both sides empty — no resting liquidity to trade against here.")
    else:
        # Implied YES ask = 100 - best NO bid; implied NO ask = 100 - best YES bid.
        best_yes_bid = max((level[0] for level in yes), default=None)
        best_no_bid = max((level[0] for level in no), default=None)
        if best_no_bid is not None:
            print(f"\n  implied YES ask = 100 - {best_no_bid} = {100 - best_no_bid}c")
        if best_yes_bid is not None:
            print(f"  implied NO  ask = 100 - {best_yes_bid} = {100 - best_yes_bid}c")


def _print_table(markets: list[dict]) -> None:
    if not markets:
        print("No markets returned.")
        return
    header = f"{'ticker':<32}{'status':>10}{'strike':>12}{'yes_bid':>9}{'yes_ask':>9}{'vol':>8}{'close_time':>22}"
    print(header)
    print("-" * len(header))
    for market in markets:
        strike = market.get("floor_strike") or market.get("cap_strike") or "-"
        print(
            f"{market.get('ticker', '?'):<32}"
            f"{market.get('status', '?'):>10}"
            f"{str(strike):>12}"
            f"{str(market.get('yes_bid', '-')):>9}"
            f"{str(market.get('yes_ask', '-')):>9}"
            f"{str(market.get('volume', '-')):>8}"
            f"{str(market.get('close_time', '-')):>22}"
        )
    print(f"\n{len(markets)} market(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description="List Kalshi markets for a series (read-only).")
    parser.add_argument("--series-ticker", default="KXBTCD", help="e.g. KXBTCD (BTC), KXETHD (ETH)")
    parser.add_argument("--status", default="open", help="open | closed | settled | unopened")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--env",
        choices=("demo", "live"),
        default="demo",
        help="Which Kalshi environment's market data to read. 'live' is READ-ONLY here — no orders.",
    )
    parser.add_argument("--base-url", default=None, help="Override the base URL entirely.")
    parser.add_argument(
        "--orderbook",
        default=None,
        metavar="TICKER",
        help="Instead of listing, fetch one market's resting order book (real depth lives here).",
    )
    args = parser.parse_args()

    base_url = args.base_url or (LIVE_BASE_URL if args.env == "live" else DEMO_BASE_URL)
    api_key_id, private_key = _load_credentials()

    if args.orderbook:
        print(f"Reading order book for {args.orderbook} from {base_url}\n")
        payload = fetch_orderbook(base_url, api_key_id, private_key, args.orderbook)
        _print_orderbook(args.orderbook, payload)
        return

    print(f"Reading {args.status} markets for series {args.series_ticker} from {base_url}\n")
    markets = list_markets(base_url, api_key_id, private_key, args.series_ticker, args.status, args.limit)
    _print_table(markets)


if __name__ == "__main__":
    main()
