"""Longbridge SDK configuration stub."""

from __future__ import annotations

from typing import Any

__all__ = ["build_sdk_config"]


def build_sdk_config(credentials: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    return {**dict(credentials or {}), **dict(overrides)}
