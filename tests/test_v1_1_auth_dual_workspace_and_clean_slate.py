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


def test_gate3_paper_and_live_routes_and_headless_document(client):
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
        assert "headless-backend" in response.text
        assert "<script" not in response.text.lower()

    # API paths must never be swallowed by the fallback.
    missing = client.get("/api/v2/does-not-exist")
    assert missing.status_code in (401, 403, 404)

    index_html = UI_ROOT / "dist" / "index.html"
    assert index_html.exists()
    document = index_html.read_text(encoding="utf-8")
    assert "HEADLESS_API" in document
    assert "<script" not in document.lower()
    assert not (UI_ROOT / "package.json").exists()
    assert not (UI_ROOT / "src").exists()


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
    assert paper_body["workspace_mode"] == "PAPER"
    assert "payload_sha256" in paper_body

    # V1.6: the workspace contract is the API, with no frontend bundle.
    document = (UI_ROOT / "dist" / "index.html").read_text(encoding="utf-8")
    assert "HEADLESS_API" in document
    assert "<script" not in document.lower()
    assert not (UI_ROOT / "dist" / "assets").exists()


# ---------------------------------------------------------------------------
# Gate 4 -- LIVE mutation guard: a PAPER session can never move live capital
# ---------------------------------------------------------------------------

LIVE_MUTATIONS = [
    (
        "/api/v2/control/brokers",
        {"silo": "US_IBKR", "mode": "LIVE", "confirm_live": True},
    ),
    (
        "/api/v2/control/brokers/credentials",
        {
            "silo": "US_IBKR",
            "venue": "alpaca",
            "api_key": "guard-key-AAA1",
            "api_secret": "guard-secret-BBB2",
            "sandbox": True,
        },
    ),
    (
        "/api/v2/control/brokers/testnet-order",
        {
            "silo": "US_IBKR",
            "venue": "alpaca",
            "symbol": "NVDA",
            "side": "BUY",
            "qty": 10.0,
            "limit_price": 120.0,
            "sandbox": True,
            "confirm_sandbox": True,
        },
    ),
    (
        "/api/v2/control/vault/rotate-keys",
        {"operator": "pytest.v11.guard", "reason": "live mutation guard drill"},
    ),
]


