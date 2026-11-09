"""OpenAlgo compatibility bridge for the Ring 0 India broker silos.

The four India broker adapters (``dhan``, ``zerodha``, ``angel``, ``upstox``) were
harvested from OpenAlgo and still import their donor support packages by absolute
name (``utils.*``, ``database.*``, ``services.*``, ``websocket_proxy.*``,
``extensions``, ``broker.*``).  Those packages do not ship with the NIOS tree.

This module installs *clean namespace aliases* into :data:`sys.modules` so the
harvested adapters import with zero ``ImportError`` while every bridge is backed
by Ring 0 primitives:

* credentials come from Ring 0's encrypted ``key_vault`` (never from a module
  level constant), with an environment-variable fallback for paper bootstrap;
* HTTP goes through one shared ``httpx`` connection pool owned by Ring 0;
* the ``broker`` namespace is a *lazy alias* onto
  ``ring0_vault.silos.india_brokers`` so ``broker.dhan.api.order_api`` and
  ``ring0_vault.silos.india_brokers.dhan.api.order_api`` resolve to the **same
  module object** (class identity is preserved, no duplicate module state).

Ring ownership: this module lives in Ring 0 and is imported by Ring 0 only.
Ring 1 and Ring 2 never import it (see the air-gap AST gate).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import os
import sys
import threading
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

__all__ = [
    "COMPAT_MODULES",
    "install",
    "compat_status",
    "uninstall",
    "shared_http_client",
    "broker_credentials",
    "seeded_instrument",
    "resolve_token",
]

_LOG = logging.getLogger("nios.ring0.openalgo_compat")

_LOCK = threading.RLock()
_INSTALLED = False
_INSTALLED_AT_NS = 0
_STATE: dict[str, str] = {}

COMPAT_MODULES: tuple[str, ...] = (
    "utils",
    "utils.logging",
    "utils.constants",
    "utils.httpx_client",
    "utils.mpp_slab",
    "utils.position_read",
    "utils.smart_order_guard",
    "utils.broker_backpressure",
    "utils.lazy",
    "utils.runtime",
    "database",
    "database.auth_db",
    "database.token_db",
    "database.token_db_enhanced",
    "database.apilog_db",
    "database.engine_factory",
    "database.market_calendar_db",
    "services",
    "services.quotes_service",
    "websocket_proxy",
    "websocket_proxy.base_adapter",
    "websocket_proxy.mapping",
    "websocket_proxy.order_adapter",
    "extensions",
    "logzero",
    "broker",
)

INDIA_VENUES: tuple[str, ...] = ("dhan", "zerodha", "angel", "upstox")

_BROKER_ALIAS = "broker"
_BROKER_TARGET = "ring0_vault.silos.india_brokers"


# ---------------------------------------------------------------------------
# permissive stub used only for symbols this bridge does not model explicitly
# ---------------------------------------------------------------------------


class _Stub:
    """Truthy, callable placeholder returned by shim ``__getattr__`` hooks.

    The bridge models every symbol the four India adapters import *today*.  The
    stub exists so a future donor refresh cannot reintroduce an ``ImportError``
    for a symbol that has not been wired yet.  It is deliberately inert: it
    never performs I/O and never fabricates a numeric market claim.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def __getattr__(self, item: str) -> "_Stub":
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        return _Stub(f"{self._name}.{item}")

    def __iter__(self) -> Any:
        return iter(())

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<nios-openalgo-stub {self._name}>"


def _module(name: str, *, package: bool = False, doc: str | None = None) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__["__nios_openalgo_compat__"] = True
    if doc:
        mod.__doc__ = doc
    if package:
        mod.__path__ = []  # type: ignore[attr-defined]
        mod.__package__ = name
    mod.__dict__.setdefault("__getattr__", lambda item: _Stub(f"{name}.{item}"))
    return mod


def _register(name: str, mod: types.ModuleType, *, replace: bool = False) -> types.ModuleType:
    existing = sys.modules.get(name)
    if existing is not None and not replace:
        _STATE[name] = "existing"
        return existing
    sys.modules[name] = mod
    _STATE[name] = "shim"
    return mod


def _real_module_available(name: str) -> bool:
    if name in sys.modules:
        return not getattr(sys.modules[name], "__nios_openalgo_compat__", False)
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Ring 0 credential bridge (encrypted key_vault, env fallback for paper)
# ---------------------------------------------------------------------------

_ENV_SUFFIX = {
    "dhan": "DHAN",
    "zerodha": "ZERODHA",
    "angel": "ANGEL",
    "upstox": "UPSTOX",
}

_CREDENTIAL_KEYS = (
    "api_key",
    "api_secret",
    "access_token",
    "refresh_token",
    "client_id",
    "user_id",
)


def _key_vault_credentials(venue: str) -> dict[str, str]:
    """Read one venue's credentials from Ring 0's encrypted key vault.

    ``broker_keys_db`` has changed shape across donor revisions, so every known
    accessor is probed defensively.  Any failure degrades to an empty mapping --
    the env fallback then decides whether the venue is usable.
    """

    venue = venue.lower()
    try:
        from ring0_vault.silos.key_vault import broker_keys_db as vault
    except Exception:  # pragma: no cover - key vault always present in Ring 0
        return {}

    candidates: list[Any] = []
    # ``broker_keys_db`` logs a full SQLAlchemy traceback when no DB bind is
    # configured (the normal state on an air-gapped host).  Silence it while
    # probing: absence of a key vault row is not an error, it just means this
    # venue is running in paper mode.
    vault_logger = logging.getLogger(
        "ring0_vault.silos.key_vault.broker_keys_db"
    )
    _saved_level = vault_logger.level
    vault_logger.setLevel(logging.CRITICAL + 1)
    for fn_name in (
        "get_broker_credentials",
        "get_credentials",
        "load_broker_keys",
        "fetch_credentials",
        "get_broker_keys",
        "read_broker_keys",
    ):
        fn = getattr(vault, fn_name, None)
        if not callable(fn):
            continue
        for call in (lambda f=fn: f(venue), lambda f=fn: f(venue, _ENV_SUFFIX[venue])):
            try:
                candidates.append(call())
            except Exception:
                continue
    for fn_name in ("all_broker_keys", "load_all", "get_all"):
        fn = getattr(vault, fn_name, None)
        if callable(fn):
            try:
                blob = fn()
            except Exception:
                continue
            if isinstance(blob, Mapping):
                candidates.append(blob.get(venue) or blob.get(venue.upper()))

    vault_logger.setLevel(_saved_level)

    for candidate in candidates:
        flat = _flatten_credentials(candidate)
        if flat:
            return flat
    return {}


