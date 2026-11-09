"""Global settings shim for the harvested ``vnpy_core`` tree.

Upstream ``vnpy.trader.setting`` persists a small ``SETTINGS`` mapping to
``.vntrader/vt_setting.json``.  The NIOS harvest kept ``engine.py``'s
``from .setting import SETTINGS`` but not the module, so this file restores the
contract with Ring 0 safe defaults: **no email, no logging to a shared file, and
no datafeed credentials read at import time**.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

__all__ = ["SETTINGS", "GlobalSetting", "DEFAULT_SETTINGS", "get_setting", "update_setting"]

DEFAULT_SETTINGS: dict[str, Any] = {
    "font.family": "Arial",
    "font.size": 12,
    "log.active": True,
    "log.level": 20,
    "log.console": True,
    "log.file": False,
    "email.server": "",
    "email.port": 465,
    "email.username": "",
    "email.password": "",
    "email.sender": "",
    "email.receiver": "",
    "datafeed.name": "",
    "datafeed.username": "",
    "datafeed.password": "",
    "database.timezone": "Asia/Shanghai",
    "database.name": "sqlite",
    "database.database": str(Path.home() / ".vntrader" / "database.db"),
    "database.host": "",
    "database.port": 0,
    "database.user": "",
    "database.password": "",
}

#: Live mapping read by ``engine.py``.  Mutated in place -- never rebound.
SETTINGS: dict[str, Any] = dict(DEFAULT_SETTINGS)


def get_setting(key: str, default: Any = None) -> Any:
    """Read one setting with a default."""

    return SETTINGS.get(key, default)


def update_setting(values: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``values`` into :data:`SETTINGS` and return the result."""

    SETTINGS.update(dict(values))
    return SETTINGS


class GlobalSetting:
    """Attribute-style view over :data:`SETTINGS` (upstream compatibility)."""

    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        self._settings = dict(settings or SETTINGS)

    def __getattr__(self, item: str) -> Any:
        key = item.replace("_", ".")
        try:
            return self._settings[key]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._settings)
