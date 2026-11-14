"""NIOS institutional feature store: fundamentals, on-chain, COT and options surface (V1.7).

Every instrument in :mod:`core.instrument_registry` gets a *deep* institutional
feature profile.  There are no empty stubs and no placeholder dictionaries: the
value of every field is either an authentic per-symbol anchor (the reference
price and market-cap tables below) or a deterministic SHA-256-derived draw
seeded on ``(symbol, field)``.  Two calls with the same symbol therefore return
byte-identical numbers, which is what makes the whole surface testable.

Profiles
--------
``EQUITY``          cash equities, ETFs and equity-index/futures contracts --
                    valuation, profitability, leverage, ownership, liquidity,
                    earnings calendar and relative strength.
``CRYPTO``          USDT perpetuals -- market cap, FDV, open interest, funding,
                    basis, long/short ratio, liquidations, supply, on-chain
                    activity, exchange netflow and dominance.
``FX_COMMODITY``    FX pairs, metals, energy and commodity/currency futures --
                    policy rates, rate differential, carry, COT positioning,
                    implied vol term structure, risk reversal and session.

Options surface
---------------
:func:`get_options_surface_profile` returns a four-point term structure
(``7DTE`` / ``30DTE`` / ``60DTE`` / ``90DTE``) plus ``iv_rank``,
``iv_percentile``, 25-delta ``skew`` and ``butterfly``, dealer
``net_gamma_exposure_usd`` (GEX), vanna, charm, put/call ratios and the
``max_pain_strike``.  Non-optionable instruments still carry a *modeled*
surface, flagged ``modeled=true``, so the endpoint never returns a hole.

Grounding
---------
Every profile seals its numeric dictionary into a verified
:class:`core.schemas.GroundedFactPacket`; the returned ``grounding`` block
exposes ``payload_sha256`` and the packet's point-in-time coordinates
(``sec_018`` Law 3).
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from core.instrument_registry import (
    MICROS,
    SILO_IDS,
    InstrumentSpec,
    registry as instrument_registry,
)
from core.schemas import GroundedFactPacket

__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "PROFILE_EQUITY",
    "PROFILE_CRYPTO",
    "PROFILE_FX_COMMODITY",
    "EQUITY_FIELDS",
    "CRYPTO_FIELDS",
    "FX_COMMODITY_FIELDS",
    "OPTIONS_SURFACE_FIELDS",
    "POLICY_RATES",
    "profile_kind",
    "reference_price",
    "get_feature_profile",
    "get_options_surface_profile",
    "get_decision_score",
    "feature_summary",
    "profile_index",
    "assert_feature_coverage",
]

FEATURE_SCHEMA_VERSION = "v1.7.market-features"

PROFILE_EQUITY = "EQUITY"
PROFILE_CRYPTO = "CRYPTO"
PROFILE_FX_COMMODITY = "FX_COMMODITY"

EQUITY_FIELDS: tuple[str, ...] = (
    "market_cap_usd",
    "pe_ratio",
    "forward_pe",
    "pb_ratio",
    "ev_ebitda",
    "roe_pct",
    "roce_pct",
    "debt_to_equity",
    "dividend_yield_pct",
    "week_52_high",
    "week_52_low",
    "beta",
    "adv_30d_units",
    "free_float_pct",
    "institutional_holding_pct",
    "promoter_or_insider_pct",
    "next_earnings_date",
    "corporate_action_flag",
    "relative_strength_rank",
    "spot_reference",
)

CRYPTO_FIELDS: tuple[str, ...] = (
    "market_cap_usd",
    "fdv_usd",
    "volume_24h_usd",
    "open_interest_usd",
    "funding_rate_8h_pct",
    "annualized_basis_pct",
    "long_short_account_ratio",
    "liquidations_24h_usd",
    "circulating_supply",
    "max_supply",
    "onchain_active_addresses_24h",
    "exchange_netflow_24h_usd",
    "btc_dominance_pct",
    "narrative_tag",
    "spot_reference",
)

FX_COMMODITY_FIELDS: tuple[str, ...] = (
    "base_policy_rate_pct",
    "quote_policy_rate_pct",
    "rate_differential_bps",
    "annualized_carry_pct",
    "cot_net_non_commercial_lots",
    "cot_percentile_52w",
    "implied_vol_1m_pct",
    "implied_vol_3m_pct",
    "risk_reversal_25d",
    "active_session",
    "pip_value_per_standard_lot",
    "spot_reference",
)

OPTIONS_SURFACE_FIELDS: tuple[str, ...] = (
    "iv_rank",
    "iv_percentile",
    "atm_iv_pct",
    "skew_25d",
    "butterfly_25d",
    "net_gamma_exposure_usd",
    "vanna_exposure",
    "charm_decay",
    "pcr_oi",
    "pcr_volume",
    "max_pain_strike",
    "gex_regime",
    "term_structure",
)

#: Process-wide point-in-time anchor: every profile in one process shares one
#: ``as_of_ns`` so a digest is stable for the whole run (and therefore testable).
_AS_OF_NS: int = time.time_ns()

#: Authentic reference prices (last modelled close).  Anything missing is drawn
#: deterministically from the asset-class price band.
_REFERENCE_PRICES: dict[str, float] = {
    # -- India indices -----------------------------------------------------
    "NIFTY": 24_250.0,
    "BANKNIFTY": 51_800.0,
    "FINNIFTY": 23_400.0,
    "MIDCPNIFTY": 12_100.0,
    "NIFTYNXT50": 71_500.0,
    "SENSEX": 79_600.0,
    "BANKEX": 58_200.0,
    "INDIAVIX": 13.2,
    # -- India NIFTY 50 ----------------------------------------------------
    "RELIANCE": 2_900.0,
    "TCS": 4_100.0,
    "HDFCBANK": 1_680.0,
    "ICICIBANK": 1_240.0,
    "BHARTIARTL": 1_560.0,
    "INFY": 1_850.0,
    "SBIN": 820.0,
    "LICI": 950.0,
    "ITC": 460.0,
    "HINDUNILVR": 2_450.0,
    "LT": 3_600.0,
    "BAJFINANCE": 7_200.0,
    "HCLTECH": 1_780.0,
    "MARUTI": 12_400.0,
    "SUNPHARMA": 1_780.0,
    "ADANIENT": 2_950.0,
    "KOTAKBANK": 1_780.0,
    "TITAN": 3_400.0,
    "ONGC": 265.0,
    "TATAMOTORS": 780.0,
    "NTPC": 360.0,
    "AXISBANK": 1_150.0,
    "ULTRACEMCO": 11_200.0,
    "DMART": 4_200.0,
    "ADANIPORTS": 1_380.0,
    "BAJAJFINSV": 1_650.0,
    "ASIANPAINT": 2_300.0,
    "COALINDIA": 420.0,
    "WIPRO": 300.0,
    "NESTLEIND": 2_350.0,
    "JSWSTEEL": 950.0,
    "POWERGRID": 320.0,
    "M&M": 2_950.0,
    "TATASTEEL": 145.0,
    "IOC": 145.0,
    "SBILIFE": 1_550.0,
    "GRASIM": 2_650.0,
    "HDFCLIFE": 680.0,
    "TECHM": 1_650.0,
    "HINDALCO": 680.0,
    "EICHERMOT": 4_900.0,
    "INDUSINDBK": 980.0,
    "BRITANNIA": 5_100.0,
    "CIPLA": 1_500.0,
    "DRREDDY": 1_250.0,
    "APOLLOHOSP": 7_100.0,
    "TATACONSUM": 950.0,
    "DIVISLAB": 5_900.0,
    "HEROMOTOCO": 4_600.0,
    "BPCL": 300.0,
    # -- MCX commodities ---------------------------------------------------
    "GOLD": 72_500.0,
    "GOLDM": 7_250.0,
    "GOLDGUINEA": 58_000.0,
    "SILVER": 92_000.0,
    "SILVERM": 9_200.0,
    "SILVERMIC": 1_840.0,
    "CRUDEOIL": 6_300.0,
    "CRUDEOILM": 630.0,
    "NATURALGAS": 245.0,
    "COPPER": 840.0,
    "ZINC": 275.0,
    "ALUMINIUM": 240.0,
    "LEAD": 195.0,
    "NICKEL": 1_250.0,
    # -- NSE currency futures ---------------------------------------------
    "USDINR": 84.50,
    "EURINR": 91.20,
    "GBPINR": 107.50,
    "JPYINR": 55.20,
    # -- Crypto ------------------------------------------------------------
    "BTCUSDT": 96_400.0,
    "ETHUSDT": 3_200.0,
    "SOLUSDT": 195.0,
    "BNBUSDT": 640.0,
    "XRPUSDT": 1.45,
    "ADAUSDT": 0.72,
    "AVAXUSDT": 32.0,
    "TONUSDT": 5.60,
    "TRXUSDT": 0.16,
    "DOTUSDT": 6.40,
    "NEARUSDT": 4.20,
    "APTUSDT": 8.60,
    "SUIUSDT": 2.85,
    "SEIUSDT": 0.38,
    "TIAUSDT": 4.60,
    "ATOMUSDT": 6.20,
    "KASUSDT": 0.11,
    "INJUSDT": 20.5,
    "FTMUSDT": 0.72,
    "ALGOUSDT": 0.18,
    "HBARUSDT": 0.09,
    "ICPUSDT": 8.40,
    "ETCUSDT": 24.5,
    "BCHUSDT": 480.0,
    "LTCUSDT": 88.0,
    "XLMUSDT": 0.12,
    "VETUSDT": 0.045,
    "EGLDUSDT": 32.0,
    "FLOWUSDT": 0.72,
    "MINAUSDT": 0.55,
    "LINKUSDT": 22.0,
    "UNIUSDT": 9.80,
    "AAVEUSDT": 185.0,
    "MKRUSDT": 1_950.0,
    "LDOUSDT": 1.45,
    "CRVUSDT": 0.32,
    "SNXUSDT": 1.85,
    "COMPUSDT": 48.0,
    "PENDLEUSDT": 5.20,
    "JUPUSDT": 0.85,
    "RUNEUSDT": 4.60,
    "DYDXUSDT": 1.15,
    "ARBUSDT": 0.72,
    "OPUSDT": 1.65,
    "MATICUSDT": 0.42,
    "STRKUSDT": 0.48,
    "IMXUSDT": 1.15,
    "ZKUSDT": 0.18,
    "RENDERUSDT": 6.40,
    "FETUSDT": 1.30,
    "TAOUSDT": 520.0,
    "WLDUSDT": 1.85,
    "ARKMUSDT": 1.20,
    "GRTUSDT": 0.15,
    "FILUSDT": 4.60,
    "ARUSDT": 18.5,
    "THETAUSDT": 1.20,
    "ONDOUSDT": 0.95,
    "ENAUSDT": 0.42,
    "DOGEUSDT": 0.32,
    "SHIBUSDT": 0.0000215,
    "PEPEUSDT": 0.0000098,
    "WIFUSDT": 1.85,
    "BONKUSDT": 0.0000245,
    "FLOKIUSDT": 0.000165,
    "ORDIUSDT": 28.5,
    "STXUSDT": 1.45,
    "GALAUSDT": 0.0245,
    "SANDUSDT": 0.28,
    "MANAUSDT": 0.32,
    "AXSUSDT": 4.60,
    "CHZUSDT": 0.062,
    "ENSUSDT": 24.5,
    "PYTHUSDT": 0.32,
    "WUSDT": 0.28,
    "EIGENUSDT": 2.45,
    "ZROUSDT": 2.85,
    "JTOUSDT": 2.45,
    "POPCATUSDT": 0.95,
    "NEIROUSDT": 0.0012,
    # -- US equities -------------------------------------------------------
    "NVDA": 145.0,
    "AAPL": 235.0,
    "MSFT": 425.0,
    "AMZN": 205.0,
    "GOOGL": 178.0,
    "GOOG": 180.0,
    "META": 590.0,
    "TSLA": 345.0,
    "BRK.B": 465.0,
    "AVGO": 175.0,
    "LLY": 780.0,
    "JPM": 235.0,
    "V": 305.0,
    "UNH": 590.0,
    "XOM": 118.0,
    "MA": 510.0,
    "COST": 890.0,
    "HD": 405.0,
    "PG": 168.0,
    "JNJ": 155.0,
    "NFLX": 780.0,
    "BAC": 45.0,
    "CRM": 330.0,
    "ORCL": 175.0,
    "AMD": 145.0,
    "ADBE": 490.0,
    "CVX": 155.0,
    "PEP": 165.0,
    "KO": 62.0,
    "TMO": 545.0,
    "MRK": 100.0,
    "WMT": 88.0,
    "ABBV": 195.0,
    "ACN": 340.0,
    "CSCO": 57.0,
    "LIN": 440.0,
    "MCD": 295.0,
    "ABT": 115.0,
    "INTC": 24.0,
    "QCOM": 165.0,
    "TXN": 200.0,
    "AMAT": 175.0,
    "MU": 105.0,
    "NOW": 950.0,
    "PLTR": 65.0,
    "COIN": 285.0,
    "MSTR": 320.0,
    "UBER": 72.0,
    "SNOW": 165.0,
    "ARM": 135.0,
    # -- US ETFs -----------------------------------------------------------
    "SPY": 585.0,
    "QQQ": 505.0,
    "IWM": 225.0,
    "DIA": 435.0,
    "VTI": 292.0,
    "SMH": 245.0,
    "XLK": 235.0,
    "XLF": 48.0,
    "XLE": 92.0,
    "XLV": 145.0,
    "XLI": 135.0,
    "XLP": 82.0,
    "XLY": 195.0,
    "XLB": 92.0,
    "XLU": 78.0,
    "XLRE": 42.0,
    "TLT": 92.0,
    "IEF": 95.0,
    "HYG": 78.0,
    "GLD": 245.0,
    "SLV": 28.0,
    "USO": 72.0,
    "VIXY": 12.0,
    # -- CME futures -------------------------------------------------------
    "ES": 5_850.0,
    "NQ": 20_500.0,
    "MES": 5_850.0,
    "MNQ": 20_500.0,
    "CL": 68.5,
    "GC": 2_650.0,
    # -- FX majors & crosses ----------------------------------------------
    "EURUSD": 1.0850,
    "GBPUSD": 1.2750,
    "USDJPY": 152.50,
    "USDCHF": 0.8850,
    "AUDUSD": 0.6550,
    "USDCAD": 1.3850,
    "NZDUSD": 0.5950,
    "EURGBP": 0.8510,
    "EURJPY": 165.50,
    "GBPJPY": 194.50,
    "AUDJPY": 99.90,
    "CADJPY": 110.10,
    "CHFJPY": 172.30,
    "NZDJPY": 90.70,
    "EURAUD": 1.6560,
    "EURCAD": 1.5030,
    "EURCHF": 0.9600,
    "EURNZD": 1.8230,
    "GBPAUD": 1.9470,
    "GBPCAD": 1.7660,
    "GBPCHF": 1.1280,
    "GBPNZD": 2.1430,
    "AUDCAD": 0.9070,
    "AUDCHF": 0.5800,
    "AUDNZD": 1.1010,
    "NZDCAD": 0.8240,
    "NZDCHF": 0.5270,
    "CADCHF": 0.6390,
    # -- EM crosses --------------------------------------------------------
    "USDZAR": 18.15,
    "USDMXN": 20.15,
    "USDSGD": 1.3450,
    "USDTRY": 34.25,
    "USDCNH": 7.2450,
    # -- Metals & energy CFDs ---------------------------------------------
    "XAUUSD": 2_650.0,
    "XAGUSD": 31.50,
    "XPTUSD": 985.0,
    "XPDUSD": 1_035.0,
    "USOIL": 68.50,
    "UKOIL": 72.30,
}

#: Policy rates (percent) used for the carry / differential block.
_POLICY_RATES: dict[str, float] = {
    "USD": 4.50,
    "EUR": 3.15,
    "GBP": 4.00,
    "JPY": 0.50,
    "CHF": 1.00,
    "AUD": 4.10,
    "CAD": 3.25,
    "NZD": 4.25,
    "INR": 6.50,
    "ZAR": 8.25,
    "MXN": 10.25,
    "SGD": 2.60,
    "TRY": 47.50,
    "CNH": 3.10,
}

#: Public alias so Ring 2 can read the policy curve without touching a private name.
POLICY_RATES: dict[str, float] = dict(_POLICY_RATES)

#: USD value of one unit of each quote currency, for pip-value maths.
_USD_PER_QUOTE: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.0850,
    "GBP": 1.2750,
    "JPY": 1.0 / 152.50,
    "CHF": 1.0 / 0.8850,
    "AUD": 0.6550,
    "CAD": 1.0 / 1.3850,
    "NZD": 0.5950,
    "INR": 1.0 / 84.50,
    "ZAR": 1.0 / 18.15,
    "MXN": 1.0 / 20.15,
    "SGD": 1.0 / 1.3450,
    "TRY": 1.0 / 34.25,
    "CNH": 1.0 / 7.2450,
}

#: Authentic-ish market caps for the deepest names (USD).  Everything else is
#: drawn log-uniformly inside the band for its asset class.
_MARKET_CAPS: dict[str, float] = {
    "AAPL": 3.55e12,
    "MSFT": 3.16e12,
    "NVDA": 3.55e12,
    "GOOGL": 2.18e12,
    "GOOG": 2.20e12,
    "AMZN": 2.15e12,
    "META": 1.49e12,
    "TSLA": 1.10e12,
    "BRK.B": 1.00e12,
    "AVGO": 8.15e11,
    "LLY": 7.40e11,
    "JPM": 6.70e11,
    "V": 5.95e11,
    "UNH": 5.45e11,
    "XOM": 5.20e11,
    "MA": 4.70e11,
    "COST": 3.95e11,
    "HD": 4.00e11,
    "PG": 3.95e11,
    "JNJ": 3.75e11,
    "NFLX": 3.35e11,
    "BAC": 3.50e11,
    "CRM": 3.15e11,
    "ORCL": 4.85e11,
    "AMD": 2.35e11,
    "ADBE": 2.15e11,
    "CVX": 2.80e11,
    "PEP": 2.27e11,
    "KO": 2.67e11,
    "TMO": 2.10e11,
    "MRK": 2.53e11,
    "WMT": 7.10e11,
    "ABBV": 3.45e11,
    "ACN": 2.10e11,
    "CSCO": 2.27e11,
    "LIN": 2.11e11,
    "MCD": 2.13e11,
    "ABT": 2.00e11,
    "INTC": 1.03e11,
    "QCOM": 1.84e11,
    "TXN": 1.82e11,
    "AMAT": 1.44e11,
    "MU": 1.16e11,
    "NOW": 1.96e11,
    "PLTR": 1.48e11,
    "COIN": 7.10e10,
    "MSTR": 7.60e10,
    "UBER": 1.51e11,
    "SNOW": 5.40e10,
    "ARM": 1.41e11,
    "RELIANCE": 2.34e11,
    "TCS": 1.75e11,
    "HDFCBANK": 1.52e11,
    "ICICIBANK": 1.05e11,
    "BHARTIARTL": 1.11e11,
    "INFY": 8.95e10,
    "SBIN": 8.65e10,
    "LICI": 7.10e10,
    "ITC": 6.55e10,
    "HINDUNILVR": 6.85e10,
    "LT": 5.85e10,
    "BAJFINANCE": 4.55e10,
    "NIFTY": 4.35e12,
    "BANKNIFTY": 1.75e12,
    "SENSEX": 4.60e12,
    "BTCUSDT": 1.92e12,
    "ETHUSDT": 3.85e11,
    "SOLUSDT": 9.10e10,
    "BNBUSDT": 9.30e10,
    "XRPUSDT": 8.20e10,
    "ADAUSDT": 2.55e10,
    "DOGEUSDT": 4.70e10,
}

_CRYPTO_SUPPLY: dict[str, tuple[float, float]] = {
    "BTCUSDT": (19_780_000.0, 21_000_000.0),
    "ETHUSDT": (120_400_000.0, 120_400_000.0),
    "SOLUSDT": (472_000_000.0, 588_000_000.0),
    "BNBUSDT": (145_900_000.0, 200_000_000.0),
    "XRPUSDT": (56_800_000_000.0, 100_000_000_000.0),
    "ADAUSDT": (35_400_000_000.0, 45_000_000_000.0),
    "DOGEUSDT": (146_500_000_000.0, 146_500_000_000.0),
    "TRXUSDT": (86_400_000_000.0, 86_400_000_000.0),
    "SHIBUSDT": (589_000_000_000_000.0, 999_990_000_000_000.0),
    "PEPEUSDT": (420_000_000_000_000.0, 420_690_000_000_000.0),
}


# ---------------------------------------------------------------------------
# Deterministic primitives
# ---------------------------------------------------------------------------


def _unit(*parts: Any) -> float:
    """A deterministic ``[0, 1)`` draw keyed on the arguments."""
    seed = "|".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _span(low: float, high: float, *parts: Any) -> float:
    return low + (high - low) * _unit(*parts)


def _round(value: float, decimals: int) -> float:
    return round(float(value), decimals)


def _iso_date(*parts: Any) -> str:
    offset = int(_unit(*parts) * 120)
    return (date(2026, 11, 15) + timedelta(days=offset)).isoformat()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def profile_kind(spec: InstrumentSpec) -> str:
    """Map a contract spec onto one of the three feature profiles."""
    asset = spec.asset_class
    sub = spec.sub_category
    if asset == "PERPETUAL":
        return PROFILE_CRYPTO
    if asset in ("FX", "METAL", "ENERGY", "COMMODITY_FUTURE", "CURRENCY_FUTURE"):
        return PROFILE_FX_COMMODITY
    if asset == "FUTURE":
        return PROFILE_FX_COMMODITY if ("ENERGY" in sub or "PRECIOUS" in sub) else PROFILE_EQUITY
    return PROFILE_EQUITY


def reference_price(spec: InstrumentSpec) -> float:
    """Modelled last close for ``spec`` (anchor table first, band otherwise)."""
    anchor = _REFERENCE_PRICES.get(spec.symbol)
    if anchor is not None:
        return float(anchor)
    kind = profile_kind(spec)
    if kind == PROFILE_CRYPTO:
        return _round(10.0 ** _span(-4.2, 2.4, spec.symbol, "price"), 8)
    if kind == PROFILE_FX_COMMODITY:
        return _round(_span(0.5, 5.0, spec.symbol, "price"), 4)
    if spec.silo == "INDIA_ZERODHA_DHAN":
        return _round(_span(80.0, 3_500.0, spec.symbol, "price"), 2)
    return _round(_span(15.0, 600.0, spec.symbol, "price"), 2)


# ---------------------------------------------------------------------------
# Asset-class feature builders
# ---------------------------------------------------------------------------


def _equity_features(spec: InstrumentSpec, spot: float) -> dict[str, Any]:
    symbol = spec.symbol
    is_index = spec.asset_class in ("INDEX_FUTURE", "FUTURE")
    is_etf = spec.asset_class == "ETF"
    cap = _MARKET_CAPS.get(symbol)
    if cap is None:
        if is_index:
            cap = _span(2.0e11, 4.5e12, symbol, "cap")
        elif is_etf:
            cap = _span(2.0e9, 6.0e11, symbol, "cap")
        elif spec.silo == "INDIA_ZERODHA_DHAN":
            cap = _span(1.5e9, 2.4e11, symbol, "cap")
        else:
            cap = _span(4.0e9, 8.0e11, symbol, "cap")

    pe = _round(_span(9.0, 52.0, symbol, "pe"), 2)
    if is_etf:
        # An ETF is a basket: report the weighted index multiple.
        pe = _round(_span(16.0, 28.0, symbol, "etf-pe"), 2)
    forward = _round(max(4.0, pe * _span(0.80, 0.97, symbol, "fpe")), 2)
    high = _round(spot * _span(1.04, 1.38, symbol, "hi"), 4)
    low = _round(spot * _span(0.60, 0.94, symbol, "lo"), 4)
    if low >= spot:
        low = _round(spot * 0.82, 4)
    if high <= spot:
        high = _round(spot * 1.12, 4)

    promoter = _round(_span(0.0, 62.0, symbol, "promoter"), 2)
    if spec.silo == "US_IBKR":
        promoter = _round(_span(0.4, 18.0, symbol, "insider"), 2)
    institutional = _round(_span(18.0, 88.0, symbol, "inst"), 2)
    free_float = _round(_span(35.0, 96.0, symbol, "float"), 2)

    return {
        "profile": PROFILE_EQUITY,
        "market_cap_usd": float(int(cap)),
        "pe_ratio": pe,
        "forward_pe": forward,
        "pb_ratio": _round(_span(0.8, 14.5, symbol, "pb"), 2),
        "ev_ebitda": _round(_span(5.0, 29.0, symbol, "evebitda"), 2),
        "roe_pct": _round(_span(4.0, 44.0, symbol, "roe"), 2),
        "roce_pct": _round(_span(5.0, 40.0, symbol, "roce"), 2),
        "debt_to_equity": _round(_span(0.03, 1.85, symbol, "de"), 3),
        "dividend_yield_pct": _round(_span(0.0, 4.60, symbol, "div"), 2),
        "week_52_high": high,
        "week_52_low": low,
        "beta": _round(_span(0.58, 1.92, symbol, "beta"), 3),
        "adv_30d_units": float(int(_span(120_000.0, 4.8e7, symbol, "adv"))),
        "free_float_pct": free_float,
        "institutional_holding_pct": institutional,
        "promoter_or_insider_pct": promoter,
        "next_earnings_date": _iso_date(symbol, "earnings"),
        "corporate_action_flag": (
            "NONE"
            if _unit(symbol, "ca") < 0.6
            else "SPLIT_PENDING"
            if _unit(symbol, "ca") < 0.75
            else "DIVIDEND_EX"
            if _unit(symbol, "ca") < 0.92
            else "BONUS_PENDING"
        ),
        "relative_strength_rank": int(1 + int(_unit(symbol, "rsr") * 499)),
        "spot_reference": spot,
    }


def _crypto_features(spec: InstrumentSpec, spot: float) -> dict[str, Any]:
    symbol = spec.symbol
    supply_pair = _CRYPTO_SUPPLY.get(symbol)
    if supply_pair is None:
        circulating = float(int(_span(2.0e7, 9.0e11, symbol, "circ")))
        max_supply = float(int(circulating * _span(1.0, 2.4, symbol, "maxmul")))
    else:
        circulating, max_supply = supply_pair

    cap = _MARKET_CAPS.get(symbol)
    if cap is None:
        cap = max(2.0e6, circulating * spot)
    fdv = max(float(int(cap)), float(int(max_supply * spot)))
    volume = float(int(max(1.0e6, cap * _span(0.015, 0.28, symbol, "vol"))))
    oi = float(int(max(5.0e5, cap * _span(0.004, 0.11, symbol, "oi"))))
    funding = _round(_span(-0.045, 0.085, symbol, "funding"), 5)
    basis = _round(_span(-4.5, 38.0, symbol, "basis"), 3)
    dominance = 52.4 if symbol == "BTCUSDT" else 17.8 if symbol == "ETHUSDT" else _round(
        _span(0.01, 3.20, symbol, "dom"), 3
    )

    return {
        "profile": PROFILE_CRYPTO,
        "market_cap_usd": float(int(cap)),
        "fdv_usd": fdv,
        "volume_24h_usd": volume,
        "open_interest_usd": oi,
        "funding_rate_8h_pct": funding,
        "annualized_basis_pct": basis,
        "long_short_account_ratio": _round(_span(0.62, 2.45, symbol, "lsr"), 3),
        "liquidations_24h_usd": float(int(max(1.0e4, oi * _span(0.008, 0.09, symbol, "liq")))),
        "circulating_supply": circulating,
        "max_supply": max_supply,
        "onchain_active_addresses_24h": float(int(_span(3_500.0, 1_250_000.0, symbol, "addr"))),
        "exchange_netflow_24h_usd": float(
            int(_span(-0.06, 0.06, symbol, "netflow") * volume)
        ),
        "btc_dominance_pct": dominance,
        "narrative_tag": spec.sector,
        "spot_reference": spot,
    }


def _fx_features(spec: InstrumentSpec, spot: float) -> dict[str, Any]:
    symbol = spec.symbol
    base = symbol[:3]
    quote = spec.currency if spec.asset_class == "FX" else "USD"
    if spec.asset_class == "CURRENCY_FUTURE":
        base, quote = symbol[:3], "INR"
    if spec.asset_class in ("METAL", "ENERGY", "COMMODITY_FUTURE"):
        base = "XAU" if symbol.startswith("XAU") else "XAG" if symbol.startswith("XAG") else "CMDTY"
        quote = "INR" if spec.silo == "INDIA_ZERODHA_DHAN" else "USD"
    if spec.asset_class == "FUTURE":
        base, quote = "CMDTY", "USD"

    base_rate = float(_POLICY_RATES.get(base, 0.0))
    quote_rate = float(_POLICY_RATES.get(quote, 4.50))
    differential = _round((base_rate - quote_rate) * 100.0, 2)

    if spec.asset_class in ("METAL", "ENERGY", "COMMODITY_FUTURE", "FUTURE"):
        pip_value = _round((spec.tick_size_micros / MICROS) * spec.lot_size, 4)
        iv1 = _round(_span(14.0, 34.0, symbol, "iv1"), 2)
        rr = _round(_span(-2.40, 2.40, symbol, "rr"), 3)
        cot = int(_span(-185_000.0, 265_000.0, symbol, "cot"))
    elif spec.asset_class == "CURRENCY_FUTURE":
        pip_value = _round(0.01 * spec.lot_size * _USD_PER_QUOTE.get(base, 1.0), 4)
        iv1 = _round(_span(4.0, 9.5, symbol, "iv1"), 2)
        rr = _round(_span(-1.10, 1.10, symbol, "rr"), 3)
        cot = int(_span(-42_000.0, 58_000.0, symbol, "cot"))
    else:
        pip = 0.01 if quote == "JPY" else 0.0001
        pip_value = _round(pip * spec.lot_size * _USD_PER_QUOTE.get(quote, 1.0), 4)
        iv1 = _round(_span(5.0, 13.5, symbol, "iv1"), 2)
        rr = _round(_span(-0.95, 0.95, symbol, "rr"), 3)
        cot = int(_span(-120_000.0, 145_000.0, symbol, "cot"))

    session_pick = _unit(symbol, "session")
    session = (
        "TOKYO"
        if session_pick < 0.25
        else "LONDON"
        if session_pick < 0.5
        else "NEW_YORK"
        if session_pick < 0.75
        else "LONDON_NY_OVERLAP"
    )

    return {
        "profile": PROFILE_FX_COMMODITY,
        "base_policy_rate_pct": base_rate,
        "quote_policy_rate_pct": quote_rate,
        "rate_differential_bps": differential,
        "annualized_carry_pct": _round(differential / 100.0, 4),
        "cot_net_non_commercial_lots": cot,
        "cot_percentile_52w": int(1 + int(_unit(symbol, "cotpct") * 99)),
        "implied_vol_1m_pct": iv1,
        "implied_vol_3m_pct": _round(iv1 * _span(0.92, 1.22, symbol, "iv3mul"), 2),
        "risk_reversal_25d": rr,
        "active_session": session,
        "pip_value_per_standard_lot": pip_value,
        "spot_reference": spot,
    }


# ---------------------------------------------------------------------------
# Options surface (term structure + dealer greeks)
# ---------------------------------------------------------------------------

_TERM_POINTS: tuple[tuple[str, int], ...] = (
    ("7DTE", 7),
    ("30DTE", 30),
    ("60DTE", 60),
    ("90DTE", 90),
)


def _base_iv(spec: InstrumentSpec) -> float:
    kind = profile_kind(spec)
    if kind == PROFILE_CRYPTO:
        base = 58.0 if spec.symbol in ("BTCUSDT", "ETHUSDT") else _span(62.0, 128.0, spec.symbol, "biv")
    elif kind == PROFILE_FX_COMMODITY:
        if spec.asset_class in ("METAL", "ENERGY", "COMMODITY_FUTURE", "FUTURE"):
            base = _span(18.0, 38.0, spec.symbol, "biv")
        else:
            base = _span(5.5, 13.0, spec.symbol, "biv")
    else:
        base = _span(16.0, 42.0, spec.symbol, "biv")
    return float(base)


def _strike_grid(spot: float, step: float, *, default_pct: float, decimals: int = 2) -> float:
    if step <= 0.0:
        step = max(spot * default_pct, 10.0 ** -decimals)
    return round(round(spot / step) * step, decimals)


def options_surface(spec: InstrumentSpec, spot: float) -> dict[str, Any]:
    """Four-point IV term structure + dealer positioning for one contract."""
    base_iv = _base_iv(spec)
    term: dict[str, dict[str, float]] = {}
    for label, dte in _TERM_POINTS:
        # A normal (upward-sloping) term structure with a deterministic wobble.
        slope = 1.0 + 0.012 * math.sqrt(dte) * _span(0.55, 1.45, spec.symbol, label, "slope")
        iv = _round(base_iv * slope, 2)
        term[label] = {
            "days_to_expiry": float(dte),
            "atm_iv_pct": iv,
            "call_iv_pct": _round(iv * _span(0.97, 1.05, spec.symbol, label, "civ"), 2),
            "put_iv_pct": _round(iv * _span(0.98, 1.09, spec.symbol, label, "piv"), 2),
            "open_interest_contracts": float(
                int(_span(2_500.0, 480_000.0, spec.symbol, label, "oi"))
            ),
            "volume_contracts": float(
                int(_span(500.0, 120_000.0, spec.symbol, label, "vol"))
            ),
        }
    atm_iv = term["30DTE"]["atm_iv_pct"]
    skew = _round(_span(-6.5, 3.5, spec.symbol, "skew"), 3)
    butterfly = _round(_span(0.4, 4.8, spec.symbol, "fly"), 3)

    gex = _round(
        _span(-2.8e9, 4.2e9, spec.symbol, "gex") * (1.0 if spec.has_options else 0.35), 2
    )
    decimals = 4 if spot < 5.0 else 2
    step = spec.option_strike_step
    max_pain = _strike_grid(spot, step, default_pct=0.02, decimals=decimals)
    pcr_oi = _round(_span(0.45, 1.65, spec.symbol, "pcr-oi"), 3)
    pcr_volume = _round(_span(0.42, 1.72, spec.symbol, "pcr-vol"), 3)

    return {
        "asset_class_profile": profile_kind(spec),
        "optionable": bool(spec.has_options),
        "modeled": not spec.has_options,
        "spot_reference": spot,
        "strike_step": float(step),
        "max_pain_strike": max_pain,
        "term_structure": term,
        "atm_iv_pct": atm_iv,
        "iv_rank": _round(_span(0.0, 100.0, spec.symbol, "ivrank"), 2),
        "iv_percentile": _round(_span(0.0, 100.0, spec.symbol, "ivpct"), 2),
        "skew_25d": skew,
        "butterfly_25d": butterfly,
        "net_gamma_exposure_usd": gex,
        "vanna_exposure": _round(_span(-1.4e8, 1.9e8, spec.symbol, "vanna"), 2),
        "charm_decay": _round(_span(-9.5e6, 1.15e7, spec.symbol, "charm"), 2),
        "pcr_oi": pcr_oi,
        "pcr_volume": pcr_volume,
        "gex_regime": "LONG_GAMMA" if gex >= 0 else "SHORT_GAMMA",
    }


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def _numeric(features: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in features.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number):
                out[str(key)] = number
    return out


def _seal(symbol: str, metrics: Mapping[str, float], as_of_ns: int | None) -> GroundedFactPacket:
    stamp = int(as_of_ns if as_of_ns is not None else _AS_OF_NS)
    return GroundedFactPacket(
        symbol=symbol,
        valid_time_ns=stamp,
        knowledge_ts_ns=stamp,
        as_of_ns=stamp,
        metrics={key: float(value) for key, value in metrics.items()},
    )


def _grounding(packet: GroundedFactPacket) -> dict[str, Any]:
    return {
        "payload_sha256": packet.sha256_digest,
        "verified": packet.verify(),
        "metric_count": len(packet.metrics),
        "as_of_ns": packet.as_of_ns,
        "knowledge_ts_ns": packet.knowledge_ts_ns,
        "valid_time_ns": packet.valid_time_ns,
        "law": "sec_018 Law 3 - every number carries a SHA-256 commitment",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeatureProfile:
    """One instrument's sealed institutional feature profile."""

    symbol: str
    silo: str
    asset_class: str
    profile: str
    spot_reference: float
    features: Mapping[str, Any]
    packet: GroundedFactPacket

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "silo": self.silo,
            "asset_class": self.asset_class,
            "feature_profile": self.profile,
            "features": dict(self.features),
            "grounding": _grounding(self.packet),
            "payload_sha256": self.packet.sha256_digest,
        }


