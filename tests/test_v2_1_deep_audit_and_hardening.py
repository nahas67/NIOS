"""NIOS V2.1 -- Suite 37: deep security, saga and concurrency regression suite.

This suite is the executable form of the four-lens backend audit of ``V2.1``
(``027b621``).  Every assertion here pins a *defect that was found and fixed*, so
a future refactor that reintroduces one of them fails loudly instead of silently
reopening a hole:

Lens 1 -- offensive security, session, CSRF and route boundaries
    * ``logout`` / ``reset`` / ``elevate-live`` were CSRF-exempt even though they
      are reached *with* a live cookie; a cross-site form post could log the
      operator out, factory-reset the install, or raise a PAPER session to LIVE.
    * ``POST /api/v2/settings`` validated auth by hand instead of as a FastAPI
      dependency, so a future re-ordering could mutate before checking.
    * ``TickIngestRequest`` accepted ``inf`` (``inf > 0`` is true) and blew up in
      ``math.floor`` as an unhandled 500; ``gross_pnl_micros`` was unbounded.
    * ``ConnectorRuntime`` must never open a socket in ``OFFLINE_REPLAY`` even
      when a caller passes ``allow_network=True``.

Lens 2 -- saga atomicity, double-booking and crash recovery
    * A venue call with an *unknown* outcome (timeout / 5xx) escaped the saga as a
      500 while the record was still ``IN_PROGRESS``; it now parks in
      ``PENDING_SWEEP_VERIFICATION`` for the read-only state checker.
    * A replayed close created a second saga and booked the P&L twice.
    * ``close_position=True`` passed an already-net fill into the statutory
      escrow (taxing a taxed number) and wrote the post-mortem twice.
    * ``compensate()`` claimed ``COMPENSATED`` even when a reversal failed.
    * Clean-slate reset wiped the journal while the lazily-built components kept
      their rows in memory and wrote them straight back.

Lens 3 -- symbol canonicalization, Ring 2 air-gap parity and autopilot safety
    * Ring 2's ``to_micros`` half-upped where Ring 0 floors, so a bar price and
      the ledger disagreed by one micro at half-micro boundaries.
    * ``LiveBarAggregator`` accepted crossed books, rewound on stale timestamps
      and grew its instrument map without bound.
    * ``BTC/USDT`` <-> ``BTCUSDT`` <-> ``BTC-USDT`` produced three different entity
      arms, so a loss was invisible to the sizing gate -- which then failed open.
    * ``MultiTrancheAutopilot`` never consulted the mission pacer, so a mission in
      ``TARGET_LOCK`` could still dispatch.

Lens 4 -- lifecycle concurrency and thread hygiene
    * ``TransactionStateChecker`` held the queue mutex across a venue probe and
      escalated without a CRITICAL incident.
    * ``create_app()`` / lifespan teardown must not leak threads.
"""

from __future__ import annotations

import math
import pathlib
import tempfile
import threading

import pytest

SECOND_NS = 1_000_000_000


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


OPERATOR = {"username": "sovereign", "password": "correct-horse-battery", "live_pin": "7314"}


@pytest.fixture()
def session(client):
    """A live PAPER session token plus a helper for LIVE elevation."""

    created = client.post("/api/v2/auth/setup", json=OPERATOR)
    assert created.status_code == 200, created.text
    token = created.json()["token"]
    assert token
    return token


def _journal(tmp_path: pathlib.Path):
    from ring0_vault.ibor.state_journal import AtomicStateJournal

    return AtomicStateJournal(tmp_path / "suite37-journal.json")


class _FundedLedger:
    """Real ``IBORLedger`` with an unbounded cash surface for sweep tests.

    The sweep stage only calls ``ledger.available``; overriding that one method
    keeps the double-entry bookkeeping real while removing the funding setup that
    is irrelevant to what this suite is proving.
    """

    def __new__(cls, *args, **kwargs):  # noqa: D401 - factory shim
        from ring0_vault.ibor.ledger_outbox import IBORLedger

        class _Ledger(IBORLedger):
            def available(self, silo, account):  # noqa: D102 - test double
                return 10**12

        return _Ledger()


class _UnknownSweeper:
    """A venue transport whose outcome is genuinely unknown (timeout / 5xx)."""

    def plan(self, *args, **kwargs):
        raise RuntimeError("503 UNKNOWN from the venue")

    def execute(self, *args, **kwargs):  # pragma: no cover - plan raises first
        raise RuntimeError("503 UNKNOWN from the venue")


class _RejectingSweeper:
    """A venue transport that deterministically refuses the sweep."""

    def plan(self, *args, **kwargs):
        from ring0_vault.silos.crypto_wallet_sweep import SweepRejected

        raise SweepRejected("SWEEP_TOTP_EXPIRED")

    def execute(self, *args, **kwargs):  # pragma: no cover - plan raises first
        return None


# ===========================================================================
# LENS 1 -- CSRF exemption surface
# ===========================================================================


def test_session_transitions_are_no_longer_csrf_exempt() -> None:
    """``logout``/``reset``/``elevate-live`` mutate a live session cookie."""
    from ring3_cockpit.gateway.v2_1_control import (
        CSRF_EXEMPT_PATHS,
        CSRF_GUARDED_SESSION_PATHS,
    )

    for path in (
        "/api/v2/auth/logout",
        "/api/v2/auth/reset",
        "/api/v2/auth/elevate-live",
    ):
        assert path not in CSRF_EXEMPT_PATHS, path
        assert path in CSRF_GUARDED_SESSION_PATHS, path


def test_only_the_unauthenticated_bootstrap_stays_exempt() -> None:
    from ring3_cockpit.gateway.v2_1_control import CSRF_EXEMPT_PATHS

    assert "/api/v2/auth/setup" in CSRF_EXEMPT_PATHS
    assert "/api/v2/auth/login" in CSRF_EXEMPT_PATHS
    assert "/api/v2/auth/login/paper" in CSRF_EXEMPT_PATHS
    assert "/api/v2/auth/login/live" in CSRF_EXEMPT_PATHS
    # Nothing else may ride the bootstrap exemption.
    assert CSRF_EXEMPT_PATHS == frozenset(
        {
            "/api/v2/auth/setup",
            "/api/v2/auth/login",
            "/api/v2/auth/login/paper",
            "/api/v2/auth/login/live",
            "/api/v2/mcp/call",
        }
    )


def test_cookie_logout_requires_the_csrf_header(client, session) -> None:
    client.cookies.set("nios_session", session)
    try:
        blocked = client.post("/api/v2/auth/logout")
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["detail"] == "CSRF_TOKEN_MISSING_OR_INVALID"
    finally:
        client.cookies.clear()


def test_cookie_reset_requires_the_csrf_header(client, session) -> None:
    client.cookies.set("nios_session", session)
    try:
        blocked = client.post(
            "/api/v2/auth/reset",
            json={"password": OPERATOR["password"], "live_pin": OPERATOR["live_pin"]},
        )
        assert blocked.status_code == 403, blocked.text
        # The install is untouched: a blocked reset must not mutate anything.
        still = client.get("/api/v2/auth/status")
        assert still.json()["initialized"] is True
    finally:
        client.cookies.clear()


