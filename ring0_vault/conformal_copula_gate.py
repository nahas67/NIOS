"""NIOS Core v3.0 -- Ring 0 precision gate and cross-silo tail-copula shield.

Governing architecture sections
-------------------------------
* ``sec_002`` flaw 4 -- conformal cold-start overconfidence: a strategy with
  fewer than 30 graded shadow fills must never be trusted.
* ``sec_003`` Pillar 2 -- capital deploys only when
  ``Net_LB = E[r] - (Spread + 2*Fee + Tax + Q_0.98(Residuals)) > 0``.
* ``sec_009`` -- the 200-trade rolling residual window and the Darwinian trust
  multiplier (0.8x .. 1.5x).
* ``sec_010`` / ``sec_013`` -- ``0.42 ms p99`` for the whole Ring 0 gate.
* ``sec_011`` -- cross-silo correlated heat above 25% of NAV blocks new risk.
* ``sec_016`` -- the plain-English proof line rendered to the cockpit.
* ``sec_018`` Law 4 -- the default answer is no trade; ~99.1% abstention.

Why this module is dependency-thin
----------------------------------
Ring 0 sits in the microsecond path.  Importing DuckDB, a NATS client or an LLM
SDK here would blow the latency budget and widen the air-gap.  The module
therefore imports only :mod:`core.schemas` plus the standard library, and keeps
an in-memory *cached mirror* of the Tier-2 conformal window that is refreshed
off-path from :class:`core.feature_store.FeatureStore`.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from core.schemas import (
    PlainEnglishCockpitCard,
    UnsignedProposal,
    net_lower_bound_bps,
)

__all__ = [
    # constants
    "GATE_LATENCY_BUDGET_NS",
    "GATE_LATENCY_P50_BUDGET_NS",
    "CONFORMAL_WINDOW_SIZE",
    "CONFORMAL_MIN_SHADOW_FILLS",
    "TRUST_MULTIPLIER_MIN",
    "TRUST_MULTIPLIER_MAX",
    "TAIL_CORR_SHRINK_THRESHOLD",
    "MAX_CORRELATED_HEAT_PCT",
    "SILO_MARKETS",
    "MARKET_BY_SILO",
    # enums
    "SiloMarket",
    "GateOutcome",
    # gate
    "ConformalMirror",
    "GateDecision",
    "SplitConformal98Gate",
    # copula
    "SiloExposure",
    "ShieldDecision",
    "CrossSiloCopulaShield",
    # helpers
    "silo_market",
    "sec016_proof_line",
]

# ---------------------------------------------------------------------------
# Architectural constants (not runtime-tunable)
# ---------------------------------------------------------------------------

#: ``sec_010`` / ``sec_013``: full Ring 0 conformal + firewall + sign budget.
GATE_LATENCY_BUDGET_NS: int = 420_000  # 0.420 ms p99
GATE_LATENCY_P50_BUDGET_NS: int = 290_000  # 0.290 ms p50

#: ``sec_009``: 200-trade rolling conformal residual window.
CONFORMAL_WINDOW_SIZE: int = 200

#: ``sec_018`` Law 4: 30 live L2 shadow fills before live graduation.
CONFORMAL_MIN_SHADOW_FILLS: int = 30

#: ``sec_009``: Darwinian trust multiplier band.
TRUST_MULTIPLIER_MIN: float = 0.8
TRUST_MULTIPLIER_MAX: float = 1.5

#: ``sec_003`` Pillar 2: tail correlation above this shrinks sizing.
TAIL_CORR_SHRINK_THRESHOLD: float = 0.60

#: ``sec_011``: correlated heat above this fraction of NAV blocks new risk.
MAX_CORRELATED_HEAT_PCT: float = 25.0

#: Sizing haircut applied at *full* correlation (corr == 1.0).
_MAX_HAIRCUT_AT_UNITY: float = 0.60

_HEX_DIGITS = frozenset("0123456789abcdef")


# ---------------------------------------------------------------------------
# Market taxonomy
# ---------------------------------------------------------------------------


class SiloMarket(StrEnum):
    """The four sovereign market silos (``sec_015``)."""

    CRYPTO = "CRYPTO"
    INDIA = "INDIA"
    US = "US"
    FX = "FX"


#: Canonical iteration order -- also the tail-correlation matrix order.
SILO_MARKETS: tuple[SiloMarket, ...] = (
    SiloMarket.CRYPTO,
    SiloMarket.INDIA,
    SiloMarket.US,
    SiloMarket.FX,
)

#: Ring 1 silo id -> Ring 0 market bucket.
MARKET_BY_SILO: Mapping[str, SiloMarket] = {
    "CRYPTO_BINANCE": SiloMarket.CRYPTO,
    "INDIA_ZERODHA_DHAN": SiloMarket.INDIA,
    "US_IBKR": SiloMarket.US,
    "FX_MT5": SiloMarket.FX,
}


def silo_market(silo: str) -> SiloMarket:
    """Resolve a silo id (or bare market token) to a :class:`SiloMarket`."""
    token = str(silo).strip().upper().replace("-", "_").replace(" ", "_")
    if token in MARKET_BY_SILO:
        return MARKET_BY_SILO[token]
    try:
        return SiloMarket(token)
    except ValueError as exc:
        raise KeyError(
            f"unknown silo {silo!r}; expected one of {sorted(MARKET_BY_SILO)} "
            f"or one of {[m.value for m in SILO_MARKETS]}"
        ) from exc


# ---------------------------------------------------------------------------
# Conformal mirror
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _StrategyWindow:
    """One strategy's cached conformal slice plus its trust multiplier."""

    residuals: deque[float] = field(
        default_factory=lambda: deque(maxlen=CONFORMAL_WINDOW_SIZE)
    )
    shadow_fills: int = 0
    trust_multiplier: float = 1.0
    refreshed_at_ns: int = 0

    @property
    def count(self) -> int:
        return len(self.residuals)

    @property
    def is_warm(self) -> bool:
        """``sec_018`` Law 4: 30 graded shadow fills before live signing."""
        return self.shadow_fills >= CONFORMAL_MIN_SHADOW_FILLS


