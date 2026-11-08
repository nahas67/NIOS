"""Ring 0 silos -- four sovereign broker silos with zero cross-border wires.

``silo_registry`` is the only module here that Ring 3 may import: it is a pure
control-plane description of the silos (mode, tax floor, ratchet assets, adapter
readiness) and it places no orders.

``india_brokers``, ``fx_mt5``, ``key_vault`` and ``analyzer_shadow`` are
harvested donor trees.  They are namespace directories rather than packages, so
they are reached by explicit submodule path (for example
``ring0_vault.silos.india_brokers.dhan``) and are never star-imported -- that is
also what keeps a broker SDK out of Ring 0's import graph until a silo worker
loads it explicitly.
"""

from __future__ import annotations

__all__ = ["silo_registry"]
