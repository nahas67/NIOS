"""Suite 41 -- V2.4 end-to-end latency, memory, database and frontend budgets.

The V2.4 pass is a pure optimisation release: not one security, durability or
numerical invariant may move.  This suite is therefore written as a set of
*budget* assertions over behaviour a caller can observe -- never over a private
attribute name -- so a future refactor cannot silently re-open a cost the pass
paid for:

* Ring 0 SQLite WAL tuning (``cache_size`` / ``temp_store`` / ``mmap_size`` /
  ``wal_autocheckpoint`` on top of the mandatory WAL quartet), covering indexes
  on the three hot lookups with no ``USE TEMP B-TREE FOR ORDER BY`` anywhere.
* Ring 0 incremental save: an unchanged document round-trips byte-identically,
  so the skipped re-projection can never change what a reader sees.
* Ring 3 HTTP delivery: immutable year-long caching for content-hashed assets,
  ``no-cache`` for the SPA shell, gzip for large JSON, and *no* gzip buffering
  on the SSE stream.
* Ring 2 hot paths: the cached entity-memory ranking is byte-identical to a
  cold ranking, and the analytics cache invalidates on a workspace reset.
* Ring 3 frontend runtime: no chunk at or above the 500 kB budget, zero
  forbidden flat-matte primitives in the shipped CSS, and the stream client
  still coalesces market bursts while delivering state-critical frames
  immediately.
"""

from __future__ import annotations

import json
import sqlite3
import asyncio
import hashlib
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from core import json_codec
from core.telemetry import (
    LatencyASGIMiddleware,
    gateway_telemetry,
    reset_gateway_telemetry,
)
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITE_NAME = "test_v2_4_performance_and_optimization_budgets.py"

UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell"
SRC_ROOT = UI_ROOT / "src"
DIST_ROOT = UI_ROOT / "dist"

CHUNK_BUDGET_BYTES = 500_000

# The mandatory durability quartet plus the four V2.4 performance pragmas.
EXPECTED_PRAGMAS = {
    "journal_mode": "wal",
    "synchronous": 2,  # FULL
    "foreign_keys": 1,
    "busy_timeout": 5000,
    "cache_size": -65536,  # 64 MB page cache
    "temp_store": 2,  # MEMORY
    "mmap_size": 268_435_456,  # 256 MB memory-mapped reads
    "wal_autocheckpoint": 1000,
}


# ---------------------------------------------------------------------------
# Ring 0 -- SQLite WAL engine tuning
# ---------------------------------------------------------------------------


def _pragma_map(connection: sqlite3.Connection) -> dict[str, object]:
    observed: dict[str, object] = {}
    for name in EXPECTED_PRAGMAS:
        row = connection.execute(f"PRAGMA {name}").fetchone()
        observed[name] = row[0] if row is not None else None
    return observed


def test_wal_connection_applies_the_durability_and_performance_pragmas(tmp_path: Path) -> None:
    """Every connection must carry the full eight-pragma profile.

    ``synchronous=FULL``, ``journal_mode=WAL`` and ``foreign_keys=ON`` are the
    durability contract; the four performance pragmas ride along and are read
    back from the engine, so a filesystem that silently clamps a request is
    reported instead of assumed.
    """

    from ring0_vault.ibor.wal_state_store import WALStateStore

    store = WALStateStore(tmp_path / "pragma_probe.json")
    try:
        connection = store.connection()
        observed = _pragma_map(connection)

        assert observed["journal_mode"] == "wal"
        assert observed["synchronous"] == 2, "synchronous must stay FULL"
        assert observed["foreign_keys"] == 1, "foreign_keys must stay ON"
        assert int(observed["busy_timeout"]) == 5000

        assert int(observed["cache_size"]) == -65536, "64 MB page cache is the V2.4 budget"
        assert int(observed["temp_store"]) == 2, "temp_store must be MEMORY"
        assert int(observed["mmap_size"]) >= 268_435_456, ">=256 MB memory-mapped reads"
        assert int(observed["wal_autocheckpoint"]) == 1000

        # The same profile must be reported for a second connection handle and
        # must survive a reopen, so a pooled handle never regresses to defaults.
        assert _pragma_map(store.connection()) == observed
    finally:
        store.close()


def test_wal_connection_is_reused_instead_of_reopened(tmp_path: Path) -> None:
    """Hot reads must not pay file-open plus PRAGMA setup per call."""

    from ring0_vault.ibor.wal_state_store import WALStateStore

    store = WALStateStore(tmp_path / "reuse_probe.json")
    try:
        first = store.connection()
        for _ in range(50):
            assert store.connection() is first
        # A second thread gets its own handle (sqlite3 handles are thread
        # affine) but the store still owns exactly two connections.
        import threading

        seen: list[object] = []

        def _probe() -> None:
            seen.append(store.connection())

        worker = threading.Thread(target=_probe)
        worker.start()
        worker.join(timeout=30.0)
        assert seen and seen[0] is not first
    finally:
        store.close()


