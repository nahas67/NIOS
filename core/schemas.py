"""NIOS Core v3.0 -- canonical wire and state contracts (STEP 2).

This module is the single source of truth for every structure that crosses a
ring boundary.  It is intentionally dependency-free (stdlib only) so that
Ring 0, Ring 1, Ring 2 and Ring 3 can all import it without pulling broker
adapters, LLM clients or any I/O into the hot path.

Governing architecture sections
-------------------------------
* ``sec_002`` -- fatal flaws (spoofed liquidity, lookahead, hallucinated math)
* ``sec_003`` -- PIT cryptographic truth, 98% split-conformal gate
* ``sec_004`` -- 4-ring IPC topology, 60s System 1 lease, kill latch
* ``sec_006`` -- dual-vendor quarantine, anti-spoof OFI, grounding ledger
* ``sec_007`` -- Tier 0/1/2 execution contract, lease TTL
* ``sec_008`` -- MiroFish contagion features
* ``sec_009`` -- Tier 0-3 state contracts, 4 vaults
* ``sec_010`` -- SLA matrix (0.14ms p99, fragility >= 0.75 halt)
* ``sec_011`` -- fixed-schema unsigned proposal, notional caps
* ``sec_016`` -- plain-English proof telemetry line
* ``sec_018`` -- the five constitutional laws

Engineering rules honoured here
-------------------------------
* No floating point money in the IBOR (this module only carries bps floats
  and *unsigned* proposals -- never ledger amounts).
* No ungrounded number may leave Ring 2 without a SHA-256 commitment.
* No lookahead: ``knowledge_ts_ns <= as_of_ns`` is enforced at construction.
"""

from __future__ import annotations

import hashlib
import math
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, ClassVar, Iterable, Mapping, Sequence

try:  # Python >= 3.11
    from enum import StrEnum
except ImportError:  # pragma: no cover - Python 3.10 fallback
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Minimal backport of :class:`enum.StrEnum`."""

        def __str__(self) -> str:
            return str(self.value)


__all__ = [
    # errors
    "NiosError",
    "TemporalIntegrityError",
    "GroundingError",
    "GroundingDigestError",
    "UngroundedNumericalClaim",
    # enums
    "LeaseMode",
    "VaultName",
    # contracts
    "System1Lease",
    "DualVendorBar",
    "ExecutedFlowTick",
    "GroundedFactPacket",
    "ContagionFeatures",
    "UnsignedProposal",
    "PlainEnglishCockpitCard",
    # helpers / constants
    "LEASE_TTL_NS",
    "SYSTEM1_LEASE_SIZE",
    "SYSTEM1_LEASE_STRUCT",
    "TAKER_BUY_RATIO_MIN",
    "TAKER_SELL_RATIO_MAX",
    "CANCEL_TO_FILL_MAX",
    "VOL_SPIKE_BREAKER",
    "FRAGILITY_HALT_THRESHOLD",
    "VENDOR_DIVERGENCE_GATE_PCT",
    "FEED_LAG_GATE_MS",
    "SPLIT_GUARD_PCT",
    "VAULT_NOTIONAL_CAP_PCT",
    "canonical_pairs_digest",
    "net_lower_bound_bps",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NiosError(Exception):
    """Base class for every NIOS contract violation."""


class TemporalIntegrityError(NiosError):
    """Raised when a record would leak future knowledge (lookahead).

    ``sec_003`` / ``sec_006``: every fact must satisfy
    ``knowledge_ts_ns <= as_of_ns``.
    """

    def __init__(
        self,
        message: str,
        *,
        knowledge_ts_ns: int | None = None,
        valid_time_ns: int | None = None,
        as_of_ns: int | None = None,
    ) -> None:
        super().__init__(message)
        self.knowledge_ts_ns = knowledge_ts_ns
        self.valid_time_ns = valid_time_ns
        self.as_of_ns = as_of_ns


class GroundingError(NiosError):
    """Base class for SHA-256 grounding-ledger failures."""


class GroundingDigestError(GroundingError):
    """Raised when a supplied digest does not match the canonical payload."""


class UngroundedNumericalClaim(GroundingError):
    """``UNGROUNDED-NUMERICAL-CLAIM`` -- a figure absent from the ledger.

    ``sec_006`` stage 4: any fabricated number aborts order signing.
    """


# ---------------------------------------------------------------------------
# Constants (thresholds are architectural, not tunable at runtime)
# ---------------------------------------------------------------------------

LEASE_TTL_NS: int = 60 * 1_000_000_000
"""System 1 dead-man TTL -- 60 seconds (``sec_004`` / ``sec_007``)."""

SYMBOL_BYTES: int = 16
"""Fixed width of the symbol field inside the 64-byte lease slot."""

#: ``<symbol:16s> <mode:u8> <pad:7x> <target_bps:f64> <stop_bps:f64>
#: <expires_at_ns:u64> <strategy_id_hash:u64> <pad:8x>`` == exactly 64 bytes.
SYSTEM1_LEASE_STRUCT: struct.Struct = struct.Struct("<16sB7xddQQ8x")
SYSTEM1_LEASE_SIZE: int = SYSTEM1_LEASE_STRUCT.size

assert SYSTEM1_LEASE_SIZE == 64, "System1Lease must be exactly one cache line"

TAKER_BUY_RATIO_MIN: float = 0.65
TAKER_SELL_RATIO_MAX: float = 0.35
CANCEL_TO_FILL_MAX: float = 3.0
VOL_SPIKE_BREAKER: float = 3.5
FRAGILITY_HALT_THRESHOLD: float = 0.75

VENDOR_DIVERGENCE_GATE_PCT: float = 1.50
FEED_LAG_GATE_MS: float = 5_000.0
SPLIT_GUARD_PCT: float = 45.0

#: ``sec_011`` notional caps, expressed as a fraction of the owning vault.
VAULT_NOTIONAL_CAP_PCT: Mapping["VaultName", float] = {}  # populated below


# ---------------------------------------------------------------------------
# Canonical hashing helpers
# ---------------------------------------------------------------------------

_CANON_KV_SEP = "\x1f"
_CANON_ROW_SEP = "\x1e"


def _canon_value(value: Any) -> str:
    """Render a scalar canonically so ints/floats hash identically."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return format(value, ".17g")
    return str(value)