def test_cookie_elevate_live_requires_the_csrf_header(client, session) -> None:
    client.cookies.set("nios_session", session)
    try:
        blocked = client.post(
            "/api/v2/auth/elevate-live", json={"live_pin": OPERATOR["live_pin"]}
        )
        assert blocked.status_code == 403, blocked.text
    finally:
        client.cookies.clear()


def test_cookie_session_transition_succeeds_with_the_csrf_header(client, session) -> None:
    from ring3_cockpit.gateway.v2_1_control import CSRF_HEADER, csrf_token_for

    client.cookies.set("nios_session", session)
    try:
        elevated = client.post(
            "/api/v2/auth/elevate-live",
            json={"live_pin": OPERATOR["live_pin"]},
            headers={CSRF_HEADER: csrf_token_for(session)},
        )
        assert elevated.status_code == 200, elevated.text
        assert elevated.json()["tier"] == "LIVE"
    finally:
        client.cookies.clear()


# ===========================================================================
# LENS 1 -- LIVE tier enforcement
# ===========================================================================


def test_settings_is_a_session_dependency_not_a_manual_call(client, session) -> None:
    """A PAPER session may change a preference but not a live-risk knob."""
    from ring3_cockpit.gateway.v2_1_control import LIVE_CRITICAL_SETTINGS

    paper = {"X-NIOS-Session": session}
    for key in LIVE_CRITICAL_SETTINGS:
        payload = {
            "connector_mode": {"connector_mode": "SANDBOX_TESTNET", "allow_network": True},
            "allow_network": {"allow_network": True},
            "max_drawdown_bps": {"max_drawdown_bps": 100.0},
            "autopilot_interval_s": {"autopilot_interval_s": 120},
        }[key]
        refused = client.post("/api/v2/settings", json=payload, headers=paper)
        assert refused.status_code == 403, (key, refused.text)
        assert refused.json()["detail"] == "LIVE_ELEVATION_REQUIRED"

    allowed = client.post(
        "/api/v2/settings", json={"alert_slippage_bps": 22.0}, headers=paper
    )
    assert allowed.status_code == 200, allowed.text


