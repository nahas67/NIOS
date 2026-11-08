"""Step 3 verification gates -- truth layer and 480+ factor/valuation scanner.

Gate 1  Self-Healing DataGuard (lag / divergence / split / PIT / corporate
        actions / PIT FX / 5-second executed-orderflow fast path)
Gate 2  SEC EDGAR knowledge-time anti-lookahead guard
Gate 3  SHA-256 numerical grounding and AST re-computation gate
Gate 4  5,000-ticker 490-factor scan SLA, 99% funnel compression and packet
        persistence
Gate 5  Full regression sentinel and four-ring air-gap AST check

Run with::

    python -m pytest tests/test_step3_truth_and_quant.py -q
"""

from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.feature_store import FeatureStore
from core.schemas import (
    LEASE_TTL_NS,
    DualVendorBar,
    GroundedFactPacket,
    LeaseMode,
    System1Lease,
    TemporalIntegrityError,
    UngroundedNumericalClaim,
)
from core.shm_ipc import (
    GlobalKillLatch,
    HaltReason,
    LeaseTableSHM,
    OfiRingBuffer,
    unlink_all,
)
from ring2_brain.quant.scanner import (
    MIN_FACTOR_COUNT,
    FactorPanel,
    FactorZooCatalog,
    ValuationLab,
    VectorizedFactorScanner,
    black_scholes,
    deflated_sharpe_ratio,
)
from ring2_brain.truth.data_guard import (
    CorporateAction,
    FilingRecord,
    FxBook,
    FxPoint,
    OrderflowNormalizer,
    QuarantineReason,
    QuarantineStore,
    SecEdgarPITGuard,
    SelfHealingDataGuard,
    iso_to_ns,
)
from ring2_brain.truth.grounding_gate import (
    UNGROUNDED_CLAIM_CODE,
    GroundingLedger,
    NumericalGroundingVerifier,
    UngroundedNumericalClaimError,
    verify_thesis_claims,
)

BASE_NS = 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# Gate 1 -- Self-Healing DataGuard
# ---------------------------------------------------------------------------


def test_gate1_feed_lag_gate_drops_stale_bars(tmp_path: Path) -> None:
    store = QuarantineStore(tmp_path / "quarantine")
    guard = SelfHealingDataGuard(store)

    stale = DualVendorBar("BTCUSDT", 100.0, 100.0, 6_000.0, BASE_NS, BASE_NS)
    assert guard.ingest_bar(stale, as_of_ns=BASE_NS) is None
    assert store.count(QuarantineReason.FEED_LAG_EXCEEDED) == 1
    assert guard.stats.lag_rejected == 1

    rows = store.query(reason=QuarantineReason.FEED_LAG_EXCEEDED)
    assert rows and rows[0]["reason"] == "FEED_LAG_EXCEEDED"
    assert rows[0]["feed_lag_ms"] == pytest.approx(6_000.0)
    assert (tmp_path / "quarantine" / "quarantine.jsonl").exists()
    store.close()


def test_gate1_cross_vendor_divergence_gate_quarantines(tmp_path: Path) -> None:
    store = QuarantineStore(tmp_path / "quarantine")
    guard = SelfHealingDataGuard(store)

    # 1.60% divergence on a ~100 mid -> above the 1.50% gate.
    divergent = DualVendorBar("ETHUSDT", 100.0, 101.6, 12.0, BASE_NS, BASE_NS)
    assert divergent.vendor_divergence_pct > 1.50
    assert guard.ingest_bar(divergent, as_of_ns=BASE_NS) is None
    assert store.count(QuarantineReason.CROSS_VENDOR_DIVERGENCE) == 1

    # 1.40% is inside tolerance and must survive.
    clean = DualVendorBar("ETHUSDT", 100.0, 101.4, 12.0, BASE_NS, BASE_NS)
    assert clean.vendor_divergence_pct < 1.50
    assert guard.ingest_bar(clean, as_of_ns=BASE_NS) is not None
    store.close()


