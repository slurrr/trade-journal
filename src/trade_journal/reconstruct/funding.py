from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from trade_journal.models import FundingEvent, Trade


@dataclass(frozen=True)
class FundingAttribution:
    event: FundingEvent
    matched_trade_id: str | None


def apply_funding_events(trades: Iterable[Trade], events: Iterable[FundingEvent]) -> list[FundingAttribution]:
    trades_by_symbol: dict[str, list[Trade]] = {}
    trade_list = list(trades)
    for trade in trade_list:
        trade.funding_fees = 0.0
        trades_by_symbol.setdefault(trade.symbol, []).append(trade)

    for trade_group in trades_by_symbol.values():
        trade_group.sort(key=lambda t: t.entry_time)

    attributions: list[FundingAttribution] = []
    for event in sorted(events, key=_event_sort_key):
        matched = _find_trade_for_event(trades_by_symbol.get(event.symbol, []), event)
        if matched is not None:
            matched.funding_fees += event.funding_value
        attributions.append(
            FundingAttribution(event=event, matched_trade_id=matched.trade_id if matched else None)
        )
    return attributions


def _event_sort_key(event: FundingEvent) -> datetime:
    return event.funding_time


def _find_trade_for_event(trades: list[Trade], event: FundingEvent) -> Trade | None:
    for trade in trades:
        if trade.side != event.side:
            continue
        if trade.entry_time <= event.funding_time <= trade.exit_time:
            return trade
    return None
