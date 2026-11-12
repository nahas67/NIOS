"""End-to-end 4-Ring runtime coordinator (Step 7).

``NIOSCoreOrchestrator`` is the deterministic wiring harness for the whole
constitution.  It owns one instance of every ring and routes a single market
event through the entire pipeline in the mandated order:

    DualVendorBar
      -> SelfHealingDataGuard (Ring 2 truth)          [quarantine / PIT]
      -> OfiRingBuffer + FeatureStore (Tier 0 / Tier 2)
      -> VectorizedFactorScanner (5,000 -> 50)        [Ring 2 quant]
      -> MiroFishContagionEngine (3-tick cascade)     [Ring 2, revokes leases]
      -> System1InstinctController (60 s lease)       [Ring 2 -> Tier 0]
      -> ZeroGCSpinalEngine (breaker + OFI + shadow)  [Ring 1, <0.14 ms]
      -> SovereignPreTradeFirewall (5 stages)         [Ring 0]
      -> SplitConformal98Gate (98% Net_LB > 0)
      -> CrossSiloCopulaShield (tail heat)
      -> CrashAtomicOutbox (two-phase reservation)
      -> OrderSigner (Ed25519 + Fernet)
      -> SmartOMS (Maker-Peg / TWAP)
      -> DelayedSettlementWorker + IBORLedger
      -> InSiloProfitRatchet (35% / 15% / 50%)
      -> PlainEnglishCockpitCard -> stream.ring3.cockpit-cards

The orchestrator is intentionally *not* a trading strategy.  It is the
constitutional wiring: every hop is an existing, individually tested object,
and every hop fails closed.
"""

from __future__ import annotations

import math
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.feature_store import FeatureStore
from core.llm_transport import LLMHttp2TransportPool, TokenBudgetLedger
from core.nats_bus import LocalJetStreamBus
from core.plugins import PluginRegistry
from core.schemas import (
    ContagionFeatures,
    DualVendorBar,
    ExecutedFlowTick,
    PlainEnglishCockpitCard,
    UnsignedProposal,
    VaultName,
)
from core.shm_ipc import (
    GlobalKillLatch,
    LeaseTableSHM,
    OfiRingBuffer,
    ShadowBookSHM,
)
from ring0_vault.conformal_copula_gate import (
    ConformalMirror,
    CrossSiloCopulaShield,
    SplitConformal98Gate,
)
from ring0_vault.ibor.ledger_outbox import (
    CrashAtomicOutbox,
    IBORLedger,
    InSiloProfitRatchet,
    LedgerStore,
    SiloGuard,
    to_micros,
)
from ring0_vault.kernel.firewalls_and_oms import (
    ADR009ReduceOnlyFastPath,
    OrderSigner,
    SmartOMS,
    SovereignPreTradeFirewall,
)
from ring0_vault.silos.key_vault.llm_credentials import LLMKeyVault
from ring0_vault.silos.key_vault.broker_credentials import BrokerCredentialVault
from ring0_vault.silos.silo_registry import MarketSiloRegistry
from ring1_spinal.shadow_twin import ShadowExecutionTwin
from ring1_spinal.spinal_loop import TickOutcome, ZeroGCSpinalEngine
from ring2_brain.cognition.committee_graph import System2InvestmentCommittee
from ring2_brain.cognition.system1_instinct import (
    InstinctContext,
    InstinctDecision,
    MultiLLMSwitchboard,
    System1InstinctController,
    default_providers,
)
from ring2_brain.forge.alpha_forge import ChampionRecord, NocturnalAlphaForge
from ring2_brain.mirofish.contagion_engine import (
    CascadeResult,
    MiroFishContagionEngine,
    ShockVector,
)
from ring2_brain.quant.scanner import FactorPanel, ScanResult, VectorizedFactorScanner
from ring2_brain.quant.tail_hedger import TacticalTailHedger
from ring2_brain.skills.skill_registry import SkillRegistry
from ring2_brain.truth.data_guard import (
    NormalizedBar,
    OrderflowNormalizer,
    QuarantineStore,
    SelfHealingDataGuard,
)
from ring2_brain.truth.grounding_gate import GroundingLedger
from ring2_brain.truth.settlement_worker import DelayedSettlementWorker
from ring0_vault.ibor.tb_pg_provisioner import TigerBeetlePG16Provisioner
from ring0_vault.kernel.formal_verifier import FormalVerificationSuite

__all__ = [
    "NIOSCoreOrchestrator",
    "ALL_SILOS",
    "AUTOPILOT_CONTAGION_EVERY",
    "AUTOPILOT_INTERVAL_S",
    "DISPLAY_SILO",
    "PRIMARY_SILO",
    "PRIMARY_SYMBOL",
    "SILO_INSTRUMENTS",
    "SILO_REFERENCE_PRICES",
    "STRATEGY_ID",
    "make_flow_tick",
    "make_dual_vendor_bar",
]

#: ``sec_016`` display names for cockpit cards.
DISPLAY_SILO: Mapping[str, str] = {
    "CRYPTO_BINANCE": "BINANCE-CRYPTO",
    "INDIA_ZERODHA_DHAN": "ZERODHA-INDIA",
    "US_IBKR": "IBKR-US",
    "FX_MT5": "MT5-FX",
}

PRIMARY_SILO = "INDIA_ZERODHA_DHAN"
PRIMARY_SYMBOL = "RELIANCE.NS"
STRATEGY_ID = "spinal-scalper-v1"

ALL_SILOS: tuple[str, ...] = (
    "CRYPTO_BINANCE",
    PRIMARY_SILO,
    "US_IBKR",
    "FX_MT5",
)

#: ``sec_015``: the one instrument each silo is whitelisted to scalp.
SILO_INSTRUMENTS: Mapping[str, str] = {
    "CRYPTO_BINANCE": "BTC-USDT",
    PRIMARY_SILO: "RELIANCE.NS",
    "US_IBKR": "NVDA",
    "FX_MT5": "EUR_USD",
}

