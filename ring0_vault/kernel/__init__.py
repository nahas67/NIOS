"""Ring 0 kernel -- sovereign pre-trade firewall, ADR-009 and the Smart OMS.

``firewalls_and_oms`` is the only module in the repository that holds order
signing material (``sec_011``).  ``nautilus`` is harvested donor source
(``crates/execution``, ``crates/risk``, ``crates/trading``) kept for reference
and for the property-test provenance; it is not a Python package and is
therefore not exported here.
"""

from __future__ import annotations

__all__ = ["firewalls_and_oms"]
