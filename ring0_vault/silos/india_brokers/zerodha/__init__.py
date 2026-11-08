"""Zerodha Kite adapter package root (Gap A closure, Step 7).

The harvested tree under this package contains three sibling subpackages:
``api`` (REST order/data/funds clients), ``mapping`` (OpenAlgo request /
response translators) and ``streaming`` (Kite WebSocket + order adapter).

Only the package root was missing during the Step 1 harvest, which made
``ring0_vault.silos.silo_registry`` probe Zerodha as ``MISSING`` while Dhan,
AngelOne and Upstox probed ``READY``.  Re-exporting the subpackages here
restores the package boundary without changing any harvested source.

The subpackages are imported lazily on first attribute access on purpose:
``streaming.zerodha_adapter`` imports the OpenAlgo runtime ``utils`` package,
which is not present in an air-gapped Ring 0 install.  Importing the package
root must never fail for that reason -- Ring 0 only needs the adapter boundary
to be importable before a silo worker is armed (``sec_004``).
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["api", "mapping", "streaming", "SUBMODULES", "submodule"]

#: Harvested sibling subpackages exposed by this package root.
SUBMODULES: tuple[str, ...] = ("api", "mapping", "streaming")


def submodule(name: str) -> Any:
    """Import and return one harvested subpackage by name.

    Raises :class:`AttributeError` for anything outside :data:`SUBMODULES`
    so a typo cannot silently resolve to an unrelated module.
    """
    token = str(name).strip()
    if token not in SUBMODULES:
        raise AttributeError(f"zerodha has no harvested submodule {name!r}")
    module = importlib.import_module(f"{__name__}.{token}")
    globals()[token] = module
    return module


def __getattr__(name: str) -> Any:
    if name in SUBMODULES:
        return submodule(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *SUBMODULES})
