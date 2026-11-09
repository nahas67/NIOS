"""Alpha Vantage vendor surface.

The harvested router imports these names at module scope.  The functions are
thin: a configured deployment can replace them, and an unconfigured one raises
``VendorNotConfiguredError`` instead of pretending it has data.
"""

from __future__ import annotations

from typing import Any

from ..errors import VendorNotConfiguredError

__all__ = [
    "get_balance_sheet",
    "get_cashflow",
    "get_fundamentals",
    "get_global_news",
    "get_income_statement",
    "get_indicator",
    "get_insider_transactions",
    "get_news",
    "get_stock",
]


def _unconfigured(name: str) -> None:
    raise VendorNotConfiguredError(f"alpha_vantage.{name}: no API key configured")


def get_balance_sheet(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_balance_sheet")


def get_cashflow(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_cashflow")


def get_fundamentals(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_fundamentals")


def get_global_news(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_global_news")


def get_income_statement(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_income_statement")


def get_indicator(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_indicator")


def get_insider_transactions(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_insider_transactions")


def get_news(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_news")


def get_stock(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_stock")