class ConformalMirror:
    """In-memory cache of the Tier-2 rolling conformal residual window.

    The Tier-2 :class:`core.feature_store.FeatureStore` owns the durable
    bitemporal truth.  Ring 0 cannot afford a DuckDB round-trip inside the
    ``0.42 ms`` budget, so this mirror is refreshed off-path (every settlement
    batch and on strategy promotion) and read with a plain dict lookup on the
    hot path.

    ``Q_0.98`` is only defined once the window holds at least
    :data:`CONFORMAL_MIN_SHADOW_FILLS` graded residuals; before that the mirror
    reports ``+inf`` so the gate abstains rather than trusting an uncalibrated
    tail (``sec_002`` fatal flaw 4).

    Trust scaling
    -------------
    The Darwinian trust multiplier expresses *earned* confidence
    (``sec_009``): ``1.5x`` is a proven champion, ``0.8x`` is on probation.
    The residual buffer is therefore divided by the multiplier -- a trusted
    agent needs a smaller worst-case cushion, a demoted agent needs a larger
    one.  The multiplier can never drive the buffer to zero because it is
    clamped inside ``[0.8, 1.5]``.
    """

    __slots__ = ("_windows", "_default_trust")

    def __init__(self, *, default_trust: float = 1.0) -> None:
        self._windows: dict[str, _StrategyWindow] = {}
        self._default_trust = _clamp_trust(default_trust)

    # -- lifecycle --------------------------------------------------------
    def window(self, strategy_id: str) -> _StrategyWindow:
        """Return (creating if needed) the cache slot for ``strategy_id``."""
        key = str(strategy_id)
        window = self._windows.get(key)
        if window is None:
            window = _StrategyWindow(trust_multiplier=self._default_trust)
            self._windows[key] = window
        return window

    def __contains__(self, strategy_id: object) -> bool:
        return str(strategy_id) in self._windows

    def __len__(self) -> int:
        return len(self._windows)

    def strategy_ids(self) -> tuple[str, ...]:
        return tuple(self._windows)

    # -- population -------------------------------------------------------
    def record(
        self,
        strategy_id: str,
        residual_bps: float,
        *,
        is_shadow: bool = True,
        now_ns: int | None = None,
    ) -> _StrategyWindow:
        """Append one graded residual (signed error, in bps)."""
        value = float(residual_bps)
        if not math.isfinite(value):
            raise ValueError("residual_bps must be finite")
        window = self.window(strategy_id)
        window.residuals.append(value)
        if is_shadow:
            window.shadow_fills += 1
        window.refreshed_at_ns = int(now_ns if now_ns is not None else time.time_ns())
        return window

    def seed(
        self,
        strategy_id: str,
        residuals: Iterable[float],
        *,
        shadow_fills: int | None = None,
        trust_multiplier: float | None = None,
    ) -> _StrategyWindow:
        """Replace a strategy's cached window wholesale (off-path refresh)."""
        window = self.window(strategy_id)
        window.residuals.clear()
        for value in residuals:
            window.residuals.append(float(value))
        window.shadow_fills = (
            int(shadow_fills) if shadow_fills is not None else len(window.residuals)
        )
        if trust_multiplier is not None:
            window.trust_multiplier = _clamp_trust(trust_multiplier)
        window.refreshed_at_ns = time.time_ns()
        return window

    def sync_from_feature_store(
        self,
        store: Any,
        strategy_id: str,
        as_of_ns: int,
        *,
        trust_multiplier: float | None = None,
    ) -> _StrategyWindow:
        """Refresh one strategy from the Tier-2 store (off the hot path).

        ``store`` is duck-typed against :class:`core.feature_store.FeatureStore`
        so this module never imports DuckDB.
        """
        rows = store.get_residuals(strategy_id, as_of_ns, window=CONFORMAL_WINDOW_SIZE)
        residuals = [float(row["residual_bps"]) for row in rows]
        shadow = sum(1 for row in rows if bool(row["is_shadow"]))
        return self.seed(
            strategy_id,
            residuals,
            shadow_fills=shadow,
            trust_multiplier=trust_multiplier,
        )

    # -- trust ------------------------------------------------------------
    def set_trust(self, strategy_id: str, multiplier: float) -> float:
        """Set the Darwinian trust multiplier (clamped to ``[0.8, 1.5]``)."""
        value = _clamp_trust(multiplier)
        self.window(strategy_id).trust_multiplier = value
        return value

    def trust(self, strategy_id: str) -> float:
        return self.window(strategy_id).trust_multiplier

    # -- reads ------------------------------------------------------------
    def q98_bps(self, strategy_id: str) -> float:
        """Empirical ``Q_0.98`` or ``+inf`` while the window is cold."""
        window = self._windows.get(str(strategy_id))
        if window is None or not window.is_warm:
            return math.inf
        values = window.residuals
        if not values:
            return math.inf
        return _quantile(sorted(values), 0.98)

    def effective_q98_bps(self, strategy_id: str) -> float:
        """``Q_0.98`` scaled by the trust multiplier (``+inf`` stays ``+inf``)."""
        q98 = self.q98_bps(strategy_id)
        if not math.isfinite(q98):
            return math.inf
        return q98 / self.trust(strategy_id)

    def snapshot(self, strategy_id: str) -> dict[str, Any]:
        window = self.window(strategy_id)
        return {
            "strategy_id": str(strategy_id),
            "count": window.count,
            "shadow_fills": window.shadow_fills,
            "is_warm": window.is_warm,
            "trust_multiplier": window.trust_multiplier,
            "q98_bps": self.q98_bps(strategy_id),
            "effective_q98_bps": self.effective_q98_bps(strategy_id),
            "refreshed_at_ns": window.refreshed_at_ns,
        }


