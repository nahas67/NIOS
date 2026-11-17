"""Ring 0 crypto wallet sweep -- Binance SPOT <-> UMFUTURE_MAIN custody transfer.

Governing architecture sections
-------------------------------
* ``sec_009`` -- every silo owns the four canonical vaults and reconciles every
  60 seconds; a custody move that the book does not know about is exactly the
  drift that freezes a silo.
* ``sec_010`` -- the two-phase outbox is the only path money may take.
* ``sec_011`` -- Ring 0 is the only ring allowed to talk to a venue.
* ``sec_015`` -- zero cross-border wires.  The destination wallet is a
  sub-wallet of the *same* Binance account, so a sweep is an internal transfer
  and never a withdrawal, a bank wire or an FX conversion.
* ``sec_018`` Law 1 -- a venue acknowledgement is a hypothesis until the ledger
  commits it.
* ``sec_018`` Law 5 -- compound inside the silo; the swept amount lands in
  ``ReserveYield`` of the originating silo and nowhere else.

Why this module exists
----------------------
Binance keeps the spot balance and the USD-M futures margin balance in two
sub-wallets of one account.  Moving collateral between them is
``POST /sapi/v1/asset/transfer`` with ``type=1`` (spot -> futures) or ``type=2``
(futures -> spot).  It is not a withdrawal.  The ledger still books the move as
a ``PROFIT_RATCHET`` two-phase transfer from ``CASH`` into ``ReserveYield``,
because an unrecorded custody change is a reconciliation break.

Network discipline
------------------
This module never opens a socket.  ``allow_network`` must stay ``False``; a
caller that passes ``True`` gets :class:`SweepRejected` with ``NETWORK_DISABLED``
*before* any transport is touched.  A live gateway is injected as ``transport``
by the Ring 0 supervisor, so a unit test can only ever reach the deterministic
offline acknowledgement.

Settlement reporting
--------------------
The ledger credits the reserve the moment the two-phase transfer posts, but the
venue only makes the balance spendable once its own settlement window elapses.
:class:`LiquidityAvailabilitySchedule` is the *reporting* surface for that gap:
it answers "how much of this silo's swept liquidity is spendable right now".
It never mutates the ledger.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable

from core.schemas import VaultName, canonical_pairs_digest
from ring0_vault.ibor.ledger_outbox import (
    CrashAtomicOutbox,
    IBORLedger,
    LedgerError,
    OverdraftError,
    TransferCode,
)

__all__ = [
    "BINANCE_TRANSFER_ENDPOINT",
    "WALLET_SPOT",
    "WALLET_UMFUTURE",
    "TRANSFER_TYPE_SPOT_TO_FUTURE",
    "TRANSFER_TYPE_FUTURE_TO_SPOT",
    "CRYPTO_SILO",
    "CASH_ACCOUNT",
    "DIRECTION_SPOT_TO_FUTURE",
    "DIRECTION_FUTURE_TO_SPOT",
    "DEFAULT_SETTLEMENT_DELAY_NS",
    "LiquidityAvailabilitySchedule",
    "SweepReceipt",
    "SweepRejected",
    "CryptoWalletSweeper",
    "WalletSweepIntent",
]

#: ``sec_011``: the only Binance endpoint this module can ever describe.
BINANCE_TRANSFER_ENDPOINT = "/sapi/v1/asset/transfer"

#: Binance wallet identifiers (``fromSymbol``/``toSymbol`` on the transfer API).
WALLET_SPOT = "SPOT"
WALLET_UMFUTURE = "UMFUTURE_MAIN"

#: Binance ``type`` discriminator for the internal asset transfer.
TRANSFER_TYPE_SPOT_TO_FUTURE = 1
TRANSFER_TYPE_FUTURE_TO_SPOT = 2

#: ``sec_015``: a sweep may only ever run inside the crypto silo.
CRYPTO_SILO = "CRYPTO_BINANCE"

#: The external cash hub that funds a sweep (``sec_009``).
CASH_ACCOUNT = "CASH"

DIRECTION_SPOT_TO_FUTURE = "SPOT_TO_FUTURE"
DIRECTION_FUTURE_TO_SPOT = "FUTURE_TO_SPOT"

#: ``direction`` -> ``(from_wallet, to_wallet)``.
DIRECTION_WALLETS: dict[str, tuple[str, str]] = {
    DIRECTION_SPOT_TO_FUTURE: (WALLET_SPOT, WALLET_UMFUTURE),
    DIRECTION_FUTURE_TO_SPOT: (WALLET_UMFUTURE, WALLET_SPOT),
}

#: The venue needs one minute before a swept balance is spendable again.
DEFAULT_SETTLEMENT_DELAY_NS: int = 60 * 1_000_000_000

_WALLETS: tuple[str, str] = (WALLET_SPOT, WALLET_UMFUTURE)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SweepRejected(Exception):
    """A sweep was refused before or during execution.

    ``code`` is a stable machine-readable token so the cockpit never has to
    parse prose.  Known codes: ``KILL_LATCH_HOT``, ``SILO_NOT_CRYPTO``,
    ``SILO_FROZEN``, ``NETWORK_DISABLED``, ``SWEEP_AMOUNT_INVALID``,
    ``SWEEP_DIRECTION_INVALID``, ``WALLET_INVALID``, ``INTENT_INVALID``,
    ``DIGEST_MISMATCH``, ``INSUFFICIENT_CASH``, ``VENUE_REJECTED``.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class WalletSweepIntent:
    """One fully specified, hash-committed custody sweep (``sec_011``).

    The intent is the *only* thing Ring 2 / Ring 3 may hand to Ring 0.  It
    carries no credential, no socket and no order: just the two wallets, the
    amount and a SHA-256 commitment over every field, so a mutated intent is
    detectable before a single micro-unit moves (``sec_018`` Law 3).
    """

    sweep_id: str
    silo: str
    from_wallet: str
    to_wallet: str
    amount_micros: int
    created_at_ns: int
    sha256_digest: str = ""
    mission_id: str = ""
    tranche_id: str = ""

    def __post_init__(self) -> None:
        if str(self.silo) != CRYPTO_SILO:
            raise SweepRejected("SILO_NOT_CRYPTO", str(self.silo))
        if (
            str(self.from_wallet) not in _WALLETS
            or str(self.to_wallet) not in _WALLETS
            or str(self.from_wallet) == str(self.to_wallet)
        ):
            raise SweepRejected(
                "WALLET_INVALID", f"{self.from_wallet}->{self.to_wallet}"
            )
        if (
            isinstance(self.amount_micros, bool)
            or not isinstance(self.amount_micros, int)
            or int(self.amount_micros) <= 0
        ):
            raise SweepRejected("SWEEP_AMOUNT_INVALID", repr(self.amount_micros))
        if isinstance(self.created_at_ns, bool) or not isinstance(
            self.created_at_ns, int
        ):
            raise SweepRejected(
                "INTENT_INVALID", "created_at_ns must be an integer nanosecond stamp"
            )
        if int(self.created_at_ns) < 0:
            raise SweepRejected("INTENT_INVALID", "created_at_ns must be non-negative")
        computed = self.compute_digest()
        supplied = str(self.sha256_digest or "")
        if supplied and supplied != computed:
            raise SweepRejected(
                "DIGEST_MISMATCH", f"supplied {supplied} != recomputed {computed}"
            )
        object.__setattr__(self, "sha256_digest", supplied or computed)

    # -- commitments ------------------------------------------------------
    def canonical_pairs(self) -> list[tuple[str, Any]]:
        """Every committed field as a ``(key, value)`` pair.

        ``sha256_digest`` is deliberately absent: a digest cannot commit to
        itself.
        """
        return [
            ("sweep_id", str(self.sweep_id)),
            ("silo", str(self.silo)),
            ("from_wallet", str(self.from_wallet)),
            ("to_wallet", str(self.to_wallet)),
            ("amount_micros", int(self.amount_micros)),
            ("created_at_ns", int(self.created_at_ns)),
            ("mission_id", str(self.mission_id)),
            ("tranche_id", str(self.tranche_id)),
        ]

    def compute_digest(self) -> str:
        """Deterministic SHA-256 over :meth:`canonical_pairs`."""
        return canonical_pairs_digest(self.canonical_pairs())

    # -- venue mapping ----------------------------------------------------
    def transfer_type(self) -> int:
        """Binance ``type`` for this wallet pair."""
        if self.from_wallet == WALLET_SPOT and self.to_wallet == WALLET_UMFUTURE:
            return TRANSFER_TYPE_SPOT_TO_FUTURE
        if self.from_wallet == WALLET_UMFUTURE and self.to_wallet == WALLET_SPOT:
            return TRANSFER_TYPE_FUTURE_TO_SPOT
        raise SweepRejected("WALLET_INVALID", f"{self.from_wallet}->{self.to_wallet}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sweep_id": str(self.sweep_id),
            "silo": str(self.silo),
            "from_wallet": str(self.from_wallet),
            "to_wallet": str(self.to_wallet),
            "amount_micros": int(self.amount_micros),
            "created_at_ns": int(self.created_at_ns),
            "sha256_digest": str(self.sha256_digest),
            "mission_id": str(self.mission_id),
            "tranche_id": str(self.tranche_id),
            "transfer_type": int(self.transfer_type()),
        }


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SweepReceipt:
    """Immutable outcome of one :meth:`CryptoWalletSweeper.execute` call.

    ``state`` is ``PENDING`` only while the two-phase transfer is in flight; a
    returned receipt is always terminal (``COMMITTED`` or ``VOIDED``), so a
    caller can never mistake a hold for a settled balance.
    """

    accepted: bool
    state: str
    reason: str
    intent: WalletSweepIntent | None
    venue_ack: dict[str, Any]
    transfer_id: int
    liquidity_available_at_utc: int
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": bool(self.accepted),
            "state": str(self.state),
            "reason": str(self.reason),
            "intent": None if self.intent is None else self.intent.to_dict(),
            "venue_ack": dict(self.venue_ack),
            "transfer_id": int(self.transfer_id),
            "liquidity_available_at_utc": int(self.liquidity_available_at_utc),
            "signature": str(self.signature),
        }


