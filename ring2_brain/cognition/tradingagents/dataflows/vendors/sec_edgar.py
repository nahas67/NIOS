"""SEC EDGAR statement vendor surface."""

from __future__ import annotations

from typing import Any

from ..errors import VendorNotConfiguredError

__all__ = ["get_balance_sheet", "get_cashflow", "get_income_statement"]


def _unconfigured(name: str) -> None:
    raise VendorNotConfiguredError(f"sec_edgar.{name}: no user-agent configured")


def get_balance_sheet(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_balance_sheet")


def get_cashflow(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_cashflow")


def get_income_statement(*args: Any, **kwargs: Any) -> Any:
    _unconfigured("get_income_statement")
