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
            f"{'NIOS STEP 8 SMOKE CHECK':<{width}}  STATUS  DETAIL",
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

        elapsed_ms = (time.perf_counter() - started) * 1e3
        table.add(
            "Smoke runtime budget",
            elapsed_ms < 120_000.0,
            f"boot={boot_ms:.0f} ms, total={elapsed_ms:.0f} ms",
        )
    except Exception as exc:  # boot failure is the loudest failure
        table.add("Runtime boot", False, f"{type(exc).__name__}: {exc}")
    finally:
        if runtime is not None:
            runtime.close()

    print(table.render())
    print()
    if table.ok:
        print("SMOKE TEST PASSED: all four rings healthy, 5 MCP tools, UI served.")
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
