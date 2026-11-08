"""NIOS Core v3.0 -- Tier 1 NATS JetStream durable event bus (STEP 2).

``sec_009`` fixes exactly four streams::

    stream.ring1.scalp-proposals      Ring 1 -> Ring 0 unsigned scalps
    stream.ring2.deep-theses          Ring 2 committee theses
    stream.ring0.execution-events     Ring 0 fills / rejects / outbox events
    stream.ring3.cockpit-cards        Ring 3 SSE telemetry fan-out

Delivery contract: **at-least-once + deterministic idempotency key**.  Every
producer supplies (or derives) a ``Nats-Msg-Id``; the broker de-duplicates
inside its duplicate window, so a producer that retries after a timeout can
never double-submit an order intent.

Two backends, one API
---------------------
:class:`LocalJetStreamBus`
    In-process, replayable, crash-safe-to-disk loopback bus.  It is selected
    **automatically** whenever no ``nats-server`` daemon answers on
    ``127.0.0.1:4222``, which is the normal state on a developer workstation
    such as ``C:\\Users\\nahas\\OneDrive\\Desktop\\NIOS``.  It implements the
    same idempotency and replay semantics as JetStream, including durable
    consumers with explicit ``ack``/``nack`` redelivery.
:class:`JetStreamBus`
    Thin async facade.  ``await bus.connect()`` probes for a live daemon and
    transparently falls back to :class:`LocalJetStreamBus`.

Nothing in this module imports a broker adapter or a credential store; Ring 1
and Ring 2 only ever see subjects and JSON payloads (``sec_011``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple
from urllib.parse import urlparse

__all__ = [
    "STREAM_RING0_EXECUTION",
    "STREAM_RING1_SCALP",
    "STREAM_RING2_THESES",
    "STREAM_RING3_COCKPIT",
    "STREAMS",
    "STREAM_SUBJECT_PATTERNS",
    "UnknownStreamError",
    "PublishReceipt",
    "StreamEvent",
    "DurableConsumer",
    "LocalJetStreamBus",
    "JetStreamBus",
    "open_bus",
    "resolve_stream",
    "detect_local_nats_server",
]

STREAM_RING0_EXECUTION = "stream.ring0.execution-events"
STREAM_RING1_SCALP = "stream.ring1.scalp-proposals"
STREAM_RING2_THESES = "stream.ring2.deep-theses"
STREAM_RING3_COCKPIT = "stream.ring3.cockpit-cards"

STREAMS: tuple[str, ...] = (
    STREAM_RING0_EXECUTION,
    STREAM_RING1_SCALP,
    STREAM_RING2_THESES,
    STREAM_RING3_COCKPIT,
)

#: NATS wildcard pattern captured by each stream.
STREAM_SUBJECT_PATTERNS: Mapping[str, str] = {
    STREAM_RING0_EXECUTION: f"{STREAM_RING0_EXECUTION}.>",
    STREAM_RING1_SCALP: f"{STREAM_RING1_SCALP}.>",
    STREAM_RING2_THESES: f"{STREAM_RING2_THESES}.>",
    STREAM_RING3_COCKPIT: f"{STREAM_RING3_COCKPIT}.>",
}

_RING_PREFIX_TO_STREAM: Mapping[str, str] = {
    "ring0": STREAM_RING0_EXECUTION,
    "ring1": STREAM_RING1_SCALP,
    "ring2": STREAM_RING2_THESES,
    "ring3": STREAM_RING3_COCKPIT,
}


class UnknownStreamError(ValueError):
    """Raised when a subject cannot be mapped onto one of the four streams."""


def resolve_stream(subject: str) -> str:
    """Map an arbitrary subject onto its owning stream.

    Accepts either the stream name itself (optionally suffixed), or a bare
    ``ringN.<entity>`` subject.
    """
    token = str(subject).strip()
    if not token:
        raise UnknownStreamError("empty subject")
    for stream in STREAMS:
        if token == stream or token.startswith(stream + "."):
            return stream
    head, _, _ = token.partition(".")
    stream = _RING_PREFIX_TO_STREAM.get(head.lower())
    if stream is None:
        raise UnknownStreamError(
            f"subject {subject!r} does not belong to any NIOS JetStream stream"
        )
    return stream


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class PublishReceipt(NamedTuple):
    """Result of an idempotent publish."""

    stream: str
    subject: str
    sequence: int
    idempotency_key: str
    duplicate: bool
    committed: bool
    stored_at_ns: int
    payload_sha256: str

    @property
    def committed_count(self) -> int:
        """``1`` for a fresh commit, ``0`` for a de-duplicated retry."""
        return 1 if self.committed else 0


@dataclass(slots=True, frozen=True)
class StreamEvent:
    """One immutable, append-only JetStream message."""

    stream: str
    subject: str
    sequence: int
    payload: Mapping[str, Any]
    idempotency_key: str
    payload_sha256: str
    stored_at_ns: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "stream": self.stream,
                "subject": self.subject,
                "seq": self.sequence,
                "key": self.idempotency_key,
                "sha256": self.payload_sha256,
                "ts": self.stored_at_ns,
                "payload": dict(self.payload),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @classmethod
    def from_json(cls, line: str) -> "StreamEvent":
        raw = json.loads(line)
        return cls(
            stream=raw["stream"],
            subject=raw["subject"],
            sequence=int(raw["seq"]),
            payload=raw["payload"],
            idempotency_key=raw["key"],
            payload_sha256=raw["sha256"],
            stored_at_ns=int(raw["ts"]),
        )


def _canonical_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(json_text, sha256)`` with a stable key order."""
    text = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), default=str
    )
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Durable consumer
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ConsumerState:
    cursor: int = 0
    pending: dict[int, StreamEvent] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.pending is None:
            self.pending = {}