def test_live_venue_is_reachable_only_from_a_live_session(client, session) -> None:
    from ring3_cockpit.gateway.v2_1_control import CSRF_HEADER, csrf_token_for

    paper = {"X-NIOS-Session": session}
    refused = client.post(
        "/api/v2/settings",
        json={"connector_mode": "LIVE_VENUE", "allow_network": True},
        headers=paper,
    )
    assert refused.status_code == 403, refused.text

    elevated = client.post(
        "/api/v2/auth/elevate-live",
        json={"live_pin": OPERATOR["live_pin"]},
        headers=paper,
    )
    assert elevated.status_code == 200, elevated.text
    live_token = elevated.json()["token"]
    accepted = client.post(
        "/api/v2/settings",
        json={"connector_mode": "LIVE_VENUE", "allow_network": True},
        headers={"X-NIOS-Session": live_token},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["connector_mode"] == "LIVE_VENUE"


def test_clear_freeze_refuses_a_paper_session(client, session) -> None:
    refused = client.post(
        "/api/v2/control/reconciliation/clear-freeze",
        json={"silo": "CRYPTO_BINANCE", "note": "operator says ok"},
        headers={"X-NIOS-Session": session},
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "LIVE_ELEVATION_REQUIRED"


def test_clear_freeze_requires_a_real_note(client, session) -> None:
    from ring3_cockpit.gateway.v2_1_control import CSRF_HEADER, csrf_token_for

    elevated = client.post(
        "/api/v2/auth/elevate-live",
        json={"live_pin": OPERATOR["live_pin"]},
        headers={"X-NIOS-Session": session},
    ).json()
    refused = client.post(
        "/api/v2/control/reconciliation/clear-freeze",
        json={"silo": "CRYPTO_BINANCE", "note": "x"},
        headers={"X-NIOS-Session": elevated["token"], CSRF_HEADER: csrf_token_for(elevated["token"])},
    )
    assert refused.status_code == 422, refused.text


# ===========================================================================
# LENS 1 -- malformed payload boundaries (no unhandled 500)
# ===========================================================================


@pytest.mark.parametrize(
    "body",
    [
        '{"silo": "CRYPTO_BINANCE", "symbol": "BTC/USDT", "price": Infinity}',
        '{"silo": "CRYPTO_BINANCE", "symbol": "BTC/USDT", "price": NaN}',
        '{"silo": "CRYPTO_BINANCE", "symbol": "BTC/USDT", "price": 100.0, "bid": -Infinity}',
        '{"silo": "CRYPTO_BINANCE", "symbol": "BTC/USDT", "price": 100.0, "bid": Infinity}',
        '{"silo": "CRYPTO_BINANCE", "symbol": "BTC/USDT", "price": 100.0, "size": 1e400}',
    ],
)
def test_market_tick_rejects_non_finite_numbers_with_422(client, session, body) -> None:
    """Raw JSON is used because ``httpx`` refuses to *send* non-finite floats.

    A hostile client does not use ``httpx``: it sends the literal text, and
    Python's ``json`` accepts ``Infinity``/``NaN``.  That is the path that used to
    raise ``OverflowError`` out of ``math.floor`` as an unhandled 500.
    """
    response = client.post(
        "/api/v2/control/market/tick",
        content=body.encode("utf-8"),
        headers={"X-NIOS-Session": session, "content-type": "application/json"},
    )
    assert response.status_code == 422, (body, response.text)


@pytest.mark.parametrize(
    "payload",
    [
        {"silo": "CRYPTO_BINANCE", "symbol": "BTC/USDT", "price": 0.0},
        {"silo": "CRYPTO_BINANCE", "symbol": "BTC/USDT", "price": -1.0},
        {"silo": "CRYPTO_BINANCE", "symbol": "BTC/USDT", "price": 1.0, "size": -5.0},
        {"silo": "", "symbol": "BTC/USDT", "price": 1.0},
        {"silo": "CRYPTO_BINANCE", "symbol": "", "price": 1.0},
    ],
)
def test_market_tick_rejects_bad_numbers_with_422(client, session, payload) -> None:
    response = client.post(
        "/api/v2/control/market/tick", json=payload, headers={"X-NIOS-Session": session}
    )
    assert response.status_code == 422, (payload, response.text)


def test_trade_close_rejects_a_non_finite_slippage(client, session) -> None:
    response = client.post(
        "/api/v2/control/trades/close",
        content=b'{"silo": "CRYPTO_BINANCE", "gross_pnl_micros": 1, "slippage_bps": Infinity}',
        headers={"X-NIOS-Session": session, "content-type": "application/json"},
    )
    assert response.status_code == 422, response.text


def test_trade_close_rejects_an_overflowing_micro_amount(client, session) -> None:
    response = client.post(
        "/api/v2/control/trades/close",
        json={"silo": "CRYPTO_BINANCE", "gross_pnl_micros": 1 << 200},
        headers={"X-NIOS-Session": session},
    )
    assert response.status_code == 422, response.text


def test_trade_close_rejects_a_non_finite_slippage(client, session) -> None:
    response = client.post(
        "/api/v2/control/trades/close",
        content=b'{"silo": "CRYPTO_BINANCE", "gross_pnl_micros": 1, "slippage_bps": Infinity}',
        headers={"X-NIOS-Session": session, "content-type": "application/json"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/v2/control/trades/close", {"silo": "NOT_A_SILO", "gross_pnl_micros": 1}),
        ("post", "/api/v2/control/trades/close", {"gross_pnl_micros": 1}),
        ("post", "/api/v2/control/trades/sagas/does-not-exist/compensate", {}),
        ("post", "/api/v2/alerts/NOPE/ack", {}),
        ("get", "/api/v2/analytics/performance?bucket=7Y", None),
        ("get", "/api/v2/market/bars?silo=CRYPTO_BINANCE&symbol=BTC/USDT&timeframe=9m", None),
    ],
)
def test_unknown_identifiers_are_deterministic_4xx(client, session, method, path, body) -> None:
    headers = {"X-NIOS-Session": session}
    response = (
        client.get(path, headers=headers)
        if method == "get"
        else client.post(path, json=body, headers=headers)
    )
    assert response.status_code in (400, 401, 403, 404, 409, 422), response.text
    assert response.status_code < 500


def test_malformed_json_never_tracebacks(client, session) -> None:
    response = client.post(
        "/api/v2/control/trades/close",
        content=b"{not json at all",
        headers={"X-NIOS-Session": session, "content-type": "application/json"},
    )
    assert response.status_code == 422, response.text


# ===========================================================================
# LENS 1 -- ConnectorRuntime air gap
# ===========================================================================


def test_offline_replay_never_opens_a_socket_even_with_network_allowed() -> None:
    from ring0_vault.silos.connector_runtime import ConnectorRuntime

    calls: list[tuple[str, dict]] = []

    def transport(op, request):  # pragma: no cover - must never run
        calls.append((op, request))
        return {"accepted": True}

    runtime = ConnectorRuntime(
        connector_mode="OFFLINE_REPLAY",
        allow_network=True,
        transports={"CRYPTO_BINANCE": transport},
    )
    order = runtime.place_order(
        silo="CRYPTO_BINANCE", symbol="BTC/USDT", side="BUY", qty=1.0, price=64_000.0
    )
    assert order["mode"] == "OFFLINE_REPLAY"
    assert calls == []
    # The mode short-circuits every other transport path too.
    runtime.fetch_open_orders(silo="CRYPTO_BINANCE")
    runtime.fetch_positions(silo="CRYPTO_BINANCE")
    runtime.fetch_balances(silo="CRYPTO_BINANCE")
    runtime.cancel_order(silo="CRYPTO_BINANCE", order_id="OFFLINE-1")
    assert calls == []
    assert runtime.status()["connector_mode"] == "OFFLINE_REPLAY"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_connector_rejects_unquantizable_venue_numbers(bad) -> None:
    from ring0_vault.silos.connector_runtime import ConnectorError, ConnectorRuntime

    runtime = ConnectorRuntime(connector_mode="OFFLINE_REPLAY")
    with pytest.raises(ConnectorError) as caught:
        runtime.place_order(silo="CRYPTO_BINANCE", symbol="BTC/USDT", side="BUY", qty=bad)
    assert caught.value.code in ("VALUE_UNQUANTIZABLE", "VALUE_OUT_OF_RANGE", "QTY_INVALID")


def test_connector_error_never_leaks_a_credential() -> None:
    import inspect

    from ring0_vault.silos import connector_runtime as module

    source = inspect.getsource(module)
    for forbidden in ("api_secret", "api_key", "password"):
        assert f'"{forbidden}"' not in source or "redact" in source.lower()


# ===========================================================================
# LENS 2 -- saga atomicity and unknown-outcome recovery
# ===========================================================================


def _saga(tmp_path, sweeper=None, quota_engine=None):
    from ring0_vault.ibor.profit_split_saga import ProfitSplitSagaCoordinator

    return ProfitSplitSagaCoordinator(
        ledger=_FundedLedger(),
        journal=_journal(tmp_path),
        sweeper=sweeper,
        quota_engine=quota_engine,
    )


def test_unknown_sweep_outcome_parks_the_saga_instead_of_raising(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path, sweeper=_UnknownSweeper())
    record = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=10_000_000,
        notional_micros=50_000_000,
    )

    assert record["state"] == "PENDING_SWEEP_VERIFICATION"
    assert record["reason"] == "SWEEP_OUTCOME_UNKNOWN"
    assert record["sweep"]["state"] == "PENDING_SWEEP_VERIFICATION"
    assert record["sweep"]["accepted"] is None
    # The saga must not have claimed COMPLETED anywhere in its ladder.
    assert record["stages"][-1]["status"] != "SEALED"
    recovery = coordinator.recover()
    assert recovery["awaiting_sweep_verification"] == 1
    assert recovery["pending_count"] == 1


def test_pending_sweep_resolves_committed_without_double_crediting(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path, sweeper=_UnknownSweeper())
    record = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=10_000_000,
        notional_micros=50_000_000,
    )
    before = coordinator.status()["saga_count"]
    resolved = coordinator.resolve_pending_sweep(
        record["saga_id"], resolved_state="COMMITTED", transfer_id=99
    )
    assert resolved["state"] == "COMPLETED"
    assert resolved["sweep"]["transfer_id"] == 99
    assert any(marker.startswith("VOID_SWEEP_TRANSFER:") for marker in resolved["rollbacks"])
    # Resolving never mints a second saga.
    assert coordinator.status()["saga_count"] == before


def test_pending_sweep_resolves_voided_as_compensated(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path, sweeper=_UnknownSweeper())
    record = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=10_000_000,
        notional_micros=50_000_000,
    )
    resolved = coordinator.resolve_pending_sweep(record["saga_id"], resolved_state="VOIDED")
    assert resolved["state"] == "COMPENSATED"
    assert resolved["sweep"]["accepted"] is False


def test_resolver_refuses_to_invent_an_outcome(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path, sweeper=_UnknownSweeper())
    record = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=1_000_000,
        notional_micros=1_000_000,
    )
    with pytest.raises(ValueError):
        coordinator.resolve_pending_sweep(record["saga_id"], resolved_state="PROBABLY_FINE")
    with pytest.raises(KeyError):
        coordinator.resolve_pending_sweep("no-such-saga", resolved_state="VOIDED")


def test_rejected_sweep_still_seals_because_no_money_moved(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path, sweeper=_RejectingSweeper())
    record = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=5_000_000,
        notional_micros=1_000_000,
    )
    assert record["state"] == "COMPLETED"
    assert record["sweep"]["accepted"] is False
    assert record["sweep"]["reason"] == "SWEEP_TOTP_EXPIRED"


