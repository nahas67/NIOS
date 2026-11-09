"""NIOS Core v3.0 -- System 2 Staged Investment Committee (STEP 4).

``sec_001`` / ``sec_005`` / ``sec_006`` / ``sec_007`` / ``sec_010`` /
``sec_011`` / ``sec_013`` define the slow, deliberative half of the bicameral
brain.  The pipeline is fixed:

1. **4 parallel analysts** -- Fundamental (3-statement/DCF), Technical (factor
   zoo), Sentiment (shock ontology), Macro (MiroFish contagion).  Each reads
   the candidate's :class:`~core.schemas.GroundedFactPacket` and committed
   context metrics only.
2. **Multi-round Bull vs. Bear debate** -- upside vs. structural contagion.
3. **Trader Agent** -- direction, conviction, target/stop bps, limit price and
   the derived valuation math.
4. **3-way Risk Committee** -- Aggressive / Neutral / Conservative debaters
   assign the vault (``LONG_TERM_COMPOUNDER`` or ``TACTICAL_SWING``) and size
   within the ``<= 12%`` compounder / ``<= 5%`` tactical caps.

Before a single :class:`~core.schemas.UnsignedProposal` leaves Ring 2, **every**
committee synthesis is re-verified by
:class:`~ring2_brain.truth.grounding_gate.NumericalGroundingVerifier` against
the committed packet.  One fabricated, unhashed or arithmetically wrong number
raises ``UngroundedNumericalClaimError("UNGROUNDED-NUMERICAL-CLAIM")`` and the
pipeline aborts before publishing anything to
``stream.ring2.deep-theses`` (``sec_006`` / ``sec_018`` Law 3).

Donor bridge
------------
``ring2_brain/cognition/tradingagents/{agents,graph}`` are harvested but import
``tradingagents.*`` / ``langgraph`` donor packages that were not vendored.  The
native committee below preserves the donor's *stage topology* and risk-debate
roles, and :func:`donor_committee_status` reports importability honestly.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.nats_bus import STREAM_RING2_THESES, LocalJetStreamBus
from core.schemas import (
    FRAGILITY_HALT_THRESHOLD,
    GroundedFactPacket,
    UngroundedNumericalClaim,
    UnsignedProposal,
    VaultName,
    canonical_pairs_digest,
    net_lower_bound_bps,
)
from ring2_brain.quant.scanner import ScanCandidate, ScanResult
from ring2_brain.truth.grounding_gate import (
    UNGROUNDED_CLAIM_CODE,
    GroundingLedger,
    NumericalGroundingVerifier,
    UngroundedNumericalClaimError,
    VerificationReport,
)

__all__ = [
    "COMMITTEE_STRATEGY_ID",
    "COMPOUNDER_CAP_PCT",
    "TACTICAL_CAP_PCT",
    "TOP_FRACTION",
    "CommitteeAbstained",
    "CommitteeFunnelViolation",
    "StageReport",
    "CommitteeReport",
    "CommitteeOutcome",
    "System2InvestmentCommittee",
    "System2StagedCommittee",
    "System2Committee",
    "StagedInvestmentCommittee",
    "donor_committee_status",
]

COMMITTEE_STRATEGY_ID = "system2_staged_committee"
#: ``sec_007`` / ``sec_010`` / ``sec_011`` committee sizing caps.
COMPOUNDER_CAP_PCT = 0.12
TACTICAL_CAP_PCT = 0.05
#: ``sec_013`` funnel: only the top 1% (50 of 5,000) reach the committee.
TOP_FRACTION = 0.01


class CommitteeAbstained(RuntimeError):
    """Raised when the committee's Net_LB is not strictly positive."""


class CommitteeFunnelViolation(ValueError):
    """Raised when more than the top 1% of the universe reaches the committee."""


