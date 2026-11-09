"""Make the repository root importable for ``import core.*`` in pytest.

Also isolates Tier-0 shared memory per test process (Operator Note #1).

``pytest`` may run at the exact same time as a live NIOS server on this
workstation.  Both would otherwise create the same ``nios-global-kill-latch`` /
``nios-active-leases`` / ``nios-ofi-cancel-ring`` / ``nios-shadow-book``
segments, and whichever process created one second would race the other into a
``FileExistsError`` (or reset a live lease table).  A process-unique
``NIOS_SHM_PREFIX`` gives every test process its own namespace, so the two can
never collide.  An explicitly supplied prefix (CI shard, xdist worker) wins.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("NIOS_SHM_PREFIX", f"nios-test-{os.getpid()}")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


try:  # pragma: no cover - pytest is always present when this module loads
    import pytest
except Exception:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


if pytest is not None:

    @pytest.fixture(scope="session", autouse=True)
    def _nios_shm_namespace_teardown():
        """Drop only this process's test segments when the session ends."""
        yield
        try:
            from core.shm_ipc import unlink_all
        except Exception:  # pragma: no cover - import failure is reported elsewhere
            return
        try:
            unlink_all()
        except Exception:  # pragma: no cover - teardown is best effort
            pass
