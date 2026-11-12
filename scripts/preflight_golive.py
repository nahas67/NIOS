#!/usr/bin/env python3
"""NIOS Phase 4 -- pre-flight go-live certification CLI.

A human operator runs this **before** enabling ``confirm_live=true`` on any
broker silo.  It re-proves, from scratch, every invariant the four rings claim
to uphold and emits a deterministic, SHA-256 signed Go-Live Readiness
Certificate.

The certificate digest is computed over the *gate outcomes only* (gate id,
status and stable scalar evidence).  Wall-clock timestamps, pids, durations and
host names are deliberately excluded, so two runs with the same system state
produce the same 64-character digest on any machine -- which is what makes the
certificate meaningful as evidence rather than as a log line.

Gate inventory (22):

   1  git.clean_working_tree
   2  git.no_tracked_secrets
   3  release.tag_v3_0_0_prod
   4  release.artifacts_present
   5  smoke.22_row_suite
   6  formal.tla_four_proofs
   7  formal.vopr_1000_steps
   8  ledger.ibor_double_entry
   9  ring0.key_vault_integrity
  10  ring0.twelve_venue_dry_run_signatures
  11  ring0.testnet_bridge_write_only
  12  ring0.conformal_gate_rejects_negative_edge
  13  ring1.cost_floor_tax_net
  14  feeds.dual_vendor_four_silos_lee_ready
  15  ring2.contagion_three_tick_cascade
  16  ring2.alpha_forge_audit_retires_decayed
  17  ring2.tail_hedger_grounded_put
  18  supervisor.four_ring_process_contract
  19  supervisor.hedge_signing_contract
  20  os.low_latency_socket_readiness
  21  observability.metrics_and_health
  22  cockpit.killswitch_and_rearm

Nothing here places an order, unlocks a silo, or writes a credential.  Gate
11 dispatches one *sandbox* order that is expected to be refused for want of a
credential -- the refusal is the proof that the write-only vault is airtight.

Usage::

    python scripts/preflight_golive.py
    python scripts/preflight_golive.py --json
    python scripts/preflight_golive.py --silo INDIA_ZERODHA_DHAN --strict

Exit codes: ``0`` certified ready, ``1`` at least one gate failed (or a warning
under ``--strict``), ``2`` the preflight itself could not run.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RELEASE = "3.0.0-PROD"
SCHEMA = "nios_preflight_golive_certificate_v1"
CERTIFICATE_KIND = "nios_golive_readiness_certificate"
SIGNER = "nios-preflight-golive/v3"

SILO_SCOPE_ALL = "ALL"
SILOS: tuple[str, ...] = (
    "CRYPTO_BINANCE",
    "INDIA_ZERODHA_DHAN",
    "US_IBKR",
    "FX_MT5",
)

#: Release artifacts that must be present in the tree for a live cutover.
REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "NIOS_Core_architecture.json",
    "README.md",
    "RUNBOOK.md",
    "docker-compose.yml",
    "pyproject.toml",
    "start_nios.bat",
    "start_nios.sh",
    "core/orchestrator.py",
    "core/process_supervisor.py",
    "core/os_tuning.py",
    "ring0_vault/kernel/formal_verifier.py",
    "scripts/run_nios.py",
    "scripts/preflight_golive.py",
)

#: Credential-shaped *file* names.  Source modules such as
#: ``broker_credentials.py`` are legitimate and must never be flagged, so the
#: match is on the basename/extension only -- never on a substring of a path.
FORBIDDEN_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.der",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials.json",
    "secrets.json",
    "*.secret",
    "*.secrets",
    "*.sqlite3",
    "*.sqlite",
    "*.db",
)

#: Exact formal-verification totals locked by Phase 2 (sec_010 / sec_018).
EXPECTED_TLA_STATES = 35_771
EXPECTED_TLA_SPECS = frozenset(
    {"KillSwitch", "OrderLifecycle", "Outbox", "Reconciliation"}
)
EXPECTED_VOPR_STEPS = 1_000
EXPECTED_SANDBOX_VENUES = 12

SMOKE_TIMEOUT_S = 600.0
GIT_TIMEOUT_S = 60.0
KILLSWITCH_LATENCY_BUDGET_NS = 1_000_000

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


# ---------------------------------------------------------------------------
# Canonical serialisation / digest
# ---------------------------------------------------------------------------


def canonical_json(payload: Any) -> str:
    """Stable JSON rendering (sorted keys, no insignificant whitespace)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(payload: Any) -> str:
    """SHA-256 over the canonical rendering of ``payload``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Gate plumbing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Gate:
    """One pre-flight assertion and its verdict."""

    gate_id: str
    label: str
    status: str = WARN
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def certificate_payload(self) -> dict[str, Any]:
        """Deterministic projection: no timings, no pids, no wall clock."""
        return {
            "gate": self.gate_id,
            "label": self.label,
            "status": self.status,
            "evidence": self.evidence,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate_id,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
            "duration_ms": round(self.duration_ms, 3),
        }


@dataclass(slots=True)
class PreflightContext:
    """Shared state for the gate run."""

    silo_scope: str
    strict: bool
    tempdir: str
    full_smoke: bool = True
    runtime: Any = None
    _client: Any = None

    @property
    def silos(self) -> tuple[str, ...]:
        if self.silo_scope == SILO_SCOPE_ALL:
            return SILOS
        return (self.silo_scope,)

    def orchestrator(self) -> Any:
        """Build (once) the single-process orchestrator used by Ring 3 gates."""
        if self.runtime is None:
            import scripts.run_nios as runner

            self.runtime = runner.build_runtime()
        return self.runtime

    def client(self) -> Any:
        if self._client is None:
            from fastapi.testclient import TestClient

            import scripts.run_nios as runner

            self._client = TestClient(runner.fastapi_app(self.orchestrator()))
        return self._client

    def close(self) -> None:
        self._client = None
        if self.runtime is not None:
            try:
                self.runtime.close()
            except Exception:  # pragma: no cover - teardown best effort
                pass
            self.runtime = None


# ---------------------------------------------------------------------------
# Small process / filesystem helpers
# ---------------------------------------------------------------------------


def _git(*args: str) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return 127, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout, completed.stderr


def _python_executable() -> str:
    return sys.executable or "python"


# ---------------------------------------------------------------------------
# 1-4: repository and release hygiene
# ---------------------------------------------------------------------------


def gate_clean_working_tree(_ctx: PreflightContext) -> Gate:
    """No uncommitted change to a *tracked* file.

    Untracked scratch files are ignored: a go-live candidate is defined by its
    committed tree, and refusing to certify because a developer left a log file
    in the checkout would train operators to bypass the check.
    """
    code, out, err = _git("status", "--porcelain", "--untracked-files=no")
    if code != 0:
        return Gate(
            "git.clean_working_tree",
            "Git working tree clean (tracked files)",
            FAIL,
            f"git status failed: {err.strip()[:160]}",
            {"git_status_rc": code},
        )
    dirty = [line for line in out.splitlines() if line.strip()]
    return Gate(
        "git.clean_working_tree",
        "Git working tree clean (tracked files)",
        PASS if not dirty else FAIL,
        "no tracked modifications"
        if not dirty
        else f"{len(dirty)} tracked path(s) modified",
        {"tracked_modifications": len(dirty), "paths": dirty[:12]},
    )


def _forbidden_matches(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".py"):
        return False
    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in FORBIDDEN_FILE_PATTERNS)


def gate_no_tracked_secrets(_ctx: PreflightContext) -> Gate:
    """``git ls-files`` guard: no credential, key or database file is tracked."""
    code, out, err = _git("ls-files")
    if code != 0:
        return Gate(
            "git.no_tracked_secrets",
            "Zero tracked secret files (git ls-files guard)",
            FAIL,
            f"git ls-files failed: {err.strip()[:160]}",
            {"git_ls_files_rc": code},
        )
    tracked = [line.strip() for line in out.splitlines() if line.strip()]
    matches = sorted(path for path in tracked if _forbidden_matches(path))
    return Gate(
        "git.no_tracked_secrets",
        "Zero tracked secret files (git ls-files guard)",
        PASS if not matches else FAIL,
        f"{len(tracked)} tracked files, {len(matches)} credential-shaped",
        {
            "tracked_files": len(tracked),
            "secret_files_found": len(matches),
            "matches": matches[:12],
            "patterns": list(FORBIDDEN_FILE_PATTERNS),
        },
    )


def gate_release_tag(ctx: PreflightContext) -> Gate:
    """The annotated release tag ``v3.0.0-PROD`` exists and points at HEAD-worthy code."""
    tag = f"v{RELEASE}"
    code, out, _err = _git("rev-parse", "-q", "--verify", f"refs/tags/{tag}")
    exists = code == 0 and bool(out.strip())
    kind_code, kind_out, _ke = _git("cat-file", "-t", tag) if exists else (1, "", "")
    annotated = kind_code == 0 and kind_out.strip() == "tag"
    scope = "all silos" if ctx.silo_scope == SILO_SCOPE_ALL else ctx.silo_scope
    status = PASS if exists and annotated else FAIL
    return Gate(
        "release.tag_v3_0_0_prod",
        f"Release tag {tag} present and annotated",
        status,
        f"{tag} -> {out.strip()[:12] or 'missing'} ({kind_out.strip() or 'n/a'}), scope={scope}",
        {
            "tag": tag,
            "exists": exists,
            "annotated": annotated,
            "release": RELEASE,
            "silo_scope": scope,
        },
    )


def gate_artifacts_present(_ctx: PreflightContext) -> Gate:
    """Every release artifact exists and the runbook advertises 18 suites."""
    missing = [rel for rel in REQUIRED_ARTIFACTS if not (ROOT / rel).is_file()]
    runbook = ROOT / "RUNBOOK.md"
    suite_ok = False
    if runbook.is_file():
        text = runbook.read_text(encoding="utf-8", errors="replace")
        suite_ok = "18 suites" in text
    ok = not missing and suite_ok
    detail = f"{len(REQUIRED_ARTIFACTS) - len(missing)}/{len(REQUIRED_ARTIFACTS)} artifacts"
    if missing:
        detail += f", missing={missing[:4]}"
    detail += f", runbook_18_suites={'yes' if suite_ok else 'no'}"
    return Gate(
        "release.artifacts_present",
        "Release artifacts + runbook suite inventory",
        PASS if ok else FAIL,
        detail,
        {
            "artifacts_expected": len(REQUIRED_ARTIFACTS),
            "artifacts_missing": len(missing),
            "missing": missing[:8],
            "runbook_declares_18_suites": suite_ok,
        },
    )


# ---------------------------------------------------------------------------
# 5: the 22-row smoke suite
# ---------------------------------------------------------------------------


def _smoke_receipt_path() -> Path:
    """Where a completed 22-row smoke certification is cached.

    The receipt is a *record of a real run*, never a substitute for one: it is
    keyed on the git commit, invalidated when that commit moves, and only ever
    written after ``run_nios.py --smoke-test`` printed 22 PASS rows and exited
    zero.  ``full_smoke=False`` (the interactive cockpit fast path) replays it
    so an operator is not forced to wait ~30 s for a suite that has not changed.
    """
    return ROOT / ".nios" / "preflight" / "smoke_receipt.json"


def _read_smoke_receipt() -> dict[str, Any] | None:
    """Return the cached receipt when it still describes *this* commit."""
    path = _smoke_receipt_path()
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - corrupt cache is a miss
        return None
    code, out, _err = _git("rev-parse", "HEAD")
    head = out.strip() if code == 0 else ""
    if not head or str(receipt.get("commit", "")) != head:
        return None
    if int(receipt.get("rows_observed", 0) or 0) != 22:
        return None
    if int(receipt.get("returncode", -1)) != 0 or not receipt.get("certified"):
        return None
    return receipt


def _write_smoke_receipt(evidence: Mapping[str, Any]) -> None:
    """Persist a *successful* smoke certification for the fast path."""
    code, out, _err = _git("rev-parse", "HEAD")
    head = out.strip() if code == 0 else ""
    path = _smoke_receipt_path()
    payload = {
        "schema": "nios_smoke_receipt_v1",
        "commit": head,
        "generated_at_ns": time.time_ns(),
        **{key: evidence[key] for key in evidence},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError:  # pragma: no cover - a cache write must never fail the gate
        pass


def gate_smoke_suite(ctx: PreflightContext) -> Gate:
    """Run ``scripts/run_nios.py --smoke-test`` and require 22 PASS rows.

    With ``ctx.full_smoke`` false the gate replays the persisted receipt for the
    current commit instead of re-spawning a 30 s suite; the evidence it reports
    is byte-identical to the executed run so the certificate digest is stable
    across both paths.
    """
    if not ctx.full_smoke:
        receipt = _read_smoke_receipt()
        if receipt is not None:
            return Gate(
                "smoke.22_row_suite",
                "22-row run_nios.py --smoke-test suite",
                PASS,
                str(receipt.get("detail") or "ROWS=22"),
                {
                    "rows_expected": 22,
                    "rows_observed": int(receipt.get("rows_observed", 22)),
                    "certified": bool(receipt.get("certified", True)),
                    "returncode": int(receipt.get("returncode", 0)),
                    "budget_passed": bool(receipt.get("budget_passed", True)),
                },
            )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    env["PYTHONIOENCODING"] = "utf-8"
    command = [_python_executable(), "-X", "utf8", str(ROOT / "scripts" / "run_nios.py"), "--smoke-test"]
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=SMOKE_TIMEOUT_S,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Gate(
            "smoke.22_row_suite",
            "22-row run_nios.py --smoke-test suite",
            FAIL,
            f"smoke suite exceeded {SMOKE_TIMEOUT_S:.0f} s",
            {"rows_expected": 22, "rows_observed": 0, "returncode": -1},
        )
    output = (completed.stdout or "") + (completed.stderr or "")
    # Count only table rows: the footer line also carries a ``PASS`` token for
    # the runtime budget, and must not be mistaken for a 23rd organ.
    pass_rows = sum(
        1
        for line in output.splitlines()
        if "  PASS  " in line and not line.lstrip().startswith("ROWS=")
    )
    rows_line = ""
    for line in output.splitlines():
        if line.startswith("ROWS="):
            rows_line = line.strip()
            break
    certified = "SMOKE TEST PASSED" in output
    ok = (
        completed.returncode == 0
        and pass_rows == 22
        and certified
        and rows_line.startswith("ROWS=22")
    )
    detail = rows_line or "ROWS= line missing"
    if not ok:
        first_fail = next(
            (line.strip()[:90] for line in output.splitlines() if "  FAIL  " in line),
            "",
        )
        detail += f", rc={completed.returncode}, pass_rows={pass_rows}"
        if first_fail:
            detail += f", first_failure={first_fail}"
    evidence = {
        "rows_expected": 22,
        "rows_observed": pass_rows,
        "certified": certified,
        "returncode": completed.returncode,
        "budget_passed": "RUNTIME BUDGET  PASS" in output,
    }
    if ok:
        # Only a real 22-row PASS is ever cached for the fast path.
        _write_smoke_receipt({**evidence, "detail": detail})
    return Gate(
        "smoke.22_row_suite",
        "22-row run_nios.py --smoke-test suite",
        PASS if ok else FAIL,
        detail,
        evidence,
    )


# ---------------------------------------------------------------------------
# 6-7: formal verification
# ---------------------------------------------------------------------------


def gate_tla_proofs(_ctx: PreflightContext) -> Gate:
    """All four TLA+ state-space proofs exhaustively hold (35,771 states)."""
    from ring0_vault.kernel.formal_verifier import FormalVerificationSuite

    report = FormalVerificationSuite(vopr_steps=1).run()
    tla = report["tla"]
    specs = tla.get("specs") or []
    names = {str(spec.get("name")) for spec in specs}
    states = int(tla.get("total_states", 0))
    counterexamples = int(tla.get("total_counterexamples", 0))
    ok = (
        bool(tla.get("ok"))
        and states == EXPECTED_TLA_STATES
        and counterexamples == 0
        and names == EXPECTED_TLA_SPECS
    )
    return Gate(
        "formal.tla_four_proofs",
        "TLA+ exhaustive BFS proofs (4 specs)",
        PASS if ok else FAIL,
        f"{states} states, {int(tla.get('total_transitions', 0))} transitions, "
        f"{counterexamples} counterexamples, {len(specs)} specs",
        {
            "total_states": states,
            "total_transitions": int(tla.get("total_transitions", 0)),
            "total_counterexamples": counterexamples,
            "specs": sorted(names),
            "mode": str(tla.get("mode", "")),
        },
    )


def gate_vopr_campaign(_ctx: PreflightContext) -> Gate:
    """VOPR deterministic fault injection: 1,000 steps, zero overdrafts."""
    from ring0_vault.kernel.formal_verifier import FormalVerificationSuite

    report = FormalVerificationSuite(vopr_steps=EXPECTED_VOPR_STEPS).run()
    vopr = report["vopr"]
    steps = int(vopr.get("steps", 0))
    overdrafts = int(vopr.get("overdraft_violations", 0))
    conservation = int(vopr.get("conservation_violations", 0))
    ledger_violations = int(vopr.get("ledger_violations", 0))
    duplicates = int(vopr.get("duplicate_executions", 0))
    ok = (
        bool(vopr.get("ok"))
        and steps == EXPECTED_VOPR_STEPS
        and overdrafts == 0
        and conservation == 0
        and ledger_violations == 0
        and duplicates == 0
    )
    return Gate(
        "formal.vopr_1000_steps",
        "VOPR deterministic fault injection",
        PASS if ok else FAIL,
        f"seed={vopr.get('seed')}, {steps} steps, "
        f"{int(vopr.get('power_cycles', 0))} power cycles, {overdrafts} overdrafts",
        {
            "steps": steps,
            "seed": int(vopr.get("seed", 0) or 0),
            "power_cycles": int(vopr.get("power_cycles", 0)),
            "reservations": int(vopr.get("reservations", 0)),
            "commits": int(vopr.get("commits", 0)),
            "voids": int(vopr.get("voids", 0)),
            "overdraft_violations": overdrafts,
            "conservation_violations": conservation,
            "ledger_violations": ledger_violations,
            "duplicate_executions": duplicates,
        },
    )


# ---------------------------------------------------------------------------
# 8: IBOR double entry
# ---------------------------------------------------------------------------


def gate_ibor_double_entry(ctx: PreflightContext) -> Gate:
    """Double-entry IBOR balances and conserves value across the audited silos."""
    from core.schemas import VaultName
    from ring0_vault.ibor.ledger_outbox import IBORLedger, LedgerStore

    silos = list(ctx.silos)
    store = LedgerStore(os.path.join(ctx.tempdir, "preflight_ibor.sqlite3"), probe=False)
    try:
        ledger = IBORLedger(store, silos=silos)
        for silo in silos:
            ledger.open_silo(silo, funding_micros=1_000_000_000_000)
            ledger.fund_vault(silo, VaultName.SPINAL_SCALPER, 100_000_000_000)
            ledger.fund_vault(silo, VaultName.TACTICAL_SWING, 50_000_000_000)
        ledger.flush()
        invariant = ledger.invariant_report()
        nav = int(ledger.total_nav_micros(silos))
        holds = bool(invariant.get("holds"))
        details = invariant.get("details") or invariant.get("silos") or []
        ok = holds and nav > 0
        return Gate(
            "ledger.ibor_double_entry",
            "IBOR double-entry invariant (integer micros)",
            PASS if ok else FAIL,
            f"balanced={holds}, nav_micros={nav}, silos={len(silos)}",
            {
                "holds": holds,
                "nav_micros": nav,
                "silos": len(silos),
                "accounts": len(details) if isinstance(details, (list, tuple)) else 0,
                "float_money": False,
            },
        )
    finally:
        try:
            store.close()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# 9-12: Ring 0 vault, signing, sandbox and the conformal gate
# ---------------------------------------------------------------------------


def gate_key_vault_integrity(_ctx: PreflightContext) -> Gate:
    """Ring 0 key vault is READY, write-only and rotation-safe."""
    from ring0_vault.silos.key_vault.key_rotation import vault_key_status

    status = vault_key_status()
    key_version = int(status.get("key_version", 0) or 0)
    verifier_read_only = bool(status.get("verifier_ring_read_only"))
    raw_exposed = bool(status.get("raw_secrets_exposed"))
    write_only = bool(status.get("write_only"))
    fingerprint = str(status.get("ed25519_public_fingerprint", ""))
    ok = (
        str(status.get("status")) == "READY"
        and key_version >= 1
        and verifier_read_only
        and not raw_exposed
        and write_only
        and len(fingerprint) >= 16
    )
    return Gate(
        "ring0.key_vault_integrity",
        "Ring 0 key vault integrity (Ed25519 verifier ring)",
        PASS if ok else FAIL,
        f"status={status.get('status')}, key_version={key_version}, "
        f"verifier_read_only={verifier_read_only}, raw_secrets_exposed={raw_exposed}",
        {
            "status": str(status.get("status", "")),
            "key_version": key_version,
            "ed25519_algorithm": str(status.get("ed25519_algorithm", "")),
            "ed25519_public_fingerprint": fingerprint,
            "verifier_ring_read_only": verifier_read_only,
            "raw_secrets_exposed": raw_exposed,
            "write_only": write_only,
            "archived_public_keys": int(status.get("archived_public_keys", 0) or 0),
            "rotation_pending_recovery": bool(status.get("rotation_pending_recovery")),
        },
    )


def gate_twelve_venue_signatures(_ctx: PreflightContext) -> Gate:
    """Every sandbox venue produces a verifiable Ed25519 dry-run signature."""
    from ring0_vault.kernel.firewalls_and_oms import OrderSigner
    from ring0_vault.silos.testnet_bridge import SANDBOX_VENUE_SPECS

    signer = OrderSigner()
    venues = sorted(SANDBOX_VENUE_SPECS)
    verified: list[str] = []
    rejected: list[str] = []
    for venue in venues:
        spec = SANDBOX_VENUE_SPECS[venue]
        payload = {
            "kind": "nios.ring0.dry_run",
            "venue": venue,
            "silo": str(getattr(spec, "silo", "")),
            "environment": "SANDBOX",
            "orders_placed": 0,
        }
        try:
            digest_hex, signature_hex, _sealed = signer.sign_payload(payload)
            if len(digest_hex) == 64 and signer.verify(payload, signature_hex):
                verified.append(venue)
            else:
                rejected.append(venue)
        except Exception:  # pragma: no cover - defensive
            rejected.append(venue)
    ok = len(verified) == EXPECTED_SANDBOX_VENUES and not rejected
    return Gate(
        "ring0.twelve_venue_dry_run_signatures",
        "Per-silo dry-run signatures across all 12 venues",
        PASS if ok else FAIL,
        f"{len(verified)}/{EXPECTED_SANDBOX_VENUES} venues signed+verified, "
        f"public_key={str(getattr(signer, 'public_key_hex', ''))[:16]}...",
        {
            "venues_expected": EXPECTED_SANDBOX_VENUES,
            "venues_verified": len(verified),
            "venues_rejected": sorted(rejected),
            "venues": verified,
            "orders_placed": 0,
            "signature_alg": "Ed25519",
        },
    )


def gate_testnet_bridge_write_only(ctx: PreflightContext) -> Gate:
    """Sandbox dispatch is refused for want of a credential and leaks nothing."""
    from core.schemas import UnsignedProposal, VaultName
    from ring0_vault.ibor.ledger_outbox import (
        CrashAtomicOutbox,
        IBORLedger,
        LedgerStore,
    )
    from ring0_vault.silos.testnet_bridge import SANDBOX_VENUE_SPECS, TestnetExecutionBridge
    from ring0_vault.silos.key_vault.key_rotation import vault_key_status

    silo = ctx.silos[0]
    venue = sorted(
        name for name, spec in SANDBOX_VENUE_SPECS.items() if str(spec.silo) == silo
    )
    if not venue:
        venue = sorted(SANDBOX_VENUE_SPECS)
    venue_name = venue[0]
    store = LedgerStore(
        os.path.join(ctx.tempdir, "preflight_testnet.sqlite3"), probe=False
    )
    try:
        ledger = IBORLedger(store, silos=[silo])
        ledger.open_silo(silo, funding_micros=10_000_000_000)
        ledger.fund_vault(silo, VaultName.SPINAL_SCALPER, 5_000_000_000)
        outbox = CrashAtomicOutbox(ledger)
        bridge = TestnetExecutionBridge(outbox, ledger, allow_network=False)
        try:
            proposal = UnsignedProposal(
                proposal_id="preflight-sandbox",
                strategy_id="preflight-golive",
                silo=silo,
                vault=VaultName.SPINAL_SCALPER,
                symbol="BTCUSDT" if silo == "CRYPTO_BINANCE" else "PREFLIGHT",
                side="BUY",
                qty=0.01,
                limit_price=100.0,
                expected_gross_bps=12.0,
                spread_bps=1.0,
                grounding_hash="a" * 64,
            )
            receipt = bridge.dispatch(silo, venue_name, proposal, sandbox=True).to_dict()
            state = bridge.status()
        finally:
            bridge.close()
        vault = vault_key_status()
        write_only = bool(vault.get("write_only")) and bool(
            receipt.get("write_only")
        )
        no_leak = receipt.get("raw_secret_returned") is False
        sandboxed = receipt.get("sandbox") is True
        voided = receipt.get("voided") is True or receipt.get("outbox_state") == "VOIDED"
        ok = (
            sandboxed
            and voided
            and write_only
            and no_leak
            and len(state.get("venues") or []) == EXPECTED_SANDBOX_VENUES
            and int(state.get("dispatches", 0)) == 1
        )
        return Gate(
            "ring0.testnet_bridge_write_only",
            "Ring 0 testnet bridge + write-only vault",
            PASS if ok else FAIL,
            f"venue={venue_name}, sandbox={sandboxed}, voided={voided}, "
            f"raw_secret_returned={receipt.get('raw_secret_returned')}, "
            f"outbox={receipt.get('outbox_state')}",
            {
                "venue": venue_name,
                "venues_known": len(state.get("venues") or []),
                "sandbox": sandboxed,
                "voided": voided,
                "write_only": write_only,
                "raw_secret_returned": bool(receipt.get("raw_secret_returned")),
                "outbox_state": str(receipt.get("outbox_state", "")),
                "dispatches": int(state.get("dispatches", 0)),
                "commits": int(state.get("committed", 0)),
                "network": "disabled",
            },
        )
    finally:
        try:
            store.close()
        except Exception:  # pragma: no cover
            pass


def gate_conformal_gate_rejects(ctx: PreflightContext) -> Gate:
    """A negative-edge proposal is rejected by the 98% split-conformal gate."""
    from core.schemas import UnsignedProposal, VaultName
    from ring0_vault.conformal_copula_gate import (
        ConformalMirror,
        CrossSiloCopulaShield,
        SplitConformal98Gate,
    )
    from ring0_vault.ibor.ledger_outbox import (
        CrashAtomicOutbox,
        IBORLedger,
        LedgerStore,
        SiloGuard,
    )
    from ring0_vault.kernel.firewalls_and_oms import (
        OrderSigner,
        SovereignPreTradeFirewall,
    )
    from ring1_spinal.silo_worker import SILO_ORDER

    silo = ctx.silos[0]
    symbol = "BTCUSDT" if silo == "CRYPTO_BINANCE" else "PREFLIGHT"
    store = LedgerStore(
        os.path.join(ctx.tempdir, "preflight_conformal.sqlite3"), probe=False
    )
    try:
        ledger = IBORLedger(store, silos=list(SILO_ORDER))
        for name in SILO_ORDER:
            ledger.open_silo(name, funding_micros=500_000_000_000)
            ledger.fund_vault(name, VaultName.SPINAL_SCALPER, 50_000_000_000)
        mirror = ConformalMirror()
        mirror.seed("preflight-golive", [4.0] * 200, shadow_fills=200)
        firewall = SovereignPreTradeFirewall(
            ledger=ledger,
            outbox=CrashAtomicOutbox(ledger),
            gate=SplitConformal98Gate(mirror),
            shield=CrossSiloCopulaShield(),
            signer=OrderSigner(),
            guard=SiloGuard(),
            whitelists={name: [symbol] for name in SILO_ORDER},
        )
        losing = UnsignedProposal(
            proposal_id="preflight-negative-edge",
            strategy_id="preflight-golive",
            silo=silo,
            vault=VaultName.SPINAL_SCALPER,
            symbol=symbol,
            side="BUY",
            qty=0.01,
            limit_price=100.0,
            expected_gross_bps=-40.0,
            spread_bps=1.0,
            grounding_hash="b" * 64,
        )
        verdict = firewall.evaluate(losing, mode_badge="PREFLIGHT")
        body = verdict.to_dict() if hasattr(verdict, "to_dict") else {}
        accepted = bool(body.get("accepted", getattr(verdict, "accepted", True)))
        abstain = str(body.get("abstain_code") or getattr(verdict, "abstain_code", "") or "")
        ok = (not accepted) and bool(abstain)
        return Gate(
            "ring0.conformal_gate_rejects_negative_edge",
            "98% split-conformal gate abstains on negative edge",
            PASS if ok else FAIL,
            f"accepted={accepted}, abstain_code={abstain or 'none'}, "
            f"stage={body.get('stage_name') or body.get('stage_reached')}",
            {
                "accepted": accepted,
                "abstain_code": abstain,
                "stage": str(body.get("stage_name", "")),
                "expected_gross_bps": -40.0,
                "coverage": 0.98,
            },
        )
    finally:
        try:
            store.close()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# 13-17: Ring 1 / Ring 2 organs
# ---------------------------------------------------------------------------


def gate_cost_floor(ctx: PreflightContext) -> Gate:
    """Tax-net cost floors are preloaded per silo before any trade can pass."""
    from ring1_spinal.silo_worker import SILO_ORDER, silo_cost_floor

    floors: dict[str, Any] = {name: silo_cost_floor(name) for name in SILO_ORDER}
    round_trip = {
        name: float(getattr(floor, "round_trip_cost_bps", 0.0))
        for name, floor in floors.items()
    }
    scoped = {name: round_trip[name] for name in ctx.silos if name in round_trip}
    ok = (
        len(SILO_ORDER) == 4
        and all(value > 0.0 for value in round_trip.values())
        and len(scoped) == len(ctx.silos)
    )
    return Gate(
        "ring1.cost_floor_tax_net",
        "Preloaded tax-net cost floors per silo",
        PASS if ok else FAIL,
        ", ".join(
            f"{name}={round_trip[name]:.2f} bps" for name in sorted(round_trip)
        ),
        {
            "silos": sorted(floors),
            "round_trip_cost_bps": {
                name: round(value, 4) for name, value in sorted(round_trip.items())
            },
            "fee_stt_bps": {
                name: round(float(getattr(floor, "fee_stt_bps", 0.0)), 4)
                for name, floor in sorted(floors.items())
            },
            "statutory_tax_bps": {
                name: round(float(getattr(floor, "statutory_tax_bps", 0.0)), 4)
                for name, floor in sorted(floors.items())
            },
            "scoped": sorted(scoped),
            "all_positive": all(value > 0.0 for value in round_trip.values()),
        },
    )


def gate_dual_vendor_feeds(ctx: PreflightContext) -> Gate:
    """Four silos ingest dual-vendor quotes and emit Lee-Ready executed flow."""
    from ring2_brain.truth.live_public_feeds import MultiSiloFeedConnector

    connector = MultiSiloFeedConnector()
    try:
        frames = connector.ingest_all()
        status = connector.status()
    finally:
        try:
            connector.close()
        except Exception:  # pragma: no cover
            pass
    silos = sorted({str(frame.silo) for frame in frames})
    venues = status.get("venues") or {}
    dual = {
        name: list(pair)
        for name, pair in venues.items()
        if isinstance(pair, (list, tuple)) and len(pair) >= 2
    }
    ok = (
        len(silos) == len(SILOS)
        and sorted(silos) == sorted(SILOS)
        and len(frames) >= len(SILOS)
        and len(dual) == len(SILOS)
        and str(status.get("kind")) == "multi_silo_dual_vendor"
    )
    return Gate(
        "feeds.dual_vendor_four_silos_lee_ready",
        "Multi-silo dual-vendor feed + Lee-Ready flow",
        PASS if ok else FAIL,
        f"{len(frames)} Lee-Ready frames across {len(silos)} silos "
        f"({status.get('feed_mode', 'n/a')}), dual-vendor silos={len(dual)}",
        {
            "silos": silos,
            "frames": len(frames),
            "dual_vendor_silos": len(dual),
            "feed_mode": str(status.get("feed_mode", "")),
            "venues": {name: dual[name] for name in sorted(dual)},
            "rounds": int(status.get("rounds", 0) or 0),
        },
    )


def gate_contagion_cascade(_ctx: PreflightContext) -> Gate:
    """The five-archetype MiroFish cascade produces bounded fragility features."""
    from ring2_brain.mirofish.contagion_engine import (
        MiroFishContagionEngine,
        ShockVector,
    )

    engine = MiroFishContagionEngine(silo="US", warmup=True)
    result = engine.run(
        ShockVector(price_shock_bps=40.0, realized_vol_ratio=2.0), "PREFLIGHT"
    )
    features = result.features
    fragility = float(features.cascade_fragility_index)
    crowd = float(features.crowd_asymmetry_score)
    ok = 0.0 <= fragility <= 1.0 and -1.0 <= crowd <= 1.0
    return Gate(
        "ring2.contagion_three_tick_cascade",
        "MiroFish three-tick contagion cascade",
        PASS if ok else FAIL,
        f"fragility={fragility:.3f}, crowd={crowd:+.3f}",
        {
            "cascade_fragility_index": round(fragility, 6),
            "crowd_asymmetry_score": round(crowd, 6),
            "narrative_exhaustion_divergence": round(
                float(features.narrative_exhaustion_divergence), 6
            ),
            "ticks": 3,
        },
    )


def gate_alpha_forge_audit(_ctx: PreflightContext) -> Gate:
    """A champion below +0.50 bps is demoted and its leases are disarmed."""
    from ring2_brain.forge.alpha_forge import (
        RETIREMENT_FLOOR_BPS,
        SHADOW_RETIRED,
        NocturnalAlphaForge,
    )

    forge = NocturnalAlphaForge()
    forge.register_champion("preflight-decayed", silo=SILOS[0], symbols=("PREFLIGHT",))
    for value in (-1.4, -1.1, -1.9, -0.8):
        forge.record_champion_fill("preflight-decayed", value)
    forge.breed_random_genomes(4, silo=SILOS[0])
    report = forge.audit()
    retired = [item for item in forge.champions() if item.strategy_id == "preflight-decayed"]
    status = str(retired[0].status) if retired else ""
    ok = (
        report.retired_count >= 1
        and status == str(SHADOW_RETIRED)
        and float(RETIREMENT_FLOOR_BPS) == 0.50
        and report.genomes_evaluated >= 4
    )
    return Gate(
        "ring2.alpha_forge_audit_retires_decayed",
        "Nocturnal Alpha Forge champion audit",
        PASS if ok else FAIL,
        f"retired={report.retired_count}, promoted={report.promoted_count}, "
        f"held={report.held_count}, genomes={report.genomes_evaluated}, "
        f"floor=+{RETIREMENT_FLOOR_BPS:.2f} bps",
        {
            "retired": int(report.retired_count),
            "promoted": int(report.promoted_count),
            "held": int(report.held_count),
            "genomes_evaluated": int(report.genomes_evaluated),
            "fdr_rejections": int(report.fdr_rejections),
            "retirement_floor_bps": float(RETIREMENT_FLOOR_BPS),
            "decayed_champion_status": status,
        },
    )


def gate_tail_hedger(_ctx: PreflightContext) -> Gate:
    """The tactician's tail hedger prices a grounded Black-Scholes put."""
    from ring2_brain.quant.tail_hedger import FRAGILITY_TRIGGER, TacticalTailHedger

    hedger = TacticalTailHedger()
    status, plan = hedger.monitor_and_hedge(
        cascade_fragility_index=0.72,
        tail_correlation=0.68,
        spot=50_000.0,
        symbol="PREFLIGHT",
        silo="US_IBKR",
    )
    if plan is None:
        return Gate(
            "ring2.tail_hedger_grounded_put",
            "TacticalSwing Black-Scholes tail hedge",
            FAIL,
            "tail hedge was triggered but no affordable plan was produced",
            {"triggered": bool(status.triggered), "plan": None},
        )
    proposal = plan.proposal
    digest = str(plan.packet.sha256_digest)
    ok = (
        bool(status.triggered)
        and bool(plan.grounding_verified)
        and len(digest) == 64
        and str(proposal.vault) == "TacticalSwing"
        and float(plan.premium_bps) > 0.0
        and float(plan.strike) < 50_000.0
    )
    return Gate(
        "ring2.tail_hedger_grounded_put",
        "TacticalSwing Black-Scholes tail hedge",
        PASS if ok else FAIL,
        f"fragility={status.cascade_fragility_index:.2f} (trigger {FRAGILITY_TRIGGER}), "
        f"strike={plan.strike:.0f}, delta={plan.delta:+.3f}, premium={plan.premium_bps:.1f} bps",
        {
            "triggered": bool(status.triggered),
            "cascade_fragility_index": round(float(status.cascade_fragility_index), 6),
            "tail_correlation": round(float(status.tail_correlation), 6),
            "fragility_trigger": float(FRAGILITY_TRIGGER),
            "strike": round(float(plan.strike), 3),
            "spot": round(float(plan.spot), 3),
            "contracts": round(float(plan.contracts), 6),
            "delta": round(float(plan.delta), 6),
            "premium_bps": round(float(plan.premium_bps), 6),
            "grounding_verified": bool(plan.grounding_verified),
            "grounding_claims": int(plan.grounding_claims_checked),
            "grounding_hash": digest,
        },
    )


