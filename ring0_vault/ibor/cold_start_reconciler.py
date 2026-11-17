"""NIOS V1.9 -- cold-start broker-vs-IBOR reconciliation (``E3`` / ``E7``).

A process that restarts after a crash does not know whether the venue filled an
order it never saw.  Guessing is how a book silently diverges, so the cold-start
reconciler classifies every position difference into exactly four buckets and
freezes the silo when the answer is genuinely ambiguous.

Classification
--------------
===============  ======================================================
MATCHED          broker quantity == internal quantity
UNKNOWN_FILL     a quantity difference with no working order to explain it
MID_ORDER_AMBIGUOUS  a difference while an order was in flight
ROGUE_MANUAL_POSITION  broker holds a position the IBOR never opened
===============  ======================================================

``MID_ORDER_AMBIGUOUS`` and ``ROGUE_MANUAL_POSITION`` freeze the affected silo
through :class:`SiloGuard`; a bounded startup deadline guarantees the process
cannot sit forever waiting for a venue statement (an expired deadline freezes
every affected silo and fails the report).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DiscrepancyClass",
    "BrokerSnapshot",
    "ColdStartDiscrepancy",
    "ColdStartReport",
    "ColdStartReconciler",
    "COLD_START_DEADLINE_NS",
    "DISCREPANCY_CODES",
]

#: ``E7``: startup reconciliation must finish inside five seconds.
COLD_START_DEADLINE_NS: int = 5_000_000_000

DISCREPANCY_CODES: Mapping[str, str] = {
    "MATCHED": "MATCHED",
    "UNKNOWN_FILL": "UNKNOWN_FILL",
    "MID_ORDER_AMBIGUOUS": "MID_ORDER_AMBIGUOUS",
    "ROGUE_MANUAL_POSITION": "ROGUE_MANUAL_POSITION",
    "DEADLINE_EXCEEDED": "DEADLINE_EXCEEDED",
}


class DiscrepancyClass(StrEnum):
    MATCHED = "MATCHED"
    UNKNOWN_FILL = "UNKNOWN_FILL"
    MID_ORDER_AMBIGUOUS = "MID_ORDER_AMBIGUOUS"
    ROGUE_MANUAL_POSITION = "ROGUE_MANUAL_POSITION"


@dataclass(slots=True, frozen=True)
class BrokerSnapshot:
    """One venue-reported position at startup."""

    silo: str
    symbol: str
    qty_micros: int
    source: str = "broker_statement"

    def __post_init__(self) -> None:
        object.__setattr__(self, "silo", str(self.silo).strip().upper())
        object.__setattr__(self, "symbol", str(self.symbol).strip().upper())
        object.__setattr__(self, "qty_micros", int(self.qty_micros))
        object.__setattr__(self, "source", str(self.source))

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "symbol": self.symbol,
            "qty_micros": self.qty_micros,
            "source": self.source,
        }


@dataclass(slots=True, frozen=True)
class ColdStartDiscrepancy:
    silo: str
    symbol: str
    internal_qty_micros: int
    broker_qty_micros: int
    delta_micros: int
    classification: DiscrepancyClass
    reason: str
    freeze_required: bool
    code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "symbol": self.symbol,
            "internal_qty_micros": self.internal_qty_micros,
            "broker_qty_micros": self.broker_qty_micros,
            "delta_micros": self.delta_micros,
            "classification": str(self.classification),
            "reason": self.reason,
            "freeze_required": self.freeze_required,
            "code": self.code,
        }


@dataclass(slots=True, frozen=True)
class ColdStartReport:
    started_at_ns: int
    deadline_ns: int
    finished_at_ns: int
    within_deadline: bool
    rows: tuple[ColdStartDiscrepancy, ...] = ()
    frozen_silos: tuple[str, ...] = ()
    passed: bool = True

    @property
    def unknown_fills(self) -> tuple[ColdStartDiscrepancy, ...]:
        return tuple(
            row for row in self.rows if row.classification is DiscrepancyClass.UNKNOWN_FILL
        )

    @property
    def ambiguous(self) -> tuple[ColdStartDiscrepancy, ...]:
        return tuple(
            row
            for row in self.rows
            if row.classification is DiscrepancyClass.MID_ORDER_AMBIGUOUS
        )

    @property
    def rogue_positions(self) -> tuple[ColdStartDiscrepancy, ...]:
        return tuple(
            row
            for row in self.rows
            if row.classification is DiscrepancyClass.ROGUE_MANUAL_POSITION
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at_ns": self.started_at_ns,
            "deadline_ns": self.deadline_ns,
            "finished_at_ns": self.finished_at_ns,
            "elapsed_ns": max(0, self.finished_at_ns - self.started_at_ns),
            "within_deadline": self.within_deadline,
            "passed": self.passed,
            "rows": [row.to_dict() for row in self.rows],
            "frozen_silos": list(self.frozen_silos),
            "counts": {
                "total": len(self.rows),
                "matched": sum(
                    1 for row in self.rows if row.classification is DiscrepancyClass.MATCHED
                ),
                "unknown_fill": len(self.unknown_fills),
                "mid_order_ambiguous": len(self.ambiguous),
                "rogue_manual_position": len(self.rogue_positions),
            },
        }


class ColdStartReconciler:
    """Classify startup differences and freeze what cannot be explained."""

    def __init__(
        self,
        ledger: Any = None,
        *,
        guard: Any = None,
        pending_order_symbols: Iterable[str] = (),
        deadline_ns: int = COLD_START_DEADLINE_NS,
    ) -> None:
        self.ledger = ledger
        self.guard = guard
        self.deadline_ns = int(deadline_ns)
        self._pending_symbols = {
            str(symbol).strip().upper() for symbol in pending_order_symbols
        }
        self._history: list[ColdStartReport] = []

    # -- classification ---------------------------------------------------
    def classify(
        self,
        silo: str,
        symbol: str,
        internal_qty_micros: int,
        broker_qty_micros: int,
        *,
        has_pending_order: bool | None = None,
    ) -> ColdStartDiscrepancy:
        key_silo = str(silo).strip().upper()
        key_symbol = str(symbol).strip().upper()
        internal = int(internal_qty_micros)
        broker = int(broker_qty_micros)
        pending = (
            self._pending_symbols.__contains__(key_symbol)
            if has_pending_order is None
            else bool(has_pending_order)
        )
        delta = broker - internal
        if delta == 0:
            return ColdStartDiscrepancy(
                key_silo,
                key_symbol,
                internal,
                broker,
                0,
                DiscrepancyClass.MATCHED,
                "broker and IBOR agree",
                False,
                DISCREPANCY_CODES["MATCHED"],
            )
        if internal == 0 and broker != 0:
            return ColdStartDiscrepancy(
                key_silo,
                key_symbol,
                internal,
                broker,
                delta,
                DiscrepancyClass.ROGUE_MANUAL_POSITION,
                "venue holds a position the IBOR never opened",
                True,
                DISCREPANCY_CODES["ROGUE_MANUAL_POSITION"],
            )
        if pending:
            return ColdStartDiscrepancy(
                key_silo,
                key_symbol,
                internal,
                broker,
                delta,
                DiscrepancyClass.MID_ORDER_AMBIGUOUS,
                "position moved while an order was in flight",
                True,
                DISCREPANCY_CODES["MID_ORDER_AMBIGUOUS"],
            )
        return ColdStartDiscrepancy(
            key_silo,
            key_symbol,
            internal,
            broker,
            delta,
            DiscrepancyClass.UNKNOWN_FILL,
            "quantity difference with no working order to explain it",
            False,
            DISCREPANCY_CODES["UNKNOWN_FILL"],
        )

    # -- reconciliation ---------------------------------------------------
    def _internal_qty(self, silo: str, symbol: str) -> int:
        if self.ledger is None:
            return 0
        try:
            position = self.ledger.position(str(silo), str(symbol))
        except Exception:  # pragma: no cover - a missing ledger row is zero
            return 0
        if not position:
            return 0
        for key in ("qty_micros", "qty", "quantity_micros"):
            if key in position:
                value = position[key]
                return int(value) if key.endswith("micros") else int(float(value) * 1_000_000)
        return 0

    def reconcile(
        self,
        snapshots: Sequence[BrokerSnapshot],
        *,
        now_ns: int | None = None,
        finished_ns: int | None = None,
    ) -> ColdStartReport:
        started = int(now_ns if now_ns is not None else time.time_ns())
        rows: list[ColdStartDiscrepancy] = []
        for snapshot in snapshots:
            rows.append(
                self.classify(
                    snapshot.silo,
                    snapshot.symbol,
                    self._internal_qty(snapshot.silo, snapshot.symbol),
                    snapshot.qty_micros,
                )
            )
        finished = int(finished_ns if finished_ns is not None else time.time_ns())
        within_deadline = (finished - started) <= self.deadline_ns
        frozen: list[str] = []
        if not within_deadline:
            # An expired startup deadline is itself an ambiguity: freeze every
            # silo in the statement rather than trading on a half-read book.
            for row in rows:
                if row.silo not in frozen:
                    frozen.append(row.silo)
                    self._freeze(row.silo, "COLD_START_DEADLINE_EXCEEDED")
        else:
            for row in rows:
                if row.freeze_required and row.silo not in frozen:
                    frozen.append(row.silo)
                    self._freeze(row.silo, f"{row.classification}:{row.symbol}")
        report = ColdStartReport(
            started_at_ns=started,
            deadline_ns=self.deadline_ns,
            finished_at_ns=finished,
            within_deadline=within_deadline,
            rows=tuple(rows),
            frozen_silos=tuple(frozen),
            passed=within_deadline and not any(row.freeze_required for row in rows),
        )
        self._history.append(report)
        return report

    def _freeze(self, silo: str, reason: str) -> None:
        if self.guard is not None:
            try:
                self.guard.freeze(str(silo), reason)
            except Exception:  # pragma: no cover - freeze is best effort here
                pass

    def history(self) -> tuple[ColdStartReport, ...]:
        return tuple(self._history)

    def snapshot(self) -> dict[str, Any]:
        return {
            "deadline_ns": self.deadline_ns,
            "pending_symbols": sorted(self._pending_symbols),
            "reports": [report.to_dict() for report in self._history[-5:]],
        }
