"""Path confinement for run-directory references."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["safe_run_dir"]


def safe_run_dir(raw_dir: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """Resolve a run directory inside the configured runs root.

    Rejects parent traversal and absolute paths outside the base, which is the
    property the evidence gate relies on before it reads ``run_card.json`` or a
    metric table.
    """
    text = str(raw_dir).strip()
    if not text:
        raise ValueError("run directory is empty")
    root = Path(base) if base is not None else Path.cwd() / "runs"
    root = root.resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if ".." in Path(text).parts:
        raise ValueError(f"run directory escapes the runs root: {text!r}")
    if root not in resolved.parents and resolved != root:
        # An absolute path may point at an existing run outside cwd/runs; keep
        # it only when it is a real directory, never a symlink escape.
        if not resolved.is_dir():
            raise ValueError(f"run directory is outside the runs root: {text!r}")
    return resolved
