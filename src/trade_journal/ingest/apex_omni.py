from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from trade_journal.models import Fill


@dataclass(frozen=True)
class IngestResult:
    fills: list[Fill]
    skipped: int = 0


def load_fills(
    path: str | Path, *, source: str | None = None, account_id: str | None = None
) -> IngestResult:
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix == ".json":
        return _load_fills_json(source_path, source_name=source, account_id=account_id)
    if suffix in {".csv", ".tsv"}:
        return _load_fills_csv(
            source_path,
            delimiter="\t" if suffix == ".tsv" else ",",
            source_name=source,
            account_id=account_id,
        )
    raise ValueError(f"Unsupported file type: {source_path.suffix}")


def load_fills_payload(
    payload: Any, *, source: str | None = None, account_id: str | None = None
) -> IngestResult:
    records = _extract_records(payload)
    fills, skipped = _normalize_records(records, source_name=source, account_id=account_id)
    return IngestResult(fills=fills, skipped=skipped)


def _load_fills_json(
    path: Path, *, source_name: str | None, account_id: str | None
) -> IngestResult:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    records = _extract_records(payload)
    fills, skipped = _normalize_records(records, source_name=source_name, account_id=account_id)
    return IngestResult(fills=fills, skipped=skipped)


def _load_fills_csv(
    path: Path, delimiter: str, *, source_name: str | None, account_id: str | None
) -> IngestResult:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        fills, skipped = _normalize_records(reader, source_name=source_name, account_id=account_id)
    return IngestResult(fills=fills, skipped=skipped)


def _extract_records(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "fills", "result"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        if "data" in payload and isinstance(payload["data"], dict):
            data = payload["data"]
            for key in ("fills", "list", "orders"):
                if key in data and isinstance(data[key], list):
                    return data[key]
    raise ValueError("Unsupported JSON format for fills payload")


def _normalize_records(
    records: Iterable[Mapping[str, Any]],
    *,
    source_name: str | None,
    account_id: str | None,
) -> tuple[list[Fill], int]:
    fills: list[Fill] = []
    skipped = 0
    for raw in records:
        try:
            fills.append(_normalize_fill(raw, source_name=source_name, account_id=account_id))
        except ValueError:
            skipped += 1
    return fills, skipped


def _normalize_fill(
    raw: Mapping[str, Any], *, source_name: str | None, account_id: str | None
) -> Fill:
    status = _pick(raw, "status", "fillStatus", "orderStatus")
    if status is not None and not _is_success_status(status):
        raise ValueError("Non-success fill status")
    fill_id = _pick(raw, "id", "fill_id", "fillId", "matchFillId")
    order_id = _pick(raw, "order_id", "orderId")
    resolved_account = account_id or _pick(raw, "accountId", "account_id")
    symbol = _pick(raw, "symbol", "market", "instrument")
    side = _normalize_side(_pick(raw, "side", "direction", "tradeSide"))
    price = _to_float(_pick(raw, "price", "fill_price", "avg_price", "latestMatchFillPrice"))
    size = _to_float(_pick(raw, "size", "qty", "quantity", "filled_qty", "cumMatchFillSize", "cumSuccessFillSize"))
    fee = _to_float(_pick(raw, "fee", "fees", "commission", "cumMatchFillFee", "cumSuccessFillFee"), default=0.0)
    fee_asset = _pick(raw, "fee_asset", "feeAsset", "commissionAsset", "feeCurrency")
    timestamp = _parse_timestamp(_pick(raw, "timestamp", "time", "created_at", "transactTime", "createdAt", "updatedTime"))

    if not symbol or not side:
        raise ValueError("Missing required fill fields")

    return Fill(
        fill_id=str(fill_id) if fill_id is not None else None,
        order_id=str(order_id) if order_id is not None else None,
        symbol=str(symbol),
        side=side,
        price=price,
        size=size,
        fee=fee,
        fee_asset=str(fee_asset) if fee_asset is not None else None,
        timestamp=timestamp,
        source=str(source_name or "apex"),
        account_id=str(resolved_account) if resolved_account is not None else None,
        raw=dict(raw),
    )


def _is_success_status(value: Any) -> bool:
    text = str(value).strip().upper()
    if "SUCCESS" in text:
        return True
    if "FILLED" in text:
        return True
    return False


def _pick(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _normalize_side(value: Any) -> str:
    if value is None:
        raise ValueError("Missing side")
    text = str(value).strip().upper()
    if text in {"BUY", "B", "LONG"}:
        return "BUY"
    if text in {"SELL", "S", "SHORT"}:
        return "SELL"
    raise ValueError(f"Unknown side: {value}")


def _to_float(value: Any, default: float | None = None) -> float:
    if value is None:
        if default is None:
            raise ValueError("Missing numeric field")
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid numeric field") from exc


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

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Unsupported timestamp format") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _timestamp_from_number(value: float) -> datetime:
    seconds = value / 1000.0 if value > 1e12 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
