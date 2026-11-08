"""NIOS Core v3.0 -- Silo-Sharded Ring 1 Workers & CPU Affinity (STEP 5).

Governing sections
------------------
``sec_004``
    Ring 1 is "Pinned CPU Cores per Silo" -- one physical core per market
    silo, running an isolated WebSocket multiplexer.
``sec_014``
    "Silo-Sharded Ring 1 Workers with CPU Affinity: Each market silo
    (Crypto-Binance, India-Zerodha-Dhan, US-IBKR, FX-MT5) runs on a dedicated
    pinned physical CPU core (Linux ``taskset`` / Docker ``cpuset-cpus``) with
    isolated WebSocket multiplexers, achieving linear horizontal scaling."
``sec_014`` tax-net floor
    "Every silo pre-loads exact venue maker/taker fees and statutory taxes
    (such as India's 5.5 bps Securities Transaction Tax and stamp duty floor)
    into Ring 0 so scalpers never churn sub-cost microstructure noise."
``sec_015``
    The four sovereign silos, their production adapters, microstructure rules
    and their reserve-yield / long-term-sweep assets.

What this module is *not*
-------------------------
It is not a broker adapter.  It holds no credentials, opens no socket and
imports nothing from Ring 0's key vault (``sec_018`` Law 2 / ``sec_004``
air-gap).  It is the **static configuration plane** for the Ring 1 worker
fleet plus a best-effort OS scheduling call, so the same code can plan a
Linux ``taskset`` deployment and a Windows developer workstation.

Honesty note on fees
--------------------
The cost floors below are *configured statutory floors*, not a live fee feed.
They encode the minimum round-trip friction a scalp must clear before Ring 0's
``Net_LB > 0 bps`` gate can pass:

``round_trip_cost_bps = spread_bps + 2 * maker_fee_bps + statutory_tax_bps``

A Ring 3 ``POST /api/v2/control/brokers`` fee-table update replaces these
values at runtime; they are deliberately conservative so the default posture
abstains rather than churns (``sec_018`` Law 4).
"""

from __future__ import annotations

import ctypes
import importlib
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "CRYPTO_BINANCE",
    "INDIA_ZERODHA_DHAN",
    "US_IBKR",
    "FX_MT5",
    "SILO_IDS",
    "SILO_CONFIGS",
    "SILO_ORDER",
    "SiloCostFloor",
    "SiloConfig",
    "SiloWorkerPlan",
    "AffinityResult",
    "silo_config",
    "silo_cost_floor",
    "describe_silo_costs",
    "pin_cpu_core",
    "set_cpu_affinity",
    "unpin_cpu",
    "current_cpu_affinity",
    "plan_silo_workers",
    "ring1_worker_status",
    "nautilus_adapter_status",
]

CRYPTO_BINANCE = "CRYPTO_BINANCE"
INDIA_ZERODHA_DHAN = "INDIA_ZERODHA_DHAN"
US_IBKR = "US_IBKR"
FX_MT5 = "FX_MT5"

#: Canonical shard order -- also the CPU-affinity assignment order.
SILO_ORDER: tuple[str, ...] = (
    CRYPTO_BINANCE,
    INDIA_ZERODHA_DHAN,
    US_IBKR,
    FX_MT5,
)

SILO_IDS = SILO_ORDER

#: Preferred first physical core for the worker fleet.  Core 0 is left for
#: the OS/IRQ plane, which is the standard low-latency pinning convention.
DEFAULT_BASE_CORE = 1


# ---------------------------------------------------------------------------
# Cost floors
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SiloCostFloor:
    """Statutory + venue round-trip cost floor for one silo (bps).

    ``round_trip_cost_bps`` mirrors the friction term of the ``sec_003``
    conformal gate (``spread + 2 * fee + tax``), so Ring 0 and Ring 1 agree
    on the same number without re-deriving it.
    """

    spread_bps: float
    maker_fee_bps: float
    taker_fee_bps: float
    statutory_tax_bps: float
    source: str = "configured statutory floor (sec_014 / sec_015)"

    def __post_init__(self) -> None:
        for field_name in (
            "spread_bps",
            "maker_fee_bps",
            "taker_fee_bps",
            "statutory_tax_bps",
        ):
            value = float(getattr(self, field_name))
            if value < 0.0:
                raise ValueError(f"{field_name} must be non-negative")
            object.__setattr__(self, field_name, value)

    @property
    def round_trip_cost_bps(self) -> float:
        """Total friction a scalp must beat to be net positive."""
        return (
            self.spread_bps
            + 2.0 * self.maker_fee_bps
            + self.statutory_tax_bps
        )

    @property
    def fee_stt_bps(self) -> float:
        """The "Fee+STT" figure rendered by the ``sec_016`` proof line."""
        return 2.0 * self.maker_fee_bps + self.statutory_tax_bps

    def to_dict(self) -> dict[str, Any]:
        return {
            "spread_bps": self.spread_bps,
            "maker_fee_bps": self.maker_fee_bps,
            "taker_fee_bps": self.taker_fee_bps,
            "statutory_tax_bps": self.statutory_tax_bps,
            "fee_stt_bps": self.fee_stt_bps,
            "round_trip_cost_bps": self.round_trip_cost_bps,
            "source": self.source,
        }


