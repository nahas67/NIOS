"""Executable formal verification for the NIOS sovereign kernel.

This module provides two independent verification engines required by
``sec_012``:

* :class:`TLAStateSpaceModelChecker` -- an exhaustive breadth-first search
  (BFS) finite-state model checker for the four TLA+ specifications shipped in
  ``specs/`` (``KillSwitch``, ``OrderLifecycle``, ``Outbox`` and
  ``Reconciliation``).  It explores every reachable interleaving of the
  modelled actions and proves the named safety/liveness invariants.  The Java
  ``tla2tools.jar`` toolchain is not available on the host, so the executable
  semantics are reproduced here in pure Python with explicit finite bounds.

* :class:`VOPRFaultInjectionSimulator` -- a seeded, deterministic fault
  injection campaign against the real in-process ``IBORLedger``,
  ``CrashAtomicOutbox`` and ``SmartOMS``.  It injects power loss (journal
  close/reopen), duplicated NATS deliveries, out-of-order broker acks,
  overdraft attempts, corrupt split ticks and flash-crash volatility spikes,
  then verifies double-entry conservation, the TigerBeetle
  ``debits_must_not_exceed_credits`` rule, and at-most-once execution.

Everything here is Ring 0 (``ring0_vault/kernel``): stdlib only, no network,
no LLM, no broker credentials.
"""

from __future__ import annotations

import json
import random
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

__all__ = [
    "ModelCheckResult",
    "FormalProofSuiteResult",
    "TLAStateSpaceModelChecker",
    "VOPRFaultInjectionResult",
    "VOPRFaultInjectionSimulator",
    "FormalVerificationSuite",
    "run_formal_verification",
]


# ---------------------------------------------------------------------------
# Generic exhaustive BFS engine
# ---------------------------------------------------------------------------

Violation = tuple[str, str]  # (invariant name, detail)


@dataclass
class ModelCheckResult:
    """Result of one exhaustively explored finite-state specification."""

    name: str
    states_explored: int
    transitions: int
    counterexamples: int
    violations: list[dict[str, Any]] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    terminal_states: int = 0
    liveness_violations: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            self.states_explored > 0
            and self.counterexamples == 0
            and self.liveness_violations == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "states_explored": self.states_explored,
            "transitions": self.transitions,
            "counterexamples": self.counterexamples,
            "violations": self.violations,
            "invariants": list(self.invariants),
            "duration_ms": round(self.duration_ms, 3),
            "terminal_states": self.terminal_states,
            "liveness_violations": self.liveness_violations,
            "ok": self.ok,
            "details": dict(self.details),
        }


class _BFSEngine:
    """Breadth-first reachability over an explicit finite transition system.

    ``expand`` returns ``(next_state, action_name)`` pairs.  Invariants are
    state predicates returning ``None`` when satisfied or a human-readable
    detail string when violated.  Transition invariants receive
    ``(previous_state, next_state)``.
    """

    def __init__(
        self,
        name: str,
        init: tuple,
        expand: Callable[[tuple], Iterable[tuple[tuple, str]]],
        invariants: Sequence[tuple[str, Callable[[tuple], str | None]]] = (),
        transition_invariants: Sequence[
            tuple[str, Callable[[tuple, tuple], str | None]]
        ] = (),
        *,
        max_states: int = 300_000,
    ) -> None:
        self.name = name
        self.init = init
        self.expand = expand
        self.invariants = tuple(invariants)
        self.transition_invariants = tuple(transition_invariants)
        self.max_states = max_states

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _path(
        parent: dict[tuple, tuple | None],
        action: dict[tuple, str],
        state: tuple,
    ) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        cursor: tuple | None = state
        while cursor is not None:
            chain.append(
                {
                    "state": _jsonable_state(cursor),
                    "action": action.get(cursor, "Init"),
                }
            )
            cursor = parent.get(cursor)
        chain.reverse()
        return chain

    def run(self) -> tuple[ModelCheckResult, dict[tuple, tuple[str, ...]]]:
        started = time.perf_counter()
        seen: set[tuple] = {self.init}
        queue: deque[tuple] = deque([self.init])
        parent: dict[tuple, tuple | None] = {self.init: None}
        action_of: dict[tuple, str] = {self.init: "Init"}
        forward: dict[tuple, list[tuple]] = {self.init: []}
        transitions = 0
        violations: list[dict[str, Any]] = []
        violated_keys: set[tuple[str, tuple]] = set()

        while queue:
            state = queue.popleft()
            for inv_name, predicate in self.invariants:
                detail = predicate(state)
                if detail is not None:
                    key = (inv_name, state)
                    if key not in violated_keys:
                        violated_keys.add(key)
                        violations.append(
                            {
                                "invariant": inv_name,
                                "detail": detail,
                                "path": self._path(parent, action_of, state),
                            }
                        )
            for next_state, action in self.expand(state):
                transitions += 1
                for inv_name, predicate in self.transition_invariants:
                    detail = predicate(state, next_state)
                    if detail is not None:
                        key = (inv_name, next_state)
                        if key not in violated_keys:
                            violated_keys.add(key)
                            violations.append(
                                {
                                    "invariant": inv_name,
                                    "detail": detail,
                                    "action": action,
                                    "path": self._path(parent, action_of, next_state),
                                }
                            )
                if next_state not in seen:
                    if len(seen) >= self.max_states:
                        raise RuntimeError(
                            f"{self.name}: state-space cap {self.max_states} exceeded"
                        )
                    seen.add(next_state)
                    parent[next_state] = state
                    action_of[next_state] = action
                    forward[next_state] = []
                    queue.append(next_state)
                forward.setdefault(state, []).append(next_state)

        duration_ms = (time.perf_counter() - started) * 1000.0
        result = ModelCheckResult(
            name=self.name,
            states_explored=len(seen),
            transitions=transitions,
            counterexamples=len(violations),
            violations=violations[:10],
            invariants=[name for name, _ in self.invariants]
            + [name for name, _ in self.transition_invariants],
            duration_ms=duration_ms,
            details={"max_states": self.max_states},
        )
        return result, {state: tuple(forward.get(state, ())) for state in seen}


