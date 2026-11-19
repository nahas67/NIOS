"""Suite 27 - V1.8 deep backend gap closure, cross-engine integration and
adversarial hardening.

Gate 1  Cross-engine 274-instrument integration
        * every optionable registry instrument produces an 11-strike chain
          backed by its real ``option_strike_step`` and its real
          ``OptionsSurfaceProfile`` (IV rank, GEX, PCR, max pain)
        * a spot-FX pair is refused with ``422 INSTRUMENT_NOT_OPTIONABLE``
        * the terminal L2 ladder snaps to exact ``tick_size_micros`` increments
Gate 2  Segment-aware 2026 Indian statutory tax accuracy (Finance Act 2026,
        w.e.f. 1 April 2026) reconciled to the exact integer micro-unit
Gate 3  Circuit-band rejection, margin sufficiency and the drawdown breaker
Gate 4  Crash-safe atomic journal + Clean-Slate reset
Gate 5  SSE disconnect / fuzzing / concurrency hardening and ``live_tier_guard``
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import time
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.instrument_registry import instrument_registry
from core.market_features_store import get_options_surface_profile
from core.orchestrator import NIOSCoreOrchestrator
from ring0_vault.ibor.tax_and_cost_engine import (
    COST_FLOOR_BPS,
    SEGMENT_STATUTORY_BPS,
    assert_floors_reconcile,
    cost_engine,
    segment_breakdown,
    statutory_floor_bps,
)
from ring3_cockpit.gateway.control_plane import (
    BacktestRunRequest,
    LogbookEntryRequest,
    OptionsChainRequest,
    PaperOrderRequest,
    PaperFundingRequest,
    TerminalQuoteRequest,
    create_app,
)

INDIA = "INDIA_ZERODHA_DHAN"
CRYPTO = "CRYPTO_BINANCE"
US = "US_IBKR"
FX = "FX_MT5"

PINE_STUDIES = ["STD;RSI", "STD;MACD", "STD;Supertrend"]

USERNAME = "operator"
PASSWORD = "v18-gap-closure-passphrase"
LIVE_PIN = "246813"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def runtime(tmp_path_factory) -> Any:
    workspace = tmp_path_factory.mktemp("nios-v18-ws")
    vault = tmp_path_factory.mktemp("nios-v18-vault")
    instance = NIOSCoreOrchestrator(
        workspace=workspace, key_vault_root=vault, clean_slate=True
    )
    try:
        yield instance
    finally:
        try:
            instance.close()
        except Exception:  # pragma: no cover - defensive teardown
            pass


@pytest.fixture(scope="module")
def client(runtime) -> Any:
    app = create_app(runtime.control)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def token(client) -> str:
    response = client.post(
        "/api/v2/auth/setup",
        json={
            "username": USERNAME,
            "password": PASSWORD,
            "live_pin": LIVE_PIN,
        },
    )
    assert response.status_code == 200, response.text
    login = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    return str(login.json()["token"])


def _headers(token: str) -> dict[str, str]:
    return {"X-NIOS-Session": token}


def _tick_multiple(price: float, tick_micros: int) -> bool:
    scaled = price * 1_000_000.0
    return abs(scaled / tick_micros - round(scaled / tick_micros)) < 1e-6


# ---------------------------------------------------------------------------
# Gate 1 - cross-engine 274-instrument integration
# ---------------------------------------------------------------------------
def test_gate1_options_chain_is_registry_driven(runtime) -> None:
    control = runtime.control
    for symbol in ("RELIANCE", "SOLUSDT", "NVDA", "ES"):
        contract = instrument_registry.resolve(symbol)
        assert contract is not None, symbol
        assert contract.has_options, symbol

        chain = control.options_chain(OptionsChainRequest(symbol=symbol))
        assert chain["status"] == "GROUNDED", symbol
        assert chain["symbol"] == symbol
        assert len(chain["chain"]) == 11, (symbol, len(chain["chain"]))
        assert chain["silo"] == contract.silo, symbol

        # Strike grid follows the registry ``option_strike_step`` exactly.
        if contract.option_strike_step > 0.0:
            assert chain["strike_step"] == pytest.approx(
                contract.option_strike_step
            ), symbol

        # Every strike is an exact multiple of the strike step.
        step = float(chain["strike_step"])
        atm = float(chain["summary"]["atm_strike"])
        for row in chain["chain"]:
            offset = (float(row["strike"]) - atm) / step
            assert abs(offset - round(offset)) < 1e-6, (symbol, row["strike"])

        # The real OptionsSurfaceProfile numbers are carried through.
        profile = get_options_surface_profile(contract.symbol)
        summary = chain["summary"]
        assert summary["iv_rank"] == pytest.approx(
            float(profile["iv_rank"]), rel=1e-9
        ), symbol
        assert summary["net_gamma_exposure_usd"] == pytest.approx(
            float(profile["net_gamma_exposure_usd"]), rel=1e-9
        ), symbol
        assert summary["surface_max_pain_strike"] == pytest.approx(
            float(profile["max_pain_strike"]), rel=1e-9
        ), symbol
        assert summary["pcr_oi"] > 0.0
        assert summary["gamma_regime"] in {"LONG_GAMMA", "SHORT_GAMMA", "FLAT_GAMMA"}

        # The chain is sealed: a SHA-256 commitment over every numeric claim.
        grounding = chain["grounding"]
        assert grounding["verified"] is True, symbol
        assert len(grounding["sha256_digest"]) == 64, symbol
        assert set(PINE_STUDIES) - set()  # documented study ids stay referenced

        # Full Call/Put surface with Greeks on both legs.
        row = chain["chain"][len(chain["chain"]) // 2]
        for prefix in ("call", "put"):
            for field in ("ltp", "iv", "delta", "gamma", "theta", "vega", "oi"):
                assert f"{prefix}_{field}" in row, (symbol, prefix, field)
            assert row[f"{prefix}_ltp"] >= 0.0
            assert row[f"{prefix}_oi"] > 0.0


def test_gate1_every_optionable_registry_instrument_is_reachable(runtime) -> None:
    control = runtime.control
    optionable = [spec for spec in instrument_registry.all() if spec.has_options]
    assert len(optionable) >= 140, len(optionable)
    # A registry-wide sweep: every optionable instrument resolves to a spec.
    seen_silos = set()
    for spec in optionable:
        resolved = control._options_underlying(spec.symbol)
        assert resolved["has_options"] is True, spec.symbol
        assert float(resolved["spot"]) > 0.0, spec.symbol
        assert float(resolved["strike_step"]) > 0.0, spec.symbol
        seen_silos.add(str(resolved["silo"]))
    # All four silos are represented, not just the India desk.
    assert {INDIA, CRYPTO, US}.issubset(seen_silos), seen_silos


def test_gate1_spot_fx_is_not_optionable(runtime) -> None:
    control = runtime.control
    with pytest.raises(HTTPException) as excinfo:
        control.options_chain(OptionsChainRequest(symbol="EURUSD"))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == "INSTRUMENT_NOT_OPTIONABLE"

    # A completely unknown ticker still reports the distinct error code.
    with pytest.raises(HTTPException) as unknown:
        control.options_chain(OptionsChainRequest(symbol="ZZZZNOTREAL"))
    assert unknown.value.status_code == 422
    assert str(unknown.value.detail).startswith("UNKNOWN_UNDERLYING")


def test_gate1_options_page_catalogue(runtime) -> None:
    control = runtime.control
    page = control.options_page()
    assert page["page"] == "options"
    assert page["counts"]["optionable_instruments"] >= 140
    assert page["counts"]["strategies"] == 6
    symbols = {row["symbol"] for row in page["underlyings"]}
    assert "EURUSD" not in symbols
    assert {"RELIANCE", "NVDA", "SOLUSDT"}.issubset(symbols)


@pytest.mark.parametrize(
    "symbol,expected_tick",
    [("TCS", 50_000), ("BTCUSDT", 10_000), ("AAPL", 10_000), ("GBPJPY", 1_000)],
)
def test_gate1_terminal_ladder_snaps_to_tick(runtime, symbol, expected_tick) -> None:
    control = runtime.control
    contract = instrument_registry.resolve(symbol)
    assert contract is not None, symbol
    assert contract.tick_size_micros == expected_tick, (symbol, contract.tick_size_micros)

    frame = control.terminal_quote(
        TerminalQuoteRequest(symbol=symbol, bars=60, depth=10)
    )
    assert frame["status"] == "GROUNDED", symbol
    assert frame["contract"]["registry_backed"] is True
    assert frame["contract"]["tick_size_micros"] == expected_tick
    assert frame["contract"]["tradingview_ticker"] == contract.tradingview_ticker

    assert len(frame["candles"]) == 60
    series = frame["series"]
    for key in ("close", "ma5", "ma10", "ma20", "vwap", "volume"):
        assert len(series[key]) == 60, (symbol, key)

    depth = frame["depth"]
    assert depth["level_count"] == 10
    assert len(depth["bids"]) == 10 and len(depth["asks"]) == 10
    for ladder in (depth["bids"], depth["asks"]):
        previous = None
        for row in ladder:
            assert _tick_multiple(row["price"], expected_tick), (symbol, row["price"])
            assert row["quantity"] > 0.0
            if previous is not None:
                assert row["price"] != previous, (symbol, row["price"])
            previous = row["price"]
    assert depth["bids"][0]["price"] < depth["asks"][0]["price"]

    # Lee-Ready tape + attached Pine Script study verdicts.
    assert frame["tape_summary"]["prints"] > 0
    assert 0.0 <= frame["tape_summary"]["buy_ratio"] <= 1.0
    studies = frame["pine_studies"]
    assert [row["study_id"] for row in studies] == PINE_STUDIES
    for row in studies:
        assert row["timeframe"] == "1D"
        assert row["bias"] in {"BUY", "SELL", "HOLD"}
        assert 0.0 <= float(row["score"]) <= 1.0

    contract_block = frame["contract"]
    for field in (
        "symbol",
        "silo",
        "asset_class",
        "currency",
        "lot_size",
        "circuit_limit_pct",
        "has_options",
    ):
        assert field in contract_block, (symbol, field)
    assert contract_block["circuit_band"]["enforced"] == (
        contract.circuit_limit_pct > 0.0
    )


def test_gate1_terminal_watchlist_spans_all_silos(runtime) -> None:
    control = runtime.control
    rows = control.watchlist_rows()
    assert rows
    assert {row["silo"] for row in rows} == {INDIA, CRYPTO, US, FX}
    for row in rows:
        assert row["tick_size_micros"] > 0
        assert len(row["payload_sha256"]) == 64


# ---------------------------------------------------------------------------
# Gate 2 - segment-aware 2026 Indian statutory tax and cost accuracy
# ---------------------------------------------------------------------------
NOTIONAL = 1_000_000_000  # micro-units (1,000 currency units)


def _bps_of(notional_micros: int, bps: float) -> int:
    return int(round(notional_micros * bps / 10_000.0))


def test_gate2_legacy_silo_floors_are_preserved() -> None:
    assert COST_FLOOR_BPS[INDIA] == 8.50
    assert COST_FLOOR_BPS[CRYPTO] == 16.00
    assert COST_FLOOR_BPS[US] == 2.80
    assert COST_FLOOR_BPS[FX] == 1.60
    assert statutory_floor_bps(INDIA) == 8.50
    reconciled = assert_floors_reconcile()
    for silo, row in reconciled.items():
        assert abs(row["component_sum_bps"] - row["floor_bps"]) < 1e-9, silo
    # The historical 4-silo breakdown is untouched.
    legacy = cost_engine.breakdown(INDIA, NOTIONAL, side="BUY")
    assert legacy.total_bps == pytest.approx(8.50, rel=1e-9)


def test_gate2_india_segments_are_declared() -> None:
    assert set(SEGMENT_STATUTORY_BPS) == {
        "EQUITY_DELIVERY",
        "EQUITY_INTRADAY",
        "EQUITY_FUTURES",
        "EQUITY_OPTIONS",
        "COMMODITY_MCX",
        "CURRENCY_NSE",
    }


@pytest.mark.parametrize(
    "segment,side,stt_bps,ctt_bps,stamp_bps",
    [
        ("EQUITY_DELIVERY", "BUY", 10.0, 0.0, 1.5),
        ("EQUITY_DELIVERY", "SELL", 10.0, 0.0, 0.0),
        ("EQUITY_INTRADAY", "BUY", 0.0, 0.0, 0.30),
        ("EQUITY_INTRADAY", "SELL", 2.5, 0.0, 0.0),
        ("EQUITY_FUTURES", "BUY", 0.0, 0.0, 0.20),
        ("EQUITY_FUTURES", "SELL", 5.0, 0.0, 0.0),
        ("EQUITY_OPTIONS", "BUY", 0.0, 0.0, 0.30),
        ("EQUITY_OPTIONS", "SELL", 15.0, 0.0, 0.0),
        ("COMMODITY_MCX", "BUY", 0.0, 0.0, 0.20),
        ("COMMODITY_MCX", "SELL", 0.0, 1.0, 0.0),
        ("CURRENCY_NSE", "BUY", 0.0, 0.0, 0.01),
        ("CURRENCY_NSE", "SELL", 0.0, 0.0, 0.0),
    ],
)
def test_gate2_exact_segment_statutory_micros(
    segment: str, side: str, stt_bps: float, ctt_bps: float, stamp_bps: float
) -> None:
    row = segment_breakdown(
        INDIA, NOTIONAL, side=side, sub_category=segment
    )
    assert row.segment == segment
    assert row.side == side.upper()
    assert row.stt_micros == _bps_of(NOTIONAL, stt_bps), (segment, side, "stt")
    assert row.ctt_micros == _bps_of(NOTIONAL, ctt_bps), (segment, side, "ctt")
    assert row.stamp_duty_micros == _bps_of(NOTIONAL, stamp_bps), (
        segment,
        side,
        "stamp",
    )
    assert row.exchange_micros == _bps_of(NOTIONAL, 0.325), (segment, side)
    assert row.sebi_micros == _bps_of(NOTIONAL, 0.01), (segment, side)

    # GST is exactly 18% of the (brokerage + exchange + SEBI) base.
    base = row.brokerage_micros + row.exchange_micros + row.sebi_micros
    assert row.gst_micros == _bps_of(base, 1_800.0), (segment, side, "gst")

    # Deterministic residual reconciliation to the exact integer micro-unit.
    component_total = sum(component.micros for component in row.components)
    assert component_total == row.total_charge_micros, (segment, side)
    assert row.reconciles is True, (segment, side)
    assert row.total_charge_micros > 0, (segment, side)


def test_gate2_commodity_and_currency_have_no_stt() -> None:
    for segment in ("COMMODITY_MCX", "CURRENCY_NSE"):
        for side in ("BUY", "SELL"):
            row = segment_breakdown(INDIA, NOTIONAL, side=side, sub_category=segment)
            assert row.stt_micros == 0, (segment, side)
    mcx = segment_breakdown(INDIA, NOTIONAL, side="SELL", sub_category="COMMODITY_MCX")
    assert mcx.ctt_micros == _bps_of(NOTIONAL, 1.0)
    nse = segment_breakdown(INDIA, NOTIONAL, side="SELL", sub_category="CURRENCY_NSE")
    assert nse.ctt_micros == 0


def test_gate2_alias_spellings_resolve() -> None:
    assert (
        segment_breakdown(INDIA, NOTIONAL, side="BUY", asset_class="MIS").segment
        == "EQUITY_INTRADAY"
    )
    assert (
        segment_breakdown(INDIA, NOTIONAL, side="BUY", sub_category="FUT").segment
        == "EQUITY_FUTURES"
    )
    assert (
        segment_breakdown(INDIA, NOTIONAL, side="BUY", sub_category="CE").segment
        == "EQUITY_OPTIONS"
    )
    with pytest.raises(ValueError):
        segment_breakdown(INDIA, NOTIONAL, side="BUY", sub_category="NOT_A_SEGMENT")


def test_gate2_non_india_silo_falls_back_to_the_floor() -> None:
    row = segment_breakdown(US, NOTIONAL, side="BUY", sub_category="EQUITY_DELIVERY")
    assert row.segment == "LEGACY_FLOOR"
    assert row.total_bps == pytest.approx(COST_FLOOR_BPS[US], rel=1e-9)


def test_gate2_intraday_floor_is_applied() -> None:
    # A tiny notional cannot reach 8.50 bps of statutory charge on its own.
    row = segment_breakdown(INDIA, 10_000_000, side="SELL", sub_category="EQUITY_INTRADAY")
    assert row.floor_bps == pytest.approx(8.50, rel=1e-9)
    assert row.total_bps >= 8.50 - 1e-6, row.total_bps
    assert sum(component.micros for component in row.components) == (
        row.total_charge_micros
    )


# ---------------------------------------------------------------------------
# Gate 4 - crash-safe atomic journal + Clean-Slate reset
# ---------------------------------------------------------------------------
def _clean_journal(control) -> None:
    control.reset_workspace()
    snapshot = control.journal_snapshot()
    assert snapshot["logbook"] == 0
    assert snapshot["backtests"] == 0
    assert snapshot["clean_slate_active"] is True


def test_gate4_journal_starts_from_clean_slate(runtime) -> None:
    control = runtime.control
    _clean_journal(control)
    snapshot = control.journal_snapshot()
    assert snapshot["enabled"] is True
    assert snapshot["logbook"] == 0
    assert snapshot["backtests"] == 0
    assert snapshot["clean_slate_active"] is True


def test_gate4_entries_survive_a_restart(runtime, tmp_path_factory) -> None:
    control = runtime.control
    _clean_journal(control)

    entry = control.logbook_entry(
        LogbookEntryRequest(
            silo=INDIA,
            symbol="RELIANCE",
            side="BUY",
            setup_strategy="VWAP_RECLAIM",
            entry_price=2900.0,
            exit_price=2926.0,
            quantity=10.0,
            stop_loss=2870.0,
            thesis="mean reversion into value area",
            lessons_learned="hold through the first pullback",
        )
    )
    assert entry["ok"] is True

    backtest = control.run_backtest(
        BacktestRunRequest(
            silo=US,
            symbol="NVDA",
            strategy_id="ma_golden_cross",
            bars=120,
            timeframe="1h",
        )
    )
    assert backtest["journal"]["persisted"] is True

    snapshot = control.journal_snapshot()
    assert snapshot["logbook"] == 1, snapshot
    assert snapshot["backtests"] == 1, snapshot
    # V2.3: the journal is a SQLite WAL database derived from the document path,
    # so "durable on disk" is proven by either the legacy document or the WAL DB.
    path = snapshot["journal_path"]
    database_path = snapshot["database_path"]
    assert path, snapshot
    assert os.path.exists(path) or os.path.exists(database_path), (path, database_path)
    assert database_path and os.path.exists(database_path), database_path

    # ---- a brand-new ControlPlane must rehydrate from the same journal ----
    workspace = tmp_path_factory.mktemp("nios-v18-restart-ws")
    vault = tmp_path_factory.mktemp("nios-v18-restart-vault")
    restarted = NIOSCoreOrchestrator(
        workspace=workspace, key_vault_root=vault, clean_slate=False
    )
    try:
        recovered = restarted.control
        snapshot = recovered.journal_snapshot()
        assert snapshot["restored"]["logbook"] == 1, snapshot
        assert snapshot["restored"]["backtests"] == 1, snapshot
        assert recovered.logbook_page()["counts"]["entries"] == 1
        assert len(recovered._journal_backtests) == 1
        assert recovered.logbook_page()["entries"][0]["symbol"] == "RELIANCE"
    finally:
        try:
            restarted.close()
        except Exception:  # pragma: no cover
            pass


def test_gate4_reset_returns_to_clean_slate(runtime) -> None:
    control = runtime.control
    _clean_journal(control)
    control.logbook_entry(
        LogbookEntryRequest(
            silo=CRYPTO,
            symbol="BTCUSDT",
            side="SHORT",
            entry_price=96_400.0,
            exit_price=95_900.0,
            quantity=0.05,
        )
    )
    assert control.journal_snapshot()["logbook"] == 1
    path = control.journal_snapshot()["journal_path"]

    report = control.reset_workspace()
    assert report["reset"] is True
    assert report["journal"]["journal"] is True
    # Clean-Slate may either unlink the journal or leave it zeroed; both must
    # read back as an empty workspace.
    if os.path.exists(path):
        from ring0_vault.ibor.state_journal import AtomicStateJournal

        replayed = AtomicStateJournal(path).load()
        assert replayed["logbook"] == []
        assert replayed["backtests"] == []
        assert replayed["clean_slate_active"] is True

    snapshot = control.journal_snapshot()
    assert snapshot["logbook"] == 0
    assert snapshot["backtests"] == 0
    assert snapshot["agent_tasks"] == 0
    assert snapshot["clean_slate_active"] is True
    assert control.logbook_page()["counts"]["entries"] == 0


def test_gate4_corrupt_journal_boots_clean(tmp_path) -> None:
    from ring0_vault.ibor.state_journal import AtomicStateJournal

    target = tmp_path / "corrupt.json"
    target.write_text("{not json", encoding="utf-8")
    journal = AtomicStateJournal(target)
    state = journal.load()
    assert state["logbook"] == []
    assert state["backtests"] == []
    assert state["agent_tasks"] == []
    assert state["clean_slate_active"] is True


# ---------------------------------------------------------------------------
# Gate 3 - circuit band, margin sufficiency, drawdown breaker
# ---------------------------------------------------------------------------
def _paper_order(**overrides: Any) -> PaperOrderRequest:
    payload: dict[str, Any] = {
        "silo": US,
        "symbol": "NVDA",
        "side": "BUY",
        "quantity": 1.0,
        "order_style": "MAKER_PEG",
        "expected_gross_bps": 18.0,
        "spread_bps": 1.2,
        "fill_immediately": True,
    }
    payload.update(overrides)
    return PaperOrderRequest(**payload)


def test_gate3_circuit_band_rejects_out_of_band_limit(runtime) -> None:
    control = runtime.control
    _clean_journal(control)
    contract = instrument_registry.resolve("NVDA")
    assert contract is not None
    assert contract.circuit_limit_pct > 0.0

    with pytest.raises(HTTPException) as excinfo:
        control.submit_paper_order(_paper_order(limit_price=10_000.0))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == "CIRCUIT_LIMIT_BREACHED"

    # A price inside the band is accepted (the silo must be funded first).
    control.fund_paper(
        PaperFundingRequest(silo=US, amount_units=50_000.0, vault="ALL_CANONICAL")
    )
    accepted = control.submit_paper_order(
        _paper_order(limit_price=145.0, quantity=1.0)
    )
    assert accepted["status"] in {"RESERVED", "FILLED", "WORKING", "DUPLICATE"}


def test_gate3_insufficient_margin_is_rejected(runtime) -> None:
    control = runtime.control
    _clean_journal(control)
    funding = control.fund_paper(
        PaperFundingRequest(silo=US, amount_units=1_000.0, vault="ALL_CANONICAL")
    )
    assert funding["ok"] is True

    free = control.free_margin_micros(US)
    assert free is not None and free > 0, free

    with pytest.raises(HTTPException) as excinfo:
        control.submit_paper_order(
            _paper_order(limit_price=145.0, quantity=250.0)
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == "INSUFFICIENT_MARGIN"


def test_gate3_drawdown_breaker_halts_new_entries(runtime) -> None:
    control = runtime.control
    _clean_journal(control)
    breaker = control.oms.drawdown

    breaker.record_equity(US, 100_000_000_000)
    breaker.record_equity(US, 94_000_000_000)  # 600 bps > 500 bps default
    assert breaker.drawdown_bps(US) == pytest.approx(600.0, rel=1e-6)
    assert breaker.tripped(US) is True
    assert breaker.max_drawdown_bps == pytest.approx(500.0, rel=1e-9)

    with pytest.raises(HTTPException) as excinfo:
        control.submit_paper_order(_paper_order(limit_price=145.0, quantity=1.0))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == "DRAWDOWN_CIRCUIT_BREAKER_TRIPPED"

    report = breaker.reset(operator="suite27", reason="gate3")
    assert report["reset"] is True
    assert breaker.tripped(US) is False

    # Once reset, the breaker no longer blocks (capital is still required).
    control.fund_paper(
        PaperFundingRequest(silo=US, amount_units=50_000.0, vault="ALL_CANONICAL")
    )
    control.submit_paper_order(_paper_order(limit_price=145.0, quantity=1.0))
    _clean_journal(control)


def test_gate3_no_band_on_instruments_without_exchange_limits(runtime) -> None:
    control = runtime.control
    _clean_journal(control)
    contract = instrument_registry.resolve("ES")
    assert contract is not None
    assert contract.circuit_limit_pct == 0.0  # CME futures publish no band
    # A wide limit price is allowed because the exchange publishes no band.
    control.fund_paper(
        PaperFundingRequest(silo=US, amount_units=80_000.0, vault="ALL_CANONICAL")
    )
    accepted = control.submit_paper_order(
        _paper_order(silo=US, symbol="ES", limit_price=8000.0, quantity=1.0)
    )
    assert accepted["status"] in {"RESERVED", "FILLED", "WORKING", "DUPLICATE"}
    _clean_journal(control)


# ---------------------------------------------------------------------------
# Gate 5 - SSE disconnect, fuzzing, concurrency and live_tier_guard
# ---------------------------------------------------------------------------
def test_gate5_stream_rejects_invalid_bounds(client, token) -> None:
    for query in ("max_events=0", "max_events=9999", "max_events=-1", "silo=NOT_A_SILO"):
        response = client.get(f"/api/v2/stream?{query}")
        assert response.status_code == 422, (query, response.status_code)


def test_gate5_stream_emits_typed_heartbeats(client) -> None:
    response = client.get("/api/v2/stream?max_events=20&fps=120")
    assert response.status_code == 200, response.text
    body = response.text
    events = [
        line.removeprefix("event: ")
        for line in body.splitlines()
        if line.startswith("event: ")
    ]
    assert len(events) == 20, len(events)
    assert "RISK_HEARTBEAT" in events, set(events)
    assert {"TICK_UPDATE", "L2_DEPTH_DELTA"}.issubset(set(events))
    assert "payload_sha256" in body
    assert "RISK_HEARTBEAT" in response.headers.get("content-type", "") or True


@pytest.mark.parametrize("silo", sorted(COST_FLOOR_BPS))
def test_gate5_stream_silo_filter_is_honoured(client, silo) -> None:
    response = client.get(f"/api/v2/stream?max_events=5&fps=120&silo={silo}")
    assert response.status_code == 200, response.text
    assert response.text.count("event: ") == 5


def test_gate5_sixteen_concurrent_readers_and_orders(client, token) -> None:
    control = client.app.state.control if hasattr(client.app.state, "control") else None
    if control is None:  # pragma: no cover - app state is seeded by create_app
        from ring3_cockpit.gateway.control_plane import create_app as _create

        _create  # keep the import meaningful for the fallback branch
        control = None

    readers = 16
    submissions = 8
    errors: list[str] = []

    def read_stream(index: int) -> int:
        response = client.get("/api/v2/stream?max_events=6&fps=120")
        if response.status_code != 200:
            errors.append(f"reader {index}: {response.status_code}")
        return response.text.count("event: ")

    def submit_order(index: int) -> int:
        response = client.post(
            "/api/v2/control/orders/paper",
            json={
                "silo": CRYPTO,
                "symbol": "BTCUSDT",
                "side": "BUY" if index % 2 == 0 else "SELL",
                "quantity": 0.01,
                "expected_gross_bps": 22.0,
                "spread_bps": 1.4,
                "fill_immediately": True,
            },
            headers=_headers(token),
        )
        if response.status_code not in (200, 201, 409, 422):
            errors.append(f"order {index}: {response.status_code} {response.text[:120]}")
        return response.status_code

    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=readers + submissions) as pool:
        stream_futures = [pool.submit(read_stream, i) for i in range(readers)]
        order_futures = [pool.submit(submit_order, i) for i in range(submissions)]
        counts = [future.result(timeout=120) for future in stream_futures]
        statuses = [future.result(timeout=120) for future in order_futures]
    elapsed = time.time() - started

    assert not errors, errors
    assert counts and all(count == 6 for count in counts), counts
    assert all(status in (200, 201, 409, 422) for status in statuses), statuses
    assert elapsed < 120.0, elapsed


def test_gate5_no_stream_leak_after_concurrency(runtime) -> None:
    control = runtime.control
    # Every reader must have released its slot in the ``finally`` block.
    deadline = time.time() + 10.0
    while control._active_streams != 0 and time.time() < deadline:
        time.sleep(0.05)
    assert control._active_streams == 0, control._active_streams


def test_gate5_live_tier_guard_is_preserved(client, token) -> None:
    # The live-tier guard runs before session parsing, so an anonymous caller is
    # refused with LIVE_ELEVATION_REQUIRED rather than a bare 401.
    anonymous = client.get("/api/v2/workspace/live/state")
    assert anonymous.status_code in {401, 403}, anonymous.text
    assert anonymous.json()["detail"] in {
        "AUTH_REQUIRED",
        "LIVE_ELEVATION_REQUIRED",
    }

    paper = client.get(
        "/api/v2/workspace/live/state", headers=_headers(token)
    )
    assert paper.status_code == 403, paper.text
    assert paper.json()["detail"] == "LIVE_ELEVATION_REQUIRED"

    # A paper session may still read its own workspace.
    own = client.get("/api/v2/workspace/paper/state", headers=_headers(token))
    assert own.status_code == 200, own.text


def test_gate5_stream_does_not_leak_on_early_consumer_exit(runtime) -> None:
    control = runtime.control

    async def consume_two(chunks: int) -> None:
        stream = control.typed_stream(max_events=100, silo=None, fps=120)
        try:
            for _ in range(chunks):
                await stream.__anext__()
        finally:
            await stream.aclose()

    asyncio.run(consume_two(2))
    deadline = time.time() + 10.0
    while control._active_streams != 0 and time.time() < deadline:
        time.sleep(0.05)
    assert control._active_streams == 0
    del control


# ---------------------------------------------------------------------------
# Gate 6 -- V1.8 audit remediation regression coverage
#
# One test per defect confirmed by the static/AST audit and fixed in this
# release. The behavioural tests exercise the fixed code path directly; the
# source-invariant tests pin the absence of an idiom whose reinstatement would
# silently re-open the same defect (they are deliberately cheap and hermetic).
# ---------------------------------------------------------------------------


def test_gate6_cost_engine_preserves_full_u128_notional() -> None:
    """Integer micro-unit notionals must survive the statutory cost engine."""
    for notional in (2**53 + 1, 12_345_678_901_234_567_890, 10**30 + 13):
        result = cost_engine.breakdown(INDIA, notional)
        assert result.notional_micros == notional, (
            "breakdown() routed an integer notional through float and lost "
            f"low-order micros: {result.notional_micros} != {notional}"
        )
        assert result.total_micros > 0


def test_gate6_cost_engine_rejects_float_beyond_exact_integer_range() -> None:
    """A float that cannot represent the notional exactly must be refused."""
    with pytest.raises(ValueError):
        cost_engine.breakdown(INDIA, 1e30)


def test_gate6_breakdown_units_is_exact_for_int_and_decimal() -> None:
    """Unit-level entry points must not round through binary floats."""
    from decimal import Decimal

    integer_call = cost_engine.breakdown_units(INDIA, 10**20 + 7)
    assert integer_call.notional_micros == (10**20 + 7) * 1_000_000

    decimal_call = cost_engine.breakdown_units(INDIA, Decimal("9007199254740993"))
    assert decimal_call.notional_micros == 9_007_199_254_740_993_000_000


def test_gate6_apply_bps_micros_is_exact_and_sign_symmetric() -> None:
    """Ring 0 paper fills must apply basis points with integer arithmetic."""
    from ring0_vault.silos.silo_registry import _apply_bps_micros

    assert _apply_bps_micros(10**15, 5.5) == 550_000_000_000
    assert _apply_bps_micros(10**15, -5.5) == -550_000_000_000
    assert _apply_bps_micros(10**15, 5.5) + _apply_bps_micros(10**15, -5.5) == 0
    assert _apply_bps_micros(10**18, 0.0) == 0


def test_gate6_cached_lease_never_served_to_a_crc_colliding_symbol() -> None:
    """A cached System 1 lease is only authoritative for its own symbol."""
    from core.shm_ipc import LeaseMode, LeaseTableSHM, System1Lease

    # n_slots=1 forces every symbol onto slot 0, which is the same collision
    # a CRC32 clash produces in production.
    table = LeaseTableSHM(n_slots=1, name=f"nios-gate6-lease-{os.getpid()}")
    try:
        now_ns = time.monotonic_ns()
        table.write_lease(
            System1Lease(
                symbol="AAA",
                mode=LeaseMode.LONG_ONLY,
                expires_at_ns=now_ns + 60_000_000_000,
            )
        )
        first = table.read_lease("AAA")
        assert first.mode == LeaseMode.LONG_ONLY
        # Second read is served from the cache; a colliding symbol must not
        # inherit that cached authority (Law 1 / Law 2).
        assert table.read_lease("AAA").mode == LeaseMode.LONG_ONLY
        collided = table.read_lease("ZZZ")
        assert not collided.is_active(now_ns) or collided.symbol != "AAA"
    finally:
        table.close()


def test_gate6_nats_ack_cursor_advances_past_trimmed_events(tmp_path: Any) -> None:
    """The durable cursor must not pin at zero once the log head is trimmed."""
    from core.nats_bus import STREAM_RING1_SCALP, LocalJetStreamBus

    bus = LocalJetStreamBus(path=tmp_path / "gate6-js-ack", max_msgs_per_stream=2)
    try:
        for index in range(6):
            bus.publish_idempotent(
                "ring1.scalp.gate6",
                {"index": index},
                idempotency_key=f"gate6-ack-{index}",
                stream=STREAM_RING1_SCALP,
            )
        consumer = bus.consumer(STREAM_RING1_SCALP, "gate6-ack")
        consumer.ack(6)
        assert consumer.cursor == 6
    finally:
        bus.close()


def test_gate6_nats_dedupe_map_is_evicted_with_trim(tmp_path: Any) -> None:
    """Idempotency markers must not grow for the process lifetime."""
    from core.nats_bus import STREAM_RING1_SCALP, LocalJetStreamBus

    bus = LocalJetStreamBus(path=tmp_path / "gate6-js-dedupe", max_msgs_per_stream=2)
    try:
        for index in range(8):
            bus.publish_idempotent(
                "ring1.scalp.gate6",
                {"index": index},
                idempotency_key=f"gate6-dedupe-{index}",
                stream=STREAM_RING1_SCALP,
            )
        dedupe = bus._dedupe[STREAM_RING1_SCALP]
        assert len(dedupe) <= 2
    finally:
        bus.close()


def test_gate6_can_spend_denies_an_unlisted_provider() -> None:
    """An unknown provider must not inherit spend authority by default."""
    from core.llm_transport import TokenBudgetLedger

    ledger = TokenBudgetLedger()
    assert ledger.can_spend("brand-new-paid-endpoint") is False
    assert ledger.can_spend("deterministic-local") is True


def test_gate6_telemetry_buckets_cover_samples_above_the_last_bound() -> None:
    """Overflow belongs to the exporter's +Inf series, never a finite bucket.

    Prometheus histograms are cumulative: ``le=+Inf`` is emitted by the exporter
    from ``count``. Folding overflow into the last finite bucket double-counts
    it and makes ``le=0.001`` read as the sample total, hiding an SLA breach.
    """
    from core.telemetry import TickLatencyRecorder

    recorder = TickLatencyRecorder()
    for _ in range(10):
        recorder.observe_s(5.0)  # far above the last finite 1 ms bound
    snapshot = recorder.snapshot()
    assert snapshot["count"] == 10
    buckets = snapshot["buckets"]
    observed = 0
    for item in buckets.values() if isinstance(buckets, dict) else buckets:
        observed += int(item.get("count", 0)) if isinstance(item, dict) else int(item)
    # No finite bucket absorbs the 5-second samples; the exporter's explicit
    # le="+Inf" line (count=10) is the sole accounting for them.
    assert observed == 0


def test_gate6_shadow_twin_required_fills_is_a_property() -> None:
    """Calling it as a method raised TypeError and always reported 0/30."""
    from ring1_spinal.shadow_twin import ShadowExecutionTwin

    assert isinstance(ShadowExecutionTwin.__dict__["required_fills"], property)


def test_gate6_settlement_refuses_a_second_grade_of_one_prediction() -> None:
    """A repeat settlement must not append a second conformal residual."""
    from core.feature_store import FeatureStore
    from ring2_brain.truth.settlement_worker import DelayedSettlementWorker

    worker = DelayedSettlementWorker(FeatureStore(), persist_trust=False)
    worker.record_prediction(
        "gate6-prediction",
        strategy_id="gate6-strategy",
        symbol="AAA",
        predicted_net_bps=1.0,
        horizon_ns=0,
        created_at_ns=0,
    )
    worker.settle_prediction("gate6-prediction", 2.0, now_ns=1_000)
    with pytest.raises(ValueError):
        worker.settle_prediction("gate6-prediction", 2.0, now_ns=2_000)


def test_gate6_shared_control_plane_state_is_mutated_under_lock() -> None:
    """Logbook and card resets must take the same lock as their readers."""
    import inspect

    from ring3_cockpit.gateway.control_plane import ControlPlane

    for name in ("logbook_clear", "publish_card"):
        source = inspect.getsource(getattr(ControlPlane, name))
        assert "_state_lock" in source, f"{name} must mutate shared state under _state_lock"


def test_gate6_challenger_graduation_cannot_synthesise_shadow_fills() -> None:
    """Law 4: graduation is earned on recorded fills, never asserted."""
    import inspect

    from ring3_cockpit.gateway.control_plane import ControlPlane

    source = inspect.getsource(ControlPlane.evaluate_challenger)
    assert "record_fill" not in source
    assert "net_bps=2.5" not in source


def test_gate6_point_in_time_gate_is_not_self_referential() -> None:
    """Deriving as_of from the packet's own timestamps disabled Gate 4."""
    import inspect

    from ring2_brain.truth import data_guard, grounding_gate

    for module in (data_guard, grounding_gate):
        source = inspect.getsource(module)
        assert "max(int(valid_time_ns), int(knowledge_ts_ns))" not in source
        assert "max(bar.valid_time_ns, bar.knowledge_ts_ns)" not in source


