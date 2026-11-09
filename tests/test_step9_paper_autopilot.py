"""STEP 9 verification -- LIVE PAPER-AUTOPILOT stream & version alignment.

Gates
-----
1. **Version alignment (``sec_001``).**  ``/api/v2/cockpit/health`` and
   ``/api/v2/cockpit/state`` report ``version = 3.0.0-PROD`` and
   ``document_id = NIOS-ARC-2026-V3``.
2. **Kill switch + re-arm without restart.**  ``POST /control/killswitch``
   drops the runtime to ``HALTED``; ``POST /control/rearm`` clears the
   ``GlobalKillLatch``, unfreezes the four silos and restores ``ARMED``.
3. **Paper-shadow autopilot.**  A burst of ``autopilot_tick`` rounds warms the
   strategy through 30 L2 shadow fills, graduates it, pushes proof cards onto
   the SSE stream and sweeps the 35% compounder / 15% reserve ratchet in every
   silo whose cost floor allows the edge (``sec_016``, ``sec_018`` Law 4/5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.orchestrator import ALL_SILOS, NIOSCoreOrchestrator
from core.schemas import VaultName

EXPECTED_VERSION = "3.0.0-PROD"
EXPECTED_DOCUMENT_ID = "NIOS-ARC-2026-V3"


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


def _vaults(runtime: NIOSCoreOrchestrator) -> dict[str, dict[str, int]]:
    return {
        silo: {
            vault.value: int(runtime.ledger.balance(silo, vault)) for vault in VaultName
        }
        for silo in ALL_SILOS
    }


def test_autopilot_rearm_and_version_3_0_0_prod(
    orchestrator: NIOSCoreOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The lifespan autopilot is exercised through explicit tick bursts so the
    # assertion is deterministic; the real server runs it continuously.
    monkeypatch.setenv("NIOS_AUTOPILOT", "0")
    app = orchestrator.control_app()

    with TestClient(app) as client:
        # ---- 1. version alignment (sec_001) --------------------------------
        health = client.get("/api/v2/cockpit/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert health.json()["status"] == "ARMED"
        assert health.json()["version"] == EXPECTED_VERSION
        assert health.json()["document_id"] == EXPECTED_DOCUMENT_ID

        state = client.get("/api/v2/cockpit/state").json()
        assert state["version"] == EXPECTED_VERSION
        assert state["document_id"] == EXPECTED_DOCUMENT_ID
        assert state["system"]["status"] == "ARMED"
        assert state["autopilot"]["mode"] == "PAPER"
        assert state["autopilot"]["interval_ms"] == 400

        # ---- 2. kill switch -> HALTED -> re-arm -> ARMED -------------------
        kill = client.post(
            "/api/v2/control/killswitch",
            json={"reason": "step9 gate", "flatten": True, "confirm": True},
        )
        assert kill.status_code == 200
        assert kill.json()["status"] == "HALTED"
        assert client.get("/api/v2/cockpit/health").json()["status"] == "HALTED"

        # The autopilot pauses immediately while the latch is hot.
        paused = orchestrator.autopilot_tick()
        assert paused["status"] == "HALTED"
        assert paused["paused"] is True
        assert paused["ticks"] == 0

        rearm = client.post(
            "/api/v2/control/rearm", json={"reason": "step9 gate re-arm"}
        )
        assert rearm.status_code == 200
        body = rearm.json()
        assert body["status"] == "ARMED"
        assert body["kill_latch_hot"] is False
        assert sorted(body["rearmed_silos"]) == sorted(ALL_SILOS)
        assert body["autopilot_resumes"] is True

        restored = client.get("/api/v2/cockpit/state").json()
        assert restored["system"]["status"] == "ARMED"
        assert restored["kill_switch"] is None

        # ---- 3. paper-shadow autopilot burst ------------------------------
        vaults_before = _vaults(orchestrator)
        tick = None
        for _ in range(30):
            tick = orchestrator.autopilot_tick()

        assert tick is not None
        assert tick["status"] == "ARMED"
        assert tick["paused"] is False
        assert tick["ticks"] == len(ALL_SILOS)
        assert tick["total_ticks"] == 30 * len(ALL_SILOS)
        assert tick["shadow_fills"] >= 30
        assert tick["graduated"] is True
        assert tick["live_fills"] > 0

        # 30 L2 shadow fills graduated the strategy in the twin.
        assert orchestrator.twin.shadow_fill_count(orchestrator.strategy_id) >= 30
        assert orchestrator.twin.is_graduated(orchestrator.strategy_id) is True

        # Every silo still exposes the exact four-vault IBOR shape.
        live_state = client.get("/api/v2/cockpit/state").json()
        for silo in ALL_SILOS:
            vaults = live_state["silos"][silo]["vaults"]
            for vault in VaultName:
                assert vault.value in vaults
            assert vaults["nav_micros"] > 0

        # Maker-Peg paper fills swept the 35% compounder / 15% reserve ratchet.
        vaults_after = _vaults(orchestrator)
        ratcheted = []
        for silo in ALL_SILOS:
            delta = {
                vault.value: vaults_after[silo][vault.value]
                - vaults_before[silo][vault.value]
                for vault in VaultName
            }
            if delta[VaultName.LONG_TERM_COMPOUNDER.value] > 0:
                ratcheted.append(silo)
                assert delta[VaultName.RESERVE_YIELD.value] > 0
                assert delta[VaultName.TACTICAL_SWING.value] == 0
                # reserve is 15% and compounder is 35% of the same realised
                # profit, so the compounder leg must dominate but stay < 3x.
                reserve = delta[VaultName.RESERVE_YIELD.value]
                compounder = delta[VaultName.LONG_TERM_COMPOUNDER.value]
                assert reserve < compounder < reserve * 3
        assert ratcheted, "the autopilot produced no in-silo ratchet sweep"

        # ---- 4. live SSE proof cards --------------------------------------
        cards = live_state["cards"]
        assert cards, "the autopilot published no cockpit cards"
        assert any(card["proof_line"] for card in cards)
        assert any(
            card["mode_badge"] in ("LIVE CAPITAL", "SHADOW WARMUP")
            for card in cards
        )
        assert any(card["mode_badge"] == "LIVE CAPITAL" for card in cards)
        assert live_state["autopilot"]["rounds"] == 30
        assert live_state["autopilot"]["graduated"] is True

        stream = client.get("/api/v2/cockpit/stream?frames=1")
        assert stream.status_code == 200
        payloads = [
            json.loads(line[len("data: "):])
            for line in stream.text.splitlines()
            if line.startswith("data: ")
        ]
        assert payloads, "the SSE stream returned no cockpit frame"
        frame = payloads[0]
        assert frame["type"] == "cockpit_frame"
        assert frame["system"]["status"] == "ARMED"
        assert frame["cards"], "the SSE frame carried no proof cards"
        assert any(card["proof_line"] for card in frame["cards"])
