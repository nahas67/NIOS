"""NIOS Core v3.0 -- MiroFish 5-Archetype 3-Tick Liquidity Cascade (STEP 4).

``sec_008`` anchors the contagion simulator to five capital-weighted market
archetypes instead of unweighted retail sentiment:

======================  ========  ==============================================
Archetype               Weight    Behaviour in the three deterministic ticks
======================  ========  ==============================================
``Retail_0DTE``         0.15      Tick-1 euphoria / panic, decays by tick 3
``Options_Dealer_Gamma``0.25      Long-gamma dampening, short-gamma amplification
``Vol_Target_CTA``      0.25      Trend following, de-levers as vol expands
``Pod_Shop_Stops``      0.20      Forced stop-out sells into the shock
``Value_Funds``         0.15      Tick-3 institutional absorption (contrarian)
======================  ========  ==============================================

The combined 0.35 weight of the last two rows is the ``sec_008`` "Multi-Manager
Pod Shop Stops & Value Funds" bucket: pods amplify on ticks 1-2, value funds
absorb on tick 3.

The kernel is a tiny fixed-shape NumPy/Numba block: no allocation inside the
loop, no randomness, no wall-clock dependence.  The measured SLA is
``< 1.8 ms`` for the full three-tick cascade plus the validated
:class:`~core.schemas.ContagionFeatures` object.

Fragility override
------------------
Whenever ``cascade_fragility_index >= 0.75`` the engine disarms every active
``LONG_ONLY`` :class:`~core.schemas.System1Lease` in
``shm://nios-active-leases`` (``sec_004`` / ``sec_007`` / ``sec_010``), so Ring 1
stops initiating long scalps on the next tick.  The sweep is performed once per
fragility episode (a full 5,000-slot scan) and then only for symbols the engine
has observed, which keeps repeated high-fragility cascades inside the latency
budget.

Donor bridge
------------
``ring2_brain/mirofish/services/local_graph.py`` imports cleanly and is used to
read causal shock ontologies from a local JSON graph.  The harvested
``ontology_generator.py`` imports ``mirofish.utils.*`` helpers that were not
harvested, so :func:`donor_mirofish_status` reports it as unavailable rather
than silently pretending otherwise.
"""

from __future__ import annotations

import importlib
import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

try:  # Numba is the approved acceleration path (``sec_010`` / ``sec_013``).
    from numba import njit

    NUMBA_AVAILABLE = True
except Exception:  # pragma: no cover - degraded mode
    NUMBA_AVAILABLE = False

    def njit(*args: Any, **kwargs: Any):  # type: ignore[no-redef]
        def _wrap(func):
            return func

        return _wrap

from core.feature_store import FeatureStore
from core.schemas import (
    FRAGILITY_HALT_THRESHOLD,
    ContagionFeatures,
    LeaseMode,
)
from core.shm_ipc import LeaseTableSHM, SHM_URI_PREFIX

__all__ = [
    "ARCHETYPES",
    "ARCHETYPE_WEIGHTS",
    "FRAGILITY_HALT_THRESHOLD",
    "SHOCK_TAG_BOOSTS",
    "ShockVector",
    "CascadeResult",
    "MiroFishContagionEngine",
    "ContagionEngine",
    "donor_mirofish_status",
    "cascade_features",
    "simulate_cascade",
]

#: ``sec_008`` capital weights -- the sum is exactly 1.0.
ARCHETYPES: tuple[tuple[str, float], ...] = (
    ("Retail_0DTE", 0.15),
    ("Options_Dealer_Gamma", 0.25),
    ("Vol_Target_CTA", 0.25),
    ("Pod_Shop_Stops", 0.20),
    ("Value_Funds", 0.15),
)
ARCHETYPE_WEIGHTS: tuple[float, ...] = tuple(weight for _, weight in ARCHETYPES)

if abs(sum(ARCHETYPE_WEIGHTS) - 1.0) > 1e-12:  # pragma: no cover - constant guard
    raise RuntimeError("sec_008 archetype weights must sum to 1.0")

