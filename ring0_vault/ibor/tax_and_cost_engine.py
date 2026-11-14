"""Granular statutory tax, brokerage and slippage breakdown engine (V1.6).

Every silo carries one statutory round-trip cost floor (``sec_014`` /
``sec_015``).  A single lump bps number is not auditable: a fill must be able to
say *how much of that drag was STT, how much was exchange turnover, how much was
GST, and how much was modeled slippage*.  This module produces that itemized
breakdown with **exact integer micro-units** and **exact bps**, and proves the
components sum back to the statutory floor to the last micro-unit.

Floors (bps, round trip)
------------------------
======================  ======  ==================================================
Silo                    Floor   Components
======================  ======  ==================================================
``INDIA_ZERODHA_DHAN``   8.50   STT, exchange txn, SEBI turnover, stamp duty,
                                 GST 18%, brokerage, modeled slippage
``CRYPTO_BINANCE``      16.00   maker/taker fee, funding 8h, liquidation insurance,
                                 modeled slippage
``US_IBKR``              2.80   SEC Section 31, FINRA TAF, OCC clearing, commission,
                                 modeled slippage
``FX_MT5``               1.60   raw spread, ECN commission, TOM/NEXT swap,
                                 modeled slippage
======================  ======  ==================================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "MICROS_PER_UNIT",
    "SILO_IDS",
    "COST_FLOOR_BPS",
    "COMPONENT_BPS",
    "CostComponent",
    "StatutoryCostBreakdown",
    "StatutoryCostEngine",
    "cost_engine",
    "statutory_floor_bps",
    "breakdown",
    "assert_floors_reconcile",
]

MICROS_PER_UNIT = 1_000_000
_BPS_SCALE = 1_000_000

SILO_IDS: tuple[str, ...] = (
    "INDIA_ZERODHA_DHAN",
    "CRYPTO_BINANCE",
    "US_IBKR",
    "FX_MT5",
)

#: Exact statutory round-trip floor per silo (bp).
COST_FLOOR_BPS: dict[str, float] = {
    "INDIA_ZERODHA_DHAN": 8.50,
    "CRYPTO_BINANCE": 16.00,
    "US_IBKR": 2.80,
    "FX_MT5": 1.60,
}

#: Itemized components.  The per-silo order is stable and deterministic.
COMPONENT_BPS: dict[str, tuple[tuple[str, float], ...]] = {
    "INDIA_ZERODHA_DHAN": (
        ("stt_bps", 3.00),
        ("exchange_txn_bps", 0.35),
        ("sebi_turnover_bps", 0.05),
        ("stamp_duty_bps", 0.30),
        ("gst_18pct_bps", 0.30),
        ("brokerage_bps", 0.50),
        ("modeled_slippage_bps", 4.00),
    ),
    "CRYPTO_BINANCE": (
        ("maker_taker_fee_bps", 7.50),
        ("funding_rate_8h_bps", 1.00),
        ("liquidation_insurance_bps", 0.50),
        ("modeled_slippage_bps", 7.00),
    ),
    "US_IBKR": (
        ("sec_section31_bps", 0.05),
        ("finra_taf_bps", 0.03),
        ("occ_clearing_bps", 0.02),
        ("commission_bps", 0.70),
        ("modeled_slippage_bps", 2.00),
    ),
    "FX_MT5": (
        ("raw_spread_bps", 0.80),
        ("ecn_commission_bps", 0.40),
        ("tom_next_swap_bps", 0.10),
        ("modeled_slippage_bps", 0.30),
    ),
}

#: Human labels used by the cockpit payloads.
COMPONENT_LABELS: dict[str, str] = {
    "stt_bps": "Securities Transaction Tax",
    "exchange_txn_bps": "Exchange transaction charges (NSE/BSE)",
    "sebi_turnover_bps": "SEBI turnover fee",
    "stamp_duty_bps": "Stamp duty",
    "gst_18pct_bps": "GST 18% on brokerage + exchange + SEBI",
    "brokerage_bps": "Brokerage",
    "modeled_slippage_bps": "Modeled slippage",
    "maker_taker_fee_bps": "Maker/taker fee",
    "funding_rate_8h_bps": "Funding rate (8h)",
    "liquidation_insurance_bps": "Liquidation insurance fund",
    "sec_section31_bps": "SEC Section 31",
    "finra_taf_bps": "FINRA Trading Activity Fee",
    "occ_clearing_bps": "OCC clearing",
    "commission_bps": "IBKR commission",
    "raw_spread_bps": "Raw spread",
    "ecn_commission_bps": "ECN commission",
    "tom_next_swap_bps": "TOM/NEXT swap",
}


def _apply_bps(amount_micros: int, bps: float) -> int:
    """Integer ``amount * bps / 10_000`` with micro-bp resolution (no float drift)."""
    scaled = int(round(float(bps) * _BPS_SCALE))
    return (int(amount_micros) * scaled) // (10_000 * _BPS_SCALE)


def _finite(name: str, value: float, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite (got {value!r})")
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {number!r})")
    return number


@dataclass(frozen=True, slots=True)
class CostComponent:
    """One statutory cost line: bps and the exact integer micros it produces."""

    key: str
    label: str
    bps: float
    micros: int
    share_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "bps": self.bps,
            "micros": self.micros,
            "share_pct": self.share_pct,
        }


@dataclass(frozen=True, slots=True)
class StatutoryCostBreakdown:
    """Complete itemized round-trip cost drag for one fill / trade / journal row."""

    silo: str
    side: str
    notional_micros: int
    floor_bps: float
    total_bps: float
    total_micros: int
    rounding_residual_micros: int
    components: tuple[CostComponent, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "side": self.side,
            "notional_micros": self.notional_micros,
            "floor_bps": self.floor_bps,
            "total_bps": self.total_bps,
            "total_micros": self.total_micros,
            "rounding_residual_micros": self.rounding_residual_micros,
            "component_bps": {c.key: c.bps for c in self.components},
            "component_micros": {c.key: c.micros for c in self.components},
            "components": [c.to_dict() for c in self.components],
            "reconciles": sum(c.micros for c in self.components) == self.total_micros,
        }

    @property
    def component_micros(self) -> dict[str, int]:
        return {c.key: c.micros for c in self.components}

    @property
    def component_bps(self) -> dict[str, float]:
        return {c.key: c.bps for c in self.components}


class StatutoryCostEngine:
    """Deterministic, allocation-light statutory cost breakdown engine."""

    def __init__(
        self,
        floors: Mapping[str, float] | None = None,
        components: Mapping[str, Iterable[tuple[str, float]]] | None = None,
    ) -> None:
        self._floors = dict(floors or COST_FLOOR_BPS)
        self._components = {
            silo: tuple((str(key), _finite(str(key), bps)) for key, bps in rows)
            for silo, rows in (components or COMPONENT_BPS).items()
        }
        for silo in self._floors:
            self._validate_silo(silo)

    # -- introspection -----------------------------------------------------
    def silos(self) -> tuple[str, ...]:
        return tuple(self._floors)

    def floor_bps(self, silo: str) -> float:
        token = str(silo).strip().upper()
        if token not in self._floors:
            raise KeyError(f"unknown silo {silo!r}; expected one of {tuple(self._floors)}")
        return float(self._floors[token])

    def component_bps(self, silo: str) -> dict[str, float]:
        token = str(silo).strip().upper()
        self.floor_bps(token)
        return {key: bps for key, bps in self._components[token]}

    def _validate_silo(self, silo: str) -> None:
        token = str(silo).strip().upper()
        rows = self._components.get(token)
        if not rows:
            raise ValueError(f"no component table for silo {silo!r}")
        total = sum(bps for _, bps in rows)
        floor = float(self._floors[token])
        if abs(total - floor) > 1e-9:
            raise ValueError(
                f"{token} components sum to {total:.4f} bps but the statutory "
                f"floor is {floor:.4f} bps"
            )

    def assert_reconciles(self) -> dict[str, dict[str, float]]:
        """Prove every silo's components add up to its floor (release gate)."""
        report: dict[str, dict[str, float]] = {}
        for silo in self._floors:
            self._validate_silo(silo)
            rows = self._components[silo]
            report[silo] = {
                "component_sum_bps": round(sum(bps for _, bps in rows), 6),
                "floor_bps": float(self._floors[silo]),
            }
        return report

    # -- breakdown ---------------------------------------------------------
    def breakdown(
        self,
        silo: str,
        notional_micros: int | float,
        *,
        side: str = "BUY",
    ) -> StatutoryCostBreakdown:
        """Itemize the statutory drag on ``notional_micros``.

        The notional is an integer micro-unit amount (``sec_009``: no float
        money).  Each component is quantised to an integer micro amount; the
        residual between the sum of those lines and the exact floor total is
        assigned to the largest component so the itemized lines always sum to
        the statutory total to the last micro-unit.
        """
        token = str(silo).strip().upper()
        floor = self.floor_bps(token)
        try:
            amount = int(round(float(notional_micros)))
        except (TypeError, ValueError) as exc:
            raise ValueError("notional_micros must be numeric") from exc
        if not math.isfinite(float(notional_micros)):
            raise ValueError("notional_micros must be finite")
        if amount < 0:
            raise ValueError("notional_micros must be >= 0")
        side_token = str(side).strip().upper() or "BUY"
        if side_token not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")

        total_micros = _apply_bps(amount, floor)
        raw = [
            (key, bps, _apply_bps(amount, bps)) for key, bps in self._components[token]
        ]
        component_sum = sum(micros for _, _, micros in raw)
        residual = total_micros - component_sum
        if residual and raw:
            # Largest component absorbs the rounding residual, deterministically.
            index = max(range(len(raw)), key=lambda i: (raw[i][2], -i))
            key, bps, micros = raw[index]
            raw[index] = (key, bps, micros + residual)

        components: list[CostComponent] = []
        for key, bps, micros in raw:
            share = (micros / total_micros * 100.0) if total_micros else 0.0
            components.append(
                CostComponent(
                    key=key,
                    label=COMPONENT_LABELS.get(key, key),
                    bps=bps,
                    micros=micros,
                    share_pct=round(share, 6),
                )
            )
        return StatutoryCostBreakdown(
            silo=token,
            side=side_token,
            notional_micros=amount,
            floor_bps=floor,
            total_bps=floor,
            total_micros=total_micros,
            rounding_residual_micros=residual,
            components=tuple(components),
        )

    def breakdown_units(
        self, silo: str, notional_units: float, *, side: str = "BUY"
    ) -> StatutoryCostBreakdown:
        """Convenience wrapper that accepts a float notional in currency units."""
        notional = _finite("notional_units", notional_units)
        return self.breakdown(
            silo, int(round(notional * MICROS_PER_UNIT)), side=side
        )


#: Module-level singleton -- the single source of statutory truth.
cost_engine: StatutoryCostEngine = StatutoryCostEngine()


def statutory_floor_bps(silo: str) -> float:
    return cost_engine.floor_bps(silo)


def breakdown(
    silo: str, notional_micros: int | float, *, side: str = "BUY"
) -> StatutoryCostBreakdown:
    return cost_engine.breakdown(silo, notional_micros, side=side)


def assert_floors_reconcile() -> dict[str, dict[str, float]]:
    return cost_engine.assert_reconciles()
