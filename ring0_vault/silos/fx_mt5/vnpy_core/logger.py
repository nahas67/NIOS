"""Logger shim for the harvested ``vnpy_core`` tree.

``engine.py`` imports ``logger`` plus the stdlib level constants from
``.logger``.  Upstream this module also mirrored output into a rotating file and
the Qt log monitor; NIOS Ring 0 deliberately keeps it to stderr so a silo worker
never blocks on a shared file handle in the hot path.
"""

from __future__ import annotations

import logging
import sys

__all__ = [
    "logger",
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
    "get_logger",
]

DEBUG = logging.DEBUG
INFO = logging.INFO
WARNING = logging.WARNING
ERROR = logging.ERROR
CRITICAL = logging.CRITICAL

_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"

logger = logging.getLogger("nios.vnpy")
if not logger.handlers:
    _handler = logging.StreamHandler(stream=sys.stderr)
    _handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(_handler)
    logger.propagate = False
logger.setLevel(INFO)


def get_logger(name: str = "nios.vnpy", level: int | None = None) -> logging.Logger:
    """Return a configured child logger."""

    child = logging.getLogger(name)
    if not child.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        child.addHandler(handler)
        child.propagate = False
    child.setLevel(level if level is not None else INFO)
    return child
