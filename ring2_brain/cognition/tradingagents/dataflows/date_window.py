"""As-of date helpers: every tool call is anchored to a run date."""

from __future__ import annotations

from datetime import date, datetime, timedelta

__all__ = [
    "get_current_date",
    "is_historical",
    "as_of",
    "as_of_window",
    "parse_date",
]


def get_current_date() -> str:
    return date.today().isoformat()


def parse_date(value: object) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a date: {value!r}")


def is_historical(value: object) -> bool:
    """True when ``value`` is strictly before today (UTC calendar date)."""
    try:
        return parse_date(value) < date.today()
    except ValueError:
        return False


def as_of(curr_date: object = None, trade_date: object = None) -> str:
    """The effective as-of date: the run date, never later than today."""
    chosen = trade_date if trade_date is not None else curr_date
    if chosen is None:
        return get_current_date()
    parsed = parse_date(chosen)
    today = date.today()
    return (parsed if parsed <= today else today).isoformat()


def as_of_window(start_date: object, end_date: object, look_back_days: int = 30) -> tuple[str, str]:
    end = parse_date(end_date)
    start = parse_date(start_date) if start_date else end - timedelta(days=int(look_back_days))
    if start > end:
        start, end = end, start
    return start.isoformat(), end.isoformat()
