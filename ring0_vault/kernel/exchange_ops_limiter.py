"""Ring 0 shared exchange-operations rate limiter and OTR meter (SEBI B2).

Governing requirements
----------------------
* **SEBI B2** -- the exchange caps *order operations per second* per trading
  member (``10`` order operations/second).  The cap is a venue obligation, so it
  is enforced once, globally, for every silo that talks to the same member.
* ``sec_004`` -- Ring 0 owns execution safety; Ring 1 may consume a lease but
  never bypasses a Ring 0 limit.
* ``sec_010`` -- the limiter sits on the order path, so it is pure integer
  arithmetic: no allocation on the hot path, no lock, no I/O, no LLM
  (``sec_018`` Law 2).
* ``sec_018`` Law 4 -- a denied operation is a normal outcome, not an error: the
  default answer is "do not send".

Design
------
The bucket is **deterministic integer micro-tokens keyed to a UTC second**, not
a wall-clock sliding window:

* ``MICRO_TOKENS_PER_OP = 1_000_000`` so one second of capacity is an exact
  integer (``10_000_000``) and no floating-point residue can accumulate.
* On a new UTC second the balance refills to ``limit * MICRO_TOKENS_PER_OP``;
  inside the same second it is only ever consumed.  The balance can never go
  negative.
* ``now_ns`` is an argument, not a call to the clock, so the state machine is
  testable and replayable.  A clock that moves *backwards* simply lands on a
  different second and is refilled from that second -- no exception, no
  negative balance, no unbounded grant.
* ``retry_after_ns`` is the exact number of nanoseconds until the next UTC
  second, so a caller can park a proposal instead of spin-retrying.

The Order-to-Trade-Ratio (OTR) meter is the second SEBI B2 obligation: a member
that submits many more orders than it executes is penalised by the exchange.
:class:`OtrMeter` computes the excess against a slab ratio and the resulting
integer micro-penalty; :func:`accrue_otr_penalty` posts it through a duck-typed
ledger (``accrue_fill_charges`` first, ``transfer_now`` second).  The ledger is
never imported -- Ring 0 accounting stays behind its own module boundary.

Nothing here places, signs or cancels an order.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "OPS_LIMIT_PER_SECOND",
    "MICRO_TOKENS_PER_OP",
    "MICRO_TOKENS_PER_SECOND",
    "OpsLimitExceeded",
    "OpsBucketState",
    "GlobalOpsTokenBucket",
    "OtrMeter",
    "accrue_otr_penalty",
]

#: SEBI B2 ceiling: order operations permitted per UTC second.
OPS_LIMIT_PER_SECOND = 10

#: Integer resolution of one order operation (1 op == 1_000_000 micro-tokens).
MICRO_TOKENS_PER_OP = 1_000_000

#: Full one-second capacity in micro-tokens (10_000_000 at the default cap).
MICRO_TOKENS_PER_SECOND = OPS_LIMIT_PER_SECOND * MICRO_TOKENS_PER_OP

_NS_PER_SECOND = 1_000_000_000

#: Ledger account credited with an OTR penalty (``AccountCode.FEES`` value).
_FEE_ACCRUAL_ACCOUNT = "FEE_TAX_ACCRUAL"

#: Ledger account debited when an OTR penalty is posted.
_CASH_ACCOUNT = "CASH"


class OpsLimitExceeded(Exception):
    """Raised by :meth:`GlobalOpsTokenBucket.require` when the cap is spent.

    ``code`` is the stable machine-readable tag consumed by Ring 3 cards and by
    the Ring 1 abstention path (``sec_016``); it is a class attribute so it can
    be matched without constructing an exception.
    """

    code = "OPS-LIMIT"

    def __init__(self, message: str, *, retry_after_ns: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_ns = max(0, int(retry_after_ns))

    def to_dict(self) -> dict[str, Any]:
        """Return the exception as a JSON-safe card payload."""

        return {
            "code": self.code,
            "message": self.message,
            "retry_after_ns": self.retry_after_ns,
        }


@dataclass(slots=True)
class OpsBucketState:
    """Immutable-by-convention view of the bucket for one UTC second."""

    utc_second: int
    tokens_micro: int
    granted: int
    denied: int

    def to_dict(self) -> dict[str, Any]:
        """Return the state as a JSON-safe mapping."""

        return {
            "utc_second": int(self.utc_second),
            "tokens_micro": int(self.tokens_micro),
            "granted": int(self.granted),
            "denied": int(self.denied),
            "remaining_ops": int(self.tokens_micro) // MICRO_TOKENS_PER_OP,
        }


class GlobalOpsTokenBucket:
    """Process-global integer-micro token bucket for SEBI B2 order operations.

    One instance is shared by Ring 1 (spinal scalper intents) and Ring 0 (Smart
    OMS slices, cancels and re-pegs) so the *venue* sees at most
    ``limit_per_second`` operations in any UTC second, no matter how many
    internal producers exist.  The bucket is not thread-safe by design: Ring 1
    and Ring 0 each own a single writer and the object is mutated only from that
    writer (``sec_009`` single-writer rule).
    """

    def __init__(
        self,
        *,
        limit_per_second: int = OPS_LIMIT_PER_SECOND,
        name: str = "GLOBAL_10_OPS",
    ) -> None:
        if int(limit_per_second) < 1:
            raise ValueError("limit_per_second must be >= 1")
        self._name = str(name)
        self._limit_per_second = int(limit_per_second)
        self._micro_tokens_per_second = self._limit_per_second * MICRO_TOKENS_PER_OP
        # ``-1`` means "no second observed yet"; the first acquire refills.
        self._utc_second = -1
        self._tokens_micro = 0
        self._granted = 0
        self._denied = 0

    # -- identity ---------------------------------------------------------
    @property
    def name(self) -> str:
        """Stable name of this bucket (telemetry key)."""

        return self._name

    @property
    def limit_per_second(self) -> int:
        """Configured operations-per-second ceiling."""

        return self._limit_per_second

    @property
    def utc_second(self) -> int:
        """Last UTC second the bucket was touched (``-1`` before first use)."""

        return self._utc_second

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _resolve(now_ns: int | None) -> int:
        """Return an explicit ``now_ns`` or the current clock reading."""

        return int(time.time_ns()) if now_ns is None else int(now_ns)

    def _roll(self, second: int) -> None:
        """Refill to full capacity when ``second`` differs from the stored one.

        A clock that jumps backwards lands on a different second and is simply
        refilled; the balance is never allowed to go negative and no exception
        is raised (a non-monotonic clock must not halt trading).
        """

        if second != self._utc_second:
            self._utc_second = second
            self._tokens_micro = self._micro_tokens_per_second

    # -- hot path ---------------------------------------------------------
    def acquire(self, *, now_ns: int | None = None, cost_ops: int = 1) -> bool:
        """Try to consume ``cost_ops`` operations for the current UTC second.

        Returns ``True`` when the operations were granted, ``False`` when the
        second's capacity is exhausted.  A denial consumes nothing and only
        increments the ``denied`` counter, so a caller may abstain and try again
        on the next second (``sec_018`` Law 4).
        """

        cost = int(cost_ops)
        if cost < 0:
            raise ValueError("cost_ops must be >= 0")
        now = self._resolve(now_ns)
        self._roll(now // _NS_PER_SECOND)
        if cost == 0:
            self._granted += 1
            return True
        needed = cost * MICRO_TOKENS_PER_OP
        if self._tokens_micro >= needed:
            self._tokens_micro -= needed
            self._granted += 1
            return True
        self._denied += 1
        return False

    def require(self, *, now_ns: int | None = None, cost_ops: int = 1) -> None:
        """Consume ``cost_ops`` or raise :class:`OpsLimitExceeded`.

        The raised error carries ``retry_after_ns``: the exact nanoseconds until
        the next UTC second, when the bucket refills to full capacity.
        """

        now = self._resolve(now_ns)
        if self.acquire(now_ns=now, cost_ops=cost_ops):
            return
        retry_after_ns = (self._utc_second + 1) * _NS_PER_SECOND - now
        raise OpsLimitExceeded(
            f"{self._name}: SEBI B2 cap of {self._limit_per_second} ops/s spent "
            f"(requested {int(cost_ops)})",
            retry_after_ns=max(0, retry_after_ns),
        )

    def remaining_ops(self, *, now_ns: int | None = None) -> int:
        """Operations still available in the current UTC second.

        Read-only: a new second reports full capacity without mutating state.
        """

        now = self._resolve(now_ns)
        second = now // _NS_PER_SECOND
        if second != self._utc_second:
            return self._limit_per_second
        return self._tokens_micro // MICRO_TOKENS_PER_OP

    # -- telemetry --------------------------------------------------------
    def state(self) -> OpsBucketState:
        """Return the last observed bucket state (no clock read)."""

        return OpsBucketState(
            utc_second=self._utc_second,
            tokens_micro=self._tokens_micro,
            granted=self._granted,
            denied=self._denied,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe telemetry mapping for Ring 3 cards (``sec_016``)."""

        return {
            "name": self._name,
            "limit_per_second": self._limit_per_second,
            "micro_tokens_per_op": MICRO_TOKENS_PER_OP,
            "micro_tokens_per_second": self._micro_tokens_per_second,
            "utc_second": self._utc_second,
            "tokens_micro": self._tokens_micro,
            "remaining_ops": self._tokens_micro // MICRO_TOKENS_PER_OP,
            "granted": self._granted,
            "denied": self._denied,
        }


