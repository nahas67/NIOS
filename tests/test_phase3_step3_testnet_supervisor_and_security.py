"""Phase 3 Step 3 -- live supervisor control, Lee-Ready flow, sandbox bridge.

Governing sections
------------------
``sec_004``  four-ring OS process isolation, SHM heartbeat spine, control plane;
``sec_006``  dual-vendor ingestion and executed-orderflow cross-verification;
``sec_011``  zero-trust credential custody (write-only, Ring 0 only);
``sec_015``  the four sovereign silos and their sandbox venue whitelists;
``sec_016``  authenticated control endpoints; sandbox-only dispatch;
``sec_018``  Law 1 (never trust one venue) and Law 4 (no orphaned reservations).

Gates
-----
1. Permanent git-index secret & key-material guard.
2. Live supervisor API control and crash-restart over HTTP.
3. Four-silo ``LIVE_PUBLIC_DUAL_VENDOR`` Lee-Ready executed-flow ingest.
4. End-to-end signed sandbox order, HMAC proof, 35/15 ratchet, void on 503.
5. Release prerequisites: test inventory, rebuilt bundle, tagged artifact.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest

from core.schemas import ExecutedFlowTick, UnsignedProposal, VaultName
from core.shm_ipc import OfiRingBuffer
from ring0_vault.silos.key_vault.broker_credentials import BrokerCredentialVault
from ring0_vault.silos.silo_registry import (
    broker_payload_schema,
    store_and_verify_broker_credentials,
)
from ring0_vault.silos.testnet_bridge import (
    CLEAN_VENUES,
    SANDBOX_VENUE_SPECS,
    SandboxNetworkDisabled,
    TestnetExecutionBridge as SandboxBridge,
)
from ring1_spinal.ws_multiplexer import SiloWebSocketMultiplexer
from ring2_brain.truth.live_public_feeds import (
    DUAL_VENDOR_AGREEMENT_PCT,
    FEED_MODE_LIVE,
    FLOW_SOURCE_LEE_READY,
    FLOW_SOURCE_SYNTHETIC_TAPE,
    FLOW_SOURCE_VENUE_TAPE,
    SILO_FEED_ORDER,    LeeReadyFlowEstimator,
    MultiSiloFeedConnector,
    VendorQuote,
    estimate_executed_flow_from_dual_quotes,
    venue_agreement,
)

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "dist"

#: Secret material that must never be tracked by git, ever (Gate 1).
FORBIDDEN_TRACKED_MARKERS: tuple[str, ...] = (
    ".secrets/",
    ".broker_secrets/",
    "llm_keys.json",
    ".tigerbeetle",
)
FORBIDDEN_TRACKED_SUFFIXES: tuple[str, ...] = (".key", ".pem")

#: The exact raw credentials Gate 4 plants and then hunts for in every receipt.
RAW_SECRETS: dict[str, tuple[str, str]] = {
    "binance": ("binance-testnet-key-AAA1", "binance-testnet-secret-9F2A"),
    "dhan": ("dhan-sandbox-key-BBB2", "dhan-sandbox-secret-8E3B"),
    "alpaca": ("alpaca-paper-key-CCC3", "alpaca-paper-secret-7D4C"),
    "oanda": ("oanda-practice-key-DDD4", "oanda-practice-secret-6C5D"),
}


# ---------------------------------------------------------------------------
# Deterministic offline transports
# ---------------------------------------------------------------------------


class AgreeingDualVendorTransport:
    """Two venues that agree, inside a rising tape, with no executed tape.

    ``binance``/``okx`` serve crypto; ``yahoo``/``stooq`` serve India, US and FX.
    Every symbol steps one documented notch per *venue pair*, so the mid rises
    monotonically and the Lee-Ready tick rule has an unambiguous uptick to
    classify.  Both venues are fed the same price, which is exactly what a
    coherent market looks like and what the estimator's agreement gate needs.
    """

    BASE = {
        "BTCUSDT": 64_000.0,
        "ETHUSDT": 2_600.0,
        "RELIANCE.NS": 2_900.0,
        "NIFTYBEES.NS": 285.0,
        "NVDA": 120.0,
        "VOO": 520.0,
        "EURUSD": 1.0842,
    }
    #: venue-native spellings (lower-case) -> canonical symbol.  Ordered so the
    #: most specific spelling wins before the bare ticker.
    ALIASES: tuple[tuple[str, str], ...] = (
        ("btc-usdt", "BTCUSDT"),
        ("btcusdt", "BTCUSDT"),
        ("btcusd", "BTCUSDT"),
        ("eth-usdt", "ETHUSDT"),
        ("ethusdt", "ETHUSDT"),
        ("ethusd", "ETHUSDT"),
        ("reliance.ns", "RELIANCE.NS"),
        ("reliance.in", "RELIANCE.NS"),
        ("niftybees.ns", "NIFTYBEES.NS"),
        ("niftybees.in", "NIFTYBEES.NS"),
        ("nvda.us", "NVDA"),
        ("nvda", "NVDA"),
        ("voo.us", "VOO"),
        ("voo", "VOO"),
        ("eurusd", "EURUSD"),
    )
    STEP_BPS = 8.0

    def __init__(self, *, tape: bool = False, spread_bps: float = 2.0) -> None:
        self.tape = bool(tape)
        self.spread_bps = float(spread_bps)
        self.calls: list[str] = []
        self._served: dict[str, int] = {}

    # -- price model -------------------------------------------------------
    def _price(self, symbol: str) -> float:
        base = self.BASE[symbol]
        seen = self._served.get(symbol, -1) + 1
        self._served[symbol] = seen
        notch = seen // 2  # primary + secondary of the same poll share a notch
        return base * (1.0 + self.STEP_BPS / 1e4 * notch)

    @classmethod
    def _symbol_from(cls, url: str) -> str:
        lowered = url.lower()
        for alias, symbol in cls.ALIASES:
            if alias in lowered:
                return symbol
        raise AssertionError(f"unexpected feed URL: {url}")

    def get_json(self, url: str):
        self.calls.append(url)
        if "aggTrades" in url or ("okx.com" in url and "trades" in url):
            if not self.tape:
                # No executed tape exists on these public quote endpoints.
                raise OSError("no executed tape for this venue")
            return [{"qty": "4.0", "isBuyerMaker": False}]
        symbol = self._symbol_from(url)
        price = self._price(symbol)
        half = price * self.spread_bps / 2.0 / 1e4
        if "bookTicker" in url:
            return {
                "bidPrice": str(price - half),
                "askPrice": str(price + half),
                "lastPrice": str(price + half),
            }
        if "okx.com" in url:
            return {
                "data": [
                    {
                        "bidPx": str(price - half),
                        "askPx": str(price + half),
                        "last": str(price + half),
                    }
                ]
            }
        if "yahoo" in url:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": price,
                                "bid": price - half,
                                "ask": price + half,
                                "regularMarketDayHigh": price * 1.01,
                            }
                        }
                    ],
                    "error": None,
                }
            }
        raise AssertionError(f"unexpected feed URL: {url}")

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        symbol = self._symbol_from(url)
        price = self._price(symbol)
        return (
            "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
            f"X,2026-11-12,09:15:00,{price},{price * 1.01},"
            f"{price * 0.99},{price},184000\n"
        )

    def status(self) -> dict:
        return {"calls": len(self.calls), "tape": self.tape}


class SandboxVenueTransport:
    """One httpx transport for all four silos' sandbox hosts.

    Records every request so Gate 4 can prove the exact venue auth header, the
    recomputable HMAC signature and the endpoint path -- without a socket.
    """

    def __init__(self, *, status: int = 200, order_ids: dict[str, str] | None = None):
        self.status = int(status)
        self.requests: list[httpx.Request] = []
        self.order_ids = order_ids or {
            "binance": "BIN-424242",
            "dhan": "DHAN-778899",
            "alpaca": "ALP-55123",
            "oanda": "OANDA-9001",
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "venue unavailable"})
        if "testnet.binance.vision" in url:
            return httpx.Response(200, json={"orderId": self.order_ids["binance"]})
        if "sandbox.dhan.co" in url:
            return httpx.Response(
                200, json={"orderId": self.order_ids["dhan"], "orderStatus": "TRANSIT"}
            )
        if "paper-api.alpaca.markets" in url:
            return httpx.Response(
                200, json={"id": self.order_ids["alpaca"], "status": "accepted"}
            )
        if "api-fxpractice.oanda.com" in url:
            return httpx.Response(
                200, json={"orderCreateTransaction": {"id": self.order_ids["oanda"]}}
            )
        return httpx.Response(404, json={"error": f"unknown sandbox host: {url}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def orchestrator(tmp_path: Path):
    from core.orchestrator import NIOSCoreOrchestrator

    runtime = NIOSCoreOrchestrator(workspace=tmp_path / "p3s3")
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture()
def client(orchestrator):
    from fastapi.testclient import TestClient

    return TestClient(orchestrator.control_app())


@pytest.fixture()
def broker_vault(tmp_path: Path) -> BrokerCredentialVault:
    """A private Fernet vault so a test never touches the operator's keystore."""

    return BrokerCredentialVault(root=tmp_path / "broker_keys")


