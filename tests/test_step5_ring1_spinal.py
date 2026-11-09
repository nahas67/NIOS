"""STEP 5 verification -- Ring 1 Spinal Reflex (Tier 0).

Gates
-----
1. Tier-0 latency + throughput SLA: ``p50 <= 0.088 ms``, ``p99 <= 0.140 ms``,
   ``>= 25,000`` ticks/sec with the cyclic collector disabled (``sec_001``,
   ``sec_007``, ``sec_010``, ``sec_013``).
2. ``0.1 ms`` microstructure circuit breaker: 3.5x volatility spike, two
   consecutive adverse maker fills inside 500 ms, spread blowout -- each trips
   the deterministic reason code and disarms the SHM lease in ``< 0.10 ms``.
3. Law 1 anti-spoofing: high cancel-to-fill or weak taker-buy ratio abstains;
   clean executed aggression triggers.
4. 30-trade live-L2 shadow warmup: trades 1..30 stay in the shadow twin and
   populate the conformal window; trade 31+ emits an unsigned proposal.
5. Law 2 air-gap AST sentinel + Step 2/3/4 regression contracts.
"""

from __future__ import annotations

import ast
import math
import time
from pathlib import Path

import pytest

from core.feature_store import CONFORMAL_MIN_SAMPLES, CONFORMAL_WINDOW, FeatureStore
from core.nats_bus import (
    STREAM_RING1_SCALP,
    STREAM_RING3_COCKPIT,
    LocalJetStreamBus,
)
from core.schemas import (
    LEASE_TTL_NS,
    TAKER_BUY_RATIO_MIN,
    TAKER_SELL_RATIO_MAX,
    VOL_SPIKE_BREAKER,
    ExecutedFlowTick,
    LeaseMode,
    PlainEnglishCockpitCard,
    System1Lease,
    UnsignedProposal,
    VaultName,
)
from core.shm_ipc import (
    GlobalKillLatch,
    LeaseTableSHM,
    ShadowBookSHM,
    unlink_all,
)
from ring1_spinal.circuit_breaker import (
    ADVERSE_FILL_WINDOW_NS,
    SPREAD_BLOWOUT_TRIP_REASON,
    TRIP_LATENCY_BUDGET_NS,
    TWO_ADVERSE_FILLS_TRIP_REASON,
    VOL_SPIKE_TRIP_REASON,
    SpinalCircuitBreaker,
    TripReason,
)
from ring1_spinal.shadow_twin import (
    REQUIRED_SHADOW_FILLS,
    ShadowExecutionTwin,
    ShadowFill,
)
from ring1_spinal.silo_worker import (
    CRYPTO_BINANCE,
    FX_MT5,
    INDIA_ZERODHA_DHAN,
    SILO_ORDER,
    US_IBKR,
    current_cpu_affinity,
    describe_silo_costs,
    nautilus_adapter_status,
    pin_cpu_core,
    plan_silo_workers,
    set_cpu_affinity,
    silo_config,
)
from ring1_spinal.spinal_loop import (
    TICK_WIRE_STRUCT,
    TickAction,
    ZeroGCSpinalEngine,
    pack_tick,
    unpack_tick,
)

ROOT = Path(__file__).resolve().parents[1]
RING1_DIR = ROOT / "ring1_spinal"

SYMBOL = "BTCUSDT"
TICK_COUNT = 25_000


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def shm_stack():
    """Fresh Tier-0 segments: kill latch + lease table + shadow book."""
    unlink_all()
    kill = GlobalKillLatch(reset=True)
    leases = LeaseTableSHM(reset=True, kill_latch=kill)
    book = ShadowBookSHM(reset=True)
    try:
        yield kill, leases, book
    finally:
        for handle in (book, leases, kill):
            try:
                handle.close()
            except Exception:
                pass
            try:
                handle.unlink()
            except Exception:
                pass
        unlink_all()


