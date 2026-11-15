"""NIOS master instrument & contract-specification registry (V1.7).

One deterministic, queryable table of every canonical instrument traded by the
four sovereign silos.  Nothing here talks to a venue: the registry is a
*contract specification* (``sec_014`` / ``sec_015``) that Ring 0 and Ring 1
agree on before an order is ever reserved.

V1.7 scope
----------
The catalogue holds **274 institutional instruments** across all four silos:

======================  =====  ==================================================
Silo                    Count  Coverage
======================  =====  ==================================================
``INDIA_ZERODHA_DHAN``     76  8 indices, all 50 NIFTY 50 constituents, 14 MCX
                               commodity futures, 4 NSE currency futures
``CRYPTO_BINANCE``         80  30 layer-1 / blue-chip perps + 50 DeFi, L2, AI,
                               DePIN, RWA, gaming and meme perps
``US_IBKR``                79  50 mega/large-cap equities, 23 benchmark, sector,
                               fixed-income and commodity ETFs, 6 CME futures
``FX_MT5``                 39  28 G10 majors + crosses, 5 EM crosses, 4 precious
                               metals, 2 energy CFDs
======================  =====  ==================================================

Guarantees
----------
* Every spec is complete: symbol, display name, silo, venue, asset class,
  sub-category, sector, currency, lot size, tick size, freeze quantity, circuit
  band, initial/maintenance margin, settlement cycle, UTC session, option strike
  step, option eligibility and a venue identifier are all present.
* Every numeric field is validated at construction; a negative or non-finite
  value can never enter the registry.
* ``validate_order`` is integer-only: quantities are compared in micro-units
  and prices rounded to the exact tick grid, so a float can never smuggle a
  half-tick price past the outbox.
* ``query`` is the single deterministic filter/sort/paginate primitive behind
  ``GET /api/v2/instruments``: same arguments, same rows, same order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable, Mapping

__all__ = [
    "MICROS",
    "SILO_IDS",
    "SETTLEMENT_CYCLES",
    "SettlementCycle",
    "InstrumentSpec",
    "InstrumentCheck",
    "InstrumentRegistry",
    "registry",
    "instrument_registry",
    "canonical_instruments",
    "validate_order",
    "describe_registry",
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
SETTLEMENT_CYCLES: tuple[str, ...] = ("T+0", "T+1", "T+2", "PERPETUAL")
_SETTLEMENT = SETTLEMENT_CYCLES

SettlementCycle = str


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


_US_NASDAQ_EQUITIES = frozenset(
    {
        "NVDA",
        "AAPL",
        "MSFT",
        "AMZN",
        "GOOGL",
        "GOOG",
        "META",
        "TSLA",
        "AVGO",
        "COST",
        "NFLX",
        "AMD",
        "ADBE",
        "PEP",
        "CSCO",
        "INTC",
        "QCOM",
        "TXN",
        "AMAT",
        "MU",
        "PLTR",
        "COIN",
        "MSTR",
        "ARM",
    }
)
_US_NASDAQ_ETFS = frozenset({"QQQ", "SMH", "TLT", "IEF"})
_US_FUTURE_EXCHANGE = {
    "ES": "CME_MINI",
    "NQ": "CME_MINI",
    "MES": "CME_MINI",
    "MNQ": "CME_MINI",
    "CL": "NYMEX",
    "GC": "COMEX",
}
_TV_FX_ENERGY = {"USOIL": "TVC:USOIL", "UKOIL": "TVC:UKOIL"}
_TV_SYMBOL_CHARS = frozenset("_.!%&")


def derive_tradingview_ticker(
    *,
    symbol: str,
    silo: str,
    venue: str,
    asset_class: str,
    sub_category: str = "",
) -> str:
    """Deterministic ``EXCHANGE:SYMBOL`` TradingView ticker for a NIOS instrument.

    The mapping is a pure function of the frozen instrument contract (silo, venue,
    asset class and symbol) so every one of the 274 mapped instruments resolves to a
    stable, exchange-prefixed TradingView feed without any network call.
    """
    sym = str(symbol or "").strip().upper()
    silo = str(silo or "").strip().upper()
    venue = str(venue or "").strip().upper()
    asset_class = str(asset_class or "").strip().upper()
    if not sym:
        raise ValueError("symbol must not be empty")
    if silo == "CRYPTO_BINANCE":
        return f"BINANCE:{sym}.P"
    if silo == "FX_MT5":
        if asset_class == "ENERGY":
            return _TV_FX_ENERGY.get(sym, f"TVC:{sym}")
        return f"OANDA:{sym}"
    if silo == "US_IBKR":
        if asset_class == "FUTURE":
            exchange = _US_FUTURE_EXCHANGE.get(sym)
            if exchange is None:
                raise ValueError(f"no TradingView futures exchange for {sym!r}")
            return f"{exchange}:{sym}1!"
        if asset_class == "ETF":
            prefix = "NASDAQ" if sym in _US_NASDAQ_ETFS else "AMEX"
            return f"{prefix}:{sym}"
        prefix = "NASDAQ" if sym in _US_NASDAQ_EQUITIES else "NYSE"
        return f"{prefix}:{sym}"
    if silo == "INDIA_ZERODHA_DHAN":
        flat = sym.replace("&", "_")
        if venue == "BSE":
            return f"BSE:{flat}"
        if venue == "MCX":
            return f"MCX:{flat}1!"
        if asset_class == "CURRENCY_FUTURE":
            return f"NSE:{flat}1!"
        return f"NSE:{flat}"
    raise ValueError(f"no TradingView mapping for silo {silo!r}")


def _normalise_tradingview_ticker(value: str) -> str:
    ticker = str(value or "").strip().upper()
    exchange, sep, rest = ticker.partition(":")
    if not sep or not exchange or not rest:
        raise ValueError(f"tradingview_ticker must be 'EXCHANGE:SYMBOL' (got {value!r})")
    for char in exchange:
        if not (char.isalnum() or char == "_"):
            raise ValueError(f"invalid TradingView exchange {exchange!r}")
    for char in rest:
        if not (char.isalnum() or char in _TV_SYMBOL_CHARS):
            raise ValueError(f"invalid TradingView symbol {rest!r}")
    return ticker


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
    display_name: str = ""
    sub_category: str = "GENERIC"
    sector: str = "DIVERSIFIED"
    has_options: bool = False
    isin_or_id: str = ""
    tradingview_ticker: str = ""

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
        display = str(self.display_name or "").strip()
        if not display:
            display = symbol
        object.__setattr__(self, "display_name", display)
        object.__setattr__(self, "venue", str(self.venue or "").strip().upper() or "UNKNOWN")
        object.__setattr__(
            self, "asset_class", str(self.asset_class or "").strip().upper() or "UNKNOWN"
        )
        object.__setattr__(
            self, "sub_category", str(self.sub_category or "").strip().upper() or "GENERIC"
        )
        object.__setattr__(
            self, "sector", str(self.sector or "").strip() or "DIVERSIFIED"
        )
        object.__setattr__(
            self, "currency", str(self.currency or "").strip().upper() or "USD"
        )
        object.__setattr__(
            self, "isin_or_id", str(self.isin_or_id or "").strip() or f"NIOS:{symbol}"
        )
        ticker = str(self.tradingview_ticker or "").strip()
        if ticker:
            ticker = _normalise_tradingview_ticker(ticker)
        else:
            ticker = derive_tradingview_ticker(
                symbol=symbol,
                silo=self.silo,
                venue=self.venue,
                asset_class=self.asset_class,
                sub_category=self.sub_category,
            )
        object.__setattr__(self, "tradingview_ticker", ticker)
        object.__setattr__(self, "has_options", bool(self.has_options))

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

    def matches(
        self,
        *,
        silo: str | None = None,
        asset_class: str | None = None,
        sector: str | None = None,
        has_options: bool | None = None,
        search: str | None = None,
    ) -> bool:
        """Deterministic predicate behind ``InstrumentRegistry.query``."""
        if silo is not None and self.silo != str(silo).strip().upper():
            return False
        if asset_class is not None and self.asset_class != str(asset_class).strip().upper():
            return False
        if sector is not None and self.sector != str(sector).strip():
            return False
        if has_options is not None and self.has_options is not bool(has_options):
            return False
        if search:
            needle = str(search).strip().lower()
            if needle:
                haystack = (
                    self.symbol,
                    self.display_name,
                    self.sub_category,
                    self.sector,
                    self.asset_class,
                    self.venue,
                    self.isin_or_id,
                    self.tradingview_ticker,
                )
                if not any(needle in str(field).lower() for field in haystack):
                    return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "display_name": self.display_name,
            "silo": self.silo,
            "venue": self.venue,
            "asset_class": self.asset_class,
            "sub_category": self.sub_category,
            "sector": self.sector,
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
            "has_options": self.has_options,
            "isin_or_id": self.isin_or_id,
            "tradingview_ticker": self.tradingview_ticker,
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

    SCHEMA_VERSION: ClassVar[str] = "v1.7.instrument-registry"

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
        existing = self._by_symbol.get(spec.symbol)
        if existing:
            raise ValueError(
                f"duplicate canonical symbol {spec.symbol!r} "
                f"(already held by {existing[0].silo})"
            )
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

    def resolve(self, symbol: str) -> InstrumentSpec | None:
        """Case/format-tolerant single-symbol lookup used by the REST layer."""
        try:
            return self.find(symbol)
        except KeyError:
            pass
        raw = str(symbol or "").strip().upper()
        if not raw:
            return None
        compact = "".join(ch for ch in raw if ch.isalnum())
        for token in (compact, compact + "T" if compact.endswith("USD") else compact):
            matches = self._by_symbol.get(token) or []
            if len(matches) == 1:
                return matches[0]
        return None

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

    def total(self) -> int:
        return len(self._by_key)

    def sectors(self, silo: str | None = None) -> dict[str, int]:
        """Sorted sector -> instrument-count histogram (optionally per silo)."""
        pool = self.by_silo(silo) if silo is not None else self.all()
        histogram: dict[str, int] = {}
        for spec in pool:
            histogram[spec.sector] = histogram.get(spec.sector, 0) + 1
        return {key: histogram[key] for key in sorted(histogram)}

    def asset_classes(self, silo: str | None = None) -> dict[str, int]:
        pool = self.by_silo(silo) if silo is not None else self.all()
        histogram: dict[str, int] = {}
        for spec in pool:
            histogram[spec.asset_class] = histogram.get(spec.asset_class, 0) + 1
        return {key: histogram[key] for key in sorted(histogram)}

    def query(
        self,
        *,
        silo: str | None = None,
        asset_class: str | None = None,
        sector: str | None = None,
        search: str | None = None,
        has_options: bool | None = None,
        tv_ticker: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Deterministic filter -> sort -> paginate over the whole catalogue.

        The stable ordering is ``(silo, symbol)`` so two identical calls always
        return the identical page, and an ``offset`` walk never skips or repeats
        a row even when the registry is rebuilt.
        """
        limit = int(limit)
        offset = int(offset)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be within 1..500")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        rows = [
            spec
            for spec in sorted(self._by_key.values(), key=lambda s: (s.silo, s.symbol))
            if spec.matches(
                silo=silo,
                asset_class=asset_class,
                sector=sector,
                has_options=has_options,
                search=search,
            )
            and (tv_ticker is None or spec.tradingview_ticker == str(tv_ticker).strip().upper())
        ]
        page = rows[offset : offset + limit]
        silo_counts: dict[str, int] = {silo_id: 0 for silo_id in SILO_IDS}
        for spec in rows:
            silo_counts[spec.silo] = silo_counts.get(spec.silo, 0) + 1
        return {
            "total_count": len(rows),
            "returned_count": len(page),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page) < len(rows),
            "silo_counts": silo_counts,
            "filters": {
                "silo": None if silo is None else str(silo).strip().upper(),
                "asset_class": (
                    None if asset_class is None else str(asset_class).strip().upper()
                ),
                "sector": None if sector is None else str(sector).strip(),
                "search": None if search is None else str(search).strip(),
                "has_options": has_options,
                "tv_ticker": None if tv_ticker is None else str(tv_ticker).strip().upper(),
            },
            "items": page,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "total_instruments": len(self._by_key),
            "silo_counts": self.counts(),
            "silos": list(SILO_IDS),
            "sector_counts": self.sectors(),
            "asset_class_counts": self.asset_classes(),
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

