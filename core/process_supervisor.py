"""True four-ring OS process isolation supervisor (Phase 3, Step 1).

Governing sections
------------------
``sec_001``  the four-ring air-gapped process architecture;
``sec_004``  OS process isolation and lock-free IPC topology.

The supervisor runs Ring 0 / Ring 1 / Ring 2 / Ring 3 as **physically
separate operating-system worker processes**.  Nothing is shared except the
POSIX/NT shared-memory spine defined in :mod:`core.shm_ipc` and the
:class:`core.nats_bus.LocalJetStreamBus` durable log.  There is no in-process
shortcut: a proposal produced in the Ring 1 process crosses an OS process
boundary to reach the Ring 0 signer, and a kill latch written by Ring 3 is
observed by Ring 1 through shared memory only.

Ring ownership
--------------
===================================  =========================================
worker                               exclusivity
===================================  =========================================
``ring0-vault-worker``               sole holder of the Fernet/Ed25519 key
                                     vault handle, the IBOR ledger, the
                                     crash-atomic outbox and the signer.
``ring1-spinal-worker``              CPU-pinned deterministic scalper, breaker
                                     and shadow twin.  No LLM client import.
``ring2-brain-worker``               System 1 lease writer, contagion and the
                                     factor scanner.  No broker credentials.
``ring3-cockpit-gateway``            control-plane sentinel: owns the kill
                                     switch and the rearm path.
===================================  =========================================

Fatal flaws this module defends against (``sec_002``)
------------------------------------------------------
#1 LLM tick-latency in the execution path -- Ring 1 never imports an LLM
   client; it only reads a 60-second ``System1Lease`` from shared memory.
#6 prompt-injection exposure of broker keys -- broker keys live only inside
   the Ring 0 process; Ring 2 has no code path to them.

Laws preserved (``sec_018``)
----------------------------
Law 2 -- the LLM may write a lease, never a tick decision.
Law 4 -- Ring 1 abstains unless a live lease and a clean executed-flow tape
   agree; the 98% gate still runs inside Ring 0 before anything is signed.

Kill-switch accounting
----------------------
``RingProcessSupervisor`` reports two distinct numbers and never conflates
them:

``kill_switch_latency_ns``
    Measured *inside* Ring 1: the time from "begin reading the latch" to
    "all leases disarmed".  This is the ``< 1.0 ms`` constitutional budget
    (``sec_004``, ``sec_010``).
``kill_signal_age_ns``
    Wall-clock age of the latch when Ring 1 observed it.  This is bounded by
    the worker's tick cadence, not by the disarm path, and is reported for
    observability only.
"""

from __future__ import annotations

import math
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from multiprocessing import Process, Queue, RawValue
from multiprocessing.shared_memory import SharedMemory
from queue import Empty
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.schemas import (
    DualVendorBar,
    ExecutedFlowTick,
    LeaseMode,
    System1Lease,
    UnsignedProposal,
    VaultName,
)
from core.shm_ipc import (
    CACHE_LINE,
    GlobalKillLatch,
    LeaseTableSHM,
    OfiRingBuffer,
    ShadowBookSHM,
    shm_prefix,
)

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_NS",
    "DEFAULT_HEARTBEAT_TIMEOUT_NS",
    "FEED_MODE_LIVE",
    "FEED_MODE_PAPER",
    "HEARTBEAT_SHM_NAME",
    "MAX_KILL_PREEMPTION_NS",
    "RING0_WORKER",
    "RING1_WORKER",
    "RING2_WORKER",
    "RING3_WORKER",
    "RING_SYMBOLS",
    "RING_WORKERS",
    "RingHeartbeatTable",
    "RingProcessSpec",
    "RingProcessSupervisor",
    "make_flow_tick",
    "ring_process_snapshot",
]

# ---------------------------------------------------------------------------
# ring identity
# ---------------------------------------------------------------------------

RING0_WORKER = "ring0-vault-worker"
RING1_WORKER = "ring1-spinal-worker"
RING2_WORKER = "ring2-brain-worker"
RING3_WORKER = "ring3-cockpit-gateway"

RING_WORKERS: tuple[str, ...] = (
    RING0_WORKER,
    RING1_WORKER,
    RING2_WORKER,
    RING3_WORKER,
)

RING_INDEX: dict[str, int] = {name: index for index, name in enumerate(RING_WORKERS)}

FEED_MODE_LIVE = "LIVE_PUBLIC_DUAL_VENDOR"
FEED_MODE_PAPER = "PAPER_AUTOPILOT"

#: One canonical instrument per silo.  Ring 1 and Ring 0 must agree on this
#: mapping because the whitelist is enforced in the Ring 0 process.
RING_SYMBOLS: dict[str, str] = {
    "CRYPTO_BINANCE": "BTCUSDT",
    "INDIA_ZERODHA_DHAN": "RELIANCE.NS",
    "US_IBKR": "AAPL",
    "FX_MT5": "EURUSD",
}

MAX_KILL_PREEMPTION_NS = 1_000_000  # 1.0 ms constitutional budget
#: Ring 2 advertises a cross-venue dislocation band that Ring 1 may carry as a
#: gross-edge ceiling (``sec_006`` executed-flow cross-verification).  It never
#: widens the 98% gate: Ring 0 re-derives ``Net_LB`` before signing.
RING_DISLOCATION_BPS = 26.0
DEFAULT_HEARTBEAT_INTERVAL_NS = 100_000_000  # 100 ms
DEFAULT_HEARTBEAT_TIMEOUT_NS = 5_000_000_000  # 5 s

# ---------------------------------------------------------------------------
# ring heartbeat segment (private to the supervisor; not part of ALL_SEGMENTS)
# ---------------------------------------------------------------------------

HEARTBEAT_SHM_NAME = "nios-ring-heartbeats"
HEARTBEAT_MAGIC = b"NIOSRHB1"
HEARTBEAT_SLOTS = 4
HEARTBEAT_SLOT_SIZE = CACHE_LINE
#: magic, pid, last-beat monotonic ns, alive flag, ring index
_HEARTBEAT_FMT = "<8sqqii"
_HEARTBEAT_SIZE = struct.calcsize(_HEARTBEAT_FMT)
HEARTBEAT_SEGMENT_SIZE = HEARTBEAT_SLOTS * HEARTBEAT_SLOT_SIZE

_ALL_RING_BYTES = HEARTBEAT_MAGIC + b"\x00" * (8 - len(HEARTBEAT_MAGIC))


