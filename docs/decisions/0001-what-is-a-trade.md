# 0001 – What is a Trade

## Decision

A trade represents a complete position lifecycle for a symbol and side,
from first fill to fully flat. Trades may contain multiple entry and exit legs.
All primary analytics (PnL, MAE, MFE, ETD) are computed at the trade level.

## Rationale

Matches discretionary trading intent and produces intuitive MAE/MFE/ETD.

## Status

Accepted
