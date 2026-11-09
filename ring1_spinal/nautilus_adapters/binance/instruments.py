"""Binance instrument loading for Ring 1 (NIOS compatibility build).

The harvested version imported ``_load_binance_instruments`` from
``nautilus_trader._libnautilus.binance``, a compiled module that does not exist
in nautilus_trader 1.231.0 and is policy-blocked on hardened hosts.

This version keeps the same coroutine contract — ``await load_binance_instruments(config)``
returns a list of instrument descriptors — but resolves them deterministically
from the NIOS instrument catalogue instead of a network call, so instrument
loading can never block the Ring 1 startup path or leak an exchange socket.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .._nautilus_compat import Shim, format_order_payload, venue_spec

__all__ = [
    "BinanceInstrument",
    "DEFAULT_BINANCE_INSTRUMENTS",
    "load_binance_instruments",
]


@dataclass(slots=True, frozen=True)
class BinanceInstrument:
    """Deterministic Binance spot instrument descriptor."""

    symbol: str
    base_currency: str
    quote_currency: str
    price_precision: int = 2
    size_precision: int = 8
    min_quantity: float = 1e-8
    min_notional: float = 5.0
    status: str = "TRADING"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "price_precision": self.price_precision,
            "size_precision": self.size_precision,
            "min_quantity": self.min_quantity,
            "min_notional": self.min_notional,
            "status": self.status,
        }


#: The pairs the CRYPTO silo actually routes: the scalp book plus the
#: ``BTC-USDT`` long-term compounder sweep ticker from ``sec_015``.
DEFAULT_BINANCE_INSTRUMENTS: tuple[BinanceInstrument, ...] = (
    BinanceInstrument("BTCUSDT", "BTC", "USDT"),
    BinanceInstrument("ETHUSDT", "ETH", "USDT"),
    BinanceInstrument("SOLUSDT", "SOL", "USDT", price_precision=3, size_precision=6),
    BinanceInstrument("BNBUSDT", "BNB", "USDT", price_precision=2, size_precision=5),
    BinanceInstrument("XRPUSDT", "XRP", "USDT", price_precision=4, size_precision=2),
    BinanceInstrument("USYCUSDT", "USYC", "USDT", price_precision=4, size_precision=4),
)


def _resolve(config: Any) -> list[BinanceInstrument]:
    requested = getattr(config, "symbols", None)
    if not requested:
        return list(DEFAULT_BINANCE_INSTRUMENTS)
    catalogue = {instrument.symbol: instrument for instrument in DEFAULT_BINANCE_INSTRUMENTS}
    resolved: list[BinanceInstrument] = []
    for symbol in requested:
        key = str(symbol).upper().replace("-", "").replace("/", "")
        resolved.append(
            catalogue.get(
                key,
                BinanceInstrument(key, key.removesuffix("USDT") or key, "USDT"),
            )
        )
    return resolved


async def load_binance_instruments(config: Any = None) -> list[BinanceInstrument]:
    """Return the Binance instruments the CRYPTO silo may trade.

    Signature-compatible with the harvested upstream coroutine; never touches
    the network and never raises for an unknown symbol (it synthesises a
    descriptor instead, so Ring 1 startup cannot fail on catalogue drift).
    """

    return await asyncio.to_thread(_resolve, config)


# Re-exported so ``from .binance.instruments import ...`` keeps working for
# callers that imported the harvested helper names.
_load_binance_instruments = _resolve
BinanceDataClientConfig = Shim
format_order_payload = format_order_payload
BINANCE_SPEC = venue_spec("binance")
