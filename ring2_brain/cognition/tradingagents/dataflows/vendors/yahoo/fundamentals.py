"""Yahoo fundamentals vendor surface."""

from __future__ import annotations

from typing import Any

from ...errors import VendorNotConfiguredError

__all__ = [
    "get_balance_sheet",
    "get_cashflow",
    "get_company_profile",
    "get_fundamentals",
    "get_income_statement",
    "get_insider_transactions",
]


def _unconfigured(name: str) -> None:
    raise VendorNotConfiguredError(f"yahoo.fundamentals.{name}: no provider configured")


def get_balance_sheet(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_balance_sheet")


def get_cashflow(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_cashflow")


def get_company_profile(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_company_profile")


def get_fundamentals(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_fundamentals")


def get_income_statement(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_income_statement")


def get_insider_transactions(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_insider_transactions")
