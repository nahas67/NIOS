"""Episodic operator memory (V2.3 PART 2.3, tier 3).

The entity graph remembers what the *market* did to an instrument.  This module
remembers what the *operator* did: an inline ``plannotator`` mission override, a
revocation, a manual tranche rearm, a ``clear-freeze`` approval or an autopilot
pause/play.  Those interventions are the strongest evidence the desk has about
its own risk appetite, and until now they vanished with the request.

Two rules shape the design:

* **The record is immutable and self-sealing.**  Every episode carries the
  PG16 ``nios_episodic_memory`` shape (``episode_id``, ``ts_utc``, ``actor``,
  ``action_kind``, ``regime``, ``context_tags``, ``decision_summary``,
  ``payload_sha256``) and its own SHA-256 commitment, so a stored decision can
  never be quietly re-worded.
* **The narrative is digit-free.**  A caution injected into an agent prompt is a
  sentence, not a spreadsheet: every figure rides a ``GroundedFactPacket`` and
  the prose deliberately carries none (``sec_018`` Law 3).

Ring containment: Ring 2 only.  This module imports ``core.schemas`` and the
standard library; it holds no broker credential, opens no exchange socket and
signs nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from core.schemas import GroundedFactPacket

__all__ = [
    "ACTION_KINDS",
    "ADVERSE_ACTION_KINDS",
    "EPISODE_SCHEMA_VERSION",
    "MAX_EPISODES",
    "EpisodicEpisode",
    "EpisodicMemoryStore",
    "default_episodic_store",
    "digitless",
]

EPISODE_SCHEMA_VERSION = "v2.3.episodic-memory"
MAX_EPISODES = 2_000
MAX_SUMMARY_CHARS = 512
MAX_TAGS = 32
MAX_CAUTIONS_PER_QUERY = 8

#: Closed vocabulary of operator interventions this store records.
ACTION_KINDS: tuple[str, ...] = (
    "MISSION_OVERRIDE",
    "MISSION_REVOKE",
    "TRANCHE_REARM",
    "CLEAR_FREEZE",
    "AUTOPILOT_PAUSE",
    "AUTOPILOT_PLAY",
)

#: Interventions that tighten risk.  They sort ahead of the rest in a caution.
ADVERSE_ACTION_KINDS: frozenset[str] = frozenset(
    {"MISSION_REVOKE", "CLEAR_FREEZE", "AUTOPILOT_PAUSE"}
)

_DIGITS = re.compile(r"[0-9]")
_SPACES = re.compile(r"\s+")


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def digitless(text: Any) -> str:
    """Return ``text`` with every digit removed and whitespace collapsed.

    The injected caution is read by a language model that is forbidden to cite an
    ungrounded figure, so the safest sentence is one that contains no figure at
    all.  Numbers belong in the sealed packet beside it.
    """

    stripped = _DIGITS.sub("", str(text or ""))
    return _SPACES.sub(" ", stripped).strip()


def _iso_utc(ns: int) -> str:
    stamp = datetime.fromtimestamp(max(0, int(ns)) / 1e9, tz=timezone.utc)
    return stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ns_of(value: Any) -> int:
    """Parse an ISO-8601 UTC string back to integer nanoseconds (0 on failure)."""

    token = str(value or "").strip()
    if not token:
        return 0
    if token.endswith("Z"):
        token = f"{token[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1e9)


# ---------------------------------------------------------------------------
# one immutable episode
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class EpisodicEpisode:
    """One operator decision, sealed and immutably committed.

    The field set mirrors the PG16 ``nios_episodic_memory`` table exactly so the
    same row can be written to the WAL store, mirrored to PostgreSQL and served
    by the REST surface without a translation layer.
    """

    episode_id: str
    ts_utc: str
    actor: str
    action_kind: str
    regime: str = "NEUTRAL"
    context_tags: Mapping[str, Any] = field(default_factory=dict)
    decision_summary: str = ""
    payload_sha256: str = ""
    #: Derived from ``ts_utc`` for ordering and packet knowledge time.  It is
    #: deliberately excluded from the commitment so the stored digest depends
    #: only on the recorded decision.
    ts_ns: int = 0

    def __post_init__(self) -> None:
        episode_id = str(self.episode_id or "").strip()
        if not episode_id:
            raise ValueError("episode_id must be a non-empty string")
        object.__setattr__(self, "episode_id", episode_id)

        stamp = str(self.ts_utc or "").strip()
        if _ns_of(stamp) <= 0:
            raise ValueError("ts_utc must be an ISO-8601 UTC timestamp")
        object.__setattr__(self, "ts_utc", stamp)

        actor = str(self.actor or "").strip() or "operator"
        object.__setattr__(self, "actor", actor)

        kind = str(self.action_kind or "").strip().upper()
        if kind not in ACTION_KINDS:
            raise ValueError(f"action_kind must be one of {ACTION_KINDS}")
        object.__setattr__(self, "action_kind", kind)

        object.__setattr__(
            self, "regime", (str(self.regime or "NEUTRAL").strip().upper() or "NEUTRAL")
        )

        tags = self.context_tags if isinstance(self.context_tags, Mapping) else {}
        normalized: dict[str, Any] = {}
        for key, value in list(tags.items())[:MAX_TAGS]:
            name = str(key).strip()
            if not name:
                continue
            normalized[name] = value
        object.__setattr__(self, "context_tags", normalized)

        object.__setattr__(
            self,
            "decision_summary",
            str(self.decision_summary or "")[:MAX_SUMMARY_CHARS],
        )

        digest = _sha256(self._canonical_payload())
        supplied = str(self.payload_sha256 or "").strip()
        if supplied and supplied != digest:
            raise ValueError("payload_sha256 does not match the episode payload")
        object.__setattr__(self, "payload_sha256", digest)

        ts_ns = int(self.ts_ns) if int(self.ts_ns) > 0 else _ns_of(self.ts_utc)
        object.__setattr__(self, "ts_ns", ts_ns)

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "ts_utc": self.ts_utc,
            "actor": self.actor,
            "action_kind": self.action_kind,
            "regime": self.regime,
            "context_tags": dict(sorted(self.context_tags.items())),
            "decision_summary": self.decision_summary,
        }

    def verify(self) -> bool:
        return self.payload_sha256 == _sha256(self._canonical_payload())

    def to_dict(self) -> dict[str, Any]:
        """The PG16 ``nios_episodic_memory`` row shape, and nothing else.

        ``ts_ns`` stays a derived dataclass attribute: ``ts_utc`` is the stored
        timestamp, so a row can be written to the WAL store, mirrored to
        PostgreSQL and served by REST without a translation layer.
        """

        payload = self._canonical_payload()
        payload["payload_sha256"] = self.payload_sha256
        return payload

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "EpisodicEpisode":
        if not isinstance(row, Mapping):
            raise TypeError("episode row must be a mapping")
        return cls(
            episode_id=str(row.get("episode_id") or ""),
            ts_utc=str(row.get("ts_utc") or ""),
            actor=str(row.get("actor") or ""),
            action_kind=str(row.get("action_kind") or ""),
            regime=str(row.get("regime") or "NEUTRAL"),
            context_tags=row.get("context_tags") or {},
            decision_summary=str(row.get("decision_summary") or ""),
            payload_sha256=str(row.get("payload_sha256") or ""),
            ts_ns=int(row.get("ts_ns") or 0),
        )


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class EpisodicMemoryStore:
    """Bounded, thread-safe, restart-safe log of operator interventions.

    The store is intentionally narrow: it records decisions, retrieves the ones
    that match a prospective trade, and seals their numeric summary into a
    ``GroundedFactPacket``.  It never places an order, never sizes a position and
    never overrides a control endpoint -- a memory failure degrades to "no
    caution", never to a blocked operator action.
    """

    def __init__(
        self,
        state: Mapping[str, Any] | None = None,
        *,
        max_episodes: int = MAX_EPISODES,
    ) -> None:
        self._lock = threading.RLock()
        self._max_episodes = max(16, int(max_episodes))
        self._episodes: list[EpisodicEpisode] = []
        self._sequence = 0
        self._dirty = False
        if isinstance(state, Mapping):
            self.load_state(state)

    # -- recording -------------------------------------------------------

    def record(
        self,
        *,
        action_kind: str,
        actor: str = "operator",
        decision_summary: str = "",
        regime: str = "",
        context_tags: Mapping[str, Any] | None = None,
        ts_utc: str | None = None,
        now_ns: int | None = None,
        episode_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one immutable episode and return it as a dictionary."""

        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            self._sequence += 1
            episode = EpisodicEpisode(
                episode_id=str(
                    episode_id
                    or f"EP-{stamp}-{self._sequence:06d}"
                ),
                ts_utc=str(ts_utc or _iso_utc(stamp)),
                actor=str(actor or "operator"),
                action_kind=action_kind,
                regime=str(regime or "NEUTRAL"),
                context_tags=dict(context_tags or {}),
                decision_summary=decision_summary,
            )
            self._episodes.append(episode)
            self._episodes.sort(key=lambda row: (row.ts_ns, row.episode_id))
            if len(self._episodes) > self._max_episodes:
                self._episodes = self._episodes[-self._max_episodes :]
            self._dirty = True
            return episode.to_dict()

    def record_mission_override(
        self,
        *,
        mission_id: str,
        actor: str = "operator",
        tranche_caps_micros: Mapping[str, int] | None = None,
        excluded_symbols: Sequence[str] | None = None,
        reserve_split_override_bps: int | None = None,
        operator_note: str = "",
        revision: int | None = None,
        approval_state: str = "",
        regime: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """``plannotator`` inline mission override at the plan-exit gate."""

        caps = {
            str(key): int(value)
            for key, value in dict(tranche_caps_micros or {}).items()
            if str(key).strip()
        }
        excluded = sorted(
            {str(item).strip().upper() for item in (excluded_symbols or ()) if str(item).strip()}
        )
        note = str(operator_note or "")[:MAX_SUMMARY_CHARS]
        parts: list[str] = [f"mission {mission_id} revised at the plan-exit gate"]
        if caps:
            parts.append(f"{len(caps)} tranche caps tightened")
        if excluded:
            parts.append(f"{len(excluded)} symbols excluded")
        if reserve_split_override_bps is not None:
            parts.append("reserve split overridden")
        if note:
            parts.append(f"note: {note}")
        return self.record(
            action_kind="MISSION_OVERRIDE",
            actor=actor,
            decision_summary="; ".join(parts),
            regime=regime,
            context_tags={
                "mission_id": str(mission_id),
                "revision": int(revision) if revision is not None else None,
                "approval_state": str(approval_state or ""),
                "tranche_caps_micros": caps,
                "excluded_symbols": excluded,
                "reserve_split_override_bps": (
                    int(reserve_split_override_bps)
                    if reserve_split_override_bps is not None
                    else None
                ),
                "operator_note": note,
                "tags": ["mission_override", *excluded],
            },
            now_ns=now_ns,
        )

    def record_mission_revoke(
        self,
        *,
        mission_id: str,
        actor: str = "operator",
        reason: str = "",
        regime: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Mission revocation -- the strongest risk-tightening signal on record."""

        detail = str(reason or "")[:MAX_SUMMARY_CHARS]
        return self.record(
            action_kind="MISSION_REVOKE",
            actor=actor,
            decision_summary=(
                f"mission {mission_id} revoked" + (f": {detail}" if detail else "")
            ),
            regime=regime,
            context_tags={
                "mission_id": str(mission_id),
                "reason": detail,
                "tags": ["mission_revoke", str(mission_id)],
            },
            now_ns=now_ns,
        )

    def record_tranche_rearm(
        self,
        *,
        tranche_id: str,
        actor: str = "operator",
        mission_id: str = "",
        rearmed: bool = True,
        regime: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Manual tranche rearm -- a deliberate re-risk of a halted tranche."""

        return self.record(
            action_kind="TRANCHE_REARM",
            actor=actor,
            decision_summary=(
                f"tranche {tranche_id} manually rearmed"
                if rearmed
                else f"tranche {tranche_id} rearm requested (no state change)"
            ),
            regime=regime,
            context_tags={
                "tranche_id": str(tranche_id),
                "mission_id": str(mission_id or ""),
                "rearmed": bool(rearmed),
                "tags": ["tranche_rearm", str(tranche_id)],
            },
            now_ns=now_ns,
        )

    def record_clear_freeze(
        self,
        *,
        silo: str,
        actor: str = "operator",
        note: str = "",
        previous_reason: str = "",
        cleared: bool = True,
        regime: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """``ColdStartReconciler`` ``clear-freeze`` approval."""

        detail = str(note or "")[:MAX_SUMMARY_CHARS]
        return self.record(
            action_kind="CLEAR_FREEZE",
            actor=actor,
            decision_summary=(
                f"reconciliation freeze on {silo} cleared by the operator"
                + (f": {detail}" if detail else "")
            ),
            regime=regime,
            context_tags={
                "silo": str(silo).strip().upper(),
                "previous_reason": str(previous_reason or ""),
                "note": detail,
                "cleared": bool(cleared),
                "tags": ["clear_freeze", str(silo).strip().upper()],
            },
            now_ns=now_ns,
        )

    def record_autopilot(
        self,
        *,
        enabled: bool,
        actor: str = "operator",
        regime: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Autopilot pause/play intervention."""

        return self.record(
            action_kind="AUTOPILOT_PLAY" if enabled else "AUTOPILOT_PAUSE",
            actor=actor,
            decision_summary=(
                "autopilot resumed by the operator"
                if enabled
                else "autopilot paused by the operator"
            ),
            regime=regime,
            context_tags={
                "enabled": bool(enabled),
                "tags": ["autopilot", "play" if enabled else "pause"],
            },
            now_ns=now_ns,
        )

    # -- reading ---------------------------------------------------------

    def episodes(
        self,
        *,
        action_kind: str = "",
        query: str = "",
        limit: int = 50,
    ) -> tuple[dict[str, Any], ...]:
        """Newest-first read view, filtered by action kind and free text."""

        ceiling = max(1, min(int(limit or 50), 500))
        kind = str(action_kind or "").strip().upper()
        needle = str(query or "").strip().lower()
        with self._lock:
            rows = list(self._episodes)
        if kind and kind != "ALL":
            rows = [row for row in rows if row.action_kind == kind]
        if needle:
            rows = [
                row
                for row in rows
                if needle in row.decision_summary.lower()
                or needle in row.actor.lower()
                or needle in row.regime.lower()
                or any(needle in str(tag).lower() for tag in row.context_tags.get("tags") or ())
            ]
        rows.sort(key=lambda row: (row.ts_ns, row.episode_id), reverse=True)
        return tuple(row.to_dict() for row in rows[:ceiling])

    def describe(
        self,
        *,
        action_kind: str = "",
        query: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Flattened payload backing ``GET /api/v2/memory/episodes``."""

        rows = self.episodes(action_kind=action_kind, query=query, limit=limit)
        counts: dict[str, int] = {}
        with self._lock:
            for row in self._episodes:
                counts[row.action_kind] = counts.get(row.action_kind, 0) + 1
            total = len(self._episodes)
        return {
            "episodes": [dict(row) for row in rows],
            "count": len(rows),
            "total": int(total),
            "action_kind": str(action_kind or ""),
            "query": str(query or ""),
            "limit": int(max(1, min(int(limit or 50), 500))),
            "counts_by_action": dict(sorted(counts.items())),
            "action_kinds": list(ACTION_KINDS),
            # ``payload_envelope.enclose`` owns the ``schema_version`` key, so the
            # episode schema is reported under its own name to survive the wrap.
            "episode_schema_version": EPISODE_SCHEMA_VERSION,
        }

    # -- cautions --------------------------------------------------------

    def _matching_episodes(
        self,
        *,
        symbol: str = "",
        silo: str = "",
        strategy_id: str = "",
        regime: str = "",
        query_text: str = "",
        top_k: int = 3,
    ) -> list[EpisodicEpisode]:
        ceiling = max(1, min(int(top_k or 3), MAX_CAUTIONS_PER_QUERY))
        symbol_key = str(symbol or "").strip().upper()
        silo_key = str(silo or "").strip().upper()
        strategy_key = str(strategy_id or "").strip().upper()
        regime_key = str(regime or "").strip().upper()
        needle = str(query_text or "").strip().lower()

        scored: list[tuple[int, int, EpisodicEpisode]] = []
        with self._lock:
            rows = list(self._episodes)
        for row in rows:
            tags = [str(tag).upper() for tag in row.context_tags.get("tags") or ()]
            # Relevance first: a caution must actually be about this entity.  An
            # adverse action kind only sharpens a match, it never creates one --
            # otherwise every pause on the desk would warn about every symbol.
            relevance = 0
            if symbol_key and (
                symbol_key in tags
                or symbol_key in row.decision_summary.upper()
            ):
                relevance += 3
            if silo_key and (
                silo_key == str(row.context_tags.get("silo", "")).upper()
                or silo_key in tags
            ):
                relevance += 2
            if strategy_key and strategy_key in row.decision_summary.upper():
                relevance += 1
            if regime_key and regime_key == row.regime:
                relevance += 2
            if needle and needle in row.decision_summary.lower():
                relevance += 1
            if relevance <= 0:
                continue
            score = relevance + (
                1 if row.action_kind in ADVERSE_ACTION_KINDS else 0
            )
            scored.append((-score, -row.ts_ns, row))
        scored.sort(key=lambda entry: (entry[0], entry[1], entry[2].episode_id))
        return [entry[2] for entry in scored[:ceiling]]

    def cautions_for(
        self,
        *,
        symbol: str = "",
        silo: str = "",
        strategy_id: str = "",
        regime: str = "",
        query_text: str = "",
        top_k: int = 3,
        now_ns: int | None = None,
    ) -> dict[str, Any] | None:
        """Digit-free caution narrative plus a sealed packet.

        Returns ``None`` when nothing matches: an empty memory must degrade to
        "no caution", never to an invented one.
        """

        matches = self._matching_episodes(
            symbol=symbol,
            silo=silo,
            strategy_id=strategy_id,
            regime=regime,
            query_text=query_text,
            top_k=top_k,
        )
        if not matches:
            return None

        stamp = int(now_ns if now_ns is not None else time.time_ns())
        newest = max(row.ts_ns for row in matches)
        knowledge_ns = min(int(newest), stamp)
        counts: dict[str, float] = {}
        for row in matches:
            counts[row.action_kind] = counts.get(row.action_kind, 0.0) + 1.0

        metrics: dict[str, float] = {
            "episode_matches": float(len(matches)),
            "episode_age_s": round(max(0.0, (stamp - knowledge_ns) / 1e9), 6),
        }
        for kind in ACTION_KINDS:
            metrics[f"episodes_{kind.lower()}"] = float(counts.get(kind, 0.0))

        kinds = sorted({row.action_kind for row in matches})
        narrative = digitless(
            "Operator cautions on record for this entity: "
            + ", ".join(kind.replace("_", " ").lower() for kind in kinds)
            + ". The desk has already intervened on this name; treat the setup as "
            + "advisory rather than fresh, and re-read the sealed packet before "
            + "sizing anything."
        )
        latest_summary = digitless(matches[0].decision_summary)
        if latest_summary:
            narrative = f"{narrative} Most recent: {latest_summary}."

        packet = GroundedFactPacket(
            symbol=str(symbol or "UNIVERSE").strip().upper() or "UNIVERSE",
            valid_time_ns=stamp,
            knowledge_ts_ns=knowledge_ns,
            as_of_ns=stamp,
            metrics=metrics,
            source_id="ring2.episodic-memory.v1",
            retrieval_attestation=_sha256(
                {
                    "episode_ids": sorted(row.episode_id for row in matches),
                    "payload_sha256": sorted(row.payload_sha256 for row in matches),
                    "as_of_ns": stamp,
                }
            ),
        )
        return {
            "narrative": narrative,
            "episode_ids": [row.episode_id for row in matches],
            "action_kinds": kinds,
            "metrics": dict(metrics),
            "packet": packet.to_dict(),
            "packet_sha256": packet.sha256_digest,
            "as_of_utc_ns": stamp,
            "digit_free": digitless(narrative) == narrative,
        }

    def inject_cautions(self, dispatcher: Any, **query: Any) -> dict[str, Any] | None:
        """Attach this store to ``dispatcher`` and return the matching cautions.

        Wiring is best effort: a dispatcher that refuses the attribute (a frozen
        proxy, a stand-in in a test) must not break the caller.  The dispatcher
        then pulls cautions per task through ``cautions_for(...)`` before a
        proposal is sealed.
        """

        try:
            setattr(dispatcher, "episodic_memory", self)
        except Exception:  # noqa: BLE001 - attachment is best effort
            pass
        try:
            return self.cautions_for(**query) if query else None
        except Exception:  # noqa: BLE001 - memory must never fail a proposal
            return None

    # -- retention -------------------------------------------------------

    def compact(self, *, keep_recent: int | None = None) -> dict[str, Any]:
        """Trim the log to the bounded hot window (episodes are not foldable)."""

        with self._lock:
            ceiling = (
                self._max_episodes
                if keep_recent is None
                else max(16, int(keep_recent))
            )
            before = len(self._episodes)
            if before > ceiling:
                self._episodes = self._episodes[-ceiling:]
                self._dirty = True
            return {
                "compacted": before > ceiling,
                "trimmed_episodes": max(0, before - ceiling),
                "remaining_episodes": len(self._episodes),
                "max_episodes": int(self._max_episodes),
            }

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
                "schema_version": EPISODE_SCHEMA_VERSION,
                "sequence": int(self._sequence),
                "episode_count": len(self._episodes),
                "episodes": [row.to_dict() for row in self._episodes],
            }

    def to_state(self) -> dict[str, Any]:
        return self.snapshot()

    def load_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(state, Mapping):
            return self.snapshot()
        rows: list[EpisodicEpisode] = []
        for row in state.get("episodes") or ():
            if not isinstance(row, Mapping):
                continue
            try:
                rows.append(EpisodicEpisode.from_dict(row))
            except (TypeError, ValueError):
                continue
        rows.sort(key=lambda row: (row.ts_ns, row.episode_id))
        with self._lock:
            self._episodes = rows[-self._max_episodes :]
            self._sequence = int(state.get("sequence") or len(self._episodes))
            self._dirty = False
            return self.snapshot()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._episodes = []
            self._sequence = 0
            self._dirty = True
            return self.snapshot()

    def __len__(self) -> int:
        with self._lock:
            return len(self._episodes)


_DEFAULT_LOCK = threading.RLock()
_DEFAULT_STORE: EpisodicMemoryStore | None = None


def default_episodic_store() -> EpisodicMemoryStore:
    """Process-wide default, used only by callers that do not inject one."""

    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = EpisodicMemoryStore()
        return _DEFAULT_STORE
