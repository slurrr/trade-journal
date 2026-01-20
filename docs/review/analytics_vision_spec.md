# Analytics Page Specification – trade_journal

## Purpose

The Analytics page is the central analysis interface of the trade_journal application.  
It provides a Tradezella-style experience for exploring performance, diagnostics, and edge attribution using a single unified filter system and a persistent tab structure.

Primary goals:

- Provide professional-grade trading analytics
- Allow deep exploration of performance across dimensions
- Maintain a consistent UX driven entirely by URL query parameters
- Require no client-side business logic beyond rendering

---

## 1) PAGE STRUCTURE

### Route

```
/analytics
```

### Layout Skeleton

```
-----------------------------------------------------------
HEADER: "Analytics"
-----------------------------------------------------------
GLOBAL FILTER BAR
-----------------------------------------------------------
TABS
-----------------------------------------------------------
TAB CONTENT AREA
-----------------------------------------------------------
```

All content on the page is controlled by query parameters.

Example:

```
/analytics?tab=overview&from=2025-01-01&to=2025-01-31&symbol=BTC,ETH&side=long
```

---

## 2) GLOBAL FILTER BAR

The filter bar is the primary control surface for the entire page.

### Components (left → right)

- Date Range Picker  
- Symbol Multi-Select  
- Side Selector (Long / Short / All)  
- Outcome Filter (All / Wins / Losses)  
- Session Filter  
- Strategy Selector  
- Account Selector  
- Normalization Mode  
- Apply Button  

### Normalization Mode Options

- Nominal (USD)
- Percentage of Account
- R-multiples

Normalization mode globally affects:

- Equity curves
- PnL metrics
- Distributions
- Expectancy displays
- Charts and tables

### Behavior Rules

- Filters persist when switching tabs  
- Every tab reads the same query parameters  
- No tab has private filters  
- Apply button triggers full page reload with new parameters  

This unified model ensures a consistent “Tradezella feel.”

---

## 3) TAB SYSTEM

### Exact Tabs

```
Overview | Diagnostics | Edge | Time | Trades
```

Five tabs only. No additional tabs should be added without explicit design revision.

---

## 4) TAB DEFINITIONS

---

## TAB 1 – OVERVIEW

### Purpose

Provide a high-level executive summary of performance for the current filter set.

### A. KPI Row

Primary cards:

- Net PnL  
- Win Rate  
- Expectancy (R)  
- Profit Factor  
- Avg Win  
- Avg Loss  
- Performance Score  

### B. Comparison Mode

Add optional comparison context:

- vs All Trades  
- vs Previous Period  
- vs Market Benchmark (BTC)

When enabled, KPIs display deltas relative to the comparison set.

### C. Primary Charts

- Equity Curve  
  - Toggle: USD / R-multiples  
  - Optional drawdown overlay  

- Cumulative PnL  

- PnL Distribution Histogram  

### D. Quick Stats

- Max Drawdown  
- Largest Win  
- Largest Loss  
- Avg Hold Time  

### E. Summary Table

Small aggregated table:

- Total Trades  
- Winners  
- Losers  
- Breakevens  

### Intent

This tab answers:

**“How am I performing overall?”**

---

## TAB 2 – DIAGNOSTICS

### Purpose

Evaluate execution quality and trade management efficiency.

### A. MAE / MFE Analysis

Charts:

- Scatter Plot  
  - X: MAE  
  - Y: MFE  

- Scatter Plot  
  - X: MFE  
  - Y: Realized PnL  

### B. Efficiency Metrics

Cards:

- MFE Capture %  
- MAE Tolerance  
- ETD Avg  
- Early Exit Rate  
- Exit Efficiency  

### C. Stop and Target Quality

Metrics:

- Avg Stop Distance  
- Stop Hit Rate  
- Target Hit Rate (if targets available)

### D. Diagnostics Table

Trade-level table:

| Symbol | PnL | MAE | MFE | Capture % |

### Intent

This tab answers:

