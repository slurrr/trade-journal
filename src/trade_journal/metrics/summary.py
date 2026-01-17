from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from trade_journal.models import Trade

Outcome = str

OUTCOME_WIN: Outcome = "win"
OUTCOME_LOSS: Outcome = "loss"
OUTCOME_BREAKEVEN: Outcome = "breakeven"

BREAKEVEN_BAND_PCT = 0.0033


@dataclass(frozen=True)
class TradeMetrics:
    trade_id: str
    symbol: str
    outcome: Outcome
    gross_pnl: float
    net_pnl: float
    entry_notional: float
    return_pct: float | None
    duration_seconds: float


@dataclass(frozen=True)
class AggregateMetrics:
    total_trades: int
    wins: int
    losses: int
    breakevens: int
    win_rate: float | None
    profit_factor: float | None
    expectancy: float | None
    avg_win: float | None
    avg_loss: float | None
    largest_win: float | None
    largest_loss: float | None
    max_consecutive_wins: int
    max_consecutive_losses: int
    total_gross_pnl: float
    total_net_pnl: float
    total_fees: float
    total_funding: float
    avg_duration_seconds: float | None
    total_duration_seconds: float
    mean_mae: float | None
    median_mae: float | None
    mean_mfe: float | None
    median_mfe: float | None
    mean_etd: float | None
    median_etd: float | None


def compute_trade_metrics(trade: Trade) -> TradeMetrics:
    net_pnl = trade.realized_pnl_net
    entry_notional = trade.entry_price * trade.entry_size
    return_pct = None
    if entry_notional:
        return_pct = net_pnl / entry_notional
    duration = trade.exit_time - trade.entry_time
    return TradeMetrics(
        trade_id=trade.trade_id,
        symbol=trade.symbol,
        outcome=classify_outcome(net_pnl, entry_notional),
        gross_pnl=trade.realized_pnl,
        net_pnl=net_pnl,
        entry_notional=entry_notional,
        return_pct=return_pct,
        duration_seconds=duration.total_seconds(),
    )


def classify_outcome(net_pnl: float, entry_notional: float) -> Outcome:
    band = 0.0
    if entry_notional > 0:
        band = entry_notional * BREAKEVEN_BAND_PCT
    if abs(net_pnl) <= band:
        return OUTCOME_BREAKEVEN
    return OUTCOME_WIN if net_pnl > 0 else OUTCOME_LOSS


def compute_aggregate_metrics(trades: Iterable[Trade]) -> AggregateMetrics:
    trade_list = list(trades)
    trade_metrics = [compute_trade_metrics(trade) for trade in trade_list]

    wins = [metric for metric in trade_metrics if metric.outcome == OUTCOME_WIN]
    losses = [metric for metric in trade_metrics if metric.outcome == OUTCOME_LOSS]
    breakevens = [metric for metric in trade_metrics if metric.outcome == OUTCOME_BREAKEVEN]

    win_count = len(wins)
    loss_count = len(losses)
    breakeven_count = len(breakevens)
    total_trades = len(trade_metrics)

    win_rate = None
    if win_count + loss_count:
        win_rate = win_count / (win_count + loss_count)

    total_win = sum(metric.net_pnl for metric in wins)
    total_loss = sum(metric.net_pnl for metric in losses)
    profit_factor = None
    if total_loss < 0:
        profit_factor = total_win / abs(total_loss)

    expectancy = None
    if total_trades:
        expectancy = sum(metric.net_pnl for metric in trade_metrics) / total_trades

    avg_win = None
    if win_count:
        avg_win = total_win / win_count

    avg_loss = None
    if loss_count:
        avg_loss = total_loss / loss_count

    largest_win = max((metric.net_pnl for metric in wins), default=None)
    largest_loss = min((metric.net_pnl for metric in losses), default=None)

    max_wins, max_losses = _max_streaks(trade_list)

    total_duration = sum(metric.duration_seconds for metric in trade_metrics)
    avg_duration = None
    if total_trades:
        avg_duration = total_duration / total_trades

    total_gross_pnl = sum(trade.realized_pnl for trade in trade_list)
    total_net_pnl = sum(trade.realized_pnl_net for trade in trade_list)
    total_fees = sum(trade.fees for trade in trade_list)
    total_funding = sum(trade.funding_fees for trade in trade_list)
    mae_values = [trade.mae for trade in trade_list if trade.mae is not None]
    mfe_values = [trade.mfe for trade in trade_list if trade.mfe is not None]
    etd_values = [trade.etd for trade in trade_list if trade.etd is not None]

    return AggregateMetrics(
        total_trades=total_trades,
        wins=win_count,
        losses=loss_count,
        breakevens=breakeven_count,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy=expectancy,
        avg_win=avg_win,
        avg_loss=avg_loss,
        largest_win=largest_win,
        largest_loss=largest_loss,
        max_consecutive_wins=max_wins,
        max_consecutive_losses=max_losses,
        total_gross_pnl=total_gross_pnl,
        total_net_pnl=total_net_pnl,
        total_fees=total_fees,
        total_funding=total_funding,
        avg_duration_seconds=avg_duration,
        total_duration_seconds=total_duration,
        mean_mae=_mean(mae_values),
        median_mae=_median(mae_values),
        mean_mfe=_mean(mfe_values),
        median_mfe=_median(mfe_values),
        mean_etd=_mean(etd_values),
        median_etd=_median(etd_values),
    )


def _max_streaks(trades: Iterable[Trade]) -> tuple[int, int]:
    ordered = sorted(trades, key=lambda trade: trade.exit_time)
    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0

    for trade in ordered:
        entry_notional = trade.entry_price * trade.entry_size
        outcome = classify_outcome(trade.realized_pnl_net, entry_notional)
        if outcome == OUTCOME_WIN:
            current_wins += 1
            current_losses = 0
        elif outcome == OUTCOME_LOSS:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = 0
            current_losses = 0
        max_wins = max(max_wins, current_wins)
        max_losses = max(max_losses, current_losses)

    return max_wins, max_losses


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_vals = sorted(values)
    mid = len(sorted_vals) // 2
    if len(sorted_vals) % 2 == 1:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2
