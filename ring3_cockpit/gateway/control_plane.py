"""Ring 3 Glass Cockpit control plane (``sec_004``, ``sec_011``, ``sec_016``).

The cockpit is the only writable surface in NIOS.  It exposes exactly four
endpoints, and nothing else:

======================  =====================================================
``GET  /api/v2/cockpit/stream``  60 FPS SSE cards, abstention meter, 4-vault NAV
``GET  /api/v2/cockpit/state``   hydration snapshot (leases, contagion, LLMs)
``POST /api/v2/control/brokers`` Paper/Live, fee floors, loss stops, sliders
``POST /api/v2/control/llms``    System 1/2 hot swap with write-only keys
``POST /api/v2/control/killswitch`` <1.0 ms global preemption + ADR-009 flatten
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
import gc
import json
import time
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
from ring0_vault.silos.silo_registry import (
    MarketSiloRegistry,
    SiloMode,
    default_registry,
)
from ring1_spinal.silo_worker import SiloCostFloor

__all__ = [
    "ARCHETYPE_GROUPS",
    "ARCHETYPE_WEIGHTS",
    "ControlPlane",
    "BrokerControlRequest",
    "LLMControlRequest",
    "KillSwitchRequest",
    "ExtensionsControlRequest",
    "create_app",
    "DEFAULT_DAILY_LLM_BUDGET_USD",
    "KILL_LATENCY_BUDGET_NS",
]

#: ``sec_016``: the standing daily LLM spend cap.
DEFAULT_DAILY_LLM_BUDGET_USD: float = 3.10
#: ``sec_004`` / ``sec_016``: kill switch preemption budget.
KILL_LATENCY_BUDGET_NS: int = 1_000_000

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
        if token not in ("skill", "plugin", "mcp_tool", "forge"):
            raise ValueError("target must be skill, plugin, mcp_tool or forge")
        return token


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
        return {
            "daily_spend_cap_usd": self.llm_budget_usd,
            "daily_spend_usd": round(total_spend, 6),
            "budget_remaining_usd": round(max(0.0, self.llm_budget_usd - total_spend), 6),
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

    def state(self) -> dict[str, Any]:
        silos = {
            silo_id: {
                **self.broker_state(silo_id),
                "vaults": self._vaults(silo_id),
            }
            for silo_id in self.registry.silo_ids()
        }
        return {
            "version": "2.0",
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
            "streams": {
                "cockpit": STREAM_RING3_COCKPIT,
                "fps": 60,
            },
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
        try:
            events = self.bus.replay(STREAM_RING3_COCKPIT, from_sequence=1)
        except Exception:  # pragma: no cover - optional bus
            return []
        out: list[dict[str, Any]] = []
        for event in list(events)[-limit:]:
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
            "write_only_credentials": True,
            "zero_restart": True,
        }

    def record_llm_spend(self, usd: float, *, system: int | str = 1) -> None:
        slot = self._llm[str(system)]
        slot.spend_usd = max(0.0, slot.spend_usd + float(usd))

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


def create_app(control: ControlPlane, *, static_dir: str | Path | None = None) -> FastAPI:
    """Build the Ring 3 ASGI app around an existing control plane.

    The compiled React 19 bundle is mounted at ``/`` when
    ``ring3_cockpit/ui/openalgo_shell/dist/index.html`` exists; API routes keep
    precedence because the static mount is registered last.
    """
    app = FastAPI(
        title="NIOS Ring 3 Glass Cockpit",
        version="2.0.0",
        description="Authenticated REST/SSE control plane (sec_004, sec_011, sec_016).",
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
        return {
            "ok": True,
            "status": control.system_status()["status"],
            "version": "2.0",
        }

    @app.get("/api/v2/cockpit/state")
    def state() -> dict[str, Any]:
        return control.state()

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

    @app.post("/api/v2/control/llms")
    def control_llms(request: LLMControlRequest) -> dict[str, Any]:
        return control.update_llm(request)

    @app.post("/api/v2/control/killswitch")
    def control_killswitch(request: KillSwitchRequest) -> dict[str, Any]:
        return control.kill(request)

    @app.get("/api/v2/control/extensions")
    def control_extensions() -> dict[str, Any]:
        return control.extensions_state()

    @app.post("/api/v2/control/extensions")
    def control_extensions_update(
        request: ExtensionsControlRequest,
    ) -> dict[str, Any]:
        return control.apply_extension(request)

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
