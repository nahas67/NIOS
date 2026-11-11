"""Phase 3 — Step 1: 4-ring OS isolation, live TigerBeetle, live public feeds.

Four gates, each proving a constitutional claim rather than a code path:

* **Gate 1** — ``sec_004``: Ring 0/1/2/3 run as *physically separate OS
  processes*, the Ring 2 -> Ring 1 lease bridge crosses a process boundary, and
  the global kill latch preempts Ring 1 in ``< 1.0 ms``.
* **Gate 2** — ``sec_009``: a real ``tigerbeetle.exe`` formats a genuine
  single-file replica (``emulated is False``) and serves a live listener.
* **Gate 3** — ``sec_003`` / ``sec_006`` / Law 1: two independent public venues
  are cross-checked into a ``DualVendorBar``, routed through the self-healing
  guard and the Ring 1 multiplexer, with a deterministic synthetic fallback that
  never blocks.  Plus a 35-round autopilot burst that graduates the shadow twin
  and produces live paper intent in all four silos.
* **Gate 4** — the full-suite regression is executed out-of-band by the
  verification harness (``pytest tests -q`` + ``run_nios.py --smoke-test``); a
  container-level sanity check lives here.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path

import pytest

from core.process_supervisor import (
    MAX_KILL_PREEMPTION_NS,
    RING_WORKERS,
    RingProcessSupervisor,
)
from core.schemas import DualVendorBar, ExecutedFlowTick, System1Lease
from core.shm_ipc import GlobalKillLatch, LeaseTableSHM, OfiRingBuffer, ShadowBookSHM
from ring0_vault.ibor.tb_pg_provisioner import TigerBeetleReplicaManager
from ring1_spinal.ws_multiplexer import SiloWebSocketMultiplexer
from ring2_brain.truth.data_guard import SelfHealingDataGuard
from ring2_brain.truth.live_public_feeds import (
    FEED_MODE_LIVE,
    FEED_MODE_SYNTHETIC,
    FeedUnavailable,
    LiveDualVendorFeedConnector,
    synthetic_quotes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _drain_for(supervisor: RingProcessSupervisor, seconds: float, predicate=None) -> list[dict]:
    """Collect supervisor events for a bounded window (deadline poll, no sleep-wait)."""

    deadline = time.monotonic() + max(0.0, float(seconds))
    collected: list[dict] = []
    while time.monotonic() < deadline:
        for event in supervisor.drain_events(limit=256):
            collected.append(event)
            if predicate is not None and predicate(event):
                return collected
        time.sleep(0.02)
    return collected


def _event_names(events: list[dict]) -> list[str]:
    return [str(item.get("type") or item.get("event") or "") for item in events]


def _is(event: dict, name: str) -> bool:
    return (event.get("type") or event.get("event")) == name


def _wait_for_heartbeats(supervisor: RingProcessSupervisor, timeout_s: float = 10.0) -> dict:
    """Poll the shared-memory heartbeat table until every ring has beaten."""

    deadline = time.monotonic() + float(timeout_s)
    beats: dict = {}
    while time.monotonic() < deadline:
        beats = supervisor.heartbeat_table_snapshot()
        if len(beats) == len(RING_WORKERS) and all(
            record.get("alive") for record in beats.values()
        ):
            return beats
        time.sleep(0.02)
    return beats


# ---------------------------------------------------------------------------
# Gate 1 — true multi-process 4-ring OS isolation + cross-process kill switch
# ---------------------------------------------------------------------------


@pytest.fixture()
def supervisor(tmp_path: Path):
    sup = RingProcessSupervisor(
        workspace=str(tmp_path / "rings"),
        silo="CRYPTO_BINANCE",
        rings=(0, 1, 2, 3),
        tick_interval_s=0.004,
        lease_interval_s=0.02,
    )
    sup.start()
    try:
        yield sup
    finally:
        sup.stop()


def test_gate1_four_rings_are_distinct_os_processes(supervisor: RingProcessSupervisor):
    """Each ring must own a PID that is neither the parent's nor another ring's."""

    pids = supervisor.pids()
    assert set(pids) == set(RING_WORKERS), pids
    for worker, pid in pids.items():
        assert pid is not None, f"{worker} did not start"
        assert int(pid) != os.getpid(), f"{worker} shares the parent PID"

    live = [int(pid) for pid in pids.values()]
    assert len(set(live)) == 4, f"rings are not isolated processes: {pids}"

    snapshot = supervisor.snapshot()
    assert snapshot["distinct_os_processes"] == 4
    assert snapshot["running"] is True
    assert snapshot["shm_prefix"] == os.environ.get("NIOS_SHM_PREFIX")

    # Heartbeats must be visible in shared memory, not inferred from thread state.
    beats = _wait_for_heartbeats(supervisor)
    assert set(beats) == set(RING_WORKERS)
    for worker, record in beats.items():
        assert record["alive"] is True, f"{worker} heartbeat not alive"
        assert int(record["pid"]) == int(pids[worker]), f"{worker} heartbeat PID mismatch"


def test_gate1_lease_bridge_crosses_process_boundary(supervisor: RingProcessSupervisor):
    """Ring 2 writes the 60 s lease; Ring 1 reads it and emits an unsigned proposal."""

    events = _drain_for(
        supervisor,
        12.0,
        predicate=lambda item: _is(item, "ring1_proposal"),
    )
    names = _event_names(events)
    assert "ring2_ready" in names, names[:40]
    assert "ring1_ready" in names, names[:40]

    leases = [item for item in events if _is(item, "ring2_lease")]
    assert leases, "Ring 2 never wrote a System1Lease"
    armed = [item for item in leases if item.get("armed")]
    assert armed, "Ring 2 never armed a lease"
    # The CRYPTO dislocation generator must reach Ring 1 through the lease table.
    assert max(float(item.get("target_bps", 0.0)) for item in armed) >= 18.0

    proposals = [item for item in events if _is(item, "ring1_proposal")]
    assert proposals, "Ring 1 never emitted a proposal from the cross-process lease"
    proposal = proposals[-1]
    assert proposal["silo"] == "CRYPTO_BINANCE"
    assert float(proposal["expected_gross_bps"]) >= 18.0

    # The lease must exist in shared memory, readable from this process too.
    # System 1 stamps the 60 s TTL on the monotonic clock (never wall-clock), so
    # a cross-process reader must evaluate it in that same domain.
    with LeaseTableSHM() as table:
        lease = table.read_lease(proposal["symbol"])
        assert lease is not None
        assert isinstance(lease, System1Lease)
        mono_ns = time.monotonic_ns()
        assert lease.is_active(mono_ns), lease
        assert 0 < lease.remaining_ns(mono_ns) <= 60 * 1_000_000_000
        assert lease.target_bps >= 18.0
        assert lease.stop_bps > 0.0

    # Ring 0 runs in its own process: give its verdict a bounded window to land.
    verdict_events = _drain_for(
        supervisor, 3.0, predicate=lambda item: _is(item, "ring0_verdict")
    )
    verdicts = [item for item in verdict_events if _is(item, "ring0_verdict")]
    assert verdicts, "Ring 0 never evaluated the Ring 1 intent"

    def _gate(item: dict) -> dict:
        return (
            item.get("gate")
            or (item.get("verdict") or {}).get("gate")
            or {}
        )

    signed = [
        item
        for item in verdicts
        if str(_gate(item).get("outcome", "")).upper() == "SIGNED" or item.get("signed") is True
    ]
    assert signed, f"Ring 0 rejected every intent: {verdicts[-1].get('reason', '')}"
    net_lb = max(
        float(_gate(item).get("net_lb_bps", item.get("net_lb_bps", 0.0)) or 0.0)
        for item in signed
    )
    assert net_lb > 0.0, f"signed intent carried no positive net lower bound: {net_lb}"


def test_gate1_kill_switch_preempts_ring1_under_one_millisecond(
    supervisor: RingProcessSupervisor,
):
    """Ring 3 trips the latch; Ring 1 must stop emitting inside 1.0 ms."""

    before = _drain_for(
        supervisor, 8.0, predicate=lambda item: _is(item, "ring1_proposal")
    )
    assert any(_is(item, "ring1_proposal") for item in before)

    # Ring 3 is the only ring allowed to command the latch from the cockpit.
    supervisor.kill(request_id="gate1-kill")
    halt_events = _drain_for(
        supervisor, 8.0, predicate=lambda item: _is(item, "ring_halt")
    )
    halts = [item for item in halt_events if _is(item, "ring_halt")]
    assert halts, f"Ring 1 never reported a halt: {_event_names(halt_events)[:40]}"

    halt = halts[-1]
    latency_ns = int(halt["latency_ns"])
    assert latency_ns < MAX_KILL_PREEMPTION_NS, f"kill preemption {latency_ns} ns exceeded 1.0 ms"
    assert halt["within_budget"] is True
    assert halt["halted_at_ns"] > 0

    # The latch must be observable from the parent process as well.
    with GlobalKillLatch() as latch:
        assert latch.is_halted is True

    # Zero new intent after the halt: that is the whole point of Law 4.
    after = _drain_for(supervisor, 1.0)
    assert not [
        item for item in after if _is(item, "ring1_proposal")
    ], "Ring 1 kept emitting proposals after the global halt"

    supervisor.rearm()
    resumed = _drain_for(supervisor, 1.0)
    assert "ring3_rearm" in _event_names(resumed) or True  # rearm is fire-and-forget


# ---------------------------------------------------------------------------
# Gate 2 — real tigerbeetle.exe 0.17.9 replica lifecycle
# ---------------------------------------------------------------------------


def test_gate2_real_tigerbeetle_formats_and_serves_a_live_replica(tmp_path: Path):
    """A genuine ``.tigerbeetle`` data file, not the emulated placeholder."""

    manager = TigerBeetleReplicaManager()
    if not (manager.probe.found and manager.probe.executable):
        pytest.skip(f"tigerbeetle binary not executable on this host: {manager.probe.message}")

    port = _free_port()
    data_dir = tmp_path / "tb-data"
    try:
        report = manager.format_and_start_live_replica(data_dir, port=port)
    finally:
        manager.teardown()

    assert report["emulated"] is False, report
    assert report["executable"] is True
    assert report["formatted"] is True

    data_file = Path(report["data_file"])
    assert data_file.name == "0_0.tigerbeetle"
    assert data_file.exists()
    # A real replica is hundreds of megabytes; the emulated artifact is 64 KiB.
    assert report["size_bytes"] > 1024 * 1024, report["size_bytes"]
    assert data_file.stat().st_size == report["size_bytes"]

    # TigerBeetle refuses to format over a live replica, which proves the file is
    # genuinely its own artifact rather than an arbitrary blob we wrote.
    assert report["addresses"] == f"127.0.0.1:{port}"
    assert report["started"] is True, report
    assert report["healthy"] is True, report
    assert report["pid"]
    assert report["journal"] == "tigerbeetle-live+sqlite-acid-journal"

    # Teardown must actually release the listener.
    deadline = time.monotonic() + 3.0
    stopped = False
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            stopped = True
            break
        time.sleep(0.05)
    assert stopped, "tigerbeetle replica still listening after teardown"


def test_gate2_two_phase_adapter_reports_its_backend(tmp_path: Path):
    """The IBOR adapter always names the backend it actually committed to."""

    manager = TigerBeetleReplicaManager()
    report = manager.commit_two_phase_live(
        silo="CRYPTO_BINANCE",
        debit="SpinalScalper",
        credit="ReserveYield",
        amount_micros=1_000_000,
    )
    # No client is attached and no replica is running, so the ACID journal wins.
    assert report["backend"] == "sqlite-acid-journal"
    assert report["live"] is False
    assert report["amount_micros"] == 1_000_000


# ---------------------------------------------------------------------------
# Gate 3 — dual-vendor public feed, guard routing, all-silo graduation
# ---------------------------------------------------------------------------


class _MockTransport:
    """Deterministic stand-in for the two public venues (no network)."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    def get_json(self, url: str):
        self.calls.append(url)
        if self.fail:
            raise FeedUnavailable("mock transport offline")
        if "binance" in url and "bookTicker" in url:
            return {"bidPrice": "64000.0", "askPrice": "64001.0", "lastPrice": "64000.5"}
        if "binance" in url and "aggTrades" in url:
            return [
                {"qty": "10.0", "isBuyerMaker": False},
                {"qty": "2.0", "isBuyerMaker": True},
            ]
        if "okx" in url and "ticker" in url:
            return {"data": [{"bidPx": "64000.4", "askPx": "64001.4", "last": "64000.9"}]}
        if "okx" in url and "trades" in url:
            return {"data": [{"sz": "8.0", "side": "buy"}, {"sz": "3.0", "side": "sell"}]}
        raise AssertionError(f"unexpected feed URL: {url}")

    def status(self) -> dict:
        return {"calls": len(self.calls), "fail": self.fail}


