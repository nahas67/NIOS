"""NIOS V2.1 -- persistent alert and incident center (``G9``).

Every surface that can stop a strategy, freeze a silo, halt the venue or expire a
credential needs one durable place to say so.  The cockpit reads that place; the
operator acknowledges rows there; a restart replays it there.

The store is a single ``alerts`` section of the Ring 0
:class:`AtomicStateJournal`, so an acknowledgement survives a crash exactly like a
logbook entry does.  Rows are immutable once acknowledged: a re-raise of the same
``(code, silo)`` while unacknowledged increments ``occurrences`` instead of
creating a duplicate incident.

Constitutional constraints:

* no float money -- an alert carries integer micro amounts or plain strings;
* no network -- ``source`` names the producer, it never names a destination;
* bounded -- the journal keeps the newest ``max_records`` rows only.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Mapping, Sequence

__all__ = [
    "ALERT_JOURNAL_KEY",
    "ALERT_SEVERITIES",
    "SEVERITY_ORDER",
    "AlertCenter",
]

#: ``G9``: the journal section holding incident rows.
ALERT_JOURNAL_KEY: str = "alerts"

#: ``G9``: the closed severity vocabulary.  The cockpit renders exactly these.
ALERT_SEVERITIES: tuple[str, ...] = ("INFO", "WARNING", "CRITICAL")

#: Numeric ordering so ``?severity=WARNING`` returns WARNING **and** CRITICAL.
SEVERITY_ORDER: Mapping[str, int] = {"INFO": 1, "WARNING": 2, "CRITICAL": 3}

#: Rows kept in the journal document.
MAX_ALERTS: int = 512


def _seal(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class AlertCenter:
    """Journal-backed incident ledger with acknowledgement and de-duplication."""

    def __init__(self, journal: Any, *, max_records: int = MAX_ALERTS) -> None:
        self.journal = journal
        self.max_records = max(16, int(max_records))
        self._lock = threading.RLock()
        self._rows: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            state = self.journal.load()
        except Exception:  # pragma: no cover - a broken journal is a clean slate
            return
        rows = state.get(ALERT_JOURNAL_KEY) if isinstance(state, Mapping) else None
        if not isinstance(rows, (list, tuple)):
            return
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            alert_id = str(row.get("alert_id", "")).strip()
            if not alert_id or alert_id in self._by_id:
                continue
            record = dict(row)
            self._rows.append(record)
            self._by_id[alert_id] = record
        # A journal written by an older build (or by hand) may carry more rows than
        # the retention policy allows; bound it on the way in, not only on write.
        if len(self._rows) > self.max_records:
            dropped = self._rows[: len(self._rows) - self.max_records]
            self._rows = self._rows[-self.max_records :]
            for gone in dropped:
                self._by_id.pop(str(gone.get("alert_id", "")), None)

    def _persist(self) -> None:
        rows = self._rows[-self.max_records :]
        try:
            self.journal.update(**{ALERT_JOURNAL_KEY: rows})
        except Exception:  # pragma: no cover - best effort under disk failure
            pass

    # -- raising ----------------------------------------------------------
    def raise_alert(
        self,
        *,
        code: str,
        severity: str = "WARNING",
        source: str = "",
        silo: str = "",
        message: str = "",
        detail: Mapping[str, Any] | None = None,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Record one incident (or count a repeat of an already-open one)."""
        token = str(severity or "WARNING").strip().upper()
        if token not in SEVERITY_ORDER:
            raise ValueError(f"UNKNOWN_SEVERITY:{severity!r}")
        alert_code = str(code or "").strip().upper()
        if not alert_code:
            raise ValueError("ALERT_CODE_REQUIRED")
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        silo_id = str(silo or "").strip().upper()
        with self._lock:
            for row in reversed(self._rows):
                if (
                    row.get("code") == alert_code
                    and row.get("silo") == silo_id
                    and not row.get("acknowledged")
                    and row.get("severity") == token
                ):
                    row["occurrences"] = int(row.get("occurrences", 1)) + 1
                    row["last_seen_at_utc_ns"] = stamp
                    row["message"] = str(message or row.get("message", "")) or row.get(
                        "message", ""
                    )
                    row["row_sha256"] = _seal(
                        {k: v for k, v in row.items() if k != "row_sha256"}
                    )
                    self._persist()
                    return dict(row)

            alert_id = f"ALRT-{stamp}-{uuid.uuid4().hex[:8]}"
            record: dict[str, Any] = {
                "alert_id": alert_id,
                "code": alert_code,
                "severity": token,
                "source": str(source or "ring3.operator"),
                "silo": silo_id,
                "message": str(message or alert_code),
                "detail": dict(detail or {}),
                "occurrences": 1,
                "acknowledged": False,
                "acknowledged_at_utc_ns": None,
                "acknowledged_by": "",
                "note": "",
                "created_at_utc_ns": stamp,
                "last_seen_at_utc_ns": stamp,
            }
            record["row_sha256"] = _seal(record)
            self._rows.append(record)
            self._by_id[alert_id] = record
            if len(self._rows) > self.max_records:
                dropped = self._rows[: len(self._rows) - self.max_records]
                self._rows = self._rows[-self.max_records :]
                for gone in dropped:
                    self._by_id.pop(str(gone.get("alert_id", "")), None)
            self._persist()
            return dict(record)

    # -- reading ----------------------------------------------------------
    def list_alerts(
        self,
        *,
        severity: str | None = None,
        unacked_only: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            rows = [dict(row) for row in self._rows]
        if severity:
            token = str(severity).strip().upper()
            if token not in SEVERITY_ORDER:
                raise ValueError(f"UNKNOWN_SEVERITY:{severity!r}")
            floor = SEVERITY_ORDER[token]
            rows = [row for row in rows if SEVERITY_ORDER.get(str(row.get("severity")), 0) >= floor]
        if unacked_only:
            rows = [row for row in rows if not row.get("acknowledged")]
        if limit is not None:
            rows = rows[-max(1, int(limit)) :]
        counts = {token: 0 for token in ALERT_SEVERITIES}
        unacked = 0
        for row in rows:
            counts[str(row.get("severity"))] = counts.get(str(row.get("severity")), 0) + 1
            if not row.get("acknowledged"):
                unacked += 1
        return {
            "alerts": rows,
            "count": len(rows),
            "unacked_count": unacked,
            "severity_counts": counts,
            "journal_key": ALERT_JOURNAL_KEY,
            "journal_backed": True,
            "filters": {
                "severity": str(severity).strip().upper() if severity else None,
                "unacked_only": bool(unacked_only),
                "limit": limit,
            },
        }

    def alert(self, alert_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._by_id.get(str(alert_id))
            return dict(found) if found is not None else None

    # -- acknowledging ----------------------------------------------------
    def acknowledge(
        self,
        alert_id: str,
        *,
        operator: str = "ring3.operator",
        note: str = "",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            row = self._by_id.get(str(alert_id))
            if row is None:
                raise KeyError(f"unknown alert {alert_id!r}")
            if not row.get("acknowledged"):
                row["acknowledged"] = True
                row["acknowledged_at_utc_ns"] = stamp
                row["acknowledged_by"] = str(operator or "ring3.operator")
                row["note"] = str(note or "")
                row["row_sha256"] = _seal(
                    {k: v for k, v in row.items() if k != "row_sha256"}
                )
                self._persist()
            return dict(row)

    # -- reporting --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        payload = self.list_alerts()
        return {
            "page": "alerts.status",
            "journal_key": ALERT_JOURNAL_KEY,
            "journal_backed": True,
            "count": payload["count"],
            "unacked_count": payload["unacked_count"],
            "severity_counts": payload["severity_counts"],
            "severities": list(ALERT_SEVERITIES),
            "max_records": self.max_records,
            "as_of_utc_ns": time.time_ns(),
            "law": "sec_016 - an incident is durable, not a transient toast",
        }

    def reset(self) -> dict[str, Any]:
        """Clean-slate hook: drop every row and persist the empty section."""
        with self._lock:
            dropped = len(self._rows)
            self._rows = []
            self._by_id = {}
            self._persist()
            return {"dropped": dropped, "count": 0}

    def codes(self, *, limit: int = 0) -> Sequence[str]:
        with self._lock:
            codes = [str(row.get("code", "")) for row in self._rows]
        return tuple(codes[-limit:] if limit else codes)
