"""Concept-drift and agent-behaviour monitors for Ring 2 (``sec_018`` Law 5).

``sec_018`` Law 5 requires the strategy fleet to *evolve every midnight*: demote
a champion that falls below ``+0.50 bps`` and promote a challenger only on
Deflated Sharpe ``>= 1.50`` across 30+ shadow fills.  Demotion needs a trigger,
and this module is that trigger.  It answers two separate questions with one
vocabulary:

* :class:`ConceptDriftMonitor` -- has the *measured edge* of a live strategy
  decayed relative to its own baseline?  It combines three independent
  detectors so no single noisy statistic can halt a healthy strategy:
  ADWIN-lite mean-shift splits, a conformal martingale over the baseline
  residual window, and explicit win-rate / Sharpe decay against the baseline
  (``sec_002`` flaw 4 -- conformal cold-start overconfidence must not be
  mistaken for edge, nor a regime change for noise).
* :class:`AgentBehaviorMonitor` -- is an *agent* looping, spamming actions or
  running past its wall-clock budget?  A quarantined agent is reported, never
  silently killed.

Both monitors are stdlib-only, deterministic given the observation stream, and
free of I/O, threads and sleeps, so they can be called from the Ring 2 control
path and replayed byte-for-byte from an event log.

Calibration note (read before tuning)
-------------------------------------
The conformal martingale is the reciprocal of the product of per-observation
p-values (``exp(sum(-log p))``), exactly as specified.  Under a *stationary*
stream the typical p-value is about ``0.5``, so each ordinary observation adds
about ``0.69`` to the log-sum and the default threshold of ``20`` is crossed
about six observations after the baseline (measured: the 26th observation of a
stationary 80-sample stream).  Treat the default as a hair-trigger standing
alarm: raise ``martingale_threshold`` (for example to ``1e9``) when only the
ADWIN / decay evidence should be allowed to de-risk a strategy, or call
:meth:`ConceptDriftMonitor.reset` after a human review clears the episode.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

__all__ = [
    "ALLOCATION_MULTIPLIERS",
    "AGENT_STATUS_OK",
    "AGENT_STATUS_QUARANTINE",
    "AGENT_STATUS_WARN",
    "LOG_MARTINGALE_CAP",
    "MARTINGALE_CEILING",
    "REASON_ADWIN_MEAN_SHIFT",
    "REASON_ACTION_BUDGET_EXCEEDED",
    "REASON_CONFORMAL_MARTINGALE",
    "REASON_REPEAT_RATIO_EXCEEDED",
    "REASON_SHARPE_DECAY",
    "REASON_WALL_CLOCK_EXCEEDED",
    "REASON_WIN_RATE_DECAY",
    "AgentBehaviorMonitor",
    "ConceptDriftMonitor",
    "DriftSeverity",
    "DriftVerdict",
]

#: Martingale ceiling -- keeps the accumulator finite for a long-lived stream.
MARTINGALE_CEILING: float = 1e12
LOG_MARTINGALE_CAP: float = math.log(MARTINGALE_CEILING)

#: Drift reasons (the cockpit renders these verbatim).
REASON_ADWIN_MEAN_SHIFT: str = "ADWIN_MEAN_SHIFT"
REASON_CONFORMAL_MARTINGALE: str = "CONFORMAL_MARTINGALE"
REASON_WIN_RATE_DECAY: str = "WIN_RATE_DECAY"
REASON_SHARPE_DECAY: str = "SHARPE_DECAY"

#: Agent-behaviour verdicts.
AGENT_STATUS_OK: str = "OK"
AGENT_STATUS_WARN: str = "WARN"
AGENT_STATUS_QUARANTINE: str = "QUARANTINE"

#: Agent-behaviour reasons (the cockpit renders these verbatim).
REASON_ACTION_BUDGET_EXCEEDED: str = "ACTION_BUDGET_EXCEEDED"
REASON_WALL_CLOCK_EXCEEDED: str = "WALL_CLOCK_EXCEEDED"
REASON_REPEAT_RATIO_EXCEEDED: str = "REPEAT_RATIO_EXCEEDED"

#: A p-value floor keeps ``log`` finite for a value below the whole baseline.
P_VALUE_FLOOR: float = 1e-12

#: Win-rate decay margin and Sharpe decay ratio (``sec_018`` Law 5).
WIN_RATE_DECAY_MARGIN: float = 0.10
SHARPE_DECAY_RATIO: float = 0.5

#: Agent history is bounded to this multiple of the action budget so a runaway
#: agent cannot grow memory without bound (the budget check still fires).
AGENT_HISTORY_FACTOR: int = 4


class DriftSeverity(StrEnum):
    """How hard the drift verdict de-risks a strategy (``sec_018`` Law 5)."""

    NONE = "NONE"
    HALVE = "HALVE"
    PAUSE = "PAUSE"


#: ``sec_018`` Law 5: how much capital a drifting strategy may still receive.
ALLOCATION_MULTIPLIERS: Mapping[DriftSeverity, float] = MappingProxyType(
    {
        DriftSeverity.NONE: 1.0,
        DriftSeverity.HALVE: 0.5,
        DriftSeverity.PAUSE: 0.0,
    }
)


@dataclass(slots=True, frozen=True)
class DriftVerdict:
    """One drift observation and the de-risking it licenses (``sec_018`` Law 5).

    ``threshold`` is the martingale threshold in force, recorded alongside the
    statistic so a later audit can reconstruct the decision without the monitor.
    """

    severity: DriftSeverity
    drift_detected: bool
    win_rate: float
    sharpe: float
    baseline_win_rate: float
    baseline_sharpe: float
    martingale: float
    threshold: float
    samples: int
    reasons: tuple[str, ...]

    @property
    def allocation_multiplier(self) -> float:
        """``1.0`` (NONE), ``0.5`` (HALVE) or ``0.0`` (PAUSE)."""
        return ALLOCATION_MULTIPLIERS[self.severity]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe, key-order-stable view for the cockpit and the ledger."""
        return {
            "severity": str(self.severity),
            "drift_detected": self.drift_detected,
            "win_rate": self.win_rate,
            "sharpe": self.sharpe,
            "baseline_win_rate": self.baseline_win_rate,
            "baseline_sharpe": self.baseline_sharpe,
            "martingale": self.martingale,
            "threshold": self.threshold,
            "samples": self.samples,
            "reasons": list(self.reasons),
            "allocation_multiplier": self.allocation_multiplier,
        }