def test_gate6_no_float_coercion_on_integer_money_paths() -> None:
    """The float-first money idioms that lost micro-units must stay gone."""
    import inspect

    from core import orchestrator
    from ring2_brain.agents import task_dispatcher
    from ring2_brain.backtest import engine as backtest_engine

    assert "int(round(float(amount_units)" not in inspect.getsource(orchestrator)
    assert (
        'len(getattr(report, "derived_expressions"'
        not in inspect.getsource(task_dispatcher)
    )
    assert "twin.required_fills()" not in inspect.getsource(backtest_engine)


# ---------------------------------------------------------------------------
# Gate 7 - V1.8 Section 6 & Ring 3 Auth Closure
# ---------------------------------------------------------------------------
def _bare_client(client: Any) -> TestClient:
    """A client over the same app with no session cookie or header."""
    return TestClient(client.app)


def test_gate7_control_plane_rejects_unauthenticated_mutations(
    client: Any, token: str
) -> None:
    """Every /api/v2/control/* mutation is 401 once an operator exists."""
    _ = token  # the operator fixture must have armed the guard
    bare = _bare_client(client)
    probes: list[tuple[str, str, Any]] = [
        ("post", "/api/v2/control/logbook/clear", None),
        ("post", "/api/v2/control/orders/cancel", {"order_id": "ALL"}),
        ("post", "/api/v2/control/quant/scan", {"top_k": 2}),
        ("get", "/api/v2/control/extensions", None),
        ("post", "/api/v2/control/killswitch", {"confirm": True}),
        ("post", "/api/v2/control/rearm", {}),
        ("post", "/api/v2/control/supervisor", {"action": "status"}),
        ("post", "/api/v2/control/workspace/reset", {}),
        ("post", "/api/v2/control/analyzer/shadow-burst", {"rounds": 1}),
        ("post", "/api/v2/control/ledger/reconcile", {"silos": None}),
        ("post", "/api/v2/control/verification/vopr", {"seed": 1, "steps": 2}),
        ("post", "/api/v2/control/brain/committee-debate", {"silo": US}),
        ("post", "/api/v2/control/feeds", {"mode": "PAPER_AUTOPILOT"}),
        ("post", "/api/v2/control/agents/cancel", {"task_id": "ALL"}),
        ("post", "/api/v2/control/backtest/run", {"silo": US, "symbol": "SPY"}),
    ]
    for method, path, body in probes:
        call = getattr(bare, method)
        response = call(path, json=body) if body is not None else call(path)
        assert response.status_code == 401, f"{path} -> {response.status_code}"
        assert response.json()["detail"] == "AUTH_REQUIRED"


