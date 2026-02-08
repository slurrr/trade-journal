from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.config.app_config import load_app_config
from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.metrics.excursions import apply_trade_excursions
from trade_journal.pricing.apex_prices import ApexPriceClient, PriceSeriesConfig
from trade_journal.reconstruct.funding import apply_funding_events
from trade_journal.reconstruct.trades import reconstruct_trades


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconstruct trades from ApeX Omni fills export.")
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
    parser.add_argument(
        "--prices",
        action="store_true",
        help="Fetch price series and compute MAE/MFE/ETD for each trade.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write summary to a file instead of stdout.")
    args = parser.parse_args(argv)

    context = resolve_account_context(args.account, env=os.environ)
    fills_path = args.fills_path or resolve_data_path(None, context, "fills.json")
    if not fills_path.exists():
        candidate = resolve_data_path(None, context, "fills.csv")
        fills_path = candidate if candidate.exists() else fills_path

    result = load_fills(fills_path, source=context.source, account_id=context.account_id)
    trades = reconstruct_trades(result.fills)

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
        if unmatched:
            print(f"Unmatched funding events: {unmatched}.", file=sys.stderr)

    if args.prices:
        app_config = load_app_config()
        price_client = ApexPriceClient(PriceSeriesConfig.from_settings(app_config.pricing))
        for trade in trades:
            try:
                bars = price_client.fetch_bars(trade.symbol, trade.entry_time, trade.exit_time)
                apply_trade_excursions(trade, bars)
            except RuntimeError as exc:
                print(
                    f"Price series missing for {trade.symbol} "
                    f"{trade.entry_time.isoformat()} -> {trade.exit_time.isoformat()}: {exc}",
                    file=sys.stderr,
                )

    if not trades:
        print("No trades reconstructed.")
        return 0

    out_path = args.out
    if out_path is None:
        default_out = Path("data/trades_summary.txt")
        if default_out.exists():
            out_path = default_out

    output = []
    output.append("symbol side entry_size exit_size entry_px exit_px close_time funding pnl_net mae mfe etd")
    for trade in trades:
        exit_time = trade.exit_time if args.utc else trade.exit_time.astimezone()
        entry_px = f"{trade.entry_price:.6g}"
        exit_px = f"{trade.exit_price:.6g}"
        close_time = exit_time.isoformat()
        pnl_net = f"{trade.realized_pnl_net:.6g}"
        mae = _format_metric(trade.mae)
        mfe = _format_metric(trade.mfe)
        etd = _format_metric(trade.etd)
        entry_size = f"{trade.entry_size:.6g}"
        exit_size = f"{trade.exit_size:.6g}"
        funding = f"{trade.funding_fees:.6g}"
        output.append(
            f"{trade.symbol} {trade.side} {entry_size} {exit_size} "
            f"{entry_px} {exit_px} {close_time} {funding} {pnl_net} {mae} {mfe} {etd}"
        )

    if out_path is None:
        for line in output:
            print(line)
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(output) + "\n", encoding="utf-8")

    return 0


def _format_metric(value: float | None) -> str:
    return "na" if value is None else f"{value:.6g}"
