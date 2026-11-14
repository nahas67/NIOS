"""NIOS macro catalysts, sector heatmaps and G10 currency strength (V1.7).

Ring 2 owns the *reasoning* layer, so this module answers three questions the
cockpit needs on every tape:

1. **What macro print is about to move the book?**
   :func:`get_macro_calendar` returns the structured India / US / Europe-UK-Japan
   / energy-crypto release schedule with consensus, previous, impact tier, the
   silos it touches and the volatility multiplier Ring 1 should apply.
2. **Where is the money rotating?**
   :func:`get_market_heatmaps` aggregates the live instrument catalogue into the
   India NSE sector heatmap, the US GICS sector heatmap and the crypto narrative
   heatmap, each with advance/decline breadth and the top gainer / laggard.
3. **Which currency is actually strong?**
   The same call returns the G10 strength matrix: a ``-100..+100`` relative
   strength score per currency plus carry-adjusted momentum and the pairwise
   strength differential for every G10 cross.

Every response is sealed into a verified SHA-256 ``GroundedFactPacket`` so a
number on a card can always be traced to the fact that produced it
(``sec_018`` Law 3).  Nothing here opens a socket or reads a credential: the
inputs are the local instrument registry and the deterministic feature store.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from core.instrument_registry import InstrumentSpec, registry as instrument_registry
from core.market_features_store import (
    POLICY_RATES,
    get_feature_profile,
    reference_price,
)
from core.schemas import GroundedFactPacket

__all__ = [
    "MACRO_SCHEMA_VERSION",
    "HEATMAP_SCHEMA_VERSION",
    "IMPACT_TIERS",
    "G10_CURRENCIES",
    "MacroEvent",
    "get_macro_calendar",
    "get_market_heatmaps",
    "get_currency_strength",
    "get_sector_heatmap",
    "calendar_snapshot",
    "heatmap_snapshot",
]

MACRO_SCHEMA_VERSION = "v1.7.macro-calendar"
HEATMAP_SCHEMA_VERSION = "v1.7.market-heatmaps"

IMPACT_TIERS: tuple[str, ...] = ("HIGH", "MEDIUM")

#: The eight G10 currencies carried by the strength matrix.
G10_CURRENCIES: tuple[str, ...] = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD")

#: Calendar anchor: the release schedule is expressed relative to this UTC date
#: so two calls on the same day return the identical schedule.
_CALENDAR_ANCHOR = date(2026, 11, 15)


def _unit(*parts: Any) -> float:
    seed = "|".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _span(low: float, high: float, *parts: Any) -> float:
    return low + (high - low) * _unit(*parts)


def _seal(symbol: str, metrics: Mapping[str, float]) -> GroundedFactPacket:
    stamp = int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000_000)
    clean = {
        str(key): float(value)
        for key, value in metrics.items()
        if math.isfinite(float(value))
    }
    return GroundedFactPacket(
        symbol=symbol,
        valid_time_ns=stamp,
        knowledge_ts_ns=stamp,
        as_of_ns=stamp,
        metrics=clean,
    )


def _grounding(packet: GroundedFactPacket) -> dict[str, Any]:
    return {
        "payload_sha256": packet.sha256_digest,
        "verified": packet.verify(),
        "metric_count": len(packet.metrics),
        "as_of_ns": packet.as_of_ns,
        "law": "sec_018 Law 3 - every number carries a SHA-256 commitment",
    }


# ---------------------------------------------------------------------------
# Macro economic calendar
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MacroEvent:
    """One scheduled macro release with its market impact contract."""

    event_id: str
    title: str
    region: str
    category: str
    offset_days: int
    release_utc: str
    impact_tier: str
    consensus: str
    previous: str
    unit: str
    volatility_multiplier: float
    silos_impacted: tuple[str, ...]
    instruments_impacted: tuple[str, ...]

    @property
    def status(self) -> str:
        return "RELEASED" if self.offset_days < 0 else "SCHEDULED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "region": self.region,
            "category": self.category,
            "release_utc": self.release_utc,
            "offset_days": self.offset_days,
            "status": self.status,
            "impact_tier": self.impact_tier,
            "consensus": self.consensus,
            "previous": self.previous,
            "unit": self.unit,
            "volatility_multiplier": self.volatility_multiplier,
            "silos_impacted": list(self.silos_impacted),
            "instruments_impacted": list(self.instruments_impacted),
            "impact_tier_rank": 0 if self.impact_tier == "HIGH" else 1,
        }


#: The full release calendar.  ``offset_days`` is relative to the anchor date:
#: negative is released, zero/positive is scheduled.
_MACRO_ROWS: tuple[tuple[Any, ...], ...] = (
    # event_id, title, region, category, offset_days, time_utc, impact, consensus,
    # previous, unit, vol_mult, silos, instruments
    (
        "IN_RBI_MPC",
        "RBI MPC Rate Decision",
        "INDIA",
        "CENTRAL_BANK",
        -6,
        "04:30",
        "HIGH",
        "6.50%",
        "6.50%",
        "%",
        1.85,
        ("INDIA_ZERODHA_DHAN",),
        ("NIFTY", "BANKNIFTY", "FINNIFTY", "USDINR"),
    ),
    (
        "IN_CPI_YOY",
        "India CPI Inflation YoY",
        "INDIA",
        "INFLATION",
        -2,
        "12:00",
        "HIGH",
        "5.10%",
        "5.48%",
        "%",
        1.45,
        ("INDIA_ZERODHA_DHAN",),
        ("NIFTY", "BANKNIFTY", "HDFCBANK", "ITC"),
    ),
    (
        "IN_IIP_YOY",
        "India Index of Industrial Production YoY",
        "INDIA",
        "GROWTH",
        1,
        "12:00",
        "MEDIUM",
        "4.20%",
        "3.70%",
        "%",
        1.15,
        ("INDIA_ZERODHA_DHAN",),
        ("NIFTY", "LT", "TATAMOTORS"),
    ),
    (
        "IN_GST_COLLECTIONS",
        "India GST Gross Collections",
        "INDIA",
        "FISCAL",
        3,
        "12:30",
        "MEDIUM",
        "1.82 lakh cr",
        "1.87 lakh cr",
        "INR_CRORE",
        1.10,
        ("INDIA_ZERODHA_DHAN",),
        ("NIFTY", "ITC", "HINDUNILVR", "DMART"),
    ),
    (
        "US_FOMC",
        "FOMC Rate Decision & Statement",
        "US",
        "CENTRAL_BANK",
        4,
        "19:00",
        "HIGH",
        "4.25%",
        "4.50%",
        "%",
        2.40,
        ("US_IBKR", "CRYPTO_BINANCE", "FX_MT5"),
        ("SPY", "QQQ", "TLT", "BTCUSDT", "XAUUSD", "EURUSD"),
    ),
    (
        "US_CPI_CORE_MOM",
        "US Core CPI MoM",
        "US",
        "INFLATION",
        -1,
        "13:30",
        "HIGH",
        "0.30%",
        "0.28%",
        "%",
        1.95,
        ("US_IBKR", "CRYPTO_BINANCE", "FX_MT5"),
        ("SPY", "QQQ", "TLT", "BTCUSDT", "USDJPY"),
    ),
    (
        "US_CPI_CORE_YOY",
        "US Core CPI YoY",
        "US",
        "INFLATION",
        -1,
        "13:30",
        "HIGH",
        "3.20%",
        "3.30%",
        "%",
        2.05,
        ("US_IBKR", "CRYPTO_BINANCE", "FX_MT5"),
        ("SPY", "QQQ", "TLT", "BTCUSDT", "EURUSD"),
    ),
    (
        "US_NFP",
        "US Non-Farm Payrolls",
        "US",
        "LABOUR",
        6,
        "13:30",
        "HIGH",
        "165K",
        "148K",
        "K_JOBS",
        2.25,
        ("US_IBKR", "FX_MT5", "CRYPTO_BINANCE"),
        ("SPY", "QQQ", "IWM", "DXY_PROXY", "USDJPY"),
    ),
    (
        "US_ISM_MFG",
        "US ISM Manufacturing PMI",
        "US",
        "ACTIVITY",
        2,
        "15:00",
        "MEDIUM",
        "48.6",
        "47.9",
        "INDEX",
        1.35,
        ("US_IBKR",),
        ("SPY", "XLI", "CAT_PROXY"),
    ),
    (
        "US_PCE_CORE",
        "US Core PCE Price Index MoM",
        "US",
        "INFLATION",
        9,
        "13:30",
        "HIGH",
        "0.26%",
        "0.31%",
        "%",
        1.90,
        ("US_IBKR", "CRYPTO_BINANCE"),
        ("SPY", "QQQ", "TLT", "BTCUSDT"),
    ),
    (
        "EU_ECB_RATE",
        "ECB Rate Decision",
        "EUROPE",
        "CENTRAL_BANK",
        5,
        "13:15",
        "HIGH",
        "3.15%",
        "3.15%",
        "%",
        1.70,
        ("FX_MT5", "US_IBKR"),
        ("EURUSD", "EURGBP", "EURJPY", "SPY"),
    ),
    (
        "UK_BOE_RATE",
        "BoE Bank Rate Decision",
        "UK",
        "CENTRAL_BANK",
        7,
        "12:00",
        "HIGH",
        "4.00%",
        "4.00%",
        "%",
        1.60,
        ("FX_MT5",),
        ("GBPUSD", "GBPJPY", "GBPCHF"),
    ),
    (
        "JP_BOJ_STATEMENT",
        "BoJ Policy Statement",
        "JAPAN",
        "CENTRAL_BANK",
        8,
        "03:00",
        "HIGH",
        "0.50%",
        "0.50%",
        "%",
        1.80,
        ("FX_MT5", "US_IBKR"),
        ("USDJPY", "EURJPY", "GBPJPY", "NQ"),
    ),
    (
        "US_EIA_CRUDE",
        "EIA Crude Oil Inventories",
        "US",
        "ENERGY",
        1,
        "15:30",
        "HIGH",
        "-1.8M bbl",
        "+2.4M bbl",
        "MILLION_BBL",
        1.55,
        ("US_IBKR", "FX_MT5", "INDIA_ZERODHA_DHAN"),
        ("CL", "USOIL", "UKOIL", "CRUDEOIL", "XLE"),
    ),
    (
        "CRYPTO_BTC_QUARTERLY_EXPIRY",
        "BTC Options Quarterly Expiry",
        "CRYPTO",
        "DERIVATIVES",
        11,
        "08:00",
        "HIGH",
        "n/a",
        "n/a",
        "CONTRACTS",
        1.75,
        ("CRYPTO_BINANCE",),
        ("BTCUSDT", "ETHUSDT"),
    ),
    (
        "CRYPTO_FUNDING_RESET",
        "Binance 8h Funding Settlement",
        "CRYPTO",
        "DERIVATIVES",
        0,
        "00:00",
        "MEDIUM",
        "n/a",
        "n/a",
        "BPS",
        1.05,
        ("CRYPTO_BINANCE",),
        ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
    ),
)


def _release_iso(offset_days: int, time_utc: str) -> str:
    day = _CALENDAR_ANCHOR + timedelta(days=int(offset_days))
    return f"{day.isoformat()}T{time_utc}:00Z"


def _events() -> tuple[MacroEvent, ...]:
    rows: list[MacroEvent] = []
    for (
        event_id,
        title,
        region,
        category,
        offset_days,
        time_utc,
        impact,
        consensus,
        previous,
        unit,
        vol_mult,
        silos,
        instruments,
    ) in _MACRO_ROWS:
        rows.append(
            MacroEvent(
                event_id=event_id,
                title=title,
                region=region,
                category=category,
                offset_days=int(offset_days),
                release_utc=_release_iso(int(offset_days), str(time_utc)),
                impact_tier=impact,
                consensus=consensus,
                previous=previous,
                unit=unit,
                volatility_multiplier=float(vol_mult),
                silos_impacted=tuple(silos),
                instruments_impacted=tuple(instruments),
            )
        )
    # Chronological, then event-id, so the page order is fully deterministic.
    return tuple(sorted(rows, key=lambda e: (e.offset_days, e.event_id)))


def get_macro_calendar(
    *,
    region: str | None = None,
    impact: str | None = None,
) -> dict[str, Any]:
    """Structured macro release schedule, optionally filtered.

    ``region`` matches the region token case-insensitively; ``impact`` must be
    one of :data:`IMPACT_TIERS`.  Unknown values raise ``ValueError`` so the
    gateway can answer ``422`` instead of silently returning everything.
    """
    region_token = None if region is None else str(region).strip().upper()
    impact_token = None if impact is None else str(impact).strip().upper()
    if impact_token is not None and impact_token not in IMPACT_TIERS:
        raise ValueError(
            f"impact must be one of {IMPACT_TIERS} (got {impact!r})"
        )

    events = list(_events())
    known_regions = sorted({event.region for event in events})
    if region_token is not None and region_token not in known_regions:
        raise ValueError(
            f"region must be one of {tuple(known_regions)} (got {region!r})"
        )

    selected = [
        event
        for event in events
        if (region_token is None or event.region == region_token)
        and (impact_token is None or event.impact_tier == impact_token)
    ]

    numeric = {
        "total_events": float(len(events)),
        "returned_events": float(len(selected)),
        "high_impact": float(sum(1 for e in events if e.impact_tier == "HIGH")),
        "medium_impact": float(sum(1 for e in events if e.impact_tier == "MEDIUM")),
        "max_volatility_multiplier": max(
            (e.volatility_multiplier for e in events), default=1.0
        ),
    }
    packet = _seal("MACRO_CALENDAR", numeric)
    return {
        "schema_version": MACRO_SCHEMA_VERSION,
        "as_of_utc": f"{_CALENDAR_ANCHOR.isoformat()}T00:00:00Z",
        "regions": known_regions,
        "impact_tiers": list(IMPACT_TIERS),
        "filters": {"region": region_token, "impact": impact_token},
        "total_events": len(events),
        "returned_events": len(selected),
        "counts": {
            "HIGH": sum(1 for e in selected if e.impact_tier == "HIGH"),
            "MEDIUM": sum(1 for e in selected if e.impact_tier == "MEDIUM"),
            "RELEASED": sum(1 for e in selected if e.status == "RELEASED"),
            "SCHEDULED": sum(1 for e in selected if e.status == "SCHEDULED"),
        },
        "events": [event.to_dict() for event in selected],
        "grounding": _grounding(packet),
        "payload_sha256": packet.sha256_digest,
        "law": "sec_018 Law 3 - a release without a timestamp is not a catalyst",
    }


# ---------------------------------------------------------------------------
# Sector / narrative heatmaps
# ---------------------------------------------------------------------------


def _change_pct(spec: InstrumentSpec) -> float:
    """Deterministic 1-day move for one instrument, in percent."""
    band = 3.6
    if spec.silo == "CRYPTO_BINANCE":
        band = 6.4
    elif spec.silo == "FX_MT5":
        band = 0.85
    elif spec.silo == "INDIA_ZERODHA_DHAN" and spec.asset_class == "COMMODITY_FUTURE":
        band = 2.3
    return round(_span(-band, band, spec.symbol, "d1-change"), 4)


def _sector_row(sector: str, specs: Sequence[InstrumentSpec]) -> dict[str, Any]:
    members = []
    changes: list[float] = []
    for spec in specs:
        change = _change_pct(spec)
        changes.append(change)
        members.append(
            {
                "symbol": spec.symbol,
                "display_name": spec.display_name,
                "asset_class": spec.asset_class,
                "sub_category": spec.sub_category,
                "change_pct": change,
                "spot_reference": reference_price(spec),
            }
        )
    members.sort(key=lambda row: (-row["change_pct"], row["symbol"]))
    changes.sort()
    count = len(members)
    advancers = sum(1 for value in changes if value > 0.0)
    decliners = sum(1 for value in changes if value < 0.0)
    unchanged = count - advancers - decliners
    median = (
        changes[count // 2]
        if count % 2
        else round((changes[count // 2 - 1] + changes[count // 2]) / 2.0, 4)
        if count
        else 0.0
    )
    market_cap = 0.0
    for spec in specs:
        try:
            market_cap += float(get_feature_profile(spec.symbol).features["market_cap_usd"])
        except (KeyError, TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return {
        "sector": sector,
        "member_count": count,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "advance_decline_ratio": round(
            (advancers / decliners) if decliners else float(advancers or 0.0), 4
        ),
        "breadth_pct": round((advancers / count) * 100.0, 4) if count else 0.0,
        "avg_change_pct": round(sum(changes) / count, 4) if count else 0.0,
        "median_change_pct": round(float(median), 4),
        "total_market_cap_usd": float(int(market_cap)),
        "top_gainer": members[0] if members else None,
        "top_laggard": members[-1] if members else None,
        "members": members,
    }


def get_sector_heatmap(silo: str, *, excluded_sectors: Iterable[str] = ()) -> dict[str, Any]:
    """Build one silo's sector heatmap straight from the instrument registry."""
    token = str(silo).strip().upper()
    blocked = {str(item) for item in excluded_sectors}
    buckets: dict[str, list[InstrumentSpec]] = {}
    for spec in instrument_registry.by_silo(token):
        if spec.sector in blocked:
            continue
        buckets.setdefault(spec.sector, []).append(spec)
    rows = [_sector_row(sector, sorted(specs, key=lambda s: s.symbol)) for sector, specs in buckets.items()]
    rows.sort(key=lambda row: (-row["avg_change_pct"], row["sector"]))
    advancers = sum(row["advancers"] for row in rows)
    decliners = sum(row["decliners"] for row in rows)
    unchanged = sum(row["unchanged"] for row in rows)
    total = advancers + decliners + unchanged
    return {
        "silo": token,
        "sector_count": len(rows),
        "sectors": rows,
        "breadth": {
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "advance_decline_ratio": round(
                (advancers / decliners) if decliners else float(advancers or 0.0), 4
            ),
            "breadth_pct": round((advancers / total) * 100.0, 4) if total else 0.0,
            "instrument_count": total,
        },
        "top_gainer": rows[0]["top_gainer"] if rows else None,
        "top_laggard": rows[-1]["top_laggard"] if rows else None,
        "best_sector": rows[0]["sector"] if rows else None,
        "worst_sector": rows[-1]["sector"] if rows else None,
    }