def make_tick(
    valid_ns: int,
    *,
    symbol: str = SYMBOL,
    last_price: float = 100.0,
    spread_bps: float = 2.0,
    realized_vol: float = 1.0,
    normal_vol: float = 1.0,
    buy_vol: float = 710.0,
    sell_vol: float = 290.0,
    bid_c2f: float = 1.4,
    ask_c2f: float = 1.4,
) -> ExecutedFlowTick:
    return ExecutedFlowTick(
        symbol=symbol,
        valid_time_ns=valid_ns,
        knowledge_ts_ns=valid_ns,
        last_price=last_price,
        spread_bps=spread_bps,
        realized_vol_5s=realized_vol,
        normal_vol_5s=normal_vol,
        taker_buy_volume_5s=buy_vol,
        taker_sell_volume_5s=sell_vol,
        bid_cancel_to_fill_ratio=bid_c2f,
        ask_cancel_to_fill_ratio=ask_c2f,
    )


def arm_lease(
    leases: LeaseTableSHM,
    now_ns: int,
    *,
    symbol: str = SYMBOL,
    mode: LeaseMode = LeaseMode.LONG_ONLY,
    target_bps: float = 18.0,
    stop_bps: float = 12.0,
    ttl_ns: int = 30_000_000_000,
) -> System1Lease:
    lease = System1Lease(
        symbol=symbol,
        mode=mode,
        target_bps=target_bps,
        stop_bps=stop_bps,
        expires_at_ns=int(now_ns) + int(min(ttl_ns, LEASE_TTL_NS)),
        strategy_id_hash=7,
    )
    leases.write_lease(lease, now_ns=now_ns)
    return lease


def graduated_engine(
    kill: GlobalKillLatch,
    leases: LeaseTableSHM,
    book: ShadowBookSHM,
    *,
    strategy_id: str = "spinal-scalper-v1",
    bus=None,
    proposal_sink=None,
    card_sink=None,
    silo: str = CRYPTO_BINANCE,
) -> tuple[ZeroGCSpinalEngine, ShadowExecutionTwin, int]:
    """Build an engine whose strategy already cleared the 30-fill warmup."""
    base = time.monotonic_ns()
    arm_lease(leases, base)
    book.write_snapshot(SYMBOL, 100.0, 100.02, 500.0, 500.0, updated_at_ns=base)
    twin = ShadowExecutionTwin(book)
    warmup = ZeroGCSpinalEngine(
        leases,
        kill_latch=kill,
        shadow_twin=twin,
        bus=None,
        strategy_id=strategy_id,
        silo=silo,
        proposal_sink=[],
        card_sink=[],
    )
    for index in range(REQUIRED_SHADOW_FILLS + 1):
        stamp = base + index
        warmup.on_tick(make_tick(stamp), now_ns=stamp)
    assert twin.is_graduated(strategy_id), "warmup did not graduate"

    engine = ZeroGCSpinalEngine(
        leases,
        kill_latch=kill,
        shadow_twin=twin,
        bus=bus,
        strategy_id=strategy_id,
        silo=silo,
        proposal_sink=[] if proposal_sink is None else proposal_sink,
        card_sink=[] if card_sink is None else card_sink,
    )
    return engine, twin, base


# ---------------------------------------------------------------------------
# Gate 1 -- Tier-0 latency and throughput SLA
# ---------------------------------------------------------------------------


