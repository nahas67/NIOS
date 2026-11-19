"""NIOS Core -- crash-safe atomic state journal (Clean-Slate compatible).

Why this module exists
----------------------
The workstation persists three user-visible collections -- the logbook, the
backtests and the agent task queue -- and the Clean-Slate guarantee says a fresh
install must report **zero** of each.  A plain ``open(path, "w")`` cannot provide
that guarantee: a crash between truncate and write leaves a half-written document,
and the next boot then either raises or, worse, reports a partially restored
workspace as a live one.

Since V2.3 the document is stored in a **SQLite WAL database**
(:mod:`ring0_vault.ibor.wal_state_store`): every mutation is one
``BEGIN IMMEDIATE`` transaction with ``synchronous=FULL``, so a crash replays the
log to the last committed state and a concurrent reader never sees a torn one.
The old monolithic JSON file is imported **once**, transparently, the first time a
legacy install is opened, and is never rewritten afterwards.  A JSON backup can be
produced on demand with :meth:`AtomicStateJournal.export_json`.

Until V2.2 this journal was one JSON document that was only ever replaced
atomically: write a sibling temp file, ``flush``, ``os.fsync`` and ``os.replace``.
That contract -- a reader sees either the previous complete document or the new
complete one -- is what the public API below still promises, and the WAL store
keeps it.  When the database is missing, unreadable, corrupt or not an object,
:meth:`AtomicStateJournal.load` answers the canonical :data:`CLEAN_SLATE` shape,
which makes "never written" and "explicitly reset" indistinguishable, and both
truthful.

Governing architecture
----------------------
* ``sec_009`` -- the storage tier is append-only and bitemporal; this journal is
  the workstation-tier mirror of that rule.  Entries are only ever appended,
  and the single wholesale rewrite (``replace_section``) is explicit.
* ``sec_012`` -- ``AtMostOnceExecution`` / ``EventualIBORConvergence``: one
  committed transaction is the one and only commit point, so a retry after a
  crash can never apply half a mutation.
* ``sec_018`` Law 5 -- the evolution loop may reset a silo, never a peer's;
  :meth:`AtomicStateJournal.reset` is scoped to this single document.

Ring containment: this is Ring 0 storage plumbing.  It holds no broker
credential, opens no socket, imports nothing from ``ring3_cockpit`` or
``ring2_brain``, and depends on the standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ring0_vault.ibor.wal_state_store import (
    LEGACY_JSON_IMPORT_MARKER,
    RESERVED_DOCUMENT_SECTION,
    WALStateStore,
    wal_store_path,
)

__all__ = [
    "SCHEMA_VERSION",
    "JOURNAL_FILENAME",
    "SECTIONS",
    "CLEAN_SLATE",
    "journal_path",
    "wal_database_path",
    "default_journal",
    "AtomicStateJournal",
]

#: Schema tag written into every document.  A future format change bumps this
#: and the reader stays tolerant of anything it does not recognise.
SCHEMA_VERSION = "v1.8.state-journal"

#: Default file name used both under ``NIOS_DATA_DIR`` and under ``<repo>/data``.
JOURNAL_FILENAME = "nios_workstation_journal.json"

#: The only collections the journal owns.  Anything else is either a derived
#: field (``counts``, ``saved_at_ns``, ``clean_slate_active``,
#: ``schema_version``) or an unknown top-level key that is preserved untouched.
SECTIONS: tuple[str, ...] = ("logbook", "backtests", "agent_tasks")

#: Fields this module computes itself; a caller may not pin them.
_DERIVED_KEYS = frozenset({"schema_version", "saved_at_ns", "counts", "clean_slate_active"})

#: ``ring0_vault/ibor/state_journal.py`` -> ``parents[2]`` is the repository root
#: (``parents[0]`` is ``ibor``, ``parents[1]`` is ``ring0_vault``).  The brief for
#: this lane said ``parents[3]``, which indexes one level *above* the repository
#: and would place the default journal on the Desktop; the intent -- repo root --
#: is what is implemented here.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _clean_slate() -> dict[str, Any]:
    """Return a brand-new Clean-Slate document (never a shared reference)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "saved_at_ns": 0,
        "logbook": [],
        "backtests": [],
        "agent_tasks": [],
        "counts": {section: 0 for section in SECTIONS},
        "clean_slate_active": True,
    }