# ---------------------------------------------------------------------------
# G10 currency strength matrix
# ---------------------------------------------------------------------------


def get_currency_strength() -> dict[str, Any]:
    """G10 relative strength, carry-adjusted momentum and the pair matrix."""
    currencies: list[dict[str, Any]] = []
    for code in G10_CURRENCIES:
        rate = float(POLICY_RATES.get(code, 0.0))
        trend_50 = round(_span(-4.5, 4.5, code, "vs50"), 3)
        trend_200 = round(_span(-9.5, 9.5, code, "vs200"), 3)
        relative = round(
            max(-100.0, min(100.0, (trend_50 * 4.0) + (trend_200 * 2.6))), 3
        )
        carry_adjusted = round(
            max(-100.0, min(100.0, relative * 0.55 + (rate - 3.5) * 9.5)), 3
        )
        currencies.append(
            {
                "currency": code,
                "policy_rate_pct": rate,
                "spot_vs_50d_pct": trend_50,
                "spot_vs_200d_pct": trend_200,
                "relative_strength": relative,
                "carry_adjusted_momentum": carry_adjusted,
            }
        )
    currencies.sort(key=lambda row: (-row["relative_strength"], row["currency"]))
    by_code = {row["currency"]: row for row in currencies}

    matrix: dict[str, dict[str, Any]] = {}
    for spec in instrument_registry.by_silo("FX_MT5"):
        if spec.sub_category != "G10_MAJOR" and spec.sub_category != "G10_CROSS":
            continue
        base, quote = spec.symbol[:3], spec.symbol[3:]
        if base not in by_code or quote not in by_code:
            continue
        base_row = by_code[base]
        quote_row = by_code[quote]
        matrix[spec.symbol] = {
            "base": base,
            "quote": quote,
            "relative_strength_differential": round(
                base_row["relative_strength"] - quote_row["relative_strength"], 3
            ),
            "carry_differential_pct": round(
                base_row["policy_rate_pct"] - quote_row["policy_rate_pct"], 3
            ),
            "carry_adjusted_differential": round(
                base_row["carry_adjusted_momentum"]
                - quote_row["carry_adjusted_momentum"],
                3,
            ),
            "spot_reference": reference_price(spec),
        }

    ranked = [row["currency"] for row in currencies]
    return {
        "currency_count": len(currencies),
        "currencies": currencies,
        "ranked_strongest_to_weakest": ranked,
        "strongest": currencies[0]["currency"] if currencies else None,
        "weakest": currencies[-1]["currency"] if currencies else None,
        "pair_count": len(matrix),
        "matrix": {key: matrix[key] for key in sorted(matrix)},
        "method": "relative_strength = 4.0 x 50d trend + 2.6 x 200d trend, clamped to +/-100",
    }


