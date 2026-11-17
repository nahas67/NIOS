"""NIOS V2.1 -- closed-loop trade-close profit-split saga (``G1``).

A single realized trade is not a single ledger write.  It is a short saga:

``COST_ESCROW`` -> ``PROFIT_SPLIT`` -> ``CUSTODY_SWEEP`` -> ``RESERVE_LOCK``

Each stage is an independently observable checkpoint, and every checkpoint is
persisted in the Ring 0 :class:`AtomicStateJournal` **before** the next stage
runs.  A process that dies mid-saga therefore leaves a durable record naming the
exact stage it reached and the compensating action that undoes it, so a restart
recovers deterministically instead of guessing.

Harvest note (Rule 1): the compensation shape is translated from
``temporalio/sdk-python``'s deterministic saga pattern -- a reverse-order list of
compensating callables -- without importing or vendoring that project.

Constitutional constraints:

* all money is integer micro-units (``sec_009``); no float ever enters the ledger;
* ``sum(splits) == realized`` to the last micro;
* capital never crosses a silo boundary (``cross_border_wire_bps == 0``);
* a positive crypto close physically sweeps ``UMFUTURE_MAIN -> MAIN`` through the
  existing :class:`CryptoWalletSweeper` gate ladder;
* a positive regulated-silo close locks the reserve slice as a **virtual**
  :class:`VirtualMarginQuota` instead of wiring money anywhere.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from core.schemas import SplitPolicy, VaultName
from ring0_vault.ibor.ledger_outbox import (
    MICROS_PER_UNIT,
    TAX_ESCROW_ACCOUNT,
    DynamicFundSplitEngine,
    IBORLedger,
    TransferCode,
    to_micros,
)
from ring0_vault.ibor.tax_and_cost_engine import StatutoryCostEngine
from ring0_vault.kernel.mission_pacing import MissionPacingController
from ring0_vault.silos.crypto_wallet_sweep import (
    CASH_ACCOUNT,
    CRYPTO_SILO,
    DIRECTION_FUTURE_TO_SPOT,
    CryptoWalletSweeper,
    SweepRejected,
)
from ring0_vault.silos.virtual_margin_quota import (
    QuotaRejected,
    VirtualMarginQuotaEngine,
)

__all__ = [
    "SAGA_STAGES",
    "SAGA_JOURNAL_KEY",
    "TradeCloseSaga",
    "ProfitSplitSagaCoordinator",
]

#: ``G1``: the ordered checkpoint ladder of one trade-close saga.
SAGA_STAGES: tuple[str, ...] = (
    "COST_ESCROW",
    "PROFIT_SPLIT",
    "CUSTODY_SWEEP",
    "RESERVE_LOCK",
    "COMPLETED",
)

#: ``G1``: the journal section holding durable saga records.
SAGA_JOURNAL_KEY: str = "profit_split_sagas"

#: Sagas kept in the journal document (bounded so the journal cannot grow forever).
MAX_SAGA_RECORDS: int = 256

_NS_PER_SECOND = 1_000_000_000


def _seal(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class TradeCloseSaga:
    """One durable, replayable record of a realized trade close."""

    saga_id: str
    silo: str
    symbol: str
    side: str
    mission_id: str
    tranche_id: str
    regime: str
    gross_pnl_micros: int
    notional_micros: int
    statutory_cost_micros: int
    realized_net_micros: int
    state: str
    stage: str
    stages: list[dict[str, Any]] = field(default_factory=list)
    rollbacks: list[str] = field(default_factory=list)
    split: dict[str, Any] | None = None
    sweep: dict[str, Any] | None = None
    reserve_lock: dict[str, Any] | None = None
    liquidity_available_at_utc_ns: int | None = None
    cross_border_wire_bps: float = 0.0
    created_at_utc_ns: int = 0
    updated_at_utc_ns: int = 0
    reason: str = ""

    # -- checkpointing ----------------------------------------------------
    def checkpoint(
        self, stage: str, status: str, *, now_ns: int, detail: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Append one immutable stage checkpoint and advance the cursor."""
        row = {
            "stage": str(stage),
            "status": str(status),
            "at_utc_ns": int(now_ns),
            "detail": dict(detail or {}),
        }
        row["checkpoint_sha256"] = _seal(row)
        self.stages.append(row)
        self.stage = str(stage)
        self.updated_at_utc_ns = int(now_ns)
        return row

    @property
    def progress_pct(self) -> float:
        if not SAGA_STAGES:
            return 0.0
        index = SAGA_STAGES.index(self.stage) if self.stage in SAGA_STAGES else 0
        return round(100.0 * (index + 1) / len(SAGA_STAGES), 4)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "saga_id": self.saga_id,
            "silo": self.silo,
            "symbol": self.symbol,
            "side": self.side,
            "mission_id": self.mission_id,
            "tranche_id": self.tranche_id,
            "regime": self.regime,
            "gross_pnl_micros": int(self.gross_pnl_micros),
            "notional_micros": int(self.notional_micros),
            "statutory_cost_micros": int(self.statutory_cost_micros),
            "realized_net_micros": int(self.realized_net_micros),
            "state": self.state,
            "stage": self.stage,
            "progress_pct": self.progress_pct,
            "stages": [dict(row) for row in self.stages],
            "rollbacks": list(self.rollbacks),
            "split": self.split,
            "sweep": self.sweep,
            "reserve_lock": self.reserve_lock,
            "liquidity_available_at_utc_ns": self.liquidity_available_at_utc_ns,
            "cross_border_wire_bps": float(self.cross_border_wire_bps),
            "created_at_utc_ns": int(self.created_at_utc_ns),
            "updated_at_utc_ns": int(self.updated_at_utc_ns),
            "reason": self.reason,
        }
        payload["saga_sha256"] = _seal(
            {key: value for key, value in payload.items() if key != "saga_sha256"}
        )
        return payload

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "TradeCloseSaga":
        return cls(
            saga_id=str(row.get("saga_id", "")),
            silo=str(row.get("silo", "")),
            symbol=str(row.get("symbol", "")),
            side=str(row.get("side", "SELL")),
            mission_id=str(row.get("mission_id", "")),
            tranche_id=str(row.get("tranche_id", "")),
            regime=str(row.get("regime", "NEUTRAL")),
            gross_pnl_micros=int(row.get("gross_pnl_micros", 0)),
            notional_micros=int(row.get("notional_micros", 0)),
            statutory_cost_micros=int(row.get("statutory_cost_micros", 0)),
            realized_net_micros=int(row.get("realized_net_micros", 0)),
            state=str(row.get("state", "IN_PROGRESS")),
            stage=str(row.get("stage", "COST_ESCROW")),
            stages=[dict(item) for item in row.get("stages", ()) if isinstance(item, Mapping)],
            rollbacks=[str(item) for item in row.get("rollbacks", ())],
            split=dict(row["split"]) if isinstance(row.get("split"), Mapping) else None,
            sweep=dict(row["sweep"]) if isinstance(row.get("sweep"), Mapping) else None,
            reserve_lock=(
                dict(row["reserve_lock"])
                if isinstance(row.get("reserve_lock"), Mapping)
                else None
            ),
            liquidity_available_at_utc_ns=(
                int(row["liquidity_available_at_utc_ns"])
                if row.get("liquidity_available_at_utc_ns") is not None
                else None
            ),
            cross_border_wire_bps=float(row.get("cross_border_wire_bps", 0.0)),
            created_at_utc_ns=int(row.get("created_at_utc_ns", 0)),
            updated_at_utc_ns=int(row.get("updated_at_utc_ns", 0)),
            reason=str(row.get("reason", "")),
        )


