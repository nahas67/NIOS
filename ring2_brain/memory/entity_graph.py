"""Entity-linked post-mortem memory with hybrid retrieval (``gbrain`` harvest).

Two layers, per ``sec_009`` tier-2 append-only discipline and ``sec_018`` Law 3
("simulate contagion, hash every number"):

``post_mortem_timeline``
    An append-only list of immutable outcome events. Every event names every
    entity it implicates -- ``symbol:``, ``silo:``, ``strategy:``, ``macro:`` and
    ``regime:`` -- so one broker rejection is findable from five directions
    instead of one. Each event carries its own SHA-256 commitment over its
    canonical payload.

``compiled_entity_truths``
    A deterministic, cached rolling truth per entity: sample count, win rate in
    basis points, average slippage, the deepest peak-to-trough fall of the
    entity's cumulative net-P&L curve, the failure modes it has already
    exhibited and the regimes that have hurt it. Recompilation is a pure
    function of the timeline, so ``truth_sha256`` is stable across a restart.

Retrieval is hybrid: entity-feature overlap (symbol outranks silo, which
outranks strategy, then regime, then macro) fused with a BM25 lexical pass over
the per-entity lesson corpus. Every numeric that survives retrieval is sealed
into a ``GroundedFactPacket`` so an agent cannot cite a post-mortem figure that
is absent from the grounding ledger.

Ring containment: this module lives in Ring 2. It imports ``core.schemas`` and
the standard library only. It holds no broker credential, opens no socket and
signs nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.schemas import GroundedFactPacket

__all__ = [
    "ADVERSE_OUTCOME_KINDS",
    "COMPACT_THRESHOLD",
    "EMBED_DIMS",
    "ENTITY_KINDS",
    "FEATURE_WEIGHT",
    "KEEP_RECENT",
    "LEXICAL_WEIGHT",
    "MAX_COLD_ARCHIVE_EVENTS",
    "MEMORY_CACHE_MAX_ENTITIES",
    "OUTCOME_KINDS",
    "SEMANTIC_WEIGHT",
    "CompiledEntityTruth",
    "EntityMemoryGraph",
    "MemoryCheckpoint",
    "PostMortemEvent",
    "PostMortemPenalty",
    "cosine_similarity",
    "hashed_embedding",
]

#: Entity namespaces an outcome event may reference.
ENTITY_KINDS: tuple[str, ...] = ("symbol", "silo", "strategy", "macro", "regime")

ENTITY_TYPE_BY_KIND: Mapping[str, str] = {
    "symbol": "SYMBOL",
    "silo": "SILO",
    "strategy": "STRATEGY",
    "macro": "MACRO",
    "regime": "REGIME",
}

#: Closed vocabulary of outcome kinds (``sec_002`` flaw tracking).
OUTCOME_KINDS: tuple[str, ...] = (
    "LOSS",
    "SLIPPAGE_SPIKE",
    "BREAKER_TRIP",
    "ORDER_REJECTED",
    "WIN",
)

ADVERSE_OUTCOME_KINDS: frozenset[str] = frozenset(
    {"LOSS", "SLIPPAGE_SPIKE", "BREAKER_TRIP", "ORDER_REJECTED"}
)

FAILURE_MODE_BY_OUTCOME: Mapping[str, str] = {
    "LOSS": "DIRECTIONAL_LOSS",
    "SLIPPAGE_SPIKE": "SLIPPAGE_SPIKE",
    "BREAKER_TRIP": "BREAKER_TRIP",
    "ORDER_REJECTED": "ORDER_REJECTED",
}

#: A fill worse than this is a failure mode in its own right.
HIGH_SLIPPAGE_BPS = 25.0
HIGH_SLIPPAGE_MODE = "HIGH_SLIPPAGE"

MAX_FAILURE_MODES = 8
MAX_CAUTION_REGIMES = 4
MAX_TIMELINE_EVENTS = 4096
MAX_TOP_K = 25
MAX_LESSON_CHARS = 512
MONEY_CEILING_MICROS = 10**18

#: V2.3 (PART 2.2) -- tier-2 compaction.  The active raw timeline is compacted
#: automatically once it grows past ``COMPACT_THRESHOLD`` events; the tail is
#: kept hot (``KEEP_RECENT``) so a live query never has to touch the archive.
COMPACT_THRESHOLD = 500
KEEP_RECENT = 200
MAX_COLD_ARCHIVE_EVENTS = 20_000
#: How much of the cold archive rides the atomic journal state.  The durable
#: record is the append-only JSONL file (``read_archive_file``); this is the
#: crash-recovery tail so an in-process restart still sees what it just sealed.
STATE_ARCHIVE_TAIL = 512
CHECKPOINT_SCHEMA_VERSION = "v2.3.memory-checkpoint"

ENTITY_SCHEMA_VERSION = "v1.9.entity-memory"

_FORBIDDEN_TOKENS = ("/", "\\", "\x00", "..")

#: Retrieval fusion (V2.3 PART 2.3).  Three signals, summing to one:
#: ``0.25`` BM25 lexical + ``0.45`` canonical entity-feature + ``0.30``
#: deterministic 384-dim hashed n-gram cosine.  Feature overlap still dominates
#: because "this is the symbol I am about to trade" is a stronger signal than a
#: word appearing in a lesson, but a morphological or regime-near lesson now
#: outranks a coincidental token hit instead of tying with it.
KIND_WEIGHTS: Mapping[str, float] = {
    "symbol": 0.35,
    "silo": 0.20,
    "strategy": 0.20,
    "regime": 0.15,
    "macro": 0.10,
}
FEATURE_WEIGHT = 0.45
LEXICAL_WEIGHT = 0.25
SEMANTIC_WEIGHT = 0.30

#: Deterministic local embedding width (no model download, no network).
EMBED_DIMS = 384
EMBED_NGRAM = 3

#: Optional pluggable embedder.  Dormant unless the operator opts in; a single
#: bounded probe decides, and a failure disables the hook for the process so a
#: dead daemon can never become a retry storm.
OLLAMA_EMBED_ENV = "NIOS_OLLAMA_EMBED_MODEL"
OLLAMA_BASE_URL_ENV = "NIOS_OLLAMA_BASE_URL"
OLLAMA_DEFAULT_BASE_URL = "http://127.0.0.1:11434"
OLLAMA_TIMEOUT_S = 2.0

BM25_K1 = 1.5
BM25_B = 0.75

#: V2.4 PART 2 -- bounded per-entity retrieval cache.  ``query_entity_memory``
#: used to re-tokenize the whole corpus, recompute document frequency and
#: re-materialise a 384-dim vector on every call.  A long-running process may
#: serve hundreds of entities, so the cache is an LRU with a hard ceiling: no
#: query pattern can grow this graph's heap without bound.
#:
#: The ceiling is sized to the live universe -- the 274-instrument registry plus
#: its silo / strategy / regime / macro arms -- so a full-universe scan is a
#: steady stream of hits instead of a thrash that would re-embed most of the
#: corpus on every call.
MEMORY_CACHE_MAX_ENTITIES = 512

#: Deterministic caution penalties. A matching caution regime costs 10 points of
#: confidence, each carried failure mode costs 5, and the combined haircut is
#: capped at 40 points so a memory warning can never veto a trade on its own.
REGIME_PENALTY = 0.10
FAILURE_MODE_PENALTY = 0.05
MAX_COMBINED_PENALTY = 0.40

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# canonical helpers
# ---------------------------------------------------------------------------


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _clean(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    for token in _FORBIDDEN_TOKENS:
        if token in text:
            raise ValueError(f"{field_name} contains a forbidden path token")
    return text


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").lower())


def _entity_id(kind: str, value: str) -> str:
    if kind not in ENTITY_KINDS:
        raise ValueError(f"unknown entity kind {kind!r}")
    return f"{kind}:{_clean(value, field_name=kind)}"


def _split_entity(entity_id: str) -> tuple[str, str]:
    kind, _, value = entity_id.partition(":")
    return kind, value


#: Characters a venue may use to separate a base from a quote.
_SYMBOL_SEPARATORS: tuple[str, ...] = ("/", "\\", "-", "_", " ", ".", ":", "\x00")


def canonical_symbol_key(value: Any) -> str:
    """Fold every venue spelling of one instrument into a single comparable key.

    ``BTC/USDT``, ``BTC-USDT``, ``btc_usdt`` and ``BTCUSDT`` all denote the same
    instrument, but they are four different strings.  A post-mortem written under
    one spelling was therefore invisible to a consumer asking with another -- and
    because "no memory" reads as "no penalty", the sizing gate failed *open* on a
    symbol it had already been burned by.

    The canonical key drops every separator and uppercases, so the four spellings
    above all become ``BTCUSDT``.  It is a *key*, never a display ticker: the
    lesson sentence and the entity payload keep the operator's original spelling.
    """
    token = str(value or "").strip().upper()
    if token.startswith("SYMBOL:"):
        token = token[len("SYMBOL:") :]
    for separator in _SYMBOL_SEPARATORS:
        token = token.replace(separator, "")
    return token


#: V2.4 PART 2: both folds are pure functions of a short string and are called
#: once per truth per query (and once per event per recompilation), so the
#: memo is a strict win.  Bounded so an adversarial ticker storm cannot grow it.
@lru_cache(maxsize=4096)
def _canonical_symbol_key_cached(token: str) -> str:
    for separator in _SYMBOL_SEPARATORS:
        token = token.replace(separator, "")
    return token


def canonical_entity_alias(entity_id: Any) -> str:
    """Return the alias a ``symbol:`` entity id is looked up under.

    Non-symbol arms (``silo:``, ``strategy:``, ``macro:``, ``regime:``) are
    returned unchanged: a silo id is not an instrument and must never be folded.
    """
    return _canonical_entity_alias_cached(str(entity_id or "").strip())


@lru_cache(maxsize=4096)
def _canonical_entity_alias_cached(token: str) -> str:
    kind, _, value = token.partition(":")
    if kind.strip().lower() != "symbol":
        return token
    return f"symbol:{_canonical_symbol_key_cached(value.strip().upper())}"


def _fold_symbol(value: Any) -> str:
    """Fold a venue ticker into the folder-safe arm this graph stores.

    The entity graph forbids path tokens (``BTC/USDT`` would read as a directory
    traversal), so ``BASE/QUOTE`` becomes ``BASE-QUOTE`` -- the exact same stored
    shape :class:`PostMortemRecorder` writes.  Lookup then goes through
    :func:`canonical_entity_alias`, so the stored spelling never has to match the
    caller's spelling.
    """
    token = str(value or "").strip().upper()
    for separator in _SYMBOL_SEPARATORS:
        token = token.replace(separator, "-")
    return token


# ---------------------------------------------------------------------------
# layer 1 -- append-only post-mortem timeline
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PostMortemEvent:
    """One immutable outcome, committed to the timeline forever.

    The event is the *observation*; the compiled truth is the *inference*. A
    single event never becomes a truth on its own -- it is one sample in a
    rolling window.
    """

    event_id: str
    ts_utc: int
    entity_ids: tuple[str, ...]
    outcome_kind: str
    net_pnl_micros: int
    slippage_bps: float = 0.0
    lesson_summary: str = ""
    payload_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _clean(self.event_id, field_name="event_id"))

        stamp = int(self.ts_utc)
        if stamp < 0:
            raise ValueError("ts_utc must be non-negative")
        object.__setattr__(self, "ts_utc", stamp)

        ids = sorted({_clean(row, field_name="entity_id") for row in self.entity_ids})
        if not ids:
            raise ValueError("entity_ids must reference at least one entity")
        for row in ids:
            kind, value = _split_entity(row)
            if kind not in ENTITY_KINDS or not value:
                raise ValueError(
                    f"entity_id {row!r} must be '<kind>:<value>' with kind in {ENTITY_KINDS}"
                )
        object.__setattr__(self, "entity_ids", tuple(ids))

        kind = str(self.outcome_kind or "").strip().upper()
        if kind not in OUTCOME_KINDS:
            raise ValueError(f"outcome_kind must be one of {OUTCOME_KINDS}")
        object.__setattr__(self, "outcome_kind", kind)

        pnl = int(self.net_pnl_micros)
        if abs(pnl) > MONEY_CEILING_MICROS:
            raise ValueError("net_pnl_micros is outside the money ceiling")
        object.__setattr__(self, "net_pnl_micros", pnl)

        slip = float(self.slippage_bps)
        if not math.isfinite(slip) or abs(slip) > 1e6:
            raise ValueError("slippage_bps must be finite and bounded")
        object.__setattr__(self, "slippage_bps", slip)

        object.__setattr__(self, "lesson_summary", str(self.lesson_summary or "")[:MAX_LESSON_CHARS])

        digest = _sha256(self._canonical_payload())
        supplied = str(self.payload_sha256 or "").strip()
        if supplied and supplied != digest:
            raise ValueError("payload_sha256 does not match the event payload")
        object.__setattr__(self, "payload_sha256", digest)

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "ts_utc": self.ts_utc,
            "entity_ids": list(self.entity_ids),
            "outcome_kind": self.outcome_kind,
            "net_pnl_micros": self.net_pnl_micros,
            "slippage_bps": round(self.slippage_bps, 6),
            "lesson_summary": self.lesson_summary,
        }

    def verify(self) -> bool:
        return self.payload_sha256 == _sha256(self._canonical_payload())

    def to_dict(self) -> dict[str, Any]:
        payload = self._canonical_payload()
        payload["payload_sha256"] = self.payload_sha256
        return payload

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "PostMortemEvent":
        if not isinstance(row, Mapping):
            raise TypeError("event row must be a mapping")
        return cls(
            event_id=str(row.get("event_id") or ""),
            ts_utc=int(row.get("ts_utc") or 0),
            entity_ids=tuple(str(item) for item in (row.get("entity_ids") or ())),
            outcome_kind=str(row.get("outcome_kind") or ""),
            net_pnl_micros=int(row.get("net_pnl_micros") or 0),
            slippage_bps=float(row.get("slippage_bps") or 0.0),
            lesson_summary=str(row.get("lesson_summary") or ""),
            payload_sha256=str(row.get("payload_sha256") or ""),
        )


# ---------------------------------------------------------------------------
# layer 2 -- compiled entity truths
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CompiledEntityTruth:
    """Cached rolling truth for one entity, recomputable from the timeline.

    ``win_rate_bps`` is a rate carried in basis points (``wins / samples`` x
    10_000) so it stays an integer-friendly number in the same unit family as
    every other NIOS cost and slippage figure.

    ``worst_drawdown_bps`` is the deepest peak-to-trough fall of the entity's
    cumulative net-P&L curve, expressed in basis points of that curve's peak
    absolute value. An entity that only ever lost money reports 10_000 bps: it
    gave back the whole reference.
    """

    entity_id: str
    entity_type: str
    sample_count: int
    win_rate_bps: float
    avg_slippage_bps: float
    worst_drawdown_bps: float
    known_failure_modes: tuple[str, ...] = ()
    caution_regimes: tuple[str, ...] = ()
    last_compiled_utc: int = 0
    truth_sha256: str = ""

    def __post_init__(self) -> None:
        entity_id = _clean(self.entity_id, field_name="entity_id")
        kind, value = _split_entity(entity_id)
        if kind not in ENTITY_KINDS or not value:
            raise ValueError(f"entity_id {entity_id!r} must be '<kind>:<value>'")
        object.__setattr__(self, "entity_id", entity_id)
        object.__setattr__(self, "entity_type", ENTITY_TYPE_BY_KIND.get(kind, "UNKNOWN"))

        samples = int(self.sample_count)
        if samples < 0:
            raise ValueError("sample_count must be non-negative")
        object.__setattr__(self, "sample_count", samples)

        win_rate = float(self.win_rate_bps)
        if not math.isfinite(win_rate) or not 0.0 <= win_rate <= 10_000.0:
            raise ValueError("win_rate_bps must be finite and inside [0, 10000]")
        object.__setattr__(self, "win_rate_bps", win_rate)

        for name in ("avg_slippage_bps", "worst_drawdown_bps"):
            raw = float(getattr(self, name))
            if not math.isfinite(raw):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, raw)

        modes = tuple(sorted({str(row).strip().upper() for row in self.known_failure_modes if str(row).strip()}))
        object.__setattr__(self, "known_failure_modes", modes[:MAX_FAILURE_MODES])

        regimes = tuple(sorted({str(row).strip().upper() for row in self.caution_regimes if str(row).strip()}))
        object.__setattr__(self, "caution_regimes", regimes[:MAX_CAUTION_REGIMES])

        object.__setattr__(self, "last_compiled_utc", int(self.last_compiled_utc))

        digest = _sha256(self._canonical_payload())
        supplied = str(self.truth_sha256 or "").strip()
        if supplied and supplied != digest:
            raise ValueError("truth_sha256 does not match the compiled truth")
        object.__setattr__(self, "truth_sha256", digest)

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "sample_count": self.sample_count,
            "win_rate_bps": round(self.win_rate_bps, 6),
            "avg_slippage_bps": round(self.avg_slippage_bps, 6),
            "worst_drawdown_bps": round(self.worst_drawdown_bps, 6),
            "known_failure_modes": list(self.known_failure_modes),
            "caution_regimes": list(self.caution_regimes),
            "last_compiled_utc": self.last_compiled_utc,
        }

    def verify(self) -> bool:
        return self.truth_sha256 == _sha256(self._canonical_payload())

    def to_dict(self) -> dict[str, Any]:
        payload = self._canonical_payload()
        payload["truth_sha256"] = self.truth_sha256
        return payload

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "CompiledEntityTruth":
        if not isinstance(row, Mapping):
            raise TypeError("truth row must be a mapping")
        return cls(
            entity_id=str(row.get("entity_id") or ""),
            entity_type=str(row.get("entity_type") or "UNKNOWN"),
            sample_count=int(row.get("sample_count") or 0),
            win_rate_bps=float(row.get("win_rate_bps") or 0.0),
            avg_slippage_bps=float(row.get("avg_slippage_bps") or 0.0),
            worst_drawdown_bps=float(row.get("worst_drawdown_bps") or 0.0),
            known_failure_modes=tuple(str(item) for item in (row.get("known_failure_modes") or ())),
            caution_regimes=tuple(str(item) for item in (row.get("caution_regimes") or ())),
            last_compiled_utc=int(row.get("last_compiled_utc") or 0),
            truth_sha256=str(row.get("truth_sha256") or ""),
        )


@dataclass(slots=True, frozen=True)
class PostMortemPenalty:
    """Deterministic confidence/sizing haircut derived from retrieved memory."""

    matched_caution_regimes: tuple[str, ...] = ()
    matched_failure_modes: tuple[str, ...] = ()
    confidence_multiplier: float = 1.0
    sizing_multiplier: float = 1.0
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_caution_regimes": list(self.matched_caution_regimes),
            "matched_failure_modes": list(self.matched_failure_modes),
            "confidence_multiplier": round(float(self.confidence_multiplier), 6),
            "sizing_multiplier": round(float(self.sizing_multiplier), 6),
            "applied": bool(self.applied),
        }


# ---------------------------------------------------------------------------
# layer 2b -- verified snapshot checkpoints (V2.3 PART 2.2)
# ---------------------------------------------------------------------------


def _canonical_truth_row(row: Any) -> dict[str, Any]:
    """Normalise one sealed truth into its canonical, hash-stable dictionary."""

    if isinstance(row, CompiledEntityTruth):
        return row.to_dict()
    if isinstance(row, Mapping):
        return CompiledEntityTruth.from_dict(row).to_dict()
    raise TypeError("compiled truth rows must be CompiledEntityTruth or mappings")


@dataclass(slots=True, frozen=True)
class MemoryCheckpoint:
    """A verified snapshot sealed over a prefix of the post-mortem timeline.

    ``checkpoint_seq`` is the *cumulative* number of raw events the checkpoint
    covers, so the canonical replay rule is simply ``seq > checkpoint_seq``.
    ``event_count_compacted`` is the size of the batch this checkpoint sealed
    (a later checkpoint over the remaining tail reports its own batch size).

    ``checkpoint_sha256`` commits to the sequence, the cutoff and the compiled
    truths in canonical JSON -- the digest is what makes a checkpoint verifiable
    rather than merely cached, so a tampered snapshot is detected at load time
    instead of silently redefining the entity's history.
    """

    checkpoint_seq: int
    cutoff_ts_utc: int
    event_count_compacted: int
    compiled_truths: tuple[dict[str, Any], ...] = ()
    checkpoint_sha256: str = ""

    def __post_init__(self) -> None:
        seq = int(self.checkpoint_seq)
        if seq < 0:
            raise ValueError("checkpoint_seq must be non-negative")
        object.__setattr__(self, "checkpoint_seq", seq)

        cutoff = int(self.cutoff_ts_utc)
        if cutoff < 0:
            raise ValueError("cutoff_ts_utc must be non-negative")
        object.__setattr__(self, "cutoff_ts_utc", cutoff)

        count = int(self.event_count_compacted)
        if count < 0:
            raise ValueError("event_count_compacted must be non-negative")
        object.__setattr__(self, "event_count_compacted", count)

        truths = tuple(
            sorted(
                (_canonical_truth_row(row) for row in self.compiled_truths),
                key=lambda row: str(row.get("entity_id", "")),
            )
        )
        object.__setattr__(self, "compiled_truths", truths)

        digest = _sha256(self._canonical_payload())
        supplied = str(self.checkpoint_sha256 or "").strip()
        if supplied and supplied != digest:
            raise ValueError("checkpoint_sha256 does not match the sealed snapshot")
        object.__setattr__(self, "checkpoint_sha256", digest)

    def _canonical_payload(self) -> dict[str, Any]:
        """Exactly the sealed inputs: sequence, cutoff and compiled truths.

        ``event_count_compacted`` is bookkeeping for the operator and is
        deliberately excluded, so a third party can re-derive the digest from
        the three things the checkpoint actually claims.
        """

        return {
            "checkpoint_seq": self.checkpoint_seq,
            "cutoff_ts_utc": self.cutoff_ts_utc,
            "compiled_truths": [dict(row) for row in self.compiled_truths],
        }

    def truth_sha256s(self) -> tuple[str, ...]:
        return tuple(
            str(row.get("truth_sha256", "")) for row in self.compiled_truths
        )

    def verify(self) -> bool:
        return self.checkpoint_sha256 == _sha256(self._canonical_payload())

    def to_dict(self) -> dict[str, Any]:
        payload = self._canonical_payload()
        payload["event_count_compacted"] = self.event_count_compacted
        payload["checkpoint_sha256"] = self.checkpoint_sha256
        return payload

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "MemoryCheckpoint":
        if not isinstance(row, Mapping):
            raise TypeError("checkpoint row must be a mapping")
        return cls(
            checkpoint_seq=int(row.get("checkpoint_seq") or 0),
            cutoff_ts_utc=int(row.get("cutoff_ts_utc") or 0),
            event_count_compacted=int(row.get("event_count_compacted") or 0),
            compiled_truths=tuple(row.get("compiled_truths") or ()),
            checkpoint_sha256=str(row.get("checkpoint_sha256") or ""),
        )


@dataclass(slots=True)
class _FoldState:
    """Exact incremental fold of one entity's canonical event order.

    The fold carries every quantity ``_compile`` needs -- sample count, wins,
    the running slippage sum, and the four drawdown accumulators -- so a
    checkpoint can be *continued* instead of recomputed.  The arithmetic is the
    identical left-to-right sequence the full replay performs, which is what
    makes ``truth_sha256`` byte-identical across the two paths.
    """

    entity_id: str
    samples: int = 0
    wins: int = 0
    slippage_sum: float = 0.0
    running: int = 0
    peak: int = 0
    trough: int = 0
    worst: int = 0
    last_ts: int = 0
    modes: set[str] = field(default_factory=set)
    regimes: set[str] = field(default_factory=set)

    def copy(self) -> "_FoldState":
        return _FoldState(
            entity_id=self.entity_id,
            samples=self.samples,
            wins=self.wins,
            slippage_sum=self.slippage_sum,
            running=self.running,
            peak=self.peak,
            trough=self.trough,
            worst=self.worst,
            last_ts=self.last_ts,
            modes=set(self.modes),
            regimes=set(self.regimes),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "samples": int(self.samples),
            "wins": int(self.wins),
            "slippage_sum": float(self.slippage_sum),
            "running": int(self.running),
            "peak": int(self.peak),
            "trough": int(self.trough),
            "worst": int(self.worst),
            "last_ts": int(self.last_ts),
            "modes": sorted(self.modes),
            "regimes": sorted(self.regimes),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "_FoldState":
        if not isinstance(row, Mapping):
            raise TypeError("fold row must be a mapping")
        state = cls(entity_id=str(row.get("entity_id") or ""))
        state.samples = int(row.get("samples") or 0)
        state.wins = int(row.get("wins") or 0)
        state.slippage_sum = float(row.get("slippage_sum") or 0.0)
        state.running = int(row.get("running") or 0)
        state.peak = int(row.get("peak") or 0)
        state.trough = int(row.get("trough") or 0)
        state.worst = int(row.get("worst") or 0)
        state.last_ts = int(row.get("last_ts") or 0)
        state.modes = {str(item) for item in (row.get("modes") or ())}
        state.regimes = {str(item) for item in (row.get("regimes") or ())}
        return state


# ---------------------------------------------------------------------------
# deterministic local embeddings (V2.3 PART 2.3)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9_]+")


def _embedding_features(text: str) -> list[str]:
    """Word unigrams plus character trigrams, lowercased and namespace-tagged."""

    features: list[str] = []
    for token in _WORD_RE.findall(str(text or "").lower()):
        if len(token) >= 2:
            features.append(f"w:{token}")
        padded = f"^{token}$"
        span = len(padded) - EMBED_NGRAM + 1
        for index in range(span if span > 0 else 1):
            features.append(f"c:{padded[index : index + EMBED_NGRAM]}")
    return features


def _feature_bucket(feature: str, dims: int) -> tuple[int, float]:
    """Stable (bucket, sign) from ``hashlib`` -- never Python's salted ``hash``."""

    bucket_digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    sign_digest = hashlib.blake2b(
        b"sign:" + feature.encode("utf-8"), digest_size=2
    ).digest()
    bucket = int.from_bytes(bucket_digest, "big") % max(1, int(dims))
    sign = 1.0 if (sign_digest[0] & 1) else -1.0
    return bucket, sign


