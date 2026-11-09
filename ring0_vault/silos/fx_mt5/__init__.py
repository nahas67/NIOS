"""Ring 0 Forex & futures silo -- MetaTrader 5, CTP and OANDA v20.

Three harvested donor trees plus one NIOS-native adapter live here:

* ``vnpy_core``  -- vn.py framework core (event engine, trader objects, gateway base)
* ``vnpy_mt5``   -- MetaTrader 5 gateway
* ``vnpy_ctp``   -- CTP futures gateway with offline ``MdApi`` / ``TdApi`` shims
* ``oanda``      -- NIOS OANDA v20 REST adapter (24/5 pip-spread gates, macro
  calendar blackout windows, roll-yield guards)

Importing this package root installs the ``vnpy`` / ``vnpy_evo`` / ``talib``
compatibility bridge first, so ``vnpy_mt5`` and ``vnpy_ctp`` import with zero
``ImportError`` on a host that has neither a MetaTrader 5 terminal nor a CTP
``.dll``/``.so`` broker session.  Live arming still fails closed: the gateway
raises ``TransportUnavailable`` when a session is requested without its
transport (``sec_004`` air-gap, ``sec_010`` SLAs).
"""

from __future__ import annotations

from . import _vnpy_compat

_vnpy_compat.install()

__all__ = ["_vnpy_compat", "oanda", "vnpy_core", "vnpy_ctp", "vnpy_mt5"]
