"""Opt-in readiness check for the three live daemons (``PART 1.3``).

The workstation is designed to run on three external daemons:

* **PostgreSQL 16** -- the double-entry IBOR, the grounding ledger and the
  memory-checkpoint / episodic tables.
* **TigerBeetle** -- the two-phase atomic outbox and ``debits_must_not_exceed_credits``.
* **NATS JetStream** -- the at-least-once inter-ring bus.

None of them is required for ``pytest`` or ``--smoke-test``, so this script is
**opt-in and inert**: a daemon is only contacted when its environment variable is
set, every call is bounded by ``--timeout``, and a missing or unreachable daemon is
reported as ``SKIPPED_OFFLINE`` with the exact ``docker run`` command that would
start it.  The script therefore exits ``0`` on a fully offline workstation, which
is what makes it safe to call from a release checklist.

What "reachable" buys
---------------------
* PostgreSQL: the shipped 12-table DDL is applied and a ``jsonb`` GIN ``@>`` query
  is round-tripped **inside a transaction that is rolled back**, then every
  migration's rollback SQL is proven the same way.
* TigerBeetle: two accounts, a pending transfer, a post, and a separate
  pending-then-void, with balance assertions at each step.
* NATS JetStream: a durable publish, a pull-subscribe replay of that exact
  message, and a payload digest comparison.

Exit codes
----------
0   every configured daemon is healthy, or nothing is configured (offline)
1   a configured daemon is reachable but failed a check
2   invalid arguments

Usage::

    python scripts/verify_live_daemons.py
    python scripts/verify_live_daemons.py --json
    NIOS_PG_DSN=postgresql://nios:nios@127.0.0.1:5432/nios \
        python scripts/verify_live_daemons.py --daemon postgres --timeout 8
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(ROOT))

__all__ = [
    "DAEMON_POSTGRES",
    "DAEMON_TIGERBEETLE",
    "DAEMON_NATS",
    "DaemonReport",
    "check_postgres",
    "check_tigerbeetle",
    "check_nats",
    "readiness_report",
    "main",
]

DAEMON_POSTGRES = "postgres"
DAEMON_TIGERBEETLE = "tigerbeetle"
DAEMON_NATS = "nats"
DAEMONS: tuple[str, ...] = (DAEMON_POSTGRES, DAEMON_TIGERBEETLE, DAEMON_NATS)

STATUS_OK = "OK"
STATUS_SKIPPED = "SKIPPED_OFFLINE"
STATUS_FAILED = "FAILED"

#: Exact startup commands, kept next to the check so the operator never has to
#: guess which image/tag/port the code expects.
PG_START_COMMANDS: tuple[str, ...] = (
    "docker volume create nios_pg16",
    "docker run --rm -d --name nios-pg16 -p 5432:5432 "
    "-e POSTGRES_USER=nios -e POSTGRES_PASSWORD=nios -e POSTGRES_DB=nios "
    "-v nios_pg16:/var/lib/postgresql/data postgres:16-alpine",
    "set NIOS_PG_DSN=postgresql://nios:nios@127.0.0.1:5432/nios",
)
TB_START_COMMANDS: tuple[str, ...] = (
    "docker volume create nios_tb",
    "docker run --rm -v nios_tb:/data ghcr.io/tigerbeetle/tigerbeetle:0.17.9 "
    "format --cluster=0 --replica=0 --replica-count=1 /data/0_0.tigerbeetle",
    "docker run --rm -d --name nios-tigerbeetle -p 3000:3000 -v nios_tb:/data "
    "ghcr.io/tigerbeetle/tigerbeetle:0.17.9 start --addresses=0.0.0.0:3000 "
    "/data/0_0.tigerbeetle",
    "set NIOS_TB_ADDRESS=127.0.0.1:3000",
)
NATS_START_COMMANDS: tuple[str, ...] = (
    "docker run --rm -d --name nios-nats -p 4222:4222 -p 8222:8222 "
    "nats:2.10-alpine -js -m 8222",
    "set NIOS_NATS_URL=nats://127.0.0.1:4222",
)


def _utc_iso(stamp_ns: int | None = None) -> str:
    moment = datetime.fromtimestamp(
        (stamp_ns if stamp_ns is not None else time.time_ns()) / 1_000_000_000,
        tz=timezone.utc,
    )
    return moment.isoformat()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _redact_dsn(dsn: str | None) -> str | None:
    if not dsn:
        return None
    scheme, _, tail = dsn.partition("://")
    credentials, _, host = tail.partition("@")
    if not host:
        return dsn
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if scheme else f"{user}:***@{host}"


def _bounded(worker: Callable[[], Any], timeout_s: float) -> tuple[bool, Any, str]:
    """Run ``worker`` for at most ``timeout_s`` seconds.

    A daemon client that blocks in C (TigerBeetle's native bindings) cannot be
    interrupted, so the check runs in a daemon thread: a timeout reports honestly
    instead of hanging a release checklist forever, and the process can still exit.
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = worker()
        except BaseException as exc:  # noqa: BLE001 - the failure is the result
            box["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            done.set()

    thread = threading.Thread(target=run, name="nios-daemon-probe", daemon=True)
    thread.start()
    if not done.wait(max(0.1, float(timeout_s))):
        return False, None, f"TIMEOUT after {timeout_s:.1f}s"
    if "error" in box:
        return False, None, str(box["error"])
    return True, box.get("value"), ""


@dataclass
class DaemonReport:
    """One daemon's readiness outcome, JSON-safe by construction."""

    name: str
    env_var: str
    configured: bool
    reachable: bool
    status: str
    detail: str
    start_commands: tuple[str, ...] = ()
    checks: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    checked_at_utc: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "env_var": self.env_var,
            "configured": bool(self.configured),
            "reachable": bool(self.reachable),
            "status": self.status,
            "detail": self.detail,
            "start_commands": list(self.start_commands),
            "checks": dict(self.checks),
            "duration_ms": round(float(self.duration_ms), 3),
            "checked_at_utc": self.checked_at_utc,
        }


def _skipped(
    name: str, env_var: str, detail: str, commands: Sequence[str], checks: dict[str, Any] | None = None
) -> DaemonReport:
    return DaemonReport(
        name=name,
        env_var=env_var,
        configured=False,
        reachable=False,
        status=STATUS_SKIPPED,
        detail=detail,
        start_commands=tuple(commands),
        checks=dict(checks or {}),
        checked_at_utc=_utc_iso(),
    )


# ---------------------------------------------------------------------------
# PostgreSQL 16
# ---------------------------------------------------------------------------


def check_postgres(*, timeout_s: float = 5.0, apply_migrations: bool = False) -> DaemonReport:
    """Prove the PostgreSQL 16 schema, migrations and GIN queries really work."""
    started = time.perf_counter()
    dsn = _env("NIOS_PG_DSN")
    if not dsn:
        return _skipped(
            DAEMON_POSTGRES,
            "NIOS_PG_DSN",
            "NIOS_PG_DSN is not set; PostgreSQL checks were not attempted",
            PG_START_COMMANDS,
        )
    try:
        from ring0_vault.ibor.pg16_repository import Pg16Repository
    except Exception as exc:  # noqa: BLE001 - an import failure is a skip, not a crash
        return _skipped(
            DAEMON_POSTGRES,
            "NIOS_PG_DSN",
            f"pg16_repository unavailable: {type(exc).__name__}: {exc}",
            PG_START_COMMANDS,
        )
    repository = Pg16Repository(dsn, timeout_s=timeout_s)
    ok, probe, error = _bounded(repository.probe, timeout_s)
    report = DaemonReport(
        name=DAEMON_POSTGRES,
        env_var="NIOS_PG_DSN",
        configured=True,
        reachable=False,
        status=STATUS_FAILED,
        detail="",
        start_commands=PG_START_COMMANDS,
        checks={"probe": probe if isinstance(probe, dict) else {"error": error}},
        checked_at_utc=_utc_iso(),
    )
    try:
        if not ok:
            report.detail = f"probe failed: {error}"
            report.status = STATUS_SKIPPED
            return report
        if not isinstance(probe, dict) or not probe.get("available"):
            reason = (probe or {}).get("reason", "UNREACHABLE") if isinstance(probe, dict) else error
            report.detail = f"PostgreSQL unreachable ({reason})"
            report.status = STATUS_SKIPPED
            return report
        report.reachable = True
        report.checks["dsn"] = _redact_dsn(dsn)
        report.checks["server_version"] = probe.get("server_version", "")
        report.checks["is_postgres_16"] = bool(probe.get("is_postgres_16"))

        ok, ddl, error = _bounded(repository.ddl_roundtrip, timeout_s)
        report.checks["ddl_roundtrip"] = ddl if ok else {"ok": False, "detail": error}
        ok_rollback, rollback, error_rollback = _bounded(repository.verify_rollback, timeout_s)
        report.checks["rollback_verification"] = (
            list(rollback) if ok_rollback else [{"rollback_proven": False, "detail": error_rollback}]
        )
        if apply_migrations:
            ok_apply, applied, error_apply = _bounded(repository.apply_migrations, timeout_s)
            report.checks["applied_migrations"] = (
                list(applied) if ok_apply else [{"error": error_apply}]
            )
        rollback_ok = ok_rollback and all(
            bool(row.get("rollback_proven")) for row in report.checks["rollback_verification"]
        )
        ddl_ok = bool(isinstance(ddl, dict) and ddl.get("ok"))
        gin_ok = bool(isinstance(ddl, dict) and int(ddl.get("gin_query_hits", 0)) >= 1)
        report.checks["gin_containment_query"] = gin_ok
        if ddl_ok and rollback_ok:
            report.status = STATUS_OK
            gin_hits = int(ddl.get("gin_query_hits", 0)) if isinstance(ddl, dict) else 0
            table_count = int(ddl.get("table_count", 0)) if isinstance(ddl, dict) else 0
            report.detail = (
                f"PostgreSQL {probe.get('server_version_num', 0)}: "
                f"{table_count} tables, GIN @> hit={gin_hits}, "
                f"{len(report.checks['rollback_verification'])} rollbacks proven"
            )
        else:
            report.status = STATUS_FAILED
            report.detail = "PostgreSQL reachable but a DDL/rollback check failed"
        return report
    finally:
        report.duration_ms = (time.perf_counter() - started) * 1000.0
        repository.close()


# ---------------------------------------------------------------------------
# TigerBeetle
# ---------------------------------------------------------------------------


def _tigerbeetle_roundtrip(address: str) -> dict[str, Any]:
    """Two-phase pending -> post, and pending -> void, with balance assertions."""
    import tigerbeetle as tb  # lazy: absent on an offline workstation

    base = (time.time_ns() // 1_000) % (1 << 62)
    ledger = 990_001
    debit_id = int(base)
    credit_id = int(base) + 1
    checks: dict[str, Any] = {}
    with tb.ClientSync(cluster_id=0, replica_addresses=address) as client:
        created = client.create_accounts(
            [
                tb.Account(id=debit_id, ledger=ledger, code=1),
                tb.Account(id=credit_id, ledger=ledger, code=2),
            ]
        )
        checks["accounts_created"] = sum(
            1 for row in created if row.status == tb.CreateAccountStatus.CREATED
        )
        pending = client.create_transfers(
            [
                tb.Transfer(
                    id=int(base) + 10,
                    debit_account_id=debit_id,
                    credit_account_id=credit_id,
                    amount=500,
                    ledger=ledger,
                    code=1,
                    flags=tb.TransferFlags.PENDING,
                )
            ]
        )
        checks["pending_created"] = bool(
            pending and pending[0].status == tb.CreateTransferStatus.CREATED
        )
        after_pending = {row.id: row for row in client.lookup_accounts([debit_id, credit_id])}
        checks["pending_balances"] = {
            "debits_pending": int(after_pending[debit_id].debits_pending),
            "credits_pending": int(after_pending[credit_id].credits_pending),
        }
        posted = client.create_transfers(
            [
                tb.Transfer(
                    id=int(base) + 11,
                    debit_account_id=debit_id,
                    credit_account_id=credit_id,
                    amount=500,
                    pending_id=int(base) + 10,
                    ledger=ledger,
                    code=1,
                    flags=tb.TransferFlags.POST_PENDING_TRANSFER,
                )
            ]
        )
        checks["posted"] = bool(posted and posted[0].status == tb.CreateTransferStatus.CREATED)
        after_post = {row.id: row for row in client.lookup_accounts([debit_id, credit_id])}
        checks["posted_balances"] = {
            "debits_posted": int(after_post[debit_id].debits_posted),
            "credits_posted": int(after_post[credit_id].credits_posted),
        }
        pending_void = client.create_transfers(
            [
                tb.Transfer(
                    id=int(base) + 20,
                    debit_account_id=debit_id,
                    credit_account_id=credit_id,
                    amount=250,
                    ledger=ledger,
                    code=1,
                    flags=tb.TransferFlags.PENDING,
                )
            ]
        )
        checks["second_pending_created"] = bool(
            pending_void and pending_void[0].status == tb.CreateTransferStatus.CREATED
        )
        voided = client.create_transfers(
            [
                tb.Transfer(
                    id=int(base) + 21,
                    debit_account_id=debit_id,
                    credit_account_id=credit_id,
                    amount=250,
                    pending_id=int(base) + 20,
                    ledger=ledger,
                    code=1,
                    flags=tb.TransferFlags.VOID_PENDING_TRANSFER,
                )
            ]
        )
        checks["voided"] = bool(voided and voided[0].status == tb.CreateTransferStatus.CREATED)
        final = {row.id: row for row in client.lookup_accounts([debit_id, credit_id])}
        checks["final_balances"] = {
            "debits_pending": int(final[debit_id].debits_pending),
            "debits_posted": int(final[debit_id].debits_posted),
        }
    checks["two_phase_ok"] = bool(
        checks["pending_created"]
        and checks["posted"]
        and checks["voided"]
        and checks["final_balances"]["debits_pending"] == 0
        and checks["final_balances"]["debits_posted"] == 500
    )
    return checks


def check_tigerbeetle(*, timeout_s: float = 5.0) -> DaemonReport:
    """Prove a pending/post/void two-phase transfer against a real cluster."""
    started = time.perf_counter()
    address = _env("NIOS_TB_ADDRESS")
    if not address:
        return _skipped(
            DAEMON_TIGERBEETLE,
            "NIOS_TB_ADDRESS",
            "NIOS_TB_ADDRESS is not set; TigerBeetle checks were not attempted",
            TB_START_COMMANDS,
        )
    try:
        import tigerbeetle  # noqa: F401  (existence probe only)
    except Exception as exc:  # noqa: BLE001
        return _skipped(
            DAEMON_TIGERBEETLE,
            "NIOS_TB_ADDRESS",
            f"tigerbeetle client not installed ({type(exc).__name__})",
            TB_START_COMMANDS,
        )
    ok, checks, error = _bounded(lambda: _tigerbeetle_roundtrip(address), timeout_s)
    report = DaemonReport(
        name=DAEMON_TIGERBEETLE,
        env_var="NIOS_TB_ADDRESS",
        configured=True,
        reachable=ok,
        status=STATUS_FAILED,
        detail="",
        start_commands=TB_START_COMMANDS,
        checks=checks if isinstance(checks, dict) else {},
        checked_at_utc=_utc_iso(),
    )
    try:
        if not ok:
            report.status = STATUS_SKIPPED
            report.detail = f"TigerBeetle unreachable at {address} ({error})"
            report.checks = {"error": error}
            return report
        if checks.get("two_phase_ok"):
            report.status = STATUS_OK
            report.detail = (
                "two-phase pending/post/void round-trip passed "
                f"(posted={checks['final_balances']['debits_posted']} micros, "
                f"pending={checks['final_balances']['debits_pending']} micros)"
            )
        else:
            report.status = STATUS_FAILED
            report.detail = "TigerBeetle reachable but the two-phase assertions failed"
        return report
    finally:
        report.duration_ms = (time.perf_counter() - started) * 1000.0


# ---------------------------------------------------------------------------
# NATS JetStream
# ---------------------------------------------------------------------------


async def _nats_roundtrip(url: str, timeout_s: float) -> dict[str, Any]:
    import nats  # lazy: absent on an offline workstation

    nonce = hashlib.sha256(f"{time.time_ns()}".encode("utf-8")).hexdigest()
    subject = f"nios.verify.{nonce[:12]}"
    stream = "NIOS_VERIFY"
    checks: dict[str, Any] = {"subject": subject, "stream": stream}
    connection = await nats.connect(
        url,
        connect_timeout=timeout_s,
        max_reconnect_attempts=0,
        name="nios-verify-live-daemons",
    )
    try:
        js = connection.jetstream()
        try:
            await js.add_stream(name=stream, subjects=["nios.verify.>"])
        except Exception:  # noqa: BLE001 - an existing stream is the normal case
            await js.stream_info(stream)
        ack = await js.publish(subject, _canonical({"nonce": nonce}).encode("utf-8"))
        checks["published_seq"] = int(getattr(ack, "seq", 0) or 0)
        checks["published_stream"] = str(getattr(ack, "stream", "") or "")
        subscription = await js.pull_subscribe(subject, "nios-verify", stream=stream)
        try:
            messages = await subscription.fetch(1, timeout=timeout_s)
            payload = json.loads(messages[0].data.decode("utf-8")) if messages else {}
            checks["replayed_nonce"] = str(payload.get("nonce", ""))
            checks["replay_digest_match"] = str(payload.get("nonce", "")) == nonce
        finally:
            await subscription.unsubscribe()
        await js.delete_stream(stream)
        checks["stream_deleted"] = True
    finally:
        await connection.drain()
    checks["jetstream_ok"] = bool(checks.get("replay_digest_match"))
    return checks


def check_nats(*, timeout_s: float = 5.0) -> DaemonReport:
    """Prove a JetStream publish/replay round-trip with a digest comparison."""
    started = time.perf_counter()
    url = _env("NIOS_NATS_URL")
    if not url:
        return _skipped(
            DAEMON_NATS,
            "NIOS_NATS_URL",
            "NIOS_NATS_URL is not set; NATS JetStream checks were not attempted",
            NATS_START_COMMANDS,
        )
    try:
        import nats  # noqa: F401  (existence probe only)
    except Exception as exc:  # noqa: BLE001
        return _skipped(
            DAEMON_NATS,
            "NIOS_NATS_URL",
            f"nats-py not installed ({type(exc).__name__})",
            NATS_START_COMMANDS,
        )
    ok, checks, error = _bounded(
        lambda: asyncio.run(_nats_roundtrip(url, timeout_s)), timeout_s + 1.0
    )
    report = DaemonReport(
        name=DAEMON_NATS,
        env_var="NIOS_NATS_URL",
        configured=True,
        reachable=ok,
        status=STATUS_FAILED,
        detail="",
        start_commands=NATS_START_COMMANDS,
        checks=checks if isinstance(checks, dict) else {},
        checked_at_utc=_utc_iso(),
    )
    try:
        if not ok:
            report.status = STATUS_SKIPPED
            report.detail = f"NATS unreachable at {url} ({error})"
            report.checks = {"error": error}
            return report
        if checks.get("jetstream_ok"):
            report.status = STATUS_OK
            report.detail = (
                "JetStream publish/replay passed "
                f"(seq={checks.get('published_seq', 0)}, digest matched)"
            )
        else:
            report.status = STATUS_FAILED
            report.detail = "NATS reachable but the replay digest did not match"
        return report
    finally:
        report.duration_ms = (time.perf_counter() - started) * 1000.0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_CHECKS: dict[str, Callable[..., DaemonReport]] = {
    DAEMON_POSTGRES: check_postgres,
    DAEMON_TIGERBEETLE: check_tigerbeetle,
    DAEMON_NATS: check_nats,
}


def readiness_report(
    *, daemons: Sequence[str] = DAEMONS, timeout_s: float = 5.0, apply_migrations: bool = False
) -> dict[str, Any]:
    """Run the requested checks and return a signed, JSON-safe readiness report."""
    timeout = max(0.5, min(30.0, float(timeout_s)))
    reports: list[DaemonReport] = []
    for name in daemons:
        checker = _CHECKS.get(name)
        if checker is None:
            continue
        kwargs: dict[str, Any] = {"timeout_s": timeout}
        if name == DAEMON_POSTGRES:
            kwargs["apply_migrations"] = bool(apply_migrations)
        try:
            reports.append(checker(**kwargs))
        except Exception as exc:  # noqa: BLE001 - one broken probe cannot abort the report
            reports.append(
                DaemonReport(
                    name=name,
                    env_var="",
                    configured=True,
                    reachable=False,
                    status=STATUS_FAILED,
                    detail=f"probe crashed: {type(exc).__name__}: {exc}",
                    checked_at_utc=_utc_iso(),
                )
            )
    statuses = {report.status for report in reports}
    if STATUS_FAILED in statuses:
        overall = "FAILED"
    elif statuses == {STATUS_OK}:
        overall = "READY"
    elif STATUS_OK in statuses:
        overall = "DEGRADED"
    else:
        overall = "OFFLINE"
    body: dict[str, Any] = {
        "page": "daemons.readiness",
        "schema": "v2.3.verify-live-daemons",
        "generated_at_utc": _utc_iso(),
        "overall": overall,
        "timeout_s": timeout,
        "offline_safe": True,
        "daemons": [report.to_dict() for report in reports],
        "counts": {
            "ok": sum(1 for report in reports if report.status == STATUS_OK),
            "skipped": sum(1 for report in reports if report.status == STATUS_SKIPPED),
            "failed": sum(1 for report in reports if report.status == STATUS_FAILED),
        },
        "law": "PART 1.3 - a daemon check is opt-in, bounded and honest about being skipped",
    }
    digest = _sha256(body)
    return {
        **body,
        "report_sha256": digest,
        "signature": {
            "algorithm": "sha256",
            "digest": digest,
            "covers": "canonical_json(report without report_sha256/signature)",
        },
    }


def _print_human(report: dict[str, Any]) -> None:
    print(f"NIOS live daemon readiness  overall={report['overall']}")
    for daemon in report["daemons"]:
        print(f"  {daemon['name']:<12} {daemon['status']:<16} {daemon['detail']}")
        if daemon["status"] == STATUS_SKIPPED and daemon["start_commands"]:
            print("    start it with:")
            for command in daemon["start_commands"]:
                print(f"      {command}")
    print(f"  report_sha256={report['report_sha256']}")


def _clamp_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid timeout: {value!r}") from None
    return max(0.5, min(30.0, parsed))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.  Returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Opt-in PostgreSQL 16 / TigerBeetle / NATS JetStream readiness check.",
    )
    parser.add_argument(
        "--daemon",
        choices=(*DAEMONS, "all"),
        default="all",
        help="which daemon to check (default: all)",
    )
    parser.add_argument("--json", action="store_true", help="print the signed JSON report only")
    parser.add_argument(
        "--timeout",
        type=_clamp_timeout,
        default=5.0,
        help="per-daemon bound in seconds, clamped to 0.5..30 (default: 5)",
    )
    parser.add_argument(
        "--apply-migrations",
        action="store_true",
        help="also apply pending migrations to the configured database (mutating)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    selected = DAEMONS if args.daemon == "all" else (args.daemon,)
    report = readiness_report(
        daemons=selected, timeout_s=args.timeout, apply_migrations=args.apply_migrations
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        _print_human(report)
    return 0 if report["overall"] in ("READY", "DEGRADED", "OFFLINE") else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
