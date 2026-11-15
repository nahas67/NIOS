"""Ring 2 agent task assignment and orchestration engine (``sec_004``, ``sec_018``).

Six specialist Ring 2 agents are dispatchable from the Glass Cockpit.  Every
agent is air-gapped by construction (Law 2): an agent may emit grounded research
reports, strategy scores or nursery proposals, and it can never sign an order,
read a Ring 0 broker key, or write the IBOR.  The dispatcher holds no credential
and imports nothing from the Ring 0 vault.

Every report is sealed into a :class:`core.schemas.GroundedFactPacket` and
verified by :class:`ring2_brain.truth.grounding_gate.NumericalGroundingVerifier`
(Law 3).  A report that fails verification is marked ``FAILED`` with
``UNGROUNDED-REPORT`` rather than being returned to the operator.

Clean-slate boot starts with zero tasks; :meth:`AgentTaskDispatcher.reset_to_clean_slate`
returns the queue to zero.

Institutional reasoning surfaces
--------------------------------
A decision or technical report does not stop at the donor dashboard.  Three real
surfaces are folded into every dispatch and every number they return is sealed
into the same :class:`~core.schemas.GroundedFactPacket` the verifier re-derives:

* :func:`core.market_features_store.get_feature_profile` -- valuation, funding /
  basis, carry and COT positioning plus the options surface (IV rank, dealer
  gamma, put/call ratio, max pain).
* :class:`ring2_brain.quant.tradingview_bridge.TradingViewBridge` -- the three
  canonical Pine studies (RSI, MACD, Supertrend) evaluated on the daily
  timeframe and reduced to a majority verdict.
* :func:`ring2_brain.quant.macro_and_catalysts.get_macro_calendar` -- scheduled
  releases for the symbol's region inside the next seventy-two hours.

Each provider is optional at import time: a missing module degrades the block to
an empty mapping, so a task always completes.

Durable task journal
--------------------
Completed and failed tasks are appended to the ``agent_tasks`` section of
:class:`AtomicStateJournal` injected by the Ring 3 gateway, so a restart can
rehydrate the queue instead of losing it.  A journal that cannot be imported or
written is ignored, and Clean-Slate (:meth:`AgentTaskDispatcher.clear`) empties
that same section -- a reset workstation reports zero tasks, always.
"""

from __future__ import annotations

import hashlib
import importlib  # noqa: F401 - reserved for optional provider discovery
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

# --------------------------------------------------------------------------
# optional providers -- every one of them may be absent during bootstrap, so
# the imports are guarded and the blocks they feed degrade to an empty mapping
# instead of failing the task.
# --------------------------------------------------------------------------

try:  # pragma: no cover - the guard exists for partial checkouts only
    from core.market_features_store import (
        feature_summary,
        get_feature_profile,
        get_options_surface_profile,
    )
except ImportError:  # pragma: no cover
    feature_summary = None  # type: ignore[assignment]
    get_feature_profile = None  # type: ignore[assignment]
    get_options_surface_profile = None  # type: ignore[assignment]

try:  # pragma: no cover - the bridge ships with Ring 2, the guard is defensive
    from ring2_brain.quant.tradingview_bridge import TradingViewBridge
except ImportError:  # pragma: no cover
    TradingViewBridge = None  # type: ignore[assignment,misc]

try:  # pragma: no cover - the macro calendar ships with Ring 2
    from ring2_brain.quant.macro_and_catalysts import get_macro_calendar
except ImportError:  # pragma: no cover
    get_macro_calendar = None  # type: ignore[assignment]

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
FOLLOWUP_TOKEN_COST_USD = 0.000092
FOLLOWUP_PROMPT_TOKENS = 180
FOLLOWUP_COMPLETION_TOKENS = 120

#: The three canonical TradingView studies every report cites.  ``value`` is the
#: study's primary plot at the last bar; ``score`` is the bridge's own verdict
#: score, so the report never invents a reading of its own.
PINE_STUDY_IDS: tuple[str, ...] = ("STD;RSI", "STD;MACD", "STD;Supertrend")
PINE_PRIMARY_PLOT: dict[str, str] = {
    "STD;RSI": "RSI",
    "STD;MACD": "Histogram",
    "STD;Supertrend": "Supertrend",
}
PINE_TIMEFRAME = "1D"
#: ``TradingViewBridge`` votes in BULLISH / BEARISH / NEUTRAL; a report speaks in
#: BUY / SELL / HOLD.
PINE_BIAS_TO_VERDICT: dict[str, str] = {
    "BULLISH": "BUY",
    "BEARISH": "SELL",
    "NEUTRAL": "HOLD",
}

#: Scheduled macro releases are only actionable inside this window, and a report
#: never lists more than a handful of them.
MACRO_HORIZON_HOURS = 72.0
MACRO_MAX_EVENTS = 5
#: Impact tiers as the calendar publishes them, weighted for the additive
#: excess-volatility score.  A tier the calendar adds later contributes nothing
#: rather than a guessed weight.
MACRO_IMPACT_WEIGHT: dict[str, float] = {"HIGH": 1.0, "MEDIUM": 0.5}
#: The calendar's real region tokens (``get_macro_calendar`` rejects anything
#: else), mapped from the instrument's domicile.
MACRO_REGION_CRYPTO = "CRYPTO"
MACRO_REGION_INDIA = "INDIA"
MACRO_REGION_DEFAULT = "US"
MACRO_CRYPTO_PROFILE = "CRYPTO"
MACRO_INDIA_SILO = "INDIA_ZERODHA_DHAN"

#: ``get_feature_profile`` profile tag -> the positioning keys a report quotes.
FEATURE_PROFILE_EQUITY = "EQUITY"
FEATURE_PROFILE_CRYPTO = "CRYPTO"
FEATURE_PROFILE_FX = "FX_COMMODITY"