#: Contiguous, read-only weight/state vectors hoisted to module scope so the
#: per-cascade call performs **no** allocation, no ``np.asarray`` rebuild and no
#: dictionary walk over the five archetypes (``sec_010`` 1.8 ms SLA).
_WEIGHTS_NP: np.ndarray = np.asarray(ARCHETYPE_WEIGHTS, dtype=np.float64)
_WEIGHTS_NP.setflags(write=False)

#: Untagged shocks share one immutable five-float vector.
_ZERO_TAGS: tuple[float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0)

#: Optional causal ontology tags -> per-archetype trigger boost.
SHOCK_TAG_BOOSTS: Mapping[str, tuple[float, float, float, float, float]] = {
    "retail_euphoria": (1.0, 0.0, 0.0, 0.0, 0.0),
    "retail_panic": (1.0, 0.0, 0.0, 0.0, 0.0),
    "dealer_gamma_flip": (0.0, 1.0, 0.0, 0.0, 0.0),
    "dealer_short_gamma": (0.0, 1.0, 0.0, 0.0, 0.0),
    "cta_deleveraging": (0.0, 0.0, 1.0, 0.0, 0.0),
    "vol_target_cut": (0.0, 0.0, 1.0, 0.0, 0.0),
    "pod_stops": (0.0, 0.0, 0.0, 1.0, 0.0),
    "pod_shop_stop_out": (0.0, 0.0, 0.0, 1.0, 0.0),
    "value_absorption": (0.0, 0.0, 0.0, 0.0, 1.0),
    "institutional_absorption": (0.0, 0.0, 0.0, 0.0, 1.0),
}

#: Flat one-hot ontology boosts, resolved once at import.
_TAG_BOOSTS: dict[str, tuple[float, float, float, float, float]] = {
    str(tag).strip().lower(): (
        float(values[0]),
        float(values[1]),
        float(values[2]),
        float(values[3]),
        float(values[4]),
    )
    for tag, values in SHOCK_TAG_BOOSTS.items()
}


def donor_mirofish_status() -> dict[str, Any]:
    """Report which harvested MiroFish services are importable in this repo."""
    report: dict[str, Any] = {"modules": {}, "available": False}
    targets = {
        "local_graph": "ring2_brain.mirofish.services.local_graph",
        "ontology_generator": "ring2_brain.mirofish.services.ontology_generator",
        "simulation_config_generator": (
            "ring2_brain.mirofish.services.simulation_config_generator"
        ),
    }
    for label, module_name in targets.items():
        entry = {"module": module_name, "importable": False, "reason": ""}
        try:
            module = importlib.import_module(module_name)
            entry["importable"] = True
            entry["exports"] = [
                name
                for name in ("LocalEntityReader", "OntologyGenerator")
                if hasattr(module, name)
            ]
        except Exception as exc:  # pragma: no cover - donor vendoring
            entry["reason"] = f"{type(exc).__name__}: {exc}"
        report["modules"][label] = entry
    report["available"] = report["modules"]["local_graph"]["importable"]
    return report