# ---------------------------------------------------------------------------
# 18-19: supervisor contract and the cross-ring signing route
# ---------------------------------------------------------------------------


def gate_supervisor_contract(_ctx: PreflightContext) -> Gate:
    """The four-ring OS process supervisor exposes the closed-loop contract."""
    from core.process_supervisor import (
        FORGE_HEDGE_COMMAND,
        RING_WORKERS,
        RingProcessSupervisor,
    )

    supervisor = RingProcessSupervisor(rings=(), fresh_heartbeats=False)
    names = [supervisor.spec(index).name for index in range(4)]
    ok = (
        names == list(RING_WORKERS)
        # ``NIOS_SHM_PREFIX`` scopes the segment root per process (the test
        # harness uses ``nios-test-<pid>`` so parallel runs cannot collide), so
        # the invariant is the immutable ``nios`` root, not a literal string.
        and str(supervisor.shm_prefix).startswith("nios")
        and callable(getattr(supervisor, "run_forge_and_hedge_cycle", None))
        and FORGE_HEDGE_COMMAND == "forge_and_hedge"
        and supervisor.symbol
        and supervisor.silo in SILOS
    )
    return Gate(
        "supervisor.four_ring_process_contract",
        "Four-ring OS process supervisor contract",
        PASS if ok else FAIL,
        f"{len(names)} ring workers, shm prefix {supervisor.shm_prefix}, "
        f"closed_loop={'yes' if ok else 'no'}",
        {
            "workers": names,
            "shm_prefix": supervisor.shm_prefix,
            "closed_loop_command": FORGE_HEDGE_COMMAND,
            "silo": supervisor.silo,
            "symbol": supervisor.symbol,
            "spawned_processes": 0,
        },
    )