def test_gate1_tier0_latency_and_throughput_sla(shm_stack):
    kill, leases, book = shm_stack
    engine, twin, base = graduated_engine(kill, leases, book)
    assert twin.shadow_fill_count(engine.strategy_id) == REQUIRED_SHADOW_FILLS

    ticks = [make_tick(base + index) for index in range(TICK_COUNT)]

    # -- throughput on an un-instrumented window ---------------------------
    assert not (leases.read_lease(SYMBOL, base).mode is LeaseMode.DISARMED)
    with engine.gc_guard():
        assert not __import__("gc").isenabled(), "gc_guard must disable the collector"
        started = time.perf_counter()
        for index, tick in enumerate(ticks):
            engine.on_tick(tick, now_ns=base + index)
        elapsed_s = time.perf_counter() - started

    assert __import__("gc").isenabled(), "gc_guard must restore the collector"
    throughput = TICK_COUNT / elapsed_s
    assert throughput >= 25_000.0, f"throughput {throughput:,.0f} ticks/sec < 25,000"
    assert engine.stats["live_proposals"] == TICK_COUNT

    # -- per-tick percentiles ---------------------------------------------
    probe = ZeroGCSpinalEngine(
        leases,
        kill_latch=kill,
        shadow_twin=twin,
        bus=None,
        strategy_id=engine.strategy_id,
        proposal_sink=[],
        card_sink=[],
    )
    samples = []
    with probe.gc_guard():
        for index, tick in enumerate(ticks):
            begin = time.perf_counter_ns()
            probe.on_tick(tick, now_ns=base + index)
            samples.append(time.perf_counter_ns() - begin)
    samples.sort()
    p50_ms = samples[len(samples) // 2] / 1_000_000.0
    p99_ms = samples[min(len(samples) - 1, int(len(samples) * 0.99))] / 1_000_000.0

    assert p50_ms <= 0.088, f"p50 {p50_ms:.4f} ms > 0.088 ms (sec_013)"
    assert p99_ms <= 0.140, f"p99 {p99_ms:.4f} ms > 0.140 ms (sec_010)"
    assert probe.fast_path_backend.startswith("python-struct")


def test_gate1_tick_wire_codec_roundtrip():
    tick = make_tick(1_234_567, last_price=101.25, spread_bps=3.5)
    frame = pack_tick(tick)
    assert len(frame) == TICK_WIRE_STRUCT.size == 96
    decoded = unpack_tick(frame)
    assert decoded.symbol == tick.symbol
    assert decoded.valid_time_ns == tick.valid_time_ns
    assert decoded.taker_buy_volume_5s == pytest.approx(710.0)
    assert decoded.spread_bps == pytest.approx(3.5)
    assert decoded.passes_anti_spoof_ofi("BUY") is True


# ---------------------------------------------------------------------------
# Gate 2 -- 0.1 ms microstructure circuit breaker
# ---------------------------------------------------------------------------


def test_gate2_volatility_spike_trips_and_disarms(shm_stack):
    kill, leases, _book = shm_stack
    now = time.monotonic_ns()
    arm_lease(leases, now)
    breaker = SpinalCircuitBreaker(leases, max_allowed_spread_bps=10.0)

    below = make_tick(now, realized_vol=3.4, normal_vol=1.0)
    assert below.vol_spike_ratio == pytest.approx(3.4)
    assert breaker.check_tick(below, now) is None
    assert leases.read_lease(SYMBOL, now).mode is LeaseMode.LONG_ONLY

    spike = make_tick(now + 1, realized_vol=3.6, normal_vol=1.0)
    assert spike.vol_spike_ratio == pytest.approx(3.6)
    trip = breaker.check_tick(spike, now + 1)

    assert trip is not None
    assert trip.reason == VOL_SPIKE_TRIP_REASON
    assert trip.reason == TripReason.VOL_SPIKE
    assert trip.lease_disarmed is True
    assert trip.disarm_latency_ns < TRIP_LATENCY_BUDGET_NS
    assert trip.within_budget
    assert leases.read_lease(SYMBOL, now + 1).mode is LeaseMode.DISARMED
    assert breaker.is_tripped(SYMBOL)
    assert breaker.trip_count_for(SYMBOL) == 1


def test_gate2_two_adverse_fills_inside_500ms_trips(shm_stack):
    kill, leases, _book = shm_stack
    now = time.monotonic_ns()
    arm_lease(leases, now)
    breaker = SpinalCircuitBreaker(leases)

    assert breaker.record_maker_fill(SYMBOL, "BUY", 100.0, -2.5, now, now_ns=now) is None
    trip = breaker.record_maker_fill(
        SYMBOL, "BUY", 100.0, -3.0, now + 300_000_000, now_ns=now + 300_000_000
    )

    assert trip is not None
    assert trip.reason == TWO_ADVERSE_FILLS_TRIP_REASON
    assert trip.lease_disarmed is True
    assert trip.disarm_latency_ns < TRIP_LATENCY_BUDGET_NS
    assert leases.read_lease(SYMBOL, now).mode is LeaseMode.DISARMED
    assert 300_000_000 < ADVERSE_FILL_WINDOW_NS


def test_gate2_two_adverse_fills_separated_by_700ms_do_not_trip(shm_stack):
    kill, leases, _book = shm_stack
    now = time.monotonic_ns()
    arm_lease(leases, now, symbol="ETHUSDT")
    breaker = SpinalCircuitBreaker(leases)

    first = breaker.record_maker_fill(
        "ETHUSDT", "BUY", 100.0, -2.5, now, now_ns=now
    )
    second = breaker.record_maker_fill(
        "ETHUSDT", "BUY", 100.0, -3.0, now + 700_000_000, now_ns=now + 700_000_000
    )

    assert first is None and second is None
    assert breaker.is_tripped("ETHUSDT") is False
    assert leases.read_lease("ETHUSDT", now).mode is LeaseMode.LONG_ONLY


def test_gate2_favourable_fill_breaks_the_adverse_chain(shm_stack):
    kill, leases, _book = shm_stack
    now = time.monotonic_ns()
    arm_lease(leases, now, symbol="SOLUSDT")
    breaker = SpinalCircuitBreaker(leases)

    assert breaker.record_maker_fill("SOLUSDT", "BUY", 100.0, -2.0, now, now_ns=now) is None
    assert (
        breaker.record_maker_fill(
            "SOLUSDT", "BUY", 100.0, +1.5, now + 100_000_000, now_ns=now + 100_000_000
        )
        is None
    )
    # Third fill is adverse but the *immediately preceding* fill was favourable.
    assert (
        breaker.record_maker_fill(
            "SOLUSDT", "BUY", 100.0, -2.0, now + 200_000_000, now_ns=now + 200_000_000
        )
        is None
    )
    assert breaker.is_tripped("SOLUSDT") is False


def test_gate2_spread_blowout_trips(shm_stack):
    kill, leases, _book = shm_stack
    now = time.monotonic_ns()
    arm_lease(leases, now, symbol="RELIANCE.NS")
    breaker = SpinalCircuitBreaker(leases, max_allowed_spread_bps=8.0)

    inside = make_tick(now, symbol="RELIANCE.NS", spread_bps=8.0)
    assert breaker.check_tick(inside, now) is None

    blown = make_tick(now + 1, symbol="RELIANCE.NS", spread_bps=8.5)
    trip = breaker.check_tick(blown, now + 1)

    assert trip is not None
    assert trip.reason == SPREAD_BLOWOUT_TRIP_REASON
    assert trip.observed == pytest.approx(8.5)
    assert trip.threshold == pytest.approx(8.0)
    assert trip.disarm_latency_ns < TRIP_LATENCY_BUDGET_NS
    assert leases.read_lease("RELIANCE.NS", now).mode is LeaseMode.DISARMED


def test_gate2_engine_halts_and_emits_card_on_trip(shm_stack):
    kill, leases, book = shm_stack
    engine, _twin, base = graduated_engine(kill, leases, book)
    cards: list[PlainEnglishCockpitCard] = []
    engine._card_sink = cards.append  # safety-latched trip card path

    outcome = engine.on_tick(
        make_tick(base + 1, realized_vol=3.6, normal_vol=1.0), now_ns=base + 1
    )

    assert outcome.action is TickAction.HALT_BREAKER
    assert outcome.reason == VOL_SPIKE_TRIP_REASON
    assert leases.read_lease(SYMBOL, base + 1).mode is LeaseMode.DISARMED
    assert engine.breaker.max_disarm_latency_ns < TRIP_LATENCY_BUDGET_NS
    assert len(cards) == 1
    assert cards[0].mode_badge == "HALT"
    assert cards[0].symbol == SYMBOL
    assert "disarmed" in cards[0].story_sentence

    # A disarmed lease can never trade again without a fresh System 1 write.
    after = engine.on_tick(make_tick(base + 2), now_ns=base + 2)
    assert after.action is TickAction.ABSTAIN_LEASE


# ---------------------------------------------------------------------------
# Gate 3 -- Law 1 anti-spoof executed OFI
# ---------------------------------------------------------------------------


def test_gate3_spoofed_book_is_rejected(shm_stack):
    kill, leases, book = shm_stack
    # A deliberately fat resting bid that a spoofer would love NIOS to chase.
    book.write_snapshot(SYMBOL, 100.0, 100.02, 5_000_000.0, 12.0)
    engine, _twin, base = graduated_engine(kill, leases, book)

    heavy_pull = make_tick(base + 1, bid_c2f=3.4, ask_c2f=1.4, buy_vol=710.0, sell_vol=290.0)
    weak_flow = make_tick(base + 2, buy_vol=520.0, sell_vol=480.0)

    first = engine.on_tick(heavy_pull, now_ns=base + 1)
    second = engine.on_tick(weak_flow, now_ns=base + 2)

    assert first.action is TickAction.ABSTAIN_OFI
    assert "ANTI_SPOOF" in first.reason
    assert second.action is TickAction.ABSTAIN_OFI
    assert engine.stats["live_proposals"] == 0
    assert engine.abstention_rate == pytest.approx(1.0)
    assert heavy_pull.max_cancel_to_fill_ratio == pytest.approx(3.4)
    assert weak_flow.taker_buy_ratio_5s == pytest.approx(0.52)


def test_gate3_clean_aggressive_flow_triggers(shm_stack):
    kill, leases, book = shm_stack
    engine, _twin, base = graduated_engine(kill, leases, book)
    proposals: list[UnsignedProposal] = []
    engine._proposal_sink = proposals.append

    tick = make_tick(base + 1, buy_vol=710.0, sell_vol=290.0, bid_c2f=1.4, ask_c2f=1.4)
    assert tick.taker_buy_ratio_5s >= TAKER_BUY_RATIO_MIN
    assert tick.max_cancel_to_fill_ratio < 3.0

    outcome = engine.on_tick(tick, now_ns=base + 1)

    assert outcome.action is TickAction.LIVE_PROPOSAL
    assert outcome.proposal is not None
    assert outcome.proposal.vault is VaultName.SPINAL_SCALPER
    assert outcome.proposal.side == "BUY"
    assert outcome.proposal.expected_gross_bps == pytest.approx(18.0)
    assert outcome.proposal.is_grounded
    assert len(proposals) == 1


def test_gate3_mean_reversion_requires_exhaustion(shm_stack):
    kill, leases, book = shm_stack
    engine, _twin, base = graduated_engine(kill, leases, book)
    arm_lease(leases, base + 10, mode=LeaseMode.MEAN_REV)
    arm_lease(leases, base + 11, mode=LeaseMode.MEAN_REV)
    arm_lease(leases, base + 12, mode=LeaseMode.MEAN_REV)

    balanced = engine.on_tick(
        make_tick(base + 10, buy_vol=500.0, sell_vol=500.0), now_ns=base + 10
    )
    exhausted_sellers = engine.on_tick(
        make_tick(base + 11, buy_vol=150.0, sell_vol=850.0), now_ns=base + 11
    )
    exhausted_buyers = engine.on_tick(
        make_tick(base + 12, buy_vol=850.0, sell_vol=150.0), now_ns=base + 12
    )

    assert balanced.action is TickAction.ABSTAIN_OFI
    assert exhausted_sellers.action is TickAction.LIVE_PROPOSAL
    assert exhausted_sellers.proposal.side == "BUY"
    assert exhausted_buyers.action is TickAction.LIVE_PROPOSAL
    assert exhausted_buyers.proposal.side == "SELL"


# ---------------------------------------------------------------------------
# Gate 4 -- 30-trade live L2 shadow warmup to live graduation
# ---------------------------------------------------------------------------


def test_gate4_thirty_trade_shadow_warmup_then_live(tmp_path, shm_stack):
    kill, leases, book = shm_stack
    store = FeatureStore(root=tmp_path / "feature_lake")
    bus = LocalJetStreamBus()
    strategy_id = "fresh-alpha-n0"

    base = time.monotonic_ns()
    arm_lease(leases, base)
    book.write_snapshot(SYMBOL, 100.0, 100.02, 500.0, 500.0, updated_at_ns=base)

    twin = ShadowExecutionTwin(book, feature_store=store)
    engine = ZeroGCSpinalEngine(
        leases,
        kill_latch=kill,
        shadow_twin=twin,
        bus=bus,
        strategy_id=strategy_id,
        proposal_sink=[],
        card_sink=[],
    )
    assert twin.shadow_fill_count(strategy_id) == 0
    assert twin.is_graduated(strategy_id) is False

    # ---- trades 1..30: shadow only --------------------------------------
    for sequence in range(1, REQUIRED_SHADOW_FILLS + 1):
        stamp = base + sequence * 1_000_000
        outcome = engine.on_tick(make_tick(stamp), now_ns=stamp)

        assert outcome.action is TickAction.SHADOW_FILL, f"trade {sequence} not shadowed"
        assert isinstance(outcome.shadow_fill, ShadowFill)
        assert outcome.shadow_fill.sequence == sequence
        assert bus.committed_count(STREAM_RING1_SCALP) == 0, (
            f"live proposal leaked during warmup at trade {sequence}"
        )
        assert twin.shadow_fill_count(strategy_id) == sequence
        assert twin.is_graduated(strategy_id) is (sequence >= REQUIRED_SHADOW_FILLS)

        if sequence == REQUIRED_SHADOW_FILLS - 1:
            cold = store.get_q98_residual(strategy_id, stamp + 1)
            assert cold.count == REQUIRED_SHADOW_FILLS - 1
            assert math.isinf(cold.q98_bps), "Q_0.98 must stay +inf while cold"

    # ---- the conformal window is now warm --------------------------------
    horizon = base + 10**12
    assert REQUIRED_SHADOW_FILLS == CONFORMAL_MIN_SAMPLES == 30
    assert store.count_trades(strategy_id, horizon) == REQUIRED_SHADOW_FILLS
    window = store.get_q98_residual(strategy_id, horizon)
    assert window.count == REQUIRED_SHADOW_FILLS
    assert window.shadow_count == REQUIRED_SHADOW_FILLS
    assert window.calibrated is True
    assert math.isfinite(window.q98_bps)
    assert window.requested_window == CONFORMAL_WINDOW

    report = twin.shadow_report(strategy_id, horizon)
    assert report["graduated"] is True
    assert report["shadow_fills"] == REQUIRED_SHADOW_FILLS
    assert report["calibrated"] is True
    assert report["settlement_errors"] == []

    # ---- trades 31..35: live unsigned proposals --------------------------
    for sequence in range(REQUIRED_SHADOW_FILLS + 1, REQUIRED_SHADOW_FILLS + 6):
        stamp = base + sequence * 1_000_000
        outcome = engine.on_tick(make_tick(stamp), now_ns=stamp)
        assert outcome.action is TickAction.LIVE_PROPOSAL, (
            f"trade {sequence} did not graduate to live"
        )
        assert isinstance(outcome.proposal, UnsignedProposal)

    assert bus.committed_count(STREAM_RING1_SCALP) == 5
    events = bus.replay(STREAM_RING1_SCALP)
    payload = events[0].payload
    assert payload["vault"] == VaultName.SPINAL_SCALPER.value
    assert payload["strategy_id"] == strategy_id
    assert payload["expected_gross_bps"] == pytest.approx(18.0)
    assert len(payload["grounding_hash"]) == 64
    assert events[0].idempotency_key.startswith(strategy_id)

    # Re-publishing the same intent is a no-op (at-least-once + idempotency).
    duplicate = bus.publish_idempotent(
        STREAM_RING1_SCALP, payload, events[0].idempotency_key
    )
    assert duplicate.duplicate is True
    assert bus.committed_count(STREAM_RING1_SCALP) == 5

    # The cockpit saw every shadow event.  Live proposals become Ring 3 cards
    # only after Ring 0 fills them (an intent is not an execution), so the
    # warmup count is the exact card count here.
    cards = bus.replay(STREAM_RING3_COCKPIT)
    assert len(cards) == REQUIRED_SHADOW_FILLS
    assert all(event.payload["mode_badge"] == "SHADOW WARMUP" for event in cards)
    assert cards[0].payload["proof_line"].startswith("[SHADOW WARMUP")

    store.close()


def test_gate4_shadow_twin_settles_through_delayed_settlement_worker(tmp_path, shm_stack):
    """The twin may settle through Step 4's worker instead of the raw store."""
    from ring2_brain.truth.settlement_worker import DelayedSettlementWorker

    kill, leases, book = shm_stack
    store = FeatureStore(root=tmp_path / "worker_lake")
    worker = DelayedSettlementWorker(store, persist_trust=True)
    strategy_id = "worker-settled"

    base = time.monotonic_ns()
    arm_lease(leases, base)
    book.write_snapshot(SYMBOL, 100.0, 100.02, 500.0, 500.0, updated_at_ns=base)
    twin = ShadowExecutionTwin(book, settlement_worker=worker)
    assert twin.feature_store is store

    for sequence in range(1, 6):
        stamp = base + sequence * 1_000_000
        fill = twin.execute_shadow_fill(
            make_tick(stamp),
            strategy_id=strategy_id,
            side="BUY",
            silo=CRYPTO_BINANCE,
            now_ns=stamp,
            expected_bps=18.0,
        )
        assert fill is not None

    assert store.count_trades(strategy_id, base + 10**12) == 5
    assert len(worker.settlements_for(strategy_id)) == 5
    # Binance's 16 bps round-trip floor swamps a 2 bps half-spread capture, so
    # every one of these probes is a directional miss and the Darwinian
    # multiplier pins to its floor -- honest grading, not a manufactured win.
    multiplier = worker.trust_multiplier(strategy_id)
    assert 0.8 <= multiplier <= 1.5
    assert multiplier == pytest.approx(0.8)
    assert worker.settlements_for(strategy_id)[0].directional_hit is False
    store.close()


def test_gate4_shadow_peg_rests_without_executed_flow(shm_stack):
    kill, leases, book = shm_stack
    base = time.monotonic_ns()
    arm_lease(leases, base)
    book.write_snapshot(SYMBOL, 100.0, 100.02, 500.0, 500.0, updated_at_ns=base)
    twin = ShadowExecutionTwin(book)
    engine = ZeroGCSpinalEngine(
        leases,
        kill_latch=kill,
        shadow_twin=twin,
        bus=None,
        strategy_id="resting-alpha",
        proposal_sink=[],
        card_sink=[],
    )

    quiet = make_tick(base + 1, buy_vol=710.0, sell_vol=0.0)
    outcome = engine.on_tick(quiet, now_ns=base + 1)

    assert outcome.action is TickAction.SHADOW_RESTING
    assert outcome.shadow_fill is None
    assert twin.open_order_count("resting-alpha") == 1
    assert twin.shadow_fill_count("resting-alpha") == 0
    assert twin.cancel_stale(base + 10_000_000_000) == 1
    assert twin.open_order_count("resting-alpha") == 0


# ---------------------------------------------------------------------------
# Gate 5 -- Law 2 air-gap and Step 2/3/4 regression contracts
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "ollama",
        "langgraph",
        "httpx",
        "ring2_brain",
        "ring0_vault",
    }
)


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative imports stay inside the package
                continue
            if node.module:
                found.append((node.module, node.lineno))
    return found


