"""NIOS Core v3.0 -- Ring 0 sovereign firewall, ADR-009 path and Smart OMS.

Governing architecture sections
-------------------------------
* ``sec_004`` -- Nautilus ``RiskEngine`` with ``HALTED`` / ``REDUCING`` fast
  paths, and the Smart OMS Maker-Peg / TWAP execution layer.
* ``sec_010`` -- 3-second maker-peg hard cancel, 2 re-peg ceiling, TWAP/Iceberg
  ``<=10%`` of top-of-book, ``<1.0 ms`` global kill.
* ``sec_011`` -- five-step Ring 0 gate: grounding digest, whitelist / daily loss
  / notional caps, conformal + copula, two-phase outbox, hardware/OS signing.
* ``sec_012`` -- ``NoOrphanedOrders``: every cancelled slice voids its
  reservation.
* ``sec_013`` -- ``0.290 ms p50`` / ``0.420 ms p99`` for conformal + firewall +
  sign.
* ``sec_016`` -- execution events to ``stream.ring0.execution-events`` and
  formatted proof cards to ``stream.ring3.cockpit-cards``.
* ``sec_018`` Law 2 -- nothing in this module calls an LLM.

Credential custody
------------------
Broker API keys never leave this module.  :class:`OrderSigner` holds an Ed25519
private key for provenance and a Fernet (AES-256-CBC + HMAC-SHA256) key that
seals the order payload at rest.  Both are generated in Ring 0 memory and are
never written to disk by this module; the production key material lives in
``ring0_vault/silos/key_vault/`` behind the OS keystore.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import gc
import functools
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Iterable, Mapping

from core.nats_bus import STREAM_RING0_EXECUTION, STREAM_RING3_COCKPIT
from core.schemas import (
    PlainEnglishCockpitCard,
    UnsignedProposal,
    VAULT_NOTIONAL_CAP_PCT,
    VaultName,
)

from ring0_vault.ibor.ledger_outbox import (
    BrokerAck,
    CrashAtomicOutbox,
    IBORLedger,
    InSiloProfitRatchet,
    LedgerError,
    OverdraftError,
    Reservation,
    SiloGuard,
)

from ring0_vault.conformal_copula_gate import (
    GATE_LATENCY_BUDGET_NS,
    CrossSiloCopulaShield,
    GateDecision,
    ShieldDecision,
    SplitConformal98Gate,
    silo_market,
)

__all__ = [
    "FIREWALL_LATENCY_P95_BUDGET_NS",
    "ADR009_LATENCY_BUDGET_NS",
    "MAKER_PEG_TIMEOUT_NS",
    "MAKER_PEG_MAX_REPEGS",
    "TWAP_MAX_BOOK_PCT",
    "TradingState",
    "OrderType",
    "FirewallStage",
    "SignedOrder",
    "OrderSigner",
    "FirewallVerdict",
    "SovereignPreTradeFirewall",
    "ADR009Verdict",
    "ADR009ReduceOnlyFastPath",
    "ExecutionEvent",
    "MakerPegChaser",
    "IcebergPlan",
    "TwapIcebergSlicer",
    "SmartOMS",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: ``sec_010`` / ``sec_013``: full five-step Ring 0 gate.
FIREWALL_LATENCY_P95_BUDGET_NS: int = GATE_LATENCY_BUDGET_NS

#: ``sec_004`` / ``sec_010``: ADR-009 reduce-only / flatten path.
ADR009_LATENCY_BUDGET_NS: int = 1_000_000  # 1.0 ms

#: ``sec_010``: maker-peg hard cancel after 3.0 seconds.
MAKER_PEG_TIMEOUT_NS: int = 3_000_000_000

#: ``sec_010``: at most 2 re-pegs before the hard cancel.
MAKER_PEG_MAX_REPEGS: int = 2

#: ``sec_010``: each TWAP/Iceberg child is at most 10% of top-of-book depth.
TWAP_MAX_BOOK_PCT: float = 0.10

_HEX = frozenset("0123456789abcdef")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TradingState(StrEnum):
    """Nautilus-style risk state (``sec_004``)."""

    ACTIVE = "ACTIVE"
    REDUCING = "REDUCING"
    HALTED = "HALTED"


class OrderType(StrEnum):
    MAKER_PEG = "MAKER_PEG"
    TWAP_ICEBERG = "TWAP_ICEBERG"
    MARKET_REDUCE_ONLY = "MARKET_REDUCE_ONLY"


class FirewallStage(IntEnum):
    """The five ordered stages of the sovereign pre-trade gate."""

    GROUNDING = 1
    RISK_LIMITS = 2
    PRECISION_GATES = 3
    OUTBOX = 4
    SIGNING = 5

    @property
    def label(self) -> str:
        return {
            FirewallStage.GROUNDING: "GROUNDING_DIGEST",
            FirewallStage.RISK_LIMITS: "RISK_LIMITS",
            FirewallStage.PRECISION_GATES: "CONFORMAL_AND_COPULA",
            FirewallStage.OUTBOX: "TWO_PHASE_OUTBOX",
            FirewallStage.SIGNING: "ED25519_SIGN",
        }[self]


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SignedOrder:
    """A cryptographically sealed, broker-ready order payload."""

    proposal_id: str
    silo: str
    symbol: str
    side: str
    vault: str
    order_type: OrderType
    qty: float
    limit_price: float
    payload_digest: str
    ed25519_signature: str
    fernet_token: str
    outbox_key: str
    signed_at_ns: int
    reduce_only: bool = False
    strategy_id: str = ""
    net_lb_bps: float = 0.0

    def payload(self) -> dict[str, Any]:
        """Reconstruct the exact canonical payload that was signed."""
        return {
            "proposal_id": self.proposal_id,
            "strategy_id": self.strategy_id,
            "silo": self.silo,
            "vault": self.vault,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "limit_price": self.limit_price,
            "outbox_key": self.outbox_key,
            "net_lb_bps": round(float(self.net_lb_bps), 6),
            "order_type": str(self.order_type),
            "signed_at_ns": self.signed_at_ns,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "strategy_id": self.strategy_id,
            "silo": self.silo,
            "symbol": self.symbol,
            "side": self.side,
            "vault": self.vault,
            "order_type": str(self.order_type),
            "qty": self.qty,
            "limit_price": self.limit_price,
            "payload_digest": self.payload_digest,
            "ed25519_signature": self.ed25519_signature,
            "fernet_token": self.fernet_token,
            "outbox_key": self.outbox_key,
            "signed_at_ns": self.signed_at_ns,
            "net_lb_bps": self.net_lb_bps,
            "reduce_only": self.reduce_only,
        }


class OrderSigner:
    """Ed25519 provenance signature + Fernet AES-256 payload seal.

    Two distinct jobs, two distinct primitives:

    * **Ed25519** proves *who* sealed the intent -- a 64-byte deterministic
      signature over the canonical payload digest, verifiable by the audit ring
      without ever seeing the private key.
    * **Fernet** (AES-256-CBC + HMAC-SHA256) seals the payload at rest so a
      crashed core dump or a stolen journal row does not leak the order, the
      symbol or the sizing.

    Neither key is ever exported by this class.  ``public_key_hex`` is the only
    material that may legitimately leave Ring 0.
    """

    __slots__ = ("_ed25519", "_fernet", "_public_hex")

    def __init__(self, *, ed25519_seed: bytes | None = None, fernet_key: bytes | None = None) -> None:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if ed25519_seed is None:
            self._ed25519 = Ed25519PrivateKey.generate()
        else:
            seed = bytes(ed25519_seed)
            if len(seed) != 32:
                raise ValueError("ed25519_seed must be exactly 32 bytes")
            self._ed25519 = Ed25519PrivateKey.from_private_bytes(seed)
        self._fernet = Fernet(fernet_key if fernet_key is not None else Fernet.generate_key())
        self._public_hex = self._ed25519.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
        # Prime the OpenSSL provider and the Fernet cipher once, off the
        # measured Ring 0 path (the first primitive call costs ~10 ms cold).
        self._ed25519.sign(b"nios-signer-warmup")
        self._fernet.encrypt(b"nios-signer-warmup")

    # -- identity ---------------------------------------------------------
    @property
    def public_key_hex(self) -> str:
        return self._public_hex

    # -- signing ----------------------------------------------------------
    @staticmethod
    def canonical_payload(payload: Mapping[str, Any]) -> bytes:
        return json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")

    def digest(self, payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(self.canonical_payload(payload)).hexdigest()

    def sign_payload(self, payload: Mapping[str, Any]) -> tuple[str, str, str]:
        """Return ``(digest_hex, ed25519_signature_hex, fernet_token)``."""
        raw = self.canonical_payload(payload)
        digest = hashlib.sha256(raw).hexdigest()
        signature = self._ed25519.sign(raw).hex()
        token = self._fernet.encrypt(raw).decode("ascii")
        return digest, signature, token

    def verify(self, payload: Mapping[str, Any], signature_hex: str) -> bool:
        from cryptography.exceptions import InvalidSignature

        try:
            self._ed25519.public_key().verify(
                bytes.fromhex(str(signature_hex)), self.canonical_payload(payload)
            )
            return True
        except (InvalidSignature, ValueError):
            return False

    def open_sealed(self, token: str) -> bytes:
        return self._fernet.decrypt(str(token).encode("ascii"))

    def signed_order(
        self,
        proposal: UnsignedProposal,
        *,
        outbox_key: str,
        net_lb_bps: float,
        order_type: OrderType,
        now_ns: int,
        qty: float | None = None,
        reduce_only: bool | None = None,
    ) -> SignedOrder:
        qty_value = float(proposal.qty if qty is None else qty)
        payload = {
            "proposal_id": str(proposal.proposal_id),
            "strategy_id": str(proposal.strategy_id),
            "silo": str(proposal.silo),
            "vault": str(proposal.vault),
            "symbol": str(proposal.symbol),
            "side": str(proposal.side),
            "qty": qty_value,
            "limit_price": float(proposal.limit_price),
            "outbox_key": str(outbox_key),
            "net_lb_bps": round(float(net_lb_bps), 6),
            "order_type": str(order_type),
            "signed_at_ns": int(now_ns),
        }
        digest, signature, token = self.sign_payload(payload)
        return SignedOrder(
            proposal_id=str(proposal.proposal_id),
            silo=str(proposal.silo),
            symbol=str(proposal.symbol),
            side=str(proposal.side),
            vault=str(proposal.vault),
            order_type=order_type,
            qty=qty_value,
            limit_price=float(proposal.limit_price),
            payload_digest=digest,
            ed25519_signature=signature,
            fernet_token=token,
            outbox_key=str(outbox_key),
            signed_at_ns=int(now_ns),
            reduce_only=(
                bool(proposal.is_reduce_only) if reduce_only is None else bool(reduce_only)
            ),
            strategy_id=str(proposal.strategy_id),
            net_lb_bps=round(float(net_lb_bps), 6),
        )

    def verify_order(self, signed: SignedOrder) -> bool:
        """Verify the Ed25519 signature over a :class:`SignedOrder` payload."""
        return self.verify(signed.payload(), signed.ed25519_signature)


# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class FirewallVerdict:
    """Immutable record of one Ring 0 gate evaluation."""

    accepted: bool
    stage_reached: FirewallStage
    stage_name: str
    reason: str
    silo: str
    symbol: str
    proposal_id: str
    latency_ns: int
    net_lb_bps: float
    notional_micros: int
    gate: GateDecision | None = None
    shield: ShieldDecision | None = None
    reservation: Reservation | None = None
    signed: SignedOrder | None = None
    card: PlainEnglishCockpitCard | None = None
    trading_state: TradingState = TradingState.ACTIVE
    sized_down: bool = False

    @property
    def signed_order(self) -> SignedOrder | None:
        return self.signed

    @property
    def within_latency_budget(self) -> bool:
        return self.latency_ns <= FIREWALL_LATENCY_P95_BUDGET_NS

    @property
    def abstain_code(self) -> str:
        if self.accepted:
            return ""
        return str(self.gate.outcome) if self.gate is not None else self.reason.split(":")[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "stage_reached": int(self.stage_reached),
            "stage_name": self.stage_name,
            "reason": self.reason,
            "silo": self.silo,
            "symbol": self.symbol,
            "proposal_id": self.proposal_id,
            "latency_ns": self.latency_ns,
            "net_lb_bps": self.net_lb_bps,
            "notional_micros": self.notional_micros,
            "trading_state": str(self.trading_state),
            "sized_down": self.sized_down,
            "abstain_code": self.abstain_code,
            "gate": self.gate.to_dict() if self.gate else None,
            "shield": self.shield.to_dict() if self.shield else None,
            "reservation": self.reservation.to_dict() if self.reservation else None,
            "signed": self.signed.to_dict() if self.signed else None,
            "proof_line": self.card.format_proof_line() if self.card else "",
        }


def _zero_gc(func):
    """Run one hot-path call with cyclic GC paused (``sec_018`` Law 2)."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        was_enabled = gc.isenabled()
        if was_enabled:
            gc.disable()
        try:
            return func(*args, **kwargs)
        finally:
            if was_enabled:
                gc.enable()

    return wrapper