#: Reference mid for the paper-shadow tape (no live venue is contacted).
SILO_REFERENCE_PRICES: Mapping[str, float] = {
    "CRYPTO_BINANCE": 64_250.0,
    PRIMARY_SILO: 2_942.5,
    "US_IBKR": 121.4,
    "FX_MT5": 1.0842,
}

#: ``sec_013``: the paper-shadow autopilot cadence (250 ms - 500 ms window).
AUTOPILOT_INTERVAL_S: float = 0.4

#: Re-run the three-tick MiroFish cascade every N autopilot rounds.
AUTOPILOT_CONTAGION_EVERY: int = 6

#: Inject a genuine cross-venue liquidity dislocation every N autopilot rounds.
#: The CRYPTO silo's round-trip floor is 16.0 bps (2 x 7.5 bps maker + 1.0 bps
#: spread), so a 20 bps deterministic ceiling cannot clear the 98% conformal
#: gate with any residual headroom left (``sec_014`` tax-net cost floors).
AUTOPILOT_DISLOCATION_EVERY: int = 4

#: Base gross edge of an injected dislocation, inside the 24-30 bps band.
AUTOPILOT_DISLOCATION_BASE_BPS: float = 26.0


def make_dual_vendor_bar(
    symbol: str = PRIMARY_SYMBOL,
    *,
    price: float = 100.0,
    now_ns: int | None = None,
    lag_ms: float = 12.0,
    divergence_bps: float = 2.0,
) -> DualVendorBar:
    """Build a PIT-clean, gate-passing dual-vendor bar for tests/replays."""
    stamp = int(now_ns if now_ns is not None else time.time_ns())
    offset = price * divergence_bps / 10_000.0 / 2.0
    return DualVendorBar(
        symbol=symbol,
        p1=price + offset,
        p2=price - offset,
        feed_lag_ms=lag_ms,
        valid_time_ns=stamp,
        knowledge_ts_ns=stamp,
    )


def make_flow_tick(
    symbol: str = PRIMARY_SYMBOL,
    *,
    last_price: float = 99.99,
    now_ns: int | None = None,
    buy_volume: float = 100.0,
    sell_volume: float = 1.0,
    spread_bps: float = 2.0,
    realized_vol: float = 5.0,
    normal_vol: float = 10.0,
) -> ExecutedFlowTick:
    """Build an executed-flow frame that passes the anti-spoof BUY gate."""
    stamp = int(now_ns if now_ns is not None else time.time_ns())
    return ExecutedFlowTick(
        symbol=symbol,
        valid_time_ns=stamp,
        knowledge_ts_ns=stamp,
        last_price=float(last_price),
        spread_bps=float(spread_bps),
        realized_vol_5s=float(realized_vol),
        normal_vol_5s=float(normal_vol),
        taker_buy_volume_5s=float(buy_volume),
        taker_sell_volume_5s=float(sell_volume),
        bid_cancel_to_fill_ratio=0.4,
        ask_cancel_to_fill_ratio=0.4,
    )


@dataclass(slots=True)
class PipelineReport:
    """Everything one end-to-end run proved, in one auditable record."""

    symbol: str
    silo: str
    shadow_fills: int
    ticks: int
    live_outcome: str
    firewall_accepted: bool
    firewall_reason: str
    net_lb_bps: float
    signed: bool
    reservation_committed: bool
    ratchet: dict[str, int]
    realized_net_micros: int
    cards: list[dict[str, Any]] = field(default_factory=list)
    vaults_before: dict[str, int] = field(default_factory=dict)
    vaults_after: dict[str, int] = field(default_factory=dict)
    contagion: dict[str, Any] = field(default_factory=dict)
    scan: dict[str, Any] = field(default_factory=dict)
    thesis: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "silo": self.silo,
            "shadow_fills": self.shadow_fills,
            "ticks": self.ticks,
            "live_outcome": self.live_outcome,
            "firewall_accepted": self.firewall_accepted,
            "firewall_reason": self.firewall_reason,
            "net_lb_bps": self.net_lb_bps,
            "signed": self.signed,
            "reservation_committed": self.reservation_committed,
            "ratchet": dict(self.ratchet),
            "realized_net_micros": self.realized_net_micros,
            "cards": list(self.cards),
            "vaults_before": dict(self.vaults_before),
            "vaults_after": dict(self.vaults_after),
            "contagion": dict(self.contagion),
            "scan": dict(self.scan),
            "thesis": dict(self.thesis),
            "elapsed_ms": self.elapsed_ms,
        }


