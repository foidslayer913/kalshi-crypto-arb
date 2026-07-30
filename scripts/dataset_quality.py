"""Coverage and completeness report for a built dataset.

For a data product this is the spec sheet. A consumer's first question is not "what columns do you
have" but "how much is missing, and where" — a series with silent holes is worse than no series,
because a backtest run over it returns a confident wrong number.

Reports, per series and overall: how many market-seconds exist against how many *should*, where the
gaps are, and how populated each column is.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default="data/timeseries.csv")
    parser.add_argument("--gap-seconds", type=int, default=5,
                        help="A jump larger than this between consecutive seconds counts as a gap.")
    args = parser.parse_args()

    path = Path(args.csv)
    if not path.exists():
        raise SystemExit(f"{path} not found — run scripts/build_timeseries first.")

    rows_by_series: dict[str, int] = defaultdict(int)
    markets_by_series: dict[str, set] = defaultdict(set)
    seconds_by_market: dict[str, list] = defaultdict(list)
    populated: dict[str, int] = defaultdict(int)
    total = 0
    columns: list[str] = []
    first_t = last_t = None

    with path.open() as handle:
        for row in csv.DictReader(handle):
            total += 1
            columns = columns or list(row)
            ticker = row["ticker"]
            series = _series_of(ticker)
            rows_by_series[series] += 1
            markets_by_series[series].add(ticker)
            second = int(row["t"])
            seconds_by_market[ticker].append(second)
            first_t = second if first_t is None else min(first_t, second)
            last_t = second if last_t is None else max(last_t, second)
            for column, value in row.items():
                if value not in ("", "None"):
                    populated[column] += 1

    if total == 0:
        raise SystemExit("No rows in the dataset.")

    print(f"rows                {total}")
    print(f"span                {datetime.fromtimestamp(first_t, tz=timezone.utc)} .. "
          f"{datetime.fromtimestamp(last_t, tz=timezone.utc)} UTC")
    print()
    print(f"{'series':<14}{'rows':>10}{'markets':>10}{'rows/market':>13}")
    print("-" * 47)
    for series in sorted(rows_by_series):
        count = len(markets_by_series[series])
        print(f"{series:<14}{rows_by_series[series]:>10}{count:>10}"
              f"{rows_by_series[series] / count:>13.0f}")

    # Continuity: within a market, seconds should be consecutive. Holes mean the capture was down.
    gaps: list[tuple[int, str]] = []
    covered = 0
    expected = 0
    for ticker, seconds in seconds_by_market.items():
        ordered = sorted(set(seconds))
        covered += len(ordered)
        expected += ordered[-1] - ordered[0] + 1
        for earlier, later in zip(ordered, ordered[1:]):
            if later - earlier > args.gap_seconds:
                gaps.append((later - earlier, ticker))

    print()
    print(f"continuity          {covered}/{expected} market-seconds present "
          f"({covered / expected * 100:.2f}%)")
    print(f"gaps > {args.gap_seconds}s          {len(gaps)}")
    for size, ticker in sorted(gaps, reverse=True)[:5]:
        print(f"  {size:>6}s  {ticker}")

    print()
    print("column completeness (a mostly-empty column is a defect, not a feature):")
    for column in columns:
        share = populated[column] / total * 100
        flag = "  <-- mostly empty" if share < 50 else ""
        print(f"  {column:<22}{share:>6.1f}%{flag}")


if __name__ == "__main__":
    main()
