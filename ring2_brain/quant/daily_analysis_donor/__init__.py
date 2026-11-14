"""Ring 2 harvest of ``ZhuLinsen/daily_stock_analysis``.

The donor repository is an MIT-licensed multi-market research assistant.  Ring 2 keeps
exactly two of its surfaces — the declarative strategy template set and the per-symbol
decision dashboard — and re-expresses both as deterministic, air-gapped, fully grounded
Python.  Everything that made the donor a *service* (its scheduler, notifiers, LLM chat
router, storage layer, web UI and broker-adjacent paths) was deliberately excluded: it
would either cross a ring boundary or put a non-deterministic component on a decision
path (``sec_002`` flaws 1 and 6).

Public surface
--------------
``strategies``
    Eight :class:`StrategySpec` templates plus pure indicator primitives and a
    reproducible OHLCV fixture generator.
``decision_dashboard``
    :class:`DecisionDashboard`, which seals every indicator into a
    :class:`core.schemas.GroundedFactPacket` before rendering operator prose.

The harvested names never collide with the upstream ``strategies`` namespace because
``ring2_brain._donor_compat`` only aliases ``src``/``backtest``/``trading_r1``/
``tradingagents``; nothing here is reachable through a donor alias.
"""

from __future__ import annotations

from ring2_brain.quant.daily_analysis_donor.decision_dashboard import (
    ADVICE_BANDS,
    MACD_STATUSES,
    OPERATION_ADVICE,
    RSI_STATUSES,
    SIGNAL_SCORE_RANGE,
    DecisionDashboard,
    DecisionReport,
)
from ring2_brain.quant.daily_analysis_donor.strategies import (
    KNOWN_SILOS,
    STRATEGY_CATALOG,
    STRATEGY_IDS,
    PriceBar,
    StrategySignal,
    StrategySpec,
    evaluate_all_strategies,
    evaluate_strategy,
    get_strategy,
    list_strategies,
    strategy_ids,
    synthetic_bars,
)

__all__ = [
    "ADVICE_BANDS",
    "DecisionDashboard",
    "DecisionReport",
    "KNOWN_SILOS",
    "MACD_STATUSES",
    "OPERATION_ADVICE",
    "PriceBar",
    "RSI_STATUSES",
    "SIGNAL_SCORE_RANGE",
    "STRATEGY_CATALOG",
    "STRATEGY_IDS",
    "StrategySignal",
    "StrategySpec",
    "evaluate_all_strategies",
    "evaluate_strategy",
    "get_strategy",
    "list_strategies",
    "strategy_ids",
    "synthetic_bars",
]