class DurableConsumer:
    """At-least-once consumer with explicit ack/nack and redelivery."""

    __slots__ = ("_bus", "stream", "name", "_state")

    def __init__(self, bus: "LocalJetStreamBus", stream: str, name: str):
        if stream not in STREAMS:
            raise UnknownStreamError(f"unknown stream {stream!r}")
        self._bus = bus
        self.stream = stream
        self.name = str(name)
        self._state = bus._consumer_state(stream, self.name)

    @property
    def cursor(self) -> int:
        return self._state.cursor

    @property
    def pending_count(self) -> int:
        return len(self._state.pending)

    def fetch(self, max_msgs: int = 100) -> list[StreamEvent]:
        """Unacked redeliveries first, then up to ``max_msgs`` new events."""
        if max_msgs <= 0:
            return []
        state = self._state
        out: list[StreamEvent] = []
        for seq in sorted(state.pending):
            if len(out) >= max_msgs:
                return out
            out.append(state.pending[seq])
        seen = set(state.pending)
        for event in self._bus.replay(
            self.stream, from_sequence=state.cursor + 1
        ):
            if len(out) >= max_msgs:
                break
            if event.sequence in seen:
                continue
            state.pending[event.sequence] = event
            seen.add(event.sequence)
            out.append(event)
        return out

    def ack(self, sequence: int) -> None:
        """Acknowledge a message; contiguous acks advance the durable cursor."""
        state = self._state
        state.pending.pop(int(sequence), None)
        while True:
            candidate = state.cursor + 1
            if candidate in state.pending:
                break
            event = self._bus.get_event(self.stream, candidate)
            if event is None:
                break
            state.cursor = candidate

    def nack(self, sequence: int) -> None:
        """Drop the pending marker so the message is redelivered."""
        self._state.pending.pop(int(sequence), None)

    def ack_all(self) -> int:
        acked = 0
        for seq in sorted(self._state.pending):
            self.ack(seq)
            acked += 1
        return acked


# ---------------------------------------------------------------------------
# In-process durable loopback bus
# ---------------------------------------------------------------------------


