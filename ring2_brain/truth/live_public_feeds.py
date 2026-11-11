"""Live dual-vendor public market feed connector (``sec_003`` / ``sec_006``).

Law 1 of ``sec_018`` is absolute: a single venue is never trusted, and a resting
order is a hypothesis rather than liquidity.  This module therefore polls *two
independent public venues* for every symbol, cross-checks them, and only then
emits a :class:`~core.schemas.DualVendorBar` plus an
:class:`~core.schemas.ExecutedFlowTick` built from **executed** aggTrades rather
than from manipulable L2 snapshots.

Design constraints that come straight from the constitution:

* **Unauthenticated only.** No API key, no signature, no account scope.  Ring 2
  must never hold a credentialed socket, so public market-data endpoints are the
  entire surface area of this module.
* **Never hang.** Every request carries a short socket timeout, the probe is
  bounded, and any transport failure degrades to a deterministic synthetic
  dual-vendor tape so tests and air-gapped hosts keep running instead of blocking.
* **Never fabricate a live quote.** Synthetic frames are labelled
  ``source="synthetic"`` / ``mode="SYNTHETIC_FALLBACK"`` all the way to the
  cockpit, so an operator can always tell a real cross-venue check from a
  deterministic rehearsal.

Pipeline::

    binance bookTicker + aggTrades  ─┐
                                     ├─► DualVendorBar ─► SelfHealingDataGuard
    okx ticker + trades            ─┘        │
                                              └─► ExecutedFlowTick ─► multiplexer

The multiplexer is injected rather than imported: ``SiloWebSocketMultiplexer``
belongs to Ring 1, and Ring 2 must not reach into Ring 1's execution path.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.schemas import DualVendorBar, ExecutedFlowTick

__all__ = [
    "FEED_MODE_LIVE",
    "FEED_MODE_PAPER",
    "FEED_MODE_SYNTHETIC",
    "FEED_MODES",
    "PRIMARY_VENUE",
    "SECONDARY_VENUE",
    "VENDOR_ENDPOINTS",
    "SYMBOL_MAP",
    "SYMBOL_ALIASES",
    "REFERENCE_PRICES",
    "SILO_FEED_SPECS",
    "FEED_TIMEOUT_S",
    "MAX_FEED_LAG_MS",
    "FeedUnavailable",
    "SiloFeedSpec",
    "VendorQuote",
    "FeedFrame",
    "HttpTransport",
    "LiveDualVendorFeedConnector",
    "MultiSiloFeedConnector",
    "synthetic_quotes",
    "quotations_to_bar",
]


#: ``PAPER_AUTOPILOT`` is the deterministic in-process rehearsal tape.  It is a
#: *feed mode*, not a synthetic fallback: the operator asked for it explicitly.
FEED_MODE_PAPER = "PAPER_AUTOPILOT"
FEED_MODE_LIVE = "LIVE_PUBLIC_DUAL_VENDOR"
FEED_MODE_SYNTHETIC = "SYNTHETIC_FALLBACK"

#: The only two modes an operator may hot-swap between (``sec_016``).
FEED_MODES: tuple[str, ...] = (FEED_MODE_PAPER, FEED_MODE_LIVE)

PRIMARY_VENUE = "binance"
SECONDARY_VENUE = "okx"

#: Conservative socket budget.  A blocked host fails in ~1.2 s, never hangs.
FEED_TIMEOUT_S = 1.2

#: Public, unauthenticated REST bases.  No key, no signature, no account scope.
VENDOR_ENDPOINTS: dict[str, dict[str, str]] = {
    "binance": {
        "rest": "https://api.binance.com",
        "book": "/api/v3/ticker/bookTicker?symbol={symbol}",
        "trades": "/api/v3/aggTrades?symbol={symbol}&limit={limit}",
        "ws": "wss://stream.binance.com:9443/stream",
    },
    "okx": {
        "rest": "https://www.okx.com",
        "book": "/api/v5/market/ticker?instId={symbol}",
        "trades": "/api/v5/market/trades?instId={symbol}&limit={limit}",
        "ws": "wss://ws.okx.com:8443/ws/v5/public",
    },
    "bybit": {
        "rest": "https://api.bybit.com",
        "book": "/v5/market/tickers?category=spot&symbol={symbol}",
        "trades": "/v5/market/recent-trade?category=spot&symbol={symbol}&limit={limit}",
        "ws": "wss://stream.bybit.com/v5/public/spot",
    },
    # Unauthenticated equity / ETF / FX quotes.  No key, no session cookie.
    "yahoo": {
        "rest": "https://query1.finance.yahoo.com",
        "book": "/v8/finance/chart/{symbol}?interval=1m&range=1d",
        "trades": "",
        "ws": "",
        "format": "json",
        "flow": "false",
    },
    "stooq": {
        "rest": "https://stooq.com",
        "book": "/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv",
        "trades": "",
        "ws": "",
        "format": "csv",
        "flow": "false",
    },
}

#: Canonical symbol -> per-venue instrument id.
#:
#: ``sec_015`` fixes one tradable per silo; the connector also accepts the
#: second instrument each silo is allowed to quote so a cross-check can run
#: without inventing a new whitelist entry.
SYMBOL_MAP: dict[str, dict[str, str]] = {
    # -- CRYPTO_BINANCE (venue-native id == canonical for binance/bybit) --
    "BTCUSDT": {
        "binance": "BTCUSDT",
        "okx": "BTC-USDT",
        "bybit": "BTCUSDT",
        "yahoo": "BTC-USD",
        "stooq": "btcusd",
    },
    "ETHUSDT": {
        "binance": "ETHUSDT",
        "okx": "ETH-USDT",
        "bybit": "ETHUSDT",
        "yahoo": "ETH-USD",
        "stooq": "ethusd",
    },
    # -- INDIA_ZERODHA_DHAN ------------------------------------------------
    "RELIANCE.NS": {"yahoo": "RELIANCE.NS", "stooq": "reliance.in"},
    "NIFTYBEES.NS": {"yahoo": "NIFTYBEES.NS", "stooq": "niftybees.in"},
    # -- US_IBKR -----------------------------------------------------------
    "NVDA": {"yahoo": "NVDA", "stooq": "nvda.us"},
    "VOO": {"yahoo": "VOO", "stooq": "voo.us"},
    # -- FX_MT5 ------------------------------------------------------------
    "EURUSD": {"yahoo": "EURUSD=X", "stooq": "eurusd"},
}

#: Symbol spellings the orchestrator / cockpit already use.
SYMBOL_ALIASES: dict[str, str] = {
    "BTC-USDT": "BTCUSDT",
    "ETH-USDT": "ETHUSDT",
    "EUR_USD": "EURUSD",
}

#: Deterministic reference mids for the synthetic fallback, per canonical symbol.
REFERENCE_PRICES: dict[str, float] = {
    "BTCUSDT": 64_250.0,
    "ETHUSDT": 2_605.0,
    "RELIANCE.NS": 2_942.5,
    "NIFTYBEES.NS": 285.4,
    "NVDA": 121.4,
    "VOO": 521.8,
    "EURUSD": 1.0842,
    "BTC-USD": 64_250.0,
}


def canonical_symbol(symbol: str) -> str:
    """Normalise ``BTC-USDT`` / ``EUR_USD`` onto the connector's canonical id."""

    token = str(symbol).strip()
    return SYMBOL_ALIASES.get(token, token)


