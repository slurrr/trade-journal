from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from trade_journal.models import Fill, Trade


@dataclass(frozen=True)
class PriceSample:
    timestamp: datetime
    price: float


@dataclass(frozen=True)
class PriceBar:
    start_time: datetime
    end_time: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ExcursionMetrics:
    mae: float
    mfe: float
    etd: float


def compute_trade_excursions(trade: Trade, prices: Iterable[PriceSample]) -> ExcursionMetrics | None:
    samples = [sample for sample in prices if trade.entry_time <= sample.timestamp <= trade.exit_time]
    if not samples:
        return None

    fills = sorted(trade.fills, key=lambda item: item.timestamp)
    samples.sort(key=lambda item: item.timestamp)

    size = 0.0
    avg_entry = 0.0
    min_unrealized: float | None = None
    max_unrealized: float | None = None

    fill_idx = 0
    sample_idx = 0
    while fill_idx < len(fills) or sample_idx < len(samples):
        next_fill = fills[fill_idx] if fill_idx < len(fills) else None
        next_sample = samples[sample_idx] if sample_idx < len(samples) else None

        if next_fill and (next_sample is None or next_fill.timestamp < next_sample.timestamp):
            size, avg_entry = _apply_fill_to_position(size, avg_entry, next_fill)
            fill_idx += 1
            continue

        if next_sample is None:
            break

        if size != 0:
            unrealized = (next_sample.price - avg_entry) * size
            min_unrealized = unrealized if min_unrealized is None else min(min_unrealized, unrealized)
            max_unrealized = unrealized if max_unrealized is None else max(max_unrealized, unrealized)
        sample_idx += 1

    if min_unrealized is None or max_unrealized is None:
        return None

    etd = max_unrealized - trade.realized_pnl_net
    return ExcursionMetrics(mae=min_unrealized, mfe=max_unrealized, etd=etd)


def compute_trade_excursions_from_bars(trade: Trade, bars: Iterable[PriceBar]) -> ExcursionMetrics:
    bar_list = list(bars)
    samples = _bar_extremes_as_samples(bar_list)
    metrics = compute_trade_excursions(trade, samples)
    if metrics is None:
        if bar_list:
            earliest = min(bar.start_time for bar in bar_list)
            latest = max(bar.end_time for bar in bar_list)
            raise RuntimeError(
                "No price samples within trade window. "
                f"bars={earliest.isoformat()} -> {latest.isoformat()} "
                f"trade={trade.entry_time.isoformat()} -> {trade.exit_time.isoformat()}"
            )
        raise RuntimeError(
            "No price samples within trade window. "
            f"trade={trade.entry_time.isoformat()} -> {trade.exit_time.isoformat()}"
        )
    return metrics


def apply_trade_excursions(trade: Trade, bars: Iterable[PriceBar]) -> None:
    metrics = compute_trade_excursions_from_bars(trade, bars)
    trade.mae = metrics.mae
    trade.mfe = metrics.mfe
    trade.etd = metrics.etd


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


def _bar_extremes_as_samples(bars: Iterable[PriceBar]) -> list[PriceSample]:
    samples: list[PriceSample] = []
    for bar in bars:
        # Use bar end time so fills inside the bar update position before extremes are applied.
        # Note: bar highs/lows may include prices before a fill within the same bar.
        samples.append(PriceSample(timestamp=bar.end_time, price=bar.high))
        samples.append(PriceSample(timestamp=bar.end_time, price=bar.low))
    return samples
