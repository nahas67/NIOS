"""V1.1 — sovereign auth, dual `/paper` vs `/live` workspaces, clean slate.

Three gates, one shared 4-ring runtime:

1. First-time operator setup, PAPER login and LIVE PIN elevation.
2. Zero-demo clean-slate reset + user-funded Paper capital (double entry).
3. Separate `/paper/*` and `/live/*` SPA routes + compiled bundle lint.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from fastapi.testclient import TestClient  # noqa: E402

from core.orchestrator import NIOSCoreOrchestrator  # noqa: E402
from ring0_vault.ibor.ledger_outbox import ALL_VAULTS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"

USERNAME = "sovereign"
PASSWORD = "correct-horse-9"
LIVE_PIN = "246810"


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("nios-v11-ws")
    vault = tmp_path_factory.mktemp("nios-v11-vault")
    runtime = NIOSCoreOrchestrator(
        workspace=workspace,
        key_vault_root=vault,
        clean_slate=True,
    )
    try:
        yield runtime
    finally:
        try:
            runtime.close()
        except Exception:  # pragma: no cover - teardown is best effort
            pass


@pytest.fixture(scope="module")
def client(runtime):
    from ring3_cockpit.gateway.control_plane import create_app

    app = create_app(runtime.control)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def paper_session(client) -> dict:
    """Run setup once and hand every gate the PAPER bearer token."""
    initial = client.get("/api/v2/auth/status")
    assert initial.status_code == 200
    assert initial.json()["initialized"] is False
    assert initial.json()["tier"] == "NONE"

    setup = client.post(
        "/api/v2/auth/setup",
        json={"username": USERNAME, "password": PASSWORD, "live_pin": LIVE_PIN},
    )
    assert setup.status_code == 200, setup.text
    body = setup.json()
    assert body["tier"] == "PAPER"
    assert body["clean_slate"] is True
    assert body["token"]
    return body


def _headers(token: str) -> dict[str, str]:
    return {"X-NIOS-Session": token}


def test_gate1_auth_setup_login_and_live_elevation(client, paper_session):
    # --- setup put the workspace in clean slate -------------------------
    status = client.get("/api/v2/auth/status", headers=_headers(paper_session["token"]))
    assert status.status_code == 200
    payload = status.json()
    assert payload["initialized"] is True
    assert payload["authenticated"] is True
    assert payload["username"] == USERNAME
    assert payload["tier"] == "PAPER"
    assert payload["clean_slate_active"] is True

    # --- duplicate registration is refused ------------------------------
    again = client.post(
        "/api/v2/auth/setup",
        json={"username": USERNAME, "password": PASSWORD, "live_pin": LIVE_PIN},
    )
    assert again.status_code == 409

    # --- a fresh login issues a PAPER token -----------------------------
    login = client.post(
        "/api/v2/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    login_body = login.json()
    assert login_body["tier"] == "PAPER"
    paper_token = login_body["token"]
    assert paper_token

    bad_login = client.post(
        "/api/v2/auth/login",
        json={"username": USERNAME, "password": "wrong-password"},
    )
    assert bad_login.status_code == 401

    # --- PAPER can never read the LIVE workspace ------------------------
    sealed = client.get(
        "/api/v2/workspace/live/state", headers=_headers(paper_token)
    )
    assert sealed.status_code == 403
    assert sealed.json()["detail"] == "LIVE_ELEVATION_REQUIRED"

    anonymous = client.get("/api/v2/workspace/live/state")
    assert anonymous.status_code == 403  # the PAPER cookie rides along
    client.cookies.clear()
    anonymous = client.get("/api/v2/workspace/live/state")
    assert anonymous.status_code == 401
    assert anonymous.json()["detail"] == "AUTH_REQUIRED"

    # --- a wrong PIN does not elevate -----------------------------------
    wrong_pin = client.post(
        "/api/v2/auth/elevate-live",
        json={"live_pin": "000000"},
        headers=_headers(paper_token),
    )
    assert wrong_pin.status_code == 403
    assert wrong_pin.json()["detail"] == "LIVE_PIN_REJECTED"

    # --- the valid PIN upgrades the same PAPER session to LIVE ----------
    elevated = client.post(
        "/api/v2/auth/elevate-live",
        json={"live_pin": LIVE_PIN},
        headers=_headers(paper_token),
    )
    assert elevated.status_code == 200, elevated.text
    live_body = elevated.json()
    assert live_body["tier"] == "LIVE"
    assert live_body["token"]
    assert "preflight_ok" in live_body

    live_state = client.get(
        "/api/v2/workspace/live/state", headers=_headers(live_body["token"])
    )
    assert live_state.status_code == 200
    live = live_state.json()
    assert live["workspace"] == "live"
    assert live["mode"] == "LIVE"
    assert live["tier"] == "LIVE"
    assert "preflight" in live
    assert "broker_credentials" in live
    assert "feed" in live

    # Elevation is single-use: the replayed PAPER token is now revoked.
    replay = client.get("/api/v2/workspace/live/state", headers=_headers(paper_token))
    assert replay.status_code == 401

    # --- logout invalidates the LIVE token ------------------------------
    logout = client.post("/api/v2/auth/logout", headers=_headers(live_body["token"]))
    assert logout.status_code == 200
    after = client.get("/api/v2/workspace/live/state", headers=_headers(live_body["token"]))
    assert after.status_code == 401


def test_gate2_zero_demo_clean_slate_and_user_paper_funding(client, runtime, paper_session):
    headers = _headers(paper_session["token"])

    reset = client.post("/api/v2/control/workspace/reset", headers=headers)
    assert reset.status_code == 200, reset.text
    report = reset.json()
    assert report["reset"] is True
    assert report["clean_slate_active"] is True
    assert report["autopilot"] is False

    # --- every silo x vault is exactly 0 micros -------------------------
    assert len(runtime.silos) == 4
    for silo in runtime.silos:
        for vault in ALL_VAULTS:
            assert runtime.ledger.balance(silo, str(vault)) == 0, (silo, vault)
        accounts = runtime.ledger.accounts(silo)
        assert len(accounts) == 7  # 4 vaults + CASH / FEES / PNL
        for account in accounts.values():
            assert account.debits_posted == 0
            assert account.credits_posted == 0
            assert account.debits_pending == 0
            assert account.credits_pending == 0
        assert runtime.ledger.silo_nav_micros(silo) == 0

    invariant = runtime.ledger.invariant_report()
    assert invariant["holds"] is True
    assert invariant["violations"] == []
    assert sum(
        account.debits_posted for account in runtime.ledger._accounts.values()
    ) == 0
    assert sum(
        account.credits_posted for account in runtime.ledger._accounts.values()
    ) == 0

    # --- no orders, no reservations, no nursery, no spend ---------------
    assert runtime.oms.open_orders() == []
    assert runtime.oms.events == ()
    assert runtime.outbox.all_reservations() == []
    assert runtime.outbox.pending_count == 0
    assert runtime.ledger.transfers() == []

    paper = client.get("/api/v2/workspace/paper/state", headers=headers)
    assert paper.status_code == 200
    paper_state = paper.json()
    assert paper_state["workspace"] == "paper"
    assert paper_state["clean_slate_active"] is True
    assert paper_state["orders"]["working"] == []
    assert paper_state["orders"]["events"] == []
    assert paper_state["shadow"]["shadow_fills"] == 0
    assert paper_state["shadow"]["live_fills"] == 0
    assert paper_state["shadow"]["graduated"] is False
    assert paper_state["shadow"]["remaining_fills"] == 30
    assert paper_state["token_spend_usd"] == pytest.approx(0.0)
    assert paper_state["autopilot"]["enabled"] is False
    for silo in paper_state["silos"].values():
        assert silo["nav_micros"] == 0
        for vault in ALL_VAULTS:
            assert silo["vaults"][str(vault)]["micros"] == 0

    status = client.get("/api/v2/auth/status", headers=headers)
    assert status.json()["tier"] == "PAPER"  # reset is not a logout
    assert status.json()["clean_slate_active"] is True

    # --- unknown silo is refused ----------------------------------------
    unknown = client.post(
        "/api/v2/control/ledger/fund-paper",
        json={"silo": "MARS_EXCHANGE", "amount_units": 1.0},
        headers=headers,
    )
    assert unknown.status_code == 404

    # --- the operator deposits exactly 25,000 units ---------------------
    funded = client.post(
        "/api/v2/control/ledger/fund-paper",
        json={
            "silo": "US_IBKR",
            "amount_units": 25_000.0,
            "vault": "ALL_CANONICAL",
        },
        headers=headers,
    )
    assert funded.status_code == 200, funded.text
    receipt = funded.json()
    assert receipt["silo"] == "US_IBKR"
    assert receipt["amount_micros"] == 25_000_000_000
    assert receipt["invariant"]["holds"] is True
    allocations = receipt["allocations"]
    assert sum(allocations.values()) == 25_000_000_000
    for vault in ALL_VAULTS:
        assert runtime.ledger.balance("US_IBKR", str(vault)) == allocations[str(vault)]
    assert runtime.ledger.silo_nav_micros("US_IBKR") == 25_000_000_000

    # --- double entry stays balanced on the genesis deposit -------------
    debit_total = sum(
        account.debits_posted for account in runtime.ledger._accounts.values()
    )
    credit_total = sum(
        account.credits_posted for account in runtime.ledger._accounts.values()
    )
    assert debit_total == credit_total == 25_000_000_000 + 25_000_000_000
    assert runtime.ledger.invariant_report()["holds"] is True

    # --- every other silo is still untouched at zero --------------------
    for silo in runtime.silos:
        if silo == "US_IBKR":
            continue
        assert runtime.ledger.silo_nav_micros(silo) == 0

    after = client.get("/api/v2/workspace/paper/state", headers=headers)
    assert after.json()["clean_slate_active"] is False
    assert after.json()["silos"]["US_IBKR"]["nav_micros"] == 25_000_000_000


def test_gate2b_autopilot_stays_off_until_operator_starts_it(client, paper_session):
    headers = _headers(paper_session["token"])
    off = client.get("/api/v2/workspace/paper/state", headers=headers).json()
    assert off["autopilot"]["enabled"] is False

    started = client.post(
        "/api/v2/control/workspace/autopilot",
        json={"enabled": True},
        headers=headers,
    )
    assert started.status_code == 200
    assert started.json()["autopilot"]["enabled"] is True

    paused = client.post(
        "/api/v2/control/workspace/autopilot",
        json={"enabled": False},
        headers=headers,
    )
    assert paused.status_code == 200
    assert paused.json()["autopilot"]["enabled"] is False


def test_gate3_paper_and_live_routes_and_bundle(client):
    routes = [
        "/login",
        "/setup",
        "/live-unlock",
        "/paper",
        "/paper/dashboard",
        "/paper/orders",
        "/paper/analyzer",
        "/paper/quant",
        "/paper/brain",
        "/paper/forge",
        "/paper/ledger",
        "/paper/verification",
        "/paper/control",
        "/live",
        "/live/dashboard",
        "/live/orders",
        "/live/positions",
        "/live/quant",
        "/live/brain",
        "/live/ledger",
        "/live/control",
        # legacy routes must keep resolving
        "/dashboard",
        "/orders",
        "/ledger",
        "/control",
    ]
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, route
        assert "text/html" in response.headers.get("content-type", "")

    # API paths must never be swallowed by the SPA fallback.
    missing = client.get("/api/v2/does-not-exist")
    assert missing.status_code in (401, 403, 404)

    index_html = UI_ROOT / "dist" / "index.html"
    assert index_html.exists()
    bundle = index_html.read_text(encoding="utf-8")
    assert "<script" in bundle

    # The compiled source tree must lint clean (0 errors, 0 warnings).
    npx = shutil.which("npx")
    if npx is None or not (UI_ROOT / "node_modules").exists():  # pragma: no cover
        pytest.skip("node toolchain is not installed in this checkout")
    completed = subprocess.run(
        f'"{npx}" biome lint src/',
        cwd=str(UI_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        shell=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    combined = f"{completed.stdout}{completed.stderr}".lower()
    assert "error" not in combined
    assert "warning" not in combined


def test_gate3b_workspace_state_contracts(client, paper_session):
    headers = _headers(paper_session["token"])
    paper = client.get("/api/v2/workspace/paper/state", headers=headers)
    assert paper.status_code == 200
    paper_body = paper.json()
    assert set(paper_body["shadow"]) >= {
        "shadow_fills",
        "required_fills",
        "remaining_fills",
        "graduated",
        "live_fills",
    }
    assert "double_entry" in paper_body["ledger"]
    assert "autopilot" in paper_body

    # The SPA bundle must ship the dedicated workspace chunks.
    assets = list((UI_ROOT / "dist" / "assets").glob("*.js"))
    assert assets
    joined = "".join(path.read_text(encoding="utf-8") for path in assets)
    for marker in ("PAPER SANDBOX", "Live Production", "Clean Slate"):
        assert marker.lower() in joined.lower(), marker
