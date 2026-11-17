"""NIOS V2.1 -- Suite 36: ``G11`` unified session + CSRF, ``G12`` reconciliation control.

Covers:

* ``G11`` -- the operator session is accepted through the ``X-NIOS-Session`` header
  **and** the ``HttpOnly; SameSite=Strict`` ``nios_session`` cookie; a cookie-authenticated
  mutation additionally costs a signed double-submit ``X-NIOS-CSRF`` header, while
  pre-session bootstrap paths and the documented read-only MCP bridge stay exempt;
  ``GET /api/v2/auth/session`` reports the tier, TTL and expiry telemetry.
* ``G12`` -- ``GET /api/v2/reconciliation/status``, ``POST /api/v2/control/reconciliation/run``
  and ``POST /api/v2/control/reconciliation/clear-freeze``: a rogue venue position
  freezes its silo, a cold-start freeze is clearable with a LIVE-elevated session and an
  operator note, and an ADR-009 loss stop is **not** clearable here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ring3_cockpit.gateway.v2_1_control import (  # noqa: E402
    CSRF_EXEMPT_PATHS,
    CSRF_HEADER,
    csrf_token_for,
)

OPERATOR = {"username": "sovereign", "password": "correct-horse-battery", "live_pin": "7314"}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime(tmp_path):
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


@pytest.fixture()
def token(client) -> str:
    created = client.post("/api/v2/auth/setup", json=OPERATOR)
    assert created.status_code == 200, created.text
    value = created.json()["token"]
    assert value
    return value


def _csrf_payload() -> dict:
    return {"code": "TEST_ALERT", "severity": "INFO", "message": "suite 36"}


# ---------------------------------------------------------------------------
# G11 -- session transports
# ---------------------------------------------------------------------------


def test_header_transport_needs_no_csrf(client, token) -> None:
    # The header transport cannot be produced by a cross-site form post.
    posted = client.post(
        "/api/v2/alerts", json=_csrf_payload(), headers={"X-NIOS-Session": token}
    )
    assert posted.status_code == 200, posted.text


def test_cookie_transport_requires_the_csrf_header(client, token) -> None:
    client.cookies.set("nios_session", token)
    try:
        blocked = client.post("/api/v2/alerts", json=_csrf_payload())
        assert blocked.status_code == 403, blocked.text
        detail = blocked.json()
        assert detail["detail"] == "CSRF_TOKEN_MISSING_OR_INVALID"
        assert detail["csrf_header"] == CSRF_HEADER

        accepted = client.post(
            "/api/v2/alerts",
            json=_csrf_payload(),
            headers={CSRF_HEADER: csrf_token_for(token)},
        )
        assert accepted.status_code == 200, accepted.text
    finally:
        client.cookies.clear()


def test_cookie_transport_rejects_a_wrong_csrf_value(client, token) -> None:
    client.cookies.set("nios_session", token)
    try:
        blocked = client.post(
            "/api/v2/alerts",
            json=_csrf_payload(),
            headers={CSRF_HEADER: "0" * 32},
        )
        assert blocked.status_code == 403, blocked.text
    finally:
        client.cookies.clear()


def test_cookie_transport_can_still_read(client, token) -> None:
    client.cookies.set("nios_session", token)
    try:
        # A safe method is never CSRF-checked.
        assert client.get("/api/v2/alerts").status_code == 200
        assert client.get("/api/v2/auth/session").status_code == 200
    finally:
        client.cookies.clear()


def test_read_only_mcp_bridge_stays_cookie_exempt(client, token) -> None:
    """The V1.9 cookie-only MCP call is a documented read-only path."""
    assert "/api/v2/mcp/call" in CSRF_EXEMPT_PATHS
    client.cookies.set("nios_session", token)
    try:
        call = {"tool": "cockpit_status", "arguments": {}}
        response = client.post("/api/v2/mcp/call", json=call)
        # 200 when the bridge accepts the tool, 422 when the name is unknown: both
        # prove the CSRF gate did not intercept a cookie-only call.
        assert response.status_code != 403, response.text
    finally:
        client.cookies.clear()


def test_auth_session_reports_tier_ttl_and_csrf(client, token) -> None:
    body = client.get(
        "/api/v2/auth/session", headers={"X-NIOS-Session": token}
    ).json()

    assert body["page"] == "auth.session"
    assert body["authenticated"] is True
    assert body["initialized"] is True
    assert body["username"] == "sovereign"
    assert body["mode"] in ("PAPER", "LIVE")
    assert body["ttl_remaining_s"] is not None and body["ttl_remaining_s"] > 0
    assert body["expires_at_utc"] and "T" in body["expires_at_utc"]
    assert body["expires_at_utc_ns"] > 0
    assert body["csrf_token"] == csrf_token_for(token)
    assert body["transport"]["header"] == "X-NIOS-Session"
    assert body["transport"]["cookie"] == "nios_session"
    assert body["transport"]["csrf_header"] == CSRF_HEADER


def test_auth_session_is_honest_when_unauthenticated(client) -> None:
    body = client.get("/api/v2/auth/session").json()
    assert body["authenticated"] is False
    assert body["mode"] == "NONE"
    assert body["ttl_remaining_s"] is None
    assert body["expires_at_utc"] is None
    assert body["csrf_token"] is None


def test_csrf_token_is_deterministic_and_session_bound() -> None:
    assert csrf_token_for("abc") == csrf_token_for("abc")
    assert csrf_token_for("abc") != csrf_token_for("abd")
    assert len(csrf_token_for("abc")) == 32


def test_cookie_written_by_login_is_httponly_and_samesite_strict(client) -> None:
    created = client.post("/api/v2/auth/setup", json=OPERATOR)
    raw = created.headers.get("set-cookie", "")
    assert "nios_session=" in raw
    assert "HttpOnly" in raw
    assert "SameSite=strict" in raw.lower().replace("samesite=strict", "Samesite=strict") or "samesite=strict" in raw.lower()


# ---------------------------------------------------------------------------
# G12 -- reconciliation control surface
# ---------------------------------------------------------------------------


def test_reconciliation_status_is_clean_on_a_fresh_install(client) -> None:
    body = client.get("/api/v2/reconciliation/status").json()
    assert body["page"] == "reconciliation.status"
    assert body["reconciler_attached"] is True
    assert body["report_count"] == 0
    assert body["frozen_silos"] == []
    assert body["last_report"] is None
    assert "LIVE-elevated" in body["clear_freeze_requires"]


def test_reconciliation_run_with_no_venue_positions_passes(client) -> None:
    body = client.post(
        "/api/v2/control/reconciliation/run", json={"dry_run": False}
    ).json()
    assert body["ran"] is True
    assert body["passed"] is True
    assert body["frozen_silos"] == []
    assert body["rows"] == []


def test_reconciliation_dry_run_does_not_classify(client) -> None:
    body = client.post(
        "/api/v2/control/reconciliation/run", json={"dry_run": True}
    ).json()
    assert body["ran"] is False
    assert body["dry_run"] is True


def test_rogue_venue_position_freezes_the_silo_then_operator_clears_it(
    client, runtime
) -> None:
    from ring0_vault.silos.connector_runtime import ConnectorRuntime

    def transport(op: str, request: dict) -> dict:
        if op == "fetch_positions":
            return {
                "positions": [
                    {
                        "symbol": "NVDA",
                        "contracts": 5.0,
                        "entryPrice": 180.0,
                        "markPrice": 181.5,
                    }
                ]
            }
        return {}

    runtime.control._connector_runtime = ConnectorRuntime(
        connector_mode="SANDBOX_TESTNET",
        allow_network=True,
        transports={"US_IBKR": transport},
    )

    ran = client.post("/api/v2/control/reconciliation/run", json={"dry_run": False})
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["ran"] is True
    assert body["passed"] is False
    assert body["frozen_silos"] == ["US_IBKR"]
    assert body["rows"][0]["classification"] == "ROGUE_MANUAL_POSITION"
    assert body["rows"][0]["freeze_required"] is True

    status = client.get("/api/v2/reconciliation/status").json()
    assert status["frozen_silos"] == ["US_IBKR"]
    assert "ROGUE" in status["freeze_reasons"]["US_IBKR"]
    assert status["report_count"] == 1

    # G9: the freeze also raised a durable incident.
    alerts = client.get("/api/v2/alerts").json()
    assert any(row["code"] == "COLD_START_RECON_FREEZE" for row in alerts["alerts"])

    cleared = client.post(
        "/api/v2/control/reconciliation/clear-freeze",
        json={"silo": "US_IBKR", "note": "confirmed manual block trade", "operator": "sovereign"},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cleared"] is True
    assert "ROGUE" in cleared.json()["previous_reason"]
    assert client.get("/api/v2/reconciliation/status").json()["frozen_silos"] == []
    assert runtime.control.guard.is_frozen("US_IBKR") is False


def test_clear_freeze_refuses_an_adr009_loss_stop(client, runtime) -> None:
    runtime.control.guard.freeze("US_IBKR", "ADR009_DAILY_LOSS_STOP")
    refused = client.post(
        "/api/v2/control/reconciliation/clear-freeze",
        json={"silo": "US_IBKR", "note": "please let me trade"},
    )
    assert refused.status_code == 409, refused.text
    assert "FREEZE_NOT_CLEARABLE_HERE" in refused.text
    assert runtime.control.guard.is_frozen("US_IBKR") is True
    runtime.control.guard.unfreeze("US_IBKR")


def test_clear_freeze_demands_an_operator_note(client) -> None:
    rejected = client.post(
        "/api/v2/control/reconciliation/clear-freeze", json={"silo": "US_IBKR", "note": ""}
    )
    assert rejected.status_code == 422, rejected.text


def test_clear_freeze_rejects_an_unknown_silo(client) -> None:
    rejected = client.post(
        "/api/v2/control/reconciliation/clear-freeze",
        json={"silo": "MARS_EXCHANGE", "note": "not a real venue"},
    )
    assert rejected.status_code == 422, rejected.text


def test_clear_freeze_on_an_unfrozen_silo_is_a_clean_no_op(client) -> None:
    body = client.post(
        "/api/v2/control/reconciliation/clear-freeze",
        json={"silo": "FX_MT5", "note": "nothing to clear"},
    ).json()
    assert body["cleared"] is False
    assert body["reason"] == "SILO_NOT_FROZEN"


def test_reconciliation_status_exposes_the_guard_snapshot(client, runtime) -> None:
    runtime.control.guard.freeze("CRYPTO_BINANCE", "COLD_START_DEADLINE_EXCEEDED")
    try:
        body = client.get("/api/v2/reconciliation/status").json()
        assert body["frozen_silos"] == ["CRYPTO_BINANCE"]
        assert body["freeze_reasons"]["CRYPTO_BINANCE"] == "COLD_START_DEADLINE_EXCEEDED"
    finally:
        runtime.control.guard.unfreeze("CRYPTO_BINANCE")
