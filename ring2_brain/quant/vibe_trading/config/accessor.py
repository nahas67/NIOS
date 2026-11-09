"""Configuration accessor used by the harvested factor backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["EnvConfig", "get_env_config"]


@dataclass(frozen=True, slots=True)
class _AgentTuning:
    vibe_trading_disable_bottleneck: bool = False


@dataclass(frozen=True, slots=True)
class EnvConfig:
    agent_tuning: _AgentTuning = _AgentTuning()
    data_root: str = ""


_CONFIG = EnvConfig()


def get_env_config() -> EnvConfig:
    """Return the process environment configuration (immutable snapshot)."""
    return _CONFIG


def get_env_value(name: str, default: object = None) -> object:
    """Return one environment value, with an explicit default.

    The donor loaders use this for vendor API keys, so an absent key is a
    normal configuration state rather than an error.
    """
    import os

    return os.environ.get(str(name), default)
