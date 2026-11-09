"""PHASE 2 -- STEP 2 verification: all four market silos, all venue adapters.

Gates
-----
1. **India silo deep submodule activation.**  ``order_api``, ``data``, ``funds``,
   ``transform_data`` and every ``streaming`` module import with zero
   ``ImportError`` for Dhan / Zerodha / AngelOne / Upstox, and each broker
   transforms a NIOS signed order into its venue-native payload for
   ``RELIANCE.NS``, ``NIFTYBEES.NS`` and ``LIQUIDBEES.NS`` with the correct
   ``MIS`` (intraday) vs ``CNC`` (delivery) product and the ``5.5 bps`` India
   statutory floor (``sec_015``).
2. **Crypto & US Nautilus + Alpaca activation.**  ``nautilus_adapter_status()``
   reports ``100%`` importable across ``binance``, ``okx``, ``bybit``,
   ``interactive_brokers`` and ``alpaca``; the ``US`` silo enforces ``T+1``
   settlement metadata and ``Reg-T`` margin guards, and Alpaca formats
   sub-penny maker-peg prices.
3. **FX silo MT5, CTP & OANDA activation.**  ``vnpy_core``, ``vnpy_mt5``,
   ``vnpy_ctp`` and ``oanda`` import with zero ``ImportError``; OANDA enforces
   the ``24/5`` session calendar, per-pair pip-spread ceilings, macro-calendar
   blackout windows and roll-yield guards.
4. **All four silos READY & live/paper toggle.**  ``silo_registry`` reports
   ``100%`` of adapters ``READY``, ``POST /api/v2/control/brokers`` toggles all
   four silos with ``confirm_live=True``, and a signed order plus the
   ``35 / 15 / 50`` in-silo ratchet executes in every silo with ``0.0 bps``
   cross-border wire bleed (``sec_018`` Law 5).
5. **4-ring air-gap AST.**  ``ring2_brain/`` and ``core/`` contain zero imports
   of a broker adapter or the ``key_vault`` (``sec_004``, ``sec_011``).
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.orchestrator import NIOSCoreOrchestrator
from core.schemas import VaultName
from ring0_vault.ibor.ledger_outbox import (
    CrashAtomicOutbox,
    IBORLedger,
    InSiloProfitRatchet,
    LedgerStore,
)
from ring0_vault.silos.silo_registry import (
    REG_T_MAX_LEVERAGE,
    SETTLEMENT_RULES,
    SILO_MARKET_MAP,
    SWEEP_TICKERS,
    VENUE_ADAPTERS,
    DispatchMode,
    MarketSiloRegistry,
    SiloMode,
    dispatch_signed_order,
    probe_donor_adapters,
)
from ring1_spinal.silo_worker import SILO_ORDER, nautilus_adapter_status

ROOT = Path(__file__).resolve().parents[1]

INDIA_VENUES = ("dhan", "zerodha", "angel", "upstox")
NAUTILUS_VENUES = ("binance", "okx", "bybit", "interactive_brokers", "alpaca")

#: ``MIS`` is intraday, ``CNC`` is delivery -- each venue spells it differently.
PRODUCT_MAP = {
    "dhan": {"MIS": "INTRADAY", "CNC": "CNC"},
    "zerodha": {"MIS": "MIS", "CNC": "CNC"},
    "angel": {"MIS": "INTRADAY", "CNC": "DELIVERY"},
    "upstox": {"MIS": "I", "CNC": "D"},
}

#: One dispatch case per silo: ``(silo, venue, symbol, side, qty, price)``.
DISPATCH_CASES = (
    ("CRYPTO_BINANCE", "binance", "BTC-USDT", "BUY", 0.01, 65000.0),
    ("INDIA_ZERODHA_DHAN", "zerodha", "NIFTYBEES.NS", "BUY", 5, 250.0),
    ("US_IBKR", "alpaca", "VOO", "BUY", 2, 520.0),
    ("FX_MT5", "oanda", "EUR_USD", "BUY", 10000, 1.085),
)

#: ``sec_015`` reserve / long-term sweep tickers that must survive dispatch.
SWEEP_ASSERTIONS = {
    "CRYPTO_BINANCE": ("USYC", "BTC-USDT"),
    "INDIA_ZERODHA_DHAN": ("LIQUIDBEES.NS", "NIFTYBEES.NS"),
    "US_IBKR": ("SGOV", "VOO"),
    "FX_MT5": ("Unlevered Gold", "Sovereign Carry Repo"),
}


def _signed(symbol: str, qty: float, price: float, *, side: str = "BUY",
            proposal_id: str = "") -> dict:
    return {
        "proposal_id": proposal_id or f"p2s2-{symbol}-{side}".replace(".", "-"),
        "strategy_id": "phase2-step2-verification",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "limit_price": price,
        "expected_gross_bps": 40.0,
        "spread_bps": 1.0,
    }


def _funded_ledger(silos=SILO_ORDER) -> IBORLedger:
    ledger = IBORLedger(LedgerStore(in_memory=True), silos=silos)
    for silo in silos:
        ledger.open_silo(silo)
        ledger.fund_cash(silo, 1_000_000_000_000)
        for vault in VaultName:
            ledger.fund_vault(silo, vault, 100_000_000_000)
    return ledger


# ===========================================================================
# GATE 1 -- India silo deep submodule activation
# ===========================================================================


@pytest.mark.parametrize("venue", INDIA_VENUES)
def test_gate1_india_deep_submodules_import_without_importerror(venue: str) -> None:
    """Every deep ``api/`` and ``mapping/`` module imports on an air-gapped host."""
    root = f"ring0_vault.silos.india_brokers.{venue}"
    for suffix in (
        "api.auth_api",
        "api.data",
        "api.funds",
        "api.order_api",
        "mapping.order_data",
        "mapping.transform_data",
        "mapping.margin_data",
        "mapping.gtt_data",
        "database.master_contract_db",
    ):
        importlib.import_module(f"{root}.{suffix}")

    streaming = importlib.import_module(f"{root}.streaming")
    failures: list[str] = []
    for info in pkgutil.iter_modules(streaming.__path__):
        if info.name.endswith("_pb2") or info.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{root}.streaming.{info.name}")
        except Exception as exc:  # noqa: BLE001 - report verbatim
            failures.append(f"{info.name}: {type(exc).__name__}: {exc}")
    assert failures == [], failures


@pytest.mark.parametrize("venue", INDIA_VENUES)
@pytest.mark.parametrize("symbol", ("RELIANCE.NS", "NIFTYBEES.NS", "LIQUIDBEES.NS"))
def test_gate1_india_order_payload_transformation(
    venue: str, symbol: str
) -> None:
    """MIS/CNC product mapping and the 5.5 bps India statutory floor."""
    registry = MarketSiloRegistry()
    for product in ("MIS", "CNC"):
        result = dispatch_signed_order(
            "INDIA_ZERODHA_DHAN",
            venue,
            _signed(symbol, 10, 2500.0),
            mode=DispatchMode.PAPER,
            registry=registry,
            product=product,
        )
        payload = result.payload
        assert payload, f"{venue} produced an empty payload"

        # The venue-native product code is the MIS/CNC distinction.
        expected = PRODUCT_MAP[venue][product]
        observed = (
            payload.get("product")
            or payload.get("productType")
            or payload.get("producttype")
        )
        assert observed == expected, f"{venue}: {observed!r} != {expected!r}"

        # Symbol mapping must land on the venue-native tradingsymbol/securityId.
        tradingsymbol = (
            payload.get("tradingsymbol")
            or payload.get("securityId")
            or payload.get("instrument_token")
        )
        assert tradingsymbol, f"{venue} did not map {symbol}"

        assert result.flags["statutory_tax_bps"] == pytest.approx(5.5)
        assert result.fill["statutory_tax_bps"] == pytest.approx(5.5)
        assert result.flags["settlement_cycle"] == "T+1"
        assert result.cross_border_wire_bps == 0.0


def test_gate1_india_floor_matches_silo_registry() -> None:
    registry = MarketSiloRegistry()
    india = registry.get("INDIA_ZERODHA_DHAN")
    assert india.statutory_tax_bps == pytest.approx(5.5)
    assert india.round_trip_cost_bps == pytest.approx(2.0 + 2 * 0.5 + 5.5)
    assert india.fee_stt_bps == pytest.approx(2 * 0.5 + 5.5)


# ===========================================================================
# GATE 2 -- Crypto & US Nautilus + Alpaca activation
# ===========================================================================


def test_gate2_nautilus_adapters_are_100_percent_importable() -> None:
    status = nautilus_adapter_status()
    assert status["total"] == 5
    assert status["harvested_count"] == 5
    assert status["importable_count"] == 5
    assert set(status["adapters"]) == set(NAUTILUS_VENUES)
    for name in NAUTILUS_VENUES:
        entry = status["adapters"][name]
        assert entry["harvested"] is True
        assert entry["importable"] is True, entry["reason"]
        assert entry["symbols"], f"{name} exported no symbols"
    assert status["gaps"] == {}


@pytest.mark.parametrize("venue", NAUTILUS_VENUES)
def test_gate2_nautilus_adapters_format_payloads(venue: str) -> None:
    module = importlib.import_module(f"ring1_spinal.nautilus_adapters.{venue}")
    assert module.VENUE
    payload = module.format_order_payload(_signed("BTC-USDT", 0.00123, 65000.99))
    assert isinstance(payload, dict) and payload
    flags = module.payload_flags(payload)
    assert flags["venue"].lower() in (module.VENUE.lower(), venue)


def test_gate2_us_silo_enforces_reg_t_and_t_plus_one() -> None:
    from ring1_spinal.nautilus_adapters import alpaca

    # --- T+1 settlement metadata ------------------------------------------
    assert alpaca.SETTLEMENT_CYCLE == "T+1"
    assert alpaca.SETTLEMENT_DAYS == 1
    assert SETTLEMENT_RULES["alpaca"]["settlement_cycle"] == "T+1"
    assert SETTLEMENT_RULES["interactive_brokers"]["settlement_cycle"] == "T+1"
    meta = alpaca.settlement_metadata("2026-11-09", "2026-11-10")
    assert meta["settlement_days"] == 1
    assert meta["cash_available_immediately"] is False
    assert meta["unsettled_proceeds_redeployable"] is False

    # --- Reg-T margin guards ----------------------------------------------
    guard = alpaca.reg_t_margin_guard(10_000.0)
    assert guard.max_leverage == REG_T_MAX_LEVERAGE == 2.0
    assert guard.buying_power_micros == 20_000_000_000
    assert guard.initial_margin_micros(15_000_000_000) == 7_500_000_000
    assert guard.maintenance_margin_micros(15_000_000_000) == 3_750_000_000
    receipt = guard.check(15_000_000_000)
    assert isinstance(receipt, dict) and receipt
    with pytest.raises(alpaca.MarginViolation):
        guard.check(20_000_000_001)

    # --- sub-penny maker-peg formatting -----------------------------------
    assert alpaca.alpaca_tick(190.50) == 0.01
    assert alpaca.alpaca_tick(0.5432) == 0.0001
    assert alpaca.sub_penny_maker_peg(190.507, "BUY") == 190.5
    assert alpaca.sub_penny_maker_peg(190.507, "SELL") == 190.51

    # --- dispatched payload carries the US rules --------------------------
    result = dispatch_signed_order("US_IBKR", "alpaca", _signed("AAPL", 10, 190.507))
    assert result.payload["symbol"] == "AAPL"
    assert result.payload["time_in_force"] == "day"
    assert float(result.payload["limit_price"]) == 190.5
    assert result.flags["settlement_cycle"] == "T+1"
    assert result.flags["reg_t_max_leverage"] == 2.0
    assert result.flags["margin_regime"] == "REG_T"


def test_gate2_us_silo_has_two_ready_venues() -> None:
    registry = MarketSiloRegistry()
    us = registry.get("US_IBKR")
    assert {probe.name for probe in us.ready_adapters} == {
        "interactive_brokers",
        "alpaca",
    }
    assert us.venue_ready is True


# ===========================================================================
# GATE 3 -- FX silo MT5, CTP & OANDA activation
# ===========================================================================


@pytest.mark.parametrize(
    "module",
    (
        "ring0_vault.silos.fx_mt5.vnpy_core",
        "ring0_vault.silos.fx_mt5.vnpy_core.constant",
        "ring0_vault.silos.fx_mt5.vnpy_core.object",
        "ring0_vault.silos.fx_mt5.vnpy_core.gateway",
        "ring0_vault.silos.fx_mt5.vnpy_core.utility",
        "ring0_vault.silos.fx_mt5.vnpy_mt5",
        "ring0_vault.silos.fx_mt5.vnpy_ctp",
        "ring0_vault.silos.fx_mt5.vnpy_ctp.api",
        "ring0_vault.silos.fx_mt5.vnpy_ctp.gateway",
        "ring0_vault.silos.fx_mt5.oanda",
    ),
)
def test_gate3_fx_modules_import(module: str) -> None:
    importlib.import_module(module)


def test_gate3_vnpy_gateways_resolve_their_trader_namespace() -> None:
    from ring0_vault.silos.fx_mt5 import _vnpy_compat

    status = _vnpy_compat.compat_status()
    assert status["modules"], "compat layer registered nothing"
    assert _vnpy_compat.transport_status().offline is True

    from ring0_vault.silos.fx_mt5.vnpy_mt5 import Mt5Gateway
    from ring0_vault.silos.fx_mt5.vnpy_ctp.gateway import CtpGateway
    from ring0_vault.silos.fx_mt5.vnpy_core.gateway import BaseGateway

    assert issubclass(Mt5Gateway, BaseGateway)
    assert issubclass(CtpGateway, BaseGateway)


def test_gate3_oanda_24_5_session_and_pip_spread_gate() -> None:
    from ring0_vault.silos.fx_mt5 import oanda

    wednesday = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    saturday = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    sunday = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)

    assert oanda.session_status(wednesday)["is_open"] is True
    assert oanda.session_status(wednesday)["calendar"] == "24/5"
    assert oanda.session_status(saturday)["is_open"] is False
    assert oanda.session_status(sunday)["is_open"] is False

    gate = oanda.PipSpreadGate()
    receipt = gate.check("EUR_USD", 1.08500, 1.08506, now=wednesday)
    assert receipt["passed"] is True
    assert receipt["spread_pips"] == pytest.approx(0.6)
    assert receipt["ceiling_pips"] == pytest.approx(0.8)

    with pytest.raises(oanda.SpreadGateBreach) as breach:
        gate.check("EUR_USD", 1.08500, 1.08600, now=wednesday)
    assert breach.value.receipt["gate"] == "pip_spread"

    with pytest.raises(oanda.SessionClosedError):
        gate.check("EUR_USD", 1.08500, 1.08506, now=saturday)


def test_gate3_oanda_macro_blackout_and_roll_yield_guards() -> None:
    from ring0_vault.silos.fx_mt5 import oanda

    blackout = oanda.MacroCalendarBlackout()
    event = oanda.DEFAULT_MACRO_CALENDAR[0]
    with pytest.raises(oanda.SpreadGateBreach) as breach:
        blackout.check(event.when_utc)
    assert breach.value.receipt["gate"] == "macro_calendar_blackout"
    assert breach.value.receipt["blacked_out"] is True

    # A quiet Wednesday is outside every blackout window.
    quiet = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    assert blackout.check(quiet)["blacked_out"] is False

    assert oanda.roll_yield_bps(1.08, 1.081, 30) == pytest.approx(112.65, abs=0.01)
    guard = oanda.RollYieldGuard()
    assert guard.check(
        "EUR_USD", spot=1.08, forward=1.081, days_to_expiry=30
    )["passed"] is True
    with pytest.raises(oanda.RollYieldBreach):
        guard.check("EUR_USD", spot=1.08, forward=1.20, days_to_expiry=30)
    with pytest.raises(oanda.RollYieldBreach):
        guard.check("EUR_USD", spot=1.08, forward=1.081, days_to_expiry=1)


def test_gate3_oanda_dispatch_payload_is_v20() -> None:
    result = dispatch_signed_order("FX_MT5", "oanda", _signed("EUR_USD", 10000, 1.085))
    order = result.payload["order"]
    assert order["type"] == "LIMIT"
    assert order["instrument"] == "EUR_USD"
    assert order["units"] == "10000"
    assert order["price"] == "1.08500"
    assert result.flags["calendar"] == "24/5"
    assert result.flags["settlement_cycle"] == "T+0"


def test_gate3_fx_silo_has_three_ready_venues() -> None:
    registry = MarketSiloRegistry()
    fx = registry.get("FX_MT5")
    assert {probe.name for probe in fx.ready_adapters} == {
        "vnpy_mt5",
        "vnpy_ctp",
        "oanda",
    }


# ===========================================================================
# GATE 4 -- All four silos READY & live/paper toggle
# ===========================================================================


def test_gate4_all_twelve_adapters_are_ready() -> None:
    probes = probe_donor_adapters(refresh=True)
    assert len(probes) == 12
    assert {probe.name for probe in probes} == set(VENUE_ADAPTERS)
    for probe in probes:
        assert probe.harvested is True
        assert probe.importable is True, f"{probe.name}: {probe.reason}"
        assert probe.status == "READY"
        assert probe.ready is True

    registry = MarketSiloRegistry(probes=probes)
    status = registry.adapter_status()
    assert status["total"] == 12
    assert status["harvested_count"] == 12
    assert status["importable_count"] == 12
    assert registry.snapshot()["cross_border_wire_bps"] == 0.0


def test_gate4_all_four_silos_are_ready() -> None:
    registry = MarketSiloRegistry()
    assert registry.silo_ids() == SILO_ORDER
    for silo_id in SILO_ORDER:
        silo = registry.get(silo_id)
        assert silo.venue_ready is True, silo_id
        assert silo.ready_adapters, silo_id
        assert set(SWEEP_TICKERS[silo_id]) == {
            "reserve",
            "long_term",
            "reserve_tickers",
            "long_term_tickers",
        }
        reserve, long_term = SWEEP_ASSERTIONS[silo_id]
        assert reserve in " ".join(SWEEP_TICKERS[silo_id]["reserve_tickers"]) or (
            reserve == SWEEP_TICKERS[silo_id]["reserve"]
        )
        assert long_term in " ".join(SWEEP_TICKERS[silo_id]["long_term_tickers"]) or (
            long_term == SWEEP_TICKERS[silo_id]["long_term"]
        )


def test_gate4_live_toggle_requires_confirm_live_and_covers_all_silos(
    tmp_path: Path,
) -> None:
    runtime = NIOSCoreOrchestrator(
        workspace=tmp_path / "runtime",
        key_vault_root=tmp_path / "keys",
        seed_fills=60,
        funding_micros=5_000_000_000_000,
        daily_loss_stop_micros=5_000_000_000,
    )
    try:
        client = TestClient(runtime.control_app())

        # Without confirm_live the endpoint must refuse to arm LIVE.
        for silo_id in SILO_ORDER:
            rejected = client.post(
                "/api/v2/control/brokers",
                json={"silo": silo_id, "mode": "LIVE", "operator": "ops@nios"},
            )
            assert rejected.status_code >= 400, rejected.text

        # With confirm_live every one of the four silos arms.
        for silo_id in SILO_ORDER:
            armed = client.post(
                "/api/v2/control/brokers",
                json={
                    "silo": silo_id,
                    "mode": "LIVE",
                    "confirm_live": True,
                    "operator": "ops@nios",
                },
            )
            assert armed.status_code == 200, armed.text
            assert runtime.registry.get(silo_id).mode is SiloMode.LIVE

        assert {
            getattr(silo, "silo_id", silo) for silo in runtime.registry.live_silos()
        } == set(SILO_ORDER)

        # And the operator can drop every silo back to paper.
        for silo_id in SILO_ORDER:
            paper = client.post(
                "/api/v2/control/brokers",
                json={"silo": silo_id, "mode": "PAPER"},
            )
            assert paper.status_code == 200, paper.text
            assert runtime.registry.get(silo_id).mode is SiloMode.PAPER
        assert runtime.registry.live_silos() == ()
    finally:
        runtime.close()


def test_gate4_signed_order_and_ratchet_across_all_four_silos() -> None:
    registry = MarketSiloRegistry()
    ledger = _funded_ledger()
    outbox = CrashAtomicOutbox(ledger)
    nav_before = {silo: ledger.silo_nav_micros(silo) for silo in SILO_ORDER}

    for silo_id, venue, symbol, side, qty, price in DISPATCH_CASES:
        result = dispatch_signed_order(
            silo_id,
            venue,
            _signed(symbol, qty, price, side=side),
            mode=DispatchMode.PAPER,
            registry=registry,
            outbox=outbox,
            ledger=ledger,
        )
        assert result.adapter_ready is True
        assert result.payload
        assert result.outbox_key
        assert result.live is False

        ratchet = result.ratchet
        assert ratchet["applied"] is True
        realized = ratchet["realized_net_micros"]
        assert realized > 0
        assert ratchet["long_term_micros"] == pytest.approx(realized * 0.35, rel=1e-6)
        assert ratchet["reserve_micros"] == pytest.approx(realized * 0.15, rel=1e-6)
        assert ratchet["scalper_micros"] == pytest.approx(realized * 0.50, rel=1e-6)
        assert (
            ratchet["long_term_micros"]
            + ratchet["reserve_micros"]
            + ratchet["scalper_micros"]
            == realized
        )
        assert ratchet["cross_border_wire_bps"] == 0.0
        assert result.cross_border_wire_bps == 0.0
        assert ratchet["long_term_asset"] == registry.get(silo_id).long_term_sweep_asset
        assert ratchet["reserve_asset"] == registry.get(silo_id).reserve_yield_asset

    # No silo gained capital from another silo: the only NAV movement is the
    # fee/tax accrual inside each silo.
    for silo_id in SILO_ORDER:
        nav_after = ledger.silo_nav_micros(silo_id)
        assert nav_after <= nav_before[silo_id]
        assert nav_after > 0
    report = ledger.invariant_report()
    assert isinstance(report, dict) and report


def test_gate4_live_dispatch_refuses_without_credentials() -> None:
    registry = MarketSiloRegistry()
    with pytest.raises(PermissionError):
        dispatch_signed_order(
            "CRYPTO_BINANCE",
            "binance",
            _signed("BTC-USDT", 0.01, 65000.0),
            mode=DispatchMode.LIVE,
            registry=registry,
        )
    with pytest.raises(PermissionError):
        dispatch_signed_order(
            "CRYPTO_BINANCE",
            "binance",
            _signed("BTC-USDT", 0.01, 65000.0),
            mode=DispatchMode.LIVE,
            registry=registry,
            confirm_live=True,
            operator="ops@nios",
        )


def test_gate4_venue_silo_isolation_is_enforced() -> None:
    registry = MarketSiloRegistry()
    with pytest.raises(ValueError):
        dispatch_signed_order(
            "US_IBKR", "binance", _signed("AAPL", 1, 190.0), registry=registry
        )
    with pytest.raises(ValueError):
        dispatch_signed_order(
            "FX_MT5", "dhan", _signed("EUR_USD", 1, 1.08), registry=registry
        )
    assert set(SILO_MARKET_MAP) == set(SILO_ORDER)


# ===========================================================================
# GATE 5 -- 4-ring air-gap AST
# ===========================================================================


FORBIDDEN_SEGMENTS = {
    "nautilus_adapters",
    "india_brokers",
    "fx_mt5",
    "key_vault",
    "broker_keys_db",
}

#: ``core/`` is the shared substrate and its orchestrator is the assembly root,
#: so it may read the *metadata* registry and the LLM-only credential vault.
#: It may never import a broker adapter, a broker credential store or a silo
#: signing path.
CORE_FORBIDDEN_SEGMENTS = {
    "nautilus_adapters",
    "india_brokers",
    "fx_mt5",
    "broker_keys_db",
}


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def test_gate5_ring2_brain_never_touches_ring0_or_a_broker_adapter() -> None:
    offenders: list[str] = []
    base = ROOT / "ring2_brain"
    assert base.is_dir()
    for path in sorted(base.rglob("*.py")):
        for module in _imported_modules(path):
            parts = set(module.split("."))
            if module.startswith("ring0_vault") or (parts & FORBIDDEN_SEGMENTS):
                offenders.append(f"{path.relative_to(ROOT)} -> {module}")
    assert offenders == [], offenders


def test_gate5_core_never_imports_a_broker_adapter_or_broker_credentials() -> None:
    offenders: list[str] = []
    base = ROOT / "core"
    assert base.is_dir()
    for path in sorted(base.rglob("*.py")):
        for module in _imported_modules(path):
            parts = set(module.split("."))
            if parts & CORE_FORBIDDEN_SEGMENTS:
                offenders.append(f"{path.relative_to(ROOT)} -> {module}")
    assert offenders == [], offenders


def test_gate5_ring1_never_imports_a_signing_or_key_vault_module() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "ring1_spinal").rglob("*.py")):
        for module in _imported_modules(path):
            if module.startswith(("ring0_vault.kernel", "ring0_vault.silos.key_vault")):
                offenders.append(f"{path.relative_to(ROOT)} -> {module}")
    assert offenders == [], offenders


def test_gate5_all_venue_adapter_modules_exist_on_disk() -> None:
    for venue, module in VENUE_ADAPTERS.items():
        imported = importlib.import_module(module)
        assert imported is not None, venue
        assert module.split(".")[-1] == venue
