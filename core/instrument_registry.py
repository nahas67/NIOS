"""NIOS master instrument & contract-specification registry (V1.6).

One deterministic, queryable table of every canonical instrument traded by the
four sovereign silos.  Nothing here talks to a venue: the registry is a
*contract specification* (``sec_014`` / ``sec_015``) that Ring 0 and Ring 1
agree on before an order is ever reserved.

Guarantees
----------
* Every spec is complete: lot size, tick size, freeze quantity, circuit band,
  initial/maintenance margin, settlement cycle, UTC session and option strike
  step are all present and finite.
* Every numeric field is validated at construction; a negative or non-finite
  value can never enter the registry.
* ``validate_order`` is integer-only: quantities are compared in micro-units
  and prices rounded to the exact tick grid, so a float can never smuggle a
  half-tick price past the outbox.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable, Mapping

__all__ = [
    "MICROS",
    "SILO_IDS",
    "SettlementCycle",
    "InstrumentSpec",
    "InstrumentCheck",
    "InstrumentRegistry",
    "registry",
    "instrument_registry",
    "canonical_instruments",
    "validate_order",
]

MICROS = 1_000_000

SILO_IDS: tuple[str, ...] = (
    "INDIA_ZERODHA_DHAN",
    "CRYPTO_BINANCE",
    "US_IBKR",
    "FX_MT5",
)

# ``T+0`` covers intraday derivatives (the position closes the same UTC day),
# ``T+1`` the cash-equity settlement cycles and ``PERPETUAL`` the 24/7 crypto
# perpetuals that never settle to a delivery date.
_SETTLEMENT = ("T+0", "T+1", "T+2", "PERPETUAL")


def _finite(name: str, value: float, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - construction bug
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite (got {value!r})")
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {number!r})")
    return number


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Complete contract specification for one tradable instrument."""

    symbol: str
    silo: str
    venue: str
    asset_class: str
    currency: str
    lot_size: float
    tick_size_micros: int
    freeze_quantity: float
    circuit_limit_pct: float
    margin_initial_pct: float
    margin_maintenance_pct: float
    settlement_cycle: str
    trading_hours_utc: str
    option_strike_step: float

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip().upper()
        if not symbol:
            raise ValueError("symbol must not be empty")
        object.__setattr__(self, "symbol", symbol)
        if self.silo not in SILO_IDS:
            raise ValueError(f"unknown silo {self.silo!r}")
        if self.settlement_cycle not in _SETTLEMENT:
            raise ValueError(
                f"settlement_cycle must be one of {_SETTLEMENT} "
                f"(got {self.settlement_cycle!r})"
            )
        lot = _finite("lot_size", self.lot_size, minimum=1e-9)
        freeze = _finite("freeze_quantity", self.freeze_quantity, minimum=1e-9)
        if freeze < lot:
            raise ValueError("freeze_quantity must be >= lot_size")
        for name in (
            "circuit_limit_pct",
            "margin_initial_pct",
            "margin_maintenance_pct",
            "option_strike_step",
        ):
            _finite(name, getattr(self, name))
        if self.margin_maintenance_pct > self.margin_initial_pct:
            raise ValueError("margin_maintenance_pct must be <= margin_initial_pct")
        for name in ("tick_size_micros",):
            value = int(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            object.__setattr__(self, name, value)
        if not str(self.trading_hours_utc).strip():
            raise ValueError("trading_hours_utc must not be empty")

    # -- integer helpers ---------------------------------------------------
    @property
    def lot_size_micros(self) -> int:
        """Lot size expressed in millionths of a unit (exact integer)."""
        return int(round(self.lot_size * MICROS))

    @property
    def freeze_quantity_micros(self) -> int:
        return int(round(self.freeze_quantity * MICROS))

    def lowers_to(self) -> dict[str, float]:
        """Static lower circuit band (numeric, for the risk engine)."""
        return {
            "upper": self.circuit_limit_pct,
            "lower": -self.circuit_limit_pct,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "silo": self.silo,
            "venue": self.venue,
            "asset_class": self.asset_class,
            "currency": self.currency,
            "lot_size": self.lot_size,
            "lot_size_micros": self.lot_size_micros,
            "tick_size_micros": self.tick_size_micros,
            "tick_size": self.tick_size_micros / MICROS,
            "freeze_quantity": self.freeze_quantity,
            "freeze_quantity_micros": self.freeze_quantity_micros,
            "circuit_limit_pct": self.circuit_limit_pct,
            "circuit_band_pct": self.lowers_to(),
            "margin_initial_pct": self.margin_initial_pct,
            "margin_maintenance_pct": self.margin_maintenance_pct,
            "settlement_cycle": self.settlement_cycle,
            "trading_hours_utc": self.trading_hours_utc,
            "option_strike_step": self.option_strike_step,
        }


@dataclass(frozen=True, slots=True)
class InstrumentCheck:
    """Result of one deterministic lot / tick / freeze validation."""

    ok: bool
    reason: str
    symbol: str
    silo: str
    quantity: float
    price: float
    adjusted_price: float
    price_changed: bool
    lot_multiple: float
    ticks: int
    notional_micros: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "symbol": self.symbol,
            "silo": self.silo,
            "quantity": self.quantity,
            "price": self.price,
            "adjusted_price": self.adjusted_price,
            "price_changed": self.price_changed,
            "lot_multiple": self.lot_multiple,
            "ticks": self.ticks,
            "notional_micros": self.notional_micros,
        }


