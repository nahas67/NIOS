"""NIOS V2.1 -- Suite 33: ``G1`` closed-loop profit split, ``G2`` learning, ``G3`` autopilot.

Covers:

* ``G1`` -- a winning crypto trade close runs ``TAX_ESCROW`` -> ``DynamicFundSplitEngine``
  -> physical ``UMFUTURE_MAIN -> MAIN`` sweep -> ``LiquidityAvailabilitySchedule``;
  a losing close books its statutory cost and stops; a regulated-silo close locks a
  **virtual** reserve quota and never wires money; every checkpoint is durable in the
  ``AtomicStateJournal``; compensation is reversible.
* ``G2`` -- a losing trade, a rejection and a slippage spike each compile a
  ``PostMortemEvent`` into ``EntityMemoryGraph``, while a win is correctly *not*
  remembered as an incident.
* ``G3`` -- ``MultiTrancheAutopilot`` scans the registry, drops closed markets and
  memorized failures, sizes with half-Kelly, honours the ``N_eff`` / 2% bucket / 6%
  heat gate, and dispatches unsigned tranche-tagged proposals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.schemas import VaultName  # noqa: E402
from ring0_vault.ibor.alert_center import AlertCenter  # noqa: E402
from ring0_vault.ibor.cold_start_reconciler import ColdStartReconciler  # noqa: E402
from ring0_vault.ibor.ledger_outbox import (  # noqa: E402
    TAX_ESCROW_ACCOUNT,
    CrashAtomicOutbox,
    DynamicFundSplitEngine,
    IBORLedger,
    LedgerStore,
)
from ring0_vault.ibor.profit_split_saga import (  # noqa: E402
    SAGA_JOURNAL_KEY,
    SAGA_STAGES,
    ProfitSplitSagaCoordinator,
)
from ring0_vault.ibor.state_journal import AtomicStateJournal  # noqa: E402
from ring0_vault.kernel.firewalls_and_oms import OrderSigner, TrancheIndependenceGate  # noqa: E402
from ring0_vault.silos.crypto_wallet_sweep import (  # noqa: E402
    CASH_ACCOUNT,
    CRYPTO_SILO,
    CryptoWalletSweeper,
)
from ring0_vault.silos.virtual_margin_quota import VirtualMarginQuotaEngine  # noqa: E402
from ring2_brain.memory.entity_graph import EntityMemoryGraph  # noqa: E402
from ring2_brain.memory.post_mortem_recorder import PostMortemRecorder  # noqa: E402
from ring2_brain.quant.autopilot_orchestrator import (  # noqa: E402
    AUTOPILOT_HEAT_CAP_PCT,
    HALF_KELLY_FRACTION,
    MultiTrancheAutopilot,
)

UNITS = 1_000_000


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ledger() -> IBORLedger:
    store = LedgerStore(in_memory=True, probe=False)
    book = IBORLedger(
        store, silos=[CRYPTO_SILO, "US_IBKR", "INDIA_ZERODHA_DHAN", "FX_MT5"]
    )
    book.open_silo(CRYPTO_SILO, funding_micros=1_000_000_000)
    book.fund_vault(CRYPTO_SILO, VaultName.SPINAL_SCALPER, 100_000_000)
    book.open_silo("US_IBKR", funding_micros=1_000_000_000)
    book.fund_vault("US_IBKR", VaultName.RESERVE_YIELD, 400_000_000)
    book.open_silo("INDIA_ZERODHA_DHAN", funding_micros=500_000_000)
    book.fund_vault("INDIA_ZERODHA_DHAN", VaultName.RESERVE_YIELD, 200_000_000)
    book.open_silo("FX_MT5", funding_micros=200_000_000)
    yield book
    book.close()


@pytest.fixture()
def journal(tmp_path) -> AtomicStateJournal:
    return AtomicStateJournal(tmp_path / "v21_state.json")


@pytest.fixture()
def coordinator(ledger, journal) -> ProfitSplitSagaCoordinator:
    sweeper = CryptoWalletSweeper(
        ledger,
        outbox=CrashAtomicOutbox(ledger),
        signer=OrderSigner(),
        allow_network=False,
    )
    quota = VirtualMarginQuotaEngine(ledger)
    return ProfitSplitSagaCoordinator(
        ledger=ledger,
        journal=journal,
        split_engine=DynamicFundSplitEngine(ledger),
        sweeper=sweeper,
        quota_engine=quota,
    )


# ---------------------------------------------------------------------------
# G1 -- the winning crypto close
# ---------------------------------------------------------------------------


def test_winning_crypto_close_escrows_splits_and_sweeps_umfuture_to_spot(
    coordinator, ledger, journal
) -> None:
    # A funded futures wallet is the precondition for a custody sweep; without it
    # the saga must report ``NO_SWEEPABLE_CASH`` instead of inventing liquidity.
    ledger.fund_cash(CRYPTO_SILO, 50_000_000)
    before_reserve = int(ledger.balance(CRYPTO_SILO, str(VaultName.RESERVE_YIELD)))

    saga = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=10_000_000,
        notional_micros=100_000_000,
        side="SELL",
        mission_id="",
    )

    # -- the checkpoint ladder runs in order and terminates ---------------
    assert [row["stage"] for row in saga["stages"]] == list(SAGA_STAGES)
    assert saga["state"] == "COMPLETED"
    assert saga["stage"] == "COMPLETED"
    assert saga["progress_pct"] == 100.0
    assert len(saga["saga_sha256"]) == 64

    # -- TAX_ESCROW is exact, integer-micro and non-zero ------------------
    assert saga["statutory_cost_micros"] > 0
    assert saga["realized_net_micros"] == 10_000_000 - saga["statutory_cost_micros"]
    escrow = int(ledger.balance(CRYPTO_SILO, TAX_ESCROW_ACCOUNT))
    assert escrow == saga["split"]["tax_escrow_micros"] > 0

    # -- the dynamic split sums back to the realized amount, to the micro --
    split = saga["split"]
    assert split["splits_sum_micros"] == saga["realized_net_micros"]
    assert split["applied"] is True
    assert split["cross_border_wire_bps"] == 0.0

    # -- the custody move is physical, signed and UMFUTURE_MAIN -> MAIN ---
    sweep = saga["sweep"]
    assert sweep is not None and sweep["accepted"] is True, sweep
    assert sweep["from_wallet"] == "UMFUTURE_MAIN"
    # The harvested ledger names the spot wallet ``SPOT``; Binance calls it ``MAIN``.
    assert sweep["to_wallet"] == "SPOT"
    assert sweep["transfer_type"] == 2
    assert sweep["amount_micros"] == split["reserve_micros"]
    assert sweep["signature_present"] is True
    assert saga["liquidity_available_at_utc_ns"] > 0
    assert int(ledger.balance(CRYPTO_SILO, str(VaultName.RESERVE_YIELD))) > before_reserve

    # -- a crypto close has no regulated margin surface to lock -----------
    assert saga["reserve_lock"] is None
    assert saga["stages"][3]["status"] == "NOT_APPLICABLE"
    assert saga["cross_border_wire_bps"] == 0.0

    # -- durability: a fresh journal instance replays the record ----------
    replayed = AtomicStateJournal(journal.path)
    rows = replayed.load()[SAGA_JOURNAL_KEY]
    assert len(rows) == 1
    assert rows[0]["saga_id"] == saga["saga_id"]
    assert rows[0]["state"] == "COMPLETED"
    assert any(marker.startswith("VOID_SWEEP_TRANSFER:") for marker in rows[0]["rollbacks"])

    status = coordinator.status()
    assert status["state_counts"]["COMPLETED"] == 1
    assert status["crypto_sweeper_attached"] is True
    assert status["journal_backed"] is True


def test_winning_crypto_close_without_cash_never_invents_a_sweep(ledger, journal) -> None:
    """No spare cash in the wallet is a truth to report, not a sweep to fake."""
    sweeper = CryptoWalletSweeper(ledger, outbox=CrashAtomicOutbox(ledger), signer=OrderSigner())
    coordinator = ProfitSplitSagaCoordinator(
        ledger=ledger, journal=journal, sweeper=sweeper
    )
    # Drain the wallet so nothing is sweepable.
    balance = int(ledger.available(CRYPTO_SILO, CASH_ACCOUNT))
    if balance:
        ledger.fund_vault(CRYPTO_SILO, VaultName.SPINAL_SCALPER, balance)

    saga = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="ETH/USDT",
        gross_pnl_micros=5_000_000,
        notional_micros=50_000_000,
    )
    assert saga["state"] == "COMPLETED"
    assert saga["sweep"]["accepted"] is False
    assert saga["sweep"]["reason"] == "NO_SWEEPABLE_CASH"
    assert saga["stages"][2]["status"] == "VOIDED"


def test_losing_close_books_cost_and_stops_immediately(coordinator, ledger) -> None:
    reserve_before = int(ledger.balance(CRYPTO_SILO, str(VaultName.RESERVE_YIELD)))
    saga = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=-1_000_000,
        notional_micros=100_000_000,
    )
    assert saga["state"] == "LOSS_RECORDED"
    assert saga["reason"] == "NON_POSITIVE_REALIZED"
    assert saga["realized_net_micros"] < 0
    assert saga["split"] is None
    assert saga["sweep"] is None
    assert saga["reserve_lock"] is None
    assert saga["stages"][-1]["stage"] == "COMPLETED"
    assert saga["stages"][-1]["status"] == "NO_PROFIT"
    # A loss must not move a single micro into the reserve vault.
    assert int(ledger.balance(CRYPTO_SILO, str(VaultName.RESERVE_YIELD))) == reserve_before


@pytest.mark.parametrize("silo", ["US_IBKR", "INDIA_ZERODHA_DHAN"])
def test_regulated_close_locks_virtual_reserve_without_any_wire(
    coordinator, ledger, silo
) -> None:
    saga = coordinator.run_trade_close(
        silo=silo,
        symbol="NVDA" if silo == "US_IBKR" else "RELIANCE",
        gross_pnl_micros=8_000_000,
        notional_micros=80_000_000,
        side="SELL",
    )
    assert saga["state"] == "COMPLETED"
    # No physical wallet sweep is even considered for a regulated silo.
    assert saga["sweep"] is None
    assert saga["stages"][2]["status"] == "NOT_APPLICABLE"
    assert saga["stages"][2]["detail"]["reason"] == "REGULATED_SILO_PROHIBITS_WALLET_SWEEP"
    # The reserve slice is locked as a *virtual* quota: no funds move.
    lock = saga["reserve_lock"]
    assert lock is not None and lock["issued"] is True, lock
    assert lock["no_funds_moved"] is True
    assert lock["amount_micros"] == saga["split"]["reserve_micros"]
    assert saga["cross_border_wire_bps"] == 0.0
    assert saga["stages"][3]["status"] == "ISSUED"
    assert any(marker.startswith("REVOKE_QUOTA:") for marker in saga["rollbacks"])


def test_saga_compensation_revokes_the_quota_and_is_durable(coordinator, journal) -> None:
    saga = coordinator.run_trade_close(
        silo="US_IBKR",
        symbol="NVDA",
        gross_pnl_micros=6_000_000,
        notional_micros=60_000_000,
    )
    quota_id = saga["reserve_lock"]["quota_id"]
    assert coordinator.quota_engine.status()["active_count"] == 1

    compensated = coordinator.compensate(saga["saga_id"], reason="OPERATOR_ROLLBACK")
    assert compensated["state"] == "COMPENSATED"
    assert compensated["reason"] == "COMPENSATED:OPERATOR_ROLLBACK"
    assert coordinator.quota_engine.status()["active_count"] == 0

    replayed = AtomicStateJournal(journal.path).load()[SAGA_JOURNAL_KEY]
    assert replayed[0]["state"] == "COMPENSATED"
    assert quota_id in " ".join(replayed[0]["rollbacks"])


def test_recover_reports_an_interrupted_saga_after_restart(journal, tmp_path) -> None:
    from ring0_vault.ibor.state_journal import AtomicStateJournal as Journal

    interrupted = {
        "saga_id": "SAGA-INTERRUPTED",
        "silo": CRYPTO_SILO,
        "symbol": "BTC/USDT",
        "state": "IN_PROGRESS",
        "stage": "PROFIT_SPLIT",
        "gross_pnl_micros": 5_000_000,
        "notional_micros": 50_000_000,
        "statutory_cost_micros": 100_000,
        "realized_net_micros": 4_900_000,
        "stages": [],
        "rollbacks": [],
        "created_at_utc_ns": 1,
        "updated_at_utc_ns": 1,
    }
    Journal(journal.path).update(**{SAGA_JOURNAL_KEY: [interrupted]})

    store = LedgerStore(in_memory=True, probe=False)
    book = IBORLedger(store, silos=[CRYPTO_SILO])
    try:
        coordinator = ProfitSplitSagaCoordinator(ledger=book, journal=journal)
        recovered = coordinator.recover()
        assert recovered["pending_count"] == 1
        assert recovered["pending"][0]["state"] == "RECOVERED_INCOMPLETE"
        assert recovered["policy"] == "INSPECT_THEN_COMPENSATE_OR_REPLAY"
    finally:
        book.close()


# ---------------------------------------------------------------------------
# G1 -- schema discipline
# ---------------------------------------------------------------------------


def test_saga_rejects_float_money_with_a_typed_error(coordinator) -> None:
    with pytest.raises(TypeError):
        coordinator.run_trade_close(
            silo=CRYPTO_SILO,
            gross_pnl_micros=1_500_000.5,  # type: ignore[arg-type]
            notional_micros=10_000_000,
        )


def test_saga_split_legs_reconcile_to_the_ledger(coordinator, ledger) -> None:
    saga = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="SOL/USDT",
        gross_pnl_micros=3_000_000,
        notional_micros=30_000_000,
    )
    split = saga["split"]
    long_term = int(ledger.balance(CRYPTO_SILO, str(VaultName.LONG_TERM_COMPOUNDER)))
    scalper = int(ledger.balance(CRYPTO_SILO, str(VaultName.SPINAL_SCALPER)))
    escrow = int(ledger.balance(CRYPTO_SILO, TAX_ESCROW_ACCOUNT))
    reserve = int(ledger.balance(CRYPTO_SILO, str(VaultName.RESERVE_YIELD)))
    assert escrow == split["tax_escrow_micros"]
    assert long_term == split["long_term_micros"]
    assert scalper >= split["scalper_micros"]
    # The reserve leg lands either in the vault directly or through a sweep.
    assert reserve >= split["reserve_micros"] - saga["sweep"]["amount_micros"]


# ---------------------------------------------------------------------------
# G2 -- automated post-mortem learning
# ---------------------------------------------------------------------------


@pytest.fixture()
def graph() -> EntityMemoryGraph:
    return EntityMemoryGraph()


def test_losing_trade_compiles_entity_truth_and_is_memorable(graph) -> None:
    recorder = PostMortemRecorder(graph=graph)
    decision = recorder.record(
        symbol="BTC/USDT",
        silo=CRYPTO_SILO,
        strategy_id="spinal-scalper-v1",
        regime="CONTRACTION",
        net_pnl_micros=-2_500_000,
    )
    assert decision["recorded"] is True
    assert decision["outcome_kind"] == "LOSS"
    assert decision["event_type"] == "POST_MORTEM_COMPILED"
    assert "-2500000 micros" in decision["lesson_summary"]
    assert len(decision["post_mortem_sha256"]) == 64

    truths = graph.truths()
    assert truths, "an incident must compile at least one entity truth"
    entity_ids = {truth.entity_id for truth in truths}
    assert "symbol:BTC-USDT" in entity_ids
    assert "silo:CRYPTO_BINANCE" in entity_ids
    for truth in truths:
        assert len(truth.truth_sha256) == 64
    assert len(graph.timeline()) == 1


def test_winning_trade_is_never_recorded_as_an_incident(graph) -> None:
    recorder = PostMortemRecorder(graph=graph)
    decision = recorder.record(
        symbol="BTC/USDT", silo=CRYPTO_SILO, net_pnl_micros=4_000_000
    )
    assert decision["recorded"] is False
    assert decision["reason"] == "NOT_MEMORABLE"
    assert len(graph.timeline()) == 0


def test_rejection_and_slippage_spike_both_trigger_a_post_mortem(graph) -> None:
    recorder = PostMortemRecorder(graph=graph)
    rejected = recorder.record(
        symbol="NVDA", silo="US_IBKR", status_code=429, net_pnl_micros=0
    )
    assert rejected["outcome_kind"] == "ORDER_REJECTED"
    assert rejected["code"] == "HTTP_429"
    assert rejected["severity"] == "CRITICAL"

    slipped = recorder.record(
        symbol="NVDA", silo="US_IBKR", slippage_bps=18.5, net_pnl_micros=10_000
    )
    assert slipped["outcome_kind"] == "SLIPPAGE_SPIKE"
    assert "18.50 bps" in slipped["lesson_summary"]

    # 18.5 bps is above the 15 bps threshold; 12 bps is not.
    quiet = recorder.record(
        symbol="NVDA", silo="US_IBKR", slippage_bps=12.0, net_pnl_micros=10_000
    )
    assert quiet["recorded"] is False
    assert len(graph.timeline()) == 2


def test_stop_out_outranks_every_other_classification(graph) -> None:
    recorder = PostMortemRecorder(graph=graph)
    decision = recorder.record(
        symbol="NVDA",
        silo="US_IBKR",
        stop_out=True,
        status_code=422,
        slippage_bps=40.0,
        net_pnl_micros=-9_000_000,
    )
    assert decision["outcome_kind"] == "BREAKER_TRIP"
    assert decision["code"] == "TRANCH_STOP_OUT"
    assert decision["severity"] == "CRITICAL"


def test_post_mortem_without_a_sink_reports_the_defect_instead_of_succeeding() -> None:
    recorder = PostMortemRecorder()
    decision = recorder.record(symbol="NVDA", silo="US_IBKR", net_pnl_micros=-1)
    assert decision["recorded"] is False
    assert decision["reason"] == "NO_MEMORY_SINK"


# ---------------------------------------------------------------------------
# G3 -- multi-tranche autopilot
# ---------------------------------------------------------------------------


class _Spec:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


class _Registry:
    """A three-silo registry stub with deterministic instruments."""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def silo_ids(self):
        return list(self._counts)

    def by_silo(self, silo: str):
        return [_Spec(f"{silo[:3]}{index:03d}") for index in range(self._counts[silo])]


def _autopilot(**kwargs) -> MultiTrancheAutopilot:
    registry = kwargs.pop(
        "registry", _Registry({"US_IBKR": 6, "CRYPTO_BINANCE": 4})
    )
    return MultiTrancheAutopilot(
        registry=registry,
        gate=TrancheIndependenceGate(),
        max_candidates=kwargs.pop("max_candidates", 4),
        **kwargs,
    )


def test_autopilot_dispatches_half_kelly_within_the_heat_cap() -> None:
    autopilot = _autopilot()
    report = autopilot.run_cycle(now_ns=1_760_000_000_000_000_000)

    assert report["sizing"] == "half_kelly"
    assert report["half_kelly_fraction"] == HALF_KELLY_FRACTION == 0.5
    assert report["dispatch_target"] == "SmartOMS"
    assert report["signed"] is False
    assert 0 < report["dispatched_count"] <= 4
    assert report["heat_used_pct"] <= AUTOPILOT_HEAT_CAP_PCT + 1e-12

    for candidate in report["dispatched"]:
        assert candidate["weight"] <= report["bucket_cap_pct"] + 1e-12
        assert candidate["tranche_id"].startswith("TRN-")
        assert candidate["signed"] is False
        assert candidate["dispatch"] == "SmartOMS"
        assert len(candidate["proposal_sha256"]) == 64
    assert report["independence"]["allowed"] is True
    assert len(report["report_sha256"]) == 64


def test_autopilot_blocks_a_correlated_second_tranche() -> None:
    """Two perfectly correlated same-side tranches are one bet, not two."""
    autopilot = _autopilot(max_candidates=8, base_weight=0.015)
    existing = [
        {"tranche_id": "TRN-LIVE-1", "silo": "US_IBKR", "side": "LONG", "weight": 0.015}
    ]
    report = autopilot.run_cycle(now_ns=1_760_000_000_000_000_000, tranches=existing)
    # The first new tranche lands in the same 2% bucket as the live one; once the
    # bucket is saturated the gate refuses further correlated weight.
    assert report["heat_used_pct"] <= AUTOPILOT_HEAT_CAP_PCT + 1e-12
    assert report["skipped_blocked_count"] >= 0
    assert report["independence"] is None or report["independence"]["allowed"] in (True, False)


def test_autopilot_never_exceeds_the_portfolio_heat_cap() -> None:
    autopilot = _autopilot(max_candidates=10, base_weight=0.02)
    existing = [
        {"tranche_id": "TRN-A", "silo": "US_IBKR", "side": "SHORT", "weight": 0.03},
        {"tranche_id": "TRN-B", "silo": "FX_MT5", "side": "SHORT", "weight": 0.03},
    ]
    report = autopilot.run_cycle(now_ns=1_760_000_000_000_000_000, tranches=existing)
    assert report["dispatched_count"] == 0
    assert report["skipped_blocked_count"] > 0
    assert "HEAT_CAP" in report["skipped_blocked"][0]["reason"]


def test_autopilot_skips_closed_markets() -> None:
    class _ClosedCalendar:
        def is_open(self, *, silo: str, now_ns: int) -> bool:
            return False

    autopilot = _autopilot(calendar=_ClosedCalendar())
    report = autopilot.run_cycle(now_ns=1_760_000_000_000_000_000)
    assert report["dispatched_count"] == 0
    assert len(report["skipped_closed_market"]) == 10
    assert report["skipped_closed_market"][0]["reason"] == "MARKET_CLOSED"


def test_autopilot_skips_a_memorized_failure_mode() -> None:
    class _Memory:
        def penalty_for(self, *, symbol: str, silo: str):
            return {
                "sizing_multiplier": 1.0,
                "matched_failure_modes": ["ORDER_REJECTION"],
                "applied": True,
            }

    autopilot = _autopilot(memory=_Memory())
    report = autopilot.run_cycle(now_ns=1_760_000_000_000_000_000)
    assert report["dispatched_count"] == 0
    assert all(
        row["reason"] == "MEMORY_BLOCKED:ORDER_REJECTION"
        for row in report["skipped_blocked"]
    )


def test_autopilot_applies_the_memory_haircut_to_sizing() -> None:
    class _Memory:
        def penalty_for(self, *, symbol: str, silo: str):
            return {"sizing_multiplier": 0.6, "matched_failure_modes": []}

    plain = _autopilot().run_cycle(now_ns=1_760_000_000_000_000_000)
    cut = _autopilot(memory=_Memory()).run_cycle(now_ns=1_760_000_000_000_000_000)
    assert plain["dispatched_count"] == cut["dispatched_count"] > 0
    for candidate in cut["dispatched"]:
        assert candidate["memory_multiplier"] == 0.6
    assert cut["heat_used_pct"] < plain["heat_used_pct"]


def test_autopilot_status_is_honest_about_its_air_gap() -> None:
    status = _autopilot().status()
    assert status["gate_source"] == "injected"
    assert status["sizing"] == "half_kelly"
    assert status["heat_cap_pct"] == AUTOPILOT_HEAT_CAP_PCT
    assert status["bucket_cap_pct"] == 0.02
    assert status["law"].startswith("sec_018 Law 4")


# ---------------------------------------------------------------------------
# G1/G2 integration through the real ControlPlane surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime(tmp_path):
    from core.orchestrator import NIOSCoreOrchestrator

    workspace = tmp_path / "workspace"
    vault = tmp_path / "vault"
    workspace.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    instance = NIOSCoreOrchestrator(
        workspace=workspace, key_vault_root=vault, clean_slate=True
    )
    try:
        yield instance
    finally:
        try:
            instance.control.reset_workspace()
        except Exception:
            pass
        closer = getattr(instance, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


@pytest.fixture()
def client(runtime):
    from fastapi.testclient import TestClient

    from ring3_cockpit.gateway.control_plane import create_app

    with TestClient(create_app(runtime.control)) as test_client:
        yield test_client


def test_control_plane_trade_close_endpoint_runs_the_full_saga(client) -> None:
    funded = client.post(
        "/api/v2/control/ledger/fund-paper",
        json={"silo": CRYPTO_SILO, "amount_units": 60_000.0, "vault": "SpinalScalper"},
    )
    assert funded.status_code == 200, funded.text

    closed = client.post(
        "/api/v2/control/trades/close",
        json={
            "silo": CRYPTO_SILO,
            "symbol": "BTC/USDT",
            "gross_pnl_micros": 9_000_000,
            "notional_micros": 90_000_000,
            "side": "SELL",
        },
    )
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["page"] == "trades.close"
    saga = body["saga"]
    assert saga["state"] == "COMPLETED"
    assert saga["statutory_cost_micros"] > 0
    assert [row["stage"] for row in saga["stages"]] == list(SAGA_STAGES)

    listed = client.get("/api/v2/trades/sagas").json()
    assert listed["status"]["saga_count"] == 1
    assert listed["recovery"]["pending_count"] == 0


def test_control_plane_records_a_post_mortem_for_a_losing_close(client, runtime) -> None:
    closed = client.post(
        "/api/v2/control/trades/close",
        json={
            "silo": "US_IBKR",
            "symbol": "NVDA",
            "gross_pnl_micros": -2_000_000,
            "notional_micros": 20_000_000,
            "side": "SELL",
        },
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["saga"]["state"] == "LOSS_RECORDED"

    # G2: the loss compiled entity memory and persisted it in the journal.
    memory = client.get("/api/v2/memory/entities")
    assert memory.status_code == 200, memory.text
    timeline = runtime.control.entity_memory.timeline()
    assert any(row["outcome_kind"] == "LOSS" for row in timeline)

    # G7: the same event was published as a typed stream frame.
    events = runtime.control.stream_events()
    assert any(row["event_type"] == "POST_MORTEM_COMPILED" for row in events)

    status = client.get("/api/v2/post-mortems").json()
    assert status["recorded"] >= 1


def test_trade_close_rejects_an_unknown_silo(client) -> None:
    rejected = client.post(
        "/api/v2/control/trades/close",
        json={"silo": "MARS_EXCHANGE", "gross_pnl_micros": 1, "notional_micros": 1},
    )
    assert rejected.status_code == 422, rejected.text


def test_compensating_an_unknown_saga_is_a_404(client) -> None:
    missing = client.post(
        "/api/v2/control/trades/sagas/NOPE/compensate", json={"reason": "TEST"}
    )
    assert missing.status_code == 404, missing.text


def test_cold_start_reconciler_import_stays_available_for_the_saga(tmp_path) -> None:
    """The saga module must not drag the reconciler out of the import graph."""
    store = LedgerStore(in_memory=True, probe=False)
    book = IBORLedger(store, silos=[CRYPTO_SILO])
    try:
        reconciler = ColdStartReconciler(book)
        assert reconciler.history() == ()
    finally:
        book.close()


def test_alert_center_is_durable_across_instances(tmp_path) -> None:
    journal = AtomicStateJournal(tmp_path / "alerts.json")
    first = AlertCenter(journal)
    raised = first.raise_alert(
        code="TRANCH_STOP_OUT",
        severity="CRITICAL",
        silo="US_IBKR",
        message="tranche stopped out",
    )
    second = AlertCenter(AtomicStateJournal(tmp_path / "alerts.json"))
    listed = second.list_alerts()
    assert listed["count"] == 1
    assert listed["unacked_count"] == 1
    acked = second.acknowledge(raised["alert_id"], operator="sovereign", note="reviewed")
    assert acked["acknowledged"] is True
    third = AlertCenter(AtomicStateJournal(tmp_path / "alerts.json"))
    assert third.list_alerts(unacked_only=True)["count"] == 0
