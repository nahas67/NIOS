"""V1.3 - redesigned dual-UI cockpit, separate paper/live login portals, 15 pages.

Four gates:

1. Two dedicated login portals (``POST /api/v2/auth/login/paper`` and
   ``POST /api/v2/auth/login/live``) plus the legacy routes staying intact.
2. Options chain with Black-Scholes Greeks, PCR and Max Pain, and the six
   multi-leg payoff templates, each sealed with a verified SHA-256 packet.
3. Market terminal (60-bar OHLCV + MA/VWAP, 10-level L2 ladder, Lee-Ready
   tape), the cost-adjusted trade logbook and the 12-venue broker hub.
4. Fifteen pages across ``/``, ``/paper/*`` and ``/live/*``, the distinct
   Paper/Live shells and the Biome lint gate on the React 19 cockpit.

Nothing here signs an order, reads a broker secret, or writes the IBOR: the
new surfaces are read-only telemetry plus the operator's own journal
(``sec_004``/``sec_016`` air gap, ``sec_018`` Law 3 grounding).
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
from fastapi.testclient import TestClient  # noqa: E402

USERNAME = "sovereign"
PASSWORD = "correct-horse-9"
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

VENUES = (
    "binance",
    "okx",
    "bybit",
    "dhan",
    "zerodha",
    "angel",
    "upstox",
    "interactive_brokers",
    "alpaca",
    "vnpy_mt5",
    "vnpy_ctp",
    "oanda",
)


def _routes() -> list[str]:
    routes: list[str] = []
    for key in PAGE_KEYS:
        routes.append(f"/{key}")
        routes.append(f"/paper/{key}")
        routes.append(f"/live/{key}")
    return routes


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("nios-v13-ws")
    vault = tmp_path_factory.mktemp("nios-v13-vault")
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


def _headers(token: str) -> dict[str, str]:
    return {"X-NIOS-Session": token}


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


# ---------------------------------------------------------------------------
# Gate 1 - separate /paper/login and /live/login portals and APIs
# ---------------------------------------------------------------------------


def test_gate1_paper_portal_mints_a_paper_session(client, operator) -> None:
    response = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["portal"] == "paper"
    assert body["tier"] == "PAPER"
    assert body["workspace"] == "PAPER"
    assert body["session_ttl_s"] == 43_200
    assert body["expires_ns"] > body["issued_ns"]
    # The HttpOnly cookie is set by the gateway; the body token is the header path.
    assert "nios_session" in response.cookies

    token = str(body["token"])
    live = client.get("/api/v2/workspace/live/state", headers=_headers(token))
    assert live.status_code == 403, live.text
    assert live.json()["detail"] == "LIVE_ELEVATION_REQUIRED"

    paper = client.get("/api/v2/workspace/paper/state", headers=_headers(token))
    assert paper.status_code == 200, paper.text
    assert paper.json()["mode"] == "PAPER"


def test_gate1_live_portal_rejects_a_bad_or_missing_pin(client, operator) -> None:
    wrong = client.post(
        "/api/v2/auth/login/live",
        json={
            "username": USERNAME,
            "password": PASSWORD,
            "live_pin": "000000",
        },
    )
    assert wrong.status_code == 403, wrong.text
    assert wrong.json()["detail"] == "LIVE_PIN_REJECTED"

    missing = client.post(
        "/api/v2/auth/login/live",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert missing.status_code == 403, missing.text
    assert missing.json()["detail"] == "LIVE_PIN_REJECTED"

    non_numeric = client.post(
        "/api/v2/auth/login/live",
        json={"username": USERNAME, "password": PASSWORD, "live_pin": "abcdef"},
    )
    assert non_numeric.status_code == 403, non_numeric.text
    assert non_numeric.json()["detail"] == "LIVE_PIN_REJECTED"

    wrong_password = client.post(
        "/api/v2/auth/login/live",
        json={"username": USERNAME, "password": "not-the-password", "live_pin": LIVE_PIN},
    )
    assert wrong_password.status_code == 401, wrong_password.text
    assert wrong_password.json()["detail"] == "INVALID_CREDENTIALS"


def test_gate1_live_portal_issues_a_live_session_in_one_request(client, operator) -> None:
    response = client.post(
        "/api/v2/auth/login/live",
        json={"username": USERNAME, "password": PASSWORD, "live_pin": LIVE_PIN},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["portal"] == "live"
    assert body["tier"] == "LIVE"
    assert body["workspace"] == "LIVE"
    assert body["session_ttl_s"] == 1_800
    assert 0 < (body["expires_ns"] - body["issued_ns"]) <= 1_800 * 1_000_000_000
    assert "preflight_ok" in body
    assert "nios_session" in response.cookies

    live = client.get("/api/v2/workspace/live/state", headers=_headers(body["token"]))
    assert live.status_code == 200, live.text
    assert live.json()["mode"] == "LIVE"


def test_gate1_auth_status_advertises_both_portals(client, operator) -> None:
    live = client.post(
        "/api/v2/auth/login/live",
        json={"username": USERNAME, "password": PASSWORD, "live_pin": LIVE_PIN},
    )
    assert live.status_code == 200, live.text

    status = client.get("/api/v2/auth/status", headers=_headers(live.json()["token"]))
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["login_portals"] == {
        "paper": "/paper/login",
        "live": "/live/login",
        "gateway": "/login",
    }
    assert body["active_workspace"] == "LIVE"
    assert body["tier"] == "LIVE"
    assert body["authenticated"] is True
    assert body["workspace"] == "LIVE"

    anonymous = client.get("/api/v2/auth/status")
    assert anonymous.status_code == 200
    # Drop the portal cookie this module's earlier logins planted, otherwise the
    # "anonymous" probe is still authenticated by the TestClient cookie jar.
    client.cookies.clear()
    anonymous = client.get("/api/v2/auth/status")
    assert anonymous.status_code == 200
    assert anonymous.json()["active_workspace"] == "NONE"


def test_gate1_legacy_auth_routes_still_work(client, operator) -> None:
    legacy = client.post(
        "/api/v2/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["tier"] == "PAPER"

    elevated = client.post(
        "/api/v2/auth/elevate-live",
        json={"live_pin": LIVE_PIN},
        headers=_headers(legacy.json()["token"]),
    )
    assert elevated.status_code == 200, elevated.text
    assert elevated.json()["tier"] == "LIVE"


@pytest.mark.parametrize("route", ["/login", "/paper/login", "/live/login"])
def test_gate1_login_portals_serve_html(client, route: str) -> None:
    response = client.get(route)
    assert response.status_code == 200, route
    assert "text/html" in response.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Gate 2 - options chain and the multi-leg strategy builder
# ---------------------------------------------------------------------------


def test_gate2_options_page_lists_underlyings_and_templates(client, operator) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    response = client.get("/api/v2/cockpit/options", headers=_headers(token))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["page"] == "options"
    assert body["ring"] == "RING 2"
    assert body["read_only"] is True
    symbols = {row["symbol"] for row in body["underlyings"]}
    assert {"NIFTY", "BANKNIFTY", "BTCUSDT", "SPY", "EURUSD"} <= symbols
    assert {row["silo"] for row in body["underlyings"]} == {
        "INDIA_ZERODHA_DHAN",
        "CRYPTO_BINANCE",
        "US_IBKR",
        "FX_MT5",
    }
    assert [row["id"] for row in body["strategies"]] == [
        "BULL_CALL_SPREAD",
        "BEAR_PUT_SPREAD",
        "SHORT_STRADDLE",
        "LONG_STRANGLE",
        "IRON_CONDOR",
        "PROTECTIVE_PUT",
    ]
    assert body["counts"]["strategies"] == 6
    assert body["counts"]["strikes"] == 11

    chain = body["chain"]
    assert chain["symbol"] == "NIFTY"
    assert chain["summary"]["strike_count"] == 11
    assert _is_sha256(chain["grounding"]["sha256_digest"])
    assert chain["grounding"]["verified"] is True


@pytest.mark.parametrize(
    ("symbol", "silo", "strike"),
    [
        ("NIFTY", "INDIA_ZERODHA_DHAN", 24_250.0),
        ("BTCUSDT", "CRYPTO_BINANCE", 96_400.0),
    ],
)
def test_gate2_chain_computes_greeks_pcr_and_max_pain(
    client, operator, symbol: str, silo: str, strike: float
) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]

    response = client.post(
        "/api/v2/control/options/chain",
        json={"symbol": symbol, "strike_count": 11},
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "GROUNDED"
    assert body["symbol"] == symbol
    assert body["silo"] == silo
    assert len(body["chain"]) == 11
    assert _is_sha256(body["grounding"]["sha256_digest"])
    assert body["grounding"]["verified"] is True
    assert body["grounding"]["claims_checked"] > 0

    strikes = [row["strike"] for row in body["chain"]]
    assert strikes == sorted(strikes)
    assert body["underlying"]["spot"] > 0.0
    # The ATM strike sits on the venue grid around spot.
    assert abs(body["summary"]["atm_strike"] - strike) <= body["strike_step"]

    for row in body["chain"]:
        for field in (
            "call_ltp",
            "call_iv",
            "call_delta",
            "call_gamma",
            "call_theta",
            "call_vega",
            "call_oi",
            "put_ltp",
            "put_iv",
            "put_delta",
            "put_gamma",
            "put_theta",
            "put_vega",
            "put_oi",
            "moneyness",
        ):
            assert field in row, field
        assert row["call_ltp"] >= 0.0
        assert row["put_ltp"] >= 0.0
        assert 0.0 <= row["call_delta"] <= 1.0
        assert -1.0 <= row["put_delta"] <= 0.0
        assert row["call_gamma"] >= 0.0
        assert row["call_oi"] > 0
        assert row["put_oi"] > 0

    summary = body["summary"]
    assert summary["total_call_oi"] > 0.0
    assert summary["total_put_oi"] > 0.0
    assert summary["pcr"] > 0.0
    assert summary["pcr_regime"] in ("PUT_HEAVY", "CALL_HEAVY", "BALANCED")
    assert summary["max_pain_strike"] in strikes
    assert summary["atm_iv"] > 0.0


def test_gate2_chain_honours_overrides_and_rejects_unknown_underlying(
    client, operator
) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]

    override = client.post(
        "/api/v2/control/options/chain",
        json={
            "symbol": "SPY",
            "spot": 600.0,
            "volatility": 0.30,
            "days_to_expiry": 14,
            "strike_count": 7,
        },
        headers=_headers(token),
    )
    assert override.status_code == 200, override.text
    body = override.json()
    assert body["underlying"]["spot"] == 600.0
    assert body["underlying"]["volatility"] == 0.30
    assert body["underlying"]["days_to_expiry"] == 14
    assert len(body["chain"]) == 7

    unknown = client.post(
        "/api/v2/control/options/chain",
        json={"symbol": "NOT_A_SYMBOL"},
        headers=_headers(token),
    )
    assert unknown.status_code == 422, unknown.text
    assert "UNKNOWN_UNDERLYING" in unknown.json()["detail"]


@pytest.mark.parametrize(
    ("strategy", "position"),
    [
        ("IRON_CONDOR", "CREDIT"),
        ("BULL_CALL_SPREAD", "DEBIT"),
        ("SHORT_STRADDLE", "CREDIT"),
    ],
)
def test_gate2_strategy_payoff_is_grounded(
    client, operator, strategy: str, position: str
) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]

    response = client.post(
        "/api/v2/control/options/strategy-payoff",
        json={"symbol": "BTCUSDT", "strategy": strategy},
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "GROUNDED"
    assert body["strategy"] == strategy
    assert body["net_position"] == position
    assert len(body["payoff_curve"]) == 25
    assert body["curve_points"] == 25
    assert _is_sha256(body["grounding"]["sha256_digest"])
    assert body["grounding"]["verified"] is True
    assert body["legs"]
    for leg in body["legs"]:
        assert leg["leg"]
        assert leg["kind"] in ("CALL", "PUT", "STOCK")
        assert leg["direction"] in (1, -1)
        assert leg["premium"] >= 0.0

    assert body["max_profit"] >= body["max_loss"]
    if position == "CREDIT":
        assert body["net_debit_credit"] < 0.0
        assert body["max_profit"] > 0.0
    else:
        assert body["net_debit_credit"] > 0.0
        assert body["max_loss"] < 0.0
    for point in body["payoff_curve"]:
        assert point["expiry_spot"] > 0.0
        assert isinstance(point["pnl"], float)
    assert isinstance(body["breakeven_points"], list)


def test_gate2_every_template_is_priceable(client, operator) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]

    for strategy in (
        "BULL_CALL_SPREAD",
        "BEAR_PUT_SPREAD",
        "SHORT_STRADDLE",
        "LONG_STRANGLE",
        "IRON_CONDOR",
        "PROTECTIVE_PUT",
    ):
        response = client.post(
            "/api/v2/control/options/strategy-payoff",
            json={"symbol": "NIFTY", "strategy": strategy, "width_steps": 2},
            headers=_headers(token),
        )
        assert response.status_code == 200, f"{strategy}: {response.text}"
        assert response.json()["strategy"] == strategy


# ---------------------------------------------------------------------------
# Gate 3 - market terminal, trade logbook and the 12-venue broker hub
# ---------------------------------------------------------------------------


def test_gate3_terminal_page_and_quote(client, operator) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    headers = _headers(token)

    page = client.get("/api/v2/cockpit/terminal", headers=headers)
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["page"] == "terminal"
    assert body["ring"] == "RING 1"
    assert body["counts"]["bars"] == 60
    assert body["default_symbol"] == "BTCUSDT"
    assert {row["silo"] for row in body["watchlist"]} == {
        "CRYPTO_BINANCE",
        "INDIA_ZERODHA_DHAN",
        "US_IBKR",
        "FX_MT5",
    }

    quote = client.post(
        "/api/v2/control/terminal/quote",
        json={"symbol": "NIFTY", "bars": 60, "depth": 10},
        headers=headers,
    )
    assert quote.status_code == 200, quote.text
    frame = quote.json()

    assert frame["status"] == "GROUNDED"
    assert frame["symbol"] == "NIFTY"
    assert frame["silo"] == "INDIA_ZERODHA_DHAN"
    assert len(frame["candles"]) == 60
    for candle in frame["candles"]:
        assert candle["high"] >= candle["low"]
        assert candle["high"] >= candle["open"]
        assert candle["high"] >= candle["close"]
        assert candle["low"] <= candle["open"]
        assert candle["low"] <= candle["close"]
        assert candle["volume"] > 0.0

    for series in ("close", "ma5", "ma10", "ma20", "vwap", "volume"):
        assert len(frame["series"][series]) == 60
    # VWAP is a cumulative volume-weighted average of the typical price, so it
    # must sit inside the traded high/low envelope and never be zero.
    lowest = min(candle["low"] for candle in frame["candles"])
    highest = max(candle["high"] for candle in frame["candles"])
    assert frame["series"]["vwap"][-1] > 0.0
    assert lowest <= frame["series"]["vwap"][-1] <= highest

    depth = frame["depth"]
    assert depth["level_count"] == 10
    assert len(depth["bids"]) == 10
    assert len(depth["asks"]) == 10
    for level in depth["bids"]:
        assert level["price"] > 0.0
        assert level["quantity"] > 0.0
    for level in depth["asks"]:
        assert level["price"] > 0.0
        assert level["quantity"] > 0.0
    assert depth["bids"][0]["price"] < depth["asks"][0]["price"]
    assert -1.0 <= depth["imbalance_ratio"] <= 1.0
    assert depth["imbalance_regime"] in ("BID_HEAVY", "ASK_HEAVY", "BALANCED")

    tape = frame["tape"]
    assert len(tape) == 24
    sides = {print_row["side"] for print_row in tape}
    assert sides <= {"BUY_INITIATED", "SELL_INITIATED"}
    assert frame["tape_summary"]["prints"] == 24
    assert (
        frame["tape_summary"]["buy_initiated"] + frame["tape_summary"]["sell_initiated"]
        == 24
    )
    assert 0.0 <= frame["tape_summary"]["buy_ratio"] <= 1.0
    assert _is_sha256(frame["grounding"]["sha256_digest"])
    assert frame["grounding"]["verified"] is True

    # Determinism: the identical request returns the identical frame.
    repeat = client.post(
        "/api/v2/control/terminal/quote",
        json={"symbol": "NIFTY", "bars": 60, "depth": 10},
        headers=headers,
    ).json()
    assert repeat["series"]["close"] == frame["series"]["close"]
    assert repeat["tape"] == frame["tape"]
    assert repeat["depth"]["bids"] == frame["depth"]["bids"]


def test_gate3_terminal_rejects_an_unknown_silo(client, operator) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    response = client.post(
        "/api/v2/control/terminal/quote",
        json={"symbol": "NIFTY", "silo": "MARS_EXCHANGE"},
        headers=_headers(token),
    )
    assert response.status_code == 422, response.text
    assert "UNKNOWN_SILO" in response.json()["detail"]


def test_gate3_logbook_records_cost_adjusted_pnl(client, operator) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    headers = _headers(token)

    initial = client.get("/api/v2/cockpit/logbook", headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["entries"] == []
    assert initial.json()["clean_slate"] is True
    assert initial.json()["cost_floor_bps"] == {
        "INDIA_ZERODHA_DHAN": 8.50,
        "CRYPTO_BINANCE": 16.00,
        "US_IBKR": 2.80,
        "FX_MT5": 1.60,
    }

    entry = client.post(
        "/api/v2/control/logbook/entry",
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "side": "LONG",
            "setup_strategy": "ma_golden_cross",
            "entry_price": 500.0,
            "exit_price": 510.0,
            "quantity": 10.0,
            "stop_loss": 495.0,
            "thesis": "trend continuation after the golden cross",
            "lessons_learned": "size up when the tape confirms",
            "workspace": "PAPER",
        },
        headers=headers,
    )
    assert entry.status_code == 200, entry.text
    record = entry.json()["entry"]

    assert record["entry_id"].startswith("log_")
    assert record["silo"] == "US_IBKR"
    assert record["side"] == "LONG"
    assert record["gross_pnl"] == pytest.approx(100.0)
    assert record["cost_bps"] == pytest.approx(2.80)
    assert record["cost_drag"] == pytest.approx(5000.0 * 2.80 / 10_000.0)
    assert record["net_pnl"] == pytest.approx(record["gross_pnl"] - record["cost_drag"])
    assert record["r_multiple"] == pytest.approx(2.0)
    assert record["outcome"] == "WIN"
    assert record["workspace"] == "PAPER"

    # A losing short in the India silo pays the 8.50 bps floor.
    loss = client.post(
        "/api/v2/control/logbook/entry",
        json={
            "silo": "INDIA_ZERODHA_DHAN",
            "symbol": "RELIANCE",
            "side": "SELL",
            "entry_price": 2900.0,
            "exit_price": 2920.0,
            "quantity": 5.0,
            "stop_loss": 2930.0,
        },
        headers=headers,
    )
    assert loss.status_code == 200, loss.text
    loser = loss.json()["entry"]
    assert loser["side"] == "SHORT"
    assert loser["gross_pnl"] == pytest.approx(-100.0)
    assert loser["cost_bps"] == pytest.approx(8.50)
    assert loser["net_pnl"] < loser["gross_pnl"]
    assert loser["outcome"] == "LOSS"
    # risk per unit is |2900 - 2930| = 30, so a 20-point adverse move is -0.67R
    # (the record rounds to four decimals before it is stored).
    assert loser["r_multiple"] == pytest.approx(-20.0 / 30.0, abs=1e-4)

    page = client.get("/api/v2/cockpit/logbook", headers=headers).json()
    assert page["counts"] == {
        "entries": 2,
        "wins": 1,
        "losses": 1,
        "flat": 0,
        "paper": 2,
        "live": 0,
    }
    assert page["aggregate"]["win_rate"] == pytest.approx(0.5)
    assert page["aggregate"]["gross_pnl"] == pytest.approx(0.0)
    assert page["aggregate"]["cost_drag"] > 0.0
    assert page["aggregate"]["net_pnl"] < 0.0
    assert page["clean_slate"] is False

    cleared = client.post("/api/v2/control/logbook/clear", headers=headers)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cleared"] == 2
    assert cleared.json()["entries"] == []

    after = client.get("/api/v2/cockpit/logbook", headers=headers).json()
    assert after["entries"] == []
    assert after["counts"]["entries"] == 0
    assert after["clean_slate"] is True


def test_gate3_clean_slate_reset_zeroes_the_logbook(client, operator) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    headers = _headers(token)

    recorded = client.post(
        "/api/v2/control/logbook/entry",
        json={
            "silo": "CRYPTO_BINANCE",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry_price": 96_000.0,
            "exit_price": 96_500.0,
            "quantity": 0.1,
            "stop_loss": 95_500.0,
        },
        headers=headers,
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["entry"]["cost_bps"] == pytest.approx(16.00)

    reset = client.post("/api/v2/control/workspace/reset", headers=headers)
    assert reset.status_code == 200, reset.text
    assert reset.json()["logbook"]["entries"] == []
    assert reset.json()["logbook"]["clean_slate"] is True

    page = client.get("/api/v2/cockpit/logbook", headers=headers).json()
    assert page["entries"] == []
    assert page["counts"]["entries"] == 0
    assert page["aggregate"]["net_pnl"] == 0.0
    assert page["clean_slate"] is True


def test_gate3_broker_hub_covers_all_twelve_venues(client, operator) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    headers = _headers(token)

    response = client.get("/api/v2/cockpit/brokers", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["page"] == "brokers"
    assert body["ring"] == "RING 0"
    assert tuple(body["venues_supported"]) == VENUES
    assert body["counts"]["venues"] == 12
    assert body["counts"]["silos"] == 4
    assert body["air_gap"] == {
        "raw_keys_exposed": 0,
        "order_signing_tools": 0,
        "ring3_read_only": True,
    }

    rows = {row["venue"]: row for row in body["venues"]}
    assert tuple(sorted(rows)) == tuple(sorted(VENUES))
    for venue, row in rows.items():
        assert row["silo"] in {"CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"}
        assert row["credential_status"] in ("CONFIGURED", "UNSET")
        assert row["adapter_module"].startswith(("ring1_spinal", "ring0_vault"))
        assert row["adapter_importable"] is True, venue
        assert row["signing"]["algorithm"] == "Ed25519"
        assert row["signing"]["dry_run_verified"] is True, venue
        assert row["feed"]["dual_vendor"] is True
        assert row["settlement"]["settlement_cycle"]
        # A raw secret must never appear in the payload.
        assert row["api_key_masked"] is None or row["api_key_masked"].startswith("*") or (
            "***" in str(row["api_key_masked"]) or str(row["api_key_masked"]).endswith("…")
        )

    assert body["vault"]["write_only"] is True
    assert body["vault"]["raw_secrets_exposed"] is False
    assert len(str(body["vault"]["ed25519_public_fingerprint"])) == 16


@pytest.mark.parametrize("venue", VENUES)
def test_gate3_broker_ping_proves_signing(client, operator, venue: str) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]

    response = client.post(
        "/api/v2/control/brokers/ping",
        json={"venue": venue},
        headers=_headers(token),
    )
    assert response.status_code == 200, f"{venue}: {response.text}"
    body = response.json()

    assert body["venue"] == venue
    assert body["adapter"]["importable"] is True
    assert body["signing"]["algorithm"] == "Ed25519"
    assert body["signing"]["verified"] is True
    assert body["signing"]["dry_run"] is True
    assert body["signing"]["signature_length"] == 64
    assert _is_sha256(body["signing"]["message_sha256"])
    assert body["credential"]["status"] in ("CONFIGURED", "UNSET")
    assert body["credential"]["raw_secret_returned"] is False
    assert body["ok"] is True

    unknown = client.post(
        "/api/v2/control/brokers/ping",
        json={"venue": "not_a_venue"},
        headers=_headers(token),
    )
    assert unknown.status_code == 422


# ---------------------------------------------------------------------------
# Gate 4 - fifteen pages across all three route trees and the distinct shells
# ---------------------------------------------------------------------------


def test_gate4_pages_index_advertises_fifteen_pages(client, operator) -> None:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    response = client.get("/api/v2/cockpit/pages", headers=_headers(token))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["page_count"] == 15
    assert tuple(page["key"] for page in body["pages"]) == PAGE_KEYS

    groups = [page["group"] for page in body["pages"]]
    assert set(groups) == {
        "CORE_EXECUTION",
        "QUANT_OPTIONS",
        "BICAMERAL_BRAIN",
        "LEDGER_VERIFICATION",
        "SOVEREIGN_CONTROL",
    }

    for key in ("terminal", "options", "logbook", "brokers"):
        route = f"/{key}"
        assert body["routes"][route] == key
        page = next(item for item in body["pages"] if item["key"] == key)
        assert page["live"] is True
        assert page["status"] == "READY"
        assert page["read_only"] is True
        assert page["api"] == f"/api/v2/cockpit/{key}"

    assert body["air_gap"]["ring3_read_only"] is True
    assert body["air_gap"]["order_signing_tools"] == 0
    assert body["air_gap"]["key_reading_tools"] == 0


@pytest.mark.parametrize("route", _routes())
def test_gate4_all_fifteen_pages_serve_all_three_trees(client, route: str) -> None:
    response = client.get(route)
    assert response.status_code == 200, f"{route}: {response.text[:200]}"
    assert "text/html" in response.headers.get("content-type", "")


def test_gate4_unknown_api_path_is_not_swallowed_by_the_spa(client) -> None:
    response = client.get("/api/v2/nope")
    assert response.status_code in (401, 403, 404)


def test_gate4_router_sidebar_and_shells_are_wired() -> None:
    """The V1.4 clean-room layout keeps every V1.3 navigation contract."""

    registry = (SRC_ROOT / "pages" / "registry.ts").read_text(encoding="utf-8")
    for key in PAGE_KEYS:
        assert f'key: "{key}"' in registry, key
    for label in (
        "Core Execution",
        "Quant & Research",
        "Bicameral Brain",
        "Ledger & Audit",
        "Sovereign Control",
    ):
        assert label in registry, label
    assert "NAV_GROUPS" in registry
    assert "PageKey" in registry

    sidebar = (SRC_ROOT / "shells" / "Sidebar.tsx").read_text(encoding="utf-8")
    assert "aria-expanded" in sidebar
    assert "NAV_GROUPS" in sidebar
    assert "aria-current" in sidebar
    for key in PAGE_KEYS:
        assert key in sidebar, key

    router = (SRC_ROOT / "lib" / "router.ts").read_text(encoding="utf-8")
    for route in ("/setup", "/login", "/paper/login", "/live/login", "/live-unlock"):
        assert route in router, route
    assert "pagePath" in router
    assert "popstate" in router

    app = (SRC_ROOT / "App.tsx").read_text(encoding="utf-8")
    for name in (
        "DashboardPage",
        "TerminalPage",
        "OrdersPage",
        "OptionsPage",
        "AnalyzerPage",
        "QuantPage",
        "AgentsPage",
        "BacktestPage",
        "BrainPage",
        "ForgePage",
        "LedgerPage",
        "LogbookPage",
        "VerificationPage",
        "BrokersPage",
        "ControlPage",
    ):
        assert name in app, name
    assert "PaperShell" in app
    assert "LiveShell" in app
    assert "CommandPalette" in app

    paper = (SRC_ROOT / "shells" / "PaperShell.tsx").read_text(encoding="utf-8")
    assert "PAPER SANDBOX" in paper
    assert "Deposit Paper Capital" in paper
    assert "Reset All Values to Zero" in paper
    assert "Autopilot" in paper

    live = (SRC_ROOT / "shells" / "LiveShell.tsx").read_text(encoding="utf-8")
    assert "LIVE PRODUCTION — REAL CAPITAL" in live
    assert "EMERGENCY KILL SWITCH" in live
    assert "Lock Live Vault (Drop to Paper)" in live
    assert "22-gate" in live.lower()
    live_lower = live.lower()
    # Live must never render a paper deposit or a shadow-burst control.
    assert "deposit paper capital" not in live_lower
    assert "shadow-burst" not in live_lower
    assert "reset all values to zero" not in live_lower

    gateway = (SRC_ROOT / "portals" / "GatewayPortal.tsx").read_text(encoding="utf-8")
    assert "ENTER PAPER SANDBOX" in gateway
    assert "LIVE SOVEREIGN VAULT" in gateway

    paper_login = (SRC_ROOT / "portals" / "PaperLoginPortal.tsx").read_text(
        encoding="utf-8"
    )
    assert "SIMULATED EXECUTION ONLY (ALL VALUES START AT 0)" in paper_login
    assert "loginPaper" in paper_login

    live_login = (SRC_ROOT / "portals" / "LiveLoginPortal.tsx").read_text(
        encoding="utf-8"
    )
    assert "live_pin" in live_login
    assert "loginLive" in live_login
    assert "acknowledged" in live_login
    assert "PIN" in live_login

    css = (SRC_ROOT / "index.css").read_text(encoding="utf-8")
    css += (SRC_ROOT / "design-system" / "tokens.css").read_text(encoding="utf-8")
    assert "backdrop-filter" not in css
    assert "box-shadow" not in css
    assert "#0d0e10" in css
    assert "#f96316" in css
    assert ".nios-shell-paper" in css
    assert ".nios-shell-live" in css
    assert ".nios-iso-plane" in css
    assert ".nios-hazard" in css
    assert ".nios-tabular" in css

    for page in PAGE_KEYS:
        source = (SRC_ROOT / "pages" / f"{page[:1].upper()}{page[1:]}Page.tsx").read_text(encoding="utf-8")
        assert "SectionHeader" in source, page
        assert "PageShell" not in source, page


def test_gate4_biome_lint_is_clean() -> None:
    npx = shutil.which("npx")
    if npx is None:  # pragma: no cover - node toolchain is optional
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
    combined = f"{completed.stdout}{completed.stderr}".lower()
    assert "found 0 errors" in combined or "checked" in combined


def test_gate4_cockpit_bundle_is_built_with_both_shells() -> None:
    index_html = UI_ROOT / "dist" / "index.html"
    assert index_html.exists(), "run `npm run build` in ring3_cockpit/ui/openalgo_shell"
    text = index_html.read_text(encoding="utf-8")
    assert "assets/" in text
    assets = list((UI_ROOT / "dist" / "assets").glob("*.js"))
    assert assets, "dist/assets has no JavaScript bundle"
    joined = "".join(path.read_text(encoding="utf-8") for path in assets)
    for marker in ("PAPER SANDBOX", "LIVE PRODUCTION", "Clean slate", "15 production pages"):
        assert marker.lower() in joined.lower(), marker
