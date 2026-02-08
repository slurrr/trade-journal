from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from trade_journal.config.accounts import resolve_account_context
from trade_journal.config.app_config import apply_api_settings, load_app_config
from trade_journal import compute_excursions
from trade_journal.ingest.hyperliquid import (
    load_hyperliquid_clearinghouse_state_payload,
    load_hyperliquid_fills_payload,
    load_hyperliquid_historical_orders_payload,
    load_hyperliquid_open_orders_payload,
)
from trade_journal.ingest.hyperliquid_api import HyperliquidInfoClient, HyperliquidInfoConfig
from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv
from trade_journal.ingest.apex_equity import load_equity_history_payload
from trade_journal.ingest.apex_funding import load_funding_payload
from trade_journal.ingest.apex_liquidations import extract_liquidations
from trade_journal.ingest.apex_omni import load_fills_payload
from trade_journal.ingest.apex_orders import load_orders_payload
from trade_journal.metrics.excursions import PriceBar
from trade_journal.pricing.hyperliquid_prices import HyperliquidPriceClient
from trade_journal.reconcile import load_historical_pnl_payload
from trade_journal.reconstruct.trades import reconstruct_trades
from trade_journal.storage import sqlite_reader
from trade_journal.storage.sqlite_store import (
    connect,
    init_db,
    get_sync_state,
    upsert_sync_state,
    upsert_fills,
    upsert_funding,
    upsert_liquidations,
    upsert_historical_pnl,
    upsert_orders,
    upsert_account_equity,
    upsert_accounts,
    upsert_account_snapshots,
    upsert_price_bars,
)

_CANONICAL_TIMEFRAME = "1m"
_CANONICAL_INTERVAL = timedelta(minutes=1)
_BENCHMARK_SYMBOL = "BTC-USDC"


def main(argv: list[str] | None = None) -> int:
    app_config = load_app_config()
    parser = argparse.ArgumentParser(description="Sync venue API data directly into SQLite.")
    parser.add_argument("--db", type=Path, default=app_config.app.db_path, help="SQLite DB path.")
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file.")
    parser.add_argument("--base-url", type=str, default=None, help="Override APEX_BASE_URL.")
    parser.add_argument("--limit", type=int, default=None, help="Records per page (default config).")
    parser.add_argument("--max-pages", type=int, default=app_config.sync.max_pages, help="Maximum pages per endpoint.")
    parser.add_argument(
        "--overlap-hours",
        type=float,
        default=app_config.sync.overlap_hours,
        help="Overlap window in hours when resuming.",
    )
    parser.add_argument("--end-ms", type=int, default=None, help="Optional end timestamp (ms).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe API + normalization without writing venue rows/checkpoints (Hyperliquid only).",
    )
    parser.add_argument(
        "--skip-post-sync",
        action="store_true",
        help="Skip post-sync jobs (excursions + Hyperliquid benchmark bars).",
    )
    args = parser.parse_args(argv)

    limit = args.limit if args.limit is not None else app_config.sync.limit
    totals = sync_once(
        db_path=args.db,
        account=args.account,
        env_path=args.env,
        base_url=args.base_url,
        limit=limit,
        max_pages=args.max_pages,
        overlap_hours=args.overlap_hours,
        end_ms=args.end_ms if args.end_ms is not None else app_config.sync.end_ms,
        dry_run=args.dry_run,
    )
    total_rows = sum(totals.values())
    print(f"synced_rows {total_rows}")
    for name, count in totals.items():
        print(f"{name} {count}")
    if not args.dry_run and not args.skip_post_sync:
        post_totals = run_post_sync_jobs(
            db_path=args.db,
            account=args.account,
            env_path=args.env,
            run_excursions=bool(app_config.sync.run_excursions),
            series_max_points=app_config.sync.series_max_points,
        )
        post_rows = sum(post_totals.values())
        print(f"post_sync_rows {post_rows}")
        for name, count in post_totals.items():
            print(f"{name} {count}")
    return 0


