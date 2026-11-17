"""Binance venue micro-structure guards for the crypto silo.

Governing requirements
----------------------
* **C5** -- REST weight budget.  Binance meters every request against an
  ``X-MBX-USED-WEIGHT-1M`` budget (``6000``/min for the spot/IP bucket).  The
  budget is *server-reported*, so the local tracker mirrors what the server
  said and refuses a request that would cross the ceiling before it is sent.
* **C8** -- error taxonomy.  A ``418`` is an IP ban, not a retryable failure: the
  silo halts, goes reduce-only and retries **zero** times.  A ``5xx``, a timeout
  or an unknown outcome is ambiguous -- the order may have reached the matching
  engine -- so the only safe action is *query the order by its deterministic
  ``clientOrderId`` first, then retry at most once*.
* **C4** -- maintenance-margin brackets.  Leverage is only legal inside the
  bracket the notional falls into; a leverage above the bracket maximum is
  rejected pre-trade instead of being liquidated later.
* **C6** -- ``listenKey`` keepalive.  A user-data stream key dies after 60
  minutes of silence; the supervisor renews on a 30-minute cadence so the
  position/order stream never silently stops.
* **C7** -- ``recvWindow`` / clock skew.  Binance rejects a signed request whose
  timestamp is outside ``recvWindow``; the tracker measures the offset against
  ``/api/v3/time`` and widens ``recvWindow`` by the absolute offset (capped at
  ``60000`` ms).

Everything here is pure, synchronous and allocation-light: no network call, no
thread, no sleep, no clock read unless ``now_ns`` is omitted.  The module never
places or cancels an order (``sec_004`` air-gap contract) -- it decides whether
Ring 0 may.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Sequence

__all__ = [
    "BinanceErrorClass",
    "WeightBucketState",
    "BinanceWeightBucket",
    "BinanceResponseGuard",
    "MaintenanceMarginBracket",
    "DEFAULT_BINANCE_BRACKETS",
    "validate_maintenance_bracket",
    "ListenKeySupervisor",
    "RecvWindowTracker",
]

_NS_PER_SECOND = 1_000_000_000
_MS_PER_SECOND = 1000

#: Hard ceiling on ``recvWindow`` accepted by Binance.
MAX_RECV_WINDOW_MS = 60_000

#: Ambiguous outcomes: the order may or may not have reached the venue.
_AMBIGUOUS: frozenset[str] = frozenset({"SERVER_ERROR", "TIMEOUT", "UNKNOWN"})


def _resolve(now_ns: int | None) -> int:
    """Return an explicit ``now_ns`` or the current clock reading."""

    return int(time.time_ns()) if now_ns is None else int(now_ns)


class BinanceErrorClass(StrEnum):
    """Normalised Binance REST outcome (C8)."""

    OK = "OK"
    RATE_LIMITED_418 = "RATE_LIMITED_418"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    CLIENT_ERROR = "CLIENT_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class WeightBucketState:
    """Server-reported weight budget for one silo (C5)."""

    used_weight: int
    limit: int
    window_started_ns: int
    exhausted: bool

    def remaining(self) -> int:
        """Weight left in the current window (clamped at ``0``)."""

        return max(0, int(self.limit) - int(self.used_weight))

    def to_dict(self) -> dict[str, Any]:
        """Return the state as a JSON-safe mapping."""

        return {
            "used_weight": int(self.used_weight),
            "limit": int(self.limit),
            "remaining": self.remaining(),
            "window_started_ns": int(self.window_started_ns),
            "exhausted": bool(self.exhausted),
        }

    def __getitem__(self, key: str) -> Any:
        """Read a field by name, so the state doubles as its own dict view.

        ``observe`` returns this object and callers legitimately read it as a
        mapping (``observe(...)["exhausted"]``); supporting both keeps the typed
        return value and the dict-style read identical.
        """

        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        """``dict.get`` equivalent over :meth:`to_dict`."""

        return self.to_dict().get(key, default)


class BinanceWeightBucket:
    """Per-silo mirror of Binance's minute weight budget (C5).

    The server is authoritative: :meth:`observe` copies the reported
    ``used_weight`` (never decreases inside a window) and :meth:`allow` refuses a
    request that would exceed ``limit`` *before* it is sent, so the silo cannot
    walk itself into a ``418`` ban by optimistic accounting.
    """

    def __init__(self, *, limit: int = 6000, window_seconds: int = 60) -> None:
        if int(limit) < 1:
            raise ValueError("limit must be >= 1")
        if int(window_seconds) < 1:
            raise ValueError("window_seconds must be >= 1")
        self._limit = int(limit)
        self._window_seconds = int(window_seconds)
        self._window_ns = self._window_seconds * _NS_PER_SECOND
        self._used = 0
        self._window_started_ns = -1
        self._exhausted = False

    # -- window arithmetic ------------------------------------------------
    def _roll(self, now_ns: int, *, fresh_used: int | None = None) -> bool:
        """Start a new window when the current one has elapsed.

        Returns ``True`` when a new window was opened.  ``-1`` is the
        "no window observed yet" sentinel so ``now_ns == 0`` stays a legal
        timestamp (a test or replay clock may legitimately start at zero).
        """

        if self._window_started_ns < 0 or now_ns - self._window_started_ns >= self._window_ns:
            self._window_started_ns = now_ns
            self._used = 0 if fresh_used is None else max(0, int(fresh_used))
            self._exhausted = self._used >= self._limit
            return True
        return False

    # -- observations -----------------------------------------------------
    def observe(self, used_weight: int, *, now_ns: int | None = None) -> WeightBucketState:
        """Record the server-reported ``used_weight`` and return the new state."""

        used = int(used_weight)
        if used < 0:
            raise ValueError("used_weight must be >= 0")
        now = _resolve(now_ns)
        rolled = self._roll(now, fresh_used=used)
        if rolled:
            self._used = used
        else:
            # Inside a window the server counter only grows; keep the maximum so
            # a stale header from a parallel request cannot under-count.
            self._used = max(self._used, used)
        self._exhausted = self._used >= self._limit
        return self.state()

    def state(self) -> WeightBucketState:
        """Return the last observed state (no clock read)."""

        return WeightBucketState(
            used_weight=self._used,
            limit=self._limit,
            window_started_ns=self._window_started_ns,
            exhausted=self._exhausted,
        )

    # -- admission --------------------------------------------------------
    def allow(self, cost: int, *, now_ns: int | None = None) -> bool:
        """Reserve ``cost`` weight, or refuse when it would cross the ceiling."""

        weight = int(cost)
        if weight < 0:
            raise ValueError("cost must be >= 0")
        now = _resolve(now_ns)
        self._roll(now)
        if weight == 0:
            return True
        if self._used + weight > self._limit:
            self._exhausted = True
            return False
        self._used += weight
        self._exhausted = self._used >= self._limit
        return True

    def remaining(self, *, now_ns: int | None = None) -> int:
        """Weight left in the current window (read-only)."""

        now = _resolve(now_ns)
        if (
            self._window_started_ns < 0
            or now - self._window_started_ns >= self._window_ns
        ):
            return self._limit
        return max(0, self._limit - self._used)

    def snapshot(self, *, now_ns: int | None = None) -> dict[str, Any]:
        """Return a JSON-safe telemetry mapping."""

        now = _resolve(now_ns)
        elapsed = (
            0
            if self._window_started_ns < 0
            else max(0, now - self._window_started_ns)
        )
        return {
            "used_weight": self._used,
            "limit": self._limit,
            "remaining": self.remaining(now_ns=now),
            "window_seconds": self._window_seconds,
            "window_started_ns": self._window_started_ns,
            "window_elapsed_ns": elapsed,
            "exhausted": self._exhausted,
        }


class BinanceResponseGuard:
    """Classify Binance responses and decide halt / query-then-retry (C8).

    Halt state is per silo and sticky: once a ``418`` is seen the silo is
    reduce-only until an operator clears it (``sec_018`` Law 4 -- the default
    answer is "do not send").  An ambiguous outcome never authorises a blind
    retry: the plan is always ``QUERY_THEN_RETRY`` against the deterministic
    ``clientOrderId``, which is what makes at-most-once execution provable.
    """

    def __init__(self, *, weight_limit: int = 6000) -> None:
        self._weight_limit = int(weight_limit)
        self._buckets: dict[str, BinanceWeightBucket] = {}
        self._halted: dict[str, bool] = {}
        self._counts: dict[str, int] = {}

    # -- classification ---------------------------------------------------
    @staticmethod
    def classify(status_code: int | None, *, timed_out: bool = False) -> BinanceErrorClass:
        """Map an HTTP status (and timeout flag) onto :class:`BinanceErrorClass`.

        Order matters: ``418`` is a ``4xx`` but is checked first because it is a
        ban, and a ``5xx`` outranks a timeout flag because the server did answer.
        """

        if status_code is not None and int(status_code) == 418:
            return BinanceErrorClass.RATE_LIMITED_418
        if status_code is not None and 500 <= int(status_code) <= 599:
            return BinanceErrorClass.SERVER_ERROR
        if timed_out:
            return BinanceErrorClass.TIMEOUT
        if status_code is None:
            return BinanceErrorClass.UNKNOWN
        code = int(status_code)
        if 200 <= code <= 299:
            return BinanceErrorClass.OK
        if 400 <= code <= 499:
            return BinanceErrorClass.CLIENT_ERROR
        return BinanceErrorClass.UNKNOWN

    # -- state ------------------------------------------------------------
    def bucket(self, silo: str) -> BinanceWeightBucket:
        """Return (creating on first use) the weight bucket for ``silo``."""

        bucket = self._buckets.get(silo)
        if bucket is None:
            bucket = BinanceWeightBucket(limit=self._weight_limit)
            self._buckets[silo] = bucket
        return bucket

    def halted(self, silo: str) -> bool:
        """``True`` when ``silo`` is halted by a ``418`` (reduce-only)."""

        return bool(self._halted.get(silo, False))

    def clear_halt(self, silo: str) -> bool:
        """Operator-only reset of the ``418`` halt; returns the previous state."""

        previous = self.halted(silo)
        self._halted[silo] = False
        return previous

    def _count(self, error_class: BinanceErrorClass) -> None:
        key = str(error_class)
        self._counts[key] = self._counts.get(key, 0) + 1

    # -- response handling ------------------------------------------------
    def on_response(
        self,
        silo: str,
        status_code: int | None,
        *,
        now_ns: int | None = None,
        timed_out: bool = False,
        used_weight: int | None = None,
    ) -> dict[str, Any]:
        """Record one REST outcome for ``silo`` and return the routing decision.

        A ``418`` halts the silo (reduce-only, zero retries, no query).  An
        ambiguous outcome (``5xx``/timeout/unknown) is retryable **only** after
        the order is queried by ``clientOrderId``; :meth:`retry_plan` carries
        that id, so this dict intentionally omits it.
        """

        error_class = self.classify(status_code, timed_out=timed_out)
        self._count(error_class)
        if used_weight is not None:
            self.bucket(silo).observe(used_weight, now_ns=now_ns)

        if error_class is BinanceErrorClass.RATE_LIMITED_418:
            self._halted[silo] = True
            return {
                "error_class": str(error_class),
                "silo": silo,
                "halted": True,
                "retry_allowed": False,
                "reduce_only": True,
                "retries": 0,
                "query_before_retry": False,
            }

        halted = self.halted(silo)
        if str(error_class) in _AMBIGUOUS:
            return {
                "error_class": str(error_class),
                "silo": silo,
                "halted": halted,
                "retry_allowed": True,
                "reduce_only": halted,
                "retries": 1,
                "query_before_retry": True,
            }

        # OK and CLIENT_ERROR are both deterministic: the venue answered and a
        # replay would not change the answer.
        return {
            "error_class": str(error_class),
            "silo": silo,
            "halted": halted,
            "retry_allowed": False,
            "reduce_only": halted,
            "retries": 0,
            "query_before_retry": False,
        }

    def retry_plan(
        self, client_order_id: str, error_class: BinanceErrorClass
    ) -> dict[str, Any]:
        """Return the only legal retry for ``client_order_id`` under ``error_class``.

        ``deterministic_client_order_id`` is always ``True``: the id is what makes
        a query-then-retry at-most-once, so a caller must never mint a new one.
        """

        cls = BinanceErrorClass(error_class)
        if cls is BinanceErrorClass.RATE_LIMITED_418:
            action, max_retries = "HALT", 0
        elif str(cls) in _AMBIGUOUS:
            action, max_retries = "QUERY_THEN_RETRY", 1
        else:
            action, max_retries = "NONE", 0
        return {
            "client_order_id": str(client_order_id),
            "error_class": str(cls),
            "action": action,
            "max_retries": max_retries,
            "deterministic_client_order_id": True,
        }

    # -- telemetry --------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe guard report for Ring 3 cards (``sec_016``)."""

        return {
            "weight_limit": self._weight_limit,
            "halted_silos": sorted(silo for silo, flag in self._halted.items() if flag),
            "halted": {silo: bool(flag) for silo, flag in self._halted.items()},
            "counts": dict(self._counts),
            "buckets": {silo: bucket.snapshot() for silo, bucket in self._buckets.items()},
        }