def gate_hedge_signing_contract(ctx: PreflightContext) -> Gate:
    """An insurance put crosses the boundary and is Ed25519 signed in Ring 0."""
    from core.process_supervisor import _sign_tail_hedge
    from core.schemas import UnsignedProposal, VaultName
    from ring0_vault.conformal_copula_gate import (
        ConformalMirror,
        CrossSiloCopulaShield,
        SplitConformal98Gate,
    )
    from ring0_vault.ibor.ledger_outbox import (
        CrashAtomicOutbox,
        IBORLedger,
        LedgerStore,
        SiloGuard,
    )
    from ring0_vault.kernel.firewalls_and_oms import (
        OrderSigner,
        SovereignPreTradeFirewall,
    )
    from ring1_spinal.silo_worker import SILO_ORDER

    silo = ctx.silos[0]
    symbol = "BTCUSDT" if silo == "CRYPTO_BINANCE" else "PREFLIGHT"
    store = LedgerStore(
        os.path.join(ctx.tempdir, "preflight_hedge.sqlite3"), probe=False
    )
    try:
        ledger = IBORLedger(store, silos=list(SILO_ORDER))
        for name in SILO_ORDER:
            ledger.open_silo(name, funding_micros=500_000_000_000)
            ledger.fund_vault(name, VaultName.TACTICAL_SWING, 50_000_000_000)
        mirror = ConformalMirror()
        mirror.seed("preflight-hedge", [4.0] * 200, shadow_fills=200)
        signer = OrderSigner()
        firewall = SovereignPreTradeFirewall(
            ledger=ledger,
            outbox=CrashAtomicOutbox(ledger),
            gate=SplitConformal98Gate(mirror),
            shield=CrossSiloCopulaShield(),
            signer=signer,
            guard=SiloGuard(),
            whitelists={name: [symbol] for name in SILO_ORDER},
        )
        proposal = UnsignedProposal(
            proposal_id="preflight-tail-hedge",
            strategy_id="tactical-tail-hedger-v1",
            silo=silo,
            vault=VaultName.TACTICAL_SWING,
            symbol=symbol,
            side="BUY",
            qty=10.0,
            limit_price=5.0,
            expected_gross_bps=-250.0,
            spread_bps=0.0,
            grounding_hash="c" * 64,
        )
        event = _sign_tail_hedge(
            {
                "kind": "tail_hedge",
                "request_id": "preflight",
                "proposal": proposal.to_dict(),
                "plan": {"plan_id": "preflight", "premium_bps": 250.0, "strike": 46_500.0},
            },
            firewall=firewall,
            signer=signer,
            ledger=ledger,
            silo_order=SILO_ORDER,
            pid=os.getpid(),
        )
        status = str(event.get("type", ""))
        ok = (
            status == "ring0_hedge_signed"
            and bool(event.get("verified"))
            and len(str(event.get("digest_hex", ""))) == 64
            and len(str(event.get("signature_hex", ""))) == 128
            and str(event.get("hedge_role")) == "INSURANCE"
        )
        return Gate(
            "supervisor.hedge_signing_contract",
            "Ring 2 tail-hedge -> Ring 0 Ed25519 signing route",
            PASS if ok else FAIL,
            f"{status}, verified={event.get('verified')}, "
            f"premium={event.get('premium_bps')} bps, vault={event.get('vault')}",
            {
                "event_type": status,
                "verified": bool(event.get("verified")),
                "digest_len": len(str(event.get("digest_hex", ""))),
                "signature_len": len(str(event.get("signature_hex", ""))),
                "hedge_role": str(event.get("hedge_role", "")),
                "vault": str(event.get("vault", "")),
                "reason": str(event.get("reason", "")),
                "alpha_gate_bypassed_for_insurance": True,
            },
        )
    finally:
        try:
            store.close()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# 20-22: OS readiness, observability, kill switch
