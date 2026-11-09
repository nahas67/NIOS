"""Default run configuration for the harvested TradingAgents graph."""

from __future__ import annotations

import tempfile
from pathlib import Path

__all__ = ["DEFAULT_CONFIG"]

_BASE = Path(tempfile.gettempdir()) / "nios_tradingagents"

DEFAULT_CONFIG: dict[str, object] = {
    "data_cache_dir": str(_BASE / "cache"),
    "results_dir": str(_BASE / "results"),
    "memory_log_path": str(_BASE / "memory" / "decisions.jsonl"),
    "checkpoint_enabled": False,
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    "max_tool_rounds": 3,
    "output_language": "English",
    "llm_max_retries": 3,
    "global_news_lookback_days": 7,
}