def run_post_sync_jobs(
    *,
    db_path: Path,
    account: str | None,
    env_path: Path,
    run_excursions: bool,
    series_max_points: int | None,
) -> dict[str, int]:
    totals = {
        "excursions": 0,
        "benchmark_price_bars": 0,
    }
    if run_excursions:
        totals["excursions"] = run_excursions_job(
            db_path=db_path,
            account=account,
            series_max_points=series_max_points,
        )
    totals["benchmark_price_bars"] = sync_hyperliquid_benchmark_bars(
        db_path=db_path,
        env_path=env_path,
        symbol=_BENCHMARK_SYMBOL,
    )
    return totals


def run_excursions_job(
    *,
    db_path: Path,
    account: str | None,
    series_max_points: int | None,
) -> int:
    args = ["--db", str(db_path)]
    if account:
        args += ["--account", account]
    if series_max_points is not None:
        args += ["--series-max-points", str(series_max_points)]
    compute_excursions.main(args)
    return 1


def sync_hyperliquid_benchmark_bars(
    *,
    db_path: Path,
    env_path: Path,
    symbol: str,
) -> int:
    conn = connect(db_path)
    init_db(conn)
    try:
        fills = sqlite_reader.load_fills_all(conn)
        apex_trades = reconstruct_trades([fill for fill in fills if fill.source == "apex"])
        window = _benchmark_window_from_trades(apex_trades)
        if window is None:
            return 0
        start, end = window
        latest = _latest_bar_timestamp(
            conn,
            source="hyperliquid",
            symbol=symbol,
            timeframe=_CANONICAL_TIMEFRAME,
        )
    finally:
        conn.close()

    fetch_start = start
    if latest is not None:
        fetch_start = max(start, latest - (_CANONICAL_INTERVAL * 2))
    if fetch_start >= end:
        return 0

    env = dict(os.environ)
    env.update(load_dotenv(env_path))
    price_client = HyperliquidPriceClient(HyperliquidInfoConfig.from_env(env))
    bars = price_client.fetch_bars(symbol, fetch_start, end)
    rows = _bars_to_rows("hyperliquid", symbol, bars)

    conn = connect(db_path)
    init_db(conn)
    try:
        return upsert_price_bars(conn, rows)
    finally:
        conn.close()


def sync_once(
    *,
    db_path: Path,
    account: str | None,
    env_path: Path,
    base_url: str | None,
    limit: int | None,
    max_pages: int,
    overlap_hours: float,
    end_ms: int | None,
    dry_run: bool = False,
) -> dict[str, int]:
    env = dict(os.environ)
    env.update(load_dotenv(env_path))
    app_config = load_app_config()
    env = apply_api_settings(env, app_config, base_url_override=base_url)
    context = resolve_account_context(account, env=env)

    conn = connect(db_path)
    init_db(conn)

    overlap_ms = int(overlap_hours * 60 * 60 * 1000)
    totals: dict[str, int] = {}
    totals["accounts"] = 0 if dry_run else _upsert_account_context(conn, context)

    if context.source == "apex":
        if dry_run:
            raise ValueError("Dry-run mode is currently supported only for source=hyperliquid.")
        config = ApexApiConfig.from_env(env)
        client = ApexApiClient(config)
        totals.update(
            _sync_apex_once(
                conn,
                client,
                context,
                limit=limit,
                max_pages=max_pages,
                overlap_ms=overlap_ms,
                end_ms=end_ms,
            )
        )
        return totals

    if context.source == "hyperliquid":
        hl_config = HyperliquidInfoConfig.from_env(env)
        client = HyperliquidInfoClient(hl_config)
        totals.update(
            _sync_hyperliquid_once(
                conn,
                client,
                context,
                max_pages=max_pages,
                overlap_ms=overlap_ms,
                end_ms=end_ms,
                dry_run=dry_run,
                funding_enabled=_env_truthy(env.get("HYPERLIQUID_ENABLE_FUNDING")),
            )
        )
        return totals

    raise ValueError(f"Unsupported source: {context.source}")


