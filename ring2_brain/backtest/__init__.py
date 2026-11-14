"""Ring 2 backtesting package (``sec_004`` / ``sec_018``).

Exposes the deterministic tax-net walk-forward engine that powers the Cockpit
``/backtest`` workstation and the ``backtest_audit_agent`` specialist.
"""

from __future__ import annotations

from ring2_brain.backtest.engine import (
    CHAMPION_NET_EDGE_FLOOR_BPS,
    DSR_GATE,
    EMBARGO_PCT,
    PURGED_K_FOLDS,
    REPORT_TOLERANCE,
    SILO_COST_FLOORS_BPS,
    BacktestGateError,
    BacktestRun,
    NurseryGateError,
    WalkForwardBacktestEngine,
    silo_cost_floor_bps,
)

__all__ = [
    "CHAMPION_NET_EDGE_FLOOR_BPS",
    "DSR_GATE",
    "EMBARGO_PCT",
    "PURGED_K_FOLDS",
    "REPORT_TOLERANCE",
    "SILO_COST_FLOORS_BPS",
    "BacktestGateError",
    "BacktestRun",
    "NurseryGateError",
    "WalkForwardBacktestEngine",
    "silo_cost_floor_bps",
]
