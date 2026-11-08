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

import importlib
import threading
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
    "default_registry",
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
        ("vnpy_mt5", "vnpy_ctp"),
    ),
    (
        "ring1.nautilus_adapters",
        "ring1_spinal.nautilus_adapters",
        ROOT / "ring1_spinal" / "nautilus_adapters",
        ("binance", "okx", "bybit", "interactive_brokers"),
    ),
)

#: Which donor adapters back which market bucket.
_ADAPTERS_BY_MARKET: Mapping[SiloMarket, tuple[str, ...]] = {
    SiloMarket.CRYPTO: ("binance", "okx", "bybit"),
    SiloMarket.INDIA: ("dhan", "zerodha", "angel", "upstox"),
    SiloMarket.US: ("interactive_brokers",),
    SiloMarket.FX: ("vnpy_mt5", "vnpy_ctp"),
}

_ADAPTER_NOTES: tuple[str, ...] = (
    "Harvested wrappers are thin re-exports; importability depends on the "
    "upstream SDK (nautilus_trader, MetaTrader5, CTP native DLLs, broker REST "
    "sessions) being installed and licensed on this host.",
    "Ring 0 never imports a broker adapter on the signing path; adapters are "
    "loaded only inside the silo worker after the kill latch is armed.",
    "Alpaca is a specified target with no harvested wrapper; the US silo runs "
    "IBKR until one is vendored (sec_005 harvest gap).",
    "Zerodha's package-root __init__.py was restored in Step 7 (Gap A), so all "
    "four India broker adapters (Dhan / Zerodha / AngelOne / Upstox) now probe "
    "READY. The streaming subpackage stays lazy because it imports the OpenAlgo "
    "runtime 'utils' package, which is not installed in an air-gapped Ring 0.",
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
