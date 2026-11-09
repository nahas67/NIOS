"""Perpetual-futures risk primitives referenced by the harvested engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AccountState",
    "CrossMarginRiskModel",
    "ExecutionFrame",
    "MaintenanceSchedule",
    "MarketRiskFrame",
    "PositionState",
    "RiskSnapshot",
    "evaluate_isolated",
]


@dataclass(slots=True)
class PositionState:
    symbol: str = ""
    quantity: float = 0.0
    entry_price: float = 0.0
    mark_price: float = 0.0
    side: str = "long"

    @property
    def notional(self) -> float:
        return abs(float(self.quantity) * float(self.mark_price))


@dataclass(slots=True)
class AccountState:
    cash: float = 0.0
    equity: float = 0.0
    positions: dict[str, PositionState] = field(default_factory=dict)


@dataclass(slots=True)
class MaintenanceSchedule:
    brackets: tuple[tuple[float, float], ...] = ((0.0, 0.005),)

    def rate_for(self, notional: float) -> float:
        rate = self.brackets[0][1]
        for threshold, candidate in self.brackets:
            if notional >= threshold:
                rate = candidate
        return float(rate)


@dataclass(slots=True)
class MarketRiskFrame:
    symbol: str = ""
    mark_price: float = 0.0
    funding_rate: float = 0.0


@dataclass(slots=True)
class ExecutionFrame:
    symbol: str = ""
    price: float = 0.0
    quantity: float = 0.0
    fee: float = 0.0


@dataclass(slots=True)
class RiskSnapshot:
    equity: float = 0.0
    maintenance_margin: float = 0.0
    margin_ratio: float = 0.0
    liquidated: bool = False


def evaluate_isolated(
    account: AccountState,
    schedule: MaintenanceSchedule | None = None,
    **_: Any,
) -> RiskSnapshot:
    schedule = schedule or MaintenanceSchedule()
    notional = sum(position.notional for position in account.positions.values())
    maintenance = sum(
        position.notional * schedule.rate_for(position.notional)
        for position in account.positions.values()
    )
    equity = float(account.equity or account.cash)
    ratio = equity / maintenance if maintenance > 0 else float("inf")
    return RiskSnapshot(
        equity=equity,
        maintenance_margin=maintenance,
        margin_ratio=ratio,
        liquidated=maintenance > 0 and equity <= maintenance,
    )


class CrossMarginRiskModel:
    def __init__(self, schedule: MaintenanceSchedule | None = None) -> None:
        self.schedule = schedule or MaintenanceSchedule()

    def evaluate(self, account: AccountState) -> RiskSnapshot:
        return evaluate_isolated(account, self.schedule)