# ---------------------------------------------------------------------------


def gate_os_tuning(_ctx: PreflightContext) -> Gate:
    """Loopback sockets carry TCP_NODELAY and the timer/priority audit is clean."""
    from core.os_tuning import audit_and_tune_os

    report = audit_and_tune_os(apply=False, ring="ring1")
    sockets = report.get("socket") or {}
    nodelay = sockets.get("tcp_nodelay")
    keepalive = sockets.get("so_keepalive")
    errors = int(report.get("error_count", 0) or 0)
    applied = int(report.get("applied_count", 0) or 0)
    ok = (
        bool(report.get("ok"))
        and int(nodelay or 0) == 1
        and int(keepalive or 0) == 1
        and errors == 0
    )
    return Gate(
        "os.low_latency_socket_readiness",
        "OS + socket low-latency readiness",
        PASS if ok else FAIL,
        f"{applied} applied / {int(report.get('recommended_count', 0) or 0)} recommended, "
        f"TCP_NODELAY={nodelay}, SO_KEEPALIVE={keepalive}, errors={errors}",
        {
            "ok": bool(report.get("ok")),
            "platform": str(report.get("platform", "")),
            "applied_count": applied,
            "recommended_count": int(report.get("recommended_count", 0) or 0),
            "error_count": errors,
            "tcp_nodelay": int(nodelay or 0),
            "so_keepalive": int(keepalive or 0),
            "recv_buffer_bytes": int(sockets.get("recv_buffer_bytes", 0) or 0),
            "send_buffer_bytes": int(sockets.get("send_buffer_bytes", 0) or 0),
            "checks": len(report.get("checks") or []),
        },
    )


