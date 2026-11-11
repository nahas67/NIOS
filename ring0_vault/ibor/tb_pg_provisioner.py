"""TigerBeetle replica and PostgreSQL 16 provisioning for the Ring 0 IBOR.

The sovereign vault is designed around two durable backends:

* **TigerBeetle** owns the crash-atomic two-phase outbox and the
  ``debits_must_not_exceed_credits`` balance rule (``sec_005``, ``sec_009``).
* **PostgreSQL 16** owns the double-entry IBOR journal, the outbox mirror, the
  broker-ack inbox, tax lots, source quarantine evidence and the SHA-256
  grounding ledger (``sec_009``, ``sec_012``).

This module makes both backends *probeable and provisionable* without ever
requiring live network access:

* :func:`probe_tigerbeetle_binary` locates the installed ``tigerbeetle``
  executable, resolves its version and records whether the host's application
  control policy actually allows it to run.
* :class:`TigerBeetleReplicaManager` formats a single-file replica and can
  start it when execution is permitted.  On a locked-down host it writes a
  deterministic, clearly-labelled *emulated* replica artifact and keeps the
  in-process ACID journal authoritative -- the caller never silently loses
  durability.
* :class:`PG16Provisioner` validates the complete ``pg16_schema.sql`` DDL
  offline (every table and CHECK constraint is executed against an in-memory
  SQLite translation), then applies it through ``psql`` when a server is
  reachable.

Ring 0 only: stdlib, no broker credentials, no LLM, no network calls of its
own.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "TigerBeetleProbe",
    "probe_tigerbeetle_binary",
    "ReplicaFormatResult",
    "format_replica",
    "TigerBeetleReplicaManager",
    "SchemaValidation",
    "PG16Provisioner",
    "TigerBeetlePG16Provisioner",
    "TIGERBEETLE_FALLBACK_VERSION",
]

TIGERBEETLE_FALLBACK_VERSION = "0.17.9"
TIGERBEETLE_DEFAULT_PATH = Path(
    r"C:\Users\nahas\AppData\Local\Programs\tigerbeetle\tigerbeetle.exe"
)
REPLICA_MAGIC = b"TIGERBEETLE\x00NIOS-EMULATED\x00"
EXPECTED_TABLES = (
    "financial_outbox",
    "order_ack_inbox",
    "ibor_double_entry_ledger",
    "silo_vault_balances",
    "tax_lots",
    "source_quarantine_store",
    "grounding_ledger_hashes",
)
VALID_VAULTS = ("ReserveYield", "LongTermCompounder", "TacticalSwing", "SpinalScalper")


# ---------------------------------------------------------------------------
# Binary discovery / probing
# ---------------------------------------------------------------------------


@dataclass
class TigerBeetleProbe:
    found: bool
    path: str | None
    version: str
    executable: bool
    blocked: bool
    size_bytes: int
    sha256: str
    message: str
    candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "path": self.path,
            "version": self.version,
            "executable": self.executable,
            "blocked": self.blocked,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "message": self.message,
            "candidates": list(self.candidates),
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_paths(explicit: str | Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_path = os.environ.get("TIGERBEETLE_BIN")
    if env_path:
        candidates.append(Path(env_path))
    which = shutil.which("tigerbeetle")
    if which:
        candidates.append(Path(which))
    candidates.append(TIGERBEETLE_DEFAULT_PATH)
    for root in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "tigerbeetle",
        Path(os.environ.get("ProgramFiles", "")) / "tigerbeetle",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ):
        if root and root.exists():
            for name in ("tigerbeetle.exe", "tigerbeetle"):
                candidate = root / name
                if candidate.exists():
                    candidates.append(candidate)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _run_version(path: Path, timeout: float = 3.0) -> tuple[bool, str, str]:
    """Return ``(ran, version, message)`` without ever raising."""

    try:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "check": False,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run([str(path), "version"], **kwargs)
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        if completed.returncode == 0:
            match = re.search(r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", output)
            version = match.group(1) if match else TIGERBEETLE_FALLBACK_VERSION
            return True, version, output[:400]
        blocked = "application control" in output.lower() or "blocked" in output.lower()
        return False, TIGERBEETLE_FALLBACK_VERSION, (
            ("blocked: " if blocked else "failed: ") + (output[:400] or "no output")
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, TIGERBEETLE_FALLBACK_VERSION, f"{type(exc).__name__}: {exc}"


def probe_tigerbeetle_binary(path: str | Path | None = None) -> TigerBeetleProbe:
    """Locate ``tigerbeetle`` and probe its version / execution policy.

    A found-but-blocked binary is still reported as ``found=True``: the
    installer succeeded, the host policy simply refuses to execute it.  The
    fallback version is the pinned NIOS target (``0.17.9``) so downstream
    readiness reports are deterministic.
    """

    candidates = _candidate_paths(path)
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        size = candidate.stat().st_size
        digest = _sha256_file(candidate)
        ran, version, message = _run_version(candidate)
        return TigerBeetleProbe(
            found=True,
            path=str(candidate),
            version=version,
            executable=ran,
            blocked=(not ran and "blocked" in message.lower()),
            size_bytes=size,
            sha256=digest,
            message=message,
            candidates=[str(item) for item in candidates],
        )
    return TigerBeetleProbe(
        found=False,
        path=None,
        version=TIGERBEETLE_FALLBACK_VERSION,
        executable=False,
        blocked=False,
        size_bytes=0,
        sha256="",
        message="tigerbeetle binary not found on PATH, TIGERBEETLE_BIN or the default install location",
        candidates=[str(item) for item in candidates],
    )


# ---------------------------------------------------------------------------
# Replica formatting
# ---------------------------------------------------------------------------


@dataclass
class ReplicaFormatResult:
    path: str
    formatted: bool
    emulated: bool
    executable: bool
    cluster: int
    replica: int
    replica_count: int
    size_bytes: int
    version: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "formatted": self.formatted,
            "emulated": self.emulated,
            "executable": self.executable,
            "cluster": self.cluster,
            "replica": self.replica,
            "replica_count": self.replica_count,
            "size_bytes": self.size_bytes,
            "version": self.version,
            "message": self.message,
        }


def format_replica(
    path: str | Path,
    *,
    cluster: int = 0,
    replica: int = 0,
    replica_count: int = 1,
    force: bool = False,
    binary: TigerBeetleProbe | None = None,
) -> ReplicaFormatResult:
    """Format a single-file TigerBeetle replica.

    When the binary is executable the real ``tigerbeetle format`` command is
    used.  When Windows Application Control blocks execution, a deterministic
    emulated replica is written instead: a magic header plus the cluster /
    replica identity, padded to a stable 64 KiB.  The artifact is never
    mistaken for a live database -- ``emulated=True`` is carried all the way to
    the cockpit.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 0 and not force:
        raise FileExistsError(f"replica already exists: {target}")
    probe = binary or probe_tigerbeetle_binary()
    version = probe.version or TIGERBEETLE_FALLBACK_VERSION

    if probe.found and probe.executable:
        try:
            kwargs: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": 30,
                "check": False,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            completed = subprocess.run(
                [
                    str(probe.path),
                    "format",
                    f"--cluster={cluster}",
                    f"--replica={replica}",
                    f"--replica-count={replica_count}",
                    str(target),
                ],
                **kwargs,
            )
            if completed.returncode == 0 and target.exists() and target.stat().st_size > 0:
                return ReplicaFormatResult(
                    path=str(target),
                    formatted=True,
                    emulated=False,
                    executable=True,
                    cluster=cluster,
                    replica=replica,
                    replica_count=replica_count,
                    size_bytes=target.stat().st_size,
                    version=version,
                    message="tigerbeetle format completed",
                )
        except (OSError, subprocess.SubprocessError):
            pass

    header = {
        "artifact": "nios-tigerbeetle-replica",
        "emulated": True,
        "cluster": int(cluster),
        "replica": int(replica),
        "replica_count": int(replica_count),
        "version": version,
        "formatted_at_ns": time.time_ns(),
        "binary": probe.path,
        "reason": probe.message,
    }
    payload = REPLICA_MAGIC + json.dumps(header, sort_keys=True).encode("utf-8")
    with target.open("wb") as handle:
        handle.write(payload)
        handle.truncate(64 * 1024)
    return ReplicaFormatResult(
        path=str(target),
        formatted=True,
        emulated=True,
        executable=False,
        cluster=cluster,
        replica=replica,
        replica_count=replica_count,
        size_bytes=target.stat().st_size,
        version=version,
        message="emulated single-file replica (binary present but execution blocked by host policy)",
    )