class InstrumentRegistry:
    """Read-only, deterministic registry of every canonical instrument."""

    def __init__(self, specs: Iterable[InstrumentSpec] = ()) -> None:
        self._by_key: dict[tuple[str, str], InstrumentSpec] = {}
        self._by_symbol: dict[str, list[InstrumentSpec]] = {}
        for spec in specs:
            self.add(spec)

    # -- construction ------------------------------------------------------
    def add(self, spec: InstrumentSpec) -> InstrumentSpec:
        key = (spec.silo, spec.symbol)
        if key in self._by_key:
            raise ValueError(f"duplicate instrument {spec.silo}/{spec.symbol}")
        self._by_key[key] = spec
        self._by_symbol.setdefault(spec.symbol, []).append(spec)
        return spec

    # -- queries -----------------------------------------------------------
    def get(self, silo: str, symbol: str) -> InstrumentSpec:
        raw = str(symbol).strip().upper()
        silo_token = str(silo).strip().upper()
        candidates = [raw]
        compact = "".join(ch for ch in raw if ch.isalnum())
        candidates.append(compact)
        if compact.endswith("USD"):
            candidates.append(compact + "T")
        for token in candidates:
            spec = self._by_key.get((silo_token, token))
            if spec is not None:
                return spec
        # A unique cross-silo symbol still resolves when the caller names a silo
        # whose own catalogue does not list it (for example BTC-USD -> BTCUSDT).
        for token in candidates:
            matches = self._by_symbol.get(token) or []
            if len(matches) == 1:
                return matches[0]
        raise KeyError(
            f"unknown instrument {silo_token}/{raw}; "
            f"registry holds {len(self._by_key)} specs"
        )

    def find(self, symbol: str) -> InstrumentSpec:
        """Resolve a symbol that is unique across all silos."""
        token = str(symbol).strip().upper()
        matches = self._by_symbol.get(token) or []
        if not matches:
            raise KeyError(f"unknown symbol {token!r}")
        if len(matches) > 1:
            raise KeyError(
                f"symbol {token!r} is ambiguous across "
                f"{sorted({spec.silo for spec in matches})}; pass a silo"
            )
        return matches[0]

    def has(self, silo: str, symbol: str) -> bool:
        try:
            self.get(silo, symbol)
        except KeyError:
            return False
        return True

    def by_silo(self, silo: str) -> tuple[InstrumentSpec, ...]:
        token = str(silo).strip().upper()
        return tuple(
            spec for (owner, _), spec in self._by_key.items() if owner == token
        )

    def all(self) -> tuple[InstrumentSpec, ...]:
        return tuple(self._by_key.values())

    def symbols(self, silo: str | None = None) -> tuple[str, ...]:
        if silo is None:
            return tuple(sorted(self._by_symbol))
        return tuple(sorted({spec.symbol for spec in self.by_silo(silo)}))

    def counts(self) -> dict[str, int]:
        return {silo: len(self.by_silo(silo)) for silo in SILO_IDS}

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": "v1.6.instrument-registry",
            "total_instruments": len(self._by_key),
            "silo_counts": self.counts(),
            "silos": list(SILO_IDS),
            "instruments": [spec.to_dict() for spec in self.all()],
        }

    # -- validation --------------------------------------------------------
    def validate_order(
        self,
        silo: str,
        symbol: str,
        quantity: float,
        price: float,
        *,
        round_price: bool = True,
    ) -> InstrumentCheck:
        """Validate a quantity/price pair against the contract specification.

        ``ok`` is ``False`` for an unknown symbol, a non-finite/negative value,
        an off-lot quantity or a quantity above the exchange freeze limit.  An
        off-tick price is rounded to the nearest tick (half-up, deterministic)
        when ``round_price`` is true and reported via ``price_changed``;
        otherwise it is rejected.
        """
        try:
            spec = self.get(silo, symbol)
        except KeyError as exc:
            return InstrumentCheck(
                ok=False,
                reason=str(exc),
                symbol=str(symbol).upper(),
                silo=str(silo).upper(),
                quantity=0.0,
                price=0.0,
                adjusted_price=0.0,
                price_changed=False,
                lot_multiple=0.0,
                ticks=0,
                notional_micros=0,
            )

        try:
            qty = float(quantity)
            px = float(price)
        except (TypeError, ValueError):
            return self._reject(spec, quantity, price, "quantity and price must be numeric")
        if not math.isfinite(qty) or not math.isfinite(px):
            return self._reject(spec, qty, px, "quantity and price must be finite")
        if qty <= 0.0:
            return self._reject(spec, qty, px, "quantity must be > 0")
        if px <= 0.0:
            return self._reject(spec, qty, px, "price must be > 0")

        qty_micros = int(round(qty * MICROS))
        if spec.lot_size_micros > 0 and qty_micros % spec.lot_size_micros != 0:
            return self._reject(
                spec,
                qty,
                px,
                f"quantity {qty:g} is not a multiple of lot_size {spec.lot_size:g}",
            )
        if qty_micros > spec.freeze_quantity_micros:
            return self._reject(
                spec,
                qty,
                px,
                f"quantity {qty:g} exceeds freeze_quantity {spec.freeze_quantity:g}",
            )

        price_micros = int(round(px * MICROS))
        tick = spec.tick_size_micros
        ticks = int(round(price_micros / tick))
        adjusted = ticks * tick
        changed = adjusted != price_micros
        if changed and not round_price:
            return self._reject(
                spec,
                qty,
                px,
                f"price {px:g} is off the {spec.tick_size_micros} micro tick grid",
            )
        notional = (qty_micros * adjusted) // MICROS
        return InstrumentCheck(
            ok=True,
            reason="OK" if not changed else "PRICE_ROUNDED_TO_TICK",
            symbol=spec.symbol,
            silo=spec.silo,
            quantity=qty,
            price=px,
            adjusted_price=adjusted / MICROS,
            price_changed=changed,
            lot_multiple=qty / spec.lot_size if spec.lot_size else 0.0,
            ticks=ticks,
            notional_micros=notional,
        )

    @staticmethod
    def _reject(
        spec: InstrumentSpec, qty: float, price: float, reason: str
    ) -> InstrumentCheck:
        return InstrumentCheck(
            ok=False,
            reason=reason,
            symbol=spec.symbol,
            silo=spec.silo,
            quantity=float(qty) if isinstance(qty, (int, float)) else 0.0,
            price=float(price) if isinstance(price, (int, float)) else 0.0,
            adjusted_price=0.0,
            price_changed=False,
            lot_multiple=0.0,
            ticks=0,
            notional_micros=0,
        )


