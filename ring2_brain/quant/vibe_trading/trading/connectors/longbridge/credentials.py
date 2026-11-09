"""Longbridge credential resolver stub.

Credentials belong to Ring 0's encrypted key vault.  The Ring 2 loader only
needs the import surface; resolving a credential here fails loudly.
"""

from __future__ import annotations

__all__ = ["LongbridgeCredentialError", "resolve_longbridge_credentials"]


class LongbridgeCredentialError(RuntimeError):
    pass


def resolve_longbridge_credentials(*args: object, **kwargs: object) -> dict[str, str]:
    raise LongbridgeCredentialError(
        "Longbridge credentials are held by the Ring 0 key vault; Ring 2 cannot resolve them"
    )