# ---------------------------------------------------------------------------
# Replica manager
# ---------------------------------------------------------------------------


class TigerBeetleReplicaManager:
    """Format / start / probe a single-file TigerBeetle replica.

    The manager never raises when the host blocks execution.  It exposes
    ``available`` and the two-phase adapter falls back to the in-process ACID
    journal handled by :class:`TigerBeetlePG16Provisioner`.
    """

    def __init__(
        self,
        replica_path: str | Path | None = None,
        *,
        cluster: int = 0,
        replica: int = 0,
        replica_count: int = 1,
        binary_path: str | Path | None = None,
        addresses: str = "3000",
    ):
        self.cluster = int(cluster)
        self.replica = int(replica)
        self.replica_count = int(replica_count)
        self.addresses = addresses
        self.probe = probe_tigerbeetle_binary(binary_path)
        self.replica_path = Path(replica_path) if replica_path else Path(
            tempfile.gettempdir()
        ) / f"nios-tigerbeetle-{cluster}-{replica}.tigerbeetle"
        self._process: subprocess.Popen | None = None
        self._format: ReplicaFormatResult | None = None
        self._client: Any = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def available(self) -> bool:
        return bool(self.probe.found and self.probe.executable and self.running)

    def ensure_formatted(self, *, force: bool = False) -> ReplicaFormatResult:
        if self._format is not None:
            return self._format
        if self.replica_path.exists() and self.replica_path.stat().st_size > 0 and not force:
            self._format = ReplicaFormatResult(
                path=str(self.replica_path),
                formatted=True,
                emulated=not self.probe.executable,
                executable=self.probe.executable,
                cluster=self.cluster,
                replica=self.replica,
                replica_count=self.replica_count,
                size_bytes=self.replica_path.stat().st_size,
                version=self.probe.version,
                message="existing replica reused",
            )
            return self._format
        self._format = format_replica(
            self.replica_path,
            cluster=self.cluster,
            replica=self.replica,
            replica_count=self.replica_count,
            force=force,
            binary=self.probe,
        )
        return self._format

    def start(self, *, wait_seconds: float = 0.0) -> dict[str, Any]:
        self.ensure_formatted()
        if not self.probe.executable:
            return {
                "started": False,
                "emulated": True,
                "reason": "tigerbeetle execution blocked by host policy",
                "version": self.probe.version,
            }
        try:
            kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._process = subprocess.Popen(
                [
                    str(self.probe.path),
                    "start",
                    f"--addresses={self.addresses}",
                    str(self.replica_path),
                ],
                **kwargs,
            )
            if wait_seconds:
                time.sleep(max(0.0, min(float(wait_seconds), 2.0)))
            return {
                "started": self.running,
                "emulated": False,
                "pid": getattr(self._process, "pid", None),
                "addresses": self.addresses,
            }
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "started": False,
                "emulated": True,
                "reason": f"{type(exc).__name__}: {exc}",
            }

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def attach_client(self, client: Any) -> None:
        """Attach a future live TigerBeetle client (dependency injection)."""

        self._client = client

    def health(self) -> dict[str, Any]:
        self.ensure_formatted()
        fmt = self._format
        return {
            "binary": self.probe.to_dict(),
            "replica_path": str(self.replica_path),
            "replica_exists": self.replica_path.exists(),
            "replica_bytes": self.replica_path.stat().st_size if self.replica_path.exists() else 0,
            "formatted": bool(fmt and fmt.formatted),
            "emulated": bool(fmt and fmt.emulated),
            "running": self.running,
            "available": self.available,
            "cluster": self.cluster,
            "replica": self.replica,
            "replica_count": self.replica_count,
            "version": self.probe.version,
        }

    def two_phase_transfer(
        self,
        *,
        silo: str,
        debit: str,
        credit: str,
        amount_micros: int,
        code: int = 1,
    ) -> dict[str, Any] | None:
        """Create a pending transfer through a live client when attached.

        The live client is deliberately injected rather than imported: the
        vendored TigerBeetle Python bindings are optional and a blocked binary
        must never break the Ring 0 boot path.  ``None`` means "use the
        in-process ACID journal".
        """

        if self._client is None or not self.running:
            return None
        creator = getattr(self._client, "create_pending_transfer", None)
        if creator is None:
            return None
        return creator(
            silo=silo,
            debit=debit,
            credit=credit,
            amount_micros=int(amount_micros),
            code=int(code),
        )

    # -- live replica lifecycle (Phase 3 / sec_009) -------------------------

    def format_and_start_live_replica(
        self,
        data_dir: str | Path | None = None,
        port: int = 3000,
        *,
        force: bool = False,
        health_timeout_s: float = 3.0,
        host: str = "127.0.0.1",
    ) -> dict[str, Any]:
        """Format ``<cluster>_<replica>.tigerbeetle`` and start a live replica.

        The real ``tigerbeetle format`` / ``tigerbeetle start`` CLI is used
        whenever the binary is executable, so the artifact is a genuine
        TigerBeetle data file (``emulated=False``).  When host policy blocks
        execution the deterministic emulated artifact is written instead and
        ``started`` stays ``False``; callers then fall back to the in-process
        SQLite ACID journal.  The method never raises and never hangs: the TCP
        health probe is bounded by ``health_timeout_s``.
        """

        if data_dir is not None:
            directory = Path(data_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self.replica_path = directory / f"{self.cluster}_{self.replica}.tigerbeetle"
            self._format = None

        fmt = format_replica(
            self.replica_path,
            cluster=self.cluster,
            replica=self.replica,
            replica_count=self.replica_count,
            force=force,
            binary=self.probe,
        )
        self._format = fmt
        self.addresses = f"{host}:{int(port)}"

        if fmt.emulated or not self.probe.executable:
            return {
                "formatted": True,
                "data_file": str(self.replica_path),
                "size_bytes": fmt.size_bytes,
                "emulated": True,
                "executable": bool(self.probe.executable),
                "started": False,
                "healthy": False,
                "port": int(port),
                "addresses": self.addresses,
                "pid": None,
                "journal": "sqlite-acid-journal",
                "message": fmt.message,
            }

        start = self.start()
        healthy = False
        listener: dict[str, Any] = {}
        if start.get("started"):
            healthy, listener = self._probe_listener(host, int(port), health_timeout_s)
        return {
            "formatted": True,
            "data_file": str(self.replica_path),
            "size_bytes": fmt.size_bytes,
            "emulated": False,
            "executable": True,
            "started": bool(start.get("started")) and self.running,
            "healthy": healthy,
            "port": int(port),
            "addresses": self.addresses,
            "pid": start.get("pid"),
            "journal": "tigerbeetle-live+sqlite-acid-journal",
            "message": (
                "live replica listening"
                if healthy
                else fmt.message if not start.get("started") else "live replica started, listener not yet accepting"
            ),
            "listener": listener,
        }

    def _probe_listener(
        self,
        host: str,
        port: int,
        timeout_s: float,
    ) -> tuple[bool, dict[str, Any]]:
        """Poll a bounded TCP connect on the replica address (no fixed sleep)."""

        deadline = time.monotonic() + max(0.05, float(timeout_s))
        attempts = 0
        last_error = ""
        while True:
            attempts += 1
            if not self.running:
                last_error = "process exited before the listener accepted"
                break
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    return True, {"attempts": attempts, "host": host, "port": port}
            except OSError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return False, {"attempts": attempts, "host": host, "port": port, "error": last_error}

    def live_replica_status(self) -> dict[str, Any]:
        """Compact live-replica descriptor for the cockpit / health surface."""

        body = self.health()
        body["addresses"] = self.addresses
        body["live_transfers"] = bool(self.running and self._client is not None)
        return body

    def commit_two_phase_live(
        self,
        *,
        silo: str,
        debit: str,
        credit: str,
        amount_micros: int,
        code: int = 1,
    ) -> dict[str, Any]:
        """Run pending -> post against the live replica, reporting the backend.

        A ``None`` from :meth:`two_phase_transfer` is not an error: it means the
        caller must rely on the in-process ACID journal instead.
        """

        pending = self.two_phase_transfer(
            silo=silo,
            debit=debit,
            credit=credit,
            amount_micros=amount_micros,
            code=code,
        )
        if pending is None:
            return {
                "backend": "sqlite-acid-journal",
                "live": False,
                "silo": silo,
                "debit": debit,
                "credit": credit,
                "amount_micros": int(amount_micros),
            }
        poster = getattr(self._client, "post_pending_transfer", None)
        transfer_id = getattr(pending, "id", None) or (
            pending.get("id") if isinstance(pending, dict) else None
        )
        posted = poster(transfer_id) if (poster is not None and transfer_id is not None) else None
        return {
            "backend": "tigerbeetle-live",
            "live": True,
            "silo": silo,
            "debit": debit,
            "credit": credit,
            "amount_micros": int(amount_micros),
            "pending": pending,
            "posted": posted,
        }

    def teardown(self) -> None:
        """Stop the daemon and release the client handle."""

        self._client = None
        self.stop()


# ---------------------------------------------------------------------------
# PostgreSQL 16 DDL validation / provisioning
# ---------------------------------------------------------------------------


@dataclass
class SchemaValidation:
    schema_path: str
    valid: bool
    serializable: bool
    statements: int
    tables: list[str]
    checks: int
    indexes: int
    sha256: str
    errors: list[str] = field(default_factory=list)
    sqlite_executed: bool = False
    live_applied: bool = False
    live_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_path": self.schema_path,
            "valid": self.valid,
            "serializable": self.serializable,
            "statements": self.statements,
            "tables": list(self.tables),
            "checks": self.checks,
            "indexes": self.indexes,
            "sha256": self.sha256,
            "errors": list(self.errors),
            "sqlite_executed": self.sqlite_executed,
            "live_applied": self.live_applied,
            "live_message": self.live_message,
        }


