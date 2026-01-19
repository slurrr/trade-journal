# 0018 – Initial Stop Used for R Multiple

## Decision
R multiple is computed using a size-weighted average of the stop-loss prices attached to entry fills (from `openSlParam` on the corresponding entry orders). If no entry stop is available, use the first TPSL stop order created after entry (within the trade window) as the initial stop. Later stop modifications do not change R.

## Rationale
The size-weighted entry stop matches how entry price is computed during scale-ins. Falling back to the earliest TPSL stop preserves a usable R estimate when entry stops are unavailable.

## Status
Provisional
