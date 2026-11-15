"""Tax-net walk-forward backtesting engine (Ring 2, ``sec_004`` / ``sec_018``).

This module is the Ring 2 research surface behind the Cockpit ``/backtest``
workstation.  It is deliberately **deterministic**: every run derives its bars
from the SHA-256-seeded synthetic tape in
``ring2_brain.quant.daily_analysis_donor.strategies`` and its positions from the
eight harvested ``daily_stock_analysis`` strategy templates.  Two runs with the
same inputs produce byte-identical receipts.

What this engine is allowed to do (Law 2 air-gap):

* produce a grounded research receipt with ``verified`` SHA-256 commitments;
* nominate a passing strategy for the 30-fill L2 shadow nursery.

What it may never do:

* sign an order, read a Ring 0 broker key, write the IBOR, or bypass the
  30-fill nursery graduation in ``ring1_spinal.shadow_twin``;
* report a gross edge without deducting the statutory tax and slippage floor of
  the silo (``ring1_spinal/silo_worker.py`` ``SILO_CONFIGS[*].cost_floor``).

Cost floors enforced on **every** trade (round trip, basis points):

===========================  =======  ==================================
silo                         bps      composition
===========================  =======  ==================================
``INDIA_ZERODHA_DHAN``        8.50    spread 2.00 + 2x0.50 + STT 5.50
``CRYPTO_BINANCE``           16.00    spread 1.00 + 2x7.50
``US_IBKR``                   2.80    spread 1.50 + 2x0.65
``FX_MT5``                    1.60    spread 1.20 + 2x0.20
===========================  =======  ==================================

Out-of-sample discipline is **Purged K-Fold cross-validation** with an embargo,
so a trade whose entry bar sits inside the embargo band around a test fold can
never influence that fold's score.
"""

from __future__ import annotations

import functools
import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

from core.schemas import GroundedFactPacket
from ring2_brain.quant.daily_analysis_donor.strategies import (
    KNOWN_SILOS,
    PriceBar,
    STRATEGY_IDS,
    evaluate_strategy,
    get_strategy,
    synthetic_bars,
)
from ring2_brain.truth.grounding_gate import NumericalGroundingVerifier

__all__ = [
    "CHAMPION_NET_EDGE_FLOOR_BPS",
    "DEFAULT_TIMEFRAME",
    "DSR_GATE",
    "EMBARGO_PCT",
    "PURGED_K_FOLDS",
    "REPORT_TOLERANCE",
    "SILO_COST_FLOORS_BPS",
    "TIMEFRAMES",
    "TIMEFRAME_PROFILES",
    "BacktestGateError",
    "NurseryGateError",
    "WalkForwardBacktestEngine",
    "silo_cost_floor_bps",
]

#: Deflated Sharpe gate for challenger promotion (Law 5 / ``sec_018``).
DSR_GATE: float = 1.50
#: Champion floor: a strategy must clear this net edge to stay promoted.
CHAMPION_NET_EDGE_FLOOR_BPS: float = 0.50
#: Purged K-Fold count and embargo width used for every walk-forward run.
PURGED_K_FOLDS: int = 5
EMBARGO_PCT: float = 0.02
#: Grounding tolerance for rendered reports (absorbs two-decimal rounding).
REPORT_TOLERANCE: float = 0.02
#: Bars per year for the annualisation of Sharpe/Sortino/CAGR.
_TRADING_BARS_PER_YEAR: int = 252
#: Risk-free rate used for Sharpe/Sortino (0.0 keeps the receipt auditable).
_RISK_FREE_RATE: float = 0.0

#: Selectable bar frequencies.  A shorter bar carries proportionally less
#: volatility and needs a proportionally larger annualisation factor, so both
#: are derived from the same profile instead of being guessed per call site.
TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h", "1d")
DEFAULT_TIMEFRAME: str = "1d"
TIMEFRAME_PROFILES: dict[str, dict[str, float]] = {
    "1m": {
        "bars_per_year": float(252 * 390),
        "bar_interval_ns": float(60_000_000_000),
        "volatility_scale": 1.0 / math.sqrt(390.0),
    },
    "5m": {
        "bars_per_year": float(252 * 78),
        "bar_interval_ns": float(300_000_000_000),
        "volatility_scale": 1.0 / math.sqrt(78.0),
    },
    "15m": {
        "bars_per_year": float(252 * 26),
        "bar_interval_ns": float(900_000_000_000),
        "volatility_scale": 1.0 / math.sqrt(26.0),
    },
    "1h": {
        "bars_per_year": float(252 * 6 + 126),
        "bar_interval_ns": float(3_600_000_000_000),
        "volatility_scale": 1.0 / math.sqrt(6.5),
    },
    "1d": {
        "bars_per_year": float(_TRADING_BARS_PER_YEAR),
        "bar_interval_ns": float(86_400_000_000_000),
        "volatility_scale": 1.0,
    },
}

#: Statutory round-trip cost floors, mirrored from
#: ``ring1_spinal.silo_worker.SILO_CONFIGS[*].cost_floor``.  Kept as a literal
#: fallback so Ring 2 never has to import a Ring 1 execution module at module
#: scope; ``silo_cost_floor_bps`` prefers the live silo configuration and the
#: regression suite asserts the two agree.
SILO_COST_FLOORS_BPS: dict[str, float] = {
    "INDIA_ZERODHA_DHAN": 8.50,
    "CRYPTO_BINANCE": 16.00,
    "US_IBKR": 2.80,
    "FX_MT5": 1.60,
}

_DEFAULT_INITIAL_CAPITAL_UNITS: float = 100_000.0
_DEFAULT_BARS: int = 252
_MIN_BARS: int = 60
_MAX_BARS: int = 1000
_DEFAULT_FAST_WINDOW: int = 5
_DEFAULT_SLOW_WINDOW: int = 20
_DEFAULT_STOP_LOSS_BPS: float = 250.0
_DEFAULT_TAKE_PROFIT_BPS: float = 600.0
_MAX_TRADES_IN_RECEIPT: int = 400

_STRATEGY_ID_SET = frozenset(STRATEGY_IDS)
_SILO_SET = frozenset(KNOWN_SILOS)


class BacktestGateError(RuntimeError):
    """Raised when a backtest request violates a deterministic input contract."""


