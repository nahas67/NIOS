"""STEP 8 verification -- Nocturnal Alpha Forge, MCP bridge, plugins, skills, deploy.

Gates
-----
1. **Forge.**  A live champion at ``+0.30 bps`` (< ``+0.50``) is retired to
   ``SHADOW_RETIRED`` and its SHM lease is disarmed.  1,000 challenger genomes
   are evaluated under purged/embargoed K-fold CV + Benjamini-Hochberg FDR;
   only ``Deflated Sharpe >= 1.50`` with ``>= 30`` winning shadow fills is
   promoted.  A 1.80 DSR genome with 20 fills stays in shadow.
2. **Read-only MCP.**  All five tools execute through
   ``/api/v2/mcp/tools`` and ``/api/v2/mcp/call``; the air-gap audit proves zero
   order-signing and zero key-reading tools; JSON-RPC and SSE transports work.
3. **Skills & plugins.**  The four core ``SKILL.md`` files load progressively
   (frontmatter first, body on demand).  A registered factor pack starts in the
   shadow nursery at 0/30 and is a Forge challenger.
4. **Unified production server.**  ``GET /`` serves the compiled React 19
   ``index.html`` from ``dist/`` while ``/api/v2/*`` stays live;
   ``python scripts/run_nios.py --smoke-test`` exits 0; ``npm run build``
   succeeds.
5. Full regression is the complete pytest run (all Step 2-8 files).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.orchestrator import (
    NIOSCoreOrchestrator,
    PRIMARY_SILO,
    PRIMARY_SYMBOL,
)
from core.plugins import PluginKind, PluginManifest, PluginRegistry
from core.schemas import LeaseMode, System1Lease
from core.shm_ipc import LeaseTableSHM
from ring2_brain.forge.alpha_forge import (
    CHALLENGER,
    LIVE_CHAMPION,
    MIN_WINNING_SHADOW_FILLS,
    PROMOTION_DSR_FLOOR,
    RETIREMENT_FLOOR_BPS,
    SHADOW,
    SHADOW_RETIRED,
    ChampionRecord,
    ForgeDataset,
    NocturnalAlphaForge,
    PurgedKFoldCV,
    StrategyGenome,
    random_genomes,
)
from ring2_brain.skills.skill_registry import CORE_SKILLS, SkillRegistry, seed_core_skills

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"
DIST_ROOT = UI_ROOT / "dist"

MCP_TOOL_NAMES = {
    "nios_get_ibor_vaults",
    "nios_verify_grounding_hash",
    "nios_simulate_mirofish_cascade",
    "nios_scan_top_candidates",
    "nios_emergency_killswitch",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def orchestrator(tmp_path: Path):
    runtime = NIOSCoreOrchestrator(
        workspace=tmp_path / "runtime",
        key_vault_root=tmp_path / "keys",
        seed_fills=60,
        funding_micros=5_000_000_000_000,
        daily_loss_stop_micros=5_000_000_000,
    )
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture()
def client(orchestrator: NIOSCoreOrchestrator) -> TestClient:
    return TestClient(orchestrator.control_app())


def _one_hot(width: int, index: int = 0) -> np.ndarray:
    vector = np.zeros(width, dtype=np.float64)
    vector[index] = 1.0
    return vector


# ---------------------------------------------------------------------------
# GATE 1 -- Nocturnal Alpha Forge
# ---------------------------------------------------------------------------


def test_gate1_champion_below_50_bps_retires_and_disarms_shm_lease() -> None:
    lease_table = LeaseTableSHM(reset=True)
    symbol = "STEP8-DECAY.NS"
    try:
        now = time.monotonic_ns()
        lease_table.write_lease(
            System1Lease(
                symbol=symbol,
                mode=LeaseMode.LONG_ONLY,
                target_bps=8.0,
                stop_bps=4.0,
                expires_at_ns=now + 30_000_000_000,
                strategy_id_hash=0x1234,
            ),
            now_ns=now,
        )
        assert lease_table.read_lease(symbol, now).mode is LeaseMode.LONG_ONLY

        forge = NocturnalAlphaForge(
            lease_table=lease_table,
            dataset=ForgeDataset.synthesize(n_periods=96, n_factors=12),
        )
        champion = ChampionRecord(
            strategy_id="decayed-champion",
            silo=PRIMARY_SILO,
            symbols=(symbol,),
        )
        for _ in range(40):
            champion.record_fill(0.30)  # +0.30 bps rolling net edge
        forge.register_champion(champion)

        report = forge.audit(now_ns=time.time_ns())

        assert champion.status == SHADOW_RETIRED
        assert champion.retired_at_ns > 0
        assert "0.50" in champion.retire_reason
        assert report.retired, report.to_dict()
        assert report.retired[0]["strategy_id"] == "decayed-champion"
        assert report.retired[0]["disarmed_leases"] >= 1
        assert report.retired[0]["rolling_net_bps"] == pytest.approx(0.30)
        # The lease the champion owned is now disarmed in shared memory.
        after = lease_table.read_lease(symbol, now)
        assert after.mode is LeaseMode.DISARMED
        assert after.is_active(now) is False
    finally:
        lease_table.close()


def test_gate1_purged_kfold_removes_overlap_and_embargoes_boundary() -> None:
    cv = PurgedKFoldCV(n_folds=5, embargo_fraction=0.02, label_horizon=2)
    folds = cv.splits(120)
    assert len(folds) == 5
    embargo_size = int(round(120 * 0.02))
    for fold in folds:
        train = set(fold.train)
        test = set(fold.test)
        assert not (train & test)
        test_start, test_end = fold.test_bounds
        # No training label may overlap the test block.
        for index in fold.train:
            assert not (index <= test_end and index + 2 >= test_start)
        # No training observation may start inside the post-test embargo.
        for index in fold.train:
            assert not (test_end < index <= test_end + embargo_size)
    assert sum(fold.purged for fold in folds) > 0

    dataset = ForgeDataset.synthesize(n_periods=120, n_factors=12)
    result = cv.evaluate(dataset.factor_returns, _one_hot(12))
    # Contiguous folds cover every observation exactly once out of sample.
    assert result.oos_observations == 120
    assert result.observed_sharpe > 0.0
    assert len(result.fold_sharpes) == 5
    assert result.purged > 0


def test_gate1_thousand_genomes_promote_only_dsr_and_30_fills() -> None:
    dataset = ForgeDataset.synthesize(n_periods=120, n_factors=16)
    forge = NocturnalAlphaForge(dataset=dataset)

    genomes = random_genomes(998, n_factors=16, seed=8)
    strong = StrategyGenome(
        genome_id="genome-strong",
        weights=_one_hot(16),
        silo=PRIMARY_SILO,
        shadow_fills=MIN_WINNING_SHADOW_FILLS,
        winning_shadow_fills=MIN_WINNING_SHADOW_FILLS,
    )
    high_dsr_but_short = StrategyGenome(
        genome_id="genome-dsr180-20fills",
        weights=_one_hot(16),
        silo=PRIMARY_SILO,
        shadow_fills=20,
        winning_shadow_fills=20,
        deflated_sharpe=1.80,
        p_value=1e-9,
    )
    assert len(genomes) + 2 == 1_000
    forge.enqueue_genomes(genomes)
    forge.enqueue_challenger(strong)
    forge.enqueue_challenger(high_dsr_but_short)

    report = forge.audit()

    assert report.genomes_evaluated == 1_000
    assert report.fdr_rejections >= 1
    assert forge.get_challenger("genome-strong").status == LIVE_CHAMPION
    assert forge.get_champion("genome-strong") is not None
    assert forge.get_challenger("genome-strong").deflated_sharpe >= PROMOTION_DSR_FLOOR

    held = forge.get_challenger("genome-dsr180-20fills")
    assert held.status == SHADOW
    assert held.deflated_sharpe == pytest.approx(1.80)
    assert "WINNING_SHADOW_FILLS_20_BELOW_30" in held.hold_reason
    assert held.to_dict()["shadow_fills"] == 20

    promoted_ids = {row["genome_id"] for row in report.promoted}
    assert promoted_ids == {"genome-strong"}
    assert report.champions_after == 1
    assert report.held_count >= 1


def test_gate1_forge_schedule_is_next_utc_midnight() -> None:
    forge = NocturnalAlphaForge(
        dataset=ForgeDataset.synthesize(n_periods=64, n_factors=8)
    )
    noon = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
    next_ns = forge.next_run_ns(int(noon.timestamp() * 1e9))
    assert datetime.fromtimestamp(next_ns / 1e9, tz=timezone.utc) == datetime(
        2026, 11, 10, 0, 0, 0, tzinfo=timezone.utc
    )
    assert forge.seconds_until_next_run(int(noon.timestamp() * 1e9)) == pytest.approx(
        12 * 3600.0
    )
    assert RETIREMENT_FLOOR_BPS == 0.50


# ---------------------------------------------------------------------------
# GATE 2 -- Read-only air-gapped MCP bridge
# ---------------------------------------------------------------------------


def test_gate2_mcp_tools_are_exactly_five_and_air_gapped(client: TestClient) -> None:
    response = client.get("/api/v2/mcp/tools")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["count"] == 5
    assert {tool["name"] for tool in payload["tools"]} == MCP_TOOL_NAMES
    assert payload["air_gap"]["pass"] is True, payload["air_gap"]
    assert payload["air_gap"]["order_signing_tools"] == []
    assert payload["air_gap"]["key_reading_tools"] == []
    assert payload["air_gap"]["read_only_tools"] == 4
    assert payload["air_gap"]["safety_tools"] == 1
    assert set(payload["transports"]) >= {"http", "sse", "stdio", "json-rpc"}

    forbidden = (
        "place_order",
        "submit_order",
        "sign_order",
        "order_signing",
        "api_key",
        "private_key",
        "read_key",
        "withdraw",
        "transfer_funds",
    )
    for tool in payload["tools"]:
        haystack = json.dumps(tool).lower()
        for term in forbidden:
            assert term not in haystack, (tool["name"], term)


def test_gate2_all_five_mcp_tools_execute(
    client: TestClient, orchestrator: NIOSCoreOrchestrator
) -> None:
    # 1. Four-vault IBOR read across all silos.
    response = client.post(
        "/api/v2/mcp/call",
        json={"name": "nios_get_ibor_vaults", "arguments": {}},
    ).json()
    assert response["ok"] is True, response
    vaults = response["result"]["silos"]
    assert set(vaults) == {"CRYPTO_BINANCE", PRIMARY_SILO, "US_IBKR", "FX_MT5"}
    assert response["result"]["total_nav_micros"] > 0
    assert set(vaults[PRIMARY_SILO]["vaults"]) == {
        "ReserveYield",
        "LongTermCompounder",
        "TacticalSwing",
        "SpinalScalper",
    }

    # 2. SHA-256 grounding digest + thesis audit.
    now = time.time_ns()
    packet = orchestrator.grounding.commit(
        PRIMARY_SYMBOL,
        {"gross_bps": 18.0, "net_lb_bps": 6.3},
        valid_time_ns=now,
        knowledge_ts_ns=now,
        as_of_ns=now,
        silo=PRIMARY_SILO,
    )
    response = client.post(
        "/api/v2/mcp/call",
        json={
            "name": "nios_verify_grounding_hash",
            "arguments": {
                "thesis_text": "Gross edge is 18.0 bps and net lower bound is 6.3 bps.",
                "digest": packet.sha256_digest,
            },
        },
    ).json()
    assert response["ok"] is True, response
    assert response["result"]["verified"] is True
    assert response["result"]["report"]["claims_checked"] >= 2

    # 3. Deterministic five-archetype three-tick cascade.
    response = client.post(
        "/api/v2/mcp/call",
        json={
            "name": "nios_simulate_mirofish_cascade",
            "arguments": {"price_shock_bps": 55.0, "realized_vol_ratio": 1.8},
        },
    ).json()
    assert response["ok"] is True, response
    features = response["result"]["features"]
    assert isinstance(features["requires_ring1_halt"], bool)
    assert 0.0 <= features["cascade_fragility_index"] <= 1.0
    assert response["result"]["latency_ms"] >= 0.0

    # 4. Top-1% grounded scanner output.
    response = client.post(
        "/api/v2/mcp/call",
        json={
            "name": "nios_scan_top_candidates",
            "arguments": {"n_symbols": 400, "n_periods": 32, "top_k": 4},
        },
    ).json()
    assert response["ok"] is True, response
    scan = response["result"]
    assert scan["universe_size"] == 400
    assert scan["candidate_count"] == 4
    assert scan["top_fraction"] == pytest.approx(0.01)
    assert scan["candidates"][0]["sha256_digest"]

    # 5. One-way emergency kill switch (last: it halts the runtime).
    response = client.post(
        "/api/v2/mcp/call",
        json={
            "name": "nios_emergency_killswitch",
            "arguments": {"reason": "step8 mcp drill", "flatten": True, "confirm": True},
        },
    ).json()
    assert response["ok"] is True, response
    kill = response["result"]
    assert kill["triggered"] is True
    assert kill["status"] == "HALTED"
    assert kill["kill_latch_hot"] is True
    assert kill["within_budget"] is True
    assert orchestrator.kill_latch.is_halted is True


def test_gate2_jsonrpc_and_sse_transports(client: TestClient) -> None:
    initialized = client.post(
        "/api/v2/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    ).json()
    assert initialized["result"]["protocolVersion"] == "2024-11-05"
    assert initialized["result"]["serverInfo"]["name"] == "nios-ring3-mcp"

    listed = client.post(
        "/api/v2/mcp/rpc",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ).json()
    assert len(listed["result"]["tools"]) == 5

    called = client.post(
        "/api/v2/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "nios_get_ibor_vaults", "arguments": {}},
        },
    ).json()
    assert called["result"]["isError"] is False
    assert "CRYPTO_BINANCE" in called["result"]["content"][0]["text"]

    sse = client.get("/api/v2/mcp/sse")
    assert sse.status_code == 200
    assert "text/event-stream" in sse.headers["content-type"]
    assert "event: endpoint" in sse.text
    assert "nios_get_ibor_vaults" in sse.text


# ---------------------------------------------------------------------------
# GATE 3 -- Skills + hot-swap plugins
# ---------------------------------------------------------------------------


def test_gate3_progressive_skill_loading_of_four_core_skills(tmp_path: Path) -> None:
    seed_core_skills(tmp_path)
    registry = SkillRegistry(tmp_path)
    catalog = registry.describe()
    assert catalog["count"] == 4
    assert set(CORE_SKILLS) == {skill["name"] for skill in catalog["skills"]}
    assert catalog["progressive"] is True
    assert catalog["bodies_loaded"] == 0
    assert catalog["token_overhead"] < 2_000

    body = registry.load("india_fno_stt_gamma")
    assert "5.5 bps" in body
    assert "dealer gamma" in body.lower()
    after = registry.describe()
    assert after["bodies_loaded"] == 1
    assert after["loaded_tokens_estimate"] > 0

    india = registry.for_silo("INDIA_ZERODHA_DHAN")
    assert [item["name"] for item in india] == ["india_fno_stt_gamma"]
    assert registry.describe()["bodies_loaded"] == 1

    registry.set_enabled("india_fno_stt_gamma", False)
    assert registry.get("india_fno_stt_gamma").enabled is False
    assert registry.for_silo("INDIA_ZERODHA_DHAN") == []


def test_gate3_plugin_starts_in_shadow_and_enters_forge_as_challenger() -> None:
    dataset = ForgeDataset.synthesize(n_periods=96, n_factors=12)
    forge = NocturnalAlphaForge(dataset=dataset)
    registry = PluginRegistry(forge=forge)

    record = registry.register(
        PluginManifest(
            name="custom_momentum_pack",
            kind=PluginKind.FACTOR_PACK,
            version="0.1.0",
            description="Operator-supplied momentum factor pack",
            factor_weights={"alpha_001": 0.7, "qlib_roc5": 0.3},
            ring="ring2",
        )
    )
    assert record.shadow_fills == 0
    assert record.winning_shadow_fills == 0
    assert record.graduated is False
    assert record.status.value == "SHADOW"

    challenger = forge.get_challenger("plugin:custom_momentum_pack")
    assert challenger is not None
    assert challenger.status == CHALLENGER

    for _ in range(29):
        registry.record_shadow_fill("custom_momentum_pack", 1.2)
    assert registry.get("custom_momentum_pack").graduated is False
    registry.record_shadow_fill("custom_momentum_pack", 1.2)
    assert registry.get("custom_momentum_pack").graduated is True
    assert registry.get("custom_momentum_pack").shadow_fills == 30

    with pytest.raises(ValueError):
        PluginManifest(
            name="illegal-broker",
            kind=PluginKind.BROKER_SILO,
            ring="ring2",
        )


# ---------------------------------------------------------------------------
# GATE 4 -- Unified production server, static UI, smoke test, npm build
# ---------------------------------------------------------------------------


def test_gate4_unified_app_serves_api_and_react_bundle(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200, index.text
    assert "root" in index.text.lower()
    assert "/assets/" in index.text
    assert (DIST_ROOT / "index.html").exists()

    health = client.get("/api/v2/cockpit/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    state = client.get("/api/v2/cockpit/state").json()
    assert "extensions" in state
    assert state["extensions"]["counts"]["skills"] == 4
    assert state["extensions"]["counts"]["mcp_tools"] == 5
    assert state["extensions"]["air_gap"]["mcp_air_gap_pass"] is True


def test_gate4_extensions_toggle_and_manual_forge_audit(client: TestClient) -> None:
    payload = client.get("/api/v2/control/extensions").json()
    assert payload["counts"] == {
        "skills": 4,
        "plugins": 0,
        "mcp_tools": 5,
        "champions": 0,
        "challengers": 0,
    }

    toggled = client.post(
        "/api/v2/control/extensions",
        json={"target": "skill", "name": "fx_macro_blackout", "enabled": False},
    ).json()
    assert toggled["item"]["enabled"] is False
    assert toggled["zero_restart"] is True
    assert (
        client.get("/api/v2/control/extensions").json()["counts"]["skills"] == 4
    )

    disabled = client.post(
        "/api/v2/control/extensions",
        json={"target": "mcp_tool", "name": "nios_scan_top_candidates", "enabled": False},
    ).json()
    assert disabled["item"]["enabled"] is False
    denied = client.post(
        "/api/v2/mcp/call",
        json={"name": "nios_scan_top_candidates", "arguments": {}},
    )
    assert denied.status_code == 403

    audit = client.post(
        "/api/v2/control/extensions",
        json={"target": "forge", "action": "run_audit"},
    ).json()
    assert audit["updated"] == ["forge.audit"]
    assert audit["report"]["next_run_ns"] > 0
    assert audit["report"]["thresholds"]["retirement_floor_bps"] == 0.50
    assert audit["report"]["thresholds"]["promotion_dsr_floor"] == 1.50


def test_gate4_smoke_script_exits_zero() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_nios.py"), "--smoke-test"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined[-4000:]
    assert "SMOKE TEST PASSED" in completed.stdout, combined[-4000:]
    assert "Ring 0 sovereign vault / IBOR" in completed.stdout
    assert "Ring 3 read-only MCP bridge" in completed.stdout
    assert "Unified server static React UI" in completed.stdout


def test_gate4_npm_build_succeeds() -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        pytest.skip("npm is not installed in this environment")
    completed = subprocess.run(
        (
            ["cmd", "/c", "npm", "run", "build"]
            if os.name == "nt"
            else ["npm", "run", "build"]
        ),
        cwd=str(UI_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined[-4000:]
    assert (DIST_ROOT / "index.html").exists()
    assets = list((DIST_ROOT / "assets").glob("*.js"))
    assert assets, "vite build produced no JavaScript asset"
