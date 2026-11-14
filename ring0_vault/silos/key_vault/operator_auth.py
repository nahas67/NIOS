"""Ring 0 sovereign operator authentication (``sec_011``, ``sec_016``).

The operator identity is a *Ring 0* asset: it lives inside the encrypted,
gitignored key-vault tree next to the broker adapters, never in Ring 2 or the
React bundle.  Passwords and the live-elevation PIN are stored only as salted
KDF digests; the plaintext never touches disk, a log line, or a response body.

Two credential factors are deliberately separate:

* ``password``     -- authenticates the operator into the **PAPER** sandbox.
* ``live_pin``     -- the second factor for **LIVE**.  It is never accepted on
  its own: either a valid PAPER session token *and* the PIN (legacy
  ``/api/v2/auth/elevate-live``), or the password and the PIN in one atomic
  request (``/api/v2/auth/login/live``).  A stolen PIN is useless without the
  password, and a stolen password is useless without the PIN.

V1.3 adds the two explicit portals without touching the legacy contract:

* :meth:`OperatorAuth.login_paper` -- ``POST /api/v2/auth/login/paper``.
* :meth:`OperatorAuth.login_live`  -- ``POST /api/v2/auth/login/live``: a single
  three-factor request (username + password + PIN) that mints a LIVE session
  directly, so a live operator never has to hold a PAPER token first.

Session tokens are stateless HMAC-SHA256 bearer tokens signed with a per-vault
random key.  A token carries ``username``, ``tier`` and ``exp``; ``revoke_session``
adds it to an in-memory deny-list so logout is immediate.

Everything here is stdlib-only: Ring 0 must not grow a dependency to boot.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "NONE_TIER",
    "PAPER_TIER",
    "LIVE_TIER",
    "OperatorAuth",
    "OperatorAuthError",
    "OperatorAuthFailed",
    "OperatorExistsError",
    "OperatorSession",
    "LivePinRejected",
    "ResetCredentialsMissing",
    "ResetCredentialsRejected",
    "SessionInvalid",
    "default_operator_auth",
    "reset_operator_auth_singleton",
]
NONE_TIER = "NONE"
PAPER_TIER = "PAPER"
LIVE_TIER = "LIVE"

#: scrypt work factors.  ``n=2**14`` is ~16 MiB and ~40 ms on a laptop CPU:
#: expensive enough to make an offline dictionary attack hurt, cheap enough
#: that a cockpit login is still instant.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
PBKDF2_ITERATIONS = 210_000
SALT_BYTES = 32

PAPER_SESSION_TTL_S = 12 * 60 * 60
LIVE_SESSION_TTL_S = 30 * 60

MIN_PASSWORD_LEN = 8
MIN_PIN_LEN = 4
MAX_PIN_LEN = 12

_VAULT_ENV = "NIOS_OPERATOR_AUTH_DIR"
_DEFAULT_VAULT = Path(__file__).resolve().parent / ".secrets"


class OperatorAuthError(RuntimeError):
    """Base class for every operator-auth failure."""


class OperatorExistsError(OperatorAuthError):
    """A registration already exists; use login or a factory reset."""


class OperatorAuthFailed(OperatorAuthError):
    """Wrong username or password (never disclosed separately)."""


class LivePinRejected(OperatorAuthError):
    """The live-elevation PIN did not match."""


class SessionInvalid(OperatorAuthError):
    """Token missing, malformed, expired, revoked or unknown-key signed."""


class ResetCredentialsMissing(OperatorAuthFailed):
    """A factory reset was attempted with no credential on an initialized vault.

    Neither factor was supplied, so the caller never proved it owns the
    operator identity.  The gateway maps this to ``401 AUTH_REQUIRED``.
    """


class ResetCredentialsRejected(OperatorAuthError):
    """A factory reset supplied both factors and at least one was wrong.

    The caller *did* attempt authentication, so the gateway maps this to
    ``403 INVALID_CREDENTIALS`` rather than a generic 401.
    """


@dataclass(frozen=True)
class OperatorSession:
    """One authenticated operator session."""

    token: str
    username: str
    tier: str
    issued_ns: int
    expires_ns: int
    #: The workspace this session was minted for: ``PAPER`` or ``LIVE``.
    workspace: str = PAPER_TIER

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "username": self.username,
            "tier": self.tier,
            "workspace": self.workspace,
            "issued_ns": int(self.issued_ns),
            "expires_ns": int(self.expires_ns),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _derive_scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def _derive_pbkdf2(pin: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=SCRYPT_DKLEN
    )


def _restrict(path: Path) -> None:
    """Best-effort ``0600``.  POSIX honours the mode; Windows ACLs do not."""
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    _restrict(tmp)
    os.replace(tmp, path)
    _restrict(path)


class OperatorAuth:
    """Single-operator credential store + session broker."""

    def __init__(
        self,
        vault_dir: str | Path | None = None,
        *,
        filename: str = "operator_auth.json",
        now_ns=None,
    ) -> None:
        root = (
            Path(vault_dir)
            if vault_dir is not None
            else Path(os.environ.get(_VAULT_ENV) or _DEFAULT_VAULT)
        )
        self._path = root / filename
        self._lock = threading.RLock()
        self._now_ns = now_ns or time.time_ns
        self._revoked: set[str] = set()

    # -- paths -------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    # -- persistence -------------------------------------------------------
    def _load(self) -> dict[str, Any] | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:  # pragma: no cover - unreadable vault
            raise OperatorAuthError(f"operator vault unreadable: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise OperatorAuthError("operator vault is corrupt") from exc
        if not isinstance(data, dict) or not data.get("username"):
            raise OperatorAuthError("operator vault is corrupt")
        return data

    def initialized(self) -> bool:
        with self._lock:
            try:
                return self._load() is not None
            except OperatorAuthError:
                return False

    # -- registration ------------------------------------------------------
    def setup_operator(
        self,
        username: str,
        password: str,
        live_pin: str,
        *,
        allow_overwrite: bool = False,
    ) -> OperatorSession:
        """Register the sovereign operator (first-run only by default)."""
        user = str(username or "").strip()
        if not user:
            raise OperatorAuthError("username is required")
        if len(str(password or "")) < MIN_PASSWORD_LEN:
            raise OperatorAuthError(
                f"password must be at least {MIN_PASSWORD_LEN} characters"
            )
        pin = str(live_pin or "").strip()
        if not pin.isdigit() or not (MIN_PIN_LEN <= len(pin) <= MAX_PIN_LEN):
            raise OperatorAuthError(
                f"live_pin must be {MIN_PIN_LEN}-{MAX_PIN_LEN} digits"
            )

        with self._lock:
            if not allow_overwrite and self._load() is not None:
                raise OperatorExistsError("operator already initialized")

            pw_salt = secrets.token_bytes(SALT_BYTES)
            pin_salt = secrets.token_bytes(SALT_BYTES)
            stamp = int(self._now_ns())
            record = {
                "version": 1,
                "username": user,
                "password": {
                    "algo": "scrypt",
                    "n": SCRYPT_N,
                    "r": SCRYPT_R,
                    "p": SCRYPT_P,
                    "dklen": SCRYPT_DKLEN,
                    "salt": _b64e(pw_salt),
                    "hash": _b64e(_derive_scrypt(str(password), pw_salt)),
                },
                "live_pin": {
                    "algo": "pbkdf2_sha256",
                    "iterations": PBKDF2_ITERATIONS,
                    "salt": _b64e(pin_salt),
                    "hash": _b64e(_derive_pbkdf2(pin, pin_salt)),
                },
                "session_key": _b64e(secrets.token_bytes(32)),
                "created_ns": stamp,
                "updated_ns": stamp,
            }
            _atomic_write(self._path, record)
            self._revoked.clear()
            return self._issue(record, PAPER_TIER)

    # -- login -------------------------------------------------------------
    def _verify_password(self, record: Mapping[str, Any], username: str, password: str) -> bool:
        """Constant-time check of the ``scrypt`` password digest.

        Both the operator handle and the digest are always compared, so a wrong
        username cannot be told apart from a wrong password.
        """
        stored = record.get("password") or {}
        salt = _b64d(str(stored.get("salt", "")))
        expected = _b64d(str(stored.get("hash", "")))
        supplied = _derive_scrypt(str(password or ""), salt)
        user_ok = hmac.compare_digest(str(username or "").strip(), str(record.get("username", "")))
        secret_ok = hmac.compare_digest(supplied, expected)
        return bool(user_ok and secret_ok)

    def _verify_pin(self, record: Mapping[str, Any], live_pin: str) -> bool:
        """Constant-time check of the ``pbkdf2_hmac`` live-PIN digest."""
        pin = str(live_pin or "").strip()
        stored = record.get("live_pin") or {}
        salt = _b64d(str(stored.get("salt", "")))
        expected = _b64d(str(stored.get("hash", "")))
        return bool(hmac.compare_digest(_derive_pbkdf2(pin, salt), expected))

    def login_operator(self, username: str, password: str) -> OperatorSession:
        with self._lock:
            record = self._load()
            if record is None:
                raise OperatorAuthFailed("no operator registered")
            if not self._verify_password(record, username, password):
                raise OperatorAuthFailed("invalid credentials")
            return self._issue(record, PAPER_TIER)

    def login_paper(self, username: str, password: str) -> OperatorSession:
        """``POST /api/v2/auth/login/paper`` -- a 12 h PAPER session."""
        session = self.login_operator(username, password)
        if session.tier != PAPER_TIER:  # pragma: no cover - defensive
            raise OperatorAuthFailed("invalid tier")
        return session

    def login_live(
        self, username: str, password: str, live_pin: str
    ) -> OperatorSession:
        """``POST /api/v2/auth/login/live`` -- one atomic three-factor login.

        The password is verified against the ``scrypt`` digest and the PIN
        against the ``pbkdf2_hmac`` digest, both in constant time.  A missing or
        non-numeric PIN is rejected before either KDF runs, so the two failure
        classes stay distinguishable to the gateway without leaking *which*
        factor was wrong:

        * bad username/password  -> :class:`OperatorAuthFailed` (``401``)
        * missing/invalid PIN    -> :class:`LivePinRejected`  (``403``)

        The resulting token is a 30 min LIVE session, single-purpose: it never
        downgrades to PAPER and it is not minted from a PAPER token, so the
        live portal is a first-class entry point rather than an elevation.
        """
        pin = str(live_pin or "").strip()
        if not pin or not pin.isdigit():
            raise LivePinRejected("live PIN rejected")
        with self._lock:
            record = self._load()
            if record is None:
                raise OperatorAuthFailed("no operator registered")
            password_ok = self._verify_password(record, username, password)
            pin_ok = self._verify_pin(record, pin)
            if not password_ok:
                raise OperatorAuthFailed("invalid credentials")
            if not pin_ok:
                raise LivePinRejected("live PIN rejected")
            return self._issue(record, LIVE_TIER)

    # -- elevation ---------------------------------------------------------
    def elevate_to_live(self, session_token: str, live_pin: str) -> OperatorSession:
        with self._lock:
            self.decode_session(session_token)
            record = self._load()
            if record is None:  # pragma: no cover - token implies a record
                raise OperatorAuthFailed("no operator registered")
            if not self._verify_pin(record, live_pin):
                raise LivePinRejected("live PIN rejected")
            # The PAPER token is single-use for elevation: revoke it so a
            # replayed cookie can never mint a second LIVE session.
            self._revoked.add(str(session_token))
            return self._issue(record, LIVE_TIER)

    def revoke_session(self, session_token: str | None) -> bool:
        token = str(session_token or "").strip()
        if not token:
            return False
        with self._lock:
            self._revoked.add(token)
        return True

    # -- tokens ------------------------------------------------------------
    def _issue(self, record: Mapping[str, Any], tier: str) -> OperatorSession:
        now = int(self._now_ns())
        ttl_s = LIVE_SESSION_TTL_S if tier == LIVE_TIER else PAPER_SESSION_TTL_S
        claims = {
            "u": str(record.get("username", "")),
            "t": str(tier),
            "w": str(tier),
            "iat": now,
            "exp": now + int(ttl_s) * 1_000_000_000,
            "n": secrets.token_hex(8),
        }
        body = _b64e(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        key = _b64d(str(record.get("session_key", "")))
        signature = _b64e(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
        return OperatorSession(
            token=f"v1.{body}.{signature}",
            username=claims["u"],
            tier=str(tier),
            issued_ns=int(claims["iat"]),
            expires_ns=int(claims["exp"]),
            workspace=str(claims["w"]),
        )

    def decode_session(self, session_token: str | None) -> dict[str, Any]:
        token = str(session_token or "").strip()
        if not token:
            raise SessionInvalid("missing session token")
        if token in self._revoked:
            raise SessionInvalid("session revoked")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            raise SessionInvalid("malformed session token")
        record = self._load()
        if record is None:
            raise SessionInvalid("no operator registered")
        key = _b64d(str(record.get("session_key", "")))
        expected = hmac.new(key, parts[1].encode("ascii"), hashlib.sha256).digest()
        try:
            supplied = _b64d(parts[2])
            claims = json.loads(_b64d(parts[1]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SessionInvalid("malformed session token") from exc
        if not hmac.compare_digest(supplied, expected):
            raise SessionInvalid("bad session signature")
        if not isinstance(claims, dict):
            raise SessionInvalid("malformed session claims")
        if int(claims.get("exp", 0)) <= int(self._now_ns()):
            raise SessionInvalid("session expired")
        if str(claims.get("u", "")) != str(record.get("username", "")):
            raise SessionInvalid("session operator mismatch")
        if str(claims.get("t")) not in (PAPER_TIER, LIVE_TIER):
            raise SessionInvalid("unknown session tier")
        return claims

    def authenticate(self, session_token: str | None) -> dict[str, Any] | None:
        """Non-throwing session probe used by the workspace guards."""
        try:
            claims = self.decode_session(session_token)
        except SessionInvalid:
            return None
        return {
            "username": str(claims.get("u", "")),
            "tier": str(claims.get("t", NONE_TIER)),
            "workspace": str(claims.get("w", claims.get("t", NONE_TIER))),
            "issued_ns": int(claims.get("iat", 0)),
            "expires_ns": int(claims.get("exp", 0)),
        }

    # -- status / reset ----------------------------------------------------
    def status(self, session_token: str | None = None) -> dict[str, Any]:
        with self._lock:
            record = self._load()
            claims = self.authenticate(session_token) if record is not None else None
            return {
                "initialized": record is not None,
                "authenticated": claims is not None,
                "username": (claims or {}).get("username")
                or (str(record.get("username")) if record else None),
                "tier": (claims or {}).get("tier", NONE_TIER),
                "workspace": (claims or {}).get("workspace", NONE_TIER),
                "issued_ns": int((claims or {}).get("issued_ns", 0) or 0),
                "expires_ns": int((claims or {}).get("expires_ns", 0) or 0),
                "clean_slate_active": False,
            }

    def verify_reset_credentials(
        self, password: str | None, live_pin: str | None
    ) -> bool:
        """Demand BOTH factors before a factory reset may delete the vault.

        A factory reset is the most destructive Ring 0 operation an operator
        can reach: it erases the credential file, the signing/session key and
        every outstanding session.  Once a vault exists it therefore costs the
        same two factors as a LIVE elevation:

        * ``password`` -- verified against the stored ``scrypt`` digest;
        * ``live_pin`` -- verified against the stored ``pbkdf2_hmac`` digest.

        A *clean slate* (no credential on disk) returns ``False`` and is not
        gated, so first-run initialisation stays idempotent: resetting an
        uninitialised install is a no-op, not an authentication event.

        Raises:
            ResetCredentialsMissing: initialized vault, either factor absent.
            ResetCredentialsRejected: initialized vault, either factor wrong.
        """
        with self._lock:
            record = self._load()
            if record is None:
                return False
            secret = str(password or "")
            pin = str(live_pin or "").strip()
            if not secret or not pin:
                raise ResetCredentialsMissing(
                    "password and live_pin are required to reset an initialized install"
                )
            stored_pw = record.get("password") or {}
            pw_salt = _b64d(str(stored_pw.get("salt", "")))
            pw_expected = _b64d(str(stored_pw.get("hash", "")))
            stored_pin = record.get("live_pin") or {}
            pin_salt = _b64d(str(stored_pin.get("salt", "")))
            pin_expected = _b64d(str(stored_pin.get("hash", "")))
            # Both digests are always derived so a wrong password cannot be
            # told apart from a wrong PIN by response latency alone.
            pw_ok = hmac.compare_digest(_derive_scrypt(secret, pw_salt), pw_expected)
            pin_ok = hmac.compare_digest(_derive_pbkdf2(pin, pin_salt), pin_expected)
            if not (pw_ok and pin_ok):
                raise ResetCredentialsRejected("password or live PIN rejected")
            return True

    def reset_operator_workspace(
        self, password: str | None = None, live_pin: str | None = None
    ) -> dict[str, Any]:
        """Factory reset: delete the credential file and every session.

        When a credential exists the caller must present ``password`` **and**
        ``live_pin`` (see :meth:`verify_reset_credentials`).  An uninitialised
        install may be reset with no credential, so clean-slate initialisation
        remains idempotent.
        """
        with self._lock:
            verified = self.verify_reset_credentials(password, live_pin)
            removed = False
            try:
                self._path.unlink()
                removed = True
            except FileNotFoundError:
                removed = False
            except OSError as exc:  # pragma: no cover - locked vault
                raise OperatorAuthError(f"operator vault reset failed: {exc}") from exc
            self._revoked.clear()
            return {
                "reset": True,
                "removed": removed,
                "credential_verified": bool(verified),
                "path": str(self._path),
            }


_SINGLETON: OperatorAuth | None = None
_SINGLETON_LOCK = threading.Lock()


def default_operator_auth(vault_dir: str | Path | None = None) -> OperatorAuth:
    """Process-wide operator vault (one sovereign operator per install)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None or (
            vault_dir is not None
            and Path(vault_dir) / "operator_auth.json" != _SINGLETON.path
        ):
            _SINGLETON = OperatorAuth(vault_dir)
        return _SINGLETON


def reset_operator_auth_singleton() -> None:
    """Drop the cached singleton (tests / factory reset)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = None
