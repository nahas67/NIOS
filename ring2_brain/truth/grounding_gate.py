"""NIOS Core v3.0 -- SHA-256 Numerical Grounding Gate (``sec_006`` Stage 4).

The zero-hallucination mandate
------------------------------
Every number a System 2 LLM is allowed to say out loud must already exist in an
immutable :class:`core.schemas.GroundedFactPacket`.  The packet carries a
canonical SHA-256 digest over its header fields *and* every metric, so a single
mutated digit invalidates the commitment.  Before a thesis can reach Ring 0, an
AST + regex verifier re-derives every cited figure against that packet and
raises ``UNGROUNDED-NUMERICAL-CLAIM`` on the first fabricated, unhashed or
arithmetically wrong number (``sec_002``, ``sec_006``, ``sec_010``, ``sec_018``).

Donor bridge
------------
``ring2_brain/truth/vibe_trading/grounding/ledger.py`` is a run-scoped identity
state machine coupled to the donor's agent loop (``src.agent.*``); it cannot be
imported in this repository.  Rather than pretending otherwise, this module
records the donor's exact status via :func:`donor_grounding_status` and
re-implements its *commitment contract* -- the immutable figure map with a
per-fact SHA-256 seal -- on top of the Step 2 wire contracts.  The donor
``trading_r1/verification`` re-computation pass is mirrored by
:class:`AstRecomputer`, which evaluates arithmetic expressions with a strictly
whitelisted AST (no names, calls, attributes or comprehensions).
"""

from __future__ import annotations

import ast
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.schemas import (
    GroundedFactPacket,
    GroundingDigestError,
    UngroundedNumericalClaim,
    canonical_pairs_digest,
    net_lower_bound_bps,
)

__all__ = [
    "UNGROUNDED_CLAIM_CODE",
    "UngroundedNumericalClaimError",
    "donor_grounding_status",
    "GroundingLedger",
    "LedgerEntry",
    "NumericalGroundingVerifier",
    "AstRecomputer",
    "VerificationReport",
    "Claim",
    "verify_thesis_claims",
]

#: The exact token Ring 0 matches when aborting order signing.
UNGROUNDED_CLAIM_CODE = "UNGROUNDED-NUMERICAL-CLAIM"

#: The user-facing alias requested by the Step 3 contract.
UngroundedNumericalClaimError = UngroundedNumericalClaim


def donor_grounding_status() -> dict[str, Any]:
    """Report whether the harvested grounding ledger can be imported here."""
    candidates = {
        "ledger": Path(__file__).resolve().parent
        / "vibe_trading"
        / "grounding"
        / "ledger.py",
        "figures": Path(__file__).resolve().parent
        / "vibe_trading"
        / "grounding"
        / "figures.py",
    }
    report: dict[str, Any] = {"modules": {}, "available": False}
    import importlib

    for name, path in candidates.items():
        entry = {"path": str(path), "harvested": path.exists(), "importable": False, "reason": ""}
        if path.exists():
            try:
                importlib.import_module(f"ring2_brain.truth.vibe_trading.grounding.{name}")
                entry["importable"] = True
            except Exception as exc:  # pragma: no cover - donor vendoring
                entry["reason"] = f"{type(exc).__name__}: {exc}"
        else:
            entry["reason"] = "not harvested"
        report["modules"][name] = entry
    report["available"] = any(m["importable"] for m in report["modules"].values())
    return report


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LedgerEntry:
    """One committed packet plus the wall-clock instant it was sealed."""

    digest: str
    symbol: str
    committed_at_ns: int
    metric_count: int
    as_of_ns: int


