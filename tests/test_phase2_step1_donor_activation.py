"""Phase 2 Step 1 -- harvested donor activation gates.

Every gate asserts observable behaviour, not a wish: the staging tree is gone,
the donor valuation/grounding/factor modules import *and execute* under their
upstream names, all 48 Trading-R1 compute modules import with zero
``ImportError``, the TradingAgents truth/graph and MiroFish services import, and
the two Ring 2 bridges report 100% donor coverage without breaching the
air-gap.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _factor_panel(periods: int = 320) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(20261109)
    index = pd.date_range("2025-01-02", periods=periods, freq="B")
    columns = [f"SYM{index:02d}" for index in range(6)]
    returns = rng.normal(0.0003, 0.011, size=(periods, len(columns)))
    close = pd.DataFrame(
        100.0 * np.exp(np.cumsum(returns, axis=0)), index=index, columns=columns
    )
    volume = pd.DataFrame(
        rng.lognormal(13.0, 0.25, size=close.shape), index=index, columns=columns
    )
    return {
        "open": close * 1.001,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": volume,
        "amount": close * volume,
        "vwap": (close * 1.001 + close * 1.01 + close * 0.99 + close) / 4.0,
        "fund:accruals": pd.DataFrame(
            rng.normal(0.0, 0.04, size=close.shape), index=index, columns=columns
        ),
    }


# ---------------------------------------------------------------------------
# GATE 1 -- staging purge
# ---------------------------------------------------------------------------


def test_gate1_donor_staging_is_purged_and_ignored() -> None:
    assert not (ROOT / "_donor_staging").exists(), (
        "_donor_staging/ must be deleted after donor activation"
    )
    for name in (".gitignore", ".dockerignore"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "_donor_staging" in text, f"{name} must ignore _donor_staging/"


# ---------------------------------------------------------------------------
# GATE 2 -- Vibe-Trading valuation, grounding and 478-factor zoo
# ---------------------------------------------------------------------------


def test_gate2_vibe_trading_valuation_grounding_and_factors_execute() -> None:
    from ring2_brain.quant.vibe_trading.quantlib import fixedincome, impact
    from ring2_brain.quant.vibe_trading.quantlib.valuation import comps, dcf, threestatement
    from ring2_brain.truth.vibe_trading.grounding import figures, ledger

    # -- valuation: WACC, FCFF bridge, comps EV bridge, 3-statement ----------
    wacc = dcf.wacc(
        risk_free_rate=0.04,
        beta=1.1,
        equity_risk_premium=0.05,
        pretax_cost_of_debt=0.06,
        tax_rate=0.21,
        capital_structure_basis="current",
        market_value_of_equity=1_000.0,
        market_value_of_debt=400.0,
    )
    assert 0.0 < wacc.wacc < 0.20
    fcff = dcf.fcff_bridge(
        ebit=[100.0, 110.0],
        tax_rate=0.21,
        depreciation_amortization=[10.0, 11.0],
        capex=[20.0, 22.0],
        delta_nwc=[5.0, 5.0],
    )
    assert fcff[-1].fcff == pytest.approx(70.9)

    bridge = comps.enterprise_value(
        market_cap=1_000.0, total_debt=400.0, cash_and_equivalents=100.0
    )
    assert bridge.enterprise_value == pytest.approx(1_300.0)

    opening = {
        "revenue": 1_000.0,
        "cash": 100.0,
        "net_working_capital": 50.0,
        "ppe": 300.0,
        "revolver_balance": 0.0,
        "paid_in_capital": 200.0,
        "retained_earnings": 250.0,
    }
    driver_values = {
        "revenue_growth": 0.05,
        "gross_margin": 0.40,
        "opex_pct_revenue": 0.20,
        "capex_pct_revenue": 0.06,
        "nwc_pct_revenue": 0.10,
        "tax_rate": 0.21,
        "dividend_payout_ratio": 0.30,
        "depreciation_amortization": 20.0,
        "interest_rate": 0.05,
        "minimum_cash": 50.0,
    }
    drivers = {name: [value] * 3 for name, value in driver_values.items()}
    projection = threestatement.project_three_statement(opening, drivers)
    assert len(projection.periods) == 3

    # -- impact / fixed income ----------------------------------------------
    impacted = impact.sqrt_impact(
        price=100.0,
        direction=1,
        volume_traded=10_000.0,
        adv=1_000_000.0,
        volatility=0.02,
        eta=0.5,
    )
    assert impacted > 100.0
    price = fixedincome.bond_price(
        face=1_000.0, coupon_rate=0.05, ytm=0.045, n_periods=10, freq=2
    )
    assert 900.0 < price < 1_100.0

    # -- grounding ledger/figures -------------------------------------------
    assert ledger.GroundingLedger is not None
    assert figures.FiguresBlock is not None

    # -- 478-module factor zoo: one module from every family executes -------
    from ring2_brain.quant.vibe_trading.factors.zoo.academic import bab
    from ring2_brain.quant.vibe_trading.factors.zoo.alpha101 import alpha_001
    from ring2_brain.quant.vibe_trading.factors.zoo.fundamental import accruals
    from ring2_brain.quant.vibe_trading.factors.zoo.gtja191 import alpha_001 as gtja_001
    from ring2_brain.quant.vibe_trading.factors.zoo.qlib158 import beta10

    panel = _factor_panel()
    for module, minimum in (
        (alpha_001, 100),
        (gtja_001, 100),
        (beta10, 100),
        (accruals, 100),
        (bab, 1),
    ):
        output = module.compute(panel)
        assert isinstance(output, pd.DataFrame)
        assert output.shape == panel["close"].shape
        assert int(output.notna().sum().sum()) >= minimum, module.__name__

    # -- the two bridges report full activation -----------------------------
    from ring2_brain.quant.scanner import ValuationLab
    from ring2_brain.truth.grounding_gate import donor_grounding_status

    status = ValuationLab().donor_status()
    assert status["percent"] == 100.0, status
    assert status["active"] == status["total"] == 11

    grounding = donor_grounding_status()
    assert grounding["percent"] == 100.0, grounding
    assert grounding["available"] is True
    assert grounding["active"] == grounding["total"] == 2


# ---------------------------------------------------------------------------
# GATE 3 -- Trading-R1 compute + verification
# ---------------------------------------------------------------------------


def test_gate3_trading_r1_compute_and_verification_import_cleanly() -> None:
    compute_root = ROOT / "ring2_brain" / "quant" / "trading_r1" / "compute"
    verification_root = ROOT / "ring2_brain" / "truth" / "trading_r1" / "verification"

    modules = sorted(path for path in compute_root.rglob("*.py"))
    assert len(modules) == 48, f"expected 48 compute modules, found {len(modules)}"
    modules += sorted(verification_root.glob("*.py"))
    assert len(modules) == 51

    failures: list[tuple[str, str]] = []
    for path in modules:
        name = _module_name(path)
        try:
            importlib.import_module(name)
        except ImportError as exc:  # the gate is specifically about ImportError
            failures.append((name, f"{type(exc).__name__}: {exc}"))
        except Exception as exc:  # a non-import failure is still a failure
            failures.append((name, f"{type(exc).__name__}: {exc}"))
    assert not failures, failures

    from ring2_brain.quant.trading_r1.compute import COMPUTE_SPECS

    assert len(COMPUTE_SPECS) >= 30
    assert all(spec.result_kind == "compute" for spec in COMPUTE_SPECS)


# ---------------------------------------------------------------------------
# GATE 4 -- TradingAgents truth/committee + MiroFish services
# ---------------------------------------------------------------------------


def test_gate4_tradingagents_and_mirofish_import_within_air_gap() -> None:
    modules = [
        "ring2_brain.truth.tradingagents.sec_edgar",
        "ring2_brain.truth.tradingagents.router",
        "ring2_brain.truth.tradingagents.settlement",
        "ring2_brain.cognition.tradingagents.graph",
        "ring2_brain.mirofish.services.ontology_generator",
        "ring2_brain.mirofish.services.local_graph",
    ]
    for name in modules:
        importlib.import_module(name)

    from ring2_brain.truth.data_guard import SecEdgarPITGuard, VendorRouterBridge
    from ring2_brain.truth.settlement_worker import DelayedSettlementWorker, donor_settlement_status
    from ring2_brain.cognition.committee_graph import (
        System2InvestmentCommittee,
        donor_committee_status,
    )
    from ring2_brain.cognition.tradingagents.graph import TradingAgentsGraph
    from ring2_brain.mirofish.services.ontology_generator import OntologyGenerator

    guard = SecEdgarPITGuard()
    assert guard.donor["available"] is True, guard.donor
    router = VendorRouterBridge()
    assert router.available is True, router.reason

    settlement = donor_settlement_status()
    assert settlement["importable"] is True, settlement
    assert DelayedSettlementWorker is not None

    committee = donor_committee_status()
    assert committee["available"] is True, committee
    assert System2InvestmentCommittee is not None
    assert TradingAgentsGraph is not None
    assert OntologyGenerator is not None

    forbidden = {
        "ring0_vault",
        "nautilus_trader",
        "tigerbeetle",
        "key_vault",
        "ib_insync",
        "ccxt",
        "socket",
        "websocket",
    }
    for name in modules:
        path = ROOT / (name.replace(".", "/") + ".py")
        if not path.exists():
            path = ROOT / name.replace(".", "/") / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for target in imported:
                root = target.split(".")[0]
                assert root not in forbidden, f"{name} imports {target!r}"


# ---------------------------------------------------------------------------
# GATE 5 -- air-gap sentinel and 490-factor contract
# ---------------------------------------------------------------------------


def test_gate5_ring2_bridges_stay_air_gapped_and_factor_count_holds() -> None:
    forbidden = {
        "ring0_vault",
        "nautilus_trader",
        "tigerbeetle",
        "key_vault",
        "ib_insync",
        "ccxt",
        "socket",
        "websocket",
        "requests",
        "httpx",
    }
    for relative in (
        "ring2_brain/quant/scanner.py",
        "ring2_brain/truth/grounding_gate.py",
        "ring2_brain/_donor_compat.py",
    ):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for target in imported:
                assert target.split(".")[0] not in forbidden, (
                    f"{relative} imports forbidden surface {target!r}"
                )

    from ring2_brain.quant.scanner import MIN_FACTOR_COUNT, FactorZooCatalog

    zoo = FactorZooCatalog()
    assert zoo.donor_factor_count == 478
    assert zoo.factor_count >= MIN_FACTOR_COUNT
    assert zoo.families["alpha101"] == 101
    assert zoo.families["gtja191"] == 191
    assert zoo.families["qlib158"] == 154
    assert zoo.families["fundamental"] == 20
    assert zoo.families["academic"] == 12
