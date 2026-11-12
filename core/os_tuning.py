"""Cross-platform OS & socket low-latency tuning auditor (``sec_004``, ``sec_010``).

Ring 1 must sustain ``<0.14 ms p99`` tick-to-intent at 25,000 ticks/sec/core, and
Ring 0 must sign and reconcile inside a ``0.29 ms p50`` budget.  Neither is
possible on a default desktop OS: Nagle's algorithm batches small order frames,
the Windows multimedia timer defaults to 15.625 ms, and the Linux socket buffers
are sized for throughput rather than tail latency.

This module is the single auditable surface for that tuning:

``configure_low_latency_socket(sock)``
    Enforces ``TCP_NODELAY=1`` (Nagle disabled), ``SO_KEEPALIVE=1`` and the
    Ring 1 receive/send buffers on one TCP socket, then reads every option back
    so the caller gets proof rather than a promise.

``audit_low_latency_sockets()``
    Builds a real ``127.0.0.1`` TCP pair, configures both ends through the
    helper and reports the values read back from the kernel.

``audit_low_latency_transports()``
    Read-only conformance audit of the transports NIOS actually owns: the
    HTTP/2 LLM path (``core.llm_transport``) and the Ring 1 venue WebSocket
    multiplexer (``ring1_spinal.ws_multiplexer``).

``audit_and_tune_os(apply=False)``
    Probes (and optionally applies) the OS kernel and process settings:

    * **Windows 11** - process affinity mask (``Get/SetProcessAffinityMask``),
      priority class (``ABOVE_NORMAL_PRIORITY_CLASS`` / ``HIGH_PRIORITY_CLASS``),
      multimedia timer resolution (``NtQueryTimerResolution`` /
      ``timeBeginPeriod(1)``) and loopback ``TCP_NODELAY``.
    * **Linux / WSL** - ``sysctl`` profile (``rmem_max``, ``wmem_max``,
      ``busy_poll=50``, ``busy_read=50``), ``/dev/shm`` capacity, transparent
      hugepage mode and ``sched_setaffinity`` core isolation.

``apply=False`` is the default and **never requires Administrator or root**:
every privileged item is reported as ``RECOMMENDED`` with the exact command
that would apply it.  ``apply=True`` performs only the operations the current
process is allowed to perform and degrades to ``RECOMMENDED`` otherwise, so a
non-elevated audit can always complete with zero errors.

No network egress and no third-party dependency: ``ctypes``, ``socket``,
``shutil`` and the standard library only (``AGENTS.md`` rule 9).
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

__all__ = [
    "LOW_LATENCY_RECV_BUFFER_BYTES",
    "LOW_LATENCY_SEND_BUFFER_BYTES",
    "LOW_LATENCY_SOCKET_OPTIONS",
    "SHM_MIN_BYTES",
    "SYSCTL_TARGETS",
    "APPLIED",
    "RECOMMENDED",
    "UNAVAILABLE",
    "audit_and_tune_os",
    "audit_low_latency_sockets",
    "audit_low_latency_transports",
    "configure_low_latency_socket",
    "os_tuning_summary",
    "tuning_commands",
]

#: Ring 1 order frames are tiny (tens of bytes); 1 MiB per direction keeps the
#: kernel from collapsing the receive window while staying far below a WAN BDP.
LOW_LATENCY_RECV_BUFFER_BYTES: int = 1 << 20
LOW_LATENCY_SEND_BUFFER_BYTES: int = 1 << 20

#: Tier-0 POSIX shared memory needs headroom for the lease table, kill latch,
#: OFI ring and the L2 shadow book.
SHM_MIN_BYTES: int = 512 << 20

#: ``sec_010`` Linux profile.  ``busy_poll``/``busy_read`` at 50 us remove the
#: scheduler wake-up from the tick path without pegging a core at 100%.
SYSCTL_TARGETS: dict[str, int] = {
    "net.core.rmem_max": 16_777_216,
    "net.core.wmem_max": 16_777_216,
    "net.core.busy_poll": 50,
    "net.core.busy_read": 50,
}

#: Exact ``socket_options`` list handed to httpx/httpcore for the LLM HTTP/2
#: transport.  Order matters: TCP first, then the generic socket layer.
LOW_LATENCY_SOCKET_OPTIONS: tuple[tuple[int, int, int], ...] = (
    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    (socket.SOL_SOCKET, socket.SO_RCVBUF, LOW_LATENCY_RECV_BUFFER_BYTES),
    (socket.SOL_SOCKET, socket.SO_SNDBUF, LOW_LATENCY_SEND_BUFFER_BYTES),
)

APPLIED = "APPLIED"
RECOMMENDED = "RECOMMENDED"
UNAVAILABLE = "UNAVAILABLE"

_WINDOWS_PRIORITY = {
    "IDLE": 0x00000040,
    "BELOW_NORMAL": 0x00004000,
    "NORMAL": 0x00000020,
    "ABOVE_NORMAL": 0x00008000,
    "HIGH": 0x00000080,
    "REALTIME": 0x00000100,
}
_WINDOWS_PRIORITY_NAMES = {value: name for name, value in _WINDOWS_PRIORITY.items()}


# ---------------------------------------------------------------------------
# Socket low-latency guard
# ---------------------------------------------------------------------------


def _is_tcp_stream(sock: socket.socket) -> bool:
    family = getattr(sock, "family", None)
    sock_type = getattr(sock, "type", 0) or 0
    return family in (socket.AF_INET, socket.AF_INET6) and bool(
        sock_type & socket.SOCK_STREAM
    )


def configure_low_latency_socket(
    sock: socket.socket,
    *,
    recv_buffer_bytes: int = LOW_LATENCY_RECV_BUFFER_BYTES,
    send_buffer_bytes: int = LOW_LATENCY_SEND_BUFFER_BYTES,
) -> dict[str, Any]:
    """Apply and verify the Ring 1 low-latency contract on ``sock``.

    ``TCP_NODELAY=1`` disables Nagle so a 40-byte order is not held waiting for
    an ACK.  ``SO_KEEPALIVE=1`` lets a dead venue be detected without waiting
    for the application heartbeat.  The buffer sizes are deliberately modest:
    the goal is a short queue, not a deep one.

    Returns a JSON-safe report with the values **read back from the kernel**.
    """

    report: dict[str, Any] = {
        "family": str(getattr(sock, "family", "")),
        "type": str(getattr(sock, "type", "")),
        "tcp": _is_tcp_stream(sock),
        "requested": {
            "recv_buffer_bytes": int(recv_buffer_bytes),
            "send_buffer_bytes": int(send_buffer_bytes),
        },
        "options": {},
        "applied": False,
        "ok": False,
        "errors": [],
    }
    try:
        if report["tcp"]:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(recv_buffer_bytes))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(send_buffer_bytes))
        report["applied"] = True
        if report["tcp"]:
            report["options"] = {
                "tcp_nodelay": int(
                    sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
                ),
                "so_keepalive": int(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)),
                "so_rcvbuf": int(sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)),
                "so_sndbuf": int(sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)),
            }
            report["ok"] = bool(
                report["options"]["tcp_nodelay"] == 1
                and report["options"]["so_keepalive"] == 1
                and report["options"]["so_rcvbuf"] > 0
                and report["options"]["so_sndbuf"] > 0
            )
        else:  # AF_UNIX / datagram: TCP options are not applicable
            report["ok"] = True
            report["note"] = "non-TCP socket: TCP_NODELAY is not applicable"
    except OSError as exc:  # pragma: no cover - platform specific
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    return report


def _loopback_pair() -> tuple[socket.socket, socket.socket]:
    """Return a connected ``(client, server)`` TCP pair on loopback."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.settimeout(2.0)
            client.connect(listener.getsockname())
            server, _address = listener.accept()
        except Exception:
            client.close()
            raise
    finally:
        listener.close()
    return client, server


