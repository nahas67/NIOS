"""Prometheus / OpenTelemetry metrics exporter for the NIOS Glass Cockpit.

Governing sections
------------------
* ``sec_004`` -- Ring 3 is the only ring allowed to expose observability
  surfaces.  This module is read-only: every collector performs a pure read of
  Ring 0 (IBOR + outbox), Ring 1 (leases, heartbeat SHM, latency histogram) and
  the Ring 2 spend ledger.  Nothing here can sign, reserve, or mutate money.
* ``sec_009`` -- the 128-bit micro-unit NAV per silo is exported straight from
  ``IBORLedger.vault_balances`` so the cockpit never re-derives money from
  floats.
* ``sec_016`` -- the control plane exposes ``GET /api/v2/cockpit/stream`` for
  SSE.  Metrics are deliberately a *separate* pull surface so a Prometheus
  scrape never competes with the 60 FPS cockpit stream.

Endpoints registered by :func:`register_metrics_routes`::

    GET /api/v2/metrics        Prometheus / OpenMetrics text exposition
    GET /metrics               alias (standard scrape path)
    GET /api/v2/metrics/json   JSON telemetry for the React cockpit chip

The text format is hand-rendered (no ``prometheus_client`` dependency) so the
approved dependency stack in ``AGENTS.md`` rule 9 is unchanged.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.schemas import VaultName
from core.telemetry import TICK_LATENCY, TickLatencyRecorder, tick_latency_snapshot

__all__ = [
    "CONTENT_TYPE_PROMETHEUS",
    "MetricsExporter",
    "register_metrics_routes",
]

CONTENT_TYPE_PROMETHEUS = "text/plain; version=0.0.4; charset=utf-8"

RING_LABELS: tuple[tuple[int, str], ...] = (
    (0, "ring0_vault"),
    (1, "ring1_spinal"),
    (2, "ring2_brain"),
    (3, "ring3_cockpit"),
)

#: Canonical four-silo order (mirrors ``ring1_spinal.silo_worker.SILO_ORDER``).
_FALLBACK_SILOS: tuple[str, ...] = (
    "CRYPTO_BINANCE",
    "INDIA_ZERODHA_DHAN",
    "US_IBKR",
    "FX_MT5",
)

_VAULT_ORDER: tuple[str, ...] = (
    VaultName.RESERVE_YIELD,
    VaultName.LONG_TERM_COMPOUNDER,
    VaultName.TACTICAL_SWING,
    VaultName.SPINAL_SCALPER,
)

_HEARTBEAT_UNKNOWN = -1.0
_Q98_COLD_START_BPS = 6.2


# ---------------------------------------------------------------------------
# Prometheus text helpers
# ---------------------------------------------------------------------------


def _escape_label(value: Any) -> str:
    text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _fmt(value: Any) -> str:
    """Render a sample value in a Prometheus-compatible form."""
    if value is None:
        return "0"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def _labels(pairs: Sequence[tuple[str, Any]]) -> str:
    if not pairs:
        return ""
    body = ",".join(f'{key}="{_escape_label(val)}"' for key, val in pairs)
    return "{" + body + "}"


class _Builder:
    """Accumulates metric families with correct ``# HELP`` / ``# TYPE`` order."""

    __slots__ = ("_lines", "_seen")

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._seen: set[str] = set()

    def family(self, name: str, kind: str, help_text: str) -> None:
        if name in self._seen:
            return
        self._seen.add(name)
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {kind}")

    def sample(
        self,
        name: str,
        value: Any,
        *,
        kind: str = "gauge",
        help_text: str = "",
        labels: Sequence[tuple[str, Any]] = (),
    ) -> None:
        self.family(name, kind, help_text or name)
        self._lines.append(f"{name}{_labels(labels)} {_fmt(value)}")

    def blank(self) -> None:
        self._lines.append("")

    def text(self) -> str:
        body = "\n".join(self._lines).strip("\n")
        return body + "\n"


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


