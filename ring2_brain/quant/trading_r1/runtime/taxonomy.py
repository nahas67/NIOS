"""Tool taxonomy used by the prompt assembler."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["DOMAINS", "DOMAIN_SPECS", "classify_tool"]


@dataclass(frozen=True, slots=True)
class DomainSpec:
    name: str
    description: str = ""
    prefixes: tuple[str, ...] = field(default_factory=tuple)


DOMAIN_SPECS: tuple[DomainSpec, ...] = (
    DomainSpec("compute", "Pure-math analytics", ("compute_",)),
    DomainSpec("data", "Market and filing data", ("data_", "get_")),
    DomainSpec("web", "Web research", ("web_", "visit", "search")),
)

DOMAINS: tuple[str, ...] = tuple(spec.name for spec in DOMAIN_SPECS)


def classify_tool(name: str) -> str:
    text = str(name)
    for spec in DOMAIN_SPECS:
        if any(text.startswith(prefix) for prefix in spec.prefixes):
            return spec.name
    return "general"
