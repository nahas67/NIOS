"""Ring 0 silos -- four sovereign broker silos with zero cross-border wires.

``silo_registry`` is the only module here that Ring 3 may import: it is a pure
control-plane description of the silos (mode, tax floor, ratchet assets, adapter
readiness) and it places no orders.

``india_brokers``, ``fx_mt5``, ``key_vault`` and ``analyzer_shadow`` are
harvested donor trees.  They are reached by explicit submodule path (for example
``ring0_vault.silos.india_brokers.dhan``) and are never star-imported -- that is
also what keeps a broker SDK out of Ring 0's import graph until a silo worker
loads it explicitly.

Phase 2 Step 2 wires the two runtime compatibility bridges here:

* :mod:`ring0_vault.silos.india_brokers._openalgo_compat` aliases the OpenAlgo
  donor namespaces (``utils.*``, ``database.*``, ``services.*``,
  ``websocket_proxy.*``, ``extensions``, ``broker.*``) onto Ring 0 primitives.
* :mod:`ring0_vault.silos.fx_mt5._vnpy_compat` aliases ``vnpy`` / ``vnpy_evo``
  onto the harvested ``vnpy_core`` tree plus offline IPC transport shims.

Both installs are idempotent, never replace a real third-party package, and are
wrapped so a bridge failure can never break the Ring 3 control plane that also
imports this package.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = [
    "silo_registry",
    "india_openalgo_compat_status",
    "fx_vnpy_compat_status",
]

_LOG = logging.getLogger("nios.ring0.silos")

_INDIA_COMPAT: Any = None
_FX_COMPAT: Any = None


def _install_india_compat() -> Any:
    """Install the OpenAlgo bridge once; return the module (or ``None``)."""

    global _INDIA_COMPAT
    if _INDIA_COMPAT is not None:
        return _INDIA_COMPAT
    try:
        from ring0_vault.silos.india_brokers import _openalgo_compat as compat

        compat.install()
        _INDIA_COMPAT = compat
    except Exception:  # pragma: no cover - bridge is best-effort at import time
        _LOG.warning("OpenAlgo compat bridge unavailable", exc_info=True)
        _INDIA_COMPAT = None
    return _INDIA_COMPAT


def _install_fx_compat() -> Any:
    """Install the vnpy bridge once; return the module (or ``None``)."""

    global _FX_COMPAT
    if _FX_COMPAT is not None:
        return _FX_COMPAT
    try:
        from ring0_vault.silos.fx_mt5 import _vnpy_compat as compat

        compat.install()
        _FX_COMPAT = compat
    except Exception:  # pragma: no cover - bridge is best-effort at import time
        _LOG.warning("vnpy compat bridge unavailable", exc_info=True)
        _FX_COMPAT = None
    return _FX_COMPAT


def india_openalgo_compat_status() -> dict[str, Any]:
    """Ring 0 diagnostics: which OpenAlgo namespaces are bridged."""

    compat = _install_india_compat()
    if compat is None:
        return {"installed": False, "reason": "bridge unavailable"}
    return compat.compat_status()


def fx_vnpy_compat_status() -> dict[str, Any]:
    """Ring 0 diagnostics: which vnpy namespaces are bridged."""

    compat = _install_fx_compat()
    if compat is None:
        return {"installed": False, "reason": "bridge unavailable"}
    return compat.compat_status()


_install_india_compat()
_install_fx_compat()
