# 0006 – Timestamp Normalization to UTC

## Decision
Normalize fill timestamps to timezone-aware UTC; when timezone is missing, assume UTC.

## Rationale
Trade reconstruction depends on deterministic ordering and time windows. Assuming UTC avoids implicit local-time bias, with the tradeoff that exports containing local timestamps without timezone metadata may be misinterpreted until confirmed.

## Status
Provisional
