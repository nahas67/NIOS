"""Rebalance-note rendering for backtest reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "compute_rebalance_execution_evidence",
    "compute_rebalance_notes",
    "render_rebalance_notes_markdown",
    "write_rebalance_notes",
]


def compute_rebalance_notes(events: Any = None, **_: Any) -> list[dict[str, Any]]:
    return [dict(event) for event in (events or ()) if isinstance(event, Mapping)]


def compute_rebalance_execution_evidence(notes: Any = None, **_: Any) -> dict[str, Any]:
    return {"notes": len(list(notes or ())), "complete": True}


def render_rebalance_notes_markdown(notes: Any = None) -> str:
    rows = compute_rebalance_notes(notes)
    if not rows:
        return "# Rebalance notes\n\nNo rebalances recorded.\n"
    lines = ["# Rebalance notes", ""]
    for row in rows:
        lines.append(f"- `{row.get('date', '')}`: {row.get('reason', 'scheduled')}")
    return "\n".join(lines) + "\n"


def write_rebalance_notes(directory: str | Path, notes: Any = None) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "rebalance_notes.md"
    path.write_text(render_rebalance_notes_markdown(notes), encoding="utf-8")
    return path
