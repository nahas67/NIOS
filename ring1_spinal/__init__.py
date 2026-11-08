"""NIOS Core v3.0 -- Ring 1 Spinal Reflex (Tier 0).

Deterministic, zero-GC market microstructure execution (``sec_004``,
``sec_007``, ``sec_010``, ``sec_013``).

Modules
-------
``circuit_breaker``
    The Tier-1 ``0.1 ms`` microstructure breaker: 3.5x volatility spike, two
    consecutive adverse maker fills inside 500 ms, spread blowout.  A trip
    writes ``LeaseMode.DISARMED`` into shared memory and nothing else.
``shadow_twin``
    The 30-trade live-L2 shadow execution twin that resolves the ``N = 0``
    conformal cold-start deadlock (``sec_002`` flaw 4, ``sec_018`` Law 4).
``spinal_loop``
    The Tier-0 four-step fast path: lease read -> breaker -> anti-spoof
    executed OFI -> shadow warmup or unsigned Ring 0 proposal.
``silo_worker``
    Per-silo cost floors (including India's 5.5 bps STT floor), CPU-affinity
    pinning and the honest NautilusTrader adapter status bridge.

Air-gap
-------
Nothing in this package imports an LLM client, ``ring2_brain`` or
``ring0_vault.silos.key_vault``.  Ring 1 holds no credentials and cannot sign
(``sec_004``, ``sec_018`` Law 2).
"""

from __future__ import annotations

__all__ = [
    "circuit_breaker",
    "shadow_twin",
    "spinal_loop",
    "silo_worker",
]
