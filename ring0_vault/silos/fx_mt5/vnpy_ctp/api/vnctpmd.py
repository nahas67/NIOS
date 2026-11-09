"""Offline CTP market-data transport shim (``MdApi``).

Upstream ``vnpy_ctp`` binds ``MdApi`` to the CTP ``thostmduserapi`` C++ library
through pybind11.  That binding is not part of the NIOS harvest, and the vendor
``.dll``/``.so`` is not licensed for redistribution inside this tree.

This module provides the *same Python surface* backed by an inert offline
transport:

* importing it never touches a native library, so the FX silo stays importable
  on any host;
* every request method records the call and returns a failure code instead of
  pretending the venue accepted it;
* :func:`transport_available` reports whether a real CTP library is present, and
  :class:`~ring0_vault.silos.fx_mt5._vnpy_compat.TransportUnavailable` is raised
  by the gateway layer when a live session is requested without one.

Paper dispatch never reaches this class -- Ring 0 formats the order and fills it
deterministically (see ``silo_registry.dispatch_signed_order``).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

__all__ = ["MdApi", "transport_available", "OfflineMdTransport"]

_LOG = logging.getLogger("nios.ring0.ctp.md")

#: CTP convention: negative return codes are failures.
_OFFLINE_RC = -1


def transport_available() -> bool:
    """True when a real CTP market-data library is installed on this host."""

    try:
        from ring0_vault.silos.fx_mt5 import _vnpy_compat

        md, _td = _vnpy_compat.ctp_library_available()
        return md
    except Exception:  # pragma: no cover - compat always importable in Ring 0
        return False


class OfflineMdTransport:
    """Records CTP market-data requests without opening a socket."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.front: str = ""
        self.connected = False
        self.spi: Any = None
        self._logged_in = False

    # -- introspection -----------------------------------------------------
    @property
    def offline(self) -> bool:
        return not transport_available()

    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def last_calls(self, limit: int = 20) -> list[tuple[str, tuple[Any, ...]]]:
        with self._lock:
            return list(self.calls[-int(limit):])

    # -- lifecycle ---------------------------------------------------------
    def registerFront(self, address: str) -> None:  # noqa: N802 - CTP API name
        self.front = str(address)
        self._record("registerFront", address)

    def registerSpi(self, spi: Any) -> None:  # noqa: N802 - CTP API name
        self.spi = spi
        self._record("registerSpi", spi)

    def init(self) -> None:  # noqa: N802 - CTP API name
        self.connected = False
        self._record("init")

    def exit(self) -> None:  # noqa: N802 - CTP API name
        self.connected = False
        self._logged_in = False
        self._record("exit")

    def getTradingDay(self) -> str:  # noqa: N802 - CTP API name
        self._record("getTradingDay")
        return time.strftime("%Y%m%d", time.gmtime())

    def subscribeMarketData(self, instruments: Any) -> int:  # noqa: N802
        self._record("subscribeMarketData", instruments)
        return _OFFLINE_RC

    def unSubscribeMarketData(self, instruments: Any) -> int:  # noqa: N802
        self._record("unSubscribeMarketData", instruments)
        return _OFFLINE_RC

    def ReqUserLogin(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqUserLogin", req, request_id)
        self._logged_in = False
        return _OFFLINE_RC

    def ReqUserLogout(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqUserLogout", req, request_id)
        self._logged_in = False
        return _OFFLINE_RC

    # -- catch-all ---------------------------------------------------------
    def __getattr__(self, item: str) -> Any:
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)

        def _offline(*args: Any, **kwargs: Any) -> int:
            self._record(item, *args)
            return _OFFLINE_RC

        return _offline

    def _record(self, name: str, *args: Any) -> None:
        with self._lock:
            self.calls.append((name, args))
            if len(self.calls) > 4096:
                del self.calls[: len(self.calls) - 4096]
        _LOG.debug("offline CTP md call %s(%s)", name, ", ".join(type(a).__name__ for a in args))


class MdApi(OfflineMdTransport):
    """Drop-in ``MdApi`` with an offline transport.

    ``createFtdcMdApi`` mirrors the upstream static factory.  When a real CTP
    library is present a future build can delegate to it; today the shim is the
    only implementation shipped in-tree.
    """

    def __init__(self, flow_path: str = "", log: bool = False) -> None:
        super().__init__()
        self.flow_path = flow_path
        self.log = bool(log)

    @staticmethod
    def createFtdcMdApi(flow_path: str = "", log: bool = False) -> "MdApi":  # noqa: N802
        return MdApi(flow_path, log)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"MdApi(offline={self.offline}, front={self.front!r})"