def test_gate1_phantom_split_guard_blocks_45pct_single_feed_gap(tmp_path: Path) -> None:
    store = QuarantineStore(tmp_path / "quarantine")
    guard = SelfHealingDataGuard(store)

    phantom = DualVendorBar("RELIANCE", 100.0, 54.0, 8.0, BASE_NS, BASE_NS)
    assert phantom.split_gap_pct >= 45.0
    assert guard.ingest_bar(phantom, as_of_ns=BASE_NS) is None
    assert store.count(QuarantineReason.UNANNOUNCED_SPLIT_GAP) == 1

    # A 44% gap is below the split guard, so it is diagnosed as divergence.
    below_split = DualVendorBar("RELIANCE", 100.0, 56.0, 8.0, BASE_NS, BASE_NS)
    assert below_split.split_gap_pct < 45.0
    assert guard.ingest_bar(below_split, as_of_ns=BASE_NS) is None
    assert store.count(QuarantineReason.CROSS_VENDOR_DIVERGENCE) == 1

    # A genuine announced split is normalized instead of blocked.
    guard.corporate_actions.register(
        CorporateAction(
            symbol="RELIANCE",
            kind="SPLIT",
            effective_ns=BASE_NS,
            knowledge_ts_ns=BASE_NS - 86_400 * 1_000_000_000,
            ratio=2.0,
        )
    )
    post_split = DualVendorBar("reliance", 50.0, 50.2, 5.0, BASE_NS + 1, BASE_NS + 1)
    normalized = guard.ingest_bar(post_split, as_of_ns=BASE_NS + 1)
    assert normalized is not None
    assert normalized.symbol == "RELIANCE"
    assert normalized.adjustment_factor == pytest.approx(1.0)
    store.close()


def test_gate1_pit_violation_is_quarantined_and_raised(tmp_path: Path) -> None:
    store = QuarantineStore(tmp_path / "quarantine")
    guard = SelfHealingDataGuard(store)

    future = DualVendorBar(
        "BTCUSDT",
        100.0,
        100.1,
        5.0,
        valid_time_ns=BASE_NS + 10_000_000_000,
        knowledge_ts_ns=BASE_NS + 10_000_000_000,
    )
    with pytest.raises(TemporalIntegrityError) as excinfo:
        guard.ingest_bar(future, as_of_ns=BASE_NS)
    assert "TEMPORAL_LOOKAHEAD" in str(excinfo.value)
    assert store.count(QuarantineReason.TEMPORAL_LOOKAHEAD) == 1
    store.close()


def test_gate1_clean_bar_normalizes_security_master_fx_and_dividend(
    tmp_path: Path,
) -> None:
    store = QuarantineStore(tmp_path / "quarantine")
    guard = SelfHealingDataGuard(store)
    guard.security_master.register("RELIANCE.NS", "RELIANCE", "RIL", "NSE:RELIANCE")
    guard.corporate_actions.register(
        CorporateAction(
            symbol="RELIANCE.NS",
            kind="DIVIDEND",
            effective_ns=BASE_NS,
            knowledge_ts_ns=BASE_NS - 1_000,
            amount=5.0,
        )
    )
    guard.fx.register(FxPoint("INR", "USD", 0.012, BASE_NS))

    bar = DualVendorBar("nse:reliance", 2_900.0, 2_902.0, 15.0, BASE_NS, BASE_NS - 1)
    normalized = guard.ingest_bar(bar, as_of_ns=BASE_NS, currency="INR")
    assert normalized is not None
    assert normalized.symbol == "RELIANCE.NS"
    assert normalized.adjustment_factor == pytest.approx(1.0)
    assert normalized.dividend == pytest.approx(5.0)
    assert normalized.fx_rate == pytest.approx(0.012)
    assert normalized.adjusted_price == pytest.approx((2_901.0 - 5.0) * 0.012)
    assert len(normalized.sha256_digest) == 64
    assert guard.stats.accepted == 1
    store.close()


def test_gate1_pit_fx_never_uses_a_future_rate() -> None:
    fx = FxBook()
    fx.register(FxPoint("INR", "USD", 0.010, BASE_NS))
    fx.register(FxPoint("INR", "USD", 0.020, BASE_NS + 86_400 * 1_000_000_000))
    assert fx.rate("INR", "USD", BASE_NS) == pytest.approx(0.010)
    assert fx.rate("INR", "USD", BASE_NS + 2 * 86_400 * 1_000_000_000) == pytest.approx(0.020)
    assert fx.rate("USD", "INR", BASE_NS) == pytest.approx(100.0)


