# 0010 – Soft-Fail Missing Price Series

## Decision
If price data is missing or incomplete for a trade window, MAE/MFE/ETD computation is skipped for that trade and a loud warning is emitted. The trade still renders with empty excursion metrics.

## Rationale
Price data availability can be inconsistent. Soft-failing keeps the journal usable while making gaps explicit so the user can monitor and correct sources.

## Status
Provisional