@dataclass(slots=True, frozen=True)
class MaintenanceMarginBracket:
    """One Binance leverage bracket (C4).

    ``notional_floor_micros`` is the bracket's lower bound in integer micro-USDT;
    ``max_leverage`` and ``maintenance_margin_pct`` are the venue's published
    values for that notional band.
    """

    notional_floor_micros: int
    max_leverage: float
    maintenance_margin_pct: float


#: Binance USDT-M published brackets (floor, max leverage, maintenance margin).
#: Floors are micro-USDT, so ``50_000`` USDT is ``50_000_000_000`` micros.
DEFAULT_BINANCE_BRACKETS: tuple[MaintenanceMarginBracket, ...] = (
    MaintenanceMarginBracket(0, 125.0, 0.40),
    MaintenanceMarginBracket(50_000_000_000, 100.0, 0.50),
    MaintenanceMarginBracket(600_000_000_000, 75.0, 0.65),
    MaintenanceMarginBracket(3_000_000_000_000, 50.0, 1.00),
    MaintenanceMarginBracket(12_000_000_000_000, 25.0, 2.00),
    MaintenanceMarginBracket(25_000_000_000_000, 20.0, 2.50),
    MaintenanceMarginBracket(60_000_000_000_000, 10.0, 5.00),
    MaintenanceMarginBracket(150_000_000_000_000, 5.0, 10.00),
    MaintenanceMarginBracket(300_000_000_000_000, 3.0, 12.50),
    MaintenanceMarginBracket(600_000_000_000_000, 2.0, 15.00),
    MaintenanceMarginBracket(1_200_000_000_000_000, 1.0, 25.00),
)


