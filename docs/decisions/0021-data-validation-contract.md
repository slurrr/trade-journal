# 0021 – Data Validation Contract

## Decision

Before persisting data, we validate required fields and basic sanity checks per dataset. Invalid rows are skipped and counted; they are not coerced into the database.

Required checks:

- fills: size > 0, price > 0
- orders: size > 0, price > 0 if present
- funding: symbol present (non-empty), side present
- liquidations: size > 0, symbol/side present
- historical PnL: size > 0, symbol present

Skipped rows are reported by dataset during sync.

## Rationale

This prevents malformed or partial payloads from poisoning the database while keeping behavior transparent through explicit skip counts.

## Status

Accepted
