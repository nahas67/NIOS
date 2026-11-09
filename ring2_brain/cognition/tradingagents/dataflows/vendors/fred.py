"""FRED macro vendor surface."""

from __future__ import annotations

from typing import Any

from ..errors import VendorNotConfiguredError

__all__ = ["get_macro_data"]


def get_macro_data(*args: Any, **kwargs: Any) -> Any:
    raise VendorNotConfiguredError("fred.get_macro_data: no FRED API key configured")
