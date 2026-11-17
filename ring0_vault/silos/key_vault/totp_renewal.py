"""Pre-market TOTP renewal state machine (SEBI B4).

Governing requirements
----------------------
* **SEBI B4** -- the exchange mandates a *daily* TOTP re-authentication for
  broker API sessions.  A session that was renewed yesterday is dead today, so
  the renewal must run **before** the 09:15 IST NSE open, not at first order.
* ``03:00 UTC == 08:30 IST`` -- the renewal window opens 45 minutes before the
  open (``IST_OFFSET_MINUTES = 330``).
* ``sec_004`` -- TOTP material is broker credential material and stays in
  ``ring0_vault/silos/key_vault``; Ring 2 never sees it.
* ``sec_010`` -- renewal is an off-hot-path control-plane action; nothing here
  touches the tick path.
* ``sec_018`` Law 5 -- the daily cycle is a state machine, not an ad-hoc script:
  every silo is either provably live or provably not, and a failure is visible
  rather than silent.

State machine (strict allowlist)
--------------------------------
``PENDING -> RENEWING -> RENEWED | FAILED``, ``RENEWED -> EXPIRED`` and
``FAILED -> RENEWING`` (retry).  Every other edge raises :class:`ValueError` via
:func:`transition`, so a bug cannot quietly resurrect a dead session.

Because ``EXPIRED`` has no outgoing edge in that allowlist, a record that has
already expired is *re-armed* before a new daily cycle: re-arming is a
registration-level reset to ``PENDING`` (attempts preserved, error cleared), not
a state transition, and it is the only way a new day can begin after a session
went stale.  ``renew`` performs it automatically for ``RENEWED``/``EXPIRED``
records; :func:`transition` remains strictly allowlisted.

A record past its TTL reports ``EXPIRED`` lazily in
:meth:`TotpRenewalRecord.to_dict` -- the stored state is only moved by
:meth:`PreMarketTotpRenewal.tick` or by an explicit :func:`transition`, so
telemetry is a pure read.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Sequence

__all__ = [
    "RENEWAL_UTC_HOUR",
    "IST_OFFSET_MINUTES",
    "DEFAULT_TTL_SECONDS",
    "TotpRenewalState",
    "TotpRenewalRecord",
    "PreMarketTotpRenewal",
    "transition",
]

#: UTC hour at which the pre-market renewal window opens (03:00 UTC).
RENEWAL_UTC_HOUR = 3

#: IST is UTC+05:30, expressed in minutes (no DST in India).
IST_OFFSET_MINUTES = 330

#: Default session TTL: 12 hours, comfortably past the NSE close.
DEFAULT_TTL_SECONDS = 12 * 3600

_NS_PER_SECOND = 1_000_000_000
_NS_PER_DAY = 86_400 * _NS_PER_SECOND
_SECONDS_PER_DAY = 86_400

#: Default silo/venue pairs the pre-market job must renew.
_DEFAULT_SILOS: tuple[tuple[str, str], ...] = (
    ("INDIA_ZERODHA_DHAN", "zerodha"),
    ("INDIA_ZERODHA_DHAN", "dhan"),
)


class TotpRenewalState(StrEnum):
    """Lifecycle of one silo/venue TOTP session (SEBI B4)."""

    PENDING = "PENDING"
    RENEWING = "RENEWING"
    RENEWED = "RENEWED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


#: The strict allowlist.  ``EXPIRED`` is intentionally terminal here; see the
#: module docstring for the re-arm path that starts a new daily cycle.
_ALLOWED_TRANSITIONS: dict[TotpRenewalState, frozenset[TotpRenewalState]] = {
    TotpRenewalState.PENDING: frozenset({TotpRenewalState.RENEWING}),
    TotpRenewalState.RENEWING: frozenset(
        {TotpRenewalState.RENEWED, TotpRenewalState.FAILED}
    ),
    TotpRenewalState.RENEWED: frozenset({TotpRenewalState.EXPIRED}),
    TotpRenewalState.FAILED: frozenset({TotpRenewalState.RENEWING}),
    TotpRenewalState.EXPIRED: frozenset(),
}


def _resolve(now_ns: int | None) -> int:
    """Return an explicit ``now_ns`` or the current clock reading (UTC)."""

    return int(time.time_ns()) if now_ns is None else int(now_ns)


def _ist_label(utc_hour: int) -> str:
    """Render a UTC hour as its IST ``HH:MM IST`` equivalent."""

    total = (int(utc_hour) * 60 + IST_OFFSET_MINUTES) % (_SECONDS_PER_DAY // 60)
    return f"{total // 60:02d}:{total % 60:02d} IST"


def transition(
    record: TotpRenewalRecord,
    target: TotpRenewalState,
    *,
    now_ns: int | None = None,
) -> TotpRenewalRecord:
    """Move ``record`` to ``target`` if and only if the edge is allowlisted.

    ``now_ns`` is used only to stamp ``renewed_at_utc``/``expires_at_utc`` on a
    ``RENEWING -> RENEWED`` edge; every other edge ignores it.
    """

    target = TotpRenewalState(target)
    allowed = _ALLOWED_TRANSITIONS.get(record.state, frozenset())
    if target not in allowed:
        raise ValueError(
            f"illegal TOTP renewal transition {record.state} -> {target} "
            f"for {record.silo}/{record.venue}"
        )
    record.state = target
    if target is TotpRenewalState.EXPIRED and now_ns is not None:
        # Keep the expiry stamp truthful if the caller observed the crossing.
        if int(now_ns) > record.expires_at_utc:
            record.expires_at_utc = int(now_ns)
    return record


@dataclass(slots=True)
class TotpRenewalRecord:
    """One silo/venue TOTP session and its renewal bookkeeping (SEBI B4).

    ``last_window_day`` is the integer UTC day index
    (``now_ns // 86_400e9``) of the window this record was last renewed in, so
    "already renewed today" is an integer comparison rather than a date string.
    """

    silo: str
    venue: str
    state: TotpRenewalState = TotpRenewalState.PENDING
    renewed_at_utc: int = 0
    expires_at_utc: int = 0
    attempts: int = 0
    last_error: str = ""
    last_window_day: int = -1

    @property
    def key(self) -> str:
        """Stable ``<silo>:<venue>`` record key used by :meth:`PreMarketTotpRenewal.due`."""

        return f"{self.silo}:{self.venue}"

    def ttl_seconds_remaining(self, now_ns: int | None = None) -> int:
        """Whole seconds of TTL left, clamped at ``0`` (never negative)."""

        remaining_ns = int(self.expires_at_utc) - _resolve(now_ns)
        if remaining_ns <= 0:
            return 0
        return remaining_ns // _NS_PER_SECOND

    def is_live(self, now_ns: int | None = None) -> bool:
        """``True`` only for a ``RENEWED`` record strictly inside its TTL."""

        return (
            self.state is TotpRenewalState.RENEWED
            and _resolve(now_ns) < int(self.expires_at_utc)
        )

    def to_dict(self, now_ns: int | None = None) -> dict[str, Any]:
        """Return a JSON-safe view; a stale ``RENEWED`` record reports ``EXPIRED``.

        The read is pure: the stored ``state`` is not mutated, so telemetry can
        never race the state machine.
        """

        now = _resolve(now_ns)
        effective = self.state
        if self.state is TotpRenewalState.RENEWED and now >= int(self.expires_at_utc):
            effective = TotpRenewalState.EXPIRED
        return {
            "silo": self.silo,
            "venue": self.venue,
            "key": self.key,
            "state": str(effective),
            "stored_state": str(self.state),
            "renewed_at_utc": int(self.renewed_at_utc),
            "expires_at_utc": int(self.expires_at_utc),
            "ttl_seconds_remaining": self.ttl_seconds_remaining(now_ns=now),
            "is_live": self.state is TotpRenewalState.RENEWED
            and now < int(self.expires_at_utc),
            "attempts": int(self.attempts),
            "last_error": self.last_error,
            "last_window_day": int(self.last_window_day),
        }


class PreMarketTotpRenewal:
    """Daily pre-market TOTP renewal for every silo/venue pair (SEBI B4).

    The job is deterministic given ``now_ns``: the window is a pure function of
    the UTC time of day, so a replay produces the same due list and the same
    transitions.  ``renewer`` is injected (default: always succeeds) so the
    state machine can be tested without a broker or a TOTP secret.
    """

    def __init__(
        self,
        *,
        silos: Sequence[tuple[str, str]] = _DEFAULT_SILOS,
        renew_at_utc_hour: int = RENEWAL_UTC_HOUR,
        lead_minutes: int = 15,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        renewer: Callable[[str, str], bool] | None = None,
    ) -> None:
        if not 0 <= int(renew_at_utc_hour) <= 23:
            raise ValueError("renew_at_utc_hour must be within 0..23")
        if not 1 <= int(lead_minutes) <= _SECONDS_PER_DAY // 60:
            raise ValueError("lead_minutes must be within 1..1440")
        if int(ttl_seconds) <= 0:
            raise ValueError("ttl_seconds must be > 0")

        self._renew_at_utc_hour = int(renew_at_utc_hour)
        self._lead_minutes = int(lead_minutes)
        self._ttl_seconds = int(ttl_seconds)
        self._ttl_ns = self._ttl_seconds * _NS_PER_SECOND
        self._renewer: Callable[[str, str], bool] = renewer or (lambda _silo, _venue: True)
        self._records: dict[str, TotpRenewalRecord] = {}
        for silo, venue in silos:
            self.register(silo, venue)

    # -- introspection ----------------------------------------------------
    @property
    def renew_at_utc_hour(self) -> int:
        """UTC hour the renewal window opens (default ``03``)."""

        return self._renew_at_utc_hour

    @property
    def lead_minutes(self) -> int:
        """Minutes the renewal window stays open."""

        return self._lead_minutes

    @property
    def ttl_seconds(self) -> int:
        """Session TTL applied on a successful renewal."""

        return self._ttl_seconds

    def keys(self) -> list[str]:
        """Registered ``<silo>:<venue>`` keys in registration order."""

        return list(self._records)

    # -- registration -----------------------------------------------------
    def register(self, silo: str, venue: str) -> TotpRenewalRecord:
        """Register (or fetch) the record for ``silo``/``venue``."""

        key = f"{silo}:{venue}"
        record = self._records.get(key)
        if record is None:
            record = TotpRenewalRecord(silo=str(silo), venue=str(venue))
            self._records[key] = record
        return record

    # -- window arithmetic ------------------------------------------------
    def _window(self, now_ns: int) -> tuple[bool, int]:
        """Return ``(window_open, utc_day_index)`` for ``now_ns``.

        The window is ``[hour:00, hour:00 + lead_minutes)`` in UTC time-of-day;
        it never wraps past midnight for the supported hours.
        """

        day = now_ns // _NS_PER_DAY
        second_of_day = (now_ns % _NS_PER_DAY) // _NS_PER_SECOND
        start = self._renew_at_utc_hour * 3600
        end = start + self._lead_minutes * 60
        return (start <= second_of_day < end), int(day)

    def window_open(self, now_ns: int | None = None) -> bool:
        """``True`` when the current UTC time-of-day is inside the window."""

        return self._window(_resolve(now_ns))[0]

    # -- due / renew ------------------------------------------------------
    def due(self, now_ns: int | None = None) -> list[str]:
        """Return the ``<silo>:<venue>`` keys whose renewal window is open.

        A record is due when the window is open *and* it has not already been
        renewed inside today's window (``last_window_day``).  A record mid-call
        (``RENEWING``) is not listed twice.
        """

        now = _resolve(now_ns)
        open_now, day = self._window(now)
        if not open_now:
            return []
        return [
            key
            for key, record in self._records.items()
            if record.state is not TotpRenewalState.RENEWING
            and int(record.last_window_day) != day
        ]

    def _rearm(self, record: TotpRenewalRecord) -> TotpRenewalRecord:
        """Re-arm a stale record for a new cycle (registration-level reset).

        ``EXPIRED`` is terminal in the allowlist, so a new daily cycle cannot be
        expressed as a transition.  Re-arming resets the state to ``PENDING``
        while preserving ``attempts`` (audit history) and clearing the error.
        """

        record.state = TotpRenewalState.PENDING
        record.last_error = ""
        return record

    def renew(
        self,
        silo: str,
        venue: str,
        *,
        now_ns: int | None = None,
        force: bool = False,
    ) -> TotpRenewalRecord:
        """Run one renewal attempt for ``silo``/``venue``.

        ``force=True`` bypasses the *window* check only; the state machine still
        applies (``PENDING``/``FAILED`` -> ``RENEWING`` -> ``RENEWED``/``FAILED``).
        A stale ``RENEWED``/``EXPIRED`` record is re-armed first so a new day can
        start.  A closed window without ``force`` raises :class:`ValueError`.
        """

        now = _resolve(now_ns)
        record = self.register(silo, venue)
        open_now, day = self._window(now)
        if not force:
            if not open_now:
                raise ValueError(
                    f"TOTP renewal window closed for {record.key}: opens at "
                    f"{self._renew_at_utc_hour:02d}:00 UTC for "
                    f"{self._lead_minutes} minutes"
                )
            if int(record.last_window_day) == day:
                raise ValueError(
                    f"TOTP already renewed for today's window: {record.key} "
                    f"(day {day})"
                )

        if record.state in (TotpRenewalState.RENEWED, TotpRenewalState.EXPIRED):
            self._rearm(record)

        transition(record, TotpRenewalState.RENEWING, now_ns=now)
        record.attempts += 1
        try:
            renewed = bool(self._renewer(record.silo, record.venue))
        except Exception as exc:  # noqa: BLE001 - a renewer failure is data
            record.last_error = f"{type(exc).__name__}: {exc}"
            return transition(record, TotpRenewalState.FAILED, now_ns=now)
        if not renewed:
            record.last_error = "renewer returned False"
            return transition(record, TotpRenewalState.FAILED, now_ns=now)

        record.renewed_at_utc = now
        record.expires_at_utc = now + self._ttl_ns
        record.last_window_day = day
        record.last_error = ""
        return transition(record, TotpRenewalState.RENEWED, now_ns=now)

    def tick(self, now_ns: int | None = None) -> list[TotpRenewalRecord]:
        """Advance the whole machine one step and return the records it touched.

        Stale ``RENEWED`` records are moved to ``EXPIRED`` first, then every due
        key is renewed once.  A failed renewal is recorded on the record (never
        raised) so one broken venue cannot stop the others from renewing.
        """

        now = _resolve(now_ns)
        touched: list[TotpRenewalRecord] = []
        for record in self._records.values():
            if (
                record.state is TotpRenewalState.RENEWED
                and now >= int(record.expires_at_utc)
            ):
                touched.append(transition(record, TotpRenewalState.EXPIRED, now_ns=now))

        for key in self.due(now_ns=now):
            record = self._records[key]
            touched.append(self.renew(record.silo, record.venue, now_ns=now))
        return touched

    # -- telemetry --------------------------------------------------------
    def telemetry(self, now_ns: int | None = None) -> dict[str, Any]:
        """Return a JSON-safe pre-market renewal report for Ring 3 (``sec_016``)."""

        now = _resolve(now_ns)
        records = [record.to_dict(now_ns=now) for record in self._records.values()]
        live_count = sum(
            1 for record in self._records.values() if record.is_live(now_ns=now)
        )
        expired_count = sum(
            1
            for record in self._records.values()
            if record.state is TotpRenewalState.EXPIRED
            or (
                record.state is TotpRenewalState.RENEWED
                and now >= int(record.expires_at_utc)
            )
        )
        failed_count = sum(
            1
            for record in self._records.values()
            if record.state is TotpRenewalState.FAILED
        )
        return {
            "renew_at_utc_hour": self._renew_at_utc_hour,
            "ist_renew_at": _ist_label(self._renew_at_utc_hour),
            "window_open": self._window(now)[0],
            "lead_minutes": self._lead_minutes,
            "ttl_seconds": self._ttl_seconds,
            "records": records,
            "due": self.due(now_ns=now),
            "live_count": live_count,
            "expired_count": expired_count,
            "failed_count": failed_count,
        }