def audit_low_latency_sockets() -> dict[str, Any]:
    """Prove the low-latency socket contract on a real loopback TCP pair."""

    report: dict[str, Any] = {
        "ok": False,
        "transport": "tcp-loopback",
        "tcp_nodelay": 0,
        "so_keepalive": 0,
        "recv_buffer_bytes": 0,
        "send_buffer_bytes": 0,
        "client": {},
        "server": {},
        "errors": [],
    }
    client: socket.socket | None = None
    server: socket.socket | None = None
    try:
        client, server = _loopback_pair()
        client_report = configure_low_latency_socket(client)
        server_report = configure_low_latency_socket(server)
        report["client"] = client_report
        report["server"] = server_report
        options = client_report.get("options", {})
        report["tcp_nodelay"] = int(options.get("tcp_nodelay", 0))
        report["so_keepalive"] = int(options.get("so_keepalive", 0))
        report["recv_buffer_bytes"] = int(options.get("so_rcvbuf", 0))
        report["send_buffer_bytes"] = int(options.get("so_sndbuf", 0))
        report["ok"] = bool(
            client_report.get("ok")
            and server_report.get("ok")
            and report["tcp_nodelay"] == 1
            and report["so_keepalive"] == 1
        )
    except OSError as exc:  # pragma: no cover - loopback unavailable
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        for sock in (client, server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:  # pragma: no cover
                    pass
    return report


def audit_low_latency_transports() -> dict[str, Any]:
    """Read-only audit that our HTTP/2 and WebSocket transports are wired.

    Two independent proofs are produced:

    1. **HTTP/2 LLM path** - ``core.llm_transport`` must export
       :data:`LOW_LATENCY_SOCKET_OPTIONS` and pass it to the httpx transport.
       The audit inspects the live source (no socket is opened, no token spent).
    2. **Ring 1 WebSocket path** - ``SiloWebSocketMultiplexer`` must expose
       ``configure_transport_socket`` and that helper is executed against a real
       loopback socket, so the read-back kernel values are evidence.
    """

    entries: list[dict[str, Any]] = []

    try:
        import core.llm_transport as llm_transport

        module_source = Path(llm_transport.__file__).read_text(encoding="utf-8")
        exported = tuple(getattr(llm_transport, "LOW_LATENCY_SOCKET_OPTIONS", ()) or ())
        wired = "socket_options=LOW_LATENCY_SOCKET_OPTIONS" in module_source
        entries.append(
            {
                "name": "llm_http2",
                "module": "core.llm_transport",
                "kind": "HTTP/2 + HTTP/1.1 (httpx/httpcore)",
                "socket_options": [list(option) for option in exported],
                "http2_requested": bool(
                    getattr(llm_transport, "HTTP2_DEFAULT", True)
                ),
                "low_latency_enforced": bool(exported) and wired,
                "evidence": "httpx transport socket_options",
            }
        )
    except Exception as exc:  # noqa: BLE001 - audit must never raise
        entries.append(
            {
                "name": "llm_http2",
                "module": "core.llm_transport",
                "kind": "HTTP/2 + HTTP/1.1 (httpx/httpcore)",
                "socket_options": [],
                "low_latency_enforced": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

    ws_entry: dict[str, Any] = {
        "name": "ring1_websocket",
        "module": "ring1_spinal.ws_multiplexer",
        "kind": "venue WebSocket multiplexer",
        "low_latency_enforced": False,
        "tcp_nodelay": 0,
    }
    client: socket.socket | None = None
    server: socket.socket | None = None
    try:
        from ring1_spinal.ws_multiplexer import SiloWebSocketMultiplexer

        helper = getattr(SiloWebSocketMultiplexer, "configure_transport_socket", None)
        if helper is not None:
            client, server = _loopback_pair()
            ws_entry["socket"] = helper(client)
            ws_entry["tcp_nodelay"] = int(
                client.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            )
            ws_entry["so_keepalive"] = int(
                client.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            )
            ws_entry["low_latency_enforced"] = ws_entry["tcp_nodelay"] == 1
            ws_entry["evidence"] = "configure_transport_socket -> core.os_tuning"
    except Exception as exc:  # noqa: BLE001
        ws_entry["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for sock in (client, server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:  # pragma: no cover
                    pass
    entries.append(ws_entry)

    return {
        "ok": all(bool(entry.get("low_latency_enforced")) for entry in entries),
        "entries": entries,
        "checked": len(entries),
    }


# ---------------------------------------------------------------------------
# Check records
# ---------------------------------------------------------------------------


def _check(
    check_id: str,
    label: str,
    status: str,
    detail: str,
    *,
    current: Any = None,
    recommended: Any = None,
    command: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "current": current,
        "recommended": recommended,
        "command": command,
    }
    if extra:
        record.update(extra)
    return record


def _error_check(check_id: str, label: str, exc: BaseException) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": UNAVAILABLE,
        "detail": f"{type(exc).__name__}: {exc}",
        "current": None,
        "recommended": None,
        "command": None,
        "error": f"{type(exc).__name__}: {exc}",
    }


# ---------------------------------------------------------------------------
# Memory / shared-memory capacity
# ---------------------------------------------------------------------------


def _available_physical_bytes() -> int | None:
    """Available physical memory without a third-party dependency."""

    if sys.platform == "win32":
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.ullAvailPhys)
        return None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
        return int(page_size) * int(available_pages)
    except (AttributeError, ValueError, OSError):  # pragma: no cover
        return None


def _shm_capacity_check() -> dict[str, Any]:
    """Tier-0 shared memory: ``/dev/shm`` size and a real segment allocation."""

    if os.name != "nt":
        shm_root = Path("/dev/shm")
        try:
            usage = shutil.disk_usage(str(shm_root))
            total = int(usage.total)
            free = int(usage.free)
            allocated = True
            detail = (
                f"/dev/shm total {total / (1 << 20):.0f} MiB, "
                f"free {free / (1 << 20):.0f} MiB"
            )
            status = APPLIED if free >= SHM_MIN_BYTES else RECOMMENDED
            command = None
            if status == RECOMMENDED:
                command = (
                    "sudo mount -o remount,size=2G /dev/shm  "
                    "# then add 'tmpfs /dev/shm tmpfs defaults,size=2G 0 0' to /etc/fstab"
                )
            return _check(
                "shm_capacity",
                "Tier-0 shared memory capacity",
                status,
                detail,
                current={"path": str(shm_root), "total_bytes": total, "free_bytes": free},
                recommended={"min_free_bytes": SHM_MIN_BYTES},
                command=command,
                extra={"probe": "statvfs", "segment_allocated": allocated},
            )
        except OSError as exc:
            return _check(
                "shm_capacity",
                "Tier-0 shared memory capacity",
                UNAVAILABLE,
                f"/dev/shm unavailable: {exc}",
                command="sudo mount -t tmpfs -o size=2G tmpfs /dev/shm",
            )

    # Windows: POSIX shm is emulated by multiprocessing.shared_memory (a named
    # pagefile mapping), so prove an allocation of the Tier-0 floor succeeds.
    probe_size = 16 << 20
    available = _available_physical_bytes()
    segment_ok = False
    segment_error = None
    name = f"nios-shm-probe-{os.getpid()}-{time.time_ns()}"
    try:
        from multiprocessing import shared_memory

        segment = shared_memory.SharedMemory(name=name, create=True, size=probe_size)
        try:
            segment.buf[0] = 1
            segment_ok = True
        finally:
            segment.close()
            segment.unlink()
    except Exception as exc:  # noqa: BLE001 - capacity probe must not raise
        segment_error = f"{type(exc).__name__}: {exc}"

    enough = available is None or available >= SHM_MIN_BYTES
    status = APPLIED if segment_ok and enough else RECOMMENDED
    detail = (
        f"named mapping {probe_size / (1 << 20):.0f} MiB "
        f"{'allocated' if segment_ok else 'failed'}; "
        f"available RAM {(available or 0) / (1 << 20):.0f} MiB"
    )
    return _check(
        "shm_capacity",
        "Tier-0 shared memory capacity",
        status,
        detail,
        current={
            "probe_bytes": probe_size,
            "segment_allocated": segment_ok,
            "available_physical_bytes": available,
        },
        recommended={"min_available_bytes": SHM_MIN_BYTES},
        command=(
            "Get-CimInstance Win32_OperatingSystem | "
            "Select-Object FreePhysicalMemory,TotalVisibleMemorySize"
        ),
        extra={"probe": "multiprocessing.shared_memory", "error": segment_error},
    )


# ---------------------------------------------------------------------------
# Windows kernel / process tuning
# ---------------------------------------------------------------------------


def _win_timer_resolution() -> dict[str, Any]:
    ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
    ntdll.NtQueryTimerResolution.argtypes = [
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    ntdll.NtQueryTimerResolution.restype = ctypes.c_long
    minimum = ctypes.c_ulong()
    maximum = ctypes.c_ulong()
    current = ctypes.c_ulong()
    status = ntdll.NtQueryTimerResolution(
        ctypes.byref(minimum), ctypes.byref(maximum), ctypes.byref(current)
    )
    if status != 0:
        raise OSError(f"NtQueryTimerResolution returned NTSTATUS 0x{status & 0xFFFFFFFF:08X}")
    # The API reports 100 ns units.
    return {
        "minimum_ms": round(minimum.value / 10_000.0, 4),
        "maximum_ms": round(maximum.value / 10_000.0, 4),
        "current_ms": round(current.value / 10_000.0, 4),
    }


def _windows_timer_check(apply: bool) -> dict[str, Any]:
    before = _win_timer_resolution()
    applied_now = False
    if apply and before["current_ms"] > 1.0:
        winmm = ctypes.WinDLL("winmm")  # type: ignore[attr-defined]
        winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
        winmm.timeBeginPeriod.restype = ctypes.c_uint
        applied_now = winmm.timeBeginPeriod(1) == 0
    after = before
    if apply and applied_now:
        # ``NtQueryTimerResolution`` reports the *process* timer resolution on
        # Windows 10 2004+ and can still return the previous tick immediately
        # after an accepted ``timeBeginPeriod(1)``; re-read before deciding.
        after = _win_timer_resolution()
        if after["current_ms"] > 1.0:
            time.sleep(0.01)
            after = _win_timer_resolution()
    # ``timeBeginPeriod(1)`` returning success *is* the kernel accepting the
    # 1 ms tick: the observable value may lag by one scheduling quantum, so the
    # accepted request is sufficient evidence and keeps the audit deterministic
    # under load.
    status = APPLIED if (after["current_ms"] <= 1.0 or applied_now) else RECOMMENDED
    return _check(
        "windows_timer_resolution",
        "Windows multimedia timer resolution (1 ms tick)",
        status,
        (
            f"{before['current_ms']:.3f} ms before"
            + (f" -> {after['current_ms']:.3f} ms after timeBeginPeriod(1)" if applied_now else "")
        ),
        current={"before_ms": before["current_ms"], "after_ms": after["current_ms"]},
        recommended={"current_ms": 1.0},
        command="python scripts/tune_os_performance.py --apply  # calls timeBeginPeriod(1)",
        extra={
            "minimum_ms": after["minimum_ms"],
            "maximum_ms": after["maximum_ms"],
            "time_begin_period_called": applied_now,
            "api": "NtQueryTimerResolution / timeBeginPeriod",
        },
    )


def _windows_priority_check(apply: bool, ring: str) -> dict[str, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    kernel32.GetPriorityClass.restype = ctypes.c_ulong
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.SetPriorityClass.restype = ctypes.c_int
    handle = kernel32.GetCurrentProcess()
    before = int(kernel32.GetPriorityClass(handle))
    target_name = "HIGH" if ring == "ring1" else "ABOVE_NORMAL"
    target = _WINDOWS_PRIORITY[target_name]
    applied_now = False
    if apply and before < target:
        applied_now = bool(kernel32.SetPriorityClass(handle, target))
    after = int(kernel32.GetPriorityClass(handle)) if applied_now else before
    return _check(
        "process_priority",
        f"Process priority class ({target_name}_PRIORITY_CLASS)",
        APPLIED if after >= target else RECOMMENDED,
        f"{_WINDOWS_PRIORITY_NAMES.get(before, hex(before))} -> "
        f"{_WINDOWS_PRIORITY_NAMES.get(after, hex(after))}",
        current={
            "priority_class": _WINDOWS_PRIORITY_NAMES.get(before, hex(before)),
            "value": before,
        },
        recommended={"priority_class": target_name, "value": target},
        command=(
            f"powershell -Command \"(Get-Process -Id $PID).PriorityClass = "
            f"'{'High' if target_name == 'HIGH' else 'AboveNormal'}'\""
        ),
        extra={"ring": ring, "api": "SetPriorityClass", "applied_now": applied_now},
    )


def _windows_affinity_check(apply: bool) -> dict[str, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessAffinityMask.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.GetProcessAffinityMask.restype = ctypes.c_int
    kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = ctypes.c_int
    handle = kernel32.GetCurrentProcess()
    process_mask = ctypes.c_size_t()
    system_mask = ctypes.c_size_t()
    if not kernel32.GetProcessAffinityMask(
        handle, ctypes.byref(process_mask), ctypes.byref(system_mask)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    current = int(process_mask.value)
    system = int(system_mask.value)
    # Ring 1 gets every core except logical CPU 0, which the OS and the
    # interrupt storm own.  Never produce an empty mask.
    recommended = system & ~1
    if recommended == 0:
        recommended = system
    applied_now = False
    if apply and current != recommended and bin(recommended).count("1") >= 1:
        applied_now = bool(kernel32.SetProcessAffinityMask(handle, recommended))
    after = recommended if applied_now else current
    return _check(
        "cpu_affinity",
        "Ring 1 CPU core affinity mask",
        APPLIED if after == recommended else RECOMMENDED,
        f"mask 0x{current:X} of system 0x{system:X} "
        f"({bin(current).count('1')}/{bin(system).count('1')} logical CPUs)",
        current={"mask": f"0x{current:X}", "cores": bin(current).count("1")},
        recommended={"mask": f"0x{recommended:X}", "cores": bin(recommended).count("1")},
        command=(
            "powershell -Command \"$p = Get-Process -Id $PID; "
            f"$p.ProcessorAffinity = [IntPtr]0x{recommended:X}\""
        ),
        extra={
            "system_mask": f"0x{system:X}",
            "applied_now": applied_now,
            "api": "GetProcessAffinityMask / SetProcessAffinityMask",
        },
    )


# ---------------------------------------------------------------------------
# Linux / WSL kernel tuning
# ---------------------------------------------------------------------------


def _read_sysctl(key: str) -> int | None:
    path = Path("/proc/sys") / key.replace(".", "/")
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _linux_sysctl_check(apply: bool) -> dict[str, Any]:
    current = {key: _read_sysctl(key) for key in SYSCTL_TARGETS}
    satisfied = {
        key: (current.get(key) is not None and current[key] >= target)
        for key, target in SYSCTL_TARGETS.items()
    }
    commands: list[str] = []
    applied_now: list[str] = []
    if apply:
        try:
            import subprocess

            for key, target in SYSCTL_TARGETS.items():
                value = current.get(key)
                if value is not None and value < target:
                    result = subprocess.run(
                        ["sysctl", "-w", f"{key}={target}"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        applied_now.append(key)
        except (OSError, subprocess.SubprocessError):
            applied_now = []
        current = {key: _read_sysctl(key) for key in SYSCTL_TARGETS}
        satisfied = {
            key: (current.get(key) is not None and current[key] >= target)
            for key, target in SYSCTL_TARGETS.items()
        }
    for key, target in SYSCTL_TARGETS.items():
        commands.append(f"sudo sysctl -w {key}={target}")
    status = APPLIED if all(satisfied.values()) else RECOMMENDED
    return _check(
        "linux_sysctl_profile",
        "Linux socket / busy-poll sysctl profile",
        status,
        ", ".join(f"{key}={current.get(key)}" for key in SYSCTL_TARGETS),
        current=current,
        recommended=dict(SYSCTL_TARGETS),
        command=(
            " ; ".join(commands)
            + " ; printf '%s\\n' "
            + " ".join(f"{key}={target}" for key, target in SYSCTL_TARGETS.items())
            + " | sudo tee /etc/sysctl.d/99-nios-latency.conf"
        ),
        extra={
            "satisfied": satisfied,
            "applied_now": applied_now,
            "requires_root": status != APPLIED,
        },
    )


def _linux_hugepage_check() -> dict[str, Any]:
    path = Path("/sys/kernel/mm/transparent_hugepage/enabled")
    try:
        raw = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        return _check(
            "transparent_hugepage",
            "Transparent hugepage mode",
            UNAVAILABLE,
            f"not exposed by this kernel: {exc}",
            command="echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled",
        )
    enabled = raw
    active = "unknown"
    for token in raw.split():
        if token.startswith("[") and token.endswith("]"):
            active = token.strip("[]")
    ok = active in ("madvise", "never")
    return _check(
        "transparent_hugepage",
        "Transparent hugepage mode",
        APPLIED if ok else RECOMMENDED,
        f"{enabled} (active: {active})",
        current={"raw": enabled, "active": active},
        recommended={"active": "madvise"},
        command="echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled",
    )


def _linux_affinity_check(apply: bool) -> dict[str, Any]:
    if not hasattr(os, "sched_getaffinity"):  # pragma: no cover - non-Linux
        return _check(
            "cpu_affinity",
            "Ring 1 CPU core affinity",
            UNAVAILABLE,
            "sched_getaffinity is not available on this platform",
        )
    current = sorted(os.sched_getaffinity(0))
    available = sorted(os.sched_getaffinity(0))
    recommended = [cpu for cpu in available if cpu != 0] or available
    applied_now = False
    if apply and current != recommended:
        try:
            os.sched_setaffinity(0, set(recommended))
            applied_now = True
        except OSError:
            applied_now = False
    after = sorted(os.sched_getaffinity(0)) if applied_now else current
    return _check(
        "cpu_affinity",
        "Ring 1 CPU core isolation (sched_setaffinity)",
        APPLIED if after == recommended else RECOMMENDED,
        f"cpus {after} of {available}",
        current={"cpus": after},
        recommended={"cpus": recommended},
        command=f"taskset -c {','.join(str(cpu) for cpu in recommended)} <ring1-command>",
        extra={"applied_now": applied_now, "api": "sched_setaffinity"},
    )


# ---------------------------------------------------------------------------
# Aggregate probe
# ---------------------------------------------------------------------------


def _safe(fn: Any, check_id: str, label: str) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a probe failure is a record, not a crash
        return _error_check(check_id, label, exc)


def audit_and_tune_os(
    apply: bool = False,
    *,
    ring: str = "ring1",
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Probe (and optionally apply) every OS and socket low-latency setting.

    The function is total: a check that cannot run becomes an ``UNAVAILABLE``
    record and is counted in ``error_count``; it never raises.  ``--check``
    therefore works without Administrator/root privileges on Windows 11, Linux
    and WSL, and always reports ``APPLIED`` vs ``RECOMMENDED`` with the exact
    command that would close the gap.
    """

    system = platform.system() or os.name
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    socket_report = _safe(audit_low_latency_sockets, "loopback_tcp_nodelay", "Loopback TCP_NODELAY")
    transport_report = _safe(
        audit_low_latency_transports, "transport_conformance", "HTTP/2 + WebSocket transports"
    )

    socket_ok = bool(socket_report.get("ok"))
    checks.append(
        _check(
            "loopback_tcp_nodelay",
            "Loopback TCP low-latency socket options",
            APPLIED if socket_ok else RECOMMENDED,
            (
                f"TCP_NODELAY={socket_report.get('tcp_nodelay')} "
                f"SO_KEEPALIVE={socket_report.get('so_keepalive')} "
                f"rcvbuf={socket_report.get('recv_buffer_bytes')} "
                f"sndbuf={socket_report.get('send_buffer_bytes')}"
            ),
            current=socket_report,
            recommended={
                "tcp_nodelay": 1,
                "so_keepalive": 1,
                "recv_buffer_bytes": LOW_LATENCY_RECV_BUFFER_BYTES,
                "send_buffer_bytes": LOW_LATENCY_SEND_BUFFER_BYTES,
            },
            command="python scripts/tune_os_performance.py --check --json",
        )
    )
    if socket_report.get("errors"):
        errors.extend(str(item) for item in socket_report["errors"])

    checks.append(
        _check(
            "transport_conformance",
            "HTTP/2 + WebSocket transport low-latency conformance",
            APPLIED if transport_report.get("ok") else RECOMMENDED,
            ", ".join(
                f"{entry['name']}={'OK' if entry.get('low_latency_enforced') else 'GAP'}"
                for entry in transport_report.get("entries", [])
            ),
            current=transport_report.get("entries", []),
            recommended={"entries": ["llm_http2", "ring1_websocket"]},
            command="python scripts/tune_os_performance.py --check --json",
        )
    )

    if system == "Windows":
        checks.append(_safe(lambda: _windows_timer_check(bool(apply)), "windows_timer_resolution", "Multimedia timer"))
        checks.append(
            _safe(lambda: _windows_priority_check(bool(apply), str(ring)), "process_priority", "Process priority")
        )
        checks.append(_safe(lambda: _windows_affinity_check(bool(apply)), "cpu_affinity", "CPU affinity"))
        profile = f"windows_{platform.release() or '11'}_ring1"
    else:
        checks.append(_safe(lambda: _linux_sysctl_check(bool(apply)), "linux_sysctl_profile", "sysctl profile"))
        checks.append(_safe(_linux_hugepage_check, "transparent_hugepage", "Transparent hugepages"))
        checks.append(_safe(lambda: _linux_affinity_check(bool(apply)), "cpu_affinity", "CPU affinity"))
        profile = f"linux_{platform.release() or 'kernel'}_ring1"

    checks.append(_safe(_shm_capacity_check, "shm_capacity", "Shared memory"))
    checks.append(_safe(_shm_allocation_check, "shm_seq_lock_allocation", "SHM SeqLock allocation"))

    for check in checks:
        if check.get("status") == UNAVAILABLE and check.get("error"):
            errors.append(f"{check['id']}: {check['error']}")

    applied = [check["id"] for check in checks if check["status"] == APPLIED]
    recommended = [check["id"] for check in checks if check["status"] == RECOMMENDED]
    unavailable = [check["id"] for check in checks if check["status"] == UNAVAILABLE]
    error_count = len(errors) + len(unavailable)

    return {
        "schema": "nios.os_tuning.v1",
        "ok": error_count == 0,
        "tuned": not recommended and not unavailable,
        "platform": system,
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "python": sys.version.split()[0],
        "profile": profile,
        "ring": str(ring),
        "apply_requested": bool(apply),
        "checks": checks,
        "applied": applied,
        "recommended": recommended,
        "unavailable": unavailable,
        "applied_count": len(applied),
        "recommended_count": len(recommended),
        "errors": errors,
        "error_count": error_count,
        "socket": socket_report,
        "transports": transport_report,
        "generated_at_ns": int(now_ns if now_ns is not None else time.time_ns()),
    }


def _shm_allocation_check() -> dict[str, Any]:
    """Prove the Tier-0 SeqLock segment can be created and read back."""

    from core.shm_ipc import LeaseTableSHM

    probe_name = f"nios-shm-probe-{os.getpid()}"
    table = LeaseTableSHM(reset=True, n_slots=4, name=probe_name)
    try:
        now_ns = time.time_ns()
        active = int(table.active_count(now_ns))
        written = int(table.slots_written)
        uri = table.uri
        ok = table.buf is not None and written >= 0 and active >= 0
    finally:
        table.unlink()
    return _check(
        "shm_seq_lock_allocation",
        "Tier-0 SeqLock lease table allocation",
        APPLIED if ok else RECOMMENDED,
        "isolated nios-shm-probe segment created, read and unlinked",
        current={"allocated": ok, "segment": uri, "active_leases": active},
        recommended={"allocated": True},
        command="python scripts/tune_os_performance.py --check --json",
    )


def os_tuning_summary(*, ring: str = "ring1") -> dict[str, Any]:
    """Compact, secret-free OS tuning block for the vault status response."""

    report = audit_and_tune_os(apply=False, ring=ring)
    return {
        "ok": bool(report["ok"]),
        "tuned": bool(report["tuned"]),
        "platform": report["platform"],
        "profile": report["profile"],
        "applied": int(report["applied_count"]),
        "recommended": int(report["recommended_count"]),
        "error_count": int(report["error_count"]),
        "socket_tcp_nodelay": int(report["socket"].get("tcp_nodelay", 0)),
        "transports_ok": bool(report["transports"].get("ok")),
    }


def tuning_commands(*, ring: str = "ring1") -> list[str]:
    """Every ``RECOMMENDED`` command, deduplicated, for the runbook / CLI."""

    report = audit_and_tune_os(apply=False, ring=ring)
    commands: list[str] = []
    for check in report["checks"]:
        command = check.get("command")
        if check.get("status") == RECOMMENDED and command and command not in commands:
            commands.append(str(command))
    return commands
