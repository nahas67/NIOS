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
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from core.schemas import GroundedFactPacket

__all__ = [
    "ADVERSE_OUTCOME_KINDS",
    "ENTITY_KINDS",
    "OUTCOME_KINDS",
    "CompiledEntityTruth",
    "EntityMemoryGraph",
    "PostMortemEvent",
    "PostMortemPenalty",
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

ENTITY_SCHEMA_VERSION = "v1.9.entity-memory"

_FORBIDDEN_TOKENS = ("/", "\\", "\x00", "..")

#: Retrieval fusion. Feature overlap dominates because "this is the symbol I am
#: about to trade" is a stronger signal than a word appearing in a lesson.
KIND_WEIGHTS: Mapping[str, float] = {
    "symbol": 0.35,
    "silo": 0.20,
    "strategy": 0.20,
    "regime": 0.15,
    "macro": 0.10,
}
FEATURE_WEIGHT = 0.70
LEXICAL_WEIGHT = 0.30
BM25_K1 = 1.5
BM25_B = 0.75

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
# the graph
# ---------------------------------------------------------------------------


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
    ) -> None:
        self._lock = threading.RLock()
        self._max_events = max(64, int(max_events))
        self._events: list[PostMortemEvent] = []
        self._truths: dict[str, CompiledEntityTruth] = {}
        self._sequence = 0
        self._dirty = False
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
                ("symbol", str(symbol or "").strip().upper()),
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
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events :]
            self._dirty = True
            touched = self._recompile(event.entity_ids, fallback_stamp=stamp)
            return {
                "event": event.to_dict(),
                "compiled": {key: truth.to_dict() for key, truth in sorted(touched.items())},
                "timeline_size": len(self._events),
                "entity_count": len(self._truths),
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
            return self._truths.get(key)

    def _recompile(
        self,
        entity_ids: Iterable[str] | None,
        *,
        fallback_stamp: int,
    ) -> dict[str, CompiledEntityTruth]:
        if entity_ids is None:
            targets = sorted(
                {arm for event in self._events for arm in event.entity_ids} | set(self._truths)
            )
        else:
            targets = sorted({str(row) for row in entity_ids})
        touched: dict[str, CompiledEntityTruth] = {}
        for entity_id in targets:
            truth = self._compile(entity_id, fallback_stamp=fallback_stamp)
            if truth.sample_count <= 0:
                self._truths.pop(entity_id, None)
                continue
            self._truths[entity_id] = truth
            touched[entity_id] = truth
        return touched

    def _compile(self, entity_id: str, *, fallback_stamp: int) -> CompiledEntityTruth:
        rows = sorted(
            (event for event in self._events if entity_id in event.entity_ids),
            key=lambda event: (event.ts_utc, event.event_id),
        )
        samples = len(rows)
        wins = sum(1 for event in rows if event.outcome_kind == "WIN")
        win_rate_bps = (wins / samples) * 10_000.0 if samples else 0.0
        avg_slippage_bps = (
            sum(event.slippage_bps for event in rows) / samples if samples else 0.0
        )

        running = 0
        peak = 0
        trough = 0
        worst = 0
        for event in rows:
            running += event.net_pnl_micros
            trough = min(trough, running)
            peak = max(peak, running)
            worst = max(worst, peak - running)
        reference = max(peak, abs(trough), 1)
        worst_drawdown_bps = max(0.0, min(10_000.0, (worst / reference) * 10_000.0))

        modes: set[str] = set()
        regimes: set[str] = set()
        for event in rows:
            mode = FAILURE_MODE_BY_OUTCOME.get(event.outcome_kind)
            if mode:
                modes.add(mode)
            if abs(event.slippage_bps) >= HIGH_SLIPPAGE_BPS:
                modes.add(HIGH_SLIPPAGE_MODE)
            if event.outcome_kind in ADVERSE_OUTCOME_KINDS:
                for arm in event.entity_ids:
                    kind, value = _split_entity(arm)
                    if kind == "regime" and value:
                        regimes.add(value.upper())

        watermark = max((event.ts_utc for event in rows), default=int(fallback_stamp))
        return CompiledEntityTruth(
            entity_id=entity_id,
            entity_type=ENTITY_TYPE_BY_KIND.get(_split_entity(entity_id)[0], "UNKNOWN"),
            sample_count=samples,
            win_rate_bps=round(win_rate_bps, 6),
            avg_slippage_bps=round(avg_slippage_bps, 6),
            worst_drawdown_bps=round(worst_drawdown_bps, 6),
            known_failure_modes=tuple(sorted(modes)[:MAX_FAILURE_MODES]),
            caution_regimes=tuple(sorted(regimes)[:MAX_CAUTION_REGIMES]),
            last_compiled_utc=int(watermark),
        )

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
    ) -> dict[str, Any]:
        """Hybrid lexical + feature retrieval that seals its numbers.

        Returns the ranked compiled truths, the past lessons that produced them,
        the deterministic penalty implied by the match, and a verified
        ``GroundedFactPacket`` carrying the four retrieved metrics plus the
        confidence multiplier.
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
        target_ids = {_entity_id(kind, value) for kind, value in active.items()}

        with self._lock:
            truth_rows = list(self._truths.values())
            events = list(self._events)

        lexical = self._bm25(self._corpus(truth_rows, events), _tokens(query_text))
        ceiling = max(lexical.values()) if lexical else 0.0

        scored: list[tuple[float, CompiledEntityTruth]] = []
        for truth in truth_rows:
            feature_score = (
                KIND_WEIGHTS[_split_entity(truth.entity_id)[0]] / total_weight
                if truth.entity_id in target_ids
                else 0.0
            )
            lexical_score = (lexical.get(truth.entity_id, 0.0) / ceiling) if ceiling > 0 else 0.0
            score = FEATURE_WEIGHT * feature_score + LEXICAL_WEIGHT * lexical_score
            if not active and not query_text.strip():
                score = 1.0
            elif score <= 0.0:
                continue
            scored.append((score, truth))

        scored.sort(key=lambda row: (-row[0], -row[1].sample_count, row[1].entity_id))
        selected = scored[:limit]
        truths = [truth for _, truth in selected]
        entity_ids = [truth.entity_id for truth in truths]

        lessons = self._lessons(entity_ids, events, limit)
        penalty = self._penalty(truths, features["regime"])
        metrics = self._aggregate(truths)
        metrics["post_mortem_confidence_multiplier"] = penalty.confidence_multiplier

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
                {**truth.to_dict(), "score": round(score, 6)} for score, truth in selected
            ],
            "lessons": lessons,
            "metrics": dict(metrics),
            "penalty": penalty.to_dict(),
            "sample_count": int(metrics["sample_count"]),
            "packet": packet.to_dict(),
            "packet_sha256": packet.sha256_digest,
            "as_of_utc_ns": stamp,
            "timeline_size": len(events),
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
                "event_count": len(self._events),
                "entity_count": len(self._truths),
                "timeline": [event.to_dict() for event in self._events],
                "truths": [truth.to_dict() for _, truth in sorted(self._truths.items())],
                "failure_modes": sorted(
                    {mode for truth in self._truths.values() for mode in truth.known_failure_modes}
                ),
                "caution_regimes": sorted(
                    {row for truth in self._truths.values() for row in truth.caution_regimes}
                ),
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

        with self._lock:
            self._events = events[-self._max_events :]
            self._truths = truths
            self._sequence = int(state.get("sequence") or len(self._events))
            # The timeline is the source of truth; replay it so a tampered or
            # truncated cache can never outvote the raw events.
            self._recompile(None, fallback_stamp=time.time_ns())
            self._dirty = False
            return self.snapshot()

    def reset(self) -> dict[str, Any]:
        """Zero both layers. Used by the clean-slate reset funnel."""

        with self._lock:
            self._events = []
            self._truths = {}
            self._sequence = 0
            self._dirty = True
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
            "event_count": len(self._events),
            "entity_count": len(self._truths),
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