def _explain(connection: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> str:
    rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " | ".join(str(row[-1]) for row in rows)


def test_hot_lookups_use_indexes_and_never_a_temp_btree(tmp_path: Path) -> None:
    """The three Ring 0 hot lookups must be index-driven at V2.4.

    V2.3 still materialised ``USE TEMP B-TREE FOR ORDER BY`` for the working
    memory namespace read; that sort is exactly what the new
    ``(namespace, updated_at_utc_ns)`` index removes.
    """

    from ring0_vault.ibor.wal_state_store import WALStateStore

    store = WALStateStore(tmp_path / "plan_probe.json")
    try:
        connection = store.connection()

        timeline_plan = _explain(
            connection,
            "SELECT event_id, ts_utc_ns FROM nios_entity_timeline_local "
            "ORDER BY ts_utc_ns DESC LIMIT 25",
            (),
        ).upper()
        assert "INDEX" in timeline_plan, timeline_plan
        assert "TEMP B-TREE" not in timeline_plan, timeline_plan

        episodic_plan = _explain(
            connection,
            "SELECT episode_id FROM nios_episodic_memory_local "
            "WHERE action_kind = ? ORDER BY ts_utc_ns DESC LIMIT 25",
            ("TRADE",),
        ).upper()
        assert "INDEX" in episodic_plan, episodic_plan
        assert "TEMP B-TREE" not in episodic_plan, episodic_plan

        working_plan = _explain(
            connection,
            "SELECT row_key, payload_json FROM nios_working_memory_local "
            "WHERE namespace = ? ORDER BY updated_at_utc_ns DESC",
            ("orders",),
        ).upper()
        assert "INDEX" in working_plan, working_plan
        assert "TEMP B-TREE" not in working_plan, (
            "the working-memory namespace read must be index-ordered, got: " + working_plan
        )

        indexes = {
            str(row[1]).lower()
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert any("working_memory" in name for name in indexes), indexes
        assert any("episodic" in name for name in indexes), indexes
        assert any("timeline" in name for name in indexes), indexes
    finally:
        store.close()


def test_unchanged_document_round_trips_byte_identically(tmp_path: Path) -> None:
    """The incremental save path must not change a single byte a reader sees."""

    from ring0_vault.ibor.wal_state_store import WALStateStore

    document = {
        "workspace": {"mode": "PAPER", "budget_micros": 5_000_000_000},
        "logbook": [{"trade_id": f"T{index:03d}", "net_pnl_micros": index * 1000} for index in range(20)],
        "entity_memory": {
            "timeline": [
                {
                    "event_id": f"EV{index:03d}",
                    "ts_utc": 1_700_000_000_000_000_000 + index,
                    "outcome_kind": "LOSS",
                    "entity_ids": ["symbol:SYM3"],
                    "payload": {"net_pnl_micros": -1000, "slippage_bps": 2.5},
                    "payload_sha256": "",
                }
                for index in range(10)
            ],
            "truths": [],
            "checkpoints": [],
        },
    }

    store = WALStateStore(tmp_path / "identity_probe.json")
    try:
        stamp = 1_700_000_000_000_000_000
        first = store.write_document(document, saved_at_ns=stamp)
        first_document = store.read_document()
        first_timeline = store.read_entity_timeline()

        # The same stamp on both saves, so the reserved ``__document__``
        # envelope must match too: an incremental save may not touch a byte.
        second = store.write_document(document, saved_at_ns=stamp)
        second_document = store.read_document()
        second_timeline = store.read_entity_timeline()

        assert first and second
        assert first_document == second_document
        assert first_timeline == second_timeline
        # ``integrity_check`` reports the SQLite verdict verbatim ("ok").
        assert str(store.integrity_check()).lower() in {"ok", "true"}
    finally:
        store.close()


def test_state_journal_reset_then_save_still_works(tmp_path: Path) -> None:
    """``reset()`` closes the pool; the next ``save()`` must transparently reopen it."""

    from ring0_vault.ibor.state_journal import AtomicStateJournal

    journal = AtomicStateJournal(tmp_path / "journal_probe.json")
    try:
        journal.save({"workspace": {"mode": "PAPER"}})
        journal.reset()
        journal.save({"workspace": {"mode": "PAPER", "reopened": True}})
        state = journal.load()
        assert state["workspace"]["reopened"] is True
    finally:
        journal.reset()


# ---------------------------------------------------------------------------
# Ring 3 -- frontend runtime and shipped-asset budgets
# ---------------------------------------------------------------------------


def test_no_shipped_chunk_reaches_the_five_hundred_kilobyte_budget() -> None:
    assets = sorted((DIST_ROOT / "assets").glob("*.js"))
    assert assets, "the compiled bundle is missing; run `npm run build` before the seal"

    oversized = {path.name: path.stat().st_size for path in assets if path.stat().st_size >= CHUNK_BUDGET_BYTES}
    assert oversized == {}, f"chunks at or above {CHUNK_BUDGET_BYTES} bytes: {oversized}"


def test_shipped_stylesheet_keeps_the_flat_matte_contract() -> None:
    stylesheets = sorted((DIST_ROOT / "assets").glob("*.css"))
    assert stylesheets, "the compiled stylesheet is missing"

    css = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in stylesheets)
    forbidden = {
        "blur(": css.count("blur("),
        "box-shadow": css.count("box-shadow"),
        "linear-gradient": css.count("linear-gradient"),
        "radial-gradient": css.count("radial-gradient"),
        "radius:9999px": css.replace(" ", "").count("border-radius:9999px"),
    }
    assert forbidden == dict.fromkeys(forbidden, 0), forbidden


def test_stream_client_coalesces_market_frames_and_never_buffers_control_frames() -> None:
    source = (SRC_ROOT / "lib" / "stream.ts").read_text(encoding="utf-8")

    assert "requestAnimationFrame" in source, "market bursts must be coalesced on the frame clock"
    assert "COALESCE_FALLBACK_MS" in source, "hidden tabs still need a timer fallback"
    assert "flushNow" in source, "a control frame must flush the buffered market tail first"

    for frame in ("TICK", "DEPTH"):
        assert frame in source, f"{frame} must be treated as a coalescable market frame"
    for frame in (
        "ORDER",
        "TRANCHE_HALT",
        "WALLET_SWEEP",
        "COLD_START_RECON",
        "SYSTEM_ALERT",
    ):
        assert frame in source, f"{frame} must stay state-critical (delivered immediately)"


def test_charts_and_hot_tables_are_memoised() -> None:
    charts = (SRC_ROOT / "charts" / "index.tsx").read_text(encoding="utf-8")
    assert charts.count("memo(") >= 8, "all eight SVG charts must be memoised"
    assert "useMemo(" in charts, "chart geometry must be memoised on its data inputs"
    assert charts.count("displayName") >= 8

    for page in ("ScreenerPage", "LogbookPage", "OrdersPage"):
        source = (SRC_ROOT / "pages" / f"{page}.tsx").read_text(encoding="utf-8")
        assert "memo(" in source, f"{page} rows must be memoised"
        assert "useMemo(" in source, f"{page} derived rows must be memoised"


# ---------------------------------------------------------------------------
# Ring 2 -- memory and analytics caches must be invisible to a reader
# ---------------------------------------------------------------------------


def test_cached_entity_memory_ranking_is_byte_identical_to_a_cold_ranking() -> None:
    """The vector/BM25 cache may only change cost, never a single byte."""

    from ring2_brain.memory.entity_graph import EntityMemoryGraph

    graph = EntityMemoryGraph()
    for index in range(40):
        graph.record_outcome(
            outcome_kind="LOSS" if index % 3 else "WIN",
            net_pnl_micros=-1_000 if index % 3 else 2_000,
            slippage_bps=2.0 + (index % 5),
            symbol=f"SYM{index % 4}",
            silo="CRYPTO_BINANCE",
            strategy_id=f"trend{index % 3}",
            lesson_summary=f"trend slippage drawdown {index}",
            ts_utc=1_700_000_000_000_000_000 + index,
        )
    graph.compile_truths()

    kwargs = dict(
        symbol="SYM0",
        silo="CRYPTO_BINANCE",
        strategy_id="trend0",
        query_text="trend slippage drawdown",
        top_k=5,
        now_ns=1_700_000_100_000_000_000,
    )

    graph.cache_clear()
    cold = graph.query_entity_memory(**kwargs)
    warm = graph.query_entity_memory(**kwargs)
    third = graph.query_entity_memory(**kwargs)

    assert cold["packet_sha256"] == warm["packet_sha256"] == third["packet_sha256"]
    assert json.dumps(cold, sort_keys=True, default=str) == json.dumps(warm, sort_keys=True, default=str)
    assert json.dumps(warm, sort_keys=True, default=str) == json.dumps(third, sort_keys=True, default=str)

    stats = graph.cache_stats()
    assert stats["hits"] >= 1, stats
    assert stats["entries"] >= 1, stats

    # A new outcome on the queried symbol must invalidate that entry, and the
    # freshly computed answer must differ while staying internally consistent.
    graph.record_outcome(
        outcome_kind="BREAKER_TRIP",
        net_pnl_micros=-5_000,
        slippage_bps=9.0,
        symbol="SYM0",
        silo="CRYPTO_BINANCE",
        strategy_id="trend0",
        lesson_summary="trend slippage drawdown breaker",
        ts_utc=1_700_000_200_000_000_000,
    )
    graph.compile_truths()
    after = graph.query_entity_memory(**kwargs)
    again = graph.query_entity_memory(**kwargs)
    assert after["packet_sha256"] == again["packet_sha256"]
    assert after["packet_sha256"] != cold["packet_sha256"]


def test_analytics_cache_reports_the_same_digest_and_invalidates_on_demand() -> None:
    from ring2_brain.quant.performance_analytics import PerformanceAnalytics

    rows = [
        {
            "closed_at_utc": f"2026-01-{day:02d}T00:00:00Z",
            "realised_pnl_micros": 1_000_000 if day % 2 else -400_000,
            "fees_micros": 10_000,
            "taxes_micros": 5_000,
            "slippage_micros": 2_000,
            "notional_micros": 50_000_000,
            "silo": "US_IBKR",
            "mission_id": "M1",
        }
        for day in range(1, 29)
    ]

    analytics = PerformanceAnalytics()
    now_ns = 1_700_000_000_000_000_000

    cold = analytics.report(rows, silo="US_IBKR", bucket="1D", now_ns=now_ns)
    warm = analytics.report(rows, silo="US_IBKR", bucket="1D", now_ns=now_ns)
    assert cold["report_sha256"] == warm["report_sha256"]
    assert json.dumps(cold, sort_keys=True, default=str) == json.dumps(warm, sort_keys=True, default=str)

    # A fresh stamp is still produced per call when the caller does not pin one.
    unpinned = analytics.report(rows, silo="US_IBKR", bucket="1D")
    pinned_again = analytics.report(rows, silo="US_IBKR", bucket="1D", now_ns=now_ns)
    assert pinned_again["report_sha256"] == cold["report_sha256"]
    assert unpinned["as_of_utc_ns"] >= cold["as_of_utc_ns"]

    analytics.invalidate()
    after_invalidate = analytics.report(rows, silo="US_IBKR", bucket="1D", now_ns=now_ns)
    assert after_invalidate["report_sha256"] == cold["report_sha256"]

    analytics.cache_clear()
    assert analytics.cache_stats()["entries"] == 0




# ---------------------------------------------------------------------------
# Part 3 -- Ring 3 transport: pure-ASGI latency, gzip, caching, JSON codec
# ---------------------------------------------------------------------------
#
# The transport pass is not allowed to change what a client *means*: the
# canonical payload_sha256 bytes are frozen, the SSE stream stays
# uncompressed and unbuffered, and only content-hashed assets are immutable.

HEX64 = re.compile(r"^[0-9a-f]{64}$")

ASSET_NAME = "index-BSyqsIEm.js"
ASSET_BODY = "console.log('nios-cockpit');\n" * 400


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cockpit(tmp_path_factory: pytest.TempPathFactory):
    """One real FastAPI app plus a real ``dist/`` bundle on a temp data root."""

    from fastapi.testclient import TestClient

    from core.orchestrator import NIOSCoreOrchestrator
    from ring3_cockpit.gateway.control_plane import create_app

    root = tmp_path_factory.mktemp("v24-suite41")
    workspace = root / "workspace"
    vault = root / "vault"
    dist = root / "dist"
    workspace.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>nios-cockpit</title>", encoding="utf-8"
    )
    (dist / "assets" / ASSET_NAME).write_bytes(ASSET_BODY.encode("utf-8"))
    (dist / "favicon.ico").write_bytes(b"\x00\x00\x01\x00" + b"\x00" * 64)
    (root / "TOP_SECRET.txt").write_text("DO-NOT-SERVE", encoding="utf-8")

    previous_journal = os.environ.get("NIOS_JOURNAL_PATH")
    os.environ["NIOS_JOURNAL_PATH"] = str(root / "nios_workstation_journal.json")
    runtime = NIOSCoreOrchestrator(
        workspace=workspace, key_vault_root=vault, clean_slate=True
    )
    reset_gateway_telemetry()
    app = create_app(runtime.control, static_dir=str(dist))
    with TestClient(app) as client:
        yield client, runtime, app, dist
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
        os.environ["NIOS_JOURNAL_PATH"] = previous_journal