@dataclass(slots=True, frozen=True)
class SiloConfig:
    """Static identity + economics of one sovereign market silo."""

    silo_id: str
    label: str
    venue: str
    adapters: tuple[str, ...]
    cost_floor: SiloCostFloor
    reserve_yield_asset: str
    long_term_sweep_asset: str
    microstructure_rules: str
    default_cpu_core: int = DEFAULT_BASE_CORE

    @property
    def fee_bps(self) -> float:
        return self.cost_floor.maker_fee_bps

    @property
    def tax_bps(self) -> float:
        return self.cost_floor.statutory_tax_bps

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo_id": self.silo_id,
            "label": self.label,
            "venue": self.venue,
            "adapters": list(self.adapters),
            "cost_floor": self.cost_floor.to_dict(),
            "reserve_yield_asset": self.reserve_yield_asset,
            "long_term_sweep_asset": self.long_term_sweep_asset,
            "microstructure_rules": self.microstructure_rules,
            "default_cpu_core": self.default_cpu_core,
        }


#: ``sec_015`` silo table.  Crypto VIP0 with BNB discount; India intraday with
#: the statutory 5.5 bps STT + stamp-duty floor; IBKR Pro fixed with SEC
#: Section 31 + FINRA TAF on the sell side; MT5 raw-spread FX.
SILO_CONFIGS: dict[str, SiloConfig] = {
    CRYPTO_BINANCE: SiloConfig(
        silo_id=CRYPTO_BINANCE,
        label="Crypto (24/7)",
        venue="BINANCE",
        adapters=("binance", "okx", "bybit"),
        cost_floor=SiloCostFloor(
            spread_bps=1.0,
            maker_fee_bps=7.5,
            taker_fee_bps=7.5,
            statutory_tax_bps=0.0,
        ),
        reserve_yield_asset="USYC Tokenized T-Bill",
        long_term_sweep_asset="BTC-USDT / ETH-USDT",
        microstructure_rules=(
            "Post-only maker-peg chasing; perpetual funding-rate & "
            "liquidation cascade hooks"
        ),
        default_cpu_core=DEFAULT_BASE_CORE + 0,
    ),
    INDIA_ZERODHA_DHAN: SiloConfig(
        silo_id=INDIA_ZERODHA_DHAN,
        label="India Equities & F&O",
        venue="NSE/BSE",
        adapters=("zerodha", "dhan", "angel", "upstox"),
        # 5.5 bps = STT + stamp duty + exchange transaction charges floor
        # (``sec_014``); brokerage is the 0.5 bps per-side component.
        cost_floor=SiloCostFloor(
            spread_bps=2.0,
            maker_fee_bps=0.5,
            taker_fee_bps=0.5,
            statutory_tax_bps=5.5,
        ),
        reserve_yield_asset="LIQUIDBEES.NS",
        long_term_sweep_asset="NIFTYBEES.NS & DCF Picks",
        microstructure_rules=(
            "NSE/BSE MIS intraday vs. CNC delivery; 5.5 bps STT + Stamp "
            "Duty + Brokerage floor"
        ),
        default_cpu_core=DEFAULT_BASE_CORE + 1,
    ),
    US_IBKR: SiloConfig(
        silo_id=US_IBKR,
        label="US & Global Equities",
        venue="IBKR",
        adapters=("interactive_brokers",),
        cost_floor=SiloCostFloor(
            spread_bps=1.5,
            maker_fee_bps=0.5,
            taker_fee_bps=0.5,
            statutory_tax_bps=0.3,
        ),
        reserve_yield_asset="SGOV (0-3M Treasury ETF)",
        long_term_sweep_asset="VOO / QQQ & DCF Picks",
        microstructure_rules=(
            "T+1 settlement, Reg-T margin guards, sub-penny maker pegging, "
            "SEC EDGAR PIT"
        ),
        default_cpu_core=DEFAULT_BASE_CORE + 2,
    ),
    FX_MT5: SiloConfig(
        silo_id=FX_MT5,
        label="Forex & Futures",
        venue="MT5",
        adapters=("mt5", "ctp"),
        cost_floor=SiloCostFloor(
            spread_bps=0.8,
            maker_fee_bps=0.4,
            taker_fee_bps=0.4,
            statutory_tax_bps=0.0,
        ),
        reserve_yield_asset="Unlevered Gold / Short-Duration Sovereign",
        long_term_sweep_asset="Sovereign Carry Repo",
        microstructure_rules=(
            "24/5 pip-spread gates, macro calendar blackout windows, futures "
            "roll-yield guards"
        ),
        default_cpu_core=DEFAULT_BASE_CORE + 3,
    ),
}


