"""Session recorder surface for the Trading-R1 loop."""

from __future__ import annotations

from typing import Any

__all__ = ["Recorder", "recorder"]


class Recorder:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, value: Any) -> Any:
        self.records.append(value)
        return value

    def failure_text(self, exc: BaseException) -> str:
        return f"{type(exc).__name__}: {exc}"

    def clear(self) -> None:
        self.records.clear()


recorder = Recorder()
