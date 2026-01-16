# 0007 – Funding Fee Attribution

## Decision
Funding events are attributed to the trade that is open for the same symbol and side at the funding timestamp (entry_time <= funding_time <= exit_time). Funding is included in net PnL alongside realized PnL and fees.

## Rationale
Funding is not part of fills, but it directly impacts realized profitability. Matching by symbol/side within the trade window keeps attribution explainable and auditable while aligning with ApeX’s funding semantics.

## Status
Provisional
