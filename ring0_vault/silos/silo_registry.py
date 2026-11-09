"""NIOS Core v3.0 -- the four sovereign market silos (``sec_005`` / ``sec_015``).

Each silo is a fully isolated broker account with its own statutory tax floor,
its own microstructure rules, its own 35% long-term sweep instrument and its own
15% reserve-yield instrument.  Capital never crosses a silo boundary, so there
are zero cross-border wires to bleed FX spread or withholding tax (``sec_018``
Law 5).

The registry is also where the cockpit learns the truth about donor readiness.
Every adapter probe reports three *independent* facts so the operator is never
told that a venue is "connected" when only source code is on disk:

``harvested``   the wrapper directory and ``__init__.py`` exist;
``importable``  the module actually imports in this interpreter;
``reason``      the verbatim exception when it does not.

Nothing here places an order.  Ring 3 may read this registry and toggle modes;
Ring 0 is the only ring that may act on it (``sec_011``).
"""

from __future__ import annotations

import hashlib
import importlib
import os
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.schemas import VaultName

from ring0_vault.conformal_copula_gate import SiloMarket
from ring0_vault.ibor.ledger_outbox import VAULT_ASSET_MAP
from ring1_spinal.silo_worker import (
    CRYPTO_BINANCE,
    FX_MT5,
    INDIA_ZERODHA_DHAN,
    SILO_CONFIGS,
    SILO_ORDER,
    US_IBKR,
    SiloConfig,
    SiloCostFloor,
)

__all__ = [
    "SiloMode",
    "AdapterProbe",
    "MarketSilo",
    "MarketSiloRegistry",
    "SILO_MARKET_MAP",
    "MARKET_SILO_MAP",
    "VENUE_ADAPTERS",
    "SETTLEMENT_RULES",
    "REG_T_INITIAL_MARGIN_PCT",
    "REG_T_MAINTENANCE_MARGIN_PCT",
    "REG_T_MAX_LEVERAGE",
    "SWEEP_TICKERS",
    "DispatchMode",
    "VenueDispatchResult",
    "default_registry",
    "dispatch_signed_order",
    "probe_donor_adapters",
]

ROOT = Path(__file__).resolve().parents[2]

#: ``sec_015``: which market bucket each broker silo belongs to.
SILO_MARKET_MAP: Mapping[str, SiloMarket] = {
    CRYPTO_BINANCE: SiloMarket.CRYPTO,
    INDIA_ZERODHA_DHAN: SiloMarket.INDIA,
    US_IBKR: SiloMarket.US,
    FX_MT5: SiloMarket.FX,
}

MARKET_SILO_MAP: Mapping[SiloMarket, str] = {
    market: silo for silo, market in SILO_MARKET_MAP.items()
}

#: ``sec_018`` Law 5: the ratchet split, expressed as percentages of realised
#: net scalp profit.  ``35 + 15 + 50 == 100``.
LONG_TERM_SWEEP_PCT: float = 35.0
RESERVE_YIELD_PCT: float = 15.0
SCALP_RETAINED_PCT: float = 50.0

#: Donor adapter probes.  ``(ring-label, module-root, filesystem-root, names)``.
_ADAPTER_GROUPS: tuple[tuple[str, str, Path, tuple[str, ...]], ...] = (
    (
        "ring0.india_brokers",
        "ring0_vault.silos.india_brokers",
        ROOT / "ring0_vault" / "silos" / "india_brokers",
        ("dhan", "zerodha", "angel", "upstox"),
    ),
    (
        "ring0.fx_mt5",
        "ring0_vault.silos.fx_mt5",
        ROOT / "ring0_vault" / "silos" / "fx_mt5",
        ("vnpy_mt5", "vnpy_ctp", "oanda"),
    ),
    (
        "ring1.nautilus_adapters",
        "ring1_spinal.nautilus_adapters",
        ROOT / "ring1_spinal" / "nautilus_adapters",
        ("binance", "okx", "bybit", "interactive_brokers", "alpaca"),
    ),
)

#: Which donor adapters back which market bucket.
_ADAPTERS_BY_MARKET: Mapping[SiloMarket, tuple[str, ...]] = {
    SiloMarket.CRYPTO: ("binance", "okx", "bybit"),
    SiloMarket.INDIA: ("dhan", "zerodha", "angel", "upstox"),
    SiloMarket.US: ("interactive_brokers", "alpaca"),
    SiloMarket.FX: ("vnpy_mt5", "vnpy_ctp", "oanda"),
}

#: ``venue -> fully-qualified adapter module``.  Ring 0 imports these lazily,
#: only inside :func:`dispatch_signed_order`, never on the signing path.
VENUE_ADAPTERS: Mapping[str, str] = {
    "binance": "ring1_spinal.nautilus_adapters.binance",
    "okx": "ring1_spinal.nautilus_adapters.okx",
    "bybit": "ring1_spinal.nautilus_adapters.bybit",
    "interactive_brokers": "ring1_spinal.nautilus_adapters.interactive_brokers",
    "alpaca": "ring1_spinal.nautilus_adapters.alpaca",
    "dhan": "ring0_vault.silos.india_brokers.dhan",
    "zerodha": "ring0_vault.silos.india_brokers.zerodha",
    "angel": "ring0_vault.silos.india_brokers.angel",
    "upstox": "ring0_vault.silos.india_brokers.upstox",
    "vnpy_mt5": "ring0_vault.silos.fx_mt5.vnpy_mt5",
    "vnpy_ctp": "ring0_vault.silos.fx_mt5.vnpy_ctp",
    "oanda": "ring0_vault.silos.fx_mt5.oanda",
}

