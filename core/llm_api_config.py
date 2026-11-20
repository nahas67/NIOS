"""V2.5 live LLM API configuration loader (secret-safe, single-model).

This is the *only* place that reads the operator's API-key file.  The raw key
is never logged, printed, embedded in a repr, or persisted anywhere; it lives
for the lifetime of an :class:`LLMApiConfig` instance and is used solely to
build the outbound ``Authorization`` header when live execution is explicitly
enabled (``NIOS_LLM_LIVE=1`` or :func:`set_live_enabled`).

The loader accepts free-form operator prose (a key token and an advisory
"use this model -- <model-id>" line) and extracts the longest key-like token
(``[A-Za-z0-9_-]{20,}``).  The model id in the prose is advisory only: the
V2.5 lock always forces ``hy4-flash-free`` via :mod:`core.llm_model_policy`.

Air-gap note
------------
No cryptographic or credential-store import is required here.  Masking uses a
local helper with the exact same semantics as
``ring0_vault.silos.key_vault.llm_credentials.mask_secret`` (``***`` + last
four characters), so this module stays ring-neutral and can be imported by
``core.llm_transport`` and the Ring 3 control plane without dragging Ring 0
code into their address space.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from core.llm_model_policy import ALLOWED_MODEL, coerce_model

__all__ = [
    "DEFAULT_LLM_API_FILE",
    "LIVE_ENV",
    "LLMApiConfig",
    "config_payload",
    "is_live_enabled",
    "load_llm_api_config",
    "mask_api_key",
    "set_live_enabled",
]

#: The operator-managed key file.  Contains prose, not a structured config.
DEFAULT_LLM_API_FILE = (
    r"C:\Users\nahas\OneDrive\Desktop\opencode-g0-api.txt"
)
#: Environment variable that explicitly enables live LLM execution.
LIVE_ENV = "NIOS_LLM_LIVE"
#: Optional base-URL overrides.
API_FILE_ENV = "NIOS_LLM_API_FILE"
BASE_URL_ENV = "NIOS_LLM_BASE_URL"

#: Longest key-like token in free-form prose (API keys, session tokens, ...).
_KEY_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,}")
#: An ``http(s)://`` URL, if the operator pasted one alongside the key.
_URL = re.compile(r"https?://[^\s\"'<>]+")
#: Advisory model hint in prose ("use this model -- hy4-flash-free").
_MODEL_HINT = re.compile(
    r"(?:model|use\s+this\s+model)\s*[:=-]{1,3}\s*([A-Za-z0-9_.\-/]+)",
    re.IGNORECASE,
)

_MASK_PREFIX = "***"
_MASK_VISIBLE_CHARS = 4

_runtime_live = False


def mask_api_key(secret: str) -> str:
    """Return ``***`` + last four characters (matches Ring 0 ``mask_secret``).

    Short secrets (<= 4 chars) are fully masked so they cannot be recovered.
    """
    token = str(secret or "")
    if len(token) <= _MASK_VISIBLE_CHARS:
        return _MASK_PREFIX
    return f"{_MASK_PREFIX}{token[-_MASK_VISIBLE_CHARS:]}"


def is_live_enabled() -> bool:
    """True only when live LLM execution was explicitly enabled.

    ``NIOS_LLM_LIVE=1`` (case-insensitive ``"1"``/``"true"``/``"yes"``) or a
    runtime flag set with :func:`set_live_enabled`.  Offline default is ``False``.
    """
    env = str(os.environ.get(LIVE_ENV, "")).strip().lower()
    env_on = env in {"1", "true", "yes", "on"}
    return bool(env_on or _runtime_live)


def set_live_enabled(enabled: bool) -> None:
    """Set the process-wide runtime live flag (for controller wiring/tests)."""
    global _runtime_live  # noqa: PLW0603 - explicit module-level enable switch
    _runtime_live = bool(enabled)


@dataclass(frozen=True, slots=True, repr=False)
class LLMApiConfig:
    """Resolved live LLM configuration.

    ``api_key`` is the raw secret and MUST NOT be logged or serialised.  Every
    public representation (:meth:`__repr__`, :meth:`__str__`, :meth:`to_dict`)
    is redacted.
    """

    api_key: str
    model: str
    base_url: str | None
    source_path: str
    declared_model: str | None = None
    api_key_found: bool = False

    @property
    def masked_key(self) -> str:
        """Write-only token for cockpit/telemetry payloads."""
        return mask_api_key(self.api_key)

    @property
    def model_hint_allowed(self) -> bool:
        """Whether the prose-declared model already matches the V2.5 lock."""
        return self.declared_model is None or str(self.declared_model) == ALLOWED_MODEL

    def to_dict(self) -> dict[str, object]:
        """Secret-safe mapping: masked key only, never the raw value."""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "source_path": self.source_path,
            "api_key_masked": self.masked_key,
            "api_key_found": bool(self.api_key_found),
            "declared_model": self.declared_model,
            "model_hint_allowed": self.model_hint_allowed,
        }

    def __repr__(self) -> str:  # pragma: no cover - exercised via tests
        return (
            f"LLMApiConfig(model={self.model!r}, base_url={self.base_url!r}, "
            f"source_path={self.source_path!r}, api_key={self.masked_key!r}, "
            f"api_key_found={self.api_key_found!r})"
        )

    __str__ = __repr__


def _resolve_path(file_path: str | None) -> str:
    if file_path:
        return str(file_path)
    env_path = str(os.environ.get(API_FILE_ENV, "")).strip()
    return env_path or DEFAULT_LLM_API_FILE


def load_llm_api_config(file_path: str | None = None) -> LLMApiConfig:
    """Load and parse the operator key file into a redacted config object.

    Path resolution: explicit ``file_path`` -> ``NIOS_LLM_API_FILE`` ->
    :data:`DEFAULT_LLM_API_FILE`.  The longest key-like token becomes the
    api key; the model id is always coerced to :data:`ALLOWED_MODEL`.
    """
    path = _resolve_path(file_path)
    text = Path(path).read_text(encoding="utf-8", errors="replace")

    tokens = _KEY_TOKEN.findall(text)
    key = max(tokens, key=len) if tokens else ""

    declared: str | None = None
    hint = _MODEL_HINT.search(text)
    if hint is not None:
        declared = hint.group(1).strip()

    # The V2.5 single-model lock overrides any advisory model id in the prose.
    model = coerce_model(declared if declared else ALLOWED_MODEL)

    base_url: str | None = None
    env_url = str(os.environ.get(BASE_URL_ENV, "")).strip()
    if env_url:
        base_url = env_url.rstrip("/")
    else:
        url_match = _URL.search(text)
        if url_match is not None:
            base_url = url_match.group(0).rstrip("/.,;)")

    return LLMApiConfig(
        api_key=key,
        model=model,
        base_url=base_url,
        source_path=path,
        declared_model=declared,
        api_key_found=bool(key),
    )


def config_payload(config: LLMApiConfig | None) -> dict[str, object] | None:
    """Return a secret-safe mapping suitable for a probe/API response."""
    return config.to_dict() if config is not None else None
