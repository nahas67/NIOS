"""Reflection over settled decisions."""

from __future__ import annotations

from typing import Any

__all__ = ["Reflector"]


class Reflector:
    """Turn a settled decision into a lesson for the next run."""

    def __init__(self, config: Any = None) -> None:
        self.config = config

    def reflect(self, decision: Any, outcome: Any = None) -> str:
        action = getattr(decision, "action", "unknown")
        return f"prior action {action!r}; review the thesis against the settled outcome"

    __call__ = reflect
