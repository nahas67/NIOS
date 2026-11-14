"""Eight built-in strategy templates harvested from ``ZhuLinsen/daily_stock_analysis``.

Donor provenance (``sec_008``): the upstream repository ships fifteen declarative
strategy templates as YAML documents under ``strategies/``.  Ring 2 keeps the eight
that map onto the Sovereign Kernel's instrument universe and re-expresses each one as
a deterministic, allocation-free evaluator so a strategy can never drift between two
runs of the same bar series.

============================  =========================  ================
NIOS strategy id              donor template             donor file
============================  =========================  ================
``ma_golden_cross``           ``ma_golden_cross``        ``ma_golden_cross.yaml``
``bullish_trend_alignment``   ``bull_trend``             ``bull_trend.yaml``
``chanlun_pivot_breakout``    ``chan_theory``            ``chan_theory.yaml``
``elliott_wave_impulse``      ``wave_theory``            ``wave_theory.yaml``
``event_catalyst_momentum``   ``event_driven``           ``event_driven.yaml``
``growth_quality_compounder`` ``growth_quality``         ``growth_quality.yaml``
``expectation_rerating``      ``expectation_repricing``  ``expectation_repricing.yaml``
``cross_asset_etf_rotation``  (synthesised)              ``hot_theme.yaml``
============================  =========================  ================

Air-gap contract (``sec_002`` flaw 2, ``sec_004``): nothing in this module opens a
socket, reads an environment variable, holds a broker key, or signs an order.  Every
function is a pure transformation from a :class:`PriceBar` series to a
:class:`StrategySignal`.  The donor's YAML descriptions were treated as untrusted
data: they were read for parameter intent only, never executed.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "BAR_INDEX_BASE",
    "KNOWN_SILOS",
    "PriceBar",
    "StrategySignal",
    "StrategySpec",
    "STRATEGY_IDS",
    "STRATEGY_CATALOG",
    "get_strategy",
    "list_strategies",
    "strategy_ids",
    "evaluate_strategy",
    "evaluate_all_strategies",
    "synthetic_bars",
    "closes",
    "sma",
    "ema",
    "rsi",
    "atr",
    "macd",
    "stdev",
    "roc",
    "max_drawdown_pct",
    "volume_ratio",
]

#: The four statutory silos (``sec_014``).  A strategy runs on all of them.
KNOWN_SILOS: tuple[str, ...] = (
    "CRYPTO_BINANCE",
    "INDIA_ZERODHA_DHAN",
    "US_IBKR",
    "FX_MT5",
)

#: Bar indices are zero-based and strictly increasing within a series.
BAR_INDEX_BASE: int = 0

#: Neutral score awarded to a strategy that has not fired.
NEUTRAL_SCORE: float = 50.0
MIN_SCORE: float = 0.0
MAX_SCORE: float = 100.0


# ---------------------------------------------------------------------------
# Bar series
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PriceBar:
    """One OHLCV observation.

    ``ts_ns`` is the *knowledge* timestamp (``sec_009`` bitemporal contract): a
    strategy may only read a bar whose ``ts_ns`` is at or before the decision
    instant, which is enforced by the caller assembling the series.
    """

    index: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "ts_ns": self.ts_ns,
        }


@dataclass(slots=True, frozen=True)
class StrategySpec:
    """Declarative description of one harvested strategy template."""

    strategy_id: str
    name: str
    family: str
    donor_source: str
    donor_file: str
    timeframe: str
    summary: str
    required_bars: int
    parameters: Mapping[str, float] = field(default_factory=dict)
    silos: tuple[str, ...] = KNOWN_SILOS

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "family": self.family,
            "donor_source": self.donor_source,
            "donor_file": self.donor_file,
            "timeframe": self.timeframe,
            "summary": self.summary,
            "required_bars": self.required_bars,
            "parameters": {key: float(value) for key, value in self.parameters.items()},
            "silos": list(self.silos),
        }


@dataclass(slots=True)
class StrategySignal:
    """Deterministic output of one strategy evaluation.

    ``metrics`` is the only channel through which a numeric claim may travel: the
    decision dashboard seals it into a :class:`core.schemas.GroundedFactPacket`
    before any number reaches an operator (Law 3).
    """

    strategy_id: str
    symbol: str
    silo: str
    score: float
    triggered: bool
    direction: str
    entry_price: float
    target_price: float
    stop_loss: float
    reward_risk: float
    reasons: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
    bars_used: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "silo": self.silo,
            "score": round(self.score, 4),
            "triggered": self.triggered,
            "direction": self.direction,
            "entry_price": round(self.entry_price, 6),
            "target_price": round(self.target_price, 6),
            "stop_loss": round(self.stop_loss, 6),
            "reward_risk": round(self.reward_risk, 4),
            "reasons": list(self.reasons),
            "risk_factors": list(self.risk_factors),
            "metrics": {key: round(float(value), 8) for key, value in self.metrics.items()},
            "bars_used": self.bars_used,
        }


# ---------------------------------------------------------------------------
# Indicator primitives (pure Python, deterministic, no numpy allocation)
# ---------------------------------------------------------------------------


def closes(bars: Sequence[PriceBar]) -> list[float]:
    """Closing prices of ``bars``."""

    return [float(bar.close) for bar in bars]


def sma(values: Sequence[float], window: int) -> list[float | None]:
    """Simple moving average; ``None`` until ``window`` samples are available."""

    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += float(value)
        if index >= window:
            running -= float(values[index - window])
        if index >= window - 1:
            out[index] = running / float(window)
    return out


def ema(values: Sequence[float], window: int) -> list[float | None]:
    """Exponential moving average seeded with the first ``window`` mean."""

    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < window:
        return out
    seed = sum(float(v) for v in values[:window]) / float(window)
    out[window - 1] = seed
    multiplier = 2.0 / (float(window) + 1.0)
    previous = seed
    for index in range(window, len(values)):
        previous = (float(values[index]) - previous) * multiplier + previous
        out[index] = previous
    return out


def rsi(values: Sequence[float], window: int = 14) -> list[float | None]:
    """Wilder relative strength index."""

    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) <= window:
        return out
    gains = 0.0
    losses = 0.0
    for index in range(1, window + 1):
        delta = float(values[index]) - float(values[index - 1])
        if delta >= 0.0:
            gains += delta
        else:
            losses -= delta
    average_gain = gains / float(window)
    average_loss = losses / float(window)
    out[window] = _rsi_value(average_gain, average_loss)
    for index in range(window + 1, len(values)):
        delta = float(values[index]) - float(values[index - 1])
        gain = delta if delta > 0.0 else 0.0
        loss = -delta if delta < 0.0 else 0.0
        average_gain = (average_gain * (window - 1) + gain) / float(window)
        average_loss = (average_loss * (window - 1) + loss) / float(window)
        out[index] = _rsi_value(average_gain, average_loss)
    return out


def _rsi_value(average_gain: float, average_loss: float) -> float:
    if average_loss <= 0.0:
        return 100.0 if average_gain > 0.0 else 50.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def atr(bars: Sequence[PriceBar], window: int = 14) -> float:
    """Average true range of the most recent ``window`` bars."""

    if len(bars) < 2:
        return 0.0
    span = min(int(window), len(bars) - 1)
    total = 0.0
    for index in range(len(bars) - span, len(bars)):
        current = bars[index]
        previous_close = float(bars[index - 1].close)
        true_range = max(
            float(current.high) - float(current.low),
            abs(float(current.high) - previous_close),
            abs(float(current.low) - previous_close),
        )
        total += true_range
    return total / float(span)


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None]]:
    """Return ``(macd_line, signal_line)``."""

    fast_series = ema(values, fast)
    slow_series = ema(values, slow)
    line: list[float | None] = [None] * len(values)
    for index in range(len(values)):
        fast_value = fast_series[index]
        slow_value = slow_series[index]
        if fast_value is not None and slow_value is not None:
            line[index] = fast_value - slow_value
    compact = [value for value in line if value is not None]
    signal_compact = ema(compact, signal)
    signal_line: list[float | None] = [None] * len(values)
    offset = len(values) - len(compact)
    for position, value in enumerate(signal_compact):
        signal_line[offset + position] = value
    return line, signal_line


def stdev(values: Sequence[float]) -> float:
    """Population standard deviation."""

    if len(values) < 2:
        return 0.0
    mean = sum(float(v) for v in values) / float(len(values))
    variance = sum((float(v) - mean) ** 2 for v in values) / float(len(values))
    return math.sqrt(variance)


def roc(values: Sequence[float], window: int) -> float:
    """Rate of change over ``window`` bars, in percent."""

    if len(values) <= window or window <= 0:
        return 0.0
    reference = float(values[-1 - window])
    if reference == 0.0:
        return 0.0
    return (float(values[-1]) - reference) / reference * 100.0


def max_drawdown_pct(values: Sequence[float]) -> float:
    """Largest peak-to-trough drawdown of ``values``, as a negative percentage."""

    peak = 0.0
    worst = 0.0
    for value in values:
        current = float(value)
        if current > peak:
            peak = current
        if peak > 0.0:
            drawdown = (current - peak) / peak * 100.0
            if drawdown < worst:
                worst = drawdown
    return worst


def volume_ratio(bars: Sequence[PriceBar], window: int = 20) -> float:
    """Latest volume divided by the trailing mean volume."""

    if not bars:
        return 0.0
    span = bars[-int(window) :]
    mean = sum(float(bar.volume) for bar in span) / float(len(span))
    if mean <= 0.0:
        return 0.0
    return float(bars[-1].volume) / mean


def _clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _pct(numerator: float, denominator: float) -> float:
    if denominator == 0.0:
        return 0.0
    return numerator / denominator * 100.0


def _round_score(value: float) -> float:
    return round(_clamp(value, MIN_SCORE, MAX_SCORE), 4)


def _swing_points(bars: Sequence[PriceBar], lookback: int = 3) -> tuple[list[int], list[int]]:
    """Return ``(swing_high_indices, swing_low_indices)`` for a fractal window."""

    highs: list[int] = []
    lows: list[int] = []
    total = len(bars)
    for index in range(lookback, total - lookback):
        window_slice = bars[index - lookback : index + lookback + 1]
        high = float(bars[index].high)
        low = float(bars[index].low)
        if high >= max(float(bar.high) for bar in window_slice):
            highs.append(index)
        if low <= min(float(bar.low) for bar in window_slice):
            lows.append(index)
    return highs, lows


def _targets(entry: float, average_range: float, *, rr: float, cap_pct: float, stop_pct: float) -> tuple[float, float]:
    """ATR-bounded deterministic target/stop pair."""

    atr_span = average_range if average_range > 0.0 else entry * 0.01
    target = entry + atr_span * rr
    stop = entry - atr_span
    target = min(target, entry * (1.0 + cap_pct))
    stop = max(stop, entry * (1.0 - stop_pct))
    return target, stop


def _reward_risk(entry: float, target: float, stop: float) -> float:
    risk = entry - stop
    if risk <= 0.0:
        return 0.0
    return (target - entry) / risk


# ---------------------------------------------------------------------------
# Strategy catalog
# ---------------------------------------------------------------------------


STRATEGY_CATALOG: tuple[StrategySpec, ...] = (
    StrategySpec(
        strategy_id="ma_golden_cross",
        name="MA Golden Cross",
        family="TREND",
        donor_source="ZhuLinsen/daily_stock_analysis",
        donor_file="strategies/ma_golden_cross.yaml",
        timeframe="SWING",
        summary="MA5/MA10/MA20 golden cross with a bias-MA5 entry filter.",
        required_bars=40,
        parameters={"fast": 5.0, "mid": 10.0, "slow": 20.0, "bias_floor_pct": -3.0, "bias_ceiling_pct": 8.0},
    ),
    StrategySpec(
        strategy_id="bullish_trend_alignment",
        name="Bullish Trend Alignment",
        family="TREND",
        donor_source="ZhuLinsen/daily_stock_analysis",
        donor_file="strategies/bull_trend.yaml",
        timeframe="POSITION",
        summary="Multi-timeframe bullish stack (fast above mid above slow) with volume confirmation.",
        required_bars=80,
        parameters={"fast": 5.0, "mid": 10.0, "slow": 20.0, "long": 60.0, "volume_floor": 1.2},
    ),
    StrategySpec(
        strategy_id="chanlun_pivot_breakout",
        name="Chanlun Pivot Breakout",
        family="STRUCTURE",
        donor_source="ZhuLinsen/daily_stock_analysis",
        donor_file="strategies/chan_theory.yaml",
        timeframe="SWING",
        summary="Central pivot zone breakout with price/RSI divergence veto.",
        required_bars=70,
        parameters={"swing_lookback": 3.0, "zone_bars": 30.0, "divergence_rsi_gap": 4.0},
    ),
    StrategySpec(
        strategy_id="elliott_wave_impulse",
        name="Elliott Wave Impulse",
        family="WAVE",
        donor_source="ZhuLinsen/daily_stock_analysis",
        donor_file="strategies/wave_theory.yaml",
        timeframe="SWING",
        summary="Wave-3 impulse continuation after a Fibonacci retracement band holds.",
        required_bars=90,
        parameters={"fib_low": 0.382, "fib_high": 0.618, "extension": 1.618},
    ),
    StrategySpec(
        strategy_id="event_catalyst_momentum",
        name="Event Catalyst Momentum",
        family="EVENT",
        donor_source="ZhuLinsen/daily_stock_analysis",
        donor_file="strategies/event_driven.yaml",
        timeframe="INTRADAY_TO_SWING",
        summary="Catalyst gap-up with a volume surge confirmation.",
        required_bars=30,
        parameters={"gap_floor_pct": 1.5, "volume_floor": 1.8},
    ),
    StrategySpec(
        strategy_id="growth_quality_compounder",
        name="Growth Quality Compounder",
        family="QUALITY",
        donor_source="ZhuLinsen/daily_stock_analysis",
        donor_file="strategies/growth_quality.yaml",
        timeframe="POSITION",
        summary="Return-on-equity and margin quality screened against realised drawdown.",
        required_bars=60,
        parameters={"roe_floor_pct": 15.0, "margin_floor_pct": 12.0, "drawdown_ceiling_pct": 18.0},
    ),
    StrategySpec(
        strategy_id="expectation_rerating",
        name="Expectation Rerating",
        family="VALUE",
        donor_source="ZhuLinsen/daily_stock_analysis",
        donor_file="strategies/expectation_repricing.yaml",
        timeframe="POSITION",
        summary="Valuation compression to mean reversion on a discounted quality book.",
        required_bars=120,
        parameters={"z_floor": -1.0, "z_ceiling": 0.25, "mean_window": 100.0},
    ),
    StrategySpec(
        strategy_id="cross_asset_etf_rotation",
        name="Cross-Asset ETF Rotation",
        family="ROTATION",
        donor_source="ZhuLinsen/daily_stock_analysis",
        donor_file="strategies/hot_theme.yaml",
        timeframe="POSITION",
        summary="Relative strength against the silo benchmark, scaled by realised volatility.",
        required_bars=80,
        parameters={"relative_window": 60.0, "vol_window": 40.0, "score_floor": 0.35},
    ),
)

STRATEGY_IDS: tuple[str, ...] = tuple(spec.strategy_id for spec in STRATEGY_CATALOG)

_BY_ID: dict[str, StrategySpec] = {spec.strategy_id: spec for spec in STRATEGY_CATALOG}


def strategy_ids() -> tuple[str, ...]:
    """Every registered strategy template, in catalog order."""

    return STRATEGY_IDS


def list_strategies() -> list[dict[str, Any]]:
    """Serialisable catalog for the Ring 3 workstation."""

    return [spec.to_dict() for spec in STRATEGY_CATALOG]


def get_strategy(strategy_id: str) -> StrategySpec:
    """Look up one template; ``KeyError`` for an unknown id."""

    try:
        return _BY_ID[str(strategy_id)]
    except KeyError as error:  # pragma: no cover - defensive
        raise KeyError(f"unknown strategy template: {strategy_id!r}") from error


#: Deterministic per-silo benchmark anchors used by the rotation template.  These are
#: index levels, not prices of a tradable instrument, and never leave Ring 2.
_BENCHMARK_ANCHOR: dict[str, float] = {
    "CRYPTO_BINANCE": 1.0,
    "INDIA_ZERODHA_DHAN": 1.0,
    "US_IBKR": 1.0,
    "FX_MT5": 1.0,
}


# ---------------------------------------------------------------------------
# Deterministic synthetic bars
# ---------------------------------------------------------------------------


def _seed_for(symbol: str, silo: str) -> int:
    material = f"nios-ring2-synthetic|{silo}|{symbol}".encode("utf-8")
    return int(hashlib.sha256(material).hexdigest()[:16], 16)


def synthetic_bars(
    symbol: str,
    silo: str = "US_IBKR",
    *,
    bars: int = 252,
    start_price: float = 100.0,
    start_ts_ns: int = 1_700_000_000_000_000_000,
    bar_interval_ns: int = 86_400_000_000_000,
) -> tuple[PriceBar, ...]:
    """Reproducible OHLCV series for ``symbol`` on ``silo``.

    The generator is seeded from ``sha256(silo|symbol)`` so the same request always
    reconstructs the same series — a hard requirement for a reproducible backtest
    (``sec_010``).  It is a *research fixture*: it carries no market claim and is
    labelled as synthetic everywhere it surfaces in Ring 3.
    """

    if silo not in KNOWN_SILOS:
        raise ValueError(f"unknown silo: {silo!r}")
    count = int(bars)
    if count < 2:
        raise ValueError("bars must be >= 2")
    rng = random.Random(_seed_for(str(symbol), str(silo)))
    price = float(start_price) * (0.7 + rng.random() * 0.6)
    volatility = 0.008 + rng.random() * 0.014
    drift = (rng.random() - 0.44) * 0.0016
    base_volume = 400_000.0 + rng.random() * 900_000.0
    regime_left = 18 + int(rng.random() * 26)
    regime_drift = drift
    out: list[PriceBar] = []
    for index in range(count):
        if regime_left <= 0:
            regime_left = 18 + int(rng.random() * 26)
            regime_drift = (rng.random() - 0.44) * 0.0026
        regime_left -= 1
        shock = rng.gauss(0.0, 1.0)
        step = regime_drift + volatility * shock
        open_price = price
        close_price = max(0.0001, open_price * (1.0 + step))
        wick = abs(shock) * volatility * 0.6 + 0.0012
        high_price = max(open_price, close_price) * (1.0 + wick)
        low_price = min(open_price, close_price) * (1.0 - wick)
        volume = base_volume * (0.65 + abs(shock) * 0.9 + rng.random() * 0.5)
        out.append(
            PriceBar(
                index=index,
                open=round(open_price, 6),
                high=round(high_price, 6),
                low=round(low_price, 6),
                close=round(close_price, 6),
                volume=round(volume, 2),
                ts_ns=int(start_ts_ns) + index * int(bar_interval_ns),
            )
        )
        price = close_price
    return tuple(out)


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


def _base_signal(
    spec: StrategySpec,
    *,
    symbol: str,
    silo: str,
    bars: Sequence[PriceBar],
) -> StrategySignal:
    entry = float(bars[-1].close)
    return StrategySignal(
        strategy_id=spec.strategy_id,
        symbol=str(symbol),
        silo=str(silo),
        score=NEUTRAL_SCORE,
        triggered=False,
        direction="FLAT",
        entry_price=entry,
        target_price=entry,
        stop_loss=entry,
        reward_risk=0.0,
        bars_used=len(bars),
    )


def _finish(
    signal: StrategySignal,
    *,
    score: float,
    triggered: bool,
    average_range: float,
    rr: float,
    cap_pct: float,
    stop_pct: float,
) -> StrategySignal:
    signal.score = _round_score(score)
    signal.triggered = bool(triggered)
    if triggered:
        target, stop = _targets(
            signal.entry_price,
            average_range,
            rr=rr,
            cap_pct=cap_pct,
            stop_pct=stop_pct,
        )
        signal.direction = "LONG"
        signal.target_price = round(target, 6)
        signal.stop_loss = round(stop, 6)
        signal.reward_risk = round(_reward_risk(signal.entry_price, target, stop), 4)
    else:
        signal.direction = "FLAT"
        signal.target_price = signal.entry_price
        signal.stop_loss = signal.entry_price
        signal.reward_risk = 0.0
    return signal


def _evaluate_ma_golden_cross(spec: StrategySpec, symbol: str, silo: str, bars: Sequence[PriceBar]) -> StrategySignal:
    signal = _base_signal(spec, symbol=symbol, silo=silo, bars=bars)
    series = closes(bars)
    fast = float(spec.parameters["fast"])
    mid = float(spec.parameters["mid"])
    slow = float(spec.parameters["slow"])
    ma_fast = sma(series, int(fast))
    ma_mid = sma(series, int(mid))
    ma_slow = sma(series, int(slow))
    average_range = atr(bars, 14)
    if ma_fast[-1] is None or ma_mid[-1] is None or ma_slow[-1] is None:
        signal.reasons = ("insufficient history for the moving-average scaffold",)
        signal.risk_factors = ("the template requires a longer bar window",)
        return _finish(signal, score=NEUTRAL_SCORE, triggered=False, average_range=average_range, rr=2.0, cap_pct=0.08, stop_pct=0.05)
    fast_now = float(ma_fast[-1])
    mid_now = float(ma_mid[-1])
    slow_now = float(ma_slow[-1])
    close_now = series[-1]
    bias = _pct(close_now - fast_now, fast_now)
    crossed = False
    lookback = min(4, len(series) - 1)
    for offset in range(1, lookback + 1):
        previous_fast = ma_fast[-1 - offset]
        previous_mid = ma_mid[-1 - offset]
        if previous_fast is None or previous_mid is None:
            continue
        if fast_now > mid_now and float(previous_fast) <= float(previous_mid):
            crossed = True
            break
    stacked = fast_now > mid_now > slow_now
    bias_floor = float(spec.parameters["bias_floor_pct"])
    bias_ceiling = float(spec.parameters["bias_ceiling_pct"])
    bias_ok = bias_floor <= bias <= bias_ceiling
    volume = volume_ratio(bars, 20)
    score = 34.0
    reasons: list[str] = []
    risks: list[str] = []
    if crossed:
        score += 26.0
        reasons.append("MA5 crossed above MA10 inside the confirmation window")
    elif fast_now > mid_now:
        score += 12.0
        reasons.append("MA5 already holds above MA10")
    else:
        risks.append("MA5 sits at or below MA10, so no cross is in force")
    if stacked:
        score += 16.0
        reasons.append("MA5 above MA10 above MA20 confirms the bullish stack")
    else:
        risks.append("the moving-average stack is not fully bullish")
    if bias_ok:
        score += 12.0
        reasons.append("price sits inside the bias-MA5 entry band")
    else:
        risks.append("price is outside the bias-MA5 entry band")
    if volume >= 1.0:
        score += 6.0
        reasons.append("volume is at or above its trailing mean")
    else:
        risks.append("volume is below its trailing mean")
    if close_now > slow_now:
        score += 6.0
        reasons.append("price holds above the MA20 trend anchor")
    else:
        risks.append("price has lost the MA20 trend anchor")
    triggered = crossed and bias_ok and close_now > slow_now
    signal.metrics = {
        "ma5": fast_now,
        "ma10": mid_now,
        "ma20": slow_now,
        "bias_ma5_pct": bias,
        "close_price": close_now,
        "atr_14": average_range,
        "volume_ratio": volume,
    }
    signal.reasons = tuple(reasons)
    signal.risk_factors = tuple(risks)
    return _finish(signal, score=score, triggered=triggered, average_range=average_range, rr=2.2, cap_pct=0.08, stop_pct=0.05)


def _evaluate_bullish_trend_alignment(spec: StrategySpec, symbol: str, silo: str, bars: Sequence[PriceBar]) -> StrategySignal:
    signal = _base_signal(spec, symbol=symbol, silo=silo, bars=bars)
    series = closes(bars)
    long_window = int(float(spec.parameters["long"]))
    average_range = atr(bars, 14)
    ma_fast = sma(series, int(float(spec.parameters["fast"])))
    ma_mid = sma(series, int(float(spec.parameters["mid"])))
    ma_slow = sma(series, int(float(spec.parameters["slow"])))
    ma_long = sma(series, long_window)
    if ma_long[-1] is None or ma_slow[-1] is None:
        signal.reasons = ("the multi-timeframe stack is still forming",)
        signal.risk_factors = ("a longer bar window is required",)
        return _finish(signal, score=NEUTRAL_SCORE, triggered=False, average_range=average_range, rr=2.0, cap_pct=0.08, stop_pct=0.05)
    fast_now = float(ma_fast[-1])
    mid_now = float(ma_mid[-1])
    slow_now = float(ma_slow[-1])
    long_now = float(ma_long[-1])
    close_now = series[-1]
    volume = volume_ratio(bars, 20)
    volume_floor = float(spec.parameters["volume_floor"])
    aligned = fast_now > mid_now > slow_now > long_now
    above_all = close_now > fast_now
    returns = [series[i] / series[i - 1] - 1.0 for i in range(len(series) - 20, len(series)) if series[i - 1] != 0.0]
    trend_slope = _pct(long_now - float(ma_long[-min(int(float(spec.parameters["slow"])), len(ma_long) - 1)] or long_now), long_now)
    score = 30.0
    reasons: list[str] = []
    risks: list[str] = []
    if aligned:
        score += 32.0
        reasons.append("all four timeframes are stacked bullishly")
    else:
        risks.append("at least one timeframe breaks the bullish stack")
    if above_all:
        score += 14.0
        reasons.append("price trades above the fast anchor")
    else:
        risks.append("price has slipped below the fast anchor")
    if volume >= volume_floor:
        score += 16.0
        reasons.append("volume confirms the advance")
    else:
        risks.append("volume does not yet confirm the advance")
    if long_now > 0.0 and close_now > long_now:
        score += 8.0
        reasons.append("the long anchor is still rising under price")
    if returns:
        dispersion = stdev(returns)
        if dispersion > 0.0:
            score += _clamp(6.0 - dispersion * 120.0, -4.0, 6.0)
    triggered = aligned and above_all and volume >= volume_floor
    signal.metrics = {
        "ma5": fast_now,
        "ma10": mid_now,
        "ma20": slow_now,
        "ma60": long_now,
        "close_price": close_now,
        "atr_14": average_range,
        "volume_ratio": volume,
        "trend_slope_pct": trend_slope,
        "return_dispersion": stdev(returns) if returns else 0.0,
    }
    signal.reasons = tuple(reasons)
    signal.risk_factors = tuple(risks)
    return _finish(signal, score=score, triggered=triggered, average_range=average_range, rr=2.6, cap_pct=0.10, stop_pct=0.06)


def _evaluate_chanlun_pivot_breakout(spec: StrategySpec, symbol: str, silo: str, bars: Sequence[PriceBar]) -> StrategySignal:
    signal = _base_signal(spec, symbol=symbol, silo=silo, bars=bars)
    series = closes(bars)
    average_range = atr(bars, 14)
    lookback = int(float(spec.parameters["swing_lookback"]))
    zone_bars = int(float(spec.parameters["zone_bars"]))
    swing_highs, swing_lows = _swing_points(bars, lookback)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        signal.reasons = ("no completed central pivot zone in the window",)
        signal.risk_factors = ("the structure needs two completed swings",)
        return _finish(signal, score=NEUTRAL_SCORE, triggered=False, average_range=average_range, rr=2.0, cap_pct=0.09, stop_pct=0.05)
    zone_slice = bars[-zone_bars:] if len(bars) > zone_bars else bars
    zone_high = max(float(bar.high) for bar in zone_slice[:-1]) if len(zone_slice) > 1 else float(zone_slice[-1].high)
    zone_low = min(float(bar.low) for bar in zone_slice[:-1]) if len(zone_slice) > 1 else float(zone_slice[-1].low)
    close_now = series[-1]
    breakout = close_now > zone_high
    zone_height_pct = _pct(zone_high - zone_low, zone_low)
    rsi_series = rsi(series, 14)
    divergence = False
    rsi_now = rsi_series[-1]
    gap_floor = float(spec.parameters["divergence_rsi_gap"])
    last_high_index = swing_highs[-1]
    if len(swing_highs) >= 2:
        previous_high_index = swing_highs[-2]
        price_higher = float(bars[last_high_index].high) > float(bars[previous_high_index].high)
        current_rsi = rsi_series[last_high_index]
        previous_rsi = rsi_series[previous_high_index]
        if price_higher and current_rsi is not None and previous_rsi is not None:
            divergence = float(current_rsi) < float(previous_rsi) - gap_floor
    score = 32.0
    reasons: list[str] = []
    risks: list[str] = []
    if breakout:
        score += 30.0
        reasons.append("price cleared the upper bound of the central pivot zone")
    else:
        risks.append("price remains inside the central pivot zone")
    if divergence:
        score -= 18.0
        risks.append("bearish RSI divergence vetoes the breakout")
    else:
        score += 8.0
        reasons.append("no bearish divergence on the completed swing highs")
    if zone_height_pct < 12.0:
        score += 10.0
        reasons.append("the pivot zone is tight, which sharpens the breakout")
    else:
        risks.append("the pivot zone is wide and dilutes breakout quality")
    volume = volume_ratio(bars, 20)
    if volume >= 1.1:
        score += 10.0
        reasons.append("volume expands on the structural break")
    else:
        risks.append("volume is flat against the structural break")
    triggered = breakout and not divergence
    signal.metrics = {
        "zone_high": zone_high,
        "zone_low": zone_low,
        "zone_height_pct": zone_height_pct,
        "close_price": close_now,
        "atr_14": average_range,
        "rsi_14": float(rsi_now) if rsi_now is not None else 50.0,
        "volume_ratio": volume,
        "swing_high_count": float(len(swing_highs)),
        "swing_low_count": float(len(swing_lows)),
    }
    signal.reasons = tuple(reasons)
    signal.risk_factors = tuple(risks)
    return _finish(signal, score=score, triggered=triggered, average_range=average_range, rr=2.4, cap_pct=0.10, stop_pct=0.055)


def _evaluate_elliott_wave_impulse(spec: StrategySpec, symbol: str, silo: str, bars: Sequence[PriceBar]) -> StrategySignal:
    signal = _base_signal(spec, symbol=symbol, silo=silo, bars=bars)
    series = closes(bars)
    average_range = atr(bars, 14)
    swing_highs, swing_lows = _swing_points(bars, 3)
    if len(swing_highs) < 1 or len(swing_lows) < 1:
        signal.reasons = ("no impulse leg has completed",)
        signal.risk_factors = ("the wave count needs a completed impulse",)
        return _finish(signal, score=NEUTRAL_SCORE, triggered=False, average_range=average_range, rr=2.0, cap_pct=0.10, stop_pct=0.06)
    impulse_low = min(float(bars[i].low) for i in swing_lows[-2:])
    impulse_high = max(float(bars[i].high) for i in swing_highs[-2:])
    advance = impulse_high - impulse_low
    if advance <= 0.0:
        signal.reasons = ("the impulse leg has no measurable range",)
        signal.risk_factors = ("wave geometry is degenerate",)
        return _finish(signal, score=NEUTRAL_SCORE, triggered=False, average_range=average_range, rr=2.0, cap_pct=0.10, stop_pct=0.06)
    close_now = series[-1]
    retracement = (impulse_high - close_now) / advance
    fib_low = float(spec.parameters["fib_low"])
    fib_high = float(spec.parameters["fib_high"])
    extension = float(spec.parameters["extension"])
    inside_band = fib_low <= retracement <= fib_high
    holding = close_now > impulse_low
    score = 34.0
    reasons: list[str] = []
    risks: list[str] = []
    if inside_band:
        score += 28.0
        reasons.append("price retraced into the Fibonacci continuation band")
    else:
        risks.append("price is outside the Fibonacci continuation band")
    if holding:
        score += 14.0
        reasons.append("the impulse origin still holds as support")
    else:
        risks.append("price broke the impulse origin")
    projected = impulse_low + advance * extension
    headroom = _pct(projected - close_now, close_now)
    if headroom > 3.0:
        score += 12.0
        reasons.append("measured extension leaves room above")
    else:
        risks.append("the measured extension is nearly exhausted")
    volume = volume_ratio(bars, 20)
    if volume >= 1.0:
        score += 8.0
    triggered = inside_band and holding and headroom > 3.0
    signal.metrics = {
        "impulse_low": impulse_low,
        "impulse_high": impulse_high,
        "retracement_ratio": retracement,
        "fib_projection": projected,
        "projection_headroom_pct": headroom,
        "close_price": close_now,
        "atr_14": average_range,
        "volume_ratio": volume,
    }
    signal.reasons = tuple(reasons)
    signal.risk_factors = tuple(risks)
    return _finish(signal, score=score, triggered=triggered, average_range=average_range, rr=2.8, cap_pct=0.12, stop_pct=0.06)


def _evaluate_event_catalyst_momentum(spec: StrategySpec, symbol: str, silo: str, bars: Sequence[PriceBar]) -> StrategySignal:
    signal = _base_signal(spec, symbol=symbol, silo=silo, bars=bars)
    series = closes(bars)
    average_range = atr(bars, 14)
    previous_close = float(bars[-2].close)
    gap_pct = _pct(float(bars[-1].open) - previous_close, previous_close)
    gap_floor = float(spec.parameters["gap_floor_pct"])
    volume = volume_ratio(bars, 20)
    volume_floor = float(spec.parameters["volume_floor"])
    close_now = series[-1]
    holds_gap = float(bars[-1].low) >= float(bars[-1].open) * 0.995
    score = 30.0
    reasons: list[str] = []
    risks: list[str] = []
    if gap_pct >= gap_floor:
        score += 30.0
        reasons.append("the session opened above the catalyst gap floor")
    else:
        risks.append("no catalyst gap cleared the floor")
    if volume >= volume_floor:
        score += 24.0
        reasons.append("volume surged past the confirmation floor")
    else:
        risks.append("volume did not confirm the catalyst")
    if holds_gap:
        score += 10.0
        reasons.append("the gap held into the close")
    else:
        risks.append("the gap faded intraday")
    triggered = gap_pct >= gap_floor and volume >= volume_floor and holds_gap
    signal.metrics = {
        "gap_pct": gap_pct,
        "volume_ratio": volume,
        "close_price": close_now,
        "open_price": float(bars[-1].open),
        "atr_14": average_range,
        "return_5d_pct": roc(series, 5),
    }
    signal.reasons = tuple(reasons)
    signal.risk_factors = tuple(risks)
    return _finish(signal, score=score, triggered=triggered, average_range=average_range, rr=2.0, cap_pct=0.09, stop_pct=0.05)


def _fundamental_profile(symbol: str, silo: str) -> dict[str, float]:
    """Deterministic stand-in fundamental book for a symbol.

    The donor repository sources these from a vendor API.  Ring 2 is air-gapped from
    vendor sockets (``sec_004``), so the template consumes a stable, symbol-derived
    profile instead.  The numbers are a research fixture, never a market claim.
    """

    digest = hashlib.sha256(f"nios-fundamental|{silo}|{symbol}".encode("utf-8")).hexdigest()
    buckets = [int(digest[i : i + 4], 16) / 65535.0 for i in range(0, 32, 4)]
    return {
        "roe_pct": 8.0 + buckets[0] * 26.0,
        "net_margin_pct": 4.0 + buckets[1] * 24.0,
        "revenue_growth_pct": -4.0 + buckets[2] * 32.0,
        "debt_to_equity": 0.2 + buckets[3] * 1.8,
        "pe_ratio": 8.0 + buckets[4] * 34.0,
        "book_yield_pct": 2.0 + buckets[5] * 9.0,
        "quality_percentile": 20.0 + buckets[6] * 78.0,
        "valuation_percentile": 6.0 + buckets[7] * 90.0,
    }


def _evaluate_growth_quality_compounder(spec: StrategySpec, symbol: str, silo: str, bars: Sequence[PriceBar]) -> StrategySignal:
    signal = _base_signal(spec, symbol=symbol, silo=silo, bars=bars)
    series = closes(bars)
    average_range = atr(bars, 14)
    profile = _fundamental_profile(symbol, silo)
    drawdown = max_drawdown_pct(series)
    roe_floor = float(spec.parameters["roe_floor_pct"])
    margin_floor = float(spec.parameters["margin_floor_pct"])
    drawdown_ceiling = float(spec.parameters["drawdown_ceiling_pct"])
    quality_pass = (
        profile["roe_pct"] >= roe_floor
        and profile["net_margin_pct"] >= margin_floor
        and abs(drawdown) <= drawdown_ceiling
        and profile["debt_to_equity"] <= 1.6
    )
    score = 30.0
    reasons: list[str] = []
    risks: list[str] = []
    if profile["roe_pct"] >= roe_floor:
        score += 18.0
        reasons.append("return on equity clears the quality floor")
    else:
        risks.append("return on equity is below the quality floor")
    if profile["net_margin_pct"] >= margin_floor:
        score += 14.0
        reasons.append("net margin clears the profitability floor")
    else:
        risks.append("net margin is below the profitability floor")
    if abs(drawdown) <= drawdown_ceiling:
        score += 16.0
        reasons.append("realised drawdown stays inside the compounding budget")
    else:
        risks.append("realised drawdown exceeds the compounding budget")
    if profile["revenue_growth_pct"] > 0.0:
        score += 8.0
        reasons.append("revenue growth is positive")
    if profile["debt_to_equity"] <= 1.0:
        score += 8.0
        reasons.append("the balance sheet is lightly levered")
    else:
        risks.append("leverage is elevated for a compounder")
    triggered = quality_pass
    signal.metrics = {
        "roe_pct": profile["roe_pct"],
        "net_margin_pct": profile["net_margin_pct"],
        "revenue_growth_pct": profile["revenue_growth_pct"],
        "debt_to_equity": profile["debt_to_equity"],
        "quality_percentile": profile["quality_percentile"],
        "realised_drawdown_pct": drawdown,
        "close_price": series[-1],
        "atr_14": average_range,
        "return_60d_pct": roc(series, min(60, len(series) - 1)),
    }
    signal.reasons = tuple(reasons)
    signal.risk_factors = tuple(risks)
    return _finish(signal, score=score, triggered=triggered, average_range=average_range, rr=3.0, cap_pct=0.14, stop_pct=0.07)


def _evaluate_expectation_rerating(spec: StrategySpec, symbol: str, silo: str, bars: Sequence[PriceBar]) -> StrategySignal:
    signal = _base_signal(spec, symbol=symbol, silo=silo, bars=bars)
    series = closes(bars)
    average_range = atr(bars, 14)
    window = min(int(float(spec.parameters["mean_window"])), len(series))
    sample = series[-window:]
    mean = sum(sample) / float(len(sample))
    dispersion = stdev(sample)
    z_score = (series[-1] - mean) / dispersion if dispersion > 0.0 else 0.0
    z_floor = float(spec.parameters["z_floor"])
    z_ceiling = float(spec.parameters["z_ceiling"])
    profile = _fundamental_profile(symbol, silo)
    cheap = profile["valuation_percentile"] <= 45.0
    quality = profile["quality_percentile"] >= 45.0
    score = 32.0
    reasons: list[str] = []
    risks: list[str] = []
    if z_score <= z_floor:
        score += 24.0
        reasons.append("price trades below the reversion band")
    elif z_score <= z_ceiling:
        score += 10.0
        reasons.append("price is near the reversion band")
    else:
        risks.append("price is extended above the reversion band")
    if cheap:
        score += 14.0
        reasons.append("valuation percentile sits in the compressed half")
    else:
        risks.append("valuation is not compressed")
    if quality:
        score += 12.0
        reasons.append("quality percentile supports a re-rating")
    else:
        risks.append("quality does not support a re-rating")
    if profile["book_yield_pct"] >= 4.0:
        score += 8.0
        reasons.append("book yield provides a valuation floor")
    triggered = z_score <= z_ceiling and cheap and quality
    signal.metrics = {
        "z_score": z_score,
        "mean_price": mean,
        "price_dispersion": dispersion,
        "valuation_percentile": profile["valuation_percentile"],
        "quality_percentile": profile["quality_percentile"],
        "book_yield_pct": profile["book_yield_pct"],
        "pe_ratio": profile["pe_ratio"],
        "close_price": series[-1],
        "atr_14": average_range,
        "return_120d_pct": roc(series, min(120, len(series) - 1)),
    }
    signal.reasons = tuple(reasons)
    signal.risk_factors = tuple(risks)
    return _finish(signal, score=score, triggered=triggered, average_range=average_range, rr=2.8, cap_pct=0.12, stop_pct=0.07)


def _evaluate_cross_asset_etf_rotation(spec: StrategySpec, symbol: str, silo: str, bars: Sequence[PriceBar]) -> StrategySignal:
    signal = _base_signal(spec, symbol=symbol, silo=silo, bars=bars)
    series = closes(bars)
    average_range = atr(bars, 14)
    relative_window = min(int(float(spec.parameters["relative_window"])), len(series) - 1)
    vol_window = min(int(float(spec.parameters["vol_window"])), len(series) - 1)
    symbol_return = roc(series, relative_window)
    benchmark = synthetic_bars(symbol="__SILO_BENCHMARK__", silo=silo, bars=len(series), start_price=100.0)
    benchmark_series = closes(benchmark)
    benchmark_return = roc(benchmark_series, relative_window)
    relative_strength = symbol_return - benchmark_return
    returns = [series[i] / series[i - 1] - 1.0 for i in range(len(series) - vol_window, len(series)) if series[i - 1] != 0.0]
    realised_vol = stdev(returns) * 100.0
    vol_adjusted = relative_strength / realised_vol if realised_vol > 0.0 else 0.0
    score_floor = float(spec.parameters["score_floor"])
    above_anchor = series[-1] > (sum(series[-20:]) / float(min(20, len(series))))
    score = 30.0
    reasons: list[str] = []
    risks: list[str] = []
    if relative_strength > 0.0:
        score += 18.0
        reasons.append("the instrument outpaces its silo benchmark")
    else:
        risks.append("the instrument lags its silo benchmark")
    if vol_adjusted >= score_floor:
        score += 22.0
        reasons.append("volatility-adjusted relative strength clears the rotation floor")
    else:
        risks.append("volatility-adjusted relative strength is below the floor")
    if above_anchor:
        score += 12.0
        reasons.append("price holds above its short-term anchor")
    else:
        risks.append("price is below its short-term anchor")
    if realised_vol <= 3.5:
        score += 10.0
        reasons.append("realised volatility is contained")
    else:
        risks.append("realised volatility is elevated for rotation")
    triggered = relative_strength > 0.0 and vol_adjusted >= score_floor and above_anchor
    signal.metrics = {
        "relative_strength_pct": relative_strength,
        "symbol_return_pct": symbol_return,
        "benchmark_return_pct": benchmark_return,
        "realised_volatility_pct": realised_vol,
        "vol_adjusted_strength": vol_adjusted,
        "close_price": series[-1],
        "atr_14": average_range,
        "return_20d_pct": roc(series, min(20, len(series) - 1)),
    }
    signal.reasons = tuple(reasons)
    signal.risk_factors = tuple(risks)
    return _finish(signal, score=score, triggered=triggered, average_range=average_range, rr=2.4, cap_pct=0.10, stop_pct=0.055)


#: Dispatch table.  Every evaluator shares the
#: ``(spec, symbol, silo, bars) -> StrategySignal`` contract.
_EVALUATORS = {
    "ma_golden_cross": _evaluate_ma_golden_cross,
    "bullish_trend_alignment": _evaluate_bullish_trend_alignment,
    "chanlun_pivot_breakout": _evaluate_chanlun_pivot_breakout,
    "elliott_wave_impulse": _evaluate_elliott_wave_impulse,
    "event_catalyst_momentum": _evaluate_event_catalyst_momentum,
    "growth_quality_compounder": _evaluate_growth_quality_compounder,
    "expectation_rerating": _evaluate_expectation_rerating,
    "cross_asset_etf_rotation": _evaluate_cross_asset_etf_rotation,
}


def evaluate_strategy(
    strategy_id: str,
    *,
    symbol: str,
    silo: str,
    bars: Sequence[PriceBar] | None = None,
    bar_count: int = 252,
) -> StrategySignal:
    """Evaluate one template against one symbol.

    ``bars`` is used verbatim when supplied; otherwise :func:`synthetic_bars`
    reconstructs a reproducible series.  An unknown ``silo`` or ``strategy_id``
    raises ``ValueError`` so Ring 3 can answer ``422``.
    """

    if silo not in KNOWN_SILOS:
        raise ValueError(f"unknown silo: {silo!r}")
    spec = _BY_ID.get(str(strategy_id))
    if spec is None:
        raise ValueError(f"unknown strategy template: {strategy_id!r}")
    series = tuple(bars) if bars is not None else synthetic_bars(str(symbol), silo, bars=int(bar_count))
    minimum = min(int(spec.required_bars), 25)
    if len(series) < minimum:
        raise ValueError(f"strategy {strategy_id!r} needs at least {minimum} bars, received {len(series)}")
    return _EVALUATORS[spec.strategy_id](spec, str(symbol), str(silo), series)


def evaluate_all_strategies(
    *,
    symbol: str,
    silo: str,
    bars: Sequence[PriceBar] | None = None,
    bar_count: int = 252,
) -> list[StrategySignal]:
    """Evaluate every template, strongest score first (stable on ties)."""

    series = tuple(bars) if bars is not None else synthetic_bars(str(symbol), silo, bars=int(bar_count))
    signals = [
        _EVALUATORS[spec.strategy_id](spec, str(symbol), str(silo), series)
        for spec in STRATEGY_CATALOG
    ]
    return sorted(signals, key=lambda item: (-item.score, item.strategy_id))


def strategy_scores(signals: Iterable[StrategySignal]) -> dict[str, float]:
    """Compact ``{strategy_id: score}`` view used by the dashboard."""

    return {signal.strategy_id: float(signal.score) for signal in signals}
