"""Run-scoped configuration for the TradingAgents tools."""

from __future__ import annotations

import contextvars
from contextlib import AbstractContextManager
from functools import partial
from typing import Any, Mapping

__all__ = [
    "get_config",
    "set_config",
    "run_config",
    "run_config_context",
    "reset_config",
]

_DEFAULTS: dict[str, Any] = {
    "output_language": "English",
    "data_cache_dir": "data_cache",
    "results_dir": "results",
    "llm_max_retries": 3,
    "global_news_lookback_days": 7,
}

_CONFIG: dict[str, Any] = dict(_DEFAULTS)
_CURRENT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "tradingagents_config", default=_CONFIG
)


def get_config() -> dict[str, Any]:
    """Return the configuration visible to the current run."""
    return dict(_CURRENT.get())


def set_config(config: Mapping[str, Any] | None) -> None:
    """Replace the process default configuration."""
    global _CONFIG
    merged = dict(_DEFAULTS)
    merged.update(dict(config or {}))
    _CONFIG = merged
    _CURRENT.set(merged)


def reset_config() -> None:
    set_config(_DEFAULTS)


class _RunConfig(AbstractContextManager["_RunConfig"]):
    """Context manager that also offers ``Context.run``-style execution."""

    def __init__(self, config: Mapping[str, Any] | None) -> None:
        self._config = dict(_CURRENT.get())
        self._config.update(dict(config or {}))
        self._token: contextvars.Token[dict[str, Any]] | None = None

    def __enter__(self) -> "_RunConfig":
        self._token = _CURRENT.set(self._config)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None

    def run(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run ``func`` under this configuration context."""
        ctx = contextvars.copy_context()
        ctx.run(partial(_CURRENT.set, self._config))
        return ctx.run(func, *args, **kwargs)


def run_config(config: Mapping[str, Any] | None = None) -> _RunConfig:
    return _RunConfig(config)


def run_config_context(config: Mapping[str, Any] | None = None) -> _RunConfig:
    return _RunConfig(config)