class OtrMeter:
    """Order-to-Trade-Ratio meter for SEBI B2 (integer micro-penalties).

    The exchange penalises a member whose submitted-order count exceeds
    ``slab_ratio`` times its executed-order count.  ``record`` is called once per
    settlement window with the two integer counts and returns the per-window
    verdict; the meter accumulates the totals so
    :func:`accrue_otr_penalty` can post the outstanding penalty exactly once.

    Amounts are integer micro-units (``sec_009``: no floating-point money).  The
    ratio itself is a diagnostic float -- it is never used to move money.
    """

    def __init__(
        self,
        *,
        slab_ratio: float = 50.0,
        penalty_micros_per_excess_order: int = 1,
    ) -> None:
        if float(slab_ratio) <= 0.0:
            raise ValueError("slab_ratio must be > 0")
        if int(penalty_micros_per_excess_order) < 0:
            raise ValueError("penalty_micros_per_excess_order must be >= 0")
        self._slab_ratio = float(slab_ratio)
        self._penalty_per_excess = int(penalty_micros_per_excess_order)
        self._samples = 0
        self._total_submitted = 0
        self._total_executed = 0
        self._total_excess = 0
        self._total_penalty_micros = 0
        self._breaches = 0
        self._accrued_micros = 0

    # -- properties -------------------------------------------------------
    @property
    def slab_ratio(self) -> float:
        """Submitted/executed ratio above which orders count as excess."""

        return self._slab_ratio

    @property
    def total_penalty_micros(self) -> int:
        """Penalty accumulated across every recorded window."""

        return self._total_penalty_micros

    @property
    def accrued_micros(self) -> int:
        """Penalty already posted to the ledger."""

        return self._accrued_micros

    @property
    def pending_penalty_micros(self) -> int:
        """Penalty accumulated but not yet posted to the ledger."""

        return max(0, self._total_penalty_micros - self._accrued_micros)

    # -- recording --------------------------------------------------------
    def record(self, *, submitted: int, executed: int) -> dict[str, Any]:
        """Record one OTR window and return its verdict.

        ``otr`` is ``submitted / executed`` when anything executed, otherwise
        the raw submitted count (an infinite ratio is not representable and a
        zero-execution window is the worst case, so it is reported as the count
        itself).  ``excess_orders`` is measured against
        ``slab_ratio * max(executed, 1)`` so a zero-execution window still has a
        finite allowance of ``slab_ratio`` orders.
        """

        submitted_i = int(submitted)
        executed_i = int(executed)
        if submitted_i < 0 or executed_i < 0:
            raise ValueError("submitted and executed must be >= 0")

        otr = (submitted_i / executed_i) if executed_i > 0 else float(submitted_i)
        allowance = int(self._slab_ratio * max(executed_i, 1))
        excess = max(0, submitted_i - allowance)
        penalty_micros = excess * self._penalty_per_excess
        breached = excess > 0

        self._samples += 1
        self._total_submitted += submitted_i
        self._total_executed += executed_i
        self._total_excess += excess
        self._total_penalty_micros += penalty_micros
        if breached:
            self._breaches += 1

        return {
            "submitted": submitted_i,
            "executed": executed_i,
            "otr": otr,
            "slab_ratio": self._slab_ratio,
            "excess_orders": excess,
            "penalty_micros": penalty_micros,
            "breached": breached,
        }

    def mark_accrued(self, amount_micros: int) -> int:
        """Mark ``amount_micros`` of the pending penalty as posted.

        Returns the amount actually applied (clamped to the pending balance) so
        a double-post cannot over-accrue.
        """

        applied = min(max(0, int(amount_micros)), self.pending_penalty_micros)
        self._accrued_micros += applied
        return applied

    def reset(self) -> None:
        """Clear every counter, including the accrual bookkeeping."""

        self._samples = 0
        self._total_submitted = 0
        self._total_executed = 0
        self._total_excess = 0
        self._total_penalty_micros = 0
        self._breaches = 0
        self._accrued_micros = 0

    # -- telemetry --------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe telemetry mapping (aggregate plus accrual state)."""

        aggregate_otr = (
            self._total_submitted / self._total_executed
            if self._total_executed > 0
            else float(self._total_submitted)
        )
        return {
            "slab_ratio": self._slab_ratio,
            "penalty_micros_per_excess_order": self._penalty_per_excess,
            "samples": self._samples,
            "submitted": self._total_submitted,
            "executed": self._total_executed,
            "otr": aggregate_otr,
            "excess_orders": self._total_excess,
            "penalty_micros": self._total_penalty_micros,
            "breaches": self._breaches,
            "breached": self._total_excess > 0,
            "accrued_micros": self._accrued_micros,
            "pending_micros": self.pending_penalty_micros,
        }