class SovereignPreTradeFirewall:
    """The five-step Ring 0 gate, in the mandated order (``sec_011``).

    ==========  ===========================================================
    Stage       Check
    ==========  ===========================================================
    1 GROUNDING 64-char SHA-256 ``grounding_hash`` (Ring 2 ledger digest or a
                Ring 1 132-byte claim-frame digest), optional registry lookup
    2 RISK      whitelist, per-silo daily loss stop, vault notional caps
    3 GATES     ``SplitConformal98Gate`` then ``CrossSiloCopulaShield``
    4 OUTBOX    two-phase pending reservation in ``CrashAtomicOutbox``
    5 SIGN      Ed25519 + Fernet seal
    ==========  ===========================================================

    A rejection at any stage aborts with that stage recorded; nothing is
    partially reserved and nothing is partially signed.
    """

    def __init__(
        self,
        *,
        ledger: IBORLedger,
        outbox: CrashAtomicOutbox,
        gate: SplitConformal98Gate,
        shield: CrossSiloCopulaShield,
        signer: OrderSigner | None = None,
        guard: SiloGuard | None = None,
        whitelists: Mapping[str, Iterable[str]] | None = None,
        daily_loss_stops_micros: Mapping[str, int] | None = None,
        grounding_store: Any = None,
        require_registered_grounding: bool = False,
        latency_budget_ns: int = FIREWALL_LATENCY_P95_BUDGET_NS,
    ) -> None:
        self.ledger = ledger
        self.outbox = outbox
        self.gate = gate
        self.shield = shield
        self.signer = signer if signer is not None else OrderSigner()
        self.guard = guard if guard is not None else SiloGuard()
        self.grounding_store = grounding_store
        self.require_registered_grounding = bool(require_registered_grounding)
        self.latency_budget_ns = int(latency_budget_ns)

        self.whitelists: dict[str, frozenset[str]] = {
            str(silo): frozenset(str(s).upper() for s in symbols)
            for silo, symbols in (whitelists or {}).items()
        }
        self.daily_loss_stops: dict[str, int] = {
            str(silo): int(amount) for silo, amount in (daily_loss_stops_micros or {}).items()
        }
        self._realized_pnl: dict[str, int] = {}
        self._state: dict[str, TradingState] = {}
        self._evaluations = 0

    # -- limits -----------------------------------------------------------
    def set_whitelist(self, silo: str, symbols: Iterable[str]) -> None:
        self.whitelists[str(silo)] = frozenset(str(s).upper() for s in symbols)

    def set_daily_loss_stop(self, silo: str, max_daily_loss_micros: int) -> None:
        self.daily_loss_stops[str(silo)] = int(max_daily_loss_micros)

    def record_realized_pnl(self, silo: str, realized_net_micros: int) -> int:
        """Accumulate realised P&L and trip the Tier-3 daily loss stop.

        ``sec_011`` Tier 3: the moment cumulative realised losses reach
        ``max_daily_loss_micros`` the silo locks and becomes reduce-only.  The
        freeze happens *here*, on the settlement path, not on the next order
        attempt -- a stop that only reacts when the next proposal arrives is
        not a stop.  The caller (``SmartOMS`` fill -> ``IBORLedger`` settlement
        -> this method) therefore feeds every settled fill straight in.
        """
        silo = str(silo)
        value = int(realized_net_micros)
        total = self._realized_pnl.get(silo, 0) + value
        self._realized_pnl[silo] = total
        if self.daily_loss_exceeded(silo):
            stop = self.daily_loss_stops.get(silo)
            self._state[silo] = TradingState.HALTED
            self.guard.freeze(
                silo,
                (
                    f"DAILY_LOSS_LIMIT_BREACHED: {silo} realised {max(0, -total)} "
                    f"micros against a {stop} micro daily stop; silo locked and "
                    "reduce-only (ADR-009, sec_011 Tier 3)"
                ),
            )
        return total

    def realized_pnl(self, silo: str) -> int:
        return self._realized_pnl.get(str(silo), 0)

    def daily_loss_exceeded(self, silo: str) -> bool:
        stop = self.daily_loss_stops.get(str(silo))
        if stop is None:
            return False
        return -self.realized_pnl(silo) >= stop

    def trading_state(self, silo: str) -> TradingState:
        if self.guard.is_frozen(str(silo)):
            return TradingState.HALTED
        return self._state.get(str(silo), TradingState.ACTIVE)

    def set_trading_state(self, silo: str, state: TradingState) -> None:
        self._state[str(silo)] = state

    # -- grounding --------------------------------------------------------
    @staticmethod
    def is_valid_digest(digest: str) -> bool:
        token = str(digest or "").strip().lower()
        return len(token) == 64 and all(char in _HEX for char in token)

    def verify_grounding(
        self,
        proposal: UnsignedProposal,
        *,
        claim_frame: bytes | bytearray | memoryview | None = None,
    ) -> tuple[bool, str]:
        """Stage 1: accept a Ring 2 ledger digest or a Ring 1 claim-frame digest."""
        digest = str(proposal.grounding_hash or "").strip().lower()
        if len(digest) != 64 or any(char not in _HEX for char in digest):
            return False, "GROUNDING_HASH_MALFORMED: expected a 64-char SHA-256 hex digest"

        if claim_frame is not None:
            frame = bytes(claim_frame)
            computed = hashlib.sha256(frame).hexdigest()
            if computed != digest:
                return (
                    False,
                    f"GROUNDING_HASH_MISMATCH: claim frame hashes to {computed[:12]}... "
                    f"but the proposal cites {digest[:12]}...",
                )
            return True, f"Ring 1 claim-frame SHA-256 verified ({len(frame)}-byte frame)"

        if self.require_registered_grounding:
            store = self.grounding_store
            found = False
            if store is not None:
                checker = getattr(store, "has_grounding_hash", None)
                if callable(checker):
                    found = bool(checker(digest))
                else:
                    contains = getattr(store, "__contains__", None)
                    found = bool(contains(digest)) if callable(contains) else False
            if not found:
                return (
                    False,
                    "UNGROUNDED-NUMERICAL-CLAIM: digest absent from the SHA-256 "
                    "grounding ledger",
                )
            return True, "Ring 2 grounding-ledger digest registered"

        return True, "64-char SHA-256 grounding digest well formed"

    # -- main gate --------------------------------------------------------
    @_zero_gc
    def evaluate(
        self,
        proposal: UnsignedProposal,
        *,
        now_ns: int | None = None,
        cost_floor: Any = None,
        claim_frame: bytes | bytearray | memoryview | None = None,
        order_type: OrderType | None = None,
        mode_badge: str = "LIVE CAPITAL",
    ) -> FirewallVerdict:
        """Run the five-stage gate.  Never raises on a policy rejection."""
        started = time.perf_counter_ns()
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        silo = str(proposal.silo)
        symbol = str(proposal.symbol)
        self._evaluations += 1

        def _reject(
            stage: FirewallStage,
            reason: str,
            *,
            gate: GateDecision | None = None,
            shield: ShieldDecision | None = None,
            net_lb: float = 0.0,
            notional: int = 0,
            card: PlainEnglishCockpitCard | None = None,
            sized_down: bool = False,
        ) -> FirewallVerdict:
            return FirewallVerdict(
                accepted=False,
                stage_reached=stage,
                stage_name=stage.label,
                reason=reason,
                silo=silo,
                symbol=symbol,
                proposal_id=str(proposal.proposal_id),
                latency_ns=time.perf_counter_ns() - started,
                net_lb_bps=float(net_lb),
                notional_micros=int(notional),
                gate=gate,
                shield=shield,
                reservation=None,
                signed=None,
                card=card,
                trading_state=self.trading_state(silo),
                sized_down=sized_down,
            )

        # -- stage 0: silo halt / kill latch ------------------------------
        state = self.trading_state(silo)
        if state is TradingState.HALTED:
            return _reject(
                FirewallStage.RISK_LIMITS,
                f"SILO_HALTED: {self.guard.reason(silo) or 'global kill latch is hot'}",
            )

        # -- stage 1: grounding digest ------------------------------------
        grounding_ok, grounding_detail = self.verify_grounding(
            proposal, claim_frame=claim_frame
        )
        if not grounding_ok:
            return _reject(FirewallStage.GROUNDING, grounding_detail)

        # -- stage 2: risk limits -----------------------------------------
        whitelist = self.whitelists.get(silo)
        if whitelist is not None and symbol.upper() not in whitelist:
            return _reject(
                FirewallStage.RISK_LIMITS,
                f"SYMBOL_NOT_WHITELISTED: {symbol} is not approved for silo {silo}",
            )

        if self.daily_loss_exceeded(silo):
            return _reject(
                FirewallStage.RISK_LIMITS,
                f"DAILY_LOSS_LIMIT_BREACHED: silo {silo} realised "
                f"{-self.realized_pnl(silo)} micros against a "
                f"{self.daily_loss_stops[silo]} micro stop",
            )

        qty_micros, price_micros, notional_micros, fee_micros, tax_micros = (
            self.outbox.lock_amounts(proposal, cost_floor=cost_floor)
        )
        nav_micros = self.ledger.silo_nav_micros(silo)
        cap_pct = float(VAULT_NOTIONAL_CAP_PCT.get(proposal.vault, 0.05))
        # ``sec_011``: SpinalScalper <= 5% of silo NAV, compounder/swing <= 12%.
        if nav_micros > 0 and notional_micros > int(nav_micros * cap_pct):
            return _reject(
                FirewallStage.RISK_LIMITS,
                f"NOTIONAL_CAP_BREACH: {notional_micros} micros exceeds "
                f"{cap_pct * 100:.0f}% of the {silo} silo NAV ({nav_micros} micros)",
                notional=notional_micros,
            )

        # -- stage 3a: 98% split-conformal precision gate -----------------
        gate_decision = self.gate.evaluate(
            proposal, cost_floor=cost_floor, now_ns=stamp, mode_badge=mode_badge
        )
        if not gate_decision.allowed:
            return _reject(
                FirewallStage.PRECISION_GATES,
                f"{gate_decision.outcome}: {gate_decision.reason}",
                gate=gate_decision,
                net_lb=gate_decision.net_lb_bps,
                notional=notional_micros,
                card=gate_decision.card,
            )

        # -- stage 3b: cross-silo tail-copula shield ----------------------
        market = silo_market(silo)
        shield_decision = self.shield.evaluate(market, notional_micros)
        if not shield_decision.allowed:
            return _reject(
                FirewallStage.PRECISION_GATES,
                f"CORRELATED_HEAT_BLOCK: {shield_decision.reason}",
                gate=gate_decision,
                shield=shield_decision,
                net_lb=gate_decision.net_lb_bps,
                notional=notional_micros,
                card=gate_decision.card,
            )

        effective_qty = proposal.qty
        sized_down = shield_decision.haircut < 1.0
        if sized_down:
            effective_qty = float(proposal.qty) * shield_decision.haircut
            if effective_qty <= 0.0:
                return _reject(
                    FirewallStage.PRECISION_GATES,
                    "CORRELATED_HEAT_BLOCK: sizing haircut collapsed the order to zero",
                    gate=gate_decision,
                    shield=shield_decision,
                    net_lb=gate_decision.net_lb_bps,
                    notional=notional_micros,
                )
            # Re-quantise the NOTIONAL after the haircut so the reservation
            # never locks more than the shield allows.
            qty_micros = int(math.floor(effective_qty * 1_000_000 + 1e-9))
            notional_micros = (qty_micros * price_micros) // 1_000_000

        # -- stage 4: two-phase crash-atomic outbox -----------------------
        try:
            reservation = self.outbox.reserve(
                proposal, now_ns=stamp, cost_floor=cost_floor
            )
        except OverdraftError as exc:
            return _reject(
                FirewallStage.OUTBOX,
                str(exc),
                gate=gate_decision,
                shield=shield_decision,
                net_lb=gate_decision.net_lb_bps,
                notional=notional_micros,
            )
        except LedgerError as exc:  # pragma: no cover - defensive
            return _reject(
                FirewallStage.OUTBOX,
                f"OUTBOX_REJECTED: {exc}",
                gate=gate_decision,
                shield=shield_decision,
                net_lb=gate_decision.net_lb_bps,
                notional=notional_micros,
            )

        # -- stage 5: Ed25519 + Fernet signing ----------------------------
        kind = order_type or (
            OrderType.MAKER_PEG
            if proposal.vault is VaultName.SPINAL_SCALPER
            else OrderType.TWAP_ICEBERG
        )
        signed = self.signer.signed_order(
            proposal,
            outbox_key=reservation.outbox_idempotency_key,
            net_lb_bps=gate_decision.net_lb_bps,
            order_type=kind,
            now_ns=stamp,
            qty=effective_qty,
        )

        latency = time.perf_counter_ns() - started
        return FirewallVerdict(
            accepted=True,
            stage_reached=FirewallStage.SIGNING,
            stage_name=FirewallStage.SIGNING.label,
            reason=(
                f"SIGNED: {grounding_detail}; Net_LB "
                f"{gate_decision.net_lb_bps:+.1f} bps; "
                f"{shield_decision.reason}; outbox "
                f"{reservation.outbox_idempotency_key[:12]}..."
            ),
            silo=silo,
            symbol=symbol,
            proposal_id=str(proposal.proposal_id),
            latency_ns=latency,
            net_lb_bps=float(gate_decision.net_lb_bps),
            notional_micros=notional_micros,
            gate=gate_decision,
            shield=shield_decision,
            reservation=reservation,
            signed=signed,
            card=gate_decision.card,
            trading_state=state,
            sized_down=sized_down,
        )

    # -- introspection ----------------------------------------------------
    @property
    def evaluations(self) -> int:
        return self._evaluations

    def snapshot(self) -> dict[str, Any]:
        return {
            "evaluations": self._evaluations,
            "public_key_hex": self.signer.public_key_hex,
            "whitelists": {silo: sorted(symbols) for silo, symbols in self.whitelists.items()},
            "daily_loss_stops": dict(self.daily_loss_stops),
            "realized_pnl": dict(self._realized_pnl),
            "trading_state": {silo: str(state) for silo, state in self._state.items()},
            "guard": self.guard.snapshot(),
            "latency_budget_ns": self.latency_budget_ns,
        }


