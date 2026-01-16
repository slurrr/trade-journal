# 0004 – Symbol Netting (One-Way Positions)

## Decision
Reconstruct trades by netting fills into a single position stream per symbol, assuming one-way positioning.

## Rationale
This matches the trade definition (flat to flat) and keeps reconstruction explainable with a single position timeline. The tradeoff is that hedge-mode (simultaneous long and short on the same symbol) is not supported without separate buckets.

## Status
Accepted
