"""Reconcile a capture's order book volume against the markets a fill analysis can actually score.

A fill analysis can only score a market it has metadata for (`record_market`) *and* whose settlement
window it actually recorded. When those sets diverge — e.g. a capture spanning two runs, where the
market records come from one run and the order book volume from another — the analysis silently
scores the quiet markets and ignores the busy ones, producing a confident-looking zero.

This prints where the order book volume actually is, which tickers have metadata, and whether their
windows fall inside the captured span, so that divergence is visible instead of inferred.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone

from backtest.recorder import capture_files, captured_markets, read_captures


def _iso(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="captures")
    parser.add_argument("--top", type=int, default=15, help="How many busiest tickers to show.")
    args = parser.parse_args()

    deltas: Counter[str] = Counter()
    snapshots: Counter[str] = Counter()
    first_ws: dict[str, float] = {}
    last_ws: dict[str, float] = {}
    first_t: float | None = None
    last_t: float | None = None
    tick_count = 0
    tick_ts: dict[str, list[float]] = {}

    for event in read_captures(args.dir, kinds={"ws", "tick"}):
        first_t = event.t if first_t is None else min(first_t, event.t)
        last_t = event.t if last_t is None else max(last_t, event.t)
        if event.kind == "tick":
            tick_count += 1
            tick_ts.setdefault(event.data["symbol"], []).append(event.data["ts"])
            continue
        payload = event.data["payload"]
        ticker = payload.get("msg", {}).get("market_ticker")
        if ticker is None:
            continue
        if payload.get("type") == "orderbook_delta":
            deltas[ticker] += 1
        elif payload.get("type") == "orderbook_snapshot":
            snapshots[ticker] += 1
        first_ws.setdefault(ticker, event.t)
        last_ws[ticker] = event.t

    markets = {market.ticker: market for market in captured_markets(args.dir)}

    print(f"files              {[p.name for p in capture_files(args.dir)]}")
    print(f"captured span      {_iso(first_t)} .. {_iso(last_t)} UTC")
    print(f"index ticks        {tick_count}")
    print(f"deltas             {sum(deltas.values())} across {len(deltas)} tickers")
    print(f"snapshots          {sum(snapshots.values())} across {len(snapshots)} tickers")
    print(f"market metadata    {len(markets)} tickers")

    with_meta = sum(count for ticker, count in deltas.items() if ticker in markets)
    total = sum(deltas.values())
    print(
        f"\ndeltas on tickers WITH metadata: {with_meta}/{total}"
        f" ({(with_meta / total * 100) if total else 0:.1f}%)"
    )
    print("If that share is low, the fill analysis is scoring the quiet markets and ignoring the busy ones.")

    # Index tick coverage. The bounds cannot be evaluated for a window with no ticks, so a hole in
    # this feed silently removes markets from the fill analysis — which is indistinguishable from
    # those markets having no liquidity unless the gaps are shown.
    print("\nindex tick coverage (the settlement bounds need ~1 Hz here):")
    for symbol, stamps in sorted(tick_ts.items()):
        stamps.sort()
        gaps = [(stamps[i + 1] - stamps[i], stamps[i]) for i in range(len(stamps) - 1)]
        big = sorted((gap for gap in gaps if gap[0] > 5.0), reverse=True)[:5]
        span = stamps[-1] - stamps[0] if len(stamps) > 1 else 0.0
        expected = int(span) + 1
        print(
            f"  {symbol}: {len(stamps)} ticks over {span / 60:.1f} min "
            f"({len(stamps) / expected * 100 if expected else 0:.0f}% of 1 Hz), "
            f"{_iso(stamps[0])} .. {_iso(stamps[-1])}"
        )
        if big:
            print(f"    largest gaps > 5s:")
            for gap, at in big:
                print(f"      {gap / 60:>6.1f} min starting {_iso(at)} UTC")

    # Per-market-event window coverage: does each hourly close have ticks in its final 60 seconds?
    print("\nsettlement-window tick coverage by close time:")
    for close_iso in sorted({_iso(m.close_time.timestamp()) for m in captured_markets(args.dir)}):
        close_dt = datetime.strptime(close_iso, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        close_ts = close_dt.timestamp()
        for symbol, stamps in sorted(tick_ts.items()):
            in_window = sum(1 for ts in stamps if close_ts - 60 <= ts <= close_ts)
            flag = "OK" if in_window >= 30 else "GAP — markets here cannot be scored"
            print(f"  {close_iso} {symbol}: {in_window}/60 ticks   {flag}")

    # Group metadata by close time so a multi-run capture's separate market sets are obvious.
    by_close: Counter[str] = Counter()
    for market in markets.values():
        by_close[_iso(market.close_time.timestamp())] += 1
    print("\nmarket metadata by close time:")
    for close, count in sorted(by_close.items()):
        print(f"  {close} UTC   {count} markets")

    print(f"\nbusiest {args.top} tickers by delta count:")
    header = f"{'ticker':<30}{'deltas':>8}{'snaps':>7}{'meta?':>7}{'close_time':>21}{'window captured?':>18}"
    print(header)
    print("-" * len(header))
    for ticker, count in deltas.most_common(args.top):
        market = markets.get(ticker)
        close = _iso(market.close_time.timestamp()) if market else "-"
        if market is None:
            captured = "no metadata"
        else:
            close_ts = market.close_time.timestamp()
            window_start, window_end = close_ts - 60, close_ts - 1
            inside = (
                first_t is not None and last_t is not None
                and first_t <= window_start and window_end <= last_t
            )
            has_ws_in_window = (
                ticker in first_ws
                and first_ws[ticker] <= window_end
                and last_ws[ticker] >= window_start
            )
            captured = "yes" if inside and has_ws_in_window else ("span only" if inside else "no")
        print(
            f"{ticker:<30}{count:>8}{snapshots.get(ticker, 0):>7}"
            f"{('yes' if market else 'NO'):>7}{close:>21}{captured:>18}"
        )


if __name__ == "__main__":
    main()
