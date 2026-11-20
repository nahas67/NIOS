"""Shared latency telemetry substrate (``sec_004`` / ``sec_016``).

Ring 1 owns the hot path but must not import anything from Ring 3.  This
module lives in ``core/`` -- the shared substrate -- so the deterministic
spinal loop can publish per-tick latency without ever touching the cockpit
package, and the Ring 3 metrics exporter can read the same numbers.

The recorder is a fixed-bucket histogram plus a bounded reservoir used only
for quantile estimation.  ``observe`` is O(1), allocates nothing on the hot
path, and never raises.

``GatewayTelemetryTracker`` (V2.3) is the second recorder: bounded
``deque(maxlen=512)`` ring buffers per ring (0..3) and per named metric, fed by
real ``time.perf_counter_ns()`` spans, exposing exact p50/p95/p99 in
**milliseconds**.  Every ring in the four-ring topology writes into it:

* Ring 0 -- sovereign pre-trade firewall, outbox reserve, OMS execution;
* Ring 1 -- SHM SeqLock / lease-table / kill-latch reads;
* Ring 2 -- agent dispatch, quant scan, contagion snapshot;
* Ring 3 -- the FastAPI request middleware.

It lives in ``core/`` so no ring has to import another ring to publish a
duration.  When a buffer is empty its quantiles are ``None`` -- an unmeasured
latency is not ``0.0 ms``, and a fabricated percentile sealed inside a
``payload_sha256`` is indistinguishable from a measured one.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_TICK_BUCKETS",
    "GATEWAY_RING_MAXLEN",
    "GATEWAY_TELEMETRY_SCHEMA",
    "GatewayLatencySpan",
    "GatewayTelemetryTracker",
    "LatencyASGIMiddleware",
    "QUANTILE_KEYS",
    "TickLatencyRecorder",
    "TICK_LATENCY",
    "gateway_telemetry",
    "iso_utc_from_ns",
    "observe_tick_latency_ns",
    "observe_tick_latency_s",
    "reset_gateway_telemetry",
    "tick_latency_snapshot",
]

#: Histogram bounds in **seconds**.  The Ring 1 SLA is 0.14 ms p99, so the
#: buckets cluster tightly around the 50-140 us band and then fan out to 1 ms.
DEFAULT_TICK_BUCKETS: tuple[float, ...] = (
    0.000_025,  # 25 us
    0.000_050,  # 50 us
    0.000_075,  # 75 us
    0.000_088,  # 88 us -- documented p50
    0.000_100,  # 100 us
    0.000_125,  # 125 us
    0.000_140,  # 140 us -- documented p99
    0.000_175,  # 175 us
    0.000_250,  # 250 us -- System 1 hard cap
    0.000_500,  # 500 us
    0.001_000,  # 1 ms
)

#: Quantiles surfaced by ``GET /api/v2/metrics`` and the JSON telemetry feed.
QUANTILE_KEYS: tuple[tuple[str, float], ...] = (
    ("p50", 0.50),
    ("p90", 0.90),
    ("p99", 0.99),
    ("p99_9", 0.999),
)

_TREND_RING = 32


class TickLatencyRecorder:
    """Bounded, thread-safe histogram of Ring 1 tick-to-intent latency."""

    __slots__ = (
        "_buckets",
        "_counts",
        "_sum_s",
        "_count",
        "_reservoir",
        "_lock",
        "_trend",
    )

    def __init__(
        self,
        buckets: Sequence[float] = DEFAULT_TICK_BUCKETS,
        *,
        reservoir: int = 4096,
    ) -> None:
        self._buckets = tuple(float(b) for b in buckets)
        self._counts = [0] * len(self._buckets)
        self._sum_s = 0.0
        self._count = 0
        self._reservoir: deque[float] = deque(maxlen=max(64, int(reservoir)))
        self._lock = threading.Lock()
        self._trend: deque[float] = deque(maxlen=_TREND_RING)

    # -- write path ---------------------------------------------------------
    def observe_ns(self, elapsed_ns: int) -> None:
        """Record one tick-to-intent sample expressed in nanoseconds."""
        try:
            self.observe_s(int(elapsed_ns) / 1_000_000_000.0)
        except Exception:  # pragma: no cover - telemetry must never raise
            pass

    def observe_s(self, elapsed_s: float) -> None:
        """Record one sample expressed in seconds."""
        value = float(elapsed_s)
        if not math.isfinite(value) or value < 0.0:
            return
        with self._lock:
            for idx, bound in enumerate(self._buckets):
                if value <= bound:
                    # Non-cumulative buckets: only the first matching bound is
                    # incremented, so the exporter stays Prometheus-correct.
                    self._counts[idx] += 1
                    break
            else:
                # Samples above the final finite bound belong EXCLUSIVELY to
                # the implicit +Inf bucket, which the metrics exporter emits
                # explicitly from ``count`` (see metrics_exporter.py). Do NOT
                # fold them into the last finite bucket: that double-counts
                # overflow in Prometheus, makes le=0.001 read as the total,
                # and masks a sec_010 tick-latency budget breach.
                pass
            self._sum_s += value
            self._count += 1
            self._reservoir.append(value)
            self._trend.append(value)

    def reset(self) -> None:
        with self._lock:
            self._counts = [0] * len(self._buckets)
            self._sum_s = 0.0
            self._count = 0
            self._reservoir.clear()
            self._trend.clear()

    # -- read path ----------------------------------------------------------
    def quantile(self, q: float) -> float:
        """Nearest-rank quantile over the bounded reservoir."""
        with self._lock:
            samples = sorted(self._reservoir)
        if not samples:
            return 0.0
        rank = int(math.ceil(float(q) * len(samples))) - 1
        rank = max(0, min(rank, len(samples) - 1))
        return samples[rank]

    def snapshot(self) -> dict[str, Any]:
        """Serialisable histogram + quantile view for exporters and tests."""
        with self._lock:
            counts = list(self._counts)
            total = self._count
            total_s = self._sum_s
            trend = list(self._trend)

        cumulative = 0
        buckets: list[dict[str, Any]] = []
        for bound, count in zip(self._buckets, counts):
            cumulative += count
            buckets.append({"le": bound, "count": cumulative})

        # Nearest-rank quantiles on a consistent copy of the reservoir.
        with self._lock:
            samples = sorted(self._reservoir)
        quantiles: dict[str, float] = {}
        for key, q in QUANTILE_KEYS:
            if samples:
                rank = max(0, min(int(math.ceil(q * len(samples))) - 1, len(samples) - 1))
                quantiles[key] = samples[rank]
            else:
                quantiles[key] = 0.0

        return {
            "count": total,
            "sum_seconds": total_s,
            "mean_seconds": (total_s / total) if total else 0.0,
            "buckets": buckets,
            "quantiles": quantiles,
            "quantiles_ns": {k: v * 1_000_000_000.0 for k, v in quantiles.items()},
            "recent_mean_seconds": (sum(trend) / len(trend)) if trend else 0.0,
            "p99_budget_seconds": 0.000_140,
            "p99_within_budget": (
                (quantiles.get("p99", 0.0) <= 0.000_140) if total else True
            ),
            "sample_capacity": self._reservoir.maxlen or 0,
        }


#: Process-wide recorder.  Ring 1 writes, Ring 3 reads, the soak harness audits.
TICK_LATENCY = TickLatencyRecorder()


def observe_tick_latency_ns(elapsed_ns: int) -> None:
    """Module-level convenience wrapper around the process-wide recorder."""
    TICK_LATENCY.observe_ns(elapsed_ns)


def observe_tick_latency_s(elapsed_s: float) -> None:
    TICK_LATENCY.observe_s(elapsed_s)


def tick_latency_snapshot() -> dict[str, Any]:
    return TICK_LATENCY.snapshot()


# ===========================================================================
# V2.3 -- GatewayTelemetryTracker (all four rings)
# ===========================================================================

#: ``sec_004`` / V2.3 PART 3: every ring keeps a bounded reservoir so the
#: cockpit can publish an exact p99 without unbounded memory growth.
GATEWAY_RING_MAXLEN: int = 512

#: Wire contract for the ``telemetry`` block the gateway seals.
GATEWAY_TELEMETRY_SCHEMA: str = "v2.3.gateway-telemetry"

_NS_PER_MS = 1_000_000.0

#: The four rings, in topology order.
_GATEWAY_RINGS: tuple[int, ...] = (0, 1, 2, 3)


def iso_utc_from_ns(stamp_ns: Any) -> str | None:
    """ISO-8601 UTC second-resolution stamp, or ``None`` for "never"."""
    try:
        value = int(stamp_ns)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return (
        datetime.fromtimestamp(value / 1_000_000_000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _nearest_rank_ms(ordered: Sequence[int], q: float, total: int) -> float | None:
    """Nearest-rank quantile of a sorted nanosecond list, in milliseconds."""
    if total <= 0:
        return None
    rank = int(math.ceil(float(q) * total)) - 1
    rank = max(0, min(rank, total - 1))
    return round(int(ordered[rank]) / _NS_PER_MS, 4)


class GatewayLatencySpan:
    """One measured span; records into a :class:`GatewayTelemetryTracker`."""

    __slots__ = ("_tracker", "_ring", "_metric", "_started_ns")

    def __init__(
        self,
        tracker: "GatewayTelemetryTracker",
        ring: int,
        metric: str | None,
        started_ns: int,
    ) -> None:
        self._tracker = tracker
        self._ring = ring
        self._metric = metric
        self._started_ns = started_ns

    def __enter__(self) -> "GatewayLatencySpan":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._tracker.record(
            self._ring, time.perf_counter_ns() - self._started_ns, self._metric
        )
        return False

    #: Elapsed nanoseconds since the span opened (never negative).
    def elapsed_ns(self) -> int:
        return max(0, time.perf_counter_ns() - self._started_ns)


class GatewayTelemetryTracker:
    """Bounded, thread-safe per-ring + per-metric latency ledger.

    ``record`` is the only method on the write path: one ``deque.append`` and a
    handful of attribute touches, no interpolation and no sorting.  Quantiles
    are computed on a copied, sorted reservoir, so the read path (a cockpit
    payload) never mutates the hot data.
    """

    __slots__ = ("_metric_maxlen", "_metrics", "_rings", "_lock", "_started_ns")

    def __init__(
        self,
        *,
        maxlen: int = GATEWAY_RING_MAXLEN,
        metric_maxlen: int | None = None,
    ) -> None:
        cap = max(8, int(maxlen))
        self._metric_maxlen = max(8, int(metric_maxlen if metric_maxlen else cap))
        self._lock = threading.Lock()
        self._rings: dict[int, deque[int]] = {
            ring: deque(maxlen=cap) for ring in _GATEWAY_RINGS
        }
        self._metrics: dict[str, deque[int]] = {}
        self._started_ns = time.time_ns()

    # -- write path ---------------------------------------------------------
    def record(self, ring: int, duration_ns: Any, metric: str | None = None) -> None:
        """Record one real measured duration.  Never raises."""
        try:
            elapsed = int(duration_ns)
        except (TypeError, ValueError):
            return
        if elapsed < 0:
            return
        try:
            key = int(ring)
        except (TypeError, ValueError):
            return
        with self._lock:
            buffer = self._rings.get(key)
            if buffer is not None:
                buffer.append(elapsed)
            if metric:
                name = str(metric)
                named = self._metrics.get(name)
                if named is None:
                    named = deque(maxlen=self._metric_maxlen)
                    self._metrics[name] = named
                named.append(elapsed)

    def measure(self, ring: int, metric: str | None = None) -> GatewayLatencySpan:
        """Context manager that records one real span into ``ring``/``metric``."""
        return GatewayLatencySpan(self, int(ring), metric, time.perf_counter_ns())

    def reset(self) -> None:
        """Drop every sample (tests and the clean-slate reset)."""
        with self._lock:
            for buffer in self._rings.values():
                buffer.clear()
            self._metrics.clear()
            self._started_ns = time.time_ns()

    # -- read path ----------------------------------------------------------
    def ring_samples(self, ring: int) -> list[int]:
        with self._lock:
            buffer = self._rings.get(int(ring))
            return [] if buffer is None else list(buffer)

    def metric_samples(self, metric: str) -> list[int]:
        with self._lock:
            buffer = self._metrics.get(str(metric))
            return [] if buffer is None else list(buffer)

    def count(self, ring: int) -> int:
        with self._lock:
            buffer = self._rings.get(int(ring))
            return 0 if buffer is None else len(buffer)

    @staticmethod
    def _stats(samples: Sequence[int]) -> dict[str, Any]:
        total = len(samples)
        if total == 0:
            return {
                "count": 0,
                "last_ms": None,
                "min_ms": None,
                "max_ms": None,
                "p50_ms": None,
                "p95_ms": None,
                "p99_ms": None,
            }
        ordered = sorted(int(value) for value in samples)
        return {
            "count": total,
            "last_ms": round(int(samples[-1]) / _NS_PER_MS, 4),
            "min_ms": round(ordered[0] / _NS_PER_MS, 4),
            "max_ms": round(ordered[-1] / _NS_PER_MS, 4),
            "p50_ms": _nearest_rank_ms(ordered, 0.50, total),
            "p95_ms": _nearest_rank_ms(ordered, 0.95, total),
            "p99_ms": _nearest_rank_ms(ordered, 0.99, total),
        }

    def percentile(self, ring: int, q: float) -> float | None:
        """Exact nearest-rank quantile for one ring, in milliseconds."""
        samples = self.ring_samples(ring)
        return _nearest_rank_ms(sorted(samples), float(q), len(samples))

    def last_ms(self, ring: int) -> float | None:
        samples = self.ring_samples(ring)
        return round(samples[-1] / _NS_PER_MS, 4) if samples else None

    def max_ms(self, ring: int) -> float | None:
        samples = self.ring_samples(ring)
        return round(max(samples) / _NS_PER_MS, 4) if samples else None

    def snapshot(self, *, overall_ring: int = 3) -> dict[str, Any]:
        """Full telemetry view: per-ring quantiles + per-metric quantiles."""
        with self._lock:
            rings = {ring: list(buffer) for ring, buffer in self._rings.items()}
            metrics = {name: list(buffer) for name, buffer in self._metrics.items()}
        per_ring = {f"ring{ring}": self._stats(rings.get(ring, ())) for ring in _GATEWAY_RINGS}
        named = {name: self._stats(samples) for name, samples in sorted(metrics.items())}
        overall_key = f"ring{int(overall_ring)}"
        overall = per_ring.get(overall_key) or self._stats(())
        return {
            "schema": GATEWAY_TELEMETRY_SCHEMA,
            "generated_at_ns": time.time_ns(),
            "generated_at_utc": iso_utc_from_ns(time.time_ns()),
            "sample_capacity": GATEWAY_RING_MAXLEN,
            # The UI's single LATENCY tile: the Ring 3 HTTP request round trip,
            # as one number (``payload.latency_ms``), with the quantiles kept
            # beside it so the cockpit can render last/p50/p95/p99 if it wants.
            "latency_ms": overall["last_ms"],
            "latency_detail": {
                "ring": int(overall_ring),
                "count": int(overall["count"]),
                "last": overall["last_ms"],
                "p50": overall["p50_ms"],
                "p95": overall["p95_ms"],
                "p99": overall["p99_ms"],
                "max": overall["max_ms"],
            },
            "latency_last_ms": overall["last_ms"],
            "ring0_p99_ms": per_ring["ring0"]["p99_ms"],
            "ring1_p99_ms": per_ring["ring1"]["p99_ms"],
            "ring2_p99_ms": per_ring["ring2"]["p99_ms"],
            "ring3_p99_ms": per_ring["ring3"]["p99_ms"],
            "latency_p50_ms": {
                f"ring{ring}": per_ring[f"ring{ring}"]["p50_ms"] for ring in _GATEWAY_RINGS
            },
            "latency_p95_ms": {
                f"ring{ring}": per_ring[f"ring{ring}"]["p95_ms"] for ring in _GATEWAY_RINGS
            },
            "latency_p99_ms": {
                f"ring{ring}": per_ring[f"ring{ring}"]["p99_ms"] for ring in _GATEWAY_RINGS
            },
            "ring_samples": {
                f"ring{ring}": per_ring[f"ring{ring}"]["count"] for ring in _GATEWAY_RINGS
            },
            "per_ring": per_ring,
            "metrics": named,
        }


_GATEWAY_TELEMETRY_LOCK = threading.Lock()
_GATEWAY_TELEMETRY: GatewayTelemetryTracker | None = None


def gateway_telemetry() -> GatewayTelemetryTracker:
    """Process-wide tracker singleton (created once, under a lock)."""
    global _GATEWAY_TELEMETRY
    tracker = _GATEWAY_TELEMETRY
    if tracker is None:
        with _GATEWAY_TELEMETRY_LOCK:
            tracker = _GATEWAY_TELEMETRY
            if tracker is None:
                tracker = GatewayTelemetryTracker()
                _GATEWAY_TELEMETRY = tracker
    return tracker


def reset_gateway_telemetry() -> GatewayTelemetryTracker:
    """Clear the singleton's samples and return it (tests / clean slate)."""
    tracker = gateway_telemetry()
    tracker.reset()
    return tracker


