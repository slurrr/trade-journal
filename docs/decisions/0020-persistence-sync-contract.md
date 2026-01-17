# 0020 – Persistence Sync Contract

## Decision

We will enforce an idempotent sync contract for SQLite:

- Stable ApeX IDs are primary keys where available:
  - fills: `matchFillId` → fallback `id`
  - orders: `orderId` (aka `id`)
  - funding: `transactionId` → fallback `id`
  - liquidations: `id`/`liquidationId`
  - historical PnL: `id`
- If a stable ID is missing, we will store a deterministic content hash (symbol, side, price, size, timestamp, plus any available order/fill IDs) as the primary key.
- Fetches are upserted by primary key, and partial/failed fetches do not advance the sync checkpoint.

## Rationale

This preserves auditability and repeatability while avoiding duplicates or silent gaps. It enables safe re-runs and incremental backfills without manual cleanup, even when certain endpoints omit stable IDs.

## Status

Accepted
