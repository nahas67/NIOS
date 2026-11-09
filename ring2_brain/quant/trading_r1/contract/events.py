"""Event vocabulary emitted by the Trading-R1 run loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

__all__ = [
    "Compaction",
    "DataRecord",
    "Event",
    "LastMessage",
    "Phase",
    "Reasoning",
    "RoundStart",
    "RunError",
    "Token",
    "ToolCall",
    "ToolResult",
    "Usage",
    "send",
]


class Phase(str, Enum):
    RESEARCH = "research"
    ANSWER = "answer"
    GROUNDING = "grounding"


@dataclass(slots=True)
class Event:
    phase: Phase | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RoundStart(Event):
    round: int = 0


@dataclass(slots=True)
class Token(Event):
    text: str = ""


@dataclass(slots=True)
class Reasoning(Event):
    text: str = ""


@dataclass(slots=True)
class ToolCall(Event):
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult(Event):
    name: str = ""
    output: Any = None


@dataclass(slots=True)
class DataRecord(Event):
    record_id: str = ""


@dataclass(slots=True)
class Usage(Event):
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(slots=True)
class Compaction(Event):
    removed: int = 0


@dataclass(slots=True)
class LastMessage(Event):
    content: str = ""


@dataclass(slots=True)
class RunError(Event):
    message: str = ""


def send(callback: Callable[[Event], None] | None, event: Event) -> None:
    if callback is not None:
        callback(event)
