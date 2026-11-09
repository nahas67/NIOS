"""PHASE 2 -- STEP 3 verification: feeds, multiplexers, LLM pool and budget.

Gates
-----
1. **100% loader & regional-engine activation.**  Every harvested ``*.py``
   module under ``vibe_trading/loaders/`` and ``vibe_trading/engines/`` imports
   with zero ``ImportError``; the 12 regional engines (plus the options
   portfolio) resolve; ``DualVendorLoaderRouter`` feeds
   ``SelfHealingDataGuard`` and quarantine rejects a divergent pair.
2. **Silo-sharded WebSocket multiplexer SLA & gap recovery.**  10,000 L2/trade
   frames across the four silos normalize at ``p99 < 0.05 ms``; lagged
   (>5,000 ms) and sequence-gapped frames are dropped; exponential backoff
   doubles; ``ZeroGCSpinalEngine`` drains every resulting ``ExecutedFlowTick``.
3. **HTTP/2 multi-LLM switchboard & 250 ms circuit breaker.**  Every System 1 /
   System 2 provider completes through an injected ``httpx.MockTransport``; a
   260 ms System 1 response trips the breaker and demotes the provider; the
   switchboard still refreshes the 60 s SHM lease inside the ``< 78 ms p99``
   SLA via the deterministic fallback.
4. **$3.10/day token budget cap & cockpit telemetry.**  ``TokenBudgetLedger``
   tracks exact prompt/completion tokens and USD; the cap locks every paid
   provider and routes to ``$0.00`` local inference; ``GET
   /api/v2/cockpit/state`` and ``POST /api/v2/control/llms`` expose the live
   metrics and hot-swap the cap.
5. **4-ring air-gap AST & full-system regression.**  ``ws_multiplexer.py`` has
   zero Ring 2 / LLM / key-vault imports, Ring 2 has zero Ring 0 / broker
   imports, and every prior suite file is present for the full regression run.
"""

from __future__ import annotations

import ast
import gc
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from core.llm_transport import (
    BASELINE_USD_PER_DAY,
    DEFAULT_DAILY_USD_CAP,
    HARD_TIMEOUT_MS,
    LLMHttp2TransportPool,
    ProviderTimeout,
    TokenBudgetLedger,
)
from core.orchestrator import NIOSCoreOrchestrator
from core.schemas import LEASE_TTL_NS, LeaseMode, System1Lease
from core.shm_ipc import (
    GlobalKillLatch,
    LeaseTableSHM,
    OfiRingBuffer,
    ShadowBookSHM,
)
from ring1_spinal.spinal_loop import TickOutcome, ZeroGCSpinalEngine
from ring1_spinal.ws_multiplexer import (
    SiloMultiplexerPool,
    SiloWebSocketMultiplexer,
    StreamState,
)
from ring2_brain.quant.vibe_trading.activation import (
    REGIONAL_ENGINE_EXPORTS,
    activate_loaders_and_engines,
    loader_activation_status,
    regional_engine_classes,
)
from ring2_brain.truth.data_guard import (
    CallableLoader,
    DualVendorLoaderRouter,
    QuarantineStore,
    SelfHealingDataGuard,
)
from ring2_brain.cognition.system1_instinct import (
    InstinctContext,
    MultiLLMSwitchboard,
    P99_SLA_MS,
    ProviderResponse,
    System1InstinctController,
)

ROOT = Path(__file__).resolve().parents[1]
SILOS = ("CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5")
SYMBOLS = {
    "CRYPTO_BINANCE": "BTC-USDT",
    "INDIA_ZERODHA_DHAN": "RELIANCE.NS",
    "US_IBKR": "AAPL",
    "FX_MT5": "EUR_USD",
}
MS = 1_000_000
NS = 1_000_000_000


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


# ===========================================================================
# GATE 1 -- 100% loader & regional engine activation
# ===========================================================================


