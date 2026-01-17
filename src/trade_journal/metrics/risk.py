from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from trade_journal.ingest.apex_orders import OrderRecord
from trade_journal.models import Fill, Trade


@dataclass(frozen=True)
class RiskSummary:
    stop_price: float | None
    risk_amount: float | None
    r_multiple: float | None
    source: str | None


def initial_stop_for_trade(trade: Trade, orders: Iterable[OrderRecord]) -> RiskSummary:
    symbol_orders = [order for order in orders if order.symbol == trade.symbol]
    entry_order_ids = _entry_order_ids(trade)

    for order_id in entry_order_ids:
        order = _find_order(symbol_orders, order_id)
        if order is None:
            continue
        stop_price = _stop_from_open_sl(order)
        if stop_price is not None:
            risk_amount = _risk_amount(trade, stop_price)
            r_multiple = _r_multiple(trade, risk_amount)
            return RiskSummary(stop_price=stop_price, risk_amount=risk_amount, r_multiple=r_multiple, source="open_sl")

    stop_order = _first_tpsl_stop(trade, symbol_orders)
    if stop_order is not None:
        stop_price = _stop_from_tpsl(stop_order)
        if stop_price is not None:
            risk_amount = _risk_amount(trade, stop_price)
            r_multiple = _r_multiple(trade, risk_amount)
            return RiskSummary(stop_price=stop_price, risk_amount=risk_amount, r_multiple=r_multiple, source="tpsl")

    return RiskSummary(stop_price=None, risk_amount=None, r_multiple=None, source=None)


def _entry_order_ids(trade: Trade) -> list[str]:
    entry_side = "BUY" if trade.side == "LONG" else "SELL"
    entry_fills = [fill for fill in trade.fills if fill.side == entry_side]
    entry_fills.sort(key=lambda fill: fill.timestamp)
    seen: set[str] = set()
    order_ids: list[str] = []
    for fill in entry_fills:
        if fill.order_id is None:
            continue
        if fill.order_id in seen:
            continue
        seen.add(fill.order_id)
        order_ids.append(fill.order_id)
    return order_ids


def _find_order(orders: list[OrderRecord], order_id: str) -> OrderRecord | None:
    for order in orders:
        if order.order_id == order_id or order.client_order_id == order_id:
            return order
    return None


def _stop_from_open_sl(order: OrderRecord) -> float | None:
    if not order.open_sl_param:
        return None
    if not order.is_set_open_sl:
        return None
    price = order.open_sl_param.get("price") or order.open_sl_param.get("triggerPrice")
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def _first_tpsl_stop(trade: Trade, orders: list[OrderRecord]) -> OrderRecord | None:
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
        order_type = (order.order_type or "").upper()
        if "STOP" not in order_type and order.trigger_price is None:
            continue
        candidates.append(order)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.created_at)
    return candidates[0]


def _stop_from_tpsl(order: OrderRecord) -> float | None:
    if order.trigger_price is None:
        return None
    return order.trigger_price


def _risk_amount(trade: Trade, stop_price: float) -> float | None:
    risk_per_unit = abs(trade.entry_price - stop_price)
    if risk_per_unit == 0:
        return None
    return risk_per_unit * trade.entry_size


def _r_multiple(trade: Trade, risk_amount: float | None) -> float | None:
    if not risk_amount:
        return None
    return trade.realized_pnl_net / risk_amount
