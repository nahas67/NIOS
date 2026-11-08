"""STEP 4 verification -- Ring 2 cognition, contagion, settlement and committee.

Gates
-----
1. Delayed settlement + Darwinian trust multiplier ``[0.8, 1.5]``.
2. MiroFish 5-archetype 3-tick cascade ``< 1.8 ms`` p99 + fragility override.
3. System 1 auto-failover under a >250 ms endpoint and ``< 78 ms`` p99 refresh.
4. System 2 staged committee grounding pass + hallucination abort.
5. Four-ring air-gap AST sentinel and Step 2/3 regression contracts.
"""

from __future__ import annotations

import ast
import math
import time
from pathlib import Path

import pytest

from core.feature_store import FeatureStore, quantile_linear
from core.nats_bus import LocalJetStreamBus, STREAM_RING2_THESES
from core.schemas import (
    FRAGILITY_HALT_THRESHOLD,
    LEASE_TTL_NS,
    ContagionFeatures,
    ExecutedFlowTick,
    GroundedFactPacket,
    LeaseMode,
    System1Lease,
    TemporalIntegrityError,
    UngroundedNumericalClaim,
    VaultName,
)
from core.shm_ipc import GlobalKillLatch, LeaseTableSHM, unlink_all
from ring2_brain.cognition.committee_graph import (
    COMPOUNDER_CAP_PCT,
    TACTICAL_CAP_PCT,
    System2InvestmentCommittee,
    UngroundedNumericalClaimError,
)
from ring2_brain.cognition.system1_instinct import (
    HARD_TIMEOUT_MS,
    P99_SLA_MS,
    InstinctContext,
    MultiLLMSwitchboard,
    ProviderResponse,
    System1InstinctController,
)
from ring2_brain.mirofish.contagion_engine import (
    ARCHETYPES,
    ARCHETYPE_WEIGHTS,
    MiroFishContagionEngine,
    ShockVector,
)
from ring2_brain.quant.scanner import FactorPanel, VectorizedFactorScanner
from ring2_brain.truth.settlement_worker import (
    TRUST_MAX,
    TRUST_MIN,
    DelayedSettlementWorker,
    donor_settlement_status,
)

ROOT = Path(__file__).resolve().parents[1]


def _percentile(values: list[float], q: float) -> float:
    return quantile_linear(values, q)


def _high_fragility_shock() -> ShockVector:
    return ShockVector(
        price_shock_bps=120.0,
        realized_vol_ratio=4.0,
        spread_bps=25.0,
        dealer_gamma_flip_proximity=0.9,
        tags=("pod_stops", "dealer_gamma_flip", "cta_deleveraging"),
    )


# ---------------------------------------------------------------------------
# Gate 1 -- delayed settlement and Darwinian memory
# ---------------------------------------------------------------------------


def test_gate1_delayed_settlement_updates_q98_and_trust(tmp_path: Path) -> None:
    store = FeatureStore(tmp_path / "lake")
    worker = DelayedSettlementWorker(store)
    base_ns = time.time_ns()

    expected: dict[str, list[float]] = {"agent_winner": [], "agent_loser": []}
    realized_by_prediction: dict[str, float] = {}

    for index in range(200):
        winning = index % 2 == 0
        strategy = "agent_winner" if winning else "agent_loser"
        predicted = 10.0 + float(index % 7)
        realized = predicted + 0.1 if winning else -predicted
        prediction = worker.record_prediction(
            strategy_id=strategy,
            symbol=f"SYM{index:03d}",
            predicted_net_bps=predicted,
            horizon_ns=5,
            created_at_ns=base_ns,
            grounding_hash="a" * 64,
        )
        realized_by_prediction[prediction.prediction_id] = realized
        expected[strategy].append(abs(predicted - realized))

    # No prediction may settle before its own horizon elapses.
    assert worker.settle_due(base_ns + 4, realized_by_prediction) == []
    assert len(worker.pending_predictions) == 200

    outcomes = worker.settle_due(base_ns + 10, realized_by_prediction)
    assert len(outcomes) == 200
    assert len(worker.pending_predictions) == 0
    assert store.table_counts()["conformal_residuals"] == 200

    for strategy, residuals in expected.items():
        window = store.get_q98_residual(strategy, worker.last_as_of_ns)
        assert window.count == 100
        assert math.isfinite(window.q98_bps)
        assert window.q98_bps == pytest.approx(
            quantile_linear(residuals, 0.98), rel=1e-12, abs=1e-12
        )
        multiplier = worker.trust_multiplier(strategy)
        assert TRUST_MIN <= multiplier <= TRUST_MAX

    # Perfect directional accuracy pins the winner at the ceiling; a strategy
    # that is on the wrong side every single time pins the loser at the floor.
    assert worker.trust_multiplier("agent_winner") == pytest.approx(TRUST_MAX)
    assert worker.trust_multiplier("agent_loser") == pytest.approx(TRUST_MIN)

    snapshots = worker.trust_snapshots("agent_winner")
    assert snapshots
    assert snapshots[-1].multiplier == pytest.approx(TRUST_MAX)
    assert snapshots[-1].settled_count == 100

    # Every graded residual is within the strategy's own empirical window.
    residuals = store.get_residuals("agent_loser", worker.last_as_of_ns, window=200)
    assert len(residuals) == 100
    assert all(
        row["residual_bps"] == pytest.approx(2.0 * abs(row["realized_net_bps"]))
        for row in residuals
    )

    donor = donor_settlement_status()
    assert donor["harvested"] is True
    assert "importable" in donor
    store.close()


