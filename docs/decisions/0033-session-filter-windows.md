# 0033 – Session Filter Windows

## Decision

Define session buckets using **UTC** at trade exit:

- Asia: 00:00–07:59
- London: 08:00–15:59
- NY: 16:00–23:59

## Rationale

Phase 2 requires a session filter that maps to global FX sessions. Using three non‑overlapping 8‑hour UTC blocks keeps the filter deterministic and aligns with the UTC storage standard (Decision 0006). Display can remain in local time without affecting session attribution.

## Status

Provisional