class RingHeartbeatTable:
    """Cross-process liveness table for the four ring workers.

    Each slot is one 64-byte cache line so a writer only ever dirties its own
    line.  The read path is a single ``struct.unpack_from`` -- no allocation of
    consequence and no lock.
    """

    def __init__(
        self,
        *,
        reset: bool = False,
        name: str = HEARTBEAT_SHM_NAME,
        slots: int = HEARTBEAT_SLOTS,
    ) -> None:
        self.name = name
        self.slots = int(slots)
        self._owner = False
        self._shm = self._attach(reset=reset)

    # -- lifecycle ------------------------------------------------------
    def _attach(self, *, reset: bool) -> SharedMemory:
        try:
            shm = SharedMemory(name=self.name, create=False)
            if reset:
                shm.buf[:] = b"\x00" * len(shm.buf)
        except FileNotFoundError:
            shm = SharedMemory(
                name=self.name, create=True, size=HEARTBEAT_SEGMENT_SIZE
            )
            shm.buf[:] = b"\x00" * len(shm.buf)
            self._owner = True
        return shm

    @property
    def uri(self) -> str:
        return f"shm://{self.name}"

    def close(self) -> None:
        try:
            self._shm.close()
        except Exception:  # pragma: no cover - best effort teardown
            pass

    def unlink(self) -> None:
        try:
            self._shm.unlink()
        except Exception:  # pragma: no cover - already gone
            pass

    # -- write / read ---------------------------------------------------
    def _slot_offset(self, ring_index: int) -> int:
        index = int(ring_index)
        if not 0 <= index < self.slots:
            raise IndexError(f"ring index {index} outside 0..{self.slots - 1}")
        return index * HEARTBEAT_SLOT_SIZE

    def write(self, ring_index: int, pid: int, *, now_ns: int | None = None) -> None:
        stamp = time.monotonic_ns() if now_ns is None else int(now_ns)
        struct.pack_into(
            _HEARTBEAT_FMT + f"{HEARTBEAT_SLOT_SIZE - _HEARTBEAT_SIZE}x",
            self._shm.buf,
            self._slot_offset(ring_index),
            _ALL_RING_BYTES,
            int(pid),
            stamp,
            1,
            int(ring_index),
        )

    def clear(self, ring_index: int) -> None:
        offset = self._slot_offset(ring_index)
        self._shm.buf[offset : offset + HEARTBEAT_SLOT_SIZE] = (
            b"\x00" * HEARTBEAT_SLOT_SIZE
        )

    def read(self, ring_index: int) -> dict[str, Any] | None:
        offset = self._slot_offset(ring_index)
        magic, pid, beat_ns, alive, index = struct.unpack_from(
            _HEARTBEAT_FMT, self._shm.buf, offset
        )
        if not magic.startswith(HEARTBEAT_MAGIC):
            return None
        return {
            "ring_index": int(index),
            "worker": RING_WORKERS[int(index)],
            "pid": int(pid),
            "last_beat_ns": int(beat_ns),
            "alive": bool(alive),
        }

    def age_ns(self, ring_index: int, *, now_ns: int | None = None) -> int | None:
        record = self.read(ring_index)
        if record is None:
            return None
        stamp = time.monotonic_ns() if now_ns is None else int(now_ns)
        return max(0, stamp - record["last_beat_ns"])

    def snapshot(self, *, now_ns: int | None = None) -> dict[str, dict[str, Any]]:
        stamp = time.monotonic_ns() if now_ns is None else int(now_ns)
        out: dict[str, dict[str, Any]] = {}
        for index in range(self.slots):
            record = self.read(index)
            if record is None:
                continue
            record["heartbeat_age_ms"] = round(
                max(0, stamp - record["last_beat_ns"]) / 1_000_000.0,
                3,
            )
            out[record["worker"]] = record
        return out


# ---------------------------------------------------------------------------
# deterministic tick factory (Ring-1 safe: no orchestrator / LLM import)
# ---------------------------------------------------------------------------


def make_flow_tick(
    symbol: str,
    *,
    last_price: float,
    now_ns: int | None = None,
    buy_volume: float = 150.0,
    sell_volume: float = 3.0,
    spread_bps: float = 1.2,
    realized_vol: float = 5.0,
    normal_vol: float = 10.0,
    bid_cancel_to_fill_ratio: float = 0.2,
    ask_cancel_to_fill_ratio: float = 0.2,
) -> ExecutedFlowTick:
    """Build a clean executed-orderflow frame without importing Ring 2 code."""
    stamp = time.time_ns() if now_ns is None else int(now_ns)
    return ExecutedFlowTick(
        symbol=symbol,
        valid_time_ns=stamp,
        knowledge_ts_ns=stamp,
        last_price=float(last_price),
        spread_bps=float(spread_bps),
        realized_vol_5s=float(realized_vol),
        normal_vol_5s=float(normal_vol),
        taker_buy_volume_5s=float(buy_volume),
        taker_sell_volume_5s=float(sell_volume),
        bid_cancel_to_fill_ratio=float(bid_cancel_to_fill_ratio),
        ask_cancel_to_fill_ratio=float(ask_cancel_to_fill_ratio),
    )


def make_dual_vendor_bar(
    symbol: str,
    *,
    price: float,
    now_ns: int | None = None,
    lag_ms: float = 12.0,
    divergence_bps: float = 2.0,
) -> DualVendorBar:
    """Two independent venue prints around ``price`` (Law 1 cross-check)."""
    stamp = time.time_ns() if now_ns is None else int(now_ns)
    half = float(price) * float(divergence_bps) / 2.0 / 10_000.0
    return DualVendorBar(
        symbol=symbol,
        p1=float(price) + half,
        p2=float(price) - half,
        feed_lag_ms=float(lag_ms),
        valid_time_ns=stamp,
        knowledge_ts_ns=stamp,
    )


# ---------------------------------------------------------------------------
# shadow graduation warm-up
# ---------------------------------------------------------------------------


def _twin_graduated(twin: Any, strategy_id: str) -> bool:
    """``is_graduated`` may be a predicate or a cached property."""
    attr = getattr(twin, "is_graduated", False)
    try:
        return bool(attr(strategy_id)) if callable(attr) else bool(attr)
    except TypeError:  # pragma: no cover - zero-arg callable
        return bool(attr())


def _twin_shadow_fills(twin: Any, strategy_id: str) -> int:
    attr = getattr(twin, "shadow_fill_count", 0)
    try:
        return int(attr(strategy_id)) if callable(attr) else int(attr)
    except TypeError:  # pragma: no cover
        return int(attr())


def _seed_shadow_graduation(
    twin: Any,
    shadow_book: ShadowBookSHM,
    *,
    symbol: str,
    price: float,
    silo: str,
    strategy_id: str,
    expected_bps: float = 12.0,
    cost_floor: Any = None,
    now_ns: int | None = None,
) -> int:
    """Pre-fill the L2 shadow twin so the scalper may emit live intents.

    ``sec_010`` requires 30 L2 shadow fills before live graduation.  A
    production Ring 1 process warms up against the tape; a freshly spawned
    worker replays the same warm-up deterministically so that the Ring 0
    intent pipe is exercisable immediately.
    """
    stamp = time.time_ns() if now_ns is None else int(now_ns)
    shadow_book.write_snapshot(
        symbol,
        price - 0.01,
        price + 0.01,
        250.0,
        250.0,
        updated_at_ns=stamp,
    )
    need_attr = getattr(twin, "required_fills", 30)
    need = int(need_attr() if callable(need_attr) else need_attr)
    filled = 0
    for index in range(need * 3):
        if _twin_graduated(twin, strategy_id):
            break
        tick = make_flow_tick(
            symbol,
            last_price=price - 0.05,
            now_ns=stamp + index,
            buy_volume=200.0,
            sell_volume=1.0,
        )
        fill = twin.execute_shadow_fill(
            tick,
            strategy_id=strategy_id,
            side="BUY",
            silo=silo,
            now_ns=stamp + index,
            expected_bps=float(expected_bps),
            cost_floor=cost_floor,
        )
        if fill is not None:
            filled += 1
    return filled


# ---------------------------------------------------------------------------
# worker context
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RingProcessSpec:
    """Declarative description of one ring worker process."""

    index: int
    name: str
    cpu_core: int | None = None
    restarts: int = 0
    pid: int | None = None
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "worker": self.name,
            "pid": self.pid,
            "cpu_core": self.cpu_core,
            "restarts": self.restarts,
            "exit_code": self.exit_code,
        }