# ---------------------------------------------------------------------------
# Gate 2 -- MiroFish 5-archetype 3-tick cascade
# ---------------------------------------------------------------------------


def test_gate2_five_archetypes_and_three_tick_sla() -> None:
    assert [name for name, _ in ARCHETYPES] == [
        "Retail_0DTE",
        "Options_Dealer_Gamma",
        "Vol_Target_CTA",
        "Pod_Shop_Stops",
        "Value_Funds",
    ]
    assert sum(ARCHETYPE_WEIGHTS) == pytest.approx(1.0)
    assert ARCHETYPE_WEIGHTS[0] == pytest.approx(0.15)
    assert ARCHETYPE_WEIGHTS[1] == pytest.approx(0.25)
    assert ARCHETYPE_WEIGHTS[2] == pytest.approx(0.25)
    assert ARCHETYPE_WEIGHTS[3] + ARCHETYPE_WEIGHTS[4] == pytest.approx(0.35)

    engine = MiroFishContagionEngine(warmup=True)
    shocks = [
        ShockVector(5.0, 1.05, 3.0, 0.05),
        ShockVector(-40.0, 1.8, 8.0, 0.35, tags=("cta_deleveraging",)),
        _high_fragility_shock(),
    ]
    latencies: list[float] = []
    for index in range(1_000):
        result = engine.run(
            shocks[index % len(shocks)],
            f"SYM{index % 17:03d}",
            now_ns=1_800_000_000_000_000_000 + index,
            persist=False,
        )
        assert -1.0 <= result.features.crowd_asymmetry_score <= 1.0
        assert 0.0 <= result.features.cascade_fragility_index <= 1.0
        assert -1.0 <= result.features.narrative_exhaustion_divergence <= 1.0
        latencies.append(result.latency_ms)

    p99 = _percentile(latencies, 0.99)
    assert p99 < 1.8, f"3-tick cascade p99 {p99:.4f} ms exceeds 1.8 ms"
    assert engine.run(_high_fragility_shock(), "SYM999", persist=False).features.cascade_fragility_index >= FRAGILITY_HALT_THRESHOLD
    assert engine.run(ShockVector(5.0, 1.05, 3.0, 0.05), "SYM999", persist=False).features.cascade_fragility_index < 0.5


def test_gate2_persists_features_and_disarms_long_leases(tmp_path: Path) -> None:
    store = FeatureStore(tmp_path / "lake")
    latch = GlobalKillLatch(reset=True)
    table = LeaseTableSHM(reset=True, kill_latch=latch)
    try:
        now = time.monotonic_ns()
        table.write_lease(
            System1Lease(
                symbol="ACME",
                mode=LeaseMode.LONG_ONLY,
                target_bps=12.0,
                stop_bps=5.0,
                expires_at_ns=now + LEASE_TTL_NS,
            ),
            now_ns=now,
        )
        table.write_lease(
            System1Lease(
                symbol="BETA",
                mode=LeaseMode.SHORT_ONLY,
                target_bps=10.0,
                stop_bps=4.0,
                expires_at_ns=now + LEASE_TTL_NS,
            ),
            now_ns=now,
        )

        engine = MiroFishContagionEngine(feature_store=store, lease_table=table)
        result = engine.run(
            _high_fragility_shock(), "ACME", now_ns=time.time_ns(), lease_now_ns=now
        )

        assert result.features.requires_ring1_halt is True
        assert result.fragility_override is True
        assert result.revoked_long_leases >= 1
        assert table.read_lease("ACME", now_ns=now).mode is LeaseMode.DISARMED
        # The override is surgical: shorts stay armed.
        assert table.read_lease("BETA", now_ns=now).mode is LeaseMode.SHORT_ONLY

        rows = store.query_contagion("US", "ACME", int(result.features.as_of_ns))
        assert len(rows) == 1
        assert rows[0]["requires_ring1_halt"] is True
        assert rows[0]["cascade_fragility_index"] >= FRAGILITY_HALT_THRESHOLD

        # A benign tape must not disarm anything, and the next high-fragility
        # episode must re-arm the override.
        table.write_lease(
            System1Lease(
                symbol="ACME",
                mode=LeaseMode.LONG_ONLY,
                target_bps=8.0,
                stop_bps=3.0,
                expires_at_ns=now + LEASE_TTL_NS,
            ),
            now_ns=now,
        )
        benign = engine.run(
            ShockVector(4.0, 1.0, 2.0, 0.0),
            "ACME",
            now_ns=time.time_ns(),
            lease_now_ns=now,
        )
        assert benign.fragility_override is False
        assert table.read_lease("ACME", now_ns=now).mode is LeaseMode.LONG_ONLY
    finally:
        table.close()
        latch.close()
        unlink_all()
        store.close()