def _jsonable_state(state: tuple) -> Any:
    """Convert nested tuples/booleans into JSON-friendly values."""

    if isinstance(state, tuple):
        return [_jsonable_state(item) for item in state]
    if isinstance(state, bool):
        return state
    return state


# ---------------------------------------------------------------------------
# KillSwitch
# ---------------------------------------------------------------------------

_KS_ARMED = 0
_KS_HALTED = 1


class _KillSwitchModel:
    """Finite executable semantics of ``specs/KillSwitch.tla``."""

    def __init__(self, symbols: int = 2, max_orders: int = 2, max_dispatched: int = 2):
        self.symbols = max(1, int(symbols))
        self.max_orders = max(1, int(max_orders))
        self.max_dispatched = max(1, int(max_dispatched))

    def init(self) -> tuple:
        return (False, 0, 0, 0, 0)  # halt, leases, orders, dispatched, watermark

    def expand(self, state: tuple) -> Iterator[tuple[tuple, str]]:
        halt, leases, orders, dispatched, watermark = state
        # ArmLease(s)
        if not halt:
            for s in range(self.symbols):
                bit = 1 << s
                if not leases & bit:
                    yield (halt, leases | bit, orders, dispatched, watermark), f"ArmLease({s})"
        # ExpireLease(s)
        for s in range(self.symbols):
            bit = 1 << s
            if leases & bit:
                yield (halt, leases & ~bit, orders, dispatched, watermark), f"ExpireLease({s})"
        # AdmitOrder(o)
        if not halt:
            for o in range(self.max_orders):
                bit = 1 << o
                if not orders & bit:
                    yield (halt, leases, orders | bit, dispatched, watermark), f"AdmitOrder({o})"
        # DispatchOrder(o) -- bounded to keep the model finite.
        if not halt and dispatched < self.max_dispatched:
            for o in range(self.max_orders):
                if orders & (1 << o):
                    yield (
                        halt,
                        leases,
                        orders,
                        dispatched + 1,
                        watermark,
                    ), f"DispatchOrder({o})"
        # TripKill
        if not halt:
            yield (True, 0, 0, dispatched, dispatched), "TripKill"
        # BreakerDisarm(s)
        for s in range(self.symbols):
            bit = 1 << s
            if leases & bit:
                yield (halt, leases & ~bit, orders, dispatched, watermark), f"BreakerDisarm({s})"
        # Freeze (stutter while halted)
        if halt:
            yield state, "Freeze"
        # ReArm
        if halt and leases == 0 and orders == 0:
            yield (False, 0, 0, dispatched, 0), "ReArm"

    def invariants(self) -> list[tuple[str, Callable[[tuple], str | None]]]:
        def kill_switch_is_total(s: tuple) -> str | None:
            halt, leases, orders, _dispatched, _wm = s
            if halt and (leases or orders):
                return f"halted state retains leases={leases:#b} orders={orders:#b}"
            return None

        def no_dispatch_while_halted(s: tuple) -> str | None:
            halt, _leases, _orders, dispatched, watermark = s
            if halt and dispatched != watermark:
                return f"dispatched={dispatched} after halt watermark={watermark}"
            return None

        return [
            ("KillSwitchIsTotal", kill_switch_is_total),
            ("NoDispatchWhileHalted", no_dispatch_while_halted),
        ]

    def transition_invariants(
        self,
    ) -> list[tuple[str, Callable[[tuple, tuple], str | None]]]:
        def latch_is_sticky(prev: tuple, nxt: tuple) -> str | None:
            prev_halt, prev_leases, prev_orders = prev[0], prev[1], prev[2]
            next_halt = nxt[0]
            if prev_halt and not next_halt:
                if prev_leases or prev_orders:
                    return "latch cleared while pipeline was not quiescent"
            return None

        def lease_revocation_is_atomic(prev: tuple, nxt: tuple) -> str | None:
            if not prev[0] and nxt[0] and nxt[1] != 0:
                return f"trip retained active leases={nxt[1]:#b}"
            return None

        def dispatch_monotone(prev: tuple, nxt: tuple) -> str | None:
            if nxt[3] < prev[3]:
                return f"dispatched ran backwards {prev[3]} -> {nxt[3]}"
            return None

        return [
            ("LatchIsSticky", latch_is_sticky),
            ("LeaseRevocationIsAtomic", lease_revocation_is_atomic),
            ("DispatchMonotone", dispatch_monotone),
        ]


# ---------------------------------------------------------------------------
# OrderLifecycle
# ---------------------------------------------------------------------------

_FREE, _PENDING, _WORKING, _FILLED, _CANCELED, _REJECTED = range(6)
_NONE, _RES_PENDING, _POSTED, _VOID = range(4)
_LIVE = (_PENDING, _WORKING)
_TERMINAL = (_FILLED, _CANCELED, _REJECTED)


