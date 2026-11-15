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

Segment layer (Finance Act 2026, w.e.f. 1 April 2026)
----------------------------------------------------
A single India floor is still not auditable: equity delivery pays STT on both
sides, intraday and F&O pay STT/CTT on the sell leg only, and stamp duty is a
buy-side charge.  ``segment_breakdown`` therefore computes the statutory charge
for **one side of one segment** from ``SEGMENT_STATUTORY_BPS`` and reports it in
``SegmentCostBreakdown``.  Rates are bps of notional and money is integer
micro-units; every line is quantised with :func:`_apply_bps` and the residual is
absorbed by the largest line so the itemised lines sum to the charge exactly.

=========================  ============  ===========  =======  ========  =====  =========
Segment                    STT buy       STT sell     Stamp    Exchange  SEBI   Brokerage
=========================  ============  ===========  =======  ========  =====  =========
``EQUITY_DELIVERY``        10.00         10.00        1.50     0.325     0.01   0.35
``EQUITY_INTRADAY``        0.00          2.50         0.30     0.325     0.01   0.35
``EQUITY_FUTURES``         0.00          5.00         0.20     0.325     0.01   0.20
``EQUITY_OPTIONS``         0.00          15.00        0.30     0.325     0.01   0.20
``COMMODITY_MCX``          0.00          0.00 (CTT)   0.20     0.325     0.01   0.20
``CURRENCY_NSE``           0.00          0.00         0.01     0.325     0.01   0.20
=========================  ============  ===========  =======  ========  =====  =========

Stamp duty is charged on the **buy** leg only; ``COMMODITY_MCX`` charges CTT
(not STT) on the sell leg; ``CURRENCY_NSE`` is exempt from both.  GST is 18% of
``(brokerage + exchange + SEBI)`` -- the brokerage bps above are **deterministic
modeled brokerage used only to build that GST base**, and ``brokerage_micros``
is exposed on the breakdown so the GST line stays auditable.  ``EQUITY_INTRADAY``
is floored to the silo round-trip floor (``COST_FLOOR_BPS["INDIA_ZERODHA_DHAN"]``,
8.50 bps) with an explicit ``modeled_slippage_bps`` line; the other five segments
report their raw statutory charge.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
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
    "INDIA_SEGMENTS",
    "SEGMENT_STATUTORY_BPS",
    "SegmentCostBreakdown",
    "resolve_segment",
    "segment_breakdown",
    "india_segment_bps",
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
    "ctt_bps": "Commodities Transaction Tax",
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


#: Above this magnitude a binary float can no longer represent every integer,
#: so an amount that arrives as a float is refused instead of being silently
#: rounded to a neighbouring micro-unit (``sec_009``: money is a 128-bit int).
EXACT_FLOAT_INT_LIMIT: int = 1 << 53


