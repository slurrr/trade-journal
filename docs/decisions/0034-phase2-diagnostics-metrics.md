# 0034 – Phase 2 Diagnostics Metric Definitions

## Decision

Define Phase 2 diagnostics metrics as follows (trade-level unless noted):

- **MFE Capture %**: mean capture percentage for winning trades with MFE > 0, where capture % = realized net PnL ÷ MFE × 100.
- **MAE Tolerance**: mean heat percentage for trades with MFE > 0, where heat % = |MAE| ÷ MFE × 100.
- **Early Exit Rate**: share of winning trades with MFE > 0 whose capture % < 25%.
- **Exit Efficiency**: weighted capture efficiency for winning trades with MFE > 0, computed as sum(realized net PnL) ÷ sum(MFE) × 100.
- **Stop Hit Rate**: share of trades with an initial stop and MAE where MAE ≤ −initial_risk.
- **Target Hit Rate**:
  - If a trade has a target tag (type in {"tp", "target"}), it is counted as target-hit.
  - Otherwise, if a target price is available from orders, the trade is target-hit when MFE ≥ target_pnl.
  - Rate is hits ÷ trades with target definitions.

## Rationale

These definitions keep diagnostics explainable with existing data (MFE/MAE, net PnL, initial stop, target price) and avoid introducing new data sources. Using capture/heat aligns with current metrics and provides stable, comparable thresholds. Stop/target hit are based on initial stop/target and excursions to avoid dependence on partial fills or moved orders.

## Status

Accepted – 2026-01-22