def test_gate4_live_mutations_require_a_live_session(client, paper_session):
    """Once an operator exists, every live-mutating route demands ``tier=LIVE``.

    ``/api/v2/control/brokers`` (``mode=LIVE``), ``/brokers/credentials``,
    ``/brokers/testnet-order`` and ``/vault/rotate-keys`` are the four Ring 0
    surfaces where a Paper tab could otherwise escalate.  A configured operator
    turns each of them into a 401 (no session) / 403 (PAPER session) gate, and
    ``POST /api/v2/auth/elevate-live`` is the only way through.
    """
    paper_headers = _headers(paper_session["token"])

    # --- a PAPER session is refused on all four live mutations ----------
    for route, payload in LIVE_MUTATIONS:
        denied = client.post(route, json=payload, headers=paper_headers)
        assert denied.status_code == 403, (route, denied.text)
        assert denied.json()["detail"] == "LIVE_ELEVATION_REQUIRED", route

    # --- no session at all is an authentication failure -----------------
    client.cookies.clear()
    for route, payload in LIVE_MUTATIONS:
        anonymous = client.post(route, json=payload)
        assert anonymous.status_code == 401, (route, anonymous.text)
        assert anonymous.json()["detail"] == "AUTH_REQUIRED", route

    # --- a PAPER-only mutation stays open (guard is scoped to LIVE) -----
    paper_mode = client.post(
        "/api/v2/control/brokers",
        json={"silo": "US_IBKR", "mode": "PAPER"},
        headers=paper_headers,
    )
    assert paper_mode.status_code == 200, paper_mode.text
    assert paper_mode.json()["broker"]["mode"] == "PAPER"

    # --- the PIN is the only door to the live surface -------------------
    login = client.post(
        "/api/v2/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    elevated = client.post(
        "/api/v2/auth/elevate-live",
        json={"live_pin": LIVE_PIN},
        headers=_headers(login.json()["token"]),
    )
    assert elevated.status_code == 200, elevated.text
    live_headers = _headers(elevated.json()["token"])
    assert elevated.json()["tier"] == "LIVE"

    # --- credentials + key rotation now succeed -------------------------
    stored = client.post(
        "/api/v2/control/brokers/credentials",
        json=LIVE_MUTATIONS[1][1],
        headers=live_headers,
    )
    assert stored.status_code == 200, stored.text

    rotated = client.post(
        "/api/v2/control/vault/rotate-keys",
        json=LIVE_MUTATIONS[3][1],
        headers=live_headers,
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["status"] == "ROTATED"

    # --- the sandbox dispatch clears the guard and signs a receipt ------
    dispatched = client.post(
        "/api/v2/control/brokers/testnet-order",
        json=LIVE_MUTATIONS[2][1],
        headers=live_headers,
    )
    assert dispatched.status_code == 200, dispatched.text
    receipt = dispatched.json()
    assert receipt["venue"] == "alpaca"
    assert receipt["order"]["symbol"] == "NVDA"

    # --- the LIVE cutover clears the guard (pre-flight still governs) ---
    cutover = client.post(
        "/api/v2/control/brokers",
        json=LIVE_MUTATIONS[0][1],
        headers=live_headers,
    )
    assert cutover.status_code in (200, 409), cutover.text
    detail = cutover.json().get("detail") if cutover.status_code == 409 else None
    if isinstance(detail, dict):
        assert detail.get("error") == "PRE_FLIGHT_GATE_BLOCKED"
    else:
        assert cutover.status_code == 200
    assert cutover.status_code != 403

    # Leave the runtime paper-only and unauthenticated again.
    back_to_paper = client.post(
        "/api/v2/control/brokers",
        json={"silo": "US_IBKR", "mode": "PAPER"},
        headers=live_headers,
    )
    assert back_to_paper.status_code == 200, back_to_paper.text
    assert back_to_paper.json()["broker"]["mode"] == "PAPER"

    logout = client.post("/api/v2/auth/logout", headers=live_headers)
    assert logout.status_code == 200
    after = client.post(
        "/api/v2/control/vault/rotate-keys",
        json=LIVE_MUTATIONS[3][1],
        headers=live_headers,
    )
    assert after.status_code == 401


# ---------------------------------------------------------------------------
# Gate 5 -- factory reset costs both factors once an operator exists
# ---------------------------------------------------------------------------


def test_gate5_auth_reset_requires_password_and_live_pin(client, paper_session):
    """``POST /api/v2/auth/reset`` is credential-gated, not merely confirm-gated.

    A reset deletes the credential file, the HMAC session key and every
    outstanding token, so a UI "confirm?" click is not proof of identity: it is
    the one Ring 3-reachable action that can lock the human operator out of
    their own install.  Once a vault exists the route therefore costs the same
    two factors as a LIVE elevation -- ``password`` (``scrypt`` digest) and
    ``live_pin`` (``pbkdf2_hmac`` digest):

    * no factor / one factor -> ``401 AUTH_REQUIRED``
    * both supplied, any wrong -> ``403 INVALID_CREDENTIALS``
    * both correct -> ``200`` and the vault really is erased
    * clean slate (no vault) -> ``200`` with no body, so first-run
      initialisation stays idempotent.

    Every rejected attempt must leave the credential intact; this test is last
    in the module because the accepted attempt destroys the operator.
    """
    headers = _headers(paper_session["token"])

    # --- nothing supplied: the caller never proved who it is ------------
    anonymous = client.post("/api/v2/auth/reset")
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.json()["detail"] == "AUTH_REQUIRED"

    # --- one factor is never enough for a destructive reset -------------
    for partial in (
        {"password": PASSWORD, "live_pin": ""},
        {"password": "", "live_pin": LIVE_PIN},
    ):
        missing = client.post("/api/v2/auth/reset", json=partial, headers=headers)
        assert missing.status_code == 401, (partial, missing.text)
        assert missing.json()["detail"] == "AUTH_REQUIRED"

    # --- both supplied, either wrong: authenticated but rejected --------
    for wrong in (
        {"password": "wrong-password", "live_pin": LIVE_PIN},
        {"password": PASSWORD, "live_pin": "000000"},
    ):
        rejected = client.post("/api/v2/auth/reset", json=wrong, headers=headers)
        assert rejected.status_code == 403, (wrong, rejected.text)
        assert rejected.json()["detail"] == "INVALID_CREDENTIALS"

    # --- every rejection left the vault and the session intact ----------
    still_there = client.get("/api/v2/auth/status", headers=headers)
    assert still_there.status_code == 200
    assert still_there.json()["initialized"] is True
    assert still_there.json()["authenticated"] is True

    # --- the real pair performs the factory reset -----------------------
    done = client.post(
        "/api/v2/auth/reset",
        json={"password": PASSWORD, "live_pin": LIVE_PIN},
        headers=headers,
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["ok"] is True
    assert body["reset"] is True
    assert body["clean_slate_active"] is True
    assert body["credential"]["reset"] is True
    assert body["credential"]["removed"] is True
    assert body["credential"]["credential_verified"] is True

    # --- the credential and every session are gone ----------------------
    client.cookies.clear()
    after = client.get("/api/v2/auth/status", headers=headers)
    assert after.status_code == 200
    assert after.json()["initialized"] is False
    assert after.json()["authenticated"] is False
    assert after.json()["tier"] == "NONE"

    # --- a clean slate is idempotent: a bare reset still succeeds -------
    idempotent = client.post("/api/v2/auth/reset")
    assert idempotent.status_code == 200, idempotent.text
    assert idempotent.json()["ok"] is True
    assert idempotent.json()["credential"]["removed"] is False
