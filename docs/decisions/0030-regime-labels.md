# 0030 – Regime Label Storage

## Decision

Store regime labels as a time‑based series (`regime_series`) separate from trades. Trade/regime linkage is derived by time overlap and does not require a foreign key at ingest.

## Rationale

Regime detection is experimental and may change. Keeping regimes in their own time series avoids coupling core trade data to a specific model and allows multiple sources (manual or model versions) with confidence scoring.

## Status

Accepted – 2026-01-21
