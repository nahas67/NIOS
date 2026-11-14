"""V1.2 - Agent task assignment workstation + tax-net walk-forward backtester.

Four gates:

1. Harvested ``daily_stock_analysis`` strategies and the grounded decision
   dashboard, verified across all four silos.
2. Agent task assignment lifecycle and clean-slate reset.
3. Tax-net walk-forward backtester and the 30-fill nursery promotion gate.
4. SPA routes (``/agents`` and ``/backtest`` in all three workspaces) plus the
   Biome lint gate on the React 19 cockpit.

The suite never signs an order, never reads a broker key, and never writes the
IBOR: Ring 2 emits grounded research only (Law 2 + Law 3, ``sec_004``/``sec_018``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"
SRC_ROOT = UI_ROOT / "src"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.orchestrator import NIOSCoreOrchestrator  # noqa: E402
from ring2_brain.quant.daily_analysis_donor import (  # noqa: E402
    KNOWN_SILOS,
    STRATEGY_CATALOG,
    STRATEGY_IDS,
    DecisionDashboard,
    evaluate_strategy,
    list_strategies,
    strategy_ids,
)
from fastapi.testclient import TestClient  # noqa: E402

USERNAME = "sovereign"
PASSWORD = "correct-horse-9"
LIVE_PIN = "246810"

AGENT_IDS = (
    "daily_decision_agent",
    "technical_strategy_agent",
    "fundamental_valuation_agent",
    "contagion_stress_agent",
    "alpha_forge_agent",
    "backtest_audit_agent",
)

# The eight harvested strategy templates, in catalog order.
EXPECTED_STRATEGIES = (
    "ma_golden_cross",
    "bullish_trend_alignment",
    "chanlun_pivot_breakout",
    "elliott_wave_impulse",
    "event_catalyst_momentum",
    "growth_quality_compounder",
    "expectation_rerating",
    "cross_asset_etf_rotation",
)

SPA_ROUTES = (
    "/agents",
    "/paper/agents",
    "/live/agents",
    "/backtest",
    "/paper/backtest",
    "/live/backtest",
)


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("nios-v12-ws")
    vault = tmp_path_factory.mktemp("nios-v12-vault")
    instance = NIOSCoreOrchestrator(
        workspace=workspace,
        key_vault_root=vault,
        clean_slate=True,
    )
    try:
        yield instance
    finally:
        try:
            instance.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def client(runtime):
    from ring3_cockpit.gateway.control_plane import create_app

    app = create_app(runtime.control)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def session_token(client) -> str:
    response = client.post(
        "/api/v2/auth/setup",
        json={"username": USERNAME, "password": PASSWORD, "live_pin": LIVE_PIN},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _headers(token: str) -> dict[str, str]:
    return {"X-NIOS-Session": token}


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


# ---------------------------------------------------------------------------
# Gate 1 - harvested strategy templates and the grounded decision dashboard
# ---------------------------------------------------------------------------


def test_gate1_eight_harvested_strategy_templates_are_registered() -> None:
    assert len(STRATEGY_IDS) == 8
    assert tuple(STRATEGY_IDS) == EXPECTED_STRATEGIES
    assert strategy_ids() == EXPECTED_STRATEGIES
    assert len(list_strategies()) == 8
    assert len(STRATEGY_CATALOG) == 8

    for spec in list_strategies():
        assert spec["strategy_id"] in EXPECTED_STRATEGIES
        assert spec["name"]
        assert spec["family"]
        assert spec["donor_source"]
        assert spec["donor_file"]
        assert spec["required_bars"] >= 30
        assert spec["parameters"]
        assert set(spec["silos"]) == set(KNOWN_SILOS)


def test_gate1_donor_strategy_templates_are_named_for_the_harvest() -> None:
    donors = {spec["strategy_id"]: spec["donor_file"] for spec in list_strategies()}
    assert donors["ma_golden_cross"].endswith("ma_golden_cross.yaml")
    assert donors["bullish_trend_alignment"].endswith("bull_trend.yaml")
    assert donors["chanlun_pivot_breakout"].endswith("chan_theory.yaml")
    assert donors["elliott_wave_impulse"].endswith("wave_theory.yaml")
    assert donors["event_catalyst_momentum"].endswith("event_driven.yaml")
    assert donors["growth_quality_compounder"].endswith("growth_quality.yaml")
    assert donors["expectation_rerating"].endswith("expectation_repricing.yaml")
    assert donors["cross_asset_etf_rotation"].endswith(".yaml")


def test_gate1_every_strategy_scores_every_silo() -> None:
    for silo in KNOWN_SILOS:
        for strategy_id in EXPECTED_STRATEGIES:
            signal = evaluate_strategy(
                strategy_id,
                symbol="RELIANCE",
                silo=silo,
                bar_count=252,
            )
            assert signal.strategy_id == strategy_id
            assert signal.silo == silo
            assert 0.0 <= signal.score <= 100.0
            assert signal.entry_price > 0.0
            assert signal.target_price > 0.0
            assert signal.stop_loss > 0.0
            assert signal.direction in {"LONG", "FLAT", "SHORT", "NEUTRAL"}
            assert signal.bars_used >= 1


def test_gate1_decision_dashboard_is_grounded_across_all_four_silos() -> None:
    dashboard = DecisionDashboard()
    seen_digests: set[str] = set()

    for silo in ("CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"):
        report = dashboard.evaluate("RELIANCE", silo, bar_count=252)

        assert report.silo == silo
        assert report.symbol == "RELIANCE"
        assert report.verified is True
        packet = report.grounded_fact_packet
        digest = (
            packet["sha256_digest"]
            if isinstance(packet, dict)
            else packet.sha256_digest
        )
        assert report.grounding_sha256 == digest
        assert _is_sha256(report.grounding_sha256)
        if hasattr(packet, "verify"):
            assert packet.verify() is True
        assert report.verification["claims_checked"] >= 1

        # Deterministic dashboard surface (sec_003 / Law 3).
        assert 0.0 <= report.signal_score <= 100.0
        assert report.operation_advice in {
            "STRONG_BUY",
            "BUY",
            "HOLD",
            "WAIT",
            "AVOID",
        }
        assert report.macd_status in {"GOLDEN_CROSS", "BULLISH", "BEARISH"}
        assert report.rsi_status in {
            "OVERSOLD",
            "WEAK",
            "NEUTRAL",
            "STRONG",
            "OVERBOUGHT",
        }
        assert report.ma5 > 0.0
        assert report.ma10 > 0.0
        assert report.ma20 > 0.0
        assert report.rsi_14 >= 0.0
        assert report.best_strategy_id in EXPECTED_STRATEGIES

        # ATR-bounded, deterministic target/stop geometry.
        assert report.stop_loss < report.close_price < report.target_price
        assert report.target_distance_pct > 0.0
        assert report.stop_distance_pct > 0.0

        assert report.signal_reasons
        assert report.risk_factors
        assert report.catalysts
        assert report.markdown_report.strip()
        assert "=" not in report.markdown_report

        # Metrics must be sealed into the packet, not just printed.
        sealed = packet["metrics"] if isinstance(packet, dict) else packet.metrics
        for key in (
            "ma5",
            "ma10",
            "ma20",
            "bias_ma5_pct",
            "rsi_14",
            "signal_score",
            "target_price",
            "stop_loss",
        ):
            assert key in sealed

        seen_digests.add(report.grounding_sha256)

    assert len(seen_digests) == 4


def test_gate1_unknown_silo_is_rejected() -> None:
    with pytest.raises(ValueError):
        DecisionDashboard().evaluate("RELIANCE", "MOON_BASE")
    with pytest.raises(ValueError):
        evaluate_strategy("ma_golden_cross", symbol="RELIANCE", silo="MOON_BASE")
    with pytest.raises(ValueError):
        evaluate_strategy("not_a_strategy", symbol="RELIANCE", silo="US_IBKR")


# ---------------------------------------------------------------------------
# Gate 2 - agent task assignment lifecycle and clean-slate reset
# ---------------------------------------------------------------------------


def test_gate2_agent_page_starts_at_clean_slate(client, session_token) -> None:
    headers = _headers(session_token)
    reset = client.post("/api/v2/control/workspace/reset", headers=headers)
    assert reset.status_code == 200, reset.text

    page = client.get("/api/v2/cockpit/agents", headers=headers)
    assert page.status_code == 200, page.text
    body = page.json()

    assert body["page"] == "agents"
    assert body["route"] == "/agents"
    assert body["task_count"] == 0
    assert body["tasks"] == []
    assert body["clean_slate"] is True

    assert body["agent_count"] == 6
    assert [agent["agent_id"] for agent in body["agents"]] == list(AGENT_IDS)
    for agent in body["agents"]:
        assert agent["ring"] == "ring2"
        assert agent["signs_orders"] is False
        assert agent["reads_broker_keys"] is False
        assert agent["capabilities"]

    assert body["strategy_count"] == 8
    assert [spec["strategy_id"] for spec in body["strategies"]] == list(
        EXPECTED_STRATEGIES
    )
    assert set(body["silos"]) == set(KNOWN_SILOS)

    assert body["token"]["cap_usd"] == pytest.approx(3.10)
    assert body["token"]["spend_usd"] == pytest.approx(0.0)

    air_gap = body["air_gap"]
    assert air_gap["ring2_only"] is True
    assert air_gap["signs_orders"] is False
    assert air_gap["reads_broker_keys"] is False
    assert air_gap["order_signing_tools"] == 0
    assert air_gap["broker_key_reading_tools"] == 0


def test_gate2_every_specialist_agent_completes_a_grounded_task(
    client,
    session_token,
) -> None:
    headers = _headers(session_token)
    receipts = []

    for agent_id in AGENT_IDS:
        response = client.post(
            "/api/v2/control/agents/assign",
            headers=headers,
            json={
                "agent_id": agent_id,
                "silo": "INDIA_ZERODHA_DHAN",
                "symbols": ["RELIANCE", "INFY"],
                "strategy_id": "ma_golden_cross",
                "prompt": "Audit the harvested decision dashboard.",
                "priority": "HIGH",
            },
        )
        assert response.status_code == 200, f"{agent_id}: {response.text}"
        receipts.append(response.json())

    assert len(receipts) == len(AGENT_IDS)

    for agent_id, receipt in zip(AGENT_IDS, receipts):
        assert receipt["agent_id"] == agent_id
        assert receipt["status"] == "COMPLETED", receipt.get("error")
        assert receipt["verified"] is True
        assert _is_sha256(receipt["grounding_sha256"])
        assert receipt["progress_pct"] == 100
        assert receipt["latency_ms"] >= 0.0
        assert receipt["token_cost_usd"] > 0.0
        assert receipt["markdown_report"].strip()
        assert "=" not in receipt["markdown_report"]
        assert receipt["verification"]["verified"] is True
        assert receipt["verification"]["claims_checked"] >= 1
        assert receipt["struct_result" if "struct_result" in receipt else "structured_result"]
        assert receipt["task_id"].startswith("task_")
        assert receipt["silo"] == "INDIA_ZERODHA_DHAN"
        assert receipt["symbols"] == ["RELIANCE", "INFY"]

    # The decision agent must surface the dashboard contract.
    decision = receipts[0]["structured_result"]
    assert decision["reports"]
    for report in decision["reports"]:
        assert 0.0 <= report["signal_score"] <= 100.0
        assert report["operation_advice"] in {
            "STRONG_BUY",
            "BUY",
            "HOLD",
            "WAIT",
            "AVOID",
        }
        assert _is_sha256(report["grounding_sha256"])

    # The backtest audit agent must surface the receipt contract.
    audit = receipts[-1]["structured_result"]
    runs = audit.get("runs") or []
    assert runs, audit
    row = runs[0]
    assert str(row.get("backtest_id", "")).startswith("bt_")
    assert row.get("cost_floor_bps") == pytest.approx(8.50)
    assert "horizon_matrix" in row or "t_plus_1" in row


def test_gate2_task_status_endpoint_returns_the_grounded_report(
    client,
    session_token,
) -> None:
    headers = _headers(session_token)
    assigned = client.post(
        "/api/v2/control/agents/assign",
        headers=headers,
        json={
            "agent_id": "technical_strategy_agent",
            "silo": "US_IBKR",
            "symbols": ["SPY"],
            "strategy_id": "bullish_trend_alignment",
            "priority": "NORMAL",
        },
    )
    assert assigned.status_code == 200, assigned.text
    task_id = assigned.json()["task_id"]

    fetched = client.get(
        f"/api/v2/control/agents/tasks/{task_id}",
        headers=headers,
    )
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["task_id"] == task_id
    assert body["status"] == "COMPLETED"
    assert body["strategy_id"] == "bullish_trend_alignment"
    assert body["grounding_sha256"] == assigned.json()["grounding_sha256"]
    assert body["markdown_report"] == assigned.json()["markdown_report"]

    missing = client.get(
        "/api/v2/control/agents/tasks/task_missing",
        headers=headers,
    )
    assert missing.status_code == 404


def test_gate2_unknown_agent_and_silo_are_rejected(client, session_token) -> None:
    headers = _headers(session_token)

    bad_agent = client.post(
        "/api/v2/control/agents/assign",
        headers=headers,
        json={
            "agent_id": "order_signing_agent",
            "silo": "US_IBKR",
            "symbols": ["SPY"],
            "priority": "NORMAL",
        },
    )
    assert bad_agent.status_code >= 400

    bad_silo = client.post(
        "/api/v2/control/agents/assign",
        headers=headers,
        json={
            "agent_id": "daily_decision_agent",
            "silo": "MOON_BASE",
            "symbols": ["SPY"],
            "priority": "NORMAL",
        },
    )
    assert bad_silo.status_code >= 400


def test_gate2_reset_clears_every_task_back_to_zero(client, session_token) -> None:
    headers = _headers(session_token)

    before = client.get("/api/v2/cockpit/agents", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["task_count"] > 0

    cancelled = client.post(
        "/api/v2/control/agents/cancel",
        headers=headers,
        json={"task_id": "ALL"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancelled"] >= 1
    assert cancelled.json()["remaining_tasks"] == 0

    reset = client.post("/api/v2/control/workspace/reset", headers=headers)
    assert reset.status_code == 200, reset.text
    payload = reset.json()
    assert payload["clean_slate"] is True
    assert payload["agents"]["task_count"] == 0
    assert payload["agents"]["clean_slate"] is True
    assert payload["backtest"]["run_count"] == 0

    after = client.get("/api/v2/cockpit/agents", headers=headers)
    assert after.status_code == 200, after.text
    assert after.json()["task_count"] == 0
    assert after.json()["tasks"] == []
    assert after.json()["token"]["spend_usd"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Gate 3 - tax-net walk-forward backtester and the nursery gate
# ---------------------------------------------------------------------------


def test_gate3_backtest_page_publishes_cost_floors(client, session_token) -> None:
    headers = _headers(session_token)
    page = client.get("/api/v2/cockpit/backtest", headers=headers)
    assert page.status_code == 200, page.text
    body = page.json()

    assert body["page"] == "backtest"
    assert body["route"] == "/backtest"
    assert body["run_count"] == 0
    assert body["runs"] == []
    assert body["clean_slate"] is True
    assert body["dsr_gate"] == pytest.approx(1.50)
    assert body["champion_net_edge_floor_bps"] == pytest.approx(0.50)
    assert body["k_folds"] == 5
    assert body["embargo_pct"] == pytest.approx(0.02)
    assert body["strategy_count"] == 8
    assert [spec["strategy_id"] for spec in body["strategies"]] == list(
        EXPECTED_STRATEGIES
    )

    floors = {row["silo"]: row for row in body["cost_floor_table"]}
    assert set(floors) == set(KNOWN_SILOS)
    assert floors["INDIA_ZERODHA_DHAN"]["cost_floor_bps"] == pytest.approx(8.50)
    assert floors["INDIA_ZERODHA_DHAN"]["statutory_tax_bps"] == pytest.approx(5.50)
    assert floors["CRYPTO_BINANCE"]["cost_floor_bps"] == pytest.approx(16.00)
    assert floors["US_IBKR"]["cost_floor_bps"] == pytest.approx(2.80)
    assert floors["FX_MT5"]["cost_floor_bps"] == pytest.approx(1.60)


def test_gate3_india_backtest_deducts_statutory_stt_floor(
    client,
    session_token,
) -> None:
    headers = _headers(session_token)
    response = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "INDIA_ZERODHA_DHAN",
            "symbol": "RELIANCE",
            "strategy_id": "ma_golden_cross",
            "bars": 252,
            "initial_capital_units": 100000.0,
        },
    )
    assert response.status_code == 200, response.text
    receipt = response.json()

    assert receipt["silo"] == "INDIA_ZERODHA_DHAN"
    assert receipt["cost_floor_bps"] == pytest.approx(8.50)
    assert receipt["statutory_tax_bps"] == pytest.approx(5.50)
    assert receipt["backtest_id"]
    assert receipt["bars"] == 252
    assert receipt["initial_capital_units"] == pytest.approx(100000.0)

    # Grounding (Law 3).
    assert receipt["verified"] is True
    assert _is_sha256(receipt["grounding_sha256"])
    assert receipt["verification"]["verified"] is True
    assert receipt["verification"]["claims_checked"] >= 1
    assert "=" not in receipt["markdown_report"]

    # Equity, drawdown and benchmark curves are bar-aligned.
    assert len(receipt["equity_curve"]) == 252
    assert len(receipt["drawdown_curve_pct"]) == 252
    assert len(receipt["benchmark_curve_units"]) == 252
    assert receipt["final_nav_units"] > 0.0
    assert receipt["benchmark_final_units"] > 0.0
    assert all(value <= 0.0 for value in receipt["drawdown_curve_pct"])

    metrics = receipt["metrics"]
    for key in (
        "total_return_pct",
        "benchmark_return_pct",
        "alpha_pct",
        "cagr_pct",
        "sharpe_ratio",
        "deflated_sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "max_drawdown_pct",
        "win_rate_pct",
        "profit_factor",
        "total_trades",
        "gross_edge_bps",
        "tax_and_cost_drag_bps",
        "net_edge_bps",
    ):
        assert key in metrics, key

    assert metrics["total_trades"] >= 1
    assert receipt["trade_count"] == int(metrics["total_trades"])

    # Every blotter row must net to gross minus the statutory floor.
    assert receipt["trades"]
    for trade in receipt["trades"]:
        assert trade["side"] == "LONG"
        assert trade["reason"] in {
            "STOP_LOSS",
            "TAKE_PROFIT",
            "SIGNAL_EXIT",
            "END_OF_SERIES",
        }
        assert trade["cost_bps"] == pytest.approx(8.50)
        assert trade["net_bps"] == pytest.approx(
            trade["gross_bps"] - trade["cost_bps"],
            abs=1e-6,
        )

    # Harvested T+1 / T+5 / T+20 horizon matrix.
    horizon = receipt["horizon_matrix"]
    assert set(horizon) == {"t_plus_1", "t_plus_5", "t_plus_20"}
    for key, row in horizon.items():
        assert row["horizon_bars"] > 0, key
        assert row["samples"] >= 0
        assert 0.0 <= row["win_rate_pct"] <= 100.0
        assert row["cost_bps"] == pytest.approx(8.50)

    # Purged K-Fold + embargo, no leakage.
    kfold = receipt["purged_kfold"]
    assert kfold["k"] == 5
    assert kfold["embargo_pct"] == pytest.approx(0.02)
    assert kfold["fold_count"] == 5
    assert len(kfold["folds"]) == 5
    assert kfold["leakage_free"] is True
    assert kfold["purged"] >= 1


def test_gate3_crypto_backtest_uses_the_sixteen_bps_floor(
    client,
    session_token,
) -> None:
    headers = _headers(session_token)
    response = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "CRYPTO_BINANCE",
            "symbol": "BTCUSDT",
            "strategy_id": "ma_golden_cross",
            "bars": 252,
            "initial_capital_units": 250000.0,
        },
    )
    assert response.status_code == 200, response.text
    receipt = response.json()

    assert receipt["silo"] == "CRYPTO_BINANCE"
    assert receipt["cost_floor_bps"] == pytest.approx(16.00)
    assert receipt["statutory_tax_bps"] == pytest.approx(0.0)
    assert receipt["verified"] is True
    assert _is_sha256(receipt["grounding_sha256"])
    assert len(receipt["equity_curve"]) == 252
    assert receipt["metrics"]["total_trades"] > 0
    assert receipt["metrics"]["tax_and_cost_drag_bps"] > 0.0

    for trade in receipt["trades"]:
        assert trade["cost_bps"] == pytest.approx(16.00)


def test_gate3_invalid_backtest_payloads_are_rejected(client, session_token) -> None:
    headers = _headers(session_token)

    bad_silo = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "MOON_BASE",
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "bars": 252,
        },
    )
    assert bad_silo.status_code >= 400

    bad_strategy = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "strategy_id": "moon_phase_alpha",
            "bars": 252,
        },
    )
    assert bad_strategy.status_code >= 400

    too_short = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "bars": 12,
        },
    )
    assert too_short.status_code >= 400


def test_gate3_nursery_rejects_a_sub_threshold_backtest(
    client,
    session_token,
) -> None:
    headers = _headers(session_token)
    weak = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "INDIA_ZERODHA_DHAN",
            "symbol": "RELIANCE",
            "strategy_id": "ma_golden_cross",
            "bars": 252,
        },
    )
    assert weak.status_code == 200, weak.text
    weak_receipt = weak.json()
    assert weak_receipt["deflated_sharpe_ratio"] < 1.50
    assert weak_receipt["passes_dsr_gate"] is False
    assert weak_receipt["promotable"] is False

    rejected = client.post(
        "/api/v2/control/backtest/promote-to-nursery",
        headers=headers,
        json={"backtest_id": weak_receipt["backtest_id"]},
    )
    assert rejected.status_code == 409, rejected.text
    detail = rejected.json()["detail"]
    assert "NURSERY-GATE-REJECTED" in detail
    assert "deflated_sharpe_ratio" in detail

    unknown = client.post(
        "/api/v2/control/backtest/promote-to-nursery",
        headers=headers,
        json={"backtest_id": "bt_missing"},
    )
    assert unknown.status_code == 404


def test_gate3_nursery_promotes_a_qualifying_backtest(
    client,
    session_token,
) -> None:
    headers = _headers(session_token)
    strong = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "CRYPTO_BINANCE",
            "symbol": "TCS",
            "strategy_id": "ma_golden_cross",
            "bars": 252,
            "initial_capital_units": 100000.0,
            "fast_window": 6,
            "slow_window": 40,
            "stop_loss_bps": 300.0,
            "take_profit_bps": 900.0,
        },
    )
    assert strong.status_code == 200, strong.text
    receipt = strong.json()

    assert receipt["cost_floor_bps"] == pytest.approx(16.00)
    assert receipt["deflated_sharpe_ratio"] >= 1.50
    assert receipt["passes_dsr_gate"] is True
    assert receipt["net_edge_bps"] >= 0.50
    assert receipt["passes_champion_floor"] is True
    assert receipt["promotable"] is True
    assert receipt["verified"] is True

    promoted = client.post(
        "/api/v2/control/backtest/promote-to-nursery",
        headers=headers,
        json={"backtest_id": receipt["backtest_id"]},
    )
    assert promoted.status_code == 200, promoted.text
    body = promoted.json()

    assert body["promoted"] is True
    assert body["nursery_bypassed"] is False
    assert body["backtest_id"] == receipt["backtest_id"]
    assert body["strategy_id"] == receipt["strategy_id"]
    assert body["silo"] == receipt["silo"]
    assert body["required_fills"] == 30
    assert body["remaining_fills"] == 30
    assert body["shadow_fills"] == 0
    assert body["graduated"] is False
    assert body["next"] == "analyzer"
    assert body["plugin_name"].startswith("nursery-ma_golden_cross-")
    assert body["grounding_sha256"] == receipt["grounding_sha256"]

    # The 30-fill nursery may be re-registered, never bypassed.
    again = client.post(
        "/api/v2/control/backtest/promote-to-nursery",
        headers=headers,
        json={"backtest_id": receipt["backtest_id"]},
    )
    assert again.status_code == 200, again.text
    assert again.json()["remaining_fills"] == 30
    assert again.json()["nursery_bypassed"] is False


def test_gate3_backtest_history_survives_until_reset_purges_it(
    client,
    session_token,
) -> None:
    headers = _headers(session_token)
    page = client.get("/api/v2/cockpit/backtest", headers=headers)
    assert page.status_code == 200, page.text
    assert page.json()["run_count"] >= 3
    assert page.json()["clean_slate"] is False

    saved = page.json()["runs"]
    for row in saved:
        assert row["backtest_id"]
        assert row["silo"] in KNOWN_SILOS
        assert row["cost_floor_bps"] in (8.50, 16.00, 2.80, 1.60)
        assert row["grounding_sha256"]

    reset = client.post("/api/v2/control/workspace/reset", headers=headers)
    assert reset.status_code == 200, reset.text
    assert reset.json()["backtest"]["run_count"] == 0

    after = client.get("/api/v2/cockpit/backtest", headers=headers)
    assert after.status_code == 200, after.text
    assert after.json()["run_count"] == 0
    assert after.json()["runs"] == []
    assert after.json()["clean_slate"] is True


# ---------------------------------------------------------------------------
# Gate 4 - SPA routes and the React 19 bundle
# ---------------------------------------------------------------------------


def test_gate4_pages_index_advertises_fifteen_pages(client, session_token) -> None:
    headers = _headers(session_token)
    response = client.get("/api/v2/cockpit/pages", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["page_count"] == 15
    keys = [page["key"] for page in body["pages"]]
    assert keys == [
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
    ]

    for key, route in (("agents", "/agents"), ("backtest", "/backtest")):
        assert body["routes"][route] == key
        page = next(item for item in body["pages"] if item["key"] == key)
        assert page["live"] is True
        assert page["status"] == "READY"
        assert page["read_only"] is True
        assert page["ring"] == "RING 2"
        assert page["api"] == f"/api/v2/cockpit/{key}"

    assert body["air_gap"]["ring3_read_only"] is True
    assert body["air_gap"]["order_signing_tools"] == 0
    assert body["air_gap"]["key_reading_tools"] == 0


@pytest.mark.parametrize("route", SPA_ROUTES)
def test_gate4_spa_routes_serve_the_cockpit_bundle(
    client,
    session_token,
    route: str,
) -> None:
    headers = _headers(session_token)
    response = client.get(route, headers=headers)
    assert response.status_code == 200, f"{route}: {response.text[:200]}"
    assert "text/html" in response.headers.get("content-type", "")


def test_gate4_page_aliases_are_declared_in_the_backend() -> None:
    """V1.6: the alias map lives in the Ring 3 page catalogue, not a router."""
    source = (
        ROOT / "ring3_cockpit" / "gateway" / "control_plane.py"
    ).read_text(encoding="utf-8")
    for key in ('"agents"', '"backtest"', '"/agents"', '"/backtest"'):
        assert key in source, key
    assert '"aliases"' in source
    assert '"group"' in source
    document = (UI_ROOT / "dist" / "index.html").read_text(encoding="utf-8")
    assert "HEADLESS_API" in document
    assert "<script" not in document.lower()


def test_gate4_biome_lint_is_clean() -> None:
    npx = shutil.which("npx")
    if npx is None:
        pytest.skip("npx is not available on this host")

    completed = subprocess.run(
        [npx, "biome", "lint", "src/"],
        cwd=str(UI_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout + completed.stderr)[-4000:]


def test_gate4_headless_document_is_built() -> None:
    index_html = UI_ROOT / "dist" / "index.html"
    assert index_html.exists(), "the headless status document is missing"
    text = index_html.read_text(encoding="utf-8")
    assert "headless-backend" in text
    assert "HEADLESS_API" in text
    assert "<script" not in text.lower()
    assert "/assets/" not in text
    assert not (UI_ROOT / "src").exists()
    assert not (UI_ROOT / "package.json").exists()

def test_gate5_watchlist_batch_qa_followup_and_8_strategy_comparison(
    client,
    session_token,
) -> None:
    """V1.2 Step 2: batch watchlist, grounded Q&A, timeframes, 8-way compare."""

    from ring2_brain.quant.daily_analysis_donor import OPERATION_ADVICE

    headers = _headers(session_token)

    reset = client.post("/api/v2/control/workspace/reset", headers=headers)
    assert reset.status_code == 200, reset.text

    # -- 1. Multi-symbol watchlist batch ranking --------------------------
    batch = client.post(
        "/api/v2/control/agents/batch-watchlist",
        headers=headers,
        json={
            "silo": "US_IBKR",
            "symbols": ["SPY", "QQQ", "IWM", "DIA"],
            "strategy_id": "ma_golden_cross",
        },
    )
    assert batch.status_code == 200, batch.text
    body = batch.json()
    assert body["status"] == "COMPLETED"
    assert body["verified"] is True
    assert body["task_id"].startswith("task_")
    assert _is_sha256(body["grounding_sha256"])
    assert body["fact_count"] > 0

    structured = body["structured_result"]
    ranking = structured["ranking"]
    assert structured["batch"] is True
    assert structured["watchlist_size"] == 4
    assert structured["strategy_id"] == "ma_golden_cross"
    assert [row["rank"] for row in ranking] == [1, 2, 3, 4]
    assert [row["symbol"] for row in ranking] == sorted(
        (row["symbol"] for row in ranking),
        key=lambda symbol: -next(
            row["signal_score"] for row in ranking if row["symbol"] == symbol
        ),
    )
    for row in ranking:
        assert 0.0 <= row["signal_score"] <= 100.0
        assert row["operation_advice"] in OPERATION_ADVICE
        assert _is_sha256(row["grounding_sha256"])
        assert row["target_price"] > 0.0
        assert row["stop_loss"] > 0.0
        assert row["reward_risk"] >= 0.0
    scores = [row["signal_score"] for row in ranking]
    assert scores == sorted(scores, reverse=True)
    assert structured["top_symbol"] == ranking[0]["symbol"]

    stored = client.get(
        f"/api/v2/control/agents/tasks/{body['task_id']}", headers=headers
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["conversation_history"] == []

    # -- 2. Grounded multi-turn strategy Q&A follow-up --------------------
    first = client.post(
        "/api/v2/control/agents/followup",
        headers=headers,
        json={
            "task_id": body["task_id"],
            "question": "What are the target and stop levels and the main risk?",
        },
    )
    assert first.status_code == 200, first.text
    turn = first.json()
    assert turn["verified"] is True
    assert turn["task_id"] == body["task_id"]
    assert turn["turn_index"] == 1
    assert len(turn["conversation_history"]) == 2
    assert turn["conversation_history"][0]["role"] == "operator"
    assert turn["conversation_history"][1]["role"] == "agent"
    assert _is_sha256(turn["grounding_sha256"])
    assert "levels stop_loss" in turn["answer"]
    assert "Risk factors" in turn["answer"]
    verification = turn["verification"]
    assert verification["verified"] is True
    assert verification["claims_checked"] >= 1
    assert verification["digest"] == turn["grounding_sha256"]

    second = client.post(
        "/api/v2/control/agents/followup",
        headers=headers,
        json={
            "task_id": body["task_id"],
            "question": "How do the RSI and moving-average indicators look?",
        },
    )
    assert second.status_code == 200, second.text
    second_turn = second.json()
    assert second_turn["turn_index"] == 2
    assert len(second_turn["conversation_history"]) == 4
    assert "indicators rsi_fourteen" in second_turn["answer"]
    assert "indicators atr_fourteen" in second_turn["answer"]
    assert second_turn["verified"] is True

    missing = client.post(
        "/api/v2/control/agents/followup",
        headers=headers,
        json={"task_id": "task_missing", "question": "What is the target?"},
    )
    assert missing.status_code == 404

    # -- 3. Timeframe selection scales annualisation and volatility -------
    daily = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "bars": 120,
            "timeframe": "1d",
        },
    )
    assert daily.status_code == 200, daily.text
    daily_body = daily.json()
    assert daily_body["timeframe"] == "1d"
    assert daily_body["bars_per_year"] == 252
    assert daily_body["metrics"]["bars_per_year"] == 252.0
    assert daily_body["bar_interval_ns"] == 86_400_000_000_000

    five_minute = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "bars": 120,
            "timeframe": "5m",
        },
    )
    assert five_minute.status_code == 200, five_minute.text
    five_body = five_minute.json()
    assert five_body["timeframe"] == "5m"
    assert five_body["bars_per_year"] == 19656
    assert five_body["metrics"]["bars_per_year"] == 19656.0
    assert five_body["bar_interval_ns"] == 300_000_000_000
    assert five_body["metrics"]["sharpe_ratio"] != daily_body["metrics"]["sharpe_ratio"]

    minute = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "bars": 90,
            "timeframe": "1m",
        },
    )
    assert minute.status_code == 200, minute.text
    assert minute.json()["metrics"]["bars_per_year"] == 98280.0

    bad_timeframe = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "bars": 120,
            "timeframe": "4h",
        },
    )
    assert bad_timeframe.status_code == 422

    # -- 4. Eight-strategy tax-net comparison matrix ----------------------
    compare = client.post(
        "/api/v2/control/backtest/compare-strategies",
        headers=headers,
        json={
            "silo": "INDIA_ZERODHA_DHAN",
            "symbol": "RELIANCE",
            "timeframe": "1d",
            "bars": 180,
        },
    )
    assert compare.status_code == 200, compare.text
    matrix = compare.json()
    assert matrix["strategy_count"] == 8
    assert matrix["timeframe"] == "1d"
    assert matrix["bars_per_year"] == 252
    assert matrix["cost_floor_bps"] == 8.5
    assert matrix["statutory_tax_bps"] == 5.5
    assert _is_sha256(matrix["comparison_sha256"])
    leaderboard = matrix["leaderboard"]
    assert len(leaderboard) == 8
    assert {row["strategy_id"] for row in leaderboard} == set(EXPECTED_STRATEGIES)
    assert [row["rank"] for row in leaderboard] == list(range(1, 9))
    assert [row["deflated_sharpe_ratio"] for row in leaderboard] == sorted(
        (row["deflated_sharpe_ratio"] for row in leaderboard), reverse=True
    )
    for row in leaderboard:
        assert row["cost_floor_bps"] == 8.5
        assert isinstance(row["passes_dsr_gate"], bool)
        assert row["max_drawdown_pct"] >= 0.0
        assert 0.0 <= row["win_rate_pct"] <= 100.0
        assert _is_sha256(row["grounding_sha256"])
        assert row["backtest_id"].startswith("bt_")
    assert matrix["best_strategy"]["strategy_id"] == leaderboard[0]["strategy_id"]

    us_compare = client.post(
        "/api/v2/control/backtest/compare-strategies",
        headers=headers,
        json={"silo": "US_IBKR", "symbol": "SPY", "timeframe": "1h", "bars": 120},
    )
    assert us_compare.status_code == 200, us_compare.text
    us_body = us_compare.json()
    assert us_body["cost_floor_bps"] == 2.8
    assert us_body["statutory_tax_bps"] == 0.0
    assert us_body["bars_per_year"] == 1638
    assert len(us_body["leaderboard"]) == 8

    crypto_compare = client.post(
        "/api/v2/control/backtest/compare-strategies",
        headers=headers,
        json={"silo": "CRYPTO_BINANCE", "symbol": "BTC-USDT", "bars": 90},
    )
    assert crypto_compare.status_code == 200, crypto_compare.text
    assert crypto_compare.json()["cost_floor_bps"] == 16.0

    # -- 5. Page contracts advertise the new controls ---------------------
    agents_page = client.get("/api/v2/cockpit/agents", headers=headers).json()
    assert agents_page["routes"]["batch_watchlist"] == (
        "/api/v2/control/agents/batch-watchlist"
    )
    assert agents_page["routes"]["followup"] == "/api/v2/control/agents/followup"
    backtest_page = client.get("/api/v2/cockpit/backtest", headers=headers).json()
    assert backtest_page["routes"]["compare"] == (
        "/api/v2/control/backtest/compare-strategies"
    )
    assert "1m" in backtest_page["timeframes"]
    assert "1d" in backtest_page["timeframes"]

    purge = client.post("/api/v2/control/workspace/reset", headers=headers)
    assert purge.status_code == 200, purge.text