def validate_maintenance_bracket(
    notional_micros: int,
    leverage: float,
    brackets: Sequence[MaintenanceMarginBracket] = DEFAULT_BINANCE_BRACKETS,
) -> dict[str, Any]:
    """Check ``leverage`` against the bracket that ``notional_micros`` falls into.

    The bracket chosen is the one with the highest ``notional_floor_micros`` that
    is still ``<= notional_micros``; a notional below the lowest floor uses the
    lowest bracket.  ``valid`` is ``False`` when the requested leverage exceeds
    the bracket maximum, so Ring 0 can reject the order pre-trade (C4).
    """

    notional = int(notional_micros)
    lev = float(leverage)
    if notional < 0:
        raise ValueError("notional_micros must be >= 0")
    if lev <= 0.0:
        raise ValueError("leverage must be > 0")
    if not brackets:
        raise ValueError("brackets must not be empty")

    ordered = sorted(brackets, key=lambda item: int(item.notional_floor_micros))
    chosen = ordered[0]
    for bracket in ordered:
        if int(bracket.notional_floor_micros) <= notional:
            chosen = bracket
        else:
            break

    max_leverage = float(chosen.max_leverage)
    valid = lev <= max_leverage
    return {
        "notional_micros": notional,
        "bracket": {
            "notional_floor_micros": int(chosen.notional_floor_micros),
            "max_leverage": max_leverage,
            "maintenance_margin_pct": float(chosen.maintenance_margin_pct),
        },
        "leverage": lev,
        "max_leverage": max_leverage,
        "maintenance_margin_pct": float(chosen.maintenance_margin_pct),
        "required_margin_micros": int(notional / lev),
        "valid": valid,
    }


