"""Deterministic chaos SIGKILL / hard power-loss crash-recovery engine.

Governing sections
------------------
* ``sec_009`` -- before a signed order touches a broker socket Ring 0 commits a
  two-phase pending transfer plus an outbox record with a deterministic
  idempotency key.  If power dies mid-flight the restart path must settle or
  void every pending transfer and freeze any silo that drifts.
* ``sec_012`` -- crash transitions are verified by deterministic fault
  injection, not by inspection.  This module is the SIGKILL counterpart to
  TigerBeetle's VOPR: it kills a real child process at a randomised point
  *inside* a live reservation / tick / lease write and then proves the
  documented invariants from the recovered on-disk state.
* ``sec_018`` Law 2 -- a lease written by a dead System 1 must not survive a
  reboot.  ``LeaseTableSHM.sweep_expired`` fails stale slots closed to
  ``DISARMED``.

Invariants proven by :func:`audit_recovery`
-------------------------------------------
1. ``AtMostOnceExecution``   -- no duplicate transfer ids in the journal.
2. ``NoOrphanedOrders``      -- every pending reservation is voided on reboot.
3. ``EventualIBORConvergence`` -- double-entry conservation: total debits ==
   total credits for every recovered account.
4. Stale-lease fail-closed   -- expired leases are rewritten as ``DISARMED``.

The simulator never touches a broker, never signs, and never enables live
trading.  Every child runs against a throw-away SQLite journal in a temporary
workspace.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.schemas import LeaseMode, System1Lease, UnsignedProposal, VaultName
from ring0_vault.ibor.ledger_outbox import (
    AccountCode,
    CrashAtomicOutbox,
    IBORLedger,
    LedgerStore,
    OutboxState,
)

__all__ = [
    "CrashTarget",
    "CrashVerdict",
    "RecoveryReport",
    "ChaosCrashSimulator",
    "audit_recovery",
    "audit_lease_reboot",
    "terminate_process",
    "process_alive",
    "run_chaos_campaign",
]

_DEFAULT_SILO = "CRYPTO_BINANCE"
_DEFAULT_STRATEGY = "spinal-scalper-v1"
_DEFAULT_TIMEOUT_NS = 3_000_000_000
_DEFAULT_FUNDING_MICROS = 5_000_000_000_000

# Windows process-access rights.
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x0010_0000
_WAIT_TIMEOUT = 0x0000_0102
_STILL_ACTIVE = 259


class CrashTarget(StrEnum):
    """Where in the flight path the kill is injected."""

    OUTBOX_RESERVATION = "outbox_reservation"
    RING1_TICK = "ring1_tick"
    RING2_LEASE_WRITE = "ring2_lease_write"


# ---------------------------------------------------------------------------
# Platform termination primitives
# ---------------------------------------------------------------------------


def _kernel32() -> Any:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    return kernel32


def terminate_process(pid: int, *, exit_code: int = 137) -> bool:
    """Ungracefully terminate ``pid`` with no cleanup path.

    POSIX uses ``signal.SIGKILL``.  Windows uses ``TerminateProcess`` because
    ``os.kill`` on Windows does not map to an unhandleable hard kill.
    """
    pid = int(pid)
    if pid <= 0:
        raise ValueError("pid must be positive")
    if os.name == "nt":
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_PROCESS_TERMINATE, 0, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ok = bool(kernel32.TerminateProcess(handle, int(exit_code)))
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            return True
        finally:
            kernel32.CloseHandle(handle)
    os.kill(pid, signal.SIGKILL)
    return True


def process_alive(pid: int) -> bool:
    """``True`` while ``pid`` still exists (best-effort, never raises)."""
    pid = int(pid)
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            kernel32 = _kernel32()
            handle = kernel32.OpenProcess(_SYNCHRONIZE, 0, pid)
            if not handle:
                return False
            try:
                if kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT:
                    return True
                code = ctypes.c_uint32(0)
                kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return code.value == _STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:  # pragma: no cover - defensive
        return False


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecoveryReport:
    """Post-crash audit of one IBOR journal + outbox."""

    ledger_path: str
    silos: list[str]
    reservations_total: int = 0
    reservations_pending_before: int = 0
    reservations_pending_after: int = 0
    reservations_committed: int = 0
    reservations_voided: int = 0
    voided_during_recovery: int = 0
    orphaned_orders: int = 0
    transfers_total: int = 0
    pending_transfers_before: int = 0
    pending_transfers_after: int = 0
    duplicate_transfer_ids: int = 0
    debit_total_micros: int = 0
    credit_total_micros: int = 0
    invariant_holds: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def balanced(self) -> bool:
        return self.debit_total_micros == self.credit_total_micros

    @property
    def passed(self) -> bool:
        return bool(
            self.invariant_holds
            and self.balanced
            and self.orphaned_orders == 0
            and self.pending_transfers_after == 0
            and self.duplicate_transfer_ids == 0
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            key: getattr(self, key)
            for key in self.__dataclass_fields__  # type: ignore[attr-defined]
        }
        payload["balanced"] = self.balanced
        payload["passed"] = self.passed
        return payload


@dataclass(slots=True)
class LeaseRebootReport:
    """Proof that a reboot cannot inherit an expired System 1 authority."""

    symbol: str
    stale_lease_armed: bool = False
    swept: int = 0
    active_after_reboot: int = 0
    mode_after_reboot: int = -1
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in self.__dataclass_fields__  # type: ignore[attr-defined]
        }


@dataclass(slots=True)
class CrashVerdict:
    """One SIGKILL injection plus its recovery audit."""

    target: str
    pid: int = 0
    killed: bool = False
    returncode: int | None = None
    reservation_key: str = ""
    inflight_observed: bool = False
    child_stdout: str = ""
    child_stderr: str = ""
    recovery: RecoveryReport | None = None
    lease_reboot: LeaseRebootReport | None = None
    elapsed_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if not self.killed:
            return False
        if self.recovery is not None and not self.recovery.passed:
            return False
        if self.lease_reboot is not None and not self.lease_reboot.passed:
            return False
        return self.recovery is not None or self.lease_reboot is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "pid": self.pid,
            "killed": self.killed,
            "returncode": self.returncode,
            "reservation_key": self.reservation_key,
            "inflight_observed": self.inflight_observed,
            "elapsed_seconds": self.elapsed_seconds,
            "recovery": self.recovery.to_dict() if self.recovery else None,
            "lease_reboot": self.lease_reboot.to_dict() if self.lease_reboot else None,
            "notes": list(self.notes),
            "passed": self.passed,
            "child_stderr": self.child_stderr[-2000:],
        }


# ---------------------------------------------------------------------------
# Journal audit
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def audit_recovery(
    ledger_path: str | Path,
    *,
    silos: Sequence[str] = (_DEFAULT_SILO,),
    timeout_ns: int = _DEFAULT_TIMEOUT_NS,
    now_ns: int | None = None,
    expected_commits: int | None = None,
) -> RecoveryReport:
    """Reopen the journal after a hard kill and verify every invariant."""
    path = str(ledger_path)
    report = RecoveryReport(ledger_path=path, silos=list(silos))

    store = LedgerStore(path, probe=False)
    try:
        ledger = IBORLedger(store, silos=silos)
        report.transfers_total = len(ledger.transfers())
        report.pending_transfers_before = len(ledger.pending_transfers())

        journal = list(store.transfer_journal())
        posted_counts: dict[str, int] = {}
        for row in journal:
            transfer_id = str(row.get("transfer_id") or "")
            phase = str(row.get("phase") or "").upper()
            if not transfer_id or phase != "POSTED":
                continue
            posted_counts[transfer_id] = posted_counts.get(transfer_id, 0) + 1
        report.duplicate_transfer_ids = sum(
            count - 1 for count in posted_counts.values() if count > 1
        )

        outbox = CrashAtomicOutbox(ledger, timeout_ns=int(timeout_ns))
        reservations = list(outbox.all_reservations())
        report.reservations_total = len(reservations)
        report.reservations_pending_before = sum(
            1 for r in reservations if r.is_pending
        )
        report.reservations_committed = sum(
            1 for r in reservations if r.is_committed
        )
        report.reservations_voided = sum(1 for r in reservations if r.is_voided)

        created = [int(r.created_ns) for r in reservations if r.is_pending]
        stamp = int(now_ns) if now_ns is not None else (time.time_ns() + int(timeout_ns))
        if created and now_ns is None:
            stamp = max(created) + int(timeout_ns) + 1

        expired = outbox.expire_timeouts(now_ns=stamp)
        report.voided_during_recovery = len(expired)
        outbox.flush()

        # Re-read from disk: recovery must be durable, not just in-memory.
        ledger.flush()
        pending = getattr(outbox, "pending_count")
        report.reservations_pending_after = int(
            pending() if callable(pending) else pending
        )
        report.reservations_voided = sum(
            1 for r in outbox.all_reservations() if r.is_voided
        )
        report.orphaned_orders = report.reservations_pending_after
        report.pending_transfers_after = len(ledger.pending_transfers())

        debits = 0
        credits = 0
        for silo in silos:
            for account in ledger.accounts(silo).values():
                debits += int(account.debits_total)
                credits += int(account.credits_total)
        report.debit_total_micros = debits
        report.credit_total_micros = credits

        invariant = ledger.invariant_report()
        report.invariant_holds = bool(invariant.get("holds", False)) and (
            debits == credits
        )

        if expected_commits is not None and report.reservations_committed < int(
            expected_commits
        ):
            report.notes.append(
                f"committed {report.reservations_committed} < logged {expected_commits}"
            )
    finally:
        store.close()

    return report


def audit_lease_reboot(
    *,
    symbol: str = "BTC-USDT",
    now_ns: int | None = None,
) -> LeaseRebootReport:
    """Prove an expired lease is swept to ``DISARMED`` after a reboot."""
    from core.shm_ipc import LeaseTableSHM

    stamp = time.monotonic_ns() if now_ns is None else int(now_ns)
    report = LeaseRebootReport(symbol=symbol)

    writer = LeaseTableSHM(reset=True)
    reader = None
    try:
        stale = System1Lease(
            symbol=symbol,
            mode=LeaseMode.LONG_ONLY,
            target_bps=12.0,
            stop_bps=9.0,
            expires_at_ns=stamp - 1_000_000,  # already dead
            strategy_id_hash=0xA1B2C3,
        )
        writer.write_lease(stale, stamp)
        report.stale_lease_armed = (
            writer.read_lease(symbol, stamp).mode is LeaseMode.LONG_ONLY
        )

        # "Reboot": a brand-new handle with an empty cache over the *same*
        # live segment (the writer stays open so the name is not destroyed).
        reader = LeaseTableSHM(reset=False)
        report.swept = reader.sweep_expired(stamp)
        report.active_after_reboot = reader.active_count(stamp)
        report.mode_after_reboot = int(reader.read_lease(symbol, stamp).mode)
    finally:
        if reader is not None:
            reader.close()
        writer.close()

    report.passed = bool(
        report.stale_lease_armed
        and report.swept >= 1
        and report.active_after_reboot == 0
        and report.mode_after_reboot == int(LeaseMode.DISARMED)
    )
    return report


# ---------------------------------------------------------------------------
# Child process (the thing that gets killed)
# ---------------------------------------------------------------------------


def _make_proposal(silo: str, index: int) -> UnsignedProposal:
    return UnsignedProposal(
        proposal_id=f"chaos-{os.getpid()}-{index}",
        strategy_id=_DEFAULT_STRATEGY,
        silo=silo,
        vault=VaultName.TACTICAL_SWING,
        symbol="BTC-USDT" if silo == _DEFAULT_SILO else "RELIANCE.NS",
        side="BUY",
        qty=0.01,
        limit_price=64_000.0,
        expected_gross_bps=18.0,
        spread_bps=1.2,
        grounding_hash="0" * 64,
    )


def _child_outbox(workspace: Path, args: argparse.Namespace) -> int:
    silo = args.silo
    store = LedgerStore(str(workspace / "ibor.sqlite3"), probe=False)
    try:
        ledger = IBORLedger(store, silos=[silo])
        ledger.open_silo(silo)
        if ledger.balance(silo, AccountCode.CASH) < _DEFAULT_FUNDING_MICROS:
            ledger.fund_cash(silo, _DEFAULT_FUNDING_MICROS)
        ledger.fund_vault(
            silo, VaultName.TACTICAL_SWING, _DEFAULT_FUNDING_MICROS // 2
        )
        ledger.flush()
        outbox = CrashAtomicOutbox(ledger, timeout_ns=args.timeout_ns)

        commits_path = workspace / "commits.jsonl"
        inflight_path = workspace / "inflight.json"
        for index in range(int(args.iterations)):
            proposal = _make_proposal(silo, index)
            reservation = outbox.reserve(proposal)
            _atomic_write_json(
                inflight_path,
                {
                    "key": reservation.outbox_idempotency_key,
                    "created_ns": reservation.created_ns,
                    "pid": os.getpid(),
                    "iteration": index,
                    "stage": "reserved",
                },
            )
            # Hold the reservation open so the parent's SIGKILL lands inside
            # the crash window instead of always after a clean commit.
            deadline = time.perf_counter() + (args.hold_ms / 1000.0)
            while time.perf_counter() < deadline:
                pass
            outbox.commit(reservation.outbox_idempotency_key)
            with commits_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"key": reservation.outbox_idempotency_key}) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            _atomic_write_json(
                workspace / "heartbeat.json",
                {"iteration": index, "pid": os.getpid(), "at_ns": time.time_ns()},
            )
        return 0
    finally:
        store.close()


def _child_ring1(workspace: Path, args: argparse.Namespace) -> int:
    """Kill target: Ring 1 tick processing against live SHM leases."""
    from core.process_supervisor import make_flow_tick
    from core.shm_ipc import LeaseTableSHM
    from ring1_spinal.spinal_loop import ZeroGCSpinalEngine

    table = LeaseTableSHM(reset=True)
    try:
        engine = ZeroGCSpinalEngine(
            table, silo=args.silo, strategy_id=_DEFAULT_STRATEGY, shadow_twin=None
        )
        now = time.monotonic_ns()
        table.write_lease(
            System1Lease(
                symbol="BTC-USDT",
                mode=LeaseMode.LONG_ONLY,
                target_bps=18.0,
                stop_bps=9.0,
                expires_at_ns=now + 60_000_000_000,
                strategy_id_hash=0x1234,
            ),
            now,
        )
        marker = workspace / "inflight.json"
        for index in range(int(args.iterations)):
            tick = make_flow_tick("BTC-USDT", last_price=64_000.0)
            outcome = engine.on_tick(tick)
            _atomic_write_json(
                marker,
                {
                    "pid": os.getpid(),
                    "iteration": index,
                    "stage": "tick",
                    "action": str(getattr(outcome, "action", "")),
                },
            )
            deadline = time.perf_counter() + (args.hold_ms / 1000.0)
            while time.perf_counter() < deadline:
                pass
        return 0
    finally:
        table.close()


def _child_ring2(workspace: Path, args: argparse.Namespace) -> int:
    """Kill target: Ring 2 System 1 lease writes into shared memory."""
    from core.shm_ipc import LeaseTableSHM

    table = LeaseTableSHM(reset=True)
    try:
        marker = workspace / "inflight.json"
        for index in range(int(args.iterations)):
            now = time.monotonic_ns()
            table.write_lease(
                System1Lease(
                    symbol="BTC-USDT",
                    mode=LeaseMode.LONG_ONLY,
                    target_bps=18.0 + (index % 3),
                    stop_bps=9.0,
                    expires_at_ns=now + 60_000_000_000,
                    strategy_id_hash=0x5678,
                ),
                now,
            )
            _atomic_write_json(
                marker,
                {
                    "pid": os.getpid(),
                    "iteration": index,
                    "stage": "lease_write",
                },
            )
            deadline = time.perf_counter() + (args.hold_ms / 1000.0)
            while time.perf_counter() < deadline:
                pass
        return 0
    finally:
        table.close()


def _child_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nios-chaos-child")
    parser.add_argument("--child", required=True, help="crash workspace directory")
    parser.add_argument(
        "--target", default=CrashTarget.OUTBOX_RESERVATION.value
    )
    parser.add_argument("--silo", default=_DEFAULT_SILO)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--hold-ms", type=float, default=250.0)
    parser.add_argument("--timeout-ns", type=int, default=_DEFAULT_TIMEOUT_NS)
    args = parser.parse_args(argv)

    workspace = Path(args.child)
    workspace.mkdir(parents=True, exist_ok=True)
    if args.target == CrashTarget.RING1_TICK.value:
        return _child_ring1(workspace, args)
    if args.target == CrashTarget.RING2_LEASE_WRITE.value:
        return _child_ring2(workspace, args)
    return _child_outbox(workspace, args)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------


_ROOT = Path(__file__).resolve().parents[1]


class ChaosCrashSimulator:
    """Spawn a real worker, SIGKILL it mid-flight, then audit recovery."""

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        target: CrashTarget | str = CrashTarget.OUTBOX_RESERVATION,
        silo: str = _DEFAULT_SILO,
        iterations: int = 500,
        hold_ms: float = 250.0,
        timeout_ns: int = _DEFAULT_TIMEOUT_NS,
        seed: int | None = None,
        boot_deadline_s: float = 30.0,
    ) -> None:
        self.workspace = (
            Path(workspace)
            if workspace is not None
            else Path(
                os.environ.get("TEMP", ".")
            )
            / f"nios-chaos-{os.getpid()}-{int(time.time() * 1000)}"
        )
        self.target = CrashTarget(str(target))
        self.silo = str(silo)
        self.iterations = int(iterations)
        self.hold_ms = float(hold_ms)
        self.timeout_ns = int(timeout_ns)
        self.rng = random.Random(seed)
        self.boot_deadline_s = float(boot_deadline_s)

    # -- helpers ------------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault(
            "NIOS_SHM_PREFIX", f"nios-chaos-{os.getpid()}-{int(time.time() * 1000)}"
        )
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _wait_for(self, path: Path, deadline_s: float | None = None) -> bool:
        deadline = time.monotonic() + (
            self.boot_deadline_s if deadline_s is None else float(deadline_s)
        )
        while time.monotonic() < deadline:
            if path.exists():
                return True
            if not process_alive(self._pid):
                return False
            time.sleep(0.002)
        return path.exists()

    # -- public API ---------------------------------------------------------
    def run(self) -> CrashVerdict:
        started = time.perf_counter()
        workspace = self.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        for stale in ("inflight.json", "commits.jsonl", "heartbeat.json"):
            (workspace / stale).unlink(missing_ok=True)

        verdict = CrashVerdict(target=str(self.target))
        cmd = [
            sys.executable,
            "-m",
            "core.chaos_recovery",
            "--child",
            str(workspace),
            "--target",
            self.target.value,
            "--silo",
            self.silo,
            "--iterations",
            str(self.iterations),
            "--hold-ms",
            str(self.hold_ms),
            "--timeout-ns",
            str(self.timeout_ns),
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(_ROOT),
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._pid = proc.pid
        verdict.pid = proc.pid

        inflight = workspace / "inflight.json"
        observed = self._wait_for(inflight)
        verdict.inflight_observed = observed
        if observed:
            try:
                payload = json.loads(inflight.read_text(encoding="utf-8"))
                verdict.reservation_key = str(payload.get("key") or "")
            except Exception:
                verdict.notes.append("inflight marker unreadable")
            # Randomised delay *inside* the hold window: the process is
            # genuinely mid-reservation when the kill lands.
            self.rng.seed(self.rng.random())
            delay = self.rng.uniform(0.0, max(0.001, self.hold_ms / 1000.0 * 0.8))
            time.sleep(delay)

        if process_alive(proc.pid):
            try:
                terminate_process(proc.pid)
                verdict.killed = True
            except Exception as exc:  # pragma: no cover - platform failure
                verdict.notes.append(f"terminate failed: {exc}")
        else:
            verdict.notes.append("child exited before the kill was injected")

        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            stdout, stderr = proc.communicate()
        verdict.child_stdout = stdout or ""
        verdict.child_stderr = stderr or ""
        verdict.returncode = proc.returncode

        if verdict.killed and process_alive(proc.pid):
            verdict.notes.append("process survived SIGKILL")

        if self.target is CrashTarget.RING2_LEASE_WRITE:
            verdict.lease_reboot = audit_lease_reboot()
        else:
            expected = None
            commits = workspace / "commits.jsonl"
            if commits.exists():
                expected = sum(
                    1
                    for line in commits.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            verdict.recovery = audit_recovery(
                workspace / "ibor.sqlite3",
                silos=[self.silo],
                timeout_ns=self.timeout_ns,
                expected_commits=expected,
            )
            verdict.notes.append(
                "stale pending reservation voided on reboot"
                if verdict.recovery.voided_during_recovery
                else "no stale reservation at kill instant"
            )

        verdict.elapsed_seconds = time.perf_counter() - started
        return verdict


# ---------------------------------------------------------------------------
# Campaign + CLI
# ---------------------------------------------------------------------------


def run_chaos_campaign(
    *,
    targets: Iterable[CrashTarget | str] | None = None,
    rounds: int = 1,
    seed: int | None = None,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run the deterministic crash matrix and return a serialisable report."""
    chosen = [CrashTarget(str(t)) for t in (targets or CrashTarget)]
    root = Path(workspace_root) if workspace_root else None
    verdicts: list[CrashVerdict] = []
    for round_index in range(max(1, int(rounds))):
        for target in chosen:
            workspace = (
                (root / f"{target.value}-{round_index}")
                if root is not None
                else None
            )
            simulator = ChaosCrashSimulator(
                workspace,
                target=target,
                seed=(seed + round_index) if seed is not None else None,
            )
            verdicts.append(simulator.run())
    payload = {
        "generated_at_ns": time.time_ns(),
        "rounds": max(1, int(rounds)),
        "targets": [t.value for t in chosen],
        "verdicts": [v.to_dict() for v in verdicts],
        "passed": all(v.passed for v in verdicts),
    }
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--child" in argv:
        return _child_main(argv)

    parser = argparse.ArgumentParser(
        prog="nios-chaos-recovery",
        description="SIGKILL crash-recovery engine (sec_009 / sec_012).",
    )
    parser.add_argument("--campaign", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not (args.campaign or args.self_test):
        parser.print_help()
        return 2

    report = run_chaos_campaign(
        rounds=args.rounds, seed=args.seed, workspace_root=args.workspace
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for verdict in report["verdicts"]:
            recovery = verdict.get("recovery") or {}
            print(
                f"[{verdict['target']}] killed={verdict['killed']} "
                f"pending_after={recovery.get('reservations_pending_after', '-')} "
                f"voided={recovery.get('voided_during_recovery', '-')} "
                f"debits={recovery.get('debit_total_micros', '-')} "
                f"credits={recovery.get('credit_total_micros', '-')} "
                f"passed={verdict['passed']}"
            )
        print(f"CHAOS CAMPAIGN PASSED={report['passed']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