# ---------------------------------------------------------------------------
# Canonical contract specifications (``sec_014`` / ``sec_015``)
# ---------------------------------------------------------------------------

# India: NSE cash equity + index derivatives.  Tick ₹0.05, T+1 cash equity,
# T+0 intraday derivatives, 20% equity circuit band (10% for index futures).
_INDIA_ROWS: tuple[tuple[Any, ...], ...] = (
    # symbol, asset_class, lot, freeze, circuit, im, mm, settle, strike_step
    ("NIFTY", "INDEX_FUTURE", 75.0, 1800.0, 10.0, 12.0, 8.0, "T+0", 50.0),
    ("BANKNIFTY", "INDEX_FUTURE", 35.0, 900.0, 10.0, 12.0, 8.0, "T+0", 100.0),
    ("FINNIFTY", "INDEX_FUTURE", 65.0, 1800.0, 10.0, 12.0, 8.0, "T+0", 50.0),
    ("MIDCPNIFTY", "INDEX_FUTURE", 120.0, 2400.0, 10.0, 12.0, 8.0, "T+0", 25.0),
    ("RELIANCE", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 20.0),
    ("TCS", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 50.0),
    ("HDFCBANK", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 20.0),
    ("INFY", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 20.0),
    ("ICICIBANK", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 20.0),
    ("SBIN", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 10.0),
    ("LT", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 50.0),
    ("AXISBANK", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 10.0),
    ("KOTAKBANK", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 20.0),
    ("BHARTIARTL", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 20.0),
    ("ITC", "EQUITY", 1.0, 1800.0, 20.0, 20.0, 12.5, "T+1", 5.0),
)