# ---------------------------------------------------------------------------
# Gate 3 -- System 1 auto-failover and lease SLA
# ---------------------------------------------------------------------------


class _SlowProvider:
    """A primary endpoint that blows the 250 ms hard cap."""

    def __call__(self, context: InstinctContext) -> ProviderResponse:
        time.sleep(0.30)
        return ProviderResponse(LeaseMode.LONG_ONLY, 20.0, 8.0, 0.9, "slow")


def test_gate3_auto_demotes_slow_provider_and_meets_lease_sla() -> None:
    assert HARD_TIMEOUT_MS == 250.0
    board = MultiLLMSwitchboard()
    board.install_probe("ollama-qwen2.5-3b", _SlowProvider())
    assert board.provider("ollama-qwen2.5-3b").demoted is False

    latch = GlobalKillLatch(reset=True)
    table = LeaseTableSHM(reset=True, kill_latch=latch)
    try:
        controller = System1InstinctController(board, table, kill_latch=latch)
        context = InstinctContext(
            symbol="AAPL",
            realized_vol_1m=0.010,
            normal_vol_1m=0.010,
            spread_bps=3.0,
            taker_buy_ratio=0.80,
        )
        now = time.monotonic_ns()
        latencies: list[float] = []
        for _ in range(200):
            decision = controller.evaluate(context, now_ns=now)
            latencies.append(decision.latency_ms)

        primary = board.provider("ollama-qwen2.5-3b")
        assert primary.demoted is True
        assert "timeout" in primary.demoted_reason
        assert primary.demoted_reason.startswith("timeout>")

        # Every decision after the demotion is deterministic.
        assert all(decision.used_fallback for decision in controller.decisions)
        assert all(
            decision.provider_name == "deterministic-local"
            for decision in controller.decisions
        )

        p99 = _percentile(latencies, 0.99)
        assert p99 < P99_SLA_MS, f"lease refresh p99 {p99:.3f} ms exceeds 78 ms"
        stats = controller.latency_stats()
        assert stats["p50_ms"] < 48.0

        lease = table.read_lease("AAPL", now_ns=now)
        assert lease.mode is LeaseMode.LONG_ONLY
        assert lease.is_active(now)
        remaining = lease.expires_at_ns - now
        assert 0 < remaining <= LEASE_TTL_NS
        assert lease.strategy_id_hash != 0

        # Fragility override: a LONG_ONLY lease must never be armed.
        features = ContagionFeatures(
            symbol="AAPL",
            as_of_ns=time.time_ns(),
            crowd_asymmetry_score=0.9,
            cascade_fragility_index=0.9,
            narrative_exhaustion_divergence=0.4,
        )
        halted = controller.evaluate(
            InstinctContext(
                symbol="AAPL",
                realized_vol_1m=0.02,
                normal_vol_1m=0.01,
                spread_bps=4.0,
                taker_buy_ratio=0.90,
                contagion=features,
            ),
            now_ns=now,
        )
        assert halted.mode is LeaseMode.DISARMED
        assert halted.target_bps == 0.0
        assert table.read_lease("AAPL", now_ns=now).mode is LeaseMode.DISARMED

        # The 5-second executed-flow frame folds directly into the 1-minute
        # digest the controller evaluates.
        tick = ExecutedFlowTick(
            symbol="AAPL",
            valid_time_ns=time.time_ns(),
            knowledge_ts_ns=time.time_ns(),
            last_price=100.0,
            spread_bps=3.0,
            realized_vol_5s=0.01,
            normal_vol_5s=0.01,
            taker_buy_volume_5s=80.0,
            taker_sell_volume_5s=20.0,
            bid_cancel_to_fill_ratio=1.0,
            ask_cancel_to_fill_ratio=1.0,
        )
        flow_context = InstinctContext.from_executed_flow(tick)
        assert flow_context.taker_buy_ratio == pytest.approx(0.8)
        flow_decision = controller.evaluate(flow_context, now_ns=now)
        assert flow_decision.mode is LeaseMode.LONG_ONLY
    finally:
        table.close()
        latch.close()
        unlink_all()