def test_gate1_every_loader_and_engine_module_imports() -> None:
    report = activate_loaders_and_engines(refresh=True)
    assert report.import_error_count == 0, [
        entry.to_dict() for entry in (*report.loader_failures, *report.engine_failures)
    ]
    assert report.ok is True
    # 47 harvested loader files (46 .py + the SEC lineage sidecar) and 17
    # engine files; every importable module is imported.
    assert report.loader_file_count == 47
    assert report.engine_file_count == 17
    assert len(report.loaders) == 45
    assert len(report.engines) == 16
    assert report.total_modules == 61

    # Every declared regional engine exported its class/function.
    for module_name, exports in REGIONAL_ENGINE_EXPORTS.items():
        entry = next(
            item for item in report.engines if item.module.endswith(f".{module_name}")
        )
        assert entry.ok, entry.to_dict()
        assert set(exports).issubset(set(entry.exports)), (
            module_name,
            entry.exports,
        )

    classes = regional_engine_classes(refresh=False)
    for expected in (
        "CryptoEngine",
        "ForexEngine",
        "ChinaAEngine",
        "ChinaFuturesEngine",
        "GlobalFuturesEngine",
        "GlobalEquityEngine",
        "IndiaEquityEngine",
        "KoreaEquityEngine",
        "VietnamEquityEngine",
        "BrazilEquityEngine",
        "ArgentinaEquityEngine",
        "CompositeEngine",
    ):
        assert expected in classes, expected

    status = loader_activation_status(refresh=False)
    assert status["ok"] is True
    assert status["import_errors"] == 0
    assert {"binance", "yfinance", "stooq", "okx", "finnhub"} <= set(
        status["registry_sources"]
    )


def test_gate1_dual_vendor_router_feeds_self_healing_guard(tmp_path: Path) -> None:
    guard = SelfHealingDataGuard(
        quarantine=QuarantineStore(tmp_path / "quarantine")
    )
    primary = CallableLoader("vendor-a", lambda *args: 100.0)
    secondary = CallableLoader("vendor-b", lambda *args: 100.05)
    router = DualVendorLoaderRouter(guard, primary, secondary)

    now = time.time_ns()
    bar = router.quote("AAPL", start_date="2026-11-01", end_date="2026-11-09", as_of_ns=now)
    assert bar.symbol == "AAPL"
    assert bar.p1 == pytest.approx(100.0)
    assert bar.p2 == pytest.approx(100.05)
    assert bar.feed_lag_ms == pytest.approx(0.0)
    assert bar.passes_quarantine is True

    normalized = router.route(
        "AAPL", start_date="2026-11-01", end_date="2026-11-09", as_of_ns=now
    )
    assert normalized is not None
    assert normalized.symbol == "AAPL"
    assert normalized.adjusted_price == pytest.approx(100.025)
    assert len(normalized.sha256_digest) == 64
    assert guard.stats.accepted == 1

    # A 10% cross-vendor divergence must be quarantined, never normalized.
    divergent = DualVendorLoaderRouter(
        guard,
        CallableLoader("vendor-a", lambda *args: 100.0),
        CallableLoader("vendor-b", lambda *args: 110.0),
    )
    rejected = divergent.route("AAPL", as_of_ns=now)
    assert rejected is None
    assert guard.stats.rejected == 1
    assert guard.stats.divergence_rejected == 1
    assert divergent.status()["rejected"] == 1


# ===========================================================================
# GATE 2 -- silo-sharded WebSocket multiplexers
# ===========================================================================


def _frame(
    kind: str,
    symbol: str,
    seq: int,
    ts_ns: int,
    price: float,
) -> dict:
    if kind == "l2":
        return {
            "type": "l2",
            "symbol": symbol,
            "seq": seq,
            "bid": price - 0.01,
            "ask": price + 0.01,
            "bid_qty": 5.0,
            "ask_qty": 6.0,
            "ts_ns": ts_ns,
        }
    return {
        "type": "trade",
        "symbol": symbol,
        "seq": seq,
        "price": price,
        "qty": 1.0,
        "side": "BUY" if seq % 2 == 0 else "SELL",
        "ts_ns": ts_ns,
    }


