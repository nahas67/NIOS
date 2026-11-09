"""Atomic file replacement for vendor caches."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["replace_file"]


def replace_file(path: str | os.PathLike[str], content: str | bytes, *, encoding: str = "utf-8") -> Path:
    """Write ``content`` to ``path`` atomically.

    A partially written cache file is worse than a missing one: a later reader
    cannot tell it from a complete response.  The temporary file lives in the
    same directory, so ``os.replace`` is atomic on Windows and POSIX.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode(encoding) if isinstance(content, str) else bytes(content)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