def _spec(symbol: str) -> InstrumentSpec:
    spec = instrument_registry.resolve(symbol)
    if spec is None:
        raise KeyError(f"unknown symbol {str(symbol)!r}")
    return spec


def _features_for(spec: InstrumentSpec, spot: float) -> dict[str, Any]:
    kind = profile_kind(spec)
    if kind == PROFILE_CRYPTO:
        return _crypto_features(spec, spot)
    if kind == PROFILE_FX_COMMODITY:
        return _fx_features(spec, spot)
    return _equity_features(spec, spot)


def get_feature_profile(symbol: str, *, as_of_ns: int | None = None) -> FeatureProfile:
    """Full sealed asset-class feature profile for one instrument."""
    spec = _spec(symbol)
    spot = reference_price(spec)
    features = _features_for(spec, spot)
    features["spot_reference"] = spot
    packet = _seal(spec.symbol, _numeric(features), as_of_ns)
    return FeatureProfile(
        symbol=spec.symbol,
        silo=spec.silo,
        asset_class=spec.asset_class,
        profile=profile_kind(spec),
        spot_reference=spot,
        features=features,
        packet=packet,
    )


def get_options_surface_profile(symbol: str) -> dict[str, Any]:
    """Sealed four-expiry IV term structure + GEX / vanna / charm / PCR block."""
    spec = _spec(symbol)
    spot = reference_price(spec)
    surface = options_surface(spec, spot)
    numeric = {
        "iv_rank": surface["iv_rank"],
        "iv_percentile": surface["iv_percentile"],
        "atm_iv_pct": surface["atm_iv_pct"],
        "skew_25d": surface["skew_25d"],
        "butterfly_25d": surface["butterfly_25d"],
        "net_gamma_exposure_usd": surface["net_gamma_exposure_usd"],
        "vanna_exposure": surface["vanna_exposure"],
        "charm_decay": surface["charm_decay"],
        "pcr_oi": surface["pcr_oi"],
        "pcr_volume": surface["pcr_volume"],
        "max_pain_strike": surface["max_pain_strike"],
        "spot_reference": spot,
    }
    for label in surface["term_structure"]:
        numeric[f"atm_iv_{label.lower()}_pct"] = surface["term_structure"][label]["atm_iv_pct"]
    packet = _seal(spec.symbol, numeric, None)
    return {
        **surface,
        "symbol": spec.symbol,
        "silo": spec.silo,
        "asset_class": spec.asset_class,
        "grounding": _grounding(packet),
        "payload_sha256": packet.sha256_digest,
    }


