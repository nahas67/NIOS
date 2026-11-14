"""V1.6 - headless institutional terminal contract (supersedes the V1.5 suite).

``v3.7.0-V1.6`` is backend-only.  The institutional number contract that the
V1.5 terminal rendered is now enforced directly on the API:

1. Every client route answers one deterministic, zero-JavaScript document.
2. Money telemetry is integer micro-units; no float can be presented as money.
3. The four statutory cost floors are exact and itemized on orders, backtests
   and logbook rows.
4. The 15-page catalogue and the two workspace trees are complete.
5. Paper and Live remain separated at the API boundary.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"
DIST_ROOT = UI_ROOT / "dist"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.orchestrator import NIOSCoreOrchestrator  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

USERNAME = "terminal-operator"
PASSWORD = "institutional-9"
LIVE_PIN = "246810"

PAGE_KEYS = (
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
)

STATUTORY_FLOORS = {
    "INDIA_ZERODHA_DHAN": 8.50,
    "CRYPTO_BINANCE": 16.00,
    "US_IBKR": 2.80,
    "FX_MT5": 1.60,
}


def _fifty_routes() -> list[str]:
    routes: list[str] = []
    for key in PAGE_KEYS:
        routes.append(f"/{key}")
        routes.append(f"/paper/{key}")
        routes.append(f"/live/{key}")
    routes.extend(
        (
            "/",
            "/positions",
            "/chain",
            "/journal",
            "/venues",
            "/xray",
            "/mcp",
            "/setup",
            "/login",
            "/paper/login",
            "/live/login",
            "/live-unlock",
        )
    )
    return routes


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("nios-v15-ws")
    vault = tmp_path_factory.mktemp("nios-v15-vault")
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
def operator(client) -> None:
    response = client.post(
        "/api/v2/auth/setup",
        json={"username": USERNAME, "password": PASSWORD, "live_pin": LIVE_PIN},
    )
    assert response.status_code == 200, response.text


def _headers(client) -> dict[str, str]:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    return {"X-NIOS-Session": token}


# ---------------------------------------------------------------------------
# Gate 1 - fifty routes, one headless document
# ---------------------------------------------------------------------------


def test_v1_6_fifty_routes_serve_the_headless_document(client) -> None:
    index_bytes = (DIST_ROOT / "index.html").read_bytes()
    routes = _fifty_routes()
    assert len(routes) >= 50, len(routes)
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, route
        assert response.headers["content-type"].startswith("text/html"), route
        assert response.content == index_bytes, route


def test_v1_6_document_has_no_style_framework() -> None:
    text = (DIST_ROOT / "index.html").read_text(encoding="utf-8")
    lowered = text.lower()
    for forbidden in ("tailwind", "react", "vite", "styled-components", "emotion"):
        assert forbidden not in lowered, forbidden
    assert "<script" not in lowered
    assert "backdrop-filter" not in lowered


# ---------------------------------------------------------------------------
# Gate 2 - integer money and exact statutory floors
# ---------------------------------------------------------------------------


def test_v1_6_statutory_floors_are_exact(client) -> None:
    ledger = client.get("/api/v2/cockpit/ledger").json()
    costs = ledger["statutory_costs"]
    assert set(costs) == set(STATUTORY_FLOORS)
    for silo, expected in STATUTORY_FLOORS.items():
        row = costs[silo]
        assert row["cost_floor_bps"] == expected, silo
        breakdown = row["breakdown"]
        assert breakdown["total_bps"] == expected, silo
        assert breakdown["reconciles"] is True, silo
        assert sum(breakdown["component_micros"].values()) == breakdown["total_micros"]
        assert all(isinstance(v, int) for v in breakdown["component_micros"].values())

    india = costs["INDIA_ZERODHA_DHAN"]["breakdown"]["component_bps"]
    assert set(india) == {
        "stt_bps",
        "exchange_txn_bps",
        "sebi_turnover_bps",
        "stamp_duty_bps",
        "gst_18pct_bps",
        "brokerage_bps",
        "modeled_slippage_bps",
    }
    crypto = costs["CRYPTO_BINANCE"]["breakdown"]["component_bps"]
    assert set(crypto) == {
        "maker_taker_fee_bps",
        "funding_rate_8h_bps",
        "liquidation_insurance_bps",
        "modeled_slippage_bps",
    }
    us = costs["US_IBKR"]["breakdown"]["component_bps"]
    assert set(us) == {
        "sec_section31_bps",
        "finra_taf_bps",
        "occ_clearing_bps",
        "commission_bps",
        "modeled_slippage_bps",
    }
    fx = costs["FX_MT5"]["breakdown"]["component_bps"]
    assert set(fx) == {
        "raw_spread_bps",
        "ecn_commission_bps",
        "tom_next_swap_bps",
        "modeled_slippage_bps",
    }


def test_v1_6_risk_telemetry_is_integer_micros(client) -> None:
    body = client.get("/api/v2/cockpit/orders").json()
    for silo, row in body["risk"].items():
        for field in (
            "nav_micros",
            "gross_exposure_micros",
            "net_exposure_micros",
            "margin_used_micros",
            "free_margin_micros",
            "realized_pnl_micros",
            "unrealized_pnl_micros",
            "total_tax_paid_micros",
        ):
            assert isinstance(row[field], int), (silo, field)
        assert row["free_margin_micros"] >= 0
        assert row["cvar_99_bps"] >= row["var_99_bps"]


# ---------------------------------------------------------------------------
# Gate 3 - page catalogue and dual workspace tree
# ---------------------------------------------------------------------------


def test_v1_6_fifteen_page_catalogue(client) -> None:
    body = client.get("/api/v2/cockpit/pages").json()
    assert body["page_count"] == 15
    assert {page["key"] for page in body["pages"]} == set(PAGE_KEYS)
    assert body["counts"]["silos"] >= 4


def test_v1_6_dual_workspace_boundary(client, operator) -> None:
    headers = _headers(client)
    paper = client.get("/api/v2/workspace/paper/state", headers=headers)
    assert paper.status_code == 200
    assert paper.json()["workspace_mode"] == "PAPER"

    assert client.get("/api/v2/workspace/live/state").status_code in (401, 403)
    client.cookies.clear()
    assert client.get("/api/v2/workspace/live/state").status_code == 401
    assert (
        client.get("/api/v2/workspace/live/state", headers=headers).status_code
        == 403
    )

    live = client.post(
        "/api/v2/auth/login/live",
        json={"username": USERNAME, "password": PASSWORD, "live_pin": LIVE_PIN},
    )
    assert live.status_code == 200, live.text
    live_headers = {"X-NIOS-Session": live.json()["token"]}
    live_state = client.get("/api/v2/workspace/live/state", headers=live_headers)
    assert live_state.status_code == 200
    assert live_state.json()["workspace_mode"] == "LIVE"


# ---------------------------------------------------------------------------
# Gate 4 - the digest is deterministic
# ---------------------------------------------------------------------------


def test_v1_6_payload_digest_is_reproducible() -> None:
    from ring3_cockpit.gateway.payload_envelope import enclose

    payload = {"page": "quant", "factors": 490, "nested": {"a": 1}}
    first = enclose(payload, workspace_mode="PAPER", generated_at_ns=1)
    second = enclose(payload, workspace_mode="PAPER", generated_at_ns=1)
    assert first["payload_sha256"] == second["payload_sha256"]
    body = {k: v for k, v in first.items() if k != "payload_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    assert first["payload_sha256"] == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