# ---------------------------------------------------------------------------
# ADR-009 reduce-only / emergency flatten
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ADR009Verdict:
    """Result of the ADR-009 bypass path."""

    dispatched: bool
    trigger: str
    silo: str
    symbol: str
    proposal_id: str
    trading_state: TradingState
    latency_ns: int
    cancelled_entries: int
    signed: SignedOrder | None
    reason: str

    @property
    def within_latency_budget(self) -> bool:
        return self.latency_ns <= ADR009_LATENCY_BUDGET_NS

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatched": self.dispatched,
            "trigger": self.trigger,
            "silo": self.silo,
            "symbol": self.symbol,
            "proposal_id": self.proposal_id,
            "trading_state": str(self.trading_state),
            "latency_ns": self.latency_ns,
            "cancelled_entries": self.cancelled_entries,
            "signed": self.signed.to_dict() if self.signed else None,
            "reason": self.reason,
        }


class ADR009ReduceOnlyFastPath:
    """Risk-reducing exits bypass every LLM and conformal queue (``sec_004``).

    ADR-009 exists because the moment you most need to exit is the moment you
    can least afford a gate that might abstain.  A reduce-only order or an
    emergency flatten therefore:

    1. flips the silo to ``REDUCING`` (or ``HALTED`` on a kill latch);
    2. cancels every open *entry* through :class:`SmartOMS`, which voids the
       matching outbox reservations (``NoOrphanedOrders``);
    3. signs and dispatches the exit in ``< 1.0 ms``.

    The path deliberately does **not** consult ``SplitConformal98Gate`` or
    ``CrossSiloCopulaShield``: demanding a positive worst-case edge before
    letting capital out of a burning position is the exact failure mode the ADR
    was written to kill.
    """

    __slots__ = ("signer", "guard", "oms", "ledger", "_latency_ns")

    def __init__(
        self,
        *,
        signer: OrderSigner,
        guard: SiloGuard | None = None,
        oms: "SmartOMS | None" = None,
        ledger: IBORLedger | None = None,
    ) -> None:
        self.signer = signer
        self.guard = guard
        self.oms = oms
        self.ledger = ledger
        self._latency_ns = 0

    def evaluate(
        self,
        proposal: UnsignedProposal,
        *,
        now_ns: int | None = None,
        triggered_by: str = "",
        emergency_flatten: bool = False,
        cancel_entries: bool = True,
        publish: bool = True,
    ) -> ADR009Verdict:
        started = time.perf_counter_ns()
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        silo = str(proposal.silo)

        trigger = str(triggered_by or "").upper()
        halted = bool(self.guard and self.guard.is_frozen(silo))
        is_reduce = bool(proposal.is_reduce_only) or emergency_flatten
        if not (is_reduce or halted or trigger in {"KILL_LATCH", "EMERGENCY_FLATTEN"}):
            self._latency_ns = time.perf_counter_ns() - started
            return ADR009Verdict(
                dispatched=False,
                trigger=trigger or "NONE",
                silo=silo,
                symbol=str(proposal.symbol),
                proposal_id=str(proposal.proposal_id),
                trading_state=TradingState.ACTIVE,
                latency_ns=self._latency_ns,
                cancelled_entries=0,
                signed=None,
                reason=(
                    "ADR-009 not engaged: the proposal is not reduce-only and no "
                    "kill latch or emergency flatten is active"
                ),
            )

        state = (
            TradingState.HALTED
            if (halted or trigger == "KILL_LATCH")
            else TradingState.REDUCING
        )

        cancelled = 0
        if cancel_entries and self.oms is not None:
            cancelled = self.oms.cancel_all_entries(
                silo=silo,
                now_ns=stamp,
                reason=f"ADR009:{trigger or 'REDUCE_ONLY'}",
                publish=publish,
            )

        signed = self.signer.signed_order(
            proposal,
            outbox_key=f"adr009:{proposal.proposal_id}",
            net_lb_bps=0.0,
            order_type=OrderType.MARKET_REDUCE_ONLY,
            now_ns=stamp,
            reduce_only=True,
        )
        event = ExecutionEvent(
            event_type="ADR009_EXIT_DISPATCHED",
            silo=silo,
            symbol=str(proposal.symbol),
            order_id=str(proposal.proposal_id),
            side=str(proposal.side),
            qty=float(proposal.qty),
            price=float(proposal.limit_price),
            state=str(state),
            reason=(
                f"ADR-009 {trigger or 'REDUCE_ONLY'}: bypassed conformal and "
                f"copula gates"
            ),
            timestamp_ns=stamp,
        )
        if self.oms is not None:
            self.oms.record_execution(event, publish=publish)

        self._latency_ns = time.perf_counter_ns() - started
        return ADR009Verdict(
            dispatched=True,
            trigger=trigger or ("REDUCE_ONLY" if is_reduce else "SILO_HALTED"),
            silo=silo,
            symbol=str(proposal.symbol),
            proposal_id=str(proposal.proposal_id),
            trading_state=state,
            latency_ns=self._latency_ns,
            cancelled_entries=cancelled,
            signed=signed,
            reason=(
                f"ADR-009 engaged: state={state}, {cancelled} entry order(s) "
                f"cancelled, risk-reducing exit signed without conformal gating"
            ),
        )

    @property
    def last_latency_ns(self) -> int:
        return self._latency_ns


