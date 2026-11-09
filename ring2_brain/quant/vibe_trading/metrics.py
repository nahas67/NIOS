"""Backtest metrics used by the harvested engines."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

__all__ = [
    "bar_returns",
    "by_exit_reason_stats",
    "by_symbol_stats",
    "calc_fill_turnover_series",
    "calc_metrics",
    "effective_bars_per_year",
    "entry_returns",
    "period_returns",
]


def bar_returns(equity: pd.Series) -> pd.Series:
    return equity.pct_change().fillna(0.0)


def period_returns(equity: pd.Series, period: str = "M") -> pd.Series:
    return equity.resample(period).last().pct_change().dropna()


def entry_returns(trades: Iterable[Any]) -> list[float]:
    out = []
    for trade in trades:
        entry = float(getattr(trade, "entry_price", 0.0) or 0.0)
        exit_price = float(getattr(trade, "exit_price", 0.0) or 0.0)
        sign = 1.0 if str(getattr(trade, "side", "")).lower() in {"long", "buy"} else -1.0
        if entry > 0.0:
            out.append(sign * (exit_price / entry - 1.0))
    return out


def effective_bars_per_year(index: pd.Index) -> float:
    if len(index) < 2:
        return 252.0
    try:
        span = (pd.Timestamp(index[-1]) - pd.Timestamp(index[0])).days
    except Exception:
        return 252.0
    return (len(index) - 1) * 365.25 / span if span > 0 else 252.0


def calc_fill_turnover_series(fills: Iterable[Any]) -> pd.Series:
    rows = [
        (getattr(fill, "ts", None), abs(float(getattr(fill, "quantity", 0.0) or 0.0)))
        for fill in fills
    ]
    if not rows:
        return pd.Series(dtype=float)
    frame = pd.DataFrame(rows, columns=["ts", "quantity"]).dropna(subset=["ts"])
    return frame.groupby("ts")["quantity"].sum().sort_index()


def _group_stats(trades: Iterable[Any], key: str) -> dict[str, dict[str, float]]:
    groups: dict[str, list[float]] = {}
    for trade in trades:
        label = str(getattr(trade, key, "") or "unknown")
        groups.setdefault(label, []).append(float(getattr(trade, "pnl", 0.0) or 0.0))
    return {
        label: {
            "count": float(len(values)),
            "total_pnl": float(sum(values)),
            "mean_pnl": float(np.mean(values)) if values else 0.0,
        }
        for label, values in groups.items()
    }


def by_exit_reason_stats(trades: Iterable[Any]) -> dict[str, dict[str, float]]:
    return _group_stats(trades, "exit_reason")


def by_symbol_stats(trades: Iterable[Any]) -> dict[str, dict[str, float]]:
    return _group_stats(trades, "symbol")


def calc_metrics(
    equity: pd.Series,
    trades: Iterable[Any] = (),
    **_: Any,
) -> dict[str, float]:
    values = pd.Series(equity).astype(float).dropna()
    if len(values) < 2:
        return {"total_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0, "trade_count": 0.0}
    returns = values.pct_change().dropna()
    std = float(returns.std(ddof=1))
    running = values.cummax()
    drawdown = float((values / running - 1.0).min())
    return {
        "total_return": float(values.iloc[-1] / values.iloc[0] - 1.0),
        "sharpe": float(returns.mean() / std * np.sqrt(252.0)) if std > 0 else 0.0,
        "max_drawdown": drawdown,
        "trade_count": float(len(list(trades))),
    }
