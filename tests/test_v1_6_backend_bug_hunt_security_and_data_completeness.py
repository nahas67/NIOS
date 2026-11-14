"""Suite 24 -- V1.6 backend bug hunt, security and data completeness.

Five gates:

1. **Master instrument registry & lot/tick enforcement** -- 40+ canonical
   instruments across the four silos carry complete contract specs, and
   ``SmartOMS`` deterministically rejects an off-lot / over-freeze order while
   snapping an off-tick price onto the exact tick grid.
2. **Itemized statutory tax & cost engine** -- every silo's itemized components
   sum to the exact statutory floor in integer micros and bps, and the
   breakdown is exposed on orders, backtests, ledger and logbook payloads.
3. **Adversarial numerical & boundary fuzzing** -- ``NaN`` / ``Inf`` / ``-Inf``
   / ``0`` / negative / ``1e18`` / traversal strings never produce a ``500``.
4. **Concurrency, idempotency & double-entry conservation** -- 32 threads
   funding, ordering, cancelling and reconciling leave the book balanced, with
   zero duplicate fills for an identical ``idempotency_key``.
5. **Auth brute-force lockout & secret non-leakage** -- consecutive failures
   answer ``429 TOO_MANY_ATTEMPTS`` and no API payload ever carries a raw
   secret, digest or signing key.

Nothing in this suite signs an order, reads a broker key or writes a live IBOR.
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"
DIST_ROOT = UI_ROOT / "dist"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.instrument_registry import (  # noqa: E402
    SILO_IDS,
    InstrumentRegistry,
    registry as instrument_registry,
)
from core.orchestrator import NIOSCoreOrchestrator  # noqa: E402
from core.schemas import UnsignedProposal, VaultName  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from ring0_vault.ibor.tax_and_cost_engine import (  # noqa: E402
    COST_FLOOR_BPS,
    StatutoryCostEngine,
    cost_engine,
)
from ring2_brain.quant.catalog import (  # noqa: E402
    CATALOG_TOTAL,
    ENGINE_TOTAL,
    LOADER_TOTAL,
    factor_catalog,
    engines,
    loaders,
)
from ring3_cockpit.gateway.payload_envelope import payload_digest  # noqa: E402

USERNAME = "bug-hunt-operator"
PASSWORD = "bug-hunt-password-9"
LIVE_PIN = "975310"

CANONICAL_SYMBOLS = (
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "INFY",
    "ICICIBANK",
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "SPY",
    "QQQ",
    "NVDA",
    "AAPL",
    "MSFT",
    "TSLA",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "XAUUSD",
)

REQUIRED_SPEC_FIELDS = (
    "lot_size",
    "tick_size_micros",
    "freeze_quantity",
    "circuit_limit_pct",
    "margin_initial_pct",
    "margin_maintenance_pct",
    "settlement_cycle",
    "trading_hours_utc",
    "currency",
    "option_strike_step",
)

SECRET_MARKERS = (
    PASSWORD,
    LIVE_PIN,
    "scrypt",
    "pbkdf2",
    "session_key",
    "BEGIN PRIVATE KEY",
)


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("nios-v16-bughunt-ws")
    vault = tmp_path_factory.mktemp("nios-v16-bughunt-vault")
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


@pytest.fixture(scope="module")
def headers(client, operator) -> dict[str, str]:
    token = client.post(
        "/api/v2/auth/login/paper",
        json={"username": USERNAME, "password": PASSWORD},
    ).json()["token"]
    return {"X-NIOS-Session": token}


def _proposal(silo: str, symbol: str, qty: float, price: float) -> UnsignedProposal:
    return UnsignedProposal(
        proposal_id=f"suite24-{silo.lower()}-{symbol.lower()}-{abs(hash((qty, price))) % 10**8}",
        strategy_id="suite24",
        silo=silo,
        vault=VaultName.SPINAL_SCALPER,
        symbol=symbol,
        side="BUY",
        qty=qty,
        limit_price=price,
        expected_gross_bps=24.0,
        spread_bps=1.0,
        grounding_hash="0" * 64,
        created_at_ns=1,
    )


# ---------------------------------------------------------------------------
# Gate 1 -- master instrument registry & lot/tick enforcement
# ---------------------------------------------------------------------------


def test_gate1_registry_covers_forty_plus_instruments_across_four_silos() -> None:
    specs = instrument_registry.all()
    assert len(specs) >= 40, len(specs)
    assert set(SILO_IDS) == set(instrument_registry.counts())
    for silo, count in instrument_registry.counts().items():
        assert count >= 9, (silo, count)

    for spec in specs:
        payload = spec.to_dict()
        for field in REQUIRED_SPEC_FIELDS:
            assert field in payload, (spec.silo, spec.symbol, field)
            assert payload[field] not in (None, ""), (spec.silo, spec.symbol, field)
        assert payload["tick_size_micros"] > 0
        assert payload["lot_size"] > 0
        assert payload["freeze_quantity"] >= payload["lot_size"]
        assert payload["margin_maintenance_pct"] <= payload["margin_initial_pct"]
        assert payload["settlement_cycle"] in {"T+0", "T+1", "T+2", "PERPETUAL"}
        assert payload["circuit_band_pct"]["upper"] == payload["circuit_limit_pct"]

    for symbol in CANONICAL_SYMBOLS:
        spec = instrument_registry.find(symbol)
        assert spec.symbol == symbol


def test_gate1_lot_size_is_enforced() -> None:
    # NIFTY trades in lots of 75; 74 is not an order.
    rejected = instrument_registry.validate_order(
        "INDIA_ZERODHA_DHAN", "NIFTY", 74, 24_000.0
    )
    assert rejected.ok is False
    assert "lot_size" in rejected.reason
    accepted = instrument_registry.validate_order(
        "INDIA_ZERODHA_DHAN", "NIFTY", 75, 24_000.0
    )
    assert accepted.ok is True


def test_gate1_freeze_quantity_is_enforced() -> None:
    spec = instrument_registry.get("CRYPTO_BINANCE", "BTCUSDT")
    over = instrument_registry.validate_order(
        "CRYPTO_BINANCE", "BTCUSDT", spec.freeze_quantity + 1.0, 50_000.0
    )
    assert over.ok is False
    assert "freeze_quantity" in over.reason


def test_gate1_tick_size_rounds_or_rejects() -> None:
    rounded = instrument_registry.validate_order(
        "US_IBKR", "SPY", 10, 512.345678
    )
    assert rounded.ok is True
    assert rounded.price_changed is True
    assert abs(rounded.adjusted_price * 1_000_000 % 10_000) == 0
    strict = instrument_registry.validate_order(
        "US_IBKR", "SPY", 10, 512.345678, round_price=False
    )
    assert strict.ok is False
    assert "tick" in strict.reason


def test_gate1_smart_oms_deterministically_rejects_bad_contracts(runtime) -> None:
    oms = runtime.control.oms
    with pytest.raises(ValueError):
        oms.validate_proposal(
            _proposal("INDIA_ZERODHA_DHAN", "NIFTY", 74, 24_000.0)
        )
    check = oms.validate_proposal(
        _proposal("INDIA_ZERODHA_DHAN", "NIFTY", 75, 24_000.0)
    )
    assert check.ok is True


def test_gate1_http_order_rejects_off_lot(client, headers) -> None:
    response = client.post(
        "/api/v2/control/orders/paper",
        headers=headers,
        json={
            "silo": "INDIA_ZERODHA_DHAN",
            "symbol": "NIFTY",
            "side": "BUY",
            "quantity": 74,
            "limit_price": 24000.0,
            "fill_immediately": True,
        },
    )
    assert response.status_code == 422, response.text
    assert "INSTRUMENT_REJECTED" in response.text


# ---------------------------------------------------------------------------
# Gate 2 -- itemized statutory tax & cost engine
# ---------------------------------------------------------------------------


def test_gate2_component_tables_reconcile_to_every_floor() -> None:
    report = cost_engine.assert_reconciles()
    assert set(report) == set(COST_FLOOR_BPS)
    for silo, row in report.items():
        assert row["component_sum_bps"] == row["floor_bps"] == COST_FLOOR_BPS[silo]


@pytest.mark.parametrize(
    "silo",
    ("INDIA_ZERODHA_DHAN", "CRYPTO_BINANCE", "US_IBKR", "FX_MT5"),
)
def test_gate2_breakdown_sums_exactly_in_micros_and_bps(silo: str) -> None:
    for notional in (1_000_000, 12_345_678, 999_999_999, 1):
        book = cost_engine.breakdown(silo, notional)
        assert book.total_bps == COST_FLOOR_BPS[silo]
        assert sum(c.bps for c in book.components) == pytest.approx(
            COST_FLOOR_BPS[silo], abs=1e-9
        )
        assert sum(c.micros for c in book.components) == book.total_micros
        assert all(isinstance(c.micros, int) for c in book.components)
        assert all(c.micros >= 0 for c in book.components)


def test_gate2_india_component_identity() -> None:
    book = cost_engine.breakdown("INDIA_ZERODHA_DHAN", 10_000_000)
    keys = set(book.component_bps)
    assert keys == {
        "stt_bps",
        "exchange_txn_bps",
        "sebi_turnover_bps",
        "stamp_duty_bps",
        "gst_18pct_bps",
        "brokerage_bps",
        "modeled_slippage_bps",
    }
    assert book.component_bps["stt_bps"] == 3.0


def test_gate2_engine_rejects_a_broken_component_table() -> None:
    with pytest.raises(ValueError):
        StatutoryCostEngine(
            floors={"INDIA_ZERODHA_DHAN": 9.0},
            components={"INDIA_ZERODHA_DHAN": (("stt_bps", 3.0),)},
        )


def test_gate2_cost_is_exposed_on_orders_backtest_ledger_and_logbook(
    client, headers
) -> None:
    funded = client.post(
        "/api/v2/control/ledger/fund-paper",
        headers=headers,
        json={"silo": "US_IBKR", "amount_units": 250_000.0, "vault": "ALL_CANONICAL"},
    )
    assert funded.status_code == 200, funded.text

    order = client.post(
        "/api/v2/control/orders/paper",
        headers=headers,
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "side": "BUY",
            "quantity": 10,
            "limit_price": 512.34,
            "fill_immediately": True,
        },
    )
    assert order.status_code == 200, order.text
    body = order.json()
    assert body["statutory_cost"]["total_bps"] == 2.80
    assert body["statutory_cost"]["reconciles"] is True
    assert body["instrument"]["symbol"] == "SPY"

    backtest = client.post(
        "/api/v2/control/backtest/run",
        headers=headers,
        json={
            "silo": "US_IBKR",
            "symbol": "SPY",
            "strategy_id": "ma_golden_cross",
            "bars": 120,
            "timeframe": "5m",
        },
    )
    assert backtest.status_code == 200, backtest.text
    assert backtest.json()["statutory_cost"]["total_bps"] == 2.80

    entry = client.post(
        "/api/v2/control/logbook/entry",
        headers=headers,
        json={
            "silo": "INDIA_ZERODHA_DHAN",
            "symbol": "RELIANCE",
            "side": "LONG",
            "entry_price": 2900.0,
            "exit_price": 2950.0,
            "quantity": 10,
        },
    )
    assert entry.status_code == 200, entry.text
    row = entry.json()["entry"]
    assert row["statutory_cost"]["total_bps"] == 8.50
    assert row["cost_drag_micros"] == row["statutory_cost"]["total_micros"]
    assert sum(row["statutory_cost"]["component_micros"].values()) == row[
        "cost_drag_micros"
    ]

    ledger = client.get("/api/v2/cockpit/ledger").json()
    assert ledger["statutory_costs"]["CRYPTO_BINANCE"]["cost_floor_bps"] == 16.0
    orders = client.get("/api/v2/cockpit/orders").json()
    assert orders["statutory_costs"]["FX_MT5"]["cost_floor_bps"] == 1.6
    logbook = client.get("/api/v2/cockpit/logbook").json()
    assert logbook["entries"][-1]["statutory_cost"]["reconciles"] is True


# ---------------------------------------------------------------------------
# Gate 3 -- adversarial numerical & boundary fuzzing
# ---------------------------------------------------------------------------

POISON_NUMBERS = (float("nan"), float("inf"), float("-inf"), 0, -1, 1e18)
TRAVERSAL = ("../../etc/passwd", "..\\secrets", "..", "/etc/passwd", "\x00bad")

FUZZ_CASES = {
    "/api/v2/control/orders/paper": {
        "silo": "US_IBKR",
        "symbol": "SPY",
        "side": "BUY",
        "quantity": 1,
        "limit_price": 100.0,
    },
    "/api/v2/control/quant/price-option": {
        "symbol": "BTC-OPT",
        "spot": 50_000.0,
        "strike": 46_500.0,
        "volatility": 0.62,
        "days_to_expiry": 30,
        "risk_free_rate": 0.05,
        "option_type": "PUT",
    },
    "/api/v2/control/backtest/run": {
        "silo": "US_IBKR",
        "symbol": "SPY",
        "strategy_id": "ma_golden_cross",
        "bars": 120,
        "timeframe": "5m",
    },
    "/api/v2/control/options/chain": {
        "symbol": "NIFTY",
        "spot": 24_000.0,
        "volatility": 0.18,
        "days_to_expiry": 30,
        "risk_free_rate": 0.06,
    },
    "/api/v2/control/options/strategy-payoff": {
        "symbol": "NIFTY",
        "strategy": "BULL_CALL_SPREAD",
        "spot": 24_000.0,
        "volatility": 0.18,
        "days_to_expiry": 30,
    },
    "/api/v2/control/ledger/fund-paper": {
        "silo": "US_IBKR",
        "amount_units": 1_000.0,
        "vault": "ALL_CANONICAL",
    },
    "/api/v2/control/logbook/entry": {
        "silo": "US_IBKR",
        "symbol": "SPY",
        "side": "LONG",
        "entry_price": 100.0,
        "exit_price": 101.0,
        "quantity": 1,
    },
    "/api/v2/control/brain/mirofish-shock": {
        "symbol": "BTCUSDT",
        "shock_bps": -45.0,
        "sentiment_tag": "panic",
        "volatility_ratio": 2.0,
        "silo": "CRYPTO_BINANCE",
    },
}

NUMERIC_FIELDS = {
    "/api/v2/control/orders/paper": ("quantity", "limit_price", "expected_gross_bps"),
    "/api/v2/control/quant/price-option": (
        "spot",
        "strike",
        "volatility",
        "days_to_expiry",
        "risk_free_rate",
    ),
    "/api/v2/control/backtest/run": ("bars",),
    "/api/v2/control/options/chain": (
        "spot",
        "volatility",
        "days_to_expiry",
        "risk_free_rate",
    ),
    "/api/v2/control/options/strategy-payoff": (
        "spot",
        "volatility",
        "days_to_expiry",
    ),
    "/api/v2/control/ledger/fund-paper": ("amount_units",),
    "/api/v2/control/logbook/entry": (
        "entry_price",
        "exit_price",
        "quantity",
        "stop_loss",
    ),
    "/api/v2/control/brain/mirofish-shock": ("shock_bps", "volatility_ratio"),
}

STRING_FIELDS = {
    "/api/v2/control/orders/paper": ("symbol", "silo", "strategy_id"),
    "/api/v2/control/quant/price-option": ("symbol",),
    "/api/v2/control/backtest/run": ("symbol", "silo", "strategy_id"),
    "/api/v2/control/options/chain": ("symbol",),
    "/api/v2/control/options/strategy-payoff": ("symbol",),
    "/api/v2/control/ledger/fund-paper": ("silo",),
    "/api/v2/control/logbook/entry": ("symbol", "silo", "setup_strategy"),
    "/api/v2/control/brain/mirofish-shock": ("symbol", "silo"),
}


@pytest.mark.parametrize("endpoint", sorted(FUZZ_CASES))
def test_gate3_numerical_poison_never_returns_500(client, headers, endpoint: str) -> None:
    base = dict(FUZZ_CASES[endpoint])
    for field in NUMERIC_FIELDS[endpoint]:
        for poison in POISON_NUMBERS:
            payload = dict(base)
            payload[field] = poison
            response = client.post(
                endpoint,
                headers={**headers, "Content-Type": "application/json"},
                content=json.dumps(payload, allow_nan=True),
            )
            assert response.status_code != 500, (endpoint, field, poison, response.text)
            assert response.status_code in (200, 400, 404, 409, 422), (
                endpoint,
                field,
                poison,
                response.status_code,
            )
            assert response.headers.get("content-type", "").startswith(
                "application/json"
            )


@pytest.mark.parametrize("endpoint", sorted(FUZZ_CASES))
def test_gate3_traversal_and_malformed_payloads_are_clean(
    client, headers, endpoint: str
) -> None:
    base = dict(FUZZ_CASES[endpoint])
    for field in STRING_FIELDS[endpoint]:
        for poison in TRAVERSAL:
            payload = dict(base)
            payload[field] = poison
            response = client.post(
                endpoint,
                headers={**headers, "Content-Type": "application/json"},
                content=json.dumps(payload, allow_nan=True),
            )
            assert response.status_code != 500, (endpoint, field, poison, response.text)
            assert response.status_code in (200, 400, 404, 409, 422)

    for malformed in ({}, [], {"unexpected": [1, 2, 3]}, {"silo": None}):
        response = client.post(endpoint, headers=headers, json=malformed)
        assert response.status_code != 500, (endpoint, malformed)
        assert response.status_code in (200, 400, 404, 409, 422)

    # Invalid JSON must be a clean 422, never a traceback.
    broken = client.post(
        endpoint,
        headers={**headers, "Content-Type": "application/json"},
        content=b"{not-json",
    )
    assert broken.status_code in (400, 422), broken.text


# ---------------------------------------------------------------------------
# Gate 4 -- concurrency, idempotency & double-entry conservation
# ---------------------------------------------------------------------------


def test_gate4_thirty_two_threads_leave_the_book_balanced(runtime, headers) -> None:
    control = runtime.control
    ledger = control.ledger
    silos = tuple(control.registry.silo_ids())
    assert len(silos) == 4

    errors: list[str] = []
    lock = threading.Lock()

    # Capital is the only way an order can be reserved; pre-fund every silo so
    # the concurrent order threads exercise the OMS, not the overdraft guard.
    for silo in silos:
        control.fund_paper(_funding(silo, 50_000_000.0))

    def _fund(index: int) -> None:
        silo = silos[index % len(silos)]
        try:
            control.fund_paper(
                _funding(silo, 1_000.0 + index)
            )
        except Exception as exc:  # noqa: BLE001 - collected as evidence
            with lock:
                errors.append(f"fund:{type(exc).__name__}:{exc}")

    def _order(index: int) -> None:
        silo = silos[index % len(silos)]
        symbol = {
            "CRYPTO_BINANCE": "BTCUSDT",
            "INDIA_ZERODHA_DHAN": "NIFTY",
            "US_IBKR": "SPY",
            "FX_MT5": "EURUSD",
        }[silo]
        quantity = {
            "CRYPTO_BINANCE": 0.01,
            "INDIA_ZERODHA_DHAN": 75,
            "US_IBKR": 1,
            "FX_MT5": 100_000.0,
        }[silo]
        try:
            control.submit_paper_order(
                _order_request(
                    silo,
                    symbol,
                    quantity,
                    idempotency_key=f"suite24-key-{index % 8}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"order:{type(exc).__name__}:{exc}")

    def _cancel(index: int) -> None:
        try:
            control.cancel_paper_orders(_cancel_request("ALL"))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"cancel:{type(exc).__name__}:{exc}")

    def _reconcile(index: int) -> None:
        try:
            control.reconcile_ledger(_reconcile_request())
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"reconcile:{type(exc).__name__}:{exc}")

    jobs = []
    for index in range(32):
        jobs.append((_fund, index))
        jobs.append((_order, index))
        jobs.append((_cancel, index))
        jobs.append((_reconcile, index))

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(fn, index) for fn, index in jobs]
        for future in futures:
            future.result(timeout=120)

    assert errors == [], errors[:10]

    for silo in silos:
        total = sum(
            account.balance for account in ledger.accounts(silo).values()
        )
        assert total == 0, (silo, total)
    invariant = ledger.invariant_report()
    assert invariant["holds"] is True
    assert invariant["violations"] == []

    # Idempotency: eight distinct keys, never more than eight reservations,
    # and each key maps to exactly one order id.
    reservations = {
        row.outbox_idempotency_key: row for row in control.outbox.all_reservations()
    }
    by_proposal: dict[str, set[str]] = {}
    for row in reservations.values():
        by_proposal.setdefault(str(row.proposal_id), set()).add(
            str(row.outbox_idempotency_key)
        )
    for proposal_id, keys in by_proposal.items():
        assert len(keys) == 1, (proposal_id, keys)
    idempotent = {
        row.proposal_id: row
        for row in reservations.values()
        if str(row.proposal_id).startswith("paper-idem-")
    }
    assert len(idempotent) == 8, sorted(idempotent)
    committed = [row for row in idempotent.values() if row.is_committed]
    assert len(committed) <= 8
    assert control.outbox.pending_count >= 0


def test_gate4_duplicate_idempotency_key_replays_one_receipt(
    client, headers
) -> None:
    payload = {
        "silo": "US_IBKR",
        "symbol": "SPY",
        "side": "BUY",
        "quantity": 1,
        "limit_price": 512.34,
        "fill_immediately": True,
        "idempotency_key": "suite24-http-idem-1",
    }
    first = client.post("/api/v2/control/orders/paper", headers=headers, json=payload)
    second = client.post("/api/v2/control/orders/paper", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["order_id"] == second.json()["order_id"]
    assert second.json()["duplicate"] is True
    assert second.json()["idempotent_replay"] is True

    invariant = client.get("/api/v2/cockpit/ledger").json()["invariant"]
    assert invariant["holds"] is True


# ---------------------------------------------------------------------------
# Gate 5 -- auth brute-force lockout & secret non-leakage
# ---------------------------------------------------------------------------


def test_gate5_operator_auth_locks_out_after_repeated_failures(tmp_path) -> None:
    from ring0_vault.silos.key_vault.operator_auth import (
        OperatorAuth,
        OperatorAuthLocked,
    )

    auth = OperatorAuth(
        tmp_path,
        max_failed_attempts=3,
        failure_window_s=600,
        base_lockout_s=60,
        max_lockout_s=600,
    )
    auth.setup_operator("lockout", "lockout-password-1", "123456")
    for _ in range(3):
        with pytest.raises(Exception):
            auth.login_operator("lockout", "wrong-password")
    state = auth.lockout_state()
    assert state["locked"] is True
    assert state["failures"] >= 3
    with pytest.raises(OperatorAuthLocked) as excinfo:
        auth.login_operator("lockout", "lockout-password-1")
    assert excinfo.value.retry_after_s >= 1


def test_gate5_http_login_answers_429_after_five_failures(tmp_path) -> None:
    from ring0_vault.silos.key_vault.operator_auth import OperatorAuth
    from ring3_cockpit.gateway.control_plane import create_app

    workspace = tmp_path / "ws"
    vault = tmp_path / "vault"
    workspace.mkdir()
    vault.mkdir()
    instance = NIOSCoreOrchestrator(
        workspace=workspace, key_vault_root=vault, clean_slate=True
    )
    try:
        instance.control._operator_auth = OperatorAuth(vault / "operator")
        app = create_app(instance.control)
        with TestClient(app) as local:
            setup = local.post(
                "/api/v2/auth/setup",
                json={
                    "username": "lockout",
                    "password": "lockout-password-1",
                    "live_pin": "123456",
                },
            )
            assert setup.status_code == 200, setup.text
            for attempt in range(5):
                failed = local.post(
                    "/api/v2/auth/login/paper",
                    json={"username": "lockout", "password": f"wrong-{attempt}"},
                )
                assert failed.status_code == 401, failed.text
            locked = local.post(
                "/api/v2/auth/login/paper",
                json={"username": "lockout", "password": "lockout-password-1"},
            )
            assert locked.status_code == 429, locked.text
            assert locked.json()["detail"] == "TOO_MANY_ATTEMPTS"
            assert int(locked.headers.get("retry-after", "0")) >= 1
    finally:
        try:
            instance.close()
        except Exception:
            pass


def test_gate5_no_api_payload_leaks_a_secret(client, headers) -> None:
    paths = (
        "/api/v2/cockpit/health",
        "/api/v2/cockpit/state",
        "/api/v2/cockpit/pages",
        "/api/v2/cockpit/orders",
        "/api/v2/cockpit/options",
        "/api/v2/cockpit/terminal",
        "/api/v2/cockpit/analyzer",
        "/api/v2/cockpit/quant",
        "/api/v2/cockpit/agents",
        "/api/v2/cockpit/backtest",
        "/api/v2/cockpit/forge",
        "/api/v2/cockpit/ledger",
        "/api/v2/cockpit/logbook",
        "/api/v2/cockpit/brokers",
        "/api/v2/cockpit/mcp",
        "/api/v2/control/vault/status",
        "/api/v2/auth/status",
    )
    bodies: list[str] = []
    for path in paths:
        response = client.get(path, headers=headers)
        assert response.status_code in (200, 401, 403), path
        bodies.append(response.text)
    blob = "\n".join(bodies)
    for marker in SECRET_MARKERS:
        assert marker not in blob, marker
    json.loads(bodies[-1])


# ---------------------------------------------------------------------------
# Manifest and quant data completeness (Part 2)
# ---------------------------------------------------------------------------


def test_manifest_is_headless_zero_javascript() -> None:
    index = DIST_ROOT / "index.html"
    assert index.is_file()
    text = index.read_text(encoding="utf-8")
    assert "HEADLESS_API" in text
    assert "<script" not in text.lower()
    assert "/assets/" not in text
    assert not (UI_ROOT / "src").exists()
    assert not (UI_ROOT / "package.json").exists()


def test_quant_catalog_is_complete() -> None:
    assert CATALOG_TOTAL == 490
    assert factor_catalog.total == 490
    assert set(factor_catalog.taxonomy_counts) == {
        "Momentum",
        "Volatility",
        "Microstructure / L2 Imbalance",
        "Cross-Asset Carry",
        "Quality / Valuation",
        "Regime / Tail-Risk",
    }
    for factor in factor_catalog.factors:
        assert factor.factor_id
        assert -1.0 <= factor.ic <= 1.0
        assert factor.decay_half_life_days > 0.0
        assert factor.z_score == pytest.approx(factor.z_score)
        assert factor.contribution_weight > 0.0

    assert len(loaders()) == LOADER_TOTAL
    for loader in loaders():
        assert loader.loader_id
        assert loader.fields_provided
        assert loader.refresh_cadence_s >= 0
        assert loader.cost_floor_bps in set(COST_FLOOR_BPS.values()) | {8.5}
        assert loader.status == "READY"

    assert len(engines()) == ENGINE_TOTAL
    for engine in engines():
        assert engine.engine_id
        assert engine.settlement_cycle
        assert engine.cost_floor_bps > 0.0


def test_quant_endpoint_exposes_full_telemetry(client) -> None:
    body = client.get("/api/v2/cockpit/quant").json()
    assert body["factor_catalog"]["catalog_total"] == 490
    assert body["data_loaders"]["loader_total"] == 47
    assert body["regional_engines"]["engine_total"] == 17
    factor = body["factor_catalog"]["factors"][0]
    for field in (
        "factor_id",
        "taxonomy",
        "ic",
        "decay_half_life_days",
        "z_score",
        "contribution_weight",
    ):
        assert field in factor, field
    loader = body["data_loaders"]["loaders"][0]
    for field in (
        "loader_id",
        "asset_class",
        "region",
        "refresh_cadence_s",
        "fields_provided",
        "cost_floor_bps",
        "status",
    ):
        assert field in loader, field
    assert body["payload_sha256"] == payload_digest(body)


# -- small builders ---------------------------------------------------------


def _funding(silo: str, amount: float):
    from ring3_cockpit.gateway.control_plane import PaperFundingRequest

    return PaperFundingRequest(silo=silo, amount_units=amount, vault="ALL_CANONICAL")


def _order_request(silo: str, symbol: str, quantity: float, *, idempotency_key: str):
    from ring3_cockpit.gateway.control_plane import PaperOrderRequest

    price = {
        "CRYPTO_BINANCE": 50_000.0,
        "INDIA_ZERODHA_DHAN": 24_000.0,
        "US_IBKR": 512.0,
        "FX_MT5": 1.0850,
    }[silo]
    return PaperOrderRequest(
        silo=silo,
        symbol=symbol,
        side="BUY",
        quantity=quantity,
        limit_price=price,
        fill_immediately=True,
        idempotency_key=idempotency_key,
    )


def _cancel_request(order_id: str):
    from ring3_cockpit.gateway.control_plane import CancelOrdersRequest

    return CancelOrdersRequest(order_id=order_id, reason="suite24")


def _reconcile_request():
    from ring3_cockpit.gateway.control_plane import LedgerReconcileRequest

    return LedgerReconcileRequest(silos=None)
