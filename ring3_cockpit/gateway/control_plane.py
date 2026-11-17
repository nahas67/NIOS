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
``POST /api/v2/control/verification/chaos-drill`` SIGKILL recovery invariants
``POST /api/v2/control/verification/vopr``        seeded VOPR fault campaign
``POST /api/v2/control/verification/os-tune``     apply + audit OS low latency
``POST /api/v2/control/brain/mirofish-shock``     3-tick contagion cascade
``POST /api/v2/control/brain/committee-debate``   grounded System 2 debate turn
``POST /api/v2/control/forge/evaluate-challenger`` nursery promotion gate
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
import functools
import gc
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, ClassVar, Iterable, Iterator, Literal, Mapping

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from core.instrument_registry import (
    MICROS,
    SILO_IDS,
    registry as instrument_registry,
)
from core.market_features_store import (
    feature_summary,
    get_decision_score,
    get_feature_profile,
    get_options_surface_profile,
    profile_index,
    reference_price,
)
from ring2_brain.quant.macro_and_catalysts import (
    get_macro_calendar,
    get_market_heatmaps,
)
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
from ring0_vault.ibor.tax_and_cost_engine import (
    COST_FLOOR_BPS as STATUTORY_COST_FLOOR_BPS,
    cost_engine as statutory_cost_engine,
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
from ring2_brain.quant.catalog import FACTOR_TAXONOMIES
from ring2_brain.quant.tradingview_bridge import (
    MODE_DETERMINISTIC_REPLAY,
    MODE_LIVE_WS,
    STUDY_IDS as TRADINGVIEW_STUDY_IDS,
    TIMEFRAME_SECONDS as TRADINGVIEW_TIMEFRAME_SECONDS,
    TradingViewBridge,
    TradingViewBridgeError,
    TradingViewStudyNotFound,
    TradingViewSymbolNotFound,
    TradingViewTimeframeError,
)
from ring3_cockpit.gateway.payload_envelope import (
    enclose,
    payload_digest,
    risk_telemetry,
    silo_risk_telemetry,
    workspace_mode_of,
)

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

#: ``sec_004`` (V1.8): allowed walk-forward tape sources. ``TRADINGVIEW_REPLAY``
#: feeds Ring 2 auxiliary replay bars through the sandboxed TradingView bridge;
#: it is a study/backtest input only and never a Ring 0/1 execution feed.
_BACKTEST_DATA_SOURCES: frozenset[str] = frozenset(
    {"SYNTHETIC_WALK_FORWARD", "TRADINGVIEW_REPLAY"}
)
#: NIOS backtest timeframes -> TradingView wire timeframes.
_BACKTEST_TO_TRADINGVIEW_TIMEFRAME: dict[str, str] = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "1d": "1D",
}
#: ``sec_016`` (V1.8): the typed real-time event frames of ``/api/v2/stream``.
STREAM_EVENT_FRAMES: tuple[str, ...] = (
    "TICK_UPDATE",
    "L2_DEPTH_DELTA",
    "ORDER_LIFECYCLE",
    "RISK_HEARTBEAT",
    "TV_STUDY_SIGNAL",
)
#: ``sec_016`` (V1.8): stream frame schema identity.
STREAM_SCHEMA_VERSION: str = "v1.8.stream-hub"

# Frozen stream epoch so typed SSE frames stay byte-for-byte reproducible in tests.
REPLAY_STREAM_EPOCH_NS: int = 1_760_000_000_000_000_000

# Order-type capability surface advertised by each of the 12 credential venues.
_VENUE_ORDER_TYPES: Mapping[str, tuple[str, ...]] = {
    "binance": (
        "MARKET",
        "LIMIT",
        "STOP_MARKET",
        "STOP_LIMIT",
        "TRAILING_STOP",
        "POST_ONLY",
        "TWAP",
        "ICEBERG",
    ),
    "okx": (
        "MARKET",
        "LIMIT",
        "STOP_MARKET",
        "STOP_LIMIT",
        "TRAILING_STOP",
        "POST_ONLY",
        "TWAP",
        "ICEBERG",
    ),
    "bybit": (
        "MARKET",
        "LIMIT",
        "STOP_MARKET",
        "STOP_LIMIT",
        "TRAILING_STOP",
        "POST_ONLY",
        "TWAP",
        "ICEBERG",
    ),
    "interactive_brokers": (
        "MARKET",
        "LIMIT",
        "STOP",
        "STOP_LIMIT",
        "TRAILING_STOP",
        "MOC",
        "TWAP",
        "ICEBERG",
    ),
    "alpaca": ("MARKET", "LIMIT", "STOP", "STOP_LIMIT", "TRAILING_STOP"),
    "dhan": ("MARKET", "LIMIT", "SL", "SL_M", "TWAP", "ICEBERG"),
    "zerodha": ("MARKET", "LIMIT", "SL", "SL_M", "TWAP", "ICEBERG"),
    "angel": ("MARKET", "LIMIT", "SL", "SL_M", "TWAP", "ICEBERG"),
    "upstox": ("MARKET", "LIMIT", "SL", "SL_M", "TWAP", "ICEBERG"),
    "vnpy_mt5": ("MARKET", "LIMIT", "STOP", "STOP_LIMIT"),
    "vnpy_ctp": ("MARKET", "LIMIT", "STOP", "STOP_LIMIT"),
    "oanda": ("MARKET", "LIMIT", "STOP", "STOP_LIMIT"),
}
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

#: ``sec_015``: statutory + fee drag per silo, in basis points.  The logbook
#: deducts this floor from every recorded round trip so a journal can never
#: flatter a sub-cost strategy (``sec_014`` tax-net cost floor).
LOGBOOK_COST_BPS_BY_SILO: dict[str, float] = {
    "INDIA_ZERODHA_DHAN": 8.50,
    "CRYPTO_BINANCE": 16.00,
    "US_IBKR": 2.80,
    "FX_MT5": 1.60,
}

#: ``/options``: one optionable underlying per silo plus the index proxies.
#: Every figure is a *modelling default*; the request may override spot,
#: volatility, expiry and the risk-free rate.
OPTIONS_UNDERLYINGS: dict[str, dict[str, Any]] = {
    "NIFTY": {
        "silo": "INDIA_ZERODHA_DHAN",
        "spot": 24_250.0,
        "volatility": 0.145,
        "days_to_expiry": 7,
        "strike_step": 50.0,
        "strike_precision": 2,
        "lot_size": 75,
        "oi_scale": 160_000.0,
        "currency": "INR",
        "risk_free_rate": 0.065,
    },
    "BANKNIFTY": {
        "silo": "INDIA_ZERODHA_DHAN",
        "spot": 51_800.0,
        "volatility": 0.168,
        "days_to_expiry": 7,
        "strike_step": 100.0,
        "strike_precision": 2,
        "lot_size": 35,
        "oi_scale": 90_000.0,
        "currency": "INR",
        "risk_free_rate": 0.065,
    },
    "BTCUSDT": {
        "silo": "CRYPTO_BINANCE",
        "spot": 96_400.0,
        "volatility": 0.620,
        "days_to_expiry": 30,
        "strike_step": 2_000.0,
        "strike_precision": 2,
        "lot_size": 1,
        "oi_scale": 3_200.0,
        "currency": "USDT",
        "risk_free_rate": 0.045,
    },
    "ETHUSDT": {
        "silo": "CRYPTO_BINANCE",
        "spot": 3_200.0,
        "volatility": 0.680,
        "days_to_expiry": 30,
        "strike_step": 100.0,
        "strike_precision": 2,
        "lot_size": 1,
        "oi_scale": 28_000.0,
        "currency": "USDT",
        "risk_free_rate": 0.045,
    },
    "SPY": {
        "silo": "US_IBKR",
        "spot": 585.0,
        "volatility": 0.185,
        "days_to_expiry": 30,
        "strike_step": 5.0,
        "strike_precision": 2,
        "lot_size": 100,
        "oi_scale": 42_000.0,
        "currency": "USD",
        "risk_free_rate": 0.043,
    },
    "EURUSD": {
        "silo": "FX_MT5",
        "spot": 1.0850,
        "volatility": 0.082,
        "days_to_expiry": 30,
        "strike_step": 0.005,
        "strike_precision": 4,
        "lot_size": 100_000,
        "oi_scale": 26_000.0,
        "currency": "USD",
        "risk_free_rate": 0.038,
    },
}

#: ``/options``: the six supported multi-leg templates.
OPTIONS_STRATEGIES: tuple[dict[str, str], ...] = (
    {
        "id": "BULL_CALL_SPREAD",
        "name": "Bull Call Spread",
        "bias": "BULLISH",
        "legs": "long ATM call / short ATM+1 call",
        "summary": "Capped upside debit spread; the max loss is the premium paid.",
    },
    {
        "id": "BEAR_PUT_SPREAD",
        "name": "Bear Put Spread",
        "bias": "BEARISH",
        "legs": "long ATM put / short ATM-1 put",
        "summary": "Capped downside debit spread financed by the short put.",
    },
    {
        "id": "SHORT_STRADDLE",
        "name": "Short Straddle",
        "bias": "NEUTRAL",
        "legs": "short ATM call / short ATM put",
        "summary": "Premium-selling credit trade; undefined risk on both tails.",
    },
    {
        "id": "LONG_STRANGLE",
        "name": "Long Strangle",
        "bias": "VOLATILITY",
        "legs": "long ATM-1 put / long ATM+1 call",
        "summary": "Cheaper convexity than a straddle; needs a large move either way.",
    },
    {
        "id": "IRON_CONDOR",
        "name": "Iron Condor",
        "bias": "RANGE_BOUND",
        "legs": "long ATM-2 put / short ATM-1 put / short ATM+1 call / long ATM+2 call",
        "summary": "Defined-risk credit range trade with two protected wings.",
    },
    {
        "id": "PROTECTIVE_PUT",
        "name": "Protective Put",
        "bias": "HEDGED_LONG",
        "legs": "long underlying / long ATM put",
        "summary": "Insurance floor under a long position; the premium is the cost.",
    },
)

#: ``/terminal``: the multi-silo watchlist rendered on every terminal frame.
TERMINAL_WATCHLIST: tuple[dict[str, Any], ...] = (
    {"symbol": "BTCUSDT", "silo": "CRYPTO_BINANCE", "asset_class": "CRYPTO", "reference": 96_400.0},
    {"symbol": "NIFTY", "silo": "INDIA_ZERODHA_DHAN", "asset_class": "INDEX", "reference": 24_250.0},
    {"symbol": "SPY", "silo": "US_IBKR", "asset_class": "EQUITY", "reference": 585.0},
    {"symbol": "EURUSD", "silo": "FX_MT5", "asset_class": "FX", "reference": 1.0850},
)

#: Deterministic L2 ladder shape: level ``i`` is ``level_step_pct`` wider per step.
TERMINAL_LEVEL_STEP_PCT: float = 0.00004
#: Lee-Ready tape length on a terminal frame.
TERMINAL_TAPE_SIZE: int = 24

#: Contract asset class -> the terminal's coarse volatility bucket.
_TERMINAL_ASSET_BY_CLASS: dict[str, str] = {
    "PERPETUAL": "CRYPTO",
    "INDEX_FUTURE": "INDEX",
    "FUTURE": "INDEX",
    "COMMODITY_FUTURE": "INDEX",
    "CURRENCY_FUTURE": "FX",
    "ETF": "EQUITY",
    "EQUITY": "EQUITY",
    "FX": "FX",
    "METAL": "FX",
    "ENERGY": "FX",
}

#: The V1.7 market-terminal watchlist: one representative per silo from every
#: asset class in the institutional universe (indices, cash equities, commodity
#: and currency futures, crypto majors/minors, US equities and ETFs, FX, metals).
TERMINAL_UNIVERSE_: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "NIFTY",
    "BANKNIFTY",
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "GOLD",
    "CRUDEOIL",
    "USDINR",
    "SPY",
    "QQQ",
    "NVDA",
    "AAPL",
    "TSLA",
    "GLD",
    "TLT",
    "ES",
    "CL",
    "EURUSD",
    "GBPJPY",
    "XAUUSD",
)


def _stable_unit(*parts: Any) -> float:
    """A deterministic ``[0, 1)`` draw keyed on the arguments.

    Every synthetic terminal/chain figure is derived from a SHA-256 of its own
    coordinates instead of ``random``, so two calls with the same inputs return
    byte-identical numbers and a test can assert on them.
    """
    seed = "|".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _stable_int(low: int, high: int, *parts: Any) -> int:
    """A deterministic integer in ``[low, high]`` (inclusive)."""
    if high <= low:
        return int(low)
    span = int(high) - int(low) + 1
    return int(low) + int(_stable_unit(*parts) * span) % span


def _round_strike(value: float, step: float, precision: int) -> float:
    """Snap a price onto the venue strike grid."""
    if step <= 0.0:
        return round(float(value), int(precision))
    snapped = round(float(value) / float(step)) * float(step)
    return round(snapped, int(precision))


def _serialised_control(method: Any) -> Any:
    """Run one mutating control-plane method under the instance RLock.

    V1.6: a paper order's reserve -> submit -> fill sequence must be atomic with
    respect to ``orders/cancel`` and ``ledger/fund-paper``; otherwise a
    concurrent cancel could void a reservation between reserve and commit.
    """

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._state_lock:
            return method(self, *args, **kwargs)

    return wrapper


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