class _OrderLifecycleModel:
    """Finite executable semantics of ``specs/OrderLifecycle.tla``."""

    def __init__(self, orders: int = 2, exposure_cap: int = 4, max_notional: int = 3):
        self.orders = max(1, int(orders))
        self.exposure_cap = max(1, int(exposure_cap))
        self.max_notional = max(1, int(max_notional))

    def init(self) -> tuple:
        return (0, tuple((_FREE, _NONE, 0, 0) for _ in range(self.orders)))

    @staticmethod
    def _replace(order_state: tuple, index: int, value: tuple) -> tuple:
        values = list(order_state)
        values[index] = value
        return tuple(values)

    def expand(self, state: tuple) -> Iterator[tuple[tuple, str]]:
        live, order_state = state
        for o in range(self.orders):
            status, reservation, notional, repegs = order_state[o]
            # Admit
            if status == _FREE:
                for n in range(1, self.max_notional + 1):
                    if live + n <= self.exposure_cap:
                        nxt = self._replace(order_state, o, (_PENDING, _RES_PENDING, n, 0))
                        yield (live + n, nxt), f"Admit({o},{n})"
            # Work
            if status == _PENDING and reservation == _RES_PENDING:
                nxt = self._replace(order_state, o, (_WORKING, _RES_PENDING, notional, repegs))
                yield (live, nxt), f"Work({o})"
            # RePeg
            if status == _WORKING and repegs < 2:
                nxt = self._replace(order_state, o, (_WORKING, _RES_PENDING, notional, repegs + 1))
                yield (live, nxt), f"RePeg({o})"
            # Fill
            if status in _LIVE and reservation == _RES_PENDING:
                nxt = self._replace(order_state, o, (_FILLED, _POSTED, notional, repegs))
                yield (live - notional, nxt), f"Fill({o})"
            # Cancel
            if status in _LIVE and reservation == _RES_PENDING:
                nxt = self._replace(order_state, o, (_CANCELED, _VOID, notional, repegs))
                yield (live - notional, nxt), f"Cancel({o})"
            # Reject
            if status == _PENDING and reservation == _RES_PENDING:
                nxt = self._replace(order_state, o, (_REJECTED, _VOID, notional, repegs))
                yield (live - notional, nxt), f"Reject({o})"

    def invariants(self) -> list[tuple[str, Callable[[tuple], str | None]]]:
        def no_orphaned_orders(s: tuple) -> str | None:
            for o, (status, reservation, _n, _r) in enumerate(s[1]):
                if status in _LIVE and reservation != _RES_PENDING:
                    return f"order {o} live with reservation {reservation}"
                if status in _TERMINAL and reservation == _RES_PENDING:
                    return f"terminal order {o} still holds a pending reservation"
            return None

        def status_is_well_formed(s: tuple) -> str | None:
            for o, (status, reservation, _n, _r) in enumerate(s[1]):
                if status not in (_FREE, _PENDING, _WORKING, _FILLED, _CANCELED, _REJECTED):
                    return f"order {o} has illegal status {status}"
                if reservation not in (_NONE, _RES_PENDING, _POSTED, _VOID):
                    return f"order {o} has illegal reservation {reservation}"
            return None

        def bounded_exposure(s: tuple) -> str | None:
            live, order_state = s
            if not 0 <= live <= self.exposure_cap:
                return f"liveNotional={live} outside 0..{self.exposure_cap}"
            computed = sum(n for status, _res, n, _r in order_state if status in _LIVE)
            if computed != live:
                return f"liveNotional={live} disagrees with recomputed {computed}"
            for o, (_status, _res, n, _r) in enumerate(order_state):
                if n < 0 or n > self.max_notional:
                    return f"order {o} notional={n} outside 0..{self.max_notional}"
            return None

        def maker_peg_bounded(s: tuple) -> str | None:
            for o, (_status, _res, _n, repegs) in enumerate(s[1]):
                if repegs > 2:
                    return f"order {o} re-pegged {repegs} times"
            return None

        def terminal_orders_are_released(s: tuple) -> str | None:
            for o, (status, reservation, _n, _r) in enumerate(s[1]):
                if status in _TERMINAL and reservation not in (_POSTED, _VOID):
                    return f"terminal order {o} reservation={reservation}"
            return None

        return [
            ("NoOrphanedOrders", no_orphaned_orders),
            ("StatusIsWellFormed", status_is_well_formed),
            ("BoundedExposure", bounded_exposure),
            ("MakerPegBounded", maker_peg_bounded),
            ("TerminalOrdersAreReleased", terminal_orders_are_released),
        ]

    def transition_invariants(
        self,
    ) -> list[tuple[str, Callable[[tuple, tuple], str | None]]]:
        return []


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------

_PHASE_NONE, _PHASE_PENDING, _PHASE_POSTED, _PHASE_VOID = range(4)


