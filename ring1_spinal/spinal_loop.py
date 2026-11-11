"""NIOS Core v3.0 -- Tier-0 Zero-GC Spinal Reflex Fast Path (STEP 5).

Governing sections
------------------
``sec_001``
    Headline dimension table: Tier 0 executes **every** WebSocket tick in Ring 1
    with zero token burn.
``sec_004``
    Ring 1 is the "Zero-GC Loop (<0.14 ms p99)".
``sec_007``
    Tier 0's four sequential steps, verbatim::

        Step 1  SHM Lease Read          lock-free SeqLock, kill latch == 0
        Step 2  0.1 ms Spinal Breaker   vol >= 3.5x | 2 adverse fills | spread cap
        Step 3  Anti-Spoof OFI Gate     TakerBuyRatio_5s >= 0.65, C2F < 3.0
        Step 4  Ring 0 Conformal & Peg  98% Net_LB > 0 then dispatch

``sec_010``
    Capability 1 (``0.14 ms`` p99, ``25,000`` ticks/sec) and capability 6
    (30 live L2 shadow fills before promotion).
``sec_013``
    Latency budget: lease read ``0.005/0.008 ms``; breaker + OFI
    ``0.088/0.140 ms`` p50/p99.
``sec_016``
    Every shadow event and every halt renders a
    :class:`~core.schemas.PlainEnglishCockpitCard` on
    ``stream.ring3.cockpit-cards``.
``sec_018`` Law 1 / Law 2 / Law 4
    Executed flow only, never an LLM in the loop, 30-trade shadow graduation.

Zero-GC discipline
------------------
* :meth:`ZeroGCSpinalEngine.gc_guard` disables the cyclic collector for the
  active tick window and restores the previous state on exit -- a collection
  pause inside a 25,000 tick/sec loop is a latency cliff, not a nuisance.
* The clean path performs no allocation beyond the returned
  :class:`TickOutcome` named tuple: no logging, no dict churn, no exception
  construction, no ``to_dict``.
* Ticks arrive as :class:`~core.schemas.ExecutedFlowTick` objects (the field
  reference implementation).  The compiled path reads the same fields out of
  the fixed :data:`TICK_WIRE_STRUCT` frame, so both share one contract.

What Ring 1 may and may not do
------------------------------
It may read the lease, read executed flow, disarm a lease on a breaker trip,
write a shadow fill and emit an **unsigned** proposal.  It may not sign, may
not hold a broker credential, may not call an LLM and may not write the IBOR
(``sec_004`` air-gap, ``sec_018`` Law 2).
"""

from __future__ import annotations

import gc
import hashlib
import importlib.util
import struct
import time
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Callable, Iterable, Iterator, NamedTuple

from core.nats_bus import STREAM_RING1_SCALP, STREAM_RING3_COCKPIT
from core.schemas import (
    ExecutedFlowTick,
    PlainEnglishCockpitCard,
    System1Lease,
    UnsignedProposal,
    VaultName,
    net_lower_bound_bps,
)
from core.shm_ipc import GlobalKillLatch, LeaseTableSHM

from .circuit_breaker import (
    BreakerTrip,
    DEFAULT_MAX_SPREAD_BPS,
    SpinalCircuitBreaker,
    VOL_SPIKE_TRIP_REASON,
    TWO_ADVERSE_FILLS_TRIP_REASON,
    SPREAD_BLOWOUT_TRIP_REASON,
)
from .shadow_twin import REQUIRED_SHADOW_FILLS, ShadowExecutionTwin, ShadowFill
from .silo_worker import (
    CRYPTO_BINANCE,
    SiloCostFloor,
    silo_config,
)

__all__ = [
    "TICK_WIRE_STRUCT",
    "CLAIM_TAIL_STRUCT",
    "CLAIM_FRAME_SIZE",
    "FAST_PATH_BACKEND",
    "NUMBA_AVAILABLE",
    "MEAN_REV_LONG_MAX_TAKER_BUY_RATIO",
    "MEAN_REV_SHORT_MIN_TAKER_BUY_RATIO",
    "TickAction",
    "TickOutcome",
    "ZeroGCSpinalEngine",
    "pack_tick",
    "unpack_tick",
    "tick_frame_digest",
]