class GroundingLedger:
    """Immutable append-only registry of SHA-256 committed numeric facts.

    The ledger never mutates a packet.  Re-committing the same symbol/day
    produces a *new* entry (append-only), which keeps a walk-forward replay
    bit-for-bit reproducible.
    """

    def __init__(self, feature_store: Any | None = None, *, persist: bool = True) -> None:
        self.feature_store = feature_store
        self.persist_enabled = bool(persist) and feature_store is not None
        self._packets: dict[str, GroundedFactPacket] = {}
        self._entries: list[LedgerEntry] = []
        self._by_symbol: dict[str, list[LedgerEntry]] = {}
        self._by_metric_key: dict[tuple[str, str], list[str]] = {}
        self.donor = donor_grounding_status()

    # -- commit -----------------------------------------------------------
    def commit(
        self,
        symbol: str,
        metrics: Mapping[str, float],
        *,
        valid_time_ns: int,
        knowledge_ts_ns: int,
        as_of_ns: int | None = None,
        silo: str = "RING2",
        artifact_kind: str | None = None,
        payload: Mapping[str, Any] | None = None,
        persist: bool | None = None,
    ) -> GroundedFactPacket:
        """Seal ``metrics`` into a packet and register it.

        Raises
        ------
        TemporalIntegrityError
            If the packet would reference knowledge newer than ``as_of_ns``.
        GroundingDigestError
            If a packet with the same digest would carry different content.
        """
        facts = {str(name): float(value) for name, value in dict(metrics).items()}
        if not facts:
            raise ValueError("cannot commit an empty fact set")
        for name, value in facts.items():
            if not math.isfinite(value):
                raise ValueError(f"metric {name!r} is not finite ({value!r})")

        as_of = int(
            as_of_ns
            if as_of_ns is not None
            else max(int(valid_time_ns), int(knowledge_ts_ns))
        )
        packet = GroundedFactPacket(
            symbol=str(symbol).strip().upper(),
            valid_time_ns=int(valid_time_ns),
            knowledge_ts_ns=int(knowledge_ts_ns),
            as_of_ns=as_of,
            metrics=facts,
        )
        return self.append(packet, silo=silo, artifact_kind=artifact_kind, payload=payload, persist=persist)

    def append(
        self,
        packet: GroundedFactPacket,
        *,
        silo: str = "RING2",
        artifact_kind: str | None = None,
        payload: Mapping[str, Any] | None = None,
        persist: bool | None = None,
    ) -> GroundedFactPacket:
        if not isinstance(packet, GroundedFactPacket):
            raise TypeError(f"expected GroundedFactPacket, got {type(packet).__name__}")

        # ``GroundedFactPacket.__post_init__`` already computed and validated
        # the canonical digest, so re-hashing here would double the SHA-256 cost
        # of every scan for no security gain.  Tampering after construction is
        # caught by :meth:`verify_all` / :meth:`GroundedFactPacket.verify`.
        computed = str(packet.sha256_digest or "")
        if not computed:
            computed = packet.compute_digest()
        if len(computed) != 64 or any(c not in "0123456789abcdef" for c in computed):
            raise GroundingDigestError(
                f"UNGROUNDED-NUMERICAL-CLAIM: packet for {packet.symbol} "
                "does not carry a 64-char SHA-256 digest"
            )
        existing = self._packets.get(computed)
        if existing is not None:
            if existing.to_dict() != packet.to_dict():
                raise GroundingDigestError(
                    "SHA-256 collision with different content for " + packet.symbol
                )
            return existing

        entry = LedgerEntry(
            digest=computed,
            symbol=packet.symbol,
            committed_at_ns=time.time_ns(),
            metric_count=len(packet.metrics),
            as_of_ns=packet.as_of_ns,
        )
        self._packets[computed] = packet
        self._entries.append(entry)
        self._by_symbol.setdefault(packet.symbol, []).append(entry)
        for metric_name in packet.metrics:
            self._by_metric_key.setdefault((packet.symbol, metric_name), []).append(computed)

        should_persist = self.persist_enabled if persist is None else bool(persist)
        if should_persist and self.feature_store is not None:
            self.feature_store.append_alpha_factors(
                silo,
                packet.symbol,
                packet.as_of_ns,
                packet.metrics,
                knowledge_ts_ns=packet.knowledge_ts_ns,
                valid_time_ns=packet.valid_time_ns,
                source="grounding_ledger",
            )
            if artifact_kind is not None:
                self.feature_store.append_valuation(
                    silo,
                    packet.symbol,
                    packet.as_of_ns,
                    artifact_kind,
                    dict(payload or packet.metrics),
                    knowledge_ts_ns=packet.knowledge_ts_ns,
                    valid_time_ns=packet.valid_time_ns,
                )
        return packet

    # -- read -------------------------------------------------------------
    def get(self, digest: str) -> GroundedFactPacket:
        try:
            return self._packets[str(digest).strip().lower()]
        except KeyError as exc:
            raise KeyError(f"no grounded packet with digest {digest!r}") from exc

    def latest(self, symbol: str) -> GroundedFactPacket | None:
        entries = self._by_symbol.get(str(symbol).strip().upper(), [])
        return self.get(entries[-1].digest) if entries else None

    def packets(self) -> tuple[GroundedFactPacket, ...]:
        return tuple(self._packets.values())

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def history(self, symbol: str) -> tuple[LedgerEntry, ...]:
        return tuple(self._by_symbol.get(str(symbol).strip().upper(), ()))

    def metric_history(self, symbol: str, metric: str) -> tuple[int, ...]:
        digests = self._by_metric_key.get((str(symbol).strip().upper(), str(metric)), [])
        return tuple(self.get(d).as_of_ns for d in digests)

    def __len__(self) -> int:
        return len(self._packets)

    def __contains__(self, digest: object) -> bool:
        return str(digest).strip().lower() in self._packets

    # -- audit ------------------------------------------------------------
    def verify_all(self) -> int:
        """Recompute every digest; raises on the first tampered packet."""
        for digest, packet in self._packets.items():
            if packet.compute_digest() != digest:
                raise GroundingDigestError(
                    f"UNGROUNDED-NUMERICAL-CLAIM: ledger packet {packet.symbol} "
                    "no longer matches its committed digest"
                )
        return len(self._packets)

    def verify_thesis(
        self,
        thesis_text: str,
        packet: GroundedFactPacket | str,
        *,
        tolerance: float = 1e-4,
    ) -> "VerificationReport":
        resolved = self.get(packet) if isinstance(packet, str) else packet
        return NumericalGroundingVerifier(tolerance=tolerance).verify_thesis_claims(
            thesis_text, resolved
        )

    # -- convenience ------------------------------------------------------
    def commit_valuation(
        self,
        symbol: str,
        artifact_kind: str,
        payload: Mapping[str, Any],
        *,
        valid_time_ns: int,
        knowledge_ts_ns: int,
        as_of_ns: int | None = None,
        silo: str = "RING2",
    ) -> GroundedFactPacket:
        """Flatten a valuation artefact into numbers and commit both forms."""
        metrics: dict[str, float] = {}

        def _walk(prefix: str, value: Any, depth: int = 0) -> None:
            if depth > 3:
                return
            if isinstance(value, Mapping):
                for key, item in value.items():
                    _walk(f"{prefix}.{key}" if prefix else str(key), item, depth + 1)
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    _walk(f"{prefix}.{index}", item, depth + 1)
            elif isinstance(value, bool):
                metrics[prefix] = 1.0 if value else 0.0
            elif isinstance(value, (int, float)):
                metrics[prefix] = float(value)

        _walk("", payload)
        if not metrics:
            raise ValueError("valuation payload carries no numeric fields")
        return self.commit(
            symbol,
            metrics,
            valid_time_ns=valid_time_ns,
            knowledge_ts_ns=knowledge_ts_ns,
            as_of_ns=as_of_ns,
            silo=silo,
            artifact_kind=str(artifact_kind),
            payload=payload,
        )


