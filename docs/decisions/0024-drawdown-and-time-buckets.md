# 0024 – Drawdown and Time-Based Buckets

## Decision

- Max drawdown is computed from the cumulative net PnL curve (ordered by trade exit time), as peak-to-trough drop in absolute terms and percentage of the prior peak.
- Time-of-day and day-of-week performance buckets use trade **exit time** converted to the local timezone.

## Rationale

Cumulative net PnL provides a consistent, explainable drawdown baseline without requiring account balance inputs. Exit time reflects when PnL is realized, and local time aligns with the exchange UI for day/hour analysis.

## Status

Accepted
