"""Yahoo market vendor surface."""

from __future__ import annotations

from typing import Any

from ...errors import VendorNotConfiguredError

__all__ = [
    "get_YFin_data_online",
    "get_closes",
    "get_stock_stats_indicators_window",
]


def get_YFin_data_online(*args: Any, **kwargs: Any) -> Any:
    raise VendorNotConfiguredError("yahoo.market.get_YFin_data_online: no provider configured")


def get_closes(*args: Any, **kwargs: Any) -> Any:
    raise VendorNotConfiguredError("yahoo.market.get_closes: no provider configured")


def get_stock_stats_indicators_window(*args: Any, **kwargs: Any) -> Any:
    raise VendorNotConfiguredError(
        "yahoo.market.get_stock_stats_indicators_window: no provider configured"
    )
