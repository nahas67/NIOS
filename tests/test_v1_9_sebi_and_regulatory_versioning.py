"""V1.9 Suite 29 -- SEBI/NSE compliance and effective-dated US/FX rules.

Covers ``B1``-``B6`` and ``D1``-``D5``:

* ``B1`` exchange algo-id tagging on the proposal, the signed order and the
  Ring 0 preflight (``ALGO-ID-MISSING``);
* ``B2`` the global 10 OPS integer-micro token bucket and OTR penalty accrual;
* ``B3`` the static egress IP allowlist;
* ``B4`` the 03:00 UTC (08:30 IST) pre-market TOTP renewal state machine;
* ``B5`` freeze-quantity auto-slicing;
* ``B6`` hedge-leg exit ordering;
* ``D1`` effective-dated rule selection (legacy PDT vs FINRA 4210);
* ``D2`` locate fee / borrow confirmation in the conformal net bound;
* ``D4`` SSR uptick enforcement and ``D5`` extended-hours spread guard.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.schemas import (  # noqa: E402
    REGULATORY_RULES,
    UnsignedProposal,
    VaultName,
    net_lower_bound_bps,
    select_regulatory_rules,
)
from ring0_vault.ibor.ledger_outbox import (  # noqa: E402
    CrashAtomicOutbox,
    IBORLedger,
    LedgerStore,
)
from ring0_vault.kernel.exchange_ops_limiter import (  # noqa: E402
    GlobalOpsTokenBucket,
    OtrMeter,
)
from ring0_vault.kernel.firewalls_and_oms import (  # noqa: E402
    FirewallStage,
    HedgeExitOrderingViolation,
    OrderSigner,
    OrderState,
    SmartOMS,
    SovereignPreTradeFirewall,
)
from ring0_vault.silos.key_vault.broker_credentials import (  # noqa: E402
    DEFAULT_GENERIC_ALGO_ID,
    resolve_exchange_algo_id,
)
from ring0_vault.silos.key_vault.totp_renewal import (  # noqa: E402
    PreMarketTotpRenewal,
    TotpRenewalState,
)
from ring0_vault.silos.silo_registry import (  # noqa: E402
    EGRESS_IP_WHITELIST_BY_SILO,
    is_egress_ip_whitelisted,
    register_egress_ip,
)

DAY_NS = 86_400_000_000_000


def _proposal(
    *,
    silo: str = "US_IBKR",
    symbol: str = "SPY",
    side: str = "BUY",
    vault: str = "SpinalScalper",
    algo_id: str = DEFAULT_GENERIC_ALGO_ID,
    algo_kind: str = "GENERIC",
    tranche_id: str = "",
) -> UnsignedProposal:
    return UnsignedProposal(
        proposal_id="p-1",
        strategy_id="suite-29",
        silo=silo,
        vault=vault,
        symbol=symbol,
        side=side,
        qty=1.0,
        limit_price=100.0,
        expected_gross_bps=12.0,
        spread_bps=1.0,
        grounding_hash="a" * 64,
        exchange_algo_id=algo_id,
        algo_id_kind=algo_kind,
        tranche_id=tranche_id,
    )


class _Stub:
    """A placeholder for a dependency the preflight path never touches."""

    def __getattr__(self, name: str):  # pragma: no cover - defensive
        raise AssertionError(f"preflight touched unexpected dependency {name!r}")


@pytest.fixture()
def firewall() -> SovereignPreTradeFirewall:
    return SovereignPreTradeFirewall(
        ledger=_Stub(), outbox=_Stub(), gate=_Stub(), shield=_Stub()
    )


# ---------------------------------------------------------------------------
# B1 -- exchange algo identification
# ---------------------------------------------------------------------------


def test_algo_id_defaults_and_registered_resolution() -> None:
    generic, kind = resolve_exchange_algo_id("zerodha")
    assert generic == DEFAULT_GENERIC_ALGO_ID
    assert kind == "GENERIC"
    registered, registered_kind = resolve_exchange_algo_id(
        "zerodha", configured="NSE-ALGO-00042"
    )
    assert registered == "NSE-ALGO-00042"
    assert registered_kind == "REGISTERED"

    proposal = _proposal()
    assert proposal.exchange_algo_id == DEFAULT_GENERIC_ALGO_ID
    assert proposal.algo_id_kind == "GENERIC"
    with pytest.raises(ValueError):
        _proposal(algo_kind="SELF_DECLARED")
    with pytest.raises(ValueError):
        UnsignedProposal(
            proposal_id="p",
            strategy_id="s",
            silo="US_IBKR",
            vault="SpinalScalper",
            symbol="SPY",
            side="BUY",
            qty=1.0,
            limit_price=100.0,
            expected_gross_bps=10.0,
            spread_bps=1.0,
            grounding_hash="a" * 64,
            exchange_algo_id="",
        )


def test_signed_order_carries_and_verifies_the_algo_tag() -> None:
    signer = OrderSigner()
    proposal = _proposal(
        silo="INDIA_ZERODHA_DHAN",
        symbol="NIFTY26JANFUT",
        algo_id="NSE-ALGO-00042",
        algo_kind="REGISTERED",
    )
    signed = signer.signed_order(
        proposal,
        outbox_key="k" * 32,
        net_lb_bps=6.0,
        order_type="MAKER_PEG",  # type: ignore[arg-type]
        now_ns=time.time_ns(),
    )
    assert signed.exchange_algo_id == "NSE-ALGO-00042"
    assert signed.algo_id_kind == "REGISTERED"
    assert signed.payload()["exchange_algo_id"] == "NSE-ALGO-00042"
    assert signer.verify_order(signed) is True
    # A tampered tag no longer verifies.
    tampered = signed.__class__(
        **{
            **{
                field: getattr(signed, field)
                for field in (
                    "proposal_id",
                    "silo",
                    "symbol",
                    "side",
                    "vault",
                    "order_type",
                    "qty",
                    "limit_price",
                    "payload_digest",
                    "ed25519_signature",
                    "fernet_token",
                    "outbox_key",
                    "signed_at_ns",
                    "reduce_only",
                    "strategy_id",
                    "net_lb_bps",
                    "algo_id_kind",
                    "recv_window_offset_ms",
                    "fencing_epoch",
                )
            },
            "exchange_algo_id": "NSE-ALGO-99999",
        }
    )
    assert signer.verify_order(tampered) is False


def test_ring0_rejects_generic_algo_id_when_registered_is_required(firewall) -> None:
    firewall.require_registered_algo_id = True
    now = time.time_ns()
    generic = firewall.preflight_checks(
        _proposal(silo="INDIA_ZERODHA_DHAN", symbol="NIFTY26JANFUT"), now_ns=now
    )
    assert generic is not None
    assert generic[0] is FirewallStage.ALGO_ID
    assert generic[1].startswith("ALGO-ID-MISSING")

    registered = firewall.preflight_checks(
        _proposal(
            silo="INDIA_ZERODHA_DHAN",
            symbol="NIFTY26JANFUT",
            algo_id="NSE-ALGO-00042",
            algo_kind="REGISTERED",
        ),
        now_ns=now,
    )
    assert registered is None


# ---------------------------------------------------------------------------
# B2 -- 10 OPS bucket and OTR metering
# ---------------------------------------------------------------------------


def test_global_ten_ops_bucket_is_deterministic_per_utc_second() -> None:
    bucket = GlobalOpsTokenBucket()
    second = 1_800_000_000 * 1_000_000_000
    granted = [bucket.acquire(now_ns=second) for _ in range(11)]
    assert granted == [True] * 10 + [False]
    assert bucket.remaining_ops(now_ns=second) == 0
    assert bucket.acquire(now_ns=second + 1_000_000_000) is True
    assert bucket.remaining_ops(now_ns=second + 1_000_000_000) == 9

    firewall = SovereignPreTradeFirewall(
        ledger=_Stub(), outbox=_Stub(), gate=_Stub(), shield=_Stub(), ops_bucket=bucket
    )
    exhausted = SovereignPreTradeFirewall(
        ledger=_Stub(),
        outbox=_Stub(),
        gate=_Stub(),
        shield=_Stub(),
        ops_bucket=GlobalOpsTokenBucket(),
    )
    now = 1_800_000_001 * 1_000_000_000
    for _ in range(10):
        assert exhausted.preflight_checks(_proposal(), now_ns=now) is None
    blocked = exhausted.preflight_checks(_proposal(), now_ns=now)
    assert blocked is not None
    assert blocked[0] is FirewallStage.OPS_LIMIT
    assert blocked[1].startswith("OPS-LIMIT")
    del firewall


def test_otr_meter_and_penalty_accrual() -> None:
    meter = OtrMeter()
    row = meter.record(submitted=120, executed=2)
    assert row["otr"] == pytest.approx(60.0)
    assert row["breached"] is True
    assert row["penalty_micros"] > 0

    store = LedgerStore(in_memory=True, probe=False)
    ledger = IBORLedger(store, silos=["INDIA_ZERODHA_DHAN"])
    ledger.open_silo("INDIA_ZERODHA_DHAN", funding_micros=10_000_000)
    accrued = ledger.accrue_otr_penalty("INDIA_ZERODHA_DHAN", row["penalty_micros"])
    assert accrued == row["penalty_micros"]
    assert ledger.otr_penalty_micros("INDIA_ZERODHA_DHAN") == accrued
    assert ledger.invariant_report()["holds"] is True
    ledger.close()


# ---------------------------------------------------------------------------
# B3 -- static egress IP allowlist
# ---------------------------------------------------------------------------


def test_egress_ip_allowlist_rejects_unregistered_addresses(firewall) -> None:
    assert is_egress_ip_whitelisted("zerodha", "127.0.0.1") is True
    assert is_egress_ip_whitelisted("zerodha", "198.51.100.7") is False
    register_egress_ip("zerodha", "198.51.100.7", primary=False)
    assert is_egress_ip_whitelisted("zerodha", "198.51.100.7") is True

    now = time.time_ns()
    firewall.egress_whitelist = {
        "INDIA_ZERODHA_DHAN": frozenset(EGRESS_IP_WHITELIST_BY_SILO["INDIA_ZERODHA_DHAN"])
    }
    firewall.egress_ip = "203.0.113.250"
    rejected = firewall.preflight_checks(
        _proposal(silo="INDIA_ZERODHA_DHAN", symbol="NIFTY26JANFUT"), now_ns=now
    )
    assert rejected is not None
    assert rejected[0] is FirewallStage.EGRESS_IP
    assert rejected[1].startswith("EGRESS_IP_NOT_WHITELISTED")

    firewall.egress_ip = "127.0.0.1"
    assert (
        firewall.preflight_checks(
            _proposal(silo="INDIA_ZERODHA_DHAN", symbol="NIFTY26JANFUT"), now_ns=now
        )
        is None
    )


# ---------------------------------------------------------------------------
# B4 -- pre-market TOTP renewal
# ---------------------------------------------------------------------------


def test_pre_market_totp_renewal_state_machine_and_ttl() -> None:
    machine = PreMarketTotpRenewal()
    # 2026-01-05 03:05 UTC is inside the 03:00 renewal window.
    now = 1_767_582_300 * 1_000_000_000
    due = machine.due(now_ns=now)
    assert due, due
    record = machine.renew("INDIA_ZERODHA_DHAN", "zerodha", now_ns=now)
    assert record.state is TotpRenewalState.RENEWED
    assert record.ttl_seconds_remaining(now) > 0
    telemetry = machine.telemetry(now_ns=now)
    assert telemetry["live_count"] == 1
    assert telemetry["renew_at_utc_hour"] == 3
    assert telemetry["ist_renew_at"] == "08:30 IST"

    # TTL telemetry decays to zero without mutating the record.
    assert record.ttl_seconds_remaining(now + 24 * 3600 * 1_000_000_000) == 0

    # A failed renewal is retryable; the state machine never jumps backwards.
    failing = PreMarketTotpRenewal(renewer=lambda silo, venue: False)
    failed = failing.renew("INDIA_ZERODHA_DHAN", "dhan", now_ns=now, force=True)
    assert failed.state is TotpRenewalState.FAILED
    assert failed.attempts == 1
    retried = failing.renew("INDIA_ZERODHA_DHAN", "dhan", now_ns=now, force=True)
    assert retried.state is TotpRenewalState.FAILED
    assert retried.attempts == 2


# ---------------------------------------------------------------------------
# B5 / B6 -- freeze slicing and hedge exit ordering
# ---------------------------------------------------------------------------


@pytest.fixture()
def oms() -> SmartOMS:
    store = LedgerStore(in_memory=True, probe=False)
    ledger = IBORLedger(store, silos=["INDIA_ZERODHA_DHAN"])
    ledger.open_silo("INDIA_ZERODHA_DHAN", funding_micros=1_000_000_000_000_000)
    ledger.fund_vault("INDIA_ZERODHA_DHAN", VaultName.SPINAL_SCALPER, 900_000_000_000_000)
    outbox = CrashAtomicOutbox(ledger)
    instance = SmartOMS(outbox, ledger=ledger)
    yield instance
    ledger.close()


def test_freeze_quantity_auto_slicing_respects_the_contract(oms: SmartOMS) -> None:
    spec = oms._instrument_spec("NIFTY26JANFUT")
    if spec is None or int(getattr(spec, "freeze_quantity", 0) or 0) <= 0:
        class FakeSpec:
            freeze_quantity = 1_800
            lot_size = 75

        oms._instrument_spec = lambda symbol: FakeSpec()  # type: ignore[assignment]
        freeze = 1_800
        lot = 75
    else:
        freeze = int(spec.freeze_quantity)
        lot = int(spec.lot_size)

    qty = freeze * 2 + lot
    slices = oms.freeze_slices("NIFTY26JANFUT", qty)
    assert len(slices) >= 3
    assert sum(slices) == pytest.approx(qty)
    assert all(child <= freeze for child in slices)
    assert all(abs(child / lot - round(child / lot)) < 1e-9 for child in slices)

    from dataclasses import replace

    parent = replace(
        _proposal(silo="INDIA_ZERODHA_DHAN", symbol="NIFTY26JANFUT"), qty=qty
    )
    submitted = oms.submit_frozen_order(
        parent, book=(100.0, 100.5), now_ns=time.time_ns()
    )
    assert submitted["parent_idempotency_key"]
    assert len(submitted["children"]) == len(slices)
    assert all(child["qty"] <= freeze for child in submitted["children"])


def test_hedge_exit_ordering_closes_the_protective_leg_last(oms: SmartOMS) -> None:
    legs = [
        {"leg_id": "short-call", "side": "SELL", "margin_pct": 18.0, "is_hedge": False},
        {"leg_id": "long-put", "side": "BUY", "margin_pct": 4.0, "is_hedge": True},
        {"leg_id": "long-call", "side": "BUY", "margin_pct": 9.0, "is_hedge": False},
    ]
    ordered = oms.exit_leg_group(legs)
    assert [leg["leg_id"] for leg in ordered] == ["short-call", "long-call", "long-put"]
    with pytest.raises(HedgeExitOrderingViolation) as violation:
        oms.exit_leg_group(legs, requested_order=["long-put", "short-call", "long-call"])
    assert "HEDGE_EXIT_ORDERING_VIOLATION" in str(violation.value)
    # The compliant order is accepted.
    oms.exit_leg_group(legs, requested_order=["short-call", "long-call", "long-put"])


# ---------------------------------------------------------------------------
# D1 -- effective-dated regulatory rules
# ---------------------------------------------------------------------------


def test_effective_dated_rules_switch_on_finra_4210_date() -> None:
    legacy = select_regulatory_rules("US_IBKR", 1_735_689_600 * 1_000_000_000)  # 2025-01-01
    assert legacy.rule_id == "US.PDT_25K.2020"
    assert legacy.pdt_equity_floor_micros == 25_000_000_000
    assert legacy.intraday_margin_model == "PDT_25K"

    modern = select_regulatory_rules("US_IBKR", 1_781_000_000 * 1_000_000_000)  # 2026-06-12
    assert modern.rule_id == "US.FINRA_4210_INTRADAY.2026"
    assert modern.pdt_equity_floor_micros == 2_000_000_000
    assert modern.maintenance_model == "FINRA_4210"
    assert modern.effective_from_utc == 1_780_531_200 * 1_000_000_000  # 2026-06-04 UTC

    before_switch = select_regulatory_rules("US_IBKR", 1_780_531_199 * 1_000_000_000)
    assert before_switch.rule_id == "US.PDT_25K.2020"
    at_switch = select_regulatory_rules("US_IBKR", 1_780_531_200 * 1_000_000_000)
    assert at_switch.rule_id == "US.FINRA_4210_INTRADAY.2026"

    assert len(REGULATORY_RULES) >= 5
    assert select_regulatory_rules("FX_MT5", time.time_ns()).intraday_margin_model
    with pytest.raises(KeyError):
        select_regulatory_rules("US_IBKR", 1_000_000_000)


# ---------------------------------------------------------------------------
# D2 / D4 / D5 -- locate fee, SSR and extended hours
# ---------------------------------------------------------------------------


def test_locate_fee_and_funding_reduce_the_conformal_net_bound() -> None:
    plain = net_lower_bound_bps(20.0, 1.0, 2.0, 3.0, 5.0)
    assert plain == pytest.approx(7.0)
    with_locate = net_lower_bound_bps(20.0, 1.0, 2.0, 3.0, 5.0, locate_fee_bps=2.5)
    with_funding = net_lower_bound_bps(20.0, 1.0, 2.0, 3.0, 5.0, funding_bps=1.5)
    assert with_locate == pytest.approx(4.5)
    assert with_funding == pytest.approx(5.5)


def test_short_sale_needs_a_confirmed_borrow(firewall) -> None:
    firewall.locate_fee_bps = 2.5
    firewall.borrow_confirmed = False
    now = time.time_ns()
    rejected = firewall.preflight_checks(_proposal(side="SELL"), now_ns=now)
    assert rejected is not None
    assert rejected[1].startswith("SHORT_BORROW_UNCONFIRMED")
    firewall.borrow_confirmed = True
    assert firewall.preflight_checks(_proposal(side="SELL"), now_ns=now) is None


def test_ssr_uptick_rule_rejects_a_short_below_the_prior_close(firewall) -> None:
    firewall.ssr_active = True
    firewall.prior_close_price = 100.0
    now = time.time_ns()
    firewall.last_sale_price = 99.5
    rejected = firewall.preflight_checks(_proposal(side="SELL"), now_ns=now)
    assert rejected is not None
    assert rejected[1].startswith("SSR_UPTICK_VIOLATION")
    firewall.last_sale_price = 100.25
    assert firewall.preflight_checks(_proposal(side="SELL"), now_ns=now) is None
    # A buy is never blocked by SSR.
    firewall.last_sale_price = 99.0
    assert firewall.preflight_checks(_proposal(side="BUY"), now_ns=now) is None


def test_extended_hours_spread_guard(firewall) -> None:
    from core.exchange_calendar import is_open

    # 2026-01-05 23:00 UTC is after the US close.
    after_hours = 1_767_654_000 * 1_000_000_000
    assert is_open("US_IBKR", after_hours) is False
    firewall.extended_hours_spread_cap_bps = 5.0
    wide = _proposal()
    wide = wide.__class__(
        **{**wide.to_dict(), "spread_bps": 12.0, "vault": wide.vault}
    )
    rejected = firewall.preflight_checks(wide, now_ns=after_hours)
    assert rejected is not None
    assert rejected[1].startswith("EXTENDED_HOURS_SPREAD_GUARD")

    tight = wide.__class__(**{**wide.to_dict(), "spread_bps": 2.0, "vault": wide.vault})
    assert firewall.preflight_checks(tight, now_ns=after_hours) is None


def test_order_state_machine_is_a_strict_allowlist() -> None:
    from ring0_vault.kernel.firewalls_and_oms import OrderLifecycleTracker

    tracker = OrderLifecycleTracker()
    tracker.track("order-1", 10.0)
    assert tracker.transition("order-1", OrderState.PENDING_NEW) is OrderState.PENDING_NEW
    assert tracker.transition("order-1", OrderState.ACCEPTED) is OrderState.ACCEPTED
    assert tracker.transition("order-1", OrderState.PARTIALLY_FILLED) is OrderState.PARTIALLY_FILLED
    assert tracker.transition("order-1", OrderState.FILLED) is OrderState.FILLED
    assert tracker.void_allowed("order-1") is False
    with pytest.raises(Exception):
        tracker.transition("order-1", OrderState.ACCEPTED)
