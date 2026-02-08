# 0042 – Frontend Polling for Open Positions Freshness

## Decision

Add a small frontend poller to keep the “Open Positions” panel fresh without requiring a full page reload:

1. Poll an API endpoint (proposed: `/api/account/open-positions`) every 15–30 seconds.
2. Re-render only the Open Positions table and the “last snapshot” timestamp.
3. Keep the existing server-rendered HTML as the initial render and fallback when polling fails.

## Rationale

The journal is local-first and primarily read-only. Lightweight polling provides a responsive UX (positions update shortly after closes/changes) without adding WebSocket complexity or requiring the backend to push events.

Scoping updates to the Open Positions panel reduces UI churn and avoids re-fetching full analytics payloads.

## Status

Acceoted – 2026-02-07