def _upsert_account_context(conn, context) -> int:
    return upsert_accounts(
        conn,
        [
            {
                "source": context.source,
                "account_id": context.account_id or context.name,
                "name": context.name,
                "exchange": context.exchange or context.source,
                "base_currency": context.base_currency,
                "starting_equity": context.starting_equity,
                "active": context.active,
                "raw_json": {
                    "source": context.source,
                    "wallet": context.account_id if context.source == "hyperliquid" else None,
                    "data_dir": str(context.data_dir),
                },
            }
        ],
    )


def _sync_apex_once(
    conn,
    client: ApexApiClient,
    context,
    *,
    limit: int | None,
    max_pages: int,
    overlap_ms: int,
    end_ms: int | None,
) -> dict[str, int]:
    totals: dict[str, int] = {}
    totals["fills"] = _sync_fills(
        conn,
        client,
        context,
        limit=limit,
        max_pages=max_pages,
        overlap_ms=overlap_ms,
        end_ms=end_ms,
    )
    totals["funding"] = _sync_funding(
        conn,
        client,
        context,
        limit=limit,
        max_pages=max_pages,
        overlap_ms=overlap_ms,
        end_ms=end_ms,
    )
    totals["orders"] = _sync_orders(
        conn,
        client,
        context,
        limit=limit,
        max_pages=max_pages,
        overlap_ms=overlap_ms,
    )
    totals["historical_pnl"] = _sync_historical_pnl(
        conn,
        client,
        context,
        limit=limit,
        max_pages=max_pages,
        overlap_ms=overlap_ms,
    )
    totals["equity_history"] = _sync_equity_history(conn, client, context)
    totals["account_snapshot"] = _sync_account_snapshot(conn, client, context)
    return totals


def _sync_fills(
    conn,
    client: ApexApiClient,
    context,
    *,
    limit: int | None,
    max_pages: int,
    overlap_ms: int,
    end_ms: int | None,
) -> int:
    endpoint = _sync_key("fills", context)
    begin_ms = _resume_timestamp(conn, endpoint, overlap_ms)
    records = _fetch_paged(
        lambda page: client.fetch_fills(limit=limit, page=page, begin_ms=begin_ms, end_ms=end_ms),
        _extract_records,
        max_pages=max_pages,
        limit=limit,
        stop_before_ms=None,
        timestamp_key="createdAt",
    )
    result = load_fills_payload(records, source=context.source, account_id=context.account_id)
    count = upsert_fills(conn, result.fills)
    _update_state(conn, endpoint, context, result.fills, lambda item: item.timestamp)
    return count


def _sync_funding(
    conn,
    client: ApexApiClient,
    context,
    *,
    limit: int | None,
    max_pages: int,
    overlap_ms: int,
    end_ms: int | None,
) -> int:
    endpoint = _sync_key("funding", context)
    begin_ms = _resume_timestamp(conn, endpoint, overlap_ms)
    records = _fetch_paged(
        lambda page: client.fetch_funding(limit=limit, page=page, begin_ms=begin_ms, end_ms=end_ms),
        _extract_records,
        max_pages=max_pages,
        limit=limit,
        stop_before_ms=None,
        timestamp_key="fundingTime",
    )
    result = load_funding_payload(records, source=context.source, account_id=context.account_id)
    count = upsert_funding(conn, result.events)
    _update_state(conn, endpoint, context, result.events, lambda item: item.funding_time)
    return count


