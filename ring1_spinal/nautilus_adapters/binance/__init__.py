"""Binance adapter for Ring 1 (NIOS compatibility build).

Harvested from ``nautilus_trader.adapters.binance``.  The upstream module
imports ``nautilus_trader._fixup`` and ``nautilus_trader._libnautilus.binance``,
neither of which exists in nautilus_trader 1.231.0, and both of which are
unavailable on hosts where the compiled extensions are policy-blocked.  The
public surface is preserved here through
:mod:`ring1_spinal.nautilus_adapters._nautilus_compat`.

This module is import-safe, allocation-free at import time and carries no
credentials.  It only describes and formats Binance orders; Ring 0 signs and
sends them.
"""

from __future__ import annotations

from .._nautilus_compat import adapter_exports

__all__ = [
    "BINANCE",
    "BINANCE_CLIENT_ID",
    "BINANCE_VENUE",
    "BinanceBar",
    "BinanceDataClientConfig",
    "BinanceDataClientFactory",
    "BinanceEnvironment",
    "BinanceExecutionClientConfig",
    "BinanceExecutionClientFactory",
    "BinanceFuturesLiquidation",
    "BinanceFuturesMarkPriceUpdate",
    "BinanceFuturesOpenInterest",
    "BinanceFuturesOpenInterestHist",
    "BinanceFuturesOpenInterestHistPoint",
    "BinanceFuturesTicker",
    "BinanceInstrumentProviderConfig",
    "BinanceMarginType",
    "BinancePositionSide",
    "BinanceProductType",
    "BinanceSpotMarketDataMode",
    "BinanceSpotTicker",
    "decode_binance_futures_client_order_id",
    "decode_binance_spot_client_order_id",
    "get_binance_arrow_schema_map",
    "load_binance_instruments",
    "load_binance_order_book_deltas",
]

globals().update(adapter_exports("binance", __all__))

#: Ring 1 venue metadata used by ``ring0_vault.silos.silo_registry``.
VENUE = "BINANCE"
SILO = "CRYPTO_BINANCE"
SUPPORTS_POST_ONLY = True
