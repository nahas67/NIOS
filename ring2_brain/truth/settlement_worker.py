"""NIOS Core v3.0 -- Delayed Settlement & Darwinian Episodic Memory (STEP 4).

Implements the ``sec_005`` / ``sec_009`` delayed-memory contract:

* every agent prediction is logged with an explicit evaluation horizon and the
  SHA-256 digest of the :class:`~core.schemas.GroundedFactPacket` it was
  derived from;
* once ``now_ns >= created_at_ns + horizon_ns`` the worker grades the
  prediction against realized net basis points, computes the absolute
  prediction-error residual, and appends it to the strategy's rolling
  200-trade split-conformal window through
  :meth:`core.feature_store.FeatureStore.append_conformal_residual`, so
  ``Q_0.98(Residuals)`` stays calibrated;
* the agent's **Darwinian trust multiplier** is recomputed from rolling
  directional accuracy and clamped strictly to ``[0.8, 1.5]``.

Donor bridge
------------
``ring2_brain/truth/tradingagents/settlement.py`` is harvested but imports
``tradingagents.dataflows.*`` (a donor package that does not exist in this
repository).  :func:`donor_settlement_status` reports that fact honestly instead
of pretending the module can be imported.  The native worker below re-implements
the same delayed-grading contract directly on ``core.schemas`` and
``core.feature_store`` and accepts an optional ``donor_settler`` callable for
installations where the donor package is vendored.

Engineering rules honoured
--------------------------
* No lookahead: a prediction is never settled before its own horizon elapses.
* No ungrounded number: the prediction carries the grounding digest it was
  committed against (``sec_018`` Law 3).
* Residuals are absolute nonconformity scores; they are subtracted from gross
  edge by the Ring 0 ``Net_LB`` gate (``sec_003`` / ``sec_009``).
"""

from __future__ import annotations

import importlib
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.feature_store import CONFORMAL_WINDOW, FeatureStore
from core.schemas import canonical_pairs_digest

__all__ = [
    "TRUST_MIN",
    "TRUST_MAX",
    "TRUST_WINDOW",
    "Prediction",
    "SettlementOutcome",
    "TrustSnapshot",
    "DelayedSettlementWorker",
    "donor_settlement_status",
]

#: ``sec_009`` Darwinian trust multiplier bounds.
TRUST_MIN = 0.8
TRUST_MAX = 1.5
#: Rolling accuracy window used for the multiplier (aligned with the conformal
#: 200-trade window).
TRUST_WINDOW = CONFORMAL_WINDOW


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _validate_digest(digest: str) -> str:
    token = str(digest or "").strip().lower()
    if token and (
        len(token) != 64 or any(char not in "0123456789abcdef" for char in token)
    ):
        raise ValueError("grounding_hash must be a 64-char SHA-256 hex digest")
    return token


def donor_settlement_status() -> dict[str, Any]:
    """Report whether the harvested TradingAgents settlement module imports."""
    path = Path(__file__).resolve().parent / "tradingagents" / "settlement.py"
    entry: dict[str, Any] = {
        "path": str(path),
        "harvested": path.exists(),
        "importable": False,
        "reason": "",
    }
    if path.exists():
        try:
            module = importlib.import_module(
                "ring2_brain.truth.tradingagents.settlement"
            )
            entry["importable"] = True
            entry["symbols"] = [
                name
                for name in ("Settlement", "resolve_benchmark", "fetch_returns")
                if hasattr(module, name)
            ]
        except Exception as exc:  # pragma: no cover - donor vendoring
            entry["reason"] = f"{type(exc).__name__}: {exc}"
    else:
        entry["reason"] = "not harvested"
    return entry