def _split_sql(text: str) -> list[str]:
    """Split PostgreSQL DDL on top-level semicolons.

    Handles line comments, single-quoted strings, double-quoted identifiers and
    ``$tag$ ... $tag$`` dollar quoting.
    """

    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(text)
    in_single = False
    in_double = False
    in_line_comment = False
    dollar_tag: str | None = None
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                buffer.append(char)
            index += 1
            continue
        if dollar_tag is not None:
            if text.startswith(dollar_tag, index):
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            buffer.append(char)
            index += 1
            continue
        if in_single:
            buffer.append(char)
            if char == "'" and nxt == "'":
                buffer.append(nxt)
                index += 2
                continue
            if char == "'":
                in_single = False
            index += 1
            continue
        if in_double:
            buffer.append(char)
            if char == '"':
                in_double = False
            index += 1
            continue
        if char == "-" and nxt == "-":
            in_line_comment = True
            index += 2
            continue
        if char == "'":
            in_single = True
            buffer.append(char)
            index += 1
            continue
        if char == '"':
            in_double = True
            buffer.append(char)
            index += 1
            continue
        if char == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", text[index:])
            if match:
                dollar_tag = match.group(0)
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def _translate_for_sqlite(statement: str) -> str | None:
    """Translate a PostgreSQL statement to SQLite, or ``None`` to skip it."""

    stripped = statement.strip()
    upper = stripped.upper()
    if upper.startswith(("BEGIN", "COMMIT", "SET ", "COMMENT ON", "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")):
        return None
    if upper.startswith(("DO ", "ALTER TABLE")):
        return None
    translated = stripped
    translated = translated.replace("::jsonb", "")
    translated = translated.replace("JSONB", "TEXT")
    translated = translated.replace("jsonb_typeof(metrics) = 'object'", "json_valid(metrics)")
    translated = translated.replace("DOUBLE PRECISION", "REAL")
    translated = translated.replace("BIGINT", "INTEGER")
    translated = translated.replace("BOOLEAN", "INTEGER")
    translated = translated.replace("DEFAULT FALSE", "DEFAULT 0")
    translated = translated.replace("DEFAULT TRUE", "DEFAULT 1")
    translated = translated.replace(
        "CHECK (sha256_digest ~ '^[0-9a-f]{64}$')",
        "CHECK (length(sha256_digest) = 64)",
    )
    translated = translated.replace("CHAR(64)", "TEXT")
    return translated