_CRYPTO_ROWS: tuple[tuple[Any, ...], ...] = (
    # symbol, lot, freeze, tick micros
    ("BTCUSDT", 0.001, 100.0, 10_000),
    ("ETHUSDT", 0.001, 1_000.0, 10_000),
    ("SOLUSDT", 0.01, 10_000.0, 1_000),
    ("BNBUSDT", 0.001, 1_000.0, 10_000),
    ("XRPUSDT", 1.0, 1_000_000.0, 100),
    ("ADAUSDT", 1.0, 1_000_000.0, 100),
    ("DOGEUSDT", 1.0, 5_000_000.0, 10),
    ("AVAXUSDT", 0.01, 50_000.0, 1_000),
    ("LINKUSDT", 0.01, 50_000.0, 1_000),
    ("MATICUSDT", 1.0, 1_000_000.0, 100),
)

_US_ROWS: tuple[tuple[Any, ...], ...] = (
    ("SPY", "ETF", 1.0, 5_000.0, 20.0, 25.0, 15.0, "T+1", 1.0),
    ("QQQ", "ETF", 1.0, 5_000.0, 20.0, 25.0, 15.0, "T+1", 1.0),
    ("IWM", "ETF", 1.0, 5_000.0, 20.0, 25.0, 15.0, "T+1", 1.0),
    ("DIA", "ETF", 1.0, 5_000.0, 20.0, 25.0, 15.0, "T+1", 1.0),
    ("NVDA", "EQUITY", 1.0, 10_000.0, 20.0, 25.0, 15.0, "T+1", 2.5),
    ("AAPL", "EQUITY", 1.0, 10_000.0, 20.0, 25.0, 15.0, "T+1", 2.5),
    ("MSFT", "EQUITY", 1.0, 10_000.0, 20.0, 25.0, 15.0, "T+1", 2.5),
    ("TSLA", "EQUITY", 1.0, 10_000.0, 20.0, 25.0, 15.0, "T+1", 2.5),
    ("AMZN", "EQUITY", 1.0, 10_000.0, 20.0, 25.0, 15.0, "T+1", 2.5),
    ("GOOGL", "EQUITY", 1.0, 10_000.0, 20.0, 25.0, 15.0, "T+1", 2.5),
    ("META", "EQUITY", 1.0, 10_000.0, 20.0, 25.0, 15.0, "T+1", 2.5),
    ("AMD", "EQUITY", 1.0, 10_000.0, 20.0, 25.0, 15.0, "T+1", 2.5),
)