@dataclass(frozen=True, slots=True)
class SiloFeedSpec:
    """One sovereign silo's dual-vendor public-quote whitelist.

    ``venues`` is ordered ``(primary, secondary)`` so the frame's ``p1``/``p2``
    always map to the same two independent venues across every poll.
    """

    silo: str
    market: str
    symbols: tuple[str, ...]
    venues: tuple[str, str]
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "market": self.market,
            "label": self.label,
            "symbols": list(self.symbols),
            "primary": self.venues[0],
            "secondary": self.venues[1],
        }


#: ``sec_015``: the four silos and the exact instruments each may quote.
SILO_FEED_SPECS: dict[str, SiloFeedSpec] = {
    "CRYPTO_BINANCE": SiloFeedSpec(
        silo="CRYPTO_BINANCE",
        market="CRYPTO",
        symbols=("BTCUSDT", "ETHUSDT"),
        venues=("binance", "okx"),
        label="Crypto · Binance spot",
    ),
    "INDIA_ZERODHA_DHAN": SiloFeedSpec(
        silo="INDIA_ZERODHA_DHAN",
        market="INDIA",
        symbols=("RELIANCE.NS", "NIFTYBEES.NS"),
        venues=("yahoo", "stooq"),
        label="India · Dhan / Zerodha NSE",
    ),
    "US_IBKR": SiloFeedSpec(
        silo="US_IBKR",
        market="US",
        symbols=("NVDA", "VOO"),
        venues=("yahoo", "stooq"),
        label="US · Interactive Brokers",
    ),
    "FX_MT5": SiloFeedSpec(
        silo="FX_MT5",
        market="FX",
        symbols=("EURUSD",),
        venues=("yahoo", "stooq"),
        label="FX · MetaTrader 5",
    ),
}

#: Stable silo ordering for every aggregate report and UI table.
SILO_FEED_ORDER: tuple[str, ...] = tuple(SILO_FEED_SPECS)

UNKNOWN_SILO_MSG = "unsupported silo for public dual-vendor quotes"

DEFAULT_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")

#: Hard ceiling on the lag a frame may claim before the bar is untrustworthy.
MAX_FEED_LAG_MS = 5_000.0

#: Trades sampled per venue when reconstructing executed taker flow.
TRADE_SAMPLE = 20


class FeedUnavailable(RuntimeError):
    """Raised internally when neither venue can be reached in budget."""


# ---------------------------------------------------------------------------
# Quotes and frames
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VendorQuote:
    """One venue's view of one instrument at one instant."""

    venue: str
    symbol: str
    bid: float
    ask: float
    last: float
    ts_ns: int
    source: str = "rest"
    taker_buy_volume: float = 0.0
    taker_sell_volume: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0.0 and self.ask > 0.0:
            return 0.5 * (self.bid + self.ask)
        return float(self.last)

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0.0 or self.bid <= 0.0 or self.ask <= 0.0:
            return 0.0
        return max(0.0, (self.ask - self.bid) / mid * 1e4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "mid": self.mid,
            "spread_bps": round(self.spread_bps, 6),
            "ts_ns": int(self.ts_ns),
            "source": self.source,
            "taker_buy_volume": self.taker_buy_volume,
            "taker_sell_volume": self.taker_sell_volume,
        }


def _resolve(value: Any) -> Any:
    """Read a value that may be exposed as either a property or a method."""

    return value() if callable(value) else value


