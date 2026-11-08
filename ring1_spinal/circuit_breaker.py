"""NIOS Core v3.0 -- Tier-1 ``0.1 ms`` Spinal Microstructure Circuit Breaker (STEP 5).

Governing sections
------------------
``sec_004``
    Ring 1 components: "``0.1 ms`` Vol Spike Breaker (>= 3.5x)".
``sec_007``
    Tier 0 Step 2: "Instant HALT & lease disarm if ``Vol_5s / NormVol_5s >=
    3.5x``, if 2 consecutive maker fills move adversely within 500 ms, or if
    spread exceeds cap."
``sec_010``
    Capability 1 failure fallback: "Instant lease disarm on vol spike or 2
    consecutive adverse fills".
``sec_011``
    Three-tier breaker matrix -- T1 is the *microstructure* tier and must fire
    locally in ``0.1 ms`` without a NATS round-trip.
``sec_018`` Law 2
    The brake is deterministic compiled math; no LLM may sit between the tick
    and the disarm.

Design contract
---------------
* The breaker owns exactly three deterministic triggers and nothing else::

      Condition 1  VOL_SPIKE_3_5X            realized_vol_5s / normal_vol_5s >= 3.5
      Condition 2  TWO_ADVERSE_FILLS_500MS   2 consecutive adverse maker fills
                                             inside 500_000_000 ns
      Condition 3  SPREAD_BLOWOUT            spread_bps > max_allowed_spread_bps

* A trip performs the *only* mutation Ring 1 is allowed to make to execution
  authority: it writes ``LeaseMode.DISARMED`` into the symbol's
  :class:`~core.shm_ipc.LeaseTableSHM` slot.  It never places, cancels or
  signs an order (``sec_004`` air-gap).
* The clean path allocates nothing: two float comparisons and a ``None``
  return.  Object construction happens only when a breaker actually fires,
  which is by design a rare event.
* Every trip is recorded with the deterministic reason code so Ring 3 can
  render the ``sec_016`` proof line and Ring 0 can attribute the halt.

Python note
-----------
The authoritative implementation target is the compiled Ring 1 loop
(Rust/Cython ring buffers, ``sec_001``).  This module is the reference
semantics with the same public surface, so the compiled path can be swapped
in behind :class:`SpinalCircuitBreaker` without touching
:mod:`ring1_spinal.spinal_loop`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from core.schemas import (
    TAKER_BUY_RATIO_MIN,
    TAKER_SELL_RATIO_MAX,
    VOL_SPIKE_BREAKER,
    ExecutedFlowTick,
)
from core.shm_ipc import LeaseTableSHM

__all__ = [
    "VOL_SPIKE_TRIP_REASON",
    "TWO_ADVERSE_FILLS_TRIP_REASON",
    "SPREAD_BLOWOUT_TRIP_REASON",
    "ADVERSE_FILL_WINDOW_NS",
    "ADVERSE_FILLS_REQUIRED",
    "TRIP_LATENCY_BUDGET_NS",
    "DEFAULT_MAX_SPREAD_BPS",
    "TripReason",
    "TripCode",
    "BreakerTrip",
    "MakerFill",
    "SpinalCircuitBreaker",
]

#: Deterministic reason codes (asserted verbatim by the Step 5 gates).
VOL_SPIKE_TRIP_REASON: str = "VOL_SPIKE_3_5X"
TWO_ADVERSE_FILLS_TRIP_REASON: str = "TWO_ADVERSE_FILLS_500MS"
SPREAD_BLOWOUT_TRIP_REASON: str = "SPREAD_BLOWOUT"

#: ``sec_007``: the adverse-selection pair must land inside 500 ms.
ADVERSE_FILL_WINDOW_NS: int = 500_000_000
ADVERSE_FILLS_REQUIRED: int = 2

#: ``sec_007`` / ``sec_010``: trip -> disarmed lease in under 0.10 ms.
TRIP_LATENCY_BUDGET_NS: int = 100_000

#: Conservative default cap for a liquid scalp instrument (bps of mid).
DEFAULT_MAX_SPREAD_BPS: float = 12.0


class TripReason:
    """String constants for the three Tier-1 triggers."""

    VOL_SPIKE = VOL_SPIKE_TRIP_REASON
    ADVERSE_FILLS = TWO_ADVERSE_FILLS_TRIP_REASON
    SPREAD_BLOWOUT = SPREAD_BLOWOUT_TRIP_REASON

    ALL: tuple[str, ...] = (
        VOL_SPIKE,
        ADVERSE_FILLS,
        SPREAD_BLOWOUT,
    )


class TripCode(IntEnum):
    """Compact numeric mirror used by the compiled path and telemetry."""

    NONE = 0
    VOL_SPIKE = 1
    ADVERSE_FILLS = 2
    SPREAD_BLOWOUT = 3


_CODE_BY_REASON: dict[str, int] = {
    VOL_SPIKE_TRIP_REASON: int(TripCode.VOL_SPIKE),
    TWO_ADVERSE_FILLS_TRIP_REASON: int(TripCode.ADVERSE_FILLS),
    SPREAD_BLOWOUT_TRIP_REASON: int(TripCode.SPREAD_BLOWOUT),
}


@dataclass(slots=True, frozen=True)
class MakerFill:
    """One **maker** fill with its post-fill markout.

    ``markout_bps`` is the signed markout measured by Ring 0 after the fill
    horizon: positive means the mid moved in NIOS's favour.  A negative
    markout is *adverse selection* -- the counterparty knew something the
    resting quote did not.
    """

    symbol: str
    side: str
    fill_price: float
    fill_time_ns: int
    markout_bps: float
    fill_id: str = ""

    def __post_init__(self) -> None:
        side = str(self.side).strip().upper()
        if side in ("B", "LONG"):
            side = "BUY"
        elif side in ("S", "SHORT"):
            side = "SELL"
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {self.side!r}")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "symbol", str(self.symbol))
        object.__setattr__(self, "fill_price", float(self.fill_price))
        object.__setattr__(self, "fill_time_ns", int(self.fill_time_ns))
        object.__setattr__(self, "markout_bps", float(self.markout_bps))

    @property
    def is_adverse(self) -> bool:
        """``True`` when the fill marked out against NIOS."""
        return self.markout_bps < 0.0


@dataclass(slots=True, frozen=True)
class BreakerTrip:
    """Immutable record of one Tier-1 trip and the disarm it forced."""

    symbol: str
    reason: str
    tripped_at_ns: int
    observed: float
    threshold: float
    lease_disarmed: bool
    disarm_latency_ns: int = 0

    @property
    def code(self) -> int:
        return _CODE_BY_REASON.get(self.reason, 0)

    @property
    def within_budget(self) -> bool:
        """``sec_007``: the disarm must land inside ``0.10 ms``."""
        return self.lease_disarmed and self.disarm_latency_ns < TRIP_LATENCY_BUDGET_NS

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "reason": self.reason,
            "code": self.code,
            "tripped_at_ns": self.tripped_at_ns,
            "observed": self.observed,
            "threshold": self.threshold,
            "lease_disarmed": self.lease_disarmed,
            "disarm_latency_ns": self.disarm_latency_ns,
        }


class SpinalCircuitBreaker:
    """The Tier-1 ``0.1 ms`` microstructure circuit breaker.

    Parameters
    ----------
    lease_table:
        The Tier-0 :class:`~core.shm_ipc.LeaseTableSHM`.  A trip writes
        ``LeaseMode.DISARMED`` into the symbol slot; Ring 1 keeps no other
        authority over execution.
    max_allowed_spread_bps:
        Condition 3 cap.  ``tick.spread_bps > cap`` trips.
    vol_spike_ratio:
        Condition 1 multiple (``sec_007`` pins this at ``3.5``).
    adverse_fill_window_ns:
        Condition 2 window (``sec_007`` pins this at ``500 ms``).
    adverse_fills_required:
        Condition 2 count (``sec_007`` pins this at ``2`` consecutive fills).
    """

    __slots__ = (
        "_lease_table",
        "_max_spread_bps",
        "_vol_ratio",
        "_adverse_window_ns",
        "_adverse_required",
        "_last_fill",
        "_trips",
        "_trip_by_symbol",
        "_trip_counts",
        "_disarm_ns_total",
        "_disarm_ns_max",
    )

    def __init__(
        self,
        lease_table: LeaseTableSHM,
        *,
        max_allowed_spread_bps: float = DEFAULT_MAX_SPREAD_BPS,
        vol_spike_ratio: float = VOL_SPIKE_BREAKER,
        adverse_fill_window_ns: int = ADVERSE_FILL_WINDOW_NS,
        adverse_fills_required: int = ADVERSE_FILLS_REQUIRED,
    ) -> None:
        if lease_table is None:
            raise ValueError("SpinalCircuitBreaker requires a LeaseTableSHM")
        cap = float(max_allowed_spread_bps)
        if cap <= 0.0:
            raise ValueError("max_allowed_spread_bps must be positive")
        ratio = float(vol_spike_ratio)
        if ratio <= 0.0:
            raise ValueError("vol_spike_ratio must be positive")
        window = int(adverse_fill_window_ns)
        if window <= 0:
            raise ValueError("adverse_fill_window_ns must be positive")
        required = int(adverse_fills_required)
        if required < 2:
            raise ValueError("adverse_fills_required must be >= 2")

        self._lease_table = lease_table
        self._max_spread_bps = cap
        self._vol_ratio = ratio
        self._adverse_window_ns = window
        self._adverse_required = required
        self._last_fill: dict[str, MakerFill] = {}
        self._trips: list[BreakerTrip] = []
        self._trip_by_symbol: dict[str, BreakerTrip] = {}
        self._trip_counts: dict[str, int] = {}
        self._disarm_ns_total = 0
        self._disarm_ns_max = 0

    # -- configuration ----------------------------------------------------
    @property
    def max_allowed_spread_bps(self) -> float:
        return self._max_spread_bps

    def set_spread_cap(self, bps: float) -> None:
        cap = float(bps)
        if cap <= 0.0:
            raise ValueError("spread cap must be positive")
        self._max_spread_bps = cap

    @property
    def vol_spike_ratio(self) -> float:
        return self._vol_ratio

    @property
    def adverse_fill_window_ns(self) -> int:
        return self._adverse_window_ns

    # -- condition 1 + 3 (tick path) --------------------------------------
    def check_tick(
        self, tick: ExecutedFlowTick, now_ns: int | None = None
    ) -> BreakerTrip | None:
        """Evaluate Conditions 1 and 3 for one executed-flow frame.

        Returns ``None`` on the clean path (no allocation) or the
        :class:`BreakerTrip` that just disarmed the symbol.
        """
        if tick.vol_spike_ratio >= self._vol_ratio:
            return self._trip(
                tick.symbol,
                VOL_SPIKE_TRIP_REASON,
                now_ns,
                float(tick.vol_spike_ratio),
                self._vol_ratio,
            )
        if tick.spread_bps > self._max_spread_bps:
            return self._trip(
                tick.symbol,
                SPREAD_BLOWOUT_TRIP_REASON,
                now_ns,
                float(tick.spread_bps),
                self._max_spread_bps,
            )
        return None

    # -- condition 2 (fill path) ------------------------------------------
    def record_maker_fill(
        self,
        symbol: str,
        side: str,
        fill_price: float,
        markout_bps: float,
        fill_time_ns: int,
        *,
        now_ns: int | None = None,
        fill_id: str = "",
    ) -> BreakerTrip | None:
        """Register a maker fill and trip on two consecutive adverse fills.

        "Consecutive" means the adverse fill *immediately preceding* this one
        in time was also adverse and landed within
        :attr:`adverse_fill_window_ns`.  A favourable fill breaks the chain,
        which is what stops a slow bleed of isolated bad fills from halting a
        healthy strategy.
        """
        fill = MakerFill(
            symbol=symbol,
            side=side,
            fill_price=fill_price,
            fill_time_ns=fill_time_ns,
            markout_bps=markout_bps,
            fill_id=fill_id,
        )
        previous = self._last_fill.get(fill.symbol)
        self._last_fill[fill.symbol] = fill

        if not fill.is_adverse:
            return None
        if previous is None or not previous.is_adverse:
            return None

        delta = fill.fill_time_ns - previous.fill_time_ns
        if delta < 0 or delta > self._adverse_window_ns:
            return None

        return self._trip(
            fill.symbol,
            TWO_ADVERSE_FILLS_TRIP_REASON,
            now_ns if now_ns is not None else fill.fill_time_ns,
            float(fill.markout_bps + previous.markout_bps),
            0.0,
        )

    # -- trip / disarm -----------------------------------------------------
    def _trip(
        self,
        symbol: str,
        reason: str,
        now_ns: int | None,
        observed: float,
        threshold: float,
    ) -> BreakerTrip:
        stamp = int(now_ns) if now_ns is not None else time.monotonic_ns()
        started = time.perf_counter_ns()
        try:
            self._lease_table.disarm(symbol, now_ns=stamp)
            disarmed = True
        except Exception:  # pragma: no cover - SHM unavailable
            disarmed = False
        elapsed = time.perf_counter_ns() - started

        trip = BreakerTrip(
            symbol=str(symbol),
            reason=str(reason),
            tripped_at_ns=stamp,
            observed=float(observed),
            threshold=float(threshold),
            lease_disarmed=disarmed,
            disarm_latency_ns=elapsed,
        )
        self._trips.append(trip)
        self._trip_by_symbol[trip.symbol] = trip
        self._trip_counts[trip.symbol] = self._trip_counts.get(trip.symbol, 0) + 1
        self._disarm_ns_total += elapsed
        if elapsed > self._disarm_ns_max:
            self._disarm_ns_max = elapsed
        return trip

    # -- introspection -----------------------------------------------------
    @property
    def trips(self) -> tuple[BreakerTrip, ...]:
        return tuple(self._trips)

    @property
    def trip_count(self) -> int:
        return len(self._trips)

    def trip_count_for(self, symbol: str) -> int:
        return self._trip_counts.get(str(symbol), 0)

    def last_trip(self, symbol: str | None = None) -> BreakerTrip | None:
        if symbol is None:
            return self._trips[-1] if self._trips else None
        return self._trip_by_symbol.get(str(symbol))

    def is_tripped(self, symbol: str) -> bool:
        return str(symbol) in self._trip_by_symbol

    @property
    def tripped_symbols(self) -> tuple[str, ...]:
        return tuple(self._trip_by_symbol)

    @property
    def max_disarm_latency_ns(self) -> int:
        return self._disarm_ns_max

    @property
    def mean_disarm_latency_ns(self) -> float:
        if not self._trips:
            return 0.0
        return self._disarm_ns_total / float(len(self._trips))

    def trip_reasons(self) -> dict[str, int]:
        """Histogram keyed by deterministic reason code."""
        counts: dict[str, int] = {}
        for trip in self._trips:
            counts[trip.reason] = counts.get(trip.reason, 0) + 1
        return counts

    # -- human reset -------------------------------------------------------
    def reset(self, symbol: str | None = None) -> int:
        """Clear trip state after an explicit human review (``sec_011``).

        This does **not** re-arm the lease: only System 1 may write an armed
        lease, and only Ring 3 may re-arm the global kill latch.
        """
        if symbol is None:
            cleared = len(self._trip_by_symbol)
            self._trip_by_symbol.clear()
            self._trip_counts.clear()
            self._last_fill.clear()
            return cleared
        token = str(symbol)
        cleared = 1 if token in self._trip_by_symbol else 0
        self._trip_by_symbol.pop(token, None)
        self._trip_counts.pop(token, None)
        self._last_fill.pop(token, None)
        return cleared

    def describe(self) -> dict[str, Any]:
        return {
            "trips": len(self._trips),
            "tripped_symbols": list(self._trip_by_symbol),
            "reasons": self.trip_reasons(),
            "max_disarm_latency_ns": self._disarm_ns_max,
            "mean_disarm_latency_ns": self.mean_disarm_latency_ns,
            "vol_spike_ratio": self._vol_ratio,
            "max_allowed_spread_bps": self._max_spread_bps,
            "adverse_fill_window_ns": self._adverse_window_ns,
        }


#: Re-exported so a caller can assert the gate without importing ``core``.
OFI_LONG_THRESHOLD: float = TAKER_BUY_RATIO_MIN
OFI_SHORT_THRESHOLD: float = TAKER_SELL_RATIO_MAX