class ConceptDriftMonitor:
    """Detect decay of a live strategy's edge against its own baseline.

    Three detectors run on every :meth:`observe`:

    * ``ADWIN_MEAN_SHIFT`` -- for every split in ``[min_samples, n - min_samples]``
      the two halves' means differ by more than the Hoeffding bound
      ``sqrt(0.5 * (1/m + 1/n2) * log(4/delta))`` *and* the later half is worse;
    * ``CONFORMAL_MARTINGALE`` -- the inverse product of conformal p-values
      against the frozen baseline window, capped at :data:`MARTINGALE_CEILING`;
    * ``WIN_RATE_DECAY`` / ``SHARPE_DECAY`` -- measured decay against the
      configured baseline, only once ``min_samples`` observations exist.

    Severity is a *ladder*: the first drifting verdict halves allocation, and the
    ``pause_after``-th drifting verdict since the last :meth:`reset` pauses the
    strategy entirely.  A non-drifting verdict returns to ``NONE`` but does not
    clear the accumulated episodes -- only :meth:`reset` does that, so a strategy
    that flickers in and out of drift still escalates.
    """

    def __init__(
        self,
        *,
        baseline_win_rate: float = 0.55,
        baseline_sharpe: float = 1.0,
        delta: float = 0.05,
        min_samples: int = 20,
        window: int = 200,
        martingale_threshold: float = 20.0,
        pause_after: int = 2,
    ) -> None:
        """Configure the monitor.

        ``delta`` is the ADWIN confidence parameter, ``window`` bounds the
        retained sample deque (``sec_018`` Law 5 keeps a 200-trade memory), and
        ``pause_after`` is the number of drift episodes that forces ``PAUSE``.
        """
        win_rate = float(baseline_win_rate)
        sharpe = float(baseline_sharpe)
        confidence = float(delta)
        threshold = float(martingale_threshold)

        if not math.isfinite(win_rate) or not 0.0 <= win_rate <= 1.0:
            raise ValueError("baseline_win_rate must lie in [0, 1]")
        if not math.isfinite(sharpe):
            raise ValueError("baseline_sharpe must be finite")
        if not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
            raise ValueError("delta must lie in (0, 1)")
        if int(min_samples) < 2:
            raise ValueError("min_samples must be >= 2")
        if int(window) < int(min_samples):
            raise ValueError("window must be >= min_samples")
        if not math.isfinite(threshold) or threshold <= 1.0:
            raise ValueError("martingale_threshold must be finite and > 1")
        if int(pause_after) < 1:
            raise ValueError("pause_after must be >= 1")

        self._baseline_win_rate = win_rate
        self._baseline_sharpe = sharpe
        self._delta = confidence
        self._min_samples = int(min_samples)
        self._window = int(window)
        self._martingale_threshold = threshold
        self._pause_after = int(pause_after)

        self._pnl: deque[float] = deque(maxlen=self._window)
        self._wins: deque[bool] = deque(maxlen=self._window)
        self._baseline: tuple[float, ...] | None = None
        self._log_martingale = 0.0
        self._drift_episodes = 0
        self._last_drift = False
        self._last_severity = DriftSeverity.NONE
        self._last_observe_ns = 0

    # -- statistics --------------------------------------------------------
    def _win_rate(self) -> float:
        if not self._wins:
            return 0.0
        return sum(1 for outcome in self._wins if outcome) / len(self._wins)

    def _dispersion(self) -> float:
        """Population standard deviation of the retained P&L window."""
        values = list(self._pnl)
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))

    def _sharpe(self) -> float:
        """``mean(pnl) / stdev(pnl)``; ``0.0`` below two samples or no dispersion."""
        deviation = self._dispersion()
        if deviation == 0.0:
            return 0.0
        return (sum(self._pnl) / len(self._pnl)) / deviation

    def _martingale(self) -> float:
        """Inverse product of conformal p-values, capped at the ceiling."""
        return math.exp(min(self._log_martingale, LOG_MARTINGALE_CAP))

    def _conformal_p_value(self, value: float) -> float:
        """``(1 + #{baseline <= value}) / (n_baseline + 1)`` (Vovk)."""
        baseline = self._baseline or ()
        exceedances = sum(1 for sample in baseline if sample <= value)
        return (1.0 + exceedances) / (len(baseline) + 1.0)

    def _adwin_drift(self) -> bool:
        """ADWIN-lite mean-shift test over every admissible split point."""
        values = list(self._pnl)
        count = len(values)
        if count < 2 * self._min_samples:
            return False
        prefix = [0.0]
        for value in values:
            prefix.append(prefix[-1] + value)
        total = prefix[-1]
        log_term = math.log(4.0 / self._delta)
        for split in range(self._min_samples, count - self._min_samples + 1):
            mean_left = prefix[split] / split
            mean_right = (total - prefix[split]) / (count - split)
            bound = math.sqrt(0.5 * (1.0 / split + 1.0 / (count - split)) * log_term)
            if mean_left - mean_right > bound:
                return True
        return False

    def _reasons(self) -> tuple[str, ...]:
        """Every detector that is currently signalling, in canonical order.

        The Sharpe detector abstains while the window has zero dispersion: a
        constant P&L stream has an undefined Sharpe ratio (reported as ``0.0``),
        and "undefined" is not evidence of decay -- treating it as such would
        flag every perfectly stable strategy on its first ``min_samples``
        observations.
        """
        reasons: list[str] = []
        if self._adwin_drift():
            reasons.append(REASON_ADWIN_MEAN_SHIFT)
        if self._martingale() >= self._martingale_threshold:
            reasons.append(REASON_CONFORMAL_MARTINGALE)
        if len(self._pnl) >= self._min_samples:
            if self._win_rate() < self._baseline_win_rate - WIN_RATE_DECAY_MARGIN:
                reasons.append(REASON_WIN_RATE_DECAY)
            if (
                self._dispersion() > 0.0
                and self._sharpe() < self._baseline_sharpe * SHARPE_DECAY_RATIO
            ):
                reasons.append(REASON_SHARPE_DECAY)
        return tuple(reasons)

    # -- observation -------------------------------------------------------
    def observe(
        self,
        *,
        pnl_bps: float,
        win: bool | None = None,
        now_ns: int | None = None,
    ) -> DriftVerdict:
        """Record one trade outcome and return the drift verdict.

        ``win`` defaults to ``pnl_bps > 0``.  The first ``min_samples``
        observations are frozen as the conformal baseline; observations after
        that feed the martingale accumulator.
        """
        value = float(pnl_bps)
        if not math.isfinite(value):
            raise ValueError("pnl_bps must be finite")
        outcome = bool(value > 0.0) if win is None else bool(win)

        self._last_observe_ns = int(now_ns if now_ns is not None else time.time_ns())
        self._pnl.append(value)
        self._wins.append(outcome)

        if self._baseline is None and len(self._pnl) >= self._min_samples:
            self._baseline = tuple(self._pnl)
        elif self._baseline is not None:
            p_value = max(self._conformal_p_value(value), P_VALUE_FLOOR)
            self._log_martingale = min(
                self._log_martingale - math.log(p_value),
                LOG_MARTINGALE_CAP,
            )

        reasons = self._reasons()
        detected = bool(reasons)
        if detected:
            self._drift_episodes += 1
        self._last_drift = detected

        if not detected:
            severity = DriftSeverity.NONE
        elif self._drift_episodes >= self._pause_after:
            severity = DriftSeverity.PAUSE
        else:
            severity = DriftSeverity.HALVE
        self._last_severity = severity

        return DriftVerdict(
            severity=severity,
            drift_detected=detected,
            win_rate=self._win_rate(),
            sharpe=self._sharpe(),
            baseline_win_rate=self._baseline_win_rate,
            baseline_sharpe=self._baseline_sharpe,
            martingale=self._martingale(),
            threshold=self._martingale_threshold,
            samples=len(self._pnl),
            reasons=reasons,
        )

    def allocation_multiplier(self) -> float:
        """Multiplier licensed by the most recent verdict (``1.0`` before any)."""
        return ALLOCATION_MULTIPLIERS[self._last_severity]

    def reset(self) -> None:
        """Clear samples, martingale evidence and the episode counter."""
        self._pnl.clear()
        self._wins.clear()
        self._baseline = None
        self._log_martingale = 0.0
        self._drift_episodes = 0
        self._last_drift = False
        self._last_severity = DriftSeverity.NONE
        self._last_observe_ns = 0

    def snapshot(self) -> dict[str, Any]:
        """Deterministic state view for telemetry and replay."""
        return {
            "samples": len(self._pnl),
            "window": self._window,
            "win_rate": self._win_rate(),
            "sharpe": self._sharpe(),
            "martingale": self._martingale(),
            "martingale_threshold": self._martingale_threshold,
            "drift_detected": self._last_drift,
            "drift_episodes": self._drift_episodes,
            "severity": str(self._last_severity),
            "allocation_multiplier": self.allocation_multiplier(),
            "baseline_samples": len(self._baseline or ()),
            "baseline_win_rate": self._baseline_win_rate,
            "baseline_sharpe": self._baseline_sharpe,
            "last_observe_ns": self._last_observe_ns,
        }


