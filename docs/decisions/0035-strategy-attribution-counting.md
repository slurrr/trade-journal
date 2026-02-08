# 0035 – Strategy Attribution Counting

## Decision

Strategy attribution counts a trade in **every** strategy tag it carries. If a trade has multiple strategy tags, it contributes to multiple strategy rows.

## Rationale

Tags are multi-label by design. Allowing a trade to contribute to multiple strategy buckets preserves the tagging intent and avoids silently discarding tags. Consumers should treat totals as non-exclusive.

## Status

Accepted – 2026-01-22
