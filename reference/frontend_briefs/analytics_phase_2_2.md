## Frontend Brief (for UI integration)

Context
Phase 2 backend now supplies:

- Filters: filters.entry_session, filters.exit_session (base-only), filters.exit_window (base + aux), filters.strategy, filters.account, filters.normalization
- Exposure filters: exp_<window>_seconds (minimum exposure threshold), e.g. exp_london_ny_overlap_seconds
- Lists: accounts (active), strategies, exposure_windows (base + aux)
- Diagnostics: diagnostics_phase2 (mfe_capture_pct, mae_tolerance_pct, early_exit_rate, exit_efficiency, stop_hit_rate, target_hit_rate)
- Attribution tables: strategy_attribution, size_buckets, regime_attribution
- Duration data: duration_charts (scatter points + histogram bins/counts)
- Trades include: entry_session, exit_session, session_exposures, exp_<window>_seconds, initial_target, target_pnl, target_source

UI tasks we can do now

1. Filters
   - Add Exit Window selector tied to query param `exit_window` (accepts base + aux windows).
   - Keep Exit Session selector tied to `exit_session` for base-only grouping safety.
   - For overlaps, use `exit_window=london_ny_overlap` to filter by exit_time in overlap window.
   - For exposure filters, use `exp_<window>_seconds` thresholds (e.g. 1 second or 300 seconds).
2. Diagnostics tab
   - Render metrics from `diagnostics_phase2` with % formatting.
   - Consider tooltip copy: Early Exit Rate = exited before target or stop; Exit Efficiency = realized pnl / (mfe or target).
3. Edge/Attribution tab
   - Strategy attribution table: use `strategy_attribution` rows and totals.
   - Size bucket attribution: use `size_buckets` rows (labels from backend).
   - Regime attribution: use `regime_attribution` rows (per entry/exit regime).
4. Time/Duration tab
   - Scatter plot using `duration_charts.scatter` (duration seconds vs outcome/realized pnl).
   - Histogram using `duration_charts.histogram` bins + counts.

UI tasks to postpone (if needed)

- Styling decisions for overlap/session selector UX (can be iterative).

Backend dependencies needed

- None for the above; all data already supplied.

———