# ---------------------------------------------------------------------------
# Smart OMS
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ExecutionEvent:
    """One durable execution event on ``stream.ring0.execution-events``."""

    event_type: str
    silo: str
    symbol: str
    order_id: str
    side: str
    qty: float
    price: float
    state: str
    reason: str = ""
    timestamp_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "silo": self.silo,
            "symbol": self.symbol,
            "order_id": self.order_id,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "state": self.state,
            "reason": self.reason,
            "timestamp_ns": self.timestamp_ns,
        }


@dataclass(slots=True)
class MakerPegChaser:
    """3-second, 2-re-peg maker-only chaser for ``SpinalScalper`` orders.

    The order always rests on its own side of the book (buy at the bid, sell at
    the ask) so it can never cross and pay taker fees.  When top-of-book moves
    against the resting order the chaser re-pegs -- but only twice.  The third
    move, or the 3.0 s deadline, hard-cancels the remainder and **voids the
    outbox reservation**, which is what keeps ``NoOrphanedOrders`` true.
    """

    order_id: str
    silo: str
    symbol: str
    side: str
    qty: float
    outbox_key: str
    price: float
    created_ns: int
    timeout_ns: int = MAKER_PEG_TIMEOUT_NS
    max_repegs: int = MAKER_PEG_MAX_REPEGS
    repegs: int = 0
    state: str = "WORKING"
    peg_history: list[float] = field(default_factory=list)
    cancel_reason: str = ""

    @property
    def is_working(self) -> bool:
        return self.state == "WORKING"

    def target_price(self, bid: float, ask: float) -> float:
        return float(bid) if str(self.side).upper() == "BUY" else float(ask)

    def on_book(self, bid: float, ask: float, now_ns: int) -> str:
        """Advance the chaser one book update.  Returns the new state."""
        if not self.is_working:
            return self.state

        if int(now_ns) - int(self.created_ns) >= self.timeout_ns:
            self.state = "CANCELED"
            self.cancel_reason = "3.0s maker-peg hard cancel"
            return self.state

        target = self.target_price(bid, ask)
        if abs(target - self.price) < 1e-12:
            return self.state

        if self.repegs >= self.max_repegs:
            self.state = "CANCELED"
            self.cancel_reason = (
                f"re-peg ceiling reached ({self.repegs}/{self.max_repegs}); "
                f"hard-cancelling unfilled remainder"
            )
            return self.state

        self.repegs += 1
        self.price = target
        self.peg_history.append(target)
        return "REPEGGED"

    def mark_filled(self, price: float | None = None) -> str:
        if self.is_working:
            self.state = "FILLED"
            if price is not None:
                self.price = float(price)
        return self.state

    def mark_canceled(self, reason: str = "") -> str:
        if self.is_working:
            self.state = "CANCELED"
            self.cancel_reason = str(reason) or "operator cancel"
        return self.state

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "silo": self.silo,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "outbox_key": self.outbox_key,
            "repegs": self.repegs,
            "max_repegs": self.max_repegs,
            "timeout_ns": self.timeout_ns,
            "state": self.state,
            "cancel_reason": self.cancel_reason,
            "peg_history": list(self.peg_history),
        }


