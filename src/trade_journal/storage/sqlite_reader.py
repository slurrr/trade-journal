from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from trade_journal.ingest.apex_liquidations import LiquidationEvent
from trade_journal.ingest.apex_orders import OrderRecord
from trade_journal.models import EquitySnapshot, Fill, FundingEvent
from trade_journal.reconcile import PnlRecord


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_fills(
    conn: sqlite3.Connection, *, source: str, account_id: str | None
) -> list[Fill]:
    rows = _fetch(conn, "fills", source, account_id)
    fills: list[Fill] = []
    for row in rows:
        fills.append(
            Fill(
                fill_id=row["fill_id"],
                order_id=row["order_id"],
                symbol=row["symbol"],
                side=row["side"],
                price=row["price"],
                size=row["size"],
                fee=row["fee"],
                fee_asset=row["fee_asset"],
                timestamp=_parse_iso(row["timestamp"]),
                source=row["source"],
                account_id=row["account_id"],
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return fills


def load_funding(
    conn: sqlite3.Connection, *, source: str, account_id: str | None
) -> list[FundingEvent]:
    rows = _fetch(conn, "funding", source, account_id)
    events: list[FundingEvent] = []
    for row in rows:
        events.append(
            FundingEvent(
                funding_id=row["funding_id"],
                transaction_id=row["transaction_id"],
                symbol=row["symbol"],
                side=row["side"],
                rate=row["rate"],
                position_size=row["position_size"],
                price=row["price"],
                funding_time=_parse_iso(row["funding_time"]),
                funding_value=row["funding_value"],
                status=row["status"],
                source=row["source"],
                account_id=row["account_id"],
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return events


def load_orders(
    conn: sqlite3.Connection, *, source: str, account_id: str | None
) -> list[OrderRecord]:
    rows = _fetch(conn, "orders", source, account_id)
    orders: list[OrderRecord] = []
    for row in rows:
        orders.append(
            OrderRecord(
                order_id=row["order_id"],
                client_order_id=row["client_order_id"],
                source=row["source"],
                account_id=row["account_id"],
                symbol=row["symbol"],
                side=row["side"],
                size=row["size"],
                price=row["price"],
                reduce_only=bool(row["reduce_only"]),
                is_position_tpsl=bool(row["is_position_tpsl"]),
                is_open_tpsl=bool(row["is_open_tpsl"]),
                is_set_open_sl=bool(row["is_set_open_sl"]),
                is_set_open_tp=bool(row["is_set_open_tp"]),
                open_sl_param=_maybe_json(row["open_sl_param"]),
                open_tp_param=_maybe_json(row["open_tp_param"]),
                trigger_price=row["trigger_price"],
                order_type=row["order_type"],
                status=row["status"],
                created_at=_parse_iso(row["created_at"]),
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return orders


def load_liquidations(
    conn: sqlite3.Connection, *, source: str, account_id: str | None
) -> list[LiquidationEvent]:
    rows = _fetch(conn, "liquidations", source, account_id)
    events: list[LiquidationEvent] = []
    for row in rows:
        events.append(
            LiquidationEvent(
                liquidation_id=row["liquidation_id"],
                source=row["source"],
                account_id=row["account_id"],
                symbol=row["symbol"],
                side=row["side"],
                size=row["size"],
                entry_price=row["entry_price"],
                exit_price=row["exit_price"],
                total_pnl=row["total_pnl"],
                fee=row["fee"],
                liquidate_fee=row["liquidate_fee"],
                created_at=_parse_iso(row["created_at"]),
                exit_type=row["exit_type"],
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return events


def load_historical_pnl(
    conn: sqlite3.Connection, *, source: str, account_id: str | None
) -> list[PnlRecord]:
    rows = _fetch(conn, "historical_pnl", source, account_id)
    records: list[PnlRecord] = []
    for row in rows:
        records.append(
            PnlRecord(
                record_id=row["record_id"],
                source=row["source"],
                account_id=row["account_id"],
                symbol=row["symbol"],
                side=row["side"],
                size=row["size"],
                exit_time=_parse_iso(row["exit_time"]),
                total_pnl=row["total_pnl"],
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return records


def load_equity_history(
    conn: sqlite3.Connection, *, source: str, account_id: str | None
) -> list[EquitySnapshot]:
    rows = _fetch(conn, "account_equity", source, account_id, timestamp_column="timestamp")
    snapshots: list[EquitySnapshot] = []
    for row in rows:
        snapshots.append(
            EquitySnapshot(
                timestamp=_parse_iso(row["timestamp"]),
                total_value=row["total_value"],
                source=row["source"],
                account_id=row["account_id"],
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return snapshots


def load_account_snapshot(
    conn: sqlite3.Connection, *, source: str, account_id: str | None
) -> dict[str, Any] | None:
    clauses = ["source = ?"]
    params: list[Any] = [source]
    if account_id is None:
        clauses.append("account_id IS NULL")
    else:
        clauses.append("account_id = ?")
        params.append(account_id)
    query = (
        "SELECT * FROM account_snapshots WHERE "
        + " AND ".join(clauses)
        + " ORDER BY timestamp DESC LIMIT 1"
    )
    row = conn.execute(query, params).fetchone()
    if row is None:
        return None
    return {
        "total_equity": row["total_equity"],
        "available_balance": row["available_balance"],
        "margin_balance": row["margin_balance"],
        "timestamp": row["timestamp"],
        "raw": _maybe_json(row["raw_json"]),
    }


def _fetch(
    conn: sqlite3.Connection,
    table: str,
    source: str,
    account_id: str | None,
    *,
    timestamp_column: str | None = None,
) -> list[sqlite3.Row]:
    clauses = ["source = ?"]
    params: list[Any] = [source]
    if account_id is None:
        clauses.append("account_id IS NULL")
    else:
        clauses.append("account_id = ?")
        params.append(account_id)
    order_by = f" ORDER BY {timestamp_column}" if timestamp_column else ""
    query = f"SELECT * FROM {table} WHERE {' AND '.join(clauses)}{order_by}"
    return conn.execute(query, params).fetchall()


def _fetch_all(
    conn: sqlite3.Connection,
    table: str,
    *,
    order_by: str | None = None,
) -> list[sqlite3.Row]:
    clause = f" ORDER BY {order_by}" if order_by else ""
    return conn.execute(f"SELECT * FROM {table}{clause}").fetchall()


def _maybe_json(value: Any) -> Any:
    if value is None:
        return {}
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _parse_iso(value: str | None) -> datetime:
    if value is None:
        raise ValueError("Missing timestamp")
    return datetime.fromisoformat(value)


def load_fills_all(conn: sqlite3.Connection) -> list[Fill]:
    rows = _fetch_all(conn, "fills", order_by="timestamp")
    fills: list[Fill] = []
    for row in rows:
        fills.append(
            Fill(
                fill_id=row["fill_id"],
                order_id=row["order_id"],
                symbol=row["symbol"],
                side=row["side"],
                price=row["price"],
                size=row["size"],
                fee=row["fee"],
                fee_asset=row["fee_asset"],
                timestamp=_parse_iso(row["timestamp"]),
                source=row["source"],
                account_id=row["account_id"],
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return fills


def load_funding_all(conn: sqlite3.Connection) -> list[FundingEvent]:
    rows = _fetch_all(conn, "funding", order_by="funding_time")
    events: list[FundingEvent] = []
    for row in rows:
        events.append(
            FundingEvent(
                funding_id=row["funding_id"],
                transaction_id=row["transaction_id"],
                symbol=row["symbol"],
                side=row["side"],
                rate=row["rate"],
                position_size=row["position_size"],
                price=row["price"],
                funding_time=_parse_iso(row["funding_time"]),
                funding_value=row["funding_value"],
                status=row["status"],
                source=row["source"],
                account_id=row["account_id"],
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return events


def load_orders_all(conn: sqlite3.Connection) -> list[OrderRecord]:
    rows = _fetch_all(conn, "orders", order_by="created_at")
    orders: list[OrderRecord] = []
    for row in rows:
        orders.append(
            OrderRecord(
                order_id=row["order_id"],
                client_order_id=row["client_order_id"],
                source=row["source"],
                account_id=row["account_id"],
                symbol=row["symbol"],
                side=row["side"],
                size=row["size"],
                price=row["price"],
                reduce_only=bool(row["reduce_only"]),
                is_position_tpsl=bool(row["is_position_tpsl"]),
                is_open_tpsl=bool(row["is_open_tpsl"]),
                is_set_open_sl=bool(row["is_set_open_sl"]),
                is_set_open_tp=bool(row["is_set_open_tp"]),
                open_sl_param=_maybe_json(row["open_sl_param"]),
                open_tp_param=_maybe_json(row["open_tp_param"]),
                trigger_price=row["trigger_price"],
                order_type=row["order_type"],
                status=row["status"],
                created_at=_parse_iso(row["created_at"]),
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return orders


def load_liquidations_all(conn: sqlite3.Connection) -> list[LiquidationEvent]:
    rows = _fetch_all(conn, "liquidations", order_by="created_at")
    events: list[LiquidationEvent] = []
    for row in rows:
        events.append(
            LiquidationEvent(
                liquidation_id=row["liquidation_id"],
                source=row["source"],
                account_id=row["account_id"],
                symbol=row["symbol"],
                side=row["side"],
                size=row["size"],
                entry_price=row["entry_price"],
                exit_price=row["exit_price"],
                total_pnl=row["total_pnl"],
                fee=row["fee"],
                liquidate_fee=row["liquidate_fee"],
                created_at=_parse_iso(row["created_at"]),
                exit_type=row["exit_type"],
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return events


def load_equity_history_all(conn: sqlite3.Connection) -> list[EquitySnapshot]:
    rows = _fetch_all(conn, "account_equity", order_by="timestamp")
    snapshots: list[EquitySnapshot] = []
    for row in rows:
        snapshots.append(
            EquitySnapshot(
                timestamp=_parse_iso(row["timestamp"]),
                total_value=row["total_value"],
                source=row["source"],
                account_id=row["account_id"],
                raw=_maybe_json(row["raw_json"]),
            )
        )
    return snapshots


def load_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _fetch_all(conn, "accounts", order_by="name")
    return [dict(row) for row in rows]


def load_tags(conn: sqlite3.Connection, *, active_only: bool = True) -> list[dict[str, Any]]:
    if active_only:
        rows = conn.execute("SELECT * FROM tags WHERE active = 1 ORDER BY name").fetchall()
    else:
        rows = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def load_trade_tags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM trade_tags").fetchall()
    return [dict(row) for row in rows]


def load_benchmark_prices(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    clauses = ["symbol = ?", "timeframe = ?"]
    params: list[Any] = [symbol, timeframe]
    if start is not None:
        clauses.append("timestamp >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("timestamp <= ?")
        params.append(end.isoformat())
    query = "SELECT * FROM benchmark_prices WHERE " + " AND ".join(clauses) + " ORDER BY timestamp"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]
