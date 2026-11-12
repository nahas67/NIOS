"""Unified NIOS production server and smoke-test entry point (Step 8).

Two modes, one process model:

* ``python scripts/run_nios.py --smoke-test`` boots the complete 4-ring
  ``NIOSCoreOrchestrator``, runs a self-check across every ring (IBOR invariant,
  shared-memory lease table, 480+ factor scanner, MiroFish cascade, Forge,
  control plane, five MCP tools, static React bundle), prints a health table and
  exits ``0`` only when every check passes.
* ``python scripts/run_nios.py`` serves the unified FastAPI app (``/api/v2/*``
  REST + SSE + MCP + the compiled React 19 bundle at ``/``) with uvicorn.  The
  bind port is ``--port`` or ``$NIOS_PORT`` when the operator forces one;
  otherwise a busy ``8000`` falls back to the first free port in
  ``8000..8010`` and the exact live URL is printed (Operator Note #2).

The four ``--role`` modes exist for the network-segmented docker-compose
topology: each ring process boots only its own dependencies, prints a health
line, and then stays alive until SIGTERM.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "PORT_SCAN_START",
    "PORT_SCAN_END",
    "build_runtime",
    "fastapi_app",
    "live_url",
    "main",
    "resolve_port",
    "run_multiprocess",
    "run_role",
    "run_smoke",
    "run_server",
]

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

#: Operator Note #2: when the default port is busy and no port was forced,
#: the launcher takes the first free port in this inclusive range.
PORT_SCAN_START = 8000
PORT_SCAN_END = 8010

SMOKE_UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "dist"

# ``scripts/`` is ``sys.path[0]`` when this file is executed directly; it is a
# namespace package under the repository root when imported as a module.
try:  # pragma: no cover - import shim
    from nios_port_check import PortUnavailable
    from nios_port_check import resolve_port as _resolve_port
except ImportError:  # pragma: no cover - ``python -m scripts.run_nios``
    from scripts.nios_port_check import PortUnavailable
    from scripts.nios_port_check import resolve_port as _resolve_port


def resolve_port(
    host: str,
    requested: int = DEFAULT_PORT,
    *,
    allow_fallback: bool = True,
) -> int:
    """Port this launch will bind (Operator Note #2 fallback included).

    ``allow_fallback=False`` is used when the operator explicitly forced a port
    via ``--port`` / ``NIOS_PORT``: the launch then fails loudly instead of
    moving a pinned service.
    """
    return _resolve_port(
        host,
        requested,
        allow_fallback=allow_fallback,
        start=PORT_SCAN_START,
        end=PORT_SCAN_END,
    )


def live_url(host: str, port: int) -> str:
    """Human-clickable URL for a bind host (``0.0.0.0`` -> ``127.0.0.1``)."""
    display = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    if ":" in display and not display.startswith("["):
        display = f"[{display}]"
    return f"http://{display}:{int(port)}"


def build_runtime(
    workspace: str | Path | None = None,
    *,
    key_vault_root: str | Path | None = None,
):
    """Boot the complete 4-ring runtime (or a caller-provided workspace)."""
    from core.orchestrator import NIOSCoreOrchestrator

    resolved = (
        Path(workspace)
        if workspace is not None
        else Path(tempfile.mkdtemp(prefix="nios-runtime-"))
    )
    return NIOSCoreOrchestrator(
        workspace=resolved,
        key_vault_root=key_vault_root,
        seed_fills=60,
        funding_micros=5_000_000_000_000,
        daily_loss_stop_micros=5_000_000_000,
    )


def fastapi_app(runtime):
    """The unified FastAPI app (REST + SSE + MCP + static React bundle)."""
    return runtime.control_app()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


class _HealthTable:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, "PASS" if ok else "FAIL", detail))

    def check(self, name: str, fn) -> None:
        try:
            detail = fn()
        except Exception as exc:  # a failed check is data, not a crash
            self.add(name, False, f"{type(exc).__name__}: {exc}")
            return
        self.add(name, True, "" if detail is None else str(detail))

    @property
    def ok(self) -> bool:
        return all(status == "PASS" for _, status, _ in self.rows)

    def render(self) -> str:
        width = max(len(name) for name, _, _ in self.rows) if self.rows else 20
        lines = [
            f"{'NIOS PHASE 4 FINAL CLOSURE SMOKE CHECK':<{width}}  STATUS  DETAIL",
            f"{'-' * width}  ------  {'-' * 42}",
        ]
        for name, status, detail in self.rows:
            lines.append(f"{name:<{width}}  {status:<6}  {detail}")
        return "\n".join(lines)


def run_smoke(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    workspace: str | Path | None = None,
) -> int:
    """Boot all four rings, self-check them, print the table, return exit code."""
    del host, port  # the smoke client is in-process; kept for CLI symmetry
    from fastapi.testclient import TestClient

    from core.orchestrator import PRIMARY_SILO

    table = _HealthTable()
    runtime = None
    try:
        started = time.perf_counter()
        runtime = build_runtime(workspace)
        boot_ms = (time.perf_counter() - started) * 1e3

        # ---- Ring 0: Sovereign Vault -----------------------------------
        def ring0() -> str:
            report = runtime.ledger.invariant_report()
            if not report.get("holds"):
                raise AssertionError(f"IBOR invariant violated: {report['violations']}")
            vaults = runtime.ledger.vault_balances(PRIMARY_SILO)
            return (
                f"IBOR balanced, {report['accounts']} accounts, "
                f"{len(vaults)} vaults, "
                f"nav_micros={runtime.ledger.silo_nav_micros(PRIMARY_SILO)}"
            )

        table.check("Ring 0 sovereign vault / IBOR", ring0)

        # ---- Ring 1: Spinal Reflex -------------------------------------
        def ring1() -> str:
            stats = dict(runtime.engine.stats)
            assert runtime.engine.shadow_twin is runtime.twin
            assert runtime.lease_table is not None
            return (
                f"engine={runtime.engine.strategy_id}, ticks={stats['ticks']}, "
                f"shadow_fills={runtime.twin.shadow_fill_count(runtime.strategy_id)}, "
                f"leases={runtime.lease_table.active_count(time.monotonic_ns())}"
            )

        table.check("Ring 1 spinal engine / shadow twin", ring1)

        # ---- Ring 2: quant / contagion / truth / forge -----------------
        def ring2_quant() -> str:
            assert runtime.scanner.factor_count >= 480, runtime.scanner.factor_count
            result = runtime.scan_universe(
                n_symbols=500, n_periods=32, commit=False
            )
            assert result.candidates, "scanner produced no candidates"
            return (
                f"{runtime.scanner.factor_count} factors, "
                f"{result.universe_size}->{len(result.candidates)} candidates, "
                f"{result.elapsed_ms:.1f} ms"
            )

        table.check("Ring 2 vectorized factor scanner", ring2_quant)

        def ring2_contagion() -> str:
            result = runtime.run_contagion(price_shock_bps=40.0)
            features = result.features
            return (
                f"fragility={features.cascade_fragility_index:.3f}, "
                f"crowd={features.crowd_asymmetry_score:+.3f}, "
                f"{result.latency_ms:.2f} ms"
            )

        table.check("Ring 2 MiroFish 3-tick cascade", ring2_contagion)

        def ring2_forge() -> str:
            report = runtime.forge.audit()
            return (
                f"genomes={report.genomes_evaluated}, "
                f"retired={len(report.retired)}, promoted={len(report.promoted)}, "
                f"next=00:00 UTC"
            )

        table.check("Ring 2 Nocturnal Alpha Forge", ring2_forge)

        def ring2_skills() -> str:
            catalog = runtime.skills.describe()
            assert catalog["count"] == 4, catalog["count"]
            return (
                f"{catalog['count']} skills, "
                f"frontmatter_tokens={catalog['token_overhead']}, "
                f"bodies_loaded={catalog['bodies_loaded']}"
            )

        table.check("Ring 2 progressive SKILL.md registry", ring2_skills)

        # ---- Phase 2 Step 4: formal proofs / VOPR / backends / hedger ---
        def formal_tla() -> str:
            result = runtime.formal_verifier.run()
            tla = result["tla"]
            assert tla["total_states"] > 500, tla["total_states"]
            assert tla["total_counterexamples"] == 0, tla["total_counterexamples"]
            names = {spec["name"] for spec in tla["specs"]}
            assert names == {"KillSwitch", "OrderLifecycle", "Outbox", "Reconciliation"}, names
            return (
                f"{tla['total_states']} states, "
                f"{tla['total_transitions']} transitions, 0 counterexamples"
            )

        table.check("Phase 2 TLA+ exhaustive BFS proofs", formal_tla)

        def vopr_campaign() -> str:
            result = runtime.formal_verifier.vopr.run_campaign()
            assert result.ok, result.to_dict()
            faults = sum(result.fault_counts.values())
            return (
                f"seed={result.seed}, {result.steps} steps, {faults} faults, "
                f"{result.power_cycles} power cycles, 0 overdrafts"
            )

        table.check("Phase 2 VOPR deterministic fault injection", vopr_campaign)

        def backend_provisioning() -> str:
            from ring0_vault.ibor.tb_pg_provisioner import TigerBeetlePG16Provisioner

            provisioner = runtime.tb_pg_provisioner or TigerBeetlePG16Provisioner()
            formatted = provisioner.format_temporary_replica(
                Path(tempfile.mkdtemp(prefix="nios-smoke-tb-"))
            )
            status = provisioner.status()
            assert status["tigerbeetle"]["found"], status["tigerbeetle"]
            assert status["tigerbeetle"]["version"] == "0.17.9", status["tigerbeetle"]
            schema = status["postgres"]["schema"]
            assert schema["valid"], schema
            assert len(schema["tables"]) == 7, schema["tables"]
            assert formatted.formatted and Path(formatted.path).exists(), formatted
            return (
                f"TigerBeetle {status['tigerbeetle']['version']} "
                f"({'emulated' if formatted.emulated else 'live'}), "
                f"PG16 {len(schema['tables'])} tables / {schema['checks']} checks"
            )

        table.check("Phase 2 TigerBeetle replica + PG16 DDL", backend_provisioning)

        def tail_hedger() -> str:
            from core.schemas import VaultName

            status, plan = runtime.tail_hedger.monitor_and_hedge(
                cascade_fragility_index=0.72,
                tail_correlation=0.68,
                spot=50_000.0,
                portfolio_notional_micros=runtime.ledger.total_nav_micros(runtime.silos),
                tactical_vault_micros=max(
                    1, runtime.ledger.balance("US_IBKR", VaultName.TACTICAL_SWING)
                ),
            )
            assert status.triggered, status.to_dict()
            assert plan is not None, "tail hedger emitted no plan"
            assert plan.proposal.vault is VaultName.TACTICAL_SWING
            assert plan.grounding_verified, plan.grounding_claims_checked
            assert plan.delta < 0.0 and plan.premium_bps > 0.0
            return (
                f"fragility={plan.cascade_fragility_index:.2f}, "
                f"tail_corr={plan.tail_correlation:.2f}, "
                f"strike={plan.strike:.0f}, delta={plan.delta:+.3f}, "
                f"premium={plan.premium_bps:.1f} bps"
            )

        table.check("Phase 2 TacticalSwing options tail-hedger", tail_hedger)

        # ---- Ring 3: cockpit / MCP / static UI -------------------------
        client = TestClient(fastapi_app(runtime))

        def ring3_health() -> str:
            response = client.get("/api/v2/cockpit/health")
            assert response.status_code == 200, response.text
            payload = response.json()
            return f"status={payload['status']}, version={payload['version']}"

        table.check("Ring 3 cockpit REST health", ring3_health)

        def ring3_state() -> str:
            response = client.get("/api/v2/cockpit/state")
            assert response.status_code == 200, response.text
            payload = response.json()
            for key in ("silos", "leases", "contagion", "extensions", "formal_proofs"):
                assert key in payload, key
            assert payload["formal_proofs"]["summary"]["counterexamples"] == 0
            return (
                f"silos={len(payload['silos'])}, cards={len(payload['cards'])}, "
                f"extensions={payload['extensions']['counts']}, "
                f"proofs={payload['formal_proofs']['summary']['states_explored']} states"
            )

        table.check("Ring 3 cockpit state hydration", ring3_state)

        def ring3_mcp() -> str:
            response = client.get("/api/v2/mcp/tools")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["count"] == 5, payload["count"]
            assert payload["air_gap"]["pass"] is True, payload["air_gap"]
            return (
                f"{payload['count']} tools, air_gap=PASS, "
                f"read_only={payload['air_gap']['read_only_tools']}"
            )

        table.check("Ring 3 read-only MCP bridge", ring3_mcp)

        def ring3_extensions() -> str:
            response = client.get("/api/v2/control/extensions")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["counts"]["skills"] == 4
            assert payload["counts"]["mcp_tools"] == 5
            return (
                f"skills={payload['counts']['skills']}, "
                f"plugins={payload['counts']['plugins']}, "
                f"mcp={payload['counts']['mcp_tools']}"
            )

        table.check("Ring 3 extension control surface", ring3_extensions)

        def ring3_static() -> str:
            index = SMOKE_UI_ROOT / "index.html"
            assert index.exists(), f"missing {index}"
            response = client.get("/")
            assert response.status_code == 200, response.text
            assert "root" in response.text.lower(), "React root div missing"
            assets = list((SMOKE_UI_ROOT / "assets").glob("*"))
            assert assets, "compiled asset bundle missing"
            return f"index.html + {len(assets)} assets served at /"

        table.check("Unified server static React UI", ring3_static)

        def ring3_mcp_jsonrpc() -> str:
            response = client.post(
                "/api/v2/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                },
            )
            assert response.status_code == 200, response.text
            tools = response.json()["result"]["tools"]
            assert len(tools) == 5, len(tools)
            return f"JSON-RPC tools/list -> {len(tools)} tools"

        table.check("MCP JSON-RPC / SSE transport", ring3_mcp_jsonrpc)

        # ------------------------------------------------------------------
        # Phase 3 organ coverage: rows 17-22 of the 22-row final closure table.
        # ------------------------------------------------------------------

        def phase3_process_supervisor() -> str:
            from core.process_supervisor import RING_WORKERS, RingProcessSupervisor

            # Construct only: spawning is the multiprocess harness' job.  The
            # snapshot accessors are avoided because they attach (and reset)
            # the shared heartbeat segment the live supervisor owns.
            supervisor = RingProcessSupervisor(rings=(), fresh_heartbeats=False)
            names = [supervisor.spec(index).name for index in range(4)]
            assert names == list(RING_WORKERS), names
            # The exact prefix is environment-scoped (``NIOS_SHM_PREFIX`` lets a
            # test process carve out its own namespace), so the contract is the
            # immutable ``nios`` root rather than a literal string.
            assert str(supervisor.shm_prefix).startswith("nios"), supervisor.shm_prefix
            assert callable(supervisor.run_forge_and_hedge_cycle), "forge cycle missing"
            return f"{len(names)} ring workers, shm prefix {supervisor.shm_prefix}"

        def phase3_crash_recovery() -> str:
            from core.chaos_recovery import audit_recovery
            from core.schemas import VaultName
            from ring0_vault.ibor.ledger_outbox import IBORLedger, LedgerStore

            recovery_dir = tempfile.mkdtemp(prefix="nios-smoke-recovery-")
            path = os.path.join(recovery_dir, "ledger.sqlite3")
            store = LedgerStore(path, probe=False)
            ledger = IBORLedger(store, silos=["CRYPTO_BINANCE"])
            ledger.open_silo("CRYPTO_BINANCE", funding_micros=1_000_000_000)
            ledger.fund_vault("CRYPTO_BINANCE", VaultName.SPINAL_SCALPER, 100_000_000)
            ledger.flush()
            report = audit_recovery(path, silos=("CRYPTO_BINANCE",))
            assert report.passed, report.to_dict()
            assert report.invariant_holds, "double-entry invariant broken after recovery"
            return (
                f"recovered ledger balanced={report.balanced} "
                f"invariant={report.invariant_holds}"
            )

        def phase3_multisilo_feed() -> str:
            from ring2_brain.truth.live_public_feeds import MultiSiloFeedConnector

            connector = MultiSiloFeedConnector()
            try:
                frames = connector.ingest_all()
                feed_state = connector.status()
            finally:
                connector.close()
            silos = sorted({frame.silo for frame in frames})
            assert len(silos) == 4, silos
            assert feed_state["kind"] == "multi_silo_dual_vendor", feed_state["kind"]
            assert len(feed_state["venues"]) == 4, sorted(feed_state["venues"])
            return (
                f"{len(frames)} Lee-Ready frames across {len(silos)} silos "
                f"({feed_state['feed_mode']})"
            )

        def phase3_testnet_bridge() -> str:
            from core.schemas import UnsignedProposal, VaultName
            from ring0_vault.ibor.ledger_outbox import (
                CrashAtomicOutbox,
                IBORLedger,
                LedgerStore,
            )
            from ring0_vault.silos.key_vault.key_rotation import vault_key_status
            from ring0_vault.silos.testnet_bridge import (
                SANDBOX_VENUE_SPECS,
                TestnetExecutionBridge,
            )

            sandbox_dir = tempfile.mkdtemp(prefix="nios-smoke-testnet-")
            store = LedgerStore(os.path.join(sandbox_dir, "ledger.sqlite3"), probe=False)
            ledger = IBORLedger(store, silos=["CRYPTO_BINANCE"])
            ledger.open_silo("CRYPTO_BINANCE", funding_micros=10_000_000_000)
            ledger.fund_vault("CRYPTO_BINANCE", VaultName.SPINAL_SCALPER, 5_000_000_000)
            outbox = CrashAtomicOutbox(ledger)
            bridge = TestnetExecutionBridge(outbox, ledger, allow_network=False)
            try:
                receipt = bridge.dispatch(
                    "CRYPTO_BINANCE",
                    "binance",
                    UnsignedProposal(
                        proposal_id="smoke-sandbox",
                        strategy_id="smoke",
                        silo="CRYPTO_BINANCE",
                        vault=VaultName.SPINAL_SCALPER,
                        symbol="BTCUSDT",
                        side="BUY",
                        qty=0.01,
                        limit_price=100.0,
                        expected_gross_bps=12.0,
                        spread_bps=1.0,
                        grounding_hash="a" * 64,
                    ),
                    sandbox=True,
                )
                bridge_state = bridge.status()
            finally:
                bridge.close()
            assert len(SANDBOX_VENUE_SPECS) == 12, len(SANDBOX_VENUE_SPECS)
            assert bridge_state["write_only_credentials"] is True
            body = receipt.to_dict()
            assert body["sandbox"] is True, body
            assert body["write_only"] is True, body
            assert body["raw_secret_returned"] is False, body
            vault = vault_key_status()
            assert vault["verifier_ring_read_only"] is True
            assert vault["raw_secrets_exposed"] is False
            assert int(vault["key_version"]) >= 1, vault["key_version"]
            return (
                f"{len(SANDBOX_VENUE_SPECS)} sandbox venues write-only, "
                f"key v{vault['key_version']}, outbox {body['outbox_state']}"
            )

        def observability_metrics() -> str:
            prometheus = client.get("/metrics")
            assert prometheus.status_code == 200, prometheus.text
            assert len(prometheus.text) > 256, len(prometheus.text)
            payload = client.get("/api/v2/metrics/json")
            assert payload.status_code == 200, payload.text
            body = payload.json()
            assert body.get("exposition"), sorted(body)
            return (
                f"/metrics {len(prometheus.text)} bytes, "
                f"/api/v2/metrics/json {len(body)} keys"
            )

        def os_low_latency_auditor() -> str:
            from core.os_tuning import audit_and_tune_os

            report = audit_and_tune_os(apply=False, ring="ring1")
            socket_report = report["socket"]
            assert socket_report["tcp_nodelay"] == 1, socket_report
            assert socket_report["so_keepalive"] == 1, socket_report
            assert int(report["error_count"]) == 0, report["errors"]
            return (
                f"{report['applied_count']} applied / "
                f"{report['recommended_count']} recommended, TCP_NODELAY=1"
            )

        table.check("Phase 3 four-ring OS process supervisor", phase3_process_supervisor)
        table.check("Phase 3 crash-recovery / outbox audit", phase3_crash_recovery)
        table.check("Multi-Silo Dual-Vendor Feed & Lee-Ready Flow", phase3_multisilo_feed)
        table.check("Ring 0 Testnet Execution Bridge & Write-Only Vault", phase3_testnet_bridge)
        table.check("Prometheus / OpenTelemetry Metrics Exporter", observability_metrics)
        table.check("OS & Socket Low-Latency Tuning Auditor", os_low_latency_auditor)

        # The runtime budget gates the exit code but is deliberately not a row:
        # the release table is fixed at 22 hardware/software organ checks.
        elapsed_ms = (time.perf_counter() - started) * 1e3
        budget_ok = elapsed_ms < 120_000.0
        budget_detail = f"boot={boot_ms:.0f} ms, total={elapsed_ms:.0f} ms"
    except Exception as exc:  # boot failure is the loudest failure
        table.add("Runtime boot", False, f"{type(exc).__name__}: {exc}")
        budget_ok = False
        budget_detail = "not measured (boot failure)"
    finally:
        if runtime is not None:
            runtime.close()

    print(table.render())
    print(
        f"ROWS={len(table.rows)}  RUNTIME BUDGET  "
        f"{'PASS' if budget_ok else 'FAIL'}  {budget_detail}"
    )
    print()
    if table.ok and budget_ok:
        print(
            "SMOKE TEST PASSED: all four rings healthy, 22 organs certified, "
            "5 MCP tools, UI served."
        )
        return 0
    print("SMOKE TEST FAILED: see FAIL rows above.")
    return 1


# ---------------------------------------------------------------------------
# Server / ring roles
# ---------------------------------------------------------------------------


def run_server(
    *,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    workspace: str | Path | None = None,
    allow_port_fallback: bool = True,
) -> int:
    import uvicorn

    requested = DEFAULT_PORT if port is None else int(port)
    resolved = resolve_port(host, requested, allow_fallback=allow_port_fallback)
    if resolved != requested:
        print(f"[NIOS] port {requested} is busy -> auto-selected {resolved}")
    print(f"[NIOS] live cockpit: {live_url(host, resolved)}")
    sys.stdout.flush()

    runtime = build_runtime(workspace)
    try:
        app = fastapi_app(runtime)
        uvicorn.run(app, host=host, port=resolved, log_level="info")
    finally:
        runtime.close()
    return 0


def run_role(role: str, *, workspace: str | Path | None = None) -> int:
    """Boot one ring process for the docker-compose topology and idle."""
    runtime = build_runtime(workspace)
    token = str(role).strip().lower()
    try:
        if token == "ring0-vault":
            report = runtime.ledger.invariant_report()
            print(
                json.dumps(
                    {
                        "role": "ring0-vault",
                        "ibor_holds": report["holds"],
                        "accounts": report["accounts"],
                    }
                )
            )
        elif token == "ring1-spinal":
            runtime.engine.prewarm(["RELIANCE.NS"])
            print(
                json.dumps(
                    {
                        "role": "ring1-spinal",
                        "strategy": runtime.engine.strategy_id,
                        "shadow_fills": runtime.twin.shadow_fill_count(
                            runtime.strategy_id
                        ),
                    }
                )
            )
        elif token == "ring2-brain":
            runtime.forge.audit()
            threading.Thread(
                target=runtime.forge.run_scheduler_forever,
                name="nios-alpha-forge-0000utc",
                daemon=True,
            ).start()
            print(
                json.dumps(
                    {
                        "role": "ring2-brain",
                        "skills": runtime.skills.describe()["count"],
                        "forge_next_utc": "00:00",
                        "forge_scheduler": "running",
                    }
                )
            )
        elif token == "ring3-cockpit":
            env_port = os.environ.get("NIOS_PORT", "").strip()
            requested = int(env_port) if env_port else DEFAULT_PORT
            return run_server(
                host=os.environ.get("NIOS_HOST", DEFAULT_HOST),
                port=requested,
                workspace=workspace,
                allow_port_fallback=not bool(env_port),
            )
        else:
            raise SystemExit(f"unknown role {role!r}")
        sys.stdout.flush()
        # Idle until the orchestrator receives SIGTERM / Ctrl-C.
        try:
            while True:
                threading.Event().wait(30.0)
        except KeyboardInterrupt:
            return 0
    finally:
        runtime.close()


def run_multiprocess(
    *,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    workspace: str | Path | None = None,
    silo: str = "CRYPTO_BINANCE",
    feed_mode: str = "PAPER_AUTOPILOT",
    seconds: float | None = None,
    allow_port_fallback: bool = True,
) -> int:
    """Boot the four rings as four physically separate OS worker processes.

    ``sec_004`` requires strict OS process isolation: Ring 0 (vault + signer),
    Ring 1 (spinal), Ring 2 (brain) and Ring 3 (cockpit) each get their own PID
    and communicate only through the shared-memory spine and the JetStream bus.
    The FastAPI cockpit keeps running in this (parent) process, and it is handed
    the supervisor so ``/api/v2/cockpit/state`` can prove the PID topology.
    """

    import json
    import time as _time

    from core.process_supervisor import RingProcessSupervisor

    supervisor = RingProcessSupervisor(
        workspace=workspace,
        silo=silo,
        feed_mode=feed_mode,
    )
    supervisor.start()
    runtime = None
    try:
        runtime = build_runtime(workspace=getattr(supervisor, "workspace", workspace))
        control = getattr(runtime, "control", None)
        if control is not None:
            control.process_supervisor = supervisor
            control._feed_mode = feed_mode
        if seconds is not None:
            deadline = _time.monotonic() + max(0.0, float(seconds))
            while _time.monotonic() < deadline:
                supervisor.poll()
                _time.sleep(0.05)
            print(json.dumps(supervisor.describe(), indent=2, default=str))
            return 0
        return run_server(
            host=host,
            port=port,
            workspace=getattr(supervisor, "workspace", workspace),
            allow_port_fallback=allow_port_fallback,
        )
    finally:
        supervisor.stop()
        if runtime is not None:
            try:
                runtime.close()
            except Exception:  # pragma: no cover - shutdown is best effort
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_nios.py",
        description="Unified NIOS 4-ring production server and smoke test.",
    )
    parser.add_argument("--smoke-test", action="store_true", help="boot all rings and self-check")
    parser.add_argument("--host", default=os.environ.get("NIOS_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "force a port (otherwise $NIOS_PORT, otherwise the first free port "
            f"in {PORT_SCAN_START}..{PORT_SCAN_END})"
        ),
    )
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--multiprocess",
        action="store_true",
        help=(
            "run Ring 0/1/2/3 as four separate OS worker processes "
            "(sec_004 process isolation)"
        ),
    )
    parser.add_argument(
        "--silo",
        default="CRYPTO_BINANCE",
        choices=[
            "CRYPTO_BINANCE",
            "INDIA_ZERODHA_DHAN",
            "US_IBKR",
            "FX_MT5",
        ],
        help="silo the Ring 1 worker pins to (ignored without --multiprocess)",
    )
    parser.add_argument(
        "--feed-mode",
        default="PAPER_AUTOPILOT",
        choices=["PAPER_AUTOPILOT", "LIVE_PUBLIC_DUAL_VENDOR"],
        help="market-data path advertised to the cockpit",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="run the supervisor headless for N seconds instead of serving HTTP",
    )
    parser.add_argument(
        "--role",
        default=None,
        choices=["ring0-vault", "ring1-spinal", "ring2-brain", "ring3-cockpit"],
        help="run one ring node for the segmented compose topology",
    )
    args = parser.parse_args(argv)

    env_port = os.environ.get("NIOS_PORT", "").strip()
    if args.port is not None:
        requested, forced = int(args.port), True
    elif env_port:
        try:
            requested, forced = int(env_port), True
        except ValueError:
            print(f"[NIOS] FATAL: invalid NIOS_PORT={env_port!r}", file=sys.stderr)
            return 2
    else:
        requested, forced = DEFAULT_PORT, False

    if args.smoke_test:
        return run_smoke(host=args.host, port=requested, workspace=args.workspace)
    if args.role:
        return run_role(args.role, workspace=args.workspace)
    if args.multiprocess:
        try:
            return run_multiprocess(
                host=args.host,
                port=requested,
                workspace=args.workspace,
                silo=args.silo,
                feed_mode=args.feed_mode,
                seconds=args.seconds,
                allow_port_fallback=not forced,
            )
        except PortUnavailable as exc:
            print(f"[NIOS] FATAL: {exc}", file=sys.stderr)
            return 1
    try:
        return run_server(
            host=args.host,
            port=requested,
            workspace=args.workspace,
            allow_port_fallback=not forced,
        )
    except PortUnavailable as exc:
        print(f"[NIOS] FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
