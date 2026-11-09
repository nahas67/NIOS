"""Async dispatcher for Trading-R1 compute tools."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Mapping

from trading_r1.contract.spec import Result, ToolError, ToolSpec

from .tool_registry import ToolRegistry, ToolSessionState

__all__ = ["DispatchResult", "dispatch", "dispatch_json_args"]


@dataclass(slots=True)
class DispatchResult:
    """Outcome of one dispatch: a result or a typed failure reason."""

    name: str
    result: Result | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and not self.error



async def dispatch(
    name: str,
    args: Mapping[str, Any] | Any,
    *,
    registry: ToolRegistry,
    session_state: ToolSessionState | None = None,
    record: bool = True,
) -> Result:
    """Validate ``args`` against the tool's request schema and run it."""
    spec: ToolSpec | None = registry.get(name)
    if spec is None:
        raise ToolError(f"unknown tool {name!r}")
    if spec.handler is None:
        raise ToolError(f"tool {spec.name!r} has no handler")

    request = args
    if isinstance(args, Mapping) and isinstance(spec.request_schema, type):
        request = spec.request_schema(**dict(args))

    produced = spec.handler(request)
    if inspect.isawaitable(produced):
        produced = await produced
    if not isinstance(produced, Result):
        raise ToolError(
            f"tool {spec.name!r} returned {type(produced).__name__}, expected Result"
        )
    if record and session_state is not None:
        session_state.record(spec.name, produced)
    return produced


async def dispatch_json_args(
    name: str,
    args: str | Mapping[str, Any] | None,
    *,
    registry: ToolRegistry,
    session_state: ToolSessionState | None = None,
    record: bool = True,
) -> DispatchResult:
    """Dispatch a wire-format JSON argument object."""
    try:
        payload = json.loads(args) if isinstance(args, str) else dict(args or {})
    except (TypeError, ValueError) as exc:
        return DispatchResult(name=str(name), error=f"invalid JSON arguments: {exc}")
    try:
        result = await dispatch(
            name,
            payload,
            registry=registry,
            session_state=session_state,
            record=record,
        )
    except (ToolError, KeyError, ValueError) as exc:
        return DispatchResult(name=str(name), error=f"{type(exc).__name__}: {exc}")
    return DispatchResult(name=str(name), result=result)