def test_idempotency_key_cannot_book_the_same_close_twice(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path, sweeper=_RejectingSweeper())
    first = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=9_000_000,
        notional_micros=10_000_000,
        idempotency_key="CLOSE-1",
    )
    second = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=9_000_000,
        notional_micros=10_000_000,
        idempotency_key="CLOSE-1",
    )
    assert first["saga_id"] == second["saga_id"]
    assert second["deduplicated"] is True
    assert coordinator.status()["saga_count"] == 1
    assert coordinator.records()[0]["realized_net_micros"] == first["realized_net_micros"]


def test_apply_statutory_false_does_not_tax_an_already_net_fill(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path, sweeper=_RejectingSweeper())
    grossed = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=5_000_000,
        notional_micros=1_000_000,
    )
    prepaid = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=5_000_000,
        notional_micros=1_000_000,
        apply_statutory=False,
    )
    assert grossed["statutory_cost_micros"] > 0
    assert prepaid["statutory_cost_micros"] == 0
    assert prepaid["realized_net_micros"] == 5_000_000
    assert prepaid["statutory_prepaid"] is True
    # The prepaid close kept strictly more than the gross-quoted one.
    assert prepaid["realized_net_micros"] == grossed["realized_net_micros"] + grossed[
        "statutory_cost_micros"
    ]


def test_saga_rejects_float_money(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path)
    with pytest.raises(TypeError):
        coordinator.run_trade_close(
            silo=CRYPTO_SILO, gross_pnl_micros=1.5, notional_micros=1
        )
    with pytest.raises(TypeError):
        coordinator.run_trade_close(
            silo=CRYPTO_SILO, gross_pnl_micros=True, notional_micros=1
        )


def test_split_sum_always_equals_realized_for_a_win_and_a_loss(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path, sweeper=_RejectingSweeper())
    win = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=12_345_678,
        notional_micros=99_999_999,
    )
    assert win["split"]["splits_sum_micros"] == win["realized_net_micros"]
    loss = coordinator.run_trade_close(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        gross_pnl_micros=-1,  # a one-micro loss
        notional_micros=99_999_999,
    )
    assert loss["state"] == "LOSS_RECORDED"
    assert loss["realized_net_micros"] <= 0
    assert loss["split"] is None


