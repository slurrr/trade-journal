# 0017 – Liquidation Source and Matching

## Decision
Liquidations are derived from fills and history orders by detecting liquidation signals (exitType = Liquidate, isLiquidate, or liquidateFee > 0). Each liquidation is matched to a trade by symbol, side, size, and a close-time window.

## Rationale
Liquidation signals appear in fills/orders even when the historical PnL feed omits them. Using exitType/liquidateFee provides direct evidence of forced closes while keeping the matching explainable.

## Status
Provisional