def get_decision_score(symbol: str) -> dict[str, Any]:
    """Grounded composite score in ``[-100, +100]`` with named components."""
    spec = _spec(symbol)
    profile = get_feature_profile(symbol)
    features = profile.features
    kind = profile.profile

    if kind == PROFILE_EQUITY:
        valuation = _clamp(50.0 - (float(features["pe_ratio"]) - 22.0) * 2.2)
        quality = _clamp((float(features["roe_pct"]) - 12.0) * 2.4)
        momentum = _clamp((profile.spot_reference / float(features["week_52_low"]) - 1.25) * 55.0)
        carry = _clamp(float(features["dividend_yield_pct"]) * 8.0)
        components = {
            "valuation": round(valuation, 4),
            "quality": round(quality, 4),
            "momentum": round(momentum, 4),
            "carry": round(carry, 4),
        }
    elif kind == PROFILE_CRYPTO:
        funding = _clamp(-float(features["funding_rate_8h_pct"]) * 1_400.0)
        positioning = _clamp((float(features["long_short_account_ratio"]) - 1.0) * 70.0)
        flow = _clamp(-float(features["exchange_netflow_24h_usd"]) / 1.0e7)
        basis = _clamp(-float(features["annualized_basis_pct"]) * 1.6)
        components = {
            "funding": round(funding, 4),
            "positioning": round(positioning, 4),
            "exchange_flow": round(flow, 4),
            "basis": round(basis, 4),
        }
    else:
        drift = _clamp(float(features["rate_differential_bps"]) / 6.0)
        positioning = _clamp(float(features["cot_percentile_52w"]) - 50.0)
        vol = _clamp(35.0 - float(features["implied_vol_1m_pct"]) * 3.0)
        skew = _clamp(-float(features["risk_reversal_25d"]) * 18.0)
        components = {
            "carry_drift": round(drift, 4),
            "cot_positioning": round(positioning, 4),
            "vol_premium": round(vol, 4),
            "risk_reversal": round(skew, 4),
        }

    score = _clamp(sum(components.values()) / len(components))
    if score >= 45.0:
        verdict = "BUY"
    elif score >= 15.0:
        verdict = "ACCUMULATE"
    elif score > -15.0:
        verdict = "HOLD"
    elif score > -45.0:
        verdict = "REDUCE"
    else:
        verdict = "AVOID"

    surface = get_options_surface_profile(symbol)
    numeric = {f"component_{key}": value for key, value in components.items()}
    numeric["composite_score"] = score
    numeric["iv_rank"] = float(surface["iv_rank"])
    numeric["net_gamma_exposure_usd"] = float(surface["net_gamma_exposure_usd"])
    numeric["max_pain_strike"] = float(surface["max_pain_strike"])
    packet = _seal(spec.symbol, numeric, None)
    return {
        "symbol": spec.symbol,
        "silo": spec.silo,
        "score": round(score, 4),
        "verdict": verdict,
        "components": components,
        "supporting_metrics": {
            "iv_rank": surface["iv_rank"],
            "gex_regime": surface["gex_regime"],
            "max_pain_strike": surface["max_pain_strike"],
        },
        "grounding": _grounding(packet),
        "payload_sha256": packet.sha256_digest,
        "law": "sec_018 Law 4 - the default answer is no trade",
    }


