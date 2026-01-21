# 0002 – Trade Excursion Metrics (MAE / MFE / ETD)

## Scope

These metrics apply at the **trade level**, where a trade is a complete
position lifecycle from first fill to fully flat.

## Definitions

### Maximum Adverse Excursion (MAE)

The maximum **price-based total PnL drawdown** experienced at any point during
the trade. This is computed as:

`realized_price_pnl_so_far + unrealized_price_pnl`

Price-only means **fees and funding are excluded**.

MAE represents the worst pain endured during the trade.

### Maximum Favorable Excursion (MFE)

The maximum **price-based total PnL** experienced at any point during the trade.
This includes realized price PnL from scale-outs plus the unrealized price PnL
on the remaining open size.

Price-only means **fees and funding are excluded**.

MFE represents the maximum opportunity available during the trade.

### End Trade Drawdown (ETD)

The difference between MFE and the realized PnL at trade exit.

ETD = MFE − Realized Price PnL

ETD represents how much unrealized profit was given back before exit.

## Notes

- MAE and MFE are computed over the full trade duration.
- Scale-ins and scale-outs are included naturally via position size.
- Metrics are independent of exit strategy and execution quality.
