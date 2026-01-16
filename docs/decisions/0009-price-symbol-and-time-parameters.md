# 0009 – Price Symbol and Time Parameters (ApeX Klines)

## Decision
For ApeX kline price data, request symbols without dashes (e.g., `XPL-USDT` -> `XPLUSDT`) and send `start`/`end` timestamps in seconds.

## Rationale
The ApeX `/v3/klines` endpoint uses compact symbols and second-based time parameters. Conforming to that format ensures price-series coverage aligns with trade windows.

## Status
Provisional
