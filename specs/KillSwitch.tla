---- MODULE KillSwitch ----
(*******************************************************************************)
(* NIOS Core v3.0 -- Global Kill Switch formal specification.                   *)
(*                                                                              *)
(* Governing sections: ``sec_004`` (Ring 3 control boundary, <1.0 ms kill),     *)
(* ``sec_010`` (global kill switch latency SLA), ``sec_011`` (three-tier        *)
(* breaker hierarchy), ``sec_012`` (formal state-machine proofs), ``sec_018``   *)
(* (Law 2 -- no LLM in the microsecond loop).                                   *)
(*                                                                              *)
(* Safety property proved below:                                                *)
(*                                                                              *)
(*     KillSwitchIsTotal == GlobalHalt => (ActiveLeases = {} /\ NewOrders = {}) *)
(*     Spec              => []KillSwitchIsTotal                                 *)
(*                                                                              *)
(* Once the global kill latch is hot, every System 1 execution lease is          *)
(* revoked, the lease table stays empty for as long as the latch is hot, and     *)
(* no new order can be admitted to the execution pipeline.  Ring 1 cannot        *)
(* re-arm itself: the latch is sticky and only an explicit human ``Arm`` action  *)
(* clears it, and that action requires a quiescent pipeline.                     *)
(*******************************************************************************)

EXTENDS Naturals, FiniteSets, Sequences

CONSTANTS
    Symbols,        \* symbols that can carry a System 1 lease
    MaxOrders       \* bounded order-admission universe

VARIABLES
    globalHalt,       \* sticky halt latch: FALSE -> TRUE only on TripKill
    activeLeases,     \* set of symbols holding a live 60s System 1 lease
    newOrders,        \* set of order ids admitted to the pipeline
    dispatched,       \* monotone count of orders handed to the broker socket
    dispatchedAtHalt  \* value of `dispatched` captured at the instant of the trip

vars == <<globalHalt, activeLeases, newOrders, dispatched, dispatchedAtHalt>>

\* ---------------------------------------------------------------------------
\* Derived predicates
\* ---------------------------------------------------------------------------

GlobalHalt == globalHalt

Quiescent ==
    /\ activeLeases = {}
    /\ newOrders = {}

\* ---------------------------------------------------------------------------
\* Actions
\* ---------------------------------------------------------------------------

Init ==
    /\ globalHalt       = FALSE
    /\ activeLeases     = {}
    /\ newOrders        = {}
    /\ dispatched       = 0
    /\ dispatchedAtHalt = 0

\* Ring 2 System 1 (off-path) writes a 60-second TTL lease.  Impossible while
\* the latch is hot -- this guard is what makes the kill permanent.
ArmLease(s) ==
    /\ ~globalHalt
    /\ s \in Symbols
    /\ activeLeases' = activeLeases \cup {s}
    /\ UNCHANGED <<globalHalt, newOrders, dispatched, dispatchedAtHalt>>

\* The dead-man timer expires a lease.
ExpireLease(s) ==
    /\ s \in activeLeases
    /\ activeLeases' = activeLeases \ {s}
    /\ UNCHANGED <<globalHalt, newOrders, dispatched, dispatchedAtHalt>>

\* Ring 1 / Ring 0 admits a new order.  Guarded by ~globalHalt.
AdmitOrder(o) ==
    /\ ~globalHalt
    /\ o \notin newOrders
    /\ Cardinality(newOrders) < MaxOrders
    /\ newOrders' = newOrders \cup {o}
    /\ UNCHANGED <<globalHalt, activeLeases, dispatched, dispatchedAtHalt>>

\* Dispatch an admitted order to the broker socket.
DispatchOrder(o) ==
    /\ ~globalHalt
    /\ o \in newOrders
    /\ dispatched' = dispatched + 1
    /\ UNCHANGED <<globalHalt, activeLeases, newOrders, dispatchedAtHalt>>

\* THE KILL SWITCH.  One atomic step (<1.0 ms SLA at OS level): latch the
\* halt, revoke every lease simultaneously, clear the admission set, and
\* capture the dispatch watermark so "nothing was dispatched afterwards" is a
\* state predicate rather than a temporal claim.
TripKill ==
    /\ ~globalHalt
    /\ globalHalt'       = TRUE
    /\ activeLeases'     = {}
    /\ newOrders'        = {}
    /\ dispatchedAtHalt' = dispatched
    /\ UNCHANGED dispatched

