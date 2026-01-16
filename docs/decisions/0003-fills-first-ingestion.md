# 0003 – Fills-First Ingestion

## Decision

Use ApeX Omni fills as the primary ingestion source and reconstruct trades from fills.

## Rationale

Fills are the highest-granularity ground truth and make trade reconstruction explainable back to raw data. This supports scale-ins/outs and aligns with the "flat to flat" trade definition, at the cost of more parsing and reconstruction logic.

## Status

Accepted
