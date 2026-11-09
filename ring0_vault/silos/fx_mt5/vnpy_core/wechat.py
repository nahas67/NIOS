"""WeChat notification shim for the harvested ``vnpy_core`` tree.

``engine.py`` imports ``Credentials``, ``SessionExpired``, ``WeixinError`` and
``send_text`` from ``.wechat`` so the main engine can push trade alerts to a
WeChat group.  The NIOS harvest excluded every messaging integration on purpose
(``AGENTS.md`` section 8.2: "chat bots ... were excluded"), so this module keeps
the *contract* and disables delivery.

Nothing is ever sent.  :func:`send_text` raises :class:`WeixinError` unless a
caller explicitly opts in with ``NIOS_WECHAT_ENABLED=1`` **and** supplies a
webhook; even then it only posts to the operator-provided URL, never to a
hard-coded vendor endpoint.  Ring 0 must never leak order flow to a third party.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

__all__ = ["Credentials", "SessionExpired", "WeixinError", "send_text", "WechatBot"]

_LOG = logging.getLogger("nios.ring0.wechat")


class WeixinError(RuntimeError):
    """Raised when a WeChat notification cannot be delivered."""


class SessionExpired(WeixinError):
    """Raised when a cached WeChat session is no longer valid."""


@dataclass(slots=True)
class Credentials:
    """Notification credentials (disabled unless explicitly enabled)."""

    token: str = ""
    webhook: str = ""

    @property
    def enabled(self) -> bool:
        return bool(os.environ.get("NIOS_WECHAT_ENABLED", "0") == "1" and self.webhook)


def send_text(text: str, credentials: Credentials | None = None, **_: Any) -> bool:
    """Deliver ``text`` if (and only if) notifications are explicitly enabled.

    Returns ``True`` when delivered, ``False`` when the channel is disabled.
    Raises :class:`WeixinError` when delivery was requested but failed.
    """

    creds = credentials or Credentials()
    if not creds.enabled:
        _LOG.debug("WeChat delivery disabled; dropping notification (%d chars)", len(text or ""))
        return False

    try:
        from ring0_vault.silos.india_brokers._openalgo_compat import shared_http_client

        response = shared_http_client().post(creds.webhook, json={"text": text})
        response.raise_for_status()
        return True
    except Exception as exc:  # pragma: no cover - network path
        raise WeixinError(f"wechat delivery failed: {exc}") from exc


class WechatBot:
    """Minimal bot facade kept for ``MainEngine`` compatibility."""

    def __init__(self, credentials: Credentials | None = None) -> None:
        self.credentials = credentials or Credentials()

    def send_text(self, text: str) -> bool:
        return send_text(text, self.credentials)

    def send_trade(self, trade: Any) -> bool:
        return send_text(f"trade: {trade!r}", self.credentials)

    def send_order(self, order: Any) -> bool:
        return send_text(f"order: {order!r}", self.credentials)

    def send_log(self, log: Any) -> bool:
        return send_text(f"log: {log!r}", self.credentials)
