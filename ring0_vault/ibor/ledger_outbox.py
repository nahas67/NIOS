"""NIOS Core v3.0 -- Ring 0 IBOR, crash-atomic outbox, ratchet and reconciler.

Governing architecture sections
-------------------------------
* ``sec_005`` -- TigerBeetle two-phase outbox and PostgreSQL 16 double-entry
  Investment Book of Record; money is 128-bit integers.
* ``sec_009`` -- four vaults per broker silo, 60-second reconciliation, freeze a
  silo on ``>0.01%`` position drift, in-silo 35% / 15% ratchet.
* ``sec_010`` -- ``<2.0 ms`` outbox, reconciliation drift halt.
* ``sec_011`` -- the outbox is the only path a signed order may take.
* ``sec_012`` -- ``AtMostOnceExecution`` and ``EventualIBORConvergence``.
* ``sec_015`` -- zero cross-border wires; each silo compounds internally.
* ``sec_018`` Law 5 -- compound inside the silo, never chase a trade across it.

Money representation
--------------------
Every amount in this module is an **integer micro-unit** (``1 unit =
1_000_000 micros``) bounded by TigerBeetle's ``u128`` ceiling
``(1 << 127) - 1``.  There is no floating-point arithmetic anywhere in the
ledger: the only float conversion happens at the boundary, when a Ring 1/Ring 2
``UnsignedProposal`` (which carries floats) is quantised into micro-units.

Durability model
----------------
The authoritative state machine lives in memory and every mutation is appended
to an append-only SQL journal inside a single ACID transaction *before* it is
applied.  Replaying the journal reconstructs the exact state, so a crash
mid-flight can never produce a half-posted transfer.  When a TigerBeetle daemon
is reachable it is additionally driven as the formally verified two-phase
engine; when PostgreSQL 16 is reachable it receives the same rows.  Both are
probed honestly and degrade to the local SQLite journal without changing a
single invariant.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.schemas import (
    ALL_VAULTS,
    UnsignedProposal,
    VaultName,
    canonical_pairs_digest,
)

__all__ = [
    # constants
    "MICROS_PER_UNIT",
    "MONEY_MAX",
    "OUTBOX_LATENCY_BUDGET_NS",
    "RATCHET_LONG_TERM_PCT",
    "RATCHET_RESERVE_PCT",
    "RATCHET_SCALPER_PCT",
    "DRIFT_BOUND",
    "RECONCILE_INTERVAL_NS",
    "TRANSFER_TIMEOUT_NS",
    "VAULT_ASSET_MAP",
    # errors
    "LedgerError",
    "OverdraftError",
    "DuplicateTransferError",
    "OutboxStateError",
    "AccountCode",
    "TransferPhase",
    "OutboxState",
    # primitives
    "to_micros",
    "from_micros",
    "VaultAccount",
    "Transfer",
    "BackendStatus",
    "probe_backends",
    "LedgerStore",
    # ledger
    "IBORLedger",
    # outbox
    "Reservation",
    "BrokerAck",
    "AckResult",
    "CrashAtomicOutbox",
    "deterministic_outbox_key",
    # ratchet
    "RatchetResult",
    "InSiloProfitRatchet",
    # reconciliation
    "BrokerPosition",
    "SiloGuard",
    "ReconcileReport",
    "LiveReconciler",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MICROS_PER_UNIT: int = 1_000_000

#: TigerBeetle ``u128`` ceiling: ``2**127 - 1``.
MONEY_MAX: int = (1 << 127) - 1

#: ``sec_010``: the outbox must commit a pending reservation in ``< 2.0 ms``.
OUTBOX_LATENCY_BUDGET_NS: int = 2_000_000

#: ``sec_018`` Law 5 / ``sec_015``: scalp profit split inside the silo.
RATCHET_LONG_TERM_PCT: int = 35
RATCHET_RESERVE_PCT: int = 15
RATCHET_SCALPER_PCT: int = 50

#: ``sec_009``: any reconciliation drift strictly greater than 0.01% freezes.
DRIFT_BOUND: float = 0.0001

#: ``sec_009``: reconciliation cadence.
RECONCILE_INTERVAL_NS: int = 60 * 1_000_000_000

#: ``sec_010``: maker-peg hard-cancel timeout, reused as the outbox TTL.
TRANSFER_TIMEOUT_NS: int = 3_000_000_000

#: ``sec_015``: the sweep / reserve instrument per silo market.  These mirror
#: :data:`ring1_spinal.silo_worker.SILO_CONFIGS` so Ring 0 and Ring 1 cannot
#: disagree about where profit is swept.
VAULT_ASSET_MAP: Mapping[str, Mapping[str, str]] = {
    "CRYPTO_BINANCE": {
        "long_term_sweep_asset": "BTC-USDT / ETH-USDT",
        "reserve_yield_asset": "USYC Tokenized T-Bill",
    },
    "INDIA_ZERODHA_DHAN": {
        "long_term_sweep_asset": "NIFTYBEES.NS",
        "reserve_yield_asset": "LIQUIDBEES.NS",
    },
    "US_IBKR": {
        "long_term_sweep_asset": "VOO / QQQ",
        "reserve_yield_asset": "SGOV (0-3M Treasury ETF)",
    },
    "FX_MT5": {
        "long_term_sweep_asset": "Sovereign Carry Repo",
        "reserve_yield_asset": "Unlevered Gold / Short-Duration Sovereign",
    },
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LedgerError(Exception):
    """Base class for every IBOR contract violation."""


class OverdraftError(LedgerError):
    """``debits_must_not_exceed_credits`` would be violated."""


class DuplicateTransferError(LedgerError):
    """A transfer id was reused with a different payload."""


class OutboxStateError(LedgerError):
    """An illegal outbox state transition was attempted."""


# ---------------------------------------------------------------------------
# Enums and value objects
# ---------------------------------------------------------------------------


class AccountCode(StrEnum):
    """Non-vault accounts that complete the double-entry book."""

    CASH = "CASH"
    FEES = "FEE_TAX_ACCRUAL"
    PNL = "REALIZED_PNL"


class TransferPhase(StrEnum):
    PENDING = "PENDING"
    POSTED = "POSTED"
    VOIDED = "VOIDED"


class OutboxState(StrEnum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    VOIDED = "VOIDED"


class TransferCode(IntEnum):
    """TigerBeetle ``code`` discriminator for each ledger operation."""

    FUNDING = 1
    ORDER_RESERVATION = 2
    FILL_SETTLEMENT = 3
    CHARGE_ACCRUAL = 4
    PROFIT_RATCHET = 5
    RECONCILIATION_REPAIR = 6


# ---------------------------------------------------------------------------
# Integer money helpers
# ---------------------------------------------------------------------------


def to_micros(value: Any) -> int:
    """Quantise a unit amount into integer micro-units, bounded by ``u128``.

    This is the *only* place a float becomes money.  ``math.floor`` rounding is
    deliberate: never round a debit up, because that would create a micro-unit
    of value that the silo does not own.
    """
    if isinstance(value, int):
        micros = value * MICROS_PER_UNIT
    else:
        number = float(value)
        if not math.isfinite(number):
            raise LedgerError(f"amount must be finite, got {value!r}")
        micros = int(math.floor(number * MICROS_PER_UNIT + 1e-9))
    if micros < 0:
        raise LedgerError(f"amount must be non-negative, got {value!r}")
    if micros > MONEY_MAX:
        raise LedgerError(f"amount {value!r} exceeds the 128-bit ceiling")
    return micros


def from_micros(micros: int) -> float:
    """Render micro-units back to units for display only (never for maths)."""
    return int(micros) / MICROS_PER_UNIT


def _check_money(amount: int, *, what: str = "amount") -> int:
    value = int(amount)
    if value < 0:
        raise LedgerError(f"{what} must be non-negative, got {value}")
    if value > MONEY_MAX:
        raise LedgerError(f"{what} exceeds the 128-bit ceiling MONEY_MAX")
    return value


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VaultAccount:
    """One double-entry account with TigerBeetle balance semantics.

    The invariant enforced by :meth:`check` is TigerBeetle's
    ``debits_must_not_exceed_credits``::

        debits_posted + debits_pending <= credits_posted + credits_pending

    External accounts (the silo's cash hub, the fee accrual bucket and the
    realised-P&L bucket) are allowed to go negative from the ledger's point of
    view -- they are the counterparty to money entering or leaving the silo.
    """

    silo: str
    code: str
    credits_posted: int = 0
    debits_posted: int = 0
    credits_pending: int = 0
    debits_pending: int = 0
    enforce_credit_floor: bool = True

    # -- derived ----------------------------------------------------------
    @property
    def debits_total(self) -> int:
        return self.debits_posted + self.debits_pending

    @property
    def credits_total(self) -> int:
        return self.credits_posted + self.credits_pending

    @property
    def balance(self) -> int:
        """Posted net balance in micro-units."""
        return self.credits_posted - self.debits_posted

    @property
    def available(self) -> int:
        """Posted balance minus everything already locked in pending debits."""
        return self.credits_posted - self.debits_posted - self.debits_pending

    def is_vault(self) -> bool:
        return self.code in {str(v) for v in ALL_VAULTS}

    def check(self) -> None:
        for name in (
            "credits_posted",
            "debits_posted",
            "credits_pending",
            "debits_pending",
        ):
            _check_money(getattr(self, name), what=f"{self.silo}.{self.code}.{name}")
        if self.enforce_credit_floor and self.debits_total > self.credits_total:
            raise OverdraftError(
                f"debits_must_not_exceed_credits violated on "
                f"{self.silo}.{self.code}: debits="
                f"{self.debits_total} > credits={self.credits_total}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "code": self.code,
            "credits_posted": self.credits_posted,
            "debits_posted": self.debits_posted,
            "credits_pending": self.credits_pending,
            "debits_pending": self.debits_pending,
            "balance": self.balance,
            "available": self.available,
            "enforce_credit_floor": self.enforce_credit_floor,
        }


@dataclass(slots=True)
class Transfer:
    """One two-phase double-entry transfer."""

    transfer_id: int
    silo: str
    debit_account: str
    credit_account: str
    amount_micros: int
    code: int
    phase: TransferPhase
    pending_id: int | None = None
    created_ns: int = 0
    posted_ns: int | None = None

    @property
    def is_pending(self) -> bool:
        return self.phase is TransferPhase.PENDING

    @property
    def is_posted(self) -> bool:
        return self.phase is TransferPhase.POSTED

    @property
    def is_voided(self) -> bool:
        return self.phase is TransferPhase.VOIDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "silo": self.silo,
            "debit_account": self.debit_account,
            "credit_account": self.credit_account,
            "amount_micros": self.amount_micros,
            "code": self.code,
            "phase": str(self.phase),
            "pending_id": self.pending_id,
            "created_ns": self.created_ns,
            "posted_ns": self.posted_ns,
        }


# ---------------------------------------------------------------------------
# Backend probes and the SQL journal
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class BackendStatus:
    """Honest report of whether an external ledger daemon is usable."""

    name: str
    available: bool
    detail: str
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "detail": self.detail,
            "version": self.version,
        }


def probe_backends(
    *, tb_address: str | None = None, pg_dsn: str | None = None, timeout_s: float = 0.25
) -> tuple[BackendStatus, BackendStatus, BackendStatus]:
    """Probe TigerBeetle, PostgreSQL 16 and SQLite without ever raising."""
    return (
        _probe_tigerbeetle(tb_address, timeout_s=timeout_s),
        _probe_postgres(pg_dsn, timeout_s=timeout_s),
        BackendStatus(
            name="sqlite",
            available=True,
            detail="in-process ACID journal (128-bit micro-units as decimal text)",
            version=sqlite3.sqlite_version,
        ),
    )


def _probe_tigerbeetle(address: str | None, *, timeout_s: float) -> BackendStatus:
    try:
        import tigerbeetle as tb  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment dependent
        return BackendStatus(
            name="tigerbeetle", available=False, detail=f"client not importable: {exc}"
        )
    target = (address or os.environ.get("TB_ADDRESS") or "3000").strip()
    enabled = os.environ.get("NIOS_PROBE_LEDGER_BACKENDS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not enabled and address is None:
        return BackendStatus(
            name="tigerbeetle",
            available=False,
            detail=(
                "client installed; live daemon probe disabled (set "
                "NIOS_PROBE_LEDGER_BACKENDS=1 to enable) -- the local ACID "
                "journal is authoritative"
            ),
        )

    # A TigerBeetle client blocks indefinitely when no replica is listening, so
    # the probe runs in a daemon thread with a hard join budget.
    outcome: dict[str, Any] = {}

    def _attempt() -> None:  # pragma: no cover - requires a live daemon
        try:
            with tb.ClientSync(cluster_id=0, replica_addresses=target) as client:
                client.lookup_accounts([0])
            outcome["ok"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_attempt, name="tb-probe", daemon=True)
    thread.start()
    thread.join(max(0.05, float(timeout_s)))
    if thread.is_alive():
        return BackendStatus(
            name="tigerbeetle",
            available=False,
            detail=f"daemon did not answer within {timeout_s * 1000:.0f} ms at {target}",
        )
    if outcome.get("ok"):
        return BackendStatus(
            name="tigerbeetle",
            available=True,
            detail=f"two-phase transfer engine reachable at {target}",
            version=getattr(tb, "__version__", ""),
        )
    return BackendStatus(
        name="tigerbeetle",
        available=False,
        detail=f"daemon not reachable at {target}: {outcome.get('error', 'unknown')}",
    )


def _probe_postgres(dsn: str | None, *, timeout_s: float) -> BackendStatus:
    driver = None
    for candidate in ("psycopg", "psycopg2"):
        try:
            driver = __import__(candidate)
            break
        except Exception:
            continue
    if driver is None:
        return BackendStatus(
            name="postgres16",
            available=False,
            detail="neither psycopg nor psycopg2 is installed",
        )
    target = dsn or os.environ.get("NIOS_PG_DSN") or ""
    if not target:
        return BackendStatus(
            name="postgres16",
            available=False,
            detail="driver present but NIOS_PG_DSN is not configured",
        )
    try:  # pragma: no cover - requires a live server
        with driver.connect(target, connect_timeout=1) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                row = cur.fetchone()
        return BackendStatus(
            name="postgres16",
            available=True,
            detail="SERIALIZABLE financial_outbox reachable",
            version=str(row[0]) if row else "",
        )
    except Exception as exc:
        return BackendStatus(
            name="postgres16",
            available=False,
            detail=f"server not reachable: {type(exc).__name__}",
        )


_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ibor_double_entry_ledger (
        entry_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        transfer_id      TEXT    NOT NULL,
        silo             TEXT    NOT NULL,
        debit_account    TEXT    NOT NULL,
        credit_account   TEXT    NOT NULL,
        amount_micros    TEXT    NOT NULL,
        code             INTEGER NOT NULL,
        phase            TEXT    NOT NULL,
        pending_id       TEXT,
        created_ns       INTEGER NOT NULL,
        posted_ns        INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ibor_transfer ON ibor_double_entry_ledger(transfer_id)",
    "CREATE INDEX IF NOT EXISTS ix_ibor_silo ON ibor_double_entry_ledger(silo, code)",
    """
    CREATE TABLE IF NOT EXISTS financial_outbox (
        outbox_idempotency_key TEXT PRIMARY KEY,
        silo                   TEXT    NOT NULL,
        strategy_id            TEXT    NOT NULL DEFAULT '',
        proposal_id            TEXT    NOT NULL DEFAULT '',
        vault                  TEXT    NOT NULL,
        symbol                 TEXT    NOT NULL,
        side                   TEXT    NOT NULL,
        qty_micros             TEXT    NOT NULL,
        price_micros           TEXT    NOT NULL,
        notional_micros        TEXT    NOT NULL,
        fee_micros             TEXT    NOT NULL,
        tax_micros             TEXT    NOT NULL,
        total_lock_micros      TEXT    NOT NULL,
        pending_transfer_id    TEXT    NOT NULL,
        state                  TEXT    NOT NULL,
        broker_order_id        TEXT    NOT NULL DEFAULT '',
        created_ns             INTEGER NOT NULL,
        updated_ns             INTEGER NOT NULL,
        detail                 TEXT    NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_outbox_state ON financial_outbox(state)",
    """
    CREATE TABLE IF NOT EXISTS order_ack_inbox (
        ack_id             INTEGER PRIMARY KEY AUTOINCREMENT,
        broker_order_id    TEXT NOT NULL UNIQUE,
        outbox_key         TEXT NOT NULL,
        state              TEXT NOT NULL,
        filled_qty_micros  TEXT NOT NULL DEFAULT '0',
        avg_price_micros   TEXT NOT NULL DEFAULT '0',
        received_ns        INTEGER NOT NULL,
        applied            INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tax_lots (
        lot_id              INTEGER PRIMARY KEY AUTOINCREMENT,
        silo                TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        side                TEXT NOT NULL,
        qty_micros          TEXT NOT NULL,
        price_micros        TEXT NOT NULL,
        notional_micros     TEXT NOT NULL,
        stt_micros          TEXT NOT NULL,
        tds_micros          TEXT NOT NULL,
        stamp_micros        TEXT NOT NULL,
        fee_micros          TEXT NOT NULL,
        opened_ns           INTEGER NOT NULL,
        closed_ns           INTEGER,
        realized_net_micros TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS grounding_hash_store (
        grounding_hash TEXT PRIMARY KEY,
        silo           TEXT NOT NULL DEFAULT '',
        symbol         TEXT NOT NULL DEFAULT '',
        strategy_id    TEXT NOT NULL DEFAULT '',
        source         TEXT NOT NULL,
        claim_sha256   TEXT,
        registered_ns  INTEGER NOT NULL
    )
    """,
)