def test_gate2_multiplexers_meet_sla_and_drain_into_spinal_engine() -> None:
    book = ShadowBookSHM(reset=True, n_slots=256)
    ring = OfiRingBuffer(reset=True, n_slots=16_384)
    multiplexers = {
        silo: SiloWebSocketMultiplexer(
            silo,
            shadow_book=book,
            ofi_ring=ring,
            base_backoff_ms=25.0,
            max_backoff_ms=2_000.0,
        )
        for silo in SILOS
    }
    try:
        base = time.time_ns()
        sequences = {symbol: 0 for symbol in SYMBOLS.values()}

        # Warm the per-symbol caches before measuring.
        for silo in SILOS:
            symbol = SYMBOLS[silo]
            for index in range(20):
                sequences[symbol] += 1
                ts = base + index * MS
                kind = "l2" if index % 2 == 0 else "trade"
                multiplexers[silo].ingest_frame(
                    _frame(kind, symbol, sequences[symbol], ts, 100.0 + index * 0.01),
                    now_ns=ts,
                )
        for mux in multiplexers.values():
            mux.reset_latencies()
        warmup_cursor = ring.cursor

        # 10,000 frames: 2,500 per silo, alternating L2 and executed trades.
        # The Ring 1 hot path runs with GC disabled; the per-frame work is
        # allocation-light and the p99 (not every individual frame) owns the
        # 0.05 ms SLA.
        total_frames = 10_000
        per_silo = total_frames // len(SILOS)
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            for silo in SILOS:
                mux = multiplexers[silo]
                symbol = SYMBOLS[silo]
                for index in range(per_silo):
                    sequences[symbol] += 1
                    ts = base + (100 + index) * MS
                    kind = "l2" if index % 2 == 0 else "trade"
                    price = 100.0 + (index % 17) * 0.01
                    result = mux.ingest_frame(
                        _frame(kind, symbol, sequences[symbol], ts, price),
                        now_ns=ts,
                    )
                    assert result.accepted is True, result
        finally:
            if gc_was_enabled:
                gc.enable()

        for silo, mux in multiplexers.items():
            stats = mux.latency_stats()
            assert stats["count"] == float(per_silo)
            # Per-silo p95 guards each worker; the 10,000-frame combined p99
            # owns the 0.05 ms SLA (a single host-scheduling outlier must not
            # masquerade as a normalizer regression).
            assert stats["p95_ms"] < 0.05, (silo, stats)
            snapshot = book.read_snapshot(SYMBOLS[silo])
            assert snapshot is not None
            assert snapshot.best_bid > 0.0 and snapshot.best_ask > snapshot.best_bid

        combined = [
            latency
            for mux in multiplexers.values()
            for latency in mux.latencies_ms
        ]
        assert len(combined) == 10_000
        assert _percentile(combined, 0.99) < 0.05, _percentile(combined, 0.99)

        # 5,000 executed prints landed in the OFI ring.
        assert ring.cursor - warmup_cursor == 5_000
        latest = ring.latest()
        assert latest is not None and latest.symbol in SYMBOLS.values()

        # ---- lag gate (> 5,000 ms) -------------------------------------
        silo = "CRYPTO_BINANCE"
        mux = multiplexers[silo]
        symbol = SYMBOLS[silo]
        sequences[symbol] += 1
        stale_ts = base + 3_000 * MS
        lagged = mux.ingest_frame(
            _frame("trade", symbol, sequences[symbol], stale_ts - 6_000 * MS, 100.0),
            now_ns=stale_ts,
        )
        assert lagged.accepted is False
        assert lagged.reason == "FEED_LAG_EXCEEDED"
        assert lagged.state == StreamState.RECONNECTING.value
        assert mux.health()["drops"]["FEED_LAG_EXCEEDED"] >= 1

        # ---- exponential backoff while the stream is down --------------
        health = mux.health()["streams"][symbol]
        first_backoff = health["backoff_ms"]
        assert first_backoff == pytest.approx(25.0)
        sequences[symbol] += 1
        second = mux.ingest_frame(
            _frame("l2", symbol, sequences[symbol], stale_ts + MS, 100.0),
            now_ns=stale_ts + MS,
        )
        assert second.accepted is False
        second_backoff = mux.health()["streams"][symbol]["backoff_ms"]
        assert second_backoff == pytest.approx(first_backoff * 2)
        sequences[symbol] += 1
        third = mux.ingest_frame(
            _frame("l2", symbol, sequences[symbol], stale_ts + 2 * MS, 100.0),
            now_ns=stale_ts + 2 * MS,
        )
        assert third.accepted is False
        third_backoff = mux.health()["streams"][symbol]["backoff_ms"]
        assert third_backoff == pytest.approx(second_backoff * 2)

        # ---- sequence gap after a clean reconnect ----------------------
        reconnect_at = mux.health()["streams"][symbol]["next_reconnect_ns"]
        assert mux.poll_reconnects(reconnect_at + 1) == (symbol,)
        health = mux.health()["streams"][symbol]
        assert health["state"] == StreamState.CONNECTED.value
        assert health["last_seq"] == -1

        baseline_ts = reconnect_at + 10 * MS
        sequences[symbol] += 1
        resumed = mux.ingest_frame(
            _frame("l2", symbol, sequences[symbol], baseline_ts, 100.0),
            now_ns=baseline_ts,
        )
        assert resumed.accepted is True

        gapped = mux.ingest_frame(
            _frame("l2", symbol, sequences[symbol] + 10, baseline_ts + MS, 100.0),
            now_ns=baseline_ts + MS,
        )
        assert gapped.accepted is False
        assert gapped.reason == "SEQUENCE_GAP"
        assert mux.health()["drops"]["SEQUENCE_GAP"] >= 1
        assert mux.health()["quarantine"], "quarantine deque must record the gap"

        # ---- ZeroGCSpinalEngine drains every tick ----------------------
        latch = GlobalKillLatch(reset=True)
        table = LeaseTableSHM(reset=True, kill_latch=latch)
        mono = time.monotonic_ns()
        for ticker in SYMBOLS.values():
            table.write_lease(
                System1Lease(
                    ticker,
                    LeaseMode.LONG_ONLY,
                    target_bps=12.0,
                    stop_bps=6.0,
                    expires_at_ns=mono + LEASE_TTL_NS,
                ),
                now_ns=mono,
            )
        engine = ZeroGCSpinalEngine(
            table, kill_latch=latch, silo="CRYPTO_BINANCE"
        )
        expected_ticks = ring.cursor
        outcomes = mux.drain_to(engine, now_ns=mono)
        assert len(outcomes) == expected_ticks
        assert all(isinstance(outcome, TickOutcome) for outcome in outcomes)
        assert engine.stats["ticks"] == expected_ticks
        # The cursor is monotonic: a second drain processes nothing new.
        assert mux.drain_to(engine, now_ns=mono) == []
    finally:
        book.close()
        ring.close()


