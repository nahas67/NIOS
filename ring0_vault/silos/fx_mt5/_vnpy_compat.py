"""``vnpy`` / ``vnpy_evo`` compatibility bridge for the Ring 0 FX & futures silo.

The FX silo harvested three donor trees from vn.py:

* ``vnpy_core``   -- the framework core (event engine, trader objects, gateway base)
* ``vnpy_mt5``    -- the MetaTrader 5 gateway (``Mt5Gateway`` / ``Mt5Client``)
* ``vnpy_ctp``    -- the CTP futures gateway (``CtpGateway`` + ``MdApi``/``TdApi``)

Upstream those trees import the ``vnpy`` / ``vnpy_evo`` namespace packages, plus
``talib`` for indicator maths, and they expect a live MetaTrader 5 terminal or
CTP ``.dll``/``.so`` session at import time.  None of that exists on a
bare Ring 0 host.

This module installs the bridge:

* ``vnpy.event``            -> a real, dependency-free ``Event`` / ``EventEngine``
* ``vnpy.trader.constant``  -> ``vnpy_core.constant``
* ``vnpy.trader.object``    -> ``vnpy_core.object``
* ``vnpy.trader.gateway``   -> ``vnpy_core.gateway``
* ``vnpy.trader.utility``   -> ``vnpy_core.utility``
* ``vnpy.trader.event``     -> ``vnpy_core.event``
* ``vnpy_evo.*``            -> the same modules (upstream renamed the package)
* ``talib``                 -> a NumPy indicator shim (no TA-Lib C library needed)

Transport availability is *probed, not assumed*.  ``mt5_terminal_available()``
and ``ctp_library_available()`` report what the host actually has, and
``require_transport()`` raises :class:`TransportUnavailable` when a live session
is asked for without the venue present.  Importing the gateways never requires
a terminal -- only arming a live session does.

Ring ownership: Ring 0 only.  Ring 1 and Ring 2 never import this module.
"""

from __future__ import annotations

import importlib
import logging
import os
import queue
import sys
import threading
import time
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "COMPAT_MODULES",
    "TransportUnavailable",
    "install",
    "uninstall",
    "compat_status",
    "mt5_terminal_available",
    "ctp_library_available",
    "require_transport",
    "transport_status",
    "Event",
    "EventEngine",
    "EVENT_TIMER",
]

_LOG = logging.getLogger("nios.ring0.vnpy_compat")

_LOCK = threading.RLock()
_INSTALLED = False
_STATE: dict[str, str] = {}

COMPAT_MODULES: tuple[str, ...] = (
    "vnpy",
    "vnpy.event",
    "vnpy.trader",
    "vnpy.trader.constant",
    "vnpy.trader.object",
    "vnpy.trader.gateway",
    "vnpy.trader.utility",
    "vnpy.trader.event",
    "vnpy_evo",
    "vnpy_evo.event",
    "vnpy_evo.trader",
    "vnpy_evo.trader.constant",
    "vnpy_evo.trader.object",
    "vnpy_evo.trader.gateway",
    "vnpy_evo.trader.utility",
    "talib",
)

_FX_ROOT = "ring0_vault.silos.fx_mt5"


# ---------------------------------------------------------------------------
# transport probing
# ---------------------------------------------------------------------------


class TransportUnavailable(RuntimeError):
    """Raised when a live venue session is requested without its transport."""


@dataclass(slots=True)
class TransportStatus:
    """What this host can actually reach for the FX silo."""

    mt5_package: bool
    mt5_terminal: bool
    mt5_terminal_path: str
    ctp_md_library: bool
    ctp_td_library: bool
    ctp_library_paths: tuple[str, ...]
    offline: bool

    @property
    def mt5_ready(self) -> bool:
        return self.mt5_package and self.mt5_terminal

    @property
    def ctp_ready(self) -> bool:
        return self.ctp_md_library and self.ctp_td_library

    def to_dict(self) -> dict[str, Any]:
        return {
            "mt5_package": self.mt5_package,
            "mt5_terminal": self.mt5_terminal,
            "mt5_terminal_path": self.mt5_terminal_path,
            "mt5_ready": self.mt5_ready,
            "ctp_md_library": self.ctp_md_library,
            "ctp_td_library": self.ctp_td_library,
            "ctp_library_paths": list(self.ctp_library_paths),
            "ctp_ready": self.ctp_ready,
            "offline": self.offline,
        }


