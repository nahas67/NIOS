"""Deterministic IV Rank in integer basis points (``sec_013``).

IV Rank answers one question: *where does today's implied volatility sit inside
its own 52-week range?*  It is a **min-max rank**, not a percentile and not a
volatility level:

.. code-block:: text

    rank_bps = (iv_now - iv_low) * 10_000 // (iv_high - iv_low)

so ``0`` means "the calmest IV in the window" and ``10_000`` means "the most
expensive IV in the window".  Everything is integer arithmetic -- the rank is a
ratio expressed in bps, and a float ratio that lands at ``0.9999999`` must not
serialise as ``9999`` on one machine and ``10000`` on another.

The helper is pure standard library: it is imported from the Ring 3 gateway and
from Ring 2 analytics, neither of which should have to pull SciPy in to divide
two numbers.

**Insufficient history is reported, never invented.**  A brand-new process has
one IV observation, and a one-sample min-max range is degenerate (the low equals
the high).  ``iv_rank_payload`` then returns ``iv_rank_bps = None`` with a named
``status`` instead of a plausible-looking ``5000``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

__all__ = [
    "IV_RANK_MAX_BPS",
    "IV_RANK_MIN_SAMPLES",
    "IV_RANK_WINDOW_SECONDS",
    "IV_RANK_INSUFFICIENT",
    "IV_RANK_NO_CURRENT_IV",
    "IV_RANK_OK",
    "iv_rank_payload",
    "minmax_rank_bps",
]

#: The rank scale: bps of the full min-max range.
IV_RANK_MAX_BPS: int = 10_000

#: A min-max rank needs at least a low, a high and the current point to mean
#: anything.  Two samples already collapse to 0/5000/10000, which would report
#: "cheap" or "expensive" from a single comparison.
IV_RANK_MIN_SAMPLES: int = 3

#: 52 weeks, in seconds.  The trailing window the spec asks for.
IV_RANK_WINDOW_SECONDS: int = 52 * 7 * 86_400

IV_RANK_OK: str = "OK"
IV_RANK_INSUFFICIENT: str = "INSUFFICIENT_HISTORY"
IV_RANK_NO_CURRENT_IV: str = "NO_CURRENT_IV"

_IV_MICROS = 1_000_000


def _iso_utc(stamp_ns: int | None) -> str | None:
    if not stamp_ns:
        return None
    return (
        datetime.fromtimestamp(int(stamp_ns) / 1_000_000_000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def minmax_rank_bps(value: float, low: float, high: float) -> int:
    """``(value - low) / (high - low)`` in bps, clamped to ``0..10000``.

    Integer math on micro-vol units: ``1.0`` IV step == ``1_000_000`` units, so
    a rank is exact to ``1e-6`` of the window width and identical on every run.
    """
    denominator = int(round((float(high) - float(low)) * _IV_MICROS))
    if denominator <= 0:
        # A degenerate window has no rank to report; mid-range is the only
        # statement that is true of both ends.
        return IV_RANK_MAX_BPS // 2
    numerator = int(round((float(value) - float(low)) * _IV_MICROS))
    rank = (numerator * IV_RANK_MAX_BPS) // denominator
    return max(0, min(IV_RANK_MAX_BPS, int(rank)))


def iv_rank_payload(
    current_iv: Any,
    history: Iterable[Sequence[Any]] = (),
    *,
    now_ns: int | None = None,
    window_seconds: int = IV_RANK_WINDOW_SECONDS,
    min_samples: int = IV_RANK_MIN_SAMPLES,
) -> dict[str, Any]:
    """IV Rank in bps for ``current_iv`` against a timestamped IV window.

    ``history`` is an iterable of ``(ts_utc_ns, iv)`` pairs.  Samples older than
    ``window_seconds`` are excluded, so the rank is always a *trailing 52-week*
    statement.  ``iv_rank_bps`` is ``None`` -- never a placeholder -- when the
    window holds fewer than ``min_samples`` observations or when the caller has
    no current IV to rank.
    """
    stamp = int(now_ns if now_ns is not None else time.time_ns())
    horizon_ns = stamp - int(window_seconds) * 1_000_000_000

    retained: list[tuple[int, float]] = []
    for row in history or ():
        try:
            ts_ns = int(row[0])
            iv = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if ts_ns < horizon_ns or iv <= 0.0:
            continue
        retained.append((ts_ns, iv))
    retained.sort(key=lambda item: item[0])

    payload: dict[str, Any] = {
        "iv_rank_bps": None,
        "iv_rank_status": IV_RANK_INSUFFICIENT,
        "iv_window_samples": len(retained),
        "iv_window_seconds": int(window_seconds),
        "iv_window_low": None,
        "iv_window_high": None,
        "iv_window_start_utc": _iso_utc(retained[0][0]) if retained else None,
        "iv_window_end_utc": _iso_utc(retained[-1][0]) if retained else None,
        "iv_current": None,
        "iv_rank_basis": "MIN_MAX_52W",
    }

    try:
        now_iv = float(current_iv) if current_iv is not None else 0.0
    except (TypeError, ValueError):
        now_iv = 0.0
    if now_iv <= 0.0:
        payload["iv_rank_status"] = IV_RANK_NO_CURRENT_IV
        return payload

    payload["iv_current"] = round(now_iv, 6)
    if len(retained) < max(1, int(min_samples)):
        return payload

    low = min(iv for _, iv in retained)
    high = max(iv for _, iv in retained)
    payload["iv_window_low"] = round(low, 6)
    payload["iv_window_high"] = round(high, 6)
    payload["iv_rank_bps"] = minmax_rank_bps(now_iv, low, high)
    payload["iv_rank_status"] = IV_RANK_OK
    return payload