def test_gate2_pool_creates_one_isolated_multiplexer_per_silo() -> None:
    pool = SiloMultiplexerPool(silos=SILOS)
    try:
        assert set(pool.multiplexers) == set(SILOS)
        assert len({id(mux.ofi_ring) for mux in pool.multiplexers.values()}) == 4
        assert len({id(mux.shadow_book) for mux in pool.multiplexers.values()}) == 4
        health = pool.health()
        assert set(health["multiplexers"]) == set(SILOS)
        assert health["p99_ms"] == 0.0
    finally:
        pool.close()


# ===========================================================================
# GATE 3 -- HTTP/2 multi-LLM switchboard & 250 ms breaker
# ===========================================================================


def _mock_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content) if request.content else {}
    model = str(body.get("model") or "")
    if model == "gemini-2.5-flash-lite":
        time.sleep(0.26)
    if request.url.path.endswith("/api/generate"):
        return httpx.Response(
            200,
            json={
                "response": json.dumps(
                    {
                        "mode": "LONG_ONLY",
                        "target_bps": 12.0,
                        "stop_bps": 6.0,
                        "confidence": 0.7,
                        "rationale": "executed buyers dominate",
                    }
                ),
                "prompt_eval_count": 120,
                "eval_count": 24,
            },
        )
    if request.url.path.endswith("/v1/messages"):
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "committee thesis"}],
                "usage": {"input_tokens": 2_000, "output_tokens": 500},
            },
        )
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"mode": "LONG_ONLY", "target_bps": 10.0, "stop_bps": 5.0}
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 800, "completion_tokens": 200},
        },
    )