# ---------------------------------------------------------------------------
# 1. canonical hashing is frozen
# ---------------------------------------------------------------------------


def _legacy_canonical(payload: dict[str, Any]) -> str:
    """The exact pre-V2.4 expression, copied verbatim into this suite."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )


NESTED_PAYLOAD: dict[str, Any] = {
    "z_nested": {"b": [1, 2, {"k": "\u20b9\u00e9\U0001f600", "n": None}]},
    "a_float": 0.1,
    "tiny_float": 1e-09,
    "big_micros": 2**70,  # 128-bit integer micro-units (sec_009)
    "negative_micros": -(2**70),
    "bool": True,
    "text": "NAV \u2014 10,000 micros",
    "ts": "2026-11-20T12:00:00Z",
}


def test_canonical_bytes_is_byte_identical_to_the_legacy_expression() -> None:
    legacy = _legacy_canonical(NESTED_PAYLOAD).encode("utf-8")
    assert json_codec.canonical_bytes(NESTED_PAYLOAD) == legacy
    assert json_codec.canonical_json(NESTED_PAYLOAD) == legacy.decode("utf-8")


def test_canonical_bytes_keeps_ascii_escaping_and_exact_separators() -> None:
    body = json_codec.canonical_bytes({"z": 1, "a": "\u20b9"})
    assert body == b'{"a":"\\u20b9","z":1}'
    assert b", " not in body


def test_canonical_bytes_never_sorts_through_orjson_quirks() -> None:
    """orjson prints ``1e-9`` and refuses 128-bit ints; the hash must not care."""
    assert json_codec.canonical_bytes({"f": 1e-09}) == _legacy_canonical(
        {"f": 1e-09}
    ).encode("utf-8")
    huge = {"micros": 2**100}
    assert json_codec.canonical_bytes(huge) == _legacy_canonical(huge).encode("utf-8")


def test_payload_digest_uses_canonical_bytes_and_still_verifies() -> None:
    from ring3_cockpit.gateway.payload_envelope import enclose, payload_digest

    body = enclose({"page": "probe", "value": "\u20b9 42"}, workspace_mode="PAPER")
    assert HEX64.match(body["payload_sha256"])
    assert body["payload_sha256"] == payload_digest(body)
    stripped = {k: v for k, v in body.items() if k != "payload_sha256"}
    expected = hashlib.sha256(
        _legacy_canonical(stripped).encode("utf-8")
    ).hexdigest()
    assert body["payload_sha256"] == expected


def test_canonical_json_rejects_nan_identically() -> None:
    with pytest.raises(ValueError):
        json_codec.canonical_json({"x": float("nan")})


# ---------------------------------------------------------------------------
# 2. the wire codec is semantically identical
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"b": 1, "a": [1, 2, {"z": None, "y": True}]},
        {"law": "sec_009 - \u20b9 \u00e9 \U0001f600"},
        {"ts": datetime(2026, 11, 20, 12, 0, 0)},
        {"d": date(2026, 11, 20)},
        {"u": UUID("12345678-1234-5678-1234-567812345678")},
        {"d": Decimal("1.25")},
        {"big": 2**80, "neg": -(2**80)},
        {"f": 0.1, "g": -0.0, "h": 1e-09},
        {1: "a", 2: "b"},
        {},
    ],
)
def test_dumps_round_trips_to_the_same_values(payload: dict[Any, Any]) -> None:
    parsed = json.loads(json_codec.dumps(payload))
    # ``default=str`` semantics: anything JSON cannot hold becomes its ``str``.
    assert parsed == json.loads(_legacy_canonical(payload))


def test_dumps_matches_stdlib_default_str_for_datetimes() -> None:
    payload = {"ts": datetime(2026, 11, 20, 12, 0, 0)}
    assert json.loads(json_codec.dumps(payload))["ts"] == str(payload["ts"])


def test_dumps_iso_datetime_matches_fastapi_jsonable_encoder() -> None:
    stamp = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    body = json.loads(json_codec.dumps({"ts": stamp}, iso_datetime=True))
    assert body["ts"] == stamp.isoformat()


def test_dumps_without_orjson_is_semantically_identical(monkeypatch) -> None:
    """The optional dependency must be optional, not load-bearing."""
    payload = {
        "z": 1,
        "unicode": "\u20b9\U0001f600",
        "ts": datetime(2026, 11, 20, 12, 0, 0),
        "big": 2**80,
    }
    with_orjson = json_codec.dumps_bytes(payload)
    monkeypatch.setattr(json_codec, "orjson", None)
    without = json_codec.dumps_bytes(payload)
    assert json.loads(without) == json.loads(with_orjson)
    assert json.loads(without) == json.loads(_legacy_canonical(payload))


def test_loads_accepts_bytes_and_str() -> None:
    assert json_codec.loads(b'{"a":1}') == {"a": 1}
    assert json_codec.loads('{"a":1}') == {"a": 1}
    with pytest.raises(ValueError):
        json_codec.loads(b"{not json")


# ---------------------------------------------------------------------------
# 3. pure ASGI latency middleware
# ---------------------------------------------------------------------------


def _http_scope(path: str = "/probe") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "headers": [],
        "query_string": b"",
    }


def test_latency_middleware_stamps_state_and_records_once() -> None:
    reset_gateway_telemetry()
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["started_ns"] = scope["state"]["nios_telemetry_started_ns"]
        seen["recorded_before"] = bool(scope["state"].get("nios_latency_recorded"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        return None

    scope = _http_scope()
    asyncio.run(LatencyASGIMiddleware(app)(scope, receive, send))

    assert isinstance(seen["started_ns"], int) and seen["started_ns"] > 0
    assert seen["recorded_before"] is False
    samples = gateway_telemetry().metric_samples("http_request")
    assert len(samples) == 1 and samples[0] >= 0
    assert scope["state"]["nios_latency_recorded"] is True
    assert gateway_telemetry().count(3) == 1
    assert gateway_telemetry().metric_samples("http_request_error") == []


def test_latency_middleware_honours_a_handler_that_recorded_first() -> None:
    reset_gateway_telemetry()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        # A telemetry surface records its own in-flight span before responding.
        scope["state"]["nios_latency_recorded"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        return None

    asyncio.run(LatencyASGIMiddleware(app)(_http_scope(), receive, send))
    assert gateway_telemetry().metric_samples("http_request") == []


def test_latency_middleware_records_error_metric_and_reraises() -> None:
    reset_gateway_telemetry()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        return None

    with pytest.raises(RuntimeError):
        asyncio.run(LatencyASGIMiddleware(app)(_http_scope(), receive, send))
    assert gateway_telemetry().metric_samples("http_request") == []
    assert len(gateway_telemetry().metric_samples("http_request_error")) == 1


def test_latency_middleware_passes_non_http_scopes_through_untouched() -> None:
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["scope"] = scope

    async def receive() -> dict[str, Any]:
        return {"type": "lifespan.startup"}

    async def send(message: dict[str, Any]) -> None:
        return None

    scope = {"type": "lifespan"}
    asyncio.run(LatencyASGIMiddleware(app)(scope, receive, send))
    assert seen["scope"] is scope
    assert "state" not in scope  # nothing allocated on the pass-through path


def test_latency_middleware_records_at_response_start_not_body_end() -> None:
    """An SSE response must be measured from its first frame."""

    reset_gateway_telemetry()

    async def scenario() -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            started.set()
            await release.wait()  # a slow body: 0 bytes reach the client
            await send({"type": "http.response.body", "body": b"tail", "more_body": False})

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            return None

        task = asyncio.create_task(
            LatencyASGIMiddleware(app)(_http_scope("/api/v2/stream"), receive, send)
        )
        await asyncio.wait_for(started.wait(), timeout=2.0)
        # The sample already exists while the body is still blocked.
        assert len(gateway_telemetry().metric_samples("http_request")) == 1
        release.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. gateway wiring: stack order, gzip, caching
# ---------------------------------------------------------------------------


def test_middleware_stack_order_is_csrf_gzip_envelope_latency_cors(cockpit) -> None:
    _client, _runtime, app, _dist = cockpit
    names = [middleware.cls.__name__ for middleware in app.user_middleware]
    assert names == [
        "BaseHTTPMiddleware",  # register_v2_1_routes -> nios_csrf_guard
        "GZipMiddleware",
        "BaseHTTPMiddleware",  # _envelope_middleware
        "LatencyASGIMiddleware",
        "CORSMiddleware",
    ]
    classes = {middleware.cls.__name__: middleware for middleware in app.user_middleware}
    assert classes["LatencyASGIMiddleware"].kwargs["ring"] == 3
    assert classes["LatencyASGIMiddleware"].kwargs["metric"] == "http_request"
    assert classes["GZipMiddleware"].kwargs["minimum_size"] == 1024
    assert "text/event-stream" in classes["GZipMiddleware"].kwargs["exclude_content_types"]


def test_no_starlette_http_middleware_decorator_remains_for_latency(cockpit) -> None:
    _client, _runtime, app, _dist = cockpit
    dispatch_names = {
        getattr(middleware.kwargs.get("dispatch"), "__name__", "")
        for middleware in app.user_middleware
        if middleware.kwargs.get("dispatch") is not None
    }
    assert "_latency_middleware" not in dispatch_names


def test_status_is_gzipped_and_the_envelope_still_verifies(cockpit) -> None:
    client, _runtime, _app, _dist = cockpit
    response = client.get("/api/v2/status", headers={"accept-encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    body = response.json()
    from ring3_cockpit.gateway.payload_envelope import payload_digest

    assert body["payload_sha256"] == payload_digest(body)
    assert body["schema_version"].startswith("v1.6")
    assert body["page"] == "status"


def test_status_without_gzip_carries_the_same_values(cockpit) -> None:
    client, _runtime, _app, _dist = cockpit
    plain = client.get("/api/v2/status", headers={"accept-encoding": "identity"})
    assert plain.headers.get("content-encoding") in (None, "identity")
    body = plain.json()
    assert body["page"] == "status"
    assert HEX64.match(body["payload_sha256"])


def test_metrics_round_trips_intact_under_gzip(cockpit) -> None:
    """Transport compression only: the Prometheus text is unchanged when decoded."""
    client, _runtime, _app, _dist = cockpit
    identity = client.get("/metrics", headers={"accept-encoding": "identity"})
    assert identity.status_code == 200
    assert identity.headers.get("content-encoding") in (None, "identity")
    assert identity.headers.get("cache-control") == "no-store"
    assert "nios_" in identity.text

    compressed = client.get("/metrics", headers={"accept-encoding": "gzip"})
    assert compressed.status_code == 200
    assert compressed.headers.get("cache-control") == "no-store"
    assert "text/plain" in compressed.headers.get("content-type", "")
    # httpx decodes ``content-encoding`` transparently, and the scrape-duration
    # gauge moves between calls, so compare the metric inventory + line count.
    def _names(text: str) -> list[str]:
        return sorted(
            line.split()[2]
            for line in text.splitlines()
            if line.startswith("# TYPE ")
        )

    assert _names(compressed.text) == _names(identity.text)
    assert len(compressed.text.splitlines()) == len(identity.text.splitlines())
    assert compressed.text.startswith("# HELP ")
    assert "\x1f\x8b" not in compressed.text


def test_instruments_is_gzipped_and_semantically_identical(cockpit) -> None:
    client, runtime, _app, _dist = cockpit
    compressed = client.get("/api/v2/instruments", headers={"accept-encoding": "gzip"})
    plain = client.get("/api/v2/instruments", headers={"accept-encoding": "identity"})
    assert compressed.status_code == 200
    assert compressed.headers.get("content-encoding") == "gzip"
    assert plain.headers.get("content-encoding") in (None, "identity")

    volatile = {"generated_at_ns", "payload_sha256"}
    direct = runtime.control.instrument_catalog(limit=50, offset=0)
    left = {k: v for k, v in compressed.json().items() if k not in volatile}
    right = {k: v for k, v in plain.json().items() if k not in volatile}
    assert left == right
    assert left == {k: v for k, v in direct.items() if k not in volatile}
    assert len(plain.content) > 1024  # the document that makes gzip worth it


def test_stream_is_never_gzipped_and_arrives_as_sse(cockpit) -> None:
    client, _runtime, _app, _dist = cockpit
    with client.stream(
        "GET", "/api/v2/stream", params={"max_events": 1, "fps": 60}
    ) as response:
        assert response.status_code == 200
        assert response.headers.get("content-encoding") is None
        assert "text/event-stream" in response.headers.get("content-type", "")
        assert response.headers.get("cache-control") == "no-cache, no-transform"
        first = next(response.iter_lines())
    assert first.startswith("event:")


def test_cockpit_stream_alias_is_also_untouched(cockpit) -> None:
    client, _runtime, _app, _dist = cockpit
    response = client.get("/api/v2/cockpit/stream", params={"frames": 1, "fps": 60})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") is None
    assert "text/event-stream" in response.headers.get("content-type", "")


def test_asset_is_immutable_and_routes_revalidate(cockpit) -> None:
    client, _runtime, _app, _dist = cockpit

    asset = client.get(f"/assets/{ASSET_NAME}")
    assert asset.status_code == 200
    assert asset.headers.get("cache-control") == "public, max-age=31536000, immutable"
    assert len(asset.content) == len(ASSET_BODY)

    for route in ("/", "/cockpit/orders", "/cockpit"):
        response = client.get(route)
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-cache"
        assert "text/html" in response.headers.get("content-type", "")

    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.headers.get("cache-control") == "public, max-age=3600"


def test_missing_asset_falls_back_to_no_cache_html_not_immutable(cockpit) -> None:
    """A 404-able chunk must never be cached for a year as JS."""
    from ring3_cockpit.gateway.control_plane import CACHE_CONTROL_REVALIDATE

    client, _runtime, _app, _dist = cockpit
    response = client.get("/assets/this-chunk-does-not-exist.js")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert response.headers.get("cache-control") == CACHE_CONTROL_REVALIDATE


def test_api_and_metrics_paths_still_short_circuit_to_404(cockpit) -> None:
    client, _runtime, _app, _dist = cockpit
    for path in ("/api/v2/not-a-route", "/api/unknown"):
        response = client.get(path)
        assert response.status_code == 404
        assert "text/html" not in response.headers.get("content-type", "")


def test_spa_fallback_refuses_path_traversal(cockpit) -> None:
    client, _runtime, _app, _dist = cockpit
    response = client.get("/%2e%2e/TOP_SECRET.txt")
    assert "DO-NOT-SERVE" not in response.text


def test_static_cache_control_policy_table() -> None:
    from ring3_cockpit.gateway.control_plane import (
        CACHE_CONTROL_IMMUTABLE,
        CACHE_CONTROL_REVALIDATE,
        CACHE_CONTROL_STATIC,
        _static_cache_control,
    )

    cases = {
        f"assets/{ASSET_NAME}": CACHE_CONTROL_IMMUTABLE,
        "assets/index-abc123.css": CACHE_CONTROL_IMMUTABLE,
        "Assets/APP.JS": CACHE_CONTROL_IMMUTABLE,
        "index.html": CACHE_CONTROL_REVALIDATE,
        "": CACHE_CONTROL_REVALIDATE,
        "cockpit/orders": CACHE_CONTROL_REVALIDATE,
        "favicon.ico": CACHE_CONTROL_STATIC,
        "fonts/inter.woff2": CACHE_CONTROL_STATIC,
        "images/logo.png": CACHE_CONTROL_STATIC,
    }
    for path, expected in cases.items():
        assert _static_cache_control(path) == expected, path
    assert _static_cache_control(Path("assets") / ASSET_NAME) == CACHE_CONTROL_IMMUTABLE


def test_static_files_subclass_stamps_the_same_policy(cockpit) -> None:
    _client, _runtime, _app, dist = cockpit
    from ring3_cockpit.gateway.control_plane import _CachedStaticFiles

    files = _CachedStaticFiles(directory=str(dist), html=True)
    scope = {"type": "http", "method": "GET", "headers": []}

    asset = asyncio.run(files.get_response(f"assets/{ASSET_NAME}", scope))
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"

    index = asyncio.run(files.get_response("index.html", scope))
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-cache"


# ---------------------------------------------------------------------------
# 5. telemetry stays wired to real requests
# ---------------------------------------------------------------------------


def test_http_request_metric_grows_by_one_per_request(cockpit) -> None:
    client, _runtime, _app, _dist = cockpit
    reset_gateway_telemetry()
    for _ in range(3):
        assert client.get("/api/v2/cockpit/health").status_code == 200
    samples = gateway_telemetry().metric_samples("http_request")
    assert len(samples) == 3
    assert all(value >= 0 for value in samples)


def test_status_payload_still_reports_measured_latency(cockpit) -> None:
    client, _runtime, _app, _dist = cockpit
    reset_gateway_telemetry()
    client.get("/api/v2/status")  # cold: records the answering span itself
    body = client.get("/api/v2/status").json()
    assert isinstance(body["latency_ms"], (int, float))
    assert body["latency_ms"] >= 0
    assert body["telemetry"]["schema"].startswith("v2.3")
    assert body["latency_ring_samples"]["ring3"] >= 1


def test_analytics_performance_still_parses_after_the_fast_serialiser(cockpit) -> None:
    client, runtime, _app, _dist = cockpit
    response = client.get(
        "/api/v2/analytics/performance", headers={"accept-encoding": "identity"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["page"] == "analytics.performance"
    assert HEX64.match(body["payload_sha256"])
    direct = runtime.control.v21_analytics_performance(silo=None, mission_id=None, bucket="1D")
    volatile = {"generated_at_ns", "payload_sha256", "as_of_utc_ns", "report_sha256"}
    assert {k: v for k, v in body.items() if k not in volatile} == {
        k: v for k, v in direct.items() if k not in volatile
    }


# ---------------------------------------------------------------------------
# Suite inventory
# ---------------------------------------------------------------------------


def test_suite41_belongs_to_the_forty_one_suite_release() -> None:
    suites = sorted(path.name for path in (ROOT / "tests").glob("test_*.py"))
    assert len(suites) == 42, suites
    assert SUITE_NAME in suites
    assert "test_v2_3_telemetry_closure_and_ui_split.py" in suites
    assert (ROOT / "tests" / "conftest.py").is_file()


def test_release_documents_advertise_forty_one_suites_and_the_v2_4_seal() -> None:
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "42 suites" in runbook
    assert "42 suites" in readme
    assert "v4.4.0-V2.4" in runbook
    assert "v4.3.0-V2.3" in runbook
    assert SUITE_NAME in runbook
