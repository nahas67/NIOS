"""NIOS V2.3 -- real PostgreSQL 16 repository with versioned migrations (``PART 1.2``).

The settlement schema (:mod:`ring0_vault.ibor.pg16_schema`) has always been
validated offline against a SQLite translation.  This module is the **live** half:
a pooled repository that applies the schema as numbered migrations, records a
SHA-256 of every migration body, proves each migration's rollback is valid, and
answers the entity queries the memory tier needs.

Design rules
------------
* **Offline by default.** ``psycopg`` / ``psycopg_pool`` are imported *inside* the
  methods that use them, and only when ``NIOS_PG_DSN`` (or an explicit ``dsn``)
  is set.  Constructing :class:`Pg16Repository` with no DSN therefore never
  touches a driver, never opens a socket, and reports ``available=False`` -- which
  is what keeps ``pytest`` and ``--smoke-test`` deterministic.
* **Forward-only, checksum-pinned.** Every migration records the SHA-256 of its own
  body in ``nios_schema_migrations``.  Re-running against a database whose
  recorded checksum no longer matches the code raises instead of silently
  applying different DDL over live data.
* **Rollback is proven, not claimed.** :meth:`Pg16Repository.verify_rollback`
  executes each migration's forward SQL *and* its rollback SQL inside a
  transaction that is then rolled back, so the proof costs nothing and leaves no
  trace.  A migration whose rollback is invalid fails loudly here rather than
  during an incident.
* **Parameterised only.** The entity queries bind values through placeholders and
  cast JSON with ``%s::jsonb``; no value is ever interpolated into SQL text.

Ring containment: Ring 0 storage plumbing.  It holds no broker credential and
imports no Ring 2/3 module; the driver is an optional, lazily-resolved dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "MIGRATION_LOCK_KEY",
    "Migration",
    "MigrationChecksumMismatch",
    "Pg16Repository",
    "Pg16Unavailable",
    "migrations",
    "pg_schema_statements",
]

#: Transaction-scoped advisory lock id used to serialise concurrent migrators.
#: ``'NIOS'`` in ASCII, so the constant is recognisable in ``pg_locks``.
MIGRATION_LOCK_KEY: int = 0x4E494F53

#: Default pool ceiling.  A Ring 0 writer is one caller at a time; four connections
#: leave head-room for the read surfaces without letting a leak hide.
DEFAULT_MAX_POOL_SIZE: int = 4

#: Default connect/acquire timeout (seconds).  Bounded on purpose: a probe that
#: hangs is worse than one that reports the database unreachable.
DEFAULT_TIMEOUT_S: float = 5.0


class Pg16Unavailable(RuntimeError):
    """Raised when a live PostgreSQL operation is attempted without a broker."""


class MigrationChecksumMismatch(RuntimeError):
    """Raised when a recorded migration checksum disagrees with the code."""


def _redact_dsn(dsn: str | None) -> str | None:
    """Hide the password of a DSN before it reaches a log or a JSON report."""
    if not dsn:
        return None
    token = str(dsn)
    if "@" not in token:
        return token
    scheme, _, tail = token.partition("://")
    if not tail:
        return "<redacted>"
    credentials, _, host = tail.partition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if scheme else f"{user}:***@{host}"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise_sql(sql: str) -> str:
    """Whitespace-normalised SQL, so formatting edits do not change a checksum."""
    return " ".join(str(sql).split())


def _ns_to_datetime(stamp_ns: int | None) -> datetime:
    """Convert Ring 0 nanoseconds to an aware UTC ``datetime`` (0 -> epoch)."""
    micros = max(0, int(stamp_ns or 0)) // 1_000
    return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Migration chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward DDL step plus the exact SQL that undoes it."""

    version: int
    name: str
    forward_sql: str
    rollback_sql: str

    @property
    def checksum_sha256(self) -> str:
        """SHA-256 of the normalised forward body (the recorded pin)."""
        return _sha256_text(f"{self.version}:{self.name}:{_normalise_sql(self.forward_sql)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "checksum_sha256": self.checksum_sha256,
            "forward_bytes": len(self.forward_sql.encode("utf-8")),
            "rollback_bytes": len(self.rollback_sql.encode("utf-8")),
        }