def _tracked_files() -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _secret_violations(paths: list[str]) -> list[str]:
    bad: list[str] = []
    for path in paths:
        lowered = path.lower()
        if any(marker in lowered for marker in FORBIDDEN_TRACKED_MARKERS):
            bad.append(path)
            continue
        if any(lowered.endswith(suffix) for suffix in FORBIDDEN_TRACKED_SUFFIXES):
            bad.append(path)
    return bad


# ---------------------------------------------------------------------------
# Gate 1 -- permanent git index secret & key-material guard
# ---------------------------------------------------------------------------


def test_gate1_no_secret_or_key_material_is_tracked_by_git() -> None:
    paths = _tracked_files()
    assert len(paths) > 100, "git ls-files returned an implausibly small tree"

    violations = _secret_violations(paths)
    assert violations == [], f"secret material is tracked by git: {violations}"

    # The guard must be permanent: the patterns are ignored, not merely absent.
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        ".secrets/",
        ".broker_secrets/",
        "*.key",
        "*.pem",
        "llm_keys.json",
        "*.tigerbeetle",
    ):
        assert pattern in gitignore, f"{pattern} is missing from .gitignore"


def test_gate1_ring0_key_vault_is_git_ignored(orchestrator, tmp_path: Path) -> None:
    """A freshly written broker secret must not become a tracked file."""

    vault = BrokerCredentialVault(root=tmp_path / "leak_check")
    vault.put("broker.binance.api_key", "leak-check-value-1234")
    written = sorted(p.name for p in (tmp_path / "leak_check").glob("*"))
    assert written, "the vault wrote nothing; the guard would be vacuous"
    tracked = {Path(item).name for item in _tracked_files()}
    assert not (set(written) & tracked), written


