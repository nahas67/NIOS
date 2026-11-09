"""Data-layer exception hierarchy."""

from __future__ import annotations

__all__ = [
    "DataflowError",
    "NoMarketDataError",
    "VendorNotConfiguredError",
    "VendorUnavailableError",
    "SymbolNotFoundError",
]


class DataflowError(Exception):
    """Base class for every data-layer refusal."""


class NoMarketDataError(DataflowError):
    """The vendor answered, but had no data for the requested instrument."""


class VendorUnavailableError(DataflowError):
    """The vendor could not be reached or refused the request."""


class VendorNotConfiguredError(VendorUnavailableError):
    """The requested vendor has no credentials configured."""


class SymbolNotFoundError(NoMarketDataError):
    """The requested symbol does not resolve to an instrument."""