def _api_keys() -> dict[str, str]:
    return {
        "gemini-2.5-flash-lite": "test-gemini",
        "groq": "test-groq",
        "deepseek-r1": "test-deepseek",
        "claude-sonnet": "test-claude",
        "claude-opus": "test-claude",
        "openrouter": "test-openrouter",
    }


def test_gate3_http2_pool_routes_every_provider_and_trips_breaker() -> None:
    ledger = TokenBudgetLedger(daily_usd_cap=DEFAULT_DAILY_USD_CAP)
    pool = LLMHttp2TransportPool(
        ledger=ledger,
        transport=httpx.MockTransport(_mock_handler),
        api_keys=_api_keys(),
    )
    try:
        assert pool.http2_enabled is True
        assert pool.keepalive["max_keepalive_connections"] >= 4
        assert pool.sync_client is pool.sync_client

        # System 1 local + cloud, then System 2 providers.
        for provider in ("ollama-qwen2.5-3b", "ollama-llama3.2-3b", "groq"):
            completion = pool.complete(provider, "instinct prompt")
            assert completion.system == 1
            assert completion.text
            assert completion.prompt_tokens > 0
            assert completion.completion_tokens > 0
            if provider.startswith("ollama"):
                assert completion.usd_cost == 0.0
            else:
                assert completion.usd_cost > 0.0

        for provider in ("deepseek-r1", "claude-sonnet", "claude-opus", "openrouter"):
            completion = pool.complete(provider, "committee prompt")
            assert completion.system == 2
            assert completion.text
            assert completion.usd_cost > 0.0
            assert completion.reasoning_effort

        # A 260 ms System 1 response must trip the 250 ms breaker.
        with pytest.raises(ProviderTimeout):
            pool.complete("gemini-2.5-flash-lite", "instinct prompt")
        assert pool.is_demoted("gemini-2.5-flash-lite") is True
        status = pool.status()
        assert "gemini-2.5-flash-lite" in status["demoted_providers"]
        assert "hard-cap" in status["demotion_reasons"]["gemini-2.5-flash-lite"]
        assert pool.active_provider(1) != "gemini-2.5-flash-lite"

        # A demoted provider is skipped, not retried on the next call.
        with pytest.raises(Exception):
            pool.complete("gemini-2.5-flash-lite", "instinct prompt")

        # The pool-backed switchboard still routes a healthy local provider.
        board = MultiLLMSwitchboard(
            transport_pool=pool, ledger=ledger, auto_probe=True
        )
        context = InstinctContext(
            symbol="AAPL",
            realized_vol_1m=0.01,
            normal_vol_1m=0.01,
            spread_bps=3.0,
            taker_buy_ratio=0.8,
        )
        decision = board.route(context)
        assert decision.used_fallback is False
        assert decision.provider_name.startswith("ollama-")
        assert decision.suggestion is not None
        assert decision.suggestion.mode is LeaseMode.LONG_ONLY
        assert ledger.snapshot()["active_system1_provider"] == decision.provider_name
    finally:
        pool.close()


