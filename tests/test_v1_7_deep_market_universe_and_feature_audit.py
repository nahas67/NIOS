"""Suite 25 -- V1.7 deep market universe, feature store and macro audit.

Five self-grading audits prove the institutional data expansion is real rather
than a wider list of the same shallow stubs:

Audit 1  250+ multi-asset universe completeness across all four silos, zero
         duplicate symbols, complete contract specs on every entry.
Audit 2  Deep asset-class feature store and the four-expiry options surface,
         seeded with valid SHA-256 grounding packets, on RELIANCE / AAPL /
         BTCUSDT / EURUSD / NIFTY.
Audit 3  Search, filter and pagination accuracy on ``GET /api/v2/instruments``.
Audit 4  Macro calendar and the four cross-asset sector / narrative / currency
         heatmaps on ``GET /api/v2/market/*``.
Audit 5  End-to-end integration with ``SmartOMS`` and
         ``WalkForwardBacktestEngine`` on four newly added instruments
         (``TCS``, ``SOLUSDT``, ``NVDA``, ``GBPJPY``): exact registry lot size,
         tick grid and statutory cost floor enforced by the paper path and
         deducted by the tax-net backtester.
"""

from __future__ import annotations

import re

import pytest

from core.instrument_registry import (
    MICROS,
    SETTLEMENT_CYCLES,
    SILO_IDS,
    registry as instrument_registry,
)
from core.market_features_store import (
    CRYPTO_FIELDS,
    EQUITY_FIELDS,
    FX_COMMODITY_FIELDS,
    OPTIONS_SURFACE_FIELDS,
    assert_feature_coverage,
    get_feature_profile,
    get_options_surface_profile,
)
from ring0_vault.ibor.tax_and_cost_engine import COST_FLOOR_BPS
from ring3_cockpit.gateway.payload_envelope import payload_digest

USERNAME = "sovereign"
PASSWORD = "v17-deep-market-audit-Passw0rd!"
LIVE_PIN = "90210"

HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: Every NIFTY 50 constituent that must resolve inside the India silo.
NIFTY_50 = (
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "ICICIBANK",
    "BHARTIARTL",
    "INFY",
    "SBIN",
    "LICI",
    "ITC",
    "HINDUNILVR",
    "LT",
    "BAJFINANCE",
    "HCLTECH",
    "MARUTI",
    "SUNPHARMA",
    "ADANIENT",
    "KOTAKBANK",
    "TITAN",
    "ONGC",
    "TATAMOTORS",
    "NTPC",
    "AXISBANK",
    "ULTRACEMCO",
    "DMART",
    "ADANIPORTS",
    "BAJAJFINSV",
    "ASIANPAINT",
    "COALINDIA",
    "WIPRO",
    "NESTLEIND",
    "JSWSTEEL",
    "POWERGRID",
    "M&M",
    "TATASTEEL",
    "IOC",
    "SBILIFE",
    "GRASIM",
    "HDFCLIFE",
    "TECHM",
    "HINDALCO",
    "EICHERMOT",
    "INDUSINDBK",
    "BRITANNIA",
    "CIPLA",
    "DRREDDY",
    "APOLLOHOSP",
    "TATACONSUM",
    "DIVISLAB",
    "HEROMOTOCO",
    "BPCL",
)

#: All 28 G10 pairs that must resolve inside the FX silo.
G10_PAIRS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "NZDJPY",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "EURNZD",
    "GBPAUD",
    "GBPCAD",
    "GBPCHF",
    "GBPNZD",
    "AUDCAD",
    "AUDCHF",
    "AUDNZD",
    "NZDCAD",
    "NZDCHF",
    "CADCHF",
)

REQUIRED_SPEC_FIELDS = (
    "symbol",
    "display_name",
    "silo",
    "venue",
    "asset_class",
    "sub_category",
    "sector",
    "currency",
    "lot_size",
    "tick_size_micros",
    "freeze_quantity",
    "circuit_limit_pct",
    "margin_initial_pct",
    "margin_maintenance_pct",
    "settlement_cycle",
    "trading_hours_utc",
    "option_strike_step",
    "has_options",
    "isin_or_id",
)