def silo_config(silo_id: str) -> SiloConfig:
    """Look up a silo by id (case/underscore tolerant)."""
    token = str(silo_id).strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return SILO_CONFIGS[token]
    except KeyError as exc:
        raise KeyError(
            f"unknown silo {silo_id!r}; expected one of {SILO_ORDER}"
        ) from exc


def silo_cost_floor(silo_id: str) -> SiloCostFloor:
    return silo_config(silo_id).cost_floor


def describe_silo_costs() -> dict[str, dict[str, Any]]:
    """Machine-readable cost table for the cockpit control plane."""
    return {silo_id: SILO_CONFIGS[silo_id].to_dict() for silo_id in SILO_ORDER}


# ---------------------------------------------------------------------------
# CPU affinity
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class AffinityResult:
    """Outcome of a best-effort CPU pinning attempt."""

    core_id: int
    pinned: bool
    mechanism: str
    reason: str = ""
    previous: tuple[int, ...] = ()
    effective: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "core_id": self.core_id,
            "pinned": self.pinned,
            "mechanism": self.mechanism,
            "reason": self.reason,
            "previous": list(self.previous),
            "effective": list(self.effective),
        }


def current_cpu_affinity() -> tuple[int, ...] | None:
    """Current process CPU set, or ``None`` when the platform cannot report."""
    if hasattr(os, "sched_getaffinity"):
        try:
            return tuple(sorted(int(core) for core in os.sched_getaffinity(0)))
        except Exception:  # pragma: no cover - restricted sandbox
            return None
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.GetProcessAffinityMask.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_size_t),
            ]
            kernel32.GetProcessAffinityMask.restype = ctypes.c_int
            process_mask = ctypes.c_size_t()
            system_mask = ctypes.c_size_t()
            ok = kernel32.GetProcessAffinityMask(
                kernel32.GetCurrentProcess(),
                ctypes.byref(process_mask),
                ctypes.byref(system_mask),
            )
            if not ok:
                return None
            mask = int(process_mask.value)
            cores = tuple(bit for bit in range(64) if mask & (1 << bit))
            return cores or None
        except Exception:
            return None
    return None


def _cpu_count() -> int:
    try:
        return max(1, int(os.cpu_count() or 1))
    except Exception:  # pragma: no cover
        return 1


def pin_cpu_core(core_id: int, *, dry_run: bool = False) -> AffinityResult:
    """Pin the **current process** to one physical CPU core.

    * Linux: ``os.sched_setaffinity(0, {core_id})`` (``sec_014`` ``taskset``).
    * Windows: ``kernel32.SetProcessAffinityMask`` (processor-affinity mask).
    * Everywhere else, or when the OS refuses: returns ``pinned=False`` with a
      human-readable reason instead of raising, so a degraded single-core
      container still boots.

    ``dry_run=True`` reports what *would* happen without mutating the process.
    """
    core = int(core_id)
    n_cpus = _cpu_count()
    previous = current_cpu_affinity() or ()
    mechanism = "unsupported"

    if core < 0 or core >= n_cpus:
        return AffinityResult(
            core_id=core,
            pinned=False,
            mechanism="range-check",
            reason=f"core {core} outside the available range [0, {n_cpus - 1}]",
            previous=previous,
            effective=previous,
        )

    if dry_run:
        mechanism = (
            "os.sched_setaffinity" if hasattr(os, "sched_setaffinity") else
            "SetProcessAffinityMask" if sys.platform == "win32" else "unsupported"
        )
        return AffinityResult(
            core_id=core,
            pinned=False,
            mechanism=f"{mechanism} (dry-run)",
            reason="dry-run: no affinity change performed",
            previous=previous,
            effective=previous,
        )

    if hasattr(os, "sched_setaffinity"):
        mechanism = "os.sched_setaffinity"
        try:
            os.sched_setaffinity(0, {core})
            effective = current_cpu_affinity() or ()
            return AffinityResult(core, True, mechanism, "", previous, effective)
        except Exception as exc:
            return AffinityResult(
                core_id=core,
                pinned=False,
                mechanism=mechanism,
                reason=f"{type(exc).__name__}: {exc}",
                previous=previous,
                effective=previous,
            )

    if sys.platform == "win32":  # pragma: no cover - executed on Windows
        mechanism = "SetProcessAffinityMask"
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetProcessAffinityMask.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            kernel32.SetProcessAffinityMask.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            ok = kernel32.SetProcessAffinityMask(handle, ctypes.c_size_t(1 << core))
            if not ok:
                error = ctypes.get_last_error()
                return AffinityResult(
                    core_id=core,
                    pinned=False,
                    mechanism=mechanism,
                    reason=f"SetProcessAffinityMask failed (GetLastError={error})",
                    previous=previous,
                    effective=previous,
                )
            effective = current_cpu_affinity() or ()
            return AffinityResult(core, True, mechanism, "", previous, effective)
        except Exception as exc:
            return AffinityResult(
                core_id=core,
                pinned=False,
                mechanism=mechanism,
                reason=f"{type(exc).__name__}: {exc}",
                previous=previous,
                effective=previous,
            )

    return AffinityResult(
        core_id=core,
        pinned=False,
        mechanism="unsupported",
        reason=f"no CPU-affinity API on platform {platform.system()!r}",
        previous=previous,
        effective=previous,
    )