def _sync_orders(
    conn,
    client: ApexApiClient,
    context,
    *,
    limit: int | None,
    max_pages: int,
    overlap_ms: int,
) -> int:
    endpoint = _sync_key("history_orders", context)
    resume_ms = _resume_timestamp(conn, endpoint, overlap_ms)
    records = _fetch_paged(
        lambda page: client.fetch_history_orders(limit=limit, page=page),
        _extract_records,
        max_pages=max_pages,
        limit=limit,
        stop_before_ms=resume_ms,
        timestamp_key="createdAt",
    )
    result = load_orders_payload(records, source=context.source, account_id=context.account_id)
    count = upsert_orders(conn, result.orders)
    _update_state(conn, endpoint, context, result.orders, lambda item: item.created_at)
    return count


def _sync_historical_pnl(
    conn,
    client: ApexApiClient,
    context,
    *,
    limit: int | None,
    max_pages: int,
    overlap_ms: int,
) -> int:
    endpoint = _sync_key("historical_pnl", context)
    resume_ms = _resume_timestamp(conn, endpoint, overlap_ms)
    records = _fetch_paged(
        lambda page: client.fetch_historical_pnl(limit=limit, page=page),
        _extract_records,
        max_pages=max_pages,
        limit=limit,
        stop_before_ms=resume_ms,
        timestamp_key="createdAt",
    )
    pnl_records = load_historical_pnl_payload(records, source=context.source, account_id=context.account_id)
    count = upsert_historical_pnl(conn, pnl_records)
    liquidation_result = extract_liquidations(records, source=context.source, account_id=context.account_id)
    upsert_liquidations(conn, liquidation_result.events)
    _update_state(conn, endpoint, context, pnl_records, lambda item: item.exit_time)
    return count


def _sync_equity_history(
    conn,
    client: ApexApiClient,
    context,
) -> int:
    payload = client.fetch_equity_history()
    result = load_equity_history_payload(
        payload, source=context.source, account_id=context.account_id, min_value=0.0
    )
    count = upsert_account_equity(conn, result.snapshots)
    _update_state(conn, _sync_key("equity_history", context), context, result.snapshots, lambda item: item.timestamp)
    return count


def _sync_account_snapshot(
    conn,
    client: ApexApiClient,
    context,
) -> int:
    payload = client.fetch_account()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return 0
    snapshot = {
        "account_id": context.account_id,
        "source": context.source,
        "timestamp": _snapshot_timestamp(data),
        "total_equity": _first_float(data, "totalEquity", "totalAccountValue", "equity"),
        "available_balance": _first_float(
            data, "availableBalance", "availableBalanceValue", "available"
        ),
        "margin_balance": _first_float(data, "marginBalance", "marginBalanceValue", "balance"),
        "raw_json": data,
    }
    if snapshot["timestamp"] is None:
        snapshot["timestamp"] = datetime.now(timezone.utc).isoformat()
    return upsert_account_snapshots(conn, [snapshot])


def _sync_hyperliquid_once(
    conn,
    client: HyperliquidInfoClient,
    context,
    *,
    max_pages: int,
    overlap_ms: int,
    end_ms: int | None,
    dry_run: bool,
    funding_enabled: bool,
) -> dict[str, int]:
    totals = {
        "fills": 0,
        "funding": 0,
        "orders": 0,
        "historical_pnl": 0,
        "equity_history": 0,
        "account_snapshot": 0,
    }
    totals["fills"] = _sync_hyperliquid_fills(
        conn,
        client,
        context,
        max_pages=max_pages,
        overlap_ms=overlap_ms,
        end_ms=end_ms,
        dry_run=dry_run,
    )
    totals["funding"] = _sync_hyperliquid_funding_scaffold(
        conn,
        client,
        context,
        overlap_ms=overlap_ms,
        end_ms=end_ms,
        dry_run=dry_run,
        enabled=funding_enabled,
    )
    totals["orders"] = _sync_hyperliquid_orders(
        conn, client, context, dry_run=dry_run
    )
    totals["account_snapshot"] = _sync_hyperliquid_account_snapshot(
        conn, client, context, dry_run=dry_run
    )
    return totals


