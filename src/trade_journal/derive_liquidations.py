from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.ingest.apex_liquidations import LiquidationEvent, extract_liquidations
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.ingest.apex_orders import load_orders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive liquidation events from fills and/or history orders."
    )
    parser.add_argument("--fills", type=Path, default=None, help="Fills JSON path.")
    parser.add_argument(
        "--orders",
        type=Path,
        default=None,
        help="History orders JSON path.",
    )
    parser.add_argument(
        "--source",
        choices=("fills", "orders", "both"),
        default="both",
        help="Data sources to inspect.",
    )
    parser.add_argument("--raw", action="store_true", help="Write raw records instead of normalized events.")
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument("--out", type=Path, default=None, help="Output file.")
    args = parser.parse_args(argv)

    context = resolve_account_context(args.account)
    fills_path = args.fills or resolve_data_path(None, context, "fills.json")
    if not fills_path.exists():
        candidate = resolve_data_path(None, context, "fills.csv")
        fills_path = candidate if candidate.exists() else fills_path
    orders_path = args.orders or resolve_data_path(None, context, "history_orders.json")

    records: list[Mapping[str, Any]] = []
    if args.source in {"fills", "both"} and fills_path.exists():
        records.extend(_records_from_fills(fills_path, context))
    if args.source in {"orders", "both"} and orders_path.exists():
        records.extend(_records_from_orders(orders_path, context))

    candidates = _filter_liquidations(records)
    if args.raw:
        payload = json.dumps(candidates, indent=2, sort_keys=True)
    else:
        result = extract_liquidations(candidates, source=context.source, account_id=context.account_id)
        payload = json.dumps([_event_to_dict(event) for event in result.events], indent=2, sort_keys=True)

    out_path = args.out or resolve_data_path(None, context, "liquidations.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload + "\n", encoding="utf-8")
    return 0


def _records_from_fills(path: Path, context) -> list[Mapping[str, Any]]:
    ingest = load_fills(path, source=context.source, account_id=context.account_id)
    return [fill.raw for fill in ingest.fills]


def _records_from_orders(path: Path, context) -> list[Mapping[str, Any]]:
    ingest = load_orders(path, source=context.source, account_id=context.account_id)
    return [order.raw for order in ingest.orders]


def _filter_liquidations(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for raw in records:
        if not _looks_like_liquidation(raw):
            continue
        item = dict(raw)
        item.setdefault("exitType", "Liquidate")
        item.setdefault("isLiquidate", True)
        key = _event_key(item)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _looks_like_liquidation(raw: Mapping[str, Any]) -> bool:
    exit_type = str(raw.get("exitType", raw.get("exit_type", ""))).strip().lower()
    if exit_type in {"liquidate", "liquidation"}:
        return True
    record_type = str(raw.get("type", "")).strip().lower()
    if record_type in {"liquidate", "liquidation"}:
        return True
    if _truthy(raw.get("isLiquidate")) or _truthy(raw.get("is_liquidate")):
        return True
    if _float_value(raw.get("liquidateFee", raw.get("liquidate_fee"))) > 0:
        return True
    return False


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"true", "1", "yes"}


def _float_value(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _event_key(raw: Mapping[str, Any]) -> str:
    for key in ("matchFillId", "fillId", "id", "orderId", "order_id", "clientOrderId", "clientId"):
        value = raw.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    parts = [
        str(raw.get("symbol", "")),
        str(raw.get("side", "")),
        str(raw.get("price", "")),
        str(raw.get("size", "")),
        str(raw.get("createdAt", raw.get("timestamp", raw.get("time", "")))),
    ]
    return "fallback:" + "|".join(parts)


def _event_to_dict(event: LiquidationEvent) -> dict[str, Any]:
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
        "created_at": event.created_at.isoformat(),
        "exit_type": event.exit_type,
        "is_liquidate": True,
        "raw": event.raw,
    }


if __name__ == "__main__":
    raise SystemExit(main())