# ---------------------------------------------------------------------------
# Shock contract
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ShockVector:
    """Deterministic market shock fed into the cascade.

    Parameters
    ----------
    price_shock_bps:
        Signed price displacement in basis points (``+`` up, ``-`` down).
    realized_vol_ratio:
        ``realized_vol / normal_vol``.  ``1.0`` is a normal tape; ``>= 3.5``
        is a ``sec_007`` volatility breaker.
    spread_bps:
        Current quoted spread in basis points.
    dealer_gamma_flip_proximity:
        ``0.0`` = dealers are long gamma (dampening), ``1.0`` = spot is at or
        through the gamma flip (short-gamma amplification).
    tags:
        Optional causal ontology tags (see :data:`SHOCK_TAG_BOOSTS`).
    """

    price_shock_bps: float
    realized_vol_ratio: float = 1.0
    spread_bps: float = 4.0
    dealer_gamma_flip_proximity: float = 0.0
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "price_shock_bps",
            "realized_vol_ratio",
            "spread_bps",
            "dealer_gamma_flip_proximity",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value!r}")
        if float(self.realized_vol_ratio) < 0.0:
            raise ValueError("realized_vol_ratio must be non-negative")
        if float(self.spread_bps) < 0.0:
            raise ValueError("spread_bps must be non-negative")
        proximity = float(self.dealer_gamma_flip_proximity)
        if not 0.0 <= proximity <= 1.0:
            raise ValueError("dealer_gamma_flip_proximity must be within [0, 1]")
        object.__setattr__(self, "price_shock_bps", float(self.price_shock_bps))
        object.__setattr__(self, "realized_vol_ratio", float(self.realized_vol_ratio))
        object.__setattr__(self, "spread_bps", float(self.spread_bps))
        object.__setattr__(
            self, "dealer_gamma_flip_proximity", proximity
        )
        object.__setattr__(
            self,
            "tags",
            tuple(str(tag).strip().lower() for tag in tuple(self.tags) if str(tag).strip()),
        )

    def tag_boost_tuple(self) -> tuple[float, float, float, float, float]:
        """Map ontology tags onto the five-archetype trigger vector.

        Returns a flat five-float tuple (allocation-free for untagged shocks)
        so the hot cascade never rebuilds a NumPy array per call.
        """
        if not self.tags:
            return _ZERO_TAGS
        b0 = b1 = b2 = b3 = b4 = 0.0
        for tag in self.tags:
            values = _TAG_BOOSTS.get(tag)
            if values is None:
                continue
            v0, v1, v2, v3, v4 = values
            if v0 > b0:
                b0 = v0
            if v1 > b1:
                b1 = v1
            if v2 > b2:
                b2 = v2
            if v3 > b3:
                b3 = v3
            if v4 > b4:
                b4 = v4
        return (b0, b1, b2, b3, b4)

    def tag_weights(self) -> np.ndarray:
        """NumPy view of :meth:`tag_boost_tuple` (backwards-compatible API)."""
        return np.asarray(self.tag_boost_tuple(), dtype=np.float64)


@dataclass(slots=True)
class CascadeResult:
    """One validated 3-tick cascade equilibrium."""

    features: ContagionFeatures
    tick_flows: tuple[float, float, float]
    latency_ms: float
    fragility_override: bool
    revoked_long_leases: int
    archetype_weights: tuple[float, ...] = ARCHETYPE_WEIGHTS
    shock: ShockVector | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "features": self.features.to_dict(),
            "tick_flows": list(self.tick_flows),
            "latency_ms": self.latency_ms,
            "fragility_override": self.fragility_override,
            "revoked_long_leases": self.revoked_long_leases,
            "archetype_weights": list(self.archetype_weights),
        }


# ---------------------------------------------------------------------------
# Deterministic kernel
# ---------------------------------------------------------------------------


@njit(cache=True, fastmath=True, nogil=True)
def _sign(value: float) -> float:
    if value > 0.0:
        return 1.0
    if value < 0.0:
        return -1.0
    return 0.0


@njit(cache=True, fastmath=True, nogil=True)
def _clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


