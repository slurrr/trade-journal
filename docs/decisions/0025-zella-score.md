# 0025 – Zella Score Definition

## Decision

We compute a composite Zella Score (0–100) as a weighted sum of six normalized sub-scores:

- Profit Factor (25%)
- Max Drawdown (20%, inverted)
- Avg Win/Loss (20%)
- Win % (15%)
- Recovery Factor (10%)
- Consistency (10%)

Sub-score normalization defaults:

- Profit Factor and Avg Win/Loss: `score = clamp(value / 3, 0..1) * 100`
- Win %: `score = clamp((win_rate - 0.30) / 0.40, 0..1) * 100`
- Max Drawdown %: `score = clamp(1 - (dd_pct / 0.40), 0..1) * 100`
- Recovery Factor: `score = clamp(recovery / 5, 0..1) * 100`
- Consistency: `score = clamp(1 - (stdev(daily_pnl) / max(1, avg_abs_daily_pnl * 2)), 0..1) * 100`

Missing sub-scores are excluded and weights are re-normalized over the available metrics.

## Rationale

This mirrors TradeZella’s six-axis model while keeping the scoring transparent and tunable. Linear caps provide stable, interpretable scores without overfitting.

Current defaults explained

- Profit Factor cap = 3.0
Maps PF 0→3 to 0→100.
PF of 3 is already very strong; beyond that we don’t keep inflating the score.
- Avg Win/Loss cap = 3.0
Same idea as PF. A 3:1 payoff is “excellent,” so it gets full credit.
- Win% range = 30%–70%
Below 30% is poor even with payoff; above 70% is elite.
We scale inside that band so it’s not overly sensitive around common win rates.
- Max Drawdown cap = 40% (inverted)
0% drawdown = 100, 40% drawdown = 0.
Past 40% is still “bad” for most systems, so it’s capped.
- Recovery Factor cap = 5.0
Recovery = net_pnl / max_drawdown.
5 means you’ve made 5× your worst drawdown; that’s strong.
- Consistency
Uses daily PnL volatility:
1 - (stdev / max(1, avg_abs_daily_pnl * 2))
So a trader with smoother daily outcomes scores higher; noisy days lower.

## Status

Accepted