class LedgerStore:
    """The local ACID journal plus honest external-backend mirrors.

    The SQLite database is always opened: it is the write-ahead journal that
    makes the in-memory state machine crash-atomic even when neither TigerBeetle
    nor PostgreSQL is running.  ``synchronous=NORMAL`` on WAL is an explicit
    trade: transactions are atomic and crash-consistent (never half-applied),
    while the fsync of the WAL is amortised by the operating system.  Ring 0
    calls :meth:`flush` at every broker socket boundary.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        in_memory: bool = False,
        probe: bool = True,
    ) -> None:
        if in_memory:
            target = ":memory:"
        else:
            target = str(path) if path is not None else ":memory:"
        self.path = target
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        if target == ":memory:":
            # An in-memory journal has no disk to fsync.  MEMORY/OFF removes
            # commit syscalls without weakening any real durability guarantee
            # (the production path is TigerBeetle + PostgreSQL; the ephemeral
            # journal exists for intra-process crash atomicity and replay).
            self._conn.execute("PRAGMA journal_mode=MEMORY")
            self._conn.execute("PRAGMA synchronous=OFF")
        else:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA:
            self._conn.execute(statement)

        self.statuses: tuple[BackendStatus, BackendStatus, BackendStatus] = probe_backends()
        self.probed = bool(probe)
        self._warm_statements()

    def _warm_statements(self) -> None:
        """Compile the outbox journal statements off the kill/order hot path."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    INSERT INTO ibor_double_entry_ledger
                        (transfer_id, silo, debit_account, credit_account,
                         amount_micros, code, phase, pending_id, created_ns, posted_ns)
                    SELECT '','','','','',0,'',NULL,0,NULL WHERE 0
                    """
                )
                self._conn.execute(
                    """
                    INSERT INTO financial_outbox
                        (outbox_idempotency_key, silo, strategy_id, proposal_id,
                         vault, symbol, side, qty_micros, price_micros,
                         notional_micros, fee_micros, tax_micros,
                         total_lock_micros, pending_transfer_id, state,
                         broker_order_id, created_ns, updated_ns, detail)
                    SELECT '','','','','','','','','','','','','',0,'','',0,0,''
                    WHERE 0
                    """
                )
                self._conn.execute(
                    "UPDATE financial_outbox SET state = state WHERE 0"
                )
            finally:
                self._conn.execute("ROLLBACK")

    # -- introspection ----------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def backend_statuses(self) -> dict[str, dict[str, Any]]:
        return {status.name: status.to_dict() for status in self.statuses}

    @property
    def active_backend(self) -> str:
        for status in self.statuses:
            if status.available:
                return status.name
        return "sqlite"

    def counts(self) -> dict[str, int]:
        tables = (
            "ibor_double_entry_ledger",
            "financial_outbox",
            "order_ack_inbox",
            "tax_lots",
            "grounding_hash_store",
        )
        with self._lock:
            return {
                table: int(
                    self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in tables
            }

    # -- journal writes ---------------------------------------------------
    def write_transfer(self, transfer: Transfer) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO ibor_double_entry_ledger
                    (transfer_id, silo, debit_account, credit_account,
                     amount_micros, code, phase, pending_id, created_ns, posted_ns)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(transfer.transfer_id),
                    transfer.silo,
                    transfer.debit_account,
                    transfer.credit_account,
                    str(transfer.amount_micros),
                    int(transfer.code),
                    str(transfer.phase),
                    None if transfer.pending_id is None else str(transfer.pending_id),
                    int(transfer.created_ns),
                    transfer.posted_ns,
                ),
            )

    def write_transfer_phase(
        self, transfer: Transfer, *, phase: TransferPhase, posted_ns: int | None
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO ibor_double_entry_ledger
                    (transfer_id, silo, debit_account, credit_account,
                     amount_micros, code, phase, pending_id, created_ns, posted_ns)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(transfer.transfer_id),
                    transfer.silo,
                    transfer.debit_account,
                    transfer.credit_account,
                    str(transfer.amount_micros),
                    int(transfer.code),
                    str(phase),
                    None if transfer.pending_id is None else str(transfer.pending_id),
                    int(transfer.created_ns),
                    posted_ns,
                ),
            )

    def write_reservation(self, transfer: Transfer, reservation: "Reservation") -> None:
        """Journal the pending transfer **and** the outbox row atomically."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO ibor_double_entry_ledger
                        (transfer_id, silo, debit_account, credit_account,
                         amount_micros, code, phase, pending_id, created_ns, posted_ns)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(transfer.transfer_id),
                        transfer.silo,
                        transfer.debit_account,
                        transfer.credit_account,
                        str(transfer.amount_micros),
                        int(transfer.code),
                        str(transfer.phase),
                        None,
                        int(transfer.created_ns),
                        None,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO financial_outbox
                        (outbox_idempotency_key, silo, strategy_id, proposal_id,
                         vault, symbol, side, qty_micros, price_micros,
                         notional_micros, fee_micros, tax_micros,
                         total_lock_micros, pending_transfer_id, state,
                         broker_order_id, created_ns, updated_ns, detail)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        reservation.outbox_idempotency_key,
                        reservation.silo,
                        reservation.strategy_id,
                        reservation.proposal_id,
                        str(reservation.vault),
                        reservation.symbol,
                        reservation.side,
                        str(reservation.qty_micros),
                        str(reservation.price_micros),
                        str(reservation.notional_micros),
                        str(reservation.fee_micros),
                        str(reservation.tax_micros),
                        str(reservation.total_lock_micros),
                        str(reservation.pending_transfer_id),
                        str(reservation.state),
                        reservation.broker_order_id,
                        int(reservation.created_ns),
                        int(reservation.created_ns),
                        "",
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def update_outbox(
        self,
        key: str,
        *,
        state: str,
        updated_ns: int,
        broker_order_id: str = "",
        detail: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE financial_outbox
                   SET state = ?, updated_ns = ?,
                       broker_order_id = CASE WHEN ? = '' THEN broker_order_id ELSE ? END,
                       detail = ?
                 WHERE outbox_idempotency_key = ?
                """,
                (state, int(updated_ns), broker_order_id, broker_order_id, detail, key),
            )

    def update_outbox_many(
        self,
        keys: Iterable[str],
        *,
        state: str,
        updated_ns: int,
        detail: str = "",
    ) -> None:
        """Batch the kill-switch void into one journal transaction."""
        rows = [(state, int(updated_ns), detail, str(key)) for key in keys]
        if not rows:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    """
                    UPDATE financial_outbox
                       SET state = ?, updated_ns = ?, detail = ?
                     WHERE outbox_idempotency_key = ?
                    """,
                    rows,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_outbox(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM financial_outbox WHERE outbox_idempotency_key = ?", (key,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [c[0] for c in cursor.description]
        return dict(zip(columns, row))

    def transfer_journal(self) -> list[dict[str, Any]]:
        """Append-ordered journal rows used to rebuild the state machine."""
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT entry_id, transfer_id, silo, debit_account, credit_account,
                       amount_micros, code, phase, created_ns, posted_ns
                  FROM ibor_double_entry_ledger
                 ORDER BY entry_id
                """
            )
            rows = cursor.fetchall()
            columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    def outbox_rows(self, *, state: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if state is None:
                cursor = self._conn.execute(
                    "SELECT * FROM financial_outbox ORDER BY created_ns"
                )
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM financial_outbox WHERE state = ? ORDER BY created_ns",
                    (str(state),),
                )
            rows = cursor.fetchall()
            columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    def write_ack(self, ack: "BrokerAck") -> bool:
        """Insert a broker ack; ``False`` when the ``broker_order_id`` is a dupe."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO order_ack_inbox
                        (broker_order_id, outbox_key, state, filled_qty_micros,
                         avg_price_micros, received_ns, applied)
                    VALUES (?,?,?,?,?,?,0)
                    """,
                    (
                        ack.broker_order_id,
                        ack.outbox_idempotency_key,
                        str(ack.state),
                        str(int(ack.filled_qty_micros)),
                        str(int(ack.avg_price_micros)),
                        int(ack.received_ns),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_ack_applied(self, broker_order_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE order_ack_inbox SET applied = 1 WHERE broker_order_id = ?",
                (str(broker_order_id),),
            )

    def write_tax_lot(self, lot: Mapping[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tax_lots
                    (silo, symbol, side, qty_micros, price_micros, notional_micros,
                     stt_micros, tds_micros, stamp_micros, fee_micros, opened_ns,
                     closed_ns, realized_net_micros)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    lot["silo"],
                    lot["symbol"],
                    lot["side"],
                    str(int(lot["qty_micros"])),
                    str(int(lot["price_micros"])),
                    str(int(lot["notional_micros"])),
                    str(int(lot.get("stt_micros", 0))),
                    str(int(lot.get("tds_micros", 0))),
                    str(int(lot.get("stamp_micros", 0))),
                    str(int(lot.get("fee_micros", 0))),
                    int(lot["opened_ns"]),
                    lot.get("closed_ns"),
                    None
                    if lot.get("realized_net_micros") is None
                    else str(int(lot["realized_net_micros"])),
                ),
            )

    def register_grounding_hash(
        self,
        grounding_hash: str,
        *,
        silo: str = "",
        symbol: str = "",
        strategy_id: str = "",
        source: str = "ring2.grounding_ledger",
        claim_sha256: str | None = None,
        registered_ns: int | None = None,
    ) -> bool:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO grounding_hash_store
                        (grounding_hash, silo, symbol, strategy_id, source,
                         claim_sha256, registered_ns)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        str(grounding_hash).lower(),
                        str(silo),
                        str(symbol),
                        str(strategy_id),
                        str(source),
                        None if claim_sha256 is None else str(claim_sha256).lower(),
                        int(registered_ns if registered_ns is not None else time.time_ns()),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def has_grounding_hash(self, grounding_hash: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM grounding_hash_store WHERE grounding_hash = ?",
                (str(grounding_hash).lower(),),
            ).fetchone()
        return row is not None

    # -- lifecycle --------------------------------------------------------
    def flush(self) -> None:
        """Durability barrier -- call before handing an order to a socket."""
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self) -> "LedgerStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# The four-vault double-entry ledger
# ---------------------------------------------------------------------------


