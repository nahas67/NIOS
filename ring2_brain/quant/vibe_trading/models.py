"""Backtest accounting models referenced by the harvested engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["EquitySnapshot", "FillRecord", "Position", "TradeRecord"]


@dataclass(slots=True)
class FillRecord:
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    price: float = 0.0
    ts: Any = None
    fee: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TradeRecord:
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    entry_price: float = 0.0
    exit_price: float | None = None
    entry_ts: Any = None
    exit_ts: Any = None
    pnl: float = 0.0
    fee: float = 0.0
    exit_reason: str = ""


@dataclass(slots=True)
class Position:
    symbol: str = ""
    quantity: float = 0.0
    average_price: float = 0.0
    market_price: float = 0.0
    currency: str = "USD"

    @property
    def market_value(self) -> float:
        return float(self.quantity) * float(self.market_price)


@dataclass(slots=True)
class EquitySnapshot:
    ts: Any = None
    cash: float = 0.0
    positions_value: float = 0.0
    equity: float = 0.0
    currency: str = "USD"
