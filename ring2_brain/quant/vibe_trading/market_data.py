"""Market-data helpers used by the grounding identity gate."""

from __future__ import annotations

import re

__all__ = ["canonical_fx_pair"]

_ISO_CODES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "CHF",
        "AUD",
        "NZD",
        "CAD",
        "CNY",
        "CNH",
        "HKD",
        "INR",
        "KRW",
        "SGD",
        "TWD",
        "BRL",
        "MXN",
        "ZAR",
        "SEK",
        "NOK",
        "DKK",
        "PLN",
        "TRY",
        "RUB",
        "AED",
        "SAR",
        "THB",
        "IDR",
        "MYR",
        "PHP",
        "VND",
    }
)

_FX_RE = re.compile(
    r"^(?P<base>[A-Z]{3})\s*[/\-. _]?\s*(?P<quote>[A-Z]{3})(?:[=:]?X)?$"
)


def canonical_fx_pair(value: object) -> str | None:
    """Return ``"BASE/QUOTE"`` for an FX symbol, else ``None``.

    Accepts the spellings vendors use: ``EURUSD``, ``EUR/USD``, ``EUR-USD``,
    ``EUR.USD`` and the Yahoo suffix form ``EURUSD=X``.  A six-letter ticker
    whose halves are not both ISO currencies is not an FX pair.
    """
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    match = _FX_RE.match(text)
    if match is None:
        return None
    base, quote = match.group("base"), match.group("quote")
    if base == quote:
        return None
    if base not in _ISO_CODES or quote not in _ISO_CODES:
        return None
    return f"{base}/{quote}"