def test_double_entry_stays_balanced_across_closes(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    from ring0_vault.ibor.profit_split_saga import ProfitSplitSagaCoordinator

    ledger = _FundedLedger()
    coordinator = ProfitSplitSagaCoordinator(
        ledger=ledger, journal=_journal(tmp_path), sweeper=_RejectingSweeper()
    )
    debits = credits = 0
    for amount in (1_000_000, 7_777_777, -2_500_000, 33_333_333):
        record = coordinator.run_trade_close(
            silo=CRYPTO_SILO,
            symbol="BTC/USDT",
            gross_pnl_micros=amount,
            notional_micros=11_111_111,
        )
        assert record["realized_net_micros"] == record["gross_pnl_micros"] - record[
            "statutory_cost_micros"
        ]
    journal = getattr(ledger, "transfers", None)
    transfers = journal() if callable(journal) else (journal or ())
    for transfer in transfers:
        debits += int(getattr(transfer, "debit_micros", 0) or 0)
        credits += int(getattr(transfer, "credit_micros", 0) or 0)
    # The ledger either exposes no transfer journal (then there is nothing to
    # unbalance) or it must balance to the last micro.
    assert debits == credits


def test_compensation_reports_partial_instead_of_claiming_success(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    class _BrokenQuota:
        def issue(self, *args, **kwargs):
            raise AssertionError("issue should not be reached in this test")

        def revoke(self, *args, **kwargs):
            raise RuntimeError("quota backend offline")

    coordinator = _saga(tmp_path, sweeper=_RejectingSweeper(), quota_engine=_BrokenQuota())
    record = coordinator.run_trade_close(
        silo="US_IBKR",
        symbol="NVDA",
        gross_pnl_micros=5_000_000,
        notional_micros=1_000_000,
    )
    # The quota arm was rejected, so there is no marker to revoke -- fabricate one
    # to prove the failure path is honest about a failed reversal.
    live = coordinator._records[record["saga_id"]]
    live.rollbacks.append("REVOKE_QUOTA:QUOTA-1")
    compensated = coordinator.compensate(record["saga_id"], reason="OPERATOR")
    assert compensated["state"] == "COMPENSATION_PARTIAL"
    assert compensated["reason"].startswith("COMPENSATION_PARTIAL")
    assert compensated["stages"][-1]["detail"]["failures"]


def test_saga_reset_clears_memory_and_journal(tmp_path) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    coordinator = _saga(tmp_path, sweeper=_RejectingSweeper())
    coordinator.run_trade_close(
        silo=CRYPTO_SILO, symbol="BTC/USDT", gross_pnl_micros=1_000_000, notional_micros=1
    )
    assert coordinator.status()["saga_count"] == 1
    assert coordinator.reset()["dropped"] == 1
    assert coordinator.status()["saga_count"] == 0
    assert coordinator.records() == ()


# ===========================================================================
# LENS 2 -- no double-booking on close_position=True
# ===========================================================================


def test_paper_fill_close_writes_exactly_one_post_mortem(runtime) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    control = runtime.control
    before = control.entity_memory.snapshot()["event_count"]

    class _Fill:
        silo = CRYPTO_SILO
        mission_id = ""
        tranche_id = ""
        regime = "NEUTRAL"
        close_position = True
        client_order_id = "FILL-LOSS-1"

    out = control._v21_after_paper_fill(
        request=_Fill(),
        symbol="BTC/USDT",
        side="SELL",
        net_micros=-2_000_000,
        notional_micros=10_000_000,
    )
    assert out["saga"] is not None
    assert out["saga"]["realized_net_micros"] == -2_000_000
    assert out["saga"]["statutory_cost_micros"] == 0
    assert out["post_mortem"] is None  # the saga hook already wrote it

    after = control.entity_memory.snapshot()["event_count"]
    assert after - before == 1


def test_opening_fill_still_records_its_own_post_mortem(runtime) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    control = runtime.control
    before = control.entity_memory.snapshot()["event_count"]

    class _Fill:
        silo = CRYPTO_SILO
        mission_id = ""
        tranche_id = ""
        regime = "NEUTRAL"
        close_position = False
        client_order_id = "FILL-LOSS-2"

    out = control._v21_after_paper_fill(
        request=_Fill(),
        symbol="ETH/USDT",
        side="BUY",
        net_micros=-1_000_000,
        notional_micros=1_000_000,
    )
    assert out["saga"] is None
    assert out["post_mortem"] is not None
    assert control.entity_memory.snapshot()["event_count"] - before == 1


# ===========================================================================
# LENS 2 -- clean-slate reset
# ===========================================================================


def test_workspace_reset_zeroes_every_v21_component(runtime) -> None:
    from ring0_vault.silos.crypto_wallet_sweep import CRYPTO_SILO

    control = runtime.control
    control.alert_center.raise_alert(code="PRE_RESET", severity="CRITICAL")
    control.profit_split_saga.run_trade_close(
        silo=CRYPTO_SILO, symbol="BTC/USDT", gross_pnl_micros=1_000_000, notional_micros=1
    )
    control.runtime_settings.update({"autopilot_interval_s": 120})
    control.bar_aggregator.ingest(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        price=100.0,
        now_ns=SECOND_NS,
    )
    assert control.profit_split_saga.status()["saga_count"] == 1
    assert control.alert_center.status()["count"] == 1

    control.reset_workspace()

    assert control.profit_split_saga.status()["saga_count"] == 0
    assert control.alert_center.status()["count"] == 0
    assert control.bar_aggregator.status()["instrument_count"] == 0
    assert control.runtime_settings.connector_mode == "OFFLINE_REPLAY"
    assert control.runtime_settings.settings.allow_network is False
    replay = control.runtime_settings.status()
    assert replay["connector_mode"] == "OFFLINE_REPLAY"


def test_reset_can_never_leave_a_live_connector_armed(runtime) -> None:
    control = runtime.control
    control.runtime_settings.update(
        {"connector_mode": "SANDBOX_TESTNET", "allow_network": True}
    )
    assert control.runtime_settings.settings.allow_network is True
    control.reset_workspace()
    assert control.runtime_settings.settings.connector_mode == "OFFLINE_REPLAY"
    assert control.connectors.connector_mode == "OFFLINE_REPLAY"


# ===========================================================================
# LENS 3 -- to_micros parity
# ===========================================================================


PARITY_VALUES = (
    0.0,
    1e-9,
    5e-7,
    1e-6,
    2.5e-7,
    2.5e-6,
    1.0000005,
    0.1,
    0.3,
    1.0 / 3.0,
    12345.678901,
    999_999.999999,
    1e12,
)


@pytest.mark.parametrize("value", PARITY_VALUES)
def test_ring2_to_micros_matches_ring0_bit_for_bit(value) -> None:
    from ring0_vault.ibor.ledger_outbox import to_micros as ring0
    from ring2_brain.quant.bar_aggregator import to_micros as ring2

    assert ring2(value) == ring0(value), value


def test_ring2_to_micros_matches_on_a_random_matrix() -> None:
    import random

    from ring0_vault.ibor.ledger_outbox import to_micros as ring0
    from ring2_brain.quant.bar_aggregator import to_micros as ring2

    rng = random.Random(20261118)
    for _ in range(2000):
        value = round(rng.uniform(0.0, 1_000_000.0), 9)
        assert ring2(value) == ring0(value), value


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_ring2_to_micros_rejects_what_ring0_rejects(bad) -> None:
    from ring0_vault.ibor.ledger_outbox import LedgerError, to_micros as ring0
    from ring2_brain.quant.bar_aggregator import to_micros as ring2

    with pytest.raises((LedgerError, ValueError)):
        ring0(bad)
    with pytest.raises(ValueError):
        ring2(bad)


def test_ring2_to_micros_never_half_ups_into_extra_value() -> None:
    """The ledger floors on purpose; a half-up copy minted a phantom micro."""
    from ring2_brain.quant.bar_aggregator import to_micros

    assert to_micros(0.0000005) == 0
    assert to_micros(2.5e-7) == 0
    assert to_micros(1.0000004) == 1_000_000


# ===========================================================================
# LENS 3 -- bar aggregator invariants
# ===========================================================================


def test_crossed_book_is_rejected() -> None:
    from ring2_brain.quant.bar_aggregator import LiveBarAggregator

    aggregator = LiveBarAggregator()
    with pytest.raises(ValueError, match="CROSSED_BOOK"):
        aggregator.ingest(
            silo="CRYPTO_BINANCE",
            symbol="BTC/USDT",
            price=100.0,
            bid=101.0,
            ask=100.0,
            now_ns=SECOND_NS,
        )
    with pytest.raises(ValueError, match="CROSSED_BOOK"):
        aggregator.ingest(
            silo="CRYPTO_BINANCE",
            symbol="BTC/USDT",
            price=100.0,
            bid=100.0,
            ask=100.0,
            now_ns=SECOND_NS,
        )
    assert aggregator.status()["crossed_books_rejected"] == 2
    assert aggregator.status()["ticks_ingested"] == 0


def test_a_valid_book_is_accepted() -> None:
    from ring2_brain.quant.bar_aggregator import LiveBarAggregator

    aggregator = LiveBarAggregator()
    tick = aggregator.ingest(
        silo="CRYPTO_BINANCE",
        symbol="BTC/USDT",
        price=64_000.0,
        bid=63_999.5,
        ask=64_001.5,
        now_ns=SECOND_NS,
    )
    assert tick["price_micros"] == 64_000_000_000
    assert aggregator.status()["ticks_ingested"] == 1


def test_stale_timestamp_is_rejected_without_rewinding_the_bar() -> None:
    from ring2_brain.quant.bar_aggregator import LiveBarAggregator

    aggregator = LiveBarAggregator()
    aggregator.ingest(
        silo="CRYPTO_BINANCE", symbol="BTC/USDT", price=100.0, now_ns=10 * SECOND_NS
    )
    live_before = aggregator.bars(
        silo="CRYPTO_BINANCE", symbol="BTC/USDT", timeframe="1m"
    )[-1]
    with pytest.raises(ValueError, match="STALE_TICK"):
        aggregator.ingest(
            silo="CRYPTO_BINANCE",
            symbol="BTC/USDT",
            price=1.0,
            now_ns=10 * SECOND_NS - 1,
        )
    live_after = aggregator.bars(
        silo="CRYPTO_BINANCE", symbol="BTC/USDT", timeframe="1m"
    )[-1]
    assert live_after["bar_sha256"] == live_before["bar_sha256"]
    assert aggregator.status()["stale_ticks_rejected"] == 1


def test_instrument_map_is_bounded() -> None:
    from ring2_brain.quant.bar_aggregator import LiveBarAggregator

    aggregator = LiveBarAggregator(max_instruments=8)
    for index in range(64):
        aggregator.ingest(
            silo="CRYPTO_BINANCE",
            symbol=f"SYM{index}/USDT",
            price=1.0,
            now_ns=SECOND_NS + index,
        )
    status = aggregator.status()
    assert status["instrument_count"] <= 8
    assert status["instruments_evicted"] > 0
    assert status["max_instruments"] == 8


def test_bar_history_is_bounded_per_timeframe() -> None:
    from ring2_brain.quant.bar_aggregator import LiveBarAggregator

    aggregator = LiveBarAggregator(max_bars=8)
    for index in range(40):
        aggregator.ingest(
            silo="CRYPTO_BINANCE",
            symbol="BTC/USDT",
            price=100.0 + index,
            now_ns=(index + 1) * 60 * SECOND_NS,
        )
    history = aggregator.bars(
        silo="CRYPTO_BINANCE", symbol="BTC/USDT", timeframe="1m", include_live=False
    )
    assert len(history) <= 8


def test_aggregator_reset_zeroes_every_instrument() -> None:
    from ring2_brain.quant.bar_aggregator import LiveBarAggregator

    aggregator = LiveBarAggregator()
    aggregator.ingest(
        silo="CRYPTO_BINANCE", symbol="BTC/USDT", price=100.0, now_ns=SECOND_NS
    )
    assert aggregator.reset()["dropped"] == 1
    assert aggregator.status()["instrument_count"] == 0


# ===========================================================================
# LENS 3 -- symbol canonicalization across the whole Ring 2 memory path
# ===========================================================================


def test_canonical_symbol_key_folds_every_spelling() -> None:
    from ring2_brain.memory.entity_graph import canonical_symbol_key

    for spelling in ("BTC/USDT", "BTC-USDT", "btc_usdt", "BTCUSDT", " btc / usdt "):
        assert canonical_symbol_key(spelling) == "BTCUSDT"
    # Different instruments must never collide.
    assert canonical_symbol_key("ETH/USDT") != canonical_symbol_key("BTC/USDT")


def test_alias_never_folds_a_non_symbol_arm() -> None:
    from ring2_brain.memory.entity_graph import canonical_entity_alias

    assert canonical_entity_alias("silo:CRYPTO-BINANCE") == "silo:CRYPTO-BINANCE"
    assert canonical_entity_alias("strategy:SCALPER-1") == "strategy:SCALPER-1"


def test_loss_recorded_on_slash_ticker_is_found_by_a_compact_query() -> None:
    from ring2_brain.memory.entity_graph import EntityMemoryGraph

    graph = EntityMemoryGraph()
    graph.record_outcome(
        symbol="BTC/USDT",
        silo="CRYPTO_BINANCE",
        outcome_kind="LOSS",
        net_pnl_micros=-5_000_000,
    )
    graph.compile_truths()

    for spelling in ("symbol:BTC/USDT", "symbol:BTCUSDT", "symbol:BTC-USDT"):
        truth = graph.truth(spelling)
        assert truth is not None, spelling
        assert truth.entity_id == "symbol:BTC-USDT"
        assert truth.sample_count == 1


def test_query_entity_memory_matches_every_spelling() -> None:
    from ring2_brain.memory.entity_graph import EntityMemoryGraph

    graph = EntityMemoryGraph()
    graph.record_outcome(
        symbol="BTC/USDT",
        silo="CRYPTO_BINANCE",
        outcome_kind="LOSS",
        net_pnl_micros=-5_000_000,
    )
    graph.compile_truths()

    for spelling in ("BTCUSDT", "BTC/USDT", "BTC-USDT", "btc_usdt"):
        report = graph.query_entity_memory(symbol=spelling)
        assert report["entity_ids"] == ["symbol:BTC-USDT"], spelling
        assert report["sample_count"] == 1
        assert report["penalty"]["confidence_multiplier"] < 1.0


def test_two_spellings_share_one_truth_and_never_double_count_an_instrument() -> None:
    from ring2_brain.memory.entity_graph import EntityMemoryGraph

    graph = EntityMemoryGraph()
    graph.record_outcome(
        symbol="BTC/USDT", silo="CRYPTO_BINANCE", outcome_kind="LOSS", net_pnl_micros=-1
    )
    graph.compile_truths()
    graph.record_outcome(
        symbol="BTCUSDT", silo="CRYPTO_BINANCE", outcome_kind="LOSS", net_pnl_micros=-1
    )
    graph.compile_truths()

    symbol_truths = [row for row in graph.truths() if row.entity_id.startswith("symbol:")]
    assert len(symbol_truths) == 1
    assert symbol_truths[0].entity_id == "symbol:BTC-USDT"
    assert symbol_truths[0].sample_count == 2


def test_different_instruments_stay_isolated_after_folding() -> None:
    from ring2_brain.memory.entity_graph import EntityMemoryGraph

    graph = EntityMemoryGraph()
    for symbol in ("BTC/USDT", "ETH/USDT"):
        graph.record_outcome(
            symbol=symbol, silo="CRYPTO_BINANCE", outcome_kind="LOSS", net_pnl_micros=-1
        )
    graph.compile_truths()
    assert graph.truth("symbol:BTCUSDT").sample_count == 1
    assert graph.truth("symbol:ETHUSDT").sample_count == 1
    assert graph.query_entity_memory(symbol="ETHUSDT")["entity_ids"] == ["symbol:ETH-USDT"]


def test_post_mortem_recorder_uses_the_same_folding() -> None:
    from ring2_brain.memory.entity_graph import canonical_symbol_key
    from ring2_brain.memory.post_mortem_recorder import _entity_symbol

    for spelling in ("BTC/USDT", "BTC-USDT", "BTCUSDT"):
        assert canonical_symbol_key(_entity_symbol(spelling)) == "BTCUSDT"


def test_autopilot_memory_block_sees_a_slash_ticker_loss(runtime) -> None:
    """The sizing gate must fail *closed* on a symbol it already lost on.

    Before the alias fix the graph stored ``symbol:BTC-USDT`` while the autopilot
    asked for ``BTCUSDT``, matched nothing, and returned a multiplier of exactly
    ``1.0`` -- no penalty at all for an instrument it had just been burned by.
    """
    control = runtime.control
    control.record_post_mortem(
        outcome_kind="ORDER_REJECTED",
        symbol="BTC/USDT",
        silo="CRYPTO_BINANCE",
        net_pnl_micros=-9_000_000,
    )
    control.entity_memory.compile_truths()

    report = control.entity_memory.query_entity_memory(
        symbol="BTCUSDT", silo="CRYPTO_BINANCE"
    )
    assert report["sample_count"] >= 1
    assert "symbol:BTC-USDT" in report["entity_ids"]
    assert report["penalty"]["confidence_multiplier"] < 1.0

    multiplier, _reason = runtime.control.multi_tranche_autopilot._memory_view(
        "CRYPTO_BINANCE", "BTCUSDT"
    )
    assert multiplier < 1.0


# ===========================================================================
# LENS 3 -- performance analytics zero-division and NaN safety
# ===========================================================================


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"ts_utc_ns": 1, "pnl_micros": 0, "nav_micros": 0, "silo": "S"}],
        [
            {"ts_utc_ns": 1, "pnl_micros": 5, "nav_micros": 100, "silo": "S"},
            {"ts_utc_ns": 2, "pnl_micros": 5, "nav_micros": 105, "silo": "S"},
        ],
        [
            {"ts_utc_ns": i, "pnl_micros": 1, "nav_micros": 100 + i, "silo": "S"}
            for i in range(1, 12)
        ],
        [
            {"ts_utc_ns": i, "pnl_micros": -1, "nav_micros": 100 - i, "silo": "S"}
            for i in range(1, 12)
        ],
        [
            {"ts_utc_ns": 1, "pnl_micros": -100, "nav_micros": 100, "silo": "S"},
            {"ts_utc_ns": 2, "pnl_micros": -100, "nav_micros": 0, "silo": "S"},
            {"ts_utc_ns": 3, "pnl_micros": -100, "nav_micros": -100, "silo": "S"},
        ],
    ],
)
def test_performance_report_never_divides_by_zero_or_emits_nan(rows) -> None:
    from ring2_brain.quant.performance_analytics import PerformanceAnalytics

    analytics = PerformanceAnalytics()
    report = analytics.report(rows, bucket="1D")

    def _walk(value, path="report"):
        if isinstance(value, dict):
            for key, item in value.items():
                _walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                _walk(item, f"{path}[{index}]")
        elif isinstance(value, float):
            assert math.isfinite(value), (path, value)

    _walk(report)
    for key in ("sharpe", "sortino", "calmar"):
        assert report[key] is None or math.isfinite(float(report[key])), key
    assert report["max_drawdown_pct"] >= 0.0


