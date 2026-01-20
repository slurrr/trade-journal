from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from trade_journal.models import FundingEvent


@dataclass(frozen=True)
class FundingIngestResult:
    events: list[FundingEvent]
    skipped: int = 0


def load_funding(
    path: str | Path, *, source: str | None = None, account_id: str | None = None
) -> FundingIngestResult:
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix == ".json":
        return _load_funding_json(source_path, source_name=source, account_id=account_id)
    if suffix in {".csv", ".tsv"}:
        return _load_funding_csv(
            source_path,
            delimiter="\t" if suffix == ".tsv" else ",",
            source_name=source,
            account_id=account_id,
        )
    raise ValueError(f"Unsupported file type: {source_path.suffix}")


def _load_funding_json(
    path: Path, *, source_name: str | None, account_id: str | None
) -> FundingIngestResult:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    records = _extract_records(payload)
    events, skipped = _normalize_records(records, source_name=source_name, account_id=account_id)
    return FundingIngestResult(events=events, skipped=skipped)


def _load_funding_csv(
    path: Path, delimiter: str, *, source_name: str | None, account_id: str | None
) -> FundingIngestResult:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        events, skipped = _normalize_records(reader, source_name=source_name, account_id=account_id)
    return FundingIngestResult(events=events, skipped=skipped)


def _extract_records(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "funding", "result"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        if "data" in payload and isinstance(payload["data"], dict):
            data = payload["data"]
            for key in ("funding", "fundingValues", "list", "records"):
                if key in data and isinstance(data[key], list):
                    return data[key]
    raise ValueError("Unsupported JSON format for funding payload")


def _normalize_records(
    records: Iterable[Mapping[str, Any]],
    *,
    source_name: str | None,
    account_id: str | None,
) -> tuple[list[FundingEvent], int]:
    events: list[FundingEvent] = []
    skipped = 0
    for raw in records:
        try:
            events.append(_normalize_event(raw, source_name=source_name, account_id=account_id))
        except ValueError:
            skipped += 1
    return events, skipped


def _normalize_event(
    raw: Mapping[str, Any], *, source_name: str | None, account_id: str | None
) -> FundingEvent:
    funding_id = _pick(raw, "id", "fundingId")
    transaction_id = _pick(raw, "transactionId", "txId")
    resolved_account = account_id or _pick(raw, "accountId", "account_id")
    symbol = _pick(raw, "symbol", "market", "instrument")
    side = _normalize_side(_pick(raw, "side", "positionSide"))
    rate = _to_float(_pick(raw, "rate", "fundingRate"), default=0.0)
    position_size = _to_float(_pick(raw, "positionSize", "size", "qty"), default=0.0)
    price = _to_float(_pick(raw, "price", "markPrice"), default=0.0)
    funding_time = _parse_timestamp(_pick(raw, "fundingTime", "timestamp", "time"))
    funding_value = _to_float(_pick(raw, "fundingValue", "value", "amount"), default=0.0)
    status = _pick(raw, "status")

    if not symbol or not side:
        raise ValueError("Missing required funding fields")

    return FundingEvent(
        funding_id=str(funding_id) if funding_id is not None else None,
        transaction_id=str(transaction_id) if transaction_id is not None else None,
        symbol=str(symbol),
        side=side,
        rate=rate,
        position_size=position_size,
        price=price,
        funding_time=funding_time,
        funding_value=funding_value,
        status=str(status) if status is not None else None,
        source=str(source_name or "apex"),
        account_id=str(resolved_account) if resolved_account is not None else None,
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
    if text in {"LONG", "L"}:
        return "LONG"
    if text in {"SHORT", "S"}:
        return "SHORT"
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
