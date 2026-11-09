"""Ring 2 -- Bicameral Brain.

The harvested donor trees (Vibe-Trading, Trading-R1, TradingAgents) were written
against their upstream repository roots (``src.*``, ``backtest.*``,
``trading_r1.*``, ``tradingagents.*``).  Importing this package installs those
namespace aliases once, so the donor modules execute unmodified instead of
being replaced by native fallbacks (``sec_005``).
"""

from __future__ import annotations

from ._donor_compat import install_donor_aliases

install_donor_aliases()

__all__ = ["install_donor_aliases"]