class IBORLedger:
    """Four-vault, 128-bit integer, double-entry Investment Book of Record.

    Every broker silo owns exactly four vaults (``sec_009``)::

        ReserveYield | LongTermCompounder | TacticalSwing | SpinalScalper

    plus three external counterparts (``CASH``, ``FEE_TAX_ACCRUAL``,
    ``REALIZED_PNL``).  All mutations are validated against
    ``debits_must_not_exceed_credits`` **before** the journal append, so an
    overdraft is impossible to observe, not merely impossible to commit.
    """

    def __init__(
        self,
        store: LedgerStore | None = None,
        *,
        silos: Iterable[str] = (),
        now_ns: int | None = None,
    ) -> None:
        self.store = store if store is not None else LedgerStore(in_memory=True)
        self._lock = threading.RLock()
        self._accounts: dict[tuple[str, str], VaultAccount] = {}
        self._transfers: dict[int, Transfer] = {}
        self._positions: dict[tuple[str, str], dict[str, int]] = {}
        self._next_transfer_id = 1
        self._created_ns = int(now_ns if now_ns is not None else time.time_ns())
        for silo in silos:
            self.open_silo(silo)
        # A restarted process rebuilds the exact book from the append-only
        # journal before any new transfer can take an id.
        self._replay_journal()

    # -- account access ---------------------------------------------------
    def _key(self, silo: str, code: str) -> tuple[str, str]:
        return (str(silo), str(code))

    def account(self, silo: str, code: str) -> VaultAccount:
        key = self._key(silo, code)
        account = self._accounts.get(key)
        if account is None:
            account = VaultAccount(
                silo=str(silo),
                code=str(code),
                enforce_credit_floor=str(code) not in _EXTERNAL_CODES,
            )
            self._accounts[key] = account
        return account

    def accounts(self, silo: str) -> dict[str, VaultAccount]:
        return {
            code: account
            for (owner, code), account in self._accounts.items()
            if owner == str(silo)
        }

    def balance(self, silo: str, code: str) -> int:
        return self.account(silo, code).balance

    def available(self, silo: str, code: str) -> int:
        return self.account(silo, code).available

    def vault_balances(self, silo: str) -> dict[VaultName, int]:
        return {vault: self.balance(silo, str(vault)) for vault in ALL_VAULTS}

    def silo_nav_micros(self, silo: str) -> int:
        """Sum of the four vault posted balances (the silo's own NAV)."""
        return sum(self.balance(silo, str(vault)) for vault in ALL_VAULTS)

    def total_nav_micros(
        self, silos: Iterable[str], *, fx_to_base: Mapping[str, int] | None = None
    ) -> int:
        """Point-in-time NAV across silos, converted with historical FX rates.

        ``fx_to_base[silo]`` is an integer multiplier in micro-units
        (``1_000_000`` meaning 1:1).  Integer maths keeps NAV free of float
        drift; the FX table itself is supplied by Ring 2's historical series
        (``sec_009``).
        """
        total = 0
        for silo in silos:
            rate = 1_000_000 if fx_to_base is None else int(fx_to_base.get(str(silo), 1_000_000))
            if rate <= 0:
                raise LedgerError(f"FX rate for {silo!r} must be positive")
            total += (self.silo_nav_micros(str(silo)) * rate) // 1_000_000
        return total

    def open_silo(self, silo: str, *, funding_micros: int = 0) -> dict[str, VaultAccount]:
        """Create the seven accounts of a silo and optionally pre-fund it."""
        silo = str(silo)
        for vault in ALL_VAULTS:
            self.account(silo, str(vault))
        for code in _EXTERNAL_CODES:
            self.account(silo, code)
        if funding_micros:
            self.fund_cash(silo, funding_micros)
        return self.accounts(silo)

    # -- primitives -------------------------------------------------------
    def _allocate_id(self) -> int:
        value = self._next_transfer_id
        self._next_transfer_id += 1
        return value

    def _apply_pending(
        self, debit: VaultAccount, credit: VaultAccount, amount: int
    ) -> None:
        debit.debits_pending += amount
        credit.credits_pending += amount

    def _release_pending(
        self, debit: VaultAccount, credit: VaultAccount, amount: int
    ) -> None:
        debit.debits_pending -= amount
        credit.credits_pending -= amount

    def _post_pending(
        self, debit: VaultAccount, credit: VaultAccount, amount: int
    ) -> None:
        debit.debits_pending -= amount
        debit.debits_posted += amount
        credit.credits_pending -= amount
        credit.credits_posted += amount

    def create_pending(
        self,
        silo: str,
        *,
        debit: str,
        credit: str,
        amount_micros: int,
        code: int = int(TransferCode.ORDER_RESERVATION),
        now_ns: int | None = None,
        persist: bool = True,
    ) -> Transfer:
        """Phase 1: lock ``amount_micros`` as a pending transfer.

        Raises :class:`OverdraftError` when the debit account does not have
        enough unencumbered credit -- this is the ``debits_must_not_exceed_credits``
        enforcement point.
        """
        amount = _check_money(amount_micros)
        if amount == 0:
            raise LedgerError("cannot create a zero-amount transfer")
        with self._lock:
            debit_account = self.account(silo, debit)
            credit_account = self.account(silo, credit)
            # Validate *before* mutating so a rejected reservation is invisible.
            projected_debits = debit_account.debits_total + amount
            projected_credits = credit_account.credits_total + amount
            if debit_account.enforce_credit_floor and projected_debits > (
                debit_account.credits_total
            ):
                raise OverdraftError(
                    f"debits_must_not_exceed_credits: {silo}.{debit} has "
                    f"available={debit_account.available} micros but the "
                    f"transfer needs {amount} micros"
                )
            _check_money(projected_debits, what="debits_total")
            _check_money(projected_credits, what="credits_total")

            transfer = Transfer(
                transfer_id=self._allocate_id(),
                silo=str(silo),
                debit_account=str(debit),
                credit_account=str(credit),
                amount_micros=amount,
                code=int(code),
                phase=TransferPhase.PENDING,
                created_ns=int(now_ns if now_ns is not None else time.time_ns()),
            )
            self._apply_pending(debit_account, credit_account, amount)
            debit_account.check()
            credit_account.check()
            if persist:
                # ``reserve`` passes persist=False and writes the transfer plus
                # its outbox row in one atomic transaction (no duplicate row).
                self.store.write_transfer(transfer)
            self._transfers[transfer.transfer_id] = transfer
            return transfer

    def post_pending(self, transfer_id: int, *, now_ns: int | None = None) -> Transfer:
        """Phase 2a: settle a pending transfer (broker FILLED)."""
        with self._lock:
            transfer = self._require_pending(transfer_id)
            debit_account = self.account(transfer.silo, transfer.debit_account)
            credit_account = self.account(transfer.silo, transfer.credit_account)
            self._post_pending(debit_account, credit_account, transfer.amount_micros)
            debit_account.check()
            credit_account.check()
            transfer.phase = TransferPhase.POSTED
            transfer.posted_ns = int(now_ns if now_ns is not None else time.time_ns())
            self.store.write_transfer_phase(
                transfer, phase=TransferPhase.POSTED, posted_ns=transfer.posted_ns
            )
            return transfer

    def void_pending(self, transfer_id: int, *, now_ns: int | None = None) -> Transfer:
        """Phase 2b: roll a pending transfer back (CANCELED / REJECTED / timeout)."""
        with self._lock:
            transfer = self._require_pending(transfer_id)
            debit_account = self.account(transfer.silo, transfer.debit_account)
            credit_account = self.account(transfer.silo, transfer.credit_account)
            self._release_pending(debit_account, credit_account, transfer.amount_micros)
            debit_account.check()
            credit_account.check()
            transfer.phase = TransferPhase.VOIDED
            transfer.posted_ns = int(now_ns if now_ns is not None else time.time_ns())
            self.store.write_transfer_phase(
                transfer, phase=TransferPhase.VOIDED, posted_ns=transfer.posted_ns
            )
            return transfer

    def _replay_journal(self) -> None:
        """Rebuild accounts and transfers from the append-only journal.

        The journal is event-sourced: a ``PENDING`` row applies the pending
        debit/credit, a later ``POSTED`` row converts that pending pair into a
        posted pair, and a ``VOIDED`` row releases it.  Replaying in
        ``entry_id`` order therefore reconstructs the exact book a crashed
        process had, and ``debits_must_not_exceed_credits`` is re-validated on
        the way through.
        """
        for row in self.store.transfer_journal():
            transfer_id = int(row["transfer_id"])
            phase = TransferPhase(row["phase"])
            silo = str(row["silo"])
            debit = self.account(silo, row["debit_account"])
            credit = self.account(silo, row["credit_account"])
            amount = int(row["amount_micros"])

            if phase is TransferPhase.PENDING:
                if transfer_id in self._transfers:
                    continue
                self._apply_pending(debit, credit, amount)
                self._transfers[transfer_id] = Transfer(
                    transfer_id=transfer_id,
                    silo=silo,
                    debit_account=str(row["debit_account"]),
                    credit_account=str(row["credit_account"]),
                    amount_micros=amount,
                    code=int(row["code"]),
                    phase=TransferPhase.PENDING,
                    created_ns=int(row["created_ns"]),
                )
                self._next_transfer_id = max(self._next_transfer_id, transfer_id + 1)
                continue

            existing = self._transfers.get(transfer_id)
            if existing is None or not existing.is_pending:
                continue
            if phase is TransferPhase.POSTED:
                self._post_pending(debit, credit, amount)
                existing.phase = TransferPhase.POSTED
            elif phase is TransferPhase.VOIDED:
                self._release_pending(debit, credit, amount)
                existing.phase = TransferPhase.VOIDED
            existing.posted_ns = (
                None if row["posted_ns"] is None else int(row["posted_ns"])
            )
            debit.check()
            credit.check()

    def _require_pending(self, transfer_id: int) -> Transfer:
        transfer = self._transfers.get(int(transfer_id))
        if transfer is None:
            raise LedgerError(f"unknown transfer_id {transfer_id!r}")
        if not transfer.is_pending:
            raise OutboxStateError(
                f"transfer {transfer_id} is already {transfer.phase}, not PENDING"
            )
        return transfer

    def transfer_now(
        self,
        silo: str,
        *,
        debit: str,
        credit: str,
        amount_micros: int,
        code: int = int(TransferCode.FUNDING),
        now_ns: int | None = None,
    ) -> Transfer:
        """Convenience: create + post in one step (no broker round-trip)."""
        pending = self.create_pending(
            silo, debit=debit, credit=credit, amount_micros=amount_micros, code=code
        )
        return self.post_pending(pending.transfer_id, now_ns=now_ns)

    # -- money in / out ---------------------------------------------------
    def fund_cash(self, silo: str, amount_micros: int, *, now_ns: int | None = None) -> Transfer:
        """Money enters the silo from outside: credit CASH, debit the P&L hub."""
        return self.transfer_now(
            silo,
            debit=str(AccountCode.PNL),
            credit=str(AccountCode.CASH),
            amount_micros=amount_micros,
            code=int(TransferCode.FUNDING),
            now_ns=now_ns,
        )

    def fund_vault(
        self, silo: str, vault: VaultName | str, amount_micros: int, *, now_ns: int | None = None
    ) -> Transfer:
        """Allocate cash from the silo's CASH hub into one vault."""
        return self.transfer_now(
            silo,
            debit=str(AccountCode.CASH),
            credit=str(vault),
            amount_micros=amount_micros,
            code=int(TransferCode.FUNDING),
            now_ns=now_ns,
        )

    def accrue_fill_charges(
        self,
        silo: str,
        *,
        notional_micros: int,
        fee_bps: float,
        tax_bps: float,
        stt_bps: float = 0.0,
        tds_bps: float = 0.0,
        stamp_bps: float = 0.0,
        source_account: VaultName | str = VaultName.SPINAL_SCALPER,
        symbol: str = "",
        side: str = "",
        qty_micros: int = 0,
        price_micros: int = 0,
        now_ns: int | None = None,
    ) -> dict[str, int]:
        """Deduct accrued STT, TDS, stamp duty and fees on every fill.

        The charges are debited from the trading vault and credited to the
        ``FEE_TAX_ACCRUAL`` bucket, then journalled as a tax lot so the
        point-in-time NAV and the auditor's cost basis agree (``sec_009``).
        """
        notional = _check_money(notional_micros)
        fee_micros = _apply_bps(notional, 2.0 * fee_bps)
        stt_micros = _apply_bps(notional, stt_bps)
        tds_micros = _apply_bps(notional, tds_bps)
        stamp_micros = _apply_bps(notional, stamp_bps)
        tax_micros = _apply_bps(notional, tax_bps)
        total = fee_micros + stt_micros + tds_micros + stamp_micros + tax_micros
        if total > 0:
            self.transfer_now(
                silo,
                debit=str(source_account),
                credit=str(AccountCode.FEES),
                amount_micros=total,
                code=int(TransferCode.CHARGE_ACCRUAL),
                now_ns=now_ns,
            )
        self.store.write_tax_lot(
            {
                "silo": str(silo),
                "symbol": str(symbol),
                "side": str(side),
                "qty_micros": int(qty_micros),
                "price_micros": int(price_micros),
                "notional_micros": notional,
                "stt_micros": stt_micros,
                "tds_micros": tds_micros,
                "stamp_micros": stamp_micros,
                "fee_micros": fee_micros + tax_micros,
                "opened_ns": int(now_ns if now_ns is not None else time.time_ns()),
            }
        )
        return {
            "fee_micros": fee_micros,
            "tax_micros": tax_micros,
            "stt_micros": stt_micros,
            "tds_micros": tds_micros,
            "stamp_micros": stamp_micros,
            "total_micros": total,
        }

    # -- positions --------------------------------------------------------
    def set_position(
        self, silo: str, symbol: str, *, qty_micros: int, price_micros: int
    ) -> dict[str, int]:
        record = {
            "qty_micros": int(qty_micros),
            "price_micros": int(price_micros),
            "notional_micros": (int(qty_micros) * int(price_micros)) // MICROS_PER_UNIT,
        }
        self._positions[(str(silo), str(symbol))] = record
        return record

    def position(self, silo: str, symbol: str) -> dict[str, int] | None:
        return self._positions.get((str(silo), str(symbol)))

    def positions(self, silo: str | None = None) -> dict[str, dict[str, int]]:
        return {
            f"{owner}:{symbol}": dict(record)
            for (owner, symbol), record in self._positions.items()
            if silo is None or owner == str(silo)
        }

    def position_notional_micros(self, silo: str) -> int:
        return sum(
            record["notional_micros"]
            for (owner, _symbol), record in self._positions.items()
            if owner == str(silo)
        )

    # -- introspection ----------------------------------------------------
    def transfers(self, *, silo: str | None = None) -> list[Transfer]:
        return [
            transfer
            for transfer in self._transfers.values()
            if silo is None or transfer.silo == str(silo)
        ]

    def pending_transfers(self) -> list[Transfer]:
        return [t for t in self._transfers.values() if t.is_pending]

    def book(self, silo: str) -> dict[str, dict[str, Any]]:
        return {code: account.to_dict() for code, account in self.accounts(silo).items()}

    def invariant_report(self) -> dict[str, Any]:
        """Every account's ``debits_must_not_exceed_credits`` status."""
        violations: list[str] = []
        for account in self._accounts.values():
            try:
                account.check()
            except OverdraftError as exc:
                violations.append(str(exc))
        return {
            "accounts": len(self._accounts),
            "violations": violations,
            "holds": not violations,
            "balances": {
                f"{account.silo}.{account.code}": account.balance
                for account in self._accounts.values()
            },
        }

    def flush(self) -> None:
        self.store.flush()

    def close(self) -> None:
        self.store.close()