def test_gate3_slow_switchboard_still_meets_lease_sla() -> None:
    def slow_probe(context: InstinctContext) -> ProviderResponse:  # noqa: ARG001
        time.sleep(0.30)
        return ProviderResponse(LeaseMode.LONG_ONLY, 20.0, 8.0, 0.9, "slow")

    ledger = TokenBudgetLedger(daily_usd_cap=DEFAULT_DAILY_USD_CAP)
    board = MultiLLMSwitchboard(ledger=ledger)
    for provider in board.providers:
        board.install_probe(provider.name, slow_probe)

    latch = GlobalKillLatch(reset=True)
    table = LeaseTableSHM(reset=True, kill_latch=latch)
    try:
        controller = System1InstinctController(
            board, table, kill_latch=latch, ledger=ledger
        )
        context = InstinctContext(
            symbol="AAPL",
            realized_vol_1m=0.01,
            normal_vol_1m=0.01,
            spread_bps=3.0,
            taker_buy_ratio=0.8,
        )
        now = time.monotonic_ns()
        latencies: list[float] = []
        for _ in range(200):
            decision = controller.evaluate(context, now_ns=now)
            latencies.append(decision.latency_ms)

        p99 = _percentile(latencies, 0.99)
        assert p99 < P99_SLA_MS, f"lease refresh p99 {p99:.3f} ms exceeds 78 ms"
        assert all(decision.used_fallback for decision in controller.decisions)
        assert all(
            decision.provider_name == "deterministic-local"
            for decision in controller.decisions
        )
        assert ledger.snapshot()["active_system1_provider"] == "deterministic-local"

        lease = table.read_lease("AAPL", now_ns=now)
        assert lease.mode is LeaseMode.LONG_ONLY
        assert lease.is_active(now)
        assert 0 < lease.expires_at_ns - now <= LEASE_TTL_NS
        assert controller.llm_state()["budget"]["daily_cap_usd"] == pytest.approx(
            DEFAULT_DAILY_USD_CAP
        )
    finally:
        table.close()


# ===========================================================================
# GATE 4 -- $3.10/day cap & cockpit telemetry
# ===========================================================================


def _simulate_top_one_percent(ledger: TokenBudgetLedger) -> None:
    """System 1 chatter plus the top-1% System 2 committee load."""
    for _ in range(100):
        if ledger.can_spend("gemini-2.5-flash-lite", 1):
            ledger.record(
                system=1,
                provider="gemini-2.5-flash-lite",
                model="gemini-2.5-flash-lite",
                prompt_tokens=500,
                completion_tokens=100,
                input_usd_per_mtok=0.10,
                output_usd_per_mtok=0.40,
            )
    for _ in range(50):
        if not ledger.can_spend("claude-sonnet", 2):
            break
        ledger.record(
            system=2,
            provider="claude-sonnet",
            model="claude-sonnet-4-5",
            prompt_tokens=8_000,
            completion_tokens=4_000,
            input_usd_per_mtok=3.00,
            output_usd_per_mtok=15.00,
        )