class NIOSCoreOrchestrator:
    """One deterministic runtime that owns all four rings."""

    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        silos: Iterable[str] = ALL_SILOS,
        strategy_id: str = STRATEGY_ID,
        seed_fills: int = 60,
        funding_micros: int = 5_000_000_000_000,
        daily_loss_stop_micros: int = 5_000_000_000,
        llm_budget_usd: float = 3.10,
        registry: MarketSiloRegistry | None = None,
        bus: LocalJetStreamBus | None = None,
        key_vault_root: str | Path | None = None,
        require_registered_grounding: bool = False,
    ) -> None:
        self.strategy_id = str(strategy_id)
        self.silos = tuple(str(s) for s in silos)
        self.symbol = PRIMARY_SYMBOL
        self._owns_workspace = workspace is None
        self.workspace = (
            Path(workspace)
            if workspace is not None
            else Path(tempfile.mkdtemp(prefix="nios-e2e-"))
        )
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._verdicts: dict[str, Any] = {}
        self._last_gate_card: dict[str, Any] | None = None
        self._last_live_verdict: Any = None
        self._last_committed_reservation: Any = None
        self._last_buy_ratio: float = 0.0
        self._report: PipelineReport | None = None
        self._cards: list[dict[str, Any]] = []
        # Paper-shadow autopilot telemetry (Ring 3 lifespan drives ``autopilot_tick``).
        self._autopilot_rounds = 0
        self._autopilot_ticks = 0
        self._autopilot_live_fills = 0
        self._autopilot_last_ns = 0
        self._autopilot_paused = False
        self._autopilot_last: dict[str, Any] = {}

        # ---- Tier 1 / Tier 2 -------------------------------------------
        self.bus = bus if bus is not None else LocalJetStreamBus(self.workspace / "bus")
        self.feature_store = FeatureStore(self.workspace / "features")
        self.grounding = GroundingLedger(self.feature_store, persist=False)

        # ---- Tier 0 -----------------------------------------------------
        self.kill_latch = GlobalKillLatch(reset=True)
        self.lease_table = LeaseTableSHM(reset=True, kill_latch=self.kill_latch)
        self.ofi_ring = OfiRingBuffer(reset=True)
        self.shadow_book = ShadowBookSHM(reset=True)

        # ---- Ring 2 truth / quant / cognition ---------------------------
        self.data_guard = SelfHealingDataGuard(
            quarantine=QuarantineStore(self.workspace / "quarantine")
        )
        self.orderflow = OrderflowNormalizer(self.ofi_ring)
        self.scanner = VectorizedFactorScanner(ledger=self.grounding)
        self.scanner.warmup()
        self.contagion = MiroFishContagionEngine(
            self.feature_store, self.lease_table, silo=PRIMARY_SILO, warmup=True
        )
        self.switchboard = MultiLLMSwitchboard(default_providers())
        self.token_ledger = TokenBudgetLedger(daily_usd_cap=float(llm_budget_usd))
        self.llm_transport = LLMHttp2TransportPool(ledger=self.token_ledger)
        self.switchboard.ledger = self.token_ledger
        self.switchboard.transport_pool = self.llm_transport
        self.system1 = System1InstinctController(
            self.switchboard,
            self.lease_table,
            kill_latch=self.kill_latch,
            ledger=self.token_ledger,
            transport_pool=self.llm_transport,
        )
        self.settlement = DelayedSettlementWorker(
            self.feature_store, persist_trust=False
        )
        self.registry = registry if registry is not None else MarketSiloRegistry()
        self.committee = System2InvestmentCommittee(
            ledger=self.grounding,
            feature_store=self.feature_store,
            bus=self.bus,
            silo=PRIMARY_SILO,
            nav=float(funding_micros) / 1_000_000.0 * 4.0,
            transport_pool=self.llm_transport,
            token_ledger=self.token_ledger,
        )

        # ---- Ring 0 -----------------------------------------------------
        self.store = LedgerStore(in_memory=True)
        self.ledger = IBORLedger(self.store, silos=self.silos)
        for silo in self.silos:
            self.ledger.fund_cash(silo, int(funding_micros) * 4)
            for vault in VaultName:
                self.ledger.fund_vault(silo, vault, int(funding_micros))
        self.mirror = ConformalMirror()
        self.mirror.seed(self.strategy_id, [6.2] * int(seed_fills), shadow_fills=seed_fills)
        self.gate = SplitConformal98Gate(self.mirror)
        total_nav = sum(self.ledger.silo_nav_micros(silo) for silo in self.silos)
        self.shield = CrossSiloCopulaShield(nav_micros=total_nav)
        self.formal_verifier = FormalVerificationSuite(vopr_steps=1000, vopr_seed=7)
        self.tail_hedger = TacticalTailHedger(
            contagion_engine=self.contagion,
            copula_shield=self.shield,
            portfolio_notional_micros=total_nav,
            tactical_vault_micros=max(
                1, self.ledger.balance("US_IBKR", VaultName.TACTICAL_SWING)
            ),
        )
        self.tb_pg_provisioner: TigerBeetlePG16Provisioner | None = None
        self.outbox = CrashAtomicOutbox(self.ledger)
        self.signer = OrderSigner()
        self.guard = SiloGuard()
        self.firewall = SovereignPreTradeFirewall(
            ledger=self.ledger,
            outbox=self.outbox,
            gate=self.gate,
            shield=self.shield,
            signer=self.signer,
            guard=self.guard,
            whitelists={
                silo: [SILO_INSTRUMENTS.get(silo, PRIMARY_SYMBOL)]
                for silo in self.silos
            },
            daily_loss_stops_micros={
                silo: int(daily_loss_stop_micros) for silo in self.silos
            },
            grounding_store=self.grounding,
            require_registered_grounding=bool(require_registered_grounding),
        )
        self.ratchet = InSiloProfitRatchet(self.ledger)
        self.oms = SmartOMS(
            self.outbox,
            bus=self.bus,
            guard=self.guard,
            ledger=self.ledger,
            ratchet=self.ratchet,
        )
        self.adr009 = ADR009ReduceOnlyFastPath(
            signer=self.signer, guard=self.guard, oms=self.oms, ledger=self.ledger
        )

        # ---- Ring 1 -----------------------------------------------------
        self.twin = ShadowExecutionTwin(
            self.shadow_book,
            feature_store=self.feature_store,
            settlement_worker=self.settlement,
            required_fills=30,
        )
        self.engines: dict[str, ZeroGCSpinalEngine] = {}
        for silo in self.silos:
            self.engines[silo] = ZeroGCSpinalEngine(
                self.lease_table,
                kill_latch=self.kill_latch,
                shadow_twin=self.twin,
                bus=self.bus,
                silo=silo,
                strategy_id=self.strategy_id,
                qty=10.0,
                proposal_sink=self._on_live_proposal,
                card_sink=self._on_card,
                ring0_gate=self._ring0_gate,
            )
        self.engine = self.engines.get(PRIMARY_SILO) or next(iter(self.engines.values()))

        # ---- Ring 3 read/control model ---------------------------------
        from ring3_cockpit.gateway.control_plane import ControlPlane

        # ---- Step 8: Forge / plugins / skills --------------------------
        self.forge = NocturnalAlphaForge(
            lease_table=self.lease_table,
            shadow_twin=self.twin,
            silos=self.silos,
        )
        for strategy in self.twin.graduated_strategies():
            if self.forge.get_champion(strategy) is not None:
                continue
            record = ChampionRecord(
                strategy_id=strategy,
                silo=PRIMARY_SILO,
                symbols=(PRIMARY_SYMBOL,),
                shadow_fills=self.twin.shadow_fill_count(strategy),
                winning_shadow_fills=sum(
                    1
                    for fill in self.twin.fills(strategy)
                    if float(fill.realized_net_bps) > 0.0
                ),
            )
            for fill in self.twin.fills(strategy):
                record.record_fill(float(fill.realized_net_bps))
            self.forge.register_champion(record)
        self.plugins = PluginRegistry(forge=self.forge, shadow_twin=self.twin)
        self.skills = SkillRegistry()

        self.control = ControlPlane(
            ledger=self.ledger,
            firewall=self.firewall,
            outbox=self.outbox,
            oms=self.oms,
            adr009=self.adr009,
            registry=self.registry,
            lease_table=self.lease_table,
            kill_latch=self.kill_latch,
            mirror=self.mirror,
            feature_store=self.feature_store,
            bus=self.bus,
            engine=self.engine,
            contagion_provider=self._contagion_provider,
            grounding=self.grounding,
            guard=self.guard,
            feed_guard=self.data_guard,
            key_vault=LLMKeyVault(key_vault_root),
            broker_vault=BrokerCredentialVault(
                Path(key_vault_root) / "broker"
                if key_vault_root is not None
                else self.workspace / "broker_vault"
            ),
            llm_budget_usd=llm_budget_usd,
            strategy_id=self.strategy_id,
            scanner=self.scanner,
            shadow_twin=self.twin,
            cascade_runner=self.run_contagion,
            forge=self.forge,
            plugins=self.plugins,
            skills=self.skills,
            token_ledger=self.token_ledger,
            llm_transport=self.llm_transport,
            formal_verifier=self.formal_verifier,
            tail_hedger=self.tail_hedger,
            tb_pg_provisioner=self.tb_pg_provisioner,
            autopilot_runner=self.autopilot_tick,
            autopilot_provider=self.autopilot_snapshot,
            forge_hedge_runner=self.run_forge_and_hedge_cycle,
        )

    # -- properties -------------------------------------------------------
    @property
    def report(self) -> PipelineReport | None:
        return self._report

    def _contagion_provider(self) -> CascadeResult | None:
        return self.contagion.last_result

    # -- Ring 2 -> Tier 0/2 ----------------------------------------------
    def ingest_bar(
        self,
        bar: DualVendorBar,
        tick: ExecutedFlowTick,
        *,
        silo: str = PRIMARY_SILO,
        as_of_ns: int | None = None,
    ) -> NormalizedBar | None:
        as_of = int(
            as_of_ns
            if as_of_ns is not None
            else max(bar.valid_time_ns, bar.knowledge_ts_ns, tick.knowledge_ts_ns)
        )
        normalized = self.data_guard.ingest_bar(bar, as_of_ns=as_of)
        if normalized is None:
            return None
        self.ofi_ring.append(tick, now_ns=as_of)
        self.feature_store.append_alpha_factors(
            silo,
            tick.symbol,
            as_of,
            {
                "ofi_taker_buy_ratio": tick.taker_buy_ratio_5s,
                "spread_bps": tick.spread_bps,
                "realized_vol_5s": tick.realized_vol_5s,
                "normal_vol_5s": tick.normal_vol_5s,
                "cancel_to_fill": tick.max_cancel_to_fill_ratio,
            },
            knowledge_ts_ns=tick.knowledge_ts_ns,
            valid_time_ns=tick.valid_time_ns,
            source="ring2.data_guard",
        )
        return normalized

    def scan_universe(
        self,
        *,
        n_symbols: int = 5_000,
        n_periods: int = 64,
        top_fraction: float = 0.01,
        silo: str = PRIMARY_SILO,
        commit: bool = True,
    ) -> ScanResult:
        panel = FactorPanel.generate(n_symbols=int(n_symbols), n_periods=int(n_periods))
        top_k = max(1, int(round(int(n_symbols) * float(top_fraction))))
        return self.scanner.scan(
            panel,
            as_of_ns=time.time_ns(),
            top_k=top_k,
            commit=commit,
            silo=silo,
        )

    def run_contagion(
        self,
        *,
        price_shock_bps: float = 40.0,
        realized_vol_ratio: float = 1.4,
        spread_bps: float = 6.0,
        dealer_gamma_flip_proximity: float = 0.2,
        tags: tuple[str, ...] = (),
        symbol: str | None = None,
        now_ns: int | None = None,
    ) -> CascadeResult:
        return self.contagion.run(
            ShockVector(
                price_shock_bps=price_shock_bps,
                realized_vol_ratio=realized_vol_ratio,
                spread_bps=spread_bps,
                dealer_gamma_flip_proximity=dealer_gamma_flip_proximity,
                tags=tags,
            ),
            symbol or self.symbol,
            now_ns=now_ns,
            lease_now_ns=time.monotonic_ns(),
        )

    def _hedge_silo_for(self, symbols: Sequence[str]) -> str:
        """Pick the silo whose Ring 0 whitelist actually admits this symbol."""
        probes = {str(item).upper() for item in symbols if str(item)}
        for candidate in self.silos:
            allowed = self.firewall.whitelists.get(str(candidate))
            if allowed and probes & {str(item).upper() for item in allowed}:
                return str(candidate)
        return str(self.silos[0])

    def run_forge_and_hedge_cycle(
        self,
        *,        strategy_id: str | None = None,
        champion_fills_bps: Iterable[float] = (),
        champion_symbols: Iterable[str] = (),
        challengers: int = 4,
        price_shock_bps: float = 120.0,
        tail_correlation: float = 0.68,
        spot: float = 50_000.0,
        request_id: str = "",
    ) -> dict[str, Any]:
        """Single-process closed-loop Nightly Alpha Forge + tail-hedge cycle.

        Byte-for-byte the same ordering the Ring 2 worker executes in
        ``--multiprocess`` mode, but wired in-process: the forge audits and
        demotes decayed champions (disarming their SHM leases), the MiroFish
        cascade prices a grounded put, and Ring 0 -- here ``self.signer`` --
        verifies and Ed25519-signs it.  Ring 2 never touches the key: the
        proposal crosses a shim that only Ring 0 code can service.
        """
        import os as _os

        from core.process_supervisor import _forge_and_hedge_cycle, _sign_tail_hedge
        from ring1_spinal.silo_worker import SILO_ORDER
        from ring2_brain.mirofish.contagion_engine import ShockVector

        champion_symbols_t = tuple(str(item) for item in champion_symbols)
        command = {
            "cmd": "forge_and_hedge",
            "request_id": str(request_id or uuid.uuid4().hex[:8]),
            "strategy_id": str(strategy_id or self.strategy_id),
            "champion_fills_bps": [float(value) for value in champion_fills_bps],
            "champion_symbols": list(champion_symbols_t),
            "challengers": int(challengers),
            "price_shock_bps": float(price_shock_bps),
            "tail_correlation": float(tail_correlation),
            "spot": float(spot),
        }

        signed: list[dict[str, Any]] = []
        firewall = self.firewall
        signer = self.signer
        ledger = self.ledger

        class _Ring0SigningShim:
            """Ring 2's outbound queue, serviced by Ring 0 in this process."""

            def put(self, payload: Mapping[str, Any]) -> None:
                signed.append(
                    _sign_tail_hedge(
                        payload,
                        firewall=firewall,
                        signer=signer,
                        ledger=ledger,
                        silo_order=tuple(SILO_ORDER),
                        pid=_os.getpid(),
                    )
                )

        silo = self._hedge_silo_for(command["champion_symbols"])
        # The insurance is bought *inside* the silo that owns the vault
        # (Law 5).  Sizing against the whole book would price a premium the
        # single TacticalSwing vault can never pay, so the hedge basis is the
        # silo's own NAV and its own tactical vault balance.
        hedge_basis = {
            "portfolio_notional_micros": max(
                1, int(self.ledger.silo_nav_micros(silo) or 0)
            ),
            "tactical_vault_micros": max(
                1,
                int(self.ledger.balance(silo, VaultName.TACTICAL_SWING) or 0),
            ),
        }

        event = _forge_and_hedge_cycle(
            {"ring0_queue": _Ring0SigningShim()},
            command=command,
            leases=self.lease_table,
            silo=silo,
            symbol=command["champion_symbols"][0] if command["champion_symbols"] else self.symbol,
            strategy_id=self.strategy_id,
            toolkit={
                "forge": self.forge,
                "hedger": self.tail_hedger,
                "contagion": self.contagion,
                "shock": ShockVector,
            },
            hedge_basis=hedge_basis,
        )
        event["mode"] = "single_process"
        event["ring0_signature"] = signed[0] if signed else None
        event["signed"] = bool(signed and signed[0].get("verified"))
        return event

    def refresh_system1(
        self,
        tick: ExecutedFlowTick,
        *,
        contagion: ContagionFeatures | None = None,
        now_ns: int | None = None,
        dislocation_bps: float = 0.0,
    ) -> InstinctDecision:
        context = InstinctContext.from_executed_flow(
            tick,
            contagion=contagion,
            strategy_id=self.strategy_id,
            dislocation_bps=dislocation_bps,
        )
        return self.system1.evaluate(
            context,
            now_ns=int(now_ns if now_ns is not None else time.monotonic_ns()),
        )

    def run_committee(
        self,
        scan_result: ScanResult,
        *,
        contagion: CascadeResult | None = None,
        publish: bool = False,
    ) -> Any:
        if not scan_result.candidates:
            return None
        return self.committee.run(
            scan_result.candidates[0],
            contagion=contagion,
            spread_bps=2.0,
            realized_vol_ratio=1.0,
            publish=publish,
            q98_residual_bps=6.2,
        )

    # -- Ring 1 -----------------------------------------------------------
    def run_tick(
        self,
        tick: ExecutedFlowTick,
        *,
        silo: str = PRIMARY_SILO,
        now_ns: int | None = None,
        book: tuple[float, float] | None = None,
    ) -> TickOutcome:
        stamp = int(now_ns if now_ns is not None else time.monotonic_ns())
        if book is not None:
            self.shadow_book.write_snapshot(
                tick.symbol,
                best_bid=float(book[0]),
                best_ask=float(book[1]),
                bid_qty=50_000.0,
                ask_qty=50_000.0,
                updated_at_ns=stamp,
            )
        engine = self.engines.get(str(silo), self.engine)
        return engine.on_tick(tick, now_ns=stamp)

    # -- Ring 1 -> Ring 0 -> OMS -----------------------------------------
    def _ring0_gate(self, proposal: UnsignedProposal) -> bool:
        floor = self.control.cost_floor(proposal.silo)
        verdict = self.firewall.evaluate(
            proposal,
            now_ns=time.monotonic_ns(),
            cost_floor=floor,
            mode_badge="LIVE CAPITAL",
        )
        # Ring 1 numbers proposals per engine, so the same id can be produced by
        # two silos inside one autopilot round.  The verdict key carries the
        # silo to keep the reservations from crossing silo boundaries.
        key = (str(proposal.silo), str(proposal.proposal_id))
        if verdict.accepted:
            self._verdicts[key] = verdict
        else:
            # A rejected verdict is never consumed by the sink; drop it so a
            # long autopilot run cannot grow this map without bound.
            self._verdicts.pop(key, None)
        self._last_live_verdict = verdict
        return bool(verdict.accepted)

    def _on_live_proposal(self, proposal: UnsignedProposal) -> None:
        verdict = self._verdicts.pop(
            (str(proposal.silo), str(proposal.proposal_id)), None
        )
        if verdict is None or not verdict.accepted or verdict.reservation is None:
            return
        # One OMS order id per silo: ``proposal_id`` alone is engine-local.
        order = replace(
            proposal, proposal_id=f"{proposal.silo}:{proposal.proposal_id}"
        )
        now = time.monotonic_ns()
        price = float(proposal.limit_price)
        half_spread = price * 0.0001
        self.oms.submit_maker_peg(
            order,
            verdict.reservation,
            (price - half_spread, price + half_spread),
            now_ns=now,
        )
        realized = self._realized_net_micros(proposal)
        reservation = self.oms.on_fill(
            order.proposal_id,
            price=price,
            realized_net_micros=realized,
            now_ns=now,
        )
        self._autopilot_live_fills += 1
        self._last_committed_reservation = reservation
        self.firewall.record_realized_pnl(proposal.silo, realized)
        self._settle_live_trade(order, realized, now_ns=now)
        display = DISPLAY_SILO.get(proposal.silo, proposal.silo)
        gate = verdict.gate
        card = PlainEnglishCockpitCard(
            silo=display,
            symbol=str(proposal.symbol),
            mode_badge="LIVE CAPITAL",
            story_sentence=(
                f"Spinal Reflex bought {proposal.symbol.split('.')[0]}: aggressive "
                f"buyers took {self._last_buy_ratio * 100:.0f}% of 5s executed "
                "volume with a clean book."
            ),
            gross_bps=float(gate.gross_bps if gate is not None else proposal.expected_gross_bps),
            fee_stt_bps=float(gate.fee_stt_bps if gate is not None else 0.0),
            uncert_q98_bps=float(
                gate.effective_q98_bps if gate is not None else 0.0
            ),
            net_lb_bps=float(verdict.net_lb_bps),
            timestamp_ns=int(now),
        )
        self.control.publish_card(card)
        self._last_gate_card = {
            **card.to_dict(),
            "reservation_committed": reservation.is_committed,
            "signed": verdict.signed.to_dict() if verdict.signed else None,
            "realized_net_micros": realized,
        }

    def _realized_net_micros(self, proposal: UnsignedProposal) -> int:
        floor = self.control.cost_floor(proposal.silo)
        net_bps = (
            float(proposal.expected_gross_bps)
            - float(floor.spread_bps)
            - float(floor.fee_stt_bps)
        )
        notional_micros = (to_micros(proposal.qty) * to_micros(proposal.limit_price)) // 1_000_000
        return int(round(notional_micros * net_bps / 10_000.0))

    def _settle_live_trade(
        self, proposal: UnsignedProposal, realized_micros: int, *, now_ns: int
    ) -> None:
        notional_micros = (to_micros(proposal.qty) * to_micros(proposal.limit_price)) // 1_000_000
        realized_bps = (
            realized_micros / float(notional_micros) * 10_000.0
            if notional_micros
            else 0.0
        )
        predicted = float(proposal.expected_gross_bps) - float(
            self.control.cost_floor(proposal.silo).fee_stt_bps
        )
        trade_id = f"live:{proposal.proposal_id}"
        try:
            self.settlement.record_prediction(
                trade_id,
                strategy_id=proposal.strategy_id,
                symbol=proposal.symbol,
                predicted_net_bps=predicted,
                horizon_ns=0,
                created_at_ns=now_ns,
                grounding_hash=proposal.grounding_hash,
                silo=proposal.silo,
                is_shadow=False,
            )
            self.settlement.settle_prediction(
                trade_id, realized_bps, now_ns=now_ns, is_shadow=False
            )
        except Exception:  # pragma: no cover - settlement telemetry is non-fatal
            pass
        try:
            self.mirror.record(
                proposal.strategy_id,
                abs(predicted - realized_bps),
                is_shadow=False,
                now_ns=time.time_ns(),
            )
        except Exception:  # pragma: no cover - conformal telemetry is non-fatal
            pass

    def _on_card(self, card: PlainEnglishCockpitCard) -> None:
        self._cards.append(card.to_dict())

    # -- paper-shadow autopilot (Ring 3 lifespan) -------------------------
    def autopilot_snapshot(self) -> dict[str, Any]:
        """Live autopilot telemetry for ``GET /api/v2/cockpit/state``."""
        twin = self.twin
        return {
            "enabled": True,
            "mode": "PAPER",
            "interval_ms": int(AUTOPILOT_INTERVAL_S * 1000),
            "instruments": dict(SILO_INSTRUMENTS),
            "rounds": int(self._autopilot_rounds),
            "ticks": int(self._autopilot_ticks),
            "live_fills": int(self._autopilot_live_fills),
            "shadow_fills": int(twin.shadow_fill_count(self.strategy_id)),
            "graduated": bool(twin.is_graduated(self.strategy_id)),
            "paused": bool(self._autopilot_paused),
            "last_tick_ns": int(self._autopilot_last_ns),
            "last": dict(self._autopilot_last),
        }

    def autopilot_tick(
        self,
        *,
        now_ns: int | None = None,
        silos: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Feed one realistic dual-vendor round across all four silos.

        Each round is the full constitutional path: dual-vendor bar ->
        ``SelfHealingDataGuard`` -> Tier-2 factors -> MiroFish contagion ->
        System 1 60 s lease -> Tier-0 spinal engine -> 30-fill shadow
        graduation -> Ring 0 conformal/firewall -> Maker-Peg paper fill ->
        in-silo 35/15 ratchet -> Ring 3 proof card.

        The loop is *paused* the instant the ``GlobalKillLatch`` is hot (the
        red ``<1ms FLATTEN`` button) or a silo is frozen, and resumes only when
        the operator re-arms the system (``POST /api/v2/control/rearm``).
        """
        stamp_wall = int(now_ns if now_ns is not None else time.time_ns())
        round_no = self._autopilot_rounds
        if self.kill_latch.is_halted or bool(self.guard.frozen_silos):
            self._autopilot_paused = True
            payload = {
                "status": "HALTED",
                "paused": True,
                "round": round_no,
                "ticks": 0,
                "total_ticks": int(self._autopilot_ticks),
                "actions": {},
                "shadow_fills": int(self.twin.shadow_fill_count(self.strategy_id)),
                "live_fills": int(self._autopilot_live_fills),
                "graduated": bool(self.twin.is_graduated(self.strategy_id)),
                "timestamp_ns": stamp_wall,
            }
            self._autopilot_last = payload
            self._autopilot_last_ns = stamp_wall
            return payload

        self._autopilot_paused = False
        targets = tuple(str(s) for s in (silos if silos is not None else self.silos))
        stamp_mono = time.monotonic_ns()

        # ``sec_018`` Law 3: refresh the five-archetype cascade on a cadence and
        # hand the freshest fragility to System 1 before any lease is armed.
        if round_no % AUTOPILOT_CONTAGION_EVERY == 0:
            self.run_contagion(symbol=self.symbol, now_ns=stamp_wall)
        last_cascade = self.contagion.last_result
        features = last_cascade.features if last_cascade is not None else None

        actions: dict[str, str] = {}
        prices: dict[str, float] = {}
        for index, silo in enumerate(targets):
            symbol = SILO_INSTRUMENTS.get(silo, PRIMARY_SYMBOL)
            reference = SILO_REFERENCE_PRICES.get(silo, 100.0)
            wave = math.sin((round_no + index) * 0.35)
            price = reference * (1.0 + 0.0008 * wave)
            prices[symbol] = round(price, 6)
            half_spread = price * 0.00005
            # Two thirds of the tape carries an executed-flow edge; the rest is
            # balanced noise that must abstain (``sec_018`` Law 4).  When the
            # tape prints at the touch the maker BUY peg is filled exactly as a
            # live L2 would fill it.
            active = (round_no + index) % 3 != 0
            bar = make_dual_vendor_bar(symbol, price=price, now_ns=stamp_wall)
            tick = make_flow_tick(
                symbol,
                last_price=(price - half_spread) if active else price,
                now_ns=stamp_wall,
                buy_volume=(150.0 + 40.0 * wave) if active else 120.0,
                sell_volume=3.0 if active else 120.0,
                spread_bps=1.2,
                realized_vol=5.0,
                normal_vol=10.0,
            )
            # ``sec_014`` tax-net cost floor: the CRYPTO silo pays 16.0 bps round
            # trip (2 x 7.5 bps maker + 1.0 bps spread), so a 20 bps deterministic
            # ceiling can never clear the gate with residual headroom.  A genuine
            # cross-venue dislocation (24-30 bps of gross edge on a post-only
            # maker peg) is injected on a cadence instead of pretending the
            # microstructure edge is larger than it is.
            dislocation_bps = 0.0
            if str(silo) == "CRYPTO_BINANCE" and round_no % AUTOPILOT_DISLOCATION_EVERY == 2:
                dislocation_bps = AUTOPILOT_DISLOCATION_BASE_BPS + 2.0 * wave
            self.ingest_bar(bar, tick, silo=silo, as_of_ns=stamp_wall)
            self.refresh_system1(
                tick,
                contagion=features,
                now_ns=stamp_mono,
                dislocation_bps=dislocation_bps,
            )
            self._last_buy_ratio = tick.taker_buy_ratio_5s
            self.shadow_book.write_snapshot(
                symbol,
                best_bid=price - half_spread,
                best_ask=price + half_spread,
                bid_qty=50_000.0,
                ask_qty=50_000.0,
                updated_at_ns=stamp_mono,
            )
            engine = self.engines.get(silo)
            if engine is None:
                continue
            outcome = engine.on_tick(tick, now_ns=stamp_mono)
            actions[silo] = str(outcome.action)
            self._autopilot_ticks += 1

        self._autopilot_rounds = round_no + 1
        self._autopilot_last_ns = stamp_wall
        payload = {
            "status": "ARMED",
            "paused": False,
            "round": self._autopilot_rounds,
            "ticks": len(actions),
            "total_ticks": int(self._autopilot_ticks),
            "actions": actions,
            "prices": prices,
            "shadow_fills": int(self.twin.shadow_fill_count(self.strategy_id)),
            "live_fills": int(self._autopilot_live_fills),
            "graduated": bool(self.twin.is_graduated(self.strategy_id)),
            "cascade_fragility_index": float(
                features.cascade_fragility_index if features is not None else 0.0
            ),
            "timestamp_ns": stamp_wall,
        }
        self._autopilot_last = payload
        return payload

    # -- full end-to-end lifecycle ---------------------------------------
    def run_end_to_end(
        self,
        *,
        symbol: str = PRIMARY_SYMBOL,
        silo: str = PRIMARY_SILO,
        warmup: int = 30,
        start_ns: int | None = None,
        run_scan: bool = True,
        scan_symbols: int = 5_000,
    ) -> PipelineReport:
        """Raw ticks -> 30 shadow fills -> trade #31 -> Ring 3 proof card."""
        started = time.perf_counter()
        now = int(start_ns if start_ns is not None else time.time_ns())
        mono = time.monotonic_ns()
        self._last_gate_card = None
        self._last_live_verdict = None
        self._last_committed_reservation = None
        vaults_before = {
            vault.value: int(self.ledger.balance(silo, vault.value))
            for vault in VaultName
        }

        scan_payload: dict[str, Any] = {}
        if run_scan:
            scan_result = self.scan_universe(n_symbols=int(scan_symbols), silo=silo)
            scan_payload = {
                "universe": scan_result.universe_size,
                "candidates": len(scan_result.candidates),
                "compression_ratio": scan_result.compression_ratio,
                "latency_ms": scan_result.elapsed_ms,
            }

        contagion = self.run_contagion(symbol=symbol, now_ns=now)

        # 30 live-L2 shadow fills, each preceded by a fresh System 1 lease.
        ticks = 0
        for index in range(int(warmup)):
            stamp_wall = now + index * 1_000_000_000
            stamp_mono = mono + index * 1_000_000_000
            tick = make_flow_tick(symbol, now_ns=stamp_wall)
            bar = make_dual_vendor_bar(symbol, now_ns=stamp_wall)
            self.ingest_bar(bar, tick, silo=silo, as_of_ns=stamp_wall)
            self.refresh_system1(tick, contagion=contagion.features, now_ns=stamp_mono)
            self._last_buy_ratio = tick.taker_buy_ratio_5s
            self.shadow_book.write_snapshot(
                symbol,
                best_bid=99.99,
                best_ask=100.01,
                bid_qty=50_000.0,
                ask_qty=50_000.0,
                updated_at_ns=stamp_mono,
            )
            self.engines[silo].on_tick(tick, now_ns=stamp_mono)
            ticks += 1

        # Trade #31: the strategy is now graduated; the same tape must reach
        # Ring 0 through the full firewall and settle through the IBOR.
        live_wall = now + int(warmup) * 1_000_000_000
        live_mono = mono + int(warmup) * 1_000_000_000
        tick = make_flow_tick(symbol, now_ns=live_wall)
        bar = make_dual_vendor_bar(symbol, now_ns=live_wall)
        self.ingest_bar(bar, tick, silo=silo, as_of_ns=live_wall)
        decision = self.refresh_system1(
            tick, contagion=contagion.features, now_ns=live_mono
        )
        self._last_buy_ratio = tick.taker_buy_ratio_5s
        self.shadow_book.write_snapshot(
            symbol,
            best_bid=99.99,
            best_ask=100.01,
            bid_qty=50_000.0,
            ask_qty=50_000.0,
            updated_at_ns=live_mono,
        )
        live = self.engines[silo].on_tick(tick, now_ns=live_mono)
        ticks += 1

        thesis_payload: dict[str, Any] = {}
        if run_scan and scan_payload:
            try:
                thesis = self.run_committee(
                    self.scan_universe(n_symbols=int(scan_symbols), silo=silo),
                    contagion=contagion,
                    publish=False,
                )
                if thesis is not None:
                    thesis_payload = (
                        thesis.to_dict() if hasattr(thesis, "to_dict") else {"ok": True}
                    )
            except Exception as exc:  # pragma: no cover - optional cognition
                thesis_payload = {"error": f"{type(exc).__name__}: {exc}"}

        gate_card = self._last_gate_card or {}
        verdict = self._last_live_verdict
        vaults_after = {
            vault.value: int(self.ledger.balance(silo, vault.value))
            for vault in VaultName
        }
        cards = self.control.recent_cards(limit=50)
        live_cards = [
            card for card in cards if card.get("mode_badge") == "LIVE CAPITAL"
        ][-5:]
        report = PipelineReport(
            symbol=symbol,
            silo=silo,
            shadow_fills=self.twin.shadow_fill_count(self.strategy_id),
            ticks=ticks,
            live_outcome=live.action.value,
            firewall_accepted=bool(verdict is not None and verdict.accepted),
            firewall_reason=(
                verdict.reason
                if verdict is not None
                else "ABSTAIN: no Ring 0 verdict was produced"
            ),
            net_lb_bps=float(verdict.net_lb_bps if verdict is not None else 0.0),
            signed=bool(verdict is not None and verdict.signed is not None),
            reservation_committed=bool(
                self._last_committed_reservation is not None
                and self._last_committed_reservation.is_committed
            ),
            ratchet={
                vault.value: int(vaults_after.get(vault.value, 0))
                - int(vaults_before.get(vault.value, 0))
                for vault in VaultName
            },
            realized_net_micros=int(gate_card.get("realized_net_micros", 0)),
            cards=live_cards,
            vaults_before=vaults_before,
            vaults_after=vaults_after,
            contagion=contagion.to_dict(),
            scan=scan_payload,
            thesis=thesis_payload,
            elapsed_ms=(time.perf_counter() - started) * 1e3,
        )
        assert live is not None
        report.live_outcome = live.action.value
        report.ticks = ticks
        report.shadow_fills = self.twin.shadow_fill_count(self.strategy_id)
        # A tiny, honest cross-check: the lease that armed trade #31.
        report.thesis = {**thesis_payload, "system1_mode": decision.mode.name}
        self._report = report
        return report

    # -- lifecycle --------------------------------------------------------
    def control_app(self):
        """Return the FastAPI app wired to this orchestrator."""
        from ring3_cockpit.gateway.control_plane import create_app

        return create_app(self.control)

    def trigger_global_kill_switch(self, client: Any = None) -> dict[str, Any]:
        """Measure one global halt/re-arm pair through the Ring 3 control plane.

        Gate 22 certifies the ``< 1,000,000 ns`` hardware latch budget, so the
        caller must never be charged for one-time ``TestClient``/anyio/Pydantic
        setup.  A discarded warm-up halt/re-arm (which leaves the platform in
        the same ``ARMED`` state the timed pass starts from) runs first; only
        the second halt is timed.  Pass an existing client to reuse it, or let
        this build a fresh in-process one.
        """
        if client is None:
            from fastapi.testclient import TestClient

            client = TestClient(self.control_app())
        # Un-timed warm-up: route resolution, threadpool, model construction.
        client.get("/api/v2/cockpit/health")
        client.post("/api/v2/control/killswitch", json={"confirm": True})
        client.post("/api/v2/control/rearm", json={"confirm": True})
        # Timed pass: the hardware preemption cascade only.
        halted = client.post("/api/v2/control/killswitch", json={"confirm": True})
        halt_body = halted.json() if halted.status_code == 200 else {}
        halted_state = client.get("/api/v2/cockpit/health")
        rearmed = client.post("/api/v2/control/rearm", json={"confirm": True})
        rearm_body = rearmed.json() if rearmed.status_code == 200 else {}
        final_state = client.get("/api/v2/cockpit/health")
        final_body = final_state.json() if final_state.status_code == 200 else {}
        return {
            "halt_status": int(halted.status_code),
            "latency_ns": int(halt_body.get("latency_ns", 0) or 0),
            "latency_ms": float(halt_body.get("latency_ms", 0.0) or 0.0),
            "halted_state": str(
                (halted_state.json() if halted_state.status_code == 200 else {}).get(
                    "status", ""
                )
            ),
            "rearm_status": int(rearmed.status_code),
            "rearm_state": str(rearm_body.get("status", "")),
            "final_state": str(final_body.get("status", "")),
            "warmup_passes": 1,
            "orders_placed": 0,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource in (
            self.lease_table,
            self.kill_latch,
            self.ofi_ring,
            self.shadow_book,
        ):
            try:
                resource.close()
            except Exception:
                pass
        for resource in (self.feature_store, self.store, self.bus):
            try:
                resource.close()
            except Exception:
                pass
        try:
            self.llm_transport.close()
        except Exception:
            pass
        try:
            self.data_guard.quarantine.close()
        except Exception:
            pass

    def __enter__(self) -> "NIOSCoreOrchestrator":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass
