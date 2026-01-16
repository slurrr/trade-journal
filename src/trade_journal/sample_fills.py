from __future__ import annotations

import argparse
from pathlib import Path

from trade_journal.ingest.apex_omni import load_fills


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a sample of normalized fills.")
    parser.add_argument(
        "fills_path",
        type=Path,
        nargs="?",
        default=Path("data/fills.json"),
        help="Path to fills JSON file.",
    )
    parser.add_argument("--symbol", type=str, default=None, help="Filter by symbol.")
    parser.add_argument("--limit", type=int, default=20, help="Max fills to print.")
    parser.add_argument(
        "--only-matched",
        action="store_true",
        help="Only include rows that look like actual fills (matchFillId + price/size).",
    )
    parser.add_argument(
        "--utc",
        action="store_true",
        help="Print timestamps in UTC instead of local timezone.",
    )
    args = parser.parse_args(argv)

    result = load_fills(args.fills_path)
    fills = result.fills
    if args.symbol:
        fills = [fill for fill in fills if fill.symbol == args.symbol]
    if args.only_matched:
        fills = [
            fill
            for fill in fills
            if fill.raw.get("matchFillId") and fill.price and fill.size
        ]

    fills = sorted(fills, key=lambda item: item.timestamp)

    print("symbol side size price time")
    for fill in fills[: args.limit]:
        timestamp = fill.timestamp
        if not args.utc:
            timestamp = timestamp.astimezone()
        print(f"{fill.symbol} {fill.side} {fill.size:.6g} {fill.price:.6g} {timestamp.isoformat()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