def _clamp_trust(value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("trust multiplier must be finite")
    return min(TRUST_MULTIPLIER_MAX, max(TRUST_MULTIPLIER_MIN, number))


def _quantile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolated empirical quantile over an already-sorted sequence."""
    n = len(ordered)
    if n == 0:
        return math.inf
    if n == 1:
        return float(ordered[0])
    position = float(q) * (n - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, n - 1)
    fraction = position - lower
    return float(ordered[lower]) + (float(ordered[upper]) - float(ordered[lower])) * fraction


# ---------------------------------------------------------------------------
# Gate outcomes
# ---------------------------------------------------------------------------


class GateOutcome(StrEnum):
    """Deterministic result of one 98% split-conformal evaluation."""

    SIGNED = "SIGNED"
    SHADOW_WARMUP_REQUIRED = "SHADOW_WARMUP_REQUIRED"
    CONFORMAL_NET_LB_NON_POSITIVE = "CONFORMAL_NET_LB_NON_POSITIVE"

    @property
    def is_signed(self) -> bool:
        return self is GateOutcome.SIGNED

    @property
    def is_abstain(self) -> bool:
        return self is not GateOutcome.SIGNED


@dataclass(slots=True, frozen=True)
class GateDecision:
    """Immutable record of one conformal evaluation.

    Carries every input to the proof line so the cockpit card can be rendered
    byte-for-byte deterministically (``sec_016``) and so the decision can be
    replayed during an audit.
    """

    allowed: bool
    outcome: GateOutcome
    reason: str
    silo: str
    symbol: str
    strategy_id: str
    gross_bps: float
    spread_bps: float
    fee_bps: float
    tax_bps: float
    q98_bps: float
    trust_multiplier: float
    net_lb_bps: float
    shadow_fills: int
    calibrated: bool
    latency_ns: int
    card: PlainEnglishCockpitCard | None = None

    # -- derived ----------------------------------------------------------
    @property
    def fee_stt_bps(self) -> float:
        """The ``Fee+STT`` figure: ``2 * Fee + Tax`` (``sec_016``)."""
        return 2.0 * self.fee_bps + self.tax_bps

    @property
    def effective_q98_bps(self) -> float:
        return self.q98_bps / self.trust_multiplier

    @property
    def within_latency_budget(self) -> bool:
        return self.latency_ns <= GATE_LATENCY_BUDGET_NS

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "outcome": str(self.outcome),
            "reason": self.reason,
            "silo": self.silo,
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "gross_bps": self.gross_bps,
            "spread_bps": self.spread_bps,
            "fee_bps": self.fee_bps,
            "tax_bps": self.tax_bps,
            "fee_stt_bps": self.fee_stt_bps,
            "q98_bps": self.q98_bps,
            "effective_q98_bps": self.effective_q98_bps,
            "trust_multiplier": self.trust_multiplier,
            "net_lb_bps": self.net_lb_bps,
            "shadow_fills": self.shadow_fills,
            "calibrated": self.calibrated,
            "latency_ns": self.latency_ns,
            "proof_line": self.card.format_proof_line() if self.card else "",
        }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class SplitConformal98Gate:
    """The 98% split-conformal precision gate (``sec_003`` Pillar 2).

    Evaluates, in a single dict lookup plus four arithmetic ops::

        Net_LB = E[r] - (Spread + 2*Fee + Tax + Q_0.98(Residuals))

    where ``2*Fee + Tax`` is the silo's preloaded statutory round-trip floor
    (e.g. India's ``5.5 bps`` STT + stamp duty + brokerage floor, ``sec_014`` /
    ``sec_015``) and ``Q_0.98`` comes from the strategy's mirrored 200-trade
    rolling residual window, divided by its Darwinian trust multiplier.

    The gate returns ``SIGNED`` only for ``Net_LB > 0``; every other path is an
    ``ABSTAIN`` that emits an operator-readable cockpit card.  There is no
    "almost passed" state (``sec_018`` Law 4).
    """

    __slots__ = ("mirror", "latency_budget_ns", "_last_latency_ns")

    def __init__(
        self,
        mirror: ConformalMirror | None = None,
        *,
        latency_budget_ns: int = GATE_LATENCY_BUDGET_NS,
    ) -> None:
        self.mirror = mirror if mirror is not None else ConformalMirror()
        self.latency_budget_ns = int(latency_budget_ns)
        self._last_latency_ns = 0

    # -- evaluation -------------------------------------------------------
    def evaluate(
        self,
        proposal: UnsignedProposal,
        *,
        cost_floor: Any = None,
        now_ns: int | None = None,
        mode_badge: str = "LIVE CAPITAL",
    ) -> GateDecision:
        """Evaluate one ``UnsignedProposal`` against the conformal bound."""
        started = time.perf_counter_ns()

        silo = str(proposal.silo)
        symbol = str(proposal.symbol)
        strategy_id = str(proposal.strategy_id)

        spread_bps, fee_bps, tax_bps = self._friction(cost_floor, proposal)

        window = self.mirror.window(strategy_id)
        trust = window.trust_multiplier
        calibrated = window.is_warm
        shadow_fills = window.shadow_fills

        if not calibrated:
            q98 = math.inf
            decision = self._abstain(
                GateOutcome.SHADOW_WARMUP_REQUIRED,
                proposal,
                spread_bps=spread_bps,
                fee_bps=fee_bps,
                tax_bps=tax_bps,
                q98_bps=q98,
                trust=trust,
                net_lb_bps=-math.inf,
                shadow_fills=shadow_fills,
                calibrated=False,
                latency_ns=time.perf_counter_ns() - started,
                started_ns=started,
                now_ns=now_ns,
                mode_badge=mode_badge,
                story=(
                    f"{_actor(mode_badge)} stood down on {symbol}: only "
                    f"{shadow_fills} of the {CONFORMAL_MIN_SHADOW_FILLS} live "
                    f"shadow fills required for graduation are graded, so the "
                    f"98% residual band is not calibrated."
                ),
            )
            self._last_latency_ns = decision.latency_ns
            return decision

        q98 = self.mirror.q98_bps(strategy_id)
        effective_q98 = q98 / trust
        net_lb = net_lower_bound_bps(
            proposal.expected_gross_bps,
            spread_bps,
            fee_bps,
            tax_bps,
            effective_q98,
        )

        if net_lb <= 0.0:
            decision = self._abstain(
                GateOutcome.CONFORMAL_NET_LB_NON_POSITIVE,
                proposal,
                spread_bps=spread_bps,
                fee_bps=fee_bps,
                tax_bps=tax_bps,
                q98_bps=q98,
                trust=trust,
                net_lb_bps=net_lb,
                shadow_fills=shadow_fills,
                calibrated=True,
                latency_ns=time.perf_counter_ns() - started,
                started_ns=started,
                now_ns=now_ns,
                mode_badge=mode_badge,
                story=(
                    f"{_actor(mode_badge)} stood down on {symbol}: worst-case "
                    f"net edge was {net_lb:+.1f} bps after spread, fees, "
                    f"statutory tax and the 98% residual quantile."
                ),
            )
            self._last_latency_ns = decision.latency_ns
            return decision

        latency = time.perf_counter_ns() - started
        card = self._card(
            proposal,
            mode_badge=mode_badge,
            story=(
                f"{_actor(mode_badge)} passed the 98% conformal gate on {symbol}: "
                f"the worst-case net edge is {net_lb:+.1f} bps."
            ),
            gross_bps=proposal.expected_gross_bps,
            fee_stt_bps=2.0 * fee_bps + tax_bps,
            uncert_q98_bps=effective_q98,
            net_lb_bps=net_lb,
            timestamp_ns=now_ns if now_ns is not None else time.time_ns(),
        )
        decision = GateDecision(
            allowed=True,
            outcome=GateOutcome.SIGNED,
            reason=(
                f"Net_LB {net_lb:+.1f} bps > 0.0 after "
                f"{2.0 * fee_bps + tax_bps:.1f} bps fee+tax and "
                f"{effective_q98:.1f} bps residual buffer"
            ),
            silo=silo,
            symbol=symbol,
            strategy_id=strategy_id,
            gross_bps=float(proposal.expected_gross_bps),
            spread_bps=spread_bps,
            fee_bps=fee_bps,
            tax_bps=tax_bps,
            q98_bps=q98,
            trust_multiplier=trust,
            net_lb_bps=net_lb,
            shadow_fills=shadow_fills,
            calibrated=True,
            latency_ns=latency,
            card=card,
        )
        self._last_latency_ns = latency
        return decision

    # -- direct arithmetic (used by tests, the firewall and the cockpit) ---
    @staticmethod
    def net_lb_bps(
        *,
        expected_gross_bps: float,
        spread_bps: float,
        fee_bps: float,
        tax_bps: float,
        q98_bps: float,
        trust_multiplier: float = 1.0,
    ) -> float:
        """Closed-form ``Net_LB`` without touching any cache."""
        effective = float(q98_bps)
        if math.isfinite(effective):
            effective /= _clamp_trust(trust_multiplier)
        return net_lower_bound_bps(
            expected_gross_bps, spread_bps, fee_bps, tax_bps, effective
        )

    # -- internals --------------------------------------------------------
    @staticmethod
    def _friction(cost_floor: Any, proposal: UnsignedProposal) -> tuple[float, float, float]:
        """Resolve ``(spread, fee, tax)`` from the silo's statutory floor."""
        if cost_floor is not None:
            return (
                float(getattr(cost_floor, "spread_bps", proposal.spread_bps)),
                float(getattr(cost_floor, "maker_fee_bps")),
                float(getattr(cost_floor, "statutory_tax_bps")),
            )
        from ring1_spinal.silo_worker import silo_cost_floor  # local, off hot path

        floor = silo_cost_floor(str(proposal.silo))
        return (
            float(floor.spread_bps),
            float(floor.maker_fee_bps),
            float(floor.statutory_tax_bps),
        )

    @staticmethod
    def _card(
        proposal: UnsignedProposal,
        *,
        mode_badge: str,
        story: str,
        gross_bps: float,
        fee_stt_bps: float,
        uncert_q98_bps: float,
        net_lb_bps: float,
        timestamp_ns: int,
    ) -> PlainEnglishCockpitCard:
        return PlainEnglishCockpitCard(
            silo=str(proposal.silo),
            symbol=str(proposal.symbol),
            mode_badge=str(mode_badge),
            story_sentence=str(story),
            gross_bps=float(gross_bps),
            fee_stt_bps=float(fee_stt_bps),
            uncert_q98_bps=float(uncert_q98_bps),
            net_lb_bps=float(net_lb_bps),
            timestamp_ns=int(timestamp_ns),
        )

    def _abstain(
        self,
        outcome: GateOutcome,
        proposal: UnsignedProposal,
        *,
        spread_bps: float,
        fee_bps: float,
        tax_bps: float,
        q98_bps: float,
        trust: float,
        net_lb_bps: float,
        shadow_fills: int,
        calibrated: bool,
        latency_ns: int,
        started_ns: int,
        now_ns: int | None,
        mode_badge: str,
        story: str,
    ) -> GateDecision:
        uncert = math.inf if not math.isfinite(q98_bps) else q98_bps / trust
        card = self._card(
            proposal,
            mode_badge="SHADOW MODE" if not calibrated else mode_badge,
            story=story,
            gross_bps=proposal.expected_gross_bps,
            fee_stt_bps=2.0 * fee_bps + tax_bps,
            # ``inf`` cannot be rendered by the ``:.1f`` proof template, so the
            # card carries the sentinel ``0.0`` and the abstain reason carries
            # the truth.  The decision record keeps the real ``+inf``.
            uncert_q98_bps=0.0 if not math.isfinite(uncert) else uncert,
            net_lb_bps=0.0 if not math.isfinite(net_lb_bps) else net_lb_bps,
            timestamp_ns=now_ns if now_ns is not None else started_ns,
        )
        return GateDecision(
            allowed=False,
            outcome=outcome,
            reason=(
                f"{outcome}: {story}"
                if outcome is GateOutcome.SHADOW_WARMUP_REQUIRED
                else f"{outcome}: Net_LB {net_lb_bps:+.1f} bps <= 0.0"
            ),
            silo=str(proposal.silo),
            symbol=str(proposal.symbol),
            strategy_id=str(proposal.strategy_id),
            gross_bps=float(proposal.expected_gross_bps),
            spread_bps=spread_bps,
            fee_bps=fee_bps,
            tax_bps=tax_bps,
            q98_bps=q98_bps,
            trust_multiplier=trust,
            net_lb_bps=net_lb_bps,
            shadow_fills=shadow_fills,
            calibrated=calibrated,
            latency_ns=latency_ns,
            card=card,
        )


def _actor(mode_badge: str) -> str:
    return "Spinal Reflex" if str(mode_badge).startswith("LIVE") else "Shadow twin"


# ---------------------------------------------------------------------------
# Cross-silo tail-copula shield
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SiloExposure:
    """One silo's live exposure, in 128-bit integer micro-units."""

    market: SiloMarket
    notional_micros: int = 0
    nav_micros: int = 0
    open_positions: int = 0

    def __post_init__(self) -> None:
        self.market = (
            self.market
            if isinstance(self.market, SiloMarket)
            else SiloMarket(str(self.market).upper())
        )
        self.notional_micros = max(0, int(self.notional_micros))
        self.nav_micros = max(0, int(self.nav_micros))
        self.open_positions = max(0, int(self.open_positions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": str(self.market),
            "notional_micros": self.notional_micros,
            "nav_micros": self.nav_micros,
            "open_positions": self.open_positions,
        }


@dataclass(slots=True, frozen=True)
class ShieldDecision:
    """Result of one cross-silo copula evaluation."""

    allowed: bool
    market: SiloMarket
    requested_notional_micros: int
    adjusted_notional_micros: int
    haircut: float
    max_tail_correlation: float
    correlated_heat_pct: float
    threshold_corr: float
    threshold_heat_pct: float
    reason: str

    @property
    def sized_down(self) -> bool:
        return self.allowed and self.haircut < 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "market": str(self.market),
            "requested_notional_micros": self.requested_notional_micros,
            "adjusted_notional_micros": self.adjusted_notional_micros,
            "haircut": self.haircut,
            "max_tail_correlation": self.max_tail_correlation,
            "correlated_heat_pct": self.correlated_heat_pct,
            "threshold_corr": self.threshold_corr,
            "threshold_heat_pct": self.threshold_heat_pct,
            "reason": self.reason,
        }


class CrossSiloCopulaShield:
    """Tail-copula sizing haircut and correlated-heat circuit breaker.

    ``sec_003`` Pillar 2 says the shield "dynamically shrinks position sizing
    whenever tail correlation across silos exceeds 0.60"; ``sec_011`` says the
    firewall blocks new risk outright once *correlated heat* exceeds 25% of
    total NAV.

    Tail correlation here is the empirical **lower-tail dependence
    coefficient**, not Pearson correlation::

        lambda(a, b) = P(a <= Q_q(a) | b <= Q_q(b))

    which is the quantity that actually matters when four silos are being
    liquidated by the same macro shock.  A measured matrix supplied by the Ring 2
    MiroFish cascade takes precedence over the empirical estimator, because that
    cascade is the subsystem that models second-order forced liquidations
    (``sec_008``).
    """

    __slots__ = (
        "_returns",
        "_measured",
        "_exposures",
        "_nav_micros",
        "_quantile",
        "_min_samples",
    )

    def __init__(
        self,
        *,
        quantile: float = 0.10,
        min_samples: int = 30,
        nav_micros: int = 0,
    ) -> None:
        if not 0.0 < float(quantile) < 0.5:
            raise ValueError("quantile must be inside (0, 0.5)")
        self._returns: dict[SiloMarket, deque[float]] = {
            market: deque(maxlen=CONFORMAL_WINDOW_SIZE) for market in SILO_MARKETS
        }
        self._measured: dict[tuple[SiloMarket, SiloMarket], float] = {}
        self._exposures: dict[SiloMarket, SiloExposure] = {
            market: SiloExposure(market=market) for market in SILO_MARKETS
        }
        self._nav_micros = max(0, int(nav_micros))
        self._quantile = float(quantile)
        self._min_samples = max(3, int(min_samples))

    # -- observations -----------------------------------------------------
    def record_return(self, market: SiloMarket | str, value: float) -> None:
        """Append one mark-to-market return observation for a silo."""
        resolved = market if isinstance(market, SiloMarket) else SiloMarket(str(market).upper())
        self._returns[resolved].append(float(value))

    def set_tail_correlation(
        self, a: SiloMarket | str, b: SiloMarket | str, value: float
    ) -> float:
        """Install a *measured* tail correlation (Ring 2 cascade output)."""
        left = a if isinstance(a, SiloMarket) else SiloMarket(str(a).upper())
        right = b if isinstance(b, SiloMarket) else SiloMarket(str(b).upper())
        number = float(value)
        if not 0.0 <= number <= 1.0:
            raise ValueError("tail correlation must be within [0, 1]")
        if left == right:
            raise ValueError("self-correlation is pinned at 1.0")
        self._measured[(left, right)] = number
        self._measured[(right, left)] = number
        return number

    def measured_pairs(self) -> dict[tuple[str, str], float]:
        return {
            (str(left), str(right)): value
            for (left, right), value in self._measured.items()
            if str(left) < str(right)
        }

    # -- exposure book ----------------------------------------------------
    def set_exposure(
        self,
        market: SiloMarket | str,
        notional_micros: int,
        *,
        nav_micros: int | None = None,
        open_positions: int | None = None,
    ) -> SiloExposure:
        resolved = market if isinstance(market, SiloMarket) else SiloMarket(str(market).upper())
        exposure = self._exposures[resolved]
        exposure.notional_micros = max(0, int(notional_micros))
        if nav_micros is not None:
            exposure.nav_micros = max(0, int(nav_micros))
            self._nav_micros = max(self._nav_micros, exposure.nav_micros)
        if open_positions is not None:
            exposure.open_positions = max(0, int(open_positions))
        return exposure

    def set_nav(self, total_nav_micros: int) -> None:
        self._nav_micros = max(0, int(total_nav_micros))

    @property
    def nav_micros(self) -> int:
        return self._nav_micros

    def exposure(self, market: SiloMarket | str) -> SiloExposure:
        resolved = market if isinstance(market, SiloMarket) else SiloMarket(str(market).upper())
        return self._exposures[resolved]

    def book(self) -> dict[str, dict[str, Any]]:
        return {str(m): e.to_dict() for m, e in self._exposures.items()}

    # -- correlation ------------------------------------------------------
    def tail_correlation(
        self, a: SiloMarket | str, b: SiloMarket | str
    ) -> float:
        """Lower-tail dependence in ``[0, 1]`` (measured value wins)."""
        left = a if isinstance(a, SiloMarket) else SiloMarket(str(a).upper())
        right = b if isinstance(b, SiloMarket) else SiloMarket(str(b).upper())
        if left == right:
            return 1.0
        measured = self._measured.get((left, right))
        if measured is not None:
            return measured
        return self._empirical_tail_dependence(left, right)

    def _empirical_tail_dependence(self, a: SiloMarket, b: SiloMarket) -> float:
        left = list(self._returns[a])
        right = list(self._returns[b])
        n = min(len(left), len(right))
        if n < self._min_samples:
            return 0.0
        left = left[-n:]
        right = right[-n:]
        qa = _quantile(sorted(left), self._quantile)
        qb = _quantile(sorted(right), self._quantile)
        joint = 0
        conditioning = 0
        for x, y in zip(left, right):
            if y <= qb:
                conditioning += 1
                if x <= qa:
                    joint += 1
        if conditioning == 0:
            return 0.0
        return joint / conditioning

    def correlation_matrix(self) -> dict[tuple[str, str], float]:
        matrix: dict[tuple[str, str], float] = {}
        for i, left in enumerate(SILO_MARKETS):
            for right in SILO_MARKETS[i + 1 :]:
                matrix[(str(left), str(right))] = self.tail_correlation(left, right)
        return matrix

    def max_tail_correlation(
        self, market: SiloMarket | str, *, exclude: Iterable[SiloMarket] = ()
    ) -> float:
        resolved = market if isinstance(market, SiloMarket) else SiloMarket(str(market).upper())
        skip = set(exclude)
        worst = 0.0
        for other in SILO_MARKETS:
            if other is resolved or other in skip:
                continue
            worst = max(worst, self.tail_correlation(resolved, other))
        return worst

    def correlated_heat_pct(
        self, market: SiloMarket | str, *, threshold: float = TAIL_CORR_SHRINK_THRESHOLD
    ) -> float:
        """Correlated cluster heat as a percentage of total NAV."""
        resolved = market if isinstance(market, SiloMarket) else SiloMarket(str(market).upper())
        if self._nav_micros <= 0:
            return 0.0
        cluster = self._exposures[resolved].notional_micros
        for other in SILO_MARKETS:
            if other is resolved:
                continue
            if self.tail_correlation(resolved, other) > threshold:
                cluster += self._exposures[other].notional_micros
        return cluster / self._nav_micros * 100.0

    # -- decision ---------------------------------------------------------
    def evaluate(
        self,
        market: SiloMarket | str,
        requested_notional_micros: int,
        *,
        threshold_corr: float = TAIL_CORR_SHRINK_THRESHOLD,
        threshold_heat_pct: float = MAX_CORRELATED_HEAT_PCT,
    ) -> ShieldDecision:
        """Apply the haircut / block rule for one candidate order."""
        resolved = market if isinstance(market, SiloMarket) else SiloMarket(str(market).upper())
        requested = max(0, int(requested_notional_micros))

        corr = self.max_tail_correlation(resolved)
        heat = self.correlated_heat_pct(resolved, threshold=threshold_corr)

        if heat > threshold_heat_pct:
            return ShieldDecision(
                allowed=False,
                market=resolved,
                requested_notional_micros=requested,
                adjusted_notional_micros=0,
                haircut=0.0,
                max_tail_correlation=corr,
                correlated_heat_pct=heat,
                threshold_corr=threshold_corr,
                threshold_heat_pct=threshold_heat_pct,
                reason=(
                    f"CORRELATED_HEAT_BLOCK: {heat:.2f}% of NAV sits in the "
                    f"correlated cluster, above the {threshold_heat_pct:.0f}% cap"
                ),
            )

        haircut = self.haircut_for(corr, threshold_corr=threshold_corr)
        adjusted = int(requested * haircut)
        if haircut >= 1.0:
            reason = (
                f"PASS: max tail correlation {corr:.2f} <= {threshold_corr:.2f} "
                f"and correlated heat {heat:.2f}% <= {threshold_heat_pct:.0f}%"
            )
        else:
            reason = (
                f"TAIL_CORR_SIZED_DOWN: max tail correlation {corr:.2f} > "
                f"{threshold_corr:.2f}; notional haircut to {haircut:.2f}x "
                f"(correlated heat {heat:.2f}%)"
            )
        return ShieldDecision(
            allowed=True,
            market=resolved,
            requested_notional_micros=requested,
            adjusted_notional_micros=adjusted,
            haircut=haircut,
            max_tail_correlation=corr,
            correlated_heat_pct=heat,
            threshold_corr=threshold_corr,
            threshold_heat_pct=threshold_heat_pct,
            reason=reason,
        )

    @staticmethod
    def haircut_for(
        tail_correlation: float, *, threshold_corr: float = TAIL_CORR_SHRINK_THRESHOLD
    ) -> float:
        """Linear haircut from ``1.0`` at the threshold to ``0.40`` at unity.

        ``corr 0.60 -> 1.00x``, ``corr 0.72 -> 0.82x``, ``corr 1.00 -> 0.40x``.
        The floor is deliberately non-zero: the shield sizes risk down, it does
        not silently replace a risk decision with a zero-size order.
        """
        corr = float(tail_correlation)
        if corr <= threshold_corr:
            return 1.0
        span = max(1e-9, 1.0 - float(threshold_corr))
        excess = min(1.0, (corr - float(threshold_corr)) / span)
        return max(0.0, 1.0 - excess * _MAX_HAIRCUT_AT_UNITY)


# ---------------------------------------------------------------------------
# ``sec_016`` compatibility helper
# ---------------------------------------------------------------------------


def sec016_proof_line(
    *,
    mode_badge: str = "LIVE CAPITAL",
    silo: str = "ZERODHA-INDIA",
    symbol: str = "RELIANCE.NS",
    story: str = (
        "Spinal Reflex bought RELIANCE: Aggressive buyers took 71% of 5s "
        "executed volume with clean book."
    ),
    gross_bps: float = 18.0,
    fee_bps: float = 2.5,
    tax_bps: float = 0.5,
    q98_bps: float = 6.2,
    spread_bps: float = 0.0,
) -> str:
    """Render the documented ``sec_016`` example proof line.

    With ``gross=18.0``, ``2*Fee+Tax=5.5`` and ``Q98=6.2`` this reproduces the
    architecture document's ``Net LB: +6.3 bps`` line exactly.
    """
    net = gross_bps - (spread_bps + 2.0 * fee_bps + tax_bps) - q98_bps
    card = PlainEnglishCockpitCard(
        silo=silo,
        symbol=symbol,
        mode_badge=mode_badge,
        story_sentence=story,
        gross_bps=gross_bps,
        fee_stt_bps=2.0 * fee_bps + tax_bps,
        uncert_q98_bps=q98_bps,
        net_lb_bps=net,
        timestamp_ns=0,
    )
    return card.format_proof_line()
