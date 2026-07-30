"""Turn a raw capture into a per-second CSV — the consumable form of the dataset.

    python -m scripts.build_timeseries --dir captures -o data/timeseries.csv

The capture on disk is unparsed WebSocket traffic; this is the artifact someone else could actually
use. Backfilling the index column from Binance afterwards is usually better than relying on the
live crypto feed, which is rate-limited and drops most of its ticks.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from backtest.conditional import MinuteIndex
from dataset.timeseries import build_rows, write_csv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="captures", help="Capture directory")
    parser.add_argument("-o", "--out", default="data/timeseries.csv")
    parser.add_argument(
        "--within-minutes", type=float, default=20.0,
        help="Only emit rows for markets this close to settling (default 20). Every open strike "
             "every second would be tens of millions of rows of mostly-static book.",
    )
    parser.add_argument(
        "--index", action="append", default=None, metavar="SYMBOL=PATH",
        help="Backfill the index column from a 1m series (scripts/fetch_index_minutes). Repeatable. "
             "The live crypto feed is rate-limited and drops most ticks, so without this the index "
             "column is mostly empty.",
    )
    args = parser.parse_args()

    backfill = {}
    for entry in args.index or []:
        symbol, _, path = entry.partition("=")
        if not path:
            raise SystemExit(f"--index expects SYMBOL=PATH, got {entry!r}")
        backfill[symbol] = MinuteIndex.from_csv(path)
        print(f"Backfilling {symbol} from {path} ({len(backfill[symbol])} minutes)")

    rows = build_rows(args.dir, within_minutes=args.within_minutes, index_backfill=backfill or None)
    written = write_csv(rows, args.out)
    if written == 0:
        raise SystemExit(
            f"No rows produced from {args.dir!r}. Either the capture holds no market metadata, or "
            "no captured market was within --within-minutes of its close while recording."
        )
    size_mb = Path(args.out).stat().st_size / 1e6
    print(f"Wrote {written} rows ({size_mb:.1f} MB) to {args.out}")
    print("Columns: t, ticker, seconds_to_close, yes_bid, yes_ask, no_bid, no_ask, sizes, mid,")
    print("         spread, strike, index_price, index_distance_pct, stale_seconds")


if __name__ == "__main__":
    main()
