# 0022 – Sync Error Policy

## Decision

API fetches use bounded retries with exponential backoff for transient failures, and paging fails loudly on non-OK payloads. Sync checkpoints/logs are only written on successful completion.

## Rationale

This reduces intermittent API noise without masking real errors, and prevents partial data from advancing the sync state.

## Status

Accepted
