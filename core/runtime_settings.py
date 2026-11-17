"""NIOS V2.1 -- journal-backed runtime settings and risk policy (``G10``).

A trading system's most dangerous configuration is the one that only exists in a
process's memory: a restart silently reverts ``connector_mode`` to a default that
nobody chose, or resurrects a drawdown breaker the operator had tightened.

This module makes the runtime configuration a **document**, not a constant:

* the shape and its validation live in :class:`NiosRuntimeSettings`
  (``pydantic-settings``, so environment variables can seed a fresh install);
* the durable value lives in the Ring 0 :class:`AtomicStateJournal` under
  ``runtime_settings``, so an operator change survives a crash, a restart and a
  clean-slate-aware process supervisor.

Defaults are deliberately the safest possible ones: ``OFFLINE_REPLAY`` with
``allow_network=False``, so a bare boot can neither reach a venue nor leak a
socket.  ``SANDBOX_TESTNET`` and ``LIVE_VENUE`` are opt-in through
``POST /api/v2/settings`` under a LIVE-elevated session.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CONNECTOR_MODES",
    "DEFAULT_RUNTIME_SETTINGS",
    "RUNTIME_SETTINGS_JOURNAL_KEY",
    "NiosRuntimeSettings",
    "RuntimeSettingsStore",
]

#: ``G5``/``G10``: the three explicit venue runtime modes.
CONNECTOR_MODES: tuple[str, ...] = ("OFFLINE_REPLAY", "SANDBOX_TESTNET", "LIVE_VENUE")

#: ``G10``: the journal section holding the effective runtime document.
RUNTIME_SETTINGS_JOURNAL_KEY: str = "runtime_settings"

#: ``G10``: the SQL-free default document.  Nothing here touches a socket.
DEFAULT_RUNTIME_SETTINGS: dict[str, Any] = {
    "connector_mode": "OFFLINE_REPLAY",
    "allow_network": False,
    "default_reserve_floor_pct": 15,
    "default_tax_escrow_pct": 20,
    "default_regime": "NEUTRAL",
    "max_drawdown_bps": 250.0,
    "autopilot_interval_s": 60,
    "autopilot_max_symbols": 274,
    "alert_slippage_bps": 15.0,
    "alert_drift_threshold": 0.75,
    "alert_totp_warn_hours": 6,
    "lifecycle_enabled": True,
    "reconciliation_interval_s": 60,
}


def _seal(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class NiosRuntimeSettings(BaseModel):
    """The validated runtime document (``G10``).

    ``extra="forbid"`` matters: a typo'd key must be a 422, never a silently
    ignored risk knob.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector_mode: str = Field(default="OFFLINE_REPLAY")
    allow_network: bool = Field(default=False)
    default_reserve_floor_pct: int = Field(default=15, ge=0, le=90)
    default_tax_escrow_pct: int = Field(default=20, ge=0, le=90)
    default_regime: str = Field(default="NEUTRAL")
    max_drawdown_bps: float = Field(default=250.0, ge=0.0, le=10_000.0)
    autopilot_interval_s: int = Field(default=60, ge=5, le=86_400)
    autopilot_max_symbols: int = Field(default=274, ge=1, le=5_000)
    alert_slippage_bps: float = Field(default=15.0, ge=0.0, le=10_000.0)
    alert_drift_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    alert_totp_warn_hours: int = Field(default=6, ge=0, le=168)
    lifecycle_enabled: bool = Field(default=True)
    reconciliation_interval_s: int = Field(default=60, ge=1, le=86_400)

    @field_validator("connector_mode")
    @classmethod
    def _mode_ok(cls, value: str) -> str:
        token = str(value or "").strip().upper()
        if token not in CONNECTOR_MODES:
            raise ValueError(f"connector_mode must be one of {CONNECTOR_MODES}")
        return token

    @field_validator("default_regime")
    @classmethod
    def _regime_ok(cls, value: str) -> str:
        token = str(value or "NEUTRAL").strip().upper()
        if token not in ("EXPANSION", "NEUTRAL", "CONTRACTION"):
            raise ValueError("default_regime must be EXPANSION, NEUTRAL or CONTRACTION")
        return token

    @field_validator("default_tax_escrow_pct")
    @classmethod
    def _tax_le_90_with_reserve(cls, value: int, info: Any) -> int:
        reserve = int((info.data or {}).get("default_reserve_floor_pct", 15))
        if reserve + int(value) > 90:
            raise ValueError("default_reserve_floor_pct + default_tax_escrow_pct must be <= 90")
        return int(value)

    def to_document(self) -> dict[str, Any]:
        payload = self.model_dump()
        payload["settings_sha256"] = _seal(payload)
        return payload

    def split_policy_kwargs(self) -> dict[str, Any]:
        """Arguments for ``SplitPolicy`` built from the live policy document."""
        return {
            "reserve_floor_pct": int(self.default_reserve_floor_pct),
            "tax_escrow_pct": int(self.default_tax_escrow_pct),
            "regime": str(self.default_regime),
        }

    @property
    def live(self) -> bool:
        """Only ``LIVE_VENUE`` may reach a real venue, and only with networking."""
        return self.connector_mode == "LIVE_VENUE" and bool(self.allow_network)