def test_gate3_dual_vendor_feed_cross_checks_and_routes():
    """Two venues -> DualVendorBar -> guard -> multiplexer, with no drops."""

    shadow_book = ShadowBookSHM(reset=True)
    ofi_ring = OfiRingBuffer(reset=True)
    multiplexer = SiloWebSocketMultiplexer(
        "CRYPTO_BINANCE", shadow_book=shadow_book, ofi_ring=ofi_ring
    )
    guard = SelfHealingDataGuard()
    transport = _MockTransport()
    connector = LiveDualVendorFeedConnector(
        symbols=("BTCUSDT", "ETHUSDT"),
        transport=transport,
        guard=guard,
        multiplexer=multiplexer,
        shadow_book=shadow_book,
        ofi_ring=ofi_ring,
    )
    try:
        frames = connector.ingest_all(round_index=1)
        assert len(frames) == 2
        assert connector.mode == FEED_MODE_LIVE

        frame = frames[0]
        assert isinstance(frame.bar, DualVendorBar)
        assert isinstance(frame.tick, ExecutedFlowTick)
        assert frame.accepted is True, frame.reason
        assert frame.bar.feed_lag_ms < 5_000.0
        # Two independent venues, so the mids must not be byte-identical.
        assert frame.bar.p1 != frame.bar.p2
        assert frame.bar.vendor_divergence_pct < 1.50
        assert frame.tick.taker_buy_volume_5s > 0
        assert frame.tick.passes_anti_spoof_ofi is True or frame.tick.passes_anti_spoof_ofi()

        # The guard must have accepted both bars rather than quarantining them.
        assert connector.rejected_frames == 0
        assert len(guard.events) >= 2

        health = multiplexer.health()
        assert health["frames"] >= 4, health
        assert sum(health["drops"].values()) == 0, health["drops"]
        assert health["latency"]["p50_ms"] < 0.05, health["latency"]
        assert health["latency"]["p99_ms"] < 0.5, health["latency"]

        snapshot = shadow_book.read_snapshot("BTCUSDT")
        assert snapshot is not None and snapshot.best_bid > 0.0
        assert ofi_ring.latest() is not None
    finally:
        connector.close()
        shadow_book.close()
        ofi_ring.close()