def hashed_embedding(text: str, dims: int = EMBED_DIMS) -> tuple[float, ...]:
    """Deterministic 384-dim hashed n-gram embedding, L2-normalised.

    Zero model download, zero network: every feature is hashed with
    ``hashlib.blake2b`` so the vector is identical across processes and across
    restarts.  ``PYTHONHASHSEED`` cannot perturb it because ``hash()`` is never
    used.
    """

    width = max(1, int(dims))
    vector = [0.0] * width
    for feature in _embedding_features(text):
        bucket, sign = _feature_bucket(feature, width)
        vector[bucket] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return tuple(vector)
    return tuple(value / norm for value in vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine of two vectors already L2-normalised by the embedder."""

    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    for a, b in zip(left, right):
        dot += float(a) * float(b)
    return max(-1.0, min(1.0, dot))


#: Bounded memo so a large truth set does not re-embed identical corpora.
_EMBED_CACHE: dict[str, tuple[float, ...]] = {}
_EMBED_CACHE_LIMIT = 4096


def _cached_hashed_embedding(text: str) -> tuple[float, ...]:
    key = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
    cached = _EMBED_CACHE.get(key)
    if cached is not None:
        return cached
    vector = hashed_embedding(text)
    if len(_EMBED_CACHE) >= _EMBED_CACHE_LIMIT:
        _EMBED_CACHE.clear()
    _EMBED_CACHE[key] = vector
    return vector


def _sparse_vector(text: str) -> tuple[tuple[float, ...], tuple[int, ...], tuple[float, ...]]:
    """``(dense, non_zero_indices, non_zero_values)`` for the hashed embedding.

    The hashed n-gram vector is extremely sparse (a few dozen live buckets out of
    384), so a cosine computed over the live buckets of the *query* adds exactly
    the same terms, in exactly the same ascending-index order, as the dense dot
    product: every skipped term is ``0.0 * value == +/-0.0`` and adding a signed
    zero to a float accumulator is an identity.
    """

    vector = _cached_hashed_embedding(text)
    indices: list[int] = []
    values: list[float] = []
    for index, value in enumerate(vector):
        if value:
            indices.append(index)
            values.append(value)
    return vector, tuple(indices), tuple(values)


def _sparse_cosine(
    query_indices: Sequence[int],
    query_values: Sequence[float],
    document: Sequence[float],
) -> float:
    """Bit-identical cosine via the query's non-zero buckets only."""

    width = len(document)
    if not query_indices or not width or query_indices[-1] >= width:
        return 0.0
    dot = 0.0
    for index, value in zip(query_indices, query_values):
        dot += value * document[index]
    return max(-1.0, min(1.0, dot))


#: One bounded probe per process; a dead daemon disables the hook for good.
_OLLAMA_STATE: dict[str, Any] = {"model": "", "enabled": False, "probed": False}


def _ollama_embedding(text: str, model: str, base_url: str) -> tuple[float, ...] | None:
    """Opt-in Ollama embedding.  Bounded, silent, and never retried on failure."""

    try:  # imported lazily: the default path must not even load the module
        import urllib.request

        payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_S) as response:
            body = json.loads(response.read().decode("utf-8"))
        raw = body.get("embedding")
        if not isinstance(raw, list) or not raw:
            return None
        values = [float(item) for item in raw]
    except Exception:  # noqa: BLE001 - an unreachable model degrades silently
        return None
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0.0:
        return None
    return tuple(value / norm for value in values)


