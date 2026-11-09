"""Activation of every harvested Vibe-Trading loader and regional engine.

``sec_005`` / ``sec_006`` require the full donor data plane to be live before a
single dual-vendor bar can reach the self-healing guard.  The harvest kept the
upstream files verbatim under :mod:`ring2_brain.quant.vibe_trading.loaders` and
:mod:`ring2_brain.quant.vibe_trading.engines`; this module is the single,
idempotent activation switch:

* install the upstream namespace aliases (:mod:`ring2_brain._donor_compat`);
* import **every** ``*.py`` module in both directories;
* record the exported regional engine classes;
* report file counts, module counts and any failure verbatim.

Nothing here opens a socket, reads a broker credential or calls an LLM.  The
loaders are *available*; they only touch the network when a caller explicitly
invokes ``DataLoader.fetch``.  That keeps the activation pass safe on an
air-gapped host and in CI.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ring2_brain._donor_compat import install_donor_aliases

__all__ = [
    "ENGINES_DIR",
    "ENGINES_PACKAGE",
    "LOADERS_DIR",
    "LOADERS_PACKAGE",
    "ActivationEntry",
    "ActivationReport",
    "activate_loaders_and_engines",
    "loader_activation_status",
    "regional_engine_classes",
]

_PACKAGE_ROOT = Path(__file__).resolve().parent
LOADERS_DIR = _PACKAGE_ROOT / "loaders"
ENGINES_DIR = _PACKAGE_ROOT / "engines"
LOADERS_PACKAGE = "ring2_brain.quant.vibe_trading.loaders"
ENGINES_PACKAGE = "ring2_brain.quant.vibe_trading.engines"

#: The 12 regional engines (plus the cross-market composite) that ``sec_005``
#: names.  A module that stops exporting its class is a regression, not a
#: silent absence.
REGIONAL_ENGINE_EXPORTS: dict[str, tuple[str, ...]] = {
    "argentina_equity": ("ArgentinaEquityEngine",),
    "brazil_equity": ("BrazilEquityEngine",),
    "china_a": ("ChinaAEngine",),
    "china_futures": ("ChinaFuturesEngine",),
    "composite": ("CompositeEngine",),
    "crypto": ("CryptoEngine",),
    "forex": ("ForexEngine",),
    "global_equity": ("GlobalEquityEngine",),
    "global_futures": ("GlobalFuturesEngine",),
    "india_equity": ("IndiaEquityEngine",),
    "korea_equity": ("KoreaEquityEngine",),
    "options_portfolio": ("OptionPosition", "run_options_backtest"),
    "vietnam_equity": ("VietnamEquityEngine",),
}


@dataclass(slots=True, frozen=True)
class ActivationEntry:
    """One imported module plus the names it exports."""

    module: str
    ok: bool
    error: str = ""
    exports: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "ok": self.ok,
            "error": self.error,
            "exports": list(self.exports),
        }


@dataclass(slots=True)
class ActivationReport:
    """Result of one activation pass."""

    loaders: tuple[ActivationEntry, ...] = ()
    engines: tuple[ActivationEntry, ...] = ()
    loader_file_count: int = 0
    engine_file_count: int = 0
    registry_sources: tuple[str, ...] = ()
    engine_classes: dict[str, str] = field(default_factory=dict)

    @property
    def loader_modules(self) -> tuple[str, ...]:
        return tuple(entry.module for entry in self.loaders)

    @property
    def engine_modules(self) -> tuple[str, ...]:
        return tuple(entry.module for entry in self.engines)

    @property
    def loader_failures(self) -> tuple[ActivationEntry, ...]:
        return tuple(entry for entry in self.loaders if not entry.ok)

    @property
    def engine_failures(self) -> tuple[ActivationEntry, ...]:
        return tuple(entry for entry in self.engines if not entry.ok)

    @property
    def ok(self) -> bool:
        return not self.loader_failures and not self.engine_failures

    @property
    def import_error_count(self) -> int:
        return len(self.loader_failures) + len(self.engine_failures)

    @property
    def total_modules(self) -> int:
        return len(self.loaders) + len(self.engines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "import_errors": self.import_error_count,
            "loader_file_count": self.loader_file_count,
            "engine_file_count": self.engine_file_count,
            "loader_module_count": len(self.loaders),
            "engine_module_count": len(self.engines),
            "total_modules": self.total_modules,
            "registry_sources": list(self.registry_sources),
            "engine_classes": dict(self.engine_classes),
            "loaders": [entry.to_dict() for entry in self.loaders],
            "engines": [entry.to_dict() for entry in self.engines],
            "failures": [
                entry.to_dict()
                for entry in (*self.loader_failures, *self.engine_failures)
            ],
        }


def _module_names(directory: Path, package: str) -> list[str]:
    if not directory.is_dir():
        return []
    names: list[str] = []
    for info in pkgutil.iter_modules([str(directory)]):
        if info.name == "__init__":
            continue
        names.append(f"{package}.{info.name}")
    return sorted(names)


def _file_count(directory: Path) -> int:
    """Count harvested files (``*.py`` plus data sidecars, no caches)."""
    if not directory.is_dir():
        return 0
    return sum(
        1
        for path in directory.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )


def _import_entry(module_name: str, expected: Iterable[str] = ()) -> ActivationEntry:
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - reported verbatim
        return ActivationEntry(
            module=module_name,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    exports = tuple(
        name for name in expected if hasattr(module, name)
    )
    return ActivationEntry(module=module_name, ok=True, exports=exports)


def _registry_sources() -> tuple[str, ...]:
    try:
        registry = importlib.import_module(f"{LOADERS_PACKAGE}.registry")
        return tuple(sorted(getattr(registry, "VALID_SOURCES", ())))
    except Exception:  # pragma: no cover - registry is part of the harvest
        return ()


def activate_loaders_and_engines(*, refresh: bool = False) -> ActivationReport:
    """Import every harvested loader/engine module exactly once.

    ``refresh=True`` forces a fresh pass (tests use it); otherwise the cached
    report is returned so hot paths do not re-walk the filesystem.
    """
    global _REPORT
    if _REPORT is not None and not refresh:
        return _REPORT

    install_donor_aliases()

    loaders = tuple(
        _import_entry(name) for name in _module_names(LOADERS_DIR, LOADERS_PACKAGE)
    )
    engines = tuple(
        _import_entry(
            name,
            REGIONAL_ENGINE_EXPORTS.get(name.rsplit(".", 1)[-1], ()),
        )
        for name in _module_names(ENGINES_DIR, ENGINES_PACKAGE)
    )

    engine_classes: dict[str, str] = {}
    for entry in engines:
        for export in entry.exports:
            engine_classes[export] = entry.module

    _REPORT = ActivationReport(
        loaders=loaders,
        engines=engines,
        loader_file_count=_file_count(LOADERS_DIR),
        engine_file_count=_file_count(ENGINES_DIR),
        registry_sources=_registry_sources(),
        engine_classes=engine_classes,
    )
    return _REPORT


_REPORT: ActivationReport | None = None


def loader_activation_status(*, refresh: bool = False) -> dict[str, Any]:
    """JSON-safe activation snapshot for the cockpit / tests."""
    return activate_loaders_and_engines(refresh=refresh).to_dict()


def regional_engine_classes(*, refresh: bool = False) -> dict[str, type]:
    """Resolve every regional engine class by its exported name."""
    report = activate_loaders_and_engines(refresh=refresh)
    resolved: dict[str, type] = {}
    for name, module_name in report.engine_classes.items():
        module = importlib.import_module(module_name)
        candidate = getattr(module, name, None)
        if isinstance(candidate, type):
            resolved[name] = candidate
    return resolved