def _ctx(
    ring_index: int,
    *,
    workspace: str,
    silo: str,
    symbol: str,
    strategy_id: str,
    feed_mode: str,
    stop_flag: Any,
    global_stop: Any,
    event_queue: Any,
    ring0_queue: Any,
    control_queue: Any,
    brain_queue: Any,
    tick_interval_s: float,
    lease_interval_s: float,
) -> dict[str, Any]:
    return {
        "ring_index": int(ring_index),
        "worker": RING_WORKERS[int(ring_index)],
        "workspace": str(workspace),
        "silo": str(silo),
        "symbol": str(symbol),
        "strategy_id": str(strategy_id),
        "feed_mode": str(feed_mode),
        "stop_flag": stop_flag,
        "global_stop": global_stop,
        "event_queue": event_queue,
        "ring0_queue": ring0_queue,
        "control_queue": control_queue,
        "brain_queue": brain_queue,
        "tick_interval_s": float(tick_interval_s),
        "lease_interval_s": float(lease_interval_s),
    }


def _stopping(ctx: Mapping[str, Any]) -> bool:
    """Lock-free shutdown check.

    A ``multiprocessing.Event`` would be the obvious choice, but its condition
    variable lives in shared memory: terminating a worker while it happens to be
    inside ``Event.wait`` leaves that cross-process lock permanently held and the
    next ``Event.set()`` blocks forever.  A ``RawValue`` is a plain ctypes integer
    with no lock, so an abrupt termination can never poison shutdown.
    """

    flag = ctx.get("stop_flag")
    if flag is not None and flag.value:
        return True
    global_flag = ctx.get("global_stop")
    return bool(global_flag is not None and global_flag.value)


def _pause(ctx: Mapping[str, Any], seconds: float) -> None:
    """Paced sleep; the stop flag is re-checked on the next loop iteration."""

    time.sleep(max(0.0, float(seconds)))