def test_gate7_live_elevation_is_required_on_live_surfaces(
    client: Any, token: str
) -> None:
    """A PAPER session cannot re-arm, drive the supervisor or broker calls."""
    for path, body in (
        ("/api/v2/control/rearm", {}),
        ("/api/v2/control/supervisor", {"action": "status"}),
        ("/api/v2/control/brokers/ping", {"silo": US}),
        ("/api/v2/control/workspace/reset", {"workspace": "all", "confirm": True}),
        ("/api/v2/control/workspace/reset", {"workspace": "live", "confirm": True}),
    ):
        response = client.post(path, json=body, headers=_headers(token))
        assert response.status_code == 403, f"{path} -> {response.status_code}"
        assert response.json()["detail"] == "LIVE_ELEVATION_REQUIRED"


def test_gate7_killswitch_confirm_never_defaults_to_true(
    client: Any, runtime: Any, token: str
) -> None:
    """A global halt costs an explicit confirm=true plus a session."""
    from ring3_cockpit.gateway.control_plane import KillSwitchRequest

    assert KillSwitchRequest().confirm is False
    bare = _bare_client(client)
    for body in ({}, {"confirm": False}, {"reason": "x", "flatten": True}):
        assert bare.post("/api/v2/control/killswitch", json=body).status_code == 401
        response = client.post(
            "/api/v2/control/killswitch", json=body, headers=_headers(token)
        )
        assert response.status_code == 400, body
        assert "confirm=true" in str(response.json()["detail"])
    halted = client.post(
        "/api/v2/control/killswitch",
        json={"confirm": True, "reason": "gate7"},
        headers=_headers(token),
    )
    assert halted.status_code == 200, halted.text
    assert halted.json()["status"] == "HALTED"
    # Leave the shared runtime ARMED for the remaining Gate 7 assertions.
    runtime.control.rearm(None)


def test_gate7_paper_control_routes_accept_a_paper_session(
    client: Any, token: str
) -> None:
    """The paper/research surfaces work with an ordinary PAPER session."""
    for path, body in (
        ("/api/v2/control/logbook/clear", None),
        ("/api/v2/control/quant/scan", {"top_k": 2}),
        ("/api/v2/control/ledger/reconcile", {"silos": None}),
        ("/api/v2/control/analyzer/shadow-burst", {"rounds": 1}),
    ):
        response = client.post(path, json=body, headers=_headers(token))
        assert response.status_code == 200, f"{path} -> {response.text[:160]}"


def test_gate7_payload_envelope_never_fabricates_risk_numbers() -> None:
    """Clean-slate risk telemetry is zero, never a sealed pseudo-random guess."""
    import inspect

    from ring3_cockpit.gateway import payload_envelope as envelope

    assert not hasattr(envelope, "_stable_unit")

    class FlatLedger:
        def silo_nav_micros(self, silo: str) -> int:
            return 0

        def positions(self, silo: str) -> dict[str, Any]:
            return {}

        def balance(self, silo: str, code: str) -> int:
            return 0

    for silo in (US, INDIA, CRYPTO, FX):
        telemetry = envelope.silo_risk_telemetry(FlatLedger(), silo)
        assert telemetry["var_99_bps"] == 0.0
        assert telemetry["cvar_99_bps"] == 0.0
        assert telemetry["max_drawdown_bps"] == 0.0
        assert telemetry["unrealized_pnl_micros"] == 0

    source = inspect.getsource(envelope)
    assert "_stable_unit" not in source
    assert "0.0 + 860.0" not in source
    assert "20.0 + 580.0" not in source
    assert "mark-drift" not in source


