"""Benchmark resolution for the harvested engines."""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["default_benchmark", "resolve_benchmark"]

_DEFAULTS: dict[str, str] = {
    "equity_us": "SPY",
    "equity_in": "NIFTY50",
    "equity_cn": "CSI300",
    "equity_hk": "HSI",
    "equity_kr": "KOSPI",
    "crypto": "BTC-USD",
    "forex": "DXY",
    "futures": "",
}


def default_benchmark(market: str = "equity_us") -> str:
    return _DEFAULTS.get(str(market), "")


def resolve_benchmark(
    market: str = "equity_us",
    config: Mapping[str, Any] | None = None,
    **_: Any,
) -> str:
    configured = dict(config or {}).get("benchmark")
    return str(configured) if configured else default_benchmark(market)
