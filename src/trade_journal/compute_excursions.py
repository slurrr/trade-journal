from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.ingest.apex_api import load_dotenv
from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.metrics.excursions import apply_trade_excursions
from trade_journal.pricing.apex_prices import ApexPriceClient, PriceSeriesConfig
from trade_journal.reconstruct.funding import apply_funding_events
from trade_journal.reconstruct.trades import reconstruct_trades


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute MAE/MFE/ETD and cache to JSON.")
    parser.add_argument(
        "fills_path",
        type=Path,
        nargs="?",
        default=None,
        help="Path to ApeX fills export (json/csv/tsv).",
    )
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument(
        "--funding",
        type=Path,
        default=None,
        help="Optional funding export (json/csv/tsv) to apply to trades.",
    )
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file.")
    parser.add_argument("--out", type=Path, default=None, help="Output file.")
    parser.add_argument("--local", action="store_true", help="Use local timestamps for keys (default uses UTC).")
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

    env = dict(os.environ)
    env.update(load_dotenv(args.env))
    price_client = ApexPriceClient(PriceSeriesConfig.from_env(env))

    payload: dict[str, dict[str, float | None]] = {}
    for trade in trades:
        key = _trade_key(trade, local=args.local)
        try:
            bars = price_client.fetch_bars(trade.symbol, trade.entry_time, trade.exit_time)
            apply_trade_excursions(trade, bars)
            payload[key] = {
                "mae": trade.mae,
                "mfe": trade.mfe,
                "etd": trade.etd,
            }
        except RuntimeError as exc:
            print(
                f"Price series missing for {trade.symbol} "
                f"{trade.entry_time.isoformat()} -> {trade.exit_time.isoformat()}: {exc}",
                file=sys.stderr,
            )
            payload[key] = {
                "mae": None,
                "mfe": None,
                "etd": None,
            }

    out_path = args.out or resolve_data_path(None, context, "excursions.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def _trade_key(trade, local: bool) -> str:
    entry = trade.entry_time.astimezone() if local else trade.entry_time
    exit_ = trade.exit_time.astimezone() if local else trade.exit_time
    parts = [
        trade.source,
        trade.account_id or "",
        trade.symbol,
        trade.side,
        entry.isoformat(),
        exit_.isoformat(),
        f"{trade.entry_price:.8f}",
        f"{trade.exit_price:.8f}",
        f"{trade.entry_size:.8f}",
        f"{trade.exit_size:.8f}",
        f"{trade.realized_pnl:.8f}",
    ]
    return "|".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
