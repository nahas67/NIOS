"""StockTwits sentiment vendor surface."""

from __future__ import annotations

from typing import Any

from ..errors import VendorNotConfiguredError

__all__ = ["fetch_stocktwits_messages"]


def fetch_stocktwits_messages(*args: Any, **kwargs: Any) -> Any:
    raise VendorNotConfiguredError("stocktwits.fetch_stocktwits_messages: no provider configured")