def _fx_dir() -> Any:
    from pathlib import Path

    return Path(__file__).resolve().parent


def _mt5_terminal_candidates() -> tuple[str, ...]:
    env = os.environ.get("NIOS_MT5_TERMINAL", "")
    candidates = [env] if env else []
    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        if base:
            candidates.append(os.path.join(base, "MetaTrader 5", "terminal64.exe"))
            candidates.append(os.path.join(base, "MetaTrader 5", "terminal.exe"))
    candidates.append(os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "MetaQuotes", "Terminal"))
    return tuple(candidates)


def _probe_transport() -> TransportStatus:
    offline = os.environ.get("NIOS_VNPY_OFFLINE", "1") != "0"

    try:
        import MetaTrader5  # noqa: F401

        mt5_package = True
    except Exception:
        mt5_package = False

    terminal_path = ""
    mt5_terminal = False
    for candidate in _mt5_terminal_candidates():
        if candidate and os.path.exists(candidate):
            terminal_path = candidate
            mt5_terminal = True
            break
    if not mt5_terminal:
        try:
            import MetaTrader5 as _mt5

            info = _mt5.terminal_info()
            mt5_terminal = bool(info)
            terminal_path = getattr(info, "path", "") or ""
        except Exception:
            mt5_terminal = False

    api_dir = _fx_dir() / "vnpy_ctp" / "api"
    libs = tuple(
        str(p)
        for p in api_dir.glob("*")
        if p.suffix.lower() in {".dll", ".so", ".dylib"} and "thost" in p.name.lower()
    )
    md = any("md" in os.path.basename(p).lower() for p in libs)
    td = any("td" in os.path.basename(p).lower() or "trader" in os.path.basename(p).lower() for p in libs)

    return TransportStatus(
        mt5_package=mt5_package,
        mt5_terminal=mt5_terminal,
        mt5_terminal_path=terminal_path,
        ctp_md_library=md,
        ctp_td_library=td,
        ctp_library_paths=libs,
        offline=offline,
    )


_TRANSPORT: TransportStatus | None = None


def transport_status(*, refresh: bool = False) -> TransportStatus:
    """Cached transport probe (filesystem + optional package import only)."""

    global _TRANSPORT
    with _LOCK:
        if _TRANSPORT is None or refresh:
            _TRANSPORT = _probe_transport()
        return _TRANSPORT


def mt5_terminal_available() -> bool:
    return transport_status().mt5_ready


def ctp_library_available() -> tuple[bool, bool]:
    status = transport_status()
    return status.ctp_md_library, status.ctp_td_library


def require_transport(venue: str) -> None:
    """Fail closed when a live session is requested without its transport.

    ``venue`` is one of ``"mt5"``, ``"ctp"``, ``"ctp_md"``, ``"ctp_td"``.
    """

    token = str(venue).strip().lower()
    status = transport_status(refresh=True)
    if token == "mt5" and not status.mt5_ready:
        raise TransportUnavailable(
            "MetaTrader 5 terminal is offline: "
            f"package={status.mt5_package} terminal={status.mt5_terminal}. "
            "Paper dispatch remains available; set NIOS_MT5_TERMINAL to arm live."
        )
    if token in {"ctp", "ctp_md", "ctp_td"}:
        need_md = token in {"ctp", "ctp_md"}
        need_td = token in {"ctp", "ctp_td"}
        missing = []
        if need_md and not status.ctp_md_library:
            missing.append("md")
        if need_td and not status.ctp_td_library:
            missing.append("td")
        if missing:
            raise TransportUnavailable(
                "CTP transport library unavailable (" + "/".join(missing) + "): "
                f"searched {status.ctp_library_paths or 'no thost libraries'}. "
                "Paper dispatch remains available."
            )


# ---------------------------------------------------------------------------
# event engine (real implementation -- the gateways genuinely use it)
# ---------------------------------------------------------------------------


EVENT_TIMER = "eTimer"


