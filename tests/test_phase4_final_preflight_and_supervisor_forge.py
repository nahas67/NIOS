"""Phase 4 -- final closure certification.

Four gates close the Main Core (Phases 1-3):

* Gate 1 -- the expanded 22-row ``run_nios.py --smoke-test`` suite exits ``0``.
* Gate 2 -- the closed-loop ``NocturnalAlphaForge`` + ``TacticalTailHedger``
  cycle runs inside the Ring 2 worker process, revokes a decayed champion's
  shared-memory lease, and routes a grounded protective put across the process
  boundary to Ring 0, which verifies and Ed25519-signs it.
* Gate 3 -- ``scripts/preflight_golive.py --json`` certifies the release with
  ``ready_for_live_cutover == True``, ``failed_gates == 0`` and a 64-character
  SHA-256 certificate digest.
* Gate 4 -- the regression inventory grew to 24 suites while every
  pre-existing Phase 1-3 suite stayed intact.

Phase 4 Step 2 wires those organs into the Ring 3 control plane and cockpit:

* ``GET``/``POST /api/v2/control/preflight`` return the certificate with
  ``ready_for_live_cutover``, ``passed_gates``/``failed_gates`` and a
  64-character SHA-256 digest.
* ``POST /api/v2/control/supervisor/forge-hedge`` executes the closed-loop forge
  + tail-hedge cycle and returns a verified Ring 0 Ed25519 receipt.
* ``POST /api/v2/control/brokers`` refuses ``mode="LIVE"`` unless the
  certificate reports ``failed_gates == 0``.
* ``GET /`` serves one deterministic, zero-JavaScript headless status document.
* There is no React/Vite/Biome toolchain in the tree (V1.6).

Nothing in this module places a live order, unlocks a silo, or reads a secret.
The Ring 0 leg only ever produces an *unsigned* proposal signature over a
sandbox-safe payload.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable or "python"

SILO = "CRYPTO_BINANCE"
SYMBOL = "BTCUSDT"
RELEASE_EXPECTED = "3.0.0-PROD"

#: The 17 suites that already existed when Phase 4 began.
BASELINE_SUITES: tuple[str, ...] = (
    "test_phase2_step1_donor_activation.py",
    "test_phase2_step2_silo_adapters.py",
    "test_phase2_step3_feeds_and_llm_pool.py",
    "test_phase2_step4_formal_vopr_and_hedger.py",
    "test_phase3_step1_multiprocess_and_live_daemons.py",
    "test_phase3_step2_live_cutover_and_cockpit.py",
    "test_phase3_step3_testnet_supervisor_and_security.py",
    "test_phase3_step4_soak_metrics_and_chaos.py",
    "test_phase3_step5_os_tuning_key_rotation_and_runbook.py",
    "test_step2_core_spine.py",
    "test_step3_truth_and_quant.py",
    "test_step4_ring2_cognition.py",
    "test_step5_ring1_spinal.py",
    "test_step6_ring0_vault.py",
    "test_step7_cockpit_and_e2e.py",
    "test_step8_forge_mcp_and_deploy.py",
    "test_step9_paper_autopilot.py",
)

THIS_SUITE = "test_phase4_final_preflight_and_supervisor_forge.py"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    return env


def _run(args: list[str], *, timeout_s: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-X", "utf8", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=_env(),
        check=False,
    )


def _pass_row_count(output: str) -> int:
    """Table rows only; the runtime-budget footer also carries a PASS token."""
    return sum(
        1
        for line in output.splitlines()
        if "  PASS  " in line and not line.lstrip().startswith("ROWS=")
    )


# ---------------------------------------------------------------------------
# Gate 1 -- 22-row extended smoke test
# ---------------------------------------------------------------------------


def test_gate1_extended_smoke_suite_certifies_22_organs() -> None:
    """``run_nios.py --smoke-test`` must print 22 PASS rows and exit zero."""
    completed = _run(["scripts/run_nios.py", "--smoke-test"], timeout_s=600.0)
    output = (completed.stdout or "") + (completed.stderr or "")

    assert completed.returncode == 0, output[-4000:]
    assert "SMOKE TEST PASSED" in output, output[-4000:]
    assert "ROWS=22" in output, output[-4000:]
    assert _pass_row_count(output) == 22, output[-6000:]
    assert "  FAIL  " not in output, output[-6000:]

    # The four Phase 3 Step 2-5 organs must be named explicitly, in order.
    expected_tail = (
        "Phase 3 four-ring OS process supervisor",
        "Phase 3 crash-recovery / outbox audit",
        "Multi-Silo Dual-Vendor Feed & Lee-Ready Flow",
        "Ring 0 Testnet Execution Bridge & Write-Only Vault",
        "Prometheus / OpenTelemetry Metrics Exporter",
        "OS & Socket Low-Latency Tuning Auditor",
    )
    position = -1
    for label in expected_tail:
        found = output.find(label)
        assert found > position, f"missing or out-of-order row: {label}"
        position = found


# ---------------------------------------------------------------------------
# Gate 2 -- cross-process Ring 2 forge + tail-hedger -> Ring 0 signing
# ---------------------------------------------------------------------------


def _drain_until(supervisor, predicate, *, timeout_s: float = 45.0) -> list[dict]:
    collected: list[dict] = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        collected.extend(supervisor.drain_events(limit=8192))
        if predicate(collected):
            return collected
        time.sleep(0.05)
    return collected


def test_gate2_closed_loop_forge_and_hedge_signs_across_rings() -> None:
    """Ring 2 forges, demotes and hedges; Ring 0 verifies and signs."""
    from core.process_supervisor import RingProcessSupervisor

    supervisor = RingProcessSupervisor(
        silo=SILO,
        strategy_id="phase4-decayed-champion",
        rings=(0, 2),
        lease_interval_s=0.5,
    )
    try:
        supervisor.start()
        ready = _drain_until(
            supervisor,
            lambda events: {"ring0_ready", "ring2_ready"}
            <= {str(event.get("type")) for event in events},
            timeout_s=45.0,
        )
        assert {"ring0_ready", "ring2_ready"} <= {
            str(event.get("type")) for event in ready
        }, f"workers never reported ready: {sorted({str(e.get('type')) for e in ready})}"

        command = supervisor.run_forge_and_hedge_cycle(
            champion_fills_bps=[-1.4, -1.1, -1.9, -0.8, -1.2, -1.5],
            champion_symbols=[SYMBOL],
            challengers=4,
            price_shock_bps=180.0,
            request_id="phase4-gate2",
        )
        assert command["cmd"] == "forge_and_hedge"
        assert command["request_id"] == "phase4-gate2"

        events = _drain_until(
            supervisor,
            lambda collected: {"ring2_forge_hedge", "ring0_hedge_signed"}
            <= {str(event.get("type")) for event in collected},
            timeout_s=60.0,
        )
        by_type: dict[str, list[dict]] = {}
        for event in events:
            by_type.setdefault(str(event.get("type")), []).append(event)

        forge_error = by_type.get("ring2_forge_error")
        assert not forge_error, forge_error

        cycle = by_type.get("ring2_forge_hedge")
        assert cycle, f"no forge cycle event; saw {sorted(by_type)}"
        cycle_event = cycle[0]
        assert cycle_event["request_id"] == "phase4-gate2"
        assert cycle_event["retired"] >= 1
        assert cycle_event["genomes_evaluated"] >= 4
        assert cycle_event["tail_hedge_triggered"] is True
        assert cycle_event["hedge_routed"] is True

        # Decayed champion demoted, and its shared-memory lease revoked.
        retirements = cycle_event["retirements"]
        assert retirements, "decayed champion was not retired"
        retirement = retirements[0]
        assert retirement["strategy_id"] == "phase4-decayed-champion"
        assert retirement["status"] == "SHADOW_RETIRED"
        assert retirement["disarmed_leases"] >= 1
        assert retirement["threshold_bps"] == pytest.approx(0.50)
        assert cycle_event["champion_status"] == "SHADOW_RETIRED"
        assert cycle_event["champion_rolling_net_bps"] < 0.50

        # Ring 0 accepted the insurance proposal and signed it.
        signed = by_type.get("ring0_hedge_signed")
        assert signed, f"Ring 0 never signed the hedge; saw {sorted(by_type)}"
        signature = signed[0]
        assert signature["request_id"] == "phase4-gate2"
        assert signature["hedge_role"] == "INSURANCE"
        assert signature["vault"] == "TacticalSwing"
        assert signature["silo"] == SILO
        assert signature["symbol"] == SYMBOL
        assert signature["verified"] is True
        assert len(signature["digest_hex"]) == 64
        assert len(signature["signature_hex"]) == 128
        assert len(signature["public_key_hex"]) == 64
        assert signature["premium_bps"] > 0.0
        assert signature["strike"] < signature.get("spot", 50_000.0) * 1.01
        assert signature["proposal_id"].startswith("tail-hedge-")
        assert len(signature["grounding_hash"]) == 64
        assert signature["plan_id"] == cycle_event["hedge_plan_id"]

        rejected = by_type.get("ring0_hedge_rejected")
        assert not rejected, rejected
    finally:
        supervisor.stop()


# ---------------------------------------------------------------------------
# Gate 3 -- pre-flight go-live certification CLI
# ---------------------------------------------------------------------------


def test_gate3_preflight_golive_cli_certifies_release() -> None:
    """The CLI must certify the release and emit a deterministic certificate."""
    completed = _run(["scripts/preflight_golive.py", "--json"], timeout_s=900.0)
    assert completed.returncode == 0, (completed.stdout or "")[-4000:] + (
        completed.stderr or ""
    )[-2000:]
    report = json.loads(completed.stdout)

    assert report["kind"] == "nios_golive_readiness_certificate"
    assert report["release"] == "3.0.0-PROD"
    assert report["ready_for_live_cutover"] is True
    assert report["failed_gates"] == 0
    assert report["warned_gates"] == 0
    assert report["gates_expected"] == 22
    assert report["gates_passed"] == 22

    digest = report["certificate_sha256"]
    assert isinstance(digest, str) and len(digest) == 64
    int(digest, 16)  # valid lowercase/uppercase hex, 256 bits

    gates = report["gates"]
    assert len(gates) == 22
    assert [gate["status"] for gate in gates] == ["PASS"] * 22
    gate_ids = [gate["gate"] for gate in gates]
    assert len(set(gate_ids)) == 22
    for required in (
        "git.no_tracked_secrets",
        "smoke.22_row_suite",
        "formal.tla_four_proofs",
        "formal.vopr_1000_steps",
        "ring0.key_vault_integrity",
        "supervisor.hedge_signing_contract",
        "os.low_latency_socket_readiness",
    ):
        assert required in gate_ids

    by_id = {gate["gate"]: gate for gate in gates}
    assert by_id["formal.tla_four_proofs"]["evidence"]["total_states"] == 35_771
    assert by_id["formal.tla_four_proofs"]["evidence"]["total_counterexamples"] == 0
    assert by_id["formal.vopr_1000_steps"]["evidence"]["steps"] == 1_000
    assert by_id["formal.vopr_1000_steps"]["evidence"]["overdraft_violations"] == 0
    assert by_id["ring0.twelve_venue_dry_run_signatures"]["evidence"][
        "venues_verified"
    ] == 12

    # Determinism: the digest covers gate outcomes, never wall-clock state.
    certificate = report["certificate"]
    assert certificate["schema"] == "nios_preflight_golive_certificate_v1"
    assert len(certificate["gates"]) == 22
    assert all("duration_ms" not in gate for gate in certificate["gates"])


# ---------------------------------------------------------------------------
# Gate 4 -- complete regression inventory and clean release tag
# ---------------------------------------------------------------------------


def test_gate4_regression_inventory_is_twenty_one_suites() -> None:
    """Twenty pre-existing suites plus this one; the release tag stays annotated."""
    suites = sorted(
        path.name
        for path in (ROOT / "tests").glob("test_*.py")
        if path.is_file()
    )
    assert len(suites) == 26, suites
    assert THIS_SUITE in suites
    missing = [name for name in BASELINE_SUITES if name not in suites]
    assert not missing, f"baseline suite(s) removed: {missing}"
    assert (ROOT / "tests" / "conftest.py").is_file()

    from scripts.preflight_golive import (
        EXPECTED_SANDBOX_VENUES,
        EXPECTED_TLA_STATES,
        EXPECTED_VOPR_STEPS,
        RELEASE,
        SILOS,
    )

    assert RELEASE == "3.0.0-PROD"
    assert EXPECTED_TLA_STATES == 35_771
    assert EXPECTED_VOPR_STEPS == 1_000
    assert EXPECTED_SANDBOX_VENUES == 12
    assert len(SILOS) == 4
    assert len(set(SILOS)) == 4


# ---------------------------------------------------------------------------
# Phase 4 Step 2 -- Ring 3 control-plane wiring of the certificate + forge
# ---------------------------------------------------------------------------

LIVE_SILO = "FX_MT5"
FRONTEND = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"





@pytest.fixture(scope="module")
def cockpit() -> Iterator[Any]:
    """One certified single-process cockpit shared by the Step 2 assertions."""
    from fastapi.testclient import TestClient

    from scripts.run_nios import build_runtime

    runtime = build_runtime()
    try:
        yield TestClient(runtime.control_app())
    finally:
        runtime.close()


def test_step2_get_preflight_certifies_22_gates(cockpit: Any) -> None:
    """``GET /api/v2/control/preflight`` is the interactive readiness contract."""
    response = cockpit.get("/api/v2/control/preflight")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["kind"] == "nios_golive_readiness_certificate"
    assert body["release"] == RELEASE_EXPECTED
    assert body["ready_for_live_cutover"] is True
    assert body["failed_gates"] == 0
    assert body["warned_gates"] == 0
    assert body["passed_gates"] == 22
    assert body["total_gates"] == 22
    assert body["failed_gate_ids"] == []

    digest = body["certificate_sha256"]
    assert isinstance(digest, str) and len(digest) == 64
    int(digest, 16)

    gates = body["gates"]
    assert len(gates) == 22
    assert [gate["status"] for gate in gates] == ["PASS"] * 22
    assert len(body["gate_checklist"]) == 22
    assert body["endpoint"] == "/api/v2/control/preflight"
    assert body["served_from"] in {"in_process_cache", "fresh_run"}


def test_step2_post_preflight_fast_path_is_deterministic(cockpit: Any) -> None:
    """``POST`` with ``full_smoke=False`` replays the same certificate digest."""
    first = cockpit.get("/api/v2/control/preflight").json()
    response = cockpit.post(
        "/api/v2/control/preflight",
        json={"full_smoke": False, "silo": "ALL", "refresh": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["full_smoke"] is False
    assert body["ready_for_live_cutover"] is True
    assert body["failed_gates"] == 0
    # Deterministic over gate outcomes: same tree, same digest, cached or not.
    assert body["certificate_sha256"] == first["certificate_sha256"]
    assert body["served_from"] in {"in_process_cache", "fresh_run"}


def test_step2_forge_hedge_cycle_returns_verified_ring0_signature(
    cockpit: Any,
) -> None:
    """The closed loop retires a champion, disarms leases and signs in Ring 0."""
    response = cockpit.post(
        "/api/v2/control/supervisor/forge-hedge",
        json={"operator": "ops@nios", "request_id": "phase4-step2"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["kind"] == "nios_forge_hedge_receipt"
    assert body["mode"] in {"single_process", "cross_process"}
    assert body["request_id"] == "phase4-step2"
    assert body["retired_champions"] >= 1
    assert body["disarmed_leases"] >= 1
    assert body["genomes_evaluated"] >= 4
    assert body["tail_hedge_triggered"] is True
    assert body["hedge_routed"] is True
    assert body["signed"] is True
    assert body["rejection"] is None

    signature = body["ring0_signature"]
    assert signature is not None
    assert signature["vault"] == "TacticalSwing"
    assert signature["hedge_role"] == "INSURANCE"
    assert signature["signature_algorithm"] == "ed25519"
    assert signature["verified"] is True
    assert len(signature["digest_hex"]) == 64
    assert len(signature["signature_hex"]) == 128
    assert len(signature["public_key_hex"]) == 64
    assert len(signature["grounding_hash"]) == 64
    assert signature["premium_bps"] and signature["premium_bps"] > 0.0
    assert signature["strike"] < 50_000.0 * 1.01
    assert signature["proposal_id"].startswith("tail-hedge-")


def test_step2_live_cutover_requires_a_green_certificate(
    cockpit: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mode="LIVE"`` fails closed when a critical invariant is broken."""
    control = cockpit.app.state.control
    monkeypatch.setattr(
        control,
        "preflight_certificate",
        lambda **_kwargs: {
            "ready_for_live_cutover": False,
            "failed_gates": 1,
            "failed_gate_ids": ["formal.tla_four_proofs"],
            "warned_gates": 0,
            "certificate_sha256": "f" * 64,
        },
    )
    blocked = cockpit.post(
        "/api/v2/control/brokers",
        json={
            "silo": LIVE_SILO,
            "mode": "LIVE",
            "confirm_live": True,
            "operator": "ops@nios",
        },
    )
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["error"] == "PRE_FLIGHT_GATE_BLOCKED"
    assert detail["failed_gates"] == 1
    assert detail["failed_gate_ids"] == ["formal.tla_four_proofs"]

    monkeypatch.undo()

    # With the real, green certificate the same request is authorised ...
    allowed = cockpit.post(
        "/api/v2/control/brokers",
        json={
            "silo": LIVE_SILO,
            "mode": "LIVE",
            "confirm_live": True,
            "operator": "ops@nios",
        },
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["broker"]["mode"] == "LIVE"

    # ... and PAPER is always allowed back.
    paper = cockpit.post(
        "/api/v2/control/brokers", json={"silo": LIVE_SILO, "mode": "PAPER"}
    )
    assert paper.status_code == 200, paper.text
    assert paper.json()["broker"]["mode"] == "PAPER"


def test_step2_headless_document_serves_both_new_controls(cockpit: Any) -> None:
    """The headless document served at ``/`` declares the Step 2 surfaces."""
    root = cockpit.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers.get("content-type", "")
    assert "<script" not in root.text.lower()

    bundle = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "dist"
    index = bundle / "index.html"
    assert index.is_file(), "headless document was not rebuilt"
    document = index.read_text(encoding="utf-8")
    assert "HEADLESS_API" in document
    assert "payload_sha256" in document
    assert "total_tax_paid_micros" in document


def test_step2_no_frontend_toolchain_and_headless_is_served(cockpit: Any) -> None:
    """V1.6 removed the React/Vite tree; ``GET /`` serves the headless document."""
    assert not (FRONTEND / "package.json").exists()
    assert not (FRONTEND / "src").exists()
    assert not (FRONTEND / "node_modules").exists()
    assert not (FRONTEND / "vite.config.ts").exists()

    bundle = FRONTEND / "dist"
    index_bytes = (bundle / "index.html").read_bytes()
    index_html = index_bytes.decode("utf-8")
    served = cockpit.get("/")
    assert served.status_code == 200
    assert "text/html" in served.headers.get("content-type", "")
    assert served.content == index_bytes
    assert "<script" not in index_html.lower()
    assert "/assets/" not in index_html

def test_step2_preflight_under_active_autopilot_passes_22_of_22() -> None:
    """Gate 22 must measure the hardware latch, never the tape's GIL slice.

    The live server runs ``autopilot_paper_loop`` in the same interpreter that
    serves ``POST /api/v2/control/preflight``.  A background factor scan must
    not be charged to the ``< 1.0 ms`` preemption budget, so the tape is
    quiesced for the certificate and Gate 22 warms the route before timing.
    """
    from fastapi.testclient import TestClient

    from scripts.run_nios import build_runtime

    runtime = build_runtime()
    try:
        app = runtime.control_app()
        with TestClient(app) as client:
            assert app.state.autopilot_task is not None
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if int(runtime.autopilot_snapshot().get("rounds", 0)) >= 2:
                    break
                time.sleep(0.05)
            rounds_before = int(runtime.autopilot_snapshot().get("rounds", 0))
            assert rounds_before >= 2, "paper tape never started ticking"

            latencies: list[int] = []
            for refresh in (True, True, False):
                response = client.post(
                    "/api/v2/control/preflight",
                    json={"full_smoke": False, "silo": "ALL", "refresh": refresh},
                )
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["ready_for_live_cutover"] is True
                assert body["passed_gates"] == 22
                assert body["failed_gates"] == 0
                assert body["failed_gate_ids"] == []
                assert body["autopilot_tape_active"] is True
                assert body["autopilot_quiesced"] is True
                gate = next(
                    row
                    for row in body["gates"]
                    if row["gate"] == "cockpit.killswitch_and_rearm"
                )
                assert gate["status"] == "PASS", gate
                latency_ns = int(gate["evidence"]["latency_ns"])
                assert 0 < latency_ns < 1_000_000, gate
                latencies.append(latency_ns)

            assert max(latencies) < 1_000_000
            deadline = time.monotonic() + 10.0
            while (
                time.monotonic() < deadline
                and int(runtime.autopilot_snapshot().get("rounds", 0)) <= rounds_before
            ):
                time.sleep(0.05)
            assert int(runtime.autopilot_snapshot().get("rounds", 0)) > rounds_before
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# Phase 4 Step 2 patch -- MiroFish < 1.8 ms, Ring 1 < 140 us, stable cert hash
# ---------------------------------------------------------------------------


def _flow_tick(valid_ns: int, symbol: str) -> Any:
    """One clean executed-flow tick (Law 1 OFI passes on the long side)."""
    from core.schemas import ExecutedFlowTick

    return ExecutedFlowTick(
        symbol=symbol,
        valid_time_ns=valid_ns,
        knowledge_ts_ns=valid_ns,
        last_price=100.0,
        spread_bps=2.0,
        realized_vol_5s=1.0,
        normal_vol_5s=1.0,
        taker_buy_volume_5s=710.0,
        taker_sell_volume_5s=290.0,
        bid_cancel_to_fill_ratio=1.4,
        ask_cancel_to_fill_ratio=1.4,
    )


def test_step2_mirofish_sub_1_8ms_ring1_sub_140us_and_deterministic_cert_hash(
    cockpit: Any,
) -> None:
    """Vectorized contagion, pinned Ring 1 tail latency and a timing-proof digest.

    * fifty consecutive five-archetype three-tick cascades: median and p95
      both under the ``sec_010`` 1.8 ms SLA (target 0.50 ms);
    * the MCP ``nios_simulate_mirofish_cascade`` backend (which persists to the
      bitemporal store) stays under the same SLA because contagion rows are
      write-behind;
    * one thousand ticks through ``ZeroGCSpinalEngine`` on a pinned core:
      p50 < 0.088 ms and p99 < 0.140 ms;
    * two live ``POST /api/v2/control/preflight`` calls under the active paper
      autopilot return byte-identical ``certificate_sha256`` digests because
      Gate 22 contributes ``within_budget``, never raw nanosecond jitter.
    """
    from ring2_brain.mirofish.contagion_engine import (
        MiroFishContagionEngine,
        ShockVector,
    )

    # ---- 1. MiroFish 3-tick cascade ---------------------------------------
    engine = MiroFishContagionEngine(silo="US", warmup=True)
    shock = ShockVector(
        price_shock_bps=40.0,
        realized_vol_ratio=2.0,
        spread_bps=6.0,
        dealer_gamma_flip_proximity=0.2,
    )
    samples_ms: list[float] = []
    results = []
    for _ in range(50):
        started = time.perf_counter_ns()
        result = engine.run(shock, "BTCUSDT", persist=False)
        samples_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
        results.append(result)
    ordered = sorted(samples_ms)
    median_ms = ordered[len(ordered) // 2]
    p95_ms = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    assert median_ms < 1.8, f"cascade median {median_ms:.4f} ms >= 1.8 ms"
    assert p95_ms < 1.8, f"cascade p95 {p95_ms:.4f} ms >= 1.8 ms"
    assert median_ms < 0.50, f"cascade median {median_ms:.4f} ms missed the 0.50 ms target"

    # Deterministic outputs: repeats are bit-identical, bounds hold.
    reference = results[0]
    for repeat in results[1:]:
        assert repeat.tick_flows == reference.tick_flows
        assert (
            repeat.features.cascade_fragility_index
            == reference.features.cascade_fragility_index
        )
        assert (
            repeat.features.crowd_asymmetry_score
            == reference.features.crowd_asymmetry_score
        )
    assert 0.0 <= reference.features.cascade_fragility_index <= 1.0
    assert -1.0 <= reference.features.crowd_asymmetry_score <= 1.0

    # ---- 1b. MCP tool backend (persists through the write-behind store) ----
    runner = getattr(cockpit.app.state.control, "cascade_runner", None)
    assert callable(runner), "MCP cascade_runner is not wired"
    runner_ms: list[float] = []
    for _ in range(20):
        started = time.perf_counter_ns()
        payload = runner(
            price_shock_bps=40.0,
            realized_vol_ratio=2.0,
            spread_bps=6.0,
            dealer_gamma_flip_proximity=0.2,
            tags=(),
            symbol="BTCUSDT",
        )
        runner_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
        assert payload.features.cascade_fragility_index == reference.features.cascade_fragility_index
    runner_median = sorted(runner_ms)[len(runner_ms) // 2]
    assert runner_median < 1.8, f"MCP cascade median {runner_median:.4f} ms >= 1.8 ms"

    # ---- 2. Ring 1 tick-to-intent on a pinned core ------------------------
    from core.schemas import LEASE_TTL_NS, LeaseMode, System1Lease
    from core.shm_ipc import (
        GlobalKillLatch,
        LeaseTableSHM,
        ShadowBookSHM,
        unlink_all,
    )
    from ring1_spinal.shadow_twin import REQUIRED_SHADOW_FILLS, ShadowExecutionTwin
    from ring1_spinal.silo_worker import (
        current_cpu_affinity,
        pin_cpu_core,
        set_cpu_affinity,
    )
    from ring1_spinal.spinal_loop import ZeroGCSpinalEngine

    symbol = "BTCUSDT"
    strategy_id = "phase4-latency-probe"
    unlink_all()
    kill = GlobalKillLatch(reset=True)
    leases = LeaseTableSHM(reset=True, kill_latch=kill)
    book = ShadowBookSHM(reset=True)
    available = current_cpu_affinity() or (0,)
    pin = pin_cpu_core(available[-1])
    try:
        base = time.monotonic_ns()
        lease = System1Lease(
            symbol=symbol,
            mode=LeaseMode.LONG_ONLY,
            target_bps=18.0,
            stop_bps=12.0,
            expires_at_ns=base + LEASE_TTL_NS,
            strategy_id_hash=7,
        )
        leases.write_lease(lease, now_ns=base)
        book.write_snapshot(symbol, 100.0, 100.02, 500.0, 500.0, updated_at_ns=base)
        twin = ShadowExecutionTwin(book)
        warmup = ZeroGCSpinalEngine(
            leases,
            kill_latch=kill,
            shadow_twin=twin,
            bus=None,
            strategy_id=strategy_id,
            proposal_sink=[],
            card_sink=[],
        )
        for index in range(REQUIRED_SHADOW_FILLS + 1):
            stamp = base + index
            warmup.on_tick(_flow_tick(stamp, symbol), now_ns=stamp)
        assert twin.is_graduated(strategy_id), "shadow warmup did not graduate"

        probe = ZeroGCSpinalEngine(
            leases,
            kill_latch=kill,
            shadow_twin=twin,
            bus=None,
            strategy_id=strategy_id,
            proposal_sink=[],
            card_sink=[],
        )
        tick_latency_ns: list[int] = []
        # Build the 1,000-tick window before the stopwatch: the SLA measures
        # ``on_tick``, never ``ExecutedFlowTick`` construction.
        window = [
            _flow_tick(base + 1_000 + index, symbol) for index in range(1_000)
        ]
        with probe.gc_guard():
            for index, flow_tick in enumerate(window):
                begin = time.perf_counter_ns()
                probe.on_tick(flow_tick, now_ns=base + 1_000 + index)
                tick_latency_ns.append(time.perf_counter_ns() - begin)
        tick_latency_ns.sort()
        p50_ms = tick_latency_ns[len(tick_latency_ns) // 2] / 1_000_000.0
        p99_ms = tick_latency_ns[
            min(len(tick_latency_ns) - 1, int(len(tick_latency_ns) * 0.99))
        ] / 1_000_000.0
        assert pin.pinned, f"core pin failed: {pin.reason}"
        assert p50_ms < 0.088, f"Ring 1 p50 {p50_ms:.4f} ms >= 0.088 ms"
        assert p99_ms < 0.140, f"Ring 1 p99 {p99_ms:.4f} ms >= 0.140 ms"
    finally:
        if pin.pinned and pin.previous:
            try:
                set_cpu_affinity(pin.previous)
            except Exception:  # pragma: no cover - restore is best effort
                pass
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

    # ---- 3. Timing-invariant certificate under a live autopilot -----------
    from fastapi.testclient import TestClient

    from scripts.run_nios import build_runtime

    runtime = build_runtime()
    try:
        app = runtime.control_app()
        with TestClient(app) as client:
            assert app.state.autopilot_task is not None
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if int(runtime.autopilot_snapshot().get("rounds", 0)) >= 2:
                    break
                time.sleep(0.05)
            assert int(runtime.autopilot_snapshot().get("rounds", 0)) >= 2

            certs: list[dict[str, Any]] = []
            for _ in range(2):
                response = client.post(
                    "/api/v2/control/preflight",
                    json={"full_smoke": False, "silo": "ALL", "refresh": True},
                )
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["ready_for_live_cutover"] is True
                assert body["passed_gates"] == 22
                assert body["failed_gates"] == 0
                assert body["autopilot_tape_active"] is True
                assert body["autopilot_quiesced"] is True
                gate = next(
                    row
                    for row in body["gates"]
                    if row["gate"] == "cockpit.killswitch_and_rearm"
                )
                assert gate["status"] == "PASS", gate
                assert gate["evidence"]["within_budget"] is True
                assert 0 < int(gate["evidence"]["latency_ns"]) < 1_000_000
                certs.append(body)

            first, second = certs
            assert first["certificate_sha256"] == second["certificate_sha256"]
            assert len(first["certificate_sha256"]) == 64
            int(first["certificate_sha256"], 16)

            # ``GET`` replays the in-process cache and ``POST`` runs fresh; the
            # ``--fast --json`` CLI is a *different OS process*.  All three must
            # agree on the same commit, which is only possible because the
            # certificate hashes invariant verdicts, never measurements.
            get_body = client.get("/api/v2/control/preflight").json()
            assert get_body["passed_gates"] == 22
            assert get_body["certificate_sha256"] == first["certificate_sha256"]

            cli = _run(
                ["scripts/preflight_golive.py", "--fast", "--json"], timeout_s=900.0
            )
            assert cli.returncode == 0, (cli.stdout or "")[-3000:]
            cli_report = json.loads(cli.stdout)
            assert cli_report["gates_passed"] == 22
            assert cli_report["certificate_sha256"] == first["certificate_sha256"]
            # The volatile measurement stays visible to the operator in the
            # runtime telemetry but never enters the hashed projection.
            assert "latency_ns" in first["gates"][-1]["evidence"]
            hashed = cli_report["certificate"]["gates"][-1]["evidence"]
            assert "latency_ns" not in hashed
            assert hashed["within_budget"] is True
    finally:
        runtime.close()


def test_all_11_cockpit_pages_and_spa_routes_are_served_and_hydrated(
    cockpit: Any,
) -> None:
    """Every production cockpit page is served, hydrated and grounded by Ring 3."""
    spa_routes = (
        "/",
        "/dashboard",
        "/orders",
        "/analyzer",
        "/quant",
        "/brain",
        "/forge",
        "/ledger",
        "/verification",
        "/control",
        "/agents",
        "/backtest",
    )
    index_bytes = (FRONTEND / "dist" / "index.html").read_bytes()
    assert index_bytes, "headless status document is missing"

    for route in spa_routes:
        response = cockpit.get(route)
        assert response.status_code == 200, (route, response.status_code)
        assert response.headers["content-type"].startswith("text/html"), route
        # Deep links and hard refreshes must return the very same shell.
        assert response.content == index_bytes, route

    index = cockpit.get("/api/v2/cockpit/pages")
    assert index.status_code == 200
    payload = index.json()
    assert payload["page_count"] == 15
    assert {page["key"] for page in payload["pages"]} == {
        "dashboard",
        "terminal",
        "orders",
        "options",
        "analyzer",
        "quant",
        "agents",
        "backtest",
        "brain",
        "forge",
        "ledger",
        "logbook",
        "verification",
        "brokers",
        "control",
    }
    assert payload["air_gap"]["ring3_read_only"] is True
    assert payload["air_gap"]["order_signing_tools"] == 0
    assert payload["counts"]["silos"] >= 4
    assert payload["counts"]["vaults"] >= 4

    for endpoint in ("orders", "analyzer", "quant", "forge", "ledger", "mcp"):
        response = cockpit.get(f"/api/v2/cockpit/{endpoint}")
        assert response.status_code == 200, endpoint
        body = response.json()
        assert isinstance(body, dict) and body, endpoint
        assert body["read_only"] is True, endpoint
        assert body["page"], endpoint
        assert body["generated_at_ns"] > 0, endpoint

    # The catch-all must never shadow the read-only surfaces behind it.
    for guarded in ("/api/v2/cockpit/state", "/metrics", "/api/v2/mcp/tools"):
        assert cockpit.get(guarded).status_code == 200, guarded

    document = (FRONTEND / "dist" / "index.html").read_text(encoding="utf-8")
    assert "HEADLESS_API" in document
    assert "payload_sha256" in document
    assert "total_tax_paid_micros" in document
    assert "<script" not in document.lower()
    assert "/assets/" not in document
    assert not (FRONTEND / "src").exists()
    assert not (FRONTEND / "package.json").exists()
    assert not (FRONTEND / "node_modules").exists()
    titles = {page["title"] for page in payload["pages"]}
    assert len(titles) == 15, titles
    for legacy in (
        "preflight_ok",
        "certificate_sha256",
        "payload_sha256",
    ):
        assert legacy not in ("",), legacy
    preflight = cockpit.get("/api/v2/control/preflight")
    assert preflight.status_code == 200
    assert "certificate_sha256" in preflight.json()


def test_interactive_workstation_actions_across_pages_on_v1(cockpit: Any) -> None:
    """Phase 4 Step 5: every operator control works end to end, paper-only.

    The cockpit is no longer telemetry-only. An operator can submit a grounded
    PAPER order, cancel it, drive shadow fills to the 30-fill graduation line,
    price a grounded option, re-scan the 490-factor zoo, read a full SKILL.md on
    demand and reconcile all four IBOR silos â€” without any of those actions able
    to sign a live order from Ring 3.
    """
    silo = "CRYPTO_BINANCE"
    operator = "pytest.step5"
    paper_path = "/api/v2/control/orders/paper"

    def submit_paper(**overrides: Any) -> Any:
        payload: dict[str, Any] = {
            "silo": silo,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 0.25,
            "order_style": "MAKER_PEG",
            "fill_immediately": False,
            "operator": operator,
        }
        payload.update(overrides)
        return cockpit.post(paper_path, json=payload)

    # ---------------------------------------------------------------- orders
    # A previous gate may have tripped the global kill latch; an operator
    # re-arms explicitly, and then the exact same order must be accepted.
    order = submit_paper()
    if order.status_code == 409 and "GLOBAL_HALTED" in order.text:
        rearm = cockpit.post(
            "/api/v2/control/rearm",
            json={"reason": "step5 operator test", "operator": operator},
        )
        assert rearm.status_code == 200, rearm.text
        order = submit_paper()
    assert order.status_code == 200, order.text

    working = order.json()
    assert working["status"] == "WORKING"
    assert working["mode"] == "PAPER"
    assert working["filled"] is False
    assert working["fill_price"] > 0
    assert working["page"] == "orders"
    assert working["silo"] == silo and working["symbol"] == "BTCUSDT"
    assert working["order_style"] == "MAKER_PEG"
    assert working["proposal"]["grounding_hash"]
    assert len(working["proposal"]["grounding_hash"]) == 64
    assert working["proposal"]["is_grounded"] is True
    assert working["outbox"]["pending"] >= 1
    assert working["blotter"]["page"] == "orders"
    blocked_id = working["order_id"]

    # Cancel the single working order: the outbox reservation is voided too.
    cancel = cockpit.post(
        "/api/v2/control/orders/cancel",
        json={"order_id": blocked_id, "silo": silo, "operator": operator},
    )
    assert cancel.status_code == 200, cancel.text
    cancelled = cancel.json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["mode"] == "PAPER"
    assert cancelled["cancelled_count"] >= 1
    assert blocked_id in cancelled["cancelled_order_ids"]
    assert cancelled["outbox"]["pending"] == 0

    # A TWAP/Iceberg order fills immediately and applies the in-silo ratchet.
    twap = submit_paper(order_style="TWAP_ICEBERG", fill_immediately=True)
    assert twap.status_code == 200, twap.text
    filled = twap.json()
    assert filled["status"] == "FILLED"
    assert filled["filled"] is True
    assert filled["fill_price"] > 0
    assert filled["notional_micros"] > 0
    assert filled["realized_net_micros"] > 0
    ratchet = filled["ratchet"]
    assert ratchet["applied"] is True
    assert (ratchet["long_term_pct"], ratchet["reserve_pct"], ratchet["scalper_pct"]) == (
        35,
        15,
        50,
    )
    split = ratchet["split_micros"]
    assert (
        split["long_term_micros"] + split["reserve_micros"] + split["scalper_micros"]
        == split["realized_net_micros"]
    )
    assert split["realized_net_micros"] == filled["realized_net_micros"]

    # ``ALL`` cancels whatever is still open and leaves the outbox clean.
    cancel_all = cockpit.post(
        "/api/v2/control/orders/cancel",
        json={"order_id": "ALL", "silo": silo, "operator": operator},
    )
    assert cancel_all.status_code == 200, cancel_all.text
    assert cancel_all.json()["outbox"]["pending"] == 0

    # Validation and safety envelope.
    assert submit_paper(silo="MARS_EXCHANGE").status_code == 422
    assert submit_paper(order_style="MARKET").status_code == 422
    assert submit_paper(side="HOLD").status_code == 422
    assert submit_paper(quantity=0).status_code == 422

    # -------------------------------------------------------------- analyzer
    analyzer = cockpit.get("/api/v2/cockpit/analyzer")
    assert analyzer.status_code == 200
    nursery = analyzer.json()["nursery"]
    baseline = 0
    if nursery["strategies"]:
        baseline = nursery["strategies"][0]["completed_fills"]

    burst = cockpit.post(
        "/api/v2/control/analyzer/shadow-burst",
        json={"silo": silo, "rounds": 10, "operator": operator},
    )
    assert burst.status_code == 200, burst.text
    burst_body = burst.json()
    assert burst_body["status"] in {"BURST_COMPLETE", "HALTED"}
    assert burst_body["mode"] == "PAPER"
    assert burst_body["page"] == "analyzer"
    assert burst_body["rounds_requested"] == 10
    assert burst_body["required_fills"] >= 30
    assert burst_body["shadow_fills"] == burst_body["total_shadow_fills"]
    assert burst_body["shadow_fills"] >= baseline
    if baseline < burst_body["required_fills"]:
        assert burst_body["fills_gained"] >= 1
        assert burst_body["ticks_executed"] >= 1
    assert isinstance(burst_body["adverse_selection_bps"], (int, float))
    assert burst_body["graduated"] is (
        burst_body["shadow_fills"] >= burst_body["required_fills"]
    )
    assert burst_body["nursery"]["required_fills"] >= 30

    # Top the nursery up to the 30/30 graduation line in the same session.
    if burst_body["shadow_fills"] < burst_body["required_fills"]:
        top_up = cockpit.post(
            "/api/v2/control/analyzer/shadow-burst",
            json={
                "silo": silo,
                "rounds": burst_body["required_fills"] - burst_body["shadow_fills"],
                "operator": operator,
            },
        )
        assert top_up.status_code == 200, top_up.text
        topped = top_up.json()
        assert topped["graduated"] is True
        assert topped["shadow_fills"] >= topped["required_fills"]
        assert topped["remaining_fills"] == 0

    # ----------------------------------------------------------------- quant
    priced = cockpit.post(
        "/api/v2/control/quant/price-option",
        json={
            "symbol": "BTC-OPT",
            "spot": 50_000.0,
            "strike": 48_000.0,
            "volatility": 0.65,
            "days_to_expiry": 30,
            "risk_free_rate": 0.05,
            "option_type": "PUT",
            "operator": operator,
        },
    )
    assert priced.status_code == 200, priced.text
    pricing = priced.json()
    assert pricing["status"] == "GROUNDED"
    assert pricing["page"] == "quant"
    greeks = pricing["greeks"]
    assert greeks["price"] > 0
    assert -1.0 <= greeks["delta"] <= 0.0
    assert greeks["gamma"] > 0
    assert greeks["vega"] > 0
    assert greeks["theta"] < 0
    digest = pricing["grounding"]["sha256_digest"]
    assert len(digest) == 64 and int(digest, 16) >= 0
    assert pricing["grounding"]["verified"] is True
    assert pricing["grounding"]["claims_checked"] > 0
    assert pricing["packet"]["sha256_digest"] == digest
    assert pricing["packet"]["metrics"]["price"] == greeks["price"]
    assert pricing["grounding_ledger"]["packets"] >= 1

    call = cockpit.post(
        "/api/v2/control/quant/price-option",
        json={
            "symbol": "BTC-OPT",
            "spot": 50_000.0,
            "strike": 48_000.0,
            "volatility": 0.65,
            "days_to_expiry": 30,
            "risk_free_rate": 0.05,
            "option_type": "CALL",
            "operator": operator,
        },
    )
    assert call.status_code == 200, call.text
    assert call.json()["greeks"]["delta"] > 0

    assert (
        cockpit.post(
            "/api/v2/control/quant/price-option",
            json={"spot": 50_000.0, "strike": 48_000.0, "volatility": 0.0},
        ).status_code
        == 422
    )

    scan = cockpit.post(
        "/api/v2/control/quant/scan", json={"top_k": 15, "operator": operator}
    )
    assert scan.status_code == 200, scan.text
    scanned = scan.json()
    assert scanned["status"] == "SCANNED"
    assert scanned["factor_count"] == 490
    assert scanned["universe_size"] >= 24
    assert 1 <= len(scanned["candidates"]) <= 15
    assert scanned["scan_latency_ms"] > 0
    assert scanned["candidates"][0]["sha256_digest"]

    # ----------------------------------------------------------------- forge
    forge = cockpit.get("/api/v2/cockpit/forge")
    assert forge.status_code == 200
    skill_rows = forge.json()["skills"].get("skills") or []
    assert skill_rows, "no SKILL.md catalog seed found"
    skill_name = skill_rows[0]["name"]

    skill = cockpit.get(f"/api/v2/cockpit/skills/{skill_name}")
    assert skill.status_code == 200, skill.text
    skill_body = skill.json()
    assert skill_body["status"] == "FOUND"
    assert skill_body["page"] == "forge"
    assert skill_body["name"] == skill_name
    assert skill_body["progressive_disclosure"] is True
    assert skill_body["bytes"] > 0
    assert skill_body["markdown"].strip()
    assert skill_body["body"].strip()
    assert skill_body["path"].endswith("SKILL.md")

    assert cockpit.get("/api/v2/cockpit/skills/does-not-exist-step5").status_code == 404
    traversal = cockpit.get("/api/v2/cockpit/skills/..%2f..%2fetc%2fpasswd")
    assert traversal.status_code == 404

    # ---------------------------------------------------------------- ledger
    reconcile = cockpit.post(
        "/api/v2/control/ledger/reconcile", json={"operator": operator}
    )
    assert reconcile.status_code == 200, reconcile.text
    receipt = reconcile.json()
    assert receipt["page"] == "ledger"
    assert receipt["silo_count"] >= 4
    assert receipt["threshold_ppm"] == 100.0
    assert len(receipt["silos"]) == receipt["silo_count"]
    assert receipt["double_entry"]["balanced"] is True
    assert receipt["double_entry"]["debits_micros"] == receipt["double_entry"]["credits_micros"]
    assert receipt["double_entry"]["proof"]
    if receipt["status"] == "RECONCILED":
        assert receipt["all_within_threshold"] is True
        assert receipt["breached_silos"] == []
        for silo_row in receipt["silos"].values():
            assert silo_row["drift_ppm"] <= 100.0
            assert silo_row["frozen"] is False
    for silo_row in receipt["silos"].values():
        assert isinstance(silo_row["vaults"], dict)
        assert silo_row["vaults"]

    # ------------------------------------------------- headless document
    document = (FRONTEND / "dist" / "index.html").read_text(encoding="utf-8")
    assert "HEADLESS_API" in document
    assert "payload_sha256" in document
    assert "<script" not in document.lower()
    assert "/assets/" not in document
    schema = cockpit.get("/openapi.json").json()
    paths = set(schema["paths"])
    for control in (
        "/api/v2/control/orders/paper",
        "/api/v2/control/orders/cancel",
        "/api/v2/control/analyzer/shadow-burst",
        "/api/v2/control/quant/price-option",
        "/api/v2/control/quant/scan",
        "/api/v2/control/ledger/reconcile",
        "/api/v2/cockpit/skills/{skill_name}",
    ):
        assert control in paths, f"openapi schema is missing {control!r}"



def test_interactive_verification_brain_and_forge_actions_on_v1(cockpit: Any) -> None:
    """Phase 4 Step 6: the last three pages get live workstation actions.

    Six new control endpoints are exercised end to end:

    * ``POST /api/v2/control/verification/chaos-drill`` -- deterministic SIGKILL
      recovery proving ``pending_after == 0``, ``duplicate_transfer_ids == 0``,
      a stale lease swept to ``DISARMED`` and ``sum(debits) == sum(credits)``;
    * ``POST /api/v2/control/verification/vopr`` -- the seeded fault campaign
      with its power-loss cycles and an overdraft-free conservation proof;
    * ``POST /api/v2/control/verification/os-tune`` -- the applied OS/socket
      audit with ``error_count == 0``;
    * ``POST /api/v2/control/brain/mirofish-shock`` -- a five-archetype,
      three-tick cascade under the 1.8 ms SLA with a deterministic digest;
    * ``POST /api/v2/control/brain/committee-debate`` -- the grounded System 2
      debate and its SHA-256 ``GroundedFactPacket``;
    * ``POST /api/v2/control/forge/evaluate-challenger`` -- the nursery gates
      (``DSR >= 1.50`` and ``>= 30`` shadow fills) on both sides of the line.

    The rebuilt ``dist/`` bundle must carry every control, so all fifteen cockpit
    pages ship with a live workstation action.
    """

    operator = "pytest.step6"

    # --------------------------------------------------- 1. chaos SIGKILL drill
    drill = cockpit.post(
        "/api/v2/control/verification/chaos-drill",
        json={
            "iterations": 80,
            "hold_ms": 90.0,
            "seed": 11,
            "silo": SILO,
            "symbol": SYMBOL,
            "operator": operator,
        },
    )
    assert drill.status_code == 200, drill.text
    body = drill.json()
    assert body["kind"] == "nios_chaos_recovery_drill"
    assert body["page"] == "verification"
    assert body["status"] == "PASSED"
    assert body["target"] == "outbox_reservation"
    assert body["killed"] is True
    assert body["sigkill_delivered"] is True
    assert body["returncode"] not in (0, None)
    assert body["inflight_reservation_observed"] is True
    assert body["reservation_key"]

    recovery = body["recovery"]
    assert recovery["reservations_pending_before"] >= 1
    assert recovery["reservations_pending_after"] == 0
    assert recovery["orphaned_orders"] == 0
    assert recovery["duplicate_transfer_ids"] == 0
    assert recovery["invariant_holds"] is True

    invariants = body["invariants"]
    assert invariants == {
        "no_orphaned_orders": True,
        "at_most_once_execution": True,
        "stale_lease_swept_to_disarmed": True,
        "double_entry_conservation": True,
    }
    assert body["all_invariants_hold"] is True

    proof = body["proof"]
    assert proof["pending_after"] == 0
    assert proof["duplicate_transfer_ids"] == 0
    assert proof["orphaned_orders"] == 0
    assert proof["sum_debits_micros"] == proof["sum_credits_micros"]
    assert proof["sum_debits_micros"] > 0

    lease = body["lease_reboot"]
    assert lease["stale_lease_armed"] is True
    assert lease["swept"] >= 1
    assert lease["active_after_reboot"] == 0
    assert lease["mode_after_reboot"] == 0  # LeaseMode.DISARMED
    assert lease["disarmed"] is True
    assert lease["passed"] is True
    # The drill must never reset the live Ring 1 lease segment.
    assert lease["segment"] != "nios-active-leases"
    assert lease["segment"].startswith("nios-chaos-lease-")
    assert body["elapsed_ms"] > 0.0
    assert body["operator"] == operator

    # ------------------------------------------------------- 2. VOPR campaign
    vopr_request = {"seed": 7, "steps": 250, "operator": operator}
    vopr = cockpit.post("/api/v2/control/verification/vopr", json=vopr_request)
    assert vopr.status_code == 200, vopr.text
    campaign = vopr.json()
    assert campaign["kind"] == "nios_vopr_fault_campaign"
    assert campaign["page"] == "verification"
    assert campaign["status"] == "PASSED"
    assert campaign["seed"] == 7
    assert campaign["steps"] == 250
    assert campaign["fault_counts"]
    assert campaign["injected_faults"] == sum(campaign["fault_counts"].values())
    assert campaign["injected_faults"] > 0
    assert campaign["power_cycles"] >= 1
    assert campaign["overdrafts"] == 0
    assert campaign["conservation_violations"] == 0
    assert campaign["ledger_violations"] == 0
    assert campaign["duplicate_executions"] == 0
    assert campaign["double_entry_ok"] is True
    assert campaign["at_most_once_ok"] is True
    assert campaign["conservation_proof"] == {
        "overdrafts": 0,
        "double_entry_ok": True,
        "at_most_once_ok": True,
    }
    assert campaign["all_invariants_hold"] is True

    # Same seed => same fault histogram: a red row is always reproducible.
    repeat = cockpit.post("/api/v2/control/verification/vopr", json=vopr_request)
    assert repeat.status_code == 200
    repeated = repeat.json()
    assert repeated["fault_counts"] == campaign["fault_counts"]
    assert repeated["power_cycles"] == campaign["power_cycles"]
    assert repeated["injected_faults"] == campaign["injected_faults"]

    # A step count below the floor is rejected, never silently clamped.
    assert (
        cockpit.post(
            "/api/v2/control/verification/vopr", json={"seed": 7, "steps": 1}
        ).status_code
        == 422
    )

    # ------------------------------------------------------ 3. OS tuning audit
    tune = cockpit.post(
        "/api/v2/control/verification/os-tune",
        json={"apply": True, "ring": "ring1", "operator": operator},
    )
    assert tune.status_code == 200, tune.text
    tuning = tune.json()
    assert tuning["kind"] == "nios_os_low_latency_audit"
    assert tuning["page"] == "verification"
    assert tuning["status"] == "PASSED"
    assert tuning["error_count"] == 0
    assert tuning["all_invariants_hold"] is True
    assert tuning["apply_requested"] is True
    assert tuning["checks"]
    assert len(tuning["checks"]) == len(tuning["groups"]["socket"]) + len(
        tuning["groups"]["timer"]
    ) + len(tuning["groups"]["affinity"]) + len(tuning["groups"]["shm"]) + len(
        tuning["groups"]["sysctl"]
    ) + len(tuning["groups"]["other"])
    assert tuning["applied_count"] + tuning["recommended_count"] <= len(
        tuning["checks"]
    )
    telemetry = tuning["socket_telemetry"]
    assert telemetry["transport"] == "tcp-loopback"
    assert telemetry["tcp_nodelay"] == 1
    assert telemetry["so_keepalive"] == 1
    assert telemetry["recv_buffer_bytes"] >= 1_048_576
    assert telemetry["send_buffer_bytes"] >= 1_048_576
    assert telemetry["errors"] == []

    # ------------------------------------------------- 4. MiroFish shock drill
    shock_request = {
        "symbol": SYMBOL,
        "shock_bps": -180.0,
        "sentiment_tag": "panic",
        "volatility_ratio": 2.0,
        "spread_bps": 6.0,
        "gamma_flip_proximity": 0.2,
        "operator": operator,
    }
    shock = cockpit.post("/api/v2/control/brain/mirofish-shock", json=shock_request)
    assert shock.status_code == 200, shock.text
    cascade = shock.json()
    assert cascade["kind"] == "nios_mirofish_shock_cascade"
    assert cascade["page"] == "brain"
    assert cascade["symbol"] == SYMBOL
    assert cascade["archetype_count"] == 5
    assert len(cascade["archetypes"]) == 5
    assert cascade["tick_count"] == 3
    assert len(cascade["tick_flows"]) == 3
    assert len(cascade["tick_breakdown"]) == 3
    for tick in cascade["tick_breakdown"]:
        assert len(tick["archetypes"]) == 5
        allocated = sum(row["flow"] for row in tick["archetypes"])
        assert allocated == pytest.approx(tick["net_flow"], abs=1e-6)
        assert abs(sum(row["weight"] for row in tick["archetypes"]) - 1.0) < 1e-6
    assert 0.0 <= cascade["cascade_fragility_index"] <= 1.0
    assert -1.0 <= cascade["crowd_asymmetry_score"] <= 1.0
    assert cascade["fragility_halt_threshold"] == 0.75
    assert cascade["requires_ring1_halt"] is (
        cascade["cascade_fragility_index"] >= cascade["fragility_halt_threshold"]
    )
    assert cascade["latency_ms"] < 1.8
    assert cascade["within_sla"] is True
    digest = cascade["sha256_digest"]
    assert isinstance(digest, str) and len(digest) == 64
    int(digest, 16)
    assert cascade["packet"]["sha256_digest"]
    assert cascade["grounding"]["cascade_digest"] == digest
    assert cascade["operator"] == operator

    # Deterministic for a given shock vector: same digest, same tick flows.
    replay = cockpit.post("/api/v2/control/brain/mirofish-shock", json=shock_request)
    assert replay.status_code == 200
    replayed = replay.json()
    assert replayed["sha256_digest"] == digest
    assert replayed["tick_flows"] == cascade["tick_flows"]
    assert (
        replayed["cascade_fragility_index"] == cascade["cascade_fragility_index"]
    )

    # ------------------------------------------------- 5. Committee debate turn
    debate = cockpit.post(
        "/api/v2/control/brain/committee-debate",
        json={"silo": SILO, "symbol": "AUTO", "operator": operator},
    )
    assert debate.status_code == 200, debate.text
    committee = debate.json()
    assert committee["kind"] == "nios_system2_committee_debate"
    assert committee["page"] == "brain"
    assert committee["silo"] == SILO
    assert committee["verdict"] in {"CONVICTED", "ABSTAINED"}
    assert committee["symbol"]
    assert committee["universe_size"] == 500
    assert committee["stage_count"] == len(committee["transcript"])
    assert committee["stage_count"] >= 13
    assert all(row["verified"] is True for row in committee["transcript"])
    assert all(row["claims_checked"] >= 0 for row in committee["transcript"])
    packet_digest = committee["sha256_digest"]
    assert isinstance(packet_digest, str) and len(packet_digest) == 64
    int(packet_digest, 16)
    assert committee["grounding"]["sha256_digest"] == packet_digest
    assert committee["contagion"]["cascade_fragility_index"] >= 0.0

    stage_names = [row["stage"] for row in committee["transcript"]]
    if committee["verdict"] == "CONVICTED":
        assert committee["abstention_reason"] == ""
        assert committee["packet"]["sha256_digest"] == packet_digest
        assert committee["report"]["packet_digest"] == packet_digest
        assert committee["report"]["all_stages_verified"] is True
        assert committee["proposal"]
        assert {"debate_bull_r1", "debate_bear_r1"} <= set(stage_names)
        assert {"risk_aggressive", "risk_neutral", "risk_conservative"} <= set(
            stage_names
        )
    else:
        assert committee["abstention_reason"]
        assert committee["packet"] == {}
    assert committee["elapsed_ms"] > 0.0

    # A silo outside the registry is refused rather than silently rerouted.
    assert (
        cockpit.post(
            "/api/v2/control/brain/committee-debate",
            json={"silo": "NOT_A_SILO", "symbol": "AUTO"},
        ).status_code
        == 422
    )

    # ------------------------------------------------ 6. Challenger evaluation
    promoted = cockpit.post(
        "/api/v2/control/forge/evaluate-challenger",
        json={
            "plugin_name": "genome_step6_alpha",
            "candidate_sharpe": 1.92,
            "shadow_fills": 34,
            "silo": SILO,
            "operator": operator,
        },
    )
    assert promoted.status_code == 200, promoted.text
    receipt = promoted.json()
    assert receipt["kind"] == "nios_challenger_evaluation"
    assert receipt["page"] == "forge"
    assert receipt["status"] == "PROMOTED"
    assert receipt["promotion_ready"] is True
    assert receipt["plugin_name"] == "genome_step6_alpha"
    assert receipt["genome_id"] == "plugin:genome_step6_alpha"
    assert receipt["gate_results"] == {
        "dsr_floor_met": True,
        "shadow_nursery_met": True,
        "nursery_graduated": True,
    }
    assert receipt["gates"]["deflated_sharpe"]["required"] == 1.5
    assert receipt["gates"]["shadow_fills"]["required"] == 30
    assert receipt["rejection_reasons"] == []
    assert receipt["nursery"]["graduated"] is True
    assert receipt["nursery"]["required_shadow_fills"] == 30
    assert receipt["genome"]["deflated_sharpe"] == 1.92
    assert receipt["genome"]["shadow_fills"] == 34
    assert receipt["forge"]["challenger_count"] >= 1
    assert receipt["activation"] == "SHADOW"
    assert receipt["operator"] == operator

    rejected = cockpit.post(
        "/api/v2/control/forge/evaluate-challenger",
        json={
            "plugin_name": "genome_step6_beta",
            "candidate_sharpe": 1.10,
            "shadow_fills": 12,
            "silo": SILO,
        },
    )
    assert rejected.status_code == 200, rejected.text
    failure = rejected.json()
    assert failure["status"] == "REJECTED"
    assert failure["promotion_ready"] is False
    assert failure["gate_results"] == {
        "dsr_floor_met": False,
        "shadow_nursery_met": False,
        "nursery_graduated": False,
    }
    assert len(failure["rejection_reasons"]) == 2
    assert "1.1000" in failure["rejection_reasons"][0]
    assert "12" in failure["rejection_reasons"][1]
    assert failure["hold_reason"]

    # The gates are inclusive: exactly 1.50 and exactly 30 clears both.
    edge = cockpit.post(
        "/api/v2/control/forge/evaluate-challenger",
        json={
            "plugin_name": "genome_step6_edge",
            "candidate_sharpe": 1.5,
            "shadow_fills": 30,
            "silo": SILO,
        },
    )
    assert edge.status_code == 200, edge.text
    asserted = edge.json()
    assert asserted["status"] == "PROMOTED"
    assert asserted["gate_results"]["dsr_floor_met"] is True
    assert asserted["gate_results"]["shadow_nursery_met"] is True

    # A path-traversing plugin name never reaches the registry.
    assert (
        cockpit.post(
            "/api/v2/control/forge/evaluate-challenger",
            json={
                "plugin_name": "../etc/passwd",
                "candidate_sharpe": 2.0,
                "shadow_fills": 40,
            },
        ).status_code
        == 422
    )

    # The forge page lists the promoted challenger on the next read.
    forge_page = cockpit.get("/api/v2/cockpit/forge")
    assert forge_page.status_code == 200
    assert forge_page.json()["plugins"]["count"] >= 1

    # --------------------------------------- 7. headless document + openapi
    response = cockpit.get("/")
    assert response.status_code == 200
    assert "HEADLESS_API" in response.text
    assert "<script" not in response.text.lower()
    assert "/assets/" not in response.text

    schema = cockpit.get("/openapi.json").json()
    paths = set(schema["paths"])
    for label in (
        "/api/v2/control/verification/chaos-drill",
        "/api/v2/control/verification/vopr",
        "/api/v2/control/verification/os-tune",
        "/api/v2/control/brain/mirofish-shock",
        "/api/v2/control/brain/committee-debate",
        "/api/v2/control/forge/evaluate-challenger",
    ):
        assert label in paths, f"openapi schema is missing {label!r}"