def test_gate1_executed_orderflow_frames_dispatch_under_50us() -> None:
    ring = OfiRingBuffer(reset=True, n_slots=4_096)
    normalizer = OrderflowNormalizer(ring)
    try:
        # Two symbols, 250 executed prints each, spread across the 5s window.
        for index in range(250):
            stamp = BASE_NS + index * 2_000_000
            for symbol, base in (("BTC-USD", 100.0), ("ETH-USD", 50.0)):
                direction = "BUY" if index % 3 else "SELL"
                normalizer.ingest_trade(symbol, base + index * 0.001, 1.5, direction, stamp)
                normalizer.ingest_book(
                    symbol,
                    base - 0.01,
                    base + 0.01,
                    bid_qty=10.0,
                    ask_qty=12.0,
                    bid_cancels=0.5,
                    ask_cancels=0.5,
                    ts_ns=stamp,
                )

        # Warm the ring path, then measure the real flush.
        normalizer.flush(BASE_NS + 500_000_000)
        normalizer.reset_latencies()
        report = normalizer.flush(BASE_NS + 500_000_000)

        assert report.dispatched >= 2
        assert report.p99_ms < 0.05, f"OFI dispatch p99={report.p99_ms:.4f} ms > 0.05 ms"

        latest = ring.latest()
        assert latest is not None
        assert latest.symbol in {"BTC-USD", "ETH-USD"}
        assert 0.0 <= latest.taker_buy_ratio_5s <= 1.0
        assert latest.max_cancel_to_fill_ratio < 3.0
    finally:
        ring.close()
        ring.unlink()


# ---------------------------------------------------------------------------
# Gate 2 -- SEC EDGAR knowledge-time anti-lookahead
# ---------------------------------------------------------------------------


def _q4_filing_guard() -> SecEdgarPITGuard:
    guard = SecEdgarPITGuard()
    guard.register_filing(
        FilingRecord(
            ticker="ACME",
            form="10-K",
            period_end="2025-12-31",
            acceptance_date="2026-03-15",
            facts={"revenue": 1_000.0, "net_income": 120.0, "total_assets": 2_400.0},
        )
    )
    return guard


def test_gate2_sec_edgar_rejects_january_lookahead() -> None:
    guard = _q4_filing_guard()
    january = iso_to_ns("2026-01-20")

    with pytest.raises(TemporalIntegrityError) as excinfo:
        guard.statement_as_of("ACME", january, period_end="2025-12-31")
    message = str(excinfo.value)
    assert "TEMPORAL_LOOKAHEAD" in message
    assert excinfo.value.knowledge_ts_ns > excinfo.value.as_of_ns

    # The generic guard raises for any future-dated fact, not just statements.
    with pytest.raises(TemporalIntegrityError):
        guard.assert_no_lookahead(iso_to_ns("2026-03-15"), january, label="revenue")

    # Nothing about the period is visible in January at all.
    assert guard.visible_filings("ACME", january) == ()


def test_gate2_sec_edgar_accepts_after_acceptance_date() -> None:
    guard = _q4_filing_guard()
    march = iso_to_ns("2026-03-16")
    facts = guard.statement_as_of("ACME", march, period_end="2025-12-31")
    assert facts["revenue"] == pytest.approx(1_000.0)
    assert facts["net_income"] == pytest.approx(120.0)
    assert len(guard.visible_filings("ACME", march)) == 1
    assert guard.knowledge_time("ACME", "2025-12-31") == iso_to_ns("2026-03-15")

    # Exactly on the acceptance nanosecond is already public.
    on_time = guard.statement_as_of(
        "ACME", iso_to_ns("2026-03-15"), period_end="2025-12-31"
    )
    assert on_time["revenue"] == pytest.approx(1_000.0)


def test_gate2_amendment_counts_only_from_its_own_filing_date() -> None:
    guard = _q4_filing_guard()
    guard.register_filing(
        FilingRecord(
            ticker="ACME",
            form="10-K/A",
            period_end="2025-12-31",
            acceptance_date="2026-04-10",
            facts={"revenue": 980.0},
        )
    )
    assert guard.statement_as_of(
        "ACME", iso_to_ns("2026-03-16"), period_end="2025-12-31"
    )["revenue"] == pytest.approx(1_000.0)
    assert guard.statement_as_of(
        "ACME", iso_to_ns("2026-04-11"), period_end="2025-12-31"
    )["revenue"] == pytest.approx(980.0)


# ---------------------------------------------------------------------------
# Gate 3 -- SHA-256 numerical grounding and re-computation
# ---------------------------------------------------------------------------


