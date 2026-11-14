"""Ring 2 specialist agent roster and task dispatcher (``sec_004``, ``sec_018``).

The dispatcher is the only entry point the Glass Cockpit uses to put a Ring 2
agent to work.  Agents reason about the market and emit grounded reports,
strategy scores and nursery proposals -- never an order, never a broker key.
"""

from __future__ import annotations

from .task_dispatcher import (
    AGENT_CATALOG,
    AGENT_IDS,
    TASK_PRIORITIES,
    TASK_STATUSES,
    AgentSpec,
    AgentTask,
    AgentTaskDispatcher,
    TokenBudgetExceeded,
)

__all__ = [
    "AGENT_CATALOG",
    "AGENT_IDS",
    "AgentSpec",
    "AgentTask",
    "AgentTaskDispatcher",
    "TASK_PRIORITIES",
    "TASK_STATUSES",
    "TokenBudgetExceeded",
]