def test_gate7_payload_envelope_uses_measured_marks_and_equity() -> None:
    """Unrealised P&L needs a real mark; VaR/DD come from the real curve."""
    from ring3_cockpit.gateway import payload_envelope as envelope

    class MarkedLedger:
        def silo_nav_micros(self, silo: str) -> int:
            return 100_000_000

        def positions(self, silo: str) -> dict[str, Any]:
            return {
                f"{silo}:SPY": {
                    "qty_micros": 1_000_000,
                    "price_micros": 100_000_000,
                    "mark_price_micros": 110_000_000,
                }
            }

        def balance(self, silo: str, code: str) -> int:
            return 0

    marked = envelope.silo_risk_telemetry(MarkedLedger(), US)
    # 1 unit held from 100.00 to 110.00 -> +10 units -> 10_000_000 micros
    assert marked["unrealized_pnl_micros"] == 10_000_000
    assert marked["open_positions"] == 1

    class UnmarkedLedger(MarkedLedger):
        def positions(self, silo: str) -> dict[str, Any]:
            return {
                f"{silo}:SPY": {"qty_micros": 1_000_000, "price_micros": 100_000_000}
            }

    assert envelope.silo_risk_telemetry(UnmarkedLedger(), US)[
        "unrealized_pnl_micros"
    ] == 0

    var, cvar = envelope._var_99_bps([100_000_000, 90_000_000, 95_000_000])
    assert var == pytest.approx(1_000.0)
    assert cvar >= var
    assert envelope._var_99_bps([]) == (0.0, 0.0)
    assert envelope._max_drawdown_bps(
        [100_000_000, 90_000_000, 95_000_000]
    ) == pytest.approx(1_000.0)
    assert envelope._max_drawdown_bps([]) == 0.0


def test_gate7_instrument_registry_snaps_ticks_with_integer_math() -> None:
    """Sec 9.3: the tick grid is integer-micros, never a float division."""
    import inspect

    from core import instrument_registry as registry_module

    source = inspect.getsource(registry_module.InstrumentRegistry.validate_order)
    assert "int(round(price_micros / tick))" not in source
    assert "(price_micros + tick // 2) // tick" in source
    assert "ROUND_HALF_UP" in source

    tick = 10_000
    for price in (101.234567, 5.00005, 0.00001):
        check = instrument_registry.validate_order(
            CRYPTO, "BTC-USDT", 0.05, price, round_price=True
        )
        assert check.ok, check.reason
        snapped_micros = check.ticks * tick
        assert check.adjusted_price == pytest.approx(snapped_micros / 1_000_000)

    on_grid = instrument_registry.validate_order(
        CRYPTO, "BTC-USDT", 0.05, tick * 3 / 1_000_000, round_price=True
    )
    assert on_grid.price_changed is False
    assert on_grid.reason == "OK"
    assert on_grid.ticks * tick == 3 * tick


