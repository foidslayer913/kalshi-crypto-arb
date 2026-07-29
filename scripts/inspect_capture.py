"""Dump the real shape of captured WebSocket messages.

order_book.py was written against Kalshi's *documented* message schema, which has never matched a
live feed until now. This prints one example of each WebSocket message type seen in a capture, so
the parser can be corrected against the actual wire format rather than the docs.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from backtest.recorder import read_captures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="captures")
    parser.add_argument("--per-type", type=int, default=1, help="How many examples of each type to print.")
    args = parser.parse_args()

    type_counts: Counter[str] = Counter()
    examples: dict[str, list[dict]] = {}
    for event in read_captures(args.dir, kinds={"ws"}):
        payload = event.data.get("payload", {})
        msg_type = payload.get("type", "<no type field>")
        type_counts[msg_type] += 1
        shown = examples.setdefault(msg_type, [])
        if len(shown) < args.per_type:
            shown.append(payload)

    if not type_counts:
        print(f"No WebSocket messages found in {args.dir}. Did the capture record any?")
        return

    print("WebSocket message types seen (count):")
    for msg_type, count in type_counts.most_common():
        print(f"  {msg_type}: {count}")
    print()
    for msg_type, shown in examples.items():
        for payload in shown:
            print(f"--- example: {msg_type} ---")
            print(json.dumps(payload, indent=2))
            print()


if __name__ == "__main__":
    main()