def ollama_embedder() -> Any:
    """Return a live Ollama embedder, or ``None`` when the hook is unavailable.

    Dormant unless ``NIOS_OLLAMA_EMBED_MODEL`` is explicitly set.  The probe is
    performed once, with a bounded timeout, and any failure (missing binary,
    refused connection, malformed reply) permanently disables the hook for this
    process so a dead daemon can never become a retry storm.
    """

    model = str(os.environ.get(OLLAMA_EMBED_ENV) or "").strip()
    if not model:
        return None
    state = _OLLAMA_STATE
    if state["model"] == model and state["probed"]:
        return (
            (lambda text: _ollama_embedding(text, model, _ollama_base_url()))
            if state["enabled"]
            else None
        )
    state["model"] = model
    state["probed"] = True
    state["enabled"] = False
    # One warm-up vector decides; the result is discarded.
    if _ollama_embedding("nios embedding probe", model, _ollama_base_url()) is None:
        return None
    state["enabled"] = True
    return lambda text: _ollama_embedding(text, model, _ollama_base_url())


def _ollama_base_url() -> str:
    return (
        str(os.environ.get(OLLAMA_BASE_URL_ENV) or "").strip()
        or OLLAMA_DEFAULT_BASE_URL
    )


# ---------------------------------------------------------------------------
# the graph
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RetrievalEntry:
    """One cached entity corpus projection (V2.4 PART 2).

    ``counts``/``length``/``distinct`` are the exact token statistics the BM25
    arm derives from the corpus document, so a hit never re-tokenizes; ``vector``
    is the 384-dim hashed n-gram embedding, stored as the immutable tuple the
    embedder produced (no per-call re-materialisation).

    ``timeline_seq`` and ``checkpoint_seq`` record the graph counters the entry
    was built under, for audit.  *Validation* uses the narrower
    ``(epoch, arm_revision, truth_sha256)`` triple: an append that does not touch
    this entity must not evict it, and a global sequence number would evict every
    entity on every unrelated tick.
    """

    epoch: int
    arm_revision: int
    truth_sha256: str
    counts: dict[str, int]
    length: int
    distinct: frozenset[str]
    vector: tuple[float, ...] | None
    timeline_seq: int
    checkpoint_seq: int


