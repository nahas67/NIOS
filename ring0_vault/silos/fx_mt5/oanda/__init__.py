"""OANDA v20 Forex adapter for the Ring 0 ``FX_MT5`` silo.

``sec_001`` names OANDA as the v20 Forex venue for the FX silo alongside the
MetaTrader 5 / CTP transports.  ``sec_015`` fixes the silo microstructure rules:

* ``24/5 pip-spread gates`` - the market is only tradable while the FX week is
  open, and a quote is only tradable while its spread sits inside the
  per-instrument pip ceiling.
* ``macro calendar blackout windows`` - no new risk is opened inside the
  release window of a high-impact macro print.
* ``futures roll-yield guards`` - a carry/roll position is refused when the
  annualised roll yield is implausible or the contract is too close to expiry.

This module is deliberately dependency-free: it formats the exact OANDA v20
order payload and evaluates the three gates with pure arithmetic so Ring 0 can
run it on an air-gapped host with no ``oandapyV20`` package, no network and no
credentials.  Live submission is the caller's responsibility (Ring 0 only, via
:func:`order_request` against an authenticated ``httpx`` client).
"""

from __future__ import annotations

import calendar
import json
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_MACRO_CALENDAR",
    "FX_WEEK_CLOSE_UTC",
    "FX_WEEK_OPEN_UTC",
    "MACRO_BLACKOUT_AFTER_MIN",
    "MACRO_BLACKOUT_BEFORE_MIN",
    "MAX_ROLL_YIELD_BPS",
    "MIN_DAYS_TO_EXPIRY",
    "OANDA_API_VERSION",
    "OANDA_DATA_HOST",
    "OANDA_LIVE_HOST",
    "OANDA_PRACTICE_HOST",
    "OANDA_STREAM_HOST",
    "OandaAdapter",
    "OandaEnvironment",
    "OandaInstrument",
    "OandaInstrumentError",
    "OandaOrderError",
    "MacroCalendarBlackout",
    "MacroEvent",
    "PipSpreadGate",
    "RollYieldBreach",
    "RollYieldGuard",
    "SESSION_MAINTENANCE_END_UTC",
    "SESSION_MAINTENANCE_START_UTC",
    "SILO",
    "SpreadGateBreach",
    "TradingSession",
    "VENUE",
    "default_macro_calendar",
    "format_order_payload",
    "fx_week_status",
    "oanda_instrument",
    "oanda_instruments",
    "order_request",
    "payload_flags",
    "pip_spread",
    "roll_yield_bps",
    "session_status",
]

VENUE = "OANDA"
VENUE_CODE = "OANDA"
SILO = "FX_MT5"

#: OANDA v20 REST surface.  The "v20" API is served under ``/v3``.
OANDA_API_VERSION = "v20"
OANDA_PRACTICE_HOST = "https://api-fxpractice.oanda.com"
OANDA_LIVE_HOST = "https://api-fxtrade.oanda.com"
OANDA_STREAM_HOST = "https://stream-fxpractice.oanda.com"
OANDA_DATA_HOST = "https://api-fxpractice.oanda.com"

#: The FX week: opens Sunday 22:00 UTC, closes Friday 22:00 UTC.
FX_WEEK_OPEN_UTC = (6, 22, 0)  # Sunday (Python weekday 6) 22:00
FX_WEEK_CLOSE_UTC = (4, 22, 0)  # Friday (Python weekday 4) 22:00
#: Daily rollover / maintenance window (17:00 New York) - no fills accepted.
SESSION_MAINTENANCE_START_UTC = (20, 58)
SESSION_MAINTENANCE_END_UTC = (21, 6)

MACRO_BLACKOUT_BEFORE_MIN = 15
MACRO_BLACKOUT_AFTER_MIN = 15

MAX_ROLL_YIELD_BPS = 250.0
MIN_DAYS_TO_EXPIRY = 2

_MICROS = 1_000_000


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class OandaInstrumentError(KeyError):
    """Raised when an instrument is not part of the FX silo whitelist."""


class OandaOrderError(ValueError):
    """Raised when an order cannot be expressed as a valid OANDA v20 payload."""


