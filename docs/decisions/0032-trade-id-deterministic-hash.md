# 0032 – Deterministic Trade IDs

## Decision

Use a deterministic hash for `trade_id` based on trade reconstruction fields: source, account_id, symbol, side, entry/exit timestamps, entry/exit price, entry/exit size, and realized PnL. The hash is SHA‑1 over a pipe‑delimited string with numeric fields formatted to 8 decimals.

## Rationale

Trade tags and any persisted annotations need a stable identifier that survives re‑ingest and reconstruction. Deriving `trade_id` from the reconstructed trade fields keeps it explainable back to fills, avoids random UUID churn, and provides stable joins for `trade_tags` without introducing new storage or ingestion dependencies.

## Status

Accepted – 2026-01-22