def _sync_hyperliquid_fills(
    conn,
    client: HyperliquidInfoClient,
    context,
    *,
    max_pages: int,
    overlap_ms: int,
    end_ms: int | None,
    dry_run: bool = False,
) -> int:
    endpoint = _sync_key("fills", context)
    begin_ms = _resume_timestamp(conn, endpoint, overlap_ms)
    cursor_ms = begin_ms if begin_ms is not None else 0
    final_end_ms = int(end_ms) if end_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    if final_end_ms <= cursor_ms:
        final_end_ms = cursor_ms + 1

    total_rows = 0
    latest_ts: datetime | None = None
    latest_fill_id: str | None = None
    saw_cap = False
    stall_count = 0

    for page in range(max_pages):
        records = client.fetch_user_fills_by_time(
            user=context.account_id or "",
            start_ms=cursor_ms,
            end_ms=final_end_ms,
            aggregate_by_time=False,
        )
        if not records:
            break

        result = load_hyperliquid_fills_payload(records, source=context.source, account_id=context.account_id)
        if result.fills:
            total_rows += len(result.fills)
            if not dry_run:
                upsert_fills(conn, result.fills)
            latest_item = max(result.fills, key=lambda item: item.timestamp)
            if latest_ts is None or latest_item.timestamp > latest_ts:
                latest_ts = latest_item.timestamp
                latest_fill_id = latest_item.fill_id

        cap_hit = len(records) >= client.fills_page_limit
        if not cap_hit:
            break
        saw_cap = True

        next_cursor = _max_record_timestamp_ms(records, "time")
        if next_cursor is None:
            print("warning: Hyperliquid fills paging stalled (missing time field); stopping early.")
            break
        if next_cursor < cursor_ms:
            print("warning: Hyperliquid fills paging moved backward in time; stopping early.")
            break
        if next_cursor == cursor_ms:
            stall_count += 1
            if stall_count >= 2:
                print(
                    "warning: Hyperliquid fills paging cursor did not advance; "
                    "results may be truncated by same-millisecond cap."
                )
                break
        else:
            stall_count = 0
        cursor_ms = next_cursor

        if page == max_pages - 1:
            print("warning: Hyperliquid fills sync hit max-pages limit; data may be truncated.")

    if latest_ts is not None and not dry_run:
        upsert_sync_state(
            conn,
            endpoint,
            context.source,
            context.account_id,
            int(latest_ts.timestamp() * 1000),
            latest_fill_id,
        )

    if saw_cap and begin_ms is None and total_rows >= client.fills_recent_cap:
        print(
            "warning: Hyperliquid userFillsByTime appears truncated by recent-history cap; "
            "older fills may be unavailable from this endpoint."
        )

    return total_rows


def _sync_hyperliquid_account_snapshot(
    conn,
    client: HyperliquidInfoClient,
    context,
    *,
    dry_run: bool = False,
) -> int:
    payload = client.fetch_clearinghouse_state(context.account_id or "")
    snapshot = load_hyperliquid_clearinghouse_state_payload(
        payload, source=context.source, account_id=context.account_id
    )
    if snapshot is None:
        return 0
    if dry_run:
        return 1
    count = upsert_account_snapshots(conn, [snapshot])
    upsert_sync_state(
        conn,
        _sync_key("account_snapshot", context),
        context.source,
        context.account_id,
        _iso_to_ms(str(snapshot["timestamp"])),
        None,
    )
    return count


