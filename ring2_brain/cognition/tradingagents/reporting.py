"""Report-tree writer for completed TradingAgents runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["write_report_tree"]


def write_report_tree(
    results_dir: str | Path,
    ticker: str,
    trade_date: str,
    payload: Mapping[str, Any] | str,
    **_: Any,
) -> Path:
    """Write one run's report under ``results_dir/<ticker>/<date>``."""
    target = Path(results_dir) / str(ticker).replace("/", "_") / str(trade_date)
    target.mkdir(parents=True, exist_ok=True)
    artifact = target / "report.json"
    if isinstance(payload, str):
        (target / "report.md").write_text(payload, encoding="utf-8")
    else:
        artifact.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")
    return target
