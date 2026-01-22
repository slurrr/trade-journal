from __future__ import annotations

from dataclasses import dataclass
from statistics import pstdev
from typing import Any, Iterable

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
    payoff_ratio: float | None
    max_drawdown: float | None
    max_drawdown_pct: float | None
    avg_r: float | None
    max_r: float | None
    min_r: float | None
    pct_r_below_minus_one: float | None
    roi_pct: float | None
    initial_equity: float | None
    net_return: float | None
    avg_trades_per_day: float | None
    max_trades_in_day: int
    avg_pnl_after_loss: float | None


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


def compute_aggregate_metrics(
    trades: Iterable[Trade],
    initial_equity: float | None = None,
) -> AggregateMetrics:
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
    payoff_ratio = None
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        payoff_ratio = avg_win / abs(avg_loss)

    max_drawdown, max_drawdown_pct = _max_drawdown(trade_list)

    r_values = _extract_r_values(trade_list)
    avg_r = _mean(r_values)
    max_r = max(r_values, default=None)
    min_r = min(r_values, default=None)
    pct_r_below_minus_one = None
    if r_values:
        below = sum(1 for value in r_values if value < -1.0)
        pct_r_below_minus_one = below / len(r_values)

    roi_pct = None
    net_return = None
    if initial_equity is not None:
        net_return = total_net_pnl
        if initial_equity:
            roi_pct = total_net_pnl / initial_equity

    avg_trades_per_day, max_trades_in_day = _trade_counts_by_day(trade_list)
    avg_pnl_after_loss = _avg_pnl_after_loss(trade_list)

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
        payoff_ratio=payoff_ratio,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        avg_r=avg_r,
        max_r=max_r,
        min_r=min_r,
        pct_r_below_minus_one=pct_r_below_minus_one,
        roi_pct=roi_pct,
        initial_equity=initial_equity,
        net_return=net_return,
        avg_trades_per_day=avg_trades_per_day,
        max_trades_in_day=max_trades_in_day,
        avg_pnl_after_loss=avg_pnl_after_loss,
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


def _max_drawdown(trades: list[Trade]) -> tuple[float | None, float | None]:
    ordered = sorted(trades, key=lambda trade: trade.exit_time)
    peak = 0.0
    max_dd = 0.0
    equity = 0.0
    max_dd_pct: float | None = None

    for trade in ordered:
        equity += trade.realized_pnl_net
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_pct = (drawdown / peak) if peak > 0 else None

    if max_dd == 0.0:
        return None, None
    return max_dd, max_dd_pct


def _extract_r_values(trades: list[Trade]) -> list[float]:
    values = []
    for trade in trades:
        r_value = getattr(trade, "r_multiple", None)
        if r_value is None:
            continue
        try:
            values.append(float(r_value))
        except (TypeError, ValueError):
            continue
    return values


def compute_time_performance(trades: Iterable[Trade]) -> dict[str, list[dict[str, float | int | None]]]:
    hourly: dict[int, list[float]] = {}
    weekday: dict[int, list[float]] = {}
    for trade in trades:
        exit_local = trade.exit_time.astimezone()
        hour = exit_local.hour
        day = exit_local.weekday()
        hourly.setdefault(hour, []).append(trade.realized_pnl_net)
        weekday.setdefault(day, []).append(trade.realized_pnl_net)

    hourly_rows = [_bucket_summary(hour, values) for hour, values in sorted(hourly.items())]
    weekday_rows = [_bucket_summary(day, values) for day, values in sorted(weekday.items())]
    return {"hourly": hourly_rows, "weekday": weekday_rows}


def compute_symbol_breakdown(trades: Iterable[Trade]) -> list[dict[str, float | int | str | None]]:
    buckets: dict[str, list[Trade]] = {}
    for trade in trades:
        buckets.setdefault(trade.symbol, []).append(trade)

    rows: list[dict[str, float | int | str | None]] = []
    for symbol, items in sorted(buckets.items(), key=lambda item: item[0]):
        metrics = compute_aggregate_metrics(items)
        rows.append(
            {
                "symbol": symbol,
                "trades": metrics.total_trades,
                "win_rate": metrics.win_rate,
                "total_net_pnl": metrics.total_net_pnl,
                "avg_net_pnl": metrics.expectancy,
                "avg_win": metrics.avg_win,
                "avg_loss": metrics.avg_loss,
                "profit_factor": metrics.profit_factor,
            }
        )
    return rows


def compute_pnl_distribution(
    trades: Iterable[Trade],
    bins: int = 20,
) -> dict[str, float | int | list[dict[str, float | int]]]:
    values = [trade.realized_pnl_net for trade in trades]
    if not values:
        return {"min": 0.0, "max": 0.0, "bins": []}
    min_val = min(values)
    max_val = max(values)
    if min_val == max_val:
        return {
            "min": min_val,
            "max": max_val,
            "bins": [{"start": min_val, "end": max_val, "count": len(values)}],
        }
    width = (max_val - min_val) / bins
    counts = [0] * bins
    for value in values:
        idx = int((value - min_val) / width)
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1
    buckets = []
    for idx, count in enumerate(counts):
        start = min_val + idx * width
        end = start + width
        buckets.append({"start": start, "end": end, "count": count})
    return {"min": min_val, "max": max_val, "bins": buckets}


