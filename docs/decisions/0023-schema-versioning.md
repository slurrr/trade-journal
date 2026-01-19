# 0023 – Schema and Metrics Versioning

## Decision

SQLite will include a `schema_version` table (single row) with `schema_version` and `metrics_version`. When metrics logic changes, we increment `metrics_version` and recompute derived outputs; when storage changes, we increment `schema_version` and run a migration/backfill as needed.

## Rationale

This makes changes explicit, keeps derivations reproducible, and provides a clear signal for when to recompute or migrate.

## Status

Accepted
