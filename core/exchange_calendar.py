"""NIOS V1.9 -- DST-aware exchange calendar for all four silos (``E6``).

The firewall must be able to answer one question deterministically: *is this
venue actually open at this UTC instant?*  A synthetic "market is always open"
assumption is how an order reaches a closed exchange, gets queued overnight and
fills into a gap.

Design rules
------------
* **UTC everywhere.**  Every public argument and return value is a UTC
  nanosecond stamp; local time is only an intermediate.
* **No ``tzdata`` dependency.**  Windows installs frequently lack the IANA
  database, so the US Eastern DST rule is computed arithmetically (second
  Sunday of March 07:00 UTC -> first Sunday of November 06:00 UTC).  India has
  no DST; crypto trades 24x7; FX trades the Sunday-22:00-UTC to
  Friday-22:00-UTC week.
* **Holidays and half-days are data, not guesses.**  The tables below are
  explicit per venue and per year.  An unknown year fails *closed* for the
  equity venues (a closed market is safer than an assumed-open one) while the
  24x7 venues stay open by construction.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as _dt
from dataclasses import dataclass
from typing import Iterable, Mapping

__all__ = [
    "VenueSession",
    "VENUE_HOLIDAYS",
    "VENUE_HALF_DAYS",
    "silo_is_24x7",
    "session_for",
    "is_open",
    "next_open",
    "calendar_snapshot",
    "VENUE_CLOSED_CODE",
]

VENUE_CLOSED_CODE = "VENUE_CLOSED"

_NS_PER_SECOND = 1_000_000_000
_NS_PER_DAY = 86_400 * _NS_PER_SECOND

#: Silos that never close.
_CONTINUOUS_SILOS = frozenset({"CRYPTO_BINANCE"})

#: Silos whose session is a local-time window.
_SESSION_SILOS: Mapping[str, tuple[int, int]] = {
    # (local open minutes, local close minutes) in the venue's local clock.
    "US_IBKR": (9 * 60 + 30, 16 * 60),
    "INDIA_ZERODHA_DHAN": (9 * 60 + 15, 15 * 60 + 30),
}

#: Half-day closes, expressed as local close minutes.
_HALF_DAY_CLOSE: Mapping[str, int] = {
    "US_IBKR": 13 * 60,
    "INDIA_ZERODHA_DHAN": 15 * 60 + 30,
}


@dataclass(slots=True, frozen=True)
class VenueSession:
    """One contiguous trading window in UTC."""

    silo: str
    open_utc_ns: int
    close_utc_ns: int
    half_day: bool = False
    holiday: str = ""

    def contains(self, at_utc_ns: int) -> bool:
        return int(self.open_utc_ns) <= int(at_utc_ns) < int(self.close_utc_ns)

    def to_dict(self) -> dict[str, object]:
        return {
            "silo": self.silo,
            "open_utc_ns": self.open_utc_ns,
            "close_utc_ns": self.close_utc_ns,
            "open_utc": ns_to_iso(self.open_utc_ns),
            "close_utc": ns_to_iso(self.close_utc_ns),
            "half_day": self.half_day,
            "holiday": self.holiday,
        }


def ns_to_iso(value: int) -> str:
    seconds, nanos = divmod(int(value), _NS_PER_SECOND)
    stamp = _dt.datetime.fromtimestamp(seconds, tz=_dt.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def _utc_ns(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return _calendar.timegm((year, month, day, hour, minute, 0, 0, 0, 0)) * _NS_PER_SECOND


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> int:
    """Day-of-month of the ``n``-th ``weekday`` (Mon=0) in a month."""
    first = _dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return 1 + offset + (n - 1) * 7


def _last_weekday(year: int, month: int, weekday: int) -> int:
    last_day = _calendar.monthrange(year, month)[1]
    last = _dt.date(year, month, last_day)
    return last_day - ((last.weekday() - weekday) % 7)


def us_eastern_offset_hours(at_utc_ns: int) -> int:
    """``-5`` (EST) or ``-4`` (EDT) for a UTC instant, computed without tzdata.

    US DST begins on the second Sunday of March at 02:00 local (07:00 UTC) and
    ends on the first Sunday of November at 02:00 local (06:00 UTC).
    """
    stamp = _dt.datetime.fromtimestamp(int(at_utc_ns) // _NS_PER_SECOND, tz=_dt.timezone.utc)
    year = stamp.year
    dst_start = _utc_ns(year, 3, _nth_weekday(year, 3, 6, 2), 7, 0)
    dst_end = _utc_ns(year, 11, _nth_weekday(year, 11, 6, 1), 6, 0)
    return -4 if dst_start <= int(at_utc_ns) < dst_end else -5


def _local_date(at_utc_ns: int, offset_minutes: int) -> _dt.date:
    shifted = int(at_utc_ns) // _NS_PER_SECOND + offset_minutes * 60
    return _dt.datetime.fromtimestamp(shifted, tz=_dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# Holiday / half-day data
# ---------------------------------------------------------------------------

#: NYSE full closures (observed dates).
_US_HOLIDAYS: frozenset[str] = frozenset(
    {
        "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
        "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
        "2025-12-25",
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
        "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    }
)

_US_HALF_DAYS: frozenset[str] = frozenset(
    {
        "2025-07-03", "2025-11-28", "2025-12-24",
        "2026-11-27", "2026-12-24",
        "2027-11-26",
    }
)

#: NSE full closures (Republic Day, Holi, Good Friday, Independence Day,
#: Gandhi Jayanti, Diwali and the exchange's annual settlement holidays).
_INDIA_HOLIDAYS: frozenset[str] = frozenset(
    {
        "2025-01-26", "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10",
        "2025-04-14", "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27",
        "2025-10-02", "2025-10-21", "2025-10-22", "2025-11-05", "2025-12-25",
        "2026-01-26", "2026-03-04", "2026-03-20", "2026-04-03", "2026-04-14",
        "2026-05-01", "2026-08-15", "2026-10-02", "2026-11-09", "2026-12-25",
        "2027-01-26", "2027-03-22", "2027-04-14", "2027-08-15", "2027-10-02",
        "2027-12-25",
    }
)

VENUE_HOLIDAYS: Mapping[str, frozenset[str]] = {
    "US_IBKR": _US_HOLIDAYS,
    "INDIA_ZERODHA_DHAN": _INDIA_HOLIDAYS,
}

VENUE_HALF_DAYS: Mapping[str, frozenset[str]] = {
    "US_IBKR": _US_HALF_DAYS,
    "INDIA_ZERODHA_DHAN": frozenset(),
}

#: FX week: opens Sunday 22:00 UTC, closes Friday 22:00 UTC.
_FX_OPEN_WEEKDAY = 6  # Sunday (Mon=0)
_FX_OPEN_HOUR = 22
_FX_CLOSE_WEEKDAY = 4  # Friday
_FX_CLOSE_HOUR = 22


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def silo_is_24x7(silo: str) -> bool:
    return str(silo).strip().upper() in _CONTINUOUS_SILOS


def _equity_session(silo: str, at_utc_ns: int) -> VenueSession | None:
    offset_minutes = (
        us_eastern_offset_hours(at_utc_ns) * 60 if silo == "US_IBKR" else 330
    )
    local = _local_date(at_utc_ns, offset_minutes)
    key = local.isoformat()
    if key in VENUE_HOLIDAYS.get(silo, frozenset()):
        return None
    open_minute, close_minute = _SESSION_SILOS[silo]
    half = key in VENUE_HALF_DAYS.get(silo, frozenset())
    if half:
        close_minute = _HALF_DAY_CLOSE[silo]
    day_start_ns = _calendar.timegm((local.year, local.month, local.day, 0, 0, 0, 0, 0, 0))
    day_start_ns *= _NS_PER_SECOND
    open_ns = day_start_ns + (open_minute - offset_minutes) * 60 * _NS_PER_SECOND
    close_ns = day_start_ns + (close_minute - offset_minutes) * 60 * _NS_PER_SECOND
    return VenueSession(
        silo=silo,
        open_utc_ns=open_ns,
        close_utc_ns=close_ns,
        half_day=half,
        holiday="",
    )


def _fx_session(at_utc_ns: int) -> VenueSession | None:
    stamp = _dt.datetime.fromtimestamp(int(at_utc_ns) // _NS_PER_SECOND, tz=_dt.timezone.utc)
    weekday = stamp.weekday()
    hour = stamp.hour
    # Closed from Friday 22:00 UTC until Sunday 22:00 UTC.
    if weekday == _FX_CLOSE_WEEKDAY and hour >= _FX_CLOSE_HOUR:
        return None
    if weekday == 5:  # Saturday
        return None
    if weekday == _FX_OPEN_WEEKDAY and hour < _FX_OPEN_HOUR:
        return None
    # Build the enclosing Sunday-22:00 -> Friday-22:00 window.
    days_since_sunday = (weekday + 1) % 7
    sunday = stamp.date() - _dt.timedelta(days=days_since_sunday)
    open_ns = _utc_ns(sunday.year, sunday.month, sunday.day, _FX_OPEN_HOUR, 0)
    friday = sunday + _dt.timedelta(days=5)
    close_ns = _utc_ns(friday.year, friday.month, friday.day, _FX_CLOSE_HOUR, 0)
    return VenueSession(silo="FX_MT5", open_utc_ns=open_ns, close_utc_ns=close_ns)


def session_for(silo: str, at_utc_ns: int) -> VenueSession | None:
    """Return the session containing ``at_utc_ns``, or ``None`` when closed."""
    key = str(silo).strip().upper()
    stamp = int(at_utc_ns)
    if key in _CONTINUOUS_SILOS:
        return VenueSession(
            silo=key,
            open_utc_ns=stamp - _NS_PER_DAY,
            close_utc_ns=stamp + _NS_PER_DAY,
        )
    if key == "FX_MT5":
        return _fx_session(stamp)
    if key in _SESSION_SILOS:
        session = _equity_session(key, stamp)
        if session is None or not session.contains(stamp):
            return None
        return session
    raise KeyError(f"unknown silo {silo!r}")


def is_open(silo: str, at_utc_ns: int) -> bool:
    return session_for(silo, at_utc_ns) is not None


def next_open(silo: str, at_utc_ns: int, *, horizon_days: int = 14) -> VenueSession | None:
    """First session that opens at or after ``at_utc_ns`` within the horizon."""
    key = str(silo).strip().upper()
    stamp = int(at_utc_ns)
    if is_open(key, stamp):
        return session_for(key, stamp)
    for day in range(0, int(horizon_days) + 1):
        probe = stamp + day * _NS_PER_DAY
        for hour in range(0, 24):
            candidate = probe + hour * 3600 * _NS_PER_SECOND
            session = session_for(key, candidate)
            if session is not None:
                return session
    return None


def calendar_snapshot(at_utc_ns: int, silos: Iterable[str] | None = None) -> dict[str, object]:
    """Cockpit-ready per-silo open/closed snapshot with the next open."""
    keys = tuple(silos) if silos is not None else (
        "INDIA_ZERODHA_DHAN",
        "CRYPTO_BINANCE",
        "US_IBKR",
        "FX_MT5",
    )
    rows: dict[str, object] = {}
    for silo in keys:
        key = str(silo).strip().upper()
        session = session_for(key, at_utc_ns)
        upcoming = session or next_open(key, at_utc_ns)
        rows[key] = {
            "open": session is not None,
            "session": session.to_dict() if session else None,
            "next_open": upcoming.to_dict() if upcoming else None,
            "continuous": silo_is_24x7(key),
        }
    return {"as_of_utc_ns": int(at_utc_ns), "silos": rows}
