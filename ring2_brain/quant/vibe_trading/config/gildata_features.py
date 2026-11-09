"""Feature-flag lookups for the Gildata vendor."""

from __future__ import annotations

import os

__all__ = ["feature_available"]


def feature_available(name: str) -> bool:
    key = f"GILDATA_FEATURE_{str(name).strip().upper().replace('-', '_')}"
    return str(os.environ.get(key, "")).strip().lower() in {"1", "true", "yes", "on"}
