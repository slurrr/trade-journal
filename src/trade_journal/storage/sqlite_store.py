from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from trade_journal.ingest.apex_liquidations import LiquidationEvent
from trade_journal.ingest.apex_orders import OrderRecord
from trade_journal.models import Fill, FundingEvent
from trade_journal.reconcile import PnlRecord


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fills (
            fill_id TEXT PRIMARY KEY,
            order_id TEXT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            fee REAL NOT NULL,
            fee_asset TEXT,
            timestamp TEXT NOT NULL,
            raw_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            client_order_id TEXT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            size REAL NOT NULL,
            price REAL,
            reduce_only INTEGER NOT NULL,
            is_position_tpsl INTEGER NOT NULL,
            is_open_tpsl INTEGER NOT NULL,
            is_set_open_sl INTEGER NOT NULL,
            is_set_open_tp INTEGER NOT NULL,
            open_sl_param TEXT,
            open_tp_param TEXT,
            trigger_price REAL,
            order_type TEXT,
            status TEXT,
            created_at TEXT NOT NULL,
            raw_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS funding (
            funding_id TEXT PRIMARY KEY,
            transaction_id TEXT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            rate REAL NOT NULL,
            position_size REAL NOT NULL,
            price REAL NOT NULL,
            funding_time TEXT NOT NULL,
            funding_value REAL NOT NULL,
            status TEXT,
            raw_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS liquidations (
            liquidation_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            size REAL NOT NULL,
            entry_price REAL,
            exit_price REAL,
            total_pnl REAL,
            fee REAL,
            liquidate_fee REAL,
            created_at TEXT NOT NULL,
            exit_type TEXT,
            raw_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS historical_pnl (
            record_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            size REAL NOT NULL,
            exit_time TEXT NOT NULL,
            total_pnl REAL NOT NULL,
            raw_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            endpoint TEXT PRIMARY KEY,
            last_timestamp_ms INTEGER,
            last_id TEXT,
            last_success_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            schema_version INTEGER NOT NULL,
            metrics_version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO schema_version (id, schema_version, metrics_version, updated_at)
        VALUES (1, 1, 1, CURRENT_TIMESTAMP)
        """
    )
    conn.commit()


def upsert_fills(conn: sqlite3.Connection, fills: Iterable[Fill]) -> int:
    rows = []
    for fill in fills:
        fill_id = fill.fill_id or _hash_id(
            "fill",
            fill.symbol,
            fill.side,
            fill.price,
            fill.size,
            fill.fee,
            fill.fee_asset,
            fill.order_id,
            fill.timestamp,
        )
        rows.append(
            {
                "fill_id": fill_id,
                "order_id": fill.order_id,
                "symbol": fill.symbol,
                "side": fill.side,
                "price": fill.price,
                "size": fill.size,
                "fee": fill.fee,
                "fee_asset": fill.fee_asset,
                "timestamp": fill.timestamp.isoformat(),
                "raw_json": _json_dump(fill.raw),
            }
        )
    conn.executemany(
        """
        INSERT INTO fills (
            fill_id, order_id, symbol, side, price, size, fee, fee_asset, timestamp, raw_json
        )
        VALUES (
            :fill_id, :order_id, :symbol, :side, :price, :size, :fee, :fee_asset, :timestamp, :raw_json
        )
        ON CONFLICT(fill_id) DO UPDATE SET
            order_id=excluded.order_id,
            symbol=excluded.symbol,
            side=excluded.side,
            price=excluded.price,
            size=excluded.size,
            fee=excluded.fee,
            fee_asset=excluded.fee_asset,
            timestamp=excluded.timestamp,
            raw_json=excluded.raw_json
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_orders(conn: sqlite3.Connection, orders: Iterable[OrderRecord]) -> int:
    rows = []
    for order in orders:
        order_id = order.order_id or _hash_id(
            "order",
            order.symbol,
            order.side,
            order.size,
            order.price,
            order.trigger_price,
            order.created_at,
            order.client_order_id,
        )
        rows.append(
            {
                "order_id": order_id,
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "side": order.side,
                "size": order.size,
                "price": order.price,
                "reduce_only": 1 if order.reduce_only else 0,
                "is_position_tpsl": 1 if order.is_position_tpsl else 0,
                "is_open_tpsl": 1 if order.is_open_tpsl else 0,
                "is_set_open_sl": 1 if order.is_set_open_sl else 0,
                "is_set_open_tp": 1 if order.is_set_open_tp else 0,
                "open_sl_param": _json_dump(order.open_sl_param),
                "open_tp_param": _json_dump(order.open_tp_param),
                "trigger_price": order.trigger_price,
                "order_type": order.order_type,
                "status": order.status,
                "created_at": order.created_at.isoformat(),
                "raw_json": _json_dump(order.raw),
            }
        )
    conn.executemany(
        """
        INSERT INTO orders (
            order_id, client_order_id, symbol, side, size, price, reduce_only,
            is_position_tpsl, is_open_tpsl, is_set_open_sl, is_set_open_tp,
            open_sl_param, open_tp_param, trigger_price, order_type, status, created_at, raw_json
        )
        VALUES (
            :order_id, :client_order_id, :symbol, :side, :size, :price, :reduce_only,
            :is_position_tpsl, :is_open_tpsl, :is_set_open_sl, :is_set_open_tp,
            :open_sl_param, :open_tp_param, :trigger_price, :order_type, :status, :created_at, :raw_json
        )
        ON CONFLICT(order_id) DO UPDATE SET
            client_order_id=excluded.client_order_id,
            symbol=excluded.symbol,
            side=excluded.side,
            size=excluded.size,
            price=excluded.price,
            reduce_only=excluded.reduce_only,
            is_position_tpsl=excluded.is_position_tpsl,
            is_open_tpsl=excluded.is_open_tpsl,
            is_set_open_sl=excluded.is_set_open_sl,
            is_set_open_tp=excluded.is_set_open_tp,
            open_sl_param=excluded.open_sl_param,
            open_tp_param=excluded.open_tp_param,
            trigger_price=excluded.trigger_price,
            order_type=excluded.order_type,
            status=excluded.status,
            created_at=excluded.created_at,
            raw_json=excluded.raw_json
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_funding(conn: sqlite3.Connection, events: Iterable[FundingEvent]) -> int:
    rows = []
    for event in events:
        funding_id = event.transaction_id or event.funding_id or _hash_id(
            "funding",
            event.symbol,
            event.side,
            event.funding_time,
            event.position_size,
            event.funding_value,
            event.rate,
            event.price,
        )
        rows.append(
            {
                "funding_id": funding_id,
                "transaction_id": event.transaction_id,
                "symbol": event.symbol,
                "side": event.side,
                "rate": event.rate,
                "position_size": event.position_size,
                "price": event.price,
                "funding_time": event.funding_time.isoformat(),
                "funding_value": event.funding_value,
                "status": event.status,
                "raw_json": _json_dump(event.raw),
            }
        )
    conn.executemany(
        """
        INSERT INTO funding (
            funding_id, transaction_id, symbol, side, rate, position_size, price,
            funding_time, funding_value, status, raw_json
        )
        VALUES (
            :funding_id, :transaction_id, :symbol, :side, :rate, :position_size, :price,
            :funding_time, :funding_value, :status, :raw_json
        )
        ON CONFLICT(funding_id) DO UPDATE SET
            transaction_id=excluded.transaction_id,
            symbol=excluded.symbol,
            side=excluded.side,
            rate=excluded.rate,
            position_size=excluded.position_size,
            price=excluded.price,
            funding_time=excluded.funding_time,
            funding_value=excluded.funding_value,
            status=excluded.status,
            raw_json=excluded.raw_json
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_liquidations(conn: sqlite3.Connection, events: Iterable[LiquidationEvent]) -> int:
    rows = []
    for event in events:
        liquidation_id = event.liquidation_id or _hash_id(
            "liquidation",
            event.symbol,
            event.side,
            event.size,
            event.created_at,
            event.exit_type,
            event.entry_price,
            event.exit_price,
        )
        rows.append(
            {
                "liquidation_id": liquidation_id,
                "symbol": event.symbol,
                "side": event.side,
                "size": event.size,
                "entry_price": event.entry_price,
                "exit_price": event.exit_price,
                "total_pnl": event.total_pnl,
                "fee": event.fee,
                "liquidate_fee": event.liquidate_fee,
                "created_at": event.created_at.isoformat(),
                "exit_type": event.exit_type,
                "raw_json": _json_dump(event.raw),
            }
        )
    conn.executemany(
        """
        INSERT INTO liquidations (
            liquidation_id, symbol, side, size, entry_price, exit_price, total_pnl,
            fee, liquidate_fee, created_at, exit_type, raw_json
        )
        VALUES (
            :liquidation_id, :symbol, :side, :size, :entry_price, :exit_price, :total_pnl,
            :fee, :liquidate_fee, :created_at, :exit_type, :raw_json
        )
        ON CONFLICT(liquidation_id) DO UPDATE SET
            symbol=excluded.symbol,
            side=excluded.side,
            size=excluded.size,
            entry_price=excluded.entry_price,
            exit_price=excluded.exit_price,
            total_pnl=excluded.total_pnl,
            fee=excluded.fee,
            liquidate_fee=excluded.liquidate_fee,
            created_at=excluded.created_at,
            exit_type=excluded.exit_type,
            raw_json=excluded.raw_json
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_historical_pnl(conn: sqlite3.Connection, records: Iterable[PnlRecord]) -> int:
    rows = []
    for record in records:
        record_id = record.record_id or _hash_id(
            "pnl",
            record.symbol,
            record.side,
            record.size,
            record.exit_time,
            record.total_pnl,
        )
        rows.append(
            {
                "record_id": record_id,
                "symbol": record.symbol,
                "side": record.side,
                "size": record.size,
                "exit_time": record.exit_time.isoformat(),
                "total_pnl": record.total_pnl,
                "raw_json": _json_dump(record.raw),
            }
        )
    conn.executemany(
        """
        INSERT INTO historical_pnl (
            record_id, symbol, side, size, exit_time, total_pnl, raw_json
        )
        VALUES (
            :record_id, :symbol, :side, :size, :exit_time, :total_pnl, :raw_json
        )
        ON CONFLICT(record_id) DO UPDATE SET
            symbol=excluded.symbol,
            side=excluded.side,
            size=excluded.size,
            exit_time=excluded.exit_time,
            total_pnl=excluded.total_pnl,
            raw_json=excluded.raw_json
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def _hash_id(prefix: str, *parts: object) -> str:
    normalized = []
    for part in parts:
        if isinstance(part, datetime):
            normalized.append(part.isoformat())
        else:
            normalized.append("" if part is None else str(part))
    digest = hashlib.sha1("|".join(normalized).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _json_dump(value: object) -> str:
    if value is None:
        return "{}"
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    return json.dumps(value, sort_keys=True)
