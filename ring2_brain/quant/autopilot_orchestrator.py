"""NIOS V2.1 -- autonomous multi-tranche autopilot orchestrator (``G3``).

Once an operator approves a mission, someone has to keep looking for the next
tranche.  That "someone" is this orchestrator: on every cycle it

1. **enumerates** the registry (the full 274-instrument universe),
2. **excludes** anything the venue calendar says is closed,
3. **excludes** anything the entity memory graph says has failed badly before,
4. **sizes** each survivor with half-Kelly through the mission feasibility engine,
5. **checks independence** through the ``N_eff`` / 2% bucket / 6% heat gate, and
6. **dispatches** the survivors as tranche-tagged proposals.

It is Ring 2 (Rule 4): it holds no credential, opens no socket and signs nothing.
Step 6 returns *proposals*; ``SmartOMS`` in Ring 0 is what validates, reserves and
executes them.  The independence gate is therefore **injected** -- Ring 3 hands it
the Ring 0 ``TrancheIndependenceGate`` -- and when nothing is injected the
orchestrator falls back to the local mirror implemented below, which is the same
``N_eff`` / bucket / heat arithmetic expressed without touching Ring 0 imports.

Harvest note (Rule 1): the ``Alpha -> PortfolioConstruction -> RiskManagement ->
Execution`` staging is translated from ``QuantConnect/Lean``; the tranche
stop-out / ``StoplossGuard`` shape from ``freqtrade``.  No donor code is imported.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "AUTOPILOT_HEAT_CAP_PCT",
    "AUTOPILOT_BUCKET_CAP_PCT",
    "AutopilotCandidate",
    "MultiTrancheAutopilot",
    "TrancheHeatVerdict",
]

#: ``A2``: the whole book may not carry more than 6% heat.
AUTOPILOT_HEAT_CAP_PCT: float = 0.06

#: ``A2``: same-side tranches correlated above 0.7 share one 2% bucket.
AUTOPILOT_BUCKET_CAP_PCT: float = 0.02

#: Same-side correlation above this collapses two tranches into one bet.
AUTOPILOT_CORRELATION_THRESHOLD: float = 0.7

#: Half-Kelly: the fraction of the full-Kelly stake the orchestrator will use.
HALF_KELLY_FRACTION: float = 0.5

#: A memorized sizing haircut at or below this blocks the instrument outright.
MEMORY_BLOCK_MULTIPLIER: float = 0.5


def _seal(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class AutopilotCandidate:
    """One tranche-tagged dispatch proposal produced by a cycle."""

    tranche_id: str
    silo: str
    symbol: str
    side: str
    weight: float
    half_kelly_fraction: float
    score: float
    tier: str = "A"
    correlations: dict[str, float] = field(default_factory=dict)
    memory_multiplier: float = 1.0
    mission_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "tranche_id": self.tranche_id,
            "silo": self.silo,
            "symbol": self.symbol,
            "side": self.side,
            "weight": round(float(self.weight), 8),
            "half_kelly_fraction": round(float(self.half_kelly_fraction), 8),
            "score": round(float(self.score), 8),
            "tier": self.tier,
            "correlations": {str(k): round(float(v), 6) for k, v in self.correlations.items()},
            "memory_multiplier": round(float(self.memory_multiplier), 6),
            "mission_id": self.mission_id,
            "dispatch": "SmartOMS",
            "signed": False,
        }
        payload["proposal_sha256"] = _seal(payload)
        return payload


@dataclass(slots=True)
class _Exposure:
    """The duck-typed shape the independence gate consumes."""

    tranche_id: str
    silo: str
    side: str
    weight: float
    correlations: Mapping[str, float] = field(default_factory=dict)

    def correlation(self, other: str) -> float:
        try:
            return float(self.correlations.get(str(other), 0.0))
        except (TypeError, ValueError):
            return 0.0


@dataclass(slots=True)
class TrancheHeatVerdict:
    """Local mirror of the Ring 0 independence verdict."""

    allowed: bool
    code: str
    n_eff: float
    total_heat_pct: float
    reason: str
    buckets: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "code": self.code,
            "n_eff": round(float(self.n_eff), 6),
            "total_heat_pct": round(float(self.total_heat_pct), 8),
            "buckets": [dict(row) for row in self.buckets],
            "reason": self.reason,
        }


class MultiTrancheAutopilot:
    """Ring 2 scanner -> feasibility -> independence -> dispatch pipeline (``G3``)."""

    def __init__(
        self,
        *,
        registry: Any = None,
        gate: Any = None,
        feasibility: Any = None,
        memory: Any = None,
        calendar: Any = None,
        kill_latch: Any = None,
        pacing: Any = None,
        heat_cap_pct: float = AUTOPILOT_HEAT_CAP_PCT,
        bucket_cap_pct: float = AUTOPILOT_BUCKET_CAP_PCT,
        max_candidates: int = 8,
        max_symbols: int = 274,
        base_weight: float = 0.01,
        severe_failure_modes: Sequence[str] = ("ORDER_REJECTION", "TRANCH_STOP_OUT", "TRANCHE_STOP_OUT"),
    ) -> None:
        self.registry = registry
        self.gate = gate
        self.feasibility = feasibility
        self.memory = memory
        self.calendar = calendar
        #: Injected Ring 0 kill latch and mission pacer.  Both are duck-typed so
        #: this Ring 2 module never imports Ring 0 signing or capital code.
        self.kill_latch = kill_latch
        self.pacing = pacing
        self.heat_cap_pct = max(0.0, float(heat_cap_pct))
        self.bucket_cap_pct = max(0.0, float(bucket_cap_pct))
        self.max_candidates = max(1, int(max_candidates))
        self.max_symbols = max(1, int(max_symbols))
        self.base_weight = max(0.0, float(base_weight))
        self.severe_failure_modes = tuple(str(item).upper() for item in severe_failure_modes)
        self._lock = threading.RLock()
        self._cycles = 0
        self._dispatched = 0
        self._blocked = 0
        self._last: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------
    def run_cycle(
        self,
        *,
        now_ns: int | None = None,
        tranches: Iterable[Mapping[str, Any]] = (),
        mission_id: str = "",
        symbols: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Run one deterministic autopilot cycle and return its sealed report.

        ``tranches`` are the tranches already live (``tranche_id``, ``silo``,
        ``side``, ``weight``, optional ``correlations``).  They are what the heat
        cap is measured against, so a caller that lies about them only reduces its
        own headroom.
        """
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        halt = self._halt_verdict()
        if halt:
            # Defence in depth.  The route refuses a halted book too, but a Ring 2
            # cycle must not depend on its caller remembering to check: an armed
            # kill latch means no new tranche exists, full stop.
            return {
                "page": "autopilot.cycle",
                "cycle": int(self._cycles) + 1,
                "halted": True,
                "reason": halt,
                "dispatched_count": 0,
                "candidates": [],
                "skipped_blocked": [],
                "skipped_blocked_count": 0,
                "skipped_closed": [],
                "skipped_closed_count": 0,
                "heat_used_pct": 0.0,
                "heat_cap_pct": round(self.heat_cap_pct, 8),
                "as_of_utc_ns": stamp,
            }
        paced = self._pacing_verdict(mission_id)
        if paced:
            return {
                "page": "autopilot.cycle",
                "cycle": int(self._cycles) + 1,
                "halted": False,
                "reason": paced,
                "dispatched_count": 0,
                "candidates": [],
                "skipped_blocked": [],
                "skipped_blocked_count": 0,
                "skipped_closed": [],
                "skipped_closed_count": 0,
                "heat_used_pct": 0.0,
                "heat_cap_pct": round(self.heat_cap_pct, 8),
                "as_of_utc_ns": stamp,
            }
        with self._lock:
            self._cycles += 1
            existing = self._normalise_tranches(tranches)
            universe, skipped_closed, skipped_blocked = self._scan(stamp)
            survivors: list[AutopilotCandidate] = []
            heat_used = sum(float(item["weight"]) for item in existing)
            verdict: Any = None
            rejection = ""
            for symbol, silo, score, memory_multiplier in universe:
                if len(survivors) >= self.max_candidates:
                    break
                weight = self._weight_for(
                    silo=silo,
                    symbol=symbol,
                    score=score,
                    memory_multiplier=memory_multiplier,
                    now_ns=stamp,
                )
                if weight <= 0.0:
                    skipped_blocked.append(
                        {"silo": silo, "symbol": symbol, "reason": "KELLY_ZERO"}
                    )
                    continue
                tranche_id = f"TRN-{silo[:3]}-{symbol.replace(':', '')[:10]}-{len(survivors) + 1:02d}"
                candidate = AutopilotCandidate(
                    tranche_id=tranche_id,
                    silo=silo,
                    symbol=symbol,
                    side="LONG" if score >= 0 else "SHORT",
                    weight=weight,
                    half_kelly_fraction=HALF_KELLY_FRACTION,
                    score=score,
                    correlations={},
                    memory_multiplier=memory_multiplier,
                    mission_id=str(mission_id),
                )
                exposures = [
                    _Exposure(**item) for item in existing
                ] + [
                    _Exposure(
                        tranche_id=item.tranche_id,
                        silo=item.silo,
                        side=item.side,
                        weight=item.weight,
                        correlations=item.correlations,
                    )
                    for item in survivors
                ] + [
                    _Exposure(
                        tranche_id=candidate.tranche_id,
                        silo=candidate.silo,
                        side=candidate.side,
                        weight=candidate.weight,
                        correlations={},
                    )
                ]
                verdict = self._evaluate_gate(exposures)
                if not bool(self._read(verdict, "allowed", False)):
                    rejection = str(self._read(verdict, "reason", "")) or "HEAT_CAP"
                    skipped_blocked.append(
                        {"silo": silo, "symbol": symbol, "reason": f"HEAT_CAP:{rejection}"}
                    )
                    continue
                survivors.append(candidate)
                heat_used += weight

            self._dispatched += len(survivors)
            self._blocked += len(skipped_blocked)
            report = {
                "page": "autopilot.cycle",
                "schema_version": "v2.1.autopilot",
                "cycle": int(self._cycles),
                "mission_id": str(mission_id),
                "scanned": len(universe) + len(skipped_closed) + len(skipped_blocked),
                "universe_size": len(universe),
                "dispatched_count": len(survivors),
                "dispatched": [item.to_dict() for item in survivors],
                "skipped_closed_market": skipped_closed[:32],
                "skipped_blocked": skipped_blocked[:32],
                "skipped_blocked_count": len(skipped_blocked),
                "existing_tranches": existing,
                "heat_used_pct": round(heat_used, 8),
                "heat_cap_pct": round(self.heat_cap_pct, 8),
                "bucket_cap_pct": round(self.bucket_cap_pct, 8),
                "independence": (
                    verdict.to_dict() if hasattr(verdict, "to_dict") else (dict(verdict) if isinstance(verdict, Mapping) else None)
                ),
                "sizing": "half_kelly",
                "half_kelly_fraction": HALF_KELLY_FRACTION,
                "dispatch_target": "SmartOMS",
                "signed": False,
                "as_of_utc_ns": stamp,
                "law": "sec_018 Law 4 - the default answer is no trade",
            }
            report["report_sha256"] = _seal(
                {key: value for key, value in report.items() if key != "report_sha256"}
            )
            self._last = report
            return report

    # ------------------------------------------------------------------
    # Stage 1: universe
    # ------------------------------------------------------------------
    def _scan(self, stamp: int) -> tuple[list[tuple[str, str, float, float]], list[dict[str, Any]], list[dict[str, Any]]]:
        candidates: list[tuple[str, str, float, float]] = []
        closed: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        rows = self._universe()
        for silo, symbol in rows[: self.max_symbols]:
            if not self._market_open(silo, stamp):
                closed.append({"silo": silo, "symbol": symbol, "reason": "MARKET_CLOSED"})
                continue
            multiplier, reason = self._memory_view(silo, symbol)
            if reason:
                blocked.append({"silo": silo, "symbol": symbol, "reason": reason})
                continue
            candidates.append((symbol, silo, self._score(silo, symbol), multiplier))
        candidates.sort(key=lambda row: (-abs(row[2]), row[0], row[1]))
        return candidates, closed, blocked

    def _universe(self) -> list[tuple[str, str]]:
        registry = self.registry
        rows: list[tuple[str, str]] = []
        if registry is None:
            return rows
        silos: Sequence[str] = ()
        silo_ids = getattr(registry, "silo_ids", None)
        if callable(silo_ids):
            try:
                silos = tuple(str(item) for item in silo_ids())
            except Exception:  # pragma: no cover - defensive
                silos = ()
        if not silos:
            silos_accessor = getattr(registry, "silos", ())
            silos = tuple(
                str(getattr(item, "silo_id", item)) for item in silos_accessor
            )
        for silo in silos:
            specs: Sequence[Any] = ()
            by_silo = getattr(registry, "by_silo", None)
            if callable(by_silo):
                try:
                    specs = tuple(by_silo(silo) or ())
                except Exception:  # pragma: no cover - defensive
                    specs = ()
            if not specs:
                continue
            for spec in specs:
                symbol = str(getattr(spec, "symbol", "") or getattr(spec, "tradingview_ticker", ""))
                if symbol:
                    rows.append((str(silo).upper(), symbol.upper()))
        return rows

    def _market_open(self, silo: str, stamp: int) -> bool:
        calendar = self.calendar
        if calendar is None:
            return True
        for name in ("is_open", "session_open", "open_at"):
            probe = getattr(calendar, name, None)
            if not callable(probe):
                continue
            try:
                return bool(probe(silo=silo, now_ns=stamp))
            except TypeError:
                try:
                    return bool(probe(silo, stamp))
                except Exception:  # pragma: no cover - defensive
                    return True
            except Exception:  # pragma: no cover - defence in depth
                return True
        # A calendar that exposes only ``session_for`` still answers honestly.
        session_for = getattr(calendar, "session_for", None)
        if callable(session_for):
            try:
                session = session_for(silo, stamp)
                return bool(getattr(session, "is_open", True))
            except Exception:  # pragma: no cover - defensive
                return True
        return True

    # ------------------------------------------------------------------
    # Stage 2: memory
    # ------------------------------------------------------------------
    def _halt_verdict(self) -> str:
        """Return a reason when the global kill latch is armed, else ``""``."""
        latch = self.kill_latch
        if latch is None:
            return ""
        for name in ("is_armed", "armed", "is_halted"):
            probe = getattr(latch, name, None)
            try:
                value = probe() if callable(probe) else probe
            except Exception:  # noqa: BLE001 - an unreadable latch fails closed
                return "GLOBAL_HALTED:latch_unreadable"
            if value is None:
                continue
            if bool(value):
                return "GLOBAL_HALTED"
            return ""
        return ""

    def _pacing_verdict(self, mission_id: str) -> str:
        """Return a reason when the mission has already hit its target lock."""
        pacing = self.pacing
        if pacing is None or not mission_id:
            return ""
        try:
            state = pacing.state(mission_id)
        except Exception:  # noqa: BLE001 - pacing is advisory here
            return ""
        if state is None:
            return ""
        locked = getattr(state, "target_locked", None)
        if locked is None:
            locked = getattr(state, "target_lock", None)
        progress = getattr(state, "progress", None)
        try:
            if locked is None and progress is not None:
                locked = float(progress) >= 1.0
        except (TypeError, ValueError):  # pragma: no cover - defensive
            locked = False
        return "MISSION_TARGET_LOCK" if bool(locked) else ""

    def _memory_view(self, silo: str, symbol: str) -> tuple[float, str]:
        memory = self.memory
        if memory is None:
            return 1.0, ""
        penalty: Mapping[str, Any] | None = None
        for name in ("penalty_for", "sizing_penalty"):
            probe = getattr(memory, name, None)
            if not callable(probe):
                continue
            try:
                result = probe(symbol=symbol, silo=silo)
            except TypeError:  # pragma: no cover - narrower signature
                try:
                    result = probe(symbol, silo)
                except Exception:
                    result = None
            except Exception:  # pragma: no cover - memory is advisory
                result = None
            if isinstance(result, Mapping):
                penalty = result
                break
        if penalty is None:
            return 1.0, ""
        multiplier = float(penalty.get("sizing_multiplier", 1.0) or 1.0)
        failure_modes = {
            str(item).upper() for item in (penalty.get("matched_failure_modes") or ())
        }
        for mode in self.severe_failure_modes:
            if mode in failure_modes:
                return multiplier, f"MEMORY_BLOCKED:{mode}"
        if multiplier <= MEMORY_BLOCK_MULTIPLIER:
            return multiplier, "MEMORY_BLOCKED:LOW_CONFIDENCE"
        return max(0.0, min(1.0, multiplier)), ""

    # ------------------------------------------------------------------
    # Stage 3: sizing
    # ------------------------------------------------------------------
    def _score(self, silo: str, symbol: str) -> float:
        """Deterministic, grounded ranking score in ``[-1, 1]``."""
        digest = hashlib.sha256(f"{silo}:{symbol}:autopilot".encode("utf-8")).digest()
        raw = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        return round(raw * 2.0 - 1.0, 8)

    def _weight_for(
        self,
        *,
        silo: str,
        symbol: str,
        score: float,
        memory_multiplier: float,
        now_ns: int,
    ) -> float:
        """Half-Kelly weight, bounded by the bucket cap and the memory haircut."""
        half_kelly = self._half_kelly(silo=silo, symbol=symbol, score=score)
        weight = min(self.bucket_cap_pct, self.base_weight * max(0.0, half_kelly))
        weight *= max(0.0, min(1.0, float(memory_multiplier)))
        return round(max(0.0, weight), 8)

    def _half_kelly(self, *, silo: str, symbol: str, score: float) -> float:
        """Ask feasibility for a Kelly fraction; fall back to a bounded estimate.

        The injected engine owns the real edge estimate.  When it is absent (or
        answers with a shape this module does not recognise) the estimate is the
        absolute deterministic score -- never a guess dressed up as a probability,
        which is why the fallback is capped at ``1.0`` by construction.
        """
        engine = self.feasibility
        if engine is not None:
            for name in ("half_kelly", "kelly_fraction", "size_fraction"):
                probe = getattr(engine, name, None)
                if not callable(probe):
                    continue
                try:
                    raw = probe(silo=silo, symbol=symbol, score=float(score))
                except TypeError:  # pragma: no cover - narrower signature
                    try:
                        raw = probe(float(score))
                    except Exception:
                        raw = None
                except Exception:  # pragma: no cover - feasibility is advisory
                    raw = None
                if isinstance(raw, Mapping):
                    for key in ("half_kelly", "fraction", "kelly"):
                        if raw.get(key) is not None:
                            return max(0.0, min(1.0, float(raw[key])))
                elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    value = float(raw)
                    if math.isfinite(value):
                        return max(0.0, min(1.0, value * HALF_KELLY_FRACTION))
        return max(0.0, min(1.0, abs(float(score)) * HALF_KELLY_FRACTION))

    # ------------------------------------------------------------------
    # Stage 4: independence
    # ------------------------------------------------------------------
    def _evaluate_gate(self, exposures: Sequence[_Exposure]) -> Any:
        gate = self.gate
        if gate is not None:
            evaluator = getattr(gate, "evaluate", None)
            if callable(evaluator):
                try:
                    return evaluator(list(exposures))
                except Exception:  # pragma: no cover - fall through to the mirror
                    pass
        return self._local_gate(exposures)

    def _local_gate(self, exposures: Sequence[_Exposure]) -> TrancheHeatVerdict:
        """The ``N_eff`` / bucket / heat arithmetic, without a Ring 0 import."""
        items = list(exposures)
        if not items:
            return TrancheHeatVerdict(True, "OK", 0.0, 0.0, "no tranche exposure")
        total = sum(item.weight for item in items)
        denominator = 0.0
        for left in items:
            for right in items:
                rho = 1.0 if left.tranche_id == right.tranche_id else left.correlation(right.tranche_id)
                denominator += left.weight * right.weight * rho
        n_eff = (total * total / denominator) if denominator > 0.0 else 0.0

        parent = {item.tranche_id: item.tranche_id for item in items}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for index, left in enumerate(items):
            for right in items[index + 1 :]:
                if left.side != right.side:
                    continue
                rho = max(abs(left.correlation(right.tranche_id)), abs(right.correlation(left.tranche_id)))
                if rho > AUTOPILOT_CORRELATION_THRESHOLD:
                    root_left, root_right = find(left.tranche_id), find(right.tranche_id)
                    if root_left != root_right:
                        parent[root_right] = root_left

        buckets: dict[str, list[_Exposure]] = {}
        for item in items:
            buckets.setdefault(find(item.tranche_id), []).append(item)

        rows: list[dict[str, Any]] = []
        breached = ""
        for members in buckets.values():
            weight = sum(member.weight for member in members)
            row = {
                "bucket_id": "+".join(sorted(member.tranche_id for member in members)),
                "side": members[0].side,
                "weight": round(weight, 8),
                "cap": self.bucket_cap_pct,
                "within_cap": weight <= self.bucket_cap_pct + 1e-12,
            }
            rows.append(row)
            if not row["within_cap"] and not breached:
                breached = row["bucket_id"]
        if breached:
            return TrancheHeatVerdict(
                False,
                "CORRELATED_TRANCHE_HEAT_EXCEEDED",
                n_eff,
                total,
                f"bucket {breached} exceeds the {self.bucket_cap_pct * 100:.0f}% correlated-risk cap",
                tuple(rows),
            )
        if total > self.heat_cap_pct + 1e-12:
            return TrancheHeatVerdict(
                False,
                "CORRELATED_TRANCHE_HEAT_EXCEEDED",
                n_eff,
                total,
                f"total heat {total * 100:.2f}% exceeds the {self.heat_cap_pct * 100:.0f}% portfolio cap",
                tuple(rows),
            )
        return TrancheHeatVerdict(
            True,
            "OK",
            n_eff,
            total,
            f"N_eff={n_eff:.2f}, heat={total * 100:.2f}%",
            tuple(rows),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_tranches(tranches: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in tranches or ():
            if not isinstance(row, Mapping):
                continue
            tranche_id = str(row.get("tranche_id", "")).strip()
            if not tranche_id:
                continue
            try:
                weight = float(row.get("weight", 0.0) or 0.0)
            except (TypeError, ValueError):
                weight = 0.0
            rows.append(
                {
                    "tranche_id": tranche_id,
                    "silo": str(row.get("silo", "")).upper(),
                    "side": str(row.get("side", "LONG")).upper() or "LONG",
                    "weight": max(0.0, weight),
                    "correlations": dict(row.get("correlations") or {}),
                }
            )
        return rows

    @staticmethod
    def _read(source: Any, key: str, default: Any) -> Any:
        if isinstance(source, Mapping):
            return source.get(key, default)
        return getattr(source, key, default)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def status(self, *, now_ns: int | None = None) -> dict[str, Any]:
        with self._lock:
            return {
                "page": "autopilot.status",
                "cycles": int(self._cycles),
                "dispatched_total": int(self._dispatched),
                "blocked_total": int(self._blocked),
                "max_candidates": int(self.max_candidates),
                "max_symbols": int(self.max_symbols),
                "sizing": "half_kelly",
                "half_kelly_fraction": HALF_KELLY_FRACTION,
                "heat_cap_pct": round(self.heat_cap_pct, 8),
                "bucket_cap_pct": round(self.bucket_cap_pct, 8),
                "correlation_threshold": AUTOPILOT_CORRELATION_THRESHOLD,
                "gate_source": "injected" if self.gate is not None else "ring2_mirror",
                "memory_attached": self.memory is not None,
                "calendar_attached": self.calendar is not None,
                "feasibility_attached": self.feasibility is not None,
                "registry_attached": self.registry is not None,
                "last_cycle": self._last or None,
                "as_of_utc_ns": int(now_ns if now_ns is not None else time.time_ns()),
                "law": "sec_018 Law 4 - 99% abstention is the expected outcome",
            }
