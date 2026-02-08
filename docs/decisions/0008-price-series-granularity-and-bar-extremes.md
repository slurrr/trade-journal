# 0008 – Price Series Granularity and Bar Extremes

## Decision
Use 1-minute candlesticks as the default price-series granularity for MAE/MFE/ETD. For bars fully inside the trade window, compute excursions using bar highs and lows at the bar end time. For the entry bar (trade opens inside the bar), use the entry price plus the bar close; for the exit bar (trade closes inside the bar), use the exit price only. If a trade opens and closes within the same bar, use entry and exit prices only.

## Rationale
1-minute bars are a practical balance between accuracy and API load, keeping intrabar ambiguity small without requiring tick data. Using highs/lows for interior bars preserves the most adverse/favorable price within each bar. Using entry price + bar close for the entry bar captures post-entry movement without including pre-entry extremes. Using exit price only for the exit bar prevents excursions from being influenced by prices after the trade has closed. This balances accuracy with explainability for 1-minute data.

## Status
Provisional
