from __future__ import annotations

import argparse
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from trade_journal.config.accounts import resolve_account_context
from trade_journal.ingest.apex_api import load_dotenv
from trade_journal.ingest.hyperliquid_api import HyperliquidInfoConfig
from trade_journal.metrics.excursions import PriceBar
from trade_journal.pricing.hyperliquid_prices import HyperliquidPriceClient
from trade_journal.reconstruct.trades import reconstruct_trades
from trade_journal.storage import sqlite_reader
from trade_journal.storage.sqlite_store import connect, init_db, upsert_price_bars


_DB_TIMEFRAME = "1m"
_WINDOW_PAD = timedelta(minutes=1)
_DEFAULT_CHUNK_MINUTES = 24 * 60  # 1 day


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill venue-scoped 1m OHLC bars for Hyperliquid trades into SQLite price_bars, "
            "and backfill Hyperliquid BTC benchmark bars covering the ApeX trade-history span."
        )
    )
    parser.add_argument("--db", type=Path, default=Path("data/trade_journal.sqlite"), help="SQLite DB path.")
    parser.add_argument(
        "--account",
        type=str,
        default=None,
        help="Optional account name from accounts config (limits HL fills/trades).",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="Path to .env (used for optional Hyperliquid settings overrides).",
    )
    parser.add_argument(
        "--benchmark-symbol",
        type=str,
        default="BTC-USDC",
        help="Benchmark symbol to store under source=hyperliquid.",
    )
    parser.add_argument(
        "--benchmark-start",
        type=str,
        default=None,
        help="Optional ISO8601 UTC timestamp to override benchmark start (e.g. 2026-02-02T00:00:00+00:00).",
    )
    parser.add_argument(
        "--benchmark-end",
        type=str,
        default=None,
        help="Optional ISO8601 UTC timestamp to override benchmark end.",
    )
    parser.add_argument(
        "--benchmark-only",
        action="store_true",
        help="Only backfill benchmark bars (skip Hyperliquid trade windows).",
    )
    parser.add_argument(
        "--chunk-minutes",
        type=int,
        default=_DEFAULT_CHUNK_MINUTES,
        help="Chunk size for candleSnapshot requests.",
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
    env.update(load_dotenv(args.env))
    context = resolve_account_context(args.account, env=env)
    if args.account and context.source != "hyperliquid":
        raise SystemExit(
            f"--account {args.account!r} is not a Hyperliquid account (source={context.source!r})."
        )

    conn = connect(args.db)
    init_db(conn)
    try:
        fills_all = sqlite_reader.load_fills_all(conn)
        apex_fills = [fill for fill in fills_all if getattr(fill, "source", "") == "apex"]
        hl_fills = [fill for fill in fills_all if getattr(fill, "source", "") == "hyperliquid"]
        if args.account:
            hl_fills = [fill for fill in hl_fills if getattr(fill, "account_id", None) == context.account_id]
        apex_trades = reconstruct_trades(apex_fills)
        hl_trades = reconstruct_trades(hl_fills)
    finally:
        conn.close()

    hl_merged_by_symbol: dict[str, list[tuple[datetime, datetime]]] = {}
    total_hl_windows = 0
    if not args.benchmark_only:
        hl_windows_by_symbol = _windows_by_symbol(hl_trades)
        hl_merged_by_symbol = {symbol: _merge_windows(windows) for symbol, windows in hl_windows_by_symbol.items()}
        total_hl_windows = sum(len(windows) for windows in hl_merged_by_symbol.values())

    benchmark_window = _resolve_benchmark_window(args.benchmark_start, args.benchmark_end, apex_trades)
    print(f"hyperliquid_trades {len(hl_trades)}")
    print(f"hyperliquid_symbols {len(hl_merged_by_symbol)}")
    print(f"hyperliquid_windows {total_hl_windows}")
    if benchmark_window is None:
        print("benchmark_window none")
    else:
        print(f"benchmark_window {benchmark_window[0].isoformat()} -> {benchmark_window[1].isoformat()}")

    if args.dry_run:
        for symbol, windows in sorted(hl_merged_by_symbol.items()):
            for start, end in windows:
                print(f"{symbol} {start.isoformat()} -> {end.isoformat()}")
        if benchmark_window is not None:
            start, end = benchmark_window
            print(f"{args.benchmark_symbol.upper()} {start.isoformat()} -> {end.isoformat()}")
        return 0

    hl_client = HyperliquidPriceClient(HyperliquidInfoConfig.from_env(env))

    db_conn = connect(args.db)
    init_db(db_conn)
    try:
        stored = 0
        failed = 0

        if not args.benchmark_only:
            for symbol, windows in sorted(hl_merged_by_symbol.items()):
                for start, end in windows:
                    stored_chunk, failed_chunk = _backfill_window(
                        hl_client,
                        db_conn,
                        source="hyperliquid",
                        symbol=symbol,
                        start=start,
                        end=end,
                        chunk_minutes=max(1, int(args.chunk_minutes)),
                        sleep_seconds=max(0.0, float(args.sleep_seconds)),
                    )
                    stored += stored_chunk
                    failed += failed_chunk

        if benchmark_window is not None:
            start, end = benchmark_window
            stored_chunk, failed_chunk = _backfill_window(
                hl_client,
                db_conn,
                source="hyperliquid",
                symbol=str(args.benchmark_symbol).strip().upper(),
                start=start,
                end=end,
                chunk_minutes=max(1, int(args.chunk_minutes)),
                sleep_seconds=max(0.0, float(args.sleep_seconds)),
            )
            stored += stored_chunk
            failed += failed_chunk

        print(f"stored_rows {stored}")
        print(f"failed_windows {failed}")
        return 0 if failed == 0 else 2
    finally:
        db_conn.close()


def _resolve_benchmark_window(
    start_iso: str | None,
    end_iso: str | None,
    apex_trades: Iterable,
) -> tuple[datetime, datetime] | None:
    if start_iso or end_iso:
        if not start_iso or not end_iso:
            raise SystemExit("--benchmark-start and --benchmark-end must be provided together.")
        start = _parse_iso_dt(start_iso)
        end = _parse_iso_dt(end_iso)
        if end <= start:
            raise SystemExit("Invalid benchmark override: end must be after start.")
        return start, end
    return _benchmark_window_from_trades(apex_trades)


def _benchmark_window_from_trades(trades: Iterable) -> tuple[datetime, datetime] | None:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for trade in trades:
        entry = getattr(trade, "entry_time", None)
        exit_ = getattr(trade, "exit_time", None)
        if isinstance(entry, datetime) and isinstance(exit_, datetime):
            starts.append(_ensure_utc(entry))
            ends.append(_ensure_utc(exit_))
    if not starts or not ends:
        return None
    start = min(starts) - _WINDOW_PAD
    end = max(ends) + _WINDOW_PAD
    if end <= start:
        return None
    return start, end


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


def _backfill_window(
    client: HyperliquidPriceClient,
    conn,
    *,
    source: str,
    symbol: str,
    start: datetime,
    end: datetime,
    chunk_minutes: int,
    sleep_seconds: float,
) -> tuple[int, int]:
    cursor = start
    stored = 0
    failed = 0
    chunk = timedelta(minutes=chunk_minutes)
    while cursor < end:
        chunk_end = min(end, cursor + chunk)
        try:
            bars = client.fetch_bars(symbol, cursor, chunk_end)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAILED {symbol} {cursor.isoformat()} -> {chunk_end.isoformat()}: {exc}")
            cursor = chunk_end
            continue
        stored += upsert_price_bars(conn, _bars_to_rows(source, symbol, bars))
        cursor = chunk_end
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return stored, failed


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


def _parse_iso_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip())
    return _ensure_utc(dt)


if __name__ == "__main__":
    raise SystemExit(main())
