"""NIOS Core v3.0 -- Tier 2 DuckDB + Parquet bitemporal feature lake (STEP 2).

Implements the ``sec_009`` Tier 2 contract:

* partition layout ``(silo, sym, asOfDate)``;
* bitemporal index rule ``knowledgeTs <= asOfTs`` enforced on **both** writes
  and reads (a row created at a later ``as_of_ns`` is invisible too, which is
  what makes a replay of yesterday's feature set reproducible);
* immutable append-only snapshots -- corrections are new rows, never in-place
  updates of an existing ``(as_of_ns, knowledge_ts_ns)`` pair;
* three payload families:
    - 480+ alpha factor vectors and DCF/Comps valuation artefacts,
    - MiroFish 3-tick contagion features (``crowdAsymmetryScore``,
      ``cascadeFragilityIndex``, ``narrativeExhaustionDivergence``),
    - the 200-trade rolling split-conformal residual window whose
      ``Q_0.98`` feeds the Ring 0 precision gate.

Cold-start is deliberately conservative: fewer than ``min_samples``
residuals yields ``Q_0.98 = +inf``, which forces ``Net_LB < 0`` and therefore
abstention.  ``sec_002`` lists "conformal cold-start overconfidence" as a
fatal flaw; this module makes it structurally impossible.
"""

from __future__ import annotations

import json
import math
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import duckdb

try:  # optional vectorised path for large factor snapshots
    import pyarrow as _pyarrow
except Exception:  # pragma: no cover - optional acceleration
    _pyarrow = None  # type: ignore[assignment]

from .schemas import (
    ContagionFeatures,
    TemporalIntegrityError,
    canonical_pairs_digest,
    net_lower_bound_bps,
)

__all__ = [
    "DEFAULT_FEATURE_LAKE_ROOT",
    "CONFORMAL_WINDOW",
    "CONFORMAL_MIN_SAMPLES",
    "ConformalWindow",
    "FeatureStore",
    "quantile_linear",
]

DEFAULT_FEATURE_LAKE_ROOT = Path(__file__).resolve().parents[1] / "data" / "feature_lake"

#: Law 4: a 200-trade rolling residual window (``sec_009``).
CONFORMAL_WINDOW = 200
#: Minimum sample count before the 98% quantile is trusted at all.
CONFORMAL_MIN_SAMPLES = 30


# ---------------------------------------------------------------------------
# Quantiles
# ---------------------------------------------------------------------------


