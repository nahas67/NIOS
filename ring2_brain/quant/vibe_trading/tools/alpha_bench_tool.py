"""Benchmark tool helpers referenced by the harvested factor runners.

These are the pure orchestration pieces: panel preparation, forward returns,
universe filtering and HTML report scaffolding.  Network loading is delegated
to the caller's loader, so importing this module never touches a vendor.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

__all__ = [
    "_CSP",
    "_MAX_HORIZON",
    "_REPORT_CSS",
    "_compute_forward_returns",
    "_default_output_dir",
    "_load_universe_panel",
    "_prepare_bench_panel",
    "_render_html",
    "_split_alphas_by_universe_support",
    "_validate_bench_data_requirements",
]

_MAX_HORIZON = 20
_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:;"
_REPORT_CSS = "body{font-family:system-ui,sans-serif;margin:2rem}table{border-collapse:collapse}td,th{padding:.35rem .6rem;border:1px solid #ddd}"


def _compute_forward_returns(
    close: pd.DataFrame | pd.Series,
    horizon: int = 1,
) -> pd.DataFrame | pd.Series:
    if int(horizon) < 1 or int(horizon) > _MAX_HORIZON:
        raise ValueError(f"horizon must be within [1, {_MAX_HORIZON}]")
    return close.shift(-int(horizon)) / close - 1.0


def _prepare_bench_panel(panel: Mapping[str, Any]) -> dict[str, Any]:
    prepared = dict(panel)
    close = prepared.get("close")
    if isinstance(close, pd.Series):
        prepared["close"] = close.to_frame()
    return prepared


def _split_alphas_by_universe_support(
    alphas: Iterable[Any],
    universe: str | None = None,
) -> tuple[list[Any], list[Any]]:
    supported, unsupported = [], []
    for alpha in alphas:
        meta = getattr(alpha, "__alpha_meta__", None) or {}
        markets = meta.get("universe") or ()
        (supported if not universe or universe in markets else unsupported).append(alpha)
    return supported, unsupported


def _validate_bench_data_requirements(
    panel: Mapping[str, Any],
    requirements: Iterable[str] = (),
) -> list[str]:
    missing = [name for name in requirements if name not in panel]
    if missing:
        raise ValueError(f"benchmark panel is missing columns: {missing}")
    return missing


def _default_output_dir(root: str | Path | None = None) -> Path:
    path = Path(root) if root is not None else Path.cwd() / "reports" / "alpha_bench"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _render_html(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta http-equiv='Content-Security-Policy' content=\"{_CSP}\">"
        f"<title>{html.escape(title)}</title><style>{_REPORT_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def _load_universe_panel(*args: Any, **kwargs: Any) -> pd.DataFrame:
    raise RuntimeError(
        "no universe panel is configured; pass a prepared panel to the bench runner"
    )