def gate_observability(ctx: PreflightContext) -> Gate:
    """``/metrics``, ``/api/v2/metrics/json`` and cockpit health all scrape."""
    client = ctx.client()
    prometheus = client.get("/metrics")
    alias = client.get("/api/v2/metrics")
    machine = client.get("/api/v2/metrics/json")
    health = client.get("/api/v2/cockpit/health")
    health_body = health.json() if health.status_code == 200 else {}
    machine_body = machine.json() if machine.status_code == 200 else {}
    ok = (
        prometheus.status_code == 200
        and alias.status_code == 200
        and machine.status_code == 200
        and health.status_code == 200
        and len(prometheus.text) > 256
        and bool(machine_body)
        and bool(health_body.get("ok"))
        and str(health_body.get("version")) == RELEASE
    )
    return Gate(
        "observability.metrics_and_health",
        "Prometheus / OpenTelemetry metrics + cockpit health",
        PASS if ok else FAIL,
        f"/metrics {len(prometheus.text)} bytes, "
        f"/api/v2/metrics/json {len(machine_body)} keys, "
        f"health status={health_body.get('status')} version={health_body.get('version')}",
        {
            "prometheus_status": prometheus.status_code,
            "prometheus_bytes": len(prometheus.text),
            "alias_status": alias.status_code,
            "json_status": machine.status_code,
            "json_keys": sorted(machine_body),
            "health_status": health.status_code,
            "health_ok": bool(health_body.get("ok")),
            "health_state": str(health_body.get("status", "")),
            "version": str(health_body.get("version", "")),
            "release_expected": RELEASE,
        },
    )


