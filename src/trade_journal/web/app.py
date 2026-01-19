from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_liquidations import load_liquidations
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.metrics.risk import initial_stop_for_trade
from trade_journal.metrics.summary import (
    compute_aggregate_metrics,
    compute_trade_metrics,
    compute_zella_score,
)
from trade_journal.ingest.apex_orders import load_orders
from trade_journal.reconstruct.funding import apply_funding_events
from trade_journal.reconstruct.trades import reconstruct_trades


APP_ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_ROOT / "templates"))


app = FastAPI(title="Trade Journal")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")


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


def _load_journal_state() -> dict[str, Any]:
    fills_path, funding_path, liquidations_path, excursions_path, orders_path = _resolve_paths()
    data_note = None

    if fills_path is None:
        return {
            "summary": {},
            "equity_curve": {},
            "daily_pnl": {},
            "pnl_distribution": {},
            "recent_trades": [],
            "trades": [],
            "symbols": [],
            "liquidations": [],
            "data_note": "No fills file found. Place a fills export at data/fills.json or set TRADE_JOURNAL_FILLS.",
        }

    ingest_result = load_fills(fills_path)
    trades = reconstruct_trades(ingest_result.fills)

    if ingest_result.skipped:
        data_note = f"Skipped {ingest_result.skipped} fill rows during normalization."

    if funding_path is not None:
        funding_result = load_funding(funding_path)
        attributions = apply_funding_events(trades, funding_result.events)
        unmatched = sum(1 for item in attributions if item.matched_trade_id is None)
        if funding_result.skipped:
            data_note = _append_note(data_note, f"Skipped {funding_result.skipped} funding rows.")
        if unmatched:
            data_note = _append_note(data_note, f"Unmatched funding events: {unmatched}.")

    orders: list[Any] = []
    if orders_path is not None:
        orders_result = load_orders(orders_path)
        orders = orders_result.orders
        if orders_result.skipped:
            data_note = _append_note(data_note, f"Skipped {orders_result.skipped} order rows.")
    elif trades:
        data_note = _append_note(data_note, "Orders file missing: R metrics unavailable.")

    liquidation_events: list[dict[str, Any]] = []
    liquidation_matches: dict[str, dict[str, Any]] = {}
    if liquidations_path is not None:
        liquidation_result = load_liquidations(liquidations_path)
        if liquidation_result.skipped:
            data_note = _append_note(data_note, f"Skipped {liquidation_result.skipped} liquidation rows.")

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

    risk_map: dict[str, Any] = {}
    if orders:
        for trade in trades:
            risk = initial_stop_for_trade(trade, orders)
            setattr(trade, "r_multiple", risk.r_multiple)
            risk_map[trade.trade_id] = risk

    metrics = compute_aggregate_metrics(trades) if trades else None
    summary = _summary_payload(metrics) if metrics else _empty_summary()
    zella_score = compute_zella_score(trades, metrics) if metrics else None

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
        "zella_score": zella_score,
        "recent_trades": trade_items[:6],
        "trades": trade_items,
        "symbols": sorted({item["symbol"] for item in trade_items}),
        "liquidations": liquidation_events,
        "data_note": data_note,
    }


def _resolve_paths() -> tuple[Path | None, Path | None, Path | None, Path | None, Path | None]:
    fills_env = os.environ.get("TRADE_JOURNAL_FILLS")
    funding_env = os.environ.get("TRADE_JOURNAL_FUNDING")
    liquidations_env = os.environ.get("TRADE_JOURNAL_LIQUIDATIONS")
    excursions_env = os.environ.get("TRADE_JOURNAL_EXCURSIONS")
    orders_env = os.environ.get("TRADE_JOURNAL_ORDERS")

    fills_path = Path(fills_env) if fills_env else Path("data/fills.json")
    if not fills_path.exists():
        fills_path = Path("data/fills.csv")
    if not fills_path.exists():
        fills_path = None

    funding_path = None
    if funding_env:
        candidate = Path(funding_env)
        if candidate.exists():
            funding_path = candidate
    else:
        candidate = Path("data/funding.json")
        if candidate.exists():
            funding_path = candidate
    liquidations_path = None
    if liquidations_env:
        candidate = Path(liquidations_env)
        if candidate.exists():
            liquidations_path = candidate
    else:
        candidate = Path("data/liquidations.json")
        if candidate.exists():
            liquidations_path = candidate
        else:
            candidate = Path("data/historical_pnl.json")
            if candidate.exists():
                liquidations_path = candidate

    excursions_path = None
    if excursions_env:
        candidate = Path(excursions_env)
        if candidate.exists():
            excursions_path = candidate
    else:
        candidate = Path("data/excursions.json")
        if candidate.exists():
            excursions_path = candidate

    orders_path = None
    if orders_env:
        candidate = Path(orders_env)
        if candidate.exists():
            orders_path = candidate
    else:
        candidate = Path("data/history_orders.json")
        if candidate.exists():
            orders_path = candidate
        else:
            candidate = Path("data/open_orders.json")
            if candidate.exists():
                orders_path = candidate

    return fills_path, funding_path, liquidations_path, excursions_path, orders_path


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
    return {
        "ui_id": ui_id,
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "entry_time": trade.entry_time,
        "exit_time": trade.exit_time,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "entry_size": trade.entry_size,
        "exit_size": trade.exit_size,
        "max_size": trade.max_size,
        "realized_pnl": trade.realized_pnl,
        "realized_pnl_net": trade.realized_pnl_net,
        "fees": trade.fees,
        "funding_fees": trade.funding_fees,
        "mae": trade.mae,
        "mfe": trade.mfe,
        "etd": trade.etd,
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


def _trade_ui_id(trade) -> str:
    parts = [
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


def _apply_excursions_cache(trades: list, excursions_map: dict[str, dict[str, Any]]) -> None:
    for trade in trades:
        excursions = excursions_map.get(_excursions_key(trade))
        if excursions is None:
            excursions = excursions_map.get(_excursions_key(trade, use_local=True))
        if not excursions:
            continue
        trade.mae = excursions.get("mae")
        trade.mfe = excursions.get("mfe")
        trade.etd = excursions.get("etd")


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
        "mean_mae": None,
        "median_mae": None,
        "mean_mfe": None,
        "median_mfe": None,
        "mean_etd": None,
        "median_etd": None,
    }


def _equity_curve(trades: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda item: item["exit_time"])
    cumulative = 0.0
    points = []
    for item in ordered:
        cumulative += item["realized_pnl_net"]
        points.append(
            {
                "t": item["exit_time"].isoformat(),
                "v": cumulative,
                "symbol": item["symbol"],
                "pnl": item["realized_pnl_net"],
            }
        )
    return {
        "points": points,
    }


def _daily_pnl(trades: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, float] = defaultdict(float)
    for item in trades:
        day = item["exit_time"].date().isoformat()
        buckets[day] += item["realized_pnl_net"]
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
        bucket["pnl"] += item["realized_pnl_net"]
        bucket["trades"].append(
            {
                "symbol": item["symbol"],
                "side": item["side"],
                "pnl": item["realized_pnl_net"],
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
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


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

    host = os.environ.get("TRADE_JOURNAL_HOST", "127.0.0.1")
    port_text = os.environ.get("TRADE_JOURNAL_PORT", "8000")
    try:
        port = int(port_text)
    except ValueError:
        port = 8000
    reload_flag = os.environ.get("TRADE_JOURNAL_RELOAD", "true").lower() in {"1", "true", "yes"}
    uvicorn.run("trade_journal.web.app:app", host=host, port=port, reload=reload_flag)


if __name__ == "__main__":
    main()
