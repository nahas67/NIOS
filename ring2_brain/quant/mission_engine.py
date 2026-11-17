"""Mission feasibility engine -- "can this target be reached before ruin?" (``A3``).

``A3`` makes a mission a *goal*: a profit target, a UTC deadline, a risk budget
and a scoped, time-boxed operator approval (:class:`~core.schemas.MissionSpec`,
:class:`~core.schemas.MissionAuthorization`).  Before Ring 0 lets that goal size
a position it must answer one question: given the 200-trade split-conformal
residual window, is the target reachable before the risk budget is consumed, and
is the achievable pacing fast enough to arrive before the deadline?

The answer is deliberately conservative (``sec_018`` Law 4 -- "the default answer
is no trade", ~99.1% expected abstention; ``sec_002`` flaw 4 -- conformal
cold-start overconfidence).  A mission that cannot clear every gate is refused
with ``422 INFEASIBLE_MISSION`` (:class:`~core.schemas.MissionInfeasibleError`)
instead of being traded on hope.

Failure codes carried in :attr:`MissionFeasibility.reasons`
-----------------------------------------------------------
``ZERO_EQUITY``, ``NO_EDGE``, ``KELLY_CAP_ZERO``,
``REACH_PROBABILITY_TOO_LOW``, ``RUIN_PROBABILITY_TOO_HIGH``,
``VELOCITY_BELOW_PACING``, ``MISSION_EXPIRED``, ``MISSION_UNAUTHORIZED``.

``DEFAULT_RESIDUAL_WINDOW`` is a *caveat*, not a failure: it is appended when no
residual window was supplied and a seeded synthetic window was used, so a
consumer can never mistake a synthetic edge for a measured one.  ``feasible`` is
therefore governed by the failure codes only.

Determinism
-----------
Every number is a pure function of the constructor arguments, the mission, the
equity and the residual window.  The Monte Carlo draws from a fresh
``random.Random(self._seed)`` on every :meth:`MissionFeasibilityEngine.assess`
call, so two calls with identical inputs produce byte-identical
:meth:`MissionFeasibility.to_dict` output.  No clock is read unless ``now_ns``
is omitted, no network is touched, no thread is started.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from core.schemas import (
    MissionAuthorization,
    MissionInfeasibleError,
    MissionSpec,
    SplitPolicy,
)

__all__ = [
    "DEFAULT_RESIDUAL_MEAN_BPS",
    "DEFAULT_RESIDUAL_SD_BPS",
    "DEFAULT_RESIDUAL_WINDOW",
    "FEASIBLE_CODE",
    "INFEASIBLE_CODE",
    "MAX_SIMULATION_TRADES",
    "REASON_CODES",
    "REASON_DEFAULT_RESIDUAL_WINDOW",
    "REASON_KELLY_CAP_ZERO",
    "REASON_MISSION_EXPIRED",
    "REASON_MISSION_UNAUTHORIZED",
    "REASON_NO_EDGE",
    "REASON_REACH_PROBABILITY_TOO_LOW",
    "REASON_RUIN_PROBABILITY_TOO_HIGH",
    "REASON_VELOCITY_BELOW_PACING",
    "REASON_ZERO_EQUITY",
    "MissionFeasibility",
    "MissionFeasibilityEngine",
    "split_policy_for",
]

#: ``A3``: the conformal residual window is 200 trades (``sec_018`` Law 4).
DEFAULT_RESIDUAL_WINDOW: int = 200

#: Conservative synthetic edge used when no measured window is supplied.
DEFAULT_RESIDUAL_MEAN_BPS: float = 2.0
DEFAULT_RESIDUAL_SD_BPS: float = 12.0

#: Basis-point denominator: ``micros = notional_micros * bps / 10_000``.
BPS_DENOMINATOR: float = 10_000.0

#: ``422 INFEASIBLE_MISSION`` verdict codes.
FEASIBLE_CODE: str = "FEASIBLE"
INFEASIBLE_CODE: str = "INFEASIBLE_MISSION"

#: Failure reasons (``A3``).  Names are part of the cockpit contract.
REASON_ZERO_EQUITY: str = "ZERO_EQUITY"
REASON_NO_EDGE: str = "NO_EDGE"
REASON_KELLY_CAP_ZERO: str = "KELLY_CAP_ZERO"
REASON_REACH_PROBABILITY_TOO_LOW: str = "REACH_PROBABILITY_TOO_LOW"
REASON_RUIN_PROBABILITY_TOO_HIGH: str = "RUIN_PROBABILITY_TOO_HIGH"
REASON_VELOCITY_BELOW_PACING: str = "VELOCITY_BELOW_PACING"
REASON_MISSION_EXPIRED: str = "MISSION_EXPIRED"
REASON_MISSION_UNAUTHORIZED: str = "MISSION_UNAUTHORIZED"

#: Caveat: the residual window is synthetic, not measured.
REASON_DEFAULT_RESIDUAL_WINDOW: str = "DEFAULT_RESIDUAL_WINDOW"

REASON_CODES: tuple[str, ...] = (
    REASON_ZERO_EQUITY,
    REASON_NO_EDGE,
    REASON_KELLY_CAP_ZERO,
    REASON_REACH_PROBABILITY_TOO_LOW,
    REASON_RUIN_PROBABILITY_TOO_HIGH,
    REASON_VELOCITY_BELOW_PACING,
    REASON_MISSION_EXPIRED,
    REASON_MISSION_UNAUTHORIZED,
)

#: Safety bound on the simulated horizon (512 trading days at 8 trades/day).
#: A pathological deadline must never stall the pre-trade gate; the reported
#: ``trades_remaining`` still reflects the true horizon, and truncating the
#: simulation can only *understate* reach probability, which is the
#: abstention-biased direction required by ``sec_018`` Law 4.
MAX_SIMULATION_TRADES: int = 4_096


@dataclass(slots=True, frozen=True)
class _ResidualEdge:
    """Moments of the residual window and the unclamped Kelly fraction."""

    mu_bps: float
    sigma_bps: float
    win_probability: float
    mean_win_bps: float
    mean_loss_bps: float
    kelly_fraction: float


@dataclass(slots=True, frozen=True)
class MissionFeasibility:
    """Verdict of :class:`MissionFeasibilityEngine` for one mission (``A3``).

    ``p_reach`` and ``p_ruin`` are first-passage frequencies over the simulated
    paths, so ``p_reach + p_ruin <= 1``; a path that neither reaches the target
    nor breaches the risk budget counts as neither.  ``reasons`` lists every
    failed gate in the canonical order of :data:`REASON_CODES`, followed by
    ``DEFAULT_RESIDUAL_WINDOW`` when the window was synthetic.
    """

    mission_id: str
    feasible: bool
    code: str
    p_reach: float
    p_ruin: float
    kelly_fraction: float
    capped_kelly_fraction: float
    daily_velocity_micros: int
    velocity_cap_micros: int
    trades_remaining: int
    reasons: tuple[str, ...]
    residual_samples: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe, key-order-stable view for the cockpit and the ledger."""
        return {
            "mission_id": self.mission_id,
            "feasible": self.feasible,
            "code": self.code,
            "p_reach": self.p_reach,
            "p_ruin": self.p_ruin,
            "kelly_fraction": self.kelly_fraction,
            "capped_kelly_fraction": self.capped_kelly_fraction,
            "daily_velocity_micros": self.daily_velocity_micros,
            "velocity_cap_micros": self.velocity_cap_micros,
            "trades_remaining": self.trades_remaining,
            "reasons": list(self.reasons),
            "residual_samples": self.residual_samples,
        }


