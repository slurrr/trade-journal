# 0015 - Web UI data sourcing and timing

## Decision

The web UI derives all display data directly from local fills/funding exports using existing reconstruction and metrics code, and it buckets performance by trade exit time (equity curve uses cumulative net PnL on close; daily PnL aggregates by close date).

## Rationale

This keeps the UI fully local and explainable from source data without introducing a database, while aligning performance attribution to realized outcomes at trade close. It trades off live incremental updates and persistent annotations for simplicity and transparency.

## Status

Accepted
