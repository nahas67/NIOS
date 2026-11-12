"""Phase 3 · Step 5 certification gates (final ``v3.0.0-PROD`` release).

Gate 1  OS & socket low-latency tuning auditor: ``audit_and_tune_os(apply=True)``
        plus ``scripts/tune_os_performance.py --check --json`` with ``0`` errors.
Gate 2  Zero-downtime Ring 0 key rotation: Fernet master keys + Ed25519 keypair,
        every stored LLM/broker credential re-encrypted, masks stable, historical
        signatures still verifying, no raw secret in any response or tracked file.
Gate 3  ``RUNBOOK.md`` completeness: SOPs 1-5 and every canonical ``/api/v2/*``
        endpoint documented.
Gate 4  Final full-platform inventory (17 suites), rebuilt cockpit bundle with
        the rotation control, and the annotated ``v3.0.0-PROD`` release tag.

Governing sections: ``sec_004`` (ring isolation + air-gap), ``sec_010`` /
``sec_013`` (latency budgets), ``sec_011`` (key custody), ``sec_016`` (control
plane surface).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TESTS_DIR = ROOT / "tests"
BUNDLE_DIR = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "dist"
TUNE_CLI = ROOT / "scripts" / "tune_os_performance.py"

#: Write-only fixtures.  The last four characters are the only visible part.
LLM_SECRET = "sk-nios-rotation-llm-4F2A"
BINANCE_KEY = "binance-api-key-9C71"
BINANCE_SECRET = "binance-api-secret-3D88"
DHAN_KEY = "dhan-api-key-7B42"
DHAN_SECRET = "dhan-api-secret-6E19"

CANONICAL_ENDPOINTS = (
    "/api/v2/cockpit/health",
    "/api/v2/cockpit/state",
    "/api/v2/cockpit/stream",
    "/api/v2/cockpit/formal-proofs",
    "/api/v2/control/brokers",
    "/api/v2/control/brokers/credentials",
    "/api/v2/control/brokers/testnet",
    "/api/v2/control/brokers/testnet-order",
    "/api/v2/control/feeds",
    "/api/v2/control/llms",
    "/api/v2/control/llms/probe",
    "/api/v2/control/killswitch",
    "/api/v2/control/rearm",
    "/api/v2/control/supervisor",
    "/api/v2/control/extensions",
    "/api/v2/control/vault/rotate-keys",
    "/api/v2/control/vault/status",
    "/api/v2/mcp/tools",
    "/api/v2/mcp/call",
    "/api/v2/metrics",
    "/api/v2/metrics/json",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def certified_runtime(tmp_path: Path):
    """Boot one real 4-ring runtime with an isolated Ring 0 key vault."""

    from core.orchestrator import NIOSCoreOrchestrator

    runtime = NIOSCoreOrchestrator(
        workspace=tmp_path / "runtime",
        key_vault_root=tmp_path / "keys",
        seed_fills=8,
        funding_micros=5_000_000_000_000,
        daily_loss_stop_micros=5_000_000_000,
    )
    try:
        yield runtime
    finally:
        runtime.close()


def _masked_venues(credentials: dict) -> dict[str, str]:
    return {
        venue["venue"]: venue["api_key_masked"]
        for silo in credentials.get("silos", {}).values()
        for venue in silo.get("venues", [])
        if venue.get("credential_configured")
    }


# ---------------------------------------------------------------------------
# Gate 1 — OS & socket low-latency tuning auditor
# ---------------------------------------------------------------------------


def test_gate1_os_and_socket_low_latency_auditor() -> None:
    from core.os_tuning import (
        LOW_LATENCY_RECV_BUFFER_BYTES,
        LOW_LATENCY_SEND_BUFFER_BYTES,
        audit_and_tune_os,
        configure_low_latency_socket,
    )

    report = audit_and_tune_os(apply=True)
    assert report["schema"] == "nios.os_tuning.v1"
    assert report["error_count"] == 0, report["errors"]
    assert report["ok"] is True
    assert report["platform"] in ("Windows", "Linux")
    assert report["apply_requested"] is True

    checks = {check["id"]: check for check in report["checks"]}
    for required in (
        "loopback_tcp_nodelay",
        "transport_conformance",
        "cpu_affinity",
        "shm_capacity",
        "shm_seq_lock_allocation",
    ):
        assert required in checks, f"missing OS tuning check {required}"
        assert checks[required]["status"] in ("APPLIED", "RECOMMENDED", "UNAVAILABLE")

    if report["platform"] == "Windows":
        assert "windows_timer_resolution" in checks
        assert checks["windows_timer_resolution"]["status"] == "APPLIED"
        assert checks["process_priority"]["status"] == "APPLIED"
    else:
        assert "linux_sysctl_profile" in checks
        assert "transparent_hugepage" in checks

    # The loopback probe proves the kernel accepted the options.
    assert report["socket"]["ok"] is True
    assert report["socket"]["tcp_nodelay"] == 1
    assert report["socket"]["so_keepalive"] == 1
    assert report["socket"]["recv_buffer_bytes"] > 0
    assert report["socket"]["send_buffer_bytes"] > 0

    # Both owned transports are wired to the same contract.
    transports = {entry["name"]: entry for entry in report["transports"]["entries"]}
    assert set(transports) == {"llm_http2", "ring1_websocket"}
    assert transports["llm_http2"]["low_latency_enforced"] is True
    assert transports["ring1_websocket"]["low_latency_enforced"] is True

    # Direct helper proof on an independent loopback pair.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname(), timeout=2.0)
        try:
            server, _address = listener.accept()
            try:
                for sock in (client, server):
                    applied = configure_low_latency_socket(sock)
                    assert applied["applied"] is True
                    assert applied["ok"] is True
                    assert applied["options"]["tcp_nodelay"] == 1
                    assert applied["options"]["so_keepalive"] == 1
                    # Windows reports the buffer it actually reserved; never
                    # assert equality, only that it is positive and bounded.
                    assert applied["options"]["so_rcvbuf"] > 0
                    assert applied["options"]["so_sndbuf"] > 0
                assert LOW_LATENCY_RECV_BUFFER_BYTES >= 65536
                assert LOW_LATENCY_SEND_BUFFER_BYTES >= 65536
            finally:
                server.close()
        finally:
            client.close()
    finally:
        listener.close()

    # CLI: --check --json must be pure JSON, exit 0 and report zero errors.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    process = subprocess.run(
        [sys.executable, str(TUNE_CLI), "--check", "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert process.returncode == 0, process.stderr
    payload = json.loads(process.stdout)
    assert payload["schema"] == "nios.os_tuning.v1"
    assert payload["error_count"] == 0
    assert payload["ok"] is True
    assert payload["apply_requested"] is False
    assert payload["applied_count"] + payload["recommended_count"] >= 5
    assert payload["socket"]["tcp_nodelay"] == 1


# ---------------------------------------------------------------------------
# Gate 2 — Zero-downtime Ring 0 key rotation
# ---------------------------------------------------------------------------


def test_gate2_zero_downtime_ring0_key_rotation(certified_runtime) -> None:
    from fastapi.testclient import TestClient

    from ring0_vault.silos.key_vault.broker_credentials import (
        BrokerCredentialVault,
    )

    runtime = certified_runtime
    client = TestClient(runtime.control_app())
    control = client.app.state.control
    vault = control.broker_vault
    assert vault is not None, "orchestrator must inject an isolated broker vault"

    # ---- store one LLM key and two venue credentials --------------------
    llm_response = client.post(
        "/api/v2/control/llms",
        json={
            "system": 1,
            "provider": "groq",
            "model": "llama-3.2-3b",
            "base_url": "https://api.groq.com",
            "api_key": LLM_SECRET,
        },
    )
    assert llm_response.status_code == 200, llm_response.text
    assert LLM_SECRET not in llm_response.text
    assert llm_response.json()["config"]["api_key_masked"] == "***4F2A"

    credential_responses = []
    for silo, venue, api_key, api_secret in (
        ("CRYPTO_BINANCE", "binance", BINANCE_KEY, BINANCE_SECRET),
        ("INDIA_ZERODHA_DHAN", "dhan", DHAN_KEY, DHAN_SECRET),
    ):
        response = client.post(
            "/api/v2/control/brokers/credentials",
            json={
                "silo": silo,
                "venue": venue,
                "api_key": api_key,
                "api_secret": api_secret,
                "sandbox": True,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["dry_run_verified"] is True
        assert api_key not in response.text
        assert api_secret not in response.text
        credential_responses.append(response)

    status_before = client.get("/api/v2/control/vault/status")
    assert status_before.status_code == 200
    before = status_before.json()
    assert before["key_version"] == 1
    assert before["raw_secrets_exposed"] is False
    assert before["llm_keys_stored"] >= 1
    assert before["broker_venues_stored"] >= 2
    fingerprint_before = before["ed25519_public_fingerprint"]
    assert len(fingerprint_before) == 16
    assert before["os_tuning"] is not None
    assert before["os_tuning"]["error_count"] == 0
    assert before["os_tuning"]["socket_tcp_nodelay"] == 1

    masks_before = _masked_venues(
        client.get("/api/v2/control/brokers/credentials").json()
    )
    assert masks_before["binance"] == "***9C71"
    assert masks_before["dhan"] == "***7B42"

    # ---- a real historical order signature under the old Ed25519 key ----
    probe = b"order|BTCUSDT|BUY|0.01|rotation-history-proof"
    historical_signature = vault.sign(probe)
    public_key_before = vault.public_key_hex
    fernet_key_before = vault.fernet_key_material
    llm_fernet_before = control.key_vault.key_material

    # ---- rotate ---------------------------------------------------------
    rotation = client.post(
        "/api/v2/control/vault/rotate-keys",
        json={"operator": "pytest.release", "reason": "step5 certification"},
    )
    assert rotation.status_code == 200, rotation.text
    report = rotation.json()

    assert report["status"] == "ROTATED"
    assert report["key_version"] == before["key_version"] + 1
    assert report["previous_key_version"] == before["key_version"]
    assert report["key_version_incremented"] is True
    assert report["fingerprint_changed"] is True
    assert report["ed25519_public_fingerprint"] != fingerprint_before
    assert len(report["ed25519_public_fingerprint"]) == 16
    assert report["reencrypted_llm_keys_count"] >= 1
    assert report["reencrypted_broker_venues_count"] >= 2
    assert set(report["broker_venues"]) >= {"binance", "dhan"}
    assert report["credentials_zero_loss"] is True
    assert report["dry_run_verified"] is True
    assert report["masks_unchanged"] is True
    assert report["fernet_roundtrip_ok"] is True
    assert report["historical_signature_verified"] is True
    assert report["previous_public_key_archived"] is True
    assert report["zero_downtime"] is True
    assert report["zero_loss"] is True
    assert report["raw_secrets_exposed"] is False
    assert report["os_tuning"]["error_count"] == 0

    # ---- status after rotation -----------------------------------------
    after = client.get("/api/v2/control/vault/status").json()
    assert after["key_version"] == report["key_version"]
    assert after["ed25519_public_fingerprint"] == report["ed25519_public_fingerprint"]
    assert after["reencrypted_llm_keys_count"] >= 1
    assert after["reencrypted_broker_venues_count"] >= 2
    assert after["rotation_pending_recovery"] is False
    assert after["write_only"] is True
    assert after["raw_secrets_exposed"] is False

    # ---- credentials decrypt cleanly under the new Fernet key -----------
    assert vault._raw_for_test_only("broker.binance.api_key") == BINANCE_KEY
    assert vault._raw_for_test_only("broker.binance.api_secret") == BINANCE_SECRET
    assert vault._raw_for_test_only("broker.dhan.api_key") == DHAN_KEY
    assert control.key_vault._raw_for_test_only("llm.system1") == LLM_SECRET
    assert vault.fernet_key_material != fernet_key_before
    assert control.key_vault.key_material != llm_fernet_before
    assert vault.public_key_hex != public_key_before

    masks_after = _masked_venues(
        client.get("/api/v2/control/brokers/credentials").json()
    )
    assert masks_after["binance"] == masks_before["binance"] == "***9C71"
    assert masks_after["dhan"] == masks_before["dhan"] == "***7B42"

    # ---- historical signatures still verify; new ones use the new key ---
    verifier = Path(vault.root) / "verifier_ring"
    archived_public = verifier / "ed25519_v1.pub"
    archived_metadata = json.loads(
        (verifier / "ed25519_v1.json").read_text(encoding="utf-8")
    )
    assert archived_public.exists()
    assert archived_metadata["fingerprint_sha256"] == fingerprint_before
    assert archived_metadata["read_only"] is True
    assert not os.access(archived_public, os.W_OK), "verifier ring must be read-only"

    assert BrokerCredentialVault.verify_with_public_key(
        probe, historical_signature, archived_metadata["public_key_hex"]
    ), "a pre-rotation order must still verify against the archived key"
    new_signature = vault.sign(probe)
    assert new_signature != historical_signature
    assert not BrokerCredentialVault.verify_with_public_key(
        probe, new_signature, archived_metadata["public_key_hex"]
    ), "new orders must not verify against the retired key"
    assert BrokerCredentialVault.verify_with_public_key(
        probe, new_signature, report["ed25519_public_key_hex"]
    )

    # ---- zero raw secret material in any response -----------------------
    transcript = "".join(
        response.text
        for response in (
            llm_response,
            *credential_responses,
            status_before,
            rotation,
            client.get("/api/v2/control/vault/status"),
            client.get("/api/v2/control/brokers/credentials"),
            client.get("/api/v2/cockpit/state"),
            client.get("/api/v2/cockpit/health"),
        )
    )
    for secret in (
        LLM_SECRET,
        BINANCE_KEY,
        BINANCE_SECRET,
        DHAN_KEY,
        DHAN_SECRET,
        vault.fernet_key_material.decode("ascii"),
        control.key_vault.key_material.decode("ascii"),
        vault.signer_seed.hex(),
    ):
        assert secret not in transcript, "a secret leaked into a control-plane response"
    assert "BEGIN" not in transcript

    # ---- zero raw secret material in any git-tracked file ---------------
    tracked = subprocess.run(
        ["git", "ls-files", "ring0_vault/silos/key_vault"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert tracked.returncode == 0, tracked.stderr
    for forbidden in (
        "fernet.key",
        "llm_keys.json",
        "broker_fernet.key",
        "broker_signer.key",
        "broker_credentials.json",
        "rotation.journal.json",
        "key_rotation_state.json",
    ):
        assert forbidden not in tracked.stdout, f"{forbidden} must never be git-tracked"


# ---------------------------------------------------------------------------
# Gate 3 — Production operator runbook & documentation completeness
# ---------------------------------------------------------------------------


def test_gate3_runbook_documents_all_sops_and_endpoints() -> None:
    runbook_path = ROOT / "RUNBOOK.md"
    assert runbook_path.exists(), "RUNBOOK.md is part of the release"
    runbook = runbook_path.read_text(encoding="utf-8")
    assert len(runbook) > 4000, "runbook must be operational, not a stub"

    for sop in range(1, 6):
        assert f"SOP {sop}" in runbook, f"missing SOP {sop}"

    for endpoint in CANONICAL_ENDPOINTS:
        assert endpoint in runbook, f"runbook omits {endpoint}"

    required_commands = (
        "start_nios.bat",
        "start_nios.sh",
        "python scripts/run_nios.py --multiprocess",
        "docker compose up --build",
        "python scripts/tune_os_performance.py --check",
        "python scripts/soak_test_harness.py",
        "python scripts/run_nios.py --smoke-test",
        "confirm_live=true",
        "LiveReconciler",
    )
    for command in required_commands:
        assert command in runbook, f"runbook omits {command}"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "RUNBOOK.md" in readme
    assert "vault/rotate-keys" in readme
    assert "tune_os_performance.py" in readme


# ---------------------------------------------------------------------------
# Gate 4 — Final full-platform regression inventory, bundle and release tag
# ---------------------------------------------------------------------------


def test_gate4_test_inventory_is_seventeen_files() -> None:
    test_files = sorted(TESTS_DIR.glob("test_*.py"))
    assert len(test_files) == 17, [path.name for path in test_files]
    assert "test_phase3_step5_os_tuning_key_rotation_and_runbook.py" in {
        path.name for path in test_files
    }


def test_gate4_rebuilt_bundle_contains_rotation_controls() -> None:
    index = BUNDLE_DIR / "index.html"
    assert index.exists(), "run `npm run build` in ring3_cockpit/ui/openalgo_shell"
    bundle = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted((BUNDLE_DIR / "assets").glob("*.js"))
    )
    assert bundle, "compiled JS bundle is empty"
    assert "Rotate Ring 0 Vault Keys" in bundle
    assert "OS LOW-LATENCY" in bundle
    assert "/api/v2/control/vault/rotate-keys" in bundle


def test_gate4_release_tag_is_annotated_when_present() -> None:
    result = subprocess.run(
        ["git", "tag", "-l", "--format=%(objecttype) %(contents:subject)", "v3.0.0-PROD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    if not line:
        pytest.skip("release tag is applied by the release step")
    object_type, _, subject = line.partition(" ")
    assert object_type == "tag", "v3.0.0-PROD must be annotated, not lightweight"
    assert subject, "the release tag must carry a message"
