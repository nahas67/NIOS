"""NIOS V2.3 -- Suite 40: gateway telemetry closure and the route-split UI seal.

Part 3 of V2.3 replaced six fields the cockpit could only render as ``—`` with
measured values, and Part 4 split the terminal into route-level chunks.  This
suite is the executable contract for both, and it deliberately proves
**movement**, not constants:

``latency_ms`` / per-ring ``p99``
    Every figure is a real ``time.perf_counter_ns()`` span.  A ring that has not
    been exercised reports ``None`` (never ``0.0``); after real work it reports a
    positive number.  ``latency_ring_samples`` must grow.
``feed_lag_ms``
    ``None`` + ``feed_lag_status="IDLE"`` before the first tick, then a real
    millisecond age + ``"LIVE"`` after one ingested bar.
``pending_intents_count``
    A real integer ``0`` when nothing is pending, and ``>= 1`` once a working
    paper order holds an outbox reservation.
``adwin_drift``
    Always a real object whose ``state`` is one of ``STABLE``/``HALVED``/
    ``PAUSED``; ``window_size`` is ``0`` and ``last_checked_utc`` is ``None``
    before any observation, and both move after a closed trade.
``iv_rank_bps``
    ``None`` with a named ``iv_rank_status`` until the trailing window holds
    three observations, then an int inside ``0..10000`` with a consistent
    ``iv_rank_detail`` window.

The UI half asserts the shipped artefact: at least two ``*.js`` chunks (route
splitting is real), **every** chunk below ``500 kB``, zero flat-matte violations
(``backdrop-filter`` / ``backdrop-blur`` / ``box-shadow`` / gradients /
``9999px`` / ``rounded-full``) and a LIVE analyzer that contains no shadow-burst
control at all.

If ``dist/`` has not been built, the artefact tests skip with an explicit reason
rather than pretending to pass; run ``npm run build`` in
``ring3_cockpit/ui/openalgo_shell`` to execute them.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"
DIST_ROOT = UI_ROOT / "dist"
SRC_ROOT = UI_ROOT / "src"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SUITE_NAME = Path(__file__).name
UNITS = 1_000_000
CHUNK_CEILING_BYTES = 500 * 1024
RING_P99_KEYS = ("ring0_p99_ms", "ring1_p99_ms", "ring2_p99_ms", "ring3_p99_ms")
FORBIDDEN_CSS = (
    "backdrop-filter",
    "backdrop-blur",
    "box-shadow",
    "linear-gradient",
    "radial-gradient",
    "9999px",
    "rounded-full",
)
RELEASE = "v4.3.0-V2.3"
DRIFT_STATES = ("STABLE", "HALVED", "PAUSED")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cockpit(tmp_path_factory: pytest.TempPathFactory):
    """One real FastAPI app on an isolated temp data root.

    The gateway telemetry tracker is a process singleton, so it is reset for this
    module: the "cold" assertions below must observe a genuinely unexercised
    tracker, not one an earlier suite already filled.
    """

    from fastapi.testclient import TestClient

    from core.orchestrator import NIOSCoreOrchestrator
    from core.telemetry import reset_gateway_telemetry
    from ring3_cockpit.gateway.control_plane import create_app

    root = tmp_path_factory.mktemp("v23-suite40")
    workspace = root / "workspace"
    vault = root / "vault"
    workspace.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)

    previous_journal = __import__("os").environ.get("NIOS_JOURNAL_PATH")
    __import__("os").environ["NIOS_JOURNAL_PATH"] = str(
        root / "nios_workstation_journal.json"
    )
    runtime = NIOSCoreOrchestrator(
        workspace=workspace, key_vault_root=vault, clean_slate=True
    )
    reset_gateway_telemetry()
    with TestClient(create_app(runtime.control)) as client:
        yield client, runtime
    try:
        runtime.control.reset_workspace()
    except Exception:  # pragma: no cover - teardown is best effort
        pass
    closer = getattr(runtime, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # pragma: no cover - teardown is best effort
            pass
    if previous_journal is not None:
        __import__("os").environ["NIOS_JOURNAL_PATH"] = previous_journal


def _status(client: Any) -> dict[str, Any]:
    response = client.get("/api/v2/status")
    assert response.status_code == 200, response.text
    return response.json()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _assert_measured(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    assert _is_number(value), f"{key} is not numeric: {value!r}"
    assert float(value) >= 0.0, f"{key} is negative: {value!r}"
    return float(value)


# ===========================================================================
# Gate 1 -- the intentional-null contract on a cold gateway
# ===========================================================================


def test_gate1_cold_status_respects_the_intentional_null_contract(cockpit: Any) -> None:
    """Before any activity the honest answer is ``null`` / ``IDLE`` / ``0``."""

    client, _runtime = cockpit
    body = _status(client)

    # -- the six fields are present ----------------------------------------
    for key in ("latency_ms", "latency_detail", *RING_P99_KEYS):
        assert key in body, key
    for key in (
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "latency_ring_samples",
        "latency_last_ms",
        "telemetry",
        "feed_lag_ms",
        "feed_lag_status",
        "feed_lag_by_silo",
        "feeds",
        "pending_intents_count",
        "pending_intents",
    ):
        assert key in body, key

    # -- Ring 3 is measured by the request that is answering --------------
    latency = _assert_measured(body, "latency_ms")
    assert latency >= 0.0
    detail = body["latency_detail"]
    assert detail["ring"] == 3
    assert int(detail["count"]) >= 1
    assert detail["last"] is not None
    assert detail["p50"] is not None
    assert detail["p95"] is not None
    assert detail["p99"] is not None
    assert detail["max"] is not None
    assert int(body["latency_ring_samples"]["ring3"]) >= 1
    _assert_measured(body, "ring3_p99_ms")

    # -- a ring nobody has exercised is ``null``, never ``0.0`` -----------
    for key in RING_P99_KEYS:
        value = body[key]
        assert value is None or (_is_number(value) and float(value) >= 0.0), key
        assert value != 0.0, f"{key} fabricated a zero instead of reporting null"

    # -- the nested snapshot carries the same schema ----------------------
    telemetry = body["telemetry"]
    assert telemetry["schema"] == "v2.3.gateway-telemetry"
    assert telemetry["sample_capacity"] == 512
    assert telemetry["latency_ms"] == body["latency_ms"]
    for ring in ("ring0", "ring1", "ring2", "ring3"):
        assert ring in telemetry["per_ring"]
        assert ring in telemetry["ring_samples"]
        assert ring in telemetry["latency_p50_ms"]
        assert ring in telemetry["latency_p95_ms"]
        assert ring in telemetry["latency_p99_ms"]

    # -- no tick has arrived, so the feed is IDLE with a null lag ---------
    assert body["feed_lag_ms"] is None
    assert body["feed_lag_status"] == "IDLE"
    assert body["feed_lag_by_silo"] == {}
    assert body["feeds"] == []

    # -- nothing is pending, and the answer is a real integer zero --------
    assert isinstance(body["pending_intents_count"], int)
    assert body["pending_intents_count"] == 0
    pending = body["pending_intents"]
    assert pending["outbox_pending"] == 0
    assert pending["pending_sweep_verification"] == 0
    assert pending["unknown_orders_active"] == 0
    assert len(pending["sources"]) == 3


def test_gate1_cold_iv_rank_is_null_with_a_named_status(cockpit: Any) -> None:
    """A one-sample IV window cannot produce a rank, and does not pretend to."""

    client, _runtime = cockpit
    detail = client.get("/api/v2/instruments/NIFTY")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert "iv_rank_bps" in body
    assert body["iv_rank_bps"] is None
    iv = body["iv_rank_detail"]
    assert iv["iv_rank_status"] in ("INSUFFICIENT_HISTORY", "NO_CURRENT_IV")
    assert iv["iv_rank_basis"] == "MIN_MAX_52W"
    assert iv["iv_window_seconds"] == 52 * 7 * 86_400
    assert iv["iv_window_samples"] < 3


def test_gate1_cold_forge_adwin_is_a_real_object_with_zero_window(cockpit: Any) -> None:
    """``adwin_drift`` is never ``null``; an unobserved monitor is ``STABLE``."""

    client, _runtime = cockpit
    forge = client.get("/api/v2/cockpit/forge")
    assert forge.status_code == 200, forge.text
    body = forge.json()

    drift = body["adwin_drift"]
    assert isinstance(drift, dict)
    assert drift["state"] in DRIFT_STATES
    assert drift["state"] == "STABLE"
    assert drift["drift_score"] == 0.0
    assert int(drift["window_size"]) == 0
    assert drift["last_checked_utc"] is None
    assert isinstance(body["adwin_drift_by_strategy"], dict)
    assert body["adwin_drift_by_strategy"]
    for row in body["leaderboard"]:
        assert isinstance(row["adwin_drift"], dict)
        assert row["adwin_drift"]["state"] in DRIFT_STATES


# ===========================================================================
# Gate 2 -- every field moves on real activity
# ===========================================================================


def test_gate2_feed_lag_goes_from_idle_to_live(cockpit: Any) -> None:
    client, _runtime = cockpit
    before = _status(client)
    assert before["feed_lag_ms"] is None
    assert before["feed_lag_status"] == "IDLE"

    tick = client.post(
        "/api/v2/control/market/tick",
        json={
            "silo": "CRYPTO_BINANCE",
            "symbol": "BTCUSDT",
            "price": 68_250.5,
            "size": 0.75,
        },
    )
    assert tick.status_code == 200, tick.text

    after = _status(client)
    assert after["feed_lag_status"] == "LIVE"
    lag = _assert_measured(after, "feed_lag_ms")
    # A live lag is a real age, not a sentinel; it may legitimately be < 1 ms.
    assert lag >= 0.0
    assert "CRYPTO_BINANCE" in after["feed_lag_by_silo"]
    assert after["feeds"]
    feed = after["feeds"][0]
    for key in ("silo", "symbol", "feed_lag_ms", "last_tick_utc_ns"):
        assert key in feed, key
    assert _is_number(feed["feed_lag_ms"]) and float(feed["feed_lag_ms"]) >= 0.0
    assert after["feed_instruments"] >= 1
    assert after["feed_ticks_ingested"] >= 1
    # Ingesting a tick IS Ring 2 work, so its reservoir is no longer empty.
    _assert_measured(after, "ring2_p99_ms")
    assert int(after["latency_ring_samples"]["ring2"]) >= 1


def test_gate2_pending_intents_count_a_working_paper_order(cockpit: Any) -> None:
    client, _runtime = cockpit
    assert _status(client)["pending_intents_count"] == 0

    funded = client.post(
        "/api/v2/control/ledger/fund-paper",
        json={
            "silo": "CRYPTO_BINANCE",
            "amount_units": 50_000.0,
            "vault": "SpinalScalper",
        },
    )
    assert funded.status_code == 200, funded.text

    order = client.post(
        "/api/v2/control/orders/paper",
        json={
            "silo": "CRYPTO_BINANCE",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": 0.01,
            "expected_gross_bps": 90.0,
            "order_style": "MAKER_PEG",
            "fill_immediately": False,
        },
    )
    assert order.status_code == 200, order.text

    body = _status(client)
    assert isinstance(body["pending_intents_count"], int)
    assert body["pending_intents_count"] >= 1
    assert body["pending_intents"]["outbox_pending"] >= 1
    # A working order is signed in Ring 0, so that reservoir is now populated.
    _assert_measured(body, "ring0_p99_ms")
    assert int(body["latency_ring_samples"]["ring0"]) >= 1


def test_gate2_ring1_latency_is_measured_from_shm_reads(cockpit: Any) -> None:
    """Ring 1 is real SHM/IPC work, so the status reader measures it."""

    client, _runtime = cockpit
    body = _status(client)
    assert int(body["latency_ring_samples"]["ring1"]) >= 1
    _assert_measured(body, "ring1_p99_ms")
    assert body["ring1_p99_ms"] == body["latency_p99_ms"]["ring1"]


def test_gate2_adwin_drift_window_moves_on_a_closed_trade(cockpit: Any) -> None:
    client, _runtime = cockpit
    before = client.get("/api/v2/cockpit/forge").json()["adwin_drift"]
    assert int(before["window_size"]) == 0

    closed = client.post(
        "/api/v2/control/trades/close",
        json={
            "silo": "CRYPTO_BINANCE",
            "symbol": "BTCUSDT",
            "gross_pnl_micros": 900_000,
            "notional_micros": 50_000_000,
            "side": "SELL",
        },
    )
    assert closed.status_code == 200, closed.text

    after = client.get("/api/v2/cockpit/forge").json()
    drift = after["adwin_drift"]
    assert int(drift["window_size"]) >= 1
    assert drift["window_size"] > before["window_size"]
    assert drift["last_checked_utc"] is not None
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", str(drift["last_checked_utc"]))
    assert drift["state"] in DRIFT_STATES
    assert _is_number(drift["drift_score"]) and float(drift["drift_score"]) >= 0.0
    assert int(drift["window_capacity"]) >= int(drift["window_size"])
    assert drift["severity"] in ("NONE", "HALVE", "PAUSE")

    by_strategy = after["adwin_drift_by_strategy"]
    assert by_strategy
    assert any(
        int(block.get("window_size") or 0) >= 1 for block in by_strategy.values()
    ), by_strategy
    for strategy_id, block in by_strategy.items():
        assert block["state"] in DRIFT_STATES, strategy_id
        assert _is_number(block["drift_score"]), strategy_id

    # A second close keeps moving the window rather than freezing at one.
    second = client.post(
        "/api/v2/control/trades/close",
        json={
            "silo": "CRYPTO_BINANCE",
            "symbol": "ETHUSDT",
            "gross_pnl_micros": -400_000,
            "notional_micros": 40_000_000,
            "side": "SELL",
        },
    )
    assert second.status_code == 200, second.text
    final = client.get("/api/v2/cockpit/forge").json()["adwin_drift"]
    assert int(final["window_size"]) > int(drift["window_size"])


def test_gate2_iv_rank_becomes_an_int_once_the_window_is_full(cockpit: Any) -> None:
    client, _runtime = cockpit

    ranks: list[dict[str, Any]] = []
    for volatility in (0.12, 0.45, 0.25):
        priced = client.post(
            "/api/v2/control/options/chain",
            json={"symbol": "NIFTY", "volatility": volatility},
        )
        assert priced.status_code == 200, priced.text
        ranks.append(priced.json())

    # The first chain has no history to rank against.
    assert ranks[0]["iv_rank_bps"] is None
    assert ranks[0]["iv_rank_detail"]["iv_rank_status"] in (
        "INSUFFICIENT_HISTORY",
        "NO_CURRENT_IV",
    )

    final = ranks[-1]
    rank = final["iv_rank_bps"]
    assert isinstance(rank, int) and not isinstance(rank, bool), rank
    assert 0 <= rank <= 10_000
    assert final["summary"]["iv_rank_bps"] == rank
    assert final["summary"]["iv_rank_status"] == "OK"

    detail = final["iv_rank_detail"]
    assert detail["iv_rank_status"] == "OK"
    assert detail["iv_rank_basis"] == "MIN_MAX_52W"
    assert detail["iv_window_seconds"] == 52 * 7 * 86_400
    assert int(detail["iv_window_samples"]) >= 3
    low = detail["iv_window_low"]
    high = detail["iv_window_high"]
    current = detail["iv_current"]
    assert _is_number(low) and _is_number(high) and _is_number(current)
    assert float(low) <= float(high)
    # The current observation was folded into the window before the rank was
    # computed, so it must sit inside the reported min-max bounds.
    assert float(low) <= float(current) <= float(high)
    # The reported rank is exactly the min-max rank of the reported window.
    span = float(high) - float(low)
    assert span > 0.0
    expected = int(((float(current) - float(low)) * 10_000) // span)
    assert abs(expected - rank) <= 1
    assert detail["iv_window_start_utc"] is not None
    assert detail["iv_window_end_utc"] is not None

    # The instrument detail route publishes the same measured rank.
    instrument = client.get("/api/v2/instruments/NIFTY")
    assert instrument.status_code == 200, instrument.text
    instrument_body = instrument.json()
    assert isinstance(instrument_body["iv_rank_bps"], int)
    assert 0 <= instrument_body["iv_rank_bps"] <= 10_000
    assert int(instrument_body["iv_rank_detail"]["iv_window_samples"]) >= 3


# ===========================================================================
# Gate 3 -- every documented route publishes the same block
# ===========================================================================


def test_gate3_overview_state_supervisor_and_metrics_carry_the_block(cockpit: Any) -> None:
    client, _runtime = cockpit

    # The three surfaces the spec names carry the complete six-field block.
    for path in ("/api/v2/cockpit/overview", "/api/v2/cockpit/state"):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.text[:200]}"
        body = response.json()
        for key in (
            "latency_ms",
            "latency_detail",
            *RING_P99_KEYS,
            "latency_p50_ms",
            "latency_p95_ms",
            "latency_ring_samples",
            "feed_lag_ms",
            "feed_lag_status",
            "feed_lag_by_silo",
            "feeds",
            "pending_intents_count",
            "pending_intents",
        ):
            assert key in body, f"{path} lacks {key}"
        assert _is_number(body["latency_ms"]), path
        assert body["feed_lag_status"] in ("IDLE", "LIVE"), path
        assert isinstance(body["pending_intents_count"], int), path

    # The OS supervisor answers "are the four rings alive?" and "how fast is
    # each one?" from the same tracker.  It publishes the latency block (not the
    # feed/pending/adwin surfaces, which belong to the market and ledger tiers).
    supervisor = client.get("/api/v2/control/supervisor")
    assert supervisor.status_code == 200, supervisor.text
    supervisor_body = supervisor.json()
    for key in (
        "latency_ms",
        *RING_P99_KEYS,
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_ring_samples",
        "rings",
        "workers",
    ):
        assert key in supervisor_body, key
    assert _is_number(supervisor_body["latency_ms"])

    overview = client.get("/api/v2/cockpit/overview").json()
    assert "supervisor" in overview
    # Ring 1/Ring 2/Ring 3 were all exercised by the tests above.
    for key in ("ring1_p99_ms", "ring2_p99_ms", "ring3_p99_ms"):
        assert overview[key] is not None, key

    metrics = client.get("/api/v2/metrics/json")
    assert metrics.status_code == 200, metrics.text
    payload = metrics.json()
    assert "gateway_telemetry" in payload
    telemetry = payload["gateway_telemetry"]
    assert telemetry["schema"] == "v2.3.gateway-telemetry"
    assert _is_number(telemetry["latency_ms"])
    for ring in ("ring0", "ring1", "ring2", "ring3"):
        assert int(telemetry["ring_samples"][ring]) >= 1, ring


def test_gate3_status_payload_is_enveloped_and_hashed(cockpit: Any) -> None:
    client, _runtime = cockpit
    body = _status(client)
    for key in ("schema_version", "generated_at_ns", "payload_sha256", "clean_slate_active"):
        assert key in body, key
    assert re.match(r"^[0-9a-f]{64}$", body["payload_sha256"])
    assert body["workspace_mode"] in ("PAPER", "LIVE")
    assert body["law"]


# ===========================================================================
# Gate 4 -- frontend artefact: real route splitting, zero flatness violations
# ===========================================================================


def _dist_assets() -> tuple[Path, ...]:
    return tuple(sorted((DIST_ROOT / "assets").glob("*")))


def _chunk_table() -> list[dict[str, Any]]:
    rows = []
    for path in _dist_assets():
        if path.suffix not in (".js", ".css"):
            continue
        rows.append({"name": path.name, "bytes": path.stat().st_size})
    return sorted(rows, key=lambda row: (-row["bytes"], row["name"]))


def test_gate4_bundle_is_route_split_and_under_the_chunk_ceiling() -> None:
    if not (DIST_ROOT / "assets").is_dir():
        pytest.skip("dist/assets is absent; run `npm run build` first")

    chunks = _chunk_table()
    javascript = [row for row in chunks if row["name"].endswith(".js")]
    assert len(javascript) >= 2, f"route splitting produced {len(javascript)} chunk(s)"
    # One chunk per workstation means a large, independently verifiable count.
    assert len(javascript) >= 17, [row["name"] for row in javascript]

    oversized = [
        (row["name"], row["bytes"]) for row in chunks if row["bytes"] >= CHUNK_CEILING_BYTES
    ]
    assert not oversized, f"chunk(s) over the 500 kB ceiling: {oversized}"

    biggest = chunks[0]
    assert biggest["bytes"] < CHUNK_CEILING_BYTES
    # A route chunk is genuinely smaller than the shared vendor/react entry.
    assert any(row["bytes"] < biggest["bytes"] // 2 for row in chunks)


def test_gate4_bundle_keeps_zero_forbidden_flat_matte_primitives() -> None:
    if not (DIST_ROOT / "assets").is_dir():
        pytest.skip("dist/assets is absent; run `npm run build` first")

    scanned = 0
    for path in _dist_assets():
        if path.suffix not in (".js", ".css"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for forbidden in FORBIDDEN_CSS:
            assert forbidden not in text, f"{path.name} contains {forbidden}"
        scanned += 1
    assert scanned >= 2

    document = (DIST_ROOT / "index.html").read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_CSS:
        assert forbidden not in document.lower(), forbidden
    assert (DIST_ROOT / "headless-manifest.json").is_file()


def test_gate4_release_marker_is_v2_3() -> None:
    if not DIST_ROOT.is_dir():
        pytest.skip("dist is absent; run `npm run build` first")

    document = (DIST_ROOT / "index.html").read_text(encoding="utf-8")
    assert f'content="{RELEASE}"' in document
    assert "nios-release" in document
    assert "nios-frontend" in document
    assert "V2.2-INSTITUTIONAL-TERMINAL" in document

    manifest = json.loads((DIST_ROOT / "headless-manifest.json").read_text(encoding="utf-8"))
    assert manifest["release"] == RELEASE
    assert manifest["schema_version"] == "v4.3.0.terminal"
    assert manifest["javascript"] >= 17
    assert manifest["routes"] == 56
    assert manifest["workstations"] == 17
    assert manifest["shells"] == ["PaperShell", "LiveShell"]

    # The authored document carries the same marker before the build.
    source_document = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    assert f'content="{RELEASE}"' in source_document


def test_gate4_source_carries_the_release_marker_and_no_legacy_string() -> None:
    for relative in (
        "index.html",
        "vite.config.ts",
        "src/shells/ShellFrame.tsx",
        "src/portals/PortalFrame.tsx",
    ):
        text = (UI_ROOT / relative).read_text(encoding="utf-8")
        assert RELEASE in text, relative
        assert "v4.2.0-V2.2" not in text, f"{relative} still pins the V2.2 release"


# ===========================================================================
# Gate 5 -- LIVE analyzer purity and route-level code splitting
# ===========================================================================


def test_gate5_live_shell_contains_no_burst_control() -> None:
    live_shell = SRC_ROOT / "shells" / "LiveShell.tsx"
    assert live_shell.is_file()
    assert "burst" not in live_shell.read_text(encoding="utf-8").lower()


def test_gate5_analyzer_renders_shadow_burst_only_under_paper() -> None:
    source = (SRC_ROOT / "pages" / "AnalyzerPage.tsx").read_text(encoding="utf-8")

    # Every burst control sits inside a ``mode === "PAPER" ?`` branch: there is
    # exactly one gate per handler, and no handler outside a gate.
    handlers = source.count("void burst()")
    gates = list(re.finditer(r'mode === "PAPER" \?', source))
    assert handlers >= 1, "the PAPER burst handler disappeared entirely"
    assert handlers == len(gates), (handlers, len(gates))
    for gate in gates:
        tail = source[gate.end() : gate.end() + 800]
        assert "void burst()" in tail, "a PAPER gate does not contain the burst handler"

    # The LIVE branch is an explicit removal, not a disabled button.
    assert 'disabled={mode === "LIVE"' not in source
    assert "Shadow burst is PAPER-only" not in source

    assert source.count('mode === "PAPER" ?') >= 2
    assert source.count('mode === "LIVE"') >= 1
    # The LIVE banner explains the absence rather than offering the control.
    assert "PAPER-only control" in source


def test_gate5_routes_are_lazy_loaded_behind_one_suspense_boundary() -> None:
    app = (SRC_ROOT / "App.tsx").read_text(encoding="utf-8")
    assert "lazy(() => import(" in app
    assert app.count("lazy(() => import(") >= 17
    assert "<Suspense" in app
    assert "RouteSuspense" in app
    # The shared fallback is flat matte: no animation, no shadow, no blur.
    assert "#0D0E10" in app
    assert "#27292E" in app
    assert "animate-" not in app
    assert "shadow-" not in app
    assert "backdrop" not in app

    for page in ("DashboardPage", "BrainPage", "ForgePage", "OptionsPage"):
        text = (SRC_ROOT / "pages" / f"{page}.tsx").read_text(encoding="utf-8")
        assert "telemetry" in text.lower(), f"{page} does not render the telemetry block"

    shell = (SRC_ROOT / "shells" / "ShellFrame.tsx").read_text(encoding="utf-8")
    assert "TelemetryStrip" in shell


def test_gate5_agents_page_renders_the_episodic_panel() -> None:
    agents = (SRC_ROOT / "pages" / "AgentsPage.tsx").read_text(encoding="utf-8")
    assert "Episodic operator decisions" in agents
    assert "/memory/episodes" in agents
    assert "/memory/compact" in agents
    assert "Compact Memory" in agents
    assert "EPISODE_KINDS" in agents


def test_gate5_forbidden_primitives_are_absent_from_the_sources() -> None:
    scanned = 0
    for path in sorted(SRC_ROOT.rglob("*.tsx")) + sorted(SRC_ROOT.rglob("*.ts")):
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("backdrop-filter", "backdrop-blur", "linear-gradient", "radial-gradient"):
            assert forbidden not in text, f"{path.name} contains {forbidden}"
        scanned += 1
    assert scanned >= 30, scanned


# ===========================================================================
# Gate 6 -- suite inventory
# ===========================================================================


def test_gate6_suite40_belongs_to_the_forty_suite_release() -> None:
    suites = sorted(path.name for path in (ROOT / "tests").glob("test_*.py"))
    assert len(suites) == 41, suites
    assert SUITE_NAME in suites
    assert "test_v2_3_wal_db_compaction_and_episodic_memory.py" in suites
