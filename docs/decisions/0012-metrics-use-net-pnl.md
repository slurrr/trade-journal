# 0012 – Analytics Use Net PnL

## Decision
Aggregate analytics (win rate, profit factor, expectancy, average win/loss, largest win/loss) are computed using net PnL (realized PnL minus fees plus funding). Breakeven trades are excluded from win/loss aggregates unless a metric explicitly includes them.

## Rationale
Fees and funding materially affect performance and should be included in all performance metrics to match account reality. Excluding breakeven trades keeps win/loss averages and profit factor meaningful.

## Status
Provisional