def test_gate4_token_budget_ledger_blocks_paid_cloud_at_cap() -> None:
    assert DEFAULT_DAILY_USD_CAP == pytest.approx(3.10)
    assert BASELINE_USD_PER_DAY == pytest.approx(86.40)

    ledger = TokenBudgetLedger(daily_usd_cap=DEFAULT_DAILY_USD_CAP)
    _simulate_top_one_percent(ledger)
    snapshot = ledger.snapshot()

    assert snapshot["daily_cap_usd"] == pytest.approx(3.10)
    assert snapshot["daily_spend_usd"] > 3.10
    assert snapshot["locked"] is True
    assert snapshot["locked_reason"]
    assert set(snapshot["locked_providers"]) == {
        "gemini-2.5-flash-lite",
        "groq",
        "deepseek-r1",
        "claude-sonnet",
        "claude-opus",
        "openrouter",
    }
    assert snapshot["cost_reduction_pct"] == pytest.approx(96.412, abs=0.01)
    assert snapshot["active_system1_provider"] == "ollama-qwen2.5-3b"
    assert snapshot["active_system2_provider"] == "deterministic-local"
    assert snapshot["prompt_tokens"] > 0
    assert snapshot["completion_tokens"] > 0
    assert snapshot["total_tokens"] == (
        snapshot["prompt_tokens"] + snapshot["completion_tokens"]
    )
    assert "claude-sonnet" in snapshot["by_provider"]

    # Paid cloud is blocked; local inference is always available at $0.00.
    assert ledger.can_spend("claude-sonnet", 2) is False
    assert ledger.can_spend("gemini-2.5-flash-lite", 1) is False
    assert ledger.can_spend("ollama-qwen2.5-3b", 1) is True
    assert ledger.can_spend("deterministic-local", 2) is True

    settled = ledger.record(
        system=1,
        provider="ollama-qwen2.5-3b",
        prompt_tokens=200,
        completion_tokens=40,
        usd_cost=0.0,
    )
    assert settled["usd_cost"] == 0.0
    assert settled["locked"] is True

    # Raising the cap unlocks; lowering it below spend re-locks immediately.
    ledger.set_daily_cap(5.00)
    assert ledger.snapshot()["locked"] is False
    assert ledger.can_spend("claude-sonnet", 2) is True
    ledger.set_daily_cap(3.10)
    assert ledger.snapshot()["locked"] is True
    assert ledger.can_spend("claude-sonnet", 2) is False


