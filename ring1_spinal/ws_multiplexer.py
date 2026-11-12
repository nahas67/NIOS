"""Silo-sharded WebSocket multiplexers for the Ring 1 spinal reflex.

``sec_004`` / ``sec_006`` / ``sec_014`` require one isolated multiplexer per
pinned silo worker (``CRYPTO_BINANCE``, ``INDIA_ZERODHA_DHAN``, ``US_IBKR``,
``FX_MT5``).  Each multiplexer turns raw venue L2 depth deltas and aggressive
trade prints into the two Tier-0 carriers the scalper actually reads:

* :class:`~core.shm_ipc.ShadowBookSHM` -- the latest top-of-book L2 snapshot;
* :class:`~core.shm_ipc.OfiRingBuffer` -- rolling 5-second
  :class:`~core.schemas.ExecutedFlowTick` frames.

Design constraints
------------------
* **No Ring 2, no LLM, no key vault.**  This module imports only ``core`` and
  the standard library.  The Ring 1 air-gap is audited by AST in the Phase 2
  verification suite.
* **No blocking I/O and no locks on the frame path.**  A stalled venue socket
  can never stall :class:`~ring1_spinal.spinal_loop.ZeroGCSpinalEngine`: the
  caller feeds decoded frames, and this module only mutates bounded in-memory
  state plus the mmap segments.
* **Fail closed on bad frames.**  A frame older than the 5,000 ms lag gate, a
  duplicate/out-of-order sequence, or a sequence gap is dropped and recorded in
  a bounded quarantine deque.  Gaps and lag schedule an exponential-backoff
  reconnect; the stream's sequence baseline is reset only when the reconnect
  fires.
* **Resting depth is never proof of liquidity.**  L2 frames update the shadow
  book; only executed trade prints update the executed-flow ring (Law 1).
"""

from __future__ import annotations

import json
import math
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

from core.schemas import FEED_LAG_GATE_MS, ExecutedFlowTick
from core.shm_ipc import OfiRingBuffer, ShadowBookSHM

__all__ = [
    "BASE_BACKOFF_MS",
    "DROP_REASONS",
    "DropReason",
    "FrameKind",
    "FrameResult",
    "MAX_BACKOFF_MS",
    "SILO_IDS",
    "SiloMultiplexerPool",
    "SiloWebSocketMultiplexer",
    "StreamHealth",
    "StreamState",
    "VENUE_SILO_MAP",
    "percentile",
]

SILO_IDS: tuple[str, ...] = (
    "CRYPTO_BINANCE",
    "INDIA_ZERODHA_DHAN",
    "US_IBKR",
    "FX_MT5",
)

#: Venue names used by the Ring 2 loaders / Ring 1 adapters.
VENUE_SILO_MAP: dict[str, str] = {
    "binance": "CRYPTO_BINANCE",
    "okx": "CRYPTO_BINANCE",
    "bybit": "CRYPTO_BINANCE",
    "zerodha": "INDIA_ZERODHA_DHAN",
    "dhan": "INDIA_ZERODHA_DHAN",
    "angel": "INDIA_ZERODHA_DHAN",
    "upstox": "INDIA_ZERODHA_DHAN",
    "ibkr": "US_IBKR",
    "interactive_brokers": "US_IBKR",
    "alpaca": "US_IBKR",
    "mt5": "FX_MT5",
    "oanda": "FX_MT5",
}

BASE_BACKOFF_MS = 25.0
MAX_BACKOFF_MS = 2_000.0