def _flatten_credentials(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, str):
        return {"access_token": value} if value else {}
    if isinstance(value, Mapping):
        out: dict[str, str] = {}
        for key, item in value.items():
            name = str(key).lower()
            if isinstance(item, (str, int, float)) and item not in ("", None):
                out[name] = str(item)
        if not out and "credentials" in value:
            return _flatten_credentials(value["credentials"])
        return out
    out = {}
    for key in _CREDENTIAL_KEYS:
        item = getattr(value, key, None)
        if isinstance(item, (str, int, float)) and item not in ("", None):
            out[key] = str(item)
    return out


def broker_credentials(venue: str) -> dict[str, str]:
    """Return ``{api_key, api_secret, access_token, ...}`` for ``venue``.

    Order of precedence: Ring 0 encrypted key vault, then ``NIOS_<VENUE>_*``
    environment variables (paper bootstrap only).
    """

    venue = venue.lower()
    suffix = _ENV_SUFFIX.get(venue, venue.upper())
    creds = _key_vault_credentials(venue)
    for key in _CREDENTIAL_KEYS:
        env_name = f"NIOS_{suffix}_{key.upper()}"
        if env_name in os.environ and os.environ[env_name]:
            creds.setdefault(key, os.environ[env_name])
    return creds


def has_live_credentials(venue: str) -> bool:
    """True when a venue has enough material to sign a live session."""

    creds = broker_credentials(venue)
    return bool(creds.get("api_key") and creds.get("access_token"))


# ---------------------------------------------------------------------------
# Instrument / token database (seeded NSE/BSE/MCX table + deterministic fallback)
# ---------------------------------------------------------------------------

_SEEDED_INSTRUMENTS: dict[tuple[str, str], dict[str, Any]] = {}


def _seed(
    symbol: str,
    exchange: str,
    token: str,
    *,
    brsymbol: str | None = None,
    name: str | None = None,
    brexchange: str | None = None,
    instrumenttype: str = "EQ",
    lotsize: int = 1,
    tick_size: float = 0.05,
) -> None:
    _SEEDED_INSTRUMENTS[(symbol.upper(), exchange.upper())] = {
        "symbol": symbol.upper(),
        "brsymbol": (brsymbol or symbol).upper(),
        "name": name or symbol.upper(),
        "exchange": exchange.upper(),
        "brexchange": (brexchange or exchange).upper(),
        "token": str(token),
        "expiry": "",
        "strike": "0",
        "lotsize": str(lotsize),
        "instrumenttype": instrumenttype,
        "tick_size": str(tick_size),
    }


# Cash equities / ETFs used by the India silo's sweep + reserve programs
# (``NIFTYBEES.NS`` compounder sweep, ``LIQUIDBEES.NS`` reserve yield).
_seed("RELIANCE", "NSE", "2885", name="RELIANCE INDUSTRIES LTD")
_seed("NIFTYBEES", "NSE", "10576", name="NIPPON INDIA ETF NIFTY BEES")
_seed("LIQUIDBEES", "NSE", "8084", name="NIPPON INDIA ETF LIQUID BEES", tick_size=0.001)
_seed("BANKBEES", "NSE", "10408", name="NIPPON INDIA ETF BANK BEES")
_seed("JUNIORBEES", "NSE", "10646", name="NIPPON INDIA ETF JUNIOR BEES")
_seed("GOLDBEES", "NSE", "8083", name="NIPPON INDIA ETF GOLD BEES", tick_size=0.01)
_seed("SBIN", "NSE", "3045", name="STATE BANK OF INDIA")
_seed("INFY", "NSE", "1594", name="INFOSYS LTD")
_seed("TCS", "NSE", "11536", name="TATA CONSULTANCY SERVICES")
_seed("HDFCBANK", "NSE", "1333", name="HDFC BANK LTD")
_seed("ICICIBANK", "NSE", "4963", name="ICICI BANK LTD")
_seed("AXISBANK", "NSE", "5900", name="AXIS BANK LTD")
_seed("KOTAKBANK", "NSE", "1922", name="KOTAK MAHINDRA BANK")
_seed("LT", "NSE", "11483", name="LARSEN & TOUBRO LTD")
_seed("ITC", "NSE", "1660", name="ITC LTD")
_seed("BHARTIARTL", "NSE", "10604", name="BHARTI AIRTEL LTD")
_seed("MARUTI", "NSE", "10999", name="MARUTI SUZUKI INDIA LTD")
_seed("TATAMOTORS", "NSE", "3456", name="TATA MOTORS LTD")
_seed("SUNPHARMA", "NSE", "3351", name="SUN PHARMACEUTICAL IND")
_seed("ASIANPAINT", "NSE", "236", name="ASIAN PAINTS LTD")
# Indices (F&O underlyings)
_seed("NIFTY", "NSE_INDEX", "13", brsymbol="NIFTY 50", brexchange="NSE", instrumenttype="INDEX", tick_size=0.05)
_seed("BANKNIFTY", "NSE_INDEX", "25", brsymbol="NIFTY BANK", brexchange="NSE", instrumenttype="INDEX")
_seed("FINNIFTY", "NSE_INDEX", "27", brsymbol="NIFTY FIN SERVICE", brexchange="NSE", instrumenttype="INDEX")
_seed("MIDCPNIFTY", "NSE_INDEX", "442", brsymbol="NIFTY MID SELECT", brexchange="NSE", instrumenttype="INDEX")
_seed("SENSEX", "BSE_INDEX", "1", brsymbol="SENSEX", brexchange="BSE", instrumenttype="INDEX")
_seed("BANKEX", "BSE_INDEX", "12", brsymbol="BANKEX", brexchange="BSE", instrumenttype="INDEX")
# BSE cash
_seed("RELIANCE", "BSE", "500325", name="RELIANCE INDUSTRIES LTD", brexchange="BSE")
_seed("TCS", "BSE", "532540", name="TATA CONSULTANCY SERVICES", brexchange="BSE")
# MCX commodity futures
_seed("GOLDM", "MCX", "114", brsymbol="GOLDM25AUGFUT", brexchange="MCX", instrumenttype="FUT", lotsize=10, tick_size=1.0)
_seed("SILVERM", "MCX", "125", brsymbol="SILVERM25AUGFUT", brexchange="MCX", instrumenttype="FUT", lotsize=5, tick_size=1.0)
_seed("CRUDEOILM", "MCX", "136", brsymbol="CRUDEOILM25AUGFUT", brexchange="MCX", instrumenttype="FUT", lotsize=10, tick_size=1.0)


