"""Reddit sentiment vendor surface."""

from __future__ import annotations

from typing import Any

from ..errors import VendorNotConfiguredError

__all__ = [
    "CRYPTO_SUBREDDITS",
    "DEFAULT_SUBREDDITS",
    "fetch_reddit_posts",
    "subreddits_for",
]

DEFAULT_SUBREDDITS: tuple[str, ...] = ("wallstreetbets", "stocks", "investing")
CRYPTO_SUBREDDITS: tuple[str, ...] = ("CryptoCurrency", "Bitcoin", "ethereum")


def subreddits_for(symbol: str, asset_type: str = "stock") -> tuple[str, ...]:
    return CRYPTO_SUBREDDITS if str(asset_type).lower() == "crypto" else DEFAULT_SUBREDDITS


def fetch_reddit_posts(*args: Any, **kwargs: Any) -> Any:
    raise VendorNotConfiguredError("reddit.fetch_reddit_posts: no provider configured")