# ---------------------------------------------------------------------------
# Gate 4 -- System 2 committee and grounding abort
# ---------------------------------------------------------------------------


def test_gate4_committee_grounds_and_publishes(tmp_path: Path) -> None:
    store = FeatureStore(tmp_path / "lake")
    scanner = VectorizedFactorScanner()
    scanner.warmup()
    panel = FactorPanel.generate(n_symbols=500, n_periods=32)
    result = scanner.scan(panel)
    assert len(result.candidates) == 5
    candidate = result.candidates[0]

    engine = MiroFishContagionEngine()
    contagion = engine.run(
        ShockVector(18.0, 1.3, 4.0, 0.2, tags=("value_absorption",)),
        candidate.symbol,
        now_ns=panel.valid_time_ns,
        persist=False,
    ).features

    bus = LocalJetStreamBus()
    try:
        committee = System2InvestmentCommittee(
            feature_store=store, bus=bus, cold_start_q98_bps=2.0
        )
        outcome = committee.run(
            candidate,
            contagion=contagion,
            spread_bps=3.0,
            now_ns=panel.valid_time_ns,
            universe_size=result.universe_size,
        )

        proposal = outcome.proposal
        assert proposal.grounding_hash == outcome.packet.sha256_digest
        assert outcome.packet.verify()
        assert proposal.vault in (
            VaultName.LONG_TERM_COMPOUNDER,
            VaultName.TACTICAL_SWING,
        )
        cap = (
            COMPOUNDER_CAP_PCT
            if proposal.vault is VaultName.LONG_TERM_COMPOUNDER
            else TACTICAL_CAP_PCT
        )
        assert outcome.report.position_pct <= cap + 1e-12
        assert proposal.notional <= committee.nav * cap + 1e-6
        assert proposal.qty > 0.0
        assert proposal.is_grounded

        stage_names = [stage.stage for stage in outcome.report.stages]
        assert stage_names[:4] == [
            "analyst_fundamental",
            "analyst_technical",
            "analyst_sentiment",
            "analyst_macro",
        ]
        assert "debate_bull_r1" in stage_names and "debate_bear_r1" in stage_names
        assert "debate_bull_r2" in stage_names and "debate_bear_r2" in stage_names
        assert "trader" in stage_names
        assert {"risk_aggressive", "risk_neutral", "risk_conservative"} <= set(
            stage_names
        )
        assert all(stage.verified for stage in outcome.report.stages)
        assert outcome.report.verifications
        assert all(report.claims_checked >= 0 for report in outcome.report.verifications)

        assert outcome.published is True
        assert bus.committed_count(STREAM_RING2_THESES) == 1
        event = bus.replay(STREAM_RING2_THESES)[0]
        assert event.subject.startswith(STREAM_RING2_THESES + ".")
        assert event.payload["proposal"]["proposal_id"] == proposal.proposal_id
        assert event.payload["packet_digest"] == outcome.packet.sha256_digest

        # The funnel is enforced: rank 6 of 500 is outside the top 1%.
        from ring2_brain.cognition.committee_graph import CommitteeFunnelViolation

        forged = result.candidates[0]
        forged_rank = type(forged)(
            rank=6,
            symbol=forged.symbol,
            score=forged.score,
            price=forged.price,
            features=forged.features,
            packet=forged.packet,
        )
        with pytest.raises(CommitteeFunnelViolation):
            committee.run(
                forged_rank,
                contagion=contagion,
                universe_size=result.universe_size,
                publish=False,
            )
    finally:
        bus.close()
        store.close()


