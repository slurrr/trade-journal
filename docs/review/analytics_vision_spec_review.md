**Analytics Page Spec Review**
- **High:** Global filter bar requires session/strategy/account selectors and normalization modes not currently supported by data model. These depend on tags/strategy taxonomy, accounts, and per-trade normalized values. Spec lines 55–69 (`src/trade_journal/web/analytics_page_spec.md:55`) would block a clean implementation without adding new sources. Recommend scoping to `date`, `symbol`, `side`, and `outcome` initially, and making the remaining filters explicitly “future/optional.”
    comment: postpone to phase 2
- **High:** Diagnostics tab requires metrics we do not compute (MFE capture %, MAE tolerance, early exit rate, exit efficiency, stop/target hit rates). We only have MAE/MFE/ETD and partial stop data; target data is absent. Spec lines 186–202 (`src/trade_journal/web/analytics_page_spec.md:186`) should be gated with “if available” or reduced to the metrics we actually compute.
    comment: postpone to phase 2
- **High:** Comparison mode (vs all, previous period, BTC benchmark) implies extra datasets (period aggregation and benchmark series). Not implemented and would require new data pipelines and normalization logic. Spec lines 124–133 (`src/trade_journal/web/analytics_page_spec.md:124`) should be deferred or marked optional.
    comment: postpone to phase 2
- **Medium:** Normalization modes (USD/%/R) assume account equity baseline and complete R coverage. We now have optional `TRADE_JOURNAL_INITIAL_EQUITY` and R where stops exist, but many trades may have `R=None`. Spec lines 65–77 (`src/trade_journal/web/analytics_page_spec.md:65`) should allow “USD only” fallback and note partial R coverage.
    comment: implement in phase 1, full R and account equity coverage moving forward
- **Medium:** Edge tab size buckets and regime attribution require new inputs (size bucketing rules, regime labels). Not present. Spec lines 244–255 (`src/trade_journal/web/analytics_page_spec.md:244`) should be optional or omitted until data exists.
    comment: postpone to phase 2
- **Medium:** Trades tab includes Tags and Strategy columns, but tagging doesn’t exist yet. Spec lines 309–320 (`src/trade_journal/web/analytics_page_spec.md:309`) should list tags as optional or omit.
    comment: postpone to phase 2
- **Low:** URL-driven filter system is not implemented yet, but aligns with the current Jinja + server-rendered model. No blocker; just work to add query parsing and filtering.
    comment: implement in phase 1

**Alignment With Current Build**
- **Stack alignment:** Jinja templates + Chart.js + server-driven query parameters is a good fit for our current FastAPI/Jinja setup. Spec template structure maps cleanly to our existing templates directory.
- **Available metrics now:** performance_score, equity curve, PnL distribution, win rate, profit factor, payoff ratio, max drawdown, time-of-day/day-of-week buckets, symbol breakdown, MAE/MFE/ETD, and R (when orders have stops).
- **Existing pages:** `/trades` and `/trades/{id}` already exist and can be reused for the Trades tab.
- **Calendar data:** daily PnL buckets exist for a heatmap (Time tab).
    comment: calendar and heatmap already implemented in calendar.html and can be reused for the Time tab.

**Suggested Spec Adjustments (to implement cleanly now)**
- Replace the full filter bar with **Date / Symbol / Side / Outcome** plus an “Advanced (future)” placeholder for session/strategy/account/normalization.
- Mark **Comparison Mode** and **Benchmark** as optional Phase 2.
- Diagnostics tab: limit to **MAE/MFE/ETD + scatter plots** and remove target/stop hit metrics until targets are captured.
- Edge tab: implement **Symbol** + **Direction** only; defer size buckets and regime.
- Trades tab: remove **Tags** until tagging is implemented; show `R` as `n/a` when missing.

**Changes Needed to Implement Spec Exactly**
- Add tagging/strategy taxonomy + tag UI (data model + persistence).
- Add account selector and per-account dataset separation.
- Add benchmark price series and comparison logic.
- Add explicit targets (from orders) and “exit efficiency” definitions.
- Define size bucket rules and regime labels.

**Open Questions / Assumptions**
- Should normalization to % use `TRADE_JOURNAL_INITIAL_EQUITY` or live account equity?
    comment: Account equity at time of trade. How much did the trade grow or reduce the account. 
- For “session filter,” what session taxonomy should we use (e.g., Asia/London/NY)?
    comment: Yes exactly
- For “strategy selector,” do we want freeform tags or a fixed list?
    comment: fixed list if we cannot analyze freeform tags.

**Change Summary**
- Spec is feasible in phases. Implementing the full filter bar and diagnostics as written requires new data sources. A scoped v1 of Overview/Edge/Time/Trades is implementable now with minimal changes.
