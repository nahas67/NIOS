"""NIOS V2.3 -- Suite 39: SQLite WAL durability, PG16 migrations, tier-2
compaction and episodic operator memory.

The V2.3 database lane replaced a monolithic JSON journal with a crash-safe
SQLite **WAL** database, added a versioned PostgreSQL 16 migration chain, and
built the two upper memory tiers on top of it (compacted + checkpointed
``EntityMemoryGraph``, and an episodic log of operator interventions).  This
suite is the executable contract for all of it:

1. **WAL pragmas and integrity** -- ``journal_mode=wal``, ``synchronous=FULL``,
   ``foreign_keys=ON``, ``busy_timeout=5000`` and ``PRAGMA integrity_check``
   answering ``ok``.
2. **``BEGIN IMMEDIATE`` concurrency** -- many threads *and* several spawned
   processes writing at once must all land, with no ``database is locked``.
3. **Crash-restart persistence** -- the ``UnknownOrderQueue`` ladder (``1s ->
   5s -> 10s -> 30s -> 1m``), ``LiveBarAggregator`` bars (``250`` per series)
   and agent Q&A threads (``50`` turns) survive re-instantiating the store.
4. **Clean-slate zeroing** -- a workspace reset truncates *every* WAL table and
   leaves ``clean_slate_active`` true, with ``wal_checkpoint(TRUNCATE)``.
5. **Backward compatibility** -- ``AtomicStateJournal`` keeps its whole public
   API and exports byte-identical deterministic JSON backups.
6. **PG16** -- the shipped DDL parses to exactly 12 tables (including the 3
   V2.3 tables), the provisioner validates it with no server present, and the
   migration chain is ordered and SHA-256 pinned.
7. **Opt-in daemon scripts** -- ``verify_live_daemons.py`` and
   ``testnet_burnin.py`` exit ``0`` offline with honest ``SKIPPED_OFFLINE``
   reports, and never touch the network.
8. **Tier-2 compaction** -- ``truth_sha256`` is *byte-identical* between a full
   replay, a ``checkpoint + delta`` replay and a restart from ``load_state()``.
9. **3-signal hybrid retrieval** -- the semantic arm separates a legacy 2-signal
   tie, the ``0.25/0.45/0.30`` fusion is reported per truth, and every numeric
   claim stays inside a verified ``GroundedFactPacket``.
10. **Episodic memory** -- all six action kinds are recordable, a ``plannotator``
    mission override appears through ``GET /api/v2/memory/episodes``, filters
    and limits behave, ``POST /api/v2/memory/compact`` seals a verified
    checkpoint, and a caution narrative carries no digits at all.

Everything here is offline and deterministic: no PostgreSQL, no TigerBeetle, no
NATS, no network.  The online paths are gated behind an explicitly set
``NIOS_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ring0_vault.ibor.state_journal import (  # noqa: E402
    CLEAN_SLATE,
    SECTIONS,
    AtomicStateJournal,
    wal_database_path,
)
from ring0_vault.ibor.wal_state_store import (  # noqa: E402
    LEGACY_JSON_IMPORT_MARKER,
    WAL_PRAGMAS,
    WAL_SCHEMA_VERSION,
    WAL_TABLES,
    WALStateStore,
    wal_store_path,
)

SUITE_NAME = Path(__file__).name
DAY_NS = 86_400 * 1_000_000_000
UNITS = 1_000_000
PYTHON = sys.executable

#: Every WAL table ``PART 1.1`` names.  Order is documentation only.
EXPECTED_WAL_TABLES = (
    "nios_kv_sections",
    "nios_entity_timeline_local",
    "nios_entity_truths_local",
    "nios_memory_checkpoints_local",
    "nios_episodic_memory_local",
    "nios_working_memory_local",
    "nios_schema_migrations_local",
)

#: The 12 tables the PG16 settlement schema must create.
EXPECTED_PG16_TABLES = (
    "financial_outbox",
    "order_ack_inbox",
    "ibor_double_entry_ledger",
    "silo_vault_balances",
    "tax_lots",
    "source_quarantine_store",
    "grounding_ledger_hashes",
    "nios_entity_timeline",
    "nios_compiled_entity_truths",
    "nios_memory_checkpoints",
    "nios_episodic_memory",
    "nios_schema_migrations",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# multiprocessing worker (module level so ``spawn`` can pickle it)
# ---------------------------------------------------------------------------


def _wal_touch_worker(path: str, namespace: str, rows: int, marker: str) -> None:
    """Child process: open the same WAL database and write its own namespace.

    ``BEGIN IMMEDIATE`` plus ``busy_timeout`` is what makes this safe: the two
    processes serialise on the write lock instead of failing late with
    ``SQLITE_BUSY`` half way through a transaction.
    """

    store = WALStateStore(path)
    try:
        for index in range(rows):
            store.upsert_namespace(
                namespace,
                [
                    {
                        "row_key": f"{marker}-{index:04d}",
                        "sort_key": index,
                        "marker": marker,
                        "index": index,
                        "payload": {"pid": os.getpid(), "marker": marker},
                    }
                ],
            )
        store.upsert_namespace(
            namespace,
            [{"row_key": f"{marker}-summary", "sort_key": 10_000, "rows": rows}],
        )
        store.wal_checkpoint("FULL")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def isolated_journal_path(tmp_path_factory: pytest.TempPathFactory):
    """Point the process-wide journal at this module's own temp data root.

    ``conftest.py`` already keeps every suite out of ``<repo>/data``, but a
    suite that asserts "every WAL table is now empty" must own its database
    outright, so the journal path is re-pointed for the whole module and
    restored afterwards.
    """

    root = tmp_path_factory.mktemp("v23-suite39")
    previous = os.environ.get("NIOS_JOURNAL_PATH")
    os.environ["NIOS_JOURNAL_PATH"] = str(root / "nios_workstation_journal.json")
    try:
        yield root
    finally:
        if previous is None:
            os.environ.pop("NIOS_JOURNAL_PATH", None)
        else:
            os.environ["NIOS_JOURNAL_PATH"] = previous


@pytest.fixture()
def wal_db(tmp_path: Path) -> Path:
    return tmp_path / "nios_working_memory.wal.sqlite3"


@pytest.fixture()
def store(wal_db: Path):
    instance = WALStateStore(wal_db)
    try:
        instance.connection()
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def cockpit(isolated_journal_path: Path):
    """One real FastAPI app on this module's isolated data root."""

    from fastapi.testclient import TestClient

    from core.orchestrator import NIOSCoreOrchestrator
    from ring3_cockpit.gateway.control_plane import create_app

    workspace = isolated_journal_path / "workspace"
    vault = isolated_journal_path / "vault"
    workspace.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    runtime = NIOSCoreOrchestrator(
        workspace=workspace, key_vault_root=vault, clean_slate=True
    )
    with TestClient(create_app(runtime.control)) as client:
        yield client, runtime
    try:
        runtime.control.reset_workspace()
    except Exception:  # pragma: no cover - teardown is best effort
        pass
    closer = getattr(runtime, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # pragma: no cover - teardown is best effort
            pass


def _fresh_runtime(root: Path):
    """A throwaway orchestrator + TestClient pair on ``root``."""

    from fastapi.testclient import TestClient

    from core.orchestrator import NIOSCoreOrchestrator
    from ring3_cockpit.gateway.control_plane import create_app

    workspace = root / "workspace"
    vault = root / "vault"
    workspace.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    runtime = NIOSCoreOrchestrator(
        workspace=workspace, key_vault_root=vault, clean_slate=True
    )
    return runtime, TestClient(create_app(runtime.control))


def _wal_sidecar_sizes(database: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        sizes[suffix or "main"] = candidate.stat().st_size if candidate.is_file() else 0
    return sizes


# ===========================================================================
# Section 1 -- WAL pragmas, schema and integrity
# ===========================================================================


def test_suite39_is_registered_under_the_expected_name() -> None:
    assert SUITE_NAME == "test_v2_3_wal_db_compaction_and_episodic_memory.py"


def test_wal_store_declares_the_seven_tables_from_part_1_1() -> None:
    assert WAL_TABLES == EXPECTED_WAL_TABLES
    assert len(WAL_TABLES) == 7
    # V2.4 appends migration 0002 (hot-path covering indexes) to the local DDL.
    assert WAL_SCHEMA_VERSION == "v2.4.wal-state-store"
    assert WAL_PRAGMAS == {
        "journal_mode": "wal",
        "synchronous": "full",
        "foreign_keys": "on",
        "busy_timeout": "5000",
    }


def test_wal_store_path_is_derived_from_the_document_path() -> None:
    document = Path("C:/data/nios_workstation_journal.json")
    assert wal_store_path(document).name == "nios_workstation_journal.json.wal.sqlite3"
    assert wal_database_path(document) == wal_store_path(document)
    # Two documents that differ only in their suffix can never share one database.
    assert wal_store_path("a.json") != wal_store_path("a.json.backup")


def test_wal_store_applies_every_required_pragma(store: WALStateStore) -> None:
    connection = store.connection()
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])

    assert journal_mode == "wal"
    assert store.journal_mode == "wal"
    # FULL == 2 in SQLite's synchronous enum.
    assert synchronous == 2
    assert foreign_keys == 1
    assert busy_timeout == 5_000


def test_wal_store_integrity_check_is_ok_and_schema_is_complete(store: WALStateStore) -> None:
    assert store.integrity_check() == "ok"

    connection = store.connection()
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = [table for table in EXPECTED_WAL_TABLES if table not in names]
    assert not missing, missing

    versions = store.schema_versions()
    assert [row["version"] for row in versions] == [1, 2]
    assert [row["name"] for row in versions] == [
        "0001_wal_state_store",
        "0002_hot_path_indexes",
    ]
    for row in versions:
        assert _HEX64.match(row["checksum_sha256"])

    stats = store.stats()
    assert stats["journal_mode"] == "wal"
    assert stats["synchronous"] == "FULL"
    assert stats["foreign_keys"] is True
    assert stats["busy_timeout_ms"] == 5_000
    assert stats["integrity"] == "ok"
    assert stats["tables"].keys() == set(EXPECTED_WAL_TABLES)


