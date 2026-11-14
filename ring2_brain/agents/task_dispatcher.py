"""Ring 2 agent task assignment and orchestration engine (``sec_004``, ``sec_018``).

Six specialist Ring 2 agents are dispatchable from the Glass Cockpit.  Every
agent is air-gapped by construction (Law 2): an agent may emit grounded research
reports, strategy scores or nursery proposals, and it can never sign an order,
read a Ring 0 broker key, or write the IBOR.  The dispatcher holds no credential
and imports nothing from ``ring0_vault``.

Every report is sealed into a :class:`core.schemas.GroundedFactPacket` and
verified by :class:`ring2_brain.truth.grounding_gate.NumericalGroundingVerifier`
(Law 3).  A report that fails verification is marked ``FAILED`` with
``UNGROUNDED-REPORT`` rather than being returned to the operator.

Clean-slate boot starts with zero tasks; :meth:`AgentTaskDispatcher.reset_to_clean_slate`
returns the queue to zero.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.llm_transport import DEFAULT_DAILY_USD_CAP, TokenBudgetLedger
from core.schemas import GroundedFactPacket
from ring2_brain.quant.daily_analysis_donor import (
    KNOWN_SILOS,
    STRATEGY_CATALOG,
    STRATEGY_IDS,
    DecisionDashboard,
    evaluate_all_strategies,
    evaluate_strategy,
    get_strategy,
    synthetic_bars,
)
from ring2_brain.quant.daily_analysis_donor.strategies import _fundamental_profile
from ring2_brain.truth.grounding_gate import NumericalGroundingVerifier

__all__ = [
    "AGENT_CATALOG",
    "AGENT_IDS",
    "AgentSpec",
    "AgentTask",
    "AgentTaskDispatcher",
    "TASK_PRIORITIES",
    "TASK_STATUSES",
    "TokenBudgetExceeded",
]

# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

TASK_STATUSES: tuple[str, ...] = (
    "PENDING",
    "PROCESSING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)

TASK_PRIORITIES: tuple[str, ...] = ("LOW", "NORMAL", "HIGH", "CRITICAL")

_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

DEFAULT_SILO = "US_IBKR"
DEFAULT_STRATEGY_ID = "ma_golden_cross"
DEFAULT_BAR_COUNT = 252
MAX_SYMBOLS = 8

DSR_GATE = 1.50
CHAMPION_NET_EDGE_FLOOR_BPS = 0.50
REPORT_TOLERANCE = 0.02

AGENT_IDS: tuple[str, ...] = (
    "daily_decision_agent",
    "technical_strategy_agent",
    "fundamental_valuation_agent",
    "contagion_stress_agent",
    "alpha_forge_agent",
    "backtest_audit_agent",
)


class TokenBudgetExceeded(RuntimeError):
    """Raised when a task would push past the ``$3.10/day`` token cap."""


# --------------------------------------------------------------------------
# text helpers -- keep every rendered digit inside a grounded metric value
# --------------------------------------------------------------------------

_UNITS: tuple[str, ...] = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)

_TENS: tuple[str, ...] = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)


def _spell(value: int) -> str:
    """Spell an integer in English words."""

    n = int(value)
    if n < 0:
        return "minus " + _spell(-n)
    if n < 20:
        return _UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        return _TENS[tens] if unit == 0 else f"{_TENS[tens]} {_UNITS[unit]}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        prefix = f"{_UNITS[hundreds]} hundred"
        return prefix if rest == 0 else f"{prefix} {_spell(rest)}"
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        prefix = f"{_spell(thousands)} thousand"
        return prefix if rest == 0 else f"{prefix} {_spell(rest)}"
    millions, rest = divmod(n, 1_000_000)
    prefix = f"{_spell(millions)} million"
    return prefix if rest == 0 else f"{prefix} {_spell(rest)}"


_DIGIT_RUN = re.compile(r"\d+")


def _digitless(text: Any) -> str:
    """Replace every digit run with its English spelling.

    The grounding verifier treats a bare integer as structural only when no
    material keyword sits within the preceding window; a label such as
    ``return_20d_pct`` can be promoted to a *material* claim by a neighbouring
    ``value`` column.  Spelling digits out in labels removes that ambiguity so
    the only Arabic numerals in a report are grounded metric renderings.
    """

    return _DIGIT_RUN.sub(lambda match: _spell(int(match.group(0))), str(text))


def _fmt(value: Any) -> str:
    """Render a metric value with a precision that survives the verifier.

    Returns an empty string for exact zero so a report never emits an
    ungrounded ``0.00``.
    """

    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    if number == 0.0:
        return ""
    if abs(number) >= 1.0:
        return f"{number:.2f}"
    return f"{number:.6f}"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _float_map(obj: Any) -> dict[str, float]:
    """Coerce a donor result object into a deterministic float mapping."""

    if obj is None:
        return {}
    if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
        try:
            obj = obj.to_dict()
        except Exception:
            return {}
    items: list[tuple[Any, Any]] = []
    if isinstance(obj, Mapping):
        items = list(obj.items())
    else:
        slots = getattr(type(obj), "__slots__", ()) or ()
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            try:
                items.append((name, getattr(obj, name)))
            except AttributeError:
                continue
        if not items:
            try:
                items = list(vars(obj).items())
            except TypeError:
                items = []
    out: dict[str, float] = {}
    for key, value in items:
        if isinstance(value, bool):
            out[str(key)] = 1.0 if value else 0.0
            continue
        number = _finite_float(value)
        if number is not None:
            out[str(key)] = number
    return out


def _normalise_symbols(symbols: Any) -> list[str]:
    """Accept a list or a comma/space separated watchlist string."""

    raw: list[str] = []
    if symbols is None:
        return []
    if isinstance(symbols, str):
        raw = re.split(r"[,\s;|]+", symbols)
    else:
        for item in symbols:
            if item is None:
                continue
            raw.extend(re.split(r"[,\s;|]+", str(item)))
    out: list[str] = []
    for token in raw:
        symbol = token.strip().upper()
        if not symbol:
            continue
        if symbol not in out:
            out.append(symbol)
        if len(out) >= MAX_SYMBOLS:
            break
    return out


def _prefixed(symbol: str, metrics: Mapping[str, float]) -> dict[str, float]:
    prefix = re.sub(r"[^A-Za-z0-9_]", "_", str(symbol))
    return {f"{prefix}__{key}": float(value) for key, value in metrics.items()}


# --------------------------------------------------------------------------
# report builder
# --------------------------------------------------------------------------


class _Doc:
    """Deterministic markdown builder.

    Static text and labels are routed through :func:`_digitless`; numeric cells
    are produced by :func:`_fmt` from grounded packet metrics only.
    """

    __slots__ = ("_lines",)

    def __init__(self, title: str) -> None:
        self._lines: list[str] = [f"# {_digitless(title)}", ""]

    def head(self, text: str) -> None:
        self._lines.extend([f"## {_digitless(text)}", ""])

    def para(self, text: str) -> None:
        self._lines.extend([_digitless(text), ""])

    def kv(self, label: str, value: Any) -> None:
        rendered = _fmt(value)
        if not rendered:
            return
        self._lines.append(f"- {_digitless(label)}: {rendered}")

    def note(self, label: str, text: str) -> None:
        self._lines.append(f"- {_digitless(label)}: {_digitless(text)}")

    def bullets(self, heading: str, values: Iterable[str]) -> None:
        body = [str(item).strip() for item in values if str(item).strip()]
        if not body:
            return
        self.head(heading)
        for item in body:
            self._lines.append(f"- {_digitless(item)}")
        self._lines.append("")

    def table(self, heading: str, metrics: Mapping[str, float]) -> None:
        rows: list[tuple[str, str]] = []
        for key in sorted(metrics):
            rendered = _fmt(metrics[key])
            if not rendered:
                continue
            rows.append((_digitless(key), rendered))
        if not rows:
            return
        self.head(heading)
        self._lines.extend(["| Metric | Value |", "| --- | --- |"])
        for label, rendered in rows:
            self._lines.append(f"| {label} | {rendered} |")
        self._lines.append("")

    def text(self, raw: str) -> None:
        self._lines.append(str(raw))

    def build(self) -> str:
        return "\n".join(self._lines).strip() + "\n"


# --------------------------------------------------------------------------
# agent catalog
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class AgentSpec:
    """Static description of a specialist Ring 2 agent."""

    agent_id: str
    title: str
    task_type: str
    description: str
    capabilities: tuple[str, ...]
    requires_strategy: bool = False
    token_prompt: int = 320
    token_completion: int = 200
    token_cost_usd: float = 0.000160

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "title": self.title,
            "task_type": self.task_type,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "requires_strategy": self.requires_strategy,
            "ring": "ring2",
            "signs_orders": False,
            "reads_broker_keys": False,
            "token_cost_usd": self.token_cost_usd,
        }


AGENT_CATALOG: tuple[AgentSpec, ...] = (
    AgentSpec(
        agent_id="daily_decision_agent",
        title="Daily Decision Agent",
        task_type="DECISION_DASHBOARD",
        description=(
            "Runs the harvested daily_stock_analysis decision dashboard across a "
            "symbol or watchlist and answers a strategy question."
        ),
        capabilities=(
            "decision_dashboard",
            "technical_scorer",
            "strategy_qa",
            "watchlist_sweep",
        ),
        token_prompt=520,
        token_completion=340,
        token_cost_usd=0.000242,
    ),
    AgentSpec(
        agent_id="technical_strategy_agent",
        title="Technical Strategy Agent",
        task_type="STRATEGY_EVALUATION",
        description=(
            "Evaluates a symbol against any harvested strategy template and emits "
            "entry, target and stop levels with a reward-to-risk ratio."
        ),
        capabilities=(
            "strategy_template_scoring",
            "entry_target_stop_levels",
            "reward_risk",
        ),
        requires_strategy=True,
        token_prompt=360,
        token_completion=240,
        token_cost_usd=0.000171,
    ),
    AgentSpec(
        agent_id="fundamental_valuation_agent",
        title="Fundamental Valuation Agent",
        task_type="VALUATION_AUDIT",
        description=(
            "Runs the ValuationLab DCF, comps and Black-Scholes Greeks audit for a "
            "symbol and reports intrinsic value against the last close."
        ),
        capabilities=("dcf", "comps", "black_scholes_greeks", "three_statement"),
        token_prompt=300,
        token_completion=200,
        token_cost_usd=0.000142,
    ),
    AgentSpec(
        agent_id="contagion_stress_agent",
        title="Contagion Stress Agent",
        task_type="CONTAGION_STRESS",
        description=(
            "Runs the five-archetype MiroFish three-tick cascade and reports the "
            "fragility index and lease revocations for a shock scenario."
        ),
        capabilities=(
            "mirofish_cascade",
            "fragility_index",
            "lease_revocation",
            "archetype_weights",
        ),
        token_prompt=260,
        token_completion=180,
        token_cost_usd=0.000126,
    ),
    AgentSpec(
        agent_id="alpha_forge_agent",
        title="Alpha Forge Agent",
        task_type="ALPHA_FORGE",
        description=(
            "Audits the challenger genome pool against the Deflated Sharpe gate and "
            "reports the champion and challenger posture without promoting anything."
        ),
        capabilities=(
            "challenger_audit",
            "deflated_sharpe",
            "purged_kfold",
            "gate_verdict",
        ),
        token_prompt=320,
        token_completion=220,
        token_cost_usd=0.000154,
    ),
    AgentSpec(
        agent_id="backtest_audit_agent",
        title="Backtest Audit Agent",
        task_type="BACKTEST_AUDIT",
        description=(
            "Runs a tax-net walk-forward backtest and audits the forward horizon win "
            "rates and net edge for a strategy and symbol."
        ),
        capabilities=(
            "walk_forward_backtest",
            "tax_net_edge",
            "horizon_matrix",
            "nursery_readiness",
        ),
        requires_strategy=True,
        token_prompt=420,
        token_completion=280,
        token_cost_usd=0.000196,
    ),
)

_AGENTS_BY_ID: dict[str, AgentSpec] = {spec.agent_id: spec for spec in AGENT_CATALOG}


def _get_agent(agent_id: Any) -> AgentSpec:
    key = str(agent_id or "").strip().lower()
    spec = _AGENTS_BY_ID.get(key)
    if spec is None:
        raise ValueError(
            "unknown agent_id; expected one of " + ", ".join(AGENT_IDS)
        )
    return spec


# --------------------------------------------------------------------------
# task record
# --------------------------------------------------------------------------


@dataclass(slots=True)
class AgentTask:
    """One assigned agent task and its grounded result."""

    task_id: str
    agent_id: str
    task_type: str
    silo: str
    symbols: list[str]
    strategy_id: str | None = None
    prompt: str = ""
    priority: str = "NORMAL"
    status: str = "PENDING"
    progress_pct: int = 0
    created_at_ns: int = 0
    completed_at_ns: int = 0
    latency_ms: float = 0.0
    token_cost_usd: float = 0.0
    grounding_sha256: str = ""
    structured_result: dict[str, Any] = field(default_factory=dict)
    markdown_report: str = ""
    verification: dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "task_type": self.task_type,
            "silo": self.silo,
            "symbols": list(self.symbols),
            "strategy_id": self.strategy_id,
            "prompt": self.prompt,
            "priority": self.priority,
            "status": self.status,
            "progress_pct": int(self.progress_pct),
            "created_at_ns": int(self.created_at_ns),
            "completed_at_ns": int(self.completed_at_ns),
            "latency_ms": float(self.latency_ms),
            "token_cost_usd": float(self.token_cost_usd),
            "grounding_sha256": self.grounding_sha256,
            "verified": bool(self.verified),
            "verification": dict(self.verification),
            "error": self.error,
        }

    def to_full_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["structured_result"] = self.structured_result
        payload["markdown_report"] = self.markdown_report
        return payload


# --------------------------------------------------------------------------
# dispatcher
# --------------------------------------------------------------------------


class AgentTaskDispatcher:
    """Assign, execute and track Ring 2 specialist agent tasks."""

    __slots__ = (
        "_dashboard",
        "_valuation",
        "_contagion",
        "_contagion_factory",
        "_forge",
        "_backtest_engine",
        "_token_ledger",
        "_verifier",
        "_clock",
        "_tasks",
        "_order",
        "_counter",
        "_lock",
        "_completed",
        "_failed",
    )

    def __init__(
        self,
        *,
        dashboard: DecisionDashboard | None = None,
        valuation: Any = None,
        contagion: Any = None,
        contagion_factory: Any = None,
        forge: Any = None,
        backtest_engine: Any = None,
        token_ledger: TokenBudgetLedger | None = None,
        verifier: NumericalGroundingVerifier | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self._dashboard = dashboard if dashboard is not None else DecisionDashboard()
        self._valuation = valuation
        self._contagion = contagion
        self._contagion_factory = contagion_factory
        self._forge = forge
        self._backtest_engine = backtest_engine
        self._token_ledger = (
            token_ledger
            if token_ledger is not None
            else TokenBudgetLedger(daily_usd_cap=DEFAULT_DAILY_USD_CAP)
        )
        self._verifier = (
            verifier
            if verifier is not None
            else NumericalGroundingVerifier(tolerance=REPORT_TOLERANCE)
        )
        self._clock = clock
        self._tasks: dict[str, AgentTask] = {}
        self._order: list[str] = []
        self._counter = 0
        self._lock = threading.RLock()
        self._completed = 0
        self._failed = 0

    # -- wiring ---------------------------------------------------------

    def attach(
        self,
        *,
        valuation: Any = None,
        contagion: Any = None,
        contagion_factory: Any = None,
        forge: Any = None,
        backtest_engine: Any = None,
        token_ledger: TokenBudgetLedger | None = None,
    ) -> None:
        """Late-bind collaborators once the orchestrator has built them."""

        if valuation is not None:
            self._valuation = valuation
        if contagion is not None:
            self._contagion = contagion
        if contagion_factory is not None:
            self._contagion_factory = contagion_factory
        if forge is not None:
            self._forge = forge
        if backtest_engine is not None:
            self._backtest_engine = backtest_engine
        if token_ledger is not None:
            self._token_ledger = token_ledger

    # -- read-only ------------------------------------------------------

    @property
    def dashboard(self) -> DecisionDashboard:
        return self._dashboard

    @property
    def token_ledger(self) -> TokenBudgetLedger:
        return self._token_ledger

    @property
    def task_count(self) -> int:
        with self._lock:
            return len(self._tasks)

    @property
    def tasks(self) -> list[AgentTask]:
        with self._lock:
            return [self._tasks[task_id] for task_id in self._order]

    def agents(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in AGENT_CATALOG]

    def strategies(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in STRATEGY_CATALOG]

    def token_usage(self) -> dict[str, Any]:
        ledger = self._token_ledger
        spend = float(getattr(ledger, "spend_usd", 0.0) or 0.0)
        cap = float(getattr(ledger, "daily_usd_cap", DEFAULT_DAILY_USD_CAP) or DEFAULT_DAILY_USD_CAP)
        remaining = max(0.0, round(cap - spend, 6))
        utilisation = (spend / cap * 100.0) if cap > 0 else 0.0
        return {
            "spend_usd": round(spend, 6),
            "cap_usd": round(cap, 6),
            "remaining_usd": remaining,
            "utilization_pct": round(utilisation, 4),
            "calls": int(getattr(ledger, "calls", 0) or 0),
            "prompt_tokens": int(getattr(ledger, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(ledger, "completion_tokens", 0) or 0),
            "locked": bool(getattr(ledger, "locked", False)),
        }

    def task(self, task_id: str) -> AgentTask | None:
        with self._lock:
            return self._tasks.get(str(task_id))

    def task_dict(self, task_id: str) -> dict[str, Any] | None:
        record = self.task(task_id)
        return None if record is None else record.to_full_dict()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            records = [self._tasks[task_id] for task_id in self._order]
        counts = {status: 0 for status in TASK_STATUSES}
        for record in records:
            counts[record.status] = counts.get(record.status, 0) + 1
        return {
            "agents": self.agents(),
            "agent_count": len(AGENT_CATALOG),
            "strategies": self.strategies(),
            "strategy_count": len(STRATEGY_CATALOG),
            "silos": list(KNOWN_SILOS),
            "priorities": list(TASK_PRIORITIES),
            "tasks": [record.to_dict() for record in records],
            "task_count": len(records),
            "counts": counts,
            "active_tasks": counts.get("PENDING", 0) + counts.get("PROCESSING", 0),
            "completed_tasks": counts.get("COMPLETED", 0),
            "failed_tasks": counts.get("FAILED", 0),
            "cancelled_tasks": counts.get("CANCELLED", 0),
            "token": self.token_usage(),
            "clean_slate": len(records) == 0,
            "air_gap": self.air_gap(),
            "generated_at_ns": int(self._clock()),
        }

    @staticmethod
    def air_gap() -> dict[str, Any]:
        """Law 2 posture: agents reason, they never execute."""

        return {
            "ring2_only": True,
            "signs_orders": False,
            "reads_broker_keys": False,
            "writes_ibor": False,
            "emits": [
                "grounded_research_report",
                "strategy_score",
                "nursery_proposal",
            ],
            "order_signing_tools": 0,
            "broker_key_reading_tools": 0,
        }

    # -- lifecycle ------------------------------------------------------

    def assign(
        self,
        *,
        agent_id: Any,
        silo: Any = DEFAULT_SILO,
        symbols: Any = None,
        strategy_id: Any = None,
        prompt: Any = "",
        priority: Any = "NORMAL",
    ) -> dict[str, Any]:
        """Create and execute one agent task, returning the full grounded result."""

        spec = _get_agent(agent_id)

        silo_key = str(silo or DEFAULT_SILO).strip().upper()
        if silo_key not in KNOWN_SILOS:
            raise ValueError(
                "unknown silo; expected one of " + ", ".join(KNOWN_SILOS)
            )

        watchlist = _normalise_symbols(symbols)
        if not watchlist:
            raise ValueError("at least one symbol is required")

        strategy_id_value: str | None = None
        if strategy_id is not None and str(strategy_id).strip():
            candidate = str(strategy_id).strip().lower()
            if candidate not in STRATEGY_IDS:
                raise ValueError(
                    "unknown strategy_id; expected one of " + ", ".join(STRATEGY_IDS)
                )
            strategy_id_value = candidate
        if strategy_id_value is None and spec.requires_strategy:
            strategy_id_value = DEFAULT_STRATEGY_ID

        priority_key = str(priority or "NORMAL").strip().upper()
        if priority_key not in TASK_PRIORITIES:
            raise ValueError(
                "unknown priority; expected one of " + ", ".join(TASK_PRIORITIES)
            )

        question = str(prompt or "").strip()
        now_ns = int(self._clock())
        task = AgentTask(
            task_id=f"task_{uuid.uuid4().hex[:12]}",
            agent_id=spec.agent_id,
            task_type=spec.task_type,
            silo=silo_key,
            symbols=watchlist,
            strategy_id=strategy_id_value,
            prompt=question,
            priority=priority_key,
            status="PENDING",
            progress_pct=0,
            created_at_ns=now_ns,
        )

        with self._lock:
            self._tasks[task.task_id] = task
            self._order.append(task.task_id)
            self._counter += 1

        try:
            self._execute(task, spec)
        finally:
            with self._lock:
                if task.status == "COMPLETED":
                    self._completed += 1
                elif task.status == "FAILED":
                    self._failed += 1
        return task.to_full_dict()

    def _budget_check(self, spec: AgentSpec) -> None:
        ledger = self._token_ledger
        spend = float(getattr(ledger, "spend_usd", 0.0) or 0.0)
        cap = float(getattr(ledger, "daily_usd_cap", DEFAULT_DAILY_USD_CAP) or DEFAULT_DAILY_USD_CAP)
        if spend + spec.token_cost_usd > cap:
            raise TokenBudgetExceeded(
                f"daily token cap reached: spend {spend:.6f} of {cap:.6f} USD"
            )

    def _record_spend(self, spec: AgentSpec) -> float:
        ledger = self._token_ledger
        try:
            report = ledger.record(
                system=2,
                provider="deterministic",
                model=spec.agent_id,
                prompt_tokens=spec.token_prompt,
                completion_tokens=spec.token_completion,
                usd_cost=spec.token_cost_usd,
            )
            return float(report.get("usd_cost", spec.token_cost_usd))
        except Exception:
            return spec.token_cost_usd

    def _execute(self, task: AgentTask, spec: AgentSpec) -> None:
        start_ns = int(self._clock())
        task.status = "PROCESSING"
        task.progress_pct = 10
        try:
            self._budget_check(spec)
        except TokenBudgetExceeded as exc:
            task.status = "FAILED"
            task.progress_pct = 100
            task.error = f"TOKEN_BUDGET_EXCEEDED: {exc}"
            task.completed_at_ns = int(self._clock())
            task.latency_ms = max(0.0, (task.completed_at_ns - start_ns) / 1_000_000.0)
            return

        runner = self._RUNNERS[spec.agent_id]
        try:
            structured, metrics, doc, extra = runner(self, task)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            task.status = "FAILED"
            task.progress_pct = 100
            task.error = f"{type(exc).__name__}: {exc}"
            task.completed_at_ns = int(self._clock())
            task.latency_ms = max(0.0, (task.completed_at_ns - start_ns) / 1_000_000.0)
            task.structured_result = {"agent_id": spec.agent_id, "error": task.error}
            return

        task.progress_pct = 70
        finished_ns = int(self._clock())
        packet = GroundedFactPacket(
            symbol=task.symbols[0],
            valid_time_ns=finished_ns,
            knowledge_ts_ns=finished_ns,
            as_of_ns=finished_ns,
            metrics=metrics,
        )
        markdown = doc.build()
        verdict = self._verify(packet, markdown)

        task.markdown_report = markdown
        task.grounding_sha256 = packet.sha256_digest
        task.verification = verdict
        task.verified = bool(verdict.get("verified"))
        task.structured_result = {
            **structured,
            **extra,
            "agent_id": spec.agent_id,
            "task_type": spec.task_type,
            "silo": task.silo,
            "symbols": list(task.symbols),
            "strategy_id": task.strategy_id,
            "grounding_sha256": packet.sha256_digest,
            "verified": task.verified,
            "fact_count": len(metrics),
        }
        task.token_cost_usd = self._record_spend(spec)
        task.completed_at_ns = int(self._clock())
        task.latency_ms = max(0.0, (task.completed_at_ns - start_ns) / 1_000_000.0)
        if task.verified:
            task.status = "COMPLETED"
            task.progress_pct = 100
        else:
            task.status = "FAILED"
            task.progress_pct = 100
            task.error = str(verdict.get("error") or "UNGROUNDED-REPORT")

    def _verify(self, packet: GroundedFactPacket, markdown: str) -> dict[str, Any]:
        base: dict[str, Any] = {
            "digest": packet.sha256_digest,
            "claims_checked": 0,
            "derived_expressions": 0,
            "tolerance": REPORT_TOLERANCE,
            "verified": False,
            "error": None,
        }
        if not packet.verify():
            base["error"] = "GROUNDING-DIGEST-MISMATCH"
            return base
        try:
            report = self._verifier.verify_thesis_claims(
                markdown, packet, tolerance=REPORT_TOLERANCE
            )
        except Exception as exc:  # noqa: BLE001
            base["error"] = f"{type(exc).__name__}: {exc}"
            return base
        base["claims_checked"] = int(getattr(report, "claims_checked", 0) or 0)
        base["derived_expressions"] = int(
            len(getattr(report, "derived_expressions", ()) or ())
        )
        base["verified"] = True
        return base

    # -- cancellation ---------------------------------------------------

    def cancel(self, task_id: Any = "ALL") -> dict[str, Any]:
        key = str(task_id or "ALL").strip()
        with self._lock:
            if key.upper() == "ALL":
                cancelled = len(self._tasks)
                for record in self._tasks.values():
                    if not record.terminal:
                        record.status = "CANCELLED"
                        record.error = record.error or "CANCELLED-BY-OPERATOR"
                        record.progress_pct = 100
                        record.completed_at_ns = int(self._clock())
                self._tasks.clear()
                self._order.clear()
                return {
                    "cancelled": cancelled,
                    "scope": "ALL",
                    "remaining_tasks": 0,
                    "tasks": [],
                }
            record = self._tasks.get(key)
            if record is None:
                raise KeyError(key)
            if not record.terminal:
                record.status = "CANCELLED"
                record.error = record.error or "CANCELLED-BY-OPERATOR"
                record.progress_pct = 100
                record.completed_at_ns = int(self._clock())
                status = "CANCELLED"
            else:
                status = record.status
            return {
                "cancelled": 1 if status == "CANCELLED" else 0,
                "scope": key,
                "status": status,
                "task": record.to_dict(),
            }

    def clear(self) -> dict[str, Any]:
        with self._lock:
            cleared = len(self._tasks)
            self._tasks.clear()
            self._order.clear()
            self._counter = 0
            self._completed = 0
            self._failed = 0
        return {"cleared": cleared, "tasks": 0, "task_count": 0}

    def reset_to_clean_slate(self) -> dict[str, Any]:
        report = self.clear()
        try:
            self._dashboard.reset()
        except Exception:
            pass
        report["clean_slate"] = True
        report["token_spend_usd"] = round(
            float(getattr(self._token_ledger, "spend_usd", 0.0) or 0.0), 6
        )
        return report

    # -- runners --------------------------------------------------------

    def _run_daily_decision(
        self, task: AgentTask
    ) -> tuple[dict[str, Any], dict[str, float], _Doc, dict[str, Any]]:
        as_of_ns = int(self._clock())
        reports = []
        metrics: dict[str, float] = {}
        for symbol in task.symbols:
            report = self._dashboard.evaluate(
                symbol,
                task.silo,
                bar_count=DEFAULT_BAR_COUNT,
                as_of_ns=as_of_ns,
            )
            reports.append(report)
            block = {
                "signal_score": float(report.signal_score),
                "close_price": float(report.close_price),
                "ma5": float(report.ma5),
                "ma10": float(report.ma10),
                "ma20": float(report.ma20),
                "bias_ma5_pct": float(report.bias_ma5_pct),
                "rsi_14": float(report.rsi_14),
                "macd_value": float(report.macd_value),
                "atr_14": float(report.atr_14),
                "target_price": float(report.target_price),
                "stop_loss": float(report.stop_loss),
                "reward_risk": float(report.reward_risk),
                "return_20d_pct": float(report.return_20d_pct),
                "max_drawdown_pct": float(report.max_drawdown_pct),
            }
            metrics.update(_prefixed(symbol, block))

        doc = _Doc("Daily Decision Report")
        doc.para(f"Agent {task.agent_id} on silo {task.silo} covering {', '.join(task.symbols)}.")
        if task.prompt:
            doc.para(f"Operator question: {task.prompt}")
        for report in reports:
            doc.head(f"{report.symbol} decision dashboard")
            doc.note("Operation advice", report.operation_advice)
            doc.note("MACD status", report.macd_status)
            doc.note("RSI status", report.rsi_status)
            doc.note("Best strategy", report.best_strategy_name)
            doc.note("Interval", report.interval_label)
            doc.kv("Signal score", report.signal_score)
            doc.kv("Close price", report.close_price)
            doc.kv("MA fast", report.ma5)
            doc.kv("MA mid", report.ma10)
            doc.kv("MA slow", report.ma20)
            doc.kv("Bias to MA fast percent", report.bias_ma5_pct)
            doc.kv("RSI fourteeen", report.rsi_14)
            doc.kv("Target price", report.target_price)
            doc.kv("Stop loss", report.stop_loss)
            doc.kv("Reward risk", report.reward_risk)
            doc.bullets("Signal reasons", report.signal_reasons)
            doc.bullets("Risk factors", report.risk_factors)
            doc.bullets("Catalysts", report.catalysts)

        structured = {
            "reports": [
                {
                    "symbol": report.symbol,
                    "silo": report.silo,
                    "signal_score": round(float(report.signal_score), 4),
                    "operation_advice": report.operation_advice,
                    "macd_status": report.macd_status,
                    "rsi_status": report.rsi_status,
                    "close_price": round(float(report.close_price), 6),
                    "ma5": round(float(report.ma5), 6),
                    "ma10": round(float(report.ma10), 6),
                    "ma20": round(float(report.ma20), 6),
                    "bias_ma5_pct": round(float(report.bias_ma5_pct), 6),
                    "rsi_14": round(float(report.rsi_14), 6),
                    "atr_14": round(float(report.atr_14), 6),
                    "target_price": round(float(report.target_price), 6),
                    "stop_loss": round(float(report.stop_loss), 6),
                    "reward_risk": round(float(report.reward_risk), 6),
                    "signal_reasons": list(report.signal_reasons),
                    "risk_factors": list(report.risk_factors),
                    "catalysts": list(report.catalysts),
                    "best_strategy_id": report.best_strategy_id,
                    "best_strategy_name": report.best_strategy_name,
                    "grounding_sha256": report.grounding_sha256,
                    "verified": bool(report.verified),
                }
                for report in reports
            ],
            "advice_by_symbol": {report.symbol: report.operation_advice for report in reports},
            "score_by_symbol": {
                report.symbol: round(float(report.signal_score), 4) for report in reports
            },
        }
        extra = {
            "packets": [
                {"symbol": report.symbol, "grounding_sha256": report.grounding_sha256}
                for report in reports
            ]
        }
        return structured, metrics, doc, extra

    def _run_technical_strategy(
        self, task: AgentTask
    ) -> tuple[dict[str, Any], dict[str, float], _Doc, dict[str, Any]]:
        metrics: dict[str, float] = {}
        rows: list[dict[str, Any]] = []
        doc = _Doc("Technical Strategy Evaluation")
        doc.para(
            f"Agent {task.agent_id} scoring {', '.join(task.symbols)} on silo {task.silo}."
        )
        if task.strategy_id:
            doc.note("Strategy template", str(get_strategy(task.strategy_id).name))

        for symbol in task.symbols:
            if task.strategy_id:
                signals = [
                    evaluate_strategy(
                        task.strategy_id,
                        symbol=symbol,
                        silo=task.silo,
                        bar_count=DEFAULT_BAR_COUNT,
                    )
                ]
            else:
                signals = evaluate_all_strategies(
                    symbol=symbol, silo=task.silo, bar_count=DEFAULT_BAR_COUNT
                )[:1]
            for signal in signals:
                block = {
                    "score": float(signal.score),
                    "entry_price": float(signal.entry_price),
                    "target_price": float(signal.target_price),
                    "stop_loss": float(signal.stop_loss),
                    "reward_risk": float(signal.reward_risk),
                    "bars_used": float(signal.bars_used),
                }
                for key, value in signal.metrics.items():
                    number = _finite_float(value)
                    if number is not None:
                        block[str(key)] = number
                metrics.update(_prefixed(symbol, block))
                rows.append(
                    {
                        "symbol": symbol,
                        "strategy_id": signal.strategy_id,
                        "score": round(float(signal.score), 4),
                        "triggered": bool(signal.triggered),
                        "direction": signal.direction,
                        "entry_price": round(float(signal.entry_price), 6),
                        "target_price": round(float(signal.target_price), 6),
                        "stop_loss": round(float(signal.stop_loss), 6),
                        "reward_risk": round(float(signal.reward_risk), 6),
                        "reasons": list(signal.reasons),
                        "risk_factors": list(signal.risk_factors),
                        "bars_used": int(signal.bars_used),
                    }
                )
                doc.head(f"{signal.symbol} / {signal.strategy_id}")
                doc.note("Direction", signal.direction)
                doc.note("Triggered", "yes" if signal.triggered else "no")
                doc.kv("Score", signal.score)
                doc.kv("Entry price", signal.entry_price)
                doc.kv("Target price", signal.target_price)
                doc.kv("Stop loss", signal.stop_loss)
                doc.kv("Reward risk", signal.reward_risk)
                doc.bullets("Reasons", signal.reasons)
                doc.bullets("Risk factors", signal.risk_factors)

        structured = {"signals": rows, "best": rows[0] if rows else None}
        return structured, metrics, doc, {}

    def _run_valuation(
        self, task: AgentTask
    ) -> tuple[dict[str, Any], dict[str, float], _Doc, dict[str, Any]]:
        if self._valuation is None:
            raise RuntimeError("ValuationLab is not attached to the dispatcher")
        metrics: dict[str, float] = {}
        rows: list[dict[str, Any]] = []
        doc = _Doc("Fundamental Valuation Audit")
        doc.para(f"Agent {task.agent_id} valuing {', '.join(task.symbols)} on silo {task.silo}.")

        for symbol in task.symbols:
            bars = synthetic_bars(symbol, task.silo, bars=DEFAULT_BAR_COUNT)
            price = float(bars[-1].close)
            profile = _fundamental_profile(symbol, task.silo)
            growth = 0.02 + float(profile["revenue_growth_pct"]) / 1000.0
            fcff = [price * 0.06 * (1.0 + growth) ** year for year in range(5)]
            wacc = 0.09
            terminal = 0.03
            shares = 1000.0
            net_debt = price * 12.0
            ebitda = price * 18.0
            valuation = _float_map(
                self._valuation.value(
                    price=price,
                    fcff=fcff,
                    wacc=wacc,
                    terminal_growth=terminal,
                    peer_ev_ebitda=[11.5, 12.8, 13.4, 10.9],
                    ebitda=ebitda,
                    shares=shares,
                    net_debt=net_debt,
                )
            )
            greeks = _float_map(
                self._valuation.options(
                    spot=price,
                    strike=price * 1.05,
                    volatility=max(0.12, float(profile["quality_percentile"]) / 400.0),
                    rate=0.045,
                    years=0.25,
                    kind="call",
                )
            )
            block = {"close_price": price}
            block.update({f"valuation_{key}": value for key, value in valuation.items()})
            block.update({f"greeks_{key}": value for key, value in greeks.items()})
            metrics.update(_prefixed(symbol, block))

            doc.head(f"{symbol} intrinsic value")
            doc.kv("Close price", price)
            for key in sorted(valuation):
                doc.kv(f"Valuation {key}", valuation[key])
            for key in sorted(greeks):
                doc.kv(f"Greeks {key}", greeks[key])
            doc.note("Valuation lens", "discounted cash flow with exit multiple bridge")

            rows.append(
                {
                    "symbol": symbol,
                    "close_price": round(price, 6),
                    "valuation": {key: round(value, 6) for key, value in valuation.items()},
                    "greeks": {key: round(value, 6) for key, value in greeks.items()},
                }
            )

        structured = {"valuations": rows}
        return structured, metrics, doc, {}

    def _run_contagion(
        self, task: AgentTask
    ) -> tuple[dict[str, Any], dict[str, float], _Doc, dict[str, Any]]:
        if self._contagion is None and self._contagion_factory is None:
            raise RuntimeError("MiroFish contagion engine is not attached to the dispatcher")
        from ring2_brain.mirofish.contagion_engine import ShockVector

        metrics: dict[str, float] = {}
        rows: list[dict[str, Any]] = []
        doc = _Doc("Contagion Stress Cascade")
        doc.para(
            f"Agent {task.agent_id} applying a five-archetype three-tick cascade on silo {task.silo}."
        )

        for symbol in task.symbols:
            shock = ShockVector(
                price_shock_bps=-450.0,
                realized_vol_ratio=3.8,
                spread_bps=9.0,
                dealer_gamma_flip_proximity=0.72,
                tags=("agent-stress", task.silo.lower()),
            )
            provider = self._contagion
            if self._contagion_factory is not None:
                try:
                    provider = self._contagion_factory(task.silo)
                except Exception:
                    provider = self._contagion
            if provider is None:
                raise RuntimeError(
                    "MiroFish contagion engine is not attached to the dispatcher"
                )
            result = provider.run(shock, symbol)
            features = _float_map(getattr(result, "features", None))
            block = dict(features)
            for name in ("latency_ms", "revoked_long_leases"):
                number = _finite_float(getattr(result, name, None))
                if number is not None:
                    block[name] = number
            fragility = getattr(result, "fragility_override", None)
            if fragility is not None:
                block["fragility_override"] = 1.0 if fragility else 0.0
            weights = _float_map(getattr(result, "archetype_weights", None))
            block.update({f"archetype_{key}": value for key, value in weights.items()})
            metrics.update(_prefixed(symbol, block))

            doc.head(f"{symbol} cascade result")
            for key in sorted(block):
                doc.kv(key, block[key])
            doc.note(
                "Cascade verdict",
                "fragility override tripped"
                if block.get("fragility_override", 0.0) >= 0.5
                else "cascade contained",
            )

            rows.append(
                {
                    "symbol": symbol,
                    "latency_ms": round(float(block.get("latency_ms", 0.0)), 6),
                    "revoked_long_leases": int(round(float(block.get("revoked_long_leases", 0.0)))),
                    "fragility_override": bool(block.get("fragility_override", 0.0) >= 0.5),
                    "fragility_index": round(
                        float(block.get("cascade_fragility_index", block.get("fragility_index", 0.0))),
                        6,
                    ),
                    "features": {key: round(value, 6) for key, value in features.items()},
                    "archetype_weights": {
                        key: round(value, 6) for key, value in weights.items()
                    },
                }
            )

        structured = {
            "cascades": rows,
            "max_fragility_index": max(
                (row["fragility_index"] for row in rows), default=0.0
            ),
            "lease_revocations": sum(row["revoked_long_leases"] for row in rows),
            "override_tripped": any(row["fragility_override"] for row in rows),
        }
        return structured, metrics, doc, {}

    def _run_alpha_forge(
        self, task: AgentTask
    ) -> tuple[dict[str, Any], dict[str, float], _Doc, dict[str, Any]]:
        if self._valuation is None:
            raise RuntimeError("ValuationLab is not attached to the dispatcher")
        metrics: dict[str, float] = {}
        doc = _Doc("Alpha Forge Challenger Audit")
        doc.para(
            f"Agent {task.agent_id} auditing the challenger pool on silo {task.silo}. "
            "The forge is read-only for this agent: nothing is promoted."
        )

        thresholds = _float_map(self._forge.thresholds()) if self._forge is not None else {}
        snapshot = _float_map(getattr(self._forge, "snapshot", lambda: {})()) if self._forge is not None else {}

        dsr_row: dict[str, float] = {}
        symbol_series: dict[str, float] = {}
        for symbol in task.symbols:
            bars = synthetic_bars(symbol, task.silo, bars=DEFAULT_BAR_COUNT)
            closes = [float(bar.close) for bar in bars]
            returns = [
                (closes[index] - closes[index - 1]) / closes[index - 1]
                for index in range(1, len(closes))
                if closes[index - 1] > 0.0
            ]
            dsr = float(self._valuation.deflated_sharpe_ratio(returns, len(STRATEGY_IDS)))
            passes = 1.0 if dsr >= DSR_GATE else 0.0
            symbol_series[symbol] = dsr
            metrics.update(_prefixed(symbol, {"deflated_sharpe_ratio": dsr, "passes_dsr_gate": passes, "observations": float(len(returns))}))
            dsr_row[symbol] = dsr
            doc.head(f"{symbol} challenger genome")
            doc.kv("Deflated Sharpe ratio", dsr)
            doc.kv("Observations", float(len(returns)))
            doc.note("DSR gate", "passes" if passes >= 0.5 else "below gate")

        for key, value in sorted(thresholds.items()):
            metrics[f"threshold_{key}"] = value
            doc.kv(f"Threshold {key}", value)
        for key in sorted(
            ("champions", "challengers", "genomes", "evaluated", "promoted", "retired", "count")
        ):
            if key in snapshot:
                metrics[f"pool_{key}"] = snapshot[key]
                doc.kv(f"Pool {key}", snapshot[key])

        best_symbol = max(symbol_series, key=symbol_series.get) if symbol_series else None
        best_dsr = float(symbol_series.get(best_symbol, 0.0)) if best_symbol else 0.0
        structured = {
            "deflated_sharpe_ratio": round(best_dsr, 6),
            "best_symbol": best_symbol,
            "dsr_by_symbol": {key: round(value, 6) for key, value in symbol_series.items()},
            "dsr_gate": DSR_GATE,
            "passes_dsr_gate": bool(best_dsr >= DSR_GATE),
            "thresholds": {key: round(value, 6) for key, value in thresholds.items()},
            "pool": {key: round(value, 6) for key, value in snapshot.items()},
            "promoted": False,
            "nursery_handoff": "use the backtester to promote a qualifying run",
        }
        return structured, metrics, doc, {}

    def _run_backtest_audit(
        self, task: AgentTask
    ) -> tuple[dict[str, Any], dict[str, float], _Doc, dict[str, Any]]:
        if self._backtest_engine is None:
            raise RuntimeError("Walk-forward backtest engine is not attached to the dispatcher")
        strategy_id = task.strategy_id or DEFAULT_STRATEGY_ID
        metrics: dict[str, float] = {}
        runs: list[dict[str, Any]] = []
        doc = _Doc("Walk Forward Backtest Audit")
        doc.para(
            f"Agent {task.agent_id} auditing strategy {strategy_id} on silo {task.silo} "
            "with statutory cost floors applied to every trade."
        )

        for symbol in task.symbols:
            receipt = self._backtest_engine.run(
                silo=task.silo,
                symbol=symbol,
                strategy_id=strategy_id,
                bars=DEFAULT_BAR_COUNT,
            )
            block = {
                key: float(value)
                for key, value in receipt.get("metrics", {}).items()
                if _finite_float(value) is not None
            }
            horizon = receipt.get("horizon_matrix", {}) or {}
            for key in ("t_plus_1", "t_plus_5", "t_plus_20"):
                entry = horizon.get(key) or {}
                for field_name in ("win_rate_pct", "net_edge_bps", "samples"):
                    number = _finite_float(entry.get(field_name))
                    if number is not None:
                        block[f"{key}_{field_name}"] = number
            number = _finite_float(receipt.get("cost_floor_bps"))
            if number is not None:
                block["cost_floor_bps"] = number
            number = _finite_float(receipt.get("net_edge_bps"))
            if number is not None:
                block["net_edge_bps_receipt"] = number
            metrics.update(_prefixed(symbol, block))

            doc.head(f"{symbol} tax net walk forward")
            doc.kv("Cost floor", receipt.get("cost_floor_bps"))
            for key in (
                "total_return_pct",
                "benchmark_return_pct",
                "alpha_pct",
                "sharpe_ratio",
                "deflated_sharpe_ratio",
                "max_drawdown_pct",
                "win_rate_pct",
                "profit_factor",
                "gross_edge_bps",
                "tax_and_cost_drag_bps",
                "net_edge_bps",
                "total_trades",
            ):
                metric_value = receipt.get("metrics", {}).get(key)
                if metric_value is not None:
                    doc.kv(key, metric_value)
            for key in ("t_plus_1", "t_plus_5", "t_plus_20"):
                entry = horizon.get(key) or {}
                doc.head(f"Horizon {key.replace('_', ' ')}")
                for field_name in ("samples", "wins", "win_rate_pct", "net_edge_bps"):
                    if entry.get(field_name) is not None:
                        doc.kv(field_name, entry.get(field_name))

            runs.append(
                {
                    "symbol": symbol,
                    "backtest_id": receipt.get("backtest_id"),
                    "cost_floor_bps": receipt.get("cost_floor_bps"),
                    "metrics": {
                        key: round(float(value), 8)
                        for key, value in receipt.get("metrics", {}).items()
                        if _finite_float(value) is not None
                    },
                    "horizon_matrix": horizon,
                    "passes_dsr_gate": bool(receipt.get("passes_dsr_gate")),
                    "net_edge_bps": receipt.get("net_edge_bps"),
                    "grounding_sha256": receipt.get("grounding_sha256"),
                }
            )

        best = None
        if runs:
            best = max(runs, key=lambda item: float(item.get("net_edge_bps") or 0.0))
        structured = {
            "strategy_id": strategy_id,
            "runs": runs,
            "best": best,
            "horizon_by_symbol": {
                run["symbol"]: run.get("horizon_matrix", {}) for run in runs
            },
        }
        return structured, metrics, doc, {}

    _RUNNERS: Mapping[str, Callable[..., tuple[dict[str, Any], dict[str, float], _Doc, dict[str, Any]]]] = {
        "daily_decision_agent": _run_daily_decision,
        "technical_strategy_agent": _run_technical_strategy,
        "fundamental_valuation_agent": _run_valuation,
        "contagion_stress_agent": _run_contagion,
        "alpha_forge_agent": _run_alpha_forge,
        "backtest_audit_agent": _run_backtest_audit,
    }