def _sync_hyperliquid_orders(
    conn,
    client: HyperliquidInfoClient,
    context,
    *,
    dry_run: bool = False,
) -> int:
    endpoint = _sync_key("orders", context)
    historical_records = client.fetch_historical_orders(context.account_id or "")
    hist = load_hyperliquid_historical_orders_payload(
        historical_records,
        source=context.source,
        account_id=context.account_id,
    )
    open_records = client.fetch_open_orders(context.account_id or "")
    live = load_hyperliquid_open_orders_payload(
        open_records,
        source=context.source,
        account_id=context.account_id,
    )
    merged_orders = [*hist.orders, *live.orders]
    known_oids = {str(item.order_id) for item in merged_orders if item.order_id}
    backfill_oids = [
        oid
        for oid in _recent_fill_oids(conn, context, limit=2000)
        if oid not in known_oids
    ]
    for oid in backfill_oids:
        try:
            payload = client.fetch_order_status(user=context.account_id or "", oid=oid)
        except Exception:
            continue
        if not payload:
            continue
        rows = _extract_hl_order_status_rows(payload)
        if not rows:
            continue
        parsed = load_hyperliquid_historical_orders_payload(
            rows,
            source=context.source,
            account_id=context.account_id,
        )
        if parsed.orders:
            merged_orders.extend(parsed.orders)
            known_oids.add(oid)
    if dry_run:
        return len(merged_orders)
    count = upsert_orders(conn, merged_orders)
    _update_state(conn, endpoint, context, merged_orders, lambda item: item.created_at)
    return count


def _recent_fill_oids(conn, context, *, limit: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT order_id
        FROM fills
        WHERE source = ? AND account_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (context.source, context.account_id, int(limit)),
    ).fetchall()
    output: list[str] = []
    seen: set[str] = set()
    for row in rows:
        scoped = row[0]
        if not scoped:
            continue
        text = str(scoped)
        oid = text.rsplit(":", 1)[-1]
        if not oid.isdigit():
            continue
        if oid in seen:
            continue
        seen.add(oid)
        output.append(oid)
    return output


