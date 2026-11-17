"""NIOS V2.1 -- closed-loop control-plane surfaces (``G1``-``G12``).

This module adds the V2.1 runtime to the existing :class:`ControlPlane` without
rewriting it.  Two pieces:

``V21ControlPlaneMixin``
    Lazy component wiring plus the methods the new endpoints call.  Every
    component is built on first use and every component is journal-backed, so a
    bare ``create_app()`` boot pays nothing until an operator asks for something.

``register_v2_1_routes(...)``
    The new REST surface, registered from ``create_app`` where the session and
    LIVE-tier guards already exist.

Design notes worth keeping:

* **G11 CSRF.**  Session auth already accepted ``X-NIOS-Session`` *and* the
  ``HttpOnly; SameSite=Strict`` ``nios_session`` cookie.  A cookie-authenticated
  mutation is now additionally required to echo ``X-NIOS-CSRF``, whose value is
  ``sha256("nios-csrf-v1:" + session_token)[:32]``.  Because the token itself is
  ``HttpOnly`` and therefore unreadable to script, a cross-site attacker cannot
  compute the value: this is a signed double-submit, and it needs no extra cookie.
  Pre-session bootstrap paths (setup/login/logout/reset) are exempt, and the
  documented read-only MCP bridge call is exempt because it mutates nothing.
* **G12 reconciliation.**  The status endpoint reads ``ColdStartReconciler.history()``
  plus ``SiloGuard``; ``run`` performs a real reconciliation against the positions
  the connector runtime currently reports; ``clear-freeze`` demands a LIVE-elevated
  session *and* a non-empty operator note, and refuses to unfreeze a silo whose
  guard reason is not a cold-start reason.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.lifecycle_worker import NiosLifecycleWorker
from core.runtime_settings import RuntimeSettingsStore
from ring0_vault.ibor.alert_center import AlertCenter
from ring0_vault.ibor.ledger_outbox import (
    DynamicFundSplitEngine,
    TrancheGuard,
)
from ring0_vault.silos.virtual_margin_quota import VirtualMarginQuotaEngine
from ring0_vault.ibor.profit_split_saga import ProfitSplitSagaCoordinator
from ring0_vault.silos.connector_runtime import ConnectorRuntime
from ring2_brain.memory.post_mortem_recorder import PostMortemRecorder
from ring2_brain.quant.autopilot_orchestrator import MultiTrancheAutopilot
from ring2_brain.quant.bar_aggregator import TIMEFRAMES, LiveBarAggregator
from ring2_brain.quant.performance_analytics import PerformanceAnalytics

__all__ = [
    "CSRF_EXEMPT_PATHS",
    "CSRF_EXEMPT_PREFIXES",
    "CSRF_HEADER",
    "V21ControlPlaneMixin",
    "csrf_token_for",
    "register_v2_1_routes",
]

#: ``G11``: the double-submit header a cookie-authenticated mutation must echo.
CSRF_HEADER: str = "X-NIOS-CSRF"

#: ``G11``: safe methods never need a CSRF token.
CSRF_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

#: ``G11``: exact paths exempt because they bootstrap or destroy the session.
CSRF_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v2/auth/setup",
        "/api/v2/auth/login",
        "/api/v2/auth/login/paper",
        "/api/v2/auth/login/live",
        "/api/v2/auth/elevate-live",
        "/api/v2/auth/logout",
        "/api/v2/auth/reset",
        # Documented read-only MCP bridge: it reads a tool's output and mutates
        # nothing in Ring 0, so there is no state for CSRF to protect.
        "/api/v2/mcp/call",
    }
)

#: Prefix exemptions (currently none; kept explicit so the rule is auditable).
CSRF_EXEMPT_PREFIXES: tuple[str, ...] = ()


def csrf_token_for(session_token: str) -> str:
    """The deterministic double-submit value for one session token."""
    return hashlib.sha256(f"nios-csrf-v1:{session_token}".encode("utf-8")).hexdigest()[:32]


def _iso_utc(ns: int | None) -> str | None:
    if not ns:
        return None
    try:
        return datetime.fromtimestamp(int(ns) / 1_000_000_000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return None


class AlertAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: str = Field(default="ring3.operator", max_length=128)
    note: str = Field(default="", max_length=512)


class AlertRaiseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=64)
    severity: str = Field(default="WARNING", max_length=16)
    silo: str = Field(default="", max_length=32)
    message: str = Field(default="", max_length=512)
    detail: dict[str, Any] = Field(default_factory=dict)


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_mode: str | None = None
    allow_network: bool | None = None
    default_reserve_floor_pct: int | None = None
    default_tax_escrow_pct: int | None = None
    default_regime: str | None = None
    max_drawdown_bps: float | None = None
    autopilot_interval_s: int | None = None
    autopilot_max_symbols: int | None = None
    alert_slippage_bps: float | None = None
    alert_drift_threshold: float | None = None
    alert_totp_warn_hours: int | None = None
    lifecycle_enabled: bool | None = None
    reconciliation_interval_s: int | None = None
    operator: str = Field(default="ring3.operator", max_length=128)

    def patch(self) -> dict[str, Any]:
        body = self.model_dump()
        body.pop("operator", None)
        return {key: value for key, value in body.items() if value is not None}


class LifecycleTickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    now_utc_ns: int | None = Field(default=None, ge=0)


class AutopilotCycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str = Field(default="", max_length=64)
    tranches: list[dict[str, Any]] = Field(default_factory=list)
    now_ns: int | None = Field(default=None, ge=0)


class TradeCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    silo: str = Field(..., min_length=1, max_length=32)
    symbol: str = Field(default="", max_length=64)
    gross_pnl_micros: int
    notional_micros: int = Field(default=0, ge=0)
    side: str = Field(default="SELL", max_length=8)
    mission_id: str = Field(default="", max_length=64)
    tranche_id: str = Field(default="", max_length=64)
    regime: str = Field(default="NEUTRAL", max_length=16)
    slippage_bps: float = Field(default=0.0, ge=-10_000.0, le=10_000.0)


class CompensateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="OPERATOR", max_length=128)


class TickIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    silo: str = Field(..., min_length=1, max_length=32)
    symbol: str = Field(..., min_length=1, max_length=64)
    price: float = Field(..., gt=0.0)
    size: float = Field(default=0.0, ge=0.0)
    bid: float | None = Field(default=None, gt=0.0)
    ask: float | None = Field(default=None, gt=0.0)
    now_ns: int | None = Field(default=None, ge=0)


class ReconciliationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False


class ClearFreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    silo: str = Field(..., min_length=1, max_length=32)
    note: str = Field(..., min_length=3, max_length=512)
    operator: str = Field(default="ring3.operator", max_length=128)


class V21ControlPlaneMixin:
    """V2.1 closed-loop surfaces attached to :class:`ControlPlane`."""

    # ------------------------------------------------------------------
    # Lazy components
    # ------------------------------------------------------------------
    @property
    def runtime_settings(self) -> RuntimeSettingsStore:
        store = getattr(self, "_runtime_settings_store", None)
        if store is None:
            store = RuntimeSettingsStore(self.state_journal)
            self._runtime_settings_store = store
        return store

    @property
    def alert_center(self) -> AlertCenter:
        center = getattr(self, "_alert_center", None)
        if center is None:
            center = AlertCenter(self.state_journal)
            self._alert_center = center
        return center

    @property
    def profit_split_saga(self) -> ProfitSplitSagaCoordinator:
        saga = getattr(self, "_profit_split_saga", None)
        if saga is None:
            sweeper = None
            try:
                from ring0_vault.silos.crypto_wallet_sweep import CryptoWalletSweeper

                sweeper = CryptoWalletSweeper(
                    self.ledger,
                    outbox=getattr(self, "outbox", None),
                    firewall=self.firewall,
                    signer=getattr(self.oms, "signer", None),
                    kill_latch=self.kill_latch,
                    allow_network=False,
                )
            except Exception:  # pragma: no cover - sweeper is optional in isolation
                sweeper = None
            quota_engine = None
            try:
                quota_engine = VirtualMarginQuotaEngine(
                    self.ledger, kill_latch=self.kill_latch
                )
            except Exception:  # pragma: no cover - defensive
                quota_engine = None
            saga = ProfitSplitSagaCoordinator(
                ledger=self.ledger,
                journal=self.state_journal,
                split_engine=DynamicFundSplitEngine(self.ledger),
                pacing=getattr(self.missions, "pacing", None),
                sweeper=sweeper,
                quota_engine=quota_engine,
            )
            self._profit_split_saga = saga
        return saga

    @property
    def connectors(self) -> ConnectorRuntime:
        runtime = getattr(self, "_connector_runtime", None)
        if runtime is None:
            mode = "OFFLINE_REPLAY"
            allow_network = False
            try:
                settings = self.runtime_settings.settings
                mode = settings.connector_mode
                allow_network = bool(settings.allow_network)
            except Exception:  # pragma: no cover - settings store is always present
                pass
            sweeper = None
            try:
                from ring0_vault.silos.crypto_wallet_sweep import CryptoWalletSweeper

                sweeper = CryptoWalletSweeper(
                    self.ledger,
                    outbox=getattr(self, "outbox", None),
                    firewall=self.firewall,
                    signer=getattr(self.oms, "signer", None),
                    kill_latch=self.kill_latch,
                    allow_network=False,
                )
            except Exception:  # pragma: no cover - optional
                sweeper = None
            try:
                runtime = ConnectorRuntime(
                    connector_mode=mode,
                    allow_network=allow_network,
                    sweeper=sweeper,
                )
            except Exception:  # pragma: no cover - a live flag without transport
                runtime = ConnectorRuntime(connector_mode="OFFLINE_REPLAY")
            self._connector_runtime = runtime
        return runtime

    @property
    def bar_aggregator(self) -> LiveBarAggregator:
        aggregator = getattr(self, "_bar_aggregator", None)
        if aggregator is None:
            aggregator = LiveBarAggregator(features_sink=getattr(self, "feature_store", None))
            self._bar_aggregator = aggregator
        return aggregator

    @property
    def performance_analytics(self) -> PerformanceAnalytics:
        analytics = getattr(self, "_performance_analytics", None)
        if analytics is None:
            analytics = PerformanceAnalytics()
            self._performance_analytics = analytics
        return analytics

    @property
    def post_mortem(self) -> PostMortemRecorder:
        recorder = getattr(self, "_post_mortem_recorder", None)
        if recorder is None:
            recorder = PostMortemRecorder(writer=self.record_post_mortem)
            self._post_mortem_recorder = recorder
        return recorder

    @property
    def multi_tranche_autopilot(self) -> MultiTrancheAutopilot:
        autopilot = getattr(self, "_multi_tranche_autopilot", None)
        if autopilot is None:
            gate = None
            try:
                from ring0_vault.kernel.firewalls_and_oms import TrancheIndependenceGate

                gate = TrancheIndependenceGate()
            except Exception:  # pragma: no cover - the Ring 2 mirror is always available
                gate = None
            autopilot = MultiTrancheAutopilot(
                registry=self.registry,
                gate=gate,
                feasibility=getattr(self, "_mission_feasibility", None),
                memory=getattr(self, "entity_memory", None),
                calendar=self._exchange_calendar_v21(),
                max_symbols=int(self.runtime_settings.settings.autopilot_max_symbols),
            )
            self._multi_tranche_autopilot = autopilot
        return autopilot

    def _exchange_calendar_v21(self) -> Any:
        try:
            from core.exchange_calendar import default_exchange_calendar

            return default_exchange_calendar()
        except Exception:  # pragma: no cover - a calendar is advisory, never fatal
            try:
                from core.exchange_calendar import ExchangeCalendar

                return ExchangeCalendar()
            except Exception:
                return None

    def _get_cold_start_reconciler(self) -> Any:
        reconciler = getattr(self, "_cold_start_reconciler", None)
        if reconciler is None:
            try:
                from ring0_vault.ibor.cold_start_reconciler import ColdStartReconciler

                reconciler = ColdStartReconciler(
                    self.ledger, guard=getattr(self, "guard", None)
                )
            except Exception:  # pragma: no cover - optional in isolation
                reconciler = None
            self._cold_start_reconciler = reconciler
        return reconciler

    @property
    def lifecycle_worker(self) -> NiosLifecycleWorker:
        worker = getattr(self, "_lifecycle_worker", None)
        if worker is None:
            try:
                from ring0_vault.silos.key_vault.totp_renewal import PreMarketTotpRenewal

                totp: Any = PreMarketTotpRenewal()
            except Exception:  # pragma: no cover - optional in isolation
                totp = None
            try:
                monitor = self.concept_drift_monitor()
            except Exception:  # pragma: no cover - optional
                monitor = None
            worker = NiosLifecycleWorker(
                alert_center=self.alert_center,
                quota_engine=VirtualMarginQuotaEngine(self.ledger, kill_latch=self.kill_latch),
                drift_monitor=monitor,
                totp_renewal=totp,
                connector=self.connectors,
                reconciliation=None,
                enabled=bool(self.runtime_settings.settings.lifecycle_enabled),
                totp_warn_hours=int(self.runtime_settings.settings.alert_totp_warn_hours),
            )
            self._lifecycle_worker = worker
        return worker

    # ------------------------------------------------------------------
    # G10: settings
    # ------------------------------------------------------------------
    def v21_settings_document(self) -> dict[str, Any]:
        envelope = self._envelope(self.runtime_settings.status())
        envelope["live_critical_keys"] = list(LIVE_CRITICAL_SETTINGS)
        return envelope

    def v21_update_settings(self, request: SettingsUpdateRequest) -> dict[str, Any]:
        patch = request.patch()
        if not patch:
            raise HTTPException(status_code=422, detail="SETTINGS_PATCH_EMPTY")
        try:
            report = self.runtime_settings.update(patch, operator=request.operator)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        # A connector-mode change has to reach the already-built runtime, and a
        # lifecycle toggle has to reach the already-built worker.
        if "connector_mode" in patch or "allow_network" in patch:
            self._rebuild_connector_runtime()
        if "lifecycle_enabled" in patch:
            worker = getattr(self, "_lifecycle_worker", None)
            if worker is not None:
                worker.enabled = bool(self.runtime_settings.settings.lifecycle_enabled)
        return self._envelope({"page": "settings.update", **report})

    @staticmethod
    def live_critical_patch(patch: Mapping[str, Any]) -> bool:
        return any(key in LIVE_CRITICAL_SETTINGS for key in patch)

    def _rebuild_connector_runtime(self) -> None:
        self._connector_runtime = None
        try:
            self.alert_center.raise_alert(
                code="CONNECTOR_MODE_CHANGED",
                severity="WARNING",
                source="runtime_settings",
                message=f"connector_mode is now {self.runtime_settings.connector_mode}",
                detail={
                    "connector_mode": self.runtime_settings.connector_mode,
                    "allow_network": bool(self.runtime_settings.settings.allow_network),
                },
            )
        except Exception:  # pragma: no cover - an alert never blocks a setting change
            pass

    # ------------------------------------------------------------------
    # G9: alerts
    # ------------------------------------------------------------------
    def v21_alerts(
        self,
        *,
        severity: str | None = None,
        unacked_only: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        try:
            payload = self.alert_center.list_alerts(
                severity=severity, unacked_only=unacked_only, limit=limit
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return self._envelope({"page": "alerts", **payload})

    def v21_alert_ack(self, alert_id: str, request: AlertAckRequest) -> dict[str, Any]:
        try:
            row = self.alert_center.acknowledge(
                alert_id, operator=request.operator, note=request.note
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"UNKNOWN_ALERT:{alert_id}") from None
        return self._envelope({"page": "alerts.ack", "alert": row, "acked": True})

    def v21_alert_raise(self, request: AlertRaiseRequest) -> dict[str, Any]:
        try:
            row = self.alert_center.raise_alert(
                code=request.code,
                severity=request.severity,
                source="ring3.operator",
                silo=request.silo,
                message=request.message,
                detail=request.detail,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return self._envelope({"page": "alerts.raise", "alert": row, "raised": True})

    # ------------------------------------------------------------------
    # G8: performance analytics
    # ------------------------------------------------------------------
    def performance_rows(self) -> list[dict[str, Any]]:
        """Build integer-micro performance rows from the durable logbook.

        The logbook is the only settled trade record the cockpit owns, so it is
        the honest source: NAV is walked backwards from the live silo NAV by the
        settled P&L of each entry, which makes the curve reconcile to the ledger
        by construction instead of by hope.
        """
        try:
            page = self.logbook_page()
        except Exception:  # pragma: no cover - defensive
            page = {}
        entries = page.get("entries") if isinstance(page, Mapping) else None
        if not isinstance(entries, Sequence):
            return []
        current_nav = 0
        try:
            for silo_id in self.registry.silo_ids():
                current_nav += int(self.ledger.silo_nav_micros(silo_id))
        except Exception:  # pragma: no cover - defensive
            current_nav = 0
        parsed: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            net = entry.get("net_pnl")
            try:
                pnl_micros = int(round(float(net) * 1_000_000))
            except (TypeError, ValueError):
                continue
            try:
                ts = int(entry.get("recorded_at_ns", 0))
            except (TypeError, ValueError):
                ts = 0
            parsed.append(
                {
                    "ts_utc_ns": ts,
                    "pnl_micros": pnl_micros,
                    "silo": str(entry.get("silo", "")),
                    "mission_id": str(entry.get("mission_id", "") or ""),
                    "tranche_id": str(entry.get("tranche_id", "") or ""),
                    "trade_id": str(entry.get("entry_id", "")),
                }
            )
        parsed.sort(key=lambda row: (row["ts_utc_ns"], row["trade_id"]))
        total = sum(row["pnl_micros"] for row in parsed)
        running = current_nav - total
        rows: list[dict[str, Any]] = []
        for row in parsed:
            running += int(row["pnl_micros"])
            rows.append({**row, "nav_micros": int(running)})
        return rows

    def v21_analytics_performance(
        self,
        *,
        silo: str | None = None,
        mission_id: str | None = None,
        bucket: str = "1D",
    ) -> dict[str, Any]:
        try:
            payload = self.performance_analytics.report(
                self.performance_rows(), silo=silo, mission_id=mission_id, bucket=bucket
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return self._envelope(payload)

    # ------------------------------------------------------------------
    # G11: unified session telemetry
    # ------------------------------------------------------------------
    def v21_auth_session(self, session_token: str | None) -> dict[str, Any]:
        status = self.auth_status(session_token)
        expires_ns = int(status.get("expires_ns", 0) or 0)
        now = time.time_ns()
        ttl_remaining = max(0, (expires_ns - now) // 1_000_000_000) if expires_ns else None
        token = str(session_token or "")
        return {
            "page": "auth.session",
            "authenticated": bool(status.get("authenticated", False)),
            "initialized": bool(status.get("initialized", False)),
            "mode": str(status.get("tier", "NONE")),
            "username": status.get("username"),
            "workspace": str(status.get("workspace", "NONE")),
            "ttl_remaining_s": ttl_remaining,
            "expires_at_utc": _iso_utc(expires_ns),
            "expires_at_utc_ns": expires_ns,
            "issued_at_utc_ns": int(status.get("issued_ns", 0) or 0),
            "transport": {
                "header": "X-NIOS-Session",
                "cookie": "nios_session",
                "cookie_flags": "HttpOnly; SameSite=Strict",
                "csrf_header": CSRF_HEADER,
                "csrf_required_for": "cookie-authenticated mutations",
            },
            "csrf_token": csrf_token_for(token) if token else None,
            "clean_slate_active": bool(status.get("clean_slate_active", False)),
            "as_of_utc_ns": now,
        }

    # ------------------------------------------------------------------
    # G12: reconciliation control surface
    # ------------------------------------------------------------------
    def v21_reconciliation_status(self) -> dict[str, Any]:
        reconciler = self._get_cold_start_reconciler()
        history: tuple[Any, ...] = ()
        if reconciler is not None and hasattr(reconciler, "history"):
            try:
                history = tuple(reconciler.history())
            except Exception:  # pragma: no cover - defensive
                history = ()
        last = history[-1].to_dict() if history and hasattr(history[-1], "to_dict") else None
        guard = getattr(self, "guard", None)
        frozen = ()
        reasons: dict[str, str] = {}
        if guard is not None:
            try:
                frozen = tuple(guard.frozen_silos)
                reasons = {
                    silo: str(guard.reason(silo)) for silo in frozen
                }
            except Exception:  # pragma: no cover - defensive
                frozen = ()
        return self._envelope(
            {
                "page": "reconciliation.status",
                "reconciler_attached": reconciler is not None,
                "report_count": len(history),
                "last_report": last,
                "frozen_silos": list(frozen),
                "freeze_reasons": reasons,
                "clear_freeze_requires": "LIVE-elevated session + operator note",
                "as_of_utc_ns": time.time_ns(),
                "law": "sec_012 - drift above 0.01% freezes the silo, not the account",
            }
        )

    def v21_reconciliation_run(self, *, dry_run: bool = False) -> dict[str, Any]:
        snapshots: list[Any] = []
        try:
            from ring0_vault.ibor.cold_start_reconciler import BrokerSnapshot

            for silo_id in self.registry.silo_ids():
                positions = self.connectors.fetch_positions(silo=silo_id)
                for row in positions.get("positions", ()):
                    symbol = str(row.get("symbol", ""))
                    if not symbol or int(row.get("qty_micros", 0)) == 0:
                        continue
                    snapshots.append(
                        BrokerSnapshot(
                            silo=str(silo_id),
                            symbol=symbol,
                            qty_micros=int(row.get("qty_micros", 0)),
                        )
                    )
        except Exception as exc:  # pragma: no cover - a broken probe is reported
            return self._envelope(
                {
                    "page": "reconciliation.run",
                    "ran": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "dry_run": bool(dry_run),
                    "as_of_utc_ns": time.time_ns(),
                }
            )
        reconciler = self._get_cold_start_reconciler()
        if reconciler is None:
            return self._envelope(
                {
                    "page": "reconciliation.run",
                    "ran": False,
                    "reason": "NO_RECONCILER_ATTACHED",
                    "snapshot_count": len(snapshots),
                    "dry_run": bool(dry_run),
                    "as_of_utc_ns": time.time_ns(),
                }
            )
        if dry_run:
            return self._envelope(
                {
                    "page": "reconciliation.run",
                    "ran": False,
                    "dry_run": True,
                    "snapshot_count": len(snapshots),
                    "would_classify": len(snapshots),
                    "as_of_utc_ns": time.time_ns(),
                }
            )
        report = reconciler.reconcile(snapshots)
        payload = report.to_dict() if hasattr(report, "to_dict") else {"value": report}
        if payload.get("frozen_silos"):
            try:
                self.alert_center.raise_alert(
                    code="COLD_START_RECON_FREEZE",
                    severity="CRITICAL",
                    source="reconciliation.run",
                    message="cold-start reconciliation froze one or more silos",
                    detail=payload,
                )
            except Exception:  # pragma: no cover - alerting is best effort
                pass
        return self._envelope({"page": "reconciliation.run", "ran": True, **payload})

    def v21_reconciliation_clear_freeze(self, request: ClearFreezeRequest) -> dict[str, Any]:
        guard = getattr(self, "guard", None)
        if guard is None:
            raise HTTPException(status_code=409, detail="NO_SILO_GUARD")
        silo = str(request.silo).strip().upper()
        known = {str(item).upper() for item in self._known_silos()}
        if known and silo not in known:
            raise HTTPException(status_code=422, detail=f"UNKNOWN_SILO:{silo}")
        if not guard.is_frozen(silo):
            return self._envelope(
                {
                    "page": "reconciliation.clear-freeze",
                    "silo": silo,
                    "cleared": False,
                    "reason": "SILO_NOT_FROZEN",
                }
            )
        reason = str(guard.reason(silo))
        if not reason.upper().startswith(("COLD_START", "UNKNOWN_FILL", "AMBIGUOUS", "ROGUE")):
            # Only a *reconciliation* freeze is clearable here; an ADR-009 loss stop
            # or a kill-latch freeze has a different, deliberate reset ceremony.
            raise HTTPException(
                status_code=409,
                detail=f"FREEZE_NOT_CLEARABLE_HERE:{reason}",
            )
        cleared = bool(guard.unfreeze(silo))
        try:
            self.alert_center.raise_alert(
                code="COLD_START_FREEZE_CLEARED",
                severity="WARNING",
                source="reconciliation.clear_freeze",
                silo=silo,
                message=f"operator cleared reconciliation freeze: {request.note}",
                detail={
                    "silo": silo,
                    "previous_reason": reason,
                    "operator": request.operator,
                    "note": request.note,
                },
            )
        except Exception:  # pragma: no cover - best effort
            pass
        return self._envelope(
            {
                "page": "reconciliation.clear-freeze",
                "silo": silo,
                "cleared": cleared,
                "previous_reason": reason,
                "operator": request.operator,
                "note": request.note,
                "as_of_utc_ns": time.time_ns(),
            }
        )

    # ------------------------------------------------------------------
    # G4/G5/G6 read surfaces
    # ------------------------------------------------------------------
    def v21_lifecycle_status(self) -> dict[str, Any]:
        return self._envelope(self.lifecycle_worker.status())

    def v21_lifecycle_tick(self, *, now_utc_ns: int | None = None) -> dict[str, Any]:
        payload = self.lifecycle_worker.run_maintenance_tick(now_utc=now_utc_ns)
        return self._envelope({"page": "lifecycle.tick", **payload})

    def v21_connectors_status(self) -> dict[str, Any]:
        return self._envelope(self.connectors.status())

    def v21_connector_orders(self, *, silo: str) -> dict[str, Any]:
        try:
            payload = self.connectors.fetch_open_orders(silo=silo)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=str(getattr(exc, "code", exc))
            ) from None
        return self._envelope({"page": "connectors.orders", **payload})

    def v21_connector_positions(self, *, silo: str) -> dict[str, Any]:
        try:
            payload = self.connectors.fetch_positions(silo=silo)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=str(getattr(exc, "code", exc))
            ) from None
        return self._envelope({"page": "connectors.positions", **payload})

    def v21_connector_balances(self, *, silo: str) -> dict[str, Any]:
        try:
            payload = self.connectors.fetch_balances(silo=silo)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=str(getattr(exc, "code", exc))
            ) from None
        return self._envelope({"page": "connectors.balances", **payload})

    def v21_bars(
        self,
        *,
        silo: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        try:
            rows = self.bar_aggregator.bars(
                silo=silo, symbol=symbol, timeframe=timeframe, limit=limit
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        payload = {
            "page": "market.bars",
            "timeframes": list(TIMEFRAMES),
            "count": len(rows),
            "bars": list(rows),
            "status": self.bar_aggregator.status(),
        }
        return self._envelope(payload)

    def v21_market_depth(self, *, silo: str, symbol: str) -> dict[str, Any]:
        return self._envelope(self.bar_aggregator.depth(silo=silo, symbol=symbol))

    def v21_market_surface(self, *, silo: str, symbol: str) -> dict[str, Any]:
        return self._envelope(self.bar_aggregator.surface(silo=silo, symbol=symbol))

    def v21_ingest_tick(self, request: TickIngestRequest) -> dict[str, Any]:
        known = {str(item).upper() for item in self._known_silos()}
        silo = str(request.silo).strip().upper()
        if known and silo not in known:
            raise HTTPException(status_code=422, detail=f"UNKNOWN_SILO:{silo}")
        try:
            payload = self.bar_aggregator.ingest(
                silo=silo,
                symbol=request.symbol,
                price=request.price,
                size=request.size,
                bid=request.bid,
                ask=request.ask,
                now_ns=request.now_ns,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return self._envelope({"page": "market.tick", **payload})

    # ------------------------------------------------------------------
    # G3: autopilot cycle
    # ------------------------------------------------------------------
    def v21_autopilot_cycle(self, request: AutopilotCycleRequest) -> dict[str, Any]:
        if self._halted():
            raise HTTPException(
                status_code=409,
                detail="GLOBAL_HALTED: a hot kill latch blocks any new tranche",
            )
        report = self.multi_tranche_autopilot.run_cycle(
            now_ns=request.now_ns,
            tranches=request.tranches,
            mission_id=request.mission_id,
        )
        dispatched = report.get("dispatched_count", 0)
        try:
            self.alert_center.raise_alert(
                code="AUTOPILOT_CYCLE",
                severity="INFO",
                source="autopilot",
                message=f"cycle {report.get('cycle')} dispatched {dispatched} tranche(s)",
                detail={
                    "cycle": report.get("cycle"),
                    "dispatched": dispatched,
                    "blocked": report.get("skipped_blocked_count", 0),
                    "heat_used_pct": report.get("heat_used_pct"),
                },
            )
        except Exception:  # pragma: no cover - alerting never blocks a cycle
            pass
        return self._envelope(report)

    # ------------------------------------------------------------------
    # G1: trade-close saga
    # ------------------------------------------------------------------
    def v21_trade_close(self, request: TradeCloseRequest) -> dict[str, Any]:
        known = {str(item).upper() for item in self._known_silos()}
        silo = str(request.silo).strip().upper()
        if known and silo not in known:
            raise HTTPException(status_code=422, detail=f"UNKNOWN_SILO:{silo}")
        saga = self.profit_split_saga.run_trade_close(
            silo=silo,
            symbol=request.symbol,
            gross_pnl_micros=int(request.gross_pnl_micros),
            notional_micros=int(request.notional_micros),
            side=request.side,
            mission_id=request.mission_id,
            tranche_id=request.tranche_id,
            regime=request.regime,
            reserve_floor_pct=int(self.runtime_settings.settings.default_reserve_floor_pct),
            tax_escrow_pct=int(self.runtime_settings.settings.default_tax_escrow_pct),
            slippage_bps=float(request.slippage_bps),
        )
        self._after_trade_close(saga)
        return self._envelope({"page": "trades.close", "saga": saga})

    def _after_trade_close(self, saga: Mapping[str, Any]) -> None:
        """Emit the SSE frame and the post-mortem a trade close deserves."""
        net = int(saga.get("realized_net_micros", 0))
        silo = str(saga.get("silo", ""))
        symbol = str(saga.get("symbol", ""))
        self.publish_stream_event(
            "WALLET_SWEEP" if saga.get("sweep") else "MISSION_PROGRESS",
            {
                "saga_id": str(saga.get("saga_id", "")),
                "silo": silo,
                "symbol": symbol,
                "state": str(saga.get("state", "")),
                "realized_net_micros": net,
                "statutory_cost_micros": int(saga.get("statutory_cost_micros", 0)),
                "progress_pct": float(saga.get("progress_pct", 0.0)),
                "sweep_state": (saga.get("sweep") or {}).get("state") if saga.get("sweep") else None,
                "reserve_lock_issued": bool((saga.get("reserve_lock") or {}).get("issued", False))
                if saga.get("reserve_lock")
                else False,
            },
        )
        if net < 0:
            self.v21_record_post_mortem(
                symbol=symbol,
                silo=silo,
                net_pnl_micros=net,
                regime=str(saga.get("regime", "")),
            )

    def v21_sagas(self, *, limit: int | None = None) -> dict[str, Any]:
        saga = self.profit_split_saga
        return self._envelope(
            {
                "page": "trades.sagas",
                "status": saga.status(),
                "records": list(saga.records(limit=limit)),
                "recovery": saga.recover(),
            }
        )

    def v21_compensate_saga(self, saga_id: str, request: CompensateRequest) -> dict[str, Any]:
        try:
            record = self.profit_split_saga.compensate(saga_id, reason=request.reason)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"UNKNOWN_SAGA:{saga_id}") from None
        return self._envelope({"page": "trades.compensate", "saga": record})

    # ------------------------------------------------------------------
    # G1/G2: fill -> saga -> memory
    # ------------------------------------------------------------------
    def _v21_after_paper_fill(
        self,
        *,
        request: Any,
        symbol: str,
        side: str,
        net_micros: int,
        notional_micros: int,
    ) -> dict[str, Any]:
        """Run the closed-loop hooks for one filled order.

        ``close_position`` opts into the profit-split saga; a negative P&L always
        writes a post-mortem.  Everything here is additive: an *opening* order
        with the default flags performs no saga work at all.
        """
        silo = str(getattr(request, "silo", "")).strip().upper()
        mission_id = str(getattr(request, "mission_id", "") or "")
        tranche_id = str(getattr(request, "tranche_id", "") or "")
        regime = str(getattr(request, "regime", "NEUTRAL") or "NEUTRAL")
        out: dict[str, Any] = {"saga": None, "post_mortem": None}
        if bool(getattr(request, "close_position", False)):
            saga = self.profit_split_saga.run_trade_close(
                silo=silo,
                symbol=symbol,
                gross_pnl_micros=int(net_micros),
                notional_micros=int(notional_micros),
                side=str(side),
                mission_id=mission_id,
                tranche_id=tranche_id,
                regime=regime,
                reserve_floor_pct=int(
                    self.runtime_settings.settings.default_reserve_floor_pct
                ),
                tax_escrow_pct=int(self.runtime_settings.settings.default_tax_escrow_pct),
            )
            out["saga"] = saga
            self._after_trade_close(saga)
            net_for_memory = int(saga.get("realized_net_micros", 0))
        else:
            net_for_memory = int(net_micros)
        if net_for_memory < 0:
            out["post_mortem"] = self.v21_record_post_mortem(
                symbol=symbol,
                silo=silo,
                regime=regime,
                net_pnl_micros=net_for_memory,
            )
        return out

    # ------------------------------------------------------------------
    # G2: post-mortem
    # ------------------------------------------------------------------
    def v21_record_post_mortem(
        self,
        *,
        symbol: str = "",
        silo: str = "",
        strategy_id: str = "",
        regime: str = "",
        macro_event_id: str = "",
        net_pnl_micros: int = 0,
        slippage_bps: float = 0.0,
        status_code: int | None = None,
        stop_out: bool = False,
    ) -> dict[str, Any]:
        decision = self.post_mortem.record(
            symbol=symbol,
            silo=silo,
            strategy_id=strategy_id or getattr(self, "strategy_id", ""),
            regime=regime,
            macro_event_id=macro_event_id,
            net_pnl_micros=int(net_pnl_micros),
            slippage_bps=float(slippage_bps),
            status_code=status_code,
            stop_out=stop_out,
        )
        if decision.get("recorded"):
            self.publish_stream_event(
                "POST_MORTEM_COMPILED",
                {
                    "outcome_kind": decision.get("outcome_kind"),
                    "code": decision.get("code"),
                    "severity": decision.get("severity"),
                    "symbol": decision.get("symbol"),
                    "silo": decision.get("silo"),
                    "lesson_summary": decision.get("lesson_summary"),
                    "net_pnl_micros": decision.get("net_pnl_micros"),
                },
            )
        return decision

    def v21_post_mortem_status(self) -> dict[str, Any]:
        return self._envelope(self.post_mortem.status())

    # ------------------------------------------------------------------
    # G7: stream event publishing and topic filtering
    # ------------------------------------------------------------------
    def publish_stream_event(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append one out-of-band typed event to the SSE ring buffer."""
        from ring3_cockpit.gateway.payload_envelope import payload_digest

        stamp = time.time_ns()
        row = {
            "type": str(event_type).upper(),
            "event_type": str(event_type).upper(),
            "schema_version": "v2.1.stream-hub",
            "sequence": -1,
            "silo": str(payload.get("silo", "") or "").upper(),
            "symbol": str(payload.get("symbol", "") or "").upper(),
            "ts_ns": stamp,
            "payload": {key: value for key, value in payload.items()},
        }
        row["proof_line"] = (
            f"{row['event_type']} @ {stamp} sha256 {payload_digest(row)[:16]}"
        )
        row["payload_sha256"] = payload_digest(row)
        with self._event_lock():
            buffer = getattr(self, "_stream_events", None)
            if buffer is None:
                buffer = []
                self._stream_events = buffer
            buffer.append(row)
            limit = int(getattr(self, "_stream_event_limit", 256))
            if len(buffer) > limit:
                del buffer[:-limit]
            self._stream_event_sequence = int(getattr(self, "_stream_event_sequence", 0)) + 1
            row["sequence"] = self._stream_event_sequence
        return dict(row)

    def _event_lock(self) -> Any:
        lock = getattr(self, "_stream_event_mutex", None)
        if lock is None:
            import threading

            lock = threading.RLock()
            self._stream_event_mutex = lock
        return lock

    def stream_events(self, *, limit: int | None = None, silo: str | None = None, topic: str | None = None) -> list[dict[str, Any]]:
        with self._event_lock():
            rows = [dict(row) for row in getattr(self, "_stream_events", [])]
        token = str(silo).strip().upper() if silo else None
        wanted = str(topic).strip().upper() if topic else None
        if token:
            rows = [row for row in rows if str(row.get("silo", "")).upper() == token]
        if wanted:
            rows = [row for row in rows if str(row.get("event_type", "")).upper() == wanted]
        return rows[-max(1, int(limit)) :] if limit is not None else rows


