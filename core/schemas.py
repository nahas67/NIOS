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
from types import MappingProxyType
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
    # V1.9 mission / tranche / regulatory contracts
    "MissionAuthorization",
    "MissionAuthorizationError",
    "MissionInfeasibleError",
    "MissionSpec",
    "TrancheRiskState",
    "TrancheStatus",
    "TrancheHaltedError",
    "CorrelatedTrancheHeatError",
    "SplitPolicy",
    "RegulatoryRuleSet",
    "REGULATORY_RULES",
    "select_regulatory_rules",
    "MAX_KELLY_FRACTION_CAP",
    "MONEY_CEILING_MICROS",
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
    funding_bps: float = 0.0,
    locate_fee_bps: float = 0.0,
) -> float:
    """``Net_LB = E[r] - (Spread + 2 * Fee + Tax + Q_0.98(Residuals))``.

    ``sec_003`` Pillar 2: capital deploys only when this is strictly ``> 0``.

    ``V1.9`` adds two statutory/venue frictions that a pre-V1.9 caller never
    passed: perpetual ``funding_bps`` (``C3``) and the short-borrow
    ``locate_fee_bps`` (``D2``).  Both default to ``0.0``, so every historical
    call site keeps its exact arithmetic.
    """
    friction = (
        float(spread_bps)
        + 2.0 * float(fee_bps)
        + float(tax_bps)
        + float(funding_bps)
        + float(locate_fee_bps)
    )
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
    #: ``V1.9`` (``D2``): short-borrow economics.  A short thesis whose locate
    #: fee is not carried in the net lower bound is a hallucinated edge.
    locate_fee_bps: float = 0.0
    borrow_confirmed: bool = False
    #: ``V1.9`` (``F3``): freshness lower bound.  ``0`` means "no claim"; a
    #: non-zero value is the oldest acceptable ``knowledge_ts_ns`` and a packet
    #: older than it is stale, not merely old.
    not_stale_before_utc: int = 0
    #: ``V1.9`` (``F1``): provenance.  ``source_id`` must be on the allowlist and
    #: ``retrieval_attestation`` is the SHA-256 the retrieval adapter returned.
    source_id: str = ""
    retrieval_attestation: str = ""

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
        # A frozen dataclass whose ``metrics`` dict can still be mutated is not
        # immutable and not digest-stable: any holder could append a metric
        # after ``compute_digest`` and invalidate a sealed packet in place.
        object.__setattr__(self, "metrics", MappingProxyType(normalized))
        object.__setattr__(self, "valid_time_ns", valid_time_ns)
        object.__setattr__(self, "knowledge_ts_ns", knowledge_ts_ns)
        object.__setattr__(self, "as_of_ns", as_of_ns)

        locate_fee = float(self.locate_fee_bps)
        if not math.isfinite(locate_fee) or locate_fee < 0.0:
            raise ValueError("locate_fee_bps must be finite and non-negative")
        object.__setattr__(self, "locate_fee_bps", locate_fee)
        object.__setattr__(self, "borrow_confirmed", bool(self.borrow_confirmed))
        not_stale_before = int(self.not_stale_before_utc)
        if not_stale_before < 0:
            raise ValueError("not_stale_before_utc must be non-negative")
        object.__setattr__(self, "not_stale_before_utc", not_stale_before)
        object.__setattr__(self, "source_id", str(self.source_id or "").strip())
        attestation = str(self.retrieval_attestation or "").strip().lower()
        if attestation and (
            len(attestation) != 64 or any(c not in "0123456789abcdef" for c in attestation)
        ):
            raise ValueError("retrieval_attestation must be a 64-char SHA-256 hex digest")
        object.__setattr__(self, "retrieval_attestation", attestation)

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
        """Header fields plus every metric, as ``(key, value)`` pairs.

        ``V1.9`` provenance fields only enter the digest when a caller actually
        set them.  A pre-V1.9 packet therefore keeps the exact digest it had
        before the fields existed, which is what lets historical receipts stay
        verifiable.
        """
        pairs: list[tuple[str, Any]] = [
            ("symbol", self.symbol),
            ("valid_time_ns", self.valid_time_ns),
            ("knowledge_ts_ns", self.knowledge_ts_ns),
            ("as_of_ns", self.as_of_ns),
        ]
        if self.locate_fee_bps:
            pairs.append(("locate_fee_bps", self.locate_fee_bps))
        if self.borrow_confirmed:
            pairs.append(("borrow_confirmed", True))
        if self.not_stale_before_utc:
            pairs.append(("not_stale_before_utc", self.not_stale_before_utc))
        if self.source_id:
            pairs.append(("source_id", self.source_id))
        if self.retrieval_attestation:
            pairs.append(("retrieval_attestation", self.retrieval_attestation))
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

    # -- V1.9 provenance / freshness --------------------------------------
    @property
    def is_provenance_complete(self) -> bool:
        """``F1``: a named source *and* a retrieval attestation must exist."""
        return bool(self.source_id) and len(self.retrieval_attestation) == 64

    def is_fresh(self) -> bool:
        """``F3``: ``knowledge_ts_ns >= not_stale_before_utc`` (``0`` = no bound)."""
        if self.not_stale_before_utc <= 0:
            return True
        return int(self.knowledge_ts_ns) >= int(self.not_stale_before_utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "valid_time_ns": self.valid_time_ns,
            "knowledge_ts_ns": self.knowledge_ts_ns,
            "as_of_ns": self.as_of_ns,
            "metrics": dict(self.metrics),
            "sha256_digest": self.sha256_digest,
            "locate_fee_bps": self.locate_fee_bps,
            "borrow_confirmed": self.borrow_confirmed,
            "not_stale_before_utc": self.not_stale_before_utc,
            "source_id": self.source_id,
            "retrieval_attestation": self.retrieval_attestation,
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
    #: ``V1.9`` (``C2``): Binance auto-deleveraging rank in ``[0, 1]``.  A high
    #: quantile means the venue is close to forcing profitable positions flat,
    #: so the position budget shrinks before the venue does it for us.
    adl_rank_quantile: float = 0.0

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
        object.__setattr__(
            self,
            "adl_rank_quantile",
            _validate_unit_interval("adl_rank_quantile", self.adl_rank_quantile, 0.0, 1.0),
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
            "adl_rank_quantile": self.adl_rank_quantile,
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
    #: ``V1.9`` (``B1``): SEBI/NSE algo identification.  A live Indian order
    #: without a registered exchange algo id is refused by Ring 0; paper mode
    #: defaults to the generic tag so the paper tape stays byte-identical.
    exchange_algo_id: str = "NIOS_GENERIC_ALGO"
    algo_id_kind: str = "GENERIC"
    #: ``V1.9`` (``A1``/``A8``): optional tranche attribution.  An empty value
    #: means the proposal belongs to no mission tranche (legacy path).
    mission_id: str = ""
    tranche_id: str = ""
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

        algo_id = str(self.exchange_algo_id or "").strip()
        if not algo_id:
            raise ValueError("exchange_algo_id must be a non-empty string")
        object.__setattr__(self, "exchange_algo_id", algo_id)
        kind = str(self.algo_id_kind or "GENERIC").strip().upper()
        if kind not in ("GENERIC", "REGISTERED"):
            raise ValueError("algo_id_kind must be GENERIC or REGISTERED")
        object.__setattr__(self, "algo_id_kind", kind)
        object.__setattr__(self, "mission_id", str(self.mission_id or "").strip())
        object.__setattr__(self, "tranche_id", str(self.tranche_id or "").strip())

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
            "exchange_algo_id": self.exchange_algo_id,
            "algo_id_kind": self.algo_id_kind,
            "mission_id": self.mission_id,
            "tranche_id": self.tranche_id,
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


# ---------------------------------------------------------------------------
# V1.9 -- Multi-tranche mission, tranche isolation and effective-dated rules
# ---------------------------------------------------------------------------
#
# ``A1``-``A8``: a mission is a goal with a deadline and a risk budget, split
# into per-tranche risk units that fail independently.  ``D1``-``D5``: a
# regulatory rule is a *versioned* object, selected by UTC date, so the same
# order is judged by the rule that was in force on that date.


class MissionAuthorizationError(NiosError):
    """Raised when a mission approval does not cover the action it authorizes."""


class MissionInfeasibleError(NiosError):
    """``422 INFEASIBLE_MISSION`` -- the target cannot be reached before ruin."""

    code = "INFEASIBLE_MISSION"


class TrancheHaltedError(NiosError):
    """``TRANCHE_HALT`` -- one tranche breached its stop, the rest keep trading."""

    code = "TRANCHE_HALT"


class CorrelatedTrancheHeatError(NiosError):
    """``422 CORRELATED_TRANCHE_HEAT_EXCEEDED`` -- correlated tranches over 6%."""

    code = "CORRELATED_TRANCHE_HEAT_EXCEEDED"


MISSION_AUTHORIZATION_STATES: tuple[str, ...] = ("APPROVE", "OVERRIDE", "REVOKE")


def _require_digest(name: str, value: str, *, allow_empty: bool = False) -> str:
    digest = str(value or "").strip().lower()
    if not digest and allow_empty:
        return ""
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"{name} must be a 64-char SHA-256 hex digest")
    return digest


@dataclass(slots=True, frozen=True)
class MissionAuthorization:
    """A signed, scoped, time-boxed operator approval (``A3``).

    The three-state workflow is explicit: ``APPROVE`` authorizes the mission as
    specified, ``OVERRIDE`` authorizes a different notional cap (the operator
    accepted the feasibility warning), and ``REVOKE`` withdraws the approval and
    is always honored immediately.  The signature is a SHA-256 commitment over
    the scope fields, so a tampered cap or TTL no longer verifies.
    """

    operator: str
    silos: tuple[str, ...]
    notional_cap_micros: int
    ttl_seconds: int
    state: str = "APPROVE"
    issued_at_ns: int = field(default_factory=time.time_ns)
    signature: str = ""

    def __post_init__(self) -> None:
        operator = str(self.operator or "").strip()
        if not operator:
            raise ValueError("MissionAuthorization.operator must be non-empty")
        object.__setattr__(self, "operator", operator)

        silos = tuple(sorted({str(s).strip().upper() for s in self.silos if str(s).strip()}))
        if not silos:
            raise ValueError("MissionAuthorization.silos must name at least one silo")
        object.__setattr__(self, "silos", silos)

        cap = int(self.notional_cap_micros)
        if cap <= 0 or cap > MONEY_CEILING_MICROS:
            raise ValueError("notional_cap_micros must be a positive bounded integer")
        object.__setattr__(self, "notional_cap_micros", cap)

        ttl = int(self.ttl_seconds)
        if ttl < 60 or ttl > 7 * 24 * 3600:
            raise ValueError("ttl_seconds must be within [60, 604800]")
        object.__setattr__(self, "ttl_seconds", ttl)

        state = str(self.state or "APPROVE").strip().upper()
        if state not in MISSION_AUTHORIZATION_STATES:
            raise ValueError(
                f"state must be one of {MISSION_AUTHORIZATION_STATES}, got {state!r}"
            )
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "issued_at_ns", int(self.issued_at_ns))

        computed = self.compute_signature()
        supplied = str(self.signature or "").strip().lower()
        if supplied and supplied != computed:
            raise ValueError("MissionAuthorization signature does not match its scope")
        object.__setattr__(self, "signature", computed)

    def canonical_pairs(self) -> list[tuple[str, Any]]:
        return [
            ("operator", self.operator),
            ("silos", ",".join(self.silos)),
            ("notional_cap_micros", self.notional_cap_micros),
            ("ttl_seconds", self.ttl_seconds),
            ("state", self.state),
            ("issued_at_ns", self.issued_at_ns),
        ]

    def compute_signature(self) -> str:
        return canonical_pairs_digest(self.canonical_pairs())

    @property
    def expires_at_ns(self) -> int:
        return int(self.issued_at_ns) + int(self.ttl_seconds) * 1_000_000_000

    def is_live(self, now_ns: int | None = None) -> bool:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        return self.state != "REVOKE" and stamp < self.expires_at_ns

    def covers(self, silo: str) -> bool:
        return str(silo).strip().upper() in self.silos

    def authorizes(
        self,
        silo: str,
        notional_micros: int,
        *,
        now_ns: int | None = None,
    ) -> bool:
        return (
            self.is_live(now_ns)
            and self.covers(silo)
            and 0 < int(notional_micros) <= self.notional_cap_micros
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "silos": list(self.silos),
            "notional_cap_micros": self.notional_cap_micros,
            "ttl_seconds": self.ttl_seconds,
            "state": self.state,
            "issued_at_ns": self.issued_at_ns,
            "expires_at_ns": self.expires_at_ns,
            "signature": self.signature,
        }

    @classmethod
    def mint(
        cls,
        operator: str,
        silos: Iterable[str],
        notional_cap_micros: int,
        *,
        ttl_seconds: int = 3600,
        state: str = "APPROVE",
        now_ns: int | None = None,
    ) -> "MissionAuthorization":
        return cls(
            operator=operator,
            silos=tuple(silos),
            notional_cap_micros=int(notional_cap_micros),
            ttl_seconds=int(ttl_seconds),
            state=state,
            issued_at_ns=int(now_ns if now_ns is not None else time.time_ns()),
        )

    def revoked(self) -> "MissionAuthorization":
        return MissionAuthorization(
            operator=self.operator,
            silos=self.silos,
            notional_cap_micros=self.notional_cap_micros,
            ttl_seconds=self.ttl_seconds,
            state="REVOKE",
            issued_at_ns=self.issued_at_ns,
        )