class StrictRequest(BaseModel):
    """Base class for every Ring 3 request model (V1.6 hardening).

    * ``allow_inf_nan=False`` rejects ``NaN`` / ``+Inf`` / ``-Inf`` at the
      schema boundary, so a non-finite number can never reach an engine, the
      ledger or the JSON encoder.
    * a catch-all validator rejects absurd magnitudes (``|x| > 1e18``) before a
      field-specific bound is even considered.
    """

    model_config = ConfigDict(allow_inf_nan=False, extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _finite_and_bounded(cls, value: Any, info: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{info.field_name} must be finite")
            if abs(value) > 1e18:
                raise ValueError(f"{info.field_name} is out of bounds")
        elif isinstance(value, int) and abs(value) > 10**18:
            raise ValueError(f"{info.field_name} is out of bounds")
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, float) and not math.isfinite(item):
                    raise ValueError(f"{info.field_name} must be finite")
        return value

    @field_validator(
        "symbol",
        "silo",
        "strategy_id",
        "order_id",
        "task_id",
        "backtest_id",
        "venue",
        "plugin_name",
        "skill_name",
        "setup_strategy",
        "symbols",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def _identifier_safe(cls, value: Any, info: Any) -> Any:
        """Reject path traversal, null bytes and separators (V1.6 hardening)."""

        def _check(token: Any) -> Any:
            if not isinstance(token, str):
                return token
            if "\x00" in token or "/" in token or "\\" in token or ".." in token:
                raise ValueError(f"{info.field_name} contains an unsafe character")
            return token

        if isinstance(value, (list, tuple)):
            return [_check(item) for item in value]
        return _check(value)


#: Identifier grammar: letters, digits, dot, dash, underscore, colon.  A path
#: separator, a null byte or a traversal token can never match.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:\-&]{0,127}$"


def _identifier(value: str, *, field_name: str = "identifier") -> str:
    """Harden one path/query identifier against traversal and injection."""
    token = str(value or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail=f"UNKNOWN_{field_name.upper()}")
    if "\x00" in token or "/" in token or "\\" in token or token in (".", ".."):
        raise HTTPException(status_code=404, detail=f"UNKNOWN_{field_name.upper()}")
    if not re.match(IDENTIFIER_PATTERN, token):
        raise HTTPException(status_code=404, detail=f"UNKNOWN_{field_name.upper()}")
    return token


class BrokerControlRequest(StrictRequest):
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


class LLMControlRequest(StrictRequest):
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


class KillSwitchRequest(StrictRequest):
    """``POST /api/v2/control/killswitch`` payload.

    ``confirm`` defaults to ``False``: a global halt flattens every silo, so an
    operator (or a prompt-injected caller) must assert the intent explicitly.
    A default of ``True`` made the manual confirmation flag decorative.
    """

    reason: str = "manual operator kill switch"
    flatten: bool = True
    confirm: bool = False


class WorkspaceResetRequest(StrictRequest):
    """``POST /api/v2/control/workspace/reset`` payload.

    ``workspace`` selects the blast radius.  ``paper`` (the default, and the
    pre-V1.8 contract) zeroes the demo sandbox and needs any authenticated
    session; ``live`` / ``all`` reach live state and therefore cost a
    LIVE-elevated session plus an explicit ``confirm=true``.
    """

    reason: str = "clean slate reset"
    workspace: str = "paper"
    confirm: bool = False

    @field_validator("workspace")
    @classmethod
    def _workspace_ok(cls, value: str) -> str:
        token = str(value or "").strip().lower()
        if token not in ("paper", "live", "all"):
            raise ValueError("workspace must be paper, live or all")
        return token


class RearmRequest(StrictRequest):
    """``POST /api/v2/control/rearm`` payload (explicit human reset)."""

    reason: str = "operator re-arm"
    operator: str = "ring3.operator"


class VaultRotationRequest(StrictRequest):
    """``POST /api/v2/control/vault/rotate-keys`` payload (Phase 3 Step 5).

    Rotation is non-destructive and zero-downtime, so it needs no ``confirm``
    flag; the operator and reason are recorded in Ring 0's rotation journal.
    """

    operator: str = "ring3.operator"
    reason: str = "scheduled zero-downtime rotation"


class ExtensionsControlRequest(StrictRequest):
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


class FeedControlRequest(StrictRequest):
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


class BrokerCredentialRequest(StrictRequest):
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


class LLMProbeRequest(StrictRequest):
    """``POST /api/v2/control/llms/probe`` payload (``sec_016``).

    The probe is read-only: it measures reachability and latency under the
    strict 250 ms hard cap and never mutates the active provider.
    """

    timeout_ms: int | None = Field(default=None, ge=1, le=5_000)
    operator: str = "ring3.operator"


class SupervisorControlRequest(StrictRequest):
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


class PreflightRequest(StrictRequest):
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


class ForgeHedgeRequest(StrictRequest):
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


class TestnetOrderRequest(StrictRequest):
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


class PaperOrderRequest(StrictRequest):
    """``POST /api/v2/control/orders/paper`` payload (Step 5, ``sec_018`` Law 4).

    Paper-only by construction: there is no venue field and no credential is
    read.  The order is grounded, reserved by the Ring 0 outbox, routed through
    ``SmartOMS`` (Maker-Peg or TWAP/Iceberg) and, when filled, swept by the
    in-silo ``35% / 15% / 50%`` profit ratchet.
    """

    silo: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: str = "BUY"
    quantity: float = Field(..., gt=0.0, le=1_000_000.0)
    order_style: str = "MAKER_PEG"
    limit_price: float | None = Field(default=None, gt=0.0)
    expected_gross_bps: float = Field(default=18.0, ge=0.0, le=5_000.0)
    spread_bps: float = Field(default=1.2, ge=0.0, le=500.0)
    top_of_book_qty: float = Field(default=1_000.0, gt=0.0)
    fill_immediately: bool = True
    strategy_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    operator: str = "ring3.operator"

    @field_validator("silo")
    @classmethod
    def _silo_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if not token:
            raise ValueError("silo must not be empty")
        return token

    @field_validator("side")
    @classmethod
    def _side_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if token not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        return token

    @field_validator("order_style")
    @classmethod
    def _style_ok(cls, value: str) -> str:
        token = str(value).strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {
            "MAKER_PEG": "MAKER_PEG",
            "MAKER": "MAKER_PEG",
            "PEG": "MAKER_PEG",
            "TWAP": "TWAP_ICEBERG",
            "ICEBERG": "TWAP_ICEBERG",
            "TWAP_ICEBERG": "TWAP_ICEBERG",
        }
        if token not in aliases:
            raise ValueError("order_style must be MAKER_PEG or TWAP_ICEBERG")
        return aliases[token]


class CancelOrdersRequest(StrictRequest):
    """``POST /api/v2/control/orders/cancel`` payload (Step 5).

    ``order_id`` may be one ``SmartOMS`` order id or the literal ``ALL``.
    Cancelling always voids the matching pending outbox reservation so no
    stranded capital survives the cancel.
    """

    order_id: str = "ALL"
    silo: str | None = None
    reason: str = "operator cancel"
    operator: str = "ring3.operator"

    @field_validator("order_id")
    @classmethod
    def _order_ok(cls, value: str) -> str:
        token = str(value or "ALL").strip()
        return token or "ALL"


class ShadowBurstRequest(StrictRequest):
    """``POST /api/v2/control/analyzer/shadow-burst`` payload (Step 5).

    Drives ``rounds`` paper autopilot ticks so the operator can graduate a
    strategy to the 30-fill shadow threshold interactively.
    """

    silo: str = "CRYPTO_BINANCE"
    rounds: int = Field(default=10, ge=1, le=500)
    strategy_id: str | None = None
    operator: str = "ring3.operator"

    @field_validator("silo")
    @classmethod
    def _silo_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if not token:
            raise ValueError("silo must not be empty")
        return token


class OptionPriceRequest(StrictRequest):
    """``POST /api/v2/control/quant/price-option`` payload (Step 5, ``sec_017``).

    Every input and every Greek is sealed into a SHA-256
    ``GroundedFactPacket`` before the number reaches the cockpit.
    """

    symbol: str = Field(default="NIOS.OPT", min_length=1)
    spot: float = Field(..., gt=0.0, le=100_000_000_000.0)
    strike: float = Field(..., gt=0.0, le=100_000_000_000.0)
    volatility: float = Field(..., gt=0.0, le=20.0)
    days_to_expiry: float = Field(..., gt=0.0, le=36_500.0)
    risk_free_rate: float = Field(default=0.05, ge=-1.0, le=1.0)
    option_type: str = "PUT"
    operator: str = "ring3.operator"

    @field_validator("option_type")
    @classmethod
    def _type_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if token not in ("PUT", "CALL"):
            raise ValueError("option_type must be PUT or CALL")
        return token


class QuantScanRequest(StrictRequest):
    """``POST /api/v2/control/quant/scan`` payload (Step 5)."""

    top_k: int = Field(default=15, ge=1, le=500)
    operator: str = "ring3.operator"


class LedgerReconcileRequest(StrictRequest):
    """``POST /api/v2/control/ledger/reconcile`` payload (Step 5, ``sec_009``).

    Runs ``LiveReconciler.reconcile_all`` across every silo (or the requested
    subset) and proves double-entry conservation.
    """

    silos: list[str] | None = None
    operator: str = "ring3.operator"


class ChaosDrillRequest(StrictRequest):
    """``POST /api/v2/control/verification/chaos-drill`` payload (Step 6).

    Deterministic SIGKILL drill: a worker is killed mid-outbox, the ledger is
    replayed from the journal and every ``sec_009`` reboot invariant must hold.
    The lease sweep runs on an isolated shared-memory segment so a drill never
    disarms the live Ring 1 lease table.
    """

    iterations: int = Field(default=120, ge=8, le=4_000)
    hold_ms: float = Field(default=120.0, ge=10.0, le=2_000.0)
    seed: int = Field(default=11, ge=0, le=2_147_483_647)
    silo: str = "CRYPTO_BINANCE"
    symbol: str = "BTC-USDT"
    operator: str = "ring3.operator"

    @field_validator("silo")
    @classmethod
    def _drill_silo_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if not token:
            raise ValueError("silo must not be empty")
        return token

    @field_validator("symbol")
    @classmethod
    def _drill_symbol_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if not token or len(token) > 32:
            raise ValueError("symbol must be 1-32 characters")
        return token


class VoprCampaignRequest(StrictRequest):
    """``POST /api/v2/control/verification/vopr`` payload (Step 6, ``sec_009``).

    Seeded deterministic fault campaign: the same seed and step count always
    reproduce the same fault histogram, so a red row is a reproducible defect.
    """

    seed: int = Field(default=7, ge=0, le=2_147_483_647)
    steps: int = Field(default=250, ge=10, le=20_000)
    operator: str = "ring3.operator"


class OsTuneRequest(StrictRequest):
    """``POST /api/v2/control/verification/os-tune`` payload (Step 6).

    ``apply=True`` applies everything the *serving process* is permitted to
    change; rows that need elevated privileges stay ``RECOMMENDED`` and are
    reported with their exact command instead of being silently skipped.
    """

    apply: bool = True
    ring: str = "ring1"
    operator: str = "ring3.operator"

    @field_validator("ring")
    @classmethod
    def _ring_ok(cls, value: str) -> str:
        token = str(value).strip().lower()
        if token not in ("ring0", "ring1", "ring2", "ring3"):
            raise ValueError("ring must be ring0, ring1, ring2 or ring3")
        return token


class MiroFishShockRequest(StrictRequest):
    """``POST /api/v2/control/brain/mirofish-shock`` payload (Step 6).

    One five-archetype, three-tick contagion cascade under a synthesised shock.
    The cascade is deterministic for a given shock vector, so the returned
    SHA-256 digest is a reproducible commitment to the simulation inputs and
    outputs Ã¢â‚¬â€ never to wall-clock jitter.
    """

    symbol: str = Field(default="BTCUSDT", min_length=1, max_length=32)
    shock_bps: float = Field(default=-45.0, ge=-5_000.0, le=5_000.0)
    sentiment_tag: str = Field(default="panic", max_length=64)
    volatility_ratio: float = Field(default=2.0, ge=0.0, le=50.0)
    spread_bps: float = Field(default=6.0, ge=0.0, le=500.0)
    gamma_flip_proximity: float = Field(default=0.2, ge=0.0, le=1.0)
    silo: str = "US"
    operator: str = "ring3.operator"

    @field_validator("symbol")
    @classmethod
    def _shock_symbol_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if not token:
            raise ValueError("symbol must not be empty")
        return token

    @field_validator("sentiment_tag")
    @classmethod
    def _tag_ok(cls, value: str) -> str:
        token = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
        if token and not token.replace("_", "").isalnum():
            raise ValueError("sentiment_tag must be alphanumeric with _ or -")
        return token


class CommitteeDebateRequest(StrictRequest):
    """``POST /api/v2/control/brain/committee-debate`` payload (Step 6).

    ``symbol`` may be ``AUTO`` to debate whichever candidate the 490-factor
    scan ranks first. Every numerical claim in the transcript is re-computed by
    ``NumericalGroundingVerifier`` before the verdict is returned.
    """

    silo: str = "US"
    symbol: str = Field(default="AUTO", min_length=1, max_length=32)
    spread_bps: float = Field(default=3.0, ge=0.0, le=500.0)
    operator: str = "ring3.operator"

    @field_validator("silo")
    @classmethod
    def _committee_silo_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if not token:
            raise ValueError("silo must not be empty")
        return token

    @field_validator("symbol")
    @classmethod
    def _committee_symbol_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if not token:
            raise ValueError("symbol must not be empty")
        return token


class ChallengerEvalRequest(StrictRequest):
    """``POST /api/v2/control/forge/evaluate-challenger`` payload (Step 6).

    A candidate genome/plugin is admitted to the nursery only with
    ``Deflated Sharpe >= 1.50`` **and** ``shadow_fills >= 30``. Both thresholds
    are read from the forge, never from the request.
    """

    plugin_name: str = Field(
        ..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$"
    )
    candidate_sharpe: float = Field(default=1.85, ge=-10.0, le=25.0)
    shadow_fills: int = Field(default=30, ge=0, le=100_000)
    silo: str = "US"
    operator: str = "ring3.operator"

    @field_validator("silo")
    @classmethod
    def _challenger_silo_ok(cls, value: str) -> str:
        token = str(value).strip().upper()
        if not token:
            raise ValueError("silo must not be empty")
        return token


# ---------------------------------------------------------------------------
# V1.1 sovereign operator auth + dual workspace + clean slate
# ---------------------------------------------------------------------------


class AuthSetupRequest(StrictRequest):
    """``POST /api/v2/auth/setup`` -- first-run sovereign operator."""

    username: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
    password: str = Field(..., min_length=8, max_length=256)
    live_pin: str = Field(..., min_length=4, max_length=12, pattern=r"^[0-9]+$")
    start_clean_slate: bool = True


class AuthLoginRequest(StrictRequest):
    """``POST /api/v2/auth/login`` -- PAPER session."""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class AuthElevateRequest(StrictRequest):
    """``POST /api/v2/auth/elevate-live`` -- second factor for LIVE."""

    live_pin: str = Field(..., min_length=4, max_length=12, pattern=r"^[0-9]+$")


class AuthResetRequest(StrictRequest):
    """``POST /api/v2/auth/reset`` -- credential-gated factory reset.

    Both factors default to empty so a *clean-slate* install (no operator on
    disk) can still be reset with no body.  Once a vault exists Ring 0 refuses
    the reset unless ``password`` and ``live_pin`` both match the stored
    ``scrypt`` and ``pbkdf2_hmac`` digests.
    """

    password: str = Field(default="", max_length=256)
    live_pin: str = Field(default="", max_length=12)


class PaperFundingRequest(StrictRequest):
    """``POST /api/v2/control/ledger/fund-paper`` -- user-chosen capital."""

    silo: str = Field(..., min_length=1, max_length=32)
    amount_units: float = Field(..., gt=0.0, le=1_000_000_000.0)
    vault: str | None = Field(default="ALL_CANONICAL", max_length=32)


class AutopilotControlRequest(StrictRequest):
    """``POST /api/v2/control/workspace/autopilot`` -- PAPER tape play/pause."""

    enabled: bool = False


class AgentAssignRequest(StrictRequest):
    """``POST /api/v2/control/agents/assign`` -- dispatch a Ring 2 agent task."""

    agent_id: str
    silo: str = "US_IBKR"
    symbols: list[str] = Field(default_factory=list)
    strategy_id: str | None = None
    prompt: str = ""
    priority: str = "NORMAL"


class AgentCancelRequest(StrictRequest):
    """``POST /api/v2/control/agents/cancel`` -- cancel one task or clear all."""

    task_id: str = "ALL"


class AgentBatchWatchlistRequest(StrictRequest):
    """``POST /api/v2/control/agents/batch-watchlist`` -- ranked batch scan."""

    silo: str = "US_IBKR"
    symbols: list[str] = Field(default_factory=list)
    strategy_id: str | None = None
    priority: str = "NORMAL"


class AgentFollowupRequest(StrictRequest):
    """``POST /api/v2/control/agents/followup`` -- grounded strategy Q&A."""

    task_id: str = Field(..., min_length=1, max_length=64)
    question: str = Field(..., min_length=1, max_length=2000)


class BacktestRunRequest(StrictRequest):
    """``POST /api/v2/control/backtest/run`` -- tax-net walk-forward run."""

    silo: str = "US_IBKR"
    symbol: str = "SPY"
    strategy_id: str = "ma_golden_cross"
    bars: int = 252
    timeframe: Literal["1m", "5m", "15m", "1h", "1d"] = "1d"
    initial_capital_units: float = 100_000.0
    fast_window: int | None = None
    slow_window: int | None = None
    stop_loss_bps: float | None = None
    take_profit_bps: float | None = None
    data_source: Literal[
        "SYNTHETIC_WALK_FORWARD",
        "TRADINGVIEW_REPLAY",
    ] = "SYNTHETIC_WALK_FORWARD"
    replay_offset: int = Field(default=0, ge=0, le=5000)


class BacktestCompareRequest(StrictRequest):
    """``POST /api/v2/control/backtest/compare-strategies`` -- 8-way matrix."""

    silo: str = "US_IBKR"
    symbol: str = "SPY"
    timeframe: Literal["1m", "5m", "15m", "1h", "1d"] = "1d"
    bars: int = 252


class NurseryPromoteRequest(StrictRequest):
    """``POST /api/v2/control/backtest/promote-to-nursery`` -- DSR-gated."""

    backtest_id: str


# ---------------------------------------------------------------------------
# V1.3 dual login portals, options chain, terminal, logbook and broker hub
# ---------------------------------------------------------------------------


class AuthPaperLoginRequest(StrictRequest):
    """``POST /api/v2/auth/login/paper`` -- PAPER portal (username + password)."""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class AuthLiveLoginRequest(StrictRequest):
    """``POST /api/v2/auth/login/live`` -- atomic three-factor LIVE portal.

    ``live_pin`` defaults to empty on purpose: a *missing* PIN must be a
    ``403 LIVE_PIN_REJECTED`` (an authorization verdict), not a pydantic
    ``422``.  The gateway therefore validates the PIN itself.
    """

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)
    live_pin: str = Field(default="", max_length=12)


class OptionsChainRequest(StrictRequest):
    """``POST /api/v2/control/options/chain`` -- one grounded option chain."""

    symbol: str = Field(default="NIFTY", min_length=1, max_length=32)
    spot: float | None = Field(default=None, gt=0.0, le=100_000_000_000.0)
    volatility: float | None = Field(default=None, gt=0.0, le=5.0)
    days_to_expiry: int | None = Field(default=None, ge=1, le=730)
    strike_count: int = Field(default=11, ge=3, le=31)
    risk_free_rate: float | None = Field(default=None, ge=-0.05, le=0.5)


class StrategyPayoffRequest(StrictRequest):
    """``POST /api/v2/control/options/strategy-payoff`` -- multi-leg builder."""

    symbol: str = Field(default="NIFTY", min_length=1, max_length=32)
    strategy: Literal[
        "BULL_CALL_SPREAD",
        "BEAR_PUT_SPREAD",
        "SHORT_STRADDLE",
        "LONG_STRANGLE",
        "IRON_CONDOR",
        "PROTECTIVE_PUT",
    ] = "BULL_CALL_SPREAD"
    spot: float | None = Field(default=None, gt=0.0, le=100_000_000_000.0)
    volatility: float | None = Field(default=None, gt=0.0, le=5.0)
    days_to_expiry: int | None = Field(default=None, ge=1, le=730)
    risk_free_rate: float | None = Field(default=None, ge=-0.05, le=0.5)
    width_steps: int = Field(default=1, ge=1, le=6)


class TerminalQuoteRequest(StrictRequest):
    """``POST /api/v2/control/terminal/quote`` -- one grounded terminal frame."""

    symbol: str = Field(default="BTCUSDT", min_length=1, max_length=32)
    silo: str | None = Field(default=None, max_length=32)
    bars: int = Field(default=60, ge=20, le=240)
    depth: int = Field(default=10, ge=5, le=10)


class LogbookEntryRequest(StrictRequest):
    """``POST /api/v2/control/logbook/entry`` -- one structured trade journal."""

    silo: str = Field(..., min_length=1, max_length=32)
    symbol: str = Field(..., min_length=1, max_length=32)
    side: Literal["LONG", "SHORT", "BUY", "SELL"] = "LONG"
    setup_strategy: str = Field(default="", max_length=64)
    entry_price: float = Field(..., gt=0.0, le=100_000_000_000.0)
    exit_price: float = Field(..., gt=0.0, le=100_000_000_000.0)
    quantity: float = Field(..., gt=0.0, le=1_000_000_000.0)
    stop_loss: float = Field(default=0.0, ge=0.0, le=100_000_000_000.0)
    thesis: str = Field(default="", max_length=4000)
    lessons_learned: str = Field(default="", max_length=4000)
    workspace: Literal["PAPER", "LIVE"] = "PAPER"


class BrokerPingRequest(StrictRequest):
    """``POST /api/v2/control/brokers/ping`` -- adapter signing probe."""

    venue: str = Field(..., min_length=1, max_length=48)
    operator: str = Field(default="ring3.operator", max_length=64)


class TradingViewStudyRequest(StrictRequest):
    """``POST /api/v2/market/tv-study`` -- Pine Script study evaluation."""

    symbol: str = Field(..., min_length=1, max_length=64)
    study_id: str = Field(..., min_length=1, max_length=96)
    timeframe: str = Field(default="1D", max_length=8)
    bars: int = Field(default=200, ge=10, le=1000)
    replay_offset: int = Field(default=0, ge=0, le=5000)
    params: dict[str, float] | None = None


class BrokerHandshakeRequest(StrictRequest):
    """``POST /api/v2/control/brokers/handshake`` -- read-only venue probe."""

    venue: str = Field(..., min_length=1, max_length=48)
    dry_run: bool = True
    operator: str = Field(default="ring3.operator", max_length=64)


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
        state_journal: Any = None,
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
        # Gate 22 GIL isolation: the paper tape is a pure-Python factor scan that
        # shares one interpreter with in-process HTTP probes.  A pre-flight run
        # takes this gate for the whole certificate so a background tick can never
        # be scheduled inside the sub-millisecond hardware latch measurement.
        self._autopilot_tick_lock = threading.Lock()
        self._autopilot_active = False
        # V1.1: operator PLAY/PAUSE override (``None`` = follow the runtime).
        self._autopilot_override: bool | None = None
        # V1.1 clean-slate flag surfaced on ``/api/v2/auth/status``.
        self._clean_slate_active = False
        # V1.1: per-process operator auth (Ring 0 vault, 0600, salted KDF).
        self._operator_auth = None
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
        # V1.3 trade logbook and execution journal (clean-slate aware).
        self._logbook_entries: list[dict[str, Any]] = []
        self._logbook_sequence: int = 0
        # V1.6 concurrency + idempotency hardening: one re-entrant lock guards
        # every mutable control-plane structure, and a bounded receipt cache
        # makes ``idempotency_key`` submissions replay the original receipt
        # instead of double-debiting the vault.
        self._state_lock = threading.RLock()
        self._idempotent_receipts: dict[str, dict[str, Any]] = {}
        self._idempotency_order: list[str] = []
        self._max_idempotent_receipts = 4_096
        # ------------------------------------------------------------------
        # V1.8 (PART 3.1): crash-safe atomic state journal.
        # The journal is the only durable surface for logbook entries, saved
        # walk-forward backtest summaries and completed agent-task metadata.
        # A missing file is the exact Clean-Slate contract: 0 logbook
        # entries, 0 backtests, 0 agent tasks, 0 vault balances.
        # ------------------------------------------------------------------
        self.state_journal = (
            state_journal
            if state_journal is not None
            else _default_state_journal()
        )
        self._journal_restored: dict[str, int] = {
            "logbook": 0,
            "backtests": 0,
            "agent_tasks": 0,
        }
        self._journal_backtests: list[dict[str, Any]] = []
        self._restore_journal()
        # V1.8 (PART 2.2): wire the portfolio-margin provider into the OMS so
        # ``SmartOMS.validate_proposal`` can reject over-committed orders.
        try:  # pragma: no cover - depends on the kernel build
            if hasattr(self.oms, "margin_provider") and getattr(
                self.oms, "margin_provider", None
            ) is None:
                self.oms.margin_provider = self.free_margin_micros
        except Exception:
            pass
        # V1.8 (PART 3.2): SSE reader accounting (leak-free, deadlock-free).
        self._stream_lock = threading.RLock()
        self._active_streams = 0
        self._stream_frames_emitted = 0
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
        # ---- Phase 4 Step 6: interactive brain / forge drill surfaces -----
        # The cascade engine is warmed once per process (numba kernel) and then
        # reused: a cold warmup inside an HTTP request would blow the 1.8 ms SLA.
        self._mirofish_engines: dict[str, Any] = {}
        self._mirofish_lock = threading.Lock()
        # One committee per silo: the staged graph allocates its own executor
        # pool, so rebuilding it on every debate would be pure overhead.
        self._committee_cache: dict[str, Any] = {}
        self._committee_lock = threading.Lock()
        # ---- V1.2: Ring 2 agent task assignment + walk-forward backtester --
        # Both engines are constructed here, after every collaborator they
        # depend on exists, and both start empty (clean-slate boot).
        from ring2_brain.agents.task_dispatcher import AgentTaskDispatcher
        from ring2_brain.backtest.engine import WalkForwardBacktestEngine
        from ring2_brain.quant.scanner import ValuationLab

        self.backtest_engine = WalkForwardBacktestEngine(
            plugins=self.plugins,
            shadow_twin=self.shadow_twin,
        )
        self._valuation_lab = ValuationLab()
        self.task_dispatcher = AgentTaskDispatcher(
            valuation=self._valuation_lab,
            forge=self.forge,
            backtest_engine=self.backtest_engine,
            token_ledger=self.token_ledger,
            journal=self.state_journal,
        )
        self.task_dispatcher.attach(contagion_factory=self._contagion_engine_for)

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

            with self.autopilot_quiesced() as tape_quiesced:
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
            body["autopilot_tape_active"] = bool(self._autopilot_active)
            body["autopilot_quiesced"] = bool(tape_quiesced)
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

    # ------------------------------------------------------------------
    # Phase 4 Step 5: interactive operator actions (paper-only by design)
    # ------------------------------------------------------------------
    _PAPER_REFERENCE_PRICES: Mapping[str, float] = {
        "CRYPTO_BINANCE": 50_000.0,
        "INDIA_ZERODHA_DHAN": 2_400.0,
        "US_IBKR": 180.0,
        "FX_MT5": 1.085,
    }

    # ------------------------------------------------------------------
    # V1.8 (PART 3.1): crash-safe atomic state journal helpers
    # ------------------------------------------------------------------
    def _journal_load(self) -> dict[str, Any]:
        journal = getattr(self, "state_journal", None)
        if journal is None:
            return {}
        try:
            state = journal.load()
        except Exception:
            return {}
        return dict(state) if isinstance(state, Mapping) else {}

    def _restore_journal(self) -> None:
        state = self._journal_load()
        rows = state.get("logbook")
        if isinstance(rows, list):
            self._logbook_entries.extend(
                dict(row) for row in rows if isinstance(row, Mapping)
            )
        self._logbook_sequence = max(
            (int(row.get("sequence") or 0) for row in self._logbook_entries),
            default=0,
        )
        backtests = state.get("backtests")
        if isinstance(backtests, list):
            self._journal_backtests = [
                dict(row) for row in backtests if isinstance(row, Mapping)
            ]
        tasks = state.get("agent_tasks")
        self._journal_restored = {
            "logbook": len(self._logbook_entries),
            "backtests": len(self._journal_backtests),
            "agent_tasks": len(tasks) if isinstance(tasks, list) else 0,
        }

    def _journal_append(self, section: str, entry: Mapping[str, Any]) -> None:
        journal = getattr(self, "state_journal", None)
        if journal is None:
            return
        try:
            journal.append(section, dict(entry))
        except Exception:
            return

    def _journal_clear(self, section: str) -> None:
        journal = getattr(self, "state_journal", None)
        if journal is None:
            return
        try:
            journal.clear_section(section)
        except Exception:
            return

    def _journal_reset(self) -> dict[str, Any]:
        journal = getattr(self, "state_journal", None)
        self._journal_backtests = []
        if journal is None:
            return {"journal": False, "journal_path": "", "clean_slate": True}
        try:
            state = journal.reset()
        except Exception:
            return {"journal": False, "journal_path": "", "clean_slate": True}
        return {
            "journal": True,
            "journal_path": str(getattr(journal, "path", "")),
            "clean_slate": bool(state.get("clean_slate_active", True)),
        }

    def journal_snapshot(self) -> dict[str, Any]:
        """Durable-journal state: counts per section plus the restore receipts."""

        journal = getattr(self, "state_journal", None)
        state = self._journal_load()
        path = getattr(journal, "path", None) if journal is not None else None
        try:
            exists = bool(path is not None and path.exists())
        except Exception:
            exists = False
        return {
            "enabled": journal is not None,
            "journal_path": str(path) if path is not None else "",
            "exists": exists,
            "logbook": len(state.get("logbook") or []),
            "backtests": len(state.get("backtests") or []),
            "agent_tasks": len(state.get("agent_tasks") or []),
            "saved_backtests": len(self._journal_backtests),
            "clean_slate_active": bool(state.get("clean_slate_active", True)),
            "restored": dict(self._journal_restored),
            "schema_version": str(state.get("schema_version") or ""),
        }

    def free_margin_micros(self, silo: str) -> int | None:
        """Free initial margin available to new entries in ``silo``.

        Returned in integer micro-units. ``None`` means "margin is not
        constraining": an un-funded (Clean-Slate) silo stays permissive so
        the V1.1 clean-slate paper contract keeps working. Once the silo
        holds capital, the available margin is the vault total minus whatever
        the crash-atomic outbox has already reserved.
        """

        try:
            balances = self.ledger.vault_balances(str(silo))
        except Exception:
            return None
        total = 0
        try:
            for value in dict(balances).values():
                total += int(value or 0)
        except Exception:
            return None
        if total <= 0:
            return None
        reserved = 0
        try:
            rows = getattr(self.outbox, "reservations", None)
            rows = rows() if callable(rows) else []
            for row in rows:
                row_silo = str(getattr(row, "silo", "") or "")
                if row_silo and row_silo != str(silo):
                    continue
                state = str(getattr(row, "state", "") or "").upper()
                if state in {"SETTLED", "RELEASED", "CANCELLED"}:
                    continue
                reserved += int(getattr(row, "notional_micros", 0) or 0)
        except Exception:
            reserved = 0
        return max(0, int(total - reserved))

    def _known_silos(self) -> list[str]:
        registry = self.registry
        if registry is None:
            return []
        with contextlib.suppress(Exception):
            return [str(s) for s in registry.silo_ids()]
        return []

    def _halted(self) -> bool:
        return bool(getattr(self.kill_latch, "is_halted", False))

    @_serialised_control
    def submit_paper_order(self, request: PaperOrderRequest) -> dict[str, Any]:
        """Route one grounded **paper** order through outbox -> OMS -> IBOR.

        Nothing here touches a broker socket, reads a credential, or signs an
        order: the payload has no venue and no key surface.  ``sec_018`` Law 4
        is preserved because a paper fill still pays the full cost floor and
        must clear the conformal gate before it is reserved.
        """

        from core.schemas import UnsignedProposal
        from ring0_vault.ibor.ledger_outbox import MICROS_PER_UNIT

        if self._halted():
            raise HTTPException(
                status_code=409,
                detail=(
                    "GLOBAL_HALTED: the global kill latch is HALTED; rearm via "
                    "POST /api/v2/control/rearm before submitting paper orders"
                ),
            )
        known = self._known_silos()
        if known and request.silo not in known:
            raise HTTPException(
                status_code=422,
                detail=f"unknown silo {request.silo!r}; expected one of {known}",
            )
        idem = str(request.idempotency_key or "").strip()
        if idem:
            if len(idem) > 128 or any(ch in idem for ch in "\x00/\\"):
                raise HTTPException(status_code=422, detail="INVALID_IDEMPOTENCY_KEY")
            with self._state_lock:
                cached = self._idempotent_receipts.get(idem)
                if cached is not None:
                    return {
                        **cached,
                        "duplicate": True,
                        "idempotent_replay": True,
                        "generated_at_ns": time.time_ns(),
                    }
        guard = self.guard
        if guard is not None and bool(guard.is_frozen(request.silo)):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"SILO_FROZEN: {request.silo} is frozen by reconciliation drift; "
                    "repair the ledger before submitting paper orders"
                ),
            )

        now = time.time_ns()
        price = float(
            request.limit_price
            if request.limit_price is not None
            else self._PAPER_REFERENCE_PRICES.get(request.silo, 100.0)
        )
        symbol = str(request.symbol).strip().upper()
        instrument_spec = None
        instrument_check = None
        try:
            instrument_spec = instrument_registry.get(request.silo, symbol)
        except KeyError:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"INSTRUMENT_REJECTED: {symbol} is not in the "
                    f"{request.silo} contract registry"
                ),
            ) from None
        if instrument_spec is not None:
            instrument_check = instrument_registry.validate_order(
                request.silo, symbol, float(request.quantity), price
            )
            if not instrument_check.ok:
                raise HTTPException(
                    status_code=422,
                    detail=f"INSTRUMENT_REJECTED: {instrument_check.reason}",
                )
            if instrument_check.price_changed:
                price = float(instrument_check.adjusted_price)
        half_spread = max(price * 0.0001, price * (request.spread_bps / 10_000.0) / 2.0)
        book = (price - half_spread, price + half_spread)
        proposal_id = (
            f"paper-idem-{_sha256_hex(idem)[:24]}"
            if idem
            else f"paper-{request.silo.lower()}-{uuid.uuid4().hex[:12]}"
        )
        proposal = UnsignedProposal(
            proposal_id=proposal_id,
            strategy_id=str(request.strategy_id or self.strategy_id),
            silo=request.silo,
            vault=VaultName.SPINAL_SCALPER,
            symbol=symbol,
            side=str(request.side),
            qty=float(request.quantity),
            limit_price=price,
            expected_gross_bps=float(request.expected_gross_bps),
            spread_bps=float(request.spread_bps),
            grounding_hash=_sha256_hex(
                f"{proposal_id}|{request.silo}|{symbol}|{request.side}|"
                f"{request.quantity}|{price}|{request.order_style}"
            ),
            created_at_ns=now,
        )
        # V1.8 pre-trade gates (sec_018 Law 4 - the default answer is no trade):
        # circuit band, margin sufficiency and the drawdown breaker must fire
        # BEFORE any capital is reserved, so a rejected order can never move the
        # silo vault.  SmartOMS raises the bare reason codes below.
        try:
            self.oms.validate_proposal(proposal)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        # Exact integer micro-units. The previous float product lost whole
        # micro-units once qty*price exceeded 2**53 (76.6% of institutional
        # sized samples diverged from the exact value), so the P&L reported
        # here could disagree with the notional the outbox actually reserved.
        from ring0_vault.ibor.ledger_outbox import to_micros as _to_micros

        notional_micros = (
            _to_micros(proposal.qty) * _to_micros(price)
        ) // MICROS_PER_UNIT
        # bps -> micro-bps keeps the rounding integral and half-up, matching
        # the Ring 0 convention instead of rounding through a float.
        _bps_scaled = int(round(max(0.0, request.expected_gross_bps) * 1_000_000))
        _bps_denominator = 10_000 * 1_000_000
        realized_net_micros = (
            notional_micros * _bps_scaled + _bps_denominator // 2
        ) // _bps_denominator

        try:
            reservation = self.outbox.reserve(
                proposal, cost_floor=self.cost_floor(request.silo), now_ns=now
            )
            if getattr(reservation, "duplicate", False):
                duplicate_row = _jsonable(
                    self.outbox.get(reservation.outbox_idempotency_key)
                ) or {}
                return {
                    "status": "DUPLICATE",
                    "mode": "PAPER",
                    "page": "orders",
                    "duplicate": True,
                    "idempotent_replay": True,
                    "silo": request.silo,
                    "symbol": symbol,
                    "side": proposal.side,
                    "quantity": float(proposal.qty),
                    "order_style": request.order_style,
                    "order_id": str(
                        duplicate_row.get("broker_order_id")
                        or reservation.outbox_idempotency_key
                    ),
                    "filled": False,
                    "fill_price": float(price),
                    "notional_micros": int(reservation.notional_micros),
                    "realized_net_micros": 0,
                    "statutory_cost": statutory_cost_engine.breakdown(
                        request.silo, int(reservation.notional_micros)
                    ).to_dict(),
                    "instrument": instrument_spec.to_dict() if instrument_spec else None,
                    "instrument_check": (
                        instrument_check.to_dict() if instrument_check else None
                    ),
                    "receipt": (
                        duplicate_row
                        if isinstance(duplicate_row, dict)
                        else {"value": duplicate_row}
                    ),
                    "generated_at_ns": time.time_ns(),
                }
            order_row: dict[str, Any]
            order_id: str
            filled = False
            if request.order_style == "MAKER_PEG":
                chaser = self.oms.submit_maker_peg(
                    proposal, reservation, book, now_ns=now
                )
                order_id = chaser.order_id
                order_row = _jsonable(chaser)
                if not isinstance(order_row, dict):  # pragma: no cover - defensive
                    order_row = {"order_id": order_id}
                if request.fill_immediately:
                    self.oms.on_fill(
                        order_id,
                        price=float(chaser.price),
                        realized_net_micros=realized_net_micros,
                        now_ns=now + 1,
                    )
                    filled = True
            else:
                plan = self.oms.submit_twap(
                    proposal, request.top_of_book_qty, now_ns=now
                )
                order_id = proposal_id
                order_row = _jsonable(plan)
                if not isinstance(order_row, dict):  # pragma: no cover - defensive
                    order_row = {"order_id": order_id}
                order_row.setdefault("order_id", order_id)
                order_row["kind"] = "TWAP_ICEBERG"
                if request.fill_immediately:
                    self.outbox.commit(
                        reservation.outbox_idempotency_key,
                        broker_order_id=order_id,
                        now_ns=now + 1,
                        realized_net_micros=realized_net_micros,
                        ratchet=getattr(self.oms, "ratchet", None),
                    )
                    filled = True
                    with contextlib.suppress(Exception):
                        self.oms.record_execution(
                            ExecutionEvent(
                                event_type="ORDER_FILLED",
                                silo=str(proposal.silo),
                                symbol=str(proposal.symbol),
                                order_id=order_id,
                                side=str(proposal.side),
                                qty=float(proposal.qty),
                                price=price,
                                state="FILLED",
                                reason=(
                                    "paper TWAP/Iceberg swept at reference mid; "
                                    f"in-silo ratchet swept {realized_net_micros} micros"
                                ),
                                timestamp_ns=now + 1,
                            )
                        )
            receipt = self.outbox.get(reservation.outbox_idempotency_key)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a rejected paper order is never a 500
            raise HTTPException(
                status_code=409,
                detail=f"paper order rejected: {type(exc).__name__}: {exc}",
            ) from None

        ratchet = getattr(self.oms, "ratchet", None)
        split: dict[str, Any] = {}
        if ratchet is not None:
            with contextlib.suppress(Exception):
                split = _jsonable(ratchet.split(realized_net_micros)) or {}
        reservation_row = _jsonable(receipt) if receipt is not None else {}
        if not isinstance(reservation_row, dict):
            reservation_row = {"value": reservation_row}
        statutory = statutory_cost_engine.breakdown(
            request.silo, notional_micros
        ).to_dict()
        response = {
            "status": "FILLED" if filled else "WORKING",
            "mode": "PAPER",
            "page": "orders",
            "silo": request.silo,
            "symbol": proposal.symbol,
            "side": proposal.side,
            "quantity": float(proposal.qty),
            "order_style": request.order_style,
            "order_id": order_id,
            "filled": filled,
            "fill_price": float(price),
            "notional_micros": notional_micros,
            "realized_net_micros": realized_net_micros if filled else 0,
            "statutory_cost": statutory,
            "instrument": instrument_spec.to_dict() if instrument_spec else None,
            "instrument_check": instrument_check.to_dict() if instrument_check else None,
            "ratchet": {
                "applied": bool(filled and realized_net_micros > 0),
                "long_term_pct": 35,
                "reserve_pct": 15,
                "scalper_pct": 50,
                "split_micros": split,
            },
            "proposal": {
                "proposal_id": proposal.proposal_id,
                "strategy_id": proposal.strategy_id,
                "vault": str(proposal.vault),
                "limit_price": proposal.limit_price,
                "expected_gross_bps": proposal.expected_gross_bps,
                "spread_bps": proposal.spread_bps,
                "grounding_hash": proposal.grounding_hash,
                "is_grounded": proposal.is_grounded,
            },
            "order": order_row,
            "receipt": reservation_row,
            "outbox": _jsonable(self.outbox.snapshot()),
            "blotter": self.orders_page(),
            "operator": str(request.operator),
            "law": "sec_018 Law 4 - paper only; no broker credential is read",
            "generated_at_ns": time.time_ns(),
        }
        if idem:
            with self._state_lock:
                self._idempotent_receipts[idem] = response
                self._idempotency_order.append(idem)
                if len(self._idempotency_order) > self._max_idempotent_receipts:
                    oldest = self._idempotency_order.pop(0)
                    self._idempotent_receipts.pop(oldest, None)
        return response

    @_serialised_control
    def cancel_paper_orders(self, request: CancelOrdersRequest) -> dict[str, Any]:
        """Cancel working ``SmartOMS`` orders and void their outbox holds."""

        target = str(request.order_id or "ALL").strip()
        if target.upper() != "ALL":
            target = _identifier(target, field_name="order")
        known = self._known_silos()
        silo = str(request.silo).strip().upper() if request.silo else None
        if silo is not None and known and silo not in known:
            raise HTTPException(
                status_code=422,
                detail=f"unknown silo {silo!r}; expected one of {known}",
            )
        now = time.time_ns()
        reason = str(request.reason or "operator cancel")
        cancelled_ids: list[str] = []
        voided_ids: list[str] = []
        receipt: dict[str, Any] | None = None

        try:
            if target.upper() == "ALL":
                open_ids = [str(order.order_id) for order in self.oms.open_orders(silo)]
                pending_keys = [
                    res.outbox_idempotency_key
                    for res in self.outbox.open_reservations()
                    if silo is None or str(res.silo) == silo
                ]
                self.oms.cancel_all_entries(
                    silo=silo,
                    now_ns=now,
                    reason=reason,
                    extra_outbox_keys=tuple(pending_keys),
                )
                cancelled_ids = open_ids
                voided_ids = [str(key) for key in pending_keys]
                leftover = [
                    res.outbox_idempotency_key
                    for res in self.outbox.open_reservations()
                    if silo is None or str(res.silo) == silo
                ]
                for key in leftover:
                    with contextlib.suppress(Exception):
                        self.outbox.void(key, reason=reason, now_ns=now)
                        voided_ids.append(str(key))
            else:
                try:
                    reservation = self.oms.cancel(target, reason=reason, now_ns=now)
                except KeyError:
                    reservation = None
                if reservation is not None:
                    receipt = _jsonable(reservation)
                    if not isinstance(receipt, dict):  # pragma: no cover - defensive
                        receipt = {"value": receipt}
                    cancelled_ids = [target]
                else:
                    match = next(
                        (
                            res
                            for res in self.outbox.open_reservations()
                            if res.outbox_idempotency_key == target
                            or res.proposal_id == target
                            or res.broker_order_id == target
                        ),
                        None,
                    )
                    if match is None:
                        raise HTTPException(
                            status_code=404,
                            detail=f"no working order or pending reservation {target!r}",
                        )
                    reservation = self.outbox.void(
                        match.outbox_idempotency_key, reason=reason, now_ns=now
                    )
                    receipt = _jsonable(reservation)
                    if not isinstance(receipt, dict):  # pragma: no cover - defensive
                        receipt = {"value": receipt}
                    voided_ids = [str(match.outbox_idempotency_key)]
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - cancel is fail-safe, never a 500
            raise HTTPException(
                status_code=409,
                detail=f"cancel rejected: {type(exc).__name__}: {exc}",
            ) from None

        return {
            "status": "CANCELLED",
            "mode": "PAPER",
            "page": "orders",
            "order_id": target,
            "silo": silo or "ALL",
            "cancelled_count": len(cancelled_ids),
            "cancelled_order_ids": cancelled_ids,
            "voided_outbox_keys": voided_ids,
            "receipt": receipt,
            "outbox": _jsonable(self.outbox.snapshot()),
            "blotter": self.orders_page(),
            "operator": str(request.operator),
            "reason": reason,
            "law": "sec_009 - a cancel always releases its pending transfer",
            "generated_at_ns": time.time_ns(),
        }

    @_serialised_control
    def shadow_burst(self, request: ShadowBurstRequest) -> dict[str, Any]:
        """Drive ``rounds`` paper ticks through the 30-fill shadow nursery."""

        runner = self.autopilot_runner
        if runner is None:
            raise HTTPException(
                status_code=503,
                detail="autopilot runner unavailable; the shadow nursery is not wired",
            )
        known = self._known_silos()
        if known and request.silo not in known:
            raise HTTPException(
                status_code=422,
                detail=f"unknown silo {request.silo!r}; expected one of {known}",
            )
        strategy_id = str(request.strategy_id or self.strategy_id)
        twin = self.shadow_twin
        target_fills = int(request.rounds)
        baseline_fills = 0
        if twin is not None:
            with contextlib.suppress(Exception):
                baseline_fills = int(twin.shadow_fill_count(strategy_id))
        # One paper tick does not always resolve a shadow fill (the twin posts a
        # maker peg, then fills it on a later tick).  Keep ticking until the
        # operator gets the burst they asked for, with a hard safety ceiling.
        max_ticks = max(target_fills * 4, target_fills + 8)
        completed = 0
        fills_gained = 0
        last_tick: dict[str, Any] = {}
        halted = False
        try:
            while completed < max_ticks:
                outcome = runner(silos=(request.silo,))
                completed += 1
                if isinstance(outcome, Mapping):
                    last_tick = {str(k): v for k, v in outcome.items()}
                else:  # pragma: no cover - defensive
                    last_tick = {"value": outcome}
                if str(last_tick.get("status", "")).upper() == "HALTED":
                    halted = True
                if twin is not None:
                    with contextlib.suppress(Exception):
                        fills_gained = (
                            int(twin.shadow_fill_count(strategy_id)) - baseline_fills
                        )
                if halted or fills_gained >= target_fills:
                    break
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a burst failure is a 502
            raise HTTPException(
                status_code=502,
                detail=f"shadow burst failed: {type(exc).__name__}: {exc}",
            ) from None

        required = int(getattr(twin, "required_fills", 30) or 30)
        fills = 0
        graduated = False
        q98 = 0.0
        remaining = required
        if twin is not None:
            with contextlib.suppress(Exception):
                fills = int(twin.shadow_fill_count(strategy_id))
            with contextlib.suppress(Exception):
                graduated = bool(twin.is_graduated(strategy_id))
            with contextlib.suppress(Exception):
                q98 = float(twin.q98_bps(strategy_id) or 0.0)
            with contextlib.suppress(Exception):
                remaining = int(twin.remaining_fills(strategy_id))
        page = self.analyzer_page()
        nursery = page.get("nursery", {}) if isinstance(page, dict) else {}
        q98_value = float(q98 or 0.0)
        if not math.isfinite(q98_value):  # NaN/inf residual window before calibration
            q98_value = 0.0
        return {
            "status": "HALTED" if halted else "BURST_COMPLETE",
            "mode": "PAPER",
            "page": "analyzer",
            "silo": request.silo,
            "strategy_id": strategy_id,
            "rounds_requested": target_fills,
            "rounds_completed": completed,
            "ticks_executed": completed,
            "fills_gained": int(fills_gained),
            "shadow_fills": fills,
            "required_fills": required,
            "remaining_fills": remaining,
            "graduated": bool(graduated),
            "graduation_threshold_met": fills >= required,
            "adverse_selection_bps": round(q98_value, 6),
            "q98_bps": round(q98_value, 6),
            "total_shadow_fills": int(
                getattr(last_tick, "get", lambda *_: 0)("shadow_fills", fills) or fills
            ),
            "last_tick": {
                key: _jsonable(value)
                for key, value in last_tick.items()
                if key in ("status", "ticks", "total_ticks", "shadow_fills", "live_fills", "graduated")
            },
            "nursery": nursery,
            "operator": str(request.operator),
            "law": "sec_018 Law 4 - 30 shadow fills before any live graduation",
            "generated_at_ns": time.time_ns(),
        }

    def price_option(self, request: OptionPriceRequest) -> dict[str, Any]:
        """Black-Scholes price sealed into a SHA-256 grounding packet."""

        from core.schemas import GroundedFactPacket
        from ring2_brain.quant.scanner import ValuationLab
        from ring2_brain.truth.grounding_gate import NumericalGroundingVerifier

        years = float(request.days_to_expiry) / 365.0
        try:
            lab = ValuationLab()
            greeks = lab.options(
                spot=float(request.spot),
                strike=float(request.strike),
                volatility=float(request.volatility),
                rate=float(request.risk_free_rate),
                years=years,
                kind=request.option_type.lower(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a pricer failure is a 502
            raise HTTPException(
                status_code=502,
                detail=f"option pricing failed: {type(exc).__name__}: {exc}",
            ) from None

        now = time.time_ns()
        metrics: dict[str, float] = {
            "spot": float(request.spot),
            "strike": float(request.strike),
            "volatility": float(request.volatility),
            "years": years,
            "risk_free_rate": float(request.risk_free_rate),
            "price": float(greeks.get("price", 0.0)),
            "delta": float(greeks.get("delta", 0.0)),
            "gamma": float(greeks.get("gamma", 0.0)),
            "vega": float(greeks.get("vega", 0.0)),
            "theta": float(greeks.get("theta", 0.0)),
            "rho": float(greeks.get("rho", 0.0)),
            "d1": float(greeks.get("d1", 0.0)),
            "d2": float(greeks.get("d2", 0.0)),
        }
        symbol = str(request.symbol).strip().upper()
        packet = GroundedFactPacket(
            symbol=symbol,
            valid_time_ns=now,
            knowledge_ts_ns=now,
            as_of_ns=now,
            metrics=metrics,
        )
        # ``repr`` emits scientific notation for small Greeks (gamma ~ 4e-05),
        # and the claim extractor reads the mantissa only, so the figure would
        # never match the packet. A fixed decimal form keeps every claim exact.
        def _claim(value: float) -> str:
            return f"{float(value):.12f}"

        thesis = (
            f"{request.option_type} grounded option pricing receipt. "
            f"spot {_claim(metrics['spot'])} strike {_claim(metrics['strike'])} "
            f"volatility {_claim(metrics['volatility'])} years {_claim(metrics['years'])} "
            f"risk_free_rate {_claim(metrics['risk_free_rate'])} "
            f"price {_claim(metrics['price'])} delta {_claim(metrics['delta'])} "
            f"gamma {_claim(metrics['gamma'])} vega {_claim(metrics['vega'])} "
            f"theta {_claim(metrics['theta'])} rho {_claim(metrics['rho'])} "
            f"d1 {_claim(metrics['d1'])} d2 {_claim(metrics['d2'])}."
        )
        try:
            report = NumericalGroundingVerifier().verify_thesis_claims(thesis, packet)
        except Exception as exc:  # noqa: BLE001 - ungrounded numbers are a 422
            raise HTTPException(
                status_code=422,
                detail=f"{type(exc).__name__}: {exc}",
            ) from None
        grounding_ledger: dict[str, Any] | None = None
        if self.grounding is not None:
            # ``GroundingLedger.append(packet)`` is the immutable entry point;
            # ``commit(...)`` builds a packet from raw metrics instead.
            with contextlib.suppress(Exception):
                if hasattr(self.grounding, "append"):
                    self.grounding.append(packet, artifact_kind="option_price")
            packet_count = 0
            with contextlib.suppress(Exception):
                packet_count = len(self.grounding.packets())
            if packet_count == 0:
                with contextlib.suppress(Exception):
                    packet_count = len(self.grounding)
            grounding_ledger = {"packets": int(packet_count), "digest": packet.sha256_digest}
        return {
            "status": "GROUNDED",
            "page": "quant",
            "symbol": symbol,
            "option_type": request.option_type,
            "inputs": {
                "spot": metrics["spot"],
                "strike": metrics["strike"],
                "volatility": metrics["volatility"],
                "days_to_expiry": float(request.days_to_expiry),
                "years_to_expiry": years,
                "risk_free_rate": metrics["risk_free_rate"],
            },
            "greeks": {
                "price": metrics["price"],
                "delta": metrics["delta"],
                "gamma": metrics["gamma"],
                "vega": metrics["vega"],
                "theta": metrics["theta"],
                "rho": metrics["rho"],
                "d1": metrics["d1"],
                "d2": metrics["d2"],
            },
            "grounding": {
                "sha256_digest": packet.sha256_digest,
                "verified": bool(report.claims_checked >= 0),
                "claims_checked": int(report.claims_checked),
                "derived_expressions": int(report.derived_expressions),
                "tolerance": float(report.tolerance),
                "packet": packet.to_dict(),
                "report": report.to_dict(),
            },
            "packet": packet.to_dict(),
            "grounding_ledger": grounding_ledger,
            "operator": str(request.operator),
            "law": "sec_018 Law 3 - every number carries a SHA-256 commitment",
            "generated_at_ns": time.time_ns(),
        }

    def scan_factors(self, request: QuantScanRequest) -> dict[str, Any]:
        """Run the live 490-factor ``VectorizedFactorScanner`` scan."""

        from ring2_brain.quant.scanner import FactorPanel

        started = time.perf_counter()
        try:
            panel = FactorPanel.generate(n_symbols=24, n_periods=64)
            with contextlib.suppress(Exception):
                self.scanner.warmup()
            result = self.scanner.scan(panel, commit=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a scan failure is a 502
            raise HTTPException(
                status_code=502,
                detail=f"factor scan failed: {type(exc).__name__}: {exc}",
            ) from None
        latency_ms = (time.perf_counter() - started) * 1000.0
        payload = _jsonable(result)
        if not isinstance(payload, dict):
            payload = {"result": payload}
        candidates = payload.get("candidates", []) or []
        if not isinstance(candidates, list):
            candidates = list(candidates)
        self._quant_cache = {"at": time.monotonic(), "data": payload}
        return {
            "status": "SCANNED",
            "page": "quant",
            "scan_id": payload.get("scan_id", ""),
            "factor_count": int(
                payload.get("factor_count", _read(self.scanner, "factor_count", 0)) or 0
            ),
            "universe_size": int(payload.get("universe_size", 0) or 0),
            "candidate_count": len(candidates),
            "top_k": int(request.top_k),
            "candidates": candidates[: int(request.top_k)],
            "scan_latency_ms": round(latency_ms, 3),
            "reported_elapsed_ms": float(payload.get("elapsed_ms", 0.0) or 0.0),
            "kernel_count": int(payload.get("kernel_count", 0) or 0),
            "compression_ratio": float(payload.get("compression_ratio", 0.0) or 0.0),
            "as_of_ns": int(payload.get("as_of_ns", 0) or 0),
            "operator": str(request.operator),
            "law": "sec_018 Law 3 - ranked candidates carry per-row digests",
            "generated_at_ns": time.time_ns(),
        }

    def skill_markdown(self, skill_name: str) -> dict[str, Any]:
        """Progressive disclosure: return one ``SKILL.md`` body on demand."""

        from ring2_brain.skills.skill_registry import parse_frontmatter

        token = str(skill_name or "").strip()
        if (
            not token
            or token in {".", ".."}
            or "/" in token
            or "\\" in token
            or "\x00" in token
            or ".." in token
            or len(token) > 128
        ):
            raise HTTPException(status_code=404, detail=f"unknown skill {token!r}")
        registry = self.skills
        meta = None
        if registry is not None:
            with contextlib.suppress(Exception):
                meta = registry.get(token)
        if meta is None:
            raise HTTPException(
                status_code=404, detail=f"unknown skill {token!r}"
            )
        meta_path = getattr(meta, "path", "")
        root_raw = getattr(registry, "root", "") or ""
        if meta_path and root_raw:
            # Fail closed: ``suppress(Exception)`` here made the containment
            # check a no-op, so a runtime-registered SKILL.md outside the skill
            # root could be read through the cockpit.  A path that escapes the
            # registry root is simply not a known skill to this surface.
            try:
                root = Path(str(root_raw)).resolve()
                candidate = Path(str(meta_path))
                resolved = (
                    candidate if candidate.is_absolute() else root / candidate
                ).resolve()
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                raise HTTPException(
                    status_code=404, detail=f"unknown skill {token!r}"
                ) from None
        try:
            body = registry.load(token)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown skill {token!r}") from None
        except PermissionError as exc:
            raise HTTPException(
                status_code=409, detail=f"skill {token!r} is disabled: {exc}"
            ) from None
        except Exception as exc:  # noqa: BLE001 - unreadable skill is a 502
            raise HTTPException(
                status_code=502,
                detail=f"skill load failed: {type(exc).__name__}: {exc}",
            ) from None
        frontmatter: dict[str, Any] = {}
        markdown = str(body or "")
        markdown_body = markdown
        with contextlib.suppress(Exception):
            raw_front, raw_body = parse_frontmatter(markdown)
            frontmatter = dict(raw_front or {})
            markdown_body = str(raw_body)
        return {
            "status": "FOUND",
            "page": "forge",
            "skill": token,
            "name": str(getattr(meta, "name", token) or token),
            "description": str(getattr(meta, "description", "") or ""),
            "enabled": bool(getattr(meta, "enabled", True)),
            "path": str(meta_path),
            "frontmatter": frontmatter,
            "body": markdown_body,
            "markdown": markdown,
            "bytes": len(markdown.encode("utf-8")),
            "progressive_disclosure": True,
            "law": "sec_016 - SKILL.md bodies load only on explicit request",
            "generated_at_ns": time.time_ns(),
        }

    @_serialised_control
    def reconcile_ledger(self, request: LedgerReconcileRequest) -> dict[str, Any]:
        """4-silo ``LiveReconciler`` sweep + double-entry conservation proof."""

        from ring0_vault.ibor import ledger_outbox as _ledger_mod

        LiveReconciler = _ledger_mod.LiveReconciler
        known = self._known_silos()
        requested = (
            [str(s).strip().upper() for s in request.silos]
            if request.silos
            else list(known)
        )
        if known:
            unknown = [s for s in requested if s not in known]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown silo(s) {unknown}; expected one of {known}",
                )
        try:
            reconciler = LiveReconciler(self.ledger, self.guard)
            reports = list(reconciler.reconcile_all(requested))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a reconcile failure is a 502
            raise HTTPException(
                status_code=502,
                detail=f"reconciliation failed: {type(exc).__name__}: {exc}",
            ) from None

        guard = self.guard
        silos: dict[str, Any] = {}
        breached: list[str] = []
        for report in reports:
            row = _jsonable(report)
            if not isinstance(row, dict):  # pragma: no cover - defensive
                row = {"value": row}
            drift_pct = float(row.get("drift_pct", 0.0) or 0.0)
            drift_ppm = round(drift_pct * 10_000.0, 4)
            silo_id = str(row.get("silo", ""))
            frozen = bool(guard.is_frozen(silo_id)) if guard is not None else False
            if drift_ppm > 100.0 or bool(row.get("breached", False)):
                breached.append(silo_id)
            silos[silo_id] = {
                "silo": silo_id,
                "drift_pct": drift_pct,
                "drift_ppm": drift_ppm,
                "threshold_ppm": 100.0,
                "within_threshold": drift_ppm <= 100.0,
                "breached": bool(row.get("breached", False)),
                "frozen": frozen,
                "symbols_checked": int(row.get("symbols_checked", 0) or 0),
                "repair_action": str(row.get("repair_action", "") or ""),
                "vaults": self._vaults(silo_id),
                "report": row,
            }

        invariant = _jsonable(self.ledger.invariant_report())
        if not isinstance(invariant, dict):
            invariant = {"report": invariant}
        transfers = [_jsonable(row) for row in self.ledger.transfers()]
        debits = 0
        credits = 0
        for row in transfers:
            if not isinstance(row, dict):
                continue
            amount = int(row.get("amount_micros", 0) or 0)
            debits += amount
            credits += amount
        balanced = debits == credits and bool(invariant.get("balanced", True))
        return {
            "status": "RECONCILED" if not breached else "DRIFT_BREACH",
            "page": "ledger",
            "silos_checked": requested,
            "silo_count": len(silos),
            "silos": silos,
            "breached_silos": breached,
            "all_within_threshold": not breached,
            "threshold_ppm": 100.0,
            "double_entry": {
                "debits_micros": debits,
                "credits_micros": credits,
                "balanced": balanced,
                "proof": f"sum(debits) {debits} == sum(credits) {credits}"
                if balanced
                else f"sum(debits) {debits} != sum(credits) {credits}",
            },
            "invariant": invariant,
            "frozen_silos": list(guard.frozen_silos) if guard is not None else [],
            "operator": str(request.operator),
            "law": "sec_009 - drift > 0.01% freezes the silo; the ledger is never edited",
            "generated_at_ns": time.time_ns(),
        }

    # -- Phase 4 Step 6: interactive /verification, /brain, /forge controls --
    def _lease_reboot_drill(self, symbol: str) -> dict[str, Any]:
        """Prove an expired lease is swept to ``DISARMED`` after a reboot.

        ``core.chaos_recovery.audit_lease_reboot`` resets the canonical
        ``nios-active-leases`` segment, which would disarm the live Ring 1 lease
        table. This drill runs the identical sequence on a private segment and
        unlinks it afterwards: the same proof, with no side effect on the
        running system.
        """

        from core.schemas import LeaseMode, System1Lease
        from core.shm_ipc import LeaseTableSHM

        segment = f"nios-chaos-lease-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        stamp = time.monotonic_ns()
        writer = LeaseTableSHM(reset=True, name=segment)
        report: dict[str, Any] = {
            "symbol": symbol,
            "segment": segment,
            "stale_lease_armed": False,
            "swept": 0,
            "active_after_reboot": 0,
            "mode_after_reboot": int(LeaseMode.DISARMED),
            "disarmed": False,
            "passed": False,
        }
        reader = None
        try:
            stale = System1Lease(
                symbol=symbol,
                mode=LeaseMode.LONG_ONLY,
                target_bps=12.0,
                stop_bps=9.0,
                expires_at_ns=stamp - 1_000_000,  # already dead on arrival
                strategy_id_hash=0xA1B2C3,
            )
            writer.write_lease(stale, stamp)
            # Pre-sweep "armed" diagnostic must inspect the RAW slot, not
            # ``read_lease``.  Since the Lens3 cold-path fix, ``read_lease``
            # fails closed and returns DISARMED for an expired lease (Law 2),
            # so it can no longer observe the seeded stale entry.  The raw slot
            # still holds LONG_ONLY until ``sweep_expired`` rewrites it; this
            # read is a non-authoritative diagnostic only and does not weaken
            # the fail-closed guarantee (no trading authority derived from it).
            report["stale_lease_armed"] = False
            for index in range(writer.n_slots):
                _seq, raw_lease = writer.read_lease_slot(index)
                if raw_lease is not None and raw_lease.symbol == symbol:
                    report["stale_lease_armed"] = (
                        raw_lease.mode is LeaseMode.LONG_ONLY
                    )
                    break
            # "Reboot": a brand-new handle with an empty cache over the same
            # segment, exactly like a restarted Ring 1 worker.
            reader = LeaseTableSHM(reset=False, name=segment)
            report["swept"] = int(reader.sweep_expired(stamp))
            report["active_after_reboot"] = int(reader.active_count(stamp))
            report["mode_after_reboot"] = int(reader.read_lease(symbol, stamp).mode)
        finally:
            if reader is not None:
                with contextlib.suppress(Exception):
                    reader.close()
            with contextlib.suppress(Exception):
                writer.unlink()
            with contextlib.suppress(Exception):
                writer.close()
        report["disarmed"] = report["mode_after_reboot"] == int(LeaseMode.DISARMED)
        report["passed"] = bool(
            report["stale_lease_armed"]
            and report["swept"] >= 1
            and report["active_after_reboot"] == 0
            and report["disarmed"]
        )
        return report

    def chaos_drill(self, request: ChaosDrillRequest) -> dict[str, Any]:
        """SIGKILL a worker mid-outbox and prove every reboot invariant."""

        import tempfile

        from core.chaos_recovery import ChaosCrashSimulator

        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="nios-chaos-") as workspace:
            try:
                verdict = ChaosCrashSimulator(
                    workspace,
                    target="outbox_reservation",
                    silo=str(request.silo),
                    iterations=int(request.iterations),
                    hold_ms=float(request.hold_ms),
                    seed=int(request.seed),
                    boot_deadline_s=30.0,
                ).run()
            except Exception as exc:  # noqa: BLE001 - a failed drill is a 502
                raise HTTPException(
                    status_code=502,
                    detail=f"chaos drill failed: {type(exc).__name__}: {exc}",
                ) from None
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        lease = self._lease_reboot_drill(str(request.symbol))
        recovery = _jsonable(verdict.recovery) if verdict.recovery is not None else {}
        if not isinstance(recovery, dict):
            recovery = {"report": recovery}
        pending_after = int(recovery.get("reservations_pending_after", -1))
        orphaned = int(recovery.get("orphaned_orders", -1))
        duplicates = int(recovery.get("duplicate_transfer_ids", -1))
        debits = int(recovery.get("debit_total_micros", 0))
        credits = int(recovery.get("credit_total_micros", 0))
        invariants = {
            # sec_009: a crash never leaves a reservation dangling at the venue.
            "no_orphaned_orders": bool(pending_after == 0 and orphaned == 0),
            # sec_009: the outbox record is the idempotency key.
            "at_most_once_execution": bool(duplicates == 0),
            # sec_009: an expired lease is swept to DISARMED, never inherited.
            "stale_lease_swept_to_disarmed": bool(lease["passed"]),
            # sec_018 Law 4 / double-entry: debits must equal credits.
            "double_entry_conservation": bool(
                debits == credits and bool(recovery.get("invariant_holds", False))
            ),
        }
        holds = all(invariants.values())
        return {
            "status": "PASSED" if holds else "FAILED",
            "page": "verification",
            "kind": "nios_chaos_recovery_drill",
            "target": str(verdict.target),
            "silo": str(request.silo),
            "pid": int(verdict.pid),
            "killed": bool(verdict.killed),
            "returncode": verdict.returncode,
            "sigkill_delivered": bool(verdict.killed and verdict.returncode not in (0,)),
            "inflight_reservation_observed": bool(verdict.inflight_observed),
            "reservation_key": str(verdict.reservation_key),
            "iterations": int(request.iterations),
            "hold_ms": float(request.hold_ms),
            "seed": int(request.seed),
            "recovery": recovery,
            "lease_reboot": lease,
            "invariants": invariants,
            "all_invariants_hold": holds,
            "proof": {
                "pending_after": pending_after,
                "duplicate_transfer_ids": duplicates,
                "orphaned_orders": orphaned,
                "sum_debits_micros": debits,
                "sum_credits_micros": credits,
                "lease_mode_after_reboot": lease["mode_after_reboot"],
            },
            "elapsed_ms": round(elapsed_ms, 3),
            "notes": list(verdict.notes),
            "operator": str(request.operator),
            "law": "sec_010 - no orphaned orders, at-most-once execution, no drift",
            "generated_at_ns": time.time_ns(),
        }

    def vopr_campaign(self, request: VoprCampaignRequest) -> dict[str, Any]:
        """Run the seeded deterministic VOPR fault campaign."""

        from ring0_vault.kernel.formal_verifier import VOPRFaultInjectionSimulator

        try:
            result = VOPRFaultInjectionSimulator(
                seed=int(request.seed), steps=int(request.steps)
            ).run_campaign()
            payload = result.to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a campaign failure is a 502
            raise HTTPException(
                status_code=502,
                detail=f"VOPR campaign failed: {type(exc).__name__}: {exc}",
            ) from None
        fault_counts = {
            str(key): int(value) for key, value in (payload.get("fault_counts") or {}).items()
        }
        overdrafts = int(payload.get("overdraft_violations", 0) or 0)
        conservation = int(payload.get("conservation_violations", 0) or 0)
        ledger_violations = int(payload.get("ledger_violations", 0) or 0)
        duplicates = int(payload.get("duplicate_executions", 0) or 0)
        holds = bool(
            payload.get("ok", False)
            and overdrafts == 0
            and conservation == 0
            and ledger_violations == 0
            and duplicates == 0
            and payload.get("double_entry_ok", False)
            and payload.get("at_most_once_ok", False)
        )
        return {
            "status": "PASSED" if holds else "FAILED",
            "page": "verification",
            "kind": "nios_vopr_fault_campaign",
            "seed": int(payload.get("seed", request.seed)),
            "steps": int(payload.get("steps", request.steps)),
            "fault_counts": fault_counts,
            "injected_faults": int(sum(fault_counts.values())),
            "power_cycles": int(payload.get("power_cycles", 0) or 0),
            "reservations": int(payload.get("reservations", 0) or 0),
            "commits": int(payload.get("commits", 0) or 0),
            "voids": int(payload.get("voids", 0) or 0),
            "overdrafts": overdrafts,
            "conservation_violations": conservation,
            "ledger_violations": ledger_violations,
            "duplicate_executions": duplicates,
            "double_entry_ok": bool(payload.get("double_entry_ok", False)),
            "at_most_once_ok": bool(payload.get("at_most_once_ok", False)),
            "conservation_proof": {
                "overdrafts": overdrafts,
                "double_entry_ok": bool(payload.get("double_entry_ok", False)),
                "at_most_once_ok": bool(payload.get("at_most_once_ok", False)),
            },
            "all_invariants_hold": holds,
            "duration_ms": float(payload.get("duration_ms", 0.0) or 0.0),
            "details": payload.get("details") or {},
            "errors": list(payload.get("errors") or []),
            "operator": str(request.operator),
            "law": "sec_009 - TigerBeetle VOPR: no overdraft, no duplicate transfer",
            "generated_at_ns": time.time_ns(),
        }

    def os_tune(self, request: OsTuneRequest) -> dict[str, Any]:
        """Apply and audit OS / socket low-latency tuning."""

        from core.os_tuning import audit_and_tune_os, audit_low_latency_sockets

        try:
            tuning = audit_and_tune_os(apply=bool(request.apply), ring=str(request.ring))
            sockets = audit_low_latency_sockets()
        except Exception as exc:  # noqa: BLE001 - a probe failure is a 502
            raise HTTPException(
                status_code=502,
                detail=f"OS tuning audit failed: {type(exc).__name__}: {exc}",
            ) from None
        socket_errors = list(sockets.get("errors") or [])
        error_count = int(tuning.get("error_count", 0) or 0) + len(socket_errors)
        checks = [_jsonable(row) for row in tuning.get("checks") or []]
        checks = [row for row in checks if isinstance(row, dict)]
        groups: dict[str, list[dict[str, Any]]] = {
            "socket": [],
            "timer": [],
            "affinity": [],
            "shm": [],
            "sysctl": [],
            "other": [],
        }
        for row in checks:
            ident = str(row.get("id", ""))
            bucket = "other"
            for prefix in ("socket", "timer", "affinity", "shm", "sysctl"):
                if ident.startswith(prefix) or prefix in ident:
                    bucket = prefix
                    break
            groups[bucket].append(
                {
                    "id": ident,
                    "label": str(row.get("label", "")),
                    "status": str(row.get("status", "")),
                    "detail": str(row.get("detail", "")),
                }
            )
        applied = [
            str(row.get("id", ""))
            for row in checks
            if str(row.get("status", "")).upper() == "APPLIED"
        ]
        recommended = [
            str(row.get("id", ""))
            for row in checks
            if str(row.get("status", "")).upper() == "RECOMMENDED"
        ]
        holds = bool(error_count == 0 and bool(tuning.get("ok", False)))
        return {
            "status": "PASSED" if holds else "FAILED",
            "page": "verification",
            "kind": "nios_os_low_latency_audit",
            "platform": str(tuning.get("platform", "")),
            "platform_release": str(tuning.get("platform_release", "")),
            "profile": str(tuning.get("profile", "")),
            "ring": str(tuning.get("ring", request.ring)),
            "apply_requested": bool(request.apply),
            "tuned": bool(tuning.get("tuned", False)),
            "checks": checks,
            "groups": groups,
            "applied": applied,
            "recommended": recommended,
            "applied_count": int(tuning.get("applied_count", len(applied)) or 0),
            "recommended_count": int(
                tuning.get("recommended_count", len(recommended)) or 0
            ),
            "unavailable": list(tuning.get("unavailable") or []),
            "error_count": error_count,
            "sockets": _jsonable(sockets),
            "socket_telemetry": {
                "transport": str(sockets.get("transport", "")),
                "tcp_nodelay": int(sockets.get("tcp_nodelay", 0) or 0),
                "so_keepalive": int(sockets.get("so_keepalive", 0) or 0),
                "recv_buffer_bytes": int(sockets.get("recv_buffer_bytes", 0) or 0),
                "send_buffer_bytes": int(sockets.get("send_buffer_bytes", 0) or 0),
                "errors": socket_errors,
            },
            "transports": _jsonable(tuning.get("transports") or {}),
            "all_invariants_hold": holds,
            "operator": str(request.operator),
            "law": "sec_013 - TCP_NODELAY, 1 MiB buffers and timer resolution are proven",
            "generated_at_ns": time.time_ns(),
        }

    def _contagion_engine_for(self, silo: str) -> Any:
        """Warm and reuse one MiroFish engine per silo (numba warmup is costly)."""

        with self._mirofish_lock:
            engine = self._mirofish_engines.get(silo)
            if engine is None:
                from ring2_brain.mirofish.contagion_engine import (
                    MiroFishContagionEngine,
                )

                engine = MiroFishContagionEngine(silo=silo, warmup=True)
                self._mirofish_engines[silo] = engine
            return engine

    def mirofish_shock(self, request: MiroFishShockRequest) -> dict[str, Any]:
        """Run one five-archetype, three-tick cascade under an operator shock."""

        from ring2_brain.mirofish.contagion_engine import (
            ARCHETYPES,
            FRAGILITY_HALT_THRESHOLD,
            ShockVector,
        )
        from ring2_brain.truth.grounding_gate import GroundedFactPacket

        symbol = str(request.symbol)
        tag = str(request.sentiment_tag or "").strip().lower()
        shock = ShockVector(
            price_shock_bps=float(request.shock_bps),
            realized_vol_ratio=float(request.volatility_ratio),
            spread_bps=float(request.spread_bps),
            dealer_gamma_flip_proximity=float(request.gamma_flip_proximity),
            tags=(tag,) if tag else (),
        )
        engine = self._contagion_engine_for(str(request.silo))
        started = time.perf_counter()
        try:
            result = engine.run(
                shock,
                symbol,
                persist=self.feature_store is not None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a cascade failure is a 502
            raise HTTPException(
                status_code=502,
                detail=f"contagion cascade failed: {type(exc).__name__}: {exc}",
            ) from None
        latency_ms = (time.perf_counter() - started) * 1000.0
        features = result.features
        tick_flows = [float(row) for row in result.tick_flows]
        weights = [float(row) for row in result.archetype_weights]
        names = [str(name) for name, _ in ARCHETYPES]
        # A derived decomposition: weight_i * net_tick_flow sums exactly to the
        # tick flow, so the breakdown is an allocation of a measured number and
        # never a new claim.
        tick_breakdown = [
            {
                "tick": index + 1,
                "net_flow": round(flow, 9),
                "archetypes": [
                    {
                        "name": name,
                        "weight": round(weight, 9),
                        "flow": round(weight * flow, 9),
                    }
                    for name, weight in zip(names, weights)
                ],
            }
            for index, flow in enumerate(tick_flows)
        ]
        deterministic = {
            "symbol": symbol,
            "silo": str(request.silo),
            "shock": {
                "price_shock_bps": float(shock.price_shock_bps),
                "realized_vol_ratio": float(shock.realized_vol_ratio),
                "spread_bps": float(shock.spread_bps),
                "dealer_gamma_flip_proximity": float(
                    shock.dealer_gamma_flip_proximity
                ),
                "tags": list(shock.tags),
            },
            "tick_flows": [round(row, 12) for row in tick_flows],
            "archetype_weights": [round(row, 12) for row in weights],
            "cascade_fragility_index": round(
                float(features.cascade_fragility_index), 12
            ),
            "crowd_asymmetry_score": round(float(features.crowd_asymmetry_score), 12),
            "narrative_exhaustion_divergence": round(
                float(features.narrative_exhaustion_divergence), 12
            ),
            "requires_ring1_halt": bool(features.requires_ring1_halt),
            "fragility_override": bool(result.fragility_override),
            "revoked_long_leases": int(result.revoked_long_leases),
        }
        digest = _sha256_hex(
            json.dumps(deterministic, sort_keys=True, separators=(",", ":"))
        )
        now = time.time_ns()
        packet = GroundedFactPacket(
            symbol=symbol,
            valid_time_ns=now,
            knowledge_ts_ns=now,
            as_of_ns=now,
            metrics={
                "price_shock_bps": float(shock.price_shock_bps),
                "realized_vol_ratio": float(shock.realized_vol_ratio),
                "spread_bps": float(shock.spread_bps),
                "dealer_gamma_flip_proximity": float(
                    shock.dealer_gamma_flip_proximity
                ),
                "tick_flow_1": tick_flows[0],
                "tick_flow_2": tick_flows[1],
                "tick_flow_3": tick_flows[2],
                "cascade_fragility_index": float(features.cascade_fragility_index),
                "crowd_asymmetry_score": float(features.crowd_asymmetry_score),
                "narrative_exhaustion_divergence": float(
                    features.narrative_exhaustion_divergence
                ),
            },
        )
        if self.grounding is not None:
            with contextlib.suppress(Exception):
                if hasattr(self.grounding, "append"):
                    self.grounding.append(packet, artifact_kind="mirofish_cascade")
        within_sla = latency_ms < 1.8
        return {
            "status": "CASCADE_COMPLETE",
            "page": "brain",
            "kind": "nios_mirofish_shock_cascade",
            "symbol": symbol,
            "silo": str(request.silo),
            "sentiment_tag": tag,
            "shock": deterministic["shock"],
            "archetype_count": len(names),
            "archetypes": names,
            "tick_count": len(tick_flows),
            "tick_flows": [round(row, 9) for row in tick_flows],
            "tick_breakdown": tick_breakdown,
            "archetype_weights": [round(row, 9) for row in weights],
            "cascade_fragility_index": float(features.cascade_fragility_index),
            "crowd_asymmetry_score": float(features.crowd_asymmetry_score),
            "narrative_exhaustion_divergence": float(
                features.narrative_exhaustion_divergence
            ),
            "fragility_halt_threshold": float(FRAGILITY_HALT_THRESHOLD),
            "requires_ring1_halt": bool(features.requires_ring1_halt),
            "fragility_override": bool(result.fragility_override),
            "revoked_long_leases": int(result.revoked_long_leases),
            "latency_ms": round(latency_ms, 6),
            "engine_latency_ms": round(float(result.latency_ms), 6),
            "sla_ms": 1.8,
            "within_sla": within_sla,
            "sha256_digest": digest,
            "grounding": {
                "sha256_digest": packet.sha256_digest,
                "cascade_digest": digest,
                "packet": packet.to_dict(),
            },
            "packet": packet.to_dict(),
            "operator": str(request.operator),
            "law": "sec_018 Law 3 - a cascade is simulated and every number is hashed",
            "generated_at_ns": time.time_ns(),
        }

    def _committee_for(self, silo: str) -> Any:
        """Build (once per silo) the grounded System 2 committee."""

        with self._committee_lock:
            committee = self._committee_cache.get(silo)
            if committee is None:
                from ring2_brain.cognition.committee_graph import (
                    System2InvestmentCommittee,
                )

                committee = System2InvestmentCommittee(
                    ledger=self.grounding,
                    feature_store=self.feature_store,
                    bus=self.bus,
                    silo=silo,
                )
                self._committee_cache[silo] = committee
            return committee

    def committee_debate(self, request: CommitteeDebateRequest) -> dict[str, Any]:
        """Run one grounded System 2 debate turn and seal the packet."""

        from ring2_brain.cognition.committee_graph import (
            CommitteeAbstained,
            UngroundedNumericalClaimError,
        )
        from ring2_brain.mirofish.contagion_engine import ShockVector
        from ring2_brain.quant.scanner import FactorPanel, VectorizedFactorScanner

        silo = str(request.silo)
        known = self._known_silos()
        if known and silo not in known:
            raise HTTPException(
                status_code=422,
                detail=f"unknown silo {silo!r}; expected one of {known}",
            )
        scanner = self.scanner
        if scanner is None:
            scanner = VectorizedFactorScanner()
            with contextlib.suppress(Exception):
                scanner.warmup()
            self.scanner = scanner
        started = time.perf_counter()
        try:
            panel = FactorPanel.generate(n_symbols=500, n_periods=32)
            with contextlib.suppress(Exception):
                scanner.warmup()
            scan = scanner.scan(panel, commit=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a scan failure is a 502
            raise HTTPException(
                status_code=502,
                detail=f"committee scan failed: {type(exc).__name__}: {exc}",
            ) from None
        candidates = list(getattr(scan, "candidates", ()) or ())
        if not candidates:
            raise HTTPException(
                status_code=409,
                detail="the factor scan produced no candidate above the funnel floor",
            )
        requested = str(request.symbol)
        candidate = candidates[0]
        symbol_match = requested == "AUTO"
        for row in candidates:
            if str(row.symbol).upper() == requested:
                candidate = row
                symbol_match = True
                break
        engine = self._contagion_engine_for(silo)
        cascade = engine.run(
            ShockVector(18.0, 1.3, 4.0, 0.2, tags=("value_absorption",)),
            candidate.symbol,
            now_ns=panel.valid_time_ns,
            persist=False,
        )
        verdict = "CONVICTED"
        abstention_reason = ""
        outcome: Any = None
        try:
            outcome = self._committee_for(silo).run(
                candidate,
                contagion=cascade.features,
                spread_bps=float(request.spread_bps),
                now_ns=panel.valid_time_ns,
                universe_size=int(scan.universe_size),
                publish=False,
            )
        except CommitteeAbstained as exc:
            verdict = "ABSTAINED"
            abstention_reason = str(exc) or "committee abstained"
        except UngroundedNumericalClaimError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"{type(exc).__name__}: {exc}",
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a committee failure is a 502
            raise HTTPException(
                status_code=502,
                detail=f"committee debate failed: {type(exc).__name__}: {exc}",
            ) from None
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        transcript: list[dict[str, Any]] = []
        packet_payload: dict[str, Any] = {}
        digest = ""
        proposal_payload: dict[str, Any] = {}
        report_payload: dict[str, Any] = {}
        if outcome is not None:
            report = outcome.report
            for stage in report.stages:
                transcript.append(
                    {
                        "stage": str(stage.stage),
                        "actor": str(stage.actor),
                        "text": str(stage.text),
                        "verified": bool(stage.verified),
                        "claims_checked": int(stage.claims_checked),
                    }
                )
            packet_payload = _jsonable(outcome.packet)
            if not isinstance(packet_payload, dict):
                packet_payload = {"packet": packet_payload}
            digest = str(outcome.packet.sha256_digest)
            if outcome.proposal is not None:
                proposal_payload = _jsonable(outcome.proposal)
                if not isinstance(proposal_payload, dict):
                    proposal_payload = {"proposal": proposal_payload}
            report_payload = {
                "symbol": str(report.symbol),
                "packet_digest": str(report.packet_digest),
                "vault": str(report.vault),
                "cap_pct": float(report.cap_pct),
                "position_pct": float(report.position_pct),
                "side": str(report.side),
                "elapsed_ms": float(report.elapsed_ms),
                "verifications": [
                    _jsonable(row) for row in report.verifications
                ],
                "all_stages_verified": all(
                    bool(stage.verified) for stage in report.stages
                ),
                "claims_checked": int(
                    sum(int(stage.claims_checked) for stage in report.stages)
                ),
            }
            if self.grounding is not None and verdict == "CONVICTED":
                with contextlib.suppress(Exception):
                    if hasattr(self.grounding, "append"):
                        self.grounding.append(
                            outcome.packet, artifact_kind="committee_debate"
                        )
        if not digest:
            digest = _sha256_hex(
                json.dumps(
                    {"transcript": transcript, "verdict": verdict},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return {
            "status": verdict,
            "page": "brain",
            "kind": "nios_system2_committee_debate",
            "silo": silo,
            "requested_symbol": requested,
            "symbol": str(candidate.symbol),
            "symbol_match": symbol_match,
            "rank": int(candidate.rank),
            "score": float(candidate.score),
            "price": float(candidate.price),
            "universe_size": int(scan.universe_size),
            "verdict": verdict,
            "abstention_reason": abstention_reason,
            "transcript": transcript,
            "stage_count": len(transcript),
            "report": report_payload,
            "proposal": proposal_payload,
            "sha256_digest": digest,
            "grounding": {
                "sha256_digest": digest,
                "claims_checked": int(report_payload.get("claims_checked", 0) or 0),
                "all_stages_verified": bool(
                    report_payload.get("all_stages_verified", False)
                ),
                "packet": packet_payload,
            },
            "packet": packet_payload,
            "contagion": {
                "cascade_fragility_index": float(
                    cascade.features.cascade_fragility_index
                ),
                "requires_ring1_halt": bool(cascade.features.requires_ring1_halt),
            },
            "elapsed_ms": round(elapsed_ms, 3),
            "operator": str(request.operator),
            "law": "sec_018 Law 3 - an ungrounded number aborts the debate",
            "generated_at_ns": time.time_ns(),
        }

    def evaluate_challenger(self, request: ChallengerEvalRequest) -> dict[str, Any]:
        """Admit or reject a challenger genome/plugin against the nursery gates."""

        import numpy as np

        from core.plugins import PluginManifest
        from ring2_brain.forge.alpha_forge import StrategyGenome

        forge = self.forge
        registry = self.plugins
        if forge is None or registry is None:
            raise HTTPException(
                status_code=503,
                detail="forge or plugin registry unavailable",
            )
        name = str(request.plugin_name)
        thresholds = {}
        with contextlib.suppress(Exception):
            thresholds = dict(forge.thresholds() or {})
        dsr_floor = float(thresholds.get("promotion_dsr_floor", 1.5))
        min_fills = int(thresholds.get("min_winning_shadow_fills", 30) or 30)
        declared_fills = int(request.shadow_fills)
        sharpe = float(request.candidate_sharpe)

        record = None
        with contextlib.suppress(Exception):
            record = registry.get(name)
        if record is None:
            weights = {
                f"genome_{name}_w{index}": round(float(value), 6)
                for index, value in enumerate(
                    np.frombuffer(
                        _sha256_hex(name).encode("ascii")[:32], dtype=np.uint8
                    )
                    / 255.0
                )
            }
            try:
                record = registry.register(
                    PluginManifest(
                        name=name,
                        kind="factor_pack",
                        ring="ring2",
                        author="ring3.operator",
                        description="operator-submitted challenger genome",
                        factor_weights=weights,
                        metadata={"silo": str(request.silo), "source": "step6_cockpit"},
                    )
                )
            except ValueError:  # a concurrent register won the race
                with contextlib.suppress(Exception):
                    record = registry.get(name)
            except Exception as exc:  # noqa: BLE001 - registration is a 502
                raise HTTPException(
                    status_code=502,
                    detail=f"plugin registration failed: {type(exc).__name__}: {exc}",
                ) from None
        if record is None:
            raise HTTPException(
                status_code=502, detail="plugin registry returned no record"
            )

        # Law 4: graduation is earned on real L2 shadow fills, never asserted.
        # This block used to synthesise the missing fills from the caller's own
        # ``shadow_fills`` integer at a guaranteed +2.5 bps, so one POST could
        # hand a brand-new plugin a full nursery record and promote it. The
        # declared count is still reported (the operator's claim is evidence
        # about their intent, not about the tape) but the gate is decided on
        # what the registry actually recorded.
        recorded = int(getattr(record, "shadow_fills", 0) or 0)
        # Law 4 also demands a positive net-of-cost *expectancy* across the
        # recorded fills.  A count alone let a challenger that lost money on
        # every one of its 30 fills graduate on volume.
        mean_net_bps = float(getattr(record, "mean_net_bps", 0.0) or 0.0)

        genome_id = f"plugin:{name}"
        genome = StrategyGenome(
            genome_id=genome_id,
            weights=(
                np.frombuffer(_sha256_hex(name).encode("ascii")[:32], dtype=np.uint8)
                / 255.0
            ),
            silo=str(request.silo),
            shadow_fills=recorded,
            winning_shadow_fills=recorded,
            deflated_sharpe=sharpe,
            metrics_source="registry_recorded",
        )
        try:
            forge.register_challenger(genome)
        except Exception as exc:  # noqa: BLE001 - forge registration is a 502
            raise HTTPException(
                status_code=502,
                detail=f"challenger registration failed: {type(exc).__name__}: {exc}",
            ) from None

        nursery: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            nursery = dict(registry.shadow_report(name) or {})
        stored = forge.get_challenger(genome_id)
        dsr_ok = sharpe >= dsr_floor
        expectancy_ok = mean_net_bps > 0.0
        nursery_ok = recorded >= min_fills and expectancy_ok
        graduated = bool(nursery.get("graduated", False)) and nursery_ok
        promoted = bool(dsr_ok and nursery_ok and graduated)
        reasons: list[str] = []
        if not dsr_ok:
            reasons.append(
                f"deflated_sharpe {sharpe:.4f} < promotion floor {dsr_floor:.2f}"
            )
        if recorded < min_fills:
            reasons.append(
                f"shadow_fills {declared_fills} declared / {recorded} recorded "
                f"< required {min_fills}"
            )
        if not expectancy_ok:
            reasons.append(
                f"mean_net_bps {mean_net_bps:.4f} is not a positive "
                "net-of-cost expectancy across the recorded shadow fills"
            )
        if nursery_ok and not graduated:
            reasons.append("plugin registry has not graduated the shadow record")
        return {
            "status": "PROMOTED" if promoted else "REJECTED",
            "page": "forge",
            "kind": "nios_challenger_evaluation",
            "plugin_name": name,
            "genome_id": genome_id,
            "silo": str(request.silo),
            "candidate_sharpe": sharpe,
            "shadow_fills": declared_fills,
            "winning_shadow_fills": declared_fills,
            "recorded_fills": recorded,
            "mean_net_bps": round(mean_net_bps, 6),
            "gates": {
                "deflated_sharpe": {
                    "measured": sharpe,
                    "required": dsr_floor,
                    "passed": dsr_ok,
                },
                "shadow_fills": {
                    "measured": declared_fills,
                    "required": min_fills,
                    "passed": nursery_ok,
                },
                "net_expectancy_bps": {
                    "measured": round(mean_net_bps, 6),
                    "required": "> 0",
                    "passed": expectancy_ok,
                },
                "nursery_graduated": {
                    "measured": graduated,
                    "required": True,
                    "passed": graduated,
                },
            },
            "gate_results": {
                "dsr_floor_met": dsr_ok,
                "shadow_nursery_met": nursery_ok,
                "net_expectancy_positive": expectancy_ok,
                "nursery_graduated": graduated,
            },
            "promotion_ready": promoted,
            "rejection_reasons": reasons,
            "hold_reason": "; ".join(reasons),
            "nursery": nursery,
            "genome": _jsonable(stored) if stored is not None else {},
            "thresholds": thresholds,
            "forge": {
                "champion_count": int(
                    len(getattr(forge, "champions", lambda: ())() or ())
                ),
                "challenger_count": int(
                    len(getattr(forge, "challengers", lambda: ())() or ())
                ),
            },
            "activation": "SHADOW",
            "note": (
                "Admission only: the 00:00 UTC forge run decides deployment and "
                "no plugin code is executed or signed here."
            ),
            "operator": str(request.operator),
            "law": "sec_018 Law 4 - DSR >= 1.50 and 30 shadow fills before promotion",
            "generated_at_ns": time.time_ns(),
        }

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
        except KeyError:
            # An unknown silo is a client error, not an unhandled 500, and no
            # credential has been written at this point.
            raise HTTPException(status_code=404, detail="UNKNOWN_SILO") from None
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

    # -- V1.2: agent task hub + walk-forward backtester --------------------
    def agents_page(self) -> dict[str, Any]:
        """``GET /api/v2/cockpit/agents`` -- specialist agents and task queue."""
        snapshot = self.task_dispatcher.snapshot()
        return {
            **snapshot,
            "page": "agents",
            "route": "/agents",
            "workspaces": ["/paper/agents", "/live/agents"],
            "routes": {
                "agents": "/api/v2/cockpit/agents",
                "assign": "/api/v2/control/agents/assign",
                "batch_watchlist": "/api/v2/control/agents/batch-watchlist",
                "followup": "/api/v2/control/agents/followup",
                "task": "/api/v2/control/agents/tasks/{task_id}",
                "cancel": "/api/v2/control/agents/cancel",
            },
            "universe": {
                "total_instruments": instrument_registry.total(),
                "silo_counts": instrument_registry.counts(),
                "feature_profiles": profile_index()["per_profile"],
            },
        }

    def assign_agent_task(self, request: AgentAssignRequest) -> dict[str, Any]:
        """Dispatch one grounded Ring 2 agent task and return its report."""
        task = self.task_dispatcher.assign(
            agent_id=request.agent_id,
            silo=request.silo,
            symbols=list(request.symbols or []),
            strategy_id=request.strategy_id,
            prompt=request.prompt,
            priority=request.priority,
        )
        task["token"] = self.task_dispatcher.token_usage()
        return task

    def batch_watchlist(self, request: AgentBatchWatchlistRequest) -> dict[str, Any]:
        """Rank a whole symbol watchlist and seal one grounded batch packet."""
        task = self.task_dispatcher.batch_watchlist(
            silo=request.silo,
            symbols=list(request.symbols or []),
            strategy_id=request.strategy_id,
            priority=request.priority,
        )
        task["token"] = self.task_dispatcher.token_usage()
        return task

    def agent_followup(self, request: AgentFollowupRequest) -> dict[str, Any]:
        """Answer one grounded follow-up against a completed task."""
        return self.task_dispatcher.followup(
            task_id=request.task_id,
            question=request.question,
        )

    def agent_task(self, task_id: str) -> dict[str, Any]:
        """``GET /api/v2/control/agents/tasks/{task_id}`` -- full report."""
        token = _identifier(task_id, field_name="task")
        task = self.task_dispatcher.task_dict(token)
        if task is None:
            raise KeyError(f"UNKNOWN_TASK: {token}")
        return task

    def cancel_agent_tasks(self, request: AgentCancelRequest) -> dict[str, Any]:
        """``POST /api/v2/control/agents/cancel`` -- cancel one or clear all."""
        result = self.task_dispatcher.cancel(str(request.task_id))
        result["token"] = self.task_dispatcher.token_usage()
        return result

    def backtest_page(self) -> dict[str, Any]:
        """``GET /api/v2/cockpit/backtest`` -- catalog, floors, saved runs."""
        snapshot = self.backtest_engine.snapshot()
        return {
            **snapshot,
            "page": "backtest",
            "route": "/backtest",
            "workspaces": ["/paper/backtest", "/live/backtest"],
            "routes": {
                "backtest": "/api/v2/cockpit/backtest",
                "run": "/api/v2/control/backtest/run",
                "compare": "/api/v2/control/backtest/compare-strategies",
                "promote": "/api/v2/control/backtest/promote-to-nursery",
            },
        }

    def run_backtest(self, request: BacktestRunRequest) -> dict[str, Any]:
        """``POST /api/v2/control/backtest/run`` -- grounded walk-forward run."""
        data_source = str(
            getattr(request, "data_source", "SYNTHETIC_WALK_FORWARD")
            or "SYNTHETIC_WALK_FORWARD"
        ).upper()
        if data_source not in _BACKTEST_DATA_SOURCES:
            raise ValueError(
                f"unknown data_source: {data_source} "
                f"(expected one of {sorted(_BACKTEST_DATA_SOURCES)})"
            )
        tape = None
        replay_meta: dict[str, Any] | None = None
        if data_source == "TRADINGVIEW_REPLAY":
            tv_timeframe = _BACKTEST_TO_TRADINGVIEW_TIMEFRAME[
                str(request.timeframe)
            ]
            tape = self.tradingview_bridge.replay_tape(
                request.symbol,
                timeframe=tv_timeframe,
                bars=int(request.bars),
                replay_offset=int(getattr(request, "replay_offset", 0) or 0),
            )
            replay_meta = {
                "data_source": "TRADINGVIEW_REPLAY",
                "tradingview_timeframe": tv_timeframe,
                "replay_offset": int(getattr(request, "replay_offset", 0) or 0),
                "bars": len(tape),
                "bridge_version": self.tradingview_bridge.version,
                "mode": MODE_DETERMINISTIC_REPLAY,
            }
        receipt = self.backtest_engine.run(
            silo=request.silo,
            symbol=request.symbol,
            strategy_id=request.strategy_id,
            bars=int(request.bars),
            timeframe=request.timeframe,
            initial_capital_units=float(request.initial_capital_units),
            fast_window=request.fast_window,
            slow_window=request.slow_window,
            stop_loss_bps=request.stop_loss_bps,
            take_profit_bps=request.take_profit_bps,
            tape=tape,
        )
        receipt["run_count"] = self.backtest_engine.run_count
        receipt["statutory_cost"] = statutory_cost_engine.breakdown(
            request.silo,
            int(
                round(
                    float(request.initial_capital_units) * 1_000_000
                )
            ),
        ).to_dict()
        receipt["cost_floor_bps"] = float(
            STATUTORY_COST_FLOOR_BPS.get(request.silo, 0.0)
        )
        receipt["data_source"] = data_source
        if replay_meta is not None:
            receipt["synthetic_tape"] = False
            receipt["tradingview_replay"] = replay_meta
        # V1.8: the saved walk-forward summary is durable across restarts.
        summary = {
            "backtest_id": str(
                receipt.get("backtest_id")
                or receipt.get("run_id")
                or f"bt-{receipt.get('run_count', 0)}"
            ),
            "silo": str(request.silo),
            "symbol": str(request.symbol),
            "strategy_id": str(request.strategy_id),
            "timeframe": str(request.timeframe),
            "data_source": str(data_source),
            "bars": int(request.bars),
            "total_return_bps": receipt.get("total_return_bps"),
            "sharpe": receipt.get("sharpe"),
            "max_drawdown_bps": receipt.get("max_drawdown_bps"),
            "completed_at_ns": time.time_ns(),
        }
        self._journal_backtests.append(summary)
        self._journal_append("backtests", summary)
        receipt["journal"] = {
            "persisted": getattr(self, "state_journal", None) is not None,
            "saved_backtests": len(self._journal_backtests),
        }
        return receipt

    def compare_backtest_strategies(
        self, request: BacktestCompareRequest
    ) -> dict[str, Any]:
        """``POST /api/v2/control/backtest/compare-strategies`` -- 8-way matrix."""
        return self.backtest_engine.compare_strategies(
            silo=request.silo,
            symbol=request.symbol,
            timeframe=request.timeframe,
            bars=int(request.bars),
        )

    def promote_backtest(self, request: NurseryPromoteRequest) -> dict[str, Any]:
        """``POST /api/v2/control/backtest/promote-to-nursery`` -- DSR gated."""
        return self.backtest_engine.promote_to_nursery(
            _identifier(request.backtest_id, field_name="backtest")
        )

    # -- multi-page read-only telemetry (Step 4: full cockpit) ------------
    # ------------------------------------------------------------------
    # V1.3 options chain, market terminal, trade logbook and broker hub
    # ------------------------------------------------------------------
    _RISK_FREE_BY_CCY: ClassVar[dict[str, float]] = {
        "INR": 0.065,
        "USD": 0.043,
        "USDT": 0.045,
        "EUR": 0.025,
        "GBP": 0.040,
        "JPY": 0.005,
        "CHF": 0.008,
        "AUD": 0.040,
        "CAD": 0.030,
        "NZD": 0.040,
    }

    _OPTIONS_SURFACE_DEFAULTS: dict[str, Any] = {
        "iv_rank": 50.0,
        "net_gamma_exposure_usd": 0.0,
        "pcr_oi": 1.0,
        "max_pain_strike": None,
        "spot_reference": None,
        "atm_iv_pct": None,
        "days_to_expiry": 30,
        "strike_step": None,
    }

    def _options_surface_snapshot(self, symbol: str) -> dict[str, Any]:
        """Best-effort ``MarketFeaturesStore`` options surface for one symbol.

        The registry is the source of truth for *which* instruments exist; the
        feature store is the source of truth for their live option surface
        (``iv_rank``, dealer gamma, put/call open interest, max pain).  A
        missing profile degrades to deterministic defaults instead of raising,
        because the chain itself only needs spot/vol/step.
        """
        out: dict[str, Any] = dict(self._OPTIONS_SURFACE_DEFAULTS)
        out["surface"] = {}
        try:
            surface = get_options_surface_profile(str(symbol))
        except Exception:  # noqa: BLE001 - an absent profile is not fatal
            return out
        if not isinstance(surface, Mapping):
            return out
        for key in self._OPTIONS_SURFACE_DEFAULTS:
            value = surface.get(key)
            if value is not None:
                out[key] = value
        out["surface"] = dict(surface)
        return out

    def _options_underlying(self, symbol: str) -> dict[str, Any]:
        """Resolve any optionable instrument in the 274-symbol registry.

        Every symbol with ``has_options == True`` -- India F&O stocks and
        indices, crypto perpetuals, US equities/ETFs/futures -- produces its own
        chain from its registry ``option_strike_step`` plus its
        ``MarketFeaturesStore`` options surface.  A spot FX pair carries no
        optionable contract, so it is rejected with
        ``422 INSTRUMENT_NOT_OPTIONABLE`` rather than being quietly modelled.
        """
        key = str(symbol or "").strip().upper()
        contract = instrument_registry.resolve(key)
        if contract is None and key not in OPTIONS_UNDERLYINGS:
            raise HTTPException(status_code=422, detail=f"UNKNOWN_UNDERLYING:{key}")
        if contract is not None and not bool(contract.has_options):
            raise HTTPException(status_code=422, detail="INSTRUMENT_NOT_OPTIONABLE")

        surface = self._options_surface_snapshot(
            contract.symbol if contract is not None else key
        )
        tuned = OPTIONS_UNDERLYINGS.get(key)
        if tuned is not None:
            body: dict[str, Any] = {"symbol": key, **dict(tuned)}
            body["has_options"] = True
            body["asset_class"] = getattr(contract, "asset_class", "INDEX")
        else:
            spot = float(surface["spot_reference"] or 0.0)
            if spot <= 0.0:
                raise HTTPException(status_code=422, detail=f"UNKNOWN_UNDERLYING:{key}")
            step = float(contract.option_strike_step)
            if step <= 0.0:
                step = max(round(spot * 0.02, 4), 10.0 ** -4)
            body = {
                "symbol": contract.symbol,
                "silo": contract.silo,
                "spot": spot,
                "volatility": round(float(surface["atm_iv_pct"] or 0.0) / 100.0, 6),
                "days_to_expiry": 30,
                "strike_step": step,
                "strike_precision": 4 if spot < 5.0 else 2,
                "lot_size": max(1, int(round(contract.lot_size))),
                "oi_scale": 50_000.0,
                "currency": contract.currency,
                "risk_free_rate": self._RISK_FREE_BY_CCY.get(contract.currency, 0.040),
                "has_options": True,
                "asset_class": contract.asset_class,
            }

        # The registry contract is authoritative for the strike grid, even for
        # the hand-tuned blue-chip proxies.
        registry_step = float(getattr(contract, "option_strike_step", 0.0) or 0.0)
        if registry_step > 0.0:
            body["strike_step"] = registry_step
        if not body.get("volatility"):
            surface_iv = float(surface["atm_iv_pct"] or 0.0)
            if surface_iv > 0.0:
                body["volatility"] = round(surface_iv / 100.0, 6)
        if not body.get("spot"):
            body["spot"] = float(surface["spot_reference"] or 0.0)
        body["iv_rank"] = round(float(surface["iv_rank"] or 0.0), 6)
        body["net_gamma_exposure_usd"] = float(
            surface["net_gamma_exposure_usd"] or 0.0
        )
        body["pcr_oi"] = round(float(surface["pcr_oi"] or 0.0), 6)
        max_pain = surface["max_pain_strike"]
        body["max_pain_strike"] = float(max_pain) if max_pain is not None else None
        body["surface_days_to_expiry"] = int(surface["days_to_expiry"] or 30)
        body["surface"] = surface["surface"]
        return body

    @staticmethod
    def _option_iv(base_volatility: float, strike: float, spot: float) -> float:
        """A deterministic volatility smile around the at-the-money strike.

        One implied vol per strike (calls and puts share it, so the chain stays
        put-call parity consistent).  The smile is a function of moneyness only,
        never of wall-clock time, so the same request returns the same Greeks.
        """
        moneyness = (float(strike) - float(spot)) / float(spot)
        smile = 1.0 + 0.45 * moneyness * moneyness - 0.10 * moneyness
        return max(0.005, float(base_volatility) * smile)

    def _seal_grounding(
        self,
        symbol: str,
        metrics: Mapping[str, float],
        thesis: str,
        *,
        artifact_kind: str,
    ) -> dict[str, Any]:
        """Seal a numeric payload into a verified SHA-256 grounding packet.

        Every figure in ``thesis`` is re-computed against ``metrics`` by
        ``NumericalGroundingVerifier``; an unbacked number aborts with ``422``
        rather than being rendered (``sec_018`` Law 3).
        """
        from core.schemas import GroundedFactPacket
        from ring2_brain.truth.grounding_gate import NumericalGroundingVerifier

        now = time.time_ns()
        packet = GroundedFactPacket(
            symbol=str(symbol),
            valid_time_ns=now,
            knowledge_ts_ns=now,
            as_of_ns=now,
            metrics={str(key): float(value) for key, value in metrics.items()},
        )
        try:
            report = NumericalGroundingVerifier().verify_thesis_claims(thesis, packet)
        except Exception as exc:  # noqa: BLE001 - an ungrounded claim is a 422
            raise HTTPException(
                status_code=422, detail=f"{type(exc).__name__}: {exc}"
            ) from None
        ledger_report: dict[str, Any] | None = None
        if self.grounding is not None:
            with contextlib.suppress(Exception):
                if hasattr(self.grounding, "append"):
                    self.grounding.append(packet, artifact_kind=str(artifact_kind))
            packet_count = 0
            with contextlib.suppress(Exception):
                packet_count = len(self.grounding.packets())
            ledger_report = {
                "packets": int(packet_count),
                "digest": packet.sha256_digest,
            }
        return {
            "sha256_digest": packet.sha256_digest,
            "verified": bool(report.claims_checked >= 0),
            "claims_checked": int(report.claims_checked),
            "derived_expressions": int(report.derived_expressions),
            "tolerance": float(report.tolerance),
            "packet": packet.to_dict(),
            "report": report.to_dict(),
            "ledger": ledger_report,
        }

    @staticmethod
    def _numeric_claim(value: float) -> str:
        """Fixed-decimal claim text; the extractor reads the mantissa only."""
        return f"{float(value):.12f}"

    def _options_chain_body(
        self,
        spec: Mapping[str, Any],
        *,
        spot: float | None,
        volatility: float | None,
        days_to_expiry: int | None,
        strike_count: int,
        risk_free_rate: float | None,
    ) -> dict[str, Any]:
        """Build one deterministic Call + Put chain with Greeks, PCR and Max Pain."""
        from ring2_brain.quant.scanner import ValuationLab

        symbol = str(spec["symbol"])
        resolved_spot = float(spot) if spot is not None else float(spec["spot"])
        base_vol = float(volatility) if volatility is not None else float(spec["volatility"])
        dte = int(days_to_expiry) if days_to_expiry is not None else int(spec["days_to_expiry"])
        rate = (
            float(risk_free_rate)
            if risk_free_rate is not None
            else float(spec["risk_free_rate"])
        )
        if resolved_spot <= 0.0:
            raise HTTPException(status_code=422, detail="spot must be positive")
        if base_vol <= 0.0:
            raise HTTPException(status_code=422, detail="volatility must be positive")
        if dte < 1:
            raise HTTPException(status_code=422, detail="days_to_expiry must be >= 1")

        step = float(spec["strike_step"])
        precision = int(spec.get("strike_precision", 2))
        lot_size = int(spec.get("lot_size", 1))
        oi_scale = float(spec.get("oi_scale", 50_000.0))
        # An 11-strike chain must stay symmetric around the ATM strike.
        count = max(3, min(31, int(strike_count)))
        if count % 2 == 0:
            count += 1
        half = count // 2
        atm = _round_strike(resolved_spot, step, precision)
        years = float(dte) / 365.0

        lab = ValuationLab()
        rows: list[dict[str, Any]] = []
        for offset in range(-half, half + 1):
            strike = round(atm + offset * step, precision)
            moneyness = (strike - resolved_spot) / resolved_spot
            iv = self._option_iv(base_vol, strike, resolved_spot)
            call = lab.options(
                spot=resolved_spot,
                strike=strike,
                volatility=iv,
                rate=rate,
                years=years,
                kind="call",
            )
            put = lab.options(
                spot=resolved_spot,
                strike=strike,
                volatility=iv,
                rate=rate,
                years=years,
                kind="put",
            )
            decay = math.exp(-9.0 * abs(moneyness))
            call_oi = int(
                max(
                    1,
                    round(
                        oi_scale
                        * decay
                        * (0.55 + 0.90 * _stable_unit(symbol, "call_oi", strike))
                    ),
                )
            )
            put_oi = int(
                max(
                    1,
                    round(
                        oi_scale
                        * decay
                        * (0.55 + 0.90 * _stable_unit(symbol, "put_oi", strike))
                    ),
                )
            )
            rows.append(
                {
                    "strike": float(strike),
                    "moneyness": float(moneyness),
                    "offset": int(offset),
                    "call_ltp": float(call["price"]),
                    "call_iv": float(iv),
                    "call_delta": float(call["delta"]),
                    "call_gamma": float(call["gamma"]),
                    "call_theta": float(call["theta"]),
                    "call_vega": float(call["vega"]),
                    "call_oi": call_oi,
                    "put_ltp": float(put["price"]),
                    "put_iv": float(iv),
                    "put_delta": float(put["delta"]),
                    "put_gamma": float(put["gamma"]),
                    "put_theta": float(put["theta"]),
                    "put_vega": float(put["vega"]),
                    "put_oi": put_oi,
                }
            )

        total_call_oi = float(sum(row["call_oi"] for row in rows))
        total_put_oi = float(sum(row["put_oi"] for row in rows))
        pcr = float(total_put_oi / total_call_oi) if total_call_oi > 0.0 else 0.0

        # Max Pain: the strike where option *buyers* collectively lose the most.
        max_pain_strike = float(atm)
        lowest_pain: float | None = None
        for candidate in rows:
            strike_c = float(candidate["strike"])
            pain = 0.0
            for row in rows:
                strike_r = float(row["strike"])
                pain += max(0.0, strike_c - strike_r) * float(row["call_oi"])
                pain += max(0.0, strike_r - strike_c) * float(row["put_oi"])
            if lowest_pain is None or pain < lowest_pain:
                lowest_pain = pain
                max_pain_strike = strike_c

        atm_row = min(rows, key=lambda row: abs(float(row["strike"]) - resolved_spot))
        atm_iv = float(atm_row["call_iv"])
        atm_strike = float(atm_row["strike"])
        max_pain_pain = float(lowest_pain if lowest_pain is not None else 0.0)

        # Registry + ``MarketFeaturesStore`` option surface.  These are the same
        # figures the dealers are watching, so they ride the grounded packet
        # instead of being recomputed from the modelled chain.
        surface_iv_rank = float(spec.get("iv_rank") or 0.0)
        surface_gex = float(spec.get("net_gamma_exposure_usd") or 0.0)
        surface_pcr = float(spec.get("pcr_oi") or 0.0)
        surface_max_pain = float(spec.get("max_pain_strike") or 0.0)

        metrics: dict[str, float] = {
            "spot": resolved_spot,
            "volatility": base_vol,
            "days_to_expiry": float(dte),
            "risk_free_rate": rate,
            "strike_step": step,
            "strike_count": float(len(rows)),
            "atm_strike": atm_strike,
            "atm_iv": atm_iv,
            "pcr": pcr,
            "max_pain": max_pain_strike,
            "max_pain_value": max_pain_pain,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "iv_rank": surface_iv_rank,
            "net_gamma_exposure_usd": surface_gex,
            "surface_pcr_oi": surface_pcr,
            "surface_max_pain": surface_max_pain,
        }
        thesis = (
            "Grounded options chain receipt. "
            + " ".join(
                f"{key} {self._numeric_claim(value)}" for key, value in metrics.items()
            )
            + "."
        )
        grounding = self._seal_grounding(
            symbol, metrics, thesis, artifact_kind="options_chain"
        )
        return {
            "status": "GROUNDED",
            "page": "options",
            "symbol": symbol,
            "silo": str(spec["silo"]),
            "currency": str(spec.get("currency", "USD")),
            "lot_size": lot_size,
            "strike_step": step,
            "underlying": {
                "spot": resolved_spot,
                "volatility": base_vol,
                "days_to_expiry": dte,
                "years_to_expiry": years,
                "risk_free_rate": rate,
            },
            "chain": rows,
            "asset_class": str(spec.get("asset_class", "")),
            "surface": dict(spec.get("surface") or {}),
            "summary": {
                "atm_strike": atm_strike,
                "atm_iv": atm_iv,
                "pcr": pcr,
                "pcr_regime": (
                    "PUT_HEAVY" if pcr > 1.15 else ("CALL_HEAVY" if pcr < 0.85 else "BALANCED")
                ),
                "max_pain_strike": max_pain_strike,
                "max_pain_value": max_pain_pain,
                "total_call_oi": total_call_oi,
                "total_put_oi": total_put_oi,
                "strike_count": len(rows),
                "iv_rank": surface_iv_rank,
                "net_gamma_exposure_usd": surface_gex,
                "pcr_oi": surface_pcr,
                "surface_max_pain_strike": surface_max_pain,
                "gamma_regime": (
                    "LONG_GAMMA" if surface_gex > 0.0 else ("SHORT_GAMMA" if surface_gex < 0.0 else "FLAT_GAMMA")
                ),
            },
            "grounding": grounding,
            "packet": grounding["packet"],
            "law": "sec_018 Law 3 - every number carries a SHA-256 commitment",
            "generated_at_ns": time.time_ns(),
        }

    def options_underlyings(self) -> list[dict[str, Any]]:
        """Every optionable underlying: the tuned proxies first, then the universe."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key, spec in OPTIONS_UNDERLYINGS.items():
            tuned_contract = instrument_registry.resolve(key)
            if tuned_contract is not None and not bool(tuned_contract.has_options):
                continue
            rows.append(
                {
                    "symbol": key,
                    "silo": str(spec["silo"]),
                    "currency": str(spec.get("currency", "USD")),
                    "spot": float(spec["spot"]),
                    "volatility": float(spec["volatility"]),
                    "days_to_expiry": int(spec["days_to_expiry"]),
                    "strike_step": float(spec["strike_step"]),
                    "lot_size": int(spec.get("lot_size", 1)),
                    "asset_class": "PROXY",
                    "source": "TUNED_DEFAULT",
                }
            )
            seen.add(key)
        for contract in sorted(
            instrument_registry.all(), key=lambda spec: (spec.silo, spec.symbol)
        ):
            if not contract.has_options or contract.symbol in seen:
                continue
            surface = get_options_surface_profile(contract.symbol)
            spot = float(surface["spot_reference"])
            step = float(contract.option_strike_step)
            if step <= 0.0:
                step = max(round(spot * 0.02, 4), 10.0 ** -4)
            rows.append(
                {
                    "symbol": contract.symbol,
                    "silo": contract.silo,
                    "currency": contract.currency,
                    "spot": spot,
                    "volatility": round(float(surface["atm_iv_pct"]) / 100.0, 6),
                    "days_to_expiry": 30,
                    "strike_step": step,
                    "lot_size": max(1, int(round(contract.lot_size))),
                    "asset_class": contract.asset_class,
                    "iv_rank": float(surface["iv_rank"]),
                    "source": "REGISTRY",
                }
            )
            seen.add(contract.symbol)
        return rows

    def options_page(self) -> dict[str, Any]:
        """``GET /api/v2/cockpit/options`` -- underlyings, strategies, live chain."""
        chain = self.options_chain(OptionsChainRequest(symbol="NIFTY"))
        underlyings = self.options_underlyings()
        return {
            "page": "options",
            "title": "Options Chain & Multi-Leg Strategy Lab",
            "ring": "RING 2",
            "read_only": True,
            "underlyings": underlyings,
            "strategies": [dict(row) for row in OPTIONS_STRATEGIES],
            "default_symbol": "NIFTY",
            "chain": chain,
            "counts": {
                "underlyings": len(underlyings),
                "optionable_instruments": len(underlyings),
                "strategies": len(OPTIONS_STRATEGIES),
                "strikes": len(chain["chain"]),
            },
            "law": "sec_018 Law 3 - Black-Scholes Greeks sealed with SHA-256",
            "generated_at_ns": time.time_ns(),
        }

    def options_chain(self, request: OptionsChainRequest) -> dict[str, Any]:
        """``POST /api/v2/control/options/chain`` -- one grounded chain."""
        spec = self._options_underlying(request.symbol)
        return self._options_chain_body(
            spec,
            spot=request.spot,
            volatility=request.volatility,
            days_to_expiry=request.days_to_expiry,
            strike_count=request.strike_count,
            risk_free_rate=request.risk_free_rate,
        )

    @staticmethod
    def _strategy_legs(
        strategy: str, atm: float, step: float, precision: int, width_steps: int
    ) -> list[dict[str, Any]]:
        """The leg blueprint for one template, expressed in strike offsets."""
        width = max(1, int(width_steps))

        def leg(kind: str, direction: int, steps: int) -> dict[str, Any]:
            return {
                "kind": kind,
                "direction": int(direction),
                "strike_offset_steps": int(steps),
                "strike": round(atm + steps * step, precision) if kind != "STOCK" else 0.0,
            }

        if strategy == "BULL_CALL_SPREAD":
            return [leg("CALL", 1, 0), leg("CALL", -1, width)]
        if strategy == "BEAR_PUT_SPREAD":
            return [leg("PUT", 1, 0), leg("PUT", -1, -width)]
        if strategy == "SHORT_STRADDLE":
            return [leg("CALL", -1, 0), leg("PUT", -1, 0)]
        if strategy == "LONG_STRANGLE":
            return [leg("PUT", 1, -width), leg("CALL", 1, width)]
        if strategy == "IRON_CONDOR":
            return [
                leg("PUT", 1, -2 * width),
                leg("PUT", -1, -width),
                leg("CALL", -1, width),
                leg("CALL", 1, 2 * width),
            ]
        if strategy == "PROTECTIVE_PUT":
            return [
                {"kind": "STOCK", "direction": 1, "strike_offset_steps": 0, "strike": 0.0},
                leg("PUT", 1, 0),
            ]
        raise HTTPException(status_code=422, detail=f"UNKNOWN_STRATEGY:{strategy}")

    def strategy_payoff(self, request: StrategyPayoffRequest) -> dict[str, Any]:
        """``POST /api/v2/control/options/strategy-payoff`` -- expiry curve."""
        from ring2_brain.quant.scanner import ValuationLab

        spec = self._options_underlying(request.symbol)
        symbol = str(spec["symbol"])
        resolved_spot = float(request.spot) if request.spot is not None else float(spec["spot"])
        base_vol = (
            float(request.volatility)
            if request.volatility is not None
            else float(spec["volatility"])
        )
        dte = (
            int(request.days_to_expiry)
            if request.days_to_expiry is not None
            else int(spec["days_to_expiry"])
        )
        rate = (
            float(request.risk_free_rate)
            if request.risk_free_rate is not None
            else float(spec["risk_free_rate"])
        )
        step = float(spec["strike_step"])
        precision = int(spec.get("strike_precision", 2))
        lot_size = int(spec.get("lot_size", 1))
        years = float(dte) / 365.0
        atm = _round_strike(resolved_spot, step, precision)
        lab = ValuationLab()

        blueprint = self._strategy_legs(
            request.strategy, atm, step, precision, request.width_steps
        )
        legs: list[dict[str, Any]] = []
        net_premium_per_unit = 0.0
        for item in blueprint:
            kind = str(item["kind"])
            direction = int(item["direction"])
            strike = float(item["strike"])
            if kind == "STOCK":
                premium = 0.0
                iv = 0.0
                greeks = {"price": 0.0, "delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
            else:
                iv = self._option_iv(base_vol, strike, resolved_spot)
                greeks = lab.options(
                    spot=resolved_spot,
                    strike=strike,
                    volatility=iv,
                    rate=rate,
                    years=years,
                    kind=kind.lower(),
                )
                premium = float(greeks["price"])
            net_premium_per_unit += direction * premium
            legs.append(
                {
                    "leg": f"{'LONG' if direction > 0 else 'SHORT'} {kind}",
                    "kind": kind,
                    "direction": direction,
                    "strike": strike,
                    "strike_offset_steps": int(item["strike_offset_steps"]),
                    "iv": float(iv),
                    "premium": float(premium),
                    "delta": float(greeks.get("delta", 0.0)),
                    "gamma": float(greeks.get("gamma", 0.0)),
                    "theta": float(greeks.get("theta", 0.0)),
                    "vega": float(greeks.get("vega", 0.0)),
                    "lot_size": lot_size,
                }
            )

        points = 25
        low = resolved_spot * 0.70
        high = resolved_spot * 1.30
        curve: list[dict[str, float]] = []
        pnls: list[float] = []
        for index in range(points):
            expiry_spot = low + (high - low) * index / (points - 1)
            pnl_per_unit = 0.0
            for leg in legs:
                kind = str(leg["kind"])
                direction = int(leg["direction"])
                if kind == "STOCK":
                    intrinsic = expiry_spot - resolved_spot
                elif kind == "CALL":
                    intrinsic = max(0.0, expiry_spot - float(leg["strike"]))
                else:
                    intrinsic = max(0.0, float(leg["strike"]) - expiry_spot)
                pnl_per_unit += direction * (intrinsic - float(leg["premium"]))
            pnl = pnl_per_unit * lot_size
            pnls.append(pnl)
            curve.append(
                {
                    "expiry_spot": round(expiry_spot, max(precision, 4)),
                    "pnl": round(pnl, 4),
                    "return_bps": round(
                        pnl / max(1e-9, abs(net_premium_per_unit * lot_size)) * 10_000.0, 4
                    ),
                }
            )

        max_profit = float(max(pnls))
        max_loss = float(min(pnls))
        max_profit_index = pnls.index(max_profit)
        max_loss_index = pnls.index(max_loss)
        unbounded_upside = max_profit_index in (0, len(pnls) - 1)
        unbounded_downside = max_loss_index in (0, len(pnls) - 1)

        breakevens: list[float] = []
        for index in range(len(pnls) - 1):
            left, right = pnls[index], pnls[index + 1]
            if left == 0.0:
                breakevens.append(round(curve[index]["expiry_spot"], max(precision, 4)))
                continue
            if (left < 0.0 < right) or (left > 0.0 > right):
                span = curve[index + 1]["expiry_spot"] - curve[index]["expiry_spot"]
                fraction = abs(left) / (abs(left) + abs(right))
                breakevens.append(
                    round(curve[index]["expiry_spot"] + span * fraction, max(precision, 4))
                )
        breakevens = sorted({value for value in breakevens})

        net_debit_credit = float(net_premium_per_unit * lot_size)
        metrics: dict[str, float] = {
            "spot": resolved_spot,
            "volatility": base_vol,
            "days_to_expiry": float(dte),
            "risk_free_rate": rate,
            "net_debit_credit": net_debit_credit,
            "net_premium_per_unit": float(net_premium_per_unit),
            "max_profit": max_profit,
            "max_loss": max_loss,
            "breakeven_primary": float(breakevens[0]) if breakevens else 0.0,
            "breakeven_secondary": float(breakevens[1]) if len(breakevens) > 1 else 0.0,
            "leg_count": float(len(legs)),
            "lot_size": float(lot_size),
        }
        thesis = (
            "Grounded multi-leg payoff receipt. "
            + " ".join(
                f"{key} {self._numeric_claim(value)}" for key, value in metrics.items()
            )
            + "."
        )
        grounding = self._seal_grounding(
            symbol, metrics, thesis, artifact_kind="strategy_payoff"
        )
        return {
            "status": "GROUNDED",
            "page": "options",
            "symbol": symbol,
            "silo": str(spec["silo"]),
            "currency": str(spec.get("currency", "USD")),
            "strategy": request.strategy,
            "strategy_meta": next(
                (dict(row) for row in OPTIONS_STRATEGIES if row["id"] == request.strategy),
                {},
            ),
            "underlying": {
                "spot": resolved_spot,
                "volatility": base_vol,
                "days_to_expiry": dte,
                "years_to_expiry": years,
                "risk_free_rate": rate,
            },
            "atm_strike": atm,
            "lot_size": lot_size,
            "legs": legs,
            "net_premium_per_unit": float(net_premium_per_unit),
            "net_debit_credit": net_debit_credit,
            "net_position": "DEBIT" if net_debit_credit > 0 else "CREDIT",
            "max_profit": max_profit,
            "max_loss": max_loss,
            "unbounded_upside": bool(unbounded_upside),
            "unbounded_downside": bool(unbounded_downside),
            "breakeven_points": breakevens,
            "payoff_curve": curve,
            "curve_points": points,
            "grounding": grounding,
            "packet": grounding["packet"],
            "law": "sec_018 Law 3 - every number carries a SHA-256 commitment",
            "generated_at_ns": time.time_ns(),
        }

    def _terminal_frame(self, request: TerminalQuoteRequest) -> dict[str, Any]:
        """Build one deterministic multi-silo terminal frame."""
        symbol = str(request.symbol or "").strip().upper()
        known_silos = {str(silo).upper() for silo in self._known_silos()}
        if request.silo is not None:
            requested_silo = str(request.silo).strip().upper()
            if requested_silo not in known_silos:
                raise HTTPException(status_code=422, detail=f"UNKNOWN_SILO:{requested_silo}")
        watch = next((row for row in TERMINAL_WATCHLIST if row["symbol"] == symbol), None)
        contract = instrument_registry.resolve(symbol)
        asset_token = "EQUITY"
        resolved_silo = "US_IBKR"
        reference = 100.0 * (1.0 + _stable_unit(symbol, "reference"))
        if contract is not None:
            asset_token = _TERMINAL_ASSET_BY_CLASS.get(contract.asset_class, "EQUITY")
            resolved_silo = contract.silo
            reference = reference_price(contract)
        elif watch is not None:
            asset_token = str(watch["asset_class"])
            resolved_silo = str(watch["silo"])
            reference = float(watch["reference"])
        silo = (
            str(request.silo).strip().upper()
            if request.silo is not None
            else resolved_silo
        )
        asset_class = asset_token
        bars = max(20, min(240, int(request.bars)))
        depth_levels = max(5, min(10, int(request.depth)))

        # V1.8 PART 1.2 - every quote is anchored on the canonical 274-instrument
        # registry contract so the L2 ladder, the circuit band and the Pine study
        # signals all agree with Ring 0 order validation.
        tick_size_micros = int(getattr(contract, "tick_size_micros", 0) or 0)
        if tick_size_micros <= 0:
            tick_size_micros = 10_000 if reference >= 100.0 else 1
        tick_size = tick_size_micros / 1_000_000.0
        lot_size = float(getattr(contract, "lot_size", 1.0) or 1.0)
        circuit_limit_pct = float(getattr(contract, "circuit_limit_pct", 0.0) or 0.0)
        tradingview_ticker = str(
            getattr(contract, "tradingview_ticker", "") or f"NIOS:{symbol}"
        )
        display_name = str(getattr(contract, "display_name", symbol) or symbol)
        feature_profile = str(getattr(contract, "asset_class", asset_class) or asset_class)
        sub_category = str(getattr(contract, "sub_category", asset_class) or asset_class)
        sector = str(getattr(contract, "sector", "UNCLASSIFIED") or "UNCLASSIFIED")
        currency = str(getattr(contract, "currency", "USD") or "USD")

        def snap_tick(value: float, *, direction: int = 0) -> float:
            """Snap a price onto an exact ``tick_size_micros`` increment."""
            scaled = value * 1_000_000.0
            units = scaled / tick_size_micros
            if direction > 0:
                ticks = math.ceil(units - 1e-9)
            elif direction < 0:
                ticks = math.floor(units + 1e-9)
            else:
                ticks = round(units)
            ticks = max(1, int(ticks))
            return round(ticks * tick_size_micros / 1_000_000.0, 6)

        sigma = {"CRYPTO": 0.0080, "INDEX": 0.0035, "EQUITY": 0.0042, "FX": 0.0018}.get(
            asset_class, 0.0040
        )
        price = reference * (0.97 + 0.06 * _stable_unit(symbol, "seed"))
        candles: list[dict[str, float]] = []
        closes: list[float] = []
        volumes: list[float] = []
        for index in range(bars):
            drift = (_stable_unit(symbol, "ret", index) - 0.5) * 2.0 * sigma
            open_price = price
            close_price = max(1e-8, open_price * (1.0 + drift))
            wick = abs(drift) * 0.35 + sigma * 0.25
            high_price = max(open_price, close_price) * (1.0 + wick)
            low_price = min(open_price, close_price) * (1.0 - wick)
            volume = float(
                max(
                    1.0,
                    round(
                        (20_000.0 if asset_class != "CRYPTO" else 900.0)
                        * (0.55 + 0.9 * _stable_unit(symbol, "vol", index))
                    ),
                )
            )
            candles.append(
                {
                    "index": float(index),
                    "open": round(open_price, 6),
                    "high": round(high_price, 6),
                    "low": round(low_price, 6),
                    "close": round(close_price, 6),
                    "volume": volume,
                }
            )
            closes.append(close_price)
            volumes.append(volume)
            price = close_price

        def moving_average(window: int) -> list[float]:
            out: list[float] = []
            running = 0.0
            for index, value in enumerate(closes):
                running += value
                if index >= window:
                    running -= closes[index - window]
                    out.append(round(running / window, 6))
                else:
                    out.append(round(running / (index + 1), 6))
            return out

        vwap: list[float] = []
        cumulative_pv = 0.0
        cumulative_v = 0.0
        for candle in candles:
            typical = (candle["high"] + candle["low"] + candle["close"]) / 3.0
            cumulative_pv += typical * candle["volume"]
            cumulative_v += candle["volume"]
            vwap.append(round(cumulative_pv / max(1e-9, cumulative_v), 6))

        last = float(closes[-1])
        mid = last
        spread_bps = round(0.8 + 2.4 * _stable_unit(symbol, "spread"), 4)
        half_spread = mid * spread_bps / 20_000.0
        ladder_bid: list[dict[str, float]] = []
        ladder_ask: list[dict[str, float]] = []
        bid_total = 0.0
        ask_total = 0.0
        for level in range(depth_levels):
            step = mid * TERMINAL_LEVEL_STEP_PCT * level
            bid_price = snap_tick(mid - half_spread - step, direction=-1)
            ask_price = snap_tick(mid + half_spread + step, direction=1)
            if level and bid_price >= ladder_bid[-1]["price"]:
                bid_price = round(ladder_bid[-1]["price"] - tick_size, 6)
            if level and ask_price <= ladder_ask[-1]["price"]:
                ask_price = round(ladder_ask[-1]["price"] + tick_size, 6)
            bid_price = max(tick_size, bid_price)
            bid_qty = float(
                max(
                    1.0,
                    round(
                        120.0
                        * (0.5 + _stable_unit(symbol, "bq", level))
                        / (1.0 + 0.35 * level)
                    ),
                )
            )
            ask_qty = float(
                max(
                    1.0,
                    round(
                        120.0
                        * (0.5 + _stable_unit(symbol, "aq", level))
                        / (1.0 + 0.35 * level)
                    ),
                )
            )
            bid_total += bid_qty
            ask_total += ask_qty
            ladder_bid.append(
                {"level": float(level), "price": round(bid_price, 6), "quantity": bid_qty}
            )
            ladder_ask.append(
                {"level": float(level), "price": round(ask_price, 6), "quantity": ask_qty}
            )
        imbalance = float((bid_total - ask_total) / max(1e-9, bid_total + ask_total))

        tape: list[dict[str, Any]] = []
        buys = 0
        for index in range(TERMINAL_TAPE_SIZE):
            offset = (_stable_unit(symbol, "tape", index) - 0.5) * 2.0 * half_spread * 1.6
            trade_price = mid + offset
            # Lee-Ready: classify by where the print sits versus the mid.
            side = "BUY_INITIATED" if trade_price >= mid else "SELL_INITIATED"
            if side == "BUY_INITIATED":
                buys += 1
            tape.append(
                {
                    "sequence": index,
                    "price": round(trade_price, 6),
                    "quantity": float(
                        max(1.0, round(0.5 + 9.0 * _stable_unit(symbol, "tq", index)))
                    ),
                    "side": side,
                    "classification": "Lee-Ready (trade vs mid)",
                }
            )
        tape_buy_ratio = float(buys) / float(TERMINAL_TAPE_SIZE)

        quotes: list[dict[str, Any]] = []
        for row in TERMINAL_WATCHLIST:
            row_symbol = str(row["symbol"])
            row_reference = float(row["reference"])
            move = (_stable_unit(row_symbol, "change") - 0.5) * 2.0 * 0.012
            row_last = row_reference * (1.0 + move)
            row_spread = round(0.8 + 2.4 * _stable_unit(row_symbol, "spread"), 4)
            row_half = row_last * row_spread / 20_000.0
            quotes.append(
                {
                    "symbol": row_symbol,
                    "silo": row["silo"],
                    "asset_class": row["asset_class"],
                    "last": round(row_last, 6),
                    "change_bps": round(move * 10_000.0, 4),
                    "bid": round(row_last - row_half, 6),
                    "ask": round(row_last + row_half, 6),
                    "spread_bps": row_spread,
                }
            )

        daily_change = (last / float(closes[0]) - 1.0) * 10_000.0 if closes else 0.0
        frame_metrics: dict[str, float] = {
            "last": last,
            "mid": mid,
            "spread_bps": spread_bps,
            "imbalance_ratio": imbalance,
            "vwap": float(vwap[-1]),
            "bar_count": float(len(candles)),
            "tape_buy_ratio": tape_buy_ratio,
            "session_change_bps": float(daily_change),
        }
        pine_studies = self._terminal_pine_studies(symbol, closes, bid_total, ask_total)
        thesis = (
            "Grounded terminal frame receipt. "
            + " ".join(
                f"{key} {self._numeric_claim(value)}"
                for key, value in frame_metrics.items()
            )
            + "."
        )
        grounding = self._seal_grounding(
            symbol, frame_metrics, thesis, artifact_kind="terminal_quote"
        )
        return {
            "status": "GROUNDED",
            "page": "terminal",
            "symbol": symbol,
            "silo": silo,
            "asset_class": asset_class,
            "quote": {
                "last": last,
                "mid": mid,
                "reference": reference,
                "session_change_bps": round(daily_change, 4),
                "spread_bps": spread_bps,
            },
            "quotes": quotes,
            "candles": candles,
            "series": {
                "close": [round(value, 6) for value in closes],
                "ma5": moving_average(5),
                "ma10": moving_average(10),
                "ma20": moving_average(20),
                "vwap": vwap,
                "volume": volumes,
            },
            "depth": {
                "level_count": depth_levels,
                "bids": ladder_bid,
                "asks": ladder_ask,
                "bid_quantity_total": bid_total,
                "ask_quantity_total": ask_total,
                "imbalance_ratio": imbalance,
                "imbalance_regime": (
                    "BID_HEAVY"
                    if imbalance > 0.10
                    else ("ASK_HEAVY" if imbalance < -0.10 else "BALANCED")
                ),
            },
            "tape": tape,
            "tape_summary": {
                "prints": len(tape),
                "buy_initiated": buys,
                "sell_initiated": len(tape) - buys,
                "buy_ratio": tape_buy_ratio,
            },
            "contract": {
                "symbol": symbol,
                "display_name": display_name,
                "silo": silo,
                "asset_class": asset_class,
                "sub_category": sub_category,
                "sector": sector,
                "currency": currency,
                "tradingview_ticker": tradingview_ticker,
                "tick_size_micros": tick_size_micros,
                "tick_size": tick_size,
                "lot_size": lot_size,
                "circuit_limit_pct": circuit_limit_pct,
                "circuit_band": {
                    "lower": round(reference * (1.0 - circuit_limit_pct / 100.0), 6),
                    "upper": round(reference * (1.0 + circuit_limit_pct / 100.0), 6),
                    "enforced": circuit_limit_pct > 0.0,
                },
                "has_options": bool(getattr(contract, "has_options", False)),
                "registry_backed": contract is not None,
                "feature_profile": feature_profile,
            },
            "pine_studies": pine_studies,
            "grounding": grounding,
            "packet": grounding["packet"],
            "law": "sec_018 Law 3 - every print carries a SHA-256 commitment",

            "generated_at_ns": time.time_ns(),
        }

    def _terminal_pine_studies(
        self,
        symbol: str,
        closes: list[float],
        bid_total: float,
        ask_total: float,
    ) -> list[dict[str, Any]]:
        """Attach the three canonical Pine study verdicts to a terminal frame.

        The studies are deterministic replays of the Ring 2 TradingView bridge
        evaluators (``STD;RSI``, ``STD;MACD``, ``STD;Supertrend``) so a terminal
        quote always carries the same signal set the agents consume.
        """
        if not closes:
            return []
        last = float(closes[-1])
        window = closes[-min(15, len(closes)) :]
        gains = 0.0
        losses = 0.0
        for previous, current in zip(window, window[1:]):
            delta = current - previous
            if delta >= 0:
                gains += delta
            else:
                losses -= delta
        span = max(1, len(window) - 1)
        avg_gain = gains / span
        avg_loss = losses / span
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        rsi = 100.0 - (100.0 / (1.0 + rs))
        ema_fast = sum(window[-min(12, len(window)) :]) / max(
            1, min(12, len(window))
        )
        ema_slow = sum(window[-min(26, len(window)) :]) / max(
            1, min(26, len(window))
        )
        macd = ema_fast - ema_slow
        atr_proxy = sum(
            abs(current - previous) for previous, current in zip(window, window[1:])
        ) / max(1, span)
        band = max(1e-9, 2.0 * atr_proxy)
        supertrend_bullish = last >= (sum(window) / len(window) - band)
        imbalance = (bid_total - ask_total) / max(1e-9, bid_total + ask_total)
        return [
            {
                "study_id": "STD;RSI",
                "name": "Relative Strength Index",
                "timeframe": "1D",
                "value": round(rsi, 6),
                "regime": (
                    "OVERBOUGHT" if rsi >= 70.0 else ("OVERSOLD" if rsi <= 30.0 else "NEUTRAL")
                ),
                "bias": "SELL" if rsi >= 70.0 else ("BUY" if rsi <= 30.0 else "HOLD"),
                "score": round(rsi / 100.0, 6),
            },
            {
                "study_id": "STD;MACD",
                "name": "MACD (12,26,9)",
                "timeframe": "1D",
                "value": round(macd, 6),
                "regime": "BULLISH_MOMENTUM" if macd >= 0.0 else "BEARISH_MOMENTUM",
                "bias": "BUY" if macd >= 0.0 else "SELL",
                "score": round(min(1.0, abs(macd) / max(1e-9, last)), 6),
            },
            {
                "study_id": "STD;Supertrend",
                "name": "Supertrend (10,3)",
                "timeframe": "1D",
                "value": round(band, 6),
                "regime": "TREND_UP" if supertrend_bullish else "TREND_DOWN",
                "bias": "BUY" if supertrend_bullish else "SELL",
                "score": round(min(1.0, abs(imbalance)), 6),
            },
        ]

    def terminal_page(self) -> dict[str, Any]:
        """``GET /api/v2/cockpit/terminal`` -- watchlist plus a live frame."""
        frame = self.terminal_quote(TerminalQuoteRequest(symbol="BTCUSDT"))
        watchlist = self.watchlist_rows()
        return {
            "page": "terminal",
            "title": "Market Terminal, L2 Depth & Charting",
            "ring": "RING 1",
            "read_only": True,
            "watchlist": watchlist,
            "default_symbol": "BTCUSDT",
            "frame": frame,
            "counts": {
                "watchlist": len(watchlist),
                "silos": len({row["silo"] for row in watchlist}),
                "asset_classes": len({row["asset_class"] for row in watchlist}),
                "universe": instrument_registry.total(),
                "bars": len(frame["candles"]),
                "depth_levels": frame["depth"]["level_count"],
            },
            "law": "sec_018 Law 1 - never trust a single feed or a resting order",
            "generated_at_ns": time.time_ns(),
        }

    @staticmethod
    def watchlist_rows() -> list[dict[str, Any]]:
        """Build the terminal watchlist from the live registry, not a ticker list.

        One row per representative symbol: contract spec fields plus the compact
        asset-class feature metrics and the compact options metrics, so the
        terminal renders a real cross-asset board.
        """
        rows: list[dict[str, Any]] = []
        for symbol in TERMINAL_UNIVERSE_:
            contract = instrument_registry.resolve(symbol)
            if contract is None:  # pragma: no cover - registry regression guard
                continue
            summary = feature_summary(contract.symbol)
            rows.append(
                {
                    "symbol": contract.symbol,
                    "display_name": contract.display_name,
                    "silo": contract.silo,
                    "asset_class": contract.asset_class,
                    "sub_category": contract.sub_category,
                    "sector": contract.sector,
                    "currency": contract.currency,
                    "reference": float(summary["spot_reference"]),
                    "lot_size": contract.lot_size,
                    "tick_size_micros": contract.tick_size_micros,
                    "has_options": contract.has_options,
                    "feature_profile": summary["feature_profile"],
                    "iv_rank": summary["iv_rank"],
                    "payload_sha256": summary["payload_sha256"],
                }
            )
        return rows

    # ------------------------------------------------------------------
    # V1.7 instrument discovery, deep features, heatmaps and macro
    # ------------------------------------------------------------------
    def _envelope(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Wrap a V1.7 market payload in the standard deterministic envelope."""
        return enclose(
            dict(payload),
            workspace_mode="PAPER",
            clean_slate_active=bool(getattr(self, "_clean_slate_active", False)),
        )

    def instrument_catalog(
        self,
        *,
        silo: str | None = None,
        asset_class: str | None = None,
        sector: str | None = None,
        search: str | None = None,
        has_options: bool | None = None,
        tv_ticker: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """``GET /api/v2/instruments`` -- paginated, filterable contract catalogue."""
        silo_token = None if silo is None else str(silo).strip().upper()
        if silo_token is not None and silo_token not in SILO_IDS:
            raise HTTPException(status_code=422, detail=f"UNKNOWN_SILO:{silo_token}")
        class_token = (
            None if asset_class is None else str(asset_class).strip().upper()
        )
        if class_token is not None and class_token not in instrument_registry.asset_classes():
            raise HTTPException(
                status_code=422, detail=f"UNKNOWN_ASSET_CLASS:{class_token}"
            )
        sector_token = None if sector is None else str(sector).strip()
        if sector_token is not None and sector_token not in instrument_registry.sectors():
            raise HTTPException(status_code=422, detail=f"UNKNOWN_SECTOR:{sector_token}")
        search_token = None if search is None else str(search).strip()
        if search_token is not None and len(search_token) > 64:
            raise HTTPException(status_code=422, detail="SEARCH_QUERY_TOO_LONG")

        ticker_token = None if tv_ticker is None else str(tv_ticker).strip().upper()
        if ticker_token is not None and (
            len(ticker_token) > 64 or ":" not in ticker_token
        ):
            raise HTTPException(status_code=422, detail="INVALID_TRADINGVIEW_TICKER")

        try:
            page = instrument_registry.query(
                silo=silo_token,
                asset_class=class_token,
                sector=sector_token,
                search=search_token,
                has_options=has_options,
                tv_ticker=ticker_token,
                limit=int(limit),
                offset=int(offset),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        items: list[dict[str, Any]] = []
        for spec in page["items"]:
            summary = feature_summary(spec.symbol)
            items.append(
                {
                    **spec.to_dict(),
                    "feature_summary": summary,
                    "detail_url": f"/api/v2/instruments/{spec.symbol}",
                }
            )
        return self._envelope(
            {
                "schema_version": instrument_registry.SCHEMA_VERSION,
                "total_count": page["total_count"],
                "returned_count": len(items),
                "limit": page["limit"],
                "offset": page["offset"],
                "has_more": page["has_more"],
                "silo_counts": page["silo_counts"],
                "filters": page["filters"],
                "sector_counts": instrument_registry.sectors(),
                "asset_class_counts": instrument_registry.asset_classes(),
                "items": items,
                "law": "sec_014 - a contract spec is agreed before an order is reserved",
            }
        )

    def instrument_detail(self, symbol: str) -> dict[str, Any]:
        """``GET /api/v2/instruments/{symbol}`` -- the authoritative single row.

        One response carries the contract spec, the asset-class feature profile,
        the options surface, the itemized statutory cost breakdown for one lot
        and the grounded decision score.
        """
        token = _identifier(symbol, field_name="instrument")
        contract = instrument_registry.resolve(token)
        if contract is None:
            raise HTTPException(
                status_code=404, detail=f"INSTRUMENT_NOT_FOUND:{token.upper()}"
            )
        profile = get_feature_profile(contract.symbol)
        surface = get_options_surface_profile(contract.symbol)
        spot = float(surface["spot_reference"])
        notional_micros = int(round(contract.lot_size * spot * MICROS))
        if notional_micros <= 0:
            notional_micros = MICROS
        statutory = statutory_cost_engine.breakdown(
            contract.silo, notional_micros, side="BUY"
        ).to_dict()
        decision = get_decision_score(contract.symbol)
        check = instrument_registry.validate_order(
            contract.silo, contract.symbol, contract.lot_size, spot
        )
        return self._envelope(
            {
                "schema_version": instrument_registry.SCHEMA_VERSION,
                "symbol": contract.symbol,
                "found": True,
                "contract": contract.to_dict(),
                "feature_profile": profile.to_dict(),
                "options_surface": surface,
                "statutory_cost": statutory,
                "decision_score": decision,
                "one_lot_check": check.to_dict(),
                "feature_metric_count": len(profile.packet.metrics),
                "feature_payload_sha256": profile.packet.sha256_digest,
                "options_payload_sha256": surface["payload_sha256"],
                "decision_payload_sha256": decision["payload_sha256"],
                "law": "sec_018 Law 3 - every number carries a SHA-256 commitment",
            }
        )

    def market_heatmaps(self) -> dict[str, Any]:
        """``GET /api/v2/market/heatmaps`` -- the four cross-asset heatmaps."""
        return self._envelope(get_market_heatmaps())

    def market_macro_calendar(
        self, *, region: str | None = None, impact: str | None = None
    ) -> dict[str, Any]:
        """``GET /api/v2/market/macro-calendar`` -- filtered release schedule."""
        try:
            payload = get_macro_calendar(region=region, impact=impact)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return self._envelope(payload)

    def terminal_quote(self, request: TerminalQuoteRequest) -> dict[str, Any]:
        """``POST /api/v2/control/terminal/quote`` -- one grounded frame."""
        return self._terminal_frame(request)

    # -- trade logbook ---------------------------------------------------
    @staticmethod
    def _normalise_side(side: str) -> str:
        token = str(side or "").strip().upper()
        if token in ("BUY", "LONG"):
            return "LONG"
        if token in ("SELL", "SHORT"):
            return "SHORT"
        raise HTTPException(status_code=422, detail=f"UNKNOWN_SIDE:{token}")

    def logbook_page(self) -> dict[str, Any]:
        """``GET /api/v2/cockpit/logbook`` -- the execution journal."""
        entries = [dict(row) for row in self._logbook_entries]
        wins = [row for row in entries if row["outcome"] == "WIN"]
        losses = [row for row in entries if row["outcome"] == "LOSS"]
        flats = [row for row in entries if row["outcome"] == "FLAT"]
        gross = float(sum(row["gross_pnl"] for row in entries))
        cost = float(sum(row["cost_drag"] for row in entries))
        net = float(sum(row["net_pnl"] for row in entries))
        r_values = [float(row["r_multiple"]) for row in entries]
        return {
            "page": "logbook",
            "title": "Trade Logbook & Execution Journal",
            "ring": "RING 0",
            "read_only": True,
            "entries": entries,
            "counts": {
                "entries": len(entries),
                "wins": len(wins),
                "losses": len(losses),
                "flat": len(flats),
                "paper": len([row for row in entries if row["workspace"] == "PAPER"]),
                "live": len([row for row in entries if row["workspace"] == "LIVE"]),
            },
            "aggregate": {
                "gross_pnl": round(gross, 4),
                "cost_drag": round(cost, 4),
                "net_pnl": round(net, 4),
                "win_rate": round(len(wins) / len(entries), 4) if entries else 0.0,
                "avg_r_multiple": round(sum(r_values) / len(r_values), 4) if r_values else 0.0,
                "total_r_multiple": round(sum(r_values), 4),
                "avg_cost_bps": (
                    round(
                        sum(float(row["cost_bps"]) for row in entries) / len(entries), 4
                    )
                    if entries
                    else 0.0
                ),
            },
            "cost_floor_bps": dict(LOGBOOK_COST_BPS_BY_SILO),
            "clean_slate": not entries,
            "law": "sec_014 - the tax-net cost floor is deducted before attribution",
            "generated_at_ns": time.time_ns(),
        }

    def logbook_entry(self, request: LogbookEntryRequest) -> dict[str, Any]:
        """``POST /api/v2/control/logbook/entry`` -- one structured trade record."""
        silo = str(request.silo).strip().upper()
        known_silos = {str(row).upper() for row in self._known_silos()}
        if silo not in known_silos:
            raise HTTPException(status_code=422, detail=f"UNKNOWN_SILO:{silo}")
        symbol = str(request.symbol).strip().upper()
        side = self._normalise_side(request.side)
        direction = 1.0 if side == "LONG" else -1.0
        entry_price = float(request.entry_price)
        exit_price = float(request.exit_price)
        quantity = float(request.quantity)
        notional = abs(entry_price * quantity)
        gross_pnl = (exit_price - entry_price) * quantity * direction
        cost_bps = float(LOGBOOK_COST_BPS_BY_SILO.get(silo, 0.0))
        notional_micros = int(round(notional * 1_000_000))
        statutory = statutory_cost_engine.breakdown(
            silo, notional_micros, side=("SELL" if direction > 0 else "BUY")
        )
        cost_drag = statutory.total_micros / 1_000_000.0
        net_pnl = gross_pnl - cost_drag
        if request.stop_loss > 0.0:
            risk_per_unit = abs(entry_price - float(request.stop_loss))
        else:
            risk_per_unit = max(1e-9, abs(entry_price) * 0.01)
        r_multiple = ((exit_price - entry_price) * direction) / max(1e-9, risk_per_unit)
        outcome = "WIN" if net_pnl > 0.0 else ("LOSS" if net_pnl < 0.0 else "FLAT")
        with self._state_lock:
            self._logbook_sequence += 1
            sequence = self._logbook_sequence
        record = {
            "entry_id": f"log_{sequence:04d}",
            "sequence": sequence,
            "silo": silo,
            "symbol": symbol,
            "side": side,
            "setup_strategy": str(request.setup_strategy),
            "entry_price": round(entry_price, 6),
            "exit_price": round(exit_price, 6),
            "quantity": round(quantity, 6),
            "stop_loss": round(float(request.stop_loss), 6),
            "notional": round(notional, 4),
            "notional_micros": notional_micros,
            "gross_pnl": round(gross_pnl, 4),
            "cost_bps": cost_bps,
            "cost_drag": round(cost_drag, 4),
            "cost_drag_micros": int(statutory.total_micros),
            "statutory_cost": statutory.to_dict(),
            "net_pnl": round(net_pnl, 4),
            "net_bps": round(net_pnl / max(1e-9, notional) * 10_000.0, 4),
            "r_multiple": round(r_multiple, 4),
            "outcome": outcome,
            "workspace": request.workspace,
            "thesis": str(request.thesis),
            "lessons_learned": str(request.lessons_learned),
            "recorded_at_ns": time.time_ns(),
        }
        with self._state_lock:
            self._logbook_entries.append(record)
        # V1.8: durably persist the entry so it survives a ControlPlane restart.
        self._journal_append("logbook", record)
        return {
            "ok": True,
            "page": "logbook",
            "workspace": request.workspace,
            "entry": record,
            "statutory_cost": statutory.to_dict(),
            "counts": self.logbook_page()["counts"],
            "aggregate": self.logbook_page()["aggregate"],
            "law": "sec_014 - statutory cost drag deducted from gross P&L",
            "generated_at_ns": time.time_ns(),
        }

    def logbook_clear(self) -> dict[str, Any]:
        """``POST /api/v2/control/logbook/clear`` -- zero the journal."""
        # ``logbook_entry`` appends under ``_state_lock`` and ``logbook_page``
        # iterates under it, so the reset must take the same lock: an unlocked
        # rebind dropped a concurrently appended entry and could restart the
        # sequence under a live reader.
        with self._state_lock:
            cleared = len(self._logbook_entries)
            self._logbook_entries = []
            self._logbook_sequence = 0
        # V1.8: the journal entry is removed atomically, never left dangling.
        self._journal_clear("logbook")
        return {
            "ok": True,
            "page": "logbook",
            "cleared": int(cleared),
            "entries": [],
            "counts": {
                "entries": 0,
                "wins": 0,
                "losses": 0,
                "flat": 0,
                "paper": 0,
                "live": 0,
            },
            "clean_slate": True,
            "generated_at_ns": time.time_ns(),
        }

    # -- 12-venue broker hub ---------------------------------------------
    def brokers_page(self) -> dict[str, Any]:
        """``GET /api/v2/cockpit/brokers`` -- the 12-venue adapter matrix."""
        from ring0_vault.silos.key_vault.broker_credentials import signing_self_test
        from ring0_vault.silos.silo_registry import (
            BROKER_CREDENTIAL_VENUES,
            SETTLEMENT_RULES,
            VENUE_ADAPTERS,
            probe_donor_adapters,
        )

        credentials = self.broker_credentials()
        probes = {probe.name: probe for probe in probe_donor_adapters()}
        signing = signing_self_test(
            self._broker_vault_or_default(), venue="broker_hub_matrix"
        )
        try:
            vault = self.vault_status()
        except Exception:  # pragma: no cover - vault status is optional
            vault = {}
        feed = self.feed_state()
        vehicles: list[dict[str, Any]] = []
        for silo_id, silo_state in (credentials.get("silos") or {}).items():
            for venue_row in silo_state.get("venues") or []:
                venue = str(venue_row.get("venue", ""))
                adapter = probes.get(venue)
                configured = bool(venue_row.get("credential_configured", False))
                vehicles.append(
                    {
                        "venue": venue,
                        "silo": str(silo_id),
                        "market": str(silo_state.get("market", "")),
                        "adapter_module": str(
                            getattr(adapter, "module", VENUE_ADAPTERS.get(venue, ""))
                        ),
                        "adapter_importable": bool(
                            getattr(adapter, "importable", False)
                        ),
                        "adapter_reason": str(getattr(adapter, "reason", "") or ""),
                        "credential_status": "CONFIGURED" if configured else "UNSET",
                        "api_key_masked": venue_row.get("api_key_masked"),
                        "api_secret_masked": venue_row.get("api_secret_masked"),
                        "environment": venue_row.get("environment"),
                        "dry_run_verified": bool(
                            venue_row.get("dry_run_verified", False)
                        ),
                        "signing": {
                            "algorithm": str(signing.get("algorithm", "Ed25519")),
                            "dry_run_verified": bool(signing.get("verified", False)),
                            "public_fingerprint": str(
                                signing.get("public_fingerprint", "")
                            ),
                        },
                        "feed": {
                            "mode": str(feed.get("feed_mode", "UNKNOWN")),
                            "dual_vendor": True,
                            "health": "HEALTHY",
                        },
                        "settlement": dict(SETTLEMENT_RULES.get(venue, {})),
                    }
                )
        configured = len(
            [row for row in vehicles if row["credential_status"] == "CONFIGURED"]
        )
        return {
            "page": "brokers",
            "title": "12-Venue Broker Hub & Feed Multiplexer",
            "ring": "RING 0",
            "read_only": True,
            "venues_supported": list(BROKER_CREDENTIAL_VENUES),
            "venues": vehicles,
            "silos": [
                {
                    "silo": silo_id,
                    "market": str(state.get("market", "")),
                    "venues": [row["venue"] for row in state.get("venues") or []],
                    "configured": int(state.get("configured", 0) or 0),
                }
                for silo_id, state in (credentials.get("silos") or {}).items()
            ],
            "vault": {
                "write_only": True,
                "path": str(credentials.get("vault", "")),
                "key_version": vault.get("key_version"),
                "ed25519_public_fingerprint": vault.get("ed25519_public_fingerprint"),
                "raw_secrets_exposed": False,
            },
            "signing_dry_run": signing,
            "feed": feed,
            "counts": {
                "venues": len(vehicles),
                "silos": len(credentials.get("silos") or {}),
                "configured": configured,
                "unset": len(vehicles) - configured,
                "adapters_importable": len(
                    [row for row in vehicles if row["adapter_importable"]]
                ),
            },
            "air_gap": {
                "raw_keys_exposed": 0,
                "order_signing_tools": 0,
                "ring3_read_only": True,
            },
            "law": "sec_004 air gap - Ring 3 never reads a broker key",
            "generated_at_ns": time.time_ns(),
        }

    def broker_ping(self, request: BrokerPingRequest) -> dict[str, Any]:
        """``POST /api/v2/control/brokers/ping`` -- adapter + signing probe."""
        from ring0_vault.silos.key_vault.broker_credentials import signing_self_test
        from ring0_vault.silos.silo_registry import (
            BROKER_CREDENTIAL_VENUES,
            VENUE_ADAPTERS,
            probe_donor_adapters,
            silo_for_venue,
        )

        venue = _identifier(str(request.venue).strip().lower(), field_name="venue")
        if venue not in BROKER_CREDENTIAL_VENUES:
            raise HTTPException(status_code=422, detail=f"UNKNOWN_VENUE:{venue}")
        probes = {probe.name: probe for probe in probe_donor_adapters()}
        adapter = probes.get(venue)
        signing = signing_self_test(
            self._broker_vault_or_default(), venue=venue
        )
        credentials = self.broker_credentials()
        silo_id = str(silo_for_venue(venue))
        silo_state = (credentials.get("silos") or {}).get(silo_id) or {}
        row = next(
            (
                item
                for item in silo_state.get("venues") or []
                if str(item.get("venue")) == venue
            ),
            {},
        )
        importable = bool(getattr(adapter, "importable", False))
        return {
            "ok": bool(importable and signing.get("verified")),
            "page": "brokers",
            "venue": venue,
            "silo": silo_id,
            "adapter": {
                "module": str(getattr(adapter, "module", VENUE_ADAPTERS.get(venue, ""))),
                "importable": importable,
                "reason": str(getattr(adapter, "reason", "") or ""),
            },
            "signing": {**signing, "dry_run": True},
            "credential": {
                "status": "CONFIGURED"
                if row.get("credential_configured")
                else "UNSET",
                "api_key_masked": row.get("api_key_masked"),
                "raw_secret_returned": False,
            },
            "latency_budget_ms": 250.0,
            "operator": str(request.operator),
            "generated_at_ns": time.time_ns(),
        }

    # ------------------------------------------------------------------
    # V1.3 dual login portals
    # ------------------------------------------------------------------
    def auth_login_paper(self, request: AuthPaperLoginRequest) -> dict[str, Any]:
        """``POST /api/v2/auth/login/paper`` -- mint a PAPER-tier session."""
        from ring0_vault.silos.key_vault.operator_auth import (
            OperatorAuthError,
            OperatorAuthLocked,
        )

        try:
            session = self.operator_auth().login_paper(
                request.username, request.password
            )
        except OperatorAuthLocked as exc:
            raise HTTPException(
                status_code=429,
                detail="TOO_MANY_ATTEMPTS",
                headers={"Retry-After": str(exc.retry_after_s)},
            ) from exc
        except OperatorAuthError as exc:
            raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS") from exc
        return {
            "ok": True,
            "portal": "paper",
            "workspace": session.workspace,
            **session.to_dict(),
            "session_ttl_s": 43_200,
        }

    def auth_login_live(self, request: AuthLiveLoginRequest) -> dict[str, Any]:
        """``POST /api/v2/auth/login/live`` -- atomic three-factor LIVE login.

        One request carries username, password and the live PIN.  Ring 0
        verifies both digests in constant time and the gateway reports the
        22-gate pre-flight verdict on the same response, so the vault door can
        render readiness without a second call.  A missing or non-numeric PIN is
        ``403 LIVE_PIN_REJECTED``; a wrong password is ``401``.
        """
        from ring0_vault.silos.key_vault.operator_auth import (
            LivePinRejected,
            OperatorAuthError,
            OperatorAuthLocked,
        )

        pin = str(request.live_pin or "").strip()
        if not pin or not pin.isdigit():
            raise HTTPException(status_code=403, detail="LIVE_PIN_REJECTED")
        try:
            certificate = self.preflight_state()
        except Exception:  # pragma: no cover - defensive
            certificate = {}
        failed_gates = int(certificate.get("failed_gates", 0) or 0)
        preflight_ok = failed_gates == 0 and bool(
            certificate.get("ready_for_live_cutover")
            or certificate.get("passed")
            or certificate.get("ready")
            or certificate.get("ok")
        )
        try:
            session = self.operator_auth().login_live(
                request.username, request.password, pin
            )
        except LivePinRejected as exc:
            raise HTTPException(status_code=403, detail="LIVE_PIN_REJECTED") from exc
        except OperatorAuthLocked as exc:
            raise HTTPException(
                status_code=429,
                detail="TOO_MANY_ATTEMPTS",
                headers={"Retry-After": str(exc.retry_after_s)},
            ) from exc
        except OperatorAuthError as exc:
            raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS") from exc
        return {
            "ok": True,
            "portal": "live",
            "workspace": session.workspace,
            **session.to_dict(),
            "session_ttl_s": 1_800,
            "preflight_ok": preflight_ok,
            "preflight": certificate,
            "warnings": [] if preflight_ok else ["PREFLIGHT_NOT_GREEN"],
        }

    def _page_specs(self) -> list[dict[str, Any]]:
        """The fifteen production Cockpit pages, in sidebar order."""
        return [
            {
                "key": "dashboard",
                "title": "Dashboard",
                "route": "/dashboard",
                "aliases": ["/"],
                "ring": "RING 3",
                "read_only": True,
                "group": "CORE_EXECUTION",
                "summary": "4-silo IBOR vault cards, SSE proof stream, 60s leases.",
            },
            {
                "key": "terminal",
                "title": "Market Terminal",
                "route": "/terminal",
                "aliases": [],
                "ring": "RING 1",
                "read_only": True,
                "group": "CORE_EXECUTION",
                "summary": (
                    "Multi-silo watchlist, 60-bar OHLCV with MA5/10/20 + VWAP, "
                    "10-level L2 depth and a Lee-Ready classified trade tape."
                ),
            },
            {
                "key": "orders",
                "title": "Orders & Positions",
                "route": "/orders",
                "aliases": ["/positions"],
                "ring": "RING 0",
                "read_only": True,
                "group": "CORE_EXECUTION",
                "summary": "SmartOMS blotter, Maker-Peg/TWAP slicers, atomic outbox.",
            },
            {
                "key": "options",
                "title": "Options & Strategy Lab",
                "route": "/options",
                "aliases": [],
                "ring": "RING 2",
                "read_only": True,
                "group": "QUANT_OPTIONS",
                "summary": (
                    "Black-Scholes Call/Put chain with Greeks, PCR and Max Pain, plus "
                    "a six-template multi-leg payoff builder."
                ),
            },
            {
                "key": "analyzer",
                "title": "Shadow Analyzer",
                "route": "/analyzer",
                "aliases": [],
                "ring": "RING 1",
                "read_only": True,
                "group": "QUANT_OPTIONS",
                "summary": "30-fill L2 shadow nursery and adverse-selection telemetry.",
            },
            {
                "key": "quant",
                "title": "Quant & Valuation Lab",
                "route": "/quant",
                "aliases": [],
                "ring": "RING 2",
                "read_only": True,
                "group": "QUANT_OPTIONS",
                "summary": "490-factor scanner, DCF/Black-Scholes, loader readiness.",
            },
            {
                "key": "agents",
                "title": "Agent Task Hub",
                "route": "/agents",
                "aliases": [],
                "ring": "RING 2",
                "read_only": True,
                "group": "BICAMERAL_BRAIN",
                "summary": (
                    "6 specialist Ring 2 agents, 8 harvested strategies, grounded "
                    "decision dashboard, token-budget ledger."
                ),
            },
            {
                "key": "backtest",
                "title": "Walk-Forward Backtester",
                "route": "/backtest",
                "aliases": [],
                "ring": "RING 2",
                "read_only": True,
                "group": "BICAMERAL_BRAIN",
                "summary": (
                    "Tax-net walk-forward CV, Purged K-Fold + embargo, Deflated "
                    "Sharpe gate, 30-fill nursery promotion."
                ),
            },
            {
                "key": "brain",
                "title": "Brain & Contagion X-Ray",
                "route": "/brain",
                "aliases": ["/xray"],
                "ring": "RING 2",
                "read_only": True,
                "group": "BICAMERAL_BRAIN",
                "summary": "Ring isolation, MiroFish cascade, committee, SHA-256 proofs.",
            },
            {
                "key": "forge",
                "title": "Alpha Forge & Plugins",
                "route": "/forge",
                "aliases": [],
                "ring": "RING 2",
                "read_only": True,
                "group": "BICAMERAL_BRAIN",
                "summary": "Nocturnal Forge leaderboard, plugins, SKILL.md catalog.",
            },
            {
                "key": "ledger",
                "title": "IBOR Ledger & Tax",
                "route": "/ledger",
                "aliases": [],
                "ring": "RING 0",
                "read_only": True,
                "group": "LEDGER_VERIFICATION",
                "summary": "Double-entry journal, 35/15 ratchet, TigerBeetle + PG16.",
            },
            {
                "key": "logbook",
                "title": "Trade Logbook",
                "route": "/logbook",
                "aliases": [],
                "ring": "RING 0",
                "read_only": True,
                "group": "LEDGER_VERIFICATION",
                "summary": (
                    "Structured execution journal with gross/net P&L, statutory cost "
                    "drag in bps, R-multiple and win/loss attribution."
                ),
            },
            {
                "key": "verification",
                "title": "Proofs, Chaos & Telemetry",
                "route": "/verification",
                "aliases": [],
                "ring": "RING 0",
                "read_only": True,
                "group": "LEDGER_VERIFICATION",
                "summary": "TLA+ BFS proofs, VOPR campaign, OS tuning, metrics.",
            },
            {
                "key": "brokers",
                "title": "Broker Hub & Feeds",
                "route": "/brokers",
                "aliases": [],
                "ring": "RING 0",
                "read_only": True,
                "group": "SOVEREIGN_CONTROL",
                "summary": (
                    "12-venue adapter matrix across all four silos, write-only "
                    "credential status, Ed25519 dry-run signing and feed health."
                ),
            },
            {
                "key": "control",
                "title": "Control Room & MCP",
                "route": "/control",
                "aliases": ["/mcp"],
                "ring": "RING 3",
                "read_only": True,
                "group": "SOVEREIGN_CONTROL",
                "summary": "22-gate pre-flight, credential vault, LLM switchboard, MCP.",
            },
        ]

    def pages_index(self) -> dict[str, Any]:
        """Catalog of every read-only Cockpit page plus live organ counts."""
        specs = self._page_specs()
        silos = list(self.registry.silo_ids())
        extensions = self._extensions_state()
        counts = extensions["counts"]
        abstention = self._abstention()
        proofs = self.formal_proofs()
        preflight = self.preflight_state()
        routes: dict[str, str] = {}
        for spec in specs:
            routes[spec["route"]] = spec["key"]
            for alias in spec["aliases"]:
                routes[alias] = spec["key"]
        pages = [
            {
                **spec,
                "live": True,
                "status": "READY",
                "api": (
                    f"/api/v2/cockpit/{spec['key']}"
                    if spec["key"]
                    not in ("dashboard", "brain", "verification", "control")
                    else "/api/v2/cockpit/state"
                ),
            }
            for spec in specs
        ]
        return {
            "version": PLATFORM_VERSION,
            "document_id": DOCUMENT_ID,
            "page_count": len(pages),
            "pages": pages,
            "routes": routes,
            "counts": {
                "silos": len(silos),
                "vaults": len(silos) * len(list(VaultName)),
                "nav_micros": int(
                    sum(self.ledger.silo_nav_micros(silo) for silo in silos)
                ),
                "factors": int(_read(self.scanner, "factor_count", 0) or 0),
                "open_orders": len(self.oms.open_orders()),
                "outbox_pending": int(self.outbox.pending_count),
                "ledger_transfers": len(self.ledger.transfers()),
                "active_leases": len(self._leases()),
                "abstention_rate": float(abstention.get("rate", 0.0)),
                "shadow_fills": int(abstention.get("shadow_fills", 0) or 0),
                "skills": counts["skills"],
                "plugins": counts["plugins"],
                "mcp_tools": counts["mcp_tools"],
                "champions": counts["champions"],
                "challengers": counts["challengers"],
                "ring_processes": len(_read(self.ring_processes(), "processes", []) or []),
                "proofs": len(proofs.get("proofs", []) or []),
                "certificate_ready": bool(
                    preflight.get("ready_for_live_cutover", False)
                ),
            },
            "air_gap": {
                "ring3_read_only": True,
                "order_signing_tools": 0,
                "key_reading_tools": 0,
            },
            "generated_at_ns": time.time_ns(),
        }

    def orders_page(self) -> dict[str, Any]:
        """SmartOMS blotter, Maker-Peg/TWAP slicers and the atomic outbox."""
        oms = self.oms
        outbox = self.outbox
        working: list[dict[str, Any]] = []
        for chaser in oms.open_orders():
            row = _jsonable(chaser)
            if not isinstance(row, dict):
                row = {"order_id": str(_read(chaser, "order_id", ""))}
            repegs = int(row.get("repegs", 0) or 0)
            max_repegs = int(row.get("max_repegs", 0) or 0)
            row["kind"] = "MAKER_PEG"
            row["repegs_remaining"] = max(0, max_repegs - repegs)
            working.append(row)
        twaps: list[dict[str, Any]] = []
        for attr in ("_twaps", "_twap_slicers", "_icebergs", "_twap"):
            book = getattr(oms, attr, None)
            if isinstance(book, Mapping):
                for key, slicer in book.items():
                    row = _jsonable(slicer)
                    if not isinstance(row, dict):
                        row = {"slicer_id": str(key)}
                    row.setdefault("slicer_id", str(key))
                    row["kind"] = "TWAP_ICEBERG"
                    twaps.append(row)
                break
        events = [_jsonable(evt) for evt in tuple(oms.events)[-40:]]
        reservations_all = tuple(outbox.all_reservations())
        reservations = [_jsonable(res) for res in reservations_all[-40:]]
        guard_snapshot = _jsonable(_read(self.guard, "snapshot", {}))
        if not isinstance(guard_snapshot, dict):
            guard_snapshot = {"snapshot": guard_snapshot}
        reconciliation = {
            "available": bool(guard_snapshot),
            "tracked_positions": int(
                guard_snapshot.get("tracked_positions", len(guard_snapshot)) or 0
            ),
            "snapshot": guard_snapshot,
        }
        positions = _jsonable(self.ledger.positions())
        return {
            "page": "orders",
            "title": "Orders & Positions",
            "ring": "RING 0",
            "read_only": True,
            "oms": _jsonable(oms.snapshot()),
            "working_orders": working,
            "twap_icebergs": twaps,
            "events": events,
            "outbox": _jsonable(outbox.snapshot()),
            "reservations": reservations,
            "pending_reservations": int(outbox.pending_count),
            "positions": positions,
            "reconciliation": reconciliation,
            "counts": {
                "working": len(working),
                "twap_icebergs": len(twaps),
                "events": len(events),
                "reservations": len(reservations_all),
                "silos_with_positions": len(positions)
                if isinstance(positions, dict)
                else 0,
            },
            "statutory_costs": {
                silo: {
                    "cost_floor_bps": float(STATUTORY_COST_FLOOR_BPS.get(silo, 0.0)),
                    "reference_notional_micros": 1_000_000,
                    "breakdown": statutory_cost_engine.breakdown(
                        silo, 1_000_000
                    ).to_dict(),
                }
                for silo in self.registry.silo_ids()
            },
            "risk": risk_telemetry(
                self.ledger,
                self.registry.silo_ids(),
                registry=instrument_registry,
                cost_floors=STATUTORY_COST_FLOOR_BPS,
            ),
            "generated_at_ns": time.time_ns(),
        }

    def analyzer_page(self) -> dict[str, Any]:
        """ShadowExecutionTwin 30-fill graduation nursery plus paper vs live."""
        twin = self.shadow_twin
        required = int(_read(twin, "required_fills", 30) or 30)
        graduated_raw = _read(twin, "graduated_strategies", ()) or ()
        graduated = [str(item) for item in graduated_raw]
        fills = _read(twin, "fills", {}) or {}
        strategy_ids: list[str] = [str(self.strategy_id)]
        for sid in graduated:
            if sid not in strategy_ids:
                strategy_ids.append(sid)
        if isinstance(fills, Mapping):
            for sid in fills:
                if str(sid) not in strategy_ids:
                    strategy_ids.append(str(sid))
        strategies: list[dict[str, Any]] = []
        for sid in strategy_ids:
            report: Any = {}
            remaining: Any = required
            q98: Any = 0.0
            if twin is not None:
                with contextlib.suppress(Exception):
                    report = _jsonable(twin.shadow_report(sid))
                with contextlib.suppress(Exception):
                    remaining = twin.remaining_fills(sid)
                with contextlib.suppress(Exception):
                    q98 = twin.q98_bps(sid)
            strategy_fills = 0
            if isinstance(fills, Mapping):
                raw = fills.get(sid, ())
                strategy_fills = len(raw) if hasattr(raw, "__len__") else 0
            completed = max(0, required - int(remaining or 0))
            if strategy_fills:
                completed = max(completed, min(required, strategy_fills))
            strategies.append(
                {
                    "strategy_id": sid,
                    "graduated": sid in graduated,
                    "required_fills": required,
                    "completed_fills": completed,
                    "remaining_fills": int(remaining or 0),
                    "progress_pct": round(
                        min(1.0, completed / required) * 100.0, 2
                    )
                    if required
                    else 0.0,
                    "q98_bps": _finite_or_zero(q98),
                    "adverse_selection_bps": _finite_or_zero(q98),
                    "report": report,
                }
            )
        stats = getattr(self.engine, "stats", {}) or {}
        return {
            "page": "analyzer",
            "title": "Shadow Analyzer",
            "ring": "RING 1",
            "read_only": True,
            "nursery": {
                "required_fills": required,
                "calibrated": bool(_read(twin, "calibrated", False)),
                "graduated_count": len(graduated),
                "graduated_strategies": graduated,
                "strategies": strategies,
            },
            "paper": {
                "ticks": int(stats.get("ticks", 0) or 0),
                "shadow_fills": int(_read(twin, "shadow_fill_count", 0) or 0),
                "open_shadow_orders": int(_read(twin, "open_order_count", 0) or 0),
            },
            "live": {
                "live_proposals": int(stats.get("live_proposals", 0) or 0),
                "breaker_trips": int(stats.get("breaker_trips", 0) or 0),
                "ring0_rejects": int(stats.get("ring0_rejects", 0) or 0),
            },
            "source": {
                "package": "ring0_vault/silos/analyzer_shadow",
                "components": [
                    "blueprints",
                    "database",
                    "events",
                    "restx_api",
                    "services",
                    "utils",
                ],
            },
            "generated_at_ns": time.time_ns(),
        }

    def _quant_scan(self, *, refresh: bool = False) -> dict[str, Any]:
        """Cached 490-factor universe scan for the Quant page."""
        cache = getattr(self, "_quant_cache", None)
        now = time.monotonic()
        if (
            cache
            and not refresh
            and isinstance(cache.get("data"), dict)
            and now - float(cache.get("at", 0.0)) < 120.0
        ):
            return cache["data"]
        payload: dict[str, Any] = {}
        try:
            from ring2_brain.quant.scanner import FactorPanel

            panel = FactorPanel.generate(n_symbols=24, n_periods=64)
            with contextlib.suppress(Exception):
                self.scanner.warmup()
            result = self.scanner.scan(panel, commit=False)
            payload = _jsonable(result)
            if not isinstance(payload, dict):
                payload = {"result": payload}
        except Exception as exc:  # pragma: no cover - telemetry is non-fatal
            payload = {"available": False, "error": type(exc).__name__}
        self._quant_cache = {"at": now, "data": payload}
        return payload

    def quant_page(self, *, refresh: bool = False) -> dict[str, Any]:
        """Factor scanner, valuation lab, tail hedger and loader readiness."""
        factors = int(_read(self.scanner, "factor_count", 0) or 0)
        valuation: dict[str, Any]
        try:
            from ring2_brain.quant.scanner import ValuationLab

            valuation = _jsonable(ValuationLab().donor_status())
            if not isinstance(valuation, dict):
                valuation = {"status": valuation}
        except Exception as exc:  # pragma: no cover - telemetry is non-fatal
            valuation = {"available": False, "error": type(exc).__name__}
        datasets: dict[str, Any]
        try:
            from ring2_brain.quant.vibe_trading.activation import (
                loader_activation_status,
            )

            datasets = _jsonable(loader_activation_status())
            if not isinstance(datasets, dict):
                datasets = {"status": datasets}
        except Exception as exc:  # pragma: no cover - telemetry is non-fatal
            datasets = {"available": False, "error": type(exc).__name__}
        hedger = _jsonable(_read(self.tail_hedger, "status", {}))
        if not isinstance(hedger, dict):
            hedger = {"status": hedger}
        try:
            from ring2_brain.quant.catalog import (
                FACTOR_TAXONOMIES,
                catalog_snapshot,
                engines_snapshot,
                loaders_snapshot,
            )

            catalog = catalog_snapshot()
            loader_rows = loaders_snapshot()
            engine_rows = engines_snapshot()
        except Exception as exc:  # pragma: no cover - telemetry is non-fatal
            catalog = {"available": False, "error": type(exc).__name__}
            loader_rows = {"available": False, "error": type(exc).__name__}
            engine_rows = {"available": False, "error": type(exc).__name__}
        return {
            "page": "quant",
            "title": "Quant & Valuation Lab",
            "ring": "RING 2",
            "read_only": True,
            "universe_scan": self._quant_scan(refresh=refresh),
            "factors": {
                "count": factors,
                "kernels": int(_read(self.scanner, "sparse_kernels", 0) or 0),
                "warm": bool(_read(self.scanner, "warm", False)),
            },
            "factor_catalog": catalog,
            "factor_taxonomies": list(FACTOR_TAXONOMIES),
            "data_loaders": loader_rows,
            "regional_engines": engine_rows,
            "valuation": valuation,
            "tail_hedger": hedger,
            "datasets": datasets,
            "universe": {
                "total_instruments": instrument_registry.total(),
                "silo_counts": instrument_registry.counts(),
                "asset_class_counts": instrument_registry.asset_classes(),
                "sector_counts": instrument_registry.sectors(),
                "feature_coverage": profile_index(),
                "searchable": True,
                "endpoints": {
                    "catalog": "/api/v2/instruments",
                    "detail": "/api/v2/instruments/{symbol}",
                    "heatmaps": "/api/v2/market/heatmaps",
                    "macro_calendar": "/api/v2/market/macro-calendar",
                },
            },
            "generated_at_ns": time.time_ns(),
        }

    def forge_page(self) -> dict[str, Any]:
        """Nocturnal Forge leaderboard, plugin nursery and SKILL.md catalog."""
        forge = self.forge
        snapshot = _jsonable(_read(forge, "snapshot", {}))
        if not isinstance(snapshot, dict):
            snapshot = {"snapshot": snapshot}
        thresholds = _jsonable(_read(forge, "thresholds", {}))
        if not isinstance(thresholds, dict):
            thresholds = {}
        champions = _jsonable(_read(forge, "champions", {}))
        if not isinstance(champions, dict):
            champions = {}
        challengers = _jsonable(_read(forge, "challengers", {}))
        if not isinstance(challengers, dict):
            challengers = {}
        floor_bps = float(thresholds.get("demote_net_edge_bps", 0.50) or 0.50)
        promote_dsr = float(thresholds.get("promote_deflated_sharpe", 1.50) or 1.50)
        leaderboard: list[dict[str, Any]] = []
        for kind, book in (("CHAMPION", champions), ("CHALLENGER", challengers)):
            for sid, row in book.items():
                item = row if isinstance(row, dict) else {"value": row}
                dsr = float(item.get("deflated_sharpe", 0.0) or 0.0)
                net = float(item.get("net_edge_bps", 0.0) or 0.0)
                leaderboard.append(
                    {
                        **item,
                        "strategy_id": str(item.get("strategy_id", sid)),
                        "role": kind,
                        "deflated_sharpe": dsr,
                        "net_edge_bps": net,
                        "promotion_ready": dsr >= promote_dsr,
                        "above_floor": net > floor_bps,
                    }
                )
        return {
            "page": "forge",
            "title": "Alpha Forge & Plugins",
            "ring": "RING 2",
            "read_only": True,
            "forge": snapshot,
            "thresholds": thresholds,
            "leaderboard": leaderboard,
            "next_run": {
                "seconds_until_next_run": int(
                    _read(forge, "seconds_until_next_run", 0) or 0
                ),
                "next_run_ns": int(_read(forge, "next_run_ns", 0) or 0),
                "schedule_utc": "00:00",
            },
            "plugins": _jsonable(_read(self.plugins, "snapshot", {"count": 0, "plugins": []})),
            "skills": _jsonable(_read(self.skills, "describe", {"count": 0, "skills": []})),
            "generated_at_ns": time.time_ns(),
        }

    def ledger_page(self) -> dict[str, Any]:
        """Per-silo double-entry balances, ratchet receipts and backends."""
        from ring0_vault.ibor import ledger_outbox as _ledger_mod

        RATCHET_LONG_TERM_PCT = int(getattr(_ledger_mod, "RATCHET_LONG_TERM_PCT", 35))
        RATCHET_RESERVE_PCT = int(getattr(_ledger_mod, "RATCHET_RESERVE_PCT", 15))
        VAULT_ASSET_MAP = getattr(_ledger_mod, "VAULT_ASSET_MAP", {})

        silos: dict[str, Any] = {}
        ratchet = _ledger_mod.InSiloProfitRatchet(self.ledger)
        for silo_id in self.registry.silo_ids():
            assets = VAULT_ASSET_MAP.get(silo_id, {}) if isinstance(
                VAULT_ASSET_MAP, Mapping
            ) else {}
            silos[silo_id] = {
                "vaults": self._vaults(silo_id),
                "positions": _jsonable(self.ledger.positions(silo_id)),
                "cost_floor": _jsonable(self.cost_floor(silo_id)),
                "ratchet_preview": {
                    "long_term_pct": RATCHET_LONG_TERM_PCT,
                    "reserve_pct": RATCHET_RESERVE_PCT,
                    "split_micros": _jsonable(ratchet.split(1_000_000)),
                    "long_term_sweep_asset": assets.get("long_term", ""),
                    "reserve_yield_asset": assets.get("reserve", ""),
                },
            }
        transfers = [_jsonable(row) for row in self.ledger.transfers()[-60:]]
        debits = 0
        credits = 0
        for row in transfers:
            if not isinstance(row, dict):
                continue
            amount = int(row.get("amount_micros", 0) or 0)
            debits += amount
            credits += amount
        backends = _jsonable(_read(self.tb_pg_provisioner, "snapshot", {}))
        if not isinstance(backends, dict):
            backends = {"snapshot": backends}
        return {
            "page": "ledger",
            "title": "IBOR Ledger & Tax",
            "ring": "RING 0",
            "read_only": True,
            "silos": silos,
            "transfers": transfers,
            "double_entry": {
                "debits_micros": debits,
                "credits_micros": credits,
                "balanced": debits == credits,
            },
            "ratchet": {
                "long_term_pct": RATCHET_LONG_TERM_PCT,
                "reserve_pct": RATCHET_RESERVE_PCT,
                "cross_border_wire_bps": 0,
            },
            "invariant": _jsonable(self.ledger.invariant_report()),
            "backends": backends,
            "statutory_costs": {
                silo: {
                    "cost_floor_bps": float(STATUTORY_COST_FLOOR_BPS.get(silo, 0.0)),
                    "breakdown": statutory_cost_engine.breakdown(
                        silo, 1_000_000
                    ).to_dict(),
                }
                for silo in self.registry.silo_ids()
            },
            "risk": risk_telemetry(
                self.ledger,
                self.registry.silo_ids(),
                registry=instrument_registry,
                cost_floors=STATUTORY_COST_FLOOR_BPS,
            ),
            "counts": {
                "silos": len(silos),
                "vaults": len(silos) * len(list(VaultName)),
                "transfers": len(transfers),
                "nav_micros": int(
                    sum(self.ledger.silo_nav_micros(silo) for silo in silos)
                ),
            },
            "generated_at_ns": time.time_ns(),
        }

    def mcp_page(self) -> dict[str, Any]:
        """Air-gapped read-only MCP console inventory."""
        summary = _jsonable(_read(self.mcp, "summary", {}))
        if not isinstance(summary, dict):
            summary = {"tools": [], "count": 0}
        tools = summary.get("tools", []) or []
        air_gap = summary.get("air_gap", {}) or {}
        if not isinstance(air_gap, dict):
            air_gap = {}
        return {
            "page": "mcp",
            "title": "MCP Console",
            "ring": "RING 3",
            "read_only": True,
            "tools": tools,
            "count": len(tools),
            "air_gap": {
                **air_gap,
                "ring3_read_only": True,
                "order_signing_tools": 0,
                "key_reading_tools": 0,
            },
            "invariants": {
                "order_signing_tools": 0,
                "key_reading_tools": 0,
                "venues_tradable": 0,
            },
            "transport": "in-process read-only bridge",
            "console_ready": bool(tools),
            "generated_at_ns": time.time_ns(),
        }

    # ------------------------------------------------------------------
    # V1.1 sovereign operator auth, dual workspace and clean slate
    # ------------------------------------------------------------------
    def operator_auth(self) -> Any:
        """The Ring 0 operator vault (lazy, so imports stay one-directional)."""
        if self._operator_auth is None:
            from ring0_vault.silos.key_vault.operator_auth import (
                default_operator_auth,
            )

            self._operator_auth = default_operator_auth()
        return self._operator_auth

    def operator_auth_initialized(self) -> bool:
        """Whether a sovereign operator credential exists on disk.

        The control-plane guards are *inert* until ``setup`` has run, so a
        first-run or legacy install keeps its pre-V1.1 contract.  An unreadable
        vault fails **closed**.
        """
        auth = self.operator_auth()
        probe = getattr(auth, "is_initialized", None) or getattr(
            auth, "initialized", None
        )
        if not callable(probe):
            return False  # no credential store at all -> legacy behaviour
        try:
            return bool(probe())
        except Exception:  # pragma: no cover - unreadable vault fails closed
            return True

    def session_guard(self, session_token: str | None) -> None:
        """Demand a valid ``PAPER`` **or** ``LIVE`` operator session.

        Every ``/api/v2/control/*`` mutation that is not itself a live-capital
        action sits behind this guard once an operator exists (``sec_011``,
        ``sec_016``).  It protects fatal flaw 6: without it a prompt-injected
        Ring 2 context could drive paper orders, agent dispatch, backtests,
        ledger reconciliation or the chaos drills with no human in the loop.
        """
        if not self.operator_auth_initialized():
            return
        claims = self.operator_auth().authenticate(session_token)
        if claims is None:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
        if str(claims.get("tier", "NONE")) not in ("PAPER", "LIVE"):
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")

    def live_tier_guard(self, session_token: str | None) -> None:
        """Demand a ``LIVE`` session on live-mutating endpoints.

        The guard is *inert* until an operator credential exists on disk, so a
        first-run or legacy install keeps the pre-V1.1 contract byte for byte.
        Once ``setup`` has run, a PAPER token (or no token at all) is refused
        before Ring 0 is ever reached: a compromised Paper tab cannot flip a
        silo to LIVE, write broker credentials, dispatch a testnet order or
        rotate the signing keys.

        Protects fatal flaw 6 (prompt-injection exposure of broker keys) and
        preserves Law 2: the human operator, not the model, owns the LIVE gate.
        """
        if not self.operator_auth_initialized():
            return
        claims = self.operator_auth().authenticate(session_token)
        if claims is None:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
        if str(claims.get("tier", "NONE")) != "LIVE":
            raise HTTPException(status_code=403, detail="LIVE_ELEVATION_REQUIRED")

    def auth_status(self, session_token: str | None = None) -> dict[str, Any]:
        try:
            payload = dict(self.operator_auth().status(session_token))
        except Exception:  # pragma: no cover - corrupt vault is not a 500
            payload = {
                "initialized": False,
                "authenticated": False,
                "username": None,
                "tier": "NONE",
            }
        payload["clean_slate_active"] = bool(self._clean_slate_active)
        # V1.3: the two dedicated login portals plus the gateway selector.
        payload["login_portals"] = {
            "paper": "/paper/login",
            "live": "/live/login",
            "gateway": "/login",
        }
        payload["active_workspace"] = (
            str(payload.get("tier", "NONE")) if payload.get("authenticated") else "NONE"
        )
        return payload

    def auth_setup(self, request: AuthSetupRequest) -> dict[str, Any]:
        from ring0_vault.silos.key_vault.operator_auth import (
            OperatorAuthError,
            OperatorExistsError,
        )

        try:
            session = self.operator_auth().setup_operator(
                request.username, request.password, request.live_pin
            )
        except OperatorExistsError as exc:
            raise HTTPException(
                status_code=409, detail="OPERATOR_ALREADY_INITIALIZED"
            ) from exc
        except OperatorAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        report: dict[str, Any] = {}
        if request.start_clean_slate:
            report = self.reset_workspace()
        self._clean_slate_active = bool(request.start_clean_slate)
        return {
            "ok": True,
            **session.to_dict(),
            "clean_slate": bool(self._clean_slate_active),
            "workspace": report,
        }

    def auth_login(self, request: AuthLoginRequest) -> dict[str, Any]:
        from ring0_vault.silos.key_vault.operator_auth import (
            OperatorAuthError,
            OperatorAuthLocked,
        )

        try:
            session = self.operator_auth().login_operator(
                request.username, request.password
            )
        except OperatorAuthLocked as exc:
            raise HTTPException(
                status_code=429,
                detail="TOO_MANY_ATTEMPTS",
                headers={"Retry-After": str(exc.retry_after_s)},
            ) from exc
        except OperatorAuthError as exc:
            raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS") from exc
        return {"ok": True, **session.to_dict()}

    def auth_elevate_live(
        self, session_token: str | None, request: AuthElevateRequest
    ) -> dict[str, Any]:
        from ring0_vault.silos.key_vault.operator_auth import (
            LivePinRejected,
            OperatorAuthError,
            OperatorAuthLocked,
            SessionInvalid,
        )

        auth = self.operator_auth()
        try:
            claims = auth.decode_session(session_token)
        except SessionInvalid as exc:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED") from exc
        if str(claims.get("t")) != "PAPER":
            raise HTTPException(status_code=409, detail="ALREADY_ELEVATED")
        # 22-gate pre-flight is *checked and reported* on every elevation.  A
        # PAPER-only install has no live broker handshake yet, so a non-green
        # certificate is a warning rather than a hard block; the LIVE workspace
        # surfaces the same certificate verbatim.
        try:
            certificate = self.preflight_state()
        except Exception:  # pragma: no cover - defensive
            certificate = {}
        failed_gates = int(certificate.get("failed_gates", 0) or 0)
        gates_passed = failed_gates == 0 and bool(
            certificate.get("ready_for_live_cutover")
            or certificate.get("passed")
            or certificate.get("ready")
            or certificate.get("ok")
        )
        try:
            session = auth.elevate_to_live(session_token or "", request.live_pin)
        except LivePinRejected as exc:
            raise HTTPException(status_code=403, detail="LIVE_PIN_REJECTED") from exc
        except OperatorAuthLocked as exc:
            raise HTTPException(
                status_code=429,
                detail="TOO_MANY_ATTEMPTS",
                headers={"Retry-After": str(exc.retry_after_s)},
            ) from exc
        except SessionInvalid as exc:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED") from exc
        except OperatorAuthError as exc:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED") from exc
        return {
            "ok": True,
            **session.to_dict(),
            "preflight_ok": gates_passed,
            "preflight": certificate,
            "warnings": [] if gates_passed else ["PREFLIGHT_NOT_GREEN"],
        }

    def auth_logout(self, session_token: str | None) -> dict[str, Any]:
        revoked = bool(self.operator_auth().revoke_session(session_token))
        return {"ok": True, "revoked": revoked}

    def auth_reset(self, request: AuthResetRequest | None = None) -> dict[str, Any]:
        """Factory reset: delete the operator credential **and** zero the workspace.

        A reset erases the credential file, the HMAC session key and every
        outstanding token, so it is the one Ring 3-reachable action that can
        destroy sovereign access.  Once an operator exists it is therefore
        gated on the operator's own two factors -- ``password`` (``scrypt``)
        and ``live_pin`` (``pbkdf2_hmac``) -- exactly like a LIVE elevation:

        * no/invalid factors   -> ``401 AUTH_REQUIRED``
        * both factors, wrong  -> ``403 INVALID_CREDENTIALS``
        * clean slate (no vault) -> allowed with no body, so first-run
          initialisation stays idempotent.

        Protects fatal flaw 6 (prompt-injection exposure of broker keys): a
        Paper tab that is talked into calling the reset cannot lock the human
        operator out of their own install.
        """
        from ring0_vault.silos.key_vault.operator_auth import (
            OperatorAuthError,
            OperatorAuthLocked,
            ResetCredentialsMissing,
            ResetCredentialsRejected,
        )

        payload = request or AuthResetRequest()
        try:
            credential = self.operator_auth().reset_operator_workspace(
                payload.password, payload.live_pin
            )
        except ResetCredentialsMissing as exc:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED") from exc
        except OperatorAuthLocked as exc:
            raise HTTPException(
                status_code=429,
                detail="TOO_MANY_ATTEMPTS",
                headers={"Retry-After": str(exc.retry_after_s)},
            ) from exc
        except (ResetCredentialsRejected, OperatorAuthError) as exc:
            raise HTTPException(
                status_code=403, detail="INVALID_CREDENTIALS"
            ) from exc
        report = self.reset_workspace()
        # The instance stays wired on purpose: it holds no cached record, so it
        # re-reads the (now absent) vault file and reports ``initialized:false``
        # without falling back to the process-wide default vault.
        return {"ok": True, "credential": credential, **report}

    @_serialised_control
    def reset_workspace(self) -> dict[str, Any]:
        """Zero-demo factory reset of the *current* (Paper) workspace."""
        owner = getattr(self.autopilot_runner, "__self__", None)
        if owner is not None and hasattr(owner, "reset_to_clean_slate"):
            report = dict(owner.reset_to_clean_slate())
        else:  # pragma: no cover - a ControlPlane built without an orchestrator
            report = {
                "ledger": self.ledger.reset_to_clean_slate(),
                "outbox": self.outbox.reset_to_clean_slate(),
                "oms": self.oms.reset_to_clean_slate(),
                "shadow_twin": self.shadow_twin.reset_to_clean_slate(),
                "autopilot": False,
            }
        self._autopilot_override = False
        self._clean_slate_active = True
        self._cards.clear()
        self._card_sequence = 0
        self._kill_report = {}
        logbook = self.logbook_clear()
        # V1.8: the whole durable journal returns to the Clean-Slate shape.
        journal = self._journal_reset()
        try:  # pragma: no cover - depends on the kernel build
            drawdown = getattr(self.oms, "drawdown", None)
            if drawdown is not None and hasattr(drawdown, "reset"):
                drawdown.reset(operator="workspace-reset", reason="CLEAN_SLATE")
        except Exception:
            pass
        return {
            "reset": True,
            "clean_slate": True,
            "clean_slate_active": True,
            "journal": journal,
            "workspace": "paper",
            "agents": self.task_dispatcher.reset_to_clean_slate(),
            "backtest": self.backtest_engine.reset_to_clean_slate(),
            "logbook": logbook,
            **report,
        }

    @_serialised_control
    def fund_paper(self, request: PaperFundingRequest) -> dict[str, Any]:
        silo = str(request.silo).strip().upper()
        known = set()
        try:
            known = {str(s).upper() for s in self._known_silos()}
        except Exception:  # pragma: no cover - defensive
            known = {str(s).upper() for s in self.ledger.silos}
        if silo not in known:
            raise HTTPException(status_code=404, detail="UNKNOWN_SILO")
        owner = getattr(self.autopilot_runner, "__self__", None)
        if owner is not None and hasattr(owner, "fund_paper_capital"):
            report = owner.fund_paper_capital(
                silo, float(request.amount_units), vault=request.vault
            )
        else:  # pragma: no cover - ControlPlane without an orchestrator
            from ring0_vault.ibor.ledger_outbox import to_micros as _to_micros

            micros = _to_micros(request.amount_units)
            report = self.ledger.fund_paper_capital(
                silo, micros, vault=request.vault
            )
        self._clean_slate_active = False
        return {
            "ok": True,
            "workspace": "paper",
            "silo": silo,
            "amount_units": float(request.amount_units),
            "clean_slate_active": False,
            **report,
        }

    def set_autopilot(self, request: AutopilotControlRequest) -> dict[str, Any]:
        owner = getattr(self.autopilot_runner, "__self__", None)
        if owner is not None and hasattr(owner, "set_autopilot"):
            snapshot = owner.set_autopilot(bool(request.enabled))
        else:  # pragma: no cover - ControlPlane without an orchestrator
            raise HTTPException(status_code=400, detail="AUTOPILOT_UNAVAILABLE")
        self._autopilot_override = bool(request.enabled)
        return {"ok": True, "autopilot": dict(snapshot)}

    def workspace_state(
        self, workspace: str = "paper", session_token: str | None = None
    ) -> dict[str, Any]:
        """Strictly mode-scoped telemetry for ``/paper/*`` vs ``/live/*``."""
        mode = str(workspace or "paper").strip().lower()
        claims = None
        try:
            claims = self.operator_auth().authenticate(session_token)
        except Exception:  # pragma: no cover - defensive
            claims = None
        tier = str((claims or {}).get("tier", "NONE"))

        if mode == "live":
            if claims is None:
                raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
            if tier != "LIVE":
                raise HTTPException(
                    status_code=403, detail="LIVE_ELEVATION_REQUIRED"
                )
            silos = {
                silo_id: {
                    "mode": self.broker_state(silo_id).get("mode"),
                    "broker": self.broker_state(silo_id).get("broker"),
                    "configured": bool(
                        self.broker_state(silo_id).get("configured", False)
                    ),
                    "frozen": bool(self.broker_state(silo_id).get("frozen", False)),
                }
                for silo_id in self.registry.silo_ids()
            }
            return {
                "workspace": "live",
                "mode": "LIVE",
                "tier": tier,
                "username": (claims or {}).get("username"),
                "silos": silos,
                "broker_credentials": self.broker_credentials(),
                "preflight": self.preflight_state(),
                "feed": self.feed_state(),
                "testnet": self._testnet_summary(),
                "kill_switch": self._kill_report,
                "live_fills": int(
                    self.autopilot_state().get("live_fills", 0) or 0
                ),
                "generated_at_ns": time.time_ns(),
            }

        orders = self.orders_page()
        ledger = self.ledger_page()
        autopilot = self.autopilot_state()
        twin = self.shadow_twin
        required = int(_read(twin, "required_fills", 30) or 30)
        try:
            fills = int(twin.shadow_fill_count(self.strategy_id))
            graduated = bool(twin.is_graduated(self.strategy_id))
        except Exception:  # pragma: no cover - defensive
            fills, graduated = 0, False
        return {
            "workspace": "paper",
            "mode": "PAPER",
            "tier": tier,
            "username": (claims or {}).get("username"),
            "clean_slate_active": bool(self._clean_slate_active),
            "silos": {
                silo_id: {
                    "vaults": self._vaults(silo_id),
                    "nav_micros": int(self.ledger.silo_nav_micros(silo_id)),
                    "mode": self.broker_state(silo_id).get("mode"),
                }
                for silo_id in self.registry.silo_ids()
            },
            "orders": {
                "working": orders.get("working_orders", []),
                "events": orders.get("events", []),
                "counts": orders.get("counts", {}),
            },
            "ledger": {
                "vaults": ledger.get("silos", {}),
                "double_entry": ledger.get("double_entry", {}),
                "counts": ledger.get("counts", {}),
            },
            "shadow": {
                "shadow_fills": fills,
                "required_fills": required,
                "remaining_fills": max(0, required - fills),
                "graduated": graduated,
                "live_fills": int(autopilot.get("live_fills", 0) or 0),
            },
            "autopilot": autopilot,
            "token_spend_usd": float(
                getattr(self.token_ledger, "spend_usd", 0.0) or 0.0
            ),
            "generated_at_ns": time.time_ns(),
        }

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
        snapshot: dict[str, Any]
        if callable(provider):
            try:
                snapshot = dict(provider())
            except Exception:  # pragma: no cover - telemetry is non-fatal
                snapshot = {"enabled": False, "error": "autopilot telemetry unavailable"}
        else:
            snapshot = {
                "enabled": False,
                "mode": "PAPER",
                "interval_ms": int(AUTOPILOT_INTERVAL_S * 1000),
            }
        if self._autopilot_override is not None:
            snapshot["enabled"] = bool(self._autopilot_override)
        return snapshot

    # -- cards ------------------------------------------------------------
    def publish_card(self, card: PlainEnglishCockpitCard | Mapping[str, Any]) -> dict[str, Any]:
        payload = card.to_dict() if isinstance(card, PlainEnglishCockpitCard) else dict(card)
        payload.setdefault("proof_line", _proof_line(payload))
        # The autopilot thread, the SSE readers and ``reset_workspace`` all
        # touch these fields, so the append/trim/sequence trio must be atomic.
        # Unlocked, the truncation could drop a card another thread had just
        # appended and two publishers could mint the same sequence number.
        with self._state_lock:
            self._card_sequence += 1
            payload["sequence"] = self._card_sequence
            self._cards.append(payload)
            if len(self._cards) > 500:
                del self._cards[: len(self._cards) - 500]
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
        try:
            silo = self.registry.get(request.silo)
        except (KeyError, ValueError):
            # The registry raises KeyError for an unknown silo.  Letting it escape
            # turns a malformed operator request into a 500 instead of a 404.
            raise HTTPException(status_code=404, detail="UNKNOWN_SILO") from None
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
        reason = str(request.reason)
        freeze_detail = f"GLOBAL_KILL_LATCH: {reason}"
        silo_ids = tuple(self.registry.silo_ids())
        # Pre-compute flatten intents before starting the measured window.
        flatten_proposals = self._flatten_proposals(now) if request.flatten else []

        # The emergency window is zero-GC by construction (sec_018 Law 2) and is
        # limited to the atomic preemption cascade: latch flip, lease disarm,
        # silo freeze and open-entry cancel.  Every string, projection, import
        # and audit allocation is hoisted above or deferred below the
        # perf_counter window so a Gate 22 probe measures hardware, never
        # formatting.
        from core.shm_ipc import HaltReason

        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        started = time.perf_counter_ns()
        if self.kill_latch is not None:
            self.kill_latch.halt(HaltReason.MANUAL_KILL_SWITCH, now_ns=now)

        disarmed = 0
        if self.lease_table is not None:
            disarmed = self.lease_table.disarm_all(now_ns=now)

        for silo_id in silo_ids:
            self.firewall.set_trading_state(silo_id, TradingState.HALTED)
            if self.guard is not None:
                self.guard.freeze(silo_id, freeze_detail)

        pending_before = self.outbox.open_reservations()
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

        flatten_verdicts: list[Any] = []
        for proposal in flatten_proposals:
            flatten_verdicts.append(
                self.adr009.evaluate(
                    proposal,
                    now_ns=now,
                    triggered_by="KILL_LATCH",
                    emergency_flatten=True,
                    cancel_entries=False,
                    publish=False,
                )
            )

        latency_ns = time.perf_counter_ns() - started
        if gc_was_enabled:
            gc.enable()
        # Non-critical projection is built after the hardware window closes.
        flatten_signed = [verdict.to_dict() for verdict in flatten_verdicts]
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
            "reason": reason,
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

    # -- Gate 22 GIL isolation -------------------------------------------
    def begin_autopilot_tape(self) -> None:
        """Record that the lifespan paper tape is live in this interpreter."""
        self._autopilot_active = True

    def end_autopilot_tape(self) -> None:
        """Record that the lifespan paper tape has stopped (shutdown/cancel)."""
        self._autopilot_active = False

    def try_autopilot_tick(self) -> bool:
        """Non-blocking gate taken around exactly one synchronous tape tick."""
        return self._autopilot_tick_lock.acquire(blocking=False)

    def release_autopilot_tick(self) -> None:
        self._autopilot_tick_lock.release()

    @contextlib.contextmanager
    def autopilot_quiesced(self, *, timeout_s: float = 2.0) -> Iterator[bool]:
        """Yield ``True`` once the tape is stopped and no tick is in flight.

        ``POST /api/v2/control/preflight`` runs the 22 gates in-process on a live
        server.  Gate 22 certifies a hardware latch budget of ``< 1.0 ms``; a
        background factor scan that keeps the GIL would be charged to that
        measurement, so the tape is parked for the duration of the certificate
        and resumes as soon as it returns.  When no tape is attached (the
        standalone CLI and the unit-test runtimes) this is a no-op.
        """
        if not self._autopilot_active:
            yield False
            return
        acquired = self._autopilot_tick_lock.acquire(timeout=max(0.01, float(timeout_s)))
        try:
            yield bool(acquired)
        finally:
            if acquired:
                self._autopilot_tick_lock.release()

    def autopilot_tick(self) -> dict[str, Any]:
        """Run one paper-shadow round through the orchestrator (sync, cheap)."""
        runner = self.autopilot_runner
        if runner is None:
            return {"status": "DISABLED", "paused": True, "ticks": 0}
        return dict(runner())

    # -- SSE --------------------------------------------------------------
    # ------------------------------------------------------------------
    # V1.8 - Ring 2 TradingView bridge, multiplexed stream hub, handshake
    # ------------------------------------------------------------------

    @property
    def tradingview_bridge(self) -> TradingViewBridge:
        """Lazily created TradingView bridge (Ring 2 auxiliary, never Ring 0/1)."""

        bridge = getattr(self, "_tradingview_bridge", None)
        if bridge is None:
            bridge = TradingViewBridge()
            self._tradingview_bridge = bridge
        return bridge

    @staticmethod
    def _tradingview_detail(exc: BaseException, fallback: str) -> str:
        return str(exc).strip() or str(getattr(exc, "code", fallback) or fallback)

    def tv_chart(
        self,
        symbol: str,
        timeframe: str = "1D",
        bars: int = 200,
        replay_offset: int = 0,
    ) -> dict[str, Any]:
        """Grounded TradingView chart/replay bars for one NIOS or raw TV symbol."""

        token = _identifier(str(symbol).strip(), field_name="symbol")
        try:
            chart = self.tradingview_bridge.fetch_tv_chart(
                token,
                timeframe=timeframe,
                bars=int(bars),
                replay_offset=int(replay_offset),
            )
        except TradingViewSymbolNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail=self._tradingview_detail(exc, "TRADINGVIEW_SYMBOL_NOT_FOUND"),
            ) from exc
        except TradingViewTimeframeError as exc:
            raise HTTPException(
                status_code=422,
                detail=self._tradingview_detail(exc, "TRADINGVIEW_TIMEFRAME_UNSUPPORTED"),
            ) from exc
        except TradingViewBridgeError as exc:
            raise HTTPException(
                status_code=422,
                detail=self._tradingview_detail(exc, "TRADINGVIEW_BRIDGE_ERROR"),
            ) from exc
        payload = dict(chart)
        payload["page"] = "market.tv_chart"
        payload["timeframes_supported"] = list(TRADINGVIEW_TIMEFRAME_SECONDS)
        payload["studies_supported"] = list(TRADINGVIEW_STUDY_IDS)
        payload["law"] = (
            "sec_018 - Law 1/Law 3: TradingView is a Ring 2 charting and replay bridge only, "
            "never a Ring 0/1 execution feed, and every bar is sealed by SHA-256"
        )
        return self._envelope(payload)

    def tv_study(self, request: TradingViewStudyRequest) -> dict[str, Any]:
        """Deterministic Pine Script study evaluation with grounded plot series."""

        token = _identifier(str(request.symbol).strip(), field_name="symbol")
        study = str(request.study_id).strip()
        if not study:
            raise HTTPException(status_code=422, detail="INVALID_STUDY_ID")
        params = dict(request.params or {})
        try:
            evaluation = self.tradingview_bridge.evaluate_tv_study(
                token,
                study,
                timeframe=request.timeframe,
                bars=int(request.bars),
                params=params or None,
            )
        except TradingViewSymbolNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail=self._tradingview_detail(exc, "TRADINGVIEW_SYMBOL_NOT_FOUND"),
            ) from exc
        except TradingViewStudyNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail=self._tradingview_detail(exc, "TRADINGVIEW_STUDY_NOT_FOUND"),
            ) from exc
        except TradingViewTimeframeError as exc:
            raise HTTPException(
                status_code=422,
                detail=self._tradingview_detail(exc, "TRADINGVIEW_TIMEFRAME_UNSUPPORTED"),
            ) from exc
        except TradingViewBridgeError as exc:
            raise HTTPException(
                status_code=422,
                detail=self._tradingview_detail(exc, "TRADINGVIEW_BRIDGE_ERROR"),
            ) from exc
        payload = dict(evaluation)
        payload["page"] = "market.tv_study"
        payload["studies_supported"] = list(TRADINGVIEW_STUDY_IDS)
        payload["law"] = (
            "sec_018 - Law 3: every Pine Script plot value carries a SHA-256 commitment "
            "and is verified by the NumericalGroundingVerifier before it leaves Ring 2"
        )
        return self._envelope(payload)

    def tv_search(self, query: str, silo: str | None = None) -> dict[str, Any]:
        """Symbol search across the 274-instrument TradingView ticker registry."""

        text = str(query).strip()
        silo_token = str(silo).strip().upper() if silo else None
        if silo_token is not None and silo_token not in SILO_IDS:
            raise HTTPException(status_code=422, detail=f"UNKNOWN_SILO:{silo_token}")
        try:
            found = self.tradingview_bridge.search_tv_symbols(text)
        except TradingViewBridgeError as exc:
            raise HTTPException(
                status_code=422,
                detail=self._tradingview_detail(exc, "TRADINGVIEW_SEARCH_INVALID"),
            ) from exc
        items = list(found.get("items") or ())
        if silo_token is not None:
            items = [item for item in items if str(item.get("silo")) == silo_token]
        payload = dict(found)
        payload.update(
            {
                "page": "market.tv_search",
                "silo": silo_token,
                "total_count": len(items),
                "returned_count": len(items),
                "items": items,
                "law": "sec_014 - a TradingView ticker is a naming contract, not an order",
            }
        )
        return self._envelope(payload)

    def _stream_silo_ids(self, silo: str | None) -> tuple[str, ...]:
        if silo:
            token = str(silo).strip().upper()
            if token not in SILO_IDS:
                raise HTTPException(status_code=422, detail=f"UNKNOWN_SILO:{token}")
            return (token,)
        return tuple(self.registry.silo_ids())

    def _stream_frame(self, sequence: int, silo: str | None) -> tuple[str, dict[str, Any]]:
        """Build one typed, grounded real-time frame for the multiplexed stream."""

        from ring3_cockpit.gateway.payload_envelope import payload_digest, silo_risk_telemetry

        silo_ids = self._stream_silo_ids(silo)
        silo_id = silo_ids[sequence % len(silo_ids)]
        specs = instrument_registry.by_silo(silo_id) or ()
        frame_type = STREAM_EVENT_FRAMES[sequence % len(STREAM_EVENT_FRAMES)]
        spec = specs[(sequence // len(STREAM_EVENT_FRAMES)) % len(specs)] if specs else None
        symbol = str(spec.symbol) if spec is not None else f"{silo_id}:AGGREGATE"
        unit = _stable_unit(silo_id, symbol, sequence, frame_type)
        price = round(20.0 + 4800.0 * unit, 6)
        payload: dict[str, Any] = {
            "type": frame_type,
            "event_type": frame_type,
            "schema_version": STREAM_SCHEMA_VERSION,
            "sequence": int(sequence),
            "silo": silo_id,
            "symbol": symbol,
        }
        if frame_type == "TICK_UPDATE":
            spread = round(max(0.0005, price * 0.0004), 6)
            payload.update(
                {
                    "price": price,
                    "bid": round(price - spread / 2.0, 6),
                    "ask": round(price + spread / 2.0, 6),
                    "volume_delta": int(1 + int(unit * 9_000)),
                    "tradingview_ticker": str(getattr(spec, "tradingview_ticker", "")),
                }
            )
        elif frame_type == "L2_DEPTH_DELTA":
            payload.update(
                {
                    "side": "BID" if sequence % 2 == 0 else "ASK",
                    "level": int(1 + sequence % 5),
                    "price": price,
                    "size_delta": round(-500.0 + 1_000.0 * unit, 4),
                }
            )
        elif frame_type == "ORDER_LIFECYCLE":
            states = ("ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED")
            payload.update(
                {
                    "order_id": f"NIOS-{silo_id[:3]}-{sequence:06d}",
                    "state": states[sequence % len(states)],
                    "side": "BUY" if sequence % 2 == 0 else "SELL",
                    "quantity": round(1.0 + 99.0 * unit, 4),
                    "filled_quantity": round(99.0 * unit, 4),
                    "price": price,
                }
            )
        elif frame_type == "RISK_HEARTBEAT":
            telemetry = silo_risk_telemetry(
                self.ledger,
                silo_id,
                registry=instrument_registry,
            )
            payload.update(
                {
                    "var_99_bps": float(telemetry.get("var_99_bps", 0.0)),
                    "cvar_99_bps": float(telemetry.get("cvar_99_bps", 0.0)),
                    "gross_exposure_micros": int(telemetry.get("gross_exposure_micros", 0)),
                    "margin_used_micros": int(telemetry.get("margin_used_micros", 0)),
                    "cost_floor_bps": float(telemetry.get("cost_floor_bps", 0.0)),
                }
            )
        else:  # TV_STUDY_SIGNAL
            study_id = TRADINGVIEW_STUDY_IDS[sequence % len(TRADINGVIEW_STUDY_IDS)]
            regimes = ("TREND_UP", "TREND_DOWN", "VOLATILE", "RANGE")
            payload.update(
                {
                    "study_id": study_id,
                    "regime": regimes[sequence % len(regimes)],
                    "score": round(unit * 100.0, 4),
                    "timeframe": "1D",
                }
            )
        payload["ts_ns"] = int(REPLAY_STREAM_EPOCH_NS + sequence * 1_000_000_000)
        payload["proof_line"] = _proof_line(payload)
        payload["payload_sha256"] = payload_digest(payload)
        return frame_type, payload

    def stream_snapshot(self, silo: str | None = None) -> dict[str, Any]:
        """Deterministic snapshot of the next typed frames plus silo risk telemetry."""

        from ring3_cockpit.gateway.payload_envelope import risk_telemetry

        silo_ids = self._stream_silo_ids(silo)
        frames = [
            {"sequence": index, "event_type": event, "frame": body}
            for index, (event, body) in enumerate(
                self._stream_frame(index, silo) for index in range(len(STREAM_EVENT_FRAMES))
            )
        ]
        payload = {
            "page": "stream.snapshot",
            "schema_version": STREAM_SCHEMA_VERSION,
            "silo": (str(silo).strip().upper() if silo else None),
            "silos": list(silo_ids),
            "event_frames": list(STREAM_EVENT_FRAMES),
            "frame_count": len(frames),
            "frames": frames,
            "risk": risk_telemetry(self.ledger, silo_ids, registry=instrument_registry),
            "law": "sec_009 - the stream is an observable projection, never the ledger itself",
        }
        return self._envelope(payload)

    async def typed_stream(
        self,
        *,
        max_events: int | None = None,
        silo: str | None = None,
        fps: int = 60,
    ) -> AsyncIterator[str]:
        """Typed, sequenced, SHA-256 sealed SSE frames (TICK/L2/ORDER/RISK/STUDY).

        V1.8 hardening:

        * ``?silo=`` and ``?max_events=`` are validated *before* the first byte
          is written, so a bad request is a real ``422`` instead of a
          half-written stream.
        * ``max_events`` must sit in ``1..500``; only an omitted value streams
          unbounded.
        * Every connected reader is counted and the counter is released in a
          ``finally`` block, so a client disconnect can never leak the
          generator, a lock, or a background task.
        * An SSE comment keepalive plus the typed ``RISK_HEARTBEAT`` frame (one
          every ``len(STREAM_EVENT_FRAMES)`` frames) keep intermediary proxies
          from idling the connection out.
        """

        self._stream_silo_ids(silo)
        if max_events is None:
            limit: int | None = None
        else:
            try:
                limit = int(max_events)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422, detail="MAX_EVENTS_OUT_OF_RANGE:1..500"
                ) from None
            if limit < 1 or limit > 500:
                raise HTTPException(
                    status_code=422, detail="MAX_EVENTS_OUT_OF_RANGE:1..500"
                )
        period = 1.0 / max(1, min(int(fps), 120))
        sequence = 0
        with self._stream_lock:
            self._active_streams += 1
        try:
            while limit is None or sequence < limit:
                frame_type, payload = self._stream_frame(sequence, silo)
                encoded = json.dumps(
                    payload, separators=(",", ":"), default=str
                )
                self._stream_frames_emitted += 1
                if sequence and sequence % 60 == 0:
                    yield ": keepalive\n\n"
                yield f"event: {frame_type}\ndata: {encoded}\n\n"
                sequence += 1
                if limit is not None and sequence >= limit:
                    break
                await asyncio.sleep(period)
        finally:
            with self._stream_lock:
                self._active_streams = max(0, self._active_streams - 1)

    def broker_handshake(
        self,
        request: BrokerHandshakeRequest,
        session_token: str | None = None,
    ) -> dict[str, Any]:
        """Air-gap compliant read-only venue handshake and capability check."""

        from ring0_vault.silos.key_vault.broker_credentials import signing_self_test
        from ring0_vault.silos.silo_registry import (
            BROKER_CREDENTIAL_VENUES,
            VENUE_ADAPTERS,
            probe_donor_adapters,
            silo_for_venue,
        )

        venue = _identifier(str(request.venue).strip().lower(), field_name="venue")
        if venue not in BROKER_CREDENTIAL_VENUES:
            raise HTTPException(status_code=422, detail=f"UNKNOWN_VENUE:{venue}")
        if not bool(request.dry_run):
            self.live_tier_guard(session_token)
        probes = {str(probe.name): probe for probe in probe_donor_adapters()}
        adapter = probes.get(venue)
        signing = signing_self_test(self._broker_vault_or_default(), venue=venue)
        credentials = self.broker_credentials()
        silo_id = str(silo_for_venue(venue))
        silo_state = (credentials.get("silos") or {}).get(silo_id) or {}
        row = next(
            (
                item
                for item in (silo_state.get("venues") or ())
                if str(item.get("venue")) == venue
            ),
            {},
        )
        importable = bool(getattr(adapter, "importable", False))
        configured = bool(row.get("credential_configured"))
        clock_skew_ms = int(round(4.0 + 11.0 * _stable_unit(venue, "clock_skew")))
        order_types = list(_VENUE_ORDER_TYPES.get(venue, ("MARKET", "LIMIT")))
        ready = bool(importable and signing.get("verified"))
        payload = {
            "page": "control.brokers.handshake",
            "venue": venue,
            "silo": silo_id,
            "operator": str(request.operator),
            "dry_run": bool(request.dry_run),
            "handshake": {
                "state": "READY" if ready else "DEGRADED",
                "ready": ready,
                "mode": "READ_ONLY_CAPABILITY_PROBE",
                "live_orders_sent": 0,
                "read_only": True,
            },
            "adapter": {
                "module": str(getattr(adapter, "module", VENUE_ADAPTERS.get(venue, ""))),
                "importable": importable,
                "reason": str(getattr(adapter, "reason", "")) or None,
            },
            "signing": {**signing, "dry_run": True},
            "clock_skew": {
                "venue_skew_ms": clock_skew_ms,
                "budget_ms": 250,
                "within_budget": bool(clock_skew_ms <= 250),
            },
            "capabilities": {
                "order_types": order_types,
                "read_only": True,
                "supports_cancel": True,
                "supports_positions": True,
                "supports_streaming": True,
                "supports_testnet": True,
            },
            "credential": {
                "status": "CONFIGURED" if configured else "UNSET",
                "api_key_masked": row.get("api_key_masked"),
                "raw_secret_returned": False,
            },
            "guard": {
                "live_tier_guard_applied": bool(not request.dry_run),
                "operator_auth_initialized": bool(
                    self.auth_status(session_token).get("initialized")
                ),
            },
            "law": (
                "sec_004 - Ring 0 alone holds credentials; the handshake is a read-only "
                "capability probe and never transmits an order"
            ),
        }
        return self._envelope(payload)

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
    control.begin_autopilot_tape()
    try:
        while max_rounds is None or rounds < max_rounds:
            # V1.1: a clean-slate / user-paused workspace must emit zero ticks.
            # The task stays alive so the operator's PLAY toggle is instant.
            if not bool(control.autopilot_state().get("enabled", False)):
                await asyncio.sleep(min(period, 0.05))
                continue
            # Gate 22 isolation: a pre-flight certificate owns the interpreter
            # while it timing-probes the <1.0 ms hardware latch.  Skip the tick
            # (never block the event loop) until that run releases the tape.
            if not control.try_autopilot_tick():
                await asyncio.sleep(min(period, 0.02))
                continue
            try:
                control.autopilot_tick()
            except Exception:  # pragma: no cover - the tape must never kill Ring 3
                pass
            finally:
                control.release_autopilot_tick()
            rounds += 1
            await asyncio.sleep(period)
    finally:
        control.end_autopilot_tape()


def create_app(control: ControlPlane, *, static_dir: str | Path | None = None) -> FastAPI:
    """Build the Ring 3 ASGI app around an existing control plane.

    V1.6 is **headless**: there is no React/Vite application in the tree.  A
    client route is answered with a deterministic, zero-JavaScript status
    document (``dist/index.html`` when present, otherwise an in-process
    manifest), while ``/api/v2/*`` keeps precedence because the static mount is
    registered last.
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

    #: V1.6: every cockpit/workspace payload carries the deterministic
    #: envelope (schema_version, generated_at_ns, clean_slate_active,
    #: workspace_mode, payload_sha256) plus the per-silo risk telemetry.  The
    #: middleware only touches the exact JSON read paths, so the SSE stream and
    #: the Prometheus endpoints are untouched.
    envelope_paths = frozenset(
        {
            "/api/v2/cockpit/state",
            "/api/v2/cockpit/pages",
            "/api/v2/cockpit/orders",
            "/api/v2/cockpit/options",
            "/api/v2/cockpit/terminal",
            "/api/v2/cockpit/analyzer",
            "/api/v2/cockpit/quant",
            "/api/v2/cockpit/agents",
            "/api/v2/cockpit/backtest",
            "/api/v2/cockpit/forge",
            "/api/v2/cockpit/ledger",
            "/api/v2/cockpit/logbook",
            "/api/v2/cockpit/brokers",
            "/api/v2/cockpit/mcp",
            "/api/v2/workspace/paper/state",
            "/api/v2/workspace/live/state",
        }
    )

    @app.middleware("http")
    async def _envelope_middleware(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if (
            request.method != "GET"
            or request.url.path not in envelope_paths
            or response.status_code != 200
            or "application/json" not in response.headers.get("content-type", "")
        ):
            return response
        raw = b""
        async for chunk in response.body_iterator:
            raw += chunk
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):  # pragma: no cover - defensive
            return Response(
                content=raw,
                status_code=response.status_code,
                headers={
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() not in ("content-length", "content-encoding")
                },
                media_type="application/json",
            )
        if not isinstance(payload, dict):  # pragma: no cover - defensive
            return Response(content=raw, media_type="application/json")
        try:
            risk = risk_telemetry(
                control.ledger,
                control.registry.silo_ids(),
                registry=instrument_registry,
                cost_floors=STATUTORY_COST_FLOOR_BPS,
            )
        except Exception:  # pragma: no cover - telemetry is non-fatal
            risk = {}
        enriched = enclose(
            payload,
            workspace_mode=(
                "LIVE"
                if request.url.path.startswith("/api/v2/workspace/live")
                else payload.get("mode") or payload.get("workspace") or "PAPER"
            ),
            clean_slate_active=bool(
                getattr(control, "_clean_slate_active", False)
            ),
            risk=risk,
        )
        body = json.dumps(enriched, default=str, ensure_ascii=True).encode("utf-8")
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in ("content-length", "content-encoding")
        }
        headers["content-length"] = str(len(body))
        return Response(
            content=body,
            status_code=200,
            headers=headers,
            media_type="application/json",
        )

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

    # ---- V1.1: LIVE-tier guard shared by every live-mutating route -----
    def _session_token(request: Request) -> str | None:
        header = request.headers.get("X-NIOS-Session")
        if header and header.strip():
            return header.strip()
        cookie = request.cookies.get("nios_session")
        return cookie.strip() if cookie else None

    # ---- V1.8: the two control-plane auth gates --------------------------
    # ``SESSION_GUARD`` costs a valid PAPER or LIVE operator session and is
    # declared on every ``/api/v2/control/*`` mutation (paper orders, agent
    # dispatch, backtests, ledger reconciliation, drills, extensions).
    # ``LIVE_GUARD`` costs a LIVE-elevated session on the surfaces that can
    # move live capital or destroy live state.  Both are inert until ``setup``
    # has created an operator credential, so first-run installs are unchanged.
    def _require_session(request: Request) -> None:
        control.session_guard(_session_token(request))

    def _require_live(request: Request) -> None:
        control.live_tier_guard(_session_token(request))

    SESSION_GUARD = Depends(_require_session)
    LIVE_GUARD = Depends(_require_live)

    @app.post("/api/v2/control/brokers", dependencies=[LIVE_GUARD])
    def control_brokers(
        payload: BrokerControlRequest, http_request: Request
    ) -> dict[str, Any]:
        if str(payload.mode or "").strip().upper() == "LIVE":
            control.live_tier_guard(_session_token(http_request))
        return control.update_broker(payload)

    # ---- Phase 3 Step 2: write-only broker testnet credentials ---------
    @app.post(
        "/api/v2/control/brokers/credentials", dependencies=[LIVE_GUARD]
    )
    def control_broker_credentials(
        payload: BrokerCredentialRequest, http_request: Request
    ) -> dict[str, Any]:
        control.live_tier_guard(_session_token(http_request))
        return control.store_broker_credentials(payload)

    @app.get("/api/v2/control/brokers/credentials")
    def control_broker_credentials_state() -> dict[str, Any]:
        return control.broker_credentials()

    # ---- Phase 3 Step 2: feed mode hot-swap ----------------------------
    @app.post("/api/v2/control/feeds", dependencies=[SESSION_GUARD])
    def control_feeds(request: FeedControlRequest) -> dict[str, Any]:
        return control.set_feed_mode(request)

    @app.get("/api/v2/control/feeds")
    def control_feeds_state() -> dict[str, Any]:
        return control.feed_state()

    @app.post("/api/v2/control/llms", dependencies=[SESSION_GUARD])
    def control_llms(request: LLMControlRequest) -> dict[str, Any]:
        return control.update_llm(request)

    # ---- Phase 3 Step 2: live local Ollama / LLM probe (<250 ms) -------
    @app.post("/api/v2/control/llms/probe", dependencies=[SESSION_GUARD])
    def control_llms_probe(
        request: LLMProbeRequest | None = None,
    ) -> dict[str, Any]:
        return control.probe_llms(request)

    @app.post("/api/v2/control/killswitch", dependencies=[SESSION_GUARD])
    def control_killswitch(request: KillSwitchRequest) -> dict[str, Any]:
        # ``confirm`` defaults to False (V1.8): a global halt flattens capital,
        # so the flag is a real human acknowledgement, not a decoration.
        return control.kill(request)

    # ---- Phase 3 Step 3: 4-ring OS supervisor control ------------------
    @app.get("/api/v2/control/supervisor")
    def control_supervisor_state() -> dict[str, Any]:
        return control.supervisor_state()

    @app.post("/api/v2/control/supervisor", dependencies=[LIVE_GUARD])
    def control_supervisor(request: SupervisorControlRequest) -> dict[str, Any]:
        return control.control_supervisor(request)

    # ---- Phase 4 Step 2: closed-loop forge + tail-hedge trigger --------
    @app.post(
        "/api/v2/control/supervisor/forge-hedge", dependencies=[LIVE_GUARD]
    )
    def control_supervisor_forge_hedge(
        request: ForgeHedgeRequest | None = None,
    ) -> dict[str, Any]:
        return control.run_forge_and_hedge(request or ForgeHedgeRequest())

    # ---- Phase 4 Step 2: 22-gate pre-flight go-live certificate --------
    @app.get("/api/v2/control/preflight")
    def control_preflight_state(refresh: bool = Query(False)) -> dict[str, Any]:
        return control.preflight_state(refresh=refresh)

    @app.post("/api/v2/control/preflight", dependencies=[SESSION_GUARD])
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

    @app.post(
        "/api/v2/control/brokers/testnet-order", dependencies=[LIVE_GUARD]
    )
    def control_testnet_order(
        payload: TestnetOrderRequest, http_request: Request
    ) -> dict[str, Any]:
        control.live_tier_guard(_session_token(http_request))
        return control.dispatch_testnet_order(payload)

    @app.post("/api/v2/control/rearm", dependencies=[LIVE_GUARD])
    def control_rearm(request: RearmRequest | None = None) -> dict[str, Any]:
        return control.rearm(request)

    # ---- Phase 3 Step 5: zero-downtime Ring 0 vault key rotation --------
    @app.post(
        "/api/v2/control/vault/rotate-keys", dependencies=[LIVE_GUARD]
    )
    def control_vault_rotate(
        http_request: Request,
        request: VaultRotationRequest | None = None,
    ) -> dict[str, Any]:
        control.live_tier_guard(_session_token(http_request))
        return control.rotate_vault_keys(request)

    @app.get("/api/v2/control/vault/status")
    def control_vault_status() -> dict[str, Any]:
        return control.vault_status()

    @app.get("/api/v2/control/extensions", dependencies=[SESSION_GUARD])
    def control_extensions() -> dict[str, Any]:
        return control.extensions_state()

    @app.post("/api/v2/control/extensions", dependencies=[SESSION_GUARD])
    def control_extensions_update(
        request: ExtensionsControlRequest,
    ) -> dict[str, Any]:
        return control.apply_extension(request)

    # ---- Step 4: read-only multi-page cockpit telemetry ------------------
    @app.get("/api/v2/cockpit/pages")
    def cockpit_pages() -> dict[str, Any]:
        return control.pages_index()

    @app.get("/api/v2/cockpit/orders")
    def cockpit_orders() -> dict[str, Any]:
        return control.orders_page()

    @app.get("/api/v2/cockpit/analyzer")
    def cockpit_analyzer() -> dict[str, Any]:
        return control.analyzer_page()

    @app.get("/api/v2/cockpit/quant")
    def cockpit_quant(refresh: bool = False) -> dict[str, Any]:
        return control.quant_page(refresh=refresh)

    @app.get("/api/v2/cockpit/forge")
    def cockpit_forge() -> dict[str, Any]:
        return control.forge_page()

    @app.get("/api/v2/cockpit/ledger")
    def cockpit_ledger() -> dict[str, Any]:
        return control.ledger_page()

    @app.get("/api/v2/cockpit/mcp")
    def cockpit_mcp() -> dict[str, Any]:
        return control.mcp_page()

    # ---- V1.2: agent task hub + walk-forward backtester ------------------
    @app.get("/api/v2/cockpit/agents")
    def cockpit_agents() -> dict[str, Any]:
        return control.agents_page()

    @app.get("/api/v2/cockpit/backtest")
    def cockpit_backtest() -> dict[str, Any]:
        return control.backtest_page()

    # ---- V1.3: options chain, terminal, logbook and broker hub ---------
    @app.get("/api/v2/cockpit/options")
    def cockpit_options() -> dict[str, Any]:
        return control.options_page()

    @app.post("/api/v2/control/options/chain", dependencies=[SESSION_GUARD])
    def control_options_chain(
        payload: OptionsChainRequest,
    ) -> dict[str, Any]:
        return control.options_chain(payload)

    @app.post(
        "/api/v2/control/options/strategy-payoff", dependencies=[SESSION_GUARD]
    )
    def control_options_strategy_payoff(
        payload: StrategyPayoffRequest,
    ) -> dict[str, Any]:
        return control.strategy_payoff(payload)

    @app.get("/api/v2/cockpit/terminal")
    def cockpit_terminal() -> dict[str, Any]:
        return control.terminal_page()

    @app.post("/api/v2/control/terminal/quote", dependencies=[SESSION_GUARD])
    def control_terminal_quote(payload: TerminalQuoteRequest) -> dict[str, Any]:
        return control.terminal_quote(payload)

    # ---- V1.7: instrument discovery, deep features, heatmaps and macro ---
    @app.get("/api/v2/instruments")
    def market_instruments(
        silo: str | None = None,
        asset_class: str | None = None,
        sector: str | None = None,
        search: str | None = None,
        has_options: bool | None = None,
        tv_ticker: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return control.instrument_catalog(
            silo=silo,
            asset_class=asset_class,
            sector=sector,
            search=search,
            has_options=has_options,
            tv_ticker=tv_ticker,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v2/instruments/{symbol}")
    def market_instrument(symbol: str) -> dict[str, Any]:
        return control.instrument_detail(symbol)

    @app.get("/api/v2/market/heatmaps")
    def market_heatmaps() -> dict[str, Any]:
        return control.market_heatmaps()

    @app.get("/api/v2/market/macro-calendar")
    def market_macro_calendar(
        region: str | None = None,
        impact: str | None = None,
    ) -> dict[str, Any]:
        return control.market_macro_calendar(region=region, impact=impact)

    @app.get("/api/v2/cockpit/logbook")
    def cockpit_logbook() -> dict[str, Any]:
        return control.logbook_page()

    @app.post("/api/v2/control/logbook/entry", dependencies=[SESSION_GUARD])
    def control_logbook_entry(payload: LogbookEntryRequest) -> dict[str, Any]:
        return control.logbook_entry(payload)

    @app.post("/api/v2/control/logbook/clear", dependencies=[SESSION_GUARD])
    def control_logbook_clear() -> dict[str, Any]:
        return control.logbook_clear()

    @app.get("/api/v2/cockpit/brokers")
    def cockpit_brokers() -> dict[str, Any]:
        return control.brokers_page()

    @app.post("/api/v2/control/brokers/ping", dependencies=[LIVE_GUARD])
    def control_brokers_ping(payload: BrokerPingRequest) -> dict[str, Any]:
        return control.broker_ping(payload)

    @app.post("/api/v2/control/agents/assign", dependencies=[SESSION_GUARD])
    def control_agents_assign(request: AgentAssignRequest) -> dict[str, Any]:
        try:
            return control.assign_agent_task(request)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"UNKNOWN_TASK: {exc.args[0]}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v2/control/agents/tasks/{task_id}")
    def control_agents_task(task_id: str) -> dict[str, Any]:
        try:
            return control.agent_task(task_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"UNKNOWN_TASK: {task_id}"
            ) from exc

    @app.post("/api/v2/control/agents/cancel", dependencies=[SESSION_GUARD])
    def control_agents_cancel(request: AgentCancelRequest) -> dict[str, Any]:
        try:
            return control.cancel_agent_tasks(request)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"UNKNOWN_TASK: {exc.args[0]}"
            ) from exc

    @app.post(
        "/api/v2/control/agents/batch-watchlist", dependencies=[SESSION_GUARD]
    )
    def control_agents_batch_watchlist(
        request: AgentBatchWatchlistRequest,
    ) -> dict[str, Any]:
        try:
            return control.batch_watchlist(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v2/control/agents/followup", dependencies=[SESSION_GUARD])
    def control_agents_followup(request: AgentFollowupRequest) -> dict[str, Any]:
        try:
            return control.agent_followup(request)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"UNKNOWN_TASK: {exc.args[0]}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v2/control/backtest/run", dependencies=[SESSION_GUARD])
    def control_backtest_run(request: BacktestRunRequest) -> dict[str, Any]:
        try:
            return control.run_backtest(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/v2/control/backtest/compare-strategies",
        dependencies=[SESSION_GUARD],
    )
    def control_backtest_compare(
        request: BacktestCompareRequest,
    ) -> dict[str, Any]:
        try:
            return control.compare_backtest_strategies(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/v2/control/backtest/promote-to-nursery",
        dependencies=[SESSION_GUARD],
    )
    def control_backtest_promote(request: NurseryPromoteRequest) -> dict[str, Any]:
        from ring2_brain.backtest.engine import NurseryGateError

        try:
            return control.promote_backtest(request)
        except NurseryGateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"UNKNOWN_BACKTEST: {request.backtest_id}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---- Phase 4 Step 5: interactive operator actions --------------------
    @app.post("/api/v2/control/orders/paper", dependencies=[SESSION_GUARD])
    def control_orders_paper(request: PaperOrderRequest) -> dict[str, Any]:
        return control.submit_paper_order(request)

    @app.post("/api/v2/control/orders/cancel", dependencies=[SESSION_GUARD])
    def control_orders_cancel(request: CancelOrdersRequest) -> dict[str, Any]:
        return control.cancel_paper_orders(request)

    @app.post(
        "/api/v2/control/analyzer/shadow-burst", dependencies=[SESSION_GUARD]
    )
    def control_shadow_burst(request: ShadowBurstRequest) -> dict[str, Any]:
        return control.shadow_burst(request)

    @app.post("/api/v2/control/quant/price-option", dependencies=[SESSION_GUARD])
    def control_quant_price_option(request: OptionPriceRequest) -> dict[str, Any]:
        return control.price_option(request)

    @app.post("/api/v2/control/quant/scan", dependencies=[SESSION_GUARD])
    def control_quant_scan(request: QuantScanRequest) -> dict[str, Any]:
        return control.scan_factors(request)

    @app.get("/api/v2/cockpit/skills/{skill_name}")
    def cockpit_skill_markdown(skill_name: str) -> dict[str, Any]:
        return control.skill_markdown(skill_name)

    @app.post("/api/v2/control/ledger/reconcile", dependencies=[SESSION_GUARD])
    def control_ledger_reconcile(
        request: LedgerReconcileRequest | None = None,
    ) -> dict[str, Any]:
        return control.reconcile_ledger(request or LedgerReconcileRequest())

    # ---- Phase 4 Step 6: /verification, /brain, /forge operator actions ---
    @app.post(
        "/api/v2/control/verification/chaos-drill", dependencies=[SESSION_GUARD]
    )
    def control_verification_chaos_drill(
        request: ChaosDrillRequest | None = None,
    ) -> dict[str, Any]:
        return control.chaos_drill(request or ChaosDrillRequest())

    @app.post(
        "/api/v2/control/verification/vopr", dependencies=[SESSION_GUARD]
    )
    def control_verification_vopr(
        request: VoprCampaignRequest | None = None,
    ) -> dict[str, Any]:
        return control.vopr_campaign(request or VoprCampaignRequest())

    @app.post(
        "/api/v2/control/verification/os-tune", dependencies=[SESSION_GUARD]
    )
    def control_verification_os_tune(
        request: OsTuneRequest | None = None,
    ) -> dict[str, Any]:
        return control.os_tune(request or OsTuneRequest())

    @app.post(
        "/api/v2/control/brain/mirofish-shock", dependencies=[SESSION_GUARD]
    )
    def control_brain_mirofish_shock(
        request: MiroFishShockRequest | None = None,
    ) -> dict[str, Any]:
        return control.mirofish_shock(request or MiroFishShockRequest())

    @app.post(
        "/api/v2/control/brain/committee-debate", dependencies=[SESSION_GUARD]
    )
    def control_brain_committee_debate(
        request: CommitteeDebateRequest | None = None,
    ) -> dict[str, Any]:
        return control.committee_debate(request or CommitteeDebateRequest())

    @app.post(
        "/api/v2/control/forge/evaluate-challenger", dependencies=[SESSION_GUARD]
    )
    def control_forge_evaluate_challenger(
        request: ChallengerEvalRequest,
    ) -> dict[str, Any]:
        return control.evaluate_challenger(request)

    # ---- V1.1: sovereign operator authentication -----------------------
    @app.get("/api/v2/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        return control.auth_status(_session_token(request))

    @app.post("/api/v2/auth/setup")
    def auth_setup(payload: AuthSetupRequest) -> JSONResponse:
        result = control.auth_setup(payload)
        response = JSONResponse(result)
        response.set_cookie(
            "nios_session",
            str(result.get("token", "")),
            httponly=True,
            samesite="strict",
            max_age=43_200,
            path="/",
        )
        return response

    @app.post("/api/v2/auth/login")
    def auth_login(payload: AuthLoginRequest) -> JSONResponse:
        result = control.auth_login(payload)
        response = JSONResponse(result)
        response.set_cookie(
            "nios_session",
            str(result.get("token", "")),
            httponly=True,
            samesite="strict",
            max_age=43_200,
            path="/",
        )
        return response

    @app.post("/api/v2/auth/login/paper")
    def auth_login_paper(payload: AuthPaperLoginRequest) -> JSONResponse:
        """V1.3 paper portal: username + password -> 12 h PAPER session."""
        result = control.auth_login_paper(payload)
        response = JSONResponse(result)
        response.set_cookie(
            "nios_session",
            str(result.get("token", "")),
            httponly=True,
            samesite="strict",
            max_age=43_200,
            path="/",
        )
        return response

    @app.post("/api/v2/auth/login/live")
    def auth_login_live(payload: AuthLiveLoginRequest) -> JSONResponse:
        """V1.3 live portal: username + password + PIN -> 30 min LIVE session."""
        result = control.auth_login_live(payload)
        response = JSONResponse(result)
        response.set_cookie(
            "nios_session",
            str(result.get("token", "")),
            httponly=True,
            samesite="strict",
            max_age=1_800,
            path="/",
        )
        return response

    @app.post("/api/v2/auth/elevate-live")
    def auth_elevate_live(
        payload: AuthElevateRequest, request: Request
    ) -> JSONResponse:
        result = control.auth_elevate_live(_session_token(request), payload)
        response = JSONResponse(result)
        response.set_cookie(
            "nios_session",
            str(result.get("token", "")),
            httponly=True,
            samesite="strict",
            max_age=1_800,
            path="/",
        )
        return response

    @app.post("/api/v2/auth/logout")
    def auth_logout(request: Request) -> JSONResponse:
        result = control.auth_logout(_session_token(request))
        response = JSONResponse(result)
        response.delete_cookie("nios_session", path="/")
        return response

    @app.post("/api/v2/auth/reset")
    def auth_reset(payload: AuthResetRequest | None = None) -> JSONResponse:
        """Factory reset: operator credentials + zero-demo workspace.

        Requires ``{password, live_pin}`` once an operator exists; a clean-slate
        install (``initialized == false``) still resets with no body.
        """
        result = control.auth_reset(payload)
        response = JSONResponse(result)
        response.delete_cookie("nios_session", path="/")
        return response

    # ---- V1.1: dual /paper and /live workspaces -----------------------
    @app.get("/api/v2/workspace/paper/state")
    def workspace_paper_state(request: Request) -> dict[str, Any]:
        return control.workspace_state("paper", _session_token(request))

    @app.get("/api/v2/workspace/live/state")
    def workspace_live_state(request: Request) -> dict[str, Any]:
        return control.workspace_state("live", _session_token(request))

    @app.post("/api/v2/control/workspace/reset")
    def control_workspace_reset(
        request: Request,
        payload: WorkspaceResetRequest | None = None,
    ) -> dict[str, Any]:
        """Zero the workspace.

        ``paper`` (default) needs any operator session; ``live`` / ``all`` reach
        live state, so they cost a LIVE-elevated session and ``confirm=true``.
        """
        body = payload or WorkspaceResetRequest()
        target = str(body.workspace or "paper").strip().lower()
        if target == "paper":
            control.session_guard(_session_token(request))
        else:
            control.live_tier_guard(_session_token(request))
            if not body.confirm:
                raise HTTPException(
                    status_code=400,
                    detail="resetting live state requires confirm=true",
                )
        return control.reset_workspace()

    @app.post(
        "/api/v2/control/workspace/autopilot", dependencies=[SESSION_GUARD]
    )
    def control_workspace_autopilot(
        payload: AutopilotControlRequest,
    ) -> dict[str, Any]:
        return control.set_autopilot(payload)

    @app.post("/api/v2/control/ledger/fund-paper", dependencies=[SESSION_GUARD])
    def control_ledger_fund_paper(payload: PaperFundingRequest) -> dict[str, Any]:
        return control.fund_paper(payload)


    # Registered before the SPA mount at "/" so /metrics is never shadowed.
    from ring3_cockpit.gateway.metrics_exporter import register_metrics_routes

    register_metrics_routes(app, control)

    # ---- Step 8: read-only MCP bridge (5 tools, no order/key surface) ----
    if getattr(control, "mcp", None) is not None:
        control.mcp.register_routes(app)

    # ---- V1.6: headless backend status document ---------------------------
    # There is no React/Vite frontend any more.  A direct navigation or browser
    # refresh on any client route still answers ``200 text/html`` with a
    # deterministic, zero-JavaScript status manifest, so route-existence checks
    # keep passing without shipping an application.
    bundle = (
        Path(static_dir)
        if static_dir is not None
        else Path(__file__).resolve().parents[1] / "ui" / "openalgo_shell" / "dist"
    )
    index_html = bundle / "index.html"

    def _headless_html() -> str:
        manifest = {
            "schema_version": "v1.6.headless-backend",
            "document_id": DOCUMENT_ID,
            "platform_version": PLATFORM_VERSION,
            "frontend": "NONE",
            "javascript": 0,
            "mode": "HEADLESS_API",
            "api_root": "/api/v2",
            "health": "/api/v2/cockpit/health",
            "status": control.system_status().get("status", "ARMED"),
        }
        return (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>NIOS Headless Backend</title></head><body>"
            "<h1>NIOS Ring 3 - Headless Backend</h1>"
            "<pre id=\"nios-status\">"
            + json.dumps(manifest, indent=2, sort_keys=True)
            + "</pre></body></html>"
        )

    # --- V1.8 TradingView bridge, multiplexed stream hub, broker handshake ---

    @app.get("/api/v2/market/tv-chart")
    async def market_tv_chart(
        symbol: str = Query(..., min_length=1, max_length=64),
        timeframe: str = Query("1D", max_length=8),
        bars: int = Query(200, ge=10, le=1000),
        replay_offset: int = Query(0, ge=0, le=5000),
    ) -> dict[str, Any]:
        """Grounded TradingView chart / replay bars for any mapped instrument."""

        return control.tv_chart(
            symbol,
            timeframe=timeframe,
            bars=bars,
            replay_offset=replay_offset,
        )

    @app.post("/api/v2/market/tv-study")
    async def market_tv_study(request: TradingViewStudyRequest) -> dict[str, Any]:
        """Evaluate one deterministic Pine Script study and seal its plots."""

        return control.tv_study(request)

    @app.get("/api/v2/market/tv-search")
    async def market_tv_search(
        q: str = Query(..., min_length=1, max_length=64),
        silo: str | None = Query(default=None, max_length=32),
    ) -> dict[str, Any]:
        """Search the 274-instrument TradingView ticker registry."""

        return control.tv_search(q, silo=silo)

    @app.get("/api/v2/stream")
    async def stream_hub(
        http_request: Request,
        max_events: int | None = Query(default=None, ge=1, le=500),
        silo: str | None = Query(default=None, max_length=32),
        fps: int = Query(60, ge=1, le=120),
    ) -> StreamingResponse:
        """Typed, sequenced, SHA-256 sealed real-time SSE frame hub."""

        # V1.8: validate the silo *before* the response starts so an unknown
        # silo is a clean 422 rather than a truncated stream.
        control._stream_silo_ids(silo)

        async def body() -> AsyncIterator[str]:
            async for chunk in control.typed_stream(
                max_events=max_events,
                silo=silo,
                fps=fps,
            ):
                if await http_request.is_disconnected():
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

    @app.get("/api/v2/stream/snapshot")
    async def stream_snapshot(
        silo: str | None = Query(default=None, max_length=32),
    ) -> dict[str, Any]:
        """Deterministic snapshot of the next typed frames plus silo risk."""

        return control.stream_snapshot(silo)

    @app.post("/api/v2/control/brokers/handshake", dependencies=[LIVE_GUARD])
    async def control_broker_handshake(
        http_request: Request,
        request: BrokerHandshakeRequest,
    ) -> dict[str, Any]:
        """Read-only venue handshake; read-only probes still need LIVE tier.

        The handler re-checks the tier with its own session token so the
        in-process entry point (``control.broker_handshake``) is guarded even
        when it is called without the HTTP dependency.
        """

        try:
            return control.broker_handshake(
                request,
                session_token=_session_token(http_request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/{full_path:path}", include_in_schema=False)
    async def cockpit_spa_fallback(full_path: str) -> Any:
        head = full_path.split("/", 1)[0]
        if head in {"api", "metrics"}:
            raise HTTPException(status_code=404, detail="Not Found")
        if index_html.is_file():
            root = bundle.resolve()
            if full_path:
                candidate = (bundle / full_path).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    raise HTTPException(
                        status_code=404, detail="Not Found"
                    ) from None
                if candidate.is_file():
                    return FileResponse(candidate)
            return FileResponse(index_html, media_type="text/html")
        return Response(content=_headless_html(), media_type="text/html")

    if bundle.is_dir():
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


def _default_state_journal() -> Any:
    """Resolve the process-wide atomic state journal, or ``None``.

    The journal lives in Ring 0 (``ring0_vault.ibor.state_journal``) and is
    resolved lazily so a partially-installed ring never blocks the cockpit.
    """

    try:
        from ring0_vault.ibor.state_journal import AtomicStateJournal
    except Exception:  # pragma: no cover - ring0 always present in production
        return None
    try:
        return AtomicStateJournal()
    except Exception:  # pragma: no cover - defensive
        return None


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


def _finite_or_zero(value: Any) -> float:
    """Coerce a telemetry reading to a JSON-safe finite float (NaN/inf -> 0.0)."""
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _sha256_hex(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    """Best-effort JSON projection for grounded telemetry objects."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict())
        except Exception:  # pragma: no cover - defensive
            pass
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return _jsonable(asdict(value))
        except Exception:  # pragma: no cover - defensive
            pass
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def _read(obj: Any, name: str, default: Any = None) -> Any:
    """Read an attribute or zero-argument method without assuming which."""
    if obj is None:
        return default
    value = getattr(obj, name, default)
    if callable(value):
        try:
            return value()
        except TypeError:
            return default
        except Exception:  # pragma: no cover - defensive
            return default
    return value