# ---------------------------------------------------------------------------
# Liquidity availability reporting
# ---------------------------------------------------------------------------


class LiquidityAvailabilitySchedule:
    """When a venue acknowledgement makes swept liquidity spendable.

    ``available_at_utc`` is a UTC epoch **nanosecond** stamp (the ledger clock),
    so it compares directly against ``time.time_ns()``.  This object is a
    reporting surface only: the reserve balance is credited by the ledger
    transfer, never here.
    """

    __slots__ = ("_entries", "_seq")

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []
        self._seq = 0

    def schedule(
        self,
        silo: str,
        amount_micros: int,
        *,
        available_at_utc: int,
        source: str = "",
    ) -> dict[str, Any]:
        """Record that ``amount_micros`` becomes spendable at a UTC stamp."""
        amount = int(amount_micros)
        if amount <= 0:
            raise SweepRejected("SWEEP_AMOUNT_INVALID", repr(amount_micros))
        self._seq += 1
        entry = {
            "seq": int(self._seq),
            "silo": str(silo),
            "amount_micros": amount,
            "available_at_utc": int(available_at_utc),
            "source": str(source),
        }
        self._entries.append(entry)
        return dict(entry)

    def available_micros(self, silo: str, now_ns: int) -> int:
        """Liquidity already spendable at ``now_ns`` (0 before the ack lands)."""
        stamp = int(now_ns)
        return sum(
            int(entry["amount_micros"])
            for entry in self._entries
            if entry["silo"] == str(silo) and int(entry["available_at_utc"]) <= stamp
        )

    def pending_micros(self, silo: str, now_ns: int) -> int:
        """Liquidity still inside the venue settlement window at ``now_ns``."""
        stamp = int(now_ns)
        return sum(
            int(entry["amount_micros"])
            for entry in self._entries
            if entry["silo"] == str(silo) and int(entry["available_at_utc"]) > stamp
        )

    def entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(entry) for entry in self._entries)

    def reset(self) -> None:
        self._entries.clear()
        self._seq = 0


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


