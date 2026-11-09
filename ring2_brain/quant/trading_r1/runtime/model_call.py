"""Model-call recovery policy names for the Trading-R1 loop."""

from __future__ import annotations

from enum import Enum

__all__ = [
    "CONTEXT_FULL",
    "ContextFull",
    "Purpose",
    "Trimmed",
    "WrapUpDue",
]

CONTEXT_FULL = "context_full"


class Purpose(str, Enum):
    RESEARCH = "research"
    FINAL = "final"
    OPTIONAL = "optional"


class ContextFull(Exception):
    """The model context is full and must be compacted."""


class WrapUpDue(Exception):
    """The run's wall-clock budget is due; write up what is known."""


class Trimmed(Exception):
    """A tool result was trimmed before it entered the context."""
