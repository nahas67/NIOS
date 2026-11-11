"""Ring 0 broker sandbox / testnet HTTP execution bridge (``sec_011``, ``sec_015``).

Why this module exists
----------------------
Phase 3 Step 2 proved that a venue credential can be encrypted in Ring 0 and
verified with a deterministic dry-run handshake.  That is custody, not
execution.  This module adds the missing last inch: a **signed HTTP order**
against each venue's official sandbox / testnet / paper host, still entirely
inside Ring 0.

Ring ownership
--------------
* Only Ring 0 may import this module.  It reads decrypted credentials from
  :mod:`ring0_vault.silos.key_vault.broker_credentials` at the exact moment it
  signs a request, and it never returns, logs or embeds a raw key: every
  receipt carries masked tokens and *header names* only.
* The two-phase :class:`~ring0_vault.ibor.ledger_outbox.CrashAtomicOutbox`
  reservation is always taken **before** the socket is touched, so a rejected or
  timed-out sandbox order can only ever leave a voided reservation, never an
  orphaned debit.

Fatal flaws defended (``sec_002``)
----------------------------------
#1 LLM tick-latency in the execution path -- nothing here is on the hot path;
   the bridge is driven by an explicit operator request or a Ring 0 OMS event.
#5 cross-broker wire and tax bleed -- one silo per dispatch, no cross-border
   transfer, and the 35/15 ratchet compounds inside the originating silo.
#6 prompt-injection exposure of broker keys -- keys are decrypted per request
   inside the Ring 0 process and never leave it.

Laws preserved (``sec_018``)
----------------------------
Law 4 -- nothing is signed without a Ring 0 reservation, and a failed venue
response voids that reservation instead of leaving capital locked.
Law 5 -- a fill is committed through the in-silo profit ratchet
(35% compounder / 15% reserve / 50% retained scalp).

Network discipline
------------------
``httpx`` is used only through an injected client or transport.  With neither
injected and ``allow_network=False`` (the default) a dispatch raises
:class:`SandboxNetworkDisabled` **before** any socket is created, so a unit test
can never reach a live venue by accident.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from core.schemas import UnsignedProposal, VaultName

__all__ = [
    "CLEAN_VENUES",
    "MAX_SANDBOX_RETRIES",
    "RETRY_STATUS_CODES",
    "SANDBOX_VENUE_SPECS",
    "SandboxCredentialMissing",
    "SandboxDispatchReceipt",
    "SandboxNetworkDisabled",
    "SandboxVenueSpec",
    "TestnetExecutionBridge",
    "mask_headers",
    "sandbox_target",
]

#: ``SANDBOX`` credentials can never be mistaken for production capital.
SANDBOX_ENV = "SANDBOX"
LIVE_ENV = "LIVE"

#: Bounded retry policy for a venue that answers ``429`` or ``5xx``.
MAX_SANDBOX_RETRIES = 2
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

#: Monotonic-ish clock seam so tests are deterministic.
Clock = Callable[[], int]


# ---------------------------------------------------------------------------
# Venue sandbox catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxVenueSpec:
    """One venue's official sandbox/testnet REST surface and auth scheme."""

    venue: str
    silo: str
    environment: str
    base_url: str
    order_path: str
    signing: str
    auth: str
    required_headers: tuple[str, ...] = ()
    notes: str = ""

    def url(self, **params: str) -> str:
        path = self.order_path.format(**params) if params else self.order_path
        return f"{self.base_url.rstrip('/')}{path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "silo": self.silo,
            "environment": self.environment,
            "base_url": self.base_url,
            "order_path": self.order_path,
            "signing": self.signing,
            "auth": self.auth,
            "required_headers": list(self.required_headers),
            "notes": self.notes,
            # Never a credential: the cockpit shows the destination only.
            "credential_material": "ring0_vault/silos/key_vault (write-only)",
            "socket_opened_by_registry": False,
        }


