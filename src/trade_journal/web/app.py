from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined

from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.config.app_config import load_app_config
from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_equity import load_equity_history
from trade_journal.ingest.apex_liquidations import load_liquidations
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.storage import sqlite_reader
from trade_journal.metrics.equity import apply_equity_at_entry
from trade_journal.metrics.risk import initial_stop_for_trade
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
from trade_journal.reconstruct.funding import apply_funding_events
from trade_journal.reconstruct.trades import reconstruct_trades


APP_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_ROOT / "templates"))


app = FastAPI(title="Trade Journal")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

_SYNC_LOCK = asyncio.Lock()
_SESSION_WINDOWS = {
    "asia": (0, 8),
    "london": (8, 16),
    "ny": (16, 24),
}
_SESSION_LABELS = ("asia", "london", "ny")
_NORMALIZATION_MODES = ("usd", "percent", "r")


@app.on_event("startup")
async def _start_auto_sync() -> None:
    if not _auto_sync_enabled():
        return
    asyncio.create_task(_auto_sync_loop())


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    payload = _load_journal_state()
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
        "calendar": calendar_data,
        "data_note": data_note,
        "data_note_class": "",
    }
    return TEMPLATES.TemplateResponse("dashboard.html", context)


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request) -> HTMLResponse:
    payload = _load_journal_state()
    context = {
        "request": request,
        "page": "trades",
        "trades": payload["trades"],
        "symbols": payload["symbols"],
        "data_note": payload["data_note"],
        "data_note_class": _note_class(payload["data_note"]),
    }
    return TEMPLATES.TemplateResponse("trades.html", context)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request) -> HTMLResponse:
    payload = _load_journal_state()
    month_param = request.query_params.get("month")
    calendar_data = _calendar_data(payload["trades"], month_param)
    context = {
        "request": request,
        "page": "calendar",
        "calendar": calendar_data,
        "data_note": None,
        "data_note_class": "",
    }
    return TEMPLATES.TemplateResponse("calendar.html", context)


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request) -> HTMLResponse:
    context = resolve_account_context(env=os.environ)
    db_path = _resolve_db_path()
    if db_path is not None and db_path.exists():
        payload = _load_analytics_state_db(db_path)
    else:
        payload = _load_journal_state()
    trade_items = payload["trades"]
    trade_objects = payload.get("trade_objects", [])

    accounts = payload.get("accounts") or _accounts_from_trades(trade_items)
    strategies = payload.get("strategies") or _strategies_from_trades(trade_items)
    filters = _parse_analytics_filters(request, payload["symbols"], accounts, strategies)
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
        "scatter": diagnostics,
        "diagnostics_table": diagnostics_table,
        "trades": filtered_items,
        "calendar": calendar_data,
        "symbols": payload["symbols"],
        "accounts": accounts,
        "strategies": strategies,
        "query_base": filters["query_base"],
        "data_note": payload.get("data_note"),
        "data_note_class": _note_class(payload.get("data_note")),
    }
    return TEMPLATES.TemplateResponse("analytics.html", context)


@app.get("/trades/{trade_id}", response_class=HTMLResponse)
def trade_detail(request: Request, trade_id: str) -> HTMLResponse:
    payload = _load_journal_state()
    trade = next((item for item in payload["trades"] if item["ui_id"] == trade_id), None)
    context = {
        "request": request,
        "page": "trade",
        "trade": trade,
        "data_note": None,
        "data_note_class": "",
    }
    return TEMPLATES.TemplateResponse("trade_detail.html", context)


@app.get("/api/summary")
def summary_api() -> dict[str, Any]:
    payload = _load_journal_state()
    return {
        "summary": payload["summary"],
        "equity_curve": payload["equity_curve"],
        "daily_pnl": payload["daily_pnl"],
        "pnl_distribution": payload["pnl_distribution"],
    }


@app.get("/api/trades")
def trades_api() -> list[dict[str, Any]]:
    payload = _load_journal_state()
    return payload["trades"]


@app.get("/api/trades/{trade_id}/series")
def trade_series_api(trade_id: str) -> dict[str, Any]:
    context = resolve_account_context(env=os.environ)
    series_path = _resolve_trade_series_path(context)
    if series_path is None:
        raise HTTPException(status_code=404, detail="Trade series cache not found.")

    payload = _load_journal_state()
    trade = next((item for item in payload["trade_objects"] if _trade_ui_id(item) == trade_id), None)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found.")

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