# ===========================================================================
# V2.4 -- pure ASGI Ring 3 latency middleware
# ===========================================================================


#: Scope key the handlers read through ``request.state.nios_telemetry_started_ns``.
_STATE_STARTED = "nios_telemetry_started_ns"

#: Scope key a handler sets when it already recorded its own in-flight span.
_STATE_RECORDED = "nios_latency_recorded"


class LatencyASGIMiddleware:
    """One real Ring 3 latency sample per HTTP request -- no body buffering.

    This replaces the Starlette ``@app.middleware("http")`` hook from V2.3.
    That hook is a :class:`starlette.middleware.base.BaseHTTPMiddleware` under
    the hood, which spawns a task group and pumps the response body through an
    anyio memory-object stream for **every** request.  A 60 fps SSE hub paid
    that cost on every frame even though it only needs a stamp.

    The pure-ASGI contract here is narrow on purpose:

    * ``scope["type"] != "http"`` is forwarded untouched (``lifespan``,
      ``websocket``) before anything is measured;
    * ``scope["state"]`` is created if missing and stamped with
      ``nios_telemetry_started_ns``, so ``request.state.nios_telemetry_started_ns``
      keeps working for :mod:`ring3_cockpit.gateway.telemetry_views`;
    * the sample is recorded when ``http.response.start`` is sent.  For a
      normal response that is the response head; for SSE it is the first frame
      header, so a stream is measured from first byte and never delayed;
    * ``receive`` is never wrapped and no body bytes are copied: the pass-through
      path allocates one closure and nothing else;
    * a handler that records its own span first sets
      ``state["nios_latency_recorded"] = True`` and is not double-counted;
    * an exception records ``error_metric`` and is re-raised unchanged.
    """

    __slots__ = ("app", "error_metric", "metric", "ring")

    def __init__(
        self,
        app: Any,
        *,
        ring: int = 3,
        metric: str = "http_request",
        error_metric: str = "http_request_error",
    ) -> None:
        self.app = app
        self.ring = int(ring)
        self.metric = str(metric)
        self.error_metric = str(error_metric)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        state = scope.get("state")
        if state is None:
            state = {}
            scope["state"] = state
        started_ns = time.perf_counter_ns()
        state[_STATE_STARTED] = started_ns

        recorded = False

        async def send_with_latency(message: Any) -> None:
            nonlocal recorded
            if not recorded and message.get("type") == "http.response.start":
                recorded = True
                if not state.get(_STATE_RECORDED, False):
                    state[_STATE_RECORDED] = True
                    gateway_telemetry().record(
                        self.ring,
                        time.perf_counter_ns() - started_ns,
                        metric=self.metric,
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_with_latency)
        except Exception:
            if not recorded:
                gateway_telemetry().record(
                    self.ring,
                    time.perf_counter_ns() - started_ns,
                    metric=self.error_metric,
                )
            raise