NS_PER_MS = 1_000_000
FIVE_SECONDS_NS = 5_000_000_000


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile; ``0.0`` for an empty sample."""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(q))) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def _first_float(
    payload: Mapping[str, Any],
    keys: Sequence[str],
    *,
    fallback: float = 0.0,
) -> float:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return float(fallback)


def _first_level(levels: Any, index: int) -> float:
    if not levels:
        return 0.0
    try:
        level = levels[0]
        return float(level[index])
    except (IndexError, KeyError, TypeError, ValueError):
        return 0.0


class StreamState(str, Enum):
    """Per-symbol WebSocket lifecycle."""

    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"


class FrameKind(str, Enum):
    L2 = "l2"
    TRADE = "trade"


class DropReason(str, Enum):
    """Why a frame never reached the shadow book or the OFI ring."""

    FEED_LAG = "FEED_LAG_EXCEEDED"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    STALE_SEQUENCE = "STALE_SEQUENCE"
    MALFORMED = "MALFORMED_FRAME"
    UNKNOWN_KIND = "UNKNOWN_FRAME_KIND"


DROP_REASONS: tuple[str, ...] = tuple(reason.value for reason in DropReason)


@dataclass(slots=True, frozen=True)
class FrameResult:
    """Outcome of one decoded venue frame."""

    accepted: bool
    kind: str
    symbol: str
    reason: str = ""
    latency_ms: float = 0.0
    tick: ExecutedFlowTick | None = None
    state: str = StreamState.CONNECTED.value
    reconnect_at_ns: int = 0

    @property
    def dropped(self) -> bool:
        return not self.accepted


@dataclass(slots=True)
class StreamHealth:
    """Bounded per-symbol stream state (no unbounded bookkeeping)."""

    symbol: str
    state: StreamState = StreamState.CONNECTED
    last_seq: int = -1
    failure_streak: int = 0
    backoff_ms: float = 0.0
    next_reconnect_ns: int = 0
    reconnects: int = 0
    frames: int = 0
    dropped: int = 0
    last_frame_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "last_seq": self.last_seq,
            "failure_streak": self.failure_streak,
            "backoff_ms": self.backoff_ms,
            "next_reconnect_ns": self.next_reconnect_ns,
            "reconnects": self.reconnects,
            "frames": self.frames,
            "dropped": self.dropped,
            "last_frame_ns": self.last_frame_ns,
        }


@dataclass(slots=True)
class _TradeWindow:
    """O(1) rolling 5-second executed-flow state for one symbol."""

    stamps: deque[int] = field(default_factory=lambda: deque(maxlen=8_192))
    prices: deque[float] = field(default_factory=lambda: deque(maxlen=8_192))
    quantities: deque[float] = field(default_factory=lambda: deque(maxlen=8_192))
    sides: deque[float] = field(default_factory=lambda: deque(maxlen=8_192))
    returns: deque[float] = field(default_factory=lambda: deque(maxlen=8_192))
    cancels: deque[tuple[int, float, float]] = field(
        default_factory=lambda: deque(maxlen=4_096)
    )
    buy_sum: float = 0.0
    sell_sum: float = 0.0
    qty_sum: float = 0.0
    return_sum: float = 0.0
    return_sq_sum: float = 0.0
    return_count: int = 0
    last_price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    normal_vol_bps: float = 0.0

    def add_cancel(self, ts_ns: int, bid_cancels: float, ask_cancels: float) -> None:
        if bid_cancels <= 0.0 and ask_cancels <= 0.0:
            return
        self.cancels.append((int(ts_ns), float(bid_cancels), float(ask_cancels)))

    def evict(self, cutoff_ns: int) -> None:
        while self.stamps and self.stamps[0] < cutoff_ns:
            self.stamps.popleft()
            price = self.prices.popleft()
            qty = self.quantities.popleft()
            side = self.sides.popleft()
            ret = self.returns.popleft()
            if side > 0.0:
                self.buy_sum -= qty
            else:
                self.sell_sum -= qty
            self.qty_sum -= qty
            self.return_sum -= ret
            self.return_sq_sum -= ret * ret
            self.return_count -= 1
            del price
        while self.cancels and self.cancels[0][0] < cutoff_ns:
            self.cancels.popleft()

    def add_trade(
        self,
        ts_ns: int,
        price: float,
        qty: float,
        side: float,
        cutoff_ns: int,
    ) -> None:
        ret = 0.0
        if self.last_price > 0.0 and price > 0.0:
            ret = math.log(price / self.last_price)
        self.stamps.append(int(ts_ns))
        self.prices.append(float(price))
        self.quantities.append(float(qty))
        self.sides.append(float(side))
        self.returns.append(ret)
        if side > 0.0:
            self.buy_sum += qty
        else:
            self.sell_sum += qty
        self.qty_sum += qty
        self.return_sum += ret
        self.return_sq_sum += ret * ret
        self.return_count += 1
        self.last_price = float(price)
        self.evict(cutoff_ns)

    def realized_vol_bps(self) -> float:
        n = self.return_count
        if n >= 2:
            variance = (self.return_sq_sum - (self.return_sum * self.return_sum) / n) / (
                n - 1
            )
            return math.sqrt(max(variance, 0.0)) * 10_000.0
        if n == 1:
            return abs(self.return_sum) * 10_000.0
        return 0.0

    def spread_bps(self) -> float:
        if self.best_bid > 0.0 and self.best_ask >= self.best_bid:
            mid = 0.5 * (self.best_bid + self.best_ask)
            if mid > 0.0:
                return (self.best_ask - self.best_bid) / mid * 10_000.0
        return 0.0

    def cancel_to_fill(self) -> tuple[float, float]:
        filled = max(1.0, self.qty_sum)
        bid = sum(row[1] for row in self.cancels) / filled
        ask = sum(row[2] for row in self.cancels) / filled
        return bid, ask


class SiloWebSocketMultiplexer:
    """One isolated WebSocket normalizer for one pinned silo worker."""

    @staticmethod
    def configure_transport_socket(sock: socket.socket) -> dict[str, Any]:
        """Apply the Ring 1 low-latency contract to a venue WebSocket socket.

        A venue WebSocket carries the ticks that start the ``<0.14 ms``
        tick-to-intent budget, so Nagle is never allowed on it.  The helper is
        owned by :mod:`core.os_tuning` and reads every option back from the
        kernel; the returned report is the audit evidence.
        """

        from core.os_tuning import configure_low_latency_socket

        return configure_low_latency_socket(sock)

    __slots__ = (
        "silo",
        "shadow_book",
        "ofi_ring",
        "window_ns",
        "lag_gate_ms",
        "base_backoff_ms",
        "max_backoff_ms",
        "vol_alpha",
        "clock",
        "quarantine_sink",
        "_streams",
        "_windows",
        "_latencies_ns",
        "_quarantine",
        "_drops",
        "_frames",
        "_drain_cursor",
    )

    def __init__(
        self,
        silo: str,
        *,
        shadow_book: ShadowBookSHM,
        ofi_ring: OfiRingBuffer,
        window_ns: int = FIVE_SECONDS_NS,
        lag_gate_ms: float = FEED_LAG_GATE_MS,
        base_backoff_ms: float = BASE_BACKOFF_MS,
        max_backoff_ms: float = MAX_BACKOFF_MS,
        vol_alpha: float = 0.05,
        quarantine_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        clock: Callable[[], int] = time.time_ns,
        latency_samples: int = 65_536,
        quarantine_samples: int = 512,
    ) -> None:
        if shadow_book is None or ofi_ring is None:
            raise ValueError("a multiplexer requires a shadow book and an OFI ring")
        self.silo = str(silo).strip().upper()
        if not self.silo:
            raise ValueError("silo must be non-empty")
        self.shadow_book = shadow_book
        self.ofi_ring = ofi_ring
        self.window_ns = int(window_ns)
        self.lag_gate_ms = float(lag_gate_ms)
        self.base_backoff_ms = float(base_backoff_ms)
        self.max_backoff_ms = float(max_backoff_ms)
        self.vol_alpha = float(vol_alpha)
        self.clock = clock
        self.quarantine_sink = quarantine_sink
        self._streams: dict[str, StreamHealth] = {}
        self._windows: dict[str, _TradeWindow] = {}
        self._latencies_ns: deque[int] = deque(maxlen=int(latency_samples))
        self._quarantine: deque[dict[str, Any]] = deque(maxlen=int(quarantine_samples))
        self._drops: MutableMapping[str, int] = {reason: 0 for reason in DROP_REASONS}
        self._frames = 0
        self._drain_cursor = 0

    # -- stream state -----------------------------------------------------
    def _health(self, symbol: str) -> StreamHealth:
        health = self._streams.get(symbol)
        if health is None:
            health = StreamHealth(symbol=symbol)
            self._streams[symbol] = health
        return health

    def _window(self, symbol: str) -> _TradeWindow:
        window = self._windows.get(symbol)
        if window is None:
            window = _TradeWindow()
            self._windows[symbol] = window
        return window

    def _quarantine_frame(
        self,
        reason: DropReason,
        symbol: str,
        *,
        seq: int | None,
        lag_ms: float,
        at_ns: int,
        detail: str,
    ) -> None:
        self._drops[reason.value] = self._drops.get(reason.value, 0) + 1
        record = {
            "reason": reason.value,
            "silo": self.silo,
            "symbol": symbol,
            "seq": seq,
            "lag_ms": round(float(lag_ms), 3),
            "at_ns": int(at_ns),
            "detail": str(detail),
        }
        self._quarantine.append(record)
        if self.quarantine_sink is not None:
            try:
                self.quarantine_sink(record)
            except Exception:  # pragma: no cover - telemetry must not break Ring 1
                pass

    def _schedule_reconnect(
        self, health: StreamHealth, now_ns: int, reason: str
    ) -> None:
        health.failure_streak += 1
        backoff = min(
            self.max_backoff_ms,
            self.base_backoff_ms * (2.0 ** (health.failure_streak - 1)),
        )
        health.state = StreamState.RECONNECTING
        health.backoff_ms = float(backoff)
        health.next_reconnect_ns = int(now_ns) + int(backoff * NS_PER_MS)
        health.reconnects += 1
        health.dropped += 1
        del reason

    def poll_reconnects(self, now_ns: int | None = None) -> tuple[str, ...]:
        """Fire every due reconnect and reset that stream's sequence baseline.

        This is the only place a gap is healed.  Until it fires, every frame on
        the broken stream is dropped, so the scalper never consumes a book with
        an unknown hole in it.
        """
        stamp = int(now_ns) if now_ns is not None else int(self.clock())
        reconnected: list[str] = []
        for symbol, health in self._streams.items():
            if (
                health.state is StreamState.RECONNECTING
                and stamp >= health.next_reconnect_ns
            ):
                health.state = StreamState.CONNECTED
                health.last_seq = -1
                health.failure_streak = 0
                health.backoff_ms = 0.0
                health.next_reconnect_ns = 0
                reconnected.append(symbol)
        return tuple(reconnected)

    # -- frame decoding ---------------------------------------------------
    @staticmethod
    def _decode(frame: Any) -> Mapping[str, Any]:
        if isinstance(frame, Mapping):
            return frame
        if isinstance(frame, (bytes, bytearray, memoryview)):
            payload = json.loads(bytes(frame).decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("decoded WebSocket payload is not an object")
            return payload
        raise TypeError(f"unsupported frame type {type(frame).__name__}")

    def ingest_frame(
        self,
        frame: Any,
        *,
        now_ns: int | None = None,
    ) -> FrameResult:
        """Normalize one L2 or trade frame in ``< 0.05 ms``.

        The returned :class:`FrameResult` carries the measured normalization
        latency.  Every rejection is counted and recorded; nothing is silently
        lost.
        """
        started = time.perf_counter_ns()
        recv_ns = int(now_ns) if now_ns is not None else int(self.clock())
        self._frames += 1

        try:
            payload = self._decode(frame)
        except Exception as exc:  # noqa: BLE001 - malformed venue payload
            self._quarantine_frame(
                DropReason.MALFORMED,
                "",
                seq=None,
                lag_ms=0.0,
                at_ns=recv_ns,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return FrameResult(
                accepted=False,
                kind="",
                symbol="",
                reason=DropReason.MALFORMED.value,
                latency_ms=(time.perf_counter_ns() - started) / 1e6,
            )

        kind_token = str(
            payload.get("type") or payload.get("kind") or payload.get("channel") or ""
        ).strip().lower()
        symbol = str(payload.get("symbol") or payload.get("s") or "").strip().upper()
        if not symbol:
            self._quarantine_frame(
                DropReason.MALFORMED,
                "",
                seq=payload.get("seq"),
                lag_ms=0.0,
                at_ns=recv_ns,
                detail="frame has no symbol",
            )
            return FrameResult(
                accepted=False,
                kind=kind_token,
                symbol="",
                reason=DropReason.MALFORMED.value,
                latency_ms=(time.perf_counter_ns() - started) / 1e6,
            )

        health = self._health(symbol)
        if health.state is StreamState.RECONNECTING:
            if recv_ns < health.next_reconnect_ns:
                health.dropped += 1
                self._quarantine_frame(
                    DropReason.SEQUENCE_GAP,
                    symbol,
                    seq=payload.get("seq"),
                    lag_ms=0.0,
                    at_ns=recv_ns,
                    detail="stream awaiting exponential-backoff reconnect",
                )
                # Every frame that arrives while the stream is down extends the
                # backoff.  A dead venue is never hammered at tick rate.
                self._schedule_reconnect(
                    health, recv_ns, "still-reconnecting"
                )
                return FrameResult(
                    accepted=False,
                    kind=kind_token,
                    symbol=symbol,
                    reason=DropReason.SEQUENCE_GAP.value,
                    latency_ms=(time.perf_counter_ns() - started) / 1e6,
                    state=health.state.value,
                    reconnect_at_ns=health.next_reconnect_ns,
                )
            # A due reconnect is fired lazily on the next frame so the hot path
            # never needs a background timer.
            self.poll_reconnects(recv_ns)
            health = self._health(symbol)

        ts_ns = payload.get("ts_ns")
        if ts_ns is None:
            ts_ns = payload.get("timestamp_ns") or payload.get("T") or recv_ns
        ts_ns = int(ts_ns)
        lag_ms = max(0.0, (recv_ns - ts_ns) / 1e6)

        seq_raw = payload.get("seq")
        seq = int(seq_raw) if seq_raw is not None else None

        if lag_ms > self.lag_gate_ms:
            self._quarantine_frame(
                DropReason.FEED_LAG,
                symbol,
                seq=seq,
                lag_ms=lag_ms,
                at_ns=recv_ns,
                detail=f"lag {lag_ms:.1f} ms > {self.lag_gate_ms:.0f} ms",
            )
            self._schedule_reconnect(health, recv_ns, DropReason.FEED_LAG.value)
            return FrameResult(
                accepted=False,
                kind=kind_token,
                symbol=symbol,
                reason=DropReason.FEED_LAG.value,
                latency_ms=(time.perf_counter_ns() - started) / 1e6,
                state=health.state.value,
                reconnect_at_ns=health.next_reconnect_ns,
            )

        if seq is not None and health.last_seq >= 0:
            if seq <= health.last_seq:
                health.dropped += 1
                self._quarantine_frame(
                    DropReason.STALE_SEQUENCE,
                    symbol,
                    seq=seq,
                    lag_ms=lag_ms,
                    at_ns=recv_ns,
                    detail=f"seq {seq} <= last {health.last_seq}",
                )
                return FrameResult(
                    accepted=False,
                    kind=kind_token,
                    symbol=symbol,
                    reason=DropReason.STALE_SEQUENCE.value,
                    latency_ms=(time.perf_counter_ns() - started) / 1e6,
                    state=health.state.value,
                )
            if seq > health.last_seq + 1:
                self._quarantine_frame(
                    DropReason.SEQUENCE_GAP,
                    symbol,
                    seq=seq,
                    lag_ms=lag_ms,
                    at_ns=recv_ns,
                    detail=f"seq {seq} skipped {health.last_seq + 1}..{seq - 1}",
                )
                self._schedule_reconnect(health, recv_ns, DropReason.SEQUENCE_GAP.value)
                return FrameResult(
                    accepted=False,
                    kind=kind_token,
                    symbol=symbol,
                    reason=DropReason.SEQUENCE_GAP.value,
                    latency_ms=(time.perf_counter_ns() - started) / 1e6,
                    state=health.state.value,
                    reconnect_at_ns=health.next_reconnect_ns,
                )

        if kind_token in ("l2", "book", "depth", "l2_delta", "orderbook"):
            try:
                tick = self._ingest_l2(payload, symbol, ts_ns, recv_ns)
            except Exception as exc:  # noqa: BLE001 - malformed venue payload
                health.dropped += 1
                self._quarantine_frame(
                    DropReason.MALFORMED,
                    symbol,
                    seq=seq,
                    lag_ms=lag_ms,
                    at_ns=recv_ns,
                    detail=f"L2: {type(exc).__name__}: {exc}",
                )
                return FrameResult(
                    accepted=False,
                    kind=kind_token,
                    symbol=symbol,
                    reason=DropReason.MALFORMED.value,
                    latency_ms=(time.perf_counter_ns() - started) / 1e6,
                    state=health.state.value,
                )
        elif kind_token in ("trade", "trades", "aggressive_trade", "execution", "fill"):
            try:
                tick = self._ingest_trade(payload, symbol, ts_ns, recv_ns)
            except Exception as exc:  # noqa: BLE001 - malformed venue payload
                health.dropped += 1
                self._quarantine_frame(
                    DropReason.MALFORMED,
                    symbol,
                    seq=seq,
                    lag_ms=lag_ms,
                    at_ns=recv_ns,
                    detail=f"TRADE: {type(exc).__name__}: {exc}",
                )
                return FrameResult(
                    accepted=False,
                    kind=kind_token,
                    symbol=symbol,
                    reason=DropReason.MALFORMED.value,
                    latency_ms=(time.perf_counter_ns() - started) / 1e6,
                    state=health.state.value,
                )
        else:
            health.dropped += 1
            self._quarantine_frame(
                DropReason.UNKNOWN_KIND,
                symbol,
                seq=seq,
                lag_ms=lag_ms,
                at_ns=recv_ns,
                detail=f"unknown kind {kind_token!r}",
            )
            return FrameResult(
                accepted=False,
                kind=kind_token,
                symbol=symbol,
                reason=DropReason.UNKNOWN_KIND.value,
                latency_ms=(time.perf_counter_ns() - started) / 1e6,
                state=health.state.value,
            )

        if seq is not None:
            health.last_seq = seq
        health.state = StreamState.CONNECTED
        health.failure_streak = 0
        health.backoff_ms = 0.0
        health.next_reconnect_ns = 0
        health.frames += 1
        health.last_frame_ns = recv_ns
        latency_ns = time.perf_counter_ns() - started
        self._latencies_ns.append(latency_ns)
        return FrameResult(
            accepted=True,
            kind=kind_token,
            symbol=symbol,
            latency_ms=latency_ns / 1e6,
            tick=tick,
            state=health.state.value,
        )

    def _ingest_l2(
        self,
        payload: Mapping[str, Any],
        symbol: str,
        ts_ns: int,
        recv_ns: int,
    ) -> None:
        bid = _first_float(
            payload,
            ("bid", "best_bid", "b"),
            fallback=_first_level(payload.get("bids"), 0),
        )
        ask = _first_float(
            payload,
            ("ask", "best_ask", "a"),
            fallback=_first_level(payload.get("asks"), 0),
        )
        bid_qty = _first_float(
            payload,
            ("bid_qty", "bid_size", "bq"),
            fallback=_first_level(payload.get("bids"), 1),
        )
        ask_qty = _first_float(
            payload,
            ("ask_qty", "ask_size", "aq"),
            fallback=_first_level(payload.get("asks"), 1),
        )
        if bid < 0.0 or ask < 0.0 or (bid > 0.0 and ask > 0.0 and ask < bid):
            raise ValueError("invalid L2 quote")
        self.shadow_book.write_snapshot(
            symbol,
            bid,
            ask,
            bid_qty,
            ask_qty,
            updated_at_ns=ts_ns,
        )
        window = self._window(symbol)
        window.best_bid = bid
        window.best_ask = ask
        window.add_cancel(
            ts_ns,
            float(payload.get("bid_cancels") or payload.get("bid_cancel_qty") or 0.0),
            float(payload.get("ask_cancels") or payload.get("ask_cancel_qty") or 0.0),
        )
        del recv_ns

    def _ingest_trade(
        self,
        payload: Mapping[str, Any],
        symbol: str,
        ts_ns: int,
        recv_ns: int,
    ) -> ExecutedFlowTick:
        price = float(payload.get("price") or payload.get("p") or 0.0)
        qty = float(
            payload.get("qty") or payload.get("quantity") or payload.get("q") or 0.0
        )
        if price <= 0.0 or qty <= 0.0:
            raise ValueError("trade frame requires positive price and qty")
        side_raw = str(payload.get("side") or payload.get("S") or "BUY").strip().upper()
        side = 1.0 if side_raw in ("BUY", "B", "LONG") else -1.0

        window = self._window(symbol)
        cutoff = int(recv_ns) - self.window_ns
        window.add_trade(ts_ns, price, qty, side, cutoff)

        realized = window.realized_vol_bps()
        baseline = window.normal_vol_bps
        if baseline <= 0.0:
            baseline = max(realized, 1.0)
        else:
            baseline = (1.0 - self.vol_alpha) * baseline + self.vol_alpha * max(
                realized, 1e-6
            )
        window.normal_vol_bps = baseline

        bid_c2f, ask_c2f = window.cancel_to_fill()
        tick = ExecutedFlowTick(
            symbol=symbol,
            valid_time_ns=int(ts_ns),
            knowledge_ts_ns=int(recv_ns),
            last_price=float(price),
            spread_bps=float(window.spread_bps()),
            realized_vol_5s=float(realized),
            normal_vol_5s=float(baseline),
            taker_buy_volume_5s=float(max(0.0, window.buy_sum)),
            taker_sell_volume_5s=float(max(0.0, window.sell_sum)),
            bid_cancel_to_fill_ratio=float(bid_c2f),
            ask_cancel_to_fill_ratio=float(ask_c2f),
        )
        self.ofi_ring.append(tick, now_ns=recv_ns)
        return tick

    # -- draining into the spinal engine ---------------------------------
    def drain_to(
        self,
        engine: Any,
        *,
        now_ns: int | None = None,
        limit: int | None = None,
    ) -> list[Any]:
        """Feed every newly produced tick to ``engine.on_tick`` in order.

        The cursor is monotonic, so a caller may drain repeatedly without
        re-processing a tick.  ``now_ns`` is the engine's monotonic clock (lease
        expiry), not the tick's wall-clock timestamp.
        """
        stamp = int(now_ns) if now_ns is not None else time.monotonic_ns()
        start = int(self._drain_cursor)
        end = int(self.ofi_ring.cursor)
        if limit is not None:
            end = min(end, start + max(0, int(limit)))
        outcomes: list[Any] = []
        n_slots = int(self.ofi_ring.n_slots)
        for index in range(start, end):
            tick = self.ofi_ring._read_slot(index % n_slots)
            if tick is None:
                continue
            outcomes.append(engine.on_tick(tick, now_ns=stamp))
        self._drain_cursor = end
        return outcomes

    # -- diagnostics ------------------------------------------------------
    def latency_stats(self) -> dict[str, float]:
        samples = [value / 1e6 for value in self._latencies_ns]
        return {
            "count": float(len(samples)),
            "p50_ms": percentile(samples, 0.50),
            "p95_ms": percentile(samples, 0.95),
            "p99_ms": percentile(samples, 0.99),
            "max_ms": max(samples) if samples else 0.0,
        }

    @property
    def p99_ms(self) -> float:
        if not self._latencies_ns:
            return 0.0
        return percentile([value / 1e6 for value in self._latencies_ns], 0.99)

    @property
    def latencies_ms(self) -> tuple[float, ...]:
        """Every measured normalization latency, in milliseconds."""
        return tuple(value / 1e6 for value in self._latencies_ns)

    def health(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "frames": self._frames,
            "streams": {symbol: h.to_dict() for symbol, h in self._streams.items()},
            "drops": dict(self._drops),
            "quarantine": list(self._quarantine),
            "latency": self.latency_stats(),
            "ofi_cursor": int(self.ofi_ring.cursor),
            "shadow_book_uri": self.shadow_book.uri,
            "ofi_ring_uri": self.ofi_ring.uri,
        }

    def reset_latencies(self) -> None:
        self._latencies_ns.clear()


class SiloMultiplexerPool:
    """The four isolated silo multiplexers, one per pinned worker.

    Passing a shared ``shadow_book`` / ``ofi_ring`` keeps a single-process test
    or paper deployment on the canonical Tier-0 segments; omitting them creates
    per-silo segments (``nios-shadow-book-<silo>`` /
    ``nios-ofi-<silo>``) so two workers can never tear each other's book.
    """

    def __init__(
        self,
        *,
        silos: Iterable[str] = SILO_IDS,
        shadow_book: ShadowBookSHM | None = None,
        ofi_ring: OfiRingBuffer | None = None,
        **kwargs: Any,
    ) -> None:
        self.silos = tuple(str(s).strip().upper() for s in silos)
        self._owns_books = shadow_book is None
        self._owns_rings = ofi_ring is None
        self._shared_book = shadow_book
        self._shared_ring = ofi_ring
        self.multiplexers: dict[str, SiloWebSocketMultiplexer] = {}
        for silo in self.silos:
            book = shadow_book or ShadowBookSHM(name=f"nios-shadow-book-{silo.lower()}")
            ring = ofi_ring or OfiRingBuffer(name=f"nios-ofi-{silo.lower()}")
            self.multiplexers[silo] = SiloWebSocketMultiplexer(
                silo, shadow_book=book, ofi_ring=ring, **kwargs
            )

    def get(self, silo: str) -> SiloWebSocketMultiplexer:
        return self.multiplexers[str(silo).strip().upper()]

    def ingest(self, silo: str, frame: Any, *, now_ns: int | None = None) -> FrameResult:
        return self.get(silo).ingest_frame(frame, now_ns=now_ns)

    def ingest_many(
        self,
        frames: Iterable[tuple[str, Any]],
        *,
        now_ns: int | None = None,
    ) -> list[FrameResult]:
        return [
            self.ingest(silo, frame, now_ns=now_ns) for silo, frame in frames
        ]

    def poll_reconnects(self, now_ns: int | None = None) -> dict[str, tuple[str, ...]]:
        return {
            silo: mux.poll_reconnects(now_ns)
            for silo, mux in self.multiplexers.items()
        }

    def drain_to(
        self,
        engines: Mapping[str, Any],
        *,
        now_ns: int | None = None,
    ) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for silo, mux in self.multiplexers.items():
            engine = engines.get(silo)
            if engine is None:
                continue
            out[silo] = mux.drain_to(engine, now_ns=now_ns)
        return out

    def health(self) -> dict[str, Any]:
        return {
            "silos": self.silos,
            "multiplexers": {
                silo: mux.health() for silo, mux in self.multiplexers.items()
            },
            "p99_ms": max(
                (mux.p99_ms for mux in self.multiplexers.values()), default=0.0
            ),
        }

    def close(self) -> None:
        closed_books: set[int] = set()
        closed_rings: set[int] = set()
        for mux in self.multiplexers.values():
            if self._owns_books and id(mux.shadow_book) not in closed_books:
                mux.shadow_book.close()
                closed_books.add(id(mux.shadow_book))
            if self._owns_rings and id(mux.ofi_ring) not in closed_rings:
                mux.ofi_ring.close()
                closed_rings.add(id(mux.ofi_ring))