def canonical_pairs_digest(pairs: Iterable[tuple[str, Any]]) -> str:
    """Deterministic SHA-256 over canonically sorted ``(key, value)`` pairs.

    Sorting makes the digest independent of insertion order; the explicit
    value rendering makes it independent of Python's ``repr`` quirks and of
    whether a caller passed ``5`` or ``5.0``.
    """
    rows = sorted((str(k), _canon_value(v)) for k, v in pairs)
    payload = _CANON_ROW_SEP.join(f"{k}{_CANON_KV_SEP}{v}" for k, v in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def net_lower_bound_bps(
    expected_gross_bps: float,
    spread_bps: float,
    fee_bps: float,
    tax_bps: float,
    q98_residual_bps: float,
) -> float:
    """``Net_LB = E[r] - (Spread + 2 * Fee + Tax + Q_0.98(Residuals))``.

    ``sec_003`` Pillar 2: capital deploys only when this is strictly ``> 0``.
    """
    friction = float(spread_bps) + 2.0 * float(fee_bps) + float(tax_bps)
    return float(expected_gross_bps) - friction - float(q98_residual_bps)


# ---------------------------------------------------------------------------
# Lease contract
# ---------------------------------------------------------------------------


class LeaseMode(IntEnum):
    """System 1 tactical mode written into shared memory (``sec_007``)."""

    DISARMED = 0
    LONG_ONLY = 1
    SHORT_ONLY = 2
    MEAN_REV = 3


LEASE_MODE_LABELS: Mapping[LeaseMode, str] = {
    LeaseMode.DISARMED: "DISARMED",
    LeaseMode.LONG_ONLY: "LONG-ONLY",
    LeaseMode.SHORT_ONLY: "SHORT-ONLY",
    LeaseMode.MEAN_REV: "MEAN-REV",
}


@dataclass(slots=True)
class System1Lease:
    """64-byte, binary-packable System 1 execution lease.

    Written by Ring 2 (System 1 LLM, off-path, every 30s) and read by Ring 1
    with a single-writer atomic SeqLock (``sec_004`` / ``sec_007``).

    The lease is a *permission*, never an order.  Ring 1 still has to pass the
    spinal breaker, the anti-spoof OFI gate and (via Ring 0) the 98%
    split-conformal lower bound before anything is signed.
    """

    symbol: str
    mode: LeaseMode = LeaseMode.DISARMED
    target_bps: float = 0.0
    stop_bps: float = 0.0
    expires_at_ns: int = 0
    strategy_id_hash: int = 0

    #: Fixed on-the-wire size.  Exactly one 64-byte cache line.
    SIZE: ClassVar[int] = SYSTEM1_LEASE_SIZE

    def __post_init__(self) -> None:
        if type(self.mode) is not LeaseMode:
            self.mode = LeaseMode(int(self.mode))
        self.target_bps = float(self.target_bps)
        self.stop_bps = float(self.stop_bps)
        self.expires_at_ns = int(self.expires_at_ns)
        self.strategy_id_hash = int(self.strategy_id_hash)

    # -- activation -------------------------------------------------------
    def is_active(self, now_ns: int) -> bool:
        """``True`` only inside the 60-second dead-man window.

        Three conditions must hold simultaneously:

        1. the mode is not :attr:`LeaseMode.DISARMED`;
        2. the lease has not expired (``now_ns < expires_at_ns``);
        3. the remaining lifetime does not exceed :data:`LEASE_TTL_NS`
           (a lease claiming a longer horizon is stale or forged, and is
           rejected rather than trusted).
        """
        if self.mode is LeaseMode.DISARMED:
            return False
        remaining = self.expires_at_ns - int(now_ns)
        return 0 < remaining <= LEASE_TTL_NS

    def remaining_ns(self, now_ns: int) -> int:
        """Clamped nanoseconds until expiry (``0`` when already dead)."""
        return max(0, self.expires_at_ns - int(now_ns))

    def with_ttl(self, now_ns: int, ttl_ns: int = LEASE_TTL_NS) -> "System1Lease":
        """Return a copy whose expiry is ``now_ns + ttl_ns`` (clamped)."""
        ttl = min(int(ttl_ns), LEASE_TTL_NS)
        return System1Lease(
            symbol=self.symbol,
            mode=self.mode,
            target_bps=self.target_bps,
            stop_bps=self.stop_bps,
            expires_at_ns=int(now_ns) + ttl,
            strategy_id_hash=self.strategy_id_hash,
        )

    @classmethod
    def disarmed(cls, symbol: str = "") -> "System1Lease":
        """Neutral lease returned whenever the kill latch is hot."""
        return cls(symbol=symbol, mode=LeaseMode.DISARMED)

    # -- binary codec -----------------------------------------------------
    def pack(self) -> bytes:
        """Pack to exactly :data:`SYSTEM1_LEASE_SIZE` bytes."""
        raw = self.symbol.encode("utf-8")
        if len(raw) > SYMBOL_BYTES:
            raise ValueError(
                f"symbol {self.symbol!r} exceeds {SYMBOL_BYTES} UTF-8 bytes"
            )
        return SYSTEM1_LEASE_STRUCT.pack(
            raw,
            int(self.mode),
            self.target_bps,
            self.stop_bps,
            self.expires_at_ns & 0xFFFFFFFFFFFFFFFF,
            self.strategy_id_hash & 0xFFFFFFFFFFFFFFFF,
        )

    def pack_into(self, buffer: Any, offset: int = 0) -> None:
        """Pack directly into a writable shared-memory buffer."""
        raw = self.symbol.encode("utf-8")
        if len(raw) > SYMBOL_BYTES:
            raise ValueError(
                f"symbol {self.symbol!r} exceeds {SYMBOL_BYTES} UTF-8 bytes"
            )
        SYSTEM1_LEASE_STRUCT.pack_into(
            buffer,
            offset,
            raw,
            int(self.mode),
            self.target_bps,
            self.stop_bps,
            self.expires_at_ns & 0xFFFFFFFFFFFFFFFF,
            self.strategy_id_hash & 0xFFFFFFFFFFFFFFFF,
        )

    @classmethod
    def unpack(cls, data: bytes | bytearray | memoryview, offset: int = 0) -> "System1Lease":
        """Inverse of :meth:`pack`."""
        raw, mode, target, stop, expires, strategy_hash = SYSTEM1_LEASE_STRUCT.unpack_from(
            data, offset
        )
        return cls(
            symbol=raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
            mode=LeaseMode(mode),
            target_bps=target,
            stop_bps=stop,
            expires_at_ns=expires,
            strategy_id_hash=strategy_hash,
        )

    # ``read_lease`` in shm_ipc uses this alias for clarity.
    unpack_from = unpack


# ---------------------------------------------------------------------------
# Market data contracts (Stage 1-2, sec_006)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class DualVendorBar:
    """A price observed independently by two vendors.

    Quarantine rules (``sec_006`` Stage 2) are exposed as properties so the
    ingestion router never has to re-implement the arithmetic:

    * drop the vendor when ``feed_lag_ms > 5,000``;
    * drop the tick when ``|P1 - P2| / P > 1.5%``;
    * block a "phantom crash" when a single feed gaps ``>= 45%``.
    """

    symbol: str
    p1: float
    p2: float
    feed_lag_ms: float
    valid_time_ns: int
    knowledge_ts_ns: int

    @property
    def price(self) -> float:
        """Volume-agnostic mid used for divergence maths."""
        if self.p1 <= 0.0 and self.p2 <= 0.0:
            return 0.0
        if self.p1 <= 0.0:
            return float(self.p2)
        if self.p2 <= 0.0:
            return float(self.p1)
        return 0.5 * (float(self.p1) + float(self.p2))

    @property
    def vendor_divergence_pct(self) -> float:
        """``|P1 - P2| / P * 100`` (``0.0`` when mid is degenerate)."""
        mid = self.price
        if mid <= 0.0:
            return 0.0
        return abs(float(self.p1) - float(self.p2)) / mid * 100.0

    @property
    def split_gap_pct(self) -> float:
        """Largest single-feed gap versus the surviving mid, in percent."""
        base = max(float(self.p1), float(self.p2), 0.0)
        if base <= 0.0:
            return 0.0
        return abs(float(self.p1) - float(self.p2)) / base * 100.0

    @property
    def passes_lag_gate(self) -> bool:
        return float(self.feed_lag_ms) <= FEED_LAG_GATE_MS

    @property
    def passes_divergence_gate(self) -> bool:
        return self.vendor_divergence_pct <= VENDOR_DIVERGENCE_GATE_PCT

    @property
    def passes_split_guard(self) -> bool:
        return self.split_gap_pct < SPLIT_GUARD_PCT

    @property
    def passes_quarantine(self) -> bool:
        """All three Stage-2 gates simultaneously."""
        return (
            self.passes_lag_gate
            and self.passes_divergence_gate
            and self.passes_split_guard
        )

    def is_pit_clean(self, as_of_ns: int) -> bool:
        """``knowledge_ts_ns <= as_of_ns`` (no lookahead)."""
        return int(self.knowledge_ts_ns) <= int(as_of_ns)


# ---------------------------------------------------------------------------
# Executed orderflow (Stage 1 fast path, sec_006 / sec_007 / sec_018)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExecutedFlowTick:
    """5-second rolling *executed* orderflow frame.

    NIOS never feeds spoofable resting L2 depth to the scalper.  Only volume
    that actually crossed the spread and paid a fee counts as proof of
    liquidity (Law 1).
    """

    symbol: str
    valid_time_ns: int
    knowledge_ts_ns: int
    last_price: float
    spread_bps: float
    realized_vol_5s: float
    normal_vol_5s: float
    taker_buy_volume_5s: float
    taker_sell_volume_5s: float
    bid_cancel_to_fill_ratio: float
    ask_cancel_to_fill_ratio: float

    # -- derived physics --------------------------------------------------
    @property
    def taker_buy_ratio_5s(self) -> float:
        """Aggressive buy share of executed volume in ``[0, 1]``.

        With zero executed flow there is no proof of liquidity, so the ratio
        is ``0.0`` and every directional gate fails closed.
        """
        total = float(self.taker_buy_volume_5s) + float(self.taker_sell_volume_5s)
        if total <= 0.0:
            return 0.0
        return float(self.taker_buy_volume_5s) / total

    @property
    def taker_sell_ratio_5s(self) -> float:
        total = float(self.taker_buy_volume_5s) + float(self.taker_sell_volume_5s)
        if total <= 0.0:
            return 0.0
        return float(self.taker_sell_volume_5s) / total

    @property
    def vol_spike_ratio(self) -> float:
        """``realized_vol_5s / normal_vol_5s``; ``inf`` on a degenerate base."""
        base = float(self.normal_vol_5s)
        realized = float(self.realized_vol_5s)
        if base <= 0.0:
            return 0.0 if realized <= 0.0 else math.inf
        return realized / base

    @property
    def vol_breaker_tripped(self) -> bool:
        """``sec_007`` Step 2: ``Vol_5s / NormVol_5s >= 3.5x``."""
        return self.vol_spike_ratio >= VOL_SPIKE_BREAKER

    @property
    def max_cancel_to_fill_ratio(self) -> float:
        return max(
            float(self.bid_cancel_to_fill_ratio),
            float(self.ask_cancel_to_fill_ratio),
        )

    @property
    def book_is_clean(self) -> bool:
        """``CancelToFill < 3.0`` on both sides (phantom pulls excluded)."""
        return self.max_cancel_to_fill_ratio < CANCEL_TO_FILL_MAX

    def passes_anti_spoof_ofi(self, side: str = "BUY") -> bool:
        """Executed-taker OFI + cancel-to-fill gate (``sec_002`` / ``sec_018``).

        BUY  requires ``taker_buy_ratio_5s >= 0.65``.
        SELL requires ``taker_buy_ratio_5s <= 0.35`` (equivalently, aggressive
        sell share >= 0.65).
        Both directions additionally require bid *and* ask cancel-to-fill
        ratios strictly below ``3.0``.
        """
        if not self.book_is_clean:
            return False
        token = str(side).strip().upper()
        ratio = self.taker_buy_ratio_5s
        if token in ("BUY", "B", "LONG"):
            return ratio >= TAKER_BUY_RATIO_MIN
        if token in ("SELL", "S", "SHORT"):
            return ratio <= TAKER_SELL_RATIO_MAX
        raise ValueError(f"unknown side {side!r}; expected BUY or SELL")

    def spread_within(self, cap_bps: float) -> bool:
        return float(self.spread_bps) <= float(cap_bps)

    def is_pit_clean(self, as_of_ns: int) -> bool:
        return int(self.knowledge_ts_ns) <= int(as_of_ns)


# ---------------------------------------------------------------------------
# Grounding ledger (Stage 4, sec_003 / sec_006)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class GroundedFactPacket:
    """An immutable, SHA-256 committed bundle of numeric facts.

    Construction is where point-in-time integrity is enforced: a packet whose
    ``knowledge_ts_ns`` or ``valid_time_ns`` is later than ``as_of_ns`` would
    let a walk-forward learner read tomorrow's filing today, so it raises
    :class:`TemporalIntegrityError` immediately (``sec_006`` bullet 2).
    """

    symbol: str
    valid_time_ns: int
    knowledge_ts_ns: int
    as_of_ns: int
    metrics: Mapping[str, float]
    sha256_digest: str = ""

    def __post_init__(self) -> None:
        valid_time_ns = int(self.valid_time_ns)
        knowledge_ts_ns = int(self.knowledge_ts_ns)
        as_of_ns = int(self.as_of_ns)

        if knowledge_ts_ns > as_of_ns:
            raise TemporalIntegrityError(
                "knowledge_ts_ns "
                f"({knowledge_ts_ns}) > as_of_ns ({as_of_ns}): "
                f"{self.symbol} would leak future knowledge",
                knowledge_ts_ns=knowledge_ts_ns,
                valid_time_ns=valid_time_ns,
                as_of_ns=as_of_ns,
            )
        if valid_time_ns > as_of_ns:
            raise TemporalIntegrityError(
                "valid_time_ns "
                f"({valid_time_ns}) > as_of_ns ({as_of_ns}): "
                f"{self.symbol} references a bar from the future",
                knowledge_ts_ns=knowledge_ts_ns,
                valid_time_ns=valid_time_ns,
                as_of_ns=as_of_ns,
            )

        normalized = {
            str(key): float(value) for key, value in dict(self.metrics).items()
        }
        object.__setattr__(self, "metrics", normalized)
        object.__setattr__(self, "valid_time_ns", valid_time_ns)
        object.__setattr__(self, "knowledge_ts_ns", knowledge_ts_ns)
        object.__setattr__(self, "as_of_ns", as_of_ns)

        computed = self.compute_digest()
        supplied = str(self.sha256_digest or "").strip().lower()
        if supplied and supplied != computed:
            raise GroundingDigestError(
                f"digest mismatch for {self.symbol}: "
                f"supplied={supplied} computed={computed}"
            )
        object.__setattr__(self, "sha256_digest", computed)

    # -- hashing ----------------------------------------------------------
    def canonical_pairs(self) -> list[tuple[str, Any]]:
        """Header fields plus every metric, as ``(key, value)`` pairs."""
        pairs: list[tuple[str, Any]] = [
            ("symbol", self.symbol),
            ("valid_time_ns", self.valid_time_ns),
            ("knowledge_ts_ns", self.knowledge_ts_ns),
            ("as_of_ns", self.as_of_ns),
        ]
        pairs.extend(self.metrics.items())
        return pairs

    def compute_digest(self) -> str:
        return canonical_pairs_digest(self.canonical_pairs())

    def verify(self) -> bool:
        return self.compute_digest() == self.sha256_digest

    # -- accessors --------------------------------------------------------
    def metric(self, key: str, default: float | None = None) -> float | None:
        return self.metrics.get(key, default)

    def require(self, key: str) -> float:
        """Fetch a metric or raise ``UNGROUNDED-NUMERICAL-CLAIM``."""
        try:
            return self.metrics[key]
        except KeyError as exc:  # pragma: no cover - exercised by Ring 2
            raise UngroundedNumericalClaim(
                f"UNGROUNDED-NUMERICAL-CLAIM: {key!r} is not in the "
                f"grounding ledger for {self.symbol}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "valid_time_ns": self.valid_time_ns,
            "knowledge_ts_ns": self.knowledge_ts_ns,
            "as_of_ns": self.as_of_ns,
            "metrics": dict(self.metrics),
            "sha256_digest": self.sha256_digest,
        }


# ---------------------------------------------------------------------------
# MiroFish contagion features (sec_008 / sec_010)
# ---------------------------------------------------------------------------


def _validate_unit_interval(name: str, value: float, low: float, high: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if not (low <= number <= high):
        raise ValueError(f"{name}={number} outside [{low}, {high}]")
    return number


@dataclass(slots=True, frozen=True)
class ContagionFeatures:
    """Tick-3 equilibrium of the five-archetype MiroFish cascade.

    ``sec_008``: capital weights are Retail 0DTE 15%, Dealer gamma 25%,
    Vol-target CTAs 25%, Pod shops + value 35%.
    """

    symbol: str
    as_of_ns: int
    crowd_asymmetry_score: float
    cascade_fragility_index: float
    narrative_exhaustion_divergence: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "crowd_asymmetry_score",
            _validate_unit_interval(
                "crowd_asymmetry_score", self.crowd_asymmetry_score, -1.0, 1.0
            ),
        )
        object.__setattr__(
            self,
            "cascade_fragility_index",
            _validate_unit_interval(
                "cascade_fragility_index", self.cascade_fragility_index, 0.0, 1.0
            ),
        )
        object.__setattr__(
            self,
            "narrative_exhaustion_divergence",
            _validate_unit_interval(
                "narrative_exhaustion_divergence",
                self.narrative_exhaustion_divergence,
                -1.0,
                1.0,
            ),
        )
        object.__setattr__(self, "as_of_ns", int(self.as_of_ns))

    @property
    def requires_ring1_halt(self) -> bool:
        """``sec_008`` / ``sec_010``: fragility ``>= 0.75`` revokes all leases."""
        return self.cascade_fragility_index >= FRAGILITY_HALT_THRESHOLD

    @property
    def is_crowd_long(self) -> bool:
        return self.crowd_asymmetry_score >= 0.5

    @property
    def is_crowd_short(self) -> bool:
        return self.crowd_asymmetry_score <= -0.5

    def to_dict(self) -> dict[str, float | str | int]:
        return {
            "symbol": self.symbol,
            "as_of_ns": self.as_of_ns,
            "crowd_asymmetry_score": self.crowd_asymmetry_score,
            "cascade_fragility_index": self.cascade_fragility_index,
            "narrative_exhaustion_divergence": self.narrative_exhaustion_divergence,
            "requires_ring1_halt": self.requires_ring1_halt,
        }


# ---------------------------------------------------------------------------
# Vaults (sec_009)
# ---------------------------------------------------------------------------


class VaultName(StrEnum):
    """The four vaults that make up every broker silo (``sec_009``)."""

    RESERVE_YIELD = "ReserveYield"
    LONG_TERM_COMPOUNDER = "LongTermCompounder"
    TACTICAL_SWING = "TacticalSwing"
    SPINAL_SCALPER = "SpinalScalper"


VAULT_NOTIONAL_CAP_PCT = {  # type: ignore[assignment]
    VaultName.RESERVE_YIELD: 1.00,
    VaultName.LONG_TERM_COMPOUNDER: 0.12,
    VaultName.TACTICAL_SWING: 0.12,
    VaultName.SPINAL_SCALPER: 0.05,
}

#: ``sec_010`` in-silo ratchet: 35% compounder + 15% reserve.
VAULT_RATCHET_TARGETS: Mapping[VaultName, float] = {
    VaultName.LONG_TERM_COMPOUNDER: 0.35,
    VaultName.RESERVE_YIELD: 0.15,
}

#: ``sec_009`` public name -- kept as a tuple for iteration/auditing.
ALL_VAULTS: tuple[VaultName, ...] = tuple(VaultName)


# ---------------------------------------------------------------------------
# Ring 1/2 -> Ring 0 intent contract (sec_004 / sec_011)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UnsignedProposal:
    """Fixed-schema, *unsigned* trade intent.

    Emitted by Ring 1 (scalp) or Ring 2 (thesis).  It carries no credential,
    no signature and no broker session -- Ring 0 re-verifies the grounding
    digest, the symbol whitelist, the notional cap, the 98% conformal bound
    and the copula shield before anything can be signed (``sec_011``).
    """

    proposal_id: str
    strategy_id: str
    silo: str
    vault: VaultName
    symbol: str
    side: str
    qty: float
    limit_price: float
    expected_gross_bps: float
    spread_bps: float
    grounding_hash: str
    is_reduce_only: bool = False
    created_at_ns: int = field(default_factory=time.time_ns)

    def __post_init__(self) -> None:
        side = str(self.side).strip().upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {self.side!r}")
        object.__setattr__(self, "side", side)

        vault = self.vault
        if not isinstance(vault, VaultName):
            object.__setattr__(self, "vault", VaultName(str(vault)))

        qty = float(self.qty)
        if not math.isfinite(qty) or qty <= 0.0:
            raise ValueError(f"qty must be a positive finite number, got {self.qty!r}")
        object.__setattr__(self, "qty", qty)

        limit_price = float(self.limit_price)
        if not math.isfinite(limit_price) or limit_price <= 0.0:
            raise ValueError(
                f"limit_price must be positive and finite, got {self.limit_price!r}"
            )
        object.__setattr__(self, "limit_price", limit_price)

        gross = float(self.expected_gross_bps)
        if not math.isfinite(gross):
            raise ValueError("expected_gross_bps must be finite")
        object.__setattr__(self, "expected_gross_bps", gross)

        spread = float(self.spread_bps)
        if not math.isfinite(spread) or spread < 0.0:
            raise ValueError("spread_bps must be finite and non-negative")
        object.__setattr__(self, "spread_bps", spread)

        digest = str(self.grounding_hash or "").strip().lower()
        if digest:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("grounding_hash must be a 64-char SHA-256 hex digest")
        object.__setattr__(self, "grounding_hash", digest)
        object.__setattr__(self, "is_reduce_only", bool(self.is_reduce_only))
        object.__setattr__(self, "created_at_ns", int(self.created_at_ns))

    @property
    def notional(self) -> float:
        return self.qty * self.limit_price

    @property
    def notional_cap_pct(self) -> float:
        return VAULT_NOTIONAL_CAP_PCT[self.vault]

    @property
    def is_grounded(self) -> bool:
        return len(self.grounding_hash) == 64

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "strategy_id": self.strategy_id,
            "silo": self.silo,
            "vault": self.vault.value,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "limit_price": self.limit_price,
            "expected_gross_bps": self.expected_gross_bps,
            "spread_bps": self.spread_bps,
            "grounding_hash": self.grounding_hash,
            "is_reduce_only": self.is_reduce_only,
            "created_at_ns": self.created_at_ns,
        }