def test_wal_store_rejects_a_tampered_migration_checksum(store: WALStateStore) -> None:
    """A recorded checksum that no longer matches the code must raise, not guess."""

    from ring0_vault.ibor.wal_state_store import WALStoreError

    with store.transaction() as tx:
        tx.execute(
            "UPDATE nios_schema_migrations_local SET checksum_sha256 = ? WHERE version = 1",
            ("0" * 64,),
        )
    with pytest.raises(WALStoreError, match="WAL_MIGRATION_CHECKSUM_MISMATCH"):
        store.apply_migrations()


# ===========================================================================
# Section 2 -- BEGIN IMMEDIATE concurrent write safety
# ===========================================================================


def test_begin_immediate_survives_concurrent_threads(wal_db: Path) -> None:
    """Twelve threads writing through ``BEGIN IMMEDIATE`` all land."""

    store = WALStateStore(wal_db, busy_timeout_ms=15_000)
    errors: list[str] = []
    threads = 12
    rows = 15
    try:
        store.connection()

        def worker(index: int) -> None:
            try:
                for turn in range(rows):
                    store.upsert_namespace(
                        f"nios.test.threads.{index}",
                        [
                            {
                                "row_key": f"t{index}-{turn:03d}",
                                "sort_key": turn,
                                "thread": index,
                                "turn": turn,
                            }
                        ],
                    )
            except Exception as exc:  # noqa: BLE001 - the failure is the assertion
                errors.append(f"{type(exc).__name__}: {exc}")

        handles = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
        for handle in handles:
            handle.start()
        for handle in handles:
            handle.join(timeout=60)

        assert not errors, errors
        assert not any("locked" in message.lower() for message in errors)

        total = 0
        for index in range(threads):
            total += store.count_namespace(f"nios.test.threads.{index}")
        assert total == threads * rows
        assert store.integrity_check() == "ok"
    finally:
        store.close()


def test_begin_immediate_survives_concurrent_processes(wal_db: Path) -> None:
    """Two spawned processes (plus this one) writing one database all land."""

    import multiprocessing

    store = WALStateStore(wal_db, busy_timeout_ms=20_000)
    try:
        store.connection()
        context = multiprocessing.get_context("spawn")
        workers = [
            context.Process(
                target=_wal_touch_worker,
                args=(str(wal_db), f"nios.test.proc.{tag}", 12, tag),
            )
            for tag in ("alpha", "beta")
        ]
        for worker in workers:
            worker.start()
        for index in range(8):
            store.upsert_namespace(
                "nios.test.proc.parent",
                [{"row_key": f"parent-{index:03d}", "sort_key": index}],
            )
        for worker in workers:
            worker.join(timeout=120)
            assert worker.exitcode == 0, worker.exitcode

        assert store.count_namespace("nios.test.proc.parent") == 8
        for tag in ("alpha", "beta"):
            # 12 rows + the summary row each child writes last.
            assert store.count_namespace(f"nios.test.proc.{tag}") == 13
        assert store.integrity_check() == "ok"
        assert store.stats()["integrity"] == "ok"
    finally:
        store.close()


def test_begin_immediate_rolls_back_a_failed_transaction(store: WALStateStore) -> None:
    """A raising unit of work leaves no half-written row behind."""

    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction() as tx:
            tx.execute(
                "INSERT OR REPLACE INTO nios_working_memory_local"
                " (namespace, row_key, sort_key, payload_json, payload_sha256, updated_at_utc_ns)"
                " VALUES ('nios.test.rollback', 'k', 0, '{}', 'x', 0)"
            )
            raise RuntimeError("boom")
    assert store.count_namespace("nios.test.rollback") == 0


def test_transaction_is_reentrant_on_one_thread(store: WALStateStore) -> None:
    """A helper inside an outer transaction joins it instead of deadlocking."""

    with store.transaction() as outer:
        outer.execute(
            "INSERT OR REPLACE INTO nios_working_memory_local"
            " (namespace, row_key, sort_key, payload_json, payload_sha256, updated_at_utc_ns)"
            " VALUES ('nios.test.reentrant', 'a', 0, '{}', 'x', 0)"
        )
        store.upsert_namespace("nios.test.reentrant", [{"row_key": "b", "sort_key": 1}])
    assert store.count_namespace("nios.test.reentrant") == 2


# ===========================================================================
# Section 3 -- crash-restart persistence of Tier-1 working memory
# ===========================================================================


def _tier1_fixture_populate(store_wal: WALStateStore) -> dict[str, Any]:
    """Write the exact Tier-1 state a mid-day crash would leave behind."""

    from ring2_brain.memory.working_memory import WorkingMemoryStore

    memory = WorkingMemoryStore(backend=store_wal)
    base_ns = 1_700_000_000_000_000_000

    # -- retry ladder -------------------------------------------------------
    for index, attempts in enumerate((0, 1, 2, 3, 5)):
        memory.record_unknown_order(
            order_id=f"UNK-{index}",
            silo="CRYPTO_BINANCE",
            symbol="BTCUSDT",
            client_order_id=f"COID-{index}",
            attempts=attempts,
            now_ns=base_ns + index * 1_000_000,
        )
    # -- bars ---------------------------------------------------------------
    for index in range(4):
        memory.ingest_bar(
            {
                "silo": "CRYPTO_BINANCE",
                "symbol": "BTCUSDT",
                "timeframe": "1m",
                "start_utc_ns": base_ns + index * 60 * 1_000_000_000,
                "open_micros": 68_000 * UNITS,
                "high_micros": 68_100 * UNITS,
                "low_micros": 67_900 * UNITS,
                "close_micros": 68_050 * UNITS,
                "volume_micros": 12 * UNITS,
                "trades": 4,
                "closed": index < 3,
            },
            now_ns=base_ns + index * 1_000_000,
        )
    # -- chat ---------------------------------------------------------------
    for turn in range(6):
        memory.append_chat_turn(
            session_id="sess-1",
            topic="btc",
            role="operator" if turn % 2 == 0 else "agent",
            content=f"turn {turn}",
            now_ns=base_ns + turn * 1_000_000,
        )
    return memory.export()


def test_tier1_working_memory_survives_a_restart(wal_db: Path) -> None:
    """Re-instantiating the store against the same file recovers everything."""

    from ring2_brain.memory.working_memory import (
        MAX_BARS_PER_SERIES,
        MAX_CHAT_TURNS,
        WorkingMemoryStore,
        sha256_turn_digest,
    )

    before = WALStateStore(wal_db)
    try:
        before.connection()
        exported = _tier1_fixture_populate(before)
    finally:
        before.close()

    after = WALStateStore(wal_db)
    try:
        recovered = WorkingMemoryStore(backend=after)
        assert recovered.durable is True

        # -- the retry ladder is intact, in due order ----------------------
        orders = recovered.unknown_orders()
        assert [row["order_id"] for row in orders] == [
            "UNK-0",
            "UNK-1",
            "UNK-2",
            "UNK-3",
            "UNK-4",
        ]
        assert [int(row["attempts"]) for row in orders] == [0, 1, 2, 3, 5]
        assert all(row["state"] == "PENDING" for row in orders)
        assert [row["client_order_id"] for row in orders] == [
            f"COID-{index}" for index in range(5)
        ]

        # -- the ladder is exactly 1s -> 5s -> 10s -> 30s -> 1m -------------
        assert [WorkingMemoryStore.retry_delay_s(rung) for rung in range(5)] == [
            1,
            5,
            10,
            30,
            60,
        ]
        # Past the last rung the delay saturates instead of running off the end.
        assert WorkingMemoryStore.retry_delay_s(9) == 60
        for row in orders:
            expected = int(row["updated_at_utc_ns"]) + WorkingMemoryStore.retry_delay_s(
                int(row["attempts"])
            ) * 1_000_000_000
            assert int(row["next_retry_at_utc_ns"]) == expected

        # -- bars ----------------------------------------------------------
        bars = recovered.bars(silo="CRYPTO_BINANCE", symbol="BTCUSDT", timeframe="1m")
        assert len(bars) == 4
        assert [bar["closed"] for bar in bars] == [True, True, True, False]
        assert bars[-1]["close_micros"] == 68_050 * UNITS
        assert recovered.counts()["instruments"] == 1
        assert len(bars) <= MAX_BARS_PER_SERIES

        # -- chat turns, with deterministic digests ------------------------
        thread = recovered.chat_thread("sess-1", "btc")
        assert len(thread) == 6
        assert [row["role"] for row in thread][:2] == ["operator", "agent"]
        for row in thread:
            recomputed = sha256_turn_digest(
                session_id=row["session_id"],
                topic=row["topic"],
                turn_index=int(row["turn_index"]),
                role=row["role"],
                content=row["content"],
                ts_utc_ns=int(row["ts_utc_ns"]),
            )
            assert row["turn_sha256"] == recomputed
            assert _HEX64.match(row["turn_sha256"])
        # The digest is a pure function of the turn, not of the writer's clock.
        assert sha256_turn_digest(
            session_id="sess-1",
            topic="btc",
            turn_index=0,
            role="operator",
            content="turn 0",
            ts_utc_ns=1_700_000_000_000_000_000,
        ) == thread[0]["turn_sha256"]

        # -- a second export of the recovered state is byte-identical ------
        assert recovered.export()["payload_sha256"] == exported["payload_sha256"]
        assert len(thread) <= MAX_CHAT_TURNS
        assert recovered.wipe()["counts"] == {
            "unknown_orders": 0,
            "bars": 0,
            "instruments": 0,
            "chat_turns": 0,
        }
    finally:
        after.close()