class SpreadGateBreach(ValueError):
    """Raised when a quote breaches the 24/5 pip-spread gate."""

    def __init__(self, message: str, *, receipt: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.receipt = dict(receipt or {})


class RollYieldBreach(ValueError):
    """Raised when a carry/roll position breaches the roll-yield guard."""

    def __init__(self, message: str, *, receipt: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.receipt = dict(receipt or {})


class SessionClosedError(SpreadGateBreach):
    """Raised when the 24/5 FX week (or the daily rollover) is closed."""


# ---------------------------------------------------------------------------
# environment / instruments
# ---------------------------------------------------------------------------
class OandaEnvironment(StrEnum):
    """OANDA environment selector."""

    PRACTICE = "practice"
    LIVE = "live"

    @property
    def host(self) -> str:
        return OANDA_LIVE_HOST if self is OandaEnvironment.LIVE else OANDA_PRACTICE_HOST

    @property
    def is_live(self) -> bool:
        return self is OandaEnvironment.LIVE


@dataclass(slots=True, frozen=True)
class OandaInstrument:
    """A tradable OANDA instrument with its pip geometry and spread ceiling."""

    name: str
    display_precision: int
    pip_size: float
    max_spread_pips: float
    quote_currency: str
    min_units: int = 1
    max_units: int = 50_000_000
    roll_yield_bps: float = MAX_ROLL_YIELD_BPS
    notes: str = ""

    @property
    def is_jpy(self) -> bool:
        return self.quote_currency == "JPY"

    @property
    def is_metal(self) -> bool:
        return self.name.startswith("XAU") or self.name.startswith("XAG")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_precision": self.display_precision,
            "pip_size": self.pip_size,
            "max_spread_pips": self.max_spread_pips,
            "quote_currency": self.quote_currency,
            "min_units": self.min_units,
            "max_units": self.max_units,
            "roll_yield_bps": self.roll_yield_bps,
            "is_jpy": self.is_jpy,
            "is_metal": self.is_metal,
            "notes": self.notes,
        }


#: FX silo whitelist (``sec_001``).  Spread ceilings are the venue's typical
#: retail ceiling, expressed in pips, and are the 24/5 gate's hard limit.
_INSTRUMENTS: tuple[OandaInstrument, ...] = (
    OandaInstrument("EUR_USD", 5, 0.0001, 0.8, "USD"),
    OandaInstrument("GBP_USD", 5, 0.0001, 1.2, "USD"),
    OandaInstrument("USD_JPY", 3, 0.01, 0.9, "JPY"),
    OandaInstrument("EUR_JPY", 3, 0.01, 1.4, "JPY"),
    OandaInstrument("GBP_JPY", 3, 0.01, 2.0, "JPY"),
    OandaInstrument("AUD_USD", 5, 0.0001, 1.0, "USD"),
    OandaInstrument("NZD_USD", 5, 0.0001, 1.5, "USD"),
    OandaInstrument("USD_CAD", 5, 0.0001, 1.2, "CAD"),
    OandaInstrument("USD_CHF", 5, 0.0001, 1.3, "CHF"),
    OandaInstrument("USD_SGD", 5, 0.0001, 1.8, "SGD"),
    OandaInstrument("USD_INR", 5, 0.0001, 4.0, "INR", notes="India silo hedge leg"),
    OandaInstrument("EUR_GBP", 5, 0.0001, 1.5, "GBP"),
    OandaInstrument("EUR_CHF", 5, 0.0001, 1.6, "CHF"),
    OandaInstrument("AUD_JPY", 3, 0.01, 1.6, "JPY"),
    OandaInstrument("XAU_USD", 2, 0.01, 30.0, "USD", notes="Unlevered gold reserve proxy"),
    OandaInstrument("XAG_USD", 3, 0.001, 40.0, "USD"),
)

_INSTRUMENT_INDEX: dict[str, OandaInstrument] = {i.name: i for i in _INSTRUMENTS}


def _normalise_instrument(name: str) -> str:
    raw = str(name or "").strip().upper().replace("/", "_").replace("-", "_")
    if not raw:
        raise OandaInstrumentError("instrument name is required")
    if raw in _INSTRUMENT_INDEX:
        return raw
    # EURUSD -> EUR_USD
    if "_" not in raw and len(raw) == 6:
        candidate = f"{raw[:3]}_{raw[3:]}"
        if candidate in _INSTRUMENT_INDEX:
            return candidate
    raise OandaInstrumentError(f"{name!r} is not an FX-silo OANDA instrument")


def oanda_instrument(name: str) -> OandaInstrument:
    """Return the whitelisted instrument descriptor for ``name``."""

    return _INSTRUMENT_INDEX[_normalise_instrument(name)]


def oanda_instruments() -> tuple[OandaInstrument, ...]:
    """Return the full FX silo instrument whitelist."""

    return _INSTRUMENTS


# ---------------------------------------------------------------------------
# 24/5 session
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class TradingSession:
    """The 24/5 FX week plus the daily rollover maintenance window."""

    week_open: tuple[int, int, int] = FX_WEEK_OPEN_UTC
    week_close: tuple[int, int, int] = FX_WEEK_CLOSE_UTC
    maintenance_start: tuple[int, int] = SESSION_MAINTENANCE_START_UTC
    maintenance_end: tuple[int, int] = SESSION_MAINTENANCE_END_UTC

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _utc(now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(timezone.utc)
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def in_maintenance(self, now: datetime | None = None) -> bool:
        stamp = self._utc(now)
        current = stamp.hour * 60 + stamp.minute
        start = self.maintenance_start[0] * 60 + self.maintenance_start[1]
        end = self.maintenance_end[0] * 60 + self.maintenance_end[1]
        return start <= current < end

    def week_open_at(self, now: datetime | None = None) -> datetime:
        """Most recent FX-week open at or before ``now``."""

        stamp = self._utc(now)
        weekday, hour, minute = self.week_open
        days_back = (stamp.weekday() - weekday) % 7
        candidate = (stamp - timedelta(days=days_back)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate > stamp:
            candidate -= timedelta(days=7)
        return candidate

    def week_close_at(self, now: datetime | None = None) -> datetime:
        """Close of the FX week that contains (or most recently contained) ``now``.

        The FX week is exactly five 24-hour days long, so the close is always
        the Sunday open plus five days (Friday 22:00 UTC).
        """

        return self.week_open_at(now) + timedelta(days=5)

    def is_open(self, now: datetime | None = None) -> bool:
        stamp = self._utc(now)
        if not (self.week_open_at(stamp) <= stamp < self.week_close_at(stamp)):
            return False
        return not self.in_maintenance(stamp)

    def next_open(self, now: datetime | None = None) -> datetime:
        stamp = self._utc(now)
        if self.in_maintenance(stamp):
            end = stamp.replace(
                hour=self.maintenance_end[0],
                minute=self.maintenance_end[1],
                second=0,
                microsecond=0,
            )
            if end > stamp:
                return end
        if self.is_open(stamp):
            return stamp
        weekday, hour, minute = self.week_open
        days_ahead = (weekday - stamp.weekday()) % 7
        candidate = (stamp + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= stamp:
            candidate += timedelta(days=7)
        return candidate

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        stamp = self._utc(now)
        return {
            "venue": VENUE,
            "silo": SILO,
            "calendar": "24/5",
            "is_open": self.is_open(stamp),
            "in_maintenance": self.in_maintenance(stamp),
            "now_utc": stamp.isoformat(),
            "week_open_utc": self.week_open_at(stamp).isoformat(),
            "week_close_utc": self.week_close_at(stamp).isoformat(),
            "next_open_utc": self.next_open(stamp).isoformat(),
            "maintenance_window_utc": [
                f"{self.maintenance_start[0]:02d}:{self.maintenance_start[1]:02d}",
                f"{self.maintenance_end[0]:02d}:{self.maintenance_end[1]:02d}",
            ],
        }


_DEFAULT_SESSION = TradingSession()


def session_status(now: datetime | None = None) -> dict[str, Any]:
    """Return the 24/5 FX-week status receipt."""

    return _DEFAULT_SESSION.status(now)


def fx_week_status(now: datetime | None = None) -> dict[str, Any]:
    """Alias of :func:`session_status` used by the silo readiness report."""

    return _DEFAULT_SESSION.status(now)


# ---------------------------------------------------------------------------
# pip-spread gate
# ---------------------------------------------------------------------------
def pip_spread(instrument: str, bid: float, ask: float) -> float:
    """Spread in pips for ``instrument`` between ``bid`` and ``ask``."""

    spec = oanda_instrument(instrument)
    bid_f, ask_f = float(bid), float(ask)
    if not (math.isfinite(bid_f) and math.isfinite(ask_f)):
        raise SpreadGateBreach(f"{spec.name}: non-finite quote ({bid!r}/{ask!r})")
    if bid_f <= 0.0 or ask_f <= 0.0:
        raise SpreadGateBreach(f"{spec.name}: non-positive quote ({bid!r}/{ask!r})")
    if ask_f < bid_f:
        raise SpreadGateBreach(f"{spec.name}: crossed quote ({bid!r}/{ask!r})")
    return (ask_f - bid_f) / spec.pip_size


@dataclass(slots=True)
class PipSpreadGate:
    """The 24/5 pip-spread gate.

    A quote passes only when the FX week is open *and* the spread is inside the
    instrument's pip ceiling.  ``bps`` is reported for the cost-floor maths.
    """

    session: TradingSession = field(default_factory=TradingSession)
    max_spread_pips: Mapping[str, float] = field(default_factory=dict)
    max_spread_bps: float = 12.0
    breach_count: int = 0
    pass_count: int = 0

    def ceiling_pips(self, instrument: str) -> float:
        spec = oanda_instrument(instrument)
        return float(self.max_spread_pips.get(spec.name, spec.max_spread_pips))

    def check(
        self,
        instrument: str,
        bid: float,
        ask: float,
        *,
        now: datetime | None = None,
        require_open: bool = True,
    ) -> dict[str, Any]:
        spec = oanda_instrument(instrument)
        stamp = self.session._utc(now)
        session_open = self.session.is_open(stamp)
        pips = pip_spread(spec.name, bid, ask)
        mid = (float(bid) + float(ask)) / 2.0
        bps = ((float(ask) - float(bid)) / mid) * 10_000.0 if mid > 0 else math.inf
        ceiling = self.ceiling_pips(spec.name)
        receipt = {
            "venue": VENUE,
            "silo": SILO,
            "instrument": spec.name,
            "bid": float(bid),
            "ask": float(ask),
            "pip_size": spec.pip_size,
            "spread_pips": round(pips, 6),
            "spread_bps": round(bps, 6),
            "ceiling_pips": ceiling,
            "max_spread_bps": self.max_spread_bps,
            "session_open": session_open,
            "in_maintenance": self.session.in_maintenance(stamp),
            "now_utc": stamp.isoformat(),
            "calendar": "24/5",
        }
        if require_open and not session_open:
            self.breach_count += 1
            raise SessionClosedError(
                f"{spec.name}: FX week closed at {stamp.isoformat()} "
                f"(next open {self.session.next_open(stamp).isoformat()})",
                receipt={**receipt, "gate": "session"},
            )
        if pips > ceiling:
            self.breach_count += 1
            raise SpreadGateBreach(
                f"{spec.name}: spread {pips:.3f} pips breaches the {ceiling:.3f} pip "
                f"24/5 gate",
                receipt={**receipt, "gate": "pip_spread"},
            )
        if bps > self.max_spread_bps:
            self.breach_count += 1
            raise SpreadGateBreach(
                f"{spec.name}: spread {bps:.3f} bps breaches the "
                f"{self.max_spread_bps:.3f} bps ceiling",
                receipt={**receipt, "gate": "bps_spread"},
            )
        self.pass_count += 1
        return {**receipt, "gate": "pip_spread", "passed": True}

    def status(self) -> dict[str, Any]:
        return {
            "venue": VENUE,
            "silo": SILO,
            "gate": "24/5 pip-spread",
            "pass_count": self.pass_count,
            "breach_count": self.breach_count,
            "instruments": {
                spec.name: {
                    "pip_size": spec.pip_size,
                    "ceiling_pips": self.ceiling_pips(spec.name),
                }
                for spec in _INSTRUMENTS
            },
            "max_spread_bps": self.max_spread_bps,
        }


# ---------------------------------------------------------------------------
# macro calendar blackout
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class MacroEvent:
    """A high-impact macro release with its blackout window."""

    name: str
    currency: str
    when_utc: datetime
    impact: str = "high"
    blackout_before_min: int = MACRO_BLACKOUT_BEFORE_MIN
    blackout_after_min: int = MACRO_BLACKOUT_AFTER_MIN

    def __post_init__(self) -> None:
        stamp = self.when_utc
        if stamp.tzinfo is None:
            object.__setattr__(self, "when_utc", stamp.replace(tzinfo=timezone.utc))
        else:
            object.__setattr__(self, "when_utc", stamp.astimezone(timezone.utc))

    @property
    def window(self) -> tuple[datetime, datetime]:
        return (
            self.when_utc - timedelta(minutes=self.blackout_before_min),
            self.when_utc + timedelta(minutes=self.blackout_after_min),
        )

    def to_dict(self) -> dict[str, Any]:
        start, end = self.window
        return {
            "name": self.name,
            "currency": self.currency,
            "impact": self.impact,
            "when_utc": self.when_utc.isoformat(),
            "blackout_start_utc": start.isoformat(),
            "blackout_end_utc": end.isoformat(),
        }


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the ``n``-th ``weekday`` of ``month`` (1-based)."""

    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (n - 1) * 7
    last = calendar.monthrange(year, month)[1]
    while day > last:
        day -= 7
    return date(year, month, day)


def _recurring_events(year: int) -> list[MacroEvent]:
    """Deterministic high-impact macro calendar for ``year``.

    The schedule is the conventional cadence rather than a vendor feed:

    * US Non-Farm Payrolls - first Friday, 12:30 UTC
    * US CPI - second Tuesday, 12:30 UTC
    * FOMC decision - third Wednesday, 18:00 UTC
    * ECB decision - second Thursday, 12:15 UTC
    * BoE decision - first Thursday, 11:00 UTC
    * BoJ decision - third Friday, 03:00 UTC
    """

    events: list[MacroEvent] = []
    for month in range(1, 13):
        events.append(
            MacroEvent(
                "US Non-Farm Payrolls",
                "USD",
                datetime.combine(_nth_weekday(year, month, 4, 1), time(12, 30), timezone.utc),
            )
        )
        events.append(
            MacroEvent(
                "US CPI",
                "USD",
                datetime.combine(_nth_weekday(year, month, 1, 2), time(12, 30), timezone.utc),
            )
        )
        events.append(
            MacroEvent(
                "FOMC Rate Decision",
                "USD",
                datetime.combine(_nth_weekday(year, month, 2, 3), time(18, 0), timezone.utc),
                blackout_before_min=30,
                blackout_after_min=30,
            )
        )
        events.append(
            MacroEvent(
                "ECB Rate Decision",
                "EUR",
                datetime.combine(_nth_weekday(year, month, 3, 2), time(12, 15), timezone.utc),
            )
        )
        events.append(
            MacroEvent(
                "BoE Rate Decision",
                "GBP",
                datetime.combine(_nth_weekday(year, month, 3, 1), time(11, 0), timezone.utc),
            )
        )
        events.append(
            MacroEvent(
                "BoJ Rate Decision",
                "JPY",
                datetime.combine(_nth_weekday(year, month, 4, 3), time(3, 0), timezone.utc),
            )
        )
    return events


def default_macro_calendar(year: int | None = None) -> tuple[MacroEvent, ...]:
    """Return the deterministic macro calendar (``year`` defaults to UTC now)."""

    stamp_year = year or datetime.now(timezone.utc).year
    return tuple(_recurring_events(stamp_year))


#: Calendar seeded for the current UTC year plus the next one so a window that
#: straddles New Year is still covered.
DEFAULT_MACRO_CALENDAR: tuple[MacroEvent, ...] = tuple(
    _recurring_events(datetime.now(timezone.utc).year)
    + _recurring_events(datetime.now(timezone.utc).year + 1)
)


@dataclass(slots=True)
class MacroCalendarBlackout:
    """Refuse new risk inside a high-impact macro release window."""

    events: tuple[MacroEvent, ...] = DEFAULT_MACRO_CALENDAR
    currencies: tuple[str, ...] = ()
    blackout_count: int = 0

    @classmethod
    def from_env(cls, variable: str = "NIOS_MACRO_CALENDAR") -> "MacroCalendarBlackout":
        """Load a calendar JSON file from ``variable`` when present."""

        raw = os.environ.get(variable, "").strip()
        if not raw:
            return cls()
        path = Path(raw)
        if not path.is_file():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        events: list[MacroEvent] = []
        for row in payload if isinstance(payload, list) else payload.get("events", []):
            if not isinstance(row, Mapping):
                continue
            when = row.get("when_utc") or row.get("when")
            if not when:
                continue
            try:
                stamp = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            except ValueError:
                continue
            events.append(
                MacroEvent(
                    str(row.get("name", "macro")),
                    str(row.get("currency", "")).upper(),
                    stamp,
                    str(row.get("impact", "high")),
                    int(row.get("blackout_before_min", MACRO_BLACKOUT_BEFORE_MIN)),
                    int(row.get("blackout_after_min", MACRO_BLACKOUT_AFTER_MIN)),
                )
            )
        return cls(tuple(events) or DEFAULT_MACRO_CALENDAR)

    def add(self, event: MacroEvent) -> "MacroCalendarBlackout":
        self.events = (*self.events, event)
        return self

    def _applies(self, event: MacroEvent, currency: str | None) -> bool:
        if event.impact.lower() not in {"high", "3", "red"}:
            return False
        if self.currencies and event.currency not in self.currencies:
            return False
        if currency and event.currency and event.currency != currency.upper():
            return False
        return True

    def active(
        self,
        now: datetime | None = None,
        *,
        currency: str | None = None,
    ) -> list[MacroEvent]:
        stamp = TradingSession._utc(now)
        hits: list[MacroEvent] = []
        for event in self.events:
            if not self._applies(event, currency):
                continue
            start, end = event.window
            if start <= stamp <= end:
                hits.append(event)
        return hits

    def next_event(
        self,
        now: datetime | None = None,
        *,
        currency: str | None = None,
    ) -> MacroEvent | None:
        stamp = TradingSession._utc(now)
        upcoming = [
            event
            for event in self.events
            if self._applies(event, currency) and event.window[1] >= stamp
        ]
        return min(upcoming, key=lambda e: e.when_utc, default=None)

    def check(
        self,
        now: datetime | None = None,
        *,
        currency: str | None = None,
    ) -> dict[str, Any]:
        stamp = TradingSession._utc(now)
        hits = self.active(stamp, currency=currency)
        upcoming = self.next_event(stamp, currency=currency)
        receipt = {
            "venue": VENUE,
            "silo": SILO,
            "gate": "macro_calendar_blackout",
            "now_utc": stamp.isoformat(),
            "currency": (currency or "").upper() or None,
            "active_events": [event.to_dict() for event in hits],
            "next_event": upcoming.to_dict() if upcoming else None,
            "blacked_out": bool(hits),
        }
        if hits:
            self.blackout_count += 1
            names = ", ".join(event.name for event in hits)
            raise SpreadGateBreach(
                f"macro blackout active ({names}); no new FX risk until "
                f"{max(event.window[1] for event in hits).isoformat()}",
                receipt={**receipt, "gate": "macro_calendar_blackout"},
            )
        return receipt

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        stamp = TradingSession._utc(now)
        return {
            "venue": VENUE,
            "silo": SILO,
            "gate": "macro_calendar_blackout",
            "event_count": len(self.events),
            "blackout_count": self.blackout_count,
            "next_event": (
                self.next_event(stamp).to_dict() if self.next_event(stamp) else None
            ),
        }


# ---------------------------------------------------------------------------
# roll-yield guard
# ---------------------------------------------------------------------------
def roll_yield_bps(spot: float, forward: float, days_to_expiry: int) -> float:
    """Annualised roll yield in basis points.

    ``((forward - spot) / spot) * (365 / days_to_expiry) * 10_000``
    """

    spot_f, fwd_f = float(spot), float(forward)
    days = int(days_to_expiry)
    if not (math.isfinite(spot_f) and math.isfinite(fwd_f)):
        raise RollYieldBreach(f"non-finite roll inputs (spot={spot!r}, forward={forward!r})")
    if spot_f <= 0.0:
        raise RollYieldBreach(f"spot must be positive (got {spot!r})")
    if days <= 0:
        raise RollYieldBreach(f"days_to_expiry must be positive (got {days!r})")
    return ((fwd_f - spot_f) / spot_f) * (365.0 / days) * 10_000.0


@dataclass(slots=True)
class RollYieldGuard:
    """Futures roll-yield guard.

    Refuses a roll when the contract is inside ``min_days_to_expiry`` or the
    annualised roll yield exceeds ``max_roll_yield_bps`` (a carry that rich is a
    data error or a squeeze, not a trade).
    """

    max_roll_yield_bps: float = MAX_ROLL_YIELD_BPS
    min_days_to_expiry: int = MIN_DAYS_TO_EXPIRY
    breach_count: int = 0

    def check(
        self,
        instrument: str,
        *,
        spot: float,
        forward: float,
        days_to_expiry: int,
    ) -> dict[str, Any]:
        spec = oanda_instrument(instrument)
        days = int(days_to_expiry)
        ceiling = float(min(spec.roll_yield_bps, self.max_roll_yield_bps))
        receipt = {
            "venue": VENUE,
            "silo": SILO,
            "gate": "roll_yield",
            "instrument": spec.name,
            "spot": float(spot),
            "forward": float(forward),
            "days_to_expiry": days,
            "min_days_to_expiry": self.min_days_to_expiry,
            "ceiling_bps": ceiling,
        }
        if days < self.min_days_to_expiry:
            self.breach_count += 1
            raise RollYieldBreach(
                f"{spec.name}: {days} day(s) to expiry is inside the "
                f"{self.min_days_to_expiry} day roll guard",
                receipt={**receipt, "roll_yield_bps": None},
            )
        bps = roll_yield_bps(spot, forward, days)
        receipt["roll_yield_bps"] = round(bps, 6)
        if abs(bps) > ceiling:
            self.breach_count += 1
            raise RollYieldBreach(
                f"{spec.name}: annualised roll yield {bps:.3f} bps breaches the "
                f"{ceiling:.3f} bps guard",
                receipt=receipt,
            )
        return {**receipt, "passed": True}

    def status(self) -> dict[str, Any]:
        return {
            "venue": VENUE,
            "silo": SILO,
            "gate": "roll_yield",
            "max_roll_yield_bps": self.max_roll_yield_bps,
            "min_days_to_expiry": self.min_days_to_expiry,
            "breach_count": self.breach_count,
        }


# ---------------------------------------------------------------------------
# OANDA v20 order payload
# ---------------------------------------------------------------------------
def _price(value: float, precision: int) -> str:
    return f"{float(value):.{precision}f}"


def _units(quantity: float, side: str, spec: OandaInstrument) -> str:
    qty = float(quantity)
    if not math.isfinite(qty) or qty <= 0:
        raise OandaOrderError(f"{spec.name}: quantity must be positive (got {quantity!r})")
    units = int(round(qty))
    if units < spec.min_units:
        raise OandaOrderError(
            f"{spec.name}: {units} units is below the venue minimum {spec.min_units}"
        )
    if units > spec.max_units:
        raise OandaOrderError(
            f"{spec.name}: {units} units exceeds the venue maximum {spec.max_units}"
        )
    return str(units if side == "BUY" else -units)


def format_order_payload(order: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Format a NIOS signed order into an OANDA v20 order payload.

    ``order`` may be a mapping or an object exposing the same attributes
    (``symbol``/``instrument``, ``side``, ``quantity``/``qty``, ``limit_price``,
    ``order_type``, ``time_in_force``, ``proposal_id``, ``take_profit``,
    ``stop_loss``).
    """

    def _get(*names: str, default: Any = None) -> Any:
        if isinstance(order, Mapping):
            for name in names:
                if name in order and order[name] is not None:
                    return order[name]
            return default
        for name in names:
            value = getattr(order, name, None)
            if value is not None:
                return value
        return default

    raw_symbol = _get("symbol", "instrument", "ticker", default="")
    spec = oanda_instrument(str(raw_symbol))
    side = str(_get("side", "direction", default="BUY")).strip().upper()
    if side in {"L", "LONG"}:
        side = "BUY"
    elif side in {"S", "SHORT"}:
        side = "SELL"
    if side not in {"BUY", "SELL"}:
        raise OandaOrderError(f"{spec.name}: unsupported side {side!r}")

    quantity = _get("quantity", "qty", "units", default=0)
    price = _get("limit_price", "price", "px")
    if price is None:
        raise OandaOrderError(f"{spec.name}: limit_price is required for a maker order")
    price_f = float(price)
    if not math.isfinite(price_f) or price_f <= 0:
        raise OandaOrderError(f"{spec.name}: invalid limit_price {price!r}")

    order_type = str(_get("order_type", "type", default="LIMIT")).strip().upper()
    oanda_type = {
        "LIMIT": "LIMIT",
        "LMT": "LIMIT",
        "MARKET": "MARKET",
        "MKT": "MARKET",
        "STOP": "STOP",
        "STP": "STOP",
        "MARKET_IF_TOUCHED": "MARKET_IF_TOUCHED",
        "MIT": "MARKET_IF_TOUCHED",
    }.get(order_type)
    if oanda_type is None:
        raise OandaOrderError(f"{spec.name}: unsupported order_type {order_type!r}")

    tif = str(_get("time_in_force", "tif", default="GTC")).strip().upper()
    tif = {"DAY": "GTC", "GTC": "GTC", "IOC": "IOC", "FOK": "FOK"}.get(tif, "GTC")

    client_id = str(_get("client_order_id", "proposal_id", default="") or "").strip()
    tag = str(_get("tag", default="nios") or "nios")[:128]

    payload: dict[str, Any] = {
        "type": oanda_type,
        "instrument": spec.name,
        "units": _units(quantity, side, spec),
        "timeInForce": tif,
        "positionFill": "DEFAULT",
    }
    if oanda_type in {"LIMIT", "STOP", "MARKET_IF_TOUCHED"}:
        payload["price"] = _price(price_f, spec.display_precision)
    if client_id:
        payload["clientExtensions"] = {"id": client_id[:128], "tag": tag}

    take_profit = _get("take_profit", "tp")
    stop_loss = _get("stop_loss", "sl")
    if take_profit is not None:
        payload["takeProfitOnFill"] = {
            "price": _price(float(take_profit), spec.display_precision)
        }
    if stop_loss is not None:
        payload["stopLossOnFill"] = {
            "price": _price(float(stop_loss), spec.display_precision),
            "timeInForce": "GTC",
        }
    return {"order": payload}


def order_request(
    order: Mapping[str, Any] | Any,
    *,
    account_id: str,
    environment: OandaEnvironment | str = OandaEnvironment.PRACTICE,
) -> dict[str, Any]:
    """Build the full ``POST /v3/accounts/{id}/orders`` request envelope."""

    env = (
        environment
        if isinstance(environment, OandaEnvironment)
        else OandaEnvironment(str(environment).lower())
    )
    if not str(account_id or "").strip():
        raise OandaOrderError("account_id is required")
    return {
        "method": "POST",
        "url": f"{env.host}/v3/accounts/{account_id}/orders",
        "headers": {
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        },
        "json": format_order_payload(order),
    }


def payload_flags(payload: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Describe the routing flags carried by an OANDA payload."""

    body = payload.get("order", payload) if isinstance(payload, Mapping) else {}
    spec = oanda_instrument(str(body.get("instrument", "EUR_USD")))
    units = int(body.get("units", 0) or 0)
    return {
        "venue": VENUE,
        "venue_code": VENUE_CODE,
        "silo": SILO,
        "instrument": spec.name,
        "post_only": False,
        "product": "spot",
        "settlement_cycle": "T+0",
        "settlement_days": 0,
        "price_tick": spec.pip_size,
        "quote_currency": spec.quote_currency,
        "direction": "BUY" if units >= 0 else "SELL",
        "units": abs(units),
        "time_in_force": body.get("timeInForce", "GTC"),
        "order_type": body.get("type", "LIMIT"),
        "calendar": "24/5",
    }


# ---------------------------------------------------------------------------
# adapter facade
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class OandaAdapter:
    """The FX silo's OANDA v20 venue adapter."""

    venue: str = VENUE
    silo: str = SILO
    environment: OandaEnvironment = OandaEnvironment.PRACTICE
    account_id: str = ""
    session: TradingSession = field(default_factory=TradingSession)
    spread_gate: PipSpreadGate = field(default_factory=PipSpreadGate)
    blackout: MacroCalendarBlackout = field(default_factory=MacroCalendarBlackout)
    roll_guard: RollYieldGuard = field(default_factory=RollYieldGuard)

    @property
    def spec(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "silo": self.silo,
            "api": OANDA_API_VERSION,
            "host": self.environment.host,
            "environment": self.environment.value,
            "calendar": "24/5",
            "settlement_cycle": "T+0",
            "gates": ["24/5 pip-spread", "macro_calendar_blackout", "roll_yield"],
            "instruments": [spec.name for spec in _INSTRUMENTS],
        }

    def build_order(self, order: Mapping[str, Any] | Any) -> dict[str, Any]:
        return format_order_payload(order)

    def describe(self, order: Mapping[str, Any] | Any) -> dict[str, Any]:
        payload = format_order_payload(order)
        return {
            "venue": self.venue,
            "silo": self.silo,
            "environment": self.environment.value,
            "payload": payload,
            "flags": payload_flags(payload),
        }

    def gate(
        self,
        order: Mapping[str, Any] | Any,
        *,
        bid: float,
        ask: float,
        now: datetime | None = None,
        forward: float | None = None,
        days_to_expiry: int | None = None,
    ) -> dict[str, Any]:
        """Run the 24/5 spread gate, the macro blackout and the roll guard."""

        payload = format_order_payload(order)
        instrument = payload["order"]["instrument"]
        quote_currency = oanda_instrument(instrument).quote_currency
        receipts = {
            "spread": self.spread_gate.check(instrument, bid, ask, now=now),
            "macro": self.blackout.check(now, currency=quote_currency),
        }
        if forward is not None and days_to_expiry is not None:
            receipts["roll"] = self.roll_guard.check(
                instrument,
                spot=float((bid + ask) / 2.0),
                forward=forward,
                days_to_expiry=days_to_expiry,
            )
        return {
            "venue": self.venue,
            "silo": self.silo,
            "instrument": instrument,
            "payload": payload,
            "flags": payload_flags(payload),
            "gates": receipts,
            "passed": True,
        }

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "session": self.session.status(now),
            "spread_gate": self.spread_gate.status(),
            "blackout": self.blackout.status(now),
            "roll_guard": self.roll_guard.status(),
        }


#: Module-level facade used by ``silo_registry.dispatch_signed_order``.
ADAPTER = OandaAdapter()
ADAPTER_SPEC = ADAPTER.spec
