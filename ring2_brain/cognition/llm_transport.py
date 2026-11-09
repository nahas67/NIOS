"""Ring 2 cognition re-export of the shared HTTP/2 LLM transport.

The transport itself lives in :mod:`core.llm_transport` because it is shared
substrate: Ring 2 cognition uses it for System 1 / System 2 turns and the Ring 3
control plane uses it for the live token-budget telemetry.  Keeping the socket
client out of ``ring2_brain`` preserves the Step 4 air-gap audit while the
cognition modules still import the capability under their own namespace.

No broker credential, exchange socket or Ring 0 code is reachable from here.
"""

from __future__ import annotations

from core.llm_transport import (  # noqa: F401
    BASELINE_USD_PER_DAY,
    DEFAULT_DAILY_USD_CAP,
    HARD_TIMEOUT_MS,
    LOCAL_PROVIDERS,
    PAID_PROVIDERS,
    SYSTEM1_PROVIDERS,
    SYSTEM2_PROVIDERS,
    BudgetExceeded,
    LLMCompletion,
    LLMHttp2TransportPool,
    LLMProviderSpec,
    ProviderDemoted,
    ProviderTimeout,
    ProviderUnavailable,
    TokenBudgetLedger,
    default_provider_specs,
    parse_instinct_response,
    reasoning_budget_tokens,
)
from core.llm_transport import __all__ as __all__

__all__ = list(__all__)
