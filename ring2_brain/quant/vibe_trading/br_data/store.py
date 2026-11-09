"""Local research-data store (SQLite)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

__all__ = ["db_path", "connect"]


def db_path() -> Path:
    return Path(os.environ.get("VIBE_BR_DB", Path.cwd() / "runtime" / "br_data.sqlite3"))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)
