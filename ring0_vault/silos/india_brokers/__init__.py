"""Ring 0 India equities & F&O broker silo.

Four harvested OpenAlgo broker adapters live here -- ``dhan``, ``zerodha``,
``angel`` and ``upstox``.  Importing this package root installs the OpenAlgo
compatibility bridge so every deep submodule (``api/order_api.py``,
``api/data.py``, ``api/funds.py``, ``mapping/transform_data.py``,
``mapping/order_data.py``, ``streaming/``) resolves its donor imports
(``utils.*``, ``database.*``, ``services.*``, ``websocket_proxy.*``,
``extensions``, ``broker.*``) against Ring 0 primitives.

The adapters themselves are never star-imported here: a silo worker loads the
one venue it needs, which keeps a broker SDK out of the Ring 0 import graph
until the silo is armed (``sec_004``).
"""

from __future__ import annotations

from . import _openalgo_compat

_openalgo_compat.install()

__all__ = ["_openalgo_compat"]
