"""OKX adapter for Ring 1 (NIOS compatibility build).

Harvested from ``nautilus_trader.adapters.okx``; re-expressed through
:mod:`ring1_spinal.nautilus_adapters._nautilus_compat` so it imports on any host.
No credentials, no sockets, no signing.
"""

from __future__ import annotations

from .._nautilus_compat import adapter_exports

__all__ = [
    "OKX",
    "OKX_CLIENT_ID",
    "OKX_VENUE",
    "OKXDataClientConfig",
    "OKXDataClientFactory",
    "OKXEnvironment",
    "OKXExecutionClientConfig",
    "OKXExecutionClientFactory",
]

globals().update(adapter_exports("okx", __all__))

VENUE = "OKX"
SILO = "CRYPTO_BINANCE"
SUPPORTS_POST_ONLY = True