def _load_journal_state() -> dict[str, Any]:
    context = resolve_account_context(env=os.environ)
    db_path = _resolve_db_path()
    if db_path is not None and db_path.exists():
        return _load_journal_state_db(context, db_path)
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
            "data_note": note,
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
        "account": _load_account_snapshot(context),
        "account_context": {
            "name": context.name,
            "source": context.source,
            "account_id": context.account_id,
            "data_dir": str(context.data_dir),
        },
        "recent_trades": trade_items[:6],
        "trades": trade_items,
        "trade_objects": trades,
        "symbols": sorted({item["symbol"] for item in trade_items}),
        "liquidations": liquidation_events,
        "data_note": data_note,
    }


def _load_journal_state_db(context, db_path: Path) -> dict[str, Any]:
    conn = sqlite_reader.connect(db_path)
    from trade_journal.storage.sqlite_store import init_db

    init_db(conn)
    try:
        fills = sqlite_reader.load_fills(conn, source=context.source, account_id=context.account_id)
        trades = reconstruct_trades(fills)
        funding_events = sqlite_reader.load_funding(conn, source=context.source, account_id=context.account_id)
        attributions = apply_funding_events(trades, funding_events)
        unmatched = sum(1 for item in attributions if item.matched_trade_id is None)
        orders = sqlite_reader.load_orders(conn, source=context.source, account_id=context.account_id)
        liquidation_events_raw = sqlite_reader.load_liquidations(
            conn, source=context.source, account_id=context.account_id
        )
        equity_snapshots = sqlite_reader.load_equity_history(
            conn, source=context.source, account_id=context.account_id
        )
    finally:
        conn.close()

    data_note = None
    if not fills:
        data_note = "No fills found in database."
    if unmatched:
        data_note = _append_note(data_note, f"Unmatched funding events: {unmatched}.")

    app_config = load_app_config()
    excursions_map: dict[str, dict[str, Any]] = {}
    excursions_path = None
    candidate = app_config.paths.excursions
    if candidate and candidate.exists():
        excursions_path = candidate
    else:
        candidate = resolve_data_path(None, context, "excursions.json")
        if candidate.exists():
            excursions_path = candidate
    if excursions_path is not None:
        try:
            excursions_map = json.loads(excursions_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data_note = _append_note(data_note, "Failed to parse excursions cache.")
    elif trades:
        data_note = _append_note(data_note, "Excursions cache missing: MAE/MFE/ETD shown as n/a.")

    if excursions_map:
        _apply_excursions_cache(trades, excursions_map)

    if equity_snapshots:
        _apply_equity(trades, equity_snapshots, context)

    liquidation_matches = _match_liquidations(trades, liquidation_events_raw)
    liquidation_events = _build_liquidation_events(trades, liquidation_events_raw, liquidation_matches)

    risk_map: dict[str, Any] = {}
    if orders:
        for trade in trades:
            risk = initial_stop_for_trade(trade, orders)
            setattr(trade, "r_multiple", risk.r_multiple)
            setattr(trade, "initial_risk", risk.risk_amount)
            risk_map[trade.trade_id] = risk

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
        "account": _load_account_snapshot(context),
        "account_context": {
            "name": context.name,
            "source": context.source,
            "account_id": context.account_id,
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
        fills = sqlite_reader.load_fills_all(conn)
        trades = reconstruct_trades(fills)
        funding_events = sqlite_reader.load_funding_all(conn)
        attributions = apply_funding_events(trades, funding_events)
        unmatched = sum(1 for item in attributions if item.matched_trade_id is None)
        orders = sqlite_reader.load_orders_all(conn)
        liquidation_events_raw = sqlite_reader.load_liquidations_all(conn)
        equity_snapshots = sqlite_reader.load_equity_history_all(conn)
        accounts = sqlite_reader.load_accounts(conn)
        tags = sqlite_reader.load_tags(conn, active_only=True)
        trade_tags = sqlite_reader.load_trade_tags(conn)
    finally:
        conn.close()

    data_note = None
    if not fills:
        data_note = "No fills found in database."
    if unmatched:
        data_note = _append_note(data_note, f"Unmatched funding events: {unmatched}.")

    app_config = load_app_config()
    excursions_map: dict[str, dict[str, Any]] = {}
    excursions_path = None
    candidate = app_config.paths.excursions
    if candidate and candidate.exists():
        excursions_path = candidate
    else:
        candidate = resolve_data_path(None, resolve_account_context(env=os.environ), "excursions.json")
        if candidate.exists():
            excursions_path = candidate
    if excursions_path is not None:
        try:
            excursions_map = json.loads(excursions_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data_note = _append_note(data_note, "Failed to parse excursions cache.")
    elif trades:
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

    trade_items = [
        _trade_payload(
            trade,
            liquidation_matches.get(trade.trade_id),
            risk_map.get(trade.trade_id),
        )
        for trade in trades
    ]
    trade_items.sort(key=lambda item: item["exit_time"], reverse=True)

    strategies = _apply_trade_tags(trade_items, tags, trade_tags)
    active_accounts = [row for row in accounts if row.get("active", 1)]

    return {
        "trades": trade_items,
        "trade_objects": trades,
        "symbols": sorted({item["symbol"] for item in trade_items}),
        "liquidations": liquidation_events,
        "data_note": data_note,
        "accounts": active_accounts,
        "strategies": strategies,
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
        str(account.get("account_id")): account
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
        account_id = key[1]
        fallback = None
        if account_id and account_id in account_map:
            fallback = account_map[account_id].get("starting_equity")
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
            await asyncio.to_thread(_sync_once)
            if _sync_runs_excursions():
                await asyncio.to_thread(_run_excursions, db_path)
    except Exception as exc:
        print(f"Auto-sync failed: {exc}")


def _sync_once() -> None:
    from trade_journal import sync_api

    app_config = load_app_config()
    env_path = app_config.app.env_path
    sync_api.sync_once(
        db_path=_resolve_db_path() or Path("data/trade_journal.sqlite"),
        account=os.environ.get("TRADE_JOURNAL_ACCOUNT_NAME"),
        env_path=env_path,
        base_url=app_config.api.base_url,
        limit=_sync_limit(),
        max_pages=_sync_max_pages(),
        overlap_hours=_sync_overlap_hours(),
        end_ms=_sync_end_ms(),
    )


def _run_excursions(db_path: Path) -> None:
    from trade_journal import compute_excursions

    args = ["--db", str(db_path)]
    account = os.environ.get("TRADE_JOURNAL_ACCOUNT_NAME")
    if account:
        args += ["--account", account]
    max_points = _series_max_points()
    if max_points is not None:
        args += ["--series-max-points", str(max_points)]
    compute_excursions.main(args)


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
) -> dict[str, Any]:
    metrics = compute_trade_metrics(trade)
    ui_id = _trade_ui_id(trade)
    capture_pct, heat_pct = _capture_heat_pct(trade)
    return {
        "ui_id": ui_id,
        "trade_id": trade.trade_id,
        "source": trade.source,
        "account_id": trade.account_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "entry_time": trade.entry_time,
        "exit_time": trade.exit_time,
        "session": _session_label(trade.exit_time),
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
    }


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
    account_ids_set: set[str] = set()
    for item in items:
        account_id = item.get("account_id")
        if not account_id:
            continue
        account_ids_set.add(str(account_id))
    account_ids = sorted(account_ids_set)
    return [{"account_id": account_id, "name": account_id} for account_id in account_ids]


def _strategies_from_trades(items: list[dict[str, Any]]) -> list[str]:
    strategies: set[str] = set()
    for item in items:
        for strategy in item.get("strategy_tags") or []:
            if not strategy:
                continue
            strategies.add(str(strategy))
    return sorted(strategies)


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
    capture_pct = (trade.realized_pnl_net / mfe) * 100.0
    heat_pct = abs((trade.mae / mfe) * 100.0) if trade.mae is not None else None
    return capture_pct, heat_pct


def _session_label(timestamp: datetime) -> str:
    hour = timestamp.astimezone(timezone.utc).hour
    for label, window in _SESSION_WINDOWS.items():
        start, end = window
        if start <= hour < end:
            return label
    return "asia"


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
            payload["total_pnl"] = trade.realized_pnl_net
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


def _load_account_snapshot(context) -> dict[str, Any] | None:
    db_path = _resolve_db_path()
    if db_path is not None:
        conn = sqlite_reader.connect(db_path)
        try:
            return sqlite_reader.load_account_snapshot(
                conn, source=context.source, account_id=context.account_id
            )
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
    return snapshot


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

    account_ids = [str(account.get("account_id")) for account in accounts if account.get("account_id")]
    account = params.get("account", "all").strip()
    if not account or account.lower() == "all":
        account = "all"
    elif account not in account_ids:
        account = "all"

    session = params.get("session", "all").strip().lower()
    if session not in _SESSION_LABELS:
        session = "all"

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
    if session != "all":
        query_parts.append(f"session={session}")
    if strategy != "all":
        query_parts.append(f"strategy={strategy}")
    if normalization != "usd":
        query_parts.append(f"normalization={normalization}")
    query_base = "&".join(query_parts)

    return {
        "tab": tab,
        "from": from_date,
        "to": to_date,
        "symbols": valid_symbols,
        "side": side,
        "outcome": outcome,
        "account": account,
        "session": session,
        "strategy": strategy,
        "normalization": normalization,
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
        if filters["account"] != "all" and item.get("account_id") != filters["account"]:
            continue
        if filters["session"] != "all" and item.get("session") != filters["session"]:
            continue
        if filters["strategy"] != "all":
            strategies = item.get("strategy_tags") or []
            if filters["strategy"] not in strategies:
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
        payload["pnl_value"] = trade.realized_pnl_net
        payload["mae_value"] = trade.mae
        payload["mfe_value"] = trade.mfe
        payload["etd_value"] = trade.etd
        normalized_items.append(payload)
    return normalized_items


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


def _benchmark_candidates() -> tuple[list[str], list[str]]:
    app_config = load_app_config()
    interval = str(app_config.pricing.interval)
    timeframes = []
    if interval.endswith(("m", "h", "d")):
        timeframes.append(interval)
    else:
        timeframes.extend([f"{interval}m", interval])
    if "1m" not in timeframes:
        timeframes.append("1m")
    symbols = ["BTCUSDT", "BTC-USD", "BTC-USDT"]
    return symbols, timeframes


def _benchmark_return(
    db_path: Path | None,
    window: tuple[datetime, datetime] | None,
) -> float | None:
    if db_path is None or window is None or not db_path.exists():
        return None
    start, end = window
    symbols, timeframes = _benchmark_candidates()
    conn = sqlite_reader.connect(db_path)
    try:
        for symbol in symbols:
            for timeframe in timeframes:
                rows = sqlite_reader.load_benchmark_prices(
                    conn,
                    symbol=symbol,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                )
                if len(rows) < 2:
                    continue
                first_open = rows[0].get("open")
                last_close = rows[-1].get("close")
                if not first_open:
                    continue
                return (last_close - first_open) / first_open
    finally:
        conn.close()
    return None


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
            "pnl": item.get("pnl_value", item["realized_pnl_net"]),
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
            "y": item.get("pnl_value", item["realized_pnl_net"]),
            "symbol": item["symbol"],
            "trade_id": item["ui_id"],
            "pnl": item.get("pnl_value", item["realized_pnl_net"]),
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
            "pnl": item.get("pnl_value", item["realized_pnl_net"]),
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
        pnl_value = item.get("pnl_value", item["realized_pnl_net"])
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
        buckets[day] += item.get("pnl_value", item["realized_pnl_net"])
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
        hourly[exit_local.hour].append(item["realized_pnl_net"])
        weekday[exit_local.weekday()].append(item["realized_pnl_net"])
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
        wins = sum(1 for item in items if item["realized_pnl_net"] > 0)
        losses = sum(1 for item in items if item["realized_pnl_net"] < 0)
        total = len(items)
        win_rate = wins / (wins + losses) if wins + losses else None
        total_net = sum(item["realized_pnl_net"] for item in items)
        total_win = sum(item["realized_pnl_net"] for item in items if item["realized_pnl_net"] > 0)
        total_loss = sum(item["realized_pnl_net"] for item in items if item["realized_pnl_net"] < 0)
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
        bucket["pnl"] += item.get("pnl_value", item["realized_pnl_net"])
        bucket["trades"].append(
            {
                "symbol": item["symbol"],
                "side": item["side"],
                "pnl": item.get("pnl_value", item["realized_pnl_net"]),
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
    values = [item["realized_pnl_net"] for item in trades]
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


def timestamp_filter(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    local = value.astimezone()
    return local.strftime("%b %d, %Y %H:%M")


def date_only_filter(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    local = value.astimezone()
    return local.strftime("%b %d, %Y")


def iso_filter(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone().isoformat()


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