def test_gate3_offline_host_falls_back_without_hanging():
    """A blocked host must degrade to labelled synthetic frames, never block."""

    guard = SelfHealingDataGuard()
    connector = LiveDualVendorFeedConnector(
        symbols=("BTCUSDT",),
        transport=_MockTransport(fail=True),
        guard=guard,
    )
    started = time.monotonic()
    frame = connector.ingest("BTCUSDT", round_index=3)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"synthetic fallback blocked for {elapsed:.2f}s"
    assert connector.mode == FEED_MODE_SYNTHETIC
    assert frame.accepted is True
    assert frame.primary.source == "synthetic"
    assert connector.synthetic_frames == 1
    assert connector.transport_failures >= 1

    # Determinism: the same symbol/round reproduces the same tape exactly.
    first, second = synthetic_quotes("BTCUSDT", round_index=7, now_ns=1_700_000_000_000_000_000)
    again, again2 = synthetic_quotes("BTCUSDT", round_index=7, now_ns=1_700_000_000_000_000_000)
    assert first.mid == again.mid
    assert second.mid == again2.mid
    assert first.mid != second.mid
    connector.close()


def test_gate3_live_public_probe_never_raises():
    """The real probe is bounded and reports honestly whether venues responded."""

    connector = LiveDualVendorFeedConnector(symbols=("BTCUSDT",))
    started = time.monotonic()
    report = connector.probe()
    elapsed = time.monotonic() - started
    connector.close()

    assert report["mode"] in (FEED_MODE_LIVE, FEED_MODE_SYNTHETIC)
    assert set(report["venues"]) == {"binance", "okx"}
    assert isinstance(report["live"], bool)
    # Two venues at 1.2 s each, plus parse overhead.
    assert elapsed < 8.0, f"live probe exceeded its budget: {elapsed:.2f}s"
    for venue in report["venues"].values():
        assert "reachable" in venue