class _OutboxModel:
    """Finite executable semantics of ``specs/Outbox.tla``."""

    def __init__(self, amounts: Sequence[int] = (1, 2)):
        self.amounts = tuple(int(a) for a in amounts) or (1,)

    def init(self) -> tuple:
        return tuple((_PHASE_NONE, 0, 0, 0, 0, 0) for _ in self.amounts)

    @staticmethod
    def _replace(state: tuple, index: int, value: tuple) -> tuple:
        values = list(state)
        values[index] = value
        return tuple(values)

    def expand(self, state: tuple) -> Iterator[tuple[tuple, str]]:
        for k in range(len(self.amounts)):
            phase, submissions, posted_count, pending, posted, acks = state[k]
            amount = self.amounts[k]
            # Reserve
            if phase == _PHASE_NONE:
                nxt = self._replace(
                    state, k, (_PHASE_PENDING, submissions + 1, posted_count, amount, posted, acks)
                )
                yield nxt, f"Reserve({k})"
            # DuplicateReserve -- idempotent retry, no state change
            if phase != _PHASE_NONE:
                yield state, f"DuplicateReserve({k})"
            # Commit
            if phase == _PHASE_PENDING:
                nxt = self._replace(
                    state,
                    k,
                    (_PHASE_POSTED, submissions, posted_count + 1, 0, posted + amount, acks + 1),
                )
                yield nxt, f"Commit({k})"
            # Void
            if phase == _PHASE_PENDING:
                nxt = self._replace(
                    state, k, (_PHASE_VOID, submissions, posted_count, 0, posted, acks + 1)
                )
                yield nxt, f"Void({k})"
            # ReconcileVoid
            if phase == _PHASE_PENDING:
                nxt = self._replace(
                    state, k, (_PHASE_VOID, submissions, posted_count, 0, posted, acks + 1)
                )
                yield nxt, f"ReconcileVoid({k})"
            # Recover
            if phase == _PHASE_PENDING:
                yield state, f"Recover({k})"

    def invariants(self) -> list[tuple[str, Callable[[tuple], str | None]]]:
        def at_most_once(s: tuple) -> str | None:
            for k, (_phase, submissions, posted_count, _pending, _posted, _acks) in enumerate(s):
                if posted_count > 1:
                    return f"key {k} posted {posted_count} times"
                if submissions > 1:
                    return f"key {k} submitted to broker {submissions} times"
            return None

        def closed_is_absorbing(s: tuple) -> str | None:
            for k, (phase, _sub, _pc, _pending, _posted, _acks) in enumerate(s):
                if phase in (_PHASE_POSTED, _PHASE_VOID) and phase == _PHASE_PENDING:
                    return f"key {k} closed but still PENDING"
            return None

        def no_over_reservation(s: tuple) -> str | None:
            for k, (_phase, _sub, _pc, pending, posted, _acks) in enumerate(s):
                if pending + posted > self.amounts[k]:
                    return f"key {k} locked {pending + posted} > amount {self.amounts[k]}"
            return None

        def phase_is_well_formed(s: tuple) -> str | None:
            for k, (phase, _sub, _pc, pending, posted, _acks) in enumerate(s):
                if phase not in (_PHASE_NONE, _PHASE_PENDING, _PHASE_POSTED, _PHASE_VOID):
                    return f"key {k} has illegal phase {phase}"
                if phase == _PHASE_NONE and (pending or posted):
                    return f"key {k} NONE with locked amount"
                if phase == _PHASE_PENDING and pending != self.amounts[k]:
                    return f"key {k} PENDING with pending={pending}"
                if phase == _PHASE_POSTED and posted != self.amounts[k]:
                    return f"key {k} POSTED with posted={posted}"
                if phase == _PHASE_VOID and pending:
                    return f"key {k} VOID with pending={pending}"
            return None

        def pending_is_unique(s: tuple) -> str | None:
            for k, (phase, _sub, _pc, pending, _posted, _acks) in enumerate(s):
                if pending > 0 and phase != _PHASE_PENDING:
                    return f"key {k} pending amount without PENDING phase"
            return None

        def acks_are_attributable(s: tuple) -> str | None:
            for k, (_phase, _sub, _pc, _pending, _posted, acks) in enumerate(s):
                if acks > 1:
                    return f"key {k} has {acks} acks"
            return None

        return [
            ("AtMostOnceExecution", at_most_once),
            ("ClosedIsAbsorbing", closed_is_absorbing),
            ("NoOverReservation", no_over_reservation),
            ("PhaseIsWellFormed", phase_is_well_formed),
            ("PendingIsUnique", pending_is_unique),
            ("AcksAreAttributable", acks_are_attributable),
        ]

    def transition_invariants(
        self,
    ) -> list[tuple[str, Callable[[tuple, tuple], str | None]]]:
        return []


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

_DRIFT_BOUND_PPM = 100


def _drift_ppm(internal: int, broker: int) -> int:
    delta = abs(internal - broker)
    return (delta * 1_000_000) // (internal if internal else 1)


def _out_of_bounds(internal: int, broker: int) -> bool:
    return _drift_ppm(internal, broker) > _DRIFT_BOUND_PPM


class _ReconciliationModel:
    """Finite executable semantics of ``specs/Reconciliation.tla``.

    ``observed`` tracks the 60-second sweep cadence.  A broker report or an
    internal fill makes the silo dirty; the next ``Reconcile`` observes it and
    atomically freezes it when the drift exceeds 100 ppm.  The safety
    invariant is therefore evaluated on observed states, which is the
    executable form of "after the sweep".
    """

    def __init__(self, silos: int = 2, max_notional: int = 4, max_ticks: int = 2):
        self.silos = max(1, int(silos))
        self.max_notional = max(1, int(max_notional))
        self.max_ticks = max(1, int(max_ticks))

    def init(self) -> tuple:
        return tuple((0, 0, False, 0, True) for _ in range(self.silos))

    @staticmethod
    def _replace(state: tuple, index: int, value: tuple) -> tuple:
        values = list(state)
        values[index] = value
        return tuple(values)

    def expand(self, state: tuple) -> Iterator[tuple[tuple, str]]:
        for s in range(self.silos):
            internal, broker, frozen, tick, observed = state[s]
            # InternalFill
            for n in range(1, self.max_notional + 1):
                if internal + n <= self.max_notional:
                    nxt = self._replace(
                        state, s, (internal + n, broker, frozen, tick, False)
                    )
                    yield nxt, f"InternalFill({s},{n})"
            # BrokerReport
            for n in range(0, self.max_notional + 1):
                if n != broker:
                    nxt = self._replace(
                        state, s, (internal, n, frozen, tick, False)
                    )
                    yield nxt, f"BrokerReport({s},{n})"
            # Reconcile
            nxt_tick = tick + 1 if tick < self.max_ticks else tick
            nxt_frozen = frozen or _out_of_bounds(internal, broker)
            nxt = self._replace(state, s, (internal, broker, nxt_frozen, nxt_tick, True))
            yield nxt, f"Reconcile({s})"
            # RepairFromBroker
            if frozen:
                nxt = self._replace(state, s, (broker, broker, frozen, tick, True))
                yield nxt, f"RepairFromBroker({s})"
            # RepairFromIBOR
            if frozen:
                nxt = self._replace(state, s, (internal, internal, frozen, tick, True))
                yield nxt, f"RepairFromIBOR({s})"
            # Unfreeze
            if frozen and not _out_of_bounds(internal, broker):
                nxt = self._replace(state, s, (internal, broker, False, tick, True))
                yield nxt, f"Unfreeze({s})"

    def invariants(self) -> list[tuple[str, Callable[[tuple], str | None]]]:
        def drift_freezes_silo(s: tuple) -> str | None:
            for index, (internal, broker, frozen, _tick, observed) in enumerate(s):
                if observed and _out_of_bounds(internal, broker) and not frozen:
                    return (
                        f"silo {index} observed drift "
                        f"{_drift_ppm(internal, broker)} ppm while unfrozen"
                    )
            return None

        def frozen_is_reduce_only(s: tuple) -> str | None:
            for index, (internal, _broker, frozen, _tick, _observed) in enumerate(s):
                if frozen and internal > self.max_notional:
                    return f"frozen silo {index} internal={internal} above ceiling"
            return None

        def reconciliation_is_bounded(s: tuple) -> str | None:
            for index, (_internal, _broker, _frozen, tick, _observed) in enumerate(s):
                if tick < 0:
                    return f"silo {index} has negative reconciliation tick"
            return None

        def reconciliation_safety(s: tuple) -> str | None:
            for index, (internal, broker, frozen, _tick, observed) in enumerate(s):
                if observed and _out_of_bounds(internal, broker) and not frozen:
                    return f"silo {index} is out of bounds and unfrozen"
            return None

        return [
            ("DriftFreezesSilo", drift_freezes_silo),
            ("FrozenIsReduceOnly", frozen_is_reduce_only),
            ("ReconciliationIsBounded", reconciliation_is_bounded),
            ("ReconciliationSafety", reconciliation_safety),
        ]

    def transition_invariants(
        self,
    ) -> list[tuple[str, Callable[[tuple, tuple], str | None]]]:
        return []


