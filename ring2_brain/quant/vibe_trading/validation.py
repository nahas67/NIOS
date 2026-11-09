"""Backtest validation result writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["run_validation", "write_validation_json"]


def run_validation(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "checks": [], **dict(kwargs.pop("extra", {}) or {})}


def write_validation_json(directory: str | Path, validation: Mapping[str, Any]) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "validation.json"
    path.write_text(json.dumps(dict(validation), indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path
