from __future__ import annotations

from typing import Iterable

from trade_journal.models import EquitySnapshot, Trade


def apply_equity_at_entry(
    trades: Iterable[Trade],
    snapshots: Iterable[EquitySnapshot],
    *,
    fallback_equity: float | None = None,
) -> None:
    ordered_trades = sorted(trades, key=lambda trade: trade.entry_time)
    ordered_snapshots = sorted(snapshots, key=lambda snap: snap.timestamp)
    if not ordered_trades:
        return
    idx = 0
    latest = None
    for trade in ordered_trades:
        while idx < len(ordered_snapshots) and ordered_snapshots[idx].timestamp <= trade.entry_time:
            latest = ordered_snapshots[idx]
            idx += 1
        if latest is not None:
            trade.equity_at_entry = latest.total_value
        else:
            trade.equity_at_entry = fallback_equity
