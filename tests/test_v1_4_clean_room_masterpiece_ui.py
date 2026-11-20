"""V1.6 - headless backend contract (supersedes the V1.4 UI suite).

Release ``v3.7.0-V1.6`` removed the React/Vite frontend entirely.  This suite
holds the backend contract that replaced it:

1. There is no ``src/``, ``public/``, ``node_modules/``, Vite or Biome toolchain
   in the active tree.
2. All fifteen pages across ``/``, ``/paper/*`` and ``/live/*`` plus the five
   auth surfaces answer ``200 text/html`` with one deterministic, zero-JavaScript
   status document.
3. ``/api/v2/*`` is never swallowed by the client catch-all.
4. Every cockpit page endpoint returns the V1.6 envelope
   (``schema_version``, ``generated_at_ns``, ``clean_slate_active``,
   ``workspace_mode``, ``payload_sha256``) plus per-silo risk telemetry.
5. The Paper and Live workspaces stay physically separated at the API boundary.
6. A pristine install still renders the clean slate.

Nothing in this suite signs an order, reads a broker secret or writes the IBOR.
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
SRC_ROOT = UI_ROOT / "src"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.orchestrator import NIOSCoreOrchestrator  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

USERNAME = "headless-operator"
PASSWORD = "headless-backend-9"
LIVE_PIN = "135790"

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

PORTAL_ROUTES = (
    "/setup",
    "/login",
    "/paper/login",
    "/live/login",
    "/live-unlock",
)

PAGE_ENDPOINTS = (
    "pages",
    "terminal",
    "orders",
    "options",
    "analyzer",
    "quant",
    "agents",
    "backtest",
    "forge",
    "ledger",
    "logbook",
    "brokers",
    "mcp",
)

RISK_FIELDS = (
    "gross_exposure_micros",
    "net_exposure_micros",
    "margin_used_micros",
    "free_margin_micros",
    "var_99_bps",
    "cvar_99_bps",
    "max_drawdown_bps",
    "realized_pnl_micros",
    "unrealized_pnl_micros",
    "total_tax_paid_micros",
)

ENVELOPE_FIELDS = (
    "schema_version",
    "generated_at_ns",
    "clean_slate_active",
    "workspace_mode",
    "payload_sha256",
)


def _routes() -> list[str]:
    routes: list[str] = []
    for key in PAGE_KEYS:
        routes.append(f"/{key}")
        routes.append(f"/paper/{key}")
        routes.append(f"/live/{key}")
    routes.extend(PORTAL_ROUTES)
    routes.extend(("/", "/positions", "/chain", "/journal", "/venues", "/xray", "/mcp"))
    return routes


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("nios-v16-ws")
    vault = tmp_path_factory.mktemp("nios-v16-vault")
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
    assert response.json()["tier"] == "PAPER"


def _headers(client) -> dict[str, str]:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    return {"X-NIOS-Session": token}


# ---------------------------------------------------------------------------
# Gate 1 - the V2.2 institutional terminal ships
# ---------------------------------------------------------------------------


def test_v1_6_frontend_tree_is_v2_2_terminal() -> None:
    """V2.6 removed the frontend tree entirely; only the headless document ships."""
    assert not SRC_ROOT.exists()
    assert not (UI_ROOT / "public").exists()
    assert not (UI_ROOT / "package.json").exists()
    assert not (UI_ROOT / "package-lock.json").exists()
    assert not (UI_ROOT / "vite.config.ts").exists()
    assert not (UI_ROOT / "biome.json").exists()
    assert not (UI_ROOT / "tsconfig.json").exists()
    assert not (UI_ROOT / "tsconfig.app.json").exists()
    assert not (UI_ROOT / "postcss.config.mjs").exists()
    assert not (UI_ROOT / "postcss.flatness.mjs").exists()
    assert not (DIST_ROOT / "assets").exists()


def test_v1_6_document_ships_v2_2_bundle() -> None:
    index = DIST_ROOT / "index.html"
    assert index.is_file()
    text = index.read_text(encoding="utf-8")
    assert "nios-frontend" in text
    assert 'content="NONE"' in text
    assert "HEADLESS_API" in text
    assert "v4.6.0-V2.6" in text
    assert "<script" not in text.lower()
    assert "/assets/" not in text

    manifest = json.loads((DIST_ROOT / "headless-manifest.json").read_text(encoding="utf-8"))
    assert manifest["frontend"] == "NONE"
    assert manifest["javascript"] == 0
    assert manifest["contracts"]["statutory_floors_bps"]["INDIA_ZERODHA_DHAN"] == 8.5


# ---------------------------------------------------------------------------
# Gate 2 - every client route serves the same deterministic document
# ---------------------------------------------------------------------------


def test_v1_6_every_route_serves_the_headless_document(client) -> None:
    index_bytes = (DIST_ROOT / "index.html").read_bytes()
    for route in _routes():
        response = client.get(route)
        assert response.status_code == 200, route
        assert response.headers["content-type"].startswith("text/html"), route
        assert response.content == index_bytes, route


def test_v1_6_api_paths_are_not_swallowed(client) -> None:
    for route in ("/api/v2/does-not-exist", "/api/v2/cockpit/does-not-exist"):
        response = client.get(route)
        assert response.status_code in (401, 403, 404, 405), route
        assert "headless-backend" not in response.text

    assert client.get("/metrics").status_code == 200


# ---------------------------------------------------------------------------
# Gate 3 - deterministic envelope on every cockpit payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", PAGE_ENDPOINTS)
def test_v1_6_page_envelope_and_risk_telemetry(client, endpoint: str) -> None:
    response = client.get(f"/api/v2/cockpit/{endpoint}")
    assert response.status_code == 200, endpoint
    body = response.json()
    for field in ENVELOPE_FIELDS:
        assert field in body, (endpoint, field)
    assert body["schema_version"] == "v1.6.headless-backend"
    assert body["generated_at_ns"] > 0
    assert body["workspace_mode"] in ("PAPER", "LIVE", "NONE")
    digest = body.pop("payload_sha256")
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )
    assert digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert isinstance(body["risk"], dict) and body["risk"]
    money_fields = (
        "gross_exposure_micros",
        "net_exposure_micros",
        "margin_used_micros",
        "free_margin_micros",
        "realized_pnl_micros",
        "unrealized_pnl_micros",
        "total_tax_paid_micros",
    )
    for silo, row in body["risk"].items():
        for field in RISK_FIELDS:
            assert field in row, (endpoint, silo, field)
        for field in money_fields:
            assert isinstance(row[field], int), (endpoint, silo, field)
        for field in ("var_99_bps", "cvar_99_bps", "max_drawdown_bps"):
            assert isinstance(row[field], float), (endpoint, silo, field)


def test_v1_6_workspace_payloads_are_mode_scoped(client, operator) -> None:
    headers = _headers(client)
    paper = client.get("/api/v2/workspace/paper/state", headers=headers)
    assert paper.status_code == 200
    body = paper.json()
    assert body["workspace_mode"] == "PAPER"
    assert body["clean_slate_active"] is True
    assert "payload_sha256" in body

    client.cookies.clear()
    anonymous = client.get("/api/v2/workspace/live/state")
    assert anonymous.status_code == 401
    denied = client.get("/api/v2/workspace/live/state", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "LIVE_ELEVATION_REQUIRED"


# ---------------------------------------------------------------------------
# Gate 4 - clean slate survives the headless conversion
# ---------------------------------------------------------------------------


def test_v1_6_clean_slate_renders_zeroes(client, operator) -> None:
    headers = _headers(client)
    state = client.get("/api/v2/workspace/paper/state", headers=headers).json()
    for silo, row in state["silos"].items():
        assert row["nav_micros"] == 0, silo
        for vault, balance in row["vaults"].items():
            if isinstance(balance, dict):
                assert balance["micros"] == 0, (silo, vault)
            else:
                assert balance == 0, (silo, vault)
    assert state["orders"]["working"] == []
    assert state["shadow"]["shadow_fills"] == 0
    assert state["shadow"]["remaining_fills"] == 30
    assert state["shadow"]["graduated"] is False
    assert state["autopilot"]["enabled"] is False

    logbook = client.get("/api/v2/cockpit/logbook").json()
    assert logbook["entries"] == []


# ---------------------------------------------------------------------------
# Gate 5 - auth portals remain first-class API surfaces
# ---------------------------------------------------------------------------


def test_v1_6_auth_routes_exist_in_the_openapi_schema(client) -> None:
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    for route in (
        "/api/v2/auth/status",
        "/api/v2/auth/setup",
        "/api/v2/auth/login",
        "/api/v2/auth/login/paper",
        "/api/v2/auth/login/live",
        "/api/v2/auth/elevate-live",
        "/api/v2/auth/logout",
        "/api/v2/auth/reset",
        "/api/v2/workspace/paper/state",
        "/api/v2/workspace/live/state",
    ):
        assert route in paths, route
