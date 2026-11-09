"""Shoonya connector stub (Ring 0 owns broker sessions)."""

from __future__ import annotations

__all__ = ["sdk"]


class _UnavailableSdk:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(
            "Shoonya SDK access is a Ring 0 responsibility; Ring 2 cannot open "
            "a broker session"
        )


sdk = _UnavailableSdk()
