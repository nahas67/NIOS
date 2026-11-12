"""Write-only LLM credential custody for the Ring 3 control plane.

``sec_011`` and ``sec_016`` require that API keys entered in the Hot-Swap
Control Room are **write-only**: Ring 0 stores the raw secret encrypted at
rest and every response (GET or POST) returns only a masked token such as
``***9A4F``.  A prompt-injected Ring 2 process, an SSE consumer or an operator
reading the DOM must never be able to recover the key.

This module is deliberately small and dependency-light: Fernet (AES-128-CBC +
HMAC-SHA256) from ``cryptography``, one JSON ciphertext file, and a key file
created with owner-only permissions.  It lives under
``ring0_vault/silos/key_vault`` because only Ring 0 may hold credentials
(``sec_004`` air-gap contract).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["LLMKeyVault", "mask_secret", "atomic_write_private", "MASK_PREFIX"]

#: Prefix used for every masked token returned to Ring 3.
MASK_PREFIX = "***"
#: Number of trailing characters preserved for operator identification.
MASK_VISIBLE_CHARS = 4


def atomic_write_private(
    path: str | Path, payload: bytes, mode: int = 0o600
) -> Path:
    """Write ``payload`` to ``path`` atomically with owner-only permissions.

    The temporary file lives in the same directory (same filesystem), is
    fsynced, then ``os.replace`` swaps it in.  A reader therefore sees either
    the complete old file or the complete new one, never a half-written key or
    credential store.  ``0o600`` is applied both at creation and after the
    rename so a pre-existing file with looser permissions is tightened too.
    """

    target = Path(path)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, int(mode))
    try:
        os.write(fd, bytes(payload))
        os.fsync(fd)
    finally:
        os.close(fd)
    if target.exists():
        # Windows refuses to replace a file carrying the read-only attribute
        # (0o400 archives).  Clear it first; the final chmod restores the mode.
        try:
            os.chmod(target, 0o600)
        except OSError:  # pragma: no cover - platform specific
            pass
    os.replace(tmp, target)
    try:
        os.chmod(target, int(mode))
    except OSError:  # pragma: no cover - Windows read-only semantics
        pass
    return target


def mask_secret(secret: str) -> str:
    """Return a stable write-only token: ``***`` + last four characters.

    Short secrets are fully masked so a 1-4 character key cannot be recovered.
    """
    token = str(secret or "")
    if len(token) <= MASK_VISIBLE_CHARS:
        return MASK_PREFIX
    return f"{MASK_PREFIX}{token[-MASK_VISIBLE_CHARS:]}"


class LLMKeyVault:
    """Encrypted, write-only credential store for System 1 / System 2 keys.

    Parameters
    ----------
    root:
        Directory holding ``fernet.key`` and ``llm_keys.json``.  Defaults to
        ``ring0_vault/silos/key_vault/.secrets`` (override with
        ``NIOS_KEY_VAULT_DIR``).
    """

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            root = os.environ.get("NIOS_KEY_VAULT_DIR") or (
                Path(__file__).resolve().parent / ".secrets"
            )
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._key_path = self.root / "fernet.key"
        self._store_path = self.root / "llm_keys.json"
        self._lock = threading.RLock()
        self._key = self._load_or_create_key()
        self._fernet = Fernet(self._key)
        if not self._store_path.exists():
            self._store_path.write_text("{}", encoding="utf-8")

    # -- key material -----------------------------------------------------
    def _load_or_create_key(self) -> bytes:
        if self._key_path.exists():
            return self._key_path.read_bytes().strip()
        key = Fernet.generate_key()
        fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key

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

    # -- public API -------------------------------------------------------
    def put(self, name: str, secret: str) -> dict[str, Any]:
        """Encrypt and persist ``secret``; return only masked metadata."""
        token = str(secret or "")
        if not token:
            raise ValueError("API key must be a non-empty string")
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
        """Return the masked token for ``name`` without decrypting it.

        The ciphertext does not reveal the last four characters, so masking
        requires a decrypt.  The plaintext never leaves this method.
        """
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

    # -- zero-downtime key rotation (Ring 0 only) -------------------------
    @property
    def key_material(self) -> bytes:
        """The current Fernet key bytes.  Ring 0 rotation only."""

        return bytes(self._key)

    @property
    def store_path(self) -> Path:
        return self._store_path

    @contextlib.contextmanager
    def rotation_guard(self) -> Iterator[None]:
        """Hold the vault write lock for the whole rotation window.

        A credential written by the cockpit while keys rotate would otherwise
        be encrypted under the old Fernet and lost.  The lock makes snapshot +
        re-encrypt + adopt one atomic critical section inside Ring 0.
        """

        with self._lock:
            yield

    def rotation_snapshot(self) -> dict[str, str]:
        """Decrypt every stored secret for a rotation.  Ring 0 only.

        Raises ``ValueError`` rather than silently dropping an entry: a rotation
        must never lose a credential.
        """

        with self._lock:
            data = self._read()
            secrets: dict[str, str] = {}
            for name, blob in data.items():
                try:
                    secrets[name] = self._fernet.decrypt(blob.encode("ascii")).decode(
                        "utf-8"
                    )
                except (InvalidToken, ValueError) as exc:
                    raise ValueError(
                        f"credential {name!r} cannot be decrypted; "
                        "refusing to rotate and lose it"
                    ) from exc
            return secrets

    def install_rotated_key(
        self, new_key: bytes, plaintext: Mapping[str, str]
    ) -> dict[str, Any]:
        """Write the new key and re-encrypted store atomically, then adopt it."""

        with self._lock:
            fernet = Fernet(bytes(new_key))
            payload = {
                str(name): fernet.encrypt(str(value).encode("utf-8")).decode("ascii")
                for name, value in plaintext.items()
            }
            atomic_write_private(self._key_path, bytes(new_key), 0o600)
            atomic_write_private(
                self._store_path,
                json.dumps(payload, sort_keys=True).encode("utf-8"),
                0o600,
            )
            self._key = bytes(new_key)
            self._fernet = fernet
            return {"store": str(self._store_path), "entries": len(payload)}

    def adopt_fernet_key(self, new_key: bytes) -> None:
        """Adopt key material already written to disk (crash recovery path)."""

        with self._lock:
            self._key = bytes(new_key)
            self._fernet = Fernet(self._key)

    def fernet_roundtrip(self, message: bytes) -> bool:
        """Prove the adopted Fernet envelope still decrypts to the same bytes."""

        with self._lock:
            blob = self._fernet.encrypt(bytes(message))
            return self._fernet.decrypt(blob) == bytes(message)

    # -- internal ---------------------------------------------------------
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