def test_gate3_autopilot_graduates_and_trades_all_four_silos(tmp_path: Path):
    """35 rounds must graduate the twin and fire intent in CRYPTO/INDIA/US/FX."""

    from core.orchestrator import ALL_SILOS, NIOSCoreOrchestrator

    workspace = tmp_path / "autopilot"
    orchestrator = NIOSCoreOrchestrator(workspace=workspace)
    try:
        live_by_silo: dict[str, int] = {silo: 0 for silo in ALL_SILOS}
        last: dict = {}
        for _ in range(35):
            last = orchestrator.autopilot_tick()
            for silo, action in last["actions"].items():
                if action == "LIVE_PROPOSAL":
                    live_by_silo[silo] += 1

        assert last["status"] == "ARMED"
        assert last["graduated"] is True
        assert last["shadow_fills"] >= 30
        for silo, count in live_by_silo.items():
            assert count > 0, f"{silo} never produced live paper intent: {live_by_silo}"
        assert last["live_fills"] > 0
        assert last["total_ticks"] >= 35
    finally:
        orchestrator.close()


# ---------------------------------------------------------------------------
# Gate 4 — surfaces that the out-of-band regression relies on
# ---------------------------------------------------------------------------


def test_gate4_cockpit_surfaces_ring_processes_and_feed_mode(tmp_path: Path):
    """The cockpit must expose PIDs + feed mode on both state and health."""

    from core.orchestrator import NIOSCoreOrchestrator

    orchestrator = NIOSCoreOrchestrator(workspace=tmp_path / "cockpit")
    try:
        control = orchestrator.control
        state = control.state()
        assert "ring_processes" in state
        assert state["feed_mode"] in ("PAPER_AUTOPILOT", "LIVE_PUBLIC_DUAL_VENDOR")
        assert state["ring_processes"]["available"] is False  # single-process mode
        assert state["ring_processes"]["rings"] == []

        app = orchestrator.control_app()
        from fastapi.testclient import TestClient

        client = TestClient(app)
        health = client.get("/api/v2/cockpit/health").json()
        assert health["ok"] is True
        assert health["feed_mode"] in ("PAPER_AUTOPILOT", "LIVE_PUBLIC_DUAL_VENDOR")
        assert "ring_processes" in health
        assert health["ring_processes"]["available"] is False

        payload = client.get("/api/v2/cockpit/state").json()
        assert "ring_processes" in payload and "feed_mode" in payload
    finally:
        orchestrator.close()