class Event:
    """A vn.py style event envelope."""

    __slots__ = ("type", "data")

    def __init__(self, type: str, data: Any = None) -> None:  # noqa: A002 - upstream name
        self.type = type
        self.data = data

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Event(type={self.type!r}, data={self.data!r})"


class EventEngine:
    """Deterministic, dependency-free replacement for ``vnpy.event.EventEngine``.

    Starts no thread until :meth:`start` is called.  Handlers are invoked on the
    engine thread in FIFO order, exactly like upstream, so gateway code that
    registers ``EVENT_TIMER`` / ``EVENT_TICK`` handlers behaves identically.
    """

    def __init__(self, interval: int = 1) -> None:
        self._interval = max(1, int(interval))
        self._queue: "queue.Queue[Event]" = queue.Queue()
        self._active = False
        self._thread: threading.Thread | None = None
        self._timer: threading.Thread | None = None
        self._handlers: dict[str, list[Callable[[Event], Any]]] = {}
        self._general_handlers: list[Callable[[Event], Any]] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._thread = threading.Thread(target=self._run, name="nios-vnpy-event", daemon=True)
        self._thread.start()
        self._timer = threading.Thread(target=self._run_timer, name="nios-vnpy-timer", daemon=True)
        self._timer.start()

    def stop(self) -> None:
        self._active = False
        for thread in (self._timer, self._thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        self._thread = None
        self._timer = None

    @property
    def active(self) -> bool:
        return self._active

    # -- registration ------------------------------------------------------
    def register(self, type: str, handler: Callable[[Event], Any]) -> None:  # noqa: A002
        handlers = self._handlers.setdefault(type, [])
        if handler not in handlers:
            handlers.append(handler)

    def unregister(self, type: str, handler: Callable[[Event], Any]) -> None:  # noqa: A002
        handlers = self._handlers.get(type, [])
        if handler in handlers:
            handlers.remove(handler)

    def register_general(self, handler: Callable[[Event], Any]) -> None:
        if handler not in self._general_handlers:
            self._general_handlers.append(handler)

    def unregister_general(self, handler: Callable[[Event], Any]) -> None:
        if handler in self._general_handlers:
            self._general_handlers.remove(handler)

    def register_type(self, type: str) -> None:  # noqa: A002 - upstream API
        self._handlers.setdefault(type, [])

    # -- dispatch ----------------------------------------------------------
    def put(self, event: Event) -> None:
        self._queue.put(event)

    def _process(self, event: Event) -> None:
        for handler in list(self._handlers.get(event.type, ())):
            self._safe_call(handler, event)
        for handler in list(self._general_handlers):
            self._safe_call(handler, event)

    @staticmethod
    def _safe_call(handler: Callable[[Event], Any], event: Event) -> None:
        try:
            handler(event)
        except Exception:
            _LOG.exception("event handler %r failed for %s", handler, event.type)

    def _run(self) -> None:
        while self._active:
            try:
                event = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._process(event)

    def _run_timer(self) -> None:
        while self._active:
            time.sleep(self._interval)
            if self._active:
                self.put(Event(EVENT_TIMER))


def _build_event_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__["__nios_vnpy_compat__"] = True
    mod.__doc__ = "NIOS Ring 0 vn.py event engine."
    mod.Event = Event
    mod.EventEngine = EventEngine
    mod.EVENT_TIMER = EVENT_TIMER
    mod.__all__ = ["Event", "EventEngine", "EVENT_TIMER"]
    return mod


# ---------------------------------------------------------------------------
# talib shim (NumPy only)
# ---------------------------------------------------------------------------


def _as_array(values: Any) -> Any:
    import numpy as np

    return np.asarray(values, dtype=float)


def _rolling(values: Any, window: int, fn: str) -> Any:
    import numpy as np

    arr = _as_array(values)
    n = int(max(1, window))
    out = np.full(arr.shape, np.nan, dtype=float)
    if arr.size < n:
        return out
    if fn == "sma":
        cumsum = np.cumsum(np.insert(arr, 0, 0.0))
        out[n - 1:] = (cumsum[n:] - cumsum[:-n]) / n
    elif fn == "max":
        for i in range(n - 1, arr.size):
            out[i] = arr[i - n + 1: i + 1].max()
    elif fn == "min":
        for i in range(n - 1, arr.size):
            out[i] = arr[i - n + 1: i + 1].min()
    elif fn == "std":
        for i in range(n - 1, arr.size):
            out[i] = arr[i - n + 1: i + 1].std(ddof=0)
    return out


def _sma(values: Any, timeperiod: int = 30, **_: Any) -> Any:
    return _rolling(values, timeperiod, "sma")


def _ema(values: Any, timeperiod: int = 30, **_: Any) -> Any:
    import numpy as np

    arr = _as_array(values)
    n = int(max(1, timeperiod))
    out = np.full(arr.shape, np.nan, dtype=float)
    if arr.size == 0:
        return out
    alpha = 2.0 / (n + 1.0)
    acc = arr[0]
    out[0] = acc
    for i in range(1, arr.size):
        acc = alpha * arr[i] + (1.0 - alpha) * acc
        out[i] = acc
    return out


def _wma(values: Any, timeperiod: int = 30, **_: Any) -> Any:
    import numpy as np

    arr = _as_array(values)
    n = int(max(1, timeperiod))
    weights = np.arange(1, n + 1, dtype=float)
    out = np.full(arr.shape, np.nan, dtype=float)
    for i in range(n - 1, arr.size):
        out[i] = float((arr[i - n + 1: i + 1] * weights).sum() / weights.sum())
    return out


def _trange(high: Any, low: Any, close: Any, **_: Any) -> Any:
    import numpy as np

    h, l, c = _as_array(high), _as_array(low), _as_array(close)
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    return np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])


