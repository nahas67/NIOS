"""NIOS Core v3.0 -- Ring 0 Sovereign Vault (STEP 6).

The only ring permitted to hold broker credentials, sign orders, or mutate the
ledger (``sec_004`` / ``sec_011``).

Modules
-------
``conformal_copula_gate``
    The 98% split-conformal precision gate plus the cross-silo tail-copula
    shield.  Evaluates ``Net_LB = E[r] - (Spread + 2*Fee + Tax +
    Q_0.98(Residuals))`` in under ``0.42 ms`` (``sec_003``, ``sec_010``,
    ``sec_013``).
``ibor.ledger_outbox``
    Four-vault, 128-bit integer double-entry IBOR, the two-phase crash-atomic
    outbox, the in-silo 35/15 profit ratchet and the 60-second two-way
    reconciler (``sec_005``, ``sec_009``, ``sec_012``, ``sec_015``).
``kernel.firewalls_and_oms``
    The five-step sovereign pre-trade firewall, the ADR-009 reduce-only fast
    path, and the Smart OMS maker-peg / TWAP iceberg execution layer
    (``sec_004``, ``sec_010``, ``sec_011``, ``sec_012``, ``sec_016``).
``silos.silo_registry``
    The four isolated market silos with their statutory tax floors, ratchet
    assets, paper/live toggles and honest donor adapter probes (``sec_005``,
    ``sec_015``).

Air-gap
-------
Nothing in this package imports ``ring2_brain`` or any LLM client.  Ring 2
never receives a credentialed connection; Ring 0 never reasons about the
market (``sec_004``, ``sec_018`` Law 2).
"""

from __future__ import annotations

__all__ = [
    "conformal_copula_gate",
    "ibor",
    "kernel",
    "silos",
]