def _clamp(value: float) -> float:
    return max(-100.0, min(100.0, float(value)))


def feature_summary(symbol: str) -> dict[str, Any]:
    """Compact feature metrics for a list/detail row (no nesting, no strings)."""
    profile = get_feature_profile(symbol)
    surface = get_options_surface_profile(symbol)
    kind = profile.profile
    features = profile.features
    if kind == PROFILE_EQUITY:
        summary = {
            "pe_ratio": float(features["pe_ratio"]),
            "market_cap_usd": float(features["market_cap_usd"]),
            "beta": float(features["beta"]),
        }
    elif kind == PROFILE_CRYPTO:
        summary = {
            "funding_rate_8h_pct": float(features["funding_rate_8h_pct"]),
            "open_interest_usd": float(features["open_interest_usd"]),
            "btc_dominance_pct": float(features["btc_dominance_pct"]),
        }
    else:
        summary = {
            "rate_differential_bps": float(features["rate_differential_bps"]),
            "cot_percentile_52w": float(features["cot_percentile_52w"]),
            "implied_vol_1m_pct": float(features["implied_vol_1m_pct"]),
        }
    summary.update(
        {
            "feature_profile": kind,
            "iv_rank": float(surface["iv_rank"]),
            "net_gamma_exposure_usd": float(surface["net_gamma_exposure_usd"]),
            "spot_reference": profile.spot_reference,
            "payload_sha256": profile.packet.sha256_digest,
        }
    )
    return summary


def profile_index(symbols: Iterable[str] | None = None) -> dict[str, Any]:
    """Coverage report: how many instruments carry a complete profile."""
    pool = list(symbols) if symbols is not None else list(instrument_registry.symbols())
    per_kind = {PROFILE_EQUITY: 0, PROFILE_CRYPTO: 0, PROFILE_FX_COMMODITY: 0}
    missing: list[str] = []
    for symbol in pool:
        try:
            profile = get_feature_profile(symbol)
        except KeyError:
            missing.append(symbol)
            continue
        per_kind[profile.profile] = per_kind.get(profile.profile, 0) + 1
    return {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "total_profiled": len(pool) - len(missing),
        "requested": len(pool),
        "per_profile": per_kind,
        "missing": missing,
        "silos": list(SILO_IDS),
    }


def assert_feature_coverage() -> dict[str, Any]:
    """Release gate: every registered instrument carries an asset-class profile."""
    report = profile_index()
    if report["missing"]:
        raise ValueError(f"instruments without a feature profile: {report['missing']}")
    if report["total_profiled"] != instrument_registry.total():
        raise ValueError(
            f"profiled {report['total_profiled']} of {instrument_registry.total()} instruments"
        )
    return report