def _bucket_summary(key: int, values: list[float]) -> dict[str, float | int | None]:
    wins = sum(1 for value in values if value > 0)
    losses = sum(1 for value in values if value < 0)
    total = len(values)
    win_rate = wins / (wins + losses) if wins + losses else None
    return {
        "bucket": key,
        "count": total,
        "total_pnl": sum(values),
        "avg_pnl": sum(values) / total if total else 0.0,
        "win_rate": win_rate,
    }


def compute_performance_score(trades: Iterable[Trade], metrics: AggregateMetrics) -> dict[str, Any]:
    weights = {
        "profit_factor": 0.25,
        "max_drawdown": 0.20,
        "avg_win_loss": 0.20,
        "win_rate": 0.15,
        "recovery_factor": 0.10,
        "consistency": 0.10,
    }

    profit_factor_score = _score_ratio(metrics.profit_factor, cap=3.0)
    avg_win_loss_score = _score_ratio(metrics.payoff_ratio, cap=3.0)
    win_rate_score = _score_range(metrics.win_rate, low=0.30, high=0.70)
    max_drawdown_score = _score_inverse(metrics.max_drawdown_pct, cap=0.40)

    recovery_factor = None
    if metrics.max_drawdown is not None and metrics.max_drawdown > 0:
        recovery_factor = metrics.total_net_pnl / metrics.max_drawdown
    recovery_factor_score = _score_ratio(recovery_factor, cap=5.0)

    consistency_raw = _consistency_ratio(trades)
    consistency_score = _score_ratio(consistency_raw, cap=1.0)

    component_scores = {
        "profit_factor": profit_factor_score,
        "max_drawdown": max_drawdown_score,
        "avg_win_loss": avg_win_loss_score,
        "win_rate": win_rate_score,
        "recovery_factor": recovery_factor_score,
        "consistency": consistency_score,
    }

    total_weight = sum(weights[key] for key, value in component_scores.items() if value is not None)
    if total_weight == 0:
        overall = None
    else:
        weighted_sum = 0.0
        for key, value in component_scores.items():
            if value is None:
                continue
            weighted_sum += value * weights[key]
        overall = weighted_sum / total_weight

    return {
        "score": overall,
        "components": component_scores,
        "weights": weights,
        "raw": {
            "profit_factor": metrics.profit_factor,
            "avg_win_loss": metrics.payoff_ratio,
            "win_rate": metrics.win_rate,
            "max_drawdown_pct": metrics.max_drawdown_pct,
            "recovery_factor": recovery_factor,
            "consistency": consistency_raw,
        },
    }


def _score_ratio(value: float | None, cap: float) -> float | None:
    if value is None:
        return None
    return _clamp(value / cap) * 100.0


def _score_range(value: float | None, low: float, high: float) -> float | None:
    if value is None:
        return None
    return _clamp((value - low) / (high - low)) * 100.0


def _score_inverse(value: float | None, cap: float) -> float | None:
    if value is None:
        return None
    return _clamp(1.0 - (value / cap)) * 100.0


def _clamp(value: float) -> float:
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


def _consistency_ratio(trades: Iterable[Trade]) -> float | None:
    daily = _daily_pnls(trades)
    if len(daily) < 2:
        return None
    abs_mean = _mean([abs(value) for value in daily]) or 0.0
    scale = max(1.0, abs_mean * 2.0)
    deviation = pstdev(daily)
    return _clamp(1.0 - (deviation / scale))


def _daily_pnls(trades: Iterable[Trade]) -> list[float]:
    buckets: dict[str, float] = {}
    for trade in trades:
        day = trade.exit_time.astimezone().date().isoformat()
        buckets[day] = buckets.get(day, 0.0) + trade.realized_pnl_net
    return list(buckets.values())


def _trade_counts_by_day(trades: list[Trade]) -> tuple[float | None, int]:
    buckets: dict[str, int] = {}
    for trade in trades:
        day = trade.exit_time.astimezone().date().isoformat()
        buckets[day] = buckets.get(day, 0) + 1
    if not buckets:
        return None, 0
    counts = list(buckets.values())
    return sum(counts) / len(counts), max(counts)


def _avg_pnl_after_loss(trades: list[Trade]) -> float | None:
    ordered = sorted(trades, key=lambda trade: trade.exit_time)
    values = []
    for idx in range(1, len(ordered)):
        prev = ordered[idx - 1]
        if prev.realized_pnl_net < 0:
            values.append(ordered[idx].realized_pnl_net)
    if not values:
        return None
    return sum(values) / len(values)
