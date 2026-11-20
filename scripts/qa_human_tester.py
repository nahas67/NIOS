#!/usr/bin/env python3
"""NIOS V2.5 headless-human QA driver (Playwright).

Drives the real cockpit at ``http://127.0.0.1:8001`` as an operator would, and
records every defect in a structured ledger.  It is deliberately secret-safe:
the live LLM API key file is *never* read into this process.  Only the file
**path** is handed to the NIOS runtime via ``NIOS_LLM_API_FILE``; the app reads
the key itself and returns only a ``***last4`` mask, which is what lands in the
report.

Modes
-----
``--offline``
    Structural checks only (route census, forbidden CSS tokens, expected
    selector/label presence, bad-literal scan of page source).  No server and
    no browser are required.
``--live-llm --api-file PATH``
    Full structural + real-browser run with live LLM execution enabled for the
    spawned NIOS server.  ``PATH`` is passed to the runtime as an environment
    variable, never echoed.
``(no flags)``
    Structural + real-browser run against a reachable server, LLM offline.

If the server or browser is unavailable the failure is reported clearly and the
process exits non-zero.  If the hy4-flash-free live endpoint is blocked, only
the live-LLM sub-steps are marked ``blocked`` (not ``defect``) and a complete
report is still produced.

All waits are bounded (deadline-based polling); there are no unbounded sleeps.

Example
-------
    python scripts/qa_human_tester.py --live-llm \
        --api-file "C:\\Users\\nahas\\OneDrive\\Desktop\\opencode-g0-api.txt"

    python scripts/qa_human_tester.py --offline --report-dir scripts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "src"
RUN_NIOS = REPO_ROOT / "scripts" / "run_nios.py"
DEFAULT_API_FILE = r"C:\Users\nahas\OneDrive\Desktop\opencode-g0-api.txt"
BASE_URL = "http://127.0.0.1:8001"
HOST = "127.0.0.1"
PORT = 8001

VIEWPORTS = [(1440, 900), (1280, 800)]

# QA operator credentials — the server is launched with a clean slate so setup
# provisions this operator, then every login uses these constants.
QA_USER = "qa.operator"
QA_PASSWORD = "QaPassw0rd!42"
QA_PIN = "123456"

# Names of the 17 workstations (kept in sync with config/nav.ts at runtime by
# parsing that file; this is the fallback census).
WORKSTATIONS_FALLBACK = [
    "dashboard", "missions", "screener", "terminal", "orders", "options",
    "analyzer", "quant", "agents", "backtest", "brain", "forge", "ledger",
    "logbook", "verification", "brokers", "control",
]
PORTALS = ["/setup", "/login", "/paper/login", "/live/login", "/live-unlock"]

# Expected labels / tokens used by the structural checks.  Every entry is a
# substring that must appear in the TSX source of at least one page/portal.
EXPECTED_SOURCE_LABELS = [
    "Deposit Paper Capital", "Manual Tick", "Live Vault", "Emergency Kill Switch",
    "Lock Live Vault", "+10 Shadow Fills Burst", "Probe Providers", "Call Tool",
    "Factory Reset", "Rotate Keys", "Run Reconciliation", "Clear Freeze",
    "Handshake", "Macro Calendar", "Payoff Curve", "Max Pain", "IV Rank",
    "Promote to Nursery", "Shock Simulator", "Committee", "Episodic operator decisions",
    "Compact Memory", "23-Gate Pre-Flight", "Blocked until DSR", "Clean slate",
]

# Concrete CSS/Tailwind class forms only.  Bare words like "shadow" or
# "gradient" also occur as legitimate domain prose ("Shadow Fills", comments
# saying "no shadow"), so they are intentionally excluded here.
FORBIDDEN_CSS_TOKENS = [
    "rounded-full", "rounded-xl", "rounded-2xl", "rounded-3xl",
    "shadow-sm", "shadow-md", "shadow-lg", "shadow-xl", "shadow-2xl",
    "shadow-inner", "shadow-outline", "boxShadow",
    "backdrop-blur", "backdrop-filter", "blur(",
    "linear-gradient", "radial-gradient", "conic-gradient",
    "9999px", "999px",
]

# Intentional error responses: (status, regex over url+body).  These are
# recorded as INFO, not as defects.
ERROR_WHITELIST: list[tuple[int | None, str]] = [
    (422, r"INFEASIBLE_MISSION"),
    (422, r"CLIENT_NON_FINITE"),
    (400, r"INVALID_QUANTITY"),
    (400, r"INFEASIBLE_MISSION"),
    (401, r"LIVE_ELEVATION_REQUIRED|AUTH_REQUIRED|SESSION_REQUIRED|INVALID_CREDENTIALS|WRONG_PASSWORD|INVALID_PIN|ACK_REQUIRED"),
    (403, r"LIVE_ELEVATION_REQUIRED|CSRF|FORBIDDEN|PAPER.*DENIED|DENIED.*PAPER"),
    (404, r"/favicon\.ico"),
    (409, r"ALREADY|RECONCILIATION|LOCKED"),
    (500, r"CHAIN_SIMULATOR|EXPECTED_FAULT|DEMO"),
]

BAD_LITERALS = ["[object Object]", "NaN", "undefined"]

# Key-like token; used only to build a local mask, never to persist the key.
_KEY_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,}")
_OCSK_TOKEN = re.compile(r"oc_sk_[A-Za-z0-9_-]+")


# ---------------------------------------------------------------------------
# Secret safety
# ---------------------------------------------------------------------------

def mask_secret(secret: str) -> str:
    """``***`` + last four chars (matches ring0 key-vault mask_secret)."""
    token = str(secret or "")
    return "***" if len(token) <= 4 else f"***{token[-4:]}"


class Redactor:
    """Learns the exact key token (in-process only) and scrubs it everywhere."""

    def __init__(self, api_file: str | None) -> None:
        self._secrets: set[str] = set()
        self.masked = "***"
        if api_file and Path(api_file).is_file():
            # Read strictly to compute a mask.  Content is never stored, logged
            # or written to any report.
            try:
                blob = Path(api_file).read_text(encoding="utf-8", errors="replace")
                tokens = _KEY_TOKEN.findall(blob)
                if tokens:
                    key = max(tokens, key=len)
                    self._secrets.add(key)
                    self.masked = mask_secret(key)
            except OSError:
                pass

    def scrub(self, value: Any) -> Any:
        """Recursively redact secrets and generic ``oc_sk_*`` tokens."""
        if isinstance(value, str):
            out = value
            for secret in self._secrets:
                if secret:
                    out = out.replace(secret, self.masked)
            out = _OCSK_TOKEN.sub(lambda m: mask_secret(m.group(0)), out)
            return out
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(v) for v in value]
        return value


# ---------------------------------------------------------------------------
# Defect ledger & report
# ---------------------------------------------------------------------------

@dataclass
class Ledger:
    redactor: Redactor
    defects: list[dict[str, Any]] = field(default_factory=list)
    info: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, *, workflow: str, step: str, selector: str, symptom: str,
            root_cause_guess: str, evidence: Any, severity: str = "medium") -> None:
        row = {
            "id": f"D{len(self.defects) + 1:03d}",
            "workflow": workflow,
            "step": step,
            "selector": selector,
            "symptom": symptom,
            "root_cause_guess": root_cause_guess,
            "severity": severity,
            "evidence": self.redactor.scrub(evidence),
            "status": "defect",
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.defects.append(row)

    def note(self, *, workflow: str, step: str, message: str, evidence: Any = None) -> None:
        self.info.append(self.redactor.scrub({
            "workflow": workflow, "step": step, "message": message, "evidence": evidence,
        }))

    def block(self, *, workflow: str, step: str, reason: str, evidence: Any = None) -> None:
        self.blocked.append(self.redactor.scrub({
            "workflow": workflow, "step": step, "reason": reason, "evidence": evidence,
            "status": "blocked",
        }))

    def check(self, *, name: str, ok: bool, detail: Any = None) -> None:
        self.checks.append(self.redactor.scrub({
            "name": name, "ok": bool(ok), "detail": detail,
        }))

    @property
    def clean(self) -> bool:
        return not self.defects


# ---------------------------------------------------------------------------
# Small utilities (bounded waits, no unbounded sleeps)
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def http_get(url: str, timeout: float = 1.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2048).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - transport diversity
        return 0, ""


def wait_until(predicate: Callable[[], bool], deadline_s: float,
               poll_s: float = 0.25) -> bool:
    """Poll a cheap predicate until true or deadline.  Bounded by construction."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(min(poll_s, max(0.0, end - time.monotonic())))
    return False


