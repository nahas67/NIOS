"""Decision memory for TradingAgents runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import settlement

__all__ = ["TradingMemoryLog", "Decision", "settlement"]


@dataclass(slots=True)
class Decision:
    ticker: str
    trade_date: str
    action: str
    rationale: str = ""
    settled: bool = False


@dataclass(slots=True)
class TradingMemoryLog:
    """Append-only run memory.

    The log is a JSONL file so a crashed run leaves every prior decision
    readable.  Writes are atomic at the line level (one ``write`` of a complete
    JSON object).
    """

    config: Mapping[str, Any] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)

    def __post_init__(self) -> None:
        path = self.config.get("memory_log_path")
        self.path = Path(str(path)) if path else None

    def record(self, decision: Decision) -> None:
        self.decisions.append(decision)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "ticker": decision.ticker,
                            "trade_date": decision.trade_date,
                            "action": decision.action,
                            "rationale": decision.rationale,
                            "settled": decision.settled,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

    def pending(self) -> list[Decision]:
        return [decision for decision in self.decisions if not decision.settled]