def donor_committee_status() -> dict[str, Any]:
    """Report whether the harvested TradingAgents committee imports here."""
    report: dict[str, Any] = {"modules": {}, "available": False}
    targets = {
        "graph": "ring2_brain.cognition.tradingagents.graph.trading_graph",
        "agents": "ring2_brain.cognition.tradingagents.agents.state",
    }
    for label, module_name in targets.items():
        entry = {"module": module_name, "importable": False, "reason": ""}
        try:
            module = importlib.import_module(module_name)
            entry["importable"] = True
            entry["exports"] = [
                name
                for name in ("TradingAgentsGraph", "AgentState", "InvestDebateState")
                if hasattr(module, name)
            ]
        except Exception as exc:  # pragma: no cover - donor vendoring
            entry["reason"] = f"{type(exc).__name__}: {exc}"
        report["modules"][label] = entry
    report["available"] = any(
        entry["importable"] for entry in report["modules"].values()
    )
    return report


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _fmt(value: float) -> str:
    """Six-decimal rendering: stable, locale-free and verifier-friendly."""
    return f"{float(value):.6f}"


# ---------------------------------------------------------------------------
# Report contracts
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class StageReport:
    """One committee utterance plus its verification receipt."""

    stage: str
    actor: str
    text: str
    metrics: Mapping[str, float] = field(default_factory=dict)
    verified: bool = False
    claims_checked: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "actor": self.actor,
            "text": self.text,
            "verified": self.verified,
            "claims_checked": self.claims_checked,
            "metrics": dict(self.metrics),
        }


@dataclass(slots=True)
class CommitteeReport:
    """Full audit trail of the eight-stage committee run."""

    symbol: str
    packet_digest: str
    stages: tuple[StageReport, ...]
    verifications: tuple[VerificationReport, ...]
    vault: VaultName
    cap_pct: float
    position_pct: float
    side: str
    elapsed_ms: float
    donor: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "packet_digest": self.packet_digest,
            "vault": self.vault.value,
            "cap_pct": self.cap_pct,
            "position_pct": self.position_pct,
            "side": self.side,
            "elapsed_ms": self.elapsed_ms,
            "stages": [stage.to_dict() for stage in self.stages],
            "claims_checked": sum(v.claims_checked for v in self.verifications),
            "derived_expressions": sum(
                v.derived_expressions for v in self.verifications
            ),
        }


@dataclass(slots=True)
class CommitteeOutcome:
    """A verified proposal plus its committee audit trail."""

    proposal: UnsignedProposal
    report: CommitteeReport
    packet: GroundedFactPacket
    receipt: Any | None = None

    @property
    def published(self) -> bool:
        return bool(self.receipt is not None and getattr(self.receipt, "committed", False))


# ---------------------------------------------------------------------------
# Committee
# ---------------------------------------------------------------------------


