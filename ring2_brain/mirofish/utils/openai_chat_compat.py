"""OpenAI-compatible chat helpers for the MiroFish services."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["create_chat_completion", "extract_chat_completion_text"]


def create_chat_completion(
    messages: Sequence[Mapping[str, Any]],
    **kwargs: Any,
) -> Any:
    """Create a chat completion.

    The base implementation refuses rather than fabricating content: the
    harvested service must be given a concrete provider, because a fake
    completion would become a fake ontology.
    """
    raise RuntimeError("no OpenAI-compatible provider is configured for MiroFish")


def extract_chat_completion_text(response: Any) -> str:
    """Extract assistant text from an OpenAI-shaped response."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is not None:
            return str(getattr(message, "content", "") or "")
    if isinstance(response, Mapping):
        choices = response.get("choices") or []
        if choices:
            return str((choices[0].get("message") or {}).get("content") or "")
    return str(response)
