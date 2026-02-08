from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from trade_journal.ingest.apex_orders import OrderRecord
from trade_journal.models import Trade


@dataclass(frozen=True)
class TargetSummary:
    target_price: float | None
    target_pnl: float | None
    source: str | None


def initial_target_for_trade(trade: Trade, orders: Iterable[OrderRecord]) -> TargetSummary:
    symbol_orders = [
        order
        for order in orders
        if order.symbol == trade.symbol
        and order.source == trade.source
        and order.account_id == trade.account_id
    ]
    target_price, size_with_target, total_entry_size = _weighted_open_tp_target(trade, symbol_orders)
    if target_price is not None:
        size_for_target = total_entry_size
        source = "open_tp_weighted"
        if total_entry_size <= 0 or abs(total_entry_size - size_with_target) > 1e-9:
            size_for_target = size_with_target
            source = "open_tp_weighted_partial"
        target_pnl = _target_pnl(trade, target_price, size_for_target)
        return TargetSummary(target_price=target_price, target_pnl=target_pnl, source=source)

    target_order = _first_tpsl_target(trade, symbol_orders)
    if target_order is not None:
        target_price = _target_from_tpsl(target_order)
        if target_price is not None:
            target_pnl = _target_pnl(trade, target_price, trade.entry_size)
            return TargetSummary(target_price=target_price, target_pnl=target_pnl, source="tpsl")

    return TargetSummary(target_price=None, target_pnl=None, source=None)


def _weighted_open_tp_target(
    trade: Trade,
    orders: list[OrderRecord],
) -> tuple[float | None, float, float]:
    entry_side = "BUY" if trade.side == "LONG" else "SELL"
    weighted_sum = 0.0
    size_sum = 0.0
    total_entry_size = 0.0
    for fill in trade.fills:
        if fill.side != entry_side:
            continue
        total_entry_size += fill.size
        if fill.order_id is None:
            continue
        order = _find_order(orders, fill.order_id)
        if order is None:
            continue
        target_price = _target_from_open_tp(order)
        if target_price is None:
            continue
        weighted_sum += fill.size * target_price
        size_sum += fill.size
    if size_sum <= 0:
        return None, 0.0, total_entry_size
    return weighted_sum / size_sum, size_sum, total_entry_size


def _find_order(orders: list[OrderRecord], order_id: str) -> OrderRecord | None:
    for order in orders:
        if order.order_id == order_id or order.client_order_id == order_id:
            return order
    return None


def _target_from_open_tp(order: OrderRecord) -> float | None:
    if not order.open_tp_param:
        return None
    if not order.is_set_open_tp:
        return None
    price = order.open_tp_param.get("price") or order.open_tp_param.get("triggerPrice")
    if price is None:
        return None
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def _first_tpsl_target(trade: Trade, orders: list[OrderRecord]) -> OrderRecord | None:
    entry_time = trade.entry_time
    exit_time = trade.exit_time
    opposite_side = "SELL" if trade.side == "LONG" else "BUY"
    candidates = []
    for order in orders:
        if not order.is_position_tpsl:
            continue
        if not order.reduce_only:
            continue
        if order.created_at < entry_time or order.created_at > exit_time:
            continue
        if order.side != opposite_side:
            continue
        if not _is_target_order(order):
            continue
        candidates.append(order)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.created_at)
    return candidates[0]


def _is_target_order(order: OrderRecord) -> bool:
    if order.order_type:
        text = order.order_type.upper()
        if "TAKE" in text or "TP" in text:
            return True
        if "STOP" in text:
            return False
    return order.trigger_price is not None


def _target_from_tpsl(order: OrderRecord) -> float | None:
    if order.trigger_price is None:
        return None
    return order.trigger_price


def _target_pnl(trade: Trade, target_price: float, size: float) -> float | None:
    if size <= 0:
        return None
    if trade.side == "LONG":
        delta = target_price - trade.entry_price
    else:
        delta = trade.entry_price - target_price
    if delta <= 0:
        return None
    return delta * size