# ---------------------------------------------------------------------------
# Ring 3 cockpit telemetry (sec_016)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PlainEnglishCockpitCard:
    """Tab 1 SSE card: one sentence of story plus the deterministic proof.

    ``sec_016`` fixes the proof line verbatim, e.g.::

        [LIVE CAPITAL \\u2022 ZERODHA-INDIA \\u2022 RELIANCE.NS] Spinal Reflex
        bought RELIANCE: Aggressive buyers took 71% of 5s executed volume with
        clean book. | Gross: +18.0 bps | Fee+STT: 5.5 bps | Uncert (Q98): 6.2 bps
        | Net LB: +6.3 bps.

    ``Story_sentence`` carries the human sentence (including the verb and the
    instrument); the three numbers are rendered with fixed precision so the
    line is stable across locales and can be diffed byte-for-byte in tests.
    """

    silo: str
    symbol: str
    mode_badge: str
    story_sentence: str
    gross_bps: float
    fee_stt_bps: float
    uncert_q98_bps: float
    net_lb_bps: float
    timestamp_ns: int

    SEPARATOR: ClassVar[str] = " \u2022 "
    PROOF_TEMPLATE: ClassVar[str] = (
        "[{mode_badge}{sep}{silo}{sep}{symbol}] {story}"
        " | Gross: {gross:+.1f} bps"
        " | Fee+STT: {fee:.1f} bps"
        " | Uncert (Q98): {q98:.1f} bps"
        " | Net LB: {net:+.1f} bps."
    )

    def format_proof_line(self) -> str:
        """Render the exact ``sec_016`` proof line."""
        return self.PROOF_TEMPLATE.format(
            mode_badge=str(self.mode_badge),
            sep=self.SEPARATOR,
            silo=str(self.silo),
            symbol=str(self.symbol),
            story=str(self.story_sentence),
            gross=float(self.gross_bps),
            fee=float(self.fee_stt_bps),
            q98=float(self.uncert_q98_bps),
            net=float(self.net_lb_bps),
        )

    @property
    def is_net_edge_positive(self) -> bool:
        """``Net_LB > 0 bps`` -- the only state in which capital may deploy."""
        return self.net_lb_bps > 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "symbol": self.symbol,
            "mode_badge": self.mode_badge,
            "story_sentence": self.story_sentence,
            "gross_bps": self.gross_bps,
            "fee_stt_bps": self.fee_stt_bps,
            "uncert_q98_bps": self.uncert_q98_bps,
            "net_lb_bps": self.net_lb_bps,
            "timestamp_ns": self.timestamp_ns,
            "proof_line": self.format_proof_line(),
        }
