"""Portfolio constraint application for the harvested engines."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pandas as pd

__all__ = ["apply_constraints_frame", "load_constraints"]


def load_constraints(path: str | Path | None = None) -> dict[str, Any]:
    """Load a constraints document; an absent path means no constraints."""
    if path is None:
        return {}
    target = Path(path)
    if not target.exists():
        return {}
    import json

    return json.loads(target.read_text(encoding="utf-8"))


def apply_constraints_frame(
    frame: pd.DataFrame,
    constraints: Mapping[str, Any] | None = None,
    **_: Any,
) -> pd.DataFrame:
    """Apply row-level constraints and return the surviving frame."""
    result = frame
    rules = dict(constraints or {})
    for column, maximum in (rules.get("max") or {}).items():
        if column in result.columns:
            result = result[result[column].abs() <= float(maximum)]
    for column, minimum in (rules.get("min") or {}).items():
        if column in result.columns:
            result = result[result[column].abs() >= float(minimum)]
    return result
