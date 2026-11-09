"""Agent handle used by the Trading-R1 loop (typing surface)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Agent"]


@dataclass(slots=True)
class Agent:
    model: str = ""
    tools: list[Any] = field(default_factory=list)
    mode: str = "general"

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no model provider is configured for the Trading-R1 agent")