def _grounded_packet() -> GroundedFactPacket:
    ledger = GroundingLedger(persist=False)
    return ledger.commit(
        "ACME",
        {
            "close": 250.0,
            "dcf_value": 142.5,
            "pe_ratio": 18.4,
            "ev_ebitda": 11.2,
            "expected_gross_bps": 18.0,
            "spread_bps": 2.0,
            "fee_bps": 1.75,
            "tax_bps": 2.0,
            "q98_residual_bps": 6.25,
        },
        valid_time_ns=BASE_NS,
        knowledge_ts_ns=BASE_NS - 1_000,
    )


def test_gate3_exact_and_derived_claims_pass() -> None:
    packet = _grounded_packet()
    assert packet.verify()
    assert len(packet.sha256_digest) == 64

    thesis = (
        "ACME trades at $250.00 against a DCF fair value of 142.50. "
        "The peer set supports 11.2x EV/EBITDA and 18.4x earnings. "
        "Net edge reconciles as 18.0 - 2.0 - 3.5 - 2.0 - 6.25 = 4.25 bps."
    )
    report = verify_thesis_claims(thesis, packet, tolerance=1e-4)
    assert report.claims_checked >= 6
    assert report.derived_expressions == 1
    assert report.digest == packet.sha256_digest

    ledger = GroundingLedger(persist=False)
    ledger.append(packet)
    assert ledger.verify_thesis(thesis, packet.sha256_digest).claims_checked >= 6
    assert ledger.verify_all() == 1


def test_gate3_hallucinated_figure_is_rejected() -> None:
    packet = _grounded_packet()
    thesis = "The DCF fair value is $164.20, implying 23.7% upside from spot."
    with pytest.raises(UngroundedNumericalClaim) as excinfo:
        verify_thesis_claims(thesis, packet)
    assert UNGROUNDED_CLAIM_CODE in str(excinfo.value)
    assert excinfo.type is UngroundedNumericalClaimError


def test_gate3_wrong_arithmetic_is_rejected() -> None:
    packet = _grounded_packet()
    thesis = "Net edge: 18.0 - 2.0 - 3.5 - 2.0 - 6.25 = 7.10 bps after friction."
    with pytest.raises(UngroundedNumericalClaim) as excinfo:
        verify_thesis_claims(thesis, packet)
    assert UNGROUNDED_CLAIM_CODE in str(excinfo.value)
    assert "arithmetic mismatch" in str(excinfo.value)


def test_gate3_tampered_packet_is_rejected() -> None:
    packet = _grounded_packet()
    packet.metrics["dcf_value"] = 999.0  # in-place tamper
    with pytest.raises(UngroundedNumericalClaim) as excinfo:
        verify_thesis_claims("The DCF value is 999.0.", packet)
    assert UNGROUNDED_CLAIM_CODE in str(excinfo.value)
    assert "digest" in str(excinfo.value)


def test_gate3_verifier_rejects_unhashed_packet() -> None:
    packet = _grounded_packet()
    object.__setattr__(packet, "sha256_digest", "")
    with pytest.raises(UngroundedNumericalClaim):
        NumericalGroundingVerifier().verify_thesis_claims("Close was 250.", packet)


# ---------------------------------------------------------------------------
# Gate 4 -- 5,000-ticker 490-factor scan SLA and funnel compression
# ---------------------------------------------------------------------------


def test_gate4_factor_zoo_meets_480_columns() -> None:
    zoo = FactorZooCatalog()
    assert zoo.factor_count >= MIN_FACTOR_COUNT
    assert zoo.factor_count == 490
    assert zoo.donor_factor_count == 478
    families = zoo.families
    assert families["alpha101"] == 101
    assert families["gtja191"] == 191
    assert families["qlib158"] == 154
    assert families["fundamental"] == 20
    assert families["native"] == 12


