"""Environment loading for local model providers."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["_ensure_dotenv"]


def _ensure_dotenv() -> bool:
    """Load ``.env`` once if python-dotenv is installed.

    Never raises: a missing optional dependency or a missing file simply means
    the process environment is the only configuration source.
    """
    if getattr(_ensure_dotenv, "_loaded", False):
        return True
    path = Path(".env")
    if not path.exists():
        _ensure_dotenv._loaded = True  # type: ignore[attr-defined]
        return False
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        _ensure_dotenv._loaded = True  # type: ignore[attr-defined]
        return False
    load_dotenv(path, override=False)
    _ensure_dotenv._loaded = True  # type: ignore[attr-defined]
    return True
