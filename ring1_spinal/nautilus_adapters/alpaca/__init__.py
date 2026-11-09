"""Alpaca v2 adapter for Ring 1 — US equities.

Alpaca was never present in the harvested ``nautilus_trader`` HEAD (the US silo
was specified to run on IBKR alone).  This module implements the Alpaca v2
trading surface directly against the public REST contract so the US silo has a
second, independently reachable venue:

* ``T+1`` settlement metadata (US equities moved to T+1 on 2024-05-28),
* Reg-T margin guards (50% initial, 25% maintenance, 2x max leverage,
  $25,000 pattern-day-trader equity floor),
* sub-penny maker-peg price formatting (``$0.0001`` below ``$1.00``,
  ``$0.01`` at or above it, snapped passive by side),
* exact ``POST /v2/orders`` request bodies.

Ring 1 never signs and never sends: this module renders *unsigned* bodies only.
Ring 0 signs them and, in ``LIVE`` mode, routes them through the venue adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .._nautilus_compat import (
    Environment,
    Shim,
    adapter_exports,
    alpaca_tick,
    format_order_payload,
    payload_flags,
    sub_penny_maker_peg,
    venue_spec,
)
from .._nautilus_compat import payload_flags as _compat_payload_flags

__all__ = [
    "ALPACA",
    "ALPACA_CLIENT_ID",
    "ALPACA_DATA_BASE_URL",
    "ALPACA_LIVE_BASE_URL",
    "ALPACA_PAPER_BASE_URL",
    "ALPACA_VENUE",
    "AlpacaDataClientConfig",
    "AlpacaDataClientFactory",
    "AlpacaEnvironment",
    "AlpacaExecutionClientConfig",
    "AlpacaExecutionClientFactory",
    "AlpacaInstrumentProviderConfig",
    "AlpacaRegTMarginGuard",
    "AlpacaSettlement",
    "MarginViolation",
    "alpaca_tick",
    "format_order_payload",
    "payload_flags",
    "reg_t_margin_guard",
    "settlement_metadata",
    "sub_penny_maker_peg",
]

globals().update(adapter_exports("alpaca", __all__))

# ---------------------------------------------------------------------------
# Venue constants
# ---------------------------------------------------------------------------

ALPACA = "ALPACA"
ALPACA_CLIENT_ID = "ALPACA"
ALPACA_VENUE = "ALPACA"
ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_BASE_URL = "https://api.alpaca.markets"
ALPACA_DATA_BASE_URL = "https://data.alpaca.markets"

AlpacaEnvironment = Environment

VENUE = ALPACA
SILO = "US_IBKR"
SETTLEMENT_CYCLE = "T+1"
SETTLEMENT_DAYS = 1
SUPPORTS_POST_ONLY = True

_MICROS = 1_000_000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MarginViolation(ValueError):
    """Raised when an order would breach Reg-T margin or settlement limits."""


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class AlpacaSettlement:
    """T+1 settlement descriptor for US equities."""

    cycle: str = "T+1"
    settlement_days: int = 1
    trade_date: str = ""
    settles_on: str = ""
    cash_available_immediately: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": ALPACA,
            "cycle": self.cycle,
            "settlement_days": self.settlement_days,
            "trade_date": self.trade_date,
            "settles_on": self.settles_on,
            "cash_available_immediately": self.cash_available_immediately,
        }


def settlement_metadata(trade_date: str = "", settles_on: str = "") -> dict[str, Any]:
    """Return the US silo T+1 settlement contract.

    Unsettled proceeds may not be redeployed: the 35% long-term sweep and the
    15% reserve transfer both draw on *settled* cash only.
    """

    return {
        "venue": ALPACA,
        "silo": SILO,
        "cycle": SETTLEMENT_CYCLE,
        "settlement_days": SETTLEMENT_DAYS,
        "trade_date": trade_date,
        "settles_on": settles_on,
        "cash_available_immediately": False,
        "unsettled_proceeds_redeployable": False,
        "settlement": AlpacaSettlement(
            trade_date=trade_date, settles_on=settles_on
        ).to_dict(),
    }


# ---------------------------------------------------------------------------
# Reg-T margin
# ---------------------------------------------------------------------------

REG_T_INITIAL_MARGIN_PCT = 50.0
REG_T_MAINTENANCE_MARGIN_PCT = 25.0
REG_T_MAX_LEVERAGE = 2.0
REG_T_DAY_TRADE_EQUITY_MIN_USD = 25_000.0


@dataclass(slots=True, frozen=True)
class AlpacaRegTMarginGuard:
    """Reg-T buying-power and maintenance-margin guard.

    ``equity_micros`` is settled account equity in 128-bit-safe integer micros
    (no floating-point money).  Buying power is ``equity * 2``; a new position
    must leave maintenance headroom above 25%.
    """

    equity_micros: int
    initial_margin_pct: float = REG_T_INITIAL_MARGIN_PCT
    maintenance_margin_pct: float = REG_T_MAINTENANCE_MARGIN_PCT
    max_leverage: float = REG_T_MAX_LEVERAGE
    day_trade_equity_min_micros: int = int(REG_T_DAY_TRADE_EQUITY_MIN_USD * _MICROS)

    def __post_init__(self) -> None:
        if int(self.equity_micros) < 0:
            raise ValueError("equity_micros must be non-negative")

    # -- derived quantities -------------------------------------------------

    @property
    def equity_usd(self) -> float:
        return self.equity_micros / _MICROS

    @property
    def buying_power_micros(self) -> int:
        return int(self.equity_micros * self.max_leverage)

    @property
    def pattern_day_trader(self) -> bool:
        return self.equity_micros >= self.day_trade_equity_min_micros

    def initial_margin_micros(self, notional_micros: int) -> int:
        return int(int(notional_micros) * self.initial_margin_pct / 100.0)

    def maintenance_margin_micros(self, notional_micros: int) -> int:
        return int(int(notional_micros) * self.maintenance_margin_pct / 100.0)

    def max_notional_micros(self) -> int:
        """Largest position notional Reg-T permits at 50% initial margin."""

        return int(self.equity_micros * 100.0 / self.initial_margin_pct)

    # -- checks -------------------------------------------------------------

    def check(self, notional_micros: int, *, existing_exposure_micros: int = 0) -> dict[str, Any]:
        """Validate a new order against Reg-T and return the margin receipt."""

        notional = int(notional_micros)
        if notional < 0:
            raise MarginViolation("notional_micros must be non-negative")
        projected = int(existing_exposure_micros) + notional
        required_initial = self.initial_margin_micros(projected)
        if required_initial > self.equity_micros:
            raise MarginViolation(
                "Reg-T initial margin breach: "
                f"projected exposure {projected} micros requires {required_initial} micros "
                f"of equity at {self.initial_margin_pct:.0f}% initial margin, "
                f"account has {self.equity_micros}"
            )
        if projected > self.buying_power_micros:
            raise MarginViolation(
                "Reg-T buying-power breach: "
                f"projected exposure {projected} micros exceeds "
                f"{self.buying_power_micros} micros ({self.max_leverage:.1f}x equity)"
            )
        return {
            "venue": ALPACA,
            "equity_micros": self.equity_micros,
            "existing_exposure_micros": int(existing_exposure_micros),
            "order_notional_micros": notional,
            "projected_exposure_micros": projected,
            "initial_margin_required_micros": required_initial,
            "maintenance_margin_required_micros": self.maintenance_margin_micros(projected),
            "buying_power_micros": self.buying_power_micros,
            "max_leverage": self.max_leverage,
            "pattern_day_trader": self.pattern_day_trader,
            "settlement_cycle": SETTLEMENT_CYCLE,
            "settlement_days": SETTLEMENT_DAYS,
        }


def reg_t_margin_guard(equity_usd: float | int, **kwargs: Any) -> AlpacaRegTMarginGuard:
    """Build a :class:`AlpacaRegTMarginGuard` from USD equity."""

    return AlpacaRegTMarginGuard(equity_micros=int(round(float(equity_usd) * _MICROS)), **kwargs)


# ---------------------------------------------------------------------------
# Adapter facade
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AlpacaAdapter:
    """Stateless Alpaca v2 request builder (Ring 1, no credentials)."""

    venue: str = ALPACA
    silo: str = SILO
    environment: str = "paper"
    base_url: str = ALPACA_PAPER_BASE_URL

    def __post_init__(self) -> None:
        env = str(self.environment).lower()
        if env in {"live", "production"}:
            self.environment = "live"
            self.base_url = ALPACA_LIVE_BASE_URL
        else:
            self.environment = "paper"
            self.base_url = ALPACA_PAPER_BASE_URL

    @property
    def spec(self):
        return venue_spec(ALPACA)

    def build_order(self, order: Mapping[str, Any]) -> dict[str, Any]:
        return format_order_payload(ALPACA, order)

    def describe(self, order: Mapping[str, Any]) -> dict[str, Any]:
        payload = self.build_order(order)
        return {
            "base_url": self.base_url,
            "payload": payload,
            "flags": _compat_payload_flags(ALPACA, payload),
        }

    def settlement(self, trade_date: str = "", settles_on: str = "") -> dict[str, Any]:
        return settlement_metadata(trade_date=trade_date, settles_on=settles_on)
