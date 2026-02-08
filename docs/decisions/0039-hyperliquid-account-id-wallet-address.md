# 0039 – Hyperliquid Account Identity (Wallet Address)

## Decision

For Hyperliquid, the journal will use the **user wallet address** (e.g. `0x...`) as the canonical `account_id`.

We will normalize Hyperliquid wallet addresses to **lowercase** (while preserving the original value in `accounts.raw_json` when available) to avoid duplicate accounts caused by checksum/mixed-case formatting.

## Rationale

Hyperliquid identifies users by wallet address, and most API calls key off that `user` value. Using the wallet address as `account_id` keeps ingestion and sync explainable and enables stable scoping for idempotent upserts (Decision 0020) and multi-account filtering in analytics.

Lowercasing provides a stable canonical form across CLI/UI/config, avoids accidental duplication, and remains compatible with any external display needs via `raw_json`.

## Status

Accepted – 2026-02-06