# ---------------------------------------------------------------------------
# Prediction contract
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Prediction:
    """One agent forecast with an explicit evaluation horizon."""

    prediction_id: str
    strategy_id: str
    symbol: str
    predicted_net_bps: float
    horizon_ns: int
    created_at_ns: int
    grounding_hash: str = ""
    silo: str = "US"
    is_shadow: bool = True

    def __post_init__(self) -> None:
        if not str(self.prediction_id).strip():
            raise ValueError("prediction_id must be non-empty")
        if not str(self.strategy_id).strip():
            raise ValueError("strategy_id must be non-empty")
        predicted = float(self.predicted_net_bps)
        if not math.isfinite(predicted):
            raise ValueError("predicted_net_bps must be finite")
        horizon = int(self.horizon_ns)
        if horizon < 0:
            raise ValueError("horizon_ns must be non-negative")
        object.__setattr__(self, "predicted_net_bps", predicted)
        object.__setattr__(self, "horizon_ns", horizon)
        object.__setattr__(self, "created_at_ns", int(self.created_at_ns))
        object.__setattr__(self, "symbol", str(self.symbol).strip().upper())
        object.__setattr__(self, "grounding_hash", _validate_digest(self.grounding_hash))
        object.__setattr__(self, "is_shadow", bool(self.is_shadow))

    @property
    def due_at_ns(self) -> int:
        return self.created_at_ns + self.horizon_ns

    def is_due(self, now_ns: int) -> bool:
        return int(now_ns) >= self.due_at_ns

    @property
    def is_grounded(self) -> bool:
        return len(self.grounding_hash) == 64

    def residual_bps(self, realized_net_bps: float) -> float:
        """Absolute nonconformity score fed to the conformal window."""
        realized = float(realized_net_bps)
        if not math.isfinite(realized):
            raise ValueError("realized_net_bps must be finite")
        return abs(self.predicted_net_bps - realized)

    def hit(self, realized_net_bps: float) -> bool:
        """Directional accuracy (long/short call was on the right side)."""
        realized = float(realized_net_bps)
        return self.predicted_net_bps * realized > 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "predicted_net_bps": self.predicted_net_bps,
            "horizon_ns": self.horizon_ns,
            "created_at_ns": self.created_at_ns,
            "due_at_ns": self.due_at_ns,
            "grounding_hash": self.grounding_hash,
            "silo": self.silo,
            "is_shadow": self.is_shadow,
        }


@dataclass(slots=True, frozen=True)
class SettlementOutcome:
    """Result of grading one prediction."""

    prediction_id: str
    strategy_id: str
    symbol: str
    predicted_net_bps: float
    realized_net_bps: float
    residual_bps: float
    settled_at_ns: int
    q98_bps: float
    trust_multiplier: float
    directional_hit: bool
    window_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "predicted_net_bps": self.predicted_net_bps,
            "realized_net_bps": self.realized_net_bps,
            "residual_bps": self.residual_bps,
            "settled_at_ns": self.settled_at_ns,
            "q98_bps": self.q98_bps,
            "trust_multiplier": self.trust_multiplier,
            "directional_hit": self.directional_hit,
            "window_count": self.window_count,
        }


