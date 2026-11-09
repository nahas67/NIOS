"""Minimal Trading-R1 runtime used by the harvested compute tools.

Only the surface the compute tree imports is provided: a registry of
``ToolSpec`` objects, a per-run session state, and an async dispatcher.  It is
deliberately network-free -- the compute family is pure math (``sec_007``).
"""

from __future__ import annotations

from .dispatch import dispatch
from .tool_registry import ToolRegistry, ToolSessionState

__all__ = ["ToolRegistry", "ToolSessionState", "dispatch"]
