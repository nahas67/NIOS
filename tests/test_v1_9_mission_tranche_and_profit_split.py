"""V1.9 Suite 28 -- multi-tranche mission, profit split and capital mobility.

Covers ``A1``-``A8`` and ``C1``:

* the immutable mission contract and the three-state operator workflow;
* the half-Kelly feasibility gate and ``422 INFEASIBLE_MISSION``;
* per-tranche stop isolation (one tranche halts, nothing else does);
* the correlation independence gate (``N_eff``, 2% buckets, 6% heat);
* goal pacing, best-day cap and ``TARGET_LOCK``;
* the dynamic integer-micro profit split with a monotonic reserve floor;
* physical crypto wallet sweeps and virtual India/US/FX margin quotas;
* the ``/api/v2/missions`` and ``/api/v2/tranches`` control surfaces.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.schemas import (  # noqa: E402
    MissionAuthorization,
    MissionInfeasibleError,
    MissionSpec,
    SplitPolicy,
    TrancheRiskState,
    TrancheStatus,
    VaultName,
)
from core.shm_ipc import TrancheLatchTable  # noqa: E402
from ring0_vault.ibor.ledger_outbox import (  # noqa: E402
    CrashAtomicOutbox,
    DynamicFundSplitEngine,
    IBORLedger,
    LedgerStore,
    SiloGuard,
    TrancheGuard,
)
from ring0_vault.kernel.firewalls_and_oms import (  # noqa: E402
    TrancheExposure,
    TrancheIndependenceGate,
)
from ring0_vault.kernel.mission_pacing import MissionPacingController  # noqa: E402
from ring0_vault.silos.crypto_wallet_sweep import (  # noqa: E402
    CryptoWalletSweeper,
    SweepRejected,
)
from ring0_vault.silos.virtual_margin_quota import (  # noqa: E402
    QuotaRejected,
    VirtualMarginQuotaEngine,
)
from ring2_brain.quant.mission_engine import MissionFeasibilityEngine  # noqa: E402
from ring3_cockpit.gateway.mission_control import (  # noqa: E402
    MissionControlError,
    MissionControlSurface,
)

DAY_NS = 86_400_000_000_000


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ledger() -> IBORLedger:
    store = LedgerStore(in_memory=True, probe=False)
    book = IBORLedger(store, silos=["US_IBKR", "CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN"])
    book.open_silo("US_IBKR", funding_micros=1_000_000_000)
    book.fund_vault("US_IBKR", VaultName.RESERVE_YIELD, 500_000_000)
    book.fund_vault("US_IBKR", VaultName.SPINAL_SCALPER, 100_000_000)
    book.open_silo("CRYPTO_BINANCE", funding_micros=1_000_000_000)
    book.fund_vault("CRYPTO_BINANCE", VaultName.SPINAL_SCALPER, 200_000_000)
    book.open_silo("INDIA_ZERODHA_DHAN", funding_micros=500_000_000)
    book.fund_vault("INDIA_ZERODHA_DHAN", VaultName.RESERVE_YIELD, 200_000_000)
    yield book
    book.close()


def _approval(now: int, silos=("US_IBKR",), cap: int = 50_000_000) -> MissionAuthorization:
    return MissionAuthorization.mint(
        "sovereign", list(silos), cap, ttl_seconds=3600, now_ns=now
    )


def _mission(
    now: int,
    *,
    mission_id: str = "mission-alpha",
    target: int = 1_000_000,
    risk: int = 200_000,
    days: int = 30,
    silos=("US_IBKR",),
) -> MissionSpec:
    return MissionSpec(
        mission_id=mission_id,
        target_micros=target,
        deadline_utc=now + days * DAY_NS,
        risk_budget_micros=risk,
        approval=_approval(now, silos),
        created_at_ns=now,
    )


# ---------------------------------------------------------------------------
# A3 / F4 -- mission contract, authorization and feasibility
# ---------------------------------------------------------------------------


def test_mission_spec_and_authorization_contract() -> None:
    now = time.time_ns()
    approval = _approval(now)
    mission = _mission(now)
    assert mission.approval is not None
    assert mission.approval.state == "APPROVE"
    assert mission.approval.is_live(now)
    assert mission.approval.covers("US_IBKR")
    assert mission.authorized("US_IBKR", 1_000_000, now_ns=now)
    assert not mission.authorized("CRYPTO_BINANCE", 1_000_000, now_ns=now)
    assert not mission.authorized("US_IBKR", 999_000_000, now_ns=now)

    # The three-state workflow is explicit and the signature is scope-bound.
    assert approval.revoked().state == "REVOKE"
    assert not approval.revoked().is_live(now)
    assert MissionAuthorization.mint(
        "sovereign", ["US_IBKR"], 50_000_000, ttl_seconds=3600, state="OVERRIDE", now_ns=now
    ).state == "OVERRIDE"
    with pytest.raises(ValueError):
        MissionAuthorization.mint(
            "sovereign", ["US_IBKR"], 50_000_000, ttl_seconds=3600, state="MAYBE"
        )
    with pytest.raises(ValueError):
        MissionAuthorization(
            operator="sovereign",
            silos=("US_IBKR",),
            notional_cap_micros=50_000_000,
            ttl_seconds=3600,
            issued_at_ns=now,
            signature="0" * 64,
        )

    with pytest.raises(ValueError):
        MissionSpec(
            mission_id="bad",
            target_micros=100,
            deadline_utc=now - 1,
            risk_budget_micros=10,
            created_at_ns=now,
        )
    with pytest.raises(ValueError):
        MissionSpec(
            mission_id="bad",
            target_micros=100,
            deadline_utc=now + DAY_NS,
            risk_budget_micros=200,
            created_at_ns=now,
        )
    with pytest.raises(ValueError):
        MissionSpec(
            mission_id="bad",
            target_micros=100,
            deadline_utc=now + DAY_NS,
            risk_budget_micros=10,
            kelly_fraction_cap=0.9,
            created_at_ns=now,
        )


def test_feasibility_gate_rejects_suicidal_and_accepts_reachable() -> None:
    now = time.time_ns()
    engine = MissionFeasibilityEngine()
    mission = _mission(now)
    zero_equity = engine.assess(mission, equity_micros=0, now_ns=now)
    assert zero_equity.code == "INFEASIBLE_MISSION"
    assert "ZERO_EQUITY" in zero_equity.reasons
    with pytest.raises(MissionInfeasibleError):
        engine.require_feasible(mission, equity_micros=0, now_ns=now)

    strong = MissionFeasibilityEngine(residuals_bps=[30.0] * 180 + [25.0] * 20)
    reachable = MissionSpec(
        mission_id="mission-reachable",
        target_micros=1_000_000,
        deadline_utc=now + 365 * DAY_NS,
        risk_budget_micros=500_000,
        approval=_approval(now),
        created_at_ns=now,
    )
    verdict = strong.assess(reachable, equity_micros=1_000_000_000, now_ns=now)
    assert verdict.code == "FEASIBLE", verdict.reasons
    assert verdict.p_reach >= 0.55
    assert verdict.capped_kelly_fraction <= reachable.kelly_fraction_cap
    # Determinism: the same inputs produce the same verdict.
    again = strong.assess(reachable, equity_micros=1_000_000_000, now_ns=now)
    assert again.to_dict() == verdict.to_dict()


def test_mission_control_surface_enforces_feasibility_and_revocation(ledger: IBORLedger) -> None:
    now = time.time_ns()
    surface = MissionControlSurface(ledger=ledger, feasibility=MissionFeasibilityEngine())
    with pytest.raises(MissionControlError) as infeasible:
        surface.create(
            mission_id="impossible",
            target_units=1_000.0,
            deadline_utc=now + DAY_NS,
            risk_budget_units=10.0,
            operator="sovereign",
            silos=["US_IBKR"],
            notional_cap_units=10.0,
            now_ns=now,
        )
    assert infeasible.value.code == "INFEASIBLE_MISSION"
    assert infeasible.value.http_status == 422

    open_surface = MissionControlSurface(ledger=ledger)
    created = open_surface.create(
        mission_id="mission-open",
        target_units=1.0,
        deadline_utc=now + 30 * DAY_NS,
        risk_budget_units=0.1,
        operator="sovereign",
        silos=["US_IBKR"],
        notional_cap_units=10.0,
        now_ns=now,
    )
    assert created["mission_id"] == "mission-open"
    assert created["approval_state"] == "APPROVE"
    assert open_surface.list_missions(now_ns=now)["count"] == 1
    approved = open_surface.approve(
        "mission-open", operator="sovereign", state="OVERRIDE", now_ns=now
    )
    assert approved["approval_state"] == "OVERRIDE"
    revoked = open_surface.revoke("mission-open", reason="thesis invalid", now_ns=now)
    assert revoked["revoked"] is True
    assert revoked["approval_state"] == "REVOKE"
    with pytest.raises(MissionControlError) as missing:
        open_surface.mission_status("nope")
    assert missing.value.code == "UNKNOWN_MISSION"


# ---------------------------------------------------------------------------
# A1 / A2 / A8 -- tranche isolation and correlation independence
# ---------------------------------------------------------------------------


def test_tranche_stop_halts_only_that_tranche(ledger: IBORLedger) -> None:
    now = time.time_ns()
    silo_guard = SiloGuard()
    latch = TrancheLatchTable(reset=True)
    try:
        guard = TrancheGuard(ledger, latch=latch, dll_micros=100_000_000)
        first = guard.open_tranche(
            "t-one", mission_id="m", silo="US_IBKR", allocated_micros=10_000_000, now_ns=now
        )
        second = guard.open_tranche(
            "t-two", mission_id="m", silo="US_IBKR", allocated_micros=10_000_000, now_ns=now
        )
        assert first.stop_micros > 0
        breach = guard.record_realized("t-one", -first.stop_micros, reason="stop test")
        assert breach.halted is True
        assert breach.global_halt is False
        assert guard.is_halted("t-one") is True
        assert guard.is_halted("t-two") is False
        assert latch.is_halted("t-one") is True
        assert latch.is_halted("t-two") is False
        assert silo_guard.is_frozen("US_IBKR") is False
        assert second.status is TrancheStatus.ACTIVE

        # A human re-arm clears exactly one slot.
        assert guard.rearm("t-one") is True
        assert guard.is_halted("t-one") is False
        assert latch.is_halted("t-one") is False
        assert guard.snapshot()["global_halt"] is False
    finally:
        latch.close()
        latch.unlink()


def test_tranche_risk_state_never_lets_unrealized_raise_hwm() -> None:
    state = TrancheRiskState(
        tranche_id="t",
        mission_id="m",
        silo="US_IBKR",
        allocated_micros=1_000_000,
        stop_micros=100_000,
    )
    state.record_realized(50_000)
    assert state.trailing_hwm_micros == 50_000
    state.record_realized(-20_000)
    assert state.trailing_hwm_micros == 50_000
    assert state.giveback_micros == 20_000
    assert state.stop_breached is False
    state.record_realized(-80_000)
    assert state.stop_breached is True


def test_independence_gate_effective_bets_and_caps() -> None:
    gate = TrancheIndependenceGate()
    independent = gate.evaluate(
        [
            TrancheExposure("a", "US_IBKR", "LONG", 0.01),
            TrancheExposure("b", "CRYPTO_BINANCE", "SHORT", 0.01, {"a": 0.0}),
        ]
    )
    assert independent.allowed is True
    assert independent.n_eff == pytest.approx(2.0, abs=1e-6)

    correlated = gate.evaluate(
        [
            TrancheExposure("a", "US_IBKR", "LONG", 0.03, {"b": 0.9}),
            TrancheExposure("b", "US_IBKR", "LONG", 0.03, {"a": 0.9}),
        ]
    )
    assert correlated.allowed is False
    assert correlated.code == "CORRELATED_TRANCHE_HEAT_EXCEEDED"
    assert correlated.n_eff < 1.2

    # Same correlation but opposite sides are not one bet.
    hedged = gate.evaluate(
        [
            TrancheExposure("a", "US_IBKR", "LONG", 0.02, {"b": 0.95}),
            TrancheExposure("b", "US_IBKR", "SHORT", 0.02, {"a": 0.95}),
        ]
    )
    assert hedged.allowed is True

    overheated = gate.evaluate(
        [
            TrancheExposure("a", "US_IBKR", "LONG", 0.02),
            TrancheExposure("b", "CRYPTO_BINANCE", "LONG", 0.02),
            TrancheExposure("c", "FX_MT5", "LONG", 0.02),
            TrancheExposure("d", "INDIA_ZERODHA_DHAN", "SHORT", 0.02),
        ]
    )
    assert overheated.allowed is False
    assert "6%" in overheated.reason


# ---------------------------------------------------------------------------
# A4 / A5 -- pacing, target lock and the dynamic split
# ---------------------------------------------------------------------------


def test_pacing_throttles_and_latches_the_target() -> None:
    now = time.time_ns()
    mission = _mission(now, target=1_000, risk=100)
    pacing = MissionPacingController()
    pacing.register(mission, now_ns=now)
    assert pacing.sizing_multiplier(mission.mission_id) == 1.0

    pacing.record_realized(mission.mission_id, 350, now_ns=now)
    assert pacing.status(mission.mission_id)["progress"] == pytest.approx(0.35)
    # Best-day cap: the second 350 on the same UTC day is refused.
    state = pacing.record_realized(mission.mission_id, 350, now_ns=now)
    assert state.day_cap_reached is True
    assert pacing.can_open_risk(mission.mission_id, now_ns=now) is False

    pacing.record_realized(mission.mission_id, 350, now_ns=now + DAY_NS)
    pacing.record_realized(mission.mission_id, 100, now_ns=now + 2 * DAY_NS)
    assert pacing.status(mission.mission_id)["progress"] == pytest.approx(0.80)
    assert pacing.sizing_multiplier(mission.mission_id) == pytest.approx(0.25)

    pacing.record_realized(mission.mission_id, 200, now_ns=now + 3 * DAY_NS)
    assert pacing.is_target_locked(mission.mission_id) is True
    assert pacing.can_open_risk(mission.mission_id, now_ns=now + 4 * DAY_NS) is False
    assert pacing.status(mission.mission_id)["reason"] == "TARGET_LOCK"

    # Unrealized P&L never raises the high-water mark.
    before = pacing.status(mission.mission_id)["high_water_micros"]
    pacing.record_unrealized(mission.mission_id, 999_999)
    assert pacing.status(mission.mission_id)["high_water_micros"] == before


def test_dynamic_split_is_exact_and_reserve_floor_is_monotonic(ledger: IBORLedger) -> None:
    engine = DynamicFundSplitEngine(ledger)
    splits = engine.split(1_000_003, SplitPolicy())
    assert splits["long_term_micros"] + splits["reserve_micros"] + splits[
        "tax_escrow_micros"
    ] + splits["scalper_micros"] == 1_000_003

    first = engine.apply("US_IBKR", 1_000_000, policy=SplitPolicy(mission_progress=0.1))
    assert first.applied is True
    assert first.splits_sum_micros == 1_000_000
    assert first.cross_border_wire_bps == 0.0
    floor_after_first = engine.reserve_floor_micros
    assert floor_after_first > 0

    second = engine.apply(
        "US_IBKR", 1_000_000, policy=SplitPolicy(mission_progress=0.9, regime="CONTRACTION")
    )
    assert second.reserve_pct >= first.reserve_pct
    assert engine.reserve_floor_micros >= floor_after_first
    assert second.tax_escrow_micros > 0
    assert ledger.balance("US_IBKR", "TAX_ESCROW") > 0
    assert ledger.invariant_report()["holds"] is True

    losing = engine.apply("US_IBKR", -5_000, policy=SplitPolicy())
    assert losing.applied is False
    assert losing.splits_sum_micros == 0


# ---------------------------------------------------------------------------
# C1 -- physical crypto sweeps and virtual margin quotas
# ---------------------------------------------------------------------------


def test_crypto_wallet_sweep_moves_capital_after_venue_ack(ledger: IBORLedger) -> None:
    from ring0_vault.kernel.firewalls_and_oms import OrderSigner

    outbox = CrashAtomicOutbox(ledger)
    sweeper = CryptoWalletSweeper(ledger, outbox=outbox, signer=OrderSigner())
    intent = sweeper.plan("CRYPTO_BINANCE", 10_000_000)
    assert intent.from_wallet == "SPOT"
    assert intent.to_wallet == "UMFUTURE_MAIN"
    assert intent.transfer_type() == 1
    receipt = sweeper.execute(intent)
    assert receipt.state == "COMMITTED", receipt.reason
    assert receipt.venue_ack["offline"] is True
    assert receipt.signature
    status = sweeper.status()
    assert status["allow_network"] is False
    assert status["committed"] == 1

    # A hot kill latch voids before any capital is touched.
    class HotLatch:
        is_halted = True

    halted = CryptoWalletSweeper(ledger, kill_latch=HotLatch())
    blocked = halted.execute(halted.plan("CRYPTO_BINANCE", 1_000_000))
    assert blocked.state == "VOIDED"
    assert blocked.reason == "KILL_LATCH_HOT"

    with pytest.raises(SweepRejected):
        sweeper.plan("US_IBKR", 1_000_000)


def test_virtual_margin_quota_moves_no_funds_and_settles_t_plus_one(ledger: IBORLedger) -> None:
    engine = VirtualMarginQuotaEngine(ledger)
    before = {
        str(vault): ledger.balance("US_IBKR", str(vault)) for vault in VaultName
    }
    quota = engine.issue("US_IBKR", 1_000_000)
    after = {str(vault): ledger.balance("US_IBKR", str(vault)) for vault in VaultName}
    assert before == after
    assert quota.status == "ACTIVE"
    assert engine.verify(quota.token) is not None
    assert engine.active_micros("US_IBKR") == 1_000_000
    assert quota.available_at_utc > quota.issued_at_ns // 1_000_000_000
    assert engine.status()["no_funds_moved"] is True

    assert engine.revoke(quota.quota_id) is True
    assert engine.verify(quota.token) is None
    assert engine.revoke(quota.quota_id) is False

    with pytest.raises(QuotaRejected) as unregulated:
        engine.issue("CRYPTO_BINANCE", 1_000_000)
    assert unregulated.value.code == "SILO_NOT_REGULATED"

    with pytest.raises(QuotaRejected) as oversized:
        engine.issue("US_IBKR", 900_000_000_000)
    assert oversized.value.code == "QUOTA_EXCEEDS_RESERVE"

    class HotLatch:
        is_halted = True

    with pytest.raises(QuotaRejected) as halted:
        VirtualMarginQuotaEngine(ledger, kill_latch=HotLatch()).issue("US_IBKR", 1_000)
    assert halted.value.code == "KILL_LATCH_HOT"


# ---------------------------------------------------------------------------
# REST surfaces
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    from core.orchestrator import NIOSCoreOrchestrator

    workspace = tmp_path_factory.mktemp("nios-v19-mission")
    vault = tmp_path_factory.mktemp("nios-v19-mission-vault")
    instance = NIOSCoreOrchestrator(workspace=workspace, key_vault_root=vault, clean_slate=True)
    try:
        yield instance
    finally:
        try:
            instance.close()
        except Exception:  # pragma: no cover - teardown is best effort
            pass


@pytest.fixture(scope="module")
def client(runtime):
    from fastapi.testclient import TestClient

    from ring3_cockpit.gateway.control_plane import create_app

    with TestClient(create_app(runtime.control)) as test_client:
        yield test_client


def test_mission_routes_create_list_allocate_rearm_and_revoke(client, runtime) -> None:
    # V1.9 PART 1 made MissionFeasibilityEngine default-on, so a zero-equity
    # clean slate can no longer reach a 1.00 target in 30 days -- which is the
    # whole point of the gate.  Fund the silo with realistic paper capital for
    # the duration of this route test, then hand the workspace back untouched.
    funded = client.post(
        "/api/v2/control/ledger/fund-paper",
        json={
            "silo": "US_IBKR",
            "amount_units": 100_000.0,
            "vault": "LongTermCompounder",
        },
    )
    assert funded.status_code == 200, funded.text
    now = time.time_ns()
    payload = {
        "mission_id": "route-mission",
        # A realistic paper mission: 100.00 profit on 100,000.00 of capital in
        # 30 days, with the whole profit budget at risk.  The default engine
        # scores this 0.834 reach / 0.166 ruin against the 200-trade window, so
        # the route exercises a feasible mission without weakening half-Kelly.
        "target_units": 100.0,
        "deadline_utc": now + 30 * DAY_NS,
        "risk_budget_units": 100.0,
        "operator": "sovereign",
        "silos": ["US_IBKR"],
        "notional_cap_units": 10.0,
    }
    created = client.post("/api/v2/missions", json=payload)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["mission_id"] == "route-mission"
    assert body["approval_state"] == "APPROVE"

    listed = client.get("/api/v2/missions")
    assert listed.status_code == 200
    assert any(row["mission_id"] == "route-mission" for row in listed.json()["missions"])

    status = client.get("/api/v2/missions/route-mission")
    assert status.status_code == 200
    assert status.json()["approval_live"] is True

    approved = client.post(
        "/api/v2/missions/route-mission/approve",
        json={"operator": "sovereign", "state": "OVERRIDE", "ttl_seconds": 7200},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval_state"] == "OVERRIDE"

    tranche = client.post(
        "/api/v2/missions/route-mission/tranches",
        json={"silo": "US_IBKR", "allocated_units": 0.5, "side": "LONG"},
    )
    assert tranche.status_code == 200, tranche.text
    tranche_id = tranche.json()["tranche_id"]
    assert tranche.json()["status"] == "ACTIVE"

    listed_tranches = client.get("/api/v2/tranches")
    assert listed_tranches.status_code == 200
    assert listed_tranches.json()["count"] >= 1

    # Force a halt through the Ring 0 guard, then re-arm through the route.
    guard = runtime.control.missions.tranche_guard
    guard.halt(tranche_id, reason="suite-28")
    halted = client.get(f"/api/v2/tranches/{tranche_id}")
    assert halted.status_code == 200
    assert halted.json()["status"] == "HALTED"
    rearmed = client.post(f"/api/v2/tranches/{tranche_id}/rearm", json={"operator": "sovereign"})
    assert rearmed.status_code == 200, rearmed.text
    assert rearmed.json()["rearmed"] is True
    assert rearmed.json()["status"] == "ACTIVE"

    revoked = client.post(
        "/api/v2/missions/route-mission/revoke",
        json={"operator": "sovereign", "reason": "done"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["approval_state"] == "REVOKE"
    assert tranche_id in revoked.json()["halted_tranches"]

    missing = client.get("/api/v2/missions/absent")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "UNKNOWN_MISSION"

    clock = client.get("/api/v2/cockpit/clock")
    assert clock.status_code == 200
    assert clock.json()["synthetic"] is False

    # Hand the module fixture back as a clean slate: later tests assert the
    # untouched workspace, and the funding above was scoped to this test.
    reset = client.post("/api/v2/control/workspace/reset", json={})
    assert reset.status_code == 200, reset.text
    assert reset.json()["clean_slate_active"] is True


def test_mission_route_rejects_infeasible_target(client, runtime) -> None:
    control = runtime.control
    control._mission_surface = None
    control._mission_feasibility = MissionFeasibilityEngine()
    try:
        now = time.time_ns()
        response = client.post(
            "/api/v2/missions",
            json={
                "mission_id": "route-impossible",
                "target_units": 1000.0,
                "deadline_utc": now + DAY_NS,
                "risk_budget_units": 10.0,
                "operator": "sovereign",
                "silos": ["US_IBKR"],
                "notional_cap_units": 10.0,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "INFEASIBLE_MISSION"
    finally:
        control._mission_surface = None
        # V1.9 PART 1: never leave the shared control plane with feasibility
        # switched off -- that is the failure mode the default-on wiring fixes.
        control._mission_feasibility = MissionFeasibilityEngine()


def test_clean_slate_workspace_has_no_demo_mission(runtime) -> None:
    assert runtime.control.autopilot_state()["enabled"] is False
    assert runtime.ledger.invariant_report()["holds"] is True
    assert runtime.control.clock_health()["synthetic"] is False
    assert os.path.isdir(tempfile.gettempdir())
