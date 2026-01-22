# 0027 – Equity History for % Normalization

## Decision

Store account equity snapshots over time and compute `equity_at_entry` per trade on ingest by selecting the most recent snapshot at or before the trade entry time.

## Rationale

% normalization needs equity at entry. Snapshots preserve historical account equity while keeping the trade record self-contained for fast queries. Computing `equity_at_entry` at ingest gives deterministic results and avoids recomputing on every view, while snapshots provide an audit trail and allow rebackfill if needed.

## Status

Accepted – 2026-01-21
