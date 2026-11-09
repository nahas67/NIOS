"""Perpetual-evidence report helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["SCHEMA_VERSION", "build_perpetual_summary", "write_perpetual_evidence"]

SCHEMA_VERSION = "1.0"


def build_perpetual_summary(records: Any = None, **_: Any) -> dict[str, Any]:
    values = [record for record in (records or ()) if isinstance(record, Mapping)]
    return {"schema_version": SCHEMA_VERSION, "count": len(values), "records": values}


def write_perpetual_evidence(directory: str | Path, summary: Mapping[str, Any]) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "perpetual_evidence.json"
    path.write_text(json.dumps(dict(summary), indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path
