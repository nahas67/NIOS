"""Launch preflight: which ``host:port`` can NIOS actually bind?

The launcher scripts must never let uvicorn die with ``WinError 10048`` several
seconds into boot, and they must never refuse to start on a workstation where
port ``8000`` happens to be held by some unrelated daemon.  This module owns
both decisions:

* :func:`can_bind` performs the *real* bind the server is about to perform and
  closes it immediately.  ``connect_ex`` is not a reliable occupancy test on
  Windows: a socket can be bound without listening, in which case a connect is
  refused but a bind still collides.
* :func:`first_free_port` / :func:`resolve_port` implement the Operator Note #2
  fallback: when the default port is busy and the operator did **not** force a
  port, take the first free port in ``8000..8010`` instead of failing.

Exit codes (CLI)
----------------
0   the address is free / a free port was found and printed
1   the address is in use / no port in the scan range is free
2   the arguments were invalid

Usage::

    python scripts/nios_port_check.py 127.0.0.1 8000
    python scripts/nios_port_check.py 127.0.0.1 --scan
    python scripts/nios_port_check.py 127.0.0.1 --scan 8000 8010
"""

from __future__ import annotations

import socket
import sys

__all__ = [
    "DEFAULT_PORT_START",
    "DEFAULT_PORT_END",
    "PortUnavailable",
    "can_bind",
    "first_free_port",
    "resolve_port",
    "main",
]

DEFAULT_PORT_START = 8000
DEFAULT_PORT_END = 8010


class PortUnavailable(RuntimeError):
    """No port in the requested range could be bound."""


def _family_for(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def can_bind(host: str, port: int) -> bool:
    """True when a listening socket can be bound to ``host:port`` right now."""

    sock = socket.socket(_family_for(host), socket.SOCK_STREAM)
    try:
        # Windows: refuse to share the address, matching uvicorn's default bind
        # semantics.  POSIX: allow immediate reuse so a just-closed listener in
        # TIME_WAIT does not look like a conflict.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def first_free_port(
    host: str,
    start: int = DEFAULT_PORT_START,
    end: int = DEFAULT_PORT_END,
) -> int | None:
    """First bindable port in ``start..end`` (inclusive), or ``None``."""
    low, high = int(start), int(end)
    if low > high:
        low, high = high, low
    for port in range(low, high + 1):
        if can_bind(host, port):
            return port
    return None


def resolve_port(
    host: str,
    requested: int = DEFAULT_PORT_START,
    *,
    allow_fallback: bool = True,
    start: int = DEFAULT_PORT_START,
    end: int = DEFAULT_PORT_END,
) -> int:
    """Return ``requested`` when bindable, else the first free port in range.

    ``allow_fallback=False`` means the operator explicitly forced a port
    (``NIOS_PORT`` / ``--port``): honour it or fail loudly, never silently
    move a service the operator pinned.  With fallback enabled -- the default
    workflow -- an occupied ``8000`` degrades to ``8001..8010``.
    """
    requested = int(requested)
    if can_bind(host, requested):
        return requested
    if not allow_fallback:
        raise PortUnavailable(
            f"{host}:{requested} is already in use. Stop the process holding it, "
            f"or unset NIOS_PORT / omit --port to auto-select "
            f"{int(start)}..{int(end)}."
        )
    fallback = first_free_port(host, start, end)
    if fallback is None:
        raise PortUnavailable(
            f"{host}:{requested} is in use and no free port exists in "
            f"{int(start)}..{int(end)}."
        )
    return fallback


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    scan = "--scan" in args
    args = [arg for arg in args if arg != "--scan"]

    if not args:
        print(
            "usage: nios_port_check.py <host> <port> | <host> --scan [start] [end]",
            file=sys.stderr,
        )
        return 2

    host = args[0] or "127.0.0.1"

    try:
        if scan:
            if len(args) == 1:
                start, end = DEFAULT_PORT_START, DEFAULT_PORT_END
            elif len(args) == 2:
                start = end = int(args[1])
            elif len(args) == 3:
                start, end = int(args[1]), int(args[2])
            else:
                raise ValueError("--scan takes at most two ports")
            port = first_free_port(host, start, end)
            if port is None:
                print(
                    f"{host}: no free port in {start}..{end}",
                    file=sys.stderr,
                )
                return 1
            print(port)
            return 0

        if len(args) != 2:
            print(
                "usage: nios_port_check.py <host> <port> | <host> --scan [start] [end]",
                file=sys.stderr,
            )
            return 2
        port = int(args[1])
    except ValueError as exc:
        print(f"invalid arguments: {exc}", file=sys.stderr)
        return 2

    return 0 if can_bind(host, port) else 1


if __name__ == "__main__":
    raise SystemExit(main())