def test_gate5_ring1_air_gap_ast_sentinel():
    files = sorted(RING1_DIR.rglob("*.py"))
    assert files, "no Ring 1 sources found"

    offenders: list[str] = []
    for path in files:
        for module, lineno in _imported_modules(path):
            root = module.split(".")[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno} imports {module}")
            if module.startswith("ring0_vault.silos.key_vault"):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno} imports {module}")

    assert offenders == [], "Ring 1 air-gap violated:\n" + "\n".join(offenders)


def test_gate5_ring1_holds_no_credentials_or_signers():
    forbidden_tokens = (
        "api_secret",
        "api_key_secret",
        "Fernet(",
        "Ed25519PrivateKey",
        "sign_order",
    )
    hits: list[str] = []
    for path in sorted(RING1_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in text:
                hits.append(f"{path.relative_to(ROOT)} contains {token!r}")
    assert hits == [], "Ring 1 must never hold signing material:\n" + "\n".join(hits)


def test_gate5_architectural_constants_unchanged():
    assert VOL_SPIKE_BREAKER == 3.5
    assert ADVERSE_FILL_WINDOW_NS == 500_000_000
    assert TRIP_LATENCY_BUDGET_NS == 100_000
    assert REQUIRED_SHADOW_FILLS == CONFORMAL_MIN_SAMPLES == 30
    assert System1Lease.SIZE == 64
    assert LEASE_TTL_NS == 60_000_000_000
    assert TAKER_BUY_RATIO_MIN == 0.65
    assert TAKER_SELL_RATIO_MAX == 0.35


# ---------------------------------------------------------------------------
# Silo shards, cost floors and CPU affinity
# ---------------------------------------------------------------------------


def test_silo_configs_are_the_four_canonical_silos():
    plans = plan_silo_workers()
    assert tuple(plans) == SILO_ORDER
    assert set(plans) == {CRYPTO_BINANCE, INDIA_ZERODHA_DHAN, US_IBKR, FX_MT5}

    cores = [plan.cpu_core for plan in plans.values()]
    assert len(set(cores)) == 4, "each silo must own a dedicated core"
    assert all(plan.round_trip_cost_bps >= 0.0 for plan in plans.values())

    india = silo_config(INDIA_ZERODHA_DHAN)
    assert india.cost_floor.statutory_tax_bps == pytest.approx(5.5)
    assert india.cost_floor.round_trip_cost_bps == pytest.approx(
        india.cost_floor.spread_bps
        + 2.0 * india.cost_floor.maker_fee_bps
        + 5.5
    )
    assert india.cost_floor.fee_stt_bps == pytest.approx(6.5)

    costs = describe_silo_costs()
    assert costs[US_IBKR]["reserve_yield_asset"] == "SGOV (0-3M Treasury ETF)"
    assert "NIFTYBEES.NS" in costs[INDIA_ZERODHA_DHAN]["long_term_sweep_asset"]
    assert costs[CRYPTO_BINANCE]["cost_floor"]["source"].startswith("configured")


def test_cpu_affinity_pinning_and_restore():
    original = current_cpu_affinity()
    assert original is not None, "platform could not report a CPU set"

    target = min(original)
    dry = pin_cpu_core(target, dry_run=True)
    assert dry.pinned is False and "dry-run" in dry.mechanism
    assert current_cpu_affinity() == original

    pinned = pin_cpu_core(target)
    if pinned.pinned:
        assert current_cpu_affinity() == (target,)
    else:  # pragma: no cover - restricted container
        assert pinned.reason

    restored = set_cpu_affinity(original)
    assert restored.pinned is True
    assert current_cpu_affinity() == original

    out_of_range = pin_cpu_core(len(original) + 999)
    assert out_of_range.pinned is False
    assert "range" in out_of_range.reason


def test_nautilus_adapter_status_is_honest():
    status = nautilus_adapter_status()
    assert status["harvested_count"] == 5
    assert status["total"] == 5
    assert set(status["adapters"]) == {
        "binance",
        "okx",
        "bybit",
        "interactive_brokers",
        "alpaca",
    }
    for entry in status["adapters"].values():
        assert entry["harvested"] is True
        # Phase 2 -- Step 2 activated every adapter through _nautilus_compat, so
        # importability no longer depends on the upstream C extensions loading.
        assert entry["importable"] is True, entry["reason"]
        assert entry["reason"] == ""
    assert status["importable_count"] == status["total"] == 5
    assert status["gaps"] == {}