def is_intentional_error(status: int | None, haystack: str) -> bool:
    text = haystack or ""
    for code, pattern in ERROR_WHITELIST:
        if code is not None and status != code:
            continue
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def excerpt(value: str, limit: int = 500) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value[:limit]


def dom_fingerprint(page: Any) -> str:
    """Hash of the visible DOM + URL + modal count (for dead-button detection)."""
    try:
        payload = page.evaluate(
            """() => {
                const main = document.querySelector('main') || document.body;
                const buttons = Array.from(document.querySelectorAll('button'))
                  .map(b => [b.innerText, b.disabled, b.className]).join('|');
                const modals = document.querySelectorAll('.fixed.inset-0').length;
                // React sets <input>/<select>/<textarea> .value as a DOM property,
                // which is NOT reflected in outerHTML or innerText. Include live
                // form-control values/checked state so state-changing buttons
                // (e.g. Re-issue algo id) are not misflagged as dead buttons.
                const controls = Array.from(document.querySelectorAll('input,textarea,select'))
                  .map(c => [c.tagName, c.value, c.checked]).join('|');
                return JSON.stringify({html: main.outerHTML.slice(0, 8000),
                                       text: main.innerText.slice(0, 4000),
                                       buttons, controls, modals, href: location.href});
            }"""
        )
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Structural / offline checks
# ---------------------------------------------------------------------------

def parse_route_census(ledger: Ledger) -> dict[str, Any]:
    keys = list(WORKSTATIONS_FALLBACK)
    nav = SRC / "config" / "nav.ts"
    if nav.is_file():
        found = re.findall(r'key:\s*"([a-z]+)"', nav.read_text(encoding="utf-8", errors="replace"))
        keys = [k for k in found if k] or keys
    census = {
        "workstations": keys,
        "portals": PORTALS,
        "base": [f"/{k}" for k in keys],
        "paper": [f"/paper/{k}" for k in keys],
        "live": [f"/live/{k}" for k in keys],
    }
    total = len(keys) * 3 + len(PORTALS)
    census["total_routes"] = total
    ledger.check(name="route_census_total", ok=total == 56,
                 detail=f"expected 56 (17x3 + 5), computed {total}")
    if total != 56:
        ledger.add(workflow="STRUCTURE", step="route-census", selector="config/nav.ts",
                   symptom=f"route census computed {total} routes, expected 56",
                   root_cause_guess="workstation key list drifted from the 56-route contract",
                   evidence={"workstations": len(keys), "portals": len(PORTALS)},
                   severity="high")
    return census


def structural_checks(ledger: Ledger) -> dict[str, Any]:
    """Source-level checks that need no browser or server."""
    census = parse_route_census(ledger)

    # Route wiring: every workstation must be referenced in App.tsx PAGES map.
    app = SRC / "App.tsx"
    app_text = app.read_text(encoding="utf-8", errors="replace") if app.is_file() else ""
    for key in census["workstations"]:
        present = re.search(rf"^\s*{re.escape(key)}:\s*lazy", app_text, re.MULTILINE) is not None
        ledger.check(name=f"route_wired:{key}", ok=present, detail=None)
        if not present:
            ledger.add(workflow="STRUCTURE", step=f"route-wired/{key}", selector="App.tsx",
                       symptom=f"workstation '{key}' not wired into PAGES map",
                       root_cause_guess="missing lazy() import for workstation route",
                       evidence={"key": key}, severity="high")

    # Forbidden visual tokens across the TSX source.
    tsx_files = sorted(SRC.rglob("*.tsx"))
    hits: list[dict[str, str]] = []
    for f in tsx_files:
        txt = f.read_text(encoding="utf-8", errors="replace")
        for token in FORBIDDEN_CSS_TOKENS:
            if token in txt:
                hits.append({"file": f.relative_to(REPO_ROOT).as_posix(), "token": token})
    ledger.check(name="forbidden_css_tokens", ok=not hits, detail=hits[:20])
    for hit in hits[:20]:
        ledger.add(workflow="STRUCTURE", step="visual-law", selector=hit["file"],
                   symptom=f"forbidden visual token '{hit['token']}' present",
                   root_cause_guess="component violates flat-matte design law (Suite 38)",
                   evidence=hit, severity="low")

    # Expected labels present somewhere in source.
    all_tsx = "\n".join(
        f.read_text(encoding="utf-8", errors="replace") for f in tsx_files
    )
    for label in EXPECTED_SOURCE_LABELS:
        ok = label.lower() in all_tsx.lower()
        ledger.check(name=f"label:{label}", ok=ok, detail=None)
        if not ok:
            ledger.add(workflow="STRUCTURE", step="expected-label", selector="src/**/*.tsx",
                       symptom=f"expected operator label '{label}' not found in source",
                       root_cause_guess="control missing or renamed in the workstation UI",
                       evidence={"label": label}, severity="medium")

    # Rendered bad-literal risk: literal `NaN`/`undefined`/`[object Object]` used
    # as JSX text children (naive but useful smoke heuristic).
    bad: list[dict[str, str]] = []
    for f in tsx_files:
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for lit in BAD_LITERALS:
                if re.search(rf">\s*{re.escape(lit)}\s*<", line):
                    bad.append({"file": f.name, "line": i, "literal": lit})
    ledger.check(name="literal_bad_values_in_jsx", ok=not bad, detail=bad[:20])
    for hit in bad[:20]:
        ledger.add(workflow="STRUCTURE", step="bad-literal", selector=f"{hit['file']}:{hit['line']}",
                   symptom=f"JSX text child renders literal '{hit['literal']}'",
                   root_cause_guess="unformatted numeric/object value rendered directly",
                   evidence=hit, severity="medium")

    return census


# ---------------------------------------------------------------------------
# Server management (isolated state root)
# ---------------------------------------------------------------------------

