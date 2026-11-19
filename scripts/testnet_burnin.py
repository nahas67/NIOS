"""Opt-in Binance **testnet** burn-in (``PART 1.3``).

A sandbox verifier that only runs when an operator asks for it **and** provides
testnet credentials.  It answers one question that a mock cannot: does the real
transport still work -- TLS, HMAC signing, the exchange's own rate-limit header,
and a live ``listenKey`` lifecycle?

Nothing here touches mainnet and nothing here runs during ``pytest`` or
``scripts/run_nios.py --smoke-test``: without ``--venue binance-testnet`` and
without ``NIOS_BINANCE_TESTNET_KEY`` / ``NIOS_BINANCE_TESTNET_SECRET`` the script
prints a skip and exits ``0``.

Checks
------
1. **TLS** -- a verified handshake with the testnet host (protocol, cipher,
   certificate expiry), using the platform trust store.
2. **HMAC-SHA256 signing** -- the exact Binance query-string/signature routine,
   cross-checked against an independent RFC 2104 construction and the RFC 4231
   known-answer vector.
3. **``X-MBX-USED-WEIGHT``** -- a real response header parsed into
   ``(interval, used, limit_hint)`` instead of being ignored.
4. **``listenKey``** -- create, keepalive, then close, so the burn-in leaves no
   dangling user-data stream.
5. **``to_micros()`` precision** -- the Ring 0 and Ring 2 conversions must agree
   bit-for-bit on boundary values (``0.1``, ``1.0000005``, ``0.0000025`` ...).

Exit codes
----------
0   skipped (no opt-in / no credentials) or every check passed
1   a check failed against a reachable testnet
2   invalid arguments

Usage::

    python scripts/testnet_burnin.py --venue binance-testnet
    python scripts/testnet_burnin.py --venue binance-testnet --json --timeout 10
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(ROOT))

__all__ = [
    "BASE_URL",
    "VENUE_BINANCE_TESTNET",
    "BurnInReport",
    "run_burnin",
    "main",
]

VENUE_BINANCE_TESTNET = "binance-testnet"

#: Spot testnet.  Never ``api.binance.com``: a burn-in must not be able to place
#: a real order by accident.
BASE_URL = "https://testnet.binance.vision"
BASE_HOST = "testnet.binance.vision"

#: RFC 4231 test case 1 -- HMAC-SHA256 over a known key/message pair.
_RFC4231_KEY = bytes([0x0B]) * 20
_RFC4231_DATA = b"Hi There"
_RFC4231_DIGEST = "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"

#: Values whose micro conversion is a known trap (float representation, not math).
_MICROS_VECTORS: tuple[float, ...] = (
    0.0,
    0.1,
    0.000001,
    0.0000025,
    1.0,
    1.0000005,
    95_000.42,
    0.30000000000000004,
)


def _utc_iso(stamp_ns: int | None = None) -> str:
    return datetime.fromtimestamp(
        (stamp_ns if stamp_ns is not None else time.time_ns()) / 1_000_000_000,
        tz=timezone.utc,
    ).isoformat()


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


# ---------------------------------------------------------------------------
# Independent HMAC (RFC 2104), used to cross-check the stdlib result
# ---------------------------------------------------------------------------


def _manual_hmac_sha256(key: bytes, message: bytes) -> str:
    """HMAC exactly as RFC 2104 defines it, with no ``hmac`` module involved.

    Using the same library to check itself proves nothing; constructing the
    inner/outer pads by hand is a genuinely independent implementation of the
    construction the venue relies on.
    """
    block = 64
    if len(key) > block:
        key = hashlib.sha256(key).digest()
    key = key.ljust(block, b"\x00")
    inner = bytes(byte ^ 0x36 for byte in key) + message
    outer = bytes(byte ^ 0x5C for byte in key) + hashlib.sha256(inner).digest()
    return hashlib.sha256(outer).hexdigest()


def binance_signature(query_string: str, secret: str) -> str:
    """The signature Binance expects: lowercase hex HMAC-SHA256 of the query."""
    return hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()


def check_signing() -> dict[str, Any]:
    """Known-answer HMAC vector plus the venue's query-string signing routine."""
    rfc = hmac.new(_RFC4231_KEY, _RFC4231_DATA, hashlib.sha256).hexdigest()
    manual = _manual_hmac_sha256(_RFC4231_KEY, _RFC4231_DATA)
    query = urllib.parse.urlencode(
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": "0.001",
            "price": "10000.00",
            "recvWindow": "5000",
            "timestamp": "1700000000000",
        }
    )
    secret = "nios-testnet-signing-probe"
    signature = binance_signature(query, secret)
    return {
        "rfc4231_vector_ok": rfc == _RFC4231_DIGEST,
        "manual_hmac_matches_stdlib": manual == rfc,
        "query_string": query,
        "signature": signature,
        "signature_length": len(signature),
        "signature_lowercase_hex": signature == signature.lower()
        and all(char in "0123456789abcdef" for char in signature),
        "ok": bool(
            rfc == _RFC4231_DIGEST
            and manual == rfc
            and len(signature) == 64
            and signature == signature.lower()
        ),
    }


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------


