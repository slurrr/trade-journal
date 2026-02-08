# 0044 – Canonical 1-Minute Price Bars

## Decision

Use **1-minute** OHLC bars (`timeframe="1m"`) as the canonical stored granularity for all venue-scoped price data in SQLite.

Larger timeframes (5m/15m/1h/1d) are derived from 1m bars when needed, rather than stored as additional authoritative series by default.

## Rationale

Minute-level bars are granular enough to support trade-window metrics (MAE/MFE/ETD) and short benchmark windows without requiring tick data, while remaining storage-feasible for local-first use.

Deriving larger bars from 1m keeps semantics consistent across venues and avoids duplicated storage for multiple timeframes.

## Status

Accepted – 2026-02-07