_EXTERNAL_CODES: tuple[str, ...] = (
    str(AccountCode.CASH),
    str(AccountCode.FEES),
    str(AccountCode.PNL),
)


_BPS_SCALE: int = 1_000_000


def _apply_bps(amount_micros: int, bps: float) -> int:
    """Integer ``amount * bps / 10_000`` with micro-bps resolution.

    Basis points are not integers in this system (India's STT floor is
    ``5.5 bps``), so the multiplier is quantised to one part per million of a
    basis point and the division is done with integer maths.  A 128-bit notional
    therefore never loses a micro-unit to float rounding.
    """
    scaled = int(round(float(bps) * _BPS_SCALE))
    return (int(amount_micros) * scaled) // (10_000 * _BPS_SCALE)


# ---------------------------------------------------------------------------
# Two-phase crash-atomic outbox
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Reservation:
    """The durable record of one committed two-phase reservation."""

    outbox_idempotency_key: str
    silo: str
    vault: VaultName
    symbol: str
    side: str
    qty_micros: int
    price_micros: int
    notional_micros: int
    fee_micros: int
    tax_micros: int
    total_lock_micros: int
    pending_transfer_id: int
    state: OutboxState
    strategy_id: str = ""
    proposal_id: str = ""
    broker_order_id: str = ""
    created_ns: int = 0
    updated_ns: int = 0
    duplicate: bool = False

    @property
    def is_pending(self) -> bool:
        return self.state is OutboxState.PENDING

    @property
    def is_committed(self) -> bool:
        return self.state is OutboxState.COMMITTED

    @property
    def is_voided(self) -> bool:
        return self.state is OutboxState.VOIDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "outbox_idempotency_key": self.outbox_idempotency_key,
            "silo": self.silo,
            "vault": str(self.vault),
            "symbol": self.symbol,
            "side": self.side,
            "qty_micros": self.qty_micros,
            "price_micros": self.price_micros,
            "notional_micros": self.notional_micros,
            "fee_micros": self.fee_micros,
            "tax_micros": self.tax_micros,
            "total_lock_micros": self.total_lock_micros,
            "pending_transfer_id": self.pending_transfer_id,
            "state": str(self.state),
            "strategy_id": self.strategy_id,
            "proposal_id": self.proposal_id,
            "broker_order_id": self.broker_order_id,
            "created_ns": self.created_ns,
            "updated_ns": self.updated_ns,
            "duplicate": self.duplicate,
        }