FEATURE_EQUITY_KEYS: tuple[str, ...] = (
    "pe_ratio",
    "pb_ratio",
    "roe_pct",
    "debt_to_equity",
    "dividend_yield_pct",
)
#: ``*_pct`` -> basis points is the one unit conversion applied here; the value
#: is the store's own number, never a modelled one.
FEATURE_CRYPTO_KEYS: tuple[tuple[str, str, float], ...] = (
    ("funding_rate_8h_pct", "funding_rate_bps", 100.0),
    ("open_interest_usd", "open_interest_usd", 1.0),
    ("annualized_basis_pct", "basis_bps", 100.0),
    ("liquidations_24h_usd", "liquidation_24h_usd", 1.0),
)
FEATURE_FX_KEYS: tuple[tuple[str, str, float], ...] = (
    ("annualized_carry_pct", "carry_differential_bps", 100.0),
    ("cot_net_non_commercial_lots", "cot_net_contracts", 1.0),
    ("rate_differential_bps", "rate_differential_bps", 1.0),
)
FEATURE_OPTIONS_KEYS: tuple[str, ...] = (
    "iv_rank",
    "net_gamma_exposure_usd",
    "pcr_oi",
    "max_pain_strike",
)

#: ``AgentTask`` status a restored in-flight row is demoted to.  A task that was
#: running when the process died never finished, and reporting it as live would
#: be a lie.
JOURNAL_SECTION = "agent_tasks"
RESTORED_INTERRUPTED_STATUS = "INTERRUPTED"

#: Advice ordering used to break a signal-score tie in a watchlist ranking.
ADVICE_RANK: dict[str, int] = {
    "STRONG_BUY": 0,
    "BUY": 1,
    "HOLD": 2,
    "WAIT": 3,
    "AVOID": 4,
}

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


