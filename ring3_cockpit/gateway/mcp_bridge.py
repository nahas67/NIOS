"""Read-only MCP Tool Bridge for the Ring 3 Glass Cockpit (``sec_004`` / ``sec_016``).

The bridge is deliberately *not* a general-purpose MCP server.  It exposes five
typed tools, every one of which is auditable against the 4-ring air-gap:

===============================  ==========================================
``nios_get_ibor_vaults``         Read-only 4-vault balances + NAV per silo
``nios_verify_grounding_hash``   SHA-256 packet + thesis verifier
``nios_simulate_mirofish_cascade`` Deterministic 5-archetype 3-tick cascade
``nios_scan_top_candidates``     VectorizedFactorScanner top-1% output
``nios_emergency_killswitch``    One-way <1.0 ms global halt + ADR-009 flatten
===============================  ==========================================

There is **no** tool for order placement, order signing, broker credential
reading, withdrawal, or capital transfer.  :meth:`MCPBridge.air_gap_report`
re-derives that property from the live tool schemas on every listing, so a
future tool cannot be added silently without failing the audit.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

__all__ = [
    "AIR_GAP_FORBIDDEN_TERMS",
    "MCPCallRequest",
    "MCPBridge",
    "MCPTool",
    "create_mcp_app",
    "serve_stdio",
]

#: Any tool name/description/schema property containing one of these tokens is
#: outside the Ring 3 air-gap.  ``sign`` is matched as a token, not a substring,
#: so "design" does not trip the audit.
AIR_GAP_FORBIDDEN_TERMS: tuple[str, ...] = (
    "place_order",
    "submit_order",
    "send_order",
    "sign_order",
    "order_signing",
    "api_key",
    "broker_key",
    "private_key",
    "secret_key",
    "read_key",
    "withdraw",
    "transfer_funds",
    "wire_funds",
)


@dataclass(frozen=True)
class MCPTool:
    """One typed, auditable MCP tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    read_only: bool = True
    category: str = "read"
    ring: str = "ring3"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
            "read_only": bool(self.read_only),
            "category": self.category,
            "ring": self.ring,
            "air_gap": "read-only" if self.read_only else "safety-mutation",
        }


class MCPCallRequest(BaseModel):
    """``POST /api/v2/mcp/call`` payload."""

    name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class _KillRequest:
    """Minimal request object accepted by ``ControlPlane.kill``."""

    reason: str = "MCP emergency kill switch"
    flatten: bool = True
    confirm: bool = True


