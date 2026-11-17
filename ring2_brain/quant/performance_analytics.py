"""NIOS V2.1 -- equity-curve and performance analytics engine (``G8``).

The cockpit needs to answer three questions without asking the ledger to lie:

1. *What is the account worth over time?* -- a NAV equity curve.
2. *How bad was the worst stretch?* -- an underwater drawdown series.
3. *Is the edge real?* -- Sharpe, Sortino, Calmar, win rate, profit factor.

``PerformanceAnalytics`` is pure compute over rows it is **handed**:

.. code-block:: python

    {"ts_utc_ns": ..., "nav_micros": ..., "pnl_micros": ..., "silo": ...,
     "mission_id": ..., "tranche_id": ..., "trade_id": ...}

It never imports the ledger, never opens a socket, never holds a key (Rule 4).
Money in and money out is integer micro-units; ratios are dimensionless floats
labelled as such, which is why the module can honestly claim
``money_units="integer_micros"`` in its own status document.

Harvest note (Rule 1): the Sharpe / Sortino / Calmar / profit-factor definitions
are translated from ``ranaroussi/quantstats`` (``sharpe``, ``sortino``,
``calmar``, ``profit_factor``, ``to_drawdown_series``).  No donor code is imported.

Formula decisions that matter and are therefore stated explicitly:

* **Sharpe** uses the excess-of-zero daily return series, population standard
  deviation, and an annualisation factor of ``sqrt(252)`` for daily buckets
  (``sqrt(252 * periods_per_day)`` for intraday buckets).
* **Sortino** divides by the *downside* deviation computed over negative returns
  only, with the same annualisation.  A series with no negative return reports
  ``None`` -- not infinity.
* **Calmar** is annualised return over the absolute maximum drawdown.  A series
  with no drawdown reports ``None`` for the same reason.
* **Profit factor** is gross profit / gross loss over closed trades.  A series
  with no losses reports ``None``.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "BUCKETS",
    "PerformanceAnalytics",
    "PerformanceRow",
]

#: Supported curve granularities (seconds per bucket).
BUCKETS: Mapping[str, int] = {"1h": 3_600, "1D": 86_400, "1W": 604_800}

_NS_PER_SECOND = 1_000_000_000

#: Trading days assumed for annualisation.
TRADING_DAYS: int = 252


def _seal(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class PerformanceRow(dict):
    """A validated performance observation.

    Subclassing ``dict`` keeps the row trivially serialisable while the
    constructor enforces the integer-micro money contract at the boundary.
    """

    def __init__(
        self,
        *,
        ts_utc_ns: int,
        nav_micros: int,
        pnl_micros: int = 0,
        silo: str = "",
        mission_id: str = "",
        tranche_id: str = "",
        trade_id: str = "",
    ) -> None:
        for name, value in (("ts_utc_ns", ts_utc_ns), ("nav_micros", nav_micros), ("pnl_micros", pnl_micros)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int micro/ns value, got {type(value).__name__}")
        super().__init__(
            ts_utc_ns=int(ts_utc_ns),
            nav_micros=int(nav_micros),
            pnl_micros=int(pnl_micros),
            silo=str(silo or "").strip().upper(),
            mission_id=str(mission_id or ""),
            tranche_id=str(tranche_id or ""),
            trade_id=str(trade_id or ""),
        )


class PerformanceAnalytics:
    """Deterministic performance math over handed-in rows (``G8``)."""

    def __init__(self, *, risk_free_annual: float = 0.0) -> None:
        self.risk_free_annual = float(risk_free_annual)

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------
    @staticmethod
    def _rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
        normalised: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, Mapping):
                try:
                    ts = int(row.get("ts_utc_ns", 0))
                except (TypeError, ValueError):
                    continue
                try:
                    nav = int(row.get("nav_micros", 0))
                except (TypeError, ValueError):
                    continue
                try:
                    pnl = int(row.get("pnl_micros", 0))
                except (TypeError, ValueError):
                    pnl = 0
                normalised.append(
                    {
                        "ts_utc_ns": ts,
                        "nav_micros": nav,
                        "pnl_micros": pnl,
                        "silo": str(row.get("silo", "")).strip().upper(),
                        "mission_id": str(row.get("mission_id", "")),
                        "tranche_id": str(row.get("tranche_id", "")),
                        "trade_id": str(row.get("trade_id", "")),
                    }
                )
            else:
                raise TypeError(f"performance row must be a mapping, got {type(row).__name__}")
        normalised.sort(key=lambda item: (item["ts_utc_ns"], item["trade_id"]))
        return normalised

    def _filter(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        silo: str | None,
        mission_id: str | None,
    ) -> list[dict[str, Any]]:
        token = str(silo).strip().upper() if silo else None
        mission = str(mission_id) if mission_id else None
        selected: list[dict[str, Any]] = []
        for row in rows:
            if token is not None and str(row["silo"]) != token:
                continue
            if mission is not None and str(row["mission_id"]) != mission:
                continue
            selected.append(dict(row))
        return selected

    # ------------------------------------------------------------------
    # Series math
    # ------------------------------------------------------------------
    @staticmethod
    def _buckets(rows: Sequence[Mapping[str, Any]], bucket: str) -> list[dict[str, Any]]:
        span = BUCKETS.get(str(bucket), BUCKETS["1D"]) * _NS_PER_SECOND
        out: list[dict[str, Any]] = []
        for row in rows:
            key = int(row["ts_utc_ns"]) // span
            if out and out[-1]["bucket"] == key:
                out[-1]["nav_micros"] = int(row["nav_micros"])
                out[-1]["pnl_micros"] += int(row["pnl_micros"])
                out[-1]["observations"] += 1
                out[-1]["end_utc_ns"] = int(row["ts_utc_ns"])
            else:
                out.append(
                    {
                        "bucket": key,
                        "start_utc_ns": int(key * span),
                        "end_utc_ns": int(row["ts_utc_ns"]),
                        "nav_micros": int(row["nav_micros"]),
                        "pnl_micros": int(row["pnl_micros"]),
                        "observations": 1,
                    }
                )
        return out

    @staticmethod
    def _returns(curve: Sequence[Mapping[str, Any]]) -> list[float]:
        returns: list[float] = []
        for previous, current in zip(curve, curve[1:]):
            base = float(previous["nav_micros"])
            if base == 0.0:
                continue
            returns.append((float(current["nav_micros"]) - base) / base)
        return returns

    @staticmethod
    def _annualiser(bucket: str) -> float:
        span = BUCKETS.get(str(bucket), BUCKETS["1D"])
        periods_per_year = (365.0 * 86_400.0) / float(span)
        return math.sqrt(periods_per_year)

    @staticmethod
    def _mean(values: Sequence[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _stdev(values: Sequence[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = PerformanceAnalytics._mean(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return math.sqrt(max(0.0, variance))

    def _sharpe(self, returns: Sequence[float], bucket: str) -> float | None:
        if len(returns) < 2:
            return None
        per_period_rf = (1.0 + self.risk_free_annual) ** (1.0 / ((365.0 * 86_400.0) / BUCKETS.get(bucket, BUCKETS["1D"]))) - 1.0
        excess = [value - per_period_rf for value in returns]
        deviation = self._stdev(excess)
        if deviation == 0.0:
            return None
        return round(self._mean(excess) / deviation * self._annualiser(bucket), 6)

    def _sortino(self, returns: Sequence[float], bucket: str) -> float | None:
        if len(returns) < 2:
            return None
        downside = [value for value in returns if value < 0.0]
        if not downside:
            return None
        deviation = math.sqrt(sum(value * value for value in downside) / len(returns))
        if deviation == 0.0:
            return None
        return round(self._mean(returns) / deviation * self._annualiser(bucket), 6)

    @staticmethod
    def _drawdown(curve: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int, float]:
        """Return ``(underwater_series, max_drawdown_micros, max_drawdown_pct)``."""
        series: list[dict[str, Any]] = []
        peak = 0
        worst_micros = 0
        worst_pct = 0.0
        for point in curve:
            nav = int(point["nav_micros"])
            peak = max(peak, nav)
            drawdown = nav - peak
            pct = (drawdown / peak) if peak > 0 else 0.0
            series.append(
                {
                    "start_utc_ns": int(point["start_utc_ns"]),
                    "nav_micros": nav,
                    "peak_micros": int(peak),
                    "drawdown_micros": int(drawdown),
                    "drawdown_pct": round(pct * 100.0, 6),
                }
            )
            if drawdown < worst_micros:
                worst_micros = drawdown
                worst_pct = abs(pct)
        return series, int(worst_micros), round(worst_pct, 8)

    def _calmar(self, curve: Sequence[Mapping[str, Any]], max_dd_pct: float) -> float | None:
        if len(curve) < 2 or max_dd_pct <= 0.0:
            return None
        start = float(curve[0]["nav_micros"])
        end = float(curve[-1]["nav_micros"])
        if start <= 0.0:
            return None
        total_return = (end - start) / start
        span_ns = max(1, int(curve[-1]["start_utc_ns"]) - int(curve[0]["start_utc_ns"]))
        years = span_ns / (365.0 * 86_400.0 * _NS_PER_SECOND)
        if years <= 0.0:
            return None
        annualised = (1.0 + total_return) ** (1.0 / years) - 1.0
        return round(annualised / max_dd_pct, 6)

    @staticmethod
    def _trade_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        pnls = [int(row["pnl_micros"]) for row in rows if int(row["pnl_micros"]) != 0]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor: float | None
        if gross_loss == 0:
            profit_factor = None
        else:
            profit_factor = round(gross_profit / gross_loss, 6)
        return {
            "trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / len(pnls), 4) if pnls else 0.0,
            "gross_profit_micros": int(gross_profit),
            "gross_loss_micros": int(gross_loss),
            "net_pnl_micros": int(gross_profit - gross_loss),
            "profit_factor": profit_factor,
            "avg_win_micros": int(gross_profit // len(wins)) if wins else 0,
            "avg_loss_micros": int(-(gross_loss // len(losses))) if losses else 0,
            "largest_win_micros": int(max(wins)) if wins else 0,
            "largest_loss_micros": int(min(losses)) if losses else 0,
        }

    @staticmethod
    def _attribute(rows: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            token = str(row.get(key, "") or "UNASSIGNED")
            entry = buckets.setdefault(
                token,
                {"key": token, "net_pnl_micros": 0, "trades": 0, "wins": 0, "losses": 0},
            )
            pnl = int(row["pnl_micros"])
            entry["net_pnl_micros"] += pnl
            if pnl != 0:
                entry["trades"] += 1
                if pnl > 0:
                    entry["wins"] += 1
                else:
                    entry["losses"] += 1
        out = sorted(buckets.values(), key=lambda item: (-int(item["net_pnl_micros"]), item["key"]))
        for entry in out:
            trades = int(entry["trades"])
            entry["win_rate_pct"] = round(100.0 * int(entry["wins"]) / trades, 4) if trades else 0.0
        return out

    # ------------------------------------------------------------------
    # Public report
    # ------------------------------------------------------------------
    def report(
        self,
        rows: Iterable[Any],
        *,
        silo: str | None = None,
        mission_id: str | None = None,
        bucket: str = "1D",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Build the full performance document for the requested slice."""
        granularity = str(bucket or "1D")
        if granularity not in BUCKETS:
            raise ValueError(f"UNKNOWN_BUCKET:{bucket!r}")
        stamp = int(now_ns if now_ns is not None else time.time_ns())
        all_rows = self._rows(rows)
        selected = self._filter(all_rows, silo=silo, mission_id=mission_id)
        curve = self._buckets(selected, granularity)
        returns = self._returns(curve)
        underwater, max_dd_micros, max_dd_pct = self._drawdown(curve)
        first_nav = int(curve[0]["nav_micros"]) if curve else 0
        last_nav = int(curve[-1]["nav_micros"]) if curve else 0
        stats = self._trade_stats(selected)
        payload = {
            "page": "analytics.performance",
            "schema_version": "v2.1.performance",
            "bucket": granularity,
            "buckets": list(BUCKETS),
            "silo": str(silo).strip().upper() if silo else None,
            "mission_id": str(mission_id) if mission_id else None,
            "row_count": len(selected),
            "bucket_count": len(curve),
            "first_nav_micros": first_nav,
            "last_nav_micros": last_nav,
            "total_return_pct": round(
                100.0 * (last_nav - first_nav) / first_nav, 6
            ) if first_nav else 0.0,
            "equity_curve": [
                {
                    "start_utc_ns": int(point["start_utc_ns"]),
                    "end_utc_ns": int(point["end_utc_ns"]),
                    "nav_micros": int(point["nav_micros"]),
                    "pnl_micros": int(point["pnl_micros"]),
                    "observations": int(point["observations"]),
                }
                for point in curve
            ],
            "underwater": underwater,
            "max_drawdown_micros": int(max_dd_micros),
            "max_drawdown_pct": round(max_dd_pct * 100.0, 6),
            "sharpe": self._sharpe(returns, granularity),
            "sortino": self._sortino(returns, granularity),
            "calmar": self._calmar(curve, max_dd_pct),
            "risk_free_annual": round(self.risk_free_annual, 6),
            "annualisation_basis": granularity,
            **stats,
            "by_silo": self._attribute(selected, "silo"),
            "by_tranche": self._attribute(selected, "tranche_id"),
            "by_mission": self._attribute(selected, "mission_id"),
            "money_units": "integer_micros",
            "ratio_units": "dimensionless",
            "as_of_utc_ns": stamp,
            "law": "sec_013 - a performance figure is a deterministic function of settled rows",
        }
        payload["report_sha256"] = _seal(
            {key: value for key, value in payload.items() if key != "report_sha256"}
        )
        return payload

    def status(self) -> dict[str, Any]:
        return {
            "page": "analytics.status",
            "buckets": list(BUCKETS),
            "metrics": [
                "sharpe",
                "sortino",
                "calmar",
                "max_drawdown",
                "win_rate",
                "profit_factor",
            ],
            "attribution": ["silo", "tranche_id", "mission_id"],
            "money_units": "integer_micros",
            "reads_ledger": False,
            "law": "sec_013 - Ring 2 analytics never touches Ring 0 state",
        }
