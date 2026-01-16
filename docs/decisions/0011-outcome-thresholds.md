# 0011 – Win/Loss/Breakeven Thresholds

## Decision
Classify trade outcomes by net PnL with a breakeven band: breakeven if |net PnL| <= 0.33% of entry notional, win if above that band, loss if below.

## Rationale
Net PnL is the most truthful measure after fees and funding, but tiny residuals from fees and rounding should not be treated as wins or losses. Using a small percentage of entry notional scales the band sensibly across instruments and sizes.

## Status
Provisional
