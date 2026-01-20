from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass
class Fill:
    fill_id: str | None
    order_id: str | None
    symbol: str
    side: str
    price: float
    size: float
    fee: float
    fee_asset: str | None
    timestamp: datetime
    source: str = "apex"
    account_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class FundingEvent:
    funding_id: str | None
    transaction_id: str | None
    symbol: str
    side: str
    rate: float
    position_size: float
    price: float
    funding_time: datetime
    funding_value: float
    status: str | None
    source: str = "apex"
    account_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Trade:
    trade_id: str
    symbol: str
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    entry_size: float
    exit_size: float
    max_size: float
    realized_pnl: float
    fees: float
    source: str = "apex"
    account_id: str | None = None
    funding_fees: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    mae: float | None = None
    mfe: float | None = None
    etd: float | None = None

    @property
    def realized_pnl_net(self) -> float:
        return self.realized_pnl - self.fees + self.funding_fees