# ---------------------------------------------------------------------------
# AST re-computation pass
# ---------------------------------------------------------------------------


class AstRecomputer:
    """Evaluate a numeric expression with a strictly whitelisted AST.

    Mirrors the harvested ``trading_r1/verification`` re-computation rule: the
    reviewer never trusts a stated result, it recomputes the expression.  Only
    literals and ``+ - * / **`` are permitted -- no names, calls, attributes,
    subscripts, comparisons or comprehensions.
    """

    _ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
    _ALLOWED_UNARY = (ast.UAdd, ast.USub)

    def evaluate(self, expression: str) -> float:
        text = str(expression).replace(",", "").strip()
        if not text:
            raise ValueError("empty arithmetic expression")
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"unparseable expression {expression!r}") from exc

        def _walk(node: ast.AST) -> float:
            if isinstance(node, ast.Expression):
                return _walk(node.body)
            if isinstance(node, ast.Constant):
                if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                    raise ValueError(f"non-numeric literal in {expression!r}")
                return float(node.value)
            if isinstance(node, ast.BinOp) and isinstance(node.op, self._ALLOWED_BINOPS):
                left = _walk(node.left)
                right = _walk(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    if right == 0.0:
                        raise ValueError(f"division by zero in {expression!r}")
                    return left / right
                return left ** right
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, self._ALLOWED_UNARY):
                operand = _walk(node.operand)
                return operand if isinstance(node.op, ast.UAdd) else -operand
            raise ValueError(f"disallowed syntax in {expression!r}")

        value = _walk(tree)
        if not math.isfinite(value):
            raise ValueError(f"non-finite result for {expression!r}")
        return float(value)


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"(?<![\w.])"
    r"(?P<sign>[-+])?"
    r"(?P<currency>[$₹€£])?"
    r"(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?P<suffix>\s?(?:%|bps|bp|x|×))?"
)

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