# ---------------------------------------------------------------------------
# Gate 2 -- live supervisor API control and crash-restart over HTTP
# ---------------------------------------------------------------------------


def test_gate2_supervisor_starts_restarts_and_stops_over_http(client) -> None:
    initial = client.get("/api/v2/control/supervisor").json()
    assert initial["running"] is False
    assert initial["available"] is False
    assert initial["distinct_os_processes"] == 0
    assert initial["actions"] == ["start", "stop", "restart_ring", "status"]
    assert initial["workers"] == [
        "ring0-vault-worker",
        "ring1-spinal-worker",
        "ring2-brain-worker",
        "ring3-cockpit-gateway",
    ]

    try:
        start = client.post("/api/v2/control/supervisor", json={"action": "start"})
        assert start.status_code == 200, start.text
        body = start.json()
        assert body["changed"] is True
        assert body["distinct_os_processes"] == 4, body
        assert body["four_ring_isolation"] is True

        # ---- the cockpit must reflect the live topology, not a promise ----
        state = client.get("/api/v2/cockpit/state").json()
        rings = state["ring_processes"]
        assert rings["available"] is True
        assert rings["running"] is True
        assert rings["distinct_os_processes"] == 4

        pids = [entry["pid"] for entry in rings["rings"]]
        assert all(isinstance(pid, int) and pid > 0 for pid in pids), pids
        assert len(set(pids)) == 4, f"rings share an OS process: {pids}"
        assert all(entry["alive"] is True for entry in rings["rings"])

        ages = [entry["heartbeat_age_ms"] for entry in rings["rings"]]
        assert all(age is not None for age in ages), ages
        assert all(0.0 <= float(age) < 1_000.0 for age in ages), ages
        assert all(entry["spawned"] is True for entry in rings["rings"])

        # ---- crash recovery: Ring 1 must come back on a *new* PID --------
        before = {entry["worker"]: entry["pid"] for entry in rings["rings"]}
        restart = client.post(
            "/api/v2/control/supervisor",
            json={"action": "restart_ring", "ring_index": 1},
        )
        assert restart.status_code == 200, restart.text
        restart_body = restart.json()
        assert restart_body["changed"] is True
        assert restart_body["ring_index"] == 1
        assert restart_body["restart"]["worker"] == "ring1-spinal-worker"
        assert restart_body["new_pid"] != before["ring1-spinal-worker"]
        assert restart_body["pid_changed"] is True

        after_state = client.get("/api/v2/cockpit/state").json()
        after = after_state["ring_processes"]
        assert after["distinct_os_processes"] == 4
        by_worker = {entry["worker"]: entry for entry in after["rings"]}
        assert by_worker["ring1-spinal-worker"]["pid"] == restart_body["new_pid"]
        assert by_worker["ring1-spinal-worker"]["alive"] is True
        assert by_worker["ring1-spinal-worker"]["restarts"] >= 1
        # Ring 0 / 2 / 3 were never restarted by a Ring 1 recovery.
        for worker in (
            "ring0-vault-worker",
            "ring2-brain-worker",
            "ring3-cockpit-gateway",
        ):
            assert by_worker[worker]["pid"] == before[worker]

        healthy_ages = [
            entry["heartbeat_age_ms"]
            for entry in after["rings"]
            if entry["heartbeat_age_ms"] is not None
        ]
        assert healthy_ages and all(float(age) < 1_000.0 for age in healthy_ages)

        status = client.post(
            "/api/v2/control/supervisor", json={"action": "status"}
        ).json()
        assert status["changed"] is False
        assert status["distinct_os_processes"] == 4
    finally:
        stopped = client.post("/api/v2/control/supervisor", json={"action": "stop"})
        assert stopped.status_code == 200, stopped.text
        stop_body = stopped.json()
        assert stop_body["stopped"] is True
        assert stop_body["distinct_os_processes"] == 0

    final = client.get("/api/v2/cockpit/state").json()
    assert final["ring_processes"]["distinct_os_processes"] == 0
    assert final["ring_processes"]["running"] is False