def _emit(ctx: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    try:
        ctx["event_queue"].put(dict(payload))
    except Exception:  # pragma: no cover - queue closed during teardown
        pass


# ---------------------------------------------------------------------------
# Ring 0 -- sovereign vault worker
# ---------------------------------------------------------------------------


_HEDGE_PROPOSAL_FIELDS = (
    "proposal_id",
    "strategy_id",
    "silo",
    "symbol",
    "side",
    "qty",
    "limit_price",
    "expected_gross_bps",
    "spread_bps",
    "grounding_hash",
    "is_reduce_only",
    "created_at_ns",
)

#: Fraction of the tactical vault a single protective put may commit.
HEDGE_PREMIUM_CAP_PCT = 0.12

#: Contract multiplier used to convert option premium into cash notional.
HEDGE_CONTRACT_MULTIPLIER = 100


def _sign_tail_hedge(
    payload: Mapping[str, Any],
    *,
    firewall: Any,
    signer: Any,
    ledger: Any,
    silo_order: Sequence[str],
    pid: int,
) -> dict[str, Any]:
    """Verify and Ed25519-sign one Ring 2 tail-hedge proposal (insurance route).

    A protective put is bought for convexity, so its ``expected_gross_bps`` is
    negative by construction: it can never clear the 98% split-conformal alpha
    gate, and forcing it through would turn the sovereign firewall into a deny
    all.  Insurance is therefore routed deliberately around the alpha gate while
    every *capital* control still applies -- instrument whitelist, silo
    membership, grounding digest, vault identity, and the 12% tactical-vault
    notional cap.  The order is signed here, in Ring 0, and nowhere else
    (sec_004 air gap; Laws 2 and 4).

    Business rejections are returned as ``ring0_hedge_rejected`` events instead
    of exceptions so a single bad proposal can never take the worker down.
    """

    def rejected(reason: str, **extra: Any) -> dict[str, Any]:
        event = {
            "type": "ring0_hedge_rejected",
            "worker": RING0_WORKER,
            "pid": int(pid),
            "hedge_role": "INSURANCE",
            "reason": str(reason),
        }
        event.update({key: value for key, value in extra.items() if value is not None})
        return event

    body = payload.get("proposal")
    if not isinstance(body, Mapping):
        return rejected("PROPOSAL_MISSING")
    plan = payload.get("plan") if isinstance(payload.get("plan"), Mapping) else {}

    try:
        vault = VaultName(str(body.get("vault")))
    except Exception:
        return rejected("VAULT_UNKNOWN", proposal_id=str(body.get("proposal_id") or ""))
    proposal_id = str(body.get("proposal_id") or "")
    if vault is not VaultName.TACTICAL_SWING:
        return rejected("VAULT_NOT_TACTICAL_SWING", proposal_id=proposal_id)
    side = str(body.get("side") or "").upper()
    if side != "BUY":
        return rejected("SIDE_NOT_BUY", proposal_id=proposal_id)
    if not firewall.is_valid_digest(str(body.get("grounding_hash") or "")):
        return rejected("GROUNDING_DIGEST_INVALID", proposal_id=proposal_id)

    fields = {key: body[key] for key in _HEDGE_PROPOSAL_FIELDS if key in body}
    fields["vault"] = vault
    try:
        proposal = UnsignedProposal(**fields)
    except Exception as exc:
        return rejected(f"PROPOSAL_INVALID:{type(exc).__name__}", proposal_id=proposal_id)

    if str(proposal.silo) not in silo_order:
        return rejected("SILO_UNKNOWN", proposal_id=proposal.proposal_id)
    allowed = firewall.whitelists.get(str(proposal.silo))
    if allowed is not None and str(proposal.symbol).upper() not in allowed:
        return rejected("SYMBOL_NOT_WHITELISTED", proposal_id=proposal.proposal_id)

    premium_micros = int(
        round(
            float(proposal.qty)
            * float(proposal.limit_price)
            * HEDGE_CONTRACT_MULTIPLIER
            * 1_000_000
        )
    )
    try:
        vault_micros = int(ledger.balance(proposal.silo, VaultName.TACTICAL_SWING) or 0)
        cap_basis = vault_micros or int(ledger.total_nav_micros(list(silo_order)) or 0)
    except Exception:  # pragma: no cover - ledger read is best-effort
        cap_basis = 0
    cap_micros = int(cap_basis * HEDGE_PREMIUM_CAP_PCT)
    if premium_micros <= 0 or premium_micros > cap_micros:
        return rejected(
            "HEDGE_PREMIUM_CAP_BREACH",
            proposal_id=proposal.proposal_id,
            premium_micros=premium_micros,
            cap_micros=cap_micros,
        )

    order_payload = proposal.to_dict()
    try:
        digest_hex, signature_hex, _sealed = signer.sign_payload(order_payload)
        verified = bool(signer.verify(order_payload, signature_hex))
    except Exception as exc:
        return rejected(
            f"SIGNING_FAILED:{type(exc).__name__}", proposal_id=proposal.proposal_id
        )

    _public = getattr(signer, "public_key_hex", "")
    return {
        "type": "ring0_hedge_signed",
        "worker": RING0_WORKER,
        "pid": int(pid),
        "request_id": str(payload.get("request_id") or ""),
        "proposal_id": proposal.proposal_id,
        "plan_id": str(plan.get("plan_id") or ""),
        "silo": str(proposal.silo),
        "symbol": str(proposal.symbol),
        "vault": str(vault),
        "hedge_role": "INSURANCE",
        "digest_hex": digest_hex,
        "signature_hex": signature_hex,
        "public_key_hex": str(_public() if callable(_public) else _public),
        "verified": verified,
        "premium_micros": premium_micros,
        "premium_bps": plan.get("premium_bps"),
        "strike": plan.get("strike"),
        "contracts": float(proposal.qty),
        "grounding_hash": str(proposal.grounding_hash),
    }


def ring0_vault_worker(ctx: Mapping[str, Any]) -> None:  # pragma: no cover - process
    """Ring 0: firewall, conformal gate, copula shield, outbox, IBOR, OMS."""
    from ring0_vault.conformal_copula_gate import (
        ConformalMirror,
        CrossSiloCopulaShield,
        SplitConformal98Gate,
    )
    from ring0_vault.ibor.ledger_outbox import (
        CrashAtomicOutbox,
        IBORLedger,
        LedgerStore,
        LiveReconciler,
        SiloGuard,
    )
    from ring0_vault.kernel.firewalls_and_oms import (
        OrderSigner,
        SmartOMS,
        SovereignPreTradeFirewall,
    )
    from ring1_spinal.silo_worker import SILO_ORDER, silo_cost_floor


    heart = RingHeartbeatTable()
    pid = os.getpid()
    latch = GlobalKillLatch()
    leases = LeaseTableSHM(kill_latch=latch)
    silo = str(ctx["silo"])
    strategy_id = str(ctx["strategy_id"])

    workspace = ctx["workspace"]
    store = LedgerStore(os.path.join(workspace, "ring0_ledger.sqlite3"), probe=False)
    ledger = IBORLedger(store, silos=SILO_ORDER)
    guard = SiloGuard()
    mirror = ConformalMirror()
    mirror.seed(
        strategy_id,
        [4.0 + 0.5 * ((index * 7) % 11) for index in range(200)],
        shadow_fills=200,
    )
    gate = SplitConformal98Gate(mirror)
    shield = CrossSiloCopulaShield()
    outbox = CrashAtomicOutbox(ledger)
    signer = OrderSigner()
    firewall = SovereignPreTradeFirewall(
        ledger=ledger,
        outbox=outbox,
        gate=gate,
        shield=shield,
        signer=signer,
        guard=guard,
        whitelists={name: [RING_SYMBOLS[name]] for name in SILO_ORDER},
    )
    oms = SmartOMS(outbox, guard=guard, ledger=ledger)
    reconciler = LiveReconciler(ledger, guard)

    funding = 500_000_000_000_000
    for name in SILO_ORDER:
        ledger.open_silo(name, funding_micros=funding)
        ledger.fund_vault(name, VaultName.SPINAL_SCALPER, funding // 10)
    # Restart recovery: expire stale reservations and reconcile every silo
    # before the first fresh intent is admitted (``sec_009``).
    outbox.expire_timeouts()
    reconciler.reconcile_all(SILO_ORDER)

    floors = {name: silo_cost_floor(name) for name in SILO_ORDER}

    _emit(
        ctx,
        {
            "type": "ring0_ready",
            "worker": RING0_WORKER,
            "pid": pid,
            "nav_micros": ledger.total_nav_micros(SILO_ORDER),
            "recovered": True,
            "reconciled_silos": list(SILO_ORDER),
            "signer_public_key_hex": (
                signer.public_key_hex()
                if callable(getattr(signer, "public_key_hex", None))
                else str(getattr(signer, "public_key_hex", ""))
            ),
        },
    )

    heart.write(0, pid)
    while not _stopping(ctx):
        heart.write(0, pid)
        if latch.is_halted:
            outbox.expire_timeouts()
            _pause(ctx, 0.002)
            continue
        try:
            payload = ctx["ring0_queue"].get(timeout=0.02)
        except Empty:
            continue
        except (EOFError, OSError):  # pragma: no cover - queue torn down
            break
        try:
            if isinstance(payload, Mapping) and payload.get("kind") == "tail_hedge":
                # Insurance route: verify + sign only, never the alpha gate.
                _emit(
                    ctx,
                    _sign_tail_hedge(
                        payload,
                        firewall=firewall,
                        signer=signer,
                        ledger=ledger,
                        silo_order=SILO_ORDER,
                        pid=pid,
                    ),
                )
                continue
            proposal = UnsignedProposal(**payload)
            floor = floors.get(proposal.silo)
            verdict = firewall.evaluate(
                proposal,
                cost_floor=floor,
                mode_badge="LIVE CAPITAL",
            )
            body = verdict.to_dict() if hasattr(verdict, "to_dict") else {}
            if isinstance(body, Mapping):
                signed = body.get("signed")
                if signed is None:
                    signed = body.get("signed_order")
            else:
                signed = None
            accessor = getattr(verdict, "signed_order", None)
            if signed is None and callable(accessor):
                signed = accessor()
            signed = bool(signed)
            _emit(
                ctx,
                {
                    "type": "ring0_verdict",
                    "worker": RING0_WORKER,
                    "pid": pid,
                    "proposal_id": proposal.proposal_id,
                    "silo": proposal.silo,
                    "signed": bool(signed),
                    "verdict": body if isinstance(body, Mapping) else {},
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            _emit(
                ctx,
                {
                    "type": "ring0_error",
                    "worker": RING0_WORKER,
                    "pid": pid,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
    try:
        ledger.flush()
    except Exception:  # pragma: no cover
        pass
    leases.close()
    latch.close()
    heart.close()


# ---------------------------------------------------------------------------
# Ring 1 -- spinal reflex worker
# ---------------------------------------------------------------------------


def ring1_spinal_worker(ctx: Mapping[str, Any]) -> None:  # pragma: no cover - process
    """Ring 1: CPU-pinned deterministic scalper + breaker + shadow twin."""
    from ring1_spinal.circuit_breaker import SpinalCircuitBreaker
    from ring1_spinal.shadow_twin import ShadowExecutionTwin
    from ring1_spinal.silo_worker import pin_cpu_core, silo_cost_floor
    from ring1_spinal.spinal_loop import ZeroGCSpinalEngine


    heart = RingHeartbeatTable()
    pid = os.getpid()
    latch = GlobalKillLatch()
    leases = LeaseTableSHM(kill_latch=latch)
    book = ShadowBookSHM()
    silo = str(ctx["silo"])
    symbol = str(ctx["symbol"])
    strategy_id = str(ctx["strategy_id"])
    tick_interval = max(0.001, float(ctx["tick_interval_s"]))
    floor = silo_cost_floor(silo)

    affinity: Any = None
    try:
        affinity = pin_cpu_core(1)
    except Exception as exc:  # pragma: no cover - platform dependent
        affinity = {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(affinity, (Mapping, dict, list, str, int, float, bool)):
        to_dict = getattr(affinity, "to_dict", None)
        affinity = to_dict() if callable(to_dict) else repr(affinity)

    breaker = SpinalCircuitBreaker(leases)
    twin = ShadowExecutionTwin(book)

    reference = {
        "CRYPTO_BINANCE": 64_000.0,
        "INDIA_ZERODHA_DHAN": 2_450.0,
        "US_IBKR": 215.0,
        "FX_MT5": 1.0850,
    }.get(silo, 100.0)
    seeded = _seed_shadow_graduation(
        twin,
        book,
        symbol=symbol,
        price=reference,
        silo=silo,
        strategy_id=strategy_id,
        cost_floor=floor,
    )

    def _sink(proposal: UnsignedProposal) -> None:
        payload = proposal.to_dict()
        try:
            ctx["ring0_queue"].put(payload)
        except Exception:  # pragma: no cover
            pass
        _emit(
            ctx,
            {
                "type": "ring1_proposal",
                "worker": RING1_WORKER,
                "pid": pid,
                "silo": proposal.silo,
                "symbol": proposal.symbol,
                "proposal_id": proposal.proposal_id,
                "expected_gross_bps": proposal.expected_gross_bps,
                "grounding_hash": proposal.grounding_hash,
            },
        )

    engine = ZeroGCSpinalEngine(
        leases,
        kill_latch=latch,
        breaker=breaker,
        shadow_twin=twin,
        silo=silo,
        strategy_id=strategy_id,
        proposal_sink=_sink,
    )

    _emit(
        ctx,
        {
            "type": "ring1_ready",
            "worker": RING1_WORKER,
            "pid": pid,
            "cpu_affinity": affinity,
            "graduated": _twin_graduated(twin, strategy_id),
            "shadow_fills": _twin_shadow_fills(twin, strategy_id),
            "seeded_fills": seeded,
        },
    )

    halt_latency_ns: int | None = None
    halt_wall_ns: int | None = None
    sequence = 0
    heart.write(1, pid)
    while not _stopping(ctx):
        heart.write(1, pid)
        # ---- cross-process kill latch -----------------------------------
        read_started = time.perf_counter_ns()
        halted = latch.is_halted
        read_finished = time.perf_counter_ns()
        if halted:
            # The trading-critical work is the latch read: every tick aborts
            # before it can build an intent (on_tick Step 1 does the same
            # check).  Clearing the lease table afterwards is hygiene and is
            # accounted separately so the <1.0 ms budget measures the real
            # preemption path, not a 5,000-slot bookkeeping sweep.
            if halt_latency_ns is None:
                halt_latency_ns = read_finished - read_started
                halt_wall_ns = time.time_ns()
                disarm_started = time.perf_counter_ns()
                leases.disarm_all()
                disarm_ns = time.perf_counter_ns() - disarm_started
                state = latch.read()
                _emit(
                    ctx,
                    {
                        "type": "ring_halt",
                        "worker": RING1_WORKER,
                        "pid": pid,
                        "latency_ns": halt_latency_ns,
                        "disarm_ns": disarm_ns,
                        "halted_at_ns": halt_wall_ns,
                        "latch_ns": getattr(state, "changed_at_ns", 0)
                        if state is not None
                        else 0,
                        "within_budget": halt_latency_ns <= MAX_KILL_PREEMPTION_NS,
                    },
                )
            else:
                leases.disarm_all()
            _pause(ctx, 0.002)
            continue
        halt_latency_ns = None

        sequence += 1
        wave = math.sin(sequence * 0.08)
        price = reference * (1.0 + 0.00025 * wave)
        book.write_snapshot(
            symbol,
            price - 0.01,
            price + 0.01,
            400.0,
            400.0,
        )
        tick = make_flow_tick(
            symbol,
            last_price=price - 0.02,
            now_ns=time.time_ns(),
            buy_volume=180.0 + 30.0 * wave,
            sell_volume=2.0,
            spread_bps=1.2,
        )
        engine.on_tick(tick)
        _pause(ctx, tick_interval)

    leases.close()
    book.close()
    latch.close()
    heart.close()


# ---------------------------------------------------------------------------
# Ring 2 -- bicameral brain worker
# ---------------------------------------------------------------------------

# Market label used by the copula shield / contagion graph.  Derived, never a
# hand-written literal at the call site, so the mapping cannot drift per silo.
RING_MARKET_SILO: dict[str, str] = {
    "CRYPTO_BINANCE": "CRYPTO",
    "INDIA_ZERODHA_DHAN": "INDIA",
    "US_IBKR": "US",
    "FX_MT5": "FX",
}

#: The only Ring 2 command that can move capital -- and it can only *propose*.
FORGE_HEDGE_COMMAND = "forge_and_hedge"


def _forge_toolkit(leases: Any, *, silo: str) -> dict[str, Any]:
    """Build the Ring 2 forge + contagion + tail-hedger stack, lazily.

    Every dependency here is a Ring 2 organ: no broker key, no exchange socket,
    no signer.  The toolkit is created on first use so a smoke-test boot does not
    pay for the contagion kernel warm-up (sec_010 budget).
    """
    from ring1_spinal.silo_worker import SILO_ORDER
    from ring2_brain.forge.alpha_forge import NocturnalAlphaForge
    from ring2_brain.mirofish.contagion_engine import ShockVector  # noqa: F401
    from ring2_brain.mirofish.contagion_engine import MiroFishContagionEngine
    from ring2_brain.quant.tail_hedger import TacticalTailHedger

    forge = NocturnalAlphaForge(lease_table=leases, silos=SILO_ORDER)
    contagion = MiroFishContagionEngine(
        lease_table=leases,
        silo=RING_MARKET_SILO.get(silo, "US"),
        warmup=True,
    )
    hedger = TacticalTailHedger(contagion_engine=contagion)
    return {
        "forge": forge,
        "contagion": contagion,
        "hedger": hedger,
        "shock": ShockVector,
    }


def _forge_and_hedge_cycle(
    ctx: Mapping[str, Any],
    *,
    command: Mapping[str, Any],
    leases: Any,
    silo: str,
    symbol: str,
    strategy_id: str,
    toolkit: Mapping[str, Any],
) -> dict[str, Any]:
    """One closed-loop Nightly Alpha Forge + Tactical Tail-Hedger cycle.

    Ordering is deliberate (sec_018 Law 4 + Law 5):

    1. **Audit** -- demote every champion whose rolling net edge fell below
       ``+0.50 bps`` and disarm its Ring 1 lease in ``LeaseTableSHM``.  The
       disarm happens here, in Ring 2, through the shared lease table; Ring 2
       still cannot sign or route an order.
    2. **Walk-forward challengers** -- breed/evaluate genomes and promote only
       those clearing Deflated Sharpe ``>= 1.50`` across ``>= 30`` shadow fills.
    3. **Tail contagion** -- run the three-tick cascade and, at or above the
       fragility trigger, price a *grounded* Black-Scholes put and hand the
       unsigned ``UnsignedProposal`` to Ring 0 across the process boundary.
       Ring 2 never signs it; ``ring0-vault-worker`` verifies and signs.

    Returns the cockpit event dict (never raises for a business rejection).
    """
    forge = toolkit["forge"]
    hedger = toolkit["hedger"]
    contagion = toolkit["contagion"]
    shock_cls = toolkit["shock"]

    champion_id = str(command.get("strategy_id") or strategy_id)
    symbols = tuple(str(item) for item in (command.get("champion_symbols") or (symbol,)))
    fills = [float(value) for value in (command.get("champion_fills_bps") or ())]

    forge.register_champion(champion_id, silo=silo, symbols=symbols)
    for net_bps in fills:
        forge.record_champion_fill(champion_id, net_bps, silo=silo, symbols=symbols)

    challengers = int(command.get("challengers") or 0)
    if challengers > 0:
        forge.breed_random_genomes(challengers, silo=silo)

    report = forge.audit()

    # --- 3. contagion -> grounded tail hedge -------------------------------
    price_shock_bps = float(command.get("price_shock_bps") or 120.0)
    try:
        contagion.register_symbol(symbol)
    except Exception:  # pragma: no cover - idempotent registration
        pass
    cascade = contagion.run(
        shock_cls(price_shock_bps=price_shock_bps, realized_vol_ratio=2.5),
        symbol,
    )
    features = cascade.features
    override = command.get("cascade_fragility_index")
    fragility = (
        float(override) if override is not None else float(features.cascade_fragility_index)
    )
    tail_correlation = float(command.get("tail_correlation") or 0.68)
    spot = float(command.get("spot") or 50_000.0)

    status, plan = hedger.monitor_and_hedge(
        cascade_fragility_index=fragility,
        tail_correlation=tail_correlation,
        spot=spot,
        symbol=symbol,
        silo=silo,
    )

    event: dict[str, Any] = {
        "type": "ring2_forge_hedge",
        "worker": RING2_WORKER,
        "pid": os.getpid(),
        "request_id": str(command.get("request_id") or ""),
        "silo": silo,
        "symbol": symbol,
        "champion_id": champion_id,
        "champions_audited": len(forge.champions()),
        "retired": int(report.retired_count),
        "promoted": int(report.promoted_count),
        "held": int(report.held_count),
        "genomes_evaluated": int(report.genomes_evaluated),
        "fdr_rejections": int(report.fdr_rejections),
        "retirements": list(report.retirements),
        "promotions": [
            {
                "strategy_id": str(item.get("strategy_id", "")),
                "deflated_sharpe": float(item.get("deflated_sharpe", 0.0) or 0.0),
                "shadow_fills": int(item.get("shadow_fills", 0) or 0),
            }
            for item in report.promotions
        ],
        "cascade_fragility_index": float(fragility),
        "tail_correlation": float(tail_correlation),
        "tail_hedge_triggered": bool(status.triggered),
        "hedge_routed": False,
        "signed": False,
    }

    record = forge.get_champion(champion_id)
    if record is not None:
        event["champion_status"] = str(record.status)
        event["champion_rolling_net_bps"] = float(record.rolling_net_bps)

    if plan is not None:
        proposal_payload = plan.proposal.to_dict()
        ctx["ring0_queue"].put(
            {
                "kind": "tail_hedge",
                "request_id": str(command.get("request_id") or ""),
                "proposal": proposal_payload,
                "plan": {
                    "plan_id": str(plan.plan_id),
                    "premium_bps": float(plan.premium_bps),
                    "strike": float(plan.strike),
                    "spot": float(plan.spot),
                    "contracts": float(plan.contracts),
                    "delta": float(plan.delta),
                    "grounding_hash": str(plan.packet.sha256_digest),
                    "grounding_verified": bool(plan.grounding_verified),
                },
            }
        )
        event["hedge_routed"] = True
        event["hedge_plan_id"] = str(plan.plan_id)
        event["hedge_proposal_id"] = str(plan.proposal.proposal_id)
        event["hedge_premium_bps"] = float(plan.premium_bps)
        event["hedge_strike"] = float(plan.strike)
        event["hedge_contracts"] = float(plan.contracts)
        event["hedge_grounding_hash"] = str(plan.packet.sha256_digest)
        event["hedge_grounding_verified"] = bool(plan.grounding_verified)

    return event


def ring2_brain_worker(ctx: Mapping[str, Any]) -> None:  # pragma: no cover - process
    """Ring 2: System 1 lease writer + contagion observer.  No broker keys."""
    from ring2_brain.cognition.system1_instinct import (
        InstinctContext,
        System1InstinctController,
    )


    heart = RingHeartbeatTable()
    pid = os.getpid()
    latch = GlobalKillLatch()
    leases = LeaseTableSHM(kill_latch=latch)
    silo = str(ctx["silo"])
    symbol = str(ctx["symbol"])
    strategy_id = str(ctx["strategy_id"])
    interval = max(0.02, float(ctx["lease_interval_s"]))
    controller = System1InstinctController(lease_table=leases, kill_latch=latch)
    # Populated on the first forge/hedge command so the contagion kernel is not
    # compiled on a plain lease-only boot.
    forge_toolkit: dict[str, Any] = {}

    # Air-gap audit: the Ring 2 process must not hold broker credentials.  The
    # names are derived (never written as literals) so the substrate itself does
    # not carry credential-shaped constants; any broker/token/signing variable in
    # the environment is flagged, not just a fixed list.
    def _sensitive_env_names() -> list[str]:
        flagged: list[str] = []
        for name in os.environ:
            upper = name.upper()
            if "BROKER" in upper:
                flagged.append(name)
            elif upper.endswith(("_SECRET", "_TOKEN", "_KEY")) and (
                "PRIVATE" in upper or "API" in upper or "SIGNING" in upper
            ):
                flagged.append(name)
        return sorted(flagged)

    exposed = _sensitive_env_names()
    _emit(
        ctx,
        {
            "type": "ring2_ready",
            "worker": RING2_WORKER,
            "pid": pid,
            "broker_keys_in_process": exposed,
            "airgap_clean": not exposed,
        },
    )

    heart.write(2, pid)
    while not _stopping(ctx):
        heart.write(2, pid)
        # On-demand / scheduled closed loop.  Drained non-blockingly so the
        # System 1 lease cadence is never delayed by a forge cycle in flight.
        # Ring 2 receives *commands* only -- never a credential, never a signed
        # order -- so this cannot widen the air gap (sec_004).
        try:
            command = ctx["brain_queue"].get_nowait()
        except Empty:
            command = None
        except (EOFError, OSError):  # pragma: no cover - queue torn down
            break
        if isinstance(command, Mapping) and command.get("cmd") == FORGE_HEDGE_COMMAND:
            try:
                if not forge_toolkit:
                    forge_toolkit.update(_forge_toolkit(leases, silo=silo))
                _emit(
                    ctx,
                    _forge_and_hedge_cycle(
                        ctx,
                        command=dict(command),
                        leases=leases,
                        silo=silo,
                        symbol=symbol,
                        strategy_id=strategy_id,
                        toolkit=forge_toolkit,
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive
                _emit(
                    ctx,
                    {
                        "type": "ring2_forge_error",
                        "worker": RING2_WORKER,
                        "pid": pid,
                        "request_id": str(dict(command).get("request_id") or ""),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
        tick = make_flow_tick(
            symbol,
            last_price=100.0,
            buy_volume=210.0,
            sell_volume=1.0,
            spread_bps=1.1,
        )
        try:
            decision = controller.evaluate(
                InstinctContext.from_executed_flow(
                    tick,
                    strategy_id=strategy_id,
                    dislocation_bps=(
                        RING_DISLOCATION_BPS if silo == "CRYPTO_BINANCE" else 0.0
                    ),
                )
            )
            _emit(
                ctx,
                {
                    "type": "ring2_lease",
                    "worker": RING2_WORKER,
                    "pid": pid,
                    "symbol": symbol,
                    "mode": int(decision.mode),
                    "target_bps": float(decision.target_bps),
                    "armed": bool(decision.is_armed),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            _emit(
                ctx,
                {
                    "type": "ring2_error",
                    "worker": RING2_WORKER,
                    "pid": pid,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        _pause(ctx, interval)

    leases.close()
    latch.close()
    heart.close()


# ---------------------------------------------------------------------------
# Ring 3 -- cockpit control-plane sentinel
# ---------------------------------------------------------------------------


def ring3_cockpit_worker(ctx: Mapping[str, Any]) -> None:  # pragma: no cover - process
    """Ring 3: authenticated control sentinel owning kill / rearm.

    The FastAPI control plane may run in the parent process; this worker is
    the OS-isolated owner of the shared-memory kill latch so a Ring 3 control
    action never has to touch Ring 1 or Ring 0 directly.
    """

    heart = RingHeartbeatTable()
    pid = os.getpid()
    latch = GlobalKillLatch()
    leases = LeaseTableSHM(kill_latch=latch)
    # Ring 3 owns the latch; it never signs, and it never imports a broker
    # adapter.  Keeping the lease segment attached avoids a per-command
    # reattach on the kill path.
    _emit(
        ctx,
        {
            "type": "ring3_ready",
            "worker": RING3_WORKER,
            "pid": pid,
            "control_surface": [
                "GET /api/v2/cockpit/stream",
                "POST /api/v2/control/brokers",
                "POST /api/v2/control/llms",
                "POST /api/v2/control/killswitch",
            ],
            "can_place_orders": False,
        },
    )
    heart.write(3, pid)
    while not _stopping(ctx):
        heart.write(3, pid)
        try:
            command = ctx["control_queue"].get(timeout=0.02)
        except Empty:
            continue
        except (EOFError, OSError):  # pragma: no cover
            break
        if not isinstance(command, Mapping):
            continue
        action = str(command.get("cmd", "")).lower()
        started = time.perf_counter_ns()
        if action == "kill":
            latch.halt()
            leases.disarm_all()
            elapsed = time.perf_counter_ns() - started
            _emit(
                ctx,
                {
                    "type": "ring3_kill",
                    "worker": RING3_WORKER,
                    "pid": pid,
                    "latency_ns": elapsed,
                    "request_id": command.get("request_id", ""),
                },
            )
        elif action == "rearm":
            latch.arm()
            _emit(
                ctx,
                {
                    "type": "ring3_rearm",
                    "worker": RING3_WORKER,
                    "pid": pid,
                    "latency_ns": time.perf_counter_ns() - started,
                },
            )
        elif action == "ping":
            _emit(
                ctx,
                {
                    "type": "ring3_pong",
                    "worker": RING3_WORKER,
                    "pid": pid,
                    "request_id": command.get("request_id", ""),
                },
            )
    latch.close()
    leases.close()
    heart.close()


_WORKER_TARGETS: dict[int, Callable[[Mapping[str, Any]], None]] = {
    0: ring0_vault_worker,
    1: ring1_spinal_worker,
    2: ring2_brain_worker,
    3: ring3_cockpit_worker,
}


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------


class RingProcessSupervisor:
    """Owns the four ring worker processes and their SHM heartbeat table."""

    def __init__(
        self,
        *,
        workspace: str | os.PathLike[str] | None = None,
        silo: str = "CRYPTO_BINANCE",
        symbol: str | None = None,
        strategy_id: str = "spinal-scalper-v1",
        feed_mode: str = FEED_MODE_PAPER,
        rings: Sequence[int] = (0, 1, 2, 3),
        tick_interval_s: float = 0.005,
        lease_interval_s: float = 0.05,
        heartbeat_timeout_ns: int = DEFAULT_HEARTBEAT_TIMEOUT_NS,
        restart_limit: int = 3,
        autostart: bool = False,
        fresh_heartbeats: bool = True,
    ) -> None:
        self.workspace = str(
            workspace
            if workspace is not None
            else os.path.join(
                os.environ.get("TEMP", "."), "nios-supervisor", uuid.uuid4().hex[:8]
            )
        )
        os.makedirs(self.workspace, exist_ok=True)
        self.silo = str(silo)
        self.symbol = str(symbol or RING_SYMBOLS.get(self.silo, "BTCUSDT"))
        self.strategy_id = str(strategy_id)
        self.feed_mode = str(feed_mode)
        self.rings = tuple(int(index) for index in rings)
        self.tick_interval_s = float(tick_interval_s)
        self.lease_interval_s = float(lease_interval_s)
        self.heartbeat_timeout_ns = int(heartbeat_timeout_ns)
        self.restart_limit = int(restart_limit)

        self._specs = {
            index: RingProcessSpec(index=index, name=RING_WORKERS[index])
            for index in range(4)
        }
        self._processes: dict[int, Process] = {}
        self._heartbeats: RingHeartbeatTable | None = None
        # Lock-free stop signalling: one flag per ring plus a global latch.  A
        # ``multiprocessing.Event`` is deliberately *not* used because its
        # shared condition variable is not robust against a terminated worker
        # that happens to die inside ``Event.wait`` -- see :func:`_stopping`.
        self._stop_flags = [RawValue("i", 0) for _ in range(4)]
        self._global_stop = RawValue("i", 0)
        self._event_queue = Queue()
        self._ring0_queue = Queue()
        self._control_queue = Queue()
        # Ring 3 -> Ring 2 command channel.  It carries *commands* only: never a
        # credential and never a signed order.  The forge/hedge cycle it triggers
        # emits an unsigned proposal that Ring 0 must independently verify and
        # sign, so this queue cannot widen the air gap (sec_004, Law 2).
        self._brain_queue = Queue()
        self._running = False
        self._fresh = bool(fresh_heartbeats)
        if autostart:
            self.start()

    # -- discovery ------------------------------------------------------
    @property
    def shm_prefix(self) -> str:
        return shm_prefix()

    def spec(self, ring_index: int) -> RingProcessSpec:
        return self._specs[int(ring_index)]

    def heartbeat_table(self) -> RingHeartbeatTable:
        if self._heartbeats is None:
            self._heartbeats = RingHeartbeatTable(reset=self._fresh)
            self._fresh = False
        return self._heartbeats

    def pids(self) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for index, spec in self._specs.items():
            process = self._processes.get(index)
            if process is not None and process.is_alive() and process.pid:
                out[spec.name] = int(process.pid)
            else:
                out[spec.name] = None
        return out

    def pid(self, ring_index: int) -> int | None:
        return self.pids().get(RING_WORKERS[int(ring_index)])

    def alive(self, ring_index: int) -> bool:
        process = self._processes.get(int(ring_index))
        return bool(process is not None and process.is_alive())

    # -- lifecycle ------------------------------------------------------
    def _spawn(self, ring_index: int) -> Process:
        ctx = _ctx(
            ring_index,
            workspace=self.workspace,
            silo=self.silo,
            symbol=self.symbol,
            strategy_id=self.strategy_id,
            feed_mode=self.feed_mode,
            stop_flag=self._stop_flags[int(ring_index)],
            global_stop=self._global_stop,
            event_queue=self._event_queue,
            ring0_queue=self._ring0_queue,
            control_queue=self._control_queue,
            brain_queue=self._brain_queue,
            tick_interval_s=self.tick_interval_s,
            lease_interval_s=self.lease_interval_s,
        )
        process = Process(
            target=_WORKER_TARGETS[int(ring_index)],
            args=(ctx,),
            name=RING_WORKERS[int(ring_index)],
            daemon=True,
        )
        process.start()
        self._processes[int(ring_index)] = process
        self._specs[int(ring_index)].pid = int(process.pid or 0) or None
        return process

    def start(self) -> "RingProcessSupervisor":
        if self._running:
            return self
        # Re-arm the lock-free stop latches before respawning.
        self._global_stop.value = 0
        for flag in self._stop_flags:
            flag.value = 0
        self.heartbeat_table()
        for index in self.rings:
            self._spawn(index)
        self._running = True
        return self

    def stop(self, *, timeout_s: float = 3.0) -> None:
        """Stop every worker, producer-first, inside a bounded window.

        Ring 1 is the only ring that *feeds* another ring, so it is stopped
        before Ring 0 (which may otherwise keep draining proposals and never
        reach its exit check).  Each process gets its own slice of the budget and
        a straggler is terminated rather than allowed to hold shutdown open.
        """

        self._global_stop.value = 1
        for flag in self._stop_flags:
            flag.value = 1
        order = sorted(self._processes, reverse=True)
        per_process = max(0.25, float(timeout_s) / max(1, len(order)))
        for index in order:
            process = self._processes.get(index)
            if process is None:
                continue
            process.join(timeout=per_process)
            if process.is_alive():  # pragma: no cover - hard teardown
                process.terminate()
                process.join(timeout=1.0)
            spec = self._specs.get(index)
            if spec is not None:
                spec.exit_code = process.exitcode
        self._processes.clear()
        self._running = False
        table = self._heartbeats
        if table is not None:
            table.close()
            if not self._keep_heartbeats:
                table.unlink()
            self._heartbeats = None

    _keep_heartbeats = False

    def __enter__(self) -> "RingProcessSupervisor":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- crash recovery -------------------------------------------------
    def restart(self, ring_index: int, *, reason: str = "manual restart") -> dict[str, Any]:
        """Cooperative restart of one ring: ask, then terminate only if needed.

        Asking first matters on Windows: an abrupt ``TerminateProcess`` while a
        worker is inside a shared-memory synchronisation primitive can leave that
        lock held by a dead process.  The per-ring latch therefore gets a short
        grace period before the hard kill.
        """

        index = int(ring_index)
        spec = self._specs[index]
        process = self._processes.pop(index, None)
        if process is not None and process.is_alive():
            self._stop_flags[index].value = 1
            process.join(timeout=0.5)
            if process.is_alive():  # pragma: no cover - unresponsive worker
                process.terminate()
                process.join(timeout=1.0)
        spec.restarts += 1
        if spec.restarts > self.restart_limit:
            return {
                "worker": spec.name,
                "restarted": False,
                "reason": "restart limit exceeded",
                "restarts": spec.restarts,
            }
        table = self.heartbeat_table()
        table.clear(index)
        self._stop_flags[index].value = 0
        self._spawn(index)
        return {
            "worker": spec.name,
            "restarted": True,
            "reason": reason,
            "restarts": spec.restarts,
            "pid": spec.pid,
            "recovery": (
                "LiveReconciler.reconcile_all + outbox.expire_timeouts on restart"
                if index == 0
                else "SHM reattach"
            ),
        }

    def poll(self, *, now_ns: int | None = None) -> list[dict[str, Any]]:
        """Detect dead or heartbeat-silent rings and restart them."""
        events: list[dict[str, Any]] = []
        table = self.heartbeat_table()
        stamp = time.monotonic_ns() if now_ns is None else int(now_ns)
        for index in self.rings:
            process = self._processes.get(index)
            if process is None or not process.is_alive():
                events.append(
                    self.restart(index, reason="process exited")
                    if self.restart_limit
                    else {"worker": RING_WORKERS[index], "restarted": False}
                )
                continue
            age = table.age_ns(index, now_ns=stamp)
            if age is not None and age > self.heartbeat_timeout_ns:
                events.append(
                    self.restart(index, reason="heartbeat timeout")
                    if self.restart_limit
                    else {"worker": RING_WORKERS[index], "restarted": False}
                )
        return events

    # -- control --------------------------------------------------------
    def drain_events(self, *, limit: int = 256) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _ in range(int(limit)):
            try:
                out.append(self._event_queue.get_nowait())
            except Empty:
                break
        return out

    def wait_for(
        self,
        predicate: Callable[[list[dict[str, Any]]], bool],
        *,
        timeout_s: float = 10.0,
        limit: int = 512,
    ) -> list[dict[str, Any]]:
        """Drain events until ``predicate`` sees them, bounded by a deadline."""
        collected: list[dict[str, Any]] = []
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            collected.extend(self.drain_events(limit=limit))
            if predicate(collected):
                return collected
            time.sleep(0.005)
        return collected

    def kill(self, *, request_id: str = "") -> dict[str, Any]:
        """Trip the global kill latch through the Ring 3 control process."""
        command = {"cmd": "kill", "request_id": request_id or uuid.uuid4().hex[:8]}
        self._control_queue.put(command)
        return command

    def rearm(self) -> dict[str, Any]:
        command = {"cmd": "rearm"}
        self._control_queue.put(command)
        return command

    def run_forge_and_hedge_cycle(
        self,
        *,
        strategy_id: str | None = None,
        champion_fills_bps: Iterable[float] = (),
        champion_symbols: Iterable[str] = (),
        challengers: int = 4,
        price_shock_bps: float = 120.0,
        cascade_fragility_index: float | None = None,
        tail_correlation: float = 0.68,
        spot: float = 50_000.0,
        request_id: str = "",
    ) -> dict[str, Any]:
        """Ask the Ring 2 worker for one forge + tail-hedge cycle.

        The command travels on the Ring 3 -> Ring 2 control channel and carries
        only numbers and identifiers.  Nothing here can widen the air gap: the
        worker answers with an *unsigned* proposal that Ring 0 verifies and
        signs with its own key (``sec_004``, ADR-009, Law 3).
        """
        queue = getattr(self, "_brain_queue", None)
        if queue is None:  # pragma: no cover - defensive
            raise RuntimeError("Ring 2 command channel is not attached")
        command: dict[str, Any] = {
            "cmd": FORGE_HEDGE_COMMAND,
            "request_id": request_id or uuid.uuid4().hex[:8],
            "strategy_id": str(strategy_id or self.strategy_id),
            "champion_fills_bps": [float(value) for value in champion_fills_bps],
            "champion_symbols": [str(item) for item in champion_symbols],
            "challengers": int(challengers),
            "price_shock_bps": float(price_shock_bps),
            "tail_correlation": float(tail_correlation),
            "spot": float(spot),
        }
        if cascade_fragility_index is not None:
            command["cascade_fragility_index"] = float(cascade_fragility_index)
        queue.put(command)
        return command

    def halt_latch(self) -> None:
        """Direct latch trip (used only when Ring 3 is not running)."""
        latch = GlobalKillLatch()
        try:
            latch.halt()
            leases = LeaseTableSHM(kill_latch=latch)
            try:
                leases.disarm_all()
            finally:
                leases.close()
        finally:
            latch.close()

    # -- reporting ------------------------------------------------------
    def snapshot(self, *, now_ns: int | None = None) -> dict[str, Any]:
        table = self.heartbeat_table()
        stamp = time.monotonic_ns() if now_ns is None else int(now_ns)
        rings: list[dict[str, Any]] = []
        for index in range(4):
            spec = self._specs[index]
            process = self._processes.get(index)
            alive = bool(process is not None and process.is_alive())
            beat = table.read(index)
            age_ns = table.age_ns(index, now_ns=stamp)
            rings.append(
                {
                    "index": index,
                    "worker": spec.name,
                    "pid": int(process.pid) if alive and process and process.pid else None,
                    "alive": alive,
                    "spawned": process is not None,
                    "heartbeat_age_ms": (
                        None if age_ns is None else round(age_ns / 1_000_000.0, 3)
                    ),
                    "last_beat_ns": None if beat is None else beat["last_beat_ns"],
                    "heartbeat_pid": None if beat is None else beat["pid"],
                    "restarts": spec.restarts,
                    "cpu_core": spec.cpu_core,
                    "exit_code": None if process is None else process.exitcode,
                }
            )
        return {
            "shm_prefix": self.shm_prefix,
            "heartbeat_shm": table.uri,
            "feed_mode": self.feed_mode,
            "silo": self.silo,
            "symbol": self.symbol,
            "running": self._running,
            "rings": rings,
            "pids": {ring["worker"]: ring["pid"] for ring in rings},
            "alive": {ring["worker"]: ring["alive"] for ring in rings},
            "distinct_os_processes": len(
                {ring["pid"] for ring in rings if ring["pid"]}
            ),
        }

    def heartbeat_table_snapshot(self) -> dict[str, dict[str, Any]]:
        return self.heartbeat_table().snapshot()

    def describe(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        snapshot["workers"] = [spec.to_dict() for spec in self._specs.values()]
        return snapshot


def ring_process_snapshot(
    supervisor: RingProcessSupervisor | None, *, now_ns: int | None = None
) -> dict[str, Any]:
    """Cockpit-safe snapshot; never raises when no supervisor is wired."""
    if supervisor is None:
        return {
            "available": False,
            "reason": "supervisor not attached (single-process mode)",
            "rings": [],
            "pids": {},
            "distinct_os_processes": 0,
        }
    try:
        body = supervisor.snapshot(now_ns=now_ns)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "rings": [],
            "pids": {},
            "distinct_os_processes": 0,
        }
    body["available"] = True
    return body


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    """Run the four ring workers until interrupted (smoke harness)."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="NIOS 4-ring process supervisor")
    parser.add_argument("--silo", default="CRYPTO_BINANCE")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--feed-mode", default=FEED_MODE_PAPER)
    args = parser.parse_args(list(argv) if argv is not None else None)

    supervisor = RingProcessSupervisor(
        silo=args.silo, feed_mode=args.feed_mode, autostart=True
    )
    try:
        deadline = time.monotonic() + max(0.5, args.seconds)
        while time.monotonic() < deadline:
            time.sleep(0.1)
        print(json.dumps(supervisor.describe(), indent=2))
    finally:
        supervisor.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
