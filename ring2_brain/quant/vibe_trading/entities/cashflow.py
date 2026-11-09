"""Dated, signed, single-currency cash-flow series.

The fund-math modules refuse bare lists on purpose: dates, currency and flow
kind are load-bearing for IRR/NPV/PME.  This module provides that contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

from .models import normalize_date

__all__ = ["CashFlow", "CashFlowSeries", "normalize_kind"]

_KIND_ALIASES: dict[str, str] = {
    "contribution": "contribution",
    "capital_call": "contribution",
    "call": "contribution",
    "distribution": "distribution",
    "dividend": "distribution",
    "return_of_capital": "distribution",
    "valuation": "valuation",
    "nav": "valuation",
    "mark": "valuation",
    "terminal": "valuation",
    "fee": "fee",
    "expense": "fee",
    "income": "income",
    "interest": "income",
}

_VALUATION_KINDS = frozenset({"valuation"})


def normalize_kind(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _KIND_ALIASES.get(text, text or "other")


@dataclass(frozen=True, slots=True)
class CashFlow:
    """One dated movement, signed from the holder's perspective."""

    amount: float
    date: date
    kind: str = "other"
    currency: str = "USD"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", float(self.amount))
        object.__setattr__(self, "date", normalize_date(self.date, field_name="date"))
        object.__setattr__(self, "kind", normalize_kind(self.kind))
        object.__setattr__(self, "currency", str(self.currency or "USD").upper())
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def is_valuation(self) -> bool:
        return self.kind in _VALUATION_KINDS


@dataclass(frozen=True, slots=True)
class CashFlowSeries:
    """A same-currency series of dated cash flows."""

    flows: tuple[CashFlow, ...] = ()
    currency: str = "USD"
    pre_translated: bool = False

    def __post_init__(self) -> None:
        values = tuple(self.flows)
        for flow in values:
            if not isinstance(flow, CashFlow):
                raise TypeError(f"expected CashFlow, got {type(flow).__name__}")
        currency = str(self.currency or (values[0].currency if values else "USD")).upper()
        for flow in values:
            if flow.currency != currency:
                raise ValueError(
                    f"cash-flow series mixes currencies: {currency} and {flow.currency}"
                )
        object.__setattr__(self, "flows", values)
        object.__setattr__(self, "currency", currency)

    def __iter__(self):
        return iter(self.flows)

    def __len__(self) -> int:
        return len(self.flows)

    @property
    def is_empty(self) -> bool:
        return not self.flows

    def dates(self) -> list[date]:
        return [flow.date for flow in self.flows]

    def filter(self, *, kind: Iterable[str] | str | None = None) -> "CashFlowSeries":
        wanted = None
        if kind is not None:
            wanted = {normalize_kind(value) for value in ([kind] if isinstance(kind, str) else kind)}
        flows = tuple(
            flow for flow in self.flows if wanted is None or flow.kind in wanted
        )
        return CashFlowSeries(flows, currency=self.currency, pre_translated=self.pre_translated)

    def total(self, *, include_valuations: bool = True) -> float:
        return float(
            sum(
                flow.amount
                for flow in self.flows
                if include_valuations or not flow.is_valuation
            )
        )
