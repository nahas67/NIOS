"""Symbol normalisation and filename-safe ticker components."""

from __future__ import annotations

import re

__all__ = ["normalize_symbol", "safe_ticker_component"]

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def normalize_symbol(symbol: object) -> str:
    """Return a trimmed, upper-cased ticker (``brk.b`` -> ``BRK.B``)."""
    if symbol is None:
        return ""
    return str(symbol).strip().upper().replace(" ", "")


def safe_ticker_component(ticker: object) -> str:
    """Return a ticker safe to use as one path component.

    Rejects empty names, ``.``/``..`` and separators; every other unsafe
    character becomes ``_`` so a vendor-controlled symbol cannot traverse the
    cache directory.
    """
    text = normalize_symbol(ticker)
    if not text or text in {".", ".."}:
        raise ValueError(f"unsafe ticker component: {ticker!r}")
    text = _SAFE_RE.sub("_", text).strip("._")
    if not text or ".." in text or "/" in text or "\\" in text:
        raise ValueError(f"unsafe ticker component: {ticker!r}")
    return text[:128]