#: NSE/BSE index derivatives.  Tick ₹0.05, T+0 intraday settlement, 10% index
#: circuit band, SPAN+exposure style initial margin.
#: ``symbol, display_name, sector, venue, lot, freeze, tick, strike_step, options``
_INDIA_INDICES: tuple[tuple[Any, ...], ...] = (
    ("NIFTY", "Nifty 50 Index Futures", "Broad Market", "NSE", 75.0, 1_800.0, 50_000, 50.0, True),
    ("BANKNIFTY", "Nifty Bank Index Futures", "Banking & Financials", "NSE", 35.0, 900.0, 50_000, 100.0, True),
    ("FINNIFTY", "Nifty Financial Services Index Futures", "Banking & Financials", "NSE", 65.0, 1_800.0, 50_000, 50.0, True),
    ("MIDCPNIFTY", "Nifty Midcap Select Index Futures", "Broad Market", "NSE", 120.0, 2_400.0, 50_000, 25.0, True),
    ("NIFTYNXT50", "Nifty Next 50 Index Futures", "Broad Market", "NSE", 25.0, 500.0, 50_000, 100.0, True),
    ("SENSEX", "BSE Sensex Index Futures", "Broad Market", "BSE", 20.0, 500.0, 50_000, 100.0, True),
    ("BANKEX", "BSE Bankex Index Futures", "Banking & Financials", "BSE", 15.0, 500.0, 50_000, 100.0, True),
    ("INDIAVIX", "India VIX Volatility Index", "Broad Market", "NSE", 1.0, 10_000.0, 50_000, 0.0, False),
)

