"""Runtime configuration for the MiroFish services."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["Config"]


class Config:
    """Environment-backed settings.

    The donor services read these as class attributes (``Config.LLM_API_KEY``)
    and as a mapping (``config.get(...)``); both shapes are supported.  Secrets
    are never written to disk here -- they are read from the process
    environment at call time.
    """

    LLM_API_KEY: str = os.environ.get("MIROFISH_LLM_API_KEY", "")
    LLM_BASE_URL: str = os.environ.get("MIROFISH_LLM_BASE_URL", "")
    LLM_MODEL_NAME: str = os.environ.get("MIROFISH_LLM_MODEL", "")
    UPLOAD_FOLDER: str = os.environ.get("MIROFISH_UPLOAD_FOLDER", str(Path.cwd() / "uploads"))
    MAX_ONTOLOGY_TYPES: int = 12
    SIMULATION_ROUNDS: int = 3

    @classmethod
    def get(cls, key: str, default: object = None) -> object:
        return getattr(cls, str(key), default)

    @classmethod
    def as_dict(cls) -> dict[str, object]:
        return {
            name: value
            for name, value in vars(cls).items()
            if name.isupper()
        }