def test_tier1_bounds_trim_to_the_newest_rows(wal_db: Path) -> None:
    """``250`` bars per series and ``50`` turns per thread are hard bounds."""

    from ring2_brain.memory.working_memory import (
        MAX_BARS_PER_SERIES,
        MAX_CHAT_TURNS,
        WorkingMemoryStore,
    )

    store_wal = WALStateStore(wal_db)
    try:
        memory = WorkingMemoryStore(backend=store_wal)
        assert memory.max_bars_per_series == 250
        assert memory.max_chat_turns == 50

        for index in range(MAX_BARS_PER_SERIES + 12):
            memory.ingest_bar(
                {
                    "silo": "US_IBKR",
                    "symbol": "AAPL",
                    "timeframe": "1m",
                    "start_utc_ns": 1_700_000_000_000_000_000 + index * 60_000_000_000,
                    "close_micros": (180 * UNITS) + index,
                    "closed": True,
                }
            )
        series = memory.bars(silo="US_IBKR", symbol="AAPL", timeframe="1m")
        assert len(series) == MAX_BARS_PER_SERIES
        # The window is the *newest* 250, never the oldest.
        assert int(series[-1]["start_utc_ns"]) > int(series[0]["start_utc_ns"])
        assert int(series[-1]["close_micros"]) == 180 * UNITS + MAX_BARS_PER_SERIES + 11

        for turn in range(MAX_CHAT_TURNS + 10):
            memory.append_chat_turn(
                session_id="sess-bound",
                topic="bounds",
                role="agent",
                content=f"turn {turn}",
            )
        thread = memory.chat_thread("sess-bound", "bounds")
        assert len(thread) == MAX_CHAT_TURNS
        assert thread[-1]["content"] == f"turn {MAX_CHAT_TURNS + 9}"
        # The index keeps advancing once the window is full, so every retained
        # turn still carries a unique, self-consistent digest.
        assert [row["turn_index"] for row in thread] == list(
            range(10, MAX_CHAT_TURNS + 10)
        )
        digests = [row["turn_sha256"] for row in thread]
        assert len(set(digests)) == len(digests)
    finally:
        store_wal.close()