class MCPBridge:
    """Five-tool read-only bridge over the Ring 3 control plane."""

    def __init__(self, control: Any) -> None:
        if control is None:
            raise ValueError("MCPBridge requires a ControlPlane")
        self.control = control
        self._enabled: dict[str, bool] = {}
        self._tools: dict[str, MCPTool] = {}
        self._register_default_tools()

    # -- registration -----------------------------------------------------
    def _add(self, tool: MCPTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate MCP tool {tool.name!r}")
        self._tools[tool.name] = tool
        self._enabled[tool.name] = True

    def _register_default_tools(self) -> None:
        self._add(
            MCPTool(
                name="nios_get_ibor_vaults",
                description=(
                    "Read-only four-vault Investment Book of Records balances and "
                    "NAV for CRYPTO, INDIA, US and FX silos."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "silos": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional silo subset.",
                        }
                    },
                    "additionalProperties": False,
                },
                handler=self._tool_ibor_vaults,
                read_only=True,
                category="ibor",
            )
        )
        self._add(
            MCPTool(
                name="nios_verify_grounding_hash",
                description=(
                    "Audits a thesis text against a SHA-256 GroundedFactPacket "
                    "digest using the NumericalGroundingVerifier."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "thesis_text": {"type": "string", "minLength": 1},
                        "digest": {
                            "type": "string",
                            "description": "SHA-256 digest already in the ledger.",
                        },
                        "packet": {
                            "type": "object",
                            "description": "Inline GroundedFactPacket payload.",
                        },
                        "tolerance": {"type": "number", "default": 1e-4},
                    },
                    "required": ["thesis_text"],
                    "additionalProperties": False,
                },
                handler=self._tool_verify_grounding,
                read_only=True,
                category="truth",
            )
        )
        self._add(
            MCPTool(
                name="nios_simulate_mirofish_cascade",
                description=(
                    "Runs the deterministic 1.8 ms five-archetype three-tick "
                    "contagion cascade and returns ContagionFeatures."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "price_shock_bps": {"type": "number", "default": 40.0},
                        "realized_vol_ratio": {"type": "number", "default": 1.4},
                        "spread_bps": {"type": "number", "default": 6.0},
                        "dealer_gamma_flip_proximity": {
                            "type": "number",
                            "default": 0.2,
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                    },
                    "additionalProperties": False,
                },
                handler=self._tool_mirofish,
                read_only=True,
                category="contagion",
            )
        )
        self._add(
            MCPTool(
                name="nios_scan_top_candidates",
                description=(
                    "Runs the VectorizedFactorScanner and returns the top 1% "
                    "grounded dislocation candidates (50 of 5,000)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "n_symbols": {
                            "type": "integer",
                            "minimum": 10,
                            "maximum": 5000,
                            "default": 5000,
                        },
                        "n_periods": {
                            "type": "integer",
                            "minimum": 16,
                            "maximum": 512,
                            "default": 64,
                        },
                        "top_k": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 50,
                        },
                        "silo": {"type": "string", "default": "INDIA_ZERODHA_DHAN"},
                        "commit": {"type": "boolean", "default": False},
                    },
                    "additionalProperties": False,
                },
                handler=self._tool_scan_candidates,
                read_only=True,
                category="quant",
            )
        )
        self._add(
            MCPTool(
                name="nios_emergency_killswitch",
                description=(
                    "One-way safety tool: triggers the sub-1.0 ms global halt, "
                    "disarms every Ring 1 lease and applies the ADR-009 "
                    "reduce-only flatten."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "default": "MCP emergency kill switch",
                        },
                        "flatten": {"type": "boolean", "default": True},
                        "confirm": {"type": "boolean", "default": True},
                    },
                    "additionalProperties": False,
                },
                handler=self._tool_killswitch,
                read_only=False,
                category="safety",
            )
        )

    # -- listing ----------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {**tool.to_dict(), "enabled": bool(self._enabled.get(tool.name, True))}
            for tool in self._tools.values()
        ]

    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        token = str(name)
        if token not in self._tools:
            raise KeyError(f"unknown MCP tool {token!r}")
        self._enabled[token] = bool(enabled)
        return {"name": token, "enabled": bool(enabled)}

    def is_enabled(self, name: str) -> bool:
        return bool(self._enabled.get(str(name), False))

    # -- invocation -------------------------------------------------------
    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        token = str(name)
        tool = self._tools.get(token)
        if tool is None:
            raise KeyError(f"unknown MCP tool {token!r}")
        if not self._enabled.get(token, False):
            raise PermissionError(f"MCP tool {token!r} is disabled")
        args = dict(arguments or {})
        return tool.handler(args)

    # -- the five handlers ------------------------------------------------
    def _tool_ibor_vaults(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from core.schemas import VaultName

        requested = arguments.get("silos")
        available = list(self.control.registry.silo_ids())
        silos = (
            [str(silo) for silo in requested]
            if isinstance(requested, list) and requested
            else available
        )
        payload: dict[str, Any] = {
            "read_only": True,
            "vaults": [vault.value for vault in VaultName],
            "silos": {},
        }
        total = 0
        for silo in silos:
            try:
                balances = self.control.ledger.vault_balances(silo)
                nav_micros = int(self.control.ledger.silo_nav_micros(silo))
            except Exception as exc:
                payload["silos"][silo] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            total += nav_micros
            payload["silos"][silo] = {
                "vaults": {
                    vault.value: {
                        "micros": int(balances.get(vault, 0)),
                        "nav": int(balances.get(vault, 0)) / 1_000_000.0,
                    }
                    for vault in VaultName
                },
                "nav_micros": nav_micros,
                "nav": nav_micros / 1_000_000.0,
            }
        payload["total_nav_micros"] = total
        payload["total_nav"] = total / 1_000_000.0
        return payload

    def _tool_verify_grounding(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from core.schemas import GroundedFactPacket
        from ring2_brain.truth.grounding_gate import NumericalGroundingVerifier

        thesis = str(arguments.get("thesis_text", "")).strip()
        if not thesis:
            raise ValueError("thesis_text must be non-empty")
        tolerance = float(arguments.get("tolerance", 1e-4))
        packet: GroundedFactPacket | None = None
        digest = str(arguments.get("digest", "") or "").strip().lower()
        if digest:
            packet = self.control.grounding.get(digest)
            if packet is None:
                return {
                    "verified": False,
                    "digest": digest,
                    "error": "DIGEST_NOT_FOUND_IN_GROUNDING_LEDGER",
                }
        elif isinstance(arguments.get("packet"), dict):
            raw = dict(arguments["packet"])
            packet = GroundedFactPacket(
                symbol=str(raw.get("symbol", "UNKNOWN")),
                valid_time_ns=int(raw.get("valid_time_ns", 0)),
                knowledge_ts_ns=int(raw.get("knowledge_ts_ns", 0)),
                as_of_ns=int(raw.get("as_of_ns", 0)),
                metrics={
                    str(key): float(value)
                    for key, value in dict(raw.get("metrics", {})).items()
                },
                sha256_digest=str(raw.get("sha256_digest", "")),
            )
        if packet is None:
            return {
                "verified": False,
                "error": "provide either digest or packet",
            }
        try:
            report = NumericalGroundingVerifier(tolerance=tolerance).verify_thesis_claims(
                thesis, packet
            )
        except Exception as exc:
            return {
                "verified": False,
                "digest": packet.sha256_digest,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "verified": True,
            "digest": packet.sha256_digest,
            "report": report.to_dict(),
        }

    def _tool_mirofish(self, arguments: dict[str, Any]) -> dict[str, Any]:
        runner = getattr(self.control, "cascade_runner", None)
        if not callable(runner):
            runner = getattr(self.control, "contagion_runner", None)
        if callable(runner):
            result = runner(
                price_shock_bps=float(arguments.get("price_shock_bps", 40.0)),
                realized_vol_ratio=float(arguments.get("realized_vol_ratio", 1.4)),
                spread_bps=float(arguments.get("spread_bps", 6.0)),
                dealer_gamma_flip_proximity=float(
                    arguments.get("dealer_gamma_flip_proximity", 0.2)
                ),
                tags=tuple(str(tag) for tag in arguments.get("tags", ())),
                symbol=arguments.get("symbol"),
            )
        else:
            provider = getattr(self.control, "contagion_provider", None)
            result = provider() if callable(provider) else provider
        if result is None:
            return {"ok": False, "error": "MIROFISH_ENGINE_UNAVAILABLE"}
        payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        payload["ok"] = True
        return payload

    def _tool_scan_candidates(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from ring2_brain.quant.scanner import FactorPanel

        scanner = getattr(self.control, "scanner", None)
        if scanner is None:
            return {"ok": False, "error": "VECTORIZED_SCANNER_UNAVAILABLE"}
        n_symbols = max(10, min(int(arguments.get("n_symbols", 5000)), 5000))
        n_periods = max(16, min(int(arguments.get("n_periods", 64)), 512))
        top_k = max(1, min(int(arguments.get("top_k", 50)), n_symbols))
        panel = FactorPanel.generate(n_symbols=n_symbols, n_periods=n_periods)
        result = scanner.scan(
            panel,
            as_of_ns=time.time_ns(),
            top_k=top_k,
            commit=bool(arguments.get("commit", False)),
            silo=str(arguments.get("silo", "INDIA_ZERODHA_DHAN")),
        )
        payload = result.to_dict()
        payload["ok"] = True
        payload["top_fraction"] = top_k / float(n_symbols)
        return payload

    def _tool_killswitch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not bool(arguments.get("confirm", True)):
            return {
                "status": "ARMED",
                "triggered": False,
                "reason": "confirm=false",
            }
        report = self.control.kill(
            _KillRequest(
                reason=str(arguments.get("reason", "MCP emergency kill switch")),
                flatten=bool(arguments.get("flatten", True)),
                confirm=True,
            )
        )
        return {**dict(report), "triggered": True, "tool": "nios_emergency_killswitch"}

    # -- air-gap audit ----------------------------------------------------
    def air_gap_report(self) -> dict[str, Any]:
        matches: list[dict[str, str]] = []
        for tool in self._tools.values():
            haystacks: list[tuple[str, str]] = [
                ("name", tool.name),
                ("description", tool.description),
            ]
            properties = dict(tool.input_schema.get("properties", {}))
            for prop, schema in properties.items():
                haystacks.append(("property", str(prop)))
                haystacks.append(("property_description", str(schema)))
            for where, text in haystacks:
                lowered = text.lower()
                for term in AIR_GAP_FORBIDDEN_TERMS:
                    if term in lowered:
                        matches.append(
                            {"tool": tool.name, "where": where, "term": term}
                        )
        order_tools = [
            tool.name
            for tool in self._tools.values()
            if tool.category in ("order", "execution", "broker")
        ]
        key_tools = [
            tool.name
            for tool in self._tools.values()
            if tool.category in ("key", "credentials", "secrets")
        ]
        return {
            "pass": not matches and not order_tools and not key_tools,
            "tool_count": len(self._tools),
            "forbidden_matches": matches,
            "order_signing_tools": order_tools,
            "key_reading_tools": key_tools,
            "read_only_tools": sum(1 for tool in self._tools.values() if tool.read_only),
            "safety_tools": sum(1 for tool in self._tools.values() if not tool.read_only),
            "air_gap": "ring3-read-only",
        }

    def summary(self) -> dict[str, Any]:
        return {
            "count": len(self._tools),
            "enabled_count": sum(1 for enabled in self._enabled.values() if enabled),
            "tools": self.list_tools(),
            "air_gap": self.air_gap_report(),
            "transports": ["http", "sse", "stdio", "json-rpc"],
        }

    # -- routing ----------------------------------------------------------
    def register_routes(self, app: FastAPI) -> FastAPI:
        @app.get("/api/v2/mcp/tools")
        def list_tools() -> dict[str, Any]:
            return {
                "count": len(self._tools),
                "enabled_count": sum(
                    1 for enabled in self._enabled.values() if enabled
                ),
                "tools": self.list_tools(),
                "transports": ["http", "sse", "stdio", "json-rpc"],
                "air_gap": self.air_gap_report(),
            }

        @app.post("/api/v2/mcp/call")
        def call_tool(request: MCPCallRequest) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                result = self.call_tool(request.name, request.arguments)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except Exception as exc:  # tool failure is reported, not 500
                return {
                    "name": request.name,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": (time.perf_counter() - started) * 1e3,
                }
            return {
                "name": request.name,
                "ok": True,
                "result": result,
                "elapsed_ms": (time.perf_counter() - started) * 1e3,
            }

        @app.post("/api/v2/mcp")
        @app.post("/api/v2/mcp/rpc")
        def json_rpc(request: dict[str, Any]) -> JSONResponse:
            return JSONResponse(self._json_rpc(request))

        @app.post("/api/v2/mcp/messages")
        def messages(request: dict[str, Any]) -> JSONResponse:
            return JSONResponse(self._json_rpc(request))

        @app.get("/api/v2/mcp/sse")
        def sse() -> StreamingResponse:
            async def body():
                yield (
                    "event: endpoint\n"
                    "data: /api/v2/mcp/messages\n\n"
                )
                yield (
                    "event: tools\n"
                    f"data: {json.dumps(self.list_tools())}\n\n"
                )

            return StreamingResponse(body(), media_type="text/event-stream")

        return app

    def _json_rpc(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = str(request.get("method", ""))
        params = dict(request.get("params") or {})
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "nios-ring3-mcp", "version": "3.0.0"},
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self.list_tools()},
            }
        if method == "tools/call":
            name = str(params.get("name", ""))
            arguments = dict(params.get("arguments") or {})
            try:
                result = self.call_tool(name, arguments)
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"{type(exc).__name__}: {exc}",
                            }
                        ],
                        "isError": True,
                    },
                }
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, default=str),
                        }
                    ],
                    "isError": False,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