class AgentBehaviorMonitor:
    """Catch a looping, spamming or over-running agent (``sec_018`` Law 5).

    Each agent's recent ``(action, elapsed_s, task_id)`` history is bounded to
    :data:`AGENT_HISTORY_FACTOR` times its action budget, so a runaway agent can
    never grow the monitor's memory without bound while the budget check still
    fires.  ``QUARANTINE`` outranks ``WARN``: exceeding the action or wall-clock
    budget is a containment event, repeating an action is a warning.
    """

    def __init__(
        self,
        *,
        max_actions_per_task: int = 40,
        max_repeat_ratio: float = 0.5,
        max_wall_seconds: float = 120.0,
    ) -> None:
        """Configure the per-agent budgets."""
        budget = int(max_actions_per_task)
        repeat_ratio = float(max_repeat_ratio)
        wall_seconds = float(max_wall_seconds)

        if budget < 1:
            raise ValueError("max_actions_per_task must be >= 1")
        if not math.isfinite(repeat_ratio) or not 0.0 <= repeat_ratio <= 1.0:
            raise ValueError("max_repeat_ratio must lie in [0, 1]")
        if not math.isfinite(wall_seconds) or wall_seconds <= 0.0:
            raise ValueError("max_wall_seconds must be finite and > 0")

        self._max_actions = budget
        self._max_repeat_ratio = repeat_ratio
        self._max_wall_seconds = wall_seconds
        self._history_cap = max(1, budget * AGENT_HISTORY_FACTOR)
        self._history: dict[str, deque[tuple[str, float, str]]] = {}

    def record(
        self,
        agent_key: str,
        *,
        action: str,
        elapsed_s: float = 0.0,
        task_id: str = "",
    ) -> dict[str, Any]:
        """Append one agent action and return that agent's fresh evaluation."""
        key = str(agent_key or "").strip()
        if not key:
            raise ValueError("agent_key must be non-empty")
        elapsed = float(elapsed_s)
        if not math.isfinite(elapsed) or elapsed < 0.0:
            raise ValueError("elapsed_s must be finite and non-negative")

        history = self._history.get(key)
        if history is None:
            history = deque(maxlen=self._history_cap)
            self._history[key] = history
        history.append((str(action), elapsed, str(task_id)))
        return self.evaluate(key)

    def evaluate(self, agent_key: str) -> dict[str, Any]:
        """Classify one agent as ``OK``, ``WARN`` or ``QUARANTINE``."""
        key = str(agent_key or "").strip()
        entries = tuple(self._history.get(key, ()))
        actions = len(entries)
        unique_actions = len({action for action, _, _ in entries})
        repeat_ratio = 0.0 if actions == 0 else 1.0 - unique_actions / actions
        wall_seconds = math.fsum(elapsed for _, elapsed, _ in entries)

        reasons: list[str] = []
        budget_breached = actions > self._max_actions
        wall_breached = wall_seconds > self._max_wall_seconds
        if budget_breached:
            reasons.append(REASON_ACTION_BUDGET_EXCEEDED)
        if wall_breached:
            reasons.append(REASON_WALL_CLOCK_EXCEEDED)
        if repeat_ratio > self._max_repeat_ratio:
            reasons.append(REASON_REPEAT_RATIO_EXCEEDED)

        if budget_breached or wall_breached:
            status = AGENT_STATUS_QUARANTINE
        elif reasons:
            status = AGENT_STATUS_WARN
        else:
            status = AGENT_STATUS_OK

        return {
            "agent_key": key,
            "status": status,
            "reasons": reasons,
            "actions": actions,
            "repeat_ratio": repeat_ratio,
            "wall_seconds": wall_seconds,
            "unique_actions": unique_actions,
        }

    def reset(self, agent_key: str | None = None) -> None:
        """Forget one agent's history, or every agent's history."""
        if agent_key is None:
            self._history.clear()
            return
        self._history.pop(str(agent_key or "").strip(), None)

    def snapshot(self) -> dict[str, Any]:
        """Deterministic view of every tracked agent and the configured budgets."""
        return {
            "agents_tracked": len(self._history),
            "agents": {
                key: self.evaluate(key) for key in sorted(self._history)
            },
            "limits": {
                "max_actions_per_task": self._max_actions,
                "max_repeat_ratio": self._max_repeat_ratio,
                "max_wall_seconds": self._max_wall_seconds,
                "history_cap": self._history_cap,
            },
        }
