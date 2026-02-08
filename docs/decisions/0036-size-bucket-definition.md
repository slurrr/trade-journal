# 0036 – Position Size Buckets

## Decision

Position size buckets are computed from **entry notional** (entry_price × entry_size) and use configurable bucket edges from `config/app.toml` under `[analytics] size_buckets`. Buckets are inclusive of the lower bound and exclusive of the upper bound, with a final `>=` bucket for values above the last edge.

Default edges (USD notional): 1k, 5k, 10k, 25k, 50k.

## Rationale

Entry notional is already used as the return % denominator (Decision 0013), making it the most consistent sizing proxy. Configurable edges allow tuning to account size without changing code. Simple left‑closed/right‑open buckets keep attribution deterministic.

## Status

Accepted – 2026-01-22