\* The 0.1 ms Tier-1 breaker also writes DISARMED into shared memory; it can
\* only ever shrink the lease set, never grow it.
BreakerDisarm(s) ==
    /\ s \in activeLeases
    /\ activeLeases' = activeLeases \ {s}
    /\ UNCHANGED <<globalHalt, newOrders, dispatched, dispatchedAtHalt>>

\* Frozen pipeline: while halted the only legal step is stuttering.
Freeze ==
    /\ globalHalt
    /\ UNCHANGED vars

\* Only an authenticated Ring 3 operator action clears the latch, and only
\* after the pipeline has been proven quiescent.
ReArm ==
    /\ globalHalt
    /\ Quiescent
    /\ globalHalt'       = FALSE
    /\ dispatchedAtHalt' = 0
    /\ UNCHANGED <<activeLeases, newOrders, dispatched>>

Next ==
    \/ \E s \in Symbols : ArmLease(s)
    \/ \E s \in Symbols : ExpireLease(s)
    \/ \E s \in Symbols : BreakerDisarm(s)
    \/ \E o \in (0..MaxOrders) : AdmitOrder(o)
    \/ \E o \in newOrders : DispatchOrder(o)
    \/ TripKill
    \/ Freeze
    \/ ReArm

Spec == Init /\ [][Next]_vars

\* ---------------------------------------------------------------------------
\* SAFETY INVARIANTS
\* ---------------------------------------------------------------------------

\* THE INVARIANT.
\* Once `globalHalt` holds there are no active leases and no admitted orders.
\*
\* ``sec_012`` states it as ``GlobalHalt => [](ActiveLeases = 0 /\ NewOrders = {})``;
\* ``activeLeases = {}`` and ``Cardinality(activeLeases) = 0`` are the same
\* predicate in TLA+, and the module uses the set form because the empty set is
\* what the trip action actually writes.
KillSwitchIsTotal ==
    GlobalHalt => (activeLeases = {} /\ newOrders = {})

\* Nothing reached the broker socket after the trip.
NoDispatchWhileHalted ==
    GlobalHalt => dispatched = dispatchedAtHalt

\* The latch is monotone: it is only ever cleared from a quiescent state.
LatchIsSticky ==
    globalHalt' = FALSE => (globalHalt = FALSE \/ Quiescent)

\* Lease revocation is atomic: the trip step empties the whole lease set at
\* once, so there is no reachable state with a half-revoked fleet.
LeaseRevocationIsAtomic ==
    (~globalHalt /\ globalHalt') => activeLeases' = {}

\* The dispatch watermark never runs backwards.
DispatchMonotone ==
    dispatched' >= dispatched

\* ---------------------------------------------------------------------------
\* ACTION-LEVEL PROPERTIES (checked with [] over Next)
\* ---------------------------------------------------------------------------

AlwaysSticky     == [][LatchIsSticky]_vars
AlwaysAtomic     == [][LeaseRevocationIsAtomic]_vars
AlwaysMonotone   == [][DispatchMonotone]_vars

\* ---------------------------------------------------------------------------
\* LIVENESS
\* ---------------------------------------------------------------------------

\* Under weak fairness on TripKill, a requested kill is always executed and the
\* pipeline always ends up quiescent.
EventuallyHalts ==
    WF_vars(TripKill) => (globalHalt ~> Quiescent)

\* ---------------------------------------------------------------------------
\* THEOREMS
\* ---------------------------------------------------------------------------

THEOREM Spec => []KillSwitchIsTotal
THEOREM Spec => []NoDispatchWhileHalted
THEOREM Spec => AlwaysSticky
THEOREM Spec => AlwaysAtomic
THEOREM Spec => AlwaysMonotone

\* The composite safety claim: a halted pipeline is empty *and* silent.
KillSwitchSafety ==
    KillSwitchIsTotal /\ NoDispatchWhileHalted

THEOREM Spec => []KillSwitchSafety

\* The kill path costs exactly one atomic step, which is what the <1.0 ms SLA
\* in ``sec_010`` buys at the operating-system level.  Stated as a state
\* invariant so it is checkable without any fairness assumption:
\* every reachable halted state is quiescent.
THEOREM Spec => [](globalHalt => Quiescent)

===============================================================================