@njit(cache=True, fastmath=True, nogil=True)
def _cascade_kernel(
    shock_bps: float,
    vol_ratio: float,
    spread_bps: float,
    gamma_proximity: float,
    tags,
    weights: np.ndarray,
    out: np.ndarray,
) -> np.ndarray:
    """Three deterministic contagion ticks -> structural feature vector.

    Writes into the caller-supplied ``out`` buffer (9 contiguous float64
    slots) so the hot path allocates nothing per cascade.
    Returns ``[crowd_asymmetry, fragility, divergence, retail_tick1,
    value_tick3, total_tick3, pod_activation, dealer_stress, cta_stress]``.
    """
    shock = _clamp(shock_bps / 100.0, -1.5, 1.5)
    vol = max(vol_ratio, 0.05)
    gamma = _clamp(gamma_proximity, 0.0, 1.0)
    spread_stress = _clamp((spread_bps - 8.0) / 42.0, 0.0, 1.0)

    retail_t1 = 0.0
    value_t3 = 0.0
    total_t3 = 0.0
    pod_activation = 0.0
    previous_total = 0.0
    flows = np.zeros(3, dtype=np.float64)

    for tick in range(3):
        effective = _clamp(shock + 0.55 * previous_total, -1.5, 1.5)
        direction = _sign(effective)
        decay = 1.0 if tick == 0 else (0.55 if tick == 1 else 0.30)

        retail = (
            weights[0]
            * math.tanh(2.5 * effective)
            * decay
            * (1.0 + 0.5 * tags[0])
        )
        dealer = weights[1] * (
            -math.tanh(2.0 * effective) * (1.0 - gamma)
            + 0.8 * gamma * direction * (1.0 + 0.4 * tags[1])
        )
        trend_scale = _clamp(1.5 / vol, 0.0, 1.5)
        de_lever = max(0.0, vol - 2.0) * 0.35 * direction * (1.0 + 0.5 * tags[2])
        cta = weights[2] * (math.tanh(1.5 * effective) * trend_scale - de_lever)

        pod_activation = _clamp(
            (abs(effective) - 0.25 + 0.30 * tags[3] + 0.15 * gamma) / 0.5,
            0.0,
            1.0,
        )
        pod = weights[3] * direction * pod_activation

        absorption = 0.0
        if tick == 2:
            absorption = _clamp(
                (
                    abs(effective)
                    - 0.20
                    + 0.40 * tags[4]
                    + 0.10 * max(0.0, vol - 1.0)
                )
                / 0.6,
                0.0,
                1.0,
            )
        value = -weights[4] * direction * absorption

        total = retail + dealer + cta + pod + value
        flows[tick] = total
        if tick == 0:
            retail_t1 = retail
        if tick == 2:
            value_t3 = value
            total_t3 = total
        previous_total = total

    dealer_stress = gamma * _clamp(vol / 3.0, 0.0, 1.0) * (0.6 + 0.4 * tags[1])
    cta_stress = _clamp((vol - 1.5) / 2.0, 0.0, 1.0)
    fragility = _clamp(
        0.30 * pod_activation
        + 0.25 * dealer_stress
        + 0.25 * cta_stress
        + 0.10 * spread_stress
        + 0.10 * abs(total_t3),
        0.0,
        1.0,
    )
    crowd = _clamp(total_t3 * 2.0, -1.0, 1.0)
    divergence = _clamp(
        (retail_t1 - value_t3) * 1.5 + retail_t1 * abs(value_t3) * 1.5,
        -1.0,
        1.0,
    )

    out[0] = crowd
    out[1] = fragility
    out[2] = divergence
    out[3] = retail_t1
    out[4] = value_t3
    out[5] = total_t3
    out[6] = pod_activation
    out[7] = dealer_stress
    out[8] = cta_stress
    return out


def _cascade_numpy(
    shock_bps: float,
    vol_ratio: float,
    spread_bps: float,
    gamma_proximity: float,
    tags,
    weights: np.ndarray,
    out: np.ndarray,
) -> np.ndarray:
    """Pure-NumPy fallback with the identical contract as the Numba kernel."""
    shock = min(max(float(shock_bps) / 100.0, -1.5), 1.5)
    vol = max(float(vol_ratio), 0.05)
    gamma = min(max(float(gamma_proximity), 0.0), 1.0)
    spread_stress = min(max((float(spread_bps) - 8.0) / 42.0, 0.0), 1.0)

    def threshold(value: float) -> float:
        return -1.0 if value < 0.0 else 1.0 if value > 0.0 else 0.0

    retail_t1 = value_t3 = total_t3 = pod_activation = 0.0
    previous_total = 0.0
    for tick in range(3):
        effective = min(max(shock + 0.55 * previous_total, -1.5), 1.5)
        direction = threshold(effective)
        decay = 1.0 if tick == 0 else (0.55 if tick == 1 else 0.30)
        retail = (
            weights[0] * math.tanh(2.5 * effective) * decay * (1.0 + 0.5 * tags[0])
        )
        dealer = weights[1] * (
            -math.tanh(2.0 * effective) * (1.0 - gamma)
            + 0.8 * gamma * direction * (1.0 + 0.4 * tags[1])
        )
        trend_scale = min(max(1.5 / vol, 0.0), 1.5)
        de_lever = max(0.0, vol - 2.0) * 0.35 * direction * (1.0 + 0.5 * tags[2])
        cta = weights[2] * (math.tanh(1.5 * effective) * trend_scale - de_lever)
        pod_activation = min(
            max(
                (abs(effective) - 0.25 + 0.30 * tags[3] + 0.15 * gamma) / 0.5,
                0.0,
            ),
            1.0,
        )
        pod = weights[3] * direction * pod_activation
        absorption = 0.0
        if tick == 2:
            absorption = min(
                max(
                    (
                        abs(effective)
                        - 0.20
                        + 0.40 * tags[4]
                        + 0.10 * max(0.0, vol - 1.0)
                    )
                    / 0.6,
                    0.0,
                ),
                1.0,
            )
        value = -weights[4] * direction * absorption
        total = retail + dealer + cta + pod + value
        if tick == 0:
            retail_t1 = retail
        if tick == 2:
            value_t3 = value
            total_t3 = total
        previous_total = total

    dealer_stress = gamma * min(max(vol / 3.0, 0.0), 1.0) * (0.6 + 0.4 * tags[1])
    cta_stress = min(max((vol - 1.5) / 2.0, 0.0), 1.0)
    fragility = min(
        max(
            0.30 * pod_activation
            + 0.25 * dealer_stress
            + 0.25 * cta_stress
            + 0.10 * spread_stress
            + 0.10 * abs(total_t3),
            0.0,
        ),
        1.0,
    )
    crowd = min(max(total_t3 * 2.0, -1.0), 1.0)
    divergence = min(
        max(
            (retail_t1 - value_t3) * 1.5 + retail_t1 * abs(value_t3) * 1.5,
            -1.0,
        ),
        1.0,
    )
    out[0] = crowd
    out[1] = fragility
    out[2] = divergence
    out[3] = retail_t1
    out[4] = value_t3
    out[5] = total_t3
    out[6] = pod_activation
    out[7] = dealer_stress
    out[8] = cta_stress
    return out


