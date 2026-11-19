"""NIOS V2.3 -- crash-safe SQLite WAL state store (``PART 1.1``).

Why this module exists
----------------------
The workstation used to persist its journal by rewriting one monolithic JSON
document on **every** write.  That is crash-safe (``os.replace`` is atomic) but it
is O(document) per mutation, it holds the whole book in memory to change one row,
and it gives no transactional read: two sections cannot be updated atomically.

This store is the durable replacement.  A single SQLite database runs in **WAL**
mode so a reader never blocks a writer and a crash replays from the log instead of
losing the last commit:

.. code-block:: sql

    PRAGMA journal_mode = WAL;
    PRAGMA synchronous  = FULL;
    PRAGMA foreign_keys = ON;
    PRAGMA busy_timeout = 5000;

Every mutation is one ``BEGIN IMMEDIATE`` transaction.  ``IMMEDIATE`` takes the
write lock up front, so two processes racing for the same row serialise on
``busy_timeout`` instead of failing late with ``SQLITE_BUSY`` after doing half the
work -- the classic "lost update" in a deferred transaction.

Tables (``PART 1.1`` contract)
------------------------------
``nios_kv_sections``             the journal's sections and carry-through keys
``nios_entity_timeline_local``   append-only post-mortem outcome timeline
``nios_entity_truths_local``     derived per-entity truth cache
``nios_memory_checkpoints_local`` sealed compaction checkpoints
``nios_episodic_memory_local``   operator decision episodes
``nios_working_memory_local``    bounded Tier-1 runtime namespaces
``nios_schema_migrations_local``  applied DDL versions with SHA-256 checksums

The last one is the migration ledger: an application that cannot prove *which*
DDL it runs cannot be reproduced.  Each version records a SHA-256 of its own SQL,
and a stored checksum that no longer matches the code raises instead of silently
running different DDL over live data.

Ring containment: Ring 0 storage plumbing only.  No broker credential, no socket,
no LLM client, no import from ``ring2_brain``/``ring3_cockpit``; the standard
library is the only dependency.  Money never appears here as a float -- amounts
are opaque JSON payloads that the owning module keeps in integer micros.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

__all__ = [
    "WAL_BUSY_TIMEOUT_MS",
    "WAL_PRAGMAS",
    "WAL_SCHEMA_VERSION",
    "WAL_TABLES",
    "LEGACY_JSON_IMPORT_MARKER",
    "RESERVED_DOCUMENT_SECTION",
    "WALStateStore",
    "WALStoreError",
    "default_wal_store",
    "wal_store_path",
]

#: Logical version of the local DDL below.  A change to any statement bumps the
#: numeric migration version **and** this tag.
WAL_SCHEMA_VERSION: str = "v2.3.wal-state-store"

#: The four pragmas ``PART 1.1`` requires, spelled exactly as they are applied.
WAL_PRAGMAS: Mapping[str, str] = {
    "journal_mode": "wal",
    "synchronous": "full",
    "foreign_keys": "on",
    "busy_timeout": "5000",
}

#: Write-lock contention window.  A ``BEGIN IMMEDIATE`` that meets another writer
#: waits up to this long before raising ``SQLITE_BUSY``.
WAL_BUSY_TIMEOUT_MS: int = 5_000

#: The clean-slate truncation order (children before parents; there are no foreign
#: keys between these tables today, so the order is documentation only).
WAL_TABLES: tuple[str, ...] = (
    "nios_kv_sections",
    "nios_entity_timeline_local",
    "nios_entity_truths_local",
    "nios_memory_checkpoints_local",
    "nios_episodic_memory_local",
    "nios_working_memory_local",
    "nios_schema_migrations_local",
)

#: Reserved kv section carrying the document envelope (``saved_at_ns`` and the
#: journal schema tag).  Double underscore so no user key can collide with it.
RESERVED_DOCUMENT_SECTION: str = "__document__"

#: Bookkeeping row recorded in ``nios_schema_migrations_local`` (version ``0``)
#: once a legacy monolithic JSON journal has been imported.  It stops a stale
#: file from being re-imported over newer data.
LEGACY_JSON_IMPORT_MARKER: str = "legacy_json_import"

#: Version reserved for the legacy-import marker; real DDL versions start at 1.
LEGACY_IMPORT_VERSION: int = 0

#: The migration ledger must exist before the ledger can be read, so it is
#: bootstrapped ahead of every migration and then re-created idempotently by 0001.
_MIGRATION_LEDGER_DDL: str = """
    CREATE TABLE IF NOT EXISTS nios_schema_migrations_local (
        version           INTEGER PRIMARY KEY CHECK (version >= 0),
        name              TEXT    NOT NULL,
        checksum_sha256   TEXT    NOT NULL,
        applied_at_utc_ns INTEGER NOT NULL DEFAULT 0 CHECK (applied_at_utc_ns >= 0)
    )
    """

#: The complete local DDL, one statement per entry, applied in order.
_LOCAL_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS nios_kv_sections (
        section        TEXT    PRIMARY KEY,
        payload_json   TEXT    NOT NULL,
        row_count      INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
        payload_sha256 TEXT    NOT NULL,
        saved_at_ns    INTEGER NOT NULL DEFAULT 0 CHECK (saved_at_ns >= 0),
        updated_utc_ns INTEGER NOT NULL DEFAULT 0 CHECK (updated_utc_ns >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nios_entity_timeline_local (
        seq                INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id           TEXT    NOT NULL UNIQUE,
        ts_utc_ns          INTEGER NOT NULL DEFAULT 0 CHECK (ts_utc_ns >= 0),
        outcome_kind       TEXT    NOT NULL DEFAULT '',
        entity_ids_json    TEXT    NOT NULL DEFAULT '[]',
        payload_json       TEXT    NOT NULL,
        payload_sha256     TEXT    NOT NULL,
        recorded_at_utc_ns INTEGER NOT NULL DEFAULT 0 CHECK (recorded_at_utc_ns >= 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_entity_timeline_local_ts
        ON nios_entity_timeline_local (ts_utc_ns DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS nios_entity_truths_local (
        entity_id            TEXT    PRIMARY KEY,
        entity_type          TEXT    NOT NULL DEFAULT '',
        sample_count         INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
        truth_json           TEXT    NOT NULL,
        truth_sha256         TEXT    NOT NULL,
        last_compiled_utc_ns INTEGER NOT NULL DEFAULT 0 CHECK (last_compiled_utc_ns >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nios_memory_checkpoints_local (
        checkpoint_seq        INTEGER PRIMARY KEY CHECK (checkpoint_seq >= 0),
        cutoff_ts_utc_ns      INTEGER NOT NULL DEFAULT 0 CHECK (cutoff_ts_utc_ns >= 0),
        event_count_compacted INTEGER NOT NULL DEFAULT 0 CHECK (event_count_compacted >= 0),
        compiled_truths_json  TEXT    NOT NULL DEFAULT '{}',
        checkpoint_sha256     TEXT    NOT NULL,
        created_at_utc_ns     INTEGER NOT NULL DEFAULT 0 CHECK (created_at_utc_ns >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nios_episodic_memory_local (
        episode_id          TEXT    PRIMARY KEY,
        ts_utc_ns           INTEGER NOT NULL DEFAULT 0 CHECK (ts_utc_ns >= 0),
        actor               TEXT    NOT NULL DEFAULT '',
        action_kind         TEXT    NOT NULL DEFAULT '',
        regime              TEXT    NOT NULL DEFAULT '',
        context_tags_json   TEXT    NOT NULL DEFAULT '[]',
        decision_summary    TEXT    NOT NULL DEFAULT '',
        payload_json        TEXT    NOT NULL DEFAULT '{}',
        payload_sha256      TEXT    NOT NULL,
        recorded_at_utc_ns  INTEGER NOT NULL DEFAULT 0 CHECK (recorded_at_utc_ns >= 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_episodic_memory_local_time
        ON nios_episodic_memory_local (ts_utc_ns DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_episodic_memory_local_kind
        ON nios_episodic_memory_local (action_kind, ts_utc_ns DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS nios_working_memory_local (
        namespace         TEXT    NOT NULL,
        row_key           TEXT    NOT NULL,
        sort_key          INTEGER NOT NULL DEFAULT 0,
        payload_json      TEXT    NOT NULL,
        payload_sha256    TEXT    NOT NULL,
        updated_at_utc_ns INTEGER NOT NULL DEFAULT 0 CHECK (updated_at_utc_ns >= 0),
        PRIMARY KEY (namespace, row_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_working_memory_local_order
        ON nios_working_memory_local (namespace, sort_key, row_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS nios_schema_migrations_local (
        version           INTEGER PRIMARY KEY CHECK (version >= 0),
        name              TEXT    NOT NULL,
        checksum_sha256   TEXT    NOT NULL,
        applied_at_utc_ns INTEGER NOT NULL DEFAULT 0 CHECK (applied_at_utc_ns >= 0)
    )
    """,
)