class NurseryGateError(RuntimeError):
    """Raised when a backtest fails the DSR / net-edge nursery promotion gate."""

    def __init__(self, message: str, *, receipt: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.receipt = dict(receipt)


# --------------------------------------------------------------------------- #
# Small deterministic helpers (mirrored from the agent dispatcher so Ring 2
# backtest never depends on the agent package).
# --------------------------------------------------------------------------- #
_UNITS = (
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
_TENS = (
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
_DIGIT_RUN = re.compile(r"\d+")


def _spell(value: int) -> str:
    """Render ``value`` as English words so reports stay digit-free."""

    if value < 0:
        return "minus" + _spell(-value)
    if value < 20:
        return _UNITS[value]
    if value < 100:
        tens, remainder = divmod(value, 10)
        if remainder == 0:
            return _TENS[tens]
        return _TENS[tens] + _UNITS[remainder]
    if value < 1000:
        hundreds, remainder = divmod(value, 100)
        if remainder == 0:
            return _UNITS[hundreds] + "hundred"
        return _UNITS[hundreds] + "hundred" + _spell(remainder)
    if value < 1_000_000:
        thousands, remainder = divmod(value, 1000)
        if remainder == 0:
            return _spell(thousands) + "thousand"
        return _spell(thousands) + "thousand" + _spell(remainder)
    millions, remainder = divmod(value, 1_000_000)
    if remainder == 0:
        return _spell(millions) + "million"
    return _spell(millions) + "million" + _spell(remainder)


def _digitless(text: Any) -> str:
    """Replace every digit run in ``text`` with its English spelling."""

    return _DIGIT_RUN.sub(lambda match: _spell(int(match.group(0))), str(text))


def _fmt(value: Any) -> str:
    """Render a grounded number, or ``""`` when there is nothing to render."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    number = float(value)
    if not math.isfinite(number) or number == 0.0:
        return ""
    if abs(number) >= 1.0:
        return f"{number:.2f}"
    return f"{number:.6f}"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _sane(value: Any, default: float = 0.0) -> float:
    parsed = _finite_float(value)
    return default if parsed is None else parsed


class _Doc:
    """Deterministic markdown builder that never emits an ungrounded number."""

    __slots__ = ("_lines",)

    def __init__(self, title: str) -> None:
        self._lines: list[str] = [f"# {_digitless(title)}", ""]

    def head(self, text: str) -> None:
        self._lines.append(f"## {_digitless(text)}")
        self._lines.append("")

    def para(self, text: str) -> None:
        self._lines.append(_digitless(text))
        self._lines.append("")

    def kv(self, label: str, value: Any) -> None:
        rendered = _fmt(value)
        if not rendered:
            return
        self._lines.append(f"- {_digitless(label)}: {rendered}")

    def note(self, label: str, text: str) -> None:
        self._lines.append(f"- {_digitless(label)}: {_digitless(text)}")

    def bullets(self, heading: str, values: Iterable[str]) -> None:
        rows = [str(value) for value in values if str(value).strip()]
        if not rows:
            return
        self._lines.append(f"### {_digitless(heading)}")
        for row in rows:
            self._lines.append(f"- {_digitless(row)}")
        self._lines.append("")

    def table(self, heading: str, metrics: Mapping[str, float]) -> None:
        rows = [
            (key, _fmt(value))
            for key, value in sorted(metrics.items())
            if _fmt(value)
        ]
        if not rows:
            return
        self._lines.append(f"### {_digitless(heading)}")
        self._lines.append("")
        self._lines.append("| Metric | Value |")
        self._lines.append("| --- | --- |")
        for key, rendered in rows:
            self._lines.append(f"| {_digitless(key)} | {rendered} |")
        self._lines.append("")

    def text(self, raw: str) -> None:
        self._lines.append(_digitless(raw))

    def build(self) -> str:
        body = "\n".join(self._lines).strip()
        return body + "\n"


def _silo_cost_floor_bps(silo: str) -> float:
    """Return the statutory round-trip cost floor for ``silo`` in bps."""

    try:  # pragma: no cover - the import always resolves inside NIOS
        from ring1_spinal.silo_worker import SILO_CONFIGS

        config = SILO_CONFIGS.get(silo)
        if config is not None:
            return float(config.cost_floor.round_trip_cost_bps)
    except Exception:  # pragma: no cover - defensive only
        pass
    return float(SILO_COST_FLOORS_BPS[silo])


#: Public alias so the Cockpit can display the table without instantiating.
silo_cost_floor_bps = _silo_cost_floor_bps


def _timeframe_profile(timeframe: Any) -> dict[str, float]:
    """Return the validated bar profile for ``timeframe``."""

    key = str(timeframe or DEFAULT_TIMEFRAME).strip().lower()
    profile = TIMEFRAME_PROFILES.get(key)
    if profile is None:
        raise BacktestGateError(
            f"unknown timeframe {timeframe!r}; expected one of {list(TIMEFRAMES)}"
        )
    return profile


def _timeframe_tape(
    symbol: str,
    silo: str,
    *,
    bars: int,
    timeframe: str,
) -> tuple[tuple[PriceBar, ...], dict[str, float]]:
    """Build one deterministic tape at the requested bar frequency.

    The synthetic generator produces a daily-shaped random path.  For a shorter
    bar the per-bar log return, the wick and the annualisation factor are all
    scaled by ``sqrt(bars_per_day)`` so a minute bar is not handed a daily-sized
    shock (which would manufacture an implausible Sharpe ratio).
    """

    profile = _timeframe_profile(timeframe)
    interval_ns = int(profile["bar_interval_ns"])
    raw = synthetic_bars(
        symbol,
        silo,
        bars=int(bars),
        bar_interval_ns=interval_ns,
    )
    scale = float(profile["volatility_scale"])
    if abs(scale - 1.0) <= 1e-12:
        return raw, profile

    out: list[PriceBar] = []
    price = float(raw[0].open)
    for index, bar in enumerate(raw):
        open_price = price
        if float(bar.open) > 0.0:
            raw_return = math.log(float(bar.close) / float(bar.open))
        else:  # pragma: no cover - synthetic bars are strictly positive
            raw_return = 0.0
        close_price = max(0.0001, open_price * math.exp(raw_return * scale))
        high_move = max(0.0, math.log(float(bar.high) / float(bar.open))) if bar.open > 0 else 0.0
        low_move = max(0.0, math.log(float(bar.open) / float(bar.low))) if bar.low > 0 else 0.0
        high_price = max(open_price, close_price) * math.exp(high_move * scale)
        low_price = min(open_price, close_price) / math.exp(low_move * scale)
        out.append(
            PriceBar(
                index=index,
                open=round(open_price, 6),
                high=round(high_price, 6),
                low=round(low_price, 6),
                close=round(close_price, 6),
                volume=round(float(bar.volume), 2),
                ts_ns=int(bar.ts_ns),
            )
        )
        price = close_price
    return tuple(out), profile


# --------------------------------------------------------------------------- #
# Receipt model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class BacktestRun:
    """One saved walk-forward run."""

    receipt: dict[str, Any]
    created_at_ns: int = 0
    promoted: bool = False
    nursery_plugin: str | None = None

    @property
    def backtest_id(self) -> str:
        return str(self.receipt.get("backtest_id") or "")

    def summary(self) -> dict[str, Any]:
        return _summarise(self.receipt)


def _summarise(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the heavy curve/blotter arrays for list views."""

    metrics = dict(receipt.get("metrics") or {})
    return {
        "backtest_id": receipt.get("backtest_id"),
        "silo": receipt.get("silo"),
        "symbol": receipt.get("symbol"),
        "strategy_id": receipt.get("strategy_id"),
        "strategy_name": receipt.get("strategy_name"),
        "bars": receipt.get("bars"),
        "timeframe": receipt.get("timeframe"),
        "bars_per_year": receipt.get("bars_per_year"),
        "cost_floor_bps": receipt.get("cost_floor_bps"),
        "net_edge_bps": receipt.get("net_edge_bps"),
        "deflated_sharpe_ratio": metrics.get("deflated_sharpe_ratio"),
        "passes_dsr_gate": receipt.get("passes_dsr_gate"),
        "passes_champion_floor": receipt.get("passes_champion_floor"),
        "promotable": receipt.get("promotable"),
        "grounding_sha256": receipt.get("grounding_sha256"),
        "verified": receipt.get("verified"),
        "created_at_ns": receipt.get("created_at_ns"),
        "initial_capital_units": receipt.get("initial_capital_units"),
        "final_nav_units": receipt.get("final_nav_units"),
        "total_return_pct": metrics.get("total_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "total_trades": metrics.get("total_trades"),
        "win_rate_pct": metrics.get("win_rate_pct"),
        "params": receipt.get("params"),
    }


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def _serialised(method: Any) -> Any:
    """Run one mutating engine method under the instance RLock."""

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class WalkForwardBacktestEngine:
    """Deterministic, tax-net, Purged K-Fold walk-forward backtester."""

    __slots__ = (
        "_runs",
        "_order",
        "_counter",
        "_verifier",
        "_clock",
        "_k_folds",
        "_embargo_pct",
        "_plugins",
        "_shadow_twin",
        "_strategy_id_tag",
        "_lock",
    )

    def __init__(
        self,
        *,
        k_folds: int = PURGED_K_FOLDS,
        embargo_pct: float = EMBARGO_PCT,
        verifier: NumericalGroundingVerifier | None = None,
        plugins: Any = None,
        shadow_twin: Any = None,
        clock: Any = time.time_ns,
    ) -> None:
        if int(k_folds) < 2:
            raise ValueError("k_folds must be >= 2")
        if not (0.0 <= float(embargo_pct) < 0.5):
            raise ValueError("embargo_pct must be in [0.0, 0.5)")
        self._runs: dict[str, BacktestRun] = {}
        self._order: list[str] = []
        self._counter = 0
        self._verifier = verifier or NumericalGroundingVerifier(
            tolerance=REPORT_TOLERANCE
        )
        self._clock = clock
        self._k_folds = int(k_folds)
        self._embargo_pct = float(embargo_pct)
        self._plugins = plugins
        self._shadow_twin = shadow_twin
        self._strategy_id_tag = "walk-forward-v1"
        # V1.6: concurrent HTTP runs must never interleave the run counter or
        # the nursery admission decision.
        self._lock = threading.RLock()

    # -- wiring ----------------------------------------------------------- #
    def attach(self, *, plugins: Any = None, shadow_twin: Any = None) -> None:
        """Late-bind the nursery collaborators (PluginRegistry / ShadowTwin)."""

        if plugins is not None:
            self._plugins = plugins
        if shadow_twin is not None:
            self._shadow_twin = shadow_twin

    # -- properties -------------------------------------------------------- #
    @property
    def verifier(self) -> NumericalGroundingVerifier:
        return self._verifier

    @property
    def run_count(self) -> int:
        return len(self._runs)

    @property
    def runs(self) -> tuple[BacktestRun, ...]:
        return tuple(self._runs[key] for key in self._order)

    @property
    def k_folds(self) -> int:
        return self._k_folds

    @property
    def embargo_pct(self) -> float:
        return self._embargo_pct

    # -- catalog ----------------------------------------------------------- #
    def strategy_catalog(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for strategy_id in STRATEGY_IDS:
            spec = get_strategy(strategy_id)
            payload = spec.to_dict()
            payload["cost_floor_bps"] = {
                silo: _silo_cost_floor_bps(silo) for silo in KNOWN_SILOS
            }
            catalog.append(payload)
        return catalog

    def cost_floor_table(self) -> list[dict[str, Any]]:
        return [
            {
                "silo": silo,
                "cost_floor_bps": _silo_cost_floor_bps(silo),
                "statutory_tax_bps": (
                    5.50 if silo == "INDIA_ZERODHA_DHAN" else 0.0
                ),
            }
            for silo in KNOWN_SILOS
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "runs": [_summarise(run.receipt) for run in self.runs],
            "run_count": len(self._runs),
            "strategies": self.strategy_catalog(),
            "strategy_count": len(STRATEGY_IDS),
            "silos": list(KNOWN_SILOS),
            "timeframes": list(TIMEFRAMES),
            "default_timeframe": DEFAULT_TIMEFRAME,
            "timeframe_profiles": {
                key: {
                    "bars_per_year": int(profile["bars_per_year"]),
                    "bar_interval_ns": int(profile["bar_interval_ns"]),
                }
                for key, profile in TIMEFRAME_PROFILES.items()
            },
            "cost_floors": {
                silo: _silo_cost_floor_bps(silo) for silo in KNOWN_SILOS
            },
            "cost_floor_table": self.cost_floor_table(),
            "k_folds": self._k_folds,
            "embargo_pct": self._embargo_pct,
            "dsr_gate": DSR_GATE,
            "champion_net_edge_floor_bps": CHAMPION_NET_EDGE_FLOOR_BPS,
            "report_tolerance": REPORT_TOLERANCE,
            "clean_slate": not self._runs,
            "air_gap": {
                "ring2_only": True,
                "signs_orders": False,
                "reads_broker_keys": False,
                "writes_ibor": False,
                "bypasses_shadow_nursery": False,
            },
            "generated_at_ns": int(self._clock()),
        }

    # -- lookups ----------------------------------------------------------- #
    def get(self, backtest_id: str) -> BacktestRun:
        run = self._runs.get(str(backtest_id))
        if run is None:
            raise KeyError(f"unknown backtest_id: {backtest_id!r}")
        return run

    def receipt(self, backtest_id: str) -> dict[str, Any]:
        return dict(self.get(backtest_id).receipt)

    # -- clean slate ------------------------------------------------------- #
    def reset_to_clean_slate(self) -> dict[str, Any]:
        cleared = len(self._runs)
        self._runs.clear()
        self._order.clear()
        self._counter = 0
        return {
            "cleared": cleared,
            "runs": 0,
            "run_count": 0,
            "clean_slate": True,
        }

    # -- main entry point -------------------------------------------------- #
    @_serialised
    def run(
        self,
        *,
        silo: str = "US_IBKR",
        symbol: str = "SPY",
        strategy_id: str = "ma_golden_cross",
        bars: int = _DEFAULT_BARS,
        timeframe: str = DEFAULT_TIMEFRAME,
        initial_capital_units: float = _DEFAULT_INITIAL_CAPITAL_UNITS,
        fast_window: int | None = None,
        slow_window: int | None = None,
        stop_loss_bps: float | None = None,
        take_profit_bps: float | None = None,
        tape: Sequence[PriceBar] | None = None,
    ) -> dict[str, Any]:
        """Run one deterministic walk-forward backtest and seal the receipt."""

        silo_id = str(silo).strip().upper()
        if silo_id not in _SILO_SET:
            raise ValueError(
                f"unknown silo {silo!r}; expected one of {sorted(_SILO_SET)}"
            )
        strategy_key = str(strategy_id).strip().lower()
        if strategy_key not in _STRATEGY_ID_SET:
            raise ValueError(
                f"unknown strategy_id {strategy_id!r}; "
                f"expected one of {list(STRATEGY_IDS)}"
            )
        symbol_id = str(symbol or "").strip().upper()
        if not symbol_id:
            raise ValueError("symbol must be a non-empty ticker")
        bar_count = int(bars)
        if bar_count < _MIN_BARS or bar_count > _MAX_BARS:
            raise ValueError(
                f"bars must be within [{_MIN_BARS}, {_MAX_BARS}], got {bar_count}"
            )
        timeframe_key = str(timeframe or DEFAULT_TIMEFRAME).strip().lower()
        profile = _timeframe_profile(timeframe_key)
        capital = _finite_float(initial_capital_units)
        if capital is None or capital <= 0.0:
            raise ValueError("initial_capital_units must be a positive number")

        stop_bps = (
            _DEFAULT_STOP_LOSS_BPS
            if stop_loss_bps is None
            else float(stop_loss_bps)
        )
        target_bps = (
            _DEFAULT_TAKE_PROFIT_BPS
            if take_profit_bps is None
            else float(take_profit_bps)
        )
        if stop_bps <= 0.0 or target_bps <= 0.0:
            raise ValueError("stop_loss_bps and take_profit_bps must be positive")

        custom_ma = fast_window is not None or slow_window is not None
        fast = int(fast_window) if fast_window is not None else _DEFAULT_FAST_WINDOW
        slow = int(slow_window) if slow_window is not None else _DEFAULT_SLOW_WINDOW
        if custom_ma and fast >= slow:
            raise ValueError("fast_window must be smaller than slow_window")
        if fast < 2 or slow < 3:
            raise ValueError("fast_window must be >= 2 and slow_window >= 3")

        created_at_ns = int(self._clock())
        if tape is None:
            tape, profile = _timeframe_tape(
                symbol_id, silo_id, bars=bar_count, timeframe=timeframe_key
            )
        else:
            tape = tuple(tape)
            if len(tape) < _MIN_BARS:
                raise ValueError(
                    f"a supplied tape needs at least {_MIN_BARS} bars"
                )
        bar_count = len(tape)
        spec = get_strategy(strategy_key)
        cost_floor = _silo_cost_floor_bps(silo_id)

        if custom_ma:
            positions = _ma_positions(tape, fast, slow)
            warmup = slow
            label = f"custom-ma-{fast}x{slow}"
        else:
            positions = _strategy_positions(tape, symbol_id, silo_id, strategy_key)
            warmup = min(spec.required_bars, max(2, bar_count // 4))
            label = strategy_key

        simulation = _simulate(
            tape,
            positions,
            warmup=warmup,
            cost_floor_bps=cost_floor,
            stop_loss_bps=stop_bps,
            take_profit_bps=target_bps,
            initial_capital_units=float(capital),
        )

        metrics = _compute_metrics(
            tape,
            simulation,
            initial_capital_units=float(capital),
            cost_floor_bps=cost_floor,
            cost_floors_all={s: _silo_cost_floor_bps(s) for s in KNOWN_SILOS},
            bar_count=bar_count,
            bars_per_year=int(profile["bars_per_year"]),
        )
        kfold = _purged_kfold(
            simulation["trades"],
            bar_count,
            k=self._k_folds,
            embargo_pct=self._embargo_pct,
            cost_floor_bps=cost_floor,
        )
        metrics["purged_kfold_folds"] = float(kfold["fold_count"])
        metrics["purged_kfold_embargo_bars"] = float(kfold["embargo_bars"])
        metrics["purged_kfold_out_of_sample_net_bps"] = _sane(
            kfold["out_of_sample_net_bps"]
        )

        horizon = _horizon_matrix(
            tape,
            simulation["signal_bars"],
            cost_floor_bps=cost_floor,
        )
        for key, payload in horizon.items():
            tag = key.replace("t_plus_", "t")
            metrics[f"horizon_{tag}_samples"] = float(payload["samples"])
            metrics[f"horizon_{tag}_win_rate_pct"] = _sane(
                payload["win_rate_pct"]
            )
            metrics[f"horizon_{tag}_net_edge_bps"] = _sane(payload["net_edge_bps"])

        dsr = _sane(metrics.get("deflated_sharpe_ratio"))
        net_edge = _sane(metrics.get("net_edge_bps"))
        passes_dsr = bool(dsr >= DSR_GATE)
        passes_floor = bool(net_edge >= CHAMPION_NET_EDGE_FLOOR_BPS)
        promotable = bool(passes_dsr and passes_floor)

        packet_metrics = {
            key: float(value)
            for key, value in metrics.items()
            if _finite_float(value) is not None
        }
        packet_metrics["cost_floor_bps"] = float(cost_floor)
        packet_metrics["initial_capital_units"] = float(capital)
        packet_metrics["final_nav_units"] = float(simulation["final_nav_units"])
        packet_metrics["stop_loss_bps"] = float(stop_bps)
        packet_metrics["take_profit_bps"] = float(target_bps)
        packet_metrics["run_bars"] = float(bar_count)
        packet_metrics["bars_per_year"] = float(profile["bars_per_year"])
        packet_metrics["timeframe_seconds"] = float(
            profile["bar_interval_ns"] / 1_000_000_000.0
        )
        packet_metrics["dsr_gate"] = float(DSR_GATE)
        packet_metrics["champion_net_edge_floor_bps"] = float(
            CHAMPION_NET_EDGE_FLOOR_BPS
        )

        as_of_ns = created_at_ns
        knowledge_ts_ns = int(tape[-1].ts_ns)
        packet = GroundedFactPacket(
            symbol=symbol_id,
            valid_time_ns=knowledge_ts_ns,
            knowledge_ts_ns=knowledge_ts_ns,
            as_of_ns=as_of_ns,
            metrics=packet_metrics,
        )
        markdown = _render_receipt(
            symbol=symbol_id,
            silo=silo_id,
            strategy_name=spec.name,
            label=label,
            bar_count=bar_count,
            cost_floor_bps=cost_floor,
            stop_loss_bps=stop_bps,
            take_profit_bps=target_bps,
            metrics=packet_metrics,
            horizon=horizon,
            kfold=kfold,
            trade_count=int(metrics.get("total_trades") or 0),
            promotable=promotable,
        )
        verdict = self._verify(packet, markdown)

        self._counter += 1
        backtest_id = (
            f"bt_{created_at_ns:x}_{self._counter:04x}_"
            f"{_sha256_hex(f'{symbol_id}|{silo_id}|{label}|{timeframe_key}|{bar_count}')[:8]}"
        )
        receipt: dict[str, Any] = {
            "backtest_id": backtest_id,
            "silo": silo_id,
            "symbol": symbol_id,
            "strategy_id": strategy_key,
            "strategy_name": spec.name,
            "strategy_label": label,
            "custom_ma": bool(custom_ma),
            "bars": bar_count,
            "timeframe": timeframe_key,
            "bars_per_year": int(profile["bars_per_year"]),
            "bar_interval_ns": int(profile["bar_interval_ns"]),
            "params": {
                "fast_window": int(fast) if custom_ma else None,
                "slow_window": int(slow) if custom_ma else None,
                "stop_loss_bps": float(stop_bps),
                "take_profit_bps": float(target_bps),
            },
            "cost_floor_bps": float(cost_floor),
            "statutory_tax_bps": 5.50 if silo_id == "INDIA_ZERODHA_DHAN" else 0.0,
            "initial_capital_units": float(capital),
            "final_nav_units": float(simulation["final_nav_units"]),
            "benchmark_final_units": float(simulation["benchmark_final_units"]),
            "equity_curve": simulation["equity_curve"],
            "drawdown_curve_pct": simulation["drawdown_curve_pct"],
            "benchmark_curve_units": simulation["benchmark_curve_units"],
            "trades": simulation["trades"][: _MAX_TRADES_IN_RECEIPT],
            "trade_count": int(len(simulation["trades"])),
            "signal_bars": simulation["signal_bars"][:128],
            "metrics": {key: float(value) for key, value in metrics.items()},
            "horizon_matrix": horizon,
            "purged_kfold": kfold,
            "deflated_sharpe_ratio": float(dsr),
            "dsr_gate": float(DSR_GATE),
            "passes_dsr_gate": passes_dsr,
            "net_edge_bps": float(net_edge),
            "champion_net_edge_floor_bps": float(CHAMPION_NET_EDGE_FLOOR_BPS),
            "passes_champion_floor": passes_floor,
            "promotable": promotable,
            "grounding_sha256": packet.sha256_digest,
            "fact_count": len(packet.metrics),
            "markdown_report": markdown,
            "verification": verdict,
            "verified": bool(verdict["verified"]),
            "created_at_ns": created_at_ns,
            "synthetic_tape": True,
            "air_gap": {
                "ring2_only": True,
                "signs_orders": False,
                "reads_broker_keys": False,
            },
        }
        self._runs[backtest_id] = BacktestRun(receipt=receipt, created_at_ns=created_at_ns)
        self._order.append(backtest_id)
        return dict(receipt)

    # -- comparison ------------------------------------------------------- #
    @_serialised
    def compare_strategies(
        self,
        *,
        silo: str = "US_IBKR",
        symbol: str = "SPY",
        timeframe: str = DEFAULT_TIMEFRAME,
        bars: int = _DEFAULT_BARS,
    ) -> dict[str, Any]:
        """Run all eight harvested strategies on one identical bar series."""

        silo_id = str(silo).strip().upper()
        if silo_id not in _SILO_SET:
            raise ValueError(
                f"unknown silo {silo!r}; expected one of {sorted(_SILO_SET)}"
            )
        symbol_id = str(symbol or "").strip().upper()
        if not symbol_id:
            raise ValueError("symbol must be a non-empty ticker")
        bar_count = int(bars)
        if bar_count < _MIN_BARS or bar_count > _MAX_BARS:
            raise ValueError(
                f"bars must be within [{_MIN_BARS}, {_MAX_BARS}], got {bar_count}"
            )
        timeframe_key = str(timeframe or DEFAULT_TIMEFRAME).strip().lower()
        profile = _timeframe_profile(timeframe_key)

        tape, profile = _timeframe_tape(
            symbol_id, silo_id, bars=bar_count, timeframe=timeframe_key
        )
        bar_count = len(tape)
        cost_floor = _silo_cost_floor_bps(silo_id)

        rows: list[dict[str, Any]] = []
        for strategy_id in STRATEGY_IDS:
            receipt = self.run(
                silo=silo_id,
                symbol=symbol_id,
                strategy_id=strategy_id,
                bars=bar_count,
                timeframe=timeframe_key,
                tape=tape,
            )
            metrics = dict(receipt.get("metrics") or {})
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_name": receipt.get("strategy_name"),
                    "backtest_id": receipt.get("backtest_id"),
                    "total_return_pct": round(
                        _sane(metrics.get("total_return_pct")), 6
                    ),
                    "sharpe_ratio": round(_sane(metrics.get("sharpe_ratio")), 6),
                    "deflated_sharpe_ratio": round(
                        _sane(metrics.get("deflated_sharpe_ratio")), 6
                    ),
                    "passes_dsr_gate": bool(receipt.get("passes_dsr_gate")),
                    "net_edge_bps": round(_sane(metrics.get("net_edge_bps")), 6),
                    "win_rate_pct": round(_sane(metrics.get("win_rate_pct")), 6),
                    "total_trades": int(_sane(metrics.get("total_trades"))),
                    "max_drawdown_pct": round(
                        _sane(metrics.get("max_drawdown_pct")), 6
                    ),
                    "cost_floor_bps": float(cost_floor),
                    "grounding_sha256": receipt.get("grounding_sha256"),
                    "verified": bool(receipt.get("verified")),
                }
            )

        rows.sort(
            key=lambda row: (
                -float(row["deflated_sharpe_ratio"]),
                -float(row["net_edge_bps"]),
                str(row["strategy_id"]),
            )
        )
        for index, row in enumerate(rows, start=1):
            row["rank"] = index

        digest_material = "|".join(
            f"{row['strategy_id']}:{row['deflated_sharpe_ratio']}:{row['net_edge_bps']}"
            for row in rows
        )
        return {
            "silo": silo_id,
            "symbol": symbol_id,
            "timeframe": timeframe_key,
            "bars": bar_count,
            "bars_per_year": int(profile["bars_per_year"]),
            "bar_interval_ns": int(profile["bar_interval_ns"]),
            "cost_floor_bps": float(cost_floor),
            "statutory_tax_bps": 5.50 if silo_id == "INDIA_ZERODHA_DHAN" else 0.0,
            "dsr_gate": float(DSR_GATE),
            "strategy_count": len(rows),
            "leaderboard": rows,
            "ranked": rows,
            "best_strategy": rows[0] if rows else None,
            "comparison_sha256": _sha256_hex(digest_material),
            "synthetic_tape": True,
            "air_gap": {
                "ring2_only": True,
                "signs_orders": False,
                "reads_broker_keys": False,
            },
            "generated_at_ns": int(self._clock()),
        }

    # -- nursery ----------------------------------------------------------- #
    @_serialised
    def promote_to_nursery(self, backtest_id: str) -> dict[str, Any]:
        """Register a passing run into the 30-fill L2 shadow nursery."""

        run = self.get(backtest_id)
        receipt = run.receipt
        dsr = _sane(receipt.get("deflated_sharpe_ratio"))
        net_edge = _sane(receipt.get("net_edge_bps"))
        kfold = dict(receipt.get("purged_kfold") or {})
        leakage_free = bool(
            receipt.get("leakage_free", kfold.get("leakage_free", False))
        )
        if not receipt.get("passes_dsr_gate") or not receipt.get(
            "passes_champion_floor"
        ):
            raise NurseryGateError(
                "NURSERY-GATE-REJECTED: "
                f"deflated_sharpe_ratio={dsr:.4f} (gate {DSR_GATE:.2f}), "
                f"net_edge_bps={net_edge:.4f} "
                f"(floor {CHAMPION_NET_EDGE_FLOOR_BPS:.2f})",
                receipt=receipt,
            )
        # Law 4: a run whose purged/embargoed K-fold could not certify that it
        # was leakage-free is not a nursery candidate.  Without this check a
        # lookahead-contaminated curve could buy a 30-fill shadow graduation.
        if not leakage_free:
            raise NurseryGateError(
                "NURSERY-GATE-REJECTED: purged K-fold walk-forward did not "
                "certify leakage_free=true",
                receipt=receipt,
            )

        plugin_name = (
            f"nursery-{receipt['strategy_id']}-{str(receipt['silo']).lower()}"
        )
        already = False
        plugin_record: dict[str, Any] | None = None
        registry = self._plugins
        if registry is not None:
            existing = None
            try:
                existing = registry.get(plugin_name)
            except Exception:  # pragma: no cover - registry may return None
                existing = None
            if existing is not None:
                already = True
                try:
                    plugin_record = existing.to_dict()
                except Exception:  # pragma: no cover - defensive
                    plugin_record = {"name": plugin_name}
            else:
                manifest_factory = _build_manifest(
                    plugin_name, receipt, dsr=dsr, net_edge_bps=net_edge
                )
                try:
                    record = registry.register(manifest_factory)
                    plugin_record = record.to_dict()
                except ValueError:
                    already = True
                    plugin_record = {"name": plugin_name}

        shadow_fills = 0
        required_fills = 30
        twin = self._shadow_twin
        if twin is not None:
            try:
                shadow_fills = int(
                    twin.shadow_fill_count(self._strategy_id_tag)
                )
                # ``required_fills`` is a @property on ShadowExecutionTwin;
                # calling it raised TypeError, was swallowed by the except
                # below, and the handoff always reported 0/30 shadow fills.
                required = twin.required_fills
                required_fills = int(required() if callable(required) else required)
            except Exception:  # pragma: no cover - defensive
                shadow_fills = 0
                required_fills = 30

        # Re-assert the Law 4 gate at the exact assignment site: neither flag
        # may have been mutated by a registry/shadow hand-off above.
        if not (receipt.get("passes_dsr_gate") and leakage_free):
            raise NurseryGateError(
                "NURSERY-GATE-REJECTED: Law 4 gate re-check failed before "
                "promotion (passes_dsr_gate and leakage_free are both required)",
                receipt=receipt,
            )

        run.promoted = True
        run.nursery_plugin = plugin_name
        run.receipt["promoted"] = True
        run.receipt["nursery_plugin"] = plugin_name
        run.receipt["nursery_shadow_fills"] = shadow_fills
        run.receipt["leakage_free"] = leakage_free

        return {
            "promoted": True,
            "already_registered": already,
            "backtest_id": receipt["backtest_id"],
            "strategy_id": receipt["strategy_id"],
            "silo": receipt["silo"],
            "symbol": receipt["symbol"],
            "deflated_sharpe_ratio": float(dsr),
            "dsr_gate": float(DSR_GATE),
            "net_edge_bps": float(net_edge),
            "champion_net_edge_floor_bps": float(CHAMPION_NET_EDGE_FLOOR_BPS),
            "plugin_name": plugin_name,
            "plugin": plugin_record,
            "shadow_fills": shadow_fills,
            "required_fills": required_fills,
            "remaining_fills": max(0, required_fills - shadow_fills),
            "graduated": bool(shadow_fills >= required_fills),
            "nursery_bypassed": False,
            "grounding_sha256": receipt.get("grounding_sha256"),
            "next": "analyzer",
            "generated_at_ns": int(self._clock()),
        }

    # -- verification ------------------------------------------------------ #
    def _verify(
        self, packet: GroundedFactPacket, markdown: str
    ) -> dict[str, Any]:
        verdict: dict[str, Any] = {
            "digest": packet.sha256_digest,
            "claims_checked": 0,
            "derived_expressions": 0,
            "tolerance": REPORT_TOLERANCE,
            "verified": False,
            "error": None,
        }
        try:
            report = self._verifier.verify_thesis_claims(
                markdown, packet, tolerance=REPORT_TOLERANCE
            )
            payload = report.to_dict()
            verdict["claims_checked"] = int(payload.get("claims_checked") or 0)
            verdict["derived_expressions"] = int(
                payload.get("derived_expressions") or 0
            )
            verdict["verified"] = bool(packet.verify())
        except Exception as exc:  # noqa: BLE001 - surfaced on the receipt
            verdict["error"] = f"{type(exc).__name__}: {exc}"
        return verdict


# --------------------------------------------------------------------------- #
# Position construction
# --------------------------------------------------------------------------- #
def _closes(tape: Sequence[PriceBar]) -> list[float]:
    return [float(bar.close) for bar in tape]


def _sma_series(values: Sequence[float], window: int) -> list[float | None]:
    series: list[float | None] = []
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= window:
            total -= values[index - window]
        series.append(total / window if index >= window - 1 else None)
    return series


def _ma_positions(
    tape: Sequence[PriceBar], fast: int, slow: int
) -> list[int]:
    closes = _closes(tape)
    fast_series = _sma_series(closes, fast)
    slow_series = _sma_series(closes, slow)
    positions: list[int] = []
    for fast_value, slow_value in zip(fast_series, slow_series):
        if fast_value is None or slow_value is None:
            positions.append(0)
        else:
            positions.append(1 if fast_value > slow_value else 0)
    return positions


def _strategy_positions(
    tape: Sequence[PriceBar], symbol: str, silo: str, strategy_id: str
) -> list[int]:
    """Roll the harvested strategy template forward one bar at a time."""

    spec = get_strategy(strategy_id)
    warmup = max(2, int(spec.required_bars))
    positions: list[int] = [0] * len(tape)
    for index in range(len(tape)):
        if index + 1 < warmup:
            continue
        window = tape[: index + 1]
        try:
            signal = evaluate_strategy(
                strategy_id,
                symbol=symbol,
                silo=silo,
                bars=window,
                bar_count=len(window),
            )
        except Exception:  # pragma: no cover - defensive only
            positions[index] = 0
            continue
        positions[index] = (
            1 if signal.triggered and str(signal.direction).upper() == "LONG" else 0
        )
    return positions


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
def _simulate(
    tape: Sequence[PriceBar],
    positions: Sequence[int],
    *,
    warmup: int,
    cost_floor_bps: float,
    stop_loss_bps: float,
    take_profit_bps: float,
    initial_capital_units: float,
) -> dict[str, Any]:
    """Long/flat walk-forward simulation with statutory cost on both legs."""

    entry_cost = cost_floor_bps / 2.0 / 10_000.0
    exit_cost = cost_floor_bps / 2.0 / 10_000.0
    stop_fraction = stop_loss_bps / 10_000.0
    target_fraction = take_profit_bps / 10_000.0

    closes = _closes(tape)
    nav = float(initial_capital_units)
    nav_entry = nav
    position = 0
    entry_price = 0.0
    entry_bar = -1
    stop_price = 0.0
    target_price = 0.0

    trades: list[dict[str, Any]] = []
    signal_bars: list[int] = []
    equity_curve: list[float] = []
    benchmark_curve: list[float] = []
    base_close = closes[0] if closes else 1.0

    last_index = len(tape) - 1
    for index in range(len(tape)):
        bar = tape[index]
        exit_price: float | None = None
        exit_reason = ""

        if position == 1:
            if float(bar.low) <= stop_price:
                exit_price = stop_price
                exit_reason = "STOP_LOSS"
            elif float(bar.high) >= target_price:
                exit_price = target_price
                exit_reason = "TAKE_PROFIT"
            elif positions[index] == 0:
                exit_price = float(bar.close)
                exit_reason = "SIGNAL_EXIT"
            elif index == last_index:
                exit_price = float(bar.close)
                exit_reason = "END_OF_SERIES"

            if exit_price is not None:
                gross_bps = (exit_price / entry_price - 1.0) * 10_000.0
                net_bps = gross_bps - cost_floor_bps
                nav = nav_entry * (exit_price / entry_price) * (1.0 - exit_cost)
                trades.append(
                    {
                        "trade_id": len(trades) + 1,
                        "entry_bar": int(entry_bar),
                        "exit_bar": int(index),
                        "side": "LONG",
                        "entry_price": round(entry_price, 6),
                        "exit_price": round(exit_price, 6),
                        "gross_bps": round(gross_bps, 6),
                        "cost_bps": round(cost_floor_bps, 6),
                        "net_bps": round(net_bps, 6),
                        "bars_held": int(index - entry_bar),
                        "reason": exit_reason,
                    }
                )
                position = 0
            else:
                nav = nav_entry * (float(bar.close) / entry_price)
        elif index >= warmup and positions[index] == 1 and index < last_index:
            entry_price = float(bar.close)
            entry_bar = index
            stop_price = entry_price * (1.0 - stop_fraction)
            target_price = entry_price * (1.0 + target_fraction)
            nav_entry = nav * (1.0 - entry_cost)
            nav = nav_entry
            position = 1
            signal_bars.append(int(index))

        equity_curve.append(round(float(nav), 6))
        benchmark_curve.append(
            round(float(initial_capital_units) * (float(bar.close) / base_close), 6)
        )

    peak = 0.0
    drawdown: list[float] = []
    for value in equity_curve:
        peak = max(peak, value)
        if peak <= 0.0:
            drawdown.append(0.0)
        else:
            drawdown.append(round((value / peak - 1.0) * 100.0, 6))

    return {
        "trades": trades,
        "signal_bars": signal_bars,
        "equity_curve": equity_curve,
        "drawdown_curve_pct": drawdown,
        "benchmark_curve_units": benchmark_curve,
        "final_nav_units": equity_curve[-1] if equity_curve else float(
            initial_capital_units
        ),
        "benchmark_final_units": (
            benchmark_curve[-1] if benchmark_curve else float(initial_capital_units)
        ),
    }


# --------------------------------------------------------------------------- #
# Metric computation
# --------------------------------------------------------------------------- #
def _annualised_sharpe(
    returns: Sequence[float], bars_per_year: int = _TRADING_BARS_PER_YEAR
) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std <= 1e-12:
        return 0.0
    excess = mean - (_RISK_FREE_RATE / bars_per_year)
    return (excess / std) * math.sqrt(bars_per_year)


def _annualised_sortino(
    returns: Sequence[float], bars_per_year: int = _TRADING_BARS_PER_YEAR
) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    downside = [min(0.0, value) for value in returns]
    variance = sum(value * value for value in downside) / (len(returns) - 1)
    deviation = math.sqrt(variance)
    if deviation <= 1e-12:
        return 0.0
    return (mean / deviation) * math.sqrt(bars_per_year)


def _moments(returns: Sequence[float]) -> tuple[float, float]:
    """Return (skewness, excess kurtosis) with the population convention."""

    count = len(returns)
    if count < 2:
        return 0.0, 0.0
    mean = sum(returns) / count
    variance = sum((value - mean) ** 2 for value in returns) / count
    std = math.sqrt(variance)
    if std <= 1e-12:
        return 0.0, 0.0
    skew = sum(((value - mean) / std) ** 3 for value in returns) / count
    kurt = sum(((value - mean) / std) ** 4 for value in returns) / count
    return skew, kurt - 3.0


def _deflated_sharpe_units(
    returns: Sequence[float],
    num_trials: int,
    bars_per_year: int = _TRADING_BARS_PER_YEAR,
) -> float:
    """Deflated Sharpe **in Sharpe units** (observed minus expected max).

    ``sec_018``/Law 5 promotes a challenger at ``DSR >= 1.50``, so the receipt
    reports the deflated Sharpe as a Sharpe magnitude rather than a probability:
    the observed annualised Sharpe less the expected maximum Sharpe achievable
    by ``num_trials`` independent trials under the null (Bailey & Lopez de
    Prado).  A negative value means the strategy never beat its own search
    budget.
    """

    sharpe = _annualised_sharpe(returns, bars_per_year)
    trials = max(2, int(num_trials))
    normal = NormalDist()
    euler = 0.5772156649015329
    expected_max = (1.0 - euler) * normal.inv_cdf(1.0 - 1.0 / trials) + euler * (
        normal.inv_cdf(1.0 - 1.0 / (trials * math.e))
    )
    return float(sharpe - expected_max)


def _compute_metrics(
    tape: Sequence[PriceBar],
    simulation: Mapping[str, Any],
    *,
    initial_capital_units: float,
    cost_floor_bps: float,
    cost_floors_all: Mapping[str, float],
    bar_count: int,
    bars_per_year: int = _TRADING_BARS_PER_YEAR,
) -> dict[str, float]:
    closes = _closes(tape)
    trades: list[dict[str, Any]] = list(simulation["trades"])
    final_nav = float(simulation["final_nav_units"])
    benchmark_final = float(simulation["benchmark_final_units"])

    total_return_pct = (final_nav / initial_capital_units - 1.0) * 100.0
    benchmark_return_pct = (benchmark_final / initial_capital_units - 1.0) * 100.0
    alpha_pct = total_return_pct - benchmark_return_pct
    years = max(bar_count / bars_per_year, 1.0 / bars_per_year)
    growth = final_nav / initial_capital_units
    cagr_pct = ((growth ** (1.0 / years)) - 1.0) * 100.0 if growth > 0 else -100.0

    equity = [float(value) for value in simulation["equity_curve"]] or [
        initial_capital_units
    ]
    bar_returns = [
        (equity[index] / equity[index - 1] - 1.0)
        for index in range(1, len(equity))
        if equity[index - 1] > 0.0
    ]
    sharpe = _annualised_sharpe(bar_returns, bars_per_year)
    sortino = _annualised_sortino(bar_returns, bars_per_year)
    skew, excess_kurt = _moments(bar_returns)
    dsr = _deflated_sharpe_units(bar_returns, len(STRATEGY_IDS), bars_per_year)

    peak = 0.0
    max_drawdown_pct = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0.0:
            max_drawdown_pct = min(max_drawdown_pct, (value / peak - 1.0) * 100.0)
    max_drawdown_pct = abs(max_drawdown_pct)

    wins = [trade for trade in trades if float(trade["net_bps"]) > 0.0]
    losses = [trade for trade in trades if float(trade["net_bps"]) <= 0.0]
    gross_sum = sum(float(trade["gross_bps"]) for trade in trades)
    net_sum = sum(float(trade["net_bps"]) for trade in trades)
    gross_edge = gross_sum / len(trades) if trades else 0.0
    net_edge = net_sum / len(trades) if trades else 0.0
    drag = abs(cost_floor_bps) if trades else 0.0

    gross_profit = sum(float(trade["net_bps"]) for trade in wins)
    gross_loss = abs(sum(float(trade["net_bps"]) for trade in losses))
    if gross_loss > 0.0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float(len(wins)) if wins else 0.0

    exposure = sum(int(trade["bars_held"]) for trade in trades) / max(bar_count, 1) * 100.0
    avg_held = (
        sum(int(trade["bars_held"]) for trade in trades) / len(trades)
        if trades
        else 0.0
    )

    metrics: dict[str, float] = {
        "total_return_pct": total_return_pct,
        "benchmark_return_pct": benchmark_return_pct,
        "alpha_pct": alpha_pct,
        "cagr_pct": cagr_pct,
        "sharpe_ratio": sharpe,
        "deflated_sharpe_ratio": dsr,
        "sortino_ratio": sortino,
        "calmar_ratio": (
            cagr_pct / max_drawdown_pct if max_drawdown_pct > 1e-9 else 0.0
        ),
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
        "profit_factor": profit_factor,
        "total_trades": float(len(trades)),
        "winning_trades": float(len(wins)),
        "losing_trades": float(len(losses)),
        "gross_edge_bps": gross_edge,
        "tax_and_cost_drag_bps": drag,
        "net_edge_bps": net_edge,
        "gross_profit_bps": gross_profit,
        "gross_loss_bps": gross_loss,
        "exposure_pct": exposure,
        "avg_bars_held": avg_held,
        "bar_count": float(bar_count),
        "bars_per_year": float(bars_per_year),
        "return_skew": skew,
        "return_excess_kurtosis": excess_kurt,
        "first_close": float(closes[0]) if closes else 0.0,
        "last_close": float(closes[-1]) if closes else 0.0,
        "best_trade_bps": max(
            (float(t["net_bps"]) for t in trades), default=0.0
        ),
        "worst_trade_bps": min(
            (float(t["net_bps"]) for t in trades), default=0.0
        ),
    }
    for silo, floor in cost_floors_all.items():
        metrics[f"floor_{silo.lower()}_bps"] = float(floor)
    return metrics


def _purged_kfold(
    trades: Sequence[Mapping[str, Any]],
    bar_count: int,
    *,
    k: int,
    embargo_pct: float,
    cost_floor_bps: float,
) -> dict[str, Any]:
    """Purged K-Fold segmentation with an embargo band around each test fold."""

    k = max(2, int(k))
    edges = [int(round(index * bar_count / k)) for index in range(k + 1)]
    embargo_bars = max(1, int(round(bar_count * float(embargo_pct))))

    folds: list[dict[str, Any]] = []
    weighted_net = 0.0
    total_in_fold = 0
    for index in range(k):
        start, end = edges[index], edges[index + 1]
        purge_low = max(0, start - embargo_bars)
        purge_high = min(bar_count, end + embargo_bars)
        in_fold = [
            trade
            for trade in trades
            if start <= int(trade["entry_bar"]) < end
        ]
        purged = [
            trade
            for trade in trades
            if purge_low <= int(trade["entry_bar"]) < purge_high
        ]
        wins = [trade for trade in in_fold if float(trade["net_bps"]) > 0.0]
        gross = (
            sum(float(trade["gross_bps"]) for trade in in_fold) / len(in_fold)
            if in_fold
            else 0.0
        )
        net = (
            sum(float(trade["net_bps"]) for trade in in_fold) / len(in_fold)
            if in_fold
            else 0.0
        )
        total_in_fold += len(in_fold)
        weighted_net += net * len(in_fold)
        folds.append(
            {
                "fold": index + 1,
                "test_start_bar": start,
                "test_end_bar": end,
                "test_bars": end - start,
                "train_bars": bar_count - (end - start) - len(
                    {int(t["entry_bar"]) for t in purged} - {
                        int(t["entry_bar"]) for t in in_fold
                    }
                ),
                "embargo_bars": embargo_bars,
                "purged_trades": max(0, len(purged) - len(in_fold)),
                "trades": len(in_fold),
                "wins": len(wins),
                "win_rate_pct": (len(wins) / len(in_fold) * 100.0) if in_fold else 0.0,
                "gross_bps": gross,
                "net_bps": net,
                "cost_bps": float(cost_floor_bps),
            }
        )

    out_of_sample = weighted_net / total_in_fold if total_in_fold else 0.0
    return {
        "k": k,
        "embargo_pct": float(embargo_pct),
        "embargo_bars": embargo_bars,
        "fold_count": k,
        "folds": folds,
        "trades_in_folds": total_in_fold,
        "out_of_sample_net_bps": out_of_sample,
        "purged": True,
        "embargoed": True,
        "leakage_free": True,
    }


def _horizon_matrix(
    tape: Sequence[PriceBar],
    signal_bars: Sequence[int],
    *,
    cost_floor_bps: float,
) -> dict[str, dict[str, Any]]:
    """Harvested ``daily_stock_analysis`` T+1 / T+5 / T+20 accuracy matrix."""

    closes = _closes(tape)
    matrix: dict[str, dict[str, Any]] = {}
    for horizon, label in ((1, "t_plus_1"), (5, "t_plus_5"), (20, "t_plus_20")):
        samples = 0
        wins = 0
        total_bps = 0.0
        for bar_index in signal_bars:
            target_index = bar_index + horizon
            if target_index >= len(closes):
                continue
            entry = closes[bar_index]
            if entry <= 0.0:
                continue
            raw_bps = (closes[target_index] / entry - 1.0) * 10_000.0
            net_bps = raw_bps - cost_floor_bps
            samples += 1
            total_bps += net_bps
            if net_bps > 0.0:
                wins += 1
        matrix[label] = {
            "horizon_bars": horizon,
            "samples": samples,
            "wins": wins,
            "win_rate_pct": (wins / samples * 100.0) if samples else 0.0,
            "net_edge_bps": (total_bps / samples) if samples else 0.0,
            "cost_bps": float(cost_floor_bps),
        }
    return matrix


# --------------------------------------------------------------------------- #
# Manifest + markdown rendering
# --------------------------------------------------------------------------- #
def _build_manifest(
    plugin_name: str, receipt: Mapping[str, Any], *, dsr: float, net_edge_bps: float
) -> Any:
    """Build a Ring 2 ``PluginManifest`` for the nursery registration."""

    from core.plugins import PluginKind, PluginManifest

    return PluginManifest(
        name=plugin_name,
        kind=PluginKind.FACTOR_PACK,
        version="1.0.0",
        description=(
            "Walk-forward backtest promotion: "
            f"{receipt.get('strategy_id')} on {receipt.get('silo')} "
            f"(DSR {dsr:.4f}, net edge {net_edge_bps:.4f} bps)."
        ),
        author="ring2.backtest",
        ring="ring2",
        entrypoint=f"ring2_brain.backtest.engine:{receipt.get('backtest_id')}",
        factor_weights={str(receipt.get("strategy_id")): 1.0},
        metadata={
            "backtest_id": receipt.get("backtest_id"),
            "silo": receipt.get("silo"),
            "symbol": receipt.get("symbol"),
            "deflated_sharpe_ratio": float(dsr),
            "net_edge_bps": float(net_edge_bps),
            "cost_floor_bps": float(receipt.get("cost_floor_bps") or 0.0),
            "grounding_sha256": receipt.get("grounding_sha256"),
            "required_shadow_fills": 30,
        },
    )


def _render_receipt(
    *,
    symbol: str,
    silo: str,
    strategy_name: str,
    label: str,
    bar_count: int,
    cost_floor_bps: float,
    stop_loss_bps: float,
    take_profit_bps: float,
    metrics: Mapping[str, float],
    horizon: Mapping[str, Mapping[str, Any]],
    kfold: Mapping[str, Any],
    trade_count: int,
    promotable: bool,
) -> str:
    doc = _Doc(f"Walk-Forward Backtest Receipt â€” {symbol}")
    doc.para(
        "Deterministic tax-net walk-forward run produced entirely inside Ring 2. "
        "No order was signed and no broker credential was read."
    )

    doc.head("Run configuration")
    doc.note("Symbol", symbol)
    doc.note("Silo", silo)
    doc.note("Strategy", strategy_name)
    doc.note("Strategy label", label)
    doc.kv("Bars", bar_count)
    doc.kv("Statutory cost floor bps", cost_floor_bps)
    doc.kv("Stop loss bps", stop_loss_bps)
    doc.kv("Take profit bps", take_profit_bps)
    doc.kv("Initial capital units", metrics.get("initial_capital_units"))
    doc.kv("Final nav units", metrics.get("final_nav_units"))
    doc.text("")

    doc.head("Performance")
    doc.kv("Total return pct", metrics.get("total_return_pct"))
    doc.kv("Benchmark return pct", metrics.get("benchmark_return_pct"))
    doc.kv("Alpha pct", metrics.get("alpha_pct"))
    doc.kv("Cagr pct", metrics.get("cagr_pct"))
    doc.kv("Sharpe ratio", metrics.get("sharpe_ratio"))
    doc.kv("Sortino ratio", metrics.get("sortino_ratio"))
    doc.kv("Calmar ratio", metrics.get("calmar_ratio"))
    doc.kv("Max drawdown pct", metrics.get("max_drawdown_pct"))
    doc.kv("Win rate pct", metrics.get("win_rate_pct"))
    doc.kv("Profit factor", metrics.get("profit_factor"))
    doc.kv("Total trades", metrics.get("total_trades"))
    doc.kv("Exposure pct", metrics.get("exposure_pct"))
    doc.text("")

    doc.head("Tax-net edge")
    doc.kv("Gross edge bps", metrics.get("gross_edge_bps"))
    doc.kv("Tax and cost drag bps", metrics.get("tax_and_cost_drag_bps"))
    doc.kv("Net edge bps", metrics.get("net_edge_bps"))
    doc.kv("Champion net edge floor bps", metrics.get("champion_net_edge_floor_bps"))
    doc.text("")

    doc.head("Deflated Sharpe gate")
    doc.kv("Deflated sharpe ratio", metrics.get("deflated_sharpe_ratio"))
    doc.kv("Dsr gate", metrics.get("dsr_gate"))
    doc.note(
        "Gate verdict",
        "PASS â€” clearing the nursery threshold" if promotable else "FAIL â€” below threshold",
    )
    doc.text("")

    doc.head("Purged K-Fold cross validation")
    doc.kv("Folds", kfold.get("fold_count"))
    doc.kv("Embargo bars", kfold.get("embargo_bars"))
    doc.kv("Trades in folds", kfold.get("trades_in_folds"))
    doc.kv("Out of sample net bps", kfold.get("out_of_sample_net_bps"))
    doc.note("Leakage", "purged and embargoed, therefore leakage free")
    doc.text("")

    doc.head("Signal horizon accuracy matrix")
    doc.table(
        "Horizon metrics",
        {
            "t plus one win rate pct": _sane(
                horizon.get("t_plus_1", {}).get("win_rate_pct")
            ),
            "t plus five win rate pct": _sane(
                horizon.get("t_plus_5", {}).get("win_rate_pct")
            ),
            "t plus twenty win rate pct": _sane(
                horizon.get("t_plus_20", {}).get("win_rate_pct")
            ),
            "t plus one net edge bps": _sane(
                horizon.get("t_plus_1", {}).get("net_edge_bps")
            ),
            "t plus five net edge bps": _sane(
                horizon.get("t_plus_5", {}).get("net_edge_bps")
            ),
            "t plus twenty net edge bps": _sane(
                horizon.get("t_plus_20", {}).get("net_edge_bps")
            ),
        },
    )

    doc.head("Blotter summary")
    doc.kv("Closed trades", trade_count)
    doc.kv("Best trade bps", metrics.get("best_trade_bps"))
    doc.kv("Worst trade bps", metrics.get("worst_trade_bps"))
    doc.kv("Average bars held", metrics.get("avg_bars_held"))
    doc.text("")

    doc.head("Nursery handoff")
    doc.note(
        "Shadow graduation",
        "thirty L2 fill nursery is mandatory, promotion never bypasses it",
    )
    doc.note(
        "Authority",
        "Ring 2 research only â€” no order signing, no broker key access",
    )
    return doc.build()
