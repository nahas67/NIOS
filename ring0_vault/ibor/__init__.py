"""Ring 0 IBOR -- double-entry Investment Book of Record (``sec_005``/``sec_009``).

``ledger_outbox`` owns the four-vault 128-bit integer ledger, the two-phase
crash-atomic outbox, the in-silo 35/15 profit ratchet and the 60-second live
reconciler.

``tigerbeetle`` is harvested donor reference material (the Python client
bindings and the two-phase transfer samples).  It is *not* a package root, so it
is deliberately absent from ``__all__``: importing it would require the vendored
``python_client`` path to be added manually.
"""

from __future__ import annotations

__all__ = ["ledger_outbox"]