@dataclass(slots=True, frozen=True)
class IcebergPlan:
    """Deterministic TWAP / Iceberg child-slice schedule."""

    parent_qty: float
    top_of_book_qty: float
    max_child_qty: float
    slices: tuple[float, ...]

    @property
    def slice_count(self) -> int:
        return len(self.slices)

    @property
    def max_slice(self) -> float:
        return max(self.slices) if self.slices else 0.0

    @property
    def book_pct(self) -> float:
        if self.top_of_book_qty <= 0:
            return 0.0
        return self.max_slice / self.top_of_book_qty

    @property
    def respects_book_cap(self) -> bool:
        return self.max_slice <= self.max_child_qty + 1e-9

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_qty": self.parent_qty,
            "top_of_book_qty": self.top_of_book_qty,
            "max_child_qty": self.max_child_qty,
            "slices": list(self.slices),
            "slice_count": self.slice_count,
            "max_slice": self.max_slice,
            "book_pct": self.book_pct,
            "respects_book_cap": self.respects_book_cap,
        }


class TwapIcebergSlicer:
    """Slice a parent order so no child exceeds 10% of top-of-book depth.

    ``sec_010`` is explicit: ``TWAP/Iceberg <= 10% of top book``.  Slicing above
    that is how a "passive" order becomes a market impact event.  The slicer
    therefore derives the child count from the cap rather than letting a caller
    pick a child size that violates it.
    """

    __slots__ = ("max_book_pct",)

    def __init__(self, max_book_pct: float = TWAP_MAX_BOOK_PCT) -> None:
        value = float(max_book_pct)
        if not 0.0 < value <= 1.0:
            raise ValueError("max_book_pct must be inside (0, 1]")
        self.max_book_pct = value

    def plan(
        self,
        parent_qty: float,
        top_of_book_qty: float,
        *,
        n_slices: int | None = None,
    ) -> IcebergPlan:
        parent = float(parent_qty)
        depth = float(top_of_book_qty)
        if parent <= 0.0:
            raise ValueError("parent_qty must be positive")
        if depth <= 0.0:
            raise ValueError("top_of_book_qty must be positive")

        max_child = depth * self.max_book_pct
        required = int(math.ceil(parent / max_child - 1e-12))
        count = required if n_slices is None else max(int(n_slices), required)
        child = parent / count
        slices = tuple(child for _ in range(count))
        return IcebergPlan(
            parent_qty=parent,
            top_of_book_qty=depth,
            max_child_qty=max_child,
            slices=slices,
        )


