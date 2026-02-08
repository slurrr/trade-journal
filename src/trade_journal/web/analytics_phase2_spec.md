# Analytics Page Spec – Phase 2 (Full Vision)

Phase 2 completes the full analytics vision described in `analytics_vision_spec.md`. It builds on Phase 1 without breaking URLs or templates.

---

## 1) Global Filter Bar (Expanded)

Add to Phase 1 filters:

- Session filter (Asia / London / NY)
- Strategy selector (fixed list; tags mapped to strategy type)
- Account selector (future multi/sub account capable)
- Normalization mode: USD | % of account | R‑multiples

Notes:

- Normalization in % uses **account equity at time of trade** (baseline per trade).
- If equity history is missing, % mode should be disabled or show n/a.

---

## 2) Comparison Mode (Overview tab)

Add comparison sets:

- vs All Trades
- vs Previous Period
- vs BTC Benchmark

Implementation notes:

- Previous period uses same length as current date filter.
- Benchmark needs OHLC or benchmark returns series (BTC).
- KPIs show deltas vs comparison.

---

## 3) Diagnostics (Expanded)

Add metrics:

- MFE Capture %
- MAE Tolerance
- ETD Avg (already present)
- Early Exit Rate
- Exit Efficiency
- Stop Hit Rate
- Target Hit Rate (requires target orders, or a planned TP tag)

Requirements:

- Target data from orders (TP) or planned TP tag
- Stop‑hit detection (requires stop orders or liquidation flags)

---

## 4) Edge (Expanded)

Add:

- Strategy attribution table
- Position size buckets (configured rules)
- Market regime attribution (requires regime labels per trade)

Requirements:

- Tagging model with strategy types
- Size‑bucket config
- Regime data source (external or computed)

---

## 5) Time (Expanded)

Add:

- PnL vs duration scatter
- Hold time distribution histogram

Requires:

- duration bins or scatter data (already available at trade level)

---

## 6) Trades Tab (Expanded)

Add columns:

- Tags
- Strategy
- Account

Requires:

- Tag system
- Strategy taxonomy
- Account table

---

## 7) Data & Schema Requirements

To avoid rework:

- `accounts` table (multi account/sub account)
- `tags` + `trade_tags`
- `strategies` (or tag types)
- `trade_equity_snapshots` or account equity history per trade
- `benchmark_prices` (BTC)
- `regime_labels` (by date or trade)

---

## 8) Normalization Details

- **USD**: raw net PnL.
- **% of account**: net PnL / equity_at_entry.
- **R‑multiples**: net PnL / risk_amount (only where stop exists).

Missing values:

- If R is missing, display `n/a` or exclude from R‑mode charts.
- If equity history missing, disable % mode or show `n/a`.

---

## 9) Completion Definition

Phase 2 is complete when:

- All filters are live and URL‑driven.
- All tab definitions from `analytics_vision_spec.md` are implemented.
- Normalization + comparisons work across all charts/tables.