def test_gate4_scan_sla_and_funnel_compression(tmp_path: Path) -> None:
    panel = FactorPanel.generate(n_symbols=5_000, n_periods=64, seed=7)
    store = FeatureStore(tmp_path / "feature_lake")
    ledger = GroundingLedger(store, persist=True)
    scanner = VectorizedFactorScanner(ledger=ledger)
    try:
        scanner.warmup()
        scanner.scan(panel, as_of_ns=panel.valid_time_ns)  # warm caches
        samples = [
            scanner.scan(panel, as_of_ns=panel.valid_time_ns).elapsed_ms for _ in range(3)
        ]
        result = scanner.scan(panel, as_of_ns=panel.valid_time_ns)

        assert result.universe_size == 5_000
        assert result.factor_count >= 480
        assert result.kernel_count == 18
        assert len(result.candidates) == 50
        assert result.compression_ratio >= 0.99
        median_ms = sorted(samples)[1]
        assert median_ms < 120.0, f"scan p50={median_ms:.2f} ms (samples={samples})"

        scores = [candidate.score for candidate in result.candidates]
        assert scores == sorted(scores, reverse=True)
        assert len({candidate.symbol for candidate in result.candidates}) == 50

        for candidate in result.candidates:
            assert len(candidate.features) == 492
            assert candidate.packet.verify()
            assert candidate.packet.symbol == candidate.symbol
            assert candidate.packet.as_of_ns == panel.valid_time_ns
            assert candidate.packet.sha256_digest == candidate.packet.compute_digest()

        written = scanner.persist(result, store)
        assert written["symbols"] == 50
        assert written["factor_rows"] == 50 * 492

        probe = result.candidates[0]
        vector = store.get_factor_vector("US", probe.symbol, panel.valid_time_ns)
        assert len(vector) == 492
        assert vector["dislocation_score"] == pytest.approx(probe.score)
        assert vector["close"] == pytest.approx(probe.price)

        valuations = store.query_valuation(
            "US", probe.symbol, panel.valid_time_ns, artifact_kind="factor_scan"
        )
        assert valuations
        assert valuations[0]["payload"]["sha256_digest"] == probe.packet.sha256_digest
        assert len(ledger) == 50
        assert ledger.verify_all() == 50
    finally:
        store.close()


def test_gate4_polars_panel_bridge_preserves_column_order() -> None:
    import polars as pl

    rows = []
    for step in range(40):
        for symbol in ("AAA", "BBB", "CCC"):
            price = 100.0 + step * 0.5
            rows.append(
                {
                    "ts": step,
                    "symbol": symbol,
                    "close": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "open": price,
                    "volume": 1_000.0 + step,
                }
            )
    panel = FactorPanel.from_polars(pl.DataFrame(rows))
    assert panel.symbols == ("AAA", "BBB", "CCC")
    assert panel.close.shape == (40, 3)
    assert panel.close[-1, 0] == pytest.approx(119.5)
    scanner = VectorizedFactorScanner()
    scanner.warmup()
    result = scanner.scan(panel, top_k=2)
    assert [candidate.symbol for candidate in result.candidates] == ["AAA", "BBB"]


def test_gate4_valuation_lab_metrics_are_groundable() -> None:
    lab = ValuationLab()
    dcf = lab.dcf(
        fcff=[100.0, 110.0, 121.0, 133.0, 146.0],
        wacc=0.09,
        terminal_growth=0.025,
        net_debt=200.0,
        cash=50.0,
        shares=100.0,
    )
    assert dcf["dcf_enterprise_value"] > 0.0
    assert 0.0 < dcf["dcf_terminal_pct"] < 1.0
    assert dcf["dcf_per_share"] == pytest.approx(
        (dcf["dcf_enterprise_value"] - 200.0 + 50.0) / 100.0
    )

    comps = lab.comps(
        peer_ev_ebitda=[8.0, 10.0, 12.0, 14.0],
        ebitda=500.0,
        net_debt=200.0,
        shares=100.0,
    )
    assert comps["comps_ev_ebitda_median"] == pytest.approx(11.0)
    assert comps["comps_enterprise_value"] == pytest.approx(5_500.0)

    statements = lab.three_statement(
        revenue=1_000.0,
        growth=0.08,
        ebit_margin=0.20,
        tax_rate=0.25,
        capex_pct=0.05,
        nwc_pct=0.02,
        years=5,
    )
    assert statements["three_st_revenue"] == pytest.approx(1_000.0 * 1.08 ** 5)
    assert statements["three_st_fcf"] < statements["three_st_revenue"]

    impact = lab.market_impact(price=100.0, quantity=10_000.0, adv=1_000_000.0, volatility=0.02)
    assert impact["impact_bps"] > 0.0
    assert 0.0 < impact["impact_participation"] < 1.0

    bond = lab.fixed_income(face=1_000.0, coupon_rate=0.05, years=10.0, ytm=0.04, freq=2)
    assert bond["bond_price"] > 1_000.0  # coupon above yield -> premium bond
    assert bond["bond_modified_duration"] > 0.0
    assert bond["bond_ytm"] == pytest.approx(0.04, abs=1e-4)

    option = lab.options(spot=100.0, strike=100.0, volatility=0.2, rate=0.03, years=1.0)
    assert 5.0 < option["price"] < 20.0
    assert 0.4 < option["delta"] < 0.8

    # Deterministic, monotone DSR calibration.
    rng = np.random.default_rng(11)
    strong = rng.normal(0.0040, 0.0020, 1_000)
    weak = rng.normal(0.0, 0.0020, 1_000)
    strong_dsr = deflated_sharpe_ratio(strong, num_trials=5)
    weak_dsr = deflated_sharpe_ratio(weak, num_trials=5)
    assert 0.0 <= weak_dsr <= 1.0
    assert 0.0 <= strong_dsr <= 1.0
    assert strong_dsr > weak_dsr

    merged = lab.value(
        price=100.0,
        fcff=[10.0, 11.0, 12.0, 13.0, 14.0],
        wacc=0.10,
        terminal_growth=0.02,
        peer_ev_ebitda=[8.0, 9.0, 10.0],
        ebitda=100.0,
        shares=50.0,
        net_debt=100.0,
    )
    assert "dcf_gap" in merged and "comps_gap" in merged

    donor = lab.donor_status()
    assert donor["quantlib.impact"]["importable"] is True
    assert donor["quantlib.fixedincome"]["importable"] is True