def _safe(call: Callable[[], Any], default: Any = None) -> Any:
    try:
        return call()
    except Exception:  # pragma: no cover - a broken collector must not 500
        return default


def _engine_stats(control: Any) -> dict[str, Any]:
    engine = getattr(control, "engine", None)
    stats = getattr(engine, "stats", None) if engine is not None else None
    return dict(stats) if isinstance(stats, Mapping) else {}


def _canonical_silos(control: Any) -> tuple[str, ...]:
    try:  # pragma: no cover - import guard only
        from ring1_spinal.silo_worker import SILO_ORDER

        if SILO_ORDER:
            return tuple(str(s) for s in SILO_ORDER)
    except Exception:
        pass
    registry = getattr(control, "registry", None)
    silos = getattr(registry, "silos", None) if registry is not None else None
    if isinstance(silos, Mapping) and silos:
        return tuple(str(s) for s in silos)
    return _FALLBACK_SILOS


def _heartbeat_rows(control: Any) -> list[dict[str, Any]]:
    """Read ``shm://nios-ring-heartbeats`` live, one row per ring."""
    snapshot: Mapping[str, Any] | None = None
    supervisor = getattr(control, "process_supervisor", None)
    if supervisor is not None:
        reader = getattr(supervisor, "heartbeat_table_snapshot", None)
        if callable(reader):
            snapshot = _safe(reader)
    if not snapshot:
        def _read() -> Mapping[str, Any]:
            from core.process_supervisor import RingHeartbeatTable

            table = RingHeartbeatTable()
            try:
                return table.snapshot()
            finally:
                table.close()

        snapshot = _safe(_read) or {}

    rows: list[dict[str, Any]] = []
    for ring_index, worker in RING_LABELS:
        payload: Mapping[str, Any] | None = None
        if isinstance(snapshot, Mapping):
            for key, value in snapshot.items():
                if not isinstance(value, Mapping):
                    continue
                if int(value.get("ring_index", -1)) == ring_index or str(key) == worker:
                    payload = value
                    break
        if payload is None:
            rows.append(
                {
                    "ring": ring_index,
                    "worker": worker,
                    "pid": 0,
                    "alive": False,
                    "heartbeat_age_ms": _HEARTBEAT_UNKNOWN,
                }
            )
            continue
        age_ms = payload.get("heartbeat_age_ms")
        rows.append(
            {
                "ring": ring_index,
                "worker": str(payload.get("worker") or worker),
                "pid": int(payload.get("pid") or 0),
                "alive": bool(payload.get("alive")),
                "heartbeat_age_ms": (
                    float(age_ms) if age_ms is not None else _HEARTBEAT_UNKNOWN
                ),
            }
        )
    return rows


def _conformal_row(control: Any) -> dict[str, Any]:
    strategy = str(getattr(control, "strategy_id", "spinal-scalper-v1"))
    mirror = getattr(control, "mirror", None)
    q98 = _Q98_COLD_START_BPS
    effective = _Q98_COLD_START_BPS
    samples = 0
    coverage_target = 0.98
    if mirror is not None:
        value = _safe(lambda: float(mirror.q98_bps(strategy)))
        if value is not None:
            q98 = value
        eff = _safe(lambda: float(mirror.effective_q98_bps(strategy)))
        if eff is None:
            eff = _safe(lambda: float(mirror.effective_q98_bps()))
        effective = float(eff) if eff is not None else q98
        snap = _safe(lambda: mirror.snapshot())
        if isinstance(snap, Mapping):
            samples = int(
                snap.get("samples")
                or snap.get("window_size")
                or snap.get("n")
                or 0
            )
            coverage_target = float(snap.get("coverage") or coverage_target)
    return {
        "strategy": strategy,
        "q98_bps": float(q98),
        "effective_q98_bps": float(effective),
        "samples": samples,
        "coverage_target": coverage_target,
    }