#: Every venue Phase 3 Step 3 must be able to dispatch to, sandbox-only.
#:
#: Sources: Binance Spot Testnet, OKX Demo Trading, Bybit Testnet, Dhan Sandbox,
#: Kite Connect (paper key), Angel SmartAPI sandbox, Upstox Sandbox, Alpaca Paper,
#: IBKR Client Portal paper gateway, OANDA v20 Practice, and the local vn.py
#: MT5/CTP paper IPC gateways.
SANDBOX_VENUE_SPECS: dict[str, SandboxVenueSpec] = {
    # ---- Crypto ---------------------------------------------------------
    "binance": SandboxVenueSpec(
        venue="binance",
        silo="CRYPTO_BINANCE",
        environment="testnet",
        base_url="https://testnet.binance.vision",
        order_path="/api/v3/order",
        signing="hmac_sha256_query",
        auth="X-MBX-APIKEY",
        required_headers=("X-MBX-APIKEY",),
        notes="Spot Testnet: HMAC-SHA256 over the query string + timestamp.",
    ),
    "okx": SandboxVenueSpec(
        venue="okx",
        silo="CRYPTO_BINANCE",
        environment="demo",
        base_url="https://www.okx.com",
        order_path="/api/v5/trade/order",
        signing="hmac_sha256_base64",
        auth="OK-ACCESS-KEY",
        required_headers=(
            "OK-ACCESS-KEY",
            "OK-ACCESS-SIGN",
            "OK-ACCESS-TIMESTAMP",
            "OK-ACCESS-PASSPHRASE",
            "x-simulated-trading",
        ),
        notes="Demo Trading is selected by the x-simulated-trading: 1 header.",
    ),
    "bybit": SandboxVenueSpec(
        venue="bybit",
        silo="CRYPTO_BINANCE",
        environment="testnet",
        base_url="https://api-testnet.bybit.com",
        order_path="/v5/order/create",
        signing="hmac_sha256_hex_headers",
        auth="X-BAPI-API-KEY",
        required_headers=(
            "X-BAPI-API-KEY",
            "X-BAPI-TIMESTAMP",
            "X-BAPI-RECV-WINDOW",
            "X-BAPI-SIGN",
        ),
        notes="V5 testnet: sign(timestamp + apiKey + recvWindow + rawBody).",
    ),
    # ---- India ----------------------------------------------------------
    "dhan": SandboxVenueSpec(
        venue="dhan",
        silo="INDIA_ZERODHA_DHAN",
        environment="sandbox",
        base_url="https://sandbox.dhan.co",
        order_path="/v2/orders",
        signing="none",
        auth="access-token",
        required_headers=("access-token",),
        notes="Dhan Sandbox authenticates with the access-token header.",
    ),
    "zerodha": SandboxVenueSpec(
        venue="zerodha",
        silo="INDIA_ZERODHA_DHAN",
        environment="paper",
        base_url="https://api.kite.trade",
        order_path="/orders/regular",
        signing="kite_checksum",
        auth="Authorization",
        required_headers=("X-Kite-Version", "Authorization"),
        notes="Kite: Authorization: token api_key:sha256(api_key+ts+secret).",
    ),
    "angel": SandboxVenueSpec(
        venue="angel",
        silo="INDIA_ZERODHA_DHAN",
        environment="sandbox",
        base_url="https://apiconnect.angelone.in",
        order_path="/rest/secure/angelbroking/order/v1/placeOrder",
        signing="none",
        auth="X-PrivateKey",
        required_headers=("X-PrivateKey", "Authorization"),
        notes="SmartAPI sandbox: X-PrivateKey + a short-lived session bearer.",
    ),
    "upstox": SandboxVenueSpec(
        venue="upstox",
        silo="INDIA_ZERODHA_DHAN",
        environment="sandbox",
        base_url="https://api-sandbox.upstox.com",
        order_path="/v2/order/place",
        signing="none",
        auth="Authorization",
        required_headers=("Authorization", "Api-Version"),
        notes="Upstox Sandbox: bearer token plus Api-Version: 2.0.",
    ),
    # ---- US -------------------------------------------------------------
    "alpaca": SandboxVenueSpec(
        venue="alpaca",
        silo="US_IBKR",
        environment="paper",
        base_url="https://paper-api.alpaca.markets",
        order_path="/v2/orders",
        signing="none",
        auth="APCA-API-KEY-ID",
        required_headers=("APCA-API-KEY-ID", "APCA-API-SECRET-KEY"),
        notes="Alpaca Paper: key-id/secret-key header pair, no HMAC.",
    ),
    "interactive_brokers": SandboxVenueSpec(
        venue="interactive_brokers",
        silo="US_IBKR",
        environment="paper",
        base_url="https://localhost:5000",
        order_path="/v1/api/iserver/account/{account_id}/orders",
        signing="none",
        auth="Authorization",
        required_headers=("Authorization",),
        notes="IBKR Client Portal paper gateway (local, TLS, session token).",
    ),
    # ---- FX -------------------------------------------------------------
    "oanda": SandboxVenueSpec(
        venue="oanda",
        silo="FX_MT5",
        environment="practice",
        base_url="https://api-fxpractice.oanda.com",
        order_path="/v3/accounts/{account_id}/orders",
        signing="none",
        auth="Authorization",
        required_headers=("Authorization",),
        notes="OANDA v20 Practice: bearer token, account id in the path.",
    ),
    "vnpy_mt5": SandboxVenueSpec(
        venue="vnpy_mt5",
        silo="FX_MT5",
        environment="paper",
        base_url="http://127.0.0.1:8787",
        order_path="/order",
        signing="none",
        auth="X-NIOS-Gateway-Token",
        required_headers=("X-NIOS-Gateway-Token",),
        notes="vn.py MT5 IPC paper gateway, bound to loopback only.",
    ),
    "vnpy_ctp": SandboxVenueSpec(
        venue="vnpy_ctp",
        silo="FX_MT5",
        environment="paper",
        base_url="http://127.0.0.1:8788",
        order_path="/order",
        signing="none",
        auth="X-NIOS-Gateway-Token",
        required_headers=("X-NIOS-Gateway-Token",),
        notes="vn.py CTP IPC paper gateway, bound to loopback only.",
    ),
}

