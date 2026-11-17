"""NIOS V1.9 -- mission pacing controller (``A4``).

A mission that reaches its goal must not give the gain back, and a mission that
is close to its goal must not keep betting like it is at the start.  The pacing
controller is the Ring 0 object that enforces both:

* ``g = realized_locked_micros / target_micros``;
* for ``g >= 0.80`` sizing is throttled by ``f_g = f_0 * max(0.25, (1-g)^2)``;
* a single UTC day may contribute at most 35% of the target (best-day cap);
* ``g >= 1.0`` latches ``TARGET_LOCK`` and blocks every new risk entry;
* **unrealized P&L never raises the high-water mark** -- only settled, locked
  profit does, because unrealized profit is exactly what a give-back consumes.

All money is integer micro-units and all dates are UTC.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from core.schemas import MissionSpec

__all__ = [
    "BEST_DAY_CAP_PCT",
    "THROTTLE_START_PROGRESS",
    "THROTTLE_FLOOR",
    "PacingState",
    "MissionPacingController",
]

#: ``A4``: no single day may contribute more than 35% of the mission target.
BEST_DAY_CAP_PCT: float = 0.35

#: ``A4``: throttling starts once 80% of the target is locked.
THROTTLE_START_PROGRESS: float = 0.80

#: ``A4``: the throttle never collapses below a quarter of the base size.
THROTTLE_FLOOR: float = 0.25

_NS_PER_DAY = 86_400_000_000_000


def _utc_day(now_ns: int) -> int:
    return int(now_ns) // _NS_PER_DAY


@dataclass(slots=True)
class PacingState:
    """Live pacing state for one mission."""

    mission_id: str
    target_micros: int
    risk_budget_micros: int
    realized_locked_micros: int = 0
    high_water_micros: int = 0
    unrealized_micros: int = 0
    day_key: int = 0
    day_realized_micros: int = 0
    target_locked: bool = False
    day_cap_reached: bool = False
    reason: str = "PACING_ACTIVE"

    @property
    def progress(self) -> float:
        if self.target_micros <= 0:
            return 0.0
        return max(0.0, self.realized_locked_micros / self.target_micros)

    @property
    def sizing_multiplier(self) -> float:
        """``f_g`` -- 1.0 below 80%, then the squared give-back throttle."""
        progress = self.progress
        if progress < THROTTLE_START_PROGRESS:
            return 1.0
        return max(THROTTLE_FLOOR, (1.0 - progress) ** 2)

    @property
    def can_open_risk(self) -> bool:
        return not self.target_locked and not self.day_cap_reached

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "target_micros": self.target_micros,
            "risk_budget_micros": self.risk_budget_micros,
            "realized_locked_micros": self.realized_locked_micros,
            "high_water_micros": self.high_water_micros,
            "unrealized_micros": self.unrealized_micros,
            "progress": round(self.progress, 8),
            "sizing_multiplier": round(self.sizing_multiplier, 8),
            "day_realized_micros": self.day_realized_micros,
            "target_locked": self.target_locked,
            "day_cap_reached": self.day_cap_reached,
            "can_open_risk": self.can_open_risk,
            "reason": self.reason,
        }


class MissionPacingController:
    """Track progress, throttle sizing, cap the best day and latch the target."""

    def __init__(
        self,
        *,
        best_day_cap_pct: float = BEST_DAY_CAP_PCT,
        throttle_start: float = THROTTLE_START_PROGRESS,
        throttle_floor: float = THROTTLE_FLOOR,
    ) -> None:
        self.best_day_cap_pct = float(best_day_cap_pct)
        self.throttle_start = float(throttle_start)
        self.throttle_floor = float(throttle_floor)
        if not 0.0 < self.best_day_cap_pct <= 1.0:
            raise ValueError("best_day_cap_pct must be in (0, 1]")
        self._states: dict[str, PacingState] = {}

    # -- registration -----------------------------------------------------
    def register(self, mission: MissionSpec, *, now_ns: int | None = None) -> PacingState:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        state = PacingState(
            mission_id=mission.mission_id,
            target_micros=int(mission.target_micros),
            risk_budget_micros=int(mission.risk_budget_micros),
            day_key=_utc_day(stamp),
        )
        self._states[mission.mission_id] = state
        return state

    def state(self, mission_id: str) -> PacingState | None:
        return self._states.get(str(mission_id))

    def states(self) -> tuple[PacingState, ...]:
        return tuple(self._states.values())

    # -- accounting -------------------------------------------------------
    def record_realized(
        self, mission_id: str, delta_micros: int, *, now_ns: int | None = None
    ) -> PacingState:
        """Book settled P&L, applying the best-day cap and the target latch."""
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        state = self._states.get(str(mission_id))
        if state is None:
            raise KeyError(f"unknown mission {mission_id!r}")
        today = _utc_day(stamp)
        if today != state.day_key:
            state.day_key = today
            state.day_realized_micros = 0
            state.day_cap_reached = False
        delta = int(delta_micros)
        if delta > 0:
            day_cap = int(state.target_micros * self.best_day_cap_pct)
            remaining_cap = max(0, day_cap - state.day_realized_micros)
            if remaining_cap <= 0:
                state.day_cap_reached = True
                state.reason = "BEST_DAY_CAP_REACHED"
                return state
            accepted = min(delta, remaining_cap)
            state.realized_locked_micros += accepted
            state.day_realized_micros += accepted
            if state.day_realized_micros >= day_cap:
                state.day_cap_reached = True
        else:
            state.realized_locked_micros += delta
        state.high_water_micros = max(
            state.high_water_micros, state.realized_locked_micros
        )
        if state.progress >= 1.0:
            state.target_locked = True
            state.reason = "TARGET_LOCK"
        elif state.day_cap_reached:
            state.reason = "BEST_DAY_CAP_REACHED"
        else:
            state.reason = "PACING_ACTIVE"
        return state

    def record_unrealized(
        self, mission_id: str, unrealized_micros: int
    ) -> PacingState:
        """Record mark-to-market **without** touching the high-water mark."""
        state = self._states.get(str(mission_id))
        if state is None:
            raise KeyError(f"unknown mission {mission_id!r}")
        state.unrealized_micros = int(unrealized_micros)
        return state

    # -- gates ------------------------------------------------------------
    def sizing_multiplier(self, mission_id: str) -> float:
        state = self._states.get(str(mission_id))
        if state is None:
            raise KeyError(f"unknown mission {mission_id!r}")
        if not state.can_open_risk:
            return 0.0
        return state.sizing_multiplier

    def is_target_locked(self, mission_id: str) -> bool:
        state = self._states.get(str(mission_id))
        return bool(state is not None and state.target_locked)

    def can_open_risk(self, mission_id: str, *, now_ns: int | None = None) -> bool:
        state = self._states.get(str(mission_id))
        if state is None:
            return False
        if now_ns is not None and _utc_day(now_ns) != state.day_key:
            # A new UTC day clears yesterday's cap, never the target latch.
            state.day_key = _utc_day(now_ns)
            state.day_realized_micros = 0
            state.day_cap_reached = False
        return state.can_open_risk

    def status(
        self, mission_id: str | None = None, *, now_ns: int | None = None
    ) -> dict[str, Any]:
        if mission_id is not None:
            state = self._states.get(str(mission_id))
            if state is None:
                raise KeyError(f"unknown mission {mission_id!r}")
            return state.to_dict()
        return {
            "missions": [state.to_dict() for state in self._states.values()],
            "count": len(self._states),
            "as_of_utc_ns": int(now_ns if now_ns is not None else time.time_ns()),
        }