INDIA_SECTORS = (
    "IT",
    "Banking & Financials",
    "Oil & Gas",
    "Automobiles",
    "Pharma & Healthcare",
    "FMCG",
    "Metals & Mining",
    "Capital Goods & Infra",
    "Power & Utilities",
    "Consumer Durables",
)

US_GICS_SECTORS = (
    "Information Technology",
    "Financials",
    "Healthcare",
    "Consumer Discretionary",
    "Communication Services",
    "Industrials",
    "Consumer Staples",
    "Energy",
    "Utilities",
    "Materials",
    "Real Estate",
)

CRYPTO_NARRATIVES = (
    "Layer-1 Smart Contracts",
    "AI & DePIN",
    "DeFi & DEX",
    "Layer-2 Scaling",
    "Real-World Assets (RWA)",
    "Meme & Community",
)

#: (symbol, silo, contract lot size, reference price, statutory floor bps)
AUDIT_5_CASES = (
    ("TCS", "INDIA_ZERODHA_DHAN", 175.0, 4_100.0, 8.50),
    ("SOLUSDT", "CRYPTO_BINANCE", 0.01, 195.0, 16.00),
    ("NVDA", "US_IBKR", 1.0, 145.0, 2.80),
    ("GBPJPY", "FX_MT5", 100_000.0, 194.5, 1.60),
)

FUNDING = {
    "INDIA_ZERODHA_DHAN": 400_000_000.0,
    "CRYPTO_BINANCE": 50_000_000.0,
    "US_IBKR": 50_000_000.0,
    "FX_MT5": 400_000_000.0,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    from core.orchestrator import NIOSCoreOrchestrator

    workspace = tmp_path_factory.mktemp("nios-v17-deep-market-ws")
    vault = tmp_path_factory.mktemp("nios-v17-deep-market-vault")
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
        except Exception:  # pragma: no cover - shutdown is best effort
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
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    return {"X-NIOS-Session": token}


@pytest.fixture(scope="module")
def funded(runtime) -> dict[str, str]:
    from ring3_cockpit.gateway.control_plane import PaperFundingRequest

    allocated: dict[str, str] = {}
    for silo, amount in FUNDING.items():
        report = runtime.control.fund_paper(
            PaperFundingRequest(silo=silo, amount_units=amount, vault="ALL_CANONICAL")
        )
        assert report["ok"] is True, report
        allocated[silo] = str(report.get("workspace", "paper"))
    return allocated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paper_order(client, headers, silo, symbol, quantity, price):
    return client.post(
        "/api/v2/control/orders/paper",
        headers=headers,
        json={
            "silo": silo,
            "symbol": symbol,
            "side": "BUY",
            "quantity": quantity,
            "limit_price": price,
            "fill_immediately": True,
        },
    )


def _assert_digest(body: dict) -> None:
    digest = body.get("payload_sha256")
    assert isinstance(digest, str) and HEX64.match(digest), digest
    assert payload_digest(body) == digest


def _assert_grounded(block: dict) -> None:
    grounding = block["grounding"]
    assert HEX64.match(grounding["payload_sha256"]), grounding["payload_sha256"]
    assert grounding["verified"] is True
    assert grounding["metric_count"] >= 5
    assert grounding["knowledge_ts_ns"] <= grounding["as_of_ns"]


# ---------------------------------------------------------------------------
# Audit 1 -- 250+ multi-asset universe completeness
# ---------------------------------------------------------------------------


def test_audit1_universe_holds_two_hundred_and_fifty_plus_instruments() -> None:
    counts = instrument_registry.counts()
    assert instrument_registry.total() >= 250, instrument_registry.total()
    assert set(counts) == set(SILO_IDS)

    assert counts["INDIA_ZERODHA_DHAN"] >= 70, counts["INDIA_ZERODHA_DHAN"]
    assert counts["CRYPTO_BINANCE"] >= 80, counts["CRYPTO_BINANCE"]
    assert counts["US_IBKR"] >= 65, counts["US_IBKR"]
    assert counts["FX_MT5"] >= 35, counts["FX_MT5"]


def test_audit1_every_promised_symbol_resolves() -> None:
    india = set(instrument_registry.symbols("INDIA_ZERODHA_DHAN"))
    for symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX", "INDIAVIX"):
        assert symbol in india, symbol
    missing = [symbol for symbol in NIFTY_50 if symbol not in india]
    assert missing == [], missing

    fx = set(instrument_registry.symbols("FX_MT5"))
    missing_fx = [symbol for symbol in G10_PAIRS if symbol not in fx]
    assert missing_fx == [], missing_fx
    for symbol in ("USDZAR", "USDMXN", "USDSGD", "USDTRY", "USDCNH", "XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD", "USOIL", "UKOIL"):
        assert symbol in fx, symbol

    crypto = set(instrument_registry.symbols("CRYPTO_BINANCE"))
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "PEPEUSDT", "TAOUSDT", "ONDOUSDT", "NEIROUSDT"):
        assert symbol in crypto, symbol

    us = set(instrument_registry.symbols("US_IBKR"))
    for symbol in ("NVDA", "AAPL", "MSFT", "BRK.B", "SPY", "QQQ", "TLT", "GLD", "ES", "NQ", "CL", "GC"):
        assert symbol in us, symbol


