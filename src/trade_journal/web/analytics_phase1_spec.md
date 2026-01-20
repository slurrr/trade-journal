# Analytics Page Spec – Phase 1 (Implementation-Ready)

This is the Phase 1 implementation spec derived from `analytics_vision_spec.md`.  
Phase 1 is intentionally scoped to current data capabilities while preserving the URL/query model and tab layout needed for Phase 2.

---

## 1) Purpose

Phase 1 delivers a working analytics page with:
- a single global filter model,
- five tabs (Overview, Diagnostics, Edge, Time, Trades),
- server‑rendered analytics from the existing dataset,
- no client‑side business logic.

Phase 1 **does not** implement benchmark comparisons, strategy/account filters, regime/size attribution, or target/stop hit metrics.

---

## 2) Route & URL Model

Route: `/analytics`

Query parameters (Phase 1):
- `tab`: one of `overview|diagnostics|edge|time|trades`
- `from`: ISO date (inclusive)
- `to`: ISO date (inclusive)
- `symbol`: comma‑separated symbols (e.g., `BTC-USDT,ETH-USDT`)
- `side`: `long|short|all`
- `outcome`: `all|win|loss|breakeven`

Behavior:
- Changing tabs preserves all query params.
- Apply button triggers full page reload.
- Unknown/extra params are ignored (reserved for Phase 2).

---

## 3) Global Filter Bar (Phase 1)

Components (left → right):
- Date range (from/to)
- Symbol multi‑select
- Side selector (Long/Short/All)
- Outcome selector (All/Wins/Losses/Breakeven)
- Apply button

Notes:
- Normalization mode is **not implemented** in Phase 1 (USD only).
- Session, Strategy, Account selectors are **not shown** but the layout should leave room for them.

---

## 4) Tabs

### Tab 1 – Overview

**KPI Cards**
- Net PnL (net)
- Win Rate
- Expectancy (net per trade)
- Profit Factor
- Avg Win
- Avg Loss
- Performance Score

**Charts**
- Equity curve (net PnL cumulative)
- PnL distribution histogram

**Quick Stats**
- Max drawdown (absolute + pct)
- Largest win
- Largest loss
- Avg hold time

**Summary Table**
- Total trades
- Winners
- Losers
- Breakevens

---

### Tab 2 – Diagnostics

**MAE/MFE Scatter**
- X: MAE
- Y: MFE

**MFE vs Realized PnL Scatter**
- X: MFE
- Y: Realized net PnL

**Efficiency Metrics (only what exists now)**
- Mean MAE / MFE / ETD
- Median MAE / MFE / ETD

**Diagnostics Table**
| Symbol | Net PnL | MAE | MFE | ETD |

Notes:
- No target/stop hit rates yet.
- No exit efficiency/early exit metrics yet.

---

### Tab 3 – Edge

**Symbol Attribution**
| Symbol | Trades | Win% | PF | Expectancy | Net PnL |

**Direction Analysis**
Long vs Short:
- Win rate
- Profit factor
- Expectancy

Notes:
- Strategy attribution, size buckets, regime attribution deferred to Phase 2.

---

### Tab 4 – Time

**Hour of Day**
- PnL by hour
- Win rate by hour

**Day of Week**
- PnL by weekday
- Trades by weekday

**Calendar Heatmap**
- Daily PnL (existing calendar buckets)

Notes:
- Duration analysis charts deferred to Phase 2.

---

### Tab 5 – Trades

**Table Columns**
- Date (exit time)
- Symbol
- Side
- Net PnL
- R (if available; otherwise n/a)
- MAE
- MFE
- Duration

**Table Features**
- Column sort
- Text search
- Export CSV of current filtered set (optional in Phase 1; allowed if trivial)

Row click -> `/trades/{id}`

---

## 5) Data Flow (Phase 1)

Backend:
1. Parse query parameters
2. Filter trades server‑side
3. Compute aggregates + per‑tab datasets
4. Pass ready data to templates

Client:
- Chart.js rendering only
- No analytic computation in JS

---

## 6) Architectural Notes (Phase‑2 Readiness)

Phase 1 should be implemented with these Phase‑2 constraints in mind:
- Query param parsing must allow additional filters without breaking existing URLs.
- The filter pipeline must be structured so additional predicates (account/strategy/session/normalization) can be inserted later.
- Keep analytics calculations isolated to reusable helpers (avoid duplicating logic in templates).
- Avoid embedding metric logic in JavaScript; pass prepared series from Python.

Database prep (to avoid rework later):
- Anticipate `tags`, `trade_tags`, `strategies`, and `accounts` tables.
- Consider storing `trade_equity_at_entry` and `trade_equity_at_exit` when account equity history is available.
- Keep derived metrics versioned (already in schema_version).

---

## 7) Non‑Goals (Phase 1)

- Benchmark comparisons
- Normalization modes (% of account, R‑multiples)
- Strategy/tag filters
- Account selector
- Regime attribution
- Target/stop hit metrics

---

# 8) UI INSPIRATION ASSETS

## Purpose

The repository contains example images from Tradezella that are provided solely as visual inspiration references.

These assets must be used only for:

- Layout concepts
- Chart composition ideas
- Visual hierarchy
- Information density
- Interaction patterns

They must NOT be used to:

- Copy exact designs
- Replicate color schemes
- Duplicate branding
- Mimic fonts or styles

## Location

All reference images are stored in:

reference/ui_examples

## Usage Instructions for AI Implementation

When implementing UI components, Codex must:

1. Review the images in `ui_examples/`
2. Extract structural ideas such as:
   - Chart types used for specific metrics
   - Panel layout proportions
   - Legend placement
   - Axis choices
   - Table organization

3. Apply those ideas using:

- The trade_journal design system
- Our existing color palette
- Our typography
- Our HTML templates
- Our CSS framework

## Chart Guidance

For each analytics section, Codex should:

- Compare the desired chart in this spec with similar charts shown in `ui_examples`
- Match functional composition where appropriate

Example mappings:

- Equity curve → emulate visual composition of equity chart images
- MAE/MFE scatter → mirror axis orientation and dot density style
- PnL distribution → histogram layout similar to examples

## Prohibition

Under no circumstances should Codex:

- Copy pixel-perfect layouts
- Recreate Tradezella branding
- Reproduce proprietary icons or fonts

---

## 9) Phase 2 Link

Phase 2 (see `analytics_phase2_spec.md`) is the target design; Phase 1 should preserve its layout and URL model so the transition is additive.