def test_gate2_restart_without_a_running_supervisor_is_a_409(client) -> None:
    response = client.post(
        "/api/v2/control/supervisor",
        json={"action": "restart_ring", "ring_index": 2},
    )
    assert response.status_code == 409
    assert "start" in response.json()["detail"]


def test_gate2_supervisor_rejects_an_unknown_action(client) -> None:
    response = client.post(
        "/api/v2/control/supervisor", json={"action": "detonate"}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Gate 3 -- four-silo LIVE_PUBLIC_DUAL_VENDOR Lee-Ready flow ingest
# ---------------------------------------------------------------------------


def test_gate3_lee_ready_estimator_classifies_dual_quotes() -> None:
    """Unit-level proof the estimator is deterministic and self-consistent."""

    def quote(venue: str, mid: float, *, last: float | None = None) -> VendorQuote:
        half = mid * 2.0 / 2.0 / 1e4
        return VendorQuote(
            venue=venue,
            symbol="NVDA",
            bid=mid - half,
            ask=mid + half,
            last=mid if last is None else last,
            ts_ns=1_700_000_000_000_000_000,
        )

    agree, pct = venue_agreement(100.0, 100.5)
    assert agree is True and pct < DUAL_VENDOR_AGREEMENT_PCT
    disagree, wide = venue_agreement(100.0, 110.0)
    assert disagree is False and wide > DUAL_VENDOR_AGREEMENT_PCT

    estimator = LeeReadyFlowEstimator()
    now = 1_700_000_000_000_000_000
    cold = estimate_executed_flow_from_dual_quotes(
        "NVDA", quote("yahoo", 100.0), quote("stooq", 100.0), now_ns=now
    )
    assert cold.flow_source == FLOW_SOURCE_LEE_READY
    # A print exactly at the midpoint cannot be assigned a side; it is split.
    assert cold.taker_buy_volume_5s == pytest.approx(cold.taker_sell_volume_5s)
    assert cold.total_volume_5s > 0.0

    # A print at the offer *is* buyer-initiated (the quote rule), so a cold
    # observation is not always a tie.
    lifted = estimate_executed_flow_from_dual_quotes(
        "NVDA",
        quote("yahoo", 100.0, last=100.05),
        quote("stooq", 100.0, last=100.05),
        now_ns=now,
    )
    assert lifted.taker_buy_volume_5s > 0.0
    assert lifted.taker_sell_volume_5s == 0.0

    first = estimator.observe(
        "NVDA", quote("yahoo", 100.0), quote("stooq", 100.0), now_ns=now
    )
    assert first.observations == 1
    rising = [
        estimator.observe(
            "NVDA",
            quote("yahoo", 100.0 + step * 0.05),
            quote("stooq", 100.0 + step * 0.05),
            now_ns=now + step * 1_000_000,
        )
        for step in range(1, 6)
    ]
    last = rising[-1]
    assert last.taker_buy_volume_5s > last.taker_sell_volume_5s
    assert last.taker_buy_ratio_5s > 0.65
    assert last.venues_agree is True
    assert last.bid_cancel_to_fill_ratio < 3.0
    assert last.ask_cancel_to_fill_ratio < 3.0
    assert last.book_is_clean is True
    assert last.realized_vol_5s > 0.0
    assert last.normal_vol_5s >= 0.5
    assert last.to_tick().passes_anti_spoof_ofi("BUY") is True

    # A diverged pair must never report a clean book (Law 1).
    diverged = LeeReadyFlowEstimator().observe(
        "NVDA", quote("yahoo", 100.0), quote("stooq", 130.0), now_ns=now
    )
    assert diverged.venues_agree is False
    assert diverged.bid_cancel_to_fill_ratio >= 3.0
    assert diverged.book_is_clean is False
    assert diverged.to_tick().passes_anti_spoof_ofi("BUY") is False


def test_gate3_four_silos_emit_lee_ready_executed_flow_into_the_ofi_ring(
    orchestrator,
) -> None:
    transport = AgreeingDualVendorTransport(tape=False)
    ring = OfiRingBuffer(reset=True)
    shadow_book = orchestrator.shadow_book
    multiplexer = SiloWebSocketMultiplexer(
        "CRYPTO_BINANCE", shadow_book=shadow_book, ofi_ring=ring
    )
    guard = orchestrator.control.feed_guard
    assert guard is not None, "the SelfHealingDataGuard must be wired into Ring 3"

    connector = MultiSiloFeedConnector(
        transport=transport,
        guard=guard,
        ofi_ring=ring,
        multiplexer=multiplexer,
    )
    try:
        # Three polls: the first seeds the prior quote, the rest classify it.
        for _ in range(3):
            report = connector.ingest_cycle()

        assert set(report["silos"]) == set(SILO_FEED_ORDER)
        assert report["frames"] == 7, report["frames"]
        assert report["accepted"] == report["frames"]
        assert report["flow_ticks"] >= 4
        assert report["all_silos_executed_flow"] is True
        assert set(report["flow_sources"].values()) == {FLOW_SOURCE_LEE_READY}

        for silo in SILO_FEED_ORDER:
            entry = report["silos"][silo]
            assert entry["mode"] == FEED_MODE_LIVE, entry
            assert entry["accepted"] == entry["frames"], entry
            assert entry["rejected"] == 0, entry
            # 1. a verified dual-vendor bar exists for this silo
            frame = connector.connector(silo).last_frame
            assert frame is not None
            assert frame.accepted is True
            assert frame.bar.vendor_divergence_pct < 1.5
            # 2. and a non-zero executed-flow tick, with honest provenance
            assert frame.flow_source == FLOW_SOURCE_LEE_READY
            assert frame.flow_estimated is True
            assert frame.flow is not None
            assert entry["flow_ticks"] >= 1
            assert entry["taker_buy_volume_5s"] + entry["taker_sell_volume_5s"] > 0.0
            assert entry["taker_buy_ratio_5s"] > 0.5
            tick = frame.tick
            assert isinstance(tick, ExecutedFlowTick)
            assert tick.taker_buy_volume_5s > 0.0
            assert tick.taker_buy_ratio_5s >= 0.65
            assert tick.max_cancel_to_fill_ratio < 3.0
            assert tick.realized_vol_5s >= 0.0
            assert tick.normal_vol_5s > 0.0
            assert tick.passes_anti_spoof_ofi("BUY") is True

        # The OFI ring carries every silo's tick, not just the crypto tape.
        window = ring.window(time.time_ns(), window_ns=60_000_000_000)
        symbols = {tick.symbol for tick in window}
        assert {"BTCUSDT", "ETHUSDT"} <= symbols
        assert {"RELIANCE.NS", "NIFTYBEES.NS"} <= symbols
        assert {"NVDA", "VOO"} <= symbols
        assert "EURUSD" in symbols
        assert all(
            tick.taker_buy_volume_5s + tick.taker_sell_volume_5s > 0.0
            for tick in window
        )

        # The Ring 1 multiplexer accepted the quote-derived frames too.
        health = multiplexer.health()
        assert health["frames"] > 0
        assert len(multiplexer.latencies_ms) > 0
        assert len(transport.calls) > 0
    finally:
        connector.close()
        ring.close()


def test_gate3_a_real_executed_tape_outranks_the_estimator(orchestrator) -> None:
    """Truthfulness: a venue tape is never overwritten by the quote model."""

    connector = MultiSiloFeedConnector(
        transport=AgreeingDualVendorTransport(tape=True),
        guard=orchestrator.control.feed_guard,
        silos=("CRYPTO_BINANCE",),
    )
    try:
        connector.ingest_cycle()
        frame = connector.connector("CRYPTO_BINANCE").last_frame
        assert frame is not None
        assert frame.flow_source == FLOW_SOURCE_VENUE_TAPE
        assert frame.flow_estimated is False
        assert frame.flow is None
        assert frame.tick.taker_buy_volume_5s > 0.0
    finally:
        connector.close()


def test_gate3_feed_failure_still_degrades_without_fabricating_flow() -> None:
    class DeadTransport:
        def get_json(self, url: str):
            raise OSError("offline")

        def get_text(self, url: str):
            raise OSError("offline")

    connector = MultiSiloFeedConnector(transport=DeadTransport())
    try:
        report = connector.ingest_cycle()
        assert report["all_live"] is False
        for entry in report["silos"].values():
            assert entry["mode"] != FEED_MODE_LIVE
            # The rehearsal tape is labelled synthetic all the way down; it never
            # claims to be a venue print and never claims to be a quote estimate.
            assert entry["flow_source"] == FLOW_SOURCE_SYNTHETIC_TAPE
            assert entry["flow_estimated"] is False
    finally:
        connector.close()


# ---------------------------------------------------------------------------
# Gate 4 -- end-to-end signed sandbox order via TestnetExecutionBridge
# ---------------------------------------------------------------------------

SANDBOX_PLANS: tuple[tuple[str, str, str, float, float], ...] = (
    ("CRYPTO_BINANCE", "binance", "BTCUSDT", 0.01, 64_000.0),
    ("INDIA_ZERODHA_DHAN", "dhan", "RELIANCE.NS", 10.0, 2_900.0),
    ("US_IBKR", "alpaca", "NVDA", 10.0, 120.0),
    ("FX_MT5", "oanda", "EURUSD", 10_000.0, 1.0842),
)


def _store_sandbox_credentials(orchestrator, vault: BrokerCredentialVault) -> None:
    for silo, venue, _symbol, _qty, _px in SANDBOX_PLANS:
        key, secret = RAW_SECRETS[venue]
        report = store_and_verify_broker_credentials(
            silo,
            venue,
            key,
            secret,
            sandbox=True,
            registry=orchestrator.registry,
            vault=vault,
        )
        assert report["dry_run_verified"] is True
        assert report["sandbox"] is True
        assert report["sandbox_endpoint"]["environment"] != "production"
        assert report["api_key_masked"] == "***" + key[-4:]


def _proposal(silo: str, venue: str, symbol: str, qty: float, px: float) -> UnsignedProposal:
    return UnsignedProposal(
        proposal_id=f"p3s3-{venue}-{time.time_ns()}",
        strategy_id="phase3-step3-gate4",
        silo=silo,
        vault=VaultName.SPINAL_SCALPER,
        symbol=symbol,
        side="BUY",
        qty=qty,
        limit_price=px,
        expected_gross_bps=45.0,
        spread_bps=1.0,
        grounding_hash=hashlib.sha256(f"{venue}|{symbol}|{px}".encode()).hexdigest(),
    )


def test_gate4_all_four_silos_dispatch_a_signed_sandbox_order(
    orchestrator, broker_vault: BrokerCredentialVault
) -> None:
    _store_sandbox_credentials(orchestrator, broker_vault)
    venue_transport = SandboxVenueTransport()

    bridge = SandboxBridge(
        orchestrator.outbox,
        orchestrator.ledger,
        vault=broker_vault,
        registry=orchestrator.registry,
        transport=venue_transport.transport(),
    )
    try:
        assert bridge.allow_network is False
        receipts = {}
        for silo, venue, symbol, qty, px in SANDBOX_PLANS:
            before = orchestrator.ledger.vault_balances(silo)
            receipt = bridge.dispatch(
                silo, venue, _proposal(silo, venue, symbol, qty, px)
            )
            body = receipt.to_dict()
            receipts[venue] = body

            assert body["sandbox"] is True
            assert body["environment"] != "production"
            assert body["http_status"] == 200
            assert body["accepted"] is True
            assert body["committed"] is True
            assert body["voided"] is False
            assert body["outbox_state"] == "COMMITTED"
            assert body["broker_order_id"] == venue_transport.order_ids[venue]
            assert body["raw_secret_returned"] is False
            assert body["write_only"] is True

            # -- the exact venue auth surface --------------------------------
            spec = SANDBOX_VENUE_SPECS[venue]
            assert body["endpoint"].startswith(spec.base_url)
            for name in spec.required_headers:
                assert name in body["header_names"], (venue, name, body["header_names"])
            for name, value in body["masked_headers"].items():
                if name.lower() in (
                    "content-type",
                    "accept",
                    "api-version",
                    "x-kite-version",
                    "x-simulated-trading",
                    "x-bapi-recv-window",
                ):
                    continue
                assert value.startswith("***"), (venue, name, value)

            # -- no raw credential ever leaves Ring 0 -------------------------
            key, secret = RAW_SECRETS[venue]
            serialised = json.dumps(body)
            assert key not in serialised
            assert secret not in serialised
            assert body["api_key_masked"] == "***" + key[-4:]

            # -- the fill landed in the IBOR with the 35/15 ratchet ----------
            assert body["realized_net_micros"] > 0
            ratchet = body["ratchet"]
            assert ratchet["applied"] is True
            assert ratchet["long_term_pct"] == 35.0
            assert ratchet["reserve_pct"] == 15.0
            assert ratchet["cross_border_wire_bps"] == 0.0
            after = orchestrator.ledger.vault_balances(silo)
            assert (
                after[VaultName.LONG_TERM_COMPOUNDER]
                - before[VaultName.LONG_TERM_COMPOUNDER]
                == ratchet["long_term_micros"]
            )
            assert (
                after[VaultName.RESERVE_YIELD] - before[VaultName.RESERVE_YIELD]
                == ratchet["reserve_micros"]
            )
            # The retained 50% is re-credited to the scalper vault inside the
            # same silo, after the reservation debit settled at commit time.
            assert ratchet["scalper_micros"] == (
                body["realized_net_micros"]
                - ratchet["long_term_micros"]
                - ratchet["reserve_micros"]
            )
            assert body["reconcile"] is not None

        # ---- HMAC-SHA256 query signature is externally recomputable -------
        binance = next(
            request
            for request in venue_transport.requests
            if "testnet.binance.vision" in str(request.url)
        )
        assert binance.headers["X-MBX-APIKEY"] == RAW_SECRETS["binance"][0]
        raw_query = str(binance.url).split("?", 1)[1]
        assert "signature=" in raw_query
        unsigned, _, signature = raw_query.rpartition("&signature=")
        expected = hmac.new(
            RAW_SECRETS["binance"][1].encode("utf-8"),
            unsigned.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert unquote_plus(signature) == expected
        assert "timestamp=" in unsigned
        assert "recvWindow=" in unsigned
        assert binance.method == "POST"

        # ---- the other three venues carry their own auth scheme -----------
        dhan = next(
            request
            for request in venue_transport.requests
            if "sandbox.dhan.co" in str(request.url)
        )
        assert dhan.headers["access-token"] == RAW_SECRETS["dhan"][0]
        dhan_body = json.loads(dhan.content)
        for field in broker_payload_schema("dhan"):
            assert field in dhan_body, (field, sorted(dhan_body))
        assert dhan_body["quantity"] == 10.0
        assert str(dhan.url).endswith("/v2/orders")

        alpaca = next(
            request
            for request in venue_transport.requests
            if "paper-api.alpaca.markets" in str(request.url)
        )
        assert alpaca.headers["APCA-API-KEY-ID"] == RAW_SECRETS["alpaca"][0]
        assert alpaca.headers["APCA-API-SECRET-KEY"] == RAW_SECRETS["alpaca"][1]
        assert str(alpaca.url).endswith("/v2/orders")

        oanda = next(
            request
            for request in venue_transport.requests
            if "api-fxpractice.oanda.com" in str(request.url)
        )
        assert oanda.headers["Authorization"] == (
            "Bearer " + RAW_SECRETS["oanda"][0]
        )
        assert "/v3/accounts/" in str(oanda.url)
        assert str(oanda.url).endswith("/orders")

        # ---- outbox is fully resolved: no orphaned reservations -----------
        snapshot = orchestrator.outbox.snapshot()
        assert snapshot["pending"] == 0
        assert snapshot["counts"]["COMMITTED"] == len(SANDBOX_PLANS)
        assert snapshot["counts"]["VOIDED"] == 0
        assert snapshot["counts"]["PENDING"] == 0
        assert orchestrator.ledger.invariant_report().get("ok") is not False
        assert bridge.committed == len(SANDBOX_PLANS)
        assert bridge.voided == 0
        assert bridge.status()["transport_injected"] is True
        assert set(bridge.status()["venues"]) == set(CLEAN_VENUES)
    finally:
        bridge.close()


def test_gate4_http_503_voids_the_pending_reservation(
    orchestrator, broker_vault: BrokerCredentialVault
) -> None:
    _store_sandbox_credentials(orchestrator, broker_vault)
    flaky = SandboxVenueTransport(status=503)
    bridge = SandboxBridge(
        orchestrator.outbox,
        orchestrator.ledger,
        vault=broker_vault,
        registry=orchestrator.registry,
        transport=flaky.transport(),
        retry_backoff_s=0.0,
    )
    silo, venue, symbol, qty, px = SANDBOX_PLANS[1]  # Dhan sandbox
    try:
        available_before = orchestrator.ledger.available(
            silo, VaultName.SPINAL_SCALPER.value
        )
        balance_before = orchestrator.ledger.vault_balances(silo)
        receipt = bridge.dispatch(silo, venue, _proposal(silo, venue, symbol, qty, px))
        body = receipt.to_dict()

        assert body["http_status"] == 503
        assert body["accepted"] is False
        assert body["committed"] is False
        assert body["voided"] is True
        assert body["outbox_state"] == "VOIDED"
        # bounded retry: one attempt plus two retries, then the void
        assert body["attempts"] == 3
        assert len(flaky.requests) == 3
        assert bridge.retries == 2
        assert bridge.voided == 1

        # NoOrphanedOrders: the reservation is released, not merely forgotten.
        snapshot = orchestrator.outbox.snapshot()
        assert snapshot["pending"] == 0
        assert snapshot["counts"]["VOIDED"] == 1
        assert snapshot["counts"]["PENDING"] == 0
        reservation = orchestrator.outbox.get(body["reservation_key"])
        assert reservation is not None and reservation.is_voided
        assert orchestrator.ledger.available(
            silo, VaultName.SPINAL_SCALPER.value
        ) == available_before
        assert orchestrator.ledger.vault_balances(silo) == balance_before
    finally:
        bridge.close()


def test_gate4_a_4xx_rejection_voids_immediately(
    orchestrator, broker_vault: BrokerCredentialVault
) -> None:
    _store_sandbox_credentials(orchestrator, broker_vault)
    rejecting = SandboxVenueTransport(status=409)
    bridge = SandboxBridge(
        orchestrator.outbox,
        orchestrator.ledger,
        vault=broker_vault,
        registry=orchestrator.registry,
        transport=rejecting.transport(),
    )
    silo, venue, symbol, qty, px = SANDBOX_PLANS[2]  # Alpaca paper
    try:
        receipt = bridge.dispatch(silo, venue, _proposal(silo, venue, symbol, qty, px))
        body = receipt.to_dict()
        assert body["http_status"] == 409
        assert body["voided"] is True
        assert body["attempts"] == 1, "a 4xx is terminal, never retried"
        assert len(rejecting.requests) == 1
        assert orchestrator.outbox.get(body["reservation_key"]).is_voided
    finally:
        bridge.close()


def test_gate4_no_injected_transport_never_opens_a_socket(
    orchestrator, broker_vault: BrokerCredentialVault
) -> None:
    _store_sandbox_credentials(orchestrator, broker_vault)
    bridge = SandboxBridge(
        orchestrator.outbox,
        orchestrator.ledger,
        vault=broker_vault,
        registry=orchestrator.registry,
        allow_network=False,
    )
    silo, venue, symbol, qty, px = SANDBOX_PLANS[0]
    try:
        receipt = bridge.dispatch(silo, venue, _proposal(silo, venue, symbol, qty, px))
        body = receipt.to_dict()
        assert body["committed"] is False
        assert body["voided"] is True
        assert "refusing to open a live broker socket" in body["error"]
        assert orchestrator.outbox.get(body["reservation_key"]).is_voided
    finally:
        bridge.close()


def test_gate4_missing_credential_voids_before_any_request(
    orchestrator, broker_vault: BrokerCredentialVault, tmp_path: Path
) -> None:
    empty_vault = BrokerCredentialVault(root=tmp_path / "empty_keys")
    recorder = SandboxVenueTransport()
    bridge = SandboxBridge(
        orchestrator.outbox,
        orchestrator.ledger,
        vault=empty_vault,
        registry=orchestrator.registry,
        transport=recorder.transport(),
    )
    silo, venue, symbol, qty, px = SANDBOX_PLANS[3]
    try:
        receipt = bridge.dispatch(silo, venue, _proposal(silo, venue, symbol, qty, px))
        body = receipt.to_dict()
        assert body["committed"] is False
        assert body["voided"] is True
        assert "credential" in body["error"].lower()
        assert recorder.requests == [], "a socket was opened without a credential"
        assert orchestrator.outbox.get(body["reservation_key"]).is_voided
    finally:
        bridge.close()


def test_gate4_control_endpoint_dispatches_a_sandbox_order(
    orchestrator, client, broker_vault: BrokerCredentialVault
) -> None:
    orchestrator.control.broker_vault = broker_vault
    recorder = SandboxVenueTransport()

    # No credential yet: the endpoint refuses before creating a bridge.
    blocked = client.post(
        "/api/v2/control/brokers/testnet-order",
        json={
            "silo": "US_IBKR",
            "venue": "alpaca",
            "symbol": "NVDA",
            "side": "BUY",
            "qty": 10.0,
            "limit_price": 120.0,
            "sandbox": True,
            "confirm_sandbox": True,
        },
    )
    assert blocked.status_code == 409
    assert "credential" in blocked.json()["detail"].lower()

    _store_sandbox_credentials(orchestrator, broker_vault)
    orchestrator.control.testnet_bridge = SandboxBridge(
        orchestrator.outbox,
        orchestrator.ledger,
        vault=broker_vault,
        registry=orchestrator.registry,
        transport=recorder.transport(),
    )
    response = client.post(
        "/api/v2/control/brokers/testnet-order",
        json={
            "silo": "US_IBKR",
            "venue": "alpaca",
            "symbol": "NVDA",
            "side": "BUY",
            "qty": 10.0,
            "limit_price": 120.0,
            "sandbox": True,
            "confirm_sandbox": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["committed"] is True
    assert body["venue"] == "alpaca"
    assert body["sandbox"] is True
    assert body["raw_secret_returned"] is False
    assert body["order"]["symbol"] == "NVDA"
    assert len(recorder.requests) == 1

    state = client.get("/api/v2/control/brokers/testnet").json()
    assert state["sandbox_only"] is True
    assert state["write_only_credentials"] is True
    assert state["bridge_attached"] is True
    assert state["receipts"][-1]["venue"] == "alpaca"

    # A live-mode request is rejected by the request model, not by the bridge.
    live = client.post(
        "/api/v2/control/brokers/testnet-order",
        json={
            "silo": "US_IBKR",
            "venue": "alpaca",
            "symbol": "NVDA",
            "qty": 1.0,
            "limit_price": 120.0,
            "sandbox": False,
        },
    )
    assert live.status_code == 422


# ---------------------------------------------------------------------------
# Gate 5 -- release prerequisites (inventory, bundle, tagged artifact)
# ---------------------------------------------------------------------------


def test_gate5_test_inventory_and_rebuilt_cockpit_bundle() -> None:
    test_files = sorted((ROOT / "tests").glob("test_*.py"))
    assert len(test_files) == 16, [path.name for path in test_files]
    assert (
        ROOT / "tests" / "test_phase3_step3_testnet_supervisor_and_security.py"
        in test_files
    )
    assert (
        ROOT / "tests" / "test_phase3_step4_soak_metrics_and_chaos.py"
        in test_files
    )

    index = BUNDLE_DIR / "index.html"
    assert index.exists(), "the React bundle was not rebuilt"
    assets = sorted((BUNDLE_DIR / "assets").glob("*.js"))
    assert assets, "no bundled JavaScript chunk"
    bundle = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in assets)
    assert "START 4 RINGS" in bundle
    assert "RESTART RING 1" in bundle
    assert "/api/v2/control/supervisor" in bundle
    # Phase 3 Step 4 telemetry surface.
    assert "Prometheus / OpenTelemetry telemetry" in bundle
    assert "/api/v2/metrics" in bundle


def test_gate5_release_tag_is_annotated_when_present() -> None:
    completed = subprocess.run(
        ["git", "tag", "-l", "--format=%(objecttype) %(contents:subject)", "v3.0.0-PROD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    text = completed.stdout.strip()
    if not text:
        # The tag is created by the release step *after* this suite passes; the
        # guard below still forbids a lightweight tag from ever appearing.
        assert re.search(r"v3\.0\.0-PROD", (ROOT / "AGENTS.md").read_text(encoding="utf-8"))
        return
    assert text.split()[0] == "tag", f"v3.0.0-PROD must be annotated: {text}"