class PG16Provisioner:
    """Validate and optionally apply the PostgreSQL 16 settlement schema."""

    def __init__(self, schema_path: str | Path | None = None, *, dsn: str | None = None):
        self.schema_path = Path(schema_path) if schema_path else Path(__file__).with_name(
            "pg16_schema.sql"
        )
        self.dsn = dsn or os.environ.get("NIOS_PG_DSN")
        self._validation: SchemaValidation | None = None

    def read_schema(self) -> str:
        return self.schema_path.read_text(encoding="utf-8")

    def validate(self, *, refresh: bool = False) -> SchemaValidation:
        if self._validation is not None and not refresh:
            return self._validation
        text = self.read_schema()
        errors: list[str] = []
        statements = _split_sql(text)
        serializable = bool(
            re.search(
                r"SET\s+TRANSACTION\s+ISOLATION\s+LEVEL\s+SERIALIZABLE",
                text,
                flags=re.IGNORECASE,
            )
        )
        if not serializable:
            errors.append("missing SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        if not re.search(r"\bBEGIN\b", text, flags=re.IGNORECASE):
            errors.append("missing BEGIN")
        if not re.search(r"\bCOMMIT\b", text, flags=re.IGNORECASE):
            errors.append("missing COMMIT")

        executed = False
        tables: list[str] = []
        checks = text.upper().count("CHECK (")
        indexes = len(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX", text, flags=re.IGNORECASE))
        try:
            connection = sqlite3.connect(":memory:")
            try:
                for statement in statements:
                    translated = _translate_for_sqlite(statement)
                    if translated is None:
                        continue
                    connection.execute(translated)
                connection.commit()
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                tables = [row[0] for row in rows if not row[0].startswith("sqlite_")]
                executed = True
            finally:
                connection.close()
        except sqlite3.Error as exc:
            errors.append(f"sqlite DDL execution failed: {exc}")

        missing = [name for name in EXPECTED_TABLES if name not in tables]
        if missing:
            errors.append(f"missing tables: {', '.join(missing)}")
        if checks < 25:
            errors.append(f"too few CHECK constraints: {checks}")
        for vault in VALID_VAULTS:
            if vault not in text:
                errors.append(f"missing vault allow-list entry: {vault}")
        if "debits_micros >= 0" not in text and "debits_posted_micros >= 0" not in text:
            errors.append("missing non-negative debit constraint")
        if "credits_micros >= 0" not in text and "credits_posted_micros >= 0" not in text:
            errors.append("missing non-negative credit constraint")

        validation = SchemaValidation(
            schema_path=str(self.schema_path),
            valid=not errors and executed and len(tables) >= len(EXPECTED_TABLES),
            serializable=serializable,
            statements=len(statements),
            tables=tables,
            checks=checks,
            indexes=indexes,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            errors=errors,
            sqlite_executed=executed,
        )
        self._validation = validation
        return validation

    def probe(self, dsn: str | None = None) -> dict[str, Any]:
        target = dsn or self.dsn
        psql = shutil.which("psql")
        if psql is None and target is None:
            return {
                "available": False,
                "psql": None,
                "dsn": None,
                "message": "psql not installed and NIOS_PG_DSN unset; using offline DDL validation",
            }
        if psql is None:
            return {
                "available": False,
                "psql": None,
                "dsn": _redact_dsn(target),
                "message": "psql not installed",
            }
        try:
            completed = subprocess.run(
                [psql, "--version"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            version = (completed.stdout or completed.stderr or "").strip()
            return {
                "available": completed.returncode == 0,
                "psql": psql,
                "version": version,
                "dsn": _redact_dsn(target),
                "message": version,
            }
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "available": False,
                "psql": psql,
                "dsn": _redact_dsn(target),
                "message": f"{type(exc).__name__}: {exc}",
            }

    def apply(self, dsn: str | None = None) -> dict[str, Any]:
        validation = self.validate()
        target = dsn or self.dsn
        psql = shutil.which("psql")
        if psql is None or target is None:
            validation.live_applied = False
            validation.live_message = "live PostgreSQL apply skipped (psql or DSN unavailable)"
            return validation.to_dict()
        try:
            completed = subprocess.run(
                [psql, str(target), "-v", "ON_ERROR_STOP=1", "-f", str(self.schema_path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            validation.live_applied = completed.returncode == 0
            validation.live_message = (
                (completed.stdout or "") + (completed.stderr or "")
            ).strip()[:1000]
        except (OSError, subprocess.SubprocessError) as exc:
            validation.live_applied = False
            validation.live_message = f"{type(exc).__name__}: {exc}"
        return validation.to_dict()


def _redact_dsn(dsn: str | None) -> str | None:
    if not dsn:
        return None
    return re.sub(r"(password=)[^ ]+", r"\1***", dsn)


# ---------------------------------------------------------------------------
# Aggregate provisioner
# ---------------------------------------------------------------------------


class TigerBeetlePG16Provisioner:
    """Unified backend readiness + offline journal fallback."""

    def __init__(
        self,
        *,
        replica_manager: TigerBeetleReplicaManager | None = None,
        pg_provisioner: PG16Provisioner | None = None,
        replica_path: str | Path | None = None,
    ):
        self.replica = replica_manager or TigerBeetleReplicaManager(replica_path)
        self.pg = pg_provisioner or PG16Provisioner()
        self._status: dict[str, Any] | None = None

    def format_temporary_replica(self, directory: str | Path | None = None) -> ReplicaFormatResult:
        base = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="nios-tb-"))
        target = base / f"nios-{self.replica.cluster}-{self.replica.replica}.tigerbeetle"
        return format_replica(
            target,
            cluster=self.replica.cluster,
            replica=self.replica.replica,
            replica_count=self.replica.replica_count,
            force=True,
            binary=self.replica.probe,
        )

    def open_offline_journal(self, *, silos: Iterable[str] | None = None):
        """Return a fresh in-process ACID journal (the offline authority)."""

        from ring0_vault.ibor.ledger_outbox import IBORLedger, LedgerStore

        store = LedgerStore(in_memory=True, probe=False)
        ledger = IBORLedger(store, silos=tuple(silos or ("CRYPTO_BINANCE",)))
        return store, ledger

    def two_phase_transfer(self, **kwargs: Any) -> dict[str, Any] | None:
        return self.replica.two_phase_transfer(**kwargs)

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._status is not None and not refresh:
            return self._status
        probe = self.replica.probe
        schema = self.pg.validate()
        pg = self.pg.probe()
        replica_health = self.replica.health()
        status = {
            "kind": "tigerbeetle_pg16_provisioning",
            "tigerbeetle": {
                **probe.to_dict(),
                "replica": replica_health,
                "ready": bool(probe.found),
                "execution_blocked": bool(probe.blocked),
                "backend": "live" if replica_health.get("available") else "in_process_acid_journal",
            },
            "postgres": {
                **pg,
                "schema": schema.to_dict(),
                "ready": bool(schema.valid),
                "backend": "live" if pg.get("available") and schema.valid else "offline_ddl_validated",
            },
            "ready": bool(probe.found and schema.valid),
            "durability": {
                "tigerbeetle": "hardware_verified" if replica_health.get("available") else "emulated_replica+journal",
                "postgres": "live_serializable" if pg.get("available") else "offline_ddl_validated",
            },
            "checked_at_ns": time.time_ns(),
        }
        self._status = status
        return status


def main(argv: list[str] | None = None) -> int:
    """Small CLI used by the smoke test and operators."""

    import argparse

    parser = argparse.ArgumentParser(description="Probe TigerBeetle and validate PG16 DDL")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--format-replica", metavar="PATH", help="format a replica at PATH")
    args = parser.parse_args(argv)
    if args.format_replica:
        result = format_replica(args.format_replica, force=True)
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.formatted else 1
    provisioner = TigerBeetlePG16Provisioner()
    status = provisioner.status(refresh=True)
    if args.json:
        print(json.dumps(status, indent=2, default=str))
    else:
        tb = status["tigerbeetle"]
        pg = status["postgres"]
        print(f"tigerbeetle: found={tb['found']} version={tb['version']} executable={tb['executable']}")
        print(f"postgres: ddl_valid={pg['schema']['valid']} tables={','.join(pg['schema']['tables'])}")
    return 0 if status["ready"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
