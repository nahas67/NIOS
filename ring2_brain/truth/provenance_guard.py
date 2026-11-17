"""NIOS Core v3.0 -- Evidence Provenance Guard (V1.9 ``F1`` + ``F3``).

Why this gate exists
--------------------
``F1`` (provenance): a number is only admissible if the system can name the feed
it came from and prove that feed was actually queried.  A ``GroundedFactPacket``
therefore carries ``source_id`` and ``retrieval_attestation``; a packet with no
named source is indistinguishable from a model's invention, so it is rejected
before it can reach a thesis (``sec_006`` Stage 4, ``sec_018`` Law 3).

``F3`` (freshness): a packet also carries ``not_stale_before_utc``, the oldest
``knowledge_ts_ns`` that is still admissible.  Freshness is decided by the
packet's own committed bound, never by how recently the guard happened to run,
so the same packet verifies identically on a replay, a backtest and a live tick.

Gate order
----------
All four checks are always evaluated -- the digest is never skipped because a
source looked suspicious -- so a rejection reports every defect at once instead
of hiding a tampered payload behind an allowlist miss.  The verdict's ``code``
is the *first* failing check in the fixed order digest -> source -> attestation
-> freshness, which keeps the operator-facing reason stable.

This module performs no I/O, opens no socket, holds no credential and imports
no broker adapter: it is pure Ring 2 contract logic (``sec_004``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from core.schemas import GroundedFactPacket, NiosError

__all__ = [
    "PROVENANCE_OK",
    "SOURCE_NOT_ALLOWLISTED",
    "ATTESTATION_MISSING",
    "STALE_EVIDENCE",
    "DIGEST_INVALID",
    "PROVENANCE_INCOMPLETE",
    "DEFAULT_SOURCE_ALLOWLIST",
    "ProvenanceError",
    "ProvenanceVerdict",
    "SourceAttestation",
    "EvidenceProvenanceGuard",
    "verify_evidence_provenance",
]

#: Verdict code: every check passed.
PROVENANCE_OK = "PROVENANCE_OK"
#: Verdict code: ``source_id`` is not permitted for the requested silo.
SOURCE_NOT_ALLOWLISTED = "SOURCE_NOT_ALLOWLISTED"
#: Verdict code: ``retrieval_attestation`` is absent or not a SHA-256 length.
ATTESTATION_MISSING = "RETRIEVAL_ATTESTATION_MISSING"
#: Verdict code: ``knowledge_ts_ns`` predates ``not_stale_before_utc`` (``F3``).
STALE_EVIDENCE = "STALE_EVIDENCE"
#: Verdict code: the packet's canonical SHA-256 no longer matches its payload.
DIGEST_INVALID = "DIGEST_INVALID"
#: Verdict code: the packet names no source at all (``F1``).
PROVENANCE_INCOMPLETE = "PROVENANCE_INCOMPLETE"

#: Default allowlist: silo -> tuple of source ids permitted for that silo.
#:
#: Each silo sees only the feeds that can legitimately price its instruments:
#: an Indian cash-equity packet must not be "confirmed" by a Binance ticker, and
#: a crypto packet must not cite a US filing.  ``exchange_calendar`` and
#: ``internal_valuation`` are cross-silo because a trading calendar and our own
#: DCF/Comps output are meaningful in every market.  Treat this mapping as
#: read-only: :class:`EvidenceProvenanceGuard` copies it at construction.
DEFAULT_SOURCE_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "INDIA_ZERODHA_DHAN": (
        "nse_bhavcopy",
        "rbi_reference",
        "exchange_calendar",
        "internal_valuation",
    ),
    "CRYPTO_BINANCE": (
        "binance_rest",
        "exchange_calendar",
        "internal_valuation",
    ),
    "US_IBKR": (
        "sec_edgar",
        "exchange_calendar",
        "internal_valuation",
    ),
    "FX_MT5": (
        "oanda_rates",
        "rbi_reference",
        "exchange_calendar",
        "internal_valuation",
    ),
}


class ProvenanceError(NiosError):
    """Raised by :meth:`EvidenceProvenanceGuard.enforce` on a failed verdict.

    Carries the machine-readable :attr:`code` (one of the module constants) so
    a caller can branch without parsing the message, and is a
    :class:`core.schemas.NiosError` so Ring 2's existing handlers catch it.
    """

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(slots=True, frozen=True)
class ProvenanceVerdict:
    """Immutable outcome of one provenance check (``F1`` + ``F3``).

    ``reasons`` is empty on success and otherwise lists every failed check in
    gate order, so an operator sees all defects rather than only the first.
    """

    ok: bool
    code: str
    symbol: str
    source_id: str
    reasons: tuple[str, ...]
    freshness_ok: bool
    attestation_ok: bool
    digest_ok: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view (``reasons`` becomes a list)."""
        return {
            "ok": self.ok,
            "code": self.code,
            "symbol": self.symbol,
            "source_id": self.source_id,
            "reasons": list(self.reasons),
            "freshness_ok": self.freshness_ok,
            "attestation_ok": self.attestation_ok,
            "digest_ok": self.digest_ok,
        }