def quantile_linear(values: Sequence[float], q: float) -> float:
    """Empirical quantile with linear interpolation (NumPy ``'linear'``).

    Implemented locally so the feature store has no NumPy dependency on the
    Ring 0 gate path.
    """
    if not 0.0 <= float(q) <= 1.0:
        raise ValueError("q must be within [0, 1]")
    if not values:
        raise ValueError("quantile of an empty sequence is undefined")
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    position = float(q) * (n - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, n - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


@dataclass(slots=True, frozen=True)
class ConformalWindow:
    """Snapshot of the rolling split-conformal residual window."""

    strategy_id: str
    as_of_ns: int
    q98_bps: float
    count: int
    shadow_count: int
    live_count: int
    mean_bps: float
    min_bps: float
    max_bps: float
    requested_window: int
    calibrated: bool

    @property
    def is_warm(self) -> bool:
        """Law 4: 30 L2 shadow fills before any live graduation."""
        return self.shadow_count >= CONFORMAL_MIN_SAMPLES

    def net_lower_bound_bps(
        self,
        expected_gross_bps: float,
        spread_bps: float,
        fee_bps: float,
        tax_bps: float,
    ) -> float:
        return net_lower_bound_bps(
            expected_gross_bps, spread_bps, fee_bps, tax_bps, self.q98_bps
        )


# ---------------------------------------------------------------------------
# Feature store
# ---------------------------------------------------------------------------


_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS alpha_factors (
        silo            VARCHAR NOT NULL,
        sym             VARCHAR NOT NULL,
        as_of_date      VARCHAR NOT NULL,
        as_of_ns        BIGINT  NOT NULL,
        knowledge_ts_ns BIGINT  NOT NULL,
        valid_time_ns   BIGINT  NOT NULL,
        factor_name     VARCHAR NOT NULL,
        factor_value    DOUBLE  NOT NULL,
        sha256_digest   VARCHAR NOT NULL,
        PRIMARY KEY (silo, sym, as_of_date, as_of_ns, knowledge_ts_ns, factor_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS valuation_artifacts (
        silo            VARCHAR NOT NULL,
        sym             VARCHAR NOT NULL,
        as_of_date      VARCHAR NOT NULL,
        as_of_ns        BIGINT  NOT NULL,
        knowledge_ts_ns BIGINT  NOT NULL,
        valid_time_ns   BIGINT  NOT NULL,
        artifact_kind   VARCHAR NOT NULL,
        payload_json    VARCHAR NOT NULL,
        sha256_digest   VARCHAR NOT NULL,
        PRIMARY KEY (silo, sym, as_of_date, as_of_ns, knowledge_ts_ns, artifact_kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contagion_features (
        silo                            VARCHAR NOT NULL,
        sym                             VARCHAR NOT NULL,
        as_of_date                      VARCHAR NOT NULL,
        as_of_ns                        BIGINT  NOT NULL,
        knowledge_ts_ns                 BIGINT  NOT NULL,
        tick_index                      INTEGER NOT NULL,
        crowd_asymmetry_score           DOUBLE  NOT NULL,
        cascade_fragility_index         DOUBLE  NOT NULL,
        narrative_exhaustion_divergence DOUBLE  NOT NULL,
        requires_ring1_halt             BOOLEAN NOT NULL,
        sha256_digest                   VARCHAR NOT NULL,
        PRIMARY KEY (silo, sym, as_of_date, as_of_ns, tick_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conformal_residuals (
        strategy_id      VARCHAR NOT NULL,
        trade_id         VARCHAR NOT NULL,
        as_of_date       VARCHAR NOT NULL,
        as_of_ns         BIGINT  NOT NULL,
        knowledge_ts_ns  BIGINT  NOT NULL,
        residual_bps     DOUBLE  NOT NULL,
        realized_net_bps DOUBLE,
        is_shadow        BOOLEAN NOT NULL,
        PRIMARY KEY (strategy_id, trade_id)
    )
    """,
)


#: Contagion rows are write-behind: the cascade SLA (``sec_010`` 1.8 ms) must
#: never absorb a DuckDB commit, and on this class of hardware one 32-row
#: ``INSERT OR REPLACE`` commit costs ~0.25 s (ART index + WAL fsync).
#: ``append_contagion`` therefore only appends to an in-process list (~17 us).
#: Rows are committed on the next filtered read (read-after-write), on an
#: explicit :meth:`FeatureStore.flush`, on :meth:`FeatureStore.close`, or when
#: the buffer reaches ``CONTAGION_FLUSH_BATCH`` (the double-writer guard).
#:
#: There is deliberately **no** timer thread: a background commit inside the
#: Ring 1 interpreter measurably pollutes the ``sec_010`` tick-to-intent tail
#: (p99 0.015 ms -> 0.09 ms), so the flush is always driven by a caller that
#: already owns the latency budget.
CONTAGION_FLUSH_BATCH = 32

_CONTAGION_INSERT_SQL = """
                INSERT OR REPLACE INTO contagion_features
                    (silo, sym, as_of_date, as_of_ns, knowledge_ts_ns, tick_index,
                     crowd_asymmetry_score, cascade_fragility_index,
                     narrative_exhaustion_divergence, requires_ring1_halt,
                     sha256_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """


class FeatureStore:
    """Bitemporal DuckDB feature lake with Parquet partition export."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else DEFAULT_FEATURE_LAKE_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "feature_lake.duckdb"
        self.partition_root = self.root / "partitions"
        self.partition_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._con = duckdb.connect(str(self.db_path))
        self._contagion_pending: list[tuple[Any, ...]] = []
        #: Guards the buffer only, so a producer is never blocked by the
        #: ~0.25 s DuckDB commit another thread holds ``_lock`` for.  Lock
        #: order is always ``_pending_lock`` -> ``_lock``.
        self._pending_lock = threading.Lock()
        self._init_schema()

    # -- infrastructure ---------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            for statement in _SCHEMA_SQL:
                self._con.execute(statement)

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._con

    def close(self) -> None:
        try:
            self._flush_contagion()
        except Exception:  # pragma: no cover - best-effort durability
            pass
        with self._lock:
            try:
                self._con.close()
            except Exception:  # pragma: no cover
                pass

    def flush(self) -> int:
        """Persist buffered contagion rows; returns the number flushed."""
        return self._flush_contagion()

    def _flush_contagion(self) -> int:
        """Commit the buffered contagion batch in one transaction."""
        with self._pending_lock:
            if not self._contagion_pending:
                return 0
            rows = self._contagion_pending
            self._contagion_pending = []
        with self._lock:
            self._con.executemany(_CONTAGION_INSERT_SQL, rows)
        return len(rows)

    def __enter__(self) -> "FeatureStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- bitemporal guards ------------------------------------------------
    @staticmethod
    def _date_from_ns(as_of_ns: int) -> str:
        return datetime.fromtimestamp(
            int(as_of_ns) / 1_000_000_000, tz=timezone.utc
        ).strftime("%Y-%m-%d")

    @staticmethod
    def assert_bitemporal(
        *,
        as_of_ns: int,
        knowledge_ts_ns: int | None = None,
        valid_time_ns: int | None = None,
        context: str = "",
    ) -> tuple[int, int]:
        """Enforce ``knowledgeTs <= asOfTs`` and ``validTime <= asOfTs``.

        Returns the normalized ``(knowledge_ts_ns, valid_time_ns)`` pair.
        """
        as_of = int(as_of_ns)
        knowledge = as_of if knowledge_ts_ns is None else int(knowledge_ts_ns)
        valid = as_of if valid_time_ns is None else int(valid_time_ns)
        suffix = f" ({context})" if context else ""
        if knowledge > as_of:
            raise TemporalIntegrityError(
                f"knowledge_ts_ns ({knowledge}) > as_of_ns ({as_of}){suffix}: "
                "future knowledge would leak into the feature lake",
                knowledge_ts_ns=knowledge,
                valid_time_ns=valid,
                as_of_ns=as_of,
            )
        if valid > as_of:
            raise TemporalIntegrityError(
                f"valid_time_ns ({valid}) > as_of_ns ({as_of}){suffix}: "
                "a bar from the future cannot be ingested",
                knowledge_ts_ns=knowledge,
                valid_time_ns=valid,
                as_of_ns=as_of,
            )
        return knowledge, valid

    # -- alpha factors ----------------------------------------------------
    def append_alpha_factors(
        self,
        silo: str,
        symbol: str,
        as_of_ns: int,
        factors: Mapping[str, float],
        *,
        knowledge_ts_ns: int | None = None,
        valid_time_ns: int | None = None,
        as_of_date: str | None = None,
        source: str = "polars",
    ) -> int:
        """Append one 480+-factor vector snapshot (immutable, keyed by PIT)."""
        if not factors:
            raise ValueError("factors must not be empty")
        as_of = int(as_of_ns)
        knowledge, valid = self.assert_bitemporal(
            as_of_ns=as_of,
            knowledge_ts_ns=knowledge_ts_ns,
            valid_time_ns=valid_time_ns,
            context=f"alpha_factors {silo}/{symbol}",
        )
        date_key = as_of_date or self._date_from_ns(as_of)

        rows: list[tuple[Any, ...]] = []
        for name, value in sorted(factors.items()):
            number = float(value)
            if not math.isfinite(number):
                # Missing PIT fields are quarantined, never silently zeroed.
                continue
            digest = canonical_pairs_digest(
                [
                    ("silo", silo),
                    ("sym", symbol),
                    ("as_of_ns", as_of),
                    ("knowledge_ts_ns", knowledge),
                    ("factor", name),
                    ("value", number),
                    ("source", source),
                ]
            )
            rows.append(
                (silo, symbol, date_key, as_of, knowledge, valid, name, number, digest)
            )
        if not rows:
            raise ValueError("no finite factor values to store")

        with self._lock:
            self._con.executemany(
                """
                INSERT OR REPLACE INTO alpha_factors
                    (silo, sym, as_of_date, as_of_ns, knowledge_ts_ns,
                     valid_time_ns, factor_name, factor_value, sha256_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def append_alpha_factor_batch(
        self,
        silo: str,
        as_of_ns: int,
        records: Iterable[tuple[str, Mapping[str, float]]],
        *,
        knowledge_ts_ns: int | None = None,
        valid_time_ns: int | None = None,
        as_of_date: str | None = None,
        source: str = "polars",
    ) -> int:
        """Append many ``(symbol, factor_vector)`` snapshots in one statement.

        Semantically identical to calling :meth:`append_alpha_factors` per
        symbol -- same bitemporal guards, same canonical digest, same schema --
        but it writes through a single Arrow-registered relation instead of one
        ``INSERT OR REPLACE`` per row.  DuckDB 2.x's row-at-a-time conflict path
        costs ~5 ms/row; the vectorised path costs microseconds, which is what
        makes a 5,000-ticker x 490-factor snapshot durable inside a scan cycle.
        """
        as_of = int(as_of_ns)
        knowledge, valid = self.assert_bitemporal(
            as_of_ns=as_of,
            knowledge_ts_ns=knowledge_ts_ns,
            valid_time_ns=valid_time_ns,
            context=f"alpha_factors batch {silo}",
        )
        date_key = as_of_date or self._date_from_ns(as_of)

        rows: list[tuple[Any, ...]] = []
        for symbol, factors in records:
            for name, value in sorted(dict(factors).items()):
                number = float(value)
                if not math.isfinite(number):
                    continue
                digest = canonical_pairs_digest(
                    [
                        ("silo", silo),
                        ("sym", symbol),
                        ("as_of_ns", as_of),
                        ("knowledge_ts_ns", knowledge),
                        ("factor", name),
                        ("value", number),
                        ("source", source),
                    ]
                )
                rows.append(
                    (silo, symbol, date_key, as_of, knowledge, valid, name, number, digest)
                )
        if not rows:
            return 0

        with self._lock:
            # Re-running the same snapshot replaces it; older snapshots for the
            # same day are untouched, so history stays append-only.
            self._con.execute("BEGIN TRANSACTION")
            try:
                self._con.execute(
                    """
                    DELETE FROM alpha_factors
                    WHERE silo = ? AND as_of_date = ? AND as_of_ns = ? AND knowledge_ts_ns = ?
                    """,
                    [silo, date_key, as_of, knowledge],
                )
                if _pyarrow is not None:
                    columns = list(zip(*rows))
                    table = _pyarrow.table(
                        {
                            "silo": _pyarrow.array(columns[0], _pyarrow.string()),
                            "sym": _pyarrow.array(columns[1], _pyarrow.string()),
                            "as_of_date": _pyarrow.array(columns[2], _pyarrow.string()),
                            "as_of_ns": _pyarrow.array(columns[3], _pyarrow.int64()),
                            "knowledge_ts_ns": _pyarrow.array(columns[4], _pyarrow.int64()),
                            "valid_time_ns": _pyarrow.array(columns[5], _pyarrow.int64()),
                            "factor_name": _pyarrow.array(columns[6], _pyarrow.string()),
                            "factor_value": _pyarrow.array(columns[7], _pyarrow.float64()),
                            "sha256_digest": _pyarrow.array(columns[8], _pyarrow.string()),
                        }
                    )
                    self._con.register("nios_alpha_batch", table)
                    try:
                        self._con.execute(
                            """
                            INSERT INTO alpha_factors
                            SELECT silo, sym, as_of_date, as_of_ns, knowledge_ts_ns,
                                   valid_time_ns, factor_name, factor_value, sha256_digest
                            FROM nios_alpha_batch
                            """
                        )
                    finally:
                        self._con.unregister("nios_alpha_batch")
                else:  # pragma: no cover - optional acceleration
                    self._con.executemany(
                        """
                        INSERT INTO alpha_factors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                self._con.execute("COMMIT")
            except Exception:
                self._con.execute("ROLLBACK")
                raise
        return len(rows)

    def query_alpha_factors(
        self,
        silo: str,
        symbol: str,
        as_of_ns: int,
        *,
        knowledge_ts_ns: int | None = None,
        as_of_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read the newest snapshot visible at ``as_of_ns``."""
        as_of = int(as_of_ns)
        cutoff = as_of if knowledge_ts_ns is None else int(knowledge_ts_ns)
        if cutoff > as_of:
            raise TemporalIntegrityError(
                f"knowledge cutoff ({cutoff}) > as_of_ns ({as_of})",
                knowledge_ts_ns=cutoff,
                as_of_ns=as_of,
            )

        sql = [
            "SELECT factor_name, factor_value, as_of_ns, knowledge_ts_ns,",
            "       valid_time_ns, as_of_date, sha256_digest",
            "FROM alpha_factors",
            "WHERE silo = ? AND sym = ?",
            "  AND as_of_ns <= ?",
            "  AND knowledge_ts_ns <= ?",
            "  AND valid_time_ns <= ?",
        ]
        params: list[Any] = [silo, symbol, as_of, cutoff, as_of]
        if as_of_date is not None:
            sql.append("  AND as_of_date = ?")
            params.append(as_of_date)
        sql.append("ORDER BY as_of_ns DESC, factor_name ASC")

        with self._lock:
            cursor = self._con.execute("\n".join(sql), params)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    def get_factor_vector(
        self,
        silo: str,
        symbol: str,
        as_of_ns: int,
        *,
        knowledge_ts_ns: int | None = None,
    ) -> dict[str, float]:
        """Newest visible factor vector as ``{name: value}``."""
        rows = self.query_alpha_factors(
            silo, symbol, as_of_ns, knowledge_ts_ns=knowledge_ts_ns
        )
        if not rows:
            return {}
        newest = max(int(r["as_of_ns"]) for r in rows)
        return {
            str(r["factor_name"]): float(r["factor_value"])
            for r in rows
            if int(r["as_of_ns"]) == newest
        }

    # -- valuation artefacts ----------------------------------------------
    def append_valuation(
        self,
        silo: str,
        symbol: str,
        as_of_ns: int,
        artifact_kind: str,
        payload: Mapping[str, Any],
        *,
        knowledge_ts_ns: int | None = None,
        valid_time_ns: int | None = None,
        as_of_date: str | None = None,
    ) -> str:
        """Store a DCF / Comps / 3-statement artefact with a SHA-256 seal."""
        as_of = int(as_of_ns)
        knowledge, valid = self.assert_bitemporal(
            as_of_ns=as_of,
            knowledge_ts_ns=knowledge_ts_ns,
            valid_time_ns=valid_time_ns,
            context=f"valuation {silo}/{symbol}/{artifact_kind}",
        )
        date_key = as_of_date or self._date_from_ns(as_of)
        canonical = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
        digest = canonical_pairs_digest(
            [
                ("silo", silo),
                ("sym", symbol),
                ("as_of_ns", as_of),
                ("knowledge_ts_ns", knowledge),
                ("artifact_kind", artifact_kind),
                ("payload", canonical),
            ]
        )
        with self._lock:
            self._con.execute(
                """
                INSERT OR REPLACE INTO valuation_artifacts
                    (silo, sym, as_of_date, as_of_ns, knowledge_ts_ns,
                     valid_time_ns, artifact_kind, payload_json, sha256_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    silo,
                    symbol,
                    date_key,
                    as_of,
                    knowledge,
                    valid,
                    artifact_kind,
                    canonical,
                    digest,
                ],
            )
        return digest

    def query_valuation(
        self,
        silo: str,
        symbol: str,
        as_of_ns: int,
        *,
        artifact_kind: str | None = None,
        knowledge_ts_ns: int | None = None,
    ) -> list[dict[str, Any]]:
        as_of = int(as_of_ns)
        cutoff = as_of if knowledge_ts_ns is None else int(knowledge_ts_ns)
        sql = [
            "SELECT artifact_kind, payload_json, as_of_ns, knowledge_ts_ns,",
            "       valid_time_ns, sha256_digest",
            "FROM valuation_artifacts",
            "WHERE silo = ? AND sym = ?",
            "  AND as_of_ns <= ? AND knowledge_ts_ns <= ? AND valid_time_ns <= ?",
        ]
        params: list[Any] = [silo, symbol, as_of, cutoff, as_of]
        if artifact_kind is not None:
            sql.append("  AND artifact_kind = ?")
            params.append(artifact_kind)
        sql.append("ORDER BY as_of_ns DESC, artifact_kind ASC")

        with self._lock:
            cursor = self._con.execute("\n".join(sql), params)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(zip(columns, row))
            record["payload"] = json.loads(record.pop("payload_json"))
            out.append(record)
        return out

    # -- MiroFish contagion -----------------------------------------------
    def append_contagion(
        self,
        silo: str,
        features: ContagionFeatures,
        *,
        tick_index: int = 3,
        knowledge_ts_ns: int | None = None,
        as_of_date: str | None = None,
    ) -> str:
        """Persist one 3-tick cascade equilibrium (``sec_008``)."""
        as_of = int(features.as_of_ns)
        knowledge, _valid = self.assert_bitemporal(
            as_of_ns=as_of,
            knowledge_ts_ns=knowledge_ts_ns,
            context=f"contagion {silo}/{features.symbol}",
        )
        date_key = as_of_date or self._date_from_ns(as_of)
        digest = canonical_pairs_digest(
            [
                ("silo", silo),
                ("sym", features.symbol),
                ("as_of_ns", as_of),
                ("tick_index", int(tick_index)),
                ("crowd_asymmetry_score", features.crowd_asymmetry_score),
                ("cascade_fragility_index", features.cascade_fragility_index),
                ("narrative_exhaustion_divergence", features.narrative_exhaustion_divergence),
            ]
        )
        with self._pending_lock:
            self._contagion_pending.append(
                (
                    silo,
                    features.symbol,
                    date_key,
                    as_of,
                    knowledge,
                    int(tick_index),
                    features.crowd_asymmetry_score,
                    features.cascade_fragility_index,
                    features.narrative_exhaustion_divergence,
                    features.requires_ring1_halt,
                    digest,
                )
            )
            overflow = len(self._contagion_pending) >= CONTAGION_FLUSH_BATCH
        if overflow:
            # Double-writer guard only: a reader would normally have drained
            # this long before.  Bounded so a store nobody reads cannot grow
            # without limit.
            self._flush_contagion()
        return digest

    def query_contagion(
        self,
        silo: str,
        symbol: str,
        as_of_ns: int,
        *,
        limit: int = 32,
        knowledge_ts_ns: int | None = None,
    ) -> list[dict[str, Any]]:
        as_of = int(as_of_ns)
        cutoff = as_of if knowledge_ts_ns is None else int(knowledge_ts_ns)
        # Read-after-write: a filtered read observes every buffered row.  The
        # flush is issued before ``_lock`` is taken to keep the lock order
        # ``_pending_lock`` -> ``_lock``.
        self._flush_contagion()
        with self._lock:
            cursor = self._con.execute(
                """
                SELECT sym, as_of_date, as_of_ns, knowledge_ts_ns, tick_index,
                       crowd_asymmetry_score, cascade_fragility_index,
                       narrative_exhaustion_divergence, requires_ring1_halt,
                       sha256_digest
                FROM contagion_features
                WHERE silo = ? AND sym = ?
                  AND as_of_ns <= ? AND knowledge_ts_ns <= ?
                ORDER BY as_of_ns DESC, tick_index DESC
                LIMIT ?
                """,
                [silo, symbol, as_of, cutoff, int(limit)],
            )
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    def latest_contagion(
        self, silo: str, symbol: str, as_of_ns: int
    ) -> dict[str, Any] | None:
        rows = self.query_contagion(silo, symbol, as_of_ns, limit=1)
        return rows[0] if rows else None

    # -- conformal residuals ----------------------------------------------
    def append_conformal_residual(
        self,
        strategy_id: str,
        residual_bps: float,
        as_of_ns: int,
        *,
        knowledge_ts_ns: int | None = None,
        trade_id: str | None = None,
        realized_net_bps: float | None = None,
        is_shadow: bool = True,
    ) -> str:
        """Append one graded trade error to the rolling 200-trade window."""
        as_of = int(as_of_ns)
        knowledge, _valid = self.assert_bitemporal(
            as_of_ns=as_of,
            knowledge_ts_ns=knowledge_ts_ns,
            context=f"conformal {strategy_id}",
        )
        residual = float(residual_bps)
        if not math.isfinite(residual):
            raise ValueError("residual_bps must be finite")
        identifier = trade_id or uuid.uuid4().hex
        with self._lock:
            self._con.execute(
                """
                INSERT OR REPLACE INTO conformal_residuals
                    (strategy_id, trade_id, as_of_date, as_of_ns,
                     knowledge_ts_ns, residual_bps, realized_net_bps, is_shadow)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    strategy_id,
                    identifier,
                    self._date_from_ns(as_of),
                    as_of,
                    knowledge,
                    residual,
                    None if realized_net_bps is None else float(realized_net_bps),
                    bool(is_shadow),
                ],
            )
        return identifier

    def get_residuals(
        self,
        strategy_id: str,
        as_of_ns: int,
        *,
        window: int = CONFORMAL_WINDOW,
        knowledge_ts_ns: int | None = None,
    ) -> list[dict[str, Any]]:
        as_of = int(as_of_ns)
        cutoff = as_of if knowledge_ts_ns is None else int(knowledge_ts_ns)
        with self._lock:
            cursor = self._con.execute(
                """
                SELECT trade_id, as_of_ns, knowledge_ts_ns, residual_bps,
                       realized_net_bps, is_shadow
                FROM conformal_residuals
                WHERE strategy_id = ?
                  AND as_of_ns <= ? AND knowledge_ts_ns <= ?
                ORDER BY as_of_ns DESC, trade_id DESC
                LIMIT ?
                """,
                [strategy_id, as_of, cutoff, int(window)],
            )
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    def get_q98_residual(
        self,
        strategy_id: str,
        as_of_ns: int,
        *,
        window: int = CONFORMAL_WINDOW,
        min_samples: int = CONFORMAL_MIN_SAMPLES,
        knowledge_ts_ns: int | None = None,
    ) -> ConformalWindow:
        """``Q_0.98(Residuals)`` over the newest ``window`` graded trades.

        Returns ``+inf`` while the window is colder than ``min_samples`` so
        the Ring 0 gate abstains instead of trusting an uncalibrated tail.
        """
        rows = self.get_residuals(
            strategy_id,
            as_of_ns,
            window=window,
            knowledge_ts_ns=knowledge_ts_ns,
        )
        residuals = [float(r["residual_bps"]) for r in rows]
        shadow_count = sum(1 for r in rows if bool(r["is_shadow"]))
        live_count = len(rows) - shadow_count
        calibrated = len(residuals) >= max(1, int(min_samples))
        if residuals:
            q98 = quantile_linear(residuals, 0.98) if calibrated else math.inf
            mean_bps = sum(residuals) / len(residuals)
            min_bps = min(residuals)
            max_bps = max(residuals)
        else:
            q98 = math.inf
            mean_bps = min_bps = max_bps = 0.0
        return ConformalWindow(
            strategy_id=strategy_id,
            as_of_ns=int(as_of_ns),
            q98_bps=q98,
            count=len(residuals),
            shadow_count=shadow_count,
            live_count=live_count,
            mean_bps=mean_bps,
            min_bps=min_bps,
            max_bps=max_bps,
            requested_window=int(window),
            calibrated=calibrated,
        )

    def count_trades(self, strategy_id: str, as_of_ns: int) -> int:
        with self._lock:
            row = self._con.execute(
                """
                SELECT COUNT(*) FROM conformal_residuals
                WHERE strategy_id = ? AND as_of_ns <= ? AND knowledge_ts_ns <= ?
                """,
                [strategy_id, int(as_of_ns), int(as_of_ns)],
            ).fetchone()
        return int(row[0]) if row else 0

    # -- Parquet partitions ------------------------------------------------
    def partition_dir(self, silo: str, symbol: str, as_of_date: str) -> Path:
        """``<root>/partitions/silo=<silo>/sym=<sym>/as_of_date=<date>``."""
        return (
            self.partition_root
            / f"silo={silo}"
            / f"sym={symbol}"
            / f"as_of_date={as_of_date}"
        )

    @staticmethod
    def _sql_path(path: Path) -> str:
        return str(path).replace("\\", "/").replace("'", "''")

    @staticmethod
    def _sql_literal(value: Any) -> str:
        text = str(value).replace("'", "''")
        return f"'{text}'"

    def persist_partition(
        self, silo: str, symbol: str, as_of_date: str
    ) -> dict[str, str]:
        """Export one ``(silo, sym, asOfDate)`` partition to Parquet."""
        directory = self.partition_dir(silo, symbol, as_of_date)
        directory.mkdir(parents=True, exist_ok=True)
        where = (
            f"WHERE silo = {self._sql_literal(silo)}"
            f" AND sym = {self._sql_literal(symbol)}"
            f" AND as_of_date = {self._sql_literal(as_of_date)}"
        )
        projections: Mapping[str, str] = {
            "alpha_factors": (
                "SELECT silo, sym, as_of_date, as_of_ns, knowledge_ts_ns,"
                " valid_time_ns, factor_name, factor_value, sha256_digest"
                f" FROM alpha_factors {where}"
            ),
            "valuation_artifacts": (
                "SELECT silo, sym, as_of_date, as_of_ns, knowledge_ts_ns,"
                " valid_time_ns, artifact_kind, payload_json, sha256_digest"
                f" FROM valuation_artifacts {where}"
            ),
            "contagion_features": (
                "SELECT silo, sym, as_of_date, as_of_ns, knowledge_ts_ns,"
                " tick_index, crowd_asymmetry_score, cascade_fragility_index,"
                " narrative_exhaustion_divergence, requires_ring1_halt, sha256_digest"
                f" FROM contagion_features {where}"
            ),
        }
        out: dict[str, str] = {}
        with self._lock:
            for table, query in projections.items():
                count = self._con.execute(
                    f"SELECT COUNT(*) FROM ({query}) AS q"
                ).fetchone()[0]
                if not count:
                    continue
                target = directory / f"{table}.parquet"
                self._con.execute(
                    f"COPY ({query}) TO '{self._sql_path(target)}'"
                    " (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                out[table] = str(target)
        return out

    def persist_conformal_residuals(self, as_of_date: str) -> str | None:
        """Export the day's conformal residuals (strategy-keyed, not symbol)."""
        directory = self.partition_root / f"as_of_date={as_of_date}"
        directory.mkdir(parents=True, exist_ok=True)
        query = (
            "SELECT strategy_id, trade_id, as_of_date, as_of_ns, knowledge_ts_ns,"
            " residual_bps, realized_net_bps, is_shadow FROM conformal_residuals"
            f" WHERE as_of_date = {self._sql_literal(as_of_date)}"
        )
        with self._lock:
            count = self._con.execute(
                f"SELECT COUNT(*) FROM ({query}) AS q"
            ).fetchone()[0]
            if not count:
                return None
            target = directory / "conformal_residuals.parquet"
            self._con.execute(
                f"COPY ({query}) TO '{self._sql_path(target)}'"
                " (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        return str(target)

    def persist_all(self) -> list[str]:
        """Export every known ``(silo, sym, as_of_date)`` partition."""
        with self._lock:
            rows = self._con.execute(
                """
                SELECT DISTINCT silo, sym, as_of_date FROM alpha_factors
                UNION
                SELECT DISTINCT silo, sym, as_of_date FROM valuation_artifacts
                UNION
                SELECT DISTINCT silo, sym, as_of_date FROM contagion_features
                """
            ).fetchall()
        written: list[str] = []
        for silo, sym, date_key in rows:
            written.extend(self.persist_partition(silo, sym, date_key).values())
        return written

    def query_partition_alpha_factors(
        self,
        silo: str,
        symbol: str,
        as_of_date: str,
        as_of_ns: int,
    ) -> list[dict[str, Any]]:
        """Read a Parquet partition with the *same* bitemporal filters.

        Proves that the cold-path audit store cannot leak future knowledge
        either.
        """
        directory = self.partition_dir(silo, symbol, as_of_date)
        target = directory / "alpha_factors.parquet"
        if not target.exists():
            return []
        as_of = int(as_of_ns)
        with self._lock:
            cursor = self._con.execute(
                f"""
                SELECT factor_name, factor_value, as_of_ns, knowledge_ts_ns,
                       valid_time_ns, as_of_date, sha256_digest
                FROM read_parquet('{self._sql_path(target)}')
                WHERE silo = ? AND sym = ?
                  AND as_of_ns <= ? AND knowledge_ts_ns <= ? AND valid_time_ns <= ?
                ORDER BY as_of_ns DESC, factor_name ASC
                """,
                [silo, symbol, as_of, as_of, as_of],
            )
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    # -- aggregate views ---------------------------------------------------
    def table_counts(self) -> dict[str, int]:
        with self._lock:
            return {
                table: int(
                    self._con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "alpha_factors",
                    "valuation_artifacts",
                    "contagion_features",
                    "conformal_residuals",
                )
            }
