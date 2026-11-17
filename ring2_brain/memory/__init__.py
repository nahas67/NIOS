"""Ring 2 memory surfaces.

``entity_graph`` holds the entity-linked post-mortem memory harvested from
``garrytan/gbrain``: an append-only outcome timeline plus deterministically
compiled per-entity truths, retrieved with hybrid lexical + feature matching
and sealed into a ``GroundedFactPacket`` before any figure reaches a thesis.

Ring containment: Ring 2 imports ``core.schemas`` only. No broker credential,
no exchange socket, no order signing reaches this package.
"""

from __future__ import annotations

from ring2_brain.memory.entity_graph import (
    ADVERSE_OUTCOME_KINDS,
    ENTITY_KINDS,
    OUTCOME_KINDS,
    CompiledEntityTruth,
    EntityMemoryGraph,
    PostMortemEvent,
    PostMortemPenalty,
)

__all__ = [
    "ADVERSE_OUTCOME_KINDS",
    "ENTITY_KINDS",
    "OUTCOME_KINDS",
    "CompiledEntityTruth",
    "EntityMemoryGraph",
    "PostMortemEvent",
    "PostMortemPenalty",
]
