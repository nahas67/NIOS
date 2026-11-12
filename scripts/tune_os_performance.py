"""Operator CLI for the NIOS OS & socket low-latency tuning auditor.

Usage
-----
``python scripts/tune_os_performance.py --check``
    Read-only audit.  Never needs Administrator or root: privileged items are
    reported as ``RECOMMENDED`` with the exact PowerShell / ``sysctl`` command.

``python scripts/tune_os_performance.py --apply``
    Apply every setting the current process is allowed to change (affinity,
    priority, ``timeBeginPeriod(1)``, ``sysctl`` when already root).  Items the
    operator still has to run are printed as ``RECOMMENDED``.

``--json``
    Emit the full machine-readable report on stdout and nothing else, so the
    cockpit, CI or a monitoring probe can parse it directly.

Governing sections: ``sec_004`` (ring isolation), ``sec_010`` / ``sec_013``
(latency budgets), ``sec_016`` (operator control surface).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.os_tuning import (  # noqa: E402 - sys.path bootstrap must run first
    APPLIED,
    RECOMMENDED,
    UNAVAILABLE,
    audit_and_tune_os,
)

_STATUS_ORDER = {APPLIED: 0, RECOMMENDED: 1, UNAVAILABLE: 2}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tune_os_performance",
        description=(
            "Audit or apply the NIOS Ring 0/Ring 1 OS and socket low-latency profile."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="read-only audit (default); no Administrator/root required",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="apply every setting this process is permitted to change",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit only the JSON report on stdout"
    )
    parser.add_argument(
        "--ring",
        default="ring1",
        choices=("ring0", "ring1", "ring3"),
        help="ring profile to tune (default: ring1)",
    )
    return parser.parse_args(argv)


def _render(report: dict) -> str:
    lines: list[str] = []
    lines.append(
        f"NIOS OS low-latency audit - {report['platform']} {report['platform_release']} "
        f"({report['profile']})"
    )
    lines.append(
        f"mode={'APPLY' if report['apply_requested'] else 'CHECK'} "
        f"applied={report['applied_count']} recommended={report['recommended_count']} "
        f"unavailable={len(report['unavailable'])} errors={report['error_count']}"
    )
    lines.append("")
    for check in sorted(report["checks"], key=lambda item: _STATUS_ORDER.get(item["status"], 3)):
        lines.append(f"[{check['status']:<12}] {check['id']:<30} {check['detail']}")
        if check["status"] == RECOMMENDED and check.get("command"):
            lines.append(f"{'':<15} -> {check['command']}")
    socket_report = report.get("socket", {})
    lines.append("")
    lines.append(
        "socket: "
        f"TCP_NODELAY={socket_report.get('tcp_nodelay')} "
        f"SO_KEEPALIVE={socket_report.get('so_keepalive')} "
        f"rcvbuf={socket_report.get('recv_buffer_bytes')} "
        f"sndbuf={socket_report.get('send_buffer_bytes')} "
        f"ok={socket_report.get('ok')}"
    )
    for entry in report.get("transports", {}).get("entries", []):
        lines.append(
            f"transport {entry['name']}: "
            f"{'OK' if entry.get('low_latency_enforced') else 'GAP'} ({entry.get('module')})"
        )
    lines.append("")
    lines.append(
        "RESULT: "
        + (
            "OS profile fully applied"
            if report["tuned"]
            else "no errors; RECOMMENDED items remain (see commands above)"
        )
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = audit_and_tune_os(apply=bool(args.apply), ring=str(args.ring))
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render(report) + "\n")
    return 0 if report["error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