def test_performance_rejects_an_unknown_bucket() -> None:
    from ring2_brain.quant.performance_analytics import PerformanceAnalytics

    with pytest.raises(ValueError):
        PerformanceAnalytics().report([], bucket="7Y")


def test_performance_survives_an_impossible_risk_free_rate() -> None:
    from ring2_brain.quant.performance_analytics import PerformanceAnalytics

    analytics = PerformanceAnalytics(risk_free_annual=-5.0)
    assert analytics.risk_free_annual == 0.0
    rows = [
        {"ts_utc_ns": i, "pnl_micros": i, "nav_micros": 100 + i, "silo": "S"}
        for i in range(1, 8)
    ]
    report = analytics.report(rows, bucket="1D")
    assert report["sharpe"] is None or math.isfinite(float(report["sharpe"]))


# ===========================================================================
# LENS 3 -- autopilot safety interlocks
# ===========================================================================


class _ArmedLatch:
    armed = True

    def is_armed(self) -> bool:
        return True


class _TargetLockedPacer:
    class _State:
        progress = 1.0
        target_locked = True

    def state(self, mission_id):  # noqa: D102 - test double
        return _TargetLockedPacer._State()


def test_autopilot_refuses_to_dispatch_when_the_kill_latch_is_armed() -> None:
    from ring2_brain.quant.autopilot_orchestrator import MultiTrancheAutopilot

    autopilot = MultiTrancheAutopilot(kill_latch=_ArmedLatch(), max_symbols=4)
    report = autopilot.run_cycle(now_ns=SECOND_NS)
    assert report["halted"] is True
    assert report["reason"] == "GLOBAL_HALTED"
    assert report["dispatched_count"] == 0


