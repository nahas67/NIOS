"""V2.5 single-model dispatch policy for NIOS AI endpoints.

A V2.5 work order locks every AI endpoint (System 1 instinct, System 2
committee, cockpit probes and control surfaces) to one model,
``hy4-flash-free``.  This module is the single source of truth for that lock.

It is intentionally dependency-free and ring-neutral so it may be imported
from ``core``, ``ring2_brain`` and ``ring3_cockpit`` without pulling a broker
adapter, an HTTP client or a credential store into any air-gapped process.

Standard policy
---------------
* :data:`ALLOWED_MODEL` -- the only model id permitted on any dispatch path.
* :func:`is_allowed_model` -- non-raising predicate.
* :func:`enforce_model` -- raise :class:`ModelNotAllowed` for anything else.
* :func:`coerce_model` -- override any other id to :data:`ALLOWED_MODEL`.

The lock is *additive*: offline/deterministic defaults are left untouched by
callers that do not opt in.  Callers gate the policy on an explicit live-mode
flag (``NIOS_LLM_LIVE=1``, see :mod:`core.llm_api_config`) so unit tests and the
offline deterministic engine keep their existing behaviour verbatim.
"""

from __future__ import annotations

__all__ = [
    "ALLOWED_MODEL",
    "MODEL_NOT_ALLOWED",
    "ModelNotAllowed",
    "coerce_model",
    "enforce_model",
    "is_allowed_model",
]

#: The single active model id.  Never change without a V2.5 spec amendment.
ALLOWED_MODEL = "hy4-flash-free"

#: Stable error code surfaced to the cockpit / API callers.
MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"


class ModelNotAllowed(ValueError):
    """Raised when a dispatch requests a model other than :data:`ALLOWED_MODEL`.

    ``code`` is the stable machine-readable string ``MODEL_NOT_ALLOWED``; the
    human message names the rejected id only (never a key or endpoint).
    """

    code = MODEL_NOT_ALLOWED

    def __init__(self, model: object = "") -> None:
        self.model = str(model)
        super().__init__(
            f"{MODEL_NOT_ALLOWED}: model {self.model!r} is not permitted; "
            f"only {ALLOWED_MODEL!r} may be dispatched"
        )


def is_allowed_model(model: object) -> bool:
    """True when ``model`` is exactly the single allowed model id."""
    return str(model) == ALLOWED_MODEL


def enforce_model(model: object) -> str:
    """Return ``model`` when allowed, else raise :class:`ModelNotAllowed`."""
    token = str(model)
    if token != ALLOWED_MODEL:
        raise ModelNotAllowed(token)
    return token


def coerce_model(model: object = "") -> str:
    """Override any model id to :data:`ALLOWED_MODEL` (the V2.5 lock)."""
    return ALLOWED_MODEL