def test_audit1_symbols_are_unique_across_all_silos() -> None:
    symbols = [spec.symbol for spec in instrument_registry.all()]
    assert len(symbols) == len(set(symbols)), "duplicate canonical symbols"
    assert len(instrument_registry.symbols()) == instrument_registry.total()


def test_audit1_every_contract_spec_is_complete_and_valid() -> None:
    for spec in instrument_registry.all():
        payload = spec.to_dict()
        for field in REQUIRED_SPEC_FIELDS:
            assert field in payload, (spec.silo, spec.symbol, field)
            assert payload[field] not in (None, ""), (spec.silo, spec.symbol, field)
        assert isinstance(payload["has_options"], bool)
        assert payload["display_name"] != payload["symbol"] or len(payload["symbol"]) > 3
        assert payload["lot_size"] > 0
        assert payload["tick_size_micros"] > 0
        assert payload["freeze_quantity"] >= payload["lot_size"]
        assert payload["circuit_limit_pct"] >= 0.0
        assert payload["margin_initial_pct"] > 0.0
        assert 0.0 <= payload["margin_maintenance_pct"] <= payload["margin_initial_pct"]
        assert payload["option_strike_step"] >= 0.0
        assert payload["settlement_cycle"] in SETTLEMENT_CYCLES
        assert re.match(r"^\d{2}:\d{2}-\d{2}:\d{2}$", payload["trading_hours_utc"])
        assert ":" in payload["isin_or_id"]
        assert payload["silo"] in SILO_IDS
        assert payload["currency"]
        assert payload["sector"]
        assert payload["sub_category"]


def test_audit1_sector_and_asset_class_histograms_are_complete() -> None:
    india = instrument_registry.sectors("INDIA_ZERODHA_DHAN")
    for sector in INDIA_SECTORS:
        assert sector in india, sector
    us = instrument_registry.sectors("US_IBKR")
    for sector in US_GICS_SECTORS:
        assert sector in us, sector
    crypto = instrument_registry.sectors("CRYPTO_BINANCE")
    for narrative in CRYPTO_NARRATIVES:
        assert narrative in crypto, narrative

    classes = instrument_registry.asset_classes()
    for asset_class in (
        "EQUITY",
        "ETF",
        "INDEX_FUTURE",
        "PERPETUAL",
        "FX",
        "METAL",
        "COMMODITY_FUTURE",
        "CURRENCY_FUTURE",
        "FUTURE",
    ):
        assert asset_class in classes, asset_class


def test_audit1_feature_store_covers_every_instrument() -> None:
    report = assert_feature_coverage()
    assert report["missing"] == []
    assert report["total_profiled"] == instrument_registry.total()
    assert report["per_profile"]["CRYPTO"] >= 80
    assert report["per_profile"]["FX_COMMODITY"] >= 35
    assert report["per_profile"]["EQUITY"] >= 60


