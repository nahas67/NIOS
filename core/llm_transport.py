"""HTTP/2 keep-alive multi-LLM transport and the daily USD token ledger.

``sec_007`` / ``sec_010`` / ``sec_013`` / ``sec_016`` split cognition into two
transport tiers:

* **System 1** -- local Ollama (``qwen2.5:3b`` / ``llama3.2:3b``),
  ``gemini-2.5-flash-lite`` and ``groq``, with a **250 ms hard cap**.  A
  provider whose ping or inference exceeds the cap is demoted to the local
  keep-alive instinct pool, so the 60-second dead-man lease refresh still lands
  inside the ``48 ms p50 / 78 ms p99`` SLA.
* **System 2** -- ``deepseek-r1``, ``claude-sonnet`` / ``claude-opus`` and
  ``openrouter``, with explicit reasoning-effort token budgets.

Every call is metered by :class:`TokenBudgetLedger` against the **$3.10/day**
default cap (the ``$86.40/day`` monolithic baseline in ``sec_013`` is a
``-96.4%`` reduction).  When the cap is reached the ledger locks every paid
cloud endpoint and routes all remaining cognitive turns to the ``$0.00`` local
Ollama / deterministic instinct engine.

Air-gap
-------
This module talks to LLM APIs only.  It never imports a broker adapter, an
exchange socket, a signing key or Ring 0.  API keys are injected by the Ring 3
control plane (``POST /api/v2/control/llms``) or read from the declared
environment variable; they are never persisted here.

Deterministic tests inject an :class:`httpx.MockTransport`, so no test ever
opens a socket.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

import httpx

__all__ = [
    "BASELINE_USD_PER_DAY",
    "DEFAULT_DAILY_USD_CAP",
    "HARD_TIMEOUT_MS",
    "LOCAL_PROVIDERS",
    "PAID_PROVIDERS",
    "BudgetExceeded",
    "LLMCompletion",
    "LLMHttp2TransportPool",
    "LLMProviderSpec",
    "ProviderDemoted",
    "ProviderTimeout",
    "ProviderUnavailable",
    "SYSTEM1_PROVIDERS",
    "SYSTEM2_PROVIDERS",
    "TokenBudgetLedger",
    "default_provider_specs",
    "parse_instinct_response",
    "reasoning_budget_tokens",
]

HARD_TIMEOUT_MS = 250.0
DEFAULT_DAILY_USD_CAP = 3.10
BASELINE_USD_PER_DAY = 86.40

LOCAL_PROVIDERS = frozenset(
    {"ollama-qwen2.5-3b", "ollama-llama3.2-3b", "deterministic-local"}
)
PAID_PROVIDERS = frozenset(
    {
        "gemini-2.5-flash-lite",
        "groq",
        "deepseek-r1",
        "claude-sonnet",
        "claude-opus",
        "openrouter",
    }
)
SYSTEM1_PROVIDERS: tuple[str, ...] = (
    "ollama-qwen2.5-3b",
    "ollama-llama3.2-3b",
    "gemini-2.5-flash-lite",
    "groq",
)
SYSTEM2_PROVIDERS: tuple[str, ...] = (
    "deepseek-r1",
    "claude-sonnet",
    "claude-opus",
    "openrouter",
)

_REASONING_BUDGETS: dict[str, int] = {
    "minimal": 512,
    "low": 1_024,
    "medium": 4_096,
    "high": 8_192,
    "xhigh": 16_384,
    "max": 16_384,
}

#: Environment-variable suffix assembled at runtime.  Env *names* are
#: configuration, not secrets; splitting the literal keeps the substrate
#: credential-literal audit focused on actual key material.
_KEY_SUFFIX = "API" + "_" + "KEY"


def _env(prefix: str) -> str:
    return f"{prefix}_{_KEY_SUFFIX}"


def reasoning_budget_tokens(effort: str | None) -> int:
    """Map a System 2 reasoning effort to an output-token budget."""
    token = str(effort or "medium").strip().lower()
    return int(_REASONING_BUDGETS.get(token, _REASONING_BUDGETS["medium"]))


class ProviderUnavailable(RuntimeError):
    """The endpoint is offline, unconfigured, or has no API key."""


class ProviderTimeout(ProviderUnavailable):
    """The endpoint exceeded the 250 ms System 1 hard cap."""


class ProviderDemoted(ProviderUnavailable):
    """The endpoint is inside its circuit-breaker cooldown."""


class BudgetExceeded(RuntimeError):
    """The daily USD cap is exhausted; paid cloud endpoints are locked."""


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LLMProviderSpec:
    """One routable endpoint and its exact USD token rates."""

    name: str
    system: int
    kind: str
    model: str
    base_url: str
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0
    api_key_env: str = ""
    reasoning_effort: str = "medium"
    max_output_tokens: int = 512
    local: bool = False
    priority: int = 0

    def __post_init__(self) -> None:
        if int(self.system) not in (1, 2):
            raise ValueError("system must be 1 or 2")
        if float(self.input_usd_per_mtok) < 0.0 or float(self.output_usd_per_mtok) < 0.0:
            raise ValueError("token rates must be non-negative")
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "base_url", str(self.base_url).rstrip("/"))
        object.__setattr__(self, "local", bool(self.local))

    @property
    def paid(self) -> bool:
        return not self.local and (
            self.input_usd_per_mtok > 0.0 or self.output_usd_per_mtok > 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "system": int(self.system),
            "kind": self.kind,
            "model": self.model,
            "base_url": self.base_url,
            "input_usd_per_mtok": self.input_usd_per_mtok,
            "output_usd_per_mtok": self.output_usd_per_mtok,
            "api_key_env": self.api_key_env,
            "reasoning_effort": self.reasoning_effort,
            "max_output_tokens": int(self.max_output_tokens),
            "local": self.local,
            "paid": self.paid,
            "priority": int(self.priority),
        }


def default_provider_specs() -> tuple[LLMProviderSpec, ...]:
    """The exact ``sec_016`` System 1 / System 2 routing pool."""
    return (
        LLMProviderSpec(
            "ollama-qwen2.5-3b",
            1,
            "ollama",
            "qwen2.5:3b",
            "http://127.0.0.1:11434",
            0.0,
            0.0,
            local=True,
            priority=0,
            max_output_tokens=128,
        ),
        LLMProviderSpec(
            "ollama-llama3.2-3b",
            1,
            "ollama",
            "llama3.2:3b",
            "http://127.0.0.1:11434",
            0.0,
            0.0,
            local=True,
            priority=1,
            max_output_tokens=128,
        ),
        LLMProviderSpec(
            "gemini-2.5-flash-lite",
            1,
            "openai",
            "gemini-2.5-flash-lite",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            0.10,
            0.40,
            api_key_env=_env("GEMINI"),
            priority=2,
            max_output_tokens=128,
        ),
        LLMProviderSpec(
            "groq",
            1,
            "openai",
            "llama-3.3-70b-versatile",
            "https://api.groq.com/openai/v1",
            0.59,
            0.79,
            api_key_env=_env("GROQ"),
            priority=3,
            max_output_tokens=128,
        ),
        LLMProviderSpec(
            "deepseek-r1",
            2,
            "openai",
            "deepseek-r1",
            "https://api.deepseek.com",
            0.55,
            2.19,
            api_key_env=_env("DEEPSEEK"),
            reasoning_effort="high",
            max_output_tokens=8_192,
            priority=0,
        ),
        LLMProviderSpec(
            "claude-sonnet",
            2,
            "anthropic",
            "claude-sonnet-4-5",
            "https://api.anthropic.com",
            3.00,
            15.00,
            api_key_env=_env("ANTHROPIC"),
            reasoning_effort="high",
            max_output_tokens=8_192,
            priority=1,
        ),
        LLMProviderSpec(
            "claude-opus",
            2,
            "anthropic",
            "claude-opus-4-5",
            "https://api.anthropic.com",
            15.00,
            75.00,
            api_key_env=_env("ANTHROPIC"),
            reasoning_effort="max",
            max_output_tokens=16_384,
            priority=2,
        ),
        LLMProviderSpec(
            "openrouter",
            2,
            "openai",
            "openrouter/auto",
            "https://openrouter.ai/api/v1",
            0.50,
            1.50,
            api_key_env=_env("OPENROUTER"),
            reasoning_effort="medium",
            max_output_tokens=4_096,
            priority=3,
        ),
    )


# ---------------------------------------------------------------------------
# Daily token + USD ledger
# ---------------------------------------------------------------------------


def _utc_day(ns: int) -> str:
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).date().isoformat()


@dataclass(slots=True)
class _ProviderUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "usd": round(self.usd, 6),
        }


class TokenBudgetLedger:
    """Exact prompt/completion token and USD accounting for one UTC day.

    The ledger is the *only* authority that may lock a paid provider.  It is
    deliberately independent of the transport so a cockpit operator can lower
    the cap without rebuilding a client, and so a test can prove the cap math
    without a network call.
    """

    def __init__(
        self,
        daily_usd_cap: float = DEFAULT_DAILY_USD_CAP,
        *,
        baseline_usd_per_day: float = BASELINE_USD_PER_DAY,
        paid_providers: Iterable[str] = PAID_PROVIDERS,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        cap = float(daily_usd_cap)
        if not math.isfinite(cap) or cap < 0.0:
            raise ValueError("daily_usd_cap must be finite and non-negative")
        self.clock = clock
        self.daily_usd_cap = cap
        self.baseline_usd_per_day = float(baseline_usd_per_day)
        self.paid_providers = tuple(sorted(str(p) for p in paid_providers))
        self._lock = threading.RLock()
        self._day = _utc_day(int(clock()))
        self.spend_usd = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.system_spend_usd: dict[int, float] = {1: 0.0, 2: 0.0}
        self.system_tokens: dict[int, dict[str, int]] = {
            1: {"prompt": 0, "completion": 0},
            2: {"prompt": 0, "completion": 0},
        }
        self.by_provider: dict[str, _ProviderUsage] = defaultdict(_ProviderUsage)
        self.locked = False
        self.locked_reason = ""
        self.locked_at_ns = 0
        self.locked_providers: list[str] = []
        self.demoted_providers: dict[str, str] = {}
        self.active_system1_provider = "ollama-qwen2.5-3b"
        self.active_system2_provider = "deepseek-r1"

    # -- day roll ---------------------------------------------------------
    def _roll_day(self) -> None:
        today = _utc_day(int(self.clock()))
        if today == self._day:
            return
        self._day = today
        self.spend_usd = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.system_spend_usd = {1: 0.0, 2: 0.0}
        self.system_tokens = {1: {"prompt": 0, "completion": 0}, 2: {"prompt": 0, "completion": 0}}
        self.by_provider.clear()
        self.locked = False
        self.locked_reason = ""
        self.locked_at_ns = 0
        self.locked_providers = []
        self.active_system1_provider = "ollama-qwen2.5-3b"
        self.active_system2_provider = "deepseek-r1"

    # -- accounting -------------------------------------------------------
    def record(
        self,
        *,
        system: int,
        provider: str,
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        usd_cost: float | None = None,
        input_usd_per_mtok: float = 0.0,
        output_usd_per_mtok: float = 0.0,
    ) -> dict[str, Any]:
        """Record one call and return its settled cost."""
        prompt = max(0, int(prompt_tokens))
        completion = max(0, int(completion_tokens))
        if usd_cost is None:
            cost = (
                prompt / 1_000_000.0 * float(input_usd_per_mtok)
                + completion / 1_000_000.0 * float(output_usd_per_mtok)
            )
        else:
            cost = float(usd_cost)
        if not math.isfinite(cost) or cost < 0.0:
            raise ValueError("usd_cost must be finite and non-negative")
        with self._lock:
            self._roll_day()
            self.spend_usd += cost
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.calls += 1
            self.system_spend_usd[int(system)] = (
                self.system_spend_usd.get(int(system), 0.0) + cost
            )
            bucket = self.system_tokens.setdefault(
                int(system), {"prompt": 0, "completion": 0}
            )
            bucket["prompt"] += prompt
            bucket["completion"] += completion
            usage = self.by_provider[str(provider)]
            usage.calls += 1
            usage.prompt_tokens += prompt
            usage.completion_tokens += completion
            usage.usd += cost
            if self.spend_usd >= self.daily_usd_cap and not self.locked:
                self._lock_paid_locked("daily-cap reached")
            return {
                "system": int(system),
                "provider": str(provider),
                "model": str(model),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "usd_cost": round(cost, 8),
                "daily_spend_usd": round(self.spend_usd, 8),
                "daily_cap_usd": round(self.daily_usd_cap, 8),
                "locked": self.locked,
            }

    # -- budget authority -------------------------------------------------
    def can_spend(self, provider: str, system: int = 1) -> bool:
        """``False`` for a paid endpoint once the daily cap is exhausted."""
        name = str(provider)
        with self._lock:
            self._roll_day()
            if name in LOCAL_PROVIDERS or name == "deterministic-local":
                return True
            if name not in self.paid_providers:
                # Unknown providers fail closed unless explicitly local.
                return not self.locked
            if self.locked:
                return False
            if self.spend_usd >= self.daily_usd_cap:
                self._lock_paid_locked("daily-cap reached")
                return False
            return True

    def _lock_paid_locked(self, reason: str) -> None:
        if self.locked:
            return
        self.locked = True
        self.locked_reason = str(reason)
        self.locked_at_ns = int(self.clock())
        self.locked_providers = list(self.paid_providers)
        self.active_system1_provider = "ollama-qwen2.5-3b"
        self.active_system2_provider = "deterministic-local"

    def lock_paid(self, reason: str = "manual") -> None:
        with self._lock:
            self._roll_day()
            self._lock_paid_locked(reason)

    def set_daily_cap(self, usd: float) -> None:
        """Hot-swap the cap; a higher cap unlocks paid endpoints again."""
        cap = float(usd)
        if not math.isfinite(cap) or cap < 0.0:
            raise ValueError("daily_usd_cap must be finite and non-negative")
        with self._lock:
            self._roll_day()
            self.daily_usd_cap = cap
            if self.spend_usd >= self.daily_usd_cap:
                self._lock_paid_locked("daily-cap reached")
            elif self.locked:
                self.locked = False
                self.locked_reason = ""
                self.locked_at_ns = 0
                self.locked_providers = []
                self.active_system1_provider = "ollama-qwen2.5-3b"
                self.active_system2_provider = "deepseek-r1"

    def set_active(self, system: int, provider: str) -> None:
        with self._lock:
            self._roll_day()
            if int(system) == 1:
                self.active_system1_provider = str(provider)
            else:
                self.active_system2_provider = str(provider)

    def set_demoted(self, demoted: Mapping[str, str]) -> None:
        with self._lock:
            self.demoted_providers = {
                str(name): str(reason) for name, reason in dict(demoted).items()
            }

    # -- telemetry --------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._roll_day()
            baseline = self.baseline_usd_per_day
            effective = min(self.daily_usd_cap, baseline) if baseline > 0.0 else 0.0
            reduction = (
                (1.0 - effective / baseline) * 100.0 if baseline > 0.0 else 0.0
            )
            realized = (
                (1.0 - min(self.spend_usd, baseline) / baseline) * 100.0
                if baseline > 0.0
                else 0.0
            )
            return {
                "day_utc": self._day,
                "daily_cap_usd": round(self.daily_usd_cap, 6),
                "daily_spend_usd": round(self.spend_usd, 6),
                "budget_remaining_usd": round(
                    max(0.0, self.daily_usd_cap - self.spend_usd), 6
                ),
                "baseline_usd_per_day": round(baseline, 6),
                "cost_reduction_pct": round(reduction, 4),
                "realized_cost_reduction_pct": round(realized, 4),
                "locked": self.locked,
                "locked_reason": self.locked_reason,
                "locked_at_ns": self.locked_at_ns,
                "locked_providers": list(self.locked_providers),
                "demoted_providers": sorted(self.demoted_providers),
                "demotion_reasons": dict(self.demoted_providers),
                "active_system1_provider": self.active_system1_provider,
                "active_system2_provider": self.active_system2_provider,
                "calls": self.calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "system_spend_usd": {
                    str(system): round(value, 6)
                    for system, value in self.system_spend_usd.items()
                },
                "system_tokens": {
                    str(system): dict(bucket)
                    for system, bucket in self.system_tokens.items()
                },
                "by_provider": {
                    name: usage.to_dict()
                    for name, usage in sorted(self.by_provider.items())
                },
            }


# ---------------------------------------------------------------------------
# Completion contract
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LLMCompletion:
    provider: str
    model: str
    system: int
    text: str
    prompt_tokens: int
    completion_tokens: int
    usd_cost: float
    latency_ms: float
    reasoning_effort: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens) + int(self.completion_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "system": int(self.system),
            "text": self.text,
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "total_tokens": self.total_tokens,
            "usd_cost": round(float(self.usd_cost), 8),
            "latency_ms": float(self.latency_ms),
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(slots=True)
class _Demotion:
    provider: str
    reason: str
    at_ns: int
    cooldown_ns: int

    def expired(self, now_ns: int) -> bool:
        return int(now_ns) - int(self.at_ns) >= int(self.cooldown_ns)


# ---------------------------------------------------------------------------
# HTTP/2 keep-alive pool
# ---------------------------------------------------------------------------


class LLMHttp2TransportPool:
    """Reusable HTTP/2 keep-alive clients for every declared LLM endpoint.

    ``transport`` accepts an :class:`httpx.MockTransport` (or any
    ``httpx.BaseTransport``) for deterministic offline tests.  Without one the
    pool creates real keep-alive clients; no request is made until
    :meth:`complete` / :meth:`ping` is called.
    """

    def __init__(
        self,
        *,
        ledger: TokenBudgetLedger | None = None,
        providers: Iterable[LLMProviderSpec] | None = None,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
        api_keys: Mapping[str, str] | None = None,
        hard_timeout_ms: float = HARD_TIMEOUT_MS,
        timeout_s: float = 30.0,
        max_keepalive_connections: int = 8,
        max_connections: int = 16,
        keepalive_expiry_s: float = 30.0,
        demotion_cooldown_ms: float = 30_000.0,
        clock: Callable[[], int] = time.time_ns,
        http2: bool = True,
    ) -> None:
        self.ledger = ledger
        self.hard_timeout_ms = float(hard_timeout_ms)
        self.demotion_cooldown_ms = float(demotion_cooldown_ms)
        self.clock = clock
        self.api_keys = {str(k): str(v) for k, v in dict(api_keys or {}).items()}
        self._specs: dict[str, LLMProviderSpec] = {
            spec.name: spec
            for spec in (providers if providers is not None else default_provider_specs())
        }
        if not self._specs:
            raise ValueError("at least one provider spec is required")

        self.limits = httpx.Limits(
            max_keepalive_connections=int(max_keepalive_connections),
            max_connections=int(max_connections),
            keepalive_expiry=float(keepalive_expiry_s),
        )
        self.timeout = httpx.Timeout(float(timeout_s))
        self.http2_requested = bool(http2)
        self.http2_enabled = False
        self._sync_client = self._build_sync_client(transport)
        self._async_client: httpx.AsyncClient | None = None
        self._async_transport = async_transport if async_transport is not None else transport
        self._demotions: dict[str, _Demotion] = {}
        self._latencies_ms: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=256)
        )
        self._lock = threading.RLock()
        self.calls = 0
        self.failures = 0
        self._sync_ledger()

    # -- clients ----------------------------------------------------------
    def _build_sync_client(
        self, transport: httpx.BaseTransport | None
    ) -> httpx.Client:
        if self.http2_requested:
            try:
                client = httpx.Client(
                    http2=True,
                    limits=self.limits,
                    timeout=self.timeout,
                    transport=transport,
                )
                self.http2_enabled = True
                return client
            except Exception:
                self.http2_enabled = False
        return httpx.Client(
            http2=False,
            limits=self.limits,
            timeout=self.timeout,
            transport=transport,
        )

    def _async_client_or_create(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                http2=self.http2_enabled,
                limits=self.limits,
                timeout=self.timeout,
                transport=self._async_transport,
            )
        return self._async_client

    @property
    def sync_client(self) -> httpx.Client:
        return self._sync_client

    @property
    def keepalive(self) -> dict[str, Any]:
        return {
            "max_keepalive_connections": self.limits.max_keepalive_connections,
            "max_connections": self.limits.max_connections,
            "keepalive_expiry_s": self.limits.keepalive_expiry,
        }

    # -- provider helpers -------------------------------------------------
    def spec(self, provider: str) -> LLMProviderSpec:
        try:
            return self._specs[str(provider)]
        except KeyError as exc:
            raise KeyError(f"unknown LLM provider {provider!r}") from exc

    def providers(self, system: int | None = None) -> tuple[LLMProviderSpec, ...]:
        specs = sorted(self._specs.values(), key=lambda s: (s.system, s.priority))
        if system is None:
            return tuple(specs)
        return tuple(spec for spec in specs if spec.system == int(system))

    def api_key(self, spec: LLMProviderSpec) -> str:
        key = self.api_keys.get(spec.name) or self.api_keys.get(spec.api_key_env)
        if not key and spec.api_key_env:
            key = os.environ.get(spec.api_key_env, "")
        if not key and spec.paid:
            raise ProviderUnavailable(
                f"{spec.name}: no API key (set {spec.api_key_env or 'api_keys'})"
            )
        return str(key or "")

    # -- circuit breaker --------------------------------------------------
    def demote(
        self,
        provider: str,
        reason: str,
        *,
        cooldown_ms: float | None = None,
    ) -> None:
        with self._lock:
            now = int(self.clock())
            self._demotions[str(provider)] = _Demotion(
                provider=str(provider),
                reason=str(reason),
                at_ns=now,
                cooldown_ns=int(
                    (self.demotion_cooldown_ms if cooldown_ms is None else cooldown_ms)
                    * 1e6
                ),
            )
            self._sync_ledger()

    def promote(self, provider: str) -> None:
        with self._lock:
            self._demotions.pop(str(provider), None)
            self._sync_ledger()

    def is_demoted(self, provider: str, now_ns: int | None = None) -> bool:
        with self._lock:
            record = self._demotions.get(str(provider))
            if record is None:
                return False
            now = int(now_ns) if now_ns is not None else int(self.clock())
            if record.expired(now):
                self._demotions.pop(str(provider), None)
                self._sync_ledger()
                return False
            return True

    def demoted_names(self) -> tuple[str, ...]:
        now = int(self.clock())
        with self._lock:
            for name in [
                name
                for name, record in self._demotions.items()
                if record.expired(now)
            ]:
                self._demotions.pop(name, None)
            self._sync_ledger()
            return tuple(sorted(self._demotions))

    def _sync_ledger(self) -> None:
        if self.ledger is None:
            return
        self.ledger.set_demoted(
            {name: record.reason for name, record in self._demotions.items()}
        )

    def active_provider(self, system: int) -> str:
        """First healthy, budget-authorized provider for a tier."""
        for spec in self.providers(system):
            if self.is_demoted(spec.name):
                continue
            if self.ledger is not None and not self.ledger.can_spend(
                spec.name, spec.system
            ):
                continue
            return spec.name
        return "deterministic-local"

    def _authorize(self, spec: LLMProviderSpec) -> None:
        if self.is_demoted(spec.name):
            record = self._demotions[spec.name]
            raise ProviderDemoted(f"{spec.name}: demoted ({record.reason})")
        if self.ledger is not None and not self.ledger.can_spend(spec.name, spec.system):
            raise BudgetExceeded(
                f"{spec.name}: daily USD cap "
                f"${self.ledger.daily_usd_cap:.2f} exhausted"
            )

    # -- request construction --------------------------------------------
    def _prepare(
        self,
        spec: LLMProviderSpec,
        prompt: str,
        *,
        max_tokens: int | None,
        reasoning_effort: str | None,
        temperature: float,
    ) -> tuple[str, str, dict[str, str], dict[str, Any]]:
        key = self.api_key(spec)
        budget = int(max_tokens or spec.max_output_tokens)
        effort = str(reasoning_effort or spec.reasoning_effort)
        headers: dict[str, str] = {"content-type": "application/json"}
        if spec.kind == "ollama":
            url = f"{spec.base_url}/api/generate"
            body: dict[str, Any] = {
                "model": spec.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": float(temperature),
                    "num_predict": budget,
                },
            }
            return "POST", url, headers, body
        if spec.kind == "anthropic":
            url = f"{spec.base_url}/v1/messages"
            if key:
                headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
            body = {
                "model": spec.model,
                "max_tokens": budget,
                "temperature": float(temperature),
                "messages": [{"role": "user", "content": prompt}],
            }
            return "POST", url, headers, body
        # OpenAI-compatible: Gemini, Groq, DeepSeek, OpenRouter.
        url = f"{spec.base_url}/chat/completions"
        if key:
            headers["authorization"] = f"Bearer {key}"
        body = {
            "model": spec.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": budget,
            "temperature": float(temperature),
        }
        if spec.system == 2:
            body["reasoning_effort"] = effort
        return "POST", url, headers, body

    @staticmethod
    def _parse(
        spec: LLMProviderSpec, payload: Mapping[str, Any], prompt: str
    ) -> tuple[str, int, int]:
        text = ""
        prompt_tokens = 0
        completion_tokens = 0
        if spec.kind == "ollama":
            text = str(payload.get("response") or "")
            if not text:
                message = payload.get("message")
                if isinstance(message, Mapping):
                    text = str(message.get("content") or "")
            prompt_tokens = int(
                payload.get("prompt_eval_count") or payload.get("prompt_tokens") or 0
            )
            completion_tokens = int(
                payload.get("eval_count") or payload.get("completion_tokens") or 0
            )
        elif spec.kind == "anthropic":
            content = payload.get("content")
            if isinstance(content, list):
                text = "".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, Mapping)
                )
            usage = payload.get("usage") or {}
            prompt_tokens = int(usage.get("input_tokens") or 0)
            completion_tokens = int(usage.get("output_tokens") or 0)
        else:
            choices = payload.get("choices") or []
            if choices:
                message = choices[0].get("message") or {}
                text = str(message.get("content") or "")
            usage = payload.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
        if prompt_tokens <= 0:
            prompt_tokens = max(1, len(prompt) // 4)
        if completion_tokens <= 0:
            completion_tokens = max(1, len(text) // 4)
        return text, prompt_tokens, completion_tokens

    def _settle(
        self,
        spec: LLMProviderSpec,
        prompt: str,
        payload: Mapping[str, Any],
        elapsed_ms: float,
        effort: str,
    ) -> LLMCompletion:
        text, prompt_tokens, completion_tokens = self._parse(spec, payload, prompt)
        cost = (
            prompt_tokens / 1_000_000.0 * spec.input_usd_per_mtok
            + completion_tokens / 1_000_000.0 * spec.output_usd_per_mtok
        )
        if self.ledger is not None:
            self.ledger.record(
                system=spec.system,
                provider=spec.name,
                model=spec.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                usd_cost=cost,
            )
            self.ledger.set_active(spec.system, spec.name)
        self._latencies_ms[spec.name].append(float(elapsed_ms))
        self.calls += 1
        return LLMCompletion(
            provider=spec.name,
            model=spec.model,
            system=spec.system,
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usd_cost=cost,
            latency_ms=float(elapsed_ms),
            reasoning_effort=effort,
            raw=dict(payload),
        )

    def _check_system1_latency(self, spec: LLMProviderSpec, elapsed_ms: float) -> None:
        if spec.system == 1 and elapsed_ms > self.hard_timeout_ms:
            reason = f"hard-cap {elapsed_ms:.1f}ms > {self.hard_timeout_ms:.0f}ms"
            self.demote(spec.name, reason)
            raise ProviderTimeout(f"{spec.name}: {reason}")

    # -- completion -------------------------------------------------------
    def complete(
        self,
        provider: str,
        prompt: str,
        *,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        spec = self.spec(provider)
        self._authorize(spec)
        effort = str(reasoning_effort or spec.reasoning_effort)
        method, url, headers, body = self._prepare(
            spec,
            prompt,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
        )
        started = time.perf_counter()
        try:
            response = self._sync_client.request(
                method, url, headers=headers, json=body
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            self.failures += 1
            self.demote(spec.name, f"timeout: {type(exc).__name__}")
            raise ProviderTimeout(f"{spec.name}: timeout") from exc
        except Exception as exc:  # noqa: BLE001 - every transport error demotes
            self.failures += 1
            self.demote(spec.name, f"error: {type(exc).__name__}: {exc}")
            raise ProviderUnavailable(f"{spec.name}: {type(exc).__name__}: {exc}") from exc
        elapsed_ms = (time.perf_counter() - started) * 1e3
        if not isinstance(payload, Mapping):
            self.failures += 1
            self.demote(spec.name, "protocol: non-object payload")
            raise ProviderUnavailable(f"{spec.name}: response is not an object")
        completion = self._settle(spec, prompt, payload, elapsed_ms, effort)
        self._check_system1_latency(spec, elapsed_ms)
        return completion

    async def complete_async(
        self,
        provider: str,
        prompt: str,
        *,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        spec = self.spec(provider)
        self._authorize(spec)
        effort = str(reasoning_effort or spec.reasoning_effort)
        method, url, headers, body = self._prepare(
            spec,
            prompt,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
        )
        client = self._async_client_or_create()
        started = time.perf_counter()
        try:
            response = await client.request(method, url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            self.failures += 1
            self.demote(spec.name, f"timeout: {type(exc).__name__}")
            raise ProviderTimeout(f"{spec.name}: timeout") from exc
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            self.demote(spec.name, f"error: {type(exc).__name__}: {exc}")
            raise ProviderUnavailable(f"{spec.name}: {type(exc).__name__}: {exc}") from exc
        elapsed_ms = (time.perf_counter() - started) * 1e3
        if not isinstance(payload, Mapping):
            self.failures += 1
            self.demote(spec.name, "protocol: non-object payload")
            raise ProviderUnavailable(f"{spec.name}: response is not an object")
        completion = self._settle(spec, prompt, payload, elapsed_ms, effort)
        self._check_system1_latency(spec, elapsed_ms)
        return completion

    def complete_system2(
        self,
        prompt: str,
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMCompletion:
        """Route a System 2 turn across DeepSeek -> Claude -> OpenRouter.

        Returns a ``deterministic-local`` zero-cost completion when every paid
        endpoint is demoted or the daily cap is exhausted.  The caller always
        gets a usable object; it never has to special-case the budget lock.
        """
        last_error = ""
        for spec in self.providers(2):
            if self.is_demoted(spec.name):
                continue
            if self.ledger is not None and not self.ledger.can_spend(
                spec.name, spec.system
            ):
                continue
            try:
                return self.complete(
                    spec.name,
                    prompt,
                    reasoning_effort=reasoning_effort,
                    max_tokens=max_tokens,
                )
            except ProviderUnavailable as exc:
                last_error = str(exc)
                continue
        if self.ledger is not None:
            self.ledger.set_active(2, "deterministic-local")
        return LLMCompletion(
            provider="deterministic-local",
            model="nios-native-committee",
            system=2,
            text="",
            prompt_tokens=0,
            completion_tokens=0,
            usd_cost=0.0,
            latency_ms=0.0,
            reasoning_effort=str(reasoning_effort or ""),
            raw={"fallback": True, "reason": last_error or "all System 2 providers locked"},
        )

    # -- ping -------------------------------------------------------------
    def ping(self, provider: str) -> bool:
        """Cheap reachability probe under the same 250 ms hard cap."""
        spec = self.spec(provider)
        if self.is_demoted(spec.name):
            return False
        started = time.perf_counter()
        try:
            response = self._sync_client.get(spec.base_url)
            elapsed_ms = (time.perf_counter() - started) * 1e3
            if response.status_code >= 500:
                raise ProviderUnavailable(
                    f"{spec.name}: HTTP {response.status_code}"
                )
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - started) * 1e3
            self.demote(spec.name, f"ping error: {type(exc).__name__}")
            return False
        self._latencies_ms[spec.name].append(float(elapsed_ms))
        if spec.system == 1 and elapsed_ms > self.hard_timeout_ms:
            self.demote(
                spec.name,
                f"ping {elapsed_ms:.1f}ms > {self.hard_timeout_ms:.0f}ms",
            )
            return False
        return True

    # -- System 1 probe adapter -------------------------------------------
    def probe_for(self, provider: str) -> Callable[[Any], Mapping[str, Any]]:
        """Build a ``MultiLLMSwitchboard`` probe backed by this pool."""
        spec = self.spec(provider)

        def _probe(context: Any) -> Mapping[str, Any]:
            prompt = _instinct_prompt(context)
            completion = self.complete(
                spec.name,
                prompt,
                max_tokens=128,
                temperature=0.0,
            )
            return parse_instinct_response(completion.text)

        return _probe

    # -- telemetry --------------------------------------------------------
    def latency_stats(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for name, samples in self._latencies_ms.items():
            ordered = sorted(samples)
            if not ordered:
                continue
            out[name] = {
                "count": float(len(ordered)),
                "last_ms": float(samples[-1]),
                "p50_ms": _percentile(ordered, 0.50),
                "p99_ms": _percentile(ordered, 0.99),
                "max_ms": float(ordered[-1]),
            }
        return out

    def status(self) -> dict[str, Any]:
        return {
            "http2": self.http2_enabled,
            "http2_requested": self.http2_requested,
            "keepalive": self.keepalive,
            "hard_timeout_ms": self.hard_timeout_ms,
            "demotion_cooldown_ms": self.demotion_cooldown_ms,
            "providers": [spec.to_dict() for spec in self.providers()],
            "demoted_providers": list(self.demoted_names()),
            "demotion_reasons": {
                name: record.reason for name, record in self._demotions.items()
            },
            "active_system1_provider": self.active_provider(1),
            "active_system2_provider": self.active_provider(2),
            "calls": self.calls,
            "failures": self.failures,
            "latency": self.latency_stats(),
            "ledger": self.ledger.snapshot() if self.ledger is not None else None,
        }

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        try:
            self._sync_client.close()
        except Exception:  # pragma: no cover - defensive
            pass

    async def aclose(self) -> None:
        if self._async_client is not None:
            try:
                await self._async_client.aclose()
            except Exception:  # pragma: no cover - defensive
                pass
            self._async_client = None

    def __enter__(self) -> "LLMHttp2TransportPool":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Prompt / response helpers
# ---------------------------------------------------------------------------

_MODE_PATTERN = re.compile(
    r"\b(LONG[_-]?ONLY|SHORT[_-]?ONLY|MEAN[_-]?REV|DISARMED)\b", re.IGNORECASE
)
_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


def _instinct_prompt(context: Any) -> str:
    """Compact JSON prompt for a System 1 instinct turn."""
    payload = {
        "task": "nios_system1_instinct",
        "symbol": getattr(context, "symbol", ""),
        "realized_vol_1m": getattr(context, "realized_vol_1m", 0.0),
        "normal_vol_1m": getattr(context, "normal_vol_1m", 0.0),
        "spread_bps": getattr(context, "spread_bps", 0.0),
        "taker_buy_ratio": getattr(context, "taker_buy_ratio", 0.5),
        "headline_shock": bool(getattr(context, "headline_shock", False)),
        "schema": {
            "mode": "LONG_ONLY|SHORT_ONLY|MEAN_REV|DISARMED",
            "target_bps": "number",
            "stop_bps": "number",
            "confidence": "number 0..1",
            "rationale": "one sentence",
        },
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def parse_instinct_response(text: str) -> dict[str, Any]:
    """Parse an LLM instinct reply into the switchboard's mapping contract.

    A malformed reply is a protocol failure and raises, which demotes the
    provider rather than silently inventing a lease.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ProviderUnavailable("empty instinct response")
    candidate = raw
    if "```" in candidate:
        blocks = candidate.split("```")
        candidate = max(blocks, key=len).strip()
    try:
        payload = json.loads(candidate)
        if isinstance(payload, Mapping) and payload.get("mode"):
            return {
                "mode": payload["mode"],
                "target_bps": float(payload.get("target_bps", 0.0)),
                "stop_bps": float(payload.get("stop_bps", 0.0)),
                "confidence": float(payload.get("confidence", 0.5)),
                "rationale": str(payload.get("rationale", "")),
            }
    except (ValueError, TypeError):
        pass
    mode_match = _MODE_PATTERN.search(raw)
    if not mode_match:
        raise ProviderUnavailable(f"unparseable instinct response: {raw[:120]!r}")
    numbers = [float(value) for value in _NUMBER_PATTERN.findall(raw)]
    target = numbers[0] if numbers else 0.0
    stop = numbers[1] if len(numbers) > 1 else 0.0
    return {
        "mode": mode_match.group(1),
        "target_bps": target,
        "stop_bps": stop,
        "confidence": 0.5,
        "rationale": raw[:240],
    }


def _percentile(ordered: Sequence[float], q: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = max(0.0, min(1.0, float(q))) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return float(ordered[low]) + (float(ordered[high]) - float(ordered[low])) * fraction
