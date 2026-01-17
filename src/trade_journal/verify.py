from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.ingest.apex_orders import load_orders
from trade_journal.metrics.risk import initial_stop_for_trade
from trade_journal.reconcile import load_historical_pnl, match_trades
from trade_journal.reconstruct.funding import apply_funding_events
from trade_journal.reconstruct.trades import reconstruct_trades


@dataclass(frozen=True)
class VerifyConfig:
    pnl_abs_threshold: float
    pnl_pct_threshold: float
    r_outlier_threshold: float
    match_window_seconds: int
    min_notional: float
    neighbor_window_seconds: int
    funding_since: datetime | None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run verification checks on trades and inputs.")
    parser.add_argument(
        "fills_path",
        type=Path,
        nargs="?",
        default=Path("data/fills.json"),
        help="Path to ApeX fills export (json/csv/tsv).",
    )
    parser.add_argument(
        "--historical-pnl",
        type=Path,
        default=Path("data/historical_pnl.json"),
        help="Historical PnL JSON path.",
    )
    parser.add_argument(
        "--funding",
        type=Path,
        default=Path("data/funding.json"),
        help="Optional funding export path.",
    )
    parser.add_argument(
        "--orders",
        type=Path,
        default=Path("data/history_orders.json"),
        help="Optional history orders export path.",
    )
    parser.add_argument(
        "--excursions",
        type=Path,
        default=Path("data/excursions.json"),
        help="Optional excursions cache path.",
    )
    parser.add_argument("--out", type=Path, default=Path("data/verify_report.json"), help="Output file.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if critical issues found.")
    parser.add_argument(
        "--funding-table",
        action="store_true",
        help="Print a table of unmatched funding events.",
    )
    parser.add_argument(
        "--funding-out",
        type=Path,
        default=None,
        help="Write unmatched funding table to a file.",
    )
    parser.add_argument("--pnl-abs", type=float, default=0.5, help="PnL mismatch absolute threshold.")
    parser.add_argument(
        "--pnl-pct",
        type=float,
        default=0.0005,
        help="PnL mismatch threshold as fraction of entry notional.",
    )
    parser.add_argument("--r-outlier", type=float, default=20.0, help="Flag |R| above this threshold.")
    parser.add_argument(
        "--match-window",
        type=int,
        default=900,
        help="Historical PnL match window in seconds.",
    )
    parser.add_argument(
        "--min-notional",
        type=float,
        default=100.0,
        help="Flag trades with entry notional below this threshold.",
    )
    parser.add_argument(
        "--neighbor-window",
        type=int,
        default=120,
        help="Seconds around trade entry/exit to include nearby fills for tiny trades.",
    )
    parser.add_argument(
        "--funding-since",
        type=str,
        default=None,
        help="Ignore unmatched funding events before this datetime (local or ISO) or epoch ms.",
    )
    args = parser.parse_args(argv)

    config = VerifyConfig(
        pnl_abs_threshold=args.pnl_abs,
        pnl_pct_threshold=args.pnl_pct,
        r_outlier_threshold=args.r_outlier,
        match_window_seconds=args.match_window,
        min_notional=args.min_notional,
        neighbor_window_seconds=args.neighbor_window,
        funding_since=_parse_datetime_arg(args.funding_since) if args.funding_since else None,
    )

    report = _run_checks(
        fills_path=args.fills_path,
        historical_pnl_path=args.historical_pnl,
        funding_path=args.funding,
        orders_path=args.orders,
        excursions_path=args.excursions,
        config=config,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    _print_summary(report)

    if args.funding_table:
        _print_funding_table(report["funding_unmatched"])
    if args.funding_out is not None:
        args.funding_out.parent.mkdir(parents=True, exist_ok=True)
        args.funding_out.write_text(_format_funding_table(report["funding_unmatched"]) + "\n", encoding="utf-8")

    if args.strict and report["summary"]["critical"] > 0:
        return 1
    return 0


def _run_checks(
    fills_path: Path,
    historical_pnl_path: Path,
    funding_path: Path,
    orders_path: Path,
    excursions_path: Path,
    config: VerifyConfig,
) -> dict[str, Any]:
    ingest = load_fills(fills_path)
    fills = ingest.fills
    trades = reconstruct_trades(fills)

    funding_unmatched: list[dict[str, Any]] = []
    if funding_path.exists():
        funding_result = load_funding(funding_path)
        attributions = apply_funding_events(trades, funding_result.events)
        for item in attributions:
            if item.matched_trade_id is None:
                event = item.event
                if config.funding_since and event.funding_time < config.funding_since:
                    continue
                context = _funding_context(event, trades)
                funding_unmatched.append(
                    {
                        "funding_id": event.funding_id,
                        "transaction_id": event.transaction_id,
                        "symbol": event.symbol,
                        "side": event.side,
                        "position_size": event.position_size,
                        "funding_value": event.funding_value,
                        "funding_time": event.funding_time.isoformat(),
                        "context": context,
                    }
                )

    orders = load_orders(orders_path).orders if orders_path.exists() else []
    excursions_map = _load_excursions(excursions_path) if excursions_path.exists() else {}
    if excursions_map:
        _apply_excursions_cache(trades, excursions_map)

    historical_records = load_historical_pnl(historical_pnl_path) if historical_pnl_path.exists() else []
    matches = match_trades(trades, historical_records, window_seconds=config.match_window_seconds)

    fill_issues = _check_fills(fills)
    trade_issues = _check_trades(trades)
    pnl_mismatches = _check_pnl(matches, config)
    stop_anomalies, r_outliers = _check_risk(trades, orders, config)
    excursions_missing = _check_excursions(trades)
    tiny_trades = _check_tiny_trades(trades, fills, config)

    critical = len(fill_issues["critical"]) + len(trade_issues["critical"]) + len(pnl_mismatches["critical"])
    warning = (
        len(fill_issues["warning"])
        + len(trade_issues["warning"])
        + len(pnl_mismatches["warning"])
        + len(stop_anomalies)
        + len(r_outliers)
        + len(excursions_missing)
    )

    return {
        "summary": {
            "fills": len(fills),
            "trades": len(trades),
            "critical": critical,
            "warning": warning,
        },
        "fills": fill_issues,
        "trades": trade_issues,
        "pnl_mismatches": pnl_mismatches,
        "stop_anomalies": stop_anomalies,
        "r_outliers": r_outliers,
        "excursions_missing": excursions_missing,
        "tiny_trades": tiny_trades,
        "funding_unmatched": funding_unmatched,
    }


def _check_fills(fills) -> dict[str, list[dict[str, Any]]]:
    critical: list[dict[str, Any]] = []
    warning: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fill in fills:
        if fill.size <= 0 or fill.price <= 0:
            critical.append(_fill_issue(fill, "non_positive_size_or_price"))
        if fill.fill_id:
            if fill.fill_id in seen:
                warning.append(_fill_issue(fill, "duplicate_fill_id"))
            seen.add(fill.fill_id)
    return {"critical": critical, "warning": warning}


def _check_trades(trades) -> dict[str, list[dict[str, Any]]]:
    critical: list[dict[str, Any]] = []
    warning: list[dict[str, Any]] = []
    for trade in trades:
        if trade.entry_size <= 0 or trade.exit_size <= 0:
            critical.append(_trade_issue(trade, "zero_size_trade"))
        if trade.entry_time >= trade.exit_time:
            critical.append(_trade_issue(trade, "non_positive_duration"))
        if trade.entry_price <= 0 or trade.exit_price <= 0:
            warning.append(_trade_issue(trade, "non_positive_price"))
    return {"critical": critical, "warning": warning}


def _check_pnl(matches, config: VerifyConfig) -> dict[str, list[dict[str, Any]]]:
    critical: list[dict[str, Any]] = []
    warning: list[dict[str, Any]] = []
    for match in matches:
        trade = match.trade
        record = match.record
        delta = trade.realized_pnl_net - record.total_pnl
        entry_notional = trade.entry_price * trade.entry_size
        threshold = max(config.pnl_abs_threshold, entry_notional * config.pnl_pct_threshold)
        if abs(delta) > threshold:
            warning.append(
                {
                    "trade_id": trade.trade_id,
                    "ui_id": _trade_ui_id(trade),
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "delta": delta,
                    "threshold": threshold,
                    "exit_time": trade.exit_time.isoformat(),
                }
            )
    return {"critical": critical, "warning": warning}


def _check_risk(trades, orders, config: VerifyConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    anomalies: list[dict[str, Any]] = []
    outliers: list[dict[str, Any]] = []
    if not orders:
        return anomalies, outliers
    for trade in trades:
        risk = initial_stop_for_trade(trade, orders)
        if risk.stop_price is None:
            continue
        if trade.side == "LONG" and risk.stop_price >= trade.entry_price:
            anomalies.append(_risk_issue(trade, risk, "stop_above_entry_long"))
        if trade.side == "SHORT" and risk.stop_price <= trade.entry_price:
            anomalies.append(_risk_issue(trade, risk, "stop_below_entry_short"))
        if risk.r_multiple is not None and abs(risk.r_multiple) > config.r_outlier_threshold:
            outliers.append(_risk_issue(trade, risk, "r_outlier"))
    return anomalies, outliers


def _check_excursions(trades) -> list[dict[str, Any]]:
    missing = []
    for trade in trades:
        if trade.mae is None or trade.mfe is None or trade.etd is None:
            missing.append(_trade_issue(trade, "missing_excursions"))
    return missing


def _funding_context(event, trades) -> dict[str, Any] | None:
    candidates = [
        trade
        for trade in trades
        if trade.symbol == event.symbol and trade.side == event.side
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda trade: trade.entry_time)
    closest = None
    closest_delta = None
    position = None

    for trade in candidates:
        if trade.entry_time <= event.funding_time <= trade.exit_time:
            return {
                "status": "within_trade",
                "trade_id": trade.trade_id,
                "ui_id": _trade_ui_id(trade),
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
            }
        if event.funding_time < trade.entry_time:
            delta = (trade.entry_time - event.funding_time).total_seconds()
            if closest_delta is None or delta < closest_delta:
                closest_delta = delta
                closest = trade
                position = "before_entry"
        if event.funding_time > trade.exit_time:
            delta = (event.funding_time - trade.exit_time).total_seconds()
            if closest_delta is None or delta < closest_delta:
                closest_delta = delta
                closest = trade
                position = "after_exit"

    if closest is None:
        return None
    return {
        "status": position,
        "trade_id": closest.trade_id,
        "ui_id": _trade_ui_id(closest),
        "delta_seconds": closest_delta,
        "entry_time": closest.entry_time.isoformat(),
        "exit_time": closest.exit_time.isoformat(),
    }


def _check_tiny_trades(trades, fills, config: VerifyConfig) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not fills:
        return results
    for trade in trades:
        entry_notional = trade.entry_price * trade.entry_size
        if entry_notional >= config.min_notional:
            continue
        nearby_fills = _nearby_fills(trade, fills, config.neighbor_window_seconds)
        results.append(
            {
                "trade": _trade_issue(trade, "tiny_trade"),
                "entry_notional": entry_notional,
                "fills": [
                    {
                        "fill_id": fill.fill_id,
                        "order_id": fill.order_id,
                        "symbol": fill.symbol,
                        "side": fill.side,
                        "price": fill.price,
                        "size": fill.size,
                        "timestamp": fill.timestamp.isoformat(),
                    }
                    for fill in trade.fills
                ],
                "nearby_fills": [
                    {
                        "fill_id": fill.fill_id,
                        "order_id": fill.order_id,
                        "symbol": fill.symbol,
                        "side": fill.side,
                        "price": fill.price,
                        "size": fill.size,
                        "timestamp": fill.timestamp.isoformat(),
                    }
                    for fill in nearby_fills
                ],
            }
        )
    return results


def _nearby_fills(trade, fills, window_seconds: int) -> list[Any]:
    start = trade.entry_time.timestamp() - window_seconds
    end = trade.exit_time.timestamp() + window_seconds
    neighbors = [
        fill
        for fill in fills
        if fill.symbol == trade.symbol and start <= fill.timestamp.timestamp() <= end
    ]
    neighbors.sort(key=lambda item: item.timestamp)
    return neighbors


def _fill_issue(fill, reason: str) -> dict[str, Any]:
    return {
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "symbol": fill.symbol,
        "side": fill.side,
        "price": fill.price,
        "size": fill.size,
        "timestamp": fill.timestamp.isoformat(),
        "reason": reason,
    }


def _trade_issue(trade, reason: str) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "ui_id": _trade_ui_id(trade),
        "symbol": trade.symbol,
        "side": trade.side,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "entry_size": trade.entry_size,
        "exit_size": trade.exit_size,
        "reason": reason,
    }


def _risk_issue(trade, risk, reason: str) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "ui_id": _trade_ui_id(trade),
        "symbol": trade.symbol,
        "side": trade.side,
        "entry_price": trade.entry_price,
        "stop_price": risk.stop_price,
        "r_multiple": risk.r_multiple,
        "source": risk.source,
        "reason": reason,
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


def _load_excursions(path: Path) -> dict[str, dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _apply_excursions_cache(trades, excursions_map: dict[str, dict[str, Any]]) -> None:
    for trade in trades:
        excursions = excursions_map.get(_excursions_key(trade))
        if excursions is None:
            excursions = excursions_map.get(_excursions_key(trade, use_local=True))
        if not excursions:
            continue
        trade.mae = excursions.get("mae")
        trade.mfe = excursions.get("mfe")
        trade.etd = excursions.get("etd")


def _excursions_key(trade, use_local: bool = False) -> str:
    entry = trade.entry_time.astimezone() if use_local else trade.entry_time
    exit_ = trade.exit_time.astimezone() if use_local else trade.exit_time
    parts = [
        trade.symbol,
        trade.side,
        entry.isoformat(),
        exit_.isoformat(),
        f"{trade.entry_price:.8f}",
        f"{trade.exit_price:.8f}",
        f"{trade.entry_size:.8f}",
        f"{trade.exit_size:.8f}",
        f"{trade.realized_pnl:.8f}",
    ]
    return "|".join(parts)


def _print_summary(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"fills {summary['fills']}")
    print(f"trades {summary['trades']}")
    print(f"critical {summary['critical']}")
    print(f"warning {summary['warning']}")
    print(f"fill_critical {len(report['fills']['critical'])}")
    print(f"fill_warning {len(report['fills']['warning'])}")
    print(f"trade_critical {len(report['trades']['critical'])}")
    print(f"trade_warning {len(report['trades']['warning'])}")
    print(f"pnl_warning {len(report['pnl_mismatches']['warning'])}")
    print(f"stop_anomalies {len(report['stop_anomalies'])}")
    print(f"r_outliers {len(report['r_outliers'])}")
    print(f"excursions_missing {len(report['excursions_missing'])}")
    print(f"tiny_trades {len(report['tiny_trades'])}")
    print(f"funding_unmatched {len(report['funding_unmatched'])}")


def _print_funding_table(items: list[dict[str, Any]]) -> None:
    if not items:
        print("No unmatched funding events.")
        return
    print(_format_funding_table(items))


def _format_funding_table(items: list[dict[str, Any]]) -> str:
    headers = ["time", "symbol", "side", "size", "funding", "context"]
    rows = []
    for item in items:
        context = item.get("context") or {}
        status = context.get("status", "")
        rows.append(
            [
                item.get("funding_time", ""),
                item.get("symbol", ""),
                item.get("side", ""),
                _format_float(item.get("position_size")),
                _format_float(item.get("funding_value")),
                status,
            ]
        )
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    lines = []
    lines.append(" ".join(h.ljust(widths[idx]) for idx, h in enumerate(headers)))
    lines.append(" ".join("-" * widths[idx] for idx in range(len(headers))))
    for row in rows:
        lines.append(" ".join(row[idx].ljust(widths[idx]) for idx in range(len(headers))))
    return "\n".join(lines)


def _format_float(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.6g}"
    except (TypeError, ValueError):
        return "n/a"


def _parse_datetime_arg(value: str) -> datetime:
    text = value.strip()
    if text.isdigit():
        epoch = int(text)
        if len(text) >= 13:
            return datetime.fromtimestamp(epoch / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