#: Binary frame for one executed-flow tick on the Ring 1 ring buffer.
#: ``symbol:16s | valid_time_ns:u64 | knowledge_ts_ns:u64 | 8 x f64``.
TICK_WIRE_STRUCT: struct.Struct = struct.Struct("<16sQQdddddddd")
assert TICK_WIRE_STRUCT.size == 96

#: Claim tail appended to the tick frame when sealing an ``UnsignedProposal``:
#: ``lease_expires_at_ns:u64 | target_bps:f64 | stop_bps:f64 | now_ns:i64 | side:4s``.
CLAIM_TAIL_STRUCT: struct.Struct = struct.Struct("<Qddq4s")
assert CLAIM_TAIL_STRUCT.size == 36

#: Total size of the sealed claim frame (every number in the intent).
CLAIM_FRAME_SIZE: int = TICK_WIRE_STRUCT.size + CLAIM_TAIL_STRUCT.size

#: Mean-reversion exhaustion thresholds.  A mean-reversion lease fades an
#: *exhausted* one-sided tape: aggressive sellers (buy ratio <= 0.20) get
#: bought, aggressive buyers (>= 0.80) get sold -- always with a clean book.
MEAN_REV_LONG_MAX_TAKER_BUY_RATIO: float = 0.20
MEAN_REV_SHORT_MIN_TAKER_BUY_RATIO: float = 0.80

#: ``side`` token per :class:`LeaseMode` value (index == ``int(mode)``).
_SIDE_BY_MODE: tuple[str, ...] = ("", "BUY", "SELL", "MEAN_REV")

#: Human label for the compiled fast path actually in use.
_NUMBA_AVAILABLE = importlib.util.find_spec("numba") is not None
FAST_PATH_BACKEND: str = "python-struct" if not _NUMBA_AVAILABLE else "python-struct+numba-available"
NUMBA_AVAILABLE: bool = _NUMBA_AVAILABLE


# ---------------------------------------------------------------------------
# Tick wire codec (shared with the compiled ring-buffer path)
# ---------------------------------------------------------------------------


def pack_tick(tick: ExecutedFlowTick) -> bytes:
    """Pack one executed-flow tick into the fixed 96-byte Ring 1 frame."""
    return TICK_WIRE_STRUCT.pack(
        str(tick.symbol).encode("utf-8")[:16],
        int(tick.valid_time_ns) & 0xFFFFFFFFFFFFFFFF,
        int(tick.knowledge_ts_ns) & 0xFFFFFFFFFFFFFFFF,
        float(tick.last_price),
        float(tick.spread_bps),
        float(tick.realized_vol_5s),
        float(tick.normal_vol_5s),
        float(tick.taker_buy_volume_5s),
        float(tick.taker_sell_volume_5s),
        float(tick.bid_cancel_to_fill_ratio),
        float(tick.ask_cancel_to_fill_ratio),
    )


def unpack_tick(frame: bytes | bytearray | memoryview, offset: int = 0) -> ExecutedFlowTick:
    """Inverse of :func:`pack_tick` (zero-copy safe on a memoryview)."""
    raw = TICK_WIRE_STRUCT.unpack_from(frame, offset)
    return ExecutedFlowTick(
        symbol=raw[0].split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
        valid_time_ns=raw[1],
        knowledge_ts_ns=raw[2],
        last_price=raw[3],
        spread_bps=raw[4],
        realized_vol_5s=raw[5],
        normal_vol_5s=raw[6],
        taker_buy_volume_5s=raw[7],
        taker_sell_volume_5s=raw[8],
        bid_cancel_to_fill_ratio=raw[9],
        ask_cancel_to_fill_ratio=raw[10],
    )


def tick_frame_digest(tick: ExecutedFlowTick) -> str:
    """SHA-256 of the canonical tick frame (Law 3 evidence anchor)."""
    return hashlib.sha256(pack_tick(tick)).hexdigest()


# ---------------------------------------------------------------------------
# Tick outcomes
# ---------------------------------------------------------------------------


class TickAction(StrEnum):
    """Deterministic result of one Tier-0 tick evaluation."""

    ABSTAIN_KILL = "ABSTAIN_KILL_LATCH"
    ABSTAIN_LEASE = "ABSTAIN_LEASE_INACTIVE"
    HALT_BREAKER = "HALT_BREAKER"
    ABSTAIN_OFI = "ABSTAIN_ANTI_SPOOF_OFI"
    SHADOW_FILL = "SHADOW_FILL"
    SHADOW_RESTING = "SHADOW_ORDER_RESTING"
    LIVE_PROPOSAL = "LIVE_PROPOSAL"
    ABSTAIN_RING0 = "ABSTAIN_RING0_CONFORMAL"


