from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from trade_journal.config.accounts import (
    AccountContext,
    account_key,
    load_accounts_config,
    resolve_account_context,
    resolve_data_path,
)
from trade_journal.config.app_config import load_app_config
from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_equity import load_equity_history
from trade_journal.ingest.apex_liquidations import load_liquidations
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv
from trade_journal.storage import sqlite_reader
from trade_journal.metrics.equity import apply_equity_at_entry
from trade_journal.metrics.risk import initial_stop_for_trade
from trade_journal.metrics.targets import initial_target_for_trade
from trade_journal.metrics.excursions import PriceBar
from trade_journal.metrics.series import compute_trade_series, downsample_series
from trade_journal.metrics.summary import (
    compute_aggregate_metrics,
    compute_trade_metrics,
    compute_pnl_distribution,
    compute_performance_score,
    compute_symbol_breakdown,
    compute_time_performance,
)
from trade_journal.models import Trade
from trade_journal.ingest.apex_orders import load_orders
from trade_journal.ingest.hyperliquid import load_hyperliquid_clearinghouse_state_payload
from trade_journal.ingest.hyperliquid_api import HyperliquidInfoClient, HyperliquidInfoConfig
from trade_journal.reconstruct.funding import apply_funding_events
from trade_journal.reconstruct.trades import reconstruct_trades
from trade_journal.storage.sqlite_store import connect as sqlite_connect
from trade_journal.storage.sqlite_store import init_db, upsert_account_snapshots
from trade_journal.pricing.apex_prices import ApexPriceClient, PriceSeriesConfig


APP_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_ROOT / "templates"))


app = FastAPI(title="Trade Journal")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

_SYNC_LOCK = asyncio.Lock()
_BASE_SESSION_WINDOWS = {
    "asia": (0, 8 * 60),
    "london": (8 * 60, 16 * 60),
    "ny": (16 * 60, 24 * 60),
}
_BASE_SESSION_LABELS = ("asia", "london", "ny")
_NORMALIZATION_MODES = ("usd", "percent", "r")
_EARLY_EXIT_CAPTURE_PCT = 25.0
_TARGET_TAG_TYPES = {"tp", "target"}
_DURATION_BINS_SECONDS = [
    300,
    900,
    1800,
    3600,
    7200,
    14400,
    28800,
    86400,
    172800,
    432000,
]
_SUPPORTED_VENUES = ("apex", "hyperliquid")
_VENUE_LABELS = {
    "all": "All",
    "apex": "Apex",
    "hyperliquid": "Hyperliquid",
}
_OPEN_POSITION_SIZE_EPSILON = 1e-8
_OPEN_POSITION_PRICE_CACHE_TTL_SECONDS = 14
_OPEN_POSITION_PRICE_CACHE: dict[str, tuple[datetime, float]] = {}
_OPEN_POSITION_REFRESH_AT: dict[str, datetime] = {}


def _resolve_venue(request: Request) -> str:
    raw = (request.query_params.get("venue") or "").strip().lower()
    if raw == "all":
        return "all"
    if raw in _SUPPORTED_VENUES:
        return raw
    context = resolve_account_context(env=os.environ)
    default = (context.source or "").strip().lower()
    if default in _SUPPORTED_VENUES:
        return default
    return "all"


def _venue_context(venue: str) -> dict[str, Any]:
    return {
        "venue": venue,
        "venue_options": [{"value": key, "label": label} for key, label in _VENUE_LABELS.items()],
    }


@app.on_event("startup")
async def _start_auto_sync() -> None:
    if not _auto_sync_enabled():
        return
    asyncio.create_task(_auto_sync_loop())


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    venue = _resolve_venue(request)
    payload = _load_journal_state(venue=venue)
    calendar_data = _calendar_data(payload["trades"])
    data_note = None
    context = {
        "request": request,
        "page": "dashboard",
        "summary": payload["summary"],
        "equity_curve": payload["equity_curve"],
        "daily_pnl": payload["daily_pnl"],
        "pnl_distribution": payload["pnl_distribution"],
        "recent_trades": payload["recent_trades"],
        "symbols": payload["symbols"],
        "liquidations": payload["liquidations"],
        "account": payload.get("account"),
        "open_positions": payload.get("open_positions", []),
        "calendar": calendar_data,
        "data_note": data_note,
        "data_note_class": "",
        **_venue_context(venue),
    }
    return TEMPLATES.TemplateResponse("dashboard.html", context)


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request) -> HTMLResponse:
    venue = _resolve_venue(request)
    payload = _load_journal_state(venue=venue)
    context = {
        "request": request,
        "page": "trades",
        "trades": payload["trades"],
        "symbols": payload["symbols"],
        "data_note": payload["data_note"],
        "data_note_class": _note_class(payload["data_note"]),
        **_venue_context(venue),
    }
    return TEMPLATES.TemplateResponse("trades.html", context)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request) -> HTMLResponse:
    venue = _resolve_venue(request)
    payload = _load_journal_state(venue=venue)
    month_param = request.query_params.get("month")
    calendar_data = _calendar_data(payload["trades"], month_param)
    context = {
        "request": request,
        "page": "calendar",
        "calendar": calendar_data,
        "data_note": None,
        "data_note_class": "",
        **_venue_context(venue),
    }
    return TEMPLATES.TemplateResponse("calendar.html", context)


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request) -> HTMLResponse:
    venue = _resolve_venue(request)
    context = resolve_account_context(env=os.environ)
    db_path = _resolve_db_path()
    if db_path is not None and db_path.exists():
        payload = _load_analytics_state_db(db_path)
    else:
        payload = _load_journal_state(venue=venue)
    trade_items = [item for item in payload["trades"] if venue == "all" or item.get("source") == venue]
    venue_symbols = sorted({item["symbol"] for item in trade_items})
    trade_objects = [
        trade
        for trade in payload.get("trade_objects", [])
        if venue == "all" or trade.source == venue
    ]

    accounts = payload.get("accounts") or _accounts_from_trades(trade_items)
    if venue != "all":
        accounts = [account for account in accounts if str(account.get("source") or "") == venue]
    strategies = payload.get("strategies") or _strategies_from_trades(trade_items)
    exposure_windows = list(_exposure_windows().keys())
    filters = _parse_analytics_filters(request, venue_symbols, accounts, strategies, exposure_windows)
    filtered_items = _filter_trade_items(trade_items, filters)
    filtered_ids = {item["trade_id"] for item in filtered_items}
    filtered_trades = [trade for trade in trade_objects if trade.trade_id in filtered_ids]

    normalization = _resolve_normalization(filtered_trades, filters["normalization"])
    chart_items = _normalized_trade_items(filtered_items, normalization)
    normalized_trades = normalization["trades"]

    initial_equity = context.starting_equity if normalization["mode"] == "usd" else None
    metrics = compute_aggregate_metrics(normalized_trades, initial_equity=initial_equity) if normalized_trades else None
    summary = _summary_payload(metrics) if metrics else _empty_summary()
    performance_score = compute_performance_score(normalized_trades, metrics) if metrics else None
    time_perf = compute_time_performance(normalized_trades) if normalized_trades else {"hourly": [], "weekday": []}
    symbol_breakdown = compute_symbol_breakdown(normalized_trades) if normalized_trades else []
    pnl_distribution = compute_pnl_distribution(normalized_trades) if normalized_trades else {"bins": []}
    daily_pnl = _daily_pnl(chart_items)
    equity_curve = _equity_curve(chart_items)
    diagnostics = _diagnostics_payload(chart_items)
    diagnostics_phase2 = _diagnostics_phase2(normalized_trades, filtered_items)
    diagnostics_table = [
        {
            "symbol": row["symbol"],
            "pnl": row["pnl"],
            "mae": row["mae"],
            "mfe": row["mfe"],
            "etd": row["etd"],
            "ui_id": row.get("ui_id"),
            "capture_pct": row.get("capture_pct"),
            "heat_pct": row.get("heat_pct"),
        }
        for row in diagnostics.get("table", [])
    ]
    calendar_data = _calendar_data(chart_items)
    strategy_attribution = _strategy_attribution(filtered_items, normalized_trades)
    size_buckets = _size_bucket_attribution(filtered_items, normalized_trades)
    regime_attribution = _regime_attribution(filtered_items, normalized_trades)
    duration_charts = _duration_charts(chart_items)
    comparisons = _build_comparisons(
        trade_items=trade_items,
        trade_objects=trade_objects,
        filters=filters,
        normalization=normalization,
        db_path=db_path,
        initial_equity=context.starting_equity,
    )

    context = {
        "request": request,
        "page": "analytics",
        "filters": filters,
        "tab": filters["tab"],
        "normalization": normalization,
        "comparisons": comparisons,
        "summary": summary,
        "performance_score": performance_score,
        "equity_curve": equity_curve,
        "daily_pnl": daily_pnl,
        "pnl_distribution": pnl_distribution,
        "time_performance": time_perf,
        "symbol_breakdown": symbol_breakdown,
        "direction_stats": _direction_analysis(normalized_trades),
        "diagnostics": diagnostics,
        "diagnostics_phase2": diagnostics_phase2,
        "scatter": diagnostics,
        "diagnostics_table": diagnostics_table,
        "trades": filtered_items,
        "calendar": calendar_data,
        "strategy_attribution": strategy_attribution,
        "size_buckets": size_buckets,
        "regime_attribution": regime_attribution,
        "duration_charts": duration_charts,
        "symbols": venue_symbols,
        "accounts": accounts,
        "strategies": strategies,
        "exposure_windows": exposure_windows,
        "query_base": filters["query_base"],
        "data_note": payload.get("data_note"),
        "data_note_class": _note_class(payload.get("data_note")),
        **_venue_context(venue),
    }
    return TEMPLATES.TemplateResponse("analytics.html", context)


@app.get("/trades/{trade_id}", response_class=HTMLResponse)
def trade_detail(request: Request, trade_id: str) -> HTMLResponse:
    venue = _resolve_venue(request)
    payload = _load_journal_state(venue=venue)
    trade = next((item for item in payload["trades"] if item["ui_id"] == trade_id), None)
    context = {
        "request": request,
        "page": "trade",
        "trade": trade,
        "data_note": None,
        "data_note_class": "",
        **_venue_context(venue),
    }
    return TEMPLATES.TemplateResponse("trade_detail.html", context)


@app.get("/api/summary")
def summary_api(request: Request) -> dict[str, Any]:
    venue = _resolve_venue(request)
    payload = _load_journal_state(venue=venue)
    return {
        "summary": payload["summary"],
        "equity_curve": payload["equity_curve"],
        "daily_pnl": payload["daily_pnl"],
        "pnl_distribution": payload["pnl_distribution"],
    }


@app.get("/api/trades")
def trades_api(request: Request) -> list[dict[str, Any]]:
    venue = _resolve_venue(request)
    payload = _load_journal_state(venue=venue)
    return payload["trades"]


@app.get("/api/trades/{trade_id}/series")
def trade_series_api(request: Request, trade_id: str) -> dict[str, Any]:
    venue = _resolve_venue(request)
    payload = _load_journal_state(venue=venue)
    trade = next((item for item in payload["trade_objects"] if _trade_ui_id(item) == trade_id), None)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found.")

    db_path = _resolve_db_path()
    if db_path is not None and db_path.exists():
        conn = sqlite_reader.connect(db_path)
        try:
            bars = _load_trade_bars_db(conn, trade)
        finally:
            conn.close()
        if bars:
            series_points = compute_trade_series(trade, bars)
            series_points = downsample_series(series_points, _series_max_points())
            return {
                "trade_id": trade_id,
                "symbol": trade.symbol,
                "interval": _canonical_price_timeframe(),
                "series": [
                    {
                        "t": int(point.timestamp.timestamp() * 1000),
                        "open": point.open,
                        "high": point.high,
                        "low": point.low,
                        "close": point.close,
                        "entry_return": point.entry_return,
                        "per_unit_unrealized": point.per_unit_unrealized,
                    }
                    for point in series_points
                ],
            }

    context = resolve_account_context(env=os.environ)
    series_path = _resolve_trade_series_path(context)
    if series_path is None:
        raise HTTPException(status_code=404, detail="Trade series cache not found.")

    series_map = _load_trade_series(series_path)
    key = _excursions_key(trade)
    series = series_map.get(key)
    if series is None:
        series = series_map.get(_excursions_key(trade, use_local=True))
    if series is None:
        series = series_map.get(_excursions_key_legacy(trade))
    if series is None:
        series = series_map.get(_excursions_key_legacy(trade, use_local=True))
    return {
        "trade_id": trade_id,
        "symbol": trade.symbol,
        "interval": _price_interval(),
        "series": series or [],
    }


@app.get("/api/account/open-positions")
def account_open_positions_api(request: Request) -> dict[str, Any]:
    venue = _resolve_venue(request)
    context = resolve_account_context(env=os.environ)
    source_param = request.query_params.get("source")
    account_id_param = request.query_params.get("account_id")
    source = (source_param or context.source or "").strip().lower()
    account_id = account_id_param or context.account_id

    if not source_param and not account_id_param:
        _refresh_open_position_snapshots(venue)

    account_snapshot: dict[str, Any] | None = None
    if source_param or account_id_param:
        db_path = _resolve_db_path()
        if db_path is not None and db_path.exists():
            conn = sqlite_reader.connect(db_path)
            try:
                snapshot = sqlite_reader.load_account_snapshot(conn, source=source, account_id=account_id)
            finally:
                conn.close()
            account_snapshot = _with_snapshot_positions(snapshot, source=source, account_id=account_id)
        else:
            payload = _load_journal_state(venue=venue)
            account = payload.get("account")
            if isinstance(account, dict):
                account_snapshot = account
    else:
        payload = _load_journal_state(venue=venue)
        account = payload.get("account")
        if isinstance(account, dict):
            account_snapshot = account
        if venue != "all":
            source = venue
            account_id = None
        else:
            source = "all"
            account_id = None

    account = account_snapshot or {}
    positions = []
    if source_param or account_id_param:
        if isinstance(account, dict):
            positions = account.get("open_positions") or []
    else:
        payload_positions = payload.get("open_positions") if "payload" in locals() else None
        if isinstance(payload_positions, list):
            positions = payload_positions
        elif isinstance(account, dict):
            positions = account.get("open_positions") or []

    return {
        "source": source,
        "account_id": account_id,
        "account": {
            "total_equity": account.get("total_equity") if isinstance(account, dict) else None,
            "available_balance": account.get("available_balance") if isinstance(account, dict) else None,
            "margin_balance": account.get("margin_balance") if isinstance(account, dict) else None,
            "timestamp": account.get("timestamp") if isinstance(account, dict) else None,
        },
        "open_positions": positions,
    }


