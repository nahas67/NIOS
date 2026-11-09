"""NIOS Core v3.0 -- System 1 Instinct Controller & Multi-LLM Switchboard (STEP 4).

``sec_004`` / ``sec_007`` / ``sec_010`` / ``sec_013`` / ``sec_016`` define the
Fast Instinct tier:

* System 1 runs **off the tick path** every ~30 s (or on a headline shock);
* it may write only a 60-second dead-man :class:`~core.schemas.System1Lease`;
* any LLM endpoint (local Ollama ``qwen2.5:3b`` / ``llama3.2:3b``,
  ``gemini-2.5-flash-lite``, ``groq``) whose ping/response exceeds a **250 ms
  hard cap** is demoted, and the controller falls back to a deterministic
  instinct evaluator so the lease refresh still completes inside the
  ``48.0 ms p50 / 78.0 ms p99`` SLA;
* if ``cascade_fragility_index >= 0.75`` a ``LONG_ONLY`` lease is never armed
  (``sec_008`` / ``sec_018`` Law 3).

Transport policy
----------------
No provider SDK, HTTP client or socket is imported here.  Ring 2 is air-gapped
from exchange sockets and broker credentials, and every outbound call must be
declared with its ring/credential source (``sec_011``).  Providers therefore
carry an injectable ``probe`` callable supplied by the Ring 3 control plane
(``POST /api/v2/control/llms``).  The default probes deliberately fail closed
as ``offline`` -- a deterministic local evaluator is always available, which is
also the production behaviour when the network stalls.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.schemas import (
    FRAGILITY_HALT_THRESHOLD,
    LEASE_TTL_NS,
    VOL_SPIKE_BREAKER,
    ContagionFeatures,
    LeaseMode,
    System1Lease,
)
from core.shm_ipc import GlobalKillLatch, LeaseTableSHM

__all__ = [
    "HARD_TIMEOUT_MS",
    "P50_SLA_MS",
    "P95_SLA_MS",
    "P99_SLA_MS",
    "PROBE_TIMEOUT_MS",
    "ProviderUnavailable",
    "LLMProvider",
    "ProviderResponse",
    "RoutingDecision",
    "MultiLLMSwitchboard",
    "InstinctContext",
    "InstinctDecision",
    "System1InstinctController",
    "deterministic_instinct",
]

#: ``sec_007`` / ``sec_010`` hard circuit-breaker cap.
HARD_TIMEOUT_MS = 250.0
#: Fast probe budget: a provider that has not answered inside this window is
#: demoted before it can consume the lease-refresh SLA.
PROBE_TIMEOUT_MS = 20.0
#: ``sec_010`` / ``sec_013`` System 1 SLA.
P50_SLA_MS = 48.0
P95_SLA_MS = 64.0
P99_SLA_MS = 78.0

_MODE_BY_NAME: Mapping[str, LeaseMode] = {
    "DISARMED": LeaseMode.DISARMED,
    "LONG_ONLY": LeaseMode.LONG_ONLY,
    "LONG-ONLY": LeaseMode.LONG_ONLY,
    "SHORT_ONLY": LeaseMode.SHORT_ONLY,
    "SHORT-ONLY": LeaseMode.SHORT_ONLY,
    "MEAN_REV": LeaseMode.MEAN_REV,
    "MEAN-REV": LeaseMode.MEAN_REV,
}


class ProviderUnavailable(RuntimeError):
    """Raised by a provider probe when the endpoint is offline or unconfigured."""


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _coerce_mode(value: Any) -> LeaseMode:
    if isinstance(value, LeaseMode):
        return value
    if isinstance(value, str):
        token = value.strip().upper()
        if token in _MODE_BY_NAME:
            return _MODE_BY_NAME[token]
        raise ValueError(f"unknown lease mode {value!r}")
    return LeaseMode(int(value))


def _strategy_hash(strategy_id: str) -> int:
    digest = hashlib.blake2b(str(strategy_id).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little", signed=False)


# ---------------------------------------------------------------------------
# Provider contracts
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ProviderResponse:
    """A validated LLM instinct suggestion."""

    mode: LeaseMode
    target_bps: float
    stop_bps: float = 0.0
    confidence: float = 0.5
    rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _coerce_mode(self.mode))
        target = float(self.target_bps)
        stop = float(self.stop_bps)
        confidence = float(self.confidence)
        for name, value in (
            ("target_bps", target),
            ("stop_bps", stop),
            ("confidence", confidence),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        object.__setattr__(self, "target_bps", _clamp(target, 0.0, 40.0))
        object.__setattr__(self, "stop_bps", _clamp(stop, 0.0, 30.0))
        object.__setattr__(self, "confidence", _clamp(confidence, 0.0, 1.0))


@dataclass(slots=True)
class LLMProvider:
    """One routable System 1 endpoint.

    ``probe`` receives the :class:`InstinctContext` and returns either a
    :class:`ProviderResponse`, a mapping with ``mode``/``target_bps``, or a
    ``(mode, target_bps, stop_bps)`` tuple.  It must be fast and non-blocking;
    the switchboard enforces the timeout regardless.
    """

    name: str
    kind: str
    model: str
    probe: Callable[["InstinctContext"], Any] | None = None
    priority: int = 0
    enabled: bool = True
    demoted: bool = False
    demoted_at_ns: int = 0
    demoted_reason: str = ""
    last_latency_ms: float = 0.0
    ewma_latency_ms: float = 0.0
    successes: int = 0
    failures: int = 0

    def __post_init__(self) -> None:
        for name in ("name", "kind", "model"):
            token = str(getattr(self, name)).strip()
            if not token:
                raise ValueError(f"{name} must be non-empty")
            setattr(self, name, token)
        self.priority = int(self.priority)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "model": self.model,
            "priority": self.priority,
            "enabled": self.enabled,
            "demoted": self.demoted,
            "demoted_reason": self.demoted_reason,
            "last_latency_ms": self.last_latency_ms,
            "ewma_latency_ms": self.ewma_latency_ms,
            "successes": self.successes,
            "failures": self.failures,
        }


def _offline_probe(context: "InstinctContext") -> Any:  # noqa: ARG001
    raise ProviderUnavailable(
        "offline: no Ring 2 LLM transport wired (Ring 3 control plane must inject one)"
    )


def default_providers() -> list[LLMProvider]:
    """The exact ``sec_016`` System 1 routing pool, highest priority first."""
    return [
        LLMProvider("ollama-qwen2.5-3b", "ollama", "qwen2.5:3b", _offline_probe, 0),
        LLMProvider("ollama-llama3.2-3b", "ollama", "llama3.2:3b", _offline_probe, 1),
        LLMProvider(
            "gemini-2.5-flash-lite", "gemini", "gemini-2.5-flash-lite", _offline_probe, 2
        ),
        LLMProvider("groq", "groq", "llama-3.3-70b-versatile", _offline_probe, 3),
    ]


@dataclass(slots=True)
class RoutingDecision:
    """Outcome of one switchboard route."""

    provider_name: str
    model: str
    suggestion: ProviderResponse | None
    used_fallback: bool
    latency_ms: float
    reason: str
    demoted: tuple[str, ...] = ()

    @property
    def is_deterministic(self) -> bool:
        return self.suggestion is None


# ---------------------------------------------------------------------------
# Switchboard
# ---------------------------------------------------------------------------


class MultiLLMSwitchboard:
    """Ordered provider router with a 250 ms hard cap and auto-demotion."""

    def __init__(
        self,
        providers: Sequence[LLMProvider] | None = None,
        *,
        hard_timeout_ms: float = HARD_TIMEOUT_MS,
        probe_timeout_ms: float = PROBE_TIMEOUT_MS,
        demotion_cooldown_ns: int = 30_000_000_000,
        clock: Callable[[], int] = time.monotonic_ns,
        transport_pool: Any | None = None,
        ledger: Any | None = None,
        auto_probe: bool = False,
    ) -> None:
        self.providers: list[LLMProvider] = []
        for provider in providers if providers is not None else default_providers():
            if isinstance(provider, Mapping):
                self.providers.append(LLMProvider(**dict(provider)))
            else:
                self.providers.append(provider)
        self.hard_timeout_ms = float(hard_timeout_ms)
        self.probe_timeout_ms = float(probe_timeout_ms)
        if not 0.0 < self.probe_timeout_ms <= self.hard_timeout_ms:
            raise ValueError("probe_timeout_ms must be within (0, hard_timeout_ms]")
        self.demotion_cooldown_ns = int(demotion_cooldown_ns)
        self.clock = clock
        self.transport_pool = transport_pool
        self.ledger = ledger
        self._lock = threading.RLock()
        self._routes = 0
        self._fallbacks = 0
        if auto_probe and transport_pool is not None:
            for provider in self.providers:
                try:
                    provider.probe = transport_pool.probe_for(provider.name)
                except KeyError:
                    # A provider the transport pool does not know keeps its
                    # injected probe (or the fail-closed offline default).
                    continue


    # -- management -------------------------------------------------------
    def provider(self, name: str) -> LLMProvider:
        for provider in self.providers:
            if provider.name == str(name):
                return provider
        raise KeyError(f"unknown provider {name!r}")

    def install_probe(
        self, name: str, probe: Callable[["InstinctContext"], Any]
    ) -> LLMProvider:
        provider = self.provider(name)
        provider.probe = probe
        provider.demoted = False
        provider.demoted_reason = ""
        return provider

    def demote(self, name: str, reason: str = "manual", now_ns: int | None = None) -> None:
        with self._lock:
            provider = self.provider(name)
            provider.demoted = True
            provider.demoted_reason = str(reason)
            provider.demoted_at_ns = int(now_ns if now_ns is not None else self.clock())
            provider.failures += 1

    def promote(self, name: str) -> None:
        with self._lock:
            provider = self.provider(name)
            provider.demoted = False
            provider.demoted_reason = ""

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "hard_timeout_ms": self.hard_timeout_ms,
                "probe_timeout_ms": self.probe_timeout_ms,
                "routes": self._routes,
                "fallbacks": self._fallbacks,
                "providers": [provider.to_dict() for provider in self.providers],
                "demoted_providers": list(self.demoted_names()),
                "budget": self.ledger.snapshot() if self.ledger is not None else None,
                "transport": (
                    self.transport_pool.status()
                    if self.transport_pool is not None
                    else None
                ),
            }

    def demoted_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(p.name for p in self.providers if p.demoted)

    # -- routing ----------------------------------------------------------
    def _invoke(
        self, provider: LLMProvider, context: "InstinctContext"
    ) -> tuple[Any, float, str | None]:
        """Call a probe with a hard wall-clock budget.

        Returns ``(response, elapsed_ms, error)``.  A probe still running after
        the timeout is abandoned (daemon thread) and reported as an error, so a
        stalled endpoint can never hold the lease refresh past the SLA.
        """
        outcome: dict[str, Any] = {}

        def _target() -> None:
            try:
                outcome["value"] = (
                    provider.probe(context) if provider.probe is not None else None
                )
            except BaseException as exc:  # noqa: BLE001 - reported as failure
                outcome["error"] = exc

        started = time.perf_counter()
        worker = threading.Thread(
            target=_target, name=f"nios-s1-probe-{provider.name}", daemon=True
        )
        worker.start()
        worker.join(self.probe_timeout_ms / 1e3)
        elapsed = (time.perf_counter() - started) * 1e3
        if worker.is_alive():
            return None, elapsed, f"timeout>{self.probe_timeout_ms:.0f}ms"
        error = outcome.get("error")
        if error is not None:
            return None, elapsed, f"{type(error).__name__}: {error}"
        return outcome.get("value"), elapsed, None

    @staticmethod
    def _coerce_response(value: Any) -> ProviderResponse:
        if isinstance(value, ProviderResponse):
            return value
        if isinstance(value, Mapping):
            return ProviderResponse(
                mode=value["mode"],
                target_bps=value.get("target_bps", 0.0),
                stop_bps=value.get("stop_bps", 0.0),
                confidence=value.get("confidence", 0.5),
                rationale=str(value.get("rationale", "")),
            )
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            return ProviderResponse(
                mode=value[0],
                target_bps=value[1],
                stop_bps=value[2] if len(value) > 2 else 0.0,
            )
        raise ValueError(f"unsupported provider response {type(value).__name__}")

    def route(self, context: "InstinctContext") -> RoutingDecision:
        """Route to the best healthy provider, or fall back deterministically."""
        with self._lock:
            self._routes += 1
            now = int(self.clock())
            pool = sorted(
                (p for p in self.providers if p.enabled), key=lambda p: p.priority
            )
            demoted_now: list[str] = []
            for provider in pool:
                if provider.demoted:
                    if now - provider.demoted_at_ns < self.demotion_cooldown_ns:
                        continue
                    # Half-open: give the endpoint one cheap retry.
                    provider.demoted = False
                    provider.demoted_reason = ""

                # Budget authority runs before the network: once the daily cap
                # is reached a paid endpoint is skipped, never called.
                if self.ledger is not None and not self.ledger.can_spend(
                    provider.name, 1
                ):
                    demoted_now.append(provider.name)
                    continue

                value, elapsed, error = self._invoke(provider, context)
                provider.last_latency_ms = elapsed
                provider.ewma_latency_ms = (
                    elapsed
                    if provider.ewma_latency_ms <= 0.0
                    else 0.7 * provider.ewma_latency_ms + 0.3 * elapsed
                )
                if error is not None or value is None:
                    provider.demoted = True
                    provider.demoted_at_ns = now
                    provider.demoted_reason = error or "empty response"
                    provider.failures += 1
                    demoted_now.append(provider.name)
                    continue
                try:
                    suggestion = self._coerce_response(value)
                except Exception as exc:  # noqa: BLE001 - protocol violation
                    provider.demoted = True
                    provider.demoted_at_ns = now
                    provider.demoted_reason = f"protocol: {exc}"
                    provider.failures += 1
                    demoted_now.append(provider.name)
                    continue
                if elapsed > self.hard_timeout_ms:
                    provider.demoted = True
                    provider.demoted_at_ns = now
                    provider.demoted_reason = f"hard-cap {elapsed:.1f}ms"
                    provider.failures += 1
                    demoted_now.append(provider.name)
                    continue
                provider.successes += 1
                if self.ledger is not None:
                    self.ledger.set_active(1, provider.name)
                return RoutingDecision(
                    provider_name=provider.name,
                    model=provider.model,
                    suggestion=suggestion,
                    used_fallback=False,
                    latency_ms=elapsed,
                    reason="ok",
                    demoted=tuple(demoted_now),
                )

            self._fallbacks += 1
            if self.ledger is not None:
                self.ledger.set_active(1, "deterministic-local")
            return RoutingDecision(
                provider_name="deterministic-local",
                model="nios-native-instinct",
                suggestion=None,
                used_fallback=True,
                latency_ms=0.0,
                reason="all providers offline, demoted or budget-locked",
                demoted=tuple(demoted_now),
            )


# ---------------------------------------------------------------------------
# Instinct context / decision
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class InstinctContext:
    """The 1-minute tape/regime digest System 1 evaluates."""

    symbol: str
    realized_vol_1m: float = 0.0
    normal_vol_1m: float = 0.0
    spread_bps: float = 0.0
    taker_buy_ratio: float = 0.5
    contagion: ContagionFeatures | None = None
    headline_shock: bool = False
    max_spread_bps: float = 25.0
    strategy_id: str = "system1_instinct"

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip().upper()
        if not symbol:
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "symbol", symbol)
        for name in ("realized_vol_1m", "normal_vol_1m", "spread_bps"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        ratio = float(self.taker_buy_ratio)
        if not 0.0 <= ratio <= 1.0:
            raise ValueError("taker_buy_ratio must be within [0, 1]")
        object.__setattr__(self, "taker_buy_ratio", ratio)
        object.__setattr__(self, "max_spread_bps", float(self.max_spread_bps))
        object.__setattr__(self, "headline_shock", bool(self.headline_shock))

    @property
    def vol_ratio(self) -> float:
        base = float(self.normal_vol_1m)
        realized = float(self.realized_vol_1m)
        if base <= 0.0:
            return 0.0 if realized <= 0.0 else math.inf
        return realized / base

    @property
    def fragility_halted(self) -> bool:
        contagion = self.contagion
        return bool(
            contagion is not None
            and contagion.cascade_fragility_index >= FRAGILITY_HALT_THRESHOLD
        )

    @classmethod
    def from_executed_flow(
        cls,
        tick: Any,
        *,
        realized_vol_1m: float | None = None,
        normal_vol_1m: float | None = None,
        contagion: ContagionFeatures | None = None,
        headline_shock: bool = False,
        max_spread_bps: float = 25.0,
        strategy_id: str = "system1_instinct",
    ) -> "InstinctContext":
        """Build the 1-minute digest from an ``ExecutedFlowTick`` frame.

        The tick's 5-second executed physics are the freshest proof of flow;
        the caller may overlay a true 1-minute realized/normal volatility pair.
        """
        return cls(
            symbol=tick.symbol,
            realized_vol_1m=(
                float(tick.realized_vol_5s)
                if realized_vol_1m is None
                else float(realized_vol_1m)
            ),
            normal_vol_1m=(
                float(tick.normal_vol_5s)
                if normal_vol_1m is None
                else float(normal_vol_1m)
            ),
            spread_bps=float(tick.spread_bps),
            taker_buy_ratio=float(tick.taker_buy_ratio_5s),
            contagion=contagion,
            headline_shock=headline_shock,
            max_spread_bps=max_spread_bps,
            strategy_id=strategy_id,
        )


@dataclass(slots=True)
class InstinctDecision:
    """The lease that System 1 published plus its provenance."""

    mode: LeaseMode
    target_bps: float
    stop_bps: float
    provider_name: str
    used_fallback: bool
    latency_ms: float
    reason: str
    lease: System1Lease
    strategy_id: str = "system1_instinct"

    @property
    def is_armed(self) -> bool:
        return self.mode is not LeaseMode.DISARMED

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.lease.symbol,
            "mode": self.mode.name,
            "target_bps": self.target_bps,
            "stop_bps": self.stop_bps,
            "expires_at_ns": self.lease.expires_at_ns,
            "provider": self.provider_name,
            "used_fallback": self.used_fallback,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Deterministic instinct
# ---------------------------------------------------------------------------


def deterministic_instinct(context: InstinctContext) -> ProviderResponse:
    """Fail-safe local evaluator used whenever no LLM is healthy.

    This is deliberately conservative: the default answer is ``DISARMED``
    (``sec_018`` Law 4), long scalps require both executed-buy dominance and a
    calm tape, and a fragility halt forbids ``LONG_ONLY`` outright.
    """
    vol = context.vol_ratio
    if context.fragility_halted:
        if context.taker_buy_ratio <= 0.35 and vol < VOL_SPIKE_BREAKER:
            return ProviderResponse(
                LeaseMode.SHORT_ONLY,
                target_bps=10.0,
                stop_bps=5.0,
                confidence=0.4,
                rationale="contagion halt: short-only",
            )
        return ProviderResponse(
            LeaseMode.DISARMED,
            0.0,
            0.0,
            confidence=1.0,
            rationale="cascade fragility >= 0.75",
        )
    if not math.isfinite(vol) or vol >= VOL_SPIKE_BREAKER:
        return ProviderResponse(
            LeaseMode.DISARMED, 0.0, 0.0, confidence=1.0, rationale="vol breaker"
        )
    if context.spread_bps > context.max_spread_bps:
        return ProviderResponse(
            LeaseMode.DISARMED, 0.0, 0.0, confidence=1.0, rationale="spread cap"
        )
    ratio = context.taker_buy_ratio
    if ratio >= 0.65 and vol < 2.0:
        target = 8.0 + 12.0 * min(1.0, (ratio - 0.65) / 0.35)
        return ProviderResponse(
            LeaseMode.LONG_ONLY,
            target_bps=target,
            stop_bps=max(3.0, target * 0.5),
            confidence=0.55 + 0.4 * min(1.0, (ratio - 0.65) / 0.35),
            rationale="executed taker buy dominance",
        )
    if ratio <= 0.35 and vol < 2.0:
        target = 8.0 + 12.0 * min(1.0, (0.35 - ratio) / 0.35)
        return ProviderResponse(
            LeaseMode.SHORT_ONLY,
            target_bps=target,
            stop_bps=max(3.0, target * 0.5),
            confidence=0.55 + 0.4 * min(1.0, (0.35 - ratio) / 0.35),
            rationale="executed taker sell dominance",
        )
    if context.spread_bps <= 12.0 and vol < 1.5:
        return ProviderResponse(
            LeaseMode.MEAN_REV,
            target_bps=6.0,
            stop_bps=4.0,
            confidence=0.35,
            rationale="balanced flow inside a tight spread",
        )
    return ProviderResponse(
        LeaseMode.DISARMED, 0.0, 0.0, confidence=1.0, rationale="no edge"
    )


class System1InstinctController:
    """Routes, guards, and publishes the 60-second System 1 lease."""

    def __init__(
        self,
        switchboard: MultiLLMSwitchboard | None = None,
        lease_table: LeaseTableSHM | None = None,
        *,
        kill_latch: GlobalKillLatch | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
        ledger: Any | None = None,
        transport_pool: Any | None = None,
    ) -> None:
        self.switchboard = switchboard or MultiLLMSwitchboard()
        self.lease_table = lease_table
        self.kill_latch = kill_latch
        self.clock = clock
        self.ledger = ledger if ledger is not None else getattr(
            self.switchboard, "ledger", None
        )
        self.transport_pool = (
            transport_pool
            if transport_pool is not None
            else getattr(self.switchboard, "transport_pool", None)
        )
        self._latencies_ms: list[float] = []
        self._decisions: list[InstinctDecision] = []

    # -- evaluation -------------------------------------------------------
    def evaluate(
        self,
        context: InstinctContext | None = None,
        *,
        now_ns: int | None = None,
        **fields: Any,
    ) -> InstinctDecision:
        if context is None:
            context = InstinctContext(**fields)
        elif fields:
            raise TypeError("pass either an InstinctContext or keyword fields, not both")
        started = time.perf_counter()
        now = int(now_ns) if now_ns is not None else int(self.clock())

        if self.kill_latch is not None and self.kill_latch.is_halted:
            base = ProviderResponse(
                LeaseMode.DISARMED, 0.0, 0.0, 1.0, "global kill latch"
            )
            route = RoutingDecision(
                "kill-latch",
                "nios-native-instinct",
                None,
                True,
                0.0,
                "global kill latch hot",
            )
        else:
            base = deterministic_instinct(context)
            route = self.switchboard.route(context)

        mode, target, stop, reason = self._apply_guardrails(context, base, route)
        lease = System1Lease(
            symbol=context.symbol,
            mode=mode,
            target_bps=target,
            stop_bps=stop,
            expires_at_ns=now + LEASE_TTL_NS,
            strategy_id_hash=_strategy_hash(context.strategy_id),
        )
        if self.lease_table is not None:
            self.lease_table.write_lease(lease, now_ns=now)

        latency = (time.perf_counter() - started) * 1e3
        decision = InstinctDecision(
            mode=mode,
            target_bps=target,
            stop_bps=stop,
            provider_name=route.provider_name,
            used_fallback=route.used_fallback,
            latency_ms=latency,
            reason=reason,
            lease=lease,
            strategy_id=context.strategy_id,
        )
        self._latencies_ms.append(latency)
        self._decisions.append(decision)
        return decision

    # ``refresh`` is the architecture's verb; same behaviour.
    refresh = evaluate

    @staticmethod
    def _apply_guardrails(
        context: InstinctContext,
        base: ProviderResponse,
        route: RoutingDecision,
    ) -> tuple[LeaseMode, float, float, str]:
        mode = base.mode
        target = base.target_bps
        stop = base.stop_bps
        reason = base.rationale

        suggestion = route.suggestion
        if suggestion is not None:
            if suggestion.mode is LeaseMode.DISARMED:
                mode, target, stop = LeaseMode.DISARMED, 0.0, 0.0
                reason = f"llm veto ({route.provider_name})"
            elif suggestion.mode is mode:
                target = _clamp(suggestion.target_bps, 0.0, base.target_bps)
                stop = _clamp(suggestion.stop_bps, 0.0, max(base.stop_bps, 1e-9))
                reason = suggestion.rationale or base.rationale
            else:
                reason = (
                    f"{base.rationale}; llm mode {suggestion.mode.name} rejected "
                    "by asymmetric guardrail"
                )

        # Structural guards are absolute -- no LLM can widen them.
        if context.fragility_halted and mode is LeaseMode.LONG_ONLY:
            mode, target, stop = LeaseMode.DISARMED, 0.0, 0.0
            reason = "fragility override: LONG_ONLY forbidden"
        if context.vol_ratio >= VOL_SPIKE_BREAKER:
            mode, target, stop = LeaseMode.DISARMED, 0.0, 0.0
            reason = "vol spike breaker"
        if context.spread_bps > context.max_spread_bps:
            mode, target, stop = LeaseMode.DISARMED, 0.0, 0.0
            reason = "spread cap"
        if mode is LeaseMode.DISARMED:
            return LeaseMode.DISARMED, 0.0, 0.0, reason
        target = _clamp(target, 1.0, 40.0)
        stop = _clamp(stop if stop > 0.0 else target * 0.5, 1.0, 30.0)
        return mode, target, stop, reason

    # -- diagnostics ------------------------------------------------------
    @property
    def decisions(self) -> tuple[InstinctDecision, ...]:
        return tuple(self._decisions)

    def latency_percentile(self, q: float) -> float:
        if not self._latencies_ms:
            return 0.0
        ordered = sorted(self._latencies_ms)
        if len(ordered) == 1:
            return ordered[0]
        position = float(q) * (len(ordered) - 1)
        lower = int(math.floor(position))
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

    def latency_stats(self) -> dict[str, float]:
        return {
            "count": float(len(self._latencies_ms)),
            "p50_ms": self.latency_percentile(0.50),
            "p95_ms": self.latency_percentile(0.95),
            "p99_ms": self.latency_percentile(0.99),
            "max_ms": max(self._latencies_ms) if self._latencies_ms else 0.0,
        }

    def llm_state(self) -> dict[str, Any]:
        """Live switchboard + transport + token-budget telemetry."""
        state: dict[str, Any] = {"switchboard": self.switchboard.status()}
        if self.transport_pool is not None:
            state["transport"] = self.transport_pool.status()
        if self.ledger is not None:
            state["budget"] = self.ledger.snapshot()
        return state
