"""NIOS Core v3.0 -- Tier 0 lock-free shared-memory IPC spine (STEP 2).

Implements the ``<10 us`` state plane described by ``sec_004``, ``sec_007``,
``sec_009`` and ``sec_013``:

======================  =====================================================
Segment                 Purpose
======================  =====================================================
``nios-global-kill-latch``  Atomic global halt latch read before every lease
``nios-active-leases``      5,000 x 64-byte :class:`System1Lease` slots
``nios-ofi-cancel-ring``    5-second rolling executed-OFI ring buffer
``nios-shadow-book``        L2 shadow-twin book snapshot slots
======================  =====================================================

Design notes
------------
* Built on :class:`multiprocessing.shared_memory.SharedMemory`, which maps to
  ``shm_open``/``mmap`` on POSIX and to ``CreateFileMapping`` on Windows, so
  the identical code runs in ``C:\\Users\\nahas\\OneDrive\\Desktop\\NIOS`` and
  on a Linux box.
* Every slot region is 64-byte cache-line aligned.  Each lease slot carries
  its own single-writer sequence lock: the writer publishes an odd sequence,
  writes the 64-byte lease, then publishes the next even sequence.  A reader
  that observes an odd sequence, or a sequence that changed mid-read, retries
  and finally fails **closed** (disarmed lease).
* Readers check the kill latch first.  A hot latch short-circuits to a
  disarmed lease in a couple of microseconds, which is what makes the
  ``<1.0 ms`` global kill-switch SLA (``sec_016``) achievable without waking a
  single worker process.
* No locks, no allocation, no logging, no attribute-heavy abstractions on the
  read path.
* Every segment name is namespaced by ``NIOS_SHM_PREFIX`` (default ``"nios"``
  in production).  Tests and parallel ``pytest`` runs set a process-unique
  prefix, so a test process can never collide with -- or reset -- the segments
  a live NIOS server is using.
* Segment creation is race-proof: if ``create=True`` loses to another process
  and raises ``FileExistsError``, the segment is attached with
  ``create=False`` and its header/buffer are zeroed instead of crashing.
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from enum import IntEnum
from functools import lru_cache
from multiprocessing.shared_memory import SharedMemory
from typing import Iterator, NamedTuple

from .schemas import (
    LeaseMode,
    System1Lease,
    ExecutedFlowTick,
)

__all__ = [
    "SHM_URI_PREFIX",
    "SHM_PREFIX_ENV",
    "DEFAULT_SHM_PREFIX",
    "KILL_LATCH_SHM_NAME",
    "LEASE_TABLE_SHM_NAME",
    "OFI_RING_SHM_NAME",
    "SHADOW_BOOK_SHM_NAME",
    "CACHE_LINE",
    "HaltReason",
    "KillLatchState",
    "GlobalKillLatch",
    "LeaseTableSHM",
    "OfiRingBuffer",
    "ShadowBookSnapshot",
    "ShadowBookSHM",
    "segment_name",
    "shm_prefix",
    "reset_named_segment",
    "unlink_all",
]

SHM_URI_PREFIX = "shm://"
CACHE_LINE = 64

#: Environment variable that namespaces every Tier-0 segment.
#:
#: Production leaves it unset and gets the ``nios-*`` names promised by
#: ``sec_009``.  ``tests/conftest.py`` sets a per-PID value so ``pytest`` and a
#: live NIOS server can run at the same instant with zero collisions.
SHM_PREFIX_ENV = "NIOS_SHM_PREFIX"
DEFAULT_SHM_PREFIX = "nios"

KILL_LATCH_SHM_NAME = "nios-global-kill-latch"
LEASE_TABLE_SHM_NAME = "nios-active-leases"
OFI_RING_SHM_NAME = "nios-ofi-cancel-ring"
SHADOW_BOOK_SHM_NAME = "nios-shadow-book"


def shm_prefix() -> str:
    """Active segment namespace (read live so tests can monkeypatch it)."""
    raw = os.environ.get(SHM_PREFIX_ENV, "").strip()
    if not raw:
        return DEFAULT_SHM_PREFIX
    # SharedMemory forbids path separators in names on both Windows and POSIX.
    return raw.replace("\\", "-").replace("/", "-")


def segment_name(name: str) -> str:
    """Map a logical NIOS segment name into the active prefix namespace.

    Idempotent: a name that already carries the active prefix is returned
    unchanged, so ``reset_named_segment(segment_name(x))`` is always safe.
    """
    prefix = shm_prefix()
    if prefix == DEFAULT_SHM_PREFIX:
        return name
    if name == prefix or name.startswith(prefix + "-"):
        return name
    if name.startswith(DEFAULT_SHM_PREFIX):
        name = name[len(DEFAULT_SHM_PREFIX):].lstrip("-_")
    return f"{prefix}-{name}" if name else prefix

#: 0 == ARMED, 1 == HALTED (``sec_004``).
KILL_ARMED = 0
KILL_HALTED = 1


# ---------------------------------------------------------------------------
# Low-level segment management
# ---------------------------------------------------------------------------

#: magic | version | slot_count | slot_size | reserved(32)
_SEGMENT_HEADER = struct.Struct("<8sQQQ32x")
_SEGMENT_HEADER_SIZE = _SEGMENT_HEADER.size
assert _SEGMENT_HEADER_SIZE == CACHE_LINE

_U64 = struct.Struct("<Q")
_U32 = struct.Struct("<I")

_KILL_MAGIC = b"NIOSKIL1"
_LEASE_MAGIC = b"NIOSLSE1"
_OFI_MAGIC = b"NIOSOFI1"
_BOOK_MAGIC = b"NIOSBOK1"


def _unlink_quiet(name: str) -> None:
    """Remove a named segment if it exists; never raise."""
    try:
        handle = SharedMemory(name=name)
    except FileNotFoundError:
        return
    except OSError:  # pragma: no cover - platform specific
        return
    try:
        handle.close()
    except Exception:  # pragma: no cover - best effort
        pass
    try:
        handle.unlink()
    except Exception:  # pragma: no cover - still attached elsewhere
        pass


def reset_named_segment(name: str) -> None:
    """Public helper used by tests and crash recovery to drop stale state.

    The name is resolved through :func:`segment_name`, so a logical
    ``nios-active-leases`` only ever drops the segment in the *active*
    namespace -- never a live server's.
    """
    _unlink_quiet(segment_name(name))


class _SharedSegment:
    """Create-or-attach wrapper around :class:`SharedMemory`.

    ``reset=True`` means "guarantee a fresh, zeroed segment".  It is
    implemented as *create, else attach-and-zero* instead of
    ``unlink(); create()``, because the unlink/create pair races any other
    process (or pytest worker) touching the same name:

    * ``create=True`` wins -> we own a brand-new mapping and zero it.
    * ``FileExistsError``  -> another process won the race.  Attach with
      ``create=False``, zero the header and payload, and carry on.  Never
      crash, never leave a half-initialised segment behind.

    ``reset=False`` attaches without destroying state, which is what a second
    NIOS process must do when it joins a live server's segments.
    """

    __slots__ = ("name", "size", "magic", "created", "_shm", "_buf")

    def __init__(self, name: str, size: int, magic: bytes, reset: bool = False):
        if len(magic) != 8:
            raise ValueError("magic must be exactly 8 bytes")
        self.name = segment_name(name)
        self.size = int(size)
        self.magic = magic
        self.created = False
        self._shm: SharedMemory | None = None
        self._buf = None
        self._open(bool(reset))

    # -- lifecycle --------------------------------------------------------
    def _create_or_attach(self, size: int) -> tuple[SharedMemory, bool]:
        """Create the segment, or attach when it already exists.

        Windows raises ``FileExistsError`` (WinError 183) and POSIX raises
        ``EEXIST``; both surface as ``FileExistsError`` from CPython, and both
        are handled identically: attach to the existing segment.
        """
        try:
            return SharedMemory(name=self.name, create=True, size=size), True
        except FileExistsError:
            return SharedMemory(name=self.name, create=False), False

    def _open(self, reset: bool) -> None:
        # Best effort: drop a stale segment when no live process holds it.  The
        # attach path below is what makes the operation race-proof when it does.
        if reset:
            _unlink_quiet(self.name)

        handle, created = self._create_or_attach(self.size)
        if handle.size < self.size:
            # An older build sized the segment smaller; grow in place.
            handle.close()
            _unlink_quiet(self.name)
            handle, created = self._create_or_attach(self.size)

        self._shm = handle
        self._buf = handle.buf
        self.created = created

        existing = bytes(self._buf[0:8])
        if created or reset or existing != self.magic:
            # Fresh segment, explicit reset, or stale bytes from another build:
            # zero header + payload so no lease, latch, offset or cursor can
            # leak across a restart or a namespace change.
            self._zero()
            self._write_header()

    def _zero(self) -> None:
        self._buf[:] = bytes(len(self._buf))

    def _write_header(self) -> None:
        _SEGMENT_HEADER.pack_into(self._buf, 0, self.magic, 1, 0, self.size)

    # -- access -----------------------------------------------------------
    @property
    def buf(self) -> memoryview:
        if self._buf is None:  # pragma: no cover - closed segment
            raise RuntimeError(f"segment {self.name!r} is closed")
        return self._buf

    @property
    def uri(self) -> str:
        return SHM_URI_PREFIX + self.name

    def flush(self) -> None:
        """Best-effort msync.  Correctness never depends on it (x86 TSO)."""
        handle = self._shm
        if handle is None:
            return
        try:
            handle._mmap.flush()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - platform specific
            pass

    def close(self) -> None:
        if self._buf is not None:
            try:
                self._buf.release()
            except Exception:  # pragma: no cover
                pass
            self._buf = None
        if self._shm is not None:
            try:
                self._shm.close()
            finally:
                self._shm = None

    def unlink(self) -> None:
        self.close()
        _unlink_quiet(self.name)

    def __enter__(self) -> "_SharedSegment":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Global kill latch
# ---------------------------------------------------------------------------


class HaltReason(IntEnum):
    """Reason codes carried alongside the halt flag (``sec_011`` breakers)."""

    NONE = 0
    MANUAL_KILL_SWITCH = 1
    VOL_SPIKE = 2
    CONTAGION_FRAGILITY = 3
    ADR009_DAILY_LOSS = 4
    RECONCILIATION_DRIFT = 5
    FEED_QUARANTINE = 6


class KillLatchState(NamedTuple):
    seq: int
    kill_flag: int
    updated_at_ns: int
    reason_code: int

    @property
    def is_halted(self) -> bool:
        return self.kill_flag == KILL_HALTED


#: seq | updated_at_ns | kill_flag:i32? -- flag is unsigned 32-bit, reason int32
_KILL_SLOT = struct.Struct("<QQIi40x")
_KILL_SLOT_SIZE = _KILL_SLOT.size
assert _KILL_SLOT_SIZE == CACHE_LINE

_KILL_SLOT_OFFSET = _SEGMENT_HEADER_SIZE          # 64
_KILL_FLAG_OFFSET = _KILL_SLOT_OFFSET + 16        # 80
_KILL_REASON_OFFSET = _KILL_SLOT_OFFSET + 20      # 84
_KILL_TIME_OFFSET = _KILL_SLOT_OFFSET + 8         # 72
_KILL_SEGMENT_SIZE = _SEGMENT_HEADER_SIZE + _KILL_SLOT_SIZE


class GlobalKillLatch:
    """The ``shm://nios-global-kill-latch`` atomic halt latch.

    Flipping this latch is the fastest possible action in the system: no
    queue, no NATS round-trip, no process wake-up.  Ring 1 observes it on its
    next tick and every subsequent lease read returns ``DISARMED``.
    """

    __slots__ = ("_seg",)

    def __init__(self, reset: bool = False, name: str = KILL_LATCH_SHM_NAME):
        self._seg = _SharedSegment(name, _KILL_SEGMENT_SIZE, _KILL_MAGIC, reset=reset)

    # -- properties -------------------------------------------------------
    @property
    def name(self) -> str:
        return self._seg.name

    @property
    def uri(self) -> str:
        return self._seg.uri

    @property
    def buf(self) -> memoryview:
        return self._seg.buf

    @property
    def flag_offset(self) -> int:
        return _KILL_FLAG_OFFSET

    @property
    def is_halted(self) -> bool:
        """Single 32-bit read -- the hottest branch in the system."""
        return _U32.unpack_from(self._seg.buf, _KILL_FLAG_OFFSET)[0] == KILL_HALTED

    # -- state ------------------------------------------------------------
    def read(self) -> KillLatchState:
        buf = self._seg.buf
        for _ in range(64):
            seq_before = _U64.unpack_from(buf, _KILL_SLOT_OFFSET)[0]
            if seq_before & 1:
                continue
            _seq, updated_at, flag, reason = _KILL_SLOT.unpack_from(buf, _KILL_SLOT_OFFSET)
            if _U64.unpack_from(buf, _KILL_SLOT_OFFSET)[0] == seq_before:
                return KillLatchState(seq_before, flag, updated_at, reason)
        # Fail safe: an unreadable latch is treated as HALTED.
        return KillLatchState(-1, KILL_HALTED, 0, int(HaltReason.MANUAL_KILL_SWITCH))

    def _write(self, flag: int, reason_code: int, now_ns: int | None) -> KillLatchState:
        buf = self._seg.buf
        stamp = int(now_ns) if now_ns is not None else time.time_ns()
        for _ in range(64):
            seq = _U64.unpack_from(buf, _KILL_SLOT_OFFSET)[0]
            base = seq & ~1
            _U64.pack_into(buf, _KILL_SLOT_OFFSET, base + 1)
            _U64.pack_into(buf, _KILL_TIME_OFFSET, stamp)
            _U32.pack_into(buf, _KILL_FLAG_OFFSET, int(flag) & 0xFFFFFFFF)
            struct.pack_into("<i", buf, _KILL_REASON_OFFSET, int(reason_code))
            _U64.pack_into(buf, _KILL_SLOT_OFFSET, base + 2)
            self._seg.flush()
            return KillLatchState(base + 2, int(flag), stamp, int(reason_code))
        raise RuntimeError("kill latch write failed: sequence never stabilised")

    def halt(
        self,
        reason_code: HaltReason | int = HaltReason.MANUAL_KILL_SWITCH,
        now_ns: int | None = None,
    ) -> KillLatchState:
        """Flip the latch to ``HALTED`` (``POST /api/v2/control/killswitch``)."""
        return self._write(KILL_HALTED, int(reason_code), now_ns)

    def arm(self, now_ns: int | None = None) -> KillLatchState:
        """Re-arm the system.  Requires an explicit human reset (``sec_011``)."""
        return self._write(KILL_ARMED, int(HaltReason.NONE), now_ns)

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._seg.close()

    def unlink(self) -> None:
        self._seg.unlink()

    def __enter__(self) -> "GlobalKillLatch":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# System 1 lease table
# ---------------------------------------------------------------------------

LEASE_MAX_SYMBOLS = 5_000
LEASE_SLOT_SIZE = 128                     # seq + timestamp + 64B lease + pad
_LEASE_SLOT_HEADER = 16
_LEASE_PAYLOAD_OFFSET = _LEASE_SLOT_HEADER
_LEASE_TABLE_SIZE = _SEGMENT_HEADER_SIZE + LEASE_MAX_SYMBOLS * LEASE_SLOT_SIZE


@lru_cache(maxsize=16_384)
def _slot_index(symbol: str, n_slots: int) -> int:
    """Stable CRC32 symbol -> slot mapping (cached after first touch)."""
    return zlib.crc32(symbol.encode("utf-8")) % n_slots


class LeaseTableSHM:
    """``shm://nios-active-leases`` -- 5,000 single-writer SeqLock slots."""

    __slots__ = (
        "_seg",
        "_kill",
        "_kill_buf",
        "n_slots",
        "_slots_written",
        "_written_indices",
        "_lease_cache",
    )

    def __init__(
        self,
        reset: bool = False,
        kill_latch: GlobalKillLatch | None = None,
        n_slots: int = LEASE_MAX_SYMBOLS,
        name: str = LEASE_TABLE_SHM_NAME,
    ):
        if n_slots <= 0:
            raise ValueError("n_slots must be positive")
        self.n_slots = int(n_slots)
        size = _SEGMENT_HEADER_SIZE + self.n_slots * LEASE_SLOT_SIZE
        self._seg = _SharedSegment(name, size, _LEASE_MAGIC, reset=reset)
        self._slots_written = 0
        # In-process index of slots that have ever been written.  The hot kill
        # path uses this so an emergency sweep costs O(active leases), not
        # O(5,000) (``sec_004`` <1.0 ms).  It is an optimisation only: when the
        # set is empty but the segment shows activity, ``disarm_all`` falls back
        # to the authoritative full scan (another process wrote the lease).
        self._written_indices: set[int] = set()
        # Seq-keyed read cache: repeated reads of an unchanged slot return the
        # already-decoded lease instead of allocating a new one.  The SeqLock
        # sequence is the invalidation key, so a write can never serve stale
        # field values.
        self._lease_cache: dict[int, tuple[int, System1Lease]] = {}

        if kill_latch is None and segment_name(name) == segment_name(LEASE_TABLE_SHM_NAME):
            try:
                kill_latch = GlobalKillLatch()
            except Exception:  # pragma: no cover - degraded mode
                kill_latch = None
        self._kill = kill_latch
        self._kill_buf = kill_latch.buf if kill_latch is not None else None

    # -- properties -------------------------------------------------------
    @property
    def name(self) -> str:
        return self._seg.name

    @property
    def uri(self) -> str:
        return self._seg.uri

    @property
    def buf(self) -> memoryview:
        return self._seg.buf

    def slot_offset(self, index: int) -> int:
        return _SEGMENT_HEADER_SIZE + index * LEASE_SLOT_SIZE

    # -- write path (single writer: System 1 process) ----------------------
    def write_lease(self, lease: System1Lease, now_ns: int | None = None) -> int:
        """Publish ``lease`` under its symbol using the SeqLock protocol."""
        idx = _slot_index(lease.symbol, self.n_slots)
        off = self.slot_offset(idx)
        buf = self._seg.buf
        stamp = int(now_ns) if now_ns is not None else time.monotonic_ns()

        seq = _U64.unpack_from(buf, off)[0]
        base = seq & ~1
        _U64.pack_into(buf, off, base + 1)          # -> odd: read in progress
        _U64.pack_into(buf, off + 8, stamp)
        lease.pack_into(buf, off + _LEASE_PAYLOAD_OFFSET)
        _U64.pack_into(buf, off, base + 2)          # -> even: published
        # Header word #2 is a cross-process "ever written" counter.  The kill
        # path reads it in O(1) instead of scanning 5,000 empty slots.
        written = _U64.unpack_from(buf, 16)[0]
        _U64.pack_into(buf, 16, written + 1)
        self._seg.flush()
        self._slots_written += 1
        self._written_indices.add(idx)
        return idx

    def disarm(self, symbol: str, now_ns: int | None = None) -> int:
        return self.write_lease(System1Lease.disarmed(symbol), now_ns=now_ns)

    def disarm_all(self, now_ns: int | None = None) -> int:
        """Administrative sweep; the hot kill path uses the latch instead.

        Iterates the in-process written-slot index when it has entries, so a
        kill switch with one live lease does not pay for 5,000 empty slots.
        Falls back to the authoritative full scan when the index is empty but
        the segment has been written by another process.
        """
        count = 0
        if _U64.unpack_from(self._seg.buf, 16)[0] == 0:
            # No lease has ever been published to this segment (cross-process
            # header counter); the kill latch alone is the authoritative halt.
            return 0
        indices = (
            tuple(self._written_indices)
            if self._written_indices
            else range(self.n_slots)
        )
        for idx in indices:
            off = self.slot_offset(idx)
            seq = _U64.unpack_from(self._seg.buf, off)[0]
            if seq == 0:
                continue
            payload = System1Lease.unpack(self._seg.buf, off + _LEASE_PAYLOAD_OFFSET)
            if payload.symbol and payload.mode is not LeaseMode.DISARMED:
                self.disarm(payload.symbol, now_ns=now_ns)
                count += 1
        return count

    # -- read path (Ring 1 hot loop) ---------------------------------------
    def read_lease(
        self,
        symbol: str,
        now_ns: int | None = None,
        max_retries: int = 32,
    ) -> System1Lease:
        """Lock-free lease read; fails closed on contention or a hot latch."""
        kill_buf = self._kill_buf
        if kill_buf is not None and _U32.unpack_from(kill_buf, _KILL_FLAG_OFFSET)[0] == KILL_HALTED:
            return System1Lease.disarmed(symbol)

        if now_ns is None:
            now_ns = time.monotonic_ns()

        buf = self._seg.buf
        off = _SEGMENT_HEADER_SIZE + _slot_index(symbol, self.n_slots) * LEASE_SLOT_SIZE

        for _ in range(max_retries):
            seq_before = _U64.unpack_from(buf, off)[0]
            if seq_before & 1:
                continue
            cached = self._lease_cache.get(off)
            if cached is not None and cached[0] == seq_before:
                return cached[1]
            raw = struct.unpack_from("<16sB7xddQQ", buf, off + _LEASE_PAYLOAD_OFFSET)
            if _U64.unpack_from(buf, off)[0] != seq_before:
                continue
            if raw[0].split(b"\x00", 1)[0].decode("utf-8", errors="replace") != symbol:
                # Empty slot or CRC collision -> no authority to trade.
                return System1Lease.disarmed(symbol)
            lease = System1Lease(
                symbol,
                LeaseMode(raw[1]),
                raw[2],
                raw[3],
                raw[4],
                raw[5],
            )
            self._lease_cache[off] = (seq_before, lease)
            return lease

        return System1Lease.disarmed(symbol)

    def read_lease_slot(self, index: int) -> tuple[int, System1Lease | None]:
        """Diagnostic read of a raw slot (used by the Cockpit X-ray tab)."""
        off = self.slot_offset(index)
        buf = self._seg.buf
        seq = _U64.unpack_from(buf, off)[0]
        if seq == 0:
            return 0, None
        return seq, System1Lease.unpack(buf, off + _LEASE_PAYLOAD_OFFSET)

    def iter_active_leases(self, now_ns: int) -> Iterator[System1Lease]:
        for idx in range(self.n_slots):
            seq, lease = self.read_lease_slot(idx)
            if lease is None or not lease.symbol:
                continue
            if lease.is_active(now_ns):
                yield lease

    def active_count(self, now_ns: int) -> int:
        return sum(1 for _ in self.iter_active_leases(now_ns))

    # -- lifecycle --------------------------------------------------------
    @property
    def slots_written(self) -> int:
        return self._slots_written

    def close(self) -> None:
        self._seg.close()

    def unlink(self) -> None:
        self._seg.unlink()

    def __enter__(self) -> "LeaseTableSHM":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 5-second rolling executed OFI / cancel-to-fill ring buffer