#: ``A3``: bounded integer money ceiling for mission fields (u127, same as IBOR).
MONEY_CEILING_MICROS: int = (1 << 127) - 1

#: ``A3``: the only Kelly cap the constitution permits.
MAX_KELLY_FRACTION_CAP: float = 0.5


@dataclass(slots=True, frozen=True)
class MissionSpec:
    """A goal-driven capital mission (``A3`` / ``F4``).

    ``target_micros`` is the profit goal, ``deadline_utc`` the UTC nanosecond
    deadline, and ``risk_budget_micros`` the maximum the mission may lose.  The
    feasibility engine -- not the operator -- decides whether the goal is
    reachable; Ring 0 refuses an infeasible mission with
    ``422 INFEASIBLE_MISSION`` rather than letting hope size a position.
    """

    mission_id: str
    target_micros: int
    deadline_utc: int
    risk_budget_micros: int
    kelly_fraction_cap: float = MAX_KELLY_FRACTION_CAP
    strategy_lineage_digests: tuple[str, ...] = ()
    unlock_ladder: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
    approval: MissionAuthorization | None = None
    created_at_ns: int = field(default_factory=time.time_ns)
    # -- V1.9 (PART 3, ``plannotator`` harvest): inline plan annotation -----
    # An operator may revise the plan at the approval gate rather than kicking
    # it back.  The overrides are recorded verbatim on the spec so the revision
    # is auditable after the fact, and paired with ``revision`` /
    # ``revision_sha256`` so a replay can prove which plan was approved.
    tranche_caps_micros: Mapping[str, int] = field(default_factory=dict)
    excluded_symbols: tuple[str, ...] = ()
    reserve_split_override_bps: int | None = None
    operator_note: str = ""
    revision: int = 0
    revision_sha256: str = ""

    def __post_init__(self) -> None:
        mission_id = str(self.mission_id or "").strip()
        if not mission_id:
            raise ValueError("mission_id must be non-empty")
        if any(token in mission_id for token in ("/", "\\", "\x00", "..")):
            raise ValueError("mission_id must not contain path separators or '..'")
        object.__setattr__(self, "mission_id", mission_id)

        target = int(self.target_micros)
        if target <= 0 or target > MONEY_CEILING_MICROS:
            raise ValueError("target_micros must be a positive bounded integer")
        object.__setattr__(self, "target_micros", target)

        risk = int(self.risk_budget_micros)
        if risk <= 0 or risk > target:
            raise ValueError("risk_budget_micros must be positive and <= target_micros")
        object.__setattr__(self, "risk_budget_micros", risk)

        created = int(self.created_at_ns)
        deadline = int(self.deadline_utc)
        if deadline <= created:
            raise ValueError("deadline_utc must be strictly after created_at_ns")
        object.__setattr__(self, "created_at_ns", created)
        object.__setattr__(self, "deadline_utc", deadline)

        cap = float(self.kelly_fraction_cap)
        if not math.isfinite(cap) or cap <= 0.0 or cap > MAX_KELLY_FRACTION_CAP:
            raise ValueError(
                f"kelly_fraction_cap must be in (0, {MAX_KELLY_FRACTION_CAP}]"
            )
        object.__setattr__(self, "kelly_fraction_cap", cap)

        lineage = tuple(
            _require_digest("strategy_lineage_digest", digest)
            for digest in self.strategy_lineage_digests
        )
        object.__setattr__(self, "strategy_lineage_digests", lineage)

        ladder = tuple(float(step) for step in self.unlock_ladder)
        if not ladder:
            raise ValueError("unlock_ladder must contain at least one step")
        if any(not math.isfinite(step) or step < 0.0 or step > 1.0 for step in ladder):
            raise ValueError("unlock_ladder steps must lie in [0, 1]")
        if any(b <= a for a, b in zip(ladder, ladder[1:])):
            raise ValueError("unlock_ladder must be strictly ascending")
        object.__setattr__(self, "unlock_ladder", ladder)

        if self.approval is not None and not isinstance(
            self.approval, MissionAuthorization
        ):
            raise TypeError("approval must be a MissionAuthorization")

        # V1.9 (PART 3): the inline annotation gate.  Deterministic containers
        # only -- sorted, deduped, integer -- so the revision hash is stable.
        caps: dict[str, int] = {}
        for key, value in dict(self.tranche_caps_micros or {}).items():
            tranche_key = str(key or "").strip()
            if not tranche_key:
                raise ValueError("tranche_caps_micros keys must be non-empty")
            if any(token in tranche_key for token in ("/", "\\", "\x00", "..")):
                raise ValueError(
                    "tranche_caps_micros keys must not contain path separators or '..'"
                )
            amount = int(value)
            if amount <= 0 or amount > MONEY_CEILING_MICROS:
                raise ValueError(
                    "tranche_caps_micros values must be positive bounded integers"
                )
            caps[tranche_key] = amount
        object.__setattr__(
            self, "tranche_caps_micros", MappingProxyType(dict(sorted(caps.items())))
        )

        excluded: list[str] = []
        for raw in self.excluded_symbols or ():
            token = str(raw or "").strip().upper()
            if not token:
                continue
            if len(token) > 32:
                raise ValueError("excluded_symbols entries must be <= 32 chars")
            if any(mark in token for mark in ("/", "\\", "\x00", "..")):
                raise ValueError(
                    "excluded_symbols entries must not contain path separators or '..'"
                )
            if token not in excluded:
                excluded.append(token)
        object.__setattr__(self, "excluded_symbols", tuple(sorted(excluded)))

        if self.reserve_split_override_bps is not None:
            reserve_bps = int(self.reserve_split_override_bps)
            if reserve_bps < 4000 or reserve_bps > 9500:
                raise ValueError("reserve_split_override_bps must lie in [4000, 9500]")
            object.__setattr__(self, "reserve_split_override_bps", reserve_bps)

        note = str(self.operator_note or "")
        if len(note) > 512:
            raise ValueError("operator_note must be <= 512 chars")
        object.__setattr__(self, "operator_note", note)

        revision = int(self.revision)
        if revision < 0:
            raise ValueError("revision must be >= 0")
        object.__setattr__(self, "revision", revision)
        if self.revision_sha256:
            object.__setattr__(
                self,
                "revision_sha256",
                _require_digest("revision_sha256", self.revision_sha256),
            )

    # -- time / progress ---------------------------------------------------
    def days_remaining(self, now_ns: int | None = None) -> float:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        remaining_ns = max(0, int(self.deadline_utc) - stamp)
        return remaining_ns / 86_400_000_000_000.0

    def daily_velocity_target_micros(self, now_ns: int | None = None) -> int:
        """``target / days_remaining``, floored at one micro (``A3``)."""
        days = self.days_remaining(now_ns)
        if days <= 0.0:
            return int(self.target_micros)
        return max(1, int(self.target_micros / days))

    def progress(self, realized_locked_micros: int) -> float:
        return max(0.0, float(realized_locked_micros) / float(self.target_micros))

    def is_expired(self, now_ns: int | None = None) -> bool:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        return stamp >= int(self.deadline_utc)

    def unlock_index(self, progress: float) -> int:
        index = 0
        for position, step in enumerate(self.unlock_ladder):
            if progress + 1e-12 >= step:
                index = position
        return index

    def authorized(self, silo: str, notional_micros: int, *, now_ns: int | None = None) -> bool:
        if self.approval is None:
            return False
        return self.approval.authorizes(silo, notional_micros, now_ns=now_ns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "target_micros": self.target_micros,
            "deadline_utc": self.deadline_utc,
            "risk_budget_micros": self.risk_budget_micros,
            "kelly_fraction_cap": self.kelly_fraction_cap,
            "strategy_lineage_digests": list(self.strategy_lineage_digests),
            "unlock_ladder": list(self.unlock_ladder),
            "approval": self.approval.to_dict() if self.approval else None,
            "created_at_ns": self.created_at_ns,
            "tranche_caps_micros": dict(self.tranche_caps_micros),
            "excluded_symbols": list(self.excluded_symbols),
            "reserve_split_override_bps": self.reserve_split_override_bps,
            "operator_note": self.operator_note,
            "revision": self.revision,
            "revision_sha256": self.revision_sha256,
        }


