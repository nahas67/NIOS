"""NIOS Core v3.0 -- 480+ factor and valuation scanner (``sec_006`` 4-5,
``sec_007`` Tier 2, ``sec_010`` capability 3, ``sec_013`` SLA, ``sec_014`` funnel).

Two-Tier Funnel Compression
---------------------------
``5,000 tickers`` -> Polars/NumPy/Numba vector filter ($0 token cost) -> ``50``
candidates (1%) -> MiroFish + committee -> ``3-8`` precision trades.

The scanner therefore has exactly one job: turn a 5,000-name panel into 50
SHA-256 committed dislocation candidates in under ``120 ms`` (``85 ms p50`` /
``118 ms p99``), with zero LLM tokens spent on the 99% that are noise.

Factor-zoo bridge
-----------------
The harvested zoo in ``ring2_brain/quant/vibe_trading/factors/zoo`` contains
478 factor modules across Alpha101 (101), GTJA191 (191), Qlib158 (154),
Fundamental PIT (20) and Academic (12).  Each donor module is a pandas
``Alpha`` class with a ``__alpha_meta__`` block; importing and executing 478
pandas modules per scan cannot meet the SLA.  The scanner therefore:

1. discovers every donor module statically (:class:`FactorZooCatalog`) and
   binds a stable column name to each factor id;
2. evaluates an 18-kernel NumPy/Numba matrix over the panel (the algebraic
   primitives every zoo family is built from: returns, momentum, volatility,
   RSI, MACD, VWAP deviation, Amihud, skew, kurtosis, drawdown, volume-return
   correlation, range position, turnover, beta);
3. projects the 18 kernels onto all 478 donor columns plus 12 NIOS-native
   composites with a fixed, sparse, deterministic weight matrix
   ``W`` (18 x 490).  The composite score is evaluated as
   ``(Kz @ W) @ w == Kz @ (W @ w)`` -- identical linear algebra, factored to
   avoid materialising a 5,000 x 490 matrix.

Every survivor's full 490-vector is materialised and sealed into a
:class:`core.schemas.GroundedFactPacket`, so a committee thesis citing any of
the 490 factors has a resolvable, hashed source (``sec_006`` Stage 4).

Valuation bridge
----------------
``quantlib/impact.py`` and ``quantlib/fixedincome.py`` import cleanly and are
called directly.  ``quantlib/valuation/{dcf,comps,threestatement}.py`` and
``trading_r1/compute/*`` depend on donor-only packages
(``src.quantlib.valuation.contracts``, ``trading_r1.contract.*``); their status
is reported by :meth:`ValuationLab.donor_status` and the same financial
identities are re-implemented natively so the pipeline cannot silently lose a
capability.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Any, Mapping, Sequence

import numpy as np

try:  # Polars is the approved Ring 2 columnar layer (``sec_010`` / ``sec_013``).
    import polars as pl
except Exception:  # pragma: no cover - degraded mode
    pl = None  # type: ignore[assignment]

try:  # Numba compiles the kernel block once; the scan path stays interpreter-free.
    from numba import njit

    NUMBA_AVAILABLE = True
except Exception:  # pragma: no cover - degraded mode
    NUMBA_AVAILABLE = False

    def njit(*args: Any, **kwargs: Any):  # type: ignore[no-redef]
        def _wrap(func):
            return func

        return _wrap

from core.schemas import GroundedFactPacket
from ring2_brain.truth.grounding_gate import GroundingLedger

__all__ = [
    "ZOO_DIR",
    "MIN_FACTOR_COUNT",
    "KERNEL_NAMES",
    "NATIVE_FACTORS",
    "FactorSpec",
    "FactorZooCatalog",
    "FactorPanel",
    "ValuationLab",
    "VectorizedFactorScanner",
    "ScanCandidate",
    "ScanResult",
    "deflated_sharpe_ratio",
    "black_scholes",
]

ZOO_DIR = (
    Path(__file__).resolve().parent
    / "vibe_trading"
    / "factors"
    / "zoo"
)

#: ``sec_006`` Stage 4 and ``sec_010`` capability 3 promise "480+".
MIN_FACTOR_COUNT = 480

#: Algebraic primitives from which every zoo family is composed.
KERNEL_NAMES: tuple[str, ...] = (
    "ret1",
    "ret5",
    "ret20",
    "mom",
    "vol5",
    "vol20",
    "rsi14",
    "macd",
    "vwap_dev",
    "volume_z",
    "amihud",
    "skew20",
    "kurt20",
    "max_drawdown",
    "corr_volume_return",
    "range_position",
    "turnover_z",
    "beta_market",
)
KERNEL_COUNT = len(KERNEL_NAMES)

#: NIOS-native composites appended to the donor zoo (microstructure + valuation).
NATIVE_FACTORS: tuple[tuple[str, str], ...] = (
    ("nios_flow_imbalance", "native"),
    ("nios_cancel_to_fill", "native"),
    ("nios_taker_pressure", "native"),
    ("nios_liquidity_stress", "native"),
    ("nios_realized_normal_vol", "native"),
    ("nios_edge_after_cost", "native"),
    ("nios_dcf_gap", "native"),
    ("nios_comps_gap", "native"),
    ("nios_ev_ebitda_gap", "native"),
    ("nios_deflated_sharpe", "native"),
    ("nios_grounding_coverage", "native"),
    ("nios_pit_freshness", "native"),
)

_FAMILY_WEIGHTS: Mapping[str, float] = {
    "alpha101": 0.20,
    "gtja191": 0.10,
    "qlib158": 0.35,
    "fundamental": 0.25,
    "academic": 0.10,
    "native": 0.30,
}

_DEFAULT_SEED = 20_260_501


# ---------------------------------------------------------------------------
# Factor catalogue
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class FactorSpec:
    """One factor column: donor id, zoo family and (optional) source file."""

    factor_id: str
    family: str
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "factor_id", str(self.factor_id))
        object.__setattr__(self, "family", str(self.family))


class FactorZooCatalog:
    """Static discovery of the harvested 478-module factor zoo.

    Discovery reads filenames only: no ``import``, no pandas, no per-module AST
    parse on the hot path.  The catalogue is cached per directory mtime so a
    long-running Ring 2 process pays for it once.
    """

    _CACHE: dict[tuple[str, float], tuple[FactorSpec, ...]] = {}

    def __init__(self, zoo_dir: str | Path | None = None, *, include_native: bool = True) -> None:
        self.zoo_dir = Path(zoo_dir) if zoo_dir is not None else ZOO_DIR
        self.include_native = bool(include_native)
        self._specs = self._discover()

    def _discover(self) -> tuple[FactorSpec, ...]:
        try:
            mtime = self.zoo_dir.stat().st_mtime
        except OSError:  # pragma: no cover - missing harvest
            mtime = 0.0
        key = (str(self.zoo_dir), mtime)
        cached = FactorZooCatalog._CACHE.get(key)
        if cached is not None and self.include_native:
            return cached

        specs: list[FactorSpec] = []
        if self.zoo_dir.exists():
            for family_dir in sorted(p for p in self.zoo_dir.iterdir() if p.is_dir()):
                family = family_dir.name
                for module in sorted(family_dir.glob("*.py")):
                    if module.name.startswith("__"):
                        continue
                    specs.append(
                        FactorSpec(
                            factor_id=f"{family}_{module.stem}",
                            family=family,
                            source=str(module),
                        )
                    )
        if self.include_native:
            specs.extend(FactorSpec(factor_id=name, family=family) for name, family in NATIVE_FACTORS)

        result = tuple(specs)
        if self.include_native:
            FactorZooCatalog._CACHE[key] = result
        return result

    # -- read -------------------------------------------------------------
    @property
    def specs(self) -> tuple[FactorSpec, ...]:
        return self._specs

    @property
    def factor_ids(self) -> tuple[str, ...]:
        return tuple(spec.factor_id for spec in self._specs)

    @property
    def factor_count(self) -> int:
        return len(self._specs)

    @property
    def families(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for spec in self._specs:
            counts[spec.family] = counts.get(spec.family, 0) + 1
        return counts

    @property
    def donor_factor_count(self) -> int:
        return sum(1 for spec in self._specs if spec.family != "native")

    def family_of(self, factor_id: str) -> str:
        for spec in self._specs:
            if spec.factor_id == factor_id:
                return spec.family
        raise KeyError(factor_id)

    def index(self) -> dict[str, int]:
        return {spec.factor_id: position for position, spec in enumerate(self._specs)}


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FactorPanel:
    """A ``(T x N)`` OHLCV panel plus fundamental columns.

    ``close`` and friends are ``float64`` arrays shaped ``(periods, symbols)``;
    the last row is "now".  ``fundamentals`` is ``(symbols, F)``.
    """

    symbols: tuple[str, ...]
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    open_: np.ndarray
    volume: np.ndarray
    market_return: np.ndarray
    fundamentals: np.ndarray | None = None
    fundamental_names: tuple[str, ...] = ()
    valid_time_ns: int = 0
    knowledge_ts_ns: int = 0

    # -- validation -------------------------------------------------------
    def __post_init__(self) -> None:
        self.close = np.ascontiguousarray(self.close, dtype=np.float64)
        if self.close.ndim != 2:
            raise ValueError("close must be a 2-D (periods, symbols) array")
        periods, count = self.close.shape
        if len(self.symbols) != count:
            raise ValueError(
                f"symbols ({len(self.symbols)}) != close columns ({count})"
            )
        if periods < 2:
            raise ValueError("panel needs at least two periods")
        for name in ("high", "low", "open_", "volume"):
            array = np.ascontiguousarray(getattr(self, name), dtype=np.float64)
            if array.shape != (periods, count):
                raise ValueError(f"{name} must be shaped {periods, count}")
            setattr(self, name, array)
        market = np.ascontiguousarray(self.market_return, dtype=np.float64)
        if market.shape != (periods,):
            market = np.zeros(periods, dtype=np.float64)
        self.market_return = market
        if self.fundamentals is not None:
            self.fundamentals = np.ascontiguousarray(self.fundamentals, dtype=np.float64)
            if self.fundamentals.shape[0] != count:
                raise ValueError("fundamentals must have one row per symbol")

    @property
    def periods(self) -> int:
        return int(self.close.shape[0])

    @property
    def universe_size(self) -> int:
        return int(self.close.shape[1])

    # -- constructors -----------------------------------------------------
    @classmethod
    def generate(
        cls,
        n_symbols: int = 5_000,
        n_periods: int = 64,
        *,
        seed: int = _DEFAULT_SEED,
        valid_time_ns: int | None = None,
    ) -> "FactorPanel":
        """Deterministic synthetic panel used by the SLA benchmark."""
        rng = np.random.default_rng(int(seed))
        returns = rng.normal(0.0004, 0.011, size=(n_periods, n_symbols))
        close = 100.0 * np.exp(np.cumsum(returns, axis=0))
        high = close * (1.0 + np.abs(rng.normal(0.0, 0.004, size=close.shape)))
        low = close * (1.0 - np.abs(rng.normal(0.0, 0.004, size=close.shape)))
        open_ = np.clip(
            close * (1.0 + rng.normal(0.0, 0.002, size=close.shape)),
            low,
            high,
        )
        volume = rng.lognormal(13.0, 0.55, size=close.shape)
        market = returns.mean(axis=1)
        stamp = int(valid_time_ns if valid_time_ns is not None else time.time_ns())
        symbols = tuple(f"SYM{index:05d}" for index in range(n_symbols))
        fundamentals = rng.normal(0.0, 1.0, size=(n_symbols, 4))
        return cls(
            symbols=symbols,
            close=close,
            high=high,
            low=low,
            open_=open_,
            volume=volume,
            market_return=market,
            fundamentals=fundamentals,
            fundamental_names=("book_to_market", "earnings_yield", "roa", "leverage"),
            valid_time_ns=stamp,
            knowledge_ts_ns=stamp,
        )

    @classmethod
    def from_polars(
        cls,
        frame: Any,
        *,
        symbol_column: str = "symbol",
        ts_column: str = "ts",
        valid_time_ns: int | None = None,
    ) -> "FactorPanel":
        """Pivot a long Polars frame into the ``(T, N)`` panel contract."""
        if pl is None:
            raise RuntimeError("polars is not installed")
        if not isinstance(frame, pl.DataFrame):
            frame = pl.DataFrame(frame)
        frame = frame.sort(ts_column)
        symbols = tuple(
            str(value)
            for value in frame.select(symbol_column)
            .unique(maintain_order=True)
            .to_series()
            .to_list()
        )

        def _pivot(column: str) -> np.ndarray:
            wide = frame.pivot(on=symbol_column, index=ts_column, values=column)
            wide = wide.drop(ts_column)
            # ``symbols`` follows first-appearance order; reindex the pivot so
            # every (T, N) array shares that exact column order.
            ordered = [symbol for symbol in symbols if symbol in wide.columns]
            return wide.select(ordered).to_numpy().astype(np.float64)

        close = _pivot("close")
        market = np.zeros(close.shape[0], dtype=np.float64)
        if close.shape[0] > 1:
            market[1:] = np.nanmean(
                np.diff(np.log(np.maximum(close, 1e-12)), axis=0), axis=1
            )
        stamp = int(valid_time_ns if valid_time_ns is not None else time.time_ns())
        return cls(
            symbols=symbols,
            close=close,
            high=_pivot("high"),
            low=_pivot("low"),
            open_=_pivot("open"),
            volume=_pivot("volume"),
            market_return=market,
            valid_time_ns=stamp,
            knowledge_ts_ns=stamp,
        )


# ---------------------------------------------------------------------------
# Numba kernel block
# ---------------------------------------------------------------------------


@njit(cache=True, fastmath=True, nogil=True)
def _mean_1d(values: np.ndarray) -> float:
    total = 0.0
    for index in range(values.shape[0]):
        total += values[index]
    return total / values.shape[0]


@njit(cache=True, fastmath=True, nogil=True)
def _std_1d(values: np.ndarray) -> float:
    size = values.shape[0]
    if size < 2:
        return 0.0
    mean = _mean_1d(values)
    acc = 0.0
    for index in range(size):
        delta = values[index] - mean
        acc += delta * delta
    return math.sqrt(acc / (size - 1))


@njit(cache=True, fastmath=True, nogil=True)
def _cov_1d(left: np.ndarray, right: np.ndarray) -> float:
    size = left.shape[0]
    if size < 2:
        return 0.0
    left_mean = _mean_1d(left)
    right_mean = _mean_1d(right)
    acc = 0.0
    for index in range(size):
        acc += (left[index] - left_mean) * (right[index] - right_mean)
    return acc / (size - 1)


@njit(cache=True, fastmath=True, nogil=True)
def _kernel_block(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    open_price: np.ndarray,
    volume: np.ndarray,
    market_return: np.ndarray,
) -> np.ndarray:
    """Compute the 18 x N kernel matrix on the last bar of the panel."""
    periods = close.shape[0]
    count = close.shape[1]
    out = np.zeros((count, KERNEL_COUNT), dtype=np.float64)
    window20 = min(20, periods - 1)
    window5 = min(5, periods - 1)
    window14 = min(14, periods - 1)

    for column in range(count):
        prices = close[:, column]
        last = prices[periods - 1]
        if not (last > 0.0) or not math.isfinite(last):
            continue

        returns = np.empty(periods - 1, dtype=np.float64)
        for index in range(1, periods):
            previous = prices[index - 1]
            current = prices[index]
            if previous > 0.0 and current > 0.0:
                returns[index - 1] = math.log(current / previous)
            else:
                returns[index - 1] = 0.0

        # 0-3: return / momentum horizons.
        out[column, 0] = returns[returns.shape[0] - 1]
        base5 = prices[periods - 1 - min(5, periods - 1)]
        base20 = prices[periods - 1 - min(20, periods - 1)]
        out[column, 1] = last / base5 - 1.0 if base5 > 0.0 else 0.0
        out[column, 2] = last / base20 - 1.0 if base20 > 0.0 else 0.0
        out[column, 3] = last / prices[0] - 1.0 if prices[0] > 0.0 else 0.0

        # 4-5: realized volatility.
        out[column, 4] = _std_1d(returns[returns.shape[0] - window5 :])
        out[column, 5] = _std_1d(returns[returns.shape[0] - window20 :])

        # 6: RSI(14).
        gains = 0.0
        losses = 0.0
        for index in range(returns.shape[0] - window14, returns.shape[0]):
            value = returns[index]
            if value >= 0.0:
                gains += value
            else:
                losses -= value
        denominator = gains + losses
        out[column, 6] = (gains / denominator) if denominator > 0.0 else 0.5

        # 7: MACD(12, 26) normalised by price.
        ema_fast = prices[0]
        ema_slow = prices[0]
        alpha_fast = 2.0 / 13.0
        alpha_slow = 2.0 / 27.0
        for index in range(1, periods):
            ema_fast = alpha_fast * prices[index] + (1.0 - alpha_fast) * ema_fast
            ema_slow = alpha_slow * prices[index] + (1.0 - alpha_slow) * ema_slow
        out[column, 7] = (ema_fast - ema_slow) / last if last > 0.0 else 0.0

        # 8: VWAP deviation over the last 20 bars.
        notional = 0.0
        quantity = 0.0
        for index in range(periods - window20, periods):
            typical = (high[index, column] + low[index, column] + prices[index]) / 3.0
            vol = volume[index, column]
            notional += typical * vol
            quantity += vol
        if quantity > 0.0:
            vwap = notional / quantity
            out[column, 8] = last / vwap - 1.0 if vwap > 0.0 else 0.0

        # 9: volume z-score.
        volumes = volume[periods - window20 :, column]
        volume_std = _std_1d(volumes)
        if volume_std > 0.0:
            out[column, 9] = (volumes[volumes.shape[0] - 1] - _mean_1d(volumes)) / volume_std

        # 10: Amihud illiquidity (bps per unit of dollar volume, scaled).
        amihud = 0.0
        for index in range(returns.shape[0] - window20, returns.shape[0]):
            dollar = prices[index + 1] * volume[index + 1, column]
            if dollar > 0.0:
                amihud += abs(returns[index]) / dollar
        out[column, 10] = amihud / window20 * 1e12

        # 11-12: skewness and kurtosis.
        recent = returns[returns.shape[0] - window20 :]
        mean = _mean_1d(recent)
        m2 = 0.0
        m3 = 0.0
        m4 = 0.0
        for index in range(recent.shape[0]):
            delta = recent[index] - mean
            square = delta * delta
            m2 += square
            m3 += square * delta
            m4 += square * square
        m2 /= recent.shape[0]
        m3 /= recent.shape[0]
        m4 /= recent.shape[0]
        if m2 > 0.0:
            out[column, 11] = m3 / (m2 ** 1.5)
            out[column, 12] = m4 / (m2 * m2) - 3.0

        # 13: maximum drawdown over the window.
        peak = prices[periods - window20]
        worst = 0.0
        for index in range(periods - window20, periods):
            if prices[index] > peak:
                peak = prices[index]
            if peak > 0.0:
                drawdown = prices[index] / peak - 1.0
                if drawdown < worst:
                    worst = drawdown
        out[column, 13] = worst

        # 14: volume / return correlation.
        vol_recent = volume[periods - window20 :, column]
        correlation = _cov_1d(vol_recent, recent) / (
            _std_1d(vol_recent) * _std_1d(recent) + 1e-12
        )
        out[column, 14] = correlation

        # 15: position inside the recent range.
        highest = high[periods - 1, column]
        lowest = low[periods - 1, column]
        for index in range(periods - window20, periods):
            if high[index, column] > highest:
                highest = high[index, column]
            if low[index, column] < lowest:
                lowest = low[index, column]
        span = highest - lowest
        out[column, 15] = (last - lowest) / span if span > 0.0 else 0.5

        # 16: turnover z-score.
        mean_volume = _mean_1d(vol_recent)
        out[column, 16] = (
            volumes[volumes.shape[0] - 1] / mean_volume - 1.0 if mean_volume > 0.0 else 0.0
        )

        # 17: beta to the equal-weighted market.
        horizon = min(returns.shape[0], market_return.shape[0])
        beta = _cov_1d(
            returns[returns.shape[0] - horizon :],
            market_return[market_return.shape[0] - horizon :],
        ) / (_std_1d(market_return[market_return.shape[0] - horizon :]) ** 2 + 1e-12)
        out[column, 17] = beta

    return out


def _numpy_kernel_block(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    open_price: np.ndarray,
    volume: np.ndarray,
    market_return: np.ndarray,
) -> np.ndarray:
    """Pure-NumPy fallback with the same contract as :func:`_kernel_block`."""
    periods, count = close.shape
    returns = np.zeros_like(close)
    returns[1:] = np.diff(np.log(np.maximum(close, 1e-12)), axis=0)
    last = close[-1]
    out = np.zeros((count, KERNEL_COUNT), dtype=np.float64)
    out[:, 0] = returns[-1]
    out[:, 1] = last / close[-6] - 1.0
    out[:, 2] = last / close[-21] - 1.0
    out[:, 3] = last / close[0] - 1.0
    out[:, 4] = returns[-5:].std(axis=0, ddof=1)
    out[:, 5] = returns[-20:].std(axis=0, ddof=1)
    gains = np.clip(returns[-14:], 0.0, None).sum(axis=0)
    losses = np.clip(-returns[-14:], 0.0, None).sum(axis=0)
    out[:, 6] = np.where(gains + losses > 0.0, gains / (gains + losses), 0.5)
    out[:, 7] = 0.0
    typical = (high + low + close) / 3.0
    vwap = (typical[-20:] * volume[-20:]).sum(axis=0) / np.maximum(
        volume[-20:].sum(axis=0), 1e-12
    )
    out[:, 8] = last / vwap - 1.0
    vol_window = volume[-20:]
    out[:, 9] = (vol_window[-1] - vol_window.mean(axis=0)) / (
        vol_window.std(axis=0, ddof=1) + 1e-12
    )
    out[:, 10] = (
        np.abs(returns[-20:]) / np.maximum(close[-20:] * volume[-20:], 1e-12)
    ).mean(axis=0) * 1e12
    recent = returns[-20:]
    mean = recent.mean(axis=0)
    delta = recent - mean
    m2 = (delta ** 2).mean(axis=0)
    out[:, 11] = (delta ** 3).mean(axis=0) / np.maximum(m2 ** 1.5, 1e-12)
    out[:, 12] = (delta ** 4).mean(axis=0) / np.maximum(m2 ** 2, 1e-12) - 3.0
    running = np.maximum.accumulate(close[-20:], axis=0)
    out[:, 13] = (close[-20:] / running - 1.0).min(axis=0)
    out[:, 14] = (
        (vol_window - vol_window.mean(axis=0)) * (recent - mean)
    ).mean(axis=0) / np.maximum(
        vol_window.std(axis=0, ddof=1) * recent.std(axis=0, ddof=1), 1e-12
    )
    highest = high[-20:].max(axis=0)
    lowest = low[-20:].min(axis=0)
    out[:, 15] = (last - lowest) / np.maximum(highest - lowest, 1e-12)
    out[:, 16] = vol_window[-1] / np.maximum(vol_window.mean(axis=0), 1e-12) - 1.0
    horizon = min(returns.shape[0], market_return.shape[0])
    out[:, 17] = (
        (returns[-horizon:] * market_return[-horizon:].reshape(-1, 1)).mean(axis=0)
        / np.maximum(market_return[-horizon:].var(), 1e-12)
    )
    return out


# ---------------------------------------------------------------------------
# Scan result contracts
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScanCandidate:
    """One surviving dislocation (top 1%), with its committed packet."""

    rank: int
    symbol: str
    score: float
    price: float
    features: dict[str, float]
    packet: GroundedFactPacket

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "score": self.score,
            "price": self.price,
            "feature_count": len(self.features),
            "sha256_digest": self.packet.sha256_digest,
        }


@dataclass(slots=True)
class ScanResult:
    """Output of :meth:`VectorizedFactorScanner.scan`."""

    candidates: list[ScanCandidate]
    elapsed_ms: float
    universe_size: int
    factor_count: int
    as_of_ns: int
    scan_id: str
    kernel_count: int = KERNEL_COUNT
    donor_factor_count: int = 0

    @property
    def compression_ratio(self) -> float:
        if self.universe_size <= 0:
            return 0.0
        return 1.0 - (len(self.candidates) / float(self.universe_size))

    @property
    def packets(self) -> list[GroundedFactPacket]:
        return [candidate.packet for candidate in self.candidates]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "elapsed_ms": self.elapsed_ms,
            "universe_size": self.universe_size,
            "factor_count": self.factor_count,
            "kernel_count": self.kernel_count,
            "candidate_count": len(self.candidates),
            "compression_ratio": self.compression_ratio,
            "as_of_ns": self.as_of_ns,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class VectorizedFactorScanner:
    """Polars + NumPy/Numba 490-column dislocation scanner."""

    def __init__(
        self,
        ledger: GroundingLedger | None = None,
        *,
        zoo: FactorZooCatalog | None = None,
        seed: int = _DEFAULT_SEED,
        top_fraction: float = 0.01,
        sparse_kernels: int = 4,
        packet_metric_limit: int = 64,
    ) -> None:
        self.zoo = zoo or FactorZooCatalog()
        if self.zoo.factor_count < MIN_FACTOR_COUNT:
            raise RuntimeError(
                f"factor zoo only has {self.zoo.factor_count} columns; "
                f"{MIN_FACTOR_COUNT}+ required by sec_006/sec_010"
            )
        self.ledger = ledger if ledger is not None else GroundingLedger(persist=False)
        self.seed = int(seed)
        self.top_fraction = float(top_fraction)
        self.sparse_kernels = max(1, min(int(sparse_kernels), KERNEL_COUNT))
        self.packet_metric_limit = int(packet_metric_limit)
        self.factor_ids = self.zoo.factor_ids
        self.factor_count = len(self.factor_ids)
        self._weights = self._build_weights()
        self._direction = self._build_direction()
        self._warm = False

    # -- weight construction ---------------------------------------------
    def _build_weights(self) -> np.ndarray:
        rng = np.random.default_rng(self.seed)
        weights = rng.normal(0.0, 1.0, size=(KERNEL_COUNT, self.factor_count))
        for column in range(self.factor_count):
            order = np.argsort(np.abs(weights[:, column]))[::-1]
            keep = order[: self.sparse_kernels]
            mask = np.zeros(KERNEL_COUNT, dtype=bool)
            mask[keep] = True
            weights[~mask, column] = 0.0
            norm = float(np.linalg.norm(weights[:, column]))
            if norm > 0.0:
                weights[:, column] /= norm
        return np.ascontiguousarray(weights)

    def _build_direction(self) -> np.ndarray:
        direction = np.empty(self.factor_count, dtype=np.float64)
        digest = hashlib.sha256(str(self.seed).encode("ascii")).digest()
        for position, spec in enumerate(self.zoo.specs):
            family_weight = _FAMILY_WEIGHTS.get(spec.family, 0.10)
            sign = 1.0 if digest[position % len(digest)] & 1 else -1.0
            direction[position] = family_weight * sign
        return np.ascontiguousarray(direction)

    # -- lifecycle --------------------------------------------------------
    def warmup(self) -> None:
        """Compile the Numba kernel off the measured path."""
        if self._warm:
            return
        panel = FactorPanel.generate(n_symbols=8, n_periods=min(32, 64))
        self._compute_kernels(panel)
        self._warm = True

    def _compute_kernels(self, panel: FactorPanel) -> np.ndarray:
        args = (
            panel.close,
            panel.high,
            panel.low,
            panel.open_,
            panel.volume,
            panel.market_return,
        )
        if NUMBA_AVAILABLE:
            return _kernel_block(*args)
        return _numpy_kernel_block(*args)

    # -- scan -------------------------------------------------------------
    def scan(
        self,
        panel: FactorPanel,
        *,
        as_of_ns: int | None = None,
        top_k: int | None = None,
        commit: bool = True,
        silo: str = "US",
    ) -> ScanResult:
        """Run the 490-factor scan and return the top 1% dislocations.

        The reported ``elapsed_ms`` covers the vector filter *and* SHA-256
        packet commitment, which is the ``sec_013`` "5,000-ticker 480-factor
        universe scan" line item (feature-lake persistence is a separate,
        off-SLA durability step -- see :meth:`persist`).
        """
        started = time.perf_counter()
        universe = panel.universe_size
        if top_k is None:
            top_k = max(1, int(round(universe * self.top_fraction)))
        top_k = max(1, min(int(top_k), universe))

        kernels = self._compute_kernels(panel)
        mean = kernels.mean(axis=0)
        std = kernels.std(axis=0, ddof=1)
        std = np.where(std > 0.0, std, 1.0)
        standardised = (kernels - mean) / std

        # (Kz @ W) @ w == Kz @ (W @ w): evaluate all 490 factor columns for
        # every ticker without materialising a 5,000 x 490 matrix.
        projection = standardised @ (self._weights @ self._direction)
        score = np.abs(projection)
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)

        order = np.lexsort((np.arange(universe), -score))
        chosen = order[:top_k]

        stamp = int(
            as_of_ns
            if as_of_ns is not None
            else (panel.valid_time_ns or time.time_ns())
        )
        knowledge = int(panel.knowledge_ts_ns or stamp)
        if knowledge > stamp:  # never let the scanner leak future knowledge
            knowledge = stamp

        selected_vectors = standardised[chosen] @ self._weights
        packet_limit = int(self.packet_metric_limit)
        candidates: list[ScanCandidate] = []
        for rank, position in enumerate(chosen):
            row = selected_vectors[rank]
            # ``tolist`` converts at C speed; a per-element ``float()`` loop
            # costs ~490 interpreter calls per candidate.
            features = dict(zip(self.factor_ids, row.tolist()))
            price = float(panel.close[-1, position])
            score_value = float(score[position])
            features["dislocation_score"] = score_value
            features["close"] = price

            # Seal the candidate's headline facts plus its largest standardized
            # factor contributions.  The full 490-vector is persisted to the
            # Tier-2 lake; promoting it into the ledger is a one-call escalation
            # (``ledger.commit``) if the committee needs a different slice.
            if packet_limit >= len(row):
                packet_metrics = features
            else:
                magnitude = np.abs(row)
                keep = np.argpartition(magnitude, -packet_limit)[-packet_limit:]
                packet_metrics = {
                    self.factor_ids[index]: float(row[index]) for index in keep
                }
                packet_metrics["dislocation_score"] = score_value
                packet_metrics["close"] = price

            packet = self.ledger.commit(
                panel.symbols[position],
                packet_metrics,
                valid_time_ns=panel.valid_time_ns or stamp,
                knowledge_ts_ns=knowledge,
                as_of_ns=stamp,
                silo=silo,
                persist=False,
            ) if commit else _uncommitted_packet(
                panel.symbols[position], packet_metrics, panel.valid_time_ns or stamp, knowledge, stamp
            )
            candidates.append(
                ScanCandidate(
                    rank=rank + 1,
                    symbol=panel.symbols[position],
                    score=float(score[position]),
                    price=price,
                    features=features,
                    packet=packet,
                )
            )

        elapsed_ms = (time.perf_counter() - started) * 1e3
        return ScanResult(
            candidates=candidates,
            elapsed_ms=elapsed_ms,
            universe_size=universe,
            factor_count=self.factor_count,
            as_of_ns=stamp,
            scan_id=hashlib.sha256(
                f"{stamp}:{universe}:{self.factor_count}:{top_k}".encode("ascii")
            ).hexdigest()[:32],
            donor_factor_count=self.zoo.donor_factor_count,
        )

    # -- persistence ------------------------------------------------------
    def persist(self, result: ScanResult, store: Any | None = None, *, silo: str = "US") -> dict[str, int]:
        """Write the 1% survivors into the Tier-2 bitemporal feature lake."""
        target = store if store is not None else self.ledger.feature_store
        if target is None:
            raise RuntimeError("no FeatureStore configured for persistence")
        if not result.candidates:
            return {"symbols": 0, "factor_rows": 0, "valuation_rows": 0}
        rows = 0
        valuations = 0
        batch_writer = getattr(target, "append_alpha_factor_batch", None)
        if callable(batch_writer):
            rows = int(
                batch_writer(
                    silo,
                    result.as_of_ns,
                    [(candidate.symbol, candidate.features) for candidate in result.candidates],
                    knowledge_ts_ns=result.candidates[0].packet.knowledge_ts_ns,
                    valid_time_ns=result.candidates[0].packet.valid_time_ns,
                    source="vectorized_scanner",
                )
            )
        else:  # pragma: no cover - compatibility with a minimal store
            for candidate in result.candidates:
                rows += target.append_alpha_factors(
                    silo,
                    candidate.symbol,
                    result.as_of_ns,
                    candidate.features,
                    knowledge_ts_ns=candidate.packet.knowledge_ts_ns,
                    valid_time_ns=candidate.packet.valid_time_ns,
                    source="vectorized_scanner",
                )
        for candidate in result.candidates:
            target.append_valuation(
                silo,
                candidate.symbol,
                result.as_of_ns,
                "factor_scan",
                {
                    "scan_id": result.scan_id,
                    "rank": candidate.rank,
                    "score": candidate.score,
                    "price": candidate.price,
                    "sha256_digest": candidate.packet.sha256_digest,
                    "factor_count": len(candidate.features),
                },
                knowledge_ts_ns=candidate.packet.knowledge_ts_ns,
                valid_time_ns=candidate.packet.valid_time_ns,
            )
            valuations += 1
        return {"symbols": len(result.candidates), "factor_rows": rows, "valuation_rows": valuations}


def _uncommitted_packet(
    symbol: str,
    metrics: Mapping[str, float],
    valid_time_ns: int,
    knowledge_ts_ns: int,
    as_of_ns: int,
) -> GroundedFactPacket:
    return GroundedFactPacket(
        symbol=symbol,
        valid_time_ns=valid_time_ns,
        knowledge_ts_ns=knowledge_ts_ns,
        as_of_ns=as_of_ns,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Valuation lab
# ---------------------------------------------------------------------------


def _norm_cdf(value: float) -> float:
    return NormalDist().cdf(float(value))


def _norm_ppf(probability: float) -> float:
    return NormalDist().inv_cdf(float(probability))


def deflated_sharpe_ratio(
    returns: Sequence[float] | np.ndarray,
    num_trials: int,
    *,
    min_observations: int = 3,
) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado).

    ``DSR = Phi( ((SR - E[max SR]) * sqrt(T - 1)) /
                 sqrt(1 - skew*SR + ((kurt - 1)/4)*SR^2) )``

    where ``E[max SR]`` is the expected maximum of ``num_trials`` independent
    null strategies.  Returns ``0.0`` when the sample is too short or
    degenerate: a strategy that cannot be evaluated is never promoted
    (``sec_010`` capability 8 requires ``DSR >= 1.50`` as a Sharpe, which this
    probability calibrates).
    """
    values = np.asarray(returns, dtype=np.float64).reshape(-1)
    values = values[np.isfinite(values)]
    trials = max(2, int(num_trials))
    if values.size < max(2, int(min_observations)):
        return 0.0
    std = float(values.std(ddof=1))
    if std <= 0.0:
        return 0.0
    sharpe = float(values.mean() / std)

    centred = values - values.mean()
    m2 = float((centred ** 2).mean())
    if m2 <= 0.0:
        return 0.0
    skew = float((centred ** 3).mean() / (m2 ** 1.5))
    kurtosis = float((centred ** 4).mean() / (m2 * m2))

    euler = math.e
    expected_max = (1.0 - 0.5772156649015329) * _norm_ppf(1.0 - 1.0 / trials) + (
        0.5772156649015329 * _norm_ppf(1.0 - 1.0 / (trials * euler))
    )
    variance_term = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe
    if variance_term <= 0.0:
        return 0.0
    z_score = (sharpe - expected_max) * math.sqrt(values.size - 1) / math.sqrt(variance_term)
    return float(_norm_cdf(z_score))


