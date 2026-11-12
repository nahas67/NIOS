"""Ring 3 Glass Cockpit control plane (``sec_004``, ``sec_011``, ``sec_016``).

The cockpit is the only writable surface in NIOS.  The four constitutional
endpoints remain, and Step 8 adds two explicitly authorized read/toggle
surfaces (extensions + read-only MCP) without granting order authority:

======================  =====================================================
``GET  /api/v2/cockpit/stream``  60 FPS SSE cards, abstention meter, 4-vault NAV
``GET  /api/v2/cockpit/state``   hydration snapshot (leases, contagion, LLMs)
``POST /api/v2/control/brokers`` Paper/Live, fee floors, loss stops, sliders
``POST /api/v2/control/llms``    System 1/2 hot swap with write-only keys
``POST /api/v2/control/killswitch`` <1.0 ms global preemption + ADR-009 flatten
``POST /api/v2/control/rearm``    explicit human reset back to ``ARMED``
``GET  /api/v2/control/extensions`` skills / plugins / MCP / Forge status
``POST /api/v2/control/extensions`` toggle an extension or run a Forge audit
``GET  /api/v2/mcp/tools``       five read-only MCP tools + air-gap audit
``POST /api/v2/mcp/call``        invoke one read-only tool
``POST /api/v2/control/feeds``   hot-swap PAPER_AUTOPILOT / LIVE dual-vendor
``POST /api/v2/control/brokers/credentials`` write-only testnet handshake
``POST /api/v2/control/llms/probe``          live Ollama probe under 250 ms
``POST /api/v2/control/vault/rotate-keys``   zero-downtime Ring 0 key rotation
``GET  /api/v2/control/vault/status``        key version + OS tuning status
======================  =====================================================

Design notes
------------
* Ring 3 never imports a broker adapter or signs an order.  It mutates
  in-memory control state and calls Ring 0's existing ``SovereignPreTradeFirewall``
  / ``ADR009ReduceOnlyFastPath`` objects by reference (``sec_011``).
* API keys are write-only.  ``LLMKeyVault`` encrypts them under
  ``ring0_vault/silos/key_vault``; every response carries only ``***9A4F``.
* The SSE stream is a *read* of Tier 1 (NATS cockpit cards) and Ring 0's
  ledger.  No endpoint in this module accepts an order.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr, field_validator

from core.nats_bus import STREAM_RING3_COCKPIT, LocalJetStreamBus
from core.schemas import (
    LEASE_TTL_NS,
    VaultName,
    PlainEnglishCockpitCard,
)
from ring0_vault.kernel.firewalls_and_oms import (
    ADR009ReduceOnlyFastPath,
    ExecutionEvent,
    SmartOMS,
    SovereignPreTradeFirewall,
    TradingState,
)
from ring0_vault.ibor.ledger_outbox import (
    CrashAtomicOutbox,
    IBORLedger,
    SiloGuard,
)
from ring0_vault.silos.key_vault.llm_credentials import LLMKeyVault
from ring0_vault.silos.key_vault.key_rotation import (
    rotate_ring0_vault_keys,
    vault_key_status,
)
from ring0_vault.silos.silo_registry import (
    MarketSiloRegistry,
    SiloMode,
    broker_credential_state,
    default_registry,
    store_and_verify_broker_credentials,
)
from ring1_spinal.silo_worker import SiloCostFloor

__all__ = [
    "ARCHETYPE_GROUPS",
    "ARCHETYPE_WEIGHTS",
    "ControlPlane",
    "BrokerControlRequest",
    "BrokerCredentialRequest",
    "FeedControlRequest",
    "LLMControlRequest",
    "LLMProbeRequest",
    "KillSwitchRequest",
    "RearmRequest",
    "ExtensionsControlRequest",
    "SupervisorControlRequest",
    "TestnetOrderRequest",
    "VaultRotationRequest",
    "autopilot_paper_loop",
    "create_app",
    "DEFAULT_DAILY_LLM_BUDGET_USD",
    "DOCUMENT_ID",
    "KILL_LATENCY_BUDGET_NS",
    "PLATFORM_VERSION",
]

#: ``sec_001``: the constitution's document identity and released version.
DOCUMENT_ID: str = "NIOS-ARC-2026-V3"
PLATFORM_VERSION: str = "3.0.0-PROD"

#: ``sec_016``: the standing daily LLM spend cap.
DEFAULT_DAILY_LLM_BUDGET_USD: float = 3.10
#: ``sec_004`` / ``sec_016``: kill switch preemption budget.
KILL_LATENCY_BUDGET_NS: int = 1_000_000
#: Paper-shadow autopilot cadence (250 ms - 500 ms, ``sec_013``).
AUTOPILOT_INTERVAL_S: float = 0.4

#: ``sec_008``: five capital-weighted MiroFish archetypes.
ARCHETYPE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("Retail_0DTE", 0.15),
    ("Options_Dealer_Gamma", 0.25),
    ("Vol_Target_CTA", 0.25),
    ("Pod_Shop_Stops", 0.20),
    ("Value_Funds", 0.15),
)

#: Cockpit display groups: Pod shops + value funds aggregate to 35%.
ARCHETYPE_GROUPS: tuple[dict[str, Any], ...] = (
    {"name": "Retail 0DTE", "weight": 0.15, "members": ["Retail_0DTE"]},
    {"name": "Dealer Gamma", "weight": 0.25, "members": ["Options_Dealer_Gamma"]},
    {"name": "Vol-Target CTAs", "weight": 0.25, "members": ["Vol_Target_CTA"]},
    {
        "name": "Pod Stops & Value",
        "weight": 0.35,
        "members": ["Pod_Shop_Stops", "Value_Funds"],
    },
)

_DEFAULT_ALLOCATION: dict[str, float] = {
    VaultName.RESERVE_YIELD.value: 0.15,
    VaultName.LONG_TERM_COMPOUNDER.value: 0.35,
    VaultName.TACTICAL_SWING.value: 0.00,
    VaultName.SPINAL_SCALPER.value: 0.50,
}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Phase 4 Step 2 -- pre-flight cache and closed-loop forge drill defaults
# ---------------------------------------------------------------------------

#: How long an interactive (fast-path) certificate may be replayed.
PREFLIGHT_CACHE_TTL_S = 300.0

#: Deterministic decayed-champion sample used by the cockpit drill: the rolling
#: net edge is below the +0.50 bps demotion threshold (sec_018 Law 4), so the
#: cycle retires a champion and disarms its Ring 1 lease instead of pretending
#: a live edge exists.
FORGE_HEDGE_DRILL_FILLS_BPS: tuple[float, ...] = (-1.4, -1.1, -1.9, -0.8, -1.2, -1.5)

# Request models
# ---------------------------------------------------------------------------


class BrokerControlRequest(BaseModel):
    """``POST /api/v2/control/brokers`` payload (``sec_016``)."""

    silo: str = Field(..., min_length=1)
    mode: str | None = None
    maker_fee_bps: float | None = Field(default=None, ge=0.0)
    taker_fee_bps: float | None = Field(default=None, ge=0.0)
    statutory_tax_bps: float | None = Field(default=None, ge=0.0)
    spread_bps: float | None = Field(default=None, ge=0.0)
    max_daily_loss_micros: int | None = Field(default=None, ge=0)
    allocation: dict[str, float] | None = None
    confirm_live: bool = False
    operator: str = "ring3.operator"

    @field_validator("mode")
    @classmethod
    def _mode_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        token = str(value).strip().upper()
        if token not in (SiloMode.PAPER.value, SiloMode.LIVE.value):
            raise ValueError("mode must be PAPER or LIVE")
        return token

    @field_validator("allocation")
    @classmethod
    def _allocation_ok(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return None
        allowed = {vault.value for vault in VaultName}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown vault(s): {sorted(unknown)}")
        clean = {str(k): float(v) for k, v in value.items()}
        if any(weight < 0.0 for weight in clean.values()):
            raise ValueError("allocation weights must be non-negative")
        total = sum(clean.values())
        if total <= 0.0:
            raise ValueError("allocation must assign positive weight to at least one vault")
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"allocation weights must sum to 1.0, got {total:.6f}")
        return clean


class LLMControlRequest(BaseModel):
    """``POST /api/v2/control/llms`` payload (``sec_016``).

    ``api_key`` is a :class:`SecretStr`: Pydantic never renders it in ``repr``,
    validation errors or structured logs.  It is popped into Ring 0's vault
    before any response is constructed.
    """

    system: int | str
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: SecretStr | None = None
    daily_spend_cap_usd: float | None = Field(default=None, ge=0.0)
    timeout_ms: int | None = Field(default=None, ge=1, le=5_000)

    @field_validator("system")
    @classmethod
    def _system_ok(cls, value: int | str) -> int:
        token = str(value).strip().lower().replace("system", "")
        if token not in ("1", "2"):
            raise ValueError("system must be 1 or 2")
        return int(token)


class KillSwitchRequest(BaseModel):
    """``POST /api/v2/control/killswitch`` payload."""

    reason: str = "manual operator kill switch"
    flatten: bool = True
    confirm: bool = True


class RearmRequest(BaseModel):
    """``POST /api/v2/control/rearm`` payload (explicit human reset)."""

    reason: str = "operator re-arm"
    operator: str = "ring3.operator"


class VaultRotationRequest(BaseModel):
    """``POST /api/v2/control/vault/rotate-keys`` payload (Phase 3 Step 5).

    Rotation is non-destructive and zero-downtime, so it needs no ``confirm``
    flag; the operator and reason are recorded in Ring 0's rotation journal.
    """

    operator: str = "ring3.operator"
    reason: str = "scheduled zero-downtime rotation"


class ExtensionsControlRequest(BaseModel):
    """``POST /api/v2/control/extensions`` payload (Step 8).

    ``target`` selects the surface; ``forge`` triggers a manual nocturnal audit,
    while ``skill`` / ``plugin`` / ``mcp_tool`` toggle one named extension.
    """

    target: str = Field(..., min_length=1)
    name: str | None = None
    enabled: bool | None = None
    action: str | None = None
    operator: str = "ring3.operator"

    @field_validator("target")
    @classmethod
    def _target_ok(cls, value: str) -> str:
        token = str(value).strip().lower().replace("-", "_")
        if token == "mcp":
            token = "mcp_tool"
        if token not in ("skill", "plugin", "mcp_tool", "forge"):
            raise ValueError("target must be skill, plugin, mcp_tool or forge")
        return token


class FeedControlRequest(BaseModel):
    """``POST /api/v2/control/feeds`` payload (Phase 3 Step 2).

    Hot-swaps the market-data path between the deterministic paper autopilot and
    the live multi-silo dual-vendor public feed.  ``ingest_cycle`` runs one
    immediate four-silo poll so the operator sees proof instead of a promise.
    """

    feed_mode: str = Field(..., min_length=1)
    ingest_cycle: bool = True
    operator: str = "ring3.operator"

    @field_validator("feed_mode")
    @classmethod
    def _feed_mode_ok(cls, value: str) -> str:
        token = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {
            "PAPER": "PAPER_AUTOPILOT",
            "PAPER_AUTOPILOT": "PAPER_AUTOPILOT",
            "LIVE": "LIVE_PUBLIC_DUAL_VENDOR",
            "LIVE_PUBLIC": "LIVE_PUBLIC_DUAL_VENDOR",
            "LIVE_PUBLIC_DUAL_VENDOR": "LIVE_PUBLIC_DUAL_VENDOR",
        }
        if token not in aliases:
            raise ValueError("feed_mode must be PAPER_AUTOPILOT or LIVE_PUBLIC_DUAL_VENDOR")
        return aliases[token]


class BrokerCredentialRequest(BaseModel):
    """``POST /api/v2/control/brokers/credentials`` payload (``sec_011``).

    ``api_key`` and ``api_secret`` are :class:`SecretStr`, so Pydantic never
    renders them in ``repr``, validation errors or structured logs.  They are
    encrypted inside Ring 0 before any response object exists.
    """

    silo: str = Field(..., min_length=1)
    venue: str = Field(..., min_length=1)
    api_key: SecretStr
    api_secret: SecretStr
    sandbox: bool = True
    operator: str = "ring3.operator"

    @field_validator("silo", "venue")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        token = str(value).strip()
        if not token:
            raise ValueError("must be non-empty")
        return token


class LLMProbeRequest(BaseModel):
    """``POST /api/v2/control/llms/probe`` payload (``sec_016``).

    The probe is read-only: it measures reachability and latency under the
    strict 250 ms hard cap and never mutates the active provider.
    """

    timeout_ms: int | None = Field(default=None, ge=1, le=5_000)
    operator: str = "ring3.operator"


class SupervisorControlRequest(BaseModel):
    """``POST /api/v2/control/supervisor`` payload (``sec_004``).

    ``action`` is one of ``start`` / ``stop`` / ``restart_ring`` / ``status``.
    ``ring_index`` identifies the ring for ``restart_ring``; ``rings`` narrows
    which workers ``start`` spawns (default: all four).
    """

    action: str = Field(..., min_length=1)
    ring_index: int | None = Field(default=None, ge=0, le=3)
    rings: list[int] | None = None
    silo: str | None = None
    symbol: str | None = None
    feed_mode: str | None = None
    operator: str = "ring3.operator"

    @field_validator("action")
    @classmethod
    def _action_ok(cls, value: str) -> str:
        token = str(value).strip().lower().replace("-", "_")
        aliases = {
            "start": "start",
            "spawn": "start",
            "stop": "stop",
            "shutdown": "stop",
            "restart": "restart_ring",
            "restart_ring": "restart_ring",
            "restart_worker": "restart_ring",
            "status": "status",
        }
        if token not in aliases:
            raise ValueError("action must be start, stop, restart_ring or status")
        return aliases[token]

    @field_validator("rings")
    @classmethod
    def _rings_ok(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        clean = sorted({int(item) for item in value})
        if not clean or any(index < 0 or index > 3 for index in clean):
            raise ValueError("rings must be a non-empty subset of 0..3")
        return clean

    @field_validator("feed_mode")
    @classmethod
    def _feed_mode_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        token = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {
            "PAPER": "PAPER_AUTOPILOT",
            "PAPER_AUTOPILOT": "PAPER_AUTOPILOT",
            "LIVE": "LIVE_PUBLIC_DUAL_VENDOR",
            "LIVE_PUBLIC": "LIVE_PUBLIC_DUAL_VENDOR",
            "LIVE_PUBLIC_DUAL_VENDOR": "LIVE_PUBLIC_DUAL_VENDOR",
        }
        if token not in aliases:
            raise ValueError("feed_mode must be PAPER_AUTOPILOT or LIVE_PUBLIC_DUAL_VENDOR")
        return aliases[token]


class PreflightRequest(BaseModel):
    """``POST /api/v2/control/preflight`` payload (Phase 4 Step 2).

    ``full_smoke=False`` is the interactive fast path: gate 5 replays the
    persisted 22-row smoke receipt for this commit instead of re-spawning the
    suite.  ``full_smoke=True`` re-runs every gate from scratch and is the
    release-gate form.
    """

    full_smoke: bool = False
    silo: str = "ALL"
    refresh: bool = True

    @field_validator("silo")
    @classmethod
    def _silo_ok(cls, value: str) -> str:
        token = str(value or "ALL").strip().upper()
        return token or "ALL"


class ForgeHedgeRequest(BaseModel):
    """``POST /api/v2/control/supervisor/forge-hedge`` payload (Step 2).

    The endpoint runs one *closed-loop* cycle: Ring 2 audits/demotes the
    champion, disarms its shared-memory lease, breeds challengers, runs the
    three-tick contagion cascade and prices a grounded protective put; Ring 0
    verifies and Ed25519-signs it.  Nothing here can reach a broker socket.
    """

    strategy_id: str | None = None
    champion_fills_bps: list[float] | None = None
    champion_symbols: list[str] | None = None
    challengers: int = Field(default=4, ge=0, le=64)
    price_shock_bps: float = Field(default=180.0, gt=0.0, le=5_000.0)
    tail_correlation: float = Field(default=0.68, ge=-1.0, le=1.0)
    spot: float = Field(default=50_000.0, gt=0.0)
    request_id: str | None = None
    timeout_s: float = Field(default=30.0, gt=0.0, le=120.0)
    operator: str = "ring3.operator"


class TestnetOrderRequest(BaseModel):
    """``POST /api/v2/control/brokers/testnet-order`` payload (``sec_011``).

    Sandbox-only by construction: ``sandbox`` must be ``True`` and
    ``confirm_sandbox`` is the explicit human acknowledgement that this is a
    testnet/paper venue, never production capital.
    """

    silo: str = Field(..., min_length=1)
    venue: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: str = "BUY"
    qty: float = Field(..., gt=0.0)
    limit_price: float = Field(..., gt=0.0)
    proposal_id: str | None = None
    strategy_id: str = "ring3-testnet-dispatch-v1"
    expected_gross_bps: float = 18.0
    spread_bps: float = 1.2
    account_id: str | None = None
    sandbox: bool = True
    confirm_sandbox: bool = True
    operator: str = "ring3.operator"

    @field_validator("side")
    @classmethod
    def _side_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if token not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        return token

    @field_validator("sandbox")
    @classmethod
    def _sandbox_only(cls, value: bool) -> bool:
        if not value:
            raise ValueError(
                "the testnet bridge only dispatches sandbox=True orders; live "
                "capital requires POST /api/v2/control/brokers"
            )
        return True

    @field_validator("confirm_sandbox")
    @classmethod
    def _confirm(cls, value: bool) -> bool:
        if not value:
            raise ValueError("confirm_sandbox must be true to acknowledge a paper venue")
        return True


# ---------------------------------------------------------------------------
# Mutable control state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _LLMSlot:
    provider: str
    model: str
    base_url: str
    daily_spend_cap_usd: float
    timeout_ms: int = 250
    spend_usd: float = 0.0
    api_key_masked: str | None = None
    api_key_set: bool = False
    last_latency_ms: float = 0.0
    circuit_open: bool = False
    updated_at_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "daily_spend_cap_usd": self.daily_spend_cap_usd,
            "spend_usd": round(self.spend_usd, 6),
            "budget_remaining_usd": round(
                max(0.0, self.daily_spend_cap_usd - self.spend_usd), 6
            ),
            "timeout_ms": self.timeout_ms,
            "api_key_masked": self.api_key_masked,
            "api_key_set": self.api_key_set,
            "api_key_write_only": True,
            "last_latency_ms": self.last_latency_ms,
            "circuit_open": self.circuit_open,
            "updated_at_ns": self.updated_at_ns,
        }


@dataclass(slots=True)
class _BrokerState:
    maker_fee_bps: float
    taker_fee_bps: float
    statutory_tax_bps: float
    spread_bps: float
    max_daily_loss_micros: int
    allocation: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_ALLOCATION))
    updated_at_ns: int = 0

    def cost_floor(self, silo: str) -> SiloCostFloor:
        return SiloCostFloor(
            spread_bps=self.spread_bps,
            maker_fee_bps=self.maker_fee_bps,
            taker_fee_bps=self.taker_fee_bps,
            statutory_tax_bps=self.statutory_tax_bps,
            source=f"Ring 3 control plane override for {silo}",
        )


class ControlPlane:
    """In-memory control + read model shared by REST and SSE.

    All dependencies are Ring 0 objects owned by the orchestrator; the control
    plane never constructs a broker adapter and never holds a signing key.
    """

    def __init__(
        self,
        *,
        ledger: IBORLedger,
        firewall: SovereignPreTradeFirewall,
        outbox: CrashAtomicOutbox,
        oms: SmartOMS,
        adr009: ADR009ReduceOnlyFastPath,
        registry: MarketSiloRegistry | None = None,
        lease_table: Any = None,
        kill_latch: Any = None,
        mirror: Any = None,
        feature_store: Any = None,
        bus: LocalJetStreamBus | Any | None = None,
        engine: Any = None,
        contagion_provider: Any = None,
        grounding: Any = None,
        guard: SiloGuard | None = None,
        key_vault: LLMKeyVault | None = None,
        llm_budget_usd: float = DEFAULT_DAILY_LLM_BUDGET_USD,
        strategy_id: str = "spinal-scalper-v1",
        scanner: Any = None,
        shadow_twin: Any = None,
        cascade_runner: Any = None,
        forge: Any = None,
        plugins: Any = None,
        skills: Any = None,
        mcp_bridge: Any = None,
        token_ledger: Any = None,
        llm_transport: Any = None,
        formal_verifier: Any = None,
        vopr_simulator: Any = None,
        tb_pg_provisioner: Any = None,
        tail_hedger: Any = None,
        autopilot_runner: Any = None,
        autopilot_provider: Any = None,
        process_supervisor: Any = None,
        feed_mode: str | None = None,
        feed_connector: Any = None,
        feed_guard: Any = None,
        broker_vault: Any = None,
        llm_probe_fetcher: Any = None,
        testnet_bridge: Any = None,
        forge_hedge_runner: Any = None,
    ) -> None:
        self.ledger = ledger
        self.firewall = firewall
        self.outbox = outbox
        self.oms = oms
        self.adr009 = adr009
        self.registry = registry if registry is not None else default_registry()
        self.lease_table = lease_table
        self.kill_latch = kill_latch
        self.mirror = mirror
        self.feature_store = feature_store
        self.bus = bus
        self.engine = engine
        self.contagion_provider = contagion_provider
        self.grounding = grounding
        self.guard = guard if guard is not None else getattr(firewall, "guard", None)
        self.key_vault = key_vault if key_vault is not None else LLMKeyVault()
        self.strategy_id = str(strategy_id)
        self.llm_budget_usd = float(llm_budget_usd)
        self.scanner = scanner
        self.shadow_twin = shadow_twin
        self.cascade_runner = cascade_runner
        self.llm_transport = llm_transport
        self.formal_verifier = formal_verifier
        self.vopr_simulator = vopr_simulator
        self.tb_pg_provisioner = tb_pg_provisioner
        self.tail_hedger = tail_hedger
        # The PAPER autopilot is supplied by the orchestrator; Ring 3 only
        # schedules it inside the FastAPI lifespan and never trades directly.
        self.autopilot_runner = autopilot_runner
        self.autopilot_provider = autopilot_provider
        # Phase 4 Step 2: the single-process forge + tail-hedge closed loop is a
        # composition-root callback (the orchestrator owns the Ring 0 signer), so
        # Ring 3 triggers the cycle without ever holding a signing key.
        self.forge_hedge_runner = forge_hedge_runner
        self._preflight_cache: dict[tuple[bool, str], tuple[float, dict[str, Any]]] = {}
        self._preflight_lock = threading.Lock()
        self._last_preflight: dict[str, Any] | None = None
        # ``sec_004``: the 4-ring topology is only real if the cockpit can prove
        # four distinct OS processes are alive.  The supervisor is injected so
        # single-process mode still renders (with an explicit "not attached").
        self.process_supervisor = process_supervisor
        self._feed_mode = str(feed_mode) if feed_mode else None
        # Phase 3 Step 2: the multi-silo public dual-vendor connector and the
        # write-only broker credential vault are injected so tests and the
        # 4-ring supervisor can each supply their own instance.
        self.feed_connector = feed_connector
        # ``guard`` above is Ring 0's SiloGuard (reconciliation drift).  The
        # feed cross-check needs Ring 2's SelfHealingDataGuard, which is a
        # different object with a different contract.
        if feed_guard is not None:
            self.feed_guard = feed_guard
        elif hasattr(guard, "ingest_bar"):
            self.feed_guard = guard
        else:
            self.feed_guard = None
        self.broker_vault = broker_vault
        self._llm_probe_fetcher = llm_probe_fetcher
        self._feed_report: dict[str, Any] = {}
        self._credential_reports: dict[str, dict[str, Any]] = {}
        self._llm_probe: dict[str, Any] = {}
        # Phase 3 Step 3: the Ring 0 sandbox execution bridge.  Injected so tests
        # can supply a mock httpx transport, or built lazily on first use.
        self.testnet_bridge = testnet_bridge
        self._testnet_receipts: list[dict[str, Any]] = []
        self._supervisor_action_log: list[dict[str, Any]] = []
        self.autopilot_interval_s = AUTOPILOT_INTERVAL_S
        self._bus_cursor = 0
        self._formal_cache: dict[str, Any] | None = None
        if token_ledger is not None:
            self.token_ledger = token_ledger
        else:
            try:
                from core.llm_transport import TokenBudgetLedger

                self.token_ledger = TokenBudgetLedger(daily_usd_cap=self.llm_budget_usd)
            except Exception:  # pragma: no cover - Ring 2 optional in isolation
                self.token_ledger = None

        self._llm: dict[str, _LLMSlot] = {
            "1": _LLMSlot(
                provider="ollama",
                model="qwen2.5:3b",
                base_url="http://127.0.0.1:11434",
                daily_spend_cap_usd=round(self.llm_budget_usd * 0.35, 4),
                timeout_ms=250,
                updated_at_ns=time.time_ns(),
            ),
            "2": _LLMSlot(
                provider="deepseek",
                model="deepseek-r1",
                base_url="https://api.deepseek.com",
                daily_spend_cap_usd=round(self.llm_budget_usd * 0.65, 4),
                timeout_ms=15_000,
                updated_at_ns=time.time_ns(),
            ),
        }
        self._brokers: dict[str, _BrokerState] = {}
        for silo in self.registry.silos:
            floor = silo.cost_floor
            self._brokers[silo.silo_id] = _BrokerState(
                maker_fee_bps=floor.maker_fee_bps,
                taker_fee_bps=floor.taker_fee_bps,
                statutory_tax_bps=floor.statutory_tax_bps,
                spread_bps=floor.spread_bps,
                max_daily_loss_micros=int(
                    firewall.daily_loss_stops.get(
                        silo.silo_id, 5_000_000_000
                    )
                ),
                updated_at_ns=time.time_ns(),
            )
        # Restore masked state for any key already in the vault.
        for slot in ("1", "2"):
            token = self.key_vault.masked(f"llm.system{slot}")
            if token is not None:
                self._llm[slot].api_key_masked = token
                self._llm[slot].api_key_set = True

        self._cards: list[dict[str, Any]] = []
        self._card_sequence = 0
        self._kill_report: dict[str, Any] | None = None

        # ---- Step 8 extension surfaces --------------------------------
        if forge is None:
            from ring2_brain.forge.alpha_forge import NocturnalAlphaForge

            forge = NocturnalAlphaForge(
                lease_table=self.lease_table,
                shadow_twin=self.shadow_twin,
                silos=self.registry.silo_ids(),
            )
        self.forge = forge
        if plugins is None:
            from core.plugins import PluginRegistry

            plugins = PluginRegistry(
                forge=self.forge, shadow_twin=self.shadow_twin
            )
        self.plugins = plugins
        if skills is None:
            from ring2_brain.skills.skill_registry import SkillRegistry

            skills = SkillRegistry()
        self.skills = skills
        if mcp_bridge is None:
            from ring3_cockpit.gateway.mcp_bridge import MCPBridge

            mcp_bridge = MCPBridge(control=self)
        self.mcp = mcp_bridge

    # -- reads ------------------------------------------------------------
    def broker_state(self, key: str) -> dict[str, Any]:
        silo = self.registry.get(key)
        state = self._brokers[silo.silo_id]
        floor = state.cost_floor(silo.silo_id)
        return {
            "silo_id": silo.silo_id,
            "market": str(silo.market),
            "label": silo.label,
            "venue": silo.venue,
            "mode": str(silo.mode),
            "venue_ready": silo.venue_ready,
            "trading_state": str(self.firewall.trading_state(silo.silo_id)),
            "frozen": bool(self.guard and self.guard.is_frozen(silo.silo_id)),
            "freeze_reason": self.guard.reason(silo.silo_id) if self.guard else "",
            "cost_floor": {
                "spread_bps": floor.spread_bps,
                "maker_fee_bps": floor.maker_fee_bps,
                "taker_fee_bps": floor.taker_fee_bps,
                "statutory_tax_bps": floor.statutory_tax_bps,
                "fee_stt_bps": floor.fee_stt_bps,
                "round_trip_cost_bps": floor.round_trip_cost_bps,
                "source": floor.source,
            },
            "max_daily_loss_micros": state.max_daily_loss_micros,
            "realized_pnl_micros": self.firewall.realized_pnl(silo.silo_id),
            "daily_loss_exceeded": self.firewall.daily_loss_exceeded(silo.silo_id),
            "allocation": dict(state.allocation),
            "adapters": [probe.to_dict() for probe in silo.adapters],
            "ready_adapters": [probe.name for probe in silo.ready_adapters],
            "updated_at_ns": state.updated_at_ns,
        }

    def brokers(self) -> dict[str, Any]:
        return {
            "order": list(self.registry.silo_ids()),
            "silos": {
                silo_id: self.broker_state(silo_id)
                for silo_id in self.registry.silo_ids()
            },
        }

    def cost_floor(self, silo: str) -> SiloCostFloor:
        self.registry.get(silo)  # validate
        return self._brokers[self.registry.get(silo).silo_id].cost_floor(silo)

    def _vaults(self, silo: str) -> dict[str, Any]:
        balances = self.ledger.vault_balances(silo)
        payload: dict[str, Any] = {}
        for vault in VaultName:
            micros = int(balances.get(vault, 0))
            payload[vault.value] = {
                "micros": micros,
                "nav": micros / 1_000_000.0,
            }
        payload["nav_micros"] = int(self.ledger.silo_nav_micros(silo))
        payload["nav"] = payload["nav_micros"] / 1_000_000.0
        return payload

    def _abstention(self) -> dict[str, Any]:
        engine = self.engine
        stats: dict[str, Any] = {}
        if engine is not None:
            try:
                stats = dict(engine.stats)
            except Exception:  # pragma: no cover - defensive
                stats = {}
        ticks = int(stats.get("ticks", 0))
        abstentions = int(stats.get("abstentions", 0))
        rate = (abstentions / ticks) if ticks else 0.0
        return {
            "target_rate": 0.991,
            "rate": rate,
            "abstentions": abstentions,
            "ticks": ticks,
            "trades": int(stats.get("live_proposals", 0)),
            "shadow_fills": int(stats.get("shadow_fills", 0)),
            "breaker_trips": int(stats.get("breaker_trips", 0)),
            "ring0_rejects": int(stats.get("ring0_rejects", 0)),
        }

    def _leases(self) -> list[dict[str, Any]]:
        table = self.lease_table
        if table is None:
            return []
        now = time.monotonic_ns()
        rows: list[dict[str, Any]] = []
        for lease in table.iter_active_leases(now):
            remaining = lease.remaining_ns(now)
            rows.append(
                {
                    "symbol": lease.symbol,
                    "mode": lease.mode.name,
                    "target_bps": lease.target_bps,
                    "stop_bps": lease.stop_bps,
                    "expires_at_ns": lease.expires_at_ns,
                    "remaining_ns": remaining,
                    "ttl_seconds": remaining / 1e9,
                    "ttl_pct": min(1.0, remaining / float(LEASE_TTL_NS)),
                    "strategy_id_hash": lease.strategy_id_hash,
                }
            )
        return rows

    def _contagion(self) -> dict[str, Any]:
        result = None
        provider = self.contagion_provider
        if callable(provider):
            result = provider()
        elif provider is not None:
            result = getattr(provider, "last_result", None)
        features = getattr(result, "features", None)
        payload: dict[str, Any] = {
            "available": features is not None,
            "archetypes": [
                {"name": name, "weight": weight}
                for name, weight in ARCHETYPE_WEIGHTS
            ],
            "archetype_groups": [dict(group) for group in ARCHETYPE_GROUPS],
            "archetype_count": len(ARCHETYPE_WEIGHTS),
            "fragility_halt_threshold": 0.75,
        }
        if features is not None:
            payload.update(features.to_dict())
            payload["tick_flows"] = list(getattr(result, "tick_flows", []))
            payload["latency_ms"] = getattr(result, "latency_ms", 0.0)
            payload["revoked_long_leases"] = getattr(
                result, "revoked_long_leases", 0
            )
        else:
            payload.update(
                {
                    "symbol": "",
                    "crowd_asymmetry_score": 0.0,
                    "cascade_fragility_index": 0.0,
                    "narrative_exhaustion_divergence": 0.0,
                    "requires_ring1_halt": False,
                }
            )
        grounding = self.grounding
        if grounding is not None:
            try:
                entries = len(grounding.entries())
            except Exception:  # pragma: no cover - defensive
                entries = 0
            try:
                verified = bool(grounding.verify_all())
            except Exception:  # pragma: no cover - defensive
                verified = False
            latest = None
            try:
                packets = grounding.packets()
                latest = packets[-1].grounding_hash if packets else None
            except Exception:  # pragma: no cover - defensive
                latest = None
            payload["grounding_ledger"] = {
                "entries": entries,
                "verified": verified,
                "latest_digest": latest,
            }
        else:
            payload["grounding_ledger"] = {
                "entries": 0,
                "verified": True,
                "latest_digest": None,
            }
        return payload

    def _llm_state(self) -> dict[str, Any]:
        total_spend = sum(slot.spend_usd for slot in self._llm.values())
        ledger = (
            self.token_ledger.snapshot()
            if self.token_ledger is not None
            else {}
        )
        transport = (
            self.llm_transport.status()
            if self.llm_transport is not None
            else {}
        )
        return {
            "daily_spend_cap_usd": self.llm_budget_usd,
            "daily_cap_usd": ledger.get("daily_cap_usd", self.llm_budget_usd),
            "daily_spend_usd": ledger.get(
                "daily_spend_usd", round(total_spend, 6)
            ),
            "budget_remaining_usd": ledger.get(
                "budget_remaining_usd",
                round(max(0.0, self.llm_budget_usd - total_spend), 6),
            ),
            "baseline_usd_per_day": ledger.get("baseline_usd_per_day"),
            "cost_reduction_pct": ledger.get("cost_reduction_pct", 0.0),
            "realized_cost_reduction_pct": ledger.get(
                "realized_cost_reduction_pct", 0.0
            ),
            "active_system1_provider": ledger.get(
                "active_system1_provider", self._llm["1"].provider
            ),
            "active_system2_provider": ledger.get(
                "active_system2_provider", self._llm["2"].provider
            ),
            "demoted_providers": ledger.get("demoted_providers", []),
            "demotion_reasons": ledger.get("demotion_reasons", {}),
            "locked": ledger.get("locked", False),
            "locked_reason": ledger.get("locked_reason", ""),
            "locked_providers": ledger.get("locked_providers", []),
            "calls": ledger.get("calls", 0),
            "prompt_tokens": ledger.get("prompt_tokens", 0),
            "completion_tokens": ledger.get("completion_tokens", 0),
            "total_tokens": ledger.get("total_tokens", 0),
            "system_spend_usd": ledger.get("system_spend_usd", {}),
            "system_tokens": ledger.get("system_tokens", {}),
            "by_provider": ledger.get("by_provider", {}),
            "transport": transport,
            "write_only_credentials": True,
            "system1": self._llm["1"].to_dict(),
            "system2": self._llm["2"].to_dict(),
        }

    def system_status(self) -> dict[str, Any]:
        latch_hot = bool(self.kill_latch is not None and self.kill_latch.is_halted)
        frozen = bool(self.guard.frozen_silos) if self.guard is not None else False
        halted = latch_hot or frozen
        reason = ""
        if self.guard is not None and self.guard.frozen_silos:
            reason = self.guard.reason(self.guard.frozen_silos[0])
        if latch_hot and not reason:
            reason = "GLOBAL_KILL_LATCH"
        return {
            "status": "HALTED" if halted else "ARMED",
            "kill_latch_hot": latch_hot,
            "halt_reason": reason,
            "timestamp_ns": time.time_ns(),
        }

    def _extensions_state(self) -> dict[str, Any]:
        """Skills, plugins, MCP tools and Forge status for the Cockpit."""
        skills = self.skills.describe() if self.skills is not None else {}
        plugins = (
            self.plugins.snapshot()
            if self.plugins is not None
            else {"count": 0, "plugins": []}
        )
        mcp = (
            self.mcp.summary()
            if self.mcp is not None
            else {"count": 0, "tools": []}
        )
        forge = (
            self.forge.snapshot()
            if self.forge is not None
            else {"champion_count": 0, "challenger_count": 0}
        )
        return {
            "skills": skills,
            "plugins": plugins,
            "mcp": mcp,
            "forge": forge,
            "counts": {
                "skills": int(skills.get("count", 0)),
                "plugins": int(plugins.get("count", 0)),
                "mcp_tools": int(mcp.get("count", 0)),
                "champions": int(forge.get("champion_count", 0)),
                "challengers": int(forge.get("challenger_count", 0)),
            },
            "air_gap": {
                "ring3_read_only": True,
                "order_signing_tools": 0,
                "key_reading_tools": 0,
                "mcp_air_gap_pass": bool(
                    mcp.get("air_gap", {}).get("pass", False)
                ),
            },
        }

    def extensions_state(self) -> dict[str, Any]:
        return self._extensions_state()

    def apply_extension(self, request: ExtensionsControlRequest) -> dict[str, Any]:
        """Toggle a skill/plugin/MCP tool or run a manual Forge audit."""
        target = str(request.target)
        name = (request.name or "").strip()
        if target == "forge":
            if self.forge is None:
                raise HTTPException(status_code=503, detail="Forge unavailable")
            action = (request.action or "run_audit").strip().lower()
            if action not in ("run_audit", "audit", "run"):
                raise HTTPException(
                    status_code=400,
                    detail="forge action must be run_audit",
                )
            report = self.forge.audit()
            return {
                "updated": ["forge.audit"],
                "report": report.to_dict(),
                "extensions": self._extensions_state(),
                "zero_restart": True,
            }
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        enabled = True if request.enabled is None else bool(request.enabled)
        try:
            if target == "skill":
                if self.skills is None:
                    raise HTTPException(status_code=503, detail="skills unavailable")
                updated = self.skills.set_enabled(name, enabled)
                return {
                    "updated": [f"skill:{name}"],
                    "item": updated,
                    "extensions": self._extensions_state(),
                    "zero_restart": True,
                }
            if target == "plugin":
                if self.plugins is None:
                    raise HTTPException(status_code=503, detail="plugins unavailable")
                updated = self.plugins.toggle(name, enabled)
                return {
                    "updated": [f"plugin:{name}"],
                    "item": updated,
                    "extensions": self._extensions_state(),
                    "zero_restart": True,
                }
            if target == "mcp_tool":
                if self.mcp is None:
                    raise HTTPException(status_code=503, detail="MCP bridge unavailable")
                updated = self.mcp.set_enabled(name, enabled)
                return {
                    "updated": [f"mcp_tool:{name}"],
                    "item": updated,
                    "extensions": self._extensions_state(),
                    "zero_restart": True,
                }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=f"unsupported target {target!r}")

    def formal_proofs(self, *, refresh: bool = False) -> dict[str, Any]:
        """Live status of the four TLA+ proofs, VOPR, backends and hedger.

        The heavy campaigns are cached after the first invocation so the 60 FPS
        SSE loop and the hydration snapshot stay inexpensive.  ``refresh=True``
        re-runs all four state-space explorations and the 1,000-step VOPR.
        """

        if self._formal_cache is not None and not refresh:
            return self._formal_cache

        formal: dict[str, Any] | None = None
        if self.formal_verifier is not None:
            formal = self.formal_verifier.run(refresh=refresh)
        elif self.vopr_simulator is not None:
            formal = {
                "tla": None,
                "vopr": self.vopr_simulator.run_campaign(refresh=refresh).to_dict(),
                "ok": False,
            }

        if self.tb_pg_provisioner is None:
            from ring0_vault.ibor.tb_pg_provisioner import TigerBeetlePG16Provisioner

            self.tb_pg_provisioner = TigerBeetlePG16Provisioner()
        backend = self.tb_pg_provisioner.status(refresh=refresh)
        hedger = self.tail_hedger.status() if self.tail_hedger is not None else None

        tla = formal.get("tla") if formal else None
        vopr = formal.get("vopr") if formal else None
        counterexamples = int((tla or {}).get("total_counterexamples", 0))
        states = int((tla or {}).get("total_states", 0))
        payload = {
            "kind": "nios_phase2_step4_formal_proofs",
            "tla": tla,
            "vopr": vopr,
            "tigerbeetle": backend.get("tigerbeetle"),
            "postgres": backend.get("postgres"),
            "tail_hedger": hedger,
            "summary": {
                "states_explored": states,
                "counterexamples": counterexamples,
                "tla_ok": bool((tla or {}).get("ok", False)),
                "vopr_ok": bool((vopr or {}).get("ok", False)),
                "tigerbeetle_ready": bool(
                    (backend.get("tigerbeetle") or {}).get("ready", False)
                ),
                "postgres_ready": bool(
                    (backend.get("postgres") or {}).get("ready", False)
                ),
                "tail_hedger_ready": hedger is not None,
            },
            "ok": bool(
                (tla or {}).get("ok", False)
                and (vopr or {}).get("ok", False)
                and (backend.get("tigerbeetle") or {}).get("ready", False)
                and (backend.get("postgres") or {}).get("ready", False)
                and hedger is not None
            ),
            "checked_at_ns": time.time_ns(),
        }
        self._formal_cache = payload
        return payload

    def ring_processes(self) -> dict[str, Any]:
        """Four-ring OS process table with shared-memory heartbeat ages.

        Returns ``available: False`` (never an exception) when the cockpit runs
        in single-process mode, so the UI can say "not attached" instead of
        implying the rings are down.
        """

        try:
            from core.process_supervisor import ring_process_snapshot
        except Exception as exc:  # pragma: no cover - supervisor is optional
            return {
                "available": False,
                "reason": f"process supervisor unavailable: {type(exc).__name__}",
                "rings": [],
                "pids": {},
                "distinct_os_processes": 0,
            }
        return ring_process_snapshot(self.process_supervisor)

    # -- Phase 3 Step 3: live 4-ring supervisor control -------------------
    def supervisor_state(self) -> dict[str, Any]:
        """Live four-ring OS process table plus the control-surface contract."""

        rings = self.ring_processes()
        attached = self.process_supervisor is not None
        distinct = int(rings.get("distinct_os_processes", 0) or 0)
        return {
            "available": bool(rings.get("available", False)),
            "attached": attached,
            "running": bool(rings.get("running", False)),
            "distinct_os_processes": distinct,
            "four_ring_isolation": distinct == 4,
            "pids": rings.get("pids", {}),
            "rings": rings.get("rings", []),
            "heartbeat_shm": rings.get("heartbeat_shm"),
            "shm_prefix": rings.get("shm_prefix"),
            "silo": rings.get("silo"),
            "symbol": rings.get("symbol"),
            "feed_mode": rings.get("feed_mode") or self.feed_state()["feed_mode"],
            "reason": rings.get("reason"),
            "actions": ["start", "stop", "restart_ring", "status"],
            "workers": [
                "ring0-vault-worker",
                "ring1-spinal-worker",
                "ring2-brain-worker",
                "ring3-cockpit-gateway",
            ],
            "last_actions": list(self._supervisor_action_log[-8:]),
            "law": "sec_004 - four physically separate OS processes, SHM heartbeats",
        }

    def _ensure_supervisor(self, request: SupervisorControlRequest) -> Any:
        """Attach a supervisor on first use, preserving any injected instance."""

        if self.process_supervisor is not None:
            return self.process_supervisor
        from core.process_supervisor import RING_SYMBOLS, RingProcessSupervisor

        silo = str(request.silo or "CRYPTO_BINANCE").strip().upper()
        self.process_supervisor = RingProcessSupervisor(
            silo=silo,
            symbol=request.symbol or RING_SYMBOLS.get(silo),
            feed_mode=request.feed_mode
            or self._feed_mode
            or "PAPER_AUTOPILOT",
            rings=tuple(request.rings or (0, 1, 2, 3)),
            fresh_heartbeats=True,
        )
        return self.process_supervisor

    def control_supervisor(self, request: SupervisorControlRequest) -> dict[str, Any]:
        """Start / stop / crash-restart the four OS worker processes.

        Ring 3 only supervises processes: it still cannot sign an order or read
        a broker key, and the kill switch continues to travel through the
        shared-memory latch rather than through this endpoint.
        """

        action = str(request.action)
        stamp = time.time_ns()

        if action == "status":
            return {
                "action": action,
                "changed": False,
                "operator": str(request.operator),
                "timestamp_ns": stamp,
                **self.supervisor_state(),
            }

        if action == "start":
            supervisor = self._ensure_supervisor(request)
            already = bool(self.ring_processes().get("running", False))
            if not already:
                supervisor.start()
            body = {
                "action": action,
                "changed": not already,
                "already_running": already,
                "started_rings": list(getattr(supervisor, "rings", ())),
                "operator": str(request.operator),
                "timestamp_ns": stamp,
            }
            # Give the freshly spawned workers a bounded window to write their
            # first SHM heartbeat so the caller sees PIDs, not zeroes.
            self._await_heartbeats(timeout_s=3.0)
            body.update(self.supervisor_state())
            self._supervisor_action_log.append(
                {
                    "action": action,
                    "changed": not already,
                    "distinct_os_processes": body["distinct_os_processes"],
                    "at_ns": stamp,
                }
            )
            return body

        if action == "stop":
            supervisor = self.process_supervisor
            if supervisor is None:
                return {
                    "action": action,
                    "changed": False,
                    "stopped": False,
                    "error": "supervisor not attached (single-process mode)",
                    "operator": str(request.operator),
                    "timestamp_ns": stamp,
                    **self.supervisor_state(),
                }
            running = bool(self.ring_processes().get("running", False))
            try:
                supervisor.stop()
            except Exception as exc:  # pragma: no cover - teardown is best effort
                raise HTTPException(
                    status_code=500,
                    detail=f"supervisor stop failed: {type(exc).__name__}: {exc}",
                ) from None
            body = {
                "action": action,
                "changed": running,
                "stopped": not bool(self.ring_processes().get("running", False)),
                "operator": str(request.operator),
                "timestamp_ns": stamp,
            }
            body.update(self.supervisor_state())
            self._supervisor_action_log.append(
                {"action": action, "changed": running, "at_ns": stamp}
            )
            return body

        # action == "restart_ring"
        supervisor = self.process_supervisor
        if supervisor is None or not bool(self.ring_processes().get("running", False)):
            raise HTTPException(
                status_code=409,
                detail=(
                    "supervisor is not running; POST action=start before "
                    "restart_ring"
                ),
            )
        index = 0 if request.ring_index is None else int(request.ring_index)
        previous_pid = None
        try:
            previous_pid = supervisor.pid(index)
        except Exception:  # pragma: no cover - telemetry only
            previous_pid = None
        if previous_pid is None:
            try:
                table = supervisor.heartbeat_table_snapshot()
            except Exception:  # pragma: no cover
                table = {}
            for name, entry in table.items():
                if str(name).startswith(f"ring{index}-"):
                    previous_pid = entry.get("pid")
                    break
        result = supervisor.restart(index, reason=f"operator restart via {request.operator}")
        self._await_heartbeats(timeout_s=3.0)
        body = {
            "action": action,
            "changed": bool(result.get("restarted", False)),
            "ring_index": index,
            "restart": dict(result),
            "previous_pid": previous_pid,
            "new_pid": result.get("pid"),
            "pid_changed": bool(previous_pid) and previous_pid != result.get("pid"),
            "operator": str(request.operator),
            "timestamp_ns": stamp,
        }
        body.update(self.supervisor_state())
        self._supervisor_action_log.append(
            {
                "action": action,
                "ring_index": index,
                "restarted": bool(result.get("restarted", False)),
                "previous_pid": previous_pid,
                "new_pid": result.get("pid"),
                "at_ns": stamp,
            }
        )
        return body

    def _await_heartbeats(self, *, timeout_s: float = 3.0) -> bool:
        """Bounded wait for four live PIDs with fresh sub-second heartbeats."""

        deadline = time.monotonic() + max(0.2, float(timeout_s))
        while time.monotonic() < deadline:
            rings = self.ring_processes()
            entries = rings.get("rings") or []
            live = [
                entry
                for entry in entries
                if entry.get("alive") and entry.get("pid")
            ]
            fresh = [
                entry
                for entry in live
                if entry.get("heartbeat_age_ms") is not None
                and float(entry["heartbeat_age_ms"]) < 1_000.0
            ]
            # Every ring must be alive *and* beating: a freshly respawned worker
            # that has not written its first beat is not yet proven healthy.
            if len(live) == 4 and len(fresh) == 4:
                return True
            time.sleep(0.05)
        return False

    # -- Phase 4 Step 2: 22-gate pre-flight go-live certificate -----------
    @staticmethod
    def _preflight_body(report: Mapping[str, Any]) -> dict[str, Any]:
        """Normalise the CLI report into the cockpit/contract payload."""
        gates = [dict(gate) for gate in (report.get("gates") or [])]
        checklist = [
            f"{gate.get('status')} {gate.get('gate')}" for gate in gates
        ]
        return {
            "kind": "nios_golive_readiness_certificate",
            "release": str(report.get("release", "")),
            "schema": str(report.get("schema", "")),
            "signer": str(report.get("signer", "")),
            "silo_scope": str(report.get("silo_scope", "ALL")),
            "strict": bool(report.get("strict", False)),
            "full_smoke": bool(report.get("full_smoke", False)),
            "ready_for_live_cutover": bool(report.get("ready_for_live_cutover", False)),
            "passed_gates": int(report.get("gates_passed", 0) or 0),
            "failed_gates": int(report.get("failed_gates", 0) or 0),
            "warned_gates": int(report.get("warned_gates", 0) or 0),
            "total_gates": int(
                report.get("gates_expected", len(gates)) or len(gates)
            ),
            "failed_gate_ids": list(report.get("failed_gate_ids") or []),
            "warned_gate_ids": list(report.get("warned_gate_ids") or []),
            "certificate_sha256": str(report.get("certificate_sha256", "")),
            "certificate_digest_algorithm": str(
                report.get("certificate_digest_algorithm", "sha256")
            ),
            "duration_ms": report.get("duration_ms"),
            "generated_at_ns": report.get("generated_at_ns"),
            "gates": gates,
            "gate_checklist": checklist,
            "law": "sec_016 - nothing reaches LIVE until every invariant is certified",
        }

    def preflight_certificate(
        self,
        *,
        full_smoke: bool = False,
        silo: str = "ALL",
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Run (or replay) the 22-gate pre-flight go-live certificate.

        The certificate is deterministic over gate outcomes, so a short-lived
        in-process cache is both safe and necessary: the cockpit polls it and
        the ``LIVE`` cutover gate consults it on every request.  ``refresh``
        forces a fresh run; a failure to execute is a ``503``, never a silently
        optimistic certificate.
        """
        key = (bool(full_smoke), str(silo or "ALL").strip().upper() or "ALL")
        with self._preflight_lock:
            cached = self._preflight_cache.get(key)
            if (
                not refresh
                and cached is not None
                and (time.monotonic() - cached[0]) < PREFLIGHT_CACHE_TTL_S
            ):
                body = dict(cached[1])
                body["served_from"] = "in_process_cache"
                body["cache_age_s"] = round(time.monotonic() - cached[0], 3)
                return body

            from scripts.preflight_golive import run_preflight

            try:
                report = run_preflight(silo=silo, full_smoke=bool(full_smoke))
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "pre-flight certification could not run: "
                        f"{type(exc).__name__}"
                    ),
                ) from None
            body = self._preflight_body(report)
            self._preflight_cache[key] = (time.monotonic(), dict(body))
            self._last_preflight = body
            payload = dict(body)
            payload["served_from"] = "fresh_run"
            payload["cache_age_s"] = 0.0
            return payload

    def preflight_state(self, *, refresh: bool = False) -> dict[str, Any]:
        """``GET /api/v2/control/preflight`` -- interactive fast path."""
        certificate = self.preflight_certificate(
            full_smoke=False, silo="ALL", refresh=refresh
        )
        return {
            **certificate,
            "endpoint": "/api/v2/control/preflight",
            "full_smoke_available": True,
            "next_action": (
                "confirm_live=true may be authorised"
                if certificate["ready_for_live_cutover"]
                else "resolve every FAIL row before enabling confirm_live"
            ),
        }

    # -- Phase 4 Step 2: closed-loop forge + tail-hedge trigger -----------
    @staticmethod
    def _await_forge_hedge_events(
        supervisor: Any, request_id: str, *, timeout_s: float
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """Drain the supervisor bus until the cycle and its Ring 0 receipt land."""
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        cycle: dict[str, Any] | None = None
        signed: dict[str, Any] | None = None
        rejected: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                drained = supervisor.drain_events(limit=8192)
            except Exception:  # pragma: no cover - bus teardown
                drained = []
            for event in drained:
                kind = str(event.get("type"))
                if str(event.get("request_id") or "") != request_id:
                    continue
                if kind == "ring2_forge_hedge":
                    cycle = dict(event)
                elif kind == "ring0_hedge_signed":
                    signed = dict(event)
                elif kind in ("ring0_hedge_rejected", "ring2_forge_error"):
                    rejected = dict(event)
            if cycle is not None and (signed is not None or rejected is not None):
                return cycle, signed, rejected
            if rejected is not None:
                return cycle, signed, rejected
            time.sleep(0.05)
        return cycle, signed, rejected

    def run_forge_and_hedge(self, request: ForgeHedgeRequest) -> dict[str, Any]:
        """Execute one Nightly Alpha Forge + Tactical Tail-Hedger cycle.

        Cross-process mode drives the Ring 2 worker over its control channel and
        waits for the Ring 0 worker to verify and sign; single-process mode calls
        the orchestrator's composition-root runner.  Both return the same
        receipt shape, so the cockpit renders one contract.
        """
        started = time.monotonic()
        request_id = (
            str(request.request_id or "").strip()
            or f"forge-hedge-{uuid.uuid4().hex[:8]}"
        )
        state = self.supervisor_state()
        silo = str(state.get("silo") or "CRYPTO_BINANCE")
        from core.process_supervisor import RING_SYMBOLS

        symbols = [str(item) for item in (request.champion_symbols or []) if str(item)]
        if symbols:
            symbol = symbols[0]
        else:
            # Ring 0's whitelist is the authority: a symbol the firewall would
            # reject must never be priced into a hedge proposal (sec_004).
            allowed = sorted(
                str(item) for item in (self.firewall.whitelists.get(silo) or [])
            )
            symbol = (
                allowed[0]
                if allowed
                else str(state.get("symbol") or RING_SYMBOLS.get(silo, "BTCUSDT"))
            )
        fills = (
            [float(value) for value in request.champion_fills_bps]
            if request.champion_fills_bps
            else list(FORGE_HEDGE_DRILL_FILLS_BPS)
        )
        strategy_id = str(request.strategy_id or self.strategy_id)

        supervisor = self.process_supervisor
        running = bool(self.ring_processes().get("running", False))
        cycle: dict[str, Any] | None = None
        signature: dict[str, Any] | None = None
        rejected: dict[str, Any] | None = None

        if supervisor is not None and running:
            mode = "cross_process"
            command = supervisor.run_forge_and_hedge_cycle(
                strategy_id=strategy_id,
                champion_fills_bps=fills,
                champion_symbols=[symbol],
                challengers=int(request.challengers),
                price_shock_bps=float(request.price_shock_bps),
                tail_correlation=float(request.tail_correlation),
                spot=float(request.spot),
                request_id=request_id,
            )
            request_id = str(command.get("request_id") or request_id)
            cycle, signature, rejected = self._await_forge_hedge_events(
                supervisor, request_id, timeout_s=float(request.timeout_s)
            )
            if cycle is None:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Ring 2 did not answer the forge cycle within "
                        f"{float(request.timeout_s):.0f} s"
                    ),
                )
        else:
            mode = "single_process"
            runner = self.forge_hedge_runner
            if runner is None:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "forge+hedge runner is not attached: start the four OS "
                        "workers or run inside the single-process orchestrator"
                    ),
                )
            try:
                cycle = dict(
                    runner(
                        strategy_id=strategy_id,
                        champion_fills_bps=fills,
                        champion_symbols=[symbol],
                        challengers=int(request.challengers),
                        price_shock_bps=float(request.price_shock_bps),
                        tail_correlation=float(request.tail_correlation),
                        spot=float(request.spot),
                        request_id=request_id,
                    )
                )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"forge+hedge cycle failed: {type(exc).__name__}",
                ) from None
            signature = cycle.get("ring0_signature")
            if isinstance(signature, dict) and str(signature.get("type", "")) == "ring0_hedge_rejected":
                rejected = signature
                signature = None

        retirements = [dict(item) for item in (cycle.get("retirements") or [])]
        disarmed = sum(int(item.get("disarmed_leases", 0) or 0) for item in retirements)
        receipt: dict[str, Any] | None = None
        if signature:
            receipt = {
                "proposal_id": str(signature.get("proposal_id", "")),
                "plan_id": str(signature.get("plan_id", "")),
                "silo": str(signature.get("silo", silo)),
                "symbol": str(signature.get("symbol", symbol)),
                "vault": str(signature.get("vault", "TacticalSwing")),
                "hedge_role": str(signature.get("hedge_role", "INSURANCE")),
                "digest_hex": str(signature.get("digest_hex", "")),
                "signature_hex": str(signature.get("signature_hex", "")),
                "public_key_hex": str(signature.get("public_key_hex", "")),
                "signature_algorithm": "ed25519",
                "verified": bool(signature.get("verified", False)),
                "premium_bps": signature.get("premium_bps"),
                "strike": signature.get("strike"),
                "contracts": signature.get("contracts"),
                "grounding_hash": str(signature.get("grounding_hash", "")),
            }

        return {
            "kind": "nios_forge_hedge_receipt",
            "mode": mode,
            "request_id": request_id,
            "operator": str(request.operator),
            "silo": silo,
            "symbol": symbol,
            "strategy_id": strategy_id,
            "champion_id": str(cycle.get("champion_id") or strategy_id),
            "champion_status": cycle.get("champion_status"),
            "champion_rolling_net_bps": cycle.get("champion_rolling_net_bps"),
            "retired_champions": int(cycle.get("retired", 0) or 0),
            "disarmed_leases": disarmed,
            "retirements": retirements,
            "promoted_champions": int(cycle.get("promoted", 0) or 0),
            "genomes_evaluated": int(cycle.get("genomes_evaluated", 0) or 0),
            "cascade_fragility_index": cycle.get("cascade_fragility_index"),
            "tail_correlation": cycle.get("tail_correlation"),
            "tail_hedge_triggered": bool(cycle.get("tail_hedge_triggered", False)),
            "hedge_routed": bool(receipt is not None),
            "signed": bool(receipt is not None and receipt["verified"]),
            "ring0_signature": receipt,
            "rejection": (
                {"reason": str(rejected.get("reason", "")), **{
                    key: rejected[key]
                    for key in ("proposal_id", "premium_micros", "cap_micros")
                    if key in rejected
                }}
                if rejected
                else None
            ),
            "elapsed_ms": round((time.monotonic() - started) * 1e3, 3),
            "timestamp_ns": time.time_ns(),
            "law": (
                "sec_018 Laws 4+5 - shadow graduation, nightly forge, ADR-009 "
                "insurance route; Ring 2 proposes, Ring 0 signs"
            ),
        }

    # -- Phase 3 Step 3: Ring 0 sandbox execution bridge -----------------
    def _ensure_testnet_bridge(self) -> Any:
        if self.testnet_bridge is not None:
            return self.testnet_bridge
        from ring0_vault.silos.testnet_bridge import TestnetExecutionBridge

        self.testnet_bridge = TestnetExecutionBridge(
            self.outbox,
            self.ledger,
            vault=self.broker_vault,
            registry=self.registry,
            allow_network=False,
        )
        return self.testnet_bridge

    def testnet_state(self) -> dict[str, Any]:
        """Sandbox dispatch targets + the last receipts (never a raw key)."""

        from ring0_vault.silos.testnet_bridge import SANDBOX_VENUE_SPECS

        bridge = self.testnet_bridge
        body: dict[str, Any] = {
            "ring": "ring0_vault",
            "write_only_credentials": True,
            "sandbox_only": True,
            "bridge_attached": bridge is not None,
            "targets": {
                venue: spec.to_dict() for venue, spec in SANDBOX_VENUE_SPECS.items()
            },
            "receipts": list(self._testnet_receipts[-8:]),
            "law": "sec_016 - sandbox dispatch only; live capital is a separate endpoint",
        }
        if bridge is not None and hasattr(bridge, "status"):
            try:
                body["bridge"] = bridge.status()
            except Exception:  # pragma: no cover - telemetry is non-fatal
                body["bridge"] = {"status": "unavailable"}
        return body

    def _testnet_summary(self) -> dict[str, Any]:
        """Cheap hydration view of the sandbox bridge (no decryption)."""

        try:
            from ring0_vault.silos.testnet_bridge import SANDBOX_VENUE_SPECS
        except Exception:  # pragma: no cover - optional surface
            return {"sandbox_only": True, "venues": [], "receipts": []}
        return {
            "sandbox_only": True,
            "write_only_credentials": True,
            "bridge_attached": self.testnet_bridge is not None,
            "venues": {
                venue: {
                    "environment": spec.environment,
                    "base_url": spec.base_url,
                    "signing": spec.signing,
                }
                for venue, spec in SANDBOX_VENUE_SPECS.items()
            },
            "receipts": list(self._testnet_receipts[-5:]),
        }

    def dispatch_testnet_order(self, request: TestnetOrderRequest) -> dict[str, Any]:
        """Dispatch one signed sandbox order through the Ring 0 bridge."""

        from ring0_vault.silos.silo_registry import broker_credentials_present

        if not broker_credentials_present(request.venue, vault=self.broker_vault):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Ring 0 holds no sandbox credential for {request.venue!r}; "
                    "POST /api/v2/control/brokers/credentials first"
                ),
            )
        bridge = self._ensure_testnet_bridge()

        from core.schemas import UnsignedProposal, VaultName

        proposal_id = request.proposal_id or (
            f"testnet-{request.silo.lower()}-{request.venue}-{time.time_ns()}"
        )
        proposal = UnsignedProposal(
            proposal_id=proposal_id,
            strategy_id=str(request.strategy_id),
            silo=str(request.silo).strip().upper(),
            vault=VaultName.SPINAL_SCALPER,
            symbol=str(request.symbol).strip(),
            side=str(request.side),
            qty=float(request.qty),
            limit_price=float(request.limit_price),
            expected_gross_bps=float(request.expected_gross_bps),
            spread_bps=float(request.spread_bps),
            grounding_hash=_sha256_hex(
                f"{proposal_id}|{request.venue}|{request.symbol}|{request.limit_price}"
            ),
        )
        try:
            receipt = bridge.dispatch(
                request.silo,
                request.venue,
                proposal,
                sandbox=True,
                account_id=request.account_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a bridge failure is a 502, never a 500
            raise HTTPException(
                status_code=502,
                detail=f"sandbox dispatch failed: {type(exc).__name__}: {exc}",
            ) from None

        body = receipt.to_dict()
        body["operator"] = str(request.operator)
        body["zero_restart"] = True
        body["order"] = {
            "proposal_id": proposal.proposal_id,
            "symbol": proposal.symbol,
            "side": proposal.side,
            "qty": proposal.qty,
            "limit_price": proposal.limit_price,
            "grounding_hash": proposal.grounding_hash,
        }
        self._testnet_receipts.append(
            {
                key: body[key]
                for key in (
                    "venue",
                    "silo",
                    "environment",
                    "endpoint",
                    "http_status",
                    "accepted",
                    "committed",
                    "voided",
                    "broker_order_id",
                    "outbox_state",
                    "attempts",
                    "latency_ms",
                    "error",
                )
            }
        )
        if len(self._testnet_receipts) > 64:
            del self._testnet_receipts[:-64]
        return body


    def feed_state(self) -> dict[str, Any]:
        """Which market-data path is live: public dual-vendor or paper autopilot."""

        supervisor = self.process_supervisor
        mode = self._feed_mode
        if mode is None and supervisor is not None:
            mode = getattr(supervisor, "feed_mode", None)
        if mode is None:
            mode = "PAPER_AUTOPILOT"
        body: dict[str, Any] = {
            "feed_mode": str(mode),
            "live_public_dual_vendor": str(mode) == "LIVE_PUBLIC_DUAL_VENDOR",
            "paper_autopilot": str(mode) == "PAPER_AUTOPILOT",
            "available_modes": ["PAPER_AUTOPILOT", "LIVE_PUBLIC_DUAL_VENDOR"],
            "silo_modes": {},
            "law": "sec_018 Law 1 - no single venue is ever trusted",
        }
        connector = getattr(self, "feed_connector", None)
        if connector is not None and hasattr(connector, "status"):
            try:
                body["connector"] = connector.status()
            except Exception:  # pragma: no cover - telemetry is non-fatal
                body["connector"] = {"status": "unavailable"}
            body["silo_modes"] = (
                dict(connector.mode) if hasattr(connector, "mode") else {}
            )
        if self._feed_report:
            body["last_cycle"] = self._feed_report
        return body

    # -- Phase 3 Step 2: feed hot-swap --------------------------------
    def set_feed_mode(self, request: FeedControlRequest) -> dict[str, Any]:
        """Hot-swap the market-data path without restarting any worker.

        ``PAPER_AUTOPILOT`` is the deterministic in-process rehearsal tape.
        ``LIVE_PUBLIC_DUAL_VENDOR`` polls two independent public venues per silo
        and degrades *per silo* to labelled synthetic ticks if a venue is
        unreachable or the market is closed (``sec_018`` Law 1).
        """

        previous = self.feed_state()["feed_mode"]
        mode = str(request.feed_mode)
        self._feed_mode = mode

        # Propagate to the 4-ring supervisor so freshly respawned Ring 1/2
        # workers inherit the same feed path (no restart of the gateway).
        supervisor = self.process_supervisor
        if supervisor is not None and hasattr(supervisor, "feed_mode"):
            supervisor.feed_mode = mode

        response: dict[str, Any] = {
            "feed_mode": mode,
            "previous_feed_mode": previous,
            "changed": mode != previous,
            "operator": str(request.operator),
            "zero_restart": True,
            "live_public_dual_vendor": mode == "LIVE_PUBLIC_DUAL_VENDOR",
            "timestamp_ns": time.time_ns(),
        }

        if mode == "LIVE_PUBLIC_DUAL_VENDOR" and request.ingest_cycle:
            response["cycle"] = self._run_feed_cycle()
        elif mode == "LIVE_PUBLIC_DUAL_VENDOR":
            response["cycle"] = {
                "skipped": True,
                "reason": "ingest_cycle disabled by the operator",
                "round": 0,
                "frames": 0,
                "accepted": 0,
                "rejected": 0,
                "silos": {},
            }
        else:
            response["cycle"] = {
                "skipped": True,
                "reason": "paper autopilot drives its own deterministic tape",
                "round": 0,
                "frames": 0,
                "accepted": 0,
                "rejected": 0,
                "silos": {},
            }
        response["feed"] = self.feed_state()
        return response

    def _ensure_feed_connector(self) -> Any:
        if self.feed_connector is not None:
            return self.feed_connector
        from ring2_brain.truth.live_public_feeds import MultiSiloFeedConnector

        self.feed_connector = MultiSiloFeedConnector(guard=self.feed_guard)
        return self.feed_connector

    def _run_feed_cycle(self) -> dict[str, Any]:
        """One immediate dual-vendor fetch/ingest cycle across all 4 silos."""

        connector = self._ensure_feed_connector()
        try:
            report = connector.ingest_cycle()
        except Exception as exc:  # pragma: no cover - feed must never 500
            report = {
                "error": f"{type(exc).__name__}: {exc}",
                "frames": 0,
                "silos": {},
                "all_live": False,
            }
        self._feed_report = dict(report)
        return report

    # -- Phase 3 Step 2: write-only broker credentials ----------------
    def broker_credentials(self) -> dict[str, Any]:
        """Masked per-venue credential inventory (never a raw secret)."""

        try:
            state = broker_credential_state(
                registry=self.registry, vault=self.broker_vault
            )
        except Exception as exc:  # pragma: no cover - vault is optional
            return {
                "write_only": True,
                "available": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "silos": {},
            }
        state["available"] = True
        if self._credential_reports:
            state["last_verification"] = {
                key: {
                    "venue": report.get("venue"),
                    "silo": report.get("silo"),
                    "dry_run_verified": report.get("dry_run_verified"),
                    "api_key_masked": report.get("api_key_masked"),
                    "sandbox": report.get("sandbox"),
                    "stored_at_ns": report.get("stored_at_ns"),
                }
                for key, report in self._credential_reports.items()
            }
        return state

    def store_broker_credentials(
        self, request: BrokerCredentialRequest
    ) -> dict[str, Any]:
        """Encrypt a testnet credential in Ring 0 and return only masked proof.

        Ring 3 never sees the plaintext: it hands the two ``SecretStr`` values to
        Ring 0, which stores them under ``ring0_vault/silos/key_vault`` and
        returns ``***last4`` plus the dry-run handshake receipt (``sec_011``).
        """

        api_key = request.api_key.get_secret_value()
        api_secret = request.api_secret.get_secret_value()
        if not api_key.strip() or not api_secret.strip():
            raise HTTPException(
                status_code=422, detail="api_key and api_secret must be non-empty"
            )
        try:
            report = store_and_verify_broker_credentials(
                request.silo,
                request.venue,
                api_key,
                api_secret,
                sandbox=bool(request.sandbox),
                registry=self.registry,
                vault=self.broker_vault,
            )
        except ValueError as exc:
            # The message names the silo/venue only; it never contains a secret.
            raise HTTPException(status_code=422, detail=str(exc)) from None
        finally:
            # Drop the plaintext references before building the response.
            del api_key
            del api_secret

        report["operator"] = str(request.operator)
        report["zero_restart"] = True
        self._credential_reports[f"{report['silo']}:{report['venue']}"] = dict(report)
        return report

    # -- Phase 3 Step 5: zero-downtime Ring 0 key rotation --------------
    def _broker_vault_or_default(self) -> Any:
        if self.broker_vault is not None:
            return self.broker_vault
        from ring0_vault.silos.key_vault.broker_credentials import default_broker_vault

        return default_broker_vault()

    def rotate_vault_keys(
        self, request: VaultRotationRequest | None = None
    ) -> dict[str, Any]:
        """Rotate the Ring 0 Fernet + Ed25519 keys while the platform runs.

        The reply is a proof receipt: version, fingerprint, re-encrypted counts
        and the OS tuning summary.  It never contains a raw Fernet key, a
        private Ed25519 seed, an API key or a broker secret.
        """

        operator = str(request.operator) if request is not None else "ring3.operator"
        try:
            report = rotate_ring0_vault_keys(
                llm_vault=self.key_vault,
                broker_vault=self._broker_vault_or_default(),
                operator=operator,
            )
        except ValueError as exc:
            # The message names the store/entry only; it never contains a secret.
            raise HTTPException(
                status_code=500, detail=f"vault rotation aborted: {exc}"
            ) from None

        # Masks cannot change (plaintext is preserved) but the live slots are
        # refreshed from the new key so the cockpit never shows a stale token.
        for slot in ("1", "2"):
            token = self.key_vault.masked(f"llm.system{slot}")
            if token is not None:
                self._llm[slot].api_key_masked = token
                self._llm[slot].api_key_set = True
                self._llm[slot].updated_at_ns = int(report["rotated_at_ns"])
        report["zero_restart"] = True
        report["operator_label"] = operator
        return report

    def vault_status(self) -> dict[str, Any]:
        """Secret-free Ring 0 key custody status + OS low-latency summary."""

        try:
            return vault_key_status(
                llm_vault=self.key_vault,
                broker_vault=self._broker_vault_or_default(),
            )
        except Exception as exc:  # noqa: BLE001 - status must render, never crash
            return {
                "schema": "nios.ring0.key_rotation_status.v1",
                "status": "UNAVAILABLE",
                "ring": "ring0_vault",
                "vault": "ring0_vault/silos/key_vault",
                "write_only": True,
                "raw_secrets_exposed": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }

    # -- Phase 3 Step 2: live local LLM probe ------------------------
    def probe_llms(self, request: LLMProbeRequest | None = None) -> dict[str, Any]:
        """Ping System 1 / System 2 under the strict 250 ms hard cap."""

        from core.llm_transport import HARD_TIMEOUT_MS, probe_llm_endpoints

        timeout_ms = (
            float(request.timeout_ms)
            if request is not None and request.timeout_ms is not None
            else float(HARD_TIMEOUT_MS)
        )
        # System 1 must always honour the constitutional 250 ms ceiling.
        cap = min(timeout_ms, float(HARD_TIMEOUT_MS))
        report = probe_llm_endpoints(
            system1=self._llm["1"].to_dict(),
            system2=self._llm["2"].to_dict(),
            hard_timeout_ms=cap,
            fetcher=self._llm_probe_fetcher,
        )
        report["operator"] = str(request.operator) if request is not None else "ring3.operator"
        report["zero_restart"] = True
        report["write_only_credentials"] = True
        self._llm_probe = dict(report)

        # Reflect the measured latency on the cockpit slot so the operator sees
        # the number that actually drove the fallback decision.
        slot1 = self._llm["1"]
        slot1.last_latency_ms = float(report["system1"]["latency_ms"])
        slot1.circuit_open = bool(report["fallback_active"])
        slot1.updated_at_ns = int(report["probed_at_ns"])
        system2 = report.get("system2")
        if system2:
            slot2 = self._llm["2"]
            slot2.last_latency_ms = float(system2["latency_ms"])
            slot2.circuit_open = not bool(system2["reachable"])
            slot2.updated_at_ns = int(report["probed_at_ns"])
        return report

    def state(self) -> dict[str, Any]:
        silos = {
            silo_id: {
                **self.broker_state(silo_id),
                "vaults": self._vaults(silo_id),
            }
            for silo_id in self.registry.silo_ids()
        }
        return {
            "version": PLATFORM_VERSION,
            "document_id": DOCUMENT_ID,
            "system": self.system_status(),
            "abstention": self._abstention(),
            "silos": silos,
            "brokers": self.brokers(),
            "leases": self._leases(),
            "contagion": self._contagion(),
            "llm": self._llm_state(),
            "cards": self.recent_cards(limit=20),
            "kill_switch": self._kill_report,
            "extensions": self._extensions_state(),
            "formal_proofs": self.formal_proofs(),
            "autopilot": self.autopilot_state(),
            "ring_processes": self.ring_processes(),
            # Phase 3 Step 3: supervisor control surface + sandbox receipts.
            "supervisor": self.supervisor_state(),
            "testnet": self._testnet_summary(),
            "feed_mode": self.feed_state()["feed_mode"],
            "feed": self.feed_state(),
            "broker_credentials": self.broker_credentials(),
            "llm_probe": self._llm_probe,
            "streams": {
                "cockpit": STREAM_RING3_COCKPIT,
                "fps": 60,
            },
        }

    def autopilot_state(self) -> dict[str, Any]:
        """Paper-shadow autopilot telemetry (``sec_013`` lifecycle)."""
        provider = self.autopilot_provider
        if callable(provider):
            try:
                return dict(provider())
            except Exception:  # pragma: no cover - telemetry is non-fatal
                return {"enabled": False, "error": "autopilot telemetry unavailable"}
        return {
            "enabled": False,
            "mode": "PAPER",
            "interval_ms": int(AUTOPILOT_INTERVAL_S * 1000),
        }

    # -- cards ------------------------------------------------------------
    def publish_card(self, card: PlainEnglishCockpitCard | Mapping[str, Any]) -> dict[str, Any]:
        payload = card.to_dict() if isinstance(card, PlainEnglishCockpitCard) else dict(card)
        payload.setdefault("proof_line", _proof_line(payload))
        self._cards.append(payload)
        if len(self._cards) > 500:
            del self._cards[: len(self._cards) - 500]
        self._card_sequence += 1
        payload["sequence"] = self._card_sequence
        if self.bus is not None:
            try:
                self.bus.publish_idempotent(
                    STREAM_RING3_COCKPIT,
                    payload,
                    idempotency_key=(
                        "card:"
                        f"{payload.get('silo', '')}:"
                        f"{payload.get('symbol', '')}:"
                        f"{payload.get('timestamp_ns', time.time_ns())}"
                    ),
                )
            except Exception:  # pragma: no cover - telemetry must not break Ring 0
                pass
        return payload

    def recent_cards(self, limit: int = 20) -> list[dict[str, Any]]:
        bus_cards = self._bus_cards(limit=limit)
        if bus_cards:
            return bus_cards[-limit:]
        return list(self._cards[-limit:])

    def _bus_cards(self, limit: int = 50) -> list[dict[str, Any]]:
        if self.bus is None or not hasattr(self.bus, "replay"):
            return []
        # Replay only a bounded tail window: a 400 ms autopilot round appends
        # cards continuously, and the state endpoint must not rescan the whole
        # session on every hydration poll.
        from_sequence = max(1, int(self._bus_cursor) - max(1, int(limit)) * 2)
        try:
            events = self.bus.replay(
                STREAM_RING3_COCKPIT, from_sequence=from_sequence
            )
        except Exception:  # pragma: no cover - optional bus
            return []
        out: list[dict[str, Any]] = []
        for event in list(events)[-max(1, int(limit)):]:
            self._bus_cursor = max(self._bus_cursor, int(event.sequence))
            payload = dict(event.payload)
            payload.setdefault("proof_line", _proof_line(payload))
            out.append(payload)
        return out

    # -- mutations --------------------------------------------------------
    def update_broker(self, request: BrokerControlRequest) -> dict[str, Any]:
        silo = self.registry.get(request.silo)
        state = self._brokers[silo.silo_id]
        updated: list[str] = []

        if request.mode is not None:
            if request.mode == SiloMode.LIVE.value and not request.confirm_live:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "LIVE mode requires confirm_live=true and an explicit "
                        "operator action (sec_011 rule 11)"
                    ),
                )
            if request.mode == SiloMode.LIVE.value and request.confirm_live:
                # Phase 4 Step 2 -- Pre-Flight Go-Live Gate.  A silo may only
                # leave PAPER when the 22-gate certificate is green: an
                # uncertified invariant (dirty tree, broken proof, stale vault)
                # must fail closed *before* capital is exposed.
                certificate = self.preflight_certificate(full_smoke=False)
                failed = int(certificate.get("failed_gates", 0) or 0)
                if failed or not certificate.get("ready_for_live_cutover", False):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "PRE_FLIGHT_GATE_BLOCKED",
                            "message": (
                                "LIVE cutover refused: the 22-gate pre-flight "
                                "certificate does not certify this release"
                            ),
                            "failed_gates": failed,
                            "failed_gate_ids": list(
                                certificate.get("failed_gate_ids") or []
                            ),
                            "warned_gates": int(
                                certificate.get("warned_gates", 0) or 0
                            ),
                            "certificate_sha256": str(
                                certificate.get("certificate_sha256", "")
                            ),
                            "remediation": (
                                "POST /api/v2/control/preflight, resolve every FAIL "
                                "row, then retry the LIVE transition"
                            ),
                        },
                    )
            self.registry.set_mode(
                silo.silo_id,
                request.mode,
                operator=request.operator,
                confirm_live=request.confirm_live,
                now_ns=time.time_ns(),
            )
            updated.append("mode")

        for name in (
            "maker_fee_bps",
            "taker_fee_bps",
            "statutory_tax_bps",
            "spread_bps",
        ):
            value = getattr(request, name)
            if value is not None:
                setattr(state, name, float(value))
                updated.append(name)

        if request.max_daily_loss_micros is not None:
            state.max_daily_loss_micros = int(request.max_daily_loss_micros)
            self.firewall.set_daily_loss_stop(
                silo.silo_id, int(request.max_daily_loss_micros)
            )
            # If the operator lowers the stop below today's realised loss, lock
            # the silo immediately rather than waiting for the next fill.
            if self.firewall.daily_loss_exceeded(silo.silo_id):
                self.firewall.record_realized_pnl(silo.silo_id, 0)
            updated.append("max_daily_loss_micros")

        if request.allocation is not None:
            state.allocation = dict(request.allocation)
            updated.append("allocation")

        state.updated_at_ns = time.time_ns()
        return {
            "updated": updated,
            "silo": silo.silo_id,
            "broker": self.broker_state(silo.silo_id),
            "effective_cost_floor": state.cost_floor(silo.silo_id).to_dict(),
            "zero_restart": True,
        }

    def update_llm(self, request: LLMControlRequest) -> dict[str, Any]:
        slot_key = str(request.system)
        slot = self._llm[slot_key]
        updated: list[str] = []

        for name in ("provider", "model", "base_url"):
            value = getattr(request, name)
            if value is not None:
                setattr(slot, name, str(value))
                updated.append(name)
        if request.timeout_ms is not None:
            slot.timeout_ms = int(request.timeout_ms)
            updated.append("timeout_ms")
        if request.daily_spend_cap_usd is not None:
            slot.daily_spend_cap_usd = float(request.daily_spend_cap_usd)
            updated.append("daily_spend_cap_usd")
            if self.token_ledger is not None and hasattr(
                self.token_ledger, "set_daily_cap"
            ):
                self.token_ledger.set_daily_cap(float(request.daily_spend_cap_usd))
                updated.append("daily_cap_usd")

        if request.api_key is not None:
            raw = request.api_key.get_secret_value()
            if not raw:
                raise HTTPException(status_code=422, detail="api_key must be non-empty")
            metadata = self.key_vault.put(f"llm.system{slot_key}", raw)
            slot.api_key_masked = metadata["masked"]
            slot.api_key_set = True
            updated.append("api_key")
            # ``raw`` / ``metadata`` never enter the response or a log record.
            del raw

        slot.updated_at_ns = time.time_ns()
        self.llm_budget_usd = sum(
            configured.daily_spend_cap_usd for configured in self._llm.values()
        )
        return {
            "updated": updated,
            "system": int(slot_key),
            "config": slot.to_dict(),
            "budget": (
                self.token_ledger.snapshot()
                if self.token_ledger is not None
                else None
            ),
            "write_only_credentials": True,
            "zero_restart": True,
        }

    def record_llm_spend(self, usd: float, *, system: int | str = 1) -> None:
        slot = self._llm[str(system)]
        slot.spend_usd = max(0.0, slot.spend_usd + float(usd))
        if self.token_ledger is not None:
            self.token_ledger.record(
                system=int(str(system)),
                provider=slot.provider,
                model=slot.model,
                prompt_tokens=0,
                completion_tokens=0,
                usd_cost=max(0.0, float(usd)),
            )

    def kill(self, request: KillSwitchRequest) -> dict[str, Any]:
        """Preempt Ring 1 + Ring 0 in ``< 1.0 ms`` and flatten ADR-009.

        The measured window covers the actual emergency actions: latch flip,
        lease disarm, open-entry cancel (which voids their outbox
        reservations), residual reservation void, and reduce-only signing.
        Request parsing and JSON serialisation are outside the budget.
        """
        if not request.confirm:
            raise HTTPException(status_code=400, detail="kill switch requires confirm=true")

        now = time.monotonic_ns()
        # Pre-compute flatten intents before starting the measured window.
        flatten_proposals = self._flatten_proposals(now) if request.flatten else []

        # The emergency window is zero-GC by construction (sec_018 Law 2).
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        started = time.perf_counter_ns()
        if self.kill_latch is not None:
            from core.shm_ipc import HaltReason

            self.kill_latch.halt(HaltReason.MANUAL_KILL_SWITCH, now_ns=now)

        disarmed = 0
        if self.lease_table is not None:
            disarmed = self.lease_table.disarm_all(now_ns=now)

        for silo_id in self.registry.silo_ids():
            self.firewall.set_trading_state(silo_id, TradingState.HALTED)
            if self.guard is not None:
                self.guard.freeze(silo_id, f"GLOBAL_KILL_LATCH: {request.reason}")

        pending_before = list(self.outbox.open_reservations())
        cancelled = self.oms.cancel_all_entries(
            now_ns=now,
            reason="GLOBAL_KILL_SWITCH",
            publish=False,
            extra_outbox_keys=[
                reservation.outbox_idempotency_key for reservation in pending_before
            ],
            persist=False,
        )
        voided = len(pending_before)

        flatten_signed: list[dict[str, Any]] = []
        for proposal in flatten_proposals:
            verdict = self.adr009.evaluate(
                proposal,
                now_ns=now,
                triggered_by="KILL_LATCH",
                emergency_flatten=True,
                cancel_entries=False,
                publish=False,
            )
            flatten_signed.append(verdict.to_dict())

        latency_ns = time.perf_counter_ns() - started
        if gc_was_enabled:
            gc.enable()
        # Durable journal catch-up: the reservations are already VOIDED in
        # memory inside the measured window.  Flush the SQLite/TigerBeetle
        # journal immediately after it, before any operator response.
        durable_flushed = True
        try:
            self.outbox.store.update_outbox_many(
                [
                    reservation.outbox_idempotency_key
                    for reservation in pending_before
                ],
                state="VOIDED",
                updated_ns=now,
                detail=f"GLOBAL_KILL_SWITCH: {request.reason}",
            )
        except Exception:  # pragma: no cover - journal outage is reported
            durable_flushed = False
        # Audit publication is deliberately after the measured window: a cold
        # NATS/file handle must not lengthen the emergency preemption path.
        try:
            self.oms.record_execution(
                ExecutionEvent(
                    event_type="KILL_SWITCH_PREEMPTED",
                    silo="*",
                    symbol="*",
                    order_id=f"kill-{now}",
                    side="FLATTEN",
                    qty=0.0,
                    price=0.0,
                    state="HALTED",
                    reason=request.reason,
                    timestamp_ns=time.time_ns(),
                ),
                publish=True,
            )
        except Exception:  # pragma: no cover - audit is best effort
            pass
        report = {
            "status": "HALTED",
            "reason": request.reason,
            "latency_ns": latency_ns,
            "latency_ms": latency_ns / 1e6,
            "budget_ns": KILL_LATENCY_BUDGET_NS,
            "within_budget": latency_ns <= KILL_LATENCY_BUDGET_NS,
            "kill_latch_hot": bool(self.kill_latch is not None and self.kill_latch.is_halted),
            "disarmed_leases": disarmed,
            "cancelled_orders": cancelled,
            "voided_reservations": voided,
            "flattened_orders": len(flatten_signed),
            "flatten": flatten_signed,
            "durable_journal_flushed": durable_flushed,
            "pending_outbox_after": self.outbox.pending_count,
            "timestamp_ns": time.time_ns(),
        }
        self._kill_report = report
        return report

    def _flatten_proposals(self, now_ns: int) -> list[Any]:
        from core.schemas import UnsignedProposal

        proposals: list[Any] = []
        positions = self.ledger.positions()
        for key, record in positions.items():
            silo, _, symbol = key.partition(":")
            qty_micros = int(record.get("qty_micros", 0))
            if qty_micros == 0:
                continue
            price_micros = max(1, int(record.get("price_micros", 1)))
            side = "SELL" if qty_micros > 0 else "BUY"
            qty = abs(qty_micros) / 1_000_000.0
            price = price_micros / 1_000_000.0
            digest = _sha256_hex(f"kill:{silo}:{symbol}:{now_ns}")
            proposals.append(
                UnsignedProposal(
                    proposal_id=f"adr009-{silo}-{symbol}-{now_ns}",
                    strategy_id="adr009-emergency-flatten",
                    silo=silo,
                    vault=VaultName.SPINAL_SCALPER,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    limit_price=price,
                    expected_gross_bps=0.0,
                    spread_bps=0.0,
                    grounding_hash=digest,
                    is_reduce_only=True,
                    created_at_ns=now_ns,
                )
            )
        return proposals

    def rearm(self, request: RearmRequest | None = None) -> dict[str, Any]:
        """Explicit human reset: clear the kill latch and re-arm every silo.

        ADR-009 requires a *human* to reset a vault stop (``sec_004`` Tier 3), so
        this is the only path back from ``HALTED`` to ``ARMED``.  It clears the
        global latch, unfreezes reconciled/ADR-009 silos and puts every silo back
        into ``ACTIVE`` trading state; the paper autopilot resumes on its next
        tick without a server restart.
        """
        reason = request.reason if request is not None else "operator re-arm"
        operator = request.operator if request is not None else "ring3.operator"
        now_mono = time.monotonic_ns()
        rearmed: list[str] = []
        if self.kill_latch is not None:
            self.kill_latch.arm(now_ns=now_mono)
        for silo_id in self.registry.silo_ids():
            self.firewall.set_trading_state(silo_id, TradingState.ACTIVE)
            if self.guard is not None:
                self.guard.unfreeze(silo_id)
            rearmed.append(silo_id)
        self._kill_report = None
        return {
            "status": "ARMED",
            "reason": str(reason),
            "operator": str(operator),
            "kill_latch_hot": bool(
                self.kill_latch is not None and self.kill_latch.is_halted
            ),
            "rearmed_silos": rearmed,
            "leases_cleared": True,
            "autopilot_resumes": self.autopilot_runner is not None,
            "timestamp_ns": time.time_ns(),
        }

    def autopilot_tick(self) -> dict[str, Any]:
        """Run one paper-shadow round through the orchestrator (sync, cheap)."""
        runner = self.autopilot_runner
        if runner is None:
            return {"status": "DISABLED", "paused": True, "ticks": 0}
        return dict(runner())

    # -- SSE --------------------------------------------------------------
    def frame(self, sequence: int, *, cards: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
        vaults = {
            silo_id: {
                "label": self.registry.get(silo_id).label,
                "mode": str(self.registry.get(silo_id).mode),
                "vaults": self._vaults(silo_id),
            }
            for silo_id in self.registry.silo_ids()
        }
        return {
            "type": "cockpit_frame",
            "sequence": sequence,
            "timestamp_ns": time.time_ns(),
            "fps": 60,
            "system": self.system_status(),
            "abstention": self._abstention(),
            "silos": vaults,
            "cards": list(cards),
        }

    async def stream(
        self,
        *,
        fps: int = 60,
        frames: int | None = None,
        include_history: bool = True,
    ):
        period = 1.0 / max(1, min(int(fps), 120))
        sequence = 0
        cursor = 0
        bus = self.bus
        first = True
        while frames is None or sequence < frames:
            cards: list[dict[str, Any]] = []
            if bus is not None and hasattr(bus, "replay"):
                try:
                    events = bus.replay(STREAM_RING3_COCKPIT, from_sequence=cursor + 1)
                    for event in events:
                        payload = dict(event.payload)
                        payload.setdefault("proof_line", _proof_line(payload))
                        cards.append(payload)
                        cursor = int(event.sequence)
                except Exception:  # pragma: no cover - optional bus
                    cards = []
            if first and include_history and not cards:
                cards = self.recent_cards(limit=20)
            first = False
            payload = self.frame(sequence, cards=cards)
            data = json.dumps(payload, separators=(",", ":"), default=str)
            yield f"event: cockpit\ndata: {data}\n\n"
            sequence += 1
            if frames is not None and sequence >= frames:
                break
            await asyncio.sleep(period)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


def autopilot_enabled(control: ControlPlane) -> bool:
    """``NIOS_AUTOPILOT`` gate: PAPER-only, opt-out via ``NIOS_AUTOPILOT=0``."""
    flag = os.environ.get("NIOS_AUTOPILOT", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if getattr(control, "autopilot_runner", None) is None:
        return False
    try:
        return all(
            str(silo.mode) == SiloMode.PAPER.value for silo in control.registry.silos
        )
    except Exception:  # pragma: no cover - defensive
        return False


def _multiprocess_enabled() -> bool:
    """``NIOS_MULTIPROCESS=1`` opts the FastAPI lifespan into 4-ring spawn."""

    return os.environ.get("NIOS_MULTIPROCESS", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _start_lifespan_supervisor(
    control: ControlPlane, *, timeout_s: float = 5.0
) -> Any | None:
    """Spawn the four ring workers from the ASGI lifespan when opted in.

    Returns the supervisor only when this lifespan started it, so a runtime that
    was handed an already-running supervisor never tears it down on shutdown.
    """

    if not _multiprocess_enabled():
        return None
    try:
        already = bool(control.ring_processes().get("running", False))
        supervisor = control._ensure_supervisor(  # noqa: SLF001 - Ring 3 internal
            SupervisorControlRequest(action="start")
        )
        if already:
            return None
        supervisor.start()
        control._await_heartbeats(timeout_s=float(timeout_s))  # noqa: SLF001
        return supervisor
    except Exception:  # pragma: no cover - a spawn failure must not kill Ring 3
        return None


async def autopilot_paper_loop(
    control: ControlPlane,
    *,
    interval_s: float | None = None,
    max_rounds: int | None = None,
) -> None:
    """Continuous paper-shadow tape for the whole four-silo runtime.

    Every ``250-500 ms`` the orchestrator receives a realistic dual-vendor bar
    and executed L2/trade flow for ``BTC-USDT``, ``RELIANCE.NS``, ``NVDA`` and
    ``EUR_USD``.  The orchestrator itself resolves the 30-fill shadow
    graduation, the 98% conformal gate, the Maker-Peg paper fill and the 35/15
    in-silo ratchet; this task only supplies the clock.

    A hot ``GlobalKillLatch`` pauses the tape immediately, and
    ``POST /api/v2/control/rearm`` resumes it.
    """
    period = float(
        AUTOPILOT_INTERVAL_S if interval_s is None else max(0.25, interval_s)
    )
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        try:
            control.autopilot_tick()
        except Exception:  # pragma: no cover - the tape must never kill Ring 3
            pass
        rounds += 1
        await asyncio.sleep(period)


def create_app(control: ControlPlane, *, static_dir: str | Path | None = None) -> FastAPI:
    """Build the Ring 3 ASGI app around an existing control plane.

    The compiled React 19 bundle is mounted at ``/`` when
    ``ring3_cockpit/ui/openalgo_shell/dist/index.html`` exists; API routes keep
    precedence because the static mount is registered last.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        forge = getattr(control, "forge", None)
        forge_task: asyncio.Task[Any] | None = None
        if forge is not None and hasattr(forge, "start_scheduler"):
            forge_task = forge.start_scheduler()
        autopilot: asyncio.Task[None] | None = None
        if autopilot_enabled(control):
            autopilot = asyncio.create_task(
                autopilot_paper_loop(control),
                name="nios-autopilot-paper",
            )
        _app.state.autopilot_task = autopilot
        # ``sec_004``: with NIOS_MULTIPROCESS=1 the FastAPI lifespan itself owns
        # the four OS worker processes, so a plain ``uvicorn`` launch proves the
        # same PID topology as ``scripts/run_nios.py --multiprocess``.
        supervisor = _start_lifespan_supervisor(control)
        _app.state.process_supervisor = supervisor
        try:
            yield
        finally:
            if supervisor is not None:
                with contextlib.suppress(Exception):
                    supervisor.stop()
            if autopilot is not None:
                autopilot.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await autopilot
            if forge_task is not None:
                forge_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await forge_task
            if forge is not None and hasattr(forge, "stop_scheduler"):
                forge.stop_scheduler()

    app = FastAPI(
        title="NIOS Ring 3 Glass Cockpit",
        version=PLATFORM_VERSION,
        description=(
            f"{DOCUMENT_ID} REST/SSE control plane (sec_004, sec_011, sec_016)."
        ),
        lifespan=_lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.state.control = control

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Never echo request input (API keys are write-only, sec_011)."""
        del request
        detail = [
            {
                "loc": list(error.get("loc", ())),
                "msg": str(error.get("msg", "invalid input")),
                "type": str(error.get("type", "value_error")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.get("/api/v2/cockpit/health")
    def health() -> dict[str, Any]:
        rings = control.ring_processes()
        feed = control.feed_state()
        credentials = control.broker_credentials()
        configured = sum(
            int(entry.get("configured", 0))
            for entry in (credentials.get("silos") or {}).values()
        )
        return {
            "ok": True,
            "status": control.system_status()["status"],
            "version": PLATFORM_VERSION,
            "document_id": DOCUMENT_ID,
            "autopilot": control.autopilot_state().get("enabled", False),
            "feed_mode": feed["feed_mode"],
            "ring_processes": {
                "available": rings.get("available", False),
                "distinct_os_processes": rings.get("distinct_os_processes", 0),
                "pids": rings.get("pids", {}),
                "rings": rings.get("rings", []),
            },
            "broker_credentials": {
                "write_only": True,
                "configured_venues": configured,
            },
            "llm_probe": {
                "status": control._llm_probe.get("status"),
                "hard_timeout_ms": control._llm_probe.get("hard_timeout_ms"),
            },
        }

    @app.get("/api/v2/cockpit/state")
    def state() -> dict[str, Any]:
        return control.state()

    @app.get("/api/v2/cockpit/formal-proofs")
    def formal_proofs(refresh: bool = Query(False)) -> dict[str, Any]:
        return control.formal_proofs(refresh=refresh)

    @app.get("/api/v2/cockpit/stream")
    async def stream(
        request: Request,
        fps: int = Query(60, ge=1, le=120),
        frames: int | None = Query(None, ge=1, le=600),
        include_history: bool = True,
    ) -> StreamingResponse:
        async def body():
            async for chunk in control.stream(
                fps=fps, frames=frames, include_history=include_history
            ):
                if await request.is_disconnected():
                    break
                yield chunk

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v2/control/brokers")
    def control_brokers(request: BrokerControlRequest) -> dict[str, Any]:
        return control.update_broker(request)

    # ---- Phase 3 Step 2: write-only broker testnet credentials ---------
    @app.post("/api/v2/control/brokers/credentials")
    def control_broker_credentials(
        request: BrokerCredentialRequest,
    ) -> dict[str, Any]:
        return control.store_broker_credentials(request)

    @app.get("/api/v2/control/brokers/credentials")
    def control_broker_credentials_state() -> dict[str, Any]:
        return control.broker_credentials()

    # ---- Phase 3 Step 2: feed mode hot-swap ----------------------------
    @app.post("/api/v2/control/feeds")
    def control_feeds(request: FeedControlRequest) -> dict[str, Any]:
        return control.set_feed_mode(request)

    @app.get("/api/v2/control/feeds")
    def control_feeds_state() -> dict[str, Any]:
        return control.feed_state()

    @app.post("/api/v2/control/llms")
    def control_llms(request: LLMControlRequest) -> dict[str, Any]:
        return control.update_llm(request)

    # ---- Phase 3 Step 2: live local Ollama / LLM probe (<250 ms) -------
    @app.post("/api/v2/control/llms/probe")
    def control_llms_probe(
        request: LLMProbeRequest | None = None,
    ) -> dict[str, Any]:
        return control.probe_llms(request)

    @app.post("/api/v2/control/killswitch")
    def control_killswitch(request: KillSwitchRequest) -> dict[str, Any]:
        return control.kill(request)

    # ---- Phase 3 Step 3: 4-ring OS supervisor control ------------------
    @app.get("/api/v2/control/supervisor")
    def control_supervisor_state() -> dict[str, Any]:
        return control.supervisor_state()

    @app.post("/api/v2/control/supervisor")
    def control_supervisor(request: SupervisorControlRequest) -> dict[str, Any]:
        return control.control_supervisor(request)

    # ---- Phase 4 Step 2: closed-loop forge + tail-hedge trigger --------
    @app.post("/api/v2/control/supervisor/forge-hedge")
    def control_supervisor_forge_hedge(
        request: ForgeHedgeRequest | None = None,
    ) -> dict[str, Any]:
        return control.run_forge_and_hedge(request or ForgeHedgeRequest())

    # ---- Phase 4 Step 2: 22-gate pre-flight go-live certificate --------
    @app.get("/api/v2/control/preflight")
    def control_preflight_state(refresh: bool = Query(False)) -> dict[str, Any]:
        return control.preflight_state(refresh=refresh)

    @app.post("/api/v2/control/preflight")
    def control_preflight(
        request: PreflightRequest | None = None,
    ) -> dict[str, Any]:
        payload = request or PreflightRequest()
        return control.preflight_certificate(
            full_smoke=payload.full_smoke,
            silo=payload.silo,
            refresh=payload.refresh,
        )

    # ---- Phase 3 Step 3: Ring 0 sandbox order dispatch -----------------
    @app.get("/api/v2/control/brokers/testnet")
    def control_testnet_state() -> dict[str, Any]:
        return control.testnet_state()

    @app.post("/api/v2/control/brokers/testnet-order")
    def control_testnet_order(request: TestnetOrderRequest) -> dict[str, Any]:
        return control.dispatch_testnet_order(request)

    @app.post("/api/v2/control/rearm")
    def control_rearm(request: RearmRequest | None = None) -> dict[str, Any]:
        return control.rearm(request)

    # ---- Phase 3 Step 5: zero-downtime Ring 0 vault key rotation --------
    @app.post("/api/v2/control/vault/rotate-keys")
    def control_vault_rotate(
        request: VaultRotationRequest | None = None,
    ) -> dict[str, Any]:
        return control.rotate_vault_keys(request)

    @app.get("/api/v2/control/vault/status")
    def control_vault_status() -> dict[str, Any]:
        return control.vault_status()

    @app.get("/api/v2/control/extensions")
    def control_extensions() -> dict[str, Any]:
        return control.extensions_state()

    @app.post("/api/v2/control/extensions")
    def control_extensions_update(
        request: ExtensionsControlRequest,
    ) -> dict[str, Any]:
        return control.apply_extension(request)

    # ---- Phase 3 Step 4: Prometheus / OpenTelemetry metrics exporter -----
    # Registered before the SPA mount at "/" so /metrics is never shadowed.
    from ring3_cockpit.gateway.metrics_exporter import register_metrics_routes

    register_metrics_routes(app, control)

    # ---- Step 8: read-only MCP bridge (5 tools, no order/key surface) ----
    if getattr(control, "mcp", None) is not None:
        control.mcp.register_routes(app)

    # ---- Step 8: unified production server serves the React bundle ------
    bundle = (
        Path(static_dir)
        if static_dir is not None
        else Path(__file__).resolve().parents[1] / "ui" / "openalgo_shell" / "dist"
    )
    if (bundle / "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount(
            "/",
            StaticFiles(directory=str(bundle), html=True),
            name="cockpit-ui",
        )

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proof_line(payload: Mapping[str, Any]) -> str:
    try:
        return PlainEnglishCockpitCard(
            silo=str(payload.get("silo", "")),
            symbol=str(payload.get("symbol", "")),
            mode_badge=str(payload.get("mode_badge", "LIVE CAPITAL")),
            story_sentence=str(payload.get("story_sentence", "")),
            gross_bps=float(payload.get("gross_bps", 0.0)),
            fee_stt_bps=float(payload.get("fee_stt_bps", 0.0)),
            uncert_q98_bps=float(payload.get("uncert_q98_bps", 0.0)),
            net_lb_bps=float(payload.get("net_lb_bps", 0.0)),
            timestamp_ns=int(payload.get("timestamp_ns", 0)),
        ).format_proof_line()
    except Exception:  # pragma: no cover - malformed telemetry
        return ""


def _sha256_hex(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
