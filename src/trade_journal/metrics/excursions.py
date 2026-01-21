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
    realized = 0.0
    min_unrealized: float | None = None
    max_unrealized: float | None = None

    fill_idx = 0
    sample_idx = 0
    while fill_idx < len(fills) or sample_idx < len(samples):
        next_fill = fills[fill_idx] if fill_idx < len(fills) else None
        next_sample = samples[sample_idx] if sample_idx < len(samples) else None

        if next_fill and (next_sample is None or next_fill.timestamp < next_sample.timestamp):
            size, avg_entry, realized = _apply_fill_to_position(size, avg_entry, realized, next_fill)
            fill_idx += 1
            continue

        if next_sample is None:
            break

        if size != 0:
            unrealized = (next_sample.price - avg_entry) * size
            total_pnl = realized + unrealized
            min_unrealized = total_pnl if min_unrealized is None else min(min_unrealized, total_pnl)
            max_unrealized = total_pnl if max_unrealized is None else max(max_unrealized, total_pnl)
        sample_idx += 1

    if min_unrealized is None or max_unrealized is None:
        return None

    etd = max_unrealized - trade.realized_pnl
    return ExcursionMetrics(mae=min_unrealized, mfe=max_unrealized, etd=etd)


def compute_trade_excursions_from_bars(trade: Trade, bars: Iterable[PriceBar]) -> ExcursionMetrics:
    bar_list = list(bars)
    entry_price, exit_price = _boundary_fill_prices(trade)
    samples = _bar_extremes_as_samples_for_trade(
        bar_list,
        trade.entry_time,
        trade.exit_time,
        entry_price,
        exit_price,
    )
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


def _apply_fill_to_position(
    size: float, avg_entry: float, realized: float, fill: Fill
) -> tuple[float, float, float]:
    signed_qty = fill.size if fill.side == "BUY" else -fill.size

    if size == 0:
        return signed_qty, fill.price, realized

    if size * signed_qty > 0:
        new_abs = abs(size) + abs(signed_qty)
        avg_entry = (avg_entry * abs(size) + fill.price * abs(signed_qty)) / new_abs
        return size + signed_qty, avg_entry, realized

    if abs(signed_qty) < abs(size):
        direction = 1.0 if size > 0 else -1.0
        realized += (fill.price - avg_entry) * abs(signed_qty) * direction
        return size + signed_qty, avg_entry, realized

    if abs(signed_qty) == abs(size):
        direction = 1.0 if size > 0 else -1.0
        realized += (fill.price - avg_entry) * abs(signed_qty) * direction
        return 0.0, 0.0, realized

    leftover = abs(signed_qty) - abs(size)
    direction = 1.0 if size > 0 else -1.0
    realized += (fill.price - avg_entry) * abs(size) * direction
    new_size = leftover if signed_qty > 0 else -leftover
    return new_size, fill.price, realized


def _bar_extremes_as_samples_for_trade(
    bars: Iterable[PriceBar],
    entry_time: datetime,
    exit_time: datetime,
    entry_price: float,
    exit_price: float,
) -> list[PriceSample]:
    samples: list[PriceSample] = []
    for bar in bars:
        if bar.end_time < entry_time or bar.start_time > exit_time:
            continue
        overlaps_entry = bar.start_time < entry_time <= bar.end_time
        overlaps_exit = bar.start_time <= exit_time < bar.end_time

        if overlaps_entry and overlaps_exit:
            # Trade opens and closes inside the same bar; avoid pre/post extremes.
            samples.append(PriceSample(timestamp=entry_time, price=entry_price))
            samples.append(PriceSample(timestamp=exit_time, price=exit_price))
            continue

        if overlaps_entry:
            # Use entry price plus bar close to capture within-bar move after entry
            # without including pre-entry extremes.
            samples.append(PriceSample(timestamp=entry_time, price=entry_price))
            samples.append(PriceSample(timestamp=bar.end_time, price=bar.close))
            continue

        if overlaps_exit:
            # Use exit price only to avoid post-exit extremes in the exit bar.
            samples.append(PriceSample(timestamp=exit_time, price=exit_price))
            continue

        # Use bar end time so fills inside the bar update position before extremes are applied.
        sample_time = bar.end_time
        samples.append(PriceSample(timestamp=sample_time, price=bar.high))
        samples.append(PriceSample(timestamp=sample_time, price=bar.low))
    return samples


def _boundary_fill_prices(trade: Trade) -> tuple[float, float]:
    fills = sorted(trade.fills, key=lambda item: item.timestamp)
    entry_side = "BUY" if trade.side == "LONG" else "SELL"
    exit_side = "SELL" if trade.side == "LONG" else "BUY"

    entry_price = trade.entry_price
    exit_price = trade.exit_price

    for fill in fills:
        if fill.side == entry_side:
            entry_price = fill.price
            break

    for fill in reversed(fills):
        if fill.side == exit_side:
            exit_price = fill.price
            break

    return entry_price, exit_price