def _atr(high: Any, low: Any, close: Any, timeperiod: int = 14, **_: Any) -> Any:
    return _ema(_trange(high, low, close), timeperiod)


def _rsi(values: Any, timeperiod: int = 14, **_: Any) -> Any:
    import numpy as np

    arr = _as_array(values)
    n = int(max(1, timeperiod))
    out = np.full(arr.shape, np.nan, dtype=float)
    if arr.size <= n:
        return out
    delta = np.diff(arr)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = gains[:n].mean()
    avg_loss = losses[:n].mean()
    for i in range(n, arr.size):
        avg_gain = (avg_gain * (n - 1) + gains[i - 1]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i - 1]) / n
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def _macd(values: Any, fastperiod: int = 12, slowperiod: int = 26, signalperiod: int = 9, **_: Any) -> Any:
    macd = _ema(values, fastperiod) - _ema(values, slowperiod)
    signal = _ema(macd, signalperiod)
    return macd, signal, macd - signal


def _stoch(high: Any, low: Any, close: Any, fastk_period: int = 5, slowk_period: int = 3, slowd_period: int = 3, **_: Any) -> Any:
    import numpy as np

    h, l, c = _as_array(high), _as_array(low), _as_array(close)
    highest = _rolling(h, fastk_period, "max")
    lowest = _rolling(l, fastk_period, "min")
    span = np.where((highest - lowest) == 0, np.nan, highest - lowest)
    fastk = 100.0 * (c - lowest) / span
    slowk = _rolling(fastk, slowk_period, "sma")
    slowd = _rolling(slowk, slowd_period, "sma")
    return slowk, slowd


def _aroon(high: Any, low: Any, timeperiod: int = 14, **_: Any) -> Any:
    import numpy as np

    h, l = _as_array(high), _as_array(low)
    n = int(max(1, timeperiod))
    down = np.full(h.shape, np.nan, dtype=float)
    up = np.full(h.shape, np.nan, dtype=float)
    for i in range(n - 1, h.size):
        win_h, win_l = h[i - n + 1: i + 1], l[i - n + 1: i + 1]
        up[i] = 100.0 * (n - 1 - int(win_h.argmax())) / (n - 1)
        down[i] = 100.0 * (n - 1 - int(win_l.argmin())) / (n - 1)
    return down, up


def _obv(close: Any, volume: Any, **_: Any) -> Any:
    import numpy as np

    c, v = _as_array(close), _as_array(volume)
    out = np.zeros(c.shape, dtype=float)
    for i in range(1, c.size):
        out[i] = out[i - 1] + (v[i] if c[i] > c[i - 1] else (-v[i] if c[i] < c[i - 1] else 0.0))
    return out


