# 0031 – Account Metadata Shape

## Decision

Define an `accounts` table with core metadata fields: `id`, `name`, `exchange`, `base_currency`, `starting_equity`, `created_at`, and `active`.

## Rationale

Multi-account support needs a stable, minimal account record that can drive normalization (base currency, starting equity) and allow future sub-accounts without redesign. The shape aligns with current config fields and avoids over-embedding account data elsewhere.

## Status

Accepted – 2026-01-21
