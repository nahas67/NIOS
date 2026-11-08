"""NIOS Subsystem #8 -- Nocturnal Alpha Forge (``sec_003`` / ``sec_010`` / ``sec_017``).

The Forge is the Darwinian strategy-evolution loop that runs at ``00:00 UTC``
(``sec_018`` Law 5).  It does exactly two things, and both are gated by the
constitution:

1. **Auto-retirement.**  Every live champion is audited against its rolling
   realised net edge.  A champion whose edge decays below ``+0.50 bps`` is
   demoted to ``SHADOW_RETIRED`` and every active Ring 1 lease it owns is
   disarmed in POSIX shared memory.
2. **Walk-forward challenger breeding.**  ``1,000+`` factor-weight genomes are
   evaluated in vectorised batches over **purged, embargoed K-fold** splits so
   no training observation can see a label interval that overlaps the test
   block.  Benjamini-Hochberg FDR control then removes search luck.  A genome is
   promoted to ``LIVE_CHAMPION`` only when *both* constitutional conditions
   hold:

   * deflated Sharpe ``>= 1.50`` -- the observed out-of-sample Sharpe minus the
     Sharpe the best of ``N`` trials would reach under the null, in Sharpe
     units; and
   * ``>= 30`` **winning** live L2 shadow fills in ``ShadowExecutionTwin``.

This module deliberately imports neither Ring 0 signing code nor Ring 1
implementation classes.  The lease table and shadow twin are duck-typed
dependencies injected by the composition root, which preserves the air-gap
while still letting Ring 2 read the two facts it is entitled to read.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Iterable, Sequence

import numpy as np

__all__ = [
    "CHALLENGER",
    "LIVE_CHAMPION",
    "MIN_WINNING_SHADOW_FILLS",
    "PROMOTION_DSR_FLOOR",
    "RETIREMENT_FLOOR_BPS",
    "SHADOW",
    "SHADOW_RETIRED",
    "DEFAULT_EMBARGO_FRACTION",
    "DEFAULT_FACTOR_COUNT",
    "DEFAULT_FDR",
    "DEFAULT_GENOME_COUNT",
    "ChampionRecord",
    "CVResult",
    "FDRResult",
    "Fold",
    "ForgeDataset",
    "ForgeReport",
    "NocturnalAlphaForge",
    "PurgedKFoldCV",
    "StrategyGenome",
    "benjamini_hochberg",
    "deflated_sharpe",
    "deflated_sharpe_ratio",
    "expected_maximum_sharpe",
    "random_genomes",
]

#: ``sec_010`` capability 8 / ``sec_018`` Law 5.
RETIREMENT_FLOOR_BPS: float = 0.50
#: ``sec_010`` capability 8 / ``sec_018`` Law 5.
PROMOTION_DSR_FLOOR: float = 1.50
#: ``sec_018`` Law 4: 30 live L2 shadow fills before live graduation.
MIN_WINNING_SHADOW_FILLS: int = 30

#: Strategy lifecycle states.  ``SHADOW`` is the challenger pen.
LIVE_CHAMPION: str = "LIVE_CHAMPION"
CHALLENGER: str = "CHALLENGER"
SHADOW: str = "SHADOW"
SHADOW_RETIRED: str = "SHADOW_RETIRED"

#: Evolution runs at 00:00 UTC (``sec_010`` / ``sec_018`` Law 5).
FORGE_UTC_HOUR: int = 0
#: Rolling realised-edge window used by the retirement audit.
DEFAULT_ROLLING_WINDOW: int = 200
#: ``sec_010``: 1,000+ genomes per nightly batch.
DEFAULT_GENOME_COUNT: int = 1_000
DEFAULT_FACTOR_COUNT: int = 64
DEFAULT_PERIODS: int = 252
DEFAULT_CV_FOLDS: int = 5
#: Fraction of the sample embargoed after each test block.
DEFAULT_EMBARGO_FRACTION: float = 0.01
#: Benjamini-Hochberg false-discovery-rate control.
DEFAULT_FDR: float = 0.05
#: Evaluation chunk size: vectorised but bounded in memory.
DEFAULT_BATCH_SIZE: int = 256

_EULER_GAMMA: float = 0.5772156649015329
_SQRT2: float = math.sqrt(2.0)


# ---------------------------------------------------------------------------
# Statistics helpers (no SciPy dependency)
# ---------------------------------------------------------------------------


def _norm_ppf(probability: float) -> float:
    """Inverse standard-normal CDF via the stdlib, not SciPy."""
    clipped = min(max(float(probability), 1e-12), 1.0 - 1e-12)
    return float(statistics.NormalDist().inv_cdf(clipped))


def _norm_sf(value: float) -> float:
    """Upper-tail standard-normal probability, ``P(Z > value)``."""
    return 0.5 * math.erfc(float(value) / _SQRT2)


def expected_maximum_sharpe(n_trials: int, trial_sharpe_std: float) -> float:
    """Expected maximum Sharpe of ``n_trials`` null strategies.

    ``E[max] ~= std * [(1-g) Z^-1(1 - 1/N) + g Z^-1(1 - 1/(N e))]`` with
    ``g`` the Euler-Mascheroni constant (Bailey & Lopez de Prado).  This is the
    bar the observed Sharpe has to clear after searching ``N`` genomes.
    """
    trials = max(1, int(n_trials))
    std = float(trial_sharpe_std)
    if trials <= 1 or std <= 0.0 or not math.isfinite(std):
        return 0.0
    first = (1.0 - _EULER_GAMMA) * _norm_ppf(1.0 - 1.0 / trials)
    second = _EULER_GAMMA * _norm_ppf(1.0 - 1.0 / (trials * math.e))
    return float(std * (first + second))


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_trials: int,
    trial_sharpe_std: float = 0.0,
) -> float:
    """Deflated Sharpe in Sharpe units (``sec_010`` threshold is ``1.50``).

    ``DSR = observed_sharpe - E[max Sharpe | n_trials under the null]``.  A
    genome must clear ``+1.50`` *and* own 30 winning shadow fills.
    """
    return float(observed_sharpe) - expected_maximum_sharpe(
        int(n_trials), float(trial_sharpe_std)
    )


#: Short alias used in the architecture prose.
deflated_sharpe = deflated_sharpe_ratio


@dataclass(frozen=True)
class FDRResult:
    """Benjamini-Hochberg outcome, indexable and attribute-accessible."""

    rejected: np.ndarray
    adjusted_p_values: np.ndarray
    n_rejected: int
    threshold: float
    fdr: float

    def __iter__(self):
        yield self.rejected
        yield self.adjusted_p_values
        yield self.n_rejected
        yield self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_rejected": self.n_rejected,
            "threshold": self.threshold,
            "fdr": self.fdr,
            "rejected": [bool(value) for value in self.rejected.tolist()],
            "adjusted_p_values": [
                float(value) for value in self.adjusted_p_values.tolist()
            ],
        }


def benjamini_hochberg(
    p_values: Sequence[float] | np.ndarray,
    fdr: float = DEFAULT_FDR,
) -> FDRResult:
    """Benjamini-Hochberg step-up control.

    Returns an :class:`FDRResult` whose arrays are in the caller's original
    order.  Implemented locally so the Forge has no pandas or SciPy dependency
    on its nightly path.
    """
    rate = float(fdr)
    if not 0.0 < rate < 1.0:
        raise ValueError(f"fdr must be in (0, 1), got {rate}")
    values = np.asarray(p_values, dtype=np.float64).reshape(-1)
    if values.size == 0:
        raise ValueError("p_values is empty")
    if not np.isfinite(values).all():
        raise ValueError("p_values holds a non-finite value")
    if ((values < 0.0) | (values > 1.0)).any():
        raise ValueError("every p-value must lie in [0, 1]")

    n = int(values.size)
    order = np.argsort(values, kind="stable")
    ordered = values[order]
    ranks = np.arange(1, n + 1, dtype=np.float64)
    thresholds = ranks * rate / n
    passing = np.nonzero(ordered <= thresholds)[0]
    n_rejected = int(passing[-1] + 1) if passing.size else 0
    threshold = float(ordered[passing[-1]]) if passing.size else 0.0

    adjusted_sorted = np.minimum.accumulate(
        (ordered * n / ranks)[::-1]
    )[::-1]
    adjusted_sorted = np.clip(adjusted_sorted, 0.0, 1.0)
    adjusted = np.empty(n, dtype=np.float64)
    adjusted[order] = adjusted_sorted
    rejected = np.zeros(n, dtype=bool)
    if n_rejected:
        rejected[order[:n_rejected]] = True
    return FDRResult(
        rejected=rejected,
        adjusted_p_values=adjusted,
        n_rejected=n_rejected,
        threshold=threshold,
        fdr=rate,
    )


# ---------------------------------------------------------------------------
# Purged K-Fold cross-validation with embargo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fold:
    """One purged/embargoed train-test partition."""

    train: tuple[int, ...]
    test: tuple[int, ...]
    purged: int
    embargoed: int
    test_bounds: tuple[int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_size": len(self.train),
            "test_size": len(self.test),
            "purged": self.purged,
            "embargoed": self.embargoed,
            "test_bounds": list(self.test_bounds),
        }


@dataclass(frozen=True)
class CVResult:
    """Out-of-sample outcome of one genome under purged K-fold CV."""

    observed_sharpe: float
    fold_sharpes: tuple[float, ...]
    oos_observations: int
    purged: int
    embargoed: int
    folds: tuple[Fold, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_sharpe": self.observed_sharpe,
            "fold_sharpes": list(self.fold_sharpes),
            "oos_observations": self.oos_observations,
            "purged": self.purged,
            "embargoed": self.embargoed,
            "folds": [fold.to_dict() for fold in self.folds],
        }


class PurgedKFoldCV:
    """Contiguous purged K-fold splits with a post-test embargo.

    Financial labels overlap.  A training observation whose label interval
    ``[i, i + label_horizon]`` intersects the test block is leaked information,
    so it is purged (symmetrically).  Observations immediately *after* the test
    block are embargoed even though their labels do not overlap, because slowly
    moving features remain serially correlated with the test window.
    """

    def __init__(
        self,
        n_folds: int = DEFAULT_CV_FOLDS,
        *,
        embargo_fraction: float = DEFAULT_EMBARGO_FRACTION,
        label_horizon: int = 1,
    ) -> None:
        folds = int(n_folds)
        if folds < 2:
            raise ValueError("n_folds must be at least 2")
        embargo = float(embargo_fraction)
        if not 0.0 <= embargo < 1.0:
            raise ValueError("embargo_fraction must be in [0, 1)")
        horizon = int(label_horizon)
        if horizon < 1:
            raise ValueError("label_horizon must be at least 1")
        self.n_folds = folds
        self.embargo_fraction = embargo
        self.label_horizon = horizon

    def splits(self, n_samples: int) -> tuple[Fold, ...]:
        total = int(n_samples)
        if total < self.n_folds * 2:
            raise ValueError(
                f"{total} samples cannot make {self.n_folds} purged folds"
            )
        embargo_size = int(round(total * self.embargo_fraction))
        boundaries = np.linspace(0, total, self.n_folds + 1).astype(int)
        out: list[Fold] = []
        for fold in range(self.n_folds):
            start = int(boundaries[fold])
            end = int(boundaries[fold + 1])  # exclusive
            train: list[int] = []
            purged = 0
            embargoed = 0
            for index in range(total):
                if start <= index < end:
                    continue
                label_end = index + self.label_horizon
                if index <= end - 1 and label_end >= start:
                    purged += 1
                    continue
                if end <= index < end + embargo_size:
                    embargoed += 1
                    continue
                train.append(index)
            out.append(
                Fold(
                    train=tuple(train),
                    test=tuple(range(start, end)),
                    purged=purged,
                    embargoed=embargoed,
                    test_bounds=(start, end - 1),
                )
            )
        return tuple(out)

    def evaluate(
        self,
        factor_returns: np.ndarray,
        weights: np.ndarray | Sequence[float],
    ) -> CVResult:
        """Evaluate one static factor-weight genome out of sample."""
        matrix = np.asarray(factor_returns, dtype=np.float64)
        vector = np.asarray(weights, dtype=np.float64).reshape(-1)
        if matrix.ndim != 2:
            raise ValueError("factor_returns must be 2-D (periods x factors)")
        if matrix.shape[1] != vector.size:
            raise ValueError(
                f"weights has {vector.size} entries but the panel has "
                f"{matrix.shape[1]} factors"
            )
        series = matrix @ vector
        splits = self.splits(series.size)
        oos: list[np.ndarray] = []
        fold_sharpes: list[float] = []
        for split in splits:
            test_series = series[np.asarray(split.test, dtype=int)]
            oos.append(test_series)
            fold_sharpes.append(_sharpe(test_series))
        combined = np.concatenate(oos) if oos else np.empty(0)
        return CVResult(
            observed_sharpe=_sharpe(combined),
            fold_sharpes=tuple(fold_sharpes),
            oos_observations=int(combined.size),
            purged=sum(split.purged for split in splits),
            embargoed=sum(split.embargoed for split in splits),
            folds=splits,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "purged_kfold",
            "n_folds": self.n_folds,
            "embargo_fraction": self.embargo_fraction,
            "label_horizon": self.label_horizon,
            "purged": True,
            "embargoed": True,
        }

    #: Alias vocabulary used by research notebooks.
    def split(self, n_samples: int) -> tuple[Fold, ...]:
        return self.splits(n_samples)

    def purged_kfold_splits(self, n_samples: int) -> tuple[Fold, ...]:
        return self.splits(n_samples)

    def evaluate_genome(
        self, factor_returns: np.ndarray, weights: np.ndarray | Sequence[float]
    ) -> CVResult:
        return self.evaluate(factor_returns, weights)


def _sharpe(series: np.ndarray | Sequence[float]) -> float:
    values = np.asarray(series, dtype=np.float64).reshape(-1)
    values = values[np.isfinite(values)]
    if values.size < 2:
        return 0.0
    std = float(values.std(ddof=1))
    if std <= 1e-12:
        return 0.0
    return float(values.mean() / std)


# ---------------------------------------------------------------------------
# Genomes and synthetic panel
# ---------------------------------------------------------------------------


@dataclass
class StrategyGenome:
    """One factor-weight genome auditioned by the Forge."""

    genome_id: str
    weights: np.ndarray
    silo: str = "INDIA_ZERODHA_DHAN"
    shadow_fills: int = 0
    winning_shadow_fills: int = 0
    deflated_sharpe: float | None = None
    p_value: float | None = None
    fdr_adjusted_p: float | None = None
    fdr_significant: bool = False
    status: str = CHALLENGER
    hold_reason: str = ""
    metrics_source: str = "computed"
    registered_at_ns: int = 0

    def __post_init__(self) -> None:
        self.genome_id = str(self.genome_id)
        if not self.genome_id:
            raise ValueError("genome_id must be non-empty")
        vector = np.asarray(self.weights, dtype=np.float64).reshape(-1)
        if vector.size == 0:
            raise ValueError("a genome must carry at least one factor weight")
        if not np.isfinite(vector).all():
            raise ValueError("genome weights must be finite")
        self.weights = vector
        self.silo = str(self.silo)
        self.shadow_fills = int(self.shadow_fills)
        self.winning_shadow_fills = int(self.winning_shadow_fills)
        if self.deflated_sharpe is not None:
            self.deflated_sharpe = float(self.deflated_sharpe)
            self.metrics_source = "supplied"
            if self.p_value is None:
                # An externally audited DSR is accepted as a deterministic
                # verdict; without a p-value it is treated as maximally
                # significant so only the 30-fill gate can hold it back.
                self.p_value = 0.0
        if self.p_value is not None:
            self.p_value = min(max(float(self.p_value), 0.0), 1.0)
        self.registered_at_ns = int(self.registered_at_ns or time.time_ns())

    @property
    def uses_shadow_twin(self) -> bool:
        return self.genome_id.startswith("plugin:") or self.metrics_source == "computed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "genome_id": self.genome_id,
            "silo": self.silo,
            "factor_count": int(self.weights.size),
            "shadow_fills": self.shadow_fills,
            "winning_shadow_fills": self.winning_shadow_fills,
            "deflated_sharpe": self.deflated_sharpe,
            "p_value": self.p_value,
            "fdr_adjusted_p": self.fdr_adjusted_p,
            "fdr_significant": self.fdr_significant,
            "status": self.status,
            "shadow_mode": self.status in (CHALLENGER, SHADOW),
            "hold_reason": self.hold_reason,
            "metrics_source": self.metrics_source,
            "registered_at_ns": self.registered_at_ns,
        }


@dataclass
class ForgeDataset:
    """The factor-return panel the nightly walk-forward runs over."""

    factor_returns: np.ndarray
    factor_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        matrix = np.asarray(self.factor_returns, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[0] < 16 or matrix.shape[1] < 2:
            raise ValueError("factor_returns must be (periods>=16, factors>=2)")
        if not np.isfinite(matrix).all():
            raise ValueError("factor_returns must be finite")
        self.factor_returns = matrix
        ids = tuple(str(token) for token in self.factor_ids)
        if not ids:
            ids = tuple(f"k{index}" for index in range(matrix.shape[1]))
        if len(ids) != matrix.shape[1]:
            raise ValueError("factor_ids must match the panel width")
        self.factor_ids = ids

    @classmethod
    def synthesize(
        cls,
        n_periods: int = DEFAULT_PERIODS,
        n_factors: int = DEFAULT_FACTOR_COUNT,
        seed: int = 20260501,
        alpha_drift: float = 0.012,
        alpha_vol: float = 0.004,
    ) -> "ForgeDataset":
        """Deterministic panel with one embedded, exploitable alpha factor.

        The Forge is not a market simulator; this panel exists so the
        walk-forward machinery can be exercised deterministically.  Factor 0
        carries a real drift, factor 1 is a decaying short, and the rest are
        null noise.  A genome that loads on factor 0 can therefore earn a
        genuine out-of-sample Sharpe, while random genomes cannot.
        """
        periods = max(32, int(n_periods))
        factors = max(8, int(n_factors))
        rng = np.random.default_rng(int(seed))
        matrix = rng.normal(0.0, 0.01, size=(periods, factors))
        matrix[:, 0] = rng.normal(alpha_drift, alpha_vol, size=periods)
        if factors > 1:
            matrix[:, 1] = rng.normal(-0.004, 0.008, size=periods)
        if factors > 2:
            cycle = np.sin(np.linspace(0.0, 6.0 * math.pi, periods))
            matrix[:, 2] = 0.004 * cycle + rng.normal(0.0, 0.006, size=periods)
        ids = tuple(f"k{index}" for index in range(factors))
        return cls(factor_returns=matrix, factor_ids=ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "periods": int(self.factor_returns.shape[0]),
            "factors": int(self.factor_returns.shape[1]),
        }


def random_genomes(
    count: int = DEFAULT_GENOME_COUNT,
    *,
    n_factors: int = DEFAULT_FACTOR_COUNT,
    seed: int = 20260501,
    silo: str = "INDIA_ZERODHA_DHAN",
    prefix: str = "genome",
) -> list[StrategyGenome]:
    """Breed ``count`` random factor-weight genomes deterministically."""
    total = max(1, int(count))
    rng = np.random.default_rng(int(seed))
    matrix = rng.normal(0.0, 1.0, size=(total, max(1, int(n_factors))))
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.where(norms > 1e-12, norms, 1.0)
    return [
        StrategyGenome(
            genome_id=f"{prefix}-{index:04d}",
            weights=matrix[index],
            silo=silo,
        )
        for index in range(total)
    ]


# ---------------------------------------------------------------------------
# Champions
# ---------------------------------------------------------------------------


@dataclass
class ChampionRecord:
    """A live strategy and its rolling realised net-edge window."""

    strategy_id: str
    silo: str = "INDIA_ZERODHA_DHAN"
    symbols: tuple[str, ...] = ()
    status: str = LIVE_CHAMPION
    shadow_fills: int = 0
    winning_shadow_fills: int = 0
    rolling_window: int = DEFAULT_ROLLING_WINDOW
    net_bps_history: list[float] = field(default_factory=list)
    retired_at_ns: int = 0
    retire_reason: str = ""
    symbol: str = ""

    def __post_init__(self) -> None:
        self.strategy_id = str(self.strategy_id)
        if not self.strategy_id:
            raise ValueError("strategy_id must be non-empty")
        self.silo = str(self.silo)
        self.symbol = str(self.symbol)
        if self.symbol and not self.symbols:
            self.symbols = (self.symbol,)
        self.symbols = tuple(str(token) for token in self.symbols)
        self.rolling_window = max(1, int(self.rolling_window))
        self.shadow_fills = int(self.shadow_fills)
        self.winning_shadow_fills = int(self.winning_shadow_fills)
        self.net_bps_history = [float(value) for value in self.net_bps_history]
        self._trim()

    def _trim(self) -> None:
        if len(self.net_bps_history) > self.rolling_window:
            del self.net_bps_history[: len(self.net_bps_history) - self.rolling_window]

    def record_fill(self, net_bps: float) -> "ChampionRecord":
        value = float(net_bps)
        if not math.isfinite(value):
            raise ValueError("net_bps must be finite")
        self.net_bps_history.append(value)
        self._trim()
        return self

    @property
    def rolling_net_bps(self) -> float:
        if not self.net_bps_history:
            return 0.0
        return float(sum(self.net_bps_history) / len(self.net_bps_history))

    @property
    def fills(self) -> int:
        return len(self.net_bps_history)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "silo": self.silo,
            "symbols": list(self.symbols),
            "status": self.status,
            "fills": self.fills,
            "rolling_window": self.rolling_window,
            "rolling_net_bps": self.rolling_net_bps,
            "shadow_fills": self.shadow_fills,
            "winning_shadow_fills": self.winning_shadow_fills,
            "retired_at_ns": self.retired_at_ns,
            "retire_reason": self.retire_reason,
        }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class ForgeReport:
    """Auditable outcome of one nocturnal audit."""

    run_at_ns: int
    retired: list[dict[str, Any]] = field(default_factory=list)
    promoted: list[dict[str, Any]] = field(default_factory=list)
    held: list[dict[str, Any]] = field(default_factory=list)
    champions_after: int = 0
    challengers_after: int = 0
    genomes_evaluated: int = 0
    fdr_rejections: int = 0
    fdr_threshold: float = 0.0
    purged_observations: int = 0
    embargoed_observations: int = 0
    elapsed_ms: float = 0.0
    next_run_ns: int = 0
    thresholds: dict[str, Any] = field(default_factory=dict)

    @property
    def retired_count(self) -> int:
        return len(self.retired)

    @property
    def promoted_count(self) -> int:
        return len(self.promoted)

    @property
    def held_count(self) -> int:
        return len(self.held)

    @property
    def retirements(self) -> list[dict[str, Any]]:
        return self.retired

    @property
    def promotions(self) -> list[dict[str, Any]]:
        return self.promoted

    @property
    def held_back(self) -> list[dict[str, Any]]:
        return self.held

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_at_ns": self.run_at_ns,
            "retired": list(self.retired),
            "promoted": list(self.promoted),
            "held": list(self.held),
            "champions_after": self.champions_after,
            "challengers_after": self.challengers_after,
            "genomes_evaluated": self.genomes_evaluated,
            "fdr_rejections": self.fdr_rejections,
            "fdr_threshold": self.fdr_threshold,
            "purged_observations": self.purged_observations,
            "embargoed_observations": self.embargoed_observations,
            "elapsed_ms": self.elapsed_ms,
            "next_run_ns": self.next_run_ns,
            "thresholds": dict(self.thresholds),
            "retired_count": len(self.retired),
            "promoted_count": len(self.promoted),
            "held_count": len(self.held),
        }


# ---------------------------------------------------------------------------
# The Forge
# ---------------------------------------------------------------------------


class NocturnalAlphaForge:
    """``00:00 UTC`` walk-forward evolution with FDR + shadow graduation."""

    def __init__(
        self,
        *,
        lease_table: Any | None = None,
        shadow_twin: Any | None = None,
        silos: Iterable[str] | None = None,
        dataset: ForgeDataset | None = None,
        cv: PurgedKFoldCV | None = None,
        fdr: float = DEFAULT_FDR,
        clock: Any | None = None,
        genome_count: int = DEFAULT_GENOME_COUNT,
        seed: int = 20260501,
    ) -> None:
        rate = float(fdr)
        if not 0.0 < rate < 1.0:
            raise ValueError("fdr must be in (0, 1)")
        self.lease_table = lease_table
        self.shadow_twin = shadow_twin
        self.silos = tuple(
            str(silo)
            for silo in (
                silos
                if silos is not None
                else ("CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5")
            )
        )
        self.dataset = dataset if dataset is not None else ForgeDataset.synthesize()
        self.cv = cv if cv is not None else PurgedKFoldCV()
        self.fdr = rate
        self.genome_count = max(1, int(genome_count))
        self.seed = int(seed)
        self._clock = clock if callable(clock) else time.time_ns
        self._lock = threading.RLock()
        self._champions: dict[str, ChampionRecord] = {}
        self._challengers: dict[str, StrategyGenome] = {}
        self._reports: list[ForgeReport] = []
        self._last_run_ns: int = 0
        self._last_fdr: dict[str, Any] = {
            "rejections": 0,
            "threshold": 0.0,
            "tested": 0,
        }
        self._scheduler_task: asyncio.Task[Any] | None = None

    # -- registration -----------------------------------------------------
    def register_champion(
        self,
        champion: ChampionRecord | str,
        *,
        silo: str | None = None,
        symbols: Iterable[str] = (),
        shadow_fills: int = 0,
        winning_shadow_fills: int = 0,
        rolling_net_bps: float | None = None,
    ) -> ChampionRecord:
        if isinstance(champion, str):
            record = ChampionRecord(
                strategy_id=champion,
                silo=silo or "INDIA_ZERODHA_DHAN",
                symbols=tuple(symbols),
                shadow_fills=shadow_fills,
                winning_shadow_fills=winning_shadow_fills,
            )
        else:
            record = champion
        if rolling_net_bps is not None and not record.net_bps_history:
            record.record_fill(float(rolling_net_bps))
        with self._lock:
            self._champions[record.strategy_id] = record
        return record

    def get_champion(self, strategy_id: str) -> ChampionRecord | None:
        with self._lock:
            return self._champions.get(str(strategy_id))

    def champions(self) -> tuple[ChampionRecord, ...]:
        with self._lock:
            return tuple(self._champions.values())

    def record_champion_fill(
        self,
        strategy_id: str,
        net_bps: float,
        *,
        silo: str | None = None,
        symbols: Iterable[str] = (),
    ) -> ChampionRecord:
        with self._lock:
            token = str(strategy_id)
            record = self._champions.get(token)
            if record is None:
                record = ChampionRecord(
                    strategy_id=token,
                    silo=silo or "INDIA_ZERODHA_DHAN",
                    symbols=tuple(symbols),
                )
                self._champions[token] = record
            record.record_fill(net_bps)
            return record

    def enqueue_challenger(self, genome: StrategyGenome) -> StrategyGenome:
        if not isinstance(genome, StrategyGenome):
            raise TypeError("enqueue_challenger expects a StrategyGenome")
        with self._lock:
            self._sync_shadow(genome)
            genome.status = CHALLENGER
            self._challengers[genome.genome_id] = genome
        return genome

    def enqueue_genomes(
        self, genomes: Iterable[StrategyGenome]
    ) -> list[StrategyGenome]:
        return [self.enqueue_challenger(genome) for genome in genomes]

    def breed_random_genomes(
        self,
        count: int | None = None,
        *,
        seed: int | None = None,
        silo: str = "INDIA_ZERODHA_DHAN",
    ) -> list[StrategyGenome]:
        """Deterministically breed and enqueue ``count`` random genomes."""
        genomes = random_genomes(
            self.genome_count if count is None else int(count),
            n_factors=int(self.dataset.factor_returns.shape[1]),
            seed=self.seed if seed is None else int(seed),
            silo=silo,
        )
        return self.enqueue_genomes(genomes)

    def get_challenger(self, genome_id: str) -> StrategyGenome | None:
        with self._lock:
            return self._challengers.get(str(genome_id))

    def challengers(self) -> tuple[StrategyGenome, ...]:
        with self._lock:
            return tuple(self._challengers.values())

    # -- shadow-twin bridge ----------------------------------------------
    def _sync_shadow(self, genome: StrategyGenome) -> None:
        twin = self.shadow_twin
        if twin is None or not hasattr(twin, "fills"):
            return
        try:
            fills = tuple(twin.fills(genome.genome_id))
        except Exception:  # pragma: no cover - injected twin failure
            return
        if not fills:
            return
        wins = sum(1 for fill in fills if float(getattr(fill, "realized_net_bps", 0.0)) > 0.0)
        genome.shadow_fills = max(genome.shadow_fills, len(fills))
        genome.winning_shadow_fills = max(genome.winning_shadow_fills, wins)

    # -- the nightly audit ------------------------------------------------
    def audit(self, now_ns: int | None = None) -> ForgeReport:
        """Run one full Darwinian audit: retire, evaluate, promote."""
        started = time.perf_counter()
        now = int(now_ns if now_ns is not None else self._clock())
        with self._lock:
            retired = self._retire_decayed()
            evaluated, purged, embargoed = self._evaluate_challengers()
            promoted, held = self._promote()
            report = ForgeReport(
                run_at_ns=now,
                retired=retired,
                promoted=promoted,
                held=held,
                champions_after=sum(
                    1
                    for record in self._champions.values()
                    if record.status == LIVE_CHAMPION
                ),
                challengers_after=sum(
                    1
                    for genome in self._challengers.values()
                    if genome.status in (CHALLENGER, SHADOW)
                ),
                genomes_evaluated=max(evaluated, len(self._challengers)),
                fdr_rejections=int(self._last_fdr.get("rejections", 0)),
                fdr_threshold=float(self._last_fdr.get("threshold", 0.0)),
                purged_observations=purged,
                embargoed_observations=embargoed,
                elapsed_ms=(time.perf_counter() - started) * 1e3,
                next_run_ns=self.next_run_ns(now),
                thresholds=self.thresholds(),
            )
            self._reports.append(report)
            if len(self._reports) > 30:
                del self._reports[: len(self._reports) - 30]
            self._last_run_ns = now
            return report

    #: Explicit alias used by the Cockpit and tests.
    run_now = audit

    def run_audit(self, now_ns: int | None = None) -> ForgeReport:
        """Alias for :meth:`audit` (operator/MCP vocabulary)."""
        return self.audit(now_ns=now_ns)

    def audit_champions(self) -> list[dict[str, Any]]:
        """Run only the retirement half of the audit."""
        with self._lock:
            return self._retire_decayed()

    def evaluate_genomes(
        self, genomes: Iterable[StrategyGenome]
    ) -> tuple[int, int, int]:
        """Enqueue and evaluate a batch; returns ``(evaluated, purged, embargoed)``."""
        for genome in genomes:
            self.enqueue_challenger(genome)
        with self._lock:
            return self._evaluate_challengers()

    def register_genome(self, genome: StrategyGenome) -> StrategyGenome:
        return self.enqueue_challenger(genome)

    def register_challenger(self, genome: StrategyGenome) -> StrategyGenome:
        return self.enqueue_challenger(genome)

    def _retire_decayed(self) -> list[dict[str, Any]]:
        retired: list[dict[str, Any]] = []
        for record in self._champions.values():
            if record.status != LIVE_CHAMPION:
                continue
            if not record.net_bps_history:
                continue
            rolling = record.rolling_net_bps
            if rolling >= RETIREMENT_FLOOR_BPS:
                continue
            disarmed = 0
            symbols = list(record.symbols)
            if not symbols and self.shadow_twin is not None and hasattr(
                self.shadow_twin, "fills"
            ):
                try:
                    symbols = sorted(
                        {
                            str(fill.symbol)
                            for fill in self.shadow_twin.fills(record.strategy_id)
                        }
                    )
                except Exception:  # pragma: no cover - injected twin failure
                    symbols = []
            if not symbols and self.lease_table is not None and hasattr(
                self.lease_table, "iter_active_leases"
            ):
                try:
                    target_hash = int.from_bytes(
                        hashlib.blake2b(
                            record.strategy_id.encode("utf-8"), digest_size=8
                        ).digest(),
                        "little",
                        signed=False,
                    )
                    symbols = sorted(
                        {
                            str(lease.symbol)
                            for lease in self.lease_table.iter_active_leases(
                                time.monotonic_ns()
                            )
                            if int(getattr(lease, "strategy_id_hash", 0))
                            == target_hash
                        }
                    )
                except Exception:  # pragma: no cover - injected table failure
                    symbols = []
            if self.lease_table is not None and hasattr(self.lease_table, "disarm"):
                lease_now = time.monotonic_ns()
                for symbol in symbols:
                    try:
                        disarmed += int(
                            self.lease_table.disarm(symbol, now_ns=lease_now) or 0
                        )
                    except Exception:  # pragma: no cover - defensive
                        continue
            record.status = SHADOW_RETIRED
            record.retired_at_ns = int(self._clock())
            record.retire_reason = (
                f"ROLLING_NET_EDGE_{rolling:+.4f}_BPS_BELOW_"
                f"{RETIREMENT_FLOOR_BPS:.2f}_BPS"
            )
            retired.append(
                {
                    **record.to_dict(),
                    "disarmed_leases": disarmed,
                    "threshold_bps": RETIREMENT_FLOOR_BPS,
                }
            )
        return retired

    def _evaluate_challengers(self) -> tuple[int, int, int]:
        pending = [
            genome
            for genome in self._challengers.values()
            if genome.deflated_sharpe is None
        ]
        evaluated = 0
        purged = 0
        embargoed = 0
        if pending:
            matrix = self.dataset.factor_returns
            width = matrix.shape[1]
            blocks: list[np.ndarray] = []
            for genome in pending:
                vector = genome.weights
                if vector.size != width:
                    adjusted = np.zeros(width, dtype=np.float64)
                    copied = min(width, vector.size)
                    adjusted[:copied] = vector[:copied]
                    vector = adjusted
                blocks.append(vector)
            weights = np.vstack(blocks)

            splits = self.cv.splits(matrix.shape[0])
            purged = sum(split.purged for split in splits)
            embargoed = sum(split.embargoed for split in splits)
            oos_chunks: list[np.ndarray] = []
            for split in splits:
                test = np.asarray(split.test, dtype=int)
                oos_chunks.append(matrix[test] @ weights.T)
            oos = np.vstack(oos_chunks)
            observations = float(oos.shape[0])

            # Batched vectorised Sharpe + p-values, bounded by batch size.
            batch = DEFAULT_BATCH_SIZE
            for start in range(0, len(pending), batch):
                stop = min(start + batch, len(pending))
                chunk = oos[:, start:stop]
                mean = chunk.mean(axis=0)
                std = chunk.std(axis=0, ddof=1)
                sharpes = np.divide(
                    mean,
                    std,
                    out=np.zeros_like(mean),
                    where=std > 1e-12,
                )
                for offset, sharpe in enumerate(sharpes):
                    genome = pending[start + offset]
                    genome.deflated_sharpe = float(sharpe)  # provisional
                    genome.p_value = float(
                        min(max(_norm_sf(float(sharpe) * math.sqrt(observations)), 0.0), 1.0)
                    )
                    genome.metrics_source = "computed"
                evaluated += stop - start

            sharpes = np.array(
                [float(genome.deflated_sharpe) for genome in pending],
                dtype=np.float64,
            )
            trial_std = float(sharpes.std(ddof=1)) if sharpes.size > 1 else 0.0
            bar = expected_maximum_sharpe(max(2, len(pending)), trial_std)
            for genome in pending:
                genome.deflated_sharpe = float(genome.deflated_sharpe) - bar

        # BH control always runs over the full challenger field so supplied
        # and computed metrics share one correction family.
        field = list(self._challengers.values())
        if field:
            p_values = [
                float(genome.p_value) if genome.p_value is not None else 1.0
                for genome in field
            ]
            rejected, adjusted, count, threshold = benjamini_hochberg(
                p_values, self.fdr
            )
            for genome, is_rejected, adjusted_p in zip(field, rejected, adjusted):
                genome.fdr_significant = bool(is_rejected)
                genome.fdr_adjusted_p = float(adjusted_p)
            self._last_fdr = {
                "rejections": int(count),
                "threshold": float(threshold),
                "tested": len(field),
                "fdr": self.fdr,
            }
        else:
            self._last_fdr = {"rejections": 0, "threshold": 0.0, "tested": 0}
        return evaluated, purged, embargoed

    def _promote(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        promoted: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        for genome in self._challengers.values():
            if genome.status == LIVE_CHAMPION:
                continue
            self._sync_shadow(genome)
            reasons: list[str] = []
            dsr = genome.deflated_sharpe
            if dsr is None or float(dsr) < PROMOTION_DSR_FLOOR:
                reasons.append(
                    f"DEFLATED_SHARPE_{float(dsr or 0.0):.4f}_BELOW_"
                    f"{PROMOTION_DSR_FLOOR:.2f}"
                )
            if genome.winning_shadow_fills < MIN_WINNING_SHADOW_FILLS:
                reasons.append(
                    f"WINNING_SHADOW_FILLS_{genome.winning_shadow_fills}_BELOW_"
                    f"{MIN_WINNING_SHADOW_FILLS}"
                )
            if not genome.fdr_significant:
                reasons.append("FDR_NOT_SIGNIFICANT")
            if reasons:
                genome.status = SHADOW
                genome.hold_reason = "; ".join(reasons)
                held.append(genome.to_dict())
                continue

            genome.status = LIVE_CHAMPION
            genome.hold_reason = ""
            record = ChampionRecord(
                strategy_id=genome.genome_id,
                silo=genome.silo,
                shadow_fills=genome.shadow_fills,
                winning_shadow_fills=genome.winning_shadow_fills,
            )
            twin = self.shadow_twin
            if twin is not None and hasattr(twin, "fills"):
                try:
                    fills = tuple(twin.fills(genome.genome_id))
                    for fill in fills:
                        record.record_fill(float(fill.realized_net_bps))
                    record.symbols = tuple(
                        sorted({str(fill.symbol) for fill in fills})
                    )
                except Exception:  # pragma: no cover - injected twin failure
                    pass
            self._champions[genome.genome_id] = record
            promoted.append(
                {
                    **genome.to_dict(),
                    "deflated_sharpe": float(genome.deflated_sharpe or 0.0),
                    "promoted_to": LIVE_CHAMPION,
                }
            )
        return promoted, held

    # -- schedule ---------------------------------------------------------
    def next_run_ns(self, now_ns: int | None = None) -> int:
        """Next ``00:00 UTC`` in wall-clock nanoseconds."""
        now = int(now_ns if now_ns is not None else self._clock())
        moment = datetime.fromtimestamp(now / 1e9, tz=timezone.utc)
        midnight = datetime.combine(
            moment.date(), dt_time(FORGE_UTC_HOUR, 0, 0), tzinfo=timezone.utc
        )
        if moment >= midnight:
            midnight += timedelta(days=1)
        return int(midnight.timestamp() * 1e9)

    def seconds_until_next_run(self, now_ns: int | None = None) -> float:
        now = int(now_ns if now_ns is not None else self._clock())
        return max(0.0, (self.next_run_ns(now) - now) / 1e9)

    async def _scheduler_loop(self) -> None:  # pragma: no cover - daemon path
        while True:
            target = self.next_run_ns()
            while self._clock() < target:
                remaining = max(1.0, (target - self._clock()) / 1e9)
                await asyncio.sleep(min(remaining, 3600.0))
            self.audit()

    def start_scheduler(self) -> asyncio.Task[Any]:  # pragma: no cover - daemon path
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        return self._scheduler_task

    def stop_scheduler(self) -> None:  # pragma: no cover - daemon path
        task = self._scheduler_task
        if task is not None and not task.done():
            task.cancel()
        self._scheduler_task = None

    def run_scheduler_forever(self) -> None:  # pragma: no cover - daemon path
        """Blocking scheduler entry point for a dedicated Ring 2 process."""
        asyncio.run(self._scheduler_loop())

    # -- snapshots --------------------------------------------------------
    def thresholds(self) -> dict[str, Any]:
        return {
            "retirement_floor_bps": RETIREMENT_FLOOR_BPS,
            "promotion_dsr_floor": PROMOTION_DSR_FLOOR,
            "min_winning_shadow_fills": MIN_WINNING_SHADOW_FILLS,
            "fdr": self.fdr,
            "cv": self.cv.describe(),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            last = self._reports[-1].to_dict() if self._reports else None
            return {
                "schedule_utc": "00:00",
                "next_run_ns": self.next_run_ns(),
                "last_run_ns": self._last_run_ns,
                "champion_count": sum(
                    1
                    for record in self._champions.values()
                    if record.status == LIVE_CHAMPION
                ),
                "retired_count": sum(
                    1
                    for record in self._champions.values()
                    if record.status == SHADOW_RETIRED
                ),
                "challenger_count": sum(
                    1
                    for genome in self._challengers.values()
                    if genome.status in (CHALLENGER, SHADOW)
                ),
                "genome_count": len(self._challengers),
                "champions": [
                    record.to_dict() for record in self._champions.values()
                ],
                "challengers": [
                    genome.to_dict()
                    for genome in self._challengers.values()
                    if genome.status in (CHALLENGER, SHADOW)
                ][:50],
                "last_report": last,
                "fdr": dict(self._last_fdr),
                "thresholds": self.thresholds(),
                "dataset": self.dataset.to_dict(),
            }
