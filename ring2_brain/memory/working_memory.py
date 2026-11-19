"""NIOS V2.3 -- Tier 1 crash-safe short-term working memory (``PART 2.1``).

Ring 2 keeps three pieces of *volatile* runtime context that a restart must not
lose:

1. the ``UnknownOrderQueue`` retry ladder for ``5xx UNKNOWN`` orders, so a
   mid-day restart resumes pending broker verification instead of forgetting an
   open query (an order that is "unknown" is never safely re-submitted);
2. the latest **closed and live** bars per ``(silo, symbol, timeframe)``, so
   intraday history and the ``MA``/``VWAP`` continuity survive a restart;
3. bounded agent Q&A threads, each turn carrying a deterministic SHA-256 digest
   so a resumed conversation can prove what it already answered.

Storage is delegated to a **generic bounded namespace backend**.  Ring 2 owns the
*port* (:class:`WorkingMemoryBackend`, structurally typed); the durable *adapter*
is the Ring 0 SQLite WAL store (:mod:`ring0_vault.ibor.wal_state_store`), injected
by a composition root -- ``core/orchestrator.py`` is the only place allowed to
compose the rings.  This module therefore contains **no** Ring 0 import at all, at
module scope or inside a function: the air-gap AST guard in
``tests/test_step2_core_spine.py`` scans the whole file, and a lazily-imported
Ring 0 module is still a Ring 0 import.

.. code-block:: python

    # composition root (Ring 0 adapter -> Ring 2 port)
    from ring0_vault.ibor.wal_state_store import WALStateStore
    store = WorkingMemoryStore(backend=WALStateStore("/data/nios_working_memory.wal.sqlite3"))

With no backend injected the store uses :class:`InMemoryWorkingMemoryBackend`,
which implements exactly the same bounds and ordering in process memory -- so a
Ring 2 test can exercise the real semantics with no disk and no Ring 0 install,
and :attr:`WorkingMemoryStore.durable` truthfully reports ``False``.

Bounds (``PART 2.1`` contract)
------------------------------
============================  =========================================
``RETRY_BACKOFF_S``           ``1s -> 5s -> 10s -> 30s -> 1m``
``MAX_BARS_PER_SERIES``       ``250`` bars per ``(silo, symbol, timeframe)``
``MAX_INSTRUMENTS``           distinct ``(silo, symbol)`` bar series kept
``MAX_CHAT_TURNS``            turns per ``(session, topic)`` thread
``MAX_UNKNOWN_ORDERS``        unresolved retry entries kept
============================  =========================================

Ring containment (Law 1): no broker credential, no exchange socket, no order
signing, no LLM call.  Bars stay in integer micros -- this module never performs
floating-point money arithmetic.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

__all__ = [
    "MAX_BARS_PER_SERIES",
    "MAX_CHAT_TURNS",
    "MAX_INSTRUMENTS",
    "MAX_UNKNOWN_ORDERS",
    "NS_BARS",
    "NS_BAR_SERIES",
    "NS_CHAT",
    "NS_UNKNOWN_ORDERS",
    "RETRY_BACKOFF_S",
    "BarPersistenceSink",
    "InMemoryWorkingMemoryBackend",
    "WorkingMemoryBackend",
    "WorkingMemoryStore",
    "default_working_memory_store",
    "register_working_memory_backend",
    "reset_working_memory_singleton",
    "sha256_turn_digest",
]

#: ``G4``: the tiered ``5xx UNKNOWN`` ladder, identical to
#: ``core.lifecycle_worker.RETRY_DELAYS_S``.  Declared locally so Ring 2 does not
#: import a Ring 3 substrate; a change on either side is a protocol change.
RETRY_BACKOFF_S: tuple[int, ...] = (1, 5, 10, 30, 60)

#: Bars retained per ``(silo, symbol, timeframe)``.
MAX_BARS_PER_SERIES: int = 250

#: Distinct ``(silo, symbol)`` pairs whose bars are retained.
MAX_INSTRUMENTS: int = 1_024

#: Turns retained per ``(session, topic)`` thread.
MAX_CHAT_TURNS: int = 50

#: Unresolved ``5xx UNKNOWN`` retry entries retained.
MAX_UNKNOWN_ORDERS: int = 256

#: Namespace holding ``UnknownOrderQueue`` retry entries.
NS_UNKNOWN_ORDERS: str = "nios.working.unknown_orders"

#: Namespace holding persisted bars (closed and live).
NS_BARS: str = "nios.working.bars"

#: Namespace holding one row per instrument, used to bound instrument count.
NS_BAR_SERIES: str = "nios.working.bar_series"

#: Namespace holding bounded agent Q&A threads.
NS_CHAT: str = "nios.working.chat"

#: Workstation namespaces this store owns (``wipe`` removes exactly these).
WORKING_NAMESPACES: tuple[str, ...] = (
    NS_UNKNOWN_ORDERS,
    NS_BARS,
    NS_BAR_SERIES,
    NS_CHAT,
)

_NS_PER_SECOND = 1_000_000_000

#: Resolution states that end a retry ladder.
TERMINAL_RESOLUTIONS: tuple[str, ...] = ("FILLED", "CANCELLED", "CANCELED", "REJECTED")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def sha256_turn_digest(
    *,
    session_id: str,
    topic: str,
    turn_index: int,
    role: str,
    content: str,
    ts_utc_ns: int,
) -> str:
    """Deterministic digest of one chat turn.

    The digest covers the *content* and its position, never the wall clock of the
    writer, so replaying the same turn in the same thread always yields the same
    commitment.  A resumed conversation can therefore detect an edited or
    reordered transcript instead of trusting it.
    """
    return _sha256(
        {
            "session_id": str(session_id),
            "topic": str(topic),
            "turn_index": int(turn_index),
            "role": str(role),
            "content": str(content),
            "ts_utc_ns": int(ts_utc_ns),
        }
    )


class WorkingMemoryBackend(Protocol):
    """The bounded namespace contract Tier 1 needs (structurally typed).

    Deliberately tiny and storage-agnostic.  ``ring0_vault.ibor.wal_state_store``
    satisfies it; so does :class:`InMemoryWorkingMemoryBackend`; so would a Redis
    or Parquet adapter without touching this module.
    """

    def read_namespace(
        self, namespace: str, *, limit: int | None = None, prefix: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        ...

    def upsert_namespace(
        self,
        namespace: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        key_field: str = "row_key",
        sort_key_field: str = "sort_key",
        now_ns: int | None = None,
    ) -> int:
        ...

    def replace_prefix(
        self,
        namespace: str,
        prefix: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        key_field: str = "row_key",
        sort_key_field: str = "sort_key",
        max_rows: int | None = None,
        now_ns: int | None = None,
    ) -> int:
        ...

    def delete_rows(self, namespace: str, keys: Sequence[str]) -> int:
        ...

    def delete_namespace(self, namespace: str) -> int:
        ...


class InMemoryWorkingMemoryBackend:
    """Process-local bounded backend with the WAL store's ordering semantics.

    Used when no durable backend is configured.  Rows are keyed by ``row_key``
    inside a namespace and ordered by ``(sort_key, row_key)``; ``replace_prefix``
    keeps the newest ``max_rows`` -- exactly the guarantees the SQLite store
    provides, so a Ring 2 test exercises the same semantics with no disk.
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def read_namespace(
        self, namespace: str, *, limit: int | None = None, prefix: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = list((self._rows.get(str(namespace)) or {}).values())
        if prefix is not None:
            head = str(prefix)
            rows = [row for row in rows if str(row.get("row_key", "")).startswith(head)]
        rows.sort(key=lambda row: (int(row.get("sort_key") or 0), str(row.get("row_key", ""))))
        if limit is not None:
            rows = rows[-max(1, int(limit)) :]
        return tuple(dict(row) for row in rows)

    def upsert_namespace(
        self,
        namespace: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        key_field: str = "row_key",
        sort_key_field: str = "sort_key",
        now_ns: int | None = None,
    ) -> int:
        written = 0
        with self._lock:
            bucket = self._rows.setdefault(str(namespace), {})
            for row in rows:
                payload = dict(row)
                key = str(payload.get(key_field, "")).strip()
                if not key:
                    raise ValueError(f"WORKING_ROW_KEY_REQUIRED:{key_field}")
                bucket[key] = payload
                written += 1
        return written

    def replace_prefix(
        self,
        namespace: str,
        prefix: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        key_field: str = "row_key",
        sort_key_field: str = "sort_key",
        max_rows: int | None = None,
        now_ns: int | None = None,
    ) -> int:
        head = str(prefix)
        materialised = [dict(row) for row in rows]
        materialised.sort(
            key=lambda row: (int(row.get(sort_key_field) or 0), str(row.get(key_field, "")))
        )
        if max_rows is not None:
            materialised = materialised[-max(1, int(max_rows)) :]
        with self._lock:
            bucket = self._rows.setdefault(str(namespace), {})
            for key in [key for key in bucket if key.startswith(head)]:
                bucket.pop(key, None)
            for row in materialised:
                bucket[str(row.get(key_field, ""))] = row
        return len(materialised)

    def delete_rows(self, namespace: str, keys: Sequence[str]) -> int:
        removed = 0
        with self._lock:
            bucket = self._rows.get(str(namespace)) or {}
            for key in keys:
                if bucket.pop(str(key), None) is not None:
                    removed += 1
        return removed

    def delete_namespace(self, namespace: str) -> int:
        with self._lock:
            bucket = self._rows.pop(str(namespace), None) or {}
            return len(bucket)


def _default_backend() -> Any | None:
    """Resolve the backend a composition root registered, or ``None``.

    This function deliberately contains **no** Ring 0 import: Ring 2 may not name
    the vault package even inside a function (the air-gap AST guard scans every
    import statement in this file).  A composition root installs the durable
    adapter through :func:`register_working_memory_backend`; without one the
    caller falls back to process memory.
    """
    factory = _BACKEND_FACTORY
    if factory is None:
        return None
    try:
        return factory()
    except Exception:  # noqa: BLE001 - a broken factory is a capability answer
        return None


#: Composition-root hook: returns the durable backend (or ``None``).
_BACKEND_FACTORY: Callable[[], Any] | None = None

_DEFAULT_LOCK = threading.RLock()
_DEFAULT_STORE: "WorkingMemoryStore | None" = None


def register_working_memory_backend(factory: Callable[[], Any] | None) -> None:
    """Install (or clear) the durable backend factory.

    Called by a **composition root** -- the one layer allowed to import both
    rings -- so Ring 2 keeps its air gap while the running application still
    persists onto the Ring 0 SQLite WAL store.  Pass ``None`` to detach (test
    teardown, or an operator returning to a memory-only workspace).
    """
    global _BACKEND_FACTORY
    with _DEFAULT_LOCK:
        _BACKEND_FACTORY = factory if callable(factory) else None


def default_working_memory_store() -> "WorkingMemoryStore":
    """Process-wide Tier-1 store, created on first use.

    Durable when a composition root registered a backend, memory-only otherwise.
    The cached instance is dropped by :func:`reset_working_memory_singleton`, so a
    test that re-points a data root never inherits a handle on the previous one.
    """
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = WorkingMemoryStore()
        return _DEFAULT_STORE


def reset_working_memory_singleton() -> None:
    """Drop the cached default store (test isolation / re-pointed data root)."""
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        _DEFAULT_STORE = None


class BarPersistenceSink:
    """Feature-store sink that also persists bars into Tier 1 (``PART 2.1``).

    ``LiveBarAggregator`` accepts exactly one ``features_sink``.  This adapter
    forwards every hook the wrapped sink implements -- ``ingest_bar``,
    ``update_spot``, ``update_l2``, ``update_option_surface`` -- so attaching it
    changes nothing observable, and additionally writes each **closed** bar to the
    working-memory store.  Persistence is best effort: a disk failure may never
    break tick ingestion, which is why every write is wrapped.

    Live (unclosed) bars are not written on every tick: a partial bucket changes
    on every tick and is re-derived from the next one, so writing it would cost a
    transaction per tick without adding restorable state.  Call
    :meth:`WorkingMemoryStore.persist_bars` at a lifecycle checkpoint when the
    live snapshot matters.
    """

    def __init__(self, sink: Any = None, store: "WorkingMemoryStore | None" = None) -> None:
        self._sink = sink
        self._store = store
        self._persisted = 0
        self._errors: list[str] = []

    @property
    def persisted_bars(self) -> int:
        return int(self._persisted)

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(self._errors[-5:])

    def _forward(self, hook: str, **kwargs: Any) -> None:
        target = getattr(self._sink, hook, None)
        if not callable(target):
            return
        try:
            target(**kwargs)
        except TypeError:
            # A narrower wrapped sink is simply uninterested in this hook.
            return
        except Exception as exc:  # noqa: BLE001 - a sink never breaks ingestion
            self._errors.append(f"{hook}:{type(exc).__name__}:{exc}")

    def ingest_bar(self, *, bar: Mapping[str, Any], **kwargs: Any) -> None:
        self._forward("ingest_bar", bar=bar, **kwargs)
        if self._store is None or not isinstance(bar, Mapping) or not bar.get("closed"):
            return
        try:
            self._store.ingest_bar(bar)
            self._persisted += 1
        except Exception as exc:  # noqa: BLE001 - persistence is best effort
            self._errors.append(f"persist:{type(exc).__name__}:{exc}")

    def update_spot(self, **kwargs: Any) -> None:
        self._forward("update_spot", **kwargs)

    def update_l2(self, **kwargs: Any) -> None:
        self._forward("update_l2", **kwargs)

    def update_option_surface(self, **kwargs: Any) -> None:
        self._forward("update_option_surface", **kwargs)


class WorkingMemoryStore:
    """Bounded, crash-safe Tier 1 working memory.

    ``backend`` may be injected (the WAL store, the in-memory backend, or any
    object satisfying :class:`WorkingMemoryBackend`).  When it is omitted the
    durable backend is resolved lazily on first use; if that is unavailable the
    store falls back to :class:`InMemoryWorkingMemoryBackend` and reports
    ``durable=False`` in :meth:`status` rather than pretending to be durable.
    """

    def __init__(
        self,
        backend: Any | None = None,
        *,
        max_bars_per_series: int = MAX_BARS_PER_SERIES,
        max_instruments: int = MAX_INSTRUMENTS,
        max_chat_turns: int = MAX_CHAT_TURNS,
        max_unknown_orders: int = MAX_UNKNOWN_ORDERS,
        clock_ns: Callable[[], int] | None = None,
    ) -> None:
        self._backend = backend
        self._lock = threading.RLock()
        self.max_bars_per_series = max(8, int(max_bars_per_series))
        self.max_instruments = max(1, int(max_instruments))
        self.max_chat_turns = max(1, int(max_chat_turns))
        self.max_unknown_orders = max(1, int(max_unknown_orders))
        self._clock_ns = clock_ns or time.time_ns

    # -------------------------------------------------------------- backend
    @property
    def backend(self) -> Any:
        """The resolved backend (durable store or the in-memory fallback)."""
        with self._lock:
            if self._backend is None:
                self._backend = _default_backend() or InMemoryWorkingMemoryBackend()
            return self._backend

    @property
    def durable(self) -> bool:
        """True when the backend is the SQLite WAL store (not the fallback)."""
        return not isinstance(self.backend, InMemoryWorkingMemoryBackend)

    def _now(self, now_ns: int | None) -> int:
        return int(now_ns if now_ns is not None else self._clock_ns())

    # ------------------------------------------------------- unknown orders
    @staticmethod
    def retry_delay_s(attempt: int) -> int:
        """The ladder delay after ``attempt`` failed probes (``1/5/10/30/60``)."""
        index = max(0, min(int(attempt), len(RETRY_BACKOFF_S) - 1))
        return int(RETRY_BACKOFF_S[index])

    def record_unknown_order(
        self,
        *,
        order_id: str,
        silo: str = "",
        symbol: str = "",
        reason: str = "5XX_UNKNOWN",
        client_order_id: str | None = None,
        attempts: int = 0,
        next_retry_at_utc_ns: int | None = None,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Persist one unresolved order so a restart does not forget the query."""
        key = str(order_id).strip()
        if not key:
            raise ValueError("UNKNOWN_ORDER_ID_REQUIRED")
        stamp = self._now(now_ns)
        existing = self._unknown_row(key)
        next_retry = int(next_retry_at_utc_ns) if next_retry_at_utc_ns is not None else (
            stamp + self.retry_delay_s(int(attempts)) * _NS_PER_SECOND
        )
        row = {
            "row_key": key,
            "order_id": key,
            "client_order_id": str(client_order_id or (existing or {}).get("client_order_id") or key),
            "silo": str(silo or (existing or {}).get("silo") or "").upper(),
            "symbol": str(symbol or (existing or {}).get("symbol") or "").upper(),
            "reason": str(reason or (existing or {}).get("reason") or "5XX_UNKNOWN"),
            "state": "PENDING",
            "attempts": int(max(0, attempts)),
            "next_retry_at_utc_ns": int(next_retry),
            "enqueued_at_utc_ns": int((existing or {}).get("enqueued_at_utc_ns") or stamp),
            "updated_at_utc_ns": stamp,
            "sort_key": int(next_retry),
        }
        with self._lock:
            self.backend.upsert_namespace(NS_UNKNOWN_ORDERS, [row], now_ns=stamp)
            self._trim_unknown_orders(now_ns=stamp)
        return row

    def _unknown_row(self, order_id: str) -> dict[str, Any] | None:
        for row in self.backend.read_namespace(NS_UNKNOWN_ORDERS):
            if str(row.get("order_id")) == str(order_id):
                return dict(row)
        return None

    def _trim_unknown_orders(self, *, now_ns: int | None = None) -> int:
        rows = self.backend.read_namespace(NS_UNKNOWN_ORDERS)
        excess = len(rows) - self.max_unknown_orders
        if excess <= 0:
            return 0
        doomed = [str(row.get("row_key", "")) for row in rows[:excess]]
        return int(self.backend.delete_rows(NS_UNKNOWN_ORDERS, doomed))

    def unknown_orders(self) -> tuple[dict[str, Any], ...]:
        """Every persisted retry entry, soonest-due first."""
        rows = [dict(row) for row in self.backend.read_namespace(NS_UNKNOWN_ORDERS)]
        rows.sort(key=lambda row: (int(row.get("next_retry_at_utc_ns") or 0), str(row.get("order_id"))))
        return tuple(rows)

    def due_unknown_orders(self, *, now_utc_ns: int | None = None) -> tuple[dict[str, Any], ...]:
        """Entries whose next probe is due (or overdue after a restart)."""
        stamp = self._now(now_utc_ns)
        return tuple(
            row
            for row in self.unknown_orders()
            if str(row.get("state")) == "PENDING"
            and int(row.get("next_retry_at_utc_ns") or 0) <= stamp
        )

    def mark_unknown_attempt(
        self,
        order_id: str,
        *,
        resolution: str = "UNKNOWN_NO_FILL",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Advance one retry: reschedule on ``UNKNOWN``, terminate on a verdict.

        The ladder is bounded exactly like the live queue: after the last rung the
        row becomes ``ESCALATED`` (a human decision), never an infinite loop.
        """
        key = str(order_id).strip()
        stamp = self._now(now_ns)
        with self._lock:
            row = self._unknown_row(key)
            if row is None:
                return {"order_id": key, "state": "MISSING"}
            token = str(resolution or "UNKNOWN").strip().upper()
            row["attempts"] = int(row.get("attempts") or 0) + 1
            row["updated_at_utc_ns"] = stamp
            if token in TERMINAL_RESOLUTIONS:
                row["state"] = "RESOLVED"
                row["resolution"] = token
                row["resolved_at_utc_ns"] = stamp
                self.backend.delete_rows(NS_UNKNOWN_ORDERS, [key])
                return row
            if int(row["attempts"]) >= len(RETRY_BACKOFF_S):
                row["state"] = "ESCALATED"
                row["escalation_state"] = "MANUAL_INTERVENTION_REQUIRED"
                row["resolution"] = "AWAITING_OPERATOR"
                row["next_retry_at_utc_ns"] = 0
                row["sort_key"] = 0
            else:
                delay = self.retry_delay_s(int(row["attempts"]))
                row["next_retry_at_utc_ns"] = stamp + delay * _NS_PER_SECOND
                row["sort_key"] = int(row["next_retry_at_utc_ns"])
            self.backend.upsert_namespace(NS_UNKNOWN_ORDERS, [row], now_ns=stamp)
            return row

    def resolve_unknown_order(
        self, order_id: str, *, resolution: str = "FILLED", now_ns: int | None = None
    ) -> dict[str, Any]:
        """Remove one entry from the ladder (a definitive venue answer arrived)."""
        key = str(order_id).strip()
        stamp = self._now(now_ns)
        with self._lock:
            row = self._unknown_row(key) or {"order_id": key, "row_key": key}
            row = dict(row)
            row["state"] = "RESOLVED"
            row["resolution"] = str(resolution or "FILLED").strip().upper()
            row["resolved_at_utc_ns"] = stamp
            row["updated_at_utc_ns"] = stamp
            self.backend.delete_rows(NS_UNKNOWN_ORDERS, [key])
            return row

    def persist_unknown_queue(self, queue: Any) -> int:
        """Mirror an ``UnknownOrderQueue``'s unresolved rows into the store.

        Duck-typed on ``rows()`` so Ring 2 never imports the Ring 3 worker that
        owns the queue.  Resolved rows are dropped from the store, because the
        store is the *resume* set, not an audit log.
        """
        reader = getattr(queue, "rows", None)
        if not callable(reader):
            return 0
        rows = [dict(row) for row in reader()]
        unresolved = [
            row
            for row in rows
            if str(row.get("state", "")).upper() in ("PENDING", "ESCALATED")
        ]
        stamp = self._now(None)
        payload: list[dict[str, Any]] = []
        for row in unresolved:
            order_id = str(row.get("order_id") or row.get("client_order_id") or "").strip()
            if not order_id:
                continue
            next_retry = int(row.get("next_attempt_at_utc_ns") or row.get("next_retry_at_utc_ns") or 0)
            payload.append(
                {
                    "row_key": order_id,
                    "order_id": order_id,
                    "client_order_id": str(row.get("client_order_id") or order_id),
                    "silo": str(row.get("silo") or "").upper(),
                    "symbol": str(row.get("symbol") or "").upper(),
                    "reason": str(row.get("reason") or "5XX_UNKNOWN"),
                    "state": str(row.get("state") or "PENDING").upper(),
                    "attempts": int(row.get("attempts") or 0),
                    "next_retry_at_utc_ns": next_retry,
                    "enqueued_at_utc_ns": int(row.get("enqueued_at_utc_ns") or stamp),
                    "updated_at_utc_ns": stamp,
                    "sort_key": next_retry,
                    "history": list(row.get("history") or ()),
                }
            )
        with self._lock:
            self.backend.replace_prefix(
                NS_UNKNOWN_ORDERS, "", payload, max_rows=self.max_unknown_orders, now_ns=stamp
            )
            self._trim_unknown_orders(now_ns=stamp)
        return len(payload)

    def restore_into_unknown_queue(self, queue: Any) -> dict[str, Any]:
        """Rehydrate ``queue`` from the store (no order is ever re-submitted)."""
        rows = self.unknown_orders()
        restorer = getattr(queue, "restore", None)
        if callable(restorer):
            report = restorer(rows)
            return dict(report) if isinstance(report, Mapping) else {"restored": len(rows)}
        return {"restored": 0, "unsupported": True, "available": len(rows)}

    # ------------------------------------------------------------------ bars
    @staticmethod
    def bar_row_key(silo: str, symbol: str, timeframe: str, start_utc_ns: int) -> str:
        """Stable, lexically sortable key for one bar."""
        return (
            f"{str(silo).upper()}:{str(symbol).upper()}:{str(timeframe)}:"
            f"{int(start_utc_ns):020d}"
        )

    def ingest_bar(self, bar: Mapping[str, Any], *, now_ns: int | None = None) -> dict[str, Any]:
        """Persist (or refresh) one closed or live bar and enforce both bounds."""
        if not isinstance(bar, Mapping):
            raise TypeError(f"BAR_NOT_MAPPING:{type(bar).__name__}")
        silo = str(bar.get("silo") or "").strip().upper()
        symbol = str(bar.get("symbol") or "").strip().upper()
        timeframe = str(bar.get("timeframe") or "").strip()
        if not silo or not symbol or not timeframe:
            raise ValueError("BAR_REQUIRES_SILO_SYMBOL_TIMEFRAME")
        start_ns = int(bar.get("start_utc_ns") or 0)
        stamp = self._now(now_ns)
        row = dict(bar)
        row.update(
            {
                "silo": silo,
                "symbol": symbol,
                "timeframe": timeframe,
                "start_utc_ns": start_ns,
                "closed": bool(bar.get("closed")),
                "row_key": self.bar_row_key(silo, symbol, timeframe, start_ns),
                "sort_key": start_ns,
                "persisted_at_utc_ns": stamp,
            }
        )
        series_prefix = f"{silo}:{symbol}:{timeframe}:"
        instrument_prefix = f"{silo}:{symbol}:"
        with self._lock:
            self.backend.upsert_namespace(NS_BARS, [row], now_ns=stamp)
            series_rows = self.backend.read_namespace(NS_BARS, prefix=series_prefix)
            excess = len(series_rows) - self.max_bars_per_series
            if excess > 0:
                doomed = [
                    str(item.get("row_key", ""))
                    for item in series_rows[:excess]
                ]
                self.backend.delete_rows(NS_BARS, doomed)
            self.backend.upsert_namespace(
                NS_BAR_SERIES,
                [
                    {
                        "row_key": f"{silo}:{symbol}",
                        "silo": silo,
                        "symbol": symbol,
                        "sort_key": max(start_ns, int(stamp)),
                        "last_bar_start_utc_ns": start_ns,
                        "updated_at_utc_ns": stamp,
                    }
                ],
                now_ns=stamp,
            )
            instruments = self.backend.read_namespace(NS_BAR_SERIES)
            overflow = len(instruments) - self.max_instruments
            if overflow > 0:
                for stale in instruments[:overflow]:
                    instrument_key = str(stale.get("row_key", ""))
                    self.backend.delete_rows(NS_BARS, self._bar_keys_for_instrument(instrument_key))
                    self.backend.delete_rows(NS_BAR_SERIES, [instrument_key])
        return row

    def _bar_keys_for_instrument(self, instrument_key: str) -> list[str]:
        prefix = f"{instrument_key}:"
        return [
            str(row.get("row_key", ""))
            for row in self.backend.read_namespace(NS_BARS, prefix=prefix)
        ]

    def bars(
        self,
        *,
        silo: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        include_live: bool = True,
        limit: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Persisted bars, oldest first, optionally filtered."""
        token = str(silo).strip().upper() if silo else None
        key_symbol = str(symbol).strip().upper() if symbol else None
        frame = str(timeframe).strip() if timeframe else None
        rows = [dict(row) for row in self.backend.read_namespace(NS_BARS)]
        if token is not None:
            rows = [row for row in rows if str(row.get("silo")) == token]
        if key_symbol is not None:
            rows = [row for row in rows if str(row.get("symbol")) == key_symbol]
        if frame is not None:
            rows = [row for row in rows if str(row.get("timeframe")) == frame]
        if not include_live:
            rows = [row for row in rows if bool(row.get("closed"))]
        rows.sort(
            key=lambda row: (
                int(row.get("start_utc_ns") or 0),
                str(row.get("silo")),
                str(row.get("symbol")),
                str(row.get("timeframe")),
            )
        )
        if limit is not None:
            rows = rows[-max(1, int(limit)) :]
        return tuple(rows)

    def latest_bar(
        self, *, silo: str, symbol: str, timeframe: str
    ) -> dict[str, Any] | None:
        rows = self.bars(silo=silo, symbol=symbol, timeframe=timeframe)
        return dict(rows[-1]) if rows else None

    def persist_bars(self, aggregator: Any, *, now_ns: int | None = None) -> int:
        """Snapshot a ``LiveBarAggregator`` (closed + live) into the store."""
        reader = getattr(aggregator, "bars", None)
        if not callable(reader):
            return 0
        try:
            rows = reader(include_live=True)
        except TypeError:  # pragma: no cover - a narrower aggregator build
            rows = reader()
        written = 0
        for row in rows or ():
            if isinstance(row, Mapping):
                self.ingest_bar(row, now_ns=now_ns)
                written += 1
        return written

    def restore_into_aggregator(self, aggregator: Any) -> dict[str, Any]:
        """Rehydrate a ``LiveBarAggregator`` so MA/VWAP continuity survives."""
        rows = self.bars()
        restorer = getattr(aggregator, "restore", None)
        if callable(restorer):
            report = restorer(rows)
            return dict(report) if isinstance(report, Mapping) else {"restored": len(rows)}
        return {"restored": 0, "unsupported": True, "available": len(rows)}

    # ------------------------------------------------------------------ chat
    @staticmethod
    def chat_prefix(session_id: str, topic: str) -> str:
        return f"{str(session_id).strip()}::{str(topic or 'default').strip()}::"

    def append_chat_turn(
        self,
        *,
        session_id: str,
        topic: str = "default",
        role: str = "agent",
        content: str = "",
        metadata: Mapping[str, Any] | None = None,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Append one bounded Q&A turn with a deterministic digest."""
        session = str(session_id).strip()
        if not session:
            raise ValueError("CHAT_SESSION_REQUIRED")
        token = str(topic or "default").strip()
        stamp = self._now(now_ns)
        prefix = self.chat_prefix(session, token)
        with self._lock:
            thread = self.backend.read_namespace(NS_CHAT, prefix=prefix)
            # The index must be **monotonic**, not ``len(thread)``: once the
            # window is full the length stops growing, so a length-derived index
            # would keep re-issuing the last slot's ``row_key``.  A duplicate key
            # is replaced rather than appended, which silently shortened the
            # thread by one row per turn and re-used a digest that already
            # committed to a different sentence.
            turn_index = max(
                (int(item.get("turn_index") or 0) for item in thread), default=-1
            ) + 1
            digest = sha256_turn_digest(
                session_id=session,
                topic=token,
                turn_index=turn_index,
                role=str(role),
                content=str(content),
                ts_utc_ns=stamp,
            )
            row = {
                "row_key": f"{prefix}{turn_index:06d}",
                "session_id": session,
                "topic": token,
                "turn_index": turn_index,
                "role": str(role or "agent"),
                "content": str(content or ""),
                "metadata": dict(metadata or {}),
                "ts_utc_ns": stamp,
                "turn_sha256": digest,
                "sort_key": turn_index,
            }
            rows = [dict(item) for item in thread] + [row]
            self.backend.replace_prefix(
                NS_CHAT, prefix, rows, max_rows=self.max_chat_turns, now_ns=stamp
            )
        return row

    def chat_thread(
        self, session_id: str, topic: str = "default", *, limit: int | None = None
    ) -> tuple[dict[str, Any], ...]:
        """One bounded thread, oldest first."""
        prefix = self.chat_prefix(session_id, topic)
        rows = [dict(row) for row in self.backend.read_namespace(NS_CHAT, prefix=prefix)]
        rows.sort(key=lambda row: (int(row.get("turn_index") or 0), str(row.get("row_key"))))
        if limit is not None:
            rows = rows[-max(1, int(limit)) :]
        return tuple(rows)

    def chat_sessions(self) -> tuple[str, ...]:
        """Distinct ``session::topic`` prefixes currently stored."""
        rows = self.backend.read_namespace(NS_CHAT)
        prefixes = sorted(
            {str(row.get("row_key", "")).rsplit("::", 1)[0] for row in rows if row.get("row_key")}
        )
        return tuple(prefixes)

    # ------------------------------------------------------- restore / export
    def _chat_document(self) -> dict[str, list[dict[str, Any]]]:
        """Every thread keyed by its ``session::topic`` prefix."""
        document: dict[str, list[dict[str, Any]]] = {}
        for prefix in self.chat_sessions():
            session, _, topic = prefix.partition("::")
            document[prefix] = list(self.chat_thread(session, topic))
        return document

    def restore(self) -> dict[str, Any]:
        """Everything a restarted process needs, without mutating the store."""
        return {
            "unknown_orders": self.unknown_orders(),
            "due_unknown_orders": self.due_unknown_orders(),
            "bars": self.bars(),
            "chat": self._chat_document(),
            "counts": self.counts(),
            "durable": self.durable,
        }

    def counts(self) -> dict[str, int]:
        """Row counts per namespace (cheap enough for a status surface)."""
        counts = {
            "unknown_orders": len(self.backend.read_namespace(NS_UNKNOWN_ORDERS)),
            "bars": len(self.backend.read_namespace(NS_BARS)),
            "instruments": len(self.backend.read_namespace(NS_BAR_SERIES)),
            "chat_turns": len(self.backend.read_namespace(NS_CHAT)),
        }
        return counts

    def export(self) -> dict[str, Any]:
        """Deterministic JSON-safe snapshot (two exports of equal state match)."""
        document: dict[str, Any] = {
            "schema_version": "v2.3.working-memory",
            "bounds": {
                "max_bars_per_series": self.max_bars_per_series,
                "max_instruments": self.max_instruments,
                "max_chat_turns": self.max_chat_turns,
                "max_unknown_orders": self.max_unknown_orders,
                "retry_backoff_s": list(RETRY_BACKOFF_S),
            },
            "unknown_orders": list(self.unknown_orders()),
            "bars": list(self.bars()),
            "chat": self._chat_document(),
        }
        document["payload_sha256"] = _sha256(document)
        return document

    def wipe(self) -> dict[str, Any]:
        """Drop every Tier-1 namespace (the workspace-reset hook)."""
        removed: dict[str, int] = {}
        with self._lock:
            for namespace in WORKING_NAMESPACES:
                removed[namespace] = int(self.backend.delete_namespace(namespace))
        return {"reset": True, "removed": removed, "counts": self.counts()}

    def reset(self) -> dict[str, Any]:
        """Alias for :meth:`wipe` (the clean-slate funnel's naming)."""
        return self.wipe()

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "page": "memory.working.status",
            "durable": self.durable,
            "backend": type(self.backend).__name__,
            "namespaces": list(WORKING_NAMESPACES),
            "counts": counts,
            "bounds": {
                "max_bars_per_series": self.max_bars_per_series,
                "max_instruments": self.max_instruments,
                "max_chat_turns": self.max_chat_turns,
                "max_unknown_orders": self.max_unknown_orders,
            },
            "retry_backoff_s": list(RETRY_BACKOFF_S),
            "as_of_utc_ns": self._now(None),
            "ring": "ring2_brain",
            "air_gap": "no credential, no socket, no order signing",
            "law": "PART 2.1 - volatile context is bounded and survives a restart",
        }