def test_gate4_cockpit_exposes_and_hot_swaps_the_budget(tmp_path: Path) -> None:
    runtime = NIOSCoreOrchestrator(
        workspace=tmp_path / "runtime",
        key_vault_root=tmp_path / "keys",
        seed_fills=60,
        funding_micros=5_000_000_000_000,
        daily_loss_stop_micros=5_000_000_000,
        llm_budget_usd=3.10,
    )
    try:
        # The orchestrator wires one ledger through System 1, System 2 and the
        # cockpit, so there is exactly one authority for the daily cap.
        assert runtime.system1.ledger is runtime.token_ledger
        assert runtime.committee.token_ledger is runtime.token_ledger
        assert runtime.control.token_ledger is runtime.token_ledger

        _simulate_top_one_percent(runtime.token_ledger)
        client = TestClient(runtime.control_app())

        state = client.get("/api/v2/cockpit/state").json()
        llm = state["llm"]
        for key in (
            "daily_spend_usd",
            "daily_cap_usd",
            "cost_reduction_pct",
            "active_system1_provider",
            "active_system2_provider",
            "demoted_providers",
            "locked_providers",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            assert key in llm, key
        assert llm["daily_cap_usd"] == pytest.approx(3.10)
        assert llm["daily_spend_usd"] > 3.10
        assert llm["cost_reduction_pct"] == pytest.approx(96.412, abs=0.01)
        assert llm["active_system1_provider"] == "ollama-qwen2.5-3b"
        assert llm["active_system2_provider"] == "deterministic-local"
        assert llm["locked"] is True

        # Hot-swap the cap upward through the operator endpoint.
        raised = client.post(
            "/api/v2/control/llms",
            json={"system": 2, "daily_spend_cap_usd": 5.00},
        )
        assert raised.status_code == 200, raised.text
        budget = raised.json()["budget"]
        assert budget["daily_cap_usd"] == pytest.approx(5.00)
        assert budget["locked"] is False
        assert budget["daily_spend_usd"] == pytest.approx(
            llm["daily_spend_usd"], abs=1e-6
        )

        state = client.get("/api/v2/cockpit/state").json()["llm"]
        assert state["daily_cap_usd"] == pytest.approx(5.00)
        assert state["locked"] is False
        assert state["active_system2_provider"] == "deepseek-r1"

        # Lowering it back below the day's spend re-locks every paid endpoint.
        lowered = client.post(
            "/api/v2/control/llms",
            json={"system": 2, "daily_spend_cap_usd": 3.10},
        )
        assert lowered.status_code == 200, lowered.text
        assert lowered.json()["budget"]["locked"] is True
        assert lowered.json()["budget"]["daily_cap_usd"] == pytest.approx(3.10)
    finally:
        runtime.close()


# ===========================================================================
# GATE 5 -- 4-ring air-gap AST & full-system regression
# ===========================================================================


FORBIDDEN_RING1_ROOTS = frozenset(
    {
        "ring2_brain",
        "ring0_vault",
        "httpx",
        "openai",
        "anthropic",
        "ollama",
        "groq",
        "gemini",
        "langchain",
        "langgraph",
        "key_vault",
    }
)
FORBIDDEN_RING2_ROOTS = frozenset(
    {
        "ring0_vault",
        "key_vault",
        "keyvault",
        "broker_keys",
        "keystore",
        "nautilus_adapters",
        "india_brokers",
        "fx_mt5",
    }
)
FORBIDDEN_RING2_TOKENS = frozenset(
    {
        "nautilus_adapters",
        "india_brokers",
        "fx_mt5",
        "key_vault",
        "broker_keys_db",
    }
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_gate5_ws_multiplexer_has_zero_ring2_or_llm_imports() -> None:
    path = ROOT / "ring1_spinal" / "ws_multiplexer.py"
    assert path.exists()
    for module in _imported_modules(path):
        root = module.split(".")[0]
        assert root not in FORBIDDEN_RING1_ROOTS, f"{module!r} violates the Ring 1 air-gap"
        assert "ring2_brain" not in module
        assert "ring0_vault" not in module


def test_gate5_ring1_never_imports_ring2() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "ring1_spinal").rglob("*.py")):
        for module in _imported_modules(path):
            if module.startswith("ring2_brain"):
                offenders.append(f"{path.relative_to(ROOT)} -> {module}")
    assert offenders == [], offenders


def test_gate5_ring2_never_imports_ring0_or_a_broker_adapter() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "ring2_brain").rglob("*.py")):
        for module in _imported_modules(path):
            parts = set(module.split("."))
            if module.startswith("ring0_vault") or (parts & FORBIDDEN_RING2_TOKENS):
                offenders.append(f"{path.relative_to(ROOT)} -> {module}")
            if parts & FORBIDDEN_RING2_ROOTS:
                offenders.append(f"{path.relative_to(ROOT)} -> {module}")
    assert offenders == [], offenders


def test_gate5_all_prior_suites_are_present_for_full_regression() -> None:
    expected = [
        "test_step2_core_spine.py",
        "test_step3_truth_and_quant.py",
        "test_step4_ring2_cognition.py",
        "test_step5_ring1_spinal.py",
        "test_step6_ring0_vault.py",
        "test_step7_cockpit_and_e2e.py",
        "test_step8_forge_mcp_and_deploy.py",
        "test_phase2_step1_donor_activation.py",
        "test_phase2_step2_silo_adapters.py",
        "test_phase2_step3_feeds_and_llm_pool.py",
    ]
    for name in expected:
        assert (ROOT / "tests" / name).exists(), name

    # The shared transport lives in the substrate; Ring 2's shim must not
    # re-introduce a socket client into the Ring 2 tree.
    shim = ROOT / "ring2_brain" / "cognition" / "llm_transport.py"
    assert shim.exists()
    for module in _imported_modules(shim):
        assert module.split(".")[0] not in {"httpx", "openai", "anthropic"}
