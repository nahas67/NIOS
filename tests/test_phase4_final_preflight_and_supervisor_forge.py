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
* Gate 4 -- the regression inventory grew from 17 to 18 suites while every
  pre-existing Phase 1-3 suite stayed intact.

Phase 4 Step 2 wires those organs into the Ring 3 control plane and cockpit:

* ``GET``/``POST /api/v2/control/preflight`` return the certificate with
  ``ready_for_live_cutover``, ``passed_gates``/``failed_gates`` and a
  64-character SHA-256 digest.
* ``POST /api/v2/control/supervisor/forge-hedge`` executes the closed-loop forge
  + tail-hedge cycle and returns a verified Ring 0 Ed25519 receipt.
* ``POST /api/v2/control/brokers`` refuses ``mode="LIVE"`` unless the
  certificate reports ``failed_gates == 0``.
* The compiled ``dist/`` bundle served at ``GET /`` carries both controls.
* ``npx biome lint src/`` in the cockpit workspace exits ``0`` with zero
  diagnostics, and ``GET /`` serves that rebuilt bundle asset-for-asset.

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


def test_gate4_regression_inventory_is_eighteen_suites() -> None:
    """17 pre-existing suites plus this one; the release tag stays annotated."""
    suites = sorted(
        path.name
        for path in (ROOT / "tests").glob("test_*.py")
        if path.is_file()
    )
    assert len(suites) == 18, suites
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


def _run_frontend(
    args: list[str], *, timeout_s: float
) -> subprocess.CompletedProcess:
    """Run a Node toolchain command in the cockpit workspace.

    ``npx`` is a ``.cmd`` shim on Windows, so route it through ``cmd.exe``;
    POSIX shells resolve the binary directly.
    """
    command = ["cmd.exe", "/c", "npx", *args] if os.name == "nt" else ["npx", *args]
    return subprocess.run(
        command,
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )


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


def test_step2_compiled_bundle_serves_both_new_controls(cockpit: Any) -> None:
    """The production bundle served at ``/`` carries the Step 2 cockpit controls."""
    root = cockpit.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers.get("content-type", "")

    bundle = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "dist"
    assert (bundle / "index.html").is_file(), "production bundle was not rebuilt"
    assets = sorted((bundle / "assets").glob("*.js"))
    assert assets, "no compiled JS chunk"
    script = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in assets
    )

    for needle in (
        "22-Gate Go-Live Readiness Certificate",
        "RUN CROSS-PROCESS FORGE",
        "/api/v2/control/preflight",
        "/api/v2/control/supervisor/forge-hedge",
        "ready_for_live_cutover",
        "certificate_sha256",
    ):
        assert needle in script, f"missing cockpit control: {needle}"


def test_step2_frontend_lint_is_zero_and_bundle_is_served(cockpit: Any) -> None:
    """Biome must exit ``0`` with zero diagnostics and ``GET /`` serves it cleanly.

    The React 19 cockpit is the operator's only window into Ring 3, so the
    production bundle is a release artefact: it may not ship with a single
    lint error, warning or unprinted diagnostic, and the served ``index.html``
    must reference the on-disk rebuilt assets byte-for-byte.
    """
    completed = _run_frontend(
        ["biome", "lint", "src/", "--reporter=json", "--max-diagnostics=200"],
        timeout_s=300.0,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    assert completed.returncode == 0, output[-4000:]

    report = json.loads(completed.stdout)
    summary = report["summary"]
    assert summary["errors"] == 0, report["diagnostics"][:5]
    assert summary["warnings"] == 0, report["diagnostics"][:5]
    assert summary["diagnosticsNotPrinted"] == 0, summary
    assert report["diagnostics"] == [], report["diagnostics"][:5]
    assert summary["changed"] + summary["unchanged"] >= 40, summary

    bundle = FRONTEND / "dist"
    index_bytes = (bundle / "index.html").read_bytes()
    index_html = index_bytes.decode("utf-8")
    served = cockpit.get("/")
    assert served.status_code == 200
    assert "text/html" in served.headers.get("content-type", "")
    assert served.content == index_bytes

    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index_html)
    assert assets, "index.html references no compiled asset"
    for asset in assets:
        assert (bundle / asset.lstrip("/")).is_file(), f"missing rebuilt asset: {asset}"
        response = cockpit.get(asset)
        assert response.status_code == 200, asset
        assert response.content, f"empty served asset: {asset}"
