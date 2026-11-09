"""``OffsetConverter`` shim for the harvested ``vnpy_core`` tree.

``engine.py`` imports ``OffsetConverter`` so the main engine can split a net
order request into today/yesterday offset legs for Chinese futures.  The harvest
kept the call site but not the module.

This implementation preserves the public contract used by ``MainEngine``:

* :meth:`OffsetConverter.update_position` -- seed/refresh from a ``PositionData``
* :meth:`OffsetConverter.update_trade`    -- apply a fill to the holding
* :meth:`OffsetConverter.convert_order_request` -- split a request into legs

It deliberately keeps the *net* position book simple (one long and one short
bucket per contract).  Ring 0's India and FX silos trade net positions; the
today/yesterday split only matters for SHFE/INE close-today rules, which the CTP
gateway applies upstream of this converter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .constant import Direction, Offset, OrderType
from .object import ContractData, OrderData, OrderRequest, PositionData, TradeData

__all__ = ["PositionHolding", "OffsetConverter"]


@dataclass(slots=True)
class PositionHolding:
    """Net long/short holding for one contract."""

    contract: ContractData
    long_qty: float = 0.0
    short_qty: float = 0.0
    long_yd_qty: float = 0.0
    short_yd_qty: float = 0.0
    long_avg_price: float = 0.0
    short_avg_price: float = 0.0

    @property
    def vt_symbol(self) -> str:
        return getattr(self.contract, "vt_symbol", "")

    @property
    def net_qty(self) -> float:
        return self.long_qty - self.short_qty

    def to_dict(self) -> dict[str, Any]:
        return {
            "vt_symbol": self.vt_symbol,
            "long_qty": self.long_qty,
            "short_qty": self.short_qty,
            "long_yd_qty": self.long_yd_qty,
            "short_yd_qty": self.short_yd_qty,
            "long_avg_price": self.long_avg_price,
            "short_avg_price": self.short_avg_price,
        }


class OffsetConverter:
    """Split order requests into open/close legs against a net holding."""

    def __init__(self, contract: ContractData) -> None:
        self.contract = contract
        self.holding = PositionHolding(contract)

    # -- book maintenance --------------------------------------------------
    def update_position(self, position: PositionData) -> None:
        direction = getattr(position, "direction", None)
        qty = float(getattr(position, "volume", 0.0) or 0.0)
        yd = float(getattr(position, "yd_volume", 0.0) or 0.0)
        price = float(getattr(position, "price", 0.0) or 0.0)
        if direction == Direction.LONG:
            self.holding.long_qty = qty
            self.holding.long_yd_qty = min(yd, qty)
            self.holding.long_avg_price = price
        elif direction == Direction.SHORT:
            self.holding.short_qty = qty
            self.holding.short_yd_qty = min(yd, qty)
            self.holding.short_avg_price = price

    def update_trade(self, trade: TradeData) -> None:
        qty = float(getattr(trade, "volume", 0.0) or 0.0)
        price = float(getattr(trade, "price", 0.0) or 0.0)
        offset = getattr(trade, "offset", Offset.NONE)
        direction = getattr(trade, "direction", Direction.LONG)

        if offset == Offset.OPEN:
            if direction == Direction.LONG:
                total = self.holding.long_qty + qty
                self.holding.long_avg_price = (
                    (self.holding.long_avg_price * self.holding.long_qty + price * qty) / total
                    if total
                    else price
                )
                self.holding.long_qty = total
            else:
                total = self.holding.short_qty + qty
                self.holding.short_avg_price = (
                    (self.holding.short_avg_price * self.holding.short_qty + price * qty) / total
                    if total
                    else price
                )
                self.holding.short_qty = total
        elif offset in (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY):
            if direction == Direction.LONG:
                self.holding.short_qty = max(0.0, self.holding.short_qty - qty)
                if offset in (Offset.CLOSE, Offset.CLOSEYESTERDAY):
                    self.holding.short_yd_qty = max(0.0, self.holding.short_yd_qty - qty)
            else:
                self.holding.long_qty = max(0.0, self.holding.long_qty - qty)
                if offset in (Offset.CLOSE, Offset.CLOSEYESTERDAY):
                    self.holding.long_yd_qty = max(0.0, self.holding.long_yd_qty - qty)

    # -- request conversion ------------------------------------------------
    def convert_order_request(
        self,
        req: OrderRequest,
        lock: bool = False,
        net: bool = False,
    ) -> list[OrderRequest]:
        """Return the legs needed to execute ``req``.

        ``net=False`` (the default, and what ``MainEngine`` passes for every
        non-Chinese venue) returns the request unchanged.  ``net=True`` splits a
        reduce-only request into close-today / close-yesterday legs.
        """

        if not net:
            return [req]

        qty = float(getattr(req, "volume", 0.0) or 0.0)
        if qty <= 0:
            return []

        if req.direction == Direction.LONG:
            available = self.holding.short_qty
            today_available = max(0.0, self.holding.short_qty - self.holding.short_yd_qty)
            yd_available = self.holding.short_yd_qty
            close_offset = Offset.CLOSE if req.offset == Offset.NONE else req.offset
        else:
            available = self.holding.long_qty
            today_available = max(0.0, self.holding.long_qty - self.holding.long_yd_qty)
            yd_available = self.holding.long_yd_qty
            close_offset = Offset.CLOSE if req.offset == Offset.NONE else req.offset

        if available <= 0:
            return [req] if req.offset != Offset.NONE else [self._with_offset(req, Offset.OPEN, qty)]

        legs: list[OrderRequest] = []
        remaining = min(qty, available)
        if close_offset == Offset.CLOSETODAY:
            legs.append(self._with_offset(req, Offset.CLOSETODAY, min(remaining, today_available)))
        elif close_offset == Offset.CLOSEYESTERDAY:
            legs.append(self._with_offset(req, Offset.CLOSEYESTERDAY, min(remaining, yd_available)))
        else:
            if today_available:
                legs.append(self._with_offset(req, Offset.CLOSETODAY, min(remaining, today_available)))
                remaining -= min(remaining, today_available)
            if remaining and yd_available:
                legs.append(self._with_offset(req, Offset.CLOSEYESTERDAY, min(remaining, yd_available)))

        legs = [leg for leg in legs if float(getattr(leg, "volume", 0.0) or 0.0) > 0]
        return legs or [req]

    @staticmethod
    def _with_offset(req: OrderRequest, offset: Offset, volume: float) -> OrderRequest:
        return OrderRequest(
            symbol=req.symbol,
            exchange=req.exchange,
            direction=req.direction,
            type=req.type,
            volume=float(volume),
            price=req.price,
            offset=offset,
            reference=req.reference,
        )
