from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from trade_journal.metrics.excursions import PriceBar
from trade_journal.models import Fill, Trade


@dataclass(frozen=True)
class TradeSeriesPoint:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    entry_return: float | None
    per_unit_unrealized: float | None


def compute_trade_series(trade: Trade, bars: Iterable[PriceBar]) -> list[TradeSeriesPoint]:
    bar_list = list(bars)
    if not bar_list:
        return []
    bar_list.sort(key=lambda bar: bar.start_time)
    fills = sorted(trade.fills, key=lambda item: item.timestamp)

    direction = 1.0 if trade.side == "LONG" else -1.0
    size = 0.0
    avg_entry = 0.0
    fill_idx = 0
    points: list[TradeSeriesPoint] = []

    for bar in bar_list:
        if bar.end_time < trade.entry_time or bar.start_time > trade.exit_time:
            continue
        while fill_idx < len(fills) and fills[fill_idx].timestamp <= bar.end_time:
            size, avg_entry = _apply_fill_to_position(size, avg_entry, fills[fill_idx])
            fill_idx += 1

        entry_return = None
        per_unit_unrealized = None
        if size != 0 and avg_entry != 0:
            entry_return = (bar.close / avg_entry - 1.0) * direction
            per_unit_unrealized = (bar.close - avg_entry) * direction

        points.append(
            TradeSeriesPoint(
                timestamp=bar.end_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                entry_return=entry_return,
                per_unit_unrealized=per_unit_unrealized,
            )
        )

    return points


def downsample_series(points: list[TradeSeriesPoint], max_points: int | None) -> list[TradeSeriesPoint]:
    if max_points is None or max_points <= 0 or len(points) <= max_points:
        return points
    stride = max(1, len(points) // max_points)
    sampled = points[::stride]
    if sampled and sampled[-1].timestamp != points[-1].timestamp:
        sampled.append(points[-1])
    return sampled


def _apply_fill_to_position(size: float, avg_entry: float, fill: Fill) -> tuple[float, float]:
    signed_qty = fill.size if fill.side == "BUY" else -fill.size

    if size == 0:
        return signed_qty, fill.price

    if size * signed_qty > 0:
        new_abs = abs(size) + abs(signed_qty)
        avg_entry = (avg_entry * abs(size) + fill.price * abs(signed_qty)) / new_abs
        return size + signed_qty, avg_entry

    if abs(signed_qty) < abs(size):
        return size + signed_qty, avg_entry

    if abs(signed_qty) == abs(size):
        return 0.0, 0.0

    leftover = abs(signed_qty) - abs(size)
    new_size = leftover if signed_qty > 0 else -leftover
    return new_size, fill.price