def _extract_hl_order_status_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    direct = payload.get("order")
    if isinstance(direct, Mapping):
        rows.append(direct)
    for key in ("orders", "children", "relatedOrders"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend([item for item in value if isinstance(item, Mapping)])
    data = payload.get("data")
    if isinstance(data, Mapping):
        order = data.get("order")
        if isinstance(order, Mapping):
            rows.append(order)
        for key in ("orders", "children", "relatedOrders"):
            value = data.get(key)
            if isinstance(value, list):
                rows.extend([item for item in value if isinstance(item, Mapping)])
    return rows


def _sync_hyperliquid_funding_scaffold(
    conn,
    client: HyperliquidInfoClient,
    context,
    *,
    overlap_ms: int,
    end_ms: int | None,
    dry_run: bool,
    enabled: bool,
) -> int:
    del conn, client, context, overlap_ms, end_ms, dry_run
    if not enabled:
        return 0
    raise NotImplementedError(
        "Hyperliquid funding ingestion is not implemented yet. "
        "Set HYPERLIQUID_ENABLE_FUNDING=false (or unset) to keep funding deferred."
    )


def _sync_key(endpoint: str, context) -> str:
    account = context.account_id or context.name
    return f"{endpoint}:{context.source}:{account}"


def _resume_timestamp(conn, endpoint: str, overlap_ms: int) -> int | None:
    state = get_sync_state(conn, endpoint)
    if not state:
        return None
    last_ms = state.get("last_timestamp_ms")
    if last_ms is None:
        return None
    if isinstance(last_ms, (int, float, str)):
        try:
            last_ms_int = int(last_ms)
        except (TypeError, ValueError):
            return None
    else:
        return None
    return max(0, last_ms_int - overlap_ms)


def _update_state(conn, endpoint: str, context, items: Iterable[Any], get_time: Callable[[Any], datetime]) -> None:
    latest = _max_timestamp(items, get_time)
    if latest is None:
        return
    upsert_sync_state(
        conn,
        endpoint,
        context.source,
        context.account_id,
        int(latest.timestamp() * 1000),
        None,
    )


def _max_timestamp(items: Iterable[Any], get_time: Callable[[Any], datetime]) -> datetime | None:
    latest = None
    for item in items:
        ts = get_time(item)
        if latest is None or ts > latest:
            latest = ts
    return latest


def _fetch_paged(
    fetch_page: Callable[[int], Mapping[str, Any]],
    extract: Callable[[Any], Iterable[Mapping[str, Any]]],
    *,
    max_pages: int,
    limit: int | None,
    stop_before_ms: int | None,
    timestamp_key: str,
) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    page = 0
    while page < max_pages:
        payload = fetch_page(page)
        error = _payload_error(payload)
        if error:
            raise RuntimeError(f"ApeX error while paging: {error}")
        page_records = list(extract(payload))
        if not page_records:
            break
        records.extend(page_records)
        if stop_before_ms is not None:
            oldest = _oldest_timestamp_ms(page_records, timestamp_key)
            if oldest is not None and oldest < stop_before_ms:
                break
        if limit is not None and len(page_records) < limit:
            break
        page += 1
    return records


def _oldest_timestamp_ms(records: list[Mapping[str, Any]], key: str) -> int | None:
    values = []
    for record in records:
        value = record.get(key)
        if value is None:
            continue
        try:
            value_float = float(value)
        except (TypeError, ValueError):
            continue
        if value_float > 1e12:
            values.append(int(value_float))
        else:
            values.append(int(value_float * 1000))
    return min(values) if values else None


def _max_record_timestamp_ms(records: Iterable[Mapping[str, Any]], key: str) -> int | None:
    values: list[int] = []
    for record in records:
        value = record.get(key)
        if value is None:
            continue
        try:
            value_float = float(value)
        except (TypeError, ValueError):
            continue
        if value_float > 1e12:
            values.append(int(value_float))
        else:
            values.append(int(value_float * 1000))
    return max(values) if values else None


def _extract_records(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict):
        for key in ("data", "fills", "orders", "funding", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [record for record in value if isinstance(record, dict)]
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("orders", "list", "records", "funding", "fundingValues", "historicalPnl"):
                value = data.get(key)
                if isinstance(value, list):
                    return [record for record in value if isinstance(record, dict)]
        if isinstance(data, list):
            return [record for record in data if isinstance(record, dict)]
    return []


def _payload_error(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code is None:
        return None
    if str(code) in {"0", "200"}:
        return None
    msg = payload.get("msg", "")
    return f"code={code} msg={msg}"


def _first_float(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    return None


def _snapshot_timestamp(data: dict[str, Any]) -> str | None:
    for key in ("updatedTime", "updateTime", "timestamp"):
        if key in data and data[key] not in (None, ""):
            value = data[key]
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if numeric > 1e12:
                return datetime.fromtimestamp(numeric / 1000, tz=timezone.utc).isoformat()
            return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()
    return None


def _iso_to_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _benchmark_window_from_trades(trades: Iterable[Any]) -> tuple[datetime, datetime] | None:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for trade in trades:
        entry = getattr(trade, "entry_time", None)
        exit_ = getattr(trade, "exit_time", None)
        if not isinstance(entry, datetime) or not isinstance(exit_, datetime):
            continue
        starts.append(_ensure_utc(entry) - _CANONICAL_INTERVAL)
        ends.append(_ensure_utc(exit_) + _CANONICAL_INTERVAL)
    if not starts or not ends:
        return None
    start = min(starts)
    end = max(ends)
    if end <= start:
        return None
    return start, end


def _latest_bar_timestamp(
    conn,
    *,
    source: str,
    symbol: str,
    timeframe: str,
) -> datetime | None:
    row = conn.execute(
        """
        SELECT MAX(timestamp)
        FROM price_bars
        WHERE source = ? AND symbol = ? AND timeframe = ?
        """,
        (source, symbol, timeframe),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    parsed = datetime.fromisoformat(str(row[0]))
    return _ensure_utc(parsed)


def _bars_to_rows(source: str, symbol: str, bars: list[PriceBar]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for bar in bars:
        start = _ensure_utc(bar.start_time)
        rows.append(
            {
                "source": source,
                "symbol": symbol,
                "timeframe": _CANONICAL_TIMEFRAME,
                "timestamp": start.isoformat(),
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


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main())
