"""Backtest CLI.

    # Tier 2: score settlement signals against how markets actually settled
    python -m backtest signal --markets settled.jsonl --series BTC-USD=btc_1s.csv

    # Tier 1: inspect what a live capture has collected so far
    python -m backtest capture --dir captures
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter

from backtest.reconstruct import (
    DEFAULT_VARIANTS,
    Variant,
    format_summaries,
    load_price_series,
    load_settled_markets,
    reconstruct,
    summarize,
)
from backtest.calibration import (
    bucket_observations,
    format_by_horizon,
    format_calibration,
    load_observations,
)
from backtest.recorder import capture_stats, format_stats
from backtest.window_fills import (
    analyze_fills,
    format_fill_debug,
    format_fill_summary,
    summarize_fills,
)


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

    fills = subparsers.add_parser(
        "fills", help="When the signal fires in a capture, was there a fillable ask?"
    )
    fills.add_argument("--dir", default="captures", help="Capture directory (default: captures)")
    fills.add_argument(
        "--delta", type=float, default=0.01,
        help="Relaxed-variant delta to score (default 0.01 = 1%%). Use 0 for the strict bound.",
    )
    fills.add_argument("--min-yield", type=float, default=0.01, help="Net-yield threshold for 'fillable'.")
    fills.add_argument("--window-size", type=int, default=60)
    fills.add_argument(
        "--debug", action="store_true",
        help="Per-market breakdown (both best bids at fire) to tell a one-sided book from an empty one.",
    )

    calibration = subparsers.add_parser(
        "calibration", help="Is the market's price a fair probability, and where is it wrong?"
    )
    calibration.add_argument("--observations", required=True, help="JSONL from scripts/fetch_calibration")
    calibration.add_argument("--width", type=float, default=0.02, help="Price bucket width (default 2c).")
    calibration.add_argument("--min-price", type=float, default=0.50)
    calibration.add_argument("--max-price", type=float, default=0.995)
    calibration.add_argument(
        "--min-minutes", type=float, default=1.0,
        help="Ignore candles closer than this to expiry (the final candle overlaps settlement).",
    )
    calibration.add_argument("--max-minutes", type=float, default=None)
    calibration.add_argument(
        "--min-markets", type=int, default=30, help="Buckets below this are reported as 'thin'.",
    )
    calibration.add_argument("--start", default=None, help="Only markets closing on/after YYYY-MM-DD.")
    calibration.add_argument("--end", default=None, help="Only markets closing on/before YYYY-MM-DD.")

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


def _run_fills(args: argparse.Namespace) -> None:
    delta = args.delta if args.delta > 0 else None
    name = "strict" if delta is None else f"relaxed-{delta:g}"
    variant = Variant(name, delta)
    reasons: Counter[str] = Counter()
    results = analyze_fills(args.dir, variant, window_size=args.window_size, reasons=reasons)

    def print_disposition() -> None:
        total = sum(reasons.values())
        print(f"captured markets   {total}")
        for reason, count in reasons.most_common():
            print(f"  {reason:<44}{count}")

    if not results:
        print_disposition()
        raise SystemExit(
            f"\nNo markets scored under {name} in {args.dir!r} — see the dispositions above for why."
        )
    print_disposition()
    print()
    print(format_fill_summary(summarize_fills(results, name, args.min_yield), args.min_yield))
    if args.debug:
        print()
        print(format_fill_debug(results))


def _run_calibration(args: argparse.Namespace) -> None:
    observations = load_observations(
        args.observations,
        min_minutes=args.min_minutes,
        max_minutes=args.max_minutes,
        min_price=args.min_price,
        max_price=args.max_price,
        start=args.start,
        end=args.end,
    )
    if not observations:
        raise SystemExit(
            f"No observations in range in {args.observations!r}. Widen --min-price/--max-price or "
            "lower --min-minutes."
        )
    markets = len({observation.ticker for observation in observations})
    print(
        f"{len(observations)} tradeable observations across {markets} markets "
        f"(prices {args.min_price}-{args.max_price}, >= {args.min_minutes} min to close)\n"
    )
    buckets = bucket_observations(observations, width=args.width)
    print(format_calibration(buckets, min_markets=args.min_markets))
    print()
    print("Edge by time remaining:")
    print(format_by_horizon(observations))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    if args.command == "signal":
        _run_signal(args)
    elif args.command == "fills":
        _run_fills(args)
    elif args.command == "calibration":
        _run_calibration(args)
    else:
        _run_capture(args)


if __name__ == "__main__":
    main()