#: Canonical zeroed state.  ``load()`` always hands back a fresh copy of this
#: shape, never this object itself, so a caller cannot poison later reads.
CLEAN_SLATE: dict[str, Any] = _clean_slate()


def _env_value(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _fsync_dir(path: Path) -> None:
    """Best-effort durability of a directory entry change (rename/unlink).

    ``os.replace``/``os.unlink`` are atomic for concurrent readers, but making
    the change survive a power loss additionally needs the parent directory
    flushed.  Some platforms (Windows) refuse to fsync a directory handle, so
    this is deliberately best-effort and never breaks the operation.
    """
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def journal_path() -> Path:
    """Resolve the journal location, in precedence order.

    1. ``NIOS_JOURNAL_PATH`` -- an explicit file, honoured verbatim.
    2. ``NIOS_DATA_DIR`` + :data:`JOURNAL_FILENAME`.
    3. ``<repo root>/data/`` + :data:`JOURNAL_FILENAME`.

    Resolution is pure: no directory is created and no file is touched here.
    """
    explicit = _env_value("NIOS_JOURNAL_PATH")
    if explicit is not None:
        return Path(explicit).expanduser()
    data_dir = _env_value("NIOS_DATA_DIR")
    if data_dir is not None:
        return Path(data_dir).expanduser() / JOURNAL_FILENAME
    return _REPO_ROOT / "data" / JOURNAL_FILENAME


def wal_database_path(path: str | os.PathLike[str] | None = None) -> Path:
    """The SQLite WAL database backing ``path`` (default: the resolved journal).

    ``path`` is the **document** path the public API has always named; the
    database is derived from it, so two journals pointed at one document share
    one database and therefore one truth.  Resolution is pure.
    """
    target = journal_path() if path is None else Path(os.fspath(path)).expanduser()
    return wal_store_path(target)


def _normalise(state: Mapping[str, Any], *, stamp: bool) -> dict[str, Any]:
    """Rebuild a canonical document from ``state``.

    ``counts`` is always derived from the real list lengths, so a stored count
    can never disagree with the rows it claims to count.  ``clean_slate_active``
    is likewise derived: a document holding any row can never claim to be a
    clean slate, and one holding none always can.  Derived fields supplied by
    the caller are ignored; unknown top-level keys are carried through.

    With ``stamp=True`` (a write) ``saved_at_ns`` becomes the current time; with
    ``stamp=False`` (a read) the stored value is kept as-is.
    """
    rows: dict[str, list[Any]] = {}
    for section in SECTIONS:
        value = state.get(section)
        rows[section] = list(value) if isinstance(value, (list, tuple)) else []

    if stamp:
        saved_at_ns = time.time_ns()
    else:
        stored = state.get("saved_at_ns")
        saved_at_ns = stored if isinstance(stored, int) and not isinstance(stored, bool) else 0

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "saved_at_ns": saved_at_ns,
        **rows,
        "counts": {section: len(rows[section]) for section in SECTIONS},
        "clean_slate_active": not any(rows.values()),
    }
    for key, value in state.items():
        if key in _DERIVED_KEYS or key in SECTIONS:
            continue
        payload[key] = value
    return payload


