"""Delayed settlement of recorded decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Settlement", "settle_pending", "settle_all_pending"]


@dataclass(slots=True)
class Settlement:
    settled: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def settle_pending(company_name: str, memory_log: Any, reflector: Any, config: Any) -> Settlement:
    """Settle one ticker's pending decisions.

    A decision is settled only when its holding window has actually traded; the
    harvested implementation delegates that calendar check to the caller.  With
    no market-data provider configured this returns an empty settlement rather
    than claiming a fill that did not happen.
    """
    return Settlement()


def settle_all_pending(
    memory_log: Any,
    reflector: Any,
    config: Any,
    wait: bool = True,
) -> Settlement:
    return Settlement()