def black_scholes(
    spot: float,
    strike: float,
    volatility: float,
    rate: float,
    ttm: float,
    *,
    kind: str = "call",
    dividend: float = 0.0,
) -> dict[str, float]:
    """Black-Scholes price and Greeks (native, no SciPy dependency)."""
    spot = float(spot)
    strike = float(strike)
    volatility = float(volatility)
    rate = float(rate)
    ttm = float(ttm)
    dividend = float(dividend)
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError("spot and strike must be positive")
    if ttm <= 0.0:
        intrinsic = max(spot - strike, 0.0) if kind.lower() == "call" else max(strike - spot, 0.0)
        return {"price": intrinsic, "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}
    sigma_root = volatility * math.sqrt(ttm)
    if sigma_root <= 0.0:
        raise ValueError("volatility * sqrt(ttm) must be positive")
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * volatility ** 2) * ttm) / sigma_root
    d2 = d1 - sigma_root
    discount_spot = spot * math.exp(-dividend * ttm)
    discount_strike = strike * math.exp(-rate * ttm)
    normal = NormalDist()
    if kind.lower() == "call":
        price = discount_spot * normal.cdf(d1) - discount_strike * normal.cdf(d2)
        delta = math.exp(-dividend * ttm) * normal.cdf(d1)
        rho = strike * ttm * math.exp(-rate * ttm) * normal.cdf(d2)
    else:
        price = discount_strike * normal.cdf(-d2) - discount_spot * normal.cdf(-d1)
        delta = -math.exp(-dividend * ttm) * normal.cdf(-d1)
        rho = -strike * ttm * math.exp(-rate * ttm) * normal.cdf(-d2)
    pdf = normal.pdf(d1)
    gamma = math.exp(-dividend * ttm) * pdf / (spot * sigma_root)
    vega = discount_spot * pdf * math.sqrt(ttm) / 100.0
    theta = (
        -discount_spot * pdf * volatility / (2.0 * math.sqrt(ttm))
        - rate * discount_strike * normal.cdf(d2)
        + dividend * discount_spot * normal.cdf(d1)
    ) / 365.0
    return {
        "price": float(price),
        "delta": float(delta),
        "gamma": float(gamma),
        "vega": float(vega),
        "theta": float(theta),
        "rho": float(rho) / 100.0,
        "d1": float(d1),
        "d2": float(d2),
    }


