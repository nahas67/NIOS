"""Locale helpers for the MiroFish services."""

from __future__ import annotations

from contextvars import ContextVar

__all__ = ["get_language_instruction", "get_locale", "set_locale", "t"]

_LOCALE: ContextVar[str] = ContextVar("mirofish_locale", default="en")

_INSTRUCTIONS: dict[str, str] = {
    "en": "Respond in English.",
    "zh": "请用中文回复。",
    "hi": "कृपया हिंदी में उत्तर दें।",
}


def get_locale() -> str:
    return _LOCALE.get()


def set_locale(locale: str) -> None:
    _LOCALE.set(str(locale or "en").split("-")[0].lower())


def get_language_instruction() -> str:
    return _INSTRUCTIONS.get(get_locale(), _INSTRUCTIONS["en"])


def t(key: str, **_: object) -> str:
    """Translate a short UI key (identity when no catalogue entry exists)."""
    return str(key)