def set_cpu_affinity(
    cores: Sequence[int], *, dry_run: bool = False
) -> AffinityResult:
    """Restrict the current process to an explicit CPU set.

    Used to restore a multi-core mask after :func:`pin_cpu_core` (tests and
    the ``Ring 3`` hot-swap control plane both need a way back).  ``core_id``
    on the result is ``-1`` to signal "CPU set" rather than one core.
    """
    requested = tuple(sorted({int(core) for core in cores}))
    if not requested:
        raise ValueError("at least one core is required")
    n_cpus = _cpu_count()
    previous = current_cpu_affinity() or ()
    invalid = [core for core in requested if core < 0 or core >= n_cpus]
    if invalid:
        return AffinityResult(
            core_id=-1,
            pinned=False,
            mechanism="range-check",
            reason=f"cores {invalid} outside [0, {n_cpus - 1}]",
            previous=previous,
            effective=previous,
        )
    if dry_run:
        return AffinityResult(
            core_id=-1,
            pinned=False,
            mechanism="cpu-set (dry-run)",
            reason="dry-run: no affinity change performed",
            previous=previous,
            effective=previous,
        )

    if hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, set(requested))
            return AffinityResult(
                -1, True, "os.sched_setaffinity", "", previous,
                current_cpu_affinity() or (),
            )
        except Exception as exc:
            return AffinityResult(
                -1, False, "os.sched_setaffinity", f"{type(exc).__name__}: {exc}",
                previous, previous,
            )

    if sys.platform == "win32":  # pragma: no cover - executed on Windows
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetProcessAffinityMask.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            kernel32.SetProcessAffinityMask.restype = ctypes.c_int
            mask = 0
            for core in requested:
                mask |= 1 << core
            ok = kernel32.SetProcessAffinityMask(
                kernel32.GetCurrentProcess(), ctypes.c_size_t(mask)
            )
            if not ok:
                return AffinityResult(
                    -1, False, "SetProcessAffinityMask",
                    "SetProcessAffinityMask failed",
                    previous, previous,
                )
            return AffinityResult(
                -1, True, "SetProcessAffinityMask", "", previous,
                current_cpu_affinity() or (),
            )
        except Exception as exc:
            return AffinityResult(
                -1, False, "SetProcessAffinityMask",
                f"{type(exc).__name__}: {exc}", previous, previous,
            )

    return AffinityResult(
        -1, False, "unsupported",
        f"no CPU-affinity API on platform {platform.system()!r}",
        previous, previous,
    )


def unpin_cpu() -> AffinityResult:
    """Restore the process to every core the OS reports."""
    return set_cpu_affinity(tuple(range(_cpu_count())))


# ---------------------------------------------------------------------------
# Worker plans
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SiloWorkerPlan:
    """One Ring 1 worker lane: a silo, a core and its economics."""

    silo_id: str
    label: str
    cpu_core: int
    venue: str
    fee_bps: float
    tax_bps: float
    round_trip_cost_bps: float
    adapters: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo_id": self.silo_id,
            "label": self.label,
            "cpu_core": self.cpu_core,
            "venue": self.venue,
            "fee_bps": self.fee_bps,
            "tax_bps": self.tax_bps,
            "round_trip_cost_bps": self.round_trip_cost_bps,
            "adapters": list(self.adapters),
        }


