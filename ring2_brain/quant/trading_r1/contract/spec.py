"""Tool specification, request/output contracts, and result envelope.

Shapes follow the harvested Trading-R1 compute modules exactly:

    class RsiOutput(Output):
        rsi: Number
        ...

    SPEC = ToolSpec(name=..., description=..., request_schema=RsiRequest,
                    handler=_handler, output=RsiOutput, tags=(...))

    async def _handler(req): return Result(RsiOutput(...), Provenance(method=...))

The contract is intentionally permissive about values (the compute modules own
their pydantic validation) and strict about identity: every result carries a
provenance record naming the method that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Output",
    "Provenance",
    "Result",
    "ToolError",
    "ToolSpec",
    "hint",
    "rendered",
]


class Output(BaseModel):
    """Base class for a tool's structured output."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Provenance(BaseModel):
    """How a result was produced -- never inferred from the value alone."""

    method: str = ""
    source: str | None = None
    as_of: str | None = None
    notes: str | None = None

    model_config = ConfigDict(extra="allow")


class Result(BaseModel):
    """One tool result: a typed output plus its provenance."""

    output: Any
    provenance: Any = Field(default_factory=Provenance)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def to_dict(self) -> dict[str, Any]:
        output = self.output
        if isinstance(output, BaseModel):
            output = output.model_dump(mode="json")
        provenance = self.provenance
        if isinstance(provenance, BaseModel):
            provenance = provenance.model_dump(mode="json")
        return {"output": output, "provenance": provenance}


class ToolError(Exception):
    """A compute tool refused to produce a number.

    Raised for a numerically undefined request (zero variance, infeasible
    optimisation, implied volatility out of range) -- never for a missing
    vendor or a transport failure, which belong to the data layer.
    """


@dataclass(frozen=True, slots=True)
class _Hint:
    """Display metadata attached through :func:`hint`."""

    values: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]


def hint(**values: Any) -> _Hint:
    """Attach rendering metadata to an ``Annotated`` output field."""
    return _Hint(dict(values))


def rendered(**values: Any) -> dict[str, Any]:
    """Return a ``model_config`` carrying the output's rendering hints."""
    return ConfigDict(json_schema_extra={"rendered": dict(values)})


class ToolSpec(BaseModel):
    """Declarative description of one compute tool."""

    name: str
    display_name: str = ""
    result_kind: str = "compute"
    tier: str = "deferred"
    description: str = ""
    request_schema: Any = None
    handler: Callable[..., Any] | None = None
    output: Any = None
    tags: tuple[str, ...] = ()

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    def model_post_init(self, __context: Any) -> None:  # pragma: no cover - pydantic hook
        if not self.display_name:
            object.__setattr__(self, "display_name", self.name)
