"""Bybit adapter for Ring 1 (NIOS compatibility build).

Harvested from ``nautilus_trader.adapters.bybit``; re-expressed through
:mod:`ring1_spinal.nautilus_adapters._nautilus_compat` so it imports on any host.
No credentials, no sockets, no signing.
"""

from __future__ import annotations

from .._nautilus_compat import adapter_exports

__all__ = [
    "BYBIT",
    "BYBIT_CLIENT_ID",
    "BYBIT_VENUE",
    "BybitDataClientConfig",
    "BybitDataClientFactory",
    "BybitEnvironment",
    "BybitExecutionClientConfig",
    "BybitExecutionClientFactory",
    "BybitLiquidation",
    "BybitMarginAction",
    "BybitMarginBorrowResult",
    "BybitMarginRepayResult",
    "BybitMarginStatusResult",
    "BybitPositionIdx",
    "BybitPositionMode",
    "BybitProductType",
    "BybitTickerData",
]

globals().update(adapter_exports("bybit", __all__))

VENUE = "BYBIT"
SILO = "CRYPTO_BINANCE"
SUPPORTS_POST_ONLY = True