def _normalise_exchange(exchange: str) -> str:
    return str(exchange or "NSE").upper().replace(" ", "_")


def _normalise_symbol(symbol: str) -> str:
    text = str(symbol or "").upper().strip()
    for suffix in (".NS", ".BO", ".NSE", ".BSE"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def seeded_instrument(symbol: str, exchange: str = "NSE") -> dict[str, Any] | None:
    """Return a copy of the seeded instrument record, or ``None``."""

    key = (_normalise_symbol(symbol), _normalise_exchange(exchange))
    record = _SEEDED_INSTRUMENTS.get(key)
    return dict(record) if record else None


def resolve_token(symbol: str, exchange: str = "NSE") -> str:
    """Resolve a broker security token.

    Seeded instruments return their published token.  Anything else gets a
    stable, collision-resistant synthetic token derived from
    ``sha256(EXCHANGE|SYMBOL)`` so order transformation never blocks on a
    missing master-contract download.  Synthetic tokens are tagged in
    :func:`instrument_record` via ``synthetic=True``.
    """

    seeded = seeded_instrument(symbol, exchange)
    if seeded is not None:
        return seeded["token"]
    digest = hashlib.sha256(
        f"{_normalise_exchange(exchange)}|{_normalise_symbol(symbol)}".encode("utf-8")
    ).hexdigest()
    return str(int(digest[:8], 16))


def instrument_record(symbol: str, exchange: str = "NSE") -> dict[str, Any]:
    """Full instrument record (seeded, else synthetic)."""

    seeded = seeded_instrument(symbol, exchange)
    if seeded is not None:
        seeded["synthetic"] = False
        return seeded
    symbol_n = _normalise_symbol(symbol)
    exchange_n = _normalise_exchange(exchange)
    return {
        "symbol": symbol_n,
        "brsymbol": symbol_n,
        "name": symbol_n,
        "exchange": exchange_n,
        "brexchange": "NSE" if exchange_n.startswith("NSE") else exchange_n,
        "token": resolve_token(symbol_n, exchange_n),
        "expiry": "",
        "strike": "0",
        "lotsize": "1",
        "instrumenttype": "EQ",
        "tick_size": "0.05",
        "synthetic": True,
    }


# ---------------------------------------------------------------------------
# Shared httpx pool (Ring 0 owns exactly one connection pool per process)
# ---------------------------------------------------------------------------

_HTTP_LOCK = threading.Lock()
_HTTP_CLIENT: Any = None


def shared_http_client() -> Any:
    """Return the process-wide ``httpx.Client`` used by every India adapter."""

    global _HTTP_CLIENT
    with _HTTP_LOCK:
        if _HTTP_CLIENT is not None and not getattr(_HTTP_CLIENT, "is_closed", False):
            return _HTTP_CLIENT
        import httpx

        _HTTP_CLIENT = httpx.Client(
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
            headers={"User-Agent": "nios-ring0-india/2.0", "Accept": "application/json"},
        )
        return _HTTP_CLIENT


# ---------------------------------------------------------------------------
# utils.* bridges
# ---------------------------------------------------------------------------


def _build_utils() -> None:
    pkg = _module("utils", package=True, doc="NIOS Ring 0 bridge for OpenAlgo utils.")
    _register("utils", pkg)

    log_mod = _module("utils.logging", doc="Ring 0 structured logging for broker adapters.")
    _FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    _configured: set[str] = set()

    def get_logger(name: str = "nios.broker", level: int | None = None) -> logging.Logger:
        logger = logging.getLogger(name)
        if name not in _configured:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(_FORMAT))
            if not logger.handlers:
                logger.addHandler(handler)
            logger.propagate = False
            _configured.add(name)
        logger.setLevel(level if level is not None else logging.INFO)
        return logger

    log_mod.get_logger = get_logger  # type: ignore[attr-defined]
    log_mod.setup_logging = lambda level=logging.INFO: get_logger("nios.broker", level)  # type: ignore[attr-defined]
    log_mod.LOG_FORMAT = _FORMAT  # type: ignore[attr-defined]
    log_mod.get_httpx_logger = lambda: get_logger("nios.broker.http")  # type: ignore[attr-defined]
    _register("utils.logging", log_mod)

    const = _module("utils.constants", doc="Ring 0 runtime constants for India adapters.")
    const.API_KEY = os.environ.get("NIOS_BROKER_API_KEY", "")  # type: ignore[attr-defined]
    const.API_SECRET = os.environ.get("NIOS_BROKER_API_SECRET", "")  # type: ignore[attr-defined]
    const.BROKER_API_KEY = const.API_KEY  # type: ignore[attr-defined]
    const.BROKER_API_SECRET = const.API_SECRET  # type: ignore[attr-defined]
    const.HOST_SERVER = os.environ.get("NIOS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    const.HOST_PORT = os.environ.get("NIOS_PORT", "5000")  # type: ignore[attr-defined]
    const.OPENALGO_HOST = f"http://{const.HOST_SERVER}:{const.HOST_PORT}"  # type: ignore[attr-defined]
    const.DATA_DIR = os.environ.get("NIOS_DATA_DIR", os.path.join(os.getcwd(), "data"))  # type: ignore[attr-defined]
    const.LOG_DIR = os.environ.get("NIOS_LOG_DIR", os.path.join(os.getcwd(), "log"))  # type: ignore[attr-defined]
    const.RATE_LIMIT_ORDERS_PER_SEC = 10  # type: ignore[attr-defined]
    const.RATE_LIMIT_MAX_RETRIES = 3  # type: ignore[attr-defined]
    const.IST = timezone(timedelta(hours=5, minutes=30))  # type: ignore[attr-defined]
    const.MARKET_OPEN = (9, 15)  # type: ignore[attr-defined]
    const.MARKET_CLOSE = (15, 30)  # type: ignore[attr-defined]
    _register("utils.constants", const)

    http_mod = _module("utils.httpx_client", doc="Ring 0 shared httpx pool bridge.")
    http_mod.get_httpx_client = lambda *a, **k: shared_http_client()  # type: ignore[attr-defined]
    http_mod.shared_client = shared_http_client  # type: ignore[attr-defined]

    def request_may_have_been_sent(exc: BaseException | None = None) -> bool:
        """True when a transport failure is ambiguous (the venue may have acked).

        Conservative by design: an ambiguous POST must be reconciled, never
        blindly retried (see ``sec_009`` idempotency keys).
        """

        import httpx

        if exc is None:
            return False
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return False
        if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
            return True
        if isinstance(exc, httpx.TransportError):
            return True
        return False

    http_mod.request_may_have_been_sent = request_may_have_been_sent  # type: ignore[attr-defined]
    http_mod.close_client = lambda: None  # type: ignore[attr-defined]
    _register("utils.httpx_client", http_mod)

    mpp = _module("utils.mpp_slab", doc="Market-price-protection slab lookup.")

    def get_instrument_type_from_symbol(symbol: str) -> str:
        text = str(symbol or "").upper()
        if text.endswith("CE") or text.endswith("PE"):
            return "OPT"
        if text.endswith("FUT"):
            return "FUT"
        if "NIFTY" in text or "SENSEX" in text:
            return "INDEX"
        return "EQ"

    def get_mpp_percentage(instrument_type: str, exchange: str | None = None) -> float:
        # SEBI MPP slabs: 2% (F&O) / 5% (index options) / 10% (equity) caps.
        table = {"EQ": 0.10, "ETF": 0.05, "FUT": 0.02, "OPT": 0.05, "INDEX": 0.05}
        return table.get(str(instrument_type or "EQ").upper(), 0.10)

    mpp.get_instrument_type_from_symbol = get_instrument_type_from_symbol  # type: ignore[attr-defined]
    mpp.get_mpp_percentage = get_mpp_percentage  # type: ignore[attr-defined]
    mpp.MPP_SLABS = {"EQ": 0.10, "FUT": 0.02, "OPT": 0.05}  # type: ignore[attr-defined]
    _register("utils.mpp_slab", mpp)

    pos = _module("utils.position_read", doc="Position book read with fail-closed guard.")

    class PositionReadError(RuntimeError):
        """Raised when the venue position book cannot be read."""

    #: venue -> last known position-book read result (``None`` = never read).
    _POSITION_BOOK_OK: dict[str, bool | None] = {}

    def _venue_from_frame() -> str:
        """Derive the venue from the nearest caller module path."""

        import inspect

        frame = inspect.currentframe()
        try:
            caller = frame.f_back if frame is not None else None
            for _ in range(12):
                if caller is None:
                    break
                module = caller.f_globals.get("__name__", "")
                for venue in INDIA_VENUES:
                    if f".{venue}." in module or module.endswith(f".{venue}"):
                        return venue
                caller = caller.f_back
        finally:
            del frame
        return ""

    def read_position_book(
        venue: str = "",
        loader: Any = None,
        ok_flag: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Read a venue position book and record whether the read succeeded.

        ``loader`` is the adapter's own ``get_positions`` callable; ``ok_flag``
        is a zero-arg predicate the adapter supplies to signal a fresh, trusted
        book.  A failed read is recorded so the smart-order guard can refuse
        rather than size against a stale book.
        """

        resolved = str(venue or _venue_from_frame())
        book: list[dict[str, Any]] = []
        ok = True
        if callable(loader):
            try:
                result = loader()
                book = list(result or [])
            except Exception:
                ok = False
                _LOG.warning("position book read failed for %s", resolved, exc_info=True)
        if callable(ok_flag):
            try:
                ok = bool(ok and ok_flag())
            except Exception:
                ok = False
        if resolved:
            _POSITION_BOOK_OK[resolved] = ok
        if not ok:
            _LOG.debug("position book for %s is stale", resolved)
        return book

    def refuse_smart_order_on_read_failure(func: Any = None) -> Any:
        """Decorator: refuse a smart order when the position book is stale.

        Used as ``@refuse_smart_order_on_read_failure`` on the adapter's smart
        order entry point.  The guard only fires when a *previous* read for the
        same venue is known to have failed -- never at import time.
        """

        import functools

        def _decorate(target: Any) -> Any:
            venue = _venue_from_frame()

            @functools.wraps(target)
            def _guarded(*args: Any, **kwargs: Any) -> Any:
                state = _POSITION_BOOK_OK.get(venue)
                if state is False:
                    raise PositionReadError(
                        f"position book unavailable for {venue or 'venue'}; smart order refused"
                    )
                return target(*args, **kwargs)

            return _guarded

        if func is None:
            return _decorate
        if callable(func):
            return _decorate(func)
        return func

    def position_book_status() -> dict[str, bool | None]:
        return dict(_POSITION_BOOK_OK)

    pos.PositionReadError = PositionReadError  # type: ignore[attr-defined]
    pos.read_position_book = read_position_book  # type: ignore[attr-defined]
    pos.refuse_smart_order_on_read_failure = refuse_smart_order_on_read_failure  # type: ignore[attr-defined]
    pos.position_book_status = position_book_status  # type: ignore[attr-defined]
    _register("utils.position_read", pos)

    guard = _module("utils.smart_order_guard", doc="Per-symbol locks + position cache.")

    class PositionBookCache:
        """Small TTL cache for a venue position book."""

        def __init__(self, ttl_seconds: float = 2.0) -> None:
            self._ttl = float(ttl_seconds)
            self._store: dict[str, tuple[float, Any]] = {}
            self._lock = threading.RLock()

        def get(self, key: str = "default") -> Any:
            with self._lock:
                hit = self._store.get(key)
            if hit is None:
                return None
            stamped, value = hit
            if time.monotonic() - stamped > self._ttl:
                return None
            return value

        def set(self, value: Any, key: str = "default") -> None:
            with self._lock:
                self._store[key] = (time.monotonic(), value)

        def invalidate(self, key: str | None = None) -> None:
            with self._lock:
                if key is None:
                    self._store.clear()
                else:
                    self._store.pop(key, None)

    class SymbolLocks:
        """Re-entrant per-symbol locks preventing crossed smart orders."""

        def __init__(self, *args: Any, name: str = "", **kwargs: Any) -> None:
            self.name = name
            self._locks: dict[str, threading.RLock] = {}
            self._guard = threading.RLock()

        def lock_for(self, symbol: str) -> threading.RLock:
            with self._guard:
                return self._locks.setdefault(str(symbol).upper(), threading.RLock())

        def acquire(self, symbol: str) -> bool:
            return self.lock_for(symbol).acquire()

        def release(self, symbol: str) -> None:
            self.lock_for(symbol).release()

        def __enter__(self) -> "SymbolLocks":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    guard.PositionBookCache = PositionBookCache  # type: ignore[attr-defined]
    guard.SymbolLocks = SymbolLocks  # type: ignore[attr-defined]
    _register("utils.smart_order_guard", guard)

    bp = _module("utils.broker_backpressure", doc="Broker busy detection + retry-after.")

    class BrokerBusyError(RuntimeError):
        """Raised when the venue is applying backpressure."""

    @dataclass(slots=True)
    class BusyResponse:
        status: str = "busy"
        retry_after: float = 1.0
        message: str = "broker busy"
        venue: str = ""

        def to_dict(self) -> dict[str, Any]:
            return {
                "status": self.status,
                "retry_after": self.retry_after,
                "message": self.message,
                "venue": self.venue,
            }

    def busy_response(*, venue: str = "", retry_after: float = 1.0, message: str = "broker busy", **_: Any) -> BusyResponse:
        return BusyResponse(retry_after=retry_after, message=message, venue=venue)

    def cap_server_delay(delay: float, *, maximum: float = 5.0) -> float:
        try:
            value = float(delay)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(value, float(maximum)))

    bp.BrokerBusyError = BrokerBusyError  # type: ignore[attr-defined]
    bp.BusyResponse = BusyResponse  # type: ignore[attr-defined]
    bp.busy_response = busy_response  # type: ignore[attr-defined]
    bp.cap_server_delay = cap_server_delay  # type: ignore[attr-defined]
    _register("utils.broker_backpressure", bp)

    lazy = _module("utils.lazy", doc="Lazy module proxy.")

    class LazyModule:
        def __init__(self, name: str) -> None:
            self._name = name
            self._module: types.ModuleType | None = None

        def _load(self) -> types.ModuleType:
            if self._module is None:
                self._module = importlib.import_module(self._name)
            return self._module

        def __getattr__(self, item: str) -> Any:
            return getattr(self._load(), item)

    lazy.LazyModule = LazyModule  # type: ignore[attr-defined]
    lazy.lazy_import = lambda name: LazyModule(name)  # type: ignore[attr-defined]
    _register("utils.lazy", lazy)

    runtime = _module("utils.runtime", doc="Process lifecycle flags for streaming adapters.")
    _shutdown = threading.Event()
    _hooks: list[Any] = []

    runtime.shutdown_event = _shutdown  # type: ignore[attr-defined]
    runtime.is_shutting_down = _shutdown.is_set  # type: ignore[attr-defined]
    runtime.register_shutdown_hook = lambda fn: _hooks.append(fn) or fn  # type: ignore[attr-defined]

    def _request_shutdown() -> None:
        _shutdown.set()
        for hook in tuple(_hooks):
            try:
                hook()
            except Exception:
                _LOG.exception("shutdown hook failed")

    runtime.request_shutdown = _request_shutdown  # type: ignore[attr-defined]
    runtime.get_runtime = lambda: runtime  # type: ignore[attr-defined]
    _register("utils.runtime", runtime)

    pkg.get_logger = get_logger  # type: ignore[attr-defined]
    pkg.runtime = runtime  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# database.* bridges
# ---------------------------------------------------------------------------


def _build_database() -> None:
    pkg = _module("database", package=True, doc="NIOS Ring 0 bridge for OpenAlgo database.")
    _register("database", pkg)

    auth = _module("database.auth_db", doc="Credential/auth bridge onto Ring 0 key_vault.")

    def get_auth_token(*, broker: str = "", api_key: str = "", user_id: str = "", **_: Any) -> str:
        venue = (broker or "").lower()
        if venue:
            return broker_credentials(venue).get("access_token", "")
        for candidate in INDIA_VENUES:
            token = broker_credentials(candidate).get("access_token", "")
            if token:
                return token
        return ""

    def get_user_id(*, broker: str = "", **_: Any) -> str:
        venue = (broker or "").lower()
        if venue:
            creds = broker_credentials(venue)
            return creds.get("user_id") or creds.get("client_id", "")
        return os.environ.get("NIOS_BROKER_USER_ID", "")

    def _known_api_keys() -> set[str]:
        keys = {k.strip() for k in os.environ.get("NIOS_API_KEYS", "").split(",") if k.strip()}
        for venue in INDIA_VENUES:
            value = broker_credentials(venue).get("api_key", "")
            if value:
                keys.add(value)
        return keys

    def verify_api_key(api_key: str, **_: Any) -> bool:
        known = _known_api_keys()
        if not known:
            # Paper bootstrap: no keys provisioned yet.  Live dispatch is gated
            # separately by ``has_live_credentials`` + ``confirm_live``.
            return bool(api_key)
        return api_key in known

    def upsert_auth(*args: Any, **kwargs: Any) -> bool:
        return True

    def get_broker_name(*_: Any, **__: Any) -> str:
        return os.environ.get("NIOS_BROKER", "")

    auth.get_auth_token = get_auth_token  # type: ignore[attr-defined]
    auth.get_user_id = get_user_id  # type: ignore[attr-defined]
    auth.verify_api_key = verify_api_key  # type: ignore[attr-defined]
    auth.upsert_auth = upsert_auth  # type: ignore[attr-defined]
    auth.get_broker_name = get_broker_name  # type: ignore[attr-defined]
    auth.has_live_credentials = has_live_credentials  # type: ignore[attr-defined]
    _register("database.auth_db", auth)

    tok = _module("database.token_db", doc="Seeded NSE/BSE/MCX token database.")

    def get_symbol_info(symbol: str, exchange: str = "NSE", **_: Any) -> dict[str, Any]:
        return instrument_record(symbol, exchange)

    def get_br_symbol(symbol: str, exchange: str = "NSE", **_: Any) -> str:
        return str(instrument_record(symbol, exchange)["brsymbol"])

    def get_oa_symbol(brsymbol: str, exchange: str = "NSE", **_: Any) -> str:
        target = str(brsymbol or "").upper()
        exchange_n = _normalise_exchange(exchange)
        for (symbol, exch), record in _SEEDED_INSTRUMENTS.items():
            if exch == exchange_n and record["brsymbol"] == target:
                return symbol
        return target

    def get_symbol(token: str, exchange: str = "NSE", **_: Any) -> str:
        target = str(token)
        exchange_n = _normalise_exchange(exchange)
        for (symbol, exch), record in _SEEDED_INSTRUMENTS.items():
            if exch == exchange_n and record["token"] == target:
                return symbol
        return ""

    def get_token(symbol: str, exchange: str = "NSE", **_: Any) -> str:
        return resolve_token(symbol, exchange)

    def get_lot_size(symbol: str, exchange: str = "NSE", **_: Any) -> int:
        return int(instrument_record(symbol, exchange)["lotsize"])

    def get_tick_size(symbol: str, exchange: str = "NSE", **_: Any) -> float:
        return float(instrument_record(symbol, exchange)["tick_size"])

    def search_symbols(query: str, exchange: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        needle = str(query or "").upper()
        out: list[dict[str, Any]] = []
        for (symbol, exch), record in _SEEDED_INSTRUMENTS.items():
            if exchange and exch != _normalise_exchange(exchange):
                continue
            if needle and needle not in symbol:
                continue
            out.append(dict(record))
            if len(out) >= limit:
                break
        return out

    tok.get_symbol_info = get_symbol_info  # type: ignore[attr-defined]
    tok.get_br_symbol = get_br_symbol  # type: ignore[attr-defined]
    tok.get_oa_symbol = get_oa_symbol  # type: ignore[attr-defined]
    tok.get_symbol = get_symbol  # type: ignore[attr-defined]
    tok.get_token = get_token  # type: ignore[attr-defined]
    tok.get_lot_size = get_lot_size  # type: ignore[attr-defined]
    tok.get_tick_size = get_tick_size  # type: ignore[attr-defined]
    tok.search_symbols = search_symbols  # type: ignore[attr-defined]
    tok.SEEDED_INSTRUMENTS = _SEEDED_INSTRUMENTS  # type: ignore[attr-defined]
    _register("database.token_db", tok)

    enh = _module("database.token_db_enhanced", doc="Cached token lookups.")
    enh.get_symbol_info = get_symbol_info  # type: ignore[attr-defined]
    enh.search_symbols = search_symbols  # type: ignore[attr-defined]
    enh.bulk_lookup = lambda items, **_: [get_symbol_info(s, e) for s, e in items]  # type: ignore[attr-defined]
    enh.invalidate_cache = lambda *a, **k: None  # type: ignore[attr-defined]
    enh.cache_stats = lambda: {"entries": len(_SEEDED_INSTRUMENTS), "hit_rate": 0.0}  # type: ignore[attr-defined]
    _register("database.token_db_enhanced", enh)

    api_log = _module("database.apilog_db", doc="Append-only API audit log (Ring 0).")

    @dataclass(slots=True)
    class ApiLogRecord:
        venue: str
        endpoint: str
        status: str = "ok"
        payload: dict[str, Any] = field(default_factory=dict)
        created_ns: int = field(default_factory=time.time_ns)

    _API_LOG: list[ApiLogRecord] = []

    def log_api_call(venue: str = "", endpoint: str = "", status: str = "ok", payload: Mapping[str, Any] | None = None, **_: Any) -> ApiLogRecord:
        record = ApiLogRecord(venue=venue, endpoint=endpoint, status=status, payload=dict(payload or {}))
        _API_LOG.append(record)
        if len(_API_LOG) > 10_000:
            del _API_LOG[: len(_API_LOG) - 10_000]
        return record

    api_log.ApiLogRecord = ApiLogRecord  # type: ignore[attr-defined]
    api_log.log_api_call = log_api_call  # type: ignore[attr-defined]
    api_log.get_api_logs = lambda limit: list(_API_LOG[-int(limit):])  # type: ignore[attr-defined]
    _register("database.apilog_db", api_log)

    eng = _module("database.engine_factory", doc="SQLite-backed engine fallback (no psycopg).")
    _ENGINE: Any = None

    def get_engine(*_: Any, **__: Any) -> Any:
        nonlocal _ENGINE
        if _ENGINE is None:
            from sqlalchemy import create_engine

            _ENGINE = create_engine("sqlite+pysqlite:///:memory:", future=True)
        return _ENGINE

    def get_session() -> Any:
        from sqlalchemy.orm import Session

        return Session(get_engine())

    class _SessionScope:
        def __enter__(self) -> Any:
            self._session = get_session()
            return self._session

        def __exit__(self, *exc: Any) -> None:
            self._session.close()

    eng.get_engine = get_engine  # type: ignore[attr-defined]
    eng.create_engine_from_env = get_engine  # type: ignore[attr-defined]
    eng.get_session = get_session  # type: ignore[attr-defined]
    eng.session_scope = lambda *a, **k: _SessionScope()  # type: ignore[attr-defined]
    _register("database.engine_factory", eng)

    cal = _module("database.market_calendar_db", doc="NSE/BSE/MCX session calendar (IST).")

    _HOLIDAYS_2026 = {
        "2026-01-26",
        "2026-03-04",
        "2026-03-21",
        "2026-04-03",
        "2026-04-14",
        "2026-05-01",
        "2026-08-15",
        "2026-10-02",
        "2026-10-21",
        "2026-11-09",
        "2026-12-25",
    }

    def _ist_now(at: datetime | None = None) -> datetime:
        return (at or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=5, minutes=30)))

    def is_trading_day(day: datetime | str | None = None, **_: Any) -> bool:
        if day is None:
            day = _ist_now()
        if isinstance(day, str):
            key = day[:10]
        else:
            key = _ist_now(day).strftime("%Y-%m-%d")
        return key not in _HOLIDAYS_2026 and datetime.strptime(key, "%Y-%m-%d").weekday() < 5

    def is_market_open(exchange: str = "NSE", at: datetime | None = None, **_: Any) -> bool:
        now = _ist_now(at)
        if not is_trading_day(now):
            return False
        open_h, open_m = (9, 15) if str(exchange).upper() != "MCX" else (9, 0)
        close_h, close_m = (15, 30) if str(exchange).upper() != "MCX" else (23, 30)
        return (now.hour, now.minute) >= (open_h, open_m) and (now.hour, now.minute) <= (close_h, close_m)

    cal.is_market_open = is_market_open  # type: ignore[attr-defined]
    cal.is_trading_day = is_trading_day  # type: ignore[attr-defined]
    cal.trading_holidays = lambda year: sorted(d for d in _HOLIDAYS_2026 if d.startswith(str(year)))  # type: ignore[attr-defined]
    cal.next_market_open = lambda exchange="NSE", at=None: _ist_now(at) + timedelta(days=1)  # type: ignore[attr-defined]
    _register("database.market_calendar_db", cal)

    pkg.token_db = tok  # type: ignore[attr-defined]
    pkg.auth_db = auth  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# services.* / websocket_proxy.* / extensions / logzero bridges
# ---------------------------------------------------------------------------


def _build_services() -> None:
    pkg = _module("services", package=True, doc="NIOS Ring 0 bridge for OpenAlgo services.")
    _register("services", pkg)

    quotes = _module("services.quotes_service", doc="Deterministic paper quotes for order sizing.")

    def get_multiquotes(symbols: Sequence[str] | str, exchange: str = "NSE", **_: Any) -> list[dict[str, Any]]:
        items = [symbols] if isinstance(symbols, str) else list(symbols or ())
        out: list[dict[str, Any]] = []
        for symbol in items:
            record = instrument_record(symbol, exchange)
            digest = int(hashlib.sha256(f"{exchange}|{symbol}".encode()).hexdigest()[:8], 16)
            # Deterministic paper mark: seeded from the token, never a market claim.
            last = round(100.0 + (digest % 90_000) / 100.0, 2)
            tick = float(record["tick_size"])
            out.append(
                {
                    "symbol": record["symbol"],
                    "exchange": record["exchange"],
                    "token": record["token"],
                    "ltp": last,
                    "bid": round(last - tick, 4),
                    "ask": round(last + tick, 4),
                    "volume": 0,
                    "source": "paper",
                }
            )
        return out

    quotes.get_multiquotes = get_multiquotes  # type: ignore[attr-defined]
    quotes.get_quote = lambda symbol, exchange="NSE", **k: get_multiquotes([symbol], exchange)[0]  # type: ignore[attr-defined]
    _register("services.quotes_service", quotes)
    pkg.quotes_service = quotes  # type: ignore[attr-defined]


def _build_websocket_proxy() -> None:
    pkg = _module("websocket_proxy", package=True, doc="NIOS Ring 0 bridge for websocket_proxy.")
    _register("websocket_proxy", pkg)

    base = _module("websocket_proxy.base_adapter", doc="Base streaming adapter contract.")

    class BaseBrokerWebSocketAdapter:
        """Minimal streaming adapter base: no socket is opened until ``connect``."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.connected = False
            self.subscriptions: dict[str, Any] = {}
            self.user_id = kwargs.get("user_id", "")
            self.logger = logging.getLogger(f"nios.broker.ws.{type(self).__name__}")

        def connect(self, *args: Any, **kwargs: Any) -> bool:
            self.connected = True
            return True

        def disconnect(self, *args: Any, **kwargs: Any) -> None:
            self.connected = False
            self.subscriptions.clear()

        def subscribe(self, symbol: str, exchange: str = "NSE", mode: str = "LTP", **_: Any) -> bool:
            self.subscriptions[f"{exchange}:{symbol}"] = mode
            return True

        def unsubscribe(self, symbol: str, exchange: str = "NSE", **_: Any) -> bool:
            self.subscriptions.pop(f"{exchange}:{symbol}", None)
            return True

        def publish_tick(self, *args: Any, **kwargs: Any) -> None:
            return None

        def publish_order_update(self, *args: Any, **kwargs: Any) -> None:
            return None

        def publish_position_update(self, *args: Any, **kwargs: Any) -> None:
            return None

        def get_subscriptions(self) -> dict[str, Any]:
            return dict(self.subscriptions)

    base.BaseBrokerWebSocketAdapter = BaseBrokerWebSocketAdapter  # type: ignore[attr-defined]
    _register("websocket_proxy.base_adapter", base)

    mapping = _module("websocket_proxy.mapping", doc="OpenAlgo <-> broker symbol mapping.")

    class SymbolMapper:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.cache: dict[str, Any] = {}

        def get_br_symbol(self, symbol: str, exchange: str = "NSE") -> str:
            return str(instrument_record(symbol, exchange)["brsymbol"])

        def get_oa_symbol(self, brsymbol: str, exchange: str = "NSE") -> str:
            target = str(brsymbol or "").upper()
            for (symbol, exch), record in _SEEDED_INSTRUMENTS.items():
                if exch == _normalise_exchange(exchange) and record["brsymbol"] == target:
                    return symbol
            return target

        def get_token(self, symbol: str, exchange: str = "NSE") -> str:
            return resolve_token(symbol, exchange)

        def get_symbol_info(self, symbol: str, exchange: str = "NSE") -> dict[str, Any]:
            return instrument_record(symbol, exchange)

        def map_symbol(self, symbol: str, exchange: str = "NSE") -> dict[str, Any]:
            return {
                "symbol": _normalise_symbol(symbol),
                "brsymbol": self.get_br_symbol(symbol, exchange),
                "token": self.get_token(symbol, exchange),
                "exchange": _normalise_exchange(exchange),
            }

    mapping.SymbolMapper = SymbolMapper  # type: ignore[attr-defined]
    _register("websocket_proxy.mapping", mapping)

    order_adapter = _module("websocket_proxy.order_adapter", doc="Order-update stream adapter.")

    def to_openalgo_symbol(brsymbol: str, exchange: str = "NSE") -> str:
        target = str(brsymbol or "").upper()
        for (symbol, exch), record in _SEEDED_INSTRUMENTS.items():
            if exch == _normalise_exchange(exchange) and record["brsymbol"] == target:
                return symbol
        return target

    class BaseOrderUpdateAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.running = False
            self.logger = logging.getLogger(f"nios.broker.orders.{type(self).__name__}")

        def start(self, *args: Any, **kwargs: Any) -> bool:
            self.running = True
            return True

        def stop(self, *args: Any, **kwargs: Any) -> None:
            self.running = False

        def publish_order_update(self, *args: Any, **kwargs: Any) -> None:
            return None

    order_adapter.BaseOrderUpdateAdapter = BaseOrderUpdateAdapter  # type: ignore[attr-defined]
    order_adapter.to_openalgo_symbol = to_openalgo_symbol  # type: ignore[attr-defined]
    _register("websocket_proxy.order_adapter", order_adapter)

    pkg.base_adapter = base  # type: ignore[attr-defined]
    pkg.mapping = mapping  # type: ignore[attr-defined]
    pkg.order_adapter = order_adapter  # type: ignore[attr-defined]


def _build_extensions() -> None:
    mod = _module("extensions", doc="Socket.IO stand-in (no network listener in Ring 0).")

    class _SocketIO:
        def __init__(self) -> None:
            self._handlers: dict[str, Any] = {}

        def emit(self, event: str, data: Any = None, **kwargs: Any) -> None:
            return None

        def on(self, event: str):
            def _decorate(fn: Any) -> Any:
                self._handlers[event] = fn
                return fn

            return _decorate

        def start_background_task(self, target: Any, *args: Any, **kwargs: Any) -> Any:
            return None

        def sleep(self, seconds: float) -> None:
            return None

    mod.socketio = _SocketIO()  # type: ignore[attr-defined]
    mod.socketio_instance = mod.socketio  # type: ignore[attr-defined]
    _register("extensions", mod)


def _build_logzero() -> None:
    mod = _module("logzero", doc="logzero -> stdlib logging shim.")
    _logger = logging.getLogger("nios.broker.logzero")
    if not _logger.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter("%(color)s[%(levelname)1.1s %(asctime)s]%(end_color)s %(message)s"))
        _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)

    class LogFormatter(logging.Formatter):
        DEFAULT_FORMAT = "%(color)s[%(levelname)1.1s %(asctime)s]%(end_color)s %(message)s"

        def __init__(self, fmt: str | None = None, *args: Any, **kwargs: Any) -> None:
            super().__init__(fmt or self.DEFAULT_FORMAT, *args, **kwargs)

    mod.logger = _logger  # type: ignore[attr-defined]
    mod.LogFormatter = LogFormatter  # type: ignore[attr-defined]
    mod.logzero = lambda name=None, **kwargs: _logger if not name else logging.getLogger(name)  # type: ignore[attr-defined]
    mod.setup_logger = mod.logzero  # type: ignore[attr-defined]
    mod.DEBUG = logging.DEBUG  # type: ignore[attr-defined]
    mod.INFO = logging.INFO  # type: ignore[attr-defined]
    mod.WARN = logging.WARNING  # type: ignore[attr-defined]
    mod.ERROR = logging.ERROR  # type: ignore[attr-defined]
    _register("logzero", mod)