def test_unknown_order_ladder_reschedules_then_escalates(wal_db: Path) -> None:
    """``mark_unknown_attempt`` walks the ladder and then escalates to a human."""

    from ring2_brain.memory.working_memory import RETRY_BACKOFF_S, WorkingMemoryStore

    store_wal = WALStateStore(wal_db)
    try:
        memory = WorkingMemoryStore(backend=store_wal)
        base = 1_700_000_000_000_000_000
        memory.record_unknown_order(order_id="UNK-ESC", now_ns=base)
        seen: list[int] = []
        row: dict[str, Any] = {}
        # Attempts 1..4 reschedule through the 5s/10s/30s/60s rungs.
        for attempt in range(len(RETRY_BACKOFF_S) - 1):
            row = memory.mark_unknown_attempt(
                "UNK-ESC", now_ns=base + (attempt + 1) * 1_000_000
            )
            assert row["state"] == "PENDING"
            delay = int(row["next_retry_at_utc_ns"]) - (base + (attempt + 1) * 1_000_000)
            seen.append(delay // 1_000_000_000)
        assert seen == [5, 10, 30, 60]
        # The fifth probe reaches the hard ceiling instead of looping forever.
        row = memory.mark_unknown_attempt("UNK-ESC", now_ns=base + 9_000_000)
        assert row["state"] == "ESCALATED"
        assert row["escalation_state"] == "MANUAL_INTERVENTION_REQUIRED"
        assert row["resolution"] == "AWAITING_OPERATOR"
        # Escalated rows stay on the ladder (they are still an open question) ...
        assert memory.counts()["unknown_orders"] == 1
        assert memory.restore_into_unknown_queue(_DummyQueue())["restored"] == 1
        # ... and only a definitive venue answer removes one.
        resolved = memory.resolve_unknown_order("UNK-ESC", resolution="FILLED")
        assert resolved["state"] == "RESOLVED"
        assert memory.counts()["unknown_orders"] == 0
    finally:
        store_wal.close()


class _DummyQueue:
    """Minimal ``UnknownOrderQueue`` stand-in for the restore contract."""

    def __init__(self) -> None:
        self.rows_restored: tuple[dict[str, Any], ...] = ()

    def restore(self, rows: Any) -> dict[str, Any]:
        self.rows_restored = tuple(dict(row) for row in rows)
        return {"restored": len(self.rows_restored)}


# ===========================================================================
# Section 4 -- clean slate zeroes every WAL table
# ===========================================================================


def test_clean_slate_zeroes_every_wal_table(
    store: WALStateStore, tmp_path: Path
) -> None:
    """``AtomicStateJournal.reset()`` truncates the schema *and* the WAL."""

    document = tmp_path / "journal.json"
    journal = AtomicStateJournal(document, store=store)
    journal.update(logbook=[{"row": 1}], backtests=[{"row": 2}])
    journal.store.append_episode(
        {
            "episode_id": "EP-RESET",
            "ts_utc": 1,
            "actor": "operator",
            "action_kind": "MISSION_OVERRIDE",
            "context_tags": [],
        }
    )
    journal.store.upsert_namespace("nios.working.chat", [{"row_key": "a", "sort_key": 0}])
    assert sum(journal.store.table_counts().values()) > 0

    report = journal.reset()

    counts = journal.store.table_counts()
    assert counts == {table: 0 for table in EXPECTED_WAL_TABLES}
    assert report["clean_slate_active"] is True
    assert set(report) >= set(CLEAN_SLATE)
    assert journal.load()["clean_slate_active"] is True
    assert journal.counts() == {section: 0 for section in SECTIONS}

    checkpoint = journal.wal_checkpoint("TRUNCATE")
    assert checkpoint["mode"] == "TRUNCATE"
    # TRUNCATE empties the write-ahead log instead of just folding it in.
    assert _wal_sidecar_sizes(journal.database_path)["-wal"] == 0


def test_workspace_reset_endpoint_zeroes_the_wal(store: WALStateStore, tmp_path: Path) -> None:
    """``POST /api/v2/control/workspace/reset`` leaves no WAL row anywhere."""

    journal = AtomicStateJournal(tmp_path / "journal.json", store=store)

    runtime, client = _fresh_runtime(tmp_path / "runtime")
    try:
        with client:
            # Seed every tier: journal sections, entity memory, episodes and
            # Tier-1 working memory.
            seeded = client.post(
                "/api/v2/memory/entities",
                json={
                    "outcome_kind": "LOSS",
                    "net_pnl_units": -2.5,
                    "slippage_bps": 18.0,
                    "symbol": "BTCUSDT",
                    "silo": "CRYPTO_BINANCE",
                    "lesson_summary": "reset seed",
                },
            )
            assert seeded.status_code == 200, seeded.text
            runtime.working_memory.record_unknown_order(order_id="UNK-RESET")
            runtime.working_memory.ingest_bar(
                {
                    "silo": "CRYPTO_BINANCE",
                    "symbol": "BTCUSDT",
                    "timeframe": "1m",
                    "start_utc_ns": 1_700_000_000_000_000_000,
                    "close_micros": 68_000 * UNITS,
                    "closed": True,
                }
            )
            runtime.working_memory.append_chat_turn(
                session_id="s", topic="t", content="hello"
            )
            runtime.control.record_episodic_decision(
                "CLEAR_FREEZE", {"silo": "CRYPTO_BINANCE", "note": "seed"}
            )
            snapshot = runtime.control.journal_snapshot()
            assert snapshot["storage"] == "sqlite-wal"
            assert snapshot["database_path"].endswith(".wal.sqlite3")
            assert snapshot["exists"] is True
            assert sum(runtime.control.state_journal.store.table_counts().values()) > 0

            reset = client.post(
                "/api/v2/control/workspace/reset",
                json={"reason": "suite 39", "workspace": "paper"},
            )
            assert reset.status_code == 200, reset.text
            body = reset.json()
            assert body["reset"] is True
            assert body["clean_slate_active"] is True

            counts = runtime.control.state_journal.store.table_counts()
            assert counts == {table: 0 for table in EXPECTED_WAL_TABLES}, counts
            assert runtime.control.state_journal.load()["clean_slate_active"] is True

            episodes = client.get("/api/v2/memory/episodes")
            assert episodes.status_code == 200
            assert episodes.json()["count"] == 0
            assert runtime.working_memory.counts() == {
                "unknown_orders": 0,
                "bars": 0,
                "instruments": 0,
                "chat_turns": 0,
            }
    finally:
        closer = getattr(runtime, "close", None)
        if callable(closer):
            closer()
    # The explicitly injected journal is untouched by the runtime above; its own
    # reset is the second half of the same contract.
    journal.update(logbook=[{"row": 1}])
    journal.reset()
    assert journal.store.table_counts() == {table: 0 for table in EXPECTED_WAL_TABLES}


def test_auth_reset_endpoint_zeroes_the_wal(isolated_journal_path: Path) -> None:
    """``POST /api/v2/auth/reset`` is a factory reset down to the WAL."""

    runtime, client = _fresh_runtime(isolated_journal_path / "auth-reset")
    try:
        with client:
            seeded = client.post(
                "/api/v2/memory/entities",
                json={
                    "outcome_kind": "WIN",
                    "net_pnl_units": 4.0,
                    "symbol": "ETHUSDT",
                    "silo": "CRYPTO_BINANCE",
                    "lesson_summary": "auth reset seed",
                },
            )
            assert seeded.status_code == 200, seeded.text
            runtime.working_memory.record_unknown_order(order_id="UNK-AUTH")
            assert sum(runtime.control.state_journal.store.table_counts().values()) > 0

            reset = client.post("/api/v2/auth/reset", json={})
            assert reset.status_code == 200, reset.text
            assert reset.json()["ok"] is True
            counts = runtime.control.state_journal.store.table_counts()
            assert counts == {table: 0 for table in EXPECTED_WAL_TABLES}, counts
            assert runtime.control.state_journal.load()["clean_slate_active"] is True
    finally:
        closer = getattr(runtime, "close", None)
        if callable(closer):
            closer()


# ===========================================================================
# Section 5 -- backward compatibility of AtomicStateJournal
# ===========================================================================


def test_journal_public_api_is_unchanged(tmp_path: Path) -> None:
    document = tmp_path / "nios_workstation_journal.json"
    journal = AtomicStateJournal(document)

    assert SECTIONS == ("logbook", "backtests", "agent_tasks")
    fresh = journal.load()
    assert fresh["clean_slate_active"] is True
    assert fresh["counts"] == {"logbook": 0, "backtests": 0, "agent_tasks": 0}

    journal.append("logbook", {"id": "L1"})
    journal.append("backtests", {"id": "B1"})
    journal.append("agent_tasks", {"id": "A1"})
    state = journal.load()
    assert state["counts"] == {"logbook": 1, "backtests": 1, "agent_tasks": 1}
    assert state["clean_slate_active"] is False
    assert state["schema_version"] == "v1.8.state-journal"
    assert journal.counts() == state["counts"]

    # ``update`` merges, ``replace_section`` replaces, ``clear_section`` empties.
    journal.update(logbook=[{"id": "L2"}, {"id": "L3"}])
    assert journal.counts()["logbook"] == 2
    journal.replace_section("backtests", [{"id": "B2"}])
    assert [row["id"] for row in journal.load()["backtests"]] == ["B2"]
    journal.clear_section("agent_tasks")
    assert journal.load()["agent_tasks"] == []

    # Section type enforcement survives the storage swap.
    with pytest.raises(TypeError, match="SECTION_NOT_SEQUENCE"):
        journal.save({"logbook": "not-a-sequence"})

    # ``store_stats`` reports the real durability facts.
    stats = journal.store_stats()
    assert stats["storage"] == "sqlite-wal"
    assert stats["journal_mode"] == "wal"
    assert stats["synchronous"] == "FULL"
    assert journal.database_path == wal_database_path(document)

    journal.reset()
    assert journal.load()["clean_slate_active"] is True


def test_journal_exports_deterministic_backups(tmp_path: Path) -> None:
    journal = AtomicStateJournal(tmp_path / "journal.json")
    journal.append("logbook", {"id": "L1", "note": "café"})
    journal.append("backtests", {"id": "B1"})

    first = journal.export_json(tmp_path / "backup-a.json")
    second = journal.export_json(tmp_path / "backup-b.json")
    assert first["sha256"] == second["sha256"]
    assert first["bytes"] == second["bytes"]
    assert first["deterministic"] is True
    assert Path(first["path"]).read_bytes() == Path(second["path"]).read_bytes()
    assert first["sections"] == {"logbook": 1, "backtests": 1, "agent_tasks": 0}

    document = journal.backup_document()
    assert document == journal.backup_document()
    assert json.loads(Path(first["path"]).read_text(encoding="utf-8")) == document

    store_export = journal.store.export_json(tmp_path / "wal-backup.json")
    assert store_export["deterministic"] is True
    assert store_export["storage"] == "sqlite-wal"
    store_export_again = journal.store.export_json(tmp_path / "wal-backup-2.json")
    assert store_export["sha256"] == store_export_again["sha256"]
    payload = json.loads(Path(store_export["path"]).read_text(encoding="utf-8"))
    assert payload["schema_version"] == WAL_SCHEMA_VERSION
    assert payload["tables"].keys() == set(EXPECTED_WAL_TABLES)


def test_legacy_json_journal_is_imported_once_and_never_rewritten(tmp_path: Path) -> None:
    document = tmp_path / "nios_workstation_journal.json"
    legacy = {
        "schema_version": "v1.8.state-journal",
        "saved_at_ns": 1_700_000_000_000_000_000,
        "logbook": [{"id": "LEGACY-1"}],
        "backtests": [],
        "agent_tasks": [{"id": "TASK-1"}],
    }
    document.write_text(json.dumps(legacy, indent=2), encoding="utf-8")
    before = document.read_bytes()

    journal = AtomicStateJournal(document)
    state = journal.load()
    assert [row["id"] for row in state["logbook"]] == ["LEGACY-1"]
    assert [row["id"] for row in state["agent_tasks"]] == ["TASK-1"]
    assert state["clean_slate_active"] is False
    assert journal.store.has_marker(LEGACY_JSON_IMPORT_MARKER) is True
    # The legacy file is read once and left exactly as it was found.
    assert document.read_bytes() == before

    # A second instance does not re-import over newer data.
    journal.append("logbook", {"id": "NEW-1"})
    reopened = AtomicStateJournal(document)
    assert [row["id"] for row in reopened.load()["logbook"]] == ["LEGACY-1", "NEW-1"]
    assert reopened.store.section_count() == 3

    # A reset unlinks the legacy file, so it cannot resurrect on the next open.
    reopened.reset()
    assert not document.exists()
    assert AtomicStateJournal(document).load()["clean_slate_active"] is True


# ===========================================================================
# Section 6 -- PostgreSQL 16 schema, DDL validator and migrations
# ===========================================================================


def test_pg16_schema_parses_to_exactly_twelve_tables() -> None:
    from ring0_vault.ibor.pg16_repository import pg_schema_statements

    statements = pg_schema_statements()
    created: list[str] = []
    for statement in statements:
        match = re.match(
            r"\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z0-9_]+)",
            statement,
            flags=re.IGNORECASE,
        )
        if match:
            created.append(match.group(1))
    assert len(created) == 12, created
    assert tuple(created) == EXPECTED_PG16_TABLES

    # The transaction lifecycle belongs to the repository, not to the file body.
    for statement in statements:
        head = " ".join(statement.split()).upper()
        assert not head.startswith("BEGIN")
        assert not head.startswith("COMMIT")
        assert not head.startswith("SET TRANSACTION")


def test_pg16_schema_declares_the_three_v23_tables() -> None:
    from ring0_vault.ibor.pg16_repository import pg_schema_statements

    body = "\n".join(pg_schema_statements())

    def block(table: str) -> str:
        start = body.index(f"CREATE TABLE IF NOT EXISTS {table}")
        # Statements are shipped without their trailing semicolon, so the body
        # of one table ends at the first close-paren on its own line.
        end = body.index("\n)", start)
        return body[start:end]

    checkpoints = block("nios_memory_checkpoints")
    for column in (
        "checkpoint_seq",
        "cutoff_ts_utc",
        "compiled_truths",
        "checkpoint_sha256",
        "created_at_utc",
    ):
        assert column in checkpoints, column
    assert "jsonb" in checkpoints.lower()

    episodes = block("nios_episodic_memory")
    for column in (
        "episode_id",
        "ts_utc",
        "actor",
        "action_kind",
        "regime",
        "context_tags",
        "decision_summary",
        "payload_sha256",
    ):
        assert column in episodes, column
    assert "GIN (context_tags)" in body
    assert "idx_episodic_memory_tags" in body

    migrations = block("nios_schema_migrations")
    for column in ("version", "applied_at_utc", "checksum_sha256"):
        assert column in migrations, column
    assert "0-9a-f" in migrations  # the 64-hex digest CHECK


def test_pg16_provisioner_validates_twelve_tables_offline() -> None:
    from ring0_vault.ibor.tb_pg_provisioner import EXPECTED_TABLES, PG16Provisioner

    assert len(EXPECTED_TABLES) == 12
    assert EXPECTED_TABLES == EXPECTED_PG16_TABLES

    validation = PG16Provisioner(dsn=None).validate()
    assert validation.valid is True, validation.errors
    assert validation.errors == []
    assert validation.serializable is True
    assert validation.sqlite_executed is True
    assert len(validation.tables) == 12
    assert sorted(validation.tables) == sorted(EXPECTED_PG16_TABLES)
    assert validation.checks >= 25
    assert validation.indexes >= 20
    assert _HEX64.match(validation.sha256)
    assert len(validation.to_dict()["tables"]) == 12


def test_pg16_migration_chain_is_ordered_and_checksum_pinned() -> None:
    from ring0_vault.ibor.pg16_repository import MIGRATION_LOCK_KEY, migrations

    chain = migrations()
    assert [migration.version for migration in chain] == [1, 2, 3]
    assert len({migration.checksum_sha256 for migration in chain}) == 3
    assert MIGRATION_LOCK_KEY == 0x4E494F53

    for migration in chain:
        assert _HEX64.match(migration.checksum_sha256)
        # Deterministic: recomputing the body yields the same pin.
        assert migration.checksum_sha256 == type(migration)(
            version=migration.version,
            name=migration.name,
            forward_sql=migration.forward_sql,
            rollback_sql=migration.rollback_sql,
        ).checksum_sha256
        assert migration.rollback_sql.strip().upper().startswith(("DROP", "CREATE"))
        payload = migration.to_dict()
        assert payload["forward_bytes"] > 0
        assert payload["rollback_bytes"] > 0

    # A whitespace-only edit must not change the pin (the checksum is over
    # normalised SQL, so formatting churn cannot fake a schema change).
    baseline = chain[0]
    reformatted = type(baseline)(
        version=baseline.version,
        name=baseline.name,
        forward_sql=baseline.forward_sql.replace("\n", "\n\n  "),
        rollback_sql=baseline.rollback_sql,
    )
    assert reformatted.checksum_sha256 == baseline.checksum_sha256

    # Version 1 covers the whole shipped schema, including the V2.3 tables.
    for table in EXPECTED_PG16_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in baseline.forward_sql


def test_pg16_repository_is_inert_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    from ring0_vault.ibor.pg16_repository import (
        MigrationChecksumMismatch,
        Pg16Repository,
        Pg16Unavailable,
    )

    monkeypatch.delenv("NIOS_PG_DSN", raising=False)
    repository = Pg16Repository()
    assert repository.available is False
    assert repository.dsn is None
    assert repository.probe() == {"available": False, "reason": "NO_DSN", "dsn": None}

    status = repository.status()
    assert status["available"] is False
    assert status["reason"] == "NO_DSN"
    assert len(status["migrations"]) == 3
    assert status["applied_this_process"] == []

    for call in (
        repository.apply_migrations,
        repository.migration_status,
        repository.verify_rollback,
        repository.ddl_roundtrip,
    ):
        with pytest.raises(Pg16Unavailable):
            call()
    with pytest.raises(Pg16Unavailable):
        repository.find_episodes_by_context_tags(["BTCUSDT"])
    with pytest.raises(Pg16Unavailable):
        repository.find_timeline_by_entity_ids(["symbol:BTCUSDT"])

    # A dual-write against an offline repository is a status, never an exception.
    report = repository.sync_from_journal(object())
    assert report["available"] is False
    assert report["written"] == {}

    # The DSN is redacted in every report.
    credentials = Pg16Repository("postgresql://nios:secret@127.0.0.1:5432/nios")
    assert credentials.redacted_dsn == "postgresql://nios:***@127.0.0.1:5432/nios"
    assert "secret" not in credentials.status()["dsn"]
    assert issubclass(MigrationChecksumMismatch, RuntimeError)


@pytest.mark.skipif(
    not os.environ.get("NIOS_PG_DSN"),
    reason="NIOS_PG_DSN is not set; the online PostgreSQL path is opt-in only",
)
def test_pg16_repository_live_roundtrip() -> None:  # pragma: no cover - opt-in
    """Opt-in: a real PostgreSQL 16 server must pass the DDL + GIN round-trip."""

    from ring0_vault.ibor.pg16_repository import Pg16Repository

    repository = Pg16Repository(timeout_s=8.0)
    try:
        probe = repository.probe()
        assert probe["available"] is True, probe
        ddl = repository.ddl_roundtrip()
        assert ddl["ok"] is True, ddl
        assert ddl["table_count"] == 12, ddl
        assert ddl["rolled_back"] is True
        rollback = repository.verify_rollback()
        assert all(row["rollback_proven"] for row in rollback), rollback
    finally:
        repository.close()


# ===========================================================================
# Section 7 -- opt-in daemon scripts stay offline-safe
# ===========================================================================


def _script_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in ("NIOS_PG_DSN", "NIOS_TB_ADDRESS", "NIOS_NATS_URL"):
        env.pop(name, None)
    for name in ("NIOS_BINANCE_TESTNET_KEY", "NIOS_BINANCE_TESTNET_SECRET"):
        env.pop(name, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_script(script: str, *args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(ROOT / "scripts" / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(ROOT),
        env=_script_env(),
        check=False,
    )


def test_verify_live_daemons_script_is_offline_safe() -> None:
    completed = _run_script("verify_live_daemons.py", "--json")
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout)

    assert report["overall"] == "OFFLINE"
    assert report["schema"] == "v2.3.verify-live-daemons"
    assert report["offline_safe"] is True
    assert _HEX64.match(report["report_sha256"])
    assert report["signature"]["digest"] == report["report_sha256"]
    assert report["signature"]["algorithm"] == "sha256"

    daemons = {row["name"]: row for row in report["daemons"]}
    assert sorted(daemons) == ["nats", "postgres", "tigerbeetle"]
    assert report["counts"] == {"ok": 0, "skipped": 3, "failed": 0}
    for name, row in daemons.items():
        assert row["status"] == "SKIPPED_OFFLINE", name
        assert row["configured"] is False
        assert row["reachable"] is False
        assert row["start_commands"], name

    # The human output carries the exact Docker commands an operator needs.
    human = _run_script("verify_live_daemons.py")
    assert human.returncode == 0, human.stderr[-2000:]
    assert "overall=OFFLINE" in human.stdout
    assert "SKIPPED_OFFLINE" in human.stdout
    assert "docker run" in human.stdout
    assert "postgres:16-alpine" in human.stdout
    assert "tigerbeetle" in human.stdout
    assert "nats:2.10-alpine" in human.stdout


def test_testnet_burnin_script_skips_cleanly_offline() -> None:
    completed = _run_script("testnet_burnin.py", "--json")
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout)
    assert report["skipped"] is True
    assert "not a supported burn-in target" in report["reason"]
    # A skipped report never claims a check passed.
    assert report["all_passed"] is False
    assert report["checks"] == {}

    # Opting in without credentials is still a clean skip, never a partial run.
    opted_in = _run_script("testnet_burnin.py", "--venue", "binance-testnet", "--json")
    assert opted_in.returncode == 0, opted_in.stderr[-2000:]
    optional_report = json.loads(opted_in.stdout)
    assert optional_report["skipped"] is True
    assert optional_report["venue"] == "binance-testnet"
    assert "credentials" in optional_report["reason"]
    assert optional_report["all_passed"] is False

    human = _run_script("testnet_burnin.py")
    assert human.returncode == 0, human.stderr[-2000:]
    assert "SKIPPED" in human.stdout


# ===========================================================================
# Section 8 -- tier-2 compaction and checkpoint invariance
# ===========================================================================


def _build_graph(events: int, **kwargs: Any):
    from ring2_brain.memory.entity_graph import EntityMemoryGraph

    graph = EntityMemoryGraph(**kwargs)
    for index in range(events):
        graph.record_outcome(
            outcome_kind=("WIN", "LOSS", "SLIPPAGE_SPIKE", "ORDER_REJECTED")[index % 4],
            net_pnl_micros=((index * 37) % 400 - 150) * UNITS,
            slippage_bps=float((index * 13) % 40) + 0.125,
            symbol=("BTCUSDT", "ETH-USDT", "SOLUSDT")[index % 3],
            silo="CRYPTO_BINANCE",
            strategy_id="alpha_v3",
            regime=("HIGH_VOL", "LOW_VOL")[index % 2],
            lesson_summary=f"lesson {index % 7} about gap risk on the open",
            ts_utc=1_700_000_000_000_000_000 + index * 1_000_000,
            event_id=f"E-{index:06d}",
        )
    return graph


def test_compaction_seals_a_verified_checkpoint_and_archives_events(
    tmp_path: Path,
) -> None:
    from ring2_brain.memory.entity_graph import (
        COMPACT_THRESHOLD,
        KEEP_RECENT,
        MemoryCheckpoint,
        PostMortemEvent,
    )

    assert COMPACT_THRESHOLD == 500
    assert KEEP_RECENT == 200

    archive = tmp_path / "entity_memory_cold_archive.jsonl"
    events = 620
    sealed = _build_graph(events, archive_path=archive)
    summary = sealed.seal_summary()
    checkpoint = sealed.checkpoint()

    assert isinstance(checkpoint, MemoryCheckpoint)
    assert checkpoint.verify() is True
    assert checkpoint.checkpoint_seq == summary["checkpoint_seq"] > 0
    assert checkpoint.event_count_compacted > 0
    assert summary["event_count"] == events
    assert summary["active_events"] <= COMPACT_THRESHOLD

    # The archive holds exactly the events the checkpoint sealed.
    archived = sealed.cold_archive()
    assert len(archived) == summary["archive_events"] == checkpoint.event_count_compacted
    file_rows = sealed.read_archive_file()
    assert len(file_rows) == len(archived)
    assert [row["seq"] for row in file_rows] == list(range(1, len(file_rows) + 1))
    assert archive.is_file()

    # Every sealed event is a verified, self-sealing record.
    for row in file_rows[:5]:
        event = PostMortemEvent.from_dict(row["event"])
        assert event.verify() is True

    # An explicit compaction of the hot tail is a real, verified seal that keeps
    # the total event count intact ...
    second = sealed.compact_timeline(keep_recent=KEEP_RECENT)
    assert second["compacted"] is True
    assert second["reason"] == "CHECKPOINT_SEALED"
    assert second["checkpoint_seq"] >= checkpoint.checkpoint_seq
    assert sealed.checkpoint().verify() is True
    assert sealed.seal_summary()["event_count"] == events
    # ... and a call with the timeline already inside the bound is a truthful
    # no-op, not a fake seal over nothing.
    noop = sealed.compact_timeline(keep_recent=KEEP_RECENT)
    assert noop["compacted"] is False
    assert noop["reason"] == "TIMELINE_WITHIN_HOT_BOUND"


def test_truth_sha256_is_byte_identical_across_replay_paths(tmp_path: Path) -> None:
    """full replay == checkpoint + delta == restart from ``load_state()``."""

    from ring2_brain.memory.entity_graph import EntityMemoryGraph, MemoryCheckpoint

    events = 620
    archive = tmp_path / "cold.jsonl"
    # (a) one graph that never compacts: the reference.
    full = _build_graph(events, compact_threshold=10**9, archive_path=archive)
    # (b) one graph that seals a checkpoint at 500.
    sealed = _build_graph(events, archive_path=archive)

    full_truths = {row.entity_id: row.to_dict() for row in full.truths()}
    sealed_truths = {row.entity_id: row.to_dict() for row in sealed.truths()}

    assert full_truths == sealed_truths
    assert full_truths  # something was actually compiled
    for entity_id, row in full_truths.items():
        assert row["truth_sha256"] == sealed_truths[entity_id]["truth_sha256"]
        assert _HEX64.match(row["truth_sha256"])

    # (c) a restart replays only ``seq > checkpoint_seq`` and must agree exactly.
    restored = EntityMemoryGraph(state=sealed.to_state(), archive_path=archive)
    restored_truths = {row.entity_id: row.to_dict() for row in restored.truths()}
    assert restored_truths == full_truths
    assert len(restored._events) == len(sealed._events)  # noqa: SLF001 - replay bound
    assert restored.checkpoint() is not None
    assert restored.checkpoint().verify() is True

    # The checkpoint digest commits to its own sequence, cutoff and truths.
    checkpoint = sealed.checkpoint()
    assert checkpoint.checkpoint_sha256 == MemoryCheckpoint(
        checkpoint_seq=checkpoint.checkpoint_seq,
        cutoff_ts_utc=checkpoint.cutoff_ts_utc,
        event_count_compacted=checkpoint.event_count_compacted,
        compiled_truths=checkpoint.compiled_truths,
    ).checkpoint_sha256
    assert len(set(checkpoint.truth_sha256s())) == len(checkpoint.compiled_truths)

    # A tampered checkpoint is rejected at load time instead of redefining history.
    tampered = sealed.to_state()
    tampered["checkpoint"] = {
        **dict(tampered["checkpoint"]),
        "cutoff_ts_utc": int(tampered["checkpoint"]["cutoff_ts_utc"]) + 1,
    }
    reloaded = EntityMemoryGraph(state=tampered, archive_path=archive)
    assert reloaded.checkpoint() is None


def test_compaction_is_deterministic_across_two_runs(tmp_path: Path) -> None:
    first = _build_graph(580, archive_path=tmp_path / "a.jsonl").seal_summary()
    second = _build_graph(580, archive_path=tmp_path / "b.jsonl").seal_summary()
    assert first["checkpoint_sha256"] == second["checkpoint_sha256"]
    assert first["checkpoint_seq"] == second["checkpoint_seq"]
    assert first["active_events"] == second["active_events"]


# ===========================================================================
# Section 9 -- 3-signal hybrid retrieval
# ===========================================================================


def test_three_signal_retrieval_separates_a_legacy_two_signal_tie() -> None:
    from ring2_brain.memory.entity_graph import (
        EMBED_DIMS,
        FEATURE_WEIGHT,
        LEXICAL_WEIGHT,
        SEMANTIC_WEIGHT,
        EntityMemoryGraph,
    )

    assert (LEXICAL_WEIGHT, FEATURE_WEIGHT, SEMANTIC_WEIGHT) == (0.25, 0.45, 0.30)
    assert EMBED_DIMS == 384

    graph = EntityMemoryGraph()
    # Both lessons are the same length and contain the query token exactly once,
    # so BM25 and the feature arm score them identically; the entity ids carry
    # no query token either.  Only the char-ngram arm can separate them.
    graph.record_outcome(
        outcome_kind="LOSS",
        net_pnl_micros=-1_000_000,
        symbol="LEXLOC",
        silo="US_IBKR",
        lesson_summary="auction alpha bravo charlie delta echo",
        ts_utc=1_700_000_000_000_000_000,
        event_id="E-LEX",
    )
    graph.record_outcome(
        outcome_kind="LOSS",
        net_pnl_micros=-1_000_000,
        symbol="SEMLOC",
        silo="US_IBKR",
        lesson_summary="auction slip slipstream slippy slid slide",
        ts_utc=1_700_000_000_000_000_001,
        event_id="E-SEM",
    )

    report = graph.query_entity_memory(
        query_text="auction slippage gap", top_k=5, now_ns=1_800_000_000_000_000_000
    )
    rows = {row["entity_id"]: row for row in report["truths"]}
    # ``silo:US_IBKR`` is compiled too (both events name the same silo); the two
    # symbols are the pair whose lexical scores tie exactly.
    assert {"symbol:LEXLOC", "symbol:SEMLOC"} <= set(rows)
    lexical_near = rows["symbol:LEXLOC"]
    semantic_near = rows["symbol:SEMLOC"]

    # The legacy 2-signal formula ties exactly; the 3-signal one does not.
    legacy_lexical = (
        0.30 * lexical_near["signals"]["lexical"]
        + 0.70 * lexical_near["signals"]["feature"]
    )
    legacy_semantic = (
        0.30 * semantic_near["signals"]["lexical"]
        + 0.70 * semantic_near["signals"]["feature"]
    )
    assert abs(lexical_near["signals"]["lexical"] - semantic_near["signals"]["lexical"]) < 1e-9
    assert abs(legacy_lexical - legacy_semantic) < 1e-12
    assert semantic_near["signals"]["semantic"] > lexical_near["signals"]["semantic"]

    order = [row["entity_id"] for row in report["truths"]]
    assert order.index("symbol:SEMLOC") < order.index("symbol:LEXLOC")

    # The fusion weights are reported, not implied.
    assert report["weights"] == {"lexical": 0.25, "feature": 0.45, "semantic": 0.3}
    assert report["semantic_dim"] == 384
    assert report["semantic_enabled"] is True
    for row in report["truths"]:
        signals = row["signals"]
        assert set(signals) == {"lexical", "feature", "semantic", "fused"}
        recomputed = (
            LEXICAL_WEIGHT * signals["lexical"]
            + FEATURE_WEIGHT * signals["feature"]
            + SEMANTIC_WEIGHT * signals["semantic"]
        )
        assert abs(recomputed - signals["fused"]) < 1e-6
        assert abs(recomputed - row["score"]) < 1e-6
        assert 0.0 <= rows[row["entity_id"]]["signals"]["semantic"] <= 1.0

    # Every numeric claim rides a verified GroundedFactPacket, zero ungrounded.
    from core.schemas import GroundedFactPacket

    packet = GroundedFactPacket(
        symbol=report["packet"]["symbol"],
        valid_time_ns=report["packet"]["valid_time_ns"],
        knowledge_ts_ns=report["packet"]["knowledge_ts_ns"],
        as_of_ns=report["packet"]["as_of_ns"],
        metrics=report["packet"]["metrics"],
        source_id=report["packet"]["source_id"],
        retrieval_attestation=report["packet"]["retrieval_attestation"],
    )
    assert packet.verify() is True
    assert packet.sha256_digest == report["packet_sha256"]
    for name in ("lexical", "feature", "semantic"):
        assert f"retrieval_{name}_max" in packet.metrics
        assert abs(
            packet.metrics[f"retrieval_{name}_max"]
            - max(row["signals"][name] for row in report["truths"])
        ) < 1e-9
    assert packet.metrics["retrieval_weight_lexical"] == 0.25
    assert packet.metrics["retrieval_weight_feature"] == 0.45
    assert packet.metrics["retrieval_weight_semantic"] == 0.30
    assert packet.metrics["sample_count"] == sum(
        row["sample_count"] for row in report["truths"]
    )
    # The packet carries no number the retrieval did not actually measure.
    template = graph.query_entity_memory(symbol="SEMLOC", top_k=1)
    assert template["packet"]["metrics"]["sample_count"] == 1.0


def test_semantic_retrieval_is_offline_and_deterministic() -> None:
    from ring2_brain.memory.entity_graph import (
        cosine_similarity,
        hashed_embedding,
        ollama_embedder,
    )

    first = hashed_embedding("gap risk on the open")
    second = hashed_embedding("gap risk on the open")
    assert first == second
    assert len(first) == 384
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9
    assert cosine_similarity(first, second) > 0.999
    assert cosine_similarity(first, hashed_embedding("auction tape")) < 0.999
    # The Ollama hook is dormant unless the operator opts in.
    assert ollama_embedder() is None


# ===========================================================================
# Section 10 -- episodic operator memory
# ===========================================================================


def test_episodic_store_records_all_six_action_kinds() -> None:
    from ring2_brain.memory.episodic_memory import (
        ACTION_KINDS,
        ADVERSE_ACTION_KINDS,
        EPISODE_SCHEMA_VERSION,
        MAX_EPISODES,
        EpisodicEpisode,
        EpisodicMemoryStore,
        digitless,
    )

    assert ACTION_KINDS == (
        "MISSION_OVERRIDE",
        "MISSION_REVOKE",
        "TRANCHE_REARM",
        "CLEAR_FREEZE",
        "AUTOPILOT_PAUSE",
        "AUTOPILOT_PLAY",
    )
    assert ADVERSE_ACTION_KINDS == frozenset(
        {"MISSION_REVOKE", "CLEAR_FREEZE", "AUTOPILOT_PAUSE"}
    )
    assert EPISODE_SCHEMA_VERSION == "v2.3.episodic-memory"
    assert MAX_EPISODES == 2_000

    store = EpisodicMemoryStore()
    store.record_mission_override(
        mission_id="M-1",
        tranche_caps_micros={"T1": 4_000 * UNITS},
        excluded_symbols=["btcusdt"],
        reserve_split_override_bps=7000,
        operator_note="trim the crypto leg",
        now_ns=1_700_000_000_000_000_000,
    )
    store.record_mission_revoke(
        mission_id="M-1", reason="thesis invalidated", now_ns=1_700_000_001_000_000_000
    )
    store.record_tranche_rearm(
        tranche_id="T1", mission_id="M-1", now_ns=1_700_000_002_000_000_000
    )
    store.record_clear_freeze(
        silo="US_IBKR", note="reconciled", now_ns=1_700_000_003_000_000_000
    )
    store.record_autopilot(enabled=False, now_ns=1_700_000_004_000_000_000)
    store.record_autopilot(enabled=True, now_ns=1_700_000_005_000_000_000)

    described = store.describe(limit=50)
    assert described["count"] == 6
    assert described["total"] == 6
    assert described["action_kinds"] == list(ACTION_KINDS)
    assert described["episode_schema_version"] == EPISODE_SCHEMA_VERSION
    assert set(described["counts_by_action"]) == set(ACTION_KINDS)
    assert described["episodes"][0]["action_kind"] == "AUTOPILOT_PLAY"  # newest first

    for row in described["episodes"]:
        episode = EpisodicEpisode.from_dict(row)
        assert episode.verify() is True
        assert _HEX64.match(episode.payload_sha256)
        # The stored row is exactly the PG16 ``nios_episodic_memory`` shape.
        assert set(row) == {
            "episode_id",
            "ts_utc",
            "actor",
            "action_kind",
            "regime",
            "context_tags",
            "decision_summary",
            "payload_sha256",
        }

    # Serialisation round-trips without changing a single digest.
    restored = EpisodicMemoryStore(state=store.to_state())
    assert restored.describe(limit=50)["episodes"] == described["episodes"]

    # Cautions are digit-free prose with the numbers kept in the packet.
    caution = store.cautions_for(symbol="BTCUSDT", top_k=3)
    assert caution is not None
    assert caution["digit_free"] is True
    assert not any(char.isdigit() for char in caution["narrative"])
    assert digitless("cut 12 bps now 7") == "cut bps now"
    assert digitless(caution["narrative"]) == caution["narrative"]
    assert caution["packet"]["metrics"]["episode_matches"] == float(
        len(caution["episode_ids"])
    )
    # An entity nobody has intervened on gets no invented caution.
    assert store.cautions_for(symbol="ZZZZZZ") is None


def test_episodes_endpoint_round_trips_an_operator_override(cockpit: Any) -> None:
    client, _runtime = cockpit

    before = client.get("/api/v2/memory/episodes", params={"limit": 50})
    assert before.status_code == 200, before.text
    before_body = before.json()
    assert before_body["episodes"] == []
    assert before_body["count"] == 0
    assert before_body["total"] == 0
    assert before_body["action_kinds"]
    assert _HEX64.match(before_body["payload_sha256"])

    funded = client.post(
        "/api/v2/control/ledger/fund-paper",
        json={"silo": "US_IBKR", "amount_units": 100_000.0, "vault": "LongTermCompounder"},
    )
    assert funded.status_code == 200, funded.text

    created = client.post(
        "/api/v2/missions",
        json={
            "mission_id": "M-SUITE39",
            "target_units": 100.0,
            "deadline_utc": time.time_ns() + 30 * DAY_NS,
            "risk_budget_units": 100.0,
            "operator": "sovereign",
            "silos": ["US_IBKR"],
            "notional_cap_units": 50_000.0,
            "state": "APPROVE",
        },
    )
    assert created.status_code == 200, created.text

    opened = client.post(
        "/api/v2/missions/M-SUITE39/tranches",
        json={"silo": "US_IBKR", "allocated_units": 10_000.0},
    )
    assert opened.status_code == 200, opened.text
    tranche_id = opened.json()["tranche_id"]

    approved = client.post(
        "/api/v2/missions/M-SUITE39/approve",
        json={
            "operator": "sovereign",
            "state": "APPROVE",
            "excluded_symbols": ["btcusdt"],
            "tranche_caps_micros": {tranche_id: 4_000 * UNITS},
            "reserve_split_override_bps": 7000,
            "operator_note": "Trim the crypto leg; keep the reserve floor elevated.",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval_state"] == "OVERRIDE"

    after = client.get("/api/v2/memory/episodes", params={"limit": 50})
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["count"] == 1
    assert body["total"] == 1
    episode = body["episodes"][0]
    assert episode["action_kind"] == "MISSION_OVERRIDE"
    assert episode["context_tags"]["excluded_symbols"] == ["BTCUSDT"]
    assert episode["context_tags"]["reserve_split_override_bps"] == 7000
    assert episode["context_tags"]["mission_id"] == "M-SUITE39"
    assert _HEX64.match(episode["payload_sha256"])
    assert body["counts_by_action"] == {"MISSION_OVERRIDE": 1}

    # The episode is a self-sealing immutable record, not a mutable log line.
    from ring2_brain.memory.episodic_memory import EpisodicEpisode

    assert EpisodicEpisode.from_dict(episode).verify() is True

    # -- filters ------------------------------------------------------------
    matching = client.get(
        "/api/v2/memory/episodes", params={"action_kind": "MISSION_OVERRIDE"}
    )
    assert matching.status_code == 200
    assert matching.json()["count"] == 1

    other_kind = client.get(
        "/api/v2/memory/episodes", params={"action_kind": "MISSION_REVOKE"}
    )
    assert other_kind.status_code == 200
    assert other_kind.json()["count"] == 0

    by_text = client.get("/api/v2/memory/episodes", params={"q": "crypto leg"})
    assert by_text.status_code == 200
    assert by_text.json()["count"] == 1

    no_text = client.get("/api/v2/memory/episodes", params={"q": "zzz-not-present"})
    assert no_text.status_code == 200
    assert no_text.json()["count"] == 0

    # -- limits -------------------------------------------------------------
    # The store clamps the ceiling to 500 and the floor to 1, and treats ``0``
    # as "limit not supplied" (the documented default of 50).  The HTTP route
    # documents the same 1..500 bound by rejecting out-of-range queries instead
    # of silently widening them.
    store = _runtime.control.episodic_memory
    assert store.describe(limit=0)["limit"] == 50
    assert store.describe(limit=-5)["limit"] == 1
    assert store.describe(limit=9999)["limit"] == 500
    assert client.get("/api/v2/memory/episodes", params={"limit": 0}).status_code == 422
    assert client.get("/api/v2/memory/episodes", params={"limit": 9999}).status_code == 422
    ceiling = client.get("/api/v2/memory/episodes", params={"limit": 500})
    assert ceiling.status_code == 200
    assert ceiling.json()["limit"] == 500
    floor = client.get("/api/v2/memory/episodes", params={"limit": 1})
    assert floor.status_code == 200
    assert floor.json()["limit"] == 1

    # -- compaction ---------------------------------------------------------
    for index, kind in enumerate(("LOSS", "SLIPPAGE_SPIKE", "WIN", "ORDER_REJECTED", "LOSS")):
        recorded = client.post(
            "/api/v2/memory/entities",
            json={
                "outcome_kind": kind,
                "net_pnl_units": -1.0 if kind != "WIN" else 2.0,
                "slippage_bps": 31.0 if kind == "SLIPPAGE_SPIKE" else 4.0,
                "symbol": "BTCUSDT",
                "silo": "CRYPTO_BINANCE",
                "regime": "NEUTRAL",
                "lesson_summary": f"suite39 event {index}",
            },
        )
        assert recorded.status_code == 200, recorded.text

    compacted = client.post("/api/v2/memory/compact", json={"keep_recent": 2})
    assert compacted.status_code == 200, compacted.text
    compact_body = compacted.json()
    assert compact_body["page"] == "memory.compact"
    assert compact_body["reason"] == "CHECKPOINT_SEALED"
    assert compact_body["compacted"] is True
    assert compact_body["event_count_compacted"] == 3
    assert compact_body["active_events"] == 2
    checkpoint = compact_body["checkpoint"]
    assert _HEX64.match(checkpoint["checkpoint_sha256"])
    assert compact_body["checkpoint_sha256"] == checkpoint["checkpoint_sha256"]

    from ring2_brain.memory.entity_graph import MemoryCheckpoint

    assert MemoryCheckpoint.from_dict(checkpoint).verify() is True
    assert compact_body["episodic_memory"]["remaining_episodes"] == 1
    assert _HEX64.match(compact_body["payload_sha256"])

    # Compaction never loses the operator's decision.
    final = client.get("/api/v2/memory/episodes", params={"limit": 50}).json()
    assert final["total"] == 1
    assert final["episodes"][0]["action_kind"] == "MISSION_OVERRIDE"


def test_episodic_cautions_reach_the_dispatcher_digit_free() -> None:
    from ring2_brain.agents.task_dispatcher import AgentTaskDispatcher
    from ring2_brain.memory.episodic_memory import EpisodicMemoryStore

    store = EpisodicMemoryStore()
    store.record_mission_override(
        mission_id="M-CAUTION",
        excluded_symbols=["BTCUSDT"],
        reserve_split_override_bps=7000,
        operator_note="cut size after the 12 bps slippage print",
        now_ns=1_700_000_000_000_000_000,
    )
    store.record_autopilot(enabled=False, now_ns=1_700_000_001_000_000_000)

    dispatcher = AgentTaskDispatcher()
    cautions = store.inject_cautions(
        dispatcher,
        symbol="BTCUSDT",
        silo="CRYPTO_BINANCE",
        query_text="btcusdt momentum",
        now_ns=1_700_000_100_000_000_000,
    )
    assert getattr(dispatcher, "episodic_memory", None) is store
    assert cautions is not None
    narrative = cautions["narrative"]
    assert narrative
    assert not any(char.isdigit() for char in narrative)
    assert cautions["digit_free"] is True
    assert _HEX64.match(cautions["packet_sha256"])
    assert cautions["packet"]["metrics"]["episode_matches"] == float(
        len(cautions["episode_ids"])
    )
    assert cautions["action_kinds"] == ["MISSION_OVERRIDE"]


# ===========================================================================
# Section 10b -- session + CSRF guards, and a restart that proves the database
# ===========================================================================


def test_compaction_endpoint_costs_a_session_and_a_csrf_double_submit(
    tmp_path: Path,
) -> None:
    """``POST /api/v2/memory/compact`` is a guarded, state-changing POST.

    The guards compose in the documented order: no session at all -> ``401``; a
    cookie session without the signed double submit -> ``403``; a cookie session
    that echoes ``X-NIOS-CSRF`` -> ``200`` with a ``memory.compact`` payload; and
    the ``X-NIOS-Session`` header transport is exempt, because a cross-site form
    post cannot set that header.
    """

    from ring2_brain.memory.entity_graph import MemoryCheckpoint
    from ring3_cockpit.gateway.v2_1_control import CSRF_HEADER, csrf_token_for

    runtime, client = _fresh_runtime(tmp_path / "csrf-compact")
    try:
        with client:
            created = client.post(
                "/api/v2/auth/setup",
                json={
                    "username": "sovereign",
                    "password": "correct-horse-battery",
                    "live_pin": "7314",
                },
            )
            assert created.status_code == 200, created.text
            token = created.json()["token"]
            assert token
            session = {"X-NIOS-Session": token}

            for index in range(5):
                seeded = client.post(
                    "/api/v2/memory/entities",
                    json={
                        "outcome_kind": "LOSS",
                        "net_pnl_units": -1.0,
                        "slippage_bps": 4.0,
                        "symbol": "BTCUSDT",
                        "silo": "CRYPTO_BINANCE",
                        "regime": "NEUTRAL",
                        "lesson_summary": f"csrf seed {index}",
                    },
                    headers=session,
                )
                assert seeded.status_code == 200, seeded.text

            client.cookies.clear()
            anonymous = client.post("/api/v2/memory/compact", json={"keep_recent": 2})
            assert anonymous.status_code == 401
            assert anonymous.json()["detail"] == "AUTH_REQUIRED"

            client.cookies.set("nios_session", token)
            try:
                blocked = client.post(
                    "/api/v2/memory/compact", json={"keep_recent": 2}
                )
                assert blocked.status_code == 403
                assert blocked.json()["detail"] == "CSRF_TOKEN_MISSING_OR_INVALID"
                assert blocked.json()["csrf_header"] == CSRF_HEADER

                accepted = client.post(
                    "/api/v2/memory/compact",
                    json={"keep_recent": 2},
                    headers={CSRF_HEADER: csrf_token_for(token)},
                )
                assert accepted.status_code == 200, accepted.text
                body = accepted.json()
            finally:
                client.cookies.clear()

            assert body["page"] == "memory.compact"
            assert body["compacted"] is True
            assert body["event_count_compacted"] == 3
            assert body["active_events"] == 2
            assert _HEX64.match(body["checkpoint_sha256"])
            assert MemoryCheckpoint.from_dict(body["checkpoint"]).verify() is True
            assert _HEX64.match(body["payload_sha256"])

            # The header transport is exempt from the double submit by design.
            assert (
                client.post(
                    "/api/v2/memory/compact", json={"keep_recent": 2}, headers=session
                ).status_code
                == 200
            )
    finally:
        try:
            runtime.control.reset_workspace()
        except Exception:  # pragma: no cover - teardown is best effort
            pass
        closer = getattr(runtime, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover - teardown is best effort
                pass


def test_a_brand_new_runtime_rehydrates_the_identical_wal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WAL database -- not the process -- owns the state.

    A second orchestrator over the *same workspace* must reproduce
    ``state_digest`` byte-for-byte, restore the operator episode with a truthful
    ``journal_episodes`` receipt, and -- after a clean slate -- come back up
    reporting zero restored episodes instead of a stale count.
    """

    from core.orchestrator import NIOSCoreOrchestrator
    from ring0_vault.ibor.state_journal import journal_path, state_digest

    root = tmp_path / "rehydrate"
    document = root / "journal" / "nios_workstation_journal.json"
    monkeypatch.setenv("NIOS_JOURNAL_PATH", str(document))

    runtime, client = _fresh_runtime(root)
    restarted = None
    emptied = None
    try:
        with client:
            created = client.post(
                "/api/v2/auth/setup",
                json={
                    "username": "sovereign",
                    "password": "correct-horse-battery",
                    "live_pin": "7314",
                },
            )
            assert created.status_code == 200, created.text
            session = {"X-NIOS-Session": created.json()["token"]}

            for index, kind in enumerate(("LOSS", "SLIPPAGE_SPIKE", "WIN")):
                posted = client.post(
                    "/api/v2/memory/entities",
                    json={
                        "outcome_kind": kind,
                        "net_pnl_units": 2.0 if kind == "WIN" else -1.0,
                        "slippage_bps": 31.0 if kind == "SLIPPAGE_SPIKE" else 4.0,
                        "symbol": ("BTCUSDT", "ETHUSDT", "SOLUSDT")[index],
                        "silo": "CRYPTO_BINANCE",
                        "regime": "NEUTRAL",
                        "lesson_summary": f"rehydrate seed {index}",
                    },
                    headers=session,
                )
                assert posted.status_code == 200, posted.text
            toggled = client.post(
                "/api/v2/control/workspace/autopilot",
                json={"enabled": False},
                headers=session,
            )
            assert toggled.status_code == 200, toggled.text

            snapshot = runtime.control.journal_snapshot()
            assert Path(snapshot["journal_path"]) == journal_path() == document
            assert snapshot["storage"] == "sqlite-wal"
            assert snapshot["exists"] is True
            assert snapshot["database_path"] == str(wal_database_path(document))
            assert Path(snapshot["database_path"]).is_file()

            before = runtime.control.state_journal.load()
            assert before["episodic_memory"]["episode_count"] == 1
            # The kv section is authoritative, and the structured table is its
            # queryable mirror: a restart scan can read operator decisions (and
            # a backup can export them) without decoding a blob.
            wal = runtime.control.state_journal.store
            assert wal.table_counts()["nios_episodic_memory_local"] == 1
            assert [row["action_kind"] for row in wal.read_episodes()] == [
                "AUTOPILOT_PAUSE"
            ]
            truth_ids = [row.entity_id for row in runtime.control.entity_memory.truths()]
            assert truth_ids

        restarted = NIOSCoreOrchestrator(
            workspace=root / "workspace",
            key_vault_root=root / "vault",
            clean_slate=False,
        )
        after = restarted.control.state_journal.load()
        assert state_digest(after) == state_digest(before)
        assert after["entity_memory"] == before["entity_memory"]
        assert after["episodic_memory"] == before["episodic_memory"]

        episodes = restarted.control.memory_episodes()
        assert episodes["total"] == 1
        assert episodes["journal_episodes"] == 1
        assert episodes["episodes"][0]["action_kind"] == "AUTOPILOT_PAUSE"
        assert [row.entity_id for row in restarted.control.entity_memory.truths()] == (
            truth_ids
        )
        assert all(
            _HEX64.match(row.truth_sha256)
            for row in restarted.control.entity_memory.truths()
        )

        # A clean slate zeroes the receipt too: the next boot must not claim a
        # single restored episode.
        restarted.control.reset_workspace()
        emptied = NIOSCoreOrchestrator(
            workspace=root / "workspace",
            key_vault_root=root / "vault",
            clean_slate=False,
        )
        assert emptied.control.memory_episodes()["journal_episodes"] == 0
        assert emptied.control.memory_episodes()["total"] == 0
        assert emptied.control.entity_memory.truths() == ()
        emptied_wal = emptied.control.state_journal.store
        assert emptied_wal.table_counts()["nios_episodic_memory_local"] == 0
        assert emptied_wal.read_episodes() == ()
    finally:
        for instance in (emptied, restarted, runtime):
            if instance is None:
                continue
            try:
                instance.control.reset_workspace()
            except Exception:  # pragma: no cover - teardown is best effort
                pass
            closer = getattr(instance, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - teardown is best effort
                    pass


# ===========================================================================
# Section 11 -- ring-2 air gap
# ===========================================================================


def test_memory_package_exports_the_v23_surface() -> None:
    import ring2_brain.memory as memory

    required = {
        "COMPACT_THRESHOLD",
        "EMBED_DIMS",
        "FEATURE_WEIGHT",
        "KEEP_RECENT",
        "LEXICAL_WEIGHT",
        "MAX_COLD_ARCHIVE_EVENTS",
        "SEMANTIC_WEIGHT",
        "MemoryCheckpoint",
        "cosine_similarity",
        "hashed_embedding",
        "ACTION_KINDS",
        "ADVERSE_ACTION_KINDS",
        "EPISODE_SCHEMA_VERSION",
        "MAX_EPISODES",
        "EpisodicEpisode",
        "EpisodicMemoryStore",
        "default_episodic_store",
        "digitless",
    }
    assert required <= set(memory.__all__)
    for name in required:
        assert hasattr(memory, name), name
    assert memory.COMPACT_THRESHOLD == 500
    assert memory.KEEP_RECENT == 200
    assert memory.EMBED_DIMS == 384
    assert memory.MAX_COLD_ARCHIVE_EVENTS == 20_000
    assert memory.MAX_EPISODES == 2_000


def test_ring2_memory_never_imports_ring0_signing_or_socket_modules() -> None:
    """Law 1: the memory tier may not name Ring 0 keys, sockets or signing."""

    forbidden = (
        "ring0_vault.silos.key_vault",
        "ring0_vault.ibor.state_journal",
        "broker_credential",
        "order_signer",
        "OrderSigner",
        "ccxt",
        "uvicorn",
    )
    for name in ("__init__", "working_memory", "entity_graph", "episodic_memory"):
        text = (ROOT / "ring2_brain" / "memory" / f"{name}.py").read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{name}.py references {token}"


# ===========================================================================
# Section 12 -- suite inventory
# ===========================================================================


def test_suite39_belongs_to_the_forty_suite_release() -> None:
    suites = sorted(path.name for path in (ROOT / "tests").glob("test_*.py"))
    assert len(suites) == 41, suites
    assert SUITE_NAME in suites
    assert "test_v2_3_telemetry_closure_and_ui_split.py" in suites
    assert (ROOT / "tests" / "conftest.py").is_file()


def test_release_documents_advertise_forty_suites() -> None:
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "41 suites" in runbook
    assert "41 suites" in readme
    assert "v4.3.0-V2.3" in runbook