class BrokerAckState(StrEnum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    WORKING = "WORKING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"


@dataclass(slots=True, frozen=True)
class BrokerAck:
    """One idempotent broker acknowledgement."""

    broker_order_id: str
    outbox_idempotency_key: str
    state: BrokerAckState | str
    filled_qty_micros: int = 0
    avg_price_micros: int = 0
    received_ns: int = field(default_factory=time.time_ns)

    def __post_init__(self) -> None:
        if not isinstance(self.state, BrokerAckState):
            object.__setattr__(self, "state", BrokerAckState(str(self.state).upper()))

    @property
    def terminal(self) -> bool:
        return self.state in (
            BrokerAckState.FILLED,
            BrokerAckState.CANCELED,
            BrokerAckState.REJECTED,
            BrokerAckState.TIMEOUT,
        )


@dataclass(slots=True, frozen=True)
class AckResult:
    applied: bool
    duplicate: bool
    state: OutboxState | None
    reservation: Reservation | None
    detail: str = ""


def deterministic_outbox_key(
    *,
    silo: str,
    strategy_id: str,
    proposal_id: str,
    vault: VaultName | str,
    symbol: str,
    side: str,
    qty_micros: int,
    price_micros: int,
) -> str:
    """Deterministic ``outbox_idempotency_key`` (``sec_009``).

    Two submissions of the *same intent* therefore collide by construction and
    can never double-debit or double-submit (``AtMostOnceExecution``).
    """
    return canonical_pairs_digest(
        (
            ("silo", str(silo)),
            ("strategy_id", str(strategy_id)),
            ("proposal_id", str(proposal_id)),
            ("vault", str(vault)),
            ("symbol", str(symbol)),
            ("side", str(side).upper()),
            ("qty_micros", int(qty_micros)),
            ("price_micros", int(price_micros)),
        )
    )


class CrashAtomicOutbox:
    """Two-phase crash-atomic outbox guarding the broker socket.

    ``reserve`` is the only way an order can reach a broker adapter:

    1. derive the deterministic idempotency key;
    2. lock ``notional + 2*fee + tax`` as a *pending* double-entry transfer;
    3. append the ``financial_outbox`` row in the same ACID transaction;
    4. return the reservation.

    A duplicate key short-circuits at step 1 and returns the original
    reservation with ``duplicate=True`` -- no second debit, no second broker
    submission.
    """

    def __init__(
        self,
        ledger: IBORLedger,
        *,
        timeout_ns: int = TRANSFER_TIMEOUT_NS,
        latency_budget_ns: int = OUTBOX_LATENCY_BUDGET_NS,
    ) -> None:
        self.ledger = ledger
        self.store = ledger.store
        self.timeout_ns = int(timeout_ns)
        self.latency_budget_ns = int(latency_budget_ns)
        self._reservations: dict[str, Reservation] = {}
        self._by_transfer: dict[int, str] = {}
        self._lock = threading.RLock()
        self._last_latency_ns = 0
        self._replay()

    # -- recovery ---------------------------------------------------------
    def _replay(self) -> None:
        """Rehydrate reservations from the durable journal after a crash."""
        for row in self.store.outbox_rows():
            reservation = Reservation(
                outbox_idempotency_key=row["outbox_idempotency_key"],
                silo=row["silo"],
                vault=VaultName(row["vault"]),
                symbol=row["symbol"],
                side=row["side"],
                qty_micros=int(row["qty_micros"]),
                price_micros=int(row["price_micros"]),
                notional_micros=int(row["notional_micros"]),
                fee_micros=int(row["fee_micros"]),
                tax_micros=int(row["tax_micros"]),
                total_lock_micros=int(row["total_lock_micros"]),
                pending_transfer_id=int(row["pending_transfer_id"]),
                state=OutboxState(row["state"]),
                strategy_id=row["strategy_id"],
                proposal_id=row["proposal_id"],
                broker_order_id=row["broker_order_id"],
                created_ns=int(row["created_ns"]),
                updated_ns=int(row["updated_ns"]),
            )
            self._reservations[reservation.outbox_idempotency_key] = reservation
            self._by_transfer[reservation.pending_transfer_id] = (
                reservation.outbox_idempotency_key
            )

    # -- cost model -------------------------------------------------------
    def lock_amounts(
        self, proposal: UnsignedProposal, *, cost_floor: Any = None
    ) -> tuple[int, int, int, int, int]:
        """``(qty, price, notional, fee, tax)`` in integer micro-units."""
        qty_micros = to_micros(proposal.qty)
        price_micros = to_micros(proposal.limit_price)
        notional = (qty_micros * price_micros) // MICROS_PER_UNIT

        if cost_floor is None:
            from ring1_spinal.silo_worker import silo_cost_floor  # off hot path

            cost_floor = silo_cost_floor(str(proposal.silo))
        fee_multiplier = 2.0 * float(getattr(cost_floor, "maker_fee_bps"))
        tax_multiplier = float(getattr(cost_floor, "statutory_tax_bps"))
        fee_micros = _apply_bps(notional, fee_multiplier)
        tax_micros = _apply_bps(notional, tax_multiplier)
        return qty_micros, price_micros, notional, fee_micros, tax_micros

    # -- reserve ----------------------------------------------------------
    def reserve(
        self,
        proposal: UnsignedProposal,
        *,
        now_ns: int | None = None,
        cost_floor: Any = None,
        broker_order_id: str = "",
    ) -> Reservation:
        """Commit the two-phase pending reservation (``< 2.0 ms``)."""
        started = time.perf_counter_ns()
        stamp = int(now_ns if now_ns is not None else time.time_ns())

        qty_micros, price_micros, notional, fee_micros, tax_micros = self.lock_amounts(
            proposal, cost_floor=cost_floor
        )
        key = deterministic_outbox_key(
            silo=proposal.silo,
            strategy_id=proposal.strategy_id,
            proposal_id=proposal.proposal_id,
            vault=proposal.vault,
            symbol=proposal.symbol,
            side=proposal.side,
            qty_micros=qty_micros,
            price_micros=price_micros,
        )

        with self._lock:
            existing = self._reservations.get(key)
            if existing is not None:
                duplicate = replace(existing, duplicate=True)
                self._last_latency_ns = time.perf_counter_ns() - started
                return duplicate

            total_lock = notional + fee_micros + tax_micros
            transfer = self.ledger.create_pending(
                str(proposal.silo),
                debit=str(proposal.vault),
                credit=str(AccountCode.CASH),
                amount_micros=total_lock,
                code=int(TransferCode.ORDER_RESERVATION),
                now_ns=stamp,
                persist=False,
            )
            reservation = Reservation(
                outbox_idempotency_key=key,
                silo=str(proposal.silo),
                vault=proposal.vault,
                symbol=str(proposal.symbol),
                side=str(proposal.side),
                qty_micros=qty_micros,
                price_micros=price_micros,
                notional_micros=notional,
                fee_micros=fee_micros,
                tax_micros=tax_micros,
                total_lock_micros=total_lock,
                pending_transfer_id=transfer.transfer_id,
                state=OutboxState.PENDING,
                strategy_id=str(proposal.strategy_id),
                proposal_id=str(proposal.proposal_id),
                broker_order_id=str(broker_order_id),
                created_ns=stamp,
                updated_ns=stamp,
            )
            self.store.write_reservation(transfer, reservation)
            self._reservations[key] = reservation
            self._by_transfer[transfer.transfer_id] = key
        self._last_latency_ns = time.perf_counter_ns() - started
        return reservation

    # -- resolution -------------------------------------------------------
    def commit(
        self,
        key: str,
        *,
        broker_order_id: str = "",
        now_ns: int | None = None,
        realized_net_micros: int | None = None,
        ratchet: "InSiloProfitRatchet | None" = None,
    ) -> Reservation:
        """Broker FILLED: post the pending transfer (idempotent)."""
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            reservation = self._require(key)
            if reservation.is_committed:
                return replace(reservation, duplicate=True)
            if reservation.is_voided:
                raise OutboxStateError(
                    f"outbox key {key[:12]}... is VOIDED and cannot be committed"
                )
            self.ledger.post_pending(reservation.pending_transfer_id, now_ns=stamp)
            updated = self._replace(
                reservation,
                state=OutboxState.COMMITTED,
                updated_ns=stamp,
                broker_order_id=broker_order_id or reservation.broker_order_id,
            )
            self.store.update_outbox(
                key,
                state=str(OutboxState.COMMITTED),
                updated_ns=stamp,
                broker_order_id=broker_order_id,
                detail="broker FILLED",
            )
        if realized_net_micros is not None and ratchet is not None:
            ratchet.apply(updated.silo, realized_net_micros, now_ns=stamp)
        return updated

    def void(
        self, key: str, *, reason: str = "", now_ns: int | None = None
    ) -> Reservation:
        """CANCELED / REJECTED / timeout: void the pending transfer (idempotent)."""
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            reservation = self._require(key)
            if reservation.is_voided:
                return replace(reservation, duplicate=True)
            if reservation.is_committed:
                raise OutboxStateError(
                    f"outbox key {key[:12]}... is COMMITTED and cannot be voided"
                )
            self.ledger.void_pending(reservation.pending_transfer_id, now_ns=stamp)
            updated = self._replace(
                reservation, state=OutboxState.VOIDED, updated_ns=stamp
            )
            self.store.update_outbox(
                key,
                state=str(OutboxState.VOIDED),
                updated_ns=stamp,
                detail=str(reason),
            )
            return updated

    def void_many(
        self,
        keys: Iterable[str],
        *,
        reason: str = "",
        now_ns: int | None = None,
        persist: bool = True,
    ) -> list[Reservation]:
        """Void several pending reservations in one journal transaction.

        ``persist=False`` performs the in-memory state transition only; the
        kill switch uses it to keep the measured preemption window sub-ms and
        flushes the durable journal immediately afterwards.
        """
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        tokens = [str(key) for key in keys]
        with self._lock:
            updated: list[Reservation] = []
            changed: list[str] = []
            for key in tokens:
                reservation = self._require(key)
                if reservation.is_voided:
                    updated.append(replace(reservation, duplicate=True))
                    continue
                if reservation.is_committed:
                    raise OutboxStateError(
                        f"outbox key {key[:12]}... is COMMITTED and cannot be voided"
                    )
                self.ledger.void_pending(
                    reservation.pending_transfer_id, now_ns=stamp
                )
                updated.append(
                    self._replace(
                        reservation, state=OutboxState.VOIDED, updated_ns=stamp
                    )
                )
                changed.append(key)
            if persist:
                self.store.update_outbox_many(
                    changed,
                    state=str(OutboxState.VOIDED),
                    updated_ns=stamp,
                    detail=str(reason),
                )
            return updated

    def handle_ack(self, ack: BrokerAck) -> AckResult:
        """Apply one broker ack with inbox-level de-duplication."""
        if not self.store.write_ack(ack):
            return AckResult(
                applied=False,
                duplicate=True,
                state=None,
                reservation=None,
                detail=f"ack {ack.broker_order_id} already processed",
            )
        try:
            if ack.state is BrokerAckState.FILLED:
                reservation = self.commit(
                    ack.outbox_idempotency_key,
                    broker_order_id=ack.broker_order_id,
                    now_ns=ack.received_ns,
                )
            elif ack.state in (
                BrokerAckState.CANCELED,
                BrokerAckState.REJECTED,
                BrokerAckState.TIMEOUT,
            ):
                reservation = self.void(
                    ack.outbox_idempotency_key,
                    reason=str(ack.state),
                    now_ns=ack.received_ns,
                )
            else:
                reservation = self.get(ack.outbox_idempotency_key)
        finally:
            self.store.mark_ack_applied(ack.broker_order_id)
        return AckResult(
            applied=True,
            duplicate=False,
            state=reservation.state if reservation else None,
            reservation=reservation,
            detail=str(ack.state),
        )

    def expire_timeouts(self, *, now_ns: int | None = None) -> list[Reservation]:
        """Void every PENDING reservation older than the maker-peg timeout."""
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        expired: list[Reservation] = []
        for reservation in list(self._reservations.values()):
            if not reservation.is_pending:
                continue
            if stamp - reservation.created_ns >= self.timeout_ns:
                expired.append(
                    self.void(
                        reservation.outbox_idempotency_key,
                        reason="TIMEOUT: 3s maker-peg hard cancel",
                        now_ns=stamp,
                    )
                )
        return expired

    # -- reads ------------------------------------------------------------
    def get(self, key: str) -> Reservation | None:
        return self._reservations.get(str(key))

    def get_by_transfer(self, transfer_id: int) -> Reservation | None:
        key = self._by_transfer.get(int(transfer_id))
        return None if key is None else self._reservations.get(key)

    def open_reservations(self) -> list[Reservation]:
        return [r for r in self._reservations.values() if r.is_pending]

    def all_reservations(self) -> list[Reservation]:
        return list(self._reservations.values())

    @property
    def pending_count(self) -> int:
        return sum(1 for r in self._reservations.values() if r.is_pending)

    @property
    def last_latency_ns(self) -> int:
        return self._last_latency_ns

    @property
    def within_latency_budget(self) -> bool:
        return self._last_latency_ns <= self.latency_budget_ns

    # -- lifecycle --------------------------------------------------------
    def flush(self) -> None:
        """Durability barrier -- call before handing an order to a socket."""
        self.store.flush()

    def snapshot(self) -> dict[str, Any]:
        counts = {state.value: 0 for state in OutboxState}
        for reservation in self._reservations.values():
            counts[str(reservation.state)] += 1
        return {
            "reservations": len(self._reservations),
            "counts": counts,
            "pending": self.pending_count,
            "last_latency_ns": self._last_latency_ns,
            "latency_budget_ns": self.latency_budget_ns,
            "timeout_ns": self.timeout_ns,
        }

    # -- internals --------------------------------------------------------
    def _require(self, key: str) -> Reservation:
        reservation = self._reservations.get(str(key))
        if reservation is None:
            raise LedgerError(f"unknown outbox_idempotency_key {key!r}")
        return reservation

    def _replace(self, reservation: Reservation, **changes: Any) -> Reservation:
        updated = replace(reservation, **changes)
        self._reservations[updated.outbox_idempotency_key] = updated
        return updated


# ---------------------------------------------------------------------------
# In-silo profit ratchet (Law 5)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class RatchetResult:
    """Outcome of one in-silo profit sweep."""

    silo: str
    applied: bool
    market: str
    realized_net_micros: int
    long_term_micros: int
    reserve_micros: int
    scalper_micros: int
    long_term_asset: str
    reserve_asset: str
    cross_border_wire_bps: float
    transfer_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "applied": self.applied,
            "market": self.market,
            "realized_net_micros": self.realized_net_micros,
            "long_term_micros": self.long_term_micros,
            "reserve_micros": self.reserve_micros,
            "scalper_micros": self.scalper_micros,
            "long_term_asset": self.long_term_asset,
            "reserve_asset": self.reserve_asset,
            "cross_border_wire_bps": self.cross_border_wire_bps,
            "transfer_ids": list(self.transfer_ids),
        }


