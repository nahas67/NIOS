"""NIOS V2.1 -- in-process background lifecycle worker (``G4``).

Five clocks run quietly behind the cockpit, and every one of them is a way to lose
money if it stops:

===========================  ==================  ===============================
cadence                      job                 failure it prevents
===========================  ==================  ===============================
``03:00 UTC`` daily          TOTP renewal check  an India session that cannot log in
``30 min``                   listenKey keepalive a silent Binance stream death
tiered ``1s..1m``            UNKNOWN order retry a double-filled 5xx order
``5 min``                    quota TTL sweep     phantom margin headroom
``15 min``                   concept drift check a strategy that has stopped working
===========================  ==================  ===============================

Two design rules make this testable and safe:

1. ``run_maintenance_tick(now_utc=...)`` is **synchronous** and clock-injected, so a
   test advances four hours without sleeping and gets byte-identical results.
2. Every job is an independently recorded outcome.  A job that raises is caught,
   converted into an alert, and the remaining jobs still run -- one dead clock must
   never stop the other four.

Harvest note (Rule 1): the retry ladder is translated from ``hummingbot``'s
``listenKey`` keepalive and ``ccxt`` ``X-MBX-USED-WEIGHT`` discipline; the
jittered read-only retry shape from ``jd/tenacity``; the interval bookkeeping from
``agronholm/apscheduler``.  No donor package is imported or vendored, and
``apscheduler`` is optional: when it is absent the worker keeps its own
``asyncio`` loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import threading
import time
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "DEFAULT_JOBS",
    "RETRY_DELAYS_S",
    "NiosLifecycleWorker",
    "UnknownOrderQueue",
]

#: ``G4``: the tiered delay ladder for a ``5xx UNKNOWN`` order reconciliation.
RETRY_DELAYS_S: tuple[int, ...] = (1, 5, 10, 30, 60)

_NS_PER_SECOND = 1_000_000_000
_NS_PER_MINUTE = 60 * _NS_PER_SECOND
_NS_PER_DAY = 86_400 * _NS_PER_SECOND

#: ``G4``: the job catalogue and its cadence in seconds.
DEFAULT_JOBS: Mapping[str, int] = {
    "totp_renewal": 0,  # daily at 03:00 UTC; scheduled by wall clock, not interval
    "listen_key_keepalive": 30 * 60,
    "quota_ttl_sweep": 5 * 60,
    "concept_drift_check": 15 * 60,
    "reconciliation": 60,
    "unknown_order_retry": 1,
}


def _seal(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class UnknownOrderQueue:
    """Tiered, read-only ``query-before-retry`` queue for ``5xx UNKNOWN`` orders.

    The queue never re-submits an order.  It asks the venue *what happened*, and
    only a definitive answer resolves the row.  An unanswerable order escalates
    instead of being retried blind -- a blind retry is how one fill becomes two.
    """

    def __init__(
        self,
        *,
        connector: Any = None,
        delays_s: Sequence[int] = RETRY_DELAYS_S,
        jitter_s: float = 0.0,
        max_attempts: int | None = None,
    ) -> None:
        self.connector = connector
        self.delays_s = tuple(int(max(1, int(value))) for value in delays_s) or RETRY_DELAYS_S
        self.jitter_s = max(0.0, float(jitter_s))
        self.max_attempts = int(max_attempts if max_attempts is not None else len(self.delays_s))
        self._rows: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._attempts = 0
        self._resolved = 0
        self._escalated = 0

    # -- enqueue ----------------------------------------------------------
    def enqueue(
        self,
        *,
        order_id: str,
        silo: str,
        symbol: str = "",
        reason: str = "5XX_UNKNOWN",
        now_utc: int | None = None,
    ) -> dict[str, Any]:
        stamp = int(now_utc if now_utc is not None else time.time_ns())
        key = str(order_id)
        with self._lock:
            existing = self._rows.get(key)
            if existing is not None:
                return dict(existing)
            delay = self.delays_s[0]
            row = {
                "order_id": key,
                "silo": str(silo).upper(),
                "symbol": str(symbol).upper(),
                "reason": str(reason),
                "state": "PENDING",
                "attempts": 0,
                "next_attempt_at_utc_ns": stamp + delay * _NS_PER_SECOND,
                "enqueued_at_utc_ns": stamp,
                "resolved_at_utc_ns": None,
                "resolution": "",
                "history": [],
            }
            self._rows[key] = row
            self._order.append(key)
            return dict(row)

    # -- ticking ----------------------------------------------------------
    def due(self, *, now_utc: int) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = [
                dict(row)
                for row in self._rows.values()
                if row["state"] == "PENDING" and int(row["next_attempt_at_utc_ns"]) <= int(now_utc)
            ]
        return tuple(sorted(rows, key=lambda row: (row["next_attempt_at_utc_ns"], row["order_id"])))

    def tick(self, *, now_utc: int) -> list[dict[str, Any]]:
        """Attempt every due row once through the read-only reconciler."""
        outcomes: list[dict[str, Any]] = []
        for row in self.due(now_utc=now_utc):
            outcomes.append(self._attempt(row, now_utc=now_utc))
        return outcomes

    def _attempt(self, row: Mapping[str, Any], *, now_utc: int) -> dict[str, Any]:
        key = str(row["order_id"])
        with self._lock:
            live = self._rows.get(key)
            if live is None or live["state"] != "PENDING":
                return {"order_id": key, "state": "IGNORED"}
            self._attempts += 1
            live["attempts"] = int(live["attempts"]) + 1
            attempt = int(live["attempts"])
            resolution = self._query(live)
            live["history"].append(
                {
                    "attempt": attempt,
                    "at_utc_ns": int(now_utc),
                    "resolution": str(resolution.get("state", "UNKNOWN")),
                }
            )
            # Only a definitive venue answer is terminal.  "No open order with
            # this id" is *not* proof of no fill -- it may have filled and closed
            # -- so it stays on the ladder instead of resolving blind.
            if resolution.get("state") in ("FILLED", "CANCELLED", "REJECTED"):
                live["state"] = "RESOLVED"
                live["resolution"] = str(resolution.get("state"))
                live["resolved_at_utc_ns"] = int(now_utc)
                self._resolved += 1
                return dict(live)
            if attempt >= self.max_attempts:
                live["state"] = "ESCALATED"
                live["resolution"] = "AWAITING_OPERATOR"
                live["resolved_at_utc_ns"] = int(now_utc)
                self._escalated += 1
                return dict(live)
            delay = self.delays_s[min(attempt, len(self.delays_s) - 1)]
            jitter = 0.0
            if self.jitter_s > 0.0:
                jitter = random.uniform(0.0, self.jitter_s)  # noqa: S311 - scheduling jitter
            live["next_attempt_at_utc_ns"] = int(
                now_utc + (delay + jitter) * _NS_PER_SECOND
            )
            return dict(live)

    def _query(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the venue, read-only, what happened to ``order_id``."""
        connector = self.connector
        if connector is None:
            # Without a connector there is no evidence, and no evidence must never
            # be read as "it did not fill".
            return {"state": "NO_CONNECTOR"}
        try:
            result = connector.fetch_open_orders(silo=str(row["silo"]), now_ns=int(row["enqueued_at_utc_ns"]))
        except TypeError:  # pragma: no cover - transport without now_ns
            result = connector.fetch_open_orders(silo=str(row["silo"]))
        except Exception as exc:  # noqa: BLE001 - a failed probe is inconclusive
            return {"state": "PROBE_FAILED", "detail": f"{type(exc).__name__}: {exc}"}
        orders = result.get("orders") if isinstance(result, Mapping) else None
        for order in orders or ():
            if not isinstance(order, Mapping):
                continue
            if str(order.get("client_order_id")) == str(row["order_id"]):
                status = str(order.get("status", "")).upper()
                if status in ("FILLED", "CLOSED"):
                    return {"state": "FILLED", "order": dict(order)}
                if status in ("CANCELED", "CANCELLED"):
                    return {"state": "CANCELLED", "order": dict(order)}
                if status in ("REJECTED", "EXPIRED"):
                    return {"state": "REJECTED", "order": dict(order)}
        return {"state": "UNKNOWN_NO_FILL"}

    # -- reporting --------------------------------------------------------
    def rows(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(self._rows[key]) for key in self._order)

    def status(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            for row in self._rows.values():
                counts[str(row["state"])] = counts.get(str(row["state"]), 0) + 1
            return {
                "count": len(self._rows),
                "state_counts": counts,
                "attempts": int(self._attempts),
                "resolved": int(self._resolved),
                "escalated": int(self._escalated),
                "delays_s": list(self.delays_s),
                "max_attempts": int(self.max_attempts),
                "query_before_retry": True,
                "never_resubmits": True,
            }


class NiosLifecycleWorker:
    """The in-process scheduler the FastAPI lifespan owns (``G4``).

    It is deliberately inert in tests: ``run_maintenance_tick`` does the work, and
    ``start``/``stop`` only manage a sleeping ``asyncio`` task in production.
    """

    def __init__(
        self,
        *,
        alert_center: Any = None,
        quota_engine: Any = None,
        drift_monitor: Any = None,
        totp_renewal: Any = None,
        connector: Any = None,
        reconciliation: Any = None,
        autopilot: Any = None,
        settings_store: Any = None,
        unknown_orders: UnknownOrderQueue | None = None,
        enabled: bool = True,
        totp_warn_hours: int = 6,
    ) -> None:
        self.alert_center = alert_center
        self.quota_engine = quota_engine
        self.drift_monitor = drift_monitor
        self.totp_renewal = totp_renewal
        self.connector = connector
        self.reconciliation = reconciliation
        self.autopilot = autopilot
        self.settings_store = settings_store
        self.enabled = bool(enabled)
        self.totp_warn_hours = int(totp_warn_hours)
        self.unknown_orders = (
            unknown_orders if unknown_orders is not None else UnknownOrderQueue(connector=connector)
        )
        self._lock = threading.RLock()
        self._last_run: dict[str, int] = {}
        self._runs: dict[str, int] = {job: 0 for job in DEFAULT_JOBS}
        self._failures: dict[str, str] = {}
        self._last_tick_utc_ns: int | None = None
        self._listen_key: dict[str, Any] = {
            "last_ping_utc_ns": None,
            "status": "IDLE",
            "pings": 0,
            "expires_at_utc_ns": None,
        }
        self._task: asyncio.Task[Any] | None = None
        self._scheduler: Any = None
        self._stopping = False

    # ------------------------------------------------------------------
    # The deterministic tick
    # ------------------------------------------------------------------
    def run_maintenance_tick(self, *, now_utc: int | None = None) -> dict[str, Any]:
        """Run every job whose cadence is due at ``now_utc`` (seconds-resolution safe)."""
        stamp = int(now_utc if now_utc is not None else time.time_ns())
        outcomes: list[dict[str, Any]] = []
        with self._lock:
            for job in (
                "totp_renewal",
                "listen_key_keepalive",
                "unknown_order_retry",
                "quota_ttl_sweep",
                "concept_drift_check",
                "reconciliation",
            ):
                if not self._is_due(job, stamp):
                    continue
                try:
                    detail = self._run_job(job, stamp)
                    status = "OK"
                except Exception as exc:  # noqa: BLE001 - one dead clock cannot stop the rest
                    detail = {"error": f"{type(exc).__name__}: {exc}"}
                    status = "FAILED"
                    self._failures[job] = str(detail["error"])
                self._runs[job] = self._runs.get(job, 0) + 1
                self._last_run[job] = stamp
                outcomes.append({"job": job, "status": status, "detail": detail})
            self._last_tick_utc_ns = stamp
        return {
            "tick_utc_ns": stamp,
            "jobs_run": len(outcomes),
            "outcomes": outcomes,
            "enabled": self.enabled,
            "unknown_orders": self.unknown_orders.status(),
        }

    def _is_due(self, job: str, stamp: int) -> bool:
        if not self.enabled:
            return False
        last = self._last_run.get(job)
        if job == "totp_renewal":
            return self._daily_due(stamp, last, hour=3)
        cadence = int(DEFAULT_JOBS.get(job, 0) or 0)
        if cadence <= 0:
            return False
        if last is None:
            return True
        return stamp - last >= cadence * _NS_PER_SECOND

    @staticmethod
    def _daily_due(stamp: int, last: int | None, *, hour: int) -> bool:
        """True once per UTC day, at or after ``hour:00``."""
        day = stamp // _NS_PER_DAY
        second_of_day = (stamp % _NS_PER_DAY) // _NS_PER_SECOND
        if second_of_day < hour * 3600:
            return False
        if last is None:
            return True
        return (last // _NS_PER_DAY) != day

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    def _run_job(self, job: str, stamp: int) -> dict[str, Any]:
        if job == "totp_renewal":
            return self._job_totp(stamp)
        if job == "listen_key_keepalive":
            return self._job_listen_key(stamp)
        if job == "unknown_order_retry":
            return {"attempts": self.unknown_orders.tick(now_utc=stamp)}
        if job == "quota_ttl_sweep":
            return self._job_quota_sweep(stamp)
        if job == "concept_drift_check":
            return self._job_drift(stamp)
        return self._job_reconciliation(stamp)

    def _job_totp(self, stamp: int) -> dict[str, Any]:
        if self.totp_renewal is None:
            return {"skipped": True, "reason": "NO_TOTP_SURFACE"}
        renewed: list[dict[str, Any]] = []
        for record in self.totp_renewal.tick(now_ns=stamp):
            payload = record.to_dict() if hasattr(record, "to_dict") else dict(record)
            renewed.append(payload)
            state = str(payload.get("state", "")).upper()
            if state in ("EXPIRED", "FAILED", "RENEWAL_FAILED"):
                self._alert(
                    "TOTP_EXPIRY_WARNING",
                    "CRITICAL",
                    source="lifecycle_worker",
                    silo=str(payload.get("silo", "")),
                    message=f"{payload.get('silo')}/{payload.get('venue')} TOTP {state}",
                    detail=payload,
                    now_ns=stamp,
                )
        return {"renewed": renewed, "count": len(renewed)}

    def _job_listen_key(self, stamp: int) -> dict[str, Any]:
        """Ping the Binance ``listenKey`` every 30 minutes."""
        keepalive = getattr(self.connector, "listen_key_keepalive", None)
        if callable(keepalive):
            try:
                result = keepalive(now_ns=stamp)
            except TypeError:  # pragma: no cover - transport without now_ns
                result = keepalive()
            payload = dict(result or {})
        else:
            payload = {"ok": True, "source": "offline_replay", "venue": "binance"}
        ok = bool(payload.get("ok", True))
        self._listen_key = {
            "last_ping_utc_ns": stamp,
            "status": "ALIVE" if ok else "DEAD",
            "pings": int(self._listen_key.get("pings", 0)) + 1,
            "expires_at_utc_ns": stamp + 60 * _NS_PER_MINUTE,
        }
        if not ok:
            self._alert(
                "LISTEN_KEY_EXPIRED",
                "CRITICAL",
                source="lifecycle_worker",
                silo="CRYPTO_BINANCE",
                message="Binance listenKey keepalive failed; user stream is dead",
                detail=payload,
                now_ns=stamp,
            )
        return dict(self._listen_key)

    def _job_quota_sweep(self, stamp: int) -> dict[str, Any]:
        if self.quota_engine is None:
            return {"skipped": True, "reason": "NO_QUOTA_ENGINE"}
        status = self.quota_engine.status(now_ns=stamp)
        return {
            "active_count": int(status.get("active_count", 0)),
            "active_micros_by_silo": dict(status.get("active_micros_by_silo", {})),
            "no_funds_moved": bool(status.get("no_funds_moved", True)),
        }

    def _job_drift(self, stamp: int) -> dict[str, Any]:
        if self.drift_monitor is None:
            return {"skipped": True, "reason": "NO_DRIFT_MONITOR"}
        multiplier = 1.0
        allocator = getattr(self.drift_monitor, "allocation_multiplier", None)
        if callable(allocator):
            multiplier = float(allocator())
        snapshot: dict[str, Any] = {}
        describe = getattr(self.drift_monitor, "snapshot", None)
        if callable(describe):
            try:
                snapshot = dict(describe())
            except Exception:  # pragma: no cover - advisory
                snapshot = {}
        if multiplier < 1.0:
            self._alert(
                "CONCEPT_DRIFT_SIZING_CUT",
                "WARNING",
                source="lifecycle_worker",
                message=f"concept drift cut tranche sizing to {multiplier:.4f}x",
                detail={"allocation_multiplier": round(multiplier, 8), "snapshot": snapshot},
                now_ns=stamp,
            )
        return {"allocation_multiplier": round(multiplier, 8), "throttled": multiplier < 1.0}

    def _job_reconciliation(self, stamp: int) -> dict[str, Any]:
        if self.reconciliation is None:
            return {"skipped": True, "reason": "NO_RECONCILER"}
        runner = getattr(self.reconciliation, "run", None)
        if not callable(runner):
            return {"skipped": True, "reason": "RECONCILER_HAS_NO_RUN"}
        try:
            report = runner(now_ns=stamp)
        except TypeError:  # pragma: no cover - reconciler without now_ns
            report = runner()
        payload = report.to_dict() if hasattr(report, "to_dict") else dict(report or {})
        if payload.get("frozen_silos") or payload.get("drift_detected"):
            self._alert(
                "COLD_START_RECON_DRIFT",
                "CRITICAL",
                source="lifecycle_worker",
                message="cold-start reconciliation detected ledger drift",
                detail=payload,
                now_ns=stamp,
            )
        return payload

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------
    def _alert(
        self,
        code: str,
        severity: str,
        *,
        source: str,
        message: str,
        silo: str = "",
        detail: Mapping[str, Any] | None = None,
        now_ns: int | None = None,
    ) -> None:
        if self.alert_center is None:
            return
        try:
            self.alert_center.raise_alert(
                code=code,
                severity=severity,
                source=source,
                silo=silo,
                message=message,
                detail=detail,
                now_ns=now_ns,
            )
        except Exception:  # pragma: no cover - an alert must never crash the worker
            pass

    def raise_unknown_order(
        self,
        *,
        order_id: str,
        silo: str,
        symbol: str = "",
        reason: str = "5XX_UNKNOWN",
        now_utc: int | None = None,
    ) -> dict[str, Any]:
        """Public seam used by Ring 0 when an order response is unreadable."""
        row = self.unknown_orders.enqueue(
            order_id=order_id, silo=silo, symbol=symbol, reason=reason, now_utc=now_utc
        )
        self._alert(
            "ORDER_STATE_UNKNOWN",
            "WARNING",
            source="lifecycle_worker",
            silo=silo,
            message=f"order {order_id} returned {reason}; queued for read-only reconciliation",
            detail=row,
            now_ns=now_utc,
        )
        return row

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, *, interval_s: float = 5.0) -> dict[str, Any]:
        """Start the background loop (idempotent, and a no-op when disabled)."""
        if not self.enabled:
            return {"started": False, "reason": "DISABLED"}
        if self._task is not None and not self._task.done():
            return {"started": False, "reason": "ALREADY_RUNNING"}
        self._stopping = False
        self._task = asyncio.create_task(self._loop(interval_s=float(interval_s)))
        return {
            "started": True,
            "backend": "asyncio",
            "interval_s": round(float(interval_s), 3),
            "jobs": list(DEFAULT_JOBS),
        }

    async def _loop(self, *, interval_s: float) -> None:
        period = max(0.05, float(interval_s))
        try:
            while not self._stopping:
                # Sleep *first*: a short-lived process (a test, a smoke run, a
                # one-shot CLI) must never mutate runtime state just by booting.
                await asyncio.sleep(period)
                if self._stopping:
                    break
                self.run_maintenance_tick()
        except asyncio.CancelledError:  # pragma: no cover - clean shutdown
            raise
        finally:
            self._task = None

    async def stop(self) -> dict[str, Any]:
        """Cancel the loop and wait for it to finish; never leaks a task."""
        self._stopping = True
        task = self._task
        if task is None:
            return {"stopped": False, "reason": "NOT_RUNNING"}
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # pragma: no cover - shutdown path
            pass
        self._task = None
        return {"stopped": True, "runs": dict(self._runs)}

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def status(self, *, now_utc: int | None = None) -> dict[str, Any]:
        stamp = int(now_utc if now_utc is not None else time.time_ns())
        with self._lock:
            return {
                "page": "lifecycle.status",
                "enabled": bool(self.enabled),
                "running": bool(self._task is not None and not self._task.done()),
                "jobs": dict(DEFAULT_JOBS),
                "runs": dict(self._runs),
                "failures": dict(self._failures),
                "last_run_utc_ns": {job: int(value) for job, value in self._last_run.items()},
                "last_tick_utc_ns": self._last_tick_utc_ns,
                "listen_key": dict(self._listen_key),
                "unknown_orders": self.unknown_orders.status(),
                "retry_delays_s": list(RETRY_DELAYS_S),
                "as_of_utc_ns": stamp,
                "next_totp_window_utc_hour": 3,
                "law": "sec_018 - a dead clock must scream, never whisper",
            }