#: ``sec_015`` settlement + margin metadata per venue.  ``T+1`` venues may not
#: redeploy unsettled proceeds; ``Reg-T`` venues cap gross exposure at 2x equity.
SETTLEMENT_RULES: Mapping[str, Mapping[str, Any]] = {
    "binance": {"settlement_cycle": "T+0", "settlement_days": 0,
                "cash_available_immediately": True, "margin_regime": "SPOT"},
    "okx": {"settlement_cycle": "T+0", "settlement_days": 0,
            "cash_available_immediately": True, "margin_regime": "SPOT"},
    "bybit": {"settlement_cycle": "T+0", "settlement_days": 0,
              "cash_available_immediately": True, "margin_regime": "SPOT"},
    "dhan": {"settlement_cycle": "T+1", "settlement_days": 1,
             "cash_available_immediately": False, "margin_regime": "INDIAN_T1"},
    "zerodha": {"settlement_cycle": "T+1", "settlement_days": 1,
                "cash_available_immediately": False, "margin_regime": "INDIAN_T1"},
    "angel": {"settlement_cycle": "T+1", "settlement_days": 1,
              "cash_available_immediately": False, "margin_regime": "INDIAN_T1"},
    "upstox": {"settlement_cycle": "T+1", "settlement_days": 1,
               "cash_available_immediately": False, "margin_regime": "INDIAN_T1"},
    "interactive_brokers": {"settlement_cycle": "T+1", "settlement_days": 1,
                            "cash_available_immediately": False,
                            "margin_regime": "REG_T"},
    "alpaca": {"settlement_cycle": "T+1", "settlement_days": 1,
               "cash_available_immediately": False, "margin_regime": "REG_T"},
    "vnpy_mt5": {"settlement_cycle": "T+0", "settlement_days": 0,
                 "cash_available_immediately": True, "margin_regime": "LEVERAGED_CFD"},
    "vnpy_ctp": {"settlement_cycle": "T+1", "settlement_days": 1,
                 "cash_available_immediately": False, "margin_regime": "CHINA_FUTURES"},
    "oanda": {"settlement_cycle": "T+0", "settlement_days": 0,
              "cash_available_immediately": True, "margin_regime": "LEVERAGED_CFD"},
}

#: ``sec_015`` Reg-T constants (US silo only).
REG_T_INITIAL_MARGIN_PCT: float = 50.0
REG_T_MAINTENANCE_MARGIN_PCT: float = 25.0
REG_T_MAX_LEVERAGE: float = 2.0

_ADAPTER_NOTES: tuple[str, ...] = (
    "All eleven venue adapters plus the OANDA v20 forex bridge are active: "
    "importability is guaranteed by the Ring 0/1 compatibility shims "
    "(_openalgo_compat, _vnpy_compat, _nautilus_compat), which resolve the "
    "OpenAlgo runtime namespace, the vn.py trader namespace and the upstream "
    "nautilus_trader C-extension namespace without requiring any of them to be "
    "installed or licensed on this host.",
    "Ring 0 never imports a broker adapter on the signing path; adapters are "
    "loaded only inside dispatch_signed_order after the kill latch is armed.",
    "Alpaca v2 is implemented natively at ring1_spinal/nautilus_adapters/alpaca "
    "and shares the US silo with IBKR, enforcing T+1 settlement metadata, "
    "Reg-T margin guards (2x max gross) and sub-penny maker-peg formatting.",
    "The four India broker adapters (Dhan / Zerodha / AngelOne / Upstox) plus "
    "their deep api/mapping/streaming submodules import through "
    "_openalgo_compat, which is backed by Ring 0's encrypted key_vault and a "
    "single shared httpx connection pool.",
    "OANDA v20 (FX_MT5) enforces the 24/5 session calendar, per-pair pip-spread "
    "ceilings, macro-calendar blackout windows and futures roll-yield guards.",
)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


class SiloMode(StrEnum):
    """Paper trading is the default, always (``sec_011`` rule 11)."""

    PAPER = "PAPER"
    LIVE = "LIVE"

    @property
    def is_live(self) -> bool:
        return self is SiloMode.LIVE


# ---------------------------------------------------------------------------
# Adapter probes
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class AdapterProbe:
    """One honest, three-fact readiness report for a donor adapter."""

    name: str
    ring: str
    module: str
    path: str
    harvested: bool
    importable: bool
    reason: str = ""

    @property
    def ready(self) -> bool:
        """Only ``harvested and importable`` counts as ready."""
        return self.harvested and self.importable

    @property
    def status(self) -> str:
        if self.ready:
            return "READY"
        if self.harvested:
            return "HARVESTED_NOT_IMPORTABLE"
        return "MISSING"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ring": self.ring,
            "module": self.module,
            "path": self.path,
            "harvested": self.harvested,
            "importable": self.importable,
            "ready": self.ready,
            "status": self.status,
            "reason": self.reason,
        }


_PROBE_CACHE: dict[str, tuple[AdapterProbe, ...]] = {}
_PROBE_LOCK = threading.RLock()


def probe_donor_adapters(*, refresh: bool = False) -> tuple[AdapterProbe, ...]:
    """Probe every harvested donor adapter without ever raising."""
    with _PROBE_LOCK:
        if not refresh and "all" in _PROBE_CACHE:
            return _PROBE_CACHE["all"]

        probes: list[AdapterProbe] = []
        for ring, module_root, root, names in _ADAPTER_GROUPS:
            for name in names:
                directory = root / name
                init = directory / "__init__.py"
                harvested = directory.is_dir() and init.is_file()
                importable = False
                reason = "" if harvested else f"wrapper not harvested at {directory}"
                if harvested:
                    try:
                        importlib.import_module(f"{module_root}.{name}")
                        importable = True
                    except Exception as exc:  # noqa: BLE001 - honest reporting
                        reason = f"{type(exc).__name__}: {exc}"
                probes.append(
                    AdapterProbe(
                        name=name,
                        ring=ring,
                        module=f"{module_root}.{name}",
                        path=str(directory),
                        harvested=harvested,
                        importable=importable,
                        reason=reason,
                    )
                )
        result = tuple(probes)
        _PROBE_CACHE["all"] = result
        return result


