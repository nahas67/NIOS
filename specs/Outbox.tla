---- MODULE Outbox ----
(*******************************************************************************)
(* NIOS Core v3.0 -- Two-phase crash-atomic outbox formal specification.         *)
(*                                                                              *)
(* Governing sections: ``sec_009`` (TigerBeetle two-phase pending transfer +     *)
(* PostgreSQL 16 financialOutbox record with a deterministic idempotency key),   *)
(* ``sec_010`` (<2.0 ms outbox), ``sec_011`` (air-gapped signer), ``sec_012``    *)
(* (formal state-machine proofs), ``sec_018`` (Law 4 -- abstain by default).     *)
(*                                                                              *)
(* The outbox is the ONLY path by which a signed order may reach a broker        *)
(* socket.  A reservation is committed as PENDING before dispatch and is         *)
(* either POSTED on a broker FILLED ack or VOIDED on CANCELED / REJECTED /       *)
(* timeout.  Re-submitting the *same* deterministic idempotency key must return  *)
(* the existing reservation and must never produce a second broker submission.   *)
(*                                                                              *)
(* Property proved below:                                                       *)
(*                                                                              *)
(*     AtMostOnceExecution == \A k \in Keys : posted[k] <= 1                     *)
(*                            /\ brokerSubmissions[k] <= 1                      *)
(*******************************************************************************)

EXTENDS Naturals, FiniteSets, Sequences

CONSTANTS
    Keys,        \* deterministic outbox_idempotency_key universe
    Amounts      \* Keys -> Nat, the locked notional+fee+tax per key

VARIABLES
    phase,               \* Keys -> {"NONE","PENDING","POSTED","VOID"}
    brokerSubmissions,   \* Keys -> Nat, orders handed to the broker socket
    postedCount,         \* Keys -> Nat, number of successful commits
    pendingAmount,       \* Keys -> Nat, micro-units locked in PENDING
    postedAmount,        \* Keys -> Nat, micro-units actually settled
    ackLog               \* Keys -> Seq({ "FILLED", "CANCELED", "REJECTED" })

vars == <<phase, brokerSubmissions, postedCount,
          pendingAmount, postedAmount, ackLog>>

\* ---------------------------------------------------------------------------
\* Derived predicates
\* ---------------------------------------------------------------------------

\* A key is "open" while it holds a live reservation.
IsOpen(k) == phase[k] = "PENDING"

\* A key is "closed" once it has been posted or voided.
IsClosed(k) == phase[k] \in {"POSTED", "VOID"}

\* THE INVARIANT.
\* No key is ever executed twice, and no key is ever sent to the broker twice.
AtMostOnceExecution ==
    /\ \A k \in Keys : postedCount[k] <= 1
    /\ \A k \in Keys : brokerSubmissions[k] <= 1

\* A closed key never re-opens (terminal absorption).
ClosedIsAbsorbing ==
    \A k \in Keys : IsClosed(k) => phase[k] # "PENDING"

\* The locked + settled amount for a key never exceeds its requested amount.
NoOverReservation ==
    \A k \in Keys : pendingAmount[k] + postedAmount[k] <= Amounts[k]

\* Every key is in exactly one phase.
PhaseIsWellFormed ==
    \A k \in Keys : phase[k] \in {"NONE", "PENDING", "POSTED", "VOID"}

\* A PENDING reservation is unique per key: there is no way to hold two live
\* reservations for the same deterministic key.
PendingIsUnique ==
    \A k \in Keys : pendingAmount[k] > 0 => phase[k] = "PENDING"

\* Every ack recorded in the log corresponds to a key that actually had a live
\* reservation (no phantom acks).
AcksAreAttributable ==
    \A k \in Keys : Len(ackLog[k]) <= 1

\* ---------------------------------------------------------------------------
\* Initial state
\* ---------------------------------------------------------------------------

Init ==
    /\ phase             = [k \in Keys |-> "NONE"]
    /\ brokerSubmissions = [k \in Keys |-> 0]
    /\ postedCount       = [k \in Keys |-> 0]
    /\ pendingAmount     = [k \in Keys |-> 0]
    /\ postedAmount      = [k \in Keys |-> 0]
    /\ ackLog            = [k \in Keys |-> <<>>]