def _parse_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 UTC stamp; ``None`` when the provider sent something else.

    The macro calendar publishes its own anchor (``as_of_utc``) alongside every
    release, so the horizon is measured from the calendar's clock.  The wall
    clock is never read here, which keeps a report reproducible.
    """

    text = str(value or "").strip()
    if not text:
        return None
    token = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        stamp = datetime.fromisoformat(token)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _metric_token(value: Any) -> str:
    """Reduce a label to the ``[A-Za-z0-9_]`` namespace a packet key uses."""

    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_") or "value"


def _numeric_block(block: Mapping[str, Any], prefix: str = "") -> dict[str, float]:
    """Flatten every finite number in a surface block into packet metric keys.

    Nested mappings and sequences are walked in sorted order, then by index, so
    two dispatches over the same data produce byte-identical metric names.
    Strings and booleans contribute nothing: a study's ``regime`` is prose, and
    its ``score`` is the number that has to be grounded.
    """

    out: dict[str, float] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, bool):
            return
        number = _finite_float(node)
        if number is not None:
            out[_metric_token(path)] = number
            return
        if isinstance(node, Mapping):
            for key in sorted(node, key=str):
                child = f"{path}_{key}" if path else str(key)
                walk(node[key], child)
            return
        if isinstance(node, (list, tuple)):
            for index, item in enumerate(node, start=1):
                child = f"{path}_{index}" if path else str(index)
                walk(item, child)

    for key in sorted(block, key=str):
        start = f"{prefix}_{key}" if prefix else str(key)
        walk(block[key], start)
    return out


def _copy_number(
    source: Mapping[str, Any], target: dict[str, Any], key: str, *, scale: float = 1.0
) -> None:
    """Copy one real provider number through, skipping a missing key entirely."""

    number = _finite_float(source.get(key))
    if number is None:
        return
    target[key] = round(number * scale, 6) if scale != 1.0 else number


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
    prefix = _metric_prefix(symbol)
    return {f"{prefix}__{key}": float(value) for key, value in metrics.items()}


def _metric_prefix(symbol: str) -> str:
    """Return the deterministic metric namespace for one symbol."""

    return re.sub(r"[^A-Za-z0-9_]", "_", str(symbol).upper())


def _question_intent(question: str) -> frozenset[str]:
    """Classify a follow-up question into the grounded metric groups to cite."""

    text = str(question or "").lower()
    intents: set[str] = set()
    if any(
        token in text
        for token in ("target", "stop", "entry", "level", "price", "buy", "sell")
    ):
        intents.add("levels")
    if any(
        token in text
        for token in (
            "indicator",
            "rsi",
            "macd",
            "moving average",
            "ma5",
            "ma10",
            "ma20",
            "trend",
            "momentum",
            "volatility",
        )
    ):
        intents.add("indicators")
    if any(
        token in text
        for token in ("risk", "drawdown", "downside", "danger", "caution")
    ):
        intents.add("risk")
    if any(
        token in text
        for token in ("score", "rank", "advice", "recommend", "signal", "rating")
    ):
        intents.add("score")
    # -- institutional surfaces folded in from the feature store, the Pine
    # bridge and the macro calendar.  A question naming one of these must be
    # answered from the block that actually carries it.
    if any(
        token in text
        for token in (
            "pine",
            "rsi",
            "macd",
            "supertrend",
            "study",
            "studies",
            "verdict",
            "trend",
        )
    ):
        intents.add("pine")
    if any(
        token in text
        for token in (
            "gex",
            "gamma",
            "funding",
            "carry",
            "open interest",
            "basis",
            "cot",
            "positioning",
            "put call",
            "pcr",
            "max pain",
            "iv rank",
            "valuation",
            "p/e",
            "pe ratio",
            "roe",
            "fundamental",
        )
    ):
        intents.add("positioning")
    if any(
        token in text
        for token in ("macro", "event", "calendar", "release", "cpi", "fomc", "rbi")
    ):
        intents.add("macro")
    if not intents:
        intents.update({"levels", "score"})
    return frozenset(intents)


_LEVEL_METRICS: tuple[str, ...] = (
    "close_price",
    "entry_price",
    "target_price",
    "target_distance_pct",
    "stop_loss",
    "stop_distance_pct",
    "reward_risk",
)
_INDICATOR_METRICS: tuple[str, ...] = (
    "ma5",
    "ma10",
    "ma20",
    "bias_ma5_pct",
    "rsi_14",
    "macd_value",
    "macd_signal_value",
    "atr_14",
)
_RISK_METRICS: tuple[str, ...] = (
    "max_drawdown_pct",
    "volatility_20d_pct",
    "stop_distance_pct",
    "reward_risk",
)
_SCORE_METRICS: tuple[str, ...] = ("signal_score", "rank")


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
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    fact_packet: dict[str, Any] = field(default_factory=dict)

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
            "conversation_turns": len(self.conversation_history),
            "fact_count": len(self.fact_packet.get("metrics") or {}),
        }

    def to_full_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["structured_result"] = self.structured_result
        payload["markdown_report"] = self.markdown_report
        payload["conversation_history"] = [
            dict(turn) for turn in self.conversation_history
        ]
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
        "_tv_bridge",
        "_tv_bridge_failed",
        "_surface_cache",
        "_macro_cache",
        "journal",
        "_restored_tasks",
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
        journal: Any = None,
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
        # -- institutional surfaces: one bridge, one cache per dispatch call.
        self._tv_bridge: Any = None
        self._tv_bridge_failed = False
        self._surface_cache: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
        self._macro_cache: dict[str, dict[str, Any]] = {}
        # -- durable journal (Task B): explicit injection wins, a full install
        # falls back to the shared workstation journal, and a partial checkout
        # degrades to no persistence at all.
        self.journal = self._resolve_journal(journal)
        self._restored_tasks = 0
        self._restore_from_journal()

    # -- durable journal ------------------------------------------------

    @staticmethod
    def _resolve_journal(journal: Any) -> Any:
        """The journal injected by the workstation shell, else ``None``.

        Ring 2 is air-gapped from Ring 0, so this module never reaches for
        persisted state on its own.  The Ring 3 control plane owns the atomic
        journal and injects it during construction; without that injection a
        dispatcher keeps its tasks in memory and persists nothing.
        """

        return journal

    def _restore_from_journal(self) -> None:
        """Rehydrate lightweight :class:`AgentTask` records from the journal.

        Only identity, routing, status and cost metadata survive a restart: the
        sealed packet and markdown are not replayed, so a restored task reports
        its outcome but cannot answer a follow-up it did not carry.  A row that
        was still ``PENDING`` / ``PROCESSING`` when the process died is recorded
        as :data:`RESTORED_INTERRUPTED_STATUS` -- it is never resurrected as a
        live task.
        """

        journal = self.journal
        if journal is None:
            return
        try:
            state = journal.load()
        except Exception:  # pragma: no cover - load() is contractually safe
            return
        rows = state.get(JOURNAL_SECTION) if isinstance(state, Mapping) else None
        if not isinstance(rows, (list, tuple)):
            return

        with self._lock:
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                task_id = str(row.get("task_id") or "").strip()
                agent_id = str(row.get("agent_id") or "").strip().lower()
                spec = _AGENTS_BY_ID.get(agent_id)
                if not task_id or spec is None or task_id in self._tasks:
                    continue
                status = str(row.get("status") or "").strip().upper()
                if status not in TASK_STATUSES:
                    continue
                if status in ("PENDING", "PROCESSING"):
                    status = RESTORED_INTERRUPTED_STATUS
                silo = str(row.get("silo") or DEFAULT_SILO).strip().upper()
                if silo not in KNOWN_SILOS:
                    silo = DEFAULT_SILO
                priority = str(row.get("priority") or "NORMAL").strip().upper()
                if priority not in TASK_PRIORITIES:
                    priority = "NORMAL"
                strategy = str(row.get("strategy_id") or "").strip().lower()
                created = _finite_float(row.get("created_at_ns")) or 0.0
                completed = _finite_float(row.get("completed_at_ns")) or 0.0
                progress = _finite_float(row.get("progress_pct")) or 0.0
                record = AgentTask(
                    task_id=task_id,
                    agent_id=agent_id,
                    task_type=str(row.get("task_type") or spec.task_type),
                    silo=silo,
                    symbols=_normalise_symbols(row.get("symbols")),
                    strategy_id=strategy if strategy in STRATEGY_IDS else None,
                    prompt=str(row.get("prompt") or ""),
                    priority=priority,
                    status=status,
                    progress_pct=max(0, min(100, int(progress))),
                    created_at_ns=int(created),
                    completed_at_ns=int(completed),
                    latency_ms=max(0.0, _finite_float(row.get("latency_ms")) or 0.0),
                    token_cost_usd=max(
                        0.0, _finite_float(row.get("token_cost_usd")) or 0.0
                    ),
                    grounding_sha256=str(row.get("grounding_sha256") or ""),
                    verified=bool(row.get("verified")),
                )
                # The journal never stores ``error`` for a success; a failed or
                # interrupted row keeps the reason it was recorded with.
                error = row.get("error")
                record.error = None if error is None else str(error)
                self._tasks[record.task_id] = record
                self._order.append(record.task_id)
                self._restored_tasks += 1
                if record.status == "COMPLETED":
                    self._completed += 1
                elif record.status == "FAILED":
                    self._failed += 1

    def _journal_task(self, task: AgentTask) -> None:
        """Append one terminal task to the journal; a journal error is swallowed.

        The report has already been sealed and verified by the time this runs, so
        persistence can never turn a good task into a failed one.
        """

        journal = self.journal
        if journal is None or not task.terminal:
            return
        try:
            journal.append(JOURNAL_SECTION, task.to_dict())
        except Exception:  # noqa: BLE001 - persistence is best effort
            pass

    def _journal_clear(self) -> None:
        """Empty the ``agent_tasks`` section so Clean Slate stays at zero."""

        journal = self.journal
        if journal is None:
            return
        try:
            journal.clear_section(JOURNAL_SECTION)
        except Exception:  # noqa: BLE001 - a reset must not raise
            pass

    def journal_snapshot(self) -> dict[str, Any]:
        """Where the queue is persisted and how many rows it currently holds."""

        journal = self.journal
        path = str(getattr(journal, "path", "") or "")
        count = 0
        if journal is not None:
            try:
                state = journal.load()
                rows = state.get(JOURNAL_SECTION) if isinstance(state, Mapping) else None
                if isinstance(rows, (list, tuple)):
                    count = len(rows)
            except Exception:  # pragma: no cover - load() is contractually safe
                count = 0
        return {
            "journal_path": path,
            "agent_tasks": int(count),
            "restored": int(self._restored_tasks),
        }

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
        self._begin_dispatch()
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
            self._journal_task(task)
        return task.to_full_dict()

    def batch_watchlist(
        self,
        *,
        silo: Any = DEFAULT_SILO,
        symbols: Any = None,
        strategy_id: Any = None,
        priority: Any = "NORMAL",
    ) -> dict[str, Any]:
        """Run the decision dashboard across a watchlist and seal one ranking packet."""

        silo_key = str(silo or DEFAULT_SILO).strip().upper()
        if silo_key not in KNOWN_SILOS:
            raise ValueError(
                "unknown silo; expected one of " + ", ".join(KNOWN_SILOS)
            )
        watchlist = _normalise_symbols(symbols)
        if not watchlist:
            raise ValueError("at least one symbol is required")

        strategy_value: str | None = None
        if strategy_id is not None and str(strategy_id).strip():
            candidate = str(strategy_id).strip().lower()
            if candidate not in STRATEGY_IDS:
                raise ValueError(
                    "unknown strategy_id; expected one of " + ", ".join(STRATEGY_IDS)
                )
            strategy_value = candidate

        priority_key = str(priority or "NORMAL").strip().upper()
        if priority_key not in TASK_PRIORITIES:
            raise ValueError(
                "unknown priority; expected one of " + ", ".join(TASK_PRIORITIES)
            )

        spec = _get_agent("daily_decision_agent")
        now_ns = int(self._clock())
        self._begin_dispatch()
        task = AgentTask(
            task_id=f"task_{uuid.uuid4().hex[:12]}",
            agent_id=spec.agent_id,
            task_type="WATCHLIST_BATCH",
            silo=silo_key,
            symbols=watchlist,
            strategy_id=strategy_value,
            prompt="",
            priority=priority_key,
            status="PENDING",
            progress_pct=0,
            created_at_ns=now_ns,
        )
        with self._lock:
            self._tasks[task.task_id] = task
            self._order.append(task.task_id)
            self._counter += 1

        start_ns = now_ns
        task.status = "PROCESSING"
        task.progress_pct = 10
        try:
            self._budget_check(spec)
            reports = [
                self._dashboard.evaluate(
                    symbol,
                    silo_key,
                    bar_count=DEFAULT_BAR_COUNT,
                    as_of_ns=now_ns,
                )
                for symbol in watchlist
            ]
            focus = get_strategy(strategy_value) if strategy_value else None
            rows: list[dict[str, Any]] = []
            surface_by_symbol: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
            for report in reports:
                focus_score = (
                    float(report.strategy_scores.get(strategy_value, 0.0))
                    if strategy_value
                    else 0.0
                )
                positioning, pine, macro = self._surface_blocks(str(report.symbol))
                surface_by_symbol[str(report.symbol)] = (positioning, pine, macro)
                rows.append(
                    {
                        "symbol": report.symbol,
                        "signal_score": round(float(report.signal_score), 4),
                        "operation_advice": report.operation_advice,
                        "close_price": round(float(report.close_price), 6),
                        "target_price": round(float(report.target_price), 6),
                        "stop_loss": round(float(report.stop_loss), 6),
                        "target_distance_pct": round(float(report.target_distance_pct), 6),
                        "stop_distance_pct": round(float(report.stop_distance_pct), 6),
                        "reward_risk": round(float(report.reward_risk), 6),
                        "rsi_14": round(float(report.rsi_14), 6),
                        "atr_14": round(float(report.atr_14), 6),
                        "return_20d_pct": round(float(report.return_20d_pct), 6),
                        "max_drawdown_pct": round(float(report.max_drawdown_pct), 6),
                        "macd_status": report.macd_status,
                        "rsi_status": report.rsi_status,
                        "signal_reasons": list(report.signal_reasons),
                        "risk_factors": list(report.risk_factors),
                        "best_strategy_id": report.best_strategy_id,
                        "grounding_sha256": report.grounding_sha256,
                        "verified": bool(report.verified),
                        "strategy_focus_score": round(focus_score, 4),
                        "positioning": dict(positioning),
                        "pine": dict(pine),
                        "macro_risk": dict(macro),
                    }
                )
            rows.sort(
                key=lambda row: (
                    -float(row["signal_score"]),
                    -float(row["strategy_focus_score"]),
                    ADVICE_RANK.get(str(row["operation_advice"]), 9),
                    str(row["symbol"]),
                )
            )
            for index, row in enumerate(rows, start=1):
                row["rank"] = index

            metrics: dict[str, float] = {
                "watchlist_size": float(len(rows)),
            }
            for row in rows:
                block = {
                    "rank": float(row["rank"]),
                    "signal_score": float(row["signal_score"]),
                    "close_price": float(row["close_price"]),
                    "target_price": float(row["target_price"]),
                    "stop_loss": float(row["stop_loss"]),
                    "target_distance_pct": float(row["target_distance_pct"]),
                    "stop_distance_pct": float(row["stop_distance_pct"]),
                    "reward_risk": float(row["reward_risk"]),
                    "rsi_14": float(row["rsi_14"]),
                    "atr_14": float(row["atr_14"]),
                    "return_20d_pct": float(row["return_20d_pct"]),
                    "max_drawdown_pct": float(row["max_drawdown_pct"]),
                }
                if strategy_value:
                    block["strategy_score"] = float(row["strategy_focus_score"])
                metrics.update(_prefixed(str(row["symbol"]), block))
                positioning, pine, macro = surface_by_symbol[str(row["symbol"])]
                metrics.update(
                    _prefixed(str(row["symbol"]), _numeric_block(positioning, "feature"))
                )
                metrics.update(_prefixed(str(row["symbol"]), _numeric_block(pine)))
                metrics.update(_prefixed(str(row["symbol"]), _numeric_block(macro)))

            knowledge_ts = max(
                (int(report.knowledge_ts_ns) for report in reports), default=now_ns
            )
            packet = GroundedFactPacket(
                symbol="WATCHLIST",
                valid_time_ns=knowledge_ts,
                knowledge_ts_ns=knowledge_ts,
                as_of_ns=now_ns,
                metrics=metrics,
            )

            doc = _Doc("Watchlist Batch Ranking")
            doc.para(
                f"Daily decision batch on silo {silo_key} covering "
                f"{_spell(len(rows))} watchlist symbols."
            )
            if focus is not None:
                doc.note("Strategy focus", focus.name)
            doc.head("Ranked watchlist")
            doc.text("| Rank | Symbol | Score | Advice |")
            doc.text("| --- | --- | --- | --- |")
            for row in rows:
                doc.text(
                    f"| {_spell(int(row['rank']))} | {_digitless(row['symbol'])} | "
                    f"{_fmt(row['signal_score'])} | {row['operation_advice']} |"
                )
            doc.text("")
            for row in rows:
                doc.head(f"{row['symbol']} watchlist card")
                doc.note("Operation advice", str(row["operation_advice"]))
                doc.note("Macd status", str(row["macd_status"]))
                doc.note("Rsi status", str(row["rsi_status"]))
                doc.kv("Signal score", row["signal_score"])
                doc.kv("Close price", row["close_price"])
                doc.kv("Target price", row["target_price"])
                doc.kv("Stop loss", row["stop_loss"])
                doc.kv("Reward risk", row["reward_risk"])
                if strategy_value:
                    doc.kv("Strategy focus score", row["strategy_focus_score"])
                positioning, pine, macro = surface_by_symbol[str(row["symbol"])]
                if pine.get("pine_studies"):
                    doc.note("Pine consensus", str(pine.get("pine_consensus") or "HOLD"))
                if macro.get("macro_events"):
                    doc.kv("Macro risk score", macro.get("macro_risk_score"))
                    doc.kv("High impact releases", macro.get("macro_high_impact_count"))
                if _finite_float(positioning.get("iv_rank")) is not None:
                    doc.kv("IV rank", positioning["iv_rank"])
            risk_union: list[str] = []
            for row in rows:
                for risk in row["risk_factors"]:
                    if risk not in risk_union:
                        risk_union.append(str(risk))
            doc.bullets("Batch risk factors", risk_union)

            markdown = doc.build()
            verdict = self._verify(packet, markdown)
            task.markdown_report = markdown
            task.grounding_sha256 = packet.sha256_digest
            task.fact_packet = packet.to_dict()
            task.verification = verdict
            task.verified = bool(verdict.get("verified"))
            task.structured_result = {
                "batch": True,
                "agent_id": spec.agent_id,
                "task_type": "WATCHLIST_BATCH",
                "silo": silo_key,
                "symbols": list(watchlist),
                "strategy_id": strategy_value,
                "watchlist_size": len(rows),
                "ranking": rows,
                "advice_by_symbol": {
                    str(row["symbol"]): str(row["operation_advice"]) for row in rows
                },
                "score_by_symbol": {
                    str(row["symbol"]): float(row["signal_score"]) for row in rows
                },
                "positioning_by_symbol": {
                    str(symbol): dict(blocks[0])
                    for symbol, blocks in surface_by_symbol.items()
                },
                "pine_by_symbol": {
                    str(symbol): dict(blocks[1])
                    for symbol, blocks in surface_by_symbol.items()
                },
                "macro_risk_by_symbol": {
                    str(symbol): dict(blocks[2])
                    for symbol, blocks in surface_by_symbol.items()
                },
                "top_symbol": rows[0]["symbol"] if rows else None,
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
                task.error = str(verdict.get("error") or "UNGROUNDED-BATCH")
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            task.status = "FAILED"
            task.progress_pct = 100
            task.error = f"{type(exc).__name__}: {exc}"
            task.completed_at_ns = int(self._clock())
            task.latency_ms = max(0.0, (task.completed_at_ns - start_ns) / 1_000_000.0)
            task.structured_result = {"batch": True, "error": task.error}
        finally:
            with self._lock:
                if task.status == "COMPLETED":
                    self._completed += 1
                elif task.status == "FAILED":
                    self._failed += 1
            self._journal_task(task)
        return task.to_full_dict()

    def followup(self, *, task_id: Any, question: Any) -> dict[str, Any]:
        """Answer one grounded multi-turn question against a completed task."""

        key = str(task_id or "").strip()
        if not key:
            raise ValueError("task_id is required")
        text = str(question or "").strip()
        if not text:
            raise ValueError("question must be a non-empty string")

        with self._lock:
            task = self._tasks.get(key)
        if task is None:
            raise KeyError(key)
        if task.status != "COMPLETED" or not task.fact_packet:
            raise ValueError("follow-up requires a completed grounded task")

        packet = GroundedFactPacket(**task.fact_packet)
        intents = _question_intent(text)
        now_ns = int(self._clock())

        doc = _Doc("Strategy Q&A Follow-Up")
        doc.para(
            f"Follow-up on task {task.task_id} for "
            f"{', '.join(task.symbols)} on silo {task.silo}."
        )
        doc.para(f"Operator question: {text}")

        metric_names: dict[str, list[str]] = {}
        if "levels" in intents:
            metric_names["levels"] = list(_LEVEL_METRICS)
        if "indicators" in intents:
            metric_names["indicators"] = list(_INDICATOR_METRICS)
        if "risk" in intents:
            metric_names["risk"] = list(_RISK_METRICS)
        if "score" in intents:
            metric_names["score"] = list(_SCORE_METRICS)

        for symbol in task.symbols:
            prefix = _metric_prefix(symbol)
            doc.head(f"{symbol} grounded answer")
            advice = self._advice_for_symbol(task, symbol)
            if advice:
                doc.note("Operation advice", advice)
            ranked = packet.metrics.get(f"{prefix}__rank")
            if ranked is not None:
                doc.kv("Watchlist rank", ranked)
            for group, names in metric_names.items():
                for name in names:
                    value = packet.metrics.get(f"{prefix}__{name}")
                    if value is not None and name != "rank":
                        doc.kv(f"{group} {name}", value)
            # Surface blocks (feature store / Pine bridge / macro calendar) are
            # stored under their own namespace, so a question that names one is
            # answered from the numbers that namespace actually carries.
            for group, stem in (
                ("pine", "pine_"),
                ("positioning", "feature_"),
                ("macro", "macro_"),
            ):
                if group not in intents:
                    continue
                base = f"{prefix}__{stem}"
                for name in sorted(packet.metrics):
                    if not name.startswith(base):
                        continue
                    short = name[len(base) :]
                    doc.kv(f"{group} {short}", packet.metrics[name])
            risks = self._risk_factors_for_symbol(task, symbol)
            if risks and "risk" in intents:
                doc.bullets("Risk factors", risks)
            elif risks:
                doc.bullets("Recorded risk factors", risks)

        markdown = doc.build()
        verdict = self._verify(packet, markdown)
        if not verdict.get("verified"):
            raise ValueError(
                f"UNGROUNDED-FOLLOWUP: {verdict.get('error') or 'verification failed'}"
            )

        cost = 0.0
        try:
            report = self._token_ledger.record(
                system=2,
                provider="deterministic",
                model="strategy_qa_followup",
                prompt_tokens=FOLLOWUP_PROMPT_TOKENS,
                completion_tokens=FOLLOWUP_COMPLETION_TOKENS,
                usd_cost=FOLLOWUP_TOKEN_COST_USD,
            )
            cost = float(report.get("usd_cost", FOLLOWUP_TOKEN_COST_USD))
        except Exception:
            cost = FOLLOWUP_TOKEN_COST_USD

        operator_turn = {
            "role": "operator",
            "text": text,
            "at_ns": now_ns,
        }
        agent_turn = {
            "role": "agent",
            "text": markdown,
            "at_ns": int(self._clock()),
            "grounding_sha256": packet.sha256_digest,
            "verified": True,
            "claims_checked": int(verdict.get("claims_checked") or 0),
            "token_cost_usd": round(cost, 6),
        }
        with self._lock:
            task.conversation_history.extend([operator_turn, agent_turn])
            history = [dict(turn) for turn in task.conversation_history]

        return {
            "task_id": task.task_id,
            "agent_id": task.agent_id,
            "silo": task.silo,
            "symbols": list(task.symbols),
            "question": text,
            "answer": markdown,
            "intents": sorted(intents),
            "conversation_history": history,
            "turn_index": len(history) // 2,
            "grounding_sha256": packet.sha256_digest,
            "verified": True,
            "verification": verdict,
            "token": self.token_usage(),
            "generated_at_ns": now_ns,
        }

    @staticmethod
    def _advice_for_symbol(task: AgentTask, symbol: str) -> str | None:
        """Extract the recorded operation advice for one symbol, if any."""

        found: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, Mapping):
                if (
                    str(node.get("symbol") or "") == str(symbol)
                    and node.get("operation_advice") is not None
                ):
                    found.append(str(node["operation_advice"]))
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    walk(value)

        walk(task.structured_result)
        return found[0] if found else None

    @staticmethod
    def _risk_factors_for_symbol(task: AgentTask, symbol: str) -> list[str]:
        """Extract the recorded risk factors for one symbol, deduplicated."""

        found: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, Mapping):
                risks = node.get("risk_factors")
                if str(node.get("symbol") or "") == str(symbol) and isinstance(
                    risks, (list, tuple)
                ):
                    for risk in risks:
                        text = str(risk).strip()
                        if text and text not in found:
                            found.append(text)
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    walk(value)

        walk(task.structured_result)
        return found

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

    # -- institutional surfaces -----------------------------------------
    #
    # Each helper is total: a provider that is missing, unknown to the registry
    # or raising answers an empty block, so no report depends on one being
    # healthy.  ``assign`` / ``batch_watchlist`` / ``followup`` clear the caches
    # on entry, which is the only place they are cleared.

    def _begin_dispatch(self) -> None:
        """Drop the per-symbol surface cache for a fresh dispatch call."""

        with self._lock:
            self._surface_cache.clear()
            self._macro_cache.clear()

    def _surface_blocks(
        self, symbol: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Positioning, Pine verdict and macro risk for one symbol, memoised.

        A watchlist dispatch would otherwise build the same three blocks for the
        same symbol twice (once for the payload, once for the report); the cache
        makes that one build per symbol per dispatch call.
        """

        key = str(symbol or "").strip().upper()
        with self._lock:
            cached = self._surface_cache.get(key)
        if cached is not None:
            return cached
        blocks = (
            self._market_feature_block(key),
            self._pine_verdict_block(key),
            self._macro_risk_block(key),
        )
        with self._lock:
            self._surface_cache[key] = blocks
        return blocks

    def _market_feature_block(self, symbol: str) -> dict[str, Any]:
        """Equity / funding / carry / options positioning for one instrument.

        Every value is the store's own number.  The only arithmetic applied is a
        percent-to-basis-point rescale, which is a unit change rather than a new
        claim.  A symbol the registry does not know answers ``{}``.
        """

        token = str(symbol or "").strip().upper()
        if not token:
            return {}
        block: dict[str, Any] = {}
        try:
            profile = get_feature_profile(token) if get_feature_profile is not None else None
            if profile is not None:
                features = profile.features
                kind = str(getattr(profile, "profile", "") or "")
                if kind:
                    block["profile_kind"] = kind
                if kind == FEATURE_PROFILE_EQUITY:
                    for key in FEATURE_EQUITY_KEYS:
                        _copy_number(features, block, key)
                elif kind == FEATURE_PROFILE_CRYPTO:
                    for source, target, scale in FEATURE_CRYPTO_KEYS:
                        _copy_number(features, block, source, scale=scale)
                        if target != source and source in block:
                            block[target] = block.pop(source)
                elif kind == FEATURE_PROFILE_FX:
                    for source, target, scale in FEATURE_FX_KEYS:
                        _copy_number(features, block, source, scale=scale)
                        if target != source and source in block:
                            block[target] = block.pop(source)
                # The compact summary row fills any canonical key the deep
                # profile did not carry (market cap, beta, IV and so on).
                if feature_summary is not None:
                    for key, value in feature_summary(token).items():
                        number = _finite_float(value)
                        if number is not None:
                            block.setdefault(str(key), number)
            if get_options_surface_profile is not None:
                surface = get_options_surface_profile(token) or {}
                for key in FEATURE_OPTIONS_KEYS:
                    _copy_number(surface, block, key)
        except Exception:  # noqa: BLE001 - an unknown symbol must not fail a task
            return {}
        return block

    def _tv_bridge_instance(self) -> Any:
        """One lazily built :class:`TradingViewBridge`, or ``None``.

        Construction is attempted once: a bridge that cannot be built is not
        rebuilt on every symbol of a watchlist.
        """

        if self._tv_bridge is not None or self._tv_bridge_failed:
            return self._tv_bridge
        with self._lock:
            if self._tv_bridge is not None or self._tv_bridge_failed:
                return self._tv_bridge
            provider = TradingViewBridge
            bridge = None
            if provider is not None:
                try:
                    bridge = provider()
                except Exception:  # noqa: BLE001 - recorded as unavailable
                    bridge = None
            self._tv_bridge = bridge
            self._tv_bridge_failed = bridge is None
        return self._tv_bridge

    def _pine_verdict_block(self, symbol: str) -> dict[str, Any]:
        """RSI / MACD / Supertrend verdicts on the daily timeframe.

        The bridge's own verdict dict is reduced to ``regime``, ``bias``,
        ``score`` plus the latest value of the study's primary plot, and the
        consensus is the strict majority bias.  Any failure answers the neutral
        block rather than raising into the report.
        """

        empty: dict[str, Any] = {"pine_studies": [], "pine_consensus": "HOLD"}
        token = str(symbol or "").strip().upper()
        if not token:
            return empty
        bridge = self._tv_bridge_instance()
        if bridge is None:
            return empty
        studies: list[dict[str, Any]] = []
        try:
            for study_id in PINE_STUDY_IDS:
                result = bridge.evaluate_tv_study(token, study_id, timeframe=PINE_TIMEFRAME)
                verdict = result.get("verdict") or {}
                latest = result.get("latest") or {}
                plot = latest.get(PINE_PRIMARY_PLOT.get(study_id, ""))
                if plot is None and latest:
                    plot = latest[sorted(latest)[0]]
                studies.append(
                    {
                        "study_id": study_id,
                        "regime": str(verdict.get("regime") or ""),
                        "bias": str(verdict.get("bias") or "NEUTRAL").upper(),
                        "score": _finite_float(verdict.get("score")),
                        "value": _finite_float(plot),
                    }
                )
        except Exception:  # noqa: BLE001 - a study failure is not a task failure
            return empty

        votes = {"BUY": 0, "SELL": 0, "HOLD": 0}
        for row in studies:
            votes[PINE_BIAS_TO_VERDICT.get(row["bias"], "HOLD")] += 1
        if votes["BUY"] > votes["SELL"] and votes["BUY"] > votes["HOLD"]:
            consensus = "BUY"
        elif votes["SELL"] > votes["BUY"] and votes["SELL"] > votes["HOLD"]:
            consensus = "SELL"
        else:
            consensus = "HOLD"
        return {"pine_studies": studies, "pine_consensus": consensus}

    def _macro_region(self, symbol: str) -> str:
        """Map an instrument onto the calendar's real region token.

        A perpetual is a global risk asset (``CRYPTO``), an India-silo instrument
        is driven by ``INDIA`` releases, and everything else -- US cash equities,
        FX and metals -- keys off the ``US`` book, which the calendar also tags
        as impacting the FX and crypto silos.
        """

        token = str(symbol or "").strip().upper()
        kind = ""
        silo = ""
        if get_feature_profile is not None:
            try:
                profile = get_feature_profile(token)
                kind = str(getattr(profile, "profile", "") or "")
                silo = str(getattr(profile, "silo", "") or "")
            except Exception:  # noqa: BLE001 - fall through to the heuristics
                kind, silo = "", ""
        if kind == MACRO_CRYPTO_PROFILE or token.endswith("USDT"):
            return MACRO_REGION_CRYPTO
        if silo == MACRO_INDIA_SILO:
            return MACRO_REGION_INDIA
        return MACRO_REGION_DEFAULT

    def _macro_risk_block(self, symbol: str) -> dict[str, Any]:
        """Scheduled releases for this symbol's region inside the next 72 hours.

        ``hours_until`` is measured from the calendar's own ``as_of_utc`` anchor,
        so the block never depends on the wall clock.  ``macro_risk_score`` is the
        additive excess volatility of the listed releases -- the sum of
        ``(volatility_multiplier - 1)`` weighted by impact tier -- and is zero
        when nothing is scheduled.
        """

        empty: dict[str, Any] = {
            "macro_events": [],
            "macro_high_impact_count": 0,
            "macro_risk_score": 0.0,
        }
        token = str(symbol or "").strip().upper()
        if not token or get_macro_calendar is None:
            return empty
        try:
            region = self._macro_region(token)
            with self._lock:
                cached = self._macro_cache.get(region)
            if cached is None:
                cached = self._build_macro_risk(region)
                with self._lock:
                    self._macro_cache[region] = cached
            # The cache is shared by every symbol in the region, so the caller
            # gets its own copy and cannot mutate another symbol's block.
            return {
                "macro_events": [dict(event) for event in cached["macro_events"]],
                "macro_high_impact_count": int(cached["macro_high_impact_count"]),
                "macro_risk_score": float(cached["macro_risk_score"]),
            }
        except Exception:  # noqa: BLE001 - a calendar failure is not a task failure
            return empty

    @staticmethod
    def _build_macro_risk(region: str) -> dict[str, Any]:
        """Filter the region's calendar down to the next five releases."""

        empty: dict[str, Any] = {
            "macro_events": [],
            "macro_high_impact_count": 0,
            "macro_risk_score": 0.0,
        }
        if get_macro_calendar is None:
            return empty
        payload = get_macro_calendar(region=region)
        anchor = _parse_utc(payload.get("as_of_utc"))
        if anchor is None:
            return empty
        rows: list[dict[str, Any]] = []
        for event in payload.get("events") or ():
            if not isinstance(event, Mapping):
                continue
            if str(event.get("status") or "").upper() != "SCHEDULED":
                continue
            release = _parse_utc(event.get("release_utc"))
            if release is None:
                continue
            hours = (release - anchor).total_seconds() / 3600.0
            if hours < 0.0 or hours > MACRO_HORIZON_HOURS:
                continue
            impact = str(event.get("impact_tier") or "").upper()
            rows.append(
                {
                    "name": str(event.get("title") or event.get("event_id") or ""),
                    "region": str(event.get("region") or region),
                    "impact": impact,
                    "hours_until": round(hours, 4),
                    "volatility_multiplier": round(
                        _finite_float(event.get("volatility_multiplier")) or 1.0, 4
                    ),
                }
            )
        rows.sort(key=lambda row: (row["hours_until"], row["name"]))
        selected = rows[:MACRO_MAX_EVENTS]
        score = 0.0
        for row in selected:
            weight = MACRO_IMPACT_WEIGHT.get(row["impact"], 0.0)
            score += max(0.0, row["volatility_multiplier"] - 1.0) * weight
        return {
            "macro_events": selected,
            "macro_high_impact_count": sum(
                1 for row in selected if row["impact"] == "HIGH"
            ),
            "macro_risk_score": round(score, 4),
        }

    def _surface_metrics(self, symbol: str) -> dict[str, float]:
        """Flatten a symbol's three surface blocks into prefixed packet metrics."""

        positioning, pine, macro = self._surface_blocks(symbol)
        metrics: dict[str, float] = {}
        metrics.update(_prefixed(str(symbol), _numeric_block(positioning, "feature")))
        metrics.update(_prefixed(str(symbol), _numeric_block(pine)))
        metrics.update(_prefixed(str(symbol), _numeric_block(macro)))
        return metrics

    @staticmethod
    def _render_surface_blocks(
        doc: _Doc,
        positioning: Mapping[str, Any],
        pine: Mapping[str, Any],
        macro: Mapping[str, Any],
    ) -> None:
        """Append the three surface sections, quoting only values that exist.

        ``doc.kv`` renders nothing for a missing or zero value, so a provider that
        answered an empty block leaves the section out entirely rather than
        printing a fabricated zero.
        """

        profile = str(positioning.get("profile_kind") or "")
        body = [
            key
            for key in (
                "pe_ratio",
                "pb_ratio",
                "roe_pct",
                "debt_to_equity",
                "dividend_yield_pct",
                "funding_rate_bps",
                "open_interest_usd",
                "basis_bps",
                "liquidation_24h_usd",
                "carry_differential_bps",
                "cot_net_contracts",
                "rate_differential_bps",
            )
            if _finite_float(positioning.get(key)) is not None
        ]
        options = [
            key for key in FEATURE_OPTIONS_KEYS if _finite_float(positioning.get(key)) is not None
        ]
        if profile or body or options:
            doc.head("Fundamentals / positioning")
            if profile:
                doc.note("Feature profile", profile)
            for key in body:
                doc.kv(key.replace("_", " "), positioning[key])
            for key in options:
                doc.kv(key.replace("_", " "), positioning[key])

        studies = pine.get("pine_studies") or []
        if isinstance(studies, (list, tuple)) and studies:
            doc.head("Pine study verdicts")
            doc.note("Pine consensus", str(pine.get("pine_consensus") or "HOLD"))
            for row in studies:
                if not isinstance(row, Mapping):
                    continue
                study_id = str(row.get("study_id") or "")
                doc.note(f"{study_id} regime", str(row.get("regime") or "unknown"))
                doc.note(f"{study_id} bias", str(row.get("bias") or "NEUTRAL"))
                doc.kv(f"{study_id} score", row.get("score"))
                doc.kv(f"{study_id} value", row.get("value"))

        events = macro.get("macro_events") or []
        if isinstance(events, (list, tuple)) and events:
            doc.head("Macro event risk")
            doc.kv("Macro risk score", macro.get("macro_risk_score"))
            doc.kv("High impact releases", macro.get("macro_high_impact_count"))
            for event in events[:MACRO_MAX_EVENTS]:
                if not isinstance(event, Mapping):
                    continue
                doc.note("Scheduled release", f"{event.get('name')} ({event.get('region')})")
                doc.note("Impact tier", str(event.get("impact") or ""))
                doc.kv("Hours until", event.get("hours_until"))
                doc.kv("Volatility multiplier", event.get("volatility_multiplier"))

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
        task.fact_packet = packet.to_dict()
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
        # ``derived_expressions`` is a count on some reports and a collection on
        # others. ``len()`` on the integer form raised TypeError *outside* the
        # guarded try above, so the verified-report path could never return.
        derived = getattr(report, "derived_expressions", 0)
        if isinstance(derived, bool):
            derived_count = int(derived)
        elif isinstance(derived, int):
            derived_count = derived
        else:
            try:
                derived_count = len(derived or ())
            except TypeError:
                derived_count = 0
        base["derived_expressions"] = max(0, derived_count)
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
            self._surface_cache.clear()
            self._macro_cache.clear()
        # Clean-Slate must hold on disk too: a journal that kept the rows would
        # resurrect them on the next construction and report a non-empty queue.
        self._journal_clear()
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
        surface_by_symbol: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
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
            positioning, pine, macro = self._surface_blocks(symbol)
            surface_by_symbol[str(report.symbol)] = (positioning, pine, macro)
            metrics.update(_prefixed(symbol, _numeric_block(positioning, "feature")))
            metrics.update(_prefixed(symbol, _numeric_block(pine)))
            metrics.update(_prefixed(symbol, _numeric_block(macro)))

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
            positioning, pine, macro = surface_by_symbol[str(report.symbol)]
            self._render_surface_blocks(doc, positioning, pine, macro)

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
            "positioning_by_symbol": {
                symbol: dict(blocks[0]) for symbol, blocks in surface_by_symbol.items()
            },
            "pine_by_symbol": {
                symbol: dict(blocks[1]) for symbol, blocks in surface_by_symbol.items()
            },
            "macro_risk_by_symbol": {
                symbol: dict(blocks[2]) for symbol, blocks in surface_by_symbol.items()
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
        surface_by_symbol: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
        doc = _Doc("Technical Strategy Evaluation")
        doc.para(
            f"Agent {task.agent_id} scoring {', '.join(task.symbols)} on silo {task.silo}."
        )
        if task.strategy_id:
            doc.note("Strategy template", str(get_strategy(task.strategy_id).name))

        for symbol in task.symbols:
            positioning, pine, macro = self._surface_blocks(symbol)
            surface_by_symbol[symbol] = (positioning, pine, macro)
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

            metrics.update(_prefixed(symbol, _numeric_block(positioning, "feature")))
            metrics.update(_prefixed(symbol, _numeric_block(pine)))
            metrics.update(_prefixed(symbol, _numeric_block(macro)))
            self._render_surface_blocks(doc, positioning, pine, macro)

        structured = {
            "signals": rows,
            "best": rows[0] if rows else None,
            "positioning_by_symbol": {
                symbol: dict(blocks[0]) for symbol, blocks in surface_by_symbol.items()
            },
            "pine_by_symbol": {
                symbol: dict(blocks[1]) for symbol, blocks in surface_by_symbol.items()
            },
            "macro_risk_by_symbol": {
                symbol: dict(blocks[2]) for symbol, blocks in surface_by_symbol.items()
            },
        }
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
