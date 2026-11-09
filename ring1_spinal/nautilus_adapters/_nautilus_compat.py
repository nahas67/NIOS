"""Nautilus runtime compatibility layer for the Ring 1 venue adapters.

The harvested adapters under :mod:`ring1_spinal.nautilus_adapters` were copied
from ``nautilus_trader`` and therefore begin with::

    from nautilus_trader._fixup import fixup_module_names
    from nautilus_trader._libnautilus.<venue> import *

Neither ``nautilus_trader._fixup`` nor ``nautilus_trader._libnautilus`` exists in
nautilus_trader 1.231.0, and on hardened Windows hosts the C extensions
(``book``, ``identifiers``, ``base``) are additionally blocked by Application
Control policy.  Importing the upstream module therefore fails for reasons that
have nothing to do with NIOS.

This module gives Ring 1 a deterministic, air-gap-safe adapter surface that:

* imports cleanly whether or not ``nautilus_trader`` is usable,
* prefers the real upstream classes when the runtime is healthy,
* falls back to structurally equivalent shims when it is not,
* and always exposes ``format_order_payload`` so Ring 0 can render an exact
  venue-native order body for paper/shadow dispatch.

Ring 1 never signs, never settles and never holds credentials: the payloads
produced here are *unsigned request bodies*.  Only :mod:`ring0_vault` may sign
them or hand them to a live session.
"""

from __future__ import annotations

import importlib
import importlib.util
import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import Enum
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

__all__ = [
    "ADAPTER_VENUES",
    "AdapterSpec",
    "Environment",
    "NautilusRuntime",
    "Shim",
    "Venue",
    "adapter_exports",
    "adapter_spec",
    "format_order_payload",
    "fixup_module_names",
    "nautilus_runtime",
    "payload_flags",
    "venue_spec",
]


# ---------------------------------------------------------------------------
# Runtime probing
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class NautilusRuntime:
    """Truthful description of the installed nautilus runtime."""

    available: bool
    version: str
    reason: str
    module_path: str = ""

    @property
    def degraded(self) -> bool:
        return not self.available

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "version": self.version,
            "reason": self.reason,
            "module_path": self.module_path,
        }


_RUNTIME_CACHE: NautilusRuntime | None = None


def nautilus_runtime(*, refresh: bool = False) -> NautilusRuntime:
    """Probe ``nautilus_trader`` once and cache the outcome.

    The probe imports ``nautilus_trader.model`` because every execution client
    depends on it; the top-level package alone can import while the compiled
    extensions are blocked.
    """

    global _RUNTIME_CACHE
    if _RUNTIME_CACHE is not None and not refresh:
        return _RUNTIME_CACHE

    spec = importlib.util.find_spec("nautilus_trader")
    if spec is None:
        _RUNTIME_CACHE = NautilusRuntime(
            available=False,
            version="",
            reason="nautilus_trader is not installed",
        )
        return _RUNTIME_CACHE

    module_path = ""
    try:
        module = importlib.import_module("nautilus_trader")
        module_path = str(getattr(module, "__file__", "") or "")
        version = str(getattr(module, "__version__", "") or "")
        importlib.import_module("nautilus_trader.model")
    except Exception as exc:  # pragma: no cover - host dependent
        _RUNTIME_CACHE = NautilusRuntime(
            available=False,
            version=version if "version" in locals() else "",
            reason=f"{type(exc).__name__}: {exc}",
            module_path=module_path,
        )
        return _RUNTIME_CACHE

    _RUNTIME_CACHE = NautilusRuntime(
        available=True,
        version=version,
        reason="",
        module_path=module_path,
    )
    return _RUNTIME_CACHE


def fixup_module_names(namespace: MutableMapping[str, Any], name: str = "") -> None:
    """No-op replacement for ``nautilus_trader._fixup.fixup_module_names``.

    The upstream helper rewrites ``__module__`` on every public symbol so that
    ``repr`` points at the adapter package.  Our shims already set ``__module__``
    explicitly, and the real upstream classes keep their own module, so there is
    nothing to rewrite.
    """

    return None