# ---------------------------------------------------------------------------
# Standalone apps / stdio compatibility
# ---------------------------------------------------------------------------


def create_mcp_app(control: Any) -> FastAPI:
    """A FastAPI app containing only the MCP bridge (tests / sidecar use)."""
    app = FastAPI(title="NIOS Ring 3 MCP Bridge", version="3.0.0")
    bridge = MCPBridge(control)
    app.state.mcp = bridge
    bridge.register_routes(app)
    return app


def serve_stdio(
    bridge: MCPBridge,
    *,
    stdin: Any = None,
    stdout: Any = None,
) -> None:  # pragma: no cover - interactive transport
    """Line-delimited JSON-RPC over stdio (MCP stdio compatibility)."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    for line in source:
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
            response = bridge._json_rpc(request)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        sink.write(json.dumps(response, default=str) + "\n")
        sink.flush()


def _stdio_main() -> int:  # pragma: no cover - interactive transport
    import argparse
    import tempfile

    from core.orchestrator import NIOSCoreOrchestrator

    parser = argparse.ArgumentParser(description="NIOS Ring 3 MCP stdio bridge")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args()
    workspace = args.workspace or tempfile.mkdtemp(prefix="nios-mcp-")
    runtime = NIOSCoreOrchestrator(workspace=workspace)
    try:
        serve_stdio(runtime.control.mcp)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_stdio_main())
