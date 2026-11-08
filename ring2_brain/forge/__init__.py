"""Nocturnal Alpha Forge package (Subsystem #8, ``sec_003`` / ``sec_010``)."""

from __future__ import annotations

from .alpha_forge import (
    CHALLENGER,
    LIVE_CHAMPION,
    MIN_WINNING_SHADOW_FILLS,
    PROMOTION_DSR_FLOOR,
    RETIREMENT_FLOOR_BPS,
    SHADOW,
    SHADOW_RETIRED,
    ChampionRecord,
    CVResult,
    FDRResult,
    Fold,
    ForgeDataset,
    ForgeReport,
    NocturnalAlphaForge,
    PurgedKFoldCV,
    StrategyGenome,
    benjamini_hochberg,
    deflated_sharpe_ratio,
    random_genomes,
)

__all__ = [
    "CHALLENGER",
    "LIVE_CHAMPION",
    "MIN_WINNING_SHADOW_FILLS",
    "PROMOTION_DSR_FLOOR",
    "RETIREMENT_FLOOR_BPS",
    "SHADOW",
    "SHADOW_RETIRED",
    "ChampionRecord",
    "CVResult",
    "FDRResult",
    "Fold",
    "ForgeDataset",
    "ForgeReport",
    "NocturnalAlphaForge",
    "PurgedKFoldCV",
    "StrategyGenome",
    "benjamini_hochberg",
    "deflated_sharpe_ratio",
    "random_genomes",
]
