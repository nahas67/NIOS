"""LLM client factory used by the harvested graph.

Ring 2 may reason about the market but never signs an order, and no broker or
exchange credential belongs in this module.  The factory returns a thin
provider handle; a deployment supplies the concrete chat client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = ["TierClient", "create_tier_client", "tier_provider"]


@dataclass(slots=True)
class TierClient:
    """A provider handle for one reasoning tier (``deep`` or ``quick``)."""

    provider: str
    model: str
    extra: dict[str, Any]

    def get_llm(self) -> Any:
        """Return the chat client.

        The base handle is deliberately inert; a deployment injects the model
        client through the plugin boundary.  Returning ``None`` keeps the
        harvested graph importable without inventing a provider.
        """
        return None

    def with_structured_output(self, schema: Any) -> Any:
        return None


def tier_provider(config: Mapping[str, Any] | None, tier: str = "quick") -> str:
    """Return the configured provider name for ``tier``."""
    config = dict(config or {})
    return str(config.get(f"{tier}_provider") or config.get("provider") or "local")


def create_tier_client(
    config: Mapping[str, Any] | None,
    tier: str = "quick",
    **extra: Any,
) -> TierClient:
    config = dict(config or {})
    provider = tier_provider(config, tier)
    model = str(config.get(f"{tier}_model") or config.get("model") or "")
    return TierClient(provider=provider, model=model, extra=dict(extra))
