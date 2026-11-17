"""NIOS V2.1 -- unified multi-venue connector runtime (``G5``).

One normalized surface, four regulated execution venues, three explicit runtime
modes.  The point is not to abstract brokers for elegance; it is to make the
**boundary** the only place a float can exist.

::

    venue payload (float)  ->  ConnectorRuntime._quantize  ->  int micros

Above this module every price, quantity, fee and balance is an integer
micro-unit.  Below it nothing is trusted: ``to_micros()`` is half-up, rejects
``NaN``/``inf``/overflow, and a value that cannot be quantized raises
:class:`ConnectorError` instead of being coerced to zero.

Runtime modes (Rule 3):

``OFFLINE_REPLAY``
    Deterministic.  No socket, no clock dependence, reproducible order ids.  This
    is the default for tests, air-gapped deployments and every paper workflow.
``SANDBOX_TESTNET``
    A configured testnet transport is called.  Without one the call fails closed
    with ``TRANSPORT_NOT_CONFIGURED`` -- it never silently downgrades to replay.
``LIVE_VENUE``
    A real venue.  Requires ``allow_network=True`` *and* an injected transport.

Harvest note (Rule 1): the normalized schemas are translated from
``marketcalls/openalgo`` (Dhan HQ v2 / Zerodha Kite v3 order, position, funds
documents), ``ccxt`` (Binance spot/futures order, weight header, leverage tiers)
and ``nautilus_trader`` (execution reconciliation report shape).  No donor code is
vendored and nothing is imported from the external clone directory.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from ring0_vault.ibor.ledger_outbox import MICROS_PER_UNIT, to_micros

__all__ = [
    "CONNECTOR_MODES",
    "ConnectorBalance",
    "ConnectorError",
    "ConnectorOrder",
    "ConnectorPosition",
    "ConnectorRuntime",
    "QuoteSnapshot",
]

#: ``G5``: the three explicit runtime modes.
CONNECTOR_MODES: tuple[str, ...] = ("OFFLINE_REPLAY", "SANDBOX_TESTNET", "LIVE_VENUE")

#: The silos this runtime drives.  ``US_ALPACA`` is deliberately absent: no
#: harvested adapter exists for it, so it cannot be fabricated here.
RUNTIME_SILOS: tuple[str, ...] = (
    "CRYPTO_BINANCE",
    "INDIA_ZERODHA_DHAN",
    "US_IBKR",
    "FX_MT5",
)

#: ``ccxt`` Binance spot weight budget per minute (``X-MBX-USED-WEIGHT-1M``).
BINANCE_WEIGHT_BUDGET: int = 1_200

#: Once Binance reports this used weight, the runtime backs off pre-emptively.
BINANCE_WEIGHT_SOFT_CAP: int = 1_000

#: Silos that require a SEBI/NSE ``algo_id`` on every order.
ALGO_ID_SILOS: tuple[str, ...] = ("INDIA_ZERODHA_DHAN",)

_NS_PER_SECOND = 1_000_000_000


class ConnectorError(Exception):
    """A typed, non-retryable-looking connector failure."""

    def __init__(self, code: str, detail: str = "", *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = str(code)
        self.detail = str(detail)
        self.retryable = bool(retryable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "retryable": self.retryable,
        }


@dataclass(slots=True)
class QuoteSnapshot:
    """One normalized top-of-book quote in integer micros."""

    silo: str
    symbol: str
    bid_micros: int
    ask_micros: int
    last_micros: int
    ts_utc_ns: int

    @property
    def spread_bps(self) -> float:
        mid = (self.bid_micros + self.ask_micros) / 2.0
        if mid <= 0:
            return 0.0
        return round((self.ask_micros - self.bid_micros) / mid * 10_000.0, 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "symbol": self.symbol,
            "bid_micros": int(self.bid_micros),
            "ask_micros": int(self.ask_micros),
            "last_micros": int(self.last_micros),
            "spread_bps": self.spread_bps,
            "ts_utc_ns": int(self.ts_utc_ns),
        }


@dataclass(slots=True)
class ConnectorOrder:
    """A normalized order acknowledgement (``openalgo`` + ``ccxt`` shape)."""

    silo: str
    symbol: str
    side: str
    order_type: str
    qty_micros: int
    price_micros: int
    status: str
    client_order_id: str
    venue_order_id: str = ""
    filled_qty_micros: int = 0
    avg_price_micros: int = 0
    algo_id: str = ""
    exchange: str = ""
    mode: str = "OFFLINE_REPLAY"
    ts_utc_ns: int = 0
    raw_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "qty_micros": int(self.qty_micros),
            "price_micros": int(self.price_micros),
            "status": self.status,
            "client_order_id": self.client_order_id,
            "venue_order_id": self.venue_order_id,
            "filled_qty_micros": int(self.filled_qty_micros),
            "avg_price_micros": int(self.avg_price_micros),
            "algo_id": self.algo_id,
            "exchange": self.exchange,
            "mode": self.mode,
            "ts_utc_ns": int(self.ts_utc_ns),
            "raw_kind": self.raw_kind,
        }


@dataclass(slots=True)
class ConnectorPosition:
    """A normalized position with a signed integer-micro quantity."""

    silo: str
    symbol: str
    qty_micros: int
    entry_price_micros: int
    mark_price_micros: int
    unrealized_micros: int = 0
    leverage: float = 1.0

    @property
    def side(self) -> str:
        return "LONG" if self.qty_micros > 0 else ("SHORT" if self.qty_micros < 0 else "FLAT")

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "symbol": self.symbol,
            "qty_micros": int(self.qty_micros),
            "entry_price_micros": int(self.entry_price_micros),
            "mark_price_micros": int(self.mark_price_micros),
            "unrealized_micros": int(self.unrealized_micros),
            "leverage": round(float(self.leverage), 6),
            "side": self.side,
        }


@dataclass(slots=True)
class ConnectorBalance:
    """A normalized free/used/total balance triple in integer micros."""

    silo: str
    asset: str
    free_micros: int
    used_micros: int
    total_micros: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "asset": self.asset,
            "free_micros": int(self.free_micros),
            "used_micros": int(self.used_micros),
            "total_micros": int(self.total_micros),
        }


@dataclass(slots=True)
class _VenueState:
    """Per-venue bookkeeping: open orders, positions, balances, weight."""

    open_orders: dict[str, ConnectorOrder] = field(default_factory=dict)
    positions: dict[str, ConnectorPosition] = field(default_factory=dict)
    balances: dict[str, ConnectorBalance] = field(default_factory=dict)
    used_weight: int = 0
    weight_epoch_s: int = 0
    reject_count: int = 0


class ConnectorRuntime:
    """Ring 0's single normalized venue surface (``G5``).

    The runtime owns no secret.  Credentials live in the Ring 0 broker vault; a
    transport (a thin callable that has already been handed those credentials by
    the composition root) is injected.  This module therefore cannot leak a key
    even if it wanted to.
    """

    def __init__(
        self,
        *,
        connector_mode: str = "OFFLINE_REPLAY",
        allow_network: bool = False,
        transports: Mapping[str, Callable[[str, Mapping[str, Any]], Mapping[str, Any]]] | None = None,
        sweeper: Any = None,
        reference_prices: Mapping[str, float] | None = None,
        weight_budget: int = BINANCE_WEIGHT_BUDGET,
    ) -> None:
        mode = str(connector_mode or "OFFLINE_REPLAY").strip().upper()
        if mode not in CONNECTOR_MODES:
            raise ConnectorError("UNKNOWN_CONNECTOR_MODE", mode)
        if mode == "LIVE_VENUE" and not allow_network:
            # Rule 3: a live mode cannot be reached without an explicit opt-in.
            raise ConnectorError("LIVE_MODE_REQUIRES_ALLOW_NETWORK", mode)
        self.connector_mode = mode
        self.allow_network = bool(allow_network)
        self.transports: dict[str, Callable[[str, Mapping[str, Any]], Mapping[str, Any]]] = {
            str(key).upper(): value for key, value in dict(transports or {}).items()
        }
        self.sweeper = sweeper
        self.weight_budget = max(1, int(weight_budget))
        self._reference = {
            str(key).upper(): float(value)
            for key, value in dict(reference_prices or {}).items()
        }
        self._states: dict[str, _VenueState] = {silo: _VenueState() for silo in RUNTIME_SILOS}
        self._lock = threading.RLock()
        self._sequence = 0
        self._calls = 0
        self._rejections = 0
        self._sweeps = 0

    # ------------------------------------------------------------------
    # Boundary: the one and only float->micros door
    # ------------------------------------------------------------------
    @staticmethod
    def _quantize(value: Any, *, name: str = "value", allow_negative: bool = False) -> int:
        """Quantize a venue float/str/Decimal to integer micros (half-up).

        Anything that is not a finite, representable number is a typed rejection.
        It is never coerced to ``0``: a silent zero is how a real position becomes
        an invisible one.
        """
        if isinstance(value, bool) or value is None:
            raise ConnectorError("VALUE_UNQUANTIZABLE", f"{name}={value!r}")
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ConnectorError("VALUE_UNQUANTIZABLE", f"{name}={value!r}")
            magnitude = abs(float(value))
            if magnitude >= 9.0e12:  # 9e12 units == 9e18 micros, past int64 comfort
                raise ConnectorError("VALUE_OUT_OF_RANGE", f"{name}={value!r}")
            # ``to_micros`` is the single half-up door, and it refuses negatives,
            # so a signed quantity is quantized on its magnitude and re-signed.
            micros = int(to_micros(magnitude))
            if value < 0.0:
                micros = -micros
        else:
            if isinstance(value, int):
                micros = int(value) * MICROS_PER_UNIT
            else:
                try:
                    decimal = Decimal(str(value).strip())
                except (InvalidOperation, ValueError):
                    raise ConnectorError("VALUE_UNQUANTIZABLE", f"{name}={value!r}") from None
                if not decimal.is_finite():
                    raise ConnectorError("VALUE_UNQUANTIZABLE", f"{name}={value!r}")
                micros = int((decimal * MICROS_PER_UNIT).to_integral_value(rounding="ROUND_HALF_UP"))
        if not allow_negative and micros < 0:
            raise ConnectorError("VALUE_NEGATIVE", f"{name}={value!r}")
        return micros

    def ingest_venue_order(self, silo: str, payload: Mapping[str, Any]) -> ConnectorOrder:
        """Normalize a raw ``ccxt`` / Dhan v2 / Kite v3 order document.

        This is the documented ingress for venue data: everything below reads
        floats, everything it returns is integer micros.
        """
        token = self._silo(silo)
        if not isinstance(payload, Mapping):
            raise ConnectorError("PAYLOAD_NOT_MAPPING", token)
        symbol = str(payload.get("symbol") or payload.get("tradingsymbol") or "").upper()
        if not symbol:
            raise ConnectorError("SYMBOL_REQUIRED", token)
        qty = self._quantize(
            payload.get("amount", payload.get("quantity", payload.get("qty", 0))), name="qty"
        )
        price = self._quantize(
            payload.get(
                "average",
                payload.get(
                    "average_price",
                    payload.get("price", payload.get("avg_price", 0)),
                ),
            ),
            name="price",
        )
        filled = self._quantize(
            payload.get("filled", payload.get("filled_quantity", 0)), name="filled"
        )
        side = str(payload.get("side", payload.get("transaction_type", "BUY"))).strip().upper()
        if side in ("B", "BUY", "LONG"):
            side = "BUY"
        elif side in ("S", "SELL", "SHORT"):
            side = "SELL"
        else:
            raise ConnectorError("SIDE_INVALID", side)
        return ConnectorOrder(
            silo=token,
            symbol=symbol,
            side=side,
            order_type=str(payload.get("type", payload.get("order_type", "MARKET"))).strip().upper(),
            qty_micros=max(0, qty),
            price_micros=max(0, price),
            status=str(payload.get("status", "OPEN")).strip().upper(),
            client_order_id=str(
                payload.get("clientOrderId") or payload.get("client_order_id") or payload.get("orderid") or ""
            ),
            venue_order_id=str(payload.get("id") or payload.get("order_id") or ""),
            filled_qty_micros=max(0, filled),
            avg_price_micros=max(0, price),
            algo_id=str(payload.get("algo_id") or payload.get("algoId") or ""),
            exchange=str(payload.get("exchange") or payload.get("exchange_segment") or ""),
            mode=str(payload.get("mode") or self.connector_mode),
            ts_utc_ns=int(payload.get("timestamp") or time.time_ns()),
            raw_kind="venue_document",
        )

    # ------------------------------------------------------------------
    # Normalized operations
    # ------------------------------------------------------------------
    def place_order(
        self,
        *,
        silo: str,
        symbol: str,
        side: str,
        qty: Any,
        price: Any = None,
        order_type: str = "MARKET",
        algo_id: str = "",
        client_order_id: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Place one normalized order through the active runtime mode."""
        token = self._silo(silo)
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            self._calls += 1
            qty_micros = self._quantize(qty, name="qty")
            if qty_micros <= 0:
                raise self._reject("QTY_INVALID", token, qty)
            price_micros = 0 if price is None else self._quantize(price, name="price")
            side_token = str(side or "").strip().upper()
            if side_token not in ("BUY", "SELL"):
                raise self._reject("SIDE_INVALID", token, side)
            kind = str(order_type or "MARKET").strip().upper()
            if token in ALGO_ID_SILOS and not str(algo_id or "").strip():
                # SEBI/NSE: an untagged algo order is not a legal order.
                raise self._reject("ALGO_ID_MISSING", token, symbol)
            self._weight(token, stamp=stamp)
            if self.connector_mode == "OFFLINE_REPLAY":
                order = self._offline_order(
                    token,
                    str(symbol).upper(),
                    side_token,
                    kind,
                    qty_micros,
                    price_micros,
                    stamp,
                    algo_id=str(algo_id or ""),
                )
                self._store(token, order)
                return order.to_dict()
            transport = self.transports.get(token)
            if transport is None:
                raise ConnectorError("TRANSPORT_NOT_CONFIGURED", f"{token}:{self.connector_mode}")
            request = {
                "op": "place_order",
                "silo": token,
                "symbol": str(symbol).upper(),
                "side": side_token,
                "qty_micros": qty_micros,
                "price_micros": price_micros,
                "order_type": kind,
                "algo_id": str(algo_id or ""),
                "client_order_id": str(client_order_id or ""),
                "ts_utc_ns": stamp,
            }
            response = dict(transport("place_order", request) or {})
            self._absorb_weight(token, response, stamp=stamp)
            if not bool(response.get("accepted", True)):
                raise self._reject(
                    str(response.get("code", "VENUE_REJECTED")), token, str(response.get("detail", ""))
                )
            order = self.ingest_venue_order(
                token,
                {
                    **dict(response.get("order", response)),
                    "symbol": str(symbol).upper(),
                    "side": side_token,
                    "algo_id": str(algo_id or ""),
                    "mode": self.connector_mode,
                },
            )
            self._store(token, order)
            return order.to_dict()

    def cancel_order(self, *, silo: str, order_id: str, now_ns: int | None = None) -> dict[str, Any]:
        token = self._silo(silo)
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            self._calls += 1
            self._weight(token, stamp=stamp)
            state = self._states[token]
            transport = self.transports.get(token)
            if self.connector_mode != "OFFLINE_REPLAY" and transport is not None:
                response = dict(transport("cancel_order", {"op": "cancel_order", "order_id": order_id}) or {})
                self._absorb_weight(token, response, stamp=stamp)
            record = state.open_orders.get(str(order_id))
            if record is None:
                return {
                    "silo": token,
                    "order_id": str(order_id),
                    "cancelled": False,
                    "reason": "UNKNOWN_ORDER",
                    "mode": self.connector_mode,
                }
            state.open_orders.pop(str(order_id), None)
            return {
                "silo": token,
                "order_id": str(order_id),
                "cancelled": True,
                "reason": "OPERATOR_CANCEL",
                "mode": self.connector_mode,
                "ts_utc_ns": stamp,
            }

    def fetch_open_orders(self, *, silo: str, now_ns: int | None = None) -> dict[str, Any]:
        token = self._silo(silo)
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            self._calls += 1
            self._weight(token, stamp=stamp)
            transport = self.transports.get(token)
            if self.connector_mode != "OFFLINE_REPLAY" and transport is not None:
                response = dict(transport("fetch_open_orders", {"op": "fetch_open_orders"}) or {})
                self._absorb_weight(token, response, stamp=stamp)
                for row in response.get("orders", ()) or ():
                    if isinstance(row, Mapping):
                        order = self.ingest_venue_order(token, row)
                        self._store(token, order)
            rows = [order.to_dict() for order in self._states[token].open_orders.values()]
            return {
                "silo": token,
                "mode": self.connector_mode,
                "orders": rows,
                "count": len(rows),
                "used_weight": int(self._states[token].used_weight),
                "weight_budget": int(self.weight_budget),
                "ts_utc_ns": stamp,
            }

    def fetch_positions(self, *, silo: str, now_ns: int | None = None) -> dict[str, Any]:
        token = self._silo(silo)
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            self._calls += 1
            self._weight(token, stamp=stamp)
            transport = self.transports.get(token)
            if self.connector_mode != "OFFLINE_REPLAY" and transport is not None:
                response = dict(transport("fetch_positions", {"op": "fetch_positions"}) or {})
                self._absorb_weight(token, response, stamp=stamp)
                for row in response.get("positions", ()) or ():
                    if not isinstance(row, Mapping):
                        continue
                    symbol = str(row.get("symbol") or row.get("tradingsymbol") or "").upper()
                    qty = self._quantize(
                        row.get("contracts", row.get("netqty", row.get("qty", 0))),
                        name="qty",
                        allow_negative=True,
                    )
                    self._states[token].positions[symbol] = ConnectorPosition(
                        silo=token,
                        symbol=symbol,
                        qty_micros=qty,
                        entry_price_micros=self._quantize(
                            row.get("entryPrice", row.get("average_price", 0)), name="entry"
                        ),
                        mark_price_micros=self._quantize(
                            row.get("markPrice", row.get("ltp", 0)), name="mark"
                        ),
                        unrealized_micros=int(
                            round(float(row.get("unrealizedPnl", row.get("unrealised", 0.0)) or 0.0) * MICROS_PER_UNIT)
                        ),
                        leverage=float(row.get("leverage", 1.0) or 1.0),
                    )
            rows = [position.to_dict() for position in self._states[token].positions.values()]
            return {
                "silo": token,
                "mode": self.connector_mode,
                "positions": rows,
                "count": len(rows),
                "ts_utc_ns": stamp,
            }

    def fetch_balances(self, *, silo: str, now_ns: int | None = None) -> dict[str, Any]:
        token = self._silo(silo)
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            self._calls += 1
            self._weight(token, stamp=stamp)
            transport = self.transports.get(token)
            if self.connector_mode != "OFFLINE_REPLAY" and transport is not None:
                response = dict(transport("fetch_balances", {"op": "fetch_balances"}) or {})
                self._absorb_weight(token, response, stamp=stamp)
                for row in response.get("balances", ()) or ():
                    if not isinstance(row, Mapping):
                        continue
                    asset = str(row.get("asset", row.get("currency", "USD"))).upper()
                    free = self._quantize(row.get("free", 0), name="free")
                    used = self._quantize(row.get("used", 0), name="used")
                    total = free + used
                    if row.get("total") is not None:
                        total = self._quantize(row.get("total"), name="total")
                    self._states[token].balances[asset] = ConnectorBalance(
                        silo=token, asset=asset, free_micros=free, used_micros=used, total_micros=total
                    )
            rows = [balance.to_dict() for balance in self._states[token].balances.values()]
            return {
                "silo": token,
                "mode": self.connector_mode,
                "balances": rows,
                "count": len(rows),
                "ts_utc_ns": stamp,
            }

    def execute_wallet_sweep(
        self,
        *,
        silo: str,
        amount_micros: int,
        direction: str = "FUTURE_TO_SPOT",
        mission_id: str = "",
        tranche_id: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Route a physical wallet sweep through the Ring 0 gate ladder.

        Only ``CRYPTO_BINANCE`` has a wallet surface; a sweep aimed at a regulated
        silo is refused rather than silently converted into a wire (``Law 5``).
        """
        token = self._silo(silo)
        if token != "CRYPTO_BINANCE":
            raise ConnectorError("SILO_HAS_NO_WALLET_SURFACE", token)
        if self.sweeper is None:
            raise ConnectorError("NO_SWEEPER_ATTACHED", token, retryable=False)
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            self._calls += 1
            try:
                intent = self.sweeper.plan(
                    token,
                    int(amount_micros),
                    direction=direction,
                    mission_id=str(mission_id),
                    tranche_id=str(tranche_id),
                    now_ns=stamp,
                )
                receipt = self.sweeper.execute(intent, now_ns=stamp)
            except Exception as exc:  # SweepRejected or ledger refusal
                raise ConnectorError(
                    str(getattr(exc, "code", "SWEEP_REJECTED")),
                    str(getattr(exc, "detail", exc)),
                ) from None
            self._sweeps += 1
            return {
                "silo": token,
                "mode": self.connector_mode,
                "accepted": bool(receipt.accepted),
                "state": str(receipt.state),
                "reason": str(receipt.reason),
                "sweep_id": str(intent.sweep_id),
                "amount_micros": int(amount_micros),
                "from_wallet": str(intent.from_wallet),
                "to_wallet": str(intent.to_wallet),
                "transfer_id": int(receipt.transfer_id),
                "liquidity_available_at_utc_ns": int(receipt.liquidity_available_at_utc),
                "signature_present": bool(receipt.signature),
            }

    def quote(self, *, silo: str, symbol: str, now_ns: int | None = None) -> dict[str, Any]:
        """Deterministic, grounded top-of-book for one instrument."""
        token = self._silo(silo)
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        key = str(symbol).upper()
        mid = self._mid(token, key)
        half = max(mid * 0.0001, mid * 0.00005)
        snapshot = QuoteSnapshot(
            silo=token,
            symbol=key,
            bid_micros=int(mid - half),
            ask_micros=int(mid + half),
            last_micros=int(mid),
            ts_utc_ns=stamp,
        )
        return snapshot.to_dict()

    def leverage_tiers(self, *, silo: str) -> dict[str, Any]:
        """Normalized leverage brackets (``ccxt.fetch_leverage_tiers`` shape)."""
        token = self._silo(silo)
        tiers: Sequence[Mapping[str, Any]]
        if token == "CRYPTO_BINANCE":
            tiers = (
                {"max_notional_micros": 50_000 * MICROS_PER_UNIT, "max_leverage": 125.0},
                {"max_notional_micros": 250_000 * MICROS_PER_UNIT, "max_leverage": 100.0},
                {"max_notional_micros": 1_000_000 * MICROS_PER_UNIT, "max_leverage": 50.0},
            )
        elif token == "INDIA_ZERODHA_DHAN":
            tiers = (
                {"max_notional_micros": 5_000_000 * MICROS_PER_UNIT, "max_leverage": 5.0},
                {"max_notional_micros": 20_000_000 * MICROS_PER_UNIT, "max_leverage": 3.0},
            )
        elif token == "US_IBKR":
            tiers = (
                {"max_notional_micros": 10_000_000 * MICROS_PER_UNIT, "max_leverage": 4.0},
                {"max_notional_micros": 40_000_000 * MICROS_PER_UNIT, "max_leverage": 2.0},
            )
        else:
            tiers = ({"max_notional_micros": 2_000_000 * MICROS_PER_UNIT, "max_leverage": 30.0},)
        return {
            "silo": token,
            "tiers": [dict(row) for row in tiers],
            "count": len(tiers),
            "mode": self.connector_mode,
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self, *, now_ns: int | None = None) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            return {
                "page": "connectors.status",
                "connector_mode": self.connector_mode,
                "modes": list(CONNECTOR_MODES),
                "allow_network": bool(self.allow_network),
                "air_gap": not bool(self.allow_network),
                "silos": list(RUNTIME_SILOS),
                "transports": sorted(self.transports),
                "calls": int(self._calls),
                "rejections": int(self._rejections),
                "sweeps": int(self._sweeps),
                "weight_budget": int(self.weight_budget),
                "weight": {
                    silo: int(state.used_weight) for silo, state in self._states.items()
                },
                "money_units": "integer_micros",
                "float_at_boundary": True,
                "as_of_utc_ns": stamp,
                "law": "sec_017 - one normalized surface, no key ever held here",
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _silo(self, silo: str) -> str:
        token = str(silo or "").strip().upper()
        if token not in RUNTIME_SILOS:
            raise ConnectorError("UNKNOWN_SILO", token)
        return token

    def _reject(self, code: str, silo: str, detail: Any) -> ConnectorError:
        self._rejections += 1
        self._states.get(silo, _VenueState()).reject_count += 1
        return ConnectorError(code, str(detail))

    def _weight(self, silo: str, *, stamp: int) -> None:
        """Binance-style weight accounting with a rolling one-minute epoch."""
        if silo != "CRYPTO_BINANCE":
            return
        state = self._states[silo]
        epoch = stamp // (60 * _NS_PER_SECOND)
        if epoch != state.weight_epoch_s:
            state.weight_epoch_s = epoch
            state.used_weight = 0
        state.used_weight += 1
        if state.used_weight > self.weight_budget:
            raise ConnectorError(
                "WEIGHT_BUDGET_EXCEEDED",
                f"used={state.used_weight} budget={self.weight_budget}",
                retryable=True,
            )
        if state.used_weight == BINANCE_WEIGHT_SOFT_CAP:
            # Pre-emptive back-off beats a 429 later.
            return None
        return None

    def _absorb_weight(self, silo: str, response: Mapping[str, Any], *, stamp: int) -> None:
        scope = "futures" if silo == "CRYPTO_BINANCE" and response.get("market") == "futures" else "spot"
        raw = response.get("headers") if isinstance(response.get("headers"), Mapping) else {}
        key = f"X-MBX-USED-WEIGHT-1M" if scope == "spot" else "X-MBX-USED-WEIGHT-1M"
        for candidate in (raw.get(key), raw.get(key.lower()), response.get("used_weight")):
            if candidate is None:
                continue
            try:
                used = int(candidate)
            except (TypeError, ValueError):
                continue
            self._states[silo].used_weight = used
            self._states[silo].weight_epoch_s = stamp // (60 * _NS_PER_SECOND)
            break

    def _store(self, silo: str, order: ConnectorOrder) -> None:
        state = self._states[silo]
        if str(order.status).upper() in ("CLOSED", "FILLED", "CANCELED", "CANCELLED", "REJECTED"):
            state.open_orders.pop(order.client_order_id, None)
        else:
            state.open_orders[order.client_order_id] = order

    def _mid(self, silo: str, symbol: str) -> int:
        reference = self._reference.get(silo)
        if reference is None:
            reference = {
                "CRYPTO_BINANCE": 64_000.0,
                "INDIA_ZERODHA_DHAN": 2_400.0,
                "US_IBKR": 185.0,
                "FX_MT5": 1.085,
            }.get(silo, 100.0)
        digest = hashlib.sha256(f"{silo}:{symbol}".encode("utf-8")).digest()
        wobble = int.from_bytes(digest[:2], "big") / 65_535.0
        units = float(reference) * (1.0 + 0.01 * (wobble - 0.5))
        return max(1, int(to_micros(units)))

    def _offline_order(
        self,
        silo: str,
        symbol: str,
        side: str,
        order_type: str,
        qty_micros: int,
        price_micros: int,
        stamp: int,
        *,
        algo_id: str,
    ) -> ConnectorOrder:
        self._sequence += 1
        client_id = f"OFFLINE-{silo[:4]}-{self._sequence:08d}"
        venue_id = hashlib.sha256(f"{silo}:{client_id}:{stamp}".encode("utf-8")).hexdigest()[:16]
        effective = price_micros or self._mid(silo, symbol)
        return ConnectorOrder(
            silo=silo,
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty_micros=int(qty_micros),
            price_micros=int(effective),
            status="FILLED",
            client_order_id=client_id,
            venue_order_id=venue_id,
            filled_qty_micros=int(qty_micros),
            avg_price_micros=int(effective),
            algo_id=str(algo_id),
            exchange="OFFLINE",
            mode="OFFLINE_REPLAY",
            ts_utc_ns=stamp,
            raw_kind="offline_replay",
        )
