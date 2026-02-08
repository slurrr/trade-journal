# 0026 – Filter Non-Success Fill Statuses

## Decision

Ignore fills that include a non‑success status in the ApeX payload (e.g. FAILED_*), and only ingest fills whose status indicates success/filled when the field is present.

## Rationale

We observed a duplicate LINEA fill where one record was marked `FAILED_CENSOR_FAILURE` and created a persistent open‑position drift (+1071). Treating failed/unsuccessful records as real fills breaks reconstruction and funding attribution. Filtering non‑success statuses keeps the fill stream consistent with actual executed fills while preserving raw records for audit.

## Status

Accepted – 2026-01-21
