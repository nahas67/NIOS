"""Suite 26 - V1.8 TradingView bridge, replay studies, stream hub and broker handshake.

Gates
-----
Gate 1  274-instrument ``tradingview_ticker`` mapping, uniqueness and discovery.
Gate 2  TradingView chart replay, Pine Script study plots and SHA-256 grounding.
Gate 3  ``TRADINGVIEW_REPLAY`` walk-forward backtest with statutory cost floors.
Gate 4  Multiplexed typed SSE stream and the Ring 0 broker capability handshake.

Contract references: sec_004 (ring isolation), sec_009 (storage/stream projection),
sec_014 (instrument contracts), sec_016 (control plane), sec_018 (constitutional laws).
Law 1/Law 2: TradingView is Ring 2 auxiliary charting only, never a Ring 0/1 feed.
Law 3: every bar and every study plot is sealed in a verified SHA-256 packet.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

import pytest

from ring3_cockpit.gateway.payload_envelope import payload_digest

USERNAME = "v18.operator"
PASSWORD = "v18-sovereign-passphrase"
LIVE_PIN = "8421"

HEX64 = re.compile(r"^[0-9a-f]{64}$")

FUNDING = {
    "INDIA_ZERODHA_DHAN": 400_000_000.0,
    "CRYPTO_BINANCE": 50_000_000.0,
    "US_IBKR": 50_000_000.0,
    "FX_MT5": 400_000_000.0,
}

STUDY_IDS = (
    "STD;RSI",
    "STD;MACD",
    "STD;Supertrend",
    "STD;Ichimoku%20Cloud",
    "STD;Bollinger%20Bands",
    "STD;EMA%20Ribbon",
    "STD;VWAP%20Bands",
    "NIOS;Sovereign%20Regime%20Composite",
)

STREAM_FRAMES = (
    "TICK_UPDATE",
    "L2_DEPTH_DELTA",
    "ORDER_LIFECYCLE",
    "RISK_HEARTBEAT",
    "TV_STUDY_SIGNAL",
)

HANDSHAKE_VENUES = (
    "binance",
    "okx",
    "bybit",
    "interactive_brokers",
    "alpaca",
    "dhan",
    "zerodha",
    "angel",
    "upstox",
    "vnpy_mt5",
    "vnpy_ctp",
    "oanda",
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    from core.orchestrator import NIOSCoreOrchestrator

    workspace = tmp_path_factory.mktemp("v18-tv-ws")
    vault = tmp_path_factory.mktemp("v18-tv-vault")
    instance = NIOSCoreOrchestrator(workspace=workspace, key_vault_root=vault, clean_slate=True)
    try:
        yield instance
    finally:
        try:
            instance.close()
        except Exception:  # pragma: no cover - defensive shutdown
            pass


@pytest.fixture(scope="module")
def client(runtime):
    from fastapi.testclient import TestClient

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


@pytest.fixture(scope="module")
def headers(client, operator) -> dict[str, str]:
    response = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"X-NIOS-Session": response.json()["token"]}


@pytest.fixture(scope="module")
def funded(runtime):
    from ring3_cockpit.gateway.control_plane import PaperFundingRequest

    for silo, amount in FUNDING.items():
        report = runtime.control.fund_paper(
            PaperFundingRequest(silo=silo, amount_units=amount, vault="ALL_CANONICAL")
        )
        assert report["ok"] is True, report
    return runtime.control


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _assert_digest(body: dict[str, Any]) -> None:
    digest = body.get("payload_sha256")
    assert isinstance(digest, str) and HEX64.match(digest), digest
    assert payload_digest(body) == digest


def _receipt(body: dict[str, Any]) -> dict[str, Any]:
    nested = body.get("receipt")
    return nested if isinstance(nested, dict) else body


def _assert_grounded(block: dict[str, Any]) -> None:
    grounding = block["grounding"]
    assert grounding["verified"] is True
    assert HEX64.match(grounding["digest"])
    assert grounding["claims_checked"] >= 1
    assert isinstance(grounding["thesis"], str) and grounding["thesis"]
    packet = block["packet"]
    assert packet["sha256_digest"] == grounding["digest"]
    assert packet["knowledge_ts_ns"] <= packet["as_of_ns"]
    assert packet["valid_time_ns"] <= packet["as_of_ns"]
    assert len(packet["metrics"]) >= 5


def _sse_events(chunks: Iterator[str]) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    name: str | None = None
    for line in chunks:
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            assert name is not None
            events.append((name, json.loads(line.split(":", 1)[1].strip())))
            name = None
    return events


# --------------------------------------------------------------------------- #
# Gate 1 - 274 instrument tradingview_ticker mapping and discovery
# --------------------------------------------------------------------------- #
def test_gate1_every_instrument_has_a_canonical_tradingview_ticker():
    from core.instrument_registry import SILO_IDS, registry

    specs = registry.all()
    assert len(specs) == 274, len(specs)

    expected_silo_counts = {
        "INDIA_ZERODHA_DHAN": 76,
        "CRYPTO_BINANCE": 80,
        "US_IBKR": 79,
        "FX_MT5": 39,
    }
    for silo, count in expected_silo_counts.items():
        assert len(registry.by_silo(silo)) == count, silo
    assert set(expected_silo_counts) == set(SILO_IDS)

    tickers: list[str] = []
    for spec in specs:
        ticker = spec.tradingview_ticker
        assert isinstance(ticker, str) and ticker, spec.symbol
        exchange, _, symbol = ticker.partition(":")
        assert exchange and symbol, ticker
        assert ticker == ticker.upper()
        assert " " not in ticker
        tickers.append(ticker)

    assert len(tickers) == 274
    assert len(set(tickers)) == 274, "duplicate tradingview_ticker detected"

    # spot checks across all four silos
    by_symbol = {spec.symbol: spec.tradingview_ticker for spec in specs}
    assert by_symbol["NIFTY"] == "NSE:NIFTY"
    assert by_symbol["SENSEX"] == "BSE:SENSEX"
    assert by_symbol["M&M"] == "NSE:M_M"
    assert by_symbol["GOLD"] == "MCX:GOLD1!"
    assert by_symbol["USDINR"] == "NSE:USDINR1!"
    assert by_symbol["BTCUSDT"] == "BINANCE:BTCUSDT.P"
    assert by_symbol["NVDA"] == "NASDAQ:NVDA"
    assert by_symbol["JPM"] == "NYSE:JPM"
    assert by_symbol["SPY"] == "AMEX:SPY"
    assert by_symbol["QQQ"] == "NASDAQ:QQQ"
    assert by_symbol["ES"] == "CME_MINI:ES1!"
    assert by_symbol["CL"] == "NYMEX:CL1!"
    assert by_symbol["GC"] == "COMEX:GC1!"
    assert by_symbol["EURUSD"] == "OANDA:EURUSD"
    assert by_symbol["XAUUSD"] == "OANDA:XAUUSD"
    assert by_symbol["USOIL"] == "TVC:USOIL"


def test_gate1_catalogue_and_search_expose_the_ticker(client, headers, funded):
    listing = client.get("/api/v2/instruments", params={"limit": 500}, headers=headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    _assert_digest(body)
    assert body["total_count"] == 274
    assert len(body["items"]) == 274
    assert all(item["tradingview_ticker"] for item in body["items"])

    exact = client.get(
        "/api/v2/instruments", params={"tv_ticker": "NSE:RELIANCE"}, headers=headers
    )
    assert exact.status_code == 200, exact.text
    rows = exact.json()["items"]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "RELIANCE"
    assert rows[0]["tradingview_ticker"] == "NSE:RELIANCE"

    detail = client.get("/api/v2/instruments/BTCUSDT", headers=headers)
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    _assert_digest(detail_body)
    assert detail_body["contract"]["tradingview_ticker"] == "BINANCE:BTCUSDT.P"

    search = client.get(
        "/api/v2/market/tv-search", params={"q": "semiconductor"}, headers=headers
    )
    assert search.status_code == 200, search.text
    search_body = search.json()
    _assert_digest(search_body)
    assert search_body["returned_count"] >= 1
    assert all(item["tradingview_ticker"] for item in search_body["items"])

    silo_scoped = client.get(
        "/api/v2/market/tv-search",
        params={"q": "usdt", "silo": "CRYPTO_BINANCE"},
        headers=headers,
    )
    assert silo_scoped.status_code == 200, silo_scoped.text
    scoped_items = silo_scoped.json()["items"]
    assert scoped_items and all(item["silo"] == "CRYPTO_BINANCE" for item in scoped_items)

    unknown_silo = client.get(
        "/api/v2/market/tv-search", params={"q": "gold", "silo": "MARS_EXCHANGE"}, headers=headers
    )
    assert unknown_silo.status_code == 422

    empty = client.get("/api/v2/market/tv-search", params={"q": "   "}, headers=headers)
    assert empty.status_code in (422, 404)


# --------------------------------------------------------------------------- #
# Gate 2 - chart replay and Pine Script study grounding
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("timeframe", ["1", "15", "1D"])
def test_gate2_chart_replay_is_deterministic_and_grounded(client, headers, funded, timeframe):
    params = {"symbol": "RELIANCE", "timeframe": timeframe, "bars": 120}
    first = client.get("/api/v2/market/tv-chart", params=params, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    _assert_digest(body)

    assert body["symbol"] == "RELIANCE"
    assert body["tradingview_ticker"] == "NSE:RELIANCE"
    assert body["silo"] == "INDIA_ZERODHA_DHAN"
    assert body["timeframe"] == timeframe
    assert body["mode"] == "DETERMINISTIC_REPLAY"
    assert len(body["bars"]) == 120
    assert body["replay"]["replay_offset"] == 0
    assert body["summary"]["close"] == body["bars"][-1]["close"]
    _assert_grounded(body)

    second = client.get("/api/v2/market/tv-chart", params=params, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["packet"]["sha256_digest"] == body["packet"]["sha256_digest"]

    offset = client.get(
        "/api/v2/market/tv-chart",
        params={**params, "replay_offset": 5},
        headers=headers,
    )
    assert offset.status_code == 200, offset.text
    offset_body = offset.json()
    assert offset_body["replay"]["replay_offset"] == 5
    assert len(offset_body["bars"]) == 120
    assert offset_body["bars"][-1]["ts_ns"] < body["bars"][-1]["ts_ns"]
    _assert_grounded(offset_body)


def test_gate2_chart_rejects_bad_symbol_timeframe_and_bounds(client, headers, funded):
    missing = client.get(
        "/api/v2/market/tv-chart", params={"symbol": "NOTAREALINSTRUMENT"}, headers=headers
    )
    assert missing.status_code == 404

    bad_tf = client.get(
        "/api/v2/market/tv-chart", params={"symbol": "NIFTY", "timeframe": "3h"}, headers=headers
    )
    assert bad_tf.status_code == 422

    too_few = client.get(
        "/api/v2/market/tv-chart", params={"symbol": "NIFTY", "bars": 5}, headers=headers
    )
    assert too_few.status_code == 422

    too_many = client.get(
        "/api/v2/market/tv-chart", params={"symbol": "NIFTY", "bars": 5000}, headers=headers
    )
    assert too_many.status_code == 422


@pytest.mark.parametrize("study_id", STUDY_IDS)
def test_gate2_all_eight_pine_studies_emit_grounded_plots(client, headers, funded, study_id):
    response = client.post(
        "/api/v2/market/tv-study",
        json={"symbol": "BTCUSDT", "study_id": study_id, "timeframe": "60", "bars": 200},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_digest(body)

    assert body["symbol"] == "BTCUSDT"
    assert body["tradingview_ticker"] == "BINANCE:BTCUSDT.P"
    assert body["bars"] == 200
    assert body["plots"] and isinstance(body["plots"], dict)
    for series in body["plots"].values():
        assert isinstance(series, list) and len(series) == 200
    assert body["latest"] and all(isinstance(v, float) for v in body["latest"].values())
    verdict = body["verdict"]
    assert verdict["regime"] and verdict["regime"] == verdict["regime"].upper()
    assert isinstance(verdict["score"], float)
    _assert_grounded(body)


def test_gate2_study_catalog_and_validation(client, headers, funded):
    catalog = client.post(
        "/api/v2/market/tv-study",
        json={"symbol": "NIFTY", "study_id": "RSI", "timeframe": "1D", "bars": 60},
        headers=headers,
    )
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    assert body["study_id"] == "STD;RSI"
    assert {study.replace("%20", " ") for study in STUDY_IDS}.issubset(
        {str(study_id) for study_id in body["studies_supported"]}
    )

    unknown_study = client.post(
        "/api/v2/market/tv-study",
        json={"symbol": "NIFTY", "study_id": "STD;NONEXISTENT"},
        headers=headers,
    )
    assert unknown_study.status_code == 404

    unknown_symbol = client.post(
        "/api/v2/market/tv-study",
        json={"symbol": "NOTAREALINSTRUMENT", "study_id": "STD;RSI"},
        headers=headers,
    )
    assert unknown_symbol.status_code == 404

    bad_timeframe = client.post(
        "/api/v2/market/tv-study",
        json={"symbol": "NIFTY", "study_id": "STD;RSI", "timeframe": "7h"},
        headers=headers,
    )
    assert bad_timeframe.status_code == 422

    out_of_range = client.post(
        "/api/v2/market/tv-study",
        json={"symbol": "NIFTY", "study_id": "STD;RSI", "bars": 3},
        headers=headers,
    )
    assert out_of_range.status_code == 422


# --------------------------------------------------------------------------- #
# Gate 3 - walk-forward backtest on TradingView replay bars
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("symbol", "silo", "floor_bps"),
    [("RELIANCE", "INDIA_ZERODHA_DHAN", 8.50), ("BTCUSDT", "CRYPTO_BINANCE", 16.00)],
)
def test_gate3_backtest_runs_on_tradingview_replay(
    client, headers, funded, symbol, silo, floor_bps
):
    response = client.post(
        "/api/v2/control/backtest/run",
        json={
            "silo": silo,
            "symbol": symbol,
            "strategy_id": "ma_golden_cross",
            "bars": 180,
            "timeframe": "1d",
            "data_source": "TRADINGVIEW_REPLAY",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    receipt = _receipt(response.json())

    assert receipt["symbol"] == symbol
    assert receipt["silo"] == silo
    assert receipt["data_source"] == "TRADINGVIEW_REPLAY"
    assert receipt["synthetic_tape"] is False

    replay = receipt["tradingview_replay"]
    assert replay["data_source"] == "TRADINGVIEW_REPLAY"
    assert replay["tradingview_timeframe"] == "1D"
    assert replay["bars"] == 180
    assert replay["mode"] == "DETERMINISTIC_REPLAY"

    assert receipt["cost_floor_bps"] == pytest.approx(floor_bps)
    statutory = receipt["statutory_cost"]
    assert statutory["silo"] == silo
    assert statutory["floor_bps"] == pytest.approx(floor_bps)
    assert statutory["reconciles"] is True
    assert statutory["total_bps"] >= floor_bps - 1e-9


def test_gate3_synthetic_source_remains_the_default(client, headers, funded):
    response = client.post(
        "/api/v2/control/backtest/run",
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "bars": 120,
            "timeframe": "1d",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    receipt = _receipt(response.json())
    assert receipt["data_source"] == "SYNTHETIC_WALK_FORWARD"
    assert receipt["synthetic_tape"] is True
    assert "tradingview_replay" not in receipt


def test_gate3_rejects_unknown_data_source(client, headers, funded):
    response = client.post(
        "/api/v2/control/backtest/run",
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "bars": 120,
            "data_source": "BLOOMBERG_TERMINAL",
        },
        headers=headers,
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Gate 4 - multiplexed SSE stream and broker handshake
# --------------------------------------------------------------------------- #
def test_gate4_stream_snapshot_exposes_typed_frames(client, headers, funded):
    response = client.get("/api/v2/stream/snapshot", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_digest(body)

    assert body["event_frames"] == list(STREAM_FRAMES)
    assert body["frame_count"] == len(STREAM_FRAMES)
    assert len(body["frames"]) == len(STREAM_FRAMES)
    for entry in body["frames"]:
        frame = entry["frame"]
        assert entry["event_type"] in STREAM_FRAMES
        assert frame["schema_version"] == "v1.8.stream-hub"
        assert HEX64.match(frame["payload_sha256"])
        assert frame["proof_line"]
    assert set(body["risk"]) == set(FUNDING)


def test_gate4_stream_emits_finite_typed_sse_frames(client, headers, funded):
    with client.stream("GET", "/api/v2/stream?max_events=4", headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(response.iter_lines())

    assert len(events) == 4
    assert [name for name, _ in events] == list(STREAM_FRAMES[:4])
    for name, payload in events:
        assert payload["schema_version"] == "v1.8.stream-hub"
        assert HEX64.match(payload["payload_sha256"])
        assert payload["ts_ns"] > 0
        assert payload["proof_line"]
        if name == "TICK_UPDATE":
            assert payload["price"] > 0
            assert payload["tradingview_ticker"]
        elif name == "L2_DEPTH_DELTA":
            assert payload["side"] in ("BID", "ASK")
        elif name == "ORDER_LIFECYCLE":
            assert payload["order_id"].startswith("NIOS-")
        elif name == "RISK_HEARTBEAT":
            assert payload["cost_floor_bps"] > 0


def test_gate4_stream_respects_silo_filter(client, headers, funded):
    with client.stream(
        "GET", "/api/v2/stream?max_events=3&silo=CRYPTO_BINANCE", headers=headers
    ) as response:
        assert response.status_code == 200
        events = _sse_events(response.iter_lines())

    assert len(events) == 3
    assert all(payload["silo"] == "CRYPTO_BINANCE" for _, payload in events)


@pytest.mark.parametrize("venue", HANDSHAKE_VENUES)
def test_gate4_broker_handshake_verifies_every_venue(client, headers, funded, venue):
    response = client.post(
        "/api/v2/control/brokers/handshake",
        json={"venue": venue, "dry_run": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_digest(body)

    assert body["venue"] == venue
    assert body["dry_run"] is True

    handshake = body["handshake"]
    assert handshake["ready"] is True
    assert handshake["state"] == "READY"
    assert handshake["read_only"] is True
    assert handshake["live_orders_sent"] == 0

    assert body["adapter"]["importable"] is True
    assert body["signing"]["verified"] is True
    assert body["signing"]["algorithm"].lower() == "ed25519"
    assert body["signing"]["dry_run"] is True

    skew = body["clock_skew"]
    assert 0 <= skew["venue_skew_ms"] <= skew["budget_ms"]
    assert skew["within_budget"] is True

    capabilities = body["capabilities"]
    assert "MARKET" in capabilities["order_types"]
    assert capabilities["read_only"] is True

    assert body["credential"]["raw_secret_returned"] is False
    assert body["guard"]["live_tier_guard_applied"] is False


def test_gate4_handshake_rejects_unknown_venue_and_guards_live(client, headers, funded):
    unknown = client.post(
        "/api/v2/control/brokers/handshake",
        json={"venue": "mars_exchange", "dry_run": True},
        headers=headers,
    )
    assert unknown.status_code == 422

    live = client.post(
        "/api/v2/control/brokers/handshake",
        json={"venue": "binance", "dry_run": False},
        headers=headers,
    )
    assert live.status_code in (200, 401, 403), live.text
    if live.status_code == 200:
        body = live.json()
        assert body["guard"]["live_tier_guard_applied"] is True
        assert body["handshake"]["live_orders_sent"] == 0
