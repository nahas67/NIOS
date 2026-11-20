"""NIOS V2.1 -- live tick-to-bar and option-chain aggregator (``G6``).

A venue sends ticks.  Every downstream consumer -- the TradingView bridge, the
feature store, the factor scan, the OMS preview -- wants **bars**, a spot mark, an
L2 ladder, and an implied-volatility / gamma-exposure surface.

This module is the single place that turns the first into the second, with three
properties that make it safe to sit at the edge of the hot path:

1. **Ingress quantization.** Every price and size is converted to integer micros
   on the way in (Rule 2).  No float price ever leaves this module.
2. **Deterministic bucketing.** A bar's boundary is a pure function of
   ``(timeframe, now_ns)`` in UTC, so replaying the same ticks in the same order
   produces byte-identical bars with a stable ``bar_sha256``.
3. **Ring 2 purity (Rule 4).** The aggregator holds no credential, opens no
   socket and signs nothing.  Feed transport stays in Ring 1; this object only
   consumes what it is handed.

Harvest note (Rule 1): the OHLCV consolidation contract and the aggregated-bar
fan-out are translated from ``nautechsystems/nautilus_trader``'s ``BarAggregator``.
No donor code is imported or vendored.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "TIMEFRAMES",
    "LiveBarAggregator",
    "TF_SECONDS",
]

#: Micro-units per unit.  Declared locally (Rule 4) so this Ring 2 module never
#: imports a Ring 0 ledger symbol; the value is the ``sec_009`` constant and the
#: conversion below is byte-for-byte the same arithmetic ``ring0_vault`` applies.
MICROS_PER_UNIT: int = 1_000_000

#: The Ring 0 epsilon.  ``to_micros`` adds it *before* flooring so a value that is
#: mathematically integral cannot be lost to a binary-float representation error
#: one ULP below the integer (``1.0000005 * 1e6`` is ``1000000.4999999999``).
_MICROS_EPSILON: float = 1e-9

#: ``sec_009``/``u128``: the largest amount the ledger can hold, in micros.
MICROS_MAX: int = (1 << 128) - 1


def to_micros(value: float) -> int:
    """``units -> integer micros``, bit-for-bit identical to Ring 0.

    ``ring0_vault.ibor.ledger_outbox.to_micros`` is the single source of truth
    for this conversion, and its rule is **floor with an epsilon**, not half-up:

    .. code-block:: python

        int(math.floor(number * MICROS_PER_UNIT + 1e-9))

    Rounding a debit *up* would mint a micro-unit of value the silo does not own,
    which is why the rule floors.  A Ring 2 copy that half-upped instead disagreed
    with the ledger at every half-micro boundary (``0.0000025`` became ``3`` here
    and ``2`` there), so a bar price and the resulting fill could differ by one
    micro.  This function therefore mirrors the ledger rule exactly and refuses
    anything the ledger refuses: a non-finite value, a negative value, and a
    magnitude above the 128-bit ceiling.
    """
    if isinstance(value, bool):
        raise ValueError("a boolean is not a money amount")
    if isinstance(value, int):
        micros = int(value) * MICROS_PER_UNIT
    else:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"value must be finite, got {value!r}")
        micros = int(math.floor(number * MICROS_PER_UNIT + _MICROS_EPSILON))
    if micros < 0:
        raise ValueError(f"value must be non-negative, got {value!r}")
    if micros > MICROS_MAX:
        raise ValueError(f"value {value!r} exceeds the 128-bit ceiling")
    return micros

#: ``G6``: the bar set the cockpit and the bridge both consume.
TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1D")

#: Seconds per timeframe (``1D`` is a UTC day bucket).
TF_SECONDS: Mapping[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1D": 86_400}

_NS_PER_SECOND = 1_000_000_000

#: Default L2 ladder depth kept per instrument.
DEFAULT_DEPTH_LEVELS: int = 10

#: Hard ceiling on the number of distinct ``(silo, symbol)`` states retained.
DEFAULT_MAX_INSTRUMENTS: int = 1_024


def _seal(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _micros(value: Any, *, name: str) -> int:
    """Quantize one ingress number; ``None`` and blank strings mean zero."""
    if value is None:
        return 0
    if isinstance(value, str):
        if not value.strip():
            return 0
        value = float(value)
    try:
        return int(to_micros(value))
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from None


@dataclass(slots=True)
class Bar:
    """One consolidated OHLCV bar in integer micros."""

    silo: str
    symbol: str
    timeframe: str
    open_micros: int
    high_micros: int
    low_micros: int
    close_micros: int
    volume_micros: int = 0
    trades: int = 0
    start_utc_ns: int = 0
    end_utc_ns: int = 0
    closed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "silo": self.silo,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "open_micros": int(self.open_micros),
            "high_micros": int(self.high_micros),
            "low_micros": int(self.low_micros),
            "close_micros": int(self.close_micros),
            "volume_micros": int(self.volume_micros),
            "trades": int(self.trades),
            "start_utc_ns": int(self.start_utc_ns),
            "end_utc_ns": int(self.end_utc_ns),
            "closed": bool(self.closed),
        }
        payload["bar_sha256"] = _seal(payload)
        return payload


@dataclass(slots=True)
class _InstrumentState:
    """Per-instrument hot state (V2.4 PART 2).

    ``bids``/``asks`` are re-assigned bounded ladders and ``bars``/``surface`` are
    fixed-key replacements, so ``history`` was the only collection that could grow
    per tick.  It is now a ``deque(maxlen=max_bars)`` per timeframe: the cap is
    enforced by the container instead of by a slice copy on every close, which is
    what removes the per-close ``list`` allocation churn from the hot path.
    """

    spot_micros: int = 0
    last_update_utc_ns: int = 0
    bids: list[tuple[int, int]] = field(default_factory=list)
    asks: list[tuple[int, int]] = field(default_factory=list)
    bars: dict[str, Bar] = field(default_factory=dict)
    history: dict[str, deque[dict[str, Any]]] = field(default_factory=dict)
    surface: dict[str, Any] = field(default_factory=dict)


class LiveBarAggregator:
    """Ring 2 tick consolidator (``G6``).

    ``features_sink`` is an optional duck-typed consumer.  Any of
    ``ingest_bar``, ``update_spot``, ``update_l2`` and ``update_option_surface``
    that it implements is called; a sink that implements none of them is simply a
    no-op, so this module never depends on a specific store build.
    """

    def __init__(
        self,
        *,
        features_sink: Any = None,
        depth_levels: int = DEFAULT_DEPTH_LEVELS,
        max_bars: int = 240,
        max_instruments: int = DEFAULT_MAX_INSTRUMENTS,
    ) -> None:
        self.features_sink = features_sink
        self.depth_levels = max(1, int(depth_levels))
        self.max_bars = max(8, int(max_bars))
        # ``_state`` is keyed by an attacker-influenced ``(silo, symbol)`` pair, so
        # it needs a ceiling: a caller that posts a fresh symbol on every tick must
        # not be able to grow the process heap without bound.
        self.max_instruments = max(8, int(max_instruments))
        self._state: dict[tuple[str, str], _InstrumentState] = {}
        self._lock = threading.RLock()
        self._ticks = 0
        self._bars_closed = 0
        self._surfaces = 0
        self._sink_errors: list[str] = []
        self._evictions = 0
        self._stale_rejected = 0
        self._crossed_rejected = 0
        #: V2.3: retained ATM-IV observations per ``(silo, symbol)`` so the
        #: gateway can publish a real 52-week IV Rank instead of a constant.
        self._iv_history: dict[tuple[str, str], deque[tuple[int, float]]] = {}
        self._max_iv_samples = 512

    # ------------------------------------------------------------------
    # Ingress
    # ------------------------------------------------------------------
    def ingest(
        self,
        *,
        silo: str,
        symbol: str,
        price: Any,
        size: Any = 0,
        bid: Any = None,
        ask: Any = None,
        bids: Sequence[Sequence[Any]] = (),
        asks: Sequence[Sequence[Any]] = (),
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Consume one tick and return the bars it touched.

        A tick that closes a bucket finalizes that bar and opens the next one, so
        a consumer that only reads ``closed_bars`` still sees every bucket exactly
        once even when ticks arrive irregularly.
        """
        token = str(silo or "").strip().upper()
        key_symbol = str(symbol or "").strip().upper()
        if not token or not key_symbol:
            raise ValueError("silo and symbol are required")
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        price_micros = _micros(price, name="price")
        if price_micros <= 0:
            raise ValueError("price must be positive")
        # Clamp before the conversion: a negative size is a no-fill, not a crash.
        try:
            size_units = max(0.0, float(size or 0.0))
        except (TypeError, ValueError):
            raise ValueError(f"size must be numeric, got {size!r}") from None
        size_micros = _micros(size_units, name="size")

        with self._lock:
            state = self._state.get((token, key_symbol))
            if state is not None and state.last_update_utc_ns and stamp < state.last_update_utc_ns:
                # A late tick must never rewind a live bucket: doing so would close
                # the current bar early and overwrite history with an older one.
                self._stale_rejected += 1
                raise ValueError(
                    f"STALE_TICK: {stamp} precedes {state.last_update_utc_ns} "
                    f"for {token}:{key_symbol}"
                )
            # Validate the whole tick *before* mutating anything: a rejected book
            # must not leave a spot price, an opened bucket or a tick counter
            # behind, or the next reader sees a quote that never existed.
            bid_micros = 0
            ask_micros = 0
            if bid is not None or ask is not None:
                bid_micros = _micros(bid, name="bid") if bid is not None else price_micros
                ask_micros = _micros(ask, name="ask") if ask is not None else price_micros
                if bid_micros > 0 and ask_micros > 0 and bid_micros >= ask_micros:
                    # A crossed or locked book is not a quote: no buyer and seller
                    # ever met there, so it must not price a bar.
                    self._crossed_rejected += 1
                    raise ValueError(
                        f"CROSSED_BOOK: best_bid {bid_micros} >= best_ask {ask_micros}"
                    )
            if state is None:
                state = self._open_instrument((token, key_symbol))
            self._ticks += 1
            state.spot_micros = price_micros
            state.last_update_utc_ns = stamp
            if bid_micros > 0 and ask_micros > 0:
                self._apply_ladder_state(state, [(bid_micros, 0)], [(ask_micros, 0)])
            if bids or asks:
                self._apply_ladder(state, bids, asks)

            touched: list[dict[str, Any]] = []
            closed: list[dict[str, Any]] = []
            for timeframe in TIMEFRAMES:
                bar, finished = self._roll(state, token, key_symbol, timeframe, price_micros, size_micros, stamp)
                touched.append(bar.to_dict())
                if finished is not None:
                    closed.append(finished.to_dict())
                    self._bars_closed += 1

            self._call_sink("update_spot", silo=token, symbol=key_symbol, price_micros=price_micros, now_ns=stamp)
            self._call_sink(
                "update_l2",
                silo=token,
                symbol=key_symbol,
                bids=[{"price_micros": p, "size_micros": s} for p, s in state.bids],
                asks=[{"price_micros": p, "size_micros": s} for p, s in state.asks],
                now_ns=stamp,
            )
            # V2.3 fix: dispatch the *finished* bar, not the freshly-opened one.
            # ``touched`` holds the new live bar (``closed=False`` always), so the
            # sink hook below could never fire and no consumer ever received a
            # completed bar through it.  A closed bar is final and cheap to fan
            # out; a live one is re-derived by the next tick.
            for row in closed:
                self._call_sink("ingest_bar", bar=row)

        return {
            "silo": token,
            "symbol": key_symbol,
            "price_micros": price_micros,
            "size_micros": size_micros,
            "bars": touched,
            "closed_bars": closed,
            "closed_count": len(closed),
            "ts_utc_ns": stamp,
        }

    def _open_instrument(self, key: tuple[str, str]) -> _InstrumentState:
        """Return a state slot for ``key``, evicting the coldest one if full."""
        if len(self._state) >= self.max_instruments:
            oldest = min(
                self._state.items(),
                key=lambda item: (item[1].last_update_utc_ns, item[0]),
            )[0]
            self._state.pop(oldest, None)
            self._evictions += 1
        state = _InstrumentState()
        self._state[key] = state
        return state

    def _history_for(self, state: _InstrumentState, timeframe: str) -> deque[dict[str, Any]]:
        """The bounded closed-bar ring for one timeframe (created on first close)."""

        history = state.history.get(timeframe)
        if history is None:
            history = deque(maxlen=self.max_bars)
            state.history[timeframe] = history
        return history

    def _roll(
        self,
        state: _InstrumentState,
        silo: str,
        symbol: str,
        timeframe: str,
        price_micros: int,
        size_micros: int,
        stamp: int,
    ) -> tuple[Bar, Bar | None]:
        """Advance one timeframe; return ``(live_bar, closed_bar_or_None)``."""
        seconds = TF_SECONDS[timeframe]
        span_ns = seconds * _NS_PER_SECOND
        bucket = stamp // span_ns
        start_ns = bucket * span_ns
        end_ns = start_ns + span_ns
        live = state.bars.get(timeframe)
        finished: Bar | None = None
        if live is None or live.start_utc_ns != start_ns:
            if live is not None:
                live.closed = True
                finished = live
                # ``maxlen`` drops the oldest closed bar for us: no slice, no
                # re-assignment, no unbounded list to collect.
                self._history_for(state, timeframe).append(live.to_dict())
            live = Bar(
                silo=silo,
                symbol=symbol,
                timeframe=timeframe,
                open_micros=price_micros,
                high_micros=price_micros,
                low_micros=price_micros,
                close_micros=price_micros,
                volume_micros=size_micros,
                trades=1,
                start_utc_ns=start_ns,
                end_utc_ns=end_ns,
                closed=False,
            )
            state.bars[timeframe] = live
            return live, finished
        live.high_micros = max(live.high_micros, price_micros)
        live.low_micros = min(live.low_micros, price_micros)
        live.close_micros = price_micros
        live.volume_micros += size_micros
        live.trades += 1
        return live, None

    # ------------------------------------------------------------------
    # L2 ladder
    # ------------------------------------------------------------------
    def _apply_ladder_state(
        self,
        state: _InstrumentState,
        bids: Sequence[tuple[int, int]],
        asks: Sequence[tuple[int, int]],
    ) -> None:
        state.bids = sorted([(int(p), int(s)) for p, s in bids if p > 0], key=lambda row: -row[0])[
            : self.depth_levels
        ]
        state.asks = sorted([(int(p), int(s)) for p, s in asks if p > 0], key=lambda row: row[0])[
            : self.depth_levels
        ]

    def _apply_ladder(
        self,
        state: _InstrumentState,
        bids: Sequence[Sequence[Any]],
        asks: Sequence[Sequence[Any]],
    ) -> None:
        def _rows(levels: Sequence[Sequence[Any]]) -> list[tuple[int, int]]:
            rows: list[tuple[int, int]] = []
            for level in levels:
                if isinstance(level, Mapping):
                    price = _micros(level.get("price"), name="price")
                    size = _micros(level.get("size", level.get("qty", 0)), name="size")
                elif isinstance(level, Sequence) and len(level) >= 2:
                    price = _micros(level[0], name="price")
                    size = _micros(level[1], name="size")
                else:
                    continue
                if price > 0:
                    rows.append((price, max(0, size)))
            return rows

        self._apply_ladder_state(state, _rows(bids), _rows(asks))

    # ------------------------------------------------------------------
    # Option chain -> IV / GEX surface
    # ------------------------------------------------------------------
    def on_option_chain(
        self,
        *,
        silo: str,
        symbol: str,
        expiry: str,
        strikes: Sequence[Mapping[str, Any]],
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Fold one chain snapshot into an IV / gamma-exposure surface.

        ``gamma_exposure_micros`` is a **directional-notional estimate**:
        ``sum(gamma * open_interest * contract_size * spot^2 * 0.01)`` with a sign
        per side.  It is analytics, not money, so it is reported as an integer
        micro amount alongside an explicit ``units`` label.
        """
        token = str(silo or "").strip().upper()
        key_symbol = str(symbol or "").strip().upper()
        if not token or not key_symbol:
            raise ValueError("silo and symbol are required")
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            state = self._state.setdefault((token, key_symbol), _InstrumentState())
            spot = state.spot_micros or 0
            rows: list[dict[str, Any]] = []
            total_gex = 0.0
            call_oi = 0.0
            put_oi = 0.0
            atm_iv: float | None = None
            atm_distance: int | None = None
            for strike in strikes:
                if not isinstance(strike, Mapping):
                    continue
                strike_micros = _micros(strike.get("strike"), name="strike")
                if strike_micros <= 0:
                    continue
                iv = float(strike.get("iv", strike.get("implied_volatility", 0.0)) or 0.0)
                gamma = float(strike.get("gamma", 0.0) or 0.0)
                oi = float(strike.get("open_interest", strike.get("oi", 0.0)) or 0.0)
                side = str(strike.get("type", strike.get("option_type", "CALL"))).strip().upper()
                sign = -1.0 if side.startswith("P") else 1.0
                contract_size = float(strike.get("contract_size", 1.0) or 1.0)
                gex = gamma * oi * contract_size * (spot / 1_000_000.0) ** 2 * 0.01 * sign
                total_gex += gex
                if side.startswith("P"):
                    put_oi += oi
                else:
                    call_oi += oi
                if spot > 0:
                    distance = abs(strike_micros - spot)
                    if atm_distance is None or distance < atm_distance:
                        atm_distance = distance
                        atm_iv = iv
                rows.append(
                    {
                        "strike_micros": strike_micros,
                        "type": "PUT" if side.startswith("P") else "CALL",
                        "iv": round(iv, 6),
                        "gamma": round(gamma, 8),
                        "open_interest": round(oi, 4),
                        "gamma_exposure_micros": int(round(gex * 1_000_000)),
                    }
                )
            pcr = (put_oi / call_oi) if call_oi > 0 else 0.0
            surface = {
                "silo": token,
                "symbol": key_symbol,
                "expiry": str(expiry),
                "spot_micros": int(spot),
                "strikes": rows,
                "strike_count": len(rows),
                "gamma_exposure_micros": int(round(total_gex * 1_000_000)),
                "gamma_exposure_units": "directional_notional_micros",
                "put_call_ratio": round(pcr, 6),
                "atm_iv": round(float(atm_iv), 6) if atm_iv is not None else None,
                "iv_rank_bps": int(round(float(atm_iv or 0.0) * 10_000)),
                "ts_utc_ns": stamp,
            }
            surface["surface_sha256"] = _seal(surface)
            state.surface = surface
            if atm_iv is not None and float(atm_iv) > 0.0:
                # V2.3: keep the real ATM-IV observation so ``iv_rank_bps`` can
                # be a trailing min-max rank instead of a constant.
                self._iv_history.setdefault(
                    (token, key_symbol), deque(maxlen=self._max_iv_samples)
                ).append((stamp, float(atm_iv)))
            self._surfaces += 1
            self._call_sink("update_option_surface", surface=surface, now_ns=stamp)
            return surface

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def bars(
        self,
        *,
        silo: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int | None = None,
        include_live: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        token = str(silo).strip().upper() if silo else None
        key_symbol = str(symbol).strip().upper() if symbol else None
        tf = str(timeframe).strip() if timeframe else None
        if tf is not None and tf not in TF_SECONDS:
            raise ValueError(f"UNKNOWN_TIMEFRAME:{timeframe!r}")
        with self._lock:
            rows: list[dict[str, Any]] = []
            for (silo_id, symbol_id), state in self._state.items():
                if token is not None and silo_id != token:
                    continue
                if key_symbol is not None and symbol_id != key_symbol:
                    continue
                for frame, history in state.history.items():
                    if tf is not None and frame != tf:
                        continue
                    rows.extend(history)
                if include_live:
                    for frame, live in state.bars.items():
                        if tf is not None and frame != tf:
                            continue
                        rows.append(live.to_dict())
        rows.sort(key=lambda row: (row["start_utc_ns"], row["silo"], row["symbol"], row["timeframe"]))
        if limit is not None:
            rows = rows[-max(1, int(limit)) :]
        return tuple(rows)

    def spot(self, *, silo: str, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._state.get((str(silo).strip().upper(), str(symbol).strip().upper()))
            if state is None or state.spot_micros <= 0:
                return None
            return {
                "silo": str(silo).strip().upper(),
                "symbol": str(symbol).strip().upper(),
                "price_micros": int(state.spot_micros),
                "ts_utc_ns": int(state.last_update_utc_ns),
            }

    def depth(self, *, silo: str, symbol: str) -> dict[str, Any]:
        with self._lock:
            state = self._state.get((str(silo).strip().upper(), str(symbol).strip().upper()))
            if state is None:
                return {"silo": str(silo).strip().upper(), "symbol": str(symbol).strip().upper(), "bids": [], "asks": []}
            return {
                "silo": str(silo).strip().upper(),
                "symbol": str(symbol).strip().upper(),
                "bids": [{"price_micros": p, "size_micros": s} for p, s in state.bids],
                "asks": [{"price_micros": p, "size_micros": s} for p, s in state.asks],
                "levels": self.depth_levels,
            }

    def surface(self, *, silo: str, symbol: str) -> dict[str, Any]:
        with self._lock:
            state = self._state.get((str(silo).strip().upper(), str(symbol).strip().upper()))
            if state is None or not state.surface:
                return {
                    "silo": str(silo).strip().upper(),
                    "symbol": str(symbol).strip().upper(),
                    "strike_count": 0,
                    "gamma_exposure_micros": 0,
                }
            return dict(state.surface)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self, *, now_ns: int | None = None) -> dict[str, Any]:
        with self._lock:
            instruments = [
                {
                    "silo": silo,
                    "symbol": symbol,
                    "spot_micros": int(state.spot_micros),
                    "bid_levels": len(state.bids),
                    "ask_levels": len(state.asks),
                    "bars": len(state.history.get("1m", ())) + (1 if "1m" in state.bars else 0),
                    "surface_strikes": int(state.surface.get("strike_count", 0)) if state.surface else 0,
                }
                for (silo, symbol), state in sorted(self._state.items())
            ]
            return {
                "page": "bars.status",
                "timeframes": list(TIMEFRAMES),
                "instrument_count": len(self._state),
                "instruments": instruments,
                "ticks_ingested": int(self._ticks),
                "bars_closed": int(self._bars_closed),
                "surfaces_folded": int(self._surfaces),
                "sink_attached": self.features_sink is not None,
                "sink_errors": list(self._sink_errors[-5:]),
                "max_instruments": int(self.max_instruments),
                "max_bars_per_timeframe": int(self.max_bars),
                "instruments_evicted": int(self._evictions),
                "stale_ticks_rejected": int(self._stale_rejected),
                "crossed_books_rejected": int(self._crossed_rejected),
                "as_of_utc_ns": int(now_ns if now_ns is not None else time.time_ns()),
                "law": "sec_009 - a bar is a deterministic projection of observed ticks",
            }

    def reset(self) -> dict[str, Any]:
        """Clean-slate hook: drop every instrument, bar and ladder."""
        with self._lock:
            dropped = len(self._state)
            self._state = {}
            self._iv_history = {}
            self._ticks = 0
            self._bars_closed = 0
            self._surfaces = 0
            self._sink_errors = []
            self._evictions = 0
            self._stale_rejected = 0
            self._crossed_rejected = 0
            return {"dropped": dropped, "instrument_count": 0}

    def restore(self, bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Rehydrate closed **and** live bars from a persisted snapshot (V2.3).

        Bars are the only state a restart cannot recompute: the next ``1m`` bucket
        closes on the next tick, but the previous 250 do not come back on their
        own, so a rolling ``MA``/``VWAP`` would silently restart from zero.

        Restoration is strictly defensive: a row without ``silo``/``symbol``, an
        unknown timeframe, a non-positive close or an unparseable integer is
        skipped rather than trusted.  ``last_update_utc_ns`` is seeded from the
        bar's **start** (never its bucket end, which is in the future) so the
        first real tick after a restart is not rejected as ``STALE_TICK``.
        """
        restored_closed = 0
        restored_live = 0
        seen: set[tuple[str, str]] = set()
        with self._lock:
            for row in bars or ():
                if not isinstance(row, Mapping):
                    continue
                silo = str(row.get("silo") or "").strip().upper()
                symbol = str(row.get("symbol") or "").strip().upper()
                timeframe = str(row.get("timeframe") or "").strip()
                if not silo or not symbol or timeframe not in TF_SECONDS:
                    continue
                try:
                    start_ns = int(row.get("start_utc_ns") or 0)
                    open_micros = int(row.get("open_micros") or 0)
                    high_micros = int(row.get("high_micros") or 0)
                    low_micros = int(row.get("low_micros") or 0)
                    close_micros = int(row.get("close_micros") or 0)
                    volume_micros = max(0, int(row.get("volume_micros") or 0))
                    trades = max(0, int(row.get("trades") or 0))
                except (TypeError, ValueError):
                    continue
                if close_micros <= 0 or start_ns < 0:
                    continue
                key = (silo, symbol)
                state = self._state.get(key)
                if state is None:
                    state = self._open_instrument(key)
                seen.add(key)
                bar = Bar(
                    silo=silo,
                    symbol=symbol,
                    timeframe=timeframe,
                    open_micros=open_micros or close_micros,
                    high_micros=max(high_micros, close_micros),
                    low_micros=min(low_micros or close_micros, close_micros),
                    close_micros=close_micros,
                    volume_micros=volume_micros,
                    trades=trades,
                    start_utc_ns=start_ns,
                    end_utc_ns=int(
                        row.get("end_utc_ns")
                        or start_ns + TF_SECONDS[timeframe] * _NS_PER_SECOND
                    ),
                    closed=bool(row.get("closed")),
                )
                if start_ns >= state.last_update_utc_ns:
                    state.last_update_utc_ns = start_ns
                    state.spot_micros = close_micros
                if bar.closed:
                    # Restore must keep the pre-V2.4 rule exactly: sort the whole
                    # series ascending, then retain the newest ``max_bars``.  The
                    # deque's ``maxlen`` would drop the oldest row *before* the
                    # sort and silently re-order a late (out-of-order) insert.
                    history = self._history_for(state, timeframe)
                    ordered = list(history)
                    ordered.append(bar.to_dict())
                    ordered.sort(key=lambda item: int(item.get("start_utc_ns") or 0))
                    if len(ordered) > self.max_bars:
                        ordered = ordered[-self.max_bars :]
                    history.clear()
                    history.extend(ordered)
                    restored_closed += 1
                else:
                    existing = state.bars.get(timeframe)
                    if existing is None or start_ns >= existing.start_utc_ns:
                        state.bars[timeframe] = bar
                        restored_live += 1
        return {
            "restored": restored_closed + restored_live,
            "closed": restored_closed,
            "live": restored_live,
            "instruments": len(seen),
        }

    # ------------------------------------------------------------------
    # V2.3 telemetry accessors (read-only, bounded, never raise)
    # ------------------------------------------------------------------
    def feed_telemetry(self, *, now_ns: int | None = None) -> dict[str, Any]:
        """Real per-feed tick age for ``feed_lag_ms`` (never a placeholder).

        ``feed_lag_ms`` is ``max(0, (now_utc - last_tick_utc) * 1000)`` for the
        stale/quietest active feed, aggregated per silo.  Before any tick has
        been folded the payload is ``None`` + ``feed_lag_status="IDLE"``: a
        process that has never seen a tick is idle, not zero-lag.
        """
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            raws = [
                (silo, symbol, int(state.last_update_utc_ns))
                for (silo, symbol), state in sorted(self._state.items())
                if int(state.last_update_utc_ns) > 0
            ]
            instruments = len(self._state)
            ticks = int(self._ticks)
        payload: dict[str, Any] = {
            "feed_lag_ms": None,
            "feed_lag_status": "IDLE",
            "feed_lag_by_silo": {},
            "feeds": [],
            "feed_instruments": instruments,
            "feed_ticks_ingested": ticks,
            "feed_as_of_utc_ns": stamp,
        }
        if not raws:
            return payload
        feeds: list[dict[str, Any]] = []
        by_silo: dict[str, float] = {}
        for silo, symbol, last_ns in raws:
            lag_ms = round(max(0.0, (stamp - last_ns) / 1_000_000.0), 3)
            feeds.append(
                {
                    "silo": silo,
                    "symbol": symbol,
                    "last_tick_utc_ns": last_ns,
                    "feed_lag_ms": lag_ms,
                }
            )
            current = by_silo.get(silo)
            if current is None or lag_ms > current:
                by_silo[silo] = lag_ms
        payload["feeds"] = feeds
        payload["feed_lag_by_silo"] = dict(sorted(by_silo.items()))
        payload["feed_lag_ms"] = max(by_silo.values())
        payload["feed_lag_status"] = "LIVE"
        return payload

    def record_option_iv(
        self,
        *,
        silo: str,
        symbol: str,
        atm_iv: Any,
        now_ns: int | None = None,
    ) -> int:
        """Append one observed ATM IV to the trailing window; returns the size.

        Called by the gateway after it prices a chain snapshot, and by
        :meth:`on_option_chain` when a venue chain is folded.  A non-positive or
        non-finite value is ignored: an unquoted IV is not a zero.
        """
        token = str(silo or "").strip().upper()
        key_symbol = str(symbol or "").strip().upper()
        try:
            value = float(atm_iv)
        except (TypeError, ValueError):
            return 0
        if not token or not key_symbol or not math.isfinite(value) or value <= 0.0:
            return 0
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        with self._lock:
            series = self._iv_history.setdefault(
                (token, key_symbol), deque(maxlen=self._max_iv_samples)
            )
            series.append((stamp, value))
            return len(series)

    def iv_history(
        self,
        *,
        silo: str,
        symbol: str,
        window_seconds: int = 52 * 7 * 86_400,
        now_ns: int | None = None,
    ) -> tuple[tuple[int, float], ...]:
        """Retained ``(ts_utc_ns, atm_iv)`` observations inside the window."""
        token = str(silo or "").strip().upper()
        key_symbol = str(symbol or "").strip().upper()
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        horizon = stamp - int(window_seconds) * 1_000_000_000
        with self._lock:
            samples = tuple(self._iv_history.get((token, key_symbol), ()))
        return tuple(
            (int(ts_ns), float(iv))
            for ts_ns, iv in samples
            if int(ts_ns) >= horizon and float(iv) > 0.0
        )

    def option_iv_rank(
        self,
        *,
        silo: str,
        symbol: str,
        current_iv: Any = None,
        now_ns: int | None = None,
        window_seconds: int = 52 * 7 * 86_400,
    ) -> dict[str, Any]:
        """Trailing 52-week IV Rank in bps for one instrument (``None`` if thin)."""
        from ring2_brain.quant.options_iv_rank import iv_rank_payload

        stamp = int(now_ns if now_ns is not None else time.time_ns())
        history = self.iv_history(
            silo=silo, symbol=symbol, window_seconds=window_seconds, now_ns=stamp
        )
        if current_iv is None and history:
            current_iv = history[-1][1]
        return iv_rank_payload(
            current_iv,
            history,
            now_ns=stamp,
            window_seconds=window_seconds,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _call_sink(self, hook: str, **kwargs: Any) -> None:
        sink = self.features_sink
        if sink is None:
            return
        target = getattr(sink, hook, None)
        if not callable(target):
            return
        try:
            target(**kwargs)
        except TypeError:
            # A sink with a narrower signature is simply not interested in this
            # hook; that is not an error worth failing a tick over.
            return
        except Exception as exc:  # noqa: BLE001 - a sink must never break ingestion
            self._sink_errors.append(f"{hook}:{type(exc).__name__}:{exc}")
            return
