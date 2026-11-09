"""Registry and per-run state for Trading-R1 tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from trading_r1.contract.spec import Result, ToolSpec

__all__ = ["ToolRegistry", "ToolSessionState"]


@dataclass(slots=True)
class ToolSessionState:
    """State scoped to one agent run.

    ``results`` holds the structured result of every recorded call, keyed by
    wire name, so ``prev:<tool>.<path>`` references can resolve without a
    second dispatch.
    """

    values: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Result] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def record(self, name: str, result: Result) -> None:
        self.results[str(name)] = result

    def get(self, name: str) -> Result | None:
        return self.results.get(str(name))


class ToolRegistry:
    """An ordered collection of :class:`ToolSpec` objects."""

    def __init__(self, specs: Iterable[ToolSpec] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs or ():
            self.register(spec)

    def register(self, spec: ToolSpec) -> ToolSpec:
        if not isinstance(spec, ToolSpec):
            raise TypeError(f"expected ToolSpec, got {type(spec).__name__}")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(str(name))

    def require(self, name: str) -> ToolSpec:
        spec = self.get(name)
        if spec is None:
            raise KeyError(f"unknown tool {name!r}")
        return spec

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def __contains__(self, name: object) -> bool:
        return str(name) in self._specs

    def __len__(self) -> int:
        return len(self._specs)
