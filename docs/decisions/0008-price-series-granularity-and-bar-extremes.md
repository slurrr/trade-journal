# 0008 – Price Series Granularity and Bar Extremes

## Decision
Use 1-minute candlesticks as the default price-series granularity for MAE/MFE/ETD, and compute excursions using bar highs and lows at the bar end time.

## Rationale
1-minute bars are a practical balance between accuracy and API load, keeping intrabar ambiguity small without requiring tick data. Using highs/lows preserves the most adverse/favorable price within each bar, which aligns with the excursion definitions while remaining explainable.

## Status
Provisional