# ---------------------------------------------------------------------------
# Audit 2 -- deep feature store and options surface completeness
# ---------------------------------------------------------------------------

DEEP_CASES = (
    ("RELIANCE", EQUITY_FIELDS),
    ("AAPL", EQUITY_FIELDS),
    ("NIFTY", EQUITY_FIELDS),
    ("BTCUSDT", CRYPTO_FIELDS),
    ("EURUSD", FX_COMMODITY_FIELDS),
)


@pytest.mark.parametrize("symbol,fields", DEEP_CASES)
def test_audit2_detail_endpoint_returns_deep_asset_class_profile(
    client, headers, symbol, fields
) -> None:
    response = client.get(f"/api/v2/instruments/{symbol}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["found"] is True
    assert body["symbol"] == symbol
    assert body["contract"]["symbol"] == symbol
    assert body["contract"]["silo"] in SILO_IDS
    _assert_digest(body)

    features = body["feature_profile"]["features"]
    for field in fields:
        assert field in features, (symbol, field)
        assert features[field] not in (None, ""), (symbol, field)
    assert body["feature_metric_count"] >= 10
    _assert_grounded(body["feature_profile"])
    assert body["feature_payload_sha256"] == body["feature_profile"]["payload_sha256"]

    surface = body["options_surface"]
    for field in OPTIONS_SURFACE_FIELDS:
        assert field in surface, (symbol, field)
    assert 0.0 <= surface["iv_rank"] <= 100.0
    assert 0.0 <= surface["iv_percentile"] <= 100.0
    assert surface["net_gamma_exposure_usd"] != 0.0
    assert surface["pcr_oi"] > 0.0
    assert surface["pcr_volume"] > 0.0
    assert surface["max_pain_strike"] > 0.0
    assert surface["gex_regime"] in ("LONG_GAMMA", "SHORT_GAMMA")
    assert set(surface["term_structure"]) == {"7DTE", "30DTE", "60DTE", "90DTE"}
    for label, point in surface["term_structure"].items():
        assert point["atm_iv_pct"] > 0.0, (symbol, label)
        assert point["open_interest_contracts"] > 0.0, (symbol, label)
        assert point["volume_contracts"] > 0.0, (symbol, label)
    _assert_grounded(surface)

    cost = body["statutory_cost"]
    assert cost["floor_bps"] == pytest.approx(COST_FLOOR_BPS[body["contract"]["silo"]])
    assert cost["reconciles"] is True
    assert cost["total_micros"] == sum(component["micros"] for component in cost["components"])
    assert len(cost["components"]) >= 4

    decision = body["decision_score"]
    assert -100.0 <= decision["score"] <= 100.0
    assert decision["verdict"] in ("BUY", "ACCUMULATE", "HOLD", "REDUCE", "AVOID")
    _assert_grounded(decision)

    assert body["one_lot_check"]["ok"] is True


def test_audit2_asset_class_specific_fields_are_actually_distinct() -> None:
    equity = get_feature_profile("RELIANCE").features
    crypto = get_feature_profile("BTCUSDT").features
    fx = get_feature_profile("EURUSD").features

    assert "pe_ratio" in equity and "pe_ratio" not in crypto and "pe_ratio" not in fx
    assert "funding_rate_8h_pct" in crypto and "funding_rate_8h_pct" not in equity
    assert "rate_differential_bps" in fx and "rate_differential_bps" not in equity
    assert "cot_non_commercial" not in "".join(equity)  # spelling guard
    assert equity["week_52_high"] > equity["spot_reference"] > equity["week_52_low"]
    assert crypto["fdv_usd"] >= crypto["market_cap_usd"]
    assert crypto["narrative_tag"] == "Layer-1 Smart Contracts"
    assert fx["active_session"] in (
        "TOKYO",
        "LONDON",
        "NEW_YORK",
        "LONDON_NY_OVERLAP",
    )
    assert fx["pip_value_per_standard_lot"] > 0.0


def test_audit2_options_surface_covers_non_optionable_instruments() -> None:
    surface = get_options_surface_profile("SHIBUSDT")
    assert surface["optionable"] is False
    assert surface["modeled"] is True
    assert 0.0 <= surface["iv_rank"] <= 100.0
    assert surface["net_gamma_exposure_usd"] is not None
    for label in ("7DTE", "30DTE", "60DTE", "90DTE"):
        assert surface["term_structure"][label]["atm_iv_pct"] > 0.0


def test_audit2_grounding_hashes_are_verifiable_and_stable() -> None:
    first = get_feature_profile("NVDA")
    second = get_feature_profile("NVDA")
    assert first.packet.sha256_digest == second.packet.sha256_digest
    assert first.packet.verify() is True
    assert get_feature_profile("AAPL").packet.sha256_digest != first.packet.sha256_digest


@pytest.mark.parametrize("symbol", ("NOPE", "NOT_A_THING", "..", "a" * 200))
def test_audit2_unknown_and_traversal_symbols_are_rejected(client, headers, symbol) -> None:
    response = client.get(f"/api/v2/instruments/{symbol}", headers=headers)
    assert response.status_code in (404, 422), response.text


def test_audit2_encoded_traversal_is_never_resolved(client, headers) -> None:
    response = client.get("/api/v2/instruments/..%2F..%2Fetc%2Fpasswd", headers=headers)
    assert response.status_code in (404, 422), response.text
    assert "etc" not in response.text.lower() or "not found" in response.text.lower()


# ---------------------------------------------------------------------------
# Audit 3 -- search, filter and pagination accuracy
# ---------------------------------------------------------------------------


def test_audit3_catalog_reports_the_full_universe(client, headers) -> None:
    response = client.get("/api/v2/instruments", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] == instrument_registry.total()
    assert body["returned_count"] == 50
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["has_more"] is True
    assert set(body["silo_counts"]) == set(SILO_IDS)
    assert sum(body["silo_counts"].values()) == body["total_count"]
    _assert_digest(body)
    for item in body["items"]:
        assert item["feature_summary"]["payload_sha256"]
        assert item["detail_url"] == f"/api/v2/instruments/{item['symbol']}"


def test_audit3_silo_filter_is_exact(client, headers) -> None:
    response = client.get(
        "/api/v2/instruments",
        headers=headers,
        params={"silo": "CRYPTO_BINANCE", "limit": 500},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] >= 80
    assert body["returned_count"] == body["total_count"]
    assert all(item["silo"] == "CRYPTO_BINANCE" for item in body["items"])


def test_audit3_sector_filter_is_exact(client, headers) -> None:
    response = client.get(
        "/api/v2/instruments",
        headers=headers,
        params={"sector": "Information Technology", "limit": 500},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] >= 10
    assert all(item["sector"] == "Information Technology" for item in body["items"])


def test_audit3_has_options_filter_is_exact(client, headers) -> None:
    response = client.get(
        "/api/v2/instruments",
        headers=headers,
        params={"has_options": "true", "limit": 500},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] > 0
    assert all(item["has_options"] is True for item in body["items"])

    off = client.get(
        "/api/v2/instruments",
        headers=headers,
        params={"has_options": "false", "limit": 500},
    ).json()
    assert all(item["has_options"] is False for item in off["items"])
    assert body["total_count"] + off["total_count"] == instrument_registry.total()


def test_audit3_search_matches_symbol_name_and_sector(client, headers) -> None:
    by_symbol = client.get(
        "/api/v2/instruments", headers=headers, params={"search": "btcusdt"}
    ).json()
    assert by_symbol["total_count"] == 1
    assert by_symbol["items"][0]["symbol"] == "BTCUSDT"

    by_name = client.get(
        "/api/v2/instruments", headers=headers, params={"search": "bitcoin"}
    ).json()
    assert by_name["total_count"] >= 2
    assert {"BTCUSDT", "BCHUSDT"} <= {item["symbol"] for item in by_name["items"]}

    by_sector = client.get(
        "/api/v2/instruments", headers=headers, params={"search": "semiconductor"}
    ).json()
    assert by_sector["total_count"] >= 3


def test_audit3_pagination_walks_every_row_exactly_once(client, headers) -> None:
    seen: list[str] = []
    offset = 0
    total = None
    while True:
        body = client.get(
            "/api/v2/instruments",
            headers=headers,
            params={"limit": 100, "offset": offset},
        ).json()
        total = body["total_count"]
        assert body["returned_count"] == len(body["items"])
        assert body["offset"] == offset
        seen.extend(item["symbol"] for item in body["items"])
        if not body["has_more"]:
            break
        offset += body["limit"]

    assert total == instrument_registry.total()
    assert len(seen) == total
    assert len(set(seen)) == total
    assert set(seen) == set(instrument_registry.symbols())


@pytest.mark.parametrize("params", ({"limit": 0}, {"limit": 501}, {"offset": -1}))
def test_audit3_out_of_range_pagination_is_rejected(client, headers, params) -> None:
    response = client.get("/api/v2/instruments", headers=headers, params=params)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "params",
    (
        {"silo": "MARS_EXCHANGE"},
        {"asset_class": "UNICORNS"},
        {"sector": "Vibes"},
        {"search": "x" * 80},
    ),
)
def test_audit3_unknown_filters_are_rejected(client, headers, params) -> None:
    response = client.get("/api/v2/instruments", headers=headers, params=params)
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Audit 4 -- macro calendar and the four cross-asset heatmaps
# ---------------------------------------------------------------------------