#: ``(version, name, statements)`` migration chain.  Append-only: never edit a
#: shipped version, add a new tuple.  The checksum of version 1 is over exactly
#: these statements, so a later edit fails loudly on an existing database.
LOCAL_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "0001_wal_state_store", _LOCAL_DDL),
)


class WALStoreError(RuntimeError):
    """Raised when the store cannot honour its durability contract."""


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------


def _canonical_json(payload: Any) -> str:
    """Deterministic JSON text: sorted keys, no whitespace, string fallback."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_payload(payload: Any) -> str:
    return _sha256_text(_canonical_json(payload))


def _utc_now_ns() -> int:
    return time.time_ns()


def _iso_utc_to_ns(value: Any) -> int:
    """Parse an ISO-8601 UTC timestamp to exact integer nanoseconds.

    The episodic section stores ``ts_utc`` as an ISO-8601 string (the PG16 row
    shape), while the projection column is an integer sort key.  Deriving it from
    the ``timedelta`` -- not from ``timestamp() * 1e9`` -- keeps the value exact
    at nanosecond width, where a float multiply would lose the low digits.
    """

    token = str(value or "").strip()
    if not token:
        return 0
    if token.endswith("Z"):
        token = f"{token[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _migration_checksum(name: str, statements: Sequence[str]) -> str:
    """SHA-256 over the normalised migration body (name + one statement/line)."""
    body = name + "\n" + "\n".join(" ".join(statement.split()) for statement in statements)
    return _sha256_text(body)


def wal_store_path(base: str | os.PathLike[str]) -> Path:
    """Derive the WAL database path for a journal document path.

    ``<dir>/nios_workstation_journal.json`` becomes
    ``<dir>/nios_workstation_journal.json.wal.sqlite3``.  Appending instead of
    replacing the suffix means two documents that differ only in their suffix can
    never share one database.
    """
    return Path(str(Path(os.fspath(base)).expanduser()) + ".wal.sqlite3")


def _env_value(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


#: ``ring0_vault/ibor/wal_state_store.py`` -> ``parents[2]`` is the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Default file name of the standalone working-memory database.
DEFAULT_WAL_FILENAME: str = "nios_working_memory.wal.sqlite3"


def _default_wal_db_path() -> Path:
    """Resolve the standalone store location (``NIOS_DATA_DIR`` wins)."""
    explicit = _env_value("NIOS_WORKING_MEMORY_PATH")
    if explicit is not None:
        return Path(explicit).expanduser()
    data_dir = _env_value("NIOS_DATA_DIR")
    if data_dir is not None:
        return Path(data_dir).expanduser() / DEFAULT_WAL_FILENAME
    return _REPO_ROOT / "data" / DEFAULT_WAL_FILENAME


class WALStateStore:
    """A crash-safe, transaction-per-mutation SQLite WAL database.

    The store is **thread-affine**: each thread lazily opens its own connection,
    so two worker threads never share a cursor, while ``BEGIN IMMEDIATE`` plus
    ``busy_timeout`` make cross-thread and cross-process writes serialise rather
    than interleave.  Instances are cheap; several may point at one file.

    Nothing is created under a read: :meth:`read_document` on a path that does not
    exist answers an empty mapping without creating a database file, which is what
    keeps an untouched install genuinely untouched.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = WAL_BUSY_TIMEOUT_MS,
        ensure_schema: bool = True,
    ) -> None:
        self._path = Path(os.fspath(path)).expanduser()
        self._busy_timeout_ms = max(100, int(busy_timeout_ms))
        self._ensure_schema = bool(ensure_schema)
        self._local = threading.local()
        self._lock = threading.RLock()
        self._connections: list[sqlite3.Connection] = []
        self._closed = False
        #: Journal mode SQLite actually negotiated for this file (``wal``).
        self._journal_mode = ""
        self._migrations_applied: tuple[dict[str, Any], ...] = ()

    # ------------------------------------------------------------------ paths
    @property
    def path(self) -> Path:
        """The database file this store owns."""
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    @property
    def journal_mode(self) -> str:
        """``wal`` once a connection has been opened."""
        return self._journal_mode

    # --------------------------------------------------------------- open/close
    def _apply_pragmas(self, connection: sqlite3.Connection) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            row = cursor.fetchone()
            mode = str(row[0]).lower() if row else ""
            if mode and mode != "wal":
                # A second process can hold the file in rollback mode for the
                # instant an exclusive lock is held; one bounded retry is enough.
                cursor.execute("PRAGMA journal_mode=WAL")
                row = cursor.fetchone()
                mode = str(row[0]).lower() if row else mode
            self._journal_mode = mode or "wal"
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        finally:
            cursor.close()

    def connection(self) -> sqlite3.Connection:
        """Return this thread's connection, opening and migrating on first use."""
        if self._closed:
            raise WALStoreError("WAL_STORE_CLOSED")
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            return existing
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self._path),
            timeout=self._busy_timeout_ms / 1000.0,
            isolation_level=None,  # explicit BEGIN/COMMIT only
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        self._apply_pragmas(connection)
        with self._lock:
            self._connections.append(connection)
        self._local.connection = connection
        self._local.depth = 0
        if self._ensure_schema:
            self._migrations_applied = self.apply_migrations(connection)
        return connection

    def close(self) -> None:
        """Close every connection this instance opened (best effort per thread)."""
        with self._lock:
            self._closed = True
            connections = list(self._connections)
            self._connections = []
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:  # pragma: no cover - another thread owns it
                continue
        self._local = threading.local()

    def __enter__(self) -> "WALStateStore":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ----------------------------------------------------------- transactions
    @contextmanager
    def transaction(self, *, mode: str = "IMMEDIATE") -> Iterator[sqlite3.Connection]:
        """One atomic unit of work, re-entrant inside an already-open unit.

        ``BEGIN IMMEDIATE`` takes the reserved (write) lock at open time.  A
        deferred ``BEGIN`` would take it only at the first write, so a
        read-then-write transaction could be rolled back by a peer that committed
        in between.  Re-entrancy keeps a caller's helper from deadlocking against
        its own outer transaction.
        """
        connection = self.connection()
        depth = int(getattr(self._local, "depth", 0) or 0)
        if depth:
            # Already inside a transaction on this thread: join it.
            self._local.depth = depth + 1
            try:
                yield connection
            finally:
                self._local.depth = depth
            return
        connection.execute(f"BEGIN {mode}")
        self._local.depth = 1
        try:
            yield connection
        except BaseException:
            self._local.depth = 0
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - nothing to roll back
                pass
            raise
        else:
            self._local.depth = 0
            connection.execute("COMMIT")

    # ------------------------------------------------------------- migrations
    def apply_migrations(
        self, connection: sqlite3.Connection | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Apply every pending local DDL version and return the applied ledger.

        A version already recorded with a **different** checksum raises: the code
        and the database disagree about what DDL produced this file, and guessing
        is how a schema silently drifts from the one the tests certified.
        ``connection`` is accepted for callers that already hold the thread
        connection; the transaction below reuses it (one connection per thread).
        """
        applied: list[dict[str, Any]] = []
        with self.transaction() as tx:
            tx.execute(_MIGRATION_LEDGER_DDL)
            recorded = {
                int(row["version"]): row
                for row in tx.execute(
                    "SELECT version, name, checksum_sha256 FROM nios_schema_migrations_local"
                ).fetchall()
            }
            for version, name, statements in LOCAL_MIGRATIONS:
                checksum = _migration_checksum(name, statements)
                known = recorded.get(int(version))
                if known is not None:
                    if str(known["checksum_sha256"]) != checksum:
                        raise WALStoreError(
                            f"WAL_MIGRATION_CHECKSUM_MISMATCH:{version}:{name}"
                        )
                    continue
                for statement in statements:
                    tx.execute(statement)
                tx.execute(
                    "INSERT INTO nios_schema_migrations_local"
                    " (version, name, checksum_sha256, applied_at_utc_ns)"
                    " VALUES (?, ?, ?, ?)",
                    (int(version), name, checksum, _utc_now_ns()),
                )
                applied.append(
                    {"version": int(version), "name": name, "checksum_sha256": checksum}
                )
        return tuple(applied)

    def schema_versions(self) -> tuple[dict[str, Any], ...]:
        """Every recorded DDL version, ascending (including the import marker)."""
        rows = self.connection().execute(
            "SELECT version, name, checksum_sha256, applied_at_utc_ns"
            " FROM nios_schema_migrations_local ORDER BY version"
        ).fetchall()
        return tuple(
            {
                "version": int(row["version"]),
                "name": str(row["name"]),
                "checksum_sha256": str(row["checksum_sha256"]),
                "applied_at_utc_ns": int(row["applied_at_utc_ns"]),
            }
            for row in rows
        )

    def has_marker(self, name: str) -> bool:
        """True when a bookkeeping marker has been recorded."""
        row = self.connection().execute(
            "SELECT 1 FROM nios_schema_migrations_local WHERE name = ? LIMIT 1",
            (str(name),),
        ).fetchone()
        return row is not None

    def record_marker(self, name: str, *, version: int = LEGACY_IMPORT_VERSION) -> None:
        """Record an idempotent bookkeeping marker (never a DDL version)."""
        token = str(name)
        checksum = _sha256_text(token)
        with self.transaction() as tx:
            tx.execute(
                "INSERT INTO nios_schema_migrations_local"
                " (version, name, checksum_sha256, applied_at_utc_ns)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(version) DO UPDATE SET"
                " name = excluded.name, checksum_sha256 = excluded.checksum_sha256",
                (int(version), token, checksum, _utc_now_ns()),
            )

    # --------------------------------------------------------------- sections
    def read_sections(self) -> dict[str, Any]:
        """Every kv section payload, decoded, keyed by section name."""
        rows = self.connection().execute(
            "SELECT section, payload_json FROM nios_kv_sections ORDER BY section"
        ).fetchall()
        sections: dict[str, Any] = {}
        for row in rows:
            name = str(row["section"])
            if name == RESERVED_DOCUMENT_SECTION:
                continue
            sections[name] = _decode(row["payload_json"])
        return sections

    def read_section(self, section: str) -> Any | None:
        """One section payload, or ``None`` when absent."""
        row = self.connection().execute(
            "SELECT payload_json FROM nios_kv_sections WHERE section = ?",
            (str(section),),
        ).fetchone()
        return None if row is None else _decode(row["payload_json"])

    def read_document_meta(self) -> dict[str, Any]:
        """The reserved document envelope (``saved_at_ns`` and schema tag)."""
        row = self.connection().execute(
            "SELECT payload_json FROM nios_kv_sections WHERE section = ?",
            (RESERVED_DOCUMENT_SECTION,),
        ).fetchone()
        if row is None:
            return {}
        payload = _decode(row["payload_json"])
        return dict(payload) if isinstance(payload, Mapping) else {}

    def read_document(self) -> dict[str, Any]:
        """The whole journal document: sections plus the reserved envelope.

        The envelope is returned under :data:`RESERVED_DOCUMENT_SECTION` so a
        caller can never mistake it for a user section.
        """
        document = self.read_sections()
        meta = self.read_document_meta()
        if meta:
            document[RESERVED_DOCUMENT_SECTION] = meta
        return document

    def section_count(self) -> int:
        """Number of non-reserved sections currently stored."""
        row = self.connection().execute(
            "SELECT COUNT(*) AS n FROM nios_kv_sections WHERE section <> ?",
            (RESERVED_DOCUMENT_SECTION,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def write_document(
        self,
        document: Mapping[str, Any],
        *,
        saved_at_ns: int = 0,
        schema_version: str = "",
    ) -> dict[str, Any]:
        """Replace the stored document with ``document`` in one transaction.

        Keys missing from ``document`` are **deleted**, not left behind: the
        journal's public contract is a whole-document replace, and a stale section
        surviving a save would resurrect rows the caller already dropped.
        """
        rows = {
            str(key): value
            for key, value in document.items()
            if str(key) != RESERVED_DOCUMENT_SECTION
        }
        stamp = int(saved_at_ns or _utc_now_ns())
        with self.transaction() as tx:
            self._write_document_tx(
                tx, rows, saved_at_ns=stamp, schema_version=schema_version
            )
        return rows

    def _write_document_tx(
        self,
        tx: sqlite3.Connection,
        rows: Mapping[str, Any],
        *,
        saved_at_ns: int,
        schema_version: str,
    ) -> None:
        existing = {
            str(row["section"])
            for row in tx.execute("SELECT section FROM nios_kv_sections").fetchall()
        }
        stamp = _utc_now_ns()
        for section, payload in rows.items():
            body = _canonical_json(payload)
            row_count = len(payload) if isinstance(payload, (list, tuple, Mapping)) else 0
            tx.execute(
                "INSERT INTO nios_kv_sections"
                " (section, payload_json, row_count, payload_sha256, saved_at_ns, updated_utc_ns)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(section) DO UPDATE SET"
                " payload_json = excluded.payload_json,"
                " row_count = excluded.row_count,"
                " payload_sha256 = excluded.payload_sha256,"
                " saved_at_ns = excluded.saved_at_ns,"
                " updated_utc_ns = excluded.updated_utc_ns",
                (section, body, int(row_count), _sha256_text(body), int(saved_at_ns), stamp),
            )
        for stale in existing - set(rows) - {RESERVED_DOCUMENT_SECTION}:
            tx.execute("DELETE FROM nios_kv_sections WHERE section = ?", (stale,))
        envelope = _canonical_json(
            {"saved_at_ns": int(saved_at_ns), "schema_version": str(schema_version)}
        )
        tx.execute(
            "INSERT INTO nios_kv_sections"
            " (section, payload_json, row_count, payload_sha256, saved_at_ns, updated_utc_ns)"
            " VALUES (?, ?, 0, ?, ?, ?)"
            " ON CONFLICT(section) DO UPDATE SET"
            " payload_json = excluded.payload_json,"
            " payload_sha256 = excluded.payload_sha256,"
            " saved_at_ns = excluded.saved_at_ns,"
            " updated_utc_ns = excluded.updated_utc_ns",
            (RESERVED_DOCUMENT_SECTION, envelope, _sha256_text(envelope), int(saved_at_ns), stamp),
        )
        self._project_entity_memory_tx(tx, rows.get("entity_memory"), now_ns=stamp)
        self._project_episodic_memory_tx(tx, rows.get("episodic_memory"), now_ns=stamp)

    # ------------------------------------------------------ entity projection
    def _project_entity_memory_tx(
        self, tx: sqlite3.Connection, document: Any, *, now_ns: int
    ) -> None:
        """Mirror the entity-memory document into the structured local tables.

        The kv section stays authoritative (it is what the journal hands back
        byte-for-byte); these tables are the queryable projection that a restart
        scan or a Suite-39 assertion reads without decoding a blob.
        """
        if not isinstance(document, Mapping):
            return
        timeline = document.get("timeline")
        truths = document.get("truths")
        checkpoints = document.get("checkpoints")
        tx.execute("DELETE FROM nios_entity_timeline_local")
        tx.execute("DELETE FROM nios_entity_truths_local")
        if isinstance(timeline, (list, tuple)):
            for row in timeline:
                if not isinstance(row, Mapping):
                    continue
                event_id = str(row.get("event_id") or "").strip()
                if not event_id:
                    continue
                body = _canonical_json(dict(row))
                entity_ids = row.get("entity_ids") or []
                ids_json = _canonical_json(
                    list(entity_ids) if isinstance(entity_ids, (list, tuple)) else []
                )
                tx.execute(
                    "INSERT OR REPLACE INTO nios_entity_timeline_local"
                    " (event_id, ts_utc_ns, outcome_kind, entity_ids_json,"
                    "  payload_json, payload_sha256, recorded_at_utc_ns)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        int(row.get("ts_utc") or 0),
                        str(row.get("outcome_kind") or ""),
                        ids_json,
                        body,
                        _sha256_text(body),
                        int(now_ns),
                    ),
                )
        if isinstance(truths, (list, tuple)):
            for row in truths:
                if not isinstance(row, Mapping):
                    continue
                entity_id = str(row.get("entity_id") or "").strip()
                if not entity_id:
                    continue
                body = _canonical_json(dict(row))
                tx.execute(
                    "INSERT OR REPLACE INTO nios_entity_truths_local"
                    " (entity_id, entity_type, sample_count, truth_json,"
                    "  truth_sha256, last_compiled_utc_ns)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        entity_id,
                        str(row.get("entity_type") or ""),
                        int(row.get("sample_count") or 0),
                        body,
                        str(row.get("truth_sha256") or _sha256_text(body)),
                        int(row.get("last_compiled_utc") or 0),
                    ),
                )
        if isinstance(checkpoints, (list, tuple)):
            self._replace_checkpoints_tx(tx, checkpoints, now_ns=now_ns)

    def _project_episodic_memory_tx(
        self, tx: sqlite3.Connection, document: Any, *, now_ns: int
    ) -> None:
        """Mirror the episodic-memory section into ``nios_episodic_memory_local``.

        The kv section stays authoritative -- it is what the journal hands back
        byte-for-byte and what ``EpisodicMemoryStore`` restores -- while this table
        is the queryable projection a restart scan or a Suite-39 assertion reads
        without decoding a blob.  Symmetric with the entity projection: a document
        that carries no ``episodic_memory`` section says nothing about episodes, so
        rows written through :meth:`append_episode` are left untouched.  A real
        clean slate is :meth:`truncate_all`'s job, not this projection's.
        """
        if not isinstance(document, Mapping):
            return
        episodes = document.get("episodes")
        if not isinstance(episodes, (list, tuple)):
            return
        tx.execute("DELETE FROM nios_episodic_memory_local")
        for row in episodes:
            if not isinstance(row, Mapping):
                continue
            episode_id = str(row.get("episode_id") or "").strip()
            if not episode_id:
                continue
            body = _canonical_json(dict(row))
            tags = row.get("context_tags")
            tags_body = _canonical_json(
                [str(tag) for tag in tags]
                if isinstance(tags, (list, tuple))
                else dict(tags or {})
            )
            tx.execute(
                "INSERT OR REPLACE INTO nios_episodic_memory_local"
                " (episode_id, ts_utc_ns, actor, action_kind, regime,"
                "  context_tags_json, decision_summary, payload_json,"
                "  payload_sha256, recorded_at_utc_ns)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    _iso_utc_to_ns(row.get("ts_utc")),
                    str(row.get("actor") or ""),
                    str(row.get("action_kind") or ""),
                    str(row.get("regime") or ""),
                    tags_body,
                    str(row.get("decision_summary") or ""),
                    body,
                    str(row.get("payload_sha256") or _sha256_text(body)),
                    int(now_ns),
                ),
            )

    def _replace_checkpoints_tx(
        self, tx: sqlite3.Connection, checkpoints: Sequence[Any], *, now_ns: int
    ) -> None:
        keep: list[tuple[int, ...]] = []
        for row in checkpoints:
            if not isinstance(row, Mapping):
                continue
            try:
                sequence = int(row.get("checkpoint_seq") or 0)
            except (TypeError, ValueError):
                continue
            keep.append(
                (
                    sequence,
                    int(row.get("cutoff_ts_utc") or 0),
                    int(row.get("event_count_compacted") or 0),
                    _canonical_json(row.get("compiled_truths") or {}),
                    str(row.get("checkpoint_sha256") or ""),
                    int(row.get("created_at_utc") or now_ns),
                )
            )
        tx.execute("DELETE FROM nios_memory_checkpoints_local")
        for row in keep:
            tx.execute(
                "INSERT OR REPLACE INTO nios_memory_checkpoints_local"
                " (checkpoint_seq, cutoff_ts_utc_ns, event_count_compacted,"
                "  compiled_truths_json, checkpoint_sha256, created_at_utc_ns)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )

    def read_entity_timeline(self, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        """The projected post-mortem timeline, oldest first."""
        sql = (
            "SELECT payload_json FROM nios_entity_timeline_local"
            " ORDER BY ts_utc_ns ASC, seq ASC"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql = (
                "SELECT payload_json FROM nios_entity_timeline_local"
                " ORDER BY ts_utc_ns DESC, seq DESC LIMIT ?"
            )
            params = (max(1, int(limit)),)
        rows = self.connection().execute(sql, params).fetchall()
        decoded = [row for row in (_decode(row["payload_json"]) for row in rows)]
        if limit is not None:
            decoded.reverse()
        return tuple(row for row in decoded if isinstance(row, Mapping))

    def read_entity_truths(self) -> tuple[dict[str, Any], ...]:
        """The projected per-entity truth cache, ordered by ``entity_id``."""
        rows = self.connection().execute(
            "SELECT truth_json FROM nios_entity_truths_local ORDER BY entity_id"
        ).fetchall()
        return tuple(
            row for row in (_decode(row["truth_json"]) for row in rows) if isinstance(row, Mapping)
        )

    def read_memory_checkpoints(self, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        """Sealed compaction checkpoints, ascending by ``checkpoint_seq``."""
        sql = (
            "SELECT checkpoint_seq, cutoff_ts_utc_ns, event_count_compacted,"
            " compiled_truths_json, checkpoint_sha256, created_at_utc_ns"
            " FROM nios_memory_checkpoints_local ORDER BY checkpoint_seq ASC"
        )
        rows = self.connection().execute(sql).fetchall()
        payload = [
            {
                "checkpoint_seq": int(row["checkpoint_seq"]),
                "cutoff_ts_utc": int(row["cutoff_ts_utc_ns"]),
                "event_count_compacted": int(row["event_count_compacted"]),
                "compiled_truths": _decode(row["compiled_truths_json"]) or {},
                "checkpoint_sha256": str(row["checkpoint_sha256"]),
                "created_at_utc": int(row["created_at_utc_ns"]),
            }
            for row in rows
        ]
        if limit is not None:
            payload = payload[-max(1, int(limit)) :]
        return tuple(payload)

    def record_memory_checkpoint(self, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        """Seal one checkpoint row (idempotent on ``checkpoint_seq``)."""
        sequence = int(checkpoint.get("checkpoint_seq") or 0)
        truths = checkpoint.get("compiled_truths") or {}
        body = _canonical_json(truths)
        checksum = str(checkpoint.get("checkpoint_sha256") or _sha256_text(body))
        with self.transaction() as tx:
            tx.execute(
                "INSERT OR REPLACE INTO nios_memory_checkpoints_local"
                " (checkpoint_seq, cutoff_ts_utc_ns, event_count_compacted,"
                "  compiled_truths_json, checkpoint_sha256, created_at_utc_ns)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sequence,
                    int(checkpoint.get("cutoff_ts_utc") or 0),
                    int(checkpoint.get("event_count_compacted") or 0),
                    body,
                    checksum,
                    int(checkpoint.get("created_at_utc") or _utc_now_ns()),
                ),
            )
        return {
            "checkpoint_seq": sequence,
            "checkpoint_sha256": checksum,
            "compiled_truths": _decode(body) or {},
        }

    # ---------------------------------------------------------- episodic memory
    def append_episode(self, episode: Mapping[str, Any], *, now_ns: int | None = None) -> dict[str, Any]:
        """Insert (or replace) one operator-decision episode."""
        record = dict(episode)
        episode_id = str(record.get("episode_id") or "").strip()
        if not episode_id:
            raise ValueError("EPISODE_ID_REQUIRED")
        tags = record.get("context_tags") or []
        if not isinstance(tags, (list, tuple)):
            raise ValueError("EPISODE_CONTEXT_TAGS_NOT_SEQUENCE")
        body = _canonical_json(record)
        stamp = int(now_ns if now_ns is not None else _utc_now_ns())
        checksum = str(record.get("payload_sha256") or _sha256_text(body))
        with self.transaction() as tx:
            tx.execute(
                "INSERT OR REPLACE INTO nios_episodic_memory_local"
                " (episode_id, ts_utc_ns, actor, action_kind, regime,"
                "  context_tags_json, decision_summary, payload_json,"
                "  payload_sha256, recorded_at_utc_ns)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    int(record.get("ts_utc") or stamp),
                    str(record.get("actor") or ""),
                    str(record.get("action_kind") or ""),
                    str(record.get("regime") or ""),
                    _canonical_json([str(tag) for tag in tags]),
                    str(record.get("decision_summary") or ""),
                    body,
                    checksum,
                    stamp,
                ),
            )
        return record

    def read_episodes(
        self,
        *,
        action_kind: str | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Operator episodes, newest last, optionally filtered by ``action_kind``."""
        sql = "SELECT payload_json FROM nios_episodic_memory_local"
        params: list[Any] = []
        if action_kind:
            sql += " WHERE action_kind = ?"
            params.append(str(action_kind))
        sql += " ORDER BY ts_utc_ns ASC, episode_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self.connection().execute(sql, tuple(params)).fetchall()
        return tuple(
            row for row in (_decode(row["payload_json"]) for row in rows) if isinstance(row, Mapping)
        )

    # ---------------------------------------------------------- working memory
    def replace_namespace(
        self,
        namespace: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        key_field: str = "row_key",
        sort_key_field: str = "sort_key",
        max_rows: int | None = None,
        now_ns: int | None = None,
    ) -> int:
        """Replace every row of one bounded namespace and return the count kept.

        ``max_rows`` keeps the **newest** rows by ``(sort_key, key)``: a runtime
        namespace is a rolling window, so trimming must drop the oldest tail, not
        the row that just arrived.
        """
        token = str(namespace)
        stamp = int(now_ns if now_ns is not None else _utc_now_ns())
        materialised = self._materialise_rows(rows, key_field=key_field, sort_key_field=sort_key_field)
        materialised.sort(key=lambda item: (item[0], item[1]))
        if max_rows is not None:
            materialised = materialised[-max(1, int(max_rows)) :]
        with self.transaction() as tx:
            tx.execute("DELETE FROM nios_working_memory_local WHERE namespace = ?", (token,))
            for sort_key, key, body, checksum in materialised:
                tx.execute(
                    "INSERT OR REPLACE INTO nios_working_memory_local"
                    " (namespace, row_key, sort_key, payload_json, payload_sha256, updated_at_utc_ns)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (token, key, sort_key, body, checksum, stamp),
                )
        return len(materialised)

    def read_namespace(
        self,
        namespace: str,
        *,
        limit: int | None = None,
        prefix: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Rows of one working-memory namespace, oldest first.

        ``prefix`` filters on ``row_key``: the Tier-1 namespaces are keyed
        ``series:...`` and ``session::topic::turn``, so a prefix is how one series
        or thread is read without scanning the whole namespace.
        """
        sql = (
            "SELECT payload_json FROM nios_working_memory_local"
            " WHERE namespace = ?"
        )
        params: list[Any] = [str(namespace)]
        if prefix is not None:
            sql += " AND row_key LIKE ?"
            params.append(f"{str(prefix)}%")
        sql += " ORDER BY sort_key ASC, row_key ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self.connection().execute(sql, tuple(params)).fetchall()
        return tuple(
            row for row in (_decode(row["payload_json"]) for row in rows) if isinstance(row, Mapping)
        )

    def upsert_namespace(
        self,
        namespace: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        key_field: str = "row_key",
        sort_key_field: str = "sort_key",
        now_ns: int | None = None,
    ) -> int:
        """Insert-or-replace rows in one namespace without touching its siblings.

        A bar or a retry entry is one row in a rolling window, so writing it must
        not cost a rewrite of the whole window.
        """
        token = str(namespace)
        stamp = int(now_ns if now_ns is not None else _utc_now_ns())
        materialised = self._materialise_rows(rows, key_field=key_field, sort_key_field=sort_key_field)
        with self.transaction() as tx:
            for sort_key, key, body, checksum in materialised:
                tx.execute(
                    "INSERT OR REPLACE INTO nios_working_memory_local"
                    " (namespace, row_key, sort_key, payload_json, payload_sha256, updated_at_utc_ns)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (token, key, sort_key, body, checksum, stamp),
                )
        return len(materialised)

    def replace_prefix(
        self,
        namespace: str,
        prefix: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        key_field: str = "row_key",
        sort_key_field: str = "sort_key",
        max_rows: int | None = None,
        now_ns: int | None = None,
    ) -> int:
        """Atomically replace every row under ``prefix`` (one chat thread, say)."""
        token = str(namespace)
        head = str(prefix)
        stamp = int(now_ns if now_ns is not None else _utc_now_ns())
        materialised = self._materialise_rows(rows, key_field=key_field, sort_key_field=sort_key_field)
        materialised.sort(key=lambda item: (item[0], item[1]))
        if max_rows is not None:
            materialised = materialised[-max(1, int(max_rows)) :]
        with self.transaction() as tx:
            tx.execute(
                "DELETE FROM nios_working_memory_local WHERE namespace = ? AND row_key LIKE ?",
                (token, f"{head}%"),
            )
            for sort_key, key, body, checksum in materialised:
                tx.execute(
                    "INSERT OR REPLACE INTO nios_working_memory_local"
                    " (namespace, row_key, sort_key, payload_json, payload_sha256, updated_at_utc_ns)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (token, key, sort_key, body, checksum, stamp),
                )
        return len(materialised)

    @staticmethod
    def _materialise_rows(
        rows: Sequence[Mapping[str, Any]],
        *,
        key_field: str,
        sort_key_field: str,
    ) -> list[tuple[int, str, str, str]]:
        materialised: list[tuple[int, str, str, str]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise TypeError(f"WORKING_ROW_NOT_MAPPING:{type(row).__name__}")
            payload = dict(row)
            key = str(payload.get(key_field, "")).strip()
            if not key:
                raise ValueError(f"WORKING_ROW_KEY_REQUIRED:{key_field}")
            try:
                sort_key = int(payload.get(sort_key_field) or 0)
            except (TypeError, ValueError):
                raise ValueError(f"WORKING_ROW_SORT_KEY_INVALID:{key}") from None
            body = _canonical_json(payload)
            materialised.append((sort_key, key, body, _sha256_text(body)))
        return materialised

    def delete_rows(self, namespace: str, keys: Sequence[str]) -> int:
        """Delete specific ``row_key`` values from one namespace."""
        tokens = [str(key) for key in keys]
        if not tokens:
            return 0
        removed = 0
        with self.transaction() as tx:
            for key in tokens:
                cursor = tx.execute(
                    "DELETE FROM nios_working_memory_local WHERE namespace = ? AND row_key = ?",
                    (str(namespace), key),
                )
                removed += int(cursor.rowcount or 0)
        return removed

    def delete_prefix(self, namespace: str, prefix: str) -> int:
        """Delete every row of one namespace whose ``row_key`` starts with ``prefix``."""
        with self.transaction() as tx:
            cursor = tx.execute(
                "DELETE FROM nios_working_memory_local WHERE namespace = ? AND row_key LIKE ?",
                (str(namespace), f"{str(prefix)}%"),
            )
            return int(cursor.rowcount or 0)

    def count_namespace(self, namespace: str, *, prefix: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM nios_working_memory_local WHERE namespace = ?"
        params: list[Any] = [str(namespace)]
        if prefix is not None:
            sql += " AND row_key LIKE ?"
            params.append(f"{str(prefix)}%")
        row = self.connection().execute(sql, tuple(params)).fetchone()
        return int(row["n"]) if row is not None else 0

    def delete_namespace(self, namespace: str) -> int:
        """Drop one namespace; returns the number of rows removed."""
        with self.transaction() as tx:
            cursor = tx.execute(
                "DELETE FROM nios_working_memory_local WHERE namespace = ?", (str(namespace),)
            )
            return int(cursor.rowcount or 0)

    # --------------------------------------------------------------- lifecycle
    def table_counts(self) -> dict[str, int]:
        """Row count per WAL table (used by status surfaces and reset receipts)."""
        counts: dict[str, int] = {}
        connection = self.connection()
        for table in WAL_TABLES:
            row = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"]) if row is not None else 0
        return counts

    def truncate_all(self) -> dict[str, int]:
        """Delete every row of every table, atomically, and return what went.

        The schema survives: a clean-slate reset must zero the *data*, not force
        a migration on the next open.  ``DELETE`` (not ``DROP``) also keeps the
        ``AUTOINCREMENT`` sequence table honest about what happened.
        """
        deleted: dict[str, int] = {}
        with self.transaction() as tx:
            for table in WAL_TABLES:
                cursor = tx.execute(f"DELETE FROM {table}")
                deleted[table] = int(cursor.rowcount or 0)
            if self._has_sequence_table(tx):
                # A clean slate must also drop the AUTOINCREMENT high-water mark,
                # or a fresh install would restart event numbering where the old
                # one stopped.
                tx.execute(
                    "DELETE FROM sqlite_sequence WHERE name = 'nios_entity_timeline_local'"
                )
        return deleted

    def _has_sequence_table(self, tx: sqlite3.Connection) -> bool:
        row = tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        return row is not None

    def wal_checkpoint(self, mode: str = "TRUNCATE") -> dict[str, int]:
        """Run ``PRAGMA wal_checkpoint``; ``TRUNCATE`` empties the ``-wal`` file.

        Must run outside a transaction -- SQLite refuses a checkpoint inside one,
        and a reset that leaves a full WAL behind has not really returned to a
        clean slate.
        """
        token = str(mode or "TRUNCATE").strip().upper()
        if token not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            raise ValueError(f"UNKNOWN_CHECKPOINT_MODE:{mode!r}")
        connection = self.connection()
        row = connection.execute(f"PRAGMA wal_checkpoint({token})").fetchone()
        if row is None:  # pragma: no cover - pragma always answers
            return {"busy": 0, "log_frames": 0, "checkpointed_frames": 0, "mode": token}
        return {
            "busy": int(row[0] or 0),
            "log_frames": int(row[1] or 0),
            "checkpointed_frames": int(row[2] or 0),
            "mode": token,
        }

    def integrity_check(self) -> str:
        """``PRAGMA integrity_check`` verdict (``ok`` on a healthy file)."""
        row = self.connection().execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row is not None else "unknown"

    # ---------------------------------------------------------------- backup
    def backup_document(self) -> dict[str, Any]:
        """A deterministic, byte-stable JSON backup of the whole database.

        Volatile fields (wall clock, file paths) are deliberately excluded, so two
        exports of identical data are byte-identical.  The receipt returned by
        :meth:`export_json` carries the timestamp instead.
        """
        self.wal_checkpoint("FULL")
        return {
            "schema_version": WAL_SCHEMA_VERSION,
            "storage": "sqlite-wal",
            "tables": self.table_counts(),
            "document": self.read_document(),
            "entity_timeline": list(self.read_entity_timeline()),
            "entity_truths": list(self.read_entity_truths()),
            "memory_checkpoints": list(self.read_memory_checkpoints()),
            "episodic_memory": list(self.read_episodes()),
            "working_memory": self._working_memory_dump(),
            "migrations": list(self.schema_versions()),
        }

    def _working_memory_dump(self) -> dict[str, list[dict[str, Any]]]:
        namespaces = [
            str(row["namespace"])
            for row in self.connection().execute(
                "SELECT DISTINCT namespace FROM nios_working_memory_local ORDER BY namespace"
            ).fetchall()
        ]
        return {name: list(self.read_namespace(name)) for name in namespaces}

    def export_json(self, target: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Write :meth:`backup_document` as canonical JSON; return a receipt."""
        document = self.backup_document()
        path = (
            Path(os.fspath(target)).expanduser()
            if target is not None
            else self._path.with_name(self._path.name + ".backup.json")
        )
        body = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, default=str)
        _atomic_write_text(path, body)
        return {
            "path": str(path),
            "sha256": _sha256_text(body),
            "bytes": len(body.encode("utf-8")),
            "tables": dict(document["tables"]),
            "storage": "sqlite-wal",
            "deterministic": True,
        }

    # ----------------------------------------------------------------- status
    def stats(self) -> dict[str, Any]:
        """Durability and occupancy facts for a status surface or a test."""
        return {
            "path": str(self._path),
            "exists": self.exists,
            "journal_mode": self.journal_mode or "wal",
            "synchronous": "FULL",
            "foreign_keys": True,
            "busy_timeout_ms": self._busy_timeout_ms,
            "tables": self.table_counts(),
            "sections": self.section_count(),
            "migrations": [row["version"] for row in self.schema_versions()],
            "legacy_json_imported": self.has_marker(LEGACY_JSON_IMPORT_MARKER),
            "integrity": self.integrity_check(),
        }


def _decode(raw: Any) -> Any:
    """Tolerant JSON decode: a corrupt row is ``None``, never an exception."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def _atomic_write_text(path: Path, body: str) -> None:
    """Write ``body`` via temp file + fsync + ``os.replace`` (same-filesystem)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


_DEFAULT_LOCK = threading.RLock()
_DEFAULT_STORES: dict[str, WALStateStore] = {}


def default_wal_store() -> WALStateStore:
    """Process-wide standalone store, created on first use.

    The cached instance follows the currently resolved path, so a test that
    re-points ``NIOS_DATA_DIR`` (or ``NIOS_WORKING_MEMORY_PATH``) gets its own
    database instead of a stale handle on somebody else's file.
    """
    target = _default_wal_db_path()
    key = str(target)
    with _DEFAULT_LOCK:
        store = _DEFAULT_STORES.get(key)
        if store is None or store._closed:  # noqa: SLF001 - documented singleton guard
            store = WALStateStore(target)
            _DEFAULT_STORES[key] = store
        return store
