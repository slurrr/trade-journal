# 0045: Hyperliquid Funding Normalization

Date: 2026-02-08

## Context
Phase 2 adds Hyperliquid funding ingestion into the shared `funding` table.
Funding rows must be idempotent and attributable to reconstructed trades.

## Decision
- Source contract: Hyperliquid `/info` `type="userFunding"`.
- Side normalization for funding attribution uses position side semantics:
  - `szi >= 0` => `LONG`
  - `szi < 0` => `SHORT`
- Idempotency key uses:
  - primary: `"{hash}:{time_ms}:{coin}"` (under source/account scoping in storage)
  - fallback when hash is missing: `"fallback:{time_ms}:{coin}"`
- If mark/index price is absent, derive funding `price` as:
  - `abs(usdc) / (abs(szi) * abs(fundingRate))` when denominator > 0
  - otherwise `0.0` and preserve raw payload in `raw_json`.

## Consequences
- Funding events can be re-synced safely without duplication.
- Funding attribution remains consistent with trade sides (`LONG`/`SHORT`) across venues.
- Derived funding price is best-effort; raw payload remains the explainability source.

## Status
Accepted
