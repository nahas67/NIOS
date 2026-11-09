"""STEP 7 verification -- Ring 3 Glass Cockpit + full 4-ring runtime.

Gates
-----
1. **Step 6 gap closure.**  Zerodha probes READY alongside Dhan/Angel/Upstox;
   cumulative realised losses trip ``DAILY_LOSS_LIMIT_BREACHED`` and lock the
   silo automatically (``sec_011`` Tier 3).
2. **Control plane + write-only credentials.**  Broker hot-swap with zero
   restart; ``POST /control/llms`` stores the encrypted key in Ring 0 and only
   ever returns ``***9A4F`` (``sec_011``, ``sec_016``).
3. **<1.0 ms global kill switch.**  Latch, all leases, open Maker-Peg orders,
   pending outbox reservations, and ADR-009 flatten in one measured window.
4. **SSE cockpit telemetry.**  ``/state`` and ``/stream`` emit the exact
   ``sec_016`` proof line, 99.1% abstention meter and 4-vault NAV per silo.
5. **Full 4-ring lifecycle.**  Raw dual-vendor ticks -> 30 shadow fills ->
   trade #31 -> conformal 98% -> two-phase outbox -> Ed25519 -> Maker-Peg fill
   -> 35/15 ratchet -> Ring 3 cockpit card.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.nats_bus import STREAM_RING3_COCKPIT
from core.orchestrator import (
    DISPLAY_SILO,
    NIOSCoreOrchestrator,
    PRIMARY_SILO,
    PRIMARY_SYMBOL,
)
from core.schemas import PlainEnglishCockpitCard, UnsignedProposal, VaultName
from ring0_vault.ibor.ledger_outbox import OutboxState
from ring0_vault.kernel.firewalls_and_oms import FirewallStage, TradingState
from ring0_vault.silos.silo_registry import probe_donor_adapters

SECRET = "sk-live-secret-12349A4F"
MASKED = "***9A4F"


def make_proposal(
    proposal_id: str = "step7-gate",
    *,
    silo: str = PRIMARY_SILO,
    symbol: str = PRIMARY_SYMBOL,
    qty: float = 10.0,
    price: float = 100.0,
    gross_bps: float = 18.0,
    spread_bps: float = 1.0,
) -> UnsignedProposal:
    return UnsignedProposal(
        proposal_id=proposal_id,
        strategy_id="spinal-scalper-v1",
        silo=silo,
        vault=VaultName.SPINAL_SCALPER,
        symbol=symbol,
        side="BUY",
        qty=qty,
        limit_price=price,
        expected_gross_bps=gross_bps,
        spread_bps=spread_bps,
        grounding_hash="a" * 64,
        created_at_ns=0,
    )


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


# ---------------------------------------------------------------------------
# GATE 1 -- Step 6 gaps
# ---------------------------------------------------------------------------


def test_gate1_zerodha_and_all_four_india_adapters_are_ready() -> None:
    probes = probe_donor_adapters(refresh=True)
    by_name = {probe.name: probe for probe in probes}
    for name in ("dhan", "zerodha", "angel", "upstox"):
        assert by_name[name].harvested is True
        assert by_name[name].importable is True
        assert by_name[name].status == "READY"

    registry = __import__(
        "ring0_vault.silos.silo_registry", fromlist=["MarketSiloRegistry"]
    ).MarketSiloRegistry(probes=probes)
    india = registry.get("INDIA_ZERODHA_DHAN")
    assert {probe.name for probe in india.ready_adapters} == {
        "dhan",
        "zerodha",
        "angel",
        "upstox",
    }
    assert india.venue_ready is True


def test_gate1_daily_loss_stop_locks_silo_automatically(
    orchestrator: NIOSCoreOrchestrator,
) -> None:
    firewall = orchestrator.firewall
    silo = PRIMARY_SILO
    assert firewall.realized_pnl(silo) == 0
    assert firewall.trading_state(silo) is TradingState.ACTIVE

    # Three cumulative settled losses; none alone breaches the 5,000,000,000
    # micro stop, the third does.
    assert firewall.record_realized_pnl(silo, -2_000_000_000) == -2_000_000_000
    assert firewall.daily_loss_exceeded(silo) is False
    assert firewall.record_realized_pnl(silo, -2_000_000_000) == -4_000_000_000
    assert firewall.daily_loss_exceeded(silo) is False
    total = firewall.record_realized_pnl(silo, -1_000_000_001)
    assert total == -5_000_000_001
    assert firewall.daily_loss_exceeded(silo) is True
    assert firewall.trading_state(silo) is TradingState.HALTED
    assert orchestrator.guard.is_frozen(silo) is True
    assert "DAILY_LOSS_LIMIT_BREACHED" in orchestrator.guard.reason(silo)

    verdict = firewall.evaluate(
        make_proposal("after-loss"),
        now_ns=time.monotonic_ns(),
        cost_floor=orchestrator.control.cost_floor(silo),
    )
    assert verdict.accepted is False
    assert verdict.stage_reached is FirewallStage.RISK_LIMITS
    assert "DAILY_LOSS_LIMIT_BREACHED" in verdict.reason


# ---------------------------------------------------------------------------
# GATE 2 -- control plane + masked write-only credentials
# ---------------------------------------------------------------------------


def test_gate2_broker_hot_swap_with_zero_restart(
    orchestrator: NIOSCoreOrchestrator, client: TestClient
) -> None:
    response = client.post(
        "/api/v2/control/brokers",
        json={
            "silo": PRIMARY_SILO,
            "mode": "PAPER",
            "maker_fee_bps": 0.7,
            "taker_fee_bps": 0.9,
            "statutory_tax_bps": 5.5,
            "spread_bps": 1.0,
            "max_daily_loss_micros": 123_456_789,
            "allocation": {
                "ReserveYield": 0.15,
                "LongTermCompounder": 0.35,
                "TacticalSwing": 0.0,
                "SpinalScalper": 0.50,
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["zero_restart"] is True
    assert set(body["updated"]) == {
        "mode",
        "maker_fee_bps",
        "taker_fee_bps",
        "statutory_tax_bps",
        "spread_bps",
        "max_daily_loss_micros",
        "allocation",
    }
    broker = body["broker"]
    assert broker["cost_floor"]["maker_fee_bps"] == pytest.approx(0.7)
    assert broker["cost_floor"]["taker_fee_bps"] == pytest.approx(0.9)
    assert broker["cost_floor"]["statutory_tax_bps"] == pytest.approx(5.5)
    assert broker["max_daily_loss_micros"] == 123_456_789
    assert broker["allocation"]["LongTermCompounder"] == pytest.approx(0.35)

    # The live firewall mirrors the hot-swapped stop and the GET state agrees.
    assert orchestrator.firewall.daily_loss_stops[PRIMARY_SILO] == 123_456_789
    state = client.get("/api/v2/cockpit/state").json()
    assert (
        state["silos"][PRIMARY_SILO]["max_daily_loss_micros"] == 123_456_789
    )
    assert state["silos"][PRIMARY_SILO]["cost_floor"]["spread_bps"] == pytest.approx(
        1.0
    )

    # LIVE requires an explicit human confirmation (sec_011 rule 11).
    denied = client.post(
        "/api/v2/control/brokers",
        json={"silo": PRIMARY_SILO, "mode": "LIVE"},
    )
    assert denied.status_code == 403
    allowed = client.post(
        "/api/v2/control/brokers",
        json={
            "silo": PRIMARY_SILO,
            "mode": "LIVE",
            "confirm_live": True,
            "operator": "step7-test",
        },
    )
    assert allowed.status_code == 200
    assert allowed.json()["broker"]["mode"] == "LIVE"
    reset = client.post(
        "/api/v2/control/brokers",
        json={"silo": PRIMARY_SILO, "mode": "PAPER"},
    )
    assert reset.json()["broker"]["mode"] == "PAPER"


def test_gate2_llm_api_key_is_write_only_and_masked(
    orchestrator: NIOSCoreOrchestrator, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    response = client.post(
        "/api/v2/control/llms",
        json={
            "system": 1,
            "provider": "ollama",
            "model": "qwen2.5:3b",
            "base_url": "http://127.0.0.1:11434",
            "api_key": SECRET,
            "daily_spend_cap_usd": 1.10,
            "timeout_ms": 250,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["system"] == 1
    assert body["write_only_credentials"] is True
    assert body["config"]["api_key_masked"] == MASKED
    assert body["config"]["api_key_set"] is True
    assert "api_key" not in body["config"]
    assert SECRET not in response.text
    assert SECRET not in caplog.text

    # Persisted state is masked too, and the raw key really is encrypted at
    # rest under ring0_vault/silos/key_vault (the test hook is Ring 0 only).
    state_text = client.get("/api/v2/cockpit/state").text
    assert SECRET not in state_text
    state = json.loads(state_text)
    assert state["llm"]["system1"]["api_key_masked"] == MASKED
    assert state["llm"]["system1"]["daily_spend_cap_usd"] == pytest.approx(1.10)
    assert state["llm"]["system1"]["model"] == "qwen2.5:3b"
    assert state["llm"]["system1"]["base_url"] == "http://127.0.0.1:11434"

    # A malformed key must not be echoed back in the 422 body.
    bad = client.post(
        "/api/v2/control/llms",
        json={"system": 9, "api_key": "do-not-echo-this-9999"},
    )
    assert bad.status_code == 422
    assert "do-not-echo-this-9999" not in bad.text

    vault = orchestrator.control.key_vault
    assert vault._raw_for_test_only("llm.system1") == SECRET
    ciphertext = (Path(orchestrator.control.key_vault.root) / "llm_keys.json").read_text(
        encoding="utf-8"
    )
    assert SECRET not in ciphertext
    assert MASKED not in ciphertext  # the mask is derived, never stored

    # System 2 is independent and starts unset.
    second = client.post(
        "/api/v2/control/llms",
        json={
            "system": "system2",
            "provider": "deepseek",
            "model": "deepseek-r1",
            "api_key": "sk-other-secret-ABCD",
            "daily_spend_cap_usd": 2.0,
        },
    )
    assert second.status_code == 200
    assert second.json()["config"]["api_key_masked"] == "***ABCD"
    assert "sk-other-secret-ABCD" not in second.text


# ---------------------------------------------------------------------------
# GATE 3 -- <1.0 ms global kill switch through the API
# ---------------------------------------------------------------------------


def test_gate3_killswitch_preempts_everything_under_one_ms(
    orchestrator: NIOSCoreOrchestrator, client: TestClient
) -> None:
    silo = PRIMARY_SILO
    symbol = PRIMARY_SYMBOL
    now = time.monotonic_ns()

    # One active 60 s lease in Tier-0 shared memory.
    from core.schemas import LeaseMode, System1Lease

    orchestrator.lease_table.write_lease(
        System1Lease(
            symbol=symbol,
            mode=LeaseMode.LONG_ONLY,
            target_bps=18.0,
            stop_bps=9.0,
            expires_at_ns=now + 60_000_000_000,
        ),
        now_ns=now,
    )
    assert orchestrator.lease_table.active_count(now) == 1
    lease_state = client.get("/api/v2/cockpit/state").json()
    assert len(lease_state["leases"]) == 1
    lease_row = lease_state["leases"][0]
    assert lease_row["symbol"] == symbol
    assert lease_row["mode"] == "LONG_ONLY"
    assert lease_row["target_bps"] == pytest.approx(18.0)
    assert lease_row["ttl_pct"] > 0.9

    # One working Maker-Peg entry with a live two-phase reservation.
    proposal = make_proposal("kill-open-order")
    verdict = orchestrator.firewall.evaluate(
        proposal,
        now_ns=now,
        cost_floor=orchestrator.control.cost_floor(silo),
    )
    assert verdict.accepted, verdict.reason
    orchestrator.oms.submit_maker_peg(
        proposal, verdict.reservation, (99.98, 99.99), now_ns=now
    )
    # A second reservation not yet tied to an order must also be voided.
    second = make_proposal("kill-pending-only", qty=5.0)
    orchestrator.outbox.reserve(second, now_ns=now)
    assert orchestrator.outbox.pending_count == 2
    assert len(orchestrator.oms.open_orders()) == 1

    # An open position so ADR-009 has something to flatten.
    orchestrator.ledger.set_position(
        silo, symbol, qty_micros=10_000_000, price_micros=100_000_000
    )

    response = client.post(
        "/api/v2/control/killswitch",
        json={"reason": "step7 gate3", "flatten": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "HALTED"
    assert body["kill_latch_hot"] is True
    assert body["within_budget"] is True
    assert body["latency_ns"] <= 1_000_000
    assert body["disarmed_leases"] == 1
    assert body["cancelled_orders"] == 1
    assert body["voided_reservations"] >= 1
    assert body["flattened_orders"] >= 1
    assert body["pending_outbox_after"] == 0

    # In-memory truth agrees with the response.
    assert orchestrator.kill_latch.is_halted is True
    assert orchestrator.lease_table.active_count(time.monotonic_ns()) == 0
    assert orchestrator.oms.open_orders() == []
    assert orchestrator.outbox.pending_count == 0
    assert all(
        reservation.state is OutboxState.VOIDED
        for reservation in orchestrator.outbox.all_reservations()
    )
    # ADR-009 signed a reduce-only flatten.
    assert any(
        verdict.get("dispatched") and verdict.get("trigger") == "KILL_LATCH"
        for verdict in body["flatten"]
    )
    assert any(
        event.event_type == "ADR009_EXIT_DISPATCHED"
        for event in orchestrator.oms.events
    )


# ---------------------------------------------------------------------------
# GATE 4 -- SSE cockpit telemetry
# ---------------------------------------------------------------------------


def test_gate4_state_and_sse_cards_match_sec016(
    orchestrator: NIOSCoreOrchestrator, client: TestClient
) -> None:
    card = PlainEnglishCockpitCard(
        silo="ZERODHA-INDIA",
        symbol=PRIMARY_SYMBOL,
        mode_badge="LIVE CAPITAL",
        story_sentence=(
            "Spinal Reflex bought RELIANCE: Aggressive buyers took 71% of 5s "
            "executed volume with clean book."
        ),
        gross_bps=18.0,
        fee_stt_bps=5.5,
        uncert_q98_bps=6.2,
        net_lb_bps=6.3,
        timestamp_ns=time.time_ns(),
    )
    orchestrator.control.publish_card(card)

    state = client.get("/api/v2/cockpit/state")
    assert state.status_code == 200
    payload = state.json()
    assert payload["abstention"]["target_rate"] == pytest.approx(0.991)
    assert set(payload["silos"]) == {
        "CRYPTO_BINANCE",
        "INDIA_ZERODHA_DHAN",
        "US_IBKR",
        "FX_MT5",
    }
    for silo_id in payload["silos"]:
        vaults = payload["silos"][silo_id]["vaults"]
        for vault in VaultName:
            assert vault.value in vaults
            assert "micros" in vaults[vault.value]
            assert "nav" in vaults[vault.value]
        assert vaults["nav"] == pytest.approx(vaults["nav_micros"] / 1_000_000.0)

    contagion = payload["contagion"]
    assert contagion["archetype_count"] == 5
    assert [group["weight"] for group in contagion["archetype_groups"]] == pytest.approx(
        [0.15, 0.25, 0.25, 0.35]
    )
    assert [item["weight"] for item in contagion["archetypes"]] == pytest.approx(
        [0.15, 0.25, 0.25, 0.20, 0.15]
    )
    assert {group["name"] for group in contagion["archetype_groups"]} == {
        "Retail 0DTE",
        "Dealer Gamma",
        "Vol-Target CTAs",
        "Pod Stops & Value",
    }
    assert sum(group["weight"] for group in contagion["archetype_groups"]) == pytest.approx(
        1.0
    )
    assert "grounding_ledger" in contagion

    expected_proof = (
        "[LIVE CAPITAL \u2022 ZERODHA-INDIA \u2022 RELIANCE.NS] Spinal Reflex "
        "bought RELIANCE: Aggressive buyers took 71% of 5s executed volume with "
        "clean book. | Gross: +18.0 bps | Fee+STT: 5.5 bps | Uncert (Q98): 6.2 "
        "bps | Net LB: +6.3 bps."
    )
    assert card.format_proof_line() == expected_proof
    assert payload["cards"][-1]["proof_line"] == expected_proof

    stream = client.get("/api/v2/cockpit/stream?fps=60&frames=2&include_history=true")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(block.split("data: ", 1)[1])
        for block in stream.text.split("\n\n")
        if block.strip().startswith("event: cockpit")
    ]
    assert len(frames) == 2
    for frame in frames:
        assert frame["type"] == "cockpit_frame"
        assert frame["fps"] == 60
        assert frame["abstention"]["target_rate"] == pytest.approx(0.991)
        assert set(frame["silos"]) == set(payload["silos"])
        for silo_payload in frame["silos"].values():
            assert set(silo_payload["vaults"]) >= {
                "ReserveYield",
                "LongTermCompounder",
                "TacticalSwing",
                "SpinalScalper",
                "nav_micros",
            }
    assert any(
        item["proof_line"] == expected_proof
        for frame in frames
        for item in frame["cards"]
    )


# ---------------------------------------------------------------------------
# GATE 5 -- full 4-ring lifecycle
# ---------------------------------------------------------------------------


def test_gate5_full_four_ring_lifecycle(orchestrator: NIOSCoreOrchestrator) -> None:
    report = orchestrator.run_end_to_end(
        warmup=30,
        run_scan=True,
        scan_symbols=5_000,
    )
    body = report.to_dict()

    assert body["shadow_fills"] == 30
    assert body["ticks"] == 31
    assert body["live_outcome"] == "LIVE_PROPOSAL"
    assert body["firewall_accepted"] is True
    assert body["signed"] is True
    assert body["reservation_committed"] is True
    assert body["net_lb_bps"] > 0.0
    assert "SIGNED" in body["firewall_reason"]

    # Ring 2 scan: 5,000 -> 50 (top 1%) and a grounded committee theses.
    assert body["scan"]["universe"] == 5_000
    assert body["scan"]["candidates"] == 50
    assert body["scan"]["compression_ratio"] >= 0.98

    # In-silo ratchet: exactly 35% compounder + 15% reserve of realised profit.
    realized = body["realized_net_micros"]
    assert realized > 0
    assert body["ratchet"]["ReserveYield"] == pytest.approx(
        int(realized * 0.15), abs=1
    )
    assert body["ratchet"]["LongTermCompounder"] == pytest.approx(
        int(realized * 0.35), abs=1
    )
    assert body["ratchet"]["TacticalSwing"] == 0

    # Two-phase outbox reached COMMITTED and published a fill event.
    reservations = orchestrator.outbox.all_reservations()
    assert len(reservations) == 1
    assert reservations[0].state is OutboxState.COMMITTED
    assert any(event.event_type == "ORDER_FILLED" for event in orchestrator.oms.events)

    # Ring 3 proof card with the exact sec_016 shape and display silo.
    assert body["cards"], "Ring 3 did not receive a live cockpit card"
    proof = body["cards"][-1]["proof_line"]
    assert proof.startswith(
        f"[LIVE CAPITAL \u2022 {DISPLAY_SILO[PRIMARY_SILO]} \u2022 {PRIMARY_SYMBOL}]"
    )
    for token in ("Gross:", "Fee+STT:", "Uncert (Q98):", "Net LB:"):
        assert token in proof

    # The card is durable on the Tier-1 cockpit stream for the SSE gateway.
    events = orchestrator.bus.replay(STREAM_RING3_COCKPIT)
    assert any(
        event.payload.get("mode_badge") == "LIVE CAPITAL"
        and event.payload.get("proof_line", "").startswith("[LIVE CAPITAL")
        for event in events
    )

    # System 1 armed the winning lease, not the deterministic fallback.
    assert body["thesis"]["system1_mode"] == "LONG_ONLY"
    assert orchestrator.twin.is_graduated(orchestrator.strategy_id) is True

    # The acceptance is a real Ring 0 verdict, not a stub.
    verdict = orchestrator._last_live_verdict
    assert verdict is not None and verdict.accepted
    assert verdict.signed is not None
    assert verdict.stage_reached is FirewallStage.SIGNING
    assert verdict.signed.ed25519_signature


def test_gate5_orchestrator_exposes_ring3_app(
    orchestrator: NIOSCoreOrchestrator,
) -> None:
    app = orchestrator.control_app()
    client = TestClient(app)
    health = client.get("/api/v2/cockpit/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["version"] == "3.0.0-PROD"
    assert health.json()["document_id"] == "NIOS-ARC-2026-V3"