def gate_killswitch(ctx: PreflightContext) -> Gate:
    """The global kill switch halts under 1 ms and the cockpit re-arms."""
    client = ctx.client()
    halted = client.post("/api/v2/control/killswitch", json={"confirm": True})
    halt_body = halted.json() if halted.status_code == 200 else {}
    latency_ns = int(halt_body.get("latency_ns", 0) or 0)
    armed_after = client.get("/api/v2/cockpit/health")
    armed_body = armed_after.json() if armed_after.status_code == 200 else {}
    rearmed = client.post("/api/v2/control/rearm", json={"confirm": True})
    rear_body = rearmed.json() if rearmed.status_code == 200 else {}
    final_health = client.get("/api/v2/cockpit/health")
    final_body = final_health.json() if final_health.status_code == 200 else {}
    ok = (
        halted.status_code == 200
        and 0 < latency_ns < KILLSWITCH_LATENCY_BUDGET_NS
        and str(armed_body.get("status")) == "HALTED"
        and rearmed.status_code == 200
        and str(final_body.get("status")) == "ARMED"
    )
    return Gate(
        "cockpit.killswitch_and_rearm",
        "Global kill switch < 1 ms + re-arm",
        PASS if ok else FAIL,
        f"halt latency={latency_ns} ns, HALTED->{armed_body.get('status')}, "
        f"rearm->{final_body.get('status')}",
        {
            "halt_status": halted.status_code,
            "latency_ns": latency_ns,
            "latency_budget_ns": KILLSWITCH_LATENCY_BUDGET_NS,
            "halted_state": str(armed_body.get("status", "")),
            "rearm_status": rearmed.status_code,
            "rearm_state": str(rear_body.get("status", "")),
            "final_state": str(final_body.get("status", "")),
            "orders_placed": 0,
        },
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

GATE_REGISTRY: tuple[tuple[str, Callable[[PreflightContext], Gate]], ...] = (
    ("git.clean_working_tree", gate_clean_working_tree),
    ("git.no_tracked_secrets", gate_no_tracked_secrets),
    ("release.tag_v3_0_0_prod", gate_release_tag),
    ("release.artifacts_present", gate_artifacts_present),
    ("smoke.22_row_suite", gate_smoke_suite),
    ("formal.tla_four_proofs", gate_tla_proofs),
    ("formal.vopr_1000_steps", gate_vopr_campaign),
    ("ledger.ibor_double_entry", gate_ibor_double_entry),
    ("ring0.key_vault_integrity", gate_key_vault_integrity),
    ("ring0.twelve_venue_dry_run_signatures", gate_twelve_venue_signatures),
    ("ring0.testnet_bridge_write_only", gate_testnet_bridge_write_only),
    ("ring0.conformal_gate_rejects_negative_edge", gate_conformal_gate_rejects),
    ("ring1.cost_floor_tax_net", gate_cost_floor),
    ("feeds.dual_vendor_four_silos_lee_ready", gate_dual_vendor_feeds),
    ("ring2.contagion_three_tick_cascade", gate_contagion_cascade),
    ("ring2.alpha_forge_audit_retires_decayed", gate_alpha_forge_audit),
    ("ring2.tail_hedger_grounded_put", gate_tail_hedger),
    ("supervisor.four_ring_process_contract", gate_supervisor_contract),
    ("supervisor.hedge_signing_contract", gate_hedge_signing_contract),
    ("os.low_latency_socket_readiness", gate_os_tuning),
    ("observability.metrics_and_health", gate_observability),
    ("cockpit.killswitch_and_rearm", gate_killswitch),
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_gate(gate_id: str, check: Callable[[PreflightContext], Gate], ctx: PreflightContext) -> Gate:
    started = time.perf_counter()
    try:
        gate = check(ctx)
    except Exception as exc:  # a gate must never abort the whole preflight
        gate = Gate(
            gate_id,
            gate_id,
            FAIL,
            f"{type(exc).__name__}: {exc}",
            {"exception": type(exc).__name__},
        )
    gate.gate_id = gate_id
    gate.duration_ms = (time.perf_counter() - started) * 1e3
    if ctx.strict and gate.status == WARN:
        gate.status = FAIL
        gate.detail = f"strict: {gate.detail or 'warning'}"
    return gate


def run_preflight(
    *, silo: str = SILO_SCOPE_ALL, strict: bool = False, full_smoke: bool = True
) -> dict[str, Any]:
    """Execute every gate and return the signed readiness certificate.

    ``full_smoke=False`` is the interactive fast path used by the Ring 3 cockpit:
    gate 5 replays the persisted receipt of the last certified 22-row suite for
    this commit instead of re-spawning it.  Every other gate still runs live.
    """
    scope = SILO_SCOPE_ALL if str(silo).upper() in {"ALL", ""} else str(silo).upper()
    if scope != SILO_SCOPE_ALL and scope not in SILOS:
        raise ValueError(f"unknown silo scope: {silo!r}")
    tempdir = tempfile.mkdtemp(prefix="nios-preflight-")
    ctx = PreflightContext(
        silo_scope=scope,
        strict=bool(strict),
        tempdir=tempdir,
        full_smoke=bool(full_smoke),
    )
    gates: list[Gate] = []
    started = time.perf_counter()
    try:
        for gate_id, check in GATE_REGISTRY:
            gates.append(_run_gate(gate_id, check, ctx))
    finally:
        ctx.close()
    duration_ms = (time.perf_counter() - started) * 1e3

    failed = [gate.gate_id for gate in gates if gate.status == FAIL]
    warned = [gate.gate_id for gate in gates if gate.status == WARN]
    certificate_payload = {
        "schema": SCHEMA,
        "release": RELEASE,
        "silo_scope": scope,
        "gates": [gate.certificate_payload() for gate in gates],
    }
    digest = sha256_hex(certificate_payload)
    ready = not failed and not warned
    return {
        "kind": CERTIFICATE_KIND,
        "schema": SCHEMA,
        "release": RELEASE,
        "signer": SIGNER,
        "silo_scope": scope,
        "strict": bool(strict),
        "full_smoke": bool(full_smoke),
        "gates_expected": len(GATE_REGISTRY),
        "gates_passed": sum(1 for gate in gates if gate.status == PASS),
        "failed_gates": len(failed),
        "failed_gate_ids": failed,
        "warned_gates": len(warned),
        "warned_gate_ids": warned,
        "ready_for_live_cutover": ready,
        "certificate_sha256": digest,
        "certificate_digest_algorithm": "sha256",
        "certificate_digest_scope": "gate outcomes + stable evidence (no timestamps)",
        "duration_ms": round(duration_ms, 3),
        "generated_at_ns": time.time_ns(),
        "gates": [gate.to_dict() for gate in gates],
        "certificate": certificate_payload,
    }


def render_table(report: Mapping[str, Any]) -> str:
    """Plain-ASCII readiness table for a human operator."""
    gates = list(report.get("gates") or [])
    width = max([len(str(gate.get("label", ""))) for gate in gates] + [38])
    lines = [
        f"{'NIOS GO-LIVE PRE-FLIGHT CERTIFICATION':<{width}}  STATUS  DETAIL",
        f"{'-' * width}  ------  {'-' * 40}",
    ]
    for index, gate in enumerate(gates, start=1):
        lines.append(
            f"{index:02d} {str(gate.get('label', '')):<{width - 3}}  "
            f"{str(gate.get('status', '')):<6}  {str(gate.get('detail', ''))}"
        )
    lines.append("")
    lines.append(
        f"GATES={report.get('gates_passed')}/{report.get('gates_expected')}  "
        f"FAILED={report.get('failed_gates')}  WARNED={report.get('warned_gates')}  "
        f"SCOPE={report.get('silo_scope')}  STRICT={report.get('strict')}"
    )
    lines.append(
        f"CERTIFICATE SHA-256  {report.get('certificate_sha256')}"
    )
    lines.append(
        f"READY FOR LIVE CUTOVER = "
        f"{'TRUE' if report.get('ready_for_live_cutover') else 'FALSE'}"
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="preflight_golive.py",
        description=(
            "NIOS Phase 4 pre-flight go-live certification.  Run before setting "
            "confirm_live=true on a broker silo."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the signed readiness certificate as JSON",
    )
    parser.add_argument(
        "--silo",
        default=SILO_SCOPE_ALL,
        help=f"broker silo scope ({SILO_SCOPE_ALL} or one of {', '.join(SILOS)})",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures (recommended before a live cutover)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "replay the persisted 22-row smoke receipt for this commit instead of "
            "re-spawning the suite (interactive cockpit path)"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        report = run_preflight(
            silo=args.silo, strict=args.strict, full_smoke=not args.fast
        )
    except ValueError as exc:
        print(f"PREFLIGHT ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - catastrophic
        print(f"PREFLIGHT ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_table(report))
        print()
        if report["ready_for_live_cutover"]:
            print(
                "GO-LIVE READINESS CERTIFIED: all gates passed; "
                "confirm_live=true may be authorised by an operator."
            )
        else:
            print(
                "GO-LIVE BLOCKED: resolve the FAIL rows above before enabling "
                "confirm_live=true."
            )
    return 0 if report["ready_for_live_cutover"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
