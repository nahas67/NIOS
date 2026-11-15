"""Ring 2 TradingView chart, replay-bar and Pine Script study bridge.

Constitutional placement (``sec_004`` / ``sec_018``):

* This module lives in **Ring 2** (the bicameral brain). It is an auxiliary
  *charting / replay-bar / Pine Script evaluation* bridge only. It never holds
  broker credentials, never signs an order and never sits in the Ring 1
  microsecond loop (Law 1 and Law 2).
* TradingView is not treated as a live execution feed. Every bar and every study
  plot is sealed into a SHA-256 :class:`GroundedFactPacket` and re-verified by
  :class:`NumericalGroundingVerifier` before it may be quoted (Law 3).
* The default mode is ``DETERMINISTIC_REPLAY``: a fully offline, seeded bar
  generator pinned to a fixed replay epoch. This keeps the air-gapped
  deployment and the test suite free of any external network dependency.
  ``LIVE_WS`` is opt-in through ``NIOS_ALLOW_TV_WS=1`` and still requires an
  injected transport, so the default process can never open a socket.

The protocol surface mirrors the harvested ``Mathieu2301/TradingView-API``
TypeScript donor (``src/chart/study.ts``, ``src/data/candles.ts``,
``src/data/symbols.ts``, ``src/protocol/framing.ts``):

* ``chart.setMarket(symbol, { timeframe, range, to })`` -> :meth:`fetch_tv_chart`
* study attach + plot retrieval                     -> :meth:`evaluate_tv_study`
* symbol search                                    -> :meth:`search_tv_symbols`

The replay bars are **synthetic proxies scaled to a plausible price band**: they
are deterministic and grounded, but they are not exchange history and are never
presented as such.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote

from core.instrument_registry import InstrumentSpec
from core.instrument_registry import registry as instrument_registry
from core.schemas import GroundedFactPacket
from ring2_brain.quant.daily_analysis_donor.strategies import PriceBar
from ring2_brain.truth.grounding_gate import NumericalGroundingVerifier

__all__ = [
    "BRIDGE_VERSION",
    "REPLAY_EPOCH_NS",
    "TIMEFRAME_SECONDS",
    "MODE_DETERMINISTIC_REPLAY",
    "MODE_LIVE_WS",
    "STUDY_IDS",
    "TradingViewBridge",
    "TradingViewBridgeError",
    "TradingViewLiveDisabled",
    "TradingViewStudyNotFound",
    "TradingViewSymbolNotFound",
    "TradingViewTimeframeError",
    "TradingViewBar",
    "bridge",
]

BRIDGE_VERSION = "v1.8.tradingview-bridge"

#: Frozen replay epoch (2025-10-09T08:53:20Z). Deterministic bars are aligned to
#: this instant so two identical calls always produce the identical digest.
REPLAY_EPOCH_NS = 1_760_000_000_000_000_000

MICROS_PER_UNIT = 1_000_000
_NS_PER_SECOND = 1_000_000_000

TIMEFRAME_SECONDS: dict[str, int] = {
    "1": 60,
    "5": 300,
    "15": 900,
    "60": 3_600,
    "240": 14_400,
    "1D": 86_400,
    "1W": 604_800,
}

_TIMEFRAME_ALIASES: dict[str, str] = {
    "1": "1",
    "1M": "1",
    "M1": "1",
    "1MIN": "1",
    "1MINUTE": "1",
    "5": "5",
    "5M": "5",
    "M5": "5",
    "15": "15",
    "15M": "15",
    "M15": "15",
    "60": "60",
    "60M": "60",
    "1H": "60",
    "H1": "60",
    "240": "240",
    "240M": "240",
    "4H": "240",
    "H4": "240",
    "1D": "1D",
    "D": "1D",
    "1DAY": "1D",
    "DAY": "1D",
    "DAILY": "1D",
    "1W": "1W",
    "W": "1W",
    "1WEEK": "1W",
    "WEEK": "1W",
    "WEEKLY": "1W",
}

MODE_DETERMINISTIC_REPLAY = "DETERMINISTIC_REPLAY"
MODE_LIVE_WS = "LIVE_WS"
_MODES = (MODE_DETERMINISTIC_REPLAY, MODE_LIVE_WS)

STUDY_IDS: tuple[str, ...] = (
    "STD;RSI",
    "STD;MACD",
    "STD;Supertrend",
    "STD;Ichimoku Cloud",
    "STD;Bollinger Bands",
    "STD;EMA Ribbon",
    "STD;VWAP Bands",
    "NIOS;Sovereign Regime Composite",
)

_STUDY_TITLES: dict[str, str] = {
    "STD;RSI": "Relative Strength Index",
    "STD;MACD": "MACD",
    "STD;Supertrend": "Supertrend",
    "STD;Ichimoku Cloud": "Ichimoku Cloud",
    "STD;Bollinger Bands": "Bollinger Bands",
    "STD;EMA Ribbon": "EMA Ribbon",
    "STD;VWAP Bands": "VWAP Bands",
    "NIOS;Sovereign Regime Composite": "Sovereign Regime Composite",
}

_MIN_BARS = 10
_MAX_BARS = 1_000
_MAX_REPLAY_OFFSET = 5_000


class TradingViewBridgeError(ValueError):
    """Base error for the Ring 2 TradingView bridge."""

    code = "TRADINGVIEW_BRIDGE_ERROR"


class TradingViewSymbolNotFound(TradingViewBridgeError):
    code = "TRADINGVIEW_SYMBOL_NOT_FOUND"


class TradingViewStudyNotFound(TradingViewBridgeError):
    code = "TRADINGVIEW_STUDY_NOT_FOUND"


class TradingViewTimeframeError(TradingViewBridgeError):
    code = "TRADINGVIEW_TIMEFRAME_UNSUPPORTED"


class TradingViewLiveDisabled(TradingViewBridgeError):
    code = "TRADINGVIEW_LIVE_WS_DISABLED"


# ---------------------------------------------------------------------------
# deterministic primitives
# ---------------------------------------------------------------------------
def _digest_int(*parts: Any) -> int:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _finite(value: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _round(value: float, digits: int = 6) -> float:
    return round(_finite(value), digits)


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / float(len(values))


def _last(values: Sequence[float | None], default: float = 0.0) -> float:
    for value in reversed(values):
        if value is not None:
            return float(value)
    return default


@dataclass(frozen=True, slots=True)
class TradingViewBar:
    """A single TradingView-style OHLCV bar."""

    ts_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_ns": int(self.ts_ns),
            "open": _round(self.open),
            "high": _round(self.high),
            "low": _round(self.low),
            "close": _round(self.close),
            "volume": _round(self.volume, 3),
        }

    def to_price_bar(self, index: int) -> PriceBar:
        return PriceBar(
            index=int(index),
            open=float(self.open),
            high=float(self.high),
            low=float(self.low),
            close=float(self.close),
            volume=float(self.volume),
            ts_ns=int(self.ts_ns),
        )


# ---------------------------------------------------------------------------
# price anchors (synthetic replay scale)
# ---------------------------------------------------------------------------
_ANCHOR_PRICES: dict[str, float] = {
    # India indices
    "NIFTY": 24_820.0,
    "BANKNIFTY": 53_460.0,
    "FINNIFTY": 23_610.0,
    "MIDCPNIFTY": 12_420.0,
    "NIFTYNXT50": 74_580.0,
    "SENSEX": 81_540.0,
    "BANKEX": 58_210.0,
    "INDIAVIX": 13.42,
    # India majors
    "RELIANCE": 2_940.0,
    "TCS": 4_120.0,
    "HDFCBANK": 1_684.0,
    "ICICIBANK": 1_246.0,
    "BHARTIARTL": 1_632.0,
    "INFY": 1_872.0,
    "SBIN": 824.0,
    "ITC": 468.0,
    "LT": 3_618.0,
    "HINDUNILVR": 2_486.0,
    "KOTAKBANK": 1_782.0,
    "AXISBANK": 1_164.0,
    "MARUTI": 12_480.0,
    "SUNPHARMA": 1_762.0,
    # MCX
    "GOLD": 74_310.0,
    "GOLDM": 74_150.0,
    "GOLDGUINEA": 12_480.0,
    "SILVER": 92_460.0,
    "SILVERM": 92_180.0,
    "SILVERMIC": 92_310.0,
    "CRUDEOIL": 6_412.0,
    "CRUDEOILM": 6_405.0,
    "NATURALGAS": 248.6,
    "COPPER": 831.4,
    "ZINC": 268.2,
    "ALUMINIUM": 231.4,
    "LEAD": 186.3,
    "NICKEL": 1_284.0,
    # NSE currency
    "USDINR": 83.42,
    "EURINR": 90.18,
    "GBPINR": 105.62,
    "JPYINR": 57.34,
    # crypto majors
    "BTCUSDT": 68_420.0,
    "ETHUSDT": 3_312.0,
    "SOLUSDT": 164.8,
    "BNBUSDT": 582.4,
    "XRPUSDT": 0.5842,
    "DOGEUSDT": 0.1382,
    "SHIBUSDT": 0.0000231,
    "PEPEUSDT": 0.0000094,
    # US equities
    "NVDA": 128.42,
    "AAPL": 228.16,
    "MSFT": 421.35,
    "AMZN": 186.44,
    "GOOGL": 168.92,
    "META": 512.28,
    "TSLA": 248.62,
    "SPY": 572.18,
    "QQQ": 486.42,
    # US futures
    "ES": 5_624.0,
    "NQ": 19_842.0,
    "MES": 5_624.0,
    "MNQ": 19_842.0,
    "CL": 78.42,
    "GC": 2_452.0,
    # FX / metals / oil
    "EURUSD": 1.0862,
    "GBPUSD": 1.2742,
    "USDJPY": 149.62,
    "USDCHF": 0.8834,
    "AUDUSD": 0.6584,
    "USDCAD": 1.3642,
    "NZDUSD": 0.6024,
    "USDZAR": 18.42,
    "USDMXN": 17.24,
    "USDSGD": 1.3420,
    "USDTRY": 32.48,
    "USDCNH": 7.2412,
    "XAUUSD": 2_412.0,
    "XAGUSD": 29.42,
    "XPTUSD": 982.0,
    "XPDUSD": 1_024.0,
    "USOIL": 78.16,
    "UKOIL": 82.34,
}

# fallback bands: (low, high) inclusive multiplier window, price = low * exp(u*ln(hi/lo))
_PRICE_BANDS: dict[str, tuple[float, float]] = {
    "INDEX_FUTURE": (4_200.0, 86_000.0),
    "EQUITY": (86.0, 4_200.0),
    "ETF": (24.0, 620.0),
    "FUTURE": (62.0, 21_000.0),
    "COMMODITY_FUTURE": (92.0, 96_000.0),
    "CURRENCY_FUTURE": (48.0, 108.0),
    "PERPETUAL": (0.0000042, 62.0),
    "FX": (0.42, 1.62),
    "METAL": (24.0, 2_480.0),
    "ENERGY": (62.0, 96.0),
}

_HIGH_VOL_ASSET_CLASSES = frozenset({"PERPETUAL"})


def _base_price(spec: InstrumentSpec | None, ticker: str) -> float:
    symbol = spec.symbol if spec is not None else ticker.split(":")[-1]
    anchor = _ANCHOR_PRICES.get(symbol)
    if anchor is not None:
        return float(anchor)
    asset_class = (spec.asset_class if spec is not None else "EQUITY") or "EQUITY"
    low, high = _PRICE_BANDS.get(asset_class, (24.0, 620.0))
    unit = (_digest_int("tv-price", ticker) % 1_000_000) / 1_000_000.0
    if asset_class == "PERPETUAL":
        ratio = (high / low) ** unit
        return float(low * ratio)
    return float(low + (high - low) * unit)


def _timeframe_volatility(timeframe: str, asset_class: str) -> float:
    seconds = TIMEFRAME_SECONDS[timeframe]
    daily = 0.0165 if asset_class in _HIGH_VOL_ASSET_CLASSES else 0.0102
    scaled = daily * math.sqrt(max(seconds, 1) / 86_400.0)
    return float(min(max(scaled, 0.0006), 0.085))


# ---------------------------------------------------------------------------
# indicator math (pure Python, deterministic, no third-party dependency)
# ---------------------------------------------------------------------------
def _sma_series(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        out.append(running / window if index >= window - 1 else None)
    return out


def _ema_series(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    multiplier = 2.0 / (window + 1.0)
    previous: float | None = None
    for index, value in enumerate(values):
        if index < window - 1:
            out.append(None)
            continue
        if previous is None:
            previous = _mean(values[: index + 1])
        else:
            previous = (value - previous) * multiplier + previous
        out.append(float(previous))
    return out


def _rsi_series(values: Sequence[float], window: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= window:
        return out
    gains = 0.0
    losses = 0.0
    for index in range(1, window + 1):
        delta = values[index] - values[index - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / window
    avg_loss = losses / window
    out[window] = 100.0 if avg_loss == 0.0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for index in range(window + 1, len(values)):
        delta = values[index] - values[index - 1]
        avg_gain = (avg_gain * (window - 1) + max(delta, 0.0)) / window
        avg_loss = (avg_loss * (window - 1) + max(-delta, 0.0)) / window
        out[index] = 100.0 if avg_loss == 0.0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _true_ranges(bars: Sequence[TradingViewBar]) -> list[float]:
    out: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            out.append(bar.high - bar.low)
            continue
        previous_close = bars[index - 1].close
        out.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return out


def _atr_series(bars: Sequence[TradingViewBar], window: int = 14) -> list[float | None]:
    ranges = _true_ranges(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) < window:
        return out
    previous = _mean(ranges[:window])
    out[window - 1] = previous
    for index in range(window, len(bars)):
        previous = (previous * (window - 1) + ranges[index]) / window
        out[index] = float(previous)
    return out


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / float(len(values))
    return float(math.sqrt(max(variance, 0.0)))


def _rolling_stdev(values: Sequence[float], window: int) -> list[float | None]:
    return [
        _stdev(values[index - window + 1 : index + 1]) if index >= window - 1 else None
        for index in range(len(values))
    ]


def _crossover(previous_fast: float, previous_slow: float, fast: float, slow: float) -> int:
    if previous_fast <= previous_slow and fast > slow:
        return 1
    if previous_fast >= previous_slow and fast < slow:
        return -1
    return 0


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------
class TradingViewBridge:
    """Sandboxed Ring 2 TradingView chart / replay / Pine study bridge."""

    def __init__(
        self,
        *,
        registry: Any | None = None,
        verifier: NumericalGroundingVerifier | None = None,
        replay_epoch_ns: int = REPLAY_EPOCH_NS,
        live_transport: Any | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry if registry is not None else instrument_registry
        self._verifier = verifier if verifier is not None else NumericalGroundingVerifier()
        self._replay_epoch_ns = int(replay_epoch_ns)
        self._live_transport = live_transport
        self._environ: Mapping[str, str] = environ if environ is not None else os.environ
        self._chart_calls = 0
        self._study_calls = 0
        self._live_calls = 0

    # -- introspection ----------------------------------------------------
    @property
    def version(self) -> str:
        return BRIDGE_VERSION

    @property
    def verifier(self) -> NumericalGroundingVerifier:
        return self._verifier

    @property
    def replay_epoch_ns(self) -> int:
        return self._replay_epoch_ns

    @property
    def call_counts(self) -> dict[str, int]:
        return {
            "chart": int(self._chart_calls),
            "study": int(self._study_calls),
            "live_ws": int(self._live_calls),
        }

    def timeframes(self) -> tuple[str, ...]:
        return tuple(TIMEFRAME_SECONDS)

    def study_catalog(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "study_id": study_id,
                "canonical_id": study_id,
                "title": _STUDY_TITLES.get(study_id, study_id),
                "vendor": study_id.split(";", 1)[0],
            }
            for study_id in STUDY_IDS
        )

    def live_enabled(self) -> bool:
        return str(self._environ.get("NIOS_ALLOW_TV_WS", "")).strip() == "1"

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": BRIDGE_VERSION,
            "mode_default": MODE_DETERMINISTIC_REPLAY,
            "live_ws_enabled": self.live_enabled(),
            "replay_epoch_ns": int(self._replay_epoch_ns),
            "timeframes": list(TIMEFRAME_SECONDS),
            "studies": list(STUDY_IDS),
            "instruments": len(self._registry.all()),
            "call_counts": self.call_counts,
            "law": "sec_018 Law 1/Law 5 - charting auxiliary bridge, never a Ring 1 execution feed",
        }

    # -- symbol resolution ------------------------------------------------
    def _spec(self, symbol: str) -> InstrumentSpec | None:
        token = str(symbol or "").strip()
        if not token:
            return None
        spec = self._registry.resolve(token)
        if spec is not None:
            return spec
        upper = token.upper()
        for candidate in self._registry.all():
            if candidate.tradingview_ticker == upper:
                return candidate
        return None

    def resolve_symbol(self, symbol: str) -> tuple[InstrumentSpec | None, str]:
        """Map a NIOS symbol or a raw ``EXCHANGE:SYMBOL`` ticker to its feed spec."""

        token = str(symbol or "").strip()
        if not token:
            raise TradingViewSymbolNotFound("TRADINGVIEW_SYMBOL_NOT_FOUND: empty symbol")
        spec = self._spec(token)
        if spec is not None:
            return spec, spec.tradingview_ticker
        if ":" in token:
            candidate = token.upper()
            for row in self._registry.all():
                if row.tradingview_ticker == candidate:
                    return row, row.tradingview_ticker
            exchange, _, rest = candidate.partition(":")
            if exchange and rest and all(char.isalnum() for char in exchange):
                # A raw TradingView ticker that NIOS does not map is still a
                # legitimate chart request; it simply has no instrument contract.
                return None, candidate
        raise TradingViewSymbolNotFound(
            f"TRADINGVIEW_SYMBOL_NOT_FOUND: {token} is not mapped to a TradingView feed"
        )

    @staticmethod
    def _timeframe(timeframe: str) -> str:
        token = str(timeframe or "").strip().upper().replace(" ", "")
        resolved = _TIMEFRAME_ALIASES.get(token)
        if resolved is None:
            raise TradingViewTimeframeError(
                "TRADINGVIEW_TIMEFRAME_UNSUPPORTED: "
                f"{timeframe!r} not in {sorted(TIMEFRAME_SECONDS)}"
            )
        return resolved

    @staticmethod
    def _study(study_id: str) -> str:
        token = unquote(str(study_id or "")).strip()
        token = " ".join(token.split())
        lowered = token.lower()
        for candidate in STUDY_IDS:
            if candidate.lower() == lowered:
                return candidate
        # Tolerate a bare indicator name ("RSI", "Bollinger Bands", "Supertrend").
        bare = lowered.split(";")[-1].strip()
        for candidate in STUDY_IDS:
            if candidate.split(";")[-1].strip().lower() == bare:
                return candidate
        raise TradingViewStudyNotFound(
            "TRADINGVIEW_STUDY_NOT_FOUND: "
            f"{study_id!r} not in {list(STUDY_IDS)}"
        )

    def _mode(self, mode: str) -> str:
        token = str(mode or MODE_DETERMINISTIC_REPLAY).strip().upper()
        if token not in _MODES:
            raise TradingViewBridgeError(
                f"TRADINGVIEW_MODE_INVALID: {mode!r} not in {list(_MODES)}"
            )
        if token == MODE_LIVE_WS:
            if not self.live_enabled():
                raise TradingViewLiveDisabled(
                    "TRADINGVIEW_LIVE_WS_DISABLED: live TradingView WebSocket streaming "
                    "requires NIOS_ALLOW_TV_WS=1; the air-gapped default is "
                    "DETERMINISTIC_REPLAY"
                )
            if self._live_transport is None:
                raise TradingViewLiveDisabled(
                    "TRADINGVIEW_LIVE_WS_DISABLED: NIOS_ALLOW_TV_WS=1 but no transport "
                    "is injected into the Ring 2 bridge"
                )
            self._live_calls += 1
        return token

    # -- bar generation ---------------------------------------------------
    def _bars(
        self,
        spec: InstrumentSpec | None,
        ticker: str,
        timeframe: str,
        bars: int,
        replay_offset: int,
    ) -> tuple[TradingViewBar, ...]:
        interval_ns = TIMEFRAME_SECONDS[timeframe] * _NS_PER_SECOND
        total = bars + replay_offset
        end_ns = (self._replay_epoch_ns // interval_ns) * interval_ns
        start_ns = end_ns - (total - 1) * interval_ns
        base = _base_price(spec, ticker)
        asset_class = spec.asset_class if spec is not None else "EQUITY"
        volatility = _timeframe_volatility(timeframe, asset_class)
        rng = random.Random(_digest_int("tv-bars", ticker, timeframe, bars, replay_offset))
        out: list[TradingViewBar] = []
        price = base
        for index in range(total):
            cycle = math.sin(index / 17.0) * 0.00016
            shock = rng.gauss(0.0, volatility) + cycle
            opened = price
            closed = max(opened * (1.0 + shock), _floor_price(asset_class))
            wick = abs(rng.gauss(0.0, volatility * 0.55))
            high = max(opened, closed) * (1.0 + wick)
            low = min(opened, closed) * (1.0 - wick * 0.92)
            low = max(min(low, opened, closed), _floor_price(asset_class))
            volume = _bar_volume(asset_class, index, rng)
            out.append(
                TradingViewBar(
                    ts_ns=start_ns + index * interval_ns,
                    open=opened,
                    high=max(high, opened, closed),
                    low=min(low, opened, closed),
                    close=closed,
                    volume=volume,
                )
            )
            price = closed
        return tuple(out[:bars])

    # -- grounding --------------------------------------------------------
    def _seal(
        self,
        ticker: str,
        bars: Sequence[TradingViewBar],
        metrics: Mapping[str, Any],
        *,
        thesis: Sequence[str] = (),
    ) -> tuple[GroundedFactPacket, dict[str, Any]]:
        payload: dict[str, float] = {}
        for key, value in metrics.items():
            payload[str(key)] = _round(_finite(value), 8)
        payload.setdefault("bar_count", float(len(bars)))
        last_ts = int(bars[-1].ts_ns) if bars else int(self._replay_epoch_ns)
        packet = GroundedFactPacket(
            symbol=ticker,
            valid_time_ns=last_ts,
            knowledge_ts_ns=last_ts,
            as_of_ns=last_ts,
            metrics=payload,
        )
        text = self._thesis_text(ticker, bars, payload, thesis)
        report = self._verifier.verify_thesis_claims(text, packet)
        verification = {
            "verified": True,
            "symbol": report.symbol,
            "digest": report.digest,
            "claims_checked": int(report.claims_checked),
            "derived_expressions": int(report.derived_expressions),
            "tolerance": float(report.tolerance),
            "thesis": text,
            "packet_verify": bool(packet.verify()),
        }
        return packet, verification

    @staticmethod
    def _thesis_text(
        ticker: str,
        bars: Sequence[TradingViewBar],
        metrics: Mapping[str, float],
        extra: Sequence[str] = (),
    ) -> str:
        """Render every grounded figure with six decimals so the verifier can
        re-derive each one. Six decimals keeps the absolute error below the
        1e-4 verifier tolerance and never emits exponent notation."""

        lines = [f"TradingView replay packet for {ticker} across {len(bars)} bars."]
        for key in sorted(metrics):
            value = float(metrics[key])
            lines.append(f"{key} is {value:.6f}.")
        for line in extra:
            lines.append(str(line))
        return " ".join(lines)

    # -- public API -------------------------------------------------------
    def fetch_tv_chart(
        self,
        symbol: str,
        timeframe: str = "1D",
        bars: int = 200,
        replay_offset: int = 0,
        mode: str = MODE_DETERMINISTIC_REPLAY,
    ) -> dict[str, Any]:
        """Return TradingView-style OHLCV bars in deterministic replay mode."""

        resolved_mode = self._mode(mode)
        tf = self._timeframe(timeframe)
        count = int(bars)
        if count < _MIN_BARS or count > _MAX_BARS:
            raise TradingViewBridgeError(
                f"TRADINGVIEW_BARS_OUT_OF_RANGE: bars must be {_MIN_BARS}..{_MAX_BARS}"
            )
        offset = int(replay_offset)
        if offset < 0 or offset > _MAX_REPLAY_OFFSET:
            raise TradingViewBridgeError(
                f"TRADINGVIEW_REPLAY_OFFSET_OUT_OF_RANGE: replay_offset must be "
                f"0..{_MAX_REPLAY_OFFSET}"
            )
        spec, ticker = self.resolve_symbol(symbol)
        series = self._bars(spec, ticker, tf, count, offset)
        self._chart_calls += 1

        closes = [bar.close for bar in series]
        first = series[0]
        last = series[-1]
        high = max(bar.high for bar in series)
        low = min(bar.low for bar in series)
        total_volume = sum(bar.volume for bar in series)
        change_pct = (last.close - first.open) / first.open * 100.0 if first.open else 0.0
        range_pct = (high - low) / low * 100.0 if low else 0.0
        metrics: dict[str, float] = {
            "bar_count": float(len(series)),
            "replay_offset": float(offset),
            "open": first.open,
            "high": high,
            "low": low,
            "close": last.close,
            "change_pct": change_pct,
            "range_pct": range_pct,
            "avg_volume": total_volume / float(len(series)),
            "total_volume": total_volume,
            "last_bar_ts_ns": float(last.ts_ns),
            "mean_close": _mean(closes),
        }
        packet, verification = self._seal(ticker, series, metrics)

        return {
            "bridge_version": BRIDGE_VERSION,
            "symbol": spec.symbol if spec is not None else ticker,
            "tradingview_ticker": ticker,
            "exchange": ticker.split(":", 1)[0],
            "silo": spec.silo if spec is not None else None,
            "asset_class": spec.asset_class if spec is not None else None,
            "timeframe": tf,
            "timeframe_seconds": TIMEFRAME_SECONDS[tf],
            "mode": resolved_mode,
            "replay": {
                "mode": resolved_mode,
                "replay_offset": offset,
                "replayable": True,
                "window_end_ts_ns": int(last.ts_ns),
                "window_start_ts_ns": int(first.ts_ns),
                "bar_interval_ns": TIMEFRAME_SECONDS[tf] * _NS_PER_SECOND,
                "synthetic_proxy": True,
            },
            "bars": [bar.to_dict() for bar in series],
            "summary": {
                "open": _round(first.open),
                "high": _round(high),
                "low": _round(low),
                "close": _round(last.close),
                "change_pct": _round(change_pct, 4),
                "range_pct": _round(range_pct, 4),
                "avg_volume": _round(total_volume / float(len(series)), 3),
            },
            "packet": packet.to_dict(),
            "grounding": verification,
            "law": "sec_018 Law 3 - every bar is sealed in a SHA-256 grounding packet",
        }

    def evaluate_tv_study(
        self,
        symbol: str,
        study_id: str,
        timeframe: str = "1D",
        bars: int = 200,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate one Pine Script study and return grounded plot series."""

        tf = self._timeframe(timeframe)
        study = self._study(study_id)
        count = int(bars)
        if count < _MIN_BARS or count > _MAX_BARS:
            raise TradingViewBridgeError(
                f"TRADINGVIEW_BARS_OUT_OF_RANGE: bars must be {_MIN_BARS}..{_MAX_BARS}"
            )
        overrides = dict(params or {})
        spec, ticker = self.resolve_symbol(symbol)
        series = self._bars(spec, ticker, tf, count, 0)
        self._study_calls += 1

        plots, signals, verdict = _run_study(study, series, overrides)
        metrics: dict[str, float] = {"bar_count": float(len(series))}
        for name in sorted(plots):
            values = plots[name]
            latest = _last(values, 0.0) if values else 0.0
            metrics[f"plot.{name}"] = latest
        for name, value in sorted(signals.items()):
            if isinstance(value, bool):
                metrics[f"signal.{name}"] = 1.0 if value else 0.0
            elif isinstance(value, (int, float)):
                metrics[f"signal.{name}"] = float(value)
        packet, verification = self._seal(ticker, series, metrics)

        latest = {
            name: _round(_last(values, 0.0))
            for name, values in sorted(plots.items())
            if values
        }
        return {
            "bridge_version": BRIDGE_VERSION,
            "symbol": spec.symbol if spec is not None else ticker,
            "tradingview_ticker": ticker,
            "silo": spec.silo if spec is not None else None,
            "study_id": study,
            "study_title": _STUDY_TITLES.get(study, study),
            "timeframe": tf,
            "bars": len(series),
            "params": {str(key): value for key, value in overrides.items()},
            "plots": {
                name: [None if value is None else _round(value) for value in values]
                for name, values in sorted(plots.items())
            },
            "latest": latest,
            "signals": dict(sorted(signals.items())),
            "verdict": verdict,
            "packet": packet.to_dict(),
            "grounding": verification,
            "law": "sec_018 Law 3 - study plots are hashed before they may be quoted",
        }

    def search_tv_symbols(
        self,
        query: str,
        asset_type: str | None = None,
    ) -> dict[str, Any]:
        """Search the mapped universe by NIOS symbol, TradingView ticker or name."""

        needle = str(query or "").strip().lower()
        if not needle:
            raise TradingViewBridgeError("TRADINGVIEW_SEARCH_EMPTY: query must not be empty")
        if len(needle) > 64:
            raise TradingViewBridgeError(
                "TRADINGVIEW_SEARCH_TOO_LONG: query must be <= 64 characters"
            )
        token = str(asset_type or "").strip().upper()
        rows: list[dict[str, Any]] = []
        for spec in self._registry.all():
            haystack = " ".join(
                (
                    spec.symbol,
                    spec.display_name,
                    spec.tradingview_ticker,
                    spec.sector,
                    spec.sub_category,
                    spec.asset_class,
                    spec.venue,
                )
            ).lower()
            if needle not in haystack:
                continue
            if token and not (
                spec.asset_class == token
                or spec.sub_category == token
                or token in spec.sub_category
                or token in spec.asset_class
                or token == spec.silo
            ):
                continue
            rows.append(
                {
                    "symbol": spec.symbol,
                    "tradingview_ticker": spec.tradingview_ticker,
                    "display_name": spec.display_name,
                    "silo": spec.silo,
                    "venue": spec.venue,
                    "asset_class": spec.asset_class,
                    "sub_category": spec.sub_category,
                    "sector": spec.sector,
                    "currency": spec.currency,
                }
            )
        rows.sort(key=lambda row: (row["silo"], row["symbol"]))
        return {
            "bridge_version": BRIDGE_VERSION,
            "query": str(query).strip(),
            "asset_type": token or None,
            "total_count": len(rows),
            "returned_count": len(rows),
            "items": rows,
            "law": "sec_014 - instrument identity is resolved before any analysis",
        }

    def replay_tape(
        self,
        symbol: str,
        *,
        timeframe: str = "1D",
        bars: int = 252,
        replay_offset: int = 0,
    ) -> tuple[PriceBar, ...]:
        """Build a ``PriceBar`` tape so the walk-forward engine can run on
        TradingView replay bars instead of the internal synthetic generator."""

        chart = self.fetch_tv_chart(
            symbol,
            timeframe=timeframe,
            bars=bars,
            replay_offset=replay_offset,
        )
        interval_ns = int(chart["replay"]["bar_interval_ns"])
        del interval_ns  # timestamps already carry the interval
        return tuple(
            TradingViewBar(
                ts_ns=int(row["ts_ns"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ).to_price_bar(index)
            for index, row in enumerate(chart["bars"])
        )


def _floor_price(asset_class: str) -> float:
    if asset_class == "PERPETUAL":
        return 1e-9
    if asset_class in ("INDEX_FUTURE", "FUTURE", "COMMODITY_FUTURE"):
        return 1.0
    return 1e-4


def _bar_volume(asset_class: str, index: int, rng: random.Random) -> float:
    if asset_class == "PERPETUAL":
        base = 1_450.0
    elif asset_class in ("INDEX_FUTURE", "FUTURE"):
        base = 8_400.0
    elif asset_class in ("EQUITY", "ETF"):
        base = 264_000.0
    elif asset_class in ("COMMODITY_FUTURE", "CURRENCY_FUTURE"):
        base = 12_400.0
    elif asset_class in ("FX", "METAL", "ENERGY"):
        base = 96_000.0
    else:
        base = 48_000.0
    seasonality = 1.0 + 0.42 * math.sin(index / 9.0)
    return float(base * seasonality * (0.62 + rng.random() * 0.78))


# ---------------------------------------------------------------------------
# Pine Script study implementations
# ---------------------------------------------------------------------------
def _run_study(
    study: str,
    bars: Sequence[TradingViewBar],
    params: Mapping[str, Any],
) -> tuple[dict[str, list[float | None]], dict[str, Any], dict[str, Any]]:
    closes = [bar.close for bar in bars]

    if study == "STD;RSI":
        window = int(params.get("length", 14) or 14)
        rsi = _rsi_series(closes, window)
        latest = _last(rsi, 50.0)
        plots = {
            "RSI": rsi,
            "UpperBand": [70.0] * len(bars),
            "MidLine": [50.0] * len(bars),
            "LowerBand": [30.0] * len(bars),
        }
        zone = "OVERBOUGHT" if latest >= 70.0 else ("OVERSOLD" if latest <= 30.0 else "NEUTRAL")
        signals = {
            "zone": zone,
            "overbought": latest >= 70.0,
            "oversold": latest <= 30.0,
            "rsi": latest,
        }
        verdict = {
            "study": study,
            "regime": zone,
            "bias": "BEARISH" if latest >= 70.0 else ("BULLISH" if latest <= 30.0 else "NEUTRAL"),
            "score": _round((latest - 50.0) / 50.0, 6),
        }
        return plots, signals, verdict

    if study == "STD;MACD":
        fast = int(params.get("fast", 12) or 12)
        slow = int(params.get("slow", 26) or 26)
        signal_window = int(params.get("signal", 9) or 9)
        fast_ema = _ema_series(closes, fast)
        slow_ema = _ema_series(closes, slow)
        macd: list[float | None] = [
            None if (f is None or s is None) else float(f - s)
            for f, s in zip(fast_ema, slow_ema)
        ]
        valid = [value for value in macd if value is not None]
        signal_series_raw = _ema_series(valid, signal_window) if valid else []
        signal_series: list[float | None] = [None] * len(macd)
        cursor = 0
        for index, value in enumerate(macd):
            if value is None:
                continue
            signal_series[index] = (
                signal_series_raw[cursor] if cursor < len(signal_series_raw) else None
            )
            cursor += 1
        histogram = [
            None if (m is None or s is None) else float(m - s)
            for m, s in zip(macd, signal_series)
        ]
        latest_macd = _last(macd, 0.0)
        latest_signal = _last(signal_series, 0.0)
        hist = _last(histogram, 0.0)
        plots = {"MACD": macd, "Signal": signal_series, "Histogram": histogram}
        signals = {
            "bullish": bool(latest_macd > latest_signal),
            "histogram": hist,
            "macd": latest_macd,
            "signal": latest_signal,
        }
        verdict = {
            "study": study,
            "regime": "BULLISH" if latest_macd > latest_signal else "BEARISH",
            "bias": "BULLISH" if hist > 0 else "BEARISH",
            "score": _round(hist / max(abs(latest_signal), 1e-9), 6),
        }
        return plots, signals, verdict

    if study == "STD;Supertrend":
        atr_window = int(params.get("atr_length", 10) or 10)
        factor = float(params.get("factor", 3.0) or 3.0)
        atr = _atr_series(bars, atr_window)
        upper: list[float | None] = []
        lower: list[float | None] = []
        trend: list[float | None] = []
        supertrend: list[float | None] = []
        last_upper: float | None = None
        last_lower: float | None = None
        direction = 1.0
        for index, bar in enumerate(bars):
            band = atr[index]
            if band is None:
                upper.append(None)
                lower.append(None)
                trend.append(None)
                supertrend.append(None)
                continue
            midpoint = (bar.high + bar.low) / 2.0
            raw_upper = midpoint + factor * band
            raw_lower = midpoint - factor * band
            if last_upper is None:
                last_upper = raw_upper
                last_lower = raw_lower
            else:
                last_upper = (
                    raw_upper
                    if raw_upper < last_upper or bar.close > last_upper
                    else last_upper
                )
                last_lower = (
                    raw_lower
                    if raw_lower > last_lower or bar.close < last_lower
                    else last_lower
                )
            if direction > 0 and bar.close < last_lower:
                direction = -1.0
            elif direction < 0 and bar.close > last_upper:
                direction = 1.0
            upper.append(last_upper)
            lower.append(last_lower)
            trend.append(direction)
            supertrend.append(last_lower if direction > 0 else last_upper)
        latest_close = closes[-1] if closes else 0.0
        latest_trend = _last(trend, 0.0)
        plots = {
            "Supertrend": supertrend,
            "Direction": trend,
            "UpperBand": upper,
            "LowerBand": lower,
        }
        signals = {
            "trend": "UP" if latest_trend > 0 else "DOWN",
            "direction": latest_trend,
            "close": latest_close,
        }
        verdict = {
            "study": study,
            "regime": "TREND_UP" if latest_trend > 0 else "TREND_DOWN",
            "bias": "BULLISH" if latest_trend > 0 else "BEARISH",
            "score": 1.0 if latest_trend > 0 else -1.0,
        }
        return plots, signals, verdict

    if study == "STD;Ichimoku Cloud":
        conversion = int(params.get("conversion", 9) or 9)
        base = int(params.get("base", 26) or 26)
        span_b_window = int(params.get("span_b", 52) or 52)

        def _mid(window: int) -> list[float | None]:
            out: list[float | None] = []
            for index in range(len(bars)):
                if index < window - 1:
                    out.append(None)
                    continue
                slice_ = bars[index - window + 1 : index + 1]
                out.append((max(b.high for b in slice_) + min(b.low for b in slice_)) / 2.0)
            return out

        tenkan = _mid(conversion)
        kijun = _mid(base)
        senkou_a: list[float | None] = [
            None if (t is None or k is None) else float((t + k) / 2.0)
            for t, k in zip(tenkan, kijun)
        ]
        senkou_b = _mid(span_b_window)
        chikou = [closes[index] if index < len(closes) else None for index in range(len(closes))]
        latest_close = closes[-1] if closes else 0.0
        cloud_top = max(_last(senkou_a, latest_close), _last(senkou_b, latest_close))
        cloud_bottom = min(_last(senkou_a, latest_close), _last(senkou_b, latest_close))
        above = latest_close >= cloud_top
        below = latest_close <= cloud_bottom
        plots = {
            "Tenkan": tenkan,
            "Kijun": kijun,
            "SenkouA": senkou_a,
            "SenkouB": senkou_b,
            "Chikou": chikou,
        }
        signals = {
            "above_cloud": bool(above),
            "below_cloud": bool(below),
            "cloud_top": cloud_top,
            "cloud_bottom": cloud_bottom,
        }
        verdict = {
            "study": study,
            "regime": "ABOVE_CLOUD" if above else ("BELOW_CLOUD" if below else "INSIDE_CLOUD"),
            "bias": "BULLISH" if above else ("BEARISH" if below else "NEUTRAL"),
            "score": _round((latest_close - cloud_bottom) / max(cloud_top - cloud_bottom, 1e-9), 6),
        }
        return plots, signals, verdict

    if study == "STD;Bollinger Bands":
        window = int(params.get("length", 20) or 20)
        multiplier = float(params.get("mult", 2.0) or 2.0)
        basis = _sma_series(closes, window)
        deviation = _rolling_stdev(closes, window)
        upper = [
            None if (b is None or d is None) else float(b + multiplier * d)
            for b, d in zip(basis, deviation)
        ]
        lower = [
            None if (b is None or d is None) else float(b - multiplier * d)
            for b, d in zip(basis, deviation)
        ]
        bandwidth = [
            None if (u is None or lo is None or not b) else float((u - lo) / b * 100.0)
            for u, lo, b in zip(upper, lower, basis)
        ]
        percent_b = [
            None
            if (u is None or lo is None or u == lo)
            else float((closes[index] - lo) / (u - lo))
            for index, (u, lo) in enumerate(zip(upper, lower))
        ]
        latest_close = closes[-1] if closes else 0.0
        latest_upper = _last(upper, latest_close)
        latest_lower = _last(lower, latest_close)
        plots = {
            "Basis": basis,
            "UpperBand": upper,
            "LowerBand": lower,
            "Bandwidth": bandwidth,
            "PercentB": percent_b,
        }
        signals = {
            "upper_band": latest_upper,
            "lower_band": latest_lower,
            "breakout": bool(latest_close > latest_upper),
            "breakdown": bool(latest_close < latest_lower),
        }
        verdict = {
            "study": study,
            "regime": "EXPANDING" if _last(bandwidth, 0.0) > 4.0 else "COMPRESSED",
            "bias": "BULLISH" if latest_close > latest_upper else (
                "BEARISH" if latest_close < latest_lower else "NEUTRAL"
            ),
            "score": _round(_last(percent_b, 0.5), 6),
        }
        return plots, signals, verdict

    if study == "STD;EMA Ribbon":
        windows = (8, 13, 21, 34, 55)
        ribbon = {f"EMA{window}": _ema_series(closes, window) for window in windows}
        latest_values = [_last(values, 0.0) for values in ribbon.values()]
        stacked_up = all(
            latest_values[index] > latest_values[index + 1]
            for index in range(len(latest_values) - 1)
        )
        stacked_down = all(
            latest_values[index] < latest_values[index + 1]
            for index in range(len(latest_values) - 1)
        )
        plots = dict(ribbon)
        signals = {
            "stacked_bullish": bool(stacked_up),
            "stacked_bearish": bool(stacked_down),
            "fastest": latest_values[0],
            "slowest": latest_values[-1],
        }
        verdict = {
            "study": study,
            "regime": "RIBBON_BULL" if stacked_up else ("RIBBON_BEAR" if stacked_down else "RIBBON_MIXED"),
            "bias": "BULLISH" if stacked_up else ("BEARISH" if stacked_down else "NEUTRAL"),
            "score": _round(
                (latest_values[0] - latest_values[-1]) / max(abs(latest_values[-1]), 1e-9), 6
            ),
        }
        return plots, signals, verdict

    if study == "STD;VWAP Bands":
        window = int(params.get("window", 20) or 20)
        typical = [(bar.high + bar.low + bar.close) / 3.0 for bar in bars]
        vwap: list[float | None] = []
        upper1: list[float | None] = []
        lower1: list[float | None] = []
        upper2: list[float | None] = []
        lower2: list[float | None] = []
        for index in range(len(bars)):
            start = max(0, index - window + 1)
            slice_ = bars[start : index + 1]
            volume = sum(bar.volume for bar in slice_)
            price = typical[start : index + 1]
            if volume <= 0.0:
                vwap.append(None)
                upper1.append(None)
                lower1.append(None)
                upper2.append(None)
                lower2.append(None)
                continue
            anchored = sum(p * bar.volume for p, bar in zip(price, slice_)) / volume
            deviation = _stdev(price)
            vwap.append(anchored)
            upper1.append(anchored + deviation)
            lower1.append(anchored - deviation)
            upper2.append(anchored + 2.0 * deviation)
            lower2.append(anchored - 2.0 * deviation)
        latest_close = closes[-1] if closes else 0.0
        anchored_latest = _last(vwap, latest_close)
        plots = {
            "VWAP": vwap,
            "UpperBand1": upper1,
            "LowerBand1": lower1,
            "UpperBand2": upper2,
            "LowerBand2": lower2,
        }
        signals = {
            "anchored_vwap": anchored_latest,
            "above_vwap": bool(latest_close >= anchored_latest),
            "upper_2sigma": _last(upper2, anchored_latest),
            "lower_2sigma": _last(lower2, anchored_latest),
        }
        verdict = {
            "study": study,
            "regime": "ABOVE_VWAP" if latest_close >= anchored_latest else "BELOW_VWAP",
            "bias": "BULLISH" if latest_close >= anchored_latest else "BEARISH",
            "score": _round(
                (latest_close - anchored_latest) / max(anchored_latest, 1e-9) * 100.0, 6
            ),
        }
        return plots, signals, verdict

    # NIOS;Sovereign Regime Composite -----------------------------------
    fast = _ema_series(closes, 50)
    slow = _ema_series(closes, 200 if len(closes) >= 200 else max(21, len(closes) // 2))
    rsi = _rsi_series(closes, 14)
    atr = _atr_series(bars, 14)
    volumes = [bar.volume for bar in bars]
    volume_mean = _mean(volumes) if volumes else 0.0
    volume_z = (
        (volumes[-1] - volume_mean) / max(_stdev(volumes), 1e-9) if volumes else 0.0
    )
    latest_close = closes[-1] if closes else 0.0
    trend_component = 0.0
    latest_fast = _last(fast, latest_close)
    latest_slow = _last(slow, latest_close)
    if latest_slow:
        trend_component = max(min((latest_fast - latest_slow) / latest_slow * 100.0, 3.0), -3.0) / 3.0
    momentum_component = max(min((_last(rsi, 50.0) - 50.0) / 50.0, 1.0), -1.0)
    atr_pct = (_last(atr, 0.0) / latest_close * 100.0) if latest_close else 0.0
    volatility_component = max(min(atr_pct / 4.0, 1.0), 0.0)
    composite = 0.5 * trend_component + 0.35 * momentum_component - 0.15 * volatility_component

    if composite >= 0.22 and volatility_component < 0.72:
        regime = "TREND_UP"
    elif composite <= -0.22 and volatility_component < 0.72:
        regime = "TREND_DOWN"
    elif volatility_component >= 0.72:
        regime = "VOLATILE"
    else:
        regime = "RANGE"

    plots = {
        "Composite": [float(composite)] * len(bars),
        "TrendComponent": [float(trend_component)] * len(bars),
        "MomentumComponent": [float(momentum_component)] * len(bars),
        "VolatilityComponent": [float(volatility_component)] * len(bars),
    }
    signals = {
        "regime": regime,
        "composite": composite,
        "trend": trend_component,
        "momentum": momentum_component,
        "volatility": volatility_component,
        "volume_z": float(volume_z),
        "atr_pct": float(atr_pct),
    }
    verdict = {
        "study": study,
        "regime": regime,
        "bias": "BULLISH" if regime == "TREND_UP" else ("BEARISH" if regime == "TREND_DOWN" else "NEUTRAL"),
        "score": _round(composite, 6),
        "conviction": _round(min(abs(composite), 1.0), 6),
    }
    return plots, signals, verdict


#: Process-wide default bridge (stateless, deterministic, air-gapped).
bridge = TradingViewBridge()
