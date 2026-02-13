# 0038 – Duration Histogram Bins

## Decision

Duration histograms use fixed bins (seconds):

- 0–5m
- 5–15m
- 15–30m
- 30–60m
- 1–2h
- 2–4h
- 4–8h
- 8–24h
- 1–2d
- 2–5d
- 5d+

## Rationale

Fixed, human‑readable bins match how traders reason about hold time and keep charts comparable across datasets without requiring dynamic bin tuning.

## Status

Accepted – 2026-01-22