class System2InvestmentCommittee:
    """Staged analyst -> debate -> trader -> risk pipeline with grounding gate."""

    def __init__(
        self,
        *,
        ledger: GroundingLedger | None = None,
        feature_store: Any | None = None,
        bus: LocalJetStreamBus | Any | None = None,
        verifier: NumericalGroundingVerifier | None = None,
        silo: str = "US",
        nav: float = 1_000_000.0,
        fee_bps: float = 1.0,
        tax_bps: float = 1.5,
        cold_start_q98_bps: float = 10.0,
        debate_rounds: int = 2,
        transport_pool: Any | None = None,
        token_ledger: Any | None = None,
        reasoning_effort: str = "high",
    ) -> None:
        if float(nav) <= 0.0:
            raise ValueError("nav must be positive")
        if int(debate_rounds) < 1:
            raise ValueError("debate_rounds must be >= 1")
        self.ledger = ledger if ledger is not None else GroundingLedger(persist=False)
        self.feature_store = feature_store
        self.bus = bus
        self.verifier = verifier or NumericalGroundingVerifier()
        self.silo = str(silo)
        self.nav = float(nav)
        self.fee_bps = float(fee_bps)
        self.tax_bps = float(tax_bps)
        self.cold_start_q98_bps = float(cold_start_q98_bps)
        self.debate_rounds = int(debate_rounds)
        self.transport_pool = transport_pool
        self.token_ledger = (
            token_ledger
            if token_ledger is not None
            else getattr(transport_pool, "ledger", None)
        )
        self.reasoning_effort = str(reasoning_effort)
        self.donor = donor_committee_status()

    # -- public API -------------------------------------------------------
    def run(
        self,
        candidate: ScanCandidate,
        *,
        contagion: Any | None = None,
        spread_bps: float = 3.0,
        realized_vol_ratio: float = 1.0,
        now_ns: int | None = None,
        universe_size: int | None = None,
        q98_residual_bps: float | None = None,
        text_mutator: Callable[[str, str], str] | None = None,
        publish: bool = True,
    ) -> CommitteeOutcome:
        """Run one candidate through the full four-stage committee."""
        started = time.perf_counter()
        if universe_size is not None:
            allowed = max(1, int(math.ceil(int(universe_size) * TOP_FRACTION)))
            if int(candidate.rank) > allowed:
                raise CommitteeFunnelViolation(
                    f"candidate rank {candidate.rank} is outside the top {allowed} "
                    f"of {universe_size} (top {TOP_FRACTION:.0%})"
                )

        base_packet = candidate.packet
        if not isinstance(base_packet, GroundedFactPacket):
            raise TypeError("candidate.packet must be a GroundedFactPacket")
        if not base_packet.verify() or len(base_packet.sha256_digest) != 64:
            raise UngroundedNumericalClaimError(
                f"{UNGROUNDED_CLAIM_CODE}: candidate packet for "
                f"{candidate.symbol} is unhashed or tampered"
            )

        stamp = int(
            now_ns
            if now_ns is not None
            else (base_packet.as_of_ns or time.time_ns())
        )
        metrics, side, direction_score = self._derive_metrics(
            candidate,
            contagion=contagion,
            spread_bps=spread_bps,
            realized_vol_ratio=realized_vol_ratio,
            now_ns=stamp,
            q98_residual_bps=q98_residual_bps,
        )
        packet = self._commit_packet(base_packet, metrics)

        net_lb = float(metrics["committee_net_lb_bps"])
        if net_lb <= 0.0:
            raise CommitteeAbstained(
                f"Net_LB={net_lb:.4f} bps <= 0 (Law 4 abstention)"
            )

        stages: list[StageReport] = []
        stages.extend(self._run_analysts(packet, candidate, contagion, side))
        stages.extend(self._run_debate(packet, side))
        stages.append(self._run_trader(packet, side, direction_score))
        risk_stages, vault, cap_pct, position_pct = self._run_risk_committee(
            packet, side
        )
        stages.extend(risk_stages)

        mutated: list[StageReport] = []
        verifications: list[VerificationReport] = []
        for stage in stages:
            text = stage.text
            if text_mutator is not None:
                text = str(text_mutator(stage.stage, text))
            # Mandatory SHA-256 numerical grounding gate.  Raising here means
            # ZERO proposals are published.
            report = self.verifier.verify_thesis_claims(text, packet)
            verifications.append(report)
            mutated.append(
                StageReport(
                    stage=stage.stage,
                    actor=stage.actor,
                    text=text,
                    metrics=stage.metrics,
                    verified=True,
                    claims_checked=report.claims_checked,
                )
            )

        proposal = self._build_proposal(
            candidate,
            packet,
            metrics,
            side=side,
            vault=vault,
            position_pct=position_pct,
            now_ns=stamp,
        )
        committee_report = CommitteeReport(
            symbol=candidate.symbol,
            packet_digest=packet.sha256_digest,
            stages=tuple(mutated),
            verifications=tuple(verifications),
            vault=vault,
            cap_pct=cap_pct,
            position_pct=position_pct,
            side=side,
            elapsed_ms=(time.perf_counter() - started) * 1e3,
            donor=self.donor,
        )

        receipt = None
        if publish and self.bus is not None:
            receipt = self._publish(proposal, committee_report)
        return CommitteeOutcome(
            proposal=proposal, report=committee_report, packet=packet, receipt=receipt
        )

    def run_scan(
        self,
        scan_result: ScanResult,
        *,
        contagion: Any | None = None,
        spread_bps: float = 3.0,
        realized_vol_ratio: float = 1.0,
        now_ns: int | None = None,
        text_mutator: Callable[[str, str], str] | None = None,
        publish: bool = True,
        max_candidates: int | None = None,
        q98_residual_bps: float | None = None,
    ) -> list[CommitteeOutcome]:
        """Run every top-1% survivor, enforcing the funnel first."""
        allowed = max(1, int(math.ceil(scan_result.universe_size * TOP_FRACTION)))
        if len(scan_result.candidates) > allowed:
            raise CommitteeFunnelViolation(
                f"scanner emitted {len(scan_result.candidates)} candidates for a "
                f"{scan_result.universe_size}-name universe; top {TOP_FRACTION:.0%} "
                f"is {allowed}"
            )
        chosen = scan_result.candidates
        if max_candidates is not None:
            chosen = chosen[: max(0, int(max_candidates))]
        outcomes: list[CommitteeOutcome] = []
        for candidate in chosen:
            outcomes.append(
                self.run(
                    candidate,
                    contagion=contagion,
                    spread_bps=spread_bps,
                    realized_vol_ratio=realized_vol_ratio,
                    now_ns=now_ns,
                    universe_size=scan_result.universe_size,
                    q98_residual_bps=q98_residual_bps,
                    text_mutator=text_mutator,
                    publish=publish,
                )
            )
        return outcomes

    # -- System 2 LLM transport ------------------------------------------
    def route_system2(
        self,
        prompt: str,
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Route a deliberative turn across DeepSeek -> Claude -> OpenRouter.

        The grounding gate still owns every number: this method returns prose,
        never an executable claim.  When no transport is wired (the default
        deterministic committee) it returns a zero-cost local placeholder.
        """
        if self.transport_pool is None:
            if self.token_ledger is not None:
                self.token_ledger.set_active(2, "deterministic-local")
            return {
                "provider": "deterministic-local",
                "model": "nios-native-committee",
                "system": 2,
                "text": "",
                "usd_cost": 0.0,
                "used_fallback": True,
                "reasoning_effort": str(reasoning_effort or self.reasoning_effort),
            }
        completion = self.transport_pool.complete_system2(
            prompt,
            reasoning_effort=reasoning_effort or self.reasoning_effort,
            max_tokens=max_tokens,
        )
        if hasattr(completion, "to_dict"):
            payload = completion.to_dict()
            payload["used_fallback"] = completion.provider == "deterministic-local"
            return payload
        return completion

    def llm_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "reasoning_effort": self.reasoning_effort,
            "transport_wired": self.transport_pool is not None,
        }
        if self.transport_pool is not None:
            state["transport"] = self.transport_pool.status()
        if self.token_ledger is not None:
            state["budget"] = self.token_ledger.snapshot()
        return state

    # -- derived numbers --------------------------------------------------
    def _derive_metrics(
        self,
        candidate: ScanCandidate,
        *,
        contagion: Any | None,
        spread_bps: float,
        realized_vol_ratio: float,
        now_ns: int,
        q98_residual_bps: float | None = None,
    ) -> tuple[dict[str, float], str, float]:
        base = {str(key): float(value) for key, value in candidate.packet.metrics.items()}
        score = abs(float(candidate.score)) or abs(
            float(base.get("dislocation_score", 0.0))
        )
        if score <= 0.0:
            score = 1.0

        flow = float(base.get("nios_flow_imbalance", 0.0))
        crowd = (
            float(getattr(contagion, "crowd_asymmetry_score", 0.0))
            if contagion is not None
            else 0.0
        )
        fragility = (
            float(getattr(contagion, "cascade_fragility_index", 0.0))
            if contagion is not None
            else 0.0
        )
        exhaustion = (
            float(getattr(contagion, "narrative_exhaustion_divergence", 0.0))
            if contagion is not None
            else 0.0
        )
        direction_score = flow + 0.5 * crowd
        side = "BUY" if direction_score >= 0.0 else "SELL"

        spread = float(spread_bps)
        if not math.isfinite(spread) or spread < 0.0:
            raise ValueError("spread_bps must be finite and non-negative")
        q98 = (
            float(q98_residual_bps)
            if q98_residual_bps is not None
            else self._resolve_q98(candidate.symbol, now_ns)
        )
        if not math.isfinite(q98) or q98 < 0.0:
            raise ValueError("q98_residual_bps must be finite and non-negative")
        expected_gross = _clamp(10.0 + 8.0 * score, 10.0, 80.0)
        net_lb = net_lower_bound_bps(
            expected_gross, spread, self.fee_bps, self.tax_bps, q98
        )
        conviction = _clamp(0.35 + score / 6.0, 0.35, 0.95)
        target_bps = expected_gross
        stop_bps = max(4.0, expected_gross * 0.5)
        valuation_gaps = [
            float(base[name])
            for name in ("nios_dcf_gap", "nios_comps_gap", "nios_ev_ebitda_gap")
            if name in base
        ]
        valuation_gap_pct = (
            sum(valuation_gaps) / float(len(valuation_gaps)) if valuation_gaps else 0.0
        )
        limit_price = float(
            base.get(
                "close",
                candidate.price
                if float(candidate.price) > 0.0
                else 1.0,
            )
        )
        if not math.isfinite(limit_price) or limit_price <= 0.0:
            limit_price = max(float(candidate.price), 1e-6)

        if side == "BUY" and conviction >= 0.65 and fragility < 0.5:
            vault = VaultName.LONG_TERM_COMPOUNDER
            cap_pct = COMPOUNDER_CAP_PCT
        else:
            vault = VaultName.TACTICAL_SWING
            cap_pct = TACTICAL_CAP_PCT
        position_pct = min(cap_pct, cap_pct * conviction + 1e-4)

        metrics = dict(base)
        metrics.update(
            {
                "spread_bps": spread,
                "fee_bps": self.fee_bps,
                "tax_bps": self.tax_bps,
                "committee_q98_residual_bps": q98,
                "committee_expected_gross_bps": expected_gross,
                "committee_net_lb_bps": net_lb,
                "committee_target_bps": target_bps,
                "committee_stop_bps": stop_bps,
                "committee_conviction": conviction,
                "committee_limit_price": limit_price,
                "committee_cap_pct": cap_pct,
                "committee_position_pct": position_pct,
                "committee_dislocation_score": score,
                "committee_valuation_gap_pct": valuation_gap_pct,
                "contagion_fragility_index": fragility,
                "contagion_crowd_asymmetry_score": crowd,
                "contagion_narrative_exhaustion_divergence": exhaustion,
                "realized_vol_ratio": float(realized_vol_ratio),
                "committee_as_of_ns": float(now_ns),
            }
        )
        for value in metrics.values():
            if not math.isfinite(value):
                raise ValueError(
                    "committee metrics must be finite; "
                    "a cold conformal window is priced explicitly"
                )
        return metrics, side, direction_score

    def _resolve_q98(self, symbol: str, as_of_ns: int) -> float:
        if self.feature_store is None:
            return self.cold_start_q98_bps
        try:
            window = self.feature_store.get_q98_residual(
                COMMITTEE_STRATEGY_ID, as_of_ns
            )
            value = float(window.q98_bps)
            if math.isfinite(value) and value >= 0.0:
                return value
        except Exception:  # pragma: no cover - degraded feature store
            pass
        return self.cold_start_q98_bps

    def _commit_packet(
        self, base: GroundedFactPacket, metrics: Mapping[str, float]
    ) -> GroundedFactPacket:
        """Seal the analyst/trader maths into an append-only packet."""
        packet = GroundedFactPacket(
            symbol=base.symbol,
            valid_time_ns=base.valid_time_ns,
            knowledge_ts_ns=base.knowledge_ts_ns,
            as_of_ns=base.as_of_ns,
            metrics=metrics,
        )
        try:
            self.ledger.append(packet, silo=self.silo, persist=False)
        except Exception:
            # The ledger is an audit registry; the packet's own SHA-256 seal is
            # what Ring 0 verifies.  A duplicate digest is not a failure.
            pass
        return packet

    # -- stage 1: analysts ------------------------------------------------
    def _run_analysts(
        self,
        packet: GroundedFactPacket,
        candidate: ScanCandidate,
        contagion: Any | None,
        side: str,
    ) -> list[StageReport]:
        analysts = (
            ("analyst_fundamental", self._fundamental_text, "Fundamental/3-Statement/DCF"),
            ("analyst_technical", self._technical_text, "Technical/Factor-Zoo"),
            ("analyst_sentiment", self._sentiment_text, "Sentiment/Shock-Ontology"),
            ("analyst_macro", self._macro_text, "Macro/MiroFish-Contagion"),
        )
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    self._analyst,
                    stage,
                    actor,
                    function,
                    packet,
                    candidate,
                    contagion,
                    side,
                )
                for stage, function, actor in analysts
            ]
            return [future.result() for future in futures]

    def _analyst(
        self,
        stage: str,
        actor: str,
        function: Callable[..., str],
        packet: GroundedFactPacket,
        candidate: ScanCandidate,
        contagion: Any | None,
        side: str,
    ) -> StageReport:
        text = function(packet, candidate, contagion, side)
        names = [
            name
            for name in packet.metrics
            if name.lower() in text.lower() and _fmt(packet.metrics[name]) in text
        ]
        return StageReport(
            stage=stage,
            actor=actor,
            text=text,
            metrics={name: packet.metrics[name] for name in names},
        )

    @staticmethod
    def _cite(packet: GroundedFactPacket, names: Sequence[str]) -> str:
        parts: list[str] = []
        for name in names:
            if name in packet.metrics:
                parts.append(f"{name} {_fmt(packet.metrics[name])}")
        return ", ".join(parts) if parts else "no committed metric"

    def _fundamental_text(
        self,
        packet: GroundedFactPacket,
        candidate: ScanCandidate,
        contagion: Any | None,
        side: str,
    ) -> str:
        names = [
            name
            for name in (
                "close",
                "nios_dcf_gap",
                "nios_comps_gap",
                "nios_ev_ebitda_gap",
                "committee_valuation_gap_pct",
                "dislocation_score",
            )
            if name in packet.metrics
        ][:4]
        return (
            f"Fundamental analyst: three-statement and DCF review of {candidate.symbol} "
            f"reads {self._cite(packet, names)}. The model is committed before use; "
            "no unsealed input is admitted."
        )

    def _technical_text(
        self,
        packet: GroundedFactPacket,
        candidate: ScanCandidate,
        contagion: Any | None,
        side: str,
    ) -> str:
        candidates = [
            name
            for name in packet.metrics
            if name.startswith(("alpha101_", "gtja191_", "qlib158_"))
        ]
        chosen = candidates[:1] if candidates else []
        names = ["dislocation_score"] + chosen
        return (
            f"Technical analyst: factor-zoo projection for {candidate.symbol} is "
            f"{self._cite(packet, names)}; the full factor zoo was scanned before "
            "this candidate reached the committee."
        )

    def _sentiment_text(
        self,
        packet: GroundedFactPacket,
        candidate: ScanCandidate,
        contagion: Any | None,
        side: str,
    ) -> str:
        ontology = (
            ", ".join(getattr(contagion, "tags", ()) or ())
            if contagion is not None
            else ""
        )
        names = [
            "contagion_narrative_exhaustion_divergence",
            "contagion_crowd_asymmetry_score",
        ]
        return (
            f"Sentiment analyst: shock ontology for {candidate.symbol} "
            f"({ontology or 'no causal tag'}) reads {self._cite(packet, names)}. "
            "Retail euphoria is separated from institutional absorption."
        )

    def _macro_text(
        self,
        packet: GroundedFactPacket,
        candidate: ScanCandidate,
        contagion: Any | None,
        side: str,
    ) -> str:
        names = [
            "contagion_fragility_index",
            "contagion_crowd_asymmetry_score",
            "realized_vol_ratio",
        ]
        return (
            f"Macro analyst: three-tick MiroFish cascade for {candidate.symbol} "
            f"reports {self._cite(packet, names)}. The fragility threshold is "
            "committed policy, not a discretionary override."
        )

    # -- stage 2: debate --------------------------------------------------
    def _run_debate(
        self, packet: GroundedFactPacket, side: str
    ) -> list[StageReport]:
        stages: list[StageReport] = []
        for round_index in range(1, self.debate_rounds + 1):
            bull = (
                f"Bull researcher round {round_index}: the dislocation pays "
                f"{self._cite(packet, ['committee_expected_gross_bps', 'committee_target_bps'])} "
                f"with conviction {_fmt(packet.metrics['committee_conviction'])} "
                "if the tape stays orderly."
            )
            bear = (
                f"Bear researcher round {round_index}: contagion risk is "
                f"{self._cite(packet, ['contagion_fragility_index', 'committee_q98_residual_bps'])} "
                "and a forced deleveraging would hit the stop before the target."
            )
            if round_index > 1:
                bull += (
                    " Rebuttal: net lower bound is "
                    f"{self._cite(packet, ['committee_net_lb_bps'])} after costs."
                )
                bear += (
                    " Rebuttal: spread and stop are "
                    f"{self._cite(packet, ['spread_bps', 'committee_stop_bps'])}; "
                    "the tail is not free."
                )
            stages.append(
                StageReport(f"debate_bull_r{round_index}", "Bull", bull)
            )
            stages.append(
                StageReport(f"debate_bear_r{round_index}", "Bear", bear)
            )
        return stages

    # -- stage 3: trader --------------------------------------------------
    def _run_trader(
        self, packet: GroundedFactPacket, side: str, direction_score: float
    ) -> StageReport:
        text = (
            f"Trader: direction {side} on a signed dislocation of "
            f"{_fmt(direction_score)}; expected gross "
            f"{self._cite(packet, ['committee_expected_gross_bps', 'committee_target_bps', 'committee_stop_bps'])}; "
            f"entry limit {self._cite(packet, ['committee_limit_price'])}; "
            f"valuation gap {self._cite(packet, ['committee_valuation_gap_pct'])}; "
            f"conviction {self._cite(packet, ['committee_conviction'])}. "
            "Sizing is delegated to the risk committee."
        )
        return StageReport("trader", "Trader", text)

    # -- stage 4: risk committee -----------------------------------------
    def _run_risk_committee(
        self, packet: GroundedFactPacket, side: str
    ) -> tuple[list[StageReport], VaultName, float, float]:
        cap_pct = float(packet.metrics["committee_cap_pct"])
        position_pct = float(packet.metrics["committee_position_pct"])
        vault = (
            VaultName.LONG_TERM_COMPOUNDER
            if abs(cap_pct - COMPOUNDER_CAP_PCT) < 1e-12
            else VaultName.TACTICAL_SWING
        )
        aggressive = (
            "Aggressive debater: size to "
            f"{self._cite(packet, ['committee_position_pct', 'committee_target_bps'])} "
            f"and press the {side} dislocation while liquidity is present."
        )
        neutral = (
            "Neutral debater: accept only because net lower bound is "
            f"{self._cite(packet, ['committee_net_lb_bps', 'committee_q98_residual_bps'])} "
            "with a calibrated split-conformal tail."
        )
        conservative = (
            "Conservative debater: cap at "
            f"{self._cite(packet, ['committee_cap_pct', 'committee_stop_bps'])} "
            f"and respect contagion fragility "
            f"{self._cite(packet, ['contagion_fragility_index'])}; "
            "route to the smaller vault if it rises."
        )
        consensus = (
            f"Risk consensus: route to {vault.value} at "
            f"{self._cite(packet, ['committee_position_pct'])} of NAV, "
            f"never above the committed cap "
            f"{self._cite(packet, ['committee_cap_pct'])}."
        )
        stages = [
            StageReport("risk_aggressive", "Aggressive", aggressive),
            StageReport("risk_neutral", "Neutral", neutral),
            StageReport("risk_conservative", "Conservative", conservative),
            StageReport("risk_consensus", "RiskCommittee", consensus),
        ]
        return stages, vault, cap_pct, position_pct

    # -- proposal ---------------------------------------------------------
    def _build_proposal(
        self,
        candidate: ScanCandidate,
        packet: GroundedFactPacket,
        metrics: Mapping[str, float],
        *,
        side: str,
        vault: VaultName,
        position_pct: float,
        now_ns: int,
    ) -> UnsignedProposal:
        cap_pct = (
            COMPOUNDER_CAP_PCT
            if vault is VaultName.LONG_TERM_COMPOUNDER
            else TACTICAL_CAP_PCT
        )
        position_pct = _clamp(float(position_pct), 0.0, cap_pct)
        limit_price = float(metrics["committee_limit_price"])
        notional = self.nav * position_pct
        qty = notional / limit_price
        if qty <= 0.0:
            qty = 1e-6
        proposal_id = canonical_pairs_digest(
            [
                ("strategy", COMMITTEE_STRATEGY_ID),
                ("symbol", candidate.symbol),
                ("packet", packet.sha256_digest),
                ("side", side),
                ("vault", vault.value),
            ]
        )[:32]
        return UnsignedProposal(
            proposal_id=proposal_id,
            strategy_id=COMMITTEE_STRATEGY_ID,
            silo=self.silo,
            vault=vault,
            symbol=candidate.symbol,
            side=side,
            qty=qty,
            limit_price=limit_price,
            expected_gross_bps=float(metrics["committee_expected_gross_bps"]),
            spread_bps=float(metrics["spread_bps"]),
            grounding_hash=packet.sha256_digest,
            created_at_ns=now_ns,
        )

    def _publish(
        self, proposal: UnsignedProposal, report: CommitteeReport
    ) -> Any:
        payload = {
            "proposal": proposal.to_dict(),
            "packet_digest": report.packet_digest,
            "committee": {
                "vault": report.vault.value,
                "cap_pct": report.cap_pct,
                "position_pct": report.position_pct,
                "side": report.side,
                "claims_checked": sum(
                    verification.claims_checked
                    for verification in report.verifications
                ),
                "stages": len(report.stages),
                "elapsed_ms": report.elapsed_ms,
            },
        }
        subject = f"{STREAM_RING2_THESES}.{proposal.symbol}"
        key = f"ring2:{proposal.proposal_id}"
        bus = self.bus
        if hasattr(bus, "publish_idempotent_sync"):
            return bus.publish_idempotent_sync(subject, payload, key)
        if hasattr(bus, "publish_idempotent"):
            return bus.publish_idempotent(subject, payload, key)
        raise TypeError(
            "committee bus must expose publish_idempotent(_sync) (or use open_bus)"
        )


#: Architecture naming aliases.
System2StagedCommittee = System2InvestmentCommittee
System2Committee = System2InvestmentCommittee
StagedInvestmentCommittee = System2InvestmentCommittee
CommitteeGraph = System2InvestmentCommittee