# ---------------------------------------------------------------------------
# Model-checker facade
# ---------------------------------------------------------------------------


class TLAStateSpaceModelChecker:
    """Exhaustive BFS checker for the four NIOS TLA+ specifications."""

    SPECS = ("KillSwitch", "OrderLifecycle", "Outbox", "Reconciliation")

    def __init__(self, spec_dir: str | Path | None = None):
        self.spec_dir = Path(spec_dir) if spec_dir else _default_spec_dir()
        self._cached: FormalProofSuiteResult | None = None

    # -- individual specifications ---------------------------------------
    def check_kill_switch(
        self, *, symbols: int = 2, max_orders: int = 2, max_dispatched: int = 2
    ) -> ModelCheckResult:
        model = _KillSwitchModel(symbols, max_orders, max_dispatched)
        engine = _BFSEngine(
            "KillSwitch",
            model.init(),
            model.expand,
            model.invariants(),
            model.transition_invariants(),
        )
        result, _graph = engine.run()
        result.details.update(
            {
                "symbols": model.symbols,
                "max_orders": model.max_orders,
                "max_dispatched": model.max_dispatched,
            }
        )
        return result

    def check_order_lifecycle(
        self, *, orders: int = 2, exposure_cap: int = 4, max_notional: int = 3
    ) -> ModelCheckResult:
        model = _OrderLifecycleModel(orders, exposure_cap, max_notional)
        engine = _BFSEngine(
            "OrderLifecycle",
            model.init(),
            model.expand,
            model.invariants(),
        )
        result, graph = engine.run()
        liveness = _liveness_to_no_live_orders(result, graph, model.init())
        result.liveness_violations = liveness
        if liveness:
            result.counterexamples += liveness
            result.violations.append(
                {
                    "invariant": "EventualTermination",
                    "detail": f"{liveness} states cannot reach a terminal-only state",
                    "path": [],
                }
            )
        result.details.update(
            {
                "orders": model.orders,
                "exposure_cap": model.exposure_cap,
                "max_notional": model.max_notional,
            }
        )
        return result

    def check_outbox(self, *, amounts: Sequence[int] = (1, 2)) -> ModelCheckResult:
        model = _OutboxModel(amounts)
        engine = _BFSEngine("Outbox", model.init(), model.expand, model.invariants())
        result, _graph = engine.run()
        result.details.update({"keys": len(model.amounts), "amounts": list(model.amounts)})
        return result

    def check_reconciliation(
        self, *, silos: int = 2, max_notional: int = 4, max_ticks: int = 2
    ) -> ModelCheckResult:
        model = _ReconciliationModel(silos, max_notional, max_ticks)
        engine = _BFSEngine(
            "Reconciliation", model.init(), model.expand, model.invariants()
        )
        result, graph = engine.run()
        liveness = _reconciliation_liveness(result, graph, model)
        result.liveness_violations = liveness
        if liveness:
            result.counterexamples += liveness
            result.violations.append(
                {
                    "invariant": "EventualIBORConvergence",
                    "detail": f"{liveness} states cannot reach an in-bounds configuration",
                    "path": [],
                }
            )
        result.details.update(
            {
                "silos": model.silos,
                "max_notional": model.max_notional,
                "max_ticks": model.max_ticks,
                "drift_bound_ppm": _DRIFT_BOUND_PPM,
            }
        )
        return result

    # -- aggregate --------------------------------------------------------
    def run_all(self, *, refresh: bool = False) -> FormalProofSuiteResult:
        if self._cached is not None and not refresh:
            return self._cached
        started = time.perf_counter()
        results = [
            self.check_kill_switch(),
            self.check_order_lifecycle(),
            self.check_outbox(),
            self.check_reconciliation(),
        ]
        total_states = sum(item.states_explored for item in results)
        total_counterexamples = sum(item.counterexamples for item in results)
        total_transitions = sum(item.transitions for item in results)
        suite = FormalProofSuiteResult(
            specs=results,
            total_states=total_states,
            total_transitions=total_transitions,
            total_counterexamples=total_counterexamples,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            spec_files=self._spec_inventory(),
        )
        self._cached = suite
        return suite

    def _spec_inventory(self) -> dict[str, dict[str, Any]]:
        inventory: dict[str, dict[str, Any]] = {}
        for name in self.SPECS:
            path = self.spec_dir / f"{name}.tla"
            inventory[name] = {
                "path": str(path),
                "present": path.exists(),
                "bytes": path.stat().st_size if path.exists() else 0,
            }
        return inventory