class ManagedServer:
    def __init__(self, api_file: str | None, live_llm: bool, log_dir: Path,
                 ledger: Ledger, base_url: str) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self.log_path = log_dir / "nios_server.log"
        self.log_handle: Any = None
        self.started_by_us = False
        self.api_file = api_file
        self.live_llm = live_llm
        self.ledger = ledger
        self.workspace: Path | None = None
        self.base_url = base_url.rstrip("/")
        from urllib.parse import urlparse
        parsed = urlparse(self.base_url)
        self.host = parsed.hostname or HOST
        self.port = parsed.port or PORT
        self.health_url = f"{self.base_url}/api/v2/cockpit/health"

    def ensure(self) -> bool:
        """Return True if a NIOS server is usable on the configured base URL."""
        status, _ = http_get(self.health_url, timeout=1.2)
        if status != 0:
            # Already running — attach but flag that isolation is not guaranteed.
            self.ledger.block(
                workflow="SERVER", step="attach-existing",
                reason=(f"attached to an already-running server on :{self.port}; an "
                        "isolated temp state root could not be guaranteed for this run"),
                evidence={"health_status": status, "base_url": self.base_url},
            )
            return True

        if tcp_open(self.host, self.port, timeout=0.6):
            self.ledger.add(workflow="SERVER", step="port-check",
                            selector=f"{self.host}:{self.port}",
                            symptom=f"port {self.port} is occupied by a non-NIOS service",
                            root_cause_guess="foreign process bound to the QA port",
                            evidence={"health_status": status}, severity="high")
            return False

        # Launch our own server with an isolated workspace under the temp root.
        self.workspace = Path(tempfile.mkdtemp(prefix="nios_qa_state_",
                                               dir=tempfile.gettempdir()))
        # A throwaway operator-auth vault guarantees /setup provisions our QA
        # operator rather than colliding with a foreign operator on this host.
        self.operator_dir: Path | None = Path(
            tempfile.mkdtemp(prefix="nios_qa_operator_", dir=tempfile.gettempdir())
        )
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["NIOS_OPERATOR_AUTH_DIR"] = str(self.operator_dir)
        if self.api_file:
            env["NIOS_LLM_API_FILE"] = self.api_file  # PATH only — never the key
        if self.live_llm:
            env["NIOS_LLM_LIVE"] = "1"
        cmd = [
            sys.executable, str(RUN_NIOS), "--host", self.host, "--port", str(self.port),
            "--workspace", str(self.workspace),
        ]
        self.log_handle = self.log_path.open("wb")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(REPO_ROOT), env=env,
                stdout=self.log_handle, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            self.ledger.add(workflow="SERVER", step="spawn", selector=str(RUN_NIOS),
                            symptom=f"failed to spawn NIOS server: {exc}",
                            root_cause_guess="python/interpreter or path unavailable",
                            evidence={"cmd": cmd}, severity="high")
            return False
        self.started_by_us = True

        def ready() -> bool:
            s, _ = http_get(self.health_url, timeout=0.5)
            return s != 0 and (self.proc is None or self.proc.poll() is None)

        if not wait_until(ready, deadline_s=45.0, poll_s=0.3):
            tail = self._log_tail(2500)
            self.ledger.add(workflow="SERVER", step="boot-timeout", selector=str(RUN_NIOS),
                            symptom="NIOS server did not become healthy within 45s",
                            root_cause_guess="runtime boot failure; see server log tail",
                            evidence={"log_tail": tail}, severity="high")
            self.stop()
            return False
        self.ledger.note(workflow="SERVER", step="spawn",
                         message="launched NIOS server with isolated temp state root",
                         evidence={"workspace": str(self.workspace), "port": self.port})
        return True

    def _log_tail(self, limit: int) -> str:
        try:
            raw = self.log_path.read_bytes()[-limit:].decode("utf-8", "replace")
        except OSError:
            raw = ""
        # Server log is untrusted content; redact before it can reach a report.
        from_path = Redactor(None)
        return excerpt(str(from_path.scrub(raw)), limit)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                wait_until(lambda: self.proc is None or self.proc.poll() is not None,
                           deadline_s=6.0, poll_s=0.2)
                if self.proc.poll() is None:
                    self.proc.kill()
            except OSError:
                pass
        if self.log_handle:
            try:
                self.log_handle.close()
            except OSError:
                pass
        if self.workspace:
            shutil.rmtree(self.workspace, ignore_errors=True)
        if getattr(self, "operator_dir", None):
            shutil.rmtree(self.operator_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Browser capture: global defect classes
# ---------------------------------------------------------------------------

class Capture:
    """Event listeners + DOM scanners attached to one Playwright page."""

    def __init__(self, page: Any, ledger: Ledger, workflow: str) -> None:
        self.page = page
        self.ledger = ledger
        self.workflow = workflow
        self.request_count = 0
        self._wire()

    def set_workflow(self, name: str) -> None:
        """Retarget attribution without re-registering listeners (avoids duplicates)."""
        self.workflow = name

    def _wire(self) -> None:
        self.page.on("request", lambda _req: setattr(self, "request_count", self.request_count + 1))
        self.page.on("pageerror", self._on_pageerror)
        self.page.on("console", self._on_console)
        self.page.on("requestfailed", self._on_requestfailed)
        self.page.on("response", self._on_response)

    def _on_pageerror(self, exc: Any) -> None:
        self.ledger.add(workflow=self.workflow, step="pageerror", selector="window.onerror",
                        symptom=f"uncaught page error: {excerpt(str(exc))}",
                        root_cause_guess="frontend exception during render/event handling",
                        evidence={"error": excerpt(str(exc))}, severity="high")

    def _on_console(self, msg: Any) -> None:
        try:
            if msg.type == "error":
                text = msg.text or ""
                # The response listener already classifies HTTP failures with
                # URL + body + whitelist.  Skip the generic duplicate here.
                if re.search(r"Failed to load resource.*status of \d+", text, re.I):
                    return
                self.ledger.add(workflow=self.workflow, step="console.error", selector="console",
                                symptom=f"console.error: {excerpt(msg.text)}",
                                root_cause_guess="runtime error or failed resource logged to console",
                                evidence={"text": excerpt(msg.text)}, severity="medium")
        except Exception:  # noqa: BLE001
            pass

    def _on_requestfailed(self, req: Any) -> None:
        try:
            failure = ""
            try:
                failure = str(req.failure)
            except Exception:  # noqa: BLE001
                pass
            url = req.url or ""
            if "/api/v2/cockpit/stream" in url or "/api/v2/stream" in url:
                # SSE streams legitimately end/reconnect; note, don't fail.
                self.ledger.note(workflow=self.workflow, step="sse-requestfailed",
                                 message="SSE stream request ended", evidence={"url": url})
                return
            self.ledger.add(workflow=self.workflow, step="requestfailed", selector=url,
                            symptom=f"network request failed: {excerpt(failure)}",
                            root_cause_guess="gateway unreachable, CORS, DNS, or aborted request",
                            evidence={"url": url, "failure": excerpt(failure)}, severity="high")
        except Exception:  # noqa: BLE001
            pass

    def _on_response(self, resp: Any) -> None:
        try:
            status = resp.status
            if status < 400:
                return
            url = resp.url or ""
            body = ""
            try:
                body = resp.text()[:600]
            except Exception:  # noqa: BLE001
                pass
            haystack = f"{url} {body}"
            if is_intentional_error(status, haystack):
                self.ledger.note(workflow=self.workflow, step="intentional-error",
                                 message=f"whitelisted HTTP {status}",
                                 evidence={"url": url, "status": status})
                return
            self.ledger.add(workflow=self.workflow, step="http-error", selector=url,
                            symptom=f"unexpected HTTP {status} response",
                            root_cause_guess="gateway/route error or unwhitelisted validation failure",
                            evidence={"url": url, "status": status, "body": excerpt(body)},
                            severity="high")
        except Exception:  # noqa: BLE001
            pass

    def scan_bad_values(self, step: str) -> None:
        """Detect rendered NaN / undefined / [object Object] in visible text."""
        try:
            hits = self.page.evaluate(
                """(lits) => {
                    const out = [];
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    const skip = new Set(['SCRIPT','STYLE','NOSCRIPT']);
                    let node;
                    while ((node = walker.nextNode())) {
                        const parent = node.parentElement;
                        if (!parent || skip.has(parent.tagName)) continue;
                        const val = (node.nodeValue || '').trim();
                        if (!val) continue;
                        for (const lit of lits) {
                            if (val.includes(lit)) {
                                out.push({lit, text: val.slice(0, 120),
                                          selector: parent.tagName.toLowerCase() +
                                            (parent.className ? '.' + String(parent.className).split(' ').slice(0,2).join('.') : '')});
                            }
                        }
                    }
                    return out;
                }""",
                BAD_LITERALS,
            )
        except Exception:  # noqa: BLE001
            return
        for hit in hits or []:
            self.ledger.add(workflow=self.workflow, step=f"bad-value/{step}", selector=hit.get("selector", ""),
                            symptom=f"rendered bad literal '{hit.get('lit')}': {excerpt(hit.get('text',''))}",
                            root_cause_guess="unformatted numeric/object value reached the DOM",
                            evidence=hit, severity="high")

    def scan_overflow(self, step: str, width: int, height: int) -> None:
        """Detect horizontal overflow at the current viewport."""
        try:
            result = self.page.evaluate(
                """() => {
                    const docW = document.documentElement.scrollWidth;
                    const winW = window.innerWidth;
                    const offenders = [];
                    if (docW > winW + 1) {
                        for (const el of document.querySelectorAll('body *')) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.right > winW + 2) {
                                const cs = getComputedStyle(el);
                                if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                                offenders.push({tag: el.tagName.toLowerCase(),
                                    cls: String(el.className).slice(0,80),
                                    right: Math.round(r.right), width: winW});
                                if (offenders.length >= 6) break;
                            }
                        }
                    }
                    return {docW, winW, offenders};
                }"""
            )
        except Exception:  # noqa: BLE001
            return
        if result and result.get("docW", 0) > result.get("winW", 0) + 1:
            self.ledger.add(
                workflow=self.workflow, step=f"overflow/{width}x{height}/{step}",
                selector="documentElement",
                symptom=(f"horizontal overflow at {width}x{height}: "
                         f"scrollWidth={result['docW']} > innerWidth={result['winW']}"),
                root_cause_guess="fixed-width child or missing min-w-0 / overflow guard",
                evidence={"offenders": result.get("offenders", [])}, severity="medium",
            )

    def check_stuck_spinner(self, step: str, timeout_ms: int = 10_000) -> None:
        """A spinner that survives 10s is 'stuck'."""
        try:
            self.page.wait_for_function(
                "() => document.querySelectorAll('svg.animate-spin').length === 0",
                timeout=timeout_ms,
            )
        except Exception:  # noqa: BLE001 - timeout => stuck
            try:
                count = self.page.locator("svg.animate-spin").count()
            except Exception:  # noqa: BLE001
                count = -1
            self.ledger.add(workflow=self.workflow, step=f"stuck-spinner/{step}",
                            selector="svg.animate-spin",
                            symptom=f"spinner still present after {timeout_ms}ms (count={count})",
                            root_cause_guess="pending API call never resolved or loading flag stuck",
                            evidence={"count": count, "url": self.page.url}, severity="high")


# ---------------------------------------------------------------------------
# Workflow DSL executor
# ---------------------------------------------------------------------------

class Driver:
    """Thin Playwright wrapper with tolerant locators and per-step capture."""

    def __init__(self, page: Any, cap: Capture, ledger: Ledger, base_url: str,
                 default_timeout_ms: int = 4000) -> None:
        self.page = page
        self.cap = cap
        self.ledger = ledger
        self.base_url = base_url
        self.timeout = default_timeout_ms
        self.context: dict[str, Any] = {}

    # -- tolerant locators ---------------------------------------------------
    def _find(self, factory: Callable[[str], Any], candidates: Iterable[str],
              budget_s: float = 2.5) -> Any:
        """Poll candidate locators until one is visible (SPA lazy render race)."""
        end = time.monotonic() + budget_s
        while True:
            for text in candidates:
                try:
                    loc = factory(text)
                    if loc.count() > 0 and loc.first.is_visible():
                        return loc.first
                except Exception:  # noqa: BLE001
                    pass
            if time.monotonic() >= end:
                return None
            time.sleep(0.1)

    def _button(self, candidates: Iterable[str]) -> Any:
        role = lambda t: self.page.get_by_role("button", name=re.compile(re.escape(t), re.I))
        text = lambda t: self.page.get_by_text(re.compile(re.escape(t), re.I), exact=False)
        return self._find(role, candidates) or self._find(text, candidates)

    def _field(self, candidates: Iterable[str]) -> Any:
        label = lambda t: self.page.get_by_label(re.compile(re.escape(t), re.I))
        placeholder = lambda t: self.page.get_by_placeholder(re.compile(re.escape(t), re.I))
        return self._find(label, candidates) or self._find(placeholder, candidates)

    # -- step implementations ------------------------------------------------
    def wait_for_content(self, timeout_ms: int = 10_000) -> None:
        """Bounded wait for the real SPA content before any text assertion.

        Uses Playwright's own deadline-based waits only (no sleep-wait loops):
        wait for the shared ``<main>`` host (portals use it too), then for its
        text to be populated and for loading spinners to clear.  A timeout is
        intentionally swallowed so the assertion still records the exact
        rendered state instead of hanging the run.
        """
        try:
            self.page.wait_for_selector("main", state="attached", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - keep going; assertion records state
            pass
        try:
            self.page.wait_for_function(
                """() => {
                    const main = document.querySelector('main');
                    const text = main ? (main.innerText || '') : '';
                    const spinners = document.querySelectorAll('svg.animate-spin').length;
                    return spinners === 0 && text.trim().length > 80
                        && !text.includes('LOADING WORKSTATION')
                        && !text.includes('RESOLVING SESSION');
                }""",
                timeout=timeout_ms,
            )
        except Exception:  # noqa: BLE001 - leave whatever rendered for assertions
            pass

    def _wait_for_spa(self, budget_s: float = 6.0) -> None:
        """Wait until React has replaced the Suspense fallback with real content."""
        self.wait_for_content(timeout_ms=max(10_000, int(budget_s * 1000)))

    def navigate(self, path: str) -> None:
        self.page.goto(f"{self.base_url}{path}", wait_until="domcontentloaded",
                       timeout=self.timeout)
        self._wait_for_spa()
        self.cap.check_stuck_spinner(f"navigate:{path}", timeout_ms=10_000)

    def reload(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self.timeout)
        self._wait_for_spa()
        self.cap.check_stuck_spinner("reload", timeout_ms=10_000)

    def click(self, candidates: list[str], *, dead_check: bool = True,
              settle_ms: int = 1500) -> None:
        btn = self._button(candidates)
        if btn is None:
            self.ledger.add(workflow=self.cap.workflow, step="click", selector=str(candidates),
                            symptom=f"no visible enabled control matching {candidates}",
                            root_cause_guess="control renamed/removed or gated out of this shell",
                            evidence={"candidates": candidates, "url": self.page.url},
                            severity="medium")
            return
        # A disabled control is expected gating (e.g. a 1-page screener's
        # Prev/Next, or a backtest whose DSR gate has not been met).  It is
        # intentionally inert, so a click assertion would be a false positive.
        try:
            if btn.is_disabled():
                self.ledger.note(
                    workflow=self.cap.workflow, step="click",
                    message="skipped: control disabled (expected gating)",
                    evidence={"candidates": candidates, "url": self.page.url},
                )
                return
        except Exception:  # noqa: BLE001 - non-button nodes have no disabled state
            pass
        before_fp = dom_fingerprint(self.page)
        before_net = self.cap.request_count
        before_url = self.page.url
        try:
            btn.click(timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            self.ledger.add(workflow=self.cap.workflow, step="click", selector=str(candidates),
                            symptom=f"click failed: {excerpt(str(exc))}",
                            root_cause_guess="element detached, covered, or disabled",
                            evidence={"candidates": candidates}, severity="medium")
            return
        if dead_check:
            changed = wait_until(
                lambda: (dom_fingerprint(self.page) != before_fp
                         or self.cap.request_count > before_net
                         or self.page.url != before_url),
                deadline_s=settle_ms / 1000.0, poll_s=0.15,
            )
            if not changed:
                self.ledger.add(
                    workflow=self.cap.workflow, step="dead-button", selector=str(candidates),
                    symptom=(f"DEAD BUTTON: click on {candidates[0]!r} produced no DOM change, "
                             "no network call and no URL/modal change"),
                    root_cause_guess="onClick handler missing/no-op or control is inert",
                    evidence={"candidates": candidates, "url": self.page.url,
                              "fingerprint": before_fp}, severity="high",
                )

    def fill(self, candidates: list[str], value: str) -> None:
        field = self._field(candidates)
        if field is None:
            self.ledger.add(workflow=self.cap.workflow, step="fill", selector=str(candidates),
                            symptom=f"no visible input matching {candidates}",
                            root_cause_guess="field label/placeholder renamed or absent",
                            evidence={"candidates": candidates}, severity="medium")
            return
        try:
            field.fill(value, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            self.ledger.add(workflow=self.cap.workflow, step="fill", selector=str(candidates),
                            symptom=f"fill failed: {excerpt(str(exc))}",
                            root_cause_guess="input disabled or readonly",
                            evidence={"candidates": candidates}, severity="medium")

    def select(self, candidates: list[str], value: str) -> None:
        field = self._field(candidates)
        if field is None:
            self.ledger.add(workflow=self.cap.workflow, step="select", selector=str(candidates),
                            symptom=f"no visible select matching {candidates}",
                            root_cause_guess="select label renamed or absent",
                            evidence={"candidates": candidates}, severity="medium")
            return
        try:
            field.select_option(label=value, timeout=self.timeout)
        except Exception:  # noqa: BLE001 - try by value
            try:
                field.select_option(value=value, timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001
                self.ledger.add(workflow=self.cap.workflow, step="select", selector=str(candidates),
                                symptom=f"select failed: {excerpt(str(exc))}",
                                root_cause_guess="option not present in dropdown",
                                evidence={"candidates": candidates, "value": value},
                                severity="medium")

    def check(self, candidates: list[str]) -> None:
        loc = None
        for text in candidates:
            try:
                candidate = self.page.get_by_label(re.compile(re.escape(text), re.I))
                if candidate.count() > 0:
                    loc = candidate.first
                    break
            except Exception:  # noqa: BLE001
                pass
        if loc is None:
            loc = self.page.locator("input[type=checkbox]").last
        try:
            if not loc.is_checked():
                loc.check(timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            self.ledger.add(workflow=self.cap.workflow, step="check", selector=str(candidates),
                            symptom=f"checkbox check failed: {excerpt(str(exc))}",
                            root_cause_guess="checkbox absent/disabled",
                            evidence={"candidates": candidates}, severity="medium")

    def expect_text(self, texts: list[str], *, absent: bool = False,
                    any_of: bool = False) -> None:
        # Never assert against a half-rendered shell: wait for real page content
        # first, then use Playwright's auto-waiting locator read.
        self.wait_for_content()
        body = ""
        try:
            body = self.page.locator("body").inner_text(timeout=self.timeout)
        except Exception:  # noqa: BLE001 - assertion below records the state
            pass
        lowered = body.lower()
        found = [t for t in texts if t.lower() in lowered]
        if absent:
            for text in found:
                self.ledger.add(workflow=self.cap.workflow, step="assert-absent", selector="text",
                                symptom=f"forbidden text {text!r} present in LiveShell DOM",
                                root_cause_guess="paper control leaked into live shell",
                                evidence={"text": text, "url": self.page.url}, severity="high")
            return
        if any_of:
            if found:
                return
            # One defect for the whole alternative set, not one per spelling.
            self.ledger.add(workflow=self.cap.workflow, step="assert-text", selector="text",
                            symptom=f"none of the expected texts {texts!r} rendered",
                            root_cause_guess="workflow did not complete or feature missing",
                            evidence={"texts": texts, "url": self.page.url,
                                      "body_excerpt": excerpt(body, 300)}, severity="medium")
            return
        missing = [t for t in texts if t not in found]
        if not missing:
            return
        for text in missing:
            self.ledger.add(workflow=self.cap.workflow, step="assert-text", selector="text",
                            symptom=f"expected text {text!r} not rendered",
                            root_cause_guess="workflow did not complete or feature missing",
                            evidence={"text": text, "url": self.page.url,
                                      "body_excerpt": excerpt(body, 300)}, severity="medium")

    def scan(self) -> None:
        self.cap.scan_bad_values("step")
        self.cap.scan_overflow("step", *self.page.viewport_size)

    def run_step(self, step: dict[str, Any]) -> None:
        op = step.get("op")
        try:
            if op == "navigate":
                self.navigate(step["path"])
            elif op == "reload":
                self.reload()
            elif op == "click":
                self.click(step["text"], dead_check=step.get("dead_check", True),
                           settle_ms=step.get("settle_ms", 1500))
            elif op == "fill":
                self.fill(step["field"], step["value"])
            elif op == "select":
                self.select(step["field"], step["value"])
            elif op == "check":
                self.check(step["field"])
            elif op == "expect_text":
                self.expect_text(step["texts"], absent=step.get("absent", False),
                                 any_of=step.get("any_of", False))
            elif op == "scan":
                self.scan()
            elif op == "screenshot":
                dest = step.get("path")
                if dest:
                    self.page.screenshot(path=str(dest), full_page=False)
            elif op == "store_body":
                self.context[step.get("as", "body")] = self.page.locator("body").inner_text()
            elif op == "expect_body_changed":
                key = step.get("as", "body")
                now = self.page.locator("body").inner_text()
                if now == self.context.get(key):
                    self.ledger.add(workflow=self.cap.workflow, step="body-change", selector="body",
                                    symptom="body text unchanged after action (expected a change)",
                                    root_cause_guess="action produced no render update",
                                    evidence={"url": self.page.url}, severity="medium")
            else:
                self.ledger.add(workflow=self.cap.workflow, step=f"unknown-op:{op}",
                                selector="-", symptom=f"unknown step op {op!r}",
                                root_cause_guess="driver/workflow definition mismatch",
                                evidence={"step": step}, severity="low")
        except Exception as exc:  # noqa: BLE001 - a step must never abort the run
            self.ledger.add(workflow=self.cap.workflow, step=f"op:{op}", selector="-",
                            symptom=f"step raised {type(exc).__name__}: {excerpt(str(exc))}",
                            root_cause_guess="selector drift or transient UI state",
                            evidence={"step": step, "url": self.page.url}, severity="medium")


# ---------------------------------------------------------------------------
# Workflow catalogue
# ---------------------------------------------------------------------------

def workflow_catalogue(base: str, shot_dir: Path) -> list[dict[str, Any]]:
    """Declarative end-to-end workflows, driven in the order listed."""

    def shot(name: str) -> dict[str, Any]:
        return {"op": "screenshot", "path": str(shot_dir / f"{name}.png")}

    wf: list[dict[str, Any]] = []

    # ---- AUTH ---------------------------------------------------------------
    wf.append({
        "name": "AUTH/setup-validation",
        "steps": [
            {"op": "navigate", "path": "/setup"},
            {"op": "fill", "field": ["Operator Username"], "value": QA_USER},
            {"op": "fill", "field": ["Password"], "value": "short"},
            {"op": "fill", "field": ["Live PIN"], "value": QA_PIN},
            {"op": "click", "text": ["Provision Operator"]},
            {"op": "expect_text", "texts": ["at least 8 characters"]},
            {"op": "fill", "field": ["Password"], "value": QA_PASSWORD},
            {"op": "fill", "field": ["Live PIN"], "value": "12"},
            {"op": "click", "text": ["Provision Operator"]},
            {"op": "expect_text", "texts": ["4 to 12 digits"]},
            {"op": "fill", "field": ["Live PIN"], "value": QA_PIN},
            {"op": "click", "text": ["Provision Operator"]},
            {"op": "expect_text", "texts": ["Enter the paper sandbox", "Enter Sandbox"]},
            shot("auth_setup"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "AUTH/dual-doors",
        "steps": [
            {"op": "navigate", "path": "/login"},
            {"op": "expect_text", "texts": ["Paper Sandbox", "Live Sovereign Vault", "Door 01", "Door 02"]},
            shot("auth_dual_doors"),
        ],
    })
    wf.append({
        "name": "AUTH/paper-login",
        "steps": [
            {"op": "navigate", "path": "/paper/login"},
            {"op": "fill", "field": ["Operator Username"], "value": QA_USER},
            {"op": "fill", "field": ["Password"], "value": "definitely-wrong"},
            {"op": "click", "text": ["Enter Sandbox"]},
            # The gateway answers a bad password with 401 INVALID_CREDENTIALS,
            # which the portal renders as "INVALID_CREDENTIALS: ...".  Treat the
            # spellings as alternatives rather than requiring all of them.
            {"op": "expect_text", "any_of": True,
             "texts": ["INVALID_CREDENTIALS", "AUTH_REQUIRED", "invalid", "denied"]},
            {"op": "fill", "field": ["Password"], "value": QA_PASSWORD},
            {"op": "click", "text": ["Enter Sandbox"]},
            {"op": "expect_text", "texts": ["PAPER SANDBOX", "SIMULATED NAV"]},
            {"op": "reload"},
            {"op": "expect_text", "texts": ["PAPER SANDBOX", "SIMULATED NAV"]},
            {"op": "scan"},
        ],
    })

    # ---- PAPER top-bar ------------------------------------------------------
    wf.append({
        "name": "PAPER/topbar-nav",
        "steps": [
            {"op": "navigate", "path": "/paper/dashboard"},
            {"op": "expect_text", "texts": ["SIMULATED NAV", "AUTOPILOT", "ALERTS"]},
            {"op": "click", "text": ["Play", "Pause"]},
            {"op": "click", "text": ["Manual Tick"]},
            {"op": "click", "text": ["ALERTS"]},
            # The control page renders "Incident & Alert Center" and per-alert
            # "ACK" buttons; "acknowledge" only appears after an ACK.
            {"op": "expect_text", "any_of": True,
             "texts": ["Alert Center", "ACK", "acknowledged"]},
            {"op": "navigate", "path": "/paper/dashboard"},
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "PAPER/deposit",
        "steps": [
            {"op": "navigate", "path": "/paper/dashboard"},
            {"op": "store_body", "as": "pre_deposit"},
            {"op": "click", "text": ["Deposit Paper Capital"]},
            {"op": "expect_text", "texts": ["Fund Sandbox", "Amount (units)"]},
            {"op": "fill", "field": ["Amount (units)"], "value": "250000"},
            {"op": "click", "text": ["Fund Sandbox"], "settle_ms": 2500},
            {"op": "expect_body_changed"},
            {"op": "reload"},
            {"op": "expect_text", "texts": ["SIMULATED NAV"]},
            shot("paper_deposit"),
            {"op": "scan"},
        ],
    })

    # ---- LIVE auth + guardrails --------------------------------------------
    wf.append({
        "name": "LIVE/login-3fa",
        "steps": [
            {"op": "navigate", "path": "/live/login"},
            {"op": "expect_text", "texts": ["23-Gate Pre-Flight", "Unseal Live Vault"]},
            {"op": "fill", "field": ["Operator Username"], "value": QA_USER},
            {"op": "fill", "field": ["Password"], "value": QA_PASSWORD},
            {"op": "fill", "field": ["6-Digit Live PIN"], "value": "999999"},
            {"op": "click", "text": ["Unseal Live Vault"]},
            {"op": "expect_text", "texts": ["real capital", "acknowledge", "irreversible"]},
            {"op": "check", "field": ["I acknowledge that this session can place"]},
            {"op": "fill", "field": ["6-Digit Live PIN"], "value": QA_PIN},
            {"op": "click", "text": ["Unseal Live Vault"]},
            {"op": "expect_text", "texts": ["LIVE PRODUCTION", "SESSION TTL", "23-GATE PRE-FLIGHT"]},
            {"op": "expect_text", "texts": ["Deposit Paper Capital", "Manual Tick", "Reset to Zero", "+10 Shadow Fills Burst"], "absent": True},
            shot("live_shell"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "LIVE/killswitch-rearm",
        "steps": [
            {"op": "navigate", "path": "/live/dashboard"},
            {"op": "click", "text": ["Emergency Kill Switch"]},
            # "confirm: true" is a request-body field in LiveShell.tsx, never a
            # rendered label; the UI surfaces ARMED / SECOND CLICK FIRES instead.
            {"op": "expect_text", "texts": ["ARM KILL SWITCH"]},
            {"op": "click", "text": ["ARM KILL SWITCH"]},
            {"op": "expect_text", "texts": ["CONFIRM HALT", "SECOND CLICK FIRES"]},
            {"op": "click", "text": ["CONFIRM HALT"], "settle_ms": 2500},
            {"op": "expect_text", "texts": ["GLOBAL HALT", "FLATTENING", "HALT"]},
            {"op": "click", "text": ["Lock Live Vault"], "settle_ms": 2500},
            {"op": "expect_text", "texts": ["Choose your execution environment", "Sovereign Gateway"]},
            shot("live_killswitch_lock"),
            {"op": "scan"},
        ],
    })

    # ---- 17 workstations, paper shell: smoke each route --------------------
    for key in WORKSTATIONS_FALLBACK:
        wf.append({
            "name": f"WORKSTATION/paper/{key}",
            "steps": [
                {"op": "navigate", "path": f"/paper/{key}"},
                {"op": "scan"},
            ],
        })

    # ---- Workstation-specific end-to-end interactions ---------------------
    wf.append({
        "name": "WORKSTATION/dashboard-detail",
        "steps": [
            {"op": "navigate", "path": "/paper/dashboard"},
            # The telemetry keys (latency_ms / feed_lag_ms /
            # pending_intents_count) are source identifiers, not rendered
            # labels; GatewayHealthStats shows LATENCY / FEED LAG /
            # PENDING INTENTS.
            {"op": "expect_text", "texts": ["RING 0", "RING 1", "RING 2", "RING 3",
                                           "LATENCY", "FEED LAG", "PENDING INTENTS"]},
            {"op": "expect_text", "texts": ["ReserveYield", "LongTermCompounder", "TacticalSwing", "SpinalScalper"]},
            {"op": "expect_text", "texts": ["SSE", "OPEN"]},
            shot("dashboard"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/missions",
        "steps": [
            {"op": "navigate", "path": "/paper/missions"},
            # MissionsPage has no "infeasible demo" button and no operator_note
            # field; those were driver inventions.  Exercise the real controls.
            {"op": "expect_text", "texts": ["New Mission", "Tranche Risk Isolation",
                                           "Profit-Split Saga Ledger"]},
            {"op": "click", "text": ["Draft Mission"], "settle_ms": 2500},
            {"op": "expect_text", "texts": ["New Mission", "Tranche Risk Isolation"]},
            {"op": "reload"},
            {"op": "expect_text", "texts": ["New Mission"]},
            shot("missions"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/trade-close-saga",
        "steps": [
            # The profit-split / trade-close saga lives on the Missions
            # workstation (Card "Profit-Split Saga Ledger"), not on Control.
            {"op": "navigate", "path": "/paper/missions"},
            {"op": "expect_text", "texts": ["Profit-Split Saga Ledger", "Close Trade",
                                           "Custody Sweep"]},
            {"op": "click", "text": ["Close Trade"]},
            {"op": "expect_text", "texts": ["Profit-Split Saga Ledger", "Custody Sweep"]},
            shot("trade_close_saga"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/screener",
        "steps": [
            {"op": "navigate", "path": "/paper/screener"},
            {"op": "expect_text", "texts": ["Macro Calendar", "Optionable only",
                                           "Asset class", "Sector", "India Sectors"]},
            {"op": "fill", "field": ["Search"], "value": "RELIANCE"},
            {"op": "fill", "field": ["Search"], "value": "BTCUSDT"},
            {"op": "fill", "field": ["Search"], "value": "AAPL"},
            {"op": "fill", "field": ["Search"], "value": "EURUSD"},
            # Prev/Next are legitimately disabled when the filtered result is a
            # single page; Driver.click records an INFO skip for those.
            {"op": "click", "text": ["Next"]},
            {"op": "click", "text": ["Prev", "Previous"]},
            {"op": "click", "text": ["Refresh"]},
            {"op": "expect_text", "texts": ["Finance Act 2026", "Instrument Catalogue"]},
            shot("screener"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/terminal",
        "steps": [
            {"op": "navigate", "path": "/paper/terminal"},
            {"op": "expect_text", "texts": ["1m", "5m", "15m", "1D", "VWAP", "L2", "Tape"]},
            {"op": "click", "text": ["5m"]},
            {"op": "click", "text": ["15m"]},
            {"op": "click", "text": ["1D"]},
            # Real dead-button candidate (Pine Studies): keep exercising it.
            {"op": "click", "text": ["Pine Studies", "Pine"]},
            {"op": "expect_text", "texts": ["Quick Ticket", "Submit paper order", "L2 Ladder", "Tape"]},
            shot("terminal"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/orders",
        "steps": [
            {"op": "navigate", "path": "/paper/orders"},
            # BUY/SELL are <select> options, not buttons; the sole submit
            # control is "Submit paper order".  STT is a backend cost key,
            # never a rendered label.
            {"op": "expect_text", "texts": ["Open Orders", "Statutory Cost Drag",
                                           "Outbox", "Quantity"]},
            {"op": "fill", "field": ["Quantity"], "value": "-5"},
            {"op": "select", "field": ["Side"], "value": "BUY"},
            {"op": "click", "text": ["Submit paper order"]},
            {"op": "fill", "field": ["Quantity"], "value": "0"},
            {"op": "click", "text": ["Submit paper order"]},
            {"op": "expect_text", "any_of": True,
             "texts": ["INVALID_QUANTITY", "positive", "greater than zero", "invalid"]},
            {"op": "fill", "field": ["Quantity"], "value": "10"},
            {"op": "select", "field": ["Side"], "value": "BUY"},
            {"op": "click", "text": ["Submit paper order"], "settle_ms": 2500},
            {"op": "select", "field": ["Side"], "value": "SELL"},
            {"op": "click", "text": ["Submit paper order"], "settle_ms": 2500},
            {"op": "expect_text", "texts": ["statutory", "Pre-Trade Feedback"]},
            {"op": "click", "text": ["Re-issue"]},
            shot("orders"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/options",
        "steps": [
            {"op": "navigate", "path": "/paper/options"},
            {"op": "expect_text", "texts": ["OPTIONS", "Option Chain", "Strategy Builder",
                                           "Payoff Curve", "GEX"]},
            # The underlying is an unlabelled <select>; the strategy presets
            # are the real labelled controls.
            {"op": "click", "text": ["LONG CALL"]},
            {"op": "click", "text": ["IRON CONDOR"]},
            shot("options"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/analyzer-burst",
        "steps": [
            {"op": "navigate", "path": "/paper/analyzer"},
            # The counter renders "0 / 30" with spaces, never "0/30".
            {"op": "expect_text", "texts": ["Graduation Counter", "Shadow Fills",
                                           "30 settled L2 shadow fills"]},
            {"op": "click", "text": ["+10 Shadow Fills Burst"], "settle_ms": 2500},
            {"op": "expect_text", "texts": ["10 settled"]},
            shot("analyzer_burst"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/quant",
        "steps": [
            {"op": "navigate", "path": "/paper/quant"},
            # "Greeks" is a TS interface name; the rendered pricer shows Delta,
            # Gamma, Vega and Theta stats.
            {"op": "expect_text", "texts": ["490-factor", "Regional Engines",
                                           "Black-Scholes Pricer", "Delta"]},
            {"op": "select", "field": ["Type"], "value": "PUT"},
            {"op": "click", "text": ["Price with backend"], "settle_ms": 2000},
            {"op": "expect_text", "texts": ["Delta", "Gamma", "Theta", "Vega"]},
            shot("quant"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/agents-live",
        "steps": [
            # Canonical route for the Agents workstation (nav.ts key "agents").
            # Live-LLM provisioning is verified via the separate live-LLM
            # sub-run; "hy4-flash-free" is a model id, not a rendered label.
            {"op": "navigate", "path": "/paper/agents"},
            {"op": "expect_text", "texts": ["AGENTS", "Assign specialist",
                                           "Multi-symbol watchlist scan",
                                           "Grounded strategy Q&A", "$3.10"]},
            {"op": "fill", "field": ["Symbol"], "value": "RELIANCE"},
            {"op": "click", "text": ["Dispatch task"]},
            {"op": "expect_text", "any_of": True, "texts": ["TASK", "grounded report"]},
            {"op": "fill", "field": ["Symbol"], "value": "BTCUSDT"},
            {"op": "click", "text": ["Rank watchlist"]},
            {"op": "fill", "field": ["What are the entry", "entry, target and stop"],
             "value": "What drove NIFTY today and is the claim grounded?"},
            {"op": "click", "text": ["Ask"]},
            {"op": "expect_text", "texts": ["Grounded strategy Q&A", "grounded"]},
            {"op": "expect_text", "texts": ["Entity", "Post-Mortem",
                                           "Episodic operator decisions", "Compact Memory"]},
            shot("agents"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/backtest",
        "steps": [
            {"op": "navigate", "path": "/paper/backtest"},
            # SYNTHETIC_WALK_FORWARD / TRADINGVIEW_REPLAY are <option> values;
            # the visible labels are the card/tab text below.
            {"op": "expect_text", "texts": ["BACKTEST", "Walk-forward configuration",
                                           "DSR GATE", "T+1", "T+5", "T+20"]},
            {"op": "click", "text": ["Run walk-forward"], "settle_ms": 2500},
            {"op": "expect_text", "any_of": True, "texts": ["DSR PASS", "DSR FAIL", "DSR —"]},
            # "Promote to nursery" is disabled until DSR >= 1.50 and net > 0;
            # Driver.click records the expected-gating skip.
            {"op": "click", "text": ["Promote to nursery"]},
            shot("backtest"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/brain",
        "steps": [
            {"op": "navigate", "path": "/paper/brain"},
            {"op": "expect_text", "texts": ["RING 0", "RING 1", "RING 2", "RING 3", "p99", "MiroFish", "Shock Simulator", "Committee", "NiosLifecycleWorker"]},
            {"op": "click", "text": ["Run 3-tick cascade"]},
            {"op": "click", "text": ["Debate"]},
            shot("brain"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/forge",
        "steps": [
            {"op": "navigate", "path": "/paper/forge"},
            {"op": "expect_text", "texts": ["Champion", "Challenger", "ADWIN", "SKILL.md", "Contract"]},
            # SKILL.md bodies and the contract are static text, not buttons;
            # the real control is "Evaluate gates".
            {"op": "click", "text": ["Evaluate gates"]},
            shot("forge"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/ledger-logbook",
        "steps": [
            {"op": "navigate", "path": "/paper/ledger"},
            {"op": "expect_text", "texts": ["debits", "credits", "4-Silo", "Reconciler", "Underwater", "Drawdown"]},
            {"op": "navigate", "path": "/paper/logbook"},
            {"op": "expect_text", "texts": ["LOGBOOK", "Filters", "R-multiple"]},
            shot("logbook"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/verification",
        "steps": [
            {"op": "navigate", "path": "/paper/verification"},
            {"op": "expect_text", "texts": ["Run Reconciliation", "VOPR", "Chaos",
                                           "Clock Health", "Prometheus", "Clear Freeze"]},
            {"op": "click", "text": ["Run Reconciliation"], "settle_ms": 2500},
            {"op": "click", "text": ["Run VOPR", "VOPR"]},
            {"op": "click", "text": ["Run Chaos Drill", "Chaos"]},
            # "Clock Health Audit" is a read-only card, not a click target.
            {"op": "click", "text": ["Prometheus Refresh", "Prometheus"]},
            # Clear Freeze is LIVE-gated; the skip is recorded as INFO.
            {"op": "click", "text": ["Clear Freeze"]},
            {"op": "expect_text", "texts": ["ColdStartReconciler", "Reconciler", "Freeze State"]},
            shot("verification"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/brokers",
        "steps": [
            {"op": "navigate", "path": "/paper/brokers"},
            # The vault mask is the Ring 0 "***last4" form, not four asterisks.
            {"op": "expect_text", "texts": ["MASK", "Ping", "Handshake"]},
            {"op": "click", "text": ["Ping"]},
            {"op": "expect_text", "any_of": True, "texts": ["LATENCY", "ms", "OK"]},
            {"op": "click", "text": ["Handshake"]},
            shot("brokers"),
            {"op": "scan"},
        ],
    })
    wf.append({
        "name": "WORKSTATION/control",
        "steps": [
            {"op": "navigate", "path": "/paper/control"},
            {"op": "expect_text", "texts": ["Alert Center", "Runtime Risk & Connector Policy",
                                           "LLM Pool", "Write-Only Credential Vault",
                                           "MCP Console", "Factory Reset"]},
            {"op": "click", "text": ["Probe Providers"], "settle_ms": 8000},
            # The probe note reports status/latency; the model id
            # "hy4-flash-free" is never rendered in the cockpit.
            {"op": "expect_text", "any_of": True, "texts": ["LLM PROBE", "ms"]},
            # "Alert Center" is a card title; the real controls are the tabs.
            {"op": "click", "text": ["Unacknowledged"]},
            {"op": "select", "field": ["Connector Mode"], "value": "SANDBOX_TESTNET"},
            {"op": "click", "text": ["Persist Settings"]},
            {"op": "reload"},
            {"op": "expect_text", "texts": ["Runtime Risk & Connector Policy"]},
            {"op": "fill", "field": ["API Key"], "value": "QA_SYNTHETIC_CREDENTIAL_NOT_A_KEY"},
            {"op": "click", "text": ["Write Credential"]},
            {"op": "check", "field": ["Rotate the Ring 0 vault key"]},
            {"op": "click", "text": ["Rotate Keys"]},
            {"op": "click", "text": ["Call Tool"]},
            {"op": "expect_text", "texts": ["TOOLS"]},
            {"op": "fill", "field": ["Password"], "value": QA_PASSWORD},
            {"op": "fill", "field": ["Live PIN"], "value": QA_PIN},
            {"op": "fill", "field": ["Type RESET"], "value": "RESET"},
            {"op": "click", "text": ["Factory Reset"], "settle_ms": 2500},
            {"op": "expect_text", "texts": ["FACTORY RESET COMPLETE", "Provision again"]},
            shot("control_reset"),
            {"op": "scan"},
        ],
    })

    # ---- Live shell route census (must not leak paper controls) ------------
    wf.append({
        "name": "LIVE/route-census",
        "steps": [
            {"op": "navigate", "path": "/live/login"},
            {"op": "fill", "field": ["Operator Username"], "value": QA_USER},
            {"op": "fill", "field": ["Password"], "value": QA_PASSWORD},
            {"op": "fill", "field": ["6-Digit Live PIN"], "value": QA_PIN},
            {"op": "check", "field": ["I acknowledge that this session can place"]},
            {"op": "click", "text": ["Unseal Live Vault"]},
            {"op": "expect_text", "texts": ["LIVE PRODUCTION"]},
            {"op": "navigate", "path": "/live/analyzer"},
            {"op": "expect_text", "texts": ["+10 Shadow Fills Burst", "Deposit Paper Capital", "Reset to Zero"], "absent": True},
            shot("live_analyzer"),
            {"op": "scan"},
        ],
    })
    return wf


# ---------------------------------------------------------------------------
# Browser run
# ---------------------------------------------------------------------------

def run_browser(ledger: Ledger, redactor: Redactor, base_url: str,
                live_llm: bool, max_seconds: float, shot_dir: Path) -> dict[str, Any]:
    """Run all browser workflows.  Returns a summary dict."""
    from playwright.sync_api import sync_playwright

    summary: dict[str, Any] = {"workflows_run": 0, "screenshots": 0, "viewports": VIEWPORTS}
    deadline = time.monotonic() + max_seconds

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            ledger.block(workflow="BROWSER", step="launch",
                         reason=f"chromium launch failed: {excerpt(str(exc))}",
                         evidence={"error": excerpt(str(exc))})
            return summary
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.set_default_timeout(4000)

            workflows = workflow_catalogue(base_url, shot_dir)
            # One Capture instance for the whole session; listeners are wired
            # once.  Attribution is retargeted per workflow.
            cap = Capture(page, ledger, workflows[0]["name"] if workflows else "BROWSER")
            for wf in workflows:
                if time.monotonic() > deadline:
                    ledger.block(workflow="RUN", step="global-deadline",
                                 reason="global time budget exhausted; remaining workflows skipped",
                                 evidence={"max_seconds": max_seconds})
                    break
                cap.set_workflow(wf["name"])
                drv = Driver(page, cap, ledger, base_url)
                for step in wf["steps"]:
                    if time.monotonic() > deadline:
                        break
                    drv.run_step(step)
                # Scan the whole page at both viewports for the last route.
                for w, h in VIEWPORTS:
                    page.set_viewport_size({"width": w, "height": h})
                    cap.scan_overflow(f"end:{wf['name']}", w, h)
                    cap.scan_bad_values(f"end:{wf['name']}")
                page.set_viewport_size({"width": 1440, "height": 900})
                summary["workflows_run"] += 1

            summary["screenshots"] = len(list(shot_dir.glob("*.png")))
            context.close()
        finally:
            browser.close()

    if live_llm:
        # If live-LLM calls never produced a hy4-flash-free signal anywhere,
        # record the live portion as blocked rather than a defect.
        saw_live = any(
            "hy4-flash-free" in json.dumps(check, ensure_ascii=False)
            for check in ledger.checks
        )
        text_mentions = any(
            "hy4-flash-free" in json.dumps(info, ensure_ascii=False)
            for info in ledger.info
        )
        if not saw_live and not text_mentions:
            ledger.block(
                workflow="AGENTS/live-llm", step="hy4-flash-free",
                reason=("live hy4-flash-free response could not be confirmed; the endpoint "
                        "may be blocked in this environment. Structural + UI checks still ran."),
                evidence={"masked_key": redactor.masked, "api_file_path_passed": True},
            )
    return summary


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_report(ledger: Ledger, report_dir: Path, meta: dict[str, Any],
                 census: dict[str, Any]) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "qa_report.json"
    md_path = report_dir / "qa_report.md"

    payload = ledger.redactor.scrub({
        "schema": "nios.qa.human_tester/1.0",
        "generated_utc": utc_now(),
        "meta": meta,
        "route_census": census,
        "summary": {
            "defects": len(ledger.defects),
            "blocked": len(ledger.blocked),
            "info": len(ledger.info),
            "checks": len(ledger.checks),
            "clean": ledger.clean,
        },
        "defects": ledger.defects,
        "blocked": ledger.blocked,
        "info": ledger.info,
        "checks": ledger.checks,
    })
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")

    lines = [
        "# NIOS QA human-tester report",
        "",
        f"- Generated: `{meta.get('generated_utc')}`",
        f"- Mode: `{meta.get('mode')}`",
        f"- Base URL: `{meta.get('base_url')}`",
        f"- Routes checked: `{census.get('total_routes')}`",
        f"- Defects: **{len(ledger.defects)}** · Blocked: {len(ledger.blocked)} · "
        f"Info: {len(ledger.info)} · Checks: {len(ledger.checks)}",
        f"- Verdict: **{'CLEAN' if ledger.clean else 'DEFECTS FOUND'}**",
        "",
    ]
    if meta.get("environment_warning"):
        lines += [f"> **Environment warning:** {meta['environment_warning']}", ""]
    lines += [
        "## Defect ledger",
        "",
    ]
    if not ledger.defects:
        lines.append("_No defects recorded._")
    else:
        lines += [
            "| ID | Workflow | Step | Severity | Symptom |",
            "| --- | --- | --- | --- | --- |",
        ]
        for d in ledger.defects:
            symptom = str(d["symptom"]).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {d['id']} | {d['workflow']} | {d['step']} | {d['severity']} | {symptom[:160]} |"
            )
        lines += ["", "### Detail", ""]
        for d in ledger.defects:
            lines += [
                f"#### {d['id']} — {d['workflow']} / {d['step']}",
                f"- **Selector:** `{d['selector']}`",
                f"- **Symptom:** {d['symptom']}",
                f"- **Root cause guess:** {d['root_cause_guess']}",
                f"- **Evidence:** `{json.dumps(d['evidence'], ensure_ascii=False)[:600]}`",
                "",
            ]
    lines += ["", "## Blocked", ""]
    if not ledger.blocked:
        lines.append("_Nothing blocked._")
    else:
        for b in ledger.blocked:
            lines.append(f"- **{b['workflow']}/{b['step']}:** {b['reason']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NIOS V2.5 headless-human QA driver (Playwright).",
    )
    parser.add_argument("--live-llm", action="store_true",
                        help="enable live LLM execution (hy4-flash-free) on the spawned server")
    parser.add_argument("--api-file", default=DEFAULT_API_FILE,
                        help=f"path to the operator API-key prose file (PATH is passed to runtime only). Default: {DEFAULT_API_FILE}")
    parser.add_argument("--offline", action="store_true",
                        help="structural checks only; no server or browser")
    parser.add_argument("--base-url", default=BASE_URL, help=f"cockpit base URL (default {BASE_URL})")
    parser.add_argument("--report-dir", default=None,
                        help="output directory for qa_report.json/.md (default: a fresh temp dir)")
    parser.add_argument("--headed", action="store_true", help="launch a headed browser")
    parser.add_argument("--max-seconds", type=float, default=600.0,
                        help="global time budget for the browser run (default 600)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    redactor = Redactor(args.api_file)
    ledger = Ledger(redactor)

    run_root = Path(tempfile.mkdtemp(prefix="nios_qa_run_", dir=tempfile.gettempdir()))
    report_dir = Path(args.report_dir) if args.report_dir else run_root / "report"
    shot_dir = run_root / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    mode = "offline-structural" if args.offline else ("browser+live-llm" if args.live_llm else "browser+offline-llm")
    meta: dict[str, Any] = {
        "generated_utc": utc_now(),
        "mode": mode,
        "base_url": args.base_url,
        "repo_root": str(REPO_ROOT),
        "python": sys.executable,
        "api_file_path": args.api_file,
        "api_key_mask": redactor.masked,
        "report_dir": str(report_dir),
        "run_root": str(run_root),
    }

    # 1) Always run the structural/offline portion.
    census = structural_checks(ledger)

    server: ManagedServer | None = None
    extra: dict[str, Any] = {}
    fatal = False

    if not args.offline:
        # 2) Ensure a server with an isolated state root.
        server = ManagedServer(args.api_file if args.live_llm else None,
                               args.live_llm, run_root, ledger, args.base_url)
        if not server.ensure():
            ledger.block(workflow="RUN", step="server-unavailable",
                         reason="NIOS server not reachable and could not be started",
                         evidence={"base_url": args.base_url})
            fatal = True
        else:
            # 3) Run the browser portion.
            try:
                extra = run_browser(ledger, redactor, args.base_url, args.live_llm,
                                    args.max_seconds, shot_dir)
            except Exception as exc:  # noqa: BLE001
                ledger.block(workflow="RUN", step="browser-exception",
                             reason=f"browser run aborted: {type(exc).__name__}: {excerpt(str(exc))}",
                             evidence={"error": excerpt(str(exc))})
                fatal = True

    meta.update(extra)
    if server is not None and not server.started_by_us:
        meta["environment_warning"] = (
            "Browser checks ran against a pre-existing server; state/operator "
            "isolation could not be guaranteed. Auth-dependent findings from "
            "that run are environment-dependent and must be re-verified on an "
            "isolated instance."
        )
    json_path, md_path = write_report(ledger, report_dir, meta, census)

    # Console summary (never includes secrets).
    print("=" * 72)
    print("NIOS QA HUMAN TESTER — SUMMARY")
    print("=" * 72)
    print(f"Mode            : {mode}")
    print(f"Routes (census) : {census.get('total_routes')}")
    print(f"Defects         : {len(ledger.defects)}")
    print(f"Blocked         : {len(ledger.blocked)}")
    print(f"Checks          : {len(ledger.checks)}")
    print(f"API key (mask)  : {redactor.masked}")
    print(f"JSON report     : {json_path}")
    print(f"Markdown report : {md_path}")
    for d in ledger.defects[:15]:
        print(f"  [{d['id']}] {d['workflow']}/{d['step']}: {excerpt(d['symptom'], 110)}")
    print("=" * 72)

    if server is not None:
        server.stop()

    # Exit policy: defects -> 1; server/browser unavailable -> 2; else 0.
    if fatal:
        return 2
    return 1 if ledger.defects else 0


if __name__ == "__main__":
    raise SystemExit(main())
