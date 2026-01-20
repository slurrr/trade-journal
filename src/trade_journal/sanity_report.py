from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.reconstruct.funding import apply_funding_events
from trade_journal.reconstruct.trades import reconstruct_trades


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a quick sanity report from a fills export.")
    parser.add_argument(
        "fills_path",
        type=Path,
        nargs="?",
        default=None,
        help="Path to ApeX fills export (json/csv/tsv).",
    )
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument(
        "--utc",
        action="store_true",
        help="Print timestamps in UTC instead of local timezone.",
    )
    parser.add_argument(
        "--funding",
        type=Path,
        default=None,
        help="Optional funding export (json/csv/tsv) to apply to trades.",
    )
    args = parser.parse_args(argv)

    context = resolve_account_context(args.account)
    fills_path = args.fills_path or resolve_data_path(None, context, "fills.json")
    if not fills_path.exists():
        candidate = resolve_data_path(None, context, "fills.csv")
        fills_path = candidate if candidate.exists() else fills_path

    result = load_fills(fills_path, source=context.source, account_id=context.account_id)
    fills = result.fills
    trades = reconstruct_trades(fills)

    if result.skipped:
        print(f"Skipped {result.skipped} fill rows during normalization.", file=sys.stderr)

    funding_path = args.funding
    if funding_path is None:
        default_funding = resolve_data_path(None, context, "funding.json")
        if default_funding.exists():
            funding_path = default_funding

    if funding_path is not None:
        funding_result = load_funding(funding_path, source=context.source, account_id=context.account_id)
        attributions = apply_funding_events(trades, funding_result.events)
        unmatched = sum(1 for item in attributions if item.matched_trade_id is None)
        if funding_result.skipped:
            print(
                f"Skipped {funding_result.skipped} funding rows during normalization.",
                file=sys.stderr,
            )
        print(f"funding_events {len(funding_result.events)}")
        print(f"funding_unmatched {unmatched}")

    if not fills:
        print("No fills loaded.")
        return 0

    tz = (lambda dt: dt) if args.utc else (lambda dt: dt.astimezone())

    fill_times = [fill.timestamp for fill in fills]
    fill_start = tz(min(fill_times))
    fill_end = tz(max(fill_times))
    fill_fees = sum(fill.fee for fill in fills)
    fill_notional = sum(fill.price * fill.size for fill in fills)
    symbols = sorted({fill.symbol for fill in fills})

    print(f"fills_count {len(fills)}")
    print(f"fills_time_range {fill_start.isoformat()} -> {fill_end.isoformat()}")
    print(f"fills_symbols {', '.join(symbols)}")
    print(f"fills_total_fees {fill_fees:.6g}")
    print(f"fills_total_notional {fill_notional:.6g}")

    if not trades:
        print("trades_count 0")
        return 0

    trade_entry_times = [trade.entry_time for trade in trades]
    trade_exit_times = [trade.exit_time for trade in trades]
    trade_start = tz(min(trade_entry_times))
    trade_end = tz(max(trade_exit_times))
    trade_pnl_gross = sum(trade.realized_pnl for trade in trades)
    trade_fees = sum(trade.fees for trade in trades)
    trade_funding = sum(trade.funding_fees for trade in trades)
    trade_pnl_net = sum(trade.realized_pnl_net for trade in trades)

    print(f"trades_count {len(trades)}")
    print(f"trades_time_range {trade_start.isoformat()} -> {trade_end.isoformat()}")
    print(f"trades_pnl_gross {trade_pnl_gross:.6g}")
    print(f"trades_fees {trade_fees:.6g}")
    print(f"trades_funding {trade_funding:.6g}")
    print(f"trades_pnl_net {trade_pnl_net:.6g}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
