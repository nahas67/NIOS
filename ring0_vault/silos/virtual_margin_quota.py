"""Ring 0 virtual margin quota -- headroom without moving a single micro-unit.

Why a *virtual* quota exists
----------------------------
The three regulated silos cannot be funded the way a crypto wallet can.  India
broking APIs (Zerodha / Dhan) prohibit transferring funds out of the trading
account, IBKR's US account is bound by its own cash-settlement rules, and the FX
MT5 account is margined by the broker rather than by an internal wallet sweep.
Calling ``/sapi/v1/asset/transfer`` is a Binance-only capability; there is no
legal equivalent on those venues.

So instead of moving money, this engine grants **virtual margin headroom**: a
signed, expiring claim that the silo may deploy up to ``amount_micros`` of its
own already-settled ``ReserveYield`` balance as margin, paired with a T+1
settlement schedule.  Nothing is transferred, nothing is withdrawn and no
balance changes -- ``status()`` reports ``no_funds_moved: True`` and the ledger
is only ever *read*.

Constitutional constraints
--------------------------
* ``sec_015`` -- zero cross-border wires.  ``cross_border_wire_bps`` stays ``0``
  because no money crosses a border: the headroom is drawn from the silo's own
  reserve and repaid inside the same silo.
* ``sec_018`` Law 4 -- the default answer is no trade.  A quota is headroom,
  not an order; the 98% conformal gate still has to pass before capital deploys.
* ``sec_018`` Law 5 -- compound inside the silo.  A quota can never be spent on
  another silo's trade, and it expires.
* ADR-009 -- a hot kill latch refuses every new quota; a halt means no new
  exposure of any kind, virtual or real.

Settlement model
----------------
``expires_at_utc`` is a UTC epoch **nanosecond** stamp (``issued_at_ns +
ttl_seconds * 1e9``) and is compared directly against ``time.time_ns()``.
``available_at_utc`` is a UTC epoch **second** boundary computed with
:func:`calendar.timegm` -- the next UTC midnight plus one day (T+1) -- because
that is the granularity at which a broker settles; :attr:`VirtualMarginQuota.
available_at_ns` renders the same instant in the ledger's nanosecond clock.
"""

from __future__ import annotations

import calendar
import hashlib
import time
from dataclasses import dataclass, replace
from typing import Any

from core.schemas import VaultName, canonical_pairs_digest
from ring0_vault.ibor.ledger_outbox import IBORLedger

__all__ = [
    "REGULATED_SILOS",
    "QUOTA_ACTIVE",
    "QUOTA_REVOKED",
    "QUOTA_EXPIRED",
    "TTL_MIN_SECONDS",
    "TTL_MAX_SECONDS",
    "QuotaRejected",
    "VirtualMarginQuota",
    "VirtualMarginQuotaEngine",
]

#: ``sec_015``: the silos whose venue APIs forbid fund transfers.
REGULATED_SILOS: tuple[str, ...] = ("INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5")

QUOTA_ACTIVE = "ACTIVE"
QUOTA_REVOKED = "REVOKED"
QUOTA_EXPIRED = "EXPIRED"

#: A quota shorter than a minute is unusable; longer than a day is a policy
#: hole, so the requested TTL is clamped into this window.
TTL_MIN_SECONDS = 60
TTL_MAX_SECONDS = 86_400