class TrancheStatus(StrEnum):
    """Lifecycle of one mission tranche (``A1``/``A8``)."""

    ACTIVE = "ACTIVE"
    THROTTLED = "THROTTLED"
    HALTED = "HALTED"
    TARGET_LOCKED = "TARGET_LOCKED"
    CLOSED = "CLOSED"


@dataclass(slots=True)
class TrancheRiskState:
    """Per-tranche risk isolation record (``A1`` / ``A2`` / ``A8``).

    One tranche breaching its stop must never halt a sibling tranche or an
    unrelated silo, so every field needed to decide *that* -- allocation, stop,
    high-water mark, heat and lineage -- lives on the tranche itself.
    """

    tranche_id: str
    mission_id: str
    silo: str
    allocated_micros: int
    stop_micros: int
    trailing_hwm_micros: int = 0
    heat_micros: int = 0
    lineage_digest: str = ""
    status: TrancheStatus = TrancheStatus.ACTIVE
    side: str = "LONG"
    realized_micros: int = 0
    created_at_ns: int = field(default_factory=time.time_ns)

    def __post_init__(self) -> None:
        tranche_id = str(self.tranche_id or "").strip()
        if not tranche_id:
            raise ValueError("tranche_id must be non-empty")
        if any(token in tranche_id for token in ("/", "\\", "\x00", "..")):
            raise ValueError("tranche_id must not contain path separators or '..'")
        self.tranche_id = tranche_id
        self.mission_id = str(self.mission_id or "").strip()
        self.silo = str(self.silo or "").strip().upper()
        self.allocated_micros = int(self.allocated_micros)
        if self.allocated_micros <= 0:
            raise ValueError("allocated_micros must be positive")
        self.stop_micros = int(self.stop_micros)
        if self.stop_micros <= 0:
            raise ValueError("stop_micros must be positive")
        self.trailing_hwm_micros = int(self.trailing_hwm_micros)
        self.heat_micros = max(0, int(self.heat_micros))
        self.lineage_digest = _require_digest(
            "lineage_digest", self.lineage_digest, allow_empty=True
        )
        if not isinstance(self.status, TrancheStatus):
            self.status = TrancheStatus(str(self.status).strip().upper())
        side = str(self.side or "LONG").strip().upper()
        if side not in ("LONG", "SHORT"):
            raise ValueError("side must be LONG or SHORT")
        self.side = side
        self.realized_micros = int(self.realized_micros)
        self.created_at_ns = int(self.created_at_ns)

    # -- risk maths --------------------------------------------------------
    @property
    def equity_micros(self) -> int:
        """Realized P&L attributed to this tranche (never unrealized)."""
        return int(self.realized_micros)

    @property
    def giveback_micros(self) -> int:
        """How much of the high-water mark has been given back."""
        return max(0, int(self.trailing_hwm_micros) - int(self.realized_micros))

    @property
    def stop_breached(self) -> bool:
        return self.status is TrancheStatus.HALTED or (
            self.giveback_micros >= int(self.stop_micros)
        )

    def record_realized(self, delta_micros: int) -> "TrancheRiskState":
        """Book realized P&L and lift the high-water mark on new highs."""
        self.realized_micros = int(self.realized_micros) + int(delta_micros)
        if self.realized_micros > self.trailing_hwm_micros:
            self.trailing_hwm_micros = self.realized_micros
        return self

    def halt(self) -> "TrancheRiskState":
        self.status = TrancheStatus.HALTED
        return self

    def rearm(self) -> "TrancheRiskState":
        """Human re-arm: resets the giveback baseline, not the realized P&L."""
        if self.status is TrancheStatus.HALTED:
            self.status = TrancheStatus.ACTIVE
            self.trailing_hwm_micros = self.realized_micros
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "tranche_id": self.tranche_id,
            "mission_id": self.mission_id,
            "silo": self.silo,
            "allocated_micros": self.allocated_micros,
            "stop_micros": self.stop_micros,
            "trailing_hwm_micros": self.trailing_hwm_micros,
            "heat_micros": self.heat_micros,
            "lineage_digest": self.lineage_digest,
            "status": str(self.status),
            "side": self.side,
            "realized_micros": self.realized_micros,
            "giveback_micros": self.giveback_micros,
            "created_at_ns": self.created_at_ns,
        }


