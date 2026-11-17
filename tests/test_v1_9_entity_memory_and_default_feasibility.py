"""V1.9 Suite 32 -- default-on feasibility, ``gbrain`` entity memory, MCP auth.

Four V1.9 closing gaps, each with one deterministic test:

* ``MissionFeasibilityEngine`` is wired by default, so a bare
  ``create_app(control)`` boot can never approve an impossible mission
  (``sec_018`` Law 4: the default answer is no trade).
* ``gbrain`` entity-linked post-mortem memory: append-only timeline, compiled
  truths, hybrid lexical + feature retrieval, sealed grounding packet, atomic
  journal reload and a clean-slate zero.
* Ring 2 agents retrieve that memory *before* emitting a trade proposal and
  take a deterministic confidence/sizing haircut while staying digit-grounded.
* ``plannotator`` inline annotation at the plan-exit gate revises tranche caps,
  excluded symbols and the reserve split before minting the authorization.
* ``POST /api/v2/mcp/call`` requires an operator session once the vault exists.

Fixtures are deliberately **function-scoped**: the MCP test initialises the
operator vault, and an initialised vault must never leak into the other tests
(``session_guard`` goes from inert to enforcing the moment it exists).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.schemas import GroundedFactPacket  # noqa: E402
from ring2_brain.memory.entity_graph import EntityMemoryGraph  # noqa: E402

DAY_NS = 86_400 * 1_000_000_000
UNITS = 1_000_000


def _hex64(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


# --------------------------------------------------------------------------
# fixtures (function scope -- see module docstring)
# --------------------------------------------------------------------------


@pytest.fixture()
def runtime(tmp_path):
    """A clean-slate orchestrator with a private ring 0 key vault."""

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
        # The state journal lives on a shared path, so hand it back empty: a
        # later suite must never inherit this suite's post-mortems.
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


def _mission(*, mission_id: str, target: float, risk: float, days: int, silos=None):
    return {
        "mission_id": mission_id,
        "target_units": target,
        "deadline_utc": time.time_ns() + days * DAY_NS,
        "risk_budget_units": risk,
        "operator": "sovereign",
        "silos": list(silos or ["US_IBKR"]),
        "notional_cap_units": 50_000.0,
        "state": "APPROVE",
    }


def _fund(client, units: float = 100_000.0, silo: str = "US_IBKR") -> None:
    funded = client.post(
        "/api/v2/control/ledger/fund-paper",
        json={"silo": silo, "amount_units": units, "vault": "LongTermCompounder"},
    )
    assert funded.status_code == 200, funded.text


# --------------------------------------------------------------------------
# 1. default-on feasibility engine
# --------------------------------------------------------------------------


def test_default_on_feasibility_engine_blocks_an_impossible_mission(client, runtime):
    control = runtime.control

    # ``create_app`` was handed nothing but ``control`` -- no fixture injects an
    # engine, so this asserts the default-on wiring itself.
    assert control._mission_feasibility is not None
    assert type(control._mission_feasibility).__name__ == "MissionFeasibilityEngine"
    assert control.missions.feasibility is control._mission_feasibility

    impossible = _mission(
        mission_id="M-IMPOSSIBLE-7D", target=100_000.0, risk=100_000.0, days=7
    )
    rejected = client.post("/api/v2/missions", json=impossible)
    assert rejected.status_code == 422, rejected.text
    detail = rejected.json()["detail"]
    assert detail["code"] == "INFEASIBLE_MISSION"
    assert "INFEASIBLE_MISSION" in detail["detail"]
    # Nothing was registered: an infeasible plan must not leave a record behind.
    assert client.get("/api/v2/missions").json()["count"] == 0

    _fund(client)
    accepted = client.post(
        "/api/v2/missions",
        json=_mission(mission_id="M-FEASIBLE-30D", target=100.0, risk=100.0, days=30),
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["feasibility"]["feasible"] is True
    assert body["feasibility"]["code"] == "FEASIBLE"
    # Half-Kelly is preserved: the engine caps rather than silently leveraging.
    assert 0.0 < body["feasibility"]["capped_kelly_fraction"] <= 0.5
    assert body["feasibility"]["capped_kelly_fraction"] <= (
        body["feasibility"]["kelly_fraction"] + 1e-12
    )
    assert body["feasibility"]["residual_samples"] == 200


# --------------------------------------------------------------------------
# 2. gbrain entity post-mortem memory
# --------------------------------------------------------------------------


def test_entity_memory_compiles_retrieves_reloads_and_resets(client, runtime):
    control = runtime.control

    first = control.record_post_mortem(
        outcome_kind="LOSS",
        net_pnl_micros=-2_500_000,
        slippage_bps=31.5,
        symbol="RELIANCE",
        silo="INDIA_ZERODHA_DHAN",
        strategy_id="ma_golden_cross",
        regime="CONTRACTION",
        macro_event_id="FOMC",
        lesson_summary="Momentum entry faded into a demand imbalance.",
    )
    assert first["entity_count"] == 5
    control.record_post_mortem(
        outcome_kind="SLIPPAGE_SPIKE",
        net_pnl_micros=-1_800_000,
        slippage_bps=44.0,
        symbol="BTCUSDT",
        silo="CRYPTO_BINANCE",
        strategy_id="ma_golden_cross",
        regime="CONTRACTION",
        macro_event_id="FOMC",
        lesson_summary="Funding reset widened the spread and the fill slipped.",
    )
    third = control.record_post_mortem(
        outcome_kind="WIN",
        net_pnl_micros=4_100_000,
        slippage_bps=2.0,
        symbol="BTCUSDT",
        silo="CRYPTO_BINANCE",
        strategy_id="ma_golden_cross",
        regime="CONTRACTION",
        macro_event_id="FOMC",
        lesson_summary="Trend continuation held through the FOMC drift.",
    )
    assert third["timeline_size"] == 3

    graph = control.entity_memory
    truth = graph.truth("symbol:BTCUSDT")
    assert truth is not None
    assert truth.verify() is True
    assert _hex64(truth.truth_sha256)
    assert truth.entity_type == "SYMBOL"
    assert truth.sample_count == 2
    assert truth.win_rate_bps == pytest.approx(5_000.0)
    assert truth.avg_slippage_bps == pytest.approx(23.0)
    assert "SLIPPAGE_SPIKE" in truth.known_failure_modes
    assert "HIGH_SLIPPAGE" in truth.known_failure_modes
    assert "CONTRACTION" in truth.caution_regimes
    # Layer 2 is derived: every compiled truth re-verifies against its payload.
    assert all(row.verify() is True for row in graph.truths())
    assert all(_hex64(row.truth_sha256) for row in graph.truths())

    report = control.query_entity_memory(
        symbol="BTCUSDT",
        silo="CRYPTO_BINANCE",
        strategy_id="ma_golden_cross",
        regime="CONTRACTION",
        macro_context="FOMC",
        query_text="slippage funding escalation",
        top_k=5,
    )
    # Hybrid retrieval: the symbol's own history outranks the silo/strategy arms.
    assert [row["entity_id"] for row in report["truths"]][0] == "symbol:BTCUSDT"
    assert report["truths"][0]["score"] > report["truths"][-1]["score"]
    assert report["sample_count"] >= 2
    assert report["lessons"]
    assert report["lessons"][0]["ts_utc"] >= report["lessons"][-1]["ts_utc"]
    assert report["penalty"]["applied"] is True
    assert report["penalty"]["confidence_multiplier"] < 1.0

    # The retrieved metrics are sealed: a client can re-derive the packet digest
    # instead of trusting the transport.
    sealed = report["packet"]
    rebuilt = GroundedFactPacket(**sealed)
    assert rebuilt.verify() is True
    assert rebuilt.sha256_digest == report["packet_sha256"]
    for name in (
        "sample_count",
        "win_rate_bps",
        "avg_slippage_bps",
        "worst_drawdown_bps",
    ):
        assert report["metrics"][name] == pytest.approx(sealed["metrics"][name])

    reading = client.get(
        "/api/v2/memory/entities", params={"entity_id": "symbol:BTCUSDT"}
    )
    assert reading.status_code == 200, reading.text
    listing = reading.json()
    assert _hex64(listing["payload_sha256"])
    assert listing["count"] == 1
    assert listing["entities"][0]["entity_id"] == "symbol:BTCUSDT"
    assert listing["event_count"] == 3
    # Two symbols, two silos, one strategy, one regime, one macro event.
    assert listing["entity_count"] == 7

    filtered = client.get(
        "/api/v2/memory/entities", params={"q": "high_slippage", "limit": 10}
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["count"] >= 1

    # Layer 1 is the durable source of truth; layer 2 is recomputed from it.
    stored = control.state_journal.load()
    assert "entity_memory" in stored
    assert stored["entity_memory"]["event_count"] == 3
    replay = EntityMemoryGraph(stored["entity_memory"])
    assert replay.snapshot()["event_count"] == 3
    restored = replay.truth("symbol:BTCUSDT")
    assert restored is not None and restored.truth_sha256 == truth.truth_sha256
    assert restored.caution_regimes == truth.caution_regimes

    reset = client.post("/api/v2/control/workspace/reset", json={})
    assert reset.status_code == 200, reset.text
    assert reset.json()["journal"]["entity_memory"] == {"events": 0, "entities": 0}
    emptied = control.entity_memory.snapshot()
    assert emptied["event_count"] == 0
    assert emptied["entity_count"] == 0
    assert control.entity_memory.snapshot()["event_count"] == 0
    # The journal file itself is deleted on reset, so the section is simply gone.
    assert (
        control.state_journal.load().get("entity_memory") or {}
    ).get("event_count", 0) == 0
    blank = client.get("/api/v2/memory/entities").json()
    assert blank["count"] == 0
    assert blank["event_count"] == 0
    assert blank["entity_count"] == 0


# --------------------------------------------------------------------------
# 3. pre-trade post-mortem injection in the agent dispatcher
# --------------------------------------------------------------------------


def test_agent_pretrade_memory_penalty_keeps_the_report_grounded(runtime):
    control = runtime.control
    dispatcher = control.task_dispatcher
    dispatcher.active_regime = "CONTRACTION"

    control.record_post_mortem(
        outcome_kind="SLIPPAGE_SPIKE",
        net_pnl_micros=-1_800_000,
        slippage_bps=44.0,
        symbol="BTCUSDT",
        silo="CRYPTO_BINANCE",
        strategy_id="ma_golden_cross",
        regime="CONTRACTION",
        macro_event_id="FOMC",
        lesson_summary="Funding reset widened the spread and the fill slipped.",
    )
    control.record_post_mortem(
        outcome_kind="LOSS",
        net_pnl_micros=-2_000_000,
        slippage_bps=18.0,
        symbol="BTCUSDT",
        silo="CRYPTO_BINANCE",
        strategy_id="ma_golden_cross",
        regime="CONTRACTION",
        macro_event_id="FOMC",
        lesson_summary="Breakout entry reversed on the retest.",
    )

    result = dispatcher.assign(
        agent_id="daily_decision_agent",
        silo="CRYPTO_BINANCE",
        symbols=["BTCUSDT"],
        strategy_id="ma_golden_cross",
    )

    # The numeric claims are still sealed -- the memory block is digit-free and
    # every figure rides the grounding packet instead.
    assert result["verified"] is True, result.get("verification")
    assert result["verification"]["claims_checked"] >= 1
    assert _hex64(result["grounding_sha256"])

    memory = result["structured_result"]["post_mortem"]
    assert memory["sample_count"] >= 2
    assert memory["penalty"]["applied"] is True
    assert memory["penalty"]["confidence_multiplier"] < 1.0
    assert memory["penalty"]["sizing_multiplier"] < 1.0
    assert "CONTRACTION" in memory["penalty"]["matched_caution_regimes"]
    assert _hex64(memory["packet_sha256"])
    assert any(row["entity_id"] == "symbol:BTCUSDT" for row in memory["truths"])

    reports = result["structured_result"]["reports"]
    assert reports
    multiplier = float(memory["penalty"]["confidence_multiplier"])
    for row in reports:
        assert row["post_mortem_multiplier"] == pytest.approx(multiplier)
        before = float(row["signal_score_before_post_mortem"])
        after = float(row["signal_score"])
        if before:
            # The haircut shrinks conviction toward zero in either direction.
            assert abs(after) < abs(before)
        else:
            assert after == pytest.approx(0.0)
    for symbol, score in result["structured_result"]["score_by_symbol"].items():
        assert isinstance(symbol, str) and score == pytest.approx(round(score, 6))

    # Post-mortem metrics are sealed into the packet the report was graded on.
    graded = dispatcher.task(result["task_id"])
    assert graded is not None
    packet_metrics = graded.fact_packet["metrics"]
    assert "BTCUSDT__postmortem_sample_count" in packet_metrics
    assert packet_metrics["BTCUSDT__postmortem_confidence_multiplier"] == pytest.approx(
        multiplier
    )

    # A regime with no advisory history leaves the score untouched.
    control.entity_memory.reset()
    dispatcher.active_regime = "EXPANSION"
    clean = dispatcher.assign(
        agent_id="daily_decision_agent",
        silo="CRYPTO_BINANCE",
        symbols=["BTCUSDT"],
        strategy_id="ma_golden_cross",
    )
    assert clean["verified"] is True, clean.get("verification")
    # No history -> no retrieval, no penalty, and no post-mortem block at all.
    assert "post_mortem" not in clean["structured_result"]
    import math

    for row in clean["structured_result"]["reports"]:
        # Untouched: no "before" snapshot and no multiplier are stamped.
        assert "signal_score_before_post_mortem" not in row
        assert "post_mortem_multiplier" not in row
        assert math.isfinite(float(row["signal_score"]))


# --------------------------------------------------------------------------
# 4. plannotator inline annotation at the plan-exit gate
# --------------------------------------------------------------------------


def test_plannotator_inline_annotation_revises_the_plan(client, runtime):
    _fund(client)

    created = client.post(
        "/api/v2/missions",
        json=_mission(mission_id="M-ANNOT-1", target=100.0, risk=100.0, days=30),
    )
    assert created.status_code == 200, created.text
    assert created.json()["plan_state"] == "PLAN_PROPOSED"
    assert created.json()["plan_state_history"] == ["PLAN_PROPOSED"]
    assert created.json()["revision"] == 0
    assert created.json()["revision_sha256"] == ""

    opened = client.post(
        "/api/v2/missions/M-ANNOT-1/tranches",
        json={"silo": "US_IBKR", "allocated_units": 10_000.0},
    )
    assert opened.status_code == 200, opened.text
    tranche = opened.json()
    tranche_id = tranche["tranche_id"]
    assert tranche_id == "M-ANNOT-1-T001"
    assert tranche["allocated_micros"] == 10_000 * UNITS

    cap = 4_000 * UNITS
    approved = client.post(
        "/api/v2/missions/M-ANNOT-1/approve",
        json={
            "operator": "sovereign",
            "state": "APPROVE",
            "excluded_symbols": ["btcusdt", "ethusdt"],
            "tranche_caps_micros": {tranche_id: cap},
            "reserve_split_override_bps": 7000,
            "operator_note": "Trim the crypto leg; keep the reserve floor elevated.",
        },
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()

    assert body["plan_state"] == "APPROVED"
    history = body["plan_state_history"]
    assert history[0] == "PLAN_PROPOSED"
    assert "AWAITING_OPERATOR_GATE" in history
    assert "REVISED" in history
    assert history[-1] == "APPROVED"
    assert history.index("AWAITING_OPERATOR_GATE") < history.index("REVISED")

    # An inline revision can never be recorded as a plain APPROVE.
    assert body["approval_state"] == "OVERRIDE"
    assert body["revision"] == 1
    assert _hex64(body["revision_sha256"])
    assert len(body["revisions"]) == 1
    assert body["revisions"][0]["revision_sha256"] == body["revision_sha256"]
    assert body["revisions"][0]["operator"] == "sovereign"

    assert body["excluded_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert body["reserve_split_override_bps"] == 7000
    assert body["operator_note"].startswith("Trim the crypto leg")
    assert body["tranche_caps_micros"] == {tranche_id: cap}

    rows = {row["tranche_id"]: row for row in body["tranches"]}
    assert rows[tranche_id]["allocated_micros"] == cap
    assert rows[tranche_id]["stop_micros"] <= cap

    # The revision is durable: a re-read and a later approval both see it.
    reread = client.get("/api/v2/missions/M-ANNOT-1").json()
    assert reread["revision"] == 1
    assert reread["revision_sha256"] == body["revision_sha256"]
    assert reread["plan_state"] == "APPROVED"

    reaffirmed = client.post(
        "/api/v2/missions/M-ANNOT-1/approve",
        json={"operator": "sovereign", "state": "APPROVE"},
    )
    assert reaffirmed.status_code == 200, reaffirmed.text
    again = reaffirmed.json()
    assert again["revision"] == 1
    assert again["plan_state_history"] == history
    assert again["excluded_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert again["reserve_split_override_bps"] == 7000

    # Out-of-window overrides are refused by the request model, not silently
    # clamped: the operator asked for something the split policy cannot express.
    rejected = client.post(
        "/api/v2/missions/M-ANNOT-1/approve",
        json={"operator": "sovereign", "reserve_split_override_bps": 3999},
    )
    assert rejected.status_code == 422, rejected.text


# --------------------------------------------------------------------------
# 5. MCP call requires an operator session once the vault exists
# --------------------------------------------------------------------------


def test_mcp_call_requires_an_operator_session_once_initialised(client, runtime):
    control = runtime.control
    call = {"name": "nios_get_ibor_vaults", "arguments": {}}

    assert control.operator_auth_initialized() is False
    bootstrap = client.post("/api/v2/mcp/call", json=call)
    assert bootstrap.status_code == 200, bootstrap.text
    assert bootstrap.json()["ok"] is True

    setup = client.post(
        "/api/v2/auth/setup",
        json={
            "username": "sovereign",
            "password": "correct-horse-battery-staple",
            "live_pin": "246813",
            "start_clean_slate": True,
        },
    )
    assert setup.status_code == 200, setup.text
    token = str(setup.json()["token"])
    assert token
    assert control.operator_auth_initialized() is True

    # Drop the cookie the setup call planted so "no token" really means no token.
    client.cookies.clear()
    anonymous = client.post("/api/v2/mcp/call", json=call)
    assert anonymous.status_code == 401, anonymous.text
    assert "AUTH_REQUIRED" in anonymous.text

    forged = client.post(
        "/api/v2/mcp/call", json=call, headers={"X-NIOS-Session": "not-a-session"}
    )
    assert forged.status_code == 401, forged.text

    header_ok = client.post(
        "/api/v2/mcp/call", json=call, headers={"X-NIOS-Session": token}
    )
    assert header_ok.status_code == 200, header_ok.text
    assert header_ok.json()["ok"] is True

    # The cookie transport is exercised by planting the session on the client
    # jar (per-request ``cookies=`` is deprecated by the test transport).
    client.cookies.set("nios_session", token)
    cookie_ok = client.post("/api/v2/mcp/call", json=call)
    client.cookies.clear()
    assert cookie_ok.status_code == 200, cookie_ok.text
    assert cookie_ok.json()["ok"] is True
