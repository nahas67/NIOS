"""Risk-xray report helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "average_invested_weights",
    "compute_risk_xray",
    "render_risk_xray_markdown",
    "write_risk_xray",
]


def average_invested_weights(weights: Any) -> dict[str, float]:
    if hasattr(weights, "mean"):
        series = weights.mean()
        return {str(key): float(value) for key, value in series.items()}
    return {}


def compute_risk_xray(weights: Any = None, **_: Any) -> dict[str, Any]:
    averages = average_invested_weights(weights)
    concentration = float(max((abs(value) for value in averages.values()), default=0.0))
    return {"average_weights": averages, "max_weight": concentration}


def render_risk_xray_markdown(xray: dict[str, Any]) -> str:
    lines = ["# Risk X-Ray", "", f"Max weight: {xray.get('max_weight', 0.0):.2%}", ""]
    for name, weight in sorted((xray.get("average_weights") or {}).items()):
        lines.append(f"- `{name}`: {weight:.2%}")
    return "\n".join(lines) + "\n"


def write_risk_xray(directory: str | Path, xray: dict[str, Any]) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "risk_xray.md"
    path.write_text(render_risk_xray_markdown(xray), encoding="utf-8")
    return path