@dataclass(slots=True, frozen=True)
class SplitPolicy:
    """Inputs to :class:`DynamicFundSplitEngine` (``A5``).

    ``mission_progress`` is ``realized_locked / target``; ``regime`` is the
    Ring 2 regime label; ``unlock_ladder`` is the mission's ascending ladder.
    The policy is data, never a callback, so a split is reproducible.
    """

    mission_progress: float = 0.0
    regime: str = "NEUTRAL"
    unlock_ladder: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
    reserve_floor_pct: int = 15
    tax_escrow_pct: int = 20

    def __post_init__(self) -> None:
        progress = float(self.mission_progress)
        if not math.isfinite(progress):
            raise ValueError("mission_progress must be finite")
        object.__setattr__(self, "mission_progress", min(1.0, max(0.0, progress)))
        regime = str(self.regime or "NEUTRAL").strip().upper()
        if regime not in ("EXPANSION", "NEUTRAL", "CONTRACTION"):
            raise ValueError("regime must be EXPANSION, NEUTRAL or CONTRACTION")
        object.__setattr__(self, "regime", regime)
        ladder = tuple(float(step) for step in self.unlock_ladder)
        if not ladder or any(step < 0.0 or step > 1.0 for step in ladder):
            raise ValueError("unlock_ladder steps must lie in [0, 1]")
        object.__setattr__(self, "unlock_ladder", ladder)
        floor = int(self.reserve_floor_pct)
        tax = int(self.tax_escrow_pct)
        if not 0 <= floor <= 100 or not 0 <= tax <= 100 or floor + tax > 90:
            raise ValueError("reserve_floor_pct + tax_escrow_pct must be <= 90")
        object.__setattr__(self, "reserve_floor_pct", floor)
        object.__setattr__(self, "tax_escrow_pct", tax)

    @property
    def unlock_index(self) -> int:
        index = 0
        for position, step in enumerate(self.unlock_ladder):
            if self.mission_progress + 1e-12 >= step:
                index = position
        return index

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_progress": self.mission_progress,
            "regime": self.regime,
            "unlock_ladder": list(self.unlock_ladder),
            "reserve_floor_pct": self.reserve_floor_pct,
            "tax_escrow_pct": self.tax_escrow_pct,
            "unlock_index": self.unlock_index,
        }