#: Settings whose change is a live-risk decision, not a preference.
LIVE_CRITICAL_SETTINGS: tuple[str, ...] = (
    "connector_mode",
    "allow_network",
    "max_drawdown_bps",
    "autopilot_interval_s",
)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_v2_1_routes(
    app: Any,
    control: Any,
    *,
    session_guard: Any,
    live_guard: Any,
    session_token: Any,
) -> None:
    """Attach every V2.1 endpoint and the G11 CSRF middleware."""

    @app.middleware("http")
    async def nios_csrf_guard(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if (
            request.method in CSRF_SAFE_METHODS
            or not path.startswith("/api/v2/")
            or path in CSRF_EXEMPT_PATHS
            or any(path.startswith(prefix) for prefix in CSRF_EXEMPT_PREFIXES)
        ):
            return await call_next(request)
        header_session = (request.headers.get("X-NIOS-Session") or "").strip()
        if header_session:
            # The header transport cannot be produced by a cross-site form post,
            # so it is not a CSRF vector.
            return await call_next(request)
        cookie = request.cookies.get("nios_session")
        if not cookie:
            # Unauthenticated: let the route answer with its own 401.
            return await call_next(request)
        provided = (request.headers.get(CSRF_HEADER) or "").strip()
        expected = csrf_token_for(cookie)
        if not provided or not secrets.compare_digest(provided, expected):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "CSRF_TOKEN_MISSING_OR_INVALID",
                    "csrf_header": CSRF_HEADER,
                    "hint": "call GET /api/v2/auth/session with the cookie session to obtain csrf_token",
                },
            )
        return await call_next(request)

    # ---- G8 analytics -------------------------------------------------
    @app.get("/api/v2/analytics/performance")
    def analytics_performance(
        silo: str | None = None,
        mission_id: str | None = None,
        bucket: str = "1D",
    ) -> dict[str, Any]:
        return control.v21_analytics_performance(
            silo=silo, mission_id=mission_id, bucket=bucket
        )

    # ---- G9 alerts ----------------------------------------------------
    @app.get("/api/v2/alerts")
    def alerts_list(
        severity: str | None = None,
        unacked_only: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return control.v21_alerts(severity=severity, unacked_only=unacked_only, limit=limit)

    @app.post("/api/v2/alerts", dependencies=[Depends(session_guard)])
    def alerts_raise(payload: AlertRaiseRequest) -> dict[str, Any]:
        return control.v21_alert_raise(payload)

    @app.post("/api/v2/alerts/{alert_id}/ack", dependencies=[Depends(session_guard)])
    def alerts_ack(alert_id: str, payload: AlertAckRequest | None = None) -> dict[str, Any]:
        return control.v21_alert_ack(alert_id, payload or AlertAckRequest())

    # ---- G10 settings -------------------------------------------------
    @app.get("/api/v2/settings")
    def settings_read() -> dict[str, Any]:
        return control.v21_settings_document()

    @app.post("/api/v2/settings")
    def settings_write(payload: SettingsUpdateRequest, request: Request) -> dict[str, Any]:
        patch = payload.patch()
        if control.live_critical_patch(patch):
            # A live-risk knob costs a LIVE-elevated session, exactly like a
            # broker hot-swap does.
            control.live_tier_guard(session_token(request))
        else:
            control.session_guard(session_token(request))
        return control.v21_update_settings(payload)

    # ---- G11 session telemetry ----------------------------------------
    @app.get("/api/v2/auth/session")
    def auth_session(request: Request) -> dict[str, Any]:
        return control.v21_auth_session(session_token(request))

    # ---- G12 reconciliation -------------------------------------------
    @app.get("/api/v2/reconciliation/status")
    def reconciliation_status() -> dict[str, Any]:
        return control.v21_reconciliation_status()

    @app.post(
        "/api/v2/control/reconciliation/run", dependencies=[Depends(session_guard)]
    )
    def reconciliation_run(payload: ReconciliationRunRequest | None = None) -> dict[str, Any]:
        body = payload or ReconciliationRunRequest()
        return control.v21_reconciliation_run(dry_run=body.dry_run)

    @app.post(
        "/api/v2/control/reconciliation/clear-freeze",
        dependencies=[Depends(live_guard)],
    )
    def reconciliation_clear_freeze(
        payload: ClearFreezeRequest, request: Request
    ) -> dict[str, Any]:
        control.live_tier_guard(session_token(request))
        return control.v21_reconciliation_clear_freeze(payload)

    # ---- G4 lifecycle -------------------------------------------------
    @app.get("/api/v2/lifecycle/status")
    def lifecycle_status() -> dict[str, Any]:
        return control.v21_lifecycle_status()

    @app.post("/api/v2/control/lifecycle/tick", dependencies=[Depends(session_guard)])
    def lifecycle_tick(payload: LifecycleTickRequest | None = None) -> dict[str, Any]:
        body = payload or LifecycleTickRequest()
        return control.v21_lifecycle_tick(now_utc_ns=body.now_utc_ns)

    # ---- G5 connectors ------------------------------------------------
    @app.get("/api/v2/connectors/status")
    def connectors_status() -> dict[str, Any]:
        return control.v21_connectors_status()

    @app.get("/api/v2/connectors/orders")
    def connectors_orders(silo: str) -> dict[str, Any]:
        return control.v21_connector_orders(silo=silo)

    @app.get("/api/v2/connectors/positions")
    def connectors_positions(silo: str) -> dict[str, Any]:
        return control.v21_connector_positions(silo=silo)

    @app.get("/api/v2/connectors/balances")
    def connectors_balances(silo: str) -> dict[str, Any]:
        return control.v21_connector_balances(silo=silo)

    # ---- G6 bars ------------------------------------------------------
    @app.get("/api/v2/market/bars")
    def market_bars(
        silo: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return control.v21_bars(
            silo=silo, symbol=symbol, timeframe=timeframe, limit=limit
        )

    @app.get("/api/v2/market/depth")
    def market_depth(silo: str, symbol: str) -> dict[str, Any]:
        return control.v21_market_depth(silo=silo, symbol=symbol)

    @app.get("/api/v2/market/surface")
    def market_surface(silo: str, symbol: str) -> dict[str, Any]:
        return control.v21_market_surface(silo=silo, symbol=symbol)

    @app.post("/api/v2/control/market/tick", dependencies=[Depends(session_guard)])
    def market_tick(payload: TickIngestRequest) -> dict[str, Any]:
        return control.v21_ingest_tick(payload)

    # ---- G3 autopilot -------------------------------------------------
    @app.post(
        "/api/v2/control/autopilot/tick", dependencies=[Depends(session_guard)]
    )
    def autopilot_tick(payload: AutopilotCycleRequest | None = None) -> dict[str, Any]:
        return control.v21_autopilot_cycle(payload or AutopilotCycleRequest())

    # ---- G1 trade-close saga -----------------------------------------
    @app.post("/api/v2/control/trades/close", dependencies=[Depends(session_guard)])
    def trades_close(payload: TradeCloseRequest) -> dict[str, Any]:
        return control.v21_trade_close(payload)

    @app.get("/api/v2/trades/sagas")
    def trades_sagas(limit: int | None = None) -> dict[str, Any]:
        return control.v21_sagas(limit=limit)

    @app.post(
        "/api/v2/control/trades/sagas/{saga_id}/compensate",
        dependencies=[Depends(session_guard)],
    )
    def trades_compensate(
        saga_id: str, payload: CompensateRequest | None = None
    ) -> dict[str, Any]:
        return control.v21_compensate_saga(saga_id, payload or CompensateRequest())

    # ---- G2 post-mortem ----------------------------------------------
    @app.get("/api/v2/post-mortems")
    def post_mortems() -> dict[str, Any]:
        return control.v21_post_mortem_status()