# ---------------------------------------------------------------------------
# Gate 5 -- regression sentinel and four-ring air-gap check
# ---------------------------------------------------------------------------


_FORBIDDEN_ROOTS = frozenset(
    {
        "ring0_vault",
        "nautilus_trader",
        "tigerbeetle",
        "key_vault",
        "ib_insync",
        "ccxt",
        "websocket",
        "websockets",
        "socket",
        "requests",
        "httpx",
        "aiohttp",
        "openalgo",
        "zerodha",
        "dhan",
        "upstox",
        "angel",
        "mt5",
        "MetaTrader5",
    }
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
            if node.level and node.module:
                found.add(node.module)
    return found


def test_gate5_ring2_truth_and_quant_are_air_gapped() -> None:
    modules = [
        ROOT / "ring2_brain" / "truth" / "data_guard.py",
        ROOT / "ring2_brain" / "truth" / "grounding_gate.py",
        ROOT / "ring2_brain" / "quant" / "scanner.py",
    ]
    for module in modules:
        assert module.exists(), f"missing {module}"
        imported = _imported_modules(module)
        for name in imported:
            root = name.split(".")[0]
            assert root not in _FORBIDDEN_ROOTS, (
                f"{module.name} imports forbidden Ring 0 surface {name!r}"
            )
            assert "ring0_vault" not in name, (
                f"{module.name} must not import Ring 0 code ({name!r})"
            )


def test_gate5_step2_contracts_still_hold() -> None:
    """Regression sentinel: the Step 2 kernel must remain intact."""
    latch = GlobalKillLatch(reset=True)
    table = LeaseTableSHM(reset=True, kill_latch=latch)
    try:
        now = time.monotonic_ns()
        lease = System1Lease(
            symbol="BTC-USD",
            mode=LeaseMode.LONG_ONLY,
            target_bps=12.0,
            stop_bps=4.0,
            expires_at_ns=now + LEASE_TTL_NS,
        )
        assert lease.pack().__len__() == 64
        table.write_lease(lease, now_ns=now)
        assert table.read_lease("BTC-USD", now_ns=now).mode is LeaseMode.LONG_ONLY

        latch.halt(HaltReason.MANUAL_KILL_SWITCH, now_ns=now)
        assert table.read_lease("BTC-USD", now_ns=now).mode is LeaseMode.DISARMED

        # GroundedFactPacket still refuses future knowledge.
        with pytest.raises(TemporalIntegrityError):
            GroundedFactPacket(
                symbol="ACME",
                valid_time_ns=now,
                knowledge_ts_ns=now + 1,
                as_of_ns=now,
                metrics={"close": 1.0},
            )
    finally:
        table.close()
        latch.close()
        unlink_all()


def test_gate5_black_scholes_is_deterministic_and_arbitrage_free() -> None:
    first = black_scholes(100.0, 100.0, 0.2, 0.03, 1.0)
    second = black_scholes(100.0, 100.0, 0.2, 0.03, 1.0)
    assert first == second
    assert first["price"] > max(100.0 - 100.0 * np.exp(-0.03), 0.0)
    assert first["gamma"] > 0.0
