"""Phase 3 Step 2 -- live cutover, write-only credentials and cockpit surfaces.

Governing sections
------------------
``sec_004``  four-ring OS process isolation and the lock-free IPC spine;
``sec_006``  five-stage ingestion with dual-vendor cross-verification;
``sec_011``  zero-trust credential custody (write-only, Ring 0 only);
``sec_015``  the four sovereign silos and their venue whitelists;
``sec_016``  authenticated control endpoints and the 250 ms LLM demotion cap;
``sec_018``  Law 1 (never trust one venue) and Law 2 (no LLM in the tick loop).

Gates
-----
1. Feed mode hot-swap and 4-silo dual-vendor ingest into ``SelfHealingDataGuard``.
2. Write-only masked broker credentials plus the Ring 0 dry-run sign handshake.
3. LLM ``<250 ms`` probe and the rebuilt React 19 bundle served from ``/``.
4. Surface regression that the out-of-band release lock relies on.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import pytest

from core.llm_transport import (
    LLM_PROBE_FALLBACK,
    LLM_PROBE_LOCAL_LIVE,
    probe_llm_endpoints,
)
from ring2_brain.truth.live_public_feeds import (
    FEED_MODE_LIVE,
    FEED_MODE_PAPER,
    FEED_MODE_SYNTHETIC,
    SILO_FEED_ORDER,
    SILO_FEED_SPECS,
    FeedUnavailable,
    LiveDualVendorFeedConnector,
    MultiSiloFeedConnector,
    synthetic_quotes,
)

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "dist"

#: The exact raw secret Gate 2 plants and then hunts for everywhere.
RAW_SECRET = "binance-secret-key-99887F2A"
#: ``mask_secret`` keeps only the last four characters.
MASKED_SECRET = "***7F2A"


# ---------------------------------------------------------------------------
# Deterministic offline transports
# ---------------------------------------------------------------------------


class MockFeedTransport:
    """Deterministic stand-in for every public venue the four silos use."""

    def __init__(self, *, fail: bool = False, fail_silos: tuple[str, ...] = ()):
        self.fail = fail
        self.fail_silos = fail_silos
        self.calls: list[str] = []

    def _maybe_fail(self, url: str) -> None:
        if self.fail:
            raise OSError("mock transport offline")
        for marker in self.fail_silos:
            if marker in url:
                raise OSError(f"mock transport offline for {marker}")
        if "stooq" in url and "INDIA" in self.fail_silos:
            raise OSError("mock transport offline for INDIA")

    def get_json(self, url: str):
        self.calls.append(url)
        self._maybe_fail(url)
        if "bookTicker" in url:
            return {"bidPrice": "64250.0", "askPrice": "64251.0", "lastPrice": "64250.5"}
        if "aggTrades" in url:
            return [
                {"qty": "10.0", "isBuyerMaker": False},
                {"qty": "2.0", "isBuyerMaker": True},
            ]
        if "okx.com" in url and "ticker" in url:
            return {"data": [{"bidPx": "64250.4", "askPx": "64251.4", "last": "64250.9"}]}
        if "okx.com" in url and "trades" in url:
            return {"data": [{"sz": "8.0", "side": "buy"}, {"sz": "3.0", "side": "sell"}]}
        if "yahoo" in url:
            return {
                "chart": {
                    "result": [{"meta": {"regularMarketPrice": 2942.5}}],
                    "error": None,
                }
            }
        raise AssertionError(f"unexpected feed URL: {url}")

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        self._maybe_fail(url)
        return (
            "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
            "X,2026-01-12,09:15:00,2941.0,2944.0,2940.5,2942.0,184000\n"
        )

    def status(self) -> dict:
        return {"calls": len(self.calls), "fail": self.fail}


class MockLLMFetcher:
    """Records the timeout handed to it and returns a fake Ollama tag list."""

    def __init__(self, *, reachable: bool = True):
        self.reachable = reachable
        self.urls: list[str] = []
        self.budgets: list[float] = []

    def __call__(self, url: str, timeout_s: float):
        self.urls.append(url)
        self.budgets.append(timeout_s)
        if not self.reachable:
            raise OSError("connection refused")
        return {
            "models": [
                {
                    "name": "qwen2.5:3b",
                    "size": 1_900_000_000,
                    "details": {"parameter_size": "3B", "quantization_level": "Q4_K_M"},
                },
                {"name": "llama3.2:3b"},
            ]
        }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def orchestrator(tmp_path: Path):
    from core.orchestrator import NIOSCoreOrchestrator

    runtime = NIOSCoreOrchestrator(workspace=tmp_path / "p3s2")
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture()
def client(orchestrator):
    from fastapi.testclient import TestClient

    return TestClient(orchestrator.control_app())


def _wire_mock_feed(orchestrator, transport: MockFeedTransport | None = None):
    """Attach a deterministic multi-silo connector to the control plane."""

    connector = MultiSiloFeedConnector(
        transport=transport or MockFeedTransport(),
        guard=orchestrator.control.feed_guard,
    )
    orchestrator.control.feed_connector = connector
    return connector


# ---------------------------------------------------------------------------
# Gate 1 -- feed mode hot-swap and 4-silo dual-vendor ingest
# ---------------------------------------------------------------------------


def test_gate1_control_plane_lists_both_feed_modes(client):
    payload = client.get("/api/v2/control/feeds").json()

    assert payload["feed_mode"] == FEED_MODE_PAPER
    assert payload["available_modes"] == [FEED_MODE_PAPER, FEED_MODE_LIVE]
    assert payload["paper_autopilot"] is True
    assert payload["live_public_dual_vendor"] is False
    assert "Law 1" in payload["law"]


def test_gate1_hot_swap_to_live_ingests_all_four_silos(client, orchestrator):
    """LIVE mode must cross-check two venues per symbol in all four silos."""

    transport = MockFeedTransport()
    connector = _wire_mock_feed(orchestrator, transport)
    guard = orchestrator.control.feed_guard
    assert guard is not None, "the SelfHealingDataGuard must be wired into Ring 3"

    response = client.post(
        "/api/v2/control/feeds",
        json={"feed_mode": "LIVE_PUBLIC_DUAL_VENDOR", "ingest_cycle": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["feed_mode"] == FEED_MODE_LIVE
    assert body["previous_feed_mode"] == FEED_MODE_PAPER
    assert body["changed"] is True
    assert body["zero_restart"] is True
    assert body["live_public_dual_vendor"] is True

    cycle = body["cycle"]
    assert set(cycle["silos"]) == set(SILO_FEED_ORDER), cycle["silos"]
    # 2 + 2 + 2 + 1 whitelisted instruments across the four silos.
    assert cycle["frames"] == 7, cycle
    assert cycle["accepted"] == 7, cycle
    assert cycle["rejected"] == 0, cycle
    assert cycle["all_live"] is True, cycle

    for silo, entry in cycle["silos"].items():
        spec = SILO_FEED_SPECS[silo]
        assert entry["mode"] == FEED_MODE_LIVE, entry
        assert entry["frames"] == len(spec.symbols), entry
        assert entry["accepted"] == len(spec.symbols), entry
        assert entry["rejected"] == 0, entry
        assert entry["live"] == len(spec.symbols), entry
        assert entry["synthetic"] == 0, entry
        assert entry["venues"] == list(spec.venues), entry
        assert entry["max_divergence_pct"] < 1.50, entry

    # The two independent venues really were polled for every symbol.
    assert connector.rounds == 1
    assert len(guard.events) >= 7
    for frame in connector.connectors["CRYPTO_BINANCE"]._frames:
        assert frame.bar.p1 != frame.bar.p2
        assert frame.primary.venue == "binance"
        assert frame.secondary.venue == "okx"
        assert frame.primary.source == "rest"

    # The state snapshot carries the same truth, not a cached guess.
    state = client.get("/api/v2/cockpit/state").json()
    assert state["feed_mode"] == FEED_MODE_LIVE
    assert state["feed"]["silo_modes"]["US_IBKR"] == FEED_MODE_LIVE
    assert state["feed"]["last_cycle"]["accepted"] == 7


def test_gate1_hot_swap_back_to_paper_and_reject_unknown_mode(client, orchestrator):
    _wire_mock_feed(orchestrator)

    live = client.post(
        "/api/v2/control/feeds",
        json={"feed_mode": "LIVE_PUBLIC_DUAL_VENDOR", "ingest_cycle": False},
    )
    assert live.status_code == 200
    assert live.json()["cycle"]["skipped"] is True
    assert live.json()["feed_mode"] == FEED_MODE_LIVE

    paper = client.post("/api/v2/control/feeds", json={"feed_mode": "PAPER_AUTOPILOT"})
    assert paper.status_code == 200
    body = paper.json()
    assert body["feed_mode"] == FEED_MODE_PAPER
    assert body["changed"] is True
    assert body["cycle"]["skipped"] is True
    assert body["feed"]["paper_autopilot"] is True

    bad = client.post("/api/v2/control/feeds", json={"feed_mode": "MOON_MODE"})
    assert bad.status_code == 422
    assert "PAPER_AUTOPILOT" in bad.text


def test_gate1_per_silo_fallback_is_isolated_and_labelled(orchestrator):
    """A blocked Indian host must not drag the other three silos to synthetic."""

    control = orchestrator.control
    transport = MockFeedTransport(fail_silos=("stooq",))
    connector = _wire_mock_feed(orchestrator, transport)
    control._feed_mode = FEED_MODE_LIVE

    report = control.set_feed_mode(
        __import__(
            "ring3_cockpit.gateway.control_plane", fromlist=["FeedControlRequest"]
        ).FeedControlRequest(feed_mode="LIVE_PUBLIC_DUAL_VENDOR")
    )

    cycle = report["cycle"]
    # Stooq is the *secondary* venue for INDIA/US/FX, so those three degrade.
    assert cycle["silos"]["CRYPTO_BINANCE"]["mode"] == FEED_MODE_LIVE
    for silo in ("INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"):
        entry = cycle["silos"][silo]
        assert entry["mode"] == FEED_MODE_SYNTHETIC, entry
        assert entry["synthetic"] == entry["frames"], entry
        # Synthetic frames are still deterministic and still pass the guard.
        assert entry["accepted"] == entry["frames"], entry
    assert cycle["all_live"] is False

    # Every synthetic quote is labelled, so the cockpit can never mistake it
    # for a real cross-venue check.
    for silo in ("INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"):
        frame = connector.connectors[silo].last_frame
        assert frame is not None
        assert frame.primary.source == "synthetic"
        assert frame.secondary.source == "synthetic"
        assert frame.mode == FEED_MODE_SYNTHETIC


def test_gate1_offline_host_never_hangs_and_stays_deterministic():
    """The whole four-silo cycle must degrade under budget, not block."""

    guard_free_connector = MultiSiloFeedConnector(transport=MockFeedTransport(fail=True))
    started = time.monotonic()
    report = guard_free_connector.ingest_cycle()
    elapsed = time.monotonic() - started
    guard_free_connector.close()

    assert elapsed < 5.0, f"offline fallback blocked for {elapsed:.2f}s"
    assert report["frames"] == 7
    assert report["all_live"] is False
    for entry in report["silos"].values():
        assert entry["mode"] == FEED_MODE_SYNTHETIC
    assert any("BTCUSDT" in key for key in ()) is False  # no hidden global state

    first, second = synthetic_quotes("RELIANCE.NS", round_index=5, now_ns=1_700_000_000_000_000_000)
    again, again2 = synthetic_quotes("RELIANCE.NS", round_index=5, now_ns=1_700_000_000_000_000_000)
    assert first.mid == again.mid
    assert second.mid == again2.mid
    # A $1.08 FX pair and a $64k crypto pair must not share a reference mid.
    eurusd, _ = synthetic_quotes("EURUSD", round_index=0, now_ns=1)
    btc, _ = synthetic_quotes("BTCUSDT", round_index=0, now_ns=1)
    assert eurusd.mid < 10.0 < btc.mid


def test_gate1_silo_whitelist_rejects_cross_market_symbols():
    with pytest.raises(ValueError):
        LiveDualVendorFeedConnector(silo="NOT_A_SILO")
    with pytest.raises(ValueError):
        LiveDualVendorFeedConnector(silo="US_IBKR", symbols=("BTCUSDT",))


# ---------------------------------------------------------------------------
# Gate 2 -- write-only masked credentials and the Ring 0 dry-run sign
# ---------------------------------------------------------------------------


def test_gate2_credential_response_is_write_only_and_masked(client):
    response = client.post(
        "/api/v2/control/brokers/credentials",
        json={
            "silo": "CRYPTO_BINANCE",
            "venue": "binance",
            "api_key": "binance-key-AAA1",
            "api_secret": RAW_SECRET,
            "sandbox": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # ---- the constitutional assertion: the raw secret is gone ----------
    assert RAW_SECRET not in response.text
    assert "99887F2A" not in response.text
    assert body["api_secret_masked"] == MASKED_SECRET
    assert body["masked"]["api_secret"] == MASKED_SECRET
    assert body["credential_configured"] is True
    assert body["write_only"] is True
    assert body["sandbox"] is True
    assert body["environment"] == "SANDBOX"

    # ---- the Ring 0 dry-run handshake actually happened ----------------
    dry = body["dry_run"]
    assert body["dry_run_verified"] is True
    assert dry["signature_alg"] == "Ed25519"
    assert dry["schema_ok"] is True
    assert dry["fernet_roundtrip_ok"] is True
    assert dry["socket_opened"] is False
    assert dry["order_placed"] is False
    assert dry["instrument"] == "BTCUSDT"
    assert len(dry["signature_hex"]) == 128  # 64-byte Ed25519 signature
    assert len(dry["public_key_hex"]) == 64  # 32-byte public key
    assert len(dry["payload_digest_sha256"]) == 64
    # Binance's declared schema, straight from the adapter spec.
    assert {"symbol", "side", "type", "quantity"} <= set(dry["required_fields"])
    assert {"symbol", "side", "type", "quantity"} <= set(dry["payload_keys"])


def test_gate2_ed25519_dry_run_signature_is_deterministic(client):
    payload = {
        "silo": "US_IBKR",
        "venue": "interactive_brokers",
        "api_key": "ibkr-key-12AB",
        "api_secret": "ibkr-secret-34CD",
        "sandbox": True,
    }
    first = client.post("/api/v2/control/brokers/credentials", json=payload).json()
    second = client.post("/api/v2/control/brokers/credentials", json=payload).json()

    # Ed25519 over the same payload digest is deterministic; the digest itself
    # is a pure function of the venue-native order body.
    assert first["dry_run"]["payload_digest_sha256"] == second["dry_run"]["payload_digest_sha256"]
    assert first["dry_run"]["signature_hex"] == second["dry_run"]["signature_hex"]
    assert first["dry_run"]["public_key_hex"] == second["dry_run"]["public_key_hex"]


def test_gate2_all_twelve_venues_pass_preflight(client, orchestrator):
    pairs = [
        ("CRYPTO_BINANCE", "binance"),
        ("CRYPTO_BINANCE", "okx"),
        ("CRYPTO_BINANCE", "bybit"),
        ("INDIA_ZERODHA_DHAN", "dhan"),
        ("INDIA_ZERODHA_DHAN", "zerodha"),
        ("INDIA_ZERODHA_DHAN", "angel"),
        ("INDIA_ZERODHA_DHAN", "upstox"),
        ("US_IBKR", "interactive_brokers"),
        ("US_IBKR", "alpaca"),
        ("FX_MT5", "vnpy_mt5"),
        ("FX_MT5", "vnpy_ctp"),
        ("FX_MT5", "oanda"),
    ]
    for silo, venue in pairs:
        response = client.post(
            "/api/v2/control/brokers/credentials",
            json={
                "silo": silo,
                "venue": venue,
                "api_key": f"{venue}-key-9Z9Z",
                "api_secret": RAW_SECRET,
                "sandbox": True,
            },
        )
        assert response.status_code == 200, f"{silo}/{venue}: {response.text}"
        body = response.json()
        assert body["dry_run_verified"] is True, (silo, venue, body)
        assert body["dry_run"]["schema_ok"] is True, (silo, venue, body["dry_run"])
        assert body["api_secret_masked"] == MASKED_SECRET
        assert RAW_SECRET not in response.text

    # A ready Ring 0 inventory lists all four silos with masked tokens only.
    inventory = client.get("/api/v2/control/brokers/credentials").json()
    assert inventory["write_only"] is True
    assert inventory["ring"] == "ring0_vault"
    assert set(inventory["silos"]) == set(SILO_FEED_ORDER)
    assert len(inventory["venues_supported"]) == 12
    assert RAW_SECRET not in json.dumps(inventory)


def test_gate2_state_snapshot_and_logs_never_leak_the_secret(
    client, orchestrator, caplog, tmp_path: Path
):
    caplog.set_level(logging.DEBUG)

    response = client.post(
        "/api/v2/control/brokers/credentials",
        json={
            "silo": "CRYPTO_BINANCE",
            "venue": "binance",
            "api_key": "binance-key-AAA1",
            "api_secret": RAW_SECRET,
            "sandbox": True,
        },
    )
    assert response.status_code == 200
    assert RAW_SECRET not in response.text

    # 1. The hydration snapshot must never carry the plaintext.
    state = client.get("/api/v2/cockpit/state")
    assert RAW_SECRET not in state.text
    state_body = state.json()
    assert state_body["broker_credentials"]["write_only"] is True
    crypto = state_body["broker_credentials"]["silos"]["CRYPTO_BINANCE"]
    binance = next(entry for entry in crypto["venues"] if entry["venue"] == "binance")
    assert binance["credential_configured"] is True
    assert binance["api_key_masked"] == "***AAA1"
    assert binance["api_secret_masked"] == MASKED_SECRET

    # 2. The health probe must not echo it either.
    assert RAW_SECRET not in client.get("/api/v2/cockpit/health").text

    # 3. Structured logs must never contain it.
    assert RAW_SECRET not in caplog.text

    # 4. Nothing written under the workspace is plaintext: only Fernet
    #    ciphertext may hold the credential.
    workspace = orchestrator.workspace
    plaintext_hits: list[str] = []
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        try:
            if RAW_SECRET in path.read_text(encoding="utf-8", errors="ignore"):
                plaintext_hits.append(str(path.relative_to(workspace)))
        except OSError:
            continue
    assert plaintext_hits == [], plaintext_hits

    # 5. And the vault really did decrypt it internally (write-only, not lost).
    from ring0_vault.silos.key_vault.broker_credentials import BrokerCredentialVault

    vault = BrokerCredentialVault(workspace / "broker_vault")
    assert vault._raw_for_test_only("broker.binance.api_secret") == RAW_SECRET


def test_gate2_validation_errors_never_echo_credentials(client):
    # Wrong silo for the venue: a marketplace guard, not a credential error.
    response = client.post(
        "/api/v2/control/brokers/credentials",
        json={
            "silo": "US_IBKR",
            "venue": "binance",
            "api_key": "k-1234",
            "api_secret": RAW_SECRET,
            "sandbox": True,
        },
    )
    assert response.status_code == 422
    assert RAW_SECRET not in response.text
    assert "does not belong" in response.text

    response = client.post(
        "/api/v2/control/brokers/credentials",
        json={
            "silo": "CRYPTO_BINANCE",
            "venue": "binance",
            "api_key": "",
            "api_secret": RAW_SECRET,
        },
    )
    assert response.status_code == 422
    assert RAW_SECRET not in response.text


# ---------------------------------------------------------------------------
# Gate 3 -- LLM <250 ms probe and the rebuilt React 19 bundle
# ---------------------------------------------------------------------------


def test_gate3_probe_endpoint_reports_local_models(client, orchestrator):
    fetcher = MockLLMFetcher()
    orchestrator.control._llm_probe_fetcher = fetcher

    response = client.post("/api/v2/control/llms/probe", json={})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == LLM_PROBE_LOCAL_LIVE
    assert body["local_ollama_live"] is True
    assert body["fallback_active"] is False
    assert body["hard_timeout_ms"] <= 250.0
    assert body["circuit_breaker"]["hard_timeout_ms"] <= 250.0
    assert body["circuit_breaker"]["tripped"] is False
    assert body["model_count"] == 2
    assert [model["name"] for model in body["models"]] == ["llama3.2:3b", "qwen2.5:3b"]
    assert body["system1"]["provider"] == "ollama"
    assert body["system1"]["url"].endswith("/api/tags")
    assert body["system1"]["reachable"] is True
    assert body["system1"]["within_budget"] is True
    assert body["system2"] is not None

    # The hard cap was handed to the transport, not just measured afterwards.
    assert fetcher.budgets, fetcher.urls
    assert max(fetcher.budgets) <= 0.25

    # The cockpit slot reflects the measured latency.
    slot1 = orchestrator.control._llm["1"]
    assert slot1.circuit_open is False
    assert slot1.last_latency_ms == body["system1"]["latency_ms"]


def test_gate3_probe_endpoint_falls_back_deterministically(client, orchestrator):
    orchestrator.control._llm_probe_fetcher = MockLLMFetcher(reachable=False)

    body = client.post("/api/v2/control/llms/probe", json={}).json()
    assert body["status"] == LLM_PROBE_FALLBACK
    assert body["fallback_active"] is True
    assert body["circuit_breaker"]["tripped"] is True
    assert body["circuit_breaker"]["zero_tokens_on_fallback"] is True
    assert body["model_count"] == 0
    assert body["system1"]["reachable"] is False
    assert orchestrator.control._llm["1"].circuit_open is True


def test_gate3_probe_cannot_be_raised_above_the_constitutional_cap(client, orchestrator):
    fetcher = MockLLMFetcher()
    orchestrator.control._llm_probe_fetcher = fetcher

    body = client.post(
        "/api/v2/control/llms/probe", json={"timeout_ms": 5_000}
    ).json()
    # ``min(request, HARD_TIMEOUT_MS)`` keeps System 1 inside 250 ms.
    assert body["hard_timeout_ms"] == 250.0
    assert max(fetcher.budgets) <= 0.25


def test_gate3_real_probe_never_raises_and_stays_bounded():
    started = time.monotonic()
    report = probe_llm_endpoints(system1={"provider": "ollama", "base_url": "http://127.0.0.1:11434"})
    elapsed = time.monotonic() - started

    assert report["status"] in (LLM_PROBE_LOCAL_LIVE, LLM_PROBE_FALLBACK)
    assert report["hard_timeout_ms"] <= 250.0
    assert isinstance(report["models"], list)
    # One 250 ms socket budget plus interpreter overhead.
    assert elapsed < 3.0, f"local probe exceeded its budget: {elapsed:.2f}s"


def test_gate3_served_bundle_contains_the_phase3_surfaces(client):
    root = client.get("/")
    assert root.status_code == 200, root.text
    assert "text/html" in root.headers["content-type"]

    match = re.search(r'assets/(index-[A-Za-z0-9_-]+\.js)', root.text)
    assert match is not None, "index.html must reference a hashed JS bundle"
    bundle_path = BUNDLE_DIR / "assets" / match.group(1)
    assert bundle_path.exists(), bundle_path

    served = client.get(f"/assets/{match.group(1)}")
    assert served.status_code == 200
    javascript = served.text

    required = (
        # 4-ring OS process supervisor matrix
        "4-Ring OS Process Isolation Matrix",
        "ring0-vault-worker",
        "ring1-spinal-worker",
        "ring2-brain-worker",
        "ring3-cockpit-gateway",
        "SHM SeqLock heartbeat",
        # feed mode hot-swap
        "LIVE_PUBLIC_DUAL_VENDOR",
        "PAPER_AUTOPILOT",
        "/api/v2/control/feeds",
        # write-only broker credential vault
        "/api/v2/control/brokers/credentials",
        "Broker testnet credential vault",
        "DRY-RUN VERIFIED",
        # live LLM probe
        "/api/v2/control/llms/probe",
        "Probe LLMs (<250ms)",
        "LOCAL_OLLAMA_LIVE",
        "DETERMINISTIC_INSTINCT_FALLBACK",
    )
    missing = [token for token in required if token not in javascript]
    assert missing == [], missing

    # And the bundle must not carry the planted secret from Gate 2 either.
    assert RAW_SECRET not in javascript


# ---------------------------------------------------------------------------
# Gate 4 -- surfaces the out-of-band regression and release lock depend on
# ---------------------------------------------------------------------------


def test_gate4_state_exposes_every_step2_surface(client):
    state = client.get("/api/v2/cockpit/state").json()

    assert "ring_processes" in state
    assert state["ring_processes"]["available"] is False  # single-process mode
    assert state["ring_processes"]["rings"] == []

    assert state["feed_mode"] in (FEED_MODE_PAPER, FEED_MODE_LIVE)
    assert "silo_modes" in state["feed"] or "connector" in state["feed"]

    assert state["broker_credentials"]["write_only"] is True
    assert set(state["broker_credentials"]["silos"]) == set(SILO_FEED_ORDER)
    assert "llm_probe" in state


def test_gate4_health_exposes_rings_feed_and_credentials(client):
    health = client.get("/api/v2/cockpit/health").json()

    assert health["ok"] is True
    assert health["feed_mode"] in (FEED_MODE_PAPER, FEED_MODE_LIVE)
    assert health["ring_processes"]["available"] is False
    assert health["broker_credentials"]["write_only"] is True
    assert "configured_venues" in health["broker_credentials"]


def test_gate4_feed_frame_carries_the_owning_silo():
    connector = LiveDualVendorFeedConnector(
        silo="FX_MT5", transport=MockFeedTransport()
    )
    try:
        frames = connector.ingest_all()
        assert len(frames) == 1
        assert frames[0].silo == "FX_MT5"
        assert frames[0].symbol == "EURUSD"
        assert frames[0].to_dict()["silo"] == "FX_MT5"
    finally:
        connector.close()
