"""Yahoo news vendor surface."""

from __future__ import annotations

from typing import Any

from ...errors import VendorNotConfiguredError

__all__ = ["get_global_news_yfinance", "get_news_yfinance"]


def get_news_yfinance(*args: Any, **kwargs: Any) -> Any:
    raise VendorNotConfiguredError("yahoo.news.get_news_yfinance: no provider configured")


def get_global_news_yfinance(*args: Any, **kwargs: Any) -> Any:
    raise VendorNotConfiguredError("yahoo.news.get_global_news_yfinance: no provider configured")
