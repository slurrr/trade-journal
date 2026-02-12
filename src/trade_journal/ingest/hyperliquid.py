from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from trade_journal.ingest.apex_orders import OrderRecord, OrdersIngestResult
from trade_journal.models import Fill, FundingEvent


@dataclass(frozen=True)
class IngestResult:
    fills: list[Fill]
    skipped: int = 0


@dataclass(frozen=True)
class FundingIngestResult:
    events: list[FundingEvent]
    skipped: int = 0


def load_hyperliquid_fills_payload(
    payload: Any, *, source: str = "hyperliquid", account_id: str | None = None
) -> IngestResult:
    records = _extract_fill_records(payload)
    fills: list[Fill] = []
    skipped = 0
    for raw in records:
        try:
            fills.append(_normalize_fill(raw, source=source, account_id=account_id))
        except ValueError:
            skipped += 1
    return IngestResult(fills=fills, skipped=skipped)


def load_hyperliquid_funding_payload(
    payload: Any,
    *,
    source: str = "hyperliquid",
    account_id: str | None = None,
) -> FundingIngestResult:
    records = _extract_fill_records(payload)
    events: list[FundingEvent] = []
    skipped = 0
    for raw in records:
        try:
            events.append(_normalize_funding(raw, source=source, account_id=account_id))
        except ValueError:
            skipped += 1
    return FundingIngestResult(events=events, skipped=skipped)


def load_hyperliquid_clearinghouse_state_payload(
    payload: Any, *, source: str = "hyperliquid", account_id: str | None = None
) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    timestamp = datetime.now(timezone.utc).isoformat()
    margin_summary = payload.get("marginSummary")
    cross_margin_summary = payload.get("crossMarginSummary")
    return {
        "account_id": account_id,
        "source": source,
        "timestamp": timestamp,
        "total_equity": _coalesce_float(
            _first_float(payload, "accountValue", "totalEquity"),
            _first_float(margin_summary, "accountValue"),
            _first_float(cross_margin_summary, "accountValue"),
        ),
        "available_balance": _coalesce_float(
            _first_float(payload, "withdrawable"),
            _first_float(margin_summary, "withdrawable"),
            _first_float(cross_margin_summary, "withdrawable"),
        ),
        "margin_balance": _coalesce_float(
            _first_float(margin_summary, "totalMarginUsed"),
            _first_float(cross_margin_summary, "totalMarginUsed"),
        ),
        "raw_json": dict(payload),
    }


def load_hyperliquid_open_orders_payload(
    payload: Any, *, source: str = "hyperliquid", account_id: str | None = None
) -> OrdersIngestResult:
    records = _extract_fill_records(payload)
    orders: list[OrderRecord] = []
    skipped = 0
    for raw in records:
        try:
            orders.append(_normalize_open_order(raw, source=source, account_id=account_id))
        except ValueError:
            skipped += 1
    return OrdersIngestResult(orders=orders, skipped=skipped)


def load_hyperliquid_historical_orders_payload(
    payload: Any, *, source: str = "hyperliquid", account_id: str | None = None
) -> OrdersIngestResult:
    records = _extract_fill_records(payload)
    orders: list[OrderRecord] = []
    skipped = 0
    for raw in records:
        try:
            orders.append(
                _normalize_open_order(
                    raw,
                    source=source,
                    account_id=account_id,
                    prefer_status_timestamp=True,
                )
            )
        except ValueError:
            skipped += 1
    return OrdersIngestResult(orders=orders, skipped=skipped)


def _extract_fill_records(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, Mapping)]
    return []


def _normalize_fill(raw: Mapping[str, Any], *, source: str, account_id: str | None) -> Fill:
    fill_id = raw.get("tid")
    symbol = _symbol_from_coin(raw.get("coin"))
    side = _normalize_side(raw.get("side"))
    price = _to_float(raw.get("px"))
    size = _to_float(raw.get("sz"))
    fee = _to_float(raw.get("fee"), default=0.0)
    fee_asset = raw.get("feeToken")
    timestamp = _parse_timestamp(raw.get("time"))
    order_id = raw.get("oid")
    return Fill(
        fill_id=str(fill_id) if fill_id is not None else None,
        order_id=str(order_id) if order_id is not None else None,
        symbol=symbol,
        side=side,
        price=price,
        size=size,
        fee=fee,
        fee_asset=str(fee_asset) if fee_asset not in (None, "") else None,
        timestamp=timestamp,
        source=source,
        account_id=account_id,
        raw=dict(raw),
    )