class ListenKeySupervisor:
    """Track ``listenKey`` freshness for user-data streams (C6).

    Binance expires a user-data stream 60 minutes after the last keepalive, so
    the supervisor treats a key older than ``keepalive_seconds`` (default 30
    minutes) as due and never waits for the stream to fail first.
    """

    def __init__(self, *, keepalive_seconds: int = 1800, now_ns: int | None = None) -> None:
        if int(keepalive_seconds) < 1:
            raise ValueError("keepalive_seconds must be >= 1")
        self._keepalive_seconds = int(keepalive_seconds)
        self._keepalive_ns = self._keepalive_seconds * _NS_PER_SECOND
        self._now_ns = _resolve(now_ns)
        self._keys: dict[str, int] = {}

    @property
    def keepalive_seconds(self) -> int:
        """Renewal cadence in seconds (default 1800)."""

        return self._keepalive_seconds

    def register(self, listen_key: str, *, now_ns: int | None = None) -> None:
        """Register (or re-stamp) ``listen_key`` at ``now_ns``."""

        key = str(listen_key)
        if not key:
            raise ValueError("listen_key must be non-empty")
        self._keys[key] = _resolve(now_ns) if now_ns is not None else self._now_ns

    def due(self, *, now_ns: int | None = None) -> list[str]:
        """Keys whose last renewal is at or past ``keepalive_seconds``."""

        now = _resolve(now_ns) if now_ns is not None else self._now_ns
        return [
            key
            for key, stamp in self._keys.items()
            if now - int(stamp) >= self._keepalive_ns
        ]

    def renew(
        self,
        listen_key: str,
        *,
        now_ns: int | None = None,
        renewed: bool = True,
    ) -> bool:
        """Refresh ``listen_key`` on success, or drop it on failure.

        Returns ``True`` when the key is still tracked after the call.
        """

        key = str(listen_key)
        if key not in self._keys:
            return False
        if not renewed:
            del self._keys[key]
            return False
        self._keys[key] = _resolve(now_ns) if now_ns is not None else self._now_ns
        return True

    def drop(self, listen_key: str) -> bool:
        """Forget ``listen_key``; returns ``True`` when it existed."""

        return self._keys.pop(str(listen_key), None) is not None

    def snapshot(self, *, now_ns: int | None = None) -> dict[str, Any]:
        """Return a JSON-safe telemetry mapping."""

        now = _resolve(now_ns) if now_ns is not None else self._now_ns
        ages = {
            key: max(0, now - int(stamp)) // _NS_PER_SECOND
            for key, stamp in self._keys.items()
        }
        return {
            "keepalive_seconds": self._keepalive_seconds,
            "keys": list(self._keys),
            "count": len(self._keys),
            "due": self.due(now_ns=now),
            "age_seconds": ages,
        }