class AtomicStateJournal:
    """A whole-document state journal backed by a crash-safe SQLite WAL store.

    The class keeps **no** in-memory copy of the state: every call reads or
    writes the database under one re-entrant lock.  Two instances pointed at the
    same document therefore cannot diverge, and a caller that mutates a returned
    dict is mutating a snapshot, not the journal.

    ``path`` may be a ``str``, an ``os.PathLike`` or ``None``; ``None`` defers to
    :func:`journal_path`.  The path names the *document* -- exactly what it named
    before V2.3 -- and the SQLite database is derived from it by
    :func:`wal_database_path`.  That is what lets
    ``AtomicStateJournal(journal.path)`` keep resolving to the same truth.

    Parent directories are created on the first **write** only -- a read never
    creates anything, which is what lets an untouched install stay genuinely
    untouched.  A legacy monolithic JSON document found at ``path`` is imported
    once, transparently, and is never rewritten afterwards.

    ``store`` may inject a pre-built :class:`WALStateStore` (used by tests and by
    an operator migrating a data root); it is normally built lazily.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        store: WALStateStore | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._path = journal_path() if path is None else Path(os.fspath(path)).expanduser()
        self._store: WALStateStore | None = store
        self._imported: int | None = None

    # ------------------------------------------------------------------ paths
    @property
    def path(self) -> Path:
        """The document this instance names (and where a legacy file would live)."""
        return self._path

    @property
    def database_path(self) -> Path:
        """The SQLite WAL database that actually holds the state."""
        return self._store.path if self._store is not None else wal_database_path(self._path)

    @property
    def store(self) -> WALStateStore:
        """The underlying store, created (and migrated) on first access."""
        return self._ensure_store()

    # ------------------------------------------------------------ store plumbing
    def _ensure_store(self) -> WALStateStore:
        """Return the store, creating/migrating it and importing a legacy file.

        Must be called with ``self._lock`` held.  Import happens **once**: when
        the database has no sections and no import marker yet, and a legacy JSON
        document exists at :attr:`path`.
        """
        store = self._store
        if store is None:
            store = WALStateStore(wal_database_path(self._path))
            self._store = store
        if not store.exists:
            store.connection()  # creates the file and applies the local DDL
        if self._imported is None:
            self._imported = self._import_legacy_document(store)
        return store

    def _store_for_read(self) -> WALStateStore | None:
        """The store when there is something to read, else ``None``.

        A read must not create a database on a pristine install, so this returns
        ``None`` unless the database already exists or a legacy document is there
        to import.
        """
        if self._store is not None and self._store.exists:
            return self._ensure_store()
        if wal_database_path(self._path).is_file() or self._path.is_file():
            return self._ensure_store()
        return None

    def _import_legacy_document(self, store: WALStateStore) -> int:
        """One-time import of a pre-V2.3 monolithic JSON journal.

        Returns the number of top-level keys imported (``0`` when there was
        nothing to import).  The marker row is recorded either way so a corrupt
        or already-empty legacy file is never re-examined on every open.
        """
        if store.has_marker(LEGACY_JSON_IMPORT_MARKER):
            return 0
        if store.section_count():
            store.record_marker(LEGACY_JSON_IMPORT_MARKER)
            return 0
        payload: Mapping[str, Any] | None = None
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, ValueError, RecursionError):
            raw = ""
        if raw:
            try:
                decoded = json.loads(raw)
            except (ValueError, RecursionError):
                decoded = None
            if isinstance(decoded, Mapping):
                payload = decoded
        if payload is None:
            store.record_marker(LEGACY_JSON_IMPORT_MARKER)
            return 0
        document = _normalise(payload, stamp=False)
        rows = {key: value for key, value in document.items() if key not in _DERIVED_KEYS}
        store.write_document(
            rows,
            saved_at_ns=int(document.get("saved_at_ns") or 0),
            schema_version=SCHEMA_VERSION,
        )
        store.record_marker(LEGACY_JSON_IMPORT_MARKER)
        return len(rows)

    # ------------------------------------------------------------------- read
    def _read_unlocked(self) -> dict[str, Any]:
        """Read and normalise, degrading to Clean Slate on any failure."""
        store = self._store_for_read()
        if store is None:
            return _clean_slate()
        try:
            document = store.read_document()
        except Exception:  # noqa: BLE001 - an unreadable store is a clean slate
            return _clean_slate()
        if not isinstance(document, Mapping) or not document:
            return _clean_slate()
        payload = dict(document)
        meta = payload.pop(RESERVED_DOCUMENT_SECTION, None)
        if isinstance(meta, Mapping):
            stored = meta.get("saved_at_ns")
            payload["saved_at_ns"] = (
                stored if isinstance(stored, int) and not isinstance(stored, bool) else 0
            )
        return _normalise(payload, stamp=False)

    def load(self) -> dict[str, Any]:
        """Return the current state; never raise.

        Missing, unreadable, corrupt or non-object data all answer a fresh
        :data:`CLEAN_SLATE` copy, i.e. zero logbook rows, zero backtests and zero
        agent tasks -- the Clean-Slate guarantee.
        """
        with self._lock:
            return self._read_unlocked()

    def snapshot(self) -> dict[str, Any]:
        """``load()`` plus where it came from and whether anything is on disk."""
        with self._lock:
            state = self._read_unlocked()
            state["journal_path"] = str(self._path)
            state["database_path"] = str(self.database_path)
            state["storage"] = "sqlite-wal"
            state["exists"] = bool(self._path.exists() or self.database_path.is_file())
            return state

    def counts(self) -> dict[str, int]:
        """Rows per canonical section (the document's ``counts`` field)."""
        state = self.load()
        return {
            section: len(state.get(section) or []) if isinstance(state.get(section), list) else 0
            for section in SECTIONS
        }

    # ------------------------------------------------------------------ write
    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """Hold one ``BEGIN IMMEDIATE`` transaction for a read-modify-write.

        Must be called with ``self._lock`` held.  The store's transaction is
        re-entrant, so :meth:`_write_unlocked` joins this one instead of opening a
        second write lock on the same connection.
        """
        store = self._ensure_store()
        with store.transaction():
            yield

    def _write_unlocked(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Commit ``state`` in one transaction and return the normalised document."""
        payload = _normalise(state, stamp=True)
        store = self._ensure_store()
        document = {key: value for key, value in payload.items() if key not in _DERIVED_KEYS}
        store.write_document(
            document,
            saved_at_ns=int(payload["saved_at_ns"]),
            schema_version=SCHEMA_VERSION,
        )
        # SQLite fsyncs the WAL on COMMIT (``synchronous=FULL``); the directory
        # entry for a file created by this very call still needs its own flush or
        # a power loss can lose the name while the payload survives orphaned.
        _fsync_dir(store.path)
        return payload

    def save(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Persist ``state`` atomically and return the normalised document.

        ``counts`` is recomputed from the real list lengths, ``saved_at_ns`` is
        stamped now, and ``clean_slate_active`` becomes ``False`` the moment any
        section holds a row -- a non-empty journal can never be reported as a
        clean slate.

        A section supplied as something other than a sequence is rejected rather
        than silently dropped; use :meth:`append` for one row at a time.
        """
        if not isinstance(state, Mapping):
            raise TypeError(f"STATE_NOT_MAPPING:{type(state).__name__}")
        for section in SECTIONS:
            if section in state and not isinstance(state[section], (list, tuple)):
                raise TypeError(f"SECTION_NOT_SEQUENCE:{section}")
        with self._lock:
            return self._write_unlocked(state)

    def update(self, **sections: Any) -> dict[str, Any]:
        """Merge ``sections`` into the stored document, then save it.

        A known section replaces that list wholesale (use :meth:`append` to add a
        single row); an unknown key is written through unchanged, so a caller can
        carry extra metadata without this module knowing about it.  Derived
        fields in ``sections`` are ignored -- :meth:`save` owns them.

        The read and the write share one ``BEGIN IMMEDIATE`` transaction, so two
        processes appending concurrently cannot lose each other's rows.
        """
        with self._lock:
            with self._write_transaction():
                current = self._read_unlocked()
                for section, value in sections.items():
                    if section in _DERIVED_KEYS:
                        continue
                    if section in SECTIONS:
                        if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
                            raise TypeError(f"SECTION_NOT_ITERABLE:{section}")
                        current[section] = [dict(row) if isinstance(row, Mapping) else row for row in value]
                    else:
                        current[section] = value
                return self._write_unlocked(current)

    def _read_section(self, section: str) -> dict[str, Any]:
        if section not in SECTIONS:
            raise ValueError(f"UNKNOWN_SECTION:{section}")
        return self._read_unlocked()

    def append(self, section: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Append one row to ``section``.  Nothing is capped or evicted."""
        if not isinstance(entry, Mapping):
            raise TypeError(f"ENTRY_NOT_MAPPING:{type(entry).__name__}")
        with self._lock:
            with self._write_transaction():
                current = self._read_section(section)
                current[section].append(dict(entry))
                return self._write_unlocked(current)

    def replace_section(self, section: str, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Replace every row in ``section`` with ``rows`` (explicit rewrite)."""
        with self._lock:
            with self._write_transaction():
                current = self._read_section(section)
                materialised: list[dict[str, Any]] = []
                for row in rows:
                    if not isinstance(row, Mapping):
                        raise TypeError(f"ROW_NOT_MAPPING:{type(row).__name__}")
                    materialised.append(dict(row))
                current[section] = materialised
                return self._write_unlocked(current)

    def clear_section(self, section: str) -> dict[str, Any]:
        """Empty one section, leaving the other two untouched."""
        with self._lock:
            with self._write_transaction():
                current = self._read_section(section)
                current[section] = []
                return self._write_unlocked(current)

    # ------------------------------------------------------------- backup paths
    def export_json(self, target: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """Write a deterministic JSON backup of the document; return a receipt.

        The body is canonical (``sort_keys``, two-space indent, UTF-8, trailing
        newline), so two exports of unchanged state are byte-identical -- which is
        what makes a backup verifiable rather than merely present.  This is the
        on-demand replacement for the old every-write monolithic file.
        """
        with self._lock:
            state = self._read_unlocked()
            body = json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False, default=str)
            path = (
                Path(os.fspath(target)).expanduser()
                if target is not None
                else self._path.with_name(f"{self._path.stem}.backup.json")
            )
            temp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(body)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
                _fsync_dir(path)
            except BaseException:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
        payload = body + "\n"
        return {
            "path": str(path),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "bytes": len(payload.encode("utf-8")),
            "sections": {section: len(state.get(section) or []) for section in SECTIONS},
            "clean_slate_active": bool(state.get("clean_slate_active", True)),
            "saved_at_ns": int(state.get("saved_at_ns") or 0),
            "deterministic": True,
        }

    def backup_document(self) -> dict[str, Any]:
        """The canonical document a backup would contain (no file is written)."""
        with self._lock:
            return self._read_unlocked()

    def store_stats(self) -> dict[str, Any]:
        """Durability facts about the backing database (never creates one)."""
        with self._lock:
            store = self._store_for_read()
            if store is None:
                return {
                    "path": str(wal_database_path(self._path)),
                    "exists": False,
                    "journal_mode": "wal",
                    "synchronous": "FULL",
                    "tables": {},
                    "sections": 0,
                    "legacy_json_imported": False,
                    "storage": "sqlite-wal",
                }
            stats = store.stats()
            stats["storage"] = "sqlite-wal"
            return stats

    def wal_checkpoint(self, mode: str = "TRUNCATE") -> dict[str, int]:
        """Fold the WAL back into the main file (``TRUNCATE`` empties it)."""
        with self._lock:
            store = self._store_for_read()
            if store is None:
                return {"busy": 0, "log_frames": 0, "checkpointed_frames": 0, "mode": str(mode)}
            return store.wal_checkpoint(mode)

    def reset(self) -> dict[str, Any]:
        """Atomic Clean Slate: zero every WAL table, then answer Clean Slate.

        ``PART 1.1`` requires a factory reset to truncate **every** WAL table and
        run ``PRAGMA wal_checkpoint(TRUNCATE)``: a reset that leaves rows (or a
        full write-ahead log) behind has not returned the install to zero.  The
        legacy JSON document, if one is still on disk, is unlinked too -- an
        untouched install therefore stays untouched and a migrated one stops
        looking migrated.
        """
        with self._lock:
            store: WALStateStore | None = None
            if self._store is not None and self._store.exists:
                store = self._ensure_store()
            elif wal_database_path(self._path).is_file() or self._path.is_file():
                store = self._ensure_store()
            try:
                os.unlink(self._path)
                _fsync_dir(self._path)
            except FileNotFoundError:
                pass
            except OSError:
                # A legacy file we cannot remove would be re-imported on the next
                # open, so pin the marker instead: the reset must survive a
                # read-only directory.
                if store is not None:
                    store.record_marker(LEGACY_JSON_IMPORT_MARKER)
            if store is not None:
                store.truncate_all()
                store.wal_checkpoint("TRUNCATE")
                self._imported = 1
            return _clean_slate()


_DEFAULT_LOCK = threading.RLock()
_DEFAULT_JOURNAL: AtomicStateJournal | None = None


def default_journal() -> AtomicStateJournal:
    """Process-wide journal, created on first use.

    The cached instance follows the currently resolved :func:`journal_path`, so a
    test that re-points ``NIOS_JOURNAL_PATH`` gets a journal for its own file
    rather than a stale handle on somebody else's.
    """
    global _DEFAULT_JOURNAL
    with _DEFAULT_LOCK:
        target = journal_path()
        if _DEFAULT_JOURNAL is None or _DEFAULT_JOURNAL.path != target:
            _DEFAULT_JOURNAL = AtomicStateJournal(target)
        return _DEFAULT_JOURNAL

# ---------------------------------------------------------------------------
# V1.9 (E9) -- SHA-256 replay checkpoints at event boundaries
# ---------------------------------------------------------------------------
#
# A journal that cannot prove it was replayed to a specific event boundary is
# only "probably" the same book.  A checkpoint binds the canonical state digest
# to the boundary digest, so a replay that stops early (or continues past the
# boundary) is detected instead of silently trusted.

CHECKPOINT_SECTION = "checkpoints"


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def state_digest(state: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical JSON of a journal state document.

    Derived and volatile keys are excluded: ``saved_at_ns`` changes on every
    write, ``counts`` / ``clean_slate_active`` are recomputed, and the
    checkpoint list itself would make the digest self-referential.
    """
    import hashlib

    volatile = {
        "schema_version",
        "saved_at_ns",
        "counts",
        "clean_slate_active",
        CHECKPOINT_SECTION,
    }
    payload = {key: value for key, value in dict(state).items() if key not in volatile}
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def boundary_digest(event_boundary: Any) -> str:
    """SHA-256 over the event-boundary descriptor (sequence, cursor, event id)."""
    import hashlib

    return hashlib.sha256(_canonical_json(event_boundary)).hexdigest()


class ReplayCheckpoint:
    """One immutable replay checkpoint record."""

    __slots__ = ("sequence", "state_sha256", "boundary_sha256", "created_at_ns", "label")

    def __init__(
        self,
        *,
        sequence: int,
        state_sha256: str,
        boundary_sha256: str,
        created_at_ns: int,
        label: str = "",
    ) -> None:
        self.sequence = int(sequence)
        self.state_sha256 = str(state_sha256)
        self.boundary_sha256 = str(boundary_sha256)
        self.created_at_ns = int(created_at_ns)
        self.label = str(label)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "state_sha256": self.state_sha256,
            "boundary_sha256": self.boundary_sha256,
            "created_at_ns": self.created_at_ns,
            "label": self.label,
        }


def _journal_checkpoint(
    journal: "AtomicStateJournal",
    state: Mapping[str, Any],
    *,
    event_boundary: Any,
    label: str = "",
    now_ns: int | None = None,
) -> dict[str, Any]:
    existing = journal.snapshot().get(CHECKPOINT_SECTION) or []
    rows = list(existing) if isinstance(existing, (list, tuple)) else []
    record = ReplayCheckpoint(
        sequence=len(rows) + 1,
        state_sha256=state_digest(state),
        boundary_sha256=boundary_digest(event_boundary),
        created_at_ns=int(now_ns if now_ns is not None else time.time_ns()),
        label=label,
    ).to_dict()
    rows.append(record)
    journal.update(**{CHECKPOINT_SECTION: rows})
    return record


def _journal_checkpoints(journal: "AtomicStateJournal") -> list[dict[str, Any]]:
    rows = journal.snapshot().get(CHECKPOINT_SECTION) or []
    return [dict(row) for row in rows] if isinstance(rows, (list, tuple)) else []


def _journal_verify_checkpoint(
    journal: "AtomicStateJournal",
    checkpoint: Mapping[str, Any],
    *,
    state: Mapping[str, Any] | None = None,
    event_boundary: Any = None,
) -> bool:
    target = state if state is not None else journal.snapshot()
    if state_digest(target) != str(checkpoint.get("state_sha256", "")):
        return False
    if event_boundary is not None:
        return boundary_digest(event_boundary) == str(
            checkpoint.get("boundary_sha256", "")
        )
    return True


def _journal_replay_checkpoint(
    journal: "AtomicStateJournal",
    state: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    *,
    event_boundary: Any = None,
) -> bool:
    """True only when ``state`` reproduces the checkpointed digest."""
    return _journal_verify_checkpoint(
        journal, checkpoint, state=state, event_boundary=event_boundary
    )


AtomicStateJournal.write_checkpoint = _journal_checkpoint  # type: ignore[attr-defined]
AtomicStateJournal.checkpoints = _journal_checkpoints  # type: ignore[attr-defined]
AtomicStateJournal.verify_checkpoint = _journal_verify_checkpoint  # type: ignore[attr-defined]
AtomicStateJournal.replay_checkpoint = _journal_replay_checkpoint  # type: ignore[attr-defined]