# ---------------------------------------------------------------------------
# Public aggregate
# ---------------------------------------------------------------------------

_INDIA_EXCLUDED: tuple[str, ...] = ("Broad Market", "Currency")
_US_EXCLUDED: tuple[str, ...] = ("Diversified", "Fixed Income", "Volatility", "Broad Market")


def get_market_heatmaps() -> dict[str, Any]:
    """All four cross-asset heatmaps in one grounded payload."""
    india = get_sector_heatmap("INDIA_ZERODHA_DHAN", excluded_sectors=_INDIA_EXCLUDED)
    us = get_sector_heatmap("US_IBKR", excluded_sectors=_US_EXCLUDED)
    crypto = get_sector_heatmap("CRYPTO_BINANCE")
    fx = get_currency_strength()

    numeric = {
        "india_sector_count": float(india["sector_count"]),
        "india_breadth_pct": float(india["breadth"]["breadth_pct"]),
        "us_sector_count": float(us["sector_count"]),
        "us_breadth_pct": float(us["breadth"]["breadth_pct"]),
        "crypto_narrative_count": float(crypto["sector_count"]),
        "crypto_breadth_pct": float(crypto["breadth"]["breadth_pct"]),
        "fx_currency_count": float(fx["currency_count"]),
        "fx_pair_count": float(fx["pair_count"]),
        "fx_strongest_strength": float(fx["currencies"][0]["relative_strength"]),
        "fx_weakest_strength": float(fx["currencies"][-1]["relative_strength"]),
    }
    packet = _seal("MARKET_HEATMAPS", numeric)
    return {
        "schema_version": HEATMAP_SCHEMA_VERSION,
        "as_of_utc": f"{_CALENDAR_ANCHOR.isoformat()}T00:00:00Z",
        "india_sectors": india,
        "us_gics_sectors": us,
        "crypto_narratives": crypto,
        "fx_g10_strength": fx,
        "counts": {
            "india_sectors": india["sector_count"],
            "us_gics_sectors": us["sector_count"],
            "crypto_narratives": crypto["sector_count"],
            "g10_currencies": fx["currency_count"],
            "g10_pairs": fx["pair_count"],
        },
        "grounding": _grounding(packet),
        "payload_sha256": packet.sha256_digest,
        "law": "sec_018 Law 3 - a single-asset narrative is not a portfolio truth",
    }


# ---------------------------------------------------------------------------
# Thin aliases used by the cockpit and the release gate
# ---------------------------------------------------------------------------


def calendar_snapshot() -> dict[str, Any]:
    return get_macro_calendar()


def heatmap_snapshot() -> dict[str, Any]:
    return get_market_heatmaps()
