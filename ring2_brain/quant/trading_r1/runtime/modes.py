"""Execution modes for the Trading-R1 prompt assembly."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["MODES", "ModeSpec"]


@dataclass(frozen=True, slots=True)
class ModeSpec:
    name: str
    description: str = ""
    guidance: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


MODES: dict[str, ModeSpec] = {
    "research": ModeSpec(
        name="research",
        description="Web-first investigation of a company or market question.",
        guidance="Investigate before concluding; cite every material number.",
    ),
    "quant": ModeSpec(
        name="quant",
        description="Numbers-first analysis over supplied series and statements.",
        guidance="Use the compute tools; never estimate what a tool can calculate.",
    ),
    "general": ModeSpec(
        name="general",
        description="General assistant mode.",
        guidance="Answer directly; use tools when they add evidence.",
    ),
}