@app.get("/api/sync-state")
def sync_state_api(request: Request) -> dict[str, Any]:
    db_path = _resolve_db_path()
    if db_path is None or not db_path.exists():
        raise HTTPException(status_code=404, detail="Database not found.")

    context = resolve_account_context(env=os.environ)
    source = request.query_params.get("source") or context.source
    account_id = request.query_params.get("account_id") or context.account_id
    endpoint_prefix = request.query_params.get("endpoint_prefix")

    conn = sqlite_reader.connect(db_path)
    try:
        rows = sqlite_reader.load_sync_states(
            conn,
            source=source,
            account_id=account_id,
            endpoint_prefix=endpoint_prefix,
        )
    finally:
        conn.close()
    for row in rows:
        row["cap_detected"] = bool(row.get("cap_detected"))
    return {
        "source": source,
        "account_id": account_id,
        "endpoint_prefix": endpoint_prefix,
        "states": rows,
    }


def _load_journal_state(*, venue: str | None = None) -> dict[str, Any]:
    context = resolve_account_context(env=os.environ)
    selected_venue = (venue or context.source or "all").strip().lower()
    if selected_venue not in {*_SUPPORTED_VENUES, "all"}:
        selected_venue = "all"
    db_path = _resolve_db_path()
    if db_path is not None and db_path.exists():
        return _load_journal_state_db(context, db_path, venue=selected_venue)
    (
        fills_path,
        funding_path,
        liquidations_path,
        excursions_path,
        orders_path,
        equity_history_path,
    ) = _resolve_paths(context)
    data_note = None

    if fills_path is None:
        note = (
            "No fills file found. "
            f"Place a fills export at {context.data_dir}/fills.json or set TRADE_JOURNAL_FILLS."
        )
        return {
            "summary": {},
            "equity_curve": {},
            "daily_pnl": {},
            "pnl_distribution": {},
            "recent_trades": [],
            "trades": [],
            "symbols": [],
            "liquidations": [],
            "account": None,
            "open_positions": [],
            "data_note": note,
        }

    if selected_venue not in {"all", context.source}:
        return {
            "summary": _empty_summary(),
            "equity_curve": _equity_curve([]),
            "daily_pnl": _daily_pnl([]),
            "pnl_distribution": _pnl_distribution([]),
            "time_performance": _time_performance([]),
            "symbol_breakdown": _symbol_breakdown([]),
            "performance_score": None,
            "account": None,
            "open_positions": [],
            "account_context": {
                "name": context.name,
                "source": selected_venue,
                "account_id": None,
                "data_dir": str(context.data_dir),
            },
            "recent_trades": [],
            "trades": [],
            "trade_objects": [],
            "symbols": [],
            "liquidations": [],
            "data_note": f"No local file data found for venue '{selected_venue}'.",
            "orders": [],
        }

    ingest_result = load_fills(fills_path, source=context.source, account_id=context.account_id)
    trades = reconstruct_trades(ingest_result.fills)

    # Skipped-row counts are useful in CLI stdout but noisy for UI; keep quiet here.

    if funding_path is not None:
        funding_result = load_funding(funding_path, source=context.source, account_id=context.account_id)
        attributions = apply_funding_events(trades, funding_result.events)
        unmatched = sum(1 for item in attributions if item.matched_trade_id is None)
        # Skip-row notes suppressed for UI; see CLI output for ingest warnings.
        if unmatched:
            data_note = _append_note(data_note, f"Unmatched funding events: {unmatched}.")

    orders: list[Any] = []
    if orders_path is not None:
        orders_result = load_orders(orders_path, source=context.source, account_id=context.account_id)
        orders = orders_result.orders
        # Skip-row notes suppressed for UI; see CLI output for ingest warnings.
    elif trades:
        data_note = _append_note(data_note, "Orders file missing: R metrics unavailable.")

    liquidation_events: list[dict[str, Any]] = []
    liquidation_matches: dict[str, dict[str, Any]] = {}
    if liquidations_path is not None:
        liquidation_result = load_liquidations(
            liquidations_path, source=context.source, account_id=context.account_id
        )
        # Skip-row notes suppressed for UI; see CLI output for ingest warnings.

        liquidation_matches = _match_liquidations(trades, liquidation_result.events)
        liquidation_events = _build_liquidation_events(trades, liquidation_result.events, liquidation_matches)

    excursions_map: dict[str, dict[str, Any]] = {}
    if excursions_path is not None:
        try:
            excursions_map = json.loads(excursions_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data_note = _append_note(data_note, "Failed to parse excursions cache.")
    elif trades:
        data_note = _append_note(data_note, "Excursions cache missing: MAE/MFE/ETD shown as n/a.")

    if excursions_map:
        _apply_excursions_cache(trades, excursions_map)

    if equity_history_path is not None:
        try:
            equity_result = load_equity_history(
                equity_history_path,
                source=context.source,
                account_id=context.account_id,
                min_value=0.0,
            )
        except (ValueError, OSError, json.JSONDecodeError):
            data_note = _append_note(data_note, "Failed to parse equity history.")
        else:
            _apply_equity(trades, equity_result.snapshots, context)

    risk_map: dict[str, Any] = {}
    if orders:
        for trade in trades:
            risk = initial_stop_for_trade(trade, orders)
            setattr(trade, "r_multiple", risk.r_multiple)
            setattr(trade, "initial_risk", risk.risk_amount)
            risk_map[trade.trade_id] = risk

    target_map: dict[str, Any] = {}
    if orders:
        for trade in trades:
            target = initial_target_for_trade(trade, orders)
            setattr(trade, "initial_target", target.target_price)
            setattr(trade, "target_pnl", target.target_pnl)
            target_map[trade.trade_id] = target

    metrics = (
        compute_aggregate_metrics(trades, initial_equity=context.starting_equity) if trades else None
    )
    summary = _summary_payload(metrics) if metrics else _empty_summary()
    performance_score = compute_performance_score(trades, metrics) if metrics else None

    trade_items = [
        _trade_payload(
            trade,
            liquidation_matches.get(trade.trade_id),
            risk_map.get(trade.trade_id),
            target_map.get(trade.trade_id),
        )
        for trade in trades
    ]
    trade_items.sort(key=lambda item: item["exit_time"], reverse=True)

    account_snapshot = _load_account_snapshot(context)
    open_positions = _open_positions_from_snapshot(
        account_snapshot,
        source=context.source,
        account_id=context.account_id,
    )

    account_snapshot = _load_account_snapshot(context)
    return {
        "summary": summary,
        "equity_curve": _equity_curve(trade_items),
        "daily_pnl": _daily_pnl(trade_items),
        "pnl_distribution": _pnl_distribution(trade_items),
        "time_performance": _time_performance(trade_items),
        "symbol_breakdown": _symbol_breakdown(trade_items),
        "performance_score": performance_score,
        "account": account_snapshot,
        "open_positions": open_positions,
        "account_context": {
            "name": context.name,
            "source": selected_venue,
            "account_id": context.account_id,
            "data_dir": str(context.data_dir),
        },
        "recent_trades": trade_items[:6],
        "trades": trade_items,
        "trade_objects": trades,
        "symbols": sorted({item["symbol"] for item in trade_items}),
        "liquidations": liquidation_events,
        "data_note": data_note,
        "orders": orders,
    }


def _load_journal_state_db(context, db_path: Path, *, venue: str) -> dict[str, Any]:
    conn = sqlite_reader.connect(db_path)
    from trade_journal.storage.sqlite_store import init_db

    init_db(conn)
    try:
        active_keys = _active_account_keys_from_config()
        fills = sqlite_reader.load_fills_all(conn)
        fills = _filter_by_active_account_keys(fills, active_keys)
        if venue != "all":
            fills = [item for item in fills if item.source == venue]
        trades = reconstruct_trades(fills)
        funding_events = sqlite_reader.load_funding_all(conn)
        funding_events = _filter_by_active_account_keys(funding_events, active_keys)
        if venue != "all":
            funding_events = [item for item in funding_events if item.source == venue]
        attributions = apply_funding_events(trades, funding_events)
        unmatched = sum(1 for item in attributions if item.matched_trade_id is None)
        orders = sqlite_reader.load_orders_all(conn)
        orders = _filter_by_active_account_keys(orders, active_keys)
        if venue != "all":
            orders = [item for item in orders if item.source == venue]
        liquidation_events_raw = sqlite_reader.load_liquidations_all(conn)
        liquidation_events_raw = _filter_by_active_account_keys(liquidation_events_raw, active_keys)
        if venue != "all":
            liquidation_events_raw = [item for item in liquidation_events_raw if item.source == venue]
        equity_snapshots = sqlite_reader.load_equity_history_all(conn)
        equity_snapshots = _filter_by_active_account_keys(equity_snapshots, active_keys)
        if venue != "all":
            equity_snapshots = [item for item in equity_snapshots if item.source == venue]
        accounts = sqlite_reader.load_accounts(conn)
        accounts = _filter_account_rows_by_active_keys(accounts, active_keys)
        account_snapshot, open_positions = _load_account_snapshot_view(conn, venue=venue, default_context=context)
    finally:
        conn.close()

    data_note = None
    if not fills:
        data_note = "No fills found in database."
    if unmatched:
        data_note = _append_note(data_note, f"Unmatched funding events: {unmatched}.")
    if venue in {"hyperliquid", "all"} and not liquidation_events_raw:
        data_note = _append_note(data_note, "Hyperliquid liquidation events are currently unavailable from source data.")

    excursions_map, excursions_found = _load_excursions_map(context, accounts, venue=venue)
    if not excursions_found and trades:
        data_note = _append_note(data_note, "Excursions cache missing: MAE/MFE/ETD shown as n/a.")

    if excursions_map:
        _apply_excursions_cache(trades, excursions_map)

    if equity_snapshots:
        _apply_equity_multi(trades, equity_snapshots, accounts)

    derived_open_positions = _open_positions_from_fills(fills)
    if derived_open_positions:
        existing_keys = {
            (str(item.get("account_key") or ""), str(item.get("symbol") or ""), str(item.get("side") or ""))
            for item in open_positions
        }
        for item in derived_open_positions:
            key = (str(item.get("account_key") or ""), str(item.get("symbol") or ""), str(item.get("side") or ""))
            if key in existing_keys:
                continue
            open_positions.append(item)
        open_positions.sort(key=lambda item: (str(item.get("symbol") or ""), str(item.get("side") or "")))

    liquidation_matches = _match_liquidations(trades, liquidation_events_raw)
    liquidation_events = _build_liquidation_events(trades, liquidation_events_raw, liquidation_matches)

    risk_map: dict[str, Any] = {}
    if orders:
        for trade in trades:
            risk = initial_stop_for_trade(trade, orders)
            setattr(trade, "r_multiple", risk.r_multiple)
            setattr(trade, "initial_risk", risk.risk_amount)
            risk_map[trade.trade_id] = risk

    target_map: dict[str, Any] = {}
    if orders:
        for trade in trades:
            target = initial_target_for_trade(trade, orders)
            setattr(trade, "initial_target", target.target_price)
            setattr(trade, "target_pnl", target.target_pnl)
            target_map[trade.trade_id] = target

    metrics = (
        compute_aggregate_metrics(trades, initial_equity=context.starting_equity) if trades else None
    )
    summary = _summary_payload(metrics) if metrics else _empty_summary()
    performance_score = compute_performance_score(trades, metrics) if metrics else None

    trade_items = [
        _trade_payload(
            trade,
            liquidation_matches.get(trade.trade_id),
            risk_map.get(trade.trade_id),
            target_map.get(trade.trade_id),
        )
        for trade in trades
    ]
    trade_items.sort(key=lambda item: item["exit_time"], reverse=True)
    return {
        "summary": summary,
        "equity_curve": _equity_curve(trade_items),
        "daily_pnl": _daily_pnl(trade_items),
        "pnl_distribution": _pnl_distribution(trade_items),
        "time_performance": _time_performance(trade_items),
        "symbol_breakdown": _symbol_breakdown(trade_items),
        "performance_score": performance_score,
        "account": account_snapshot,
        "open_positions": open_positions,
        "account_context": {
            "name": context.name,
            "source": venue,
            "account_id": None,
            "data_dir": str(context.data_dir),
            "db_path": str(db_path),
        },
        "recent_trades": trade_items[:6],
        "trades": trade_items,
        "trade_objects": trades,
        "symbols": sorted({item["symbol"] for item in trade_items}),
        "liquidations": liquidation_events,
        "data_note": data_note,
    }


def _load_analytics_state_db(db_path: Path) -> dict[str, Any]:
    conn = sqlite_reader.connect(db_path)
    from trade_journal.storage.sqlite_store import init_db

    init_db(conn)
    try:
        active_keys = _active_account_keys_from_config()
        fills = sqlite_reader.load_fills_all(conn)
        fills = _filter_by_active_account_keys(fills, active_keys)
        trades = reconstruct_trades(fills)
        funding_events = sqlite_reader.load_funding_all(conn)
        funding_events = _filter_by_active_account_keys(funding_events, active_keys)
        attributions = apply_funding_events(trades, funding_events)
        unmatched = sum(1 for item in attributions if item.matched_trade_id is None)
        orders = sqlite_reader.load_orders_all(conn)
        orders = _filter_by_active_account_keys(orders, active_keys)
        liquidation_events_raw = sqlite_reader.load_liquidations_all(conn)
        liquidation_events_raw = _filter_by_active_account_keys(liquidation_events_raw, active_keys)
        equity_snapshots = sqlite_reader.load_equity_history_all(conn)
        equity_snapshots = _filter_by_active_account_keys(equity_snapshots, active_keys)
        accounts = sqlite_reader.load_accounts(conn)
        accounts = _filter_account_rows_by_active_keys(accounts, active_keys)
        tags = sqlite_reader.load_tags(conn, active_only=True)
        trade_tags = sqlite_reader.load_trade_tags(conn)
        regimes = sqlite_reader.load_regime_series(conn)
    finally:
        conn.close()

    data_note = None
    if not fills:
        data_note = "No fills found in database."
    if unmatched:
        data_note = _append_note(data_note, f"Unmatched funding events: {unmatched}.")

    context = resolve_account_context(env=os.environ)
    excursions_map, excursions_found = _load_excursions_map(context, accounts, venue="all")
    if not excursions_found and trades:
        data_note = _append_note(data_note, "Excursions cache missing: MAE/MFE/ETD shown as n/a.")

    if excursions_map:
        _apply_excursions_cache(trades, excursions_map)

    if equity_snapshots:
        _apply_equity_multi(trades, equity_snapshots, accounts)

    liquidation_matches = _match_liquidations(trades, liquidation_events_raw)
    liquidation_events = _build_liquidation_events(trades, liquidation_events_raw, liquidation_matches)

    risk_map: dict[str, Any] = {}
    if orders:
        for trade in trades:
            risk = initial_stop_for_trade(trade, orders)
            setattr(trade, "r_multiple", risk.r_multiple)
            setattr(trade, "initial_risk", risk.risk_amount)
            risk_map[trade.trade_id] = risk

    target_map: dict[str, Any] = {}
    if orders:
        for trade in trades:
            target = initial_target_for_trade(trade, orders)
            setattr(trade, "initial_target", target.target_price)
            setattr(trade, "target_pnl", target.target_pnl)
            target_map[trade.trade_id] = target

    trade_items = [
        _trade_payload(
            trade,
            liquidation_matches.get(trade.trade_id),
            risk_map.get(trade.trade_id),
            target_map.get(trade.trade_id),
        )
        for trade in trades
    ]
    trade_items.sort(key=lambda item: item["exit_time"], reverse=True)

    strategies = _apply_trade_tags(trade_items, tags, trade_tags)
    _apply_regimes(trade_items, trades, regimes)
    active_accounts = []
    for row in accounts:
        source = str(row.get("source") or "")
        acct_id = str(row.get("account_id") or "")
        scoped_key = account_key(source, acct_id)
        label_name = str(row.get("name") or acct_id or scoped_key)
        active_accounts.append(
            {
                **row,
                "active": 1,
                "account_key": scoped_key,
                "label": f"{label_name} ({source})" if source else label_name,
            }
        )

    return {
        "trades": trade_items,
        "trade_objects": trades,
        "symbols": sorted({item["symbol"] for item in trade_items}),
        "liquidations": liquidation_events,
        "data_note": data_note,
        "accounts": active_accounts,
        "strategies": strategies,
        "orders": orders,
        "regimes": regimes,
    }


def _resolve_paths(
    context,
) -> tuple[Path | None, Path | None, Path | None, Path | None, Path | None, Path | None]:
    app_config = load_app_config()
    fills_path = resolve_data_path(None, context, "fills.json")
    if not fills_path.exists():
        fills_path = resolve_data_path(None, context, "fills.csv")
    if not fills_path.exists():
        fills_path = None

    funding_path = None
    candidate = resolve_data_path(None, context, "funding.json")
    if candidate.exists():
        funding_path = candidate
    liquidations_path = None
    candidate = resolve_data_path(None, context, "liquidations.json")
    if candidate.exists():
        liquidations_path = candidate
    else:
        candidate = resolve_data_path(None, context, "historical_pnl.json")
        if candidate.exists():
            liquidations_path = candidate

    excursions_path = None
    candidate = app_config.paths.excursions
    if candidate and candidate.exists():
        excursions_path = candidate
    else:
        candidate = resolve_data_path(None, context, "excursions.json")
        if candidate.exists():
            excursions_path = candidate

    orders_path = None
    candidate = resolve_data_path(None, context, "history_orders.json")
    if candidate.exists():
        orders_path = candidate
    else:
        candidate = resolve_data_path(None, context, "open_orders.json")
        if candidate.exists():
            orders_path = candidate

    equity_path = None
    candidate = app_config.paths.equity_history
    if candidate and candidate.exists():
        equity_path = candidate
    else:
        candidate = resolve_data_path(None, context, "equity_history.json")
        if candidate.exists():
            equity_path = candidate

    return fills_path, funding_path, liquidations_path, excursions_path, orders_path, equity_path


def _load_excursions_map(
    context,
    accounts: list[dict[str, Any]],
    *,
    venue: str,
) -> tuple[dict[str, dict[str, Any]], bool]:
    app_config = load_app_config()
    candidate = app_config.paths.excursions
    paths: list[Path] = []
    if candidate and candidate.exists():
        paths.append(candidate)
    else:
        for account in accounts:
            source = str(account.get("source") or "").strip().lower()
            if venue != "all" and source != venue:
                continue
            raw = account.get("raw_json")
            data_dir: str | None = None
            if isinstance(raw, dict):
                raw_dir = raw.get("data_dir")
                if raw_dir:
                    data_dir = str(raw_dir)
            if not data_dir:
                continue
            path = Path(data_dir) / "excursions.json"
            if path.exists():
                paths.append(path)
        fallback = resolve_data_path(None, context, "excursions.json")
        if fallback.exists():
            paths.append(fallback)

    seen: set[Path] = set()
    merged: dict[str, dict[str, Any]] = {}
    found_any = False
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        found_any = True
        for key, value in payload.items():
            if isinstance(value, dict):
                merged[str(key)] = value
    return merged, found_any


def _open_positions_from_fills(fills: list[Any]) -> list[dict[str, Any]]:
    positions: dict[tuple[str, str], dict[str, Any]] = {}
    for fill in fills:
        source = getattr(fill, "source", "")
        account_id = getattr(fill, "account_id", None)
        symbol = getattr(fill, "symbol", None)
        side = str(getattr(fill, "side", "")).upper()
        size = getattr(fill, "size", None)
        price = getattr(fill, "price", None)
        if not symbol:
            continue
        try:
            size_value = float(size)
        except (TypeError, ValueError):
            continue
        if size_value <= 0:
            continue
        direction = 1.0 if side in {"BUY", "LONG"} else -1.0
        key = (account_key(str(source or ""), account_id), str(symbol))
        slot = positions.setdefault(
            key,
            {
                "account_key": key[0],
                "symbol": str(symbol),
                "net_size": 0.0,
                "last_price": None,
            },
        )
        slot["net_size"] = float(slot["net_size"]) + direction * size_value
        try:
            slot["last_price"] = float(price)
        except (TypeError, ValueError):
            pass

    output: list[dict[str, Any]] = []
    for item in positions.values():
        net_size = float(item.get("net_size") or 0.0)
        if abs(net_size) <= _OPEN_POSITION_SIZE_EPSILON:
            continue
        output.append(
            {
                "account_key": item["account_key"],
                "symbol": item["symbol"],
                "side": "LONG" if net_size > 0 else "SHORT",
                "size": abs(net_size),
                "entry_price": item.get("last_price"),
                "position_value": None,
                "unrealized_pnl": None,
                "leverage": None,
                "margin_used": None,
                "liquidation_price": None,
                "return_on_equity": None,
            }
        )
    output.sort(key=lambda item: (str(item.get("symbol") or ""), str(item.get("side") or "")))
    return output


def _resolve_db_path() -> Path | None:
    app_config = load_app_config()
    return app_config.app.db_path


def _apply_equity(trades: list, snapshots, context) -> None:
    apply_equity_at_entry(trades, snapshots, fallback_equity=context.starting_equity)


def _apply_equity_multi(
    trades: list,
    snapshots: list,
    accounts: list[dict[str, Any]],
) -> None:
    fallback_equity = None
    account_map = {
        account_key(str(account.get("source") or ""), account.get("account_id")): account
        for account in accounts
        if account.get("account_id")
    }
    trades_by_key: dict[tuple[str, str | None], list] = defaultdict(list)
    snapshots_by_key: dict[tuple[str, str | None], list] = defaultdict(list)

    for trade in trades:
        trades_by_key[(trade.source, trade.account_id)].append(trade)
    for snap in snapshots:
        snapshots_by_key[(snap.source, snap.account_id)].append(snap)

    for key, trade_group in trades_by_key.items():
        scoped_account_key = account_key(key[0], key[1])
        fallback = None
        if scoped_account_key in account_map:
            fallback = account_map[scoped_account_key].get("starting_equity")
        if fallback is None:
            fallback = fallback_equity
        apply_equity_at_entry(
            trade_group,
            snapshots_by_key.get(key, []),
            fallback_equity=fallback,
        )


def _auto_sync_enabled() -> bool:
    app_config = load_app_config()
    return bool(app_config.sync.auto_sync)


async def _auto_sync_loop() -> None:
    interval = _sync_interval_seconds()
    while True:
        await _run_auto_sync_once()
        await asyncio.sleep(interval)


async def _run_auto_sync_once() -> None:
    db_path = _resolve_db_path()
    if db_path is None:
        return
    try:
        if _SYNC_LOCK.locked():
            return
        async with _SYNC_LOCK:
            did_sync = await asyncio.to_thread(_sync_once)
            if did_sync and _sync_runs_excursions():
                await asyncio.to_thread(_run_excursions, db_path)
            if did_sync:
                await asyncio.to_thread(_run_benchmark_bar_sync, db_path)
    except Exception as exc:
        print(f"Auto-sync failed: {exc}")


def _sync_once() -> bool:
    from trade_journal import sync_api

    app_config = load_app_config()
    env_path = app_config.app.env_path
    db_path = _resolve_db_path() or Path("data/trade_journal.sqlite")
    account_names = _auto_sync_account_names()
    if not account_names:
        return False
    for account_name in account_names:
        sync_api.sync_once(
            db_path=db_path,
            account=account_name,
            env_path=env_path,
            base_url=app_config.api.base_url,
            limit=_sync_limit(),
            max_pages=_sync_max_pages(),
            overlap_hours=_sync_overlap_hours(),
            end_ms=_sync_end_ms(),
        )
    return True


def _auto_sync_account_names() -> list[str | None]:
    forced_account = os.environ.get("TRADE_JOURNAL_ACCOUNT_NAME")
    if forced_account:
        return [forced_account]
    config_path = Path(os.environ.get("TRADE_JOURNAL_ACCOUNTS_CONFIG", "config/accounts.toml"))
    cfg = load_accounts_config(config_path)
    if not cfg.accounts:
        return [None]
    active = [name for name, account in cfg.accounts.items() if account.active]
    if active:
        return active
    return []


def _run_excursions(db_path: Path) -> None:
    from trade_journal import compute_excursions

    app_config = load_app_config()
    max_points = _series_max_points()

    def build_args(account_name: str | None) -> list[str]:
        args = ["--db", str(db_path)]
        if account_name:
            args += ["--account", account_name]
        if max_points is not None:
            args += ["--series-max-points", str(max_points)]
        return args

    # If output paths are globally pinned, keep single-run behavior to avoid overwrite churn.
    if app_config.paths.excursions is not None or app_config.paths.trade_series is not None:
        compute_excursions.main(build_args(os.environ.get("TRADE_JOURNAL_ACCOUNT_NAME")))
        return

    forced_account = os.environ.get("TRADE_JOURNAL_ACCOUNT_NAME")
    if forced_account:
        compute_excursions.main(build_args(forced_account))
        return

    configured_names = [name for name in _auto_sync_account_names() if isinstance(name, str) and name]
    if not configured_names:
        compute_excursions.main(build_args(None))
        return

    for name in sorted(set(configured_names)):
        compute_excursions.main(build_args(name))


def _run_benchmark_bar_sync(db_path: Path) -> None:
    from trade_journal import sync_api

    app_config = load_app_config()
    sync_api.sync_hyperliquid_benchmark_bars(
        db_path=db_path,
        env_path=app_config.app.env_path,
        symbol="BTC-USDC",
    )


def _refresh_open_position_snapshots(venue: str) -> None:
    now = datetime.now(timezone.utc)
    targets = _contexts_for_venue(venue)
    for context in targets:
        source = context.source
        last = _OPEN_POSITION_REFRESH_AT.get(source)
        if last is not None and (now - last).total_seconds() < 15:
            continue
        if _refresh_single_snapshot(context):
            _OPEN_POSITION_REFRESH_AT[source] = now


def _contexts_for_venue(venue: str) -> list[AccountContext]:
    env = os.environ
    config_path = Path(env.get("TRADE_JOURNAL_ACCOUNTS_CONFIG", "config/accounts.toml"))
    cfg = load_accounts_config(config_path)
    if not cfg.accounts:
        context = resolve_account_context(env=env)
        return [context] if venue in {"all", context.source} else []
    targets: list[AccountContext] = []
    for name in cfg.accounts:
        try:
            context = resolve_account_context(name, env=env, config_path=config_path)
        except Exception:
            continue
        if not context.active:
            continue
        if venue != "all" and context.source != venue:
            continue
        targets.append(context)
    return targets


def _refresh_single_snapshot(context: AccountContext) -> bool:
    db_path = _resolve_db_path()
    if db_path is None:
        return False
    conn = sqlite_connect(db_path)
    init_db(conn)
    try:
        if context.source == "hyperliquid":
            config = HyperliquidInfoConfig.from_env(os.environ)
            client = HyperliquidInfoClient(config)
            payload = client.fetch_clearinghouse_state(context.account_id or "")
            snapshot = load_hyperliquid_clearinghouse_state_payload(
                payload,
                source=context.source,
                account_id=context.account_id,
            )
            if snapshot is None:
                return False
            return upsert_account_snapshots(conn, [snapshot]) > 0
        if context.source == "apex":
            app_config = load_app_config()
            env = dict(os.environ)
            env.update(load_dotenv(app_config.app.env_path))
            config = ApexApiConfig.from_env(env)
            client = ApexApiClient(config)
            payload = client.fetch_account()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                return False
            snapshot = {
                "account_id": context.account_id,
                "source": context.source,
                "timestamp": _first_timestamp(data, "updatedTime", "updateTime", "timestamp")
                or datetime.now(timezone.utc).isoformat(),
                "total_equity": _first_float(data, "totalEquity", "totalAccountValue", "equity"),
                "available_balance": _first_float(
                    data,
                    "availableBalance",
                    "availableBalanceValue",
                    "available",
                ),
                "margin_balance": _first_float(data, "marginBalance", "marginBalanceValue", "balance"),
                "raw_json": data,
            }
            return upsert_account_snapshots(conn, [snapshot]) > 0
        return False
    except Exception:
        return False
    finally:
        conn.close()


def _sync_interval_seconds() -> int:
    app_config = load_app_config()
    return max(60, int(app_config.sync.interval_seconds))


def _sync_overlap_hours() -> float:
    app_config = load_app_config()
    return float(app_config.sync.overlap_hours)


def _sync_limit() -> int | None:
    app_config = load_app_config()
    return app_config.sync.limit


def _sync_max_pages() -> int:
    app_config = load_app_config()
    return int(app_config.sync.max_pages)


def _sync_end_ms() -> int | None:
    app_config = load_app_config()
    return app_config.sync.end_ms


def _sync_runs_excursions() -> bool:
    app_config = load_app_config()
    return bool(app_config.sync.run_excursions)


def _series_max_points() -> int | None:
    app_config = load_app_config()
    return app_config.sync.series_max_points


def _append_note(existing: str | None, extra: str) -> str:
    if not existing:
        return extra
    return f"{existing} {extra}"


def _strip_unmatched_funding(note: str | None) -> str | None:
    if not note:
        return note
    import re

    cleaned = re.sub(r"Unmatched funding events:\\s*\\d+\\.", "", note).strip()
    cleaned = re.sub(r"\\s{2,}", " ", cleaned).strip()
    return cleaned or None


def _note_class(note: str | None) -> str:
    if not note:
        return ""
    if "Unmatched funding events" in note:
        return "notice-hero"
    return ""


def _trade_payload(
    trade,
    liquidation: dict[str, Any] | None,
    risk_summary,
    target_summary,
) -> dict[str, Any]:
    metrics = compute_trade_metrics(trade)
    ui_id = _trade_ui_id(trade)
    capture_pct, heat_pct = _capture_heat_pct(trade)
    entry_session = _base_session_label(trade.entry_time)
    exit_session = _base_session_label(trade.exit_time)
    exposures = _session_exposures(trade.entry_time, trade.exit_time)
    payload = {
        "ui_id": ui_id,
        "trade_id": trade.trade_id,
        "source": trade.source,
        "account_id": trade.account_id,
        "account_key": account_key(trade.source, trade.account_id),
        "symbol": trade.symbol,
        "side": trade.side,
        "entry_time": trade.entry_time,
        "exit_time": trade.exit_time,
        "entry_session": entry_session,
        "exit_session": exit_session,
        "session": exit_session,
        "session_exposures": exposures,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "entry_size": trade.entry_size,
        "exit_size": trade.exit_size,
        "max_size": trade.max_size,
        "realized_pnl": trade.realized_pnl,
        "realized_pnl_net": trade.realized_pnl_net,
        "fees": trade.fees,
        "funding_fees": trade.funding_fees,
        "equity_at_entry": getattr(trade, "equity_at_entry", None),
        "mae": trade.mae,
        "mfe": trade.mfe,
        "etd": trade.etd,
        "capture_pct": capture_pct,
        "heat_pct": heat_pct,
        "fills": trade.fills,
        "outcome": metrics.outcome,
        "return_pct": metrics.return_pct,
        "duration_seconds": metrics.duration_seconds,
        "liquidated": liquidation is not None,
        "liquidation": liquidation,
        "initial_stop": risk_summary.stop_price if risk_summary else None,
        "initial_risk": risk_summary.risk_amount if risk_summary else None,
        "r_multiple": risk_summary.r_multiple if risk_summary else None,
        "stop_source": risk_summary.source if risk_summary else None,
        "initial_target": target_summary.target_price if target_summary else None,
        "target_pnl": target_summary.target_pnl if target_summary else None,
        "target_source": target_summary.source if target_summary else None,
    }
    for window, seconds in exposures.items():
        payload[f"exp_{window}_seconds"] = seconds
    return payload


def _apply_trade_tags(
    items: list[dict[str, Any]],
    tags: list[dict[str, Any]],
    trade_tags: list[dict[str, Any]],
) -> list[str]:
    tag_map = {tag["tag_id"]: tag for tag in tags if tag.get("tag_id")}
    trade_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in trade_tags:
        trade_id = link.get("trade_id")
        tag_id = link.get("tag_id")
        if not trade_id or not tag_id:
            continue
        tag = tag_map.get(tag_id)
        if tag is None:
            continue
        trade_map[trade_id].append(tag)

    strategies: set[str] = set()
    for item in items:
        tag_list = trade_map.get(item["trade_id"], [])
        item["tags"] = [tag["name"] for tag in tag_list]
        tags_by_type: dict[str, list[str]] = defaultdict(list)
        for tag in tag_list:
            tag_type = (tag.get("type") or "other").strip()
            tag_name = tag.get("name")
            if not tag_name:
                continue
            tag_name_value = str(tag_name)
            tags_by_type[tag_type].append(tag_name_value)
            if tag_type == "strategy":
                strategies.add(tag_name_value)
        item["tags_by_type"] = dict(tags_by_type)
        item["strategy_tags"] = tags_by_type.get("strategy", [])
    return sorted(strategies)


def _accounts_from_trades(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_keys = _active_account_keys_from_config()
    accounts: dict[str, dict[str, str]] = {}
    for item in items:
        account_id = item.get("account_id")
        source = item.get("source")
        if not account_id:
            continue
        key = account_key(str(source or ""), account_id)
        if active_keys is not None and key not in active_keys:
            continue
        accounts[key] = {
            "account_key": key,
            "account_id": str(account_id),
            "source": str(source or ""),
            "label": key,
        }
    return [accounts[key] for key in sorted(accounts)]


def _strategies_from_trades(items: list[dict[str, Any]]) -> list[str]:
    strategies: set[str] = set()
    for item in items:
        for strategy in item.get("strategy_tags") or []:
            if not strategy:
                continue
            strategies.add(str(strategy))
    return sorted(strategies)


def _active_account_keys_from_config() -> set[str] | None:
    config_path = Path(os.environ.get("TRADE_JOURNAL_ACCOUNTS_CONFIG", "config/accounts.toml"))
    cfg = load_accounts_config(config_path)
    if not cfg.accounts:
        return None
    keys: set[str] = set()
    for account in cfg.accounts.values():
        if not account.active:
            continue
        keys.add(account_key(account.source, account.account_id or account.name))
    return keys


def _filter_by_active_account_keys(items: list[Any], active_keys: set[str] | None) -> list[Any]:
    if active_keys is None:
        return items
    filtered: list[Any] = []
    for item in items:
        source = getattr(item, "source", "")
        account_id = getattr(item, "account_id", None)
        scoped = account_key(str(source or ""), account_id)
        if scoped in active_keys:
            filtered.append(item)
    return filtered


def _filter_account_rows_by_active_keys(
    rows: list[dict[str, Any]],
    active_keys: set[str] | None,
) -> list[dict[str, Any]]:
    if active_keys is None:
        return rows
    filtered: list[dict[str, Any]] = []
    for row in rows:
        scoped = account_key(str(row.get("source") or ""), row.get("account_id"))
        if scoped in active_keys:
            filtered.append(row)
    return filtered


def _trade_ui_id(trade) -> str:
    parts = [
        trade.source,
        trade.account_id or "",
        trade.symbol,
        trade.side,
        trade.entry_time.isoformat(),
        trade.exit_time.isoformat(),
        f"{trade.entry_price:.8f}",
        f"{trade.exit_price:.8f}",
        f"{trade.entry_size:.8f}",
        f"{trade.exit_size:.8f}",
        f"{trade.realized_pnl:.8f}",
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:12]


def _excursions_key(trade, use_local: bool = False) -> str:
    parts = [
        trade.source,
        trade.account_id or "",
        trade.symbol,
        trade.side,
        (trade.entry_time.astimezone() if use_local else trade.entry_time).isoformat(),
        (trade.exit_time.astimezone() if use_local else trade.exit_time).isoformat(),
        f"{trade.entry_price:.8f}",
        f"{trade.exit_price:.8f}",
        f"{trade.entry_size:.8f}",
        f"{trade.exit_size:.8f}",
        f"{trade.realized_pnl:.8f}",
    ]
    return "|".join(parts)


def _capture_heat_pct(trade) -> tuple[float | None, float | None]:
    mfe = trade.mfe
    if mfe is None or mfe <= 0:
        return None, None
    capture_pct = (trade.realized_pnl / mfe) * 100.0
    heat_pct = abs((trade.mae / mfe) * 100.0) if trade.mae is not None else None
    return capture_pct, heat_pct


def _base_session_label(timestamp: datetime) -> str:
    minute = _utc_minutes(timestamp)
    for label, window in _BASE_SESSION_WINDOWS.items():
        start, end = window
        if start <= minute < end:
            return label
    return "asia"


def _timestamp_in_window(timestamp: datetime, window: tuple[int, int]) -> bool:
    minute = _utc_minutes(timestamp)
    start, end = window
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


@lru_cache
def _exposure_windows() -> dict[str, tuple[int, int]]:
    windows = dict(_BASE_SESSION_WINDOWS)
    aux = load_app_config().sessions.auxiliary_windows
    for name, window in aux.items():
        if name in windows:
            continue
        windows[name] = window
    return windows


def _session_exposures(entry_time: datetime, exit_time: datetime) -> dict[str, float]:
    start = _ensure_utc(entry_time)
    end = _ensure_utc(exit_time)
    windows = _exposure_windows()
    exposures = {name: 0.0 for name in windows}
    if end <= start:
        return exposures

    start_day = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_day = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    days = (end_day - start_day).days

    for offset in range(days + 1):
        day_anchor = start_day + timedelta(days=offset)
        for name, window in windows.items():
            for win_start, win_end in _window_intervals(day_anchor, window):
                overlap_start = max(start, win_start)
                overlap_end = min(end, win_end)
                if overlap_end > overlap_start:
                    exposures[name] += (overlap_end - overlap_start).total_seconds()
    return exposures


def _window_intervals(day_anchor: datetime, window: tuple[int, int]) -> list[tuple[datetime, datetime]]:
    start_min, end_min = window
    start_time = day_anchor + timedelta(minutes=start_min)
    if start_min < end_min:
        end_time = day_anchor + timedelta(minutes=end_min)
        return [(start_time, end_time)]
    end_day = day_anchor + timedelta(days=1)
    end_time = day_anchor + timedelta(minutes=end_min)
    return [(start_time, end_day), (day_anchor, end_time)]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_minutes(value: datetime) -> int:
    utc = _ensure_utc(value)
    return utc.hour * 60 + utc.minute


def _apply_excursions_cache(trades: list, excursions_map: dict[str, dict[str, Any]]) -> None:
    for trade in trades:
        excursions = excursions_map.get(_excursions_key(trade))
        if excursions is None:
            excursions = excursions_map.get(_excursions_key(trade, use_local=True))
        if excursions is None:
            excursions = excursions_map.get(_excursions_key_legacy(trade))
        if excursions is None:
            excursions = excursions_map.get(_excursions_key_legacy(trade, use_local=True))
        if not excursions:
            continue
        trade.mae = excursions.get("mae")
        trade.mfe = excursions.get("mfe")
        trade.etd = excursions.get("etd")


def _excursions_key_legacy(trade, use_local: bool = False) -> str:
    parts = [
        trade.symbol,
        trade.side,
        (trade.entry_time.astimezone() if use_local else trade.entry_time).isoformat(),
        (trade.exit_time.astimezone() if use_local else trade.exit_time).isoformat(),
        f"{trade.entry_price:.8f}",
        f"{trade.exit_price:.8f}",
        f"{trade.entry_size:.8f}",
        f"{trade.exit_size:.8f}",
        f"{trade.realized_pnl:.8f}",
    ]
    return "|".join(parts)


def _liquidation_payload(event) -> dict[str, Any]:
    return {
        "liquidation_id": event.liquidation_id,
        "symbol": event.symbol,
        "side": event.side,
        "size": event.size,
        "entry_price": event.entry_price,
        "exit_price": event.exit_price,
        "total_pnl": event.total_pnl,
        "fee": event.fee,
        "liquidate_fee": event.liquidate_fee,
        "created_at": event.created_at,
        "exit_type": event.exit_type,
    }


def _build_liquidation_events(
    trades: list,
    events: list,
    matches: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for trade in trades:
        match = matches.get(trade.trade_id)
        if match is None:
            continue
        payload = dict(match)
        if payload.get("total_pnl") is None:
            payload["total_pnl"] = trade.realized_pnl
        output.append(payload)

    if not output:
        output = [_liquidation_payload(event) for event in events]

    output.sort(key=lambda item: item["created_at"], reverse=True)
    return output


def _match_liquidations(trades: list, events: list, window_seconds: int = 21600) -> dict[str, dict[str, Any]]:
    remaining = list(events)
    matches: dict[str, dict[str, Any]] = {}

    def find_match(
        trade,
        require_size: bool,
        require_side: bool,
    ) -> tuple[int | None, float | None]:
        best_idx = None
        best_delta = None
        for idx, event in enumerate(remaining):
            if trade.symbol != event.symbol:
                continue
            if require_side and trade.side != event.side:
                continue
            if require_size and abs(trade.exit_size - event.size) > 1e-9:
                continue
            delta = abs((trade.exit_time - event.created_at).total_seconds())
            if delta > window_seconds:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_idx = idx
        return best_idx, best_delta

    for trade in sorted(trades, key=lambda item: item.exit_time):
        best_idx, _ = find_match(trade, require_size=True, require_side=True)
        if best_idx is None:
            best_idx, _ = find_match(trade, require_size=False, require_side=True)
        if best_idx is None:
            best_idx, _ = find_match(trade, require_size=False, require_side=False)
        if best_idx is None:
            continue
        event = remaining.pop(best_idx)
        matches[trade.trade_id] = _liquidation_payload(event)

    return matches


def _summary_payload(metrics) -> dict[str, Any]:
    return {
        "total_trades": metrics.total_trades,
        "wins": metrics.wins,
        "losses": metrics.losses,
        "breakevens": metrics.breakevens,
        "win_rate": metrics.win_rate,
        "profit_factor": metrics.profit_factor,
        "expectancy": metrics.expectancy,
        "avg_win": metrics.avg_win,
        "avg_loss": metrics.avg_loss,
        "payoff_ratio": metrics.payoff_ratio,
        "largest_win": metrics.largest_win,
        "largest_loss": metrics.largest_loss,
        "max_consecutive_wins": metrics.max_consecutive_wins,
        "max_consecutive_losses": metrics.max_consecutive_losses,
        "total_gross_pnl": metrics.total_gross_pnl,
        "total_net_pnl": metrics.total_net_pnl,
        "total_fees": metrics.total_fees,
        "total_funding": metrics.total_funding,
        "avg_duration_seconds": metrics.avg_duration_seconds,
        "max_drawdown": metrics.max_drawdown,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "avg_r": metrics.avg_r,
        "max_r": metrics.max_r,
        "min_r": metrics.min_r,
        "pct_r_below_minus_one": metrics.pct_r_below_minus_one,
        "roi_pct": metrics.roi_pct,
        "initial_equity": metrics.initial_equity,
        "net_return": metrics.net_return,
        "avg_trades_per_day": metrics.avg_trades_per_day,
        "max_trades_in_day": metrics.max_trades_in_day,
        "avg_pnl_after_loss": metrics.avg_pnl_after_loss,
        "mean_mae": metrics.mean_mae,
        "median_mae": metrics.median_mae,
        "mean_mfe": metrics.mean_mfe,
        "median_mfe": metrics.median_mfe,
        "mean_etd": metrics.mean_etd,
        "median_etd": metrics.median_etd,
    }


def _empty_summary() -> dict[str, Any]:
    return {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "breakevens": 0,
        "win_rate": None,
        "profit_factor": None,
        "expectancy": None,
        "avg_win": None,
        "avg_loss": None,
        "payoff_ratio": None,
        "largest_win": None,
        "largest_loss": None,
        "max_consecutive_wins": 0,
        "max_consecutive_losses": 0,
        "total_gross_pnl": 0.0,
        "total_net_pnl": 0.0,
        "total_fees": 0.0,
        "total_funding": 0.0,
        "avg_duration_seconds": None,
        "max_drawdown": None,
        "max_drawdown_pct": None,
        "avg_r": None,
        "max_r": None,
        "min_r": None,
        "pct_r_below_minus_one": None,
        "roi_pct": None,
        "initial_equity": None,
        "net_return": None,
        "avg_trades_per_day": None,
        "max_trades_in_day": 0,
        "avg_pnl_after_loss": None,
        "mean_mae": None,
        "median_mae": None,
        "mean_mfe": None,
        "median_mfe": None,
        "mean_etd": None,
        "median_etd": None,
    }


def _load_account_snapshot_view(
    conn,
    *,
    venue: str,
    default_context,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    active_keys = _active_account_keys_from_config()
    rows = conn.execute(
        "SELECT source, account_id, total_equity, available_balance, margin_balance, timestamp, raw_json "
        "FROM account_snapshots ORDER BY timestamp DESC"
    ).fetchall()

    selected: dict[tuple[str, str | None], dict[str, Any]] = {}
    for row in rows:
        source = str(row["source"] or "").strip().lower()
        if venue != "all" and source != venue:
            continue
        account_id = row["account_id"]
        scoped = account_key(source, account_id)
        if active_keys is not None and scoped not in active_keys:
            continue
        key = (source, account_id)
        if key in selected:
            continue
        raw_payload: Any = {}
        raw_json = row["raw_json"]
        if isinstance(raw_json, str):
            text = raw_json.strip()
            if text:
                try:
                    raw_payload = json.loads(text)
                except json.JSONDecodeError:
                    raw_payload = raw_json
        selected[key] = {
            "source": source,
            "account_id": account_id,
            "total_equity": row["total_equity"],
            "available_balance": row["available_balance"],
            "margin_balance": row["margin_balance"],
            "timestamp": row["timestamp"],
            "raw": raw_payload,
        }

    if not selected and venue in _SUPPORTED_VENUES:
        fallback = sqlite_reader.load_account_snapshot(
            conn, source=venue, account_id=default_context.account_id
        )
        if fallback is not None:
            selected[(venue, default_context.account_id)] = {
                "source": venue,
                "account_id": default_context.account_id,
                **fallback,
            }

    if not selected:
        return None, []

    open_positions: list[dict[str, Any]] = []
    total_equity = 0.0
    available_balance = 0.0
    margin_balance = 0.0
    has_total_equity = False
    has_available_balance = False
    has_margin_balance = False
    latest_timestamp: str | None = None
    for snapshot in selected.values():
        wrapped = _with_snapshot_positions(
            {
                "total_equity": snapshot.get("total_equity"),
                "available_balance": snapshot.get("available_balance"),
                "margin_balance": snapshot.get("margin_balance"),
                "timestamp": snapshot.get("timestamp"),
                "raw": snapshot.get("raw"),
            },
            source=str(snapshot.get("source") or ""),
            account_id=snapshot.get("account_id"),
        )
        if not wrapped:
            continue
        open_positions.extend(wrapped.get("open_positions") or [])
        equity_value = wrapped.get("total_equity")
        if equity_value is not None:
            total_equity += float(equity_value)
            has_total_equity = True
        available_value = wrapped.get("available_balance")
        if available_value is not None:
            available_balance += float(available_value)
            has_available_balance = True
        margin_value = wrapped.get("margin_balance")
        if margin_value is not None:
            margin_balance += float(margin_value)
            has_margin_balance = True
        ts = wrapped.get("timestamp")
        if isinstance(ts, str) and (latest_timestamp is None or ts > latest_timestamp):
            latest_timestamp = ts

    open_positions.sort(key=lambda item: (str(item.get("symbol") or ""), str(item.get("side") or "")))
    aggregate = {
        "total_equity": total_equity if has_total_equity else None,
        "available_balance": available_balance if has_available_balance else None,
        "margin_balance": margin_balance if has_margin_balance else None,
        "timestamp": latest_timestamp,
    }
    return aggregate, open_positions


def _load_account_snapshot(context) -> dict[str, Any] | None:
    db_path = _resolve_db_path()
    if db_path is not None and db_path.exists():
        conn = sqlite_reader.connect(db_path)
        try:
            snapshot = sqlite_reader.load_account_snapshot(
                conn, source=context.source, account_id=context.account_id
            )
            return _with_snapshot_positions(snapshot, source=context.source, account_id=context.account_id)
        finally:
            conn.close()

    env_path = os.environ.get("TRADE_JOURNAL_ACCOUNT", "").strip()
    path = Path(env_path) if env_path else resolve_data_path(None, context, "account.json")
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    snapshot = {
        "total_equity": _first_float(data, "totalEquity", "totalAccountValue", "equity"),
        "available_balance": _first_float(data, "availableBalance", "availableBalanceValue", "available"),
        "margin_balance": _first_float(data, "marginBalance", "marginBalanceValue", "balance"),
        "timestamp": _first_timestamp(data, "updatedTime", "updateTime", "timestamp"),
        "raw": data,
    }
    if snapshot["total_equity"] is None and snapshot["available_balance"] is None:
        return None
    return _with_snapshot_positions(snapshot, source=context.source, account_id=context.account_id)


def _with_snapshot_positions(
    snapshot: dict[str, Any] | None, *, source: str, account_id: str | None
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    merged = dict(snapshot)
    merged["open_positions"] = _open_positions_from_snapshot(snapshot, source=source, account_id=account_id)
    return merged


def _open_positions_from_snapshot(
    snapshot: dict[str, Any] | None, *, source: str, account_id: str | None
) -> list[dict[str, Any]]:
    if not snapshot:
        return []
    raw = snapshot.get("raw")
    if not isinstance(raw, dict):
        return []
    account_scoped = account_key(source, account_id)
    if source == "hyperliquid":
        return _hyperliquid_open_positions(raw, account_scoped)
    if source == "apex":
        return _apex_open_positions(raw, account_scoped)
    return []


def _hyperliquid_open_positions(raw: dict[str, Any], account_scoped: str) -> list[dict[str, Any]]:
    positions_raw = raw.get("assetPositions")
    if not isinstance(positions_raw, list):
        return []
    positions: list[dict[str, Any]] = []
    for item in positions_raw:
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        if not isinstance(position, dict):
            continue
        coin = position.get("coin")
        if not coin:
            continue
        size = _first_float(position, "szi", "size")
        if size is None or abs(size) <= _OPEN_POSITION_SIZE_EPSILON:
            continue
        side = "LONG" if size > 0 else "SHORT"
        symbol = f"{coin}-USDC"
        positions.append(
            {
                "account_key": account_scoped,
                "symbol": symbol,
                "side": side,
                "size": abs(size),
                "entry_price": _first_float(position, "entryPx", "entryPrice"),
                "position_value": _first_float(position, "positionValue"),
                "unrealized_pnl": _first_float(position, "unrealizedPnl"),
                "leverage": _first_float(position, "leverage", "leverageValue"),
                "margin_used": _first_float(position, "marginUsed"),
                "liquidation_price": _first_float(position, "liquidationPx", "liquidationPrice"),
                "return_on_equity": _first_float(position, "returnOnEquity"),
            }
        )
    positions.sort(key=lambda item: (item["symbol"], item["side"]))
    return positions


def _apex_open_positions(raw: dict[str, Any], account_scoped: str) -> list[dict[str, Any]]:
    positions_raw = raw.get("positions")
    if not isinstance(positions_raw, list):
        return []
    positions: list[dict[str, Any]] = []
    for item in positions_raw:
        if not isinstance(item, dict):
            continue
        size = _first_float(item, "size", "positionSize")
        if size is None or abs(size) <= _OPEN_POSITION_SIZE_EPSILON:
            continue
        symbol = item.get("symbol")
        side_text = str(item.get("side") or "").strip().upper()
        side = "SHORT" if side_text == "SHORT" else "LONG"
        entry_price = _first_float(item, "entryPrice", "avgEntryPrice")
        unrealized_pnl = _first_float(item, "unrealizedPnl", "unrealizePnl")
        position_value = _first_float(item, "positionValue")
        if unrealized_pnl is None:
            estimated_pnl, estimated_value = _estimate_open_position_pnl(
                symbol=str(symbol) if symbol else "",
                side=side,
                size=abs(size),
                entry_price=entry_price,
            )
            if estimated_pnl is not None:
                unrealized_pnl = estimated_pnl
            if position_value is None and estimated_value is not None:
                position_value = estimated_value
        positions.append(
            {
                "account_key": account_scoped,
                "symbol": str(symbol) if symbol else "n/a",
                "side": side,
                "size": abs(size),
                "entry_price": entry_price,
                "position_value": position_value,
                "unrealized_pnl": unrealized_pnl,
                "leverage": _first_float(item, "leverage"),
                "margin_used": _first_float(item, "marginUsed"),
                "liquidation_price": _first_float(item, "liquidationPrice", "liqPrice"),
                "return_on_equity": _first_float(item, "returnOnEquity"),
            }
        )
    positions.sort(key=lambda item: (item["symbol"], item["side"]))
    return positions


def _resolve_trade_series_path(context) -> Path | None:
    app_config = load_app_config()
    candidate = app_config.paths.trade_series
    if candidate and candidate.exists():
        return candidate
    candidate = resolve_data_path(None, context, "trade_series.json")
    return candidate if candidate.exists() else None


def _load_trade_series(path: Path) -> dict[str, list[dict[str, float | None]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _price_interval() -> str:
    app_config = load_app_config()
    return str(app_config.pricing.interval)


def _canonical_price_timeframe() -> str:
    return "1m"


def _load_trade_bars_db(conn, trade) -> list[PriceBar]:
    start = trade.entry_time - timedelta(minutes=1)
    end = trade.exit_time + timedelta(minutes=1)
    rows = sqlite_reader.load_price_bars(
        conn,
        source=trade.source,
        symbol=trade.symbol,
        timeframe=_canonical_price_timeframe(),
        start=start,
        end=end,
    )
    output: list[PriceBar] = []
    for row in rows:
        ts = row.get("timestamp")
        if not isinstance(ts, str):
            continue
        start_time = datetime.fromisoformat(ts)
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        output.append(
            PriceBar(
                start_time=start_time,
                end_time=start_time + timedelta(minutes=1),
                open=float(row.get("open") or 0.0),
                high=float(row.get("high") or 0.0),
                low=float(row.get("low") or 0.0),
                close=float(row.get("close") or 0.0),
            )
        )
    output.sort(key=lambda item: item.start_time)
    return output


@lru_cache(maxsize=1)
def _price_client_cached() -> ApexPriceClient:
    app_config = load_app_config()
    return ApexPriceClient(PriceSeriesConfig.from_settings(app_config.pricing))


def _latest_symbol_price(symbol: str) -> float | None:
    if not symbol:
        return None
    now = datetime.now(timezone.utc)
    cached = _OPEN_POSITION_PRICE_CACHE.get(symbol)
    if cached is not None:
        ts, value = cached
        if (now - ts).total_seconds() <= _OPEN_POSITION_PRICE_CACHE_TTL_SECONDS:
            return value
    # Use a short trailing window to avoid partial-current-bar coverage failures.
    window_end = now - timedelta(minutes=1)
    window_start = window_end - timedelta(minutes=15)
    try:
        bars = _price_client_cached().fetch_bars(symbol, window_start, window_end)
    except Exception:
        return None
    if not bars:
        return None
    value = float(bars[-1].close)
    _OPEN_POSITION_PRICE_CACHE[symbol] = (now, value)
    return value


def _estimate_open_position_pnl(
    *,
    symbol: str,
    side: str,
    size: float,
    entry_price: float | None,
) -> tuple[float | None, float | None]:
    if entry_price is None:
        return None, None
    mark_price = _latest_symbol_price(symbol)
    if mark_price is None:
        return None, None
    direction = -1.0 if side == "SHORT" else 1.0
    unrealized = (mark_price - entry_price) * size * direction
    position_value = mark_price * size
    return unrealized, position_value


def _first_float(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            try:
                return float(data[key])
            except (TypeError, ValueError):
                return None
    return None


def _parse_analytics_filters(
    request: Request,
    symbols: list[str],
    accounts: list[dict[str, Any]],
    strategies: list[str],
    exposure_windows: list[str],
) -> dict[str, Any]:
    params = request.query_params
    tab = params.get("tab", "overview").strip().lower()
    if tab not in {"overview", "diagnostics", "edge", "time", "trades"}:
        tab = "overview"

    from_date = _parse_date(params.get("from"))
    to_date = _parse_date(params.get("to"))

    selected_symbols: list[str] = []
    if hasattr(params, "getlist"):
        selected_symbols.extend([sym for sym in params.getlist("symbol") if sym])
    symbol_param = params.get("symbol", "").strip()
    selected_symbols.extend([sym for sym in symbol_param.split(",") if sym])
    available_symbols = symbols
    valid_symbols = [sym for sym in selected_symbols if sym in available_symbols]
    if valid_symbols:
        valid_symbols = list(dict.fromkeys(valid_symbols))

    side = params.get("side", "all").strip().lower()
    if side not in {"all", "long", "short"}:
        side = "all"

    outcome = params.get("outcome", "all").strip().lower()
    if outcome not in {"all", "win", "loss", "breakeven"}:
        outcome = "all"

    account_ids = [str(account.get("account_key")) for account in accounts if account.get("account_key")]
    account = params.get("account", "all").strip()
    if not account or account.lower() == "all":
        account = "all"
    elif account not in account_ids:
        account = "all"

    entry_session = params.get("entry_session", "all").strip().lower()
    if entry_session not in _BASE_SESSION_LABELS:
        entry_session = "all"

    exit_session_raw = params.get("exit_session", "all")
    exit_session: str | list[str]
    if not exit_session_raw:
        exit_session = "all"
    else:
        cleaned = [item.strip().lower() for item in exit_session_raw.split(",") if item.strip()]
        valid_sessions = [item for item in cleaned if item in _BASE_SESSION_LABELS]
        exit_session = valid_sessions or "all"

    session_alias = params.get("session")
    if exit_session == "all" and session_alias:
        alias_value = session_alias.strip().lower()
        if alias_value in _BASE_SESSION_LABELS:
            exit_session = [alias_value]

    exit_window_raw = params.get("exit_window", "all")
    exit_window: str | list[str]
    if not exit_window_raw:
        exit_window = "all"
    else:
        cleaned = [item.strip().lower() for item in exit_window_raw.split(",") if item.strip()]
        valid_windows = [item for item in cleaned if item in exposure_windows]
        exit_window = valid_windows or "all"

    strategy = params.get("strategy", "all").strip()
    if not strategy or strategy.lower() == "all":
        strategy = "all"
    elif strategy not in strategies:
        strategy = "all"

    norm_raw = (
        params.get("normalization")
        or params.get("normalize")
        or params.get("norm")
        or "usd"
    )
    normalization = _parse_normalization(norm_raw)

    query_parts = []
    if from_date:
        query_parts.append(f"from={from_date.isoformat()}")
    if to_date:
        query_parts.append(f"to={to_date.isoformat()}")
    if valid_symbols:
        query_parts.append(f"symbol={','.join(valid_symbols)}")
    if side != "all":
        query_parts.append(f"side={side}")
    if outcome != "all":
        query_parts.append(f"outcome={outcome}")
    if account != "all":
        query_parts.append(f"account={account}")
    if entry_session != "all":
        query_parts.append(f"entry_session={entry_session}")
    if exit_session != "all":
        if isinstance(exit_session, list):
            query_parts.append(f"exit_session={','.join(exit_session)}")
        else:
            query_parts.append(f"exit_session={exit_session}")
    if exit_window != "all":
        if isinstance(exit_window, list):
            query_parts.append(f"exit_window={','.join(exit_window)}")
        else:
            query_parts.append(f"exit_window={exit_window}")
    if strategy != "all":
        query_parts.append(f"strategy={strategy}")
    if normalization != "usd":
        query_parts.append(f"normalization={normalization}")

    exposure_filters: dict[str, float] = {}
    exposure_set = set(exposure_windows)
    for key, value in params.items():
        if not key.startswith("exp_") or not key.endswith("_seconds"):
            continue
        window = key[4:-8]
        if window not in exposure_set:
            continue
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            continue
        exposure_filters[window] = threshold
        query_parts.append(f"{key}={threshold}")
    query_base = "&".join(query_parts)

    return {
        "tab": tab,
        "from": from_date,
        "to": to_date,
        "symbols": valid_symbols,
        "side": side,
        "outcome": outcome,
        "account": account,
        "entry_session": entry_session,
        "exit_session": exit_session,
        "exit_window": exit_window,
        "strategy": strategy,
        "normalization": normalization,
        "exposure_filters": exposure_filters,
        "query_base": query_base,
    }


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _parse_normalization(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned in {"usd", "$"}:
        return "usd"
    if cleaned in {"%", "pct", "percent", "account"}:
        return "percent"
    if cleaned in {"r", "r-multiple", "r_multiple", "rmultiple"}:
        return "r"
    return "usd"


def _filter_trade_items(
    items: list[dict[str, Any]],
    filters: dict[str, Any],
    *,
    override_from: date | None = None,
    override_to: date | None = None,
    ignore_date: bool = False,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    from_date = None
    to_date = None
    if not ignore_date:
        from_date = override_from if override_from is not None else filters["from"]
        to_date = override_to if override_to is not None else filters["to"]
    for item in items:
        exit_day = item["exit_time"].date()
        if from_date and exit_day < from_date:
            continue
        if to_date and exit_day > to_date:
            continue
        if filters["symbols"] and item["symbol"] not in filters["symbols"]:
            continue
        if filters["side"] != "all" and item["side"].lower() != filters["side"]:
            continue
        if filters["outcome"] != "all" and item["outcome"] != filters["outcome"]:
            continue
        if filters["account"] != "all" and item.get("account_key") != filters["account"]:
            continue
        if filters["entry_session"] != "all" and item.get("entry_session") != filters["entry_session"]:
            continue
        exit_session_filter = filters["exit_session"]
        exit_window_filter = filters.get("exit_window", "all")
        if exit_session_filter != "all" or exit_window_filter != "all":
            match_session = False
            match_window = False
            if exit_session_filter != "all":
                if isinstance(exit_session_filter, list):
                    match_session = item.get("exit_session") in exit_session_filter
                else:
                    match_session = item.get("exit_session") == exit_session_filter
            if exit_window_filter != "all":
                windows = _exposure_windows()
                if isinstance(exit_window_filter, list):
                    for window_name in exit_window_filter:
                        window = windows.get(window_name)
                        if window and _timestamp_in_window(item["exit_time"], window):
                            match_window = True
                            break
                else:
                    window = windows.get(exit_window_filter)
                    match_window = bool(window and _timestamp_in_window(item["exit_time"], window))
            if not (match_session or match_window):
                continue
        if filters["strategy"] != "all":
            strategies = item.get("strategy_tags") or []
            if filters["strategy"] not in strategies:
                continue
        exposure_filters = filters.get("exposure_filters") or {}
        exposure_ok = True
        for window, threshold in exposure_filters.items():
            value = float(item.get(f"exp_{window}_seconds", 0.0) or 0.0)
            if value < threshold:
                exposure_ok = False
                break
        if not exposure_ok:
            continue
        filtered.append(item)
    filtered.sort(key=lambda item: item["exit_time"], reverse=True)
    return filtered


def _resolve_normalization(trades: list, requested: str) -> dict[str, Any]:
    availability = {
        "usd": True,
        "percent": all(
            trade.equity_at_entry is not None and trade.equity_at_entry > 0 for trade in trades
        )
        if trades
        else False,
        "r": any(
            getattr(trade, "initial_risk", None) not in (None, 0)
            for trade in trades
        )
        if trades
        else False,
    }
    mode = requested if requested in _NORMALIZATION_MODES else "usd"
    forced = False
    if mode == "percent" and not availability["percent"]:
        mode = "usd"
        forced = True

    normalized_trades, excluded_trade_ids = _normalize_trades(trades, mode)

    return {
        "mode": mode,
        "requested": requested,
        "available": availability,
        "forced": forced,
        "excluded_trade_ids": excluded_trade_ids,
        "trades": normalized_trades,
    }


def _normalize_trades(trades: list, mode: str) -> tuple[list[Trade], set[str]]:
    if mode == "usd":
        return list(trades), set()

    outcomes = {trade.trade_id: compute_trade_metrics(trade).outcome for trade in trades}
    normalized: list[Trade] = []
    excluded: set[str] = set()
    for trade in trades:
        factor = _normalization_factor(trade, mode)
        if factor is None:
            excluded.add(trade.trade_id)
            continue
        outcome = outcomes.get(trade.trade_id)
        normalized.append(_clone_trade_scaled(trade, factor, outcome))
    return normalized, excluded


def _normalization_factor(trade, mode: str) -> float | None:
    if mode == "percent":
        equity = trade.equity_at_entry
        if equity is None or equity == 0:
            return None
        return 1.0 / equity
    if mode == "r":
        risk = getattr(trade, "initial_risk", None)
        if risk is None or risk == 0:
            return None
        return 1.0 / risk
    return 1.0


def _clone_trade_scaled(trade: Trade, factor: float, outcome: str | None) -> Trade:
    scaled_realized = trade.realized_pnl * factor
    scaled_fees = trade.fees * factor
    scaled_funding = trade.funding_fees * factor
    scaled_mae = trade.mae * factor if trade.mae is not None else None
    scaled_mfe = trade.mfe * factor if trade.mfe is not None else None
    scaled_etd = trade.etd * factor if trade.etd is not None else None

    clone = Trade(
        trade_id=trade.trade_id,
        source=trade.source,
        account_id=trade.account_id,
        symbol=trade.symbol,
        side=trade.side,
        entry_time=trade.entry_time,
        exit_time=trade.exit_time,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        entry_size=trade.entry_size,
        exit_size=trade.exit_size,
        max_size=trade.max_size,
        realized_pnl=scaled_realized,
        fees=scaled_fees,
        funding_fees=scaled_funding,
        equity_at_entry=trade.equity_at_entry,
        fills=trade.fills,
        mae=scaled_mae,
        mfe=scaled_mfe,
        etd=scaled_etd,
    )
    setattr(clone, "r_multiple", getattr(trade, "r_multiple", None))
    setattr(clone, "initial_risk", getattr(trade, "initial_risk", None))
    if outcome is not None:
        setattr(clone, "outcome_override", outcome)
    return clone


def _normalized_trade_items(
    items: list[dict[str, Any]],
    normalization: dict[str, Any],
) -> list[dict[str, Any]]:
    mode = normalization["mode"]
    if mode == "usd":
        return items
    trade_map = {trade.trade_id: trade for trade in normalization["trades"]}
    normalized_items: list[dict[str, Any]] = []
    for item in items:
        trade = trade_map.get(item["trade_id"])
        if trade is None:
            if mode == "r":
                continue
            normalized_items.append(item)
            continue
        payload = dict(item)
        payload["pnl_value"] = trade.realized_pnl
        payload["mae_value"] = trade.mae
        payload["mfe_value"] = trade.mfe
        payload["etd_value"] = trade.etd
        normalized_items.append(payload)
    return normalized_items


def _diagnostics_phase2(trades: list[Trade], items: list[dict[str, Any]]) -> dict[str, Any]:
    items_by_id = {item["trade_id"]: item for item in items}
    capture_values: list[float] = []
    heat_values: list[float] = []
    early_exit_count = 0
    early_exit_den = 0
    exit_eff_num = 0.0
    exit_eff_den = 0.0
    stop_hit_count = 0
    stop_hit_den = 0
    target_hit_count = 0
    target_hit_den = 0

    for trade in trades:
        item = items_by_id.get(trade.trade_id, {})
        mfe = trade.mfe
        if mfe is not None and mfe > 0:
            if trade.realized_pnl > 0:
                capture = (trade.realized_pnl / mfe) * 100.0
                capture_values.append(capture)
                early_exit_den += 1
                if capture < _EARLY_EXIT_CAPTURE_PCT:
                    early_exit_count += 1
                exit_eff_num += trade.realized_pnl
                exit_eff_den += mfe
            if trade.mae is not None:
                heat_values.append(abs(trade.mae) / mfe * 100.0)

        initial_risk = getattr(trade, "initial_risk", None)
        if initial_risk and trade.mae is not None:
            stop_hit_den += 1
            if trade.mae <= -initial_risk:
                stop_hit_count += 1

        target_hit, target_defined = _target_hit(trade, item)
        if target_defined:
            target_hit_den += 1
            if target_hit:
                target_hit_count += 1

    return {
        "mfe_capture_pct": _mean_value(capture_values),
        "mae_tolerance_pct": _mean_value(heat_values),
        "early_exit_rate": (early_exit_count / early_exit_den) if early_exit_den else None,
        "exit_efficiency_pct": (exit_eff_num / exit_eff_den * 100.0) if exit_eff_den else None,
        "stop_hit_rate": (stop_hit_count / stop_hit_den) if stop_hit_den else None,
        "target_hit_rate": (target_hit_count / target_hit_den) if target_hit_den else None,
        "counts": {
            "early_exit_den": early_exit_den,
            "stop_hit_den": stop_hit_den,
            "target_hit_den": target_hit_den,
        },
    }


def _target_hit(trade: Trade, item: dict[str, Any]) -> tuple[bool, bool]:
    tags_by_type = item.get("tags_by_type") or {}
    for tag_type in _TARGET_TAG_TYPES:
        if tags_by_type.get(tag_type):
            return True, True
    target_pnl = getattr(trade, "target_pnl", None)
    if target_pnl is None:
        return False, False
    if trade.mfe is None:
        return False, True
    return trade.mfe >= target_pnl, True


def _strategy_attribution(
    items: list[dict[str, Any]],
    normalized_trades: list[Trade],
) -> list[dict[str, Any]]:
    trade_map = {trade.trade_id: trade for trade in normalized_trades}
    buckets: dict[str, list[Trade]] = defaultdict(list)
    for item in items:
        strategies = item.get("strategy_tags") or []
        for strategy in strategies:
            trade = trade_map.get(item["trade_id"])
            if trade is None:
                continue
            buckets[strategy].append(trade)

    rows = []
    for strategy, trades in sorted(buckets.items(), key=lambda pair: pair[0]):
        metrics = compute_aggregate_metrics(trades) if trades else None
        rows.append(
            {
                "strategy": strategy,
                "trades": len(trades),
                "win_rate": metrics.win_rate if metrics else None,
                "profit_factor": metrics.profit_factor if metrics else None,
                "expectancy": metrics.expectancy if metrics else None,
                "net_pnl": metrics.total_net_pnl if metrics else 0.0,
            }
        )
    rows.sort(key=lambda row: row["net_pnl"], reverse=True)
    return rows


def _size_bucket_attribution(
    items: list[dict[str, Any]],
    normalized_trades: list[Trade],
) -> list[dict[str, Any]]:
    edges = _size_bucket_edges()
    trade_map = {trade.trade_id: trade for trade in normalized_trades}
    buckets: dict[str, list[Trade]] = defaultdict(list)
    for item in items:
        notional = item.get("entry_price", 0.0) * item.get("entry_size", 0.0)
        label = _size_bucket_label(notional, edges)
        trade = trade_map.get(item["trade_id"])
        if trade is None:
            continue
        buckets[label].append(trade)

    rows = []
    for label, trades in buckets.items():
        metrics = compute_aggregate_metrics(trades) if trades else None
        rows.append(
            {
                "bucket": label,
                "trades": len(trades),
                "win_rate": metrics.win_rate if metrics else None,
                "profit_factor": metrics.profit_factor if metrics else None,
                "expectancy": metrics.expectancy if metrics else None,
                "net_pnl": metrics.total_net_pnl if metrics else 0.0,
            }
        )
    return rows


def _size_bucket_edges() -> list[float]:
    app_config = load_app_config()
    edges = list(app_config.analytics.size_buckets)
    edges = [edge for edge in edges if edge > 0]
    edges.sort()
    return edges


def _size_bucket_label(value: float, edges: list[float]) -> str:
    for edge in edges:
        if value < edge:
            return f"<{_format_bucket(edge)}"
    if edges:
        return f">={_format_bucket(edges[-1])}"
    return "all"


def _format_bucket(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:.0f}"


def _regime_attribution(
    items: list[dict[str, Any]],
    normalized_trades: list[Trade],
) -> list[dict[str, Any]]:
    trade_map = {trade.trade_id: trade for trade in normalized_trades}
    buckets: dict[str, list[Trade]] = defaultdict(list)
    for item in items:
        label = item.get("entry_regime")
        if not label:
            continue
        trade = trade_map.get(item["trade_id"])
        if trade is None:
            continue
        buckets[label].append(trade)

    rows = []
    for label, trades in sorted(buckets.items(), key=lambda pair: pair[0]):
        metrics = compute_aggregate_metrics(trades) if trades else None
        rows.append(
            {
                "regime": label,
                "trades": len(trades),
                "win_rate": metrics.win_rate if metrics else None,
                "profit_factor": metrics.profit_factor if metrics else None,
                "expectancy": metrics.expectancy if metrics else None,
                "net_pnl": metrics.total_net_pnl if metrics else 0.0,
            }
        )
    rows.sort(key=lambda row: row["net_pnl"], reverse=True)
    return rows


def _duration_charts(items: list[dict[str, Any]]) -> dict[str, Any]:
    scatter = []
    histogram = _duration_histogram(items)
    for item in items:
        duration = item.get("duration_seconds")
        pnl = item.get("pnl_value", item.get("realized_pnl"))
        if duration is None or pnl is None:
            continue
        scatter.append({"x": duration, "y": pnl})
    return {"scatter": scatter, "histogram": histogram}


def _duration_histogram(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = [0 for _ in range(len(_DURATION_BINS_SECONDS) + 1)]
    for item in items:
        duration = item.get("duration_seconds")
        if duration is None:
            continue
        idx = _duration_bucket_index(float(duration))
        counts[idx] += 1

    bins = []
    prev = 0.0
    for idx, edge in enumerate(_DURATION_BINS_SECONDS):
        bins.append(
            {
                "label": _duration_label(prev, edge),
                "start": prev,
                "end": edge,
                "count": counts[idx],
            }
        )
        prev = edge
    bins.append(
        {
            "label": f">{_format_duration(prev)}",
            "start": prev,
            "end": None,
            "count": counts[-1],
        }
    )
    return bins


def _duration_bucket_index(value: float) -> int:
    for idx, edge in enumerate(_DURATION_BINS_SECONDS):
        if value < edge:
            return idx
    return len(_DURATION_BINS_SECONDS)


def _duration_label(start: float, end: float) -> str:
    return f"{_format_duration(start)}–{_format_duration(end)}"


def _apply_regimes(
    trade_items: list[dict[str, Any]],
    trades: list[Trade],
    regimes: list[dict[str, Any]],
) -> None:
    if not regimes:
        return
    regimes_by_key: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for regime in regimes:
        source = regime.get("source") or ""
        key = (str(source), regime.get("account_id"))
        regimes_by_key[key].append(regime)
    for series in regimes_by_key.values():
        series.sort(key=lambda item: item.get("ts_start") or datetime.min.replace(tzinfo=timezone.utc))

    items_by_id = {item["trade_id"]: item for item in trade_items}
    for trade in trades:
        item = items_by_id.get(trade.trade_id)
        if item is None:
            continue
        key = (trade.source, trade.account_id)
        series = regimes_by_key.get(key, [])
        item["entry_regime"] = _regime_for_time(series, trade.entry_time)
        item["exit_regime"] = _regime_for_time(series, trade.exit_time)


def _regime_for_time(series: list[dict[str, Any]], timestamp: datetime) -> str | None:
    ts = _ensure_utc(timestamp)
    candidate = None
    for regime in series:
        start = regime.get("ts_start")
        end = regime.get("ts_end")
        if start is None:
            continue
        start = _ensure_utc(start)
        end = _ensure_utc(end) if end is not None else None
        if ts < start:
            continue
        if end is not None and ts > end:
            continue
        candidate = regime
    if candidate is None:
        return None
    return candidate.get("regime_label")


def _trades_from_items(trades: list[Trade], items: list[dict[str, Any]]) -> list[Trade]:
    ids = {item["trade_id"] for item in items}
    return [trade for trade in trades if trade.trade_id in ids]


def _previous_period_range(filters: dict[str, Any]) -> tuple[date, date] | None:
    start = filters.get("from")
    end = filters.get("to")
    if start is None or end is None:
        return None
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    return prev_start, prev_end


def _normalize_for_comparison(trades: list[Trade], mode: str) -> dict[str, Any]:
    if mode == "percent":
        if any(trade.equity_at_entry is None or trade.equity_at_entry <= 0 for trade in trades):
            return {"trades": [], "available": False, "excluded": set()}
    normalized_trades, excluded = _normalize_trades(trades, mode)
    if mode == "r" and trades and not normalized_trades:
        return {"trades": [], "available": False, "excluded": excluded}
    return {"trades": normalized_trades, "available": True, "excluded": excluded}


def _metrics_payload(
    trades: list[Trade],
    mode: str,
    *,
    initial_equity: float | None,
) -> tuple[Any | None, Any | None]:
    if not trades:
        return None, None
    initial_equity = initial_equity if mode == "usd" else None
    metrics = compute_aggregate_metrics(trades, initial_equity=initial_equity)
    score = compute_performance_score(trades, metrics)
    return metrics, score


def _kpi_payload(metrics, performance_score) -> dict[str, float | None]:
    if metrics is None:
        return {}
    score_value = None
    if performance_score is not None:
        if isinstance(performance_score, dict):
            score_value = performance_score.get("score")
        else:
            score_value = getattr(performance_score, "score", None)
    return {
        "net_pnl": metrics.total_net_pnl,
        "win_rate": metrics.win_rate,
        "expectancy": metrics.expectancy,
        "profit_factor": metrics.profit_factor,
        "avg_win": metrics.avg_win,
        "avg_loss": metrics.avg_loss,
        "max_drawdown": metrics.max_drawdown,
        "performance_score": score_value,
    }


def _kpi_delta(current: dict[str, float | None], baseline: dict[str, float | None]) -> dict[str, float | None]:
    deltas: dict[str, float | None] = {}
    for key, value in current.items():
        other = baseline.get(key)
        if value is None or other is None:
            deltas[key] = None
        else:
            deltas[key] = value - other
    return deltas


def _benchmark_window(filters: dict[str, Any], trades: list[dict[str, Any]]) -> tuple[datetime, datetime] | None:
    if filters.get("from") and filters.get("to"):
        start = datetime.combine(filters["from"], datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(filters["to"], datetime.max.time(), tzinfo=timezone.utc)
        return start, end
    if not trades:
        return None
    ordered = sorted(trades, key=lambda item: item["exit_time"])
    start = ordered[0]["exit_time"].astimezone(timezone.utc)
    end = ordered[-1]["exit_time"].astimezone(timezone.utc)
    return start, end


def _benchmark_candidates() -> tuple[str, list[str], list[str]]:
    source = "hyperliquid"
    symbols = ["BTC-USDC", "BTC-USDT", "BTCUSDT"]
    timeframes = [_canonical_price_timeframe(), "5m"]
    return source, symbols, timeframes


def _benchmark_return(
    db_path: Path | None,
    window: tuple[datetime, datetime] | None,
) -> float | None:
    if db_path is None or window is None or not db_path.exists():
        return None
    start, end = window
    source, symbols, timeframes = _benchmark_candidates()
    conn = sqlite_reader.connect(db_path)
    try:
        for symbol in symbols:
            for timeframe in timeframes:
                aligned_start, aligned_end = _benchmark_aligned_window(start, end, timeframe=timeframe)
                rows = sqlite_reader.load_price_bars(
                    conn,
                    source=source,
                    symbol=symbol,
                    timeframe=timeframe,
                    start=aligned_start,
                    end=aligned_end,
                )
                if len(rows) < 2:
                    continue
                if not _benchmark_rows_fully_covered(rows, aligned_start, aligned_end, timeframe=timeframe):
                    continue
                first_open = rows[0].get("open")
                last_close = rows[-1].get("close")
                if not first_open:
                    continue
                return (last_close - first_open) / first_open
    finally:
        conn.close()
    return None


def _benchmark_aligned_window(
    start: datetime,
    end: datetime,
    *,
    timeframe: str,
) -> tuple[datetime, datetime]:
    step = _timeframe_delta(timeframe)
    return _floor_time(start, step), _floor_time(end, step)


def _benchmark_rows_fully_covered(
    rows: list[dict[str, Any]],
    aligned_start: datetime,
    aligned_end: datetime,
    *,
    timeframe: str,
) -> bool:
    if not rows:
        return False
    step = _timeframe_delta(timeframe)
    max_gap = step * 2
    timestamps: list[datetime] = []
    for row in rows:
        raw_ts = row.get("timestamp")
        if not isinstance(raw_ts, str):
            continue
        ts = datetime.fromisoformat(raw_ts)
        timestamps.append(_ensure_utc(ts))
    if len(timestamps) < 2:
        return False
    timestamps.sort()
    if timestamps[0] > aligned_start:
        return False
    if timestamps[-1] < aligned_end:
        return False
    for prev, cur in zip(timestamps, timestamps[1:]):
        if cur - prev > max_gap:
            return False
    return True


def _timeframe_delta(timeframe: str) -> timedelta:
    text = str(timeframe).strip().lower()
    if text.endswith("m"):
        minutes = int(text[:-1] or "1")
        return timedelta(minutes=max(1, minutes))
    return timedelta(minutes=1)


def _floor_time(value: datetime, step: timedelta) -> datetime:
    ts = int(_ensure_utc(value).timestamp())
    step_seconds = max(1, int(step.total_seconds()))
    floored = ts - (ts % step_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _build_comparisons(
    *,
    trade_items: list[dict[str, Any]],
    trade_objects: list[Trade],
    filters: dict[str, Any],
    normalization: dict[str, Any],
    db_path: Path | None,
    initial_equity: float | None,
) -> dict[str, Any]:
    mode = normalization["mode"]

    current_items = _filter_trade_items(trade_items, filters)
    current_trades = _trades_from_items(trade_objects, current_items)
    current_norm = _normalize_for_comparison(current_trades, mode)
    current_metrics, current_score = _metrics_payload(
        current_norm["trades"], mode, initial_equity=initial_equity
    )
    current_kpis = _kpi_payload(current_metrics, current_score)

    all_items = _filter_trade_items(trade_items, filters, ignore_date=True)
    all_trades = _trades_from_items(trade_objects, all_items)
    all_norm = _normalize_for_comparison(all_trades, mode)
    all_metrics, all_score = (
        _metrics_payload(all_norm["trades"], mode, initial_equity=initial_equity)
        if all_norm["available"]
        else (None, None)
    )
    all_kpis = _kpi_payload(all_metrics, all_score) if all_metrics else {}

    prev_range = _previous_period_range(filters)
    prev_metrics = None
    prev_score = None
    prev_kpis: dict[str, float | None] = {}
    prev_available = False
    if prev_range is not None:
        prev_items = _filter_trade_items(
            trade_items,
            filters,
            override_from=prev_range[0],
            override_to=prev_range[1],
        )
        prev_trades = _trades_from_items(trade_objects, prev_items)
        prev_norm = _normalize_for_comparison(prev_trades, mode)
        prev_available = prev_norm["available"]
        if prev_available:
            prev_metrics, prev_score = _metrics_payload(
                prev_norm["trades"], mode, initial_equity=initial_equity
            )
            prev_kpis = _kpi_payload(prev_metrics, prev_score) if prev_metrics else {}

    benchmark_return = _benchmark_return(db_path, _benchmark_window(filters, current_items))

    return {
        "current": {
            "metrics": current_metrics,
            "score": current_score,
            "kpis": current_kpis,
        },
        "all_trades": {
            "available": all_norm["available"],
            "metrics": all_metrics,
            "score": all_score,
            "kpis": all_kpis,
            "delta": _kpi_delta(current_kpis, all_kpis) if all_kpis else {},
        },
        "previous_period": {
            "available": prev_available,
            "metrics": prev_metrics,
            "score": prev_score,
            "kpis": prev_kpis,
            "delta": _kpi_delta(current_kpis, prev_kpis) if prev_kpis else {},
            "range": prev_range,
        },
        "benchmark": {
            "return": benchmark_return,
        },
    }


def _diagnostics_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    mae_mfe = [
        {
            "x": item.get("mae_value", item["mae"]),
            "y": item.get("mfe_value", item["mfe"]),
            "symbol": item["symbol"],
            "trade_id": item["ui_id"],
            "pnl": item.get("pnl_value", item["realized_pnl"]),
            "mae": item.get("mae_value", item["mae"]),
            "mfe": item.get("mfe_value", item["mfe"]),
            "capture_pct": item.get("capture_pct"),
            "heat_pct": item.get("heat_pct"),
        }
        for item in items
        if item.get("mae_value", item["mae"]) is not None
        and item.get("mfe_value", item["mfe"]) is not None
    ]
    mfe_pnl = [
        {
            "x": item.get("mfe_value", item["mfe"]),
            "y": item.get("pnl_value", item["realized_pnl"]),
            "symbol": item["symbol"],
            "trade_id": item["ui_id"],
            "pnl": item.get("pnl_value", item["realized_pnl"]),
            "mae": item.get("mae_value", item["mae"]),
            "mfe": item.get("mfe_value", item["mfe"]),
            "capture_pct": item.get("capture_pct"),
            "heat_pct": item.get("heat_pct"),
        }
        for item in items
        if item.get("mfe_value", item["mfe"]) is not None
    ]
    table = [
        {
            "symbol": item["symbol"],
            "pnl": item.get("pnl_value", item["realized_pnl"]),
            "mae": item.get("mae_value", item["mae"]),
            "mfe": item.get("mfe_value", item["mfe"]),
            "etd": item.get("etd_value", item["etd"]),
            "ui_id": item["ui_id"],
            "capture_pct": item.get("capture_pct"),
            "heat_pct": item.get("heat_pct"),
        }
        for item in items
    ]
    return {
        "mae_mfe": mae_mfe,
        "mfe_pnl": mfe_pnl,
        "table": table,
    }


def _direction_analysis(trades: list) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for side in ("long", "short"):
        side_trades = [trade for trade in trades if trade.side.lower() == side]
        metrics = compute_aggregate_metrics(side_trades) if side_trades else None
        output[side] = {
            "trades": len(side_trades),
            "win_rate": metrics.win_rate if metrics else None,
            "profit_factor": metrics.profit_factor if metrics else None,
            "expectancy": metrics.expectancy if metrics else None,
            "net_pnl": metrics.total_net_pnl if metrics else 0.0,
        }
    return output


def _first_timestamp(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            value = data[key]
            try:
                num = float(value)
                if num > 1e12:
                    num = num / 1000.0
                return datetime.fromtimestamp(num, tz=timezone.utc).isoformat()
            except (TypeError, ValueError):
                try:
                    parsed = datetime.fromisoformat(str(value))
                except ValueError:
                    return None
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.isoformat()
    return None


def _equity_curve(trades: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda item: item["exit_time"])
    cumulative = 0.0
    points = []
    for item in ordered:
        pnl_value = item.get("pnl_value", item["realized_pnl"])
        cumulative += pnl_value
        points.append(
            {
                "t": item["exit_time"].isoformat(),
                "v": cumulative,
                "symbol": item["symbol"],
                "pnl": pnl_value,
            }
        )
    return {
        "points": points,
    }


def _daily_pnl(trades: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, float] = defaultdict(float)
    for item in trades:
        day = item["exit_time"].date().isoformat()
        buckets[day] += item.get("pnl_value", item["realized_pnl"])
    series = [
        {"day": day, "pnl": pnl}
        for day, pnl in sorted(buckets.items(), key=lambda pair: pair[0])
    ]
    return {"series": series}


def _time_performance(trades: list[dict[str, Any]]) -> dict[str, Any]:
    hourly: dict[int, list[float]] = defaultdict(list)
    weekday: dict[int, list[float]] = defaultdict(list)
    for item in trades:
        exit_local = item["exit_time"].astimezone()
        hourly[exit_local.hour].append(item["realized_pnl"])
        weekday[exit_local.weekday()].append(item["realized_pnl"])
    return {
        "hourly": [_bucket_summary(hour, values) for hour, values in sorted(hourly.items())],
        "weekday": [_bucket_summary(day, values) for day, values in sorted(weekday.items())],
    }


def _symbol_breakdown(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in trades:
        buckets[item["symbol"]].append(item)
    rows = []
    for symbol, items in sorted(buckets.items(), key=lambda pair: pair[0]):
        wins = sum(1 for item in items if item["realized_pnl"] > 0)
        losses = sum(1 for item in items if item["realized_pnl"] < 0)
        total = len(items)
        win_rate = wins / (wins + losses) if wins + losses else None
        total_net = sum(item["realized_pnl"] for item in items)
        total_win = sum(item["realized_pnl"] for item in items if item["realized_pnl"] > 0)
        total_loss = sum(item["realized_pnl"] for item in items if item["realized_pnl"] < 0)
        profit_factor = None
        if total_loss < 0:
            profit_factor = total_win / abs(total_loss)
        avg_net = total_net / total if total else 0.0
        rows.append(
            {
                "symbol": symbol,
                "trades": total,
                "win_rate": win_rate,
                "total_net_pnl": total_net,
                "avg_net_pnl": avg_net,
                "profit_factor": profit_factor,
            }
        )
    return rows


def _bucket_summary(key: int, values: list[float]) -> dict[str, Any]:
    wins = sum(1 for value in values if value > 0)
    losses = sum(1 for value in values if value < 0)
    total = len(values)
    win_rate = wins / (wins + losses) if wins + losses else None
    return {
        "bucket": key,
        "count": total,
        "total_pnl": sum(values),
        "avg_pnl": sum(values) / total if total else 0.0,
        "win_rate": win_rate,
    }


def _calendar_data(trades: list[dict[str, Any]], month_param: str | None = None) -> dict[str, Any]:
    today = datetime.now().astimezone().date()
    month_start = _parse_month(month_param) or date(today.year, today.month, 1)
    next_month = month_start.replace(day=28) + timedelta(days=4)
    month_end = next_month.replace(day=1) - timedelta(days=1)

    daily_buckets: dict[str, dict[str, Any]] = {}
    for item in trades:
        day = item["exit_time"].astimezone().date()
        key = day.isoformat()
        bucket = daily_buckets.setdefault(key, {"pnl": 0.0, "trades": []})
        bucket["pnl"] += item.get("pnl_value", item["realized_pnl"])
        bucket["trades"].append(
            {
                "symbol": item["symbol"],
                "side": item["side"],
                "pnl": item.get("pnl_value", item["realized_pnl"]),
                "exit_time": item["exit_time"],
            }
        )

    max_abs = 0.0
    for bucket in daily_buckets.values():
        max_abs = max(max_abs, abs(bucket["pnl"]))

    start_weekday = month_start.weekday()  # Monday=0
    grid_start = month_start - timedelta(days=start_weekday)
    grid_end = month_end + timedelta(days=(6 - month_end.weekday()))

    weeks = []
    cursor = grid_start
    while cursor <= grid_end:
        week = []
        for _ in range(7):
            key = cursor.isoformat()
            bucket = daily_buckets.get(key, {"pnl": 0.0, "trades": []})
            week.append(
                {
                    "date": key,
                    "day": cursor.day,
                    "in_month": cursor.month == month_start.month,
                    "pnl": bucket["pnl"],
                    "trades": bucket["trades"],
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)

    return {
        "month_label": month_start.strftime("%B %Y"),
        "month_key": month_start.strftime("%Y-%m"),
        "prev_month": (month_start - timedelta(days=1)).strftime("%Y-%m"),
        "next_month": (month_end + timedelta(days=1)).strftime("%Y-%m"),
        "month_options": _month_options(trades, month_start),
        "weeks": weeks,
        "max_abs_pnl": max_abs,
    }


def _parse_month(value: str | None) -> date | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        return None
    return date(parsed.year, parsed.month, 1)


def _month_options(trades: list[dict[str, Any]], current_month: date) -> list[dict[str, str]]:
    if trades:
        min_date = min(item["exit_time"].astimezone().date() for item in trades)
        max_date = max(item["exit_time"].astimezone().date() for item in trades)
        start = date(min_date.year, min_date.month, 1)
        end = date(max_date.year, max_date.month, 1)
    else:
        start = _shift_month(current_month, -6)
        end = _shift_month(current_month, 6)

    options = []
    cursor = start
    while cursor <= end:
        options.append({"key": cursor.strftime("%Y-%m"), "label": cursor.strftime("%B %Y")})
        cursor = _shift_month(cursor, 1)
    return options


def _shift_month(value: date, delta: int) -> date:
    year = value.year + (value.month - 1 + delta) // 12
    month = (value.month - 1 + delta) % 12 + 1
    return date(year, month, 1)


def _pnl_distribution(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [item["realized_pnl"] for item in trades]
    if not values:
        return {"bins": []}

    low = min(values)
    high = max(values)
    if low == high:
        return {
            "bins": [
                {
                    "label": _format_money(low),
                    "count": len(values),
                    "start": low,
                    "end": high,
                    "side": "zero" if low == 0 else ("pos" if low > 0 else "neg"),
                }
            ],
            "low": low,
            "high": high,
        }

    buckets_per_side = 6
    bins: list[dict[str, Any]] = []

    if low < 0:
        neg_span = abs(low) / buckets_per_side
        neg_edges = [low + neg_span * idx for idx in range(buckets_per_side + 1)]
        neg_counts = [0 for _ in range(buckets_per_side)]
        for value in values:
            if value >= 0:
                continue
            idx = buckets_per_side - 1 if value == 0 else int((value - low) / neg_span)
            idx = max(0, min(buckets_per_side - 1, idx))
            neg_counts[idx] += 1
        for idx, count in enumerate(neg_counts):
            start = neg_edges[idx]
            end = neg_edges[idx + 1]
            bins.append(
                {
                    "label": f"{_format_money(start)} to {_format_money(end)}",
                    "count": count,
                    "start": start,
                    "end": end,
                    "side": "neg",
                }
            )

    if high > 0:
        pos_span = high / buckets_per_side
        pos_edges = [0.0 + pos_span * idx for idx in range(buckets_per_side + 1)]
        pos_counts = [0 for _ in range(buckets_per_side)]
        for value in values:
            if value < 0:
                continue
            idx = buckets_per_side - 1 if value == high else int(value / pos_span)
            idx = max(0, min(buckets_per_side - 1, idx))
            pos_counts[idx] += 1
        for idx, count in enumerate(pos_counts):
            start = pos_edges[idx]
            end = pos_edges[idx + 1]
            bins.append(
                {
                    "label": f"{_format_money(start)} to {_format_money(end)}",
                    "count": count,
                    "start": start,
                    "end": end,
                    "side": "pos",
                }
            )

    return {"bins": bins, "low": low, "high": high}


def _mean_value(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _format_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.2f}"


def _format_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def money_filter(value: float | None) -> str:
    if value is None or isinstance(value, Undefined):
        return "n/a"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "n/a"
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def percent_filter(value: float | None) -> str:
    return _format_percent(value)


def duration_filter(seconds: float | None) -> str:
    return _format_duration(seconds)


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, Undefined):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def timestamp_filter(value: Any) -> str:
    parsed = _coerce_datetime(value)
    if parsed is None:
        return "n/a"
    local = parsed.astimezone()
    return local.strftime("%b %d, %Y %H:%M")


def date_only_filter(value: Any) -> str:
    parsed = _coerce_datetime(value)
    if parsed is None:
        return "n/a"
    local = parsed.astimezone()
    return local.strftime("%b %d, %Y")


def iso_filter(value: Any) -> str:
    parsed = _coerce_datetime(value)
    if parsed is None:
        return ""
    return parsed.astimezone().isoformat()


def json_filter(value: Any) -> str:
    return json.dumps(value, default=str)


TEMPLATES.env.filters.update(
    {
        "money": money_filter,
        "percent": percent_filter,
        "duration": duration_filter,
        "timestamp": timestamp_filter,
        "date_only": date_only_filter,
        "iso": iso_filter,
        "json": json_filter,
    }
)


def main() -> None:
    import uvicorn

    app_config = load_app_config()
    uvicorn.run(
        "trade_journal.web.app:app",
        host=app_config.app.host,
        port=app_config.app.port,
        reload=app_config.app.reload,
    )


if __name__ == "__main__":
    main()
