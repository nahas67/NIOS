"""Locale shim for the harvested ``vnpy_core`` tree.

Upstream vn.py ships a compiled ``.mo`` catalogue and a ``locale.py`` that binds
``_`` to ``gettext.gettext``.  The NIOS harvest kept the call sites
(``from .locale import _``) but not the catalogue, so this module provides the
same contract with an identity translator.  Operator-facing strings stay in
their source language; nothing is silently rewritten.
"""

from __future__ import annotations

import gettext
import os
from pathlib import Path

__all__ = ["_", "LOCALE_DIR", "get_translator"]

LOCALE_DIR = Path(os.environ.get("NIOS_VNPY_LOCALE_DIR", Path(__file__).resolve().parent / "locale"))

_TRANSLATION: gettext.NullTranslations | None = None


def get_translator() -> gettext.NullTranslations:
    """Return the process translator (null catalogue when none is installed)."""

    global _TRANSLATION
    if _TRANSLATION is not None:
        return _TRANSLATION
    try:
        _TRANSLATION = gettext.translation(
            "vnpy",
            localedir=str(LOCALE_DIR),
            languages=[os.environ.get("LANGUAGE", "en")],
            fallback=True,
        )
    except Exception:  # pragma: no cover - never fail on a missing catalogue
        _TRANSLATION = gettext.NullTranslations()
    return _TRANSLATION


def _(message: str) -> str:
    """Translate ``message`` (identity when no catalogue is installed)."""

    return get_translator().gettext(message)
