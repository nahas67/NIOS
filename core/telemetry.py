"""Shared latency telemetry substrate (``sec_004`` / ``sec_016``).

Ring 1 owns the hot path but must not import anything from Ring 3.  This
module lives in ``core/`` -- the shared substrate -- so the deterministic
spinal loop can publish per-tick latency without ever touching the cockpit
package, and the Ring 3 metrics exporter can read the same numbers.

The recorder is a fixed-bucket histogram plus a bounded reservoir used only
for quantile estimation.  ``observe`` is O(1), allocates nothing on the hot
path, and never raises.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_TICK_BUCKETS",
    "QUANTILE_KEYS",
    "TickLatencyRecorder",
    "TICK_LATENCY",
    "observe_tick_latency_ns",
    "observe_tick_latency_s",
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