def _parameters(func: Any) -> frozenset[str]:
    """Return the parameter names of ``func``, or an empty set if unknown."""

    try:
        return frozenset(inspect.signature(func).parameters)
    except (TypeError, ValueError):  # pragma: no cover - C callables
        return frozenset()


def _accrual_kwargs(func: Any, amount_micros: int, now_ns: int | None) -> dict[str, Any] | None:
    """Build keyword arguments for a duck-typed ``accrue_fill_charges``.

    The ledger's own signature is the contract; this adapter supports both the
    itemised form (``notional_micros`` + ``components``) and the simple
    ``amount_micros`` form, and returns ``None`` when neither is accepted so the
    caller can fall back to ``transfer_now``.
    """

    params = _parameters(func)
    if not params:
        return None
    if "notional_micros" in params:
        kwargs: dict[str, Any] = {
            "notional_micros": int(amount_micros),
            "fee_bps": 0.0,
            "tax_bps": 0.0,
            "components": {"OTR_PENALTY": int(amount_micros)},
        }
    elif "amount_micros" in params:
        kwargs = {"amount_micros": int(amount_micros)}
    else:
        return None
    if "now_ns" in params and now_ns is not None:
        kwargs["now_ns"] = int(now_ns)
    return kwargs


def _accrue_via_ledger(ledger: Any, silo: str, amount_micros: int, now_ns: int | None) -> int:
    """Post ``amount_micros`` through the first ledger hook that accepts it."""

    accrue = getattr(ledger, "accrue_fill_charges", None)
    if callable(accrue):
        kwargs = _accrual_kwargs(accrue, amount_micros, now_ns)
        if kwargs is not None:
            try:
                accrue(silo, **kwargs)
                return int(amount_micros)
            except TypeError:
                pass
            except Exception:
                return 0

    transfer = getattr(ledger, "transfer_now", None)
    if callable(transfer):
        kwargs = {
            "debit": _CASH_ACCOUNT,
            "credit": _FEE_ACCRUAL_ACCOUNT,
            "amount_micros": int(amount_micros),
        }
        if "now_ns" in _parameters(transfer) and now_ns is not None:
            kwargs["now_ns"] = int(now_ns)
        try:
            transfer(silo, **kwargs)
            return int(amount_micros)
        except Exception:
            return 0
    return 0


def accrue_otr_penalty(
    ledger: Any,
    silo: str,
    meter: OtrMeter,
    *,
    now_ns: int | None = None,
) -> int:
    """Post the outstanding OTR penalty for ``silo`` and return the amount.

    The ledger is duck-typed and never imported (Ring 0 keeps its accounting
    behind its own module boundary).  Preference order:

    1. ``ledger.accrue_fill_charges`` -- the itemised statutory accrual path, so
       the penalty lands in ``FEE_TAX_ACCRUAL`` as a tax lot like every other
       charge (``sec_009``).
    2. ``ledger.transfer_now(silo, debit="CASH", credit="FEE_TAX_ACCRUAL", ...)``
       -- a plain double-entry posting for a ledger without the accrual helper.
    3. Nothing -- a ledger that exposes neither hook returns ``0`` and the
       penalty stays pending.  This function never raises for a missing hook.
    """

    pending = meter.pending_penalty_micros
    if pending <= 0:
        return 0
    accrued = _accrue_via_ledger(ledger, silo, pending, now_ns)
    if accrued <= 0:
        return 0
    meter.mark_accrued(accrued)
    return accrued
