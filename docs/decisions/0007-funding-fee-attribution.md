# 0007 – Funding Fee Attribution

## Decision
Funding events are attributed to the trade that is open for the same symbol and side at the funding timestamp (entry_time <= funding_time <= exit_time). Funding is included in net PnL alongside realized PnL and fees. Funding events that do not match any trade remain unmatched; if account open positions are available (`/v3/account`), unmatched events are associated to matching open positions (symbol + side) as an open-position attribution.

## Rationale
Funding is not part of fills, but it directly impacts realized profitability. Matching by symbol/side within the trade window keeps attribution explainable and auditable while aligning with ApeX’s funding semantics. `/v3/account` can be unreliable in other integrations, so open-position matching is optional and leaves events unmatched if the account snapshot is missing.

## Status
Provisional
