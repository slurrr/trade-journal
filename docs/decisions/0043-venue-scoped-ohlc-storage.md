# 0043 – Unified Venue-Scoped OHLC Storage in SQLite

## Decision

Store OHLC price bars in SQLite keyed by venue (`source`) so MAE/MFE/ETD and trade-detail charts use the price series from the venue the trade occurred on.

Use **one** OHLC table for all price bars (both benchmark and trade-window caching) keyed by `(source, symbol, timeframe, timestamp)`.

Concretely:

- Introduce a new OHLC table (name TBD, e.g. `price_bars`) keyed by `(source, symbol, timeframe, timestamp)`.
- For each trade, fetch and cache missing bars from that trade’s `source` (ApeX klines for ApeX trades; Hyperliquid `candleSnapshot` for Hyperliquid trades).
- Compute per-trade charts/series and excursion metrics (MAE/MFE/ETD) using those venue-scoped bars.

Benchmarks (e.g. BTC) will read from the same OHLC table by selecting a specific `(source, symbol, timeframe)` series. If the chosen benchmark series overlaps with already-cached trade-window bars, it is naturally reused with no duplication.

Benchmark series source is **Hyperliquid** by default (BTC from `source="hyperliquid"`, symbol `BTC-USDC`), not ApeX.

This supersedes Decision 0029’s “dedicated `benchmark_prices` table” approach. The existing `benchmark_prices` table may remain temporarily for backward compatibility/migration, but new code should converge on the unified OHLC table.

## Rationale

MAE/MFE/ETD and trade replay charts are sensitive to the underlying price series. Different venues can differ materially (index/mark composition, spread, liquidity, outages), so using a single global OHLC source can misstate excursions for trades executed elsewhere.

Using one venue-scoped OHLC table for all price data keeps storage simpler, eliminates duplicated data between “benchmark” and “trade-window” caches, and makes it explicit which venue’s price series is used for a given metric.

## Notes

The OHLC table should include a place for optional fields (e.g. volume, trade_count, raw payload) without changing the primary key.

Hyperliquid `candleSnapshot` appears to cap responses to the most recent ~5000 candles per request, which limits historical 1m backfills. For now, benchmark coverage is treated as “best effort” based on what is stored locally going forward; long-history benchmark backfill is deferred.

## Status

Accepted – 2026-02-07
