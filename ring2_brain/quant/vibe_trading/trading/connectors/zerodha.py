"""Zerodha connector stub (Ring 0 owns broker sessions).

The import is the pinned absolute data-plane path the harvested loader already
declares; this module only re-exports the inert handle.
"""

from __future__ import annotations

from src.trading.connectors.dhan import sdk

__all__ = ["sdk"]
