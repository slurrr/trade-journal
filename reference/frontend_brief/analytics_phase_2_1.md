## Frontend Brief (for UI integration)

Context
Phase 2 backend now supplies:

- Filters: filters.entry_session, filters.exit_session (session alias), filters.strategy, filters.account, filters.normalization
- Lists: accounts (active), strategies
- Normalization: normalization object in context with mode, available, forced
- Comparisons: comparisons with KPI deltas vs all/previous/BTC
- Trade items include tags, strategy_tags, tags_by_type, account_id, entry_session, exit_session
- Exposure windows on trades: exp_<window>_seconds (base + auxiliary), e.g. exp_london_seconds, exp_tokyo_london_overlap_seconds

UI tasks we can do now

1. Filter bar
   - Add dropdowns for Entry Session / Exit Session / Strategy / Account / Normalization.
   - Tie them to query params: entry_session, exit_session (session alias supported), strategy, account, normalization.
   - Support exposure filters via query params: exp_<window>_seconds (minimum exposure threshold).
   - Disable % and R modes if normalization.available.percent / normalization.available.r are false.
2. Overview
   - Add comparison selector (All Trades / Previous Period / BTC).
   - Display comparisons.all_trades.delta, comparisons.previous_period.delta, comparisons.benchmark.return alongside KPIs.
3. Trades tab
   - Add columns: Tags, Strategy, Account.
   - Tags: render trade.tags list.
   - Strategy: render trade.strategy_tags (first or joined).
   - Account: render trade.account_id.
   - Optional: show entry_session / exit_session for clarity when using session filters.

UI tasks to postpone

- Diagnostics expanded metrics cards (not computed yet)
- Strategy attribution table / size buckets / regime attribution
- Duration scatter + histogram

Backend dependencies for postponed UI

- Add new metrics in Python for:
  - Early Exit Rate, Exit Efficiency, Stop/Target hit, Strategy attribution, Size buckets, Regime join, Duration charts.

———
