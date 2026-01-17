# 0018 – Initial Stop Used for R Multiple

## Decision
R multiple is computed using the initial stop attached to the entry order. If the entry order does not carry an open stop, use the first TPSL stop order created after entry (within the trade window) as the initial stop. Later stop modifications do not change R.

## Rationale
R is intended to reflect the planned risk at trade inception. Using the earliest stop preserves that intent while still allowing automated extraction from ApeX data.

## Status
Provisional
