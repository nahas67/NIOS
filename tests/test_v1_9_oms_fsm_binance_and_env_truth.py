"""V1.9 Suite 30 -- OMS state machines, Binance microstructure and env truth.

Covers ``E1``-``E9`` and ``C2``-``C8``:

* ``E1`` published high-water watermark and ``STALE_POSITION_STATE``;
* ``E2`` all-or-none ``LegGroup`` basket reservations with flatten signalling;
* ``E3``/``E7`` cold-start reconciliation classification and bounded deadline;
* ``E4`` strict order FSM, ``ExecID`` de-duplication and venue ``LeavesQty``;
* ``E5`` real clock health; ``E6`` DST-aware venue calendar;
* ``E8`` consensus fencing epoch; ``E9`` SHA-256 replay checkpoints;
* ``C2`` ADL rank budget; ``C3`` funding in the net bound; ``C4`` tiered
  maintenance margin; ``C5`` used-weight bucket; ``C6`` listenKey keepalive;
  ``C7`` recvWindow offset; ``C8`` 418 halt and query-before-retry.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.exchange_calendar import (  # noqa: E402
    calendar_snapshot,
    is_open,
    next_open,
    session_for,
    us_eastern_offset_hours,
)
from core.nats_bus import LocalJetStreamBus  # noqa: E402
from core.schemas import (  # noqa: E402
    ContagionFeatures,
    UnsignedProposal,
    VaultName,
    net_lower_bound_bps,
)
from ring0_vault.ibor.cold_start_reconciler import (  # noqa: E402
    BrokerSnapshot,
    ColdStartReconciler,
    DiscrepancyClass,
)
from ring0_vault.ibor.ledger_outbox import (  # noqa: E402
    CrashAtomicOutbox,
    IBORLedger,
    LegGroup,
    LedgerStore,
    SiloGuard,
)
from ring0_vault.ibor.state_journal import AtomicStateJournal  # noqa: E402
from ring0_vault.kernel.firewalls_and_oms import (  # noqa: E402
    FirewallStage,
    OrderLifecycleTracker,
    OrderLifecycleViolation,
    OrderSigner,
    OrderState,
    SovereignPreTradeFirewall,
)
from ring0_vault.silos.binance_microstructure import (  # noqa: E402
    BinanceErrorClass,
    BinanceResponseGuard,
    BinanceWeightBucket,
    ListenKeySupervisor,
    RecvWindowTracker,
    validate_maintenance_bracket,
)

DAY_NS = 86_400_000_000_000


def _proposal(
    *,
    pid: str = "p-1",
    qty: float = 1.0,
    price: float = 100.0,
    silo: str = "US_IBKR",
    symbol: str = "SPY",
) -> UnsignedProposal:
    return UnsignedProposal(
        proposal_id=pid,
        strategy_id="suite-30",
        silo=silo,
        vault=VaultName.SPINAL_SCALPER,
        symbol=symbol,
        side="BUY",
        qty=qty,
        limit_price=price,
        expected_gross_bps=12.0,
        spread_bps=1.0,
        grounding_hash="a" * 64,
    )


class _Stub:
    def __getattr__(self, name: str):  # pragma: no cover - defensive
        raise AssertionError(f"unexpected dependency {name!r}")


@pytest.fixture()
def ledger() -> IBORLedger:
    store = LedgerStore(in_memory=True, probe=False)
    book = IBORLedger(store, silos=["US_IBKR", "CRYPTO_BINANCE"])
    book.open_silo("US_IBKR", funding_micros=10_000_000_000)
    book.fund_vault("US_IBKR", VaultName.SPINAL_SCALPER, 8_000_000_000)
    book.open_silo("CRYPTO_BINANCE", funding_micros=1_000_000_000)
    book.fund_vault("CRYPTO_BINANCE", VaultName.SPINAL_SCALPER, 800_000_000)
    yield book
    book.close()


# ---------------------------------------------------------------------------
# E1 -- published high-water watermark
# ---------------------------------------------------------------------------


def test_published_high_water_and_staleness() -> None:
    bus = LocalJetStreamBus()
    try:
        assert bus.published_high_water("stream.ring0.execution-events") == 0
        for index in range(3):
            bus.publish_idempotent(
                "ring0.execution",
                {"n": index},
                idempotency_key=f"e1-{index}",
                stream="stream.ring0.execution-events",
            )
        high = bus.published_high_water("stream.ring0.execution-events")
        assert high == 3
        assert bus.staleness("stream.ring0.execution-events", 3) == 0
        assert bus.staleness("stream.ring0.execution-events", 1) == 2
        assert bus.high_water_all()["stream.ring0.execution-events"] == 3

        firewall = SovereignPreTradeFirewall(
            ledger=_Stub(),
            outbox=_Stub(),
            gate=_Stub(),
            shield=_Stub(),
            bus=bus,
            applied_seq=1,
            staleness_budget=1,
        )
        rejected = firewall.preflight_checks(_proposal(), now_ns=time.time_ns())
        assert rejected is not None
        assert rejected[0] is FirewallStage.STALENESS
        assert rejected[1].startswith("STALE_POSITION_STATE")

        firewall.applied_seq = 2
        assert firewall.preflight_checks(_proposal(), now_ns=time.time_ns()) is None
    finally:
        bus.close()


# ---------------------------------------------------------------------------
# E2 -- all-or-none baskets
# ---------------------------------------------------------------------------


def test_leg_group_basket_reserves_all_legs_or_none(ledger: IBORLedger) -> None:
    outbox = CrashAtomicOutbox(ledger)
    group = LegGroup(
        group_id="basket-1",
        silo="US_IBKR",
        legs=(_proposal(pid="leg-1", qty=10.0), _proposal(pid="leg-2", qty=10.0)),
        max_loss_micros=5_000_000_000,
    )
    reserved = outbox.reserve_basket(group)
    assert reserved.state.value == "PENDING"
    assert reserved.flatten_required is False
    assert len(reserved.reservations) == 2
    assert reserved.total_notional_micros == 2_000_000_000

    # A basket whose second leg cannot be funded must void the first leg too.
    small_store = LedgerStore(in_memory=True, probe=False)
    small = IBORLedger(small_store, silos=["US_IBKR"])
    small.open_silo("US_IBKR", funding_micros=50_000_000)
    small.fund_vault("US_IBKR", VaultName.SPINAL_SCALPER, 10_000_000)
    small_outbox = CrashAtomicOutbox(small)
    failed = small_outbox.reserve_basket(group)
    assert failed.state.value == "VOIDED"
    assert failed.flatten_required is True
    assert failed.reservations == ()
    assert failed.reason.startswith("BASKET_REJECTED")
    assert small_outbox.pending_count == 0
    assert small.invariant_report()["holds"] is True
    small.close()

    # A defined-risk cap below the basket notional voids the whole basket.
    capped = LegGroup(
        group_id="basket-2",
        silo="US_IBKR",
        legs=(_proposal(pid="leg-3", qty=10.0),),
        max_loss_micros=1_000,
    )
    blocked = outbox.reserve_basket(capped)
    assert blocked.state.value == "VOIDED"
    assert blocked.flatten_required is True
    assert blocked.reason.startswith("DEFINED_RISK_EXCEEDED")


# ---------------------------------------------------------------------------
# E3 / E7 -- cold-start reconciliation
# ---------------------------------------------------------------------------


class _PositionLedger:
    def __init__(self, rows: dict[tuple[str, str], int]) -> None:
        self._rows = rows

    def position(self, silo: str, symbol: str):
        qty = self._rows.get((str(silo).upper(), str(symbol).upper()))
        return None if qty is None else {"qty_micros": int(qty)}


def test_cold_start_classifies_and_freezes_only_ambiguous_silos() -> None:
    ledger = _PositionLedger(
        {
            ("US_IBKR", "SPY"): 10_000_000,
            ("CRYPTO_BINANCE", "BTCUSDT"): 5_000_000,
            ("US_IBKR", "QQQ"): 0,
            ("FX_MT5", "EURUSD"): 2_000_000,
        }
    )
    guard = SiloGuard()
    reconciler = ColdStartReconciler(
        ledger, guard=guard, pending_order_symbols=("BTCUSDT",)
    )
    report = reconciler.reconcile(
        [
            BrokerSnapshot("US_IBKR", "SPY", 10_000_000),
            BrokerSnapshot("CRYPTO_BINANCE", "BTCUSDT", 6_000_000),
            BrokerSnapshot("US_IBKR", "QQQ", 1_000_000),
            BrokerSnapshot("FX_MT5", "EURUSD", 3_000_000),
        ],
        now_ns=1_000,
        finished_ns=2_000,
    )
    classes = [row.classification for row in report.rows]
    assert classes == [
        DiscrepancyClass.MATCHED,
        DiscrepancyClass.MID_ORDER_AMBIGUOUS,
        DiscrepancyClass.ROGUE_MANUAL_POSITION,
        DiscrepancyClass.UNKNOWN_FILL,
    ]
    assert report.within_deadline is True
    assert report.passed is False
    assert guard.is_frozen("CRYPTO_BINANCE") is True
    assert guard.is_frozen("US_IBKR") is True
    assert guard.is_frozen("FX_MT5") is False
    assert len(report.ambiguous) == 1
    assert len(report.rogue_positions) == 1
    assert len(report.unknown_fills) == 1


def test_cold_start_deadline_freezes_every_affected_silo() -> None:
    guard = SiloGuard()
    reconciler = ColdStartReconciler(_PositionLedger({}), guard=guard, deadline_ns=1_000)
    report = reconciler.reconcile(
        [BrokerSnapshot("US_IBKR", "SPY", 1_000_000)], now_ns=0, finished_ns=10_000
    )
    assert report.within_deadline is False
    assert report.passed is False
    assert "US_IBKR" in report.frozen_silos
    assert guard.is_frozen("US_IBKR") is True
    assert report.to_dict()["counts"]["total"] == 1


# ---------------------------------------------------------------------------
# E4 -- order FSM, ExecID de-duplication and authoritative LeavesQty
# ---------------------------------------------------------------------------


def test_order_fsm_deduplicates_exec_ids_and_gates_voids() -> None:
    tracker = OrderLifecycleTracker()
    tracker.track("o-1", 10.0)
    tracker.transition("o-1", OrderState.PENDING_NEW)
    tracker.transition("o-1", OrderState.ACCEPTED)

    first = tracker.apply_exec("o-1", exec_id="EXEC-1", last_qty=4.0, leaves_qty=6.0)
    assert first["applied"] is True
    assert first["state"] == "PARTIALLY_FILLED"
    assert first["leaves_qty"] == 6.0
    duplicate = tracker.apply_exec("o-1", exec_id="EXEC-1", last_qty=4.0, leaves_qty=6.0)
    assert duplicate["duplicate"] is True
    assert duplicate["filled_qty"] == 4.0

    # The venue's LeavesQty is authoritative even if it disagrees with the sum.
    final = tracker.apply_exec("o-1", exec_id="EXEC-2", last_qty=1.0, leaves_qty=0.0)
    assert final["state"] == "FILLED"
    assert final["leaves_qty"] == 0.0
    assert tracker.void_allowed("o-1") is False

    with pytest.raises(OrderLifecycleViolation):
        tracker.transition("o-1", OrderState.ACCEPTED)

    tracker.track("o-2", 1.0)
    tracker.transition("o-2", OrderState.PENDING_NEW)
    tracker.transition("o-2", OrderState.PENDING_CANCEL)
    tracker.transition("o-2", OrderState.CANCELED)
    assert tracker.void_allowed("o-2") is True
    assert tracker.snapshot()["count"] == 2


# ---------------------------------------------------------------------------
# C4-C8 -- Binance microstructure
# ---------------------------------------------------------------------------


def test_binance_418_halts_and_5xx_queries_before_retry() -> None:
    guard = BinanceResponseGuard()
    halted = guard.on_response("CRYPTO_BINANCE", 418)
    assert halted["halted"] is True
    assert halted["retry_allowed"] is False
    assert halted["reduce_only"] is True
    assert guard.halted("CRYPTO_BINANCE") is True
    assert guard.retry_plan("cid-1", BinanceErrorClass.RATE_LIMITED_418)["action"] == "HALT"

    other = BinanceResponseGuard()
    unknown = other.on_response("CRYPTO_BINANCE", None, timed_out=True)
    assert unknown["query_before_retry"] is True
    assert unknown["retries"] == 1
    assert other.halted("CRYPTO_BINANCE") is False
    assert other.classify(503) is BinanceErrorClass.SERVER_ERROR
    assert other.classify(400) is BinanceErrorClass.CLIENT_ERROR
    plan = other.retry_plan("cid-2", BinanceErrorClass.TIMEOUT)
    assert plan["action"] == "QUERY_THEN_RETRY"
    assert plan["deterministic_client_order_id"] is True


def test_weight_bucket_margin_brackets_listen_key_and_recv_window() -> None:
    bucket = BinanceWeightBucket(limit=100, window_seconds=60)
    state = bucket.observe(99, now_ns=0)
    assert state.exhausted is False
    assert bucket.allow(2, now_ns=0) is False
    assert bucket.allow(1, now_ns=0) is True
    assert bucket.observe(100, now_ns=0).exhausted is True
    assert bucket.observe(10, now_ns=61_000_000_000).exhausted is False

    ok = validate_maintenance_bracket(1_000_000_000, 5.0)
    assert ok["valid"] is True
    assert ok["required_margin_micros"] == 200_000_000
    too_levered = validate_maintenance_bracket(1_000_000_000, 500.0)
    assert too_levered["valid"] is False

    supervisor = ListenKeySupervisor(keepalive_seconds=1800)
    supervisor.register("key-1", now_ns=0)
    assert supervisor.due(now_ns=1_000 * 10**9) == []
    assert supervisor.due(now_ns=2_000 * 10**9) == ["key-1"]
    assert supervisor.renew("key-1", now_ns=2_000 * 10**9) is True
    assert supervisor.due(now_ns=2_100 * 10**9) == []

    tracker = RecvWindowTracker()
    assert tracker.observe_server_time(1_000_400, 1_000_000) == 400
    assert tracker.recv_window_ms == 5400
    assert tracker.observe_server_time(1_000_000, 2_000_000) == -1000
    assert tracker.offset_ms == -1000


# ---------------------------------------------------------------------------
# C2 / C3 -- ADL budget and funding in the net bound
# ---------------------------------------------------------------------------


def test_adl_rank_is_validated_and_bounded() -> None:
    features = ContagionFeatures(
        symbol="BTCUSDT",
        as_of_ns=1,
        crowd_asymmetry_score=0.0,
        cascade_fragility_index=0.2,
        narrative_exhaustion_divergence=0.0,
        adl_rank_quantile=0.95,
    )
    assert features.adl_rank_quantile == 0.95
    assert features.to_dict()["adl_rank_quantile"] == 0.95
    with pytest.raises(ValueError):
        ContagionFeatures(
            symbol="BTCUSDT",
            as_of_ns=1,
            crowd_asymmetry_score=0.0,
            cascade_fragility_index=0.2,
            narrative_exhaustion_divergence=0.0,
            adl_rank_quantile=1.5,
        )

    firewall = SovereignPreTradeFirewall(
        ledger=_Stub(),
        outbox=_Stub(),
        gate=_Stub(),
        shield=_Stub(),
        adl_rank_quantile=0.95,
    )
    rejected = firewall.preflight_checks(
        _proposal(silo="CRYPTO_BINANCE", symbol="BTCUSDT"), now_ns=time.time_ns()
    )
    assert rejected is not None
    assert rejected[1].startswith("ADL_RISK_BUDGET_EXCEEDED")


def test_funding_is_subtracted_from_the_net_lower_bound() -> None:
    assert net_lower_bound_bps(20.0, 1.0, 2.0, 3.0, 5.0) == pytest.approx(7.0)
    assert net_lower_bound_bps(
        20.0, 1.0, 2.0, 3.0, 5.0, funding_bps=4.0
    ) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# E5 / E6 -- clock health and exchange calendar
# ---------------------------------------------------------------------------


def test_clock_health_is_real_telemetry_and_fails_closed() -> None:
    from ring3_cockpit.gateway.control_plane import ControlPlane

    now = time.time_ns()
    dummy = types.SimpleNamespace(
        _clock_anchor_wall_ns=now,
        _clock_anchor_mono_ns=time.monotonic_ns(),
        _clock_budget_ms=250.0,
    )
    health = ControlPlane.clock_health(dummy)
    assert health["synthetic"] is False
    assert health["source"] == "monotonic_anchor"
    assert health["ok"] is True
    assert abs(health["offset_ms"]) < 250.0

    firewall = SovereignPreTradeFirewall(
        ledger=_Stub(),
        outbox=_Stub(),
        gate=_Stub(),
        shield=_Stub(),
        clock_health=lambda: {"ok": False, "offset_ms": 900, "budget_ms": 250},
    )
    rejected = firewall.preflight_checks(_proposal(), now_ns=now)
    assert rejected is not None
    assert rejected[0] is FirewallStage.TIME_HEALTH
    assert rejected[1].startswith("TIME_HEALTH_BUDGET_BREACH")


def test_exchange_calendar_is_dst_aware_with_holidays_and_half_days() -> None:
    import calendar as _calendar

    def utc_ns(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
        return _calendar.timegm((year, month, day, hour, minute, 0, 0, 0, 0)) * 1_000_000_000

    # US: 09:30 EST == 14:30 UTC in January, 09:30 EDT == 13:30 UTC in July.
    assert is_open("US_IBKR", utc_ns(2026, 1, 5, 14, 30)) is True
    assert is_open("US_IBKR", utc_ns(2026, 1, 5, 14, 29)) is False
    assert is_open("US_IBKR", utc_ns(2026, 7, 6, 13, 30)) is True
    assert is_open("US_IBKR", utc_ns(2026, 7, 6, 13, 29)) is False
    assert us_eastern_offset_hours(utc_ns(2026, 1, 5, 14, 30)) == -5
    assert us_eastern_offset_hours(utc_ns(2026, 7, 6, 13, 30)) == -4

    # India: 09:15 IST == 03:45 UTC.
    assert is_open("INDIA_ZERODHA_DHAN", utc_ns(2026, 1, 5, 3, 45)) is True
    assert is_open("INDIA_ZERODHA_DHAN", utc_ns(2026, 1, 5, 3, 44)) is False

    # Crypto never closes; FX closes for the weekend.
    assert is_open("CRYPTO_BINANCE", utc_ns(2026, 1, 4, 12, 0)) is True
    assert is_open("FX_MT5", utc_ns(2026, 1, 3, 12, 0)) is False  # Saturday
    assert is_open("FX_MT5", utc_ns(2026, 1, 5, 12, 0)) is True

    # Christmas Day and the US half-day close (13:00 EST == 18:00 UTC).
    assert is_open("US_IBKR", utc_ns(2026, 12, 25, 15, 0)) is False
    assert is_open("US_IBKR", utc_ns(2026, 11, 27, 18, 0)) is False
    assert is_open("US_IBKR", utc_ns(2026, 11, 27, 17, 0)) is True
    assert session_for("US_IBKR", utc_ns(2026, 12, 25, 15, 0)) is None
    upcoming = next_open("US_IBKR", utc_ns(2026, 12, 25, 15, 0))
    assert upcoming is not None and upcoming.open_utc_ns > utc_ns(2026, 12, 25, 15, 0)
    snapshot = calendar_snapshot(utc_ns(2026, 1, 5, 14, 30))
    assert snapshot["silos"]["US_IBKR"]["open"] is True

    firewall = SovereignPreTradeFirewall(
        ledger=_Stub(), outbox=_Stub(), gate=_Stub(), shield=_Stub()
    )
    import core.exchange_calendar as calendar_module

    firewall.calendar = calendar_module
    rejected = firewall.preflight_checks(_proposal(), now_ns=utc_ns(2026, 12, 25, 15, 0))
    assert rejected is not None
    assert rejected[0] is FirewallStage.MARKET_HOURS
    assert rejected[1].startswith("VENUE_CLOSED")


# ---------------------------------------------------------------------------
# E8 / E9 -- fencing epoch and replay checkpoints
# ---------------------------------------------------------------------------


def test_signed_order_carries_the_fencing_epoch() -> None:
    signer = OrderSigner()
    proposal = _proposal()
    signed = signer.signed_order(
        proposal,
        outbox_key="k" * 32,
        net_lb_bps=5.0,
        order_type="MAKER_PEG",  # type: ignore[arg-type]
        now_ns=time.time_ns(),
        fencing_epoch=7,
    )
    assert signed.fencing_epoch == 7
    assert signed.payload()["fencing_epoch"] == 7
    assert signer.verify_order(signed) is True


def test_state_journal_replay_checkpoint_binds_state_and_boundary(tmp_path) -> None:
    journal = AtomicStateJournal(tmp_path / "journal.json")
    journal.update(logbook=[{"trade": "a"}])
    checkpoint = journal.write_checkpoint(
        journal.snapshot(),
        event_boundary={"stream": "stream.ring0.execution-events", "seq": 42},
        label="e9",
    )
    assert checkpoint["sequence"] == 1
    assert journal.verify_checkpoint(checkpoint) is True
    assert (
        journal.replay_checkpoint(
            journal.snapshot(),
            checkpoint,
            event_boundary={"stream": "stream.ring0.execution-events", "seq": 42},
        )
        is True
    )
    assert journal.replay_checkpoint({"logbook": []}, checkpoint) is False
    assert (
        journal.replay_checkpoint(
            journal.snapshot(),
            checkpoint,
            event_boundary={"stream": "stream.ring0.execution-events", "seq": 43},
        )
        is False
    )