_IDENTITY_FUNCS = {
    "AD", "ADOSC", "ADX", "ADXR", "APO", "AROONOSC", "BOP", "CCI", "CMO", "DX", "KAMA",
    "MAX", "MFI", "MIN", "MINUS_DI", "MINUS_DM", "MOM", "NATR", "PLUS_DI", "PLUS_DM",
    "PPO", "ROC", "ROCP", "ROCR", "ROCR100", "SAR", "STDDEV", "TRIX", "ULTOSC", "WILLR",
}

_MULTI_FUNCS = {"MACD", "STOCH", "AROON", "BBANDS", "MAMA", "HT_PHASOR", "HT_SINE"}


def _identity(*args: Any, **_: Any) -> Any:
    for arg in args:
        if hasattr(arg, "__len__"):
            return _as_array(arg)
    import numpy as np

    return np.asarray(args[0] if args else [], dtype=float)


def _build_talib_module() -> types.ModuleType:
    mod = types.ModuleType("talib")
    mod.__dict__["__nios_vnpy_compat__"] = True
    mod.__doc__ = "NumPy-only TA-Lib shim (NIOS Ring 0)."
    mod.__version__ = "0.4.0+nios-shim"

    mod.SMA = _sma
    mod.EMA = _ema
    mod.WMA = _wma
    mod.TRANGE = _trange
    mod.ATR = _atr
    mod.RSI = _rsi
    mod.MACD = _macd
    mod.STOCH = _stoch
    mod.AROON = _aroon
    mod.OBV = _obv
    mod.MAX = lambda values, timeperiod=30, **k: _rolling(values, timeperiod, "max")
    mod.MIN = lambda values, timeperiod=30, **k: _rolling(values, timeperiod, "min")
    mod.STDDEV = lambda values, timeperiod=5, nbdev=1, **k: _rolling(values, timeperiod, "std") * float(nbdev)
    mod.NATR = lambda high, low, close, timeperiod=14, **k: _atr(high, low, close, timeperiod) / _as_array(close)
    mod.CMO = lambda values, timeperiod=14, **k: _rsi(values, timeperiod) * 2.0 - 100.0

    for name in sorted(_IDENTITY_FUNCS):
        mod.__dict__.setdefault(name, _identity)
    for name in sorted(_MULTI_FUNCS):
        mod.__dict__.setdefault(name, _identity)

    def __getattr__(item: str) -> Any:
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        return _identity

    mod.__dict__["__getattr__"] = __getattr__
    return mod


# ---------------------------------------------------------------------------
# namespace assembly
# ---------------------------------------------------------------------------


def _module(name: str, *, package: bool = False, doc: str | None = None) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__["__nios_vnpy_compat__"] = True
    if doc:
        mod.__doc__ = doc
    if package:
        mod.__path__ = []  # type: ignore[attr-defined]
        mod.__package__ = name
    return mod


def _register(name: str, mod: types.ModuleType) -> types.ModuleType:
    existing = sys.modules.get(name)
    if existing is not None and not getattr(existing, "__nios_vnpy_compat__", False):
        _STATE[name] = "existing"
        return existing
    sys.modules[name] = mod
    _STATE[name] = "shim"
    return mod


def _real_module_available(name: str) -> bool:
    if name in sys.modules:
        return not getattr(sys.modules[name], "__nios_vnpy_compat__", False)
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _alias(name: str, target: str) -> types.ModuleType:
    """Alias ``name`` onto an already-imported ``target`` module (same object)."""

    module = importlib.import_module(target)
    _register(name, module)
    return module


