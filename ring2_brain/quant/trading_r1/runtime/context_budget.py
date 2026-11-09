"""Token-budget estimates for tool schemas and messages."""

from __future__ import annotations

import json
from typing import Any

__all__ = ["estimate_tokens", "schema_tokens"]


def estimate_tokens(text: Any) -> int:
    """Cheap character-based estimate (four characters per token)."""
    if text is None:
        return 0
    if not isinstance(text, str):
        try:
            text = json.dumps(text, default=str)
        except Exception:
            text = str(text)
    return max(1, (len(text) + 3) // 4)


def schema_tokens(schema: Any) -> int:
    if hasattr(schema, "model_json_schema"):
        try:
            return estimate_tokens(schema.model_json_schema())
        except Exception:
            return 0
    return estimate_tokens(schema)