_FX_ROWS: tuple[tuple[Any, ...], ...] = (
    # symbol, lot, freeze, tick micros, strike step
    ("EURUSD", 100_000.0, 5_000_000.0, 10, 0.005),
    ("GBPUSD", 100_000.0, 5_000_000.0, 10, 0.005),
    ("USDJPY", 100_000.0, 5_000_000.0, 10, 0.01),
    ("AUDUSD", 100_000.0, 5_000_000.0, 10, 0.005),
    ("USDCAD", 100_000.0, 5_000_000.0, 10, 0.005),
    ("USDCHF", 100_000.0, 5_000_000.0, 10, 0.005),
    ("NZDUSD", 100_000.0, 5_000_000.0, 10, 0.005),
    ("EURGBP", 100_000.0, 5_000_000.0, 10, 0.005),
    ("XAUUSD", 100.0, 1_000.0, 10_000, 1.0),
    ("XAGUSD", 5_000.0, 50_000.0, 1_000, 0.05),
    ("USDINR", 100_000.0, 5_000_000.0, 10, 0.01),
)


def _build() -> InstrumentRegistry:
    rows: list[InstrumentSpec] = []
    for symbol, asset, lot, freeze, circuit, im, mm, settle, step in _INDIA_ROWS:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                silo="INDIA_ZERODHA_DHAN",
                venue="NSE/BSE",
                asset_class=asset,
                currency="INR",
                lot_size=lot,
                tick_size_micros=50_000,  # ₹0.05
                freeze_quantity=freeze,
                circuit_limit_pct=circuit,
                margin_initial_pct=im,
                margin_maintenance_pct=mm,
                settlement_cycle=settle,
                trading_hours_utc="03:45-10:00",
                option_strike_step=step,
            )
        )
    for symbol, lot, freeze, tick in _CRYPTO_ROWS:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                silo="CRYPTO_BINANCE",
                venue="BINANCE",
                asset_class="PERPETUAL",
                currency="USDT",
                lot_size=lot,
                tick_size_micros=tick,
                freeze_quantity=freeze,
                circuit_limit_pct=0.0,  # perpetuals have no exchange band
                margin_initial_pct=10.0,
                margin_maintenance_pct=5.0,
                settlement_cycle="PERPETUAL",
                trading_hours_utc="00:00-24:00",
                option_strike_step=0.0,
            )
        )
    for symbol, asset, lot, freeze, circuit, im, mm, settle, step in _US_ROWS:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                silo="US_IBKR",
                venue="IBKR",
                asset_class=asset,
                currency="USD",
                lot_size=lot,
                tick_size_micros=10_000,  # $0.01
                freeze_quantity=freeze,
                circuit_limit_pct=circuit,
                margin_initial_pct=im,
                margin_maintenance_pct=mm,
                settlement_cycle=settle,
                trading_hours_utc="13:30-20:00",
                option_strike_step=step,
            )
        )
    for symbol, lot, freeze, tick, step in _FX_ROWS:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                silo="FX_MT5",
                venue="MT5",
                asset_class="FX" if symbol != "XAUUSD" else "METAL",
                currency="USD" if symbol != "USDINR" else "INR",
                lot_size=lot,
                tick_size_micros=tick,
                freeze_quantity=freeze,
                circuit_limit_pct=0.0,
                margin_initial_pct=2.0,
                margin_maintenance_pct=1.0,
                settlement_cycle="T+1",
                trading_hours_utc="22:00-21:00",
                option_strike_step=step,
            )
        )
    return InstrumentRegistry(rows)


#: Module-level singleton used by Ring 0 / Ring 1 / Ring 3.
registry: InstrumentRegistry = _build()
instrument_registry = registry


def canonical_instruments() -> tuple[str, ...]:
    """Every canonical root symbol, de-duplicated and sorted."""
    return registry.symbols()


def validate_order(
    silo: str, symbol: str, quantity: float, price: float, **kwargs: Any
) -> InstrumentCheck:
    """Module-level convenience wrapper around :meth:`InstrumentRegistry.validate_order`."""
    return registry.validate_order(silo, symbol, quantity, price, **kwargs)


def describe_registry() -> Mapping[str, Any]:  # pragma: no cover - thin alias
    return registry.snapshot()
