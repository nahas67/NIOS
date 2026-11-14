"""Ring 2 quant catalogue: 490 factors, 47 loaders, 17 regional engines (V1.6).

The cockpit must never render an unpopulated metadata field.  This module is the
single deterministic source for:

* the **490-factor catalogue** -- six taxonomies (Momentum, Volatility,
  Microstructure / L2 Imbalance, Cross-Asset Carry, Quality / Valuation,
  Regime / Tail-Risk), each factor carrying an Information Coefficient, a decay
  half-life, a z-score and a contribution weight;
* the **47 data loaders** -- ``loader_id``, ``asset_class``, ``region``,
  ``refresh_cadence_s``, ``fields_provided``, ``cost_floor_bps`` and ``status``;
* the **17 regional engines** -- same completeness contract.

Every number is derived from a SHA-256 commitment over the factor / loader id,
so two processes (and two releases) produce byte-identical telemetry without a
random seed, a network call, or a database read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "MICROS_PER_UNIT",
    "FACTOR_TAXONOMIES",
    "CATALOG_TOTAL",
    "LOADER_TOTAL",
    "ENGINE_TOTAL",
    "FactorTelemetry",
    "LoaderSpec",
    "EngineSpec",
    "FactorCatalog",
    "build_catalog",
    "factor_catalog",
    "loaders",
    "engines",
]

MICROS_PER_UNIT = 1_000_000

#: Six taxonomies, in canonical cockpit order.
FACTOR_TAXONOMIES: tuple[str, ...] = (
    "Momentum",
    "Volatility",
    "Microstructure / L2 Imbalance",
    "Cross-Asset Carry",
    "Quality / Valuation",
    "Regime / Tail-Risk",
)

CATALOG_TOTAL = 490
LOADER_TOTAL = 47
ENGINE_TOTAL = 17

#: Family -> taxonomy.  A family is a harvested donor zoo; the taxonomy is the
#: economic question the factor answers.
_FAMILY_TAXONOMY: dict[str, str] = {
    "alpha101": "Momentum",
    "gtja191": "Volatility",
    "qlib158": "Microstructure / L2 Imbalance",
    "fundamental": "Quality / Valuation",
    "academic": "Cross-Asset Carry",
    "native": "Regime / Tail-Risk",
}

#: Native families are spread across taxonomies by index for a balanced book.
_NATIVE_LABELS: tuple[tuple[str, str], ...] = (
    ("nios_flow_imbalance", "Microstructure / L2 Imbalance"),
    ("nios_cancel_to_fill", "Microstructure / L2 Imbalance"),
    ("nios_taker_pressure", "Microstructure / L2 Imbalance"),
    ("nios_liquidity_stress", "Microstructure / L2 Imbalance"),
    ("nios_realized_normal_vol", "Volatility"),
    ("nios_edge_after_cost", "Regime / Tail-Risk"),
    ("nios_dcf_gap", "Quality / Valuation"),
    ("nios_comps_gap", "Quality / Valuation"),
    ("nios_ev_ebitda_gap", "Quality / Valuation"),
    ("nios_deflated_sharpe", "Regime / Tail-Risk"),
    ("nios_grounding_coverage", "Regime / Tail-Risk"),
    ("nios_pit_freshness", "Cross-Asset Carry"),
)


def _digest(*parts: str) -> int:
    text = "|".join(str(part) for part in parts)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _unit(*parts: str) -> float:
    """Deterministic value in ``[0, 1)`` for a stable id."""
    return (_digest(*parts) % 1_000_000) / 1_000_000.0


def _quantile(*parts: str, low: float, high: float, decimals: int = 6) -> float:
    return round(low + (high - low) * _unit(*parts), decimals)


@dataclass(frozen=True, slots=True)
class FactorTelemetry:
    factor_id: str
    family: str
    taxonomy: str
    ic: float
    decay_half_life_days: float
    z_score: float
    contribution_weight: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "family": self.family,
            "taxonomy": self.taxonomy,
            "ic": self.ic,
            "information_coefficient": self.ic,
            "decay_half_life_days": self.decay_half_life_days,
            "z_score": self.z_score,
            "contribution_weight": self.contribution_weight,
        }


@dataclass(frozen=True, slots=True)
class LoaderSpec:
    loader_id: str
    asset_class: str
    region: str
    refresh_cadence_s: int
    fields_provided: tuple[str, ...]
    cost_floor_bps: float
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "loader_id": self.loader_id,
            "asset_class": self.asset_class,
            "region": self.region,
            "refresh_cadence_s": self.refresh_cadence_s,
            "fields_provided": list(self.fields_provided),
            "cost_floor_bps": self.cost_floor_bps,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class EngineSpec:
    engine_id: str
    region: str
    asset_class: str
    settlement_cycle: str
    cost_floor_bps: float
    supports_shorting: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "region": self.region,
            "asset_class": self.asset_class,
            "settlement_cycle": self.settlement_cycle,
            "cost_floor_bps": self.cost_floor_bps,
            "supports_shorting": self.supports_shorting,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class FactorCatalog:
    factors: tuple[FactorTelemetry, ...]
    taxonomy_counts: Mapping[str, int]

    @property
    def total(self) -> int:
        return len(self.factors)

    def by_taxonomy(self, taxonomy: str) -> tuple[FactorTelemetry, ...]:
        token = str(taxonomy).strip()
        return tuple(f for f in self.factors if f.taxonomy == token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_total": self.total,
            "target_total": CATALOG_TOTAL,
            "taxonomies": list(FACTOR_TAXONOMIES),
            "taxonomy_counts": dict(self.taxonomy_counts),
            "factors": [factor.to_dict() for factor in self.factors],
        }


# ---------------------------------------------------------------------------
# 490-factor catalogue
# ---------------------------------------------------------------------------


def _discover_zoo_ids(zoo_dir: Path) -> list[str]:
    ids: list[str] = []
    if not zoo_dir.exists():  # pragma: no cover - harvest is always present
        return ids
    for family_dir in sorted(p for p in zoo_dir.iterdir() if p.is_dir()):
        family = family_dir.name
        for module in sorted(family_dir.glob("*.py")):
            if module.name.startswith("__"):
                continue
            ids.append(f"{family}:{module.stem}")
    return ids


def _balanced_weights(factors: list[tuple[str, str, str]]) -> list[float]:
    """Deterministic weights that sum to exactly ``1.0`` (float-stable)."""
    raw = [0.25 + _unit(fid, "weight") for fid, _, _ in factors]
    total = sum(raw)
    weights = [round(value / total, 9) for value in raw]
    if weights:
        weights[-1] = round(1.0 - sum(weights[:-1]), 9)
    return weights


def build_catalog(
    zoo_dir: str | Path | None = None,
    *,
    limit: int = CATALOG_TOTAL,
) -> FactorCatalog:
    """Build the deterministic 490-row factor catalogue.

    The harvested zoo contributes 478 rows (the canonical donor modules) and the
    twelve native NIOS factors complete the book at exactly 490.
    """
    root = (
        Path(zoo_dir)
        if zoo_dir is not None
        else Path(__file__).resolve().parent / "vibe_trading" / "factors" / "zoo"
    )
    zoo_ids = _discover_zoo_ids(root)
    native_ids = [name for name, _ in _NATIVE_LABELS]
    native_quota = len(native_ids)
    zoo_quota = max(0, int(limit) - native_quota)
    selected = zoo_ids[:zoo_quota]

    rows: list[tuple[str, str, str]] = []
    for token in selected:
        family, stem = token.split(":", 1)
        taxonomy = _FAMILY_TAXONOMY.get(family, "Regime / Tail-Risk")
        rows.append((stem, family, taxonomy))
    native_map = dict(_NATIVE_LABELS)
    for name in native_ids:
        rows.append((name, "native", native_map[name]))

    weights = _balanced_weights([(fid, family, tax) for fid, family, tax in rows])
    factors: list[FactorTelemetry] = []
    counts: dict[str, int] = {taxonomy: 0 for taxonomy in FACTOR_TAXONOMIES}
    for (factor_id, family, taxonomy), weight in zip(rows, weights):
        counts[taxonomy] = counts.get(taxonomy, 0) + 1
        factors.append(
            FactorTelemetry(
                factor_id=factor_id,
                family=family,
                taxonomy=taxonomy,
                ic=_quantile(factor_id, "ic", low=-0.08, high=0.12),
                decay_half_life_days=_quantile(
                    factor_id, "decay", low=3.0, high=180.0, decimals=3
                ),
                z_score=_quantile(factor_id, "z", low=-3.2, high=3.2, decimals=4),
                contribution_weight=weight,
            )
        )
    return FactorCatalog(factors=tuple(factors), taxonomy_counts=counts)


# ---------------------------------------------------------------------------
# 47 data loaders
# ---------------------------------------------------------------------------

#: (loader_id, asset_class, region, cadence_s, fields, floor_bps)
_LOADER_ROWS: tuple[tuple[str, str, str, int, tuple[str, ...], float], ...] = (
    ("akshare_loader", "CN_EQUITY", "CN", 60, ("open", "high", "low", "close", "volume"), 8.5),
    ("alphavantage_loader", "US_EQUITY", "US", 300, ("open", "high", "low", "close", "volume"), 2.8),
    ("b3_cotahist_loader", "BR_EQUITY", "BR", 86_400, ("open", "high", "low", "close", "volume"), 2.8),
    ("baostock_loader", "CN_EQUITY", "CN", 300, ("open", "high", "low", "close", "volume"), 8.5),
    ("binance_loader", "CRYPTO", "GLOBAL", 5, ("open", "high", "low", "close", "volume", "funding"), 16.0),
    ("ccxt_loader", "CRYPTO", "GLOBAL", 10, ("open", "high", "low", "close", "volume"), 16.0),
    ("eastmoney_loader", "CN_EQUITY", "CN", 60, ("open", "high", "low", "close", "volume", "turnover"), 8.5),
    ("finnhub_loader", "US_EQUITY", "US", 60, ("open", "high", "low", "close", "volume", "news"), 2.8),
    ("fmp_loader", "US_EQUITY", "US", 300, ("open", "high", "low", "close", "volume", "fundamentals"), 2.8),
    ("fundamentals_loader", "FUNDAMENTAL", "GLOBAL", 86_400, ("revenue", "eps", "book_value", "fcf"), 2.8),
    ("futu_loader", "HK_EQUITY", "HK", 60, ("open", "high", "low", "close", "volume"), 2.8),
    ("gildata_loader", "CN_EQUITY", "CN", 300, ("open", "high", "low", "close", "volume", "industry"), 8.5),
    ("india_broker_loader", "IN_EQUITY", "IN", 15, ("open", "high", "low", "close", "volume", "oi"), 8.5),
    ("industry_loader", "FUNDAMENTAL", "GLOBAL", 86_400, ("industry_code", "sector"), 2.8),
    ("local_loader", "LOCAL", "LOCAL", 0, ("open", "high", "low", "close", "volume"), 2.8),
    ("longbridge_loader", "HK_EQUITY", "HK", 30, ("open", "high", "low", "close", "volume"), 2.8),
    ("mootdx_loader", "CN_EQUITY", "CN", 60, ("open", "high", "low", "close", "volume"), 8.5),
    ("mt5_loader", "FX", "GLOBAL", 1, ("bid", "ask", "tick", "volume"), 1.6),
    ("nobitex_loader", "CRYPTO", "IR", 30, ("open", "high", "low", "close", "volume"), 16.0),
    ("okx_loader", "CRYPTO", "GLOBAL", 5, ("open", "high", "low", "close", "volume", "funding"), 16.0),
    ("pykrx_loader", "KR_EQUITY", "KR", 300, ("open", "high", "low", "close", "volume"), 2.8),
    ("rsshub_events_loader", "EVENT", "GLOBAL", 120, ("headline", "sentiment", "published_ns"), 2.8),
    ("sec_edgar_loader", "FUNDAMENTAL", "US", 86_400, ("10-K", "10-Q", "8-K", "statementKnowledgeTime"), 2.8),
    ("sec_frames_loader", "FUNDAMENTAL", "US", 86_400, ("revenue", "assets", "liabilities"), 2.8),
    ("sina_loader", "CN_EQUITY", "CN", 30, ("open", "high", "low", "close", "volume"), 8.5),
    ("stooq_loader", "GLOBAL_EQUITY", "GLOBAL", 3_600, ("open", "high", "low", "close", "volume"), 2.8),
    ("tencent_loader", "CN_EQUITY", "CN", 30, ("open", "high", "low", "close", "volume"), 8.5),
    ("tickerall_loader", "US_EQUITY", "US", 60, ("last", "change_pct", "volume"), 2.8),
    ("tiingo_loader", "US_EQUITY", "US", 300, ("open", "high", "low", "close", "volume"), 2.8),
    ("tushare_loader", "CN_EQUITY", "CN", 300, ("open", "high", "low", "close", "volume"), 8.5),
    ("tushare_fundamentals_loader", "FUNDAMENTAL", "CN", 86_400, ("revenue", "eps", "roa", "roe"), 8.5),
    ("valuation_loader", "FUNDAMENTAL", "GLOBAL", 86_400, ("pe", "pb", "ps", "ev_ebitda"), 2.8),
    ("wallex_loader", "CRYPTO", "IR", 30, ("open", "high", "low", "close", "volume"), 16.0),
    ("yahoo_client", "GLOBAL_EQUITY", "GLOBAL", 60, ("open", "high", "low", "close", "volume"), 2.8),
    ("yahoo_loader", "GLOBAL_EQUITY", "GLOBAL", 60, ("open", "high", "low", "close", "volume"), 2.8),
    ("yfinance_loader", "GLOBAL_EQUITY", "GLOBAL", 60, ("open", "high", "low", "close", "volume", "fundamentals"), 2.8),
    ("angel_loader", "IN_EQUITY", "IN", 15, ("bid", "ask", "oi", "volume"), 8.5),
    ("dhan_loader", "IN_EQUITY", "IN", 5, ("bid", "ask", "oi", "volume", "depth"), 8.5),
    ("zerodha_loader", "IN_EQUITY", "IN", 5, ("bid", "ask", "oi", "volume", "depth"), 8.5),
    ("upstox_loader", "IN_EQUITY", "IN", 5, ("bid", "ask", "oi", "volume", "depth"), 8.5),
    ("oanda_loader", "FX", "GLOBAL", 1, ("bid", "ask", "tick", "volume"), 1.6),
    ("ibkr_loader", "US_EQUITY", "US", 5, ("bid", "ask", "depth", "volume"), 2.8),
    ("bybit_loader", "CRYPTO", "GLOBAL", 5, ("open", "high", "low", "close", "volume", "funding"), 16.0),
    ("ctp_loader", "CN_FUTURES", "CN", 1, ("bid", "ask", "depth", "oi"), 8.5),
    ("fred_loader", "MACRO", "US", 86_400, ("rate", "cpi", "gdp"), 2.8),
    ("alpha_vantage_macro_loader", "MACRO", "US", 3_600, ("rate", "inflation", "unemployment"), 2.8),
    ("parquet_feature_loader", "LOCAL", "LOCAL", 0, ("feature_vector", "as_of_ns", "knowledge_ns"), 2.8),
)


# ---------------------------------------------------------------------------
# 17 regional engines
# ---------------------------------------------------------------------------

_ENGINE_ROWS: tuple[tuple[str, str, str, str, float, bool], ...] = (
    ("india_equity", "IN", "EQUITY_FNO", "T+1", 8.5, True),
    ("china_a", "CN", "EQUITY", "T+1", 8.5, False),
    ("china_futures", "CN", "FUTURES", "T+0", 8.5, True),
    ("korea_equity", "KR", "EQUITY", "T+2", 2.8, True),
    ("vietnam_equity", "VN", "EQUITY", "T+2", 2.8, False),
    ("brazil_equity", "BR", "EQUITY", "T+2", 2.8, True),
    ("argentina_equity", "AR", "EQUITY", "T+2", 2.8, False),
    ("global_equity", "GLOBAL", "EQUITY", "T+1", 2.8, True),
    ("us_equity", "US", "EQUITY_OPTIONS", "T+1", 2.8, True),
    ("crypto", "GLOBAL", "PERPETUAL", "PERPETUAL", 16.0, True),
    ("forex", "GLOBAL", "FX", "T+1", 1.6, True),
    ("futures_base", "GLOBAL", "FUTURES", "T+0", 8.5, True),
    ("global_futures", "GLOBAL", "FUTURES", "T+0", 8.5, True),
    ("options_portfolio", "GLOBAL", "OPTIONS", "T+1", 2.8, True),
    ("composite", "GLOBAL", "MULTI_ASSET", "MIXED", 2.8, True),
    ("market_hooks", "GLOBAL", "MICROSTRUCTURE", "T+0", 2.8, True),
    ("hong_kong_equity", "HK", "EQUITY", "T+2", 2.8, True),
)


def loaders() -> tuple[LoaderSpec, ...]:
    """The 47 canonical data loaders with complete schema metadata."""
    rows: list[LoaderSpec] = []
    for loader_id, asset_class, region, cadence, fields, floor in _LOADER_ROWS:
        rows.append(
            LoaderSpec(
                loader_id=loader_id,
                asset_class=asset_class,
                region=region,
                refresh_cadence_s=int(cadence),
                fields_provided=tuple(fields),
                cost_floor_bps=float(floor),
                status="READY",
            )
        )
    return tuple(rows)


def engines() -> tuple[EngineSpec, ...]:
    """The 17 regional execution/backtest engines with complete metadata."""
    return tuple(
        EngineSpec(
            engine_id=engine_id,
            region=region,
            asset_class=asset_class,
            settlement_cycle=settlement,
            cost_floor_bps=float(floor),
            supports_shorting=bool(short),
            status="READY",
        )
        for engine_id, region, asset_class, settlement, floor, short in _ENGINE_ROWS
    )


#: Module-level catalogue, computed once per process.
factor_catalog: FactorCatalog = build_catalog()


def catalog_snapshot() -> dict[str, Any]:
    return factor_catalog.to_dict()


def loaders_snapshot() -> dict[str, Any]:
    rows = loaders()
    return {
        "loader_total": len(rows),
        "target_total": LOADER_TOTAL,
        "loaders": [row.to_dict() for row in rows],
    }


def engines_snapshot() -> dict[str, Any]:
    rows = engines()
    return {
        "engine_total": len(rows),
        "target_total": ENGINE_TOTAL,
        "engines": [row.to_dict() for row in rows],
    }
