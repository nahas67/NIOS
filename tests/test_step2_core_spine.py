"""STEP 2 verification suite -- core IPC spine, schemas, Tier 0/1/2 stores.

Six architectural gates:

1. Bitemporal PIT guard (packet + feature lake, writes *and* reads).
2. Anti-spoof executed OFI + MiroFish contagion halt.
3. Tier 0 SeqLock latency (50k reads) and the <1 ms kill switch.
4. Tier 1 JetStream idempotency (duplicate key -> exactly one commit).
5. Tier 2 conformal ``Q_0.98`` over the 200-trade rolling window.
6. 4-ring air-gap AST guard (no Ring 0 vault / credential imports).

Run with::

    pytest tests/test_step2_core_spine.py -v
"""

from __future__ import annotations

import ast
import math
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from core.feature_store import (
    CONFORMAL_MIN_SAMPLES,
    CONFORMAL_WINDOW,
    FeatureStore,
    quantile_linear,
)
from core.nats_bus import (
    STREAM_RING0_EXECUTION,
    STREAM_RING1_SCALP,
    STREAM_RING2_THESES,
    STREAM_RING3_COCKPIT,
    STREAMS,
    LocalJetStreamBus,
    resolve_stream,
)
from core.schemas import (
    SYSTEM1_LEASE_SIZE,
    ContagionFeatures,
    ExecutedFlowTick,
    GroundedFactPacket,
    GroundingDigestError,
    LeaseMode,
    PlainEnglishCockpitCard,
    System1Lease,
    TemporalIntegrityError,
    UnsignedProposal,
    VaultName,
    canonical_pairs_digest,
    net_lower_bound_bps,
)
from core.shm_ipc import (
    KILL_LATCH_SHM_NAME,
    LEASE_TABLE_SHM_NAME,
    GlobalKillLatch,
    HaltReason,
    LeaseTableSHM,
    OfiRingBuffer,
    ShadowBookSHM,
    reset_named_segment,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SYMBOL = "RELIANCE.NS"
SILO = "ZERODHA-INDIA"

#: 2023-11-14T22:13:20Z -- a deterministic, realistic nanosecond epoch.
BASE_NS = 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flow_tick(**overrides: object) -> ExecutedFlowTick:
    values: dict[str, object] = dict(
        symbol=SYMBOL,
        valid_time_ns=BASE_NS,
        knowledge_ts_ns=BASE_NS,
        last_price=2_500.0,
        spread_bps=1.8,
        realized_vol_5s=0.010,
        normal_vol_5s=0.009,
        taker_buy_volume_5s=71.0,
        taker_sell_volume_5s=29.0,
        bid_cancel_to_fill_ratio=1.8,
        ask_cancel_to_fill_ratio=1.2,
    )
    values.update(overrides)
    return ExecutedFlowTick(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test 1 -- bitemporal PIT guard
# ---------------------------------------------------------------------------


def test_1_bitemporal_pit_guard(tmp_path: Path) -> None:
    """No record or query may reach past its own ``as_of_ns``."""

    # -- GroundedFactPacket: future knowledge is rejected at construction ---
    with pytest.raises(TemporalIntegrityError):
        GroundedFactPacket(
            symbol=SYMBOL,
            valid_time_ns=BASE_NS,
            knowledge_ts_ns=BASE_NS + 1_000,
            as_of_ns=BASE_NS,
            metrics={"dcf_per_share": 2_900.0},
        )

    with pytest.raises(TemporalIntegrityError):
        GroundedFactPacket(
            symbol=SYMBOL,
            valid_time_ns=BASE_NS + 1,
            knowledge_ts_ns=BASE_NS,
            as_of_ns=BASE_NS,
            metrics={"dcf_per_share": 2_900.0},
        )

    # -- clean packet: deterministic digest over sorted (key, value) pairs --
    packet_a = GroundedFactPacket(
        symbol=SYMBOL,
        valid_time_ns=BASE_NS,
        knowledge_ts_ns=BASE_NS,
        as_of_ns=BASE_NS,
        metrics={"pe": 24.5, "dcf_per_share": 2_900.0, "roe": 0.18},
    )
    packet_b = GroundedFactPacket(
        symbol=SYMBOL,
        valid_time_ns=BASE_NS,
        knowledge_ts_ns=BASE_NS,
        as_of_ns=BASE_NS,
        metrics={"roe": 0.18, "dcf_per_share": 2_900.0, "pe": 24.5},
    )
    assert packet_a.sha256_digest == packet_b.sha256_digest
    assert len(packet_a.sha256_digest) == 64
    assert packet_a.verify() is True
    assert packet_a.require("pe") == pytest.approx(24.5)

    # A tampered digest must be refused, never silently trusted.
    with pytest.raises(GroundingDigestError):
        GroundedFactPacket(
            symbol=SYMBOL,
            valid_time_ns=BASE_NS,
            knowledge_ts_ns=BASE_NS,
            as_of_ns=BASE_NS,
            metrics={"pe": 24.5},
            sha256_digest="0" * 64,
        )

    # -- FeatureStore: write-side guard ------------------------------------
    store = FeatureStore(tmp_path / "lake_pit")
    try:
        with pytest.raises(TemporalIntegrityError):
            store.append_alpha_factors(
                SILO,
                SYMBOL,
                BASE_NS,
                {"alpha101_mom": 1.25},
                knowledge_ts_ns=BASE_NS + 1,
            )

        with pytest.raises(TemporalIntegrityError):
            store.append_valuation(
                SILO,
                SYMBOL,
                BASE_NS,
                "dcf",
                {"fair_value": 2_900.0},
                knowledge_ts_ns=BASE_NS + 5,
            )

        with pytest.raises(TemporalIntegrityError):
            store.append_conformal_residual(
                "spinal-alpha101", 6.2, BASE_NS, knowledge_ts_ns=BASE_NS + 9
            )

        # -- FeatureStore: read-side guard ---------------------------------
        with pytest.raises(TemporalIntegrityError):
            store.query_alpha_factors(
                SILO, SYMBOL, BASE_NS, knowledge_ts_ns=BASE_NS + 1
            )

        written = store.append_alpha_factors(
            SILO,
            SYMBOL,
            BASE_NS,
            {"alpha101_mom": 1.25, "gtja191_rev": -0.40},
            knowledge_ts_ns=BASE_NS - 60,
            valid_time_ns=BASE_NS - 60,
        )
        assert written == 2

        # A query whose asOf precedes the snapshot must see nothing.
        assert store.query_alpha_factors(SILO, SYMBOL, BASE_NS - 1) == []

        # A query at/after the snapshot sees it.
        visible = store.query_alpha_factors(SILO, SYMBOL, BASE_NS)
        assert {row["factor_name"] for row in visible} == {
            "alpha101_mom",
            "gtja191_rev",
        }
        assert store.get_factor_vector(SILO, SYMBOL, BASE_NS)["alpha101_mom"] == pytest.approx(1.25)

        # -- Parquet cold path honours the same rule ------------------------
        date_key = store._date_from_ns(BASE_NS)
        exported = store.persist_partition(SILO, SYMBOL, date_key)
        assert "alpha_factors" in exported
        assert store.query_partition_alpha_factors(SILO, SYMBOL, date_key, BASE_NS - 1) == []
        assert len(store.query_partition_alpha_factors(SILO, SYMBOL, date_key, BASE_NS)) == 2
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Test 2 -- anti-spoof OFI and contagion halt
# ---------------------------------------------------------------------------


def test_2_anti_spoof_ofi_and_contagion_halt() -> None:
    tick = _flow_tick()
    assert tick.taker_buy_ratio_5s == pytest.approx(0.71, abs=1e-9)
    assert tick.vol_spike_ratio == pytest.approx(0.010 / 0.009)
    assert tick.book_is_clean is True
    assert tick.passes_anti_spoof_ofi("BUY") is True
    assert tick.passes_anti_spoof_ofi("buy") is True
    assert tick.passes_anti_spoof_ofi("SELL") is False

    # 71% buy flow but a 3.2x bid cancel-to-fill ratio == phantom liquidity.
    phantom = replace(tick, bid_cancel_to_fill_ratio=3.2)
    assert phantom.max_cancel_to_fill_ratio == pytest.approx(3.2)
    assert phantom.book_is_clean is False
    assert phantom.passes_anti_spoof_ofi("BUY") is False
    assert phantom.passes_anti_spoof_ofi("SELL") is False

    # Mirror image: aggressive sellers dominate.
    selling = replace(tick, taker_buy_volume_5s=29.0, taker_sell_volume_5s=71.0)
    assert selling.taker_buy_ratio_5s == pytest.approx(0.29)
    assert selling.passes_anti_spoof_ofi("SELL") is True
    assert selling.passes_anti_spoof_ofi("BUY") is False

    # Exactly at the thresholds: 0.65 passes, 0.649... does not.
    at_gate = replace(tick, taker_buy_volume_5s=65.0, taker_sell_volume_5s=35.0)
    assert at_gate.passes_anti_spoof_ofi("BUY") is True
    below_gate = replace(tick, taker_buy_volume_5s=64.9, taker_sell_volume_5s=35.1)
    assert below_gate.passes_anti_spoof_ofi("BUY") is False

    # Zero executed flow can never be proof of liquidity.
    empty = replace(tick, taker_buy_volume_5s=0.0, taker_sell_volume_5s=0.0)
    assert empty.taker_buy_ratio_5s == 0.0
    assert empty.passes_anti_spoof_ofi("BUY") is False

    # 0.1 ms volatility breaker (5s vol >= 3.5x normal).
    spiking = replace(tick, realized_vol_5s=0.040, normal_vol_5s=0.010)
    assert spiking.vol_spike_ratio == pytest.approx(4.0)
    assert spiking.vol_breaker_tripped is True

    # -- MiroFish contagion halt ------------------------------------------
    calm = ContagionFeatures(
        symbol=SYMBOL,
        as_of_ns=BASE_NS,
        crowd_asymmetry_score=0.42,
        cascade_fragility_index=0.749,
        narrative_exhaustion_divergence=-0.10,
    )
    assert calm.requires_ring1_halt is False

    fragile = ContagionFeatures(
        symbol=SYMBOL,
        as_of_ns=BASE_NS,
        crowd_asymmetry_score=0.42,
        cascade_fragility_index=0.75,
        narrative_exhaustion_divergence=-0.10,
    )
    assert fragile.requires_ring1_halt is True

    with pytest.raises(ValueError):
        ContagionFeatures(SYMBOL, BASE_NS, 1.4, 0.5, 0.0)
    with pytest.raises(ValueError):
        ContagionFeatures(SYMBOL, BASE_NS, 0.0, 0.5, -1.4)
    with pytest.raises(ValueError):
        ContagionFeatures(SYMBOL, BASE_NS, 0.0, 1.01, 0.0)


# ---------------------------------------------------------------------------
# Test 3 -- Tier 0 SeqLock latency and kill switch SLA
# ---------------------------------------------------------------------------


def test_3_tier0_seqlock_latency_and_kill_switch() -> None:
    reset_named_segment(LEASE_TABLE_SHM_NAME)
    reset_named_segment(KILL_LATCH_SHM_NAME)

    latch = GlobalKillLatch(reset=True)
    table = LeaseTableSHM(reset=True, kill_latch=latch)
    try:
        now_ns = time.monotonic_ns()
        lease = System1Lease(
            symbol=SYMBOL,
            mode=LeaseMode.LONG_ONLY,
            target_bps=18.0,
            stop_bps=9.0,
            expires_at_ns=now_ns + 30_000_000_000,
            strategy_id_hash=0xDEADBEEFCAFEBABE,
        )

        # -- wire contract --------------------------------------------------
        packed = lease.pack()
        assert len(packed) == SYSTEM1_LEASE_SIZE == 64
        round_trip = System1Lease.unpack(packed)
        assert round_trip.symbol == SYMBOL
        assert round_trip.mode is LeaseMode.LONG_ONLY
        assert round_trip.strategy_id_hash == 0xDEADBEEFCAFEBABE
        assert round_trip.is_active(now_ns) is True

        # 60-second dead-man TTL: too-far expiry and already-expired both die.
        assert lease.is_active(now_ns + 31_000_000_000) is False
        assert lease.with_ttl(now_ns).is_active(now_ns + 59_000_000_000) is True
        assert lease.with_ttl(now_ns).is_active(now_ns + 61_000_000_000) is False
        forged = replace(lease, expires_at_ns=now_ns + 120_000_000_000)
        assert forged.is_active(now_ns) is False

        # -- shared-memory round trip ---------------------------------------
        table.write_lease(lease, now_ns=now_ns)
        observed = table.read_lease(SYMBOL, now_ns)
        assert observed.symbol == SYMBOL
        assert observed.mode is LeaseMode.LONG_ONLY
        assert observed.is_active(now_ns) is True

        # A symbol with no published lease fails closed.
        unknown = table.read_lease("TCS.NS", now_ns)
        assert unknown.mode is LeaseMode.DISARMED
        assert unknown.is_active(now_ns) is False

        # -- 50,000-iteration lock-free latency run --------------------------
        iterations = 50_000
        samples = [0] * iterations
        read = table.read_lease
        counter = time.perf_counter_ns
        for index in range(iterations):
            start = counter()
            snapshot = read(SYMBOL, now_ns)
            samples[index] = counter() - start
        assert snapshot.is_active(now_ns) is True

        samples.sort()
        p50_ns = samples[iterations // 2]
        p99_ns = samples[int(iterations * 0.99)]
        p999_ns = samples[int(iterations * 0.999)]
        p50_ms = p50_ns / 1_000_000
        p99_ms = p99_ns / 1_000_000
        p999_ms = p999_ns / 1_000_000

        print(
            "\n[Tier 0] read_lease over "
            f"{iterations:,} iterations -> p50={p50_ms:.6f} ms | "
            f"p99={p99_ms:.6f} ms | p99.9={p999_ms:.6f} ms"
        )

        # sec_013 contract: 0.005 ms p50 / 0.008 ms p99.
        assert p50_ms <= 0.005, f"p50 {p50_ms:.6f} ms exceeds 0.005 ms"
        assert p99_ms <= 0.008, f"p99 {p99_ms:.6f} ms exceeds 0.008 ms"

        # -- global kill switch < 1.0 ms ------------------------------------
        kill_start = counter()
        latch.halt(HaltReason.MANUAL_KILL_SWITCH, now_ns=now_ns)
        disarmed = table.read_lease(SYMBOL, now_ns)
        kill_elapsed_ms = (counter() - kill_start) / 1_000_000

        assert disarmed.mode is LeaseMode.DISARMED
        assert disarmed.is_active(now_ns) is False
        assert latch.is_halted is True
        assert kill_elapsed_ms < 1.0, (
            f"kill switch took {kill_elapsed_ms:.6f} ms (contract < 1.0 ms)"
        )
        print(
            f"[Tier 0] halt + disarmed read -> {kill_elapsed_ms:.6f} ms "
            "(contract < 1.0 ms)"
        )

        # Re-arming restores authority because the lease itself is untouched.
        latch.arm(now_ns=now_ns)
        assert table.read_lease(SYMBOL, now_ns).is_active(now_ns) is True
    finally:
        table.close()
        latch.close()
        reset_named_segment(LEASE_TABLE_SHM_NAME)
        reset_named_segment(KILL_LATCH_SHM_NAME)


def test_3b_tier0_ring_buffer_and_shadow_book() -> None:
    """The 5s OFI ring buffer and the L2 shadow-twin slot helper round-trip."""
    reset_named_segment("nios-ofi-cancel-ring")
    reset_named_segment("nios-shadow-book")
    ring = OfiRingBuffer(reset=True, n_slots=64)
    book = ShadowBookSHM(reset=True, n_slots=64)
    try:
        tick = _flow_tick(taker_buy_volume_5s=71.0, taker_sell_volume_5s=29.0)
        ring.append(tick, now_ns=BASE_NS)
        latest = ring.latest()
        assert latest is not None
        assert latest.symbol == SYMBOL
        assert latest.taker_buy_ratio_5s == pytest.approx(0.71)

        aggregate = ring.aggregate(SYMBOL, BASE_NS)
        assert aggregate is not None
        assert aggregate.passes_anti_spoof_ofi("BUY") is True
        assert len(ring.window(BASE_NS)) == 1

        book.write_snapshot(SYMBOL, 2_499.5, 2_500.5, 120.0, 80.0, updated_at_ns=BASE_NS)
        snapshot = book.read_snapshot(SYMBOL)
        assert snapshot is not None
        assert snapshot.mid == pytest.approx(2_500.0)
        assert snapshot.spread_bps == pytest.approx(4.0, rel=1e-9)
        assert snapshot.bid_qty == pytest.approx(120.0)
    finally:
        ring.close()
        book.close()
        reset_named_segment("nios-ofi-cancel-ring")
        reset_named_segment("nios-shadow-book")


def test_3c_tier0_create_on_existing_segment_and_prefix_isolation(monkeypatch) -> None:
    """Operator Note #1: concurrent ``pytest`` + live server are collision-free.

    ``create=True`` against a segment that already exists must attach and reset
    in place (never ``FileExistsError``), and ``NIOS_SHM_PREFIX`` must keep a
    test process out of the live server's namespace entirely.
    """
    from core import shm_ipc

    original_prefix = os.environ.get(shm_ipc.SHM_PREFIX_ENV)
    live_prefix = f"nios-live-{os.getpid()}"
    test_prefix = f"nios-test-{os.getpid()}-iso"
    opened: list[tuple[str, object]] = []

    def open_in(prefix: str, factory):
        monkeypatch.setenv(shm_ipc.SHM_PREFIX_ENV, prefix)
        handle = factory()
        opened.append((prefix, handle))
        return handle

    try:
        # ---- live namespace: the three hot segments already exist ----------
        live_latch = open_in(live_prefix, lambda: GlobalKillLatch(reset=True))
        live_table = open_in(
            live_prefix, lambda: LeaseTableSHM(reset=True, kill_latch=live_latch)
        )
        live_ring = open_in(live_prefix, lambda: OfiRingBuffer(reset=True, n_slots=32))

        assert live_latch.name == f"{live_prefix}-global-kill-latch"
        assert live_table.name == f"{live_prefix}-active-leases"
        assert live_ring.uri == f"shm://{live_prefix}-ofi-cancel-ring"
        assert shm_ipc.segment_name(shm_ipc.KILL_LATCH_SHM_NAME) == live_latch.name
        # Idempotent: an already-namespaced name is never prefixed twice.
        assert shm_ipc.segment_name(live_latch.name) == live_latch.name

        now_ns = time.monotonic_ns()
        lease = System1Lease(
            symbol=SYMBOL,
            mode=LeaseMode.LONG_ONLY,
            target_bps=12.0,
            stop_bps=6.0,
            expires_at_ns=now_ns + 30_000_000_000,
            strategy_id_hash=0x5EED5EED5EED5EED,
        )
        live_table.write_lease(lease, now_ns=now_ns)
        live_ring.append(_flow_tick(), now_ns=BASE_NS)
        live_latch.halt(HaltReason.MANUAL_KILL_SWITCH, now_ns=now_ns)
        assert live_latch.is_halted is True
        assert live_table.slots_written == 1
        assert live_ring.cursor == 1

        # ---- create=True on existing segments: attach + reset, no crash ----
        # This is the exact call that raised FileExistsError on Windows before
        # Operator Note #1: pytest must be able to do it while a live server --
        # or simply these handles -- already own the names.
        try:
            reset_latch = open_in(live_prefix, lambda: GlobalKillLatch(reset=True))
            reset_table = open_in(
                live_prefix, lambda: LeaseTableSHM(reset=True, kill_latch=reset_latch)
            )
            reset_ring = open_in(
                live_prefix, lambda: OfiRingBuffer(reset=True, n_slots=32)
            )
        except FileExistsError as exc:  # pragma: no cover - regression guard
            pytest.fail(f"create=True on an existing segment raised: {exc}")

        assert reset_latch.is_halted is False          # header + payload zeroed
        assert reset_table.slots_written == 0
        assert reset_table.read_lease(SYMBOL, now_ns).mode is LeaseMode.DISARMED
        assert reset_ring.cursor == 0
        assert reset_ring.latest() is None

        # ---- prefix isolation ---------------------------------------------
        # Re-halt and re-publish in the live namespace, then prove the test
        # namespace sees none of it.
        reset_latch.halt(HaltReason.MANUAL_KILL_SWITCH, now_ns=now_ns)
        reset_table.write_lease(lease, now_ns=now_ns)

        test_latch = open_in(test_prefix, lambda: GlobalKillLatch(reset=True))
        test_table = open_in(
            test_prefix, lambda: LeaseTableSHM(reset=True, kill_latch=test_latch)
        )
        test_ring = open_in(test_prefix, lambda: OfiRingBuffer(reset=True, n_slots=32))

        assert test_latch.name != reset_latch.name
        assert test_table.name != reset_table.name
        assert test_ring.uri != reset_ring.uri
        assert test_latch.is_halted is False           # live halt is invisible
        assert test_table.read_lease(SYMBOL, now_ns).mode is LeaseMode.DISARMED
        assert test_ring.cursor == 0
        assert reset_latch.is_halted is True           # live state untouched
        assert reset_table.slots_written == 1          # live lease still published
        reset_latch.arm(now_ns=now_ns)                 # re-arm for the read path
        assert reset_table.read_lease(SYMBOL, now_ns).is_active(now_ns) is True
    finally:
        for _prefix, handle in reversed(opened):
            try:
                handle.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        for prefix in {prefix for prefix, _ in opened}:
            monkeypatch.setenv(shm_ipc.SHM_PREFIX_ENV, prefix)
            shm_ipc.unlink_all()
        if original_prefix is None:
            monkeypatch.delenv(shm_ipc.SHM_PREFIX_ENV, raising=False)
        else:
            monkeypatch.setenv(shm_ipc.SHM_PREFIX_ENV, original_prefix)


# ---------------------------------------------------------------------------
# Test 4 -- Tier 1 NATS idempotency
# ---------------------------------------------------------------------------


def test_4_tier1_nats_idempotency(tmp_path: Path) -> None:
    assert STREAMS == (
        STREAM_RING0_EXECUTION,
        STREAM_RING1_SCALP,
        STREAM_RING2_THESES,
        STREAM_RING3_COCKPIT,
    )
    assert STREAM_RING1_SCALP == "stream.ring1.scalp-proposals"
    assert STREAM_RING2_THESES == "stream.ring2.deep-theses"
    assert STREAM_RING0_EXECUTION == "stream.ring0.execution-events"
    assert STREAM_RING3_COCKPIT == "stream.ring3.cockpit-cards"
    assert resolve_stream("ring1.scalp-proposals.RELIANCE") == STREAM_RING1_SCALP

    bus = LocalJetStreamBus()
    try:
        payload = {
            "symbol": SYMBOL,
            "side": "BUY",
            "qty": 12.0,
            "net_lb_bps": 6.3,
        }
        first = bus.publish_idempotent(
            "stream.ring1.scalp-proposals.RELIANCE", payload, "scalp-RELIANCE-001"
        )
        second = bus.publish_idempotent(
            "stream.ring1.scalp-proposals.RELIANCE", payload, "scalp-RELIANCE-001"
        )

        assert first.committed is True and first.duplicate is False
        assert second.committed is False and second.duplicate is True
        assert second.sequence == first.sequence
        assert bus.committed_count(STREAM_RING1_SCALP) == 1
        assert bus.stream_depth(STREAM_RING1_SCALP) == 1
        assert bus.dedupe_hits(STREAM_RING1_SCALP) == 1
        assert len(bus.replay(STREAM_RING1_SCALP)) == 1

        # A retry with a different key is a genuinely new intent.
        third = bus.publish_idempotent(
            "stream.ring1.scalp-proposals.RELIANCE", payload, "scalp-RELIANCE-002"
        )
        assert third.committed is True
        assert third.sequence == 2
        assert bus.committed_count(STREAM_RING1_SCALP) == 2

        # Derived keys are stable for identical payloads.
        derived_a = bus.publish_idempotent("ring2.deep-theses.RELIANCE", payload)
        derived_b = bus.publish_idempotent("ring2.deep-theses.RELIANCE", payload)
        assert derived_a.idempotency_key == derived_b.idempotency_key
        assert bus.committed_count(STREAM_RING2_THESES) == 1

        # -- durable consumer: at-least-once redelivery ---------------------
        consumer = bus.consumer(STREAM_RING1_SCALP, "ring0-executor")
        batch = consumer.fetch(10)
        assert [event.sequence for event in batch] == [1, 2]
        consumer.ack(1)
        assert consumer.cursor == 1
        assert consumer.pending_count == 1

        redelivered = consumer.fetch(10)
        assert [event.sequence for event in redelivered] == [2]
        consumer.ack(2)
        assert consumer.cursor == 2
        assert consumer.fetch(10) == []
    finally:
        bus.close()

    # -- durability across a process-local restart -------------------------
    journal = tmp_path / "jetstream"
    bus_a = LocalJetStreamBus(path=journal)
    bus_a.publish_idempotent("ring0.execution-events.FILL", {"fill": "F-1"}, "fill-1")
    bus_a.close()

    bus_b = LocalJetStreamBus(path=journal)
    try:
        assert bus_b.committed_count(STREAM_RING0_EXECUTION) == 1
        assert bus_b.replay(STREAM_RING0_EXECUTION)[0].payload["fill"] == "F-1"
        replay = bus_b.publish_idempotent(
            "ring0.execution-events.FILL", {"fill": "F-1"}, "fill-1"
        )
        assert replay.duplicate is True
        assert bus_b.committed_count(STREAM_RING0_EXECUTION) == 1
    finally:
        bus_b.close()


# ---------------------------------------------------------------------------
# Test 5 -- Tier 2 conformal Q98 window
# ---------------------------------------------------------------------------


def test_5_tier2_conformal_q98_window(tmp_path: Path) -> None:
    store = FeatureStore(tmp_path / "lake_conformal")
    strategy = "spinal-alpha101"
    try:
        # 200 graded trades, residuals 0.0 .. 19.9 bps in a shuffled order.
        residuals = [((index * 37) % 200) / 10.0 for index in range(CONFORMAL_WINDOW)]
        for index, residual in enumerate(residuals):
            store.append_conformal_residual(
                strategy,
                residual,
                BASE_NS + index * 1_000_000_000,
                trade_id=f"trade-{index:03d}",
                realized_net_bps=6.3,
                is_shadow=index < 40,
            )

        window = store.get_q98_residual(strategy, BASE_NS + 10**12)
        assert window.count == CONFORMAL_WINDOW
        assert window.shadow_count == 40
        assert window.live_count == 160
        assert window.calibrated is True
        assert window.requested_window == CONFORMAL_WINDOW

        expected = quantile_linear(residuals, 0.98)
        numpy = pytest.importorskip("numpy")
        numpy_q98 = float(numpy.quantile(residuals, 0.98))
        assert window.q98_bps == pytest.approx(expected, abs=1e-9)
        assert window.q98_bps == pytest.approx(numpy_q98, abs=1e-9)
        assert window.q98_bps == pytest.approx(19.502, abs=1e-9)

        # A residual added *after* as_of_ns must not change the answer.
        store.append_conformal_residual(
            strategy, 500.0, BASE_NS + 10**13, trade_id="future-trade"
        )
        same_window = store.get_q98_residual(strategy, BASE_NS + 10**12)
        assert same_window.q98_bps == pytest.approx(window.q98_bps, abs=1e-9)
        assert same_window.count == CONFORMAL_WINDOW

        # Rolling: after 10 new trades only the newest 200 are considered.
        for index in range(10):
            store.append_conformal_residual(
                strategy,
                100.0,
                BASE_NS + 10**12 + (index + 1) * 1_000_000_000,
                trade_id=f"tail-{index}",
            )
        rolled = store.get_q98_residual(strategy, BASE_NS + 2 * 10**12)
        assert rolled.count == CONFORMAL_WINDOW
        assert rolled.max_bps == pytest.approx(100.0)
        assert store.count_trades(strategy, BASE_NS + 2 * 10**12) == 210

        # -- cold start fails closed ---------------------------------------
        cold = store.get_q98_residual("never-traded", BASE_NS)
        assert cold.count == 0
        assert math.isinf(cold.q98_bps)
        assert cold.calibrated is False

        thin = store.get_q98_residual(
            strategy, BASE_NS + 20 * 1_000_000_000, min_samples=CONFORMAL_MIN_SAMPLES
        )
        assert thin.count == 21
        assert math.isinf(thin.q98_bps)
        assert thin.calibrated is False

        # -- gate integration: a warm, positive-edge trade deploys ----------
        assert window.net_lower_bound_bps(18.0, 1.8, 2.75, 5.5) == pytest.approx(
            net_lower_bound_bps(18.0, 1.8, 2.75, 5.5, window.q98_bps)
        )
        assert window.net_lower_bound_bps(18.0, 1.8, 2.75, 5.5) < 0.0
        assert window.net_lower_bound_bps(48.0, 1.8, 2.75, 5.5) > 0.0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Test 6 -- 4-ring air-gap AST guard
# ---------------------------------------------------------------------------

HARD_FORBIDDEN_MODULE_TOKENS: tuple[str, ...] = (
    "ring0_vault",
    "key_vault",
    "keyvault",
    "broker_keys",
    "keystore",
)

#: Ring 0 signing/firewall libraries that must never appear outside Ring 0/1.
RING0_LIBRARY_ROOTS: frozenset[str] = frozenset({"nautilus_trader", "tigerbeetle"})

#: Any module touching credential or exchange-specific naming is a *surface*
#: that must be explicitly reviewed, even when it is a read-only data loader.
CREDENTIAL_SURFACE_TOKENS: frozenset[str] = frozenset(
    {
        "credential",
        "credentials",
        "apikey",
        "api_key",
        "secret",
        "token",
        "zerodha",
        "dhan",
        "angel",
        "upstox",
        "longbridge",
        "interactive_brokers",
        "ibkr",
        "binance",
        "okx",
        "bybit",
    }
)

#: Reviewed Ring 2 imports that resolve **read-only market-data** vendor
#: credentials inside the harvested Vibe-Trading loaders.  They are pinned
#: exactly: any new credential import -- or any drift in these names -- fails
#: the gate and must be re-reviewed before Ring 2 may use it.
PINNED_DATA_PLANE_CREDENTIAL_IMPORTS: frozenset[str] = frozenset(
    {
        "longbridge",
        "longbridge.openapi",
        "src.trading.connectors.dhan",
        "src.trading.connectors.dhan.sdk",
        "src.trading.connectors.longbridge.credentials",
        "src.trading.connectors.longbridge.credentials.LongbridgeCredentialError",
        (
            "src.trading.connectors.longbridge.credentials."
            "resolve_longbridge_credentials"
        ),
        "src.trading.connectors.longbridge.sdk",
        "src.trading.connectors.longbridge.sdk.build_sdk_config",
        "src.trading.connectors.zerodha",
        "src.trading.connectors.zerodha.sdk",
    }
)


def _module_parts(module: str) -> list[str]:
    return [part for part in module.lower().replace("\\", ".").split(".") if part]


def _imported_modules(source: str) -> list[str]:
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
            modules.extend(f"{node.module or ''}.{alias.name}" for alias in node.names)
    return modules


def _forbidden_imports(source: str) -> list[str]:
    """Hard air-gap violations: any path into the Ring 0 vault."""
    hits: list[str] = []
    for module in _imported_modules(source):
        parts = _module_parts(module)
        if any(token in parts for token in HARD_FORBIDDEN_MODULE_TOKENS):
            hits.append(module)
    return hits


def _credential_surface_imports(source: str) -> list[str]:
    """Imports that touch credential / exchange naming and must be reviewed."""
    hits: list[str] = []
    for module in _imported_modules(source):
        if CREDENTIAL_SURFACE_TOKENS & set(_module_parts(module)):
            hits.append(module)
    return hits


def test_6_four_ring_air_gap_ast_guard() -> None:
    # Sanity: the detector actually detects.
    assert _forbidden_imports(
        "from ring0_vault.silos.key_vault import KeyVault\n"
    ) == ["ring0_vault.silos.key_vault", "ring0_vault.silos.key_vault.KeyVault"]
    assert _forbidden_imports("import core.schemas\n") == []

    # The process composition roots.  They are deliberately the only files
    # allowed to import every ring: each constructs the four ring processes in a
    # single deterministic harness and neither is ever imported by Ring 2.
    # ``core/orchestrator.py`` boots them in-process; ``core/process_supervisor.py``
    # boots them as four separate OS processes (``sec_004``).  Both are still
    # checked below for broker-adapter leakage.
    composition_roots = ("core/orchestrator.py", "core/process_supervisor.py")

    scanned = 0
    violations: list[tuple[str, str]] = []
    ring2_violations: list[tuple[str, str]] = []
    credential_surface: set[str] = set()

    for root_name in ("core", "ring2_brain"):
        root = REPO_ROOT / root_name
        assert root.is_dir(), f"missing {root}"
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            scanned += 1
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                modules = _imported_modules(source)
            except SyntaxError:  # pragma: no cover - donor syntax
                continue

            if relative in composition_roots:
                continue

            for module in modules:
                parts = _module_parts(module)
                if any(token in parts for token in HARD_FORBIDDEN_MODULE_TOKENS):
                    violations.append((relative, module))
                    if root_name == "ring2_brain":
                        ring2_violations.append((relative, module))
                if parts and parts[0] in RING0_LIBRARY_ROOTS:
                    violations.append((relative, module))

            if root_name == "ring2_brain":
                credential_surface.update(_credential_surface_imports(source))

    assert scanned > 400, f"air-gap scan only saw {scanned} files"
    assert violations == [], f"Ring 0 vault leaked into the air-gap: {violations[:10]}"
    assert ring2_violations == []

    # A composition root may compose rings, but it still may not import a
    # broker adapter or exchange client (Ring 0 adapters stay in silo workers).
    broker_tokens = {
        "india_brokers",
        "nautilus_adapters",
        "fx_mt5",
        "kiteconnect",
        "MetaTrader5",
        "ccxt",
    }
    for composition_root in composition_roots:
        root_source = (REPO_ROOT / composition_root).read_text(encoding="utf-8")
        root_imports = _imported_modules(root_source)
        leaked = [
            module
            for module in root_imports
            if broker_tokens & set(_module_parts(module))
        ]
        assert leaked == [], f"{composition_root} imported a broker adapter: {leaked}"

    # No *unreviewed* credential surface.  The pinned set is exactly the
    # harvested read-only market-data loader allowlist; drift fails the gate.
    unreviewed = credential_surface - PINNED_DATA_PLANE_CREDENTIAL_IMPORTS
    stale = PINNED_DATA_PLANE_CREDENTIAL_IMPORTS - credential_surface
    assert unreviewed == set(), f"unreviewed credential imports in Ring 2: {sorted(unreviewed)}"
    assert stale == set(), f"pinned allowlist is stale: {sorted(stale)}"

    # No credential literals in the substrate either.
    credential_markers = ("API_KEY", "API_SECRET", "ACCESS_TOKEN", "PRIVATE_KEY")
    core_hits: list[tuple[str, str]] = []
    for path in (REPO_ROOT / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in credential_markers:
            if marker in text:
                core_hits.append((path.name, marker))
    assert core_hits == [], f"credential-shaped constants in core/: {core_hits}"

    print(
        f"\n[Air-Gap] parsed {scanned} modules under core/ and ring2_brain/; "
        "0 Ring 0 vault imports, "
        f"{len(credential_surface)} pinned data-plane credential surfaces, "
        "0 unreviewed."
    )


# ---------------------------------------------------------------------------
# Contract smoke tests (wire formats that the other rings will build on)
# ---------------------------------------------------------------------------


def test_0_wire_contracts_smoke() -> None:
    assert SYSTEM1_LEASE_SIZE == 64

    proposal = UnsignedProposal(
        proposal_id="prop-001",
        strategy_id="spinal-alpha101",
        silo=SILO,
        vault=VaultName.SPINAL_SCALPER,
        symbol=SYMBOL,
        side="buy",
        qty=12.0,
        limit_price=2_500.0,
        expected_gross_bps=18.0,
        spread_bps=1.8,
        grounding_hash="a" * 64,
        created_at_ns=BASE_NS,
    )
    assert proposal.side == "BUY"
    assert proposal.vault is VaultName.SPINAL_SCALPER
    assert proposal.notional == pytest.approx(30_000.0)
    assert proposal.notional_cap_pct == pytest.approx(0.05)
    assert proposal.to_dict()["vault"] == "SpinalScalper"

    card = PlainEnglishCockpitCard(
        silo="ZERODHA-INDIA",
        symbol="RELIANCE.NS",
        mode_badge="LIVE CAPITAL",
        story_sentence=(
            "Spinal Reflex bought RELIANCE: Aggressive buyers took 71% of 5s "
            "executed volume with clean book."
        ),
        gross_bps=18.0,
        fee_stt_bps=5.5,
        uncert_q98_bps=6.2,
        net_lb_bps=6.3,
        timestamp_ns=BASE_NS,
    )
    assert card.format_proof_line() == (
        "[LIVE CAPITAL \u2022 ZERODHA-INDIA \u2022 RELIANCE.NS] Spinal Reflex "
        "bought RELIANCE: Aggressive buyers took 71% of 5s executed volume with "
        "clean book. | Gross: +18.0 bps | Fee+STT: 5.5 bps | "
        "Uncert (Q98): 6.2 bps | Net LB: +6.3 bps."
    )
    assert card.to_dict()["proof_line"] == card.format_proof_line()
    assert card.is_net_edge_positive is True

    assert canonical_pairs_digest([("b", 2.0), ("a", 1.0)]) == canonical_pairs_digest(
        [("a", 1), ("b", 2)]
    )
    assert net_lower_bound_bps(18.0, 1.8, 2.75, 5.5, 6.2) == pytest.approx(-1.0)
