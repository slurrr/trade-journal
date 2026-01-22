from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from trade_journal.config.accounts import resolve_account_context
from trade_journal.config.app_config import apply_api_settings, load_app_config
from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv
from trade_journal.ingest.apex_equity import load_equity_history_payload
from trade_journal.ingest.apex_funding import load_funding_payload
from trade_journal.ingest.apex_liquidations import extract_liquidations
from trade_journal.ingest.apex_omni import load_fills_payload
from trade_journal.ingest.apex_orders import load_orders_payload
from trade_journal.reconcile import load_historical_pnl_payload
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
)


def main(argv: list[str] | None = None) -> int:
    app_config = load_app_config()
    parser = argparse.ArgumentParser(description="Sync ApeX Omni API data directly into SQLite.")
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
    )
    total_rows = sum(totals.values())
    print(f"synced_rows {total_rows}")
    for name, count in totals.items():
        print(f"{name} {count}")
    return 0


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
) -> dict[str, int]:
    env = dict(os.environ)
    env.update(load_dotenv(env_path))
    app_config = load_app_config()
    env = apply_api_settings(env, app_config, base_url_override=base_url)
    config = ApexApiConfig.from_env(env)
    client = ApexApiClient(config)
    context = resolve_account_context(account, env=env)

    conn = connect(db_path)
    init_db(conn)

    overlap_ms = int(overlap_hours * 60 * 60 * 1000)

    totals: dict[str, int] = {}
    totals["accounts"] = upsert_accounts(
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


if __name__ == "__main__":
    raise SystemExit(main())
