"""Broker connector stubs referenced by the harvested loaders.

Ring 2 never holds broker credentials or opens a broker socket.  These shims
exist so the loader modules import; every call that would need a live broker
raises instead of silently returning data.
"""

from __future__ import annotations

__all__ = ["sdk"]


class _UnavailableSdk:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(
            "broker SDK access is a Ring 0 responsibility; the Ring 2 loader "
            "cannot open a broker session"
        )


sdk = _UnavailableSdk()