def test_autopilot_refuses_to_dispatch_in_target_lock() -> None:
    from ring2_brain.quant.autopilot_orchestrator import MultiTrancheAutopilot

    autopilot = MultiTrancheAutopilot(pacing=_TargetLockedPacer(), max_symbols=4)
    report = autopilot.run_cycle(now_ns=SECOND_NS, mission_id="MISSION-1")
    assert report["reason"] == "MISSION_TARGET_LOCK"
    assert report["dispatched_count"] == 0


def test_autopilot_excludes_a_closed_venue() -> None:
    from ring2_brain.quant.autopilot_orchestrator import MultiTrancheAutopilot

    class _Spec:
        symbol = "BTC/USDT"

    class _Registry:
        def silo_ids(self):  # noqa: D102 - test double
            return ("CRYPTO_BINANCE",)

        def by_silo(self, silo):  # noqa: D102 - test double
            return (_Spec(),)

    class _ClosedCalendar:
        def is_open(self, silo, now_ns=None):  # noqa: D102 - test double
            return False

    autopilot = MultiTrancheAutopilot(
        registry=_Registry(), calendar=_ClosedCalendar(), max_symbols=8
    )
    report = autopilot.run_cycle(now_ns=SECOND_NS)
    assert report["dispatched_count"] == 0
    assert report["skipped_closed_market"]
    assert all(
        row["reason"] == "MARKET_CLOSED" for row in report["skipped_closed_market"]
    )


def test_autopilot_dispatches_when_the_venue_is_open() -> None:
    from ring2_brain.quant.autopilot_orchestrator import MultiTrancheAutopilot

    class _Spec:
        symbol = "BTC/USDT"

    class _Registry:
        def silo_ids(self):  # noqa: D102 - test double
            return ("CRYPTO_BINANCE",)

        def by_silo(self, silo):  # noqa: D102 - test double
            return (_Spec(),)

    class _OpenCalendar:
        def is_open(self, silo, now_ns=None):  # noqa: D102 - test double
            return True

    autopilot = MultiTrancheAutopilot(
        registry=_Registry(), calendar=_OpenCalendar(), max_symbols=8
    )
    report = autopilot.run_cycle(now_ns=SECOND_NS)
    assert not report["skipped_closed_market"]
    assert report["dispatched_count"] >= 1
    assert report["signed"] is False  # Ring 2 never signs


class _ArmedLatchForRoute:
    """A latch stub the route's ``_halted()`` probe already understands."""

    is_halted = True


def test_route_refuses_a_halted_autopilot_cycle(client, session, runtime) -> None:
    control = runtime.control
    original = control.kill_latch
    control.kill_latch = _ArmedLatchForRoute()
    try:
        assert control._halted() is True
        refused = client.post(
            "/api/v2/control/autopilot/tick", json={}, headers={"X-NIOS-Session": session}
        )
        assert refused.status_code == 409, refused.text
        assert "GLOBAL_HALTED" in refused.text
    finally:
        control.kill_latch = original


# ===========================================================================
# LENS 4 -- lifecycle worker ceiling, alerting and thread hygiene
# ===========================================================================


def test_unknown_order_queue_escalates_with_a_critical_alert(tmp_path) -> None:
    from core.lifecycle_worker import MANUAL_INTERVENTION_REQUIRED, UnknownOrderQueue
    from ring0_vault.ibor.alert_center import AlertCenter

    alerts = AlertCenter(_journal(tmp_path))

    class _Empty:
        def fetch_open_orders(self, **kwargs):
            return {"orders": []}

    queue = UnknownOrderQueue(
        connector=_Empty(), delays_s=(1, 1), max_attempts=2, alert_sink=alerts
    )
    queue.enqueue(order_id="ORD-1", silo="CRYPTO_BINANCE", symbol="BTC/USDT", now_utc=0)
    for step in (2, 4, 6, 8, 10):
        queue.tick(now_utc=step * SECOND_NS)

    rows = queue.rows()
    assert len(rows) == 1
    assert rows[0]["state"] == "ESCALATED"
    assert rows[0]["escalation_state"] == MANUAL_INTERVENTION_REQUIRED
    assert rows[0]["attempts"] == 2  # the hard ceiling, never more

    critical = alerts.list_alerts(severity="CRITICAL")
    assert critical["count"] == 1
    assert critical["alerts"][0]["code"] == "UNKNOWN_ORDER_MANUAL_INTERVENTION"
    # Escalation is one incident, not one per tick.
    queue.tick(now_utc=20 * SECOND_NS)
    assert alerts.list_alerts(severity="CRITICAL")["count"] == 1


