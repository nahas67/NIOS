"""NIOS V1.9 -- Ring 3 mission and tranche control surface (``A1``-``A8``).

This module is the *only* place the cockpit can create, approve, revoke, list or
re-arm a mission or a tranche.  It owns no broker credential and signs nothing:
it builds the immutable :class:`MissionSpec`, asks the Ring 2 feasibility engine
whether the goal is reachable, registers the Ring 0 pacing/tranche guards and
returns a proof payload.

Errors carry both a stable code and an HTTP status so the gateway can map them
without re-deriving policy:

======================  ====  ==================================================
INFEASIBLE_MISSION      422   the target cannot be reached before ruin
UNKNOWN_MISSION         404   no mission with that id
UNKNOWN_TRANCHE         404   no tranche with that id
MISSION_REVOKED         409   the mission approval was revoked
UNAUTHORIZED_MISSION    403   the approval does not cover the silo/notional
======================  ====  ==================================================
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from core.schemas import (
    MissionAuthorization,
    MissionInfeasibleError,
    MissionSpec,
    TrancheRiskState,
    TrancheStatus,
)

__all__ = [
    "MISSION_MICROS_PER_UNIT",
    "MissionControlError",
    "MissionControlSurface",
]

MISSION_MICROS_PER_UNIT: int = 1_000_000


def _micros(units: float) -> int:
    return int(round(float(units) * MISSION_MICROS_PER_UNIT))


class MissionControlError(Exception):
    """A policy denial with a stable code and HTTP status."""

    def __init__(self, code: str, message: str, *, http_status: int = 422) -> None:
        super().__init__(message)
        self.code = str(code)
        self.http_status = int(http_status)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": str(self), "http_status": self.http_status}


@dataclass(slots=True)
class _MissionRecord:
    spec: MissionSpec
    created_by: str
    created_at_ns: int
    feasibility: dict[str, Any]
    revoked: bool = False
    revoked_at_ns: int = 0
    revoke_reason: str = ""


class MissionControlSurface:
    """Mission/tranche registry shared by the REST routes and the SSE cards."""

    def __init__(
        self,
        *,
        ledger: Any = None,
        feasibility: Any = None,
        pacing: Any = None,
        tranche_guard: Any = None,
        split_engine: Any = None,
        quota_engine: Any = None,
        sweeper: Any = None,
        default_silo: str = "US_IBKR",
    ) -> None:
        self.ledger = ledger
        self.feasibility = feasibility
        self.pacing = pacing
        self.tranche_guard = tranche_guard
        self.split_engine = split_engine
        self.quota_engine = quota_engine
        self.sweeper = sweeper
        self.default_silo = str(default_silo)
        self._missions: dict[str, _MissionRecord] = {}
        self._tranche_sequence = 0

    # -- helpers ----------------------------------------------------------
    def _record(self, mission_id: str) -> _MissionRecord:
        record = self._missions.get(str(mission_id))
        if record is None:
            raise MissionControlError(
                "UNKNOWN_MISSION", f"no mission {mission_id!r}", http_status=404
            )
        return record

    def _equity_micros(self, silos: Sequence[str]) -> int:
        if self.ledger is None:
            return 0
        total = 0
        for silo in silos:
            try:
                total += int(self.ledger.silo_nav_micros(str(silo)))
            except Exception:  # pragma: no cover - a missing silo is zero NAV
                continue
        return total

    # -- missions ---------------------------------------------------------
    def create(
        self,
        *,
        mission_id: str,
        target_units: float,
        deadline_utc: int,
        risk_budget_units: float,
        operator: str,
        silos: Iterable[str],
        notional_cap_units: float,
        ttl_seconds: int = 3600,
        kelly_fraction_cap: float = 0.5,
        strategy_lineage_digests: Iterable[str] = (),
        unlock_ladder: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
        state: str = "APPROVE",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        silo_list = tuple(str(silo).strip().upper() for silo in silos if str(silo).strip())
        if not silo_list:
            raise MissionControlError("UNAUTHORIZED_MISSION", "at least one silo is required", http_status=403)
        approval = MissionAuthorization.mint(
            operator,
            silo_list,
            _micros(notional_cap_units),
            ttl_seconds=int(ttl_seconds),
            state=state,
            now_ns=stamp,
        )
        try:
            spec = MissionSpec(
                mission_id=mission_id,
                target_micros=_micros(target_units),
                deadline_utc=int(deadline_utc),
                risk_budget_micros=_micros(risk_budget_units),
                kelly_fraction_cap=float(kelly_fraction_cap),
                strategy_lineage_digests=tuple(strategy_lineage_digests),
                unlock_ladder=tuple(float(step) for step in unlock_ladder),
                approval=approval,
                created_at_ns=stamp,
            )
        except ValueError as exc:
            raise MissionControlError("INVALID_MISSION", str(exc)) from exc

        verdict: dict[str, Any] = {"feasible": True, "code": "FEASIBLE", "reasons": []}
        if self.feasibility is not None:
            try:
                assessed = self.feasibility.assess(
                    spec,
                    equity_micros=self._equity_micros(silo_list),
                    now_ns=stamp,
                )
                verdict = assessed.to_dict()
                if not assessed.feasible:
                    raise MissionInfeasibleError(
                        "INFEASIBLE_MISSION: " + ", ".join(assessed.reasons)
                    )
            except MissionInfeasibleError as exc:
                raise MissionControlError(
                    "INFEASIBLE_MISSION", str(exc), http_status=422
                ) from exc

        record = _MissionRecord(
            spec=spec,
            created_by=str(operator),
            created_at_ns=stamp,
            feasibility=verdict,
        )
        self._missions[spec.mission_id] = record
        if self.pacing is not None:
            try:
                self.pacing.register(spec, now_ns=stamp)
            except Exception:  # pragma: no cover - pacing is best effort here
                pass
        return self._mission_payload(record, now_ns=stamp)

    def approve(
        self,
        mission_id: str,
        *,
        operator: str,
        state: str = "APPROVE",
        ttl_seconds: int | None = None,
        notional_cap_units: float | None = None,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        record = self._record(mission_id)
        current = record.spec.approval
        if current is None:
            raise MissionControlError("UNAUTHORIZED_MISSION", "mission has no approval", http_status=403)
        approval = MissionAuthorization.mint(
            operator,
            current.silos,
            _micros(notional_cap_units) if notional_cap_units is not None else current.notional_cap_micros,
            ttl_seconds=int(ttl_seconds) if ttl_seconds is not None else current.ttl_seconds,
            state=state,
            now_ns=stamp,
        )
        record.spec = MissionSpec(
            mission_id=record.spec.mission_id,
            target_micros=record.spec.target_micros,
            deadline_utc=record.spec.deadline_utc,
            risk_budget_micros=record.spec.risk_budget_micros,
            kelly_fraction_cap=record.spec.kelly_fraction_cap,
            strategy_lineage_digests=record.spec.strategy_lineage_digests,
            unlock_ladder=record.spec.unlock_ladder,
            approval=approval,
            created_at_ns=record.spec.created_at_ns,
        )
        record.revoked = False
        record.revoke_reason = ""
        return self._mission_payload(record, now_ns=stamp)

    def revoke(
        self,
        mission_id: str,
        *,
        operator: str = "operator",
        reason: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        record = self._record(mission_id)
        record.revoked = True
        record.revoked_at_ns = stamp
        record.revoke_reason = str(reason)
        if record.spec.approval is not None:
            record.spec = MissionSpec(
                mission_id=record.spec.mission_id,
                target_micros=record.spec.target_micros,
                deadline_utc=record.spec.deadline_utc,
                risk_budget_micros=record.spec.risk_budget_micros,
                kelly_fraction_cap=record.spec.kelly_fraction_cap,
                strategy_lineage_digests=record.spec.strategy_lineage_digests,
                unlock_ladder=record.spec.unlock_ladder,
                approval=record.spec.approval.revoked(),
                created_at_ns=record.spec.created_at_ns,
            )
        # Revoking a mission halts every tranche that belongs to it.
        halted: list[str] = []
        if self.tranche_guard is not None:
            for state in self.tranche_guard.states():
                if state.mission_id == record.spec.mission_id and state.status is not TrancheStatus.HALTED:
                    self.tranche_guard.halt(state.tranche_id, reason="MISSION_REVOKED")
                    halted.append(state.tranche_id)
        payload = self._mission_payload(record, now_ns=stamp)
        payload["revoked_by"] = str(operator)
        payload["halted_tranches"] = halted
        return payload

    def list_missions(self, *, now_ns: int | None = None) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        rows = [self._mission_payload(record, now_ns=stamp) for record in self._missions.values()]
        return {
            "missions": rows,
            "count": len(rows),
            "active": sum(1 for row in rows if row["approval_state"] != "REVOKE"),
            "as_of_utc_ns": stamp,
        }

    def mission_status(self, mission_id: str, *, now_ns: int | None = None) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        return self._mission_payload(self._record(mission_id), now_ns=stamp)

    def _mission_payload(self, record: _MissionRecord, *, now_ns: int) -> dict[str, Any]:
        spec = record.spec
        approval = spec.approval
        pacing = None
        if self.pacing is not None:
            try:
                pacing = self.pacing.status(spec.mission_id, now_ns=now_ns)
            except Exception:  # pragma: no cover
                pacing = None
        tranches = []
        if self.tranche_guard is not None:
            tranches = [
                state.to_dict()
                for state in self.tranche_guard.states()
                if state.mission_id == spec.mission_id
            ]
        return {
            "mission_id": spec.mission_id,
            "target_micros": spec.target_micros,
            "deadline_utc": spec.deadline_utc,
            "risk_budget_micros": spec.risk_budget_micros,
            "kelly_fraction_cap": spec.kelly_fraction_cap,
            "daily_velocity_target_micros": spec.daily_velocity_target_micros(now_ns),
            "days_remaining": round(spec.days_remaining(now_ns), 6),
            "expired": spec.is_expired(now_ns),
            "approval_state": approval.state if approval else "NONE",
            "approval": approval.to_dict() if approval else None,
            "approval_live": approval.is_live(now_ns) if approval else False,
            "revoked": record.revoked,
            "revoke_reason": record.revoke_reason,
            "feasibility": record.feasibility,
            "pacing": pacing,
            "tranches": tranches,
            "created_by": record.created_by,
            "created_at_ns": record.created_at_ns,
        }

    # -- tranches ---------------------------------------------------------
    def allocate_tranche(
        self,
        mission_id: str,
        *,
        silo: str,
        allocated_units: float,
        side: str = "LONG",
        stop_units: float | None = None,
        lineage_digest: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        record = self._record(mission_id)
        if record.revoked or not record.spec.approval or not record.spec.approval.is_live(stamp):
            raise MissionControlError(
                "MISSION_REVOKED", f"mission {mission_id!r} is revoked or expired", http_status=409
            )
        allocated = _micros(allocated_units)
        if not record.spec.authorized(silo, allocated, now_ns=stamp):
            raise MissionControlError(
                "UNAUTHORIZED_MISSION",
                f"approval does not cover {allocated} micros in {silo}",
                http_status=403,
            )
        if self.tranche_guard is None:
            raise MissionControlError(
                "TRANCHE_GUARD_UNAVAILABLE", "Ring 0 tranche guard is not attached", http_status=503
            )
        self._tranche_sequence += 1
        tranche_id = f"{mission_id}-T{self._tranche_sequence:03d}"
        state = self.tranche_guard.open_tranche(
            tranche_id,
            mission_id=mission_id,
            silo=silo,
            allocated_micros=allocated,
            stop_micros=_micros(stop_units) if stop_units is not None else None,
            lineage_digest=lineage_digest,
            side=side,
            now_ns=stamp,
        )
        return state.to_dict()

    def list_tranches(
        self, *, mission_id: str | None = None, now_ns: int | None = None
    ) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        rows: list[dict[str, Any]] = []
        if self.tranche_guard is not None:
            rows = [
                state.to_dict()
                for state in self.tranche_guard.states()
                if mission_id is None or state.mission_id == str(mission_id)
            ]
        return {
            "tranches": rows,
            "count": len(rows),
            "halted": [row["tranche_id"] for row in rows if row["status"] == "HALTED"],
            "as_of_utc_ns": stamp,
        }

    def tranche_status(self, tranche_id: str, *, now_ns: int | None = None) -> dict[str, Any]:
        if self.tranche_guard is None:
            raise MissionControlError("UNKNOWN_TRANCHE", "no tranche guard", http_status=404)
        state = self.tranche_guard.state(tranche_id)
        if state is None:
            raise MissionControlError(
                "UNKNOWN_TRANCHE", f"no tranche {tranche_id!r}", http_status=404
            )
        payload = state.to_dict()
        payload["as_of_utc_ns"] = int(now_ns if now_ns is not None else time.time_ns())
        return payload

    def rearm_tranche(self, tranche_id: str, *, operator: str = "operator") -> dict[str, Any]:
        if self.tranche_guard is None:
            raise MissionControlError("UNKNOWN_TRANCHE", "no tranche guard", http_status=404)
        try:
            changed = self.tranche_guard.rearm(tranche_id)
        except KeyError as exc:
            raise MissionControlError(
                "UNKNOWN_TRANCHE", f"no tranche {tranche_id!r}", http_status=404
            ) from exc
        state = self.tranche_guard.state(tranche_id)
        payload = state.to_dict() if state is not None else {}
        payload["rearmed"] = bool(changed)
        payload["operator"] = str(operator)
        return payload

    # -- profit split -----------------------------------------------------
    def record_realized(
        self,
        mission_id: str,
        *,
        silo: str,
        delta_units: float,
        tranche_id: str = "",
        regime: str = "NEUTRAL",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        record = self._record(mission_id)
        delta_micros = _micros(delta_units)
        pacing = None
        if self.pacing is not None:
            pacing = self.pacing.record_realized(
                mission_id, delta_micros, now_ns=stamp
            ).to_dict()
        breach = None
        if tranche_id and self.tranche_guard is not None:
            breach = self.tranche_guard.record_realized(tranche_id, delta_micros).to_dict()
        split = None
        if self.split_engine is not None:
            from core.schemas import SplitPolicy

            progress = 0.0
            if pacing is not None:
                progress = float(pacing.get("progress", 0.0))
            split = self.split_engine.apply(
                silo,
                delta_micros,
                policy=SplitPolicy(
                    mission_progress=progress,
                    regime=regime,
                    unlock_ladder=record.spec.unlock_ladder,
                ),
                now_ns=stamp,
            ).to_dict()
        return {
            "mission_id": mission_id,
            "silo": silo,
            "delta_micros": delta_micros,
            "pacing": pacing,
            "tranche_breach": breach,
            "split": split,
            "as_of_utc_ns": stamp,
        }

    def snapshot(self, *, now_ns: int | None = None) -> dict[str, Any]:
        payload = self.list_missions(now_ns=now_ns)
        payload["tranche_count"] = len(self.list_tranches(now_ns=now_ns)["tranches"])
        return payload