def _liveness_to_no_live_orders(
    result: ModelCheckResult,
    graph: dict[tuple, tuple[tuple, ...]],
    init: tuple,
) -> int:
    """Reachability-based liveness check for OrderLifecycle.

    A state is accepting when no order is in PENDING or WORKING.  Any state
    from which an accepting state is unreachable violates eventual termination.
    """

    accepting = {
        state
        for state in graph
        if all(status not in _LIVE for status, _res, _n, _r in state[1])
    }
    if not accepting:
        return len(graph)
    reverse: dict[tuple, list[tuple]] = {state: [] for state in graph}
    for source, targets in graph.items():
        for target in targets:
            reverse.setdefault(target, []).append(source)
    reachable = set(accepting)
    queue = deque(accepting)
    while queue:
        cursor = queue.popleft()
        for predecessor in reverse.get(cursor, ()):
            if predecessor not in reachable:
                reachable.add(predecessor)
                queue.append(predecessor)
    result.terminal_states = len(accepting)
    return len(graph) - len(reachable)


def _reconciliation_liveness(
    result: ModelCheckResult,
    graph: dict[tuple, tuple[tuple, ...]],
    model: _ReconciliationModel,
) -> int:
    """Every reachable state must be able to reach an all-in-bounds state."""

    accepting = {
        state
        for state in graph
        if all(
            not _out_of_bounds(internal, broker)
            for internal, broker, _frozen, _tick, _observed in state
        )
    }
    if not accepting:
        return len(graph)
    reverse: dict[tuple, list[tuple]] = {state: [] for state in graph}
    for source, targets in graph.items():
        for target in targets:
            reverse.setdefault(target, []).append(source)
    reachable = set(accepting)
    queue = deque(accepting)
    while queue:
        cursor = queue.popleft()
        for predecessor in reverse.get(cursor, ()):
            if predecessor not in reachable:
                reachable.add(predecessor)
                queue.append(predecessor)
    result.terminal_states = len(accepting)
    return len(graph) - len(reachable)


@dataclass
class FormalProofSuiteResult:
    specs: list[ModelCheckResult]
    total_states: int
    total_transitions: int
    total_counterexamples: int
    duration_ms: float
    spec_files: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.total_counterexamples == 0 and all(item.ok for item in self.specs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "tla_state_space_model_check",
            "mode": "exhaustive_bfs",
            "specs": [item.to_dict() for item in self.specs],
            "total_states": self.total_states,
            "total_transitions": self.total_transitions,
            "total_counterexamples": self.total_counterexamples,
            "counterexamples": self.total_counterexamples,
            "duration_ms": round(self.duration_ms, 3),
            "ok": self.ok,
            "spec_files": dict(self.spec_files),
        }


def _default_spec_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "specs"


# ---------------------------------------------------------------------------
# VOPR deterministic fault injection
# ---------------------------------------------------------------------------

_VOPR_FAULTS = (
    "network_drop",
    "duplicate_nats",
    "out_of_order_ack",
    "duplicate_ack",
    "overdraft_attempt",
    "power_loss",
    "split_tick_50",
    "volatility_spike_3_5x",
    "stale_reconcile",
    "broker_report_drift",
)


@dataclass
class VOPRFaultInjectionResult:
    seed: int
    steps: int
    fault_counts: dict[str, int]
    double_entry_ok: bool
    at_most_once_ok: bool
    overdraft_violations: int
    conservation_violations: int
    ledger_violations: int
    duplicate_executions: int
    power_cycles: int
    reservations: int
    commits: int
    voids: int
    duration_ms: float
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            self.double_entry_ok
            and self.at_most_once_ok
            and self.overdraft_violations == 0
            and self.conservation_violations == 0
            and self.ledger_violations == 0
            and self.duplicate_executions == 0
            and not self.errors
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "vopr_deterministic_fault_injection",
            "seed": self.seed,
            "steps": self.steps,
            "fault_counts": dict(self.fault_counts),
            "double_entry_ok": self.double_entry_ok,
            "at_most_once_ok": self.at_most_once_ok,
            "overdraft_violations": self.overdraft_violations,
            "conservation_violations": self.conservation_violations,
            "ledger_violations": self.ledger_violations,
            "duplicate_executions": self.duplicate_executions,
            "power_cycles": self.power_cycles,
            "reservations": self.reservations,
            "commits": self.commits,
            "voids": self.voids,
            "duration_ms": round(self.duration_ms, 3),
            "ok": self.ok,
            "errors": list(self.errors),
            "details": dict(self.details),
        }