def check_tls(*, timeout_s: float) -> dict[str, Any]:
    """A verified TLS handshake against the testnet host."""
    context = ssl.create_default_context()
    with socket.create_connection((BASE_HOST, 443), timeout=timeout_s) as raw:
        with context.wrap_socket(raw, server_hostname=BASE_HOST) as tls:
            certificate = tls.getpeercert() or {}
            not_after = certificate.get("notAfter", "")
            expires_at = None
            if not_after:
                expires_at = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=timezone.utc
                )
            return {
                "ok": bool(tls.version() and expires_at and expires_at > datetime.now(timezone.utc)),
                "host": BASE_HOST,
                "protocol": tls.version(),
                "cipher": (tls.cipher() or ("", "", 0))[0],
                "certificate_not_after": expires_at.isoformat() if expires_at else not_after,
                "certificate_subject": str(certificate.get("subject", "")),
                "verified": True,
            }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http(
    method: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    api_key: str | None = None,
    timeout_s: float,
) -> tuple[int, dict[str, Any], Mapping[str, str]]:
    query = urllib.parse.urlencode([(key, value) for key, value in (params or {}).items()])
    url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
    request = urllib.request.Request(url, method=method.upper())
    if api_key:
        request.add_header("X-MBX-APIKEY", api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, _decode(body), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, _decode(body), {k.lower(): v for k, v in (exc.headers or {}).items()}


def _decode(body: str) -> dict[str, Any]:
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        return {"raw": body[:400]}
    return payload if isinstance(payload, dict) else {"payload": payload}


def parse_used_weight(headers: Mapping[str, str]) -> dict[str, Any]:
    """Parse Binance's ``X-MBX-USED-WEIGHT[-<interval>]`` family of headers.

    The venue ships several spellings (``-1M``, ``-5M``, bare) and a burn-in that
    only looks for one of them silently reports "no rate-limit telemetry" while
    the header is right there.
    """
    parsed: dict[str, Any] = {"intervals": {}, "raw": {}}
    for key, value in headers.items():
        lowered = key.lower()
        if not lowered.startswith("x-mbx-used-weight"):
            continue
        parsed["raw"][lowered] = value
        suffix = lowered[len("x-mbx-used-weight") :].lstrip("-").upper()
        try:
            used = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        parsed["intervals"][suffix or "DEFAULT"] = used
    totals = list(parsed["intervals"].values())
    parsed["used"] = max(totals) if totals else None
    parsed["ok"] = bool(parsed["intervals"])
    return parsed


# ---------------------------------------------------------------------------
# listenKey lifecycle
# ---------------------------------------------------------------------------


def check_listen_key(*, api_key: str, timeout_s: float) -> dict[str, Any]:
    """Create, keepalive and close a user-data ``listenKey``."""
    status, created, _ = _http("POST", "/api/v3/userDataStream", api_key=api_key, timeout_s=timeout_s)
    listen_key = str(created.get("listenKey", "")) if isinstance(created, dict) else ""
    report: dict[str, Any] = {
        "create_status": status,
        "created": bool(status == 200 and listen_key),
        "listen_key_length": len(listen_key),
    }
    if not listen_key:
        report.update({"ok": False, "detail": created})
        return report
    status_keepalive, keepalive, _ = _http(
        "PUT",
        "/api/v3/userDataStream",
        params={"listenKey": listen_key},
        api_key=api_key,
        timeout_s=timeout_s,
    )
    report["keepalive_status"] = status_keepalive
    report["keepalive_ok"] = bool(status_keepalive == 200)
    status_close, closed, _ = _http(
        "DELETE",
        "/api/v3/userDataStream",
        params={"listenKey": listen_key},
        api_key=api_key,
        timeout_s=timeout_s,
    )
    report["close_status"] = status_close
    report["closed"] = bool(status_close == 200)
    report["keepalive_payload"] = keepalive if isinstance(keepalive, dict) else {}
    report["ok"] = bool(report["created"] and report["keepalive_ok"] and report["closed"])
    return report


# ---------------------------------------------------------------------------
# to_micros precision
# ---------------------------------------------------------------------------


def check_micros_precision() -> dict[str, Any]:
    """Ring 0 and Ring 2 must convert units to micros identically, bit for bit."""
    from ring0_vault.ibor.ledger_outbox import to_micros as ring0_micros
    from ring2_brain.quant.bar_aggregator import to_micros as ring2_micros

    rows: list[dict[str, Any]] = []
    matches = True
    for value in _MICROS_VECTORS:
        left = int(ring0_micros(value))
        right = int(ring2_micros(value))
        matches = matches and left == right
        rows.append(
            {
                "units": value,
                "ring0_micros": left,
                "ring2_micros": right,
                "match": left == right,
            }
        )
    # Floor-with-epsilon, not half-up: a debit may never be rounded up.
    floor_checks = {
        "0.1": int(ring0_micros(0.1)) == 100_000,
        "1.0000005": int(ring0_micros(1.0000005)) == 1_000_000,
        "0.0000025": int(ring0_micros(0.0000025)) == 2,
        "0.3_repr": int(ring0_micros(0.30000000000000004)) == 300_000,
    }
    # A round trip back to units stays inside one micro of the original value.
    round_trip_ok = all(
        abs((row["ring0_micros"] / 1_000_000) - float(row["units"])) <= 1e-6 for row in rows
    )
    return {
        "ok": bool(matches and all(floor_checks.values()) and round_trip_ok),
        "vectors": rows,
        "floor_checks": floor_checks,
        "round_trip_within_one_micro": round_trip_ok,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class BurnInReport:
    """One burn-in run.  ``skipped`` reports never claim a check passed."""

    venue: str
    skipped: bool
    reason: str
    checks: dict[str, Any] = field(default_factory=dict)
    started_at_utc: str = ""
    finished_at_utc: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "page": "testnet.burnin",
            "schema": "v2.3.testnet-burnin",
            "venue": self.venue,
            "skipped": bool(self.skipped),
            "reason": self.reason,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "duration_ms": round(float(self.duration_ms), 3),
            "checks": dict(self.checks),
            "never_runs_under_pytest": True,
            "mainnet_reachable": False,
        }
        body["all_passed"] = bool(
            not self.skipped
            and self.checks
            and all(self._check_ok(item) for item in self.checks.values())
        )
        body["report_sha256"] = _sha256(body)
        return body

    @staticmethod
    def _check_ok(item: Any) -> bool:
        if isinstance(item, Mapping):
            if "ok" in item:
                return bool(item["ok"])
            # ``used_weight`` carries its verdict under ``ok`` as well.
            return False
        return False


def run_burnin(
    *,
    venue: str,
    timeout_s: float = 10.0,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> BurnInReport:
    """Run the requested venue's burn-in, or return a clean skip."""
    started_ns = time.time_ns()
    report = BurnInReport(
        venue=str(venue),
        skipped=True,
        reason="NOT_REQUESTED",
        started_at_utc=_utc_iso(started_ns),
    )
    if str(venue) != VENUE_BINANCE_TESTNET:
        report.reason = f"venue {venue!r} is not a supported burn-in target"
        report.finished_at_utc = _utc_iso()
        return report
    key = api_key if api_key is not None else _env("NIOS_BINANCE_TESTNET_KEY")
    secret = api_secret if api_secret is not None else _env("NIOS_BINANCE_TESTNET_SECRET")
    if not key or not secret:
        missing = [
            name
            for name, value in (
                ("NIOS_BINANCE_TESTNET_KEY", key),
                ("NIOS_BINANCE_TESTNET_SECRET", secret),
            )
            if not value
        ]
        report.reason = f"missing credentials: {', '.join(missing)}"
        report.finished_at_utc = _utc_iso()
        return report

    timeout = max(1.0, min(30.0, float(timeout_s)))
    report.skipped = False
    report.reason = "RUN"
    checks: dict[str, Any] = {}
    try:
        checks["micros_precision"] = check_micros_precision()
        checks["signing"] = check_signing()
        try:
            checks["tls"] = check_tls(timeout_s=timeout)
        except Exception as exc:  # noqa: BLE001 - the failure is the result
            checks["tls"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        # Server time is unsigned and cheap: it is the transport + header probe.
        status, payload, headers = _http("GET", "/api/v3/time", timeout_s=timeout)
        used_weight = parse_used_weight(headers)
        checks["clock"] = {
            "ok": bool(status == 200 and payload.get("serverTime")),
            "status": status,
            "server_time_ms": int(payload.get("serverTime", 0) or 0),
            "local_drift_ms": abs(
                int(payload.get("serverTime", 0) or 0) - time.time_ns() // 1_000_000
            ),
        }
        checks["used_weight"] = used_weight
        checks["listen_key"] = check_listen_key(api_key=key, timeout_s=timeout)
    except Exception as exc:  # noqa: BLE001 - a transport failure is a failed check
        checks["transport"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    report.checks = checks
    report.finished_at_utc = _utc_iso()
    report.duration_ms = (time.time_ns() - started_ns) / 1_000_000
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.  Returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Opt-in Binance testnet burn-in (TLS, HMAC, rate-limit header, listenKey).",
    )
    parser.add_argument(
        "--venue",
        choices=(VENUE_BINANCE_TESTNET, "none"),
        default="none",
        help="burn-in target; 'none' (default) prints a clean skip",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="per-request bound in seconds, clamped to 1..30 (default: 10)",
    )
    parser.add_argument("--json", action="store_true", help="print the JSON report only")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run_burnin(venue=args.venue, timeout_s=args.timeout)
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    elif payload["skipped"]:
        print(f"SKIPPED: {payload['venue']} burn-in was not run ({payload['reason']}).")
        print("  opt in with --venue binance-testnet and set")
        print("  NIOS_BINANCE_TESTNET_KEY / NIOS_BINANCE_TESTNET_SECRET.")
    else:
        print(f"Binance testnet burn-in: all_passed={payload['all_passed']}")
        for name, check in payload["checks"].items():
            verdict = "OK" if isinstance(check, dict) and check.get("ok") else "FAIL"
            print(f"  {name:<16} {verdict}")
        print(f"  report_sha256={payload['report_sha256']}")
    if payload["skipped"]:
        return 0
    return 0 if payload["all_passed"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