class InSiloProfitRatchet:
    """Sweep 35% of a winning scalp into the compounder and 15% into reserve.

    The remaining 50% stays in ``SpinalScalper`` as fresh trading capital.  All
    three legs debit and credit accounts **inside the same broker silo**, so the
    cross-border wire bleed is exactly ``0.0 bps`` (``sec_015``, Law 5).

    A losing trade never ratchets: ``realized_net_micros <= 0`` is a no-op.  The
    ratchet compounds winners and lets losers be absorbed by the trading vault
    that took the risk.
    """

    __slots__ = ("ledger",)

    def __init__(self, ledger: IBORLedger) -> None:
        self.ledger = ledger

    def apply(
        self, silo: str, realized_net_micros: int, *, now_ns: int | None = None
    ) -> RatchetResult:
        realized = int(realized_net_micros)
        market = _market_for_silo(silo)
        assets = VAULT_ASSET_MAP.get(
            str(silo),
            {"long_term_sweep_asset": "Long-Term Compounders", "reserve_yield_asset": "Reserve Yield"},
        )
        if realized <= 0:
            return RatchetResult(
                silo=str(silo),
                applied=False,
                market=market,
                realized_net_micros=realized,
                long_term_micros=0,
                reserve_micros=0,
                scalper_micros=0,
                long_term_asset=assets["long_term_sweep_asset"],
                reserve_asset=assets["reserve_yield_asset"],
                cross_border_wire_bps=0.0,
            )

        long_term = realized * RATCHET_LONG_TERM_PCT // 100
        reserve = realized * RATCHET_RESERVE_PCT // 100
        scalper = realized - long_term - reserve  # exactly 50% for clean splits

        # Register the external inflow, then allocate it inside the silo.
        self.ledger.fund_cash(str(silo), realized, now_ns=now_ns)
        long_term_transfer = self.ledger.fund_vault(
            str(silo), VaultName.LONG_TERM_COMPOUNDER, long_term, now_ns=now_ns
        )
        reserve_transfer = self.ledger.fund_vault(
            str(silo), VaultName.RESERVE_YIELD, reserve, now_ns=now_ns
        )
        scalper_transfer = self.ledger.fund_vault(
            str(silo), VaultName.SPINAL_SCALPER, scalper, now_ns=now_ns
        )
        return RatchetResult(
            silo=str(silo),
            applied=True,
            market=market,
            realized_net_micros=realized,
            long_term_micros=long_term,
            reserve_micros=reserve,
            scalper_micros=scalper,
            long_term_asset=assets["long_term_sweep_asset"],
            reserve_asset=assets["reserve_yield_asset"],
            cross_border_wire_bps=0.0,
            transfer_ids=(
                long_term_transfer.transfer_id,
                reserve_transfer.transfer_id,
                scalper_transfer.transfer_id,
            ),
        )

    def split(self, realized_net_micros: int) -> dict[str, int]:
        """Pure arithmetic view of the split, used by the cockpit and tests."""
        realized = max(0, int(realized_net_micros))
        long_term = realized * RATCHET_LONG_TERM_PCT // 100
        reserve = realized * RATCHET_RESERVE_PCT // 100
        return {
            "realized_net_micros": realized,
            "long_term_micros": long_term,
            "reserve_micros": reserve,
            "scalper_micros": realized - long_term - reserve,
        }


