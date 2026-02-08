# 0037 – Regime Attribution

## Decision

Regime attribution uses the regime label active at **trade entry** (entry_time within the regime window). This label is attached as `entry_regime` and used for regime attribution tables.

## Rationale

Entry-time attribution aligns regime context with the decision to take the trade and avoids ambiguous mid‑trade regime changes. It is consistent with the time‑window join in Decision 0030 while keeping attribution deterministic.

## Status

Accepted – 2026-01-22