def test_gate4_hallucinated_number_aborts_publication(tmp_path: Path) -> None:
    store = FeatureStore(tmp_path / "lake")
    scanner = VectorizedFactorScanner()
    scanner.warmup()
    panel = FactorPanel.generate(n_symbols=500, n_periods=32)
    result = scanner.scan(panel)
    candidate = result.candidates[0]
    engine = MiroFishContagionEngine()
    contagion = engine.run(
        ShockVector(10.0, 1.1, 3.0, 0.1),
        candidate.symbol,
        now_ns=panel.valid_time_ns,
        persist=False,
    ).features

    bus = LocalJetStreamBus()

    def inject(stage: str, text: str) -> str:
        if stage == "trader":
            return text + " Fabricated edge is 987654.31 bps."
        return text

    try:
        committee = System2InvestmentCommittee(
            feature_store=store, bus=bus, cold_start_q98_bps=2.0
        )
        with pytest.raises(UngroundedNumericalClaimError) as excinfo:
            committee.run(
                candidate,
                contagion=contagion,
                now_ns=panel.valid_time_ns,
                universe_size=result.universe_size,
                text_mutator=inject,
            )
        assert "UNGROUNDED-NUMERICAL-CLAIM" in str(excinfo.value)
        assert bus.committed_count(STREAM_RING2_THESES) == 0
        assert bus.replay(STREAM_RING2_THESES) == []
    finally:
        bus.close()
        store.close()


# ---------------------------------------------------------------------------
# Gate 5 -- air-gap AST sentinel + Step 2/3 regression
# ---------------------------------------------------------------------------

_FORBIDDEN_ROOTS = frozenset(
    {
        "ring0_vault",
        "nautilus_trader",
        "tigerbeetle",
        "key_vault",
        "ib_insync",
        "ccxt",
        "websocket",
        "websockets",
        "socket",
        "requests",
        "httpx",
        "aiohttp",
        "openalgo",
        "zerodha",
        "dhan",
        "upstox",
        "angel",
        "mt5",
        "MetaTrader5",
    }
)

#: Harvested donor trees are reference material; the air-gap contract applies
#: to first-party Ring 2 runtime modules (the Step 2/3/4 surfaces).
_DONOR_PREFIXES = (
    "ring2_brain/quant/vibe_trading/",
    "ring2_brain/quant/trading_r1/",
    "ring2_brain/truth/vibe_trading/",
    "ring2_brain/truth/trading_r1/",
    "ring2_brain/truth/tradingagents/",
    "ring2_brain/mirofish/services/",
    "ring2_brain/mirofish/models/",
    "ring2_brain/cognition/tradingagents/",
)


def _first_party_ring2_modules() -> list[Path]:
    modules: list[Path] = []
    for path in (ROOT / "ring2_brain").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if any(relative.startswith(prefix) for prefix in _DONOR_PREFIXES):
            continue
        modules.append(path)
    return modules


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
    return found


def test_gate5_ring2_cognition_is_air_gapped() -> None:
    step4 = [
        ROOT / "ring2_brain" / "truth" / "settlement_worker.py",
        ROOT / "ring2_brain" / "mirofish" / "contagion_engine.py",
        ROOT / "ring2_brain" / "cognition" / "system1_instinct.py",
        ROOT / "ring2_brain" / "cognition" / "committee_graph.py",
    ]
    modules = step4 + _first_party_ring2_modules()
    for module in modules:
        assert module.exists(), f"missing {module}"
        for name in _imported_modules(module):
            root = name.split(".")[0]
            assert root not in _FORBIDDEN_ROOTS, (
                f"{module.name} imports forbidden Ring 0 / broker surface {name!r}"
            )
            assert "ring0_vault" not in name, (
                f"{module.name} must not import Ring 0 code ({name!r})"
            )


def test_gate5_step2_and_step3_contracts_still_hold() -> None:
    latch = GlobalKillLatch(reset=True)
    table = LeaseTableSHM(reset=True, kill_latch=latch)
    try:
        now = time.monotonic_ns()
        lease = System1Lease(
            symbol="BTC-USD",
            mode=LeaseMode.LONG_ONLY,
            target_bps=12.0,
            stop_bps=4.0,
            expires_at_ns=now + LEASE_TTL_NS,
        )
        table.write_lease(lease, now_ns=now)
        assert table.read_lease("BTC-USD", now_ns=now).mode is LeaseMode.LONG_ONLY
        latch.halt(now_ns=now)
        assert table.read_lease("BTC-USD", now_ns=now).mode is LeaseMode.DISARMED

        with pytest.raises(TemporalIntegrityError):
            GroundedFactPacket(
                symbol="ACME",
                valid_time_ns=now,
                knowledge_ts_ns=now + 1,
                as_of_ns=now,
                metrics={"close": 1.0},
            )
        with pytest.raises(UngroundedNumericalClaim):
            GroundedFactPacket(
                symbol="ACME",
                valid_time_ns=now,
                knowledge_ts_ns=now,
                as_of_ns=now,
                metrics={"close": 1.0},
            ).require("missing_metric")
    finally:
        table.close()
        latch.close()
        unlink_all()