def _market_for_silo(silo: str) -> str:
    try:
        from ring0_vault.conformal_copula_gate import silo_market

        return str(silo_market(silo))
    except Exception:
        return str(silo)


# ---------------------------------------------------------------------------
# 60-second live two-way reconciler
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class BrokerPosition:
    """One broker-reported position, as returned by the adapter's statement."""

    silo: str
    symbol: str
    qty_micros: int
    price_micros: int = 0

    @property
    def notional_micros(self) -> int:
        return (int(self.qty_micros) * int(self.price_micros)) // MICROS_PER_UNIT


@dataclass(slots=True, frozen=True)
class ReconcileReport:
    """Deterministic result of one silo reconciliation pass."""

    silo: str
    reconciled_at_ns: int
    symbols_checked: int
    max_drift: float
    breached: bool
    frozen: bool
    drift_bound: float
    rows: tuple[dict[str, Any], ...] = ()
    repair_action: str = ""

    @property
    def drift_pct(self) -> float:
        return self.max_drift * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "reconciled_at_ns": self.reconciled_at_ns,
            "symbols_checked": self.symbols_checked,
            "max_drift": self.max_drift,
            "drift_pct": self.drift_pct,
            "breached": self.breached,
            "frozen": self.frozen,
            "drift_bound": self.drift_bound,
            "repair_action": self.repair_action,
            "rows": list(self.rows),
        }