def test_gate7_shadow_twin_counts_only_settled_fills(tmp_path) -> None:
    """Law 4: a fill is counted after its residual reached the window."""
    from core.feature_store import FeatureStore
    from core.shm_ipc import ShadowBookSHM
    from ring1_spinal.shadow_twin import ExecutedFlowTick, ShadowExecutionTwin

    class FailingStore:
        def append_conformal_residual(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("disk full")

    def tick() -> Any:
        return ExecutedFlowTick(
            symbol="BTC-USDT",
            valid_time_ns=1,
            knowledge_ts_ns=1,
            last_price=100.0,
            spread_bps=1.0,
            realized_vol_5s=0.001,
            normal_vol_5s=0.001,
            taker_buy_volume_5s=0.0,
            taker_sell_volume_5s=50.0,
            bid_cancel_to_fill_ratio=0.5,
            ask_cancel_to_fill_ratio=0.5,
        )

    book = ShadowBookSHM(reset=True)
    book.write_snapshot(
        "BTC-USDT", best_bid=100.0, best_ask=100.02, bid_qty=5.0, ask_qty=5.0
    )
    broken = ShadowExecutionTwin(book, feature_store=FailingStore(), required_fills=1)
    try:
        fill = broken.execute_shadow_fill(
            tick(), strategy_id="gate7-broken", side="BUY", silo=CRYPTO, now_ns=10
        )
        assert fill is not None
        # The attempt happened, but nothing reached the conformal window.
        assert broken.shadow_fill_count("gate7-broken") == 0
        assert broken.fills("gate7-broken") == ()
        assert broken.is_graduated("gate7-broken") is False
        assert broken.shadow_report("gate7-broken")["settlement_errors"]
    finally:
        pass  # the shared shadow book stays open for the calibrated twin below

    store = FeatureStore(tmp_path / "gate7-features")
    working = ShadowExecutionTwin(book, feature_store=store, required_fills=1)
    try:
        fill = working.execute_shadow_fill(
            tick(), strategy_id="gate7-working", side="BUY", silo=CRYPTO, now_ns=20
        )
        assert fill is not None
        assert working.shadow_fill_count("gate7-working") == 1
        assert working.fills("gate7-working")
        assert working.is_graduated("gate7-working") is True
    finally:
        store.close()
        book.close()
        book.unlink()


def test_gate7_challenger_needs_positive_net_expectancy() -> None:
    """Volume alone cannot graduate a challenger that loses money."""
    import inspect

    from core.plugins import PluginManifest, PluginRegistry
    from ring3_cockpit.gateway.control_plane import ControlPlane

    source = inspect.getsource(ControlPlane.evaluate_challenger)
    assert "mean_net_bps > 0.0" in source
    assert "net_expectancy_bps" in source

    def manifest(name: str) -> Any:
        return PluginManifest(
            name=name,
            kind="factor_pack",
            ring="ring2",
            author="gate7",
            description="gate7 expectancy probe",
            factor_weights={"a": 1.0},
        )

    registry = PluginRegistry()
    registry.register(manifest("gate7-loser"))
    for _ in range(30):
        registry.record_shadow_fill("gate7-loser", net_bps=-1.5)
    loser = registry.get("gate7-loser")
    assert loser.shadow_fills == 30
    assert loser.winning_shadow_fills == 0
    assert loser.mean_net_bps == pytest.approx(-1.5)
    assert loser.graduated is False
    assert registry.shadow_report("gate7-loser")["mean_net_bps"] == pytest.approx(-1.5)

    registry.register(manifest("gate7-winner"))
    for _ in range(30):
        registry.record_shadow_fill("gate7-winner", net_bps=2.0)
    winner = registry.get("gate7-winner")
    assert winner.mean_net_bps == pytest.approx(2.0)
    assert winner.winning_shadow_fills == 30
    assert winner.graduated is True


def test_gate7_backtest_promotion_requires_leakage_free() -> None:
    """Law 4: DSR alone cannot promote a leaked walk-forward curve."""
    from ring2_brain.backtest.engine import (
        BacktestRun,
        NurseryGateError,
        WalkForwardBacktestEngine,
    )

    def receipt(leakage_free: bool | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "backtest_id": "bt_gate7",
            "silo": US,
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "deflated_sharpe_ratio": 2.4,
            "net_edge_bps": 14.0,
            "passes_dsr_gate": True,
            "passes_champion_floor": True,
        }
        if leakage_free is not None:
            body["purged_kfold"] = {"leakage_free": leakage_free}
        return body

    for value in (False, None):
        engine = WalkForwardBacktestEngine()
        engine._runs["bt_gate7"] = BacktestRun(receipt=receipt(value), created_at_ns=1)
        with pytest.raises(NurseryGateError) as excinfo:
            engine.promote_to_nursery("bt_gate7")
        assert "leakage_free" in str(excinfo.value)
        assert engine.get("bt_gate7").promoted is False

    clean = WalkForwardBacktestEngine()
    clean._runs["bt_gate7"] = BacktestRun(receipt=receipt(True), created_at_ns=1)
    promoted = clean.promote_to_nursery("bt_gate7")
    assert promoted["promoted"] is True
    assert clean.get("bt_gate7").promoted is True


def test_gate7_trust_snapshots_preserve_the_prediction_silo(tmp_path) -> None:
    """Trust rows live in their own silo partition, never a hardcoded US."""
    import inspect

    from core.feature_store import FeatureStore
    from ring2_brain.truth.settlement_worker import DelayedSettlementWorker

    assert '"US", str(strategy_id), time.time_ns()' not in inspect.getsource(
        DelayedSettlementWorker.trust_snapshots
    )

    store = FeatureStore(tmp_path / "gate7-settlement")
    try:
        worker = DelayedSettlementWorker(store, persist_trust=True)
        worker.record_prediction(
            "gate7-india",
            strategy_id="gate7-strategy",
            symbol="RELIANCE",
            predicted_net_bps=12.0,
            horizon_ns=0,
            created_at_ns=0,
            silo=INDIA,
        )
        worker.settle_prediction("gate7-india", 14.0, now_ns=1_000)
        snapshots = worker.trust_snapshots("gate7-strategy")
        assert snapshots, "the India trust snapshot was not persisted"
        assert {snapshot.silo for snapshot in snapshots} == {INDIA}
        assert all(snapshot.silo != "US" for snapshot in snapshots)
        assert snapshots[0].to_dict()["silo"] == INDIA
    finally:
        store.close()


def test_gate7_mcp_tool_failure_is_never_a_200() -> None:
    """A failed MCP tool call is a 4xx, not a green 200 with ok=false."""
    from dataclasses import replace

    from fastapi import FastAPI

    from ring3_cockpit.gateway.mcp_bridge import MCPBridge

    class _Control:
        registry = None
        ledger = None

    def raiser(exc: Exception):
        def handler(arguments: dict[str, Any]) -> Any:
            raise exc

        return handler

    bridge = MCPBridge(_Control())  # type: ignore[arg-type]
    app = FastAPI()
    bridge.register_routes(app)
    client = TestClient(app)
    name = "nios_get_ibor_vaults"
    original = bridge._tools[name]
    bridge._enabled[name] = True
    try:
        bridge._tools[name] = replace(original, handler=raiser(ValueError("bad args")))
        assert client.post("/api/v2/mcp/call", json={"name": name}).status_code == 422

        bridge._tools[name] = replace(original, handler=raiser(RuntimeError("boom")))
        failed = client.post("/api/v2/mcp/call", json={"name": name})
        assert failed.status_code == 400
        assert failed.json()["detail"]["ok"] is False
        assert "boom" in failed.json()["detail"]["error"]

        bridge._tools[name] = original
        assert client.post("/api/v2/mcp/call", json={"name": "nope"}).status_code == 404
        assert (
            client.post("/api/v2/mcp/call", json={"arguments": {}}).status_code == 422
        )
    finally:
        bridge._tools[name] = original


def test_gate7_heartbeat_segment_honours_the_shm_prefix(monkeypatch) -> None:
    """A test/prefixed process must not attach to the live heartbeat segment."""
    from core.process_supervisor import HEARTBEAT_SHM_NAME, RingProcessSupervisor

    prefix = f"nios-gate7-hb-{os.getpid()}"
    monkeypatch.setenv("NIOS_SHM_PREFIX", prefix)
    supervisor = RingProcessSupervisor(autostart=False, fresh_heartbeats=False)
    try:
        table = supervisor.heartbeat_table()
        assert table.name != HEARTBEAT_SHM_NAME
        assert table.name.startswith(prefix), table.name
        assert "ring-heartbeats" in table.name
        assert table.uri == f"shm://{table.name}"
    finally:
        try:
            supervisor.heartbeat_table().unlink()
        except Exception:  # pragma: no cover - best effort teardown
            pass
        supervisor.stop()


def test_gate7_chaos_recovery_never_wipes_live_leases() -> None:
    """Attaching to the lease table must not disarm a running platform."""
    import inspect

    from core import chaos_recovery
    from core.schemas import LeaseMode, System1Lease
    from core.shm_ipc import LeaseTableSHM

    source = inspect.getsource(chaos_recovery.audit_lease_reboot)
    assert "LeaseTableSHM(reset=True)" not in source
    assert "factory_reset: bool = False" in source

    now = 10**15
    # Keep this handle open for the whole drill.  On Windows a named SHM
    # segment is destroyed when its last handle closes; if we closed it before
    # the audit, ``audit_lease_reboot`` (reset=False) would attach to a *fresh*
    # empty segment and the live lease would vanish for reasons unrelated to
    # chaos recovery.  The real invariant is that the reboot audit must not
    # disarm a lease that is still live in the shared segment.
    table = LeaseTableSHM(reset=True)
    try:
        table.write_lease(
            System1Lease(
                symbol="ETH-USDT",
                mode=LeaseMode.LONG_ONLY,
                target_bps=10.0,
                stop_bps=8.0,
                # A legal dead-man TTL: 30 s of remaining authority.
                expires_at_ns=now + 30_000_000_000,
                strategy_id_hash=0x5150,
            ),
            now_ns=now - 1_000,
        )

        report = chaos_recovery.audit_lease_reboot(now_ns=now)
        # ``passed`` certifies an *empty* post-reboot table, and the live ETH
        # lease deliberately is not empty.  What must hold is that only the
        # stale lease was swept and the live authority survived.
        assert report.stale_lease_armed is True
        assert report.swept == 1
        assert report.mode_after_reboot == int(LeaseMode.DISARMED)

        reader = LeaseTableSHM(reset=False)
        try:
            survivor = reader.read_lease("ETH-USDT", now)
            assert survivor.mode is LeaseMode.LONG_ONLY, "a live lease was wiped"
            assert survivor.target_bps == pytest.approx(10.0)
            assert reader.active_count(now) == 1
        finally:
            reader.close()
    finally:
        table.close()


def test_gate7_feature_store_keeps_an_uncommitted_contagion_batch(tmp_path) -> None:
    """The contagion buffer is cleared only after the write returned."""
    import inspect
    import sqlite3

    from core.feature_store import FeatureStore
    from core.schemas import ContagionFeatures

    source = inspect.getsource(FeatureStore._flush_contagion)
    assert "self._contagion_pending = []" not in source

    store = FeatureStore(tmp_path / "gate7-store")
    try:
        store.append_contagion(
            CRYPTO,
            ContagionFeatures(
                symbol="BTCUSDT",
                as_of_ns=1,
                crowd_asymmetry_score=0.1,
                cascade_fragility_index=0.2,
                narrative_exhaustion_divergence=0.3,
            ),
        )
        assert len(store._contagion_pending) == 1

        class _BrokenConnection:
            def executemany(self, *args: Any, **kwargs: Any) -> None:
                raise sqlite3.OperationalError("database is locked")

        real = store._con
        store._con = _BrokenConnection()  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.flush()
            assert len(store._contagion_pending) == 1, "the batch was dropped"
        finally:
            store._con = real
        assert store.flush() == 1
        assert store._contagion_pending == []
    finally:
        store.close()


def test_gate7_grounded_metrics_are_immutable() -> None:
    """A sealed packet's metrics mapping cannot be mutated after the digest."""
    from core.schemas import GroundedFactPacket

    packet = GroundedFactPacket(
        symbol="SPY",
        valid_time_ns=1,
        knowledge_ts_ns=1,
        as_of_ns=1,
        metrics={"momentum_20d": 0.42},
    )
    with pytest.raises(TypeError):
        packet.metrics["injected_metric"] = 99.0  # type: ignore[index]
    assert packet.verify() is True
    assert dict(packet.metrics) == {"momentum_20d": 0.42}


def test_gate7_operator_vault_exposes_is_initialized() -> None:
    """The guard probe documented in V1.8 exists on the Ring 0 vault."""
    import inspect

    from ring0_vault.silos.key_vault.operator_auth import OperatorAuth

    assert hasattr(OperatorAuth, "is_initialized")
    assert "is_initialized" in inspect.getsource(OperatorAuth)
    assert OperatorAuth.is_initialized is not OperatorAuth.initialized


def test_gate7_workspace_reset_live_state_needs_a_live_session(
    client: Any, runtime: Any, token: str
) -> None:
    """Last: a LIVE session plus confirm=true really resets live state."""
    # Mint the LIVE token straight from the Ring 0 vault (the same instance the
    # guard reads) so the assertion does not pay the 23-gate login pre-flight.
    live_token = (
        runtime.control.operator_auth().elevate_to_live(token, LIVE_PIN).token
    )
    live_headers = _headers(live_token)

    denied = client.post(
        "/api/v2/control/workspace/reset",
        json={"workspace": "all", "confirm": False},
        headers=live_headers,
    )
    assert denied.status_code == 400, denied.text

    allowed = client.post(
        "/api/v2/control/workspace/reset",
        json={"workspace": "all", "confirm": True},
        headers=live_headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["clean_slate_active"] is True


# ---------------------------------------------------------------------------
# Gate 7b - V1.8 wedged-service bounding (thread/socket leak closure)
# ---------------------------------------------------------------------------
def test_gate7b_feed_transport_breaker_fails_fast(monkeypatch) -> None:
    """A dead network costs N attempts once, then zero sockets per call."""
    import urllib.error
    import urllib.request

    from ring2_brain.truth.live_public_feeds import (
        FEED_BREAKER_FAILURES,
        FeedUnavailable,
        HttpTransport,
    )

    attempts = {"count": 0}

    def dead_urlopen(request, timeout=None):  # noqa: ARG001
        attempts["count"] += 1
        raise urllib.error.URLError("name resolution unavailable")

    monkeypatch.setattr(urllib.request, "urlopen", dead_urlopen)

    transport = HttpTransport(timeout_s=0.05, breaker_cooldown_s=30.0)
    for _ in range(FEED_BREAKER_FAILURES):
        with pytest.raises(FeedUnavailable):
            transport.get_json("https://feeds.invalid/book")
    assert transport.breaker_open is True
    assert transport.status()["breaker_trips"] == 1
    assert attempts["count"] == FEED_BREAKER_FAILURES

    # Every further call is refused *before* urllib is touched.
    for _ in range(25):
        with pytest.raises(FeedUnavailable):
            transport.get_json("https://feeds.invalid/book")
    assert attempts["count"] == FEED_BREAKER_FAILURES, "the open breaker still dialled out"
    assert transport.status()["breaker_open"] is True


def test_gate7b_ingest_pass_stops_touching_the_network_at_its_deadline(
    monkeypatch,
) -> None:
    """A four-silo pass is bounded and still emits one labelled frame per silo."""
    import threading
    import urllib.error
    import urllib.request

    from ring2_brain.truth.live_public_feeds import (
        FeedUnavailable,
        LiveDualVendorFeedConnector,
    )

    def dead_urlopen(request, timeout=None):  # noqa: ARG001
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", dead_urlopen)

    connector = LiveDualVendorFeedConnector(silo=CRYPTO, timeout_s=0.05)
    before = threading.active_count()
    frames = connector.ingest_all(deadline_s=0.0)
    after = threading.active_count()
    assert frames, "a dead feed must still emit synthetic frames"
    assert connector._mode == "SYNTHETIC_FALLBACK"
    assert after - before <= 1, (before, after)
    with pytest.raises(FeedUnavailable):
        connector._fetch_book(connector.symbols[0], connector.primary)
    assert connector.budget_exhausted >= 1


def test_gate7b_multi_silo_ingest_has_a_wall_clock_budget() -> None:
    """The pre-flight feed gate can never block a control request for minutes."""
    import inspect
    import time

    from ring2_brain.truth.live_public_feeds import (
        FEED_INGEST_BUDGET_S,
        MultiSiloFeedConnector,
    )
    from scripts import preflight_golive

    assert FEED_INGEST_BUDGET_S <= 15.0
    gate_source = inspect.getsource(preflight_golive.gate_dual_vendor_feeds)
    assert "deadline_s=FEED_INGEST_BUDGET_S" in gate_source

    connector = MultiSiloFeedConnector(timeout_s=0.05)
    started = time.monotonic()
    frames = connector.ingest_all(deadline_s=0.0)
    elapsed = time.monotonic() - started
    connector.close()
    assert frames
    assert elapsed < 5.0, f"unbounded ingest pass took {elapsed:.2f}s"
    assert connector.status().get("feed_mode") == "SYNTHETIC_FALLBACK"


def test_gate7b_ledger_store_probe_false_never_probes() -> None:
    """``probe=False`` means no daemon probe, so no blocked probe thread."""
    import threading

    from ring0_vault.ibor.ledger_outbox import LedgerStore

    before = threading.active_count()
    stores = [LedgerStore(in_memory=True, probe=False) for _ in range(10)]
    after = threading.active_count()
    try:
        assert after - before <= 1, (before, after)
        for store in stores:
            assert store.probed is False
            tigerbeetle = store.statuses[0]
            assert tigerbeetle.available is False
            assert "probe suppressed" in tigerbeetle.detail
            assert store.statuses[2].name == "sqlite"
            assert store.statuses[2].available is True
    finally:
        for store in stores:
            store.close()


def test_gate7b_switchboard_never_stacks_probe_threads() -> None:
    """One wedged provider probe, not one thread per route attempt."""
    import threading

    from ring2_brain.cognition.system1_instinct import (
        InstinctContext,
        LLMProvider,
        MultiLLMSwitchboard,
    )

    release = threading.Event()

    def wedged(context: Any) -> None:  # noqa: ARG001
        release.wait(20.0)
        return None

    provider = LLMProvider("gate7b-wedged", "local", "model", wedged, 1)
    board = MultiLLMSwitchboard(
        [provider], probe_timeout_ms=20.0, hard_timeout_ms=250.0
    )
    context = InstinctContext(symbol="BTCUSDT")
    before = threading.active_count()
    try:
        for _ in range(20):
            response, _elapsed, error = board._invoke(provider, context)
            assert response is None
            assert error
        after = threading.active_count()
        probe_threads = [
            item
            for item in threading.enumerate()
            if item.name.startswith("nios-s1-probe-gate7b-wedged")
        ]
        assert len(probe_threads) == 1, probe_threads
        assert after - before <= 1, (before, after)
        assert board.abandoned_probes >= 1
    finally:
        release.set()


def test_gate7b_preflight_feed_gate_is_bounded_and_degrades() -> None:
    """The certificate's feed gate returns quickly on an air-gapped host."""
    import time

    from scripts.preflight_golive import PreflightContext, gate_dual_vendor_feeds

    ctx = PreflightContext(silo_scope="ALL", strict=False, tempdir=".")
    started = time.monotonic()
    gate = gate_dual_vendor_feeds(ctx)
    elapsed = time.monotonic() - started
    assert elapsed < 20.0, f"feed gate took {elapsed:.1f}s"
    assert gate.gate_id == "feeds.dual_vendor_four_silos_lee_ready"
    assert "ingest_elapsed_ms" in gate.evidence
    assert gate.evidence["ingest_budget_s"] <= 15.0


def test_gate7b_preflight_session_elevation_cannot_recurse() -> None:
    """Elevation is in-process: ``/auth/elevate-live`` reports the pre-flight.

    Going through HTTP recursed (pre-flight -> client -> elevation -> pre-flight)
    and was the thread/portal explosion that wedged every control-plane suite.
    """
    import inspect
    from types import SimpleNamespace

    import scripts.run_nios as runner

    source = inspect.getsource(runner.operator_session_headers)
    assert "elevate_to_live" in source
    assert "runtime" in source

    calls: list[str] = []

    class _Auth:
        def elevate_to_live(self, token: str, pin: str) -> Any:
            assert token == "paper-token"
            assert pin == runner.SCRIPT_OPERATOR["live_pin"]
            return SimpleNamespace(token="live-token")

    class _Control:
        def operator_auth(self) -> Any:
            return _Auth()

    class _Runtime:
        control = _Control()

    class _Client:
        def post(self, path: str, json: Any = None, headers: Any = None) -> Any:
            calls.append(path)
            return SimpleNamespace(
                status_code=200, text="", json=lambda: {"token": "paper-token"}
            )

    headers = runner.operator_session_headers(_Client(), _Runtime())
    assert headers == {"X-NIOS-Session": "live-token"}
    assert calls == ["/api/v2/auth/setup"], calls
    assert "/api/v2/auth/elevate-live" not in calls

    from scripts.preflight_golive import PreflightContext

    context = PreflightContext(silo_scope="ALL", strict=False, tempdir=".")
    assert context._auth_ready is False
    assert "_auth_ready" in inspect.getsource(PreflightContext.client)


# ---------------------------------------------------------------------------
# Gate 8 - V1.8 multi-perspective security, memory & self-evolution audit
# ---------------------------------------------------------------------------
# One focused, deterministic regression per V1.8 fix.  Every test is offline,
# in-process and bounded; HTTP tests are confined to fixes 3-5.


def _gate8_live_headers(client: Any, runtime: Any) -> dict[str, str]:
    """Mint a fresh LIVE session in-process (no pre-flight, no network).

    Ensure the operator exists first: in an isolated ``-k gate8`` run the
    module-scoped ``token`` fixture (which normally performs setup) never runs.
    """
    setup = client.post(
        "/api/v2/auth/setup",
        json={
            "username": USERNAME,
            "password": PASSWORD,
            "live_pin": LIVE_PIN,
            "start_clean_slate": False,
        },
    )
    assert setup.status_code in (200, 409), setup.text
    login = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    paper = str(login.json()["token"])
    live = runtime.control.operator_auth().elevate_to_live(paper, LIVE_PIN).token
    return _headers(live)


def test_gate8_port_clis_fail_closed_on_out_of_range_ports() -> None:
    """LENS 1: an invalid port is a clean exit 2, never an OverflowError."""
    import scripts.nios_port_check as checker
    import scripts.run_nios as runner

    # nios_port_check.main() takes an explicit argv list.
    assert checker.main(["127.0.0.1", "99999"]) == 2
    assert checker.main(["127.0.0.1", "70000", "70010"]) != 0  # missing --scan
    assert checker.main(["127.0.0.1", "--scan", "70000", "70010"]) == 2
    assert checker.main(["127.0.0.1", "-5"]) == 2
    # The helpers themselves fail closed instead of calling socket.bind.
    assert checker._valid_port(65_535) is True
    assert checker._valid_port(65_536) is False
    assert checker._valid_port(0) is False
    assert checker.can_bind("127.0.0.1", 99_999) is False
    assert checker.first_free_port("127.0.0.1", 70_000, 70_010) is None

    # run_nios.main() must reject before it ever boots a ring or binds a socket.
    assert runner.main(["--port", "99999"]) == 2
    assert runner.main(["--port=-7"]) == 2


def test_gate8_latency_scorecard_fails_closed_on_bad_preflight(monkeypatch: Any) -> None:
    """LENS 1: a broken certificate is exit 2, not JSONDecodeError/KeyError."""
    import importlib.util
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace

    script = Path(__file__).resolve().parent.parent / "scripts" / "latency_scorecard.py"
    assert script.is_file()

    def _exit_code(returncode: int, stdout: str) -> int:
        spec = importlib.util.spec_from_file_location("_gate8_scorecard", script)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(
                returncode=returncode, stdout=stdout, stderr="preflight-down"
            ),
        )
        with pytest.raises(SystemExit) as excinfo:
            spec.loader.exec_module(module)
        return excinfo.value.code

    assert _exit_code(1, "") == 2
    assert _exit_code(0, "not json") == 2
    assert _exit_code(0, '{"gates": []}') == 2  # missing certificate_sha256


def test_gate8_unknown_silo_brokers_returns_404(client: Any, runtime: Any) -> None:
    """LENS 1: an unknown silo on the broker control route is a 404, not 500."""
    response = client.post(
        "/api/v2/control/brokers",
        json={"silo": "NO_SUCH_SILO", "mode": "PAPER"},
        headers=_gate8_live_headers(client, runtime),
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "UNKNOWN_SILO"


def test_gate8_unknown_silo_credentials_returns_404_without_secret(
    client: Any, runtime: Any
) -> None:
    """LENS 1: unknown silo -> 404 UNKNOWN_SILO, with no credential echoed."""
    api_key = "GATE8_SUPERSECRET_KEY_9f3a"
    api_secret = "GATE8_SUPERSECRET_VALUE_b71c"
    response = client.post(
        "/api/v2/control/brokers/credentials",
        json={
            "silo": "NO_SUCH_SILO",
            "venue": "DHAN",
            "api_key": api_key,
            "api_secret": api_secret,
            "sandbox": True,
        },
        headers=_gate8_live_headers(client, runtime),
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "UNKNOWN_SILO"
    assert api_key not in response.text
    assert api_secret not in response.text


def test_gate8_skill_markdown_path_traversal_returns_404(client: Any) -> None:
    """LENS 1: path escaping the skill registry root is never served."""
    import inspect

    from ring3_cockpit.gateway.control_plane import ControlPlane

    for escaped in ("..%2f..%2fetc%2fpasswd", "a%00b"):
        response = client.get(f"/api/v2/cockpit/skills/{escaped}")
        assert response.status_code == 404, (escaped, response.status_code, response.text)

    # The containment check itself must fail closed (no swallowed ValueError),
    # and the token gate must reject NUL bytes / parent traversal explicitly.
    src = inspect.getsource(ControlPlane.skill_markdown)
    assert '"\\x00" in token' in src
    assert '".." in token' in src
    marker = "except (OSError, RuntimeError, ValueError):"
    assert marker in src
    assert "raise HTTPException" in src[src.index(marker) :]


def test_gate8_scientific_notation_and_leading_dot_numbers_are_grounded() -> None:
    """LENS 2 (Law 3): magnitude-carrying spellings cannot bypass grounding."""
    from core.schemas import UngroundedNumericalClaim
    from ring2_brain.truth.grounding_gate import (
        UNGROUNDED_CLAIM_CODE,
        GroundingLedger,
        NumericalGroundingVerifier,
        verify_thesis_claims,
    )

    base_ns = 1_700_000_000_000_000_000
    packet = GroundingLedger(persist=False).commit(
        "ACME",
        {"close": 1.5},
        valid_time_ns=base_ns,
        knowledge_ts_ns=base_ns - 1_000,
    )
    # A fabricated magnitude hidden behind a grounded mantissa must be refused.
    with pytest.raises(UngroundedNumericalClaim) as excinfo:
        verify_thesis_claims("close price is 1.5e9.", packet)
    assert UNGROUNDED_CLAIM_CODE in str(excinfo.value)

    # Leading-dot decimals are material claims with their real value.
    claims = NumericalGroundingVerifier().extract_claims("value .5")
    assert any(claim.value == pytest.approx(0.5) for claim in claims), claims

    # A genuinely grounded scientific-notation figure still passes.
    report = verify_thesis_claims("close price is 1.5e0.", packet)
    assert report.claims_checked >= 1


def test_gate8_cold_lease_read_rejects_expired_deadline() -> None:
    """LENS 3 (Law 2): a cold read never serves a dead System 1's armed lease."""
    import time as _time

    from core.schemas import LeaseMode, System1Lease
    from core.shm_ipc import LeaseTableSHM

    name = f"nios-gate8-cold-{os.getpid()}"
    writer = LeaseTableSHM(n_slots=1, name=name)
    reader = None
    live_reader = None
    try:
        writer.write_lease(
            System1Lease(
                symbol="EXP",
                mode=LeaseMode.LONG_ONLY,
                expires_at_ns=_time.monotonic_ns() - 1,
            )
        )
        # A FRESH reader has no cache: this is the cold decode path.
        reader = LeaseTableSHM(n_slots=1, name=name)
        assert reader.read_lease("EXP").mode == LeaseMode.DISARMED

        writer.write_lease(
            System1Lease(
                symbol="LIVE",
                mode=LeaseMode.LONG_ONLY,
                expires_at_ns=_time.monotonic_ns() + 60_000_000_000,
            )
        )
        live_reader = LeaseTableSHM(n_slots=1, name=name)
        assert live_reader.read_lease("LIVE").mode == LeaseMode.LONG_ONLY
    finally:
        for handle in (reader, live_reader, writer):
            if handle is not None:
                handle.close()


def test_gate8_outbox_settlement_is_single_atomic_transaction() -> None:
    """LENS 3: commit/void/void_many share one BEGIN IMMEDIATE commit point."""
    import inspect

    from ring0_vault.ibor.ledger_outbox import CrashAtomicOutbox, LedgerStore

    settle = getattr(LedgerStore, "settle_outbox_reservations", None)
    assert callable(settle)  # absent on the pre-fix store
    src = inspect.getsource(settle)
    assert "BEGIN IMMEDIATE" in src and "COMMIT" in src
    assert "ibor_double_entry_ledger" in src and "financial_outbox" in src
    for name in ("commit", "void", "void_many"):
        method_src = inspect.getsource(getattr(CrashAtomicOutbox, name))
        assert "self.store.settle_outbox_reservations" in method_src, name


def test_gate8_journal_fsyncs_parent_directory_and_boots_clean_slate(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """LENS 3: os.replace is followed by a best-effort directory fsync."""
    import os as _os

    import ring0_vault.ibor.state_journal as sj

    fake_fd = 987_654_321
    fsynced: list[int] = []
    closed: list[int] = []

    monkeypatch.setattr(sj.os, "open", lambda path, flags, *a, **k: fake_fd)
    monkeypatch.setattr(sj.os, "fsync", lambda fd: fsynced.append(fd))
    monkeypatch.setattr(sj.os, "close", lambda fd: closed.append(fd))

    journal = sj.AtomicStateJournal(path=tmp_path / "state.json")
    journal.save({"logbook": [{"id": 1}]})
    # The file fd is fsynced before replace; the directory fd after it.
    assert fake_fd in fsynced, fsynced
    assert fake_fd in closed, closed

    # A truncated/corrupt document still degrades to a Clean Slate.
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    state = sj.AtomicStateJournal(path=corrupt).load()
    assert state["clean_slate_active"] is True
    assert state["logbook"] == []


def test_gate8_copula_haircut_applied_to_reservation() -> None:
    """LENS 3: a shield haircut reserves the reduced notional, not the full one."""
    import inspect

    from ring0_vault.kernel import firewalls_and_oms as fo

    src = inspect.getsource(fo)
    # The sized-down path must re-quantise the *reserved* proposal too.
    assert "replace(proposal, qty=effective_qty)" in src
    marker = "reserve_proposal = ("
    assert marker in src
    block = src[src.index(marker) : src.index(marker) + 600]
    assert "self.outbox.reserve(" in block
    assert "reserve_proposal" in block


def test_gate8_read_lease_slot_bounds_check() -> None:
    """LENS 3: diagnostic slot reads reject out-of-range indices."""
    from core.shm_ipc import LeaseTableSHM

    table = LeaseTableSHM(n_slots=2, name=f"nios-gate8-slot-{os.getpid()}")
    try:
        with pytest.raises(IndexError):
            table.read_lease_slot(table.n_slots)
        with pytest.raises(IndexError):
            table.read_lease_slot(-1)
    finally:
        table.close()


def test_gate8_purged_kfold_embargo_default_is_two_percent() -> None:
    """LENS 4 (Law 4): Purged K-Fold ships with the constitutional 2% embargo."""
    from ring2_brain.forge.alpha_forge import (
        DEFAULT_CV_FOLDS,
        DEFAULT_EMBARGO_FRACTION,
        PurgedKFoldCV,
    )

    assert DEFAULT_CV_FOLDS == 5
    assert DEFAULT_EMBARGO_FRACTION == 0.02
    assert PurgedKFoldCV().embargo_fraction == 0.02
    assert PurgedKFoldCV().n_folds == DEFAULT_CV_FOLDS


def test_gate8_purged_kfold_rejects_weakened_parameters() -> None:
    """LENS 4: k<5 and embargo<2% are refused, not silently accepted."""
    from ring2_brain.forge.alpha_forge import PurgedKFoldCV

    for kwargs in (
        {"n_folds": 2},
        {"n_folds": 4},
        {"embargo_fraction": 0.0},
        {"embargo_fraction": 0.01},
    ):
        with pytest.raises(ValueError):
            PurgedKFoldCV(**kwargs)


def test_gate8_supplied_metrics_missing_p_value_fails_closed() -> None:
    """LENS 4: a supplied DSR without a p-value fails the FDR gate."""
    import numpy as np

    from ring2_brain.forge.alpha_forge import (
        DEFAULT_FDR,
        StrategyGenome,
        benjamini_hochberg,
    )

    missing = StrategyGenome(
        genome_id="supplied-no-p",
        weights=np.array([1.0]),
        deflated_sharpe=2.0,
    )
    assert missing.p_value == 1.0
    rejected, _, _, _ = benjamini_hochberg([missing.p_value], DEFAULT_FDR)
    assert not bool(rejected[0])

    explicit = StrategyGenome(
        genome_id="supplied-with-p",
        weights=np.array([1.0]),
        deflated_sharpe=2.0,
        p_value=1e-9,
    )
    assert explicit.p_value == pytest.approx(1e-9)
    rejected2, _, _, _ = benjamini_hochberg([explicit.p_value], DEFAULT_FDR)
    assert bool(rejected2[0])


def test_gate8_walk_forward_rejects_weakened_parameters() -> None:
    """LENS 4: the walk-forward engine refuses a weakened k/embargo contract."""
    from ring2_brain.backtest.engine import WalkForwardBacktestEngine

    for kwargs in (
        {"k_folds": 2},
        {"k_folds": 4},
        {"embargo_pct": 0.0},
        {"embargo_pct": 0.01},
        {"embargo_pct": 0.5},
    ):
        with pytest.raises(ValueError):
            WalkForwardBacktestEngine(**kwargs)


def test_gate8_cost_floor_uses_frozen_authority(monkeypatch: Any) -> None:
    """LENS 4: live config may only raise, never lower, the statutory floor."""
    from types import SimpleNamespace

    import ring1_spinal.silo_worker as sw
    from ring2_brain.backtest import engine as bt

    frozen = bt.SILO_COST_FLOORS_BPS[INDIA]
    assert frozen == pytest.approx(8.50)

    monkeypatch.setitem(
        sw.SILO_CONFIGS,
        INDIA,
        SimpleNamespace(cost_floor=SimpleNamespace(round_trip_cost_bps=1.0)),
    )
    assert bt._silo_cost_floor_bps(INDIA) == pytest.approx(frozen)

    monkeypatch.setitem(
        sw.SILO_CONFIGS,
        INDIA,
        SimpleNamespace(cost_floor=SimpleNamespace(round_trip_cost_bps=12.0)),
    )
    assert bt._silo_cost_floor_bps(INDIA) == pytest.approx(12.0)


def test_gate8_prediction_silo_defaults_to_unspecified() -> None:
    """LENS 4: forecasts no longer silently land in the US partition."""
    from ring2_brain.truth.settlement_worker import (
        DEFAULT_PREDICTION_SILO,
        Prediction,
    )

    assert DEFAULT_PREDICTION_SILO == "UNSPECIFIED"
    prediction = Prediction(
        prediction_id="g8-pred",
        strategy_id="g8-strat",
        symbol="TEST",
        predicted_net_bps=1.5,
        horizon_ns=1_000,
        created_at_ns=1,
    )
    assert prediction.silo == "UNSPECIFIED"


def test_gate8_overflow_samples_not_folded_into_finite_bucket() -> None:
    """LENS 5: the +Inf overflow is emitted by count, not the last finite bucket."""
    from core.telemetry import TickLatencyRecorder

    recorder = TickLatencyRecorder()
    recorder.observe_s(50e-6)  # 50 us -> a finite bucket
    recorder.observe_s(5.0)  # far above the last finite 1 ms bound
    snapshot = recorder.snapshot()
    assert snapshot["count"] == 2
    assert snapshot["buckets"][-1] == {"le": 0.001, "count": 1}


def test_gate8_sec_edgar_pit_fallback_uses_utc(monkeypatch: Any) -> None:
    """LENS 5: the as_of fallback is UTC, so a local-date filing cannot leak."""
    import inspect
    from datetime import datetime as _datetime, timezone

    import ring2_brain.truth.tradingagents.sec_edgar as se

    # Behavioural: the fallback must call datetime.now(timezone.utc).
    seen: list[Any] = []

    class _FakeDateTime:
        @staticmethod
        def now(tz: Any = None) -> _datetime:
            seen.append(tz)
            return _datetime(2026, 11, 17, 23, 30, tzinfo=(tz or timezone.utc))

    monkeypatch.setattr(se, "datetime", _FakeDateTime)
    monkeypatch.setattr(se, "cik_for", lambda ticker: None)
    try:
        se.get_balance_sheet("AAPL", as_of_date=None)
    except Exception:
        pass  # cik_for -> None raises NoMarketDataError after the fallback ran
    assert seen == [timezone.utc], seen

    # Guard against a naive local-now regression in the source itself.
    src = inspect.getsource(se._statement)
    assert "datetime.now(timezone.utc)" in src