_EQUATION_RE = re.compile(
    r"(?P<expr>[-+(]?\s*\d[\d,]*(?:\.\d+)?"
    r"(?:\s*[-+*/]\s*[-+(]?\d[\d,]*(?:\.\d+)?\s*)+)"
    r"\s*=\s*(?P<stated>[-+]?\d[\d,]*(?:\.\d+)?)"
)

_LABEL_WORDS = frozenset(
    {
        "step",
        "law",
        "stage",
        "section",
        "figure",
        "fig",
        "tab",
        "table",
        "phase",
        "tier",
        "pillar",
        "rule",
        "chapter",
        "appendix",
        "q",
        "fiscal",
        "quarter",
    }
)

_CLAIM_KEYWORDS = (
    "price",
    "value",
    "valuation",
    "target",
    "fair",
    "dcf",
    "comps",
    "ev",
    "ebitda",
    "revenue",
    "margin",
    "net",
    "gross",
    "spread",
    "fee",
    "tax",
    "edge",
    "ratio",
    "yield",
    "vol",
    "sharpe",
    "p/e",
    "pe",
    "multiple",
    "bps",
    "basis",
    "return",
    "upside",
    "downside",
    "drawdown",
    "wacc",
    "beta",
    "alpha",
    "factor",
    "score",
    "notional",
    "position",
    "risk",
    "var",
    "carry",
    "coupon",
    "duration",
    "premium",
    "impact",
    "slippage",
    "liquidity",
)