def plan_silo_workers(
    silos: Iterable[str] | None = None,
    *,
    base_core: int = DEFAULT_BASE_CORE,
) -> dict[str, SiloWorkerPlan]:
    """Deterministically assign one dedicated CPU core per silo.

    Assignment is ``base_core + index`` in :data:`SILO_ORDER`, matching the
    ``sec_014`` "linear horizontal scaling across venues" contract: adding a
    venue adds one core rather than widening an existing worker.
    """
    selected = tuple(SILO_ORDER if silos is None else silos)
    if not selected:
        raise ValueError("at least one silo is required")
    plans: dict[str, SiloWorkerPlan] = {}
    for index, silo in enumerate(selected):
        cfg = silo_config(silo)
        plans[cfg.silo_id] = SiloWorkerPlan(
            silo_id=cfg.silo_id,
            label=cfg.label,
            cpu_core=int(base_core) + index,
            venue=cfg.venue,
            fee_bps=cfg.fee_bps,
            tax_bps=cfg.tax_bps,
            round_trip_cost_bps=cfg.cost_floor.round_trip_cost_bps,
            adapters=cfg.adapters,
        )
    return plans


# ---------------------------------------------------------------------------
# Harvester status bridge
# ---------------------------------------------------------------------------

_ADAPTER_DIRS: tuple[str, ...] = (
    "binance",
    "okx",
    "bybit",
    "interactive_brokers",
)

#: Recorded in ``AGENTS.md`` section 8.2 -- do not fabricate a wrapper.
_KNOWN_GAPS: Mapping[str, str] = {
    "alpaca": (
        "not present in the harvested nautilus_trader HEAD; the US silo must "
        "use IBKR until a wrapper is vendored"
    ),
}


def nautilus_adapter_status() -> dict[str, Any]:
    """Honest inspection of the harvested NautilusTrader adapter wrappers.

    Reports three independent facts per adapter so the cockpit never
    overstates readiness:

    * ``harvested``  -- the wrapper source exists on disk;
    * ``importable`` -- the module actually imports in this interpreter
      (it needs ``nautilus_trader`` installed);
    * ``reason``     -- why it did not import, verbatim.
    """
    root = Path(__file__).resolve().parent / "nautilus_adapters"
    adapters: dict[str, dict[str, Any]] = {}
    for name in _ADAPTER_DIRS:
        directory = root / name
        init = directory / "__init__.py"
        entry: dict[str, Any] = {
            "path": str(directory),
            "harvested": directory.is_dir() and init.is_file(),
            "importable": False,
            "symbols": [],
            "reason": "",
        }
        if not entry["harvested"]:
            entry["reason"] = "wrapper not harvested"
            adapters[name] = entry
            continue
        module_name = f"ring1_spinal.nautilus_adapters.{name}"
        try:
            module = importlib.import_module(module_name)
            entry["importable"] = True
            exports = getattr(module, "__all__", ())
            entry["symbols"] = [str(symbol) for symbol in exports][:16]
        except Exception as exc:
            entry["reason"] = f"{type(exc).__name__}: {exc}"
        adapters[name] = entry

    harvested = sum(1 for entry in adapters.values() if entry["harvested"])
    importable = sum(1 for entry in adapters.values() if entry["importable"])
    return {
        "root": str(root),
        "adapters": adapters,
        "harvested_count": harvested,
        "importable_count": importable,
        "total": len(_ADAPTER_DIRS),
        "gaps": dict(_KNOWN_GAPS),
        "note": (
            "Harvested wrappers are thin re-exports of nautilus_trader. "
            "Importability depends on nautilus_trader being installed; "
            "Ring 1 never imports a broker adapter on the tick path."
        ),
    }


def ring1_worker_status(
    silos: Iterable[str] | None = None,
) -> dict[str, Any]:
    """One-call status for the cockpit X-ray tab."""
    plans = plan_silo_workers(silos)
    return {
        "silos": {silo_id: plan.to_dict() for silo_id, plan in plans.items()},
        "affinity": {
            "mechanism": (
                "os.sched_setaffinity" if hasattr(os, "sched_setaffinity") else
                "SetProcessAffinityMask" if sys.platform == "win32" else "unsupported"
            ),
            "platform": platform.system(),
            "cpu_count": _cpu_count(),
            "current": list(current_cpu_affinity() or ()),
        },
        "adapters": nautilus_adapter_status(),
    }
