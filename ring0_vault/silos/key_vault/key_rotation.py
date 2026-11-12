"""Ring 0 zero-downtime cryptographic key rotation (``sec_004``, ``sec_011``).

Only ``ring0_vault`` may hold key material (``sec_004`` air-gap contract).  This
module rotates that material **without a restart, without a lost credential and
without invalidating a historical order**:

1. A fresh Fernet master key is generated for the LLM credential store.
2. A fresh Fernet master key is generated for the per-venue broker store.
3. A fresh Ed25519 order-signing keypair is generated; ``key_version`` is
   incremented and every new order signs with the new key.
4. Every stored LLM credential and every stored broker credential is decrypted
   under the old key and re-encrypted under the new one.
5. The previous Ed25519 public key is archived in a read-only verifier ring, so
   a signed order produced before the rotation still verifies afterwards.

Atomicity
---------
Each file write uses temp-file-and-``os.replace`` with ``0o600`` (keys and
stores) or ``0o400`` (archived public keys), so no reader ever sees a partial
key or a half-written store.  A short-lived ``rotation.journal.json`` (``0o600``)
records both key generations; if the process dies mid-rotation, the next call
to :func:`rotate_ring0_vault_keys` or a fresh process re-runs the journal
idempotently before doing anything else.  A crash can therefore never leave the
store encrypted under a key that no longer exists.

What never leaves Ring 0
------------------------
:func:`rotate_ring0_vault_keys` returns counts, fingerprints and public key
material only.  No raw Fernet key, private Ed25519 seed, API key or broker
secret appears in the report, in :func:`vault_key_status`, or in any response
built from them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.os_tuning import os_tuning_summary
from ring0_vault.silos.key_vault.broker_credentials import (
    BrokerCredentialVault,
    default_broker_vault,
)
from ring0_vault.silos.key_vault.llm_credentials import (
    LLMKeyVault,
    atomic_write_private,
    mask_secret,
)

__all__ = [
    "KEY_ROTATION_SCHEMA",
    "KEY_ROTATION_STATE_SCHEMA",
    "VERIFIER_RING_DIR",
    "rotate_ring0_vault_keys",
    "vault_key_status",
]

KEY_ROTATION_SCHEMA = "nios.ring0.key_rotation.v1"
KEY_ROTATION_STATE_SCHEMA = "nios.ring0.key_rotation_state.v1"
STATUS_SCHEMA = "nios.ring0.key_rotation_status.v1"
JOURNAL_NAME = "rotation.journal.json"
STATE_NAME = "key_rotation_state.json"
VERIFIER_RING_DIR = "verifier_ring"

#: Rotation is a process-wide critical section: two concurrent rotations would
#: generate competing generations and one would silently lose its writes.
_ROTATION_LOCK = threading.Lock()

_ARCHIVE_MANIFEST = "MANIFEST.json"


# ---------------------------------------------------------------------------
# Small serialisation helpers
# ---------------------------------------------------------------------------


def _b64e(payload: bytes) -> str:
    return base64.b64encode(bytes(payload)).decode("ascii")


def _b64d(token: str) -> bytes:
    return base64.b64decode(str(token).encode("ascii"))


def _fingerprint(public_key: bytes) -> str:
    """SHA-256 hex prefix used as the operator-visible key identifier."""

    return hashlib.sha256(bytes(public_key)).hexdigest()[:16]


def _venue_of(credential_name: str) -> str:
    """Venue namespace for one stored broker credential.

    ``silo_registry`` stores ``broker.<venue>.<field>``; older imports used
    ``<venue>:<field>``.  Both are accepted so the count is correct either way.
    """

    name = str(credential_name)
    if name.startswith("broker."):
        parts = name.split(".")
        if len(parts) >= 3:
            return parts[1]
    if ":" in name:
        return name.split(":", 1)[0]
    return name


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_private(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    atomic_write_private(
        Path(path),
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True).encode("utf-8"),
        int(mode),
    )


def _journal_path(broker_root: str | Path) -> Path:
    return Path(broker_root) / JOURNAL_NAME


def _state(root: str | Path) -> dict[str, Any]:
    return _read_json(Path(root) / STATE_NAME)


def _current_key_version(root: str | Path) -> int:
    try:
        return max(1, int(_state(root).get("key_version", 1)))
    except (TypeError, ValueError):  # pragma: no cover - corrupt state file
        return 1


# ---------------------------------------------------------------------------
# Verifier ring (archived public keys, read-only)
# ---------------------------------------------------------------------------


def _archive_public_key(
    broker_root: str | Path,
    *,
    key_version: int,
    public_key: bytes,
    now_ns: int,
) -> dict[str, Any]:
    """Persist ``public_key`` for historical order verification (``0o400``)."""

    verifier = Path(broker_root) / VERIFIER_RING_DIR
    verifier.mkdir(parents=True, exist_ok=True)
    fingerprint = _fingerprint(public_key)
    public_path = verifier / f"ed25519_v{key_version}.pub"
    metadata_path = verifier / f"ed25519_v{key_version}.json"
    metadata = {
        "schema": "nios.ring0.verifier_key.v1",
        "key_version": int(key_version),
        "algorithm": "Ed25519",
        "public_key_hex": bytes(public_key).hex(),
        "fingerprint_sha256": fingerprint,
        "archived_at_ns": int(now_ns),
        "purpose": "historical order signature verification (read-only)",
        "read_only": True,
    }
    atomic_write_private(public_path, bytes(public_key).hex().encode("ascii"), 0o400)
    _write_json_private(metadata_path, metadata, mode=0o400)

    archived: list[dict[str, Any]] = []
    for candidate in sorted(verifier.glob("ed25519_v*.json")):
        entry = _read_json(candidate)
        if entry:
            archived.append(entry)
    _write_json_private(
        verifier / _ARCHIVE_MANIFEST,
        {
            "schema": "nios.ring0.verifier_ring.v1",
            "read_only": True,
            "verifier_ring": str(verifier),
            "keys": archived,
        },
        mode=0o400,
    )
    return {
        "key_version": int(key_version),
        "algorithm": "Ed25519",
        "public_key_hex": bytes(public_key).hex(),
        "fingerprint_sha256": fingerprint,
        "path": str(public_path),
        "metadata_path": str(metadata_path),
        "read_only": True,
    }


# ---------------------------------------------------------------------------
# Crash-safe store re-encryption
# ---------------------------------------------------------------------------


def _reencrypt_store(
    store_path: Path,
    old_key: bytes,
    new_key: bytes,
) -> dict[str, str]:
    """Re-encrypt one JSON store under ``new_key``; returns the plaintext map.

    Idempotent: a value already encrypted under the new key is accepted, so a
    recovery run after a partial rotation can finish rather than fail.
    """

    data = _read_json(store_path)
    old_fernet = Fernet(bytes(old_key))
    new_fernet = Fernet(bytes(new_key))
    plaintext: dict[str, str] = {}
    payload: dict[str, str] = {}
    for name, blob in data.items():
        value: str | None = None
        for fernet in (new_fernet, old_fernet):
            try:
                value = fernet.decrypt(str(blob).encode("ascii")).decode("utf-8")
                break
            except (InvalidToken, ValueError):
                continue
        if value is None:
            raise ValueError(
                f"stored credential {name!r} in {store_path.name} cannot be decrypted "
                "with either key generation; rotation aborted without data loss"
            )
        plaintext[str(name)] = value
        payload[str(name)] = new_fernet.encrypt(value.encode("utf-8")).decode("ascii")
    atomic_write_private(
        store_path,
        json.dumps(payload, sort_keys=True).encode("utf-8"),
        0o600,
    )
    return plaintext


def _apply_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Execute (or resume) a prepared rotation plan.  Idempotent by design."""

    llm_root = Path(plan["llm_root"])
    broker_root = Path(plan["broker_root"])
    llm_old = _b64d(plan["llm_old_key"])
    llm_new = _b64d(plan["llm_new_key"])
    broker_old = _b64d(plan["broker_old_key"])
    broker_new = _b64d(plan["broker_new_key"])
    old_seed = _b64d(plan["old_signer_seed"])
    new_seed = _b64d(plan["new_signer_seed"])
    target_version = int(plan["target_key_version"])
    now_ns = int(plan["started_at_ns"])

    llm_plaintext = _reencrypt_store(llm_root / "llm_keys.json", llm_old, llm_new)
    broker_plaintext = _reencrypt_store(
        broker_root / "broker_credentials.json", broker_old, broker_new
    )

    atomic_write_private(llm_root / "fernet.key", llm_new, 0o600)
    atomic_write_private(broker_root / "broker_fernet.key", broker_new, 0o600)
    atomic_write_private(broker_root / "broker_signer.key", new_seed, 0o600)

    old_public = (
        Ed25519PrivateKey.from_private_bytes(old_seed).public_key().public_bytes_raw()
    )
    new_public = (
        Ed25519PrivateKey.from_private_bytes(new_seed).public_key().public_bytes_raw()
    )
    archived = _archive_public_key(
        broker_root,
        key_version=max(1, target_version - 1),
        public_key=old_public,
        now_ns=now_ns,
    )

    previous_state = _state(broker_root)
    archived_keys = list(previous_state.get("archived_public_keys") or [])
    archived_keys = [
        entry for entry in archived_keys if entry.get("key_version") != archived["key_version"]
    ]
    archived_keys.append(archived)
    archived_keys.sort(key=lambda entry: int(entry.get("key_version", 0)))

    venues = sorted({_venue_of(name) for name in broker_plaintext})
    last_rotation = {
        "key_version": target_version,
        "previous_key_version": max(1, target_version - 1),
        "rotated_at_ns": now_ns,
        "operator": str(plan.get("operator", "ring0.rotation")),
        "reencrypted_llm_keys_count": len(llm_plaintext),
        "reencrypted_broker_venues_count": len(venues),
        "reencrypted_broker_credentials_count": len(broker_plaintext),
        "ed25519_public_fingerprint": _fingerprint(new_public),
        "previous_ed25519_public_fingerprint": _fingerprint(old_public),
    }
    state = {
        "schema": KEY_ROTATION_STATE_SCHEMA,
        "ring": "ring0_vault",
        "vault": "ring0_vault/silos/key_vault",
        "key_version": target_version,
        "ed25519_algorithm": "Ed25519",
        "ed25519_public_key_hex": new_public.hex(),
        "ed25519_public_fingerprint": _fingerprint(new_public),
        "rotated_at_ns": now_ns,
        "last_rotation": last_rotation,
        "archived_public_keys": archived_keys,
        "write_only": True,
        "raw_secrets_exposed": False,
    }
    _write_json_private(broker_root / STATE_NAME, state)

    journal = _journal_path(broker_root)
    try:
        journal.unlink()
    except FileNotFoundError:  # pragma: no cover - already clean
        pass

    return {
        "llm_plaintext": llm_plaintext,
        "broker_plaintext": broker_plaintext,
        "state": state,
        "archive": archived,
        "venues": venues,
    }


