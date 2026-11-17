"""NIOS V2.1 -- automated post-mortem learning hook (``G2``).

A losing trade is only expensive once.  If nothing writes it down, the same
mistake is paid for again next week -- which is exactly the failure mode the
entity memory graph exists to prevent.

This module is the **decision layer** between a trade event and the graph:

* it decides whether an event deserves a post-mortem at all
  (a win is not an incident; a 12 bps slippage print is not a spike);
* it derives the entity arms (``symbol:``, ``silo:``, ``strategy:``,
  ``regime:``, ``macro:``) so retrieval can find the lesson later;
* it composes a short, digit-grounded lesson sentence -- no adjectives, no
  speculation, only the numbers that triggered it.

It writes through an injected sink:

* ``graph.record_outcome(**kwargs)`` when an :class:`EntityMemoryGraph` is
  supplied directly (Ring 2 to Ring 2), or
* whatever callable the composition root passes as ``writer=`` -- in practice
  ``ControlPlane.record_post_mortem``, which is what persists the timeline in the
  Ring 0 :class:`AtomicStateJournal` and lets the SSE hub emit
  ``POST_MORTEM_COMPILED``.

Rule 4: this module imports nothing from ``ring0_vault`` and opens no socket.

Harvest note (Rule 1): the automatic post-event entity extraction shape is
translated from ``getzep/graphiti``.  No donor code is imported.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "REJECTION_STATUSES",
    "SLIPPAGE_SPIKE_BPS",
    "PostMortemRecorder",
]

#: ``G2``: a print above this many basis points of slippage is a spike.
SLIPPAGE_SPIKE_BPS: float = 15.0

#: ``G2``: the HTTP/reason codes that always produce a post-mortem.
REJECTION_STATUSES: tuple[int, ...] = (403, 422, 429)

#: Outcome kinds this recorder can emit.
OUTCOME_KINDS: tuple[str, ...] = (
    "LOSING_TRADE",
    "SLIPPAGE_SPIKE",
    "ORDER_REJECTION",
    "TRANCHE_STOP_OUT",
)


def _entity_symbol(value: str) -> str:
    """Sanitize a ticker into an entity-safe token.

    The entity graph forbids path tokens in identifiers (``BTC/USDT`` reads as a
    directory traversal), so the venue convention ``BASE/QUOTE`` folds to
    ``BASE-QUOTE``.  The lesson sentence keeps the original ticker.
    """
    token = str(value or "").strip().upper()
    for bad in ("/", "\\", ":", ".."):
        token = token.replace(bad, "-")
    return token


def _seal(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class PostMortemRecorder:
    """Classify a trade event and compile it into durable entity memory (``G2``)."""

    def __init__(
        self,
        *,
        writer: Callable[..., Any] | None = None,
        graph: Any = None,
        slippage_threshold_bps: float = SLIPPAGE_SPIKE_BPS,
        rejection_statuses: Sequence[int] = REJECTION_STATUSES,
    ) -> None:
        self.writer = writer
        self.graph = graph
        self.slippage_threshold_bps = float(slippage_threshold_bps)
        self.rejection_statuses = tuple(int(item) for item in rejection_statuses)
        self._recorded = 0
        self._skipped = 0
        self._last: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------
    def classify(
        self,
        *,
        net_pnl_micros: int = 0,
        slippage_bps: float = 0.0,
        status_code: int | None = None,
        stop_out: bool = False,
    ) -> dict[str, Any] | None:
        """Return ``None`` when nothing is worth remembering, else the reason.

        Precedence is deliberate: a stop-out outranks a rejection, which outranks
        slippage, which outranks a plain loss.  One event gets one kind, so a
        single bad fill cannot inflate the failure count four times.
        """
        if stop_out:
            return {"outcome_kind": "BREAKER_TRIP", "code": "TRANCH_STOP_OUT", "severity": "CRITICAL"}
        if status_code is not None and int(status_code) in self.rejection_statuses:
            return {
                "outcome_kind": "ORDER_REJECTED",
                "code": f"HTTP_{int(status_code)}",
                "severity": "CRITICAL" if int(status_code) in (403, 429) else "WARNING",
            }
        if float(slippage_bps) > self.slippage_threshold_bps:
            return {
                "outcome_kind": "SLIPPAGE_SPIKE",
                "code": f"SLIPPAGE_{float(slippage_bps):.2f}bps",
                "severity": "WARNING",
            }
        if int(net_pnl_micros) < 0:
            return {
                "outcome_kind": "LOSS",
                "code": f"LOSS_{int(net_pnl_micros)}micros",
                "severity": "WARNING",
            }
        return None

    @staticmethod
    def lesson(
        *,
        outcome_kind: str,
        net_pnl_micros: int,
        slippage_bps: float,
        status_code: int | None,
        symbol: str,
        silo: str,
        regime: str,
    ) -> str:
        """A digit-grounded one-liner; every number it cites is an input."""
        parts = [f"{outcome_kind} on {symbol or 'UNKNOWN'}"]
        if silo:
            parts.append(f"silo {silo}")
        if regime:
            parts.append(f"regime {regime}")
        parts.append(f"net {int(net_pnl_micros)} micros")
        if float(slippage_bps):
            parts.append(f"slippage {float(slippage_bps):.2f} bps")
        if status_code is not None:
            parts.append(f"status {int(status_code)}")
        return "; ".join(parts)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record(
        self,
        *,
        symbol: str = "",
        silo: str = "",
        strategy_id: str = "",
        regime: str = "",
        macro_event_id: str = "",
        net_pnl_micros: int = 0,
        slippage_bps: float = 0.0,
        status_code: int | None = None,
        stop_out: bool = False,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Record one event when it warrants it; always return a decision document."""
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        verdict = self.classify(
            net_pnl_micros=net_pnl_micros,
            slippage_bps=slippage_bps,
            status_code=status_code,
            stop_out=stop_out,
        )
        if verdict is None:
            self._skipped += 1
            return {
                "recorded": False,
                "reason": "NOT_MEMORABLE",
                "outcome_kind": "",
                "as_of_utc_ns": stamp,
            }
        summary = self.lesson(
            outcome_kind=str(verdict["outcome_kind"]),
            net_pnl_micros=int(net_pnl_micros),
            slippage_bps=float(slippage_bps),
            status_code=status_code,
            symbol=str(symbol).upper(),
            silo=str(silo).upper(),
            regime=str(regime).upper(),
        )
        kwargs = {
            "outcome_kind": str(verdict["outcome_kind"]),
            "net_pnl_micros": int(net_pnl_micros),
            "slippage_bps": float(slippage_bps),
            "symbol": _entity_symbol(symbol),
            "silo": str(silo).upper(),
            "strategy_id": str(strategy_id),
            "regime": str(regime).upper(),
            "macro_event_id": str(macro_event_id).upper(),
            "lesson_summary": summary,
            "ts_utc": stamp,
        }
        sink = None
        if self.graph is not None:
            sink = getattr(self.graph, "record_outcome", None)
        if not callable(sink) and self.writer is not None:
            sink = self.writer
        if not callable(sink):
            # No sink is a configuration defect, not a silent success.
            return {
                "recorded": False,
                "reason": "NO_MEMORY_SINK",
                "outcome_kind": str(verdict["outcome_kind"]),
                "lesson_summary": summary,
                "as_of_utc_ns": stamp,
            }
        report = sink(**kwargs)
        payload = report if isinstance(report, Mapping) else {"value": report}
        self._recorded += 1
        document = {
            "recorded": True,
            "outcome_kind": str(verdict["outcome_kind"]),
            "code": str(verdict["code"]),
            "severity": str(verdict["severity"]),
            "lesson_summary": summary,
            "symbol": str(symbol).upper(),
            "silo": str(silo).upper(),
            "regime": str(regime).upper(),
            "net_pnl_micros": int(net_pnl_micros),
            "slippage_bps": round(float(slippage_bps), 6),
            "report": {key: value for key, value in payload.items() if key != "value"},
            "event_type": "POST_MORTEM_COMPILED",
            "as_of_utc_ns": stamp,
        }
        document["post_mortem_sha256"] = _seal(
            {key: value for key, value in document.items() if key != "post_mortem_sha256"}
        )
        self._last = document
        return document

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "page": "post_mortem.status",
            "recorded": int(self._recorded),
            "skipped": int(self._skipped),
            "outcome_kinds": list(OUTCOME_KINDS),
            "slippage_threshold_bps": round(self.slippage_threshold_bps, 6),
            "rejection_statuses": list(self.rejection_statuses),
            "graph_attached": self.graph is not None,
            "writer_attached": self.writer is not None,
            "last": self._last or None,
            "law": "sec_018 Law 3 - a loss that is not written down will be repeated",
        }
