# 0033 – Session Filter Windows

## Decision

Define session buckets and exposures using **UTC** at trade exit/entry:

- Asia: 00:00–07:59
- London: 08:00–15:59
- NY: 16:00–23:59

Session attribution uses:

- `entry_session`: session containing `entry_time` (mutually exclusive).
- `exit_session`: session containing `exit_time` (mutually exclusive).
- `exp_<window>_seconds`: exposure duration for each defined UTC window (base sessions + auxiliary windows).

## Rationale

Phase 2 requires a session filter that maps to global FX sessions and supports overlap. Using three non‑overlapping 8‑hour UTC blocks keeps the group‑by keys deterministic and aligns with the UTC storage standard (Decision 0006). Overlap is captured as exposure duration to windows (including auxiliary overlaps), allowing filters like “touched London” or “London–NY overlap” without changing group‑by semantics. Display can remain in local time without affecting attribution.

## Status

Provisional