def _outbox_counts(control: Any) -> dict[str, int]:
    counts = {"pending": 0, "committed": 0, "voided": 0}
    outbox = getattr(control, "outbox", None)
    if outbox is None:
        return counts
    reservations = _safe(outbox.all_reservations) or []
    for reservation in reservations:
        state = getattr(reservation, "state", None)
        key = str(getattr(state, "value", state) or "").lower()
        if key in counts:
            counts[key] += 1
    return counts


def _token_row(control: Any) -> dict[str, Any]:
    cap = float(getattr(control, "llm_budget_usd", 3.10) or 3.10)
    spend = 0.0
    ledger = getattr(control, "token_ledger", None)
    if ledger is not None:
        snap = _safe(ledger.snapshot)
        if isinstance(snap, Mapping):
            spend = float(snap.get("daily_spend_usd") or 0.0)
            cap = float(snap.get("daily_cap_usd") or cap)
    else:
        state = _safe(getattr(control, "_llm_state", None) or (lambda: {}))
        if isinstance(state, Mapping):
            spend = float(state.get("daily_spend_usd") or 0.0)
            cap = float(state.get("daily_spend_cap_usd") or cap)
    return {
        "spend_usd": spend,
        "cap_usd": cap,
        "remaining_usd": max(0.0, cap - spend),
        "utilization": (spend / cap) if cap > 0 else 0.0,
    }


def _lease_row(control: Any) -> dict[str, Any]:
    table = getattr(control, "lease_table", None)
    if table is None:
        return {"active": 0, "slots_written": 0, "shm_uri": ""}
    active = _safe(lambda: int(table.active_count(time.monotonic_ns())), 0)
    written = _safe(lambda: int(table.slots_written), 0)
    return {
        "active": active,
        "slots_written": written,
        "shm_uri": str(getattr(table, "uri", "")),
    }


def _process_row() -> dict[str, Any]:
    """Best-effort process footprint for the cockpit telemetry chip."""
    row: dict[str, Any] = {
        "rss_mb": None,
        "python_heap_mb": None,
        "threads": None,
    }
    try:  # pragma: no cover - psutil is optional
        import psutil  # type: ignore[import-not-found]

        proc = psutil.Process()
        row["rss_mb"] = round(float(proc.memory_info().rss) / (1024.0 * 1024.0), 2)
        row["threads"] = int(proc.num_threads())
    except Exception:
        pass
    try:  # pragma: no cover - only meaningful while tracemalloc is running
        import tracemalloc

        current, _peak = tracemalloc.get_traced_memory()
        row["python_heap_mb"] = round(current / (1024.0 * 1024.0), 3)
    except Exception:
        pass
    return row


