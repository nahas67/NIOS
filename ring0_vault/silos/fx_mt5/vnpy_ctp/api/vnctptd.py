"""Offline CTP trader transport shim (``TdApi``).

Upstream ``vnpy_ctp`` binds ``TdApi`` to the CTP ``thosttraderapi`` C++ library
through pybind11.  The binding and the vendor ``.dll``/``.so`` are not part of
the NIOS harvest.

This module ships the same Python surface with an inert offline transport:

* importing it never loads a native library, so the FX silo stays importable on
  any host;
* order entry (``ReqOrderInsert`` / ``ReqOrderAction``) records the request and
  returns a failure code -- it can never silently fill an order;
* :func:`transport_available` reports whether a real CTP trader library exists,
  and the gateway raises
  :class:`~ring0_vault.silos.fx_mt5._vnpy_compat.TransportUnavailable` when a
  live session is requested without one.

Ring 0 live dispatch therefore fails closed.  Paper dispatch bypasses this class
entirely and fills deterministically in ``silo_registry.dispatch_signed_order``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

__all__ = ["TdApi", "transport_available", "OfflineTdTransport"]

_LOG = logging.getLogger("nios.ring0.ctp.td")

_OFFLINE_RC = -1

#: CTP ``THOST_TE_RESUME_TYPE`` values used by ``subscribePrivateTopic``.
THOST_TERT_RESTART = 0
THOST_TERT_RESUME = 1
THOST_TERT_QUICK = 2


def transport_available() -> bool:
    """True when a real CTP trader library is installed on this host."""

    try:
        from ring0_vault.silos.fx_mt5 import _vnpy_compat

        _md, td = _vnpy_compat.ctp_library_available()
        return td
    except Exception:  # pragma: no cover - compat always importable in Ring 0
        return False


class OfflineTdTransport:
    """Records CTP trader requests without opening a socket."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.front: str = ""
        self.connected = False
        self.authenticated = False
        self.spi: Any = None
        self.settlement_confirmed = False

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

    def registerNameServer(self, address: str) -> None:  # noqa: N802 - CTP API name
        self._record("registerNameServer", address)

    def registerSpi(self, spi: Any) -> None:  # noqa: N802 - CTP API name
        self.spi = spi
        self._record("registerSpi", spi)

    def subscribePrivateTopic(self, resume_type: int) -> None:  # noqa: N802
        self._record("subscribePrivateTopic", resume_type)

    def subscribePublicTopic(self, resume_type: int) -> None:  # noqa: N802
        self._record("subscribePublicTopic", resume_type)

    def init(self) -> None:  # noqa: N802 - CTP API name
        self.connected = False
        self._record("init")

    def exit(self) -> None:  # noqa: N802 - CTP API name
        self.connected = False
        self.authenticated = False
        self.settlement_confirmed = False
        self._record("exit")

    def getTradingDay(self) -> str:  # noqa: N802 - CTP API name
        self._record("getTradingDay")
        return time.strftime("%Y%m%d", time.gmtime())

    # -- session -----------------------------------------------------------
    def ReqAuthenticate(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqAuthenticate", req, request_id)
        self.authenticated = False
        return _OFFLINE_RC

    def ReqUserLogin(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqUserLogin", req, request_id)
        return _OFFLINE_RC

    def ReqUserLogout(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqUserLogout", req, request_id)
        return _OFFLINE_RC

    def ReqSettlementInfoConfirm(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqSettlementInfoConfirm", req, request_id)
        self.settlement_confirmed = False
        return _OFFLINE_RC

    # -- trading (must never fill offline) ---------------------------------
    def ReqOrderInsert(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqOrderInsert", req, request_id)
        _LOG.warning("offline CTP trader rejected order insert (request_id=%s)", request_id)
        return _OFFLINE_RC

    def ReqOrderAction(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqOrderAction", req, request_id)
        return _OFFLINE_RC

    # -- queries -----------------------------------------------------------
    def ReqQryInvestorPosition(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqQryInvestorPosition", req, request_id)
        return _OFFLINE_RC

    def ReqQryTradingAccount(self, req: Any, request_id: int) -> int:  # noqa: N802
        self._record("ReqQryTradingAccount", req, request_id)
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
        _LOG.debug("offline CTP td call %s(%s)", name, ", ".join(type(a).__name__ for a in args))


class TdApi(OfflineTdTransport):
    """Drop-in ``TdApi`` with an offline transport."""

    def __init__(self, flow_path: str = "", log: bool = False) -> None:
        super().__init__()
        self.flow_path = flow_path
        self.log = bool(log)

    @staticmethod
    def createFtdcTraderApi(flow_path: str = "", log: bool = False) -> "TdApi":  # noqa: N802
        return TdApi(flow_path, log)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"TdApi(offline={self.offline}, front={self.front!r})"