# ---------------------------------------------------------------------------
# ``broker`` -> ``ring0_vault.silos.india_brokers`` lazy alias
# ---------------------------------------------------------------------------


class _ResolvedLoader(importlib.abc.Loader):
    """Loader that returns an already-imported module object (identity kept)."""

    def __init__(self, module: types.ModuleType) -> None:
        self._module = module

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        return self._module

    def exec_module(self, module: types.ModuleType) -> None:  # noqa: D401 - no-op
        return None


class _BrokerAliasFinder(importlib.abc.MetaPathFinder):
    """Map ``broker.<venue>...`` onto ``ring0_vault.silos.india_brokers.<venue>...``."""

    alias = _BROKER_ALIAS
    target = _BROKER_TARGET

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != self.alias and not fullname.startswith(self.alias + "."):
            return None
        if fullname in sys.modules:
            return None
        mapped = self.target + fullname[len(self.alias):]
        try:
            module = importlib.import_module(mapped)
        except Exception:
            return None
        return importlib.util.spec_from_loader(fullname, _ResolvedLoader(module))


_FINDER = _BrokerAliasFinder()


def _install_broker_alias() -> None:
    # The alias root must be importable so ``import broker.dhan.api.order_api``
    # resolves through the finder.
    root = _module(_BROKER_ALIAS, package=True, doc="Alias onto ring0_vault.silos.india_brokers.")
    root.__path__ = [os.path.dirname(os.path.abspath(__file__))]  # type: ignore[attr-defined]
    root.__dict__["__nios_openalgo_compat__"] = True
    _register(_BROKER_ALIAS, root)
    if _FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _FINDER)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def install(*, force: bool = False) -> dict[str, str]:
    """Install every bridge.  Idempotent; safe to call from any Ring 0 import."""

    global _INSTALLED, _INSTALLED_AT_NS
    with _LOCK:
        if _INSTALLED and not force:
            return dict(_STATE)
        if force:
            for name in list(_STATE):
                if _STATE[name] == "shim" and getattr(sys.modules.get(name), "__nios_openalgo_compat__", False):
                    sys.modules.pop(name, None)
            _STATE.clear()

        for name in COMPAT_MODULES:
            if name == _BROKER_ALIAS:
                continue
            if _real_module_available(name):
                _STATE[name] = "existing"

        if _STATE.get("utils") != "existing":
            _build_utils()
        if _STATE.get("database") != "existing":
            _build_database()
        if _STATE.get("services") != "existing":
            _build_services()
        if _STATE.get("websocket_proxy") != "existing":
            _build_websocket_proxy()
        if _STATE.get("extensions") != "existing":
            _build_extensions()
        if _STATE.get("logzero") != "existing":
            _build_logzero()
        _install_broker_alias()

        for name in COMPAT_MODULES:
            _STATE.setdefault(name, "shim")

        _INSTALLED = True
        _INSTALLED_AT_NS = time.time_ns()
        _LOG.debug("OpenAlgo compat bridge installed: %s", sorted(_STATE))
        return dict(_STATE)


def uninstall() -> None:
    """Remove every shim this module installed (test isolation helper)."""

    global _INSTALLED
    with _LOCK:
        for name, state in list(_STATE.items()):
            if state != "shim":
                continue
            module = sys.modules.get(name)
            if module is not None and getattr(module, "__nios_openalgo_compat__", False):
                sys.modules.pop(name, None)
        _STATE.clear()
        _INSTALLED = False
        if _FINDER in sys.meta_path:
            sys.meta_path.remove(_FINDER)


def compat_status() -> dict[str, Any]:
    """Report which namespaces are bridged and which were already real."""

    install()
    return {
        "installed": _INSTALLED,
        "installed_at_ns": _INSTALLED_AT_NS,
        "modules": dict(_STATE),
        "shim_count": sum(1 for v in _STATE.values() if v == "shim"),
        "existing_count": sum(1 for v in _STATE.values() if v == "existing"),
        "venues": list(INDIA_VENUES),
        "seeded_instruments": len(_SEEDED_INSTRUMENTS),
        "live_credentials": {v: has_live_credentials(v) for v in INDIA_VENUES},
    }


# Install on first import of this module so the donor trees work even when they
# are reached through an explicit submodule path.
install()
