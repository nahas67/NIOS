"""Minimal LLM client surface used by the MiroFish ontology generator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["LLMClient"]


@dataclass(slots=True)
class LLMClient:
    """Provider handle.

    The donor service calls ``chat``/``complete``; a deployment injects the
    concrete client.  Calling without one raises rather than fabricating a
    completion that would become an ontology.
    """

    model: str = ""
    provider: str = "local"
    config: dict[str, Any] | None = None

    def _unconfigured(self) -> None:
        raise RuntimeError("no LLM provider is configured for the MiroFish service")

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        self._unconfigured()

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        self._unconfigured()

    __call__ = chat