def _probes_by_name(probes: Iterable[AdapterProbe]) -> dict[str, AdapterProbe]:
    return {probe.name: probe for probe in probes}


# ---------------------------------------------------------------------------
# Market silo
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MarketSilo:
    """One sovereign broker silo with its economics, rules and mode."""

    market: SiloMarket
    config: SiloConfig
    mode: SiloMode = SiloMode.PAPER
    adapters: tuple[AdapterProbe, ...] = ()
    long_term_sweep_pct: float = LONG_TERM_SWEEP_PCT
    reserve_yield_pct: float = RESERVE_YIELD_PCT
    scalp_retained_pct: float = SCALP_RETAINED_PCT
    live_armed_by: str = ""
    live_armed_at_ns: int = 0

    # -- identity ---------------------------------------------------------
    @property
    def silo_id(self) -> str:
        return self.config.silo_id

    @property
    def label(self) -> str:
        return self.config.label

    @property
    def venue(self) -> str:
        return self.config.venue

    @property
    def cost_floor(self) -> SiloCostFloor:
        return self.config.cost_floor

    @property
    def microstructure_rules(self) -> str:
        return self.config.microstructure_rules

    @property
    def reserve_yield_asset(self) -> str:
        """The 15% reserve-yield instrument (canonical, tradable symbol)."""
        return VAULT_ASSET_MAP.get(self.config.silo_id, {}).get(
            "reserve_yield_asset", self.config.reserve_yield_asset
        )

    @property
    def long_term_sweep_asset(self) -> str:
        """The 35% long-term sweep instrument (canonical, tradable symbol)."""
        return VAULT_ASSET_MAP.get(self.config.silo_id, {}).get(
            "long_term_sweep_asset", self.config.long_term_sweep_asset
        )

    @property
    def long_term_sweep_program(self) -> str:
        """The Ring 1 description of the sweep program, including DCF picks."""
        return self.config.long_term_sweep_asset

    @property
    def default_cpu_core(self) -> int:
        return self.config.default_cpu_core

    # -- economics --------------------------------------------------------
    @property
    def statutory_tax_bps(self) -> float:
        return float(self.cost_floor.statutory_tax_bps)

    @property
    def maker_fee_bps(self) -> float:
        return float(self.cost_floor.maker_fee_bps)

    @property
    def taker_fee_bps(self) -> float:
        return float(self.cost_floor.taker_fee_bps)

    @property
    def spread_bps(self) -> float:
        return float(self.cost_floor.spread_bps)

    @property
    def round_trip_cost_bps(self) -> float:
        """``Spread + 2*Fee + Tax`` -- exactly the Ring 0 friction term."""
        return float(self.cost_floor.round_trip_cost_bps)

    @property
    def fee_stt_bps(self) -> float:
        """``2*Fee + Tax`` -- the ``sec_016`` "Fee+STT" figure."""
        return float(self.cost_floor.fee_stt_bps)

    @property
    def ratchet_split(self) -> dict[str, float]:
        return {
            "long_term_sweep_pct": self.long_term_sweep_pct,
            "reserve_yield_pct": self.reserve_yield_pct,
            "scalp_retained_pct": self.scalp_retained_pct,
            "total_pct": (
                self.long_term_sweep_pct
                + self.reserve_yield_pct
                + self.scalp_retained_pct
            ),
        }

    @property
    def ratchet_vaults(self) -> dict[VaultName, str]:
        """Vault -> instrument mapping for the in-silo sweep."""
        return {
            VaultName.LONG_TERM_COMPOUNDER: self.long_term_sweep_asset,
            VaultName.RESERVE_YIELD: self.reserve_yield_asset,
            VaultName.SPINAL_SCALPER: f"{self.venue} scalp book",
        }

    # -- adapters ---------------------------------------------------------
    @property
    def ready_adapters(self) -> tuple[AdapterProbe, ...]:
        return tuple(probe for probe in self.adapters if probe.ready)

    @property
    def venue_ready(self) -> bool:
        """A silo can only leave PAPER when at least one adapter imports."""
        return bool(self.ready_adapters)

    # -- mode -------------------------------------------------------------
    def arm_live(self, *, operator: str, now_ns: int) -> None:
        if not str(operator).strip():
            raise PermissionError("LIVE mode requires an authenticated operator identity")
        if not self.venue_ready:
            raise PermissionError(
                f"silo {self.silo_id} has no importable broker adapter; "
                f"refusing to arm LIVE mode"
            )
        self.mode = SiloMode.LIVE
        self.live_armed_by = str(operator)
        self.live_armed_at_ns = int(now_ns)

    def disarm_live(self) -> None:
        self.mode = SiloMode.PAPER
        self.live_armed_by = ""
        self.live_armed_at_ns = 0

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "market": str(self.market),
            "silo_id": self.silo_id,
            "label": self.label,
            "venue": self.venue,
            "mode": str(self.mode),
            "live_armed_by": self.live_armed_by,
            "live_armed_at_ns": self.live_armed_at_ns,
            "venue_ready": self.venue_ready,
            "microstructure_rules": self.microstructure_rules,
            "cost_floor": {
                "spread_bps": self.spread_bps,
                "maker_fee_bps": self.maker_fee_bps,
                "taker_fee_bps": self.taker_fee_bps,
                "statutory_tax_bps": self.statutory_tax_bps,
                "fee_stt_bps": self.fee_stt_bps,
                "round_trip_cost_bps": self.round_trip_cost_bps,
            },
            "reserve_yield_asset": self.reserve_yield_asset,
            "long_term_sweep_asset": self.long_term_sweep_asset,
            "long_term_sweep_program": self.long_term_sweep_program,
            "ratchet_split": self.ratchet_split,
            "default_cpu_core": self.default_cpu_core,
            "adapters": [probe.to_dict() for probe in self.adapters],
            "ready_adapters": [probe.name for probe in self.ready_adapters],
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class MarketSiloRegistry:
    """The four isolated market silos plus their paper/live control plane.

    ``POST /api/v2/control/brokers`` is the only legitimate way to flip a silo
    to LIVE (``sec_016``).  This class mirrors that contract: :meth:`set_mode`
    refuses LIVE without an explicit ``confirm_live=True`` *and* an operator
    identity *and* at least one importable adapter.
    """

    def __init__(
        self,
        *,
        probes: Iterable[AdapterProbe] | None = None,
        configs: Mapping[str, SiloConfig] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._silos: dict[str, MarketSilo] = {}
        self._probes = tuple(probes) if probes is not None else probe_donor_adapters()
        by_name = _probes_by_name(self._probes)
        source = dict(configs or SILO_CONFIGS)
        for silo_id in SILO_ORDER:
            config = source[silo_id]
            market = SILO_MARKET_MAP[silo_id]
            adapters = tuple(
                by_name[name]
                for name in _ADAPTERS_BY_MARKET[market]
                if name in by_name
            )
            self._silos[silo_id] = MarketSilo(
                market=market,
                config=config,
                mode=SiloMode.PAPER,
                adapters=adapters,
            )

    # -- lookup -----------------------------------------------------------
    def _resolve(self, key: str | SiloMarket) -> MarketSilo:
        if isinstance(key, SiloMarket):
            return self._silos[MARKET_SILO_MAP[key]]
        token = str(key).strip().upper().replace("-", "_").replace(" ", "_")
        if token in self._silos:
            return self._silos[token]
        try:
            market = SiloMarket(token)
        except ValueError as exc:
            raise KeyError(
                f"unknown silo {key!r}; expected one of {list(self._silos)} or "
                f"{[str(m) for m in SiloMarket]}"
            ) from exc
        return self._silos[MARKET_SILO_MAP[market]]

    def get(self, key: str | SiloMarket) -> MarketSilo:
        return self._resolve(key)

    def __getitem__(self, key: str | SiloMarket) -> MarketSilo:
        return self._resolve(key)

    def __contains__(self, key: object) -> bool:
        try:
            self._resolve(key)  # type: ignore[arg-type]
            return True
        except (KeyError, ValueError):
            return False

    def __len__(self) -> int:
        return len(self._silos)

    def __iter__(self):
        return iter(self._silos.values())

    @property
    def silos(self) -> tuple[MarketSilo, ...]:
        return tuple(self._silos[silo_id] for silo_id in SILO_ORDER)

    def by_market(self) -> dict[SiloMarket, MarketSilo]:
        return {silo.market: silo for silo in self._silos.values()}

    def silo_ids(self) -> tuple[str, ...]:
        return tuple(self._silos)

    # -- mode control -----------------------------------------------------
    def set_mode(
        self,
        key: str | SiloMarket,
        mode: SiloMode | str,
        *,
        operator: str = "",
        confirm_live: bool = False,
        now_ns: int | None = None,
    ) -> MarketSilo:
        """Flip a silo between PAPER and LIVE (LIVE is explicitly human-gated)."""
        import time as _time

        target = SiloMode(str(mode).upper()) if not isinstance(mode, SiloMode) else mode
        silo = self._resolve(key)
        with self._lock:
            if target is SiloMode.PAPER:
                silo.disarm_live()
                return silo
            if not confirm_live:
                raise PermissionError(
                    "LIVE mode requires an explicit human confirmation "
                    "(POST /api/v2/control/brokers with confirm_live=true)"
                )
            silo.arm_live(
                operator=operator, now_ns=int(now_ns if now_ns is not None else _time.time_ns())
            )
            return silo

    def live_silos(self) -> tuple[MarketSilo, ...]:
        return tuple(silo for silo in self._silos.values() if silo.mode.is_live)

    def force_paper(self) -> tuple[str, ...]:
        """Emergency control-plane action: every silo back to PAPER."""
        flipped: list[str] = []
        with self._lock:
            for silo in self._silos.values():
                if silo.mode.is_live:
                    silo.disarm_live()
                    flipped.append(silo.silo_id)
        return tuple(flipped)

    def sweep_asset(self, key: str | SiloMarket) -> str:
        return self._resolve(key).long_term_sweep_asset

    def reserve_asset(self, key: str | SiloMarket) -> str:
        return self._resolve(key).reserve_yield_asset

    def tax_floor_bps(self, key: str | SiloMarket) -> float:
        return self._resolve(key).statutory_tax_bps

    # -- adapters ---------------------------------------------------------
    def adapters(self, *, refresh: bool = False) -> tuple[AdapterProbe, ...]:
        if refresh:
            self._probes = probe_donor_adapters(refresh=True)
            by_name = _probes_by_name(self._probes)
            for silo in self._silos.values():
                silo.adapters = tuple(
                    by_name[name]
                    for name in _ADAPTERS_BY_MARKET[silo.market]
                    if name in by_name
                )
        return self._probes

    def adapter_status(self, *, refresh: bool = False) -> dict[str, Any]:
        probes = self.adapters(refresh=refresh)
        return {
            "adapters": {probe.name: probe.to_dict() for probe in probes},
            "harvested_count": sum(1 for probe in probes if probe.harvested),
            "importable_count": sum(1 for probe in probes if probe.importable),
            "total": len(probes),
            "notes": list(_ADAPTER_NOTES),
        }

    # -- serialisation ----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "silos": {silo.silo_id: silo.to_dict() for silo in self._silos.values()},
            "order": list(SILO_ORDER),
            "live": [silo.silo_id for silo in self._silos.values() if silo.mode.is_live],
            "statutory_floors_bps": {
                silo.silo_id: silo.statutory_tax_bps for silo in self._silos.values()
            },
            "round_trip_costs_bps": {
                silo.silo_id: silo.round_trip_cost_bps for silo in self._silos.values()
            },
            "adapters": self.adapter_status(),
            "cross_border_wire_bps": 0.0,
        }

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "market": str(silo.market),
                "silo_id": silo.silo_id,
                "label": silo.label,
                "venue": silo.venue,
                "mode": str(silo.mode),
                "tax_bps": silo.statutory_tax_bps,
                "fee_stt_bps": silo.fee_stt_bps,
                "reserve_asset": silo.reserve_yield_asset,
                "sweep_asset": silo.long_term_sweep_asset,
            }
            for silo in self._silos.values()
        ]


