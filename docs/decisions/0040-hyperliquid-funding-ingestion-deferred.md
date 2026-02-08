# 0040 – Hyperliquid Funding Ingestion Deferred (v1)

## Decision

For the initial Hyperliquid integration (v1), we will ingest **fills first** and reconstruct trades from fills, while deferring funding-fee ingestion until the funding endpoint semantics and payload shape are confirmed.

Until funding ingestion is implemented, Hyperliquid trade `funding_fees` will remain `0.0`, and `realized_pnl_net` will effectively reflect **realized PnL minus fees** (excluding funding).

## Rationale

Fills provide the highest-granularity, auditable ground truth for trade reconstruction (Decision 0003) and are sufficient to deliver a working journal quickly.

Funding materially affects profitability, but implementing it incorrectly is worse than omitting it temporarily. Deferring funding ingestion keeps the v1 integration safe and explainable, while making the gap explicit so it can be closed once the correct Hyperliquid funding data source and attribution rules are validated.

## Status

Accepted – 2026-02-06

