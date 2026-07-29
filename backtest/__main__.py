"""Backtest CLI.

    # Tier 2: score settlement signals against how markets actually settled
    python -m backtest signal --markets settled.jsonl --series BTC-USD=btc_1s.csv

    # Tier 1: inspect what a live capture has collected so far
    python -m backtest capture --dir captures
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
from backtest.recorder import capture_stats, format_stats


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="backtest", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    signal = subparsers.add_parser("signal", help="Tier 2 signal-correctness backtest")
    signal.add_argument("--markets", required=True, help="JSONL of settled markets (ground truth)")
    signal.add_argument(
        "--series", required=True, action="append",
        help="SYMBOL=PATH for a 1s index series; repeat per symbol",
    )
    signal.add_argument(
        "--delta", type=float, action="append", default=None,
        help="Relaxed-variant delta, e.g. 0.01; repeat for several. Defaults to 0.5%%/1%%/2%%.",
    )
    signal.add_argument("--window-size", type=int, default=60)

    capture = subparsers.add_parser("capture", help="Summarise a Tier 1 market data capture")
    capture.add_argument("--dir", default="captures", help="Capture directory (default: captures)")

    return parser.parse_args()


def _run_signal(args: argparse.Namespace) -> None:
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


def _run_capture(args: argparse.Namespace) -> None:
    stats = capture_stats(args.dir)
    if not stats["files"]:
        raise SystemExit(f"No capture files found in {args.dir!r}.")
    print(format_stats(stats))


def main() -> None:
    args = _parse_args()
    if args.command == "signal":
        _run_signal(args)
    else:
        _run_capture(args)


if __name__ == "__main__":
    main()
