from __future__ import annotations

import argparse
from pathlib import Path

from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.ingest.apex_equity import load_equity_history
from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_liquidations import load_liquidations
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.ingest.apex_orders import load_orders
from trade_journal.reconcile import load_historical_pnl
from trade_journal.storage.sqlite_store import (
    connect,
    init_db,
    upsert_account_equity,
    upsert_accounts,
    upsert_fills,
    upsert_funding,
    upsert_historical_pnl,
    upsert_liquidations,
    upsert_orders,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist ApeX data files into SQLite.")
    parser.add_argument("--db", type=Path, default=Path("data/trade_journal.sqlite"), help="SQLite DB path.")
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument("--fills", type=Path, default=None, help="Fills JSON path.")
    parser.add_argument("--orders", type=Path, default=None, help="Orders JSON path.")
    parser.add_argument("--funding", type=Path, default=None, help="Funding JSON path.")
    parser.add_argument(
        "--liquidations",
        type=Path,
        default=None,
        help="Liquidations JSON path.",
    )
    parser.add_argument(
        "--historical-pnl",
        type=Path,
        default=None,
        help="Historical PnL JSON path.",
    )
    parser.add_argument(
        "--equity-history",
        type=Path,
        default=None,
        help="Account balance history JSON path.",
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=None,
        help="Write sync validation counts to this file.",
    )
    parser.add_argument(
        "--log-out",
        type=Path,
        default=None,
        help="Write last successful sync metadata to this file.",
    )
    args = parser.parse_args(argv)

    context = resolve_account_context(args.account)
    fills_path = args.fills
    if fills_path is None:
        fills_path = resolve_data_path(None, context, "fills.json")
        if not fills_path.exists():
            candidate = resolve_data_path(None, context, "fills.csv")
            fills_path = candidate if candidate.exists() else fills_path
    orders_path = args.orders or resolve_data_path(None, context, "history_orders.json")
    funding_path = args.funding or resolve_data_path(None, context, "funding.json")
    liquidations_path = args.liquidations or resolve_data_path(None, context, "liquidations.json")
    historical_pnl_path = args.historical_pnl or resolve_data_path(None, context, "historical_pnl.json")
    equity_history_path = args.equity_history or resolve_data_path(
        None, context, "equity_history.json"
    )

    conn = connect(args.db)
    init_db(conn)

    total = 0
    summaries: list[str] = []
    report: dict[str, dict[str, int]] = {}
    account_rows = upsert_accounts(
        conn,
        [
            {
                "account_id": context.account_id or context.name,
                "name": context.name,
                "exchange": context.exchange or context.source,
                "base_currency": context.base_currency,
                "starting_equity": context.starting_equity,
                "active": context.active,
                "raw_json": {
                    "source": context.source,
                    "data_dir": str(context.data_dir),
                },
            }
        ],
    )
    total += account_rows
    summaries.append(_summary_line("accounts", account_rows, 0, 0))
    report["accounts"] = _report_entry(account_rows, 0, 0)
    if fills_path.exists():
        result = load_fills(fills_path, source=context.source, account_id=context.account_id)
        valid_fills, skipped_invalid = _validate_fills(result.fills)
        total += upsert_fills(conn, valid_fills)
        summaries.append(_summary_line("fills", len(valid_fills), result.skipped, skipped_invalid))
        report["fills"] = _report_entry(len(valid_fills), result.skipped, skipped_invalid)
    if orders_path.exists():
        result = load_orders(orders_path, source=context.source, account_id=context.account_id)
        valid_orders, skipped_invalid = _validate_orders(result.orders)
        total += upsert_orders(conn, valid_orders)
        summaries.append(_summary_line("orders", len(valid_orders), result.skipped, skipped_invalid))
        report["orders"] = _report_entry(len(valid_orders), result.skipped, skipped_invalid)
    if funding_path.exists():
        result = load_funding(funding_path, source=context.source, account_id=context.account_id)
        valid_funding, skipped_invalid = _validate_funding(result.events)
        total += upsert_funding(conn, valid_funding)
        summaries.append(_summary_line("funding", len(valid_funding), result.skipped, skipped_invalid))
        report["funding"] = _report_entry(len(valid_funding), result.skipped, skipped_invalid)
    if liquidations_path.exists():
        result = load_liquidations(liquidations_path, source=context.source, account_id=context.account_id)
        valid_liquidations, skipped_invalid = _validate_liquidations(result.events)
        total += upsert_liquidations(conn, valid_liquidations)
        summaries.append(_summary_line("liquidations", len(valid_liquidations), result.skipped, skipped_invalid))
        report["liquidations"] = _report_entry(len(valid_liquidations), result.skipped, skipped_invalid)
    if historical_pnl_path.exists():
        records = load_historical_pnl(historical_pnl_path, source=context.source, account_id=context.account_id)
        valid_pnl, skipped_invalid = _validate_pnl(records)
        total += upsert_historical_pnl(conn, valid_pnl)
        summaries.append(_summary_line("historical_pnl", len(valid_pnl), 0, skipped_invalid))
        report["historical_pnl"] = _report_entry(len(valid_pnl), 0, skipped_invalid)
    if equity_history_path.exists():
        result = load_equity_history(
            equity_history_path,
            source=context.source,
            account_id=context.account_id,
            min_value=0.0,
        )
        total += upsert_account_equity(conn, result.snapshots)
        summaries.append(_summary_line("equity_history", len(result.snapshots), result.skipped, 0))
        report["equity_history"] = _report_entry(len(result.snapshots), result.skipped, 0)

    print(f"upserted_rows {total}")
    for line in summaries:
        print(line)
    report_out = args.report_out or resolve_data_path(None, context, "sync_report.json")
    log_out = args.log_out or resolve_data_path(None, context, "last_sync.json")
    if report_out is not None:
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(_report_json(report), encoding="utf-8")
    if log_out is not None:
        log_out.parent.mkdir(parents=True, exist_ok=True)
        log_out.write_text(_log_json(args.db, total, report), encoding="utf-8")
    return 0


def _validate_fills(items):
    valid = []
    skipped = 0
    for fill in items:
        if fill.size <= 0 or fill.price <= 0:
            skipped += 1
            continue
        valid.append(fill)
    return valid, skipped


def _validate_orders(items):
    valid = []
    skipped = 0
    for order in items:
        if order.size <= 0:
            skipped += 1
            continue
        if order.price is not None and order.price <= 0:
            skipped += 1
            continue
        valid.append(order)
    return valid, skipped


def _validate_funding(items):
    valid = []
    skipped = 0
    for event in items:
        if event.symbol.strip().lower() in {"", "none", "null"}:
            skipped += 1
            continue
        valid.append(event)
    return valid, skipped


def _validate_liquidations(items):
    valid = []
    skipped = 0
    for event in items:
        if event.size <= 0:
            skipped += 1
            continue
        valid.append(event)
    return valid, skipped


def _validate_pnl(items):
    valid = []
    skipped = 0
    for record in items:
        if record.size <= 0:
            skipped += 1
            continue
        if record.symbol.strip().lower() in {"", "none", "null"}:
            skipped += 1
            continue
        valid.append(record)
    return valid, skipped


def _summary_line(name: str, accepted: int, skipped_parse: int, skipped_invalid: int) -> str:
    return f"{name} accepted={accepted} skipped_parse={skipped_parse} skipped_invalid={skipped_invalid}"


def _report_entry(accepted: int, skipped_parse: int, skipped_invalid: int) -> dict[str, int]:
    return {
        "accepted": accepted,
        "skipped_parse": skipped_parse,
        "skipped_invalid": skipped_invalid,
    }


def _report_json(report: dict[str, dict[str, int]]) -> str:
    import json

    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _log_json(db_path: Path, total: int, report: dict[str, dict[str, int]]) -> str:
    import json
    from datetime import datetime, timezone

    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "upserted_rows": total,
        "report": report,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