#: Venues whose sandbox host is not the production host, published for audits.
CLEAN_VENUES: tuple[str, ...] = tuple(SANDBOX_VENUE_SPECS)


class SandboxNetworkDisabled(RuntimeError):
    """Raised when a dispatch would open a real socket without authorization."""


class SandboxCredentialMissing(RuntimeError):
    """Raised when Ring 0 holds no credential for the requested venue."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def mask_token(token: str, *, keep: int = 4) -> str:
    """``***`` + last four characters; never the whole token."""

    text = str(token or "")
    if not text:
        return ""
    if len(text) <= keep:
        return "*" * len(text)
    return "***" + text[-keep:]


def mask_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Mask every header *value*; the names are what a receipt may prove."""

    masked: dict[str, str] = {}
    for name, value in headers.items():
        token = str(value)
        if name.lower() in ("content-type", "accept", "api-version", "x-kite-version"):
            masked[name] = token
        elif name.lower() == "x-simulated-trading":
            masked[name] = token
        elif name.lower() == "x-bapi-recv-window":
            masked[name] = token
        else:
            masked[name] = mask_token(token)
    return masked


def sandbox_target(silo: str, venue: str) -> dict[str, Any]:
    """Resolve where a sandbox order for ``venue`` would go.  No secret involved."""

    key = str(venue).strip().lower()
    spec = SANDBOX_VENUE_SPECS.get(key)
    if spec is None:
        raise ValueError(f"unsupported sandbox venue: {venue!r}")
    owner = str(silo).strip().upper()
    if owner and owner != spec.silo:
        raise ValueError(
            f"venue {key!r} belongs to silo {spec.silo!r}, not {owner!r} (sec_015)"
        )
    return spec.to_dict()


