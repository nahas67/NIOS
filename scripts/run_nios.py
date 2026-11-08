"""Unified NIOS production server and smoke-test entry point (Step 8).

Two modes, one process model:

* ``python scripts/run_nios.py --smoke-test`` boots the complete 4-ring
  ``NIOSCoreOrchestrator``, runs a self-check across every ring (IBOR invariant,
  shared-memory lease table, 480+ factor scanner, MiroFish cascade, Forge,
  control plane, five MCP tools, static React bundle), prints a health table and
  exits ``0`` only when every check passes.
* ``python scripts/run_nios.py`` serves the unified FastAPI app (``/api/v2/*``
  REST + SSE + MCP + the compiled React 19 bundle at ``/``) with uvicorn on
  ``0.0.0.0:8000``.

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
    "build_runtime",
    "fastapi_app",
    "main",
    "run_role",
    "run_smoke",
    "run_server",
]

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
SMOKE_UI_ROOT = ROOT / "ring3_cockpit" / "ui" / "openalgo_shell" / "dist"


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
                f"{len(vaults)} vaults, nav={runtime.ledger.silo_nav_micros(PRIMARY_SILO)}usd"
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
            for key in ("silos", "leases", "contagion", "extensions"):
                assert key in payload, key
            return (
                f"silos={len(payload['silos'])}, cards={len(payload['cards'])}, "
                f"extensions={payload['extensions']['counts']}"
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
    port: int = DEFAULT_PORT,
    workspace: str | Path | None = None,
) -> int:
    import uvicorn

    runtime = build_runtime(workspace)
    try:
        app = fastapi_app(runtime)
        uvicorn.run(app, host=host, port=int(port), log_level="info")
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
            print(
                json.dumps(
                    {
                        "role": "ring2-brain",
                        "skills": runtime.skills.describe()["count"],
                        "forge_next_utc": "00:00",
                    }
                )
            )
        elif token == "ring3-cockpit":
            return run_server(workspace=workspace)
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
        "--port", type=int, default=int(os.environ.get("NIOS_PORT", DEFAULT_PORT))
    )
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--role",
        default=None,
        choices=["ring0-vault", "ring1-spinal", "ring2-brain", "ring3-cockpit"],
        help="run one ring node for the segmented compose topology",
    )
    args = parser.parse_args(argv)
    if args.smoke_test:
        return run_smoke(host=args.host, port=args.port, workspace=args.workspace)
    if args.role:
        return run_role(args.role, workspace=args.workspace)
    return run_server(host=args.host, port=args.port, workspace=args.workspace)


if __name__ == "__main__":
    raise SystemExit(main())