#: Every NIFTY 50 constituent with its authentic NSE F&O lot size.
#: ``symbol, display_name, sub_category, sector, lot, strike_step``
_INDIA_EQUITIES: tuple[tuple[Any, ...], ...] = (
    ("RELIANCE", "Reliance Industries Ltd", "REFINING_AND_PETROCHEMICALS", "Oil & Gas", 500.0, 20.0),
    ("TCS", "Tata Consultancy Services Ltd", "IT_SERVICES", "IT", 175.0, 50.0),
    ("HDFCBANK", "HDFC Bank Ltd", "PRIVATE_BANK", "Banking & Financials", 550.0, 20.0),
    ("ICICIBANK", "ICICI Bank Ltd", "PRIVATE_BANK", "Banking & Financials", 700.0, 20.0),
    ("BHARTIARTL", "Bharti Airtel Ltd", "TELECOM", "Capital Goods & Infra", 475.0, 20.0),
    ("INFY", "Infosys Ltd", "IT_SERVICES", "IT", 400.0, 20.0),
    ("SBIN", "State Bank of India", "PSU_BANK", "Banking & Financials", 750.0, 10.0),
    ("LICI", "Life Insurance Corporation of India", "LIFE_INSURANCE", "Banking & Financials", 600.0, 10.0),
    ("ITC", "ITC Ltd", "DIVERSIFIED_FMCG", "FMCG", 1_600.0, 5.0),
    ("HINDUNILVR", "Hindustan Unilever Ltd", "HOME_AND_PERSONAL_CARE", "FMCG", 300.0, 50.0),
    ("LT", "Larsen & Toubro Ltd", "ENGINEERING_AND_CONSTRUCTION", "Capital Goods & Infra", 175.0, 50.0),
    ("BAJFINANCE", "Bajaj Finance Ltd", "NBFC", "Banking & Financials", 1_250.0, 100.0),
    ("HCLTECH", "HCL Technologies Ltd", "IT_SERVICES", "IT", 350.0, 20.0),
    ("MARUTI", "Maruti Suzuki India Ltd", "PASSENGER_VEHICLES", "Automobiles", 50.0, 100.0),
    ("SUNPHARMA", "Sun Pharmaceutical Industries Ltd", "PHARMACEUTICALS", "Pharma & Healthcare", 350.0, 20.0),
    ("ADANIENT", "Adani Enterprises Ltd", "CONGLOMERATE", "Capital Goods & Infra", 300.0, 50.0),
    ("KOTAKBANK", "Kotak Mahindra Bank Ltd", "PRIVATE_BANK", "Banking & Financials", 400.0, 20.0),
    ("TITAN", "Titan Company Ltd", "JEWELLERY_AND_WATCHES", "Consumer Durables", 175.0, 50.0),
    ("ONGC", "Oil & Natural Gas Corporation Ltd", "UPSTREAM_E_AND_P", "Oil & Gas", 3_850.0, 5.0),
    ("TATAMOTORS", "Tata Motors Ltd", "AUTOMOBILES", "Automobiles", 550.0, 10.0),
    ("NTPC", "NTPC Ltd", "POWER_GENERATION", "Power & Utilities", 1_500.0, 5.0),
    ("AXISBANK", "Axis Bank Ltd", "PRIVATE_BANK", "Banking & Financials", 625.0, 10.0),
    ("ULTRACEMCO", "UltraTech Cement Ltd", "CEMENT", "Capital Goods & Infra", 50.0, 100.0),
    ("DMART", "Avenue Supermarts Ltd", "RETAIL", "Consumer Durables", 150.0, 50.0),
    ("ADANIPORTS", "Adani Ports & Special Economic Zone Ltd", "PORTS_AND_LOGISTICS", "Capital Goods & Infra", 475.0, 20.0),
    ("BAJAJFINSV", "Bajaj Finserv Ltd", "NBFC", "Banking & Financials", 500.0, 20.0),
    ("ASIANPAINT", "Asian Paints Ltd", "PAINTS", "Consumer Durables", 200.0, 20.0),
    ("COALINDIA", "Coal India Ltd", "COAL_MINING", "Metals & Mining", 1_350.0, 5.0),
    ("WIPRO", "Wipro Ltd", "IT_SERVICES", "IT", 3_000.0, 5.0),
    ("NESTLEIND", "Nestle India Ltd", "PACKAGED_FOODS", "FMCG", 250.0, 50.0),
    ("JSWSTEEL", "JSW Steel Ltd", "STEEL", "Metals & Mining", 675.0, 10.0),
    ("POWERGRID", "Power Grid Corporation of India Ltd", "POWER_TRANSMISSION", "Power & Utilities", 3_600.0, 5.0),
    ("M&M", "Mahindra & Mahindra Ltd", "AUTOMOBILES", "Automobiles", 200.0, 20.0),
    ("TATASTEEL", "Tata Steel Ltd", "STEEL", "Metals & Mining", 5_500.0, 5.0),
    ("IOC", "Indian Oil Corporation Ltd", "REFINING_AND_MARKETING", "Oil & Gas", 9_750.0, 2.5),
    ("SBILIFE", "SBI Life Insurance Company Ltd", "LIFE_INSURANCE", "Banking & Financials", 375.0, 20.0),
    ("GRASIM", "Grasim Industries Ltd", "CEMENT_AND_TEXTILES", "Capital Goods & Infra", 250.0, 20.0),
    ("HDFCLIFE", "HDFC Life Insurance Company Ltd", "LIFE_INSURANCE", "Banking & Financials", 1_100.0, 10.0),
    ("TECHM", "Tech Mahindra Ltd", "IT_SERVICES", "IT", 600.0, 20.0),
    ("HINDALCO", "Hindalco Industries Ltd", "ALUMINIUM", "Metals & Mining", 1_400.0, 10.0),
    ("EICHERMOT", "Eicher Motors Ltd", "TWO_WHEELERS", "Automobiles", 175.0, 50.0),
    ("INDUSINDBK", "IndusInd Bank Ltd", "PRIVATE_BANK", "Banking & Financials", 500.0, 20.0),
    ("BRITANNIA", "Britannia Industries Ltd", "PACKAGED_FOODS", "FMCG", 125.0, 100.0),
    ("CIPLA", "Cipla Ltd", "PHARMACEUTICALS", "Pharma & Healthcare", 375.0, 20.0),
    ("DRREDDY", "Dr Reddy's Laboratories Ltd", "PHARMACEUTICALS", "Pharma & Healthcare", 625.0, 20.0),
    ("APOLLOHOSP", "Apollo Hospitals Enterprise Ltd", "HOSPITALS", "Pharma & Healthcare", 125.0, 100.0),
    ("TATACONSUM", "Tata Consumer Products Ltd", "PACKAGED_FOODS", "FMCG", 550.0, 10.0),
    ("DIVISLAB", "Divi's Laboratories Ltd", "PHARMACEUTICALS", "Pharma & Healthcare", 100.0, 100.0),
    ("HEROMOTOCO", "Hero MotoCorp Ltd", "TWO_WHEELERS", "Automobiles", 150.0, 50.0),
    ("BPCL", "Bharat Petroleum Corporation Ltd", "REFINING_AND_MARKETING", "Oil & Gas", 1_800.0, 5.0),
)

#: MCX commodity futures: ``symbol, display_name, sub_category, sector, lot,
#: freeze, tick_micros, strike_step, options``.
_INDIA_COMMODITIES: tuple[tuple[Any, ...], ...] = (
    ("GOLD", "MCX Gold Futures (1 kg)", "PRECIOUS_METALS", "Metals & Mining", 100.0, 1_000.0, 100_000, 500.0, True),
    ("GOLDM", "MCX Gold Mini Futures (100 g)", "PRECIOUS_METALS", "Metals & Mining", 10.0, 100.0, 100_000, 100.0, False),
    ("GOLDGUINEA", "MCX Gold Guinea Futures (8 g)", "PRECIOUS_METALS", "Metals & Mining", 1.0, 100.0, 100_000, 0.0, False),
    ("SILVER", "MCX Silver Futures (30 kg)", "PRECIOUS_METALS", "Metals & Mining", 30.0, 300.0, 1_000_000, 1_000.0, True),
    ("SILVERM", "MCX Silver Mini Futures (5 kg)", "PRECIOUS_METALS", "Metals & Mining", 5.0, 100.0, 1_000_000, 250.0, False),
    ("SILVERMIC", "MCX Silver Micro Futures (1 kg)", "PRECIOUS_METALS", "Metals & Mining", 1.0, 100.0, 1_000_000, 0.0, False),
    ("CRUDEOIL", "MCX Crude Oil Futures (100 bbl)", "ENERGY", "Oil & Gas", 100.0, 1_000.0, 100_000, 50.0, True),
    ("CRUDEOILM", "MCX Crude Oil Mini Futures (10 bbl)", "ENERGY", "Oil & Gas", 10.0, 100.0, 100_000, 25.0, False),
    ("NATURALGAS", "MCX Natural Gas Futures (1250 mmBtu)", "ENERGY", "Oil & Gas", 1_250.0, 12_500.0, 10_000, 2.5, True),
    ("COPPER", "MCX Copper Futures (2500 kg)", "BASE_METALS", "Metals & Mining", 2_500.0, 25_000.0, 100_000, 2.5, True),
    ("ZINC", "MCX Zinc Futures (5000 kg)", "BASE_METALS", "Metals & Mining", 5_000.0, 50_000.0, 100_000, 1.0, False),
    ("ALUMINIUM", "MCX Aluminium Futures (5000 kg)", "BASE_METALS", "Metals & Mining", 5_000.0, 50_000.0, 100_000, 1.0, False),
    ("LEAD", "MCX Lead Futures (5000 kg)", "BASE_METALS", "Metals & Mining", 5_000.0, 50_000.0, 100_000, 1.0, False),
    ("NICKEL", "MCX Nickel Futures (1500 kg)", "BASE_METALS", "Metals & Mining", 1_500.0, 15_000.0, 100_000, 2.5, False),
)

