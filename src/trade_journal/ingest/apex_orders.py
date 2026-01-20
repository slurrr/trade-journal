from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class OrderRecord:
    order_id: str | None
    client_order_id: str | None
    source: str
    account_id: str | None
    symbol: str
    side: str
    size: float
    price: float | None
    reduce_only: bool
    is_position_tpsl: bool
    is_open_tpsl: bool
    is_set_open_sl: bool
    is_set_open_tp: bool
    open_sl_param: Mapping[str, Any] | None
    open_tp_param: Mapping[str, Any] | None
    trigger_price: float | None
    order_type: str | None
    status: str | None
    created_at: datetime
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class OrdersIngestResult:
    orders: list[OrderRecord]
    skipped: int = 0


def load_orders(
    path: str | Path, *, source: str | None = None, account_id: str | None = None
) -> OrdersIngestResult:
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix == ".json":
        return _load_orders_json(source_path, source_name=source, account_id=account_id)
    if suffix in {".csv", ".tsv"}:
        return _load_orders_csv(
            source_path,
            delimiter="\t" if suffix == ".tsv" else ",",
            source_name=source,
            account_id=account_id,
        )
    raise ValueError(f"Unsupported file type: {source_path.suffix}")


def _load_orders_json(
    path: Path, *, source_name: str | None, account_id: str | None
) -> OrdersIngestResult:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    records = _extract_records(payload)
    orders, skipped = _normalize_records(records, source_name=source_name, account_id=account_id)
    return OrdersIngestResult(orders=orders, skipped=skipped)


def _load_orders_csv(
    path: Path, delimiter: str, *, source_name: str | None, account_id: str | None
) -> OrdersIngestResult:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        orders, skipped = _normalize_records(reader, source_name=source_name, account_id=account_id)
    return OrdersIngestResult(orders=orders, skipped=skipped)


def _extract_records(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "orders", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("orders", "list", "records"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        if isinstance(data, list):
            return data
    return []


def _normalize_records(
    records: Iterable[Mapping[str, Any]],
    *,
    source_name: str | None,
    account_id: str | None,
) -> tuple[list[OrderRecord], int]:
    orders: list[OrderRecord] = []
    skipped = 0
    for raw in records:
        try:
            orders.append(_normalize_order(raw, source_name=source_name, account_id=account_id))
        except ValueError:
            skipped += 1
    return orders, skipped


def _normalize_order(
    raw: Mapping[str, Any], *, source_name: str | None, account_id: str | None
) -> OrderRecord:
    order_id = _pick(raw, "orderId", "id")
    client_order_id = _pick(raw, "clientOrderId", "clientId")
    resolved_account = account_id or _pick(raw, "accountId", "account_id")
    symbol = _pick(raw, "symbol", "market")
    side = _normalize_side(_pick(raw, "side"))
    size = _to_float(_pick(raw, "size", "qty"))
    price = _to_float(_pick(raw, "price", "limitPrice"), default=None)
    reduce_only = _to_bool(_pick(raw, "reduceOnly", "reduce_only"), default=False)
    is_position_tpsl = _to_bool(_pick(raw, "isPositionTpsl"), default=False)
    is_open_tpsl = _to_bool(_pick(raw, "isOpenTpslOrder"), default=False)
    is_set_open_sl = _to_bool(_pick(raw, "isSetOpenSl"), default=False)
    is_set_open_tp = _to_bool(_pick(raw, "isSetOpenTp"), default=False)
    open_sl_param = _pick(raw, "openSlParam")
    open_tp_param = _pick(raw, "openTpParam")
    trigger_price = _to_float(_pick(raw, "triggerPrice"), default=None)
    order_type = _pick(raw, "type", "orderType")
    status = _pick(raw, "status")
    created_at = _parse_timestamp(_pick(raw, "createdAt", "created_at", "timestamp", "time"))

    if not symbol or not side:
        raise ValueError("Missing required order fields")

    return OrderRecord(
        order_id=str(order_id) if order_id is not None else None,
        client_order_id=str(client_order_id) if client_order_id is not None else None,
        source=str(source_name or "apex"),
        account_id=str(resolved_account) if resolved_account is not None else None,
        symbol=str(symbol),
        side=side,
        size=size,
        price=price,
        reduce_only=reduce_only,
        is_position_tpsl=is_position_tpsl,
        is_open_tpsl=is_open_tpsl,
        is_set_open_sl=is_set_open_sl,
        is_set_open_tp=is_set_open_tp,
        open_sl_param=open_sl_param if isinstance(open_sl_param, Mapping) else None,
        open_tp_param=open_tp_param if isinstance(open_tp_param, Mapping) else None,
        trigger_price=trigger_price,
        order_type=str(order_type) if order_type is not None else None,
        status=str(status) if status is not None else None,
        created_at=created_at,
        raw=dict(raw),
    )


def _pick(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _normalize_side(value: Any) -> str:
    if value is None:
        raise ValueError("Missing side")
    text = str(value).strip().upper()
    if text in {"BUY", "LONG"}:
        return "BUY"
    if text in {"SELL", "SHORT"}:
        return "SELL"
    raise ValueError(f"Unknown side: {value}")


def _to_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid numeric field") from exc


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return default


def _parse_timestamp(value: Any) -> datetime:
    if value is None:
        raise ValueError("Missing timestamp")
    if isinstance(value, (int, float)):
        return _timestamp_from_number(float(value))
    text = str(value).strip()
    try:
        numeric = float(text)
        return _timestamp_from_number(numeric)
    except ValueError:
        pass
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _timestamp_from_number(value: float) -> datetime:
    seconds = value / 1000.0 if value > 1e12 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