def _hmac_hex(secret: str, payload: str) -> str:
    return hmac.new(
        str(secret).encode("utf-8"), str(payload).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _hmac_b64(secret: str, payload: str) -> str:
    digest = hmac.new(
        str(secret).encode("utf-8"), str(payload).encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _kite_checksum(api_key: str, timestamp: str, api_secret: str) -> str:
    return hashlib.sha256(
        f"{api_key}{timestamp}{api_secret}".encode("utf-8")
    ).hexdigest()


@dataclass(slots=True)
class _RequestPlan:
    """A signed request ready for the transport.  Holds no reusable secret."""

    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, Any] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    signature_alg: str = "none"
    signature_preview: str = ""
    body_digest: str = ""

    def header_names(self) -> list[str]:
        return sorted(self.headers)

    def masked_headers(self) -> dict[str, str]:
        return mask_headers(self.headers)


@dataclass(slots=True)
class SandboxDispatchReceipt:
    """Operator-facing proof of one sandbox dispatch.  Never contains a key."""

    venue: str
    silo: str
    environment: str
    endpoint: str
    method: str
    sandbox: bool
    accepted: bool
    committed: bool
    voided: bool
    http_status: int | None
    attempts: int
    signature_alg: str
    signature_preview: str
    header_names: tuple[str, ...] = ()
    masked_headers: Mapping[str, str] = field(default_factory=dict)
    api_key_masked: str | None = None
    body_digest: str = ""
    broker_order_id: str = ""
    reservation_key: str = ""
    outbox_state: str = ""
    pending_transfer_id: int | None = None
    realized_net_micros: int = 0
    ratchet: Mapping[str, Any] | None = None
    reconcile: Mapping[str, Any] | None = None
    error: str = ""
    response_digest: str = ""
    latency_ms: float = 0.0
    dispatched_at_ns: int = 0
    raw_secret_returned: bool = False
    open_sockets_in_tests: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "silo": self.silo,
            "environment": self.environment,
            "endpoint": self.endpoint,
            "method": self.method,
            "sandbox": bool(self.sandbox),
            "accepted": bool(self.accepted),
            "committed": bool(self.committed),
            "voided": bool(self.voided),
            "http_status": self.http_status,
            "attempts": int(self.attempts),
            "signature_alg": self.signature_alg,
            "signature_preview": self.signature_preview,
            "header_names": list(self.header_names),
            "masked_headers": dict(self.masked_headers),
            "api_key_masked": self.api_key_masked,
            "body_digest": self.body_digest,
            "broker_order_id": self.broker_order_id,
            "reservation_key": self.reservation_key,
            "outbox_state": self.outbox_state,
            "pending_transfer_id": self.pending_transfer_id,
            "realized_net_micros": int(self.realized_net_micros),
            "ratchet": dict(self.ratchet) if self.ratchet else None,
            "reconcile": dict(self.reconcile) if self.reconcile else None,
            "error": self.error,
            "response_digest": self.response_digest,
            "latency_ms": round(float(self.latency_ms), 3),
            "dispatched_at_ns": int(self.dispatched_at_ns),
            # ---- air-gap assertions the cockpit renders verbatim ----------
            "raw_secret_returned": False,
            "write_only": True,
            "ring": "ring0_vault",
        }


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class TestnetExecutionBridge:
    """Signed sandbox order dispatch, Ring 0 only.

    Parameters
    ----------
    outbox:
        The :class:`~ring0_vault.ibor.ledger_outbox.CrashAtomicOutbox`.  A
        reservation is always taken before the socket is opened.
    ledger:
        The :class:`~ring0_vault.ibor.ledger_outbox.IBORLedger` the fill is
        committed into, including the 35/15 in-silo ratchet.
    transport / client:
        An ``httpx.BaseTransport`` or a fully-built ``httpx.Client``.  Either one
        keeps the test completely offline.
    allow_network:
        Explicit opt-in for a real socket.  Default ``False``: dispatching with
        no injected transport raises :class:`SandboxNetworkDisabled`.
    """

    def __init__(
        self,
        outbox: Any,
        ledger: Any,
        *,
        vault: Any = None,
        registry: Any = None,
        transport: Any = None,
        client: Any = None,
        allow_network: bool = False,
        max_retries: int = MAX_SANDBOX_RETRIES,
        retry_backoff_s: float = 0.0,
        accounting: Any = None,
        reconciler: Any = None,
        clock: Clock = time.time_ns,
        timeout_s: float = 5.0,
    ) -> None:
        self.outbox = outbox
        self.ledger = ledger
        self.vault = vault
        self.registry = registry
        self.transport = transport
        self._client = client
        self.allow_network = bool(allow_network)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_s = max(0.0, float(retry_backoff_s))
        self.accounting = accounting
        self.reconciler = reconciler
        self.clock = clock
        self.timeout_s = float(timeout_s)
        self.dispatches = 0
        self.committed = 0
        self.voided = 0
        self.retries = 0
        self.last_receipt: SandboxDispatchReceipt | None = None
        self._history: list[SandboxDispatchReceipt] = []

    # -- dependencies ------------------------------------------------------
    def _store(self) -> Any:
        if self.vault is not None:
            return self.vault
        from ring0_vault.silos.key_vault.broker_credentials import (
            default_broker_vault,
        )

        return default_broker_vault()

    def _market_silo(self, silo: str) -> Any:
        from ring0_vault.silos.silo_registry import default_registry

        registry = self.registry if self.registry is not None else default_registry()
        return registry.get(silo)

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.allow_network and self.transport is None:
            raise SandboxNetworkDisabled(
                "no httpx transport injected and allow_network=False; refusing to "
                "open a live broker socket from a unit test"
            )
        try:
            import httpx
        except Exception as exc:  # pragma: no cover - httpx is a test dependency
            raise SandboxNetworkDisabled(f"httpx unavailable: {exc}") from exc
        self._client = httpx.Client(
            transport=self.transport,
            timeout=self.timeout_s,
            follow_redirects=False,
        )
        return self._client

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None and self.transport is None:
            try:
                client.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def __enter__(self) -> "TestnetExecutionBridge":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- credentials -------------------------------------------------------
    def _credential(self, venue: str, field: str) -> str | None:
        """Decrypt one field inside Ring 0.  The value never leaves this stack."""

        key = f"broker.{venue}.{field}"
        store = self._store()
        try:
            value = store.reveal(key)
        except AttributeError:  # pragma: no cover - older vault
            value = store._raw_for_test_only(key)  # noqa: SLF001
        return None if value is None else str(value)

    def _require_credentials(self, venue: str) -> tuple[str, str]:
        api_key = self._credential(venue, "api_key")
        api_secret = self._credential(venue, "api_secret")
        if not api_key or not api_secret:
            raise SandboxCredentialMissing(
                f"Ring 0 holds no sandbox credential for {venue!r}; store one with "
                f"POST /api/v2/control/brokers/credentials first"
            )
        return api_key, api_secret

    def credential_environment(self, venue: str) -> str:
        env = self._credential(venue, "env")
        return str(env or SANDBOX_ENV)

    # -- request signing ---------------------------------------------------
    def _sign(
        self,
        spec: SandboxVenueSpec,
        payload: Mapping[str, Any],
        api_key: str,
        api_secret: str,
        *,
        account_id: str,
        now_ns: int,
    ) -> _RequestPlan:
        url = spec.url(account_id=account_id)
        body_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        body = dict(payload)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        params: dict[str, Any] = {}
        alg = "none"
        preview = ""

        if spec.signing == "hmac_sha256_query":
            timestamp = str(int(now_ns) // 1_000_000)
            query = dict(body)
            query["timestamp"] = timestamp
            query["recvWindow"] = 5_000
            encoded = "&".join(
                f"{name}={json.dumps(value) if isinstance(value, (dict, list)) else value}"
                for name, value in query.items()
            )
            signature = _hmac_hex(api_secret, encoded)
            params = {**query, "signature": signature}
            headers["X-MBX-APIKEY"] = api_key
            alg = "HMAC-SHA256(query)"
            preview = mask_token(signature, keep=6)
            body = None  # type: ignore[assignment]

        elif spec.signing == "hmac_sha256_base64":
            timestamp = time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(int(now_ns) / 1e9)
            )
            request_path = f"{spec.order_path}?{''}".split("?")[0]
            prehash = f"{timestamp}POST{request_path}{body_text}"
            signature = _hmac_b64(api_secret, prehash)
            passphrase = (
                self._credential(spec.venue, "passphrase") or "SANDBOX-DEMO"
            )
            headers.update(
                {
                    "OK-ACCESS-KEY": api_key,
                    "OK-ACCESS-SIGN": signature,
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": passphrase,
                    "x-simulated-trading": "1",
                }
            )
            alg = "HMAC-SHA256-BASE64(timestamp+METHOD+path+body)"
            preview = mask_token(signature, keep=6)
            body = dict(payload)

        elif spec.signing == "hmac_sha256_hex_headers":
            timestamp = str(int(now_ns) // 1_000_000)
            recv_window = "5000"
            prehash = f"{timestamp}{api_key}{recv_window}{body_text}"
            signature = _hmac_hex(api_secret, prehash)
            headers.update(
                {
                    "X-BAPI-API-KEY": api_key,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-RECV-WINDOW": recv_window,
                    "X-BAPI-SIGN": signature,
                }
            )
            alg = "HMAC-SHA256(timestamp+apiKey+recvWindow+body)"
            preview = mask_token(signature, keep=6)
            body = dict(payload)

        elif spec.signing == "kite_checksum":
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(now_ns) / 1e9))
            checksum = _kite_checksum(api_key, timestamp, api_secret)
            headers.update(
                {
                    "X-Kite-Version": "3",
                    "Authorization": f"token {api_key}:{checksum}",
                }
            )
            alg = "SHA256(api_key+timestamp+api_secret)"
            preview = mask_token(checksum, keep=6)
            body = dict(payload)

        elif spec.venue == "dhan":
            headers["access-token"] = api_key
            alg = "access-token header"
            preview = mask_token(api_key)
            body = dict(payload)

        elif spec.venue == "angel":
            headers.update(
                {
                    "X-PrivateKey": api_key,
                    "X-UserType": "USER",
                    "X-SourceID": "WEB",
                    "X-ClientLocalIP": "127.0.0.1",
                    "X-MACAddress": "00:00:00:00:00:00",
                    "Authorization": f"Bearer {api_secret}",
                }
            )
            alg = "X-PrivateKey + session bearer"
            preview = mask_token(api_key)
            body = dict(payload)

        elif spec.venue == "upstox":
            headers.update(
                {"Authorization": f"Bearer {api_key}", "Api-Version": "2.0"}
            )
            alg = "bearer"
            preview = mask_token(api_key)
            body = dict(payload)

        elif spec.venue == "alpaca":
            headers.update(
                {
                    "APCA-API-KEY-ID": api_key,
                    "APCA-API-SECRET-KEY": api_secret,
                }
            )
            alg = "APCA key/secret headers"
            preview = mask_token(api_key)
            body = dict(payload)

        elif spec.venue == "oanda":
            headers["Authorization"] = f"Bearer {api_key}"
            alg = "bearer"
            preview = mask_token(api_key)
            body = dict(payload)

        elif spec.venue == "interactive_brokers":
            headers["Authorization"] = f"Bearer {api_secret}"
            alg = "session bearer"
            preview = mask_token(api_secret)
            body = dict(payload)

        else:  # vnpy_mt5 / vnpy_ctp local paper gateways
            headers["X-NIOS-Gateway-Token"] = api_secret
            alg = "loopback gateway token"
            preview = mask_token(api_secret)
            body = dict(payload)

        digest = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        return _RequestPlan(
            method="POST",
            url=url,
            headers=headers,
            params=params,
            json_body=body,
            signature_alg=alg,
            signature_preview=preview,
            body_digest=digest,
        )

    # -- dispatch ----------------------------------------------------------
    def dispatch(
        self,
        silo: str,
        venue: str,
        order: Any,
        *,
        sandbox: bool = True,
        cost_floor: Any = None,
        now_ns: int | None = None,
        account_id: str | None = None,
        ratchet: bool = True,
        realized_net_micros: int | None = None,
    ) -> SandboxDispatchReceipt:
        """Reserve, sign, send, and resolve one sandbox order.

        The order is reserved in the crash-atomic outbox first; a fill commits it
        (and sweeps 35/15 inside the silo), while any terminal venue answer voids
        it.  ``HTTP 429`` / ``5xx`` is retried a bounded number of times before
        the void, so a transient testnet hiccup does not lose the reservation and
        a real rejection never leaves one behind.
        """

        started = time.perf_counter_ns()
        stamp = int(now_ns if now_ns is not None else self.clock())
        venue_key = str(venue).strip().lower()
        spec = SANDBOX_VENUE_SPECS.get(venue_key)
        if spec is None:
            raise ValueError(f"unsupported sandbox venue: {venue!r}")
        silo_id = str(silo).strip().upper()
        if silo_id != spec.silo:
            raise ValueError(
                f"venue {venue_key!r} belongs to silo {spec.silo!r}, not {silo_id!r}"
            )
        if not sandbox:
            raise ValueError(
                "TestnetExecutionBridge only dispatches sandbox/paper orders; "
                "live capital requires POST /api/v2/control/brokers (sec_016)"
            )

        market_silo = self._market_silo(silo_id)
        proposal = self._coerce_proposal(order, silo_id)
        if cost_floor is None:
            cost_floor = getattr(market_silo, "cost_floor", None)
            if cost_floor is None:
                from ring1_spinal.silo_worker import silo_cost_floor

                cost_floor = silo_cost_floor(silo_id)

        # ---- 1. two-phase reservation (before any socket exists) ----------
        reservation = self.outbox.reserve(
            proposal, now_ns=stamp, cost_floor=cost_floor
        )
        receipt = SandboxDispatchReceipt(
            venue=venue_key,
            silo=silo_id,
            environment=spec.environment,
            endpoint=spec.url(account_id=account_id or "SANDBOX-ACCOUNT"),
            method="POST",
            sandbox=True,
            accepted=False,
            committed=False,
            voided=False,
            http_status=None,
            attempts=0,
            signature_alg="none",
            signature_preview="",
            reservation_key=reservation.outbox_idempotency_key,
            outbox_state=str(reservation.state),
            pending_transfer_id=int(reservation.pending_transfer_id),
            dispatched_at_ns=stamp,
        )
        self.dispatches += 1

        def _finish() -> SandboxDispatchReceipt:
            receipt.latency_ms = (time.perf_counter_ns() - started) / 1e6
            self.last_receipt = receipt
            self._history.append(receipt)
            if len(self._history) > 256:
                del self._history[:-256]
            return receipt

        # ---- 2. credentials + venue-native payload ------------------------
        try:
            api_key, api_secret = self._require_credentials(venue_key)
        except SandboxCredentialMissing as exc:
            self.outbox.void(
                reservation.outbox_idempotency_key,
                reason="missing sandbox credential",
                now_ns=stamp,
            )
            self.voided += 1
            receipt.voided = True
            receipt.outbox_state = "VOIDED"
            receipt.error = str(exc)
            return _finish()

        from ring0_vault.silos.silo_registry import build_venue_payload

        payload, flags = build_venue_payload(
            venue_key, proposal, silo=market_silo
        )
        # Never send an idempotency-colliding client id twice.
        if "client_order_id" in payload and not payload["client_order_id"]:
            payload["client_order_id"] = reservation.outbox_idempotency_key[:32]

        account = (
            account_id
            or self._credential(venue_key, "account")
            or f"NIOS-{silo_id}-SANDBOX"
        )
        plan = self._sign(
            spec, payload, api_key, api_secret, account_id=account, now_ns=stamp
        )
        # The plaintext credentials are no longer needed: drop the references so
        # they cannot be captured by the rest of this frame's locals.
        del api_key
        del api_secret

        receipt.signature_alg = plan.signature_alg
        receipt.signature_preview = plan.signature_preview
        receipt.header_names = tuple(plan.header_names())
        receipt.masked_headers = plan.masked_headers()
        receipt.api_key_masked = mask_token(
            self._credential(venue_key, "api_key") or ""
        )
        receipt.body_digest = plan.body_digest
        receipt.endpoint = plan.url

        # ---- 3. bounded retry loop ---------------------------------------
        response: Any = None
        last_error = ""
        attempts = 0
        client = None
        try:
            client = self._http()
        except SandboxNetworkDisabled as exc:
            self.outbox.void(
                reservation.outbox_idempotency_key,
                reason="sandbox network disabled",
                now_ns=stamp,
            )
            self.voided += 1
            receipt.attempts = 0
            receipt.voided = True
            receipt.outbox_state = "VOIDED"
            receipt.error = str(exc)
            return _finish()

        for attempt in range(self.max_retries + 1):
            attempts = attempt + 1
            try:
                response = client.request(
                    plan.method,
                    plan.url,
                    headers=plan.headers,
                    params=plan.params or None,
                    json=plan.json_body,
                )
            except Exception as exc:  # noqa: BLE001 - transport failure
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    self.retries += 1
                    if self.retry_backoff_s:
                        time.sleep(self.retry_backoff_s)
                    continue
                break
            status = int(getattr(response, "status_code", 0) or 0)
            if status in RETRY_STATUS_CODES and attempt < self.max_retries:
                self.retries += 1
                last_error = f"HTTP {status} (retryable)"
                if self.retry_backoff_s:
                    time.sleep(self.retry_backoff_s)
                continue
            break

        receipt.attempts = attempts
        status = int(getattr(response, "status_code", 0) or 0) if response is not None else None
        receipt.http_status = status
        text = ""
        if response is not None:
            try:
                text = response.text
            except Exception:  # pragma: no cover - mock responses
                text = ""
            receipt.response_digest = hashlib.sha256(
                (text or "").encode("utf-8")
            ).hexdigest()

        # ---- 4. resolve: commit on 2xx, void on anything else ------------
        if response is not None and status is not None and 200 <= status < 300:
            broker_order_id = self._extract_order_id(text)
            realized = (
                int(realized_net_micros)
                if realized_net_micros is not None
                else self._realized_net_micros(proposal, reservation, cost_floor)
            )
            ratchet_engine = None
            if ratchet:
                from ring0_vault.ibor.ledger_outbox import InSiloProfitRatchet

                ratchet_engine = InSiloProfitRatchet(self.ledger)
            committed = self.outbox.commit(
                reservation.outbox_idempotency_key,
                broker_order_id=broker_order_id,
                now_ns=stamp,
                realized_net_micros=realized,
                ratchet=ratchet_engine,
            )
            receipt.accepted = True
            receipt.committed = True
            receipt.outbox_state = str(committed.state)
            receipt.broker_order_id = broker_order_id
            receipt.realized_net_micros = int(realized)
            if ratchet_engine is not None and realized > 0:
                receipt.ratchet = {
                    **ratchet_engine.split(realized),
                    "applied": True,
                    "silo": silo_id,
                    "cross_border_wire_bps": 0.0,
                    "long_term_pct": 35.0,
                    "reserve_pct": 15.0,
                }
            self.committed += 1
            receipt.reconcile = self._post_fill(
                proposal, reservation, committed, now_ns=stamp
            )
            return _finish()

        # Terminal rejection (4xx) or exhausted retries: void, never orphan.
        self.outbox.void(
            reservation.outbox_idempotency_key,
            reason=f"sandbox rejected: HTTP {status}" if status else "sandbox voided",
            now_ns=stamp,
        )
        self.voided += 1
        receipt.voided = True
        receipt.outbox_state = "VOIDED"
        receipt.error = last_error or (
            f"venue rejected the sandbox order with HTTP {status}"
            if status
            else "no response from sandbox venue"
        )
        return _finish()

    # -- post-fill accounting ---------------------------------------------
    def _realized_net_micros(
        self, proposal: UnsignedProposal, reservation: Any, cost_floor: Any
    ) -> int:
        notional = int(getattr(reservation, "notional_micros", 0) or 0)
        gross_bps = float(getattr(proposal, "expected_gross_bps", 0.0) or 0.0)
        spread_bps = float(getattr(cost_floor, "spread_bps", 0.0) or 0.0)
        fee_bps = 2.0 * float(getattr(cost_floor, "maker_fee_bps", 0.0) or 0.0)
        tax_bps = float(getattr(cost_floor, "statutory_tax_bps", 0.0) or 0.0)
        net_bps = gross_bps - spread_bps - fee_bps - tax_bps
        return int(notional * net_bps / 10_000.0)

    def _post_fill(
        self,
        proposal: UnsignedProposal,
        reservation: Any,
        committed: Any,
        *,
        now_ns: int,
    ) -> dict[str, Any] | None:
        """Accrue charges, book the position and reconcile the silo."""

        silo = str(proposal.silo)
        result: dict[str, Any] = {}
        try:
            charges = self.ledger.accrue_fill_charges(
                silo,
                notional_micros=int(getattr(committed, "notional_micros", 0) or 0),
                fee_bps=0.0,
                tax_bps=0.0,
                source_account=proposal.vault,
                symbol=str(proposal.symbol),
                side=str(proposal.side),
                qty_micros=int(getattr(committed, "qty_micros", 0) or 0),
                price_micros=int(getattr(committed, "price_micros", 0) or 0),
                now_ns=now_ns,
            )
            result["charges"] = {key: int(value) for key, value in charges.items()}
        except Exception as exc:  # pragma: no cover - accounting is best effort
            result["charges_error"] = f"{type(exc).__name__}: {exc}"

        try:
            position = self.ledger.set_position(
                silo,
                str(proposal.symbol),
                qty_micros=int(getattr(committed, "qty_micros", 0) or 0),
                price_micros=int(getattr(committed, "price_micros", 0) or 0),
            )
            result["position"] = {key: int(value) for key, value in position.items()}
        except Exception as exc:  # pragma: no cover
            result["position_error"] = f"{type(exc).__name__}: {exc}"

        if self.reconciler is not None:
            try:
                from ring0_vault.ibor.ledger_outbox import BrokerPosition

                self.reconciler.register_broker_position(
                    BrokerPosition(
                        silo=silo,
                        symbol=str(proposal.symbol),
                        qty_micros=int(getattr(committed, "qty_micros", 0) or 0),
                        price_micros=int(getattr(committed, "price_micros", 0) or 0),
                    )
                )
                report = self.reconciler.reconcile(silo, now_ns=now_ns)
                result["reconcile"] = (
                    report.to_dict() if hasattr(report, "to_dict") else {"ok": True}
                )
            except Exception as exc:  # pragma: no cover
                result["reconcile_error"] = f"{type(exc).__name__}: {exc}"
        return result or None

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _extract_order_id(body: str) -> str:
        """Pull a venue order id out of a sandbox response body, best effort."""

        if not body:
            return ""
        try:
            parsed = json.loads(body)
        except Exception:
            return ""
        candidates = (
            "orderId",
            "ordId",
            "order_id",
            "orderID",
            "id",
            "orderNo",
            "orderNumber",
            "orderRef",
            "clOrdId",
            "client_order_id",
        )

        def _walk(node: Any) -> str:
            if isinstance(node, Mapping):
                for name in candidates:
                    value = node.get(name)
                    if value not in (None, ""):
                        return str(value)
                for value in node.values():
                    found = _walk(value)
                    if found:
                        return found
            elif isinstance(node, list):
                for value in node:
                    found = _walk(value)
                    if found:
                        return found
            return ""

        return _walk(parsed)

    @staticmethod
    def _coerce_proposal(order: Any, silo: str) -> UnsignedProposal:
        from ring0_vault.silos.silo_registry import _coerce_proposal

        return _coerce_proposal(order, silo=silo)

    # -- introspection -----------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "kind": "ring0_testnet_execution_bridge",
            "ring": "ring0_vault",
            "allow_network": self.allow_network,
            "transport_injected": self.transport is not None or self._client is not None,
            "max_retries": self.max_retries,
            "venues": list(CLEAN_VENUES),
            "dispatches": self.dispatches,
            "committed": self.committed,
            "voided": self.voided,
            "retries": self.retries,
            "write_only_credentials": True,
            "last_receipt": (
                self.last_receipt.to_dict() if self.last_receipt is not None else None
            ),
            "targets": {
                venue: sandbox_target(spec.silo, venue)
                for venue, spec in SANDBOX_VENUE_SPECS.items()
            },
        }