#: NSE currency derivatives: ``symbol, display_name, lot, freeze, tick_micros,
#: strike_step``.
_INDIA_CURRENCY: tuple[tuple[Any, ...], ...] = (
    ("USDINR", "USD/INR Currency Futures", 1_000.0, 10_000.0, 2_500, 0.01),
    ("EURINR", "EUR/INR Currency Futures", 1_000.0, 10_000.0, 2_500, 0.01),
    ("GBPINR", "GBP/INR Currency Futures", 1_000.0, 10_000.0, 2_500, 0.05),
    ("JPYINR", "JPY/INR Currency Futures (per 100 JPY)", 100_000.0, 1_000_000.0, 2_500, 0.01),
)

#: Binance USDT perpetuals.  ``symbol, display_name, narrative, tick_micros,
#: lot, freeze, options``.  Tier helpers below keep the smaller caps coherent.
_CRYPTO_ROWS: tuple[tuple[Any, ...], ...] = (
    # -- layer-1 / blue-chip -------------------------------------------------
    ("BTCUSDT", "Bitcoin / TetherUS Perpetual", "Layer-1 Smart Contracts", 10_000, 0.001, 100.0, True),
    ("ETHUSDT", "Ethereum / TetherUS Perpetual", "Layer-1 Smart Contracts", 10_000, 0.001, 1_000.0, True),
    ("SOLUSDT", "Solana / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 10_000.0, True),
    ("BNBUSDT", "BNB / TetherUS Perpetual", "Exchange & Staking", 10_000, 0.001, 1_000.0, False),
    ("XRPUSDT", "XRP / TetherUS Perpetual", "Layer-1 Smart Contracts", 100, 1.0, 1_000_000.0, False),
    ("ADAUSDT", "Cardano / TetherUS Perpetual", "Layer-1 Smart Contracts", 100, 1.0, 1_000_000.0, False),
    ("AVAXUSDT", "Avalanche / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("TONUSDT", "Toncoin / TetherUS Perpetual", "Layer-1 Smart Contracts", 100, 1.0, 500_000.0, False),
    ("TRXUSDT", "TRON / TetherUS Perpetual", "Layer-1 Smart Contracts", 10, 1.0, 5_000_000.0, False),
    ("DOTUSDT", "Polkadot / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("NEARUSDT", "NEAR Protocol / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("APTUSDT", "Aptos / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("SUIUSDT", "Sui / TetherUS Perpetual", "Layer-1 Smart Contracts", 100, 1.0, 500_000.0, False),
    ("SEIUSDT", "Sei / TetherUS Perpetual", "Layer-1 Smart Contracts", 100, 1.0, 500_000.0, False),
    ("TIAUSDT", "Celestia / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("ATOMUSDT", "Cosmos / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("KASUSDT", "Kaspa / TetherUS Perpetual", "Layer-1 Smart Contracts", 10, 1.0, 5_000_000.0, False),
    ("INJUSDT", "Injective / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("FTMUSDT", "Fantom / TetherUS Perpetual", "Layer-1 Smart Contracts", 100, 1.0, 500_000.0, False),
    ("ALGOUSDT", "Algorand / TetherUS Perpetual", "Layer-1 Smart Contracts", 100, 1.0, 500_000.0, False),
    ("HBARUSDT", "Hedera / TetherUS Perpetual", "Layer-1 Smart Contracts", 10, 1.0, 5_000_000.0, False),
    ("ICPUSDT", "Internet Computer / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("ETCUSDT", "Ethereum Classic / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("BCHUSDT", "Bitcoin Cash / TetherUS Perpetual", "Layer-1 Smart Contracts", 10_000, 0.001, 1_000.0, False),
    ("LTCUSDT", "Litecoin / TetherUS Perpetual", "Layer-1 Smart Contracts", 10_000, 0.001, 1_000.0, False),
    ("XLMUSDT", "Stellar / TetherUS Perpetual", "Layer-1 Smart Contracts", 10, 1.0, 5_000_000.0, False),
    ("VETUSDT", "VeChain / TetherUS Perpetual", "Layer-1 Smart Contracts", 10, 1.0, 5_000_000.0, False),
    ("EGLDUSDT", "MultiversX / TetherUS Perpetual", "Layer-1 Smart Contracts", 1_000, 0.01, 50_000.0, False),
    ("FLOWUSDT", "Flow / TetherUS Perpetual", "Layer-1 Smart Contracts", 100, 1.0, 500_000.0, False),
    ("MINAUSDT", "Mina Protocol / TetherUS Perpetual", "Layer-1 Smart Contracts", 100, 1.0, 500_000.0, False),
    # -- DeFi, L2, AI/DePIN, RWA, gaming & meme ------------------------------
    ("LINKUSDT", "Chainlink / TetherUS Perpetual", "Infrastructure & Oracles", 1_000, 0.01, 50_000.0, False),
    ("UNIUSDT", "Uniswap / TetherUS Perpetual", "DeFi & DEX", 1_000, 0.01, 50_000.0, False),
    ("AAVEUSDT", "Aave / TetherUS Perpetual", "DeFi & DEX", 1_000, 0.01, 50_000.0, False),
    ("MKRUSDT", "Maker / TetherUS Perpetual", "DeFi & DEX", 10_000, 0.001, 1_000.0, False),
    ("LDOUSDT", "Lido DAO / TetherUS Perpetual", "DeFi & DEX", 1_000, 0.01, 50_000.0, False),
    ("CRVUSDT", "Curve DAO / TetherUS Perpetual", "DeFi & DEX", 100, 1.0, 500_000.0, False),
    ("SNXUSDT", "Synthetix / TetherUS Perpetual", "DeFi & DEX", 100, 1.0, 500_000.0, False),
    ("COMPUSDT", "Compound / TetherUS Perpetual", "DeFi & DEX", 1_000, 0.01, 50_000.0, False),
    ("PENDLEUSDT", "Pendle / TetherUS Perpetual", "DeFi & DEX", 1_000, 0.01, 50_000.0, False),
    ("JUPUSDT", "Jupiter / TetherUS Perpetual", "DeFi & DEX", 100, 1.0, 500_000.0, False),
    ("RUNEUSDT", "THORChain / TetherUS Perpetual", "DeFi & DEX", 1_000, 0.01, 50_000.0, False),
    ("DYDXUSDT", "dYdX / TetherUS Perpetual", "DeFi & DEX", 1_000, 0.01, 50_000.0, False),
    ("ARBUSDT", "Arbitrum / TetherUS Perpetual", "Layer-2 Scaling", 100, 1.0, 500_000.0, False),
    ("OPUSDT", "Optimism / TetherUS Perpetual", "Layer-2 Scaling", 1_000, 0.01, 50_000.0, False),
    ("MATICUSDT", "Polygon / TetherUS Perpetual", "Layer-2 Scaling", 100, 1.0, 1_000_000.0, False),
    ("STRKUSDT", "Starknet / TetherUS Perpetual", "Layer-2 Scaling", 100, 1.0, 500_000.0, False),
    ("IMXUSDT", "Immutable / TetherUS Perpetual", "Layer-2 Scaling", 100, 1.0, 500_000.0, False),
    ("ZKUSDT", "zkSync / TetherUS Perpetual", "Layer-2 Scaling", 100, 1.0, 500_000.0, False),
    ("STXUSDT", "Stacks / TetherUS Perpetual", "Layer-2 Scaling", 1_000, 0.01, 50_000.0, False),
    ("RENDERUSDT", "Render / TetherUS Perpetual", "AI & DePIN", 1_000, 0.01, 50_000.0, False),
    ("FETUSDT", "Artificial Superintelligence Alliance / TetherUS Perpetual", "AI & DePIN", 100, 1.0, 500_000.0, False),
    ("TAOUSDT", "Bittensor / TetherUS Perpetual", "AI & DePIN", 10_000, 0.001, 1_000.0, False),
    ("WLDUSDT", "Worldcoin / TetherUS Perpetual", "AI & DePIN", 1_000, 0.01, 50_000.0, False),
    ("ARKMUSDT", "Arkham / TetherUS Perpetual", "AI & DePIN", 100, 1.0, 500_000.0, False),
    ("GRTUSDT", "The Graph / TetherUS Perpetual", "AI & DePIN", 10, 1.0, 5_000_000.0, False),
    ("FILUSDT", "Filecoin / TetherUS Perpetual", "AI & DePIN", 1_000, 0.01, 50_000.0, False),
    ("ARUSDT", "Arweave / TetherUS Perpetual", "AI & DePIN", 1_000, 0.01, 50_000.0, False),
    ("THETAUSDT", "Theta Network / TetherUS Perpetual", "AI & DePIN", 100, 1.0, 500_000.0, False),
    ("ONDOUSDT", "Ondo / TetherUS Perpetual", "Real-World Assets (RWA)", 100, 1.0, 500_000.0, False),
    ("ENAUSDT", "Ethena / TetherUS Perpetual", "Real-World Assets (RWA)", 100, 1.0, 500_000.0, False),
    ("DOGEUSDT", "Dogecoin / TetherUS Perpetual", "Meme & Community", 10, 1.0, 5_000_000.0, False),
    ("SHIBUSDT", "Shiba Inu / TetherUS Perpetual", "Meme & Community", 1, 1.0, 1_000_000_000.0, False),
    ("PEPEUSDT", "Pepe / TetherUS Perpetual", "Meme & Community", 1, 1.0, 1_000_000_000.0, False),
    ("WIFUSDT", "dogwifhat / TetherUS Perpetual", "Meme & Community", 10, 1.0, 5_000_000.0, False),
    ("BONKUSDT", "Bonk / TetherUS Perpetual", "Meme & Community", 1, 1.0, 1_000_000_000.0, False),
    ("FLOKIUSDT", "Floki / TetherUS Perpetual", "Meme & Community", 1, 1.0, 1_000_000_000.0, False),
    ("ORDIUSDT", "ORDI / TetherUS Perpetual", "Infrastructure & Oracles", 1_000, 0.01, 50_000.0, False),
    ("GALAUSDT", "Gala / TetherUS Perpetual", "Gaming & Metaverse", 10, 1.0, 5_000_000.0, False),
    ("SANDUSDT", "The Sandbox / TetherUS Perpetual", "Gaming & Metaverse", 100, 1.0, 500_000.0, False),
    ("MANAUSDT", "Decentraland / TetherUS Perpetual", "Gaming & Metaverse", 100, 1.0, 500_000.0, False),
    ("AXSUSDT", "Axie Infinity / TetherUS Perpetual", "Gaming & Metaverse", 1_000, 0.01, 50_000.0, False),
    ("CHZUSDT", "Chiliz / TetherUS Perpetual", "Gaming & Metaverse", 10, 1.0, 5_000_000.0, False),
    ("ENSUSDT", "Ethereum Name Service / TetherUS Perpetual", "Infrastructure & Oracles", 1_000, 0.01, 50_000.0, False),
    ("PYTHUSDT", "Pyth Network / TetherUS Perpetual", "Infrastructure & Oracles", 100, 1.0, 500_000.0, False),
    ("WUSDT", "Wormhole / TetherUS Perpetual", "Infrastructure & Oracles", 100, 1.0, 500_000.0, False),
    ("EIGENUSDT", "EigenLayer / TetherUS Perpetual", "Infrastructure & Oracles", 1_000, 0.01, 50_000.0, False),
    ("ZROUSDT", "LayerZero / TetherUS Perpetual", "Infrastructure & Oracles", 1_000, 0.01, 50_000.0, False),
    ("JTOUSDT", "Jito / TetherUS Perpetual", "DeFi & DEX", 1_000, 0.01, 50_000.0, False),
    ("POPCATUSDT", "Popcat / TetherUS Perpetual", "Meme & Community", 10, 1.0, 5_000_000.0, False),
    ("NEIROUSDT", "Neiro / TetherUS Perpetual", "Meme & Community", 10, 1.0, 5_000_000.0, False),
)

#: US equities and ETFs cleared through IBKR: ``symbol, display_name,
#: asset_class, sub_category, sector, tick_micros, strike_step, options``.
_US_EQUITIES: tuple[tuple[Any, ...], ...] = (
    ("NVDA", "NVIDIA Corporation", "EQUITY", "SEMICONDUCTORS", "Information Technology", 10_000, 2.5, True),
    ("AAPL", "Apple Inc.", "EQUITY", "CONSUMER_ELECTRONICS", "Information Technology", 10_000, 2.5, True),
    ("MSFT", "Microsoft Corporation", "EQUITY", "SOFTWARE", "Information Technology", 10_000, 5.0, True),
    ("AMZN", "Amazon.com Inc.", "EQUITY", "E_COMMERCE", "Consumer Discretionary", 10_000, 2.5, True),
    ("GOOGL", "Alphabet Inc. Class A", "EQUITY", "INTERNET_CONTENT", "Communication Services", 10_000, 5.0, True),
    ("GOOG", "Alphabet Inc. Class C", "EQUITY", "INTERNET_CONTENT", "Communication Services", 10_000, 5.0, True),
    ("META", "Meta Platforms Inc.", "EQUITY", "SOCIAL_PLATFORMS", "Communication Services", 10_000, 10.0, True),
    ("TSLA", "Tesla Inc.", "EQUITY", "AUTOMOBILES", "Consumer Discretionary", 10_000, 5.0, True),
    ("BRK.B", "Berkshire Hathaway Inc. Class B", "EQUITY", "MULTI_SECTOR_HOLDING", "Financials", 10_000, 5.0, True),
    ("AVGO", "Broadcom Inc.", "EQUITY", "SEMICONDUCTORS", "Information Technology", 10_000, 10.0, True),
    ("LLY", "Eli Lilly and Company", "EQUITY", "PHARMACEUTICALS", "Healthcare", 10_000, 10.0, True),
    ("JPM", "JPMorgan Chase & Co.", "EQUITY", "MONEY_CENTER_BANK", "Financials", 10_000, 5.0, True),
    ("V", "Visa Inc.", "EQUITY", "PAYMENTS", "Financials", 10_000, 5.0, True),
    ("UNH", "UnitedHealth Group Inc.", "EQUITY", "MANAGED_CARE", "Healthcare", 10_000, 10.0, True),
    ("XOM", "Exxon Mobil Corporation", "EQUITY", "INTEGRATED_OIL", "Energy", 10_000, 2.5, True),
    ("MA", "Mastercard Inc.", "EQUITY", "PAYMENTS", "Financials", 10_000, 10.0, True),
    ("COST", "Costco Wholesale Corporation", "EQUITY", "WAREHOUSE_RETAIL", "Consumer Staples", 10_000, 10.0, True),
    ("HD", "The Home Depot Inc.", "EQUITY", "HOME_IMPROVEMENT_RETAIL", "Consumer Discretionary", 10_000, 5.0, True),
    ("PG", "Procter & Gamble Company", "EQUITY", "HOUSEHOLD_PRODUCTS", "Consumer Staples", 10_000, 2.5, True),
    ("JNJ", "Johnson & Johnson", "EQUITY", "PHARMACEUTICALS", "Healthcare", 10_000, 2.5, True),
    ("NFLX", "Netflix Inc.", "EQUITY", "STREAMING_MEDIA", "Communication Services", 10_000, 10.0, True),
    ("BAC", "Bank of America Corporation", "EQUITY", "MONEY_CENTER_BANK", "Financials", 10_000, 1.0, True),
    ("CRM", "Salesforce Inc.", "EQUITY", "SOFTWARE", "Information Technology", 10_000, 5.0, True),
    ("ORCL", "Oracle Corporation", "EQUITY", "SOFTWARE", "Information Technology", 10_000, 5.0, True),
    ("AMD", "Advanced Micro Devices Inc.", "EQUITY", "SEMICONDUCTORS", "Information Technology", 10_000, 2.5, True),
    ("ADBE", "Adobe Inc.", "EQUITY", "SOFTWARE", "Information Technology", 10_000, 10.0, True),
    ("CVX", "Chevron Corporation", "EQUITY", "INTEGRATED_OIL", "Energy", 10_000, 2.5, True),
    ("PEP", "PepsiCo Inc.", "EQUITY", "BEVERAGES", "Consumer Staples", 10_000, 2.5, True),
    ("KO", "The Coca-Cola Company", "EQUITY", "BEVERAGES", "Consumer Staples", 10_000, 1.0, True),
    ("TMO", "Thermo Fisher Scientific Inc.", "EQUITY", "LIFE_SCIENCE_TOOLS", "Healthcare", 10_000, 10.0, True),
    ("MRK", "Merck & Co. Inc.", "EQUITY", "PHARMACEUTICALS", "Healthcare", 10_000, 2.5, True),
    ("WMT", "Walmart Inc.", "EQUITY", "HYPERMARKET_RETAIL", "Consumer Staples", 10_000, 2.5, True),
    ("ABBV", "AbbVie Inc.", "EQUITY", "BIOTECHNOLOGY", "Healthcare", 10_000, 5.0, True),
    ("ACN", "Accenture plc", "EQUITY", "IT_SERVICES", "Information Technology", 10_000, 10.0, True),
    ("CSCO", "Cisco Systems Inc.", "EQUITY", "NETWORKING", "Information Technology", 10_000, 1.0, True),
    ("LIN", "Linde plc", "EQUITY", "INDUSTRIAL_GASES", "Materials", 10_000, 10.0, True),
    ("MCD", "McDonald's Corporation", "EQUITY", "RESTAURANTS", "Consumer Discretionary", 10_000, 5.0, True),
    ("ABT", "Abbott Laboratories", "EQUITY", "MEDICAL_DEVICES", "Healthcare", 10_000, 2.5, True),
    ("INTC", "Intel Corporation", "EQUITY", "SEMICONDUCTORS", "Information Technology", 10_000, 1.0, True),
    ("QCOM", "Qualcomm Incorporated", "EQUITY", "SEMICONDUCTORS", "Information Technology", 10_000, 5.0, True),
    ("TXN", "Texas Instruments Incorporated", "EQUITY", "SEMICONDUCTORS", "Information Technology", 10_000, 5.0, True),
    ("AMAT", "Applied Materials Inc.", "EQUITY", "SEMICONDUCTOR_EQUIPMENT", "Information Technology", 10_000, 5.0, True),
    ("MU", "Micron Technology Inc.", "EQUITY", "SEMICONDUCTORS", "Information Technology", 10_000, 2.5, True),
    ("NOW", "ServiceNow Inc.", "EQUITY", "SOFTWARE", "Information Technology", 10_000, 20.0, True),
    ("PLTR", "Palantir Technologies Inc.", "EQUITY", "SOFTWARE", "Information Technology", 10_000, 2.5, True),
    ("COIN", "Coinbase Global Inc.", "EQUITY", "CRYPTO_EXCHANGE", "Financials", 10_000, 5.0, True),
    ("MSTR", "MicroStrategy Incorporated", "EQUITY", "BITCOIN_TREASURY", "Information Technology", 10_000, 10.0, True),
    ("UBER", "Uber Technologies Inc.", "EQUITY", "RIDE_HAILING", "Industrials", 10_000, 2.5, True),
    ("SNOW", "Snowflake Inc.", "EQUITY", "SOFTWARE", "Information Technology", 10_000, 5.0, True),
    ("ARM", "Arm Holdings plc", "EQUITY", "SEMICONDUCTORS", "Information Technology", 10_000, 5.0, True),
)

#: Benchmark, sector, fixed-income and commodity ETFs: ``symbol, display_name,
#: sub_category, sector, strike_step, options``.
_US_ETFS: tuple[tuple[Any, ...], ...] = (
    ("SPY", "SPDR S&P 500 ETF Trust", "BROAD_MARKET_ETF", "Diversified", 5.0, True),
    ("QQQ", "Invesco QQQ Trust", "NASDAQ_100_ETF", "Diversified", 5.0, True),
    ("IWM", "iShares Russell 2000 ETF", "SMALL_CAP_ETF", "Diversified", 2.5, True),
    ("DIA", "SPDR Dow Jones Industrial Average ETF", "BLUE_CHIP_ETF", "Diversified", 5.0, True),
    ("VTI", "Vanguard Total Stock Market ETF", "TOTAL_MARKET_ETF", "Diversified", 5.0, True),
    ("SMH", "VanEck Semiconductor ETF", "SECTOR_ETF", "Information Technology", 5.0, True),
    ("XLK", "Technology Select Sector SPDR Fund", "SECTOR_ETF", "Information Technology", 2.5, True),
    ("XLF", "Financial Select Sector SPDR Fund", "SECTOR_ETF", "Financials", 1.0, True),
    ("XLE", "Energy Select Sector SPDR Fund", "SECTOR_ETF", "Energy", 1.0, True),
    ("XLV", "Health Care Select Sector SPDR Fund", "SECTOR_ETF", "Healthcare", 2.5, True),
    ("XLI", "Industrial Select Sector SPDR Fund", "SECTOR_ETF", "Industrials", 2.5, True),
    ("XLP", "Consumer Staples Select Sector SPDR Fund", "SECTOR_ETF", "Consumer Staples", 1.0, True),
    ("XLY", "Consumer Discretionary Select Sector SPDR Fund", "SECTOR_ETF", "Consumer Discretionary", 5.0, True),
    ("XLB", "Materials Select Sector SPDR Fund", "SECTOR_ETF", "Materials", 1.0, True),
    ("XLU", "Utilities Select Sector SPDR Fund", "SECTOR_ETF", "Utilities", 1.0, True),
    ("XLRE", "Real Estate Select Sector SPDR Fund", "SECTOR_ETF", "Real Estate", 1.0, True),
    ("TLT", "iShares 20+ Year Treasury Bond ETF", "FIXED_INCOME_ETF", "Fixed Income", 1.0, True),
    ("IEF", "iShares 7-10 Year Treasury Bond ETF", "FIXED_INCOME_ETF", "Fixed Income", 1.0, True),
    ("HYG", "iShares iBoxx High Yield Corporate Bond ETF", "FIXED_INCOME_ETF", "Fixed Income", 1.0, True),
    ("GLD", "SPDR Gold Shares", "COMMODITY_ETF", "Materials", 5.0, True),
    ("SLV", "iShares Silver Trust", "COMMODITY_ETF", "Materials", 1.0, True),
    ("USO", "United States Oil Fund", "COMMODITY_ETF", "Energy", 1.0, True),
    ("VIXY", "ProShares VIX Short-Term Futures ETF", "VOLATILITY_ETF", "Volatility", 1.0, False),
)

#: CME futures: ``symbol, display_name, sub_category, sector, tick_micros,
#: strike_step, margin_init, margin_maint, options``.
_US_FUTURES: tuple[tuple[Any, ...], ...] = (
    ("ES", "E-mini S&P 500 Futures", "EQUITY_INDEX_FUTURE", "Broad Market", 25_000, 25.0, 12.0, 8.0, True),
    ("NQ", "E-mini Nasdaq 100 Futures", "EQUITY_INDEX_FUTURE", "Broad Market", 25_000, 25.0, 14.0, 9.0, True),
    ("MES", "Micro E-mini S&P 500 Futures", "EQUITY_INDEX_FUTURE", "Broad Market", 25_000, 25.0, 12.0, 8.0, True),
    ("MNQ", "Micro E-mini Nasdaq 100 Futures", "EQUITY_INDEX_FUTURE", "Broad Market", 25_000, 25.0, 14.0, 9.0, True),
    ("CL", "WTI Crude Oil Futures", "ENERGY_FUTURE", "Energy", 1_000, 0.5, 15.0, 10.0, True),
    ("GC", "COMEX Gold Futures", "PRECIOUS_METALS_FUTURE", "Materials", 10_000, 10.0, 10.0, 6.5, True),
)

#: FX & CFD rows: ``symbol, display_name, asset_class, sub_category, sector,
#: currency, tick_micros, lot, freeze, strike_step, margin_init, margin_maint``.
_FX_ROWS: tuple[tuple[Any, ...], ...] = (
    # -- G10 majors ----------------------------------------------------------
    ("EURUSD", "Euro / US Dollar", "FX", "G10_MAJOR", "G10 Major", "USD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("GBPUSD", "British Pound / US Dollar", "FX", "G10_MAJOR", "G10 Major", "USD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("USDJPY", "US Dollar / Japanese Yen", "FX", "G10_MAJOR", "G10 Major", "JPY", 1_000, 100_000.0, 5_000_000.0, 0.01, 2.0, 1.0),
    ("USDCHF", "US Dollar / Swiss Franc", "FX", "G10_MAJOR", "G10 Major", "CHF", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("AUDUSD", "Australian Dollar / US Dollar", "FX", "G10_MAJOR", "G10 Major", "USD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("USDCAD", "US Dollar / Canadian Dollar", "FX", "G10_MAJOR", "G10 Major", "CAD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("NZDUSD", "New Zealand Dollar / US Dollar", "FX", "G10_MAJOR", "G10 Major", "USD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    # -- G10 crosses ---------------------------------------------------------
    ("EURGBP", "Euro / British Pound", "FX", "G10_CROSS", "G10 Cross", "GBP", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("EURJPY", "Euro / Japanese Yen", "FX", "G10_CROSS", "G10 Cross", "JPY", 1_000, 100_000.0, 5_000_000.0, 0.01, 2.0, 1.0),
    ("GBPJPY", "British Pound / Japanese Yen", "FX", "G10_CROSS", "G10 Cross", "JPY", 1_000, 100_000.0, 5_000_000.0, 0.01, 2.0, 1.0),
    ("AUDJPY", "Australian Dollar / Japanese Yen", "FX", "G10_CROSS", "G10 Cross", "JPY", 1_000, 100_000.0, 5_000_000.0, 0.01, 2.0, 1.0),
    ("CADJPY", "Canadian Dollar / Japanese Yen", "FX", "G10_CROSS", "G10 Cross", "JPY", 1_000, 100_000.0, 5_000_000.0, 0.01, 2.0, 1.0),
    ("CHFJPY", "Swiss Franc / Japanese Yen", "FX", "G10_CROSS", "G10 Cross", "JPY", 1_000, 100_000.0, 5_000_000.0, 0.01, 2.0, 1.0),
    ("NZDJPY", "New Zealand Dollar / Japanese Yen", "FX", "G10_CROSS", "G10 Cross", "JPY", 1_000, 100_000.0, 5_000_000.0, 0.01, 2.0, 1.0),
    ("EURAUD", "Euro / Australian Dollar", "FX", "G10_CROSS", "G10 Cross", "AUD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("EURCAD", "Euro / Canadian Dollar", "FX", "G10_CROSS", "G10 Cross", "CAD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("EURCHF", "Euro / Swiss Franc", "FX", "G10_CROSS", "G10 Cross", "CHF", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("EURNZD", "Euro / New Zealand Dollar", "FX", "G10_CROSS", "G10 Cross", "NZD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("GBPAUD", "British Pound / Australian Dollar", "FX", "G10_CROSS", "G10 Cross", "AUD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("GBPCAD", "British Pound / Canadian Dollar", "FX", "G10_CROSS", "G10 Cross", "CAD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("GBPCHF", "British Pound / Swiss Franc", "FX", "G10_CROSS", "G10 Cross", "CHF", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("GBPNZD", "British Pound / New Zealand Dollar", "FX", "G10_CROSS", "G10 Cross", "NZD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("AUDCAD", "Australian Dollar / Canadian Dollar", "FX", "G10_CROSS", "G10 Cross", "CAD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("AUDCHF", "Australian Dollar / Swiss Franc", "FX", "G10_CROSS", "G10 Cross", "CHF", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("AUDNZD", "Australian Dollar / New Zealand Dollar", "FX", "G10_CROSS", "G10 Cross", "NZD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("NZDCAD", "New Zealand Dollar / Canadian Dollar", "FX", "G10_CROSS", "G10 Cross", "CAD", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("NZDCHF", "New Zealand Dollar / Swiss Franc", "FX", "G10_CROSS", "G10 Cross", "CHF", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    ("CADCHF", "Canadian Dollar / Swiss Franc", "FX", "G10_CROSS", "G10 Cross", "CHF", 10, 100_000.0, 5_000_000.0, 0.005, 2.0, 1.0),
    # -- emerging-market crosses --------------------------------------------
    ("USDZAR", "US Dollar / South African Rand", "FX", "EMERGING", "Emerging FX", "ZAR", 10, 100_000.0, 5_000_000.0, 0.05, 4.0, 2.0),
    ("USDMXN", "US Dollar / Mexican Peso", "FX", "EMERGING", "Emerging FX", "MXN", 10, 100_000.0, 5_000_000.0, 0.02, 4.0, 2.0),
    ("USDSGD", "US Dollar / Singapore Dollar", "FX", "EMERGING", "Emerging FX", "SGD", 10, 100_000.0, 5_000_000.0, 0.005, 3.0, 1.5),
    ("USDTRY", "US Dollar / Turkish Lira", "FX", "EMERGING", "Emerging FX", "TRY", 10, 100_000.0, 5_000_000.0, 0.05, 6.0, 3.0),
    ("USDCNH", "US Dollar / Offshore Chinese Yuan", "FX", "EMERGING", "Emerging FX", "CNH", 10, 100_000.0, 5_000_000.0, 0.01, 3.0, 1.5),
    # -- precious metals -----------------------------------------------------
    ("XAUUSD", "Gold / US Dollar Spot", "METAL", "PRECIOUS_METALS", "Precious Metals", "USD", 10_000, 100.0, 1_000.0, 1.0, 5.0, 2.5),
    ("XAGUSD", "Silver / US Dollar Spot", "METAL", "PRECIOUS_METALS", "Precious Metals", "USD", 1_000, 5_000.0, 50_000.0, 0.05, 8.0, 4.0),
    ("XPTUSD", "Platinum / US Dollar Spot", "METAL", "PRECIOUS_METALS", "Precious Metals", "USD", 10_000, 100.0, 500.0, 1.0, 8.0, 4.0),
    ("XPDUSD", "Palladium / US Dollar Spot", "METAL", "PRECIOUS_METALS", "Precious Metals", "USD", 10_000, 100.0, 500.0, 1.0, 10.0, 5.0),
    # -- energy CFDs ---------------------------------------------------------
    ("USOIL", "WTI Crude Oil CFD", "ENERGY", "CRUDE_OIL", "Energy", "USD", 1_000, 1_000.0, 10_000.0, 0.5, 10.0, 5.0),
    ("UKOIL", "Brent Crude Oil CFD", "ENERGY", "CRUDE_OIL", "Energy", "USD", 1_000, 1_000.0, 10_000.0, 0.5, 10.0, 5.0),
)


def _build() -> InstrumentRegistry:
    rows: list[InstrumentSpec] = []

    # -- India: indices ------------------------------------------------------
    for symbol, name, sector, venue, lot, freeze, tick, step, options in _INDIA_INDICES:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                display_name=name,
                silo="INDIA_ZERODHA_DHAN",
                venue=venue,
                asset_class="INDEX_FUTURE",
                sub_category="VOLATILITY_INDEX" if symbol == "INDIAVIX" else "BROAD_OR_SECTOR_INDEX",
                sector=sector,
                currency="INR",
                lot_size=lot,
                tick_size_micros=tick,
                freeze_quantity=freeze,
                circuit_limit_pct=10.0,
                margin_initial_pct=100.0 if symbol == "INDIAVIX" else 12.0,
                margin_maintenance_pct=100.0 if symbol == "INDIAVIX" else 8.0,
                settlement_cycle="T+0",
                trading_hours_utc="03:45-10:00",
                option_strike_step=step,
                has_options=options,
                isin_or_id=f"{venue}:{symbol}",
            )
        )

    # -- India: NIFTY 50 cash equities --------------------------------------
    for symbol, name, sub_category, sector, lot, step in _INDIA_EQUITIES:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                display_name=name,
                silo="INDIA_ZERODHA_DHAN",
                venue="NSE",
                asset_class="EQUITY",
                sub_category=sub_category,
                sector=sector,
                currency="INR",
                lot_size=lot,
                tick_size_micros=50_000,  # ₹0.05
                freeze_quantity=lot * 10.0,
                circuit_limit_pct=20.0,
                margin_initial_pct=20.0,
                margin_maintenance_pct=12.5,
                settlement_cycle="T+1",
                trading_hours_utc="03:45-10:00",
                option_strike_step=step,
                has_options=True,
                isin_or_id=f"NSE:{symbol}",
            )
        )

    # -- India: MCX commodity futures ---------------------------------------
    for (
        symbol,
        name,
        sub_category,
        sector,
        lot,
        freeze,
        tick,
        step,
        options,
    ) in _INDIA_COMMODITIES:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                display_name=name,
                silo="INDIA_ZERODHA_DHAN",
                venue="MCX",
                asset_class="COMMODITY_FUTURE",
                sub_category=sub_category,
                sector=sector,
                currency="INR",
                lot_size=lot,
                tick_size_micros=tick,
                freeze_quantity=freeze,
                circuit_limit_pct=10.0,
                margin_initial_pct=12.0,
                margin_maintenance_pct=8.0,
                settlement_cycle="T+0",
                trading_hours_utc="03:30-23:30",
                option_strike_step=step,
                has_options=options,
                isin_or_id=f"MCX:{symbol}",
            )
        )

    # -- India: NSE currency futures ----------------------------------------
    for symbol, name, lot, freeze, tick, step in _INDIA_CURRENCY:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                display_name=name,
                silo="INDIA_ZERODHA_DHAN",
                venue="NSE",
                asset_class="CURRENCY_FUTURE",
                sub_category="CURRENCY_DERIVATIVE",
                sector="Currency",
                currency="INR",
                lot_size=lot,
                tick_size_micros=tick,
                freeze_quantity=freeze,
                circuit_limit_pct=10.0,
                margin_initial_pct=3.5,
                margin_maintenance_pct=2.0,
                settlement_cycle="T+0",
                trading_hours_utc="03:30-11:30",
                option_strike_step=step,
                has_options=True,
                isin_or_id=f"NSE:{symbol}",
            )
        )

    # -- Crypto: Binance USDT perpetuals ------------------------------------
    for symbol, name, narrative, tick, lot, freeze, options in _CRYPTO_ROWS:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                display_name=name,
                silo="CRYPTO_BINANCE",
                venue="BINANCE",
                asset_class="PERPETUAL",
                sub_category="USDT_M_PERPETUAL",
                sector=narrative,
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
                has_options=options,
                isin_or_id=f"BINANCE:{symbol}",
            )
        )

    # -- US: cash equities & ETFs -------------------------------------------
    for symbol, name, asset_class, sub_category, sector, tick, step, options in _US_EQUITIES:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                display_name=name,
                silo="US_IBKR",
                venue="IBKR",
                asset_class=asset_class,
                sub_category=sub_category,
                sector=sector,
                currency="USD",
                lot_size=1.0,
                tick_size_micros=tick,
                freeze_quantity=10_000.0,
                circuit_limit_pct=20.0,
                margin_initial_pct=25.0,
                margin_maintenance_pct=15.0,
                settlement_cycle="T+1",
                trading_hours_utc="13:30-20:00",
                option_strike_step=step,
                has_options=options,
                isin_or_id=f"US:{symbol}",
            )
        )
    for symbol, name, sub_category, sector, step, options in _US_ETFS:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                display_name=name,
                silo="US_IBKR",
                venue="IBKR",
                asset_class="ETF",
                sub_category=sub_category,
                sector=sector,
                currency="USD",
                lot_size=1.0,
                tick_size_micros=10_000,  # $0.01
                freeze_quantity=5_000.0,
                circuit_limit_pct=20.0,
                margin_initial_pct=25.0,
                margin_maintenance_pct=15.0,
                settlement_cycle="T+1",
                trading_hours_utc="13:30-20:00",
                option_strike_step=step,
                has_options=options,
                isin_or_id=f"US:{symbol}",
            )
        )

    # -- US: CME futures ----------------------------------------------------
    for symbol, name, sub_category, sector, tick, step, im, mm, options in _US_FUTURES:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                display_name=name,
                silo="US_IBKR",
                venue="CME",
                asset_class="FUTURE",
                sub_category=sub_category,
                sector=sector,
                currency="USD",
                lot_size=1.0,
                tick_size_micros=tick,
                freeze_quantity=2_000.0,
                circuit_limit_pct=0.0,
                margin_initial_pct=im,
                margin_maintenance_pct=mm,
                settlement_cycle="T+0",
                trading_hours_utc="22:00-21:00",
                option_strike_step=step,
                has_options=options,
                isin_or_id=f"CME:{symbol}",
            )
        )

    # -- FX, metals & energy CFDs -------------------------------------------
    for (
        symbol,
        name,
        asset_class,
        sub_category,
        sector,
        currency,
        tick,
        lot,
        freeze,
        step,
        im,
        mm,
    ) in _FX_ROWS:
        rows.append(
            InstrumentSpec(
                symbol=symbol,
                display_name=name,
                silo="FX_MT5",
                venue="MT5",
                asset_class=asset_class,
                sub_category=sub_category,
                sector=sector,
                currency=currency,
                lot_size=lot,
                tick_size_micros=tick,
                freeze_quantity=freeze,
                circuit_limit_pct=0.0,
                margin_initial_pct=im,
                margin_maintenance_pct=mm,
                settlement_cycle="T+1",
                trading_hours_utc="22:00-21:00",
                option_strike_step=step,
                # V1.8: spot FX pairs have no listed option chain.  ``EURUSD`` and
                # every other MT5 row must answer ``422 INSTRUMENT_NOT_OPTIONABLE``
                # from the universal options endpoint instead of silently
                # synthesising a chain from a tuned proxy.
                has_options=False,
                isin_or_id=f"MT5:{symbol}",
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
