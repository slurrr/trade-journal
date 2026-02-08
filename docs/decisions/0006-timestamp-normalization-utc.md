# 0006 – Timestamp Normalization to UTC

## Decision
Normalize all backend timestamps to timezone-aware UTC; when timezone metadata is missing, assume UTC.

Scope includes fills, orders, funding, liquidations, equity snapshots, and price bars. UI localization happens only at display time.

## Rationale
Trade reconstruction depends on deterministic ordering and time windows. Assuming UTC avoids implicit local-time bias, with the tradeoff that exports containing local timestamps without timezone metadata may be misinterpreted until confirmed. Keeping all backend times in UTC also prevents cross-source drift when combining fills, funding, orders, and price series.

## Status
Provisional
