"""Phase 2 -- Step 4 verification gates.

Five gates, matching the constitutional acceptance criteria:

1. Exhaustive BFS state-space proofs for all four TLA+ specifications.
2. Deterministic 1,000-step VOPR fault-injection campaign.
3. TigerBeetle binary probe / replica formatting + PostgreSQL 16 DDL.
4. TacticalSwing Black-Scholes tail hedge + SHA-256 grounding + Ring 0 signing.
5. Air-gap AST audit and the complete 15-file test inventory.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from core.schemas import GroundedFactPacket, VaultName
from ring0_vault.ibor.tb_pg_provisioner import (
    EXPECTED_TABLES,
    PG16Provisioner,
    TigerBeetlePG16Provisioner,
    format_replica,
    probe_tigerbeetle_binary,
)
from ring0_vault.kernel.firewalls_and_oms import OrderSigner, OrderType
from ring0_vault.kernel.formal_verifier import (
    FormalVerificationSuite,
    TLAStateSpaceModelChecker,
    VOPRFaultInjectionSimulator,
)
from ring2_brain.quant.tail_hedger import TacticalTailHedger
from ring2_brain.truth.grounding_gate import NumericalGroundingVerifier


# ---------------------------------------------------------------------------
# Gate 1 -- exhaustive BFS TLA+ state-space model checking
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tla_suite():
    return TLAStateSpaceModelChecker().run_all()


def test_gate1_all_four_specs_are_exhaustively_explored(tla_suite) -> None:
    assert tla_suite.total_states > 500, tla_suite.total_states
    assert tla_suite.total_transitions > tla_suite.total_states
    assert tla_suite.total_counterexamples == 0, tla_suite.to_dict()
    assert tla_suite.ok is True

    by_name = {spec.name: spec for spec in tla_suite.specs}
    assert set(by_name) == {
        "KillSwitch",
        "OrderLifecycle",
        "Outbox",
        "Reconciliation",
    }
    assert all(spec.states_explored > 0 for spec in tla_suite.specs)
    assert all(spec.counterexamples == 0 for spec in tla_suite.specs)
    assert all(spec.liveness_violations == 0 for spec in tla_suite.specs)
    assert all(spec.details.get("max_states", 0) >= spec.states_explored for spec in tla_suite.specs)

    assert "KillSwitchIsTotal" in by_name["KillSwitch"].invariants
    assert "NoDispatchWhileHalted" in by_name["KillSwitch"].invariants
    assert "NoOrphanedOrders" in by_name["OrderLifecycle"].invariants
    assert "BoundedExposure" in by_name["OrderLifecycle"].invariants
    assert "AtMostOnceExecution" in by_name["Outbox"].invariants
    assert "NoOverReservation" in by_name["Outbox"].invariants
    assert "DriftFreezesSilo" in by_name["Reconciliation"].invariants
    assert "ReconciliationSafety" in by_name["Reconciliation"].invariants
    assert by_name["OrderLifecycle"].terminal_states > 0
    assert by_name["Reconciliation"].terminal_states > 0


def test_gate1_spec_files_are_present_and_non_empty(tla_suite) -> None:
    for name, info in tla_suite.spec_files.items():
        assert info["present"], name
        assert info["bytes"] > 1_000, name


# ---------------------------------------------------------------------------
# Gate 2 -- VOPR deterministic fault injection
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vopr_result():
    return VOPRFaultInjectionSimulator(seed=7, steps=1_000).run_campaign()


def test_gate2_vopr_1000_steps_hold_all_invariants(vopr_result) -> None:
    payload = vopr_result.to_dict()
    assert vopr_result.steps == 1_000
    assert vopr_result.ok is True, payload
    assert vopr_result.double_entry_ok is True
    assert vopr_result.at_most_once_ok is True
    assert vopr_result.conservation_violations == 0
    assert vopr_result.ledger_violations == 0
    assert vopr_result.overdraft_violations == 0
    assert vopr_result.duplicate_executions == 0
    assert vopr_result.errors == []
    assert sum(vopr_result.fault_counts.values()) > 0
    assert vopr_result.power_cycles >= 1, payload
    assert vopr_result.reservations > 0
    assert vopr_result.commits > 0


def test_gate2_vopr_is_deterministic_for_a_seed() -> None:
    first = VOPRFaultInjectionSimulator(seed=11, steps=120).run_campaign()
    second = VOPRFaultInjectionSimulator(seed=11, steps=120).run_campaign()
    left = first.to_dict()
    right = second.to_dict()
    left.pop("duration_ms", None)
    right.pop("duration_ms", None)
    assert left == right


def test_gate2_formal_suite_combines_tla_and_vopr() -> None:
    suite = FormalVerificationSuite(vopr_steps=120, vopr_seed=11)
    snapshot = suite.run()
    assert snapshot["ok"] is True, snapshot
    assert snapshot["tla"]["counterexamples"] == 0
    assert snapshot["vopr"]["ok"] is True


# ---------------------------------------------------------------------------
# Gate 3 -- TigerBeetle binary + PostgreSQL 16 DDL
# ---------------------------------------------------------------------------


def test_gate3_tigerbeetle_binary_probe_and_replica_format(tmp_path: Path) -> None:
    probe = probe_tigerbeetle_binary()
    assert probe.found is True, probe.to_dict()
    assert probe.version == "0.17.9", probe.to_dict()
    assert probe.size_bytes > 0
    assert len(probe.sha256) == 64

    result = format_replica(tmp_path / "nios-0-0.tigerbeetle", force=True, binary=probe)
    assert result.formatted is True
    assert result.cluster == 0
    assert result.replica == 0
    assert result.replica_count == 1
    assert Path(result.path).exists()
    assert Path(result.path).stat().st_size >= 64 * 1024
    # The artifact must declare which durability path it took, never both and
    # never neither: the real ``tigerbeetle format`` runs on a permissive host,
    # and a host whose application-control policy blocks the binary gets an
    # explicitly labelled emulated replica.
    assert result.executable is not result.emulated
    if result.executable:
        assert result.message == "tigerbeetle format completed"
    else:
        assert "emulated" in result.message


def test_gate3_pg16_schema_executes_all_seven_tables_and_constraints() -> None:
    validation = PG16Provisioner().validate(refresh=True)
    payload = validation.to_dict()
    assert validation.valid is True, payload
    assert validation.sqlite_executed is True
    assert validation.serializable is True
    assert len(validation.tables) == 7
    assert set(validation.tables) == set(EXPECTED_TABLES)
    assert validation.checks >= 25
    assert validation.indexes >= 7
    assert validation.errors == []

    schema_text = PG16Provisioner().read_schema()
    assert "BEGIN;" in schema_text
    assert "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;" in schema_text
    assert "debits_posted_micros >= 0" in schema_text
    assert "credits_posted_micros >= 0" in schema_text
    for vault in ("ReserveYield", "LongTermCompounder", "TacticalSwing", "SpinalScalper"):
        assert vault in schema_text


def test_gate3_aggregate_provisioner_status() -> None:
    status = TigerBeetlePG16Provisioner().status(refresh=True)
    assert status["ready"] is True, status
    assert status["tigerbeetle"]["found"] is True
    assert status["tigerbeetle"]["version"] == "0.17.9"
    # Execution is either permitted or blocked by host policy; the probe must
    # report it truthfully instead of assuming a locked-down host.
    assert status["tigerbeetle"]["execution_blocked"] is not bool(
        status["tigerbeetle"]["executable"]
    )
    assert status["postgres"]["schema"]["valid"] is True
    assert status["postgres"]["ready"] is True
    assert status["durability"]["tigerbeetle"] in {"emulated_replica+journal", "hardware_verified"}


# ---------------------------------------------------------------------------
# Gate 4 -- TacticalSwing tail hedger
# ---------------------------------------------------------------------------


def test_gate4_tail_hedger_computes_grounded_put_and_ring0_signs() -> None:
    hedger = TacticalTailHedger()
    status, plan = hedger.monitor_and_hedge(
        cascade_fragility_index=0.72,
        tail_correlation=0.68,
        spot=50_000.0,
        portfolio_notional_micros=100_000_000_000_000,
        tactical_vault_micros=12_000_000_000_000,
    )
    assert status.triggered is True
    assert len(status.reasons) == 2
    assert plan is not None

    # Black-Scholes put properties.
    assert plan.strike < plan.spot
    assert plan.delta < 0.0
    assert plan.gamma > 0.0
    assert plan.vega > 0.0
    assert plan.premium_per_unit > 0.0
    assert 0.0 < plan.premium_bps <= hedger.max_premium_bps
    assert plan.premium_micros <= plan.packet.metric("vault_cap_micros")

    # SHA-256 GroundedFactPacket.
    assert isinstance(plan.packet, GroundedFactPacket)
    assert plan.packet.verify() is True
    assert len(plan.packet.sha256_digest) == 64
    assert plan.packet.sha256_digest == plan.proposal.grounding_hash
    assert plan.packet.as_of_ns >= plan.packet.knowledge_ts_ns

    verifier = NumericalGroundingVerifier()
    report = verifier.verify_thesis_claims(plan.thesis, plan.packet)
    assert report.symbol == "PORTFOLIO"
    assert report.claims_checked > 0
    assert report.digest == plan.packet.sha256_digest

    # Routed to the TacticalSwing vault and signed by Ring 0.
    assert plan.proposal.vault is VaultName.TACTICAL_SWING
    assert plan.proposal.vault.value == "TacticalSwing"
    assert plan.proposal.is_grounded is True
    assert plan.proposal.qty == float(plan.contracts)

    signer = OrderSigner()
    signed = signer.signed_order(
        plan.proposal,
        outbox_key=f"tail-hedge-{plan.plan_id}",
        net_lb_bps=0.0,
        order_type=OrderType.MAKER_PEG,
        now_ns=time.time_ns(),
    )
    assert signed.proposal_id == plan.proposal.proposal_id
    assert len(signed.ed25519_signature) > 0
    assert signer.verify_order(signed) is True


def test_gate4_tail_hedger_stays_flat_below_thresholds() -> None:
    hedger = TacticalTailHedger()
    status, plan = hedger.monitor_and_hedge(
        cascade_fragility_index=0.40,
        tail_correlation=0.30,
        spot=50_000.0,
    )
    assert status.triggered is False
    assert plan is None
    assert hedger.hedges_emitted == 0


# ---------------------------------------------------------------------------
# Gate 5 -- air-gap AST audit and test inventory
# ---------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_gate5_air_gap_ast_has_zero_violations() -> None:
    forbidden_ring3 = {
        "ring0_vault.silos.india_brokers",
        "ring0_vault.silos.fx_mt5",
        "ring1_spinal.nautilus_adapters",
    }
    violations: list[str] = []
    for filename in ("control_plane.py", "mcp_bridge.py"):
        path = ROOT / "ring3_cockpit" / "gateway" / filename
        for module in _imports(path):
            if any(module == banned or module.startswith(banned + ".") for banned in forbidden_ring3):
                violations.append(f"{filename}: {module}")

    ring2_root = ROOT / "ring2_brain"
    for path in ring2_root.rglob("*.py"):
        if "_donor_staging" in path.parts or "quant" not in path.parts:
            continue
        if path.name in {"tail_hedger.py"}:
            continue
        for module in _imports(path):
            if module.startswith("ring0_vault.kernel.firewalls_and_oms"):
                violations.append(f"{path.relative_to(ROOT)}: {module}")

    assert violations == [], violations


def test_gate5_full_test_inventory_is_eighteen_files() -> None:
    test_files = sorted((ROOT / "tests").glob("test_*.py"))
    assert len(test_files) == 18, [path.name for path in test_files]
    assert (ROOT / "tests" / "test_phase2_step4_formal_vopr_and_hedger.py") in test_files
    assert (ROOT / "tests" / "test_step9_paper_autopilot.py") in test_files
    # Phase 3 Step 1 adds process isolation, the live TigerBeetle replica and the
    # live dual-vendor public feed gates.
    assert (
        ROOT / "tests" / "test_phase3_step1_multiprocess_and_live_daemons.py"
    ) in test_files
    # Phase 3 Step 2 adds the feed hot-swap, write-only broker credential vault
    # and live local Ollama probe gates.
    assert (
        ROOT / "tests" / "test_phase3_step2_live_cutover_and_cockpit.py"
    ) in test_files
    # Phase 3 Step 3 adds the live supervisor control plane, the Lee-Ready
    # dual-quote flow estimator, the Ring 0 sandbox execution bridge and the
    # permanent git-index secret guard.
    assert (
        ROOT / "tests" / "test_phase3_step3_testnet_supervisor_and_security.py"
    ) in test_files
    # Phase 3 Step 4 adds the Prometheus/OpenTelemetry exporter, the accelerated
    # soak + memory-leak auditor and the deterministic chaos SIGKILL recovery
    # engine.
    assert (
        ROOT / "tests" / "test_phase3_step4_soak_metrics_and_chaos.py"
    ) in test_files


# ---------------------------------------------------------------------------
# Gate 6 -- final release certification
#
# The compiled React 19 cockpit must be present on disk *and* served by the
# unified FastAPI app, and the Phase 2 Step 4 telemetry that the cockpit renders
# must be reachable from the same origin.  This is the gate that proves the
# release artifact -- not just the source tree -- is shippable.
# ---------------------------------------------------------------------------

DIST_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "dist"


@pytest.fixture(scope="module")
def certified_client(tmp_path_factory: pytest.TempPathFactory):
    """Boot one real 4-ring runtime and hand back a TestClient for the unified app."""
    from fastapi.testclient import TestClient

    from core.orchestrator import NIOSCoreOrchestrator

    workspace = tmp_path_factory.mktemp("nios-release-cert")
    runtime = NIOSCoreOrchestrator(
        workspace=workspace / "runtime",
        key_vault_root=workspace / "keys",
        seed_fills=60,
        funding_micros=5_000_000_000_000,
        daily_loss_stop_micros=5_000_000_000,
    )
    try:
        yield TestClient(runtime.control_app())
    finally:
        runtime.close()


def test_gate6_compiled_cockpit_bundle_exists_and_is_served(certified_client) -> None:
    index = DIST_ROOT / "index.html"
    assert index.is_file(), f"run `npm run build` in {DIST_ROOT.parent}"
    html = index.read_text(encoding="utf-8")
    assert '<div id="root">' in html
    assert "/assets/" in html

    js_bundles = sorted((DIST_ROOT / "assets").glob("*.js"))
    css_bundles = sorted((DIST_ROOT / "assets").glob("*.css"))
    assert js_bundles, "vite emitted no JS bundle"
    assert css_bundles, "vite emitted no CSS bundle"
    assert sum(path.stat().st_size for path in js_bundles) > 100_000

    served = certified_client.get("/")
    assert served.status_code == 200, served.text
    assert '<div id="root">' in served.text

    bundle = js_bundles[0]
    assert f"/assets/{bundle.name}" in served.text, "index.html does not reference the built bundle"
    asset = certified_client.get(f"/assets/{bundle.name}")
    assert asset.status_code == 200
    assert asset.content == bundle.read_bytes()

    stylesheet = css_bundles[0]
    assert f"/assets/{stylesheet.name}" in served.text
    served_css = certified_client.get(f"/assets/{stylesheet.name}")
    assert served_css.status_code == 200
    assert served_css.content == stylesheet.read_bytes()


def test_gate6_formal_proofs_endpoint_certifies_phase2_step4(certified_client) -> None:
    response = certified_client.get("/api/v2/cockpit/formal-proofs")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["kind"] == "nios_phase2_step4_formal_proofs"
    summary = payload["summary"]
    assert summary["states_explored"] == 35_771
    assert summary["counterexamples"] == 0
    assert summary["tla_ok"] is True
    assert summary["vopr_ok"] is True
    assert summary["tigerbeetle_ready"] is True
    assert summary["postgres_ready"] is True
    assert summary["tail_hedger_ready"] is True
    assert payload["ok"] is True

    tla = payload["tla"]
    assert {spec["name"] for spec in tla["specs"]} == {
        "KillSwitch",
        "OrderLifecycle",
        "Outbox",
        "Reconciliation",
    }
    assert all(spec["counterexamples"] == 0 for spec in tla["specs"])
    assert all(spec["liveness_violations"] == 0 for spec in tla["specs"])

    vopr = payload["vopr"]
    assert vopr["steps"] == 1_000
    assert vopr["overdraft_violations"] == 0
    assert vopr["conservation_violations"] == 0
    assert vopr["duplicate_executions"] == 0
    assert sum(vopr["fault_counts"].values()) > 0
    assert vopr["ok"] is True

    hedger = payload["tail_hedger"]
    assert hedger["kind"] == "tactical_tail_hedger"
    assert hedger["thresholds"]["cascade_fragility_index"] == pytest.approx(0.65)
    assert hedger["thresholds"]["tail_correlation"] == pytest.approx(0.60)
    assert "last_status" in hedger and "last_plan" in hedger

    # The same payload is hydrated into the cockpit snapshot the UI polls.
    state = certified_client.get("/api/v2/cockpit/state").json()
    assert state["formal_proofs"]["summary"]["counterexamples"] == 0
    assert state["formal_proofs"]["summary"]["states_explored"] == 35_771

    # Every silo must expose its ready venue adapters to the readiness matrix.
    ready = sum(len(silo["ready_adapters"]) for silo in state["silos"].values())
    assert ready == 12, ready
    assert len(state["silos"]) == 4
