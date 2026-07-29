"""CLI for the Tier 2 signal-correctness backtest.

    python -m backtest --markets settled.jsonl --series BTC-USD=btc_1s.csv
"""

from __future__ import annotations

import argparse

from backtest.reconstruct import (
    DEFAULT_VARIANTS,
    Variant,
    format_summaries,
    load_price_series,
    load_settled_markets,
    reconstruct,
    summarize,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="backtest", description=__doc__)
    parser.add_argument("--markets", required=True, help="JSONL of settled markets (ground truth)")
    parser.add_argument(
        "--series", required=True, action="append",
        help="SYMBOL=PATH for a 1s index series; repeat per symbol",
    )
    parser.add_argument(
        "--delta", type=float, action="append", default=None,
        help="Relaxed-variant delta, e.g. 0.01; repeat for several. Defaults to 0.5%%/1%%/2%%.",
    )
    parser.add_argument("--window-size", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    series_by_symbol = {}
    for entry in args.series:
        symbol, _, path = entry.partition("=")
        if not path:
            raise SystemExit(f"--series expects SYMBOL=PATH, got {entry!r}")
        series_by_symbol[symbol] = load_price_series(path)

    variants = DEFAULT_VARIANTS
    if args.delta:
        variants = (Variant("strict"), *(Variant(f"relaxed-{d:g}", d) for d in args.delta))

    markets = load_settled_markets(args.markets)
    results = reconstruct(markets, series_by_symbol, variants, args.window_size)
    if not results:
        raise SystemExit("No markets scored — check that --series symbols match the markets file.")
    print(format_summaries(summarize(results)))


if __name__ == "__main__":
    main()