class EntityMemoryGraph:
    """Append-only entity timeline plus deterministically compiled truths.

    The graph is thread-safe and dependency-free. ``ControlPlane`` owns one
    instance, persists ``to_state()`` into the Ring 0 atomic state journal and
    restores it on boot, which is what makes the memory survive a restart and
    vanish on a clean-slate reset.
    """

    def __init__(
        self,
        state: Mapping[str, Any] | None = None,
        *,
        max_events: int = MAX_TIMELINE_EVENTS,
        compact_threshold: int = COMPACT_THRESHOLD,
        keep_recent: int = KEEP_RECENT,
        archive_path: str | Path | None = None,
        max_archive_events: int = MAX_COLD_ARCHIVE_EVENTS,
    ) -> None:
        self._lock = threading.RLock()
        self._max_events = max(64, int(max_events))
        self._compact_threshold = max(1, int(compact_threshold))
        self._keep_recent = max(0, int(keep_recent))
        self._events: list[PostMortemEvent] = []
        self._truths: dict[str, CompiledEntityTruth] = {}
        #: Sealed prefix fold, keyed by ``canonical_entity_alias``.
        self._folds: dict[str, _FoldState] = {}
        self._checkpoint: MemoryCheckpoint | None = None
        self._sealed_seq = 0
        self._sequence = 0
        self._dirty = False
        #: Cold archive: the raw events a checkpoint removed from the hot
        #: timeline.  Kept in a bounded in-memory list *and*, when a workspace
        #: path is supplied, mirrored into an append-only JSONL file so the
        #: sealed history survives a restart and stays readable.
        self._cold_archive: list[PostMortemEvent] = []
        self._max_archive_events = max(0, int(max_archive_events))
        self._archive_path: Path | None = (
            Path(archive_path) if archive_path is not None else None
        )
        # -- V2.4 PART 2: incremental retrieval cache ---------------------
        #: LRU of per-entity corpus projections, keyed by canonical entity id.
        self._retrieval_cache: OrderedDict[str, _RetrievalEntry] = OrderedDict()
        self._cache_limit = max(1, int(MEMORY_CACHE_MAX_ENTITIES))
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_evictions = 0
        #: Per-arm lesson revision.  Bumped only for the entities an appended
        #: event actually names (and for the entities a compaction removed), so
        #: an unrelated outcome never invalidates a hot entry.
        self._arm_revision: dict[str, int] = {}
        #: Bumped on every whole-graph replacement (``reset``/``load_state``).
        self._cache_epoch = 0
        #: Bumped whenever the truth set or the corpus may have changed; it keys
        #: the shared document-frequency table.
        self._corpus_generation = 0
        self._df_cache: tuple[int, dict[str, int], float, int] | None = None
        if isinstance(state, Mapping):
            self.load_state(state)

    # -- layer 1 ---------------------------------------------------------

    def record_outcome(
        self,
        *,
        outcome_kind: str,
        net_pnl_micros: int,
        slippage_bps: float = 0.0,
        symbol: str = "",
        silo: str = "",
        strategy_id: str = "",
        regime: str = "",
        macro_event_id: str = "",
        lesson_summary: str = "",
        ts_utc: int | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one immutable outcome event and recompile the touched entities."""

        stamp = int(ts_utc if ts_utc is not None else time.time_ns())
        with self._lock:
            self._sequence += 1
            arms: list[str] = []
            for kind, value in (
                ("symbol", _fold_symbol(symbol)),
                ("silo", str(silo or "").strip().upper()),
                ("strategy", str(strategy_id or "").strip()),
                ("macro", str(macro_event_id or "").strip().upper()),
                ("regime", str(regime or "").strip().upper()),
            ):
                if value:
                    arms.append(_entity_id(kind, value))
            if not arms:
                raise ValueError("record_outcome requires at least one entity reference")

            event = PostMortemEvent(
                event_id=str(event_id or f"PM-{stamp}-{self._sequence:06d}"),
                ts_utc=stamp,
                entity_ids=tuple(arms),
                outcome_kind=outcome_kind,
                net_pnl_micros=int(net_pnl_micros),
                slippage_bps=float(slippage_bps),
                lesson_summary=lesson_summary,
            )
            self._events.append(event)
            # Canonical order is ``(ts_utc, event_id)``.  Keeping the hot list
            # sorted means a checkpoint fold can never observe a different
            # sequence than a full replay of the same events.
            self._events.sort(key=lambda row: (row.ts_utc, row.event_id))
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events :]
            self._dirty = True
            # V2.4 PART 2: only the entities this event names can have a new
            # corpus document, so only their cached projections are dropped.
            self._invalidate_entities(event.entity_ids)
            # V2.3 PART 2.2: seal a checkpoint the moment the active timeline
            # outgrows the threshold so startup replay is bounded by
            # ``keep_recent`` instead of growing without limit.
            compacted: dict[str, Any] | None = None
            if len(self._events) > self._compact_threshold:
                compacted = self.compact_timeline(self._keep_recent)
            touched = self._recompile(event.entity_ids, fallback_stamp=stamp)
            return {
                "event": event.to_dict(),
                "compiled": {key: truth.to_dict() for key, truth in sorted(touched.items())},
                "timeline_size": len(self._events),
                "event_count": len(self._events) + int(self._sealed_seq),
                "entity_count": len(self._truths),
                "checkpoint_seq": int(self._sealed_seq),
                "checkpoint_sha256": (
                    self._checkpoint.checkpoint_sha256
                    if self._checkpoint is not None
                    else ""
                ),
                "compacted": bool(compacted and compacted.get("compacted")),
            }

    def timeline(self, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = list(self._events)
        if limit is not None and int(limit) > 0:
            rows = rows[-int(limit) :]
        return tuple(row.to_dict() for row in rows)

    # -- layer 2 ---------------------------------------------------------

    def compile_truths(self) -> dict[str, CompiledEntityTruth]:
        """Recompile every entity from the raw timeline."""

        with self._lock:
            touched = self._recompile(None, fallback_stamp=time.time_ns())
        return dict(sorted(touched.items()))

    def truths(self) -> tuple[CompiledEntityTruth, ...]:
        with self._lock:
            return tuple(sorted(self._truths.values(), key=lambda row: row.entity_id))

    def truth(self, entity_id: str) -> CompiledEntityTruth | None:
        key = str(entity_id or "").strip()
        with self._lock:
            found = self._truths.get(key)
            if found is not None:
                return found
            # A caller may ask with any spelling of the same instrument.  Resolve
            # through the canonical alias so ``symbol:BTCUSDT`` finds a truth that
            # was compiled under ``symbol:BTC-USDT`` and vice versa.
            alias = canonical_entity_alias(key)
            if not alias.startswith("symbol:"):
                return None
            for candidate, truth in self._truths.items():
                if canonical_entity_alias(candidate) == alias:
                    return truth
            return None

    def _recompile(
        self,
        entity_ids: Iterable[str] | None,
        *,
        fallback_stamp: int,
    ) -> dict[str, CompiledEntityTruth]:
        if entity_ids is None:
            candidates = (
                {arm for event in self._events for arm in event.entity_ids}
                | set(self._truths)
                | {state.entity_id for state in self._folds.values()}
            )
        else:
            # Fold against the *existing* truths too, not just the new arms: that
            # is what stops a second spelling of an instrument from opening a
            # parallel truth whose sample count nobody else can see.
            candidates = {str(row) for row in entity_ids} | set(self._truths)
        # One instrument, one truth: every spelling of the same symbol folds onto a
        # single representative (the folder-safe hyphen form sorts first) so the
        # sample count cannot be split across two arms that never see each other.
        representatives: dict[str, str] = {}
        for entity_id in sorted(candidates):
            alias = canonical_entity_alias(entity_id)
            if alias.startswith("symbol:"):
                representatives.setdefault(alias, entity_id)
        touched: dict[str, CompiledEntityTruth] = {}
        for entity_id in sorted(set(representatives.values()) | {
            row for row in candidates if not canonical_entity_alias(row).startswith("symbol:")
        }):
            truth = self._compile(entity_id, fallback_stamp=fallback_stamp)
            if truth.sample_count <= 0:
                self._truths.pop(entity_id, None)
                continue
            self._truths[entity_id] = truth
            touched[entity_id] = truth
        # Drop any folded alias that is no longer the representative: its samples
        # are now counted by the representative, so keeping it would double-count.
        for stale in [
            key
            for key in self._truths
            if canonical_entity_alias(key).startswith("symbol:")
            and representatives.get(canonical_entity_alias(key), key) != key
        ]:
            self._truths.pop(stale, None)
        # The truth set (and therefore the corpus) may have changed: key the
        # shared document-frequency table on the new generation.
        self._corpus_generation += 1
        return touched

    def _compile(self, entity_id: str, *, fallback_stamp: int) -> CompiledEntityTruth:
        """Compile one entity: sealed checkpoint fold + hot delta, in order.

        The fold state carries the sealed prefix (``checkpoint_seq`` events),
        so only ``seq > checkpoint_seq`` -- the hot timeline -- is walked here.
        """

        alias = canonical_entity_alias(entity_id)
        is_symbol = alias.startswith("symbol:")
        sealed = self._folds.get(alias)
        state = sealed.copy() if sealed is not None else _FoldState(entity_id=entity_id)
        state.entity_id = entity_id
        for event in self._ordered_events():
            if self._event_belongs(event, entity_id, alias=alias, is_symbol=is_symbol):
                self._apply_event(state, event)
        return self._truth_from_state(entity_id, state, fallback_stamp=fallback_stamp)

    @staticmethod
    def _event_belongs(
        event: PostMortemEvent,
        entity_id: str,
        *,
        alias: str,
        is_symbol: bool,
    ) -> bool:
        if entity_id in event.entity_ids:
            return True
        if not is_symbol:
            return False
        # Fold every spelling of the same instrument into one sample set, so a
        # loss booked under ``BTC/USDT`` counts for ``BTCUSDT`` too.
        return any(canonical_entity_alias(arm) == alias for arm in event.entity_ids)

    def _ordered_events(self) -> list[PostMortemEvent]:
        """The canonical order: ``(ts_utc, event_id)``, never append order."""

        return sorted(self._events, key=lambda event: (event.ts_utc, event.event_id))

    def _apply_event(self, state: _FoldState, event: PostMortemEvent) -> None:
        """Fold one event into a state with the exact full-replay arithmetic.

        ``state.slippage_sum = state.slippage_sum + slip`` is the same
        left-to-right float addition ``sum(...)`` performs, and the drawdown
        accumulators are the same four integers the linear scan maintains.  Two
        folds that see the same ordered events therefore produce bit-identical
        metrics.
        """

        state.samples += 1
        if event.outcome_kind == "WIN":
            state.wins += 1
        state.slippage_sum = state.slippage_sum + float(event.slippage_bps)
        state.running += int(event.net_pnl_micros)
        state.trough = min(state.trough, state.running)
        state.peak = max(state.peak, state.running)
        state.worst = max(state.worst, state.peak - state.running)
        mode = FAILURE_MODE_BY_OUTCOME.get(event.outcome_kind)
        if mode:
            state.modes.add(mode)
        if abs(event.slippage_bps) >= HIGH_SLIPPAGE_BPS:
            state.modes.add(HIGH_SLIPPAGE_MODE)
        if event.outcome_kind in ADVERSE_OUTCOME_KINDS:
            for arm in event.entity_ids:
                kind, value = _split_entity(arm)
                if kind == "regime" and value:
                    state.regimes.add(value.upper())
        state.last_ts = max(state.last_ts, int(event.ts_utc))

    def _truth_from_state(
        self,
        entity_id: str,
        state: _FoldState,
        *,
        fallback_stamp: int,
    ) -> CompiledEntityTruth:
        samples = int(state.samples)
        if samples:
            win_rate_bps = (state.wins / samples) * 10_000.0
            avg_slippage_bps = state.slippage_sum / samples
        else:
            win_rate_bps = 0.0
            avg_slippage_bps = 0.0
        reference = max(int(state.peak), abs(int(state.trough)), 1)
        worst_drawdown_bps = max(
            0.0, min(10_000.0, (int(state.worst) / reference) * 10_000.0)
        )
        watermark = int(state.last_ts) if samples else int(fallback_stamp)
        return CompiledEntityTruth(
            entity_id=entity_id,
            entity_type=ENTITY_TYPE_BY_KIND.get(_split_entity(entity_id)[0], "UNKNOWN"),
            sample_count=samples,
            win_rate_bps=round(win_rate_bps, 6),
            avg_slippage_bps=round(avg_slippage_bps, 6),
            worst_drawdown_bps=round(worst_drawdown_bps, 6),
            known_failure_modes=tuple(sorted(state.modes)[:MAX_FAILURE_MODES]),
            caution_regimes=tuple(sorted(state.regimes)[:MAX_CAUTION_REGIMES]),
            last_compiled_utc=int(watermark),
        )

    # -- tier-2 compaction -----------------------------------------------

    def _fold_event_into_entities(self, event: PostMortemEvent) -> None:
        seen: set[str] = set()
        for arm in event.entity_ids:
            alias = canonical_entity_alias(arm)
            if alias in seen:  # two spellings of one instrument must fold once
                continue
            seen.add(alias)
            state = self._folds.get(alias)
            if state is None:
                state = _FoldState(entity_id=arm)
                self._folds[alias] = state
            self._apply_event(state, event)

    def _truths_from_folds(self, *, fallback_stamp: int) -> dict[str, CompiledEntityTruth]:
        out: dict[str, CompiledEntityTruth] = {}
        for state in self._folds.values():
            truth = self._truth_from_state(
                state.entity_id, state, fallback_stamp=fallback_stamp
            )
            if truth.sample_count > 0:
                out[truth.entity_id] = truth
        return dict(sorted(out.items()))

    def compact_timeline(
        self,
        keep_recent: int = KEEP_RECENT,
        *,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Seal the oldest events into a verified checkpoint and archive them.

        Everything that sorts at or before ``cutoff_ts_utc`` leaves the hot
        timeline: its contribution is folded into a per-entity state, the
        compiled truths are sealed inside a ``MemoryCheckpoint``, and the raw
        events move to the cold archive (bounded list + append-only JSONL when a
        workspace path is configured).  Startup then replays only
        ``seq > checkpoint_seq``.
        """

        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            keep = max(0, int(keep_recent))
            if len(self._events) <= keep:
                return {
                    "compacted": False,
                    "reason": "TIMELINE_WITHIN_HOT_BOUND",
                    "checkpoint": (
                        self._checkpoint.to_dict()
                        if self._checkpoint is not None
                        else None
                    ),
                    "active_events": len(self._events),
                    "sealed_events": int(self._sealed_seq),
                    "archive_events": len(self._cold_archive),
                    "entity_count": len(self._truths),
                    "as_of_utc_ns": stamp,
                }
            ordered = self._ordered_events()
            cut = len(ordered) - keep
            sealed = ordered[:cut]
            first_seq = int(self._sealed_seq) + 1
            for event in sealed:
                self._fold_event_into_entities(event)
            self._sealed_seq += len(sealed)
            self._events = ordered[cut:]
            self._archive_events(sealed, first_seq=first_seq)
            # V2.4 PART 2: sealing removes lessons from the hot corpus, so the
            # cached projection of every entity those events named is stale.
            self._invalidate_entities(
                arm for event in sealed for arm in event.entity_ids
            )
            # The checkpoint freezes the *sealed prefix* truths ...
            sealed_truths = self._truths_from_folds(fallback_stamp=stamp)
            checkpoint = MemoryCheckpoint(
                checkpoint_seq=int(self._sealed_seq),
                cutoff_ts_utc=int(sealed[-1].ts_utc),
                event_count_compacted=len(sealed),
                compiled_truths=tuple(
                    truth.to_dict() for truth in sealed_truths.values()
                ),
            )
            self._checkpoint = checkpoint
            # ... while the graph's live truths must also carry the hot delta.
            # Recompiling here (bounded by ``keep_recent``) keeps ``truths()``
            # correct even when compaction is invoked explicitly by an operator.
            self._recompile(None, fallback_stamp=stamp)
            self._dirty = True
            return {
                "compacted": True,
                "reason": "CHECKPOINT_SEALED",
                "checkpoint": checkpoint.to_dict(),
                "checkpoint_sha256": checkpoint.checkpoint_sha256,
                "checkpoint_seq": checkpoint.checkpoint_seq,
                "cutoff_ts_utc": checkpoint.cutoff_ts_utc,
                "event_count_compacted": checkpoint.event_count_compacted,
                "active_events": len(self._events),
                "sealed_events": int(self._sealed_seq),
                "archived_events": len(sealed),
                "archive_events": len(self._cold_archive),
                "archive_path": str(self._archive_path) if self._archive_path else "",
                "entity_count": len(self._truths),
                "as_of_utc_ns": stamp,
            }

    def _archive_events(
        self, events: Sequence[PostMortemEvent], *, first_seq: int = 1
    ) -> None:
        self._cold_archive.extend(events)
        if self._max_archive_events and len(self._cold_archive) > self._max_archive_events:
            self._cold_archive = self._cold_archive[-self._max_archive_events :]
        path = self._archive_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for offset, event in enumerate(events):
                    handle.write(
                        json.dumps(
                            {"seq": int(first_seq) + offset, "event": event.to_dict()},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
        except OSError:  # pragma: no cover - a read-only workspace keeps memory-only
            return

    def cold_archive(self, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        """The in-memory cold archive tail (newest last).

        The bounded list holds what this process sealed; the durable, complete
        record is the append-only JSONL file read by :meth:`read_archive_file`.
        """

        with self._lock:
            rows = list(self._cold_archive)
        if limit is not None and int(limit) > 0:
            rows = rows[-int(limit) :]
        return tuple(row.to_dict() for row in rows)

    def read_archive_file(self, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        """Read the durable JSONL cold archive (complete, across restarts)."""

        path = self._archive_path
        if path is None:
            return ()
        try:
            if not path.is_file():
                return ()
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:  # pragma: no cover - defensive
            return ()
        rows: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except (TypeError, ValueError):
                continue
        if limit is not None and int(limit) > 0:
            rows = rows[-int(limit) :]
        return tuple(rows)

    def checkpoint(self) -> MemoryCheckpoint | None:
        with self._lock:
            return self._checkpoint

    def seal_summary(self) -> dict[str, Any]:
        """Compact description of the sealed prefix, for the REST surface."""

        with self._lock:
            checkpoint = self._checkpoint
            return {
                "checkpoint_seq": int(self._sealed_seq),
                "active_events": len(self._events),
                "event_count": len(self._events) + int(self._sealed_seq),
                "archive_events": len(self._cold_archive),
                "archive_path": str(self._archive_path) if self._archive_path else "",
                "entity_count": len(self._truths),
                "compact_threshold": int(self._compact_threshold),
                "keep_recent": int(self._keep_recent),
                "checkpoint": (
                    checkpoint.to_dict() if checkpoint is not None else None
                ),
                "checkpoint_sha256": (
                    checkpoint.checkpoint_sha256 if checkpoint is not None else ""
                ),
            }

    # -- retrieval cache (V2.4 PART 2) ------------------------------------

    def cache_stats(self) -> dict[str, int]:
        """Counters for the bounded per-entity retrieval cache."""

        with self._lock:
            return {
                "hits": int(self._cache_hits),
                "misses": int(self._cache_misses),
                "entries": len(self._retrieval_cache),
                "evictions": int(self._cache_evictions),
            }

    def cache_clear(self) -> dict[str, int]:
        """Drop every cached projection and reset the counters."""

        with self._lock:
            self._retrieval_cache.clear()
            self._arm_revision.clear()
            self._df_cache = None
            self._cache_epoch += 1
            self._cache_hits = 0
            self._cache_misses = 0
            self._cache_evictions = 0
            return {
                "hits": 0,
                "misses": 0,
                "entries": 0,
                "evictions": 0,
            }

    def _invalidate_entities(self, arms: Iterable[str]) -> None:
        """Invalidate exactly the entities the caller names."""

        for arm in arms:
            key = str(arm)
            self._retrieval_cache.pop(key, None)
            self._arm_revision[key] = self._arm_revision.get(key, 0) + 1

    def _cache_lookup(
        self,
        entity_id: str,
        *,
        epoch: int,
        revision: int,
        truth_sha256: str,
        require_vector: bool = False,
    ) -> _RetrievalEntry | None:
        entry = self._retrieval_cache.get(entity_id)
        if (
            entry is not None
            and entry.epoch == epoch
            and entry.arm_revision == revision
            and entry.truth_sha256 == truth_sha256
            and (entry.vector is not None or not require_vector)
        ):
            self._retrieval_cache.move_to_end(entity_id)
            self._cache_hits += 1
            return entry
        self._cache_misses += 1
        return None

    def _cache_store(
        self,
        entity_id: str,
        entry: _RetrievalEntry,
        *,
        epoch: int,
        revision: int,
    ) -> None:
        # Never publish an entry built from a snapshot that has since moved on.
        if entry.epoch != epoch or self._arm_revision.get(entity_id, 0) != revision:
            return
        cache = self._retrieval_cache
        cache[entity_id] = entry
        cache.move_to_end(entity_id)
        while len(cache) > self._cache_limit:
            cache.popitem(last=False)
            self._cache_evictions += 1

    @property
    def timeline_seq(self) -> int:
        """Monotonic counter of every outcome the graph has ever accepted."""

        with self._lock:
            return int(self._sequence)

    @property
    def checkpoint_seq(self) -> int:
        """Cumulative number of events sealed into the current checkpoint."""

        with self._lock:
            return int(self._sealed_seq)

    # -- hybrid retrieval ------------------------------------------------

    def query_entity_memory(
        self,
        symbol: str = "",
        silo: str = "",
        strategy_id: str = "",
        regime: str = "",
        macro_context: str = "",
        query_text: str = "",
        top_k: int = 5,
        *,
        now_ns: int | None = None,
        use_semantic: bool = True,
        embedder: Any = None,
    ) -> dict[str, Any]:
        """3-signal hybrid retrieval that seals its numbers.

        Fusion (V2.3 PART 2.3): ``0.25`` BM25 lexical + ``0.45`` canonical
        entity-feature + ``0.30`` deterministic 384-dim hashed n-gram cosine.
        The semantic arm is offline by construction; an Ollama embedder is used
        only when the operator sets ``NIOS_OLLAMA_EMBED_MODEL`` and the daemon
        answers the single bounded probe.

        Returns the ranked compiled truths (each with its per-signal breakdown),
        the past lessons that produced them, the deterministic penalty implied
        by the match, and a verified ``GroundedFactPacket`` carrying the four
        retrieved metrics, the fusion weights and the confidence multiplier.
        """

        stamp = int(now_ns if now_ns is not None else time.time_ns())
        limit = max(1, min(int(top_k), MAX_TOP_K))

        features = {
            "symbol": str(symbol or "").strip().upper(),
            "silo": str(silo or "").strip().upper(),
            "strategy": str(strategy_id or "").strip(),
            "regime": str(regime or "").strip().upper(),
            "macro": str(macro_context or "").strip().upper(),
        }
        active = {kind: value for kind, value in features.items() if value}
        total_weight = sum(KIND_WEIGHTS[kind] for kind in active) or 1.0
        # Aliases, not raw ids: a query for ``BTCUSDT`` must match a truth compiled
        # under ``BTC-USDT`` or it silently scores the instrument at zero -- which
        # is exactly how a sizing gate fails open on a symbol it already lost on.
        # Built from the alias directly so a slash ticker never reaches the
        # path-token sanitizer (which would reject ``BTC/USDT`` as a traversal).
        target_ids: set[str] = set()
        for kind, value in active.items():
            if kind == "symbol":
                target_ids.add(canonical_entity_alias(f"symbol:{value}"))
            else:
                target_ids.add(_entity_id(kind, value))

        with self._lock:
            truth_rows = list(self._truths.values())
            events = list(self._events)
            sealed_seq = int(self._sealed_seq)
            generation = int(self._corpus_generation)
            epoch = int(self._cache_epoch)
            timeline_seq = int(self._sequence)
            revisions = {
                truth.entity_id: self._arm_revision.get(truth.entity_id, 0)
                for truth in truth_rows
            }

        # Resolve the semantic arm's embedder exactly where ``_semantic_scores``
        # would, so the cacheability decision cannot change when a probe fires.
        semantic_query = ""
        if use_semantic and truth_rows:
            semantic_query = " ".join(
                part
                for part in (str(query_text or ""), *(str(value) for value in features.values()))
                if part
            ).strip()
        embed: Any = None
        cached_semantics = True
        if semantic_query:
            embed = embedder if callable(embedder) else ollama_embedder()
            if embed is None:
                embed = _cached_hashed_embedding
            else:
                # An operator-supplied or daemon embedder is opaque: it may be
                # stateful or non-deterministic, so its vectors are never cached
                # and the pre-V2.4 path is used verbatim.
                cached_semantics = False

        # The cached projection path is taken whenever the semantic arm is
        # deterministic (default hashed embedder) or disabled.  An opaque
        # embedder falls through to the verbatim pre-V2.4 pass.
        use_cached_path = (not use_semantic) or cached_semantics
        projections: dict[str, _RetrievalEntry] | None = None
        if truth_rows and use_cached_path:
            projections = self._retrieval_projections(
                truth_rows,
                events,
                epoch=epoch,
                timeline_seq=timeline_seq,
                checkpoint_seq=sealed_seq,
                revisions=revisions,
                use_semantic=use_semantic and bool(semantic_query),
            )

        if projections is not None:
            lexical = self._bm25_cached(
                truth_rows, projections, _tokens(query_text), generation
            )
            ceiling = max(lexical.values()) if lexical else 0.0
            semantic = (
                self._semantic_scores_cached(truth_rows, projections, semantic_query)
                if use_semantic
                else {}
            )
        else:
            corpus = self._corpus(truth_rows, events)
            lexical = self._bm25(corpus, _tokens(query_text))
            ceiling = max(lexical.values()) if lexical else 0.0
            semantic = (
                self._semantic_scores(
                    corpus, features=features, query_text=query_text, embedder=embedder
                )
                if use_semantic
                else {}
            )

        scored: list[dict[str, Any]] = []
        for truth in truth_rows:
            matched_feature = canonical_entity_alias(truth.entity_id) in target_ids
            if active and not matched_feature:
                # A structured query is a *scoped* question -- "this symbol, this
                # silo, this strategy".  The similarity signals rank within that
                # scope; they must never widen it, or a BTC loss would surface as
                # context for an ETH trade and an isolated instrument would lose
                # its isolation.
                continue
            feature_score = (
                KIND_WEIGHTS[_split_entity(truth.entity_id)[0]] / total_weight
                if matched_feature
                else 0.0
            )
            lexical_score = (lexical.get(truth.entity_id, 0.0) / ceiling) if ceiling > 0 else 0.0
            semantic_score = float(semantic.get(truth.entity_id, 0.0))
            blended = (
                FEATURE_WEIGHT * feature_score
                + LEXICAL_WEIGHT * lexical_score
                + SEMANTIC_WEIGHT * semantic_score
            )
            if not active and not query_text.strip():
                blended = 1.0
            elif blended <= 0.0:
                continue
            scored.append(
                {
                    "score": blended,
                    "truth": truth,
                    "signals": {
                        "lexical": round(lexical_score, 6),
                        "feature": round(feature_score, 6),
                        "semantic": round(semantic_score, 6),
                        "fused": round(blended, 6),
                    },
                }
            )

        scored.sort(
            key=lambda row: (
                -row["score"],
                -row["truth"].sample_count,
                row["truth"].entity_id,
            )
        )
        selected = scored[:limit]
        truths: list[CompiledEntityTruth] = [row["truth"] for row in selected]
        entity_ids = [truth.entity_id for truth in truths]

        lessons = self._lessons(entity_ids, events, limit)
        penalty = self._penalty(truths, features["regime"])
        metrics = self._aggregate(truths)
        metrics["post_mortem_confidence_multiplier"] = penalty.confidence_multiplier
        # Every number that survives retrieval is sealed: the three signal
        # maxima ride the packet instead of leaking as bare prose.
        for name in ("lexical", "feature", "semantic"):
            metrics[f"retrieval_{name}_max"] = (
                round(max((row["signals"][name] for row in selected), default=0.0), 6)
            )
        metrics["retrieval_weight_lexical"] = LEXICAL_WEIGHT
        metrics["retrieval_weight_feature"] = FEATURE_WEIGHT
        metrics["retrieval_weight_semantic"] = SEMANTIC_WEIGHT

        packet = self._seal(
            symbol=features["symbol"] or "UNIVERSE",
            metrics=metrics,
            truths=truths,
            now_ns=stamp,
        )

        return {
            "query": dict(features),
            "query_text": str(query_text or ""),
            "entity_ids": entity_ids,
            "truths": [
                {**row["truth"].to_dict(), "score": round(row["score"], 6), "signals": dict(row["signals"])}
                for row in selected
            ],
            "lessons": lessons,
            "metrics": dict(metrics),
            "penalty": penalty.to_dict(),
            "sample_count": int(metrics["sample_count"]),
            "packet": packet.to_dict(),
            "packet_sha256": packet.sha256_digest,
            "as_of_utc_ns": stamp,
            "timeline_size": len(events),
            "checkpoint_seq": sealed_seq,
            "weights": {
                "lexical": LEXICAL_WEIGHT,
                "feature": FEATURE_WEIGHT,
                "semantic": SEMANTIC_WEIGHT,
            },
            "semantic_dim": EMBED_DIMS,
            "semantic_enabled": bool(use_semantic),
        }

    def penalty_for(
        self,
        symbol: str = "",
        silo: str = "",
        strategy_id: str = "",
        regime: str = "",
        macro_context: str = "",
        query_text: str = "",
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Penalty-only view, for callers that do not need the sealed packet."""

        result = self.query_entity_memory(
            symbol=symbol,
            silo=silo,
            strategy_id=strategy_id,
            regime=regime,
            macro_context=macro_context,
            query_text=query_text,
            top_k=top_k,
        )
        return result["penalty"]

    # -- retrieval internals ---------------------------------------------

    def _retrieval_projections(
        self,
        truths: Sequence[CompiledEntityTruth],
        events: Sequence[PostMortemEvent],
        *,
        epoch: int,
        timeline_seq: int,
        checkpoint_seq: int,
        revisions: Mapping[str, int],
        use_semantic: bool,
    ) -> dict[str, _RetrievalEntry]:
        """Return one cached corpus projection per truth (V2.4 PART 2).

        A projection carries the exact per-document artefacts the retrieval
        arms consume -- token counts, document length, the distinct-token set
        the IDF table needs, and the 384-dim vector -- so a hit performs no
        tokenization and no embedding at all.

        Misses are rebuilt from the same ``events`` snapshot the caller is about
        to score, through a lesson index built once per call, so a cold query is
        never worse than the pre-V2.4 full pass by more than one index build.
        """

        projections: dict[str, _RetrievalEntry] = {}
        lesson_index: dict[str, list[str]] | None = None
        for truth in truths:
            entity_id = truth.entity_id
            revision = int(revisions.get(entity_id, 0))
            entry = self._cache_lookup(
                entity_id,
                epoch=epoch,
                revision=revision,
                truth_sha256=truth.truth_sha256,
                require_vector=use_semantic,
            )
            if entry is not None:
                projections[entity_id] = entry
                continue
            if lesson_index is None:
                lesson_index = self._lesson_index(events)
            text = self._corpus_text(truth, lesson_index)
            tokens = _tokens(text)
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            fresh = _RetrievalEntry(
                epoch=epoch,
                arm_revision=revision,
                truth_sha256=truth.truth_sha256,
                counts=counts,
                length=len(tokens),
                distinct=frozenset(counts),
                vector=_cached_hashed_embedding(text) if use_semantic else None,
                timeline_seq=timeline_seq,
                checkpoint_seq=checkpoint_seq,
            )
            projections[entity_id] = fresh
            with self._lock:
                self._cache_store(entity_id, fresh, epoch=epoch, revision=revision)
        return projections

    @staticmethod
    def _lesson_index(events: Sequence[PostMortemEvent]) -> dict[str, list[str]]:
        """``arm -> lesson summaries`` in canonical event order."""

        index: dict[str, list[str]] = {}
        for event in events:
            summary = event.lesson_summary
            for arm in event.entity_ids:
                index.setdefault(arm, []).append(summary)
        return index

    @staticmethod
    def _corpus_text(
        truth: CompiledEntityTruth,
        lesson_index: Mapping[str, Sequence[str]],
    ) -> str:
        """Exactly the document ``_corpus`` builds for one entity."""

        parts = [
            truth.entity_id,
            truth.entity_type,
            " ".join(truth.known_failure_modes),
            " ".join(truth.caution_regimes),
            " ".join(lesson_index.get(truth.entity_id, ())),
        ]
        return " ".join(part for part in parts if part)

    def _document_frequency(
        self,
        projections: Mapping[str, _RetrievalEntry],
        generation: int,
    ) -> tuple[dict[str, int], float, int]:
        """Corpus-global IDF inputs, memoised per corpus generation.

        Document frequency is the one genuinely global term in BM25, so it is
        derived from the cached distinct-token sets (cheap integer counts)
        rather than from a re-tokenized corpus, and shared across queries until
        the corpus itself changes.
        """

        cached = self._df_cache
        if cached is not None and cached[0] == generation:
            return cached[1], cached[2], cached[3]
        document_frequency: dict[str, int] = {}
        total_docs = 0
        length_sum = 0
        for entry in projections.values():
            total_docs += 1
            length_sum += entry.length
            for token in entry.distinct:
                document_frequency[token] = document_frequency.get(token, 0) + 1
        average = (length_sum / total_docs) if total_docs else 0.0
        self._df_cache = (generation, document_frequency, average, total_docs)
        return document_frequency, average, total_docs

    def _bm25_cached(
        self,
        truths: Sequence[CompiledEntityTruth],
        projections: Mapping[str, _RetrievalEntry],
        query: Sequence[str],
        generation: int,
    ) -> dict[str, float]:
        """BM25 over cached projections -- identical arithmetic to ``_bm25``."""

        if not projections or not query:
            return {}
        document_frequency, average, total_docs = self._document_frequency(
            projections, generation
        )
        scores: dict[str, float] = {}
        for truth in truths:
            entry = projections[truth.entity_id]
            counts = entry.counts
            length = entry.length or 1
            score = 0.0
            for token in query:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                df = document_frequency.get(token, 0)
                idf = math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))
                denominator = frequency + BM25_K1 * (
                    1.0 - BM25_B + BM25_B * (length / (average or 1.0))
                )
                score += idf * (frequency * (BM25_K1 + 1.0)) / denominator
            scores[truth.entity_id] = score
        return scores

    def _semantic_scores_cached(
        self,
        truths: Sequence[CompiledEntityTruth],
        projections: Mapping[str, _RetrievalEntry],
        query: str,
    ) -> dict[str, float]:
        """Signal 3 over cached vectors -- identical scores to ``_semantic_scores``."""

        if not query or not projections:
            return {}
        query_vector, query_indices, query_values = _sparse_vector(query)
        width = len(query_vector)
        scores: dict[str, float] = {}
        for truth in truths:
            entry = projections[truth.entity_id]
            vector = entry.vector
            if vector is None or len(vector) != width:
                scores[truth.entity_id] = 0.0
                continue
            scores[truth.entity_id] = round(
                max(0.0, min(1.0, _sparse_cosine(query_indices, query_values, vector))), 6
            )
        return scores

    def _corpus(
        self,
        truths: Sequence[CompiledEntityTruth],
        events: Sequence[PostMortemEvent],
    ) -> dict[str, str]:
        lessons: dict[str, list[str]] = {}
        for event in events:
            for arm in event.entity_ids:
                lessons.setdefault(arm, []).append(event.lesson_summary)
        corpus: dict[str, str] = {}
        for truth in truths:
            parts = [
                truth.entity_id,
                truth.entity_type,
                " ".join(truth.known_failure_modes),
                " ".join(truth.caution_regimes),
                " ".join(lessons.get(truth.entity_id, ())),
            ]
            corpus[truth.entity_id] = " ".join(part for part in parts if part)
        return corpus

    def _bm25(self, corpus: Mapping[str, str], query: Sequence[str]) -> dict[str, float]:
        if not corpus or not query:
            return {}
        docs = {key: _tokens(text) for key, text in corpus.items()}
        lengths = {key: len(tokens) for key, tokens in docs.items()}
        average = (sum(lengths.values()) / len(lengths)) if lengths else 0.0
        document_frequency: dict[str, int] = {}
        for tokens in docs.values():
            for token in set(tokens):
                document_frequency[token] = document_frequency.get(token, 0) + 1
        total_docs = len(docs)
        scores: dict[str, float] = {}
        for key, tokens in docs.items():
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            length = lengths[key] or 1
            score = 0.0
            for token in query:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                df = document_frequency.get(token, 0)
                idf = math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))
                denominator = frequency + BM25_K1 * (
                    1.0 - BM25_B + BM25_B * (length / (average or 1.0))
                )
                score += idf * (frequency * (BM25_K1 + 1.0)) / denominator
            scores[key] = score
        return scores

    def _semantic_scores(
        self,
        corpus: Mapping[str, str],
        *,
        features: Mapping[str, str],
        query_text: str,
        embedder: Any = None,
    ) -> dict[str, float]:
        """Signal 3: cosine of the query and each entity corpus in embed space.

        The default embedder is the deterministic 384-dim hashed n-gram vector,
        so this arm is offline, reproducible and identical across processes.  An
        injected callable (or the opt-in Ollama hook) may replace it; a failing
        callable degrades to a zero score rather than raising.
        """

        query = " ".join(
            part
            for part in (str(query_text or ""), *(str(value) for value in features.values()))
            if part
        ).strip()
        if not query or not corpus:
            return {}
        embed = embedder if callable(embedder) else ollama_embedder()
        if embed is None:
            embed = _cached_hashed_embedding
        query_vector = self._embed(embed, query)
        if query_vector is None:
            return {}
        scores: dict[str, float] = {}
        for entity_id, document in corpus.items():
            vector = self._embed(embed, document)
            if vector is None or len(vector) != len(query_vector):
                scores[entity_id] = 0.0
                continue
            scores[entity_id] = round(
                max(0.0, min(1.0, cosine_similarity(query_vector, vector))), 6
            )
        return scores

    @staticmethod
    def _embed(embed: Any, text: str) -> tuple[float, ...] | None:
        try:
            vector = embed(text)
        except Exception:  # noqa: BLE001 - an embedder failure is never fatal
            return None
        if not vector:
            return None
        try:
            return tuple(float(value) for value in vector)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return None

    def _lessons(
        self,
        entity_ids: Sequence[str],
        events: Sequence[PostMortemEvent],
        limit: int,
    ) -> list[dict[str, Any]]:
        wanted = set(entity_ids)
        if not wanted:
            return []
        rows = [event for event in events if wanted.intersection(event.entity_ids)]
        rows.sort(key=lambda event: (-event.ts_utc, event.event_id))
        out: list[dict[str, Any]] = []
        for event in rows[: max(limit * 2, limit)]:
            payload = event.to_dict()
            payload["entities"] = list(event.entity_ids)
            out.append(payload)
        return out

    def _aggregate(self, truths: Sequence[CompiledEntityTruth]) -> dict[str, float]:
        samples = sum(truth.sample_count for truth in truths)
        if samples <= 0:
            return {
                "sample_count": 0.0,
                "win_rate_bps": 0.0,
                "avg_slippage_bps": 0.0,
                "worst_drawdown_bps": 0.0,
            }
        return {
            "sample_count": float(samples),
            "win_rate_bps": round(
                sum(truth.win_rate_bps * truth.sample_count for truth in truths) / samples, 6
            ),
            "avg_slippage_bps": round(
                sum(truth.avg_slippage_bps * truth.sample_count for truth in truths) / samples, 6
            ),
            "worst_drawdown_bps": round(
                sum(truth.worst_drawdown_bps * truth.sample_count for truth in truths) / samples, 6
            ),
        }

    def _penalty(
        self,
        truths: Sequence[CompiledEntityTruth],
        regime: str,
    ) -> PostMortemPenalty:
        modes = sorted({mode for truth in truths for mode in truth.known_failure_modes})
        regimes = sorted({row for truth in truths for row in truth.caution_regimes})
        active = str(regime or "").strip().upper()
        matched_regimes = tuple(row for row in regimes if active and row == active)
        matched_modes = tuple(modes)
        raw = REGIME_PENALTY * len(matched_regimes) + FAILURE_MODE_PENALTY * len(matched_modes)
        haircut = min(MAX_COMBINED_PENALTY, max(0.0, raw))
        multiplier = round(1.0 - haircut, 6)
        return PostMortemPenalty(
            matched_caution_regimes=matched_regimes,
            matched_failure_modes=matched_modes,
            confidence_multiplier=multiplier,
            sizing_multiplier=multiplier,
            applied=multiplier < 1.0,
        )

    def _seal(
        self,
        *,
        symbol: str,
        metrics: Mapping[str, float],
        truths: Sequence[CompiledEntityTruth],
        now_ns: int,
    ) -> GroundedFactPacket:
        knowledge_ns = max((truth.last_compiled_utc for truth in truths), default=now_ns)
        knowledge_ns = min(int(knowledge_ns), int(now_ns))
        attestation = _sha256(
            {
                "entity_ids": sorted(truth.entity_id for truth in truths),
                "truth_sha256": sorted(truth.truth_sha256 for truth in truths),
                "as_of_ns": int(now_ns),
            }
        )
        return GroundedFactPacket(
            symbol=symbol,
            valid_time_ns=int(now_ns),
            knowledge_ts_ns=knowledge_ns,
            as_of_ns=int(now_ns),
            metrics=dict(metrics),
            source_id="ring2.entity-memory.v1",
            retrieval_attestation=attestation,
        )

    # -- persistence -----------------------------------------------------

    @property
    def dirty(self) -> bool:
        with self._lock:
            return bool(self._dirty)

    def mark_clean(self) -> None:
        with self._lock:
            self._dirty = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": ENTITY_SCHEMA_VERSION,
                "sequence": self._sequence,
                "event_count": len(self._events) + int(self._sealed_seq),
                "active_event_count": len(self._events),
                "entity_count": len(self._truths),
                "timeline": [event.to_dict() for event in self._events],
                "truths": [truth.to_dict() for _, truth in sorted(self._truths.items())],
                "failure_modes": sorted(
                    {mode for truth in self._truths.values() for mode in truth.known_failure_modes}
                ),
                "caution_regimes": sorted(
                    {row for truth in self._truths.values() for row in truth.caution_regimes}
                ),
                # -- V2.3 PART 2.2: tier-2 checkpoint state -------------------
                "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
                "checkpoint": (
                    self._checkpoint.to_dict() if self._checkpoint is not None else None
                ),
                "checkpoint_seq": int(self._sealed_seq),
                "checkpoint_folds": {
                    alias: state.to_dict()
                    for alias, state in sorted(self._folds.items())
                },
                "cold_archive_tail": [
                    event.to_dict() for event in self._cold_archive[-STATE_ARCHIVE_TAIL:]
                ],
                "archive_events": len(self._cold_archive),
                "archive_path": str(self._archive_path) if self._archive_path else "",
                "compact_threshold": int(self._compact_threshold),
                "keep_recent": int(self._keep_recent),
            }

    def to_state(self) -> dict[str, Any]:
        return self.snapshot()

    def load_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(state, Mapping):
            return self.snapshot()
        events: list[PostMortemEvent] = []
        for row in state.get("timeline") or ():
            if not isinstance(row, Mapping):
                continue
            try:
                events.append(PostMortemEvent.from_dict(row))
            except (TypeError, ValueError):
                continue
        events.sort(key=lambda event: (event.ts_utc, event.event_id))

        truths: dict[str, CompiledEntityTruth] = {}
        for row in state.get("truths") or ():
            if not isinstance(row, Mapping):
                continue
            try:
                truth = CompiledEntityTruth.from_dict(row)
            except (TypeError, ValueError):
                continue
            truths[truth.entity_id] = truth

        # -- V2.3 PART 2.2: restore the sealed snapshot before the delta -----
        checkpoint: MemoryCheckpoint | None = None
        raw_checkpoint = state.get("checkpoint")
        if isinstance(raw_checkpoint, Mapping):
            try:
                candidate = MemoryCheckpoint.from_dict(raw_checkpoint)
                if candidate.verify():
                    checkpoint = candidate
            except (TypeError, ValueError):
                checkpoint = None

        folds: dict[str, _FoldState] = {}
        if checkpoint is not None:
            for alias, row in dict(state.get("checkpoint_folds") or {}).items():
                if not isinstance(row, Mapping):
                    continue
                try:
                    folds[str(alias)] = _FoldState.from_dict(row)
                except (TypeError, ValueError):
                    continue
            if not folds:
                # A checkpoint whose fold state is missing cannot be continued;
                # the hot tail is the only honest source left, so the seal is
                # dropped rather than silently redefining the entity's history.
                checkpoint = None

        archive: list[PostMortemEvent] = []
        for row in state.get("cold_archive_tail") or ():
            if not isinstance(row, Mapping):
                continue
            try:
                archive.append(PostMortemEvent.from_dict(row))
            except (TypeError, ValueError):
                continue

        with self._lock:
            self._events = events[-self._max_events :]
            self._truths = truths if checkpoint is None else {}
            self._checkpoint = checkpoint
            self._folds = folds if checkpoint is not None else {}
            self._sealed_seq = int(
                state.get("checkpoint_seq")
                or (checkpoint.checkpoint_seq if checkpoint is not None else 0)
            )
            self._cold_archive = archive
            self._sequence = int(state.get("sequence") or len(self._events))
            # V2.4 PART 2: the whole graph was just replaced, so every cached
            # projection belongs to a history that no longer exists.
            self.cache_clear()
            # The timeline is the source of truth; replay it so a tampered or
            # truncated cache can never outvote the raw events.  With a verified
            # checkpoint this walks only ``seq > checkpoint_seq``.
            self._recompile(None, fallback_stamp=time.time_ns())
            self._dirty = False
            return self.snapshot()

    def reset(self) -> dict[str, Any]:
        """Zero both layers and the sealed checkpoint. Used by clean slate."""

        with self._lock:
            self._events = []
            self._truths = {}
            self._folds = {}
            self._checkpoint = None
            self._sealed_seq = 0
            self._cold_archive = []
            self._sequence = 0
            self._dirty = True
            # V2.4 PART 2: a clean slate invalidates every cached projection.
            self.cache_clear()
            if self._archive_path is not None:
                try:
                    if self._archive_path.is_file():
                        self._archive_path.unlink()
                except OSError:  # pragma: no cover - defensive
                    pass
            return self.snapshot()

    # -- read-only surface ------------------------------------------------

    def describe(
        self,
        *,
        entity_id: str = "",
        query: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Flattened read-only view backing ``GET /api/v2/memory/entities``."""

        ceiling = max(1, min(int(limit), 500))
        needle = str(query or "").strip().lower()
        key = str(entity_id or "").strip()

        if key:
            truth = self.truth(key)
            rows = [truth.to_dict()] if truth is not None else []
        else:
            rows = [truth.to_dict() for truth in self.truths()]

        if needle:
            rows = [
                row
                for row in rows
                if needle in str(row.get("entity_id", "")).lower()
                or any(needle in str(mode).lower() for mode in row.get("known_failure_modes") or ())
                or any(needle in str(reg).lower() for reg in row.get("caution_regimes") or ())
            ]

        rows = rows[:ceiling]
        return {
            "entities": rows,
            "count": len(rows),
            "entity_id": key,
            "query": str(query or ""),
            "event_count": len(self._events) + int(self._sealed_seq),
            "active_event_count": len(self._events),
            "entity_count": len(self._truths),
            "checkpoint_seq": int(self._sealed_seq),
            "checkpoint_sha256": (
                self._checkpoint.checkpoint_sha256
                if self._checkpoint is not None
                else ""
            ),
            "archive_events": len(self._cold_archive),
            "archive_path": str(self._archive_path) if self._archive_path else "",
        }


#: Process-wide default used only when a caller asks for one. ``ControlPlane``
#: always injects its journal-backed instance so the state survives a restart.
_DEFAULT_LOCK = threading.RLock()
_DEFAULT_GRAPH: EntityMemoryGraph | None = None


def default_entity_graph() -> EntityMemoryGraph:
    global _DEFAULT_GRAPH
    with _DEFAULT_LOCK:
        if _DEFAULT_GRAPH is None:
            _DEFAULT_GRAPH = EntityMemoryGraph()
        return _DEFAULT_GRAPH
