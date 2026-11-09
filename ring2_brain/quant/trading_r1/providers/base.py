"""Provider surface used by the Trading-R1 verification loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["AssistantTurn", "Provider", "tool_arguments"]


@dataclass(slots=True)
class AssistantTurn:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"

    def arguments_for(self, index: int) -> dict[str, Any]:
        if 0 <= index < len(self.tool_calls):
            return dict(self.tool_calls[index].get("arguments") or {})
        return {}


@dataclass(slots=True)
class Provider:
    model: str = ""

    async def complete(self, *args: Any, **kwargs: Any) -> AssistantTurn:
        raise RuntimeError("no provider is configured for the Trading-R1 loop")


def tool_arguments(call: Any) -> dict[str, Any]:
    """Return a tool call's arguments as a mapping."""
    if call is None:
        return {}
    if isinstance(call, dict):
        args = call.get("arguments", call)
        return dict(args) if isinstance(args, dict) else {}
    args = getattr(call, "arguments", None)
    if isinstance(args, str):
        import json

        try:
            parsed = json.loads(args)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(args) if isinstance(args, dict) else {}