@dataclass(slots=True, frozen=True)
class TrustSnapshot:
    """A persisted Darwinian trust observation."""

    strategy_id: str
    as_of_ns: int
    multiplier: float
    accuracy: float
    settled_count: int
    mean_abs_error_bps: float

    def __post_init__(self) -> None:
        multiplier = _clamp(float(self.multiplier), TRUST_MIN, TRUST_MAX)
        object.__setattr__(self, "multiplier", multiplier)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "as_of_ns": self.as_of_ns,
            "trust_multiplier": self.multiplier,
            "accuracy": self.accuracy,
            "settled_count": self.settled_count,
            "mean_abs_error_bps": self.mean_abs_error_bps,
        }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class DelayedSettlementWorker:
    """Grade matured predictions and evolve agent trust.

    Parameters
    ----------
    store:
        Tier-2 :class:`~core.feature_store.FeatureStore`.  Every graded residual
        is appended to the strategy's 200-trade conformal window and every
        trust update is persisted as a ``darwinian_trust`` valuation artefact.
    donor_settler:
        Optional callable ``(prediction, now_ns) -> float`` used to resolve
        realized basis points from the harvested donor settlement module when
        it is vendored.  It is consulted only when the caller does not supply
        an explicit realized value.
    """

    def __init__(
        self,
        store: FeatureStore,
        *,
        donor_settler: Callable[[Prediction, int], float] | None = None,
        persist_trust: bool = True,
    ) -> None:
        if store is None:
            raise ValueError("DelayedSettlementWorker requires a FeatureStore")
        self.store = store
        self.donor_settler = donor_settler
        self.persist_trust = bool(persist_trust)
        self.donor = donor_settlement_status()
        self._predictions: dict[str, Prediction] = {}
        self._settled: list[SettlementOutcome] = []
        self._hits: dict[str, list[bool]] = {}
        self._errors: dict[str, list[float]] = {}
        self._trust: dict[str, float] = {}
        self._settlement_seq = 0
        self._last_as_of_ns = 0

    # -- recording --------------------------------------------------------
    def record_prediction(
        self,
        prediction_id: str | Prediction | None = None,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
        predicted_net_bps: float | None = None,
        horizon_ns: int | None = None,
        created_at_ns: int | None = None,
        grounding_hash: str = "",
        silo: str = "US",
        is_shadow: bool = True,
    ) -> Prediction:
        """Log one prediction.  ``prediction_id`` defaults to a content hash.

        A fully formed :class:`Prediction` may be supplied positionally instead
        of the keyword fields.
        """
        if isinstance(prediction_id, Prediction):
            prediction = prediction_id
            if prediction.prediction_id in self._predictions:
                raise ValueError(
                    f"duplicate prediction_id {prediction.prediction_id!r}"
                )
            self._predictions[prediction.prediction_id] = prediction
            return prediction
        if strategy_id is None or symbol is None or predicted_net_bps is None:
            raise TypeError(
                "record_prediction requires strategy_id, symbol and predicted_net_bps"
            )
        created = int(created_at_ns) if created_at_ns is not None else time.time_ns()
        horizon = int(horizon_ns or 0)
        identifier = prediction_id or canonical_pairs_digest(
            [
                ("strategy_id", strategy_id),
                ("symbol", symbol),
                ("predicted_net_bps", float(predicted_net_bps)),
                ("horizon_ns", horizon),
                ("created_at_ns", created),
            ]
        )[:32]
        prediction = Prediction(
            prediction_id=str(identifier),
            strategy_id=str(strategy_id),
            symbol=str(symbol),
            predicted_net_bps=float(predicted_net_bps),
            horizon_ns=horizon,
            created_at_ns=created,
            grounding_hash=grounding_hash,
            silo=silo,
            is_shadow=is_shadow,
        )
        if prediction.prediction_id in self._predictions:
            raise ValueError(f"duplicate prediction_id {prediction.prediction_id!r}")
        self._predictions[prediction.prediction_id] = prediction
        return prediction

    # Alias matching the architecture prose ("record agent predictions").
    record = record_prediction

    def get(self, prediction_id: str) -> Prediction:
        try:
            return self._predictions[str(prediction_id)]
        except KeyError as exc:
            raise KeyError(f"unknown prediction_id {prediction_id!r}") from exc

    @property
    def pending_predictions(self) -> tuple[Prediction, ...]:
        settled = {outcome.prediction_id for outcome in self._settled}
        return tuple(
            prediction
            for prediction in self._predictions.values()
            if prediction.prediction_id not in settled
        )

    @property
    def settled_outcomes(self) -> tuple[SettlementOutcome, ...]:
        return tuple(self._settled)

    # -- grading ----------------------------------------------------------
    def settle_prediction(
        self,
        prediction_id: str,
        realized_net_bps: float,
        *,
        now_ns: int | None = None,
        is_shadow: bool | None = None,
    ) -> SettlementOutcome:
        """Grade one matured prediction and update conformal + trust state."""
        prediction = self.get(prediction_id)
        stamp = int(now_ns) if now_ns is not None else time.time_ns()
        if not prediction.is_due(stamp):
            raise ValueError(
                f"prediction {prediction_id!r} is not due until "
                f"{prediction.due_at_ns} (now={stamp})"
            )
        realized = float(realized_net_bps)
        if not math.isfinite(realized):
            raise ValueError("realized_net_bps must be finite")

        residual = prediction.residual_bps(realized)
        hit = prediction.hit(realized)
        # Every settled row needs a distinct bitemporal key, otherwise two
        # settlements sharing a nanosecond would replace each other.
        self._settlement_seq += 1
        as_of = max(stamp, prediction.due_at_ns) + self._settlement_seq
        self._last_as_of_ns = as_of

        self.store.append_conformal_residual(
            prediction.strategy_id,
            residual,
            as_of,
            knowledge_ts_ns=stamp,
            trade_id=prediction.prediction_id,
            realized_net_bps=realized,
            is_shadow=(
                prediction.is_shadow if is_shadow is None else bool(is_shadow)
            ),
        )

        hits = self._hits.setdefault(prediction.strategy_id, [])
        errors = self._errors.setdefault(prediction.strategy_id, [])
        hits.append(bool(hit))
        errors.append(residual)
        if len(hits) > TRUST_WINDOW:
            del hits[: len(hits) - TRUST_WINDOW]
            del errors[: len(errors) - TRUST_WINDOW]

        accuracy = sum(1 for value in hits if value) / float(len(hits))
        multiplier = _clamp(TRUST_MIN + (TRUST_MAX - TRUST_MIN) * accuracy,
                            TRUST_MIN, TRUST_MAX)
        self._trust[prediction.strategy_id] = multiplier

        window = self.store.get_q98_residual(prediction.strategy_id, as_of)
        if self.persist_trust:
            snapshot = TrustSnapshot(
                strategy_id=prediction.strategy_id,
                as_of_ns=as_of,
                multiplier=multiplier,
                accuracy=accuracy,
                settled_count=len(hits),
                mean_abs_error_bps=sum(errors) / float(len(errors)),
            )
            self.store.append_valuation(
                prediction.silo,
                prediction.strategy_id,
                as_of,
                "darwinian_trust",
                snapshot.to_dict(),
                knowledge_ts_ns=as_of,
                valid_time_ns=as_of,
            )

        outcome = SettlementOutcome(
            prediction_id=prediction.prediction_id,
            strategy_id=prediction.strategy_id,
            symbol=prediction.symbol,
            predicted_net_bps=prediction.predicted_net_bps,
            realized_net_bps=realized,
            residual_bps=residual,
            settled_at_ns=stamp,
            q98_bps=float(window.q98_bps),
            trust_multiplier=multiplier,
            directional_hit=bool(hit),
            window_count=window.count,
        )
        self._settled.append(outcome)
        return outcome

    def settle_due(
        self,
        now_ns: int | None = None,
        realized_net_bps_by_prediction: Mapping[str, float] | None = None,
        *,
        is_shadow: bool | None = None,
    ) -> list[SettlementOutcome]:
        """Settle every matured prediction with a known realized outcome.

        Predictions absent from the mapping are left pending -- a holding
        period that has elapsed on the clock but has no realized price is not
        a settled trade, it is a missing observation.
        """
        stamp = int(now_ns) if now_ns is not None else time.time_ns()
        realized_map = dict(realized_net_bps_by_prediction or {})
        settled_ids = {outcome.prediction_id for outcome in self._settled}
        outcomes: list[SettlementOutcome] = []
        for prediction in sorted(
            self._predictions.values(), key=lambda item: item.due_at_ns
        ):
            if prediction.prediction_id in settled_ids:
                continue
            if not prediction.is_due(stamp):
                continue
            if prediction.prediction_id in realized_map:
                outcome = self.settle_prediction(
                    prediction.prediction_id,
                    realized_map[prediction.prediction_id],
                    now_ns=stamp,
                    is_shadow=is_shadow,
                )
                outcomes.append(outcome)
                settled_ids.add(outcome.prediction_id)
            elif self.donor_settler is not None:
                realized = float(self.donor_settler(prediction, stamp))
                outcome = self.settle_prediction(
                    prediction.prediction_id,
                    realized,
                    now_ns=stamp,
                    is_shadow=is_shadow,
                )
                outcomes.append(outcome)
                settled_ids.add(outcome.prediction_id)
        return outcomes

    # -- trust ------------------------------------------------------------
    def trust_multiplier(self, strategy_id: str) -> float:
        """Current Darwinian multiplier (neutral ``1.0`` before any grading)."""
        return _clamp(
            float(self._trust.get(str(strategy_id), 1.0)), TRUST_MIN, TRUST_MAX
        )

    #: Readable aliases used by the cockpit and by downstream agents.
    get_trust_multiplier = trust_multiplier
    darwinian_trust_multiplier = trust_multiplier

    def trust_snapshots(self, strategy_id: str) -> list[TrustSnapshot]:
        """Read persisted trust observations for one strategy."""
        rows = self.store.query_valuation(
            "US", str(strategy_id), time.time_ns(), artifact_kind="darwinian_trust"
        )
        # ``query_valuation`` is newest-first; expose a chronological history.
        return [
            TrustSnapshot(
                strategy_id=str(strategy_id),
                as_of_ns=int(row["as_of_ns"]),
                multiplier=float(row["payload"]["trust_multiplier"]),
                accuracy=float(row["payload"]["accuracy"]),
                settled_count=int(row["payload"]["settled_count"]),
                mean_abs_error_bps=float(row["payload"]["mean_abs_error_bps"]),
            )
            for row in reversed(rows)
        ]

    def settlements_for(self, strategy_id: str) -> tuple[SettlementOutcome, ...]:
        return tuple(
            outcome
            for outcome in self._settled
            if outcome.strategy_id == str(strategy_id)
        )

    @property
    def last_as_of_ns(self) -> int:
        return self._last_as_of_ns