def install(*, force: bool = False) -> dict[str, str]:
    """Install the vnpy / vnpy_evo / talib bridge.  Idempotent."""

    global _INSTALLED
    with _LOCK:
        if _INSTALLED and not force:
            return dict(_STATE)

        if force:
            for name, state in list(_STATE.items()):
                if state == "shim" and getattr(sys.modules.get(name), "__nios_vnpy_compat__", False):
                    sys.modules.pop(name, None)
            _STATE.clear()

        # 1. talib must exist before ``vnpy_core.utility`` is imported.
        if _real_module_available("talib"):
            _STATE["talib"] = "existing"
        else:
            _register("talib", _build_talib_module())

        # 2. ``vnpy`` root + the event engine that ``vnpy_core.event`` imports.
        if _real_module_available("vnpy"):
            _STATE["vnpy"] = "existing"
        else:
            _register(
                "vnpy",
                _module("vnpy", package=True, doc="NIOS Ring 0 alias for the harvested vn.py core."),
            )
        if _real_module_available("vnpy.event"):
            _STATE["vnpy.event"] = "existing"
        else:
            _register("vnpy.event", _build_event_module("vnpy.event"))

        # 3. ``vnpy.trader`` is a package whose submodules are the vnpy_core ones.
        trader = _module("vnpy.trader", package=True, doc="Alias onto vnpy_core.")
        trader.__path__ = []  # type: ignore[attr-defined]
        _register("vnpy.trader", trader)

        for sub in ("constant", "object", "gateway", "utility", "event"):
            _alias(f"vnpy.trader.{sub}", f"{_FX_ROOT}.vnpy_core.{sub}")

        # 4. ``vnpy_evo`` mirrors the same layout (upstream rename).
        evo = _module("vnpy_evo", package=True, doc="Alias onto vnpy_core (upstream rename).")
        evo.__path__ = []  # type: ignore[attr-defined]
        _register("vnpy_evo", evo)
        _alias("vnpy_evo.event", "vnpy.event")

        evo_trader = _module("vnpy_evo.trader", package=True, doc="Alias onto vnpy_core.")
        evo_trader.__path__ = []  # type: ignore[attr-defined]
        _register("vnpy_evo.trader", evo_trader)
        for sub in ("constant", "object", "gateway", "utility"):
            _alias(f"vnpy_evo.trader.{sub}", f"{_FX_ROOT}.vnpy_core.{sub}")

        # 5. Expose the useful names on the package roots.
        sys.modules["vnpy"].event = sys.modules["vnpy.event"]  # type: ignore[attr-defined]
        sys.modules["vnpy"].trader = sys.modules["vnpy.trader"]  # type: ignore[attr-defined]
        sys.modules["vnpy_evo"].event = sys.modules["vnpy.event"]  # type: ignore[attr-defined]
        sys.modules["vnpy_evo"].trader = sys.modules["vnpy_evo.trader"]  # type: ignore[attr-defined]
        sys.modules["vnpy.trader"].constant = sys.modules["vnpy.trader.constant"]  # type: ignore[attr-defined]
        sys.modules["vnpy.trader"].object = sys.modules["vnpy.trader.object"]  # type: ignore[attr-defined]
        sys.modules["vnpy.trader"].gateway = sys.modules["vnpy.trader.gateway"]  # type: ignore[attr-defined]
        sys.modules["vnpy.trader"].utility = sys.modules["vnpy.trader.utility"]  # type: ignore[attr-defined]
        sys.modules["vnpy.trader"].event = sys.modules["vnpy.trader.event"]  # type: ignore[attr-defined]

        for name in COMPAT_MODULES:
            _STATE.setdefault(name, "shim")

        _INSTALLED = True
        _LOG.debug("vnpy compat bridge installed: %s", sorted(_STATE))
        return dict(_STATE)


def uninstall() -> None:
    """Remove every shim this module installed (test isolation helper)."""

    global _INSTALLED, _TRANSPORT
    with _LOCK:
        for name, state in list(_STATE.items()):
            if state != "shim":
                continue
            module = sys.modules.get(name)
            if module is not None and getattr(module, "__nios_vnpy_compat__", False):
                sys.modules.pop(name, None)
        _STATE.clear()
        _INSTALLED = False
        _TRANSPORT = None


def compat_status() -> dict[str, Any]:
    """Report bridged namespaces plus host transport availability."""

    install()
    return {
        "installed": _INSTALLED,
        "modules": dict(_STATE),
        "shim_count": sum(1 for v in _STATE.values() if v == "shim"),
        "existing_count": sum(1 for v in _STATE.values() if v == "existing"),
        "transport": transport_status(refresh=True).to_dict(),
    }


install()