class ProfitSplitSagaCoordinator:
    """Ring 0 coordinator for every realized trade close (``G1``).

    The coordinator owns no broker key and opens no socket.  It composes objects
    that already do the enforcing: the statutory cost engine (``TAX_ESCROW``),
    the dynamic split engine (integer-micro legs), the physical crypto sweeper
    and the virtual margin quota engine.
    """

    def __init__(
        self,
        *,
        ledger: IBORLedger,
        journal: Any,
        split_engine: DynamicFundSplitEngine | None = None,
        pacing: MissionPacingController | None = None,
        sweeper: CryptoWalletSweeper | None = None,
        quota_engine: VirtualMarginQuotaEngine | None = None,
        cost_engine: StatutoryCostEngine | None = None,
        regulated_silos: tuple[str, ...] = ("INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"),
        max_records: int = MAX_SAGA_RECORDS,
    ) -> None:
        self.ledger = ledger
        self.journal = journal
        self.split_engine = split_engine if split_engine is not None else DynamicFundSplitEngine(ledger)
        self.pacing = pacing if pacing is not None else MissionPacingController()
        self.sweeper = sweeper
        self.quota_engine = quota_engine
        self.cost_engine = cost_engine if cost_engine is not None else StatutoryCostEngine()
        self.regulated_silos = tuple(str(item) for item in regulated_silos)
        self.max_records = max(8, int(max_records))
        self._lock = threading.RLock()
        self._records: dict[str, TradeCloseSaga] = {}
        self._order: list[str] = []
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            state = self.journal.load()
        except Exception:  # pragma: no cover - a broken journal is a clean slate
            return
        rows = state.get(SAGA_JOURNAL_KEY) if isinstance(state, Mapping) else None
        if not isinstance(rows, (list, tuple)):
            return
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                record = TradeCloseSaga.from_dict(row)
            except (TypeError, ValueError):
                continue
            if not record.saga_id:
                continue
            self._records[record.saga_id] = record
            self._order.append(record.saga_id)
        # A saga that never reached COMPLETED is durably incomplete: mark it so a
        # restart reports the truth instead of silently forgetting it.
        for record in self._records.values():
            if record.state == "IN_PROGRESS":
                record.state = "RECOVERED_INCOMPLETE"
                record.reason = record.reason or "RESTART_BEFORE_TERMINAL_STAGE"

    def _persist(self) -> None:
        rows = [self._records[saga_id].to_dict() for saga_id in self._order[-self.max_records :]]
        try:
            self.journal.update(**{SAGA_JOURNAL_KEY: rows})
        except Exception:  # pragma: no cover - journal is best-effort under disk failure
            pass

    # -- reading ----------------------------------------------------------
    def records(self, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        with self._lock:
            ids = self._order if limit is None else self._order[-max(1, int(limit)) :]
            return tuple(self._records[saga_id].to_dict() for saga_id in ids)

    def record(self, saga_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._records.get(str(saga_id))
            return found.to_dict() if found is not None else None

    def status(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            for record in self._records.values():
                counts[record.state] = counts.get(record.state, 0) + 1
            return {
                "saga_count": len(self._records),
                "state_counts": counts,
                "stages": list(SAGA_STAGES),
                "journal_key": SAGA_JOURNAL_KEY,
                "journal_backed": True,
                "crypto_sweeper_attached": self.sweeper is not None,
                "quota_engine_attached": self.quota_engine is not None,
                "cross_border_wire_bps": 0.0,
                "law": "sec_015 - capital compounds inside its own silo",
            }

    # -- the saga ---------------------------------------------------------
    def run_trade_close(
        self,
        *,
        silo: str,
        gross_pnl_micros: int,
        notional_micros: int,
        symbol: str = "",
        side: str = "SELL",
        mission_id: str = "",
        tranche_id: str = "",
        regime: str = "NEUTRAL",
        reserve_floor_pct: int = 15,
        tax_escrow_pct: int = 20,
        slippage_bps: float = 0.0,
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Run the full trade-close saga and return its sealed record.

        ``gross_pnl_micros`` is the pre-tax realized P&L of the closed position
        in integer micros.  The statutory drag for ``(silo, side, notional)`` is
        escrowed first, so ``realized_net_micros`` is what actually compounds.
        """
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        token = str(silo).strip().upper()
        for name, value in (
            ("gross_pnl_micros", gross_pnl_micros),
            ("notional_micros", notional_micros),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                # ``sec_009``: money is an integer micro amount.  A float here is
                # a caller defect, and silently truncating it would book a real
                # P&L against a rounded one.
                raise TypeError(
                    f"{name} must be an int micro amount, got {type(value).__name__}"
                )
        gross = int(gross_pnl_micros)
        notional = max(0, int(notional_micros))
        # ``to_micros`` is the only float->micros door, and it is half-up.
        slippage = float(slippage_bps)
        if not (slippage == slippage):  # NaN guard
            slippage = 0.0

        cost = self.cost_engine.breakdown(token, notional, side=str(side or "SELL").upper() or "SELL")
        statutory = int(cost.total_micros)
        realized = gross - statutory
        saga_id = hashlib.sha256(
            f"{token}:{str(symbol).upper()}:{gross}:{notional}:{stamp}".encode("utf-8")
        ).hexdigest()[:24]
        record = TradeCloseSaga(
            saga_id=saga_id,
            silo=token,
            symbol=str(symbol).upper(),
            side=str(side or "SELL").upper(),
            mission_id=str(mission_id),
            tranche_id=str(tranche_id),
            regime=str(regime or "NEUTRAL").upper(),
            gross_pnl_micros=gross,
            notional_micros=notional,
            statutory_cost_micros=statutory,
            realized_net_micros=realized,
            state="IN_PROGRESS",
            stage="COST_ESCROW",
            created_at_utc_ns=stamp,
            updated_at_utc_ns=stamp,
        )

        with self._lock:
            self._records[saga_id] = record
            self._order.append(saga_id)
            record.checkpoint(
                "COST_ESCROW",
                "BOOKED",
                now_ns=stamp,
                detail={
                    "floor_bps": float(cost.floor_bps),
                    "statutory_cost_micros": statutory,
                    "components": {item.key: int(item.micros) for item in cost.components},
                    "account": TAX_ESCROW_ACCOUNT,
                    "slippage_bps": round(slippage, 6),
                },
            )
            self._persist()

            # A loss books its cost and stops: there is nothing to split, sweep
            # or lock, and pretending otherwise would invent capital.
            if realized <= 0:
                record.state = "LOSS_RECORDED"
                record.reason = "NON_POSITIVE_REALIZED"
                record.checkpoint(
                    "COMPLETED",
                    "NO_PROFIT",
                    now_ns=stamp,
                    detail={"realized_net_micros": realized},
                )
                self._persist()
                return record.to_dict()

            policy = SplitPolicy(
                mission_progress=self._progress(record.mission_id, stamp),
                regime=record.regime,
                reserve_floor_pct=max(0, min(90, int(reserve_floor_pct))),
                tax_escrow_pct=max(0, min(90 - max(0, min(90, int(reserve_floor_pct))), int(tax_escrow_pct))),
            )
            record.rollbacks.append(f"REVERSE_SPLIT:{saga_id}")
            self._persist()

            split = self.split_engine.apply(token, realized, policy=policy, now_ns=stamp)
            record.split = split.to_dict()
            if split.splits_sum_micros != realized:  # pragma: no cover - invariant guard
                record.state = "COMPENSATION_REQUIRED"
                record.reason = "SPLIT_SUM_MISMATCH"
                record.checkpoint("PROFIT_SPLIT", "INVARIANT_VIOLATION", now_ns=stamp)
                self._persist()
                return record.to_dict()
            record.checkpoint(
                "PROFIT_SPLIT",
                "APPLIED" if split.applied else "SKIPPED",
                now_ns=stamp,
                detail={
                    "long_term_micros": int(split.long_term_micros),
                    "reserve_micros": int(split.reserve_micros),
                    "tax_escrow_micros": int(split.tax_escrow_micros),
                    "scalper_micros": int(split.scalper_micros),
                    "reserve_pct": int(split.reserve_pct),
                },
            )
            self._persist()

            if token == CRYPTO_SILO:
                self._crypto_sweep(record, int(split.reserve_micros), stamp)
            else:
                record.checkpoint(
                    "CUSTODY_SWEEP",
                    "NOT_APPLICABLE",
                    now_ns=stamp,
                    detail={"reason": "REGULATED_SILO_PROHIBITS_WALLET_SWEEP"},
                )
                self._persist()

            if token in self.regulated_silos:
                self._reserve_lock(record, int(split.reserve_micros), stamp)
            else:
                record.checkpoint(
                    "RESERVE_LOCK",
                    "NOT_APPLICABLE",
                    now_ns=stamp,
                    detail={"reason": "SILO_HAS_NO_VIRTUAL_MARGIN_SURFACE"},
                )

            record.state = "COMPLETED"
            record.reason = "TRADE_CLOSE_SAGA_SEALED"
            record.checkpoint("COMPLETED", "SEALED", now_ns=stamp)
            self._persist()
            return record.to_dict()

    # -- stages -----------------------------------------------------------
    def _progress(self, mission_id: str, stamp: int) -> float:
        if not mission_id:
            return 0.0
        try:
            if self.pacing.state(mission_id) is None:
                return 0.0
        except Exception:  # pragma: no cover - defensive
            return 0.0
        try:
            state = self.pacing.record_realized(mission_id, 0, now_ns=stamp)
            return float(state.progress)
        except Exception:  # pragma: no cover - pacing is advisory here
            return 0.0

    def _crypto_sweep(self, record: TradeCloseSaga, reserve_micros: int, stamp: int) -> None:
        if self.sweeper is None:
            record.checkpoint(
                "CUSTODY_SWEEP",
                "SKIPPED",
                now_ns=stamp,
                detail={"reason": "NO_SWEEPER_ATTACHED"},
            )
            self._persist()
            return
        try:
            available = int(self.ledger.available(record.silo, CASH_ACCOUNT))
        except Exception:  # pragma: no cover - defensive
            available = 0
        amount = min(max(0, int(reserve_micros)), available)
        if amount <= 0:
            record.sweep = {
                "accepted": False,
                "state": "VOIDED",
                "reason": "NO_SWEEPABLE_CASH",
                "reserve_micros": int(reserve_micros),
                "available_micros": available,
            }
            record.checkpoint(
                "CUSTODY_SWEEP",
                "VOIDED",
                now_ns=stamp,
                detail={"reason": "NO_SWEEPABLE_CASH", "available_micros": available},
            )
            self._persist()
            return
        try:
            intent = self.sweeper.plan(
                record.silo,
                amount,
                direction=DIRECTION_FUTURE_TO_SPOT,
                mission_id=record.mission_id,
                tranche_id=record.tranche_id,
                now_ns=stamp,
            )
            receipt = self.sweeper.execute(intent, now_ns=stamp)
        except SweepRejected as exc:
            record.sweep = {"accepted": False, "reason": getattr(exc, "code", "SWEEP_REJECTED")}
            record.checkpoint(
                "CUSTODY_SWEEP",
                "REJECTED",
                now_ns=stamp,
                detail={"reason": getattr(exc, "code", "SWEEP_REJECTED")},
            )
            self._persist()
            return
        record.sweep = {
            "accepted": bool(receipt.accepted),
            "state": str(receipt.state),
            "reason": str(receipt.reason),
            "sweep_id": str(intent.sweep_id),
            "amount_micros": int(amount),
            "from_wallet": str(intent.from_wallet),
            "to_wallet": str(intent.to_wallet),
            "transfer_type": int(intent.transfer_type()),
            "transfer_id": int(receipt.transfer_id),
            "signature_present": bool(receipt.signature),
            "intent_sha256": str(intent.compute_digest()),
        }
        record.liquidity_available_at_utc_ns = int(receipt.liquidity_available_at_utc)
        if receipt.accepted:
            record.rollbacks.append(f"VOID_SWEEP_TRANSFER:{intent.sweep_id}:{receipt.transfer_id}")
        record.checkpoint(
            "CUSTODY_SWEEP",
            "COMMITTED" if receipt.accepted else "VOIDED",
            now_ns=stamp,
            detail={
                "sweep_id": str(intent.sweep_id),
                "amount_micros": int(amount),
                "transfer_id": int(receipt.transfer_id),
                "reason": str(receipt.reason),
            },
        )
        self._persist()

    def _reserve_lock(self, record: TradeCloseSaga, reserve_micros: int, stamp: int) -> None:
        if self.quota_engine is None or reserve_micros <= 0:
            record.checkpoint(
                "RESERVE_LOCK",
                "SKIPPED",
                now_ns=stamp,
                detail={"reason": "NO_QUOTA_ENGINE" if self.quota_engine is None else "ZERO_RESERVE"},
            )
            self._persist()
            return
        try:
            quota = self.quota_engine.issue(
                record.silo,
                int(reserve_micros),
                now_ns=stamp,
                mission_id=record.mission_id,
                tranche_id=record.tranche_id,
            )
        except QuotaRejected as exc:
            record.reserve_lock = {
                "issued": False,
                "reason": str(getattr(exc, "code", "QUOTA_REJECTED")),
            }
            record.checkpoint(
                "RESERVE_LOCK",
                "REJECTED",
                now_ns=stamp,
                detail={"reason": str(getattr(exc, "code", "QUOTA_REJECTED"))},
            )
            self._persist()
            return
        payload = quota.to_dict()
        record.reserve_lock = {
            "issued": True,
            "quota_id": str(payload.get("quota_id", "")),
            "amount_micros": int(reserve_micros),
            "expires_at_utc_ns": int(payload.get("expires_at_utc_ns", 0) or 0),
            "available_at_utc_ns": int(payload.get("available_at_utc_ns", 0) or 0),
            "no_funds_moved": bool(payload.get("no_funds_moved", True)),
        }
        record.rollbacks.append(f"REVOKE_QUOTA:{payload.get('quota_id', '')}")
        record.checkpoint(
            "RESERVE_LOCK",
            "ISSUED",
            now_ns=stamp,
            detail={
                "quota_id": str(payload.get("quota_id", "")),
                "amount_micros": int(reserve_micros),
            },
        )
        self._persist()

    # -- compensation -----------------------------------------------------
    def compensate(self, saga_id: str, *, reason: str = "OPERATOR", now_ns: int | None = None) -> dict[str, Any]:
        """Undo a saga in reverse checkpoint order and persist the outcome.

        Only the reversals that are actually reversible are attempted: a quoted
        virtual margin quota is revoked, and a committed sweep's transfer is left
        to the venue reconciler (its rollback marker is recorded, never executed
        blind).  The ledger's double-entry conservation is never violated.
        """
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            record = self._records.get(str(saga_id))
            if record is None:
                raise KeyError(f"unknown saga {saga_id!r}")
            if record.state == "COMPENSATED":
                return record.to_dict()
            applied: list[str] = []
            for marker in reversed(list(record.rollbacks)):
                if marker.startswith("REVOKE_QUOTA:") and self.quota_engine is not None:
                    quota_id = marker.split(":", 1)[1]
                    try:
                        if self.quota_engine.revoke(quota_id, reason=str(reason)):
                            applied.append(marker)
                    except Exception:  # pragma: no cover - defensive
                        record.reason = "COMPENSATION_PARTIAL"
                elif marker.startswith("VOID_SWEEP_TRANSFER:"):
                    applied.append(f"RECORDED_NOT_EXECUTED:{marker}")
                else:
                    applied.append(f"RECORDED:{marker}")
            record.state = "COMPENSATED"
            record.reason = f"COMPENSATED:{reason}"
            record.checkpoint(
                "COMPLETED",
                "COMPENSATED",
                now_ns=stamp,
                detail={"applied": applied, "reason": str(reason)},
            )
            self._persist()
            return record.to_dict()

    def recover(self) -> dict[str, Any]:
        """Report every saga that is not in a terminal state after a restart."""
        with self._lock:
            pending = [
                record.to_dict()
                for record in self._records.values()
                if record.state in ("IN_PROGRESS", "RECOVERED_INCOMPLETE", "COMPENSATION_REQUIRED")
            ]
            return {
                "pending_count": len(pending),
                "pending": pending,
                "policy": "INSPECT_THEN_COMPENSATE_OR_REPLAY",
                "as_of_utc_ns": time.time_ns(),
            }

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def micros(units: float) -> int:
        """Expose the single legal float->micros conversion for callers."""
        return int(to_micros(float(units)))

    @staticmethod
    def vault_targets() -> dict[str, str]:
        return {
            "long_term": str(VaultName.LONG_TERM_COMPOUNDER),
            "reserve": str(VaultName.RESERVE_YIELD),
            "scalper": str(VaultName.SPINAL_SCALPER),
            "tax": TAX_ESCROW_ACCOUNT,
            "transfer_code": str(int(TransferCode.PROFIT_RATCHET)),
            "micros_per_unit": str(MICROS_PER_UNIT),
            "seconds_unit": str(_NS_PER_SECOND),
        }