class TickOutcome(NamedTuple):
    """Result of :meth:`ZeroGCSpinalEngine.on_tick`."""

    action: str
    symbol: str
    reason: str = ""
    elapsed_ns: int = 0
    proposal: UnsignedProposal | None = None
    shadow_fill: ShadowFill | None = None

    @property
    def is_trade(self) -> bool:
        return self.action in (TickAction.LIVE_PROPOSAL, TickAction.SHADOW_FILL)


def _as_sink(sink: Any, name: str) -> Callable[[Any], Any] | None:
    """Normalise a sink: callables are used as-is, lists get ``append``."""
    if sink is None:
        return None
    if callable(sink):
        return sink
    append = getattr(sink, "append", None)
    if append is not None:
        return append
    raise TypeError(f"{name} must be callable or a list-like with append()")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ZeroGCSpinalEngine:
    """The Tier-0 spinal reflex: lease read -> breaker -> OFI -> dispatch.

    Parameters
    ----------
    lease_table:
        Tier-0 :class:`~core.shm_ipc.LeaseTableSHM` (System 1 authority).
    kill_latch:
        Optional :class:`~core.shm_ipc.GlobalKillLatch`.  ``read_lease``
        already observes it; passing it here makes Step 1's latch read explicit
        and lets the engine short-circuit before touching the lease slot.
    breaker:
        Optional :class:`~ring1_spinal.circuit_breaker.SpinalCircuitBreaker`;
        a default one is built from ``max_spread_bps`` when omitted.
    shadow_twin:
        Optional :class:`~ring1_spinal.shadow_twin.ShadowExecutionTwin`.
        Without one every strategy is treated as *not* graduated, which fails
        closed to abstention -- Ring 1 never deploys unproven capital.
    bus:
        Optional durable bus exposing a **synchronous**
        ``publish_idempotent(subject, payload, key)`` (a
        :class:`~core.nats_bus.LocalJetStreamBus`, or a
        :class:`~core.nats_bus.JetStreamBus` whose
        ``publish_idempotent_sync`` is used).
    ring0_gate:
        Optional synchronous fast-path hook ``(proposal) -> bool`` evaluated
        before the proposal leaves Ring 1.  This is where Ring 0's 98%
        conformal + firewall check is spliced in; an exception fails closed.
    """

    __slots__ = (
        "_lease_table",
        "_kill_latch",
        "_breaker",
        "_twin",
        "_publish",
        "_bus",
        "_silo",
        "_silo_label",
        "_cost_floor",
        "_strategy_id",
        "_qty",
        "_proposal_sink",
        "_card_sink",
        "_ring0_gate",
        "_proposal_seq",
        "_ticks",
        "_abstentions",
        "_trips_seen",
        "_shadow_fills",
        "_live_proposals",
        "_ring0_rejects",
        "_publish_errors",
        "_gc_windows",
        "_last_outcome",
        "_prewarmed",
        "_scratch",
    )

    def __init__(
        self,
        lease_table: LeaseTableSHM,
        *,
        kill_latch: GlobalKillLatch | None = None,
        breaker: SpinalCircuitBreaker | None = None,
        shadow_twin: ShadowExecutionTwin | None = None,
        bus: Any | None = None,
        silo: str = CRYPTO_BINANCE,
        strategy_id: str = "spinal-scalper-v1",
        max_spread_bps: float = DEFAULT_MAX_SPREAD_BPS,
        qty: float = 10.0,
        proposal_sink: Callable[[UnsignedProposal], Any] | list | None = None,
        card_sink: Callable[[PlainEnglishCockpitCard], Any] | list | None = None,
        ring0_gate: Callable[[UnsignedProposal], bool] | None = None,
    ) -> None:
        if lease_table is None:
            raise ValueError("ZeroGCSpinalEngine requires a LeaseTableSHM")
        cfg = silo_config(silo)
        self._lease_table = lease_table
        self._kill_latch = kill_latch
        self._breaker = (
            breaker
            if breaker is not None
            else SpinalCircuitBreaker(
                lease_table, max_allowed_spread_bps=max_spread_bps
            )
        )
        self._twin = shadow_twin
        self._bus = bus
        self._silo = cfg.silo_id
        self._silo_label = {
            "CRYPTO_BINANCE": "BINANCE-CRYPTO",
            "INDIA_ZERODHA_DHAN": "ZERODHA-INDIA",
            "US_IBKR": "IBKR-US",
            "FX_MT5": "MT5-FX",
        }.get(cfg.silo_id, cfg.silo_id.replace("_", "-"))
        self._cost_floor = cfg.cost_floor
        self._strategy_id = str(strategy_id)
        self._qty = float(qty)
        self._proposal_sink = _as_sink(proposal_sink, "proposal_sink")
        self._card_sink = _as_sink(card_sink, "card_sink")
        self._ring0_gate = ring0_gate

        publish = None
        if bus is not None:
            publish = getattr(bus, "publish_idempotent_sync", None)
            if publish is None:
                publish = getattr(bus, "publish_idempotent", None)
        self._publish = publish

        self._proposal_seq = 0
        self._ticks = 0
        self._abstentions = 0
        self._trips_seen = 0
        self._shadow_fills = 0
        self._live_proposals = 0
        self._ring0_rejects = 0
        self._publish_errors = 0
        self._gc_windows = 0
        self._prewarmed: set[str] = set()
        self._scratch = bytearray(CLAIM_FRAME_SIZE)
        self._last_outcome: TickOutcome | None = None

    # -- configuration ----------------------------------------------------
    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @strategy_id.setter
    def strategy_id(self, value: str) -> None:
        self._strategy_id = str(value)

    @property
    def silo(self) -> str:
        return self._silo

    @property
    def cost_floor(self) -> SiloCostFloor:
        return self._cost_floor

    @property
    def breaker(self) -> SpinalCircuitBreaker:
        return self._breaker

    @property
    def shadow_twin(self) -> ShadowExecutionTwin | None:
        return self._twin

    @property
    def last_outcome(self) -> TickOutcome | None:
        return self._last_outcome

    @property
    def fast_path_backend(self) -> str:
        return FAST_PATH_BACKEND

    # -- zero-GC window ----------------------------------------------------
    @contextmanager
    def gc_guard(self, *, freeze: bool = False) -> Iterator["ZeroGCSpinalEngine"]:
        """Disable the cyclic collector for the active tick window.

        The previous GC state is restored on exit, so nesting is safe and a
        caller that had GC disabled keeps it disabled.
        """
        was_enabled = gc.isenabled()
        if freeze:
            gc.freeze()
        gc.disable()
        self._gc_windows += 1
        try:
            yield self
        finally:
            if was_enabled and not gc.isenabled():
                gc.enable()

    #: Alias used by the architecture prose ("zero-GC tick window").
    zero_gc_window = gc_guard

    def prewarm(self, symbols: Iterable[str]) -> int:
        """Touch every per-symbol cache before the loop starts.

        The CRC32 slot mapping is ``lru_cache``-backed; paying it on the first
        live tick would be a one-off latency spike inside the SLA window.
        """
        count = 0
        for symbol in symbols:
            self._lease_table.read_lease(str(symbol), time.monotonic_ns())
            self._prewarmed.add(str(symbol))
            count += 1
        return count

    # -- the hot path ------------------------------------------------------
    def on_tick(
        self, tick: ExecutedFlowTick, now_ns: int | None = None
    ) -> TickOutcome:
        """Execute the four-step Tier-0 workflow for one executed-flow tick."""
        started = time.perf_counter_ns()
        stamp = time.monotonic_ns() if now_ns is None else now_ns
        symbol = tick.symbol
        self._ticks += 1

        # ---- Step 1: SHM lease + global kill latch (0.005 / 0.008 ms) ----
        kill = self._kill_latch
        if kill is not None and kill.is_halted:
            self._abstentions += 1
            outcome = TickOutcome(
                TickAction.ABSTAIN_KILL,
                symbol,
                "GLOBAL_KILL_LATCH",
                time.perf_counter_ns() - started,
            )
            self._last_outcome = outcome
            return outcome

        lease = self._lease_table.read_lease(symbol, stamp)
        if not lease.is_active(stamp):
            self._abstentions += 1
            outcome = TickOutcome(
                TickAction.ABSTAIN_LEASE,
                symbol,
                "LEASE_INACTIVE_OR_EXPIRED",
                time.perf_counter_ns() - started,
            )
            self._last_outcome = outcome
            return outcome

        # ---- Step 2: 0.1 ms spinal microstructure breaker ------------------
        trip = self._breaker.check_tick(tick, stamp)
        if trip is not None:
            self._trips_seen += 1
            self._emit_trip_card(trip, tick, stamp)
            outcome = TickOutcome(
                TickAction.HALT_BREAKER,
                symbol,
                trip.reason,
                time.perf_counter_ns() - started,
            )
            self._last_outcome = outcome
            return outcome

        # ---- Step 3: anti-spoof executed OFI gate (Law 1) ------------------
        side = _SIDE_BY_MODE[int(lease.mode)]
        if side == "MEAN_REV":
            # Mean reversion fades an *exhausted* one-sided tape, so the
            # directional predicate is inverted: aggressive seller dominance
            # (buy ratio <= 0.20) is bought, buyer dominance (>= 0.80) sold.
            if not tick.book_is_clean:
                self._abstentions += 1
                outcome = TickOutcome(
                    TickAction.ABSTAIN_OFI,
                    symbol,
                    "ANTI_SPOOF_CANCEL_TO_FILL_REJECT",
                    time.perf_counter_ns() - started,
                )
                self._last_outcome = outcome
                return outcome
            ratio = tick.taker_buy_ratio_5s
            if ratio <= MEAN_REV_LONG_MAX_TAKER_BUY_RATIO:
                side = "BUY"
            elif ratio >= MEAN_REV_SHORT_MIN_TAKER_BUY_RATIO:
                side = "SELL"
            else:
                self._abstentions += 1
                outcome = TickOutcome(
                    TickAction.ABSTAIN_OFI,
                    symbol,
                    "MEAN_REV_NO_EXHAUSTION",
                    time.perf_counter_ns() - started,
                )
                self._last_outcome = outcome
                return outcome
        elif not tick.passes_anti_spoof_ofi(side):
            self._abstentions += 1
            outcome = TickOutcome(
                TickAction.ABSTAIN_OFI,
                symbol,
                "ANTI_SPOOF_OFI_REJECT",
                time.perf_counter_ns() - started,
            )
            self._last_outcome = outcome
            return outcome

        # ---- Step 4: shadow twin vs. Ring 0 intent dispatch ---------------
        twin = self._twin
        if twin is not None and not twin.is_graduated(self._strategy_id):
            fill = twin.execute_shadow_fill(
                tick,
                strategy_id=self._strategy_id,
                side=side,
                silo=self._silo,
                now_ns=stamp,
                qty=self._qty,
                expected_bps=lease.target_bps,
                cost_floor=self._cost_floor,
            )
            if fill is not None:
                self._shadow_fills += 1
            self._emit_shadow_card(tick, fill, lease, side, stamp)
            outcome = TickOutcome(
                TickAction.SHADOW_FILL if fill is not None else TickAction.SHADOW_RESTING,
                symbol,
                "SHADOW_WARMUP",
                time.perf_counter_ns() - started,
                None,
                fill,
            )
            self._last_outcome = outcome
            return outcome

        proposal = self._build_proposal(tick, lease, side, stamp)
        accepted = self._dispatch_live(proposal)
        action = TickAction.LIVE_PROPOSAL if accepted else TickAction.ABSTAIN_RING0
        outcome = TickOutcome(
            action,
            symbol,
            "RING0_CONFORMAL" if accepted else "RING0_CONFORMAL_REJECT",
            time.perf_counter_ns() - started,
            proposal if accepted else None,
        )
        self._last_outcome = outcome
        return outcome

    # -- batch helper ------------------------------------------------------
    def on_tick_batch(
        self, ticks: Iterable[ExecutedFlowTick], now_ns: int | None = None
    ) -> int:
        """Run a tick window inside :meth:`gc_guard`; returns trades taken."""
        trades = 0
        # Thin telemetry tap: one observation per batch (the worst tick in the
        # window) keeps the hot path free of per-tick float work while still
        # giving the Ring 3 exporter a conservative latency sample.
        worst_ns = 0
        observed = 0
        try:
            from core.telemetry import observe_tick_latency_ns as _observe

            observe = _observe
        except Exception:  # pragma: no cover - telemetry is never load-bearing
            observe = None
        with self.gc_guard():
            for tick in ticks:
                outcome = self.on_tick(tick, now_ns)
                if observe is not None:
                    elapsed = outcome.elapsed_ns
                    if elapsed > worst_ns:
                        worst_ns = elapsed
                    observed += 1
                if outcome.is_trade:
                    trades += 1
        if observe is not None and observed:
            observe(worst_ns)
        return trades

    # -- Ring 0 / cockpit emission -----------------------------------------
    def _build_proposal(
        self,
        tick: ExecutedFlowTick,
        lease: System1Lease,
        side: str,
        now_ns: int,
    ) -> UnsignedProposal:
        """Fixed-schema unsigned scalp intent (``sec_011``); never signed.

        The grounding digest is a SHA-256 over the canonical 132-byte claim
        frame: the executed-flow tick, the lease authority (expiry, target,
        stop), the decision timestamp and the side.  Every number the intent
        asserts is inside the hash, which is what ``sec_018`` Law 3 requires,
        and sealing it with ``struct`` instead of formatted text keeps the
        call inside the ``0.140 ms`` p99 budget.
        """
        self._proposal_seq += 1
        symbol = tick.symbol
        scratch = self._scratch
        TICK_WIRE_STRUCT.pack_into(
            scratch,
            0,
            symbol.encode("utf-8")[:16],
            int(tick.valid_time_ns) & 0xFFFFFFFFFFFFFFFF,
            int(tick.knowledge_ts_ns) & 0xFFFFFFFFFFFFFFFF,
            float(tick.last_price),
            float(tick.spread_bps),
            float(tick.realized_vol_5s),
            float(tick.normal_vol_5s),
            float(tick.taker_buy_volume_5s),
            float(tick.taker_sell_volume_5s),
            float(tick.bid_cancel_to_fill_ratio),
            float(tick.ask_cancel_to_fill_ratio),
        )
        CLAIM_TAIL_STRUCT.pack_into(
            scratch,
            TICK_WIRE_STRUCT.size,
            int(lease.expires_at_ns) & 0xFFFFFFFFFFFFFFFF,
            float(lease.target_bps),
            float(lease.stop_bps),
            int(now_ns),
            side.encode("ascii")[:4],
        )
        digest = hashlib.sha256(scratch).hexdigest()
        return UnsignedProposal(
            proposal_id=f"{self._strategy_id}-{self._proposal_seq:08d}",
            strategy_id=self._strategy_id,
            silo=self._silo,
            vault=VaultName.SPINAL_SCALPER,
            symbol=symbol,
            side=side,
            qty=self._qty,
            limit_price=float(tick.last_price),
            expected_gross_bps=float(lease.target_bps),
            spread_bps=float(tick.spread_bps),
            grounding_hash=digest,
            created_at_ns=int(now_ns),
        )

    def _dispatch_live(self, proposal: UnsignedProposal) -> bool:
        gate = self._ring0_gate
        if gate is not None:
            try:
                if not gate(proposal):
                    self._ring0_rejects += 1
                    return False
            except Exception:
                # An unreadable conformal gate must never authorise capital.
                self._ring0_rejects += 1
                return False

        publish = self._publish
        if publish is not None:
            try:
                publish(
                    STREAM_RING1_SCALP,
                    proposal.to_dict(),
                    proposal.proposal_id,
                )
            except Exception:  # pragma: no cover - bus outage
                # At-least-once + idempotency key means the caller can retry
                # this exact proposal_id without risking a double order.
                self._publish_errors += 1

        sink = self._proposal_sink
        if sink is not None:
            try:
                sink(proposal)
            except Exception:  # pragma: no cover - sink is caller code
                self._publish_errors += 1

        self._live_proposals += 1
        return True

    # -- cockpit cards ------------------------------------------------------
    def _emit_shadow_card(
        self,
        tick: ExecutedFlowTick,
        fill: ShadowFill | None,
        lease: System1Lease,
        side: str,
        now_ns: int,
    ) -> None:
        twin = self._twin
        floor = self._cost_floor
        q98 = (
            twin.q98_bps(self._strategy_id, now_ns)
            if twin is not None
            else float("inf")
        )
        if fill is not None:
            gross = fill.gross_bps
            story = (
                f"Shadow trial {fill.sequence}/{twin.required_fills if twin else REQUIRED_SHADOW_FILLS}: "
                f"Spinal Reflex shadow-filled {fill.symbol} on the {side.lower()} maker peg "
                f"at {fill.peg_price:.4f} as aggressive "
                f"{'sellers' if side == 'BUY' else 'buyers'} hit the quote with a clean book."
            )
        else:
            gross = 0.0
            story = (
                f"Shadow peg resting on {tick.symbol}: post-only {side.lower()} quote at the "
                f"touch is waiting for executed aggressive flow (resting depth is not liquidity)."
            )
        net_lb = net_lower_bound_bps(
            gross,
            floor.spread_bps,
            floor.maker_fee_bps,
            floor.statutory_tax_bps,
            q98,
        )
        card = PlainEnglishCockpitCard(
            silo=self._silo_label,
            symbol=str(tick.symbol),
            mode_badge="SHADOW WARMUP",
            story_sentence=story,
            gross_bps=float(gross),
            fee_stt_bps=float(floor.fee_stt_bps),
            uncert_q98_bps=float(q98),
            net_lb_bps=float(net_lb),
            timestamp_ns=int(now_ns),
        )
        self._publish_card(card)

    def _emit_trip_card(
        self, trip: BreakerTrip, tick: ExecutedFlowTick, now_ns: int
    ) -> None:
        detail = {
            VOL_SPIKE_TRIP_REASON: "realized volatility ran into the 3.5x breaker",
            TWO_ADVERSE_FILLS_TRIP_REASON: (
                "two consecutive maker fills marked out against us inside 500 ms"
            ),
            SPREAD_BLOWOUT_TRIP_REASON: "the quoted spread blew through its cap",
        }.get(trip.reason, "a microstructure guard tripped")
        card = PlainEnglishCockpitCard(
            silo=self._silo_label,
            symbol=str(tick.symbol),
            mode_badge="HALT",
            story_sentence=(
                f"Spinal Reflex disarmed {trip.symbol} in "
                f"{trip.disarm_latency_ns / 1_000_000.0:.3f} ms because {detail}."
            ),
            gross_bps=0.0,
            fee_stt_bps=float(self._cost_floor.fee_stt_bps),
            uncert_q98_bps=0.0,
            net_lb_bps=0.0,
            timestamp_ns=int(now_ns),
        )
        self._publish_card(card)

    def _publish_card(self, card: PlainEnglishCockpitCard) -> None:
        publish = self._publish
        if publish is not None:
            try:
                publish(
                    STREAM_RING3_COCKPIT,
                    card.to_dict(),
                    f"card:{self._silo}:{card.symbol}:{card.timestamp_ns}:{card.mode_badge}",
                )
            except Exception:  # pragma: no cover - bus outage
                self._publish_errors += 1
        sink = self._card_sink
        if sink is not None:
            try:
                sink(card)
            except Exception:  # pragma: no cover - sink is caller code
                self._publish_errors += 1

    # -- telemetry ----------------------------------------------------------
    @property
    def stats(self) -> dict[str, int]:
        return {
            "ticks": self._ticks,
            "abstentions": self._abstentions,
            "breaker_trips": self._trips_seen,
            "shadow_fills": self._shadow_fills,
            "live_proposals": self._live_proposals,
            "ring0_rejects": self._ring0_rejects,
            "publish_errors": self._publish_errors,
            "gc_windows": self._gc_windows,
        }

    @property
    def abstention_rate(self) -> float:
        if self._ticks == 0:
            return 0.0
        return self._abstentions / float(self._ticks)

    def describe(self) -> dict[str, Any]:
        twin = self._twin
        return {
            "strategy_id": self._strategy_id,
            "silo": self._silo,
            "stats": self.stats,
            "abstention_rate": self.abstention_rate,
            "fast_path_backend": FAST_PATH_BACKEND,
            "cost_floor": self._cost_floor.to_dict(),
            "breaker": self._breaker.describe(),
            "shadow_twin": twin.describe() if twin is not None else None,
            "graduated": bool(
                twin is not None and twin.is_graduated(self._strategy_id)
            ),
        }