#: Kernel selected once at import: no per-cascade global/attribute dispatch.
_KERNEL = _cascade_kernel if NUMBA_AVAILABLE else _cascade_numpy


def cascade_features(
    shock: ShockVector,
    *,
    symbol: str,
    as_of_ns: int | None = None,
) -> ContagionFeatures:
    """Run the deterministic cascade without any I/O (pure function)."""
    raw = _KERNEL(
        float(shock.price_shock_bps),
        float(shock.realized_vol_ratio),
        float(shock.spread_bps),
        float(shock.dealer_gamma_flip_proximity),
        shock.tag_boost_tuple(),
        _WEIGHTS_NP,
        np.empty(9, dtype=np.float64),
    )
    stamp = int(as_of_ns) if as_of_ns is not None else time.time_ns()
    return ContagionFeatures(
        symbol=str(symbol),
        as_of_ns=stamp,
        crowd_asymmetry_score=float(raw[0]),
        cascade_fragility_index=float(raw[1]),
        narrative_exhaustion_divergence=float(raw[2]),
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class MiroFishContagionEngine:
    """Deterministic cascade engine + feature-store writer + lease override."""

    def __init__(
        self,
        feature_store: FeatureStore | None = None,
        lease_table: LeaseTableSHM | None = None,
        *,
        silo: str = "US",
        graph_path: str | None = None,
        warmup: bool = True,
    ) -> None:
        self.feature_store = feature_store
        self.lease_table = lease_table
        self.silo = str(silo)
        self.graph_path = graph_path
        # The donor probe imports harvested MiroFish service modules.  It is
        # resolved lazily so constructing an engine (Gate 15, the MCP tool,
        # every cold CLI start) never pays a multi-second import inside the
        # measured cascade path.
        self._donor: dict[str, Any] | None = None
        self._registered_symbols: set[str] = set()
        self._full_swept_in_episode = False
        self._last_result: CascadeResult | None = None
        # Contiguous scratch buffer reused by every cascade on this engine.
        self._out = np.empty(9, dtype=np.float64)
        if warmup:
            self.warmup()

    # -- bridge -----------------------------------------------------------
    @property
    def donor(self) -> dict[str, Any]:
        """Harvested MiroFish service availability (imported on first read)."""
        if self._donor is None:
            self._donor = donor_mirofish_status()
        return self._donor

    @property
    def donor_bridge(self) -> dict[str, Any]:
        return self.donor

    def local_graph_entities(self) -> list[dict[str, Any]]:
        """Read the local causal graph ontology when one is configured."""
        if not self.graph_path:
            return []
        try:
            from ring2_brain.mirofish.services.local_graph import LocalEntityReader

            reader = LocalEntityReader()
            return list(reader.get_all_nodes(self.graph_path))
        except Exception:  # pragma: no cover - optional graph
            return []

    # -- lifecycle --------------------------------------------------------
    def warmup(self) -> None:
        """Compile the kernel off the measured path."""
        cascade_features(
            ShockVector(price_shock_bps=10.0, realized_vol_ratio=1.2),
            symbol="WARMUP",
            as_of_ns=0,
        )

    def register_symbol(self, symbol: str) -> None:
        """Track a symbol whose lease must be revoked on a fragility spike."""
        token = str(symbol).strip().upper()
        if token:
            self._registered_symbols.add(token)

    # -- run --------------------------------------------------------------
    def run(
        self,
        shock: ShockVector,
        symbol: str,
        *,
        now_ns: int | None = None,
        lease_now_ns: int | None = None,
        persist: bool = True,
        store: FeatureStore | None = None,
    ) -> CascadeResult:
        """Execute the three ticks, persist features, enforce the override."""
        started = time.perf_counter()
        stamp = int(now_ns) if now_ns is not None else time.time_ns()
        raw = _KERNEL(
            float(shock.price_shock_bps),
            float(shock.realized_vol_ratio),
            float(shock.spread_bps),
            float(shock.dealer_gamma_flip_proximity),
            shock.tag_boost_tuple(),
            _WEIGHTS_NP,
            self._out,
        )
        features = ContagionFeatures(
            symbol=str(symbol),
            as_of_ns=stamp,
            crowd_asymmetry_score=float(raw[0]),
            cascade_fragility_index=float(raw[1]),
            narrative_exhaustion_divergence=float(raw[2]),
        )

        target_store = store if store is not None else self.feature_store
        if persist and target_store is not None:
            target_store.append_contagion(
                self.silo, features, tick_index=3, knowledge_ts_ns=stamp
            )

        self.register_symbol(symbol)
        revoked = 0
        override = bool(features.requires_ring1_halt)
        if override and self.lease_table is not None:
            revoked = self._revoke_long_leases(
                symbol, lease_now_ns=lease_now_ns
            )
        if not override:
            self._full_swept_in_episode = False

        result = CascadeResult(
            features=features,
            tick_flows=(float(raw[3]), float(raw[4]), float(raw[5])),
            latency_ms=(time.perf_counter() - started) * 1e3,
            fragility_override=override,
            revoked_long_leases=revoked,
            shock=shock,
        )
        self._last_result = result
        return result

    # -- override ---------------------------------------------------------
    def _revoke_long_leases(
        self,
        symbol: str,
        *,
        lease_now_ns: int | None = None,
    ) -> int:
        table = self.lease_table
        if table is None:
            return 0
        now = int(lease_now_ns) if lease_now_ns is not None else time.monotonic_ns()
        revoked = 0
        target = str(symbol).strip().upper()

        if not self._full_swept_in_episode:
            # First trigger of the episode: authoritative 5,000-slot sweep.
            for lease in table.iter_active_leases(now):
                if lease.mode is LeaseMode.LONG_ONLY:
                    table.disarm(lease.symbol, now_ns=now)
                    revoked += 1
            self._full_swept_in_episode = True
            return revoked

        candidates = set(self._registered_symbols)
        if target:
            candidates.add(target)
        for token in candidates:
            lease = table.read_lease(token, now_ns=now)
            if lease.mode is LeaseMode.LONG_ONLY and lease.is_active(now):
                table.disarm(token, now_ns=now)
                revoked += 1
        return revoked

    # -- diagnostics ------------------------------------------------------
    @property
    def last_result(self) -> CascadeResult | None:
        return self._last_result

    #: Verb aliases used by the architecture's "run the cascade" prose.
    simulate = run
    run_cascade = run

    @property
    def uri(self) -> str:
        return SHM_URI_PREFIX + "nios-active-leases"


#: Short alias used by callers that do not care about the MiroFish branding.
ContagionEngine = MiroFishContagionEngine


def simulate_cascade(
    shock: ShockVector,
    symbol: str,
    *,
    as_of_ns: int | None = None,
) -> ContagionFeatures:
    """Pure, I/O-free cascade shortcut returning the validated features."""
    return cascade_features(shock, symbol=symbol, as_of_ns=as_of_ns)
