from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable
from uuid import uuid4

from trade_journal.models import Fill, Trade


@dataclass
class PositionState:
    source: str
    account_id: str | None
    symbol: str
    size: float = 0.0
    avg_entry_price: float = 0.0
    entry_time: datetime | None = None
    entry_qty_total: float = 0.0
    entry_notional: float = 0.0
    exit_qty_total: float = 0.0
    exit_notional: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0
    max_size: float = 0.0
    side: str | None = None
    fills: list[Fill] = field(default_factory=list)


EPSILON = 1e-9


def reconstruct_trades(fills: Iterable[Fill]) -> list[Trade]:
    ordered = sorted(fills, key=_sort_key)
    # Assumes one-way position mode per symbol; hedge-mode would need separate buckets.
    states: dict[tuple[str, str | None, str], PositionState] = {}
    trades: list[Trade] = []

    for fill in ordered:
        if fill.size == 0:
            continue
        key = (fill.source, fill.account_id, fill.symbol)
        state = states.get(key)
        if state is None:
            state = PositionState(source=fill.source, account_id=fill.account_id, symbol=fill.symbol)
            states[key] = state
        _apply_fill_to_state(state, fill, trades)

    return trades


def _sort_key(fill: Fill) -> tuple:
    tie = fill.fill_id or fill.order_id or ""
    return (fill.source, fill.account_id or "", fill.timestamp, tie)


def _apply_fill_to_state(state: PositionState, fill: Fill, trades: list[Trade]) -> None:
    signed_qty = fill.size if fill.side == "BUY" else -fill.size

    if abs(state.size) < EPSILON:
        _start_position(state, fill, signed_qty)
        return

    if state.size * signed_qty > 0:
        _add_to_position(state, fill, signed_qty)
        return

    _reduce_or_reverse(state, fill, signed_qty, trades)


def _start_position(state: PositionState, fill: Fill, signed_qty: float) -> None:
    state.size = signed_qty
    state.avg_entry_price = fill.price
    state.entry_time = fill.timestamp
    state.entry_qty_total = abs(signed_qty)
    state.entry_notional = fill.price * abs(signed_qty)
    state.exit_qty_total = 0.0
    state.exit_notional = 0.0
    state.realized_pnl = 0.0
    state.fees = fill.fee
    state.max_size = abs(signed_qty)
    state.side = "LONG" if signed_qty > 0 else "SHORT"
    state.fills = [fill]


def _add_to_position(state: PositionState, fill: Fill, signed_qty: float) -> None:
    new_abs = abs(state.size) + abs(signed_qty)
    state.avg_entry_price = (
        state.avg_entry_price * abs(state.size) + fill.price * abs(signed_qty)
    ) / new_abs
    state.size += signed_qty
    state.entry_qty_total += abs(signed_qty)
    state.entry_notional += fill.price * abs(signed_qty)
    state.fees += fill.fee
    state.max_size = max(state.max_size, abs(state.size))
    state.fills.append(fill)


def _reduce_or_reverse(state: PositionState, fill: Fill, signed_qty: float, trades: list[Trade]) -> None:
    close_qty = min(abs(signed_qty), abs(state.size))
    direction = 1.0 if state.size > 0 else -1.0
    state.realized_pnl += (fill.price - state.avg_entry_price) * close_qty * direction
    state.exit_qty_total += close_qty
    state.exit_notional += fill.price * close_qty

    fee_per_unit = fill.fee / abs(signed_qty) if abs(signed_qty) else 0.0
    close_fee = fee_per_unit * close_qty
    state.fees += close_fee

    if close_qty != abs(signed_qty):
        close_fill = _slice_fill(fill, close_qty, close_fee, "-close", "close")
        state.fills.append(close_fill)
    else:
        state.fills.append(fill)

    remaining = abs(signed_qty) - close_qty
    if remaining < EPSILON:
        remaining = 0.0
    if remaining == 0:
        state.size += signed_qty
        if abs(state.size) < EPSILON:
            _finalize_trade(state, fill.timestamp, trades)
            _reset_state(state)
        return

    exit_time = fill.timestamp
    _finalize_trade(state, exit_time, trades)
    _reset_state(state)

    open_qty = remaining
    open_fee = fee_per_unit * open_qty
    open_fill = _slice_fill(fill, open_qty, open_fee, "-open", "reverse")
    signed_open = open_qty if signed_qty > 0 else -open_qty
    _start_position(state, open_fill, signed_open)


def _finalize_trade(state: PositionState, exit_time: datetime, trades: list[Trade]) -> None:
    if state.entry_time is None or state.side is None:
        return

    entry_price = (
        state.entry_notional / state.entry_qty_total
        if state.entry_qty_total
        else state.avg_entry_price
    )
    exit_price = (
        state.exit_notional / state.exit_qty_total
        if state.exit_qty_total
        else state.avg_entry_price
    )

    trade = Trade(
        trade_id=str(uuid4()),
        source=state.source,
        account_id=state.account_id,
        symbol=state.symbol,
        side=state.side,
        entry_time=state.entry_time,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        entry_size=state.entry_qty_total,
        exit_size=state.exit_qty_total,
        max_size=state.max_size,
        realized_pnl=state.realized_pnl,
        fees=state.fees,
        fills=list(state.fills),
    )
    trades.append(trade)


def _reset_state(state: PositionState) -> None:
    state.size = 0.0
    state.avg_entry_price = 0.0
    state.entry_time = None
    state.entry_qty_total = 0.0
    state.entry_notional = 0.0
    state.exit_qty_total = 0.0
    state.exit_notional = 0.0
    state.realized_pnl = 0.0
    state.fees = 0.0
    state.max_size = 0.0
    state.side = None
    state.fills = []


def _slice_fill(fill: Fill, size: float, fee: float, suffix: str, reason: str) -> Fill:
    raw = dict(fill.raw)
    raw["_split_reason"] = reason
    raw["_split_size"] = size
    raw["_split_fee"] = fee
    fill_id = f"{fill.fill_id}{suffix}" if fill.fill_id else None
    return Fill(
        fill_id=fill_id,
        order_id=fill.order_id,
        symbol=fill.symbol,
        side=fill.side,
        price=fill.price,
        size=size,
        fee=fee,
        fee_asset=fill.fee_asset,
        timestamp=fill.timestamp,
        source=fill.source,
        account_id=fill.account_id,
        raw=raw,
    )
