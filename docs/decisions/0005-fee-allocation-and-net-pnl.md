# 0005 – Fee Allocation and Net PnL Semantics

## Decision
Allocate fill fees proportionally by size when a fill is split (e.g., reversals), and compute net PnL as realized PnL minus summed fees. Fees are treated as if denominated in the same unit as PnL until explicit currency conversion is added.

## Rationale
Proportional fee allocation keeps each trade auditable back to fills while preserving total fees. Net PnL is the most practical summary for initial reporting, but treating fee amounts as the PnL currency is a provisional simplification.

## Status
Provisional