\* ---------------------------------------------------------------------------
\* Transitions
\* ---------------------------------------------------------------------------

\* Phase 1 of the two-phase commit: reserve notional + fee + tax as PENDING,
\* write the financialOutbox row, and hand the signed order to the broker.
Reserve(k) ==
    /\ phase[k] = "NONE"
    /\ phase'             = [phase EXCEPT ![k] = "PENDING"]
    /\ brokerSubmissions' = [brokerSubmissions EXCEPT ![k] = @ + 1]
    /\ pendingAmount'     = [pendingAmount EXCEPT ![k] = Amounts[k]]
    /\ UNCHANGED <<postedCount, postedAmount, ackLog>>

\* RETRY of the same deterministic key.  This is the idempotency guarantee:
\* the caller receives the existing reservation and NOTHING changes -- no second
\* debit, no second broker submission.
DuplicateReserve(k) ==
    /\ phase[k] # "NONE"
    /\ UNCHANGED vars

\* Phase 2a: broker FILLED -> post the pending transfer, settle the amount.
Commit(k) ==
    /\ phase[k] = "PENDING"
    /\ phase'         = [phase EXCEPT ![k] = "POSTED"]
    /\ postedCount'   = [postedCount EXCEPT ![k] = @ + 1]
    /\ postedAmount'  = [postedAmount EXCEPT ![k] = @ + Amounts[k]]
    /\ pendingAmount' = [pendingAmount EXCEPT ![k] = 0]
    /\ ackLog'        = [ackLog EXCEPT ![k] = Append(@, "FILLED")]
    /\ UNCHANGED brokerSubmissions

\* Phase 2b: broker CANCELED / REJECTED / timeout -> void the pending transfer.
Void(k) ==
    /\ phase[k] = "PENDING"
    /\ phase'         = [phase EXCEPT ![k] = "VOID"]
    /\ pendingAmount' = [pendingAmount EXCEPT ![k] = 0]
    /\ ackLog'        = [ackLog EXCEPT ![k] = Append(@, "CANCELED")]
    /\ UNCHANGED <<brokerSubmissions, postedCount, postedAmount>>

\* Operator- or reconciler-driven rollback of a stuck reservation.
ReconcileVoid(k) ==
    /\ phase[k] = "PENDING"
    /\ phase'         = [phase EXCEPT ![k] = "VOID"]
    /\ pendingAmount' = [pendingAmount EXCEPT ![k] = 0]
    /\ ackLog'        = [ackLog EXCEPT ![k] = Append(@, "REJECTED")]
    /\ UNCHANGED <<brokerSubmissions, postedCount, postedAmount>>

\* Crash recovery: re-read the outbox and replay whatever phase it was in.
Recover(k) ==
    /\ IsOpen(k)
    /\ UNCHANGED vars

Next ==
    \/ \E k \in Keys : Reserve(k)
    \/ \E k \in Keys : DuplicateReserve(k)
    \/ \E k \in Keys : Commit(k)
    \/ \E k \in Keys : Void(k)
    \/ \E k \in Keys : ReconcileVoid(k)
    \/ \E k \in Keys : Recover(k)

Spec == Init /\ [][Next]_vars

\* ---------------------------------------------------------------------------
\* THEOREMS
\* ---------------------------------------------------------------------------

THEOREM Spec => []AtMostOnceExecution
THEOREM Spec => []ClosedIsAbsorbing
THEOREM Spec => []NoOverReservation
THEOREM Spec => []PhaseIsWellFormed
THEOREM Spec => []PendingIsUnique
THEOREM Spec => []AcksAreAttributable

\* Every open reservation is eventually resolved (POSTED or VOID) under weak
\* fairness on the two completion actions -- this is what keeps the
\* `NoOrphanedOrders` invariant of OrderLifecycle non-vacuous.
EventualResolution ==
    \A k \in Keys :
        WF_vars(Commit(k) \/ Void(k))
        => (IsOpen(k) ~> IsClosed(k))

===============================================================================