def _latch_is_halted(latch: Any) -> bool:
    """Read a kill latch that may expose a property or a callable.

    A latch that cannot be read is treated as **hot**: a kill switch whose
    state is unknown must never be assumed safe (``sec_006`` / ADR-009).
    """
    if latch is None:
        return False
    flag = getattr(latch, "is_halted", None)
    if callable(flag):
        try:
            flag = flag()
        except Exception:  # noqa: BLE001 - unreadable latch fails closed
            return True
    return bool(flag)


def _silo_frozen(firewall: Any, silo: str) -> bool:
    """True when the firewall (or its ``.guard``) has frozen ``silo``."""
    if firewall is None:
        return False
    guard = getattr(firewall, "guard", firewall)
    probe = getattr(guard, "is_frozen", None)
    if not callable(probe):
        return False
    try:
        return bool(probe(str(silo)))
    except Exception:  # noqa: BLE001 - an unreadable guard fails closed
        return True


class CryptoWalletSweeper:
    """Two-phase, offline-first Binance wallet sweeper (``sec_010``/``sec_011``).

    Every execution walks the same gate ladder and a policy denial can only
    ever return a ``VOIDED`` receipt -- it never raises and never leaves a hold:

    1. kill latch hot -> ``KILL_LATCH_HOT``;
    2. firewall has frozen the silo -> ``SILO_FROZEN``;
    3. ``allow_network`` is ``True`` -> raise ``NETWORK_DISABLED``;
    4. amount above ``CASH`` available -> ``INSUFFICIENT_CASH``;
    5. lock ``CASH -> ReserveYield`` as a ``PROFIT_RATCHET`` pending transfer;
    6. ask the transport (or the deterministic offline ack);
    7. success -> post the transfer, schedule liquidity, sign the intent;
    8. failure or transport exception -> void the transfer, ``VENUE_REJECTED``.
    """

    def __init__(
        self,
        ledger: IBORLedger,
        *,
        outbox: CrashAtomicOutbox | None = None,
        firewall: Any = None,
        signer: Any = None,
        kill_latch: Any = None,
        transport: Callable[[WalletSweepIntent], dict[str, Any]] | None = None,
        venue: str = "binance",
        allow_network: bool = False,
        settlement_delay_ns: int = DEFAULT_SETTLEMENT_DELAY_NS,
    ) -> None:
        self.ledger = ledger
        self.outbox = outbox
        self.firewall = firewall
        self.signer = signer
        self.kill_latch = kill_latch
        self.transport = transport
        self.venue = str(venue)
        self.allow_network = bool(allow_network)
        self.settlement_delay_ns = int(settlement_delay_ns)
        if self.settlement_delay_ns < 0:
            raise ValueError("settlement_delay_ns must be non-negative")
        self.liquidity = LiquidityAvailabilitySchedule()
        self._receipts: list[SweepReceipt] = []

    # -- planning ---------------------------------------------------------
    @staticmethod
    def _valid_amount(amount_micros: Any) -> int:
        if (
            isinstance(amount_micros, bool)
            or not isinstance(amount_micros, int)
            or int(amount_micros) <= 0
        ):
            raise SweepRejected("SWEEP_AMOUNT_INVALID", repr(amount_micros))
        return int(amount_micros)

    def plan(
        self,
        silo: str,
        amount_micros: int,
        *,
        direction: str = DIRECTION_SPOT_TO_FUTURE,
        mission_id: str = "",
        tranche_id: str = "",
        now_ns: int | None = None,
    ) -> WalletSweepIntent:
        """Build a hash-committed intent without touching the ledger.

        The ``sweep_id`` is deterministic in
        ``(silo, direction, amount, now_ns)`` so a replayed planning call for
        the same instant produces the same identifier instead of a duplicate
        sweep.
        """
        target = str(silo)
        if target != CRYPTO_SILO:
            raise SweepRejected("SILO_NOT_CRYPTO", target)
        amount = self._valid_amount(amount_micros)
        wallets = DIRECTION_WALLETS.get(str(direction))
        if wallets is None:
            raise SweepRejected("SWEEP_DIRECTION_INVALID", str(direction))
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        digest_seed = f"{target}:{direction}:{amount}:{stamp}".encode("ascii")
        sweep_id = hashlib.sha256(digest_seed).hexdigest()[:24]
        from_wallet, to_wallet = wallets
        return WalletSweepIntent(
            sweep_id=sweep_id,
            silo=target,
            from_wallet=from_wallet,
            to_wallet=to_wallet,
            amount_micros=amount,
            created_at_ns=stamp,
            mission_id=str(mission_id),
            tranche_id=str(tranche_id),
        )

    # -- execution --------------------------------------------------------
    def execute(
        self, intent: WalletSweepIntent, *, now_ns: int | None = None
    ) -> SweepReceipt:
        """Run the gate ladder for ``intent`` and return a terminal receipt."""
        if not isinstance(intent, WalletSweepIntent):
            raise TypeError("intent must be a WalletSweepIntent")
        stamp = int(now_ns if now_ns is not None else time.time_ns())

        # Gate 1 -- the global kill latch outranks every other consideration.
        if _latch_is_halted(self.kill_latch):
            return self._voided(intent, "KILL_LATCH_HOT")

        # Gate 2 -- a frozen silo may not move custody (``sec_009``).
        if _silo_frozen(self.firewall, intent.silo):
            return self._voided(intent, "SILO_FROZEN")

        # Gate 3 -- this module can never open a socket.
        if self.allow_network:
            raise SweepRejected(
                "NETWORK_DISABLED",
                "crypto_wallet_sweep never opens a socket; inject a transport",
            )

        # Gate 4 -- never lock more than the silo can actually cover.
        if int(intent.amount_micros) > int(self.ledger.available(intent.silo, CASH_ACCOUNT)):
            return self._voided(intent, "INSUFFICIENT_CASH")

        # Gate 5 -- two-phase lock, code PROFIT_RATCHET, inside the silo.
        try:
            pending = self.ledger.create_pending(
                intent.silo,
                debit=CASH_ACCOUNT,
                credit=str(VaultName.RESERVE_YIELD),
                amount_micros=int(intent.amount_micros),
                code=int(TransferCode.PROFIT_RATCHET),
                now_ns=stamp,
            )
        except (OverdraftError, LedgerError) as exc:
            # The ledger is authoritative: a refused lock is an insufficiency,
            # never a half-applied reservation.
            return self._voided(intent, "INSUFFICIENT_CASH", detail=str(exc))
        transfer_id = int(pending.transfer_id)

        # Gate 6 -- ask the venue (or the deterministic offline ack).
        try:
            if self.transport is not None:
                ack: dict[str, Any] = dict(self.transport(intent))
            else:
                ack = self._offline_ack(intent, transfer_id)
        except Exception as exc:  # noqa: BLE001 - a venue error voids, never raises
            ack = {
                "success": False,
                "status": "TRANSPORT_ERROR",
                "venue": self.venue,
                "error": f"{type(exc).__name__}: {exc}",
            }

        # Gate 7/8 -- commit on success, void on anything else.
        if not bool(ack.get("success", False)):
            self._safe_void(transfer_id, stamp)
            return self._voided(
                intent, "VENUE_REJECTED", venue_ack=ack, transfer_id=transfer_id
            )

        try:
            self.ledger.post_pending(transfer_id, now_ns=stamp)
        except (OverdraftError, LedgerError) as exc:
            self._safe_void(transfer_id, stamp)
            return self._voided(
                intent,
                "VENUE_REJECTED",
                venue_ack=ack,
                transfer_id=transfer_id,
                detail=f"ledger post failed: {exc}",
            )

        available_at = stamp + self.settlement_delay_ns
        self.liquidity.schedule(
            intent.silo,
            int(intent.amount_micros),
            available_at_utc=available_at,
            source=intent.sweep_id,
        )
        signature, sign_reason = self._sign_intent(intent)
        receipt = SweepReceipt(
            accepted=True,
            state="COMMITTED",
            reason=sign_reason,
            intent=intent,
            venue_ack=ack,
            transfer_id=transfer_id,
            liquidity_available_at_utc=available_at,
            signature=signature,
        )
        self._receipts.append(receipt)
        return receipt

    # -- receipts / reporting ---------------------------------------------
    def receipts(self) -> tuple[SweepReceipt, ...]:
        return tuple(self._receipts)

    def status(self) -> dict[str, Any]:
        """Cockpit-safe snapshot: no credential, no transport handle."""
        outbox_snapshot: dict[str, Any] | None = None
        if self.outbox is not None:
            snapshot = getattr(self.outbox, "snapshot", None)
            if callable(snapshot):
                outbox_snapshot = dict(snapshot())
        return {
            "venue": self.venue,
            "endpoint": BINANCE_TRANSFER_ENDPOINT,
            "allow_network": self.allow_network,
            "sweeps": len(self._receipts),
            "committed": sum(1 for r in self._receipts if r.state == "COMMITTED"),
            "voided": sum(1 for r in self._receipts if r.state == "VOIDED"),
            "pending": sum(1 for r in self._receipts if r.state == "PENDING"),
            "liquidity": [dict(entry) for entry in self.liquidity.entries()],
            "settlement_delay_ns": self.settlement_delay_ns,
            "outbox": outbox_snapshot,
        }

    # -- internals --------------------------------------------------------
    def _offline_ack(
        self, intent: WalletSweepIntent, transfer_id: int
    ) -> dict[str, Any]:
        """Deterministic ack used when no live transport is injected."""
        return {
            "success": True,
            "tranId": int(transfer_id),
            "status": "SUCCESS",
            "venue": self.venue,
            "offline": True,
            "transfer_type": int(intent.transfer_type()),
        }

    def _safe_void(self, transfer_id: int, stamp: int) -> None:
        """Void a pending transfer; an already-resolved hold is not an error."""
        try:
            self.ledger.void_pending(transfer_id, now_ns=stamp)
        except (OverdraftError, LedgerError):
            return

    def _sign_intent(self, intent: WalletSweepIntent) -> tuple[str, str]:
        """Sign the intent payload; return ``(signature, reason)``.

        The ledger already moved, so a missing signer is reported rather than
        raised: ``SIGNER_UNAVAILABLE`` on an otherwise ``COMMITTED`` receipt
        tells the cockpit the custody change is real but unsigned.
        """
        if self.signer is None:
            return "", ""
        try:
            _digest, signature, _token = self.signer.sign_payload(
                dict(intent.canonical_pairs())
            )
        except Exception:  # noqa: BLE001 - signing must not undo a committed move
            return "", "SIGNER_UNAVAILABLE"
        return str(signature), ""

    def _voided(
        self,
        intent: WalletSweepIntent,
        reason: str,
        *,
        venue_ack: dict[str, Any] | None = None,
        transfer_id: int = 0,
        detail: str = "",
    ) -> SweepReceipt:
        ack = dict(venue_ack or {})
        if detail:
            ack.setdefault("detail", str(detail))
        receipt = SweepReceipt(
            accepted=False,
            state="VOIDED",
            reason=str(reason),
            intent=intent,
            venue_ack=ack,
            transfer_id=int(transfer_id),
            liquidity_available_at_utc=0,
            signature="",
        )
        self._receipts.append(receipt)
        return receipt