class RecvWindowTracker:
    """Measure clock skew and size ``recvWindow`` accordingly (C7).

    Binance rejects a signed request whose ``timestamp`` is outside
    ``recvWindow``.  The offset is clamped to ``+/- max_offset_ms`` so a bogus
    ``/api/v3/time`` answer cannot widen the window without bound, and the
    resulting ``recvWindow`` is capped at ``MAX_RECV_WINDOW_MS``.
    """

    def __init__(
        self, *, base_recv_window_ms: int = 5000, max_offset_ms: int = 1000
    ) -> None:
        if int(base_recv_window_ms) < 1:
            raise ValueError("base_recv_window_ms must be >= 1")
        if int(max_offset_ms) < 0:
            raise ValueError("max_offset_ms must be >= 0")
        self._base_recv_window_ms = int(base_recv_window_ms)
        self._max_offset_ms = int(max_offset_ms)
        self._offset_ms = 0
        self._samples = 0

    def observe_server_time(self, server_time_ms: int, local_time_ms: int) -> int:
        """Record one server/local clock pair and return the clamped offset."""

        raw = int(server_time_ms) - int(local_time_ms)
        self._offset_ms = max(-self._max_offset_ms, min(self._max_offset_ms, raw))
        self._samples += 1
        return self._offset_ms

    @property
    def offset_ms(self) -> int:
        """Clamped ``server - local`` clock offset in milliseconds."""

        return self._offset_ms

    @property
    def recv_window_ms(self) -> int:
        """``base + abs(offset)``, capped at :data:`MAX_RECV_WINDOW_MS`."""

        widened = self._base_recv_window_ms + abs(self._offset_ms)
        return min(MAX_RECV_WINDOW_MS, widened)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe telemetry mapping."""

        return {
            "offset_ms": self._offset_ms,
            "recv_window_ms": self.recv_window_ms,
            "base_recv_window_ms": self._base_recv_window_ms,
            "max_offset_ms": self._max_offset_ms,
            "samples": self._samples,
        }