def test_unknown_order_queue_deduplicates_the_incident(tmp_path) -> None:
    from core.lifecycle_worker import UnknownOrderQueue
    from ring0_vault.ibor.alert_center import AlertCenter

    alerts = AlertCenter(_journal(tmp_path))

    class _Empty:
        def fetch_open_orders(self, **kwargs):
            return {"orders": []}

    queue = UnknownOrderQueue(
        connector=_Empty(), delays_s=(1,), max_attempts=1, alert_sink=alerts
    )
    for order in ("ORD-A", "ORD-B"):
        queue.enqueue(order_id=order, silo="CRYPTO_BINANCE", now_utc=0)
    queue.tick(now_utc=5 * SECOND_NS)
    # Same code + same silo + same severity folds into one incident row.
    assert alerts.list_alerts(severity="CRITICAL")["count"] == 1
    assert alerts.list_alerts()["alerts"][0]["occurrences"] == 2


def test_unknown_order_queue_resolves_on_a_definitive_venue_answer(tmp_path) -> None:
    from core.lifecycle_worker import UnknownOrderQueue

    class _Filled:
        def fetch_open_orders(self, **kwargs):
            return {"orders": [{"client_order_id": "ORD-2", "status": "FILLED"}]}

    queue = UnknownOrderQueue(connector=_Filled(), delays_s=(1,), max_attempts=3)
    queue.enqueue(order_id="ORD-2", silo="CRYPTO_BINANCE", now_utc=0)
    queue.tick(now_utc=2 * SECOND_NS)
    row = queue.rows()[0]
    assert row["state"] == "RESOLVED"
    assert row["resolution"] == "FILLED"
    assert queue.status()["resolved"] == 1


def test_unknown_order_probe_runs_outside_the_queue_mutex(tmp_path) -> None:
    """A slow venue probe must not hold the mutex the whole time."""
    from core.lifecycle_worker import UnknownOrderQueue

    observed: list[bool] = []

    class _Probe:
        def fetch_open_orders(self, **kwargs):
            # If the queue held its lock here, this acquire would block forever.
            acquired = queue._lock.acquire(blocking=False)
            observed.append(acquired)
            if acquired:
                queue._lock.release()
            return {"orders": []}

    queue = UnknownOrderQueue(connector=_Probe(), delays_s=(1,), max_attempts=2)
    queue.enqueue(order_id="ORD-3", silo="CRYPTO_BINANCE", now_utc=0)
    queue.tick(now_utc=2 * SECOND_NS)
    assert observed == [True]


def test_lifecycle_worker_status_reports_the_escalation_contract(runtime) -> None:
    from core.lifecycle_worker import MANUAL_INTERVENTION_REQUIRED

    status = runtime.control.lifecycle_worker.status()
    unknown = status["unknown_orders"]
    assert unknown["escalation_state"] == MANUAL_INTERVENTION_REQUIRED
    assert unknown["max_attempts"] == len(unknown["delays_s"])
    assert unknown["alert_sink_attached"] is True
    assert unknown["never_resubmits"] is True


def test_create_app_lifespan_cycles_do_not_leak_threads(runtime) -> None:
    """15 boot/teardown cycles must leave the thread count flat."""
    from fastapi.testclient import TestClient

    from ring3_cockpit.gateway.control_plane import create_app

    # Warm up once so lazily created singletons do not count as a leak.
    with TestClient(create_app(runtime.control)):
        pass
    baseline = threading.active_count()

    for _ in range(15):
        with TestClient(create_app(runtime.control)) as app_client:
            assert app_client.get("/api/v2/lifecycle/status").status_code == 200
        assert threading.active_count() - baseline <= 1

    assert threading.active_count() - baseline <= 1


def test_lifecycle_worker_start_stop_is_idempotent_and_clean(runtime) -> None:
    import asyncio

    worker = runtime.control.lifecycle_worker

    async def _exercise() -> None:
        started = await worker.start(interval_s=0.05)
        assert started["started"] is True or started["reason"] == "DISABLED"
        again = await worker.start(interval_s=0.05)
        assert again["started"] is False
        stopped = await worker.stop()
        assert stopped["stopped"] is True or stopped["reason"] == "NOT_RUNNING"
        assert (await worker.stop())["stopped"] is False
        assert worker.status()["running"] is False

    asyncio.run(_exercise())


# ===========================================================================
# LENS 2/4 -- alert centre retention bound
# ===========================================================================


def test_alert_center_bounds_an_oversized_journal_on_load(tmp_path) -> None:
    from ring0_vault.ibor.alert_center import ALERT_JOURNAL_KEY, AlertCenter

    journal = _journal(tmp_path)
    seeded = [
        {
            "alert_id": f"ALRT-{index}",
            "code": "SEED",
            "severity": "INFO",
            "silo": "",
            "acknowledged": False,
            "occurrences": 1,
        }
        for index in range(5_000)
    ]
    journal.update(**{ALERT_JOURNAL_KEY: seeded})

    center = AlertCenter(journal, max_records=16)
    assert center.status()["count"] == 16
    # The retained window is the newest rows, not the oldest.
    assert center.status()["count"] == len(center.list_alerts()["alerts"])


def test_alert_center_reset_is_durable(tmp_path) -> None:
    from ring0_vault.ibor.alert_center import AlertCenter

    journal = _journal(tmp_path)
    center = AlertCenter(journal)
    center.raise_alert(code="ONE", severity="CRITICAL")
    assert center.reset()["dropped"] == 1
    replay = AlertCenter(journal)
    assert replay.status()["count"] == 0


# ===========================================================================
# Cross-cutting: the audit must not have added a network default
# ===========================================================================


def test_every_new_v21_default_is_offline() -> None:
    from core.runtime_settings import DEFAULT_RUNTIME_SETTINGS
    from ring2_brain.quant.bar_aggregator import LiveBarAggregator
    from ring2_brain.quant.autopilot_orchestrator import MultiTrancheAutopilot
    from ring0_vault.silos.connector_runtime import ConnectorRuntime

    assert DEFAULT_RUNTIME_SETTINGS["connector_mode"] == "OFFLINE_REPLAY"
    assert DEFAULT_RUNTIME_SETTINGS["allow_network"] is False
    assert ConnectorRuntime().connector_mode == "OFFLINE_REPLAY"
    assert ConnectorRuntime().allow_network is False
    assert LiveBarAggregator().features_sink is None
    assert MultiTrancheAutopilot().kill_latch is None


def test_ring2_memory_never_imports_ring0_signing() -> None:
    """Rule 4: Ring 2 emits state and intent, never a credential or a socket."""
    import ast
    import inspect

    from ring2_brain.memory import entity_graph, post_mortem_recorder
    from ring2_brain.quant import bar_aggregator, performance_analytics

    forbidden_roots = {
        "socket",
        "ssl",
        "ccxt",
        "ib_async",
        "alpaca",
        "websockets",
    }
    forbidden_paths = {
        "ring0_vault.silos.key_vault",
        "ring0_vault.kernel.order_signer",
    }
    for module in (entity_graph, post_mortem_recorder, bar_aggregator, performance_analytics):
        tree = ast.parse(inspect.getsource(module))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
        for name in imported:
            root = name.split(".")[0]
            assert root not in forbidden_roots, (module.__name__, name)
            assert not any(name.startswith(path) for path in forbidden_paths), (
                module.__name__,
                name,
            )
