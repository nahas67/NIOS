"""Make the repository root importable for ``import core.*`` in pytest.

Also isolates Tier-0 shared memory per test process (Operator Note #1).

``pytest`` may run at the exact same time as a live NIOS server on this
workstation.  Both would otherwise create the same ``nios-global-kill-latch`` /
``nios-active-leases`` / ``nios-ofi-cancel-ring`` / ``nios-shadow-book``
segments, and whichever process created one second would race the other into a
``FileExistsError`` (or reset a live lease table).  A process-unique
``NIOS_SHM_PREFIX`` gives every test process its own namespace, so the two can
never collide.  An explicitly supplied prefix (CI shard, xdist worker) wins.

The same reasoning applies to the on-disk state journal: ``NIOS_DATA_DIR`` and
``NIOS_JOURNAL_PATH`` point into a process-unique scratch directory, so a test
can never write ``<repo>/data/``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("NIOS_SHM_PREFIX", f"nios-test-{os.getpid()}")

# Every suite must see a first-run install. The V1.1 LIVE mutation guard only
# arms when an operator credential exists on disk, so a test that registers an
# operator would otherwise promote the *real* vault to "initialised" and make
# every later run answer 401 AUTH_REQUIRED on the credential surfaces. A
# process-unique scratch directory keeps the guard inert and never touches the
# operator credential of a live install (RUNBOOK SOP 1B.5 does the same for the
# manual burn-in drill).
os.environ.setdefault(
    "NIOS_OPERATOR_AUTH_DIR",
    str(Path(tempfile.gettempdir()) / f"nios-operator-test-{os.getpid()}"),
)

# Active release: the workstation persists its logbook, backtests and agent
# tasks in a crash-safe journal (``ring0_vault.ibor.state_journal``), which
# resolves ``NIOS_DATA_DIR`` before the checked-out ``<repo>/data`` directory.
# A suite that exercised a writer would otherwise drop
# ``nios_workstation_journal.json`` into the developer's real ``data/`` folder
# and pollute the next manual burn-in. A process-unique scratch directory keeps
# every write inside the temp tree. An explicitly supplied location still wins.
os.environ.setdefault(
    "NIOS_DATA_DIR",
    str(Path(tempfile.gettempdir()) / f"nios-data-test-{os.getpid()}"),
)
Path(os.environ["NIOS_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
os.environ.setdefault(
    "NIOS_JOURNAL_PATH",
    str(Path(os.environ["NIOS_DATA_DIR"]) / "nios_workstation_journal.json"),
)

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

    @pytest.fixture(scope="module", autouse=True)
    def _nios_operator_vault_reset():
        """Every suite starts from a first-run (uninitialised) operator vault.

        V1.8 authenticates every ``/api/v2/control/*`` mutation as soon as
        ``operator_auth.is_initialized()`` is true.  Without a module boundary
        reset, the operator that ``test_v1_1`` deliberately registers would arm
        the guard for every later suite in the same pytest process and turn
        their unauthenticated paper calls into ``401 AUTH_REQUIRED``.  A suite
        that wants an operator registers one itself (and passes its headers).
        """
        root = Path(os.environ["NIOS_OPERATOR_AUTH_DIR"])
        try:
            (root / "operator_auth.json").unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - best effort
            pass
        try:
            from ring0_vault.silos.key_vault.operator_auth import (
                reset_operator_auth_singleton,
            )

            reset_operator_auth_singleton()
        except Exception:  # pragma: no cover - import failure reported elsewhere
            pass
        yield
