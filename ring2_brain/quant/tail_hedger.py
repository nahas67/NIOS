"""Automated cross-asset options tail-hedging for the TacticalSwing vault.

``sec_017`` (Phase 4) authorizes the tactical vault to buy protective OTM puts
or collars when the portfolio's tail risk is elevated.  This module is the
Ring 2 decision layer: it observes the MiroFish contagion cascade and the
cross-silo tail-copula shield, prices the hedge with the shared
:class:`~ring2_brain.quant.scanner.ValuationLab` Black-Scholes engine, seals
every number into a SHA-256 :class:`GroundedFactPacket`, and emits a grounded
:class:`UnsignedProposal` routed to ``VaultName.TACTICAL_SWING``.

It never signs, never touches a broker and never writes the IBOR.  Ring 0 is
the only ring that verifies, sizes against the vault allocation and signs the
proposal.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from core.schemas import (
    ContagionFeatures,
    GroundedFactPacket,
    UnsignedProposal,
    VaultName,
)
from ring2_brain.quant.scanner import ValuationLab, black_scholes

__all__ = [
    "TailHedgeStatus",
    "TailHedgePlan",
    "TacticalTailHedger",
    "FRAGILITY_TRIGGER",
    "TAIL_CORRELATION_TRIGGER",
]

FRAGILITY_TRIGGER = 0.65
TAIL_CORRELATION_TRIGGER = 0.60
DEFAULT_RISK_FREE_RATE = 0.05
DEFAULT_IMPLIED_VOLATILITY = 0.35
DEFAULT_HORIZON_DAYS = 30
DEFAULT_CONTRACT_MULTIPLIER = 100
DEFAULT_PORTFOLIO_NOTIONAL_MICROS = 100_000_000_000_000  # 100m currency units
DEFAULT_TACTICAL_VAULT_MICROS = 12_000_000_000_000


@dataclass
class TailHedgeStatus:
    """Snapshot of the tail-risk trigger state."""

    triggered: bool
    cascade_fragility_index: float
    tail_correlation: float
    reasons: list[str] = field(default_factory=list)
    thresholds: dict[str, float] = field(
        default_factory=lambda: {
            "cascade_fragility_index": FRAGILITY_TRIGGER,
            "tail_correlation": TAIL_CORRELATION_TRIGGER,
        }
    )
    portfolio_notional_micros: int = DEFAULT_PORTFOLIO_NOTIONAL_MICROS
    tactical_vault_micros: int = DEFAULT_TACTICAL_VAULT_MICROS
    as_of_ns: int = 0
    silos: tuple[str, ...] = ("CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5")

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "cascade_fragility_index": round(self.cascade_fragility_index, 6),
            "tail_correlation": round(self.tail_correlation, 6),
            "reasons": list(self.reasons),
            "thresholds": dict(self.thresholds),
            "portfolio_notional_micros": self.portfolio_notional_micros,
            "tactical_vault_micros": self.tactical_vault_micros,
            "as_of_ns": self.as_of_ns,
            "silos": list(self.silos),
        }


@dataclass
class TailHedgePlan:
    """A fully priced, grounded protective-put proposal."""

    plan_id: str
    symbol: str
    silo: str
    vault: VaultName
    side: str
    spot: float
    strike: float
    years: float
    volatility: float
    risk_free_rate: float
    contracts: int
    contract_multiplier: int
    premium_per_unit: float
    premium_micros: int
    premium_bps: float
    hedge_notional_micros: int
    hedge_ratio: float
    cascade_fragility_index: float
    tail_correlation: float
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float
    packet: GroundedFactPacket
    proposal: UnsignedProposal
    thesis: str
    grounding_verified: bool
    grounding_claims_checked: int
    reasons: list[str] = field(default_factory=list)
    valuation_source: str = "ValuationLab.options"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "symbol": self.symbol,
            "silo": self.silo,
            "vault": str(self.vault),
            "side": self.side,
            "spot": self.spot,
            "strike": self.strike,
            "years": self.years,
            "volatility": self.volatility,
            "risk_free_rate": self.risk_free_rate,
            "contracts": self.contracts,
            "contract_multiplier": self.contract_multiplier,
            "premium_per_unit": self.premium_per_unit,
            "premium_micros": self.premium_micros,
            "premium_bps": self.premium_bps,
            "hedge_notional_micros": self.hedge_notional_micros,
            "hedge_ratio": self.hedge_ratio,
            "cascade_fragility_index": self.cascade_fragility_index,
            "tail_correlation": self.tail_correlation,
            "greeks": {
                "delta": self.delta,
                "gamma": self.gamma,
                "vega": self.vega,
                "theta": self.theta,
                "rho": self.rho,
            },
            "packet": self.packet.to_dict(),
            "proposal": self.proposal.to_dict(),
            "grounding_verified": self.grounding_verified,
            "grounding_claims_checked": self.grounding_claims_checked,
            "reasons": list(self.reasons),
            "valuation_source": self.valuation_source,
        }


class TacticalTailHedger:
    """Cross-asset protective-put / collar decision engine.

    Parameters
    ----------
    valuation_lab:
        Shared :class:`ValuationLab`; defaults to a local instance.
    contagion_engine:
        Optional :class:`MiroFishContagionEngine`; when present,
        :meth:`monitor_from_engine` reads its latest cascade features.
    copula_shield:
        Optional :class:`CrossSiloCopulaShield`; when present,
        :meth:`monitor_from_copula` reads the maximum measured tail
        correlation for the requested silo.
    """

    def __init__(
        self,
        *,
        valuation_lab: ValuationLab | None = None,
        contagion_engine: Any = None,
        copula_shield: Any = None,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        implied_volatility: float = DEFAULT_IMPLIED_VOLATILITY,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
        contract_multiplier: int = DEFAULT_CONTRACT_MULTIPLIER,
        max_premium_bps: float = 250.0,
        min_fragility: float = FRAGILITY_TRIGGER,
        min_tail_correlation: float = TAIL_CORRELATION_TRIGGER,
        portfolio_notional_micros: int = DEFAULT_PORTFOLIO_NOTIONAL_MICROS,
        tactical_vault_micros: int = DEFAULT_TACTICAL_VAULT_MICROS,
    ):
        self.valuation = valuation_lab or ValuationLab()
        self.contagion = contagion_engine
        self.copula = copula_shield
        self.risk_free_rate = float(risk_free_rate)
        self.implied_volatility = float(implied_volatility)
        self.horizon_days = max(1, int(horizon_days))
        self.contract_multiplier = max(1, int(contract_multiplier))
        self.max_premium_bps = max(1.0, float(max_premium_bps))
        self.min_fragility = float(min_fragility)
        self.min_tail_correlation = float(min_tail_correlation)
        self.portfolio_notional_micros = max(1, int(portfolio_notional_micros))
        self.tactical_vault_micros = max(1, int(tactical_vault_micros))
        self.last_status: TailHedgeStatus | None = None
        self.last_plan: TailHedgePlan | None = None
        self.triggers = 0
        self.hedges_emitted = 0
        self.history: list[dict[str, Any]] = []

    # -- monitoring -------------------------------------------------------
    def monitor(
        self,
        *,
        cascade_fragility_index: float,
        tail_correlation: float,
        portfolio_notional_micros: int | None = None,
        tactical_vault_micros: int | None = None,
        as_of_ns: int | None = None,
        silos: Sequence[str] | None = None,
    ) -> TailHedgeStatus:
        fragility = _clamp(float(cascade_fragility_index), 0.0, 1.0)
        correlation = _clamp(float(tail_correlation), 0.0, 1.0)
        reasons: list[str] = []
        if fragility >= self.min_fragility:
            reasons.append(
                f"cascade_fragility_index {fragility:.4f} >= {self.min_fragility:.4f}"
            )
        if correlation > self.min_tail_correlation:
            reasons.append(
                f"cross_silo_tail_correlation {correlation:.4f} > "
                f"{self.min_tail_correlation:.4f}"
            )
        status = TailHedgeStatus(
            triggered=bool(reasons),
            cascade_fragility_index=fragility,
            tail_correlation=correlation,
            reasons=reasons,
            portfolio_notional_micros=int(
                portfolio_notional_micros or self.portfolio_notional_micros
            ),
            tactical_vault_micros=int(tactical_vault_micros or self.tactical_vault_micros),
            as_of_ns=int(as_of_ns or time.time_ns()),
            silos=tuple(silos or ("CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5")),
        )
        self.last_status = status
        if status.triggered:
            self.triggers += 1
        return status

    def monitor_from_contagion(
        self,
        features: ContagionFeatures,
        *,
        tail_correlation: float,
        **kwargs: Any,
    ) -> TailHedgeStatus:
        return self.monitor(
            cascade_fragility_index=features.cascade_fragility_index,
            tail_correlation=tail_correlation,
            **kwargs,
        )

    def monitor_from_engine(self, symbol: str | None = None, **kwargs: Any) -> TailHedgeStatus:
        if self.contagion is None:
            raise RuntimeError("no MiroFishContagionEngine attached")
        result = getattr(self.contagion, "last_result", None)
        if result is None and symbol is not None:
            from ring2_brain.mirofish.contagion_engine import ShockVector

            result = self.contagion.run(
                ShockVector(price_shock_bps=120.0, realized_vol_ratio=2.5),
                symbol,
                persist=False,
            )
        if result is None:
            raise RuntimeError("contagion engine has no cascade result")
        features = result.features
        correlation = kwargs.pop("tail_correlation", None)
        if correlation is None and self.copula is not None:
            correlation = float(self.copula.max_tail_correlation("US"))
        if correlation is None:
            correlation = 0.0
        return self.monitor_from_contagion(
            features, tail_correlation=float(correlation), **kwargs
        )

    def monitor_from_copula(
        self,
        market: Any = "US",
        *,
        cascade_fragility_index: float | None = None,
        **kwargs: Any,
    ) -> TailHedgeStatus:
        if self.copula is None:
            raise RuntimeError("no CrossSiloCopulaShield attached")
        correlation = float(self.copula.max_tail_correlation(market))
        fragility = cascade_fragility_index
        if fragility is None and self.contagion is not None:
            result = getattr(self.contagion, "last_result", None)
            fragility = (
                float(result.features.cascade_fragility_index) if result is not None else 0.0
            )
        return self.monitor(
            cascade_fragility_index=float(fragility or 0.0),
            tail_correlation=correlation,
            **kwargs,
        )

    # -- hedge construction ----------------------------------------------
    def build_hedge(
        self,
        status: TailHedgeStatus | None = None,
        *,
        spot: float,
        symbol: str = "PORTFOLIO",
        silo: str = "US_IBKR",
        as_of_ns: int | None = None,
        implied_volatility: float | None = None,
    ) -> TailHedgePlan | None:
        state = status or self.last_status
        if state is None or not state.triggered:
            return None
        now = int(as_of_ns or state.as_of_ns or time.time_ns())
        spot = float(spot)
        if not math.isfinite(spot) or spot <= 0:
            raise ValueError("spot must be a positive finite number")
        volatility = float(implied_volatility or self.implied_volatility)
        years = self.horizon_days / 365.0
        severity = max(
            (state.cascade_fragility_index - self.min_fragility)
            / max(1e-9, 1.0 - self.min_fragility),
            (state.tail_correlation - self.min_tail_correlation)
            / max(1e-9, 1.0 - self.min_tail_correlation),
            0.0,
        )
        severity = _clamp(severity, 0.0, 1.0)
        hedge_ratio = 0.10 + 0.25 * severity
        otm_pct = 0.05 + 0.10 * severity
        strike = spot * (1.0 - otm_pct)

        greeks, source = self._price_put(spot, strike, volatility, years)
        premium_per_unit = float(greeks["price"])
        portfolio = max(1, state.portfolio_notional_micros)
        hedge_notional = int(portfolio * hedge_ratio)
        contracts = max(
            1,
            int(round(hedge_notional / max(1e-9, spot * self.contract_multiplier))),
        )
        premium_micros = int(round(premium_per_unit * contracts * self.contract_multiplier * 1_000_000))
        premium_bps = (premium_micros / portfolio) * 10_000.0
        vault_cap = max(
            1,
            min(
                state.tactical_vault_micros,
                int(portfolio * 0.12),
            ),
        )

        # Respect both the premium budget and the TacticalSwing allocation.
        while contracts > 1 and (
            premium_bps > self.max_premium_bps or premium_micros > vault_cap
        ):
            contracts -= 1
            premium_micros = int(
                round(premium_per_unit * contracts * self.contract_multiplier * 1_000_000)
            )
            premium_bps = (premium_micros / portfolio) * 10_000.0

        if premium_bps > self.max_premium_bps or premium_micros > vault_cap:
            # A single contract still breaches the allocation: emit nothing
            # rather than an unaffordable hedge.
            return None

        plan_id = hashlib.sha256(
            f"{symbol}:{silo}:{now}:{spot:.10f}:{strike:.10f}:"
            f"{state.cascade_fragility_index:.10f}:{state.tail_correlation:.10f}".encode()
        ).hexdigest()[:24]
        metrics: dict[str, float] = {
            "spot": spot,
            "strike": strike,
            "years": years,
            "implied_volatility": volatility,
            "risk_free_rate": self.risk_free_rate,
            "delta": float(greeks["delta"]),
            "gamma": float(greeks["gamma"]),
            "vega": float(greeks["vega"]),
            "theta": float(greeks["theta"]),
            "rho": float(greeks["rho"]),
            "premium_per_unit": premium_per_unit,
            "premium_micros": float(premium_micros),
            "premium_bps": premium_bps,
            "contracts": float(contracts),
            "contract_multiplier": float(self.contract_multiplier),
            "hedge_ratio": hedge_ratio,
            "hedge_notional_micros": float(hedge_notional),
            "portfolio_notional_micros": float(portfolio),
            "tactical_vault_micros": float(state.tactical_vault_micros),
            "vault_cap_micros": float(vault_cap),
            "cascade_fragility_index": state.cascade_fragility_index,
            "tail_correlation": state.tail_correlation,
            "otm_pct": otm_pct,
        }
        packet = GroundedFactPacket(
            symbol=symbol,
            valid_time_ns=now - 2,
            knowledge_ts_ns=now - 1,
            as_of_ns=now,
            metrics=metrics,
        )
        thesis = (
            f"Tactical tail hedge {symbol}: cascade fragility "
            f"{state.cascade_fragility_index:.4f}, cross-silo tail correlation "
            f"{state.tail_correlation:.4f}, spot {spot:.2f}, strike {strike:.2f}, "
            f"implied volatility {volatility:.4f}, years {years:.4f}, delta "
            f"{float(greeks['delta']):.4f}, gamma {float(greeks['gamma']):.6f}, "
            f"vega {float(greeks['vega']):.4f}, theta {float(greeks['theta']):.4f}, "
            f"rho {float(greeks['rho']):.4f}, premium per unit "
            f"{premium_per_unit:.6f}, premium {premium_bps:.2f} bps, hedge ratio "
            f"{hedge_ratio:.4f}, contracts {contracts}."
        )

        verifier = _grounding_verifier()
        report = verifier.verify_thesis_claims(thesis, packet)
        proposal = UnsignedProposal(
            proposal_id=f"tail-hedge-{plan_id}",
            strategy_id="tactical-tail-hedger-v1",
            silo=silo,
            vault=VaultName.TACTICAL_SWING,
            symbol=symbol,
            side="BUY",
            qty=float(contracts),
            limit_price=float(premium_per_unit),
            expected_gross_bps=float(-premium_bps),
            spread_bps=0.0,
            grounding_hash=packet.sha256_digest,
        )
        plan = TailHedgePlan(
            plan_id=plan_id,
            symbol=symbol,
            silo=silo,
            vault=VaultName.TACTICAL_SWING,
            side="BUY",
            spot=spot,
            strike=strike,
            years=years,
            volatility=volatility,
            risk_free_rate=self.risk_free_rate,
            contracts=contracts,
            contract_multiplier=self.contract_multiplier,
            premium_per_unit=premium_per_unit,
            premium_micros=premium_micros,
            premium_bps=premium_bps,
            hedge_notional_micros=hedge_notional,
            hedge_ratio=hedge_ratio,
            cascade_fragility_index=state.cascade_fragility_index,
            tail_correlation=state.tail_correlation,
            delta=float(greeks["delta"]),
            gamma=float(greeks["gamma"]),
            vega=float(greeks["vega"]),
            theta=float(greeks["theta"]),
            rho=float(greeks["rho"]),
            packet=packet,
            proposal=proposal,
            thesis=thesis,
            grounding_verified=bool(report is not None),
            grounding_claims_checked=int(getattr(report, "claims_checked", 0)),
            reasons=list(state.reasons),
            valuation_source=source,
        )
        self.last_plan = plan
        self.hedges_emitted += 1
        self.history.append(plan.to_dict())
        return plan

    def monitor_and_hedge(
        self,
        *,
        cascade_fragility_index: float,
        tail_correlation: float,
        spot: float,
        symbol: str = "PORTFOLIO",
        silo: str = "US_IBKR",
        **kwargs: Any,
    ) -> tuple[TailHedgeStatus, TailHedgePlan | None]:
        status = self.monitor(
            cascade_fragility_index=cascade_fragility_index,
            tail_correlation=tail_correlation,
            **{k: v for k, v in kwargs.items() if k in {"portfolio_notional_micros", "tactical_vault_micros", "as_of_ns", "silos"}},
        )
        plan = self.build_hedge(status, spot=spot, symbol=symbol, silo=silo)
        return status, plan

    def _price_put(
        self, spot: float, strike: float, volatility: float, years: float
    ) -> tuple[dict[str, float], str]:
        # The shared ValuationLab is the authoritative production path.
        try:
            result = self.valuation.options(
                spot=spot,
                strike=strike,
                volatility=volatility,
                rate=self.risk_free_rate,
                years=years,
                kind="put",
            )
            if all(key in result for key in ("price", "delta", "gamma", "vega", "theta", "rho")):
                return result, "ValuationLab.options"
        except Exception:
            pass
        return black_scholes(
            spot, strike, volatility, self.risk_free_rate, years, kind="put"
        ), "black_scholes"

    def status(self) -> dict[str, Any]:
        plan = self.last_plan
        return {
            "kind": "tactical_tail_hedger",
            "triggers": self.triggers,
            "hedges_emitted": self.hedges_emitted,
            "last_status": self.last_status.to_dict() if self.last_status else None,
            "last_plan": plan.to_dict() if plan else None,
            "thresholds": {
                "cascade_fragility_index": self.min_fragility,
                "tail_correlation": self.min_tail_correlation,
                "max_premium_bps": self.max_premium_bps,
            },
            "valuation_donor": (
                self.valuation.donor_status()
                if callable(getattr(self.valuation, "donor_status", None))
                else getattr(self.valuation, "donor_status", None)
            ),
            "contagion_attached": self.contagion is not None,
            "copula_attached": self.copula is not None,
        }

    def reset(self) -> None:
        self.last_status = None
        self.last_plan = None
        self.triggers = 0
        self.hedges_emitted = 0
        self.history.clear()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _grounding_verifier():
    from ring2_brain.truth.grounding_gate import NumericalGroundingVerifier

    return NumericalGroundingVerifier()