@dataclass(slots=True, frozen=True)
class RegulatoryRuleSet:
    """An effective-dated regulatory regime for one silo (``D1``).

    ``effective_from_utc`` is inclusive and ``effective_to_utc`` exclusive, both
    in UTC nanoseconds.  ``0`` for ``effective_to_utc`` means "still in force".
    """

    rule_id: str
    silo: str
    effective_from_utc: int
    effective_to_utc: int
    maintenance_model: str
    pdt_equity_floor_micros: int
    intraday_margin_model: str
    ssr_enforced: bool = False

    def __post_init__(self) -> None:
        rule_id = str(self.rule_id or "").strip()
        if not rule_id:
            raise ValueError("rule_id must be non-empty")
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "silo", str(self.silo or "").strip().upper())
        start = int(self.effective_from_utc)
        end = int(self.effective_to_utc)
        if start <= 0:
            raise ValueError("effective_from_utc must be a positive UTC ns stamp")
        if end and end <= start:
            raise ValueError("effective_to_utc must be after effective_from_utc")
        object.__setattr__(self, "effective_from_utc", start)
        object.__setattr__(self, "effective_to_utc", end)
        floor = int(self.pdt_equity_floor_micros)
        if floor < 0:
            raise ValueError("pdt_equity_floor_micros must be non-negative")
        object.__setattr__(self, "pdt_equity_floor_micros", floor)
        object.__setattr__(self, "maintenance_model", str(self.maintenance_model))
        object.__setattr__(self, "intraday_margin_model", str(self.intraday_margin_model))
        object.__setattr__(self, "ssr_enforced", bool(self.ssr_enforced))

    def is_effective(self, as_of_utc_ns: int) -> bool:
        stamp = int(as_of_utc_ns)
        if stamp < self.effective_from_utc:
            return False
        return not self.effective_to_utc or stamp < self.effective_to_utc

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "silo": self.silo,
            "effective_from_utc": self.effective_from_utc,
            "effective_to_utc": self.effective_to_utc,
            "maintenance_model": self.maintenance_model,
            "pdt_equity_floor_micros": self.pdt_equity_floor_micros,
            "intraday_margin_model": self.intraday_margin_model,
            "ssr_enforced": self.ssr_enforced,
        }


