# 0002 – Trade Excursion Metrics (MAE / MFE / ETD)

## Scope

These metrics apply at the **trade level**, where a trade is a complete
position lifecycle from first fill to fully flat.

## Definitions

### Maximum Adverse Excursion (MAE)

The maximum unrealized loss experienced at any point during the trade,
measured from the trade’s weighted average entry price to the worst
intratrade price, adjusted for position size.

MAE represents the worst pain endured during the trade.

### Maximum Favorable Excursion (MFE)

The maximum unrealized profit experienced at any point during the trade,
measured from the trade’s weighted average entry price to the best
intratrade price, adjusted for position size.

MFE represents the maximum opportunity available during the trade.

### End Trade Drawdown (ETD)

The difference between MFE and the realized PnL at trade exit.

ETD = MFE − Realized PnL

ETD represents how much unrealized profit was given back before exit.

## Notes

- MAE and MFE are computed over the full trade duration.
- Scale-ins and scale-outs are included naturally via position size.
- Metrics are independent of exit strategy and execution quality.
