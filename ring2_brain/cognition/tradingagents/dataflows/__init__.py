"""Dataflow compatibility layer for the harvested TradingAgents code.

Upstream keeps these modules in a sibling package that was not part of the
harvest.  They provide the configuration context, vendor routing surface and
date/symbol helpers the agents and graph import.  Network calls stay lazy:
importing this package never opens a socket, and every vendor function is
allowed to raise :class:`NoMarketDataError` when no provider is configured.
"""

from __future__ import annotations