**“Am I executing trades well?”**

---

## TAB 3 – EDGE

### Purpose

Identify where performance and edge originate.

### A. Symbol Attribution

Table + chart:

| Symbol | Trades | Win% | PF | Exp | PnL |

### B. Direction Analysis

Long vs Short comparison:

- Win rate  
- Profit factor  
- Expectancy  

### C. Strategy Attribution

When strategy tags exist:

| Strategy | Trades | Win% | PF | Exp | PnL |

### D. Position Size Attribution

Performance grouped by size bucket:

| Size Bucket | Trades | Win% | Exp |

### E. Market Regime Attribution (optional)

If regime data is available:

| Regime | Trades | Win% | PF | Exp |

### Intent

This tab answers:

**“Where does my edge actually come from?”**

---

## TAB 4 – TIME

### Purpose

Understand performance as a function of time.

### A. Hour of Day

Charts:

- PnL by hour  
- Win rate by hour  

### B. Day of Week

Bar charts:

- PnL by weekday  
- Trades by weekday  

### C. Duration Analysis

- PnL vs trade duration  
- Avg hold time distribution  

### D. Calendar Heatmap

Daily performance heatmap visualization.

### Intent

This tab answers:

**“When do I trade best?”**

---

## TAB 5 – TRADES

### Purpose

Serve as a raw trade explorer for the filtered dataset.

### A. Paginated Table

Columns:

- Date  
- Symbol  
- Side  
- PnL  
- R  
- MAE  
- MFE  
- Duration  
- Tags  

### B. Table Features

- Column sorting  
- Text search  
- Export CSV of current filtered set  

### C. Row Navigation

Clicking a row navigates to:

```
/trades/{id}
```

### Intent

This tab answers:

**“Show me the actual trades behind these analytics.”**

---

# 5) NAVIGATION LOGIC

### URL Model

Switching tabs modifies only:

```
tab=...
```

All other parameters remain intact.

Example transition:

```
/analytics?tab=overview&from=...&to=...&symbol=BTC
```

to

```
/analytics?tab=diagnostics&from=...&to=...&symbol=BTC
```

---

## 6) TEMPLATE STRUCTURE

Recommended organization:

```
templates/
  analytics.html
  analytics/
     _filters.html
     overview.html
     diagnostics.html
     edge.html
     time.html
     trades.html
```

### Base Template (analytics.html)

Pseudo-structure:

```
{% include 'analytics/_filters.html' %}

<ul class="tabs">
  ...
</ul>

<div class="tab-content">
  {% include current_tab_template %}
</div>
```

---

## 7) DATA FLOW

### Backend Responsibilities

For every request:

1. Parse query parameters  
2. Build filtered dataset  
3. Pre-compute all required analytics  
4. Pass prepared objects to templates  

Client-side JavaScript is used only for:

- Chart rendering  
- UI interactions  
- Table behaviors  

No business logic should live in the browser.

---

## 8) CHART STRATEGY

Use Chart.js for:

- Line charts  
- Bar charts  
- Scatter plots  

Recommended plugins:

- chartjs-plugin-zoom  
  - pan  
  - zoom  
  - reset  

---

## 9) DASHBOARD RELATIONSHIP

Once Analytics is complete, the main dashboard should be simplified to:

- High-level KPIs  
- Equity curve  
- Recent trades  

All deep analysis moves permanently to /analytics.

---

## 10) IMPLEMENTATION ROADMAP

To maintain momentum:

### Step 1

- Create /analytics route  
- Implement global filter bar  
- Implement Overview tab only  

### Step 2

- Add Trades tab  

### Step 3

- Add Diagnostics tab  

### Step 4

- Add Edge tab  

### Step 5

- Add Time tab  

---

## 11) DESIGN PRINCIPLES

- Server-rendered first  
- Query-parameter driven  
- No client-side business logic  
- Consistent filters across tabs  
- Minimal UX friction  
- Professional, Tradezella-class workflow  

---

## End of Specification
