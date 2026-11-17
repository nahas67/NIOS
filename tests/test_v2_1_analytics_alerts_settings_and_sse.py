"""NIOS V2.1 -- Suite 35: ``G7`` SSE topics, ``G8`` analytics, ``G9`` alerts, ``G10`` settings.

Covers:

* ``G7`` -- the multiplexed stream advertises and emits the seven closed-loop frames
  alongside the five V1.8 tape frames, supports ``?topic=`` / ``?silo=`` filtering,
  rejects an unknown topic with a clean ``422``, and surfaces an out-of-band event
  before the synthetic cycle.
* ``G8`` -- ``GET /api/v2/analytics/performance``: NAV equity curve, underwater
  drawdown, Sharpe / Sortino / Calmar, win rate, profit factor and per-silo /
  per-tranche attribution, all over integer-micro rows.
* ``G9`` -- ``GET/POST /api/v2/alerts``: raise, list, filter, acknowledge and 404.
* ``G10`` -- ``GET/POST /api/v2/settings``: the offline default document, a validated
  patch, journal persistence, the live-critical tier carve-out and typed rejections.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.runtime_settings import (  # noqa: E402
    CONNECTOR_MODES,
    DEFAULT_RUNTIME_SETTINGS,
    NiosRuntimeSettings,
    RuntimeSettingsStore,
)
from ring0_vault.ibor.state_journal import AtomicStateJournal  # noqa: E402
from ring2_brain.quant.performance_analytics import (  # noqa: E402
    BUCKETS,
    PerformanceAnalytics,
    PerformanceRow,
)

DAY_NS = 86_400_000_000_000
SECOND_NS = 1_000_000_000
EPOCH = 1_763_251_200 * SECOND_NS


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime(tmp_path):
    from core.orchestrator import NIOSCoreOrchestrator

    workspace = tmp_path / "workspace"
    vault = tmp_path / "vault"
    workspace.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    instance = NIOSCoreOrchestrator(
        workspace=workspace, key_vault_root=vault, clean_slate=True
    )
    try:
        yield instance
    finally:
        try:
            instance.control.reset_workspace()
        except Exception:
            pass
        closer = getattr(instance, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


@pytest.fixture()
def client(runtime):
    from fastapi.testclient import TestClient

    from ring3_cockpit.gateway.control_plane import create_app

    with TestClient(create_app(runtime.control)) as test_client:
        yield test_client


def _logbook(client, *, silo: str, entry: float, exit_: float, qty: float) -> None:
    posted = client.post(
        "/api/v2/control/logbook/entry",
        json={
            "silo": silo,
            "symbol": "NVDA" if silo == "US_IBKR" else "BTCUSDT",
            "side": "LONG",
            "entry_price": entry,
            "exit_price": exit_,
            "quantity": qty,
            "stop_loss": entry * 0.98,
            "setup_strategy": "v21-suite",
            "thesis": "suite 35",
            "lessons_learned": "",
        },
    )
    assert posted.status_code == 200, posted.text


# ---------------------------------------------------------------------------
# G7 -- the extended multiplexed stream
# ---------------------------------------------------------------------------


CLOSED_LOOP_FRAMES = (
    "MISSION_PROGRESS",
    "TRANCHE_HALT",
    "WALLET_SWEEP",
    "CONCEPT_DRIFT_ALERT",
    "COLD_START_RECON",
    "POST_MORTEM_COMPILED",
    "SYSTEM_ALERT",
)


def test_stream_snapshot_advertises_the_closed_loop_frames(client) -> None:
    body = client.get("/api/v2/stream/snapshot").json()
    # The V1.8 five keep their exact contract ...
    assert body["event_frames"] == [
        "TICK_UPDATE",
        "L2_DEPTH_DELTA",
        "ORDER_LIFECYCLE",
        "RISK_HEARTBEAT",
        "TV_STUDY_SIGNAL",
    ]
    assert body["frame_count"] == 5
    # ... and the closed loop is additive, not a replacement.
    assert body["closed_loop_event_frames"] == list(CLOSED_LOOP_FRAMES)
    assert body["closed_loop_frame_count"] == 7
    assert body["all_event_frames"] == list(body["event_frames"]) + list(CLOSED_LOOP_FRAMES)
    assert body["closed_loop_schema_version"] == "v2.1.stream-hub"
    for entry in body["closed_loop_frames"]:
        assert entry["event_type"] in CLOSED_LOOP_FRAMES
        assert entry["frame"]["event_type"] == entry["event_type"]
        assert entry["frame"]["schema_version"] == "v2.1.stream-hub"
        assert len(entry["frame"]["payload_sha256"]) == 64


def test_stream_snapshot_topic_filter_narrows_to_one_frame(client) -> None:
    body = client.get("/api/v2/stream/snapshot?topic=wallet_sweep").json()
    assert body["topic"] == "WALLET_SWEEP"
    assert body["closed_loop_event_frames"] == ["WALLET_SWEEP"]
    frame = body["closed_loop_frames"][0]["frame"]
    assert frame["from_wallet"] == "UMFUTURE_MAIN"
    assert frame["to_wallet"] == "MAIN"
    assert isinstance(frame["amount_micros"], int)


def test_stream_snapshot_rejects_an_unknown_topic(client) -> None:
    rejected = client.get("/api/v2/stream/snapshot?topic=NOPE")
    assert rejected.status_code == 422
    assert "UNKNOWN_TOPIC" in rejected.text


def test_typed_stream_emits_twelve_frames_per_cycle(client) -> None:
    with client.stream("GET", "/api/v2/stream?max_events=12&fps=120") as response:
        events = []
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: ") :])
    assert events == [
        "TICK_UPDATE",
        "L2_DEPTH_DELTA",
        "ORDER_LIFECYCLE",
        "RISK_HEARTBEAT",
        "TV_STUDY_SIGNAL",
        *CLOSED_LOOP_FRAMES,
    ]


def test_typed_stream_topic_filter_emits_only_that_frame(client) -> None:
    with client.stream(
        "GET", "/api/v2/stream?max_events=3&fps=120&topic=COLD_START_RECON"
    ) as response:
        events = []
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: ") :])
    assert events == ["COLD_START_RECON"] * 3


def test_published_event_leads_the_stream(client, runtime) -> None:
    published = runtime.control.publish_stream_event(
        "TRANCHE_HALT",
        {"silo": "US_IBKR", "symbol": "NVDA", "reason": "TRANCH_STOP_OUT"},
    )
    assert published["event_type"] == "TRANCHE_HALT"
    assert len(published["payload_sha256"]) == 64

    with client.stream("GET", "/api/v2/stream?max_events=1&fps=120") as response:
        payload = None
        for line in response.iter_lines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
    assert payload is not None
    assert payload["event_type"] == "TRANCHE_HALT"
    assert payload["silo"] == "US_IBKR"


def test_stream_silo_filter_is_still_validated(client) -> None:
    assert client.get("/api/v2/stream/snapshot?silo=MARS_EXCHANGE").status_code == 422


# ---------------------------------------------------------------------------
# G8 -- performance analytics
# ---------------------------------------------------------------------------


def _rows() -> list[PerformanceRow]:
    """A deterministic ten-day NAV path with one deep drawdown."""
    base = EPOCH
    rows: list[PerformanceRow] = []
    nav = 1_000_000_000
    path = [40, 25, 10, -35, -60, -20, 15, 45, 70, 90]
    for index, pnl_units in enumerate(path):
        pnl = pnl_units * 1_000_000
        nav += pnl
        rows.append(
            PerformanceRow(
                ts_utc_ns=base + index * DAY_NS,
                nav_micros=nav,
                pnl_micros=pnl,
                silo="US_IBKR" if index % 2 == 0 else "CRYPTO_BINANCE",
                tranche_id=f"TRN-{index % 3}",
                mission_id="M-ALPHA",
                trade_id=f"T-{index:02d}",
            )
        )
    return rows


def test_analytics_computes_the_full_performance_document() -> None:
    report = PerformanceAnalytics().report(_rows(), bucket="1D")

    assert report["page"] == "analytics.performance"
    assert report["bucket"] == "1D"
    assert report["bucket_count"] == 10
    assert report["row_count"] == 10
    assert report["money_units"] == "integer_micros"
    assert len(report["report_sha256"]) == 64

    assert len(report["equity_curve"]) == 10
    assert report["first_nav_micros"] == 1_040_000_000
    assert report["last_nav_micros"] == 1_180_000_000
    assert report["total_return_pct"] > 0

    # One deep drawdown: -95 units from the 1_065 peak.
    assert report["max_drawdown_micros"] < 0
    assert report["max_drawdown_pct"] > 0
    underwater = report["underwater"]
    assert len(underwater) == 10
    assert min(point["drawdown_micros"] for point in underwater) == report["max_drawdown_micros"]
    assert all(point["peak_micros"] >= point["nav_micros"] for point in underwater)

    assert report["trades"] == 10
    assert report["wins"] == 7
    assert report["losses"] == 3
    assert report["win_rate_pct"] == 70.0
    assert report["gross_profit_micros"] == 295_000_000
    assert report["gross_loss_micros"] == 115_000_000
    assert report["profit_factor"] == round(295 / 115, 6)
    assert report["sharpe"] is not None
    assert report["sortino"] is not None
    assert report["calmar"] is not None or report["max_drawdown_pct"] > 0

    assert {row["key"] for row in report["by_silo"]} == {"US_IBKR", "CRYPTO_BINANCE"}
    assert sum(row["net_pnl_micros"] for row in report["by_silo"]) == 180_000_000
    assert len(report["by_tranche"]) == 3
    assert report["by_mission"][0]["key"] == "M-ALPHA"


def test_analytics_returns_none_rather_than_infinity_for_a_clean_series() -> None:
    rows = [
        PerformanceRow(ts_utc_ns=EPOCH + index * DAY_NS, nav_micros=1_000_000 + index, pnl_micros=1)
        for index in range(5)
    ]
    report = PerformanceAnalytics().report(rows)
    # A monotone series has no downside deviation and no drawdown: both ratios
    # must be reported as ``None``, never as ``inf``.
    assert report["sortino"] is None
    assert report["calmar"] is None
    assert report["profit_factor"] is None
    assert report["max_drawdown_micros"] == 0


def test_analytics_filters_by_silo_and_validates_the_bucket() -> None:
    analytics = PerformanceAnalytics()
    us = analytics.report(_rows(), silo="US_IBKR")
    assert us["row_count"] == 5
    assert {row["key"] for row in us["by_silo"]} == {"US_IBKR"}
    with pytest.raises(ValueError):
        analytics.report(_rows(), bucket="7m")
    assert list(BUCKETS) == ["1h", "1D", "1W"]


def test_analytics_rejects_float_money_rows() -> None:
    with pytest.raises(TypeError):
        PerformanceRow(ts_utc_ns=1, nav_micros=1.5)  # type: ignore[arg-type]


def test_performance_endpoint_serves_the_logbook(client) -> None:
    empty = client.get("/api/v2/analytics/performance")
    assert empty.status_code == 200, empty.text
    assert empty.json()["page"] == "analytics.performance"
    assert empty.json()["row_count"] == 0

    _logbook(client, silo="US_IBKR", entry=100.0, exit_=110.0, qty=10.0)
    _logbook(client, silo="CRYPTO_BINANCE", entry=64_000.0, exit_=63_000.0, qty=0.5)

    body = client.get("/api/v2/analytics/performance").json()
    assert body["row_count"] == 2
    assert body["trades"] == 2
    assert body["wins"] == 1
    assert body["losses"] == 1
    assert len(body["by_silo"]) == 2
    assert isinstance(body["sharpe"], (int, float)) or body["sharpe"] is None

    filtered = client.get("/api/v2/analytics/performance?silo=US_IBKR").json()
    assert filtered["row_count"] == 1
    assert filtered["by_silo"][0]["key"] == "US_IBKR"

    rejected = client.get("/api/v2/analytics/performance?bucket=7m")
    assert rejected.status_code == 422


# ---------------------------------------------------------------------------
# G9 -- alerts
# ---------------------------------------------------------------------------


def test_alert_raise_list_filter_and_ack(client) -> None:
    raised = client.post(
        "/api/v2/alerts",
        json={
            "code": "TRANCH_STOP_OUT",
            "severity": "CRITICAL",
            "silo": "US_IBKR",
            "message": "tranche stopped out",
            "detail": {"tranche_id": "TRN-1"},
        },
    )
    assert raised.status_code == 200, raised.text
    alert = raised.json()["alert"]
    assert alert["severity"] == "CRITICAL"
    assert alert["acknowledged"] is False
    assert len(alert["row_sha256"]) == 64

    listed = client.get("/api/v2/alerts").json()
    assert listed["count"] == 1
    assert listed["unacked_count"] == 1
    assert listed["severity_counts"]["CRITICAL"] == 1

    by_severity = client.get("/api/v2/alerts?severity=CRITICAL").json()
    assert by_severity["count"] == 1
    by_info = client.get("/api/v2/alerts?severity=INFO").json()
    assert by_info["count"] == 1  # INFO is the floor: CRITICAL outranks it

    acked = client.post(
        f"/api/v2/alerts/{alert['alert_id']}/ack",
        json={"operator": "sovereign", "note": "reviewed"},
    )
    assert acked.status_code == 200, acked.text
    assert acked.json()["alert"]["acknowledged"] is True
    assert acked.json()["alert"]["acknowledged_by"] == "sovereign"

    assert client.get("/api/v2/alerts?unacked_only=true").json()["count"] == 0
    assert client.get("/api/v2/alerts").json()["count"] == 1


def test_alert_repeat_increments_occurrences_instead_of_duplicating(client) -> None:
    for _ in range(3):
        client.post(
            "/api/v2/alerts",
            json={"code": "LISTEN_KEY_EXPIRED", "severity": "WARNING", "silo": "CRYPTO_BINANCE"},
        )
    listed = client.get("/api/v2/alerts").json()
    assert listed["count"] == 1
    assert listed["alerts"][0]["occurrences"] == 3


def test_alert_ack_unknown_is_a_404(client) -> None:
    missing = client.post("/api/v2/alerts/ALRT-NOPE/ack", json={})
    assert missing.status_code == 404


def test_alert_raise_rejects_an_unknown_severity(client) -> None:
    rejected = client.post(
        "/api/v2/alerts", json={"code": "X", "severity": "APOCALYPTIC"}
    )
    assert rejected.status_code == 422
    assert "UNKNOWN_SEVERITY" in rejected.text


def test_alert_severity_filter_rejects_a_bad_token(client) -> None:
    assert client.get("/api/v2/alerts?severity=NOPE").status_code == 422


# ---------------------------------------------------------------------------
# G10 -- runtime settings
# ---------------------------------------------------------------------------


def test_settings_default_document_is_offline_and_journal_backed(client) -> None:
    body = client.get("/api/v2/settings").json()
    assert body["connector_mode"] == "OFFLINE_REPLAY"
    assert body["allow_network"] is False
    assert body["live_armed"] is False
    assert body["journal_backed"] is True
    assert body["journal_key"] == "runtime_settings"
    assert list(body["modes"]) == list(CONNECTOR_MODES)
    assert body["autopilot_interval_s"] == DEFAULT_RUNTIME_SETTINGS["autopilot_interval_s"]
    assert body["max_drawdown_bps"] == DEFAULT_RUNTIME_SETTINGS["max_drawdown_bps"]
    assert body["default_reserve_floor_pct"] == 15
    assert body["default_tax_escrow_pct"] == 20
    assert sorted(body["live_critical_keys"]) == [
        "allow_network",
        "autopilot_interval_s",
        "connector_mode",
        "max_drawdown_bps",
    ]
    assert len(body["settings_sha256"]) == 64


def test_settings_patch_persists_and_reports_the_revision(client, runtime) -> None:
    updated = client.post(
        "/api/v2/settings",
        json={"autopilot_interval_s": 90, "max_drawdown_bps": 150.0},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["changed"]["autopilot_interval_s"] == {"from": 60, "to": 90}
    assert body["changed_count"] == 2
    assert body["persisted"] is True
    assert body["revision"] == 1

    assert client.get("/api/v2/settings").json()["autopilot_interval_s"] == 90

    # The journal is the source of truth, not the process memory.
    replayed = RuntimeSettingsStore(AtomicStateJournal(runtime.control.state_journal.path))
    assert replayed.settings.autopilot_interval_s == 90
    assert replayed.settings.max_drawdown_bps == 150.0
    revisions = replayed.revisions()
    assert len(revisions) == 1
    assert revisions[0]["operator"] == "ring3.operator"


def test_settings_connector_mode_round_trip_rebuilds_the_runtime(client, runtime) -> None:
    offline = runtime.control.connectors
    assert offline.connector_mode == "OFFLINE_REPLAY"

    live = client.post(
        "/api/v2/settings",
        json={"connector_mode": "LIVE_VENUE", "allow_network": True},
    )
    assert live.status_code == 200, live.text
    assert live.json()["connector_mode"] == "LIVE_VENUE"
    assert live.json()["live_armed"] is True

    rebuilt = runtime.control.connectors
    assert rebuilt is not offline
    assert rebuilt.connector_mode == "LIVE_VENUE"
    assert rebuilt.allow_network is True

    back = client.post(
        "/api/v2/settings",
        json={"connector_mode": "OFFLINE_REPLAY", "allow_network": False},
    )
    assert back.status_code == 200, back.text
    assert runtime.control.connectors.connector_mode == "OFFLINE_REPLAY"


def test_settings_accepts_a_sandbox_promotion(client, runtime) -> None:
    promoted = client.post(
        "/api/v2/settings",
        json={"connector_mode": "SANDBOX_TESTNET", "allow_network": True},
    )
    assert promoted.status_code == 200, promoted.text
    assert runtime.control.connectors.connector_mode == "SANDBOX_TESTNET"


def test_settings_rejects_a_live_mode_without_network(client) -> None:
    rejected = client.post("/api/v2/settings", json={"connector_mode": "LIVE_VENUE"})
    assert rejected.status_code == 422
    assert "LIVE_MODE_REQUIRES_ALLOW_NETWORK" in rejected.text


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown_knob": 1},
        {"autopilot_interval_s": 1},
        {"max_drawdown_bps": -5.0},
        {"connector_mode": "MOON_MODE"},
        {"default_tax_escrow_pct": 95},
        {},
    ],
)
def test_settings_rejects_bad_documents(client, patch) -> None:
    rejected = client.post("/api/v2/settings", json=patch)
    assert rejected.status_code == 422, rejected.text


def test_settings_store_validates_directly(tmp_path) -> None:
    store = RuntimeSettingsStore(AtomicStateJournal(tmp_path / "settings.json"))
    assert store.connector_mode == "OFFLINE_REPLAY"
    with pytest.raises(ValueError):
        store.update({"connector_mode": "NOPE"})
    with pytest.raises(ValueError):
        store.update({"nonsense": True})
    reset = store.reset(operator="sovereign")
    assert reset["connector_mode"] == "OFFLINE_REPLAY"
    assert reset["reset"] is True


def test_settings_model_rejects_a_reserve_plus_tax_over_ninety() -> None:
    with pytest.raises(Exception):
        NiosRuntimeSettings(default_reserve_floor_pct=80, default_tax_escrow_pct=20)


def test_lifecycle_and_connector_status_surfaces_are_served(client) -> None:
    lifecycle = client.get("/api/v2/lifecycle/status")
    assert lifecycle.status_code == 200, lifecycle.text
    assert lifecycle.json()["page"] == "lifecycle.status"
    assert lifecycle.json()["retry_delays_s"] == [1, 5, 10, 30, 60]

    connectors = client.get("/api/v2/connectors/status")
    assert connectors.status_code == 200, connectors.text
    assert connectors.json()["connector_mode"] == "OFFLINE_REPLAY"
    assert connectors.json()["air_gap"] is True

    orders = client.get("/api/v2/connectors/orders?silo=CRYPTO_BINANCE")
    assert orders.status_code == 200, orders.text
    assert orders.json()["count"] == 0

    positions = client.get("/api/v2/connectors/positions?silo=US_IBKR")
    assert positions.status_code == 200, positions.text
    assert positions.json()["count"] == 0

    balances = client.get("/api/v2/connectors/balances?silo=FX_MT5")
    assert balances.status_code == 200, balances.text

    rejected = client.get("/api/v2/connectors/orders?silo=MARS_EXCHANGE")
    assert rejected.status_code == 422


def test_lifecycle_tick_endpoint_runs_the_maintenance_pass(client) -> None:
    ticked = client.post(
        "/api/v2/control/lifecycle/tick", json={"now_utc_ns": EPOCH + 3 * 3600 * SECOND_NS}
    )
    assert ticked.status_code == 200, ticked.text
    body = ticked.json()
    assert body["page"] == "lifecycle.tick"
    assert body["jobs_run"] >= 1
    assert body["enabled"] is True


def test_market_tick_and_bars_endpoints_close_the_aggregator_loop(client) -> None:
    posted = client.post(
        "/api/v2/control/market/tick",
        json={
            "silo": "CRYPTO_BINANCE",
            "symbol": "BTC/USDT",
            "price": 64_000.5,
            "size": 0.5,
            "bid": 63_999.0,
            "ask": 64_002.0,
            "now_ns": EPOCH,
        },
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["price_micros"] == 64_000_500_000

    bars = client.get("/api/v2/market/bars?silo=CRYPTO_BINANCE&symbol=BTC/USDT").json()
    assert bars["count"] == 4
    assert bars["timeframes"] == ["1m", "5m", "15m", "1D"]

    one_minute = client.get(
        "/api/v2/market/bars?silo=CRYPTO_BINANCE&symbol=BTC/USDT&timeframe=1m"
    ).json()
    assert one_minute["count"] == 1
    assert one_minute["bars"][0]["open_micros"] == 64_000_500_000

    depth = client.get(
        "/api/v2/market/depth?silo=CRYPTO_BINANCE&symbol=BTC/USDT"
    ).json()
    assert depth["bids"][0]["price_micros"] == 63_999_000_000

    surface = client.get(
        "/api/v2/market/surface?silo=CRYPTO_BINANCE&symbol=BTC/USDT"
    ).json()
    assert surface["strike_count"] == 0

    bad = client.get("/api/v2/market/bars?timeframe=7m")
    assert bad.status_code == 422

    bad_silo = client.post(
        "/api/v2/control/market/tick",
        json={"silo": "MARS_EXCHANGE", "symbol": "X", "price": 1.0},
    )
    assert bad_silo.status_code == 422
