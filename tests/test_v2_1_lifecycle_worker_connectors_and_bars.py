"""NIOS V2.1 -- Suite 34: ``G4`` lifecycle worker, ``G5`` connectors, ``G6`` bars.

Covers:

* ``G4`` -- the in-process lifecycle worker: the ``03:00 UTC`` TOTP window, the
  ``30-minute`` ``listenKey`` keepalive, the tiered ``1s -> 5s -> 10s -> 30s -> 1m``
  read-only ``5xx UNKNOWN`` resolution ladder, the ``VirtualMarginQuota`` TTL sweep,
  the concept-drift sizing cut and the reconciliation hook -- each one independently
  observed, each failure contained, and the whole scheduler driven by an injected clock.
* ``G5`` -- ``ConnectorRuntime``: three explicit modes, the float-to-micros boundary
  (including ``NaN``/``inf``/``None`` rejections), the mandatory SEBI ``algo_id``,
  Binance weight accounting, and a wallet sweep that refuses a regulated silo.
* ``G6`` -- ``LiveBarAggregator``: deterministic ``1m``/``5m``/``15m``/``1D`` OHLCV
  consolidation, integer-micro prices, the L2 ladder and the IV/GEX surface.
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.lifecycle_worker import (  # noqa: E402
    DEFAULT_JOBS,
    RETRY_DELAYS_S,
    NiosLifecycleWorker,
    UnknownOrderQueue,
)
from core.schemas import VaultName  # noqa: E402
from ring0_vault.ibor.alert_center import AlertCenter  # noqa: E402
from ring0_vault.ibor.ledger_outbox import (  # noqa: E402
    CrashAtomicOutbox,
    IBORLedger,
    LedgerStore,
)
from ring0_vault.ibor.state_journal import AtomicStateJournal  # noqa: E402
from ring0_vault.kernel.firewalls_and_oms import OrderSigner  # noqa: E402
from ring0_vault.silos.crypto_wallet_sweep import (  # noqa: E402
    CRYPTO_SILO,
    CryptoWalletSweeper,
)
from ring0_vault.silos.connector_runtime import (  # noqa: E402
    CONNECTOR_MODES,
    RUNTIME_SILOS,
    ConnectorError,
    ConnectorRuntime,
)
from ring0_vault.silos.virtual_margin_quota import VirtualMarginQuotaEngine  # noqa: E402
from ring2_brain.quant.bar_aggregator import (  # noqa: E402
    TIMEFRAMES,
    LiveBarAggregator,
)

SECOND_NS = 1_000_000_000
MINUTE_NS = 60 * SECOND_NS
HOUR_NS = 60 * MINUTE_NS
DAY_NS = 24 * HOUR_NS

#: 2026-11-16T00:00:00Z -- a fixed epoch so every clock assertion is arithmetic.
EPOCH = 1_763_251_200 * SECOND_NS


def _at(hours: float = 0.0, *, base: int = EPOCH) -> int:
    return int(base + hours * HOUR_NS)


@pytest.fixture()
def ledger() -> IBORLedger:
    store = LedgerStore(in_memory=True, probe=False)
    book = IBORLedger(
        store, silos=[CRYPTO_SILO, "US_IBKR", "INDIA_ZERODHA_DHAN", "FX_MT5"]
    )
    book.open_silo(CRYPTO_SILO, funding_micros=1_000_000_000)
    book.open_silo("US_IBKR", funding_micros=1_000_000_000)
    book.fund_vault("US_IBKR", VaultName.RESERVE_YIELD, 400_000_000)
    book.open_silo("INDIA_ZERODHA_DHAN", funding_micros=500_000_000)
    book.fund_vault("INDIA_ZERODHA_DHAN", VaultName.RESERVE_YIELD, 200_000_000)
    book.open_silo("FX_MT5", funding_micros=200_000_000)
    yield book
    book.close()


@pytest.fixture()
def alerts(tmp_path) -> AlertCenter:
    return AlertCenter(AtomicStateJournal(tmp_path / "lifecycle_alerts.json"))


# ---------------------------------------------------------------------------
# G4 -- the retry ladder
# ---------------------------------------------------------------------------


class _OrderBookConnector:
    """A read-only connector stub that answers ``fetch_open_orders``."""

    def __init__(self, status: str = "FILLED", *, calls_before_answer: int = 0) -> None:
        self.status = status
        self.calls_before_answer = int(calls_before_answer)
        self.calls: list[str] = []

    def fetch_open_orders(self, *, silo: str, now_ns: int | None = None):
        self.calls.append(silo)
        if len(self.calls) <= self.calls_before_answer:
            orders = []
        else:
            orders = [
                {"client_order_id": "ORD-1", "status": self.status},
            ]
        return {"orders": orders, "silo": silo}


def test_unknown_order_queue_follows_the_tiered_delay_ladder() -> None:
    connector = _OrderBookConnector(status="FILLED", calls_before_answer=1)
    queue = UnknownOrderQueue(connector=connector)
    row = queue.enqueue(
        order_id="ORD-1", silo=CRYPTO_SILO, symbol="BTC/USDT", now_utc=_at(0)
    )
    assert row["state"] == "PENDING"
    assert row["next_attempt_at_utc_ns"] == _at(0) + RETRY_DELAYS_S[0] * SECOND_NS

    # Attempt 1 at +1 s: not due before then.
    assert queue.due(now_utc=_at(0)) == ()
    first = queue.tick(now_utc=_at(0) + 1 * SECOND_NS)
    assert first[0]["attempts"] == 1
    assert first[0]["state"] == "PENDING"
    # 5 s is the second rung of the ladder.
    assert first[0]["next_attempt_at_utc_ns"] == _at(0) + 1 * SECOND_NS + 5 * SECOND_NS

    # Attempt 2 at +6 s: the venue now answers FILLED.
    second = queue.tick(now_utc=_at(0) + 6 * SECOND_NS)
    assert second[0]["state"] == "RESOLVED"
    assert second[0]["resolution"] == "FILLED"
    status = queue.status()
    assert status["attempts"] == 2
    assert status["resolved"] == 1
    assert status["query_before_retry"] is True
    assert status["never_resubmits"] is True


def test_unknown_order_queue_escalates_instead_of_retrying_blind() -> None:
    connector = _OrderBookConnector(status="OPEN", calls_before_answer=0)

    class _Empty:
        def fetch_open_orders(self, *, silo: str, now_ns: int | None = None):
            return {"orders": []}

    queue = UnknownOrderQueue(connector=_Empty(), delays_s=(1, 1), max_attempts=2)
    queue.enqueue(order_id="ORD-2", silo="US_IBKR", now_utc=_at(0))
    queue.tick(now_utc=_at(0) + 1 * SECOND_NS)
    escalated = queue.tick(now_utc=_at(0) + 3 * SECOND_NS)
    assert escalated[0]["state"] == "ESCALATED"
    assert escalated[0]["resolution"] == "AWAITING_OPERATOR"
    assert queue.status()["escalated"] == 1
    assert connector.calls == []


def test_unknown_order_queue_without_a_connector_never_invents_a_resolution() -> None:
    queue = UnknownOrderQueue()
    queue.enqueue(order_id="ORD-3", silo="US_IBKR", now_utc=_at(0))
    row = queue.tick(now_utc=_at(0) + 1 * SECOND_NS)[0]
    assert row["state"] == "PENDING"
    assert row["history"][0]["resolution"] == "NO_CONNECTOR"


# ---------------------------------------------------------------------------
# G4 -- the jobs
# ---------------------------------------------------------------------------


class _TotpStub:
    def __init__(self, state: str = "RENEWED") -> None:
        self.state = state
        self.calls: list[int] = []

    def tick(self, *, now_ns: int | None = None):
        self.calls.append(int(now_ns or 0))
        return [
            {
                "silo": "INDIA_ZERODHA_DHAN",
                "venue": "zerodha",
                "state": self.state,
                "token": "masked",
            }
        ]


def test_totp_job_runs_once_per_utc_day_at_0300(alerts) -> None:
    totp = _TotpStub()
    worker = NiosLifecycleWorker(alert_center=alerts, totp_renewal=totp)

    # 02:59 UTC: the window is closed, so nothing runs.
    before = worker.run_maintenance_tick(now_utc=_at(2.98))
    assert not any(row["job"] == "totp_renewal" for row in before["outcomes"])

    # 03:00 UTC: the window opens exactly once.
    at_window = worker.run_maintenance_tick(now_utc=_at(3.0))
    jobs = [row["job"] for row in at_window["outcomes"]]
    assert "totp_renewal" in jobs
    assert len(totp.calls) == 1

    # 09:00 UTC the same day: still once.
    worker.run_maintenance_tick(now_utc=_at(9.0))
    assert len(totp.calls) == 1

    # Next day 03:00 UTC: once more.
    worker.run_maintenance_tick(now_utc=_at(27.0))
    assert len(totp.calls) == 2


def test_totp_failure_raises_a_critical_expiry_alert(alerts) -> None:
    worker = NiosLifecycleWorker(
        alert_center=alerts, totp_renewal=_TotpStub(state="EXPIRED")
    )
    worker.run_maintenance_tick(now_utc=_at(3.0))
    listed = alerts.list_alerts()
    assert listed["count"] == 1
    assert listed["alerts"][0]["code"] == "TOTP_EXPIRY_WARNING"
    assert listed["alerts"][0]["severity"] == "CRITICAL"
    assert listed["alerts"][0]["silo"] == "INDIA_ZERODHA_DHAN"


class _ListenKeyConnector:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.pings = 0

    def listen_key_keepalive(self, *, now_ns: int | None = None):
        self.pings += 1
        return {"ok": self.ok, "venue": "binance", "expires_in_s": 1_800}


def test_listen_key_pings_every_thirty_minutes(alerts) -> None:
    connector = _ListenKeyConnector()
    worker = NiosLifecycleWorker(alert_center=alerts, connector=connector)
    worker.enabled = True

    worker.run_maintenance_tick(now_utc=_at(0))
    assert connector.pings == 1
    heartbeat = worker.status(now_utc=_at(0))["listen_key"]
    assert heartbeat["status"] == "ALIVE"
    assert heartbeat["expires_at_utc_ns"] == _at(0) + 60 * MINUTE_NS

    # 29 minutes later the cadence has not elapsed.
    worker.run_maintenance_tick(now_utc=_at(29 / 60))
    assert connector.pings == 1
    # 30 minutes later it has.
    worker.run_maintenance_tick(now_utc=_at(0.5))
    assert connector.pings == 2
    assert worker.status()["listen_key"]["pings"] == 2


def test_dead_listen_key_raises_a_critical_alert(alerts) -> None:
    worker = NiosLifecycleWorker(
        alert_center=alerts, connector=_ListenKeyConnector(ok=False)
    )
    worker.run_maintenance_tick(now_utc=_at(0))
    codes = [row["code"] for row in alerts.list_alerts()["alerts"]]
    assert "LISTEN_KEY_EXPIRED" in codes


def test_quota_ttl_sweep_expires_stale_virtual_margin(ledger) -> None:
    engine = VirtualMarginQuotaEngine(ledger, ttl_seconds=900)
    quota = engine.issue("US_IBKR", 10_000_000, now_ns=_at(0))
    assert engine.status(now_ns=_at(0))["active_count"] == 1

    worker = NiosLifecycleWorker(quota_engine=engine)
    fresh = worker.run_maintenance_tick(now_utc=_at(0))
    sweep = next(row for row in fresh["outcomes"] if row["job"] == "quota_ttl_sweep")
    assert sweep["detail"]["active_count"] == 1

    # 20 minutes later the 15-minute TTL has elapsed.
    stale = worker.run_maintenance_tick(now_utc=_at(20 / 60))
    sweep = next(row for row in stale["outcomes"] if row["job"] == "quota_ttl_sweep")
    assert sweep["detail"]["active_count"] == 0
    assert sweep["detail"]["no_funds_moved"] is True
    assert engine.status(now_ns=_at(20 / 60))["active_count"] == 0
    assert quota.quota_id


class _Drift:
    def __init__(self, multiplier: float) -> None:
        self._multiplier = multiplier

    def allocation_multiplier(self) -> float:
        return self._multiplier

    def snapshot(self) -> dict:
        return {"allocation_multiplier": self._multiplier}


def test_concept_drift_cut_raises_a_warning(alerts) -> None:
    worker = NiosLifecycleWorker(alert_center=alerts, drift_monitor=_Drift(0.5))
    worker.run_maintenance_tick(now_utc=_at(0))
    codes = [row["code"] for row in alerts.list_alerts()["alerts"]]
    assert "CONCEPT_DRIFT_SIZING_CUT" in codes

    calm = NiosLifecycleWorker(drift_monitor=_Drift(1.0))
    detail = next(
        row["detail"]
        for row in calm.run_maintenance_tick(now_utc=_at(0))["outcomes"]
        if row["job"] == "concept_drift_check"
    )
    assert detail["throttled"] is False


def test_one_failing_job_never_stops_the_others(alerts) -> None:
    class _Broken:
        def tick(self, *, now_ns: int | None = None):
            raise RuntimeError("totp backend on fire")

    worker = NiosLifecycleWorker(
        alert_center=alerts,
        totp_renewal=_Broken(),
        connector=_ListenKeyConnector(),
        drift_monitor=_Drift(1.0),
    )
    report = worker.run_maintenance_tick(now_utc=_at(3.0))
    by_job = {row["job"]: row["status"] for row in report["outcomes"]}
    assert by_job["totp_renewal"] == "FAILED"
    assert by_job["listen_key_keepalive"] == "OK"
    assert by_job["concept_drift_check"] == "OK"
    assert "totp backend on fire" in worker.status()["failures"]["totp_renewal"]


def test_disabled_worker_runs_no_jobs(ledger) -> None:
    engine = VirtualMarginQuotaEngine(ledger)
    worker = NiosLifecycleWorker(quota_engine=engine, enabled=False)
    report = worker.run_maintenance_tick(now_utc=_at(0))
    assert report["jobs_run"] == 0
    assert report["enabled"] is False


def test_worker_start_and_stop_leaves_no_task() -> None:
    async def _exercise() -> None:
        worker = NiosLifecycleWorker()
        started = await worker.start(interval_s=0.05)
        assert started["started"] is True
        running = worker.status()["running"]
        assert running is True
        stopped = await worker.stop()
        assert stopped["stopped"] is True
        assert worker.status()["running"] is False

    asyncio.run(_exercise())


def test_job_catalogue_is_the_documented_five_plus_retry() -> None:
    assert DEFAULT_JOBS["listen_key_keepalive"] == 30 * 60
    assert DEFAULT_JOBS["quota_ttl_sweep"] == 5 * 60
    assert DEFAULT_JOBS["concept_drift_check"] == 15 * 60
    assert DEFAULT_JOBS["unknown_order_retry"] == 1
    assert RETRY_DELAYS_S == (1, 5, 10, 30, 60)


# ---------------------------------------------------------------------------
# G5 -- the connector runtime
# ---------------------------------------------------------------------------


def test_connector_runtime_defaults_to_deterministic_offline() -> None:
    runtime = ConnectorRuntime()
    status = runtime.status()
    assert status["connector_mode"] == "OFFLINE_REPLAY"
    assert status["allow_network"] is False
    assert status["air_gap"] is True
    assert status["money_units"] == "integer_micros"
    assert list(CONNECTOR_MODES) == ["OFFLINE_REPLAY", "SANDBOX_TESTNET", "LIVE_VENUE"]
    assert tuple(status["silos"]) == RUNTIME_SILOS


def test_live_mode_without_network_is_refused() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        ConnectorRuntime(connector_mode="LIVE_VENUE", allow_network=False)
    assert excinfo.value.code == "LIVE_MODE_REQUIRES_ALLOW_NETWORK"


def test_sandbox_mode_without_a_transport_fails_closed() -> None:
    runtime = ConnectorRuntime(connector_mode="SANDBOX_TESTNET", allow_network=True)
    with pytest.raises(ConnectorError) as excinfo:
        runtime.place_order(
            silo="US_IBKR", symbol="NVDA", side="BUY", qty=1.0, price=185.5
        )
    assert excinfo.value.code == "TRANSPORT_NOT_CONFIGURED"


def test_place_order_quantizes_every_venue_number_to_micros() -> None:
    runtime = ConnectorRuntime()
    order = runtime.place_order(
        silo=CRYPTO_SILO,
        symbol="btc/usdt",
        side="buy",
        qty=1.234567,
        price=64_000.123456,
    )
    assert order["qty_micros"] == 1_234_567
    assert order["price_micros"] == 64_000_123_456
    assert order["filled_qty_micros"] == 1_234_567
    assert order["status"] == "FILLED"
    assert order["mode"] == "OFFLINE_REPLAY"
    assert order["side"] == "BUY"
    assert order["symbol"] == "BTC/USDT"
    for key in ("qty_micros", "price_micros", "filled_qty_micros", "avg_price_micros"):
        assert isinstance(order[key], int)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, True])
def test_unquantizable_venue_numbers_are_rejected_not_zeroed(bad) -> None:
    runtime = ConnectorRuntime()
    with pytest.raises(ConnectorError) as excinfo:
        runtime.place_order(silo=CRYPTO_SILO, symbol="BTC/USDT", side="BUY", qty=bad)
    assert excinfo.value.code in ("VALUE_UNQUANTIZABLE", "QTY_INVALID")


def test_india_orders_require_a_sebi_algo_id() -> None:
    runtime = ConnectorRuntime()
    with pytest.raises(ConnectorError) as excinfo:
        runtime.place_order(
            silo="INDIA_ZERODHA_DHAN", symbol="RELIANCE", side="BUY", qty=10, price=2400.0
        )
    assert excinfo.value.code == "ALGO_ID_MISSING"

    accepted = runtime.place_order(
        silo="INDIA_ZERODHA_DHAN",
        symbol="RELIANCE",
        side="BUY",
        qty=10,
        price=2400.0,
        algo_id="NIOS-ALGO-0001",
    )
    assert accepted["algo_id"] == "NIOS-ALGO-0001"
    assert accepted["status"] == "FILLED"


def test_ingest_venue_order_normalizes_a_ccxt_document() -> None:
    runtime = ConnectorRuntime()
    order = runtime.ingest_venue_order(
        CRYPTO_SILO,
        {
            "symbol": "BTC/USDT",
            "amount": 0.5,
            "price": 63_000.5,
            "filled": 0.25,
            "side": "sell",
            "type": "limit",
            "status": "open",
            "id": "12345",
            "clientOrderId": "cid-1",
            "timestamp": 1_700_000_000_000,
        },
    )
    assert order.side == "SELL"
    assert order.qty_micros == 500_000
    assert order.price_micros == 63_000_500_000
    assert order.filled_qty_micros == 250_000
    assert order.venue_order_id == "12345"
    assert order.to_dict()["qty_micros"] == 500_000


def test_ingest_venue_order_normalizes_an_openalgo_document() -> None:
    runtime = ConnectorRuntime()
    order = runtime.ingest_venue_order(
        "INDIA_ZERODHA_DHAN",
        {
            "tradingsymbol": "reliance",
            "quantity": 15,
            "average_price": 2401.25,
            "transaction_type": "S",
            "order_type": "MARKET",
            "order_status": "complete",
            "orderid": "IND-9",
            "algo_id": "ALGO-77",
            "exchange": "NSE",
        },
    )
    assert order.symbol == "RELIANCE"
    assert order.side == "SELL"
    assert order.qty_micros == 15_000_000
    assert order.price_micros == 2_401_250_000
    assert order.algo_id == "ALGO-77"


def test_binance_weight_budget_is_enforced() -> None:
    runtime = ConnectorRuntime(weight_budget=3)
    for _ in range(3):
        runtime.fetch_open_orders(silo=CRYPTO_SILO, now_ns=_at(0))
    with pytest.raises(ConnectorError) as excinfo:
        runtime.fetch_open_orders(silo=CRYPTO_SILO, now_ns=_at(0))
    assert excinfo.value.code == "WEIGHT_BUDGET_EXCEEDED"
    assert excinfo.value.retryable is True
    # The next minute resets the bucket.
    runtime.fetch_open_orders(silo=CRYPTO_SILO, now_ns=_at(0) + MINUTE_NS)
    assert runtime.status()["weight"][CRYPTO_SILO] == 1


def test_transport_payload_positions_and_balances_land_in_micros() -> None:
    def transport(op: str, request: dict) -> dict:
        if op == "fetch_positions":
            return {
                "positions": [
                    {
                        "symbol": "BTC/USDT",
                        "contracts": -0.75,
                        "entryPrice": 60_000.25,
                        "markPrice": 61_000.5,
                        "unrealizedPnl": -12.5,
                        "leverage": 10,
                    }
                ]
            }
        if op == "fetch_balances":
            return {
                "balances": [
                    {"asset": "USDT", "free": 1_234.567891, "used": 100.5, "total": 1_335.067891}
                ]
            }
        return {"orders": []}

    runtime = ConnectorRuntime(
        connector_mode="SANDBOX_TESTNET",
        allow_network=True,
        transports={CRYPTO_SILO: transport},
    )
    positions = runtime.fetch_positions(silo=CRYPTO_SILO)
    row = positions["positions"][0]
    assert row["qty_micros"] == -750_000
    assert row["side"] == "SHORT"
    assert row["entry_price_micros"] == 60_000_250_000
    assert row["mark_price_micros"] == 61_000_500_000

    balances = runtime.fetch_balances(silo=CRYPTO_SILO)
    balance = balances["balances"][0]
    assert balance["free_micros"] == 1_234_567_891
    assert balance["used_micros"] == 100_500_000
    assert balance["total_micros"] == 1_335_067_891


def test_wallet_sweep_is_crypto_only_and_uses_the_gate_ladder(ledger) -> None:
    sweeper = CryptoWalletSweeper(
        ledger, outbox=CrashAtomicOutbox(ledger), signer=OrderSigner()
    )
    runtime = ConnectorRuntime(sweeper=sweeper)
    receipt = runtime.execute_wallet_sweep(
        silo=CRYPTO_SILO, amount_micros=10_000_000, direction="FUTURE_TO_SPOT"
    )
    assert receipt["accepted"] is True
    assert receipt["from_wallet"] == "UMFUTURE_MAIN"
    assert receipt["to_wallet"] == "SPOT"
    assert receipt["signature_present"] is True
    assert runtime.status()["sweeps"] == 1

    with pytest.raises(ConnectorError) as excinfo:
        runtime.execute_wallet_sweep(silo="US_IBKR", amount_micros=1_000_000)
    assert excinfo.value.code == "SILO_HAS_NO_WALLET_SURFACE"


def test_leverage_tiers_are_normalized_per_silo() -> None:
    runtime = ConnectorRuntime()
    crypto = runtime.leverage_tiers(silo=CRYPTO_SILO)
    assert crypto["count"] == 3
    assert crypto["tiers"][0]["max_leverage"] == 125.0
    india = runtime.leverage_tiers(silo="INDIA_ZERODHA_DHAN")
    assert india["tiers"][0]["max_leverage"] == 5.0
    for tier in crypto["tiers"] + india["tiers"]:
        assert isinstance(tier["max_notional_micros"], int)


def test_quote_is_grounded_and_deterministic() -> None:
    runtime = ConnectorRuntime()
    first = runtime.quote(silo=CRYPTO_SILO, symbol="BTC/USDT", now_ns=1)
    second = runtime.quote(silo=CRYPTO_SILO, symbol="BTC/USDT", now_ns=2)
    assert first["last_micros"] == second["last_micros"]
    assert first["bid_micros"] < first["last_micros"] < first["ask_micros"]
    assert first["spread_bps"] > 0


def test_unknown_silo_is_a_typed_rejection() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        ConnectorRuntime().fetch_open_orders(silo="MARS_EXCHANGE")
    assert excinfo.value.code == "UNKNOWN_SILO"


# ---------------------------------------------------------------------------
# G6 -- tick-to-bar aggregation
# ---------------------------------------------------------------------------


def test_ingest_builds_all_four_timeframes_in_integer_micros() -> None:
    aggregator = LiveBarAggregator()
    tick = aggregator.ingest(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        price=64_000.5,
        size=0.25,
        bid=63_999.5,
        ask=64_001.5,
        now_ns=_at(0) + 5 * SECOND_NS,
    )
    assert tick["price_micros"] == 64_000_500_000
    assert tick["size_micros"] == 250_000
    assert len(tick["bars"]) == len(TIMEFRAMES) == 4
    for bar in tick["bars"]:
        assert isinstance(bar["open_micros"], int)
        assert bar["open_micros"] == bar["close_micros"] == 64_000_500_000
        assert bar["volume_micros"] == 250_000
        assert bar["trades"] == 1
        assert len(bar["bar_sha256"]) == 64
        assert bar["closed"] is False


def test_bar_ohlcv_accumulates_and_hash_is_stable() -> None:
    aggregator = LiveBarAggregator()
    base = _at(0)
    for offset, price in ((0, 100.0), (10, 105.0), (20, 95.0), (30, 101.0)):
        aggregator.ingest(
            silo=CRYPTO_SILO,
            symbol="BTC/USDT",
            price=price,
            size=1.0,
            now_ns=base + offset * SECOND_NS,
        )
    live = aggregator.bars(silo=CRYPTO_SILO, symbol="BTC/USDT", timeframe="1m")[-1]
    assert live["open_micros"] == 100_000_000
    assert live["high_micros"] == 105_000_000
    assert live["low_micros"] == 95_000_000
    assert live["close_micros"] == 101_000_000
    assert live["volume_micros"] == 4_000_000
    assert live["trades"] == 4

    # A second aggregator replayed with the same ticks is byte-identical.
    clone = LiveBarAggregator()
    for offset, price in ((0, 100.0), (10, 105.0), (20, 95.0), (30, 101.0)):
        clone.ingest(
            silo=CRYPTO_SILO,
            symbol="BTC/USDT",
            price=price,
            size=1.0,
            now_ns=base + offset * SECOND_NS,
        )
    assert (
        clone.bars(silo=CRYPTO_SILO, symbol="BTC/USDT", timeframe="1m")[-1]["bar_sha256"]
        == live["bar_sha256"]
    )


def test_crossing_a_boundary_closes_the_bar_and_opens_the_next() -> None:
    aggregator = LiveBarAggregator()
    aggregator.ingest(
        silo=CRYPTO_SILO, symbol="BTC/USDT", price=100.0, size=1.0, now_ns=_at(0)
    )
    rolled = aggregator.ingest(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        price=110.0,
        size=2.0,
        now_ns=_at(0) + 61 * SECOND_NS,
    )
    assert rolled["closed_count"] == 1  # only 1m rolled; 5m/15m/1D are still inside their bucket
    closed = rolled["closed_bars"][0]
    assert closed["timeframe"] == "1m"
    assert closed["closed"] is True
    assert closed["close_micros"] == 100_000_000

    history = aggregator.bars(
        silo=CRYPTO_SILO, symbol="BTC/USDT", timeframe="1m", include_live=False
    )
    assert len(history) == 1
    assert history[0]["close_micros"] == 100_000_000

    live = aggregator.bars(silo=CRYPTO_SILO, symbol="BTC/USDT", timeframe="1m")[-1]
    assert live["open_micros"] == 110_000_000
    assert live["closed"] is False


def test_daily_bar_buckets_on_the_utc_day() -> None:
    aggregator = LiveBarAggregator()
    aggregator.ingest(silo="US_IBKR", symbol="NVDA", price=180.0, size=1.0, now_ns=_at(9))
    aggregator.ingest(silo="US_IBKR", symbol="NVDA", price=185.0, size=1.0, now_ns=_at(20))
    daily = aggregator.bars(silo="US_IBKR", symbol="NVDA", timeframe="1D")
    assert len(daily) == 1
    assert daily[0]["open_micros"] == 180_000_000
    assert daily[0]["close_micros"] == 185_000_000
    assert daily[0]["start_utc_ns"] == EPOCH


def test_l2_ladder_is_sorted_and_bounded() -> None:
    aggregator = LiveBarAggregator(depth_levels=3)
    aggregator.ingest(
        silo=CRYPTO_SILO,
        symbol="BTC/USDT",
        price=100.0,
        bids=[(99.0, 1.0), (99.5, 2.0), (98.0, 3.0), (97.0, 4.0)],
        asks=[(101.0, 1.0), (100.5, 2.0), (102.0, 3.0)],
        now_ns=_at(0),
    )
    depth = aggregator.depth(silo=CRYPTO_SILO, symbol="BTC/USDT")
    assert depth["levels"] == 3
    assert [row["price_micros"] for row in depth["bids"]] == [
        99_500_000,
        99_000_000,
        98_000_000,
    ]
    assert [row["price_micros"] for row in depth["asks"]] == [
        100_500_000,
        101_000_000,
        102_000_000,
    ]


def test_option_chain_folds_into_an_iv_gex_surface() -> None:
    aggregator = LiveBarAggregator()
    aggregator.ingest(
        silo="US_IBKR", symbol="NVDA", price=185.0, size=1.0, now_ns=_at(0)
    )
    surface = aggregator.on_option_chain(
        silo="US_IBKR",
        symbol="NVDA",
        expiry="2026-12-18",
        strikes=[
            {"strike": 180.0, "type": "CALL", "iv": 0.42, "gamma": 0.03, "open_interest": 1200},
            {"strike": 185.0, "type": "CALL", "iv": 0.38, "gamma": 0.05, "open_interest": 2400},
            {"strike": 185.0, "type": "PUT", "iv": 0.40, "gamma": 0.05, "open_interest": 1800},
            {"strike": 190.0, "type": "PUT", "iv": 0.45, "gamma": 0.02, "open_interest": 900},
        ],
        now_ns=_at(0),
    )
    assert surface["strike_count"] == 4
    assert surface["spot_micros"] == 185_000_000
    assert surface["atm_iv"] == 0.38
    assert surface["iv_rank_bps"] == 3800
    assert surface["put_call_ratio"] == round((1800 + 900) / (1200 + 2400), 6)
    assert isinstance(surface["gamma_exposure_micros"], int)
    assert len(surface["surface_sha256"]) == 64
    assert aggregator.surface(silo="US_IBKR", symbol="NVDA")["strike_count"] == 4


def test_sink_failures_never_break_ingestion() -> None:
    class _BadSink:
        def ingest_bar(self, **kwargs):
            raise RuntimeError("sink exploded")

        def update_spot(self, **kwargs):
            raise RuntimeError("sink exploded")

    aggregator = LiveBarAggregator(features_sink=_BadSink())
    aggregator.ingest(silo=CRYPTO_SILO, symbol="BTC/USDT", price=100.0, now_ns=_at(0))
    aggregator.ingest(
        silo=CRYPTO_SILO, symbol="BTC/USDT", price=101.0, now_ns=_at(0) + 61 * SECOND_NS
    )
    status = aggregator.status()
    assert status["ticks_ingested"] == 2
    assert status["sink_attached"] is True
    assert status["sink_errors"], "a sink failure must be observable, not swallowed"


def test_ingest_rejects_a_non_positive_price() -> None:
    aggregator = LiveBarAggregator()
    with pytest.raises(ValueError):
        aggregator.ingest(silo=CRYPTO_SILO, symbol="BTC/USDT", price=0.0)
    with pytest.raises(ValueError):
        aggregator.ingest(silo=CRYPTO_SILO, symbol="", price=1.0)


def test_bars_rejects_an_unknown_timeframe() -> None:
    with pytest.raises(ValueError):
        LiveBarAggregator().bars(timeframe="7m")


def test_status_reports_the_timeframe_contract() -> None:
    status = LiveBarAggregator().status()
    assert status["timeframes"] == list(TIMEFRAMES) == ["1m", "5m", "15m", "1D"]
    assert status["law"].startswith("sec_009")
