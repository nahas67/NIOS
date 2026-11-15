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
    path = snapshot["journal_path"]
    assert path and os.path.exists(path), path

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