def _approval_is_live(approval: MissionAuthorization | None, now_ns: int) -> bool:
    """``A3``: an approval must exist, be scoped and not be revoked or expired."""
    return approval is not None and approval.is_live(now_ns)


def split_policy_for(
    mission: MissionSpec,
    realized_locked_micros: int,
    *,
    regime: str = "NEUTRAL",
) -> SplitPolicy:
    """``A5``: translate mission progress into the fund-split engine's policy.

    Kept here so the feasibility verdict and the split policy read the *same*
    progress number; ``SplitPolicy`` remains a pure data object.
    """
    return SplitPolicy(
        mission_progress=mission.progress(int(realized_locked_micros)),
        regime=regime,
        unlock_ladder=mission.unlock_ladder,
    )


class MissionFeasibilityEngine:
    """Deterministic reach-before-ruin gate for a capital mission (``A3``/``F4``).

    The engine holds the 200-trade split-conformal residual window, derives the
    asymmetric-payoff Kelly fraction from it, and runs a seeded first-passage
    Monte Carlo that charges fractional-Kelly sizing on notional equity.  It
    never sizes an order itself and never touches a broker: it only answers
    "may this mission proceed?", which Ring 0 turns into
    ``422 INFEASIBLE_MISSION`` when the answer is no.
    """

    def __init__(
        self,
        *,
        residuals_bps: Sequence[float] | None = None,
        seed: int = 1729,
        trades_per_day: int = 8,
        paths: int = 512,
        min_p_reach: float = 0.55,
        max_p_ruin: float = 0.25,
    ) -> None:
        """Build the gate.

        ``residuals_bps`` is the measured conformal window; when it is ``None``
        a 200-sample conservative normal (mean 2 bps, sd 12 bps) is synthesized
        from ``random.Random(seed)`` and every verdict carries the
        ``DEFAULT_RESIDUAL_WINDOW`` caveat.
        """
        self._seed = int(seed)
        self._trades_per_day = int(trades_per_day)
        self._paths = int(paths)
        self._min_p_reach = float(min_p_reach)
        self._max_p_ruin = float(max_p_ruin)

        if self._trades_per_day < 1:
            raise ValueError("trades_per_day must be >= 1")
        if self._paths < 1:
            raise ValueError("paths must be >= 1")
        if not math.isfinite(self._min_p_reach) or not 0.0 <= self._min_p_reach <= 1.0:
            raise ValueError("min_p_reach must lie in [0, 1]")
        if not math.isfinite(self._max_p_ruin) or not 0.0 <= self._max_p_ruin <= 1.0:
            raise ValueError("max_p_ruin must lie in [0, 1]")

        self._residuals: tuple[float, ...] = ()
        self._default_window = False
        if residuals_bps is None:
            self._install_default_window()
        else:
            self.set_residuals(residuals_bps)

    # -- residual window ---------------------------------------------------
    def _install_default_window(self) -> None:
        """Synthesize the conservative 200-trade window from the seed (``A3``).

        The draw is taken from ``random.Random(seed).gauss(2.0, 12.0)`` and then
        standardized so the realized window carries exactly the documented mean
        and standard deviation.  A raw 200-draw sample wanders by about
        ``12 / sqrt(200) = 0.85`` bps in the mean -- seed 1729 alone lands at
        ``0.004`` bps -- which would silently turn the documented 2 bps
        placeholder edge into a no-edge world and make every default verdict
        ``NO_EDGE``.  Standardizing keeps the placeholder deterministic *and*
        the documented moments true; a caller with measured residuals should
        use :meth:`set_residuals` instead.
        """
        rng = random.Random(self._seed)
        draws = [
            rng.gauss(DEFAULT_RESIDUAL_MEAN_BPS, DEFAULT_RESIDUAL_SD_BPS)
            for _ in range(DEFAULT_RESIDUAL_WINDOW)
        ]
        mean = sum(draws) / len(draws)
        variance = sum((value - mean) ** 2 for value in draws) / len(draws)
        deviation = math.sqrt(variance)
        if deviation > 0.0:
            self._residuals = tuple(
                DEFAULT_RESIDUAL_MEAN_BPS
                + DEFAULT_RESIDUAL_SD_BPS * ((value - mean) / deviation)
                for value in draws
            )
        else:  # pragma: no cover - a 200-draw sample cannot have zero spread
            self._residuals = tuple(
                DEFAULT_RESIDUAL_MEAN_BPS for _ in range(DEFAULT_RESIDUAL_WINDOW)
            )
        self._default_window = True

    def set_residuals(self, residuals: Sequence[float]) -> None:
        """Replace the residual window with measured conformal residuals (bps)."""
        window = tuple(float(value) for value in residuals)
        if not window:
            raise ValueError("residuals must contain at least one sample")
        if any(not math.isfinite(value) for value in window):
            raise ValueError("residuals must be finite basis-point values")
        self._residuals = window
        self._default_window = False

    @property
    def residuals(self) -> tuple[float, ...]:
        """The residual window currently in force (bps)."""
        return self._residuals

    # -- statistics --------------------------------------------------------
    def _edge(self) -> _ResidualEdge:
        """Window moments and the unclamped asymmetric-payoff Kelly fraction."""
        window = self._residuals
        count = len(window)
        mu = sum(window) / count
        sigma = (
            math.sqrt(sum((value - mu) ** 2 for value in window) / count)
            if count >= 2
            else 0.0
        )
        wins = [value for value in window if value > 0.0]
        losses = [value for value in window if value <= 0.0]
        win_probability = len(wins) / count
        mean_win = sum(wins) / len(wins) if wins else 0.0
        mean_loss = abs(sum(losses) / len(losses)) if losses else 0.0

        payoff_ratio = mean_win / mean_loss if mean_loss > 0.0 else 0.0
        if payoff_ratio > 0.0:
            kelly = win_probability - (1.0 - win_probability) / payoff_ratio
        else:
            kelly = 1.0 if win_probability >= 1.0 else -1.0
        if not math.isfinite(kelly):
            kelly = -1.0
        kelly = min(1.0, max(-1.0, kelly))

        return _ResidualEdge(
            mu_bps=mu,
            sigma_bps=sigma,
            win_probability=win_probability,
            mean_win_bps=mean_win,
            mean_loss_bps=mean_loss,
            kelly_fraction=kelly,
        )

    def kelly_fraction(self) -> float:
        """Unclamped ``f*`` for the current window (may be ``<= 0``, ``A3``)."""
        return self._edge().kelly_fraction

    # -- simulation --------------------------------------------------------
    def _first_passage(
        self,
        *,
        target_micros: int,
        risk_budget_micros: int,
        notional_micros: float,
        realized_locked_micros: int,
        steps: int,
    ) -> tuple[float, float]:
        """Seeded first-passage reach-before-ruin Monte Carlo (``A3``/``F4``)."""
        rng = random.Random(self._seed)
        window = self._residuals
        horizon = min(int(steps), MAX_SIMULATION_TRADES)
        # Precompute the micros charged per residual once: the inner loop must
        # stay allocation-free, and `int()` per path would dominate the gate.
        step_table = [int(notional_micros * (value / BPS_DENOMINATOR)) for value in window]
        draw_count = len(step_table)
        reached = 0
        ruined = 0

        for _ in range(self._paths):
            cumulative = 0
            for _ in range(horizon):
                cumulative += step_table[rng.randrange(draw_count)]
                if cumulative + realized_locked_micros >= target_micros:
                    reached += 1
                    break
                if cumulative <= -risk_budget_micros:
                    ruined += 1
                    break

        return reached / self._paths, ruined / self._paths

    # -- assessment --------------------------------------------------------
    def assess(
        self,
        mission: MissionSpec,
        *,
        equity_micros: int,
        now_ns: int | None = None,
        realized_locked_micros: int = 0,
    ) -> MissionFeasibility:
        """Judge one mission against the current window (``A3``).

        ``equity_micros`` is the notional the strategy may size against;
        ``realized_locked_micros`` is profit already banked toward the target.
        A non-positive equity is refused with ``ZERO_EQUITY`` rather than
        divided into, so the gate can never raise ``ZeroDivisionError``.
        """
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        equity = int(equity_micros)
        locked = max(0, int(realized_locked_micros))

        edge = self._edge()
        capped = min(max(edge.kelly_fraction, 0.0), mission.kelly_fraction_cap)
        trades_remaining = max(1, int(mission.days_remaining(stamp) * self._trades_per_day))
        velocity_cap = int(mission.daily_velocity_target_micros(stamp))

        failures: list[str] = []

        if equity <= 0:
            p_reach = 0.0
            p_ruin = 0.0
            daily_velocity = 0
            failures.append(REASON_ZERO_EQUITY)
        else:
            p_reach, p_ruin = self._first_passage(
                target_micros=mission.target_micros,
                risk_budget_micros=mission.risk_budget_micros,
                notional_micros=equity * capped,
                realized_locked_micros=locked,
                steps=trades_remaining,
            )
            daily_velocity = int(
                max(
                    0.0,
                    equity * capped * (edge.mu_bps / BPS_DENOMINATOR) * self._trades_per_day,
                )
            )

        if edge.kelly_fraction <= 0.0:
            failures.append(REASON_NO_EDGE)
        if capped <= 0.0:
            failures.append(REASON_KELLY_CAP_ZERO)
        if p_reach < self._min_p_reach:
            failures.append(REASON_REACH_PROBABILITY_TOO_LOW)
        if p_ruin > self._max_p_ruin:
            failures.append(REASON_RUIN_PROBABILITY_TOO_HIGH)
        if daily_velocity < velocity_cap:
            failures.append(REASON_VELOCITY_BELOW_PACING)
        if mission.is_expired(stamp):
            failures.append(REASON_MISSION_EXPIRED)
        if not _approval_is_live(mission.approval, stamp):
            failures.append(REASON_MISSION_UNAUTHORIZED)

        feasible = not failures
        reasons = tuple(failures)
        if self._default_window:
            reasons = reasons + (REASON_DEFAULT_RESIDUAL_WINDOW,)

        return MissionFeasibility(
            mission_id=mission.mission_id,
            feasible=feasible,
            code=FEASIBLE_CODE if feasible else INFEASIBLE_CODE,
            p_reach=p_reach,
            p_ruin=p_ruin,
            kelly_fraction=edge.kelly_fraction,
            capped_kelly_fraction=capped,
            daily_velocity_micros=daily_velocity,
            velocity_cap_micros=velocity_cap,
            trades_remaining=trades_remaining,
            reasons=reasons,
            residual_samples=len(self._residuals),
        )

    def require_feasible(
        self,
        mission: MissionSpec,
        *,
        equity_micros: int,
        now_ns: int | None = None,
        realized_locked_micros: int = 0,
    ) -> MissionFeasibility:
        """Return the verdict or raise ``422 INFEASIBLE_MISSION`` (``A3``)."""
        verdict = self.assess(
            mission,
            equity_micros=equity_micros,
            now_ns=now_ns,
            realized_locked_micros=realized_locked_micros,
        )
        if not verdict.feasible:
            raise MissionInfeasibleError(
                f"INFEASIBLE_MISSION: {', '.join(verdict.reasons)}"
            )
        return verdict
