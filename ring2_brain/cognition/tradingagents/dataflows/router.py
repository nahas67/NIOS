"""Vendor routing surface.

The router is intentionally thin here: the harvested graph imports it, and the
truth-layer ``VendorRouterBridge`` records which vendor answered.  A call that
cannot be served raises the typed data-layer error rather than returning an
empty frame that a model could mistake for a real answer.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .errors import NoMarketDataError, VendorUnavailableError

logger = logging.getLogger(__name__)

__all__ = [
    "no_data_available",
    "route_to_vendor",
    "vendor_unavailable",
    "register_vendor",
]

_REGISTRY: dict[str, dict[str, Callable[..., Any]]] = {}


def register_vendor(name: str, methods: dict[str, Callable[..., Any]]) -> None:
    _REGISTRY[str(name)] = dict(methods)


def route_to_vendor(method: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``method`` on the first registered vendor that provides it."""
    for vendor, methods in _REGISTRY.items():
        handler = methods.get(str(method))
        if handler is not None:
            try:
                return handler(*args, **kwargs)
            except (NoMarketDataError, VendorUnavailableError):
                raise
            except Exception as exc:  # pragma: no cover - vendor-specific
                logger.warning("vendor %s failed for %s: %s", vendor, method, exc)
                continue
    raise VendorUnavailableError(f"no vendor serves {method!r}")


def no_data_available(exc: Exception) -> str:
    return f"No market data available: {exc}"


def vendor_unavailable(method: str, exc: Exception) -> str:
    return f"Vendor unavailable while calling {method}: {exc}"
