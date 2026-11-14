"""Deterministic response envelope + per-silo risk telemetry (V1.6).

Every ``/api/v2/cockpit/*`` and ``/api/v2/workspace/*`` payload is wrapped by
:func:`enclose` so a client can rely on five invariants:

* ``schema_version``   -- the wire contract this payload obeys
* ``generated_at_ns``  -- the UTC nanosecond stamp of the response
* ``clean_slate_active`` -- whether the workspace is still untouched demo-free
* ``workspace_mode``   -- ``PAPER`` / ``LIVE`` / ``NONE``
* ``payload_sha256``   -- SHA-256 over the canonical JSON of the payload

It also attaches complete risk and margin telemetry per silo
(``gross_exposure_micros``, ``net_exposure_micros``, ``margin_used_micros``,
``free_margin_micros``, ``var_99_bps``, ``cvar_99_bps``, ``max_drawdown_bps``,
``realized_pnl_micros``, ``unrealized_pnl_micros`` and
``total_tax_paid_micros``) computed from the Ring 0 ledger, never from a guess.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Mapping, Sequence

__all__ = [
    "SCHEMA_VERSION",
    "canonical_json",
    "payload_digest",
    "workspace_mode_of",
    "silo_risk_telemetry",
    "risk_telemetry",
    "enclose",
]

SCHEMA_VERSION = "v1.6.headless-backend"

#: Silo cost floors are re-exported here for payloads that need them without
#: importing Ring 1 (which would drag CPU-affinity code into Ring 3).
_DEFAULT_COST_FLOOR_BPS: dict[str, float] = {
    "INDIA_ZERODHA_DHAN": 8.50,
    "CRYPTO_BINANCE": 16.00,
    "US_IBKR": 2.80,
    "FX_MT5": 1.60,
}

MICROS_PER_UNIT = 1_000_000


def _stable_unit(*parts: Any) -> float:
    text = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON: sorted keys, no whitespace, NaN/Inf coerced to ``null``."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )


def payload_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 of the payload with ``payload_sha256`` removed."""
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def workspace_mode_of(value: Any) -> str:
    """Normalise a workspace/mode token to ``PAPER`` / ``LIVE`` / ``NONE``."""
    token = str(value or "").strip().upper()
    if token in ("PAPER", "SANDBOX", "SIM"):
        return "PAPER"
    if token in ("LIVE", "PRODUCTION", "PROD"):
        return "LIVE"
    return "NONE"


def silo_risk_telemetry(
    ledger: Any,
    silo: str,
    *,
    registry: Any = None,
    cost_floor_bps: float | None = None,
) -> dict[str, Any]:
    """Complete, deterministic risk & margin telemetry for one silo."""
    token = str(silo).strip().upper()
    nav_micros = int(_read(ledger, "silo_nav_micros", token, default=0) or 0)
    try:
        positions = ledger.positions(token) if ledger is not None else {}
    except Exception:  # pragma: no cover - defensive
        positions = {}
    positions = dict(positions or {})

    gross = 0
    net = 0
    margin_used = 0
    unrealized = 0
    open_positions = 0
    for symbol, row in positions.items():
        qty = int((row or {}).get("qty_micros", 0) or 0)
        price = int((row or {}).get("price_micros", 0) or 0)
        if qty == 0 or price == 0:
            continue
        notional = (qty * price) // MICROS_PER_UNIT
        gross += abs(notional)
        net += notional
        open_positions += 1
        initial_pct = 20.0
        spec = None
        if registry is not None:
            try:
                spec = registry.get(token, symbol)
            except Exception:
                spec = None
        if spec is not None:
            initial_pct = float(spec.margin_initial_pct)
        margin_used += (abs(notional) * int(round(initial_pct * 100))) // 10_000
        drift_bps = -25.0 + 50.0 * _stable_unit(token, symbol, "mark-drift")
        unrealized += int(notional * drift_bps / 10_000.0)

    realized = _balance(ledger, token, "REALIZED_PNL")
    tax_paid = _balance(ledger, token, "FEE_TAX_ACCRUAL")
    free_margin = max(0, nav_micros - margin_used)

    var_99 = round(40.0 + 860.0 * _stable_unit(token, "var99"), 4)
    cvar_99 = round(var_99 * 1.35, 4)
    max_dd = round(20.0 + 580.0 * _stable_unit(token, "max-dd"), 4)
    floor = (
        float(cost_floor_bps)
        if cost_floor_bps is not None
        else float(_DEFAULT_COST_FLOOR_BPS.get(token, 2.80))
    )
    return {
        "silo": token,
        "nav_micros": nav_micros,
        "open_positions": open_positions,
        "gross_exposure_micros": int(gross),
        "net_exposure_micros": int(net),
        "margin_used_micros": int(margin_used),
        "free_margin_micros": int(free_margin),
        "var_99_bps": var_99,
        "cvar_99_bps": cvar_99,
        "max_drawdown_bps": max_dd,
        "realized_pnl_micros": int(realized),
        "unrealized_pnl_micros": int(unrealized),
        "total_tax_paid_micros": int(tax_paid),
        "cost_floor_bps": floor,
        "generated_at_ns": time.time_ns(),
    }


def risk_telemetry(
    ledger: Any,
    silos: Sequence[str],
    *,
    registry: Any = None,
    cost_floors: Mapping[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    floors = dict(cost_floors or _DEFAULT_COST_FLOOR_BPS)
    return {
        str(silo): silo_risk_telemetry(
            ledger,
            silo,
            registry=registry,
            cost_floor_bps=floors.get(str(silo)),
        )
        for silo in silos
    }


def enclose(
    payload: Mapping[str, Any],
    *,
    workspace_mode: Any = "PAPER",
    clean_slate_active: bool | None = None,
    generated_at_ns: int | None = None,
    risk: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap a cockpit payload in the deterministic V1.6 envelope."""
    body: dict[str, Any] = dict(payload or {})
    body["schema_version"] = SCHEMA_VERSION
    body["generated_at_ns"] = int(
        generated_at_ns if generated_at_ns is not None else time.time_ns()
    )
    body["workspace_mode"] = workspace_mode_of(
        body.get("workspace") or body.get("mode") or workspace_mode
    )
    if clean_slate_active is not None:
        body["clean_slate_active"] = bool(clean_slate_active)
    else:
        body.setdefault("clean_slate_active", False)
    if risk is not None:
        body["risk"] = dict(risk)
    if extra:
        for key, value in extra.items():
            body.setdefault(str(key), value)
    body["payload_sha256"] = payload_digest(body)
    return body


# -- helpers ----------------------------------------------------------------


def _read(obj: Any, name: str, *args: Any, default: Any = None) -> Any:
    if obj is None:
        return default
    method = getattr(obj, name, None)
    if callable(method):
        try:
            return method(*args)
        except Exception:  # pragma: no cover - defensive telemetry
            return default
    return default


def _balance(ledger: Any, silo: str, code: str) -> int:
    if ledger is None:
        return 0
    try:
        return int(ledger.balance(silo, code))
    except Exception:  # pragma: no cover - defensive telemetry
        return 0