@dataclass(slots=True, frozen=True)
class Claim:
    """One material numeric claim extracted from prose."""

    text: str
    value: float
    unit: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class VerificationReport:
    """Result of a successful grounding verification."""

    symbol: str
    digest: str
    claims_checked: int
    derived_expressions: int
    tolerance: float
    claims: tuple[Claim, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["claims"] = [claim.to_dict() for claim in self.claims]
        return payload


class NumericalGroundingVerifier:
    """AST + regex verifier for System 2 thesis prose."""

    def __init__(self, tolerance: float = 1e-4) -> None:
        if float(tolerance) <= 0.0:
            raise ValueError("tolerance must be positive")
        self.tolerance = float(tolerance)
        self.recomputer = AstRecomputer()
        self._date_spans: list[tuple[int, int]] = []

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _close(left: float, right: float, tolerance: float) -> bool:
        scale = max(1.0, abs(float(left)), abs(float(right)))
        return abs(float(left) - float(right)) <= tolerance * scale

    def _is_date_position(self, start: int, end: int, date_spans: Sequence[tuple[int, int]]) -> bool:
        return any(start >= low and end <= high for low, high in date_spans)

    def _is_structural(self, text: str, match: re.Match[str]) -> bool:
        token = str(match.group("num")).replace(",", "")
        try:
            value = float(token)
        except ValueError:  # pragma: no cover - regex guarantee
            return True
        suffix = (match.group("suffix") or "").strip()
        currency = match.group("currency") or ""
        start, end = match.span()

        # Dates (2026-03-15) and times are never market claims.
        if self._is_date_position(start, end, self._date_spans):
            return True

        # Filing forms: 10-K, 10-Q, 8-K, 13-F.
        if text[end : end + 2].upper() in ("-K", "-Q", "-F", "-C") and value in (8, 10, 13, 20):
            return True

        # Quarter labels (Q4) and fiscal ordinals.
        prefix = text[max(0, start - 1) : start].strip().lower()
        if prefix == "q" and 1 <= value <= 4:
            return True

        # Explicit structural labels: "Step 3", "Law 4", "Figure 2".
        preceding = text[max(0, start - 26) : start].strip().lower()
        words = re.findall(r"[a-z]+", preceding)
        if words and words[-1] in _LABEL_WORDS and not suffix and not currency:
            return True

        # Years.
        if not suffix and not currency and 1900 <= value <= 2100 and float(value).is_integer():
            return True

        # Small bare counts ("4 analysts", "3-8 trades") carry no valuation meaning.
        if not suffix and not currency and value <= 12 and float(value).is_integer():
            return True

        return False

    def _is_material(self, text: str, match: re.Match[str]) -> bool:
        """Is this a number a reader would treat as a factual claim?"""
        suffix = (match.group("suffix") or "").strip()
        currency = match.group("currency") or ""
        token = str(match.group("num")).replace(",", "")
        if currency or suffix:
            return True
        try:
            value = float(token)
        except ValueError:  # pragma: no cover
            return False
        if "." in token and len(token.split(".", 1)[1]) >= 2:
            return True
        window = text[max(0, match.start() - 28) : match.start()].lower()
        return any(keyword in window for keyword in _CLAIM_KEYWORDS)

    def extract_claims(self, thesis_text: str) -> list[Claim]:
        text = str(thesis_text)
        self._date_spans = [(m.start(), m.end()) for m in _DATE_RE.finditer(text)]
        claims: list[Claim] = []
        for match in _NUMBER_RE.finditer(text):
            if self._is_structural(text, match) or not self._is_material(text, match):
                continue
            token = str(match.group("num")).replace(",", "")
            try:
                value = float(token)
            except ValueError:  # pragma: no cover
                continue
            if match.group("sign") == "-":
                value = -value
            suffix = (match.group("suffix") or "").strip()
            currency = match.group("currency") or ""
            unit = "%" if suffix == "%" else ("bps" if suffix in ("bp", "bps") else ("x" if suffix in ("x", "×") else ("currency" if currency else "")))
            claims.append(
                Claim(
                    text=text[match.start() : match.end()],
                    value=float(value),
                    unit=unit,
                    start=match.start(),
                    end=match.end(),
                )
            )
        return claims

    def extract_equations(self, thesis_text: str) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for match in _EQUATION_RE.finditer(str(thesis_text)):
            expression = match.group("expr")
            stated = float(match.group("stated").replace(",", ""))
            out.append((expression, stated))
        return out

    # -- allowed-value construction ---------------------------------------
    def _allowed_values(
        self,
        packet: GroundedFactPacket,
        thesis_text: str,
    ) -> list[float]:
        metrics = dict(packet.metrics)
        allowed: list[float] = []
        for value in metrics.values():
            allowed.append(float(value))
            allowed.append(abs(float(value)))
            for scale in (100.0, 10_000.0, 0.01, 0.0001, 1_000_000.0, 1e-6):
                allowed.append(float(value) * scale)
            allowed.append(float(value) / 100.0)
            for multiplier in (0.5, 2.0, 3.0):
                allowed.append(float(value) * multiplier)

        # Canonical net-edge identity, recomputed rather than trusted.
        required = ("expected_gross_bps", "spread_bps", "fee_bps", "tax_bps", "q98_residual_bps")
        if all(name in metrics for name in required):
            allowed.append(
                net_lower_bound_bps(
                    metrics["expected_gross_bps"],
                    metrics["spread_bps"],
                    metrics["fee_bps"],
                    metrics["tax_bps"],
                    metrics["q98_residual_bps"],
                )
            )

        cited = self._cited_metric_names(metrics, thesis_text)
        values = [metrics[name] for name in cited]
        for i, left in enumerate(values):
            for right in values[i + 1 :]:
                allowed.append(left + right)
                allowed.append(left - right)
                allowed.append(right - left)
                allowed.append(abs(left - right))
                allowed.append(left * right)
                if right != 0.0:
                    allowed.append(left / right)
                if left != 0.0:
                    allowed.append(right / left)
        return allowed

    @staticmethod
    def _cited_metric_names(metrics: Mapping[str, float], thesis_text: str) -> list[str]:
        text = str(thesis_text).lower()
        cited = [name for name in metrics if name.lower() in text]
        return cited[:32]

    # -- verification -----------------------------------------------------
    def verify_thesis_claims(
        self,
        thesis_text: str,
        packet: GroundedFactPacket,
        tolerance: float | None = None,
    ) -> VerificationReport:
        """Verify every material number in ``thesis_text`` against ``packet``.

        Raises
        ------
        UngroundedNumericalClaim
            With ``UNGROUNDED-NUMERICAL-CLAIM`` in the message whenever a figure
            is fabricated, unhashed, or arithmetically inconsistent.
        """
        if not isinstance(packet, GroundedFactPacket):
            raise TypeError(
                f"expected GroundedFactPacket, got {type(packet).__name__}"
            )
        tol = self.tolerance if tolerance is None else float(tolerance)
        if tol <= 0.0:
            raise ValueError("tolerance must be positive")

        if not packet.verify():
            raise UngroundedNumericalClaim(
                f"{UNGROUNDED_CLAIM_CODE}: packet digest for {packet.symbol} "
                "does not match its payload"
            )
        if len(packet.sha256_digest) != 64:
            raise UngroundedNumericalClaim(
                f"{UNGROUNDED_CLAIM_CODE}: packet for {packet.symbol} is unhashed"
            )
        if not packet.metrics:
            raise UngroundedNumericalClaim(
                f"{UNGROUNDED_CLAIM_CODE}: packet for {packet.symbol} carries no metrics"
            )

        text = str(thesis_text)
        equation_count = 0
        for expression, stated in self.extract_equations(text):
            equation_count += 1
            try:
                recomputed = self.recomputer.evaluate(expression)
            except ValueError as exc:
                raise UngroundedNumericalClaim(
                    f"{UNGROUNDED_CLAIM_CODE}: cannot recompute {expression!r} ({exc})"
                ) from exc
            if not self._close(recomputed, stated, tol):
                raise UngroundedNumericalClaim(
                    f"{UNGROUNDED_CLAIM_CODE}: arithmetic mismatch -- "
                    f"{expression.strip()} = {recomputed:g}, thesis states {stated:g}"
                )

        claims = self.extract_claims(text)
        allowed = self._allowed_values(packet, text)
        for claim in claims:
            if not any(self._close(claim.value, candidate, tol) for candidate in allowed):
                raise UngroundedNumericalClaim(
                    f"{UNGROUNDED_CLAIM_CODE}: figure {claim.text.strip()!r} "
                    f"(={claim.value:g}) is absent from the grounding ledger for "
                    f"{packet.symbol} (digest {packet.sha256_digest[:12]}...)"
                )

        return VerificationReport(
            symbol=packet.symbol,
            digest=packet.sha256_digest,
            claims_checked=len(claims),
            derived_expressions=equation_count,
            tolerance=tol,
            claims=tuple(claims),
        )


def verify_thesis_claims(
    thesis_text: str,
    packet: GroundedFactPacket,
    tolerance: float = 1e-4,
) -> VerificationReport:
    """Module-level convenience wrapper around the verifier."""
    return NumericalGroundingVerifier(tolerance=tolerance).verify_thesis_claims(
        thesis_text, packet
    )