def test_audit4_heatmaps_cover_all_four_assets(client, headers) -> None:
    response = client.get("/api/v2/market/heatmaps", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_digest(body)

    india = body["india_sectors"]
    names = [row["sector"] for row in india["sectors"]]
    for sector in INDIA_SECTORS:
        assert sector in names, sector
    assert india["sector_count"] >= 10

    us = body["us_gics_sectors"]
    us_names = [row["sector"] for row in us["sectors"]]
    for sector in US_GICS_SECTORS:
        assert sector in us_names, sector
    assert us["sector_count"] >= 11

    crypto = body["crypto_narratives"]
    narratives = [row["sector"] for row in crypto["sectors"]]
    for narrative in CRYPTO_NARRATIVES:
        assert narrative in narratives, narrative

    fx = body["fx_g10_strength"]
    assert fx["currency_count"] == 8
    assert {row["currency"] for row in fx["currencies"]} == {
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "CHF",
        "AUD",
        "CAD",
        "NZD",
    }
    assert fx["pair_count"] == 28
    for row in fx["currencies"]:
        assert -100.0 <= row["relative_strength"] <= 100.0
        assert -100.0 <= row["carry_adjusted_momentum"] <= 100.0
        assert row["policy_rate_pct"] >= 0.0
    for pair, cell in fx["matrix"].items():
        assert cell["base"] and cell["quote"]
        assert -200.0 <= cell["relative_strength_differential"] <= 200.0
    assert len(fx["ranked_strongest_to_weakest"]) == 8


@pytest.mark.parametrize(
    "block,key",
    (
        ("india_sectors", "sectors"),
        ("us_gics_sectors", "sectors"),
        ("crypto_narratives", "sectors"),
    ),
)
def test_audit4_sector_breadth_adds_up(client, headers, block, key) -> None:
    heatmaps = client.get("/api/v2/market/heatmaps", headers=headers).json()
    section = heatmaps[block]
    rows = section[key]
    assert rows, block

    advancers = 0
    decliners = 0
    unchanged = 0
    for row in rows:
        assert row["member_count"] == len(row["members"])
        assert row["advancers"] + row["decliners"] + row["unchanged"] == row["member_count"]
        assert 0.0 <= row["breadth_pct"] <= 100.0
        assert row["top_gainer"]["change_pct"] >= row["top_laggard"]["change_pct"]
        assert row["top_gainer"]["symbol"] in {m["symbol"] for m in row["members"]}
        assert row["top_laggard"]["symbol"] in {m["symbol"] for m in row["members"]}
        assert row["total_market_cap_usd"] > 0.0
        advancers += row["advancers"]
        decliners += row["decliners"]
        unchanged += row["unchanged"]

    breadth = section["breadth"]
    assert breadth["advancers"] == advancers
    assert breadth["decliners"] == decliners
    assert breadth["unchanged"] == unchanged
    assert breadth["instrument_count"] == advancers + decliners + unchanged
    assert section["best_sector"] == rows[0]["sector"]


def test_audit4_macro_calendar_is_complete_and_grounded(client, headers) -> None:
    response = client.get("/api/v2/market/macro-calendar", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_digest(body)

    assert body["total_events"] >= 12
    assert body["returned_events"] == body["total_events"]
    assert set(body["impact_tiers"]) == {"HIGH", "MEDIUM"}

    ids = {event["event_id"] for event in body["events"]}
    for event_id in (
        "IN_RBI_MPC",
        "IN_CPI_YOY",
        "IN_IIP_YOY",
        "IN_GST_COLLECTIONS",
        "US_FOMC",
        "US_CPI_CORE_MOM",
        "US_CPI_CORE_YOY",
        "US_NFP",
        "US_ISM_MFG",
        "US_PCE_CORE",
        "EU_ECB_RATE",
        "UK_BOE_RATE",
        "JP_BOJ_STATEMENT",
        "US_EIA_CRUDE",
        "CRYPTO_BTC_QUARTERLY_EXPIRY",
    ):
        assert event_id in ids, event_id

    for event in body["events"]:
        assert event["impact_tier"] in ("HIGH", "MEDIUM")
        assert event["status"] in ("RELEASED", "SCHEDULED")
        assert event["consensus"]
        assert event["previous"]
        assert event["volatility_multiplier"] > 0.0
        assert event["silos_impacted"]
        assert set(event["silos_impacted"]) <= set(SILO_IDS)
        assert event["release_utc"].endswith("Z")
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z$", event["release_utc"])

    regions = {event["region"] for event in body["events"]}
    for region in ("INDIA", "US", "EUROPE", "UK", "JAPAN", "CRYPTO"):
        assert region in regions, region


def test_audit4_macro_calendar_filters(client, headers) -> None:
    us = client.get(
        "/api/v2/market/macro-calendar", headers=headers, params={"region": "US"}
    )
    assert us.status_code == 200, us.text
    body = us.json()
    assert body["returned_events"] < body["total_events"]
    assert all(event["region"] == "US" for event in body["events"])
    _assert_digest(body)

    high = client.get(
        "/api/v2/market/macro-calendar", headers=headers, params={"impact": "HIGH"}
    ).json()
    assert all(event["impact_tier"] == "HIGH" for event in high["events"])
    assert high["counts"]["MEDIUM"] == 0

    both = client.get(
        "/api/v2/market/macro-calendar",
        headers=headers,
        params={"region": "US", "impact": "HIGH"},
    ).json()
    assert both["returned_events"] >= 3
    assert all(
        event["region"] == "US" and event["impact_tier"] == "HIGH"
        for event in both["events"]
    )


@pytest.mark.parametrize(
    "params", ({"impact": "LOW"}, {"impact": "critical"}, {"region": "ATLANTIS"})
)
def test_audit4_macro_calendar_rejects_unknown_filters(client, headers, params) -> None:
    response = client.get(
        "/api/v2/market/macro-calendar", headers=headers, params=params
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Audit 5 -- SmartOMS / WalkForwardBacktestEngine integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symbol,silo,lot,price,floor", AUDIT_5_CASES)
def test_audit5_paper_order_enforces_registry_lot_and_statutory_floor(
    client, headers, funded, symbol, silo, lot, price, floor
) -> None:
    response = _paper_order(client, headers, silo, symbol, lot, price)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] in ("FILLED", "WORKING")
    assert body["mode"] == "PAPER"
    assert body["silo"] == silo
    assert body["symbol"] == symbol

    instrument = body["instrument"]
    assert instrument["symbol"] == symbol
    assert instrument["lot_size"] == pytest.approx(lot)
    check = body["instrument_check"]
    assert check["ok"] is True
    assert check["lot_multiple"] == pytest.approx(1.0)
    assert int(round(check["adjusted_price"] * MICROS)) % instrument["tick_size_micros"] == 0

    cost = body["statutory_cost"]
    assert cost["floor_bps"] == pytest.approx(floor)
    assert cost["silo"] == silo
    assert cost["reconciles"] is True
    assert cost["total_micros"] == sum(row["micros"] for row in cost["components"])
    assert cost["total_micros"] > 0
    assert {row["key"] for row in cost["components"]} >= {"modeled_slippage_bps"}


@pytest.mark.parametrize("symbol,silo,lot,price,floor", AUDIT_5_CASES)
def test_audit5_off_lot_quantity_is_rejected(
    client, headers, funded, symbol, silo, lot, price, floor
) -> None:
    response = _paper_order(client, headers, silo, symbol, lot * 1.5, price)
    assert response.status_code == 422, response.text
    assert "INSTRUMENT_REJECTED" in response.text
    assert "lot_size" in response.text


@pytest.mark.parametrize("symbol,silo,lot,price,floor", AUDIT_5_CASES)
def test_audit5_above_freeze_quantity_is_rejected(
    client, headers, funded, symbol, silo, lot, price, floor
) -> None:
    spec = instrument_registry.get(silo, symbol)
    over = spec.freeze_quantity + spec.lot_size
    response = _paper_order(client, headers, silo, symbol, over, price)
    assert response.status_code == 422, response.text
    if over <= 1_000_000.0:
        # Inside the request schema ceiling: the exchange freeze is the blocker.
        assert "freeze_quantity" in response.text
    else:
        # Above the request schema ceiling: the schema refuses it before the OMS.
        assert "less than or equal to" in response.text


def test_audit5_off_tick_limit_price_is_snapped_to_the_grid(
    client, headers, funded
) -> None:
    spec = instrument_registry.get("US_IBKR", "NVDA")
    response = _paper_order(client, headers, "US_IBKR", "NVDA", spec.lot_size, 145.037)
    assert response.status_code == 200, response.text
    body = response.json()
    check = body["instrument_check"]
    assert check["price_changed"] is True
    assert check["adjusted_price"] != 145.037
    assert int(round(check["adjusted_price"] * MICROS)) % spec.tick_size_micros == 0
    assert int(round(body["fill_price"] * MICROS)) % spec.tick_size_micros == 0


@pytest.mark.parametrize("symbol,silo,lot,price,floor", AUDIT_5_CASES)
def test_audit5_tax_net_backtest_deducts_the_silo_floor(
    client, headers, funded, symbol, silo, lot, price, floor
) -> None:
    response = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": silo,
            "symbol": symbol,
            "strategy_id": "ma_golden_cross",
            "bars": 252,
            "timeframe": "1d",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["silo"] == silo
    assert body["symbol"] == symbol
    assert body["cost_floor_bps"] == pytest.approx(floor)
    cost = body["statutory_cost"]
    assert cost["floor_bps"] == pytest.approx(floor)
    assert cost["reconciles"] is True
    assert cost["total_micros"] == sum(row["micros"] for row in cost["components"])
    assert len(cost["components"]) >= 4
    assert "net_return_bps" in body or "net_pnl" in body or "metrics" in body


def test_audit5_smart_oms_rejects_unknown_instruments(
    client, headers, funded
) -> None:
    response = _paper_order(
        client, headers, "US_IBKR", "NOT_A_TICKER", 1.0, 100.0
    )
    assert response.status_code == 422, response.text
    assert "INSTRUMENT_REJECTED" in response.text


def test_audit5_ledger_double_entry_survives_the_audit(client, headers, funded) -> None:
    invariant = client.get("/api/v2/cockpit/ledger", headers=headers).json()["invariant"]
    assert invariant["holds"] is True
    assert invariant["violations"] == []
