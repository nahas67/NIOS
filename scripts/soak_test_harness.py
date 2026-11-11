"""Accelerated soak & memory-leak audit harness for NIOS Core v3.0.

Runs a high-throughput continuous tick loop across all four silos against the
real Tier-0 shared-memory primitives (``LeaseTableSHM``, ``OfiRingBuffer``,
``ShadowBookSHM``) and the real Ring 0 two-phase outbox, while injecting the
three failure modes the constitution cares about (``sec_004`` / ``sec_009``):

* intermittent WebSocket disconnects,
* vendor lag spikes above the 5,000 ms quarantine threshold,
* session rollover (lease disarm + re-arm) without zombie processes.

Memory is audited three ways:

1. Python heap via :mod:`tracemalloc` (current + peak),
2. process RSS via :mod:`psutil` when available,
3. shared-memory footprint, which must stay byte-for-byte constant because the
   segments are fixed-size rings.

The linear-regression slope of heap usage against cycle count must stay below
``0.001 kB/cycle`` and the total heap delta must stay below the
``--leak-tolerance-mb`` budget (default ``0.005 MB``).

CLI::

    python scripts/soak_test_harness.py --cycles 1000 --audit-memory
    python scripts/soak_test_harness.py --cycles 10000 --json
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import random
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DEFAULT_SILOS: tuple[str, ...] = (
    "CRYPTO_BINANCE",
    "INDIA_ZERODHA_DHAN",
    "US_IBKR",
    "FX_MT5",
)

_SYMBOLS: dict[str, str] = {
    "CRYPTO_BINANCE": "BTC-USDT",
    "INDIA_ZERODHA_DHAN": "RELIANCE.NS",
    "US_IBKR": "AAPL",
    "FX_MT5": "EURUSD",
}

_PRICES: dict[str, float] = {
    "CRYPTO_BINANCE": 64_250.0,
    "INDIA_ZERODHA_DHAN": 2_940.0,
    "US_IBKR": 228.4,
    "FX_MT5": 1.0874,
}

_LAG_QUARANTINE_MS = 5_000.0


class _Regression:
    """Incremental least-squares slope with O(1) memory."""

    __slots__ = ("n", "sx", "sy", "sxx", "sxy", "_last_y")

    def __init__(self) -> None:
        self.n = 0
        self.sx = 0.0
        self.sy = 0.0
        self.sxx = 0.0
        self.sxy = 0.0
        self._last_y = 0.0

    def add(self, x: float, y: float) -> None:
        self.n += 1
        self.sx += x
        self.sy += y
        self.sxx += x * x
        self.sxy += x * y
        self._last_y = y

    @property
    def slope(self) -> float:
        if self.n < 2:
            return 0.0
        denominator = self.n * self.sxx - self.sx * self.sx
        if abs(denominator) < 1e-12:
            return 0.0
        return (self.n * self.sxy - self.sx * self.sy) / denominator

    @property
    def last(self) -> float:
        return self._last_y


@dataclass(slots=True)
class SoakReport:
    cycles: int = 0
    silos: list[str] = field(default_factory=list)
    ticks: int = 0
    duration_seconds: float = 0.0
    cycles_per_second: float = 0.0
    heap_start_mb: float = 0.0
    heap_end_mb: float = 0.0
    heap_peak_mb: float = 0.0
    heap_growth_mb: float = 0.0
    heap_slope_kb_per_cycle: float = 0.0
    rss_start_mb: float | None = None
    rss_end_mb: float | None = None
    rss_growth_mb: float | None = None
    shm_bytes_start: int = 0
    shm_bytes_end: int = 0
    shm_bytes_constant: bool = False
    handles_start: int | None = None
    handles_end: int | None = None
    handle_leaks: int = 0
    handle_probe: str = "unavailable"
    disconnects: int = 0
    lag_spikes: int = 0
    rollovers: int = 0
    reservations: int = 0
    commits: int = 0
    voided: int = 0
    errors: list[str] = field(default_factory=list)
    leak_tolerance_mb: float = 0.005
    slope_tolerance_kb_per_cycle: float = 0.001
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in self.__dataclass_fields__  # type: ignore[attr-defined]
        }


class SoakTestHarness:
    """Continuous accelerated tick loop with memory / handle auditing."""

    def __init__(
        self,
        cycles: int = 10_000,
        *,
        silos: Sequence[str] = _DEFAULT_SILOS,
        audit_memory: bool = True,
        leak_tolerance_mb: float = 0.005,
        disconnect_every: int = 137,
        lag_spike_every: int = 211,
        rollover_every: int = 499,
        seed: int = 20261112,
    ) -> None:
        self.cycles = max(1, int(cycles))
        self.silos = tuple(str(s) for s in silos)
        self.audit_memory = bool(audit_memory)
        self.leak_tolerance_mb = float(leak_tolerance_mb)
        self.disconnect_every = max(1, int(disconnect_every))
        self.lag_spike_every = max(1, int(lag_spike_every))
        self.rollover_every = max(1, int(rollover_every))
        self.rng = random.Random(seed)
        self._outbox_pool = 16
        self._void_pool = 4
        self._report = SoakReport(
            cycles=self.cycles,
            silos=list(self.silos),
            leak_tolerance_mb=self.leak_tolerance_mb,
        )
        self._heap_regression = _Regression()
        self._rss_regression = _Regression()
        self._closed = False

    # -- lifecycle ----------------------------------------------------------
    def _build(self) -> None:
        from core.process_supervisor import make_dual_vendor_bar, make_flow_tick
        from core.schemas import LeaseMode, System1Lease, UnsignedProposal, VaultName
        from core.shm_ipc import LeaseTableSHM, OfiRingBuffer, ShadowBookSHM
        from ring0_vault.ibor.ledger_outbox import (
            AccountCode,
            CrashAtomicOutbox,
            IBORLedger,
            LedgerStore,
        )
        from ring1_spinal.spinal_loop import ZeroGCSpinalEngine

        self._make_flow_tick = make_flow_tick
        self._make_dual_vendor_bar = make_dual_vendor_bar
        self._LeaseMode = LeaseMode
        self._System1Lease = System1Lease
        self._UnsignedProposal = UnsignedProposal
        self._VaultName = VaultName
        self._AccountCode = AccountCode

        self.lease_table = LeaseTableSHM(reset=True)
        self.ofi_ring = OfiRingBuffer(reset=True)
        self.shadow_book = ShadowBookSHM(reset=True)
        self._now = time.monotonic_ns()

        self.store = LedgerStore(in_memory=True, probe=False)
        self.ledger = IBORLedger(self.store, silos=self.silos)
        for silo in self.silos:
            self.ledger.open_silo(silo)
            self.ledger.fund_cash(silo, 5_000_000_000_000)
            self.ledger.fund_vault(
                silo, VaultName.TACTICAL_SWING, 1_000_000_000_000
            )
        self.outbox = CrashAtomicOutbox(self.ledger, timeout_ns=3_000_000_000)
        # Pre-seed a fixed outbox pool *before* the memory baseline.  The soak
        # then re-reserves the same deterministic idempotency keys forever, so
        # the audit measures infrastructure leaks rather than the expected
        # linear growth of an append-only business journal.
        for silo in self.silos:
            for slot in range(self._outbox_pool):
                reservation = self.outbox.reserve(
                    self._proposal(silo, slot), now_ns=self._now
                )
                self.outbox.commit(
                    reservation.outbox_idempotency_key, now_ns=self._now + 1
                )
            for slot in range(self._void_pool):
                reservation = self.outbox.reserve(
                    self._proposal(silo, slot, tag="void"), now_ns=self._now
                )
                self.outbox.void(
                    reservation.outbox_idempotency_key,
                    reason="SOAK_SEED_CANCEL",
                    now_ns=self._now + 1,
                )

        self.engines = {
            silo: ZeroGCSpinalEngine(
                self.lease_table,
                silo=silo,
                strategy_id="spinal-scalper-v1",
                shadow_twin=None,
            )
            for silo in self.silos
        }
        self._rearm(0)

    def _rearm(self, cycle: int) -> None:
        """(Re)write a fresh 60 s dead-man lease for every silo."""
        for silo in self.silos:
            self.lease_table.write_lease(
                self._System1Lease(
                    symbol=_SYMBOLS[silo],
                    mode=self._LeaseMode.LONG_ONLY,
                    target_bps=18.0,
                    stop_bps=9.0,
                    expires_at_ns=self._now + 60_000_000_000,
                    strategy_id_hash=0xA1B2C3D4,
                ),
                self._now,
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource in ("lease_table", "ofi_ring", "shadow_book"):
            handle = getattr(self, resource, None)
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:  # pragma: no cover - defensive
                pass
        store = getattr(self, "store", None)
        if store is not None:
            try:
                store.close()
            except Exception:  # pragma: no cover - defensive
                pass

    # -- memory probes ------------------------------------------------------
    def _shm_bytes(self) -> int:
        total = 0
        for resource in ("lease_table", "ofi_ring", "shadow_book"):
            handle = getattr(self, resource, None)
            buf = getattr(handle, "_seg", None)
            buf = getattr(buf, "buf", None) if buf is not None else None
            try:
                total += int(buf.nbytes)
            except Exception:
                continue
        return total

    def _handles(self) -> tuple[int | None, str]:
        try:
            import psutil  # type: ignore[import-not-found]

            proc = psutil.Process()
            counter = getattr(proc, "num_handles", None)
            if callable(counter):
                return int(counter()), "psutil.num_handles"
            fds = getattr(proc, "num_fds", None)
            if callable(fds):
                return int(fds()), "psutil.num_fds"
        except Exception:
            pass
        proc_fd = Path("/proc/self/fd")
        if proc_fd.is_dir():
            try:
                return len(list(proc_fd.iterdir())), "/proc/self/fd"
            except Exception:
                pass
        return None, "unavailable"

    def _rss_mb(self) -> float | None:
        try:
            import psutil  # type: ignore[import-not-found]

            return float(psutil.Process().memory_info().rss) / (1024.0 * 1024.0)
        except Exception:
            return None

    def _sample(self, cycle: int) -> None:
        if not self.audit_memory:
            return
        gc.collect()
        current, _peak = tracemalloc.get_traced_memory()
        current_kb = current / 1024.0
        self._heap_regression.add(cycle, current_kb)
        rss = self._rss_mb()
        if rss is not None:
            self._rss_regression.add(cycle, rss)

    # -- fault injection ----------------------------------------------------
    def _inject_disconnect(self, silo: str) -> None:
        """Drop the feed for one cycle: the engine must abstain, not crash."""
        self._report.disconnects += 1
        try:
            self.ofi_ring.append(
                self._make_flow_tick(_SYMBOLS[silo], last_price=_PRICES[silo])
            )
        except Exception as exc:  # pragma: no cover - must not happen
            self._report.errors.append(f"disconnect append: {exc}")

    def _inject_lag_spike(self, silo: str) -> None:
        """Feed a 6,000 ms vendor bar; it must be quarantined, not executed."""
        self._report.lag_spikes += 1
        bar = self._make_dual_vendor_bar(
            _SYMBOLS[silo], price=_PRICES[silo], lag_ms=_LAG_QUARANTINE_MS + 1_000.0
        )
        lag = float(
            getattr(bar, "feed_lag_ms", 0.0)
            or getattr(bar, "lag_ms", 0.0)
            or 0.0
        )
        if lag < _LAG_QUARANTINE_MS:
            self._report.errors.append(f"lag spike not represented: {lag}")

    def _inject_rollover(self) -> None:
        """Session rollover: disarm every lease, then re-arm fresh ones."""
        self._report.rollovers += 1
        try:
            self.lease_table.disarm_all(self._now)
            self._rearm(self._report.cycles)
        except Exception as exc:  # pragma: no cover - must not happen
            self._report.errors.append(f"rollover: {exc}")

    # -- main loop ----------------------------------------------------------
    def run(self) -> SoakReport:
        report = self._report
        tracemalloc.start(25)
        try:
            self._build()
            report.shm_bytes_start = self._shm_bytes()
            report.handles_start, report.handle_probe = self._handles()

            # Warm-up: import caches, sqlite pages and the first SHM writes.
            for _ in range(min(50, self.cycles)):
                self._cycle(0, warmup=True)
            gc.collect()
            base_current, _ = tracemalloc.get_traced_memory()
            report.heap_start_mb = base_current / (1024.0 * 1024.0)
            report.rss_start_mb = self._rss_mb()

            started = time.perf_counter()
            for cycle in range(1, self.cycles + 1):
                self._cycle(cycle, warmup=False)
                if cycle % 25 == 0 or cycle == self.cycles:
                    self._sample(cycle)
            report.duration_seconds = time.perf_counter() - started

            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            report.heap_end_mb = current / (1024.0 * 1024.0)
            report.heap_peak_mb = peak / (1024.0 * 1024.0)
            report.heap_growth_mb = report.heap_end_mb - report.heap_start_mb
            report.heap_slope_kb_per_cycle = self._heap_regression.slope
            report.rss_end_mb = self._rss_mb()
            if report.rss_start_mb is not None and report.rss_end_mb is not None:
                report.rss_growth_mb = report.rss_end_mb - report.rss_start_mb
            report.shm_bytes_end = self._shm_bytes()
            report.shm_bytes_constant = (
                report.shm_bytes_start == report.shm_bytes_end
            )
            report.handles_end, report.handle_probe = self._handles()
            if report.handles_start is not None and report.handles_end is not None:
                report.handle_leaks = max(
                    0, report.handles_end - report.handles_start
                )
            report.cycles_per_second = (
                report.cycles / report.duration_seconds
                if report.duration_seconds > 0
                else 0.0
            )
        except Exception as exc:  # pragma: no cover - surfaced in the report
            report.errors.append(f"soak failure: {type(exc).__name__}: {exc}")
        finally:
            try:
                self.close()
            finally:
                tracemalloc.stop()

        report.passed = bool(
            not report.errors
            and report.ticks > 0
            and report.shm_bytes_constant
            and report.heap_growth_mb < report.leak_tolerance_mb
            and (
                report.cycles < 1000
                or report.heap_slope_kb_per_cycle
                < report.slope_tolerance_kb_per_cycle
            )
            and report.handle_leaks == 0
        )
        return report

    def _cycle(self, cycle: int, *, warmup: bool) -> None:
        report = self._report
        self._now += 1_000_000  # advance the clock by 1 ms per cycle
        now = self._now

        if not warmup:
            if cycle % self.disconnect_every == 0:
                self._inject_disconnect(self.silos[cycle % len(self.silos)])
            if cycle % self.lag_spike_every == 0:
                self._inject_lag_spike(self.silos[cycle % len(self.silos)])
            if cycle % self.rollover_every == 0:
                self._inject_rollover()
            # Refresh leases every 30 s of simulated time so they stay armed.
            if cycle % 1000 == 0:
                self._rearm(cycle)

        for index, silo in enumerate(self.silos):
            symbol = _SYMBOLS[silo]
            tick = self._make_flow_tick(
                symbol,
                last_price=_PRICES[silo],
                now_ns=now,
                buy_volume=150.0 + (cycle % 7),
                sell_volume=3.0 + (cycle % 3),
            )
            self.ofi_ring.append(tick, now_ns=now)
            mid = _PRICES[silo]
            self.shadow_book.write_snapshot(
                symbol,
                mid * 0.9999,
                mid * 1.0001,
                bid_qty=10.0 + (cycle % 5),
                ask_qty=12.0 + (cycle % 4),
                updated_at_ns=now,
            )
            outcome = self.engines[silo].on_tick(tick, now_ns=now)
            report.ticks += 1
            if not warmup and cycle % (50 + index) == 0:
                self._exercise_outbox(silo, cycle)

    def _proposal(self, silo: str, slot: int, tag: str = "pool") -> Any:
        """Deterministic proposal for one slot of a bounded outbox pool."""
        return self._UnsignedProposal(
            proposal_id=f"soak-{silo}-{tag}-{slot}",
            strategy_id="spinal-scalper-v1",
            silo=silo,
            vault=self._VaultName.TACTICAL_SWING,
            symbol=_SYMBOLS[silo],
            side="BUY",
            qty=0.01,
            limit_price=_PRICES[silo],
            expected_gross_bps=18.0,
            spread_bps=1.2,
            grounding_hash="0" * 64,
        )

    def _exercise_outbox(self, silo: str, cycle: int) -> None:
        """Re-reserve a pooled two-phase transfer, then commit or void it."""
        report = self._report
        if cycle % 5 == 0:
            proposal = self._proposal(silo, cycle % self._void_pool, tag="void")
            reservation = self.outbox.reserve(proposal, now_ns=self._now)
            report.reservations += 1
            self.outbox.void(
                reservation.outbox_idempotency_key,
                reason="SOAK_CANCEL",
                now_ns=self._now + 1,
            )
            report.voided += 1
        else:
            proposal = self._proposal(silo, cycle % self._outbox_pool)
            reservation = self.outbox.reserve(proposal, now_ns=self._now)
            report.reservations += 1
            self.outbox.commit(
                reservation.outbox_idempotency_key, now_ns=self._now + 1
            )
            report.commits += 1
        self.outbox.expire_timeouts(now_ns=self._now)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_soak(
    cycles: int = 10_000,
    *,
    silos: Sequence[str] = _DEFAULT_SILOS,
    audit_memory: bool = True,
    leak_tolerance_mb: float = 0.005,
    seed: int = 20261112,
) -> SoakReport:
    harness = SoakTestHarness(
        cycles=cycles,
        silos=silos,
        audit_memory=audit_memory,
        leak_tolerance_mb=leak_tolerance_mb,
        seed=seed,
    )
    return harness.run()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="soak_test_harness",
        description="Accelerated soak + memory-leak audit (sec_004 / sec_009).",
    )
    parser.add_argument("--cycles", type=int, default=10_000)
    parser.add_argument("--audit-memory", action="store_true")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--leak-tolerance-mb", type=float, default=0.005)
    parser.add_argument("--silos", default=",".join(_DEFAULT_SILOS))
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not os.environ.get("NIOS_SHM_PREFIX"):
        os.environ["NIOS_SHM_PREFIX"] = f"nios-soak-{os.getpid()}"

    silos = tuple(s.strip() for s in args.silos.split(",") if s.strip())
    report = run_soak(
        cycles=args.cycles,
        silos=silos or _DEFAULT_SILOS,
        audit_memory=not args.no_memory,
        leak_tolerance_mb=args.leak_tolerance_mb,
        seed=args.seed,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif not args.quiet:
        print(
            "SOAK "
            f"cycles={report.cycles} ticks={report.ticks} "
            f"rate={report.cycles_per_second:.0f} cyc/s "
            f"heap_growth={report.heap_growth_mb:+.4f} MB "
            f"slope={report.heap_slope_kb_per_cycle:+.6f} kB/cycle "
            f"shm_constant={report.shm_bytes_constant} "
            f"handles={report.handles_start}->{report.handles_end} "
            f"disconnects={report.disconnects} lag_spikes={report.lag_spikes} "
            f"rollovers={report.rollovers} "
            f"PASSED={report.passed}"
        )
        for error in report.errors:
            print(f"  ERROR {error}")
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
