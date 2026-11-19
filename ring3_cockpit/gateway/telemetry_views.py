"""Gateway telemetry composition (V2.3 PART 3).

One place that turns the four measured sources into the payload block the
cockpit reads, so ``/api/v2/status``, ``/api/v2/cockpit/state``,
``/api/v2/cockpit/overview`` and the supervisor payload all publish the *same*
figures instead of four subtly different derivations.

Six fields, all measured, none invented:

``latency_ms`` / ``ring0_p99_ms`` .. ``ring3_p99_ms``
    :class:`core.telemetry.GatewayTelemetryTracker` quantiles.  ``None`` when
    that ring has not been exercised in this process.
``latency_p50_ms`` / ``latency_p95_ms``
    The same reservoirs at the other two quantiles.
``feed_lag_ms`` / ``feed_lag_status``
    Real tick age from :class:`LiveBarAggregator`; ``None`` + ``IDLE`` before
    the first tick.
``pending_intents_count``
    ``CrashAtomicOutbox.pending_count`` + ``ProfitSplitSagaCoordinator``
    ``PENDING_SWEEP_VERIFICATION`` + ``UnknownOrderQueue.active_count``.
``adwin_drift``
    :meth:`ConceptDriftMonitor.telemetry` per strategy.
``iv_rank_bps``
    Trailing 52-week min-max rank (see :mod:`ring2_brain.quant.options_iv_rank`).

Every reader is duck-typed and defensive: a component that is not attached
contributes ``None``/``0``, never an exception and never a stand-in number.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from core.telemetry import gateway_telemetry, iso_utc_from_ns

__all__ = [
    "adwin_drift_payload",
    "feed_lag_payload",
    "latency_fields",
    "pending_intents_payload",
    "telemetry_fields",
]

_FEED_IDLE = "IDLE"
_FEED_LIVE = "LIVE"


# ---------------------------------------------------------------------------
# Latency (all four rings)
# ---------------------------------------------------------------------------
def latency_fields(
    *, request_started_ns: int | None = None, request: Any = None
) -> dict[str, Any]:
    """Ring 3 latency block + the flat per-ring p99/p50/p95 keys.

    ``request_started_ns`` is the middleware's ``perf_counter_ns`` stamp for the
    request currently being answered.  Recording it here *and* returning the
    snapshot means a bare ``GET /api/v2/status`` on a cold process already
    reports the real latency of the request that is being answered, instead of
    an all-``null`` block that the UI would have to render as ``—``.
    """
    tracker = gateway_telemetry()
    if request_started_ns and not latency_already_recorded(request):
        tracker.record(
            3, time.perf_counter_ns() - int(request_started_ns), metric="http_request"
        )
        if request is not None:
            try:
                request.state.nios_latency_recorded = True
            except Exception:  # pragma: no cover - a bare object has no state
                pass
    snapshot = tracker.snapshot(overall_ring=3)
    return {
        "latency_ms": snapshot["latency_ms"],
        "latency_detail": snapshot["latency_detail"],
        "latency_last_ms": snapshot["latency_last_ms"],
        "ring0_p99_ms": snapshot["ring0_p99_ms"],
        "ring1_p99_ms": snapshot["ring1_p99_ms"],
        "ring2_p99_ms": snapshot["ring2_p99_ms"],
        "ring3_p99_ms": snapshot["ring3_p99_ms"],
        "latency_p50_ms": snapshot["latency_p50_ms"],
        "latency_p95_ms": snapshot["latency_p95_ms"],
        "latency_p99_ms": snapshot["latency_p99_ms"],
        "latency_ring_samples": snapshot["ring_samples"],
        "telemetry": snapshot,
    }


# ---------------------------------------------------------------------------
# Feed lag
# ---------------------------------------------------------------------------
def feed_lag_payload(control: Any, *, now_ns: int | None = None) -> dict[str, Any]:
    """Real per-silo tick age from the Ring 2 bar aggregator."""
    stamp = int(now_ns if now_ns is not None else time.time_ns())
    aggregator = None
    try:
        aggregator = getattr(control, "bar_aggregator", None)
    except Exception:  # pragma: no cover - optional component
        aggregator = None
    telemetry = None
    if aggregator is not None:
        describe = getattr(aggregator, "feed_telemetry", None)
        if callable(describe):
            try:
                telemetry = dict(describe(now_ns=stamp) or {})
            except Exception:  # pragma: no cover - telemetry is non-fatal
                telemetry = None
    if not telemetry:
        return {
            "feed_lag_ms": None,
            "feed_lag_status": _FEED_IDLE,
            "feed_lag_by_silo": {},
            "feeds": [],
            "feed_as_of_utc": iso_utc_from_ns(stamp),
        }
    lag = telemetry.get("feed_lag_ms")
    by_silo = dict(telemetry.get("feed_lag_by_silo") or {})
    return {
        "feed_lag_ms": None if lag is None else round(float(lag), 3),
        "feed_lag_status": str(telemetry.get("feed_lag_status") or _FEED_LIVE),
        "feed_lag_by_silo": by_silo,
        "feeds": list(telemetry.get("feeds") or []),
        "feed_instruments": int(telemetry.get("feed_instruments", 0) or 0),
        "feed_ticks_ingested": int(telemetry.get("feed_ticks_ingested", 0) or 0),
        "feed_as_of_utc": iso_utc_from_ns(stamp),
    }


# ---------------------------------------------------------------------------
# Pending intents
# ---------------------------------------------------------------------------
def pending_intents_payload(control: Any) -> dict[str, Any]:
    """Unresolved outbox reservations + pending sweeps + unknown orders.

    Components are read only when they are already attached: a lazily-built
    saga coordinator is *not* constructed just to count its (necessarily empty)
    journals, so the figure never changes because someone opened a page.
    """
    outbox_count = 0
    outbox = getattr(control, "outbox", None)
    if outbox is not None:
        try:
            # V2.3: the outbox reservation table IS Ring 0 state, so reading it
            # is a real (if tiny) Ring 0 span, not a modelled one.
            with gateway_telemetry().measure(0, "outbox_pending_read"):
                pending = getattr(outbox, "pending_count", 0)
                outbox_count = int(pending() if callable(pending) else pending)
        except Exception:  # pragma: no cover - telemetry is non-fatal
            outbox_count = 0

    sweep_count = 0
    saga = getattr(control, "_profit_split_saga", None)
    if saga is not None:
        describe = getattr(saga, "status", None)
        if callable(describe):
            try:
                with gateway_telemetry().measure(0, "saga_state_read"):
                    counts = dict((describe() or {}).get("state_counts") or {})
                sweep_count = int(counts.get("PENDING_SWEEP_VERIFICATION", 0) or 0)
            except Exception:  # pragma: no cover - telemetry is non-fatal
                sweep_count = 0

    unknown_count = 0
    worker = getattr(control, "_lifecycle_worker", None)
    queue = getattr(worker, "unknown_orders", None) if worker is not None else None
    if queue is not None:
        try:
            active = getattr(queue, "active_count", None)
            if active is not None:
                unknown_count = int(active() if callable(active) else active)
            else:
                status = dict(queue.status() or {})
                unknown_count = int(status.get("count", 0) or 0) - int(
                    (status.get("state_counts") or {}).get("RESOLVED", 0) or 0
                )
        except Exception:  # pragma: no cover - telemetry is non-fatal
            unknown_count = 0

    total = max(0, outbox_count) + max(0, sweep_count) + max(0, unknown_count)
    return {
        "pending_intents_count": int(total),
        "pending_intents": {
            "outbox_pending": max(0, outbox_count),
            "pending_sweep_verification": max(0, sweep_count),
            "unknown_orders_active": max(0, unknown_count),
            "sources": [
                "CrashAtomicOutbox.pending_count",
                "ProfitSplitSagaCoordinator.status.state_counts",
                "UnknownOrderQueue.active_count",
            ],
        },
    }


# ---------------------------------------------------------------------------
# ADWIN / concept drift
# ---------------------------------------------------------------------------
def adwin_drift_payload(
    control: Any, strategies: Sequence[str] | None = None
) -> dict[str, dict[str, Any]]:
    """``{strategy_id: telemetry}`` from the live ``ConceptDriftMonitor``s."""
    registry = getattr(control, "_drift_monitors", None) or {}
    factory = getattr(control, "concept_drift_monitor", None)
    names = (
        sorted(registry.keys()) if strategies is None else [str(s) for s in strategies]
    )
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        monitor = registry.get(str(name))
        if monitor is None and callable(factory):
            try:
                monitor = factory(str(name))
            except Exception:  # pragma: no cover - telemetry is non-fatal
                monitor = None
        if monitor is None:
            continue
        describe = getattr(monitor, "telemetry", None)
        if not callable(describe):
            continue
        try:
            out[str(name)] = dict(describe() or {})
        except Exception:  # pragma: no cover - telemetry is non-fatal
            continue
    return out


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------
def telemetry_fields(
    control: Any,
    *,
    request: Any = None,
    request_started_ns: int | None = None,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """The six V2.3 telemetry fields, ready to merge into any cockpit payload."""
    payload: dict[str, Any] = {}
    # Order matters: the measured reads below are part of *this* request, so
    # they are recorded before the latency snapshot is taken and the payload
    # reports the work it actually just did.
    payload.update(feed_lag_payload(control, now_ns=now_ns))
    payload.update(pending_intents_payload(control))
    payload.update(latency_fields(request_started_ns=request_started_ns, request=request))
    return payload


def request_started_ns_of(request: Any) -> int | None:
    """The ``perf_counter_ns`` stamp the latency middleware stored, if any."""
    if request is None:
        return None
    try:
        value = getattr(request.state, "nios_telemetry_started_ns", None)
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def latency_already_recorded(request: Any) -> bool:
    """Whether the handler already recorded this request's span."""
    if request is None:
        return False
    try:
        return bool(getattr(request.state, "nios_latency_recorded", False))
    except Exception:  # pragma: no cover - defensive
        return False