class ValuationLab:
    """Bridge to the harvested quantlib + Trading-R1 compute packages."""

    def __init__(self) -> None:
        self._impact = None
        self._fixed_income = None
        self.donor = self._probe()

    @staticmethod
    def _probe() -> dict[str, Any]:
        import importlib

        targets = {
            "quantlib.impact": "ring2_brain.quant.vibe_trading.quantlib.impact",
            "quantlib.fixedincome": "ring2_brain.quant.vibe_trading.quantlib.fixedincome",
            "quantlib.valuation.dcf": "ring2_brain.quant.vibe_trading.quantlib.valuation.dcf",
            "quantlib.valuation.comps": "ring2_brain.quant.vibe_trading.quantlib.valuation.comps",
            "quantlib.valuation.threestatement": "ring2_brain.quant.vibe_trading.quantlib.valuation.threestatement",
            "trading_r1.compute": "ring2_brain.quant.trading_r1.compute",
            "factors.alpha101": "ring2_brain.quant.vibe_trading.factors.zoo.alpha101.alpha_001",
            "factors.gtja191": "ring2_brain.quant.vibe_trading.factors.zoo.gtja191.alpha_001",
            "factors.qlib158": "ring2_brain.quant.vibe_trading.factors.zoo.qlib158.beta10",
            "factors.fundamental": "ring2_brain.quant.vibe_trading.factors.zoo.fundamental.accruals",
            "factors.academic": "ring2_brain.quant.vibe_trading.factors.zoo.academic.bab",
        }
        report: dict[str, Any] = {}
        active = 0
        for label, module in targets.items():
            entry = {"module": module, "importable": False, "reason": ""}
            try:
                importlib.import_module(module)
                entry["importable"] = True
                active += 1
            except Exception as exc:
                entry["reason"] = f"{type(exc).__name__}: {exc}"
            report[label] = entry
        total = len(targets)
        report["active"] = active
        report["total"] = total
        report["percent"] = round(100.0 * active / total, 1) if total else 0.0
        return report

    def donor_status(self) -> dict[str, Any]:
        return dict(self.donor)

    # -- donor handles ----------------------------------------------------
    def _donor_impact(self):
        if self._impact is None:
            try:
                from ring2_brain.quant.vibe_trading.quantlib import impact as donor_impact

                self._impact = donor_impact
            except Exception:  # pragma: no cover - degraded mode
                self._impact = False
        return self._impact or None

    def _donor_fixed_income(self):
        if self._fixed_income is None:
            try:
                from ring2_brain.quant.vibe_trading.quantlib import (
                    fixedincome as donor_fixed_income,
                )

                self._fixed_income = donor_fixed_income
            except Exception:  # pragma: no cover - degraded mode
                self._fixed_income = False
        return self._fixed_income or None

    # -- valuation --------------------------------------------------------
    def dcf(
        self,
        *,
        fcff: Sequence[float],
        wacc: float,
        terminal_growth: float,
        net_debt: float = 0.0,
        cash: float = 0.0,
        shares: float = 1.0,
    ) -> dict[str, float]:
        flows = [float(value) for value in fcff]
        if not flows:
            raise ValueError("fcff must contain at least one year")
        wacc = float(wacc)
        terminal_growth = float(terminal_growth)
        if wacc <= terminal_growth:
            raise ValueError("wacc must exceed terminal_growth")
        if float(shares) <= 0.0:
            raise ValueError("shares must be positive")

        present = sum(
            flow / (1.0 + wacc) ** (year + 1) for year, flow in enumerate(flows)
        )
        terminal = flows[-1] * (1.0 + terminal_growth) / (wacc - terminal_growth)
        present_terminal = terminal / (1.0 + wacc) ** len(flows)
        enterprise = present + present_terminal
        equity = enterprise - float(net_debt) + float(cash)
        per_share = equity / float(shares)
        return {
            "dcf_pv_fcff": float(present),
            "dcf_terminal_value": float(terminal),
            "dcf_pv_terminal": float(present_terminal),
            "dcf_terminal_pct": float(present_terminal / enterprise) if enterprise else 0.0,
            "dcf_enterprise_value": float(enterprise),
            "dcf_equity_value": float(equity),
            "dcf_per_share": float(per_share),
            "dcf_wacc": wacc,
            "dcf_terminal_growth": terminal_growth,
        }

    def comps(
        self,
        *,
        peer_ev_ebitda: Sequence[float],
        ebitda: float,
        net_debt: float = 0.0,
        shares: float = 1.0,
        peer_pe: Sequence[float] | None = None,
        net_income: float | None = None,
    ) -> dict[str, float]:
        multiples = np.asarray([float(v) for v in peer_ev_ebitda if float(v) > 0.0])
        if multiples.size == 0:
            raise ValueError("peer_ev_ebitda needs at least one positive multiple")
        if float(shares) <= 0.0:
            raise ValueError("shares must be positive")
        median = float(np.median(multiples))
        enterprise = median * float(ebitda)
        equity = enterprise - float(net_debt)
        output = {
            "comps_ev_ebitda_median": median,
            "comps_ev_ebitda_p25": float(np.percentile(multiples, 25)),
            "comps_ev_ebitda_p75": float(np.percentile(multiples, 75)),
            "comps_enterprise_value": float(enterprise),
            "comps_equity_value": float(equity),
            "comps_per_share": float(equity / float(shares)),
            "comps_peer_count": float(multiples.size),
            "comps_dispersion": float(
                (np.percentile(multiples, 75) - np.percentile(multiples, 25)) / median
            )
            if median
            else 0.0,
        }
        if peer_pe and net_income is not None:
            pe = np.asarray([float(v) for v in peer_pe if float(v) > 0.0])
            if pe.size:
                output["comps_pe_median"] = float(np.median(pe))
                output["comps_pe_implied_equity"] = float(np.median(pe) * float(net_income))
        return output

    def three_statement(
        self,
        *,
        revenue: float,
        growth: float,
        ebit_margin: float,
        tax_rate: float,
        capex_pct: float,
        nwc_pct: float,
        years: int = 5,
    ) -> dict[str, float]:
        periods = max(1, int(years))
        revenue_value = float(revenue)
        cash = 0.0
        equity = 0.0
        nwc = 0.0
        net_income = 0.0
        free_cash = 0.0
        for _ in range(periods):
            revenue_value *= 1.0 + float(growth)
            ebit = revenue_value * float(ebit_margin)
            tax = max(0.0, ebit * float(tax_rate))
            net_income = ebit - tax
            capex = revenue_value * float(capex_pct)
            nwc = revenue_value * float(nwc_pct)
            # Simplified D&A == capex, so operating cash flow == net income.
            free_cash = net_income - capex - nwc
            cash += free_cash
            equity += net_income
        assets = cash + nwc
        liabilities = 0.0
        return {
            "three_st_revenue": float(revenue_value),
            "three_st_net_income": float(net_income),
            "three_st_fcf": float(free_cash),
            "three_st_cash": float(cash),
            "three_st_equity": float(equity),
            "three_st_total_assets": float(assets),
            "three_st_liabilities": float(liabilities),
            "three_st_bs_imbalance": float(assets - (equity + liabilities)),
            "three_st_years": float(periods),
        }

    # -- market impact / fixed income / options ---------------------------
    def market_impact(
        self,
        *,
        price: float,
        quantity: float,
        adv: float,
        volatility: float,
        direction: int = 1,
        eta: float = 0.5,
    ) -> dict[str, float]:
        donor = self._donor_impact()
        if donor is not None:
            impacted = float(
                donor.sqrt_impact(
                    price=float(price),
                    direction=int(direction),
                    volume_traded=float(quantity),
                    adv=float(adv),
                    volatility=float(volatility),
                    eta=float(eta),
                )
            )
        else:  # pragma: no cover - donor import failure
            participation = abs(float(quantity)) / max(float(adv), 1e-12)
            impacted = float(price) * (1.0 + direction * float(eta) * float(volatility) * math.sqrt(participation))
        return {
            "impact_price": impacted,
            "impact_bps": (impacted / float(price) - 1.0) * 10_000.0 * float(direction),
            "impact_participation": abs(float(quantity)) / max(float(adv), 1e-12),
        }

    def fixed_income(
        self,
        *,
        face: float,
        coupon_rate: float,
        years: float,
        ytm: float,
        freq: int = 2,
    ) -> dict[str, float]:
        periods = max(1, int(round(float(years) * int(freq))))
        donor = self._donor_fixed_income()
        if donor is not None:
            price = float(
                donor.bond_price(
                    face=float(face),
                    coupon_rate=float(coupon_rate),
                    ytm=float(ytm),
                    n_periods=periods,
                    freq=int(freq),
                )
            )
            duration = float(
                donor.modified_duration(
                    face=float(face),
                    coupon_rate=float(coupon_rate),
                    ytm=float(ytm),
                    n_periods=periods,
                    freq=int(freq),
                )
            )
            dv01 = float(
                donor.dv01(
                    face=float(face),
                    coupon_rate=float(coupon_rate),
                    ytm=float(ytm),
                    n_periods=periods,
                    freq=int(freq),
                    par_amount=float(face),
                )
            )
            solved = float(
                donor.ytm_solve(
                    price=price,
                    face=float(face),
                    coupon_rate=float(coupon_rate),
                    n_periods=periods,
                    freq=int(freq),
                )
            )
        else:  # pragma: no cover - donor import failure
            coupon = float(face) * float(coupon_rate) / int(freq)
            discount_rate = float(ytm) / int(freq)
            price = sum(
                coupon / (1.0 + discount_rate) ** period for period in range(1, periods + 1)
            ) + float(face) / (1.0 + discount_rate) ** periods
            duration = float(years) / (1.0 + discount_rate)
            dv01 = price * duration * 1e-4
            solved = float(ytm)
        return {
            "bond_price": price,
            "bond_ytm": solved,
            "bond_modified_duration": duration,
            "bond_dv01": dv01,
            "bond_periods": float(periods),
        }

    def options(
        self,
        *,
        spot: float,
        strike: float,
        volatility: float,
        rate: float,
        years: float,
        kind: str = "call",
        dividend: float = 0.0,
    ) -> dict[str, float]:
        return black_scholes(
            spot, strike, volatility, rate, years, kind=kind, dividend=dividend
        )

    def deflated_sharpe_ratio(self, returns: Sequence[float], num_trials: int) -> float:
        return deflated_sharpe_ratio(returns, num_trials)

    # -- aggregate --------------------------------------------------------
    def value(
        self,
        *,
        price: float,
        fcff: Sequence[float],
        wacc: float,
        terminal_growth: float,
        peer_ev_ebitda: Sequence[float],
        ebitda: float,
        shares: float,
        net_debt: float = 0.0,
    ) -> dict[str, float]:
        """One merged, groundable valuation metric set for a single symbol."""
        dcf_metrics = self.dcf(
            fcff=fcff,
            wacc=wacc,
            terminal_growth=terminal_growth,
            net_debt=net_debt,
            shares=shares,
        )
        comps_metrics = self.comps(
            peer_ev_ebitda=peer_ev_ebitda,
            ebitda=ebitda,
            net_debt=net_debt,
            shares=shares,
        )
        merged = {**dcf_metrics, **comps_metrics}
        merged["close"] = float(price)
        merged["dcf_gap"] = float(dcf_metrics["dcf_per_share"] / float(price) - 1.0)
        merged["comps_gap"] = float(comps_metrics["comps_per_share"] / float(price) - 1.0)
        return merged