# ---------------------------------------------------------------------------
# Structural shims
# ---------------------------------------------------------------------------


class Venue(str):
    """String-compatible venue identifier (``Venue("BINANCE")``)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Venue('{str(self)}')"

    @property
    def value(self) -> str:
        return str(self)


class Environment(str, Enum):
    """Execution environment for a venue session."""

    LIVE = "LIVE"
    SANDBOX = "SANDBOX"
    PAPER = "PAPER"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class Shim:
    """Permissive stand-in for an upstream nautilus symbol.

    Instances are truthy, accept any constructor signature, record the values
    they were built with and expose every attribute as ``None`` rather than
    raising.  This keeps configuration objects renderable even on a host where
    the compiled runtime is blocked.
    """

    venue: str = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = dict(kwargs)
        for key, value in kwargs.items():
            try:
                setattr(self, key, value)
            except Exception:  # pragma: no cover - defensive
                pass

    def __getattr__(self, item: str) -> Any:
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        return None

    def __bool__(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in vars(self).items() if not k.startswith("_")}
        data.setdefault("venue", self.venue)
        return data

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} venue={self.venue or '?'}>"


def _make_shim(name: str, venue: str) -> type[Shim]:
    return type(name, (Shim,), {"__module__": f"ring1_spinal.nautilus_adapters.{venue}", "venue": venue})


def _str_enum(name: str, members: Mapping[str, str], module: str = __name__) -> type[Enum]:
    """Build a ``str``-mixin Enum through the functional API.

    The three-argument ``type()`` form cannot build an Enum: ``EnumMeta``
    requires a ``__prepare__`` namespace.  ``Enum(name, members, type=str)`` is
    the supported path.
    """

    return Enum(name, dict(members), type=str, module=module)


# ---------------------------------------------------------------------------
# Adapter specification
# ---------------------------------------------------------------------------

ADAPTER_VENUES: tuple[str, ...] = (
    "binance",
    "okx",
    "bybit",
    "interactive_brokers",
    "alpaca",
)

_SILO_BY_VENUE: dict[str, str] = {
    "binance": "CRYPTO_BINANCE",
    "okx": "CRYPTO_BINANCE",
    "bybit": "CRYPTO_BINANCE",
    "interactive_brokers": "US_IBKR",
    "alpaca": "US_IBKR",
}


@dataclass(slots=True, frozen=True)
class AdapterSpec:
    """Declarative contract for one Ring 1 venue adapter."""

    venue: str
    venue_code: str
    silo: str
    module: str
    settlement_cycle: str
    settlement_days: int
    supports_post_only: bool
    supports_fractional: bool
    price_tick: float
    quantity_step: float
    quote_currency: str
    #: Order types accepted by the venue, in NIOS vocabulary.
    order_types: tuple[str, ...] = ("LIMIT", "MARKET")
    #: Extra fields that must be present in a venue-native payload.
    required_fields: tuple[str, ...] = ()
    notes: str = ""

    @property
    def is_crypto(self) -> bool:
        return self.venue in {"binance", "okx", "bybit"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "venue_code": self.venue_code,
            "silo": self.silo,
            "module": self.module,
            "settlement_cycle": self.settlement_cycle,
            "settlement_days": self.settlement_days,
            "supports_post_only": self.supports_post_only,
            "supports_fractional": self.supports_fractional,
            "price_tick": self.price_tick,
            "quantity_step": self.quantity_step,
            "quote_currency": self.quote_currency,
            "order_types": list(self.order_types),
            "required_fields": list(self.required_fields),
            "notes": self.notes,
        }


_SPECS: dict[str, AdapterSpec] = {
    "binance": AdapterSpec(
        venue="binance",
        venue_code="BINANCE",
        silo="CRYPTO_BINANCE",
        module="ring1_spinal.nautilus_adapters.binance",
        settlement_cycle="T+0",
        settlement_days=0,
        supports_post_only=True,
        supports_fractional=True,
        price_tick=0.01,
        quantity_step=1e-8,
        quote_currency="USDT",
        order_types=("LIMIT", "MARKET", "STOP_LIMIT"),
        required_fields=("symbol", "side", "type", "quantity"),
        notes="24/7 spot; LIMIT_MAKER enforces post-only at the matching engine.",
    ),
    "okx": AdapterSpec(
        venue="okx",
        venue_code="OKX",
        silo="CRYPTO_BINANCE",
        module="ring1_spinal.nautilus_adapters.okx",
        settlement_cycle="T+0",
        settlement_days=0,
        supports_post_only=True,
        supports_fractional=True,
        price_tick=0.01,
        quantity_step=1e-8,
        quote_currency="USDT",
        order_types=("LIMIT", "MARKET"),
        required_fields=("instId", "side", "ordType", "sz"),
        notes="tdMode=cash for spot; post_only rejects taker fills.",
    ),
    "bybit": AdapterSpec(
        venue="bybit",
        venue_code="BYBIT",
        silo="CRYPTO_BINANCE",
        module="ring1_spinal.nautilus_adapters.bybit",
        settlement_cycle="T+0",
        settlement_days=0,
        supports_post_only=True,
        supports_fractional=True,
        price_tick=0.01,
        quantity_step=1e-8,
        quote_currency="USDT",
        order_types=("LIMIT", "MARKET"),
        required_fields=("category", "symbol", "side", "orderType", "qty"),
        notes="category=spot; timeInForce=PostOnly for maker pegs.",
    ),
    "interactive_brokers": AdapterSpec(
        venue="interactive_brokers",
        venue_code="IBKR",
        silo="US_IBKR",
        module="ring1_spinal.nautilus_adapters.interactive_brokers",
        settlement_cycle="T+1",
        settlement_days=1,
        supports_post_only=False,
        supports_fractional=True,
        price_tick=0.01,
        quantity_step=1.0,
        quote_currency="USD",
        order_types=("LIMIT", "MARKET", "STOP", "STOP_LIMIT"),
        required_fields=("conid", "side", "orderType", "quantity"),
        notes="T+1 settlement, Reg-T margin guards, SEC EDGAR PIT gating.",
    ),
    "alpaca": AdapterSpec(
        venue="alpaca",
        venue_code="ALPACA",
        silo="US_IBKR",
        module="ring1_spinal.nautilus_adapters.alpaca",
        settlement_cycle="T+1",
        settlement_days=1,
        supports_post_only=True,
        supports_fractional=True,
        price_tick=0.01,
        quantity_step=1e-6,
        quote_currency="USD",
        order_types=("LIMIT", "MARKET"),
        required_fields=("symbol", "qty", "side", "type", "time_in_force"),
        notes="T+1 settlement, Reg-T margin guards, sub-penny maker-peg formatting.",
    ),
}


def adapter_spec(venue: str) -> AdapterSpec:
    """Return the :class:`AdapterSpec` for ``venue``."""

    key = str(venue).strip().lower()
    try:
        return _SPECS[key]
    except KeyError as exc:
        raise KeyError(
            f"unknown Ring 1 venue {venue!r}; known venues: {', '.join(ADAPTER_VENUES)}"
        ) from exc


venue_spec = adapter_spec


# ---------------------------------------------------------------------------
# Numeric formatting helpers
# ---------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def _round_to_step(value: Any, step: float, *, direction: str = "nearest") -> Decimal:
    """Round ``value`` to a multiple of ``step``.

    ``direction`` is ``"nearest"``, ``"down"`` or ``"up"``.  Quantities always
    round down (never over-fill); prices round by maker side.
    """

    amount = _decimal(value)
    if step <= 0:
        return amount
    granularity = _decimal(step)
    units = amount / granularity
    if direction == "down":
        snapped = units.to_integral_value(rounding=ROUND_DOWN)
    elif direction == "up":
        snapped = units.to_integral_value(rounding=ROUND_UP)
    else:
        snapped = units.to_integral_value()
    return snapped * granularity


def _fmt_decimal(value: Decimal, places: int | None = None) -> str:
    """Render a Decimal as a plain string without scientific notation."""

    if places is None:
        text = format(value.normalize(), "f")
        return text
    quant = Decimal(1).scaleb(-places)
    text = format(value.quantize(quant), "f")
    return text


def _decimals_for_step(step: float) -> int:
    if step <= 0:
        return 0
    text = format(_decimal(step).normalize(), "f")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _side_upper(side: Any) -> str:
    text = str(side or "").strip().upper()
    if text in {"BUY", "B", "LONG"}:
        return "BUY"
    if text in {"SELL", "S", "SHORT"}:
        return "SELL"
    raise ValueError(f"unsupported order side {side!r}; expected BUY or SELL")


def _side_lower(side: Any) -> str:
    return _side_upper(side).lower()


def _side_title(side: Any) -> str:
    return _side_upper(side).capitalize()


def _symbol_raw(order: Mapping[str, Any]) -> str:
    symbol = str(order.get("symbol") or order.get("ticker") or "").strip()
    if not symbol:
        raise ValueError("order is missing 'symbol'")
    return symbol


def _compact_symbol(symbol: str) -> str:
    """``BTC-USDT`` / ``BTC/USDT`` / ``BTC_USDT`` -> ``BTCUSDT``."""

    return (
        symbol.upper()
        .replace("-", "")
        .replace("/", "")
        .replace("_", "")
        .replace(" ", "")
    )


def _dashed_symbol(symbol: str) -> str:
    """``BTCUSDT`` -> ``BTC-USDT`` using the venue quote-currency suffix."""

    compact = _compact_symbol(symbol)
    for quote in ("USDT", "USDC", "USD", "EUR", "BTC", "ETH"):
        if compact.endswith(quote) and len(compact) > len(quote):
            return f"{compact[: -len(quote)]}-{quote}"
    if "-" in symbol:
        return symbol.upper()
    return compact


def _client_order_id(order: Mapping[str, Any], venue: str) -> str:
    raw = order.get("client_order_id") or order.get("clientOrderId") or order.get("cid")
    if raw:
        return str(raw)
    proposal_id = str(order.get("proposal_id") or "").strip()
    if proposal_id:
        return f"nios-{venue[:3]}-{proposal_id[:24]}"
    return ""


def _quantity(order: Mapping[str, Any]) -> Decimal:
    for key in ("quantity", "qty", "sz"):
        if order.get(key) not in (None, ""):
            return _decimal(order[key])
    raise ValueError("order is missing 'quantity'")


def _price(order: Mapping[str, Any], *, required: bool) -> Decimal | None:
    for key in ("price", "limit_price", "limitPrice", "px"):
        value = order.get(key)
        if value not in (None, ""):
            return _decimal(value)
    if required:
        raise ValueError("a LIMIT order requires 'price' / 'limit_price'")
    return None


def _is_post_only(order: Mapping[str, Any]) -> bool:
    if "post_only" in order:
        return bool(order["post_only"])
    if "postOnly" in order:
        return bool(order["postOnly"])
    style = str(order.get("execution_style") or order.get("liquidity") or "").lower()
    if style in {"maker", "post_only", "post-only", "passive"}:
        return True
    if style in {"taker", "aggressive"}:
        return False
    # NIOS default: the Smart OMS pegs as a maker unless told otherwise.
    return True


def _order_type(order: Mapping[str, Any]) -> str:
    raw = str(order.get("order_type") or order.get("type") or "LIMIT").strip().upper()
    aliases = {
        "LMT": "LIMIT",
        "MKT": "MARKET",
        "STP": "STOP",
        "STP_LIMIT": "STOP_LIMIT",
        "STOPLIMIT": "STOP_LIMIT",
        "SL": "STOP_LIMIT",
        "SL-M": "STOP_MARKET",
        "SLM": "STOP_MARKET",
    }
    return aliases.get(raw, raw)


# ---------------------------------------------------------------------------
# Venue-native payload formatters
# ---------------------------------------------------------------------------


def _binance_payload(order: Mapping[str, Any]) -> dict[str, Any]:
    spec = _SPECS["binance"]
    side = _side_upper(order.get("side"))
    order_type = _order_type(order)
    post_only = _is_post_only(order)
    qty = _round_to_step(_quantity(order), spec.quantity_step, direction="down")
    price = _price(order, required=order_type != "MARKET")
    px = _round_to_step(price, spec.price_tick, direction="down" if side == "BUY" else "up") if price is not None else None

    if order_type == "MARKET":
        native_type = "MARKET"
    elif post_only:
        native_type = "LIMIT_MAKER"
    else:
        native_type = "LIMIT"

    payload: dict[str, Any] = {
        "symbol": _compact_symbol(_symbol_raw(order)),
        "side": side,
        "type": native_type,
        "quantity": _fmt_decimal(qty, _decimals_for_step(spec.quantity_step)),
        "newOrderRespType": "ACK",
    }
    if native_type != "MARKET":
        payload["timeInForce"] = str(order.get("time_in_force") or "GTC").upper()
        payload["price"] = _fmt_decimal(px, _decimals_for_step(spec.price_tick))
    if native_type == "LIMIT_MAKER":
        # Binance rejects timeInForce on LIMIT_MAKER.
        payload.pop("timeInForce", None)
    cid = _client_order_id(order, "binance")
    if cid:
        payload["newClientOrderId"] = cid[:36]
    if order.get("reduce_only"):
        payload["reduceOnly"] = True
    return payload


def _okx_payload(order: Mapping[str, Any]) -> dict[str, Any]:
    spec = _SPECS["okx"]
    side = _side_lower(order.get("side"))
    order_type = _order_type(order)
    post_only = _is_post_only(order)
    qty = _round_to_step(_quantity(order), spec.quantity_step, direction="down")
    price = _price(order, required=order_type != "MARKET")

    if order_type == "MARKET":
        native_type = "market"
    elif post_only:
        native_type = "post_only"
    else:
        native_type = "limit"

    payload: dict[str, Any] = {
        "instId": _dashed_symbol(_symbol_raw(order)),
        "tdMode": str(order.get("td_mode") or "cash").lower(),
        "side": side,
        "ordType": native_type,
        "sz": _fmt_decimal(qty, _decimals_for_step(spec.quantity_step)),
    }
    if native_type != "market" and price is not None:
        payload["px"] = _fmt_decimal(
            _round_to_step(price, spec.price_tick, direction="down" if side == "buy" else "up"),
            _decimals_for_step(spec.price_tick),
        )
    cid = _client_order_id(order, "okx")
    if cid:
        payload["clOrdId"] = cid[:32]
    if order.get("reduce_only"):
        payload["reduceOnly"] = True
    return payload


def _bybit_payload(order: Mapping[str, Any]) -> dict[str, Any]:
    spec = _SPECS["bybit"]
    side = _side_title(order.get("side"))
    order_type = _order_type(order)
    post_only = _is_post_only(order)
    qty = _round_to_step(_quantity(order), spec.quantity_step, direction="down")
    price = _price(order, required=order_type != "MARKET")

    payload: dict[str, Any] = {
        "category": str(order.get("category") or "spot").lower(),
        "symbol": _compact_symbol(_symbol_raw(order)),
        "side": side,
        "orderType": "Limit" if order_type != "MARKET" else "Market",
        "qty": _fmt_decimal(qty, _decimals_for_step(spec.quantity_step)),
    }
    if order_type != "MARKET" and price is not None:
        payload["price"] = _fmt_decimal(
            _round_to_step(price, spec.price_tick, direction="down" if side == "Buy" else "up"),
            _decimals_for_step(spec.price_tick),
        )
        payload["timeInForce"] = "PostOnly" if post_only else str(order.get("time_in_force") or "GTC").upper()
    cid = _client_order_id(order, "bybit")
    if cid:
        payload["orderLinkId"] = cid[:36]
    if order.get("reduce_only"):
        payload["reduceOnly"] = True
    return payload


def _ibkr_payload(order: Mapping[str, Any]) -> dict[str, Any]:
    spec = _SPECS["interactive_brokers"]
    side = _side_upper(order.get("side"))
    order_type = _order_type(order)
    qty = _round_to_step(_quantity(order), spec.quantity_step, direction="down")
    price = _price(order, required=order_type in {"LIMIT", "STOP_LIMIT"})

    native_type = {
        "LIMIT": "LMT",
        "MARKET": "MKT",
        "STOP": "STP",
        "STOP_LIMIT": "STP LMT",
        "STOP_MARKET": "STP",
    }.get(order_type, "LMT")

    conid = order.get("conid")
    if conid in (None, ""):
        # Deterministic paper identifier: IBKR conids are numeric and stable.
        conid = abs(hash(_compact_symbol(_symbol_raw(order)))) % 10_000_000 + 1

    payload: dict[str, Any] = {
        "conid": int(conid),
        "secType": str(order.get("sec_type") or "STK").upper(),
        "orderType": native_type,
        "side": side,
        "quantity": float(qty),
        "tif": str(order.get("time_in_force") or "DAY").upper(),
        "outsideRth": bool(order.get("outside_rth", False)),
        "transmit": True,
    }
    if price is not None:
        payload["price"] = float(_round_to_step(price, spec.price_tick))
    if order.get("account"):
        payload["acctId"] = str(order["account"])
    cid = _client_order_id(order, "interactive_brokers")
    if cid:
        payload["cOID"] = cid[:40]
    return payload


ALPACA_SUB_PENNY_THRESHOLD = 1.0
ALPACA_MAIN_TICK = 0.01
ALPACA_SUB_PENNY_TICK = 0.0001


def alpaca_tick(price: Any) -> float:
    """Return the Alpaca price increment for ``price``.

    Alpaca (and the SEC sub-penny rule) allow $0.0001 increments only below
    $1.00; at or above $1.00 the minimum increment is $0.01.
    """

    return ALPACA_SUB_PENNY_TICK if abs(float(price)) < ALPACA_SUB_PENNY_THRESHOLD else ALPACA_MAIN_TICK


def sub_penny_maker_peg(price: Any, side: Any) -> float:
    """Snap ``price`` to a legal passive tick.

    Buy maker pegs round **down** and sell maker pegs round **up**, so the
    resulting order can never cross the spread and pay taker fees.
    """

    value = float(price)
    tick = alpaca_tick(value)
    snapped = _round_to_step(value, tick, direction="down" if _side_upper(side) == "BUY" else "up")
    places = 4 if tick == ALPACA_SUB_PENNY_TICK else 2
    return float(_fmt_decimal(snapped, places))


def _alpaca_payload(order: Mapping[str, Any]) -> dict[str, Any]:
    spec = _SPECS["alpaca"]
    side = _side_lower(order.get("side"))
    order_type = _order_type(order)
    qty = _round_to_step(_quantity(order), spec.quantity_step, direction="down")
    price = _price(order, required=order_type != "MARKET")

    payload: dict[str, Any] = {
        "symbol": _symbol_raw(order).upper().replace(".", ""),
        "qty": _fmt_decimal(qty, 6).rstrip("0").rstrip(".") or "0",
        "side": side,
        "type": "limit" if order_type != "MARKET" else "market",
        "time_in_force": str(order.get("time_in_force") or "day").lower(),
        "extended_hours": bool(order.get("extended_hours", False)),
    }
    if price is not None:
        pegged = sub_penny_maker_peg(price, side) if _is_post_only(order) else float(
            _round_to_step(price, alpaca_tick(price))
        )
        payload["limit_price"] = _fmt_decimal(_decimal(pegged), 4 if alpaca_tick(pegged) < ALPACA_MAIN_TICK else 2)
    if order.get("notional") not in (None, ""):
        payload["notional"] = _fmt_decimal(_decimal(order["notional"]), 2)
        payload.pop("qty", None)
    cid = _client_order_id(order, "alpaca")
    if cid:
        payload["client_order_id"] = cid[:48]
    if order.get("order_class"):
        payload["order_class"] = str(order["order_class"]).lower()
    return payload


_FORMATTERS = {
    "binance": _binance_payload,
    "okx": _okx_payload,
    "bybit": _bybit_payload,
    "interactive_brokers": _ibkr_payload,
    "alpaca": _alpaca_payload,
}


def format_order_payload(venue: str, order: Mapping[str, Any]) -> dict[str, Any]:
    """Render an exact venue-native order body for ``venue``.

    The result is JSON-serializable, contains only venue-recognised field names
    and is *unsigned*.  Ring 0 signs the body; Ring 1 never does.
    """

    spec = adapter_spec(venue)
    try:
        formatter = _FORMATTERS[spec.venue]
    except KeyError as exc:  # pragma: no cover - guarded by adapter_spec
        raise KeyError(f"no payload formatter for venue {venue!r}") from exc
    payload = formatter(order)
    missing = [name for name in spec.required_fields if name not in payload]
    if missing:
        raise ValueError(
            f"{spec.venue_code} payload missing required fields: {', '.join(missing)}"
        )
    return payload


def payload_flags(venue: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Summarise the risk-relevant flags encoded in a venue payload."""

    spec = adapter_spec(venue)
    post_only = False
    product = ""
    if spec.venue == "binance":
        post_only = payload.get("type") == "LIMIT_MAKER"
    elif spec.venue == "okx":
        post_only = payload.get("ordType") == "post_only"
    elif spec.venue == "bybit":
        post_only = payload.get("timeInForce") == "PostOnly"
    elif spec.venue == "alpaca":
        post_only = True  # limit + maker peg by construction
    product = str(payload.get("product") or payload.get("productType") or "")
    return {
        "venue": spec.venue,
        "venue_code": spec.venue_code,
        "silo": spec.silo,
        "post_only": post_only,
        "product": product,
        "settlement_cycle": spec.settlement_cycle,
        "settlement_days": spec.settlement_days,
        "price_tick": spec.price_tick,
        "quote_currency": spec.quote_currency,
    }