class RuntimeSettingsStore:
    """Read/write the journal-backed runtime document with revision history."""

    def __init__(self, journal: Any, *, max_revisions: int = 64) -> None:
        self.journal = journal
        self.max_revisions = max(4, int(max_revisions))
        self._lock = threading.RLock()
        self._settings = NiosRuntimeSettings()
        self._revisions: list[dict[str, Any]] = []
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            state = self.journal.load()
        except Exception:  # pragma: no cover - broken journal is a clean slate
            return
        document = state.get(RUNTIME_SETTINGS_JOURNAL_KEY) if isinstance(state, Mapping) else None
        if not isinstance(document, Mapping):
            return
        payload = document.get("settings") if isinstance(document.get("settings"), Mapping) else document
        try:
            self._settings = NiosRuntimeSettings(**{str(k): v for k, v in payload.items() if k in NiosRuntimeSettings.model_fields})
        except Exception:  # pragma: no cover - a corrupt document keeps the safe default
            self._settings = NiosRuntimeSettings()
            return
        revisions = document.get("revisions")
        if isinstance(revisions, (list, tuple)):
            self._revisions = [dict(row) for row in revisions if isinstance(row, Mapping)]

    def _persist(self) -> None:
        document = {
            "settings": self._settings.to_document(),
            "revisions": self._revisions[-self.max_revisions :],
            "updated_at_utc_ns": time.time_ns(),
        }
        try:
            self.journal.update(**{RUNTIME_SETTINGS_JOURNAL_KEY: document})
        except Exception:  # pragma: no cover - best effort under disk failure
            pass

    # -- reading ----------------------------------------------------------
    @property
    def settings(self) -> NiosRuntimeSettings:
        with self._lock:
            return self._settings

    @property
    def connector_mode(self) -> str:
        return self.settings.connector_mode

    def document(self) -> dict[str, Any]:
        with self._lock:
            payload = self._settings.to_document()
            payload["journal_key"] = RUNTIME_SETTINGS_JOURNAL_KEY
            payload["journal_backed"] = True
            payload["revision_count"] = len(self._revisions)
            payload["modes"] = list(CONNECTOR_MODES)
            payload["live_armed"] = bool(self._settings.live)
            payload["defaults"] = dict(DEFAULT_RUNTIME_SETTINGS)
            return payload

    def status(self) -> dict[str, Any]:
        payload = self.document()
        payload["page"] = "settings.status"
        payload["as_of_utc_ns"] = time.time_ns()
        payload["law"] = "sec_016 - a risk knob is durable configuration, not a constant"
        return payload

    def revisions(self, *, limit: int = 20) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(row) for row in self._revisions[-max(1, int(limit)) :])

    # -- writing ----------------------------------------------------------
    def update(
        self,
        patch: Mapping[str, Any],
        *,
        operator: str = "ring3.operator",
        now_ns: int | None = None,
    ) -> dict[str, Any]:
        """Validate and persist ``patch``; raises ``ValueError`` on a bad document."""
        if not isinstance(patch, Mapping):
            raise TypeError("patch must be a mapping")
        unknown = [key for key in patch if key not in NiosRuntimeSettings.model_fields]
        if unknown:
            raise ValueError(f"UNKNOWN_SETTING:{sorted(unknown)[0]}")
        with self._lock:
            merged = self._settings.model_dump()
            merged.update({str(key): value for key, value in patch.items()})
            try:
                candidate = NiosRuntimeSettings(**merged)
            except Exception as exc:  # pydantic ValidationError
                raise ValueError(f"INVALID_SETTINGS:{exc}") from None
            if candidate.connector_mode == "LIVE_VENUE" and not candidate.allow_network:
                # A live mode with networking disabled cannot trade, and silently
                # accepting it would leave the cockpit claiming LIVE while offline.
                raise ValueError("LIVE_MODE_REQUIRES_ALLOW_NETWORK")
            changed = {
                key: {"from": self._settings.model_dump().get(key), "to": candidate.model_dump().get(key)}
                for key in candidate.model_dump()
                if candidate.model_dump().get(key) != self._settings.model_dump().get(key)
            }
            stamp = int(now_ns if now_ns is not None else time.time_ns())
            self._settings = candidate
            if changed:
                self._revisions.append(
                    {
                        "revision": len(self._revisions) + 1,
                        "operator": str(operator or "ring3.operator"),
                        "changed": changed,
                        "at_utc_ns": stamp,
                        "settings_sha256": candidate.to_document()["settings_sha256"],
                    }
                )
                self._persist()
            payload = self.document()
            payload.update(
                {
                    "changed": changed,
                    "changed_count": len(changed),
                    "revision": len(self._revisions),
                    "operator": str(operator or "ring3.operator"),
                    "persisted": bool(changed),
                    "as_of_utc_ns": stamp,
                }
            )
            return payload

    def reset(self, *, operator: str = "ring3.operator", now_ns: int | None = None) -> dict[str, Any]:
        """Restore the offline defaults and record the reset as a revision."""
        payload = self.update(
            dict(DEFAULT_RUNTIME_SETTINGS), operator=operator, now_ns=now_ns
        )
        payload["reset"] = True
        return payload