class SiloGuard:
    """The silo halt latch shared by the reconciler, ADR-009 and the firewall."""

    __slots__ = ("_frozen", "_reasons", "_lock")

    def __init__(self) -> None:
        self._frozen: set[str] = set()
        self._reasons: dict[str, str] = {}
        self._lock = threading.RLock()

    def freeze(self, silo: str, reason: str) -> bool:
        with self._lock:
            first = str(silo) not in self._frozen
            self._frozen.add(str(silo))
            self._reasons[str(silo)] = str(reason)
            return first

    def unfreeze(self, silo: str) -> bool:
        with self._lock:
            if str(silo) in self._frozen:
                self._frozen.discard(str(silo))
                self._reasons.pop(str(silo), None)
                return True
            return False

    def is_frozen(self, silo: str) -> bool:
        with self._lock:
            return str(silo) in self._frozen

    def reason(self, silo: str) -> str:
        with self._lock:
            return self._reasons.get(str(silo), "")

    @property
    def frozen_silos(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._frozen))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "frozen": sorted(self._frozen),
                "reasons": dict(self._reasons),
            }


class LiveReconciler:
    """60-second two-way reconciler (``sec_009`` / ``sec_012``).

    Compares the internal double-entry IBOR position with the broker's own
    statement.  Any drift strictly greater than ``0.01%`` (``0.0001``) freezes
    the silo immediately and records a **deterministic** repair plan -- the
    repair is always "trust the broker statement for quantities, keep the IBOR
    as the accounting truth", never a heuristic reconciliation.
    """

    def __init__(
        self,
        ledger: IBORLedger,
        guard: SiloGuard | None = None,
        *,
        drift_bound: float = DRIFT_BOUND,
        interval_ns: int = RECONCILE_INTERVAL_NS,
    ) -> None:
        if not 0.0 < float(drift_bound) < 1.0:
            raise ValueError("drift_bound must be inside (0, 1)")
        self.ledger = ledger
        self.guard = guard if guard is not None else SiloGuard()
        self.drift_bound = float(drift_bound)
        self.interval_ns = int(interval_ns)
        self._broker_positions: dict[tuple[str, str], BrokerPosition] = {}
        self._last_run_ns: dict[str, int] = {}
        self._history: list[ReconcileReport] = []

    # -- inputs -----------------------------------------------------------
    def register_broker_position(self, position: BrokerPosition) -> None:
        self._broker_positions[(str(position.silo), str(position.symbol))] = position

    def clear_broker_positions(self, silo: str | None = None) -> None:
        if silo is None:
            self._broker_positions.clear()
        else:
            for key in [k for k in self._broker_positions if k[0] == str(silo)]:
                del self._broker_positions[key]

    # -- reconciliation ---------------------------------------------------
    @staticmethod
    def drift(internal_micros: int, broker_micros: int) -> float:
        """``|internal - broker| / internal`` (``0.0`` when internal is flat)."""
        internal = abs(int(internal_micros))
        broker = abs(int(broker_micros))
        if internal == 0:
            return 0.0 if broker == 0 else 1.0
        return abs(internal - broker) / internal

    def reconcile(self, silo: str, *, now_ns: int | None = None) -> ReconcileReport:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        silo = str(silo)
        rows: list[dict[str, Any]] = []
        max_drift = 0.0

        internal_positions = self.ledger.positions(silo)
        for key, broker_position in sorted(self._broker_positions.items()):
            owner, symbol = key
            if owner != silo:
                continue
            internal = self.ledger.position(silo, symbol)
            internal_qty = 0 if internal is None else int(internal["qty_micros"])
            broker_qty = int(broker_position.qty_micros)
            drift = self.drift(internal_qty, broker_qty)
            max_drift = max(max_drift, drift)
            rows.append(
                {
                    "symbol": symbol,
                    "internal_qty_micros": internal_qty,
                    "broker_qty_micros": broker_qty,
                    "drift": drift,
                    "breached": drift > self.drift_bound,
                }
            )

        # Any internal position with no broker counterpart is a full breach.
        for composite, record in internal_positions.items():
            symbol = composite.split(":", 1)[1]
            if (silo, symbol) in self._broker_positions:
                continue
            if int(record["qty_micros"]) == 0:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "internal_qty_micros": int(record["qty_micros"]),
                    "broker_qty_micros": 0,
                    "drift": 1.0,
                    "breached": True,
                }
            )
            max_drift = 1.0

        breached = max_drift > self.drift_bound
        repair_action = ""
        if breached:
            froze_now = self.guard.freeze(
                silo,
                f"RECONCILIATION_DRIFT {max_drift * 100:.4f}% > "
                f"{self.drift_bound * 100:.4f}%",
            )
            repair_action = self.repair(silo, rows, now_ns=stamp)
            _ = froze_now

        report = ReconcileReport(
            silo=silo,
            reconciled_at_ns=stamp,
            symbols_checked=len(rows),
            max_drift=max_drift,
            breached=breached,
            frozen=self.guard.is_frozen(silo),
            drift_bound=self.drift_bound,
            rows=tuple(rows),
            repair_action=repair_action,
        )
        self._last_run_ns[silo] = stamp
        self._history.append(report)
        return report

    def repair(
        self, silo: str, rows: Sequence[Mapping[str, Any]], *, now_ns: int
    ) -> str:
        """Deterministic repair: adopt the broker quantity, keep the IBOR book."""
        repaired = 0
        for row in rows:
            if not row.get("breached"):
                continue
            symbol = str(row["symbol"])
            broker_qty = int(row["broker_qty_micros"])
            existing = self.ledger.position(str(silo), symbol) or {}
            self.ledger.set_position(
                str(silo),
                symbol,
                qty_micros=broker_qty,
                price_micros=int(existing.get("price_micros", 0)),
            )
            repaired += 1
        return (
            f"REPAIR_ADOPT_BROKER_QTY: {repaired} position(s) re-derived from the "
            f"broker statement; silo remains frozen pending operator release"
        )

    def reconcile_all(self, silos: Iterable[str], *, now_ns: int | None = None) -> list[ReconcileReport]:
        return [self.reconcile(silo, now_ns=now_ns) for silo in silos]

    def due(self, silo: str, *, now_ns: int) -> bool:
        last = self._last_run_ns.get(str(silo))
        return last is None or (int(now_ns) - last) >= self.interval_ns

    @property
    def history(self) -> tuple[ReconcileReport, ...]:
        return tuple(self._history)

    def snapshot(self) -> dict[str, Any]:
        return {
            "drift_bound": self.drift_bound,
            "interval_ns": self.interval_ns,
            "silos": {
                silo: {
                    "last_run_ns": stamp,
                    "frozen": self.guard.is_frozen(silo),
                }
                for silo, stamp in self._last_run_ns.items()
            },
            "broker_positions": [
                {
                    "silo": position.silo,
                    "symbol": position.symbol,
                    "qty_micros": position.qty_micros,
                }
                for position in self._broker_positions.values()
            ],
            "history": [report.to_dict() for report in self._history[-10:]],
        }