# ---------------------------------------------------------------------------
# Export assembly
# ---------------------------------------------------------------------------


def _bound_formatter(venue: str):
    """Return a ``format_order_payload`` bound to ``venue``.

    Accepts ``(order)``, ``(venue, order)`` and ``(order=...)`` so callers can
    use either the module-level helper or the compat-level two-argument form.
    """

    def _format(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if len(args) >= 2:
            return format_order_payload(str(args[0]), args[1])
        if args:
            return format_order_payload(venue, args[0])
        order = kwargs.get("order")
        if order is None:
            raise TypeError("format_order_payload() requires an order mapping")
        return format_order_payload(venue, order)

    _format.__name__ = "format_order_payload"
    _format.__qualname__ = f"{venue}.format_order_payload"
    return _format


def _base_exports(venue: str) -> dict[str, Any]:
    spec = _SPECS[venue]
    code = spec.venue_code
    prefix = {
        "binance": "Binance",
        "okx": "OKX",
        "bybit": "Bybit",
        "interactive_brokers": "InteractiveBrokers",
        "alpaca": "Alpaca",
    }[venue]

    exports: dict[str, Any] = {
        "VENUE": code,
        "VENUE_CODE": code,
        "VENUE_SPEC": spec,
        "ADAPTER_SPEC": spec,
        "format_order_payload": _bound_formatter(venue),
        "payload_flags": lambda payload, _v=venue: payload_flags(_v, payload),
        f"{code}_VENUE": Venue(code),
        f"{code}_CLIENT_ID": code,
    }

    exports[f"{prefix}Environment"] = Environment
    exports[f"{prefix}InstrumentProviderConfig"] = type(
        f"{prefix}InstrumentProviderConfig",
        (Shim,),
        {"__module__": spec.module, "venue": venue},
    )
    exports[f"{prefix}DataClientConfig"] = type(
        f"{prefix}DataClientConfig", (Shim,), {"__module__": spec.module, "venue": venue}
    )
    exports[f"{prefix}ExecutionClientConfig"] = type(
        f"{prefix}ExecutionClientConfig", (Shim,), {"__module__": spec.module, "venue": venue}
    )
    exports[f"{prefix}DataClientFactory"] = type(
        f"{prefix}DataClientFactory", (Shim,), {"__module__": spec.module, "venue": venue}
    )
    exports[f"{prefix}ExecutionClientFactory"] = type(
        f"{prefix}ExecutionClientFactory", (Shim,), {"__module__": spec.module, "venue": venue}
    )
    return exports


# Names whose shim class carries a more specific base than the generic Shim.
_EXTRA_EXPORTS: dict[str, dict[str, Any]] = {
    "binance": {
        "BinanceMarginType": _str_enum("BinanceMarginType", {"ISOLATED": "ISOLATED", "CROSSED": "CROSSED"}),
        "BinancePositionSide": _str_enum("BinancePositionSide", {"BOTH": "BOTH", "LONG": "LONG", "SHORT": "SHORT"}),
        "BinanceProductType": _str_enum(
            "BinanceProductType",
            {"SPOT": "SPOT", "MARGIN": "MARGIN", "USDT_FUTURES": "USDT_FUTURES", "COIN_FUTURES": "COIN_FUTURES"},
        ),
        "BinanceSpotMarketDataMode": _str_enum("BinanceSpotMarketDataMode", {"L1": "L1", "L2": "L2"}),
        "load_binance_instruments": lambda config=None, *a, **k: [],
        "load_binance_order_book_deltas": lambda *a, **k: [],
        "get_binance_arrow_schema_map": lambda *a, **k: {},
        "decode_binance_spot_client_order_id": lambda value: str(value),
        "decode_binance_futures_client_order_id": lambda value: str(value),
    },
    "okx": {},
    "bybit": {
        "BybitProductType": _str_enum(
            "BybitProductType", {"SPOT": "SPOT", "LINEAR": "LINEAR", "INVERSE": "INVERSE", "OPTION": "OPTION"}
        ),
        "BybitPositionIdx": _str_enum("BybitPositionIdx", {"ONE_WAY": "0", "HEDGE_BUY": "1", "HEDGE_SELL": "2"}),
        "BybitPositionMode": _str_enum(
            "BybitPositionMode", {"ONE_WAY": "MergedSingle", "HEDGE": "BothSide"}
        ),
    },
    "interactive_brokers": {
        "TradingMode": _str_enum("TradingMode", {"PAPER": "paper", "LIVE": "live"}),
        "MarketDataType": _str_enum(
            "MarketDataType", {"REALTIME": "REALTIME", "DELAYED": "DELAYED", "FROZEN": "FROZEN"}
        ),
        "IbOrderType": _str_enum(
            "IbOrderType", {"LIMIT": "LMT", "MARKET": "MKT", "STOP": "STP", "STOP_LIMIT": "STP LMT"}
        ),
        "IbTimeInForce": _str_enum(
            "IbTimeInForce", {"DAY": "DAY", "GTC": "GTC", "IOC": "IOC", "FOK": "FOK"}
        ),
    },
    "alpaca": {
        "ALPACA_PAPER_BASE_URL": "https://paper-api.alpaca.markets",
        "ALPACA_LIVE_BASE_URL": "https://api.alpaca.markets",
        "ALPACA_DATA_BASE_URL": "https://data.alpaca.markets",
        "alpaca_tick": alpaca_tick,
        "sub_penny_maker_peg": sub_penny_maker_peg,
    },
}


def adapter_exports(venue: str, names: Sequence[str] | None = None) -> dict[str, Any]:
    """Build the module namespace for one adapter package.

    ``names`` is the adapter's public ``__all__``; any name without an explicit
    shim receives a distinct placeholder class so ``__all__`` stays honest.
    """

    spec = adapter_spec(venue)
    exports = _base_exports(spec.venue)
    exports.update(_EXTRA_EXPORTS.get(spec.venue, {}))

    wanted: Iterable[str] = names if names is not None else ()
    for name in wanted:
        if name in exports:
            continue
        exports[name] = _make_shim(name, spec.venue)
    return exports