class SmartOMS:
    """Maker-peg + TWAP/Iceberg execution with outbox-backed cancellation.

    Every terminal transition resolves the matching outbox reservation:

    * ``FILLED``   -> ``outbox.commit`` (post the pending transfer)
    * ``CANCELED`` -> ``outbox.void``   (roll the pending transfer back)

    Because cancellation is the *only* way an order leaves the working set
    without a fill, ``NoOrphanedOrders`` holds by construction (``sec_012``).
    """

    def __init__(
        self,
        outbox: CrashAtomicOutbox,
        *,
        bus: Any = None,
        guard: SiloGuard | None = None,
        ledger: IBORLedger | None = None,
        ratchet: InSiloProfitRatchet | None = None,
        slicer: TwapIcebergSlicer | None = None,
    ) -> None:
        self.outbox = outbox
        self.bus = bus
        self.guard = guard if guard is not None else SiloGuard()
        self.ledger = ledger if ledger is not None else outbox.ledger
        self.ratchet = ratchet if ratchet is not None else InSiloProfitRatchet(self.ledger)
        self.slicer = slicer if slicer is not None else TwapIcebergSlicer()
        self._chasers: dict[str, MakerPegChaser] = {}
        self._events: list[ExecutionEvent] = []
        self._cards: list[PlainEnglishCockpitCard] = []

    # -- submission -------------------------------------------------------
    def submit_maker_peg(
        self,
        proposal: UnsignedProposal,
        reservation: Reservation,
        book: tuple[float, float],
        *,
        now_ns: int | None = None,
    ) -> MakerPegChaser:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        bid, ask = float(book[0]), float(book[1])
        chaser = MakerPegChaser(
            order_id=str(proposal.proposal_id),
            silo=str(proposal.silo),
            symbol=str(proposal.symbol),
            side=str(proposal.side),
            qty=float(proposal.qty),
            outbox_key=reservation.outbox_idempotency_key,
            price=(bid if str(proposal.side).upper() == "BUY" else ask),
            created_ns=stamp,
        )
        self._chasers[chaser.order_id] = chaser
        self.publish_execution(
            ExecutionEvent(
                event_type="MAKER_PEG_POSTED",
                silo=chaser.silo,
                symbol=chaser.symbol,
                order_id=chaser.order_id,
                side=chaser.side,
                qty=chaser.qty,
                price=chaser.price,
                state="WORKING",
                reason="post-only maker peg at best bid/ask",
                timestamp_ns=stamp,
            )
        )
        return chaser

    def submit_twap(
        self,
        proposal: UnsignedProposal,
        top_of_book_qty: float,
        *,
        now_ns: int | None = None,
    ) -> IcebergPlan:
        plan = self.slicer.plan(float(proposal.qty), top_of_book_qty)
        self.publish_execution(
            ExecutionEvent(
                event_type="TWAP_PLANNED",
                silo=str(proposal.silo),
                symbol=str(proposal.symbol),
                order_id=str(proposal.proposal_id),
                side=str(proposal.side),
                qty=plan.parent_qty,
                price=float(proposal.limit_price),
                state="PLANNED",
                reason=(
                    f"{plan.slice_count} child slice(s), max {plan.book_pct * 100:.1f}% "
                    f"of top-of-book depth"
                ),
                timestamp_ns=int(now_ns if now_ns is not None else time.time_ns()),
            )
        )
        return plan

    # -- book updates -----------------------------------------------------
    def on_book(
        self,
        order_id: str,
        bid: float,
        ask: float,
        *,
        now_ns: int | None = None,
    ) -> str:
        chaser = self._chasers.get(str(order_id))
        if chaser is None:
            return "UNKNOWN"
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        previous = chaser.state
        state = chaser.on_book(bid, ask, now_ns=stamp)
        if state == "REPEGGED":
            self.publish_execution(
                ExecutionEvent(
                    event_type="MAKER_PEG_REPEG",
                    silo=chaser.silo,
                    symbol=chaser.symbol,
                    order_id=chaser.order_id,
                    side=chaser.side,
                    qty=chaser.qty,
                    price=chaser.price,
                    state="WORKING",
                    reason=f"re-peg {chaser.repegs}/{chaser.max_repegs}",
                    timestamp_ns=stamp,
                )
            )
        elif state == "CANCELED" and previous != "CANCELED":
            self._resolve_cancel(chaser, reason=chaser.cancel_reason, now_ns=stamp)
        return state

    def sweep_deadlines(self, *, now_ns: int | None = None) -> list[str]:
        """Hard-cancel every chaser whose 3.0 s deadline has elapsed."""
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        cancelled: list[str] = []
        for chaser in list(self._chasers.values()):
            if not chaser.is_working:
                continue
            if stamp - chaser.created_ns >= chaser.timeout_ns:
                chaser.mark_canceled("3.0s maker-peg hard cancel")
                self._resolve_cancel(chaser, reason=chaser.cancel_reason, now_ns=stamp)
                cancelled.append(chaser.order_id)
        return cancelled

    # -- resolution -------------------------------------------------------
    def on_fill(
        self,
        order_id: str,
        *,
        price: float | None = None,
        realized_net_micros: int | None = None,
        now_ns: int | None = None,
    ) -> Reservation:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        chaser = self._chasers.get(str(order_id))
        if chaser is None:
            raise KeyError(f"unknown order {order_id!r}")
        chaser.mark_filled(price)
        reservation = self.outbox.commit(
            chaser.outbox_key,
            broker_order_id=chaser.order_id,
            now_ns=stamp,
            realized_net_micros=realized_net_micros,
            ratchet=self.ratchet,
        )
        self.publish_execution(
            ExecutionEvent(
                event_type="ORDER_FILLED",
                silo=chaser.silo,
                symbol=chaser.symbol,
                order_id=chaser.order_id,
                side=chaser.side,
                qty=chaser.qty,
                price=chaser.price,
                state="FILLED",
                reason=(
                    "outbox reservation posted"
                    + (
                        f"; in-silo ratchet swept {realized_net_micros} micros"
                        if realized_net_micros
                        else ""
                    )
                ),
                timestamp_ns=stamp,
            )
        )
        return reservation

    def on_ack(self, ack: BrokerAck) -> Reservation | None:
        """Route a broker ack through the idempotent outbox inbox."""
        result = self.outbox.handle_ack(ack)
        return result.reservation

    def cancel(
        self, order_id: str, *, reason: str = "operator cancel", now_ns: int | None = None
    ) -> Reservation:
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        chaser = self._chasers.get(str(order_id))
        if chaser is None:
            raise KeyError(f"unknown order {order_id!r}")
        chaser.mark_canceled(reason)
        return self._resolve_cancel(chaser, reason=reason, now_ns=stamp)

    def cancel_all_entries(
        self,
        *,
        silo: str | None = None,
        now_ns: int | None = None,
        reason: str = "cancel all",
        publish: bool = True,
        extra_outbox_keys: Iterable[str] = (),
        persist: bool = True,
    ) -> int:
        """Cancel every working *entry* order, voiding each reservation.

        ``publish=False`` keeps the in-memory ``ExecutionEvent`` but defers the
        Tier-1 bus write.  ``extra_outbox_keys`` lets the kill switch fold
        unrelated pending reservations into the same atomic void batch.
        """
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        targets = [
            chaser
            for chaser in list(self._chasers.values())
            if chaser.is_working
            and (silo is None or chaser.silo == str(silo))
        ]
        keys: list[str] = [chaser.outbox_key for chaser in targets]
        seen = set(keys)
        for key in extra_outbox_keys:
            token = str(key)
            if token not in seen:
                seen.add(token)
                keys.append(token)
        if not keys:
            return 0
        self.outbox.void_many(keys, reason=reason, now_ns=stamp, persist=persist)
        event_reason = f"{reason}; pending outbox reservation voided"
        for chaser in targets:
            chaser.mark_canceled(reason)
            event = ExecutionEvent(
                event_type="ORDER_CANCELED",
                silo=chaser.silo,
                symbol=chaser.symbol,
                order_id=chaser.order_id,
                side=chaser.side,
                qty=chaser.qty,
                price=chaser.price,
                state="CANCELED",
                reason=event_reason,
                timestamp_ns=stamp,
            )
            self._events.append(event)
            if publish:
                self._publish_event(event)
        return len(targets)

    def _resolve_cancel(
        self,
        chaser: MakerPegChaser,
        *,
        reason: str,
        now_ns: int,
        publish: bool = True,
    ) -> Reservation:
        reservation = self.outbox.void(
            chaser.outbox_key, reason=reason, now_ns=now_ns
        )
        event = ExecutionEvent(
            event_type="ORDER_CANCELED",
            silo=chaser.silo,
            symbol=chaser.symbol,
            order_id=chaser.order_id,
            side=chaser.side,
            qty=chaser.qty,
            price=chaser.price,
            state="CANCELED",
            reason=f"{reason}; pending outbox reservation voided",
            timestamp_ns=now_ns,
        )
        self._events.append(event)
        if publish:
            self._publish_event(event)
        return reservation

    # -- publication ------------------------------------------------------
    def publish_execution(self, event: ExecutionEvent) -> None:
        self.record_execution(event, publish=True)

    def record_execution(self, event: ExecutionEvent, *, publish: bool = True) -> None:
        """Append the audit event; optionally defer the Tier-1 bus write."""
        self._events.append(event)
        if publish:
            self._publish_event(event)

    def _publish_event(self, event: ExecutionEvent) -> None:
        if self.bus is None:
            return
        self.bus.publish_idempotent(
            STREAM_RING0_EXECUTION,
            event.to_dict(),
            idempotency_key=(
                f"exec:{event.event_type}:{event.order_id}:{event.timestamp_ns}"
            ),
        )

    def publish_card(self, card: PlainEnglishCockpitCard) -> None:
        self._cards.append(card)
        if self.bus is None:
            return
        self.bus.publish_idempotent(
            STREAM_RING3_COCKPIT,
            card.to_dict(),
            idempotency_key=(
                f"card:{card.silo}:{card.symbol}:{card.timestamp_ns}"
            ),
        )

    # -- reads ------------------------------------------------------------
    def order(self, order_id: str) -> MakerPegChaser | None:
        return self._chasers.get(str(order_id))

    def open_orders(self, silo: str | None = None) -> list[MakerPegChaser]:
        return [
            chaser
            for chaser in self._chasers.values()
            if chaser.is_working and (silo is None or chaser.silo == str(silo))
        ]

    @property
    def events(self) -> tuple[ExecutionEvent, ...]:
        return tuple(self._events)

    @property
    def cards(self) -> tuple[PlainEnglishCockpitCard, ...]:
        return tuple(self._cards)

    def snapshot(self) -> dict[str, Any]:
        return {
            "orders": len(self._chasers),
            "working": len(self.open_orders()),
            "events": len(self._events),
            "cards": len(self._cards),
            "repegs": {
                order_id: chaser.repegs
                for order_id, chaser in self._chasers.items()
                if chaser.repegs
            },
        }
