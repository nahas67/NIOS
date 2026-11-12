"""Phase 3 · Step 4 certification gates.

Gate 1  Prometheus / OpenMetrics format, scrape, and JSON telemetry parity.
Gate 2  Accelerated soak + memory-leak audit (2,000 cycles, 4 silos).
Gate 3  Chaos SIGKILL hard-crash recovery (outbox voiding, double-entry
        conservation, stale-lease fail-closed).
Gate 4  Full regression inventory (17 suites), rebuilt cockpit bundle, and the
        annotated ``v3.0.0-PROD`` release tag.

Governing sections: ``sec_004`` (ring isolation + heartbeat SHM), ``sec_009``
(two-phase outbox + 128-bit IBOR), ``sec_012`` (deterministic fault injection),
``sec_016`` (control plane surface).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BUNDLE_DIR = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "dist"

SAMPLE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(\{.*\})?"
    r" (\S+)$"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def certified_client(tmp_path_factory: pytest.TempPathFactory):
    """Boot one real 4-ring runtime and return a TestClient for the unified app."""
    from fastapi.testclient import TestClient

    from core.orchestrator import NIOSCoreOrchestrator

    workspace = tmp_path_factory.mktemp("nios-phase3-step4")
    runtime = NIOSCoreOrchestrator(
        workspace=workspace / "runtime",
        key_vault_root=workspace / "keys",
        seed_fills=60,
        funding_micros=5_000_000_000_000,
        daily_loss_stop_micros=5_000_000_000,
    )
    try:
        yield TestClient(runtime.control_app()), runtime
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# Gate 1 — Prometheus metrics format, scrape, and JSON telemetry
# ---------------------------------------------------------------------------


def _parse_exposition(body: str) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Return ``({family: type}, [(family, labels, value)])`` from the text body."""
    types: dict[str, str] = {}
    samples: list[tuple[str, str, str]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "TYPE":
                types[parts[2]] = parts[3]
            continue
        match = SAMPLE_RE.match(line)
        assert match is not None, f"malformed Prometheus sample: {line!r}"
        name, labels = match.group(1), match.group(2) or ""
        value = match.group(3)
        family = name
        for suffix in ("_bucket", "_sum", "_count"):
            if name.endswith(suffix):
                family = name[: -len(suffix)]
                break
        samples.append((family, labels, value))
        float(value.replace("+Inf", "inf").replace("-Inf", "-inf"))
    return types, samples


def test_gate1_prometheus_exposition_format_and_required_families(
    certified_client,
) -> None:
    client, runtime = certified_client

    # Feed Ring 1 real ticks so the histogram carries observations, not zeros.
    from core.process_supervisor import make_flow_tick

    ticks = [
        make_flow_tick("BTC-USDT", last_price=64_250.0) for _ in range(8)
    ]
    runtime.engine.on_tick_batch(ticks)

    response = client.get("/api/v2/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert body.endswith("\n")

    types, samples = _parse_exposition(body)
    families = {family for family, _labels, _value in samples}

    required = {
        "nios_tick_latency_seconds",
        "nios_ring_heartbeat_age_ms",
        "nios_conformal_q98_bps",
        "nios_outbox_reservations_total",
        "nios_token_spend_usd_total",
        "nios_token_budget_cap_usd",
        "nios_active_leases",
        "nios_silo_nav_micros",
    }
    missing = required - families
    assert not missing, f"missing metric families: {sorted(missing)}"

    assert types["nios_tick_latency_seconds"] == "histogram"
    assert types["nios_outbox_reservations_total"] == "counter"
    assert types["nios_ring_heartbeat_age_ms"] == "gauge"
    for family in required:
        assert family in types, f"{family} has samples but no # TYPE line"

    # Histogram integrity: cumulative buckets, +Inf == _count, le ascending.
    bucket_values: list[tuple[float, float]] = []
    for family, labels, value in samples:
        if family != "nios_tick_latency_seconds":
            continue
        if labels.startswith("{le="):
            le = float(labels.split('"')[1].replace("+Inf", "inf"))
            bucket_values.append((le, float(value)))
    assert bucket_values, "histogram exported no buckets"
    bucket_values.sort(key=lambda item: item[0])
    counts = [count for _le, count in bucket_values]
    assert counts == sorted(counts), "histogram buckets must be cumulative"
    assert counts[-1] > 0
    count_match = re.search(
        r"^nios_tick_latency_seconds_count (\S+)$", body, re.MULTILINE
    )
    assert count_match is not None
    assert float(count_match.group(1)) == counts[-1], (
        "+Inf bucket must equal the exported sample count"
    )

    quantiles = {
        labels: float(value)
        for family, labels, value in samples
        if family == "nios_tick_latency_quantiles_seconds"
    }
    assert set(quantiles) == {
        '{quantile="0.5"}',
        '{quantile="0.9"}',
        '{quantile="0.99"}',
        '{quantile="0.999"}',
    }
    p50 = quantiles['{quantile="0.5"}']
    p99 = quantiles['{quantile="0.99"}']
    assert 0.0 <= p50 <= p99

    # The /metrics alias must serve the identical exposition.
    alias = client.get("/metrics")
    assert alias.status_code == 200
    assert "nios_active_leases" in alias.text
    assert alias.text.splitlines()[0].startswith("# HELP nios_build_info")

    # JSON telemetry parity for the cockpit chip.
    payload = client.get("/api/v2/metrics/json").json()
    assert payload["version"] == "3.0.0-PROD"
    assert payload["document_id"] == "NIOS-ARC-2026-V3"
    assert set(payload["tick_latency"]["quantiles"]) == {"p50", "p90", "p99", "p99_9"}
    assert payload["tick_latency"]["count"] >= 1, (
        "the Ring 1 batch tap must record at least one sample"
    )
    assert len(payload["rings"]) == 4
    assert [row["ring"] for row in payload["rings"]] == [0, 1, 2, 3]
    assert len(payload["silos"]) == 4
    for silo in payload["silos"]:
        assert set(silo["vaults"]) == {
            "ReserveYield",
            "LongTermCompounder",
            "TacticalSwing",
            "SpinalScalper",
        }
        assert silo["nav_total_micros"] == sum(silo["vaults"].values())
    assert payload["outbox"]["pending"] >= 0
    assert payload["conformal"]["strategy"] == "spinal-scalper-v1"
    assert payload["tokens"]["cap_usd"] > 0
    assert payload["exposition"]["prometheus"] == "/api/v2/metrics"


# ---------------------------------------------------------------------------
# Gate 2 — accelerated soak + memory-leak audit
# ---------------------------------------------------------------------------


def test_gate2_accelerated_soak_memory_leak_audit() -> None:
    from scripts.soak_test_harness import run_soak

    report = run_soak(cycles=2_000, audit_memory=True)

    assert report.errors == []
    assert report.ticks == 2000 * 4 + min(50, 2000) * 4
    assert report.shm_bytes_constant, "shared-memory footprint grew"
    assert report.shm_bytes_start > 0
    assert report.heap_growth_mb < 0.005, (
        f"heap leak {report.heap_growth_mb:.4f} MB exceeds 0.005 MB"
    )
    assert report.heap_slope_kb_per_cycle < 0.001, (
        f"linear leak slope {report.heap_slope_kb_per_cycle:.6f} kB/cycle"
    )
    assert report.handle_leaks == 0
    assert report.disconnects > 0
    assert report.lag_spikes > 0
    assert report.rollovers > 0
    assert report.reservations > 0
    assert report.passed


# ---------------------------------------------------------------------------
# Gate 3 — chaos SIGKILL hard-crash recovery
# ---------------------------------------------------------------------------


def test_gate3_chaos_sigkill_outbox_and_ledger_recovery(tmp_path) -> None:
    from core.chaos_recovery import ChaosCrashSimulator, CrashTarget

    verdict = ChaosCrashSimulator(
        tmp_path / "crash-outbox",
        target=CrashTarget.OUTBOX_RESERVATION,
        iterations=100,
        hold_ms=400.0,
        seed=4242,
    ).run()

    assert verdict.killed, verdict.notes
    assert verdict.inflight_observed
    assert verdict.returncode not in (0, None), "child must die ungracefully"
    assert verdict.recovery is not None
    recovery = verdict.recovery

    # NoOrphanedOrders: every in-flight reservation was voided on reboot.
    assert recovery.pending_transfers_before >= 1
    assert recovery.voided_during_recovery >= 1
    assert recovery.reservations_pending_after == 0
    assert recovery.pending_transfers_after == 0
    assert recovery.orphaned_orders == 0

    # AtMostOnceExecution + double-entry conservation.
    assert recovery.duplicate_transfer_ids == 0
    assert recovery.debit_total_micros == recovery.credit_total_micros
    assert recovery.debit_total_micros > 0
    assert recovery.invariant_holds
    assert verdict.passed


def test_gate3_chaos_sigkill_lease_and_tick_recovery(tmp_path) -> None:
    from core.chaos_recovery import (
        ChaosCrashSimulator,
        CrashTarget,
        audit_lease_reboot,
    )

    lease = audit_lease_reboot()
    assert lease.stale_lease_armed
    assert lease.swept >= 1
    assert lease.active_after_reboot == 0
    assert lease.mode_after_reboot == 0  # LeaseMode.DISARMED
    assert lease.passed

    for target in (CrashTarget.RING1_TICK, CrashTarget.RING2_LEASE_WRITE):
        verdict = ChaosCrashSimulator(
            tmp_path / f"crash-{target.value}",
            target=target,
            iterations=200,
            hold_ms=120.0,
            seed=7,
        ).run()
        assert verdict.killed, (target, verdict.notes, verdict.child_stderr)
        assert verdict.passed, (target, verdict.notes)
        if verdict.recovery is not None:
            assert verdict.recovery.debit_total_micros == (
                verdict.recovery.credit_total_micros
            )
        if verdict.lease_reboot is not None:
            assert verdict.lease_reboot.active_after_reboot == 0


# ---------------------------------------------------------------------------
# Gate 4 — inventory, rebuilt cockpit bundle, annotated release tag
# ---------------------------------------------------------------------------


def test_gate4_test_inventory_is_seventeen_files() -> None:
    test_files = sorted(path.name for path in (ROOT / "tests").glob("test_*.py"))
    assert len(test_files) == 17, test_files
    for expected in (
        "test_phase3_step1_multiprocess_and_live_daemons.py",
        "test_phase3_step2_live_cutover_and_cockpit.py",
        "test_phase3_step3_testnet_supervisor_and_security.py",
        "test_phase3_step4_soak_metrics_and_chaos.py",
    ):
        assert expected in test_files


def test_gate4_cockpit_bundle_exposes_metrics_telemetry() -> None:
    index = BUNDLE_DIR / "index.html"
    assert index.exists()
    assets = sorted((BUNDLE_DIR / "assets").glob("*.js"))
    assert assets, "no compiled JS bundle"
    bundle = assets[-1].read_text(encoding="utf-8")
    for needle in (
        "Prometheus / OpenTelemetry telemetry",
        "TELEMETRY LIVE",
        "/api/v2/metrics",
        "/api/v2/metrics/json",
    ):
        assert needle in bundle, f"bundle missing {needle!r}"


def test_gate4_release_tag_is_annotated_when_present() -> None:
    result = subprocess.run(
        ["git", "tag", "-l", "--format=%(objecttype) %(contents:subject)", "v3.0.0-PROD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout.strip()
    if not output:
        assert "v3.0.0-PROD" in (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        return
    object_type, _, subject = output.partition(" ")
    assert object_type == "tag", "v3.0.0-PROD must be annotated, not lightweight"
    assert subject
