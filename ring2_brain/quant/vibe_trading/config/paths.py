"""Runtime paths for the harvested Vibe-Trading code."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["get_runtime_root"]


def get_runtime_root() -> Path:
    return Path(os.environ.get("VIBE_RUNTIME_ROOT", Path.cwd() / "runtime")).resolve()