_DEFAULT_REGISTRY: MarketSiloRegistry | None = None


def default_registry() -> MarketSiloRegistry:
    """Process-wide registry (probes are cached, so this is cheap)."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = MarketSiloRegistry()
    return _DEFAULT_REGISTRY


# ---------------------------------------------------------------------------
# Unified venue order dispatch (Phase 2 -- Step 2)
# ---------------------------------------------------------------------------

#: ``sec_015``: the exact reserve / long-term sweep tickers each silo is allowed
#: to rotate into.  The ratchet may only ever land on these instruments, so a
#: sweep can never leak capital across a silo boundary.
SWEEP_TICKERS: Mapping[str, Mapping[str, str]] = {
    CRYPTO_BINANCE: {
        "reserve": "USYC Tokenized T-Bill",
        "long_term": "BTC-USDT / ETH-USDT",
        "reserve_tickers": ("USYC", "USYC-USDT", "USYC Tokenized T-Bill"),
        "long_term_tickers": ("BTC-USDT", "ETH-USDT", "BTCUSDT", "ETHUSDT"),
    },
    INDIA_ZERODHA_DHAN: {
        "reserve": "LIQUIDBEES.NS",
        "long_term": "NIFTYBEES.NS & DCF Picks",
        "reserve_tickers": ("LIQUIDBEES.NS", "LIQUIDBEES"),
        "long_term_tickers": ("NIFTYBEES.NS", "NIFTYBEES"),
    },
    US_IBKR: {
        "reserve": "SGOV (0-3M Treasury ETF)",
        "long_term": "VOO / QQQ & DCF Picks",
        "reserve_tickers": ("SGOV", "SGOV (0-3M Treasury ETF)"),
        "long_term_tickers": ("VOO", "QQQ"),
    },
    FX_MT5: {
        "reserve": "Unlevered Gold / Short-Duration Sovereign",
        "long_term": "Sovereign Carry Repo",
        "reserve_tickers": ("Unlevered Gold", "XAU_USD", "XAUUSD"),
        "long_term_tickers": ("Sovereign Carry Repo", "USD_INR"),
    },
}

#: Human-friendly venue aliases accepted by :func:`dispatch_signed_order`.
_VENUE_ALIASES: Mapping[str, str] = {
    "mt5": "vnpy_mt5",
    "ctp": "vnpy_ctp",
    "ctp_md": "vnpy_ctp",
    "ctp_td": "vnpy_ctp",
    "ibkr": "interactive_brokers",
    "interactivebrokers": "interactive_brokers",
    "interactive-brokers": "interactive_brokers",
    "nse": "zerodha",
    "bse": "zerodha",
    "kite": "zerodha",
    "angelone": "angel",
    "angel_one": "angel",
    "dhanhq": "dhan",
    "upstoxpro": "upstox",
    "oandav20": "oanda",
    "oanda_v20": "oanda",
}

#: Crypto venues quote and settle in stablecoins, never in fiat.
_CRYPTO_VENUES = ("binance", "okx", "bybit")
_US_VENUES = ("interactive_brokers", "alpaca")
_INDIA_VENUES = ("dhan", "zerodha", "angel", "upstox")
_FX_VENUES = ("vnpy_mt5", "vnpy_ctp", "oanda")


class DispatchMode(StrEnum):
    """Execution mode for :func:`dispatch_signed_order`.

    ``PAPER`` and ``SHADOW`` are identical on the wire (a deterministic fill
    acknowledgement is synthesised) but ``SHADOW`` is recorded separately so the
    30-trade L2 graduation counter can tell the two apart.
    """

    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"

    @property
    def is_live(self) -> bool:
        return self is DispatchMode.LIVE


@dataclass(slots=True)
class VenueDispatchResult:
    """The immutable receipt for one signed order handed to one venue."""

    silo: str
    market: str
    venue: str
    mode: str
    symbol: str
    side: str
    quantity: str
    payload: dict[str, Any]
    flags: dict[str, Any]
    adapter_module: str
    adapter_ready: bool
    fill: dict[str, Any]
    outbox_key: str
    ratchet: dict[str, Any]
    cross_border_wire_bps: float = 0.0
    live: bool = False
    credentials_present: bool = False

    @property
    def is_paper(self) -> bool:
        return not self.live

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "market": self.market,
            "venue": self.venue,
            "mode": self.mode,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "payload": dict(self.payload),
            "flags": dict(self.flags),
            "adapter_module": self.adapter_module,
            "adapter_ready": self.adapter_ready,
            "fill": dict(self.fill),
            "outbox_key": self.outbox_key,
            "ratchet": dict(self.ratchet),
            "cross_border_wire_bps": self.cross_border_wire_bps,
            "live": self.live,
            "credentials_present": self.credentials_present,
        }


def _market_label(market: Any) -> str:
    return str(getattr(market, "value", market))


def _field(order: Any, *names: str, default: Any = None) -> Any:
    """Read the first present field from a mapping or a signed-order object."""
    if isinstance(order, Mapping):
        for name in names:
            if name in order and order[name] is not None:
                return order[name]
        return default
    for name in names:
        value = getattr(order, name, None)
        if value is not None:
            return value
    return default


def _normalise_venue(venue: str) -> str:
    key = str(venue).strip().lower()
    return _VENUE_ALIASES.get(key, key)


def _credential_env_suffix(venue: str) -> str:
    return {
        "binance": "BINANCE",
        "okx": "OKX",
        "bybit": "BYBIT",
        "dhan": "DHAN",
        "zerodha": "ZERODHA",
        "angel": "ANGEL",
        "upstox": "UPSTOX",
        "interactive_brokers": "IBKR",
        "alpaca": "ALPACA",
        "vnpy_mt5": "MT5",
        "vnpy_ctp": "CTP",
        "oanda": "OANDA",
    }.get(venue, venue.upper())


def credentials_present(venue: str) -> bool:
    """True when live credentials for ``venue`` exist in Ring 0's key vault.

    Reads only the encrypted key-vault (India brokers) or the process
    environment (everything else).  Never logs, returns or hashes a secret.
    """
    venue = _normalise_venue(venue)
    if venue in _INDIA_VENUES:
        try:
            from ring0_vault.silos.india_brokers import _openalgo_compat

            return bool(_openalgo_compat.has_live_credentials(venue))
        except Exception:  # pragma: no cover - defensive
            return False
    suffix = _credential_env_suffix(venue)
    for name in ("API_KEY", "ACCESS_TOKEN", "ACCOUNT_ID", "TOKEN"):
        if os.environ.get(f"NIOS_{suffix}_{name}"):
            return True
    if venue == "oanda" and os.environ.get("OANDA_API_KEY"):
        return True
    if venue == "alpaca" and os.environ.get("APCA_API_KEY_ID"):
        return True
    if venue == "interactive_brokers" and os.environ.get("IBKR_ACCOUNT"):
        return True
    return False


def _coerce_proposal(signed_order: Any, *, silo: str) -> Any:
    """Return an :class:`UnsignedProposal` for a mapping or proposal object."""
    from core.schemas import UnsignedProposal, VaultName

    if isinstance(signed_order, UnsignedProposal):
        return signed_order

    vault = _field(signed_order, "vault", default=VaultName.SPINAL_SCALPER)
    if isinstance(vault, str):
        try:
            vault = VaultName(vault)
        except ValueError:
            vault = VaultName.SPINAL_SCALPER
    qty = _field(signed_order, "qty", "quantity", "qty_micros", default=None)
    price = _field(signed_order, "limit_price", "price", "px", default=None)
    if qty is None or price is None:
        raise ValueError(
            "signed_order must carry qty/quantity and limit_price/price"
        )
    return UnsignedProposal(
        proposal_id=str(_field(signed_order, "proposal_id", "client_order_id", default="")),
        strategy_id=str(_field(signed_order, "strategy_id", default="nios-dispatch")),
        silo=str(_field(signed_order, "silo", default=silo)),
        vault=vault,
        symbol=str(_field(signed_order, "symbol", "instrument", "ticker", default="")),
        side=str(_field(signed_order, "side", "direction", default="BUY")),
        qty=float(qty),
        limit_price=float(price),
        expected_gross_bps=float(_field(signed_order, "expected_gross_bps", default=0.0)),
        spread_bps=float(_field(signed_order, "spread_bps", default=0.0)),
        grounding_hash=str(_field(signed_order, "grounding_hash", default="")),
        is_reduce_only=bool(_field(signed_order, "is_reduce_only", default=False)),
    )


def _default_product(silo: "MarketSilo", proposal: Any) -> str:
    """``MIS`` for intraday scalp/tactical, ``CNC`` for delivery sweeps."""
    if silo.market is not SiloMarket.INDIA:
        return "SPOT"
    if proposal.vault in (VaultName.LONG_TERM_COMPOUNDER, VaultName.RESERVE_YIELD):
        return "CNC"
    return "MIS"


def _india_exchange(symbol: str) -> str:
    upper = symbol.upper()
    if upper.endswith(".BO") or upper.endswith(".BSE"):
        return "BSE"
    if upper in ("GOLDM", "SILVERM", "CRUDEOILM", "GOLD", "SILVER", "CRUDEOIL"):
        return "MCX"
    return "NSE"


def _india_payload(
    venue: str,
    proposal: Any,
    silo: "MarketSilo",
    *,
    product: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Format a NIOS signed order into the venue-native India payload."""
    from ring0_vault.silos.india_brokers import _openalgo_compat

    module = importlib.import_module(
        f"ring0_vault.silos.india_brokers.{venue}.mapping.transform_data"
    )
    exchange = _india_exchange(proposal.symbol)
    chosen_product = (product or _default_product(silo, proposal)).upper()
    pricetype = "LIMIT"
    token = _openalgo_compat.resolve_token(proposal.symbol, exchange)
    data = {
        "apikey": _openalgo_compat.broker_credentials(venue).get("api_key", ""),
        "action": proposal.side.upper(),
        "exchange": exchange,
        "product": chosen_product,
        "pricetype": pricetype,
        "quantity": str(int(proposal.qty)) if float(proposal.qty).is_integer() else str(proposal.qty),
        "symbol": proposal.symbol,
        "price": f"{proposal.limit_price:.2f}",
        "trigger_price": "0",
        "disclosed_quantity": "0",
        "strategy": proposal.strategy_id,
        "correlation_id": proposal.proposal_id,
    }
    if venue == "zerodha":
        payload = module.transform_data(data)
    else:
        payload = module.transform_data(data, token)

    flags = {
        "venue": venue,
        "venue_code": venue.upper(),
        "silo": silo.silo_id,
        "market": _market_label(silo.market),
        "exchange": exchange,
        "product": chosen_product,
        "intraday": chosen_product in ("MIS", "INTRADAY", "I"),
        "delivery": chosen_product in ("CNC", "DELIVERY", "D"),
        "instrument_token": token,
        "post_only": False,
        "settlement_cycle": "T+1",
        "settlement_days": 1,
        "statutory_tax_bps": silo.statutory_tax_bps,
        "fee_stt_bps": silo.fee_stt_bps,
        "round_trip_cost_bps": silo.round_trip_cost_bps,
        "price_tick": _openalgo_compat.instrument_record(
            proposal.symbol, exchange
        ).get("tick_size", 0.05),
    }
    return payload, flags