class LocalJetStreamBus:
    """Replayable, idempotent, in-process JetStream substitute.

    Semantics mirror the real broker:

    * append-only per-stream log with monotonically increasing sequences;
    * ``Nats-Msg-Id`` de-duplication inside a duplicate window (kept in a
      dict, so it is strictly stronger than the default 2-minute broker
      window);
    * replay from any sequence;
    * durable consumers with redelivery of unacked messages;
    * optional JSONL persistence so a restart replays the same log.
    """

    backend = "local-jetstream"
    is_local = True

    def __init__(
        self,
        path: str | Path | None = None,
        max_msgs_per_stream: int = 100_000,
        fsync: bool = False,
    ):
        self._lock = threading.RLock()
        self._logs: dict[str, list[StreamEvent]] = {s: [] for s in STREAMS}
        self._dedupe: dict[str, dict[str, StreamEvent]] = {s: {} for s in STREAMS}
        self._next_seq: dict[str, int] = {s: 0 for s in STREAMS}
        self._consumers: dict[tuple[str, str], _ConsumerState] = {}
        self._dup_count: dict[str, int] = {s: 0 for s in STREAMS}
        self._max_msgs = int(max_msgs_per_stream)
        self._fsync = bool(fsync)
        self._path = Path(path) if path is not None else None
        self._files: dict[str, Any] = {}
        if self._path is not None:
            self._path.mkdir(parents=True, exist_ok=True)
            self._load()

    # -- persistence ------------------------------------------------------
    def _stream_path(self, stream: str) -> Path:
        assert self._path is not None
        return self._path / f"{stream}.jsonl"

    def _load(self) -> None:
        assert self._path is not None
        for stream in STREAMS:
            file_path = self._stream_path(stream)
            if not file_path.exists():
                continue
            with file_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = StreamEvent.from_json(line)
                    except Exception:  # pragma: no cover - corrupt tail
                        continue
                    self._logs[stream].append(event)
                    self._dedupe[stream][event.idempotency_key] = event
                    self._next_seq[stream] = max(
                        self._next_seq[stream], event.sequence
                    )

    def _persist(self, event: StreamEvent) -> None:
        if self._path is None:
            return
        handle = self._files.get(event.stream)
        if handle is None:
            handle = self._stream_path(event.stream).open("a", encoding="utf-8")
            self._files[event.stream] = handle
        handle.write(event.to_json() + "\n")
        handle.flush()
        if self._fsync:
            try:
                import os

                os.fsync(handle.fileno())
            except Exception:  # pragma: no cover
                pass

    # -- publish ----------------------------------------------------------
    def publish_idempotent(
        self,
        subject: str,
        payload: Mapping[str, Any],
        idempotency_key: str | None = None,
        stream: str | None = None,
        stored_at_ns: int | None = None,
    ) -> PublishReceipt:
        """Append ``payload`` exactly once per ``idempotency_key``.

        Re-publishing the same key returns the original sequence with
        ``duplicate=True`` and never creates a second committed event -- this
        is what makes Ring 1 -> Ring 0 retries safe.
        """
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        resolved = stream or resolve_stream(subject)
        if resolved not in STREAMS:
            raise UnknownStreamError(f"unknown stream {resolved!r}")

        _text, digest = _canonical_payload(payload)
        key = (idempotency_key or f"sha256:{digest}").strip()
        if not key:
            raise ValueError("idempotency_key must be a non-empty string")

        with self._lock:
            existing = self._dedupe[resolved].get(key)
            if existing is not None:
                self._dup_count[resolved] = self._dup_count.get(resolved, 0) + 1
                return PublishReceipt(
                    stream=resolved,
                    subject=existing.subject,
                    sequence=existing.sequence,
                    idempotency_key=key,
                    duplicate=True,
                    committed=False,
                    stored_at_ns=existing.stored_at_ns,
                    payload_sha256=existing.payload_sha256,
                )

            sequence = self._next_seq[resolved] + 1
            event = StreamEvent(
                stream=resolved,
                subject=str(subject),
                sequence=sequence,
                payload=dict(payload),
                idempotency_key=key,
                payload_sha256=digest,
                stored_at_ns=int(stored_at_ns) if stored_at_ns is not None else time.time_ns(),
            )
            self._logs[resolved].append(event)
            self._dedupe[resolved][key] = event
            self._next_seq[resolved] = sequence
            self._persist(event)
            self._trim(resolved)
            return PublishReceipt(
                stream=resolved,
                subject=event.subject,
                sequence=sequence,
                idempotency_key=key,
                duplicate=False,
                committed=True,
                stored_at_ns=event.stored_at_ns,
                payload_sha256=digest,
            )

    def publish_many(
        self,
        stream: str,
        messages: Iterable[tuple[str, Mapping[str, Any], str]],
    ) -> list[PublishReceipt]:
        """Batch helper; each tuple is ``(subject, payload, idempotency_key)``."""
        return [
            self.publish_idempotent(subject, payload, key, stream=stream)
            for subject, payload, key in messages
        ]

    def _trim(self, stream: str) -> None:
        log = self._logs[stream]
        excess = len(log) - self._max_msgs
        if excess > 0:
            del log[:excess]

    # -- read -------------------------------------------------------------
    def replay(
        self,
        stream: str,
        from_sequence: int = 1,
        to_sequence: int | None = None,
        limit: int | None = None,
    ) -> list[StreamEvent]:
        if stream not in STREAMS:
            raise UnknownStreamError(f"unknown stream {stream!r}")
        start = max(1, int(from_sequence))
        with self._lock:
            log = self._logs[stream]
            out: list[StreamEvent] = []
            for event in log:
                if event.sequence < start:
                    continue
                if to_sequence is not None and event.sequence > to_sequence:
                    break
                out.append(event)
                if limit is not None and len(out) >= limit:
                    break
            return out

    def get_event(self, stream: str, sequence: int) -> StreamEvent | None:
        if stream not in STREAMS:
            raise UnknownStreamError(f"unknown stream {stream!r}")
        with self._lock:
            for event in self._logs[stream]:
                if event.sequence == int(sequence):
                    return event
        return None

    def stream_depth(self, stream: str) -> int:
        if stream not in STREAMS:
            raise UnknownStreamError(f"unknown stream {stream!r}")
        with self._lock:
            return len(self._logs[stream])

    def committed_count(self, stream: str) -> int:
        """Total committed events (== ``last_sequence`` for an untrimmed log)."""
        if stream not in STREAMS:
            raise UnknownStreamError(f"unknown stream {stream!r}")
        with self._lock:
            return self._next_seq[stream]

    def last_sequence(self, stream: str) -> int:
        return self.committed_count(stream)

    def dedupe_hits(self, stream: str) -> int:
        """How many publishes were suppressed as duplicates."""
        if stream not in STREAMS:
            raise UnknownStreamError(f"unknown stream {stream!r}")
        with self._lock:
            return self._dup_count.get(stream, 0)

    def consumer(self, stream: str, name: str) -> DurableConsumer:
        return DurableConsumer(self, stream, name)

    def _consumer_state(self, stream: str, name: str) -> _ConsumerState:
        with self._lock:
            key = (stream, name)
            state = self._consumers.get(key)
            if state is None:
                state = _ConsumerState()
                self._consumers[key] = state
            return state

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            for handle in self._files.values():
                try:
                    handle.close()
                except Exception:  # pragma: no cover
                    pass
            self._files.clear()

    def __enter__(self) -> "LocalJetStreamBus":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Live NATS JetStream facade with automatic loopback fallback
