from __future__ import annotations

import argparse
import os
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from trade_journal.config.accounts import resolve_account_context
from trade_journal.config.app_config import load_app_config
from trade_journal.metrics.excursions import PriceBar
from trade_journal.pricing.apex_prices import ApexPriceClient, PriceSeriesConfig
from trade_journal.reconstruct.trades import reconstruct_trades
from trade_journal.storage import sqlite_reader
from trade_journal.storage.sqlite_store import connect, init_db, upsert_price_bars


_DB_TIMEFRAME = "1m"
_WINDOW_PAD = timedelta(minutes=1)


def main(argv: list[str] | None = None) -> int:
    app_config = load_app_config()
    parser = argparse.ArgumentParser(
        description="Backfill venue-scoped 1m OHLC bars for ApeX trades into SQLite price_bars."
    )
    parser.add_argument("--db", type=Path, default=app_config.app.db_path, help="SQLite DB path.")
    parser.add_argument(
        "--account",
        type=str,
        default=None,
        help="Optional account name from accounts config (limits fills/trades).",
    )
    parser.add_argument(
        "--apex-interval",
        type=str,
        default=None,
        help='Override ApeX kline interval param (e.g. "1"). DB still stores timeframe="1m".',
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="Optional sleep between API windows (rate limiting).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned windows but do not fetch or write bars.",
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        raise SystemExit(f"DB not found: {args.db}")

    env = dict(os.environ)
    context = resolve_account_context(args.account, env=env)
    if args.account and context.source != "apex":
        raise SystemExit(f"--account {args.account!r} is not an ApeX account (source={context.source!r}).")

    conn = connect(args.db)
    init_db(conn)
    try:
        fills = _load_apex_fills(conn, account_id=context.account_id if args.account else None)
        trades = reconstruct_trades(fills)
    finally:
        conn.close()

    windows_by_symbol = _windows_by_symbol(trades)
    merged_by_symbol = {symbol: _merge_windows(windows) for symbol, windows in windows_by_symbol.items()}
    total_windows = sum(len(windows) for windows in merged_by_symbol.values())
    print(f"apex_trades {len(trades)}")
    print(f"symbols {len(merged_by_symbol)}")
    print(f"windows {total_windows}")

    if args.dry_run:
        for symbol, windows in sorted(merged_by_symbol.items()):
            for start, end in windows:
                print(f"{symbol} {start.isoformat()} -> {end.isoformat()}")
        return 0

    cfg = PriceSeriesConfig.from_settings(app_config.pricing)
    if args.apex_interval:
        cfg = replace(cfg, interval=str(args.apex_interval))
    client = ApexPriceClient(cfg)

    db_conn = connect(args.db)
    init_db(db_conn)
    try:
        stored = 0
        failed = 0
        for symbol, windows in sorted(merged_by_symbol.items()):
            for start, end in windows:
                try:
                    bars = client.fetch_bars(symbol, start, end)
                except Exception as exc:  # noqa: BLE001 - surface the venue error; do not stop batch.
                    failed += 1
                    print(f"FAILED {symbol} {start.isoformat()} -> {end.isoformat()}: {exc}")
                    continue
                stored += upsert_price_bars(db_conn, _bars_to_rows("apex", symbol, bars))
                if args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)
        print(f"stored_rows {stored}")
        print(f"failed_windows {failed}")
        return 0 if failed == 0 else 2
    finally:
        db_conn.close()


def _load_apex_fills(conn, *, account_id: str | None) -> list:
    if account_id is not None:
        return sqlite_reader.load_fills(conn, source="apex", account_id=account_id)
    fills = sqlite_reader.load_fills_all(conn)
    return [fill for fill in fills if getattr(fill, "source", "") == "apex"]


def _windows_by_symbol(trades: Iterable) -> dict[str, list[tuple[datetime, datetime]]]:
    windows: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for trade in trades:
        symbol = str(getattr(trade, "symbol", "") or "").strip().upper()
        entry = getattr(trade, "entry_time", None)
        exit_ = getattr(trade, "exit_time", None)
        if not symbol or not isinstance(entry, datetime) or not isinstance(exit_, datetime):
            continue
        start = _ensure_utc(entry) - _WINDOW_PAD
        end = _ensure_utc(exit_) + _WINDOW_PAD
        if end <= start:
            continue
        windows[symbol].append((start, end))
    return dict(windows)


def _merge_windows(windows: list[tuple[datetime, datetime]], *, gap: timedelta = timedelta(minutes=2)) -> list[tuple[datetime, datetime]]:
    if not windows:
        return []
    ordered = sorted(windows, key=lambda item: item[0])
    merged: list[tuple[datetime, datetime]] = []
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end + gap:
            cur_end = max(cur_end, end)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))
    return merged


def _bars_to_rows(source: str, symbol: str, bars: list[PriceBar]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for bar in bars:
        rows.append(
            {
                "source": source,
                "symbol": symbol,
                "timeframe": _DB_TIMEFRAME,
                "timestamp": bar.start_time.astimezone(timezone.utc).isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": None,
                "trade_count": None,
                "raw_json": {},
            }
        )
    return rows


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())