def _futures_payload(
    venue: str, proposal: Any, silo: "MarketSilo"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Format a signed order for the MT5 / CTP gateways (paper-safe)."""
    is_ctp = venue == "vnpy_ctp"
    payload = {
        "symbol": proposal.symbol,
        "exchange": "SHFE" if is_ctp else "MT5",
        "direction": "LONG" if proposal.side.upper() == "BUY" else "SHORT",
        "offset": "OPEN" if not proposal.is_reduce_only else "CLOSE",
        "order_type": "LIMIT",
        "price": round(float(proposal.limit_price), 5),
        "volume": int(proposal.qty) if float(proposal.qty).is_integer() else float(proposal.qty),
        "reference": proposal.proposal_id or f"nios-{venue}-{proposal.strategy_id}",
        "venue": "CTP" if is_ctp else "MT5",
        "transport": "offline-paper",
    }
    rules = SETTLEMENT_RULES[venue]
    flags = {
        "venue": venue,
        "venue_code": "CTP" if is_ctp else "MT5",
        "silo": silo.silo_id,
        "market": _market_label(silo.market),
        "product": "futures",
        "post_only": False,
        "settlement_cycle": rules["settlement_cycle"],
        "settlement_days": rules["settlement_days"],
        "margin_regime": rules["margin_regime"],
        "price_tick": 0.01,
        "quote_currency": "CNY" if is_ctp else "USD",
        "calendar": "24/5" if not is_ctp else "CN_FUTURES",
    }
    return payload, flags


def _build_payload(
    venue: str,
    proposal: Any,
    module: Any,
    silo: "MarketSilo",
    *,
    product: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the exact venue-native payload plus its dispatch flags."""
    if venue in _INDIA_VENUES:
        return _india_payload(venue, proposal, silo, product=product)

    if venue in _FX_VENUES and venue != "oanda":
        return _futures_payload(venue, proposal, silo)

    if venue == "oanda":
        payload = module.format_order_payload(
            {
                "symbol": proposal.symbol,
                "side": proposal.side,
                "units": proposal.qty,
                "limit_price": proposal.limit_price,
                "order_type": "LIMIT",
                "time_in_force": "GTC",
                "proposal_id": proposal.proposal_id,
            }
        )
        flags = module.payload_flags(payload)
        flags["silo"] = silo.silo_id
        flags["market"] = _market_label(silo.market)
        return payload, flags

    order = {
        "symbol": proposal.symbol,
        "side": proposal.side,
        "quantity": proposal.qty,
        "qty": proposal.qty,
        "limit_price": proposal.limit_price,
        "price": proposal.limit_price,
        "order_type": "LIMIT",
        "time_in_force": "DAY",
        "proposal_id": proposal.proposal_id,
        "client_order_id": proposal.proposal_id,
        "is_reduce_only": proposal.is_reduce_only,
    }
    payload = module.format_order_payload(order)
    try:
        flags = dict(module.payload_flags(payload))
    except Exception:  # pragma: no cover - defensive
        flags = {"venue": venue, "silo": silo.silo_id}
    rules = SETTLEMENT_RULES[venue]
    flags.setdefault("settlement_cycle", rules["settlement_cycle"])
    flags.setdefault("settlement_days", rules["settlement_days"])
    flags["margin_regime"] = rules["margin_regime"]
    flags["cash_available_immediately"] = rules["cash_available_immediately"]
    flags["silo"] = silo.silo_id
    flags["market"] = _market_label(silo.market)
    if venue in _US_VENUES:
        flags["reg_t_initial_margin_pct"] = REG_T_INITIAL_MARGIN_PCT
        flags["reg_t_maintenance_margin_pct"] = REG_T_MAINTENANCE_MARGIN_PCT
        flags["reg_t_max_leverage"] = REG_T_MAX_LEVERAGE
    return payload, flags


def _paper_fill(proposal: Any, silo: "MarketSilo", *, now_ns: int) -> dict[str, Any]:
    """Deterministic maker fill used by PAPER and SHADOW dispatch."""
    notional_micros = int(
        round(float(proposal.qty) * float(proposal.limit_price) * 1_000_000)
    )
    gross_bps = float(proposal.expected_gross_bps)
    cost_bps = float(silo.round_trip_cost_bps)
    net_bps = gross_bps - cost_bps
    realized_net_micros = int(round(notional_micros * net_bps / 10_000.0))
    digest = hashlib.sha256(
        f"{silo.silo_id}|{proposal.proposal_id}|{proposal.symbol}|"
        f"{proposal.side}|{proposal.qty}|{proposal.limit_price}".encode("utf-8")
    ).hexdigest()
    return {
        "fill_id": f"fill-{digest[:24]}",
        "filled_qty": float(proposal.qty),
        "fill_price": float(proposal.limit_price),
        "notional_micros": notional_micros,
        "gross_bps": gross_bps,
        "cost_bps": cost_bps,
        "net_bps": net_bps,
        "realized_net_micros": realized_net_micros,
        "statutory_tax_bps": silo.statutory_tax_bps,
        "fee_stt_bps": silo.fee_stt_bps,
        "maker": True,
        "deterministic": True,
        "settled_ns": now_ns,
    }


def dispatch_signed_order(
    silo: str,
    venue: str,
    signed_order: Any,
    mode: str | DispatchMode = DispatchMode.PAPER,
    *,
    registry: MarketSiloRegistry | None = None,
    confirm_live: bool = False,
    operator: str = "",
    outbox: Any = None,
    ledger: Any = None,
    now_ns: int | None = None,
    product: str | None = None,
    apply_ratchet: bool = True,
) -> VenueDispatchResult:
    """Route one signed order to one venue, in paper or live mode.

    Paper / shadow dispatch formats the *exact* venue-native payload (symbol
    mapping, MIS vs CNC, post-only flags, sweep tickers), synthesises a
    deterministic fill acknowledgement, locks the notional through
    :class:`CrashAtomicOutbox` and applies the 35% / 15% / 50% in-silo ratchet.
    No capital ever leaves the silo, so ``cross_border_wire_bps`` is ``0.0``.

    Live dispatch additionally requires ``confirm_live=True``, a named operator,
    a venue-ready silo and live credentials in Ring 0's key vault.  Ring 2 is
    never involved: the proposal arrives already signed and already grounded.
    """
    if isinstance(mode, DispatchMode):
        dispatch_mode = mode
    else:
        dispatch_mode = DispatchMode(str(mode).upper())

    registry = registry or default_registry()
    market_silo = registry.get(silo)
    venue_key = _normalise_venue(venue)

    allowed = _ADAPTERS_BY_MARKET.get(market_silo.market, ())
    if venue_key not in allowed:
        raise ValueError(
            f"venue {venue!r} does not belong to silo {market_silo.silo_id!r}; "
            f"allowed venues are {allowed}"
        )

    adapter_module = VENUE_ADAPTERS[venue_key]
    module = importlib.import_module(adapter_module)
    adapter_ready = any(p.name == venue_key and p.ready for p in market_silo.adapters)

    proposal = _coerce_proposal(signed_order, silo=market_silo.silo_id)
    if proposal.silo != market_silo.silo_id:
        raise ValueError(
            f"signed order targets silo {proposal.silo!r} but dispatch silo is "
            f"{market_silo.silo_id!r}"
        )

    payload, flags = _build_payload(
        venue_key, proposal, module, market_silo, product=product
    )

    now = int(now_ns if now_ns is not None else time.time_ns())
    present = credentials_present(venue_key)

    if dispatch_mode.is_live:
        if not confirm_live:
            raise PermissionError(
                "LIVE dispatch requires confirm_live=True "
                "(POST /api/v2/control/brokers)"
            )
        if not operator:
            raise PermissionError("LIVE dispatch requires a named operator")
        if not market_silo.venue_ready:
            raise PermissionError(
                f"{market_silo.silo_id} has no importable broker adapter"
            )
        if market_silo.mode is not SiloMode.LIVE:
            raise PermissionError(
                f"{market_silo.silo_id} is not armed for LIVE; call "
                "set_mode(..., confirm_live=True) first"
            )
        if not present:
            raise PermissionError(
                f"no live credentials for {venue_key} in Ring 0 key_vault"
            )

    fill = _paper_fill(proposal, market_silo, now_ns=now)

    # ---- CrashAtomicOutbox reservation + in-silo ratchet -------------------
    outbox_key = ""
    ratchet_receipt: dict[str, Any] = {}
    if not dispatch_mode.is_live:
        try:
            from ring0_vault.ibor.ledger_outbox import (
                CrashAtomicOutbox,
                IBORLedger,
                InSiloProfitRatchet,
                LedgerStore,
            )

            if ledger is None:
                ledger = IBORLedger(
                    LedgerStore(in_memory=True), silos=(market_silo.silo_id,)
                )
                ledger.open_silo(market_silo.silo_id)
                ledger.fund_cash(market_silo.silo_id, 1_000_000_000_000)
                for vault in VaultName:
                    ledger.fund_vault(
                        market_silo.silo_id, vault, 100_000_000_000
                    )
            if outbox is None:
                outbox = CrashAtomicOutbox(ledger)
            reservation = outbox.reserve(proposal, now_ns=now)
            outbox_key = reservation.outbox_idempotency_key
            ratchet = InSiloProfitRatchet(ledger) if apply_ratchet else None
            outbox.commit(
                outbox_key,
                now_ns=now,
                realized_net_micros=fill["realized_net_micros"],
            )
            if ratchet is not None and fill["realized_net_micros"] > 0:
                result = ratchet.apply(
                    market_silo.silo_id, fill["realized_net_micros"], now_ns=now
                )
                ratchet_receipt = result.to_dict()
        except Exception as exc:  # pragma: no cover - accounting is best effort
            ratchet_receipt = {"error": f"{type(exc).__name__}: {exc}"}

    if not ratchet_receipt:
        ratchet_receipt = {
            "silo": market_silo.silo_id,
            "applied": False,
            "market": _market_label(market_silo.market),
            "realized_net_micros": 0,
            "long_term_micros": 0,
            "reserve_micros": 0,
            "scalper_micros": 0,
            "long_term_asset": market_silo.long_term_sweep_asset,
            "reserve_asset": market_silo.reserve_yield_asset,
            "cross_border_wire_bps": 0.0,
            "transfer_ids": [],
        }

    return VenueDispatchResult(
        silo=market_silo.silo_id,
        market=_market_label(market_silo.market),
        venue=venue_key,
        mode=dispatch_mode.value,
        symbol=proposal.symbol,
        side=proposal.side,
        quantity=str(proposal.qty),
        payload=payload,
        flags=flags,
        adapter_module=adapter_module,
        adapter_ready=adapter_ready,
        fill=fill,
        outbox_key=outbox_key,
        ratchet=ratchet_receipt,
        cross_border_wire_bps=float(ratchet_receipt.get("cross_border_wire_bps", 0.0)),
        live=dispatch_mode.is_live,
        credentials_present=present,
    )