_SECONDS_PER_DAY = 86_400
_NS_PER_SECOND = 1_000_000_000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QuotaRejected(Exception):
    """A virtual quota was refused.

    ``code`` is a stable machine-readable token.  Known codes:
    ``KILL_LATCH_HOT``, ``SILO_NOT_REGULATED``, ``QUOTA_EXCEEDS_RESERVE``,
    ``QUOTA_INVALID``, ``QUOTA_NOT_FOUND``.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class VirtualMarginQuota:
    """One signed, expiring grant of virtual margin headroom.

    The object is the *claim*, not a balance: it never appears in the ledger,
    so deleting every quota would leave the book byte-identical.
    """

    quota_id: str
    silo: str
    amount_micros: int
    issued_at_ns: int
    expires_at_utc: int
    available_at_utc: int
    token: str
    source_vault: str = "ReserveYield"
    mission_id: str = ""
    tranche_id: str = ""
    status: str = QUOTA_ACTIVE

    def is_live(self, now_ns: int | None = None) -> bool:
        """True while the quota is ``ACTIVE`` and has not passed its expiry."""
        if str(self.status) != QUOTA_ACTIVE:
            return False
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        return stamp < int(self.expires_at_utc)

    @property
    def available_at_ns(self) -> int:
        """The T+1 settlement boundary on the ledger's nanosecond clock."""
        return int(self.available_at_utc) * _NS_PER_SECOND

    def to_dict(self) -> dict[str, Any]:
        return {
            "quota_id": str(self.quota_id),
            "silo": str(self.silo),
            "amount_micros": int(self.amount_micros),
            "issued_at_ns": int(self.issued_at_ns),
            "expires_at_utc": int(self.expires_at_utc),
            "available_at_utc": int(self.available_at_utc),
            "available_at_ns": int(self.available_at_ns),
            "token": str(self.token),
            "source_vault": str(self.source_vault),
            "mission_id": str(self.mission_id),
            "tranche_id": str(self.tranche_id),
            "status": str(self.status),
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _next_utc_midnight_plus_one_day(now_ns: int) -> int:
    """T+1 settlement boundary as a UTC epoch **second** (``calendar.timegm``).

    "Next UTC midnight + 1 day" is the first instant a broker is guaranteed to
    have settled a trade placed today, expressed in UTC so every silo, log and
    partition agrees (``sec_010`` UTC-everywhere rule).
    """
    now_s = int(now_ns) // _NS_PER_SECOND
    tm = time.gmtime(now_s)
    midnight = calendar.timegm(
        (tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, 0)
    )
    return int(midnight) + 2 * _SECONDS_PER_DAY


def _latch_is_halted(latch: Any) -> bool:
    """Read a kill latch that may expose a property or a callable.

    An unreadable latch is treated as **hot**: an unknown kill-switch state must
    never be assumed safe (``sec_006`` / ADR-009).
    """
    if latch is None:
        return False
    flag = getattr(latch, "is_halted", None)
    if callable(flag):
        try:
            flag = flag()
        except Exception:  # noqa: BLE001 - unreadable latch fails closed
            return True
    return bool(flag)


class VirtualMarginQuotaEngine:
    """Grants, verifies and revokes virtual margin headroom (``sec_015``).

    The engine only ever *reads* the ledger: ``issue`` checks the silo's
    ``ReserveYield`` balance and NAV, then mints a signed claim.  No pending
    transfer, no posting and no void is ever created here, which is exactly why
    ``status()`` can assert ``no_funds_moved: True``.
    """

    def __init__(
        self,
        ledger: IBORLedger,
        *,
        signer: Any = None,
        kill_latch: Any = None,
        ttl_seconds: int = 900,
        max_quota_pct: float = 0.25,
        reservation_ttl_seconds: int = 3600,
    ) -> None:
        self.ledger = ledger
        self.signer = signer
        self.kill_latch = kill_latch
        self.ttl_seconds = self._clamp_ttl(ttl_seconds)
        pct = float(max_quota_pct)
        if not 0.0 < pct <= 1.0:
            raise ValueError("max_quota_pct must be in (0.0, 1.0]")
        self.max_quota_pct = pct
        self.reservation_ttl_seconds = int(reservation_ttl_seconds)
        if self.reservation_ttl_seconds <= 0:
            raise ValueError("reservation_ttl_seconds must be positive")
        self._quotas: dict[str, VirtualMarginQuota] = {}
        self._by_token: dict[str, str] = {}
        self._reasons: dict[str, str] = {}
        self._nonce = 0

    # -- issuing ----------------------------------------------------------
    def issue(
        self,
        silo: str,
        amount_micros: int,
        *,
        now_ns: int | None = None,
        mission_id: str = "",
        tranche_id: str = "",
        ttl_seconds: int | None = None,
    ) -> VirtualMarginQuota:
        """Mint a virtual quota; the ledger is read, never written.

        The bound is the tighter of the silo's own ``ReserveYield`` balance and
        ``max_quota_pct`` of its NAV.  A quota larger than that would be headroom
        the silo could not honour if it were called.
        """
        # ADR-009 first: a halt outranks every other consideration.
        if _latch_is_halted(self.kill_latch):
            raise QuotaRejected("KILL_LATCH_HOT", str(silo))

        target = str(silo)
        if target not in REGULATED_SILOS:
            raise QuotaRejected("SILO_NOT_REGULATED", target)

        amount = self._valid_amount(amount_micros)
        ttl = self.ttl_seconds if ttl_seconds is None else self._clamp_ttl(ttl_seconds)

        reserve = int(self.ledger.balance(target, str(VaultName.RESERVE_YIELD)))
        nav = int(self.ledger.silo_nav_micros(target))
        headroom = min(reserve, int(self.max_quota_pct * nav))
        if amount <= 0 or amount > headroom:
            raise QuotaRejected(
                "QUOTA_EXCEEDS_RESERVE",
                f"amount={amount} headroom={headroom} (reserve={reserve} nav={nav})",
            )

        stamp = int(now_ns if now_ns is not None else time.time_ns())
        self._nonce += 1
        quota_id = hashlib.sha256(
            f"{target}:{amount}:{stamp}:{self._nonce}".encode("ascii")
        ).hexdigest()[:24]
        expires_at = stamp + ttl * _NS_PER_SECOND
        available_at = _next_utc_midnight_plus_one_day(stamp)
        payload = {
            "quota_id": quota_id,
            "silo": target,
            "amount_micros": amount,
            "issued_at_ns": stamp,
            "expires_at_utc": expires_at,
            "available_at_utc": available_at,
            "source_vault": str(VaultName.RESERVE_YIELD),
            "mission_id": str(mission_id),
            "tranche_id": str(tranche_id),
            "nonce": int(self._nonce),
        }
        token = self._tokenise(payload)

        quota = VirtualMarginQuota(
            quota_id=quota_id,
            silo=target,
            amount_micros=amount,
            issued_at_ns=stamp,
            expires_at_utc=expires_at,
            available_at_utc=available_at,
            token=token,
            source_vault=str(VaultName.RESERVE_YIELD),
            mission_id=str(mission_id),
            tranche_id=str(tranche_id),
        )
        self._quotas[quota.quota_id] = quota
        self._by_token[quota.token] = quota.quota_id
        return quota

    # -- revocation -------------------------------------------------------
    def revoke(self, quota_id: str, *, reason: str = "OPERATOR") -> bool:
        """Revoke one live quota.  ``False`` when it is unknown or already dead."""
        key = str(quota_id)
        quota = self._quotas.get(key)
        if quota is None or quota.status != QUOTA_ACTIVE:
            return False
        self._quotas[key] = replace(quota, status=QUOTA_REVOKED)
        self._reasons[key] = str(reason)
        return True

    def revoke_all(self, *, reason: str = "KILL_LATCH") -> int:
        """Revoke every live quota; idempotent, returns the number revoked."""
        revoked = 0
        for quota in tuple(self._quotas.values()):
            if self.revoke(quota.quota_id, reason=reason):
                revoked += 1
        return revoked

    # -- verification -----------------------------------------------------
    def verify(
        self, token: str, *, now_ns: int | None = None
    ) -> VirtualMarginQuota | None:
        """Return the live quota for ``token``, else ``None``.

        An expired quota is flipped to ``EXPIRED`` lazily on first sight, so the
        stored status converges without a background sweeper (no thread, no
        timer, no sleep).
        """
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        quota_id = self._by_token.get(str(token))
        if quota_id is None:
            return None
        quota = self._expire_if_stale(quota_id, stamp)
        return quota if quota.is_live(stamp) else None

    def require(self, quota_id: str, *, now_ns: int | None = None) -> VirtualMarginQuota:
        """Strict lookup used by Ring 0 callers that must fail loudly."""
        key = str(quota_id)
        quota = self._quotas.get(key)
        if quota is None:
            raise QuotaRejected("QUOTA_NOT_FOUND", key)
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        quota = self._expire_if_stale(key, stamp)
        if not quota.is_live(stamp):
            raise QuotaRejected("QUOTA_NOT_FOUND", f"{key} is {quota.status}")
        return quota

    # -- reporting --------------------------------------------------------
    def active_micros(self, silo: str, *, now_ns: int | None = None) -> int:
        """Sum of live virtual headroom granted to ``silo``."""
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        total = 0
        for quota in tuple(self._quotas.values()):
            if quota.silo != str(silo):
                continue
            if self._expire_if_stale(quota.quota_id, stamp).is_live(stamp):
                total += int(quota.amount_micros)
        return total

    def status(self, *, now_ns: int | None = None) -> dict[str, Any]:
        """Cockpit snapshot; ``no_funds_moved`` is the constitutional claim."""
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        for quota in tuple(self._quotas.values()):
            self._expire_if_stale(quota.quota_id, stamp)
        ordered = sorted(
            self._quotas.values(), key=lambda q: (int(q.issued_at_ns), q.quota_id)
        )
        active = [q for q in ordered if q.is_live(stamp)]
        return {
            "regulated_silos": list(REGULATED_SILOS),
            "active_count": len(active),
            "active_micros_by_silo": {
                silo: sum(
                    int(q.amount_micros) for q in active if q.silo == silo
                )
                for silo in REGULATED_SILOS
            },
            "quotas": [q.to_dict() for q in ordered],
            "no_funds_moved": True,
        }

    # -- internals --------------------------------------------------------
    @staticmethod
    def _clamp_ttl(ttl_seconds: Any) -> int:
        """Clamp a requested TTL into ``[TTL_MIN_SECONDS, TTL_MAX_SECONDS]``."""
        try:
            value = int(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise QuotaRejected("QUOTA_INVALID", f"ttl_seconds={ttl_seconds!r}") from exc
        return max(TTL_MIN_SECONDS, min(TTL_MAX_SECONDS, value))

    @staticmethod
    def _valid_amount(amount_micros: Any) -> int:
        if isinstance(amount_micros, bool) or not isinstance(amount_micros, int):
            raise QuotaRejected("QUOTA_INVALID", repr(amount_micros))
        return int(amount_micros)

    def _tokenise(self, payload: dict[str, Any]) -> str:
        """Sign the quota payload, or hash it when no signer is injected.

        The payload carries the monotonic ``nonce``, so the token is unique per
        quota even when two quotas are issued in the same nanosecond.
        """
        if self.signer is not None:
            _digest, signature, _sealed = self.signer.sign_payload(dict(payload))
            return str(signature)
        return canonical_pairs_digest(payload.items())

    def _expire_if_stale(
        self, quota_id: str, now_ns: int
    ) -> VirtualMarginQuota:
        """Lazily flip an ``ACTIVE`` quota past its expiry to ``EXPIRED``."""
        quota = self._quotas[quota_id]
        if quota.status == QUOTA_ACTIVE and int(now_ns) >= int(quota.expires_at_utc):
            quota = replace(quota, status=QUOTA_EXPIRED)
            self._quotas[quota_id] = quota
        return quota
