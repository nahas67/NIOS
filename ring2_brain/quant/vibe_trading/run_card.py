"""Run-card writer for backtest runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["write_run_card"]


def write_run_card(directory: str | Path, card: Mapping[str, Any] | Any) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "run_card.json"
    if hasattr(card, "to_dict"):
        payload = card.to_dict()
    elif hasattr(card, "__dict__") and not isinstance(card, Mapping):
        payload = dict(vars(card))
    else:
        payload = dict(card)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path