# ---------------------------------------------------------------------------

OFI_RING_SLOTS = 4_096
OFI_SLOT_SIZE = 128
OFI_RING_SIZE = _SEGMENT_HEADER_SIZE + OFI_RING_SLOTS * OFI_SLOT_SIZE

#: symbol | valid_time_ns | knowledge_ts_ns | 8 x f64 (96 bytes)
_OFI_TICK = struct.Struct("<16sQQdddddddd")
assert _OFI_TICK.size == 96

_OFI_CURSOR_OFFSET = 32  # inside the segment header's reserved bytes


class OfiRingBuffer:
    """Fixed-size ring of :class:`ExecutedFlowTick` frames (``sec_009``).

    One writer (the Ring 1 normalizer), many readers.  Each slot is a
    independent SeqLock so a reader never observes a torn tick.  The ring is
    intentionally *lossy*: at 25,000 ticks/sec/core a 4,096-slot ring still
    holds far more than the 5-second window the gates need.
    """

    __slots__ = ("_seg", "n_slots")

    def __init__(
        self,
        reset: bool = False,
        n_slots: int = OFI_RING_SLOTS,
        name: str = OFI_RING_SHM_NAME,
    ):
        if n_slots <= 0:
            raise ValueError("n_slots must be positive")
        self.n_slots = int(n_slots)
        size = _SEGMENT_HEADER_SIZE + self.n_slots * OFI_SLOT_SIZE
        self._seg = _SharedSegment(name, size, _OFI_MAGIC, reset=reset)

    @property
    def uri(self) -> str:
        return self._seg.uri

    @property
    def cursor(self) -> int:
        return _U64.unpack_from(self._seg.buf, _OFI_CURSOR_OFFSET)[0]

    def _slot_offset(self, index: int) -> int:
        return _SEGMENT_HEADER_SIZE + index * OFI_SLOT_SIZE

    def append(self, tick: ExecutedFlowTick, now_ns: int | None = None) -> int:
        buf = self._seg.buf
        cursor = _U64.unpack_from(buf, _OFI_CURSOR_OFFSET)[0]
        idx = cursor % self.n_slots
        off = self._slot_offset(idx)
        stamp = int(now_ns) if now_ns is not None else time.monotonic_ns()

        seq = _U64.unpack_from(buf, off)[0]
        base = seq & ~1
        _U64.pack_into(buf, off, base + 1)
        _U64.pack_into(buf, off + 8, stamp)
        _OFI_TICK.pack_into(
            buf,
            off + 16,
            tick.symbol.encode("utf-8")[:16],
            int(tick.valid_time_ns),
            int(tick.knowledge_ts_ns),
            float(tick.last_price),
            float(tick.spread_bps),
            float(tick.realized_vol_5s),
            float(tick.normal_vol_5s),
            float(tick.taker_buy_volume_5s),
            float(tick.taker_sell_volume_5s),
            float(tick.bid_cancel_to_fill_ratio),
            float(tick.ask_cancel_to_fill_ratio),
        )
        _U64.pack_into(buf, off, base + 2)
        _U64.pack_into(buf, _OFI_CURSOR_OFFSET, cursor + 1)
        return idx

    def _read_slot(self, index: int) -> ExecutedFlowTick | None:
        buf = self._seg.buf
        off = self._slot_offset(index)
        for _ in range(32):
            seq_before = _U64.unpack_from(buf, off)[0]
            if seq_before & 1:
                continue
            raw = _OFI_TICK.unpack_from(buf, off + 16)
            if _U64.unpack_from(buf, off)[0] != seq_before:
                continue
            symbol = raw[0].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            if not symbol:
                return None
            return ExecutedFlowTick(
                symbol=symbol,
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
        return None

    def latest(self) -> ExecutedFlowTick | None:
        cursor = self.cursor
        if cursor == 0:
            return None
        return self._read_slot((cursor - 1) % self.n_slots)

    def window(self, now_ns: int, window_ns: int = 5_000_000_000) -> list[ExecutedFlowTick]:
        """Newest-first ticks inside the 5-second rolling window."""
        cursor = self.cursor
        cutoff = int(now_ns) - int(window_ns)
        out: list[ExecutedFlowTick] = []
        for back in range(min(cursor, self.n_slots)):
            tick = self._read_slot((cursor - 1 - back) % self.n_slots)
            if tick is None:
                break
            if tick.valid_time_ns < cutoff:
                break
            out.append(tick)
        return out

    def aggregate(self, symbol: str, now_ns: int, window_ns: int = 5_000_000_000) -> ExecutedFlowTick | None:
        """Fold the ring into a single synthetic tick for ``symbol``."""
        ticks = [t for t in self.window(now_ns, window_ns) if t.symbol == symbol]
        if not ticks:
            return None
        buy = sum(t.taker_buy_volume_5s for t in ticks)
        sell = sum(t.taker_sell_volume_5s for t in ticks)
        bid_c2f = max(t.bid_cancel_to_fill_ratio for t in ticks)
        ask_c2f = max(t.ask_cancel_to_fill_ratio for t in ticks)
        newest = ticks[0]
        return ExecutedFlowTick(
            symbol=symbol,
            valid_time_ns=newest.valid_time_ns,
            knowledge_ts_ns=max(t.knowledge_ts_ns for t in ticks),
            last_price=newest.last_price,
            spread_bps=max(t.spread_bps for t in ticks),
            realized_vol_5s=newest.realized_vol_5s,
            normal_vol_5s=newest.normal_vol_5s,
            taker_buy_volume_5s=buy,
            taker_sell_volume_5s=sell,
            bid_cancel_to_fill_ratio=bid_c2f,
            ask_cancel_to_fill_ratio=ask_c2f,
        )

    def close(self) -> None:
        self._seg.close()

    def unlink(self) -> None:
        self._seg.unlink()

    def __enter__(self) -> "OfiRingBuffer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# L2 shadow twin book snapshot
# ---------------------------------------------------------------------------

_BOOK_SLOT = struct.Struct("<QddddQ16x")   # seq | bid | ask | bid_qty | ask_qty | ts
assert _BOOK_SLOT.size == CACHE_LINE

SHADOW_BOOK_MAX_SYMBOLS = 5_000
_SHADOW_BOOK_SIZE = _SEGMENT_HEADER_SIZE + SHADOW_BOOK_MAX_SYMBOLS * CACHE_LINE


class ShadowBookSnapshot(NamedTuple):
    symbol: str
    best_bid: float
    best_ask: float
    bid_qty: float
    ask_qty: float
    updated_at_ns: int

    @property
    def mid(self) -> float:
        if self.best_bid <= 0.0 or self.best_ask <= 0.0:
            return 0.0
        return 0.5 * (self.best_bid + self.best_ask)

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid <= 0.0:
            return 0.0
        return (self.best_ask - self.best_bid) / mid * 10_000.0


class ShadowBookSHM:
    """``shm://nios-shadow-book`` -- last shadow-twin L2 snapshot per symbol.

    The shadow twin is where every strategy must survive 30 L2 fills before it
    may touch live capital (Law 4).  This segment is the cheap, lock-free
    carrier for its most recent top-of-book state.
    """

    __slots__ = ("_seg", "n_slots")

    def __init__(
        self,
        reset: bool = False,
        n_slots: int = SHADOW_BOOK_MAX_SYMBOLS,
        name: str = SHADOW_BOOK_SHM_NAME,
    ):
        self.n_slots = int(n_slots)
        size = _SEGMENT_HEADER_SIZE + self.n_slots * CACHE_LINE
        self._seg = _SharedSegment(name, size, _BOOK_MAGIC, reset=reset)

    @property
    def uri(self) -> str:
        return self._seg.uri

    def _slot_offset(self, symbol: str) -> int:
        return _SEGMENT_HEADER_SIZE + _slot_index(symbol, self.n_slots) * CACHE_LINE

    def write_snapshot(
        self,
        symbol: str,
        best_bid: float,
        best_ask: float,
        bid_qty: float,
        ask_qty: float,
        updated_at_ns: int | None = None,
    ) -> None:
        buf = self._seg.buf
        off = self._slot_offset(symbol)
        stamp = int(updated_at_ns) if updated_at_ns is not None else time.time_ns()
        seq = _U64.unpack_from(buf, off)[0]
        base = seq & ~1
        _U64.pack_into(buf, off, base + 1)
        struct.pack_into(
            "<ddddQ",
            buf,
            off + 8,
            float(best_bid),
            float(best_ask),
            float(bid_qty),
            float(ask_qty),
            stamp,
        )
        _U64.pack_into(buf, off, base + 2)

    def read_snapshot(self, symbol: str) -> ShadowBookSnapshot | None:
        buf = self._seg.buf
        off = self._slot_offset(symbol)
        for _ in range(32):
            seq_before = _U64.unpack_from(buf, off)[0]
            if seq_before & 1:
                continue
            _seq, bid, ask, bid_qty, ask_qty, stamp = _BOOK_SLOT.unpack_from(buf, off)
            if _U64.unpack_from(buf, off)[0] != seq_before:
                continue
            if bid <= 0.0 and ask <= 0.0:
                return None
            return ShadowBookSnapshot(symbol, bid, ask, bid_qty, ask_qty, stamp)
        return None

    def close(self) -> None:
        self._seg.close()

    def unlink(self) -> None:
        self._seg.unlink()

    def __enter__(self) -> "ShadowBookSHM":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

ALL_SEGMENTS = (
    KILL_LATCH_SHM_NAME,
    LEASE_TABLE_SHM_NAME,
    OFI_RING_SHM_NAME,
    SHADOW_BOOK_SHM_NAME,
)


def unlink_all() -> None:
    """Drop every NIOS Tier-0 segment in the active namespace.

    Used for crash recovery and test teardown.  With a test prefix set this is
    guaranteed not to touch a live server's segments.
    """
    for name in ALL_SEGMENTS:
        _unlink_quiet(segment_name(name))
