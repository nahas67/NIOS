"""NIOS Core v3.0 -- shared substrate package (STEP 2).

Submodules are imported lazily so that a Ring 1 process can pull
``core.schemas`` or ``core.shm_ipc`` without dragging DuckDB (Tier 2) or the
NATS client (Tier 1) into its address space.  Keeping the import graph thin is
part of the air-gap contract in ``sec_011``.
"""

from __future__ import annotations

from typing import Any

__version__ = "3.0.0-step2"

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # schemas
    "LeaseMode": ("core.schemas", "LeaseMode"),
    "VaultName": ("core.schemas", "VaultName"),
    "System1Lease": ("core.schemas", "System1Lease"),
    "DualVendorBar": ("core.schemas", "DualVendorBar"),
    "ExecutedFlowTick": ("core.schemas", "ExecutedFlowTick"),
    "GroundedFactPacket": ("core.schemas", "GroundedFactPacket"),
    "ContagionFeatures": ("core.schemas", "ContagionFeatures"),
    "UnsignedProposal": ("core.schemas", "UnsignedProposal"),
    "PlainEnglishCockpitCard": ("core.schemas", "PlainEnglishCockpitCard"),
    "TemporalIntegrityError": ("core.schemas", "TemporalIntegrityError"),
    # tier 0
    "GlobalKillLatch": ("core.shm_ipc", "GlobalKillLatch"),
    "LeaseTableSHM": ("core.shm_ipc", "LeaseTableSHM"),
    "OfiRingBuffer": ("core.shm_ipc", "OfiRingBuffer"),
    "ShadowBookSHM": ("core.shm_ipc", "ShadowBookSHM"),
    # tier 1
    "LocalJetStreamBus": ("core.nats_bus", "LocalJetStreamBus"),
    "JetStreamBus": ("core.nats_bus", "JetStreamBus"),
    # tier 2
    "FeatureStore": ("core.feature_store", "FeatureStore"),
}

__all__ = ["__version__", *_LAZY_EXPORTS]


def __getattr__(name: str) -> Any:
    try:
        module_path, attribute = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module 'core' has no attribute {name!r}") from exc
    import importlib

    module = importlib.import_module(module_path)
    value = getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
