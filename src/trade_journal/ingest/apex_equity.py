from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from trade_journal.models import EquitySnapshot


@dataclass(frozen=True)
class EquityHistoryResult:
    snapshots: list[EquitySnapshot]
    skipped: int = 0


def load_equity_history(
    path: str | Path,
    *,
    source: str | None = None,
    account_id: str | None = None,
    min_value: float | None = 0.0,
) -> EquityHistoryResult:
    source_path = Path(path)
    with source_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    records = _extract_records(payload)
    snapshots, skipped = _normalize_records(
        records, source_name=source, account_id=account_id, min_value=min_value
    )
    return EquityHistoryResult(snapshots=snapshots, skipped=skipped)


def load_equity_history_payload(
    payload: Any, *, source: str | None = None, account_id: str | None = None, min_value: float | None = 0.0
) -> EquityHistoryResult:
    records = _extract_records(payload)
    snapshots, skipped = _normalize_records(
        records, source_name=source, account_id=account_id, min_value=min_value
    )
    return EquityHistoryResult(snapshots=snapshots, skipped=skipped)


def _extract_records(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict):
            data = payload["data"]
            for key in ("historyValues", "equity_history", "equityHistory", "history"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        for key in ("historyValues", "equity_history", "equityHistory", "history"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
    raise ValueError("Unsupported JSON format for equity history payload")


def _normalize_records(
    records: Iterable[Mapping[str, Any]],
    *,
    source_name: str | None,
    account_id: str | None,
    min_value: float | None,
) -> tuple[list[EquitySnapshot], int]:
    snapshots: list[EquitySnapshot] = []
    skipped = 0
    for raw in records:
        try:
            snapshot = _normalize_record(
                raw, source_name=source_name, account_id=account_id, min_value=min_value
            )
        except ValueError:
            skipped += 1
            continue
        snapshots.append(snapshot)
    snapshots.sort(key=lambda item: item.timestamp)
    return snapshots, skipped


def _normalize_record(
    raw: Mapping[str, Any],
    *,
    source_name: str | None,
    account_id: str | None,
    min_value: float | None,
) -> EquitySnapshot:
    timestamp = _parse_timestamp(_pick(raw, "dateTime", "timestamp", "ts", "time"))
    total_value = _to_float(
        _pick(raw, "accountTotalValue", "totalValue", "total_value", "equity", "balance")
    )
    if min_value is not None and total_value <= min_value:
        raise ValueError("Equity below minimum threshold")
    return EquitySnapshot(
        timestamp=timestamp,
        total_value=total_value,
        source=str(source_name or "apex"),
        account_id=str(account_id) if account_id is not None else None,
        raw=dict(raw),
    )


def _pick(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _to_float(value: Any) -> float:
    if value is None:
        raise ValueError("Missing numeric field")
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