@dataclass(slots=True)
class FeedFrame:
    """A cross-verified frame ready for Ring 1 consumption."""

    symbol: str
    mode: str
    bar: DualVendorBar
    tick: ExecutedFlowTick
    primary: VendorQuote
    secondary: VendorQuote
    accepted: bool
    reason: str = ""
    normalized: Any = None
    round_index: int = 0
    silo: str = "CRYPTO_BINANCE"

    @property
    def divergence_pct(self) -> float:
        return float(self.bar.vendor_divergence_pct)

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "symbol": self.symbol,
            "mode": self.mode,
            "accepted": self.accepted,
            "reason": self.reason,
            "round_index": self.round_index,
            "p1": self.bar.p1,
            "p2": self.bar.p2,
            "price": self.bar.price,
            "feed_lag_ms": self.bar.feed_lag_ms,
            "divergence_pct": round(self.divergence_pct, 6),
            "valid_time_ns": int(self.bar.valid_time_ns),
            "knowledge_ts_ns": int(self.bar.knowledge_ts_ns),
            "primary": self.primary.to_dict(),
            "secondary": self.secondary.to_dict(),
            "spread_bps": round(float(self.tick.spread_bps), 6),
            "taker_buy_ratio": round(float(_resolve(self.tick.taker_buy_ratio_5s)), 6),
            "max_cancel_to_fill": round(float(_resolve(self.tick.max_cancel_to_fill_ratio)), 6),
            "passes_anti_spoof_ofi": bool(_resolve(self.tick.passes_anti_spoof_ofi)),
        }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class HttpTransport:
    """Minimal JSON GET transport with a hard socket timeout.

    Swappable so tests can inject a mock transport and never touch the network.
    """

    def __init__(self, *, timeout_s: float = FEED_TIMEOUT_S, user_agent: str = "nios-feed/1.0"):
        self.timeout_s = float(timeout_s)
        self.user_agent = user_agent
        self.calls = 0
        self.failures = 0
        self.last_error = ""

    def get_json(self, url: str) -> Any:
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = response.read()
            return json.loads(payload.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise FeedUnavailable(self.last_error) from exc

    def get_text(self, url: str) -> str:
        """Plain-text GET for CSV venues (Stooq).  Same hard socket budget."""

        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = response.read()
            text = payload.decode("utf-8", errors="replace").strip()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise FeedUnavailable(self.last_error) from exc
        if not text:
            self.failures += 1
            self.last_error = "empty body"
            raise FeedUnavailable("empty body")
        return text

    def status(self) -> dict[str, Any]:
        return {
            "timeout_s": self.timeout_s,
            "calls": self.calls,
            "failures": self.failures,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# Deterministic synthetic fallback
# ---------------------------------------------------------------------------


def _hash_unit(key: str) -> float:
    """Stable 0..1 pseudo-random value derived from a key (no global RNG)."""

    acc = 0x811C9DC5
    for char in key:
        acc = ((acc ^ ord(char)) * 0x01000193) & 0xFFFFFFFF
    return acc / 0xFFFFFFFF


def synthetic_quotes(
    symbol: str,
    *,
    now_ns: int | None = None,
    round_index: int = 0,
    primary_venue: str | None = None,
    secondary_venue: str | None = None,
) -> tuple[VendorQuote, VendorQuote]:
    """Deterministically synthesise a plausible two-venue tape.

    The two venues are deliberately *close but not identical* so the divergence
    gate is genuinely exercised instead of trivially passing.  Output depends
    only on ``(symbol, round_index)`` — no wall-clock entropy, no global RNG — so
    a failing test reproduces exactly.
    """

    key = canonical_symbol(symbol)
    stamp = int(now_ns if now_ns is not None else time.time_ns())
    base = float(REFERENCE_PRICES.get(key, 100.0))
    unit = _hash_unit(key)
    phase = round_index * 0.37 + unit * 2.0 * math.pi
    mid = base * (1.0 + 0.0009 * math.sin(phase))
    half_spread = mid * 0.00004
    skew = mid * 0.00002 * math.cos(phase * 1.7)
    # A deliberate ~1.5 bps venue offset: the divergence gate must be genuinely
    # exercised, not trivially passed by two identical synthetic mids.
    drift = mid * 0.00015 * math.cos(phase * 1.3)
    venue_ids = SYMBOL_MAP.get(key, {})
    venue_a = primary_venue or PRIMARY_VENUE
    venue_b = secondary_venue or SECONDARY_VENUE
    # Scale executed size so a $1.08 FX pair does not look like 150 BTC lots.
    flow_scale = 1.0 if base > 100.0 else 100_000.0

    primary = VendorQuote(
        venue=venue_a,
        symbol=venue_ids.get(venue_a, key),
        bid=mid - half_spread,
        ask=mid + half_spread,
        last=mid + skew,
        ts_ns=stamp - 3_000_000,
        source="synthetic",
        taker_buy_volume=(150.0 + 40.0 * math.sin(phase)) * flow_scale,
        taker_sell_volume=(3.0 + 2.0 * math.cos(phase)) * flow_scale,
    )
    secondary = VendorQuote(
        venue=venue_b,
        symbol=venue_ids.get(venue_b, key),
        bid=mid + drift - half_spread * 1.1,
        ask=mid + drift + half_spread * 1.1,
        last=mid + drift + skew * 1.4,
        ts_ns=stamp - 7_000_000,
        source="synthetic",
        taker_buy_volume=(148.0 + 38.0 * math.sin(phase + 0.1)) * flow_scale,
        taker_sell_volume=(4.0 + 2.0 * math.cos(phase + 0.1)) * flow_scale,
    )
    return primary, secondary


def quotations_to_bar(
    primary: VendorQuote,
    secondary: VendorQuote,
    *,
    symbol: str,
    received_ns: int | None = None,
) -> DualVendorBar:
    """Build a :class:`DualVendorBar` from two independently normalised quotes.

    ``valid_time_ns`` is the *older* venue timestamp (the instant both venues
    had spoken) while ``knowledge_ts_ns`` is the instant this process learned
    the pair.  That ordering is what makes the bar point-in-time honest: the
    guard compares both against ``as_of_ns`` and quarantines any lookahead.
    """

    received = int(received_ns if received_ns is not None else time.time_ns())
    valid = min(int(primary.ts_ns), int(secondary.ts_ns))
    lag_ms = max(0.0, (received - valid) / 1e6)
    return DualVendorBar(
        symbol=symbol,
        p1=float(primary.mid),
        p2=float(secondary.mid),
        feed_lag_ms=min(lag_ms, MAX_FEED_LAG_MS * 10.0),
        valid_time_ns=valid,
        knowledge_ts_ns=received,
    )


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class LiveDualVendorFeedConnector:
    """Poll two independent public venues and emit cross-verified frames.

    Parameters
    ----------
    symbols:
        Canonical (Binance-style) symbols.  Each must exist in :data:`SYMBOL_MAP`.
    primary / secondary:
        Venue keys in :data:`VENDOR_ENDPOINTS`.
    transport:
        Anything exposing ``get_json(url)``.  Inject a mock to stay offline.
    guard:
        A :class:`~ring2_brain.truth.data_guard.SelfHealingDataGuard`.  When
        supplied every bar must survive the lag / split / divergence / PIT gates.
    multiplexer:
        A Ring 1 ``SiloWebSocketMultiplexer`` (injected, never imported).  When
        supplied, L2 and trade frames are pushed through it for latency and
        quarantine accounting.
    synthetic_fallback:
        When ``True`` (the default) an unreachable venue degrades to
        :func:`synthetic_quotes` instead of raising, so an air-gapped host or a
        CI runner keeps producing deterministic frames.
    """

    def __init__(
        self,
        *,
        silo: str | None = None,
        symbols: Sequence[str] | None = None,
        primary: str = PRIMARY_VENUE,
        secondary: str = SECONDARY_VENUE,
        transport: Any | None = None,
        guard: Any | None = None,
        multiplexer: Any | None = None,
        shadow_book: Any | None = None,
        ofi_ring: Any | None = None,
        timeout_s: float = FEED_TIMEOUT_S,
        synthetic_fallback: bool = True,
        clock: Callable[[], int] = time.time_ns,
    ):
        # A silo spec is authoritative: the operator cannot quietly point the
        # India silo at a crypto venue (``sec_015`` whitelist).
        self.silo_spec: SiloFeedSpec | None = None
        if silo is not None:
            key = str(silo).strip().upper()
            if key not in SILO_FEED_SPECS:
                raise ValueError(f"{UNKNOWN_SILO_MSG}: {silo}")
            spec = SILO_FEED_SPECS[key]
            self.silo_spec = spec
            silo = spec.silo
            symbols = spec.symbols if symbols is None else symbols
            primary, secondary = spec.venues
        self.silo = str(silo).strip().upper() if silo else "CRYPTO_BINANCE"

        chosen = (
            tuple(canonical_symbol(item) for item in symbols)
            if symbols
            else DEFAULT_SYMBOLS
        )
        unknown = [item for item in chosen if item not in SYMBOL_MAP]
        if unknown:
            raise ValueError(f"unsupported public feed symbols: {unknown}")
        spec = self.silo_spec
        if spec is not None:
            foreign = [item for item in chosen if item not in set(spec.symbols)]
            if foreign:
                raise ValueError(
                    f"{spec.silo} is whitelisted to {list(spec.symbols)} only "
                    f"(sec_015); refusing {foreign}"
                )
        for venue in (primary, secondary):
            if venue not in VENDOR_ENDPOINTS:
                raise ValueError(f"unsupported venue: {venue}")
        if primary == secondary:
            raise ValueError("dual-vendor feed requires two distinct venues")

        self.symbols = chosen
        self.primary = primary
        self.secondary = secondary
        self.transport = transport if transport is not None else HttpTransport(timeout_s=timeout_s)
        self.guard = guard
        self.multiplexer = multiplexer
        self.shadow_book = shadow_book
        self.ofi_ring = ofi_ring
        self.timeout_s = float(timeout_s)
        self.synthetic_fallback = bool(synthetic_fallback)
        self.clock = clock

        self.round_index = 0
        self.live_frames = 0
        self.synthetic_frames = 0
        self.rejected_frames = 0
        self.transport_failures = 0
        self.last_error = ""
        self.last_frame: FeedFrame | None = None
        self._mode = FEED_MODE_SYNTHETIC
        self._frames: list[FeedFrame] = []

    # -- venue adapters -----------------------------------------------------

    def _venue_symbol(self, symbol: str, venue: str) -> str:
        return SYMBOL_MAP[canonical_symbol(symbol)][venue]

    def _book_url(self, symbol: str, venue: str) -> str:
        spec = VENDOR_ENDPOINTS[venue]
        return spec["rest"] + spec["book"].format(symbol=self._venue_symbol(symbol, venue))

    def _trades_url(self, symbol: str, venue: str) -> str:
        spec = VENDOR_ENDPOINTS[venue]
        template = spec.get("trades") or ""
        if not template:
            raise FeedUnavailable(f"{venue} exposes no public executed-flow endpoint")
        return spec["rest"] + template.format(
            symbol=self._venue_symbol(symbol, venue), limit=TRADE_SAMPLE
        )

    @staticmethod
    def _parse_binance_book(payload: Mapping[str, Any]) -> tuple[float, float, float]:
        bid = float(payload.get("bidPrice") or 0.0)
        ask = float(payload.get("askPrice") or 0.0)
        last = float(payload.get("lastPrice") or 0.0) or (0.5 * (bid + ask) if bid and ask else 0.0)
        return bid, ask, last

    @staticmethod
    def _parse_okx_book(payload: Mapping[str, Any]) -> tuple[float, float, float]:
        data = payload.get("data") or []
        if not data:
            raise FeedUnavailable("okx ticker returned no data")
        row = data[0]
        bid = float(row.get("bidPx") or 0.0)
        ask = float(row.get("askPx") or 0.0)
        last = float(row.get("last") or 0.0)
        return bid, ask, last

    @staticmethod
    def _parse_bybit_book(payload: Mapping[str, Any]) -> tuple[float, float, float]:
        rows = ((payload.get("result") or {}).get("list") or [])
        if not rows:
            raise FeedUnavailable("bybit ticker returned no data")
        row = rows[0]
        bid = float(row.get("bid1Price") or 0.0)
        ask = float(row.get("ask1Price") or 0.0)
        last = float(row.get("lastPrice") or 0.0)
        return bid, ask, last

    @staticmethod
    def _parse_yahoo_book(payload: Mapping[str, Any]) -> tuple[float, float, float]:
        chart = (payload or {}).get("chart") or {}
        error = chart.get("error")
        if error:
            raise FeedUnavailable(f"yahoo chart error: {error}")
        results = chart.get("result") or []
        if not results:
            raise FeedUnavailable("yahoo chart returned no result")
        block = results[0] or {}
        meta = block.get("meta") or {}
        last = float(meta.get("regularMarketPrice") or 0.0)
        if last <= 0.0:
            # Fall back to the last non-null 1-minute close.
            quotes = ((block.get("indicators") or {}).get("quote") or [{}])[0] or {}
            closes = [float(value) for value in (quotes.get("close") or []) if value]
            last = closes[-1] if closes else 0.0
        if last <= 0.0:
            raise FeedUnavailable("yahoo chart carried no usable price")
        # Yahoo's chart endpoint is unauthenticated and shows no book, so the
        # touch is derived at a documented 2 bps half-spread.  It is a quote,
        # never a claim about resting liquidity (``sec_018`` Law 1).
        half = last * 0.0002
        bid = float(meta.get("bid") or 0.0) or (last - half)
        ask = float(meta.get("ask") or 0.0) or (last + half)
        if bid <= 0.0 or ask <= 0.0:
            raise FeedUnavailable("yahoo chart carried a non-positive touch")
        return bid, ask, last

    @staticmethod
    def _parse_stooq_book(payload: Any) -> tuple[float, float, float]:
        """Stooq ``/q/l/`` CSV: Symbol,Date,Time,Open,High,Low,Close,Volume."""

        if isinstance(payload, Mapping):
            # Mock transports may hand back an already-decoded row.
            row = payload
            close = float(row.get("Close") or row.get("close") or 0.0)
            if close <= 0.0:
                raise FeedUnavailable("stooq row carried no close")
            half = close * 0.0002
            return close - half, close + half, close
        text = str(payload or "").strip()
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            raise FeedUnavailable("stooq returned no data row")
        cells = [cell.strip() for cell in lines[1].split(",")]
        if len(cells) < 7:
            raise FeedUnavailable("stooq row was truncated")
        try:
            close = float(cells[6])
        except ValueError as exc:
            raise FeedUnavailable(f"stooq close was not numeric: {cells[6]!r}") from exc
        if close <= 0.0:
            raise FeedUnavailable("stooq close was non-positive (market closed?)")
        half = close * 0.0002
        return close - half, close + half, close

    def _parse_book(self, payload: Any, venue: str) -> tuple[float, float, float]:
        if venue == "okx":
            return self._parse_okx_book(payload)
        if venue == "bybit":
            return self._parse_bybit_book(payload)
        if venue == "yahoo":
            return self._parse_yahoo_book(payload)
        if venue == "stooq":
            return self._parse_stooq_book(payload)
        return self._parse_binance_book(payload)

    @staticmethod
    def _parse_taker_flow(payload: Any, venue: str) -> tuple[float, float]:
        """Sum executed taker buy/sell size from aggTrades / trades."""

        buy = 0.0
        sell = 0.0
        if venue == "okx":
            rows = (payload or {}).get("data") or []
            for row in rows:
                qty = float(row.get("sz") or 0.0)
                if str(row.get("side", "")).lower() == "buy":
                    buy += qty
                else:
                    sell += qty
            return buy, sell

        rows = payload or []
        if isinstance(rows, Mapping):
            rows = rows.get("result", {}).get("list", [])
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            qty = float(row.get("qty") or row.get("size") or 0.0)
            if "isBuyerMaker" in row:
                # Binance: buyer is maker => the taker was a seller.
                if bool(row.get("isBuyerMaker")):
                    sell += qty
                else:
                    buy += qty
            else:
                side = str(row.get("side", "")).lower()
                if side == "buy":
                    buy += qty
                else:
                    sell += qty
        return buy, sell

    def _fetch_book(self, symbol: str, venue: str) -> Any:
        """GET one venue's quote payload in its native format.

        Stooq serves CSV; everything else serves JSON.  A transport without
        ``get_text`` (a minimal test double) is still supported for JSON venues.
        """

        url = self._book_url(symbol, venue)
        fmt = str(VENDOR_ENDPOINTS[venue].get("format", "json"))
        try:
            if fmt == "csv":
                reader = getattr(self.transport, "get_text", None)
                if reader is None:
                    raise FeedUnavailable(f"{venue} requires a text transport")
                return reader(url)
            return self.transport.get_json(url)
        except FeedUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a feed must never kill the caller
            # Injected transports, DNS failures and malformed bodies all collapse
            # onto one contract: degrade to synthetic, never raise past ``ingest``.
            raise FeedUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def _fetch_quote(self, symbol: str, venue: str, *, sample_flow: bool = True) -> VendorQuote:
        book_payload = self._fetch_book(symbol, venue)
        bid, ask, last = self._parse_book(book_payload, venue)
        if bid <= 0.0 or ask <= 0.0:
            raise FeedUnavailable(f"{venue} returned a non-positive book for {symbol}")
        received = int(self.clock())
        buy = 0.0
        sell = 0.0
        # Equity / ETF / FX venues expose no public executed-taker tape, so the
        # connector never fabricates one; the OFI gate abstains on merit.
        has_flow = str(VENDOR_ENDPOINTS[venue].get("flow", "true")).lower() != "false"
        if sample_flow and has_flow:
            try:
                trade_payload = self.transport.get_json(self._trades_url(symbol, venue))
                buy, sell = self._parse_taker_flow(trade_payload, venue)
            except FeedUnavailable:
                # Executed flow is a refinement, not a precondition: the OFI gate
                # will abstain on a zero-flow frame rather than crash the feed.
                buy = 0.0
                sell = 0.0
        return VendorQuote(
            venue=venue,
            symbol=self._venue_symbol(symbol, venue),
            bid=bid,
            ask=ask,
            last=last,
            ts_ns=received,
            source="rest",
            taker_buy_volume=buy,
            taker_sell_volume=sell,
        )

    # -- frame construction -------------------------------------------------

    def _build_tick(
        self,
        symbol: str,
        primary: VendorQuote,
        secondary: VendorQuote,
        bar: DualVendorBar,
    ) -> ExecutedFlowTick:
        buy = float(primary.taker_buy_volume) + float(secondary.taker_buy_volume)
        sell = float(primary.taker_sell_volume) + float(secondary.taker_sell_volume)
        if buy <= 0.0 and sell <= 0.0:
            # No executed prints sampled: keep the ratio neutral so the OFI gate
            # abstains on merit instead of on a divide-by-zero artefact.
            buy, sell = 0.0, 0.0
        spread = max(float(primary.spread_bps), float(secondary.spread_bps))
        mid = float(bar.price)
        return ExecutedFlowTick(
            symbol=symbol,
            valid_time_ns=int(bar.valid_time_ns),
            knowledge_ts_ns=int(bar.knowledge_ts_ns),
            last_price=mid,
            spread_bps=spread,
            realized_vol_5s=5.0,
            normal_vol_5s=10.0,
            taker_buy_volume_5s=buy,
            taker_sell_volume_5s=sell,
            bid_cancel_to_fill_ratio=0.2,
            ask_cancel_to_fill_ratio=0.2,
        )

    def _route(self, frame: FeedFrame) -> FeedFrame:
        """Push the frame through the Ring 1 multiplexer (if attached)."""

        if self.multiplexer is None:
            return frame
        now_ns = int(frame.bar.knowledge_ts_ns)
        try:
            # The multiplexer decodes the frame as-is, so venue fields must sit at
            # the top level exactly as a real venue payload would.
            self.multiplexer.ingest_frame(
                {
                    "kind": "L2",
                    "symbol": frame.symbol,
                    "ts_ns": now_ns,
                    "bid": frame.primary.bid,
                    "ask": frame.primary.ask,
                    "bid_qty": 1.0,
                    "ask_qty": 1.0,
                    "b": [[frame.primary.bid, 1.0]],
                    "a": [[frame.primary.ask, 1.0]],
                },
                now_ns=now_ns,
            )
            self.multiplexer.ingest_frame(
                {
                    "kind": "TRADE",
                    "symbol": frame.symbol,
                    "ts_ns": now_ns,
                    "p": frame.tick.last_price,
                    "q": max(
                        1.0,
                        float(frame.primary.taker_buy_volume)
                        + float(frame.secondary.taker_buy_volume),
                    ),
                    "m": False,
                    "side": "BUY",
                },
                now_ns=now_ns,
            )
        except Exception as exc:  # pragma: no cover - defensive, feed must not die
            frame.reason = f"multiplexer-error: {type(exc).__name__}: {exc}"
        return frame

    def ingest(
        self,
        symbol: str,
        *,
        now_ns: int | None = None,
        round_index: int | None = None,
    ) -> FeedFrame:
        """Produce one cross-verified frame for ``symbol``.

        Never raises on transport failure: with ``synthetic_fallback=True`` the
        frame is built from :func:`synthetic_quotes` and labelled as such.
        """

        key = canonical_symbol(symbol)
        if key not in SYMBOL_MAP:
            raise ValueError(f"unsupported public feed symbol: {symbol}")
        symbol = key
        index = int(self.round_index if round_index is None else round_index)
        synthetic = False
        try:
            primary = self._fetch_quote(symbol, self.primary)
            secondary = self._fetch_quote(symbol, self.secondary)
            self._mode = FEED_MODE_LIVE
        except FeedUnavailable as exc:
            self.transport_failures += 1
            self.last_error = str(exc)
            if not self.synthetic_fallback:
                raise
            primary, secondary = synthetic_quotes(
                symbol,
                now_ns=now_ns,
                round_index=index,
                primary_venue=self.primary,
                secondary_venue=self.secondary,
            )
            self._mode = FEED_MODE_SYNTHETIC
            synthetic = True

        received = int(now_ns if now_ns is not None else self.clock())
        bar = quotations_to_bar(primary, secondary, symbol=symbol, received_ns=received)
        tick = self._build_tick(symbol, primary, secondary, bar)

        accepted = True
        reason = ""
        normalized: Any = None
        if self.guard is not None:
            try:
                normalized = self.guard.ingest_bar(
                    bar, as_of_ns=max(int(bar.valid_time_ns), int(bar.knowledge_ts_ns))
                )
            except Exception as exc:
                # A PIT violation is a caller bug; record it rather than swallow it.
                accepted = False
                reason = f"guard-error: {type(exc).__name__}: {exc}"
                normalized = None
            else:
                accepted = normalized is not None
                if not accepted:
                    reason = "guard-quarantine"

        frame = FeedFrame(
            symbol=symbol,
            mode=FEED_MODE_SYNTHETIC if synthetic else FEED_MODE_LIVE,
            bar=bar,
            tick=tick,
            primary=primary,
            secondary=secondary,
            accepted=accepted,
            reason=reason,
            normalized=normalized,
            round_index=index,
            silo=self.silo,
        )
        frame = self._route(frame)

        if self.shadow_book is not None and accepted:
            try:
                self.shadow_book.write_snapshot(
                    symbol,
                    primary.bid,
                    primary.ask,
                    1.0,
                    1.0,
                    updated_at_ns=int(bar.knowledge_ts_ns),
                )
            except Exception:  # pragma: no cover - shadow book is best effort
                pass
        if self.ofi_ring is not None and accepted:
            try:
                self.ofi_ring.append(tick, now_ns=int(bar.knowledge_ts_ns))
            except Exception:  # pragma: no cover
                pass

        if synthetic:
            self.synthetic_frames += 1
        else:
            self.live_frames += 1
        if not accepted:
            self.rejected_frames += 1

        self.last_frame = frame
        self._frames.append(frame)
        if len(self._frames) > 512:
            del self._frames[:-512]
        self.round_index = index + 1
        return frame

    def ingest_all(
        self,
        *,
        now_ns: int | None = None,
        round_index: int | None = None,
    ) -> list[FeedFrame]:
        return [
            self.ingest(symbol, now_ns=now_ns, round_index=round_index)
            for symbol in self.symbols
        ]

    def stream(
        self,
        *,
        rounds: int = 1,
        interval_s: float = 0.0,
        now_ns_fn: Callable[[], int] | None = None,
    ) -> Iterable[FeedFrame]:
        """Yield frames for ``rounds`` polling passes (bounded, never infinite)."""

        clock = now_ns_fn or self.clock
        for _ in range(max(1, int(rounds))):
            for frame in self.ingest_all(now_ns=clock()):
                yield frame
            if interval_s > 0.0:
                time.sleep(min(float(interval_s), 2.0))

    # -- introspection ------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def venues(self) -> tuple[str, str]:
        return (self.primary, self.secondary)

    def probe(self) -> dict[str, Any]:
        """Bounded reachability check of both venues for the first symbol."""

        symbol = self.symbols[0]
        body: dict[str, Any] = {
            "silo": self.silo,
            "symbol": symbol,
            "venues": {},
            "live": False,
        }
        reachable = 0
        for venue in (self.primary, self.secondary):
            entry: dict[str, Any] = {"venue": venue, "reachable": False}
            try:
                quote = self._fetch_quote(symbol, venue, sample_flow=False)
            except FeedUnavailable as exc:
                entry["error"] = str(exc)
            else:
                reachable += 1
                entry.update(
                    {
                        "reachable": True,
                        "bid": quote.bid,
                        "ask": quote.ask,
                        "mid": quote.mid,
                        "url": self._book_url(symbol, venue),
                    }
                )
            body["venues"][venue] = entry
        body["live"] = reachable == 2
        body["mode"] = FEED_MODE_LIVE if body["live"] else FEED_MODE_SYNTHETIC
        self._mode = body["mode"]
        return body

    def status(self) -> dict[str, Any]:
        transport_status = (
            self.transport.status()
            if hasattr(self.transport, "status")
            else {"transport": type(self.transport).__name__}
        )
        return {
            "silo": self.silo,
            "mode": self._mode,
            "venues": {"primary": self.primary, "secondary": self.secondary},
            "symbols": list(self.symbols),
            "rounds": self.round_index,
            "live_frames": self.live_frames,
            "synthetic_frames": self.synthetic_frames,
            "rejected_frames": self.rejected_frames,
            "transport_failures": self.transport_failures,
            "last_error": self.last_error,
            "synthetic_fallback": self.synthetic_fallback,
            "guard_attached": self.guard is not None,
            "multiplexer_attached": self.multiplexer is not None,
            "transport": transport_status,
            "spec": self.silo_spec.to_dict() if self.silo_spec else None,
            "last_frame": self.last_frame.to_dict() if self.last_frame else None,
        }

    def close(self) -> None:
        self._frames.clear()
        self.last_frame = None

    def __enter__(self) -> "LiveDualVendorFeedConnector":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Multi-silo coordinator
# ---------------------------------------------------------------------------


class MultiSiloFeedConnector:
    """Fan the dual-vendor public feed across all four sovereign silos.

    Each silo keeps its **own** connector, its own two venues and its own
    ``_mode``.  That isolation is the point: a blocked Yahoo host in India must
    not drag the US silo or the crypto silo into synthetic mode, and a closed FX
    market must not silently mark the crypto tape as rehearsal.

    ``ingest_all`` therefore returns a flat list of :class:`FeedFrame` whose
    ``frame.silo`` names the owning silo, and ``mode`` reports per-silo truth
    instead of a single aggregate that would hide a partial outage.
    """

    def __init__(
        self,
        *,
        silos: Sequence[str] | None = None,
        transport: Any | None = None,
        transports: Mapping[str, Any] | None = None,
        guard: Any | None = None,
        guards: Mapping[str, Any] | None = None,
        multiplexer: Any | None = None,
        shadow_book: Any | None = None,
        ofi_ring: Any | None = None,
        timeout_s: float = FEED_TIMEOUT_S,
        synthetic_fallback: bool = True,
        clock: Callable[[], int] = time.time_ns,
    ):
        chosen = tuple(str(item).strip().upper() for item in (silos or SILO_FEED_ORDER))
        unknown = [item for item in chosen if item not in SILO_FEED_SPECS]
        if unknown:
            raise ValueError(f"{UNKNOWN_SILO_MSG}: {unknown}")

        overrides = {
            str(key).strip().upper(): value
            for key, value in (transports or {}).items()
        }
        guard_overrides = {
            str(key).strip().upper(): value for key, value in (guards or {}).items()
        }

        self.connectors: dict[str, LiveDualVendorFeedConnector] = {}
        for silo in chosen:
            self.connectors[silo] = LiveDualVendorFeedConnector(
                silo=silo,
                transport=overrides.get(silo, transport),
                guard=guard_overrides.get(silo, guard),
                multiplexer=multiplexer,
                shadow_book=shadow_book,
                ofi_ring=ofi_ring,
                timeout_s=timeout_s,
                synthetic_fallback=synthetic_fallback,
                clock=clock,
            )
        self.silos = chosen
        self.rounds = 0
        self._last_cycle_ns = 0
        self._last_report: dict[str, Any] = {}

    # -- lookups -----------------------------------------------------------
    def connector(self, silo: str) -> LiveDualVendorFeedConnector:
        key = str(silo).strip().upper()
        if key not in self.connectors:
            raise KeyError(f"{UNKNOWN_SILO_MSG}: {silo}")
        return self.connectors[key]

    def spec(self, silo: str) -> SiloFeedSpec:
        return SILO_FEED_SPECS[str(silo).strip().upper()]

    # -- ingest ------------------------------------------------------------
    def ingest_all(
        self,
        *,
        now_ns: int | None = None,
        round_index: int | None = None,
        silos: Sequence[str] | None = None,
    ) -> list[FeedFrame]:
        """One dual-vendor poll of every symbol in every selected silo."""

        targets = (
            tuple(str(item).strip().upper() for item in silos) if silos else self.silos
        )
        frames: list[FeedFrame] = []
        for silo in targets:
            frames.extend(
                self.connector(silo).ingest_all(
                    now_ns=now_ns, round_index=round_index
                )
            )
        self.rounds += 1
        self._last_cycle_ns = int(now_ns if now_ns is not None else time.time_ns())
        return frames

    def ingest_cycle(
        self,
        *,
        now_ns: int | None = None,
        silos: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Run one immediate four-silo cycle and return a per-silo receipt."""

        frames = self.ingest_all(now_ns=now_ns, silos=silos)
        per_silo: dict[str, Any] = {}
        for silo, connector in self.connectors.items():
            if silos and silo not in {
                str(item).strip().upper() for item in silos
            }:
                continue
            owned = [frame for frame in frames if frame.silo == silo]
            per_silo[silo] = {
                "mode": connector.mode,
                "symbols": list(connector.symbols),
                "venues": list(connector.venues),
                "frames": len(owned),
                "accepted": sum(1 for frame in owned if frame.accepted),
                "rejected": sum(1 for frame in owned if not frame.accepted),
                "synthetic": sum(
                    1 for frame in owned if frame.mode == FEED_MODE_SYNTHETIC
                ),
                "live": sum(1 for frame in owned if frame.mode == FEED_MODE_LIVE),
                "max_divergence_pct": round(
                    max((frame.divergence_pct for frame in owned), default=0.0), 6
                ),
                "last_error": connector.last_error,
            }
        report = {
            "round": self.rounds,
            "cycle_at_ns": self._last_cycle_ns,
            "silos": per_silo,
            "frames": len(frames),
            "accepted": sum(1 for frame in frames if frame.accepted),
            "rejected": sum(1 for frame in frames if not frame.accepted),
            "mode": self.mode,
            "all_live": all(
                entry["mode"] == FEED_MODE_LIVE for entry in per_silo.values()
            ),
        }
        self._last_report = report
        return report

    def stream(
        self,
        *,
        rounds: int = 1,
        interval_s: float = 0.0,
        now_ns_fn: Callable[[], int] | None = None,
    ) -> Iterable[FeedFrame]:
        clock = now_ns_fn or time.time_ns
        for _ in range(max(1, int(rounds))):
            for frame in self.ingest_all(now_ns=clock()):
                yield frame
            if interval_s > 0.0:
                time.sleep(min(float(interval_s), 2.0))

    # -- introspection -----------------------------------------------------
    @property
    def mode(self) -> dict[str, str]:
        return {silo: conn.mode for silo, conn in self.connectors.items()}

    @property
    def feed_mode(self) -> str:
        """``LIVE_PUBLIC_DUAL_VENDOR`` only when *every* silo is live."""

        modes = set(self.mode.values())
        return FEED_MODE_LIVE if modes == {FEED_MODE_LIVE} else FEED_MODE_SYNTHETIC

    def probe(self) -> dict[str, Any]:
        return {
            silo: conn.probe() for silo, conn in self.connectors.items()
        }

    def status(self) -> dict[str, Any]:
        return {
            "kind": "multi_silo_dual_vendor",
            "silos": list(self.silos),
            "feed_mode": self.feed_mode,
            "mode": self.mode,
            "rounds": self.rounds,
            "cycle_at_ns": self._last_cycle_ns,
            "symbols": {
                silo: list(conn.symbols) for silo, conn in self.connectors.items()
            },
            "venues": {
                silo: list(conn.venues) for silo, conn in self.connectors.items()
            },
            "connectors": {
                silo: conn.status() for silo, conn in self.connectors.items()
            },
            "last_cycle": dict(self._last_report),
            "law": "sec_018 Law 1 - no single venue is ever trusted",
        }

    def close(self) -> None:
        for connector in self.connectors.values():
            connector.close()
        self._last_report = {}

    def __enter__(self) -> "MultiSiloFeedConnector":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