def _exact_micros(value: Any, *, name: str) -> int:
    """Coerce a micro-unit amount to ``int`` without routing an int through float.

    An ``int`` is taken as-is, so a 128-bit notional keeps every micro-unit.
    A float is accepted only while it stays inside
    :data:`EXACT_FLOAT_INT_LIMIT`; beyond that ``int(round(...))`` would corrupt
    the value by whole micro-units, which is worse than refusing the call.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer micro-unit amount")
    if isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if abs(number) > EXACT_FLOAT_INT_LIMIT:
        raise ValueError(
            f"{name} exceeds the exact float range; pass an int micro-unit amount"
        )
    return int(round(number))


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
        amount = _exact_micros(notional_micros, name="notional_micros")
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
        self, silo: str, notional_units: float | int | Decimal, *, side: str = "BUY"
    ) -> StatutoryCostBreakdown:
        """Convenience wrapper that accepts a notional in whole currency units.

        The unit amount is scaled to micro-units in :class:`~decimal.Decimal`, not
        in binary float: ``notional * 1_000_000`` overflows float's exact-integer
        range at roughly 9.01e9 units, so a large institutional notional would
        otherwise lose micro-units before the integer engine ever sees it.
        """
        if isinstance(notional_units, int) and not isinstance(notional_units, bool):
            micros = notional_units * MICROS_PER_UNIT
        else:
            try:
                exact = Decimal(repr(notional_units)) if isinstance(notional_units, float) else Decimal(notional_units)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("notional_units must be numeric") from exc
            if not exact.is_finite():
                raise ValueError("notional_units must be finite")
            if exact < 0:
                raise ValueError("notional_units must be >= 0")
            micros = int(
                (exact * MICROS_PER_UNIT).to_integral_value(rounding=ROUND_HALF_EVEN)
            )
        return self.breakdown(silo, micros, side=side)


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


# ---------------------------------------------------------------------------
# Finance Act 2026 -- Indian segment- and side-aware statutory layer
# ---------------------------------------------------------------------------
#: The silo the segment table applies to.  Every other silo falls back to the
#: legacy lump-floor breakdown so callers always receive the same object.
_INDIA_SILO = "INDIA_ZERODHA_DHAN"

#: GST is 18% of (brokerage + exchange + SEBI), i.e. 1800 bps of that base.
_GST_BASE_BPS = 1_800.0

#: The six Indian segments the engine prices.  Order is stable and deterministic.
INDIA_SEGMENTS: tuple[str, ...] = (
    "EQUITY_DELIVERY",
    "EQUITY_INTRADAY",
    "EQUITY_FUTURES",
    "EQUITY_OPTIONS",
    "COMMODITY_MCX",
    "CURRENCY_NSE",
)

#: Statutory rates in bps of notional, Finance Act 2026, w.e.f. 1 April 2026.
#:
#: ``stt_buy_bps`` / ``stt_sell_bps`` are Securities Transaction Tax per leg.
#: ``ctt_sell_bps`` (present where the segment is a commodities leg) is
#: Commodities Transaction Tax and replaces STT -- a CTT segment always carries
#: ``stt_*_bps == 0``.  ``stamp_duty_bps`` is a **buy-side only** charge.
#: ``exchange_bps`` / ``sebi_bps`` are charged on both legs.
#: ``brokerage_bps`` is deterministic modeled brokerage that exists **only to
#: build the GST base** (18% of brokerage + exchange + SEBI); it is exposed on
#: the breakdown as ``brokerage_micros`` so the GST line is auditable, and it is
#: also carried as an explicit component line.
#:
#: Treat the table as read-only statutory truth.  ``india_segment_bps`` returns a
#: defensive copy for callers that need to persist or mutate a row.
SEGMENT_STATUTORY_BPS: Mapping[str, Mapping[str, float]] = {
    "EQUITY_DELIVERY": {
        "stt_buy_bps": 10.0,
        "stt_sell_bps": 10.0,
        "stamp_duty_bps": 1.5,
        "exchange_bps": 0.325,
        "sebi_bps": 0.01,
        "brokerage_bps": 0.35,
    },
    "EQUITY_INTRADAY": {
        "stt_buy_bps": 0.0,
        "stt_sell_bps": 2.5,
        "stamp_duty_bps": 0.30,
        "exchange_bps": 0.325,
        "sebi_bps": 0.01,
        "brokerage_bps": 0.35,
    },
    "EQUITY_FUTURES": {
        "stt_buy_bps": 0.0,
        "stt_sell_bps": 5.0,
        "stamp_duty_bps": 0.20,
        "exchange_bps": 0.325,
        "sebi_bps": 0.01,
        "brokerage_bps": 0.20,
    },
    "EQUITY_OPTIONS": {
        "stt_buy_bps": 0.0,
        "stt_sell_bps": 15.0,
        "stamp_duty_bps": 0.30,
        "exchange_bps": 0.325,
        "sebi_bps": 0.01,
        "brokerage_bps": 0.20,
    },
    "COMMODITY_MCX": {
        "stt_buy_bps": 0.0,
        "stt_sell_bps": 0.0,
        "ctt_sell_bps": 1.0,
        "stamp_duty_bps": 0.20,
        "exchange_bps": 0.325,
        "sebi_bps": 0.01,
        "brokerage_bps": 0.20,
    },
    "CURRENCY_NSE": {
        "stt_buy_bps": 0.0,
        "stt_sell_bps": 0.0,
        "ctt_sell_bps": 0.0,
        "stamp_duty_bps": 0.01,
        "exchange_bps": 0.325,
        "sebi_bps": 0.01,
        "brokerage_bps": 0.20,
    },
}

#: Declared round-trip floors per segment, read straight from the silo floor.
#: ``EQUITY_INTRADAY`` is the only segment whose raw statutory charge falls below
#: the silo floor, so it is the only one that can be floored.
_ROUND_TRIP_FLOOR_BPS: Mapping[str, float] = {
    "EQUITY_INTRADAY": float(COST_FLOOR_BPS[_INDIA_SILO]),
}

#: ``sub_category`` / ``asset_class`` spellings accepted by :func:`resolve_segment`.
#: Normalisation is case-insensitive and collapses ``-``, ``/``, ``_`` and spaces.
#: Commodity and currency **options** are deliberately absent: their statutory
#: treatment differs from the future leg and no segment key is authorised for
#: them, so they raise rather than get silently priced as futures.
_SEGMENT_ALIASES: Mapping[str, str] = {
    # EQUITY_DELIVERY
    "EQUITY_DELIVERY": "EQUITY_DELIVERY",
    "DELIVERY": "EQUITY_DELIVERY",
    "CASH": "EQUITY_DELIVERY",
    "EQ": "EQUITY_DELIVERY",
    "EQUITY": "EQUITY_DELIVERY",
    "EQUITY_CASH": "EQUITY_DELIVERY",
    "CASH_DELIVERY": "EQUITY_DELIVERY",
    "EQ_DELIVERY": "EQUITY_DELIVERY",
    "CNC": "EQUITY_DELIVERY",
    "DELIVERY_CNC": "EQUITY_DELIVERY",
    # EQUITY_INTRADAY
    "EQUITY_INTRADAY": "EQUITY_INTRADAY",
    "INTRADAY": "EQUITY_INTRADAY",
    "MIS": "EQUITY_INTRADAY",
    "EQUITY_MIS": "EQUITY_INTRADAY",
    "EQ_INTRADAY": "EQUITY_INTRADAY",
    "INTRADAY_MIS": "EQUITY_INTRADAY",
    # EQUITY_FUTURES
    "EQUITY_FUTURES": "EQUITY_FUTURES",
    "EQUITY_FUTURE": "EQUITY_FUTURES",
    "FUT": "EQUITY_FUTURES",
    "FUTURE": "EQUITY_FUTURES",
    "FUTURES": "EQUITY_FUTURES",
    "EQ_FUTURES": "EQUITY_FUTURES",
    "INDEX_FUTURE": "EQUITY_FUTURES",
    "INDEX_FUTURES": "EQUITY_FUTURES",
    "STOCK_FUTURE": "EQUITY_FUTURES",
    "STOCK_FUTURES": "EQUITY_FUTURES",
    "FNO_FUTURES": "EQUITY_FUTURES",
    # EQUITY_OPTIONS
    "EQUITY_OPTIONS": "EQUITY_OPTIONS",
    "EQUITY_OPTION": "EQUITY_OPTIONS",
    "OPT": "EQUITY_OPTIONS",
    "OPTION": "EQUITY_OPTIONS",
    "OPTIONS": "EQUITY_OPTIONS",
    "CE": "EQUITY_OPTIONS",
    "PE": "EQUITY_OPTIONS",
    "CALL": "EQUITY_OPTIONS",
    "PUT": "EQUITY_OPTIONS",
    "EQ_OPTIONS": "EQUITY_OPTIONS",
    "INDEX_OPTION": "EQUITY_OPTIONS",
    "INDEX_OPTIONS": "EQUITY_OPTIONS",
    "STOCK_OPTION": "EQUITY_OPTIONS",
    "STOCK_OPTIONS": "EQUITY_OPTIONS",
    "FNO_OPTIONS": "EQUITY_OPTIONS",
    # COMMODITY_MCX
    "COMMODITY_MCX": "COMMODITY_MCX",
    "MCX": "COMMODITY_MCX",
    "COMMODITY": "COMMODITY_MCX",
    "COMMODITY_FUTURE": "COMMODITY_MCX",
    "COMMODITY_FUTURES": "COMMODITY_MCX",
    # CURRENCY_NSE
    "CURRENCY_NSE": "CURRENCY_NSE",
    "CURRENCY": "CURRENCY_NSE",
    "FX": "CURRENCY_NSE",
    "CURRENCY_FUTURE": "CURRENCY_NSE",
    "CURRENCY_FUTURES": "CURRENCY_NSE",
}

#: Legacy component key -> the statutory field it maps onto on the India silo.
_LEGACY_INDIA_FIELDS: Mapping[str, str] = {
    "stt_bps": "stt_micros",
    "stamp_duty_bps": "stamp_duty_micros",
    "exchange_txn_bps": "exchange_micros",
    "sebi_turnover_bps": "sebi_micros",
    "brokerage_bps": "brokerage_micros",
    "gst_18pct_bps": "gst_micros",
}


def resolve_segment(value: str) -> str:
    """Normalise an Indian segment spelling to one of :data:`INDIA_SEGMENTS`.

    Case-insensitive; ``-``, ``/``, ``_`` and whitespace all collapse, so
    ``"equity-delivery"``, ``"EQUITY_DELIVERY"`` and ``" Equity Delivery "`` are
    the same segment.  Unknown values raise ``ValueError("UNKNOWN_SEGMENT:<value>")``
    rather than defaulting to a segment, because a wrong segment silently
    misprices STT.
    """
    if value is None:
        raise ValueError("UNKNOWN_SEGMENT:None")
    raw = str(value).strip()
    token = re.sub(r"[^A-Z0-9]+", "_", raw.upper()).strip("_")
    segment = _SEGMENT_ALIASES.get(token)
    if segment is None:
        raise ValueError(f"UNKNOWN_SEGMENT:{raw}")
    return segment


def india_segment_bps(segment: str) -> Mapping[str, float]:
    """Return a copy of the resolved statutory rate row for ``segment``."""
    resolved = resolve_segment(segment)
    return dict(SEGMENT_STATUTORY_BPS[resolved])


def _coerce_notional_micros(notional_micros: int) -> int:
    """Integer micro-unit notional; must be strictly positive."""
    amount = _exact_micros(notional_micros, name="notional")
    if amount <= 0:
        raise ValueError("INVALID_NOTIONAL")
    return amount


def _normalise_side(side: str) -> str:
    token = str(side).strip().upper()
    if token not in ("BUY", "SELL"):
        raise ValueError(f"INVALID_SIDE:{side}")
    return token


def _bps_of(micros: int, notional_micros: int) -> float:
    return round(micros * 10_000 / notional_micros, 6)


@dataclass(frozen=True, slots=True)
class SegmentCostBreakdown:
    """Statutory charge for **one side of one segment**, exact to the micro-unit.

    ``stt_micros`` / ``ctt_micros`` / ``stamp_duty_micros`` are the statutory
    lines for this leg (``stt`` and ``ctt`` are mutually exclusive, and
    ``stamp_duty`` is buy-side only).  ``brokerage_micros`` is modeled brokerage:
    it is a real line in the total and the base the 18% GST is levied on.
    ``gst_micros`` is 18% of ``brokerage + exchange + SEBI``.

    ``components`` always sums to ``total_charge_micros``; the small residual
    left by quantising each line independently is absorbed by the largest line
    and reported as ``rounding_residual_micros``.  ``reconciles`` is the proof.

    ``floor_applied`` is only ever true for ``EQUITY_INTRADAY``, when the raw
    statutory charge fell below the silo round-trip floor and a
    ``modeled_slippage_bps`` line was added to lift it to that floor.
    ``statutory`` is the rate row used (``None`` for the ``LEGACY_FLOOR``
    container, which carries the legacy lump-floor numbers unchanged).
    """

    silo: str
    segment: str
    side: str
    notional_micros: int
    stt_micros: int
    ctt_micros: int
    stamp_duty_micros: int
    exchange_micros: int
    sebi_micros: int
    brokerage_micros: int
    gst_micros: int
    total_charge_micros: int
    total_bps: float
    floor_bps: float
    floor_applied: bool
    rounding_residual_micros: int
    components: tuple[CostComponent, ...]
    reconciles: bool
    statutory: Mapping[str, float] | None = None

    @property
    def component_micros(self) -> dict[str, int]:
        return {c.key: c.micros for c in self.components}

    @property
    def round_trip_floor_bps(self) -> float | None:
        """Declared round-trip floor for this segment (``None`` when none applies)."""
        return _ROUND_TRIP_FLOOR_BPS.get(self.segment)

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "segment": self.segment,
            "side": self.side,
            "notional_micros": self.notional_micros,
            "stt_micros": self.stt_micros,
            "ctt_micros": self.ctt_micros,
            "stamp_duty_micros": self.stamp_duty_micros,
            "exchange_micros": self.exchange_micros,
            "sebi_micros": self.sebi_micros,
            "brokerage_micros": self.brokerage_micros,
            "gst_micros": self.gst_micros,
            "total_charge_micros": self.total_charge_micros,
            "total_bps": self.total_bps,
            "floor_bps": self.floor_bps,
            "floor_applied": self.floor_applied,
            "round_trip_floor_bps": self.round_trip_floor_bps,
            "rounding_residual_micros": self.rounding_residual_micros,
            "component_micros": self.component_micros,
            "components": [c.to_dict() for c in self.components],
            "reconciles": self.reconciles,
            "statutory": dict(self.statutory) if self.statutory is not None else None,
        }


def _legacy_segment_container(
    silo: str, notional_micros: int, side: str
) -> SegmentCostBreakdown:
    """Wrap the preserved lump-floor breakdown without changing its numbers."""
    legacy = cost_engine.breakdown(silo, notional_micros, side=side)
    mapped = legacy.component_micros
    fields = {name: 0 for name in _LEGACY_INDIA_FIELDS.values()}
    for key, field in _LEGACY_INDIA_FIELDS.items():
        if key in mapped:
            fields[field] = mapped[key]
    return SegmentCostBreakdown(
        silo=legacy.silo,
        segment="LEGACY_FLOOR",
        side=legacy.side,
        notional_micros=legacy.notional_micros,
        stt_micros=fields["stt_micros"],
        ctt_micros=0,
        stamp_duty_micros=fields["stamp_duty_micros"],
        exchange_micros=fields["exchange_micros"],
        sebi_micros=fields["sebi_micros"],
        brokerage_micros=fields["brokerage_micros"],
        gst_micros=fields["gst_micros"],
        total_charge_micros=legacy.total_micros,
        total_bps=legacy.total_bps,
        floor_bps=legacy.floor_bps,
        floor_applied=False,
        rounding_residual_micros=legacy.rounding_residual_micros,
        components=legacy.components,
        reconciles=(
            sum(c.micros for c in legacy.components) == legacy.total_micros
        ),
        statutory=None,
    )


def _india_segment_container(
    silo: str, notional_micros: int, side: str, segment: str
) -> SegmentCostBreakdown:
    row = SEGMENT_STATUTORY_BPS[segment]
    is_sell = side == "SELL"

    # -- statutory rates for this leg (bps of notional) ---------------------
    stt_bps = float(row["stt_sell_bps"] if is_sell else row["stt_buy_bps"])
    ctt_bps = float(row.get("ctt_sell_bps", 0.0)) if is_sell else 0.0
    stamp_bps = 0.0 if is_sell else float(row["stamp_duty_bps"])  # buy-side only
    exchange_bps = float(row["exchange_bps"])
    sebi_bps = float(row["sebi_bps"])
    brokerage_bps = float(row["brokerage_bps"])

    # -- quantise each line, then derive GST from the quantised base --------
    stt_micros = _apply_bps(notional_micros, stt_bps)
    ctt_micros = _apply_bps(notional_micros, ctt_bps)
    stamp_micros = _apply_bps(notional_micros, stamp_bps)
    exchange_micros = _apply_bps(notional_micros, exchange_bps)
    sebi_micros = _apply_bps(notional_micros, sebi_bps)
    brokerage_micros = _apply_bps(notional_micros, brokerage_bps)
    gst_base_micros = brokerage_micros + exchange_micros + sebi_micros
    gst_micros = _apply_bps(gst_base_micros, _GST_BASE_BPS)
    gst_bps = round(gst_micros * 10_000 / notional_micros, 10)

    lines: list[tuple[str, float, int]] = [("stt_bps", stt_bps, stt_micros)]
    if "ctt_sell_bps" in row:
        lines.append(("ctt_bps", ctt_bps, ctt_micros))
    lines.extend(
        (
            ("stamp_duty_bps", stamp_bps, stamp_micros),
            ("exchange_txn_bps", exchange_bps, exchange_micros),
            ("sebi_turnover_bps", sebi_bps, sebi_micros),
            ("brokerage_bps", brokerage_bps, brokerage_micros),
            ("gst_18pct_bps", gst_bps, gst_micros),
        )
    )

    # -- exact total from the aggregate rate, residual to the largest line --
    aggregate_bps = round(
        stt_bps
        + ctt_bps
        + stamp_bps
        + exchange_bps
        + sebi_bps
        + brokerage_bps
        + gst_bps,
        10,
    )
    total_micros = _apply_bps(notional_micros, aggregate_bps)
    residual = total_micros - sum(m for _, _, m in lines)
    if residual:
        # Largest line absorbs the quantisation residual, deterministically.
        index = max(range(len(lines)), key=lambda i: (lines[i][2], -i))
        key, bps, micros = lines[index]
        lines[index] = (key, bps, max(0, micros + residual))
        total_micros = sum(m for _, _, m in lines)

    # -- intraday floor: lift the raw statutory charge to the silo floor ----
    total_bps = _bps_of(total_micros, notional_micros)
    floor_bps = float(COST_FLOOR_BPS[_INDIA_SILO])
    round_trip_floor = _ROUND_TRIP_FLOOR_BPS.get(segment)
    floor_applied = False
    if round_trip_floor is not None and total_bps < round_trip_floor:
        target_micros = _apply_bps(notional_micros, round_trip_floor)
        shortfall = target_micros - total_micros
        if shortfall > 0:
            lines.append(
                (
                    "modeled_slippage_bps",
                    round(shortfall * 10_000 / notional_micros, 6),
                    shortfall,
                )
            )
            total_micros = sum(m for _, _, m in lines)
            total_bps = _bps_of(total_micros, notional_micros)
            floor_applied = True

    components = tuple(
        CostComponent(
            key=key,
            label=COMPONENT_LABELS.get(key, key),
            bps=bps,
            micros=micros,
            share_pct=round(micros / total_micros * 100.0, 6) if total_micros else 0.0,
        )
        for key, bps, micros in lines
    )
    return SegmentCostBreakdown(
        silo=_INDIA_SILO,
        segment=segment,
        side=side,
        notional_micros=notional_micros,
        stt_micros=stt_micros,
        ctt_micros=ctt_micros,
        stamp_duty_micros=stamp_micros,
        exchange_micros=exchange_micros,
        sebi_micros=sebi_micros,
        brokerage_micros=brokerage_micros,
        gst_micros=gst_micros,
        total_charge_micros=total_micros,
        total_bps=total_bps,
        floor_bps=floor_bps,
        floor_applied=floor_applied,
        rounding_residual_micros=residual,
        components=components,
        reconciles=sum(c.micros for c in components) == total_micros,
        statutory=SEGMENT_STATUTORY_BPS[segment],
    )


def segment_breakdown(
    silo: str,
    notional_micros: int,
    *,
    side: str = "BUY",
    sub_category: str | None = None,
    asset_class: str | None = None,
) -> SegmentCostBreakdown:
    """Itemize one leg of one Indian segment, or the legacy silo floor.

    ``sub_category`` (preferred) or ``asset_class`` selects the segment; both are
    resolved through :func:`resolve_segment`, so any accepted spelling works.
    When neither is supplied -- or the silo is not ``INDIA_ZERODHA_DHAN`` -- the
    legacy :meth:`StatutoryCostEngine.breakdown` numbers are returned unchanged
    inside the same container with ``segment="LEGACY_FLOOR"`` and
    ``statutory=None``, so every caller sees one uniform object.

    ``side`` is ``BUY`` or ``SELL`` (case-insensitive) and selects the STT/CTT
    leg, the buy-side stamp duty, and nothing else.

    Raises ``ValueError("INVALID_NOTIONAL")`` for a non-positive notional,
    ``ValueError("INVALID_SIDE:<value>")`` for an unknown side and
    ``ValueError("UNKNOWN_SEGMENT:<value>")`` for an unresolvable segment.
    """
    amount = _coerce_notional_micros(notional_micros)
    side_token = _normalise_side(side)
    silo_token = str(silo).strip().upper()
    raw_segment = sub_category if sub_category is not None else asset_class
    if raw_segment is None or silo_token != _INDIA_SILO:
        return _legacy_segment_container(silo_token, amount, side_token)
    segment = resolve_segment(raw_segment)
    return _india_segment_container(silo_token, amount, side_token, segment)
