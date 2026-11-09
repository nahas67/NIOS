"""Verified market snapshot used by the agent tools."""

from __future__ import annotations

from typing import Any

from ...errors import NoMarketDataError

__all__ = ["build_verified_market_snapshot"]


def build_verified_market_snapshot(
    symbol: str,
    as_of_date: str,
    look_back_days: int = 30,
) -> str:
    raise NoMarketDataError(
        f"no verified market snapshot for {symbol} as of {as_of_date} "
        f"({look_back_days}d lookback): provider not configured"
    )