#: Tables dropped by the baseline rollback, in reverse dependency order.
_BASELINE_TABLES: tuple[str, ...] = (
    "nios_schema_migrations",
    "nios_episodic_memory",
    "nios_memory_checkpoints",
    "nios_compiled_entity_truths",
    "nios_entity_timeline",
    "grounding_ledger_hashes",
    "source_quarantine_store",
    "tax_lots",
    "silo_vault_balances",
    "ibor_double_entry_ledger",
    "order_ack_inbox",
    "financial_outbox",
)

_SCHEMA_PATH: Path = Path(__file__).with_name("pg16_schema.sql")

#: Lines that own the transaction lifecycle of the standalone file.  The
#: repository supplies its own ``BEGIN``/``COMMIT`` and advisory lock, so they are
#: stripped before the body is executed statement by statement.
_TRANSACTION_STATEMENTS: tuple[str, ...] = ("BEGIN", "COMMIT", "SET TRANSACTION")


def _split_statements(text: str) -> list[str]:
    """Split PostgreSQL DDL on top-level semicolons.

    Handles line comments, single-quoted strings and dollar quoting.  The schema
    file uses none of the exotic forms today, but a splitter that only understands
    ``;`` would silently corrupt a future function body, so the real rules are
    implemented once here.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(text)
    in_single = False
    in_line_comment = False
    dollar_tag: str | None = None
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                buffer.append(char)
            index += 1
            continue
        if dollar_tag is not None:
            if text.startswith(dollar_tag, index):
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            buffer.append(char)
            index += 1
            continue
        if in_single:
            buffer.append(char)
            if char == "'" and nxt == "'":
                buffer.append(nxt)
                index += 2
                continue
            if char == "'":
                in_single = False
            index += 1
            continue
        if char == "-" and nxt == "-":
            in_line_comment = True
            index += 2
            continue
        if char == "'":
            in_single = True
            buffer.append(char)
            index += 1
            continue
        if char == "$":
            tag_match = None
            cursor = index + 1
            while cursor < length and (text[cursor].isalnum() or text[cursor] == "_"):
                cursor += 1
            if cursor < length and text[cursor] == "$":
                tag_match = text[index : cursor + 1]
            if tag_match:
                dollar_tag = tag_match
                buffer.append(tag_match)
                index += len(tag_match)
                continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def pg_schema_statements(schema_path: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    """The shipped ``pg16_schema.sql`` as executable statements.

    The file's own ``BEGIN`` / ``COMMIT`` / ``SET TRANSACTION`` statements are
    removed: the repository opens the transaction (with an advisory lock) so the
    migration and its ledger row commit or roll back as one unit.
    """
    path = Path(schema_path) if schema_path is not None else _SCHEMA_PATH
    text = path.read_text(encoding="utf-8")
    statements: list[str] = []
    for statement in _split_statements(text):
        head = _normalise_sql(statement).upper()
        if any(head.startswith(prefix) for prefix in _TRANSACTION_STATEMENTS):
            continue
        statements.append(statement)
    return tuple(statements)


def migrations() -> tuple[Migration, ...]:
    """The ordered migration chain (rebuilt from the shipped schema each call).

    Version 1 is the whole settlement schema, which is idempotent (``CREATE TABLE
    IF NOT EXISTS``), so an existing V2.2 database is upgraded in place and the
    three V2.3 tables appear without a destructive step.  Versions 2 and 3 add the
    read indexes the episodic and timeline queries rely on; keeping them separate
    is what makes the chain genuinely versioned rather than one blanket script.
    """
    baseline_body = ";\n".join(pg_schema_statements()) + ";"
    baseline_rollback = ";\n".join(
        f"DROP TABLE IF EXISTS {table} CASCADE" for table in _BASELINE_TABLES
    ) + ";"
    return (
        Migration(
            version=1,
            name="0001_core_settlement_and_memory_schema",
            forward_sql=baseline_body,
            rollback_sql=baseline_rollback,
        ),
        Migration(
            version=2,
            name="0002_entity_timeline_regime_index",
            forward_sql=(
                "CREATE INDEX IF NOT EXISTS idx_entity_timeline_regime"
                " ON nios_entity_timeline (outcome_kind, ts_utc DESC);"
            ),
            rollback_sql="DROP INDEX IF EXISTS idx_entity_timeline_regime;",
        ),
        Migration(
            version=3,
            name="0003_episodic_memory_actor_index",
            forward_sql=(
                "CREATE INDEX IF NOT EXISTS idx_episodic_memory_actor"
                " ON nios_episodic_memory (actor, ts_utc DESC);"
            ),
            rollback_sql="DROP INDEX IF EXISTS idx_episodic_memory_actor;",
        ),
    )


class _RollbackSignal(Exception):
    """Internal sentinel: unwind a verify-rollback transaction without a result."""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class Pg16Repository:
    """Pooled PostgreSQL 16 repository for the settlement and memory schema.

    Construction is **inert**: no driver import, no connection, no DNS lookup.
    :attr:`available` answers whether the driver *and* a DSN are both present;
    every live method raises :class:`Pg16Unavailable` when it is not.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        max_pool_size: int = DEFAULT_MAX_POOL_SIZE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        application_name: str = "nios-ring0",
    ) -> None:
        self._dsn = (dsn if dsn is not None else os.environ.get("NIOS_PG_DSN")) or None
        self._max_pool_size = max(1, int(max_pool_size))
        self._timeout_s = max(0.5, float(timeout_s))
        self._application_name = str(application_name)
        self._pool: Any = None
        self._lock = threading.RLock()
        self._driver_error: str | None = None
        self._applied: tuple[dict[str, Any], ...] = ()

    # ------------------------------------------------------------- availability
    @property
    def dsn(self) -> str | None:
        """The configured DSN (never logged unredacted)."""
        return self._dsn

    @property
    def redacted_dsn(self) -> str | None:
        return _redact_dsn(self._dsn)

    def _imports(self) -> tuple[Any, Any]:
        """Lazily resolve ``psycopg`` and ``psycopg_pool`` (or report why not)."""
        try:  # pragma: no cover - the driver is absent from the offline venv
            import psycopg  # type: ignore
            import psycopg_pool  # type: ignore
        except Exception as exc:  # noqa: BLE001 - any import failure means "no driver"
            self._driver_error = f"PSYCOPG_NOT_INSTALLED:{type(exc).__name__}"
            raise Pg16Unavailable(self._driver_error) from None
        return psycopg, psycopg_pool

    @property
    def available(self) -> bool:
        """True only when a DSN is configured **and** the driver imports."""
        if not self._dsn:
            return False
        try:
            self._imports()
        except Pg16Unavailable:
            return False
        return True

    def _require_dsn(self) -> str:
        if not self._dsn:
            raise Pg16Unavailable("NO_DSN")
        return self._dsn

    # -------------------------------------------------------------------- pool
    def pool(self) -> Any:
        """The lazily-built connection pool (one per repository instance)."""
        with self._lock:
            if self._pool is not None:
                return self._pool
            dsn = self._require_dsn()
            _, psycopg_pool = self._imports()
            kwargs: dict[str, Any] = {
                "min_size": 1,
                "max_size": self._max_pool_size,
                "timeout": self._timeout_s,
                "kwargs": {"application_name": self._application_name},
            }
            try:
                pool = psycopg_pool.ConnectionPool(dsn, open=True, **kwargs)
            except TypeError:  # pragma: no cover - psycopg_pool < 3.2 signature
                pool = psycopg_pool.ConnectionPool(dsn, **kwargs)
                opener = getattr(pool, "open", None)
                if callable(opener):
                    opener()
            self._pool = pool
            return pool

    def close(self) -> None:
        """Close the pool, if one was ever opened."""
        with self._lock:
            pool, self._pool = self._pool, None
        closer = getattr(pool, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover - best-effort teardown
                pass

    def __enter__(self) -> "Pg16Repository":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -------------------------------------------------------------- migrations
    def apply_migrations(self) -> tuple[dict[str, Any], ...]:
        """Apply every pending migration and record its checksum.

        Runs under a transaction-scoped advisory lock, so two processes starting
        at once serialise instead of racing the same ``CREATE TABLE``.
        """
        self._require_dsn()
        applied: list[dict[str, Any]] = []
        with self.pool().connection() as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS nios_schema_migrations ("
                    " version INTEGER PRIMARY KEY CHECK (version >= 0),"
                    " name TEXT NOT NULL,"
                    " applied_at_utc TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                    " checksum_sha256 CHAR(64) NOT NULL,"
                    " CONSTRAINT schema_migration_digest_hex"
                    "   CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'))"
                )
                recorded = {
                    int(row[0]): str(row[1])
                    for row in connection.execute(
                        "SELECT version, checksum_sha256 FROM nios_schema_migrations"
                    ).fetchall()
                }
                for migration in migrations():
                    checksum = migration.checksum_sha256
                    known = recorded.get(int(migration.version))
                    if known is not None:
                        if known != checksum:
                            raise MigrationChecksumMismatch(
                                f"MIGRATION_CHECKSUM_MISMATCH:{migration.version}"
                                f":{migration.name}"
                            )
                        continue
                    connection.execute(migration.forward_sql)
                    connection.execute(
                        "INSERT INTO nios_schema_migrations"
                        " (version, name, applied_at_utc, checksum_sha256)"
                        " VALUES (%s, %s, %s, %s)",
                        (
                            int(migration.version),
                            migration.name,
                            datetime.now(timezone.utc),
                            checksum,
                        ),
                    )
                    applied.append(migration.to_dict())
        self._applied = tuple(applied)
        return tuple(applied)

    def migration_status(self) -> tuple[dict[str, Any], ...]:
        """Every migration with whether it is applied and whether it still matches."""
        self._require_dsn()
        with self.pool().connection() as connection:
            rows = connection.execute(
                "SELECT version, name, applied_at_utc, checksum_sha256"
                " FROM nios_schema_migrations ORDER BY version"
            ).fetchall()
        recorded = {
            int(row[0]): {
                "name": str(row[1]),
                "applied_at_utc": row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]),
                "checksum_sha256": str(row[3]),
            }
            for row in rows
        }
        status: list[dict[str, Any]] = []
        for migration in migrations():
            known = recorded.get(int(migration.version))
            status.append(
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum_sha256": migration.checksum_sha256,
                    "applied": known is not None,
                    "checksum_matches": bool(
                        known is not None and known["checksum_sha256"] == migration.checksum_sha256
                    ),
                    "applied_at_utc": known["applied_at_utc"] if known else None,
                }
            )
        return tuple(status)

    def verify_rollback(self) -> tuple[dict[str, Any], ...]:
        """Prove every migration's rollback SQL is valid without changing anything.

        Each migration is applied and rolled back **inside one transaction that is
        then unwound**, so the proof leaves no table, index or ledger row behind.
        PostgreSQL DDL is transactional, which is exactly why this is a real proof
        rather than a textual one.
        """
        self._require_dsn()
        results: list[dict[str, Any]] = []
        for migration in migrations():
            detail = ""
            ok = True
            try:
                with self.pool().connection() as connection:
                    with connection.transaction():
                        connection.execute(migration.forward_sql)
                        connection.execute(migration.rollback_sql)
                        raise _RollbackSignal()
            except _RollbackSignal:
                ok = True
            except Exception as exc:  # noqa: BLE001 - the failure *is* the result
                ok = False
                detail = f"{type(exc).__name__}: {exc}"
            results.append(
                {
                    "version": migration.version,
                    "name": migration.name,
                    "rollback_proven": ok,
                    "detail": detail,
                    "checksum_sha256": migration.checksum_sha256,
                }
            )
        return tuple(results)

    # ------------------------------------------------------------------ probe
    def probe(self) -> dict[str, Any]:
        """Connect and answer server facts; never raises for an offline setup."""
        if not self._dsn:
            return {"available": False, "reason": "NO_DSN", "dsn": None}
        try:
            _, _ = self._imports()
        except Pg16Unavailable as exc:
            return {
                "available": False,
                "reason": str(exc),
                "dsn": self.redacted_dsn,
            }
        started = datetime.now(timezone.utc)
        try:
            with self.pool().connection() as connection:
                row = connection.execute(
                    "SELECT version(), current_setting('server_version_num'),"
                    " current_database(), current_user"
                ).fetchone()
        except Exception as exc:  # noqa: BLE001 - unreachable is a status, not a crash
            return {
                "available": False,
                "reason": f"CONNECT_FAILED:{type(exc).__name__}",
                "detail": str(exc)[:400],
                "dsn": self.redacted_dsn,
            }
        elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
        version_text = str(row[0]) if row else ""
        server_version = int(row[1]) if row and row[1] is not None else 0
        return {
            "available": True,
            "reason": "OK",
            "dsn": self.redacted_dsn,
            "server_version": version_text,
            "server_version_num": server_version,
            "database": str(row[2]) if row else "",
            "user": str(row[3]) if row else "",
            "latency_ms": round(elapsed_ms, 3),
            "is_postgres_16": server_version >= 160000,
        }

    def ddl_roundtrip(self) -> dict[str, Any]:
        """Apply the schema + a GIN ``@>`` query inside a rolled-back transaction.

        This is the ``PART 1.3`` check: a real 12-table DDL apply, a real
        ``jsonb`` containment query against the GIN index, and a real rollback so
        the operator's database is left exactly as it was found.
        """
        self._require_dsn()
        report: dict[str, Any] = {"ok": False}
        try:
            with self.pool().connection() as connection:
                with connection.transaction():
                    for statement in pg_schema_statements():
                        connection.execute(statement)
                    tables = [
                        str(row[0])
                        for row in connection.execute(
                            "SELECT tablename FROM pg_tables"
                            " WHERE schemaname = current_schema() ORDER BY tablename"
                        ).fetchall()
                    ]
                    connection.execute(
                        "INSERT INTO nios_episodic_memory"
                        " (episode_id, ts_utc, actor, action_kind, regime,"
                        "  context_tags, decision_summary, payload_sha256)"
                        " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                        (
                            "EP-PROBE",
                            0,
                            "probe",
                            "READINESS_PROBE",
                            "",
                            json.dumps(["PROBE", "READINESS"]),
                            "schema round-trip probe",
                            hashlib.sha256(b"probe").hexdigest(),
                        ),
                    )
                    found = connection.execute(
                        "SELECT COUNT(*) FROM nios_episodic_memory"
                        " WHERE context_tags @> %s::jsonb",
                        (json.dumps(["PROBE"]),),
                    ).fetchone()
                    report = {
                        "ok": True,
                        "table_count": len(tables),
                        "tables": tables,
                        "gin_query_hits": int(found[0]) if found else 0,
                    }
                    raise _RollbackSignal()
        except _RollbackSignal:
            report["rolled_back"] = True
            report["ok"] = bool(report.get("gin_query_hits", 0) >= 1)
        except Exception as exc:  # noqa: BLE001 - a status report, not a crash
            report = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        return report

    # --------------------------------------------------------- entity queries
    def find_episodes_by_context_tags(
        self,
        tags: Sequence[str],
        *,
        action_kind: str | None = None,
        limit: int = 50,
    ) -> tuple[dict[str, Any], ...]:
        """Episodes whose ``context_tags`` contain **every** tag (GIN ``@>``)."""
        self._require_dsn()
        wanted = [str(tag) for tag in tags if str(tag)]
        if not wanted:
            return ()
        sql = (
            "SELECT episode_id, ts_utc, actor, action_kind, regime,"
            " context_tags, decision_summary, payload_sha256"
            " FROM nios_episodic_memory WHERE context_tags @> %s::jsonb"
        )
        params: list[Any] = [json.dumps(wanted)]
        if action_kind:
            sql += " AND action_kind = %s"
            params.append(str(action_kind))
        sql += " ORDER BY ts_utc DESC, episode_id DESC LIMIT %s"
        params.append(max(1, int(limit)))
        with self.pool().connection() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return tuple(
            {
                "episode_id": str(row[0]),
                "ts_utc": int(row[1]),
                "actor": str(row[2]),
                "action_kind": str(row[3]),
                "regime": str(row[4]),
                "context_tags": list(row[5] or []),
                "decision_summary": str(row[6]),
                "payload_sha256": str(row[7]),
            }
            for row in rows
        )

    def find_timeline_by_entity_ids(
        self, entity_ids: Sequence[str], *, limit: int = 200
    ) -> tuple[dict[str, Any], ...]:
        """Timeline events referencing **every** given entity (GIN ``@>``)."""
        self._require_dsn()
        wanted = [str(row) for row in entity_ids if str(row)]
        if not wanted:
            return ()
        sql = (
            "SELECT event_id, ts_utc, entity_ids, outcome_kind, net_pnl_micros,"
            " slippage_bps, lesson_summary, payload_sha256"
            " FROM nios_entity_timeline WHERE entity_ids @> %s::jsonb"
            " ORDER BY ts_utc DESC, event_id DESC LIMIT %s"
        )
        with self.pool().connection() as connection:
            rows = connection.execute(sql, (json.dumps(wanted), max(1, int(limit)))).fetchall()
        return tuple(
            {
                "event_id": str(row[0]),
                "ts_utc": int(row[1]),
                "entity_ids": list(row[2] or []),
                "outcome_kind": str(row[3]),
                "net_pnl_micros": int(row[4]),
                "slippage_bps": float(row[5]),
                "lesson_summary": str(row[6]),
                "payload_sha256": str(row[7]),
            }
            for row in rows
        )

    # ------------------------------------------------------- journal dual-write
    def sync_from_journal(self, journal: Any) -> dict[str, Any]:
        """Dual-write the journal's memory surfaces into PostgreSQL.

        The journal stays the local source of truth; this mirrors its ``entity_memory``
        document (timeline, compiled truths, checkpoints) and any episodic episodes
        the WAL store holds.  Returns ``available=False`` when PostgreSQL is not
        configured, so a caller can always run the same code path offline.
        """
        if not self.available:
            return {
                "available": False,
                "reason": "NO_DSN" if not self._dsn else (self._driver_error or "UNAVAILABLE"),
                "written": {},
            }
        loader = getattr(journal, "load", None)
        state = loader() if callable(loader) else {}
        memory = state.get("entity_memory") if isinstance(state, Mapping) else None
        memory = memory if isinstance(memory, Mapping) else {}
        timeline = [row for row in (memory.get("timeline") or ()) if isinstance(row, Mapping)]
        truths = [row for row in (memory.get("truths") or ()) if isinstance(row, Mapping)]
        checkpoints = [
            row for row in (memory.get("checkpoints") or ()) if isinstance(row, Mapping)
        ]
        store = getattr(journal, "store", None)
        episodes: list[dict[str, Any]] = []
        reader = getattr(store, "read_episodes", None)
        if callable(reader):
            try:
                episodes = [dict(row) for row in reader()]
            except Exception:  # noqa: BLE001 - episodic mirroring is best effort
                episodes = []

        written = {"timeline": 0, "truths": 0, "checkpoints": 0, "episodes": 0}
        with self.pool().connection() as connection:
            with connection.transaction():
                for row in timeline:
                    connection.execute(
                        "INSERT INTO nios_entity_timeline"
                        " (event_id, ts_utc, entity_ids, outcome_kind, net_pnl_micros,"
                        "  slippage_bps, lesson_summary, payload_sha256)"
                        " VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (event_id) DO NOTHING",
                        (
                            str(row.get("event_id") or ""),
                            int(row.get("ts_utc") or 0),
                            json.dumps([str(item) for item in (row.get("entity_ids") or ())]),
                            str(row.get("outcome_kind") or ""),
                            int(row.get("net_pnl_micros") or 0),
                            float(row.get("slippage_bps") or 0.0),
                            str(row.get("lesson_summary") or ""),
                            str(row.get("payload_sha256") or ""),
                        ),
                    )
                    written["timeline"] += 1
                for row in truths:
                    connection.execute(
                        "INSERT INTO nios_compiled_entity_truths"
                        " (entity_id, entity_type, sample_count, win_rate_bps,"
                        "  avg_slippage_bps, worst_drawdown_bps, known_failure_modes,"
                        "  caution_regimes, last_compiled_utc, truth_sha256)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)"
                        " ON CONFLICT (entity_id) DO UPDATE SET"
                        "  entity_type = EXCLUDED.entity_type,"
                        "  sample_count = EXCLUDED.sample_count,"
                        "  win_rate_bps = EXCLUDED.win_rate_bps,"
                        "  avg_slippage_bps = EXCLUDED.avg_slippage_bps,"
                        "  worst_drawdown_bps = EXCLUDED.worst_drawdown_bps,"
                        "  known_failure_modes = EXCLUDED.known_failure_modes,"
                        "  caution_regimes = EXCLUDED.caution_regimes,"
                        "  last_compiled_utc = EXCLUDED.last_compiled_utc,"
                        "  truth_sha256 = EXCLUDED.truth_sha256",
                        (
                            str(row.get("entity_id") or ""),
                            str(row.get("entity_type") or "UNKNOWN"),
                            int(row.get("sample_count") or 0),
                            float(row.get("win_rate_bps") or 0.0),
                            float(row.get("avg_slippage_bps") or 0.0),
                            float(row.get("worst_drawdown_bps") or 0.0),
                            json.dumps([str(item) for item in (row.get("known_failure_modes") or ())]),
                            json.dumps([str(item) for item in (row.get("caution_regimes") or ())]),
                            int(row.get("last_compiled_utc") or 0),
                            str(row.get("truth_sha256") or ""),
                        ),
                    )
                    written["truths"] += 1
                for row in checkpoints:
                    connection.execute(
                        "INSERT INTO nios_memory_checkpoints"
                        " (checkpoint_seq, cutoff_ts_utc, event_count_compacted,"
                        "  compiled_truths, checkpoint_sha256)"
                        " VALUES (%s, %s, %s, %s::jsonb, %s)"
                        " ON CONFLICT (checkpoint_seq) DO UPDATE SET"
                        "  cutoff_ts_utc = EXCLUDED.cutoff_ts_utc,"
                        "  event_count_compacted = EXCLUDED.event_count_compacted,"
                        "  compiled_truths = EXCLUDED.compiled_truths,"
                        "  checkpoint_sha256 = EXCLUDED.checkpoint_sha256",
                        (
                            int(row.get("checkpoint_seq") or 0),
                            _ns_to_datetime(row.get("cutoff_ts_utc")),
                            int(row.get("event_count_compacted") or 0),
                            json.dumps(row.get("compiled_truths") or {}),
                            str(row.get("checkpoint_sha256") or ""),
                        ),
                    )
                    written["checkpoints"] += 1
                for row in episodes:
                    connection.execute(
                        "INSERT INTO nios_episodic_memory"
                        " (episode_id, ts_utc, actor, action_kind, regime,"
                        "  context_tags, decision_summary, payload_sha256)"
                        " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)"
                        " ON CONFLICT (episode_id) DO NOTHING",
                        (
                            str(row.get("episode_id") or ""),
                            int(row.get("ts_utc") or 0),
                            str(row.get("actor") or ""),
                            str(row.get("action_kind") or ""),
                            str(row.get("regime") or ""),
                            json.dumps([str(tag) for tag in (row.get("context_tags") or ())]),
                            str(row.get("decision_summary") or ""),
                            str(row.get("payload_sha256") or ""),
                        ),
                    )
                    written["episodes"] += 1
        return {"available": True, "reason": "OK", "written": written}

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        """A JSON-safe readiness summary (safe to call offline)."""
        payload: dict[str, Any] = {
            "available": self.available,
            "dsn": self.redacted_dsn,
            "reason": "OK" if self.available else ("NO_DSN" if not self._dsn else self._driver_error),
            "max_pool_size": self._max_pool_size,
            "timeout_s": self._timeout_s,
            "migrations": [migration.to_dict() for migration in migrations()],
            "applied_this_process": list(self._applied),
        }
        if self.available:
            try:
                payload["migration_status"] = list(self.migration_status())
            except Exception as exc:  # noqa: BLE001 - status never crashes
                payload["migration_status"] = []
                payload["migration_error"] = f"{type(exc).__name__}: {exc}"
        return payload
