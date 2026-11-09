"""Rebalance-mask helpers for the harvested engines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import pandas as pd

__all__ = ["RebalanceMask", "resolve_rebalance_dates", "validate_rebalance_mask"]


@dataclass(slots=True)
class RebalanceMask:
    dates: tuple[pd.Timestamp, ...] = ()
    reason: str = ""

    def contains(self, value: Any) -> bool:
        return pd.Timestamp(value) in set(self.dates)


def resolve_rebalance_dates(
    index: Sequence[Any],
    frequency: str | None = None,
    explicit: Sequence[Any] | None = None,
) -> tuple[pd.Timestamp, ...]:
    if explicit is not None:
        return tuple(pd.Timestamp(value) for value in explicit)
    if not frequency:
        return tuple(pd.Timestamp(value) for value in index)
    series = pd.Series(1, index=pd.to_datetime(list(index)))
    return tuple(series.resample(frequency).first().dropna().index)


def validate_rebalance_mask(mask: RebalanceMask) -> None:
    if not isinstance(mask, RebalanceMask):
        raise TypeError("mask must be a RebalanceMask")
    if len(set(mask.dates)) != len(mask.dates):
        raise ValueError("rebalance mask contains duplicate dates")
