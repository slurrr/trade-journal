# 0019 – Position Remainder Epsilon

## Decision
Treat residual position sizes below 1e-9 as zero during reconstruction to avoid floating-point artifacts creating phantom micro-trades.

## Rationale
Fills that should net to flat can leave tiny residuals due to floating-point arithmetic. Clamping prevents meaningless trades while preserving real sizes.

## Status
Provisional
