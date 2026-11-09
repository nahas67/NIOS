"""Currency normalisation for valuation inputs.

The donor valuation package imports ``normalize_currency`` from
``src.entities.models``.  The harvested tree carries no entity layer, so this
module implements the one pure function it needs: map the symbol/short-code
spellings a filing or a vendor may use onto an ISO-4217 code.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

__all__ = ["normalize_currency", "normalize_date"]

_ALIASES: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "USD$": "USD",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "US DOLLAR": "USD",
    "US DOLLARS": "USD",
    "\u20b9": "INR",
    "RS": "INR",
    "RS.": "INR",
    "RUPEE": "INR",
    "RUPEES": "INR",
    "INR\u20b9": "INR",
    "\u20ac": "EUR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "\u00a3": "GBP",
    "POUND": "GBP",
    "POUNDS": "GBP",
    "STERLING": "GBP",
    "\u00a5": "JPY",
    "YEN": "JPY",
    "RMB": "CNY",
    "YUAN": "CNY",
    "RENMINBI": "CNY",
    "HK$": "HKD",
    "HK DOLLAR": "HKD",
    "HKD$": "HKD",
    "S$": "SGD",
    "SGD$": "SGD",
    "A$": "AUD",
    "AUD$": "AUD",
    "C$": "CAD",
    "CAD$": "CAD",
    "CHF": "CHF",
    "SWISS FRANC": "CHF",
    "KRW": "KRW",
    "WON": "KRW",
    "BRL": "BRL",
    "REAL": "BRL",
    "ZAR": "ZAR",
    "RAND": "ZAR",
}


def normalize_currency(value: Any) -> str | None:
    """Return the ISO-4217 code for ``value``, or ``None`` when absent.

    Unknown non-empty codes are upper-cased rather than silently dropped: a
    valuation that names a currency we do not model should surface it, not
    pretend the field was missing.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    upper = text.upper()
    if upper in _ALIASES:
        return _ALIASES[upper]
    if text in _ALIASES:
        return _ALIASES[text]
    return upper


def normalize_date(value: Any, *, field_name: str = "date") -> date:
    """Return a ``datetime.date`` from a date, datetime or ISO-ish string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a date: {value!r}") from exc