@dataclass(slots=True, frozen=True)
class SourceAttestation:
    """Registry record for one retrieval source (``F1``).

    ``silos`` lists the silos the source was registered for; ``allowlisted``
    reports whether the source currently appears in *any* silo allowlist, so a
    metadata-only registration is visibly not a grant of trust.
    """

    source_id: str
    domain: str
    silos: tuple[str, ...]
    allowlisted: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", str(self.source_id).strip().lower())
        object.__setattr__(self, "domain", str(self.domain).strip())
        object.__setattr__(
            self,
            "silos",
            tuple(
                sorted({str(silo).strip().upper() for silo in self.silos if str(silo).strip()})
            ),
        )
        object.__setattr__(self, "allowlisted", bool(self.allowlisted))


class EvidenceProvenanceGuard:
    """Deny-by-default provenance gate for :class:`GroundedFactPacket` inputs.

    The guard owns a silo -> source-id allowlist and a registry of source
    attestations.  :meth:`verify` never raises for a bad packet (it returns a
    :class:`ProvenanceVerdict`); :meth:`enforce` raises
    :class:`ProvenanceError` for callers that treat a rejection as fatal.

    Freshness is judged from the packet's committed ``not_stale_before_utc``
    bound only.  ``now_ns`` is accepted for future wall-clock checks and is
    currently inert: passing it can never flip a verdict, which is what keeps
    replay and live behaviour identical (``F3``).
    """

    def __init__(
        self,
        *,
        allowlist: Mapping[str, Iterable[str]] | None = None,
        require_attestation: bool = True,
    ) -> None:
        source_map: Mapping[str, Iterable[str]] = (
            DEFAULT_SOURCE_ALLOWLIST if allowlist is None else allowlist
        )
        normalized: dict[str, tuple[str, ...]] = {}
        for raw_silo, raw_ids in source_map.items():
            silo = str(raw_silo).strip().upper()
            if not silo:
                raise ValueError("allowlist silo names must be non-empty")
            normalized[silo] = tuple(
                sorted({str(source).strip().lower() for source in raw_ids if str(source).strip()})
            )
        self._allowlist: dict[str, tuple[str, ...]] = normalized
        self._require_attestation: bool = bool(require_attestation)
        self._sources: dict[str, SourceAttestation] = {}
        self._checks: int = 0
        self._failures: int = 0

    # -- registry ---------------------------------------------------------
    def register_source(
        self,
        source_id: str,
        *,
        domain: str = "",
        silos: Iterable[str] = (),
    ) -> SourceAttestation:
        """Register (or refresh) a retrieval source.

        Naming ``silos`` grants the source membership in those silos' allowlists,
        which is the only way a source outside
        :data:`DEFAULT_SOURCE_ALLOWLIST` becomes usable.  Registering with no
        silos records metadata without granting trust, so an operator can
        inventory a candidate feed before enabling it.  Re-registering the same
        id is idempotent for the allowlist and refreshes the metadata.
        """
        key = str(source_id).strip().lower()
        if not key:
            raise ValueError("source_id must be a non-empty string")

        silo_names = tuple(
            sorted({str(silo).strip().upper() for silo in silos if str(silo).strip()})
        )
        for silo in silo_names:
            granted = set(self._allowlist.get(silo, ()))
            granted.add(key)
            self._allowlist[silo] = tuple(sorted(granted))

        attestation = SourceAttestation(
            source_id=key,
            domain=str(domain).strip(),
            silos=silo_names,
            allowlisted=self._is_allowlisted_anywhere(key),
        )
        self._sources[key] = attestation
        return attestation

    def sources(self) -> tuple[SourceAttestation, ...]:
        """Return every registered attestation, ordered by ``source_id``."""
        return tuple(self._sources[key] for key in sorted(self._sources))

    # -- verification -----------------------------------------------------
    def verify(
        self,
        packet: GroundedFactPacket,
        *,
        silo: str | None = None,
        now_ns: int | None = None,
    ) -> ProvenanceVerdict:
        """Evaluate the four provenance checks without raising.

        Order is digest -> source -> attestation -> freshness and every check is
        evaluated, so a tampered payload is always reported as such.  With
        ``silo=None`` the source only has to appear in some silo's allowlist; an
        unknown silo permits nothing (deny-by-default).

        ``now_ns`` is validated as an integer and otherwise unused: freshness
        comes from the packet's own ``not_stale_before_utc`` bound, so the value
        never changes the verdict.
        """
        if now_ns is not None:
            # Reserved for a future wall-clock staleness check.  Coerced only to
            # reject nonsense; deliberately not consulted below (``F3``).
            int(now_ns)

        self._checks += 1

        symbol = str(getattr(packet, "symbol", ""))
        raw_source = str(getattr(packet, "source_id", "") or "").strip()
        source_id = raw_source.lower()
        reasons: list[str] = []

        digest_ok = bool(packet.verify())
        if not digest_ok:
            reasons.append(
                f"sha256 digest {getattr(packet, 'sha256_digest', '')!r} does not match the "
                "canonical packet payload"
            )

        source_ok, source_code, source_reason = self._check_source(source_id, silo)
        if not source_ok:
            reasons.append(source_reason)

        attestation = str(getattr(packet, "retrieval_attestation", "") or "").strip()
        if self._require_attestation:
            attestation_ok = len(attestation) == 64
        else:
            attestation_ok = True
        if not attestation_ok:
            reasons.append(
                "retrieval_attestation must be a 64-char SHA-256 digest, got "
                f"length {len(attestation)}"
            )

        freshness_ok = bool(packet.is_fresh())
        if not freshness_ok:
            reasons.append(
                f"knowledge_ts_ns {getattr(packet, 'knowledge_ts_ns', 0)} predates "
                f"not_stale_before_utc {getattr(packet, 'not_stale_before_utc', 0)}"
            )

        ok = digest_ok and source_ok and attestation_ok and freshness_ok
        if ok:
            code = PROVENANCE_OK
        elif not digest_ok:
            code = DIGEST_INVALID
        elif not source_ok:
            code = source_code
        elif not attestation_ok:
            code = ATTESTATION_MISSING
        else:
            code = STALE_EVIDENCE

        if not ok:
            self._failures += 1

        return ProvenanceVerdict(
            ok=ok,
            code=code,
            symbol=symbol,
            source_id=source_id,
            reasons=tuple(reasons),
            freshness_ok=freshness_ok,
            attestation_ok=attestation_ok,
            digest_ok=digest_ok,
        )

    def enforce(
        self,
        packet: GroundedFactPacket,
        *,
        silo: str | None = None,
        now_ns: int | None = None,
    ) -> ProvenanceVerdict:
        """Verify and raise :class:`ProvenanceError` on any failure."""
        verdict = self.verify(packet, silo=silo, now_ns=now_ns)
        if not verdict.ok:
            raise ProvenanceError(verdict.code, f"{verdict.code}: {verdict.symbol}")
        return verdict

    def verify_batch(
        self,
        packets: Iterable[GroundedFactPacket],
        *,
        silo: str | None = None,
        now_ns: int | None = None,
    ) -> tuple[ProvenanceVerdict, ...]:
        """Verify every packet, preserving input order and counting each check."""
        return tuple(
            self.verify(packet, silo=silo, now_ns=now_ns) for packet in packets
        )

    # -- observability ----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return the guard's configuration, registry and running counters.

        ``checks`` counts :meth:`verify` calls (so a batch of three adds three)
        and ``failures`` counts the subset that returned ``ok=False``.
        """
        return {
            "require_attestation": self._require_attestation,
            "allowlist": {
                silo: list(ids) for silo, ids in sorted(self._allowlist.items())
            },
            "sources": [
                {
                    "source_id": attestation.source_id,
                    "domain": attestation.domain,
                    "silos": list(attestation.silos),
                    "allowlisted": attestation.allowlisted,
                }
                for attestation in self.sources()
            ],
            "checks": self._checks,
            "failures": self._failures,
        }

    # -- internals --------------------------------------------------------
    def _check_source(
        self, source_id: str, silo: str | None
    ) -> tuple[bool, str, str]:
        """Return ``(ok, code, reason)`` for the ``F1`` source check."""
        if not source_id:
            return (
                False,
                PROVENANCE_INCOMPLETE,
                "packet carries no source_id: an unattributed number is "
                "indistinguishable from a fabricated one",
            )
        if silo is None:
            permitted = self._is_allowlisted_anywhere(source_id)
            scope = "any silo allowlist"
        else:
            silo_key = str(silo).strip().upper()
            permitted = source_id in self._allowlist.get(silo_key, ())
            scope = f"silo {silo_key or '<empty>'}"
        if not permitted:
            return (
                False,
                SOURCE_NOT_ALLOWLISTED,
                f"source_id {source_id!r} is not on the {scope} allowlist",
            )
        return (True, PROVENANCE_OK, "")

    def _is_allowlisted_anywhere(self, source_id: str) -> bool:
        return any(source_id in ids for ids in self._allowlist.values())


def verify_evidence_provenance(
    packet: GroundedFactPacket,
    *,
    silo: str | None = None,
    allowlist: Mapping[str, Iterable[str]] | None = None,
    now_ns: int | None = None,
) -> ProvenanceVerdict:
    """One-shot provenance check for a single packet.

    Builds a throwaway :class:`EvidenceProvenanceGuard` (attestation required,
    default allowlist unless ``allowlist`` is supplied), so a caller that only
    needs a verdict does not have to own a guard instance.
    """
    guard = EvidenceProvenanceGuard(allowlist=allowlist)
    return guard.verify(packet, silo=silo, now_ns=now_ns)

# ---------------------------------------------------------------------------
# V1.9 (Gate 23) -- prompt-injection guard for retrieved evidence
# ---------------------------------------------------------------------------
#
# Evidence is data, never instructions.  A filing, a news snippet or a scraped
# page that tells the model to ignore its rules, reveal a key or move money is
# a prompt-injection attempt; the gate must catch it before the text reaches a
# reasoning agent.

INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", "IGNORE_INSTRUCTIONS"),
    (r"disregard\s+(all\s+)?(previous|prior|system)\s+", "DISREGARD_INSTRUCTIONS"),
    (r"you\s+are\s+now\s+(a|an|the)\s+", "ROLE_OVERRIDE"),
    (r"system\s*prompt", "SYSTEM_PROMPT_PROBE"),
    (r"reveal\s+(your\s+)?(api\s*key|secret|password|token)", "SECRET_EXFILTRATION"),
    (r"(wire|transfer|send)\s+(funds|money|usd|btc)", "FUNDS_TRANSFER"),
    (r"rm\s+-rf\s+/", "DESTRUCTIVE_COMMAND"),
    (r"curl\s+https?://", "OUTBOUND_FETCH"),
    (r"execute\s+the\s+following\s+(command|code)", "CODE_EXECUTION"),
)


@dataclass(slots=True, frozen=True)
class InjectionVerdict:
    detected: bool
    severity: str
    matches: tuple[str, ...]
    text_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "severity": self.severity,
            "matches": list(self.matches),
            "text_sha256": self.text_sha256,
        }


def detect_prompt_injection(text: str) -> InjectionVerdict:
    """Scan retrieved text for instruction-shaped payloads.

    ``severity`` is ``NONE``, ``SUSPICIOUS`` (one pattern) or ``MALICIOUS``
    (two or more, or any secret-exfiltration / funds-transfer match).
    """
    import hashlib as _hashlib
    import re as _re

    payload = str(text or "")
    matches: list[str] = []
    for pattern, label in INJECTION_PATTERNS:
        if _re.search(pattern, payload, flags=_re.IGNORECASE):
            matches.append(label)
    unique = tuple(sorted(set(matches)))
    critical = {"SECRET_EXFILTRATION", "FUNDS_TRANSFER", "DESTRUCTIVE_COMMAND"}
    if not unique:
        severity = "NONE"
    elif len(unique) >= 2 or critical.intersection(unique):
        severity = "MALICIOUS"
    else:
        severity = "SUSPICIOUS"
    return InjectionVerdict(
        detected=bool(unique),
        severity=severity,
        matches=unique,
        text_sha256=_hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
