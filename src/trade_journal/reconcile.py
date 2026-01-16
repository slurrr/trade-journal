from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from trade_journal.ingest.apex_omni import load_fills
from trade_journal.models import Trade
from trade_journal.reconstruct.trades import reconstruct_trades


@dataclass(frozen=True)
class PnlRecord:
    record_id: str | None
    symbol: str
    side: str
    size: float
    exit_time: datetime
    total_pnl: float
    raw: dict[str, Any]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile reconstructed trades with ApeX historical PnL.")
    parser.add_argument(
        "fills_path",
        type=Path,
        nargs="?",
        default=Path("data/fills.json"),
        help="Path to fills JSON file.",
    )
    parser.add_argument(
        "historical_pnl_path",
        type=Path,
        nargs="?",
        default=Path("data/historical_pnl.json"),
        help="Path to historical PnL JSON file.",
    )
    parser.add_argument("--window-seconds", type=int, default=900, help="Match window for exit time.")
    args = parser.parse_args(argv)

    fills = load_fills(args.fills_path).fills
    trades = reconstruct_trades(fills)
    pnl_records = load_historical_pnl(args.historical_pnl_path)

    matches = match_trades(trades, pnl_records, window_seconds=args.window_seconds)

    if not matches:
        print("No matches found.")
        return 0

    print("symbol side size trade_exit pnl_exit delta_pnl")
    for match in matches:
        trade = match.trade
        record = match.record
        delta = trade.realized_pnl_net - record.total_pnl
        print(
            f"{trade.symbol} {trade.side} {trade.max_size:.6g} "
            f"{trade.exit_time.isoformat()} {record.total_pnl:.6g} {delta:.6g}"
        )

    return 0


@dataclass(frozen=True)
class TradeMatch:
    trade: Trade
    record: PnlRecord


def load_historical_pnl(path: Path) -> list[PnlRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = _extract_records(payload)
    return [_normalize_record(record) for record in records]


def _extract_records(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict):
        for key in ("data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [record for record in value if isinstance(record, dict)]
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("list", "records", "pnl", "history", "historicalPnl"):
                value = data.get(key)
                if isinstance(value, list):
                    return [record for record in value if isinstance(record, dict)]
    return []


def _normalize_record(raw: dict[str, Any]) -> PnlRecord:
    record_id = str(raw.get("id")) if raw.get("id") is not None else None
    symbol = str(raw.get("symbol"))
    side = "LONG" if str(raw.get("side", "")).upper() in {"LONG", "BUY"} else "SHORT"
    size = _to_float(raw.get("size"))
    exit_time = _timestamp_ms(raw.get("createdAt"))
    total_pnl = _to_float(raw.get("totalPnl"))
    return PnlRecord(
        record_id=record_id,
        symbol=symbol,
        side=side,
        size=size,
        exit_time=exit_time,
        total_pnl=total_pnl,
        raw=raw,
    )


def match_trades(trades: list[Trade], records: list[PnlRecord], window_seconds: int) -> list[TradeMatch]:
    remaining = records[:]
    matches: list[TradeMatch] = []

    for trade in sorted(trades, key=lambda item: item.exit_time):
        candidate_index = _find_best_record(trade, remaining, window_seconds)
        if candidate_index is None:
            continue
        record = remaining.pop(candidate_index)
        matches.append(TradeMatch(trade=trade, record=record))

    return matches


def _find_best_record(trade: Trade, records: list[PnlRecord], window_seconds: int) -> int | None:
    best_index: int | None = None
    best_score: float | None = None

    for idx, record in enumerate(records):
        if trade.symbol != record.symbol or trade.side != record.side:
            continue
        if abs(trade.exit_size - record.size) > 1e-9:
            continue

        delta = abs((trade.exit_time - record.exit_time).total_seconds())
        if delta > window_seconds:
            continue

        score = delta
        if best_score is None or score < best_score:
            best_score = score
            best_index = idx

    return best_index


def _timestamp_ms(value: Any) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
