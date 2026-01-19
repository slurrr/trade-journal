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

We will use an incremental sync strategy with a per-endpoint checkpoint and overlap window:

- Store `sync_state` entries keyed by endpoint, including `last_timestamp_ms`, `last_id`, and `last_success_at`.
- On each sync, re-fetch with a fixed overlap window (default 24–48h) and rely on upserts to dedupe.
- Endpoints with time filters (fills/funding) use `beginTimeInclusive = last_timestamp_ms - overlap`.
- Endpoints without time filters (history-orders, historical PnL) page from 0 and stop once records are older than `last_timestamp_ms - overlap`.
- Checkpoints are only advanced on fully successful runs.

## Rationale

This preserves auditability and repeatability while avoiding duplicates or silent gaps. It enables safe re-runs and incremental backfills without manual cleanup, even when certain endpoints omit stable IDs.

## Status

Accepted
