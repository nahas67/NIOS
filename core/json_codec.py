"""Shared JSON codec: orjson on the wire, byte-exact canonical form for hashing.

The gateway serialises three very different things, and they need three
different guarantees:

``canonical_bytes`` / ``canonical_json``  **hash contract**
    ``payload_sha256`` is a *wire contract*: a stored proposal, a cockpit
    payload and a client re-verification must all produce the same 64 hex
    characters forever.  So this path is deliberately **stdlib-only** with the
    exact flags it has always used (``sort_keys``, ``(",", ":")``,
    ``ensure_ascii=True``, ``allow_nan=False``, ``default=str``).

    orjson cannot reproduce those bytes, and pretending otherwise would
    silently re-seal every historical digest.  Two verified divergences:

    * ``ensure_ascii=True`` escapes non-ASCII to ``\\uXXXX``; orjson always
      emits UTF-8.
    * orjson raises ``TypeError: Integer exceeds 64-bit range`` on the 128-bit
      integer micro-units ``sec_009`` requires, and prints ``1e-9`` where
      stdlib prints ``1e-09``.

``dumps_bytes`` / ``dumps``  **wire contract**
    Same *values*, not necessarily the same bytes.  Keys are sorted (matching
    the canonical form), non-ASCII is emitted as UTF-8, and ``default=str``
    keeps the historical rendering of anything JSON cannot represent.  When
    orjson is installed the encode runs on the C fast path; when it is absent,
    or when the payload holds something orjson refuses (128-bit integers,
    sets, custom scalars), the exact stdlib encoder answers instead.

    A JSON body is equivalent to a client either way: parsing it yields the
    same Python values, and re-running ``canonical_bytes`` on the parsed body
    reproduces the digest that was computed before serialisation.

``loads``
    ``orjson.loads`` when available (it accepts ``str``/``bytes``), stdlib
    otherwise.  Both raise ``ValueError`` subclasses on bad input.

No new dependency is required: ``orjson`` is optional and this module is the
only place that imports it.
"""

from __future__ import annotations

import datetime as _datetime
import json
from typing import Any

__all__ = [
    "CANONICAL_ENSURE_ASCII",
    "ORJSON_AVAILABLE",
    "canonical_bytes",
    "canonical_json",
    "dumps",
    "dumps_bytes",
    "loads",
]

try:  # pragma: no cover - both branches are covered by test_json_codec
    import orjson
except Exception:  # pragma: no cover - a broken install must not break Ring 3
    orjson = None  # type: ignore[assignment]
    ORJSON_AVAILABLE = False
else:  # pragma: no cover - trivial
    ORJSON_AVAILABLE = True

#: The canonical form escapes non-ASCII.  It is part of the hash contract and
#: must never be flipped: doing so changes every existing ``payload_sha256``.
CANONICAL_ENSURE_ASCII = True

_CANONICAL_SEPARATORS = (",", ":")

#: orjson option set that mirrors ``sort_keys=True`` + ``default=str``.
#: ``OPT_PASSTHROUGH_DATETIME`` is what makes ``default=str`` reach datetimes:
#: without it orjson renders ISO-8601 itself and diverges from ``str(dt)``.
_ORJSON_SORTED = orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS if orjson else 0
_ORJSON_DEFAULT_STR = (
    _ORJSON_SORTED | orjson.OPT_PASSTHROUGH_DATETIME if orjson else 0
)


def _iso_default(value: Any) -> str:
    """``default=`` hook matching FastAPI's ``jsonable_encoder`` datetime rule."""
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    return str(value)


def canonical_json(obj: Any) -> str:
    """Canonical JSON text: sorted, compact, ASCII-escaped, NaN-rejecting.

    Byte-for-byte identical to the pre-V2.4
    :func:`ring3_cockpit.gateway.payload_envelope.canonical_json`.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=_CANONICAL_SEPARATORS,
        ensure_ascii=CANONICAL_ENSURE_ASCII,
        allow_nan=False,
        default=str,
    )


def canonical_bytes(obj: Any) -> bytes:
    """UTF-8 bytes of :func:`canonical_json` -- the ``payload_sha256`` input."""
    return canonical_json(obj).encode("utf-8")


def _stdlib_bytes(obj: Any, *, ensure_ascii: bool, iso_datetime: bool) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=_CANONICAL_SEPARATORS,
        ensure_ascii=bool(ensure_ascii),
        allow_nan=False,
        default=_iso_default if iso_datetime else str,
    ).encode("utf-8")


def dumps_bytes(
    obj: Any,
    *,
    ensure_ascii: bool = False,
    iso_datetime: bool = False,
) -> bytes:
    """Serialise ``obj`` to UTF-8 JSON bytes on the fastest safe path.

    ``iso_datetime=False`` (default) matches ``json.dumps(..., default=str)``:
    a datetime becomes ``str(datetime)``.  ``iso_datetime=True`` matches
    FastAPI's ``JSONResponse`` (``datetime.isoformat()``) and is therefore the
    drop-in replacement for a handler that used to return a plain ``dict``.
    """

    if orjson is not None and not ensure_ascii:
        option = _ORJSON_DEFAULT_STR if not iso_datetime else _ORJSON_SORTED
        try:
            return orjson.dumps(obj, option=option, default=str)
        except (TypeError, ValueError):
            # 128-bit integers, sets, exotic scalars: the exact stdlib encoder
            # is authoritative, so fall back rather than 500 the request.
            pass
    return _stdlib_bytes(obj, ensure_ascii=ensure_ascii, iso_datetime=iso_datetime)


def dumps(obj: Any, *, ensure_ascii: bool = False, iso_datetime: bool = False) -> str:
    """``str`` form of :func:`dumps_bytes`."""
    return dumps_bytes(
        obj, ensure_ascii=ensure_ascii, iso_datetime=iso_datetime
    ).decode("utf-8")


def loads(data: Any) -> Any:
    """Parse JSON ``str``/``bytes`` with whichever decoder is installed."""
    if orjson is not None and isinstance(data, (str, bytes, bytearray, memoryview)):
        return orjson.loads(data)
    return json.loads(data)