def _normalize_open_order(
    raw: Mapping[str, Any],
    *,
    source: str,
    account_id: str | None,
    prefer_status_timestamp: bool = False,
) -> OrderRecord:
    payload = raw
    nested = raw.get("order")
    if isinstance(nested, Mapping):
        payload = nested
    trigger = payload.get("trigger")
    trigger_map = trigger if isinstance(trigger, Mapping) else {}

    order_id = payload.get("oid") or payload.get("orderId") or payload.get("id")
    symbol = _symbol_from_coin(payload.get("coin") or payload.get("symbol"))
    side = _normalize_side(payload.get("side"))
    size = _to_float(payload.get("sz") or payload.get("size"))
    price = _to_float(payload.get("limitPx") or payload.get("px") or payload.get("price"), default=None)
    trigger_price = _to_float(
        payload.get("triggerPx")
        or payload.get("triggerPrice")
        or payload.get("stopPx")
        or trigger_map.get("triggerPx")
        or trigger_map.get("triggerPrice"),
        default=None,
    )
    reduce_only = _to_bool(
        payload.get("reduceOnly") if "reduceOnly" in payload else raw.get("reduceOnly"),
        default=trigger_price is not None,
    )
    status_ts = raw.get("statusTimestamp") or payload.get("statusTimestamp")
    created_ts = (
        payload.get("time")
        or payload.get("timestamp")
        or payload.get("createdAt")
        or raw.get("time")
        or raw.get("timestamp")
    )
    created_at = _parse_timestamp(
        status_ts if prefer_status_timestamp and status_ts not in (None, "") else created_ts,
        default_now=True,
    )
    order_type = _order_type(payload, trigger_price)
    return OrderRecord(
        order_id=str(order_id) if order_id is not None else None,
        client_order_id=(
            str(payload.get("cloid"))
            if payload.get("cloid") not in (None, "")
            else (str(raw.get("cloid")) if raw.get("cloid") not in (None, "") else None)
        ),
        source=source,
        account_id=account_id,
        symbol=symbol,
        side=side,
        size=size,
        price=price,
        reduce_only=reduce_only,
        is_position_tpsl=_to_bool(
            payload.get("isPositionTpsl")
            if "isPositionTpsl" in payload
            else raw.get("isPositionTpsl"),
            default=trigger_price is not None,
        ),
        is_open_tpsl=_to_bool(
            payload.get("isOpenTpslOrder")
            if "isOpenTpslOrder" in payload
            else raw.get("isOpenTpslOrder"),
            default=False,
        ),
        is_set_open_sl=False,
        is_set_open_tp=False,
        open_sl_param=None,
        open_tp_param=None,
        trigger_price=trigger_price,
        order_type=order_type,
        status=(
            str(payload.get("status"))
            if payload.get("status") not in (None, "")
            else (str(raw.get("status")) if raw.get("status") not in (None, "") else "OPEN")
        ),
        created_at=created_at,
        raw=dict(raw),
    )


def _normalize_funding(
    raw: Mapping[str, Any],
    *,
    source: str,
    account_id: str | None,
) -> FundingEvent:
    event_time = _parse_timestamp(raw.get("time"))
    hash_id = raw.get("hash")
    delta = raw.get("delta")
    if not isinstance(delta, Mapping):
        raise ValueError("Missing funding delta object")
    if str(delta.get("type") or "").strip().lower() != "funding":
        raise ValueError("Not a funding delta row")
    coin = delta.get("coin")
    if coin in (None, ""):
        raise ValueError("Missing coin")
    symbol = _symbol_from_coin(coin)
    funding_rate = _to_float(delta.get("fundingRate"), default=0.0)
    size_signed = _to_float(delta.get("szi"), default=0.0)
    side = "LONG" if size_signed >= 0 else "SHORT"
    position_size = abs(size_signed)
    funding_value = _to_float(delta.get("usdc"), default=0.0)

    denom = abs(size_signed) * abs(funding_rate)
    if denom > 0:
        price = abs(funding_value) / denom
    else:
        price = 0.0

    funding_id = None
    if hash_id not in (None, ""):
        funding_id = f"{hash_id}:{int(event_time.timestamp() * 1000)}:{str(coin).upper()}"
    else:
        funding_id = f"fallback:{int(event_time.timestamp() * 1000)}:{str(coin).upper()}"

    return FundingEvent(
        funding_id=funding_id,
        transaction_id=str(hash_id) if hash_id not in (None, "") else None,
        symbol=symbol,
        side=side,
        rate=funding_rate,
        position_size=position_size,
        price=price,
        funding_time=event_time,
        funding_value=funding_value,
        status="funding",
        source=source,
        account_id=account_id,
        raw=dict(raw),
    )


def _symbol_from_coin(value: Any) -> str:
    if value in (None, ""):
        raise ValueError("Missing Hyperliquid coin")
    return f"{str(value).upper()}-USDC"


def _normalize_side(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"B", "BUY", "LONG"}:
        return "BUY"
    if text in {"A", "S", "SELL", "SHORT"}:
        return "SELL"
    raise ValueError(f"Unsupported Hyperliquid fill side: {value!r}")


def _to_float(value: Any, default: float | None = None) -> float:
    if value in (None, ""):
        if default is None:
            raise ValueError("Missing numeric value")
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid numeric value") from exc


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return default


def _order_type(raw: Mapping[str, Any], trigger_price: float | None) -> str | None:
    explicit = raw.get("orderType") or raw.get("type") or raw.get("t")
    if explicit not in (None, ""):
        return str(explicit)
    if trigger_price is not None:
        return "STOP"
    return None


def _parse_timestamp(value: Any, *, default_now: bool = False) -> datetime:
    if value in (None, ""):
        if default_now:
            return datetime.now(timezone.utc)
        raise ValueError("Missing fill timestamp")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    if numeric > 1e12:
        return datetime.fromtimestamp(numeric / 1000.0, tz=timezone.utc)
    return datetime.fromtimestamp(numeric, tz=timezone.utc)


def _first_float(payload: Any, *keys: str) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _coalesce_float(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None
