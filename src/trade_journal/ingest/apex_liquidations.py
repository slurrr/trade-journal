from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class LiquidationEvent:
    liquidation_id: str | None
    symbol: str
    side: str
    size: float
    entry_price: float | None
    exit_price: float | None
    total_pnl: float | None
    fee: float | None
    liquidate_fee: float | None
    created_at: datetime
    exit_type: str | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class LiquidationIngestResult:
    events: list[LiquidationEvent]
    skipped: int = 0


def load_liquidations(path: str | Path) -> LiquidationIngestResult:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".json":
        return _load_liquidations_json(source)
    if suffix in {".csv", ".tsv"}:
        return _load_liquidations_csv(source, delimiter="\t" if suffix == ".tsv" else ",")
    raise ValueError(f"Unsupported file type: {source.suffix}")


def extract_liquidations(payload: Any) -> LiquidationIngestResult:
    records = _extract_records(payload)
    events, skipped = _normalize_records(records)
    return LiquidationIngestResult(events=events, skipped=skipped)


def _load_liquidations_json(path: Path) -> LiquidationIngestResult:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    records = _extract_records(payload)
    events, skipped = _normalize_records(records)
    return LiquidationIngestResult(events=events, skipped=skipped)


def _load_liquidations_csv(path: Path, delimiter: str) -> LiquidationIngestResult:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        events, skipped = _normalize_records(reader)
    return LiquidationIngestResult(events=events, skipped=skipped)


def _extract_records(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "liquidations", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("liquidations", "historicalPnl", "list", "records"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
    return []


def _normalize_records(records: Iterable[Mapping[str, Any]]) -> tuple[list[LiquidationEvent], int]:
    events: list[LiquidationEvent] = []
    skipped = 0
    for raw in records:
        if not _is_liquidation(raw):
            continue
        try:
            events.append(_normalize_event(raw))
        except ValueError:
            skipped += 1
    return events, skipped


def _normalize_event(raw: Mapping[str, Any]) -> LiquidationEvent:
    liquidation_id = _pick(raw, "id", "liquidationId")
    symbol = _pick(raw, "symbol", "market")
    side = _normalize_side(_pick(raw, "side"))
    size = _to_float(_pick(raw, "size", "qty", "positionSize"), default=None)
    entry_price = _to_float(_pick(raw, "price", "entryPrice", "entry_price"), default=None)
    exit_price = _to_float(_pick(raw, "exitPrice", "closePrice", "exit_price"), default=None)
    total_pnl = _to_float(_pick(raw, "totalPnl", "pnl", "total_pnl"), default=None)
    fee = _to_float(_pick(raw, "fee", "closeSharedOpenFee"), default=None)
    liquidate_fee = _to_float(_pick(raw, "liquidateFee", "liquidate_fee"), default=None)
    created_at = _parse_timestamp(_pick(raw, "createdAt", "timestamp", "time", "created_at"))
    exit_type = _pick(raw, "exitType", "type")

    if size is None:
        raise ValueError("Missing size")
    if not symbol or not side:
        raise ValueError("Missing required liquidation fields")

    return LiquidationEvent(
        liquidation_id=str(liquidation_id) if liquidation_id is not None else None,
        symbol=str(symbol),
        side=side,
        size=size,
        entry_price=entry_price,
        exit_price=exit_price,
        total_pnl=total_pnl,
        fee=fee,
        liquidate_fee=liquidate_fee,
        created_at=created_at,
        exit_type=str(exit_type) if exit_type is not None else None,
        raw=dict(raw),
    )


def _is_liquidation(raw: Mapping[str, Any]) -> bool:
    if "liquidation_id" in raw or "liquidationId" in raw:
        return True
    if raw.get("is_liquidate") in {True, "true", "True", 1, "1"}:
        return True
    if raw.get("isLiquidate") in {True, "true", "True", 1, "1"}:
        return True
    exit_type = str(raw.get("exitType", raw.get("exit_type", ""))).strip().lower()
    if exit_type in {"liquidate", "liquidation"}:
        return True
    nested_raw = raw.get("raw")
    if isinstance(nested_raw, Mapping) and nested_raw.get("isLiquidate") in {True, "true", "True", 1, "1"}:
        return True
    record_type = str(raw.get("type", "")).strip().lower()
    return record_type in {"liquidate", "liquidation"}


def _pick(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _normalize_side(value: Any) -> str:
    if value is None:
        raise ValueError("Missing side")
    text = str(value).strip().upper()
    if text in {"LONG", "BUY"}:
        return "LONG"
    if text in {"SHORT", "SELL"}:
        return "SHORT"
    raise ValueError(f"Unknown side: {value}")


def _to_float(value: Any, default: float | None) -> float | None:
    if value is None:
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
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _timestamp_from_number(value: float) -> datetime:
    seconds = value / 1000.0 if value > 1e12 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
