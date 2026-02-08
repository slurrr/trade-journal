# 0029 – Benchmark Price Series Storage

## Decision

Store benchmark OHLC data in a dedicated `benchmark_prices` table containing only the symbols and timeframes required by the journal (e.g., BTCUSDT at 1m/5m/1h/1d).

## Rationale

Benchmark comparisons need a lightweight, auditable price history without becoming a full market database. A focused OHLC table keeps storage small while supporting normalization and comparison features in Phase 2.

## Status

Accepted – 2026-01-21
