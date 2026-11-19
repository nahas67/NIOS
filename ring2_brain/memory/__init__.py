"""Ring 2 memory surfaces.

``entity_graph`` holds the entity-linked post-mortem memory harvested from
``garrytan/gbrain``: an append-only outcome timeline plus deterministically
compiled per-entity truths, retrieved with hybrid lexical + feature matching
and sealed into a ``GroundedFactPacket`` before any figure reaches a thesis.

``working_memory`` is the V2.3 Tier-1 store: bounded, crash-safe retention of the
``UnknownOrderQueue`` retry ladder, closed/live bar snapshots and agent Q&A
threads.  It depends only on a generic bounded-namespace protocol, so this
package never hard-depends on Ring 0: the durable backend is resolved lazily and
falls back to an in-process implementation.

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
from ring2_brain.memory.entity_graph import (
    COMPACT_THRESHOLD,
    EMBED_DIMS,
    FEATURE_WEIGHT,
    KEEP_RECENT,
    LEXICAL_WEIGHT,
    MAX_COLD_ARCHIVE_EVENTS,
    SEMANTIC_WEIGHT,
    MemoryCheckpoint,
    cosine_similarity,
    hashed_embedding,
)
from ring2_brain.memory.episodic_memory import (
    ACTION_KINDS,
    ADVERSE_ACTION_KINDS,
    EPISODE_SCHEMA_VERSION,
    MAX_EPISODES,
    EpisodicEpisode,
    EpisodicMemoryStore,
    default_episodic_store,
    digitless,
)
from ring2_brain.memory.working_memory import (
    MAX_BARS_PER_SERIES,
    MAX_CHAT_TURNS,
    MAX_INSTRUMENTS,
    MAX_UNKNOWN_ORDERS,
    NS_BARS,
    NS_BAR_SERIES,
    NS_CHAT,
    NS_UNKNOWN_ORDERS,
    RETRY_BACKOFF_S,
    BarPersistenceSink,
    InMemoryWorkingMemoryBackend,
    WorkingMemoryBackend,
    WorkingMemoryStore,
    default_working_memory_store,
    register_working_memory_backend,
    reset_working_memory_singleton,
    sha256_turn_digest,
)

__all__ = [
    "ADVERSE_OUTCOME_KINDS",
    "ENTITY_KINDS",
    "OUTCOME_KINDS",
    "CompiledEntityTruth",
    "EntityMemoryGraph",
    "PostMortemEvent",
    "PostMortemPenalty",
    # V2.3 PART 2.2 -- tier-2 compaction / verified checkpoints
    "COMPACT_THRESHOLD",
    "EMBED_DIMS",
    "FEATURE_WEIGHT",
    "KEEP_RECENT",
    "LEXICAL_WEIGHT",
    "MAX_COLD_ARCHIVE_EVENTS",
    "SEMANTIC_WEIGHT",
    "MemoryCheckpoint",
    "cosine_similarity",
    "hashed_embedding",
    # V2.3 PART 2.3 -- tier-3 episodic operator memory
    "ACTION_KINDS",
    "ADVERSE_ACTION_KINDS",
    "EPISODE_SCHEMA_VERSION",
    "MAX_EPISODES",
    "EpisodicEpisode",
    "EpisodicMemoryStore",
    "default_episodic_store",
    "digitless",
    # V2.3 PART 2.1 -- Tier 1 working memory
    "MAX_BARS_PER_SERIES",
    "MAX_CHAT_TURNS",
    "MAX_INSTRUMENTS",
    "MAX_UNKNOWN_ORDERS",
    "NS_BARS",
    "NS_BAR_SERIES",
    "NS_CHAT",
    "NS_UNKNOWN_ORDERS",
    "RETRY_BACKOFF_S",
    "BarPersistenceSink",
    "InMemoryWorkingMemoryBackend",
    "WorkingMemoryBackend",
    "WorkingMemoryStore",
    "default_working_memory_store",
    "register_working_memory_backend",
    "reset_working_memory_singleton",
    "sha256_turn_digest",
]
