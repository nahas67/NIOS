"""Run state for the Trading-R1 loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

__all__ = [
    "EventCallback",
    "RunPolicy",
    "RunState",
    "Termination",
    "harness_note",
]

EventCallback = Callable[[Any], None]


class Termination(str, Enum):
    COMPLETED = "completed"
    MAX_ROUNDS = "max_rounds"
    TIMEOUT = "timeout"
    BUDGET_REPORT = "budget_report"
    CONTEXT_FULL = "context_full"
    ERROR = "error"

    @classmethod
    def of_finish(cls, finish_reason: str | None) -> "Termination":
        text = str(finish_reason or "").lower()
        if text in {"stop", "completed", "end_turn"}:
            return cls.COMPLETED
        if text in {"length", "max_tokens"}:
            return cls.BUDGET_REPORT
        return cls.COMPLETED


class RunPolicy(str, Enum):
    """How much autonomy the run has: interactive, scheduled or unattended."""

    INTERACTIVE = "interactive"
    SCHEDULED = "scheduled"
    UNATTENDED = "unattended"


@dataclass(slots=True)
class RunState:
    messages: list[dict[str, Any]] = field(default_factory=list)
    round: int = 0
    termination: Termination | None = None
    error: str | None = None
    policy: "RunPolicy" = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.policy is None:
            self.policy = RunPolicy.INTERACTIVE


def harness_note(text: str) -> str:
    """Wrap system guidance so it is distinguishable from user content."""
    return f"[harness] {text}"