def _utc_ns(year: int, month: int, day: int) -> int:
    import calendar as _calendar

    return _calendar.timegm((year, month, day, 0, 0, 0, 0, 0, 0)) * 1_000_000_000


#: ``D1``: the versioned rule book.  FINRA Rule 4210's intraday margin level
#: replaces the legacy ``$25,000`` PDT minimum on ``2026-06-04``; the legacy row
#: is kept (not deleted) so a historical backtest is judged by the rule that
#: actually applied on that date.
REGULATORY_RULES: tuple[RegulatoryRuleSet, ...] = (
    RegulatoryRuleSet(
        rule_id="US.PDT_25K.2020",
        silo="US_IBKR",
        effective_from_utc=_utc_ns(2020, 1, 1),
        effective_to_utc=_utc_ns(2026, 6, 4),
        maintenance_model="REG_T",
        pdt_equity_floor_micros=25_000_000_000,
        intraday_margin_model="PDT_25K",
        ssr_enforced=True,
    ),
    RegulatoryRuleSet(
        rule_id="US.FINRA_4210_INTRADAY.2026",
        silo="US_IBKR",
        effective_from_utc=_utc_ns(2026, 6, 4),
        effective_to_utc=0,
        maintenance_model="FINRA_4210",
        pdt_equity_floor_micros=2_000_000_000,
        intraday_margin_model="FINRA_4210_INTRADAY_MARGIN",
        ssr_enforced=True,
    ),
    RegulatoryRuleSet(
        rule_id="FX.RETAIL_LEVERAGE.2020",
        silo="FX_MT5",
        effective_from_utc=_utc_ns(2020, 1, 1),
        effective_to_utc=0,
        maintenance_model="RETAIL_FX",
        pdt_equity_floor_micros=0,
        intraday_margin_model="FX_50_TO_1",
        ssr_enforced=False,
    ),
    RegulatoryRuleSet(
        rule_id="IN.SEBI_ALGO.2024",
        silo="INDIA_ZERODHA_DHAN",
        effective_from_utc=_utc_ns(2024, 4, 1),
        effective_to_utc=0,
        maintenance_model="SEBI_MARGIN",
        pdt_equity_floor_micros=0,
        intraday_margin_model="SEBI_VAR_PLUS_ELM",
        ssr_enforced=False,
    ),
    RegulatoryRuleSet(
        rule_id="CRYPTO.VENUE_RISK.2020",
        silo="CRYPTO_BINANCE",
        effective_from_utc=_utc_ns(2020, 1, 1),
        effective_to_utc=0,
        maintenance_model="TIERED_MAINTENANCE_MARGIN",
        pdt_equity_floor_micros=0,
        intraday_margin_model="ISOLATED_CROSS",
        ssr_enforced=False,
    ),
)


def select_regulatory_rules(silo: str, as_of_utc_ns: int) -> RegulatoryRuleSet:
    """Return the rule in force for ``silo`` at ``as_of_utc_ns`` (``D1``).

    Selection is by UTC date only -- never by "latest" -- so replaying a
    historical order cannot accidentally apply today's margin model.
    """
    key = str(silo or "").strip().upper()
    matches = [
        rule
        for rule in REGULATORY_RULES
        if rule.silo == key and rule.is_effective(as_of_utc_ns)
    ]
    if not matches:
        raise KeyError(f"no regulatory rule set is effective for {key} at {as_of_utc_ns}")
    # The most recently effective rule wins when windows overlap.
    matches.sort(key=lambda rule: rule.effective_from_utc)
    return matches[-1]
