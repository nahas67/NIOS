"""Write-only broker testnet / sandbox credential custody (``sec_011``, ``sec_016``).

Only Ring 0 may hold broker credentials (``sec_004`` air-gap contract).  A
prompt-injected Ring 2 process, an SSE consumer or an operator reading the DOM
must never be able to recover an API secret, so this store is **write-only**:

``put``   encrypts with Fernet (AES-128-CBC + HMAC-SHA256) and returns only a
          ``***`` token carrying the last four characters;
``masked`` returns that token without ever handing the plaintext back;
``sign``  produces a deterministic Ed25519 signature over a caller-supplied
          digest, which is how the dry-run order preflight proves the payload
          was assembled inside Ring 0.

Everything lives under ``ring0_vault/silos/key_vault`` because that directory is
the only one the constitution authorises to touch key material.  The Fernet key
and the Ed25519 seed are created once with owner-only permissions and never
leave the process.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ring0_vault.silos.key_vault.llm_credentials import mask_secret

__all__ = [
    "BrokerCredentialVault",
    "BROKER_VAULT_ENV",
    "default_broker_vault",
    "mask_secret",
]

#: Override the vault root (tests point this at ``tmp_path``).
BROKER_VAULT_ENV = "NIOS_BROKER_VAULT_DIR"


def _default_root() -> Path:
    override = os.environ.get(BROKER_VAULT_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / ".broker_secrets"


class BrokerCredentialVault:
    """Encrypted, write-only, per-venue credential store.

    Parameters
    ----------
    root:
        Directory holding ``broker_fernet.key``, ``broker_signer.key`` and
        ``broker_credentials.json``.  Defaults to
        ``ring0_vault/silos/key_vault/.broker_secrets``.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else _default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._fernet_key_path = self.root / "broker_fernet.key"
        self._signer_key_path = self.root / "broker_signer.key"
        self._store_path = self.root / "broker_credentials.json"
        self._lock = threading.RLock()
        self._fernet = Fernet(self._load_or_create_fernet_key())
        self._signer = self._load_or_create_signer()
        if not self._store_path.exists():
            self._store_path.write_text("{}", encoding="utf-8")

    # -- key material ------------------------------------------------------
    def _load_or_create_fernet_key(self) -> bytes:
        if self._fernet_key_path.exists():
            return self._fernet_key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self._write_private(self._fernet_key_path, key)
        return key

    def _load_or_create_signer(self) -> Ed25519PrivateKey:
        if self._signer_key_path.exists():
            seed = self._signer_key_path.read_bytes().strip()
            try:
                return Ed25519PrivateKey.from_private_bytes(seed)
            except ValueError:  # pragma: no cover - corrupt key file
                pass
        signer = Ed25519PrivateKey.generate()
        self._write_private(self._signer_key_path, signer.private_bytes_raw())
        return signer

    @staticmethod
    def _write_private(path: Path, payload: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)

    # -- persistence -------------------------------------------------------
    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _write(self, data: dict[str, str]) -> None:
        tmp = self._store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._store_path)

    # -- public API --------------------------------------------------------
    def put(self, name: str, secret: str) -> dict[str, Any]:
        """Encrypt and persist ``secret``; return only masked metadata."""

        token = str(secret or "")
        if not token:
            raise ValueError("credential must be a non-empty string")
        with self._lock:
            data = self._read()
            data[str(name)] = self._fernet.encrypt(token.encode("utf-8")).decode("ascii")
            self._write(data)
        return {
            "name": str(name),
            "masked": mask_secret(token),
            "stored": True,
            "updated_at_ns": time.time_ns(),
        }

    def masked(self, name: str) -> str | None:
        secret = self._decrypt(name)
        return mask_secret(secret) if secret is not None else None

    def has(self, name: str) -> bool:
        return str(name) in self._read()

    def delete(self, name: str) -> bool:
        with self._lock:
            data = self._read()
            if str(name) not in data:
                return False
            del data[str(name)]
            self._write(data)
        return True

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._read()))

    # -- signing -----------------------------------------------------------
    @property
    def public_key_hex(self) -> str:
        return self._signer.public_key().public_bytes_raw().hex()

    def sign(self, message: bytes) -> bytes:
        """Deterministic Ed25519 signature over ``message``."""

        return self._signer.sign(bytes(message))

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(
                self._signer.public_key().public_bytes_raw()
            ).verify(bytes(signature), bytes(message))
        except Exception:  # noqa: BLE001 - any failure is simply "not verified"
            return False
        return True

    def fernet_roundtrip(self, message: bytes) -> bool:
        """Prove the Fernet envelope still decrypts to the same bytes."""

        blob = self._fernet.encrypt(bytes(message))
        return self._fernet.decrypt(blob) == bytes(message)

    # -- internal ----------------------------------------------------------
    def _decrypt(self, name: str) -> str | None:
        with self._lock:
            blob = self._read().get(str(name))
        if blob is None:
            return None
        try:
            return self._fernet.decrypt(blob.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return None

    def _raw_for_test_only(self, name: str) -> str | None:
        """Explicitly named test hook; never call from Ring 3 or Ring 2."""

        return self._decrypt(name)


_DEFAULT_VAULT: BrokerCredentialVault | None = None
_DEFAULT_LOCK = threading.Lock()


def default_broker_vault() -> BrokerCredentialVault:
    """Process-wide vault rooted at the constitutional key-vault directory."""

    global _DEFAULT_VAULT
    with _DEFAULT_LOCK:
        if _DEFAULT_VAULT is None:
            _DEFAULT_VAULT = BrokerCredentialVault()
        return _DEFAULT_VAULT


def digest_payload(payload: Any) -> bytes:
    """Stable SHA-256 digest bytes for a venue-native payload."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).digest()