def _silo_rows(control: Any) -> list[dict[str, Any]]:
    ledger = getattr(control, "ledger", None)
    rows: list[dict[str, Any]] = []
    for silo in _canonical_silos(control):
        vaults: dict[str, int] = {name: 0 for name in _VAULT_ORDER}
        if ledger is not None:
            balances = _safe(lambda: ledger.vault_balances(silo)) or {}
            for key, value in dict(balances).items():
                label = str(getattr(key, "value", key))
                vaults[label] = int(value)
        nav_micros = _safe(lambda: int(ledger.silo_nav_micros(silo)), 0) if ledger else 0
        rows.append(
            {
                "silo": silo,
                "vaults": vaults,
                "nav_micros": nav_micros,
                "nav_total_micros": sum(vaults.values()),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


class MetricsExporter:
    """Read-only metric collector + Prometheus text renderer."""

    def __init__(
        self,
        control: Any,
        *,
        node: str = "nios-core",
        version: str = "3.0.0-PROD",
        document_id: str = "NIOS-ARC-2026-V3",
        latency_recorder: TickLatencyRecorder | None = None,
    ) -> None:
        self._control = control
        self.node = node
        self.version = version
        self.document_id = document_id
        self._latency = latency_recorder or TICK_LATENCY
        self._started_ns = time.time_ns()

    # -- public API ---------------------------------------------------------
    def collect(self) -> dict[str, Any]:
        """Gather one consistent snapshot of every metric family."""
        started = time.perf_counter_ns()
        control = self._control
        latency = self._latency.snapshot()
        engine = _engine_stats(control)
        heartbeat = _heartbeat_rows(control)
        conformal = _conformal_row(control)
        outbox = _outbox_counts(control)
        tokens = _token_row(control)
        leases = _lease_row(control)
        silos = _silo_rows(control)
        process = _process_row()

        ticks = int(engine.get("ticks") or 0)
        abstentions = int(engine.get("abstentions") or 0)
        payload = {
            "generated_at_ns": time.time_ns(),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "uptime_seconds": max(0.0, (time.time_ns() - self._started_ns) / 1e9),
            "node": self.node,
            "version": self.version,
            "document_id": self.document_id,
            "exposition": {
                "prometheus": "/api/v2/metrics",
                "alias": "/metrics",
                "json": "/api/v2/metrics/json",
                "content_type": CONTENT_TYPE_PROMETHEUS,
            },
            "tick_latency": latency,
            "rings": heartbeat,
            "conformal": conformal,
            "outbox": {**outbox, "total": sum(outbox.values())},
            "tokens": tokens,
            "leases": leases,
            "silos": silos,
            "process": process,
            "ring1": {
                "ticks": ticks,
                "abstentions": abstentions,
                "abstention_ratio": (abstentions / ticks) if ticks else 0.0,
                "shadow_fills": int(engine.get("shadow_fills") or 0),
                "live_proposals": int(engine.get("live_proposals") or 0),
                "ring0_rejects": int(engine.get("ring0_rejects") or 0),
                "breaker_trips": int(engine.get("breaker_trips") or 0),
            },
            "scrape_duration_seconds": max(
                0.0, (time.perf_counter_ns() - started) / 1e9
            ),
        }
        return payload

    def snapshot(self) -> dict[str, Any]:
        return self.collect()

    def render_prometheus(self) -> str:
        """Render the standard Prometheus text exposition format."""
        data = self.collect()
        out = _Builder()

        out.sample(
            "nios_build_info",
            1,
            help_text="NIOS build identity (constant 1 per scrape).",
            labels=(
                ("version", data["version"]),
                ("document_id", data["document_id"]),
                ("node", data["node"]),
            ),
        )
        out.sample(
            "nios_uptime_seconds",
            data["uptime_seconds"],
            help_text="Seconds since this exporter instance started.",
        )
        out.sample(
            "nios_metric_scrape_duration_seconds",
            data["scrape_duration_seconds"],
            help_text="Wall-clock cost of collecting this scrape.",
        )
        process = data["process"]
        out.sample(
            "nios_process_rss_mb",
            process["rss_mb"],
            help_text="Resident set size of the cockpit gateway process in MB.",
        )
        out.sample(
            "nios_process_heap_mb",
            process["python_heap_mb"],
            help_text="Traced Python heap of the gateway process in MB (0 when off).",
        )
        out.sample(
            "nios_process_threads",
            process["threads"],
            help_text="OS threads owned by the cockpit gateway process.",
        )
        out.blank()

        # ---- Ring 1 tick-to-intent latency: histogram + quantiles ----------
        latency = data["tick_latency"]
        out.family(
            "nios_tick_latency_seconds",
            "histogram",
            "Ring 1 tick-to-intent latency in seconds (sec_010 SLA: 0.00014 p99).",
        )
        for bucket in latency["buckets"]:
            out._lines.append(  # noqa: SLF001 - local builder, explicit histogram
                "nios_tick_latency_seconds_bucket"
                + _labels((("le", _fmt(bucket["le"])),))
                + f" {_fmt(bucket['count'])}"
            )
        out._lines.append(
            'nios_tick_latency_seconds_bucket{le="+Inf"} '
            + _fmt(latency["count"])
        )
        out._lines.append(
            "nios_tick_latency_seconds_sum " + _fmt(latency["sum_seconds"])
        )
        out._lines.append(
            "nios_tick_latency_seconds_count " + _fmt(latency["count"])
        )
        out.family(
            "nios_tick_latency_quantiles_seconds",
            "gauge",
            "Observed Ring 1 tick-to-intent quantiles in seconds.",
        )
        for quantile, label in (("0.5", "p50"), ("0.9", "p90"), ("0.99", "p99"), ("0.999", "p99_9")):
            value = latency["quantiles"].get(label, 0.0)
            out._lines.append(
                "nios_tick_latency_quantiles_seconds"
                + _labels((("quantile", quantile),))
                + f" {_fmt(value)}"
            )
        out.sample(
            "nios_tick_latency_p99_within_budget",
            1 if latency.get("p99_within_budget") else 0,
            help_text="1 when the observed p99 is inside the 0.14 ms Ring 1 SLA.",
        )
        out.blank()

        # ---- Ring heartbeats ----------------------------------------------
        out.family(
            "nios_ring_heartbeat_age_ms",
            "gauge",
            "Age of the last heartbeat per ring in milliseconds (-1 = never written).",
        )
        out.family(
            "nios_ring_heartbeat_alive",
            "gauge",
            "1 when the ring heartbeat is inside the supervisor timeout window.",
        )
        for row in data["rings"]:
            labels = (("ring", row["ring"]), ("worker", row["worker"]))
            out._lines.append(
                "nios_ring_heartbeat_age_ms" + _labels(labels) + f" {_fmt(row['heartbeat_age_ms'])}"
            )
            out._lines.append(
                "nios_ring_heartbeat_alive"
                + _labels(labels)
                + f" {1 if row['alive'] else 0}"
            )
        out.blank()

        # ---- Conformal Q_0.98 ---------------------------------------------
        conformal = data["conformal"]
        labels = (("strategy", conformal["strategy"]),)
        out.sample(
            "nios_conformal_q98_bps",
            conformal["q98_bps"],
            help_text="Rolling 98% split-conformal residual bound Q_0.98 in bps.",
            labels=labels,
        )
        out.sample(
            "nios_conformal_effective_q98_bps",
            conformal["effective_q98_bps"],
            help_text="Effective Q_0.98 after cold-start shrinkage, in bps.",
            labels=labels,
        )
        out.sample(
            "nios_conformal_window_samples",
            conformal["samples"],
            help_text="Samples currently inside the conformal residual window.",
            labels=labels,
        )
        out.blank()

        # ---- Two-phase outbox ---------------------------------------------
        out.family(
            "nios_outbox_reservations_total",
            "counter",
            "Atomic outbox reservations observed by terminal state.",
        )
        for state in ("pending", "committed", "voided"):
            out._lines.append(
                "nios_outbox_reservations_total"
                + _labels((("state", state),))
                + f" {_fmt(data['outbox'][state])}"
            )
        out.sample(
            "nios_outbox_reservations",
            data["outbox"]["pending"],
            help_text="Reservations currently pending (funds locked).",
            labels=(("state", "pending"),),
        )
        out.blank()

        # ---- Token spend ---------------------------------------------------
        tokens = data["tokens"]
        out.sample(
            "nios_token_spend_usd_total",
            tokens["spend_usd"],
            help_text="Realised daily LLM spend in USD (sec_013 budget 3.10).",
        )
        out.sample(
            "nios_token_budget_cap_usd",
            tokens["cap_usd"],
            help_text="Daily LLM spend cap in USD.",
        )
        out.sample(
            "nios_token_budget_remaining_usd",
            tokens["remaining_usd"],
            help_text="Remaining daily LLM budget in USD.",
        )
        out.sample(
            "nios_token_budget_utilization_ratio",
            tokens["utilization"],
            help_text="Daily spend divided by the daily cap.",
        )
        out.blank()

        # ---- Leases --------------------------------------------------------
        out.sample(
            "nios_active_leases",
            data["leases"]["active"],
            help_text="Armed, unexpired System1Lease entries in LeaseTableSHM.",
        )
        out.sample(
            "nios_lease_slots_written",
            data["leases"]["slots_written"],
            help_text="Distinct LeaseTableSHM slots written since boot.",
        )
        out.blank()

        # ---- 128-bit NAV per silo / vault ----------------------------------
        out.family(
            "nios_silo_nav_micros",
            "gauge",
            "Silo NAV in 128-bit micro-units, per vault (sec_009).",
        )
        for silo in data["silos"]:
            for vault in _VAULT_ORDER:
                out._lines.append(
                    "nios_silo_nav_micros"
                    + _labels((("silo", silo["silo"]), ("vault", vault)))
                    + f" {_fmt(silo['vaults'].get(vault, 0))}"
                )
        out.family(
            "nios_silo_nav_total_micros",
            "gauge",
            "Total silo NAV in 128-bit micro-units across the four vaults.",
        )
        for silo in data["silos"]:
            out._lines.append(
                "nios_silo_nav_total_micros"
                + _labels((("silo", silo["silo"]),))
                + f" {_fmt(silo['nav_total_micros'])}"
            )
        out.blank()

        # ---- Ring 1 funnel counters ---------------------------------------
        ring1 = data["ring1"]
        out.family("nios_ticks_total", "counter", "Tier-0 ticks evaluated by Ring 1.")
        out._lines.append(f"nios_ticks_total {_fmt(ring1['ticks'])}")
        out.family(
            "nios_abstentions_total",
            "counter",
            "Tier-0 abstentions (Law 4: the default answer is no trade).",
        )
        out._lines.append(f"nios_abstentions_total {_fmt(ring1['abstentions'])}")
        out.sample(
            "nios_abstention_ratio",
            ring1["abstention_ratio"],
            help_text="Abstentions divided by evaluated ticks.",
        )
        out.family(
            "nios_shadow_fills_total",
            "counter",
            "Shadow-fill graduations recorded by the Ring 1 twin.",
        )
        out._lines.append(f"nios_shadow_fills_total {_fmt(ring1['shadow_fills'])}")
        out.family(
            "nios_live_proposals_total",
            "counter",
            "Unsigned scalp proposals emitted to Ring 0.",
        )
        out._lines.append(
            f"nios_live_proposals_total {_fmt(ring1['live_proposals'])}"
        )
        out.family(
            "nios_ring0_rejects_total",
            "counter",
            "Proposals rejected by the Ring 0 conformal / copula gate.",
        )
        out._lines.append(f"nios_ring0_rejects_total {_fmt(ring1['ring0_rejects'])}")
        out.family(
            "nios_breaker_trips_total",
            "counter",
            "Microstructure breaker trips observed by Ring 1.",
        )
        out._lines.append(
            f"nios_breaker_trips_total {_fmt(ring1['breaker_trips'])}"
        )

        return out.text()


# ---------------------------------------------------------------------------
# FastAPI wiring
# ---------------------------------------------------------------------------


def register_metrics_routes(app: Any, control: Any) -> MetricsExporter:
    """Attach the three read-only metric routes to ``app``.

    Must be called *before* the SPA ``StaticFiles`` mount at ``/`` so the
    catch-all mount cannot shadow ``/metrics``.
    """
    from fastapi import Response

    exporter = MetricsExporter(control)

    def prometheus_endpoint() -> Response:
        return Response(
            content=exporter.render_prometheus(),
            media_type=CONTENT_TYPE_PROMETHEUS,
            headers={"Cache-Control": "no-store"},
        )

    def json_endpoint() -> dict[str, Any]:
        return exporter.snapshot()

    app.add_api_route(
        "/api/v2/metrics",
        prometheus_endpoint,
        methods=["GET"],
        include_in_schema=False,
        name="nios-metrics",
    )
    app.add_api_route(
        "/metrics",
        prometheus_endpoint,
        methods=["GET"],
        include_in_schema=False,
        name="nios-metrics-alias",
    )
    app.add_api_route(
        "/api/v2/metrics/json",
        json_endpoint,
        methods=["GET"],
        include_in_schema=False,
        name="nios-metrics-json",
    )
    try:
        app.state.metrics_exporter = exporter
    except Exception:  # pragma: no cover - exotic ASGI app
        pass
    return exporter
