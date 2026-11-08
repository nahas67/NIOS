"""NIOS Core v3.0 -- 30-Trade Live L2 Shadow Execution Twin (STEP 5).

The constitutional problem this module solves
---------------------------------------------
``sec_002`` fatal flaw #4 -- **Conformal Cold-Start Deadlock**:

    "Applying heavy uncertainty penalties at sample size N = 0 blocks all live
    trades and prevents residual calibration."

``sec_018`` Law 4 answers it:

    "Every agent warms up in live L2 Shadow Mode for 30 trades and deploys
    real vault capital strictly when its 98% split-conformal lower bound beats
    spread, fees and statutory taxes (``Net_LB > 0 bps``)."

So the twin is not a backtest.  It is a **live, real-time** execution
simulator wired to the same ``shm://nios-shadow-book`` top-of-book snapshot the
production loop reads, and it settles every completed fill into the Tier-2
conformal window.  By trade #30 the strategy owns a calibrated
``Q_0.98(Residuals)`` instead of the ``+inf`` cold-start penalty, which is
exactly what re-enables live graduation without ever trusting an uncalibrated
tail.

Execution model
---------------
1. Read the L2 snapshot for the symbol from :class:`~core.shm_ipc.ShadowBookSHM`.
2. Post a **maker peg** at the near touch -- ``best_bid`` for a BUY,
   ``best_ask`` for a SELL (``sec_010`` capability 7, 3 s maker-peg chaser).
3. A fill requires *executed* opposite-side aggression at or through the peg
   (``taker_sell`` for a BUY, ``taker_buy`` for a SELL) -- resting depth alone
   is never proof of liquidity (``sec_018`` Law 1).
4. Gross edge is the distance from the peg to the mid (the half-spread a
   passive fill captures), minus the silo's statutory round-trip floor
   (``spread + 2 * fee + tax``) from :mod:`ring1_spinal.silo_worker`.
5. The absolute prediction error becomes a conformal residual, appended to the
   strategy's rolling 200-trade window with ``is_shadow=True``.

Air-gap
-------
Ring 1 must not import Ring 2 (``sec_004``).  Both the feature store and the
:class:`~ring2_brain.truth.settlement_worker.DelayedSettlementWorker` are
**injected** by the composition root; this module only duck-types
``append_conformal_residual`` / ``record_prediction`` / ``settle_prediction``
and therefore imports neither ``duckdb`` nor ``ring2_brain``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from core.schemas import ExecutedFlowTick
from core.shm_ipc import ShadowBookSHM

from .silo_worker import SiloCostFloor, silo_cost_floor

__all__ = [
    "REQUIRED_SHADOW_FILLS",
    "DEFAULT_MAKER_PEG_TTL_NS",
    "DEFAULT_SHADOW_QTY",
    "ShadowFill",
    "ShadowOrder",
    "ShadowExecutionTwin",
]

#: ``sec_018`` Law 4 / ``sec_002`` flaw 4: 30 completed live L2 fills.
REQUIRED_SHADOW_FILLS: int = 30

#: ``sec_010`` capability 7: the maker peg is cancelled at 3.0 s.
DEFAULT_MAKER_PEG_TTL_NS: int = 3_000_000_000

#: Default clip for a shadow probe (never touches capital).
DEFAULT_SHADOW_QTY: float = 10.0

#: How far through the peg the tape may print and still be treated as a fill.
DEFAULT_CROSS_TOLERANCE: float = 0.0


@dataclass(slots=True, frozen=True)
class ShadowFill:
    """One completed live-L2 shadow fill and its graded economics."""

    strategy_id: str
    symbol: str
    side: str
    silo: str
    sequence: int
    qty: float
    peg_price: float
    mid_price: float
    fill_time_ns: int
    gross_bps: float
    cost_bps: float
    realized_net_bps: float
    expected_bps: float
    residual_bps: float
    spread_bps: float
    trade_id: str

    @property
    def is_profitable(self) -> bool:
        return self.realized_net_bps > 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            "silo": self.silo,
            "sequence": self.sequence,
            "qty": self.qty,
            "peg_price": self.peg_price,
            "mid_price": self.mid_price,
            "fill_time_ns": self.fill_time_ns,
            "gross_bps": self.gross_bps,
            "cost_bps": self.cost_bps,
            "realized_net_bps": self.realized_net_bps,
            "expected_bps": self.expected_bps,
            "residual_bps": self.residual_bps,
            "spread_bps": self.spread_bps,
            "trade_id": self.trade_id,
        }


@dataclass(slots=True)
class ShadowOrder:
    """A resting post-only maker peg awaiting live opposite-side flow."""

    strategy_id: str
    symbol: str
    side: str
    peg_price: float
    qty: float
    placed_at_ns: int
    expires_at_ns: int
    repeg_count: int = 0

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.strategy_id, self.symbol, self.side)


class ShadowExecutionTwin:
    """Live L2 shadow execution with 30-fill graduation (``sec_018`` Law 4).

    Parameters
    ----------
    shadow_book:
        The Tier-0 :class:`~core.shm_ipc.ShadowBookSHM` L2 snapshot segment.
    feature_store:
        Optional injected Tier-2 store exposing
        ``append_conformal_residual`` and ``get_q98_residual``.  Injected, not
        imported, so Ring 1 keeps its air-gap.
    settlement_worker:
        Optional injected
        :class:`~ring2_brain.truth.settlement_worker.DelayedSettlementWorker`.
        When present it is preferred over the raw store, which also exercises
        the Step-4 Darwinian trust path.
    """

    __slots__ = (
        "_book",
        "_store",
        "_worker",
        "_required_fills",
        "_peg_ttl_ns",
        "_qty",
        "_cross_tolerance",
        "_counts",
        "_fills",
        "_orders",
        "_sequence",
        "_repeg_count",
        "_residual_latency_ns_total",
        "_settlement_errors",
    )

    def __init__(
        self,
        shadow_book: ShadowBookSHM,
        *,
        feature_store: Any | None = None,
        settlement_worker: Any | None = None,
        required_fills: int = REQUIRED_SHADOW_FILLS,
        maker_peg_ttl_ns: int = DEFAULT_MAKER_PEG_TTL_NS,
        qty: float = DEFAULT_SHADOW_QTY,
        cross_tolerance: float = DEFAULT_CROSS_TOLERANCE,
    ) -> None:
        if shadow_book is None:
            raise ValueError("ShadowExecutionTwin requires a ShadowBookSHM")
        required = int(required_fills)
        if required <= 0:
            raise ValueError("required_fills must be positive")
        ttl = int(maker_peg_ttl_ns)
        if ttl <= 0:
            raise ValueError("maker_peg_ttl_ns must be positive")
        clip = float(qty)
        if not math.isfinite(clip) or clip <= 0.0:
            raise ValueError("qty must be a positive finite number")

        self._book = shadow_book
        self._store = feature_store
        self._worker = settlement_worker
        if self._store is None and settlement_worker is not None:
            self._store = getattr(settlement_worker, "store", None)
        self._required_fills = required
        self._peg_ttl_ns = ttl
        self._qty = clip
        self._cross_tolerance = float(cross_tolerance)
        self._counts: dict[str, int] = {}
        self._fills: dict[str, list[ShadowFill]] = {}
        self._orders: dict[tuple[str, str, str], ShadowOrder] = {}
        self._sequence = 0
        self._repeg_count = 0
        self._residual_latency_ns_total = 0
        self._settlement_errors: list[str] = []

    # -- configuration ----------------------------------------------------
    @property
    def required_fills(self) -> int:
        return self._required_fills

    @property
    def shadow_book(self) -> ShadowBookSHM:
        return self._book

    @property
    def feature_store(self) -> Any | None:
        return self._store

    @property
    def settlement_worker(self) -> Any | None:
        return self._worker

    # -- graduation state -------------------------------------------------
    def shadow_fill_count(self, strategy_id: str) -> int:
        return self._counts.get(str(strategy_id), 0)

    def is_graduated(self, strategy_id: str) -> bool:
        """``True`` once the strategy owns >= 30 completed L2 shadow fills."""
        return self._counts.get(str(strategy_id), 0) >= self._required_fills

    #: Explicit alias used in the architecture prose.
    def has_graduated(self, strategy_id: str) -> bool:
        return self.is_graduated(strategy_id)

    def remaining_fills(self, strategy_id: str) -> int:
        return max(0, self._required_fills - self.shadow_fill_count(strategy_id))

    def graduated_strategies(self) -> tuple[str, ...]:
        return tuple(
            strategy
            for strategy, count in self._counts.items()
            if count >= self._required_fills
        )

    def fills(self, strategy_id: str) -> tuple[ShadowFill, ...]:
        return tuple(self._fills.get(str(strategy_id), ()))

    def open_order_for(
        self, strategy_id: str, symbol: str, side: str
    ) -> ShadowOrder | None:
        return self._orders.get(
            (str(strategy_id), str(symbol), str(side).strip().upper())
        )

    def open_order_count(self, strategy_id: str | None = None) -> int:
        if strategy_id is None:
            return len(self._orders)
        token = str(strategy_id)
        return sum(1 for order in self._orders.values() if order.strategy_id == token)

    # -- execution --------------------------------------------------------
    def execute_shadow_fill(
        self,
        tick: ExecutedFlowTick,
        *,
        strategy_id: str,
        side: str,
        silo: str,
        now_ns: int | None = None,
        qty: float | None = None,
        expected_bps: float = 0.0,
        cost_floor: SiloCostFloor | None = None,
    ) -> ShadowFill | None:
        """Attempt a live-L2 maker fill for one strategy.

        Returns the completed :class:`ShadowFill`, or ``None`` when the peg is
        resting (no opposite-side executed aggression at/through the touch).
        """
        stamp = int(now_ns) if now_ns is not None else time.monotonic_ns()
        token = str(strategy_id)
        side_token = str(side).strip().upper()
        if side_token in ("B", "LONG"):
            side_token = "BUY"
        elif side_token in ("S", "SHORT"):
            side_token = "SELL"
        if side_token not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")

        book = self._book.read_snapshot(tick.symbol)
        if book is None or book.best_bid <= 0.0 or book.best_ask <= 0.0:
            return None

        peg = book.best_bid if side_token == "BUY" else book.best_ask
        mid = book.mid
        if mid <= 0.0 or peg <= 0.0:
            return None

        if side_token == "BUY":
            aggressive = float(tick.taker_sell_volume_5s)
            fills_now = tick.last_price <= peg * (1.0 + self._cross_tolerance)
            gross_bps = (mid - peg) / peg * 10_000.0
        else:
            aggressive = float(tick.taker_buy_volume_5s)
            fills_now = tick.last_price >= peg * (1.0 - self._cross_tolerance)
            gross_bps = (peg - mid) / peg * 10_000.0

        key = (token, str(tick.symbol), side_token)
        resting = self._orders.get(key)
        if resting is not None and stamp >= resting.expires_at_ns:
            self._orders.pop(key, None)
            resting = None

        if aggressive <= 0.0 or not fills_now:
            if resting is None:
                self._orders[key] = ShadowOrder(
                    strategy_id=token,
                    symbol=str(tick.symbol),
                    side=side_token,
                    peg_price=float(peg),
                    qty=float(self._qty if qty is None else qty),
                    placed_at_ns=stamp,
                    expires_at_ns=stamp + self._peg_ttl_ns,
                )
            else:
                # 3 s maker-peg chaser: re-peg once per expired window.
                resting.peg_price = float(peg)
                resting.placed_at_ns = stamp
                resting.expires_at_ns = stamp + self._peg_ttl_ns
                resting.repeg_count += 1
                self._repeg_count += 1
            return None

        self._orders.pop(key, None)
        clip = float(self._qty if qty is None else qty)
        if not math.isfinite(clip) or clip <= 0.0:
            clip = self._qty

        floor = cost_floor if cost_floor is not None else silo_cost_floor(silo)
        cost_bps = floor.round_trip_cost_bps
        realized_net = gross_bps - cost_bps
        expected = float(expected_bps)
        residual = abs(expected - realized_net)

        self._sequence += 1
        sequence = self._sequence
        trade_id = f"{token}:{sequence}"
        fill = ShadowFill(
            strategy_id=token,
            symbol=str(tick.symbol),
            side=side_token,
            silo=str(silo),
            sequence=sequence,
            qty=clip,
            peg_price=float(peg),
            mid_price=float(mid),
            fill_time_ns=stamp,
            gross_bps=float(gross_bps),
            cost_bps=float(cost_bps),
            realized_net_bps=float(realized_net),
            expected_bps=expected,
            residual_bps=float(residual),
            spread_bps=float(book.spread_bps),
            trade_id=trade_id,
        )

        self._counts[token] = self._counts.get(token, 0) + 1
        self._fills.setdefault(token, []).append(fill)
        self._settle(fill)

        return fill

    # -- conformal settlement ---------------------------------------------
    def _settle(self, fill: ShadowFill) -> None:
        """Feed the completed shadow trade into the 200-trade conformal window."""
        settle_started = time.perf_counter_ns()
        try:
            worker = self._worker
            if worker is not None and hasattr(worker, "record_prediction"):
                worker.record_prediction(
                    fill.trade_id,
                    strategy_id=fill.strategy_id,
                    symbol=fill.symbol,
                    predicted_net_bps=fill.expected_bps,
                    horizon_ns=0,
                    created_at_ns=fill.fill_time_ns,
                    silo=fill.silo,
                    is_shadow=True,
                )
                worker.settle_prediction(
                    fill.trade_id,
                    fill.realized_net_bps,
                    now_ns=fill.fill_time_ns,
                    is_shadow=True,
                )
            elif self._store is not None:
                self._store.append_conformal_residual(
                    fill.strategy_id,
                    fill.residual_bps,
                    fill.fill_time_ns,
                    knowledge_ts_ns=fill.fill_time_ns,
                    trade_id=fill.trade_id,
                    realized_net_bps=fill.realized_net_bps,
                    is_shadow=True,
                )
        except Exception as exc:  # pragma: no cover - injected store failure
            # A settlement failure must never mask a fill: the trade already
            # happened, and losing the residual would silently uncalibrate
            # the gate.  Record it and keep the fill.
            self._settlement_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            self._residual_latency_ns_total += time.perf_counter_ns() - settle_started

    # -- conformal read-out -----------------------------------------------
    def q98_bps(self, strategy_id: str, as_of_ns: int | None = None) -> float:
        """``Q_0.98(Residuals)`` for the strategy; ``+inf`` while cold."""
        store = self._store
        if store is None or not hasattr(store, "get_q98_residual"):
            return math.inf
        stamp = int(as_of_ns) if as_of_ns is not None else time.monotonic_ns()
        window = store.get_q98_residual(str(strategy_id), stamp)
        return float(getattr(window, "q98_bps", math.inf))

    def calibrated(self, strategy_id: str, as_of_ns: int | None = None) -> bool:
        store = self._store
        if store is None or not hasattr(store, "get_q98_residual"):
            return False
        stamp = int(as_of_ns) if as_of_ns is not None else time.monotonic_ns()
        window = store.get_q98_residual(str(strategy_id), stamp)
        return bool(getattr(window, "calibrated", False))

    # -- reporting ---------------------------------------------------------
    def shadow_report(
        self, strategy_id: str, as_of_ns: int | None = None
    ) -> dict[str, Any]:
        """Everything the cockpit needs about one strategy's warmup."""
        token = str(strategy_id)
        fills = self._fills.get(token, ())
        count = len(fills)
        gross = sum(f.gross_bps for f in fills) / count if count else 0.0
        net = sum(f.realized_net_bps for f in fills) / count if count else 0.0
        residual = sum(f.residual_bps for f in fills) / count if count else 0.0
        wins = sum(1 for f in fills if f.realized_net_bps > 0.0)
        return {
            "strategy_id": token,
            "shadow_fills": count,
            "required_fills": self._required_fills,
            "remaining_fills": max(0, self._required_fills - count),
            "graduated": count >= self._required_fills,
            "open_orders": self.open_order_count(token),
            "mean_gross_bps": gross,
            "mean_realized_net_bps": net,
            "mean_residual_bps": residual,
            "hit_rate": (wins / count) if count else 0.0,
            "q98_bps": self.q98_bps(token, as_of_ns),
            "calibrated": self.calibrated(token, as_of_ns),
            "repeg_count": self._repeg_count,
            "settlement_errors": list(self._settlement_errors),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "required_fills": self._required_fills,
            "maker_peg_ttl_ns": self._peg_ttl_ns,
            "qty": self._qty,
            "strategies": {
                strategy: {
                    "shadow_fills": count,
                    "graduated": count >= self._required_fills,
                }
                for strategy, count in self._counts.items()
            },
            "open_orders": len(self._orders),
            "repeg_count": self._repeg_count,
            "settlement_errors": len(self._settlement_errors),
        }

    # -- maintenance -------------------------------------------------------
    def cancel_stale(self, now_ns: int) -> int:
        """Drop pegs whose 3 s window elapsed (cancels unfilled maker pegs)."""
        stamp = int(now_ns)
        stale = [
            key
            for key, order in self._orders.items()
            if stamp >= order.expires_at_ns
        ]
        for key in stale:
            self._orders.pop(key, None)
        return len(stale)

    def reset_strategy(self, strategy_id: str) -> int:
        """Forget one strategy's shadow history (requires human authorisation)."""
        token = str(strategy_id)
        removed = self._counts.pop(token, 0)
        self._fills.pop(token, None)
        for key in [key for key in self._orders if key[0] == token]:
            self._orders.pop(key, None)
        return removed