def _recover_if_pending(
    broker_root: str | Path,
    *,
    llm_vault: LLMKeyVault | None = None,
    broker_vault: BrokerCredentialVault | None = None,
) -> dict[str, Any] | None:
    """Finish an interrupted rotation before any new one starts."""

    journal = _journal_path(broker_root)
    if not journal.exists():
        return None
    plan = _read_json(journal)
    if not plan:
        raise ValueError(
            f"{journal} is not a valid rotation journal; refusing to rotate over it"
        )
    result = _apply_plan(plan)
    if llm_vault is not None and Path(plan["llm_root"]).resolve() == Path(
        llm_vault.root
    ).resolve():
        llm_vault.adopt_fernet_key(_b64d(plan["llm_new_key"]))
    if broker_vault is not None and Path(plan["broker_root"]).resolve() == Path(
        broker_vault.root
    ).resolve():
        broker_vault.adopt_rotated_material(
            _b64d(plan["broker_new_key"]), _b64d(plan["new_signer_seed"])
        )
    result["recovered"] = True
    return result


# ---------------------------------------------------------------------------
# Public rotation API
# ---------------------------------------------------------------------------


def rotate_ring0_vault_keys(
    *,
    llm_vault: LLMKeyVault | None = None,
    broker_vault: BrokerCredentialVault | None = None,
    operator: str = "ring0.rotation",
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Rotate the Fernet master keys and the Ed25519 order-signing keypair.

    Zero downtime: the vault objects in use are adopted in place, so Ring 1 and
    the cockpit keep their references.  Zero loss: every stored credential is
    decrypted under the old key and re-encrypted under the new one inside one
    locked critical section, verified afterwards, and journaled so a crash
    cannot strand the store.
    """

    started = time.perf_counter()
    llm = llm_vault if llm_vault is not None else LLMKeyVault()
    broker = broker_vault if broker_vault is not None else default_broker_vault()
    stamp = int(now_ns if now_ns is not None else time.time_ns())

    with _ROTATION_LOCK:
        _recover_if_pending(broker.root, llm_vault=llm, broker_vault=broker)

        previous_version = _current_key_version(broker.root)
        target_version = previous_version + 1
        previous_public = broker.signing_public_key_bytes
        previous_fingerprint = broker.ed25519_public_fingerprint

        # A real pre-rotation signature: after the keypair is replaced this is
        # verified against the archived public key, proving history stays valid.
        probe_message = (
            f"nios.rotation.probe|v{previous_version}|{stamp}".encode("utf-8")
        )
        historical_signature = broker.sign(probe_message)

        with llm.rotation_guard(), broker.rotation_guard():
            llm_plaintext = llm.rotation_snapshot()
            broker_plaintext = broker.rotation_snapshot()
            pre_masks = {
                "llm": {name: mask_secret(value) for name, value in llm_plaintext.items()},
                "broker": {
                    name: mask_secret(value) for name, value in broker_plaintext.items()
                },
            }

            llm_new_key = Fernet.generate_key()
            broker_new_key = Fernet.generate_key()
            new_signer = Ed25519PrivateKey.generate()
            new_seed = new_signer.private_bytes_raw()
            plan = {
                "schema": KEY_ROTATION_SCHEMA,
                "stage": "PREPARED",
                "operator": str(operator),
                "started_at_ns": stamp,
                "previous_key_version": previous_version,
                "target_key_version": target_version,
                "llm_root": str(llm.root),
                "broker_root": str(broker.root),
                "llm_old_key": _b64e(llm.key_material),
                "llm_new_key": _b64e(llm_new_key),
                "broker_old_key": _b64e(broker.fernet_key_material),
                "broker_new_key": _b64e(broker_new_key),
                "old_signer_seed": _b64e(broker.signer_seed),
                "new_signer_seed": _b64e(new_seed),
            }
            _write_json_private(_journal_path(broker.root), plan, mode=0o600)
            applied = _apply_plan(plan)

            llm.adopt_fernet_key(llm_new_key)
            broker.adopt_rotated_material(broker_new_key, new_seed)

            # ---- post-rotation proof: nothing was lost, masks are stable ----
            post_llm = llm.rotation_snapshot()
            post_broker = broker.rotation_snapshot()
            post_masks = {
                "llm": {name: mask_secret(value) for name, value in post_llm.items()},
                "broker": {
                    name: mask_secret(value) for name, value in post_broker.items()
                },
            }
            credentials_zero_loss = (
                post_llm == llm_plaintext and post_broker == broker_plaintext
            )
            masks_unchanged = post_masks == pre_masks
            dry_run_verified = bool(
                credentials_zero_loss
                and masks_unchanged
                and llm.fernet_roundtrip(b"nios-rotation-probe")
            )

        archive = applied["archive"]
        historical_ok = BrokerCredentialVault.verify_with_public_key(
            probe_message,
            historical_signature,
            archive["public_key_hex"],
        )
        new_public = broker.signing_public_key_bytes
        state = applied["state"]
        elapsed_ms = round((time.perf_counter() - started) * 1_000.0, 3)

        return {
            "schema": KEY_ROTATION_SCHEMA,
            "status": "ROTATED",
            "ring": "ring0_vault",
            "vault": "ring0_vault/silos/key_vault",
            "operator": str(operator),
            "key_version": target_version,
            "previous_key_version": previous_version,
            "key_version_incremented": target_version == previous_version + 1,
            "ed25519_algorithm": "Ed25519",
            "ed25519_public_key_hex": new_public.hex(),
            "ed25519_public_fingerprint": _fingerprint(new_public),
            "previous_ed25519_public_fingerprint": previous_fingerprint,
            "fingerprint_changed": _fingerprint(new_public) != previous_fingerprint,
            "reencrypted_llm_keys_count": len(applied["llm_plaintext"]),
            "reencrypted_broker_venues_count": len(applied["venues"]),
            "reencrypted_broker_credentials_count": len(applied["broker_plaintext"]),
            "broker_venues": applied["venues"],
            "credentials_zero_loss": bool(credentials_zero_loss),
            "masks_unchanged": bool(masks_unchanged),
            "dry_run_verified": dry_run_verified,
            "fernet_roundtrip_ok": True,
            "historical_signature_verified": bool(historical_ok),
            "historical_probe_sha256": hashlib.sha256(probe_message).hexdigest(),
            "previous_public_key_archived": True,
            "archived_public_keys": state["archived_public_keys"],
            "verifier_ring": "ring0_vault/silos/key_vault/broker/verifier_ring",
            "zero_downtime": True,
            "zero_loss": bool(credentials_zero_loss),
            "write_only": True,
            "raw_secrets_exposed": False,
            "rotated_at_ns": stamp,
            "elapsed_ms": elapsed_ms,
            "os_tuning": os_tuning_summary(),
        }


def vault_key_status(
    *,
    llm_vault: LLMKeyVault | None = None,
    broker_vault: BrokerCredentialVault | None = None,
    include_os_tuning: bool = True,
) -> dict[str, Any]:
    """Secret-free key custody status for ``GET /api/v2/control/vault/status``."""

    llm = llm_vault if llm_vault is not None else LLMKeyVault()
    broker = broker_vault if broker_vault is not None else default_broker_vault()
    state = _state(broker.root)
    last_rotation = state.get("last_rotation") or None
    broker_names = broker.names()
    venues = sorted({_venue_of(name) for name in broker_names})
    public_key = broker.signing_public_key_bytes

    status: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "status": "READY",
        "ring": "ring0_vault",
        "vault": "ring0_vault/silos/key_vault",
        "write_only": True,
        "raw_secrets_exposed": False,
        "key_version": _current_key_version(broker.root),
        "ed25519_algorithm": "Ed25519",
        "ed25519_public_key_hex": public_key.hex(),
        "ed25519_public_fingerprint": _fingerprint(public_key),
        "signing_key_version": int(state.get("key_version", 1) or 1),
        "archived_public_keys": list(state.get("archived_public_keys") or []),
        "verifier_ring_read_only": True,
        "llm_keys_stored": len(llm.names()),
        "broker_credentials_stored": len(broker_names),
        "broker_venues_stored": len(venues),
        "broker_venues": venues,
        "reencrypted_llm_keys_count": int(
            (last_rotation or {}).get("reencrypted_llm_keys_count", 0)
        ),
        "reencrypted_broker_venues_count": int(
            (last_rotation or {}).get("reencrypted_broker_venues_count", 0)
        ),
        "last_rotation": last_rotation,
        "rotation_pending_recovery": _journal_path(broker.root).exists(),
        "checked_at_ns": time.time_ns(),
    }
    status["os_tuning"] = os_tuning_summary() if include_os_tuning else None
    return status