# ---------------------------------------------------------------------------


def detect_local_nats_server(
    host: str = "127.0.0.1", port: int = 4222, timeout_s: float = 0.25
) -> bool:
    """Return ``True`` when a ``nats-server`` daemon answers a TCP probe."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True
    except OSError:
        return False


class JetStreamBus:
    """Async facade over a live NATS server, with automatic local fallback."""

    def __init__(
        self,
        url: str = "nats://127.0.0.1:4222",
        local_path: str | Path | None = None,
        local_bus: LocalJetStreamBus | None = None,
    ):
        self.url = url
        self.local_path = local_path
        self.backend: LocalJetStreamBus | None = local_bus
        self.mode = "local-jetstream" if local_bus is not None else "uninitialised"
        self._nc = None
        self._js = None
        self._lock = asyncio.Lock()

    # -- connection -------------------------------------------------------
    async def connect(self) -> "JetStreamBus":
        async with self._lock:
            if self.mode == "nats-jetstream" and self._js is not None:
                return self
            if self.backend is not None:
                return self

            parsed = urlparse(self.url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 4222
            if detect_local_nats_server(host, port):
                try:
                    import nats  # type: ignore
                    from nats.js.api import StreamConfig  # type: ignore

                    self._nc = await nats.connect(
                        self.url, connect_timeout=2.0, max_reconnect_attempts=2
                    )
                    self._js = self._nc.jetstream()
                    for stream, pattern in STREAM_SUBJECT_PATTERNS.items():
                        try:
                            await self._js.add_stream(
                                config=StreamConfig(
                                    name=stream,
                                    subjects=[pattern],
                                    duplicate_window=120_000_000_000,
                                )
                            )
                        except Exception:  # pragma: no cover - stream exists
                            pass
                    self.mode = "nats-jetstream"
                    return self
                except Exception:  # pragma: no cover - fall back to loopback
                    self._js = None
                    try:
                        if self._nc is not None:
                            await self._nc.close()
                    except Exception:
                        pass
                    self._nc = None

            self.backend = LocalJetStreamBus(path=self.local_path)
            self.mode = "local-jetstream"
            return self

    @property
    def is_local(self) -> bool:
        return self.mode != "nats-jetstream"

    # -- publish ----------------------------------------------------------
    async def publish_idempotent(
        self,
        subject: str,
        payload: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> PublishReceipt:
        if self.mode == "uninitialised":
            await self.connect()
        if self.mode == "nats-jetstream" and self._js is not None:  # pragma: no cover
            _text, digest = _canonical_payload(payload)
            key = (idempotency_key or f"sha256:{digest}").strip()
            data = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()
            ack = await self._js.publish(subject, data, headers={"Nats-Msg-Id": key})
            duplicate = bool(getattr(ack, "duplicate", False))
            return PublishReceipt(
                stream=resolve_stream(subject),
                subject=subject,
                sequence=int(getattr(ack, "seq", 0) or 0),
                idempotency_key=key,
                duplicate=duplicate,
                committed=not duplicate,
                stored_at_ns=time.time_ns(),
                payload_sha256=digest,
            )
        assert self.backend is not None
        return self.backend.publish_idempotent(subject, payload, idempotency_key)

    def publish_idempotent_sync(
        self,
        subject: str,
        payload: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> PublishReceipt:
        """Sync convenience for callers that already own the fallback bus."""
        if self.backend is None:
            self.backend = LocalJetStreamBus(path=self.local_path)
            self.mode = "local-jetstream"
        return self.backend.publish_idempotent(subject, payload, idempotency_key)

    # -- passthroughs -----------------------------------------------------
    def replay(self, stream: str, from_sequence: int = 1, limit: int | None = None) -> list[StreamEvent]:
        return self._require_local().replay(stream, from_sequence, limit=limit)

    def consumer(self, stream: str, name: str) -> DurableConsumer:
        return self._require_local().consumer(stream, name)

    def stream_depth(self, stream: str) -> int:
        return self._require_local().stream_depth(stream)

    def committed_count(self, stream: str) -> int:
        return self._require_local().committed_count(stream)

    def _require_local(self) -> LocalJetStreamBus:
        if self.backend is None:
            self.backend = LocalJetStreamBus(path=self.local_path)
            self.mode = "local-jetstream"
        return self.backend

    # -- lifecycle --------------------------------------------------------
    async def close(self) -> None:
        if self._nc is not None:  # pragma: no cover
            try:
                await self._nc.drain()
            except Exception:
                pass
            self._nc = None
        if self.backend is not None:
            self.backend.close()


async def open_bus(
    url: str = "nats://127.0.0.1:4222",
    local_path: str | Path | None = None,
) -> JetStreamBus:
    """Return a connected bus: live JetStream if available, else loopback."""
    bus = JetStreamBus(url=url, local_path=local_path)
    await bus.connect()
    return bus