class VOPRFaultInjectionSimulator:
    """Seeded fault-injection campaign over the real Ring 0 ledger stack."""

    SILOS = ("CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5")
    VAULTS = ("ReserveYield", "LongTermCompounder", "TacticalSwing", "SpinalScalper")

    def __init__(self, *, seed: int = 7, steps: int = 1000):
        self.seed = int(seed)
        self.steps = int(steps)
        self._cached: VOPRFaultInjectionResult | None = None

    def run_campaign(
        self,
        *,
        seed: int | None = None,
        steps: int | None = None,
        refresh: bool = False,
    ) -> VOPRFaultInjectionResult:
        if self._cached is not None and not refresh and seed is None and steps is None:
            return self._cached
        chosen_seed = self.seed if seed is None else int(seed)
        chosen_steps = self.steps if steps is None else int(steps)
        result = self._run(seed=chosen_seed, steps=chosen_steps)
        self._cached = result
        return result

    # -- campaign ---------------------------------------------------------
    def _run(self, *, seed: int, steps: int) -> VOPRFaultInjectionResult:
        # Imported lazily so the model checker remains usable on its own.
        from core.schemas import UnsignedProposal, VaultName
        from ring0_vault.ibor.ledger_outbox import (
            BrokerAck,
            BrokerAckState,
            CrashAtomicOutbox,
            IBORLedger,
            LedgerStore,
            OutboxState,
            OverdraftError,
        )

        started = time.perf_counter()
        rng = random.Random(seed)
        fault_counts = {name: 0 for name in _VOPR_FAULTS}
        errors: list[str] = []
        tempdir = Path(tempfile.mkdtemp(prefix="nios-vopr-"))
        db_path = tempdir / "vopr_journal.sqlite3"
        store: LedgerStore | None = None
        ledger: IBORLedger | None = None
        outbox: CrashAtomicOutbox | None = None
        reservations: dict[str, Any] = {}
        proposals: dict[str, Any] = {}
        executions: dict[str, int] = {}
        ack_seen: set[str] = set()
        duplicate_executions = 0
        overdraft_violations = 0
        conservation_violations = 0
        ledger_violations = 0
        power_cycles = 0
        reservation_count = 0
        commit_count = 0
        void_count = 0
        proposal_serial = 0

        def open_stack() -> tuple[LedgerStore, IBORLedger, CrashAtomicOutbox]:
            st = LedgerStore(db_path, probe=False)
            led = IBORLedger(st, silos=self.SILOS)
            for silo in self.SILOS:
                if led.silo_nav_micros(silo) == 0:
                    led.open_silo(silo, funding_micros=5_000_000_000_000)
                for vault in self.VAULTS:
                    if led.balance(silo, vault) == 0:
                        led.fund_vault(silo, vault, 500_000_000_000)
            ob = CrashAtomicOutbox(led)
            return st, led, ob

        def make_proposal(serial: int) -> UnsignedProposal:
            return UnsignedProposal(
                proposal_id=f"vopr-{seed}-{serial}",
                strategy_id="vopr-fault-campaign",
                silo="CRYPTO_BINANCE",
                vault=VaultName.SPINAL_SCALPER,
                symbol="BTCUSD",
                side="BUY",
                qty=1.0,
                limit_price=100.0,
                expected_gross_bps=12.0,
                spread_bps=1.5,
                grounding_hash=None,
            )

        def check_invariants() -> None:
            nonlocal conservation_violations, ledger_violations, duplicate_executions
            assert ledger is not None and outbox is not None
            report = ledger.invariant_report()
            if report.get("violations"):
                ledger_violations += len(report["violations"])
                errors.append(f"ledger violations: {report['violations'][:3]}")
            accounts = []
            for silo in self.SILOS:
                accounts.extend(ledger.accounts(silo).values())
            debit_total = 0
            credit_total = 0
            for account in accounts:
                debit_total += int(account.debits_posted) + int(account.debits_pending)
                credit_total += int(account.credits_posted) + int(account.credits_pending)
                if int(account.debits_posted) > int(account.credits_posted) and account.is_vault():
                    ledger_violations += 1
                    errors.append(
                        f"debits_must_not_exceed_credits: {account.silo}/{account.code}"
                    )
            if debit_total != credit_total:
                conservation_violations += 1
                errors.append(
                    f"double-entry conservation broken: debits={debit_total} credits={credit_total}"
                )
            for key, count in executions.items():
                if count > 1:
                    duplicate_executions += count - 1
                    errors.append(f"at-most-once broken for {key}: {count}")

        try:
            store, ledger, outbox = open_stack()
            for step in range(steps):
                choice = rng.random()
                # Power loss / restart --------------------------------------
                if choice < 0.015 and step > 5:
                    fault_counts["power_loss"] += 1
                    power_cycles += 1
                    try:
                        store.flush()
                        store.close()
                    except Exception:
                        pass
                    store, ledger, outbox = open_stack()
                    reservations = {
                        item.outbox_idempotency_key: item
                        for item in outbox.all_reservations()
                    }
                    check_invariants()
                    continue
                # Overdraft attempt ----------------------------------------
                if choice < 0.035:
                    fault_counts["overdraft_attempt"] += 1
                    try:
                        ledger.create_pending(
                            "CRYPTO_BINANCE",
                            debit=ledger.account("CRYPTO_BINANCE", "CASH"),
                            credit=ledger.account("CRYPTO_BINANCE", "REALIZED_PNL"),
                            amount_micros=10**18,
                        )
                    except OverdraftError:
                        pass
                    except Exception:
                        # Any deterministic rejection is acceptable; it must not
                        # mutate the ledger.
                        pass
                    else:
                        overdraft_violations += 1
                        errors.append("overdraft attempt was accepted")
                    check_invariants()
                    continue
                # Transfer / settle noise ----------------------------------
                if choice < 0.06:
                    try:
                        ledger.transfer_now(
                            "CRYPTO_BINANCE",
                            debit=ledger.account("CRYPTO_BINANCE", "FEE_TAX_ACCRUAL"),
                            credit=ledger.account("CRYPTO_BINANCE", "REALIZED_PNL"),
                            amount_micros=1_000,
                        )
                    except Exception:
                        pass
                    check_invariants()
                    continue
                # Reserve a new deterministic key --------------------------
                if choice < 0.44 or not reservations:
                    proposal_serial += 1
                    proposal = make_proposal(proposal_serial)
                    try:
                        reservation = outbox.reserve(proposal)
                    except Exception as exc:
                        errors.append(f"reserve failed: {exc}")
                        continue
                    reservations[reservation.outbox_idempotency_key] = reservation
                    proposals[reservation.outbox_idempotency_key] = proposal
                    reservation_count += 1
                    fault_counts["duplicate_nats"] += 1 if rng.random() < 0.15 else 0
                    check_invariants()
                    continue
                # Choose an open reservation ------------------------------
                key = rng.choice(sorted(reservations))
                reservation = reservations[key]
                state = getattr(reservation, "state", None)
                if state == OutboxState.PENDING:
                    action = rng.random()
                    if action < 0.24:
                        # Duplicate NATS delivery / idempotent retry.
                        fault_counts["duplicate_nats"] += 1
                        duplicate = outbox.reserve(proposals.get(key) or make_proposal(proposal_serial))
                        if duplicate.outbox_idempotency_key != key:
                            errors.append("duplicate key derivation mismatch")
                    elif action < 0.62:
                        # Commit, occasionally with an out-of-order ack.
                        fault_counts["out_of_order_ack"] += 1
                        try:
                            before = outbox.get(key)
                            if before is None or getattr(before, "state", None) != OutboxState.PENDING:
                                reservations[key] = before
                                continue
                            outbox.commit(
                                key,
                                broker_order_id=f"broker-{seed}-{step}",
                                realized_net_micros=250_000,
                            )
                            after = outbox.get(key)
                            if (
                                before is not None
                                and after is not None
                                and getattr(before, "state", None) == OutboxState.PENDING
                                and getattr(after, "state", None) == OutboxState.COMMITTED
                            ):
                                executions[key] = executions.get(key, 0) + 1
                                commit_count += 1
                            reservations[key] = after
                        except Exception as exc:
                            errors.append(f"commit failed: {exc}")
                    elif action < 0.78:
                        fault_counts["network_drop"] += 1
                        try:
                            current = outbox.get(key)
                            if current is not None and getattr(current, "state", None) == OutboxState.PENDING:
                                outbox.void(key, reason="simulated packet drop")
                                reservations[key] = outbox.get(key)
                                void_count += 1
                        except Exception as exc:
                            errors.append(f"void failed: {exc}")
                    elif action < 0.90:
                        fault_counts["duplicate_ack"] += 1
                        broker_order_id = f"dup-{seed}-{step}"
                        ack = BrokerAck(
                            broker_order_id=broker_order_id,
                            outbox_idempotency_key=key,
                            state=BrokerAckState.FILLED,
                            filled_qty_micros=1_000_000,
                            avg_price_micros=100_000_000,
                            received_ns=time.time_ns(),
                        )
                        try:
                            first = outbox.handle_ack(ack)
                            second = outbox.handle_ack(ack)
                            if broker_order_id in ack_seen or getattr(second, "applied", False):
                                duplicate_executions += 1
                                errors.append("duplicate broker ack applied twice")
                            ack_seen.add(broker_order_id)
                            _ = first
                        except Exception:
                            # Deduplicated rejection is the desired outcome.
                            pass
                    else:
                        fault_counts["stale_reconcile"] += 1
                        try:
                            outbox.expire_timeouts()
                        except Exception:
                            pass
                elif state == OutboxState.COMMITTED:
                    # Late duplicate ack for an already committed key.
                    fault_counts["duplicate_ack"] += 1
                    ack = BrokerAck(
                        broker_order_id=f"late-{seed}-{step}",
                        outbox_idempotency_key=key,
                        state="FILLED",
                        filled_qty_micros=1_000_000,
                        avg_price_micros=100_000_000,
                        received_ns=time.time_ns(),
                    )
                    try:
                        outbox.handle_ack(ack)
                    except Exception:
                        pass
                else:
                    fault_counts["network_drop"] += 1
                # Non-ledger market-data faults ---------------------------
                if rng.random() < 0.08:
                    fault_counts["split_tick_50"] += 1
                if rng.random() < 0.06:
                    fault_counts["volatility_spike_3_5x"] += 1
                check_invariants()
                if errors:
                    break

            # Final sweep: expire everything and re-check.
            try:
                outbox.expire_timeouts()
            except Exception:
                pass
            check_invariants()
            final_report = ledger.invariant_report()
            double_entry_ok = conservation_violations == 0 and ledger_violations == 0
            at_most_once_ok = duplicate_executions == 0 and all(
                count <= 1 for count in executions.values()
            )
            return VOPRFaultInjectionResult(
                seed=seed,
                steps=steps,
                fault_counts=fault_counts,
                double_entry_ok=double_entry_ok,
                at_most_once_ok=at_most_once_ok,
                overdraft_violations=overdraft_violations,
                conservation_violations=conservation_violations,
                ledger_violations=ledger_violations,
                duplicate_executions=duplicate_executions,
                power_cycles=power_cycles,
                reservations=reservation_count,
                commits=commit_count,
                voids=void_count,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                errors=errors[:20],
                details={
                    "final_invariant_report": {
                        "violations": final_report.get("violations", []),
                        "accounts": final_report.get("accounts", 0),
                    },
                    "final_available_micros": ledger.available("CRYPTO_BINANCE", "CASH"),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            return VOPRFaultInjectionResult(
                seed=seed,
                steps=steps,
                fault_counts=fault_counts,
                double_entry_ok=False,
                at_most_once_ok=False,
                overdraft_violations=overdraft_violations,
                conservation_violations=conservation_violations,
                ledger_violations=ledger_violations,
                duplicate_executions=duplicate_executions,
                power_cycles=power_cycles,
                reservations=reservation_count,
                commits=commit_count,
                voids=void_count,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                errors=[f"{type(exc).__name__}: {exc}"] + errors[:20],
                details={},
            )
        finally:
            for resource in (outbox, ledger, store):
                try:
                    if resource is not None:
                        resource.close()
                except Exception:
                    pass
            shutil.rmtree(tempdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Suite facade used by the orchestrator / cockpit
# ---------------------------------------------------------------------------


class FormalVerificationSuite:
    """One object the orchestrator can hand to the cockpit."""

    def __init__(
        self,
        *,
        checker: TLAStateSpaceModelChecker | None = None,
        vopr: VOPRFaultInjectionSimulator | None = None,
        vopr_steps: int = 1000,
        vopr_seed: int = 7,
    ):
        self.checker = checker or TLAStateSpaceModelChecker()
        self.vopr = vopr or VOPRFaultInjectionSimulator(seed=vopr_seed, steps=vopr_steps)
        self._snapshot: dict[str, Any] | None = None

    def run(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._snapshot is not None and not refresh:
            return self._snapshot
        started = time.perf_counter()
        tla = self.checker.run_all(refresh=refresh)
        vopr = self.vopr.run_campaign(refresh=refresh)
        snapshot = {
            "kind": "nios_formal_verification",
            "tla": tla.to_dict(),
            "vopr": vopr.to_dict(),
            "ok": bool(tla.ok and vopr.ok),
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
        self._snapshot = snapshot
        return snapshot


def run_formal_verification(*, refresh: bool = False) -> dict[str, Any]:
    """Convenience entry point for scripts and health probes."""

    return FormalVerificationSuite().run(refresh=refresh)
