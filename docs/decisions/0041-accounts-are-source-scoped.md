# 0041 – Accounts Are Source-Scoped (Multi-Venue Identity)

## Decision

Accounts metadata is scoped by `(source, account_id)` rather than `account_id` alone.

Concretely:

- SQLite `accounts` will be keyed by **both** `source` and `account_id` (composite primary key).
- UI/analytics account selectors will treat an account as identified by `account_key = "{source}:{account_id}"` for filtering and display.

## Rationale

Trades, fills, funding, and snapshots are already scoped by `(source, account_id)` throughout the journal. Keeping `accounts` keyed only by `account_id` creates a correctness hazard when multiple venues share the same identifier format (e.g., EVM wallet addresses reused across venues).

Source-scoping makes multi-venue behavior deterministic and avoids silent collisions in account metadata (base currency, starting equity, active flags) and any future account-level joins.

## Status

Accepted – 2026-02-06

